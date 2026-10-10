"""Close the frozen liquidation test; do not repair its admission post hoc."""
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

from mom_trading.crowding_cash_settlement import cash_market_view, replay_with_cash
from mom_trading.crowding_exit_audit import SPEC, audit_blocked_exits, evidence_class
from mom_trading.crowding_factor_replay import EventMarket
from mom_trading.suspension_liquidity import suspension_adv_view
from research.run_emotion_pattern_controls import market_chunks
from research.run_emotion_pattern_replay import INPUTS, MARKET_STATE_PATH, sha, write_new
from research.run_crowding_suspension_liquidity_replay import PANEL, COVERAGE

PRIMARY = ROOT / "output/research/crowding_alias_replay_v1.json"
PRIMARY_SHA = "2a031fe9e18d8b28753bc04ea50dbdfe6edb6775d44b89f565ad95039554580a"


def run(output: Path, progress=print):
    output = output.resolve()
    started = output.with_suffix(".started.json")
    if not output.is_relative_to(ROOT):
        raise ValueError("exit_audit_output_outside_project")
    if any(p.exists() for p in (output, started, output.with_suffix(".failed.json"))):
        raise FileExistsError("exit audit attempt exists; do not overwrite")
    if sha(PRIMARY) != PRIMARY_SHA:
        raise ValueError("exit_audit_primary_identity_mismatch")
    prior = json.loads(PRIMARY.read_text())
    refs = prior["bindings"] + [prior["selection"], prior["event_ledger"]]
    refs += [{"path": str(PRIMARY.relative_to(ROOT)), "sha256": PRIMARY_SHA}]
    refs += [{"path": p, "sha256": sha(ROOT / p)} for p in (
        "mom_trading/crowding_exit_audit.py", "research/audit_crowding_deadline_inventory.py")]
    refs = list({r["path"]: r for r in refs}.values())
    for ref in refs:
        if sha(ROOT / ref["path"]) != ref["sha256"]:
            raise ValueError(f"exit_audit_binding_mismatch:{ref['path']}")
    write_new(started, {"status": "fixed_deadline_audit_started", "spec": SPEC, "bindings": refs})
    try:
        by_code, classes = defaultdict(list), Counter()
        identities = set()
        with (ROOT / prior["event_ledger"]["path"]).open() as stream:
            for line in stream:
                event = json.loads(line)
                key = event["signal_day"], event["code"], event["slippage_bps"]
                if key in identities:
                    raise ValueError("exit_audit_duplicate_event_identity")
                identities.add(key)
                classification = evidence_class(event)
                classes[event["slippage_bps"], event["signal_emotion"], event["quintile"], classification] += 1
                if classification == "known_open_inventory":
                    by_code[event["code"]].append(event)
        if len(identities) != prior["event_path_count"]:
            raise ValueError("exit_audit_population_changed")
        all_days = json.loads(INPUTS[2][0].read_text())["trading_dates"]
        sessions = [d for d in all_days if "2021-01-04" <= d <= "2025-12-31"]
        calendar_index = {pd.Timestamp(d): i for i, d in enumerate(all_days)}
        states = {r["trade_date"]: (r.get("features") or {}).get("confirmed_state", "UNKNOWN")
                  for r in json.loads(MARKET_STATE_PATH.read_text())["rows"]}
        special = pd.read_csv(INPUTS[3][0], dtype=str)
        normalize = lambda c: c.split(".")[-1] if c.startswith(("sh.", "sz.")) else c.split(".")[0]
        special_keys = {(normalize(r.code), pd.Timestamp(r.date)) for r in special.loc[special.verification_status.eq("verified")].itertuples(index=False)}
        actions = defaultdict(dict)
        for action in json.loads(COVERAGE.read_text())["action_evidence"].values():
            actions[action["code"]][action["ex_date"]] = action
        audited = []
        compare = ("status", "reason", "entry_day", "exit_day", "entry_shares", "remaining_shares",
                   "entry_cash_yuan", "cost_yuan", "net_pnl_yuan", "fixed_budget_return", "execution_events")
        for n, (code, stock) in enumerate(market_chunks(sorted(by_code), sessions, calendar_index, special_keys)):
            symbol = f"{code}.{'SH' if code.startswith('6') else 'SZ'}"
            canonical = pd.read_parquet(PANEL, filters=[("ts_code", "=", symbol)])
            canonical["day"] = pd.to_datetime(canonical.trade_date).dt.strftime("%Y-%m-%d")
            if canonical.day.duplicated().any():
                raise ValueError("exit_audit_duplicate_canonical_day")
            rows = canonical.set_index("day").to_dict("index")
            view, proofs = cash_market_view(EventMarket(stock, sessions, states), actions[code])
            view, _ = suspension_adv_view(view, canonical)
            for event in by_code[code]:
                replay = replay_with_cash(event, view, proofs, actions[code], bps=event["slippage_bps"])
                if any(replay.get(k) != event.get(k) for k in compare):
                    raise ValueError("exit_audit_frozen_outcome_not_reproduced")
                row = audit_blocked_exits(event, rows, sessions, proofs)
                relevant = [p for d, p in proofs.items() if event["entry_day"] <= p["record_date"] < d <= row["deadline"]]
                row["unsettled_cash_entitlement_dates"] = [p["ex_date"] for p in relevant]
                row["cash_tax_and_inventory_valuation_complete"] = False
                audited.append(row)
            if n % 20 == 0:
                progress(json.dumps({"phase": "deadline_inventory_audit", "instruments": n+1, "paths": len(audited)}), flush=True)
        expected = {(e["signal_day"], e["code"], e["slippage_bps"])
                    for code_events in by_code.values() for e in code_events}
        if {(e["signal_day"], e["code"], e["slippage_bps"]) for e in audited} != expected:
            raise ValueError("exit_audit_inventory_population_changed")
        failed_selected = Counter(e["slippage_bps"] for e in audited if e["quintile"] in {"top", "bottom"})
        for ref in refs:
            if sha(ROOT / ref["path"]) != ref["sha256"]:
                raise ValueError("exit_audit_source_changed_during_run")
        result = {
            "status": "fixed_deadline_audit_completed", "spec": SPEC, "bindings": refs,
            "source_event_ledger": prior["event_ledger"], "original_ledger_unchanged": True,
            "event_path_count": len(identities), "reproduced_and_audited_inventory_paths": len(audited),
            "classifications": [{"slippage_bps": b, "signal_emotion": e, "quintile": q, "evidence_class": c, "count": n}
                                for (b, e, q, c), n in sorted(classes.items())],
            "frozen_gate_by_cost": [{"slippage_bps": b, "selected_deadline_inventory": failed_selected[b],
                                     "all_selected_entries_and_exits_resolved": False if failed_selected[b] else None}
                                    for b in (10, 20, 30)],
            "inventory_paths": audited, "production_eligible": False, "generates_alert": False,
            "conclusion": "The original strict liquidation gate fails independently of remaining entitlement gaps. Do not extend the deadline, delete members, or relabel this as a fresh blind trial.",
            "limitations": ["This does not prove the factor unprofitable in every emotion.",
                            "No marked inventory or liquidation value is computed; unknown value is not zero.",
                            "Cash entitlements on unliquidated paths still need accounting; no assumed zero dividend or tax.",
                            "Other strategy routes must not inherit this factor's admission gate."],
        }
        write_new(output, result)
        return {k: v for k, v in result.items() if k not in {"bindings", "inventory_paths", "classifications"}}
    except Exception as exc:
        write_new(output.with_suffix(".failed.json"), {"status": "fixed_deadline_audit_failed", "error": str(exc)})
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(run(parser.parse_args().output), ensure_ascii=False, indent=2))
