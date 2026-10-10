"""Research-only, fixed-origin board continuation candidate.

Supplier board daily percentages and breadth are diagnostics, not investable
returns or an accepted definition of a market mainline. No trading route reads
this module. The website only reads an explicitly saved offline report.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from statistics import median
from typing import Any, Mapping

from .cloud_research_worker import ROOT, publish
from .market_sector_strategy_adapter import _sector_rows
from .sector_forward_ledger import HORIZONS, _calendar, _read_close, _slot
from .sector_mainline_origin import read_day
from .sector_state_baseline import _date, _hash, _stamp


SCHEMA = "sector_peer_continuation_candidate.v1"
STATUS_SCHEMA = "sector_peer_continuation_status.v1"
REPORT = Path("reports/sector_peer_continuation/latest.json")
MAX_ROWS = 20_000
MAX_REPORT_BYTES = 16 * 1024 * 1024
MIN_PEERS = 5
REPORTED_UP_SHARE_THRESHOLD_PCT = 60
KINDS = ("industry", "concept")
BOUNDARY = {"research_only": True, "formal_mainline_label": False,
            "forecast_validated": False, "production_eligible": False,
            "generates_alert": False, "investable_returns_computed": False,
            "historical_membership_verified": False,
            "full_constituent_diffusion_verified": False,
            "external_issue_time_attested": False}


def _empty_count() -> dict[str, Any]:
    return {"attempted": 0, "known": 0, "positive": 0, "negative": 0,
            "unknown": 0, "reasons": {}}


def _slot_with_rows(root: Path, days: list[str], index: int,
                    as_of: datetime, reader) -> dict[str, Any]:
    slot = _slot(root, days, index, as_of, reader)
    if slot["status"] != "verified":
        return slot
    try:
        packet, _ = reader(root, days[index], as_of)
        if (packet["packet_id"] != slot["packet_id"]
                or packet["observed_at"] != slot["observed_at"]
                or packet["snapshot"]["source"] != slot["source"]):
            raise ValueError("future_packet_changed_between_reads")
        rows, _ = _sector_rows(packet["snapshot"])
        mapped = {(row["kind"], row["code"]): row for row in rows}
        if len(mapped) != len(rows):
            raise ValueError("future_board_identity_duplicate")
        return {**slot, "rows": mapped}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {**slot, "status": "changed_or_invalid_between_reads"}


def _cohort_path(origin: Mapping[str, Any], path: list[dict],
                 cohort: list[dict]) -> str | None:
    if len(cohort) < MIN_PEERS:
        return "peer_cohort_below_minimum"
    for slot in path:
        if slot["status"] != "verified":
            return "future_" + slot["status"]
        if (slot["source"] != origin["sector"]["source"]
                or slot["source_contract_id"] != origin["sector"]["source_contract_id"]):
            return "supplier_or_protocol_changed"
        for member in cohort:
            key = (member["kind"], member["code"])
            if slot["identities"].get(key) != member["name"]:
                return "origin_peer_identity_missing_or_changed"
            if key not in slot["evaluable"]:
                return "origin_peer_not_evaluable"
            row = slot["rows"].get(key)
            if (row is None or row["name"] != member["name"]
                    or type(row["change_pct"]) not in (int, float)
                    or not math.isfinite(row["change_pct"])
                    or row["change_pct"] <= -100
                    or type(row["advance_coverage_pct"]) not in (int, float)
                    or not math.isfinite(row["advance_coverage_pct"])
                    or not 0 <= row["advance_coverage_pct"] <= 100):
                return "origin_peer_daily_measure_invalid"
    return None


def _chain(path: list[dict], key: tuple[str, str]) -> float:
    return (math.prod(1 + slot["rows"][key]["change_pct"] / 100
                      for slot in path) - 1) * 100


def build(*, root=ROOT, origin_day: str, evaluation_as_of: str,
          protocol_frozen_at: str, reader=None) -> dict[str, Any]:
    """Build one bounded offline research target from frozen first-write rows."""
    root = Path(root)
    day = _date(origin_day, "origin_day")
    as_of = _stamp(evaluation_as_of, "evaluation_as_of")
    frozen = _stamp(protocol_frozen_at, "protocol_frozen_at")
    if frozen > as_of:
        raise ValueError("protocol_after_evaluation_cutoff")
    result: dict[str, Any] = {
        "schema": SCHEMA, "status": "waiting_calendar", "origin_day": day,
        "evaluation_as_of": as_of.isoformat(),
        "protocol_frozen_at": frozen.isoformat(), "calendar_sha256": None,
        "origin_record_sha256": None, "source_scope": "verified_supplier_origin_cohort_only",
        "definition": {"horizons": list(HORIZONS), "minimum_origin_peers": MIN_PEERS,
                       "reported_up_share_threshold_pct": REPORTED_UP_SHARE_THRESHOLD_PCT,
                       "reported_up_share_denominator": "supplier_up_count_plus_down_count_only",
                       "reported_up_share_days": "ceil(horizon/2)",
                       "relative_reference": "same_kind_origin_cohort_median"},
        "counts": {str(h): {kind: _empty_count() for kind in KINDS}
                   for h in HORIZONS},
        "rows": [], "future_slots": [], "input_sha256": None, **BOUNDARY,
    }
    try:
        sessions, calendar_sha = _calendar(root, as_of)
        index = sessions.index(day)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return result
    result["calendar_sha256"] = calendar_sha
    if index + max(HORIZONS) >= len(sessions):
        return {**result, "status": "future_calendar_missing"}
    try:
        origin = read_day(root, day, now=as_of)
    except (OSError, ValueError, KeyError, TypeError):
        return {**result, "status": "first_write_missing_or_invalid"}
    result["origin_record_sha256"] = origin["record_sha256"]
    if origin["calendar_window"] != sessions[index:index + max(HORIZONS) + 1]:
        return {**result, "status": "calendar_window_changed"}
    if frozen > _stamp(origin["issued_at"], "origin_issued_at"):
        return {**result, "status": "protocol_frozen_after_origin"}
    if origin["board_count"] * len(HORIZONS) > MAX_ROWS:
        return {**result, "status": "batch_too_large"}

    read = reader or _read_close
    path = [_slot_with_rows(root, sessions, index + offset, as_of, read)
            for offset in range(1, max(HORIZONS) + 1)]
    result["future_slots"] = [{name: slot[name] for name in
                               ("day", "status", "packet_id", "source",
                                "source_contract_id", "observed_at")}
                              for slot in path]
    cohorts = {kind: [board for board in origin["boards"]
                      if board["kind"] == kind
                      and board["screen_status"] != "unassessable"]
               for kind in KINDS}
    for horizon in HORIZONS:
        future = path[:horizon]
        for kind in KINDS:
            cohort = cohorts[kind]
            group_reason = _cohort_path(origin, future, cohort)
            peer_median = None
            if group_reason is None:
                peer_median = median(_chain(future, (board["kind"], board["code"]))
                                     for board in cohort)
            for board in (b for b in origin["boards"] if b["kind"] == kind):
                counter = result["counts"][str(horizon)][kind]
                counter["attempted"] += 1
                reason = ("origin_unassessable" if board["screen_status"] == "unassessable"
                          else group_reason)
                relative = chain = reported_up_days = candidate = None
                if reason is None:
                    key = (kind, board["code"])
                    chain = _chain(future, key)
                    relative = chain - peer_median
                    reported_up_days = sum(slot["rows"][key]["advance_coverage_pct"]
                                           >= REPORTED_UP_SHARE_THRESHOLD_PCT
                                           for slot in future)
                    candidate = int(relative > 0 and reported_up_days >= math.ceil(horizon / 2))
                    counter["known"] += 1
                    counter["positive" if candidate else "negative"] += 1
                else:
                    counter["unknown"] += 1
                    counter["reasons"][reason] = counter["reasons"].get(reason, 0) + 1
                result["rows"].append({
                    "origin_day": day, "kind": kind, "code": board["code"],
                    "name": board["name"], "origin_screen_status": board["screen_status"],
                    "horizon": horizon, "target_day": sessions[index + horizon],
                    "target_packet_id": future[-1]["packet_id"],
                    "label_available_at": future[-1]["observed_at"] if reason is None else None,
                    "supplier_change_chain_pct": round(chain, 6) if chain is not None else None,
                    "peer_median_chain_pct": round(peer_median, 6) if reason is None else None,
                    "relative_peer_points": round(relative, 6) if relative is not None else None,
                    "reported_up_share_days_at_or_above_60": reported_up_days,
                    "proxy_positive": candidate, "unknown_reason": reason})
    for grouped in result["counts"].values():
        for count in grouped.values():
            if (count["attempted"] != count["known"] + count["unknown"]
                    or count["known"] != count["positive"] + count["negative"]):
                raise ValueError("candidate_count_partition_invalid")
    result["status"] = ("candidate_labels_available" if any(
        count["known"] for group in result["counts"].values() for count in group.values())
        else "insufficient_verified_future")
    result["input_sha256"] = _hash({
        "calendar": calendar_sha, "origin": origin["record_sha256"],
        "future_slots": result["future_slots"],
        "protocol_frozen_at": result["protocol_frozen_at"],
        "evaluation_as_of": result["evaluation_as_of"],
        "definition": result["definition"]})
    return result


def run_archive(**kwargs) -> dict[str, Any]:
    """Explicit offline invocation; keep every attempt before updating latest."""
    root = Path(kwargs.get("root", ROOT))
    as_of = _stamp(kwargs["evaluation_as_of"], "evaluation_as_of")
    if as_of > datetime.now(timezone.utc).astimezone(as_of.tzinfo):
        raise ValueError("evaluation_cutoff_in_future")
    result = build(**kwargs)
    result["report_saved_at"] = datetime.now(timezone.utc).isoformat()
    result["report_sha256"] = _hash(result)
    path = root / REPORT
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("candidate_report_path_unsafe")
    raw = json.dumps(result, ensure_ascii=False, sort_keys=True,
                     allow_nan=False).encode("utf-8")
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("candidate_report_too_large")
    runs = path.parent / "runs"
    day_dir = runs / result["origin_day"]
    if runs.is_symlink() or day_dir.is_symlink():
        raise ValueError("candidate_run_path_unsafe")
    day_dir.mkdir(parents=True, exist_ok=True)
    run_path = day_dir / (result["report_sha256"] + ".json")
    try:
        with run_path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if run_path.is_symlink() or run_path.read_bytes() != raw:
            raise ValueError("candidate_run_identity_conflict")
    publish(path, result)
    return result


def current_status(root=ROOT) -> dict[str, Any]:
    """Bounded read-only website status; no per-board research rows exposed."""
    empty = {"schema": STATUS_SCHEMA, "status": "candidate_not_run",
             "origin_day": None, "evaluation_as_of": None,
             "known": 0, "positive": 0, "unknown": 0,
             "reasons": {}, "report_sha256": None, **BOUNDARY}
    path = Path(root) / REPORT
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        return empty
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_REPORT_BYTES + 1)
        if len(raw) > MAX_REPORT_BYTES:
            raise ValueError("candidate_report_too_large")
        report = json.loads(raw)
        if not isinstance(report, dict):
            raise ValueError("candidate_report_invalid")
        digest = report.pop("report_sha256")
        if (report.get("schema") != SCHEMA or not isinstance(digest, str)
                or _hash(report) != digest
                or any(report.get(key) != value for key, value in BOUNDARY.items())
                or report.get("status") not in {
                    "waiting_calendar", "future_calendar_missing",
                    "first_write_missing_or_invalid", "calendar_window_changed",
                    "protocol_frozen_after_origin", "batch_too_large",
                    "candidate_labels_available", "insufficient_verified_future"}
                or not isinstance(report.get("counts"), dict)
                or not isinstance(report.get("rows"), list)):
            raise ValueError("candidate_report_contract_invalid")
        run_path = path.parent / "runs" / report["origin_day"] / (digest + ".json")
        if (run_path.is_symlink() or run_path.parent.is_symlink()
                or not run_path.is_file()):
            raise ValueError("candidate_run_missing_or_changed")
        with run_path.open("rb") as stream:
            saved_run = stream.read(MAX_REPORT_BYTES + 1)
        if (len(saved_run) > MAX_REPORT_BYTES
                or json.loads(saved_run) != {**report, "report_sha256": digest}):
            raise ValueError("candidate_run_missing_or_changed")
        totals: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        for horizon in HORIZONS:
            group = report["counts"][str(horizon)]
            for kind in KINDS:
                item = group[kind]
                if (any(type(item.get(field)) is not int or item[field] < 0
                        for field in ("attempted", "known", "positive", "negative", "unknown"))
                        or item["attempted"] != item["known"] + item["unknown"]
                        or item["known"] != item["positive"] + item["negative"]
                        or not isinstance(item.get("reasons"), dict)
                        or any(type(value) is not int or value < 0
                               for value in item["reasons"].values())
                        or sum(item["reasons"].values()) != item["unknown"]):
                    raise ValueError("candidate_report_counts_invalid")
                totals.update({field: item[field] for field in
                               ("attempted", "known", "positive", "unknown")})
                reasons.update(item["reasons"])
        if totals["attempted"] != len(report["rows"]):
            raise ValueError("candidate_report_row_count_invalid")
        return {**empty, "status": report["status"], "origin_day": report["origin_day"],
                "evaluation_as_of": report["evaluation_as_of"],
                "known": totals["known"], "positive": totals["positive"],
                "unknown": totals["unknown"], "reasons": dict(sorted(reasons.items())),
                "report_sha256": digest}
    except (OSError, ValueError, KeyError, TypeError, AttributeError,
            json.JSONDecodeError):
        return {**empty, "status": "candidate_report_invalid"}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Research-only board continuation target")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--origin-day", required=True)
    parser.add_argument("--frozen-at", required=True)
    parser.add_argument("--as-of", required=True)
    args = parser.parse_args()
    output = run_archive(root=args.root, origin_day=args.origin_day,
                         protocol_frozen_at=args.frozen_at,
                         evaluation_as_of=args.as_of)
    print(json.dumps({"status": output["status"], "origin_day": output["origin_day"],
                      "counts": output["counts"], "report_sha256": output["report_sha256"]},
                     ensure_ascii=False))
