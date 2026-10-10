"""Cash repair of all already-seen controls and their frozen factor groups.

No new signals, direction selection, blind trial, or change to original rules.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from copy import copy
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from mom_trading.emotion_pattern_replay import EventMarket, replay_event
from mom_trading.emotion_pattern_research import PLAYBOOKS
from mom_trading.emotion_pattern_cash_settlement import SPEC as CASH_SPEC, replay_cash_event
from mom_trading.emotion_factor_attribution import (
    split_inputs_and_outcomes, freeze_factor_groups, evaluate_factor_groups, summarize_by_emotion,
)
from research.run_emotion_pattern_replay import INPUTS, MARKET_STATE_PATH, sha, write_new
from research.run_emotion_pattern_controls import market_chunks, replay_control
from research.run_emotion_pattern_cash_settlement import action_evidence, original_candidate
from research.run_emotion_factor_attribution import PRIMARY, PRIMARY_SHA
from research.audit_crowding_entitlement_coverage import MANIFEST, MANIFEST_SHA

ATTRIBUTION = ROOT / "output/research/emotion_factor_attribution_v1.json"
ATTRIBUTION_SHA = "6abf5e667cf3bdacea67829df1cfc53c376762d58bfee962f8cd5cbdb7b7e9f2"
SPEC = {**CASH_SPEC, "schema": "emotion_control_cash_settlement.v1",
        "scope": "all_four_original_tracks_all_five_factors_all_three_costs",
        "selection": "byte_bound_original_factor_groups_never_reselected_from_outcomes",
        "original_routed_only_limit_removed": True}


def cash_outcomes(old_events, new_events):
    """Validate immutable signal inputs before attaching cash-inclusive labels."""
    inputs, original_outcomes = split_inputs_and_outcomes(old_events)
    key = lambda row: ((row["track"], row["playbook_id"], row["signal_day"], row["code"]), row["slippage_bps"])
    old_map = {key(row): row for row in old_events}
    result = {}
    for row in new_events:
        identity = key(row)
        old = old_map.get(identity)
        if old is None or identity in result:
            raise ValueError("cash_attribution_event_identity_changed")
        for field in ("signal_factors", "signal_emotion", "playbook_name"):
            if old[field] != row[field]:
                raise ValueError("cash_attribution_signal_fields_changed")
        if row["status"] == "closed":
            if row.get("cash_settlement_complete") is not True or row["remaining_shares"]:
                raise ValueError("cash_attribution_closed_without_settlement")
            # Validate prices/orders with the frozen checker, separately from
            # dividends. It receives a copy, never altered source evidence.
            cash = sum(item["gross_yuan"] - item["tax_yuan"] for item in row["cash_entitlements"])
            if not math.isclose(row["net_pnl_yuan"], row["price_only_net_pnl_yuan"] + cash, abs_tol=1e-7):
                raise ValueError("cash_attribution_entitlements_do_not_reconcile")
            value = row["net_pnl_yuan"] / 20000.
        elif row["status"] == "unfilled":
            value = 0.
        elif row["status"] == "unresolved":
            value = None
        else:
            raise ValueError("cash_attribution_unknown_status")
        result[identity] = {"status": row["status"], "reason": row["reason"], "fixed_budget_return": value}
    if set(result) != set(original_outcomes):
        raise ValueError("cash_attribution_population_changed")
    # Whole-population check preserves the required three cost scenarios.
    price_events = [{**row, "net_pnl_yuan": row.get("price_only_net_pnl_yuan", row["net_pnl_yuan"])}
                    if row["status"] == "closed" else row for row in new_events]
    verified_inputs, _ = split_inputs_and_outcomes(price_events)
    if not verified_inputs.equals(inputs):
        raise ValueError("cash_attribution_input_population_changed")
    return inputs, result


def repair_control(old, market, proofs, *, source_complete):
    candidate = original_candidate(old, market)
    track, bps = old["track"], old["slippage_bps"]
    if track == "original_routed":
        baseline = replay_event(candidate, market, slippage_bps=bps)
    else:
        baseline = replay_control(candidate, market, bps=bps, track=track)
    if any(old.get(field) != value for field, value in baseline.items()):
        raise ValueError("cash_control_original_path_not_reproduced")
    stop = old["exit_day"] if old["status"] == "closed" else (
        market.sessions[min(market.index[old["signal_day"]]+1, len(market.sessions)-1)]
        if old["status"] == "unfilled" else market.sessions[-1])
    dates = sorted(proofs)
    affected = bisect_right(dates, stop) > bisect_right(dates, old["signal_day"])
    if not affected and source_complete:
        # No source action or factor jump anywhere in the entire execution
        # window: preserve fills and returns exactly rather than replaying.
        row = {**old, "cash_settlement_complete": old["status"] == "closed", "cash_entitlements": [],
               "price_only_net_pnl_yuan": old["net_pnl_yuan"]}
        return row
    view = market
    if track in {"no_emotion", "same_entries_no_emotion_exit"}:
        allowed = next(p["states"][0] for p in PLAYBOOKS if p["id"] == old["playbook_id"])
        view = copy(market)
        view.states = dict.fromkeys(market.sessions, allowed)
        candidate["confirmed_state"] = allowed
    fixed = replay_cash_event(candidate, view, proofs, bps=bps, source_complete=source_complete)
    return {**old, **fixed, "signal_emotion": old["signal_emotion"],
            "prior_status": old["status"], "prior_reason": old["reason"]}


def run(output: Path, progress=print):
    output = output.resolve()
    started, ledger = output.with_suffix(".started.json"), output.with_suffix(".events.jsonl")
    if not output.is_relative_to(ROOT) or any(p.exists() for p in (output, started, ledger)):
        raise ValueError("cash_control_output_outside_project_or_attempt_exists")
    for path, digest in ((PRIMARY, PRIMARY_SHA), (ATTRIBUTION, ATTRIBUTION_SHA), (MANIFEST, MANIFEST_SHA)):
        if sha(path) != digest:
            raise ValueError("cash_control_source_identity_changed")
    prior = json.loads(PRIMARY.read_text())
    attribution = json.loads(ATTRIBUTION.read_text())
    refs = prior["bindings"] + [prior["event_ledger"], attribution["selection"]]
    for path in (PRIMARY, ATTRIBUTION, MANIFEST, Path(__file__),
                 ROOT / "mom_trading/emotion_pattern_cash_settlement.py",
                 ROOT / "research/run_emotion_pattern_cash_settlement.py",
                 ROOT / "mom_trading/emotion_factor_attribution.py",
                 ROOT / "mom_trading/crowding_cash_settlement.py", ROOT / "mom_trading/cash_dividend_evidence.py"):
        refs.append({"path": str(path.relative_to(ROOT)), "sha256": sha(path)})
    refs = list({ref["path"]: ref for ref in refs}.values())
    for ref in refs:
        if sha(ROOT / ref["path"]) != ref["sha256"]:
            raise ValueError("cash_control_input_binding_changed")
    old_events = [json.loads(line) for line in (ROOT / prior["event_ledger"]["path"]).open()]
    old_inputs, _ = split_inputs_and_outcomes(old_events)
    groups = json.loads((ROOT / attribution["selection"]["path"]).read_text())["groups"]
    if freeze_factor_groups(old_inputs) != groups or len(old_events) != prior["path_count"]:
        raise ValueError("cash_control_original_factor_groups_changed")
    records = json.loads(MANIFEST.read_text())["queries"]
    if len({row["code"] for row in records}) != len(records):
        raise ValueError("cash_control_official_response_duplicate")
    records = {row["code"]: row for row in records}
    by_code = defaultdict(list)
    for row in old_events:
        by_code[row["code"]].append(row)
    responses = {}
    for code in sorted(by_code):
        record = records.get(code)
        if record is None:
            continue
        path = (ROOT / record["path"]).resolve()
        if not path.is_relative_to((MANIFEST.parent / "responses").resolve()) or sha(path) != record["sha256"]:
            raise ValueError("cash_control_official_response_identity_mismatch")
        payload = json.loads(path.read_text())
        if payload.get("code") != code or payload.get("status") not in {"ok", "empty"}:
            raise ValueError("cash_control_official_response_invalid")
        actions = payload.get("rows", [])
        if any(action.get("code") != code for action in actions):
            raise ValueError("cash_control_official_action_code_mismatch")
        responses[code] = actions
        refs.append({"path": record["path"], "sha256": record["sha256"]})
    write_new(started, {"status": "all_control_cash_settlement_started", "spec": SPEC, "bindings": refs})
    try:
        all_days = json.loads(INPUTS[2][0].read_text())["trading_dates"]
        sessions = [day for day in all_days if "2021-01-04" <= day <= "2025-12-31"]
        calendar_index = {pd.Timestamp(day): index for index, day in enumerate(all_days)}
        states = json.loads(MARKET_STATE_PATH.read_text())["rows"]
        state_map = {row["trade_date"]: row["features"].get("confirmed_state", "UNKNOWN") for row in states}
        special = pd.read_csv(INPUTS[3][0], dtype=str)
        def code6(value):
            return value.split(".")[-1] if value.startswith(("sh.", "sz.")) else value.split(".")[0]
        special_keys = {(code6(row.code), pd.Timestamp(row.date)) for row in special.loc[special.verification_status.eq("verified")].itertuples(index=False)}
        fixed, transitions = {}, Counter()
        with ledger.open("x", encoding="utf-8") as stream:
            for offset, (code, stock) in enumerate(market_chunks(sorted(by_code), sessions, calendar_index, special_keys)):
                market = EventMarket(stock, sessions, state_map)
                proofs = action_evidence(market, responses.get(code, []))
                for old in by_code[code]:
                    row = repair_control(old, market, proofs, source_complete=code in responses)
                    identity = (old["track"], old["playbook_id"], old["signal_day"], code, old["slippage_bps"])
                    fixed[identity] = row
                    stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+"\n")
                    transitions[old["track"], old["slippage_bps"], old["status"], row["status"]] += 1
                if offset % 100 == 0:
                    progress(json.dumps({"phase": "all_control_cash_settlement", "stocks": offset+1, "paths": len(fixed)}), flush=True)
        new_events = [fixed[(row["track"], row["playbook_id"], row["signal_day"], row["code"], row["slippage_bps"])] for row in old_events]
        _, outcomes = cash_outcomes(old_events, new_events)
        metrics = evaluate_factor_groups(groups, outcomes)
        for ref in refs:
            if sha(ROOT / ref["path"]) != ref["sha256"]:
                raise ValueError("cash_control_input_changed_during_run")
        result = {"status": "all_control_cash_and_factor_diagnostic_complete_not_admitted", "spec": SPEC, "bindings": refs,
                  "event_ledger": {"path": str(ledger.relative_to(ROOT)), "sha256": sha(ledger)},
                  "signal_count": len(old_inputs), "cost_path_count": len(new_events),
                  "selection": attribution["selection"], "factor_groups_unchanged": True,
                  "factor_count": 5, "factor_day_cost_count": len(metrics),
                  "summaries": summarize_by_emotion(metrics), "daily_metrics": metrics,
                  "transitions": [{"track": track, "slippage_bps": bps, "before": before, "after": after, "paths": count}
                                  for (track, bps, before, after), count in sorted(transitions.items())],
                  "source_results_unchanged": True, "production_eligible": False, "generates_alert": False, "portfolio_return": None,
                  "limitations": ["All historical tracks already seen; no fresh blind trial, direction selection or tuning.",
                                  "Unsupported corporate actions and remaining execution gaps keep full-group returns unknown.",
                                  "Complete-day subsets cannot qualify factors; independent events are not portfolio returns.",
                                  "Original raw-price thresholds unchanged; this does not optimise ex-dividend risk lines."]}
        write_new(output, result)
        return {key: value for key, value in result.items() if key not in {"bindings", "summaries", "daily_metrics"}}
    except Exception as exc:
        write_new(output.with_suffix(".failed.json"), {"status": "all_control_cash_settlement_failed", "error": str(exc)})
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(run(parser.parse_args().output), ensure_ascii=False, indent=2))
