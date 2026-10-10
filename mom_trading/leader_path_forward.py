"""Immutable PIT inputs for leader-path research; no formula or network access."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .market_state_daily import PROJECT_ROOT, _atomic_publish, _canonical_bytes, _sha256


SH_TZ = ZoneInfo("Asia/Shanghai")
PROTOCOL_PATH = PROJECT_ROOT / "research/LEADER_PATH_FORWARD_INPUT_PROTOCOL_V1.json"
PROTOCOL_SHA256 = "d04f6f8d457da1e0410df474deae04163285a6ac26a5f280fc8f93b3b6891cb3"
PACKET_DIR = PROJECT_ROOT / "research/data/leader_path_forward/v1/input_packets"

_CONSTITUENT_FIELDS = (
    "stock_code", "stock_name", "price", "change_pct", "volume", "amount",
    "turnover_rate", "total_market_cap", "float_market_cap", "data_as_of",
)
_AUTHORITY_FALSE = (
    "changes_unified_score", "changes_trade_count", "changes_position_or_quantity",
    "generates_buy_or_sell_alert", "selects_production_candidate",
    "production_strategy", "automatic_trading", "stable_profit_claim",
)


def _project_path(raw: object, *, project_root: Path) -> Path:
    path = Path(str(raw or ""))
    resolved = (path if path.is_absolute() else project_root / path).resolve()
    resolved.relative_to(project_root.resolve())
    return resolved


def _time(raw: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(SH_TZ)


def _number(raw: object) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _source_time_errors(raw: object, decision_at: datetime, label: str) -> list[str]:
    observed = _time(raw)
    if observed is None:
        return [f"{label}_invalid"]
    errors = []
    if observed.date() != decision_at.date():
        errors.append(f"{label}_not_decision_day")
    if observed > decision_at:
        errors.append(f"{label}_future")
    return errors


def verify_leader_path_protocol(
    *, project_root: Path = PROJECT_ROOT, protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str | None = PROTOCOL_SHA256,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        protocol_sha = _sha256(protocol_path)
        if expected_protocol_sha256 and protocol_sha != expected_protocol_sha256:
            errors.append("protocol_sha256_mismatch")
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        if protocol.get("status") != "frozen_before_first_forward_packet_no_outcome_access":
            errors.append("protocol_status_invalid")
        for name, binding in (protocol.get("bindings") or {}).items():
            path = _project_path(binding.get("path"), project_root=project_root)
            if not path.is_file() or _sha256(path) != binding.get("sha256"):
                errors.append(f"binding_invalid:{name}")
        boundary = protocol.get("research_boundary") or {}
        if boundary.get("returns_or_labels_read") is not False or any(
            boundary.get(key) is not False for key in (
                "leader_formula_defined", "divergence_threshold_defined",
                "re_strengthening_threshold_defined", "entry_or_exit_rule_defined",
            )
        ):
            errors.append("research_boundary_invalid")
        authority = protocol.get("authority") or {}
        if authority.get("research_only") is not True or any(
            authority.get(key) is not False for key in _AUTHORITY_FALSE
        ):
            errors.append("authority_invalid")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        protocol, protocol_sha = {}, None
        errors.append(f"protocol_unreadable:{exc}")
    return {
        "valid": not errors, "errors": errors, "protocol": protocol,
        "protocol_sha256": protocol_sha,
    }


def _constituent_errors(
    snapshot: object, decision_at: datetime,
) -> tuple[list[str], dict[str, set[str]]]:
    if not isinstance(snapshot, dict):
        return ["constituent_snapshot_invalid"], {}
    errors: list[str] = []
    if snapshot.get("decision_day") != decision_at.date().isoformat():
        errors.append("constituent_decision_day_mismatch")
    errors.extend(_source_time_errors(snapshot.get("captured_at"), decision_at, "constituent_captured_at"))
    if snapshot.get("capture_status") != "complete":
        errors.append("constituent_capture_incomplete")
    if snapshot.get("stock_selected_or_ranked") is not False:
        errors.append("constituent_snapshot_must_precede_stock_selection")
    captures = snapshot.get("sector_captures")
    if not isinstance(captures, list) or not captures:
        return errors + ["constituent_sector_captures_invalid"], {}
    if snapshot.get("confirmed_sector_count") != len(captures):
        errors.append("confirmed_sector_count_mismatch")
    membership: dict[str, set[str]] = {}
    for capture in captures:
        if not isinstance(capture, dict) or capture.get("complete") is not True:
            errors.append("constituent_sector_capture_incomplete")
            continue
        sector = capture.get("sector") if isinstance(capture.get("sector"), dict) else {}
        sector_code = str(sector.get("code") or "")
        rows = capture.get("rows")
        if not sector_code or not isinstance(rows, list):
            errors.append("constituent_sector_rows_invalid")
            continue
        if capture.get("reported_total") != len(rows) or capture.get("fetched_count") != len(rows):
            errors.append("constituent_sector_count_mismatch")
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or any(row.get(key) is None for key in _CONSTITUENT_FIELDS):
                errors.append("constituent_required_field_missing")
                continue
            code = str(row.get("stock_code") or "")
            if not re.fullmatch(r"\d{6}", code) or code in seen:
                errors.append("constituent_stock_identity_invalid")
                continue
            seen.add(code)
            if str(row.get("sector_code") or "") != sector_code:
                errors.append("constituent_sector_identity_mismatch")
            errors.extend(_source_time_errors(row.get("data_as_of"), decision_at, "constituent_data_as_of"))
            for key in _CONSTITUENT_FIELDS[2:-1]:
                if _number(row.get(key)) is None:
                    errors.append("constituent_numeric_field_invalid")
                    break
            membership.setdefault(code, set()).add(sector_code)
    return list(dict.fromkeys(errors)), membership


def _minute_path_errors(
    minute_path: object, decision_at: datetime, stock_code: str,
) -> tuple[list[str], datetime | None]:
    if not isinstance(minute_path, dict):
        return ["minute_path_invalid"], None
    errors: list[str] = []
    if minute_path.get("stock_code") != stock_code or minute_path.get("interval") != "1m":
        errors.append("minute_path_identity_invalid")
    if minute_path.get("collection_mode") != "forward_stream_from_prefrozen_leader":
        errors.append("selected_then_historical_path_fetch_forbidden")
    if minute_path.get("no_backfill") is not True:
        errors.append("minute_path_backfill_forbidden")
    errors.extend(_source_time_errors(minute_path.get("source_as_of"), decision_at, "minute_source_as_of"))
    bars = minute_path.get("bars")
    if not isinstance(bars, list) or not bars:
        return errors + ["minute_bars_missing"], None
    previous_end: datetime | None = None
    first_start: datetime | None = None
    for bar in bars:
        if not isinstance(bar, dict):
            errors.append("minute_bar_invalid")
            continue
        if bar.get("completed") is not True:
            errors.append("forming_minute_bar")
        started, ended = _time(bar.get("started_at")), _time(bar.get("ended_at"))
        if started is None or ended is None or ended - started != timedelta(minutes=1):
            errors.append("minute_bar_time_invalid")
            continue
        first_start = first_start or started
        if started.date() != decision_at.date() or ended.date() != decision_at.date():
            errors.append("minute_bar_not_decision_day")
        if ended > decision_at:
            errors.append("future_minute_bar")
        if previous_end is not None and started < previous_end:
            errors.append("minute_bars_not_strictly_ordered")
        previous_end = ended
        values = {key: _number(bar.get(key)) for key in ("open", "high", "low", "close", "volume", "amount")}
        if any(value is None for value in values.values()):
            errors.append("minute_bar_numeric_field_invalid")
        elif (
            min(values["open"], values["close"]) < values["low"]
            or max(values["open"], values["close"]) > values["high"]
            or values["low"] <= 0 or values["volume"] < 0 or values["amount"] < 0
        ):
            errors.append("minute_bar_ohlcv_invalid")
    return list(dict.fromkeys(errors)), first_start


def _leader_errors(
    leader: object, membership: dict[str, set[str]], first_path_start: datetime | None,
) -> tuple[list[str], str]:
    if not isinstance(leader, dict):
        return ["leader_identity_invalid"], ""
    errors: list[str] = []
    code = str(leader.get("stock_code") or "")
    if code not in membership:
        errors.append("leader_not_in_complete_constituent_snapshot")
    sectors = leader.get("sector_codes")
    if not isinstance(sectors, list) or not sectors or not set(map(str, sectors)).issubset(membership.get(code, set())):
        errors.append("leader_sector_identity_invalid")
    tie_break = leader.get("tie_break_order")
    if (
        not isinstance(tie_break, list) or not tie_break
        or any(not isinstance(item, str) or not item for item in tie_break)
        or len(tie_break) != len(set(tie_break))
    ):
        errors.append("leader_tie_break_invalid")
    if not leader.get("identity_rule_id") or not leader.get("identity_rule_version"):
        errors.append("leader_rule_identity_missing")
    if leader.get("selection_mode") != "prefrozen_before_intraday_path" or leader.get("post_path_selected") is not False:
        errors.append("post_path_selection_not_allowed")
    source_as_of, frozen_at = _time(leader.get("selection_input_as_of")), _time(leader.get("frozen_at"))
    if source_as_of is None or frozen_at is None or source_as_of > frozen_at:
        errors.append("leader_identity_time_invalid")
    if first_path_start is None or frozen_at is None or frozen_at > first_path_start:
        errors.append("leader_identity_not_frozen_before_path")
    return list(dict.fromkeys(errors)), code


def _identity_errors(
    price_limit: object, limit_path: object, execution: object,
    decision_at: datetime, stock_code: str,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(price_limit, dict):
        return ["price_limit_identity_invalid"]
    required_limit = (
        "stock_code", "source_as_of", "previous_close", "effective_limit_up_price",
        "effective_limit_down_price", "is_st", "board", "no_price_limit",
    )
    if any(key not in price_limit for key in required_limit) or price_limit.get("stock_code") != stock_code:
        errors.append("price_limit_identity_invalid")
    errors.extend(_source_time_errors(price_limit.get("source_as_of"), decision_at, "price_limit_source_as_of"))
    previous = _number(price_limit.get("previous_close"))
    no_limit = price_limit.get("no_price_limit")
    if previous is None or previous <= 0 or not isinstance(price_limit.get("is_st"), bool) or not isinstance(no_limit, bool):
        errors.append("price_limit_fields_invalid")
    if price_limit.get("board") not in {"main", "gemb", "star", "bse"}:
        errors.append("price_limit_board_invalid")
    lower, upper = _number(price_limit.get("effective_limit_down_price")), _number(price_limit.get("effective_limit_up_price"))
    if no_limit is False and (lower is None or upper is None or previous is None or not (0 < lower < previous < upper)):
        errors.append("effective_price_limits_invalid")
    if no_limit is True and (price_limit.get("effective_limit_down_price") is not None or price_limit.get("effective_limit_up_price") is not None):
        errors.append("no_limit_day_must_not_infer_price_bounds")

    if not isinstance(limit_path, dict):
        return list(dict.fromkeys(errors + ["limit_path_invalid"]))
    required_path = (
        "stock_code", "source_as_of", "first_touch_at", "open_events_at",
        "reseal_events_at", "last_seal_at", "decision_status",
    )
    if any(key not in limit_path for key in required_path) or limit_path.get("stock_code") != stock_code:
        errors.append("limit_path_identity_invalid")
    errors.extend(_source_time_errors(limit_path.get("source_as_of"), decision_at, "limit_path_source_as_of"))
    status = limit_path.get("decision_status")
    if status not in {"not_touched", "sealed", "opened", "failed_limit", "no_price_limit", "unavailable"}:
        errors.append("limit_path_status_invalid")
    raw_events = [limit_path.get("first_touch_at"), limit_path.get("last_seal_at")]
    for key in ("open_events_at", "reseal_events_at"):
        values = limit_path.get(key)
        if not isinstance(values, list):
            errors.append("limit_path_events_invalid")
        else:
            raw_events.extend(values)
    events = [_time(value) for value in raw_events if value is not None]
    if any(value is None for value in events):
        errors.append("limit_path_event_time_invalid")
    for event in (value for value in events if value is not None):
        if event.date() != decision_at.date() or event > decision_at:
            errors.append("limit_path_event_not_available_at_decision")
    first_touch = _time(limit_path.get("first_touch_at")) if limit_path.get("first_touch_at") else None
    last_seal = _time(limit_path.get("last_seal_at")) if limit_path.get("last_seal_at") else None
    if status in {"sealed", "opened", "failed_limit"} and first_touch is None:
        errors.append("limit_path_touch_missing")
    if status == "sealed" and last_seal is None:
        errors.append("limit_path_last_seal_missing")
    if status == "not_touched" and any(value is not None for value in raw_events):
        errors.append("not_touched_path_contains_events")
    if no_limit is True and status not in {"no_price_limit", "unavailable"}:
        errors.append("no_limit_day_path_status_invalid")

    if not isinstance(execution, dict):
        return list(dict.fromkeys(errors + ["execution_identity_invalid"]))
    required_execution = (
        "stock_code", "source_as_of", "tradable", "suspended", "T_plus_1",
        "lot_size", "next_executable_time_rule", "fill_inferred",
    )
    if any(key not in execution for key in required_execution) or execution.get("stock_code") != stock_code:
        errors.append("execution_identity_invalid")
    errors.extend(_source_time_errors(execution.get("source_as_of"), decision_at, "execution_source_as_of"))
    filled_quantity = execution.get("filled_quantity")
    if execution.get("fill_inferred") is not False or (filled_quantity is not None and filled_quantity != 0):
        errors.append("execution_fill_inference_forbidden")
    if execution.get("T_plus_1") is not True or execution.get("lot_size") != 100:
        errors.append("A_share_execution_identity_invalid")
    if not isinstance(execution.get("tradable"), bool) or not isinstance(execution.get("suspended"), bool):
        errors.append("execution_tradability_invalid")
    if execution.get("tradable") is True and execution.get("suspended") is True:
        errors.append("execution_tradability_inconsistent")
    if not execution.get("next_executable_time_rule"):
        errors.append("next_executable_time_rule_missing")
    return list(dict.fromkeys(errors))


def _packet_errors(packet: dict[str, Any], *, project_root: Path) -> list[str]:
    errors: list[str] = []
    if packet.get("schema") != "leader_path_forward_input.v1":
        errors.append("schema_invalid")
    decision_at = _time(packet.get("decision_at"))
    if decision_at is None or packet.get("decision_day") != decision_at.date().isoformat():
        return errors + ["decision_identity_invalid"]
    if packet.get("no_backfill") is not True or packet.get("input_only") is not True:
        errors.append("capture_boundary_invalid")
    protocol = packet.get("protocol") or {}
    try:
        protocol_path = _project_path(protocol.get("path"), project_root=project_root)
        protocol_check = verify_leader_path_protocol(
            project_root=project_root, protocol_path=protocol_path,
            expected_protocol_sha256=str(protocol.get("sha256") or ""),
        )
        if protocol_check.get("valid") is not True:
            errors.append("protocol_identity_invalid")
    except (OSError, ValueError):
        errors.append("protocol_identity_invalid")
    snapshot = packet.get("constituent_snapshot")
    if hashlib.sha256(_canonical_bytes(snapshot)).hexdigest() != packet.get("constituent_snapshot_sha256"):
        errors.append("constituent_snapshot_sha256_invalid")
    constituent_errors, membership = _constituent_errors(snapshot, decision_at)
    errors.extend(constituent_errors)
    minute_errors, first_path_start = _minute_path_errors(packet.get("minute_path"), decision_at, str((packet.get("leader_identity") or {}).get("stock_code") or ""))
    errors.extend(minute_errors)
    leader_errors, stock_code = _leader_errors(packet.get("leader_identity"), membership, first_path_start)
    errors.extend(leader_errors)
    errors.extend(_identity_errors(
        packet.get("price_limit_identity"), packet.get("limit_path"),
        packet.get("execution_identity"), decision_at, stock_code,
    ))
    queue = packet.get("queue_evidence") or {}
    if queue != {
        "status": "unavailable", "Level_2_observed": False,
        "partial_fill_observed": False, "fill_inferred": False,
    }:
        errors.append("queue_evidence_boundary_invalid")
    boundary = packet.get("research_boundary") or {}
    if boundary != {
        "leader_formula_defined": False, "divergence_threshold_defined": False,
        "re_strengthening_threshold_defined": False, "returns_or_labels_read": False,
    }:
        errors.append("research_boundary_invalid")
    authority = packet.get("authority") or {}
    if authority.get("research_only") is not True or any(authority.get(key) is not False for key in _AUTHORITY_FALSE):
        errors.append("authority_invalid")
    return list(dict.fromkeys(errors))


def verify_leader_path_packet(
    path: Path, *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    try:
        resolved = path.resolve()
        resolved.relative_to(project_root.resolve())
        packet = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"packet_unreadable:{exc}"]}
    identity = dict(packet)
    packet_id = str(identity.pop("packet_id", ""))
    errors = _packet_errors(packet, project_root=project_root)
    if hashlib.sha256(_canonical_bytes(identity)).hexdigest() != packet_id:
        errors.append("packet_id_invalid")
    return {
        "valid": not errors, "errors": list(dict.fromkeys(errors)),
        "packet": packet, "packet_id": packet_id,
    }


def capture_leader_path_forward(
    *, decision_at: datetime, constituent_snapshot: dict[str, Any],
    leader_identity: dict[str, Any], minute_path: dict[str, Any],
    price_limit_identity: dict[str, Any], limit_path: dict[str, Any],
    execution_identity: dict[str, Any], project_root: Path = PROJECT_ROOT,
    protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str | None = PROTOCOL_SHA256,
    packet_dir: Path = PACKET_DIR,
) -> dict[str, Any]:
    if decision_at.tzinfo is None:
        return {"status": "input_invalid", "captured": False, "errors": ["decision_at_timezone_missing"]}
    observed = decision_at.astimezone(SH_TZ)
    protocol_check = verify_leader_path_protocol(
        project_root=project_root, protocol_path=protocol_path,
        expected_protocol_sha256=expected_protocol_sha256,
    )
    if protocol_check.get("valid") is not True:
        return {"status": "identity_invalid", "captured": False, "errors": protocol_check["errors"]}
    if observed.date().isoformat() < str(protocol_check["protocol"].get("forward_start") or ""):
        return {"status": "waiting_forward_start", "captured": False}
    try:
        protocol_relative = str(protocol_path.resolve().relative_to(project_root.resolve()))
    except ValueError:
        return {"status": "identity_invalid", "captured": False, "errors": ["protocol_outside_project"]}
    packet = {
        "schema": "leader_path_forward_input.v1",
        "decision_day": observed.date().isoformat(),
        "slot_id": constituent_snapshot.get("slot_id"),
        "decision_at": observed.isoformat(timespec="seconds"),
        "no_backfill": True,
        "input_only": True,
        "protocol": {"path": protocol_relative, "sha256": protocol_check["protocol_sha256"]},
        "constituent_snapshot": constituent_snapshot,
        "constituent_snapshot_sha256": hashlib.sha256(_canonical_bytes(constituent_snapshot)).hexdigest(),
        "leader_identity": leader_identity,
        "minute_path": minute_path,
        "price_limit_identity": price_limit_identity,
        "limit_path": limit_path,
        "execution_identity": execution_identity,
        "queue_evidence": {
            "status": "unavailable", "Level_2_observed": False,
            "partial_fill_observed": False, "fill_inferred": False,
        },
        "research_boundary": {
            "leader_formula_defined": False, "divergence_threshold_defined": False,
            "re_strengthening_threshold_defined": False, "returns_or_labels_read": False,
        },
        "authority": {
            "research_only": True, "changes_unified_score": False,
            "changes_trade_count": False, "changes_position_or_quantity": False,
            "generates_buy_or_sell_alert": False, "selects_production_candidate": False,
            "production_strategy": False, "automatic_trading": False,
            "stable_profit_claim": False,
        },
    }
    errors = _packet_errors(packet, project_root=project_root)
    if errors:
        return {"status": "input_invalid", "captured": False, "errors": errors}
    stock_code = str(leader_identity.get("stock_code") or "")
    slot_id = str(packet.get("slot_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_]+", slot_id):
        return {"status": "input_invalid", "captured": False, "errors": ["slot_id_invalid"]}
    packet["packet_id"] = hashlib.sha256(_canonical_bytes(packet)).hexdigest()
    path = packet_dir / f"{packet['decision_day']}-{slot_id}-{stock_code}.json"
    if path.exists():
        verified = verify_leader_path_packet(path, project_root=project_root)
        return {
            "status": "already_captured" if verified.get("valid") else "existing_packet_invalid",
            "captured": verified.get("valid") is True, "path": str(path),
            "packet_id": verified.get("packet_id"), "errors": verified.get("errors") or [],
        }
    _atomic_publish(path, _canonical_bytes(packet))
    verified = verify_leader_path_packet(path, project_root=project_root)
    if verified.get("valid") is not True:
        raise RuntimeError("leader path packet failed post-write verification")
    return {
        "status": "captured", "captured": True, "path": str(path),
        "packet_id": packet["packet_id"], "stock_code": stock_code,
        "changes_trade_count_or_position": False,
    }


__all__ = [
    "capture_leader_path_forward", "verify_leader_path_packet",
    "verify_leader_path_protocol",
]
