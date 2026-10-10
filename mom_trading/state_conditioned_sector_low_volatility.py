"""One frozen non-factor low-volatility playbook for the strict market route.

The module consumes only completed daily rows supplied by its caller.  Orders,
fills, A-share limits, T+1 settlement and costs remain owned by the existing
joint execution engine.
"""

from __future__ import annotations

import copy
import hashlib
import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .state_conditioned_mainline_backtest import (
    BASE_EXECUTION_CONFIG,
    INITIAL_CAPITAL_YUAN,
    _daily_metrics,
    _decision_packet,
    _entry_plans,
    _exit_order,
    _market_day,
    _portfolio_mark,
    _replace_partial_with_full,
    _rows_by_code,
    evaluate_period,
    initial_portfolio_state,
)
from .state_conditioned_mainline_swing import (
    _confirmed_sectors,
    _sector_constituents,
)
from .unified_regime_route_replay import replay_joint_execution_day


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = (
    PROJECT_ROOT
    / "research/STATE_CONDITIONED_SECTOR_LOW_VOLATILITY_252D_V1_PROTOCOL.json"
)
PROTOCOL_SHA256 = "d7d0ba720160628ea9c56ea205af4706a63e074043a2717cec13a8d1f36f49e1"
PLAYBOOK_ID = "state_conditioned_sector_low_volatility_252d_v1"
ROUTE_MODES = {
    "state_sector_routed", "state_only_control", "no_route_control",
}
MAX_CONCURRENT_NAMES = 2
TARGET_EXPOSURE_PCT = 40.0
REQUIRED_COLUMNS = {
    "stable_instrument_id", "ts_code", "code", "trade_date", "open", "high",
    "low", "close", "prev_close", "amount", "adv20", "median_amount20",
    "pct_chg", "is_st", "trade_status", "adj_close", "sma20", "ret20",
    "atr14", "float_mv", "board", "adjustment_trusted", "factor_changed",
    "history_sessions", "limit_evaluable", "industry_code", "industry_name",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_protocol() -> dict[str, Any]:
    errors: list[str] = []
    try:
        if _sha256(PROTOCOL_PATH) != PROTOCOL_SHA256:
            errors.append("protocol_sha256_mismatch")
    except OSError as exc:
        errors.append(f"protocol_unreadable:{exc}")
    return {"valid": not errors, "errors": errors}


def add_realized_volatility_252d(panel: pd.DataFrame) -> pd.DataFrame:
    """Attach the frozen signal using only each row and its 251 predecessors."""
    missing = sorted(REQUIRED_COLUMNS - set(panel.columns))
    if missing:
        raise ValueError(f"missing_columns:{','.join(missing)}")
    frame = panel.sort_values(
        ["stable_instrument_id", "trade_date"], kind="mergesort",
    ).reset_index(drop=True).copy()
    if frame.duplicated(["stable_instrument_id", "trade_date"]).any():
        raise ValueError("duplicate_instrument_trade_date")
    returns = pd.to_numeric(frame["pct_chg"], errors="coerce").div(100.0)
    trusted = (
        frame["adjustment_trusted"].eq(True)
        & frame["factor_changed"].ne(True)
    )
    returns = returns.where(trusted)
    frame["realized_volatility_252d"] = (
        returns.groupby(frame["stable_instrument_id"], sort=False, observed=True)
        .rolling(window=252, min_periods=252)
        .std(ddof=1)
        .reset_index(level=0, drop=True)
    )
    return frame.sort_values(
        ["trade_date", "stable_instrument_id"], kind="mergesort",
    ).reset_index(drop=True)


def _confirmed_sector_rows(daily_rows: pd.DataFrame) -> pd.DataFrame:
    return _confirmed_sectors(_sector_constituents(daily_rows))


def build_daily_candidates(
    market_state: str,
    daily_rows: pd.DataFrame,
    *,
    is_month_end: bool,
    route_mode: str = "state_sector_routed",
    excluded_instrument_ids: set[str] | None = None,
    available_new_slots: int | None = None,
) -> dict[str, Any]:
    """Select at most one lowest-volatility stock without reading a future row."""
    if route_mode not in ROUTE_MODES:
        raise ValueError("route_mode invalid")
    missing = sorted((REQUIRED_COLUMNS | {"realized_volatility_252d"}) - set(daily_rows.columns))
    if missing:
        return {
            "status": "input_incomplete",
            "errors": [f"missing_column:{name}" for name in missing],
            "selected": [], "candidate_count": 0, "confirmed_sectors": [],
            "future_returns_or_labels_read": False,
        }
    protocol = verify_protocol()
    if not protocol["valid"]:
        return {
            "status": "protocol_invalid", "errors": protocol["errors"],
            "selected": [], "candidate_count": 0, "confirmed_sectors": [],
            "future_returns_or_labels_read": False,
        }

    sectors = _confirmed_sector_rows(daily_rows)
    state_required = route_mode != "no_route_control"
    sector_required = route_mode == "state_sector_routed"
    state_ready = str(market_state or "UNKNOWN") == "STABILIZING"
    entry_window = bool(is_month_end and (state_ready or not state_required))

    eligible = _sector_constituents(daily_rows)
    volatility = pd.to_numeric(
        eligible.get("realized_volatility_252d"), errors="coerce",
    )
    eligible = eligible.loc[
        eligible["history_sessions"].ge(252)
        & eligible["float_mv"].le(80_000_000_000)
        & volatility.notna()
        & volatility.map(math.isfinite)
    ].copy()
    if not eligible.empty:
        eligible["realized_volatility_252d"] = volatility.loc[eligible.index]
    if sector_required:
        confirmed_codes = set(sectors.get("industry_code", pd.Series(dtype="object")))
        eligible = eligible.loc[eligible["industry_code"].isin(confirmed_codes)].copy()
    excluded = {str(value) for value in (excluded_instrument_ids or set())}
    if excluded and not eligible.empty:
        eligible = eligible.loc[
            ~eligible["stable_instrument_id"].astype(str).isin(excluded)
        ].copy()
    if not eligible.empty:
        eligible.sort_values(
            ["realized_volatility_252d", "median_amount20", "stable_instrument_id"],
            ascending=[True, False, True], inplace=True,
        )
    maximum = 1 if entry_window else 0
    if available_new_slots is not None:
        maximum = min(maximum, max(0, int(available_new_slots)))
    selected = eligible.head(maximum) if maximum > 0 else eligible.iloc[:0]
    fields = [
        "stable_instrument_id", "ts_code", "code", "trade_date", "close",
        "adv20", "atr14", "industry_code", "industry_name", "median_amount20",
        "float_mv", "realized_volatility_252d",
    ]
    selected_rows = []
    for row in selected[fields].to_dict("records") if not selected.empty else []:
        selected_rows.append({
            key: value.isoformat() if isinstance(value, pd.Timestamp) else value
            for key, value in row.items()
        })
    return {
        "schema": "state_conditioned_sector_low_volatility_candidates.v1",
        "status": (
            "candidate_research_ready" if selected_rows
            else "not_month_end" if not is_month_end
            else "route_no_new_trade" if not entry_window
            else "no_eligible_candidate"
        ),
        "route_mode": route_mode,
        "market_state": str(market_state or "UNKNOWN"),
        "target_exposure_pct": TARGET_EXPOSURE_PCT,
        "maximum_new_trades": maximum,
        "confirmed_sectors": sectors.to_dict("records"),
        "candidate_count": int(len(eligible)),
        "selected_count": len(selected_rows),
        "selected": selected_rows,
        "playbook_id": PLAYBOOK_ID,
        "factor_catalog_required": False,
        "named_trader_identity_used": False,
        "future_returns_or_labels_read": False,
        "authority": {
            "research_only": True,
            "generates_alert": False,
            "production_strategy": False,
            "automatic_trading": False,
        },
    }


def _close_exit_orders(
    state: dict[str, Any],
    trackers: dict[str, dict[str, Any]],
    *,
    close_rows: dict[str, dict[str, Any]],
    market_state: str,
    confirmed_sector_codes: set[str],
    route_mode: str,
    decision_day: str,
    next_trade_date: str,
) -> list[dict[str, Any]]:
    pending_by_code = {
        str(row.get("stock_code") or ""): row
        for row in state.get("pending_exit_orders") or []
    }
    orders: list[dict[str, Any]] = []
    for code, position in sorted(state["positions"].items()):
        tracker = trackers.get(code)
        close = float((close_rows.get(code) or {}).get("close") or 0.0)
        if tracker is None or close <= 0:
            continue
        tracker["holding_sessions"] = int(tracker.get("holding_sessions", 0)) + 1
        reason = None
        if close <= float(tracker["protective_line"]):
            reason = "protective"
        elif route_mode != "no_route_control" and market_state != "STABILIZING":
            reason = "state"
        elif (
            route_mode == "state_sector_routed"
            and str(tracker.get("industry_code") or "") not in confirmed_sector_codes
        ):
            reason = "sector"
        elif tracker["holding_sessions"] >= 20:
            reason = "time"
        if reason is None:
            continue
        existing = pending_by_code.get(code)
        if existing and existing.get("full_liquidation") is True:
            continue
        _replace_partial_with_full(state, code)
        orders.append(_exit_order(
            code=code,
            shares=int(position["shares_total"]),
            decision_day=decision_day,
            valid_trade_date=next_trade_date,
            prev_close=close,
            reason=reason,
            full=True,
            priority={
                "protective": "01_protective",
                "state": "02_state",
                "sector": "03_sector",
                "time": "04_time",
            }[reason],
        ))
    return orders


def replay_daily_path(
    days: Iterable[dict[str, Any]],
    *,
    route_mode: str = "state_sector_routed",
    initial_capital: float = INITIAL_CAPITAL_YUAN,
    execution_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay one complete path through the shared A-share execution engine."""
    if route_mode not in ROUTE_MODES:
        raise ValueError("route_mode invalid")
    protocol = verify_protocol()
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
        codes = entry_codes | exit_codes | set(state["positions"]) | {
            str(order.get("stock_code") or "")
            for order in state.get("pending_exit_orders") or []
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
                "playbook_id": PLAYBOOK_ID,
                "decision_day": (pending_decision or {}).get("decision_day"),
                "strategy_event": "entry",
            }
        for order in list(state.get("pending_exit_orders") or []) + scheduled_exits:
            execution_order_meta[str(order.get("order_id") or "")] = {
                "playbook_id": PLAYBOOK_ID,
                "decision_day": str(order.get("source_packet_id") or "").removeprefix("close:"),
                "strategy_event": order.get("strategy_exit_reason") or order.get("reason"),
            }
        state = packet["state_after"]
        scheduled_exits = []
        for raw_event in packet["ledger"]:
            event = {
                **raw_event,
                **execution_order_meta.get(str(raw_event.get("order_id") or ""), {}),
                "trade_date": trade_date,
            }
            ledger.append(event)
            code = str(event["stock_code"])
            if event["side"] == "buy":
                meta = copy.deepcopy(pending_entry_meta.get(code) or {})
                fill = float(event["fill_price_yuan"])
                trackers[code] = {
                    **meta,
                    "entry_trade_date": trade_date,
                    "holding_sessions": 0,
                    "protective_line": fill * 0.90,
                }
                open_trade_pnl[code] = 0.0
            else:
                open_trade_pnl[code] = open_trade_pnl.get(code, 0.0) + float(
                    event.get("realized_pnl_yuan") or 0.0
                )
                if code not in state["positions"]:
                    completed_trades.append({
                        "stock_code": code,
                        "entry_trade_date": (trackers.get(code) or {}).get(
                            "entry_trade_date"
                        ),
                        "exit_trade_date": trade_date,
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
            sector_values[sector] = (
                sector_values.get(sector, 0.0)
                + int(position["shares_total"]) * close
            )
        records.append({
            "trade_date": trade_date,
            "market_state": str(day.get("market_state") or "UNKNOWN"),
            "equity": equity,
            "cash_yuan": float(state["cash_yuan"]),
            "exposure_pct": exposure_pct,
            "cash_weight_pct": (
                0.0 if equity <= 0 else state["cash_yuan"] / equity * 100.0
            ),
            "positions": len(state["positions"]),
            "largest_sector_share_of_gross": (
                0.0 if gross <= 0 else max(sector_values.values(), default=0.0) / gross
            ),
        })

        pending_decision = None
        pending_entry_meta = {}
        if next_day is None:
            break
        market_state = str(day.get("market_state") or "UNKNOWN")
        sectors = _confirmed_sector_rows(frame)
        confirmed_sector_codes = {
            str(value) for value in sectors.get("industry_code", [])
        }
        scheduled_exits = _close_exit_orders(
            state, trackers,
            close_rows=current_rows,
            market_state=market_state,
            confirmed_sector_codes=confirmed_sector_codes,
            route_mode=route_mode,
            decision_day=trade_date,
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
        is_month_end = trade_date[:7] != next_trade_date[:7]
        candidates = build_daily_candidates(
            market_state,
            frame,
            is_month_end=is_month_end,
            route_mode=route_mode,
            excluded_instrument_ids=excluded,
            available_new_slots=available_slots,
        )
        plans, pending_entry_meta = _entry_plans(
            candidates.get("selected") or [],
            decision_day=trade_date,
            next_trade_date=next_trade_date,
            equity=equity,
            gross=gross,
            cash=float(state["cash_yuan"]),
            ceiling_pct=TARGET_EXPOSURE_PCT,
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
        "schema": "state_conditioned_sector_low_volatility_replay.v1",
        "status": "replay_complete",
        "route_mode": route_mode,
        "records": records,
        "ledger": ledger,
        "completed_trades": completed_trades,
        "metrics": metrics,
        "unfilled_entries": unfilled_entries,
        "delayed_exits": delayed_exits,
        "final_state": state,
        "trackers": trackers,
        "future_returns_or_labels_read": True,
        "authority": {
            "research_only": True,
            "generates_buy_or_sell_alert": False,
            "production_strategy": False,
            "automatic_trading": False,
            "stable_profit_claim": False,
        },
    }


def _profit_factor_passes(metrics: dict[str, Any], threshold: float) -> bool:
    value = metrics.get("profit_factor")
    return bool(
        isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) >= threshold
    ) or bool(
        metrics.get("profit_factor_positive_infinity") is True
        and int(metrics.get("round_trips") or 0) > 0
    )


def evaluate_primary_gates(
    base_treatment: dict[str, Any],
    base_no_route_control: dict[str, Any],
    stress_treatment: dict[str, Any],
) -> dict[str, Any]:
    validation = evaluate_period(base_treatment, "2022-01-04", "2023-12-29")
    sealed = evaluate_period(base_treatment, "2024-01-02", "2025-12-31")
    control = evaluate_period(
        base_no_route_control, "2024-01-02", "2025-12-31",
    )
    stress = evaluate_period(stress_treatment, "2024-01-02", "2025-12-31")
    year_2024 = evaluate_period(base_treatment, "2024-01-02", "2024-12-31")
    year_2025 = evaluate_period(base_treatment, "2025-01-01", "2025-12-31")
    terminal_clean = bool(
        not (base_treatment.get("final_state") or {}).get("positions")
        and not (base_treatment.get("final_state") or {}).get("pending_exit_orders")
        and not (base_treatment.get("trackers") or {})
    )
    gates = {
        "validation_base_net_return_positive": (
            float(validation.get("net_total_return") or 0.0) > 0.0
        ),
        "sealed_base_net_return_positive": (
            float(sealed.get("net_total_return") or 0.0) > 0.0
        ),
        "sealed_maximum_drawdown_abs_pct_at_most_15": (
            abs(float(sealed.get("maximum_drawdown") or 0.0)) <= 0.15
        ),
        "sealed_Calmar_at_least_0_75": (
            float(sealed.get("Calmar") or 0.0) >= 0.75
        ),
        "sealed_profit_factor_at_least_1_2": _profit_factor_passes(
            sealed, 1.2,
        ),
        "sealed_round_trips_at_least_12": (
            int(sealed.get("round_trips") or 0) >= 12
        ),
        "sealed_each_calendar_year_net_return_positive": bool(
            float(year_2024.get("net_total_return") or 0.0) > 0.0
            and float(year_2025.get("net_total_return") or 0.0) > 0.0
        ),
        "sealed_return_above_same_rule_without_route": (
            float(sealed.get("net_total_return") or 0.0)
            > float(control.get("net_total_return") or 0.0)
        ),
        "sealed_drawdown_not_worse_than_same_rule_without_route": (
            abs(float(sealed.get("maximum_drawdown") or 0.0))
            <= abs(float(control.get("maximum_drawdown") or 0.0))
        ),
        "stress_sealed_net_return_positive": (
            float(stress.get("net_total_return") or 0.0) > 0.0
        ),
        "stress_sealed_profit_factor_at_least_1_0": _profit_factor_passes(
            stress, 1.0,
        ),
        "terminal_open_positions_and_pending_orders_zero": terminal_clean,
    }
    return {
        "status": "historical_primary_gates_evaluated",
        "all_passed": all(gates.values()),
        "gates": gates,
        "validation": validation,
        "sealed_out_of_sample": sealed,
        "sealed_no_route_control": control,
        "stress_sealed_out_of_sample": stress,
        "sealed_calendar_years": {"2024": year_2024, "2025": year_2025},
    }


__all__ = [
    "add_realized_volatility_252d",
    "build_daily_candidates",
    "evaluate_primary_gates",
    "replay_daily_path",
    "verify_protocol",
]
