"""First-write, research-only forecast of a sector screen re-qualifying.

This is not a mainline/leader or executable-return forecast. An issued file
is kept even when support is inadequate; later labels cannot repair it.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from .cloud_research_worker import ROOT
from .emotion_pit_archive import read_day as read_market_pit
from .sector_first_write_eval import _same_denominator, _same_source
from .sector_forward_ledger import HORIZONS, _calendar, build as build_ledger
from .sector_mainline_origin import (_window, current_status as origin_status,
                                     read_day as read_origin)
from . import sector_state_baseline as baseline


SCHEMA = "sector_proxy_first_write_forecast.v1"
STATUS_SCHEMA = "sector_proxy_forecast_status.v1"
MAX_TRAIN_ORIGINS = 180
MAX_RECORD_BYTES = 16 * 1024 * 1024
BOUNDARY = {"research_only": True, "forecast_validated": False,
            "production_eligible": False, "generates_alert": False,
            "returns_computed": False, "leader_validated": False,
            "economic_target_A_validated": False,
            "external_issue_time_attested": False}


def _path(root: Path, day: str) -> Path:
    return root / "reports/sector_proxy_forecast" / (day + ".json")


def _model_identity() -> dict[str, Any]:
    return {"name": "market_state_x_current_screen_laplace_v1",
            "train_origin_days": baseline.MIN_TRAIN_DAYS,
            "cell_origin_days": baseline.MIN_CELL_DAYS,
            "maximum_lookback_origins": MAX_TRAIN_ORIGINS,
            "label": "future_frozen_screen_requalification_not_return"}


def prepare(origin: Mapping[str, Any],
            ledgers: Mapping[str, Any] | Sequence[Mapping[str, Any]],
            market_rows: Sequence[Mapping[str, Any]], *, issued_at: str) -> dict[str, Any]:
    """Pure calculation over caller-authenticated origin and ledger receipts.

    Production ``capture`` reads the strict first-write origin and verified
    packet ledger. This function still checks their cross-identity and PIT
    chronology, and uses only labels mature strictly before the issue clock.
    """
    issued = baseline._stamp(issued_at, "forecast_issue_at")
    if (not isinstance(origin, Mapping)
            or origin.get("schema") != "sector_mainline_origin.v1"
            or origin.get("status") != "origin_recorded"
            or any(origin.get(key) != value for key, value in
                   {"research_only": True, "forecast_validated": False,
                    "production_eligible": False, "generates_alert": False,
                    "returns_computed": False, "leader_validated": False}.items())
            or not baseline._hex(origin.get("record_sha256"))):
        raise ValueError("forecast_origin_boundary_invalid")
    day = baseline._date(origin.get("origin_day"), "forecast_origin_day")
    source_issue = baseline._stamp(origin.get("issued_at"), "origin_issue_at")
    next_open = baseline._stamp(origin.get("next_open_at"), "forecast_next_open_at")
    if not source_issue <= issued < next_open:
        raise ValueError("forecast_issue_window_invalid")
    origins, rows, batches = baseline._ledgers(ledgers)
    if not origins or next(reversed(origins)) != day:
        raise ValueError("forecast_current_ledger_day_missing")
    current = origins[day]
    if (current.get("status") != "verified" or not _same_source(origin, current)
            or not _same_denominator(origin, [row for row in rows
                                               if row["origin_day"] == day])):
        raise ValueError("forecast_current_denominator_or_source_mismatch")
    for previous_day, previous in origins.items():
        if (previous.get("status") == "verified"
                and baseline._stamp(previous["observed_at"], "training_observed_at") > issued):
            raise ValueError("forecast_future_sector_receipt")
    raw_market = [row for row in market_rows
                  if isinstance(row, Mapping)
                  and row.get("trade_date", "") < day
                  and baseline._stamp(row.get("state_available_at"),
                                      "training_market_available_at") <= issued]
    market = baseline._market(raw_market, origins)
    identity = tuple(origin["market"][key] for key in
                     ("scope_id", "taxonomy_version", "feature_schema_version"))
    if any(row["identity"] != identity for row in market.values()):
        raise ValueError("forecast_market_scope_or_version_mismatch")
    state = origin["market"]["state"]
    if state not in baseline.STATES or baseline._stamp(
            origin["market"]["captured_at"], "forecast_market_at") > source_issue:
        raise ValueError("forecast_current_market_invalid")

    # A board row is one correlated observation. Support is counted in
    # distinct, mature origin days, not in the often large number of boards.
    aggregates: dict[tuple, dict[str, Any]] = {}
    accepted: list[dict[str, Any]] = []
    for row in rows:
        prior_day = row["origin_day"]
        if prior_day >= day or row["future_qualified"] is None:
            continue
        label_at = baseline._stamp(row["label_available_at"], "training_label_at")
        if label_at >= issued:
            continue
        prior = origins[prior_day]
        if (prior.get("status") != "verified"
                or prior["source"] != current["source"]
                or prior["source_contract_id"] != current["source_contract_id"]):
            continue
        prior_state = baseline._market_at(prior_day, market, origins, cutoff=issued)
        if prior_state is None:
            continue
        qualified = row["origin_qualified"]
        prefix = (row["horizon"], row["kind"])
        for key in (prefix + ("all",),
                    prefix + ("qualification", qualified),
                    prefix + ("state_qualification", prior_state, qualified)):
            bucket = aggregates.setdefault(key, {"count": 0, "positive": 0, "days": set()})
            bucket["count"] += 1
            bucket["positive"] += row["future_qualified"]
            bucket["days"].add(prior_day)
        accepted.append({key: row[key] for key in (
            "origin_day", "origin_packet_id", "kind", "code", "horizon",
            "origin_qualified", "target_day", "target_packet_id",
            "label_available_at", "future_qualified")})

    groups: dict[str, dict[str, Any]] = {}
    for horizon in HORIZONS:
        for kind in baseline.KINDS:
            all_bucket = aggregates.get((horizon, kind, "all"),
                                        {"count": 0, "positive": 0, "days": set()})
            for qualified in (False, True):
                q_bucket = aggregates.get((horizon, kind, "qualification", qualified),
                                          {"count": 0, "positive": 0, "days": set()})
                state_bucket = aggregates.get((horizon, kind, "state_qualification",
                                               state, qualified),
                                              {"count": 0, "positive": 0, "days": set()})
                supported = (len(all_bucket["days"]) >= baseline.MIN_TRAIN_DAYS
                             and len(q_bucket["days"]) >= baseline.MIN_CELL_DAYS
                             and len(state_bucket["days"]) >= baseline.MIN_CELL_DAYS)
                groups[f"{horizon}:{kind}:{'qualified' if qualified else 'not_qualified'}"] = {
                    "status": "research_probability" if supported else "abstain_insufficient_mature_origin_days",
                    "support_origin_days": {"all": len(all_bucket["days"]),
                                            "qualification": len(q_bucket["days"]),
                                            "state_qualification": len(state_bucket["days"])},
                    "support_board_rows": {"all": all_bucket["count"],
                                           "qualification": q_bucket["count"],
                                           "state_qualification": state_bucket["count"]},
                    "probabilities": ({"unconditional": baseline._probability(all_bucket),
                                       "qualification": baseline._probability(q_bucket),
                                       "market_state_qualification": baseline._probability(state_bucket)}
                                      if supported else None),
                }
    boards = [{"kind": row["kind"], "code": row["code"], "name": row["name"],
               "screen_status": row["screen_status"]} for row in origin["boards"]]
    eligible = sum(row["screen_status"] != "unassessable" for row in boards)
    if (len(boards) != origin["board_count"] or eligible == 0
            or len({(row["kind"], row["code"]) for row in boards}) != len(boards)):
        raise ValueError("forecast_board_population_invalid")
    supported = sum(row["screen_status"] != "unassessable"
                    and any(groups[f"{h}:{row['kind']}:{row['screen_status']}"]["probabilities"]
                            is not None for h in HORIZONS) for row in boards)
    market_identity = [{"day": prior_day, "state": item["state"],
                        "source_sha256": item["source_snapshot_sha256"],
                        "universe_sha256": item["universe_snapshot_sha256"],
                        "available_at": item["state_available_at"].isoformat()}
                       for prior_day, item in sorted(market.items())]
    payload = {"schema": SCHEMA,
               "status": "research_probability_issued" if supported else "abstained_insufficient_history",
               "origin_day": day, "issued_at": issued.isoformat(),
               "next_open_at": origin["next_open_at"],
               "origin_record_sha256": origin["record_sha256"],
               "sector_packet_id": origin["sector"]["packet_id"],
               "sector_source": origin["sector"]["source"],
               "sector_contract_id": origin["sector"]["source_contract_id"],
               "market_state": state, "market_identity": list(identity),
               "population": origin["sector"]["population"],
               "target_sessions": origin["target_sessions"],
               "boards": boards, "board_count": len(boards),
               "evaluable_board_count": eligible,
               "supported_board_count_any_horizon": supported,
               "groups": groups, "model": _model_identity(),
               "training_label_rows": len(accepted),
               "training_input_sha256": baseline._hash({"labels": accepted,
                                                        "market": market_identity}),
               "ledger_input_ids_sha256": baseline._hash(
                   [batch["input_sha256"] for batch in batches]),
               "target_meaning": "future_frozen_screen_requalification_not_mainline_or_return",
               "chronology": "self_reported_first_write_not_external_attestation",
               **BOUNDARY}
    payload["record_sha256"] = baseline._hash(payload)
    if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValueError("forecast_record_too_large")
    return payload


def read_day(root: Path, day: str, *, now: datetime | None = None) -> dict[str, Any]:
    root, day = Path(root), baseline._date(day, "forecast_day")
    path = _path(root, day)
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise ValueError("forecast_record_missing_or_unsafe")
    with path.open("rb") as stream:
        raw = stream.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError("forecast_record_too_large")
    try:
        record = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("forecast_record_json_invalid") from error
    if not isinstance(record, dict):
        raise ValueError("forecast_record_contract_invalid")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("forecast_read_timezone_required")
    issued = baseline._stamp(record.get("issued_at"), "forecast_issued_at")
    if (record.get("schema") != SCHEMA
            or record.get("origin_day") != day
            or record.get("status") not in {
                "research_probability_issued", "abstained_insufficient_history"}
            or any(record.get(key) != value for key, value in BOUNDARY.items())
            or record.get("target_meaning") !=
            "future_frozen_screen_requalification_not_mainline_or_return"
            or not baseline._hex(record.get("record_sha256"))
            or baseline._hash({key: value for key, value in record.items()
                               if key != "record_sha256"}) != record["record_sha256"]
            or issued > current.astimezone(baseline.SHANGHAI)
            or issued >= baseline._stamp(record.get("next_open_at"), "forecast_next_open_at")
            or not baseline._hex(record.get("origin_record_sha256"))
            or not baseline._hex(record.get("training_input_sha256"))):
        raise ValueError("forecast_record_contract_invalid")
    origin = read_origin(root, day, now=current)
    if (record["origin_record_sha256"] != origin["record_sha256"]
            or record.get("sector_packet_id") != origin["sector"]["packet_id"]
            or record.get("sector_source") != origin["sector"]["source"]
            or record.get("sector_contract_id") != origin["sector"]["source_contract_id"]
            or record.get("market_state") != origin["market"]["state"]
            or record.get("market_identity") != [origin["market"][key] for key in
                                                 ("scope_id", "taxonomy_version",
                                                  "feature_schema_version")]
            or record.get("population") != origin["sector"]["population"]
            or record.get("target_sessions") != origin["target_sessions"]
            or record.get("board_count") != origin["board_count"]
            or record.get("model") != _model_identity()
            or issued < baseline._stamp(origin["issued_at"], "origin_issued_at")):
        raise ValueError("forecast_origin_lineage_invalid")
    expected_boards = [{"kind": row["kind"], "code": row["code"],
                        "name": row["name"], "screen_status": row["screen_status"]}
                       for row in origin["boards"]]
    boards = record.get("boards")
    if boards != expected_boards:
        raise ValueError("forecast_board_denominator_invalid")
    eligible = sum(row["screen_status"] != "unassessable" for row in boards)
    groups = record.get("groups")
    expected_keys = {f"{h}:{kind}:{qual}"
                     for h in HORIZONS for kind in baseline.KINDS
                     for qual in ("qualified", "not_qualified")}
    if (not isinstance(groups, dict) or set(groups) != expected_keys
            or type(record.get("evaluable_board_count")) is not int
            or record["evaluable_board_count"] != eligible
            or type(record.get("training_label_rows")) is not int
            or record["training_label_rows"] < 0):
        raise ValueError("forecast_group_contract_invalid")
    for group in groups.values():
        if not isinstance(group, dict) or set(group) != {
                "status", "support_origin_days", "support_board_rows", "probabilities"}:
            raise ValueError("forecast_group_contract_invalid")
        days, rows = group["support_origin_days"], group["support_board_rows"]
        keys = {"all", "qualification", "state_qualification"}
        if (not isinstance(days, dict) or not isinstance(rows, dict)
                or set(days) != keys or set(rows) != keys
                or any(type(days[key]) is not int or type(rows[key]) is not int
                       or not 0 <= days[key] <= rows[key] for key in keys)
                or not days["state_qualification"] <= days["qualification"] <= days["all"]
                or not rows["state_qualification"] <= rows["qualification"] <= rows["all"]):
            raise ValueError("forecast_group_support_invalid")
        supported = (days["all"] >= baseline.MIN_TRAIN_DAYS
                     and days["qualification"] >= baseline.MIN_CELL_DAYS
                     and days["state_qualification"] >= baseline.MIN_CELL_DAYS)
        probabilities = group["probabilities"]
        if supported:
            if (group["status"] != "research_probability"
                    or not isinstance(probabilities, dict)
                    or set(probabilities) != {"unconditional", "qualification",
                                              "market_state_qualification"}
                    or any(type(value) not in (int, float) or not 0 < value < 1
                           for value in probabilities.values())):
                raise ValueError("forecast_group_probability_invalid")
        elif group["status"] != "abstain_insufficient_mature_origin_days" or probabilities is not None:
            raise ValueError("forecast_group_abstention_invalid")
    supported_boards = sum(row["screen_status"] != "unassessable"
                           and any(groups[f"{h}:{row['kind']}:{row['screen_status']}"]
                                   ["probabilities"] is not None for h in HORIZONS)
                           for row in boards)
    if (type(record.get("supported_board_count_any_horizon")) is not int
            or record["supported_board_count_any_horizon"] != supported_boards
            or record["status"] != ("research_probability_issued" if supported_boards
                                    else "abstained_insufficient_history")):
        raise ValueError("forecast_supported_population_invalid")
    return record


def capture(*, root=ROOT, now: datetime | None = None) -> dict[str, Any]:
    """Cloud timer only; issue once after the complete origin is first written."""
    if os.environ.get("REGIS_EXECUTION_ROLE") != "cloud":
        return {"status": "local_unchanged", **BOUNDARY}
    root = Path(root)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("forecast_now_timezone_required")
    current = current.astimezone(baseline.SHANGHAI)
    origin_info = origin_status(root=root, now=current)
    if origin_info["status"] != "captured":
        return {"status": "origin_" + origin_info["status"], **BOUNDARY}
    day = origin_info["origin_day"]
    path = _path(root, day)
    if path.parent.is_symlink():
        raise ValueError("forecast_directory_unsafe")
    if path.exists() or path.is_symlink():
        try:
            old = read_day(root, day, now=current)
            return {"status": "unchanged", "origin_day": day,
                    "record_sha256": old["record_sha256"], **BOUNDARY}
        except (OSError, ValueError, TypeError, KeyError):
            return {"status": "record_invalid_preserved", "origin_day": day, **BOUNDARY}
    origin = read_origin(root, day, now=current)
    sessions, _ = _calendar(root, current)
    index = _window(sessions, current)
    if index is None or sessions[index] != day:
        return {"status": "outside_after_close_window", **BOUNDARY}
    start = max(0, index - MAX_TRAIN_ORIGINS + 1)
    batches = []
    for offset in range(start, index + 1, 60):
        batch = build_ledger(root=root, start_day=sessions[offset],
                             end_day=sessions[min(offset + 59, index)],
                             evaluation_as_of=current.isoformat())
        if batch["status"] not in {"research_labels_available",
                                   "insufficient_verified_history"}:
            return {"status": "ledger_unavailable", "origin_day": day, **BOUNDARY}
        batches.append(batch)
    market_rows = []
    for batch in batches:
        for slot in batch["origin_days"]:
            if slot["day"] >= day or slot["status"] != "verified":
                continue
            try:
                market_rows.append(read_market_pit(root, slot["day"], now=current)["market_row"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
    prepared = prepare(origin, batches, market_rows, issued_at=current.isoformat())
    raw = json.dumps(prepared, sort_keys=True, ensure_ascii=False,
                     allow_nan=False).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".forecast-",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return {"status": "write_race_preserved", "origin_day": day, **BOUNDARY}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    read_day(root, day, now=current)
    return {"status": "captured", "origin_day": day,
            "record_sha256": prepared["record_sha256"],
            "model_status": prepared["status"], **BOUNDARY}


def current_status(*, root=ROOT, now: datetime | None = None) -> dict[str, Any]:
    """Small read-only status for the original website; never compute here."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("forecast_status_timezone_required")
    base = {"schema": STATUS_SCHEMA, "status": "waiting_calendar",
            "origin_day": None, "issued_at": None, "board_count": 0,
            "supported_board_count_any_horizon": 0, "groups": {}, **BOUNDARY}
    origin_info = origin_status(root=root, now=current)
    if origin_info["status"] != "captured":
        return {**base, "status": "origin_" + origin_info["status"]}
    day = origin_info["origin_day"]
    base["origin_day"] = day
    path = _path(Path(root), day)
    if not path.exists() and not path.is_symlink():
        return {**base, "status": "forecast_not_issued"}
    try:
        record = read_day(root, day, now=current)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {**base, "status": "forecast_invalid_or_unreadable"}
    return {**base, "status": record["status"], "issued_at": record["issued_at"],
            "board_count": record["board_count"],
            "supported_board_count_any_horizon": record["supported_board_count_any_horizon"],
            "groups": {key: {"status": value["status"],
                             "support_origin_days": value["support_origin_days"]}
                       for key, value in record["groups"].items()},
            "record_sha256": record["record_sha256"],
            "target_meaning": record["target_meaning"]}


__all__ = ["prepare", "capture", "read_day", "current_status", "SCHEMA"]
