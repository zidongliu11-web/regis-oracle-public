"""Bounded, research-only scoring of saved ex-ante sector proxy probabilities.

Only issued forecast files, their matched first-write origins and later verified
full-denominator labels are eligible. This is not a tradable-return evaluator.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping

from . import sector_state_baseline as baseline
from .cloud_research_worker import ROOT, publish
from .sector_first_write_eval import _same_denominator, _same_source
from .sector_forward_ledger import build as build_ledger
from .sector_mainline_origin import read_day as read_origin_day
from .sector_proxy_forecast import read_day as read_forecast_day


SCHEMA = "sector_proxy_forward_score.v1"
STATUS_SCHEMA = "sector_proxy_forward_score_status.v1"
REPORT = Path("reports/sector_proxy_forward_score/latest.json")
RUNS = Path("reports/sector_proxy_forward_score/runs")
RECOVERIES = Path("reports/sector_proxy_forward_score/recoveries")
FROZEN_START_DAY = "2026-09-28"
FROZEN_PROTOCOL_AT = "2026-09-27T20:51:02+00:00"  # GitHub contract commit c12a92d
MAX_REPORT_BYTES = 4 * 1024 * 1024
MIN_COMPLETE_ORIGIN_DAYS = 20
MODELS = ("unconditional", "qualification", "market_state_qualification")
BOUNDARY = {"research_only": True, "forecast_validated": False,
            "production_eligible": False, "generates_alert": False,
            "returns_computed": False, "leader_validated": False,
            "economic_target_A_validated": False,
            "external_issue_time_attested": False}


def _run_key(as_of: datetime) -> str:
    return as_of.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _read_run(path: Path, *, now: datetime) -> dict[str, Any]:
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise ValueError("score_run_missing_or_unsafe")
    with path.open("rb") as stream:
        raw = stream.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("score_report_too_large")
    report = json.loads(raw)
    if not isinstance(report, dict):
        raise ValueError("score_report_invalid")
    digest = report.get("report_sha256")
    if (not baseline._hex(digest)
            or baseline._hash({key: value for key, value in report.items()
                               if key != "report_sha256"}) != digest
            or report.get("schema") != SCHEMA
            or any(report.get(key) != value for key, value in BOUNDARY.items())):
        raise ValueError("score_run_contract_invalid")
    as_of = baseline._stamp(report.get("evaluation_as_of"), "score_report_as_of")
    saved = baseline._stamp(report.get("report_saved_at"), "score_report_saved_at")
    frozen = baseline._stamp(report.get("protocol_frozen_at"), "score_report_frozen_at")
    if (report.get("batch_start_day") != FROZEN_START_DAY
            or report.get("evaluation_start") != FROZEN_START_DAY
            or frozen != baseline._stamp(FROZEN_PROTOCOL_AT, "frozen_contract_at")
            or frozen > as_of or as_of > saved
            or saved > now.astimezone(baseline.SHANGHAI)
            or path.stem != _run_key(as_of)
            or not baseline._date(report.get("batch_start_day"), "score_batch_start")
            <= baseline._date(report.get("evaluation_start"), "score_start")
            <= baseline._date(report.get("batch_end_day"), "score_batch_end")
            <= as_of.date().isoformat()):
        raise ValueError("score_run_time_or_window_invalid")
    return report


def _run_names(root: Path) -> list[str]:
    runs_dir = root / RUNS
    if runs_dir.is_symlink():
        raise ValueError("score_runs_path_unsafe")
    if not runs_dir.exists():
        return []
    if not runs_dir.is_dir():
        raise ValueError("score_runs_path_unsafe")
    names = sorted(path.name for path in runs_dir.iterdir())
    if (len(names) > 512
            or any(not re.fullmatch(r"\d{8}T\d{12}Z\.json", name) for name in names)
            or any((runs_dir / name).is_symlink() or not (runs_dir / name).is_file()
                   for name in names)):
        raise ValueError("score_runs_invalid")
    return names


def _read_pointer(root: Path) -> dict[str, Any]:
    pointer_path = root / REPORT
    if (pointer_path.is_symlink() or pointer_path.parent.is_symlink()
            or not pointer_path.is_file()):
        raise ValueError("score_pointer_missing_or_unsafe")
    with pointer_path.open("rb") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("score_pointer_too_large")
    pointer = json.loads(raw)
    if not isinstance(pointer, dict):
        raise ValueError("score_pointer_invalid")
    digest = pointer.get("pointer_sha256")
    if (pointer.get("schema") != "sector_proxy_forward_score_pointer.v1"
            or not baseline._hex(digest)
            or baseline._hash({key: value for key, value in pointer.items()
                               if key != "pointer_sha256"}) != digest
            or not baseline._hex(pointer.get("report_sha256"))
            or not isinstance(pointer.get("run_key"), str)
            or not re.fullmatch(r"\d{8}T\d{12}Z", pointer["run_key"])):
        raise ValueError("score_pointer_contract_invalid")
    return pointer


def _read_latest(root: Path, *, now: datetime) -> tuple[dict[str, Any], int]:
    pointer = _read_pointer(root)
    names = _run_names(root)
    if not names or names[-1] != pointer["run_key"] + ".json":
        raise ValueError("score_pointer_not_latest_run")
    report = _read_run(root / RUNS / names[-1], now=now)
    if report["report_sha256"] != pointer["report_sha256"]:
        raise ValueError("score_pointer_report_mismatch")
    return report, len(names)


def _pointer_for(report: Mapping[str, Any], key: str) -> dict[str, Any]:
    pointer = {"schema": "sector_proxy_forward_score_pointer.v1",
               "run_key": key, "report_sha256": report["report_sha256"]}
    pointer["pointer_sha256"] = baseline._hash(pointer)
    return pointer


def _recover_latest(root: Path, *, report: dict[str, Any], key: str,
                    prior_key: str | None, now: datetime) -> None:
    """Finish a verified interrupted first-write; leave an append-only receipt."""
    mode = "initial_pointer_missing" if prior_key is None else "unpublished_latest"
    receipt_path = root / RECOVERIES / (key + ".json")
    if receipt_path.exists() or receipt_path.is_symlink():
        if receipt_path.is_symlink() or receipt_path.parent.is_symlink():
            raise ValueError("score_recovery_unsafe")
        with receipt_path.open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError("score_recovery_too_large")
        receipt = json.loads(raw)
        digest = receipt.get("receipt_sha256") if isinstance(receipt, dict) else None
        if (not baseline._hex(digest)
                or baseline._hash({k: v for k, v in receipt.items()
                                   if k != "receipt_sha256"}) != digest
                or receipt.get("schema") != "sector_proxy_forward_score_recovery.v1"
                or receipt.get("mode") != mode or receipt.get("run_key") != key
                or receipt.get("prior_pointer_key") != prior_key
                or receipt.get("report_sha256") != report["report_sha256"]
                or baseline._stamp(receipt.get("recovered_at"), "recovered_at") > now):
            raise ValueError("score_recovery_receipt_invalid")
    else:
        receipt = {"schema": "sector_proxy_forward_score_recovery.v1",
                   "mode": mode, "run_key": key, "prior_pointer_key": prior_key,
                   "report_sha256": report["report_sha256"],
                   "recovered_at": now.isoformat()}
        receipt["receipt_sha256"] = baseline._hash(receipt)
        _save_first_run(receipt_path, json.dumps(
            receipt, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8"))
    publish(root / REPORT, _pointer_for(report, key))
    _read_latest(root, now=now)


def _save_first_run(path: Path, encoded: bytes) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("score_run_path_unsafe")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".score-",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _group_result(*, attempted: int, expected_rows: int, per_origin: list[dict],
                  skips: Counter[str]) -> dict[str, Any]:
    count = len(per_origin)
    result: dict[str, Any] = {
        "status": ("paired_research_diagnostic" if count >= MIN_COMPLETE_ORIGIN_DAYS
                   else "insufficient_complete_origin_days"),
        "attempted_origin_days": attempted,
        "expected_board_rows": expected_rows,
        "common_origin_days": count,
        "common_board_rows": sum(row["board_rows"] for row in per_origin),
        "skipped_origin_days": dict(sorted(skips.items())),
        "scores_equal_origin_day": None,
        "per_origin": per_origin,
    }
    if count >= MIN_COMPLETE_ORIGIN_DAYS:
        scores = {
            measure: {model: sum(row[measure][model] for row in per_origin) / count
                      for model in MODELS}
            for measure in ("mean_brier", "mean_log_loss")
        }
        scores["paired_delta_brier_vs_unconditional"] = (
            scores["mean_brier"]["market_state_qualification"]
            - scores["mean_brier"]["unconditional"])
        scores["paired_delta_brier_vs_qualification"] = (
            scores["mean_brier"]["market_state_qualification"]
            - scores["mean_brier"]["qualification"])
        result["scores_equal_origin_day"] = scores
    return result


def evaluate(*, root: Path, ledger: Mapping[str, Any], evaluation_start: str,
             protocol_frozen_at: str, evaluation_as_of: str) -> dict[str, Any]:
    """Evaluate one complete <=60-origin ledger batch, without writing a report."""
    root = Path(root)
    start = baseline._date(evaluation_start, "score_start")
    frozen = baseline._stamp(protocol_frozen_at, "score_protocol_frozen_at")
    as_of = baseline._stamp(evaluation_as_of, "score_evaluation_as_of")
    if frozen > as_of or not isinstance(ledger, Mapping):
        raise ValueError("score_protocol_or_ledger_invalid")
    origins, rows, batches = baseline._ledgers(ledger)
    by_day: dict[str, list[dict]] = {}
    for row in rows:
        by_day.setdefault(row["origin_day"], []).append(row)
    candidates = [day for day in origins if day >= start and day <= as_of.date().isoformat()]
    selected: list[tuple[str, dict, list[dict]]] = []
    skips: Counter[str] = Counter()
    cohort: dict | None = None
    record_ids: list[tuple[str, str, str]] = []
    for day in candidates:
        ledger_origin = origins[day]
        if ledger_origin["status"] != "verified":
            skips["ledger_origin_unverified"] += 1
            continue
        try:
            forecast = read_forecast_day(root, day, now=as_of)
            origin = read_origin_day(root, day, now=as_of)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            skips["forecast_or_origin_missing_invalid"] += 1
            continue
        issued = baseline._stamp(forecast["issued_at"], "saved_forecast_issued_at")
        if issued < frozen:
            skips["issued_before_protocol_freeze"] += 1
            continue
        if (forecast.get("origin_day") != day
                or forecast.get("origin_record_sha256") != origin["record_sha256"]
                or forecast.get("target_meaning") !=
                "future_frozen_screen_requalification_not_mainline_or_return"
                or not _same_source(origin, ledger_origin)
                or not _same_denominator(origin, by_day.get(day, []))):
            skips["source_or_denominator_mismatch"] += 1
            continue
        this_cohort = {"source": forecast["sector_source"],
                       "source_contract_id": forecast["sector_contract_id"],
                       "market_identity": forecast["market_identity"],
                       "model": forecast["model"]}
        if cohort is not None and this_cohort != cohort:
            skips["issuer_cohort_changed"] += 1
            continue
        cohort = this_cohort
        selected.append((day, forecast, by_day[day]))
        record_ids.append((day, forecast["record_sha256"],
                           origin["record_sha256"]))

    groups: dict[str, dict] = {}
    scored_inputs: list[dict] = []
    for horizon in baseline.HORIZONS:
        for kind in baseline.KINDS:
            per_origin: list[dict] = []
            group_skips: Counter[str] = Counter()
            expected_rows = 0
            for day, forecast, day_rows in selected:
                boards = [board for board in forecast["boards"]
                          if board["kind"] == kind
                          and board["screen_status"] != "unassessable"]
                expected_rows += len(boards)
                if not boards:
                    group_skips["no_evaluable_boards"] += 1
                    continue
                key_rows = {(row["kind"], row["code"]): row for row in day_rows
                            if row["horizon"] == horizon}
                chosen = []
                for board in boards:
                    qualification = board["screen_status"]
                    cell = forecast["groups"][f"{horizon}:{kind}:{qualification}"]
                    chosen.append((board, key_rows[(kind, board["code"])],
                                   cell["probabilities"]))
                if any(probabilities is None for _, _, probabilities in chosen):
                    group_skips["forecast_abstained_or_partial_support"] += 1
                    continue
                issued = baseline._stamp(forecast["issued_at"], "score_issued_at")
                if any(row["target_day"] is None
                       or row["target_day"] > as_of.date().isoformat()
                       or (row["label_available_at"] is not None
                           and baseline._stamp(row["label_available_at"], "label_at") > as_of)
                       for _, row, _ in chosen):
                    group_skips["target_not_matured_as_of"] += 1
                    continue
                if any(row["future_qualified"] is None for _, row, _ in chosen):
                    group_skips["label_unknown_or_source_gap"] += 1
                    continue
                if any(baseline._stamp(row["label_available_at"], "label_at") <= issued
                       for _, row, _ in chosen):
                    group_skips["label_not_after_forecast_issue"] += 1
                    continue
                brier = {model: 0.0 for model in MODELS}
                log_loss = {model: 0.0 for model in MODELS}
                for board, row, probabilities in chosen:
                    outcome = row["future_qualified"]
                    for model in MODELS:
                        loss_brier, loss_log = baseline._score(probabilities[model], outcome)
                        brier[model] += loss_brier
                        log_loss[model] += loss_log
                    scored_inputs.append({"origin_day": day, "horizon": horizon,
                                          "kind": kind, "code": board["code"],
                                          "target_day": row["target_day"],
                                          "target_packet_id": row["target_packet_id"],
                                          "label_available_at": row["label_available_at"],
                                          "outcome": outcome,
                                          "forecast_record_sha256": forecast["record_sha256"],
                                          "probabilities": probabilities})
                divisor = len(chosen)
                per_origin.append({"origin_day": day, "board_rows": divisor,
                                   "mean_brier": {name: value / divisor
                                                  for name, value in brier.items()},
                                   "mean_log_loss": {name: value / divisor
                                                     for name, value in log_loss.items()}})
            groups[f"{horizon}:{kind}"] = _group_result(
                attempted=len(selected), expected_rows=expected_rows,
                per_origin=per_origin, skips=group_skips)

    status = ("no_matching_first_write_forecasts" if not selected else
              "paired_research_diagnostic" if any(
                  group["status"] == "paired_research_diagnostic"
                  for group in groups.values()) else
              "insufficient_complete_origin_days")
    return {"schema": SCHEMA, "status": status,
            "evaluation_start": start, "protocol_frozen_at": frozen.isoformat(),
            "evaluation_as_of": as_of.isoformat(),
            "candidate_origin_days": len(candidates),
            "matched_forecast_origin_count": len(selected),
            "matched_forecast_origin_days": [day for day, _, _ in selected],
            "skip_reasons": dict(sorted(skips.items())),
            "forecast_origin_record_ids_sha256": baseline._hash(record_ids),
            "ledger_input_ids_sha256": baseline._hash(
                [batch["input_sha256"] for batch in batches]),
            "scored_input_sha256": baseline._hash(scored_inputs),
            "source_cohort_sha256": baseline._hash(cohort) if cohort else None,
            "groups": groups,
            "target": "future_frozen_screen_requalification_not_mainline_or_return",
            "chronology": "self_reported_first_write_not_external_attestation",
            **BOUNDARY}


def run_archive(*, root=ROOT, start_day: str, end_day: str, score_start: str,
                protocol_frozen_at: str, evaluation_as_of: str) -> dict[str, Any]:
    """Manual bounded research job; the website only reads its saved report."""
    root = Path(root)
    start = baseline._date(start_day, "batch_start_day")
    end = baseline._date(end_day, "batch_end_day")
    score = baseline._date(score_start, "score_start_day")
    as_of = baseline._stamp(evaluation_as_of, "evaluation_as_of")
    frozen = baseline._stamp(protocol_frozen_at, "score_frozen_at")
    current = datetime.now(timezone.utc)
    if (start != FROZEN_START_DAY or score != FROZEN_START_DAY
            or frozen != baseline._stamp(FROZEN_PROTOCOL_AT, "frozen_contract_at")
            or not start <= end <= as_of.date().isoformat()
            or as_of > current.astimezone(baseline.SHANGHAI)):
        raise ValueError("score_range_or_cutoff_invalid")
    run_path = root / RUNS / (_run_key(as_of) + ".json")
    pointer_path = root / REPORT

    def check_parameters(saved: Mapping[str, Any]) -> None:
        if (saved["batch_start_day"] != start or saved["batch_end_day"] != end
                or saved["evaluation_start"] != score
                or baseline._stamp(saved["protocol_frozen_at"], "old_frozen_at") != frozen):
            raise ValueError("score_same_cutoff_protocol_conflict")

    names = _run_names(root)
    previous = None
    if pointer_path.exists() or pointer_path.is_symlink():
        pointer = _read_pointer(root)
        pointer_name = pointer["run_key"] + ".json"
        if pointer_name not in names:
            raise ValueError("score_pointer_run_missing")
        previous = _read_run(root / RUNS / pointer_name, now=current)
        if previous["report_sha256"] != pointer["report_sha256"]:
            raise ValueError("score_pointer_report_mismatch")
        if names[-1] != pointer_name:
            if (len(names) < 2 or names[-2] != pointer_name
                    or names[-1] != run_path.name):
                raise ValueError("score_orphan_runs_need_review")
            orphan = _read_run(run_path, now=current)
            check_parameters(orphan)
            previous_as_of = baseline._stamp(previous["evaluation_as_of"], "latest_as_of")
            if (as_of <= previous_as_of or end < previous["batch_end_day"]
                    or previous["batch_start_day"] != start
                    or previous["evaluation_start"] != score
                    or baseline._stamp(previous["protocol_frozen_at"], "latest_frozen_at")
                       != frozen):
                raise ValueError("score_nonmonotone_or_protocol_changed")
            _recover_latest(root, report=orphan, key=run_path.stem,
                            prior_key=pointer["run_key"], now=current)
            return orphan
    elif names:
        if names != [run_path.name]:
            raise ValueError("score_orphan_runs_need_review")
        orphan = _read_run(run_path, now=current)
        check_parameters(orphan)
        _recover_latest(root, report=orphan, key=run_path.stem,
                        prior_key=None, now=current)
        return orphan

    if previous is not None:
        previous_as_of = baseline._stamp(previous["evaluation_as_of"], "latest_as_of")
        if as_of == previous_as_of:
            check_parameters(previous)
            return previous
        if (as_of < previous_as_of or end < previous["batch_end_day"]
                or previous["batch_start_day"] != start
                or previous["evaluation_start"] != score
                or baseline._stamp(previous["protocol_frozen_at"], "latest_frozen_at") != frozen):
            raise ValueError("score_nonmonotone_or_protocol_changed")
    ledger = build_ledger(root=root, start_day=start, end_day=end,
                          evaluation_as_of=as_of.isoformat())
    report = evaluate(root=root, ledger=ledger, evaluation_start=score,
                      protocol_frozen_at=protocol_frozen_at,
                      evaluation_as_of=as_of.isoformat())
    report.update(batch_start_day=start, batch_end_day=end,
                  ledger_status=ledger["status"],
                  report_saved_at=datetime.now(timezone.utc).isoformat())
    report["report_sha256"] = baseline._hash(report)
    if pointer_path.is_symlink() or pointer_path.parent.is_symlink():
        raise ValueError("score_report_path_unsafe")
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True,
                         allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_REPORT_BYTES:
        raise ValueError("score_report_too_large")
    _save_first_run(run_path, encoded)
    publish(pointer_path, _pointer_for(report, _run_key(as_of)))
    return report


def current_status(root=ROOT) -> dict[str, Any]:
    """Small, fail-closed status for the original site's research panel."""
    empty = {"schema": STATUS_SCHEMA, "status": "evaluation_not_run",
             "candidate_origin_days": 0, "matched_forecast_origin_count": 0,
             "skip_reasons": {}, "groups": {}, "archived_runs": 0,
             "evaluation_as_of": None,
             "report_sha256": None, **BOUNDARY}
    path = Path(root) / REPORT
    if not path.exists() and not path.is_symlink():
        try:
            return ({**empty, "status": "evaluation_report_invalid"}
                    if _run_names(Path(root)) else empty)
        except (OSError, ValueError):
            return {**empty, "status": "evaluation_report_invalid"}
    try:
        report, archived_runs = _read_latest(Path(root), now=datetime.now(timezone.utc))
        digest = report["report_sha256"]
        if (report.get("status") not in {
                    "no_matching_first_write_forecasts", "insufficient_complete_origin_days",
                    "paired_research_diagnostic"}
                or report.get("target") !=
                    "future_frozen_screen_requalification_not_mainline_or_return"
                or type(report.get("candidate_origin_days")) is not int
                or type(report.get("matched_forecast_origin_count")) is not int
                or not 0 <= report["matched_forecast_origin_count"] <= report["candidate_origin_days"]
                or not isinstance(report.get("skip_reasons"), dict)
                or any(type(value) is not int or value < 0
                       for value in report["skip_reasons"].values())
                or report["matched_forecast_origin_count"] + sum(report["skip_reasons"].values())
                    != report["candidate_origin_days"]):
            raise ValueError("score_report_contract_invalid")
        groups = report.get("groups")
        expected = {f"{h}:{kind}" for h in baseline.HORIZONS for kind in baseline.KINDS}
        if not isinstance(groups, dict) or set(groups) != expected:
            raise ValueError("score_groups_invalid")
        public_groups = {}
        ready_groups = 0
        for key, group in groups.items():
            if (not isinstance(group, dict)
                    or group.get("status") not in {
                        "paired_research_diagnostic", "insufficient_complete_origin_days"}
                    or type(group.get("attempted_origin_days")) is not int
                    or type(group.get("common_origin_days")) is not int
                    or type(group.get("expected_board_rows")) is not int
                    or type(group.get("common_board_rows")) is not int
                    or not 0 <= group["common_origin_days"] <= group["attempted_origin_days"]
                    or group["attempted_origin_days"] != report["matched_forecast_origin_count"]
                    or not 0 <= group["common_board_rows"] <= group["expected_board_rows"]
                    or not isinstance(group.get("skipped_origin_days"), dict)
                    or any(type(value) is not int or value < 0
                           for value in group["skipped_origin_days"].values())
                    or sum(group["skipped_origin_days"].values())
                       + group["common_origin_days"] != group["attempted_origin_days"]):
                raise ValueError("score_group_contract_invalid")
            scores = group.get("scores_equal_origin_day")
            enough = group["common_origin_days"] >= MIN_COMPLETE_ORIGIN_DAYS
            if enough:
                ready_groups += 1
                if (group["status"] != "paired_research_diagnostic"
                        or not isinstance(scores, dict)
                        or any(not isinstance(scores.get(measure), dict)
                               or set(scores[measure]) != set(MODELS)
                               or any(type(value) not in (float, int)
                                      or not math.isfinite(value)
                                      or value < 0
                                      or (measure == "mean_brier" and value > 1)
                                      for value in scores[measure].values())
                               for measure in ("mean_brier", "mean_log_loss"))
                        or any(type(scores.get(key)) not in (float, int)
                               or not math.isfinite(scores[key])
                               for key in ("paired_delta_brier_vs_unconditional",
                                           "paired_delta_brier_vs_qualification"))):
                    raise ValueError("score_group_scores_invalid")
            elif scores is not None or group["status"] != "insufficient_complete_origin_days":
                raise ValueError("score_group_premature_scores")
            per_origin = group.get("per_origin")
            if (not isinstance(per_origin, list)
                    or len(per_origin) != group["common_origin_days"]
                    or sum(row.get("board_rows", 0) for row in per_origin
                           if isinstance(row, dict)) != group["common_board_rows"]):
                raise ValueError("score_group_origin_rows_invalid")
            public_groups[key] = {field: group[field] for field in (
                "status", "attempted_origin_days", "expected_board_rows",
                "common_origin_days", "common_board_rows", "skipped_origin_days",
                "scores_equal_origin_day")}
        expected_status = ("no_matching_first_write_forecasts"
                           if report["matched_forecast_origin_count"] == 0 else
                           "paired_research_diagnostic" if ready_groups else
                           "insufficient_complete_origin_days")
        if report["status"] != expected_status:
            raise ValueError("score_report_status_mismatch")
        return {**empty, "status": report["status"],
                "candidate_origin_days": report["candidate_origin_days"],
                "matched_forecast_origin_count": report["matched_forecast_origin_count"],
                "skip_reasons": report["skip_reasons"],
                "groups": public_groups,
                "archived_runs": archived_runs,
                "evaluation_as_of": report["evaluation_as_of"],
                "report_sha256": digest}
    except (OSError, ValueError, TypeError, KeyError, AttributeError,
            json.JSONDecodeError):
        return {**empty, "status": "evaluation_report_invalid"}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Score saved sector proxy forecasts")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--score-start", required=True)
    parser.add_argument("--frozen-at", required=True)
    parser.add_argument("--as-of", required=True)
    args = parser.parse_args()
    outcome = run_archive(root=args.root, start_day=args.start, end_day=args.end,
                          score_start=args.score_start,
                          protocol_frozen_at=args.frozen_at,
                          evaluation_as_of=args.as_of)
    print(json.dumps({key: outcome[key] for key in (
        "status", "candidate_origin_days", "matched_forecast_origin_count",
        "skip_reasons", "report_sha256")}, ensure_ascii=False))


__all__ = ["evaluate", "run_archive", "current_status", "SCHEMA"]
