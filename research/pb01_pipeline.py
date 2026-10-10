"""Read-only PB01 evidence interfaces. Structural checks never grant trading rights."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

REQUIRED_ROLES = (
    "stock_daily", "etf_daily", "PIT_universe_L_D_P_BSE", "PIT_ST",
    "PIT_suspension", "historical_limits", "adjustment_and_actions",
    "ETF_complete_actions", "exchange_calendar", "rights_and_retention",
    "historical_publication_evidence", "minute_execution_quotes",
)
LATER_ROLES = ("PIT_sector_membership", "attention_event_truth")
MARKET_GATE = "independent_market_observation"
SIGNAL_ROLES = tuple(r for r in REQUIRED_ROLES if r not in (
    "etf_daily", "ETF_complete_actions", "minute_execution_quotes"))
INPUT_SCOPES = {
    "pb01_stock_signal": SIGNAL_ROLES,
    "pb01_daily_diagnostic": tuple(r for r in REQUIRED_ROLES if r != "minute_execution_quotes"),
    "original_M24_replay": REQUIRED_ROLES,
    # A fixed ETF result component does not consume stock signals or breadth.
    # This binds result metadata; it is not an executable ETF replay.
    "etf_benchmark_outcome": ("etf_daily", "ETF_complete_actions",
                              "exchange_calendar", "rights_and_retention",
                              "historical_publication_evidence"),
}
# These envelopes need row/event semantic review; they are not signal features.
EVENT_EVIDENCE_ROLES = ("adjustment_and_actions", "ETF_complete_actions", "historical_publication_evidence")
FUTURE = {
    "N5": "research/GOAL_REAL_EXPERIMENT_PROTOCOL_20261001.json",
    "N6": "research/GOAL_M24_REAL_REPLAY_20261001.json",
    "N7": "research/GOAL_ECONOMIC_SCREEN_20261001.json",
    "N8": "research/GOAL_FORWARD_ADMISSION_20261001.json",
}
BOUNDARY = {
    "economic_admitted": False, "production_eligible": False,
    "generates_alert": False, "auto_orders": False, "account_access": False,
    "real_replay_enabled": False,
}


def sha256(path):
    with Path(path).open("rb") as stream:
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
        return digest.hexdigest()


def project_path(root, name):
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError("metadata_path_invalid")
    root = Path(root).resolve()
    path = (root / name).resolve()
    if path == root or root not in path.parents or ".." in Path(name).parts:
        raise ValueError("metadata_path_outside_project")
    return path


def _object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("metadata_duplicate_key")
        obj[key] = value
    return obj


def metadata(root, name):
    path = project_path(root, name)
    # The long-lived task accumulates evidence references across checkpoints.
    # Give that exact ledger a bounded 1 MiB envelope; ordinary data metadata
    # remains limited to 256 kB and keeps duplicate/nonfinite validation.
    limit = 1_048_576 if name == "WORKFLOW_TASK.json" else 256_000
    if path.stat().st_size > limit or path.suffix.lower() != ".json":
        raise ValueError("metadata_size_or_type_invalid")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("metadata_nonfinite")))


def aware(value):
    if not isinstance(value, str):
        raise ValueError("timestamp_missing")
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("timestamp_invalid") from None
    if result.utcoffset() is None:
        raise ValueError("timestamp_timezone_missing")
    return result


def reference(root, ref):
    """Bind bytes; an authentic-looking file is not verified market or legal evidence."""
    if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
        raise ValueError("reference_shape_invalid")
    if not isinstance(ref["sha256"], str) or not re.fullmatch("[0-9a-f]{64}", ref["sha256"]):
        raise ValueError("reference_hash_invalid")
    path = project_path(root, ref["path"])
    if not path.is_file() or sha256(path) != ref["sha256"]:
        raise ValueError("reference_missing_or_changed")
    return {"path": ref["path"], "sha256": ref["sha256"]}


def validate_historical_model_reference(root, ref, *, review_at=None):
    """Bind a declared historical model, without certifying it frozen or true.

    Its clocks and cash are counterfactual. Real historical side/size, market
    states and independent model review are still required for economic use.
    Personal past orders or cash statements are not historical-model inputs.
    """
    bound = reference(root, ref)
    model = metadata(root, bound["path"])
    policies = ("latency_rule", "matching_rule", "cash_availability_rule",
                "failure_and_delayed_exit_rule", "search_and_exposure_ledger")
    if (set(model) != {"schema", "scope", "declared_frozen_at", *policies}
            or model["schema"] != "regis.pb01.historical_execution_model.v1"
            or model["scope"] != "original_M24_replay"):
        raise ValueError("historical_model_shape_or_scope_invalid")
    frozen = aware(model["declared_frozen_at"])
    if review_at is not None and frozen > aware(review_at):
        raise ValueError("historical_model_after_review")
    refs = {key: reference(root, model[key]) for key in policies}
    return {"model_reference": bound, "policy_references": refs,
            "model_binding_valid": True, "frozen_protocol_verified": False,
            "counterfactual_clocks_and_cash": True,
            "personal_execution_or_credit_required": False, **BOUNDARY}


def validate_input_manifest(root, manifest, *, decisions,
                            execution_events=None, review_at=None, scope="original_M24_replay",
                            execution_kind="observed", historical_model_ref=None,
                            etf_role="signal_feature", benchmark_outcome_events=None):
    """Check candidate evidence metadata, leaving independent data admission closed.

    The caller supplies the actual decisions, rather than a manifest choosing a
    convenient later decision to excuse a late snapshot. Execution observations
    have their own caller-supplied, signal-linked matching clocks. execution_at
    is the observed execution/matching time, not order submission: a valid quote
    may update after submission. Neither this envelope nor a scope proves row-
    level historical availability, complete actions, fills, or L1 admission.
    Historical simulation explicitly binds latency, matching, cash, failure and
    exposure policies; its supplied clocks never become actual observations.
    ETF benchmark results explicitly use caller-linked outcome coverage and
    current review, while ETF signal features keep the earlier signal gate.
    Result binding cannot certify a historical execution price or feature PIT.
    No price rows are read. Original M24 replay remains the default scope.
    """
    if scope not in INPUT_SCOPES:
        raise ValueError("input_scope_unknown")
    independent_etf_result = scope == "etf_benchmark_outcome"
    expected = {"schema", "roles"} if independent_etf_result else {"schema", "roles", "market_observation"}
    if not isinstance(manifest, dict) or set(manifest) != expected or manifest["schema"] != "regis.pb01.input_candidates.v1":
        raise ValueError("input_manifest_shape_invalid")
    clocks = [aware(d) for d in decisions]
    if not clocks:
        raise ValueError("actual_decisions_required")
    if etf_role not in {"signal_feature", "benchmark_outcome"}:
        raise ValueError("etf_role_unknown")
    if independent_etf_result and etf_role != "benchmark_outcome":
        raise ValueError("independent_etf_scope_requires_outcome_role")
    if execution_kind not in {"observed", "historical_simulation"}:
        raise ValueError("execution_kind_invalid")
    model = None
    if execution_kind == "historical_simulation":
        if scope != "original_M24_replay" or historical_model_ref is None or review_at is None:
            raise ValueError("historical_model_and_review_required")
        model = validate_historical_model_reference(root, historical_model_ref, review_at=review_at)
    elif historical_model_ref is not None:
        raise ValueError("historical_model_cannot_be_observed_evidence")
    review_clock = aware(review_at) if review_at is not None else None
    if review_clock is not None and any(review_clock < d for d in clocks):
        raise ValueError("review_before_signal")
    execution_clocks = []
    if execution_events is not None:
        if not isinstance(execution_events, list):
            raise ValueError("execution_events_invalid")
        ids = set()
        for event in execution_events:
            if (not isinstance(event, dict) or set(event) != {
                    "id", "signal_decision_at", "execution_at"}
                    or not isinstance(event["id"], str) or not event["id"]
                    or event["id"] in ids):
                raise ValueError("execution_event_identity_invalid")
            signal, execution = aware(event["signal_decision_at"]), aware(event["execution_at"])
            if signal not in clocks or execution <= signal:
                raise ValueError("execution_event_unlinked_or_before_signal")
            ids.add(event["id"])
            execution_clocks.append(execution)
    if review_clock is not None and any(review_clock < d for d in execution_clocks):
        raise ValueError("review_before_observed_execution")
    outcome_clocks, outcome_signals = [], set()
    if etf_role == "benchmark_outcome":
        if "etf_daily" not in INPUT_SCOPES[scope]:
            raise ValueError("etf_outcome_role_not_in_scope")
        if review_clock is None:
            raise ValueError("benchmark_outcome_review_required")
        if not isinstance(benchmark_outcome_events, list) or not benchmark_outcome_events:
            raise ValueError("benchmark_outcome_clocks_required")
        ids = set()
        for event in benchmark_outcome_events:
            if (not isinstance(event, dict) or set(event) != {
                    "id", "signal_decision_at", "outcome_at"}
                    or not isinstance(event["id"], str) or not event["id"].strip()
                    or event["id"] in ids):
                raise ValueError("benchmark_outcome_identity_invalid")
            signal, outcome = aware(event["signal_decision_at"]), aware(event["outcome_at"])
            if signal not in clocks or outcome <= signal:
                raise ValueError("benchmark_outcome_unlinked_or_before_signal")
            ids.add(event["id"])
            outcome_signals.add(signal)
            outcome_clocks.append(outcome)
        if outcome_signals != set(clocks):
            raise ValueError("benchmark_outcome_signal_coverage_missing")
        if any(review_clock < d for d in outcome_clocks):
            raise ValueError("review_before_benchmark_outcome")
    elif benchmark_outcome_events is not None:
        raise ValueError("benchmark_outcome_clocks_cannot_be_signal_features")
    rows = manifest["roles"]
    if not isinstance(rows, list):
        raise ValueError("roles_invalid")
    by_id = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("id") not in REQUIRED_ROLES + LATER_ROLES:
            raise ValueError("role_unknown")
        if row["id"] in by_id:
            raise ValueError("role_duplicate")
        by_id[row["id"]] = row
    results = []
    requested_roles = INPUT_SCOPES[scope] if independent_etf_result else (*INPUT_SCOPES[scope], MARKET_GATE)
    for rid in requested_roles:
        row = manifest["market_observation"] if rid == MARKET_GATE else by_id.get(rid)
        reasons, refs = [], []
        benchmark_result = rid == "etf_daily" and etf_role == "benchmark_outcome"
        component_calendar = rid == "exchange_calendar" and independent_etf_result
        if benchmark_result or component_calendar:
            # A later archive can describe a realized result. Its current review
            # never establishes the feature/quote availability at earlier times.
            role_clocks = [review_clock]
            clock_role = "benchmark_calendar_review" if component_calendar else "benchmark_result_review"
        elif rid == "minute_execution_quotes":
            role_clocks = execution_clocks
            clock_role = "historical_simulation_matching" if model else "execution_matching"
            if not role_clocks:
                reasons.append("simulated_execution_clocks_required" if model else "actual_execution_clocks_required")
        elif rid == "rights_and_retention" or rid in EVENT_EVIDENCE_ROLES:
            role_clocks = [review_clock] if review_clock else []
            clock_role = "research_permission_review" if rid == "rights_and_retention" else "event_evidence_envelope_review_only"
            if not role_clocks:
                reasons.append("actual_research_review_clock_required")
        else:
            role_clocks, clock_role = clocks, "signal_input"
        fields = {"id", "source", "unit_basis", "version_basis", "rights_basis",
                  "coverage_basis", "publication_basis", "coverage_start",
                  "coverage_end", "exact_version_available_at"}
        if not isinstance(row, dict):
            reasons.append("candidate_evidence_missing")
        elif set(row) != fields or row.get("id") != rid:
            reasons.append("candidate_fields_unknown_or_missing")
        else:
            for key in ("source", "unit_basis", "version_basis", "rights_basis",
                        "coverage_basis", "publication_basis"):
                try:
                    refs.append(reference(root, row[key]))
                except (OSError, ValueError):
                    reasons.append(key + "_unbound")
            try:
                available = aware(row["exact_version_available_at"])
                if any(available > decision for decision in role_clocks):
                    reasons.append("exact_version_after_decision")
                start, end = aware(row["coverage_start"]), aware(row["coverage_end"])
                # Action/publication envelopes describe historical market event
                # coverage, not the date they are being reviewed. Their precise
                # signal-feature, entitlement and cash-credit clocks remain
                # independently unverified, so never claim a PIT pass here.
                coverage_clocks = (clocks + outcome_clocks if component_calendar else
                                   outcome_clocks if benchmark_result else
                                   [] if rid in EVENT_EVIDENCE_ROLES else role_clocks)
                if start > end or any(not start <= d <= end for d in coverage_clocks):
                    reasons.append("decision_outside_evidence_coverage")
            except ValueError:
                reasons.append("publication_or_coverage_clock_invalid")
        results.append({"id": rid, "metadata_binding_valid": not reasons,
                        "clock_role": clock_role,
                        "signal_feature_availability_checked": clock_role == "signal_input",
                        "event_semantic_review_pending": rid in EVENT_EVIDENCE_ROLES,
                        "action_envelope_usable_as_signal_feature": False,
                        "benchmark_semantic_review_pending": benchmark_result or component_calendar,
                        "historical_feature_PIT": False,
                        "execution_reachability_verified": False,
                        "references": refs, "rejection_reasons": reasons,
                        "independent_data_review_passed": False})
    return {"schema": "regis.pb01.input_preflight.v1", "roles": results,
            "scope": scope, "required_role_ids": list(INPUT_SCOPES[scope]),
            "execution_kind": execution_kind, "historical_model": model,
            "etf_role": etf_role,
            "benchmark_outcome_clock_count": len(outcome_clocks),
            "benchmark_outcome_clocks_are_metadata_only": True,
            "benchmark_result_usable_as_signal_feature": False,
            "market_observation_required": not independent_etf_result,
            "execution_observation_count": 0 if model else len(execution_clocks),
            "simulated_execution_clock_count": len(execution_clocks) if model else 0,
            "historical_quote_and_model_semantics_review_pending": bool(model),
            "omitted_roles_are_not_admitted": [r for r in REQUIRED_ROLES if r not in INPUT_SCOPES[scope]],
            "input_envelope_only_not_row_level_PIT": True,
            "candidate_metadata_valid": all(r["metadata_binding_valid"] for r in results),
            "later_roles_do_not_block_pb01": list(LATER_ROLES),
            "data_admitted": False, **BOUNDARY}


def validate_freeze_package(root, package):
    """Validate a proposed freeze package, never label it frozen or unseen OOS."""
    fields = {"schema", "input_manifest_ref", "calendar_ref", "exposure_ref",
              "costs_ref", "label_intervals_ref", "market_gate_ref", "split",
              "configurations", "execution_level", "embargo_sessions"}
    reasons = []
    label_coverage = {"expected_origin_count": None, "provided_interval_count": None,
                      "complete_unique_origin_coverage": False}
    historical = isinstance(package, dict) and package.get("execution_level") == "minute_side_size_manual_latency_simulation"
    if historical:
        fields.add("historical_model_ref")
    if not isinstance(package, dict) or set(package) != fields or package.get("schema") != "regis.pb01.freeze_candidate.v1":
        raise ValueError("freeze_package_shape_invalid")
    refs = {}
    for key in ("input_manifest_ref", "calendar_ref", "exposure_ref", "costs_ref",
                "label_intervals_ref", "market_gate_ref"):
        try:
            refs[key] = reference(root, package[key])
        except (OSError, ValueError):
            reasons.append(key + "_unbound")
    model = None
    if historical:
        try:
            model = validate_historical_model_reference(root, package["historical_model_ref"])
        except (OSError, ValueError, KeyError, TypeError):
            reasons.append("historical_model_unbound_or_invalid")
    elif package["execution_level"] != "minute_side_size_manual_event_evidence":
        reasons.append("execution_level_incompatible_with_original_M24")
    if type(package["embargo_sessions"]) is not int or package["embargo_sessions"] < 5:
        reasons.append("embargo_below_original")
    configurations = package["configurations"]
    if not isinstance(configurations, list) or not 1 <= len(configurations) <= 12:
        reasons.append("configuration_budget_invalid")
    else:
        ids = [c.get("id") for c in configurations if isinstance(c, dict)]
        families = {c.get("family") for c in configurations if isinstance(c, dict) and isinstance(c.get("family"), str)}
        if len(ids) != len(configurations) or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
            reasons.append("configuration_identity_invalid")
        if any(not isinstance(c, dict) or set(c) != {"id", "family", "horizon", "parameters_sha256"}
               or not isinstance(c.get("parameters_sha256"), str)
               or not re.fullmatch("[0-9a-f]{64}", c.get("parameters_sha256", "")) for c in configurations):
            reasons.append("configuration_fields_or_parameters_unbound")
        if not 1 <= len(families) <= 3 or any(c.get("horizon") not in (1, 3, 5) or type(c.get("horizon")) is not int for c in configurations if isinstance(c, dict)):
            reasons.append("family_or_horizon_budget_invalid")
        if any(sum(c.get("family") == family for c in configurations if isinstance(c, dict)) > 4 for family in families):
            reasons.append("per_family_configuration_budget_exceeded")
    try:
        calendar = metadata(root, refs["calendar_ref"]["path"])
        days = calendar["trading_dates"]
        if not isinstance(days, list) or not days or days != sorted(set(days)):
            raise ValueError("calendar_not_ordered_unique")
        if any(datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") != day for day in days):
            raise ValueError("calendar_date_invalid")
        split = package["split"]
        if not isinstance(split, dict) or set(split) != {"train", "validation", "test"}:
            raise ValueError("split_shape_invalid")
        windows = [split[k] for k in ("train", "validation", "test")]
        if any(not isinstance(w, list) or not w or w != sorted(set(w)) or not set(w) <= set(days) for w in windows):
            raise ValueError("split_calendar_invalid")
        if not windows[0][-1] < windows[1][0] or not windows[1][-1] < windows[2][0]:
            raise ValueError("split_overlap_or_order_invalid")
        labels = metadata(root, refs["label_intervals_ref"]["path"])["intervals"]
        if not isinstance(labels, list) or not labels:
            raise ValueError("actual_label_spans_missing")
        index = {day: i for i, day in enumerate(days)}
        expected_origins = set().union(*windows)
        label_coverage.update(expected_origin_count=len(expected_origins),
                              provided_interval_count=len(labels))
        seen_origins = set()
        max_span = 0
        for label in labels:
            if not isinstance(label, dict) or set(label) != {"decision_date", "last_outcome_date"}:
                raise ValueError("label_interval_shape_invalid")
            origin, end = label["decision_date"], label["last_outcome_date"]
            if not isinstance(origin, str) or not isinstance(end, str):
                raise ValueError("label_date_type_invalid")
            if origin not in index or end not in index or index[end] < index[origin]:
                raise ValueError("label_calendar_invalid")
            # One aggregate last outcome is required for every declared decision
            # origin, including cash/no-fill days. Duplicates or extra calendar
            # dates cannot make up for a missing origin in another partition.
            if origin not in expected_origins or origin in seen_origins:
                raise ValueError("label_origin_duplicate_or_outside_split")
            seen_origins.add(origin)
            max_span = max(max_span, index[end] - index[origin] + 1)
            for current, following in zip(windows, windows[1:]):
                if origin in current and end >= following[0]:
                    raise ValueError("delayed_label_crosses_split")
        if seen_origins != expected_origins:
            raise ValueError("label_origin_coverage_incomplete")
        label_coverage["complete_unique_origin_coverage"] = True
        if package["embargo_sessions"] < max_span - 1:
            raise ValueError("embargo_below_actual_span")
        for earlier, later in zip(windows, windows[1:]):
            if index[later[0]] - index[earlier[-1]] - 1 < package["embargo_sessions"]:
                raise ValueError("split_embargo_missing")
    except (OSError, ValueError, KeyError, TypeError):
        reasons.append("calendar_split_or_actual_labels_invalid")
    return {"candidate_structure_valid": not reasons, "rejection_reasons": reasons,
            "label_interval_coverage": label_coverage,
            "references": refs, "independent_semantic_review_required": True,
            "execution_kind": "historical_simulation" if historical else "observed",
            "historical_model": model, "personal_execution_or_credit_required": not historical,
            "protocol_frozen": False, "unseen_out_of_sample": False, **BOUNDARY}


def parent_transition(*, checkpoint_passed, independent_actions, external_blockers, delivery_passed=False, user_stopped=False):
    """Do not stop the parent because a test, review, or push passed."""
    if user_stopped:
        return "user_stopped"
    if delivery_passed:
        return "parent_delivery_complete"
    if independent_actions:
        return "continue_next_authorized_dependency"
    if external_blockers:
        return "external_dependency_handoff"
    return "continue_parent_review"  # A bare checkpoint is not a stopping condition.


def current_preflight(root):
    matrix_name = "research/GOAL_DATA_EXECUTION_MATRIX_20261001.json"
    protocol_name = "QUANT_RESEARCH_M24_PROTOCOL_20261001.json"
    matrix, protocol = metadata(root, matrix_name), metadata(root, protocol_name)
    rows = matrix["roles"]
    if not isinstance(rows, list) or len({r["id"] for r in rows}) != len(rows):
        raise ValueError("matrix_roles_invalid")
    by_id = {r["id"]: r for r in rows}
    if set(by_id) != set(REQUIRED_ROLES + LATER_ROLES):
        raise ValueError("matrix_roles_missing_or_unknown")
    roles = [{"id": rid, "required": rid in REQUIRED_ROLES, "recorded_status": by_id[rid]["status"],
              "data_admitted": False, "required_fields": by_id[rid]["required_fields"],
              "coverage_required": by_id[rid]["coverage_required"],
              "next_evidence": by_id[rid]["unblock"]} for rid in (*REQUIRED_ROLES, *LATER_ROLES)]
    artifacts = []
    for stage, name in FUTURE.items():
        path = project_path(root, name)
        artifacts.append({"stage": stage, "path": name, "present": path.is_file(),
                          "sha256": sha256(path) if path.is_file() else None,
                          "semantic_admission_verified": False})
    return {"schema": "regis.pb01.pipeline_preflight.v1", "parent_delivery_complete": False,
            "checkpoint_is_parent_completion": False, "current_stage": "N4",
            "required_roles": list(REQUIRED_ROLES), "independent_market_gate_required": MARKET_GATE,
            "roles": roles, "future_evidence": artifacts,
            "source_bindings": [{"path": matrix_name, "sha256": sha256(project_path(root, matrix_name))},
                                {"path": protocol_name, "sha256": sha256(project_path(root, protocol_name))}],
            "initial_capital_CNY_per_counterfactual": protocol["initial_capital_yuan"],
            "counterfactual_books_are_mutually_exclusive": True,
            "account_returns_summed": False, "horizons": protocol["horizons_sessions_after_entry"],
            "next_action": "完成有限独立接口后，取得逐角色获准且时点/单位/版本/覆盖合格的真实输入；随后冻结原协议并执行N6→N8。",
            "capabilities": {"metadata_get": True, "candidate_binding_validator": True,
                             "freeze_candidate_validator": True, "register": False, "run_job": False},
            "no_market_rows_or_account_payload": True, **BOUNDARY}
