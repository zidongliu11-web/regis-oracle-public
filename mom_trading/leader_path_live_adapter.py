"""Adapt verified live inputs to the frozen leader-path PIT contract."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from trade_assistant.candles import Candle, completed_signal_candles

from .leader_path_forward import capture_leader_path_forward
from .market_sector_constituent_forward import verify_constituent_packet
from .market_state_daily import PROJECT_ROOT


SH_TZ = ZoneInfo("Asia/Shanghai")
_QUEUE_UNKNOWN = {"status": "unavailable", "fill_inferred": False}
_REQUIRED = {
    "price_limit_identity": (
        "stock_code", "source_as_of", "previous_close", "effective_limit_up_price",
        "effective_limit_down_price", "is_st", "board", "no_price_limit",
    ),
    "limit_path": (
        "stock_code", "source_as_of", "first_touch_at", "open_events_at",
        "reseal_events_at", "last_seal_at", "decision_status",
    ),
    "execution_identity": (
        "stock_code", "source_as_of", "tradable", "suspended", "T_plus_1",
        "lot_size", "next_executable_time_rule", "fill_inferred",
    ),
}


def _time(raw: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SH_TZ)
    return parsed.astimezone(SH_TZ)


def _number(raw: object) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _incomplete(gaps: list[str], *, pit_capture_called: bool = False) -> dict[str, Any]:
    return {
        "status": "incomplete", "captured": False,
        "ready_for_pit_capture": False,
        "gaps": list(dict.fromkeys(gaps)),
        "queue_evidence": dict(_QUEUE_UNKNOWN),
        "no_backfill": True, "returns_or_labels_read": False,
        "pit_capture_called": pit_capture_called,
    }


def _constituent_snapshot(
    capture: object, *, project_root: Path,
    verifier: Callable[..., dict[str, Any]], decision_at: datetime,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(capture, dict) or capture.get("captured") is not True:
        return None, ["verified_constituent_capture_missing"]
    if capture.get("status") not in {"captured", "already_captured"}:
        return None, ["verified_constituent_capture_missing"]
    try:
        raw_path = Path(str(capture.get("path") or ""))
        path = (raw_path if raw_path.is_absolute() else project_root / raw_path).resolve()
        path.relative_to(project_root.resolve())
        if not path.is_file():
            raise OSError("packet missing")
        checked = verifier(path, project_root=project_root)
    except (OSError, ValueError, TypeError):
        return None, ["constituent_packet_unreadable_or_outside_project"]
    if checked.get("valid") is not True or not isinstance(checked.get("packet"), dict):
        return None, ["constituent_packet_invalid", *(checked.get("errors") or [])]
    packet = checked["packet"]
    gaps: list[str] = []
    if packet.get("decision_day") != decision_at.date().isoformat():
        gaps.append("constituent_decision_day_mismatch")
    captured_at = _time(packet.get("captured_at"))
    if captured_at is None or captured_at > decision_at:
        gaps.append("constituent_capture_time_unavailable_at_decision")
    if packet.get("capture_status") != "complete":
        gaps.append("constituent_capture_incomplete")
    if packet.get("stock_selected_or_ranked") is not False:
        gaps.append("constituent_scope_already_selected_or_ranked")
    if not packet.get("sector_captures") or any(
        not isinstance(item, dict) or item.get("complete") is not True
        for item in packet.get("sector_captures") or []
    ):
        gaps.append("complete_same_slot_sector_constituents_missing")
    return packet, gaps


def _minute_path(
    minute_stream: object, leader: object, decision_at: datetime,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(minute_stream, dict):
        return None, ["minute_forward_stream_missing"]
    gaps: list[str] = []
    if minute_stream.get("collection_mode") != "forward_stream_from_prefrozen_leader":
        gaps.append("minute_forward_stream_provenance_missing")
    if minute_stream.get("no_backfill") is not True:
        gaps.append("minute_backfill_or_history_fetch_forbidden")
    source_as_of = _time(minute_stream.get("source_as_of"))
    stream_started = _time(minute_stream.get("stream_started_at"))
    if source_as_of is None or source_as_of.date() != decision_at.date() or source_as_of > decision_at:
        gaps.append("minute_source_as_of_invalid")
    bars = minute_stream.get("bars")
    if not isinstance(bars, list) or not bars or any(not isinstance(bar, Candle) for bar in bars):
        return None, gaps + ["minute_candles_missing_or_not_provider_candles"]
    completed = completed_signal_candles(bars, "1m", observed_at=decision_at)
    if len(completed) != len(bars):
        gaps.append("minute_stream_contains_forming_or_future_bars")
    normalized: list[dict[str, Any]] = []
    first_start: datetime | None = None
    for candle in bars:
        ended = _time(candle.time)
        if ended is None or ended.date() != decision_at.date():
            gaps.append("minute_bar_not_same_decision_day")
            continue
        started = ended - timedelta(minutes=1)
        first_start = first_start or started
        values = {
            key: _number(getattr(candle, key, None))
            for key in ("open", "high", "low", "close", "volume", "amount")
        }
        if any(value is None for value in values.values()):
            gaps.append("minute_bar_required_value_missing_no_proxy")
            continue
        normalized.append({
            "started_at": started.isoformat(timespec="seconds"),
            "ended_at": ended.isoformat(timespec="seconds"),
            "completed": True, **values,
        })
    frozen_at = _time(leader.get("frozen_at")) if isinstance(leader, dict) else None
    if (
        stream_started is None or first_start is None or frozen_at is None
        or frozen_at > stream_started or stream_started > first_start
    ):
        gaps.append("prefrozen_leader_stream_start_provenance_invalid")
    if gaps:
        return None, list(dict.fromkeys(gaps))
    return {
        "stock_code": leader.get("stock_code"), "interval": "1m",
        "collection_mode": "forward_stream_from_prefrozen_leader",
        "no_backfill": True,
        "source_as_of": source_as_of.isoformat(timespec="seconds"),
        "bars": normalized,
    }, []


def _required_input_gaps(name: str, value: object) -> list[str]:
    if not isinstance(value, dict):
        return [f"{name}_missing"]
    return [f"{name}_field_missing:{key}" for key in _REQUIRED[name] if key not in value]


def adapt_and_capture_leader_path(
    *, decision_at: datetime, constituent_capture: dict[str, Any] | None,
    leader_identity: dict[str, Any] | None, minute_stream: dict[str, Any] | None,
    price_limit_identity: dict[str, Any] | None, limit_path: dict[str, Any] | None,
    execution_identity: dict[str, Any] | None,
    project_root: Path = PROJECT_ROOT,
    constituent_verifier: Callable[..., dict[str, Any]] = verify_constituent_packet,
    pit_capture: Callable[..., dict[str, Any]] = capture_leader_path_forward,
    pit_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Publish only when every causal input exists; otherwise enumerate gaps."""
    if not isinstance(decision_at, datetime) or decision_at.tzinfo is None:
        return _incomplete(["decision_at_timezone_missing"])
    observed = decision_at.astimezone(SH_TZ)
    snapshot, gaps = _constituent_snapshot(
        constituent_capture, project_root=project_root,
        verifier=constituent_verifier, decision_at=observed,
    )
    if not isinstance(leader_identity, dict):
        gaps.append("prefrozen_leader_identity_missing")
    minute_path, minute_gaps = _minute_path(minute_stream, leader_identity, observed)
    gaps.extend(minute_gaps)
    for name, value in (
        ("price_limit_identity", price_limit_identity),
        ("limit_path", limit_path),
        ("execution_identity", execution_identity),
    ):
        gaps.extend(_required_input_gaps(name, value))
    if isinstance(execution_identity, dict) and execution_identity.get("fill_inferred") is not False:
        gaps.append("fill_inference_forbidden_without_queue_evidence")
    if gaps or snapshot is None or minute_path is None or leader_identity is None:
        return _incomplete(gaps)

    options = dict(pit_options or {})
    options.setdefault("project_root", project_root)
    result = pit_capture(
        decision_at=observed, constituent_snapshot=snapshot,
        leader_identity=leader_identity, minute_path=minute_path,
        price_limit_identity=price_limit_identity, limit_path=limit_path,
        execution_identity=execution_identity, **options,
    )
    if result.get("status") in {"input_invalid", "identity_invalid", "existing_packet_invalid"}:
        incomplete = _incomplete(result.get("errors") or [str(result.get("status"))], pit_capture_called=True)
        incomplete["downstream_status"] = result.get("status")
        return incomplete
    return {
        **result, "adapter_status": "ready", "ready_for_pit_capture": True,
        "gaps": [], "queue_evidence": dict(_QUEUE_UNKNOWN),
        "no_backfill": True, "returns_or_labels_read": False,
        "pit_capture_called": True,
    }


__all__ = ["adapt_and_capture_leader_path"]
