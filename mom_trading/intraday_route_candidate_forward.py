"""Immutable 30-minute input packets for routed research candidates."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .mainline_start_candidates import verify_candidate_packet
from .market_sector_route_forward import capture_slot
from .market_state_daily import PROJECT_ROOT, _atomic_publish, _canonical_bytes, _sha256


SH_TZ = ZoneInfo("Asia/Shanghai")
PROTOCOL_PATH = (
    PROJECT_ROOT
    / "research/INTRADAY_ROUTE_CANDIDATE_30M_FORWARD_PROTOCOL_V3_AMENDMENT_2.json"
)
PROTOCOL_SHA256 = "0ee61c95d044d851620f6cf46ecf6c0a30fec4386b0a188681867ddbee3e1fae"
PACKET_DIR = PROJECT_ROOT / "research/data/market_sector_route_forward/v1/intraday_30m_packets"


def _project_path(raw: object, *, project_root: Path) -> Path:
    path = Path(str(raw or ""))
    resolved = (path if path.is_absolute() else project_root / path).resolve()
    resolved.relative_to(project_root.resolve())
    return resolved


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _bar_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SH_TZ)
    return parsed.astimezone(SH_TZ)


def verify_intraday_protocol(
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
        authority = protocol.get("authority") or {}
        if authority.get("research_only") is not True or any(
            authority.get(key) is not False for key in (
                "changes_unified_score", "changes_live_trade_count",
                "changes_live_position_or_quantity", "generates_buy_or_sell_alert",
                "selects_production_candidate", "production_strategy",
                "automatic_trading", "stable_profit_claim",
            )
        ):
            errors.append("authority_invalid")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        protocol_sha, protocol = None, {}
        errors.append(f"protocol_unreadable:{exc}")
    return {
        "valid": not errors, "errors": errors, "protocol": protocol,
        "protocol_sha256": protocol_sha,
    }


def _candidate_bars(
    row: dict[str, Any], get_candles: Callable[..., list[Any]], *,
    observed: datetime, limit: int,
) -> dict[str, Any]:
    code = str(row.get("stock_code") or "")
    errors: list[str] = []
    bars: list[dict[str, Any]] = []
    try:
        raw_bars = get_candles(code, limit, interval="30m")
    except Exception as exc:
        raw_bars = []
        errors.append(f"source_unavailable:{type(exc).__name__}")
    later_bar_count = 0
    for raw in raw_bars:
        end = _bar_time(getattr(raw, "time", None))
        if end is None:
            errors.append("bar_time_invalid")
            continue
        if end > observed:
            later_bar_count += 1
            continue
        values = {
            name: _finite(getattr(raw, name, None))
            for name in ("open", "high", "low", "close", "volume")
        }
        if any(value is None for value in values.values()):
            errors.append("bar_required_field_missing")
            continue
        amount_proxy = _finite(getattr(raw, "amount", None))
        bars.append({
            "time": end.isoformat(timespec="seconds"), **values,
            "amount_proxy": amount_proxy,
            "amount_proxy_semantics": "close_times_volume_from_existing_provider",
        })
    bars.sort(key=lambda item: item["time"])
    return {
        "stock_code": code, "stock_name": row.get("stock_name"),
        "candidate_score": row.get("score"),
        "source_bar_count": len(raw_bars), "retained_bar_count": len(bars),
        "later_or_forming_bar_count_dropped": later_bar_count,
        "complete": bool(bars and not errors),
        "errors": list(dict.fromkeys(errors)), "bars": bars,
        "bars_sha256": hashlib.sha256(_canonical_bytes(bars)).hexdigest(),
    }


def verify_intraday_packet(path: Path, *, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    errors: list[str] = []
    try:
        resolved = path.resolve()
        resolved.relative_to(project_root.resolve())
        packet = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"packet_unreadable:{exc}"]}
    identity = dict(packet)
    packet_id = str(identity.pop("packet_id", ""))
    if packet.get("schema") != "intraday_route_candidate_30m_forward.v1":
        errors.append("schema_invalid")
    if hashlib.sha256(_canonical_bytes(identity)).hexdigest() != packet_id:
        errors.append("packet_id_invalid")
    if packet.get("no_backfill") is not True or packet.get("post_decision_returns_or_labels_read") is not False:
        errors.append("causality_boundary_invalid")
    captured_at = _bar_time(packet.get("captured_at"))
    for capture in packet.get("candidate_captures") or []:
        bars = capture.get("bars")
        if not isinstance(bars, list) or hashlib.sha256(_canonical_bytes(bars)).hexdigest() != capture.get("bars_sha256"):
            errors.append("bar_rows_identity_invalid")
            continue
        if captured_at is None or any(
            (_bar_time(row.get("time")) or datetime.max.replace(tzinfo=SH_TZ)) > captured_at
            for row in bars if isinstance(row, dict)
        ):
            errors.append("post_capture_bar_present")
    authority = packet.get("authority") or {}
    if authority.get("research_only") is not True or any(
        authority.get(key) is not False for key in (
            "changes_unified_score", "changes_live_trade_count",
            "changes_live_position_or_quantity", "generates_buy_or_sell_alert",
            "selects_production_candidate", "production_strategy",
            "automatic_trading", "stable_profit_claim",
        )
    ):
        errors.append("authority_invalid")
    return {"valid": not errors, "errors": errors, "packet": packet, "packet_id": packet_id}


def capture_intraday_route_candidates(
    candidate_capture: dict[str, Any], get_candles: Callable[..., list[Any]], *,
    now: datetime | None = None, project_root: Path = PROJECT_ROOT,
    protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str | None = PROTOCOL_SHA256,
    packet_dir: Path = PACKET_DIR,
    candidate_verifier: Callable[..., dict[str, Any]] = verify_candidate_packet,
) -> dict[str, Any]:
    observed = (now or datetime.now(SH_TZ)).astimezone(SH_TZ)
    protocol_check = verify_intraday_protocol(
        project_root=project_root, protocol_path=protocol_path,
        expected_protocol_sha256=expected_protocol_sha256,
    )
    if protocol_check.get("valid") is not True:
        return {"status": "identity_invalid", "captured": False, "errors": protocol_check["errors"]}
    try:
        candidate_path = _project_path(candidate_capture.get("path"), project_root=project_root)
        candidate_check = candidate_verifier(candidate_path, project_root=project_root)
    except (OSError, ValueError):
        return {"status": "candidate_input_invalid", "captured": False}
    if candidate_check.get("valid") is not True:
        return {"status": "candidate_input_invalid", "captured": False, "errors": candidate_check.get("errors") or []}
    candidate = candidate_check["packet"]
    if observed.date().isoformat() != candidate.get("decision_day") or capture_slot(observed) != candidate.get("slot_id"):
        return {"status": "outside_original_capture_slot", "captured": False}
    settings = protocol_check["protocol"]["capture"]
    selected = ((candidate.get("research_result") or {}).get("selected") or [])[
        :int(settings["maximum_candidates"])
    ]
    captures = [
        _candidate_bars(
            row, get_candles, observed=observed,
            limit=int(settings["maximum_source_bars_per_candidate"]),
        )
        for row in selected if isinstance(row, dict)
    ]
    packet = {
        "schema": "intraday_route_candidate_30m_forward.v1",
        "decision_day": candidate.get("decision_day"),
        "slot_id": candidate.get("slot_id"),
        "captured_at": observed.isoformat(timespec="seconds"),
        "no_backfill": True,
        "protocol": {"path": str(protocol_path.relative_to(project_root)), "sha256": protocol_check["protocol_sha256"]},
        "candidate_packet": {"path": str(candidate_path.relative_to(project_root)), "sha256": _sha256(candidate_path), "packet_id": candidate.get("packet_id")},
        "candidate_captures": captures,
        "capture_status": "complete" if all(item["complete"] for item in captures) else "incomplete",
        "valid_empty_candidate_observation": not selected,
        "post_decision_returns_or_labels_read": False,
        "intraday_formula_defined": False,
        "authority": {
            "research_only": True, "changes_unified_score": False,
            "changes_live_trade_count": False,
            "changes_live_position_or_quantity": False,
            "generates_buy_or_sell_alert": False,
            "selects_production_candidate": False, "production_strategy": False,
            "automatic_trading": False, "stable_profit_claim": False,
        },
    }
    packet["packet_id"] = hashlib.sha256(_canonical_bytes(packet)).hexdigest()
    path = packet_dir / f"{packet['decision_day']}-{packet['slot_id']}.json"
    if path.exists():
        verified = verify_intraday_packet(path, project_root=project_root)
        return {
            "status": "already_captured" if verified.get("valid") else "existing_packet_invalid",
            "captured": verified.get("valid") is True, "path": str(path),
            "packet_id": verified.get("packet_id"), "errors": verified.get("errors") or [],
        }
    _atomic_publish(path, _canonical_bytes(packet))
    verified = verify_intraday_packet(path, project_root=project_root)
    if verified.get("valid") is not True:
        raise RuntimeError("intraday packet failed post-write verification")
    return {
        "status": "captured", "captured": True, "path": str(path),
        "packet_id": packet["packet_id"], "capture_status": packet["capture_status"],
        "candidate_count": len(captures), "changes_trade_count_or_position": False,
    }


__all__ = [
    "capture_intraday_route_candidates", "verify_intraday_packet",
    "verify_intraday_protocol",
]
