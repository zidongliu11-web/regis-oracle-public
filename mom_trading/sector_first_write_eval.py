"""Link saved first-write board origins to later labels before research scoring.

The file clock and hashes are self-reported, not independent publication proof.
The label is later re-qualification under a screen, not returns or a trade.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import sector_state_baseline as baseline
from .cloud_research_worker import ROOT, publish
from .emotion_pit_archive import read_day as read_market_pit
from .sector_forward_ledger import build as build_ledger
from .sector_mainline_origin import read_day


SCHEMA = "sector_first_write_requalification_eval.v1"
BOUNDARY = {"research_only": True, "forecast_validated": False,
            "production_eligible": False, "generates_alert": False,
            "returns_computed": False, "leader_validated": False,
            "external_issue_time_attested": False}
REPORT = Path("reports/sector_first_write_eval/latest.json")
MAX_REPORT_BYTES = 4 * 1024 * 1024


def _same_source(saved: Mapping[str, Any], ledger_origin: Mapping[str, Any]) -> bool:
    sector = saved["sector"]
    return (saved["next_open_at"] == ledger_origin["next_open_at"]
            and sector["packet_id"] == ledger_origin["packet_id"]
            and sector["source"] == ledger_origin["source"]
            and sector["source_contract_id"] == ledger_origin["source_contract_id"]
            and sector["source_as_of"] == ledger_origin["source_as_of"]
            and sector["observed_at"] == ledger_origin["observed_at"]
            and sector["population"] == ledger_origin["population"])


def _same_market(saved: Mapping[str, Any], market: Mapping[str, Any],
                 issued_at) -> bool:
    issued = baseline._stamp(issued_at, "first_write_issue_at")
    item = saved["market"]
    return (item["state"] == market["state"]
            and item["scope_id"] == market["identity"][0]
            and item["taxonomy_version"] == market["identity"][1]
            and item["feature_schema_version"] == market["identity"][2]
            and item["source_snapshot_sha256"] == market["source_snapshot_sha256"]
            and item["universe_snapshot_sha256"] == market["universe_snapshot_sha256"]
            and baseline._stamp(item["captured_at"], "first_write_market_at")
            == market["state_available_at"]
            and market["decision_at"] <= issued
            and market["state_available_at"] <= issued)


def _same_denominator(saved: Mapping[str, Any], rows: Sequence[dict]) -> bool:
    boards = {(row["kind"], row["code"], row["name"],
               row["screen_status"] == "qualified") for row in saved["boards"]
              if row["screen_status"] != "unassessable"}
    if not boards:
        return False
    for horizon in baseline.HORIZONS:
        actual_rows = [row for row in rows if row["horizon"] == horizon]
        actual = {(row["kind"], row["code"], row["name"], row["origin_qualified"])
                  for row in actual_rows}
        if (len(actual_rows) != len(boards) or actual != boards
                or any(row["origin_packet_id"] != saved["sector"]["packet_id"]
                       or row["target_day"] != saved["target_sessions"][str(horizon)]
                       for row in actual_rows)):
            return False
    return True


def evaluate(*, root: Path, ledger: Mapping[str, Any] | Sequence[Mapping[str, Any]],
             market_rows: Sequence[Mapping[str, Any]], evaluation_start: str,
             protocol_frozen_at: str, evaluation_as_of: str) -> dict[str, Any]:
    """Score only matching post-freeze first-write days; never repair old origins.

    Earlier historical ledger days may train after their labels mature. The
    selected scoring set is frozen from archive lineage, not later outcomes.
    """
    start = baseline._date(evaluation_start, "evaluation_start")
    frozen = baseline._stamp(protocol_frozen_at, "protocol_frozen_at")
    as_of = baseline._stamp(evaluation_as_of, "evaluation_as_of")
    if frozen > as_of:
        raise ValueError("protocol_after_evaluation_cutoff")
    origins, rows, batches = baseline._ledgers(ledger)
    market = baseline._market(market_rows, origins)
    by_day: dict[str, list[dict]] = {}
    for row in rows:
        by_day.setdefault(row["origin_day"], []).append(row)
    counts: Counter[str] = Counter()
    selected: list[str] = []
    record_ids: list[tuple[str, str]] = []
    issuer_cohort: dict[str, str] | None = None
    candidates = [day for day in origins if day >= start]
    for day in candidates:
        origin = origins[day]
        if origin["status"] != "verified":
            counts["ledger_origin_unverified"] += 1
            continue
        try:
            saved = read_day(Path(root), day, now=as_of)
        except (OSError, ValueError, KeyError, TypeError) as error:
            reason = ("first_write_missing_or_unsafe"
                      if isinstance(error, ValueError)
                      and str(error) == "origin_record_missing_or_unsafe"
                      else "first_write_invalid_or_unreadable")
            counts[reason] += 1
            continue
        issued = baseline._stamp(saved["issued_at"], "first_write_issued_at")
        if issued < frozen:
            counts["issued_before_protocol_freeze"] += 1
            continue
        if saved["origin_day"] != day or not _same_source(saved, origin):
            counts["sector_lineage_or_population_mismatch"] += 1
            continue
        observation = market.get(day)
        if (observation is None or baseline._market_at(day, market, origins, cutoff=issued) is None
                or not _same_market(saved, observation, saved["issued_at"])):
            counts["market_pit_mismatch_or_unavailable"] += 1
            continue
        if not _same_denominator(saved, by_day.get(day, [])):
            counts["board_denominator_or_target_day_mismatch"] += 1
            continue
        if issuer_cohort is not None and saved["issuer_sources"] != issuer_cohort:
            counts["issuer_source_cohort_changed"] += 1
            continue
        issuer_cohort = saved["issuer_sources"]
        selected.append(day)
        record_ids.append((day, saved["record_sha256"]))
    result: dict[str, Any] = {
        "schema": SCHEMA, "status": "no_matching_first_write_origins",
        "evaluation_start": start, "protocol_frozen_at": frozen.isoformat(),
        "evaluation_as_of": as_of.isoformat(),
        "candidate_origin_days": len(candidates), "matched_origin_days": selected,
        "matched_origin_count": len(selected), "skip_reasons": dict(sorted(counts.items())),
        "first_write_record_ids_sha256": baseline._hash(record_ids),
        "ledger_input_ids_sha256": baseline._hash([batch["input_sha256"] for batch in batches]),
        "issuer_source_cohort_sha256": baseline._hash(issuer_cohort) if issuer_cohort else None,
        "target": "future_frozen_screen_requalification_not_mainline_or_return",
        "chronology": "self_reported_first_write_not_external_attestation",
        "baseline": None, **BOUNDARY,
    }
    if selected:
        scored = baseline.evaluate(ledger, market_rows,
                                   evaluation_start=start,
                                   protocol_frozen_at=protocol_frozen_at,
                                   evaluation_as_of=evaluation_as_of,
                                   score_origin_days=selected)
        result["baseline"] = scored
        result["status"] = ("linked_research_diagnostic" if scored["status"] == "research_diagnostic"
                            else "linked_but_insufficient_scored_history")
    return result


def run_archive(*, root=ROOT, start_day: str, end_day: str,
                score_start: str, protocol_frozen_at: str,
                evaluation_as_of: str) -> dict[str, Any]:
    """Explicit bounded batch job. HTTP routes only read its saved result."""
    root = Path(root)
    start = baseline._date(start_day, "start_day")
    end = baseline._date(end_day, "end_day")
    score = baseline._date(score_start, "score_start")
    if not start <= score <= end:
        raise ValueError("score_start_outside_bounded_batch")
    as_of = baseline._stamp(evaluation_as_of, "evaluation_as_of")
    if as_of > datetime.now(timezone.utc).astimezone(baseline.SHANGHAI):
        raise ValueError("evaluation_cutoff_in_future")
    labels = build_ledger(root=root, start_day=start, end_day=end,
                          evaluation_as_of=as_of.isoformat())
    market_rows = []
    for slot in labels["origin_days"]:
        try:
            market_rows.append(read_market_pit(root, slot["day"], now=as_of)["market_row"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    result = evaluate(root=root, ledger=labels, market_rows=market_rows,
                      evaluation_start=score,
                      protocol_frozen_at=protocol_frozen_at,
                      evaluation_as_of=as_of.isoformat())
    result["batch_start_day"] = start
    result["batch_end_day"] = end
    result["ledger_status"] = labels["status"]
    result["report_saved_at"] = datetime.now(timezone.utc).isoformat()
    result["report_sha256"] = baseline._hash(result)
    path = root / REPORT
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("evaluation_report_path_unsafe")
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True,
                         allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_REPORT_BYTES:
        raise ValueError("evaluation_report_too_large")
    publish(path, result)
    return result


def current_status(root=ROOT) -> dict[str, Any]:
    """Small read-only UI contract; a saved research report is never a signal."""
    empty = {"schema": "sector_first_write_eval_status.v1",
             "status": "evaluation_not_run", "matched_origin_count": 0,
             "candidate_origin_days": 0, "skip_reasons": {},
             "group_support": {}, "evaluation_as_of": None,
             "report_sha256": None, "ledger_status": None, **BOUNDARY}
    path = Path(root) / REPORT
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        return empty
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_REPORT_BYTES + 1)
        if len(raw) > MAX_REPORT_BYTES:
            raise ValueError("evaluation_report_too_large")
        report = json.loads(raw)
        digest = report.pop("report_sha256")
        if (not isinstance(report, dict) or report.get("schema") != SCHEMA
                or not isinstance(digest, str) or baseline._hash(report) != digest
                or any(report.get(key) != value for key, value in BOUNDARY.items())
                or report.get("status") not in {
                    "no_matching_first_write_origins", "linked_research_diagnostic",
                    "linked_but_insufficient_scored_history"}
                or type(report.get("candidate_origin_days")) is not int
                or type(report.get("matched_origin_count")) is not int
                or not isinstance(report.get("skip_reasons"), dict)
                or not 0 <= report["matched_origin_count"] <= report["candidate_origin_days"]
                or any(type(value) is not int or value < 0
                       for value in report["skip_reasons"].values())
                or report["matched_origin_count"] + sum(report["skip_reasons"].values())
                != report["candidate_origin_days"]):
            raise ValueError("evaluation_report_contract_invalid")
        groups = (report.get("baseline") or {}).get("groups", {})
        support = {key: {"status": group["status"],
                         "scored_origin_days": group["common_origin_days"],
                         "known_label_rows": group["known_label_rows"]}
                   for key, group in groups.items()}
        return {**empty, "status": report["status"],
                "candidate_origin_days": report["candidate_origin_days"],
                "matched_origin_count": report["matched_origin_count"],
                "skip_reasons": report["skip_reasons"],
                "group_support": support,
                "evaluation_as_of": report["evaluation_as_of"],
                "report_sha256": digest,
                "ledger_status": report.get("ledger_status")}
    except (OSError, ValueError, TypeError, KeyError, AttributeError,
            json.JSONDecodeError):
        return {**empty, "status": "evaluation_report_invalid"}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Research-only first-write sector evaluation")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--start", required=True, help="first ledger day, including training history")
    parser.add_argument("--end", required=True)
    parser.add_argument("--score-start", required=True, help="first day eligible for scoring")
    parser.add_argument("--frozen-at", required=True, help="protocol freeze ISO timestamp")
    parser.add_argument("--as-of", required=True, help="evaluation cutoff ISO timestamp")
    args = parser.parse_args()
    outcome = run_archive(root=args.root, start_day=args.start, end_day=args.end,
                          score_start=args.score_start,
                          protocol_frozen_at=args.frozen_at,
                          evaluation_as_of=args.as_of)
    print(json.dumps({key: outcome[key] for key in (
        "status", "candidate_origin_days", "matched_origin_count",
        "skip_reasons", "report_sha256")}, ensure_ascii=False))
