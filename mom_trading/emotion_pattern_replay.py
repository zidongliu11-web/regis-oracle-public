"""Costed event-path diagnostics for the fixed emotion-pattern hypotheses.

Each event has its own hypothetical budget, NOT a shared portfolio. Overlapping
events therefore cannot be compounded into an account return. Unresolved paths
are retained and suppress the all-event return, never assigned a zero recovery.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math

import pandas as pd

from quantqc.ashare import board_of, constrain_share_quantity, opening_auction_executability
from quantqc.costs import one_side_cost_breakdown
from .emotion_pattern_research import PLAYBOOKS, research_entry_check, research_exit_check


CONTRACT = {
    "schema": "emotion_pattern_event_replay.v1",
    "purpose": "event_path_diagnostic_not_portfolio_or_strategy_admission",
    "event_cash_yuan": 20_000.0,
    "event_risk_budget_yuan": 1_000.0,
    "max_lagged_ADV_participation": 0.01,
    "impact_coefficient_bps": 25.0,
    "slippage_scenarios_bps": [10, 20, 30],
    "entry": "signal_close_to_exact_next_session_open_no_retry",
    "exit": "close_confirmed_risk_or_2R_or_five_sessions_or_emotion_then_next_executable_open",
    "holding_clock": "entry_session_counts_as_one_no_same_day_sale",
    "corporate_actions": "missing_cash_and_share_entitlements_make_path_unresolved_not_zero",
    "partial_exits": "sell_whole_lots_to_lagged_ADV_cap_keep_unsold_inventory",
    "commission": "shared_all_in_0.025pct_minimum_5_yuan_assumption_not_broker_statement",
    "historical_tax_sources": [
        "https://www.mof.gov.cn/jrttts/202308/t20230828_3904235.htm",
        "https://jrj.wuhan.gov.cn/ynzx_57/xwzx/202204/t20220429_1964160.shtml",
    ],
    "historical_reuse": True,
    "unseen_out_of_sample": False,
    "production_eligible": False,
    "generates_alert": False,
}


def contract_identity() -> str:
    return hashlib.sha256(json.dumps(CONTRACT, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class DistributionInputGap(ValueError):
    """Missing settlement evidence, distinct from a broken accounting invariant."""


def historical_costs(notional: float, side: str, day: str, adv: float, bps: int) -> dict:
    """Reuse commission/slippage/impact; apply date-specific historic tax rates."""
    if bps not in CONTRACT["slippage_scenarios_bps"] or not math.isfinite(adv) or adv <= 0:
        raise ValueError("replay_cost_inputs_invalid")
    if not math.isfinite(notional) or notional <= 0:
        raise ValueError("replay_notional_invalid")
    if not "2021-01-01" <= day <= "2025-12-31":
        raise ValueError("historical_fee_date_outside_verified_scope")
    impact = CONTRACT["impact_coefficient_bps"] / 10_000 * math.sqrt(notional / adv)
    base = one_side_cost_breakdown(notional, side, bps / 10_000, impact)
    costs = {k: float(base[k]) for k in ("commission", "transfer", "stamp", "slippage", "market_impact")}
    costs["transfer"] = notional * (0.00002 if day < "2022-04-29" else 0.00001)
    costs["stamp"] = notional * (0.001 if day < "2023-08-28" else 0.0005) if side == "sell" else 0.0
    costs["total"] = sum(costs.values())
    return costs


class EventMarket:
    """One instrument's immutable daily observations and the full session clock."""

    REQUIRED = {"code", "stable_instrument_id", "trade_date", "open", "high", "low", "close",
                "prev_close", "adj_factor", "adjustment_trusted", "trade_status", "is_st",
                "no_price_limit", "lagged_adv"}

    def __init__(self, rows: pd.DataFrame, sessions: list[str], states: dict[str, str]):
        if not sessions or sessions != sorted(set(sessions)):
            raise ValueError("replay_calendar_not_ordered_unique")
        for day in sessions:
            if pd.Timestamp(day).date().isoformat() != day:
                raise ValueError("replay_calendar_date_invalid")
        if not self.REQUIRED.issubset(rows.columns):
            raise ValueError("replay_market_columns_missing")
        frame = rows.copy()
        frame["trade_date"] = pd.to_datetime(frame.trade_date).dt.strftime("%Y-%m-%d")
        if frame.trade_date.duplicated().any() or frame.code.nunique() != 1:
            raise ValueError("replay_instrument_date_identity_invalid")
        if not frame.trade_date.isin(sessions).all():
            raise ValueError("replay_market_outside_calendar")
        self.sessions = sessions
        self.index = {day: i for i, day in enumerate(sessions)}
        self.rows = {r["trade_date"]: r for r in frame.to_dict("records")}
        self.states = dict(states)


def _positive(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (float, int)) and math.isfinite(value) and value > 0


def _price_problem(row: dict, original: dict) -> str | None:
    if row["trade_status"] not in (0, 1) or row["is_st"] not in (0, 1) or type(row["no_price_limit"]) is not bool:
        return "trading_status_or_price_limit_identity_missing"
    if not row["stable_instrument_id"] or not original["stable_instrument_id"]:
        return "stable_instrument_identity_missing"
    if row["stable_instrument_id"] != original["stable_instrument_id"]:
        return "instrument_successor_entitlements_missing"
    if row["adjustment_trusted"] is not True or not _positive(row["adj_factor"]):
        return "adjustment_identity_missing"
    if not math.isclose(row["adj_factor"], original["adj_factor"], rel_tol=0, abs_tol=1e-12):
        return "corporate_action_cash_and_share_ledger_missing"
    # OHLC consistency is an input audit, not a predicate choosing profitable paths.
    prices = [row[x] for x in ("open", "high", "low", "close")]
    if row["trade_status"] == 1 and (not all(_positive(v) for v in prices)
                                   or row["high"] < max(prices) or row["low"] > min(prices)):
        return "invalid_tradable_OHLC"
    return None


def replay_event(candidate: dict, market: EventMarket, *, slippage_bps: int, distributions=None) -> dict:
    """Replay one fixed signal, including every rejected/delayed/unresolved path."""
    if slippage_bps not in CONTRACT["slippage_scenarios_bps"]:
        raise ValueError("replay_cost_scenario_invalid")
    pattern = next((p for p in PLAYBOOKS if p["id"] == candidate["playbook_id"]), None)
    if not pattern or pattern["kind"] != "entry_research":
        raise ValueError("replay_requires_entry_pattern")
    signal_day = pd.Timestamp(candidate["trade_date"]).date().isoformat()
    state = market.states.get(signal_day, "UNKNOWN")
    if state != candidate["confirmed_state"] or state not in pattern["states"]:
        raise ValueError("replay_signal_emotion_identity_invalid")
    result = {
        "signal_day": signal_day, "code": candidate["code"], "playbook_id": pattern["id"],
        "playbook_name": pattern["name"], "signal_emotion": state, "slippage_bps": slippage_bps,
        "status": "unfilled", "reason": None, "entry_day": None, "exit_day": None,
        "simulated_entry_shares": 0, "remaining_shares": 0, "net_pnl_yuan": None,
        "net_return_on_entry_cash": None, "cost_yuan": 0.0, "exit_reasons": [],
        "execution_events": [], "actual_fill": False, "production_eligible": False,
        "generates_alert": False,
    }

    def finish(status, reason):
        result.update(status=status, reason=reason)
        return distributions.finish(result) if distributions is not None else result

    if board_of(candidate["code"]) not in {"main", "gemb"}:
        return finish("unfilled", "security_outside_verified_main_and_ChiNext_scope")
    source = market.rows.get(signal_day)
    if source is None or str(source["code"]) != str(candidate["code"]):
        raise ValueError("replay_signal_market_identity_missing")
    if (not _positive(candidate["reference_close"])
        or not math.isclose(candidate["reference_close"], source["close"], rel_tol=0, abs_tol=1e-8)):
        raise ValueError("replay_signal_reference_price_mismatch")
    signal_index = market.index[signal_day]
    if signal_index + 1 >= len(market.sessions):
        return finish("unresolved", "next_session_outside_input_horizon")
    entry_day = market.sessions[signal_index + 1]
    opening = market.rows.get(entry_day)
    if opening is None:
        return finish("unresolved", "entry_session_market_row_missing")
    problem = _price_problem(opening, source)
    if problem:
        return finish("unresolved", problem)
    normalized_opening = {**opening, "is_st": opening["is_st"] == 1}
    terms = distributions.entry_terms(candidate, entry_day) if distributions is not None else candidate
    gate = research_entry_check(terms, normalized_opening, expected_session=entry_day)
    if gate["eligible_for_execution_replay"] is not True:
        return finish("unfilled", gate["reason"])
    price, risk, adv = opening["open"], gate["risk_line"], opening["lagged_adv"]
    if not _positive(adv):
        return finish("unresolved", "entry_lagged_ADV_missing")
    requested = min(CONTRACT["event_cash_yuan"] / price,
                    CONTRACT["event_risk_budget_yuan"] / (price - risk))
    shares = constrain_share_quantity(requested, side="buy", liquidity_cap_shares=adv * 0.01 / price)["accepted_shares"]
    while shares > 0:
        costs = historical_costs(shares * price, "buy", entry_day, adv, slippage_bps)
        if shares * price + costs["total"] <= CONTRACT["event_cash_yuan"]:
            break
        shares -= 100
    if shares <= 0:
        return finish("unfilled", "cash_risk_or_liquidity_below_one_lot")
    entry_cash = shares * price + costs["total"]
    result.update(entry_day=entry_day, simulated_entry_shares=shares, remaining_shares=shares,
                  cost_yuan=costs["total"])
    result["execution_events"].append({"day": entry_day, "side": "buy", "shares": shares, "price": price, "costs": costs})
    if distributions is not None:
        distributions.bought(entry_day, shares)
    proceeds = 0.0
    pending = False
    for index in range(signal_index + 1, len(market.sessions)):
        day = market.sessions[index]
        row = market.rows.get(day)
        if row is None:
            return finish("unresolved", "holding_session_market_row_missing")
        problem = _price_problem(row, source)
        if problem:
            return finish("unresolved", problem)
        if distributions is not None:
            try:
                shares, price, risk = distributions.open_day(day, shares, price, risk)
            except DistributionInputGap as exc:
                return finish("unresolved", str(exc))
            result["remaining_shares"] = shares
        if pending and day > entry_day:
            sell_gate = opening_auction_executability(
                code=candidate["code"], trade_date=day, side="sell", open_price=row["open"],
                prev_close=row["prev_close"], tradable=row["trade_status"] == 1,
                is_risk_warning=row["is_st"] == 1, no_price_limit=row["no_price_limit"],
            )
            if sell_gate["executable"] and row["no_price_limit"] is False:
                if not _positive(row["lagged_adv"]):
                    return finish("unresolved", "exit_lagged_ADV_missing")
                cap = int(row["lagged_adv"] * 0.01 / row["open"])
                available = distributions.available(day) if distributions is not None else shares
                quantity = constrain_share_quantity(
                    shares, side="sell", available_to_sell=available,
                    full_liquidation=cap >= available, liquidity_cap_shares=cap,
                )["accepted_shares"]
                if quantity:
                    costs = historical_costs(quantity * row["open"], "sell", day, row["lagged_adv"], slippage_bps)
                    if distributions is not None:
                        try:
                            distributions.sold(day, quantity)
                        except DistributionInputGap as exc:
                            return finish("unresolved", str(exc))
                    proceeds += quantity * row["open"] - costs["total"]
                    result["cost_yuan"] += costs["total"]
                    shares -= quantity
                    result["remaining_shares"] = shares
                    result["execution_events"].append({"day": day, "side": "sell", "shares": quantity, "price": row["open"], "costs": costs})
                    if shares == 0:
                        result.update(exit_day=day, net_pnl_yuan=proceeds - entry_cash,
                                      net_return_on_entry_cash=proceeds / entry_cash - 1)
                        return finish("closed", "next_executable_open_exit")
                else:
                    reason = "distribution_shares_not_yet_sellable" if available == 0 else "ADV_below_one_lot"
                    result["execution_events"].append({"day": day, "side": "sell", "shares": 0, "reason": reason})
            else:
                result["execution_events"].append({"day": day, "side": "sell", "shares": 0, "reason": sell_gate["reason"] if not sell_gate["executable"] else "special_no_limit_day"})
        if not pending:
            held = index - signal_index
            if row["trade_status"] == 1:
                exit_check = research_exit_check(playbook_id=pattern["id"], entry_price=price, risk_line=risk,
                                                completed_close=row["close"], held_sessions=held,
                                                state=market.states.get(day, "UNKNOWN"))
                reasons = exit_check["reasons"]
            else:
                reasons = (["five_session_time_exit"] if held >= 5 else [])
                if market.states.get(day, "UNKNOWN") not in pattern["states"]:
                    reasons.append("emotion_route_invalidated")
            if reasons:
                pending = True
                result["exit_reasons"] = reasons
        if distributions is not None:
            distributions.close_day(day, shares)
    return finish("unresolved", "open_inventory_at_input_horizon")


def summarize_events(events: list[dict]) -> list[dict]:
    """No Sharpe, CAGR or account PnL from independent, overlapping events."""
    frame = pd.DataFrame(events)
    if frame.empty:
        return []
    summaries = []
    for keys, group in frame.groupby(["playbook_name", "signal_emotion", "slippage_bps"], sort=True):
        complete = group.loc[group.status.eq("closed")]
        unresolved = int(group.status.eq("unresolved").sum())
        summaries.append({
            "playbook_name": keys[0], "signal_emotion": keys[1], "slippage_bps": int(keys[2]),
            "signals": len(group), "closed_events": len(complete), "unfilled_events": int(group.status.eq("unfilled").sum()),
            "unresolved_events": unresolved, "reason_counts": dict(Counter(group.reason)),
            "closed_only_mean_net_return": None if complete.empty else float(complete.net_return_on_entry_cash.mean()),
            "closed_only_win_fraction": None if complete.empty else float(complete.net_pnl_yuan.gt(0).mean()),
            "all_filled_mean_net_return": None if unresolved or complete.empty else float(complete.net_return_on_entry_cash.mean()),
            "portfolio_return": None, "statistically_validated": False, "production_eligible": False,
        })
    return summaries
