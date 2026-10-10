"""Causal daily replay for the first state-routed A-share playbook.

The module owns no data loading and has no live authority.  A caller supplies
completed daily cross-sections in chronological order; fills and costs are
delegated to the existing shared A-share execution engine.
"""

from __future__ import annotations

import copy
import hashlib
import math
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Iterable

import numpy as np
import pandas as pd

from quantqc import ashare
from quantqc.costs import LOT_SIZE

from .market_state_daily import _canonical_bytes
from .state_conditioned_mainline_swing import (
    build_daily_candidates,
    verify_protocol_chain,
)
from .unified_regime_route_replay import replay_joint_execution_day


INITIAL_CAPITAL_YUAN = 150_000.0
MAX_SINGLE_NAME_PCT = 20.0
MAX_CONCURRENT_NAMES = 5
STATE_CEILINGS = {
    "UNKNOWN": 0.0,
    "RISK_OFF": 20.0,
    "STABILIZING": 40.0,
    "RISK_ON": 80.0,
    "OVERHEATED": 40.0,
}
BASE_EXECUTION_CONFIG = {
    "max_adv_participation": 0.05,
    "impact_coefficient_bps": 25.0,
    "slippage": 0.001,
}


def _identity(payload: dict[str, Any], field: str) -> str:
    body = copy.deepcopy(payload)
    body.pop(field, None)
    return hashlib.sha256(_canonical_bytes(body)).hexdigest()


def _code(value: object) -> str:
    return str(value or "").split(".")[0].zfill(6)


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _tick_floor(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_FLOOR))


def _daily_limit(prev_close: float, code: str, side: str) -> float:
    rate = 0.20 if ashare.board_of(code) == "gemb" else 0.10
    multiplier = Decimal("1") + (Decimal(str(rate)) if side == "up" else -Decimal(str(rate)))
    return float(
        (Decimal(str(prev_close)) * multiplier).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP,
        )
    )


def initial_portfolio_state(previous_trade_date: str, capital: float = INITIAL_CAPITAL_YUAN) -> dict[str, Any]:
    state = {
        "schema": "joint_execution_portfolio_state.v1",
        "last_trade_date": str(previous_trade_date),
        "cash_yuan": float(capital),
        "cost_model_id": "quantqc_costs_v1_default_assumption",
        "positions": {},
        "pending_exit_orders": [],
        "cumulative_costs": {
            "commission": 0.0, "transfer": 0.0, "stamp": 0.0,
            "slippage": 0.0, "market_impact": 0.0, "total": 0.0,
        },
        "cumulative_buy_notional": 0.0,
        "cumulative_sell_notional": 0.0,
    }
    state["state_id"] = _identity(state, "state_id")
    return state


def _rows_by_code(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in frame.to_dict("records"):
        code = _code(row.get("code") or row.get("ts_code"))
        if code:
            rows[code] = row
    return rows


def _quote(current: dict[str, Any] | None, previous: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(current, dict) or not isinstance(previous, dict):
        return None
    open_price = _finite(current.get("open"))
    prev_close = _finite(current.get("prev_close"))
    lagged_adv = _finite(previous.get("adv20"))
    history = int(_finite(current.get("history_sessions")) or 0)
    trade_status = int(_finite(current.get("trade_status")) or 0)
    factor_changed = current.get("factor_changed") is True
    trusted = current.get("adjustment_trusted") is True
    is_st = int(_finite(current.get("is_st")) or 0) == 1
    tradable = bool(trade_status == 1 and trusted and not factor_changed)
    no_limit = bool(
        tradable and not is_st and current.get("limit_evaluable") is not True
        and (history <= 5 or not factor_changed)
    )
    return {
        "open_price": open_price, "prev_close": prev_close,
        "lagged_adv_yuan": lagged_adv, "tradable": tradable,
        "is_risk_warning": is_st, "no_price_limit": no_limit,
    }


def _market_day(
    trade_date: str, previous_trade_date: str, current: dict[str, dict[str, Any]],
    previous: dict[str, dict[str, Any]], codes: set[str],
) -> dict[str, Any]:
    market = {
        "schema": "joint_execution_market_day.v1",
        "trade_date": trade_date,
        "previous_trade_date": previous_trade_date,
        "no_backfill": True,
        "quotes": {
            code: quote for code in sorted(codes)
            if (quote := _quote(current.get(code), previous.get(code))) is not None
        },
    }
    market["market_snapshot_id"] = _identity(market, "market_snapshot_id")
    return market


def _decision_packet(day: str, plans: list[dict[str, Any]], maximum: int) -> dict[str, Any]:
    packet = {
        "schema": "state_conditioned_mainline_execution_decision.v1",
        "decision_day": day,
        "decision": {
            "maximum_new_trades": int(maximum),
            "pre_execution_plans": copy.deepcopy(plans),
            "execution_replay_complete": False,
            "returns_or_labels_read": False,
            "authority": {"research_only": True},
        },
    }
    packet["packet_id"] = _identity(packet, "packet_id")
    return packet


def _portfolio_mark(
    state: dict[str, Any], close_rows: dict[str, dict[str, Any]],
) -> tuple[float, float, dict[str, float]]:
    values: dict[str, float] = {}
    for code, position in state["positions"].items():
        close = _finite((close_rows.get(code) or {}).get("close"))
        if close is None or close <= 0:
            raise ValueError(f"close_missing_for_position:{code}")
        values[code] = int(position["shares_total"]) * close
    gross = float(sum(values.values()))
    equity = float(state["cash_yuan"] + gross)
    return equity, gross, values


def _entry_plans(
    selected: list[dict[str, Any]], *, decision_day: str, next_trade_date: str,
    equity: float, gross: float, cash: float, ceiling_pct: float,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    remaining = max(0.0, equity * ceiling_pct / 100.0 - gross)
    if not selected or remaining <= 0 or cash <= 0:
        return [], {}
    per_name = min(remaining / len(selected), equity * MAX_SINGLE_NAME_PCT / 100.0)
    plans: list[dict[str, Any]] = []
    metadata: dict[str, dict[str, Any]] = {}
    remaining_cash = float(cash)
    for row in selected:
        close = _finite(row.get("close"))
        atr = _finite(row.get("atr14"))
        if close is None or close <= 0 or atr is None or atr <= 0:
            continue
        code = _code(row.get("code") or row.get("ts_code"))
        limit_price = min(_tick_floor(close * 1.02), _daily_limit(close, code, "up"))
        shares = int(min(per_name, remaining_cash) / (limit_price * LOT_SIZE)) * LOT_SIZE
        if shares <= 0:
            continue
        plan_id = hashlib.sha256(
            f"entry:{decision_day}:{next_trade_date}:{code}".encode("utf-8")
        ).hexdigest()
        plans.append({
            "plan_id": plan_id, "stock_code": code, "side": "buy",
            "shares": shares, "limit_price_yuan": limit_price,
            "order_session": "next_trading_day_opening_auction",
            "order_type": "limit", "status": "planned_not_executed",
        })
        metadata[code] = {
            "stable_instrument_id": str(row["stable_instrument_id"]),
            "decision_day": decision_day, "atr14_at_decision": atr,
            "industry_code": row.get("industry_code"),
            "industry_name": row.get("industry_name"),
        }
        remaining_cash -= shares * limit_price
    return plans, metadata


def _exit_order(
    *, code: str, shares: int, decision_day: str, valid_trade_date: str,
    prev_close: float, reason: str, full: bool, priority: str,
) -> dict[str, Any]:
    order_id = hashlib.sha256(
        f"exit:{decision_day}:{valid_trade_date}:{code}:{reason}:{shares}".encode("utf-8")
    ).hexdigest()
    return {
        "order_id": order_id, "source_packet_id": f"close:{decision_day}",
        "stock_code": code, "side": "sell", "requested_shares": int(shares),
        "limit_price_yuan": _daily_limit(prev_close, code, "down"),
        "valid_trade_date": valid_trade_date,
        "order_session": "next_trading_day_opening_auction",
        "order_type": "limit", "time_in_force": "carry_until_executable",
        "reason": "strategy_exit" if reason in {"protective", "time"} else "risk_reduction",
        "full_liquidation": bool(full), "priority": priority,
        "strategy_exit_reason": reason,
    }


def _replace_partial_with_full(state: dict[str, Any], code: str) -> None:
    pending = state.get("pending_exit_orders") or []
    kept = [
        row for row in pending
        if str(row.get("stock_code") or "") != code or row.get("full_liquidation") is True
    ]
    if len(kept) != len(pending):
        state["pending_exit_orders"] = kept
        state.pop("state_id", None)
        state["state_id"] = _identity(state, "state_id")


def _close_exit_orders(
    state: dict[str, Any], trackers: dict[str, dict[str, Any]], *,
    close_rows: dict[str, dict[str, Any]], market_state: str,
    route_mode: str, decision_day: str, next_trade_date: str,
) -> list[dict[str, Any]]:
    equity, gross, values = _portfolio_mark(state, close_rows)
    pending_by_code = {
        str(row.get("stock_code") or ""): row
        for row in state.get("pending_exit_orders") or []
    }
    orders: list[dict[str, Any]] = []
    full_exit_codes: set[str] = set()
    for code, position in sorted(state["positions"].items()):
        tracker = trackers.get(code)
        row = close_rows.get(code) or {}
        close = _finite(row.get("close"))
        if tracker is None or close is None:
            continue
        tracker["holding_sessions"] = int(tracker.get("holding_sessions", 0)) + 1
        reason = None
        if close <= float(tracker["protective_line"]):
            reason = "protective"
        elif tracker["holding_sessions"] >= 3:
            reason = "time"
        if reason is None:
            continue
        existing = pending_by_code.get(code)
        if existing and existing.get("full_liquidation") is True:
            full_exit_codes.add(code)
            continue
        _replace_partial_with_full(state, code)
        orders.append(_exit_order(
            code=code, shares=int(position["shares_total"]),
            decision_day=decision_day, valid_trade_date=next_trade_date,
            prev_close=close, reason=reason, full=True,
            priority="01_protective" if reason == "protective" else "02_time",
        ))
        full_exit_codes.add(code)

    if route_mode == "fixed_exposure_control" or not state["positions"]:
        return orders
    ceiling = STATE_CEILINGS.get(str(market_state), 0.0)
    exposure_pct = 0.0 if equity <= 0 else gross / equity * 100.0
    if exposure_pct <= ceiling + 1.0:
        return orders
    if ceiling == 0.0:
        for code, position in sorted(state["positions"].items()):
            if code in full_exit_codes:
                continue
            existing = pending_by_code.get(code)
            if existing and existing.get("full_liquidation") is True:
                continue
            _replace_partial_with_full(state, code)
            close = _finite((close_rows.get(code) or {}).get("close"))
            if close is None:
                continue
            orders.append(_exit_order(
                code=code, shares=int(position["shares_total"]),
                decision_day=decision_day, valid_trade_date=next_trade_date,
                prev_close=close, reason="state_zero", full=True,
                priority="03_state_zero",
            ))
        return orders
    if full_exit_codes or any(
        row.get("full_liquidation") is True for row in state.get("pending_exit_orders") or []
    ):
        return orders
    ratio = ceiling / exposure_pct
    for code, position in sorted(state["positions"].items()):
        if code in pending_by_code:
            continue
        total = int(position["shares_total"])
        retained = int(total * ratio / LOT_SIZE) * LOT_SIZE
        shares = total - retained
        close = _finite((close_rows.get(code) or {}).get("close"))
        if shares >= LOT_SIZE and close is not None:
            orders.append(_exit_order(
                code=code, shares=shares, decision_day=decision_day,
                valid_trade_date=next_trade_date, prev_close=close,
                reason="state_reduction", full=False, priority="03_state_reduction",
            ))
    return orders


def _daily_metrics(
    records: list[dict[str, Any]], completed_trades: list[dict[str, Any]],
    final_state: dict[str, Any], initial_capital: float,
    unfilled_entries: int, delayed_exits: int,
) -> dict[str, Any]:
    if not records:
        return {}
    nav = pd.Series([row["equity"] for row in records], dtype="float64")
    returns = nav.pct_change().fillna(nav.iloc[0] / initial_capital - 1.0)
    # The initial cash NAV is a high-water mark even if the first session loses.
    running_max = nav.cummax().clip(lower=float(initial_capital))
    drawdown = nav.div(running_max).sub(1.0)
    years = max(len(nav) / 242.0, 1.0 / 242.0)
    total_return = nav.iloc[-1] / initial_capital - 1.0
    cagr = (nav.iloc[-1] / initial_capital) ** (1.0 / years) - 1.0 if nav.iloc[-1] > 0 else -1.0
    std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    downside = float(returns.loc[returns < 0].std(ddof=1)) if (returns < 0).sum() > 1 else 0.0
    profits = [float(row["realized_pnl_yuan"]) for row in completed_trades if row["realized_pnl_yuan"] > 0]
    losses = [float(row["realized_pnl_yuan"]) for row in completed_trades if row["realized_pnl_yuan"] < 0]
    max_drawdown = float(drawdown.min())
    yearly: dict[str, float] = {}
    dated = pd.Series(nav.to_numpy(), index=pd.to_datetime([row["trade_date"] for row in records]))
    for year, values in dated.groupby(dated.index.year):
        prior = initial_capital if int(year) == int(dated.index.year.min()) else float(
            dated.loc[dated.index.year < year].iloc[-1]
        )
        yearly[str(year)] = float(values.iloc[-1] / prior - 1.0)
    state_attribution: dict[str, float] = {}
    record_frame = pd.DataFrame(records)
    record_frame["daily_return"] = returns.to_numpy()
    for state_name, values in record_frame.groupby("market_state", sort=True)["daily_return"]:
        state_attribution[str(state_name)] = float((1.0 + values).prod() - 1.0)
    return {
        "net_total_return": float(total_return), "net_CAGR": float(cagr),
        "maximum_drawdown": max_drawdown,
        "Sharpe": 0.0 if std == 0 else float(returns.mean() / std * math.sqrt(242.0)),
        "Sortino": 0.0 if downside == 0 else float(returns.mean() / downside * math.sqrt(242.0)),
        "Calmar": 0.0 if max_drawdown == 0 else float(cagr / abs(max_drawdown)),
        "round_trips": len(completed_trades),
        "win_rate": 0.0 if not completed_trades else len(profits) / len(completed_trades),
        "payoff_ratio": 0.0 if not profits or not losses else (sum(profits) / len(profits)) / abs(sum(losses) / len(losses)),
        "profit_factor": None if not losses else sum(profits) / abs(sum(losses)),
        "profit_factor_positive_infinity": bool(profits and not losses),
        "turnover": float(
            (final_state["cumulative_buy_notional"] + final_state["cumulative_sell_notional"])
            / max(float(nav.mean()), 1e-12)
        ),
        "cost_drag": float(final_state["cumulative_costs"]["total"] / initial_capital),
        "average_exposure": float(np.mean([row["exposure_pct"] for row in records])),
        "cash_weight": float(np.mean([row["cash_weight_pct"] for row in records])),
        "unfilled_entries": int(unfilled_entries),
        "delayed_exits": int(delayed_exits),
        "state_attribution": state_attribution,
        "sector_concentration": {
            "average_largest_sector_share_of_gross": float(np.mean([
                row["largest_sector_share_of_gross"] for row in records
            ])),
            "maximum_largest_sector_share_of_gross": float(max(
                row["largest_sector_share_of_gross"] for row in records
            )),
        },
        "year_fold_returns": yearly,
    }


def evaluate_period(
    replay: dict[str, Any], start: str, end: str,
    *, initial_capital: float = INITIAL_CAPITAL_YUAN,
) -> dict[str, Any]:
    """Apply Amendment 5 metrics to one closed date interval."""
    all_records = list(replay.get("records") or [])
    selected = [row for row in all_records if start <= str(row.get("trade_date")) <= end]
    if not selected:
        return {"status": "period_empty", "start": start, "end": end}
    earlier = [row for row in all_records if str(row.get("trade_date")) < start]
    start_nav = float(earlier[-1]["equity"]) if earlier else float(initial_capital)
    nav = np.asarray([float(row["equity"]) for row in selected], dtype="float64")
    prior = np.concatenate(([start_nav], nav[:-1]))
    returns = nav / prior - 1.0
    path = np.concatenate(([start_nav], nav))
    drawdown = path / np.maximum.accumulate(path) - 1.0
    total_return = float(nav[-1] / start_nav - 1.0)
    cagr = float((nav[-1] / start_nav) ** (242.0 / len(nav)) - 1.0) if nav[-1] > 0 else -1.0
    std = float(np.std(returns, ddof=1)) if len(returns) > 1 else 0.0
    negative = returns[returns < 0]
    downside = float(np.std(negative, ddof=1)) if len(negative) > 1 else 0.0
    trades = [
        row for row in (replay.get("completed_trades") or [])
        if start <= str(row.get("exit_trade_date")) <= end
    ]
    profits = [float(row["realized_pnl_yuan"]) for row in trades if row["realized_pnl_yuan"] > 0]
    losses = [float(row["realized_pnl_yuan"]) for row in trades if row["realized_pnl_yuan"] < 0]
    ledger = [
        row for row in (replay.get("ledger") or [])
        if start <= str(row.get("trade_date")) <= end
    ]
    costs = sum(float((row.get("costs") or {}).get("total") or 0.0) for row in ledger)
    notional = sum(float(row.get("notional_yuan") or 0.0) for row in ledger)
    record_frame = pd.DataFrame(selected)
    record_frame["daily_return"] = returns
    state_attribution = {
        str(name): float((1.0 + values).prod() - 1.0)
        for name, values in record_frame.groupby("market_state", sort=True)["daily_return"]
    }
    maximum_drawdown = float(drawdown.min())
    return {
        "status": "period_metrics_ready", "start": start, "end": end,
        "sessions": len(selected), "period_start_nav": start_nav,
        "period_end_nav": float(nav[-1]), "net_total_return": total_return,
        "net_CAGR": cagr, "maximum_drawdown": maximum_drawdown,
        "Sharpe": 0.0 if std == 0 else float(np.mean(returns) / std * math.sqrt(242.0)),
        "Sortino": 0.0 if downside == 0 else float(np.mean(returns) / downside * math.sqrt(242.0)),
        "Calmar": 0.0 if maximum_drawdown == 0 else cagr / abs(maximum_drawdown),
        "round_trips": len(trades),
        "win_rate": 0.0 if not trades else len(profits) / len(trades),
        "payoff_ratio": None if not profits or not losses else (sum(profits) / len(profits)) / abs(sum(losses) / len(losses)),
        "profit_factor": None if not losses else sum(profits) / abs(sum(losses)),
        "profit_factor_positive_infinity": bool(profits and not losses),
        "turnover": notional / max(float(np.mean(nav)), 1e-12),
        "cost_drag": costs / initial_capital,
        "average_exposure": float(record_frame["exposure_pct"].mean()),
        "cash_weight": float(record_frame["cash_weight_pct"].mean()),
        "state_attribution": state_attribution,
        "sector_concentration": {
            "average_largest_sector_share_of_gross": float(
                record_frame["largest_sector_share_of_gross"].mean()
            ),
            "maximum_largest_sector_share_of_gross": float(
                record_frame["largest_sector_share_of_gross"].max()
            ),
        },
    }


def evaluate_primary_gates(
    treatment: dict[str, Any], control: dict[str, Any],
) -> dict[str, Any]:
    sealed = evaluate_period(treatment, "2025-01-01", "2025-12-31")
    sealed_control = evaluate_period(control, "2025-01-01", "2025-12-31")
    years = dict((treatment.get("metrics") or {}).get("year_fold_returns") or {})
    positive_years = sum(float(value) > 0.0 for value in years.values())
    profit_factor = sealed.get("profit_factor")
    profit_factor_ready = (
        isinstance(profit_factor, (int, float)) and math.isfinite(float(profit_factor))
        and float(profit_factor) >= 1.2
    ) or (
        sealed.get("profit_factor_positive_infinity") is True
        and int(sealed.get("round_trips") or 0) > 0
    )
    gates = {
        "sealed_test_net_return_positive": float(sealed.get("net_total_return") or 0.0) > 0.0,
        "sealed_test_maximum_drawdown_abs_pct_at_most_20": abs(float(sealed.get("maximum_drawdown") or 0.0)) <= 0.20,
        "sealed_test_Calmar_at_least_0_75": float(sealed.get("Calmar") or 0.0) >= 0.75,
        "sealed_test_profit_factor_at_least_1_2": bool(profit_factor_ready),
        "full_period_round_trips_at_least_50": int((treatment.get("metrics") or {}).get("round_trips") or 0) >= 50,
        "positive_calendar_years_at_least_3": positive_years >= 3,
        "sealed_test_net_return_above_fixed_exposure_control": float(sealed.get("net_total_return") or 0.0) > float(sealed_control.get("net_total_return") or 0.0),
        "sealed_test_maximum_drawdown_not_worse_than_fixed_exposure_control": abs(float(sealed.get("maximum_drawdown") or 0.0)) <= abs(float(sealed_control.get("maximum_drawdown") or 0.0)),
    }
    return {
        "status": "primary_gates_evaluated", "all_passed": all(gates.values()),
        "gates": gates, "positive_calendar_years": positive_years,
        "sealed_test": sealed, "sealed_test_fixed_control": sealed_control,
    }


def replay_daily_path(
    days: Iterable[dict[str, Any]], *, route_mode: str = "state_routed",
    initial_capital: float = INITIAL_CAPITAL_YUAN,
    execution_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay supplied completed daily packets.  The caller owns input admission."""
    if route_mode not in {"state_routed", "fixed_exposure_control"}:
        raise ValueError("route_mode invalid")
    chain = verify_protocol_chain()
    if not chain["valid"]:
        return {"status": "protocol_invalid", "errors": chain["errors"]}
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
    unfilled_entries = 0
    delayed_exits = 0

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
        exit_codes = {str(order.get("stock_code") or "") for order in scheduled_exits}
        position_codes = set(state["positions"])
        codes = entry_codes | exit_codes | position_codes | {
            str(order.get("stock_code") or "") for order in state.get("pending_exit_orders") or []
        }
        market = _market_day(
            trade_date, str(state["last_trade_date"]), current_rows, previous_rows, codes,
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
        execution_order_meta: dict[str, dict[str, Any]] = {}
        for plan in (((pending_decision or {}).get("decision") or {}).get("pre_execution_plans") or []):
            plan_id = str(plan.get("plan_id") or "")
            order_id = hashlib.sha256(
                f"{(pending_decision or {}).get('packet_id')}:{plan_id}".encode("utf-8")
            ).hexdigest()
            execution_order_meta[order_id] = {
                "playbook_id": "state_conditioned_mainline_swing_v1",
                "decision_day": (pending_decision or {}).get("decision_day"),
                "strategy_event": "entry",
            }
        for order in list(state.get("pending_exit_orders") or []) + scheduled_exits:
            execution_order_meta[str(order.get("order_id") or "")] = {
                "playbook_id": "state_conditioned_mainline_swing_v1",
                "decision_day": str(order.get("source_packet_id") or "").removeprefix("close:"),
                "strategy_event": order.get("strategy_exit_reason") or order.get("reason"),
            }
        state = packet["state_after"]
        scheduled_exits = []
        for event in packet["ledger"]:
            event = {
                **event, **execution_order_meta.get(str(event.get("order_id") or ""), {}),
                "trade_date": trade_date,
            }
            ledger.append(event)
            code = event["stock_code"]
            if event["side"] == "buy":
                meta = copy.deepcopy(pending_entry_meta.get(code) or {})
                atr = float(meta.get("atr14_at_decision") or 0.0)
                fill = float(event["fill_price_yuan"])
                trackers[code] = {
                    **meta, "entry_trade_date": trade_date, "holding_sessions": 0,
                    "protective_line": max(fill * 0.92, fill - 2.0 * atr),
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
        exposure_pct = 0.0 if equity <= 0 else gross / equity * 100.0
        sector_values: dict[str, float] = {}
        for code, position in state["positions"].items():
            close = float((current_rows.get(code) or {})["close"])
            sector = str((trackers.get(code) or {}).get("industry_code") or "UNKNOWN")
            sector_values[sector] = sector_values.get(sector, 0.0) + int(position["shares_total"]) * close
        largest_sector_share = 0.0 if gross <= 0 else max(sector_values.values(), default=0.0) / gross
        records.append({
            "trade_date": trade_date, "market_state": str(day.get("market_state") or "UNKNOWN"),
            "equity": equity, "cash_yuan": float(state["cash_yuan"]),
            "exposure_pct": exposure_pct,
            "cash_weight_pct": 0.0 if equity <= 0 else state["cash_yuan"] / equity * 100.0,
            "positions": len(state["positions"]),
            "largest_sector_share_of_gross": largest_sector_share,
        })

        pending_decision = None
        pending_entry_meta = {}
        if next_day is None:
            previous_rows = current_rows
            break
        market_state = str(day.get("market_state") or "UNKNOWN")
        scheduled_exits = _close_exit_orders(
            state, trackers, close_rows=current_rows, market_state=market_state,
            route_mode=route_mode, decision_day=trade_date,
            next_trade_date=next_trade_date,
        )
        blocked_by_exit = bool(scheduled_exits or state.get("pending_exit_orders"))
        excluded = {
            str(tracker.get("stable_instrument_id") or "")
            for tracker in trackers.values()
        }
        available_slots = max(0, MAX_CONCURRENT_NAMES - len(state["positions"]))
        if blocked_by_exit:
            available_slots = 0
        candidates = build_daily_candidates(
            market_state, frame, route_mode=route_mode,
            excluded_instrument_ids=excluded, available_new_slots=available_slots,
        )
        ceiling = 80.0 if route_mode == "fixed_exposure_control" else STATE_CEILINGS.get(market_state, 0.0)
        plans, pending_entry_meta = _entry_plans(
            candidates.get("selected") or [], decision_day=trade_date,
            next_trade_date=next_trade_date, equity=equity, gross=gross,
            cash=float(state["cash_yuan"]), ceiling_pct=ceiling,
        )
        pending_decision = _decision_packet(
            trade_date, plans, int(candidates.get("maximum_new_trades") or 0),
        )
        previous_rows = current_rows
        day = next_day
        trade_date = next_trade_date

    metrics = _daily_metrics(
        records, completed_trades, state, initial_capital,
        unfilled_entries, delayed_exits,
    )
    return {
        "schema": "state_conditioned_mainline_replay.v1",
        "status": "replay_complete", "route_mode": route_mode,
        "records": records, "ledger": ledger,
        "completed_trades": completed_trades, "metrics": metrics,
        "unfilled_entries": unfilled_entries, "delayed_exits": delayed_exits,
        "final_state": state, "trackers": trackers,
        "returns_or_labels_read": True,
        "authority": {
            "research_only": True, "changes_live_score": False,
            "changes_live_position_or_quantity": False,
            "generates_buy_or_sell_alert": False, "production_strategy": False,
            "automatic_trading": False, "stable_profit_claim": False,
        },
    }


__all__ = [
    "evaluate_period", "evaluate_primary_gates",
    "initial_portfolio_state", "replay_daily_path",
]
