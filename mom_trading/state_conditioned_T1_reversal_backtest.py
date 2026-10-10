"""Causal one-completed-session replay for reversal V1, with no file I/O."""

from __future__ import annotations

import copy
import hashlib
from typing import Any, Iterable

import pandas as pd

from quantqc.costs import LOT_SIZE

from .state_conditioned_T1_reversal import build_daily_candidates, verify_protocol_chain
from .state_conditioned_mainline_backtest import (
    BASE_EXECUTION_CONFIG,
    INITIAL_CAPITAL_YUAN,
    MAX_CONCURRENT_NAMES,
    MAX_SINGLE_NAME_PCT,
    _daily_metrics,
    _decision_packet,
    _exit_order,
    _identity,
    _market_day,
    _portfolio_mark,
    _rows_by_code,
    _tick_floor,
    initial_portfolio_state,
)
from .unified_regime_route_replay import replay_joint_execution_day


def _entry_plans(
    selected: list[dict[str, Any]], *, decision_day: str,
    equity: float, gross: float, cash: float,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    capacity = max(0.0, equity * 0.40 - gross)
    if not selected or capacity <= 0 or cash <= 0:
        return [], {}
    per_name = min(capacity / len(selected), equity * MAX_SINGLE_NAME_PCT / 100.0)
    remaining_cash = float(cash)
    plans: list[dict[str, Any]] = []
    metadata: dict[str, dict[str, Any]] = {}
    for row in selected:
        close = float(row["close"])
        limit_price = _tick_floor(close)
        shares = int(min(per_name, remaining_cash) / (limit_price * LOT_SIZE)) * LOT_SIZE
        if shares <= 0:
            continue
        code = str(row.get("code") or row.get("ts_code") or "").split(".")[0].zfill(6)
        plan_id = hashlib.sha256(
            f"T1-reversal-entry:{decision_day}:{code}".encode("utf-8")
        ).hexdigest()
        plans.append({
            "plan_id": plan_id, "stock_code": code, "side": "buy",
            "shares": shares, "limit_price_yuan": limit_price,
            "order_session": "next_trading_day_opening_auction",
            "order_type": "limit", "status": "planned_not_executed",
        })
        metadata[code] = {
            "stable_instrument_id": str(row["stable_instrument_id"]),
            "decision_day": decision_day, "industry_code": row.get("industry_code"),
            "industry_name": row.get("industry_name"),
        }
        remaining_cash -= shares * limit_price
    return plans, metadata


def replay_daily_path(
    days: Iterable[dict[str, Any]], *, route_mode: str = "state_routed",
    initial_capital: float = INITIAL_CAPITAL_YUAN,
    execution_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if route_mode not in {"state_routed", "fixed_exposure_control"}:
        raise ValueError("route_mode invalid")
    protocol = verify_protocol_chain()
    if not protocol["valid"]:
        return {"status": "protocol_invalid", "errors": protocol["errors"]}
    iterator = iter(days)
    try:
        day = next(iterator)
    except StopIteration:
        return {"status": "input_empty", "errors": ["daily_path_empty"]}
    trade_date = str(day.get("trade_date") or "")
    previous_trade_date = str(day.get("previous_trade_date") or "")
    if not trade_date or not previous_trade_date or previous_trade_date >= trade_date:
        return {"status": "input_invalid", "errors": ["first_previous_trade_date_invalid"]}
    config = dict(BASE_EXECUTION_CONFIG)
    if execution_config:
        config.update(execution_config)
    state = initial_portfolio_state(previous_trade_date, initial_capital)
    trackers: dict[str, dict[str, Any]] = {}
    pending_decision: dict[str, Any] | None = None
    pending_entry_meta: dict[str, dict[str, Any]] = {}
    scheduled_exits: list[dict[str, Any]] = []
    previous_rows: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []
    completed_trades: list[dict[str, Any]] = []
    open_trade_pnl: dict[str, float] = {}
    unfilled_entries = delayed_exits = 0

    while True:
        try:
            next_day = next(iterator)
        except StopIteration:
            next_day = None
        next_trade_date = str((next_day or {}).get("trade_date") or "")
        if next_day is not None and (not next_trade_date or next_trade_date <= trade_date):
            return {"status": "input_invalid", "errors": ["trade_dates_not_unique_and_sorted"]}
        frame = day.get("rows")
        if not isinstance(frame, pd.DataFrame):
            return {"status": "input_invalid", "errors": [f"rows_not_dataframe:{trade_date}"]}
        current_rows = _rows_by_code(frame)
        entry_codes = {
            str(plan.get("stock_code") or "")
            for plan in (((pending_decision or {}).get("decision") or {}).get("pre_execution_plans") or [])
        }
        order_codes = entry_codes | set(state["positions"]) | {
            str(row.get("stock_code") or "")
            for row in list(state.get("pending_exit_orders") or []) + scheduled_exits
        }
        market = _market_day(
            trade_date, str(state["last_trade_date"]), current_rows, previous_rows, order_codes,
        )
        result = replay_joint_execution_day(
            state, [pending_decision] if pending_decision else [], market,
            scheduled_exits, execution_config=config,
        )
        if result.get("executed") is not True:
            return {
                "status": "execution_failed", "trade_date": trade_date,
                "errors": result.get("errors") or ["execution_failed"],
            }
        packet = result["packet"]
        order_meta: dict[str, dict[str, Any]] = {}
        for plan in (((pending_decision or {}).get("decision") or {}).get("pre_execution_plans") or []):
            plan_id = str(plan.get("plan_id") or "")
            order_id = hashlib.sha256(
                f"{(pending_decision or {}).get('packet_id')}:{plan_id}".encode("utf-8")
            ).hexdigest()
            order_meta[order_id] = {
                "playbook_id": "state_conditioned_T1_overreaction_reversal_v1",
                "decision_day": (pending_decision or {}).get("decision_day"),
                "strategy_event": "entry",
            }
        for order in list(state.get("pending_exit_orders") or []) + scheduled_exits:
            order_meta[str(order.get("order_id") or "")] = {
                "playbook_id": "state_conditioned_T1_overreaction_reversal_v1",
                "decision_day": str(order.get("source_packet_id") or "").removeprefix("close:"),
                "strategy_event": "one_session_exit",
            }
        state = packet["state_after"]
        scheduled_exits = []
        for raw_event in packet["ledger"]:
            event = {
                **raw_event, **order_meta.get(str(raw_event.get("order_id") or ""), {}),
                "trade_date": trade_date,
            }
            ledger.append(event)
            code = event["stock_code"]
            if event["side"] == "buy":
                trackers[code] = {
                    **copy.deepcopy(pending_entry_meta.get(code) or {}),
                    "entry_trade_date": trade_date, "holding_sessions": 0,
                }
                open_trade_pnl[code] = 0.0
            else:
                open_trade_pnl[code] = open_trade_pnl.get(code, 0.0) + float(
                    event.get("realized_pnl_yuan") or 0.0
                )
                if code not in state["positions"]:
                    completed_trades.append({
                        "stock_code": code, "exit_trade_date": trade_date,
                        "realized_pnl_yuan": open_trade_pnl.pop(code, 0.0),
                    })
                    trackers.pop(code, None)
        for event in packet["unfilled"]:
            if event.get("side") == "buy":
                unfilled_entries += 1
            elif event.get("side") == "sell":
                delayed_exits += 1

        equity, gross, _ = _portfolio_mark(state, current_rows)
        sector_values: dict[str, float] = {}
        for code, position in state["positions"].items():
            close = float(current_rows[code]["close"])
            sector = str((trackers.get(code) or {}).get("industry_code") or "UNKNOWN")
            sector_values[sector] = sector_values.get(sector, 0.0) + int(position["shares_total"]) * close
        records.append({
            "trade_date": trade_date,
            "market_state": str(day.get("market_state") or "UNKNOWN"),
            "equity": equity, "cash_yuan": float(state["cash_yuan"]),
            "exposure_pct": 0.0 if equity <= 0 else gross / equity * 100.0,
            "cash_weight_pct": 0.0 if equity <= 0 else state["cash_yuan"] / equity * 100.0,
            "positions": len(state["positions"]),
            "largest_sector_share_of_gross": 0.0 if gross <= 0 else max(sector_values.values(), default=0.0) / gross,
        })
        pending_decision = None
        pending_entry_meta = {}
        if next_day is None:
            break

        for code, position in sorted(state["positions"].items()):
            tracker = trackers.get(code)
            close = float((current_rows.get(code) or {})["close"])
            if tracker is None:
                continue
            tracker["holding_sessions"] = int(tracker.get("holding_sessions", 0)) + 1
            existing = next((
                row for row in state.get("pending_exit_orders") or []
                if str(row.get("stock_code") or "") == code
            ), None)
            if tracker["holding_sessions"] >= 1 and existing is None:
                scheduled_exits.append(_exit_order(
                    code=code, shares=int(position["shares_total"]),
                    decision_day=trade_date, valid_trade_date=next_trade_date,
                    prev_close=close, reason="time", full=True, priority="01_one_session",
                ))
        blocked = bool(scheduled_exits or state.get("pending_exit_orders"))
        excluded = {
            str(tracker.get("stable_instrument_id") or "") for tracker in trackers.values()
        }
        slots = 0 if blocked else max(0, MAX_CONCURRENT_NAMES - len(state["positions"]))
        candidates = build_daily_candidates(
            str(day.get("market_state") or "UNKNOWN"), frame,
            route_mode=route_mode, excluded_instrument_ids=excluded,
            available_new_slots=slots,
        )
        plans, pending_entry_meta = _entry_plans(
            candidates.get("selected") or [], decision_day=trade_date,
            equity=equity, gross=gross, cash=float(state["cash_yuan"]),
        )
        pending_decision = _decision_packet(
            trade_date, plans, int(candidates.get("maximum_new_trades") or 0),
        )
        previous_rows = current_rows
        day, trade_date = next_day, next_trade_date

    metrics = _daily_metrics(
        records, completed_trades, state, initial_capital,
        unfilled_entries, delayed_exits,
    )
    return {
        "schema": "state_conditioned_T1_reversal_replay.v1",
        "status": "replay_complete", "route_mode": route_mode,
        "records": records, "ledger": ledger, "completed_trades": completed_trades,
        "metrics": metrics, "unfilled_entries": unfilled_entries,
        "delayed_exits": delayed_exits, "final_state": state, "trackers": trackers,
        "returns_or_labels_read": True,
        "authority": {
            "research_only": True, "changes_live_score": False,
            "changes_live_position_or_quantity": False,
            "generates_buy_or_sell_alert": False, "production_strategy": False,
            "automatic_trading": False, "stable_profit_claim": False,
        },
    }


__all__ = ["replay_daily_path"]
