"""Complete proven cash entitlements without replacing any frozen cohort."""
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

from mom_trading.crowding_cash_settlement import SPEC, cash_market_view, replay_with_cash
from mom_trading.crowding_factor_validation import freeze_quintiles
from mom_trading.crowding_factor_replay import EventMarket, weekly_metrics, emotion_fold_metrics
from research.run_emotion_pattern_replay import INPUTS, MARKET_STATE_PATH, sha, write_new
from research.run_emotion_pattern_controls import market_chunks
from research.audit_crowding_entitlement_coverage import PRIMARY, PRIMARY_SHA

COVERAGE = ROOT / "output/research/crowding_entitlement_coverage_v1.json"
COVERAGE_SHA = "3b90ae4b48ee11689e7f55a255433eb5764a44a208a4b322378ee13f6aac1951"


def run(output: Path, progress=print):
    output = output.resolve()
    if not output.is_relative_to(ROOT):
        raise ValueError("cash_settlement_output_outside_project")
    started, ledger = output.with_suffix(".started.json"), output.with_suffix(".events.jsonl")
    if any(p.exists() for p in (output, started, ledger)):
        raise FileExistsError("cash settlement attempt exists; do not overwrite")
    if sha(PRIMARY) != PRIMARY_SHA or sha(COVERAGE) != COVERAGE_SHA:
        raise ValueError("cash_settlement_primary_or_coverage_identity_mismatch")
    coverage = json.loads(COVERAGE.read_text())
    primary = json.loads(PRIMARY.read_text())
    refs = coverage["bindings"] + coverage["official_response_bindings"] + [
        {"path": str(COVERAGE.relative_to(ROOT)), "sha256": COVERAGE_SHA}]
    refs += [{"path": path, "sha256": sha(ROOT / path)} for path in (
        "mom_trading/crowding_cash_settlement.py", "research/run_crowding_cash_entitlement_replay.py")]
    for ref in refs:
        if sha(ROOT / ref["path"]) != ref["sha256"]:
            raise ValueError(f"cash_settlement_binding_mismatch:{ref['path']}")
    write_new(started, {"status": "known_history_cash_settlement_started", "spec": SPEC, "bindings": refs,
                        "all_cohorts_unchanged": True})
    try:
        affected = {(r["signal_day"], r["code"]) for r in coverage["affected_paths"]}
        by_code = defaultdict(list)
        with (ROOT / primary["event_ledger"]["path"]).open() as stream:
            for line in stream:
                event = json.loads(line)
                if (event["signal_day"], event["code"]) in affected:
                    by_code[event["code"]].append(event)
        all_days = json.loads(INPUTS[2][0].read_text())["trading_dates"]
        sessions = [day for day in all_days if "2021-01-04" <= day <= "2025-12-31"]
        calendar_index = {pd.Timestamp(day): i for i, day in enumerate(all_days)}
        document = json.loads(MARKET_STATE_PATH.read_text())
        state_map = {r["trade_date"]: (r.get("features") or {}).get("confirmed_state", "UNKNOWN") for r in document["rows"]}
        special = pd.read_csv(INPUTS[3][0], dtype=str)
        def normalize(code):
            return code.split(".")[-1] if code.startswith(("sh.", "sz.")) else code.split(".")[0]
        special_keys = {(normalize(r.code), pd.Timestamp(r.date)) for r in special.loc[special.verification_status.eq("verified")].itertuples(index=False)}
        action_map = defaultdict(dict)
        for proof in coverage["action_evidence"].values():
            action_map[proof["code"]][proof["ex_date"]] = proof
        revised, transitions = {}, Counter()
        for n, (code, stock) in enumerate(market_chunks(sorted(by_code), sessions, calendar_index, special_keys)):
            raw = EventMarket(stock, sessions, state_map)
            view, proofs = cash_market_view(raw, action_map[code])
            for old in by_code[code]:
                event = replay_with_cash(old, view, proofs, action_map[code], bps=old["slippage_bps"])
                event.update(quintile=old["quintile"], prior_status=old["status"], prior_reason=old["reason"],
                             cash_entitlement_review="affected_path_replayed")
                key = old["signal_day"], code, old["slippage_bps"]
                revised[key] = event
                transitions[old["slippage_bps"], old["status"], event["status"]] += 1
            if n % 200 == 0:
                progress(json.dumps({"phase": "cash_settlement", "instruments_done": n+1, "replayed_paths": len(revised)}), flush=True)
        if len(revised) != len(affected) * 3:
            raise ValueError("cash_settlement_affected_population_missing")
        compact, credited = [], Counter()
        keep = ["signal_day", "code", "factor", "signal_emotion", "slippage_bps", "status", "reason", "fixed_budget_return"]
        with ledger.open("x") as target, (ROOT / primary["event_ledger"]["path"]).open() as source:
            for line in source:
                old = json.loads(line)
                key = old["signal_day"], old["code"], old["slippage_bps"]
                event = revised.get(key)
                if event is None:
                    event = {**old, "cash_entitlement_review": "no_action_detected_in_original_window"}
                target.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
                compact.append({k: event[k] for k in keep})
                if event.get("dividend_gross_yuan", 0) > 0:
                    credited[old["slippage_bps"]] += 1
        if len(compact) != primary["event_path_count"]:
            raise ValueError("cash_settlement_full_population_changed")
        signals = pd.read_csv(ROOT / primary["selection"]["path"], dtype={"code": str})
        cohorts = freeze_quintiles(signals)
        expected = set(signals.loc[signals.quintile.isin(["top", "bottom"]), ["signal_day", "code", "quintile"]].itertuples(index=False, name=None))
        if set(cohorts[["signal_day", "code", "quintile"]].itertuples(index=False, name=None)) != expected:
            raise ValueError("cash_settlement_cohorts_changed")
        frame = pd.DataFrame(compact)
        weeks = weekly_metrics(frame, cohorts)
        protocol = json.loads((ROOT / "research/RECENT_LIMIT_HIT_CROWDING_V1_PROTOCOL.json").read_text())
        folds = {name: protocol["evaluation"][key] for name, key in (
            ("development", "development_diagnostic_no_tuning"), ("validation", "validation"), ("sealed_test", "sealed_test"))}
        for ref in refs:
            if sha(ROOT / ref["path"]) != ref["sha256"]:
                raise ValueError("cash_settlement_input_or_source_changed")
        result = {"schema": SPEC["schema"], "status": "cash_entitlement_diagnostic_completed_not_admitted",
                  "spec": SPEC, "bindings": refs, "signal_count": len(signals), "event_path_count": len(frame),
                  "selected_members": len(cohorts), "affected_signal_count": len(affected), "selection": primary["selection"],
                  "event_ledger": {"path": str(ledger.relative_to(ROOT)), "sha256": sha(ledger)},
                  "replayed_transitions": [{"slippage_bps": bps, "before": before, "after": after, "count": count}
                                           for (bps, before, after), count in sorted(transitions.items())],
                  "cash_credited_events_by_cost": dict(credited),
                  "execution_by_cost": [{"slippage_bps": int(bps), "status_counts": dict(Counter(group.status)),
                                         "reason_counts": dict(Counter(group.reason))} for bps, group in frame.groupby("slippage_bps")],
                  "weekly_metrics": weeks, "emotion_fold_metrics": emotion_fold_metrics(weeks, folds),
                  "historical_qualification_restored": False, "production_eligible": False, "generates_alert": False,
                  "portfolio_return": None,
                  "limitations": ["Cash-only; mixed stock distributions and source disagreement retained as unresolved.",
                                  "Cash and tax timing are historical simulation, not actual broker settlement.",
                                  "Not a fresh blind trial; no parameter or emotion selection based on repaired returns."]}
        write_new(output, result)
        return {k: v for k, v in result.items() if k not in {"bindings", "weekly_metrics", "emotion_fold_metrics"}}
    except Exception as exc:
        write_new(output.with_suffix(".failed.json"), {"status": "cash_settlement_failed", "error": str(exc)})
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(run(parser.parse_args().output), ensure_ascii=False, indent=2))
