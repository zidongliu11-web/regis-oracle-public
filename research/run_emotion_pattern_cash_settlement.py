"""Settle all original routed events without retuning or overwriting evidence."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from mom_trading.cash_dividend_evidence import reconcile_cash_reference
from mom_trading.emotion_pattern_cash_settlement import SPEC, replay_cash_event
from mom_trading.emotion_pattern_replay import EventMarket, replay_event, summarize_events
from research.run_emotion_pattern_replay import INPUTS, MARKET_STATE_PATH, sha, write_new
from research.run_emotion_pattern_controls import market_chunks, PRIMARY, PRIMARY_SHA
from research.audit_crowding_entitlement_coverage import MANIFEST, MANIFEST_SHA


def original_candidate(event, market):
    """Rebuild only the original execution inputs, never reselect a signal."""
    day = event["signal_day"]
    source = market.rows[day]
    risk = source["low"]
    if event["playbook_id"] == "mainline_dry_pullback":
        index = market.index[day]
        if index < 3:
            raise ValueError("pattern_original_stop_history_missing")
        risk = min(market.rows[d]["low"] * market.rows[d]["adj_factor"]
                   for d in market.sessions[index-3:index+1]) / source["adj_factor"]
    return {"code": event["code"], "trade_date": day, "playbook_id": event["playbook_id"],
            "confirmed_state": event["signal_emotion"], "reference_close": source["close"],
            "maximum_entry_open": source["close"] * 1.02, "risk_line": risk}


def action_evidence(market, actions):
    by_date = defaultdict(list)
    for action in actions:
        if action.get("ex_date") in market.index:
            by_date[action["ex_date"]].append(action)
    changed = set()
    for before, after in zip(market.sessions, market.sessions[1:]):
        left, right = market.rows.get(before), market.rows.get(after)
        if left and right and abs(left["adj_factor"] - right["adj_factor"]) > 1e-12:
            changed.add(after)
    proofs = {}
    for day in sorted(changed | set(by_date)):
        index = market.index[day]
        before = market.sessions[index-1] if index else None
        if before not in market.rows or day not in market.rows:
            proof = {"verified": False, "reason": "action_market_session_missing"}
        else:
            proof = reconcile_cash_reference(market.rows[before], market.rows[day], by_date[day], previous_session=before)
        proofs[day] = {**proof, "code": next(iter(market.rows.values()))["code"],
                       "ex_date": day, "factor_changed": day in changed, "official_actions": by_date[day]}
    return proofs


def run(output: Path, progress=print):
    output = output.resolve()
    if not output.is_relative_to(ROOT):
        raise ValueError("pattern_settlement_output_outside_project")
    started = output.with_suffix(".started.json")
    if output.exists() or started.exists():
        raise FileExistsError("Pattern settlement exists; do not overwrite or rerun")
    if sha(PRIMARY) != PRIMARY_SHA or sha(MANIFEST) != MANIFEST_SHA:
        raise ValueError("pattern_settlement_original_or_action_identity_changed")
    original = json.loads(PRIMARY.read_text())
    manifest = json.loads(MANIFEST.read_text())["queries"]
    if len({row["code"] for row in manifest}) != len(manifest):
        raise ValueError("pattern_official_response_duplicate")
    records = {row["code"]: row for row in manifest}
    refs = original["inputs"] + original["source_code"] + [
        {"path": str(PRIMARY.relative_to(ROOT)), "sha256": PRIMARY_SHA},
        {"path": str(MANIFEST.relative_to(ROOT)), "sha256": MANIFEST_SHA},
    ]
    for name in ("mom_trading/emotion_pattern_cash_settlement.py", "research/run_emotion_pattern_cash_settlement.py",
                 "mom_trading/cash_dividend_evidence.py", "mom_trading/crowding_cash_settlement.py",
                 "research/run_emotion_pattern_controls.py"):
        refs.append({"path": name, "sha256": sha(ROOT / name)})
    by_code = defaultdict(list)
    keys = set()
    for event in original["events"]:
        key = event["code"], event["signal_day"], event["playbook_id"], event["slippage_bps"]
        if key in keys:
            raise ValueError("pattern_source_event_duplicate")
        keys.add(key)
        by_code[event["code"]].append(event)
    responses = {}
    for code in sorted(by_code):
        record = records.get(code)
        if record is None:
            continue
        path = (ROOT / record["path"]).resolve()
        if not path.is_relative_to((MANIFEST.parent / "responses").resolve()) or sha(path) != record["sha256"]:
            raise ValueError("pattern_official_response_identity_mismatch")
        payload = json.loads(path.read_text())
        if payload.get("code") != code or payload.get("status") not in {"ok", "empty"}:
            raise ValueError("pattern_official_response_code_or_status_invalid")
        if any(row.get("code") != code for row in payload.get("rows", [])):
            raise ValueError("pattern_official_action_code_mismatch")
        responses[code] = payload.get("rows", [])
        refs.append({"path": record["path"], "sha256": record["sha256"]})
    for ref in refs:
        if sha(ROOT / ref["path"]) != ref["sha256"]:
            raise ValueError("pattern_settlement_binding_mismatch:" + ref["path"])
    write_new(started, {"status": "pattern_cash_settlement_started", "spec": SPEC, "bindings": refs})
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
        events, evidence = [], {}
        transitions = Counter()
        for offset, (code, stock) in enumerate(market_chunks(sorted(by_code), sessions, calendar_index, special_keys)):
            market = EventMarket(stock, sessions, state_map)
            proofs = action_evidence(market, responses.get(code, []))
            for old in by_code[code]:
                candidate = original_candidate(old, market)
                baseline = replay_event(candidate, market, slippage_bps=old["slippage_bps"])
                if baseline != {key: value for key, value in old.items() if key != "signal_factors"}:
                    raise ValueError("pattern_original_event_not_exactly_reproduced")
                event = replay_cash_event(candidate, market, proofs, bps=old["slippage_bps"], source_complete=code in responses)
                event.update(signal_factors=old["signal_factors"], prior_status=old["status"], prior_reason=old["reason"])
                events.append(event)
                transitions[old["slippage_bps"], old["status"], event["status"]] += 1
            evidence[code] = proofs
            if offset % 25 == 0:
                progress(json.dumps({"phase": "original_pattern_cash_settlement", "stocks": offset+1, "paths": len(events)}), flush=True)
        if len(events) != len(original["events"]):
            raise ValueError("pattern_settlement_original_population_changed")
        for ref in refs:
            if sha(ROOT / ref["path"]) != ref["sha256"]:
                raise ValueError("pattern_settlement_inputs_changed_during_run")
        result = {"status": "original_pattern_cash_diagnostic_complete_not_admitted", "spec": SPEC, "bindings": refs,
                  "signal_count": original["candidate_count"], "cost_path_count": len(events),
                  "all_original_paths_reproduced": True, "source_results_unchanged": True,
                  "response_missing_codes": sorted(set(by_code)-set(responses)),
                  "transitions": [{"slippage_bps": bps, "before": before, "after": after, "paths": count}
                                  for (bps, before, after), count in sorted(transitions.items())],
                  "summary_by_pattern_and_emotion": summarize_events(events),
                  "action_evidence": evidence, "events": events, "production_eligible": False,
                  "generates_alert": False, "portfolio_return": None,
                  "limitations": ["Seen-history accounting repair, not a new blind trial or strategy admission.",
                                  "Original nominal entry cap, stop and 2R remain unchanged, including ex-dividend days.",
                                  "Independent event budgets are not a shared account or portfolio.",
                                  "Mixed share distributions, missing evidence and unsettled cash remain unresolved.",
                                  "Only original routed events; the other control tracks and five-factor attribution are unchanged."]}
        write_new(output, result)
        return {key: value for key, value in result.items() if key not in {"bindings", "action_evidence", "events"}}
    except Exception as exc:
        write_new(output.with_suffix(".failed.json"), {"status": "pattern_cash_settlement_failed", "error": str(exc)})
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(run(parser.parse_args().output), ensure_ascii=False, indent=2))
