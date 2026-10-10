import copy
import json
import tempfile
import unittest
from pathlib import Path

from research.pb01_pipeline import (
    REQUIRED_ROLES, MARKET_GATE, LATER_ROLES, sha256, validate_input_manifest,
    validate_freeze_package, parent_transition, current_preflight, metadata,
)


class PipelineBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def ref(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return {"path": name, "sha256": sha256(path)}

    def candidates(self):
        ref = self.ref("synthetic-evidence.json", {"synthetic": True})
        def row(rid):
            return {"id": rid, **{k: ref for k in ("source", "unit_basis", "version_basis", "rights_basis", "coverage_basis", "publication_basis")},
                    "coverage_start": "2026-09-01T00:00:00+08:00",
                    "coverage_end": "2026-10-30T23:59:00+08:00",
                    "exact_version_available_at": "2026-09-01T00:00:00+08:00"}
        return {"schema": "regis.pb01.input_candidates.v1",
                "roles": [row(rid) for rid in REQUIRED_ROLES],
                "market_observation": row(MARKET_GATE)}

    def executions(self):
        return [{"id": "synthetic-entry", "signal_decision_at": "2026-09-15T15:05:00+08:00",
                 "execution_at": "2026-09-16T09:35:20+08:00"}]

    def outcomes(self):
        return [{"id": "synthetic-etf-result", "signal_decision_at": "2026-09-15T15:05:00+08:00",
                 "outcome_at": "2026-09-16T14:55:20+08:00"}]

    def benchmark(self, manifest, **kwargs):
        kwargs.setdefault("decisions", ["2026-09-15T15:05:00+08:00"])
        kwargs.setdefault("review_at", "2026-09-17T10:00:00+08:00")
        kwargs.setdefault("etf_role", "benchmark_outcome")
        kwargs.setdefault("benchmark_outcome_events", self.outcomes())
        return validate_input_manifest(self.root, manifest, **kwargs)

    def test_late_etf_result_binds_at_review_without_becoming_signal_or_execution_evidence(self):
        manifest = self.candidates()
        etf = next(r for r in manifest["roles"] if r["id"] == "etf_daily")
        etf["exact_version_available_at"] = "2026-09-16T15:30:00+08:00"
        result = self.benchmark(manifest, execution_events=self.executions())
        self.assertTrue(result["candidate_metadata_valid"])
        row = next(r for r in result["roles"] if r["id"] == "etf_daily")
        self.assertEqual(row["clock_role"], "benchmark_result_review")
        self.assertFalse(row["signal_feature_availability_checked"])
        self.assertFalse(row["historical_feature_PIT"])
        self.assertFalse(row["execution_reachability_verified"])
        self.assertTrue(row["benchmark_semantic_review_pending"])
        self.assertFalse(result["benchmark_result_usable_as_signal_feature"])
        self.assertFalse(result["data_admitted"])
        self.assertFalse(result["economic_admitted"])
        signal = self.validate(manifest, decisions=["2026-09-15T15:05:00+08:00"],
                               execution_events=self.executions(), review_at="2026-09-17T10:00:00+08:00")
        self.assertFalse(signal["candidate_metadata_valid"])
        self.assertIn("exact_version_after_decision", next(r for r in signal["roles"] if r["id"] == "etf_daily")["rejection_reasons"])

    def test_etf_result_requires_review_and_linked_outcomes_not_just_later_decision(self):
        manifest = self.candidates()
        for args, message in (({"review_at": None}, "review_required"),
                              ({"benchmark_outcome_events": None}, "clocks_required"),
                              ({"benchmark_outcome_events": []}, "clocks_required"),
                              ({"review_at": "2026-09-16T14:55:19+08:00"}, "review_before_benchmark")):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, message):
                self.benchmark(manifest, **args)

    def test_etf_result_duplicate_unlinked_reversed_missing_signal_and_unknown_role_reject(self):
        manifest = self.candidates()
        duplicate = self.outcomes() * 2
        unlinked = self.outcomes()
        unlinked[0]["signal_decision_at"] = "2026-09-14T15:05:00+08:00"
        reversed_event = self.outcomes()
        reversed_event[0]["outcome_at"] = "2026-09-15T15:05:00+08:00"
        for events in (duplicate, unlinked, reversed_event):
            with self.subTest(events=events), self.assertRaises(ValueError):
                self.benchmark(manifest, benchmark_outcome_events=events)
        with self.assertRaisesRegex(ValueError, "signal_coverage_missing"):
            self.benchmark(manifest, decisions=["2026-09-15T15:05:00+08:00", "2026-09-14T15:05:00+08:00"])
        with self.assertRaisesRegex(ValueError, "etf_role_unknown"):
            self.benchmark(manifest, etf_role="pretend_result")
        with self.assertRaisesRegex(ValueError, "cannot_be_signal_features"):
            self.benchmark(manifest, etf_role="signal_feature")

    def test_etf_result_review_and_coverage_do_not_use_signal_or_review_date_as_result_date(self):
        manifest = self.candidates()
        etf = next(r for r in manifest["roles"] if r["id"] == "etf_daily")
        etf["coverage_start"] = "2026-09-16T00:00:00+08:00"
        etf["coverage_end"] = "2026-09-16T23:59:00+08:00"
        etf["exact_version_available_at"] = "2026-09-16T15:30:00+08:00"
        self.assertTrue(next(r for r in self.benchmark(manifest)["roles"] if r["id"] == "etf_daily")["metadata_binding_valid"])
        etf["coverage_end"] = "2026-09-16T14:55:19+08:00"
        row = next(r for r in self.benchmark(manifest)["roles"] if r["id"] == "etf_daily")
        self.assertIn("decision_outside_evidence_coverage", row["rejection_reasons"])
        etf["coverage_end"] = "2026-09-16T23:59:00+08:00"
        etf["exact_version_available_at"] = "2026-09-17T10:00:01+08:00"
        self.assertFalse(next(r for r in self.benchmark(manifest)["roles"] if r["id"] == "etf_daily")["metadata_binding_valid"])

    def test_result_mode_cannot_relax_future_quotes_or_stock_signal_snapshots(self):
        for rid, timestamp in (("minute_execution_quotes", "2026-09-16T09:35:21+08:00"),
                               ("stock_daily", "2026-09-15T15:05:01+08:00")):
            manifest = self.candidates()
            next(r for r in manifest["roles"] if r["id"] == rid)["exact_version_available_at"] = timestamp
            result = self.benchmark(manifest, execution_events=self.executions())
            row = next(r for r in result["roles"] if r["id"] == rid)
            self.assertFalse(row["metadata_binding_valid"])
            self.assertIn("exact_version_after_decision", row["rejection_reasons"])

    def test_independent_etf_result_requires_only_etf_component_and_does_not_admit_missing_stock(self):
        manifest = self.candidates()
        manifest.pop("market_observation")
        wanted = {"etf_daily", "ETF_complete_actions", "exchange_calendar", "rights_and_retention", "historical_publication_evidence"}
        manifest["roles"] = [r for r in manifest["roles"] if r["id"] in wanted]
        next(r for r in manifest["roles"] if r["id"] == "exchange_calendar")["exact_version_available_at"] = "2026-09-17T09:59:00+08:00"
        result = self.benchmark(manifest, scope="etf_benchmark_outcome")
        self.assertTrue(result["candidate_metadata_valid"])
        self.assertFalse(result["market_observation_required"])
        self.assertEqual({r["id"] for r in result["roles"]}, wanted)
        calendar = next(r for r in result["roles"] if r["id"] == "exchange_calendar")
        self.assertEqual(calendar["clock_role"], "benchmark_calendar_review")
        self.assertFalse(calendar["signal_feature_availability_checked"])
        self.assertIn("stock_daily", result["omitted_roles_are_not_admitted"])
        self.assertIn("minute_execution_quotes", result["omitted_roles_are_not_admitted"])
        self.assertFalse(result["data_admitted"])
        self.assertFalse(result["real_replay_enabled"])
        with self.assertRaisesRegex(ValueError, "requires_outcome_role"):
            self.benchmark(manifest, scope="etf_benchmark_outcome", etf_role="signal_feature", benchmark_outcome_events=None)
        with self.assertRaisesRegex(ValueError, "input_manifest_shape_invalid"):
            self.benchmark(manifest, scope="original_M24_replay")

    def validate(self, manifest, **kwargs):
        kwargs.setdefault("review_at", "2026-09-16T09:36:00+08:00")
        return validate_input_manifest(self.root, manifest, **kwargs)

    def historical_model(self):
        rule = self.ref("synthetic-policy.json", {"synthetic": True})
        return self.ref("historical-model.json", {
            "schema": "regis.pb01.historical_execution_model.v1",
            "scope": "original_M24_replay",
            "declared_frozen_at": "2026-09-16T09:35:30+08:00",
            **{k: rule for k in ("latency_rule", "matching_rule", "cash_availability_rule",
                                "failure_and_delayed_exit_rule", "search_and_exposure_ledger")}})

    def test_historical_clocks_do_not_require_personal_past_orders_or_become_observed(self):
        ref = self.historical_model()
        result = self.validate(self.candidates(), decisions=["2026-09-15T15:05:00+08:00"],
            execution_events=self.executions(), execution_kind="historical_simulation", historical_model_ref=ref)
        self.assertTrue(result["candidate_metadata_valid"])
        self.assertEqual(result["execution_observation_count"], 0)
        self.assertEqual(result["simulated_execution_clock_count"], 1)
        self.assertFalse(result["historical_model"]["personal_execution_or_credit_required"])
        self.assertFalse(result["historical_model"]["frozen_protocol_verified"])
        self.assertTrue(result["historical_quote_and_model_semantics_review_pending"])
        for key in ("data_admitted", "economic_admitted", "production_eligible", "real_replay_enabled"):
            self.assertFalse(result[key])
        with self.assertRaisesRegex(ValueError, "cannot_be_observed"):
            self.validate(self.candidates(), decisions=["2026-09-15T15:05:00+08:00"], historical_model_ref=ref)

    def test_historical_mode_requires_bound_all_policies_and_cannot_use_future_quote(self):
        manifest = self.candidates()
        with self.assertRaisesRegex(ValueError, "historical_model_and_review"):
            self.validate(manifest, decisions=["2026-09-15T15:05:00+08:00"], execution_kind="historical_simulation")
        ref = self.historical_model()
        quote = next(r for r in manifest["roles"] if r["id"] == "minute_execution_quotes")
        quote["exact_version_available_at"] = "2026-09-16T09:35:21+08:00"
        result = self.validate(manifest, decisions=["2026-09-15T15:05:00+08:00"],
            execution_events=self.executions(), execution_kind="historical_simulation", historical_model_ref=ref)
        self.assertFalse(result["candidate_metadata_valid"])
        self.assertIn("exact_version_after_decision", result["roles"][-2]["rejection_reasons"])
        (self.root/"synthetic-policy.json").write_text('{}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "reference_missing_or_changed"):
            self.validate(self.candidates(), decisions=["2026-09-15T15:05:00+08:00"],
                execution_events=self.executions(), execution_kind="historical_simulation", historical_model_ref=ref)

    def test_historical_model_future_freeze_missing_ledger_and_unknown_kind_reject(self):
        for bad in ("future", "ledger", "kind"):
            ref = self.historical_model()
            model = json.loads((self.root/ref["path"]).read_text())
            if bad == "future": model["declared_frozen_at"] = "2026-09-16T09:36:01+08:00"
            elif bad == "ledger": del model["search_and_exposure_ledger"]
            ref = self.ref("historical-model.json", model)
            with self.assertRaises(ValueError):
                self.validate(self.candidates(), decisions=["2026-09-15T15:05:00+08:00"],
                    execution_events=self.executions(), execution_kind="unknown" if bad=="kind" else "historical_simulation",
                    historical_model_ref=ref)

    def test_historical_freeze_has_distinct_model_reference_and_retains_original_limits(self):
        package = self.freeze()
        package["execution_level"] = "minute_side_size_manual_latency_simulation"
        with self.assertRaises(ValueError): validate_freeze_package(self.root, package)
        package["historical_model_ref"] = self.historical_model()
        result = validate_freeze_package(self.root, package)
        self.assertTrue(result["candidate_structure_valid"])
        self.assertFalse(result["personal_execution_or_credit_required"])
        self.assertFalse(result["protocol_frozen"])
        self.assertFalse(result["real_replay_enabled"])
        package["execution_level"] = "daily_open"
        del package["historical_model_ref"]
        self.assertIn("execution_level_incompatible_with_original_M24",
                      validate_freeze_package(self.root, package)["rejection_reasons"])

    def test_binding_pass_does_not_admit_synthetic_or_grant_rights(self):
        result = self.validate(self.candidates(), decisions=["2026-09-15T15:05:00+08:00"], execution_events=self.executions())
        self.assertTrue(result["candidate_metadata_valid"])
        for flag in ("data_admitted", "economic_admitted", "production_eligible", "real_replay_enabled"):
            self.assertIs(result[flag], False)
        self.assertEqual(result["later_roles_do_not_block_pb01"], list(LATER_ROLES))

    def test_next_day_quote_uses_matching_clock_not_previous_signal_or_submission(self):
        manifest = self.candidates()
        quote = next(r for r in manifest["roles"] if r["id"] == "minute_execution_quotes")
        # Submission is 09:35:10; a new observed quote at :15 can match at :20.
        quote["exact_version_available_at"] = "2026-09-16T09:35:15+08:00"
        quote["coverage_start"] = "2026-09-16T09:35:00+08:00"
        result = self.validate(manifest,
            decisions=["2026-09-15T15:05:00+08:00"], execution_events=self.executions())
        self.assertTrue(result["candidate_metadata_valid"])
        self.assertEqual(result["roles"][-2]["clock_role"], "execution_matching")
        self.assertFalse(result["real_replay_enabled"])
        quote["exact_version_available_at"] = "2026-09-16T09:35:20.000001+08:00"
        result = self.validate(manifest,
            decisions=["2026-09-15T15:05:00+08:00"], execution_events=self.executions())
        self.assertFalse(result["candidate_metadata_valid"])

    def test_missing_execution_clocks_closes_original_replay_only(self):
        manifest = self.candidates()
        result = validate_input_manifest(self.root, manifest, decisions=["2026-09-15T15:05:00+08:00"])
        self.assertIn("actual_execution_clocks_required", result["roles"][-2]["rejection_reasons"])
        self.assertFalse(result["candidate_metadata_valid"])
        manifest["roles"] = [r for r in manifest["roles"] if r["id"] not in {
            "minute_execution_quotes", "etf_daily", "ETF_complete_actions"}]
        signal = self.validate(manifest,
            decisions=["2026-09-15T15:05:00+08:00"], scope="pb01_stock_signal")
        self.assertTrue(signal["candidate_metadata_valid"])
        self.assertFalse(signal["data_admitted"])
        self.assertIn("etf_daily", signal["omitted_roles_are_not_admitted"])
        self.assertFalse(self.validate(manifest,
            decisions=["2026-09-15T15:05:00+08:00"], scope="pb01_daily_diagnostic")["candidate_metadata_valid"])

    def test_unlinked_before_signal_duplicate_or_naive_execution_clock_rejects(self):
        for mutation in ("unlinked", "earlier", "duplicate", "naive", "unknown"):
            with self.subTest(mutation=mutation):
                events = self.executions()
                if mutation == "unlinked": events[0]["signal_decision_at"] = "2026-09-14T15:05:00+08:00"
                elif mutation == "earlier": events[0]["execution_at"] = events[0]["signal_decision_at"]
                elif mutation == "duplicate": events.append(copy.deepcopy(events[0]))
                elif mutation == "naive": events[0]["execution_at"] = "2026-09-16T09:35:20"
                else: events[0]["unknown"] = True
                with self.assertRaises(ValueError):
                    validate_input_manifest(self.root, self.candidates(),
                        decisions=["2026-09-15T15:05:00+08:00"], execution_events=events)

    def test_current_permission_is_reviewed_at_research_time_not_historical_signal(self):
        manifest = self.candidates()
        right = next(r for r in manifest["roles"] if r["id"] == "rights_and_retention")
        right["exact_version_available_at"] = "2026-09-16T09:35:30+08:00"
        right["coverage_start"] = "2026-09-16T09:35:30+08:00"
        result = self.validate(manifest, decisions=["2026-09-15T15:05:00+08:00"], execution_events=self.executions())
        self.assertTrue(result["candidate_metadata_valid"])
        self.assertFalse(result["data_admitted"])
        self.assertEqual(next(r for r in result["roles"] if r["id"] == right["id"])["clock_role"], "research_permission_review")
        right["exact_version_available_at"] = "2026-09-16T09:36:00.000001+08:00"
        self.assertFalse(self.validate(manifest, decisions=["2026-09-15T15:05:00+08:00"], execution_events=self.executions())["candidate_metadata_valid"])

    def test_post_signal_action_envelope_never_certifies_earlier_features_or_cash(self):
        manifest = self.candidates()
        for row in manifest["roles"]:
            if row["id"] in {"adjustment_and_actions", "ETF_complete_actions", "historical_publication_evidence"}:
                row["exact_version_available_at"] = "2026-09-20T18:00:00+08:00"
                row["coverage_end"] = "2026-09-20T23:59:00+08:00"
        result = self.validate(manifest, decisions=["2026-09-15T15:05:00+08:00"],
            execution_events=self.executions(), review_at="2026-09-21T09:00:00+08:00")
        self.assertTrue(result["candidate_metadata_valid"])
        for row in result["roles"]:
            if row["event_semantic_review_pending"]:
                self.assertFalse(row["signal_feature_availability_checked"])
                self.assertFalse(row["action_envelope_usable_as_signal_feature"])
                self.assertFalse(row["independent_data_review_passed"])
        self.assertFalse(result["economic_admitted"])

    def test_actual_review_clock_required_and_cannot_precede_observed_events(self):
        manifest = self.candidates()
        result = self.validate(manifest, decisions=["2026-09-15T15:05:00+08:00"], execution_events=self.executions(), review_at=None)
        self.assertFalse(result["candidate_metadata_valid"])
        for row in result["roles"]:
            if row["id"] == "rights_and_retention":
                self.assertIn("actual_research_review_clock_required", row["rejection_reasons"])
        with self.assertRaisesRegex(ValueError, "review_before_observed_execution"):
            self.validate(manifest, decisions=["2026-09-15T15:05:00+08:00"], execution_events=self.executions(), review_at="2026-09-16T09:35:19+08:00")

    def test_late_version_cannot_be_excused_by_changing_manifest_decision(self):
        manifest = self.candidates()
        manifest["roles"][0]["exact_version_available_at"] = "2026-10-02T02:40:38+00:00"
        result = validate_input_manifest(self.root, manifest, decisions=["2026-09-15T15:05:00+08:00"])
        self.assertIn("exact_version_after_decision", result["roles"][0]["rejection_reasons"])
        self.assertFalse(result["candidate_metadata_valid"])

    def test_unknown_role_duplicate_unknown_field_missing_role_and_missing_market(self):
        for mutation in ("unknown", "duplicate", "field", "missing", "market"):
            with self.subTest(mutation=mutation):
                m = self.candidates()
                if mutation == "unknown":
                    m["roles"][0]["id"] = "raw_price_override"
                elif mutation == "duplicate":
                    m["roles"].append(copy.deepcopy(m["roles"][0]))
                elif mutation == "field":
                    m["roles"][0]["verified"] = True
                elif mutation == "missing":
                    m["roles"].pop()
                else:
                    m["market_observation"] = None
                if mutation in ("unknown", "duplicate"):
                    with self.assertRaises(ValueError):
                        validate_input_manifest(self.root, m, decisions=["2026-09-15T15:05:00+08:00"])
                else:
                    self.assertFalse(validate_input_manifest(self.root, m, decisions=["2026-09-15T15:05:00+08:00"])["candidate_metadata_valid"])

    def test_missing_changed_basis_or_naive_clock_rejects(self):
        m = self.candidates()
        (self.root / "synthetic-evidence.json").write_text("{}")
        self.assertFalse(validate_input_manifest(self.root, m, decisions=["2026-09-15T15:05:00+08:00"])["candidate_metadata_valid"])
        with self.assertRaises(ValueError):
            validate_input_manifest(self.root, m, decisions=["2026-09-15T15:05:00"])
        with self.assertRaises(ValueError):
            validate_input_manifest(self.root, m, decisions=[])

    def freeze(self):
        # Synthetic calendar tests interface logic, never an actual market calendar.
        days = [f"2026-09-{i:02}" for i in range(1, 31)]
        basis = self.ref("basis.json", {"synthetic": True})
        split = {"train": days[:4], "validation": days[10:14], "test": days[20:24]}
        labels = [{"decision_date": origin, "last_outcome_date": days[days.index(origin) + 2]}
                  for origins in split.values() for origin in origins]
        return {"schema": "regis.pb01.freeze_candidate.v1", "input_manifest_ref": basis,
                "calendar_ref": self.ref("calendar.json", {"trading_dates": days}),
                "exposure_ref": basis, "costs_ref": basis, "market_gate_ref": basis,
                "label_intervals_ref": self.ref("labels.json", {"intervals": labels}),
                "split": split,
                "configurations": [{"id": "synthetic-1", "family": "synthetic", "horizon": 1, "parameters_sha256": "a"*64}],
                "execution_level": "minute_side_size_manual_event_evidence", "embargo_sessions": 5}

    def test_structural_freeze_pass_never_claims_real_freeze_or_unseen_OOS(self):
        r = validate_freeze_package(self.root, self.freeze())
        self.assertTrue(r["candidate_structure_valid"])
        self.assertEqual(r["label_interval_coverage"], {"expected_origin_count": 12,
                         "provided_interval_count": 12, "complete_unique_origin_coverage": True})
        self.assertFalse(r["protocol_frozen"])
        self.assertFalse(r["unseen_out_of_sample"])
        self.assertFalse(r["economic_admitted"])

    def test_daily_open_replacement_missing_basis_and_overlap_are_rejected(self):
        for mutation in ("daily", "missing", "overlap", "embargo", "configs"):
            with self.subTest(mutation=mutation):
                p = self.freeze()
                if mutation == "daily": p["execution_level"] = "daily_open_proxy"
                elif mutation == "missing": p["costs_ref"] = None
                elif mutation == "overlap": p["split"]["validation"] = p["split"]["train"]
                elif mutation == "embargo": p["embargo_sessions"] = 0
                else: p["configurations"] = [{"id": str(i), "family": "x", "horizon": 1} for i in range(13)]
                self.assertFalse(validate_freeze_package(self.root, p)["candidate_structure_valid"])

    def test_delayed_labels_crossing_split_and_long_span_reject(self):
        p = self.freeze()
        labels = json.loads((self.root/p["label_intervals_ref"]["path"]).read_text())["intervals"]
        labels[0]["last_outcome_date"] = "2026-09-13"
        p["label_intervals_ref"] = self.ref("long-labels.json", {"intervals": labels})
        self.assertFalse(validate_freeze_package(self.root, p)["candidate_structure_valid"])

    def test_missing_label_in_any_partition_rejects_without_dropping_decision(self):
        for partition in ("train", "validation", "test"):
            with self.subTest(partition=partition):
                p = self.freeze()
                labels = json.loads((self.root/p["label_intervals_ref"]["path"]).read_text())["intervals"]
                labels = [row for row in labels if row["decision_date"] != p["split"][partition][-1]]
                p["label_intervals_ref"] = self.ref("missing-label.json", {"intervals": labels})
                result = validate_freeze_package(self.root, p)
                self.assertFalse(result["candidate_structure_valid"])
                self.assertFalse(result["label_interval_coverage"]["complete_unique_origin_coverage"])
                self.assertEqual(result["label_interval_coverage"]["provided_interval_count"], 11)

    def test_one_label_cannot_validate_twelve_declared_origins(self):
        p = self.freeze()
        p["label_intervals_ref"] = self.ref("single-label.json", {"intervals": [
            {"decision_date": "2026-09-01", "last_outcome_date": "2026-09-03"}]})
        result = validate_freeze_package(self.root, p)
        self.assertFalse(result["candidate_structure_valid"])
        self.assertEqual(result["label_interval_coverage"]["expected_origin_count"], 12)
        self.assertEqual(result["label_interval_coverage"]["provided_interval_count"], 1)

    def test_duplicate_label_cannot_replace_missing_origin_or_hide_conflict(self):
        for conflict in (False, True):
            with self.subTest(conflict=conflict):
                p = self.freeze()
                labels = json.loads((self.root/p["label_intervals_ref"]["path"]).read_text())["intervals"]
                labels[-1] = dict(labels[0])
                if conflict:
                    labels[-1]["last_outcome_date"] = "2026-09-04"
                p["label_intervals_ref"] = self.ref("duplicate-label.json", {"intervals": labels})
                self.assertFalse(validate_freeze_package(self.root, p)["candidate_structure_valid"])

    def test_calendar_date_outside_split_cannot_replace_required_origin(self):
        p = self.freeze()
        labels = json.loads((self.root/p["label_intervals_ref"]["path"]).read_text())["intervals"]
        labels[-1] = {"decision_date": "2026-09-07", "last_outcome_date": "2026-09-09"}
        p["label_intervals_ref"] = self.ref("outside-label.json", {"intervals": labels})
        self.assertFalse(validate_freeze_package(self.root, p)["candidate_structure_valid"])

    def test_label_outcome_after_last_decision_is_not_a_missing_origin(self):
        p = self.freeze()
        labels = json.loads((self.root/p["label_intervals_ref"]["path"]).read_text())["intervals"]
        self.assertGreater(labels[-1]["last_outcome_date"], p["split"]["test"][-1])
        result = validate_freeze_package(self.root, p)
        self.assertTrue(result["candidate_structure_valid"])
        self.assertFalse(result["protocol_frozen"])
        self.assertFalse(result["economic_admitted"])

    def test_each_family_maximum_four_and_unknown_configuration_fields_reject(self):
        p = self.freeze()
        p["configurations"] = [{"id": str(i), "family": "x", "horizon": 1, "parameters_sha256": "a"*64} for i in range(5)]
        self.assertIn("per_family_configuration_budget_exceeded", validate_freeze_package(self.root, p)["rejection_reasons"])
        p = self.freeze(); p["configurations"][0]["extra"] = "ignored"
        self.assertFalse(validate_freeze_package(self.root, p)["candidate_structure_valid"])

    def test_parent_does_not_stop_on_checkpoint_or_blockers_while_work_remains(self):
        self.assertEqual(parent_transition(checkpoint_passed=True, independent_actions=["next"], external_blockers=["rights"]), "continue_next_authorized_dependency")
        self.assertEqual(parent_transition(checkpoint_passed=True, independent_actions=[], external_blockers=[]), "continue_parent_review")
        self.assertEqual(parent_transition(checkpoint_passed=True, independent_actions=[], external_blockers=["rights"]), "external_dependency_handoff")
        self.assertEqual(parent_transition(checkpoint_passed=False, independent_actions=["next"], external_blockers=[], user_stopped=True), "user_stopped")

    def test_json_duplicate_and_path_escape_reject(self):
        (self.root / "dup.json").write_text('{"x":1,"x":2}')
        with self.assertRaises(ValueError): metadata(self.root, "dup.json")
        with self.assertRaises(ValueError): metadata(self.root, "../outside.json")

    def test_future_file_presence_does_not_grant_admission(self):
        (self.root / "research").mkdir()
        matrix = {"roles": [{"id": rid, "status": "verified", "required_fields": "x", "coverage_required": "x", "unblock": "x"} for rid in (*REQUIRED_ROLES, *LATER_ROLES)]}
        self.ref("research/GOAL_DATA_EXECUTION_MATRIX_20261001.json", matrix)
        self.ref("QUANT_RESEARCH_M24_PROTOCOL_20261001.json", {"initial_capital_yuan": 150000, "horizons_sessions_after_entry": [1, 3, 5]})
        self.ref("research/GOAL_REAL_EXPERIMENT_PROTOCOL_20261001.json", {"status": "passed"})
        r = current_preflight(self.root)
        self.assertTrue(r["future_evidence"][0]["present"])
        self.assertFalse(r["future_evidence"][0]["semantic_admission_verified"])
        self.assertFalse(r["parent_delivery_complete"])
        self.assertFalse(r["economic_admitted"])


if __name__ == "__main__":
    unittest.main()
