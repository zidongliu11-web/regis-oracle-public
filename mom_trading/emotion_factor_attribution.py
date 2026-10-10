"""Five auxiliary factors on fixed signal-first cohorts, not strategy admission."""
from __future__ import annotations

from collections import Counter
import math

import pandas as pd

from .emotion_pattern_research import FACTORS


SPEC = {
    "schema": "emotion_factor_signal_first_attribution.v1",
    "factors": [{"id": f["id"], "name": f["name"]} for f in FACTORS],
    "selection": "each_track_playbook_day_actual_emotion; finite_factor_only; before_outcomes",
    "groups": "high_and_low_ceil_20pct; factor_desc_code_asc; at_least_5_names_and_2_values",
    "input_missing": "count_and_retain_missing_inputs; never_fill_or_replace_from_outcomes",
    "return": "net_event_cash_PnL_divided_by_fixed_20000_budget_including_idle_cash",
    "unfilled": "zero_only_for_verified_no_entry_reason_with_zero_shares_and_no_orders",
    "unresolved": "retained; any_unresolved_member_suppresses_full_group_metric",
    "cost_scenarios_bps": [10, 20, 30],
    "folds": {"development_reuse": ["2021-01-01", "2022-12-31"],
              "validation_reuse": ["2023-01-01", "2024-12-31"],
              "holdout_already_seen": ["2025-01-01", "2025-12-31"]},
    "direction_selected": False, "parameter_tuning": False, "new_blind_trial": False,
    "statistical_inference": "descriptive_only; overlapping_events_and_multiple_tests_not_resolved",
    "portfolio_return": None, "production_eligible": False, "generates_alert": False,
}
KEYS = ["track", "playbook_id", "signal_day", "code"]
GROUP_KEYS = ["track", "playbook_id", "playbook_name", "signal_day", "signal_emotion"]
NO_ENTRY_REASONS = {"price_or_risk_boundary_failed", "cash_risk_or_liquidity_below_one_lot", "suspended_or_untradable"}


def split_inputs_and_outcomes(events: list[dict]) -> tuple[pd.DataFrame, dict]:
    """Require the same inputs and all three costs for every existing signal."""
    inputs, outcomes, costs = {}, {}, {}
    for e in events:
        key = tuple(e[k] for k in KEYS)
        bps = e["slippage_bps"]
        if bps not in SPEC["cost_scenarios_bps"] or (key, bps) in outcomes:
            raise ValueError("factor_attribution_duplicate_or_unknown_cost")
        row = {k: e[k] for k in set(KEYS + GROUP_KEYS)}
        for f in FACTORS:
            value = e["signal_factors"][f["id"]]
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise ValueError("factor_attribution_nonfinite_signal_input")
            row[f["id"]] = value
        if key in inputs and inputs[key] != row:
            raise ValueError("factor_attribution_costs_changed_signal_inputs")
        inputs[key] = row
        costs.setdefault(key, set()).add(bps)
        status, pnl = e["status"], e["net_pnl_yuan"]
        if status == "closed":
            if (pnl is None or not math.isfinite(pnl) or e["remaining_shares"] != 0
                    or e["simulated_entry_shares"] <= 0 or not e["execution_events"]):
                raise ValueError("factor_attribution_closed_cash_missing")
            cash, shares = 0., 0
            for order in e["execution_events"]:
                if not order["shares"]:
                    continue
                sign = -1 if order["side"] == "buy" else 1
                cash += sign*order["shares"]*order["price"] - order["costs"]["total"]
                shares -= sign*order["shares"]
            if shares or not math.isclose(cash, pnl, abs_tol=1e-7):
                raise ValueError("factor_attribution_closed_cash_does_not_reconcile")
            value = pnl / 20000.
        elif status == "unfilled":
            if (e["reason"] not in NO_ENTRY_REASONS or e["entry_day"] is not None
                    or e["simulated_entry_shares"] or e["remaining_shares"] or e["execution_events"]
                    or e["cost_yuan"] != 0 or pnl is not None):
                raise ValueError("factor_attribution_no_entry_not_verified")
            value = 0.
        elif status == "unresolved":
            if pnl is not None:
                raise ValueError("factor_attribution_unknown_return_filled")
            value = None
        else:
            raise ValueError("factor_attribution_status_unknown")
        outcomes[key, bps] = {"status": status, "reason": e["reason"], "fixed_budget_return": value}
    if any(c != {10, 20, 30} for c in costs.values()):
        raise ValueError("factor_attribution_cost_path_missing")
    frame = pd.DataFrame(inputs.values())
    if not frame.empty:
        if not frame.code.str.fullmatch(r"\d{6}").all():
            raise ValueError("factor_attribution_code_identity_invalid")
        if frame.groupby(["track", "playbook_id", "signal_day"]).signal_emotion.nunique().max() != 1:
            raise ValueError("factor_attribution_same_day_emotion_conflict")
    return frame, outcomes


def freeze_factor_groups(inputs: pd.DataFrame) -> list[dict]:
    """This function accepts signal fields only; it never reads return/status."""
    if inputs.empty:
        return []
    if inputs.duplicated(KEYS).any():
        raise ValueError("factor_attribution_duplicate_signal")
    result = []
    columns = list(dict.fromkeys(KEYS + GROUP_KEYS + [f["id"] for f in FACTORS]))
    for values, group in inputs[columns].groupby(GROUP_KEYS, sort=True):
        base = dict(zip(GROUP_KEYS, values))
        for f in FACTORS:
            ranked = group.loc[group[f["id"]].notna()].sort_values([f["id"], "code"], ascending=[False, True])
            n = len(ranked)
            eligible = n >= 5 and ranked[f["id"]].nunique() >= 2
            count = math.ceil(n * .2) if eligible else 0
            # Bottom uses the same complete ordering, including its code tie-break.
            result.append({**base, "factor_id": f["id"], "factor_name": f["name"],
                           "signal_universe_count": len(group), "factor_input_missing": len(group)-n,
                           "input_eligible": bool(eligible), "eligible_codes": ranked.code.tolist(),
                           "factor_values": ranked[f["id"]].astype(float).tolist(),
                           "high_codes": ranked.code.head(count).tolist(),
                           "low_codes": ranked.code.tail(count).tolist() if count else []})
    return result


def evaluate_factor_groups(groups: list[dict], outcomes: dict) -> list[dict]:
    result = []
    for g in groups:
        for bps in SPEC["cost_scenarios_bps"]:
            base = {k: g[k] for k in GROUP_KEYS + ["factor_id", "factor_name", "signal_universe_count", "factor_input_missing", "input_eligible"]}
            base["slippage_bps"] = bps
            for label, codes in (("universe", g["eligible_codes"]), ("high", g["high_codes"]), ("low", g["low_codes"])):
                rows = [outcomes[((g["track"], g["playbook_id"], g["signal_day"], code), bps)] for code in codes]
                returns = [r["fixed_budget_return"] for r in rows]
                complete = [v for v in returns if v is not None]
                base[label] = {"signals": len(rows), "status_counts": dict(Counter(r["status"] for r in rows)),
                               "unresolved": len(rows)-len(complete),
                               "full_mean_return": sum(complete)/len(rows) if rows and len(complete) == len(rows) else None}
            high, low = base["high"]["full_mean_return"], base["low"]["full_mean_return"]
            base["high_minus_low"] = high-low if high is not None and low is not None else None
            base["full_rank_ic"] = None
            if g["input_eligible"] and not base["universe"]["unresolved"]:
                values = [outcomes[((g["track"], g["playbook_id"], g["signal_day"], code), bps)]["fixed_budget_return"] for code in g["eligible_codes"]]
                pairs = pd.DataFrame({"factor": g["factor_values"], "return": values})
                if pairs["return"].nunique() >= 2:
                    base["full_rank_ic"] = float(pairs.rank().corr().iloc[0, 1])
            base.update(direction_selected=False, production_eligible=False)
            result.append(base)
    return result


def summarize_by_emotion(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    frame = pd.DataFrame(rows)
    result = []
    keys = ["track", "playbook_id", "playbook_name", "signal_emotion", "factor_id", "factor_name", "slippage_bps"]
    for fold, (start, end) in SPEC["folds"].items():
        for values, group in frame.loc[frame.signal_day.between(start, end)].groupby(keys, sort=True):
            row = {**dict(zip(keys, values)), "fold": fold, "signal_days": len(group),
                   "input_eligible_days": int(group.input_eligible.sum()),
                   "factor_input_missing": int(group.factor_input_missing.sum())}
            for metric in ("high_minus_low", "full_rank_ic"):
                valid = group[metric].dropna()
                row[metric] = {"complete_days": len(valid),
                               "all_signal_days_mean": float(valid.mean()) if len(valid) == len(group) else None,
                               "complete_days_only_diagnostic_mean": float(valid.mean()) if len(valid) else None}
            row.update(direction_selected=False, production_eligible=False, statistically_validated=False)
            result.append(row)
    return result
