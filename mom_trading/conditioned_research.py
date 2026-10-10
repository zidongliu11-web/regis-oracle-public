"""Evaluate frozen event cohorts by observable market state, without rerunning paths.

Daily direction is the sign of the observed cross-sectional median return. It
is a second descriptive dimension, not a retrospectively fitted trend regime.
"""
from collections import Counter, defaultdict
from datetime import date
import math
from statistics import fmean

from .emotion_factor_attribution import (
    SPEC as FACTOR_SPEC, split_inputs_and_outcomes, freeze_factor_groups,
    evaluate_factor_groups,
)

STATES = {"UNKNOWN": "情绪证据不足", "RISK_OFF": "情绪退潮", "STABILIZING": "弱修复",
          "RISK_ON": "情绪上升", "OVERHEATED": "高潮拥挤"}
DIRECTIONS = {"UP": "当日普涨", "DOWN": "当日普跌", "FLAT": "当日中位涨幅为零",
              "UNKNOWN": "涨跌数据不足", "ALL": "全部当日涨跌"}
FOLDS = {"all_history_reused": ["2021-01-04", "2025-12-31"], **FACTOR_SPEC["folds"]}
SPEC = {
    "schema": "conditioned_research.v1",
    "state": "original_input_confirmed_state_at_signal_close; never_selected_from_returns",
    "direction": "sign_of_same_day_B.median_return_pct; not_a_multi_day_trend_model",
    "episode": "consecutive_official_sessions_with_same_confirmed_state; missing_breaks_episode",
    "folds": FOLDS, "cost_scenarios_bps": [10, 20, 30],
    "event_budget_yuan": 20000, "return_denominator": "all_fixed_signal_budgets_including_unfilled",
    "unknown_outcome": "retain_signal_and_suppress_full_cohort_metric",
    "factor_selection": FACTOR_SPEC["groups"],
    "factor_increment": "high_group_minus_same_day_factor_eligible_universe; diagnostic_only",
    "historical_reuse": True, "unseen_out_of_sample": False,
    "statistical_inference": "none; episodes_and_overlapping_trades_are_not_independent_samples",
    "production_eligible": False, "generates_alert": False, "automatic_trading": False,
    "portfolio_return": None,
}


def market_context(rows, sessions):
    if sessions != sorted(set(sessions)) or any(date.fromisoformat(d).isoformat() != d for d in sessions):
        raise ValueError("conditioned_calendar_invalid")
    indexed = {}
    allowed = set(sessions)
    for row in rows:
        day = row["trade_date"]
        if day in indexed or day not in allowed:
            raise ValueError("conditioned_state_date_invalid")
        state = row.get("features", {}).get("confirmed_state", "UNKNOWN")
        if state not in STATES:
            raise ValueError("conditioned_state_unknown_label")
        value = row.get("B", {}).get("median_return_pct")
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
            raise ValueError("conditioned_direction_invalid")
        direction = "UNKNOWN" if value is None else "UP" if value > 0 else "DOWN" if value < 0 else "FLAT"
        indexed[day] = {"signal_emotion": state, "market_direction": direction}
    contexts, previous, episode = {}, None, None
    for day in sessions:
        row = indexed.get(day, {"signal_emotion": "UNKNOWN", "market_direction": "UNKNOWN"})
        state = row["signal_emotion"]
        if state != previous or day not in indexed:
            episode = day
        contexts[day] = {**row, "emotion_episode": episode, "market_row_present": day in indexed}
        previous = state if day in indexed else None
    return contexts


def prepare(events, rows, sessions):
    contexts = market_context(rows, sessions)
    tracked = [{**event, "track": "original_routed"} for event in events]
    inputs, outcomes = split_inputs_and_outcomes(tracked)
    for event in tracked:
        day = event["signal_day"]
        if day not in contexts or not FOLDS["all_history_reused"][0] <= day <= FOLDS["all_history_reused"][1]:
            raise ValueError("conditioned_signal_date_invalid")
        if event["signal_emotion"] != contexts[day]["signal_emotion"]:
            raise ValueError("conditioned_signal_emotion_mismatch")
        entry, end = event.get("entry_day"), event.get("exit_day")
        if entry is not None and (entry not in contexts or entry <= day):
            raise ValueError("conditioned_entry_not_after_signal")
        if end is not None and (entry is None or end not in contexts or end <= entry):
            raise ValueError("conditioned_exit_not_T_plus_1")
    return inputs, outcomes, contexts


def grouping(rows, contexts):
    grouped = defaultdict(list)
    for row in rows:
        context = contexts[row["signal_day"]]
        for fold, (start, end) in FOLDS.items():
            if start <= row["signal_day"] <= end:
                for direction in ("ALL", context["market_direction"]):
                    key = (row["playbook_id"], row["playbook_name"], row["signal_emotion"], direction, fold, row["slippage_bps"])
                    grouped[key].append(row)
    return grouped


def dimensions(key, rows, contexts):
    result = dict(zip(("playbook_id", "playbook_name", "signal_emotion", "market_direction", "fold", "slippage_bps"), key))
    result.update(signal_days=len({r["signal_day"] for r in rows}),
                  emotion_episodes=len({contexts[r["signal_day"]]["emotion_episode"] for r in rows}),
                  production_eligible=False, statistically_validated=False)
    return result


def conditional_summary(inputs, outcomes, contexts, playbooks):
    rows = []
    for signal in inputs.to_dict("records"):
        key = tuple(signal[k] for k in ("track", "playbook_id", "signal_day", "code"))
        for bps in SPEC["cost_scenarios_bps"]:
            rows.append({**signal, **outcomes[key, bps], "slippage_bps": bps})
    summaries = []
    for key, group in sorted(grouping(rows, contexts).items()):
        values = [r["fixed_budget_return"] for r in group]
        known = [v for v in values if v is not None]
        complete = len(known) == len(values)
        summaries.append({**dimensions(key, group, contexts), "signals": len(group),
                          "status_counts": dict(Counter(r["status"] for r in group)),
                          "reason_counts": dict(Counter(r["reason"] for r in group)),
                          "full_cohort_mean_return": fmean(known) if complete else None,
                          "full_cohort_positive_fraction": sum(v > 0 for v in known)/len(values) if complete else None,
                          "known_only_diagnostic_mean_return": fmean(known) if known else None,
                          "unresolved": len(values)-len(known),
                          "evidence_status": "historical_diagnostic_only" if complete else "unresolved_outcomes"})
    coverage = []
    records = inputs.to_dict("records")
    for playbook in playbooks:
        if playbook["kind"] != "entry_research":
            continue
        for state in STATES:
            for direction in DIRECTIONS:
                days = {d for d, c in contexts.items() if FOLDS["all_history_reused"][0] <= d <= FOLDS["all_history_reused"][1]
                        and c["signal_emotion"] == state and (direction == "ALL" or c["market_direction"] == direction)}
                cohort = [r for r in records if r["playbook_id"] == playbook["id"] and r["signal_day"] in days]
                coverage.append({"playbook_id": playbook["id"], "playbook_name": playbook["name"],
                                 "signal_emotion": state, "market_direction": direction,
                                 "market_sessions": len(days), "signals": len(cohort),
                                 "configured_for_state": state in playbook["states"],
                                 "status": "observed" if cohort else "no_signal_sample",
                                 "production_eligible": False})
    return {"conditional_summaries": summaries, "coverage": coverage}


def factor_summary(groups, outcomes, contexts):
    daily = evaluate_factor_groups(groups, outcomes)
    for row in daily:
        high, universe = row["high"]["full_mean_return"], row["universe"]["full_mean_return"]
        row["high_minus_universe"] = high-universe if high is not None and universe is not None else None
    summaries = []
    for key, rows in sorted(grouping(daily, contexts).items()):
        factors = defaultdict(list)
        for row in rows:
            factors[row["factor_id"], row["factor_name"]].append(row)
        for (factor_id, factor_name), group in sorted(factors.items()):
            summary = {**dimensions(key, group, contexts), "factor_id": factor_id, "factor_name": factor_name,
                       "input_eligible_days": sum(r["input_eligible"] for r in group),
                       "factor_input_missing": sum(r["factor_input_missing"] for r in group),
                       "direction_selected": False}
            for metric in ("high_minus_universe", "high_minus_low", "full_rank_ic"):
                values = [r[metric] for r in group if r[metric] is not None]
                summary[metric] = {"complete_days": len(values),
                                   "all_signal_days_mean": fmean(values) if len(values) == len(group) else None,
                                   "complete_days_only_diagnostic_mean": fmean(values) if values else None}
            summaries.append(summary)
    return {"factor_summaries": summaries, "daily_metrics": daily}


def analyze(events, state_rows, sessions, playbooks, *, lane):
    inputs, outcomes, contexts = prepare(events, state_rows, sessions)
    if lane == "conditional":
        result = conditional_summary(inputs, outcomes, contexts, playbooks)
    elif lane == "factor":
        groups = freeze_factor_groups(inputs)
        result = {**factor_summary(groups, outcomes, contexts), "frozen_factor_groups": groups}
    else:
        raise ValueError("conditioned_lane_invalid")
    return {"schema": SPEC["schema"], "spec": SPEC, "lane": lane,
            "candidate_count": len(inputs), "costed_event_count": len(events), **result,
            "state_labels": STATES, "direction_labels": DIRECTIONS,
            "production_eligible": False, "generates_alert": False, "portfolio_return": None,
            "admission_blockers": ["历史数据已见，不能作为新样本外证据", "事件共享日期和市场风险，尚未形成账户组合及相关性校正",
                                   "未完成基线对照、试验家族校正与真实前向验证", "本次涨跌分组是新增的描述性复核，不具备事前冻结收益目标"],
            "interpretation": "按信号形成时的情绪和当日市场涨跌评价；成本情景、重复计算与重叠分组不增加独立样本。"}
