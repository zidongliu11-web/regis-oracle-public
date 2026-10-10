"""Recompute one research route from inputs available at the decision time."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .data_semantics import MAX_SINGLE_POSITION_PCT
from .mainline_start_candidates import build_mainline_candidates, verify_formula
from .market_sector_route_forward import capture_slot
from .market_sector_strategy_adapter import (
    adapt_market_observation,
    adapt_sector_snapshot,
)
from .market_state_daily import PROJECT_ROOT, _canonical_bytes, _sha256
from .strategy_router import build_research_strategy_route
from .unified_regime_route_replay import build_joint_replay_decision


SH_TZ = ZoneInfo("Asia/Shanghai")
PROTOCOL_PATH = (
    PROJECT_ROOT / "research/NONCONVENTIONAL_MARKET_COGNITION_REPLAY_PROTOCOL_V2.json"
)
PROTOCOL_SHA256 = "90141b329c730f9e35e2809a7a84f383a3dbf94f6ba8cd283b3a39221eb28724"
STAGE_ORDER = (
    "completed_market_state",
    "news_and_market_flow_capture_only",
    "intraday_emotion_confirmation",
    "exposure_and_trade_budget",
    "sector_confirmation",
    "fixed_playbook_route",
    "stock_candidates",
    "account_plan",
)
_OUTCOME_KEYS = {
    "future_return", "future_returns", "label", "labels", "outcome", "outcomes",
    "strategy_return", "annualized_return", "max_drawdown", "sharpe", "nav", "pnl",
}
_AUDIT_OUTCOME_FLAGS = {"returns_or_labels_read", "post_decision_returns_or_labels_read"}
_TIME_KEYS = {
    "source_as_of", "available_at", "data_as_of", "captured_at", "fetched_at",
    "first_seen_at", "published_at", "timestamp", "time",
}
_SAME_DAY_SOURCE_INPUTS = {"sector", "constituents", "account", "intraday_emotion"}
_CONSTITUENT_FIELDS = (
    "stock_code", "stock_name", "price", "change_pct", "volume", "amount",
    "turnover_rate", "total_market_cap", "float_market_cap", "data_as_of",
)


def _time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SH_TZ)
    return parsed.astimezone(SH_TZ)


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _walk(value: object, path: str = ""):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield child_path, str(key), child
            yield from _walk(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")


def _input_errors(
    name: str, value: object, *, decision_at: datetime, slot_id: str,
) -> list[str]:
    if not isinstance(value, dict):
        return [f"{name}:input_missing"]
    errors: list[str] = []
    decision_day = decision_at.date().isoformat()
    if value.get("decision_day") != decision_day:
        errors.append(f"{name}:decision_day_mismatch")
    if value.get("slot_id") != slot_id:
        errors.append(f"{name}:slot_id_mismatch")
    if value.get("no_backfill") is not True:
        errors.append(f"{name}:no_backfill_required")

    source_as_of = _time(value.get("source_as_of"))
    available_at = _time(value.get("available_at"))
    if source_as_of is None:
        errors.append(f"{name}:source_as_of_missing_or_invalid")
    if available_at is None:
        errors.append(f"{name}:available_at_missing_or_invalid")
    if source_as_of is not None and source_as_of > decision_at:
        errors.append(f"{name}:source_after_decision")
    if available_at is not None and available_at > decision_at:
        errors.append(f"{name}:available_after_decision")
    if source_as_of is not None and available_at is not None and available_at < source_as_of:
        errors.append(f"{name}:available_before_source")
    if available_at is not None and available_at.date() != decision_at.date():
        errors.append(f"{name}:available_at_not_decision_day")
    if (
        name in _SAME_DAY_SOURCE_INPUTS and source_as_of is not None
        and source_as_of.date() != decision_at.date()
    ):
        errors.append(f"{name}:source_not_decision_day")
    if name == "market" and source_as_of is not None and source_as_of.date() >= decision_at.date():
        errors.append("market:completed_state_must_precede_decision_day")

    for path, key, child in _walk(value):
        lowered = key.lower()
        if lowered in _OUTCOME_KEYS:
            errors.append(f"{name}:outcome_field_forbidden:{path}")
        if lowered in _AUDIT_OUTCOME_FLAGS and child is not False:
            errors.append(f"{name}:outcome_boundary_invalid:{path}")
        if lowered in _TIME_KEYS and child is not None:
            observed = _time(child)
            if observed is None:
                errors.append(f"{name}:time_invalid:{path}")
            elif observed > decision_at:
                errors.append(f"{name}:future_time_forbidden:{path}")
            elif (
                name in _SAME_DAY_SOURCE_INPUTS and lowered in {"source_as_of", "data_as_of"}
                and observed.date() != decision_at.date()
            ):
                errors.append(f"{name}:time_not_decision_day:{path}")
    try:
        _canonical_bytes(value)
    except (TypeError, ValueError):
        errors.append(f"{name}:not_canonical_json")
    return list(dict.fromkeys(errors))


def _domain_errors(
    constituents: dict[str, Any], account: dict[str, Any], emotion: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    captures = constituents.get("sector_captures")
    if constituents.get("capture_status") != "complete":
        errors.append("constituents:capture_not_complete")
    if not isinstance(captures, list):
        errors.append("constituents:sector_captures_missing")
    else:
        for capture_index, capture in enumerate(captures):
            if not isinstance(capture, dict) or capture.get("complete") is not True:
                errors.append(f"constituents:capture_incomplete:{capture_index}")
                continue
            rows = capture.get("rows")
            if not isinstance(rows, list):
                errors.append(f"constituents:rows_missing:{capture_index}")
                continue
            for row_index, row in enumerate(rows):
                if not isinstance(row, dict) or any(
                    key not in row or row[key] is None for key in _CONSTITUENT_FIELDS
                ):
                    errors.append(
                        f"constituents:required_row_field_missing:{capture_index}:{row_index}"
                    )

    equity, cash = _finite(account.get("equity")), _finite(account.get("available_cash"))
    positions = account.get("positions")
    if account.get("cash_user_recorded") is not True:
        errors.append("account:user_recorded_cash_missing")
    if account.get("valuation_status") != "complete":
        errors.append("account:valuation_incomplete")
    snapshot_id = str(account.get("account_snapshot_sha256") or "")
    if len(snapshot_id) != 64 or any(character not in "0123456789abcdef" for character in snapshot_id.lower()):
        errors.append("account:snapshot_identity_invalid")
    if equity is None or equity <= 0:
        errors.append("account:equity_missing_or_invalid")
    if cash is None or cash < 0:
        errors.append("account:available_cash_missing_or_invalid")
    if not isinstance(positions, list):
        errors.append("account:positions_missing")
    elif any(
        not isinstance(row, dict)
        or _finite(row.get("market_value")) is None
        or float(row["market_value"]) < 0
        for row in positions
    ):
        errors.append("account:position_valuation_missing_or_invalid")

    budget = emotion.get("research_budget_hypothesis")
    confirmation = emotion.get("confirmation")
    if emotion.get("status") != "intraday_emotion_ready":
        errors.append("intraday_emotion:source_not_ready")
    if confirmation not in {"confirmed", "contradicted", "not_evaluable"}:
        errors.append("intraday_emotion:confirmation_missing")
    if not isinstance(budget, dict):
        errors.append("intraday_emotion:budget_missing")
    elif (
        "maximum_new_trades" not in budget
        or budget.get("target_exposure_range_pct") is None
        or budget.get("applied_to_execution") is not False
    ):
        errors.append("intraday_emotion:budget_field_missing")
    return errors


def _load_protocol() -> tuple[dict[str, Any], list[str]]:
    try:
        actual_sha = _sha256(PROTOCOL_PATH)
        protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {}, [f"v2_protocol_unreadable:{type(exc).__name__}"]
    errors: list[str] = []
    if actual_sha != PROTOCOL_SHA256:
        errors.append("v2_protocol_identity_invalid")
    if protocol.get("status") != (
        "successor_frozen_before_first_real_system_replay_or_performance_evaluation"
    ):
        errors.append("v2_protocol_status_invalid")
    states = (protocol.get("portfolio_router") or {}).get("states")
    if not isinstance(states, dict):
        errors.append("v2_protocol_portfolio_router_missing")
    return protocol, errors


def _budget_range(value: object) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    lower, upper = _finite(value[0]), _finite(value[1])
    if lower is None or upper is None:
        return None
    return lower, upper


def _capture_only_summary(
    value: dict[str, Any] | None, *, input_sha256: str | None,
) -> dict[str, Any]:
    if value is None:
        return {
            "status": "not_applied", "present": False,
            "reason": "optional_capture_missing", "applied_to_decision": False,
        }
    return {
        "status": "captured_not_applied", "present": True,
        "source_as_of": value.get("source_as_of"),
        "available_at": value.get("available_at"),
        "input_sha256": input_sha256, "applied_to_decision": False,
    }


def _route_with_v2_budget(
    route: dict[str, Any], policy: dict[str, Any], *, maximum_new_trades: int,
) -> dict[str, Any]:
    normalized = copy.deepcopy(route)
    existing = (route.get("research_budget_hypothesis") or {}).get(
        "allowed_playbooks"
    ) or []
    metadata = {
        str(row.get("id")): copy.deepcopy(row)
        for row in existing if isinstance(row, dict) and row.get("id")
    }
    allowed = list(policy.get("eligible_playbooks") or [])
    normalized["research_budget_hypothesis"] = {
        "maximum_new_trades": maximum_new_trades,
        "target_exposure_range_pct": [
            policy["target_exposure_pct"], policy["target_exposure_pct"],
        ],
        "allowed_playbooks": [
            metadata.get(playbook_id, {"id": playbook_id})
            for playbook_id in allowed
        ],
        "validated": False,
        "applied_to_execution": False,
        "source": "nonconventional_market_cognition_replay_v2",
    }
    return normalized


def _hold_account_plan(
    plan: dict[str, Any], emotion: dict[str, Any], policy: dict[str, Any],
) -> dict[str, Any]:
    held = copy.deepcopy(plan)
    held.update({
        "status": "research_active_no_new_entries",
        "target_exposure_pct": policy["target_exposure_pct"],
        "base_target_exposure_pct": policy["target_exposure_pct"],
        "maximum_new_trades": 0,
        "base_maximum_new_trades": policy["maximum_new_trades"],
        "intraday_emotion_contract_ready": False,
        "intraday_emotion": copy.deepcopy(emotion),
        "pre_execution_plans": [],
        "research_orders": [],
        "executed_orders": [],
        "new_buy_capacity_yuan": 0.0,
        "remaining_unallocated_buy_capacity_yuan": 0.0,
        "research_hold_reason": f"intraday_emotion_{emotion.get('confirmation')}",
    })
    for step in held.get("causal_steps") or []:
        if step.get("name") == "intraday_emotion_confirmation":
            step["value"] = {
                "ready": False, "confirmation": emotion.get("confirmation"),
                "summary": emotion.get("summary"),
            }
        elif step.get("name") == "target_exposure_pct":
            step["value"] = policy["target_exposure_pct"]
        elif step.get("name") == "maximum_new_trades":
            step["value"] = 0
    return held


def _authority() -> dict[str, bool]:
    return {
        "research_only": True,
        "changes_live_score": False,
        "changes_live_trade_count": False,
        "changes_live_position_or_quantity": False,
        "generates_buy_or_sell_alert": False,
        "production_strategy": False,
        "automatic_trading": False,
        "stable_profit_claim": False,
    }


def _finish(result: dict[str, Any]) -> dict[str, Any]:
    result["blocked_reasons"] = list(dict.fromkeys(result.get("blocked_reasons") or []))
    result["replay_id"] = hashlib.sha256(_canonical_bytes(result)).hexdigest()
    return result


def build_system_route_replay(
    *, decision_at: str | datetime,
    market_observation: dict[str, Any] | None,
    sector_snapshot: dict[str, Any] | None,
    constituent_packet: dict[str, Any] | None,
    account: dict[str, Any] | None,
    intraday_emotion: dict[str, Any] | None,
    news_capture: dict[str, Any] | None = None,
    market_flow_capture: dict[str, Any] | None = None,
    retired_entry_playbooks: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Re-run the full research decision without execution or performance data."""
    observed = _time(decision_at)
    slot_id = capture_slot(observed) if observed is not None else None
    base: dict[str, Any] = {
        "schema": "system_route_replay_driver.v2",
        "status": "blocked",
        "decision_day": observed.date().isoformat() if observed else None,
        "slot_id": slot_id,
        "decision_at": observed.isoformat(timespec="seconds") if observed else None,
        "stage_order": list(STAGE_ORDER),
        "stage_trace": [],
        "blocked_reasons": [],
        "active_no_trade": False,
        "inputs_sha256": {},
        "protocol": {
            "path": str(PROTOCOL_PATH.relative_to(PROJECT_ROOT)),
            "sha256": PROTOCOL_SHA256,
            "identity_valid": False,
        },
        "raw_state": None,
        "raw_H1_state": None,
        "capture_only_evidence": {
            "role": "timestamped_capture_only_not_applied_to_decision",
            "news": _capture_only_summary(None, input_sha256=None),
            "market_flow": _capture_only_summary(None, input_sha256=None),
        },
        "market": None,
        "market_budget": None,
        "sector": None,
        "route": None,
        "candidates": None,
        "account_plan": None,
        "account_snapshot_sha256": None,
        "returns_or_labels_read": False,
        "performance_computed": False,
        "authority": _authority(),
    }
    if observed is None:
        base["blocked_reasons"] = ["decision_at_invalid"]
        return _finish(base)
    if retired_entry_playbooks not in ((), ("mainline_start_confirmation",)):
        base["blocked_reasons"] = ["unsupported_retired_playbook_contract"]
        return _finish(base)
    if slot_id is None:
        base["blocked_reasons"] = ["decision_at_outside_frozen_slot"]
        return _finish(base)

    inputs = {
        "market": market_observation,
        "sector": sector_snapshot,
        "constituents": constituent_packet,
        "account": account,
        "intraday_emotion": intraday_emotion,
    }
    errors: list[str] = []
    for name, value in inputs.items():
        errors.extend(_input_errors(name, value, decision_at=observed, slot_id=slot_id))
        if isinstance(value, dict):
            try:
                base["inputs_sha256"][name] = hashlib.sha256(_canonical_bytes(value)).hexdigest()
            except (TypeError, ValueError):
                base["inputs_sha256"][name] = None
    optional_captures = {
        "news": news_capture,
        "market_flow": market_flow_capture,
    }
    for name, value in optional_captures.items():
        if value is None:
            base["inputs_sha256"][name] = None
            continue
        errors.extend(_input_errors(
            name, value, decision_at=observed, slot_id=slot_id,
        ))
        if isinstance(value, dict):
            try:
                base["inputs_sha256"][name] = hashlib.sha256(
                    _canonical_bytes(value)
                ).hexdigest()
            except (TypeError, ValueError):
                base["inputs_sha256"][name] = None
    if all(isinstance(value, dict) for value in (
        constituent_packet, account, intraday_emotion,
    )):
        errors.extend(_domain_errors(
            constituent_packet, account, intraday_emotion,
        ))
    if errors:
        base["blocked_reasons"] = errors
        return _finish(base)

    market_raw = copy.deepcopy(market_observation)
    sector_raw = copy.deepcopy(sector_snapshot)
    constituents = copy.deepcopy(constituent_packet)
    account_input = copy.deepcopy(account)
    base["account_snapshot_sha256"] = account_input.get(
        "account_snapshot_sha256"
    )
    emotion_input = copy.deepcopy(intraday_emotion)
    news_input = copy.deepcopy(news_capture)
    flow_input = copy.deepcopy(market_flow_capture)

    # A retired entry rule must not run merely to have its candidates discarded
    # later. Historical calls keep the original formula identity requirement.
    formula_check = None
    if not retired_entry_playbooks:
        formula_check = verify_formula()
        if formula_check.get("valid") is not True:
            base["blocked_reasons"] = [
                f"stock_formula_identity_invalid:{reason}"
                for reason in formula_check.get("errors") or ["unknown"]
            ]
            return _finish(base)
    protocol, protocol_errors = _load_protocol()
    if protocol_errors:
        base["blocked_reasons"] = protocol_errors
        return _finish(base)
    base["protocol"]["identity_valid"] = True

    market = adapt_market_observation(
        market_raw, as_of=str(market_raw["source_as_of"])[:10],
    )
    base["market"] = market
    base["raw_state"] = str(market_raw.get("state") or "UNKNOWN")
    base["raw_H1_state"] = str(market_raw.get("raw_state") or "UNKNOWN")
    base["stage_trace"].append({
        "stage": "completed_market_state", "status": market["status"],
        "raw_state": base["raw_state"],
    })
    if market.get("status") != "ready":
        base["blocked_reasons"] = [
            f"market:adapter_incomplete:{key}" for key in market.get("missing_inputs") or []
        ] or ["market:adapter_incomplete"]
        return _finish(base)

    translation = (protocol.get("market_state_contract") or {}).get(
        "raw_completed_state_to_router_state"
    ) or {}
    expected_state = translation.get(base["raw_state"])
    if expected_state is None or market.get("state") != expected_state:
        base["blocked_reasons"] = ["market:adapter_translation_not_equal_to_v2"]
        return _finish(base)

    base["capture_only_evidence"] = {
        "role": "timestamped_capture_only_not_applied_to_decision",
        "news": _capture_only_summary(
            news_input, input_sha256=base["inputs_sha256"].get("news"),
        ),
        "market_flow": _capture_only_summary(
            flow_input, input_sha256=base["inputs_sha256"].get("market_flow"),
        ),
    }
    capture_status = (
        "captured_not_applied"
        if news_input is not None or flow_input is not None else "not_applied"
    )
    base["stage_trace"].append({
        "stage": "news_and_market_flow_capture_only",
        "status": capture_status, "applied_to_decision": False,
    })

    state = str(market["state"])
    policy = ((protocol.get("portfolio_router") or {}).get("states") or {}).get(state)
    if not isinstance(policy, dict):
        base["blocked_reasons"] = ["market:state_has_no_frozen_budget"]
        return _finish(base)
    emotion_budget = emotion_input["research_budget_hypothesis"]
    confirmation = emotion_input["confirmation"]
    if emotion_input.get("market_state_being_checked") != state:
        base["blocked_reasons"] = ["intraday_emotion:market_state_mismatch"]
        return _finish(base)
    target_range = _budget_range(emotion_budget.get("target_exposure_range_pct"))
    expected_target = float(policy["target_exposure_pct"])
    target_matches = target_range == (expected_target, expected_target)
    if confirmation == "confirmed" and (
        emotion_budget.get("status") != "confirmed_base_route"
        or _finite(emotion_budget.get("maximum_new_trades")) != float(
            policy["maximum_new_trades"]
        )
        or not target_matches
    ):
        base["blocked_reasons"] = ["intraday_emotion:confirmed_budget_mismatch"]
        return _finish(base)
    if confirmation != "confirmed" and (
        emotion_budget.get("status") != "hold_new_trades"
        or _finite(emotion_budget.get("maximum_new_trades")) != 0.0
        or not target_matches
    ):
        base["blocked_reasons"] = ["intraday_emotion:hold_budget_mismatch"]
        return _finish(base)
    base["stage_trace"].append({
        "stage": "intraday_emotion_confirmation",
        "status": "confirmed" if confirmation == "confirmed" else "active_hold",
        "confirmation": confirmation,
    })
    effective_maximum_new_trades = (
        int(policy["maximum_new_trades"]) if confirmation == "confirmed" and not retired_entry_playbooks else 0
    )
    base["market_budget"] = {
        "raw_state": base["raw_state"],
        "market_state": state,
        "base_target_exposure_pct": policy["target_exposure_pct"],
        "target_exposure_pct": policy["target_exposure_pct"],
        "base_maximum_new_trades": policy["maximum_new_trades"],
        "maximum_new_trades": effective_maximum_new_trades,
        "eligible_playbooks": list(policy.get("eligible_playbooks") or []),
        "intraday_confirmation": confirmation,
        "applied_to_execution": False,
    }
    base["stage_trace"].append({
        "stage": "exposure_and_trade_budget", "status": "recomputed_v2",
    })

    sector = adapt_sector_snapshot(sector_raw, now=observed)
    base["sector"] = sector
    base["stage_trace"].append({"stage": "sector_confirmation", "status": sector["status"]})
    if sector.get("status") == "unavailable":
        base["blocked_reasons"] = ["sector:adapter_input_unavailable"]
        return _finish(base)
    if sector.get("status") == "confirmed" and not constituents.get("sector_captures"):
        base["blocked_reasons"] = ["constituents:confirmed_sector_rows_missing"]
        return _finish(base)

    route = _route_with_v2_budget(
        build_research_strategy_route(market, sector, account_input),
        policy, maximum_new_trades=effective_maximum_new_trades,
    )
    base["route"] = route
    base["stage_trace"].append({
        "stage": "fixed_playbook_route", "status": route["status"],
    })
    if retired_entry_playbooks:
        candidates = {
            "status": "terminal_playbook_retired_no_entry_candidate",
            "selected": [], "selected_count": 0, "evaluated_count": 0,
            "retired_formula_evaluated": False,
            "returns_or_labels_read": False,
        }
    else:
        candidates = build_mainline_candidates(route, constituents, formula_check["formula"])
    base["candidates"] = candidates
    base["stage_trace"].append({
        "stage": "stock_candidates", "status": candidates["status"],
        "selected_count": candidates["selected_count"],
    })
    decision_protocol = {
        "portfolio_router": copy.deepcopy(protocol["portfolio_router"]),
        "small_capital_execution": {
            "maximum_single_stock_target_pct": MAX_SINGLE_POSITION_PCT,
        },
    }
    plan_emotion = copy.deepcopy(emotion_input)
    if confirmation != "confirmed":
        plan_emotion["confirmation"] = "confirmed"
        plan_emotion["research_budget_hypothesis"] = {
            "status": "confirmed_base_route",
            "maximum_new_trades": policy["maximum_new_trades"],
            "target_exposure_range_pct": [
                policy["target_exposure_pct"], policy["target_exposure_pct"],
            ],
            "applied_to_execution": False,
        }
    if retired_entry_playbooks:
        plan_policy = decision_protocol["portfolio_router"]["states"][state]
        plan_policy["maximum_new_trades"] = 0
        plan_policy["eligible_playbooks"] = [
            name for name in plan_policy.get("eligible_playbooks", [])
            if name not in retired_entry_playbooks
        ]
        plan_emotion["research_budget_hypothesis"]["maximum_new_trades"] = 0
    plan = build_joint_replay_decision(
        route, candidates, account_input, decision_protocol,
        intraday_emotion=plan_emotion,
    )
    if confirmation != "confirmed" and plan.get("status") != "account_input_incomplete":
        plan = _hold_account_plan(plan, emotion_input, policy)
    base["account_plan"] = plan
    base["stage_trace"].append({"stage": "account_plan", "status": plan["status"]})

    if plan.get("status") == "account_input_incomplete":
        base["blocked_reasons"] = ["account:downstream_contract_incomplete"]
        return _finish(base)
    if route.get("status") != "research_route_ready":
        base["status"] = "research_no_route"
        base["active_no_trade"] = True
    elif plan.get("status") == "research_active_no_new_entries":
        base["status"] = "research_no_trade"
        base["active_no_trade"] = True
    elif not candidates.get("selected"):
        base["status"] = "research_no_candidate"
        base["active_no_trade"] = True
    else:
        base["status"] = "research_decision_ready"
    return _finish(base)


__all__ = ["STAGE_ORDER", "build_system_route_replay"]
