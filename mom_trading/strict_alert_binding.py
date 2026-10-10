"""Pure binding from a verified strict route to manual buy-alert candidates."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


_SH_TZ = ZoneInfo("Asia/Shanghai")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SLOT_VALIDITY = {
    "morning_1030": (time(10, 42), time(11, 30)),
    "afternoon_1400": (time(14, 12), time(15, 0)),
}

_STRICT_ROUTE_PROVENANCE_SCHEMA = "strict_route_production_strategy_v1"
_STRICT_ROUTE_STRATEGY_KIND = "state_routed_playbook"


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(_SH_TZ)


def _slot_is_current(latest: dict[str, Any], reference_time: object) -> bool:
    observed = _timestamp(reference_time) or datetime.now(_SH_TZ)
    decided = _timestamp(latest.get("decision_at"))
    bounds = _SLOT_VALIDITY.get(str(latest.get("slot_id") or ""))
    if decided is None or bounds is None or decided > observed:
        return False
    decision_day = str(latest.get("decision_day") or "")[:10]
    if decision_day != observed.date().isoformat() or decided.date() != observed.date():
        return False
    wall = observed.timetz().replace(tzinfo=None)
    return bounds[0] <= wall <= bounds[1]


def _immutable_json_ref(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    relative = str(value.get("path") or "").strip()
    expected = str(value.get("sha256") or "").strip().lower()
    if (
        not relative or len(expected) != 64
        or any(char not in "0123456789abcdef" for char in expected)
    ):
        return None
    return {"path": relative, "sha256": expected}


def _verified_json_ref(value: object) -> tuple[dict[str, str], dict[str, Any]] | None:
    immutable = _immutable_json_ref(value)
    if immutable is None:
        return None
    relative = immutable["path"]
    expected = immutable["sha256"]
    path = (PROJECT_ROOT / relative).resolve()
    try:
        path.relative_to(PROJECT_ROOT.resolve())
        raw = path.read_bytes()
        document = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if hashlib.sha256(raw).hexdigest() != expected or not isinstance(document, dict):
        return None
    return immutable, document


def _production_strategy_ref(latest: dict[str, Any]) -> dict[str, Any] | None:
    ref = latest.get("production_strategy_ref")
    if not isinstance(ref, dict):
        return None
    strategy_id = str(ref.get("strategy_id") or "").strip()
    strategy_version = str(ref.get("strategy_version") or "").strip()
    cost = _verified_json_ref(ref.get("cost_after_validation_ref"))
    out_of_sample = _verified_json_ref(ref.get("out_of_sample_validation_ref"))
    admission = _verified_json_ref(ref.get("production_admission_ref"))
    if not (
        ref.get("production_eligible") is True
        and strategy_id and strategy_version and cost and out_of_sample and admission
    ):
        return None
    admission_document = admission[1]
    bindings = admission_document.get("bindings") or {}
    if not (
        admission_document.get("production_eligible") is True
        and str(admission_document.get("strategy_id") or "") == strategy_id
        and str(admission_document.get("strategy_version") or "") == strategy_version
        and bindings.get("cost_after_validation") == cost[0]
        and bindings.get("out_of_sample_validation") == out_of_sample[0]
    ):
        return None
    return {
        "origin": "production_strategy",
        "strategy_kind": _STRICT_ROUTE_STRATEGY_KIND,
        "production_eligible": True,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "cost_after_validation_ref": cost[0],
        "out_of_sample_validation_ref": out_of_sample[0],
        "production_admission_ref": admission[0],
        "validation_evidence": admission[0],
    }


def _route_evidence_ref(route: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(route.get(key))
        for key in (
            "packet_id", "slot_id", "decision_day", "decision_at",
            "market_state", "playbook_id", "account_snapshot_sha256",
        )
    }


def strict_route_buy_provenance(candidate: object) -> dict[str, str | None] | None:
    """Build one non-factor production BUY identity from a verified route row."""
    row = candidate if isinstance(candidate, dict) else {}
    code = str(row.get("code") or "").strip()
    ref = row.get("strategy_ref")
    route_ref = row.get("strict_route_ref")
    if (
        row.get("source") != "strict_route"
        or len(code) != 6 or not code.isdigit()
        or not isinstance(ref, dict)
        or ref.get("origin") != "production_strategy"
        or ref.get("strategy_kind") != _STRICT_ROUTE_STRATEGY_KIND
        or ref.get("production_eligible") is not True
        or not isinstance(route_ref, dict)
        or ref.get("strict_route_ref") != route_ref
    ):
        return None
    strategy_id = str(ref.get("strategy_id") or "").strip()
    strategy_version = str(ref.get("strategy_version") or "").strip()
    cost = _immutable_json_ref(ref.get("cost_after_validation_ref"))
    out_of_sample = _immutable_json_ref(ref.get("out_of_sample_validation_ref"))
    admission = _immutable_json_ref(ref.get("production_admission_ref"))
    packet_id = str(route_ref.get("packet_id") or "").strip().lower()
    account_snapshot = str(
        route_ref.get("account_snapshot_sha256") or ""
    ).strip().lower()
    if not (
        strategy_id and strategy_version and cost and out_of_sample and admission
        and len(packet_id) == 64
        and all(char in "0123456789abcdef" for char in packet_id)
        and len(account_snapshot) == 64
        and all(char in "0123456789abcdef" for char in account_snapshot)
        and all(route_ref.get(key) for key in (
            "slot_id", "decision_day", "decision_at", "market_state", "playbook_id",
        ))
    ):
        return None
    evidence = {
        "schema": _STRICT_ROUTE_PROVENANCE_SCHEMA,
        "strategy_kind": _STRICT_ROUTE_STRATEGY_KIND,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "production_eligible": True,
        "cost_after_validation_ref": cost,
        "out_of_sample_validation_ref": out_of_sample,
        "production_admission_ref": admission,
        "validation_evidence": admission,
        "strict_forward_route": copy.deepcopy(route_ref),
        "code": code,
        "action": "BUY",
    }
    evidence_json = json.dumps(
        evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return {
        "origin": "production_strategy",
        "strategy_id": strategy_id,
        "evidence_ref_json": evidence_json,
        "signal_id": hashlib.sha256(evidence_json.encode("utf-8")).hexdigest(),
    }


def strict_route_alert_context(
    forward_status: object, *, decision_day: str,
    reference_time: object = None,
) -> dict[str, Any]:
    """Return the only strict route identity allowed to create buy alerts."""
    status = forward_status if isinstance(forward_status, dict) else {}
    latest = status.get("latest")
    latest = latest if isinstance(latest, dict) else {}
    runtime = status.get("runtime_admission")
    runtime = runtime if isinstance(runtime, dict) else {}
    selected = [
        copy.deepcopy(row) for row in latest.get("selected_candidates") or []
        if isinstance(row, dict)
    ]
    selected_codes = {
        str(row.get("stock_code") or row.get("code") or "").strip()
        for row in selected
    }
    selected_codes = {
        code for code in selected_codes if len(code) == 6 and code.isdigit()
    }
    target = _finite_number(latest.get("target_exposure_pct"))
    maximum = latest.get("maximum_new_trades")
    maximum = (
        int(maximum) if isinstance(maximum, int) and not isinstance(maximum, bool)
        and maximum >= 0 else None
    )
    eligible_playbooks = [
        str(value).strip() for value in latest.get("eligible_playbooks") or []
        if str(value).strip()
    ]
    entry_playbooks = [
        value for value in eligible_playbooks if value != "retreat_defense"
    ]
    route_current = _slot_is_current(latest, reference_time)
    strategy_ref = _production_strategy_ref(latest)
    packet_id = str(latest.get("packet_id") or "").strip().lower()
    packet_id_valid = bool(
        len(packet_id) == 64
        and all(char in "0123456789abcdef" for char in packet_id)
    )
    account_snapshot_sha256 = str(
        latest.get("account_snapshot_sha256") or ""
    ).strip().lower()
    account_snapshot_valid = bool(
        len(account_snapshot_sha256) == 64
        and all(char in "0123456789abcdef" for char in account_snapshot_sha256)
    )
    route_valid = bool(
        latest.get("valid") is True
        and runtime.get("valid") is True
        and packet_id_valid
        and str(latest.get("decision_day") or "")[:10] == decision_day
        and latest.get("decision_status") in {
            "research_decision_ready", "research_no_candidate",
            "research_no_route", "research_no_trade",
        }
        and target is not None and 0 <= target <= 100
        and maximum is not None
    )
    alert_ready = bool(
        route_valid
        and route_current
        and strategy_ref is not None
        and account_snapshot_valid
        and latest.get("intraday_confirmation") == "confirmed"
        and latest.get("confirmed_sectors")
        and selected_codes
        and maximum and maximum > 0
        and target and target > 0
        and len(entry_playbooks) == 1
    )
    if not route_valid:
        reason = "当日严格市场情绪、板块、账户与候选包尚未验证，禁止生成买入提醒"
    elif not route_current:
        reason = "严格路由不在当前盘中时段或已经过期，禁止跨时段生成买入提醒"
    elif strategy_ref is None:
        reason = "严格路由仍是研究包，未绑定成本后和样本外验证的生产策略，禁止生成买入提醒"
    elif not account_snapshot_valid:
        reason = "严格路由缺少当时账户快照身份，不能把旧仓位数量用于当前买入提醒"
    elif latest.get("intraday_confirmation") != "confirmed":
        reason = "盘中情绪未确认前一市场状态，今日不生成新买入提醒"
    elif not latest.get("confirmed_sectors"):
        reason = "同一观察时点没有确认主线板块，今日不生成新买入提醒"
    elif not selected_codes:
        reason = "严格路由没有合格股票候选，今日不生成新买入提醒"
    elif not maximum or not target:
        reason = "严格路由给出的新增交易笔数或目标仓位为0，今日不生成新买入提醒"
    elif len(entry_playbooks) != 1:
        reason = "严格路由没有唯一可审计买入战法，禁止生成买入提醒"
    else:
        reason = "当日同槽市场、板块、战法、候选与账户证据已验证"
    return {
        "route_valid": route_valid,
        "route_current": route_current,
        "alert_ready": alert_ready,
        "reason": reason,
        "packet_id": packet_id if route_valid else None,
        "slot_id": latest.get("slot_id") if route_valid else None,
        "decision_day": latest.get("decision_day") if route_valid else None,
        "decision_at": latest.get("decision_at") if route_valid else None,
        "market_state": latest.get("market_state") if route_valid else None,
        "target_exposure_pct": target if route_valid else None,
        "maximum_new_trades": maximum if route_valid else 0,
        "selected_codes": selected_codes if route_valid else set(),
        "selected_candidates": selected if route_valid else [],
        "playbook_id": entry_playbooks[0] if alert_ready else None,
        "strategy_ref": strategy_ref if alert_ready else None,
        "account_snapshot_sha256": (
            account_snapshot_sha256 if account_snapshot_valid else None
        ),
        "eligible_playbooks": eligible_playbooks if route_valid else [],
    }


def strict_route_opportunity_candidates(
    context: object,
) -> list[dict[str, Any]]:
    """Promote verified route selections into the fixed deep-review pool."""
    route = context if isinstance(context, dict) else {}
    if route.get("route_valid") is not True:
        return []
    rows: list[dict[str, Any]] = []
    for source in route.get("selected_candidates") or []:
        if not isinstance(source, dict):
            continue
        code = str(source.get("stock_code") or source.get("code") or "").strip()
        if len(code) != 6 or not code.isdigit():
            continue
        strict_route_ref = _route_evidence_ref(route)
        strategy_ref = (
            copy.deepcopy(route.get("strategy_ref"))
            if isinstance(route.get("strategy_ref"), dict) else None
        )
        if strategy_ref is not None:
            strategy_ref["strict_route_ref"] = copy.deepcopy(strict_route_ref)
        row = {
            "code": code,
            "name": source.get("stock_name") or source.get("name") or code,
            "source": "strict_route",
            "close": _finite_number(source.get("price")),
            "screen_score": _finite_number(source.get("score")),
            "avg_amount_20d": None,
            "source_data_as_of": source.get("data_as_of"),
            "strict_route_ref": strict_route_ref,
        }
        if strategy_ref is not None:
            row["strategy_ref"] = strategy_ref
            provenance = strict_route_buy_provenance(row)
            if provenance is None:
                row.pop("strategy_ref", None)
            else:
                strategy_ref["signal_id"] = provenance["signal_id"]
        rows.append(row)
    return rows


def frozen_buy_action_plan(allocation: dict, plan: dict) -> dict:
    """Freeze the exact manual action plan stored with a buy alert."""
    return {
        "trade_plan": copy.deepcopy(allocation.get("trade_plan")),
        "allocation": {
            **{
                key: copy.deepcopy(allocation.get(key))
                for key in (
                    "code", "name", "price", "shares", "amount",
                    "quality_score", "volatility_scale", "allocation_score",
                    "target_weight_pct", "estimated_buy_cash_yuan",
                    "estimated_buy_cost_yuan", "liquidity_capacity_yuan",
                    "participation_pct", "round_trip_cost_pct", "levels",
                    "risk_budget_status", "risk_budget_method",
                    "planned_loss_budget_pct", "planned_loss_budget_yuan",
                    "planned_loss_at_risk_line_yuan",
                    "planned_loss_pct_of_equity", "risk_line_price",
                    "risk_line_distance_pct", "actual_loss_can_exceed_plan",
                    "entry_timing", "strategy_ref",
                )
            },
            "market_target_exposure_pct": plan.get("market_target_exposure_pct"),
            "cash_after_yuan": plan.get("cash_buffer"),
            "risk_line": copy.deepcopy(
                ((allocation.get("levels") or {}).get("effective_risk_level"))
            ),
        },
    }


__all__ = [
    "frozen_buy_action_plan", "strict_route_alert_context",
    "strict_route_buy_provenance", "strict_route_opportunity_candidates",
]
