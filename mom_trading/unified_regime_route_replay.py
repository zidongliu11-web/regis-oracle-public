"""Pure research replay of regime -> exposure -> playbook -> stock -> account."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from quantqc import ashare as ashare_execution
from quantqc.costs import COSTS, LOT_SIZE, one_side_cost_breakdown

from .intraday_market_emotion_forward import (
    build_intraday_emotion_observation,
    verify_intraday_emotion_packet,
)
from .mainline_start_candidates import verify_candidate_packet
from .market_sector_route_shadow import verify_shadow_decision_packet
from .market_state_daily import PROJECT_ROOT, _atomic_publish, _canonical_bytes, _sha256
from .portfolio_execution_contract import (
    CONTRACT_RELATIVE_PATH, verify_daily_execution_contract,
)

DAILY_EXECUTION_CONTRACT_PATH = PROJECT_ROOT / CONTRACT_RELATIVE_PATH


PROTOCOL_PATH = PROJECT_ROOT / "research/UNIFIED_REGIME_ROUTE_PORTFOLIO_BACKTEST_PROTOCOL_V1.json"
PROTOCOL_SHA256 = "3042bad82844367e9536b591f002820ad0ac01df767ee259f7260e31a3d0fff3"
AMENDMENT_PATH = (
    PROJECT_ROOT
    / "research/UNIFIED_REGIME_ROUTE_PORTFOLIO_BACKTEST_PROTOCOL_V1_AMENDMENT_2.json"
)
AMENDMENT_SHA256 = "54018ce67cd78192f6436b36d793d9221d863d58331fb6ef248c20177a195a0d"
EXECUTION_AMENDMENT_PATH = (
    PROJECT_ROOT
    / "research/UNIFIED_REGIME_ROUTE_PORTFOLIO_BACKTEST_PROTOCOL_V1_AMENDMENT_3.json"
)
EXECUTION_AMENDMENT_SHA256 = "642d9f9e1839c776747c2c9a7e55ef774266aefb96809ded83db013d57fdc4ab"
EXECUTION_CORRECTION_PATH = (
    PROJECT_ROOT
    / "research/UNIFIED_REGIME_ROUTE_PORTFOLIO_BACKTEST_PROTOCOL_V1_AMENDMENT_4.json"
)
EXECUTION_CORRECTION_SHA256 = "feae4c5a081afc485c84b8e502a18e437bd648676f934a2078268587df38ae01"
PORTFOLIO_EXECUTION_AMENDMENT_PATH = (
    PROJECT_ROOT
    / "research/UNIFIED_REGIME_ROUTE_PORTFOLIO_BACKTEST_PROTOCOL_V1_AMENDMENT_5.json"
)
ROUTER_REBIND_AMENDMENT_PATH = (
    PROJECT_ROOT
    / "research/UNIFIED_REGIME_ROUTE_PORTFOLIO_BACKTEST_PROTOCOL_V1_AMENDMENT_14.json"
)
ROUTER_REBIND_AMENDMENT_SHA256 = (
    "0dea6f5e7d90f24a14dd9271b2b86ac4d2da63d990f474610740fbf6c1c9c203"
)
PACKET_DIR = (
    PROJECT_ROOT / "research/data/market_sector_route_forward/v1/joint_replay_packets"
)


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


_COST_KEYS = ("commission", "transfer", "stamp", "slippage", "market_impact", "total")


def _date_text(value: object) -> str | None:
    try:
        return datetime.fromisoformat(str(value)[:10]).date().isoformat()
    except (TypeError, ValueError):
        return None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        return None
    return int(number)


def _identity(payload: dict[str, Any], field: str) -> str:
    body = dict(payload)
    body.pop(field, None)
    try:
        return hashlib.sha256(_canonical_bytes(body)).hexdigest()
    except (TypeError, ValueError):
        return ""


def _portfolio_execution_amendment_check(
    *, project_root: Path = PROJECT_ROOT,
    path: Path = PORTFOLIO_EXECUTION_AMENDMENT_PATH,
    superseded_bindings: set[str] | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    sha256: str | None = None
    amendment: dict[str, Any] = {}
    try:
        sha256 = _sha256(path)
        amendment = json.loads(path.read_text(encoding="utf-8"))
        if amendment.get("status") != "daily_joint_execution_state_machine_frozen_no_outcome_run":
            errors.append("portfolio_execution_amendment_status_invalid")
        for name, binding in (amendment.get("bindings") or {}).items():
            target = _project_path(binding.get("path"), project_root=project_root)
            if (
                name not in (superseded_bindings or set())
                and (not target.is_file() or _sha256(target) != binding.get("sha256"))
            ):
                errors.append(f"portfolio_execution_binding_invalid:{name}")
        if set(amendment.get("supersedes_predecessor_bindings") or []) != {
            "shared_A_share_execution_contract", "superseding_cost_model",
        }:
            errors.append("portfolio_execution_supersession_invalid")
        contract = amendment.get("execution_contract") or {}
        if not all(contract.get(key) is True for key in (
            "sell_before_buy", "T_plus_1_share_inventory", "integer_share_state",
            "odd_lot_only_on_full_liquidation", "cash_includes_costs",
            "lagged_ADV_capacity", "market_impact_cost", "no_backfill",
            "deterministic_state_hash",
        )):
            errors.append("portfolio_execution_contract_invalid")
        authority = amendment.get("authority") or {}
        required_authority = {
            "modify_live_score", "modify_live_exposure", "modify_live_trade_count",
            "emit_buy_or_sell_signal", "run_strategy_returns", "admit_strategy",
            "production", "automatic_trading", "stable_profit_claim",
        }
        if not required_authority.issubset(authority) or any(
            value is not False for value in authority.values()
        ):
            errors.append("portfolio_execution_authority_invalid")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"portfolio_execution_amendment_unreadable:{exc}")
    return {
        "valid": not errors, "errors": errors, "sha256": sha256,
        "path": str(path), "amendment": amendment,
    }


def _current_portfolio_superseded_bindings() -> dict[str, Any]:
    """Resolve the frozen successor before accepting a stale predecessor binding."""
    errors: list[str] = []
    superseded: set[str] = set()
    try:
        if _sha256(ROUTER_REBIND_AMENDMENT_PATH) != ROUTER_REBIND_AMENDMENT_SHA256:
            errors.append("router_rebind_amendment_sha256_mismatch")
        amendment = json.loads(
            ROUTER_REBIND_AMENDMENT_PATH.read_text(encoding="utf-8")
        )
        if amendment.get("status") != (
            "unified_market_state_budget_router_rebound_without_outcome_run"
        ):
            errors.append("router_rebind_amendment_status_invalid")
        for name, binding in (amendment.get("bindings") or {}).items():
            target = _project_path(binding.get("path"), project_root=PROJECT_ROOT)
            if not target.is_file() or _sha256(target) != binding.get("sha256"):
                errors.append(f"router_rebind_binding_invalid:{name}")
        superseded = set(amendment.get("supersedes_predecessor_bindings") or [])
        if superseded != {
            "joint_execution_state_machine", "current_execution_engine",
        }:
            errors.append("router_rebind_predecessor_supersession_invalid")
        if any(
            value is not False
            for value in (amendment.get("authority") or {}).values()
        ):
            errors.append("router_rebind_authority_invalid")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"router_rebind_amendment_unreadable:{exc}")
    return {"valid": not errors, "errors": errors, "superseded": superseded}


def _current_portfolio_execution_check() -> dict[str, Any]:
    # Daily research execution has its own explicit frozen source contract.
    # Legacy higher-level protocols keep their old gates and missing records.
    return verify_daily_execution_contract(
        project_root=PROJECT_ROOT, path=DAILY_EXECUTION_CONTRACT_PATH,
    )


def _portfolio_state_errors(state: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if state.get("schema") != "joint_execution_portfolio_state.v1":
        errors.append("state_schema_invalid")
    if state.get("state_id") and state.get("state_id") != _identity(state, "state_id"):
        errors.append("state_id_invalid")
    if state.get("last_trade_date") is not None and _date_text(
        state.get("last_trade_date")
    ) is None:
        errors.append("state_trade_date_invalid")
    cash = _finite(state.get("cash_yuan"))
    if cash is None or cash < 0:
        errors.append("state_cash_invalid")
    if not str(state.get("cost_model_id") or ""):
        errors.append("state_cost_model_missing")
    positions = state.get("positions")
    if not isinstance(positions, dict):
        errors.append("state_positions_invalid")
        positions = {}
    for code, position in positions.items():
        if not code or not isinstance(position, dict):
            errors.append("position_identity_invalid")
            continue
        total = _nonnegative_int(position.get("shares_total"))
        sellable = _nonnegative_int(position.get("sellable_shares"))
        book_cost = _finite(position.get("book_cost_yuan"))
        locked = position.get("locked_buys")
        if total is None or sellable is None or book_cost is None or book_cost < 0:
            errors.append(f"position_values_invalid:{code}")
            continue
        if not isinstance(locked, list):
            errors.append(f"position_locked_buys_invalid:{code}")
            continue
        locked_total = 0
        state_day = _date_text(state.get("last_trade_date"))
        for lot in locked:
            shares = _nonnegative_int((lot or {}).get("shares") if isinstance(lot, dict) else None)
            buy_date = _date_text((lot or {}).get("buy_trade_date") if isinstance(lot, dict) else None)
            if shares is None or shares <= 0 or buy_date is None:
                errors.append(f"position_locked_lot_invalid:{code}")
                continue
            if state_day is not None and buy_date > state_day:
                errors.append(f"position_locked_lot_from_future:{code}")
            locked_total += shares
        if sellable + locked_total != total:
            errors.append(f"position_share_conservation_invalid:{code}")
    pending = state.get("pending_exit_orders")
    if not isinstance(pending, list):
        errors.append("pending_exit_orders_invalid")
        pending = []
    pending_ids = [row.get("order_id") for row in pending if isinstance(row, dict)]
    if any(not value for value in pending_ids) or len(set(pending_ids)) != len(pending_ids):
        errors.append("pending_exit_order_identity_invalid")
    cumulative = state.get("cumulative_costs")
    if not isinstance(cumulative, dict):
        errors.append("cumulative_costs_invalid")
    else:
        for key in _COST_KEYS:
            value = _finite(cumulative.get(key))
            if value is None or value < 0:
                errors.append(f"cumulative_cost_invalid:{key}")
    for key in ("cumulative_buy_notional", "cumulative_sell_notional"):
        value = _finite(state.get(key))
        if value is None or value < 0:
            errors.append(f"{key}_invalid")
    return errors


def _unlock_t1_inventory(state: dict[str, Any], trade_date: str) -> None:
    for position in state["positions"].values():
        still_locked: list[dict[str, Any]] = []
        for lot in position["locked_buys"]:
            if str(lot["buy_trade_date"]) < trade_date:
                position["sellable_shares"] += int(lot["shares"])
            else:
                still_locked.append(lot)
        position["locked_buys"] = sorted(
            still_locked, key=lambda row: (row["buy_trade_date"], row["shares"]),
        )


def _execution_config(raw: dict[str, Any] | None) -> tuple[dict[str, float], list[str]]:
    source = raw or {}
    errors: list[str] = []
    try:
        config = {
            "max_adv_participation": float(source.get("max_adv_participation", 0.05)),
            "impact_coefficient_bps": float(source.get("impact_coefficient_bps", 25.0)),
            "slippage": float(source.get("slippage", COSTS["slippage"])),
        }
    except (TypeError, ValueError):
        return {
            "max_adv_participation": math.nan,
            "impact_coefficient_bps": math.nan,
            "slippage": math.nan,
        }, ["execution_config_invalid"]
    if not math.isfinite(config["max_adv_participation"]) or not (
        0 < config["max_adv_participation"] <= 1
    ):
        errors.append("max_adv_participation_invalid")
    for key in ("impact_coefficient_bps", "slippage"):
        if not math.isfinite(config[key]) or config[key] < 0:
            errors.append(f"{key}_invalid")
    return config, errors


def _execute_opening_order(
    state: dict[str, Any], order: dict[str, Any], quote: dict[str, Any] | None,
    trade_date: str, config: dict[str, float],
) -> dict[str, Any]:
    side = str(order.get("side") or "")
    code = str(order.get("stock_code") or order.get("code") or "")
    requested = _nonnegative_int(order.get("requested_shares"))
    event = {
        "order_id": order.get("order_id"), "stock_code": code, "side": side,
        "requested_shares": requested, "filled_shares": 0,
        "status": "unfilled", "reason": None,
    }
    if side not in {"buy", "sell"} or not code or requested is None or requested <= 0:
        event["reason"] = "order_input_invalid"
        return event
    if ashare_execution.board_of(code) not in {"main", "gemb"}:
        event["reason"] = "security_outside_main_and_ChiNext_scope"
        return event
    if not isinstance(quote, dict):
        event["reason"] = "market_quote_missing"
        return event
    market = ashare_execution.opening_limit_order_executability(
        code=code, trade_date=trade_date, side=side,
        order_price=order.get("limit_price_yuan"),
        open_price=quote.get("open_price"), prev_close=quote.get("prev_close"),
        tradable=quote.get("tradable"),
        is_risk_warning=quote.get("is_risk_warning"),
        no_price_limit=quote.get("no_price_limit"),
    )
    if market.get("executable") is not True:
        event["reason"] = market.get("reason") or "execution_identity_invalid"
        event["market_check"] = market
        return event
    price = _finite(quote.get("open_price"))
    adv = _finite(quote.get("lagged_adv_yuan"))
    if price is None or price <= 0 or adv is None or adv <= 0:
        event["reason"] = "lagged_ADV_or_open_price_missing"
        return event
    cap_shares = max(0, int(adv * config["max_adv_participation"] / price))
    position = state["positions"].get(code) or {
        "shares_total": 0, "sellable_shares": 0, "locked_buys": [],
        "book_cost_yuan": 0.0,
    }
    full_liquidation = order.get("full_liquidation") is True
    if side == "sell" and full_liquidation and (
        position["shares_total"] != position["sellable_shares"]
        or requested != position["shares_total"]
    ):
        event["reason"] = "full_liquidation_inventory_not_fully_sellable"
        return event
    quantity = ashare_execution.constrain_share_quantity(
        requested, side=side,
        available_to_sell=position["sellable_shares"] if side == "sell" else None,
        full_liquidation=full_liquidation,
        liquidity_cap_shares=cap_shares,
        lot_size=LOT_SIZE,
    )
    filled = int(quantity["accepted_shares"])
    if filled <= 0:
        event["reason"] = str(quantity.get("reason") or "quantity_not_executable")
        return event
    while filled > 0:
        notional = float(filled * price)
        participation = min(1.0, notional / adv)
        impact_rate = (
            config["impact_coefficient_bps"] / 10_000.0 * math.sqrt(participation)
        )
        costs = one_side_cost_breakdown(
            notional, side, config["slippage"], impact_rate,
        )
        if side == "sell" or notional + float(costs["total"]) <= state["cash_yuan"] + 1e-9:
            break
        filled -= LOT_SIZE
    if filled <= 0:
        event["reason"] = "insufficient_cash_after_costs"
        return event
    event.update({
        "filled_shares": int(filled),
        "unfilled_shares": int(requested - filled),
        "status": "filled" if filled == requested else "partial",
        "reason": "filled" if filled == requested else "ADV_or_cash_partial_fill",
        "fill_price_yuan": float(price), "notional_yuan": float(notional),
        "actual_ADV_participation": float(participation),
        "costs": {key: float(costs[key]) for key in _COST_KEYS},
        "fill_model": "daily_open_proxy_lagged_ADV_cap_limit_event_no_fill",
    })
    return event


def _decision_entry_orders(
    packets: list[dict[str, Any]], previous_trade_date: str,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    orders: list[dict[str, Any]] = []
    packet_ids: list[str] = []
    errors: list[str] = []
    for packet in sorted(packets, key=lambda row: str(row.get("packet_id") or "")):
        packet_id = str(packet.get("packet_id") or "")
        if not packet_id or packet_id != _identity(packet, "packet_id"):
            errors.append("decision_packet_id_invalid")
            continue
        packet_ids.append(packet_id)
        if _date_text(packet.get("decision_day")) != previous_trade_date:
            errors.append(f"decision_not_from_previous_trade_date:{packet_id}")
            continue
        decision = packet.get("decision") or {}
        if (
            decision.get("returns_or_labels_read") is not False
            or (decision.get("authority") or {}).get("research_only") is not True
            or decision.get("execution_replay_complete") is not False
        ):
            errors.append(f"decision_contract_invalid:{packet_id}")
            continue
        plans = decision.get("pre_execution_plans")
        maximum = _nonnegative_int(decision.get("maximum_new_trades"))
        if not isinstance(plans, list) or maximum is None or len(plans) > maximum:
            errors.append(f"decision_plan_budget_invalid:{packet_id}")
            continue
        for index, plan in enumerate(plans):
            if not isinstance(plan, dict):
                errors.append(f"decision_plan_invalid:{packet_id}")
                continue
            plan_id = str(plan.get("plan_id") or hashlib.sha256(
                _canonical_bytes({"packet_id": packet_id, "index": index, "plan": plan})
            ).hexdigest())
            orders.append({
                "order_id": hashlib.sha256(
                    f"{packet_id}:{plan_id}".encode("utf-8")
                ).hexdigest(),
                "source_packet_id": packet_id,
                "stock_code": plan.get("stock_code"),
                "side": plan.get("side"),
                "requested_shares": plan.get("shares"),
                "limit_price_yuan": plan.get("limit_price_yuan"),
                "order_session": plan.get("order_session"),
                "order_type": plan.get("order_type"),
                "time_in_force": "opening_only",
                "full_liquidation": False,
                "reason": "strategy_entry",
            })
    return orders, packet_ids, errors


def _exit_order_errors(
    order: object, *, trade_date: str, newly_submitted: bool,
) -> list[str]:
    if not isinstance(order, dict):
        return ["exit_order_contract_invalid"]
    valid_date = _date_text(order.get("valid_trade_date"))
    requested = _nonnegative_int(order.get("requested_shares"))
    limit_price = _finite(order.get("limit_price_yuan"))
    errors: list[str] = []
    if (
        order.get("side") != "sell"
        or order.get("order_session") != "next_trading_day_opening_auction"
        or order.get("order_type") != "limit"
        or order.get("time_in_force") != "carry_until_executable"
    ):
        errors.append("exit_order_contract_invalid")
    if order.get("reason") not in {"strategy_exit", "risk_reduction"}:
        errors.append("exit_order_reason_invalid")
    if not isinstance(order.get("full_liquidation"), bool):
        errors.append("exit_order_full_liquidation_invalid")
    if not str(order.get("source_packet_id") or ""):
        errors.append("exit_order_source_missing")
    if not str(order.get("order_id") or ""):
        errors.append("exit_order_identity_invalid")
    if valid_date is None or (newly_submitted and valid_date != trade_date):
        errors.append("exit_order_valid_date_invalid")
    if requested is None or requested <= 0:
        errors.append("exit_order_quantity_invalid")
    elif order.get("full_liquidation") is False and requested % LOT_SIZE:
        errors.append("exit_order_odd_lot_partial_invalid")
    if limit_price is None or limit_price <= 0:
        errors.append("exit_order_limit_price_invalid")
    return errors


def replay_joint_execution_day(
    previous_state: dict[str, Any], decision_packets: list[dict[str, Any]],
    market_day: dict[str, Any], exit_orders: list[dict[str, Any]] | None = None,
    *, execution_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure daily replay: unlock T+1, sell first, then buy, and hash state."""
    if not isinstance(previous_state, dict) or not isinstance(market_day, dict):
        return {
            "status": "execution_input_invalid", "executed": False,
            "errors": ["state_or_market_input_invalid"], "state_unchanged": True,
            "authority": _authority(),
        }
    state_errors = _portfolio_state_errors(previous_state)
    config, config_errors = _execution_config(execution_config)
    errors = state_errors + config_errors
    execution_protocol = _current_portfolio_execution_check()
    errors.extend(execution_protocol["errors"])
    trade_date = _date_text(market_day.get("trade_date"))
    previous_trade_date = _date_text(market_day.get("previous_trade_date"))
    market_id = str(market_day.get("market_snapshot_id") or "")
    if market_day.get("no_backfill") is not True:
        errors.append("market_no_backfill_invalid")
    if not market_id or market_id != _identity(market_day, "market_snapshot_id"):
        errors.append("market_snapshot_id_invalid")
    if trade_date is None or previous_trade_date is None or trade_date <= previous_trade_date:
        errors.append("market_trade_date_invalid")
    if _date_text(previous_state.get("last_trade_date")) != previous_trade_date:
        errors.append("state_market_date_chain_invalid")
    packets = decision_packets if isinstance(decision_packets, list) else []
    if not isinstance(decision_packets, list):
        errors.append("decision_packets_invalid")
    entry_orders, decision_ids, decision_errors = _decision_entry_orders(
        packets, previous_trade_date or "",
    )
    errors.extend(decision_errors)
    if not isinstance(market_day.get("quotes"), dict):
        errors.append("market_quotes_invalid")
    if exit_orders is None:
        new_exit_orders: list[dict[str, Any]] = []
    elif isinstance(exit_orders, list):
        new_exit_orders = exit_orders
    else:
        new_exit_orders = []
        errors.append("exit_orders_invalid")
    for order in new_exit_orders:
        errors.extend(_exit_order_errors(
            order, trade_date=trade_date or "", newly_submitted=True,
        ))
    prior_state_id = (
        str(previous_state.get("state_id"))
        if previous_state.get("state_id") else _identity(previous_state, "state_id")
    )
    if errors:
        return {
            "status": "execution_input_invalid", "executed": False,
            "errors": sorted(set(errors)), "previous_state_id": prior_state_id,
            "decision_packet_ids": sorted(decision_ids),
            "market_snapshot_id": market_id or None, "state_unchanged": True,
            "authority": _authority(),
        }

    state = copy.deepcopy(previous_state)
    state.pop("state_id", None)
    _unlock_t1_inventory(state, trade_date)
    state["pending_exit_orders"] = sorted(
        list(state.get("pending_exit_orders") or []) + new_exit_orders,
        key=lambda row: (str(row.get("priority") or ""), str(row.get("stock_code") or ""),
                         str(row.get("order_id") or "")),
    )
    all_ids = [str(row.get("order_id") or "") for row in state["pending_exit_orders"]]
    if any(not value for value in all_ids) or len(set(all_ids)) != len(all_ids):
        return {
            "status": "execution_input_invalid", "executed": False,
            "errors": ["exit_order_identity_invalid"],
            "previous_state_id": prior_state_id,
            "decision_packet_ids": sorted(decision_ids),
            "market_snapshot_id": market_id, "state_unchanged": True,
            "authority": _authority(),
        }
    quotes = market_day["quotes"]
    ledger: list[dict[str, Any]] = []
    unfilled: list[dict[str, Any]] = []
    carried: list[dict[str, Any]] = []

    def apply_costs(event: dict[str, Any]) -> None:
        for key in _COST_KEYS:
            state["cumulative_costs"][key] += float(event["costs"][key])

    for order in state["pending_exit_orders"]:
        valid_date = _date_text(order.get("valid_trade_date"))
        order_errors = _exit_order_errors(
            order, trade_date=trade_date, newly_submitted=False,
        )
        if order_errors:
            return {
                "status": "execution_input_invalid", "executed": False,
                "errors": sorted(set(order_errors)),
                "previous_state_id": prior_state_id,
                "decision_packet_ids": sorted(decision_ids),
                "market_snapshot_id": market_id, "state_unchanged": True,
                "authority": _authority(),
            }
        if valid_date > trade_date:
            carried.append(dict(order))
            continue
        event = _execute_opening_order(
            state, order, quotes.get(str(order.get("stock_code") or "")),
            trade_date, config,
        )
        filled = int(event["filled_shares"])
        if filled > 0:
            code = event["stock_code"]
            position = state["positions"][code]
            old_total = int(position["shares_total"])
            released_book_cost = position["book_cost_yuan"] * filled / old_total
            position["shares_total"] -= filled
            position["sellable_shares"] -= filled
            position["book_cost_yuan"] -= released_book_cost
            state["cash_yuan"] += event["notional_yuan"] - event["costs"]["total"]
            state["cumulative_sell_notional"] += event["notional_yuan"]
            apply_costs(event)
            event["realized_pnl_yuan"] = (
                event["notional_yuan"] - event["costs"]["total"] - released_book_cost
            )
            if position["shares_total"] == 0:
                del state["positions"][code]
            ledger.append(event)
        if filled < int(event.get("requested_shares") or 0):
            carry = dict(order)
            carry["requested_shares"] = int(event.get("requested_shares") or 0) - filled
            carried.append(carry)
            unfilled.append(event)

    for order in sorted(
        entry_orders,
        key=lambda row: (str(row.get("stock_code") or ""), str(row.get("order_id") or "")),
    ):
        if (
            order.get("side") != "buy"
            or order.get("order_session") != "next_trading_day_opening_auction"
            or order.get("order_type") != "limit"
            or order.get("time_in_force") != "opening_only"
        ):
            unfilled.append({
                "order_id": order.get("order_id"), "stock_code": order.get("stock_code"),
                "side": "buy", "requested_shares": order.get("requested_shares"),
                "filled_shares": 0, "status": "unfilled",
                "reason": "entry_order_contract_invalid",
            })
            continue
        event = _execute_opening_order(
            state, order, quotes.get(str(order.get("stock_code") or "")),
            trade_date, config,
        )
        filled = int(event["filled_shares"])
        if filled > 0:
            code = event["stock_code"]
            position = state["positions"].setdefault(code, {
                "shares_total": 0, "sellable_shares": 0, "locked_buys": [],
                "book_cost_yuan": 0.0,
            })
            position["shares_total"] += filled
            position["locked_buys"].append({
                "buy_trade_date": trade_date, "shares": filled,
            })
            position["locked_buys"] = sorted(
                position["locked_buys"],
                key=lambda row: (row["buy_trade_date"], row["shares"]),
            )
            position["book_cost_yuan"] += event["notional_yuan"] + event["costs"]["total"]
            state["cash_yuan"] -= event["notional_yuan"] + event["costs"]["total"]
            state["cumulative_buy_notional"] += event["notional_yuan"]
            apply_costs(event)
            ledger.append(event)
        if filled < int(event.get("requested_shares") or 0):
            unfilled.append(event)

    state["pending_exit_orders"] = sorted(
        carried, key=lambda row: str(row.get("order_id") or ""),
    )
    state["last_trade_date"] = trade_date
    state["previous_state_id"] = prior_state_id
    state["cash_yuan"] = float(state["cash_yuan"])
    state["state_id"] = _identity(state, "state_id")
    packet = {
        "schema": "unified_regime_route_execution_day.v1",
        "trade_date": trade_date, "previous_state_id": prior_state_id,
        "decision_packet_ids": sorted(decision_ids),
        "market_snapshot_id": market_id, "no_backfill": True,
        "execution_protocol_amendment": {
            "path": DAILY_EXECUTION_CONTRACT_PATH.relative_to(PROJECT_ROOT).as_posix(),
            "sha256": execution_protocol["sha256"],
        },
        "execution_config": config,
        "execution_model": {
            "session": "opening_auction",
            "fill": "daily_open_proxy_lagged_ADV_cap_limit_event_no_fill",
            "full_exchange_matching_claimed": False,
            "opening_auction_queue_or_volume_proven": False,
            "continuous_auction_price_cage_applied": False,
            "exit_source_packet_identity_independently_verified": False,
        },
        "ledger": ledger, "unfilled": unfilled, "state_after": state,
        "returns_or_labels_read": False, "authority": _authority(),
    }
    packet["packet_id"] = _identity(packet, "packet_id")
    verified = verify_joint_execution_day_packet(
        packet, previous_state_id=prior_state_id,
        decision_packet_ids=decision_ids, market_snapshot_id=market_id,
    )
    if verified["valid"] is not True:
        return {
            "status": "execution_output_invalid", "executed": False,
            "errors": verified["errors"], "state_unchanged": True,
            "authority": _authority(),
        }
    return {"status": "executed", "executed": True, "packet": packet}


def verify_joint_execution_day_packet(
    packet: dict[str, Any], *, previous_state_id: str | None = None,
    decision_packet_ids: list[str] | None = None,
    market_snapshot_id: str | None = None,
) -> dict[str, Any]:
    """Verify one immutable daily execution transition and its state hash chain."""
    errors: list[str] = []
    if packet.get("schema") != "unified_regime_route_execution_day.v1":
        errors.append("schema_invalid")
    if packet.get("packet_id") != _identity(packet, "packet_id"):
        errors.append("packet_id_invalid")
    if packet.get("no_backfill") is not True:
        errors.append("no_backfill_invalid")
    if previous_state_id is not None and packet.get("previous_state_id") != previous_state_id:
        errors.append("previous_state_id_invalid")
    if decision_packet_ids is not None and packet.get("decision_packet_ids") != sorted(
        decision_packet_ids
    ):
        errors.append("decision_packet_ids_invalid")
    if market_snapshot_id is not None and packet.get("market_snapshot_id") != market_snapshot_id:
        errors.append("market_snapshot_id_invalid")
    execution_protocol = _current_portfolio_execution_check()
    protocol_identity = packet.get("execution_protocol_amendment") or {}
    if execution_protocol["valid"] is not True:
        errors.extend(execution_protocol["errors"])
    if protocol_identity != {
        "path": DAILY_EXECUTION_CONTRACT_PATH.relative_to(PROJECT_ROOT).as_posix(),
        "sha256": execution_protocol["sha256"],
    }:
        errors.append("execution_protocol_amendment_identity_invalid")
    state = packet.get("state_after")
    if not isinstance(state, dict):
        errors.append("state_after_invalid")
    else:
        errors.extend(_portfolio_state_errors(state))
        if state.get("previous_state_id") != packet.get("previous_state_id"):
            errors.append("state_chain_invalid")
        if state.get("last_trade_date") != packet.get("trade_date"):
            errors.append("state_trade_date_mismatch")
    if packet.get("returns_or_labels_read") is not False:
        errors.append("outcome_boundary_invalid")
    model = packet.get("execution_model") or {}
    if any(model.get(key) is not False for key in (
        "full_exchange_matching_claimed", "opening_auction_queue_or_volume_proven",
        "continuous_auction_price_cage_applied",
        "exit_source_packet_identity_independently_verified",
    )):
        errors.append("execution_fidelity_claim_invalid")
    authority = packet.get("authority") or {}
    if authority.get("research_only") is not True or any(
        authority.get(key) is not False for key in (
            "changes_live_score", "changes_live_trade_count",
            "changes_live_position_or_quantity", "generates_buy_or_sell_alert",
            "production_strategy", "automatic_trading", "stable_profit_claim",
        )
    ):
        errors.append("authority_invalid")
    return {"valid": not errors, "errors": sorted(set(errors))}


def verify_replay_protocol(
    *, project_root: Path = PROJECT_ROOT, protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str | None = PROTOCOL_SHA256,
    amendment_path: Path | None = AMENDMENT_PATH,
    expected_amendment_sha256: str | None = AMENDMENT_SHA256,
    execution_amendment_path: Path | None = EXECUTION_AMENDMENT_PATH,
    expected_execution_amendment_sha256: str | None = EXECUTION_AMENDMENT_SHA256,
    execution_correction_path: Path | None = EXECUTION_CORRECTION_PATH,
    expected_execution_correction_sha256: str | None = EXECUTION_CORRECTION_SHA256,
    portfolio_execution_amendment_path: Path | None = PORTFOLIO_EXECUTION_AMENDMENT_PATH,
    router_rebind_amendment_path: Path | None = ROUTER_REBIND_AMENDMENT_PATH,
    expected_router_rebind_sha256: str | None = ROUTER_REBIND_AMENDMENT_SHA256,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        protocol_sha = _sha256(protocol_path)
        if expected_protocol_sha256 and protocol_sha != expected_protocol_sha256:
            errors.append("protocol_sha256_mismatch")
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        if protocol.get("status") != "protocol_and_first_stock_formula_ready_waiting_forward_evidence":
            errors.append("protocol_status_invalid")
        execution_amendment_sha = None
        execution_correction_sha = None
        portfolio_execution_amendment_sha = None
        superseded_bindings: set[str] = set()
        superseded_execution_amendment_bindings: set[str] = set()
        portfolio_superseded_bindings: set[str] = set()
        successor_portfolio_superseded_bindings: set[str] = set()
        if router_rebind_amendment_path is not None:
            router_rebind_sha = _sha256(router_rebind_amendment_path)
            if (
                expected_router_rebind_sha256
                and router_rebind_sha != expected_router_rebind_sha256
            ):
                errors.append("router_rebind_amendment_sha256_mismatch")
            router_rebind = json.loads(
                router_rebind_amendment_path.read_text(encoding="utf-8")
            )
            if router_rebind.get("status") != (
                "unified_market_state_budget_router_rebound_without_outcome_run"
            ):
                errors.append("router_rebind_amendment_status_invalid")
            for name, binding in (router_rebind.get("bindings") or {}).items():
                path = _project_path(binding.get("path"), project_root=project_root)
                if not path.is_file() or _sha256(path) != binding.get("sha256"):
                    errors.append(f"router_rebind_binding_invalid:{name}")
            if set(router_rebind.get("supersedes_base_bindings") or []) != {
                "strategy_router"
            }:
                errors.append("router_rebind_supersession_invalid")
            successor_portfolio_superseded_bindings = set(
                router_rebind.get("supersedes_predecessor_bindings") or []
            )
            if successor_portfolio_superseded_bindings != {
                "joint_execution_state_machine", "current_execution_engine",
            }:
                errors.append("router_rebind_predecessor_supersession_invalid")
            elif all(
                value is False
                for value in (router_rebind.get("authority") or {}).values()
            ):
                superseded_bindings.add("strategy_router")
            else:
                errors.append("router_rebind_authority_invalid")
        if execution_correction_path is not None:
            execution_correction_sha = _sha256(execution_correction_path)
            if (
                expected_execution_correction_sha256
                and execution_correction_sha != expected_execution_correction_sha256
            ):
                errors.append("execution_correction_sha256_mismatch")
            execution_correction = json.loads(
                execution_correction_path.read_text(encoding="utf-8")
            )
            if execution_correction.get("status") != (
                "integer_share_state_correction_no_outcome_run"
            ):
                errors.append("execution_correction_status_invalid")
            for name, binding in (execution_correction.get("bindings") or {}).items():
                path = _project_path(binding.get("path"), project_root=project_root)
                if (
                    (not path.is_file() or _sha256(path) != binding.get("sha256"))
                    and name not in successor_portfolio_superseded_bindings
                ):
                    errors.append(f"execution_correction_binding_invalid:{name}")
            superseded_execution_amendment_bindings = set(
                execution_correction.get("supersedes_predecessor_bindings") or []
            )
            if superseded_execution_amendment_bindings != {
                "superseding_execution_engine"
            }:
                errors.append("execution_correction_supersession_invalid")
            if any(
                value is not False
                for value in (execution_correction.get("authority") or {}).values()
            ):
                errors.append("execution_correction_authority_invalid")
        if execution_amendment_path is None:
            portfolio_execution_amendment_path = None
        if portfolio_execution_amendment_path is not None:
            portfolio_check = _portfolio_execution_amendment_check(
                project_root=project_root, path=portfolio_execution_amendment_path,
                superseded_bindings=successor_portfolio_superseded_bindings,
            )
            portfolio_execution_amendment_sha = portfolio_check["sha256"]
            errors.extend(portfolio_check["errors"])
            portfolio_superseded_bindings = set(
                portfolio_check["amendment"].get("supersedes_predecessor_bindings") or []
            )
        if execution_amendment_path is not None:
            execution_amendment_sha = _sha256(execution_amendment_path)
            if (
                expected_execution_amendment_sha256
                and execution_amendment_sha != expected_execution_amendment_sha256
            ):
                errors.append("execution_amendment_sha256_mismatch")
            execution_amendment = json.loads(
                execution_amendment_path.read_text(encoding="utf-8")
            )
            if execution_amendment.get("status") != (
                "shared_execution_contract_phase1_pre_execution_plan_not_fill_replay"
            ):
                errors.append("execution_amendment_status_invalid")
            for name, binding in (execution_amendment.get("bindings") or {}).items():
                path = _project_path(binding.get("path"), project_root=project_root)
                if (
                    (not path.is_file() or _sha256(path) != binding.get("sha256"))
                    and name not in (
                        superseded_execution_amendment_bindings
                        | portfolio_superseded_bindings
                    )
                ):
                    errors.append(f"execution_amendment_binding_invalid:{name}")
            superseded_bindings |= set(
                execution_amendment.get("supersedes_base_bindings") or []
            )
            if not {
                "existing_execution_engine", "existing_cost_model",
            } <= superseded_bindings:
                errors.append("execution_amendment_supersession_invalid")
            if any(
                value is not False
                for value in (execution_amendment.get("authority") or {}).values()
            ):
                errors.append("execution_amendment_authority_invalid")
        for name, binding in (protocol.get("bindings") or {}).items():
            path = _project_path(binding.get("path"), project_root=project_root)
            if (
                (not path.is_file() or _sha256(path) != binding.get("sha256"))
                and name not in superseded_bindings
            ):
                errors.append(f"binding_invalid:{name}")
        if protocol.get("causal_order", [])[:3] != [
            "freeze_news_policy_event_and_completed_market_inputs_available_at_decision_time",
            "classify_market_state_without_stock_future_returns",
            "set_total_exposure_and_maximum_new_trade_budget",
        ]:
            errors.append("causal_order_invalid")
        amendment_sha = None
        if amendment_path is not None:
            amendment_sha = _sha256(amendment_path)
            if expected_amendment_sha256 and amendment_sha != expected_amendment_sha256:
                errors.append("amendment_sha256_mismatch")
            amendment = json.loads(amendment_path.read_text(encoding="utf-8"))
            if amendment.get("status") != (
                "same_slot_emotion_replay_input_frozen_before_first_forward_packet"
            ):
                errors.append("amendment_status_invalid")
            for name, binding in (amendment.get("bindings") or {}).items():
                path = _project_path(binding.get("path"), project_root=project_root)
                if not path.is_file() or _sha256(path) != binding.get("sha256"):
                    errors.append(f"amendment_binding_invalid:{name}")
            extension = amendment.get("replay_input_extension") or {}
            if not all(extension.get(key) is True for key in (
                "same_decision_day_as_candidate",
                "same_observation_slot_as_candidate",
                "packet_hash_and_protocol_identity_required",
                "complete_source_capture_required",
                "confirmation_must_match_prior_market_state",
                "no_backfill",
            )):
                errors.append("amendment_emotion_contract_invalid")
            if any(value is not False for value in (amendment.get("authority") or {}).values()):
                errors.append("amendment_authority_invalid")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        protocol_sha, amendment_sha, execution_amendment_sha = None, None, None
        execution_correction_sha, portfolio_execution_amendment_sha, protocol = None, None, {}
        errors.append(f"protocol_unreadable:{exc}")
    return {
        "valid": not errors, "errors": errors, "protocol": protocol,
        "protocol_sha256": protocol_sha, "amendment_sha256": amendment_sha,
        "execution_amendment_sha256": execution_amendment_sha,
        "execution_correction_sha256": execution_correction_sha,
        "portfolio_execution_amendment_sha256": portfolio_execution_amendment_sha,
    }


def _buy_lot(cap: float, price: float, cash: float) -> tuple[int, float, float]:
    requested = min(cap, cash) / price
    shares = ashare_execution.constrain_share_quantity(
        requested, side="buy", lot_size=LOT_SIZE,
    )["accepted_shares"]
    while shares > 0:
        notional = price * shares
        fee = float(one_side_cost_breakdown(notional, "buy")["total"])
        if notional + fee <= min(cap, cash) + 1e-9:
            return shares, notional, fee
        shares -= LOT_SIZE
    return 0, 0.0, 0.0


def build_joint_replay_decision(
    route: dict[str, Any], candidate_result: dict[str, Any], account: dict[str, Any],
    protocol: dict[str, Any], *, intraday_emotion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one causal research decision; never emits a live instruction."""
    state = str(route.get("market_state") or "unknown")
    state_policy = ((protocol.get("portfolio_router") or {}).get("states") or {}).get(state)
    if not isinstance(state_policy, dict):
        state, state_policy = "unknown", ((protocol["portfolio_router"]["states"])["unknown"])
    base_exposure_pct = float(state_policy["target_exposure_pct"])
    base_maximum_new_trades = int(state_policy["maximum_new_trades"])
    eligible_playbooks = list(state_policy.get("eligible_playbooks") or [])
    emotion = intraday_emotion if isinstance(intraday_emotion, dict) else {}
    emotion_budget = emotion.get("research_budget_hypothesis") or {}
    emotion_authority = emotion.get("authority") or {}
    emotion_trade_budget = _finite(emotion_budget.get("maximum_new_trades"))
    emotion_contract_ready = bool(
        emotion.get("status") == "intraday_emotion_ready"
        and emotion.get("confirmation") == "confirmed"
        and emotion.get("market_state_being_checked") == state
        and emotion_budget.get("status") == "confirmed_base_route"
        and emotion_trade_budget == base_maximum_new_trades
        and emotion_budget.get("applied_to_execution") is False
        and emotion.get("applied_to_live_exposure") is False
        and emotion_authority.get("research_only") is True
        and emotion_authority.get("changes_live_position_or_quantity") is False
    )
    exposure_pct = base_exposure_pct if emotion_contract_ready else 0.0
    maximum_new_trades = base_maximum_new_trades if emotion_contract_ready else 0
    causal_steps = [
        {"step": 1, "name": "market_state", "value": state},
        {"step": 2, "name": "intraday_emotion_confirmation", "value": {
            "ready": emotion_contract_ready,
            "confirmation": emotion.get("confirmation") or "missing",
            "summary": emotion.get("summary"),
        }},
        {"step": 3, "name": "target_exposure_pct", "value": exposure_pct},
        {"step": 4, "name": "maximum_new_trades", "value": maximum_new_trades},
        {"step": 5, "name": "eligible_playbooks", "value": eligible_playbooks},
    ]
    factor_evidence = candidate_result.get("factor_evidence") or {}
    factor_ids = factor_evidence.get("admitted_factor_ids")
    factor_contract_ready = (
        factor_evidence.get("status") == "strategy_formula_only_no_admitted_factor"
        and factor_ids == []
        and factor_evidence.get("factor_score_applied") is False
    ) or (
        factor_evidence.get("status") == "production_factors_applied"
        and isinstance(factor_ids, list) and bool(factor_ids)
        and factor_evidence.get("factor_score_applied") is True
        and factor_evidence.get("production_gate_valid") is True
    )
    candidate_contract_ready = (
        candidate_result.get("status") == "candidate_research_ready"
        and candidate_result.get("playbook_id") in eligible_playbooks
        and bool(candidate_result.get("stock_formula_id"))
        and factor_contract_ready
        and candidate_result.get("named_person_signal_used") is False
        and candidate_result.get("returns_or_labels_read") is False
    )

    equity = _finite(account.get("equity"))
    cash = _finite(account.get("available_cash"))
    positions = account.get("positions")
    account_ready = (
        account.get("cash_user_recorded") is True
        and account.get("valuation_status") == "complete"
        and equity is not None and equity > 0 and cash is not None and cash >= 0
        and isinstance(positions, list)
    )
    existing_value = 0.0
    if account_ready:
        values = [_finite(row.get("market_value")) for row in positions if isinstance(row, dict)]
        account_ready = all(value is not None and value >= 0 for value in values)
        existing_value = sum(value or 0.0 for value in values)
    if not account_ready:
        return {
            "status": "account_input_incomplete", "market_state": state,
            "target_exposure_pct": exposure_pct,
            "maximum_new_trades": maximum_new_trades,
            "eligible_playbooks": eligible_playbooks,
            "intraday_emotion_contract_ready": emotion_contract_ready,
            "intraday_emotion": emotion,
            "candidate_contract_ready": candidate_contract_ready,
            "factor_contract_ready": factor_contract_ready,
            "causal_steps": causal_steps,
            "pre_execution_plans": [],
            "research_orders": [],
            "research_orders_semantics": "deprecated_alias_of_pre_execution_plans_not_fills",
            "executed_orders": [],
            "execution_replay_complete": False,
            "returns_or_labels_read": False,
            "authority": _authority(),
        }

    target_value = equity * exposure_pct / 100.0
    buy_capacity = min(cash, max(0.0, target_value - existing_value))
    risk_reduction = max(0.0, existing_value - target_value)
    selected = candidate_result.get("selected") or [] if candidate_contract_ready else []
    selected = selected[:maximum_new_trades]
    if maximum_new_trades <= 0:
        selected = []
    causal_steps.append({
        "step": 6, "name": "factor_and_stock_candidates",
        "value": {
            "playbook_id": candidate_result.get("playbook_id"),
            "stock_formula_id": candidate_result.get("stock_formula_id"),
            "factor_status": factor_evidence.get("status"),
            "admitted_factor_ids": factor_ids if isinstance(factor_ids, list) else [],
            "stock_codes": [row.get("stock_code") for row in selected],
        },
    })
    orders: list[dict[str, Any]] = []
    remaining_cash = buy_capacity
    if selected and buy_capacity > 0:
        per_candidate_cap = min(
            buy_capacity / len(selected),
            equity * float(protocol["small_capital_execution"]["maximum_single_stock_target_pct"]) / 100.0,
        )
        for row in selected:
            price = _finite(row.get("price"))
            if price is None or price <= 0:
                continue
            shares, notional, fee = _buy_lot(per_candidate_cap, price, remaining_cash)
            if shares <= 0:
                continue
            remaining_cash -= notional + fee
            plan = {
                "stock_code": row.get("stock_code"), "stock_name": row.get("stock_name"),
                "side": "buy",
                "reference_price": price, "shares": shares,
                "notional_yuan": round(notional, 2),
                "estimated_buy_cost_yuan": round(fee, 2),
                "status": "planned_not_executed",
                "order_session": "next_trading_day_opening_auction",
                "order_type": "limit",
                "limit_price_yuan": price,
                "price_cage_applicability": "not_applicable_to_opening_auction",
                "earliest_fill": "next_trading_day_open_if_executable",
                "execution_contract_status": "pending_next_session_tradable_open_price_limit_and_ADV_inputs",
                "live_instruction": False,
            }
            plan["plan_id"] = hashlib.sha256(_canonical_bytes(plan)).hexdigest()
            orders.append(plan)
    causal_steps.append({
        "step": 7, "name": "account_cash_positions_and_lots",
        "value": {"existing_market_value": existing_value, "buy_capacity": buy_capacity},
    })
    return {
        "status": (
            "research_replay_decision_ready"
            if emotion_contract_ready else "intraday_emotion_not_confirmed"
        ),
        "market_state": state, "target_exposure_pct": exposure_pct,
        "base_target_exposure_pct": base_exposure_pct,
        "maximum_new_trades": maximum_new_trades,
        "base_maximum_new_trades": base_maximum_new_trades,
        "intraday_emotion_contract_ready": emotion_contract_ready,
        "intraday_emotion": emotion,
        "eligible_playbooks": eligible_playbooks,
        "candidate_contract_ready": candidate_contract_ready,
        "factor_contract_ready": factor_contract_ready,
        "factor_evidence": {
            "status": factor_evidence.get("status"),
            "admitted_factor_ids": factor_ids if isinstance(factor_ids, list) else [],
            "factor_score_applied": factor_evidence.get("factor_score_applied") is True,
        },
        "existing_exposure_pct": round(existing_value / equity * 100, 6),
        "target_market_value_yuan": round(target_value, 2),
        "new_buy_capacity_yuan": round(buy_capacity, 2),
        "risk_reduction_review_yuan": round(risk_reduction, 2),
        "risk_reduction_quantity_status": (
            "not_required" if risk_reduction <= 0
            else "position_shares_sellable_inventory_and_execution_price_required"
        ),
        "pre_execution_plans": orders,
        "research_orders": orders,
        "research_orders_semantics": "deprecated_alias_of_pre_execution_plans_not_fills",
        "executed_orders": [],
        "execution_replay_complete": False,
        "remaining_unallocated_buy_capacity_yuan": round(remaining_cash, 2),
        "causal_steps": causal_steps,
        "returns_or_labels_read": False,
        "authority": _authority(),
    }


def _authority() -> dict[str, bool]:
    return {
        "research_only": True, "changes_live_score": False,
        "changes_live_trade_count": False,
        "changes_live_position_or_quantity": False,
        "generates_buy_or_sell_alert": False, "production_strategy": False,
        "automatic_trading": False, "stable_profit_claim": False,
    }


def verify_joint_replay_packet(
    path: Path, *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        resolved = path.resolve()
        resolved.relative_to(project_root.resolve())
        packet = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"packet_unreadable:{exc}"]}
    identity = dict(packet)
    packet_id = str(identity.pop("packet_id", ""))
    if packet.get("schema") != "unified_regime_route_replay.v1":
        errors.append("schema_invalid")
    if hashlib.sha256(_canonical_bytes(identity)).hexdigest() != packet_id:
        errors.append("packet_id_invalid")
    if packet.get("no_backfill") is not True:
        errors.append("no_backfill_invalid")
    if packet.get("returns_or_labels_read") is not False:
        errors.append("outcome_boundary_invalid")
    bound_packets: dict[str, dict[str, Any]] = {}
    for key in (
        "protocol", "candidate_packet", "shadow_decision",
        "intraday_emotion_input",
    ):
        binding = packet.get(key) or {}
        try:
            target = _project_path(binding.get("path"), project_root=project_root)
            if not target.is_file() or _sha256(target) != binding.get("sha256"):
                errors.append(f"binding_invalid:{key}")
                continue
            if key != "protocol":
                bound_packet = json.loads(target.read_text(encoding="utf-8"))
                bound_packets[key] = bound_packet
                if bound_packet.get("packet_id") != binding.get("packet_id"):
                    errors.append(f"binding_packet_id_invalid:{key}")
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            errors.append(f"binding_invalid:{key}")
    amendment = packet.get("protocol_amendment") or {}
    try:
        amendment_target = _project_path(
            amendment.get("path"), project_root=project_root,
        )
        if (
            not amendment_target.is_file()
            or _sha256(amendment_target) != amendment.get("sha256")
        ):
            errors.append("binding_invalid:protocol_amendment")
    except (OSError, TypeError, ValueError):
        errors.append("binding_invalid:protocol_amendment")
    execution_amendment = packet.get("execution_protocol_amendment")
    if isinstance(execution_amendment, dict) and execution_amendment.get("path"):
        try:
            execution_amendment_target = _project_path(
                execution_amendment.get("path"), project_root=project_root,
            )
            if (
                not execution_amendment_target.is_file()
                or _sha256(execution_amendment_target)
                != execution_amendment.get("sha256")
            ):
                errors.append("binding_invalid:execution_protocol_amendment")
        except (OSError, TypeError, ValueError):
            errors.append("binding_invalid:execution_protocol_amendment")
    execution_correction = packet.get("execution_protocol_correction")
    if isinstance(execution_correction, dict) and execution_correction.get("path"):
        try:
            correction_target = _project_path(
                execution_correction.get("path"), project_root=project_root,
            )
            if (
                not correction_target.is_file()
                or _sha256(correction_target) != execution_correction.get("sha256")
            ):
                errors.append("binding_invalid:execution_protocol_correction")
        except (OSError, TypeError, ValueError):
            errors.append("binding_invalid:execution_protocol_correction")
    candidate = bound_packets.get("candidate_packet") or {}
    shadow = bound_packets.get("shadow_decision") or {}
    emotion = bound_packets.get("intraday_emotion_input") or {}
    decision_identity = (candidate.get("decision_day"), candidate.get("slot_id"))
    if decision_identity != (shadow.get("decision_day"), shadow.get("slot_id")) or (
        decision_identity != (emotion.get("decision_day"), emotion.get("slot_id"))
    ):
        errors.append("decision_day_or_slot_mismatch")
    snapshot = packet.get("account_snapshot") or {}
    replay_inputs = snapshot.get("replay_inputs")
    if not isinstance(replay_inputs, dict) or hashlib.sha256(
        _canonical_bytes(replay_inputs)
    ).hexdigest() != snapshot.get("replay_inputs_sha256"):
        errors.append("account_replay_inputs_invalid")
    decision = packet.get("decision") or {}
    if decision.get("returns_or_labels_read") is not False:
        errors.append("decision_outcome_boundary_invalid")
    if (decision.get("authority") or {}).get("research_only") is not True:
        errors.append("decision_authority_invalid")
    authority = packet.get("authority") or {}
    if authority.get("research_only") is not True or any(
        authority.get(key) is not False for key in (
            "changes_live_score", "changes_live_trade_count",
            "changes_live_position_or_quantity", "generates_buy_or_sell_alert",
            "production_strategy", "automatic_trading", "stable_profit_claim",
        )
    ):
        errors.append("authority_invalid")
    return {"valid": not errors, "errors": errors, "packet": packet, "packet_id": packet_id}


def capture_joint_replay(
    candidate_capture: dict[str, Any], intraday_emotion_capture: dict[str, Any],
    account_snapshot: dict[str, Any], *,
    project_root: Path = PROJECT_ROOT, protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str | None = PROTOCOL_SHA256,
    amendment_path: Path | None = AMENDMENT_PATH,
    expected_amendment_sha256: str | None = AMENDMENT_SHA256,
    execution_amendment_path: Path | None = EXECUTION_AMENDMENT_PATH,
    expected_execution_amendment_sha256: str | None = EXECUTION_AMENDMENT_SHA256,
    execution_correction_path: Path | None = EXECUTION_CORRECTION_PATH,
    expected_execution_correction_sha256: str | None = EXECUTION_CORRECTION_SHA256,
    router_rebind_amendment_path: Path | None = ROUTER_REBIND_AMENDMENT_PATH,
    expected_router_rebind_sha256: str | None = ROUTER_REBIND_AMENDMENT_SHA256,
    packet_dir: Path = PACKET_DIR,
    candidate_verifier: Callable[..., dict[str, Any]] = verify_candidate_packet,
    shadow_verifier: Callable[..., dict[str, Any]] = verify_shadow_decision_packet,
    emotion_verifier: Callable[..., dict[str, Any]] = verify_intraday_emotion_packet,
) -> dict[str, Any]:
    """Persist one immutable regime-first research replay after candidate capture."""
    protocol_check = verify_replay_protocol(
        project_root=project_root, protocol_path=protocol_path,
        expected_protocol_sha256=expected_protocol_sha256,
        amendment_path=amendment_path,
        expected_amendment_sha256=expected_amendment_sha256,
        execution_amendment_path=execution_amendment_path,
        expected_execution_amendment_sha256=expected_execution_amendment_sha256,
        execution_correction_path=execution_correction_path,
        expected_execution_correction_sha256=expected_execution_correction_sha256,
        router_rebind_amendment_path=router_rebind_amendment_path,
        expected_router_rebind_sha256=expected_router_rebind_sha256,
    )
    if protocol_check.get("valid") is not True:
        return {"status": "identity_invalid", "captured": False, "errors": protocol_check["errors"]}
    try:
        candidate_path = _project_path(candidate_capture.get("path"), project_root=project_root)
        candidate_check = candidate_verifier(candidate_path, project_root=project_root)
        candidate_packet = candidate_check.get("packet") or {}
        for key in ("formula", "constituent_packet", "shadow_decision"):
            binding = candidate_packet.get(key) or {}
            target = _project_path(binding.get("path"), project_root=project_root)
            if not target.is_file() or _sha256(target) != binding.get("sha256"):
                raise ValueError(f"candidate_binding_invalid:{key}")
        shadow_binding = candidate_packet["shadow_decision"]
        shadow_path = _project_path(shadow_binding["path"], project_root=project_root)
        shadow_check = shadow_verifier(shadow_path, project_root=project_root)
        emotion_path = _project_path(
            intraday_emotion_capture.get("path"), project_root=project_root,
        )
        emotion_check = emotion_verifier(emotion_path, project_root=project_root)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        return {"status": "input_invalid", "captured": False, "errors": [str(exc)]}
    if (
        candidate_check.get("valid") is not True
        or shadow_check.get("valid") is not True
        or emotion_check.get("valid") is not True
    ):
        return {
            "status": "input_invalid", "captured": False,
            "errors": (
                (candidate_check.get("errors") or [])
                + (shadow_check.get("errors") or [])
                + (emotion_check.get("errors") or [])
            ),
        }
    emotion_packet = emotion_check["packet"]
    if (
        (candidate_packet.get("decision_day"), candidate_packet.get("slot_id"))
        != (emotion_packet.get("decision_day"), emotion_packet.get("slot_id"))
        or emotion_packet.get("source_capture_status") != "complete"
    ):
        return {"status": "intraday_emotion_input_invalid", "captured": False}
    account = {
        "cash_user_recorded": account_snapshot.get("cash_user_recorded") is True,
        "valuation_status": account_snapshot.get("valuation_status"),
        "equity": account_snapshot.get("account_equity"),
        "available_cash": account_snapshot.get("available_cash"),
        "positions": [
            {
                "code": row.get("code"), "market_value": row.get("market_value"),
                "shares": row.get("shares", row.get("quantity")),
                "available_to_sell_shares": row.get(
                    "available_to_sell_shares", row.get("available_quantity"),
                ),
            }
            for row in (account_snapshot.get("positions") or [])
            if isinstance(row, dict)
        ],
    }
    route = shadow_check["packet"].get("route") or {}
    captured_at = emotion_packet.get("captured_at")
    emotion_observation = build_intraday_emotion_observation(
        route, {"breadth": emotion_packet.get("raw_observation") or {}},
        now=None if not captured_at else datetime.fromisoformat(captured_at),
    )
    decision = build_joint_replay_decision(
        route,
        candidate_packet.get("research_result") or {}, account,
        protocol_check["protocol"], intraday_emotion=emotion_observation,
    )
    packet = {
        "schema": "unified_regime_route_replay.v1",
        "decision_day": candidate_packet.get("decision_day"),
        "slot_id": candidate_packet.get("slot_id"),
        "no_backfill": True,
        "protocol": {
            "path": str(protocol_path.resolve().relative_to(project_root.resolve())),
            "sha256": protocol_check["protocol_sha256"],
        },
        "protocol_amendment": {
            "path": (
                str(amendment_path.resolve().relative_to(project_root.resolve()))
                if amendment_path is not None else None
            ),
            "sha256": protocol_check.get("amendment_sha256"),
        },
        "execution_protocol_amendment": {
            "path": (
                str(execution_amendment_path.resolve().relative_to(project_root.resolve()))
                if execution_amendment_path is not None else None
            ),
            "sha256": protocol_check.get("execution_amendment_sha256"),
        },
        "execution_protocol_correction": {
            "path": (
                str(execution_correction_path.resolve().relative_to(project_root.resolve()))
                if execution_correction_path is not None else None
            ),
            "sha256": protocol_check.get("execution_correction_sha256"),
        },
        "candidate_packet": {
            "path": str(candidate_path.relative_to(project_root.resolve())),
            "sha256": _sha256(candidate_path), "packet_id": candidate_check.get("packet_id"),
        },
        "shadow_decision": {
            "path": str(shadow_path.relative_to(project_root.resolve())),
            "sha256": _sha256(shadow_path), "packet_id": shadow_check.get("packet_id"),
        },
        "intraday_emotion_input": {
            "path": str(emotion_path.relative_to(project_root.resolve())),
            "sha256": _sha256(emotion_path),
            "packet_id": emotion_check.get("packet_id"),
        },
        "account_snapshot": {
            "identity": account_snapshot.get("immutable_account_snapshot_sha256"),
            "status": account_snapshot.get("account_snapshot_status"),
            "replay_inputs": account,
            "replay_inputs_sha256": hashlib.sha256(_canonical_bytes(account)).hexdigest(),
        },
        "decision": decision,
        "returns_or_labels_read": False,
        "authority": _authority(),
    }
    packet["packet_id"] = hashlib.sha256(_canonical_bytes(packet)).hexdigest()
    path = packet_dir / f"{packet['decision_day']}-{packet['slot_id']}.json"
    if path.exists():
        verified = verify_joint_replay_packet(path, project_root=project_root)
        return {
            "status": "already_captured" if verified.get("valid") else "existing_packet_invalid",
            "captured": verified.get("valid") is True, "path": str(path),
            "packet_id": verified.get("packet_id"), "errors": verified.get("errors") or [],
        }
    _atomic_publish(path, _canonical_bytes(packet))
    verified = verify_joint_replay_packet(path, project_root=project_root)
    if verified.get("valid") is not True:
        raise RuntimeError("joint replay packet failed post-write verification")
    return {
        "status": "captured", "captured": True, "path": str(path),
        "packet_id": packet["packet_id"], "decision_status": decision["status"],
        "changes_live_trade_count_or_position": False,
    }


__all__ = [
    "build_joint_replay_decision", "capture_joint_replay",
    "verify_joint_replay_packet", "verify_replay_protocol",
    "replay_joint_execution_day", "verify_joint_execution_day_packet",
]
