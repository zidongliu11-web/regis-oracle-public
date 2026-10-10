"""Offline, paired continuation-vs-reversal audit of a provisional board label.

This is a descriptive diagnostic on supplier boards, not a mainline forecast,
an investable return, or a trading route. Reads first-write origins and
append-only proxy-label runs. No web request starts this evaluator.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timezone
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cloud_research_worker import ROOT, publish
from .market_sector_strategy_adapter import SH_TZ
from .sector_forward_ledger import HORIZONS, _calendar
from .sector_mainline_origin import read_day
from .sector_peer_continuation import (
    BOUNDARY as LABEL_BOUNDARY,
    MAX_REPORT_BYTES,
    SCHEMA as LABEL_SCHEMA,
)
from .sector_state_baseline import _date, _hash, _stamp


SCHEMA = "sector_direction_paired_audit.v1"
STATUS_SCHEMA = "sector_direction_paired_status.v1"
REPORT = Path("reports/sector_direction_audit/latest.json")
MAX_ORIGINS = 120
MAX_RUNS_PER_ORIGIN = 16
MAX_INPUT_BYTES = 64 * 1024 * 1024
MIN_BOARDS = 5
TAIL_FRACTION = 0.20
MIN_SCORE_DAYS = 20
MIN_STATE_DAYS = 10
KINDS = ("industry", "concept")
STATES = ("RISK_OFF", "STABILIZING", "RISK_ON", "OVERHEATED")
BOUNDARY = {
    "research_only": True, "formal_mainline_label": False,
    "forecast_validated": False, "production_eligible": False,
    "generates_alert": False, "leader_validated": False,
    "investable_returns_computed": False, "external_issue_time_attested": False,
}


def _empty_group() -> dict[str, Any]:
    return {"status": "insufficient_scored_origin_days", "scored_origin_days": 0,
            "scored_board_rows": 0, "skipped_origin_days": {},
            "unscored_board_rows": 0, "state_days": {state: 0 for state in STATES},
            "rates_equal_origin_day": None, "by_market_state": None,
            "tail_mean_pct": None}


def _row_map(origin: Mapping[str, Any], report: Mapping[str, Any],
             horizon: int, kind: str) -> tuple[list[dict], dict[tuple[str, str], dict]]:
    boards = [row for row in origin["boards"] if row["kind"] == kind]
    expected = {(row["kind"], row["code"]): row for row in boards}
    if len(expected) != len(boards):
        raise ValueError("origin_board_identity_duplicate")
    rows = [row for row in report["rows"]
            if row.get("horizon") == horizon and row.get("kind") == kind]
    observed = {(row.get("kind"), row.get("code")): row for row in rows}
    if len(observed) != len(rows) or set(observed) != set(expected):
        raise ValueError("label_population_mismatch")
    for key, board in expected.items():
        row = observed[key]
        proxy = row.get("proxy_positive")
        if (row.get("origin_day") != origin["origin_day"]
                or row.get("name") != board["name"]
                or row.get("origin_screen_status") != board["screen_status"]
                or row.get("target_day") != origin["target_sessions"][str(horizon)]
                or (proxy is not None and type(proxy) is not int)
                or (proxy is not None and proxy not in (0, 1))
                or (proxy is None) != (row.get("unknown_reason") is not None)
                or (board["screen_status"] == "unassessable"
                    and proxy is not None)):
            raise ValueError("label_row_identity_or_partition_invalid")
    return boards, observed


def _complete_pair(origin: Mapping[str, Any], report: Mapping[str, Any],
                   as_of: datetime, frozen: datetime, horizon: int,
                   kind: str) -> tuple[bool, str | None]:
    if (origin.get("schema") != "sector_mainline_origin.v1"
            or origin.get("status") != "origin_recorded"
            or report.get("schema") != LABEL_SCHEMA
            or report.get("status") != "candidate_labels_available"
            or report.get("origin_day") != origin.get("origin_day")
            or report.get("origin_record_sha256") != origin.get("record_sha256")
            or report.get("source_scope") != "verified_supplier_origin_cohort_only"
            or any(report.get(key) != value for key, value in LABEL_BOUNDARY.items())
            or report.get("report_sha256") != _hash({
                key: value for key, value in report.items()
                if key != "report_sha256"})):
        return False, "origin_or_label_contract_mismatch"
    issued = _stamp(origin["issued_at"], "origin_issued_at")
    saved = _stamp(report["report_saved_at"], "report_saved_at")
    cutoff = _stamp(report["evaluation_as_of"], "report_evaluation_as_of")
    if not issued < saved <= as_of or not issued < cutoff <= saved:
        return False, "label_report_not_visible_by_cutoff"
    if frozen > issued:
        return False, "direction_protocol_late"
    if _stamp(report["protocol_frozen_at"], "label_protocol_frozen_at") > issued:
        return False, "label_protocol_late"
    if (not isinstance(report["rows"], list)
            or len(report["rows"]) != len(origin["boards"]) * len(HORIZONS)
            or not isinstance(report["future_slots"], list)
            or len(report["future_slots"]) < horizon
            or len(origin.get("calendar_window", [])) <= horizon):
        return False, "label_population_mismatch"
    for offset in range(1, horizon + 1):
        slot = report["future_slots"][offset - 1]
        observed = _stamp(slot["observed_at"], "future_observed_at").astimezone(SH_TZ)
        if (slot.get("day") != origin["calendar_window"][offset]
                or slot.get("status") != "verified"
                or slot.get("source") != origin["sector"]["source"]
                or slot.get("source_contract_id")
                != origin["sector"]["source_contract_id"]
                or not slot.get("packet_id")
                or observed.date().isoformat() != slot["day"]
                or observed.time() < time(15)
                or not issued < observed <= cutoff):
            return False, "label_future_source_mismatch"
    slot = report["future_slots"][horizon - 1]
    boards, rows = _row_map(origin, report, horizon, kind)
    for board in boards:
        row = rows[(kind, board["code"])]
        if board["screen_status"] == "unassessable":
            continue
        if row["proxy_positive"] is None:
            return False, "label_incomplete_or_unknown"
        available = _stamp(row["label_available_at"], "label_available_at")
        if (not issued < available <= cutoff
                or row.get("target_packet_id") != slot.get("packet_id")
                or row["label_available_at"] != slot.get("observed_at")):
            return False, "label_availability_invalid"
    return True, None


def _stable_outcomes(report: Mapping[str, Any], horizon: int, kind: str) -> str:
    """Compare only the matured horizon and kind; later horizons may fill in."""
    return _hash({
        "origin_record_sha256": report["origin_record_sha256"],
        "future_slots": report["future_slots"][:horizon],
        "rows": [row for row in report["rows"]
                 if row.get("horizon") == horizon and row.get("kind") == kind],
        "definition": report["definition"],
    })


def _choose_run(origin: Mapping[str, Any], runs: Sequence[Mapping[str, Any]],
                as_of: datetime, frozen: datetime, horizon: int,
                kind: str) -> tuple[Mapping[str, Any] | None, str | None]:
    completed: list[Mapping[str, Any]] = []
    failures: Counter[str] = Counter()
    for report in runs:
        try:
            valid, reason = _complete_pair(origin, report, as_of, frozen,
                                           horizon, kind)
        except (KeyError, ValueError, TypeError, AttributeError):
            valid, reason = False, "label_structure_invalid"
        if valid:
            completed.append(report)
        else:
            failures[reason or "label_unavailable"] += 1
    if not completed:
        return None, (failures.most_common(1)[0][0] if failures else "label_run_missing")
    identities = {_stable_outcomes(report, horizon, kind) for report in completed}
    if len(identities) != 1:
        return None, "completed_label_revision_conflict"
    first = min(completed, key=lambda item: (item["report_saved_at"], item["report_sha256"]))
    return first, None


def _tails(boards: list[dict]) -> tuple[set[str], set[str]] | None:
    if len(boards) < MIN_BOARDS:
        return None
    ordered = sorted((row["change_pct"] for row in boards))
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in ordered):
        return None
    count = max(1, math.ceil(len(boards) * TAIL_FRACTION))
    low, high = ordered[count - 1], ordered[-count]
    if low >= high:
        return None
    bottom = {row["code"] for row in boards if row["change_pct"] <= low}
    top = {row["code"] for row in boards if row["change_pct"] >= high}
    if top & bottom:
        return None
    return top, bottom


def evaluate_pairs(pairs: Sequence[tuple[Mapping[str, Any], Sequence[Mapping[str, Any]]]],
                   *, protocol_frozen_at: str, evaluation_as_of: str) -> dict[str, Any]:
    """Equal-weight origin-day comparison; pairs are already source-file verified."""
    frozen = _stamp(protocol_frozen_at, "protocol_frozen_at")
    as_of = _stamp(evaluation_as_of, "evaluation_as_of")
    if frozen > as_of:
        raise ValueError("direction_protocol_after_cutoff")
    if len(pairs) > MAX_ORIGINS:
        raise ValueError("direction_origin_batch_too_large")
    days = [_date(origin["origin_day"], "origin_day") for origin, _ in pairs]
    if days != sorted(set(days)):
        raise ValueError("direction_origin_order_invalid")
    result: dict[str, Any] = {
        "schema": SCHEMA, "status": "insufficient_scored_origin_days",
        "protocol_frozen_at": frozen.isoformat(),
        "evaluation_as_of": as_of.isoformat(),
        "target_schema": LABEL_SCHEMA, "target_meaning": "supplier_peer_proxy_only",
        "rank_definition": {"tails_each": TAIL_FRACTION,
                            "boundary_ties": "include_all_or_skip_if_overlap",
                            "minimum_origin_boards": MIN_BOARDS},
        "support_contract": {"score_origin_days": MIN_SCORE_DAYS,
                             "market_state_days": MIN_STATE_DAYS},
        "input_sha256": _hash([{"origin": origin.get("record_sha256"),
                                "runs": [run.get("report_sha256") for run in runs]}
                               for origin, runs in pairs]),
        "attempted_origin_days": len(pairs),
        "groups": {}, "chronology": "historical_diagnostic_only",
        "source_authentication": "caller_verified_local_receipts_not_external_attestation",
        **BOUNDARY,
    }
    for horizon in HORIZONS:
        for kind in KINDS:
            key = f"{horizon}:{kind}"
            group = _empty_group()
            daily = []
            source_identity = None
            for origin, runs in pairs:
                try:
                    report, reason = _choose_run(origin, runs, as_of, frozen,
                                                 horizon, kind)
                    if report is None:
                        raise ValueError(reason or "label_run_missing")
                    if origin["market"]["state"] not in STATES:
                        raise ValueError("origin_market_state_invalid")
                    current_source = (origin["sector"]["source"],
                                      origin["sector"]["source_contract_id"])
                    if source_identity is not None and current_source != source_identity:
                        raise ValueError("source_protocol_changed_across_origins")
                    boards, outcomes = _row_map(origin, report, horizon, kind)
                    evaluable = [row for row in boards if row["screen_status"] != "unassessable"]
                    selection = _tails(evaluable)
                    if selection is None:
                        raise ValueError("insufficient_or_tied_rank")
                    top, bottom = selection
                    values = {row["code"]: outcomes[(kind, row["code"])]["proxy_positive"]
                              for row in evaluable}
                    if any(value not in (0, 1) for value in values.values()):
                        raise ValueError("label_incomplete_or_unknown")
                    source_identity = current_source
                    day = {
                        "origin_day": origin["origin_day"], "market_state": origin["market"]["state"],
                        "origin_record_sha256": origin["record_sha256"],
                        "label_report_sha256": report["report_sha256"],
                        "source": origin["sector"]["source"],
                        "source_contract_id": origin["sector"]["source_contract_id"],
                        "board_rows": len(evaluable), "top_rows": len(top),
                        "bottom_rows": len(bottom),
                        "all_rate": sum(values.values()) / len(values),
                        "top_rate": sum(values[code] for code in top) / len(top),
                        "bottom_rate": sum(values[code] for code in bottom) / len(bottom),
                    }
                    daily.append(day)
                    group["scored_board_rows"] += len(evaluable)
                    group["state_days"][day["market_state"]] += 1
                except (KeyError, ValueError, TypeError, AttributeError) as error:
                    reason = str(error) if str(error) else "invalid_input"
                    skipped = group["skipped_origin_days"]
                    skipped[reason] = skipped.get(reason, 0) + 1
                    group["unscored_board_rows"] += sum(
                        row.get("kind") == kind and row.get("screen_status") != "unassessable"
                        for row in origin.get("boards", []) if isinstance(row, dict))
            group["scored_origin_days"] = len(daily)
            if len(daily) >= MIN_SCORE_DAYS:
                group["status"] = "research_diagnostic"
                group["rates_equal_origin_day"] = {
                    name: round(sum(row[name] for row in daily) / len(daily), 6)
                    for name in ("all_rate", "top_rate", "bottom_rate")}
                group["rates_equal_origin_day"]["top_minus_bottom"] = round(
                    sum(row["top_rate"] - row["bottom_rate"] for row in daily) / len(daily), 6)
                group["tail_mean_pct"] = {
                    "top": round(100 * sum(row["top_rows"] / row["board_rows"]
                                           for row in daily) / len(daily), 3),
                    "bottom": round(100 * sum(row["bottom_rows"] / row["board_rows"]
                                              for row in daily) / len(daily), 3)}
                group["by_market_state"] = {
                    state: {"origin_days": len(rows),
                            "top_minus_bottom": round(
                                sum(row["top_rate"] - row["bottom_rate"] for row in rows)
                                / len(rows), 6)}
                    for state in STATES
                    if len(rows := [row for row in daily if row["market_state"] == state])
                    >= MIN_STATE_DAYS}
            result["groups"][key] = group
    if any(group["status"] == "research_diagnostic" for group in result["groups"].values()):
        result["status"] = "research_diagnostic"
    return result


def _read_run(path: Path, origin_day: str, as_of: datetime) -> dict[str, Any]:
    if path.is_symlink() or path.parent.is_symlink() or path.suffix != ".json":
        raise ValueError("direction_run_path_unsafe")
    with path.open("rb") as stream:
        raw = stream.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("direction_run_too_large")
    report = json.loads(raw)
    if (not isinstance(report, dict) or report.get("origin_day") != origin_day
            or report.get("report_sha256") != path.stem
            or _hash({key: value for key, value in report.items()
                      if key != "report_sha256"}) != path.stem
            or _stamp(report["report_saved_at"], "report_saved_at") > as_of):
        raise ValueError("direction_run_identity_invalid")
    return report


def _bounded_bytes(path: Path) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("direction_report_too_large")
    return raw


def build(*, root=ROOT, protocol_frozen_at: str,
          evaluation_as_of: str) -> dict[str, Any]:
    """Read a bounded set of local original-source receipts; no data fetching."""
    root = Path(root)
    as_of = _stamp(evaluation_as_of, "evaluation_as_of")
    base = evaluate_pairs([], protocol_frozen_at=protocol_frozen_at,
                          evaluation_as_of=evaluation_as_of)
    try:
        sessions, calendar_sha = _calendar(root, as_of)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return {**base, "status": "waiting_calendar", "calendar_sha256": None,
                "groups": {}}
    base["calendar_sha256"] = calendar_sha
    directory = root / "reports/sector_mainline_origin"
    if directory.is_symlink() or not directory.is_dir():
        return {**base, "status": "origin_history_missing", "groups": {}}
    origin_paths = sorted(directory.glob("????-??-??.json"))
    if len(origin_paths) > MAX_ORIGINS:
        return {**base, "status": "batch_too_large", "groups": {},
                "origin_file_count": len(origin_paths)}
    pairs = []
    total_bytes = 0
    skipped_sources: Counter[str] = Counter()
    for path in origin_paths:
        try:
            day = _date(path.stem, "origin_day")
            if path.is_symlink() or day not in sessions:
                raise ValueError("origin_path_or_calendar_invalid")
            total_bytes += path.stat().st_size
            if total_bytes > MAX_INPUT_BYTES:
                return {**base, "status": "batch_too_large", "groups": {},
                        "origin_file_count": len(origin_paths)}
            origin = read_day(root, day, now=as_of)
            index = sessions.index(day)
            if origin["calendar_window"] != sessions[index:index + max(HORIZONS) + 1]:
                raise ValueError("calendar_window_changed")
            runs_dir = root / "reports/sector_peer_continuation/runs" / day
            if runs_dir.is_symlink():
                raise ValueError("label_run_directory_unsafe")
            paths = sorted(runs_dir.glob("*.json")) if runs_dir.is_dir() else []
            if len(paths) > MAX_RUNS_PER_ORIGIN:
                raise ValueError("label_run_batch_too_large")
            total_bytes += sum(item.stat().st_size for item in paths)
            if total_bytes > MAX_INPUT_BYTES:
                return {**base, "status": "batch_too_large", "groups": {},
                        "origin_file_count": len(origin_paths)}
            runs = [_read_run(item, day, as_of) for item in paths]
            pairs.append((origin, runs))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            skipped_sources[str(error) or "invalid_source"] += 1
    output = evaluate_pairs(pairs, protocol_frozen_at=protocol_frozen_at,
                            evaluation_as_of=evaluation_as_of)
    output["calendar_sha256"] = calendar_sha
    output["skipped_source_files"] = dict(sorted(skipped_sources.items()))
    output["origin_file_count"] = len(origin_paths)
    if skipped_sources:
        output["status"] = "origin_source_gaps"
        for group in output["groups"].values():
            group["status"] = "source_selection_incomplete"
            group["rates_equal_origin_day"] = None
            group["by_market_state"] = None
            group["tail_mean_pct"] = None
    return output


def run_archive(**kwargs) -> dict[str, Any]:
    """Explicit offline invocation; preserve each attempt before latest."""
    root = Path(kwargs.get("root", ROOT))
    as_of = _stamp(kwargs["evaluation_as_of"], "evaluation_as_of")
    if as_of > datetime.now(timezone.utc).astimezone(as_of.tzinfo):
        raise ValueError("direction_evaluation_cutoff_in_future")
    result = build(**kwargs)
    result["report_saved_at"] = datetime.now(timezone.utc).isoformat()
    result["report_sha256"] = _hash(result)
    path = root / REPORT
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("direction_report_path_unsafe")
    raw = json.dumps(result, ensure_ascii=False, sort_keys=True,
                     allow_nan=False).encode("utf-8")
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("direction_report_too_large")
    runs = path.parent / "runs"
    if runs.is_symlink():
        raise ValueError("direction_runs_path_unsafe")
    runs.mkdir(parents=True, exist_ok=True)
    run_path = runs / (result["report_sha256"] + ".json")
    try:
        with run_path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if run_path.is_symlink() or _bounded_bytes(run_path) != raw:
            raise ValueError("direction_run_identity_conflict")
    publish(path, result)
    return result


def current_status(root=ROOT) -> dict[str, Any]:
    """Small website summary; no per-board outcomes or probabilities."""
    empty = {"schema": STATUS_SCHEMA, "status": "audit_not_run",
             "attempted_origin_days": 0, "calendar_sha256": None,
             "origin_file_count": 0, "skipped_source_files": {},
             "groups": {}, "report_sha256": None, **BOUNDARY}
    path = Path(root) / REPORT
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        return empty
    try:
        raw = _bounded_bytes(path)
        report = json.loads(raw)
        digest = report.pop("report_sha256")
        if (report.get("schema") != SCHEMA or _hash(report) != digest
                or any(report.get(key) != value for key, value in BOUNDARY.items())
                or report.get("status") not in {
                    "waiting_calendar", "origin_history_missing", "batch_too_large",
                    "insufficient_scored_origin_days", "research_diagnostic",
                    "origin_source_gaps"}
                or type(report.get("attempted_origin_days")) is not int
                or report["attempted_origin_days"] < 0):
            raise ValueError("direction_report_invalid")
        origin_files = report.get("origin_file_count", 0)
        if (type(origin_files) is not int or origin_files < report["attempted_origin_days"]
                or origin_files > 10000):
            raise ValueError("direction_origin_file_count_invalid")
        run_path = path.parent / "runs" / (digest + ".json")
        if (run_path.is_symlink() or run_path.parent.is_symlink()
                or not run_path.is_file()):
            raise ValueError("direction_saved_run_missing_or_changed")
        saved_raw = _bounded_bytes(run_path)
        if json.loads(saved_raw) != {**report, "report_sha256": digest}:
            raise ValueError("direction_saved_run_missing_or_changed")
        groups = {}
        for key, group in report["groups"].items():
            if key not in {f"{h}:{kind}" for h in HORIZONS for kind in KINDS}:
                raise ValueError("direction_group_key_invalid")
            value = group["rates_equal_origin_day"]
            tails = group.get("tail_mean_pct")
            if (group["status"] == "research_diagnostic"
                    and (not isinstance(value, dict)
                         or group["scored_origin_days"] < MIN_SCORE_DAYS
                         or not isinstance(tails, dict)
                         or set(tails) != {"top", "bottom"}
                         or any(type(item) not in (int, float)
                                or not math.isfinite(item) or not 20 <= item <= 100
                                for item in tails.values()))):
                raise ValueError("direction_group_support_invalid")
            if (type(group["scored_origin_days"]) is not int
                    or not 0 <= group["scored_origin_days"] <= report["attempted_origin_days"]
                    or type(group["scored_board_rows"]) is not int
                    or group["scored_board_rows"] < 0
                    or (value is not None and (not isinstance(value, dict)
                        or any(type(v) not in (int, float) or not math.isfinite(v)
                               for v in value.values())
                        or any(not 0 <= value[name] <= 1
                               for name in ("all_rate", "top_rate", "bottom_rate"))
                        or not -1 <= value["top_minus_bottom"] <= 1))):
                raise ValueError("direction_group_values_invalid")
            groups[key] = {"status": group["status"],
                           "scored_origin_days": group["scored_origin_days"],
                           "skipped_origin_days": group["skipped_origin_days"],
                           "rates_equal_origin_day": value,
                           "tail_mean_pct": tails}
        skipped = report.get("skipped_source_files", {})
        if (not isinstance(skipped, dict) or len(skipped) > 16
                or any(not isinstance(key, str) or len(key) > 100
                       or type(value) is not int or value < 0
                       for key, value in skipped.items())):
            raise ValueError("direction_source_skips_invalid")
        if (sum(skipped.values()) > origin_files - report["attempted_origin_days"]
                or (report["status"] != "batch_too_large"
                    and sum(skipped.values()) != origin_files - report["attempted_origin_days"])):
            raise ValueError("direction_source_skip_count_invalid")
        if report["status"] == "origin_source_gaps" and not skipped:
            raise ValueError("direction_source_gap_status_invalid")
        return {**empty, "status": report["status"],
                "attempted_origin_days": report["attempted_origin_days"],
                "origin_file_count": origin_files,
                "calendar_sha256": report.get("calendar_sha256"),
                "groups": groups, "report_sha256": digest,
                "skipped_source_files": skipped}
    except (OSError, ValueError, KeyError, TypeError, AttributeError,
            json.JSONDecodeError):
        return {**empty, "status": "audit_report_invalid"}


__all__ = ["build", "evaluate_pairs", "run_archive", "current_status"]
