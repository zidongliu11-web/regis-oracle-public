"""Corrected cash/share diagnostics, not a new blind test of crowding.

The formula and pre-outcome cohorts are frozen elsewhere. Each event reserves
its own cash; overlapping events are not a portfolio or executable orders.
"""
from __future__ import annotations

from collections import Counter
import math

import pandas as pd

from quantqc.ashare import board_of, constrain_share_quantity, opening_auction_executability
from .emotion_pattern_replay import EventMarket, _positive, _price_problem, historical_costs


SPEC = {
    "schema": "crowding_cash_share_diagnostic.v1",
    "factor_id": "negative_limit_hit_count_20_v1",
    "factor_name": "近期涨停低拥挤度",
    "formula_direction_universe_and_weekly_dates_changed": False,
    "selection": "all_signal_rows_before_any_outcomes; factor_desc_code_asc; ceil_20pct",
    "entry": "exact_next_market_session_open_no_retry",
    "exit": "entry_session_index_plus_5_open_then_up_to_5_additional_sessions",
    "event_cash_yuan_including_fees": 10_000.0,
    "primary_return": "net_PnL_divided_by_reserved_10000_including_idle_cash",
    "unfilled_return": "zero_only_for_known_nonexecution; never_for_missing_data",
    "execution_corrections": [
        "raw_share_cash_ledger_not_adjusted_notional",
        "whole_lots_with_fees_inside_cash_budget",
        "date_specific_tax_and_minimum_commission",
        "lagged_ADV_cap_1pct_and_25bp_sqrt_participation_impact",
        "known_ST_or_special_no_limit_entry_not_assumed_filled",
        "missing_market_rows_and_corporate_entitlements_unresolved",
        "unknown_historical_code_price_limit_identity_unresolved_not_dropped",
        "partial_exits_preserve_inventory_and_charge_per_order",
    ],
    "cost_scenarios_bps": [10, 20, 30],
    "cohort_metrics": "full_group_metric_null_if_any_member_unresolved; no_replacement",
    "emotion": "actual_confirmed_signal_day_state; UNKNOWN_retained_not_reassigned",
    "historical_results_already_seen": True, "new_blind_trial": False,
    "parameter_tuning": False, "production_eligible": False, "generates_alert": False,
}


def replay_fixed_horizon(signal: dict, market: EventMarket, *, bps: int) -> dict:
    if bps not in SPEC["cost_scenarios_bps"]:
        raise ValueError("crowding_cost_scenario_invalid")
    day, code = signal["signal_day"], signal["code"]
    source = market.rows.get(day)
    if day not in market.index or source is None or source["code"] != code:
        raise ValueError("crowding_signal_market_identity_missing")
    if not math.isfinite(signal["factor"]):
        raise ValueError("crowding_signal_input_missing")
    result = {
        "signal_day": day, "code": code, "factor": signal["factor"],
        "signal_emotion": market.states.get(day, "UNKNOWN"), "slippage_bps": bps,
        "status": None, "reason": None, "entry_day": None, "exit_day": None,
        "entry_shares": 0, "remaining_shares": 0, "entry_cash_yuan": None,
        "cost_yuan": 0.0, "net_pnl_yuan": None, "fixed_budget_return": None,
        "invested_cash_return": None, "execution_events": [],
        "actual_fill": False, "production_eligible": False, "generates_alert": False,
    }

    def finish(status, reason):
        result.update(status=status, reason=reason)
        if status == "unfilled":
            result.update(net_pnl_yuan=0.0, fixed_budget_return=0.0)
        return result

    if board_of(code) not in {"main", "gemb"}:
        return finish("unresolved", "historical_code_price_limit_identity_unverified")
    start = market.index[day] + 1
    target, deadline = start + 5, start + 10
    if start >= len(market.sessions):
        return finish("unresolved", "next_session_outside_input_horizon")
    entry_day = market.sessions[start]
    opening = market.rows.get(entry_day)
    if opening is None:
        return finish("unresolved", "entry_session_market_row_missing")
    if opening["trade_status"] == 0:
        return finish("unfilled", "entry_suspended")
    problem = _price_problem(opening, source)
    if problem:
        return finish("unresolved", problem)
    if opening["is_st"] == 1 or opening["no_price_limit"]:
        return finish("unfilled", "ST_or_special_no_limit_entry")
    if not _positive(opening["prev_close"]):
        return finish("unresolved", "entry_price_limit_reference_missing")

    def auction(row, session, side):
        return opening_auction_executability(
            code=code, trade_date=session, side=side, open_price=row["open"],
            prev_close=row["prev_close"], tradable=row["trade_status"] == 1,
            is_risk_warning=row["is_st"] == 1, no_price_limit=row["no_price_limit"],
        )

    gate = auction(opening, entry_day, "buy")
    if not gate["executable"]:
        return finish("unfilled", gate["reason"])
    price, adv = opening["open"], opening["lagged_adv"]
    if not _positive(adv):
        return finish("unresolved", "entry_lagged_ADV_missing")
    budget = SPEC["event_cash_yuan_including_fees"]
    shares = constrain_share_quantity(budget / price, side="buy", liquidity_cap_shares=adv * .01 / price)["accepted_shares"]
    while shares:
        costs = historical_costs(shares * price, "buy", entry_day, adv, bps)
        if shares * price + costs["total"] <= budget:
            break
        shares -= 100
    if not shares:
        return finish("unfilled", "cash_or_liquidity_below_one_lot")
    entry_cash = shares * price + costs["total"]
    result.update(entry_day=entry_day, entry_shares=shares, remaining_shares=shares,
                  entry_cash_yuan=entry_cash, cost_yuan=costs["total"],
                  target_exit_day=market.sessions[target] if target < len(market.sessions) else None)
    result["execution_events"].append({"day": entry_day, "side": "buy", "shares": shares, "price": price, "costs": costs})
    proceeds = 0.0
    for index in range(start + 1, min(deadline + 1, len(market.sessions))):
        session = market.sessions[index]
        row = market.rows.get(session)
        if row is None:
            return finish("unresolved", "holding_session_market_row_missing")
        problem = _price_problem(row, opening)
        if problem:
            return finish("unresolved", problem)
        if index < target:
            continue
        if row["trade_status"] == 1 and not _positive(row["prev_close"]):
            return finish("unresolved", "exit_price_limit_reference_missing")
        gate = auction(row, session, "sell")
        if not gate["executable"] or row["no_price_limit"]:
            result["execution_events"].append({"day": session, "side": "sell", "shares": 0,
                                                "reason": gate["reason"] if not gate["executable"] else "special_no_limit_day"})
            continue
        if not _positive(row["lagged_adv"]):
            return finish("unresolved", "exit_lagged_ADV_missing")
        cap = int(row["lagged_adv"] * .01 / row["open"])
        quantity = constrain_share_quantity(shares, side="sell", available_to_sell=shares,
                                            full_liquidation=cap >= shares, liquidity_cap_shares=cap)["accepted_shares"]
        if not quantity:
            result["execution_events"].append({"day": session, "side": "sell", "shares": 0, "reason": "ADV_below_one_lot"})
            continue
        costs = historical_costs(quantity * row["open"], "sell", session, row["lagged_adv"], bps)
        proceeds += quantity * row["open"] - costs["total"]
        shares -= quantity
        result.update(remaining_shares=shares, cost_yuan=result["cost_yuan"] + costs["total"])
        result["execution_events"].append({"day": session, "side": "sell", "shares": quantity, "price": row["open"], "costs": costs})
        if not shares:
            pnl = proceeds - entry_cash
            result.update(exit_day=session, net_pnl_yuan=pnl, fixed_budget_return=pnl / budget,
                          invested_cash_return=pnl / entry_cash)
            return finish("closed", "fixed_five_session_exit" if index == target else "delayed_exit")
    return finish("unresolved", "exit_deadline_with_inventory" if deadline < len(market.sessions) else "input_horizon_with_inventory")


def weekly_metrics(events: pd.DataFrame, cohorts: pd.DataFrame) -> list[dict]:
    """Left-join predetermined groups and retain the original denominators."""
    keys = ["signal_day", "code"]
    if events.duplicated(keys + ["slippage_bps"]).any():
        raise ValueError("crowding_duplicate_event_identity")
    if not events.status.isin(["closed", "unfilled", "unresolved"]).all():
        raise ValueError("crowding_unknown_event_status")
    unresolved = events.status.eq("unresolved")
    if events.fixed_budget_return.isna().ne(unresolved).any():
        raise ValueError("crowding_return_status_mismatch")
    if not events.loc[~unresolved, "fixed_budget_return"].map(math.isfinite).all():
        raise ValueError("crowding_nonfinite_resolved_return")
    memberships = cohorts.set_index(keys).quintile
    if not memberships.index.is_unique or not memberships.isin(["top", "bottom"]).all():
        raise ValueError("crowding_duplicate_or_invalid_membership")
    result = []
    for (day, bps), group in events.groupby(["signal_day", "slippage_bps"], sort=True):
        selected = memberships.loc[memberships.index.get_level_values("signal_day") == day]
        if not len(selected):
            continue
        if group.signal_emotion.nunique() != 1 or len(group) != int(cohorts.loc[cohorts.signal_day.eq(day), "signal_universe_count"].iloc[0]):
            raise ValueError("crowding_universe_or_emotion_identity_mismatch")
        group = group.set_index(keys)
        if not selected.index.isin(group.index).all():
            raise ValueError("crowding_predetermined_member_missing")
        record = {"signal_day": day, "slippage_bps": int(bps), "signal_emotion": group.signal_emotion.iloc[0]}
        for label, subset in (("universe", group), ("top", group.loc[selected[selected.eq("top")].index]),
                              ("bottom", group.loc[selected[selected.eq("bottom")].index])):
            n_unknown = int(subset.status.eq("unresolved").sum())
            resolved = subset.fixed_budget_return.dropna()
            record[label] = {"signals": len(subset), "status_counts": dict(Counter(subset.status)),
                             "unresolved": n_unknown, "reason_counts": dict(Counter(subset.reason)),
                             "full_mean_return": None if n_unknown else float(resolved.mean()),
                             "resolved_only_mean_return": None if resolved.empty else float(resolved.mean())}
        for name, left, right in (("top_minus_bottom", "top", "bottom"), ("top_minus_universe", "top", "universe")):
            a, b = record[left]["full_mean_return"], record[right]["full_mean_return"]
            record[name] = None if a is None or b is None else a - b
        valid = group.loc[group.fixed_budget_return.notna(), ["factor", "fixed_budget_return"]]
        corr = None if len(valid) < 20 or valid.nunique().min() < 2 else float(valid.rank().corr().iloc[0, 1])
        record["resolved_only_rank_ic"] = corr
        record["full_rank_ic"] = None if record["universe"]["unresolved"] else corr
        result.append(record)
    return result


def emotion_fold_metrics(weeks: list[dict], folds: dict[str, list[str]]) -> list[dict]:
    """Time folds protect comparisons; emotion, not year, defines the route."""
    result = []
    frame = pd.DataFrame(weeks)
    for fold, (start, end) in folds.items():
        subset = frame.loc[frame.signal_day.between(start, end)]
        for (state, bps), group in subset.groupby(["signal_emotion", "slippage_bps"], sort=True):
            row = {"fold": fold, "signal_emotion": state, "slippage_bps": int(bps), "signal_weeks": len(group)}
            for label in ("universe", "top", "bottom"):
                members = group[label].tolist()
                row[label] = {"signals": sum(m["signals"] for m in members),
                              "unresolved": sum(m["unresolved"] for m in members)}
            for metric in ("top_minus_bottom", "top_minus_universe", "full_rank_ic"):
                values = group[metric].dropna()
                row[metric] = {"complete_weeks": len(values),
                               "all_weeks_mean": None if len(values) != len(group) else float(values.mean()),
                               "complete_weeks_only_mean": None if values.empty else float(values.mean())}
            row.update(historical_qualification_restored=False, production_eligible=False,
                       limitation="Resolved-only rows/weeks can be outcome-selected; not a qualification result.")
            result.append(row)
    return result
