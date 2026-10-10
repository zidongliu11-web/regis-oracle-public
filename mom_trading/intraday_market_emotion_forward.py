"""Immutable same-slot market-emotion evidence for research routing."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .market_state_daily import PROJECT_ROOT, _atomic_publish, _canonical_bytes, _sha256
from .research_artifact_paths import research_artifact_path


SH_TZ = ZoneInfo("Asia/Shanghai")
PROTOCOL_PATH = (
    PROJECT_ROOT / "research/INTRADAY_MARKET_EMOTION_FORWARD_INPUT_PROTOCOL_V1.json"
)
PROTOCOL_SHA256 = "bf39b4a859e0db3d512f8978b8dbe54714b21bd9d7b796cd8d8a49e8c3f55cbc"
PACKET_DIR = (
    PROJECT_ROOT / "research/data/intraday_market_emotion_forward/v1/input_packets"
)


def _clock(value: str) -> time:
    return time.fromisoformat(value)


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: object) -> int | None:
    number = _finite(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    return int(number)


def _source_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.replace(tzinfo=SH_TZ) if parsed.tzinfo is None else parsed.astimezone(SH_TZ)


def capture_slot(
    now: datetime | None = None, *, protocol_path: Path = PROTOCOL_PATH,
) -> str | None:
    observed = (now or datetime.now(SH_TZ)).astimezone(SH_TZ)
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    current = observed.timetz().replace(tzinfo=None)
    for slot in (protocol.get("schedule") or {}).get("slots") or []:
        if _clock(slot["capture_from"]) <= current <= _clock(slot["capture_until"]):
            return str(slot["id"])
    return None


def normalize_intraday_emotion_snapshot(
    today_market: dict[str, Any] | None, *, now: datetime | None = None,
    maximum_age_seconds: int = 180, minimum_coverage_pct: float = 99.0,
) -> tuple[dict[str, Any], list[str]]:
    """Extract raw contemporaneous breadth without imputing missing values."""
    observed = (now or datetime.now(SH_TZ)).astimezone(SH_TZ)
    payload = today_market if isinstance(today_market, dict) else {}
    breadth = payload.get("breadth") if isinstance(payload.get("breadth"), dict) else {}
    price_limit = (
        breadth.get("price_limit")
        if isinstance(breadth.get("price_limit"), dict) else {}
    )
    source_at_raw = breadth.get("data_as_of") or breadth.get("as_of")
    source_at = _source_time(source_at_raw)
    expected = _integer(breadth.get("reference_universe_expected"))
    returned = _integer(breadth.get("reference_universe_returned"))
    coverage = _finite(breadth.get("reference_coverage_ratio"))
    up, down, flat = (
        _integer(breadth.get("up")), _integer(breadth.get("down")),
        _integer(breadth.get("flat")),
    )
    up_ratio = _finite(breadth.get("up_ratio"))
    limit_up, limit_down = _integer(breadth.get("limit_up")), _integer(breadth.get("limit_down"))
    limit_reference = _integer(price_limit.get("reference_count"))
    limit_evaluable = _integer(price_limit.get("evaluable_count"))
    limit_coverage = _finite(price_limit.get("coverage_ratio"))
    errors: list[str] = []
    if source_at is None:
        errors.append("source_time_missing")
    else:
        age = (observed - source_at).total_seconds()
        if source_at.date() != observed.date():
            errors.append("source_not_same_decision_day")
        if age < 0:
            errors.append("source_time_after_capture")
        elif age > maximum_age_seconds:
            errors.append("source_stale")
    if expected is None or returned is None or expected <= 0 or returned <= 0:
        errors.append("reference_coverage_counts_missing")
    if coverage is None or coverage < minimum_coverage_pct:
        errors.append("reference_coverage_below_minimum")
    if any(value is None for value in (up, down, flat, up_ratio, limit_up, limit_down)):
        errors.append("breadth_or_limit_counts_missing")
    elif up + down + flat != returned:
        errors.append("breadth_count_arithmetic_invalid")
    if (
        limit_reference is None or limit_evaluable is None
        or limit_coverage is None or limit_coverage < minimum_coverage_pct
    ):
        errors.append("effective_price_limit_coverage_below_minimum")
    health = breadth.get("health_status")
    if health not in {"verified", "delayed"}:
        errors.append("source_health_invalid")
    raw = {
        "as_of": source_at_raw,
        "fetched_at": breadth.get("fetched_at"),
        "health_status": health,
        "source": breadth.get("source"),
        "reference_universe_expected": expected,
        "reference_universe_returned": returned,
        "reference_coverage_ratio": coverage,
        "up": up, "down": down, "flat": flat, "up_ratio": up_ratio,
        "limit_up": limit_up, "limit_down": limit_down,
        "price_limit": {
            "reference_count": limit_reference,
            "evaluable_count": limit_evaluable,
            "coverage_ratio": limit_coverage,
            "rule_version": price_limit.get("rule_version"),
        },
        "total_amount_yi": _finite(breadth.get("total_amount_yi")),
        "weighted_turnover_pct": _finite(breadth.get("weighted_turnover_pct")),
    }
    return raw, list(dict.fromkeys(errors))


def verify_intraday_emotion_packet(
    path: Path, *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        resolved = research_artifact_path(path, project_root=project_root)
        packet = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"packet_unreadable:{exc}"]}
    identity = dict(packet)
    packet_id = str(identity.pop("packet_id", ""))
    if packet.get("schema") != "intraday_market_emotion_forward_input.v1":
        errors.append("schema_invalid")
    if hashlib.sha256(_canonical_bytes(identity)).hexdigest() != packet_id:
        errors.append("packet_id_invalid")
    if packet.get("no_backfill") is not True:
        errors.append("no_backfill_invalid")
    protocol = packet.get("protocol") or {}
    try:
        protocol_path = research_artifact_path(protocol.get("path"), project_root=project_root)
    except ValueError:
        return {"valid": False, "errors": ["protocol_path_invalid"]}
    if (
        not protocol_path.is_file()
        or _sha256(protocol_path) != protocol.get("sha256")
    ):
        errors.append("protocol_identity_invalid")
    raw = packet.get("raw_observation")
    if not isinstance(raw, dict) or hashlib.sha256(
        _canonical_bytes(raw)
    ).hexdigest() != packet.get("raw_observation_sha256"):
        errors.append("raw_observation_identity_invalid")
    authority = packet.get("authority") or {}
    if authority.get("research_only") is not True or any(
        authority.get(key) is not False for key in (
            "changes_live_score", "changes_live_trade_count",
            "changes_live_position_or_quantity", "generates_buy_or_sell_alert",
            "production_strategy", "automatic_trading",
        )
    ):
        errors.append("authority_invalid")
    return {"valid": not errors, "errors": errors, "packet": packet, "packet_id": packet_id}


def capture_intraday_emotion_input(
    today_market: dict[str, Any] | None, *, now: datetime | None = None,
    project_root: Path = PROJECT_ROOT, protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str | None = PROTOCOL_SHA256,
    packet_dir: Path = PACKET_DIR,
) -> dict[str, Any]:
    try:
        packet_dir = research_artifact_path(packet_dir, project_root=project_root)
        protocol_path = research_artifact_path(protocol_path, project_root=project_root)
    except ValueError:
        return {"status": "capture_path_outside_trusted_roots", "captured": False}
    observed = (now or datetime.now(SH_TZ)).astimezone(SH_TZ)
    protocol_sha = _sha256(protocol_path)
    if expected_protocol_sha256 and protocol_sha != expected_protocol_sha256:
        return {"status": "identity_invalid", "captured": False}
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    if observed.date().isoformat() < str(protocol.get("forward_start")):
        return {"status": "waiting_forward_start", "captured": False, "forward_start": protocol.get("forward_start")}
    slot_id = capture_slot(observed, protocol_path=protocol_path)
    if slot_id is None:
        return {"status": "outside_capture_slot", "captured": False}
    source = protocol["source_contract"]
    raw, input_errors = normalize_intraday_emotion_snapshot(
        today_market,
        now=observed,
        maximum_age_seconds=int(source["maximum_age_seconds"]),
        minimum_coverage_pct=float(source["minimum_reference_coverage_pct"]),
    )
    packet = {
        "schema": "intraday_market_emotion_forward_input.v1",
        "decision_day": observed.date().isoformat(),
        "slot_id": slot_id,
        "captured_at": observed.isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "no_backfill": True,
        "protocol": {
            "path": str(protocol_path.relative_to(project_root)),
            "sha256": protocol_sha,
        },
        "raw_observation": raw,
        "raw_observation_sha256": hashlib.sha256(_canonical_bytes(raw)).hexdigest(),
        "source_capture_status": "complete" if not input_errors else "incomplete",
        "source_capture_errors": input_errors,
        "returns_or_labels_read": False,
        "authority": _authority(),
    }
    packet["packet_id"] = hashlib.sha256(_canonical_bytes(packet)).hexdigest()
    path = packet_dir / f"{packet['decision_day']}-{slot_id}.json"
    if path.exists():
        verified = verify_intraday_emotion_packet(path, project_root=project_root)
        if verified.get("valid") is not True:
            return {"status": "existing_packet_invalid", "captured": False, "errors": verified.get("errors") or []}
        existing = verified["packet"]
        return {
            "status": "already_captured", "captured": True,
            "path": str(path), "packet_id": existing.get("packet_id"),
            "source_capture_status": existing.get("source_capture_status"),
        }
    _atomic_publish(path, _canonical_bytes(packet))
    verified = verify_intraday_emotion_packet(path, project_root=project_root)
    if verified.get("valid") is not True:
        raise RuntimeError("intraday emotion packet failed post-write verification")
    return {
        "status": "captured", "captured": True, "path": str(path),
        "packet_id": packet["packet_id"],
        "source_capture_status": packet["source_capture_status"],
    }


def load_intraday_emotion_snapshot(market_intelligence: Any) -> dict[str, Any]:
    """Read one current snapshot while keeping optional evidence fail-closed."""
    try:
        return market_intelligence.panorama("today")
    except Exception as exc:
        return {"status": "unavailable", "reason": f"盘中情绪不可用：{type(exc).__name__}"}


def capture_intraday_emotion_from_source(market_intelligence: Any) -> dict[str, Any]:
    try:
        return capture_intraday_emotion_input(
            load_intraday_emotion_snapshot(market_intelligence)
        )
    except Exception as exc:
        return {"status": "error", "captured": False, "error": f"{type(exc).__name__}: {exc}"}


def build_intraday_emotion_observation(
    route: dict[str, Any], today_market: dict[str, Any] | None, *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Describe live confirmation without changing route, exposure or alerts."""
    raw, errors = normalize_intraday_emotion_snapshot(today_market, now=now)
    state = str(route.get("requested_market_state") or route.get("market_state") or "unknown")
    confirmation = "not_evaluable"
    reason = "盘中输入缺失或过期。" if errors else "当前状态缺少盘中确认定义。"
    if not errors:
        up_ratio = float(raw["up_ratio"])
        limit_up, limit_down = int(raw["limit_up"]), int(raw["limit_down"])
        if state == "sentiment_rising":
            confirmed = up_ratio >= 50 and limit_up >= limit_down
        elif state == "weak_repair":
            confirmed = 45 <= up_ratio <= 60
        elif state in {"risk_closed", "retreat"}:
            confirmed = up_ratio <= 50 or limit_down > limit_up
        else:
            confirmed = None
        if confirmed is not None:
            confirmation = "confirmed" if confirmed else "contradicted"
            reason = "同槽上涨覆盖与涨跌停平衡支持前一交易日状态。" if confirmed else "同槽上涨覆盖或涨跌停平衡反驳前一交易日状态。"
        elif state == "crowded_climax":
            reason = "缺少同槽炸板率与龙头集中度，不能仅凭涨跌家数确认高潮拥挤。"
    state_label = {
        "sentiment_rising": "情绪上升", "weak_repair": "弱修复",
        "crowded_climax": "高潮拥挤", "retreat": "退潮", "risk_closed": "风险关闭",
    }.get(state, "UNKNOWN")
    confirmation_label = {
        "confirmed": "确认", "contradicted": "反证", "not_evaluable": "待观察",
    }[confirmation]
    ratio = f" · 上涨{float(raw['up_ratio']):.1f}%" if raw.get("up_ratio") is not None else ""
    base_budget = route.get("research_budget_hypothesis") or {}
    budget_ready = confirmation == "confirmed" and route.get("status") == "research_route_ready"
    research_budget = {
        "status": "confirmed_base_route" if budget_ready else "hold_new_trades",
        "maximum_new_trades": int(base_budget.get("maximum_new_trades") or 0) if budget_ready else 0,
        "target_exposure_range_pct": base_budget.get("target_exposure_range_pct") if budget_ready else [0, 0],
        "existing_position_risk_reduction_allowed": True,
        "applied_to_execution": False,
    }
    exposure = research_budget["target_exposure_range_pct"]
    budget_summary = (
        f"研究新增≤{research_budget['maximum_new_trades']}笔/仓位{exposure[0]}–{exposure[-1]}%"
        if budget_ready else "研究不开新仓"
    )
    return {
        "status": "intraday_emotion_ready" if not errors else "intraday_emotion_unavailable",
        "market_state_being_checked": state,
        "confirmation": confirmation,
        "summary": f"{state_label} · {confirmation_label}{ratio} · {budget_summary}",
        "research_budget_hypothesis": research_budget,
        "reason": reason,
        "source_capture_errors": errors,
        "raw_observation": raw,
        "applied_to_route": False,
        "applied_to_live_exposure": False,
        "authority": _authority(),
    }


def _authority() -> dict[str, bool]:
    return {
        "research_only": True,
        "changes_live_score": False,
        "changes_live_trade_count": False,
        "changes_live_position_or_quantity": False,
        "generates_buy_or_sell_alert": False,
        "production_strategy": False,
        "automatic_trading": False,
    }


__all__ = [
    "PROTOCOL_PATH", "PROTOCOL_SHA256", "build_intraday_emotion_observation",
    "capture_intraday_emotion_from_source", "capture_intraday_emotion_input", "capture_slot",
    "load_intraday_emotion_snapshot",
    "normalize_intraday_emotion_snapshot", "verify_intraday_emotion_packet",
]
