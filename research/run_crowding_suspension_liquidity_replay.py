"""Repair explicit suspension gaps without reselecting a single factor member.

Only paths stopped at missing ADV can change: a closed or known-unfilled path
never consumed a missing ADV in the frozen engine. Reproduce each old failure
before correcting its input; keep the entire event ledger and denominators.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from mom_trading.suspension_liquidity import SPEC, suspension_adv_view
from mom_trading.crowding_cash_settlement import cash_market_view, replay_with_cash
from mom_trading.crowding_factor_replay import EventMarket, weekly_metrics, emotion_fold_metrics
from mom_trading.crowding_factor_validation import freeze_quintiles
from research.run_emotion_pattern_replay import INPUTS, MARKET_STATE_PATH, sha, write_new
from research.run_emotion_pattern_controls import market_chunks

PRIMARY = ROOT / "output/research/crowding_cash_entitlement_replay_v1.json"
PRIMARY_SHA = "5c15ad18365749ded2c5aaa44f28e67d7ea95d0f9df1e0ff939cd8016dc73863"
PANEL = ROOT / "research/backtest_project/data/universe_daily.parquet"
PANEL_SHA = "505fefc847edbfa4cefaaf56a50ba9636e50098a88fbbabb738103ea30849495"
COVERAGE = ROOT / "output/research/crowding_entitlement_coverage_v1.json"
COVERAGE_SHA = "3b90ae4b48ee11689e7f55a255433eb5764a44a208a4b322378ee13f6aac1951"
FAILURES = {"entry_lagged_ADV_missing", "exit_lagged_ADV_missing"}


def audit_changed(event: dict, panel: pd.DataFrame, sessions: list[str]):
    """Independent window and cash/share arithmetic; does not call the ADV repair."""
    rows = panel.assign(day=pd.to_datetime(panel.trade_date).dt.strftime("%Y-%m-%d")).set_index("day")
    for receipt in event["liquidity_repairs"]:
        day = receipt["execution_day"]
        index = sessions.index(day)
        days = sessions[index-20:index]
        assert len(days) == receipt["denominator"] == 20
        assert (days[0], days[-1]) == (receipt["window_start"], receipt["window_end"])
        sample = rows.loc[days]
        suspended = sample.trade_status.eq(0)
        assert sample.trade_status.isin([0, 1]).all()
        assert sample.loc[suspended, ["amount", "vol"]].fillna(0).eq(0).all().all()
        assert sample.loc[~suspended, ["amount", "vol"]].notna().all().all()
        amounts = sample.amount.mask(suspended, 0)
        assert amounts.ge(0).all()
        expected = amounts.sum() / 20
        assert math.isclose(receipt["ADV_yuan"], expected, rel_tol=1e-12)
        assert receipt["confirmed_no_trade_days"] == list(sample.index[suspended])
    orders = [r for r in event["execution_events"] if r["shares"]]
    for order in orders:
        assert order["price"] == rows.loc[order["day"], "open"]
        assert math.isclose(order["costs"]["total"], sum(v for k, v in order["costs"].items() if k != "total"), rel_tol=1e-12)
    shares = sum(r["shares"] * (1 if r["side"] == "buy" else -1) for r in orders)
    assert shares == event["remaining_shares"]
    if event["status"] == "closed":
        assert shares == 0
        cash = sum(r["shares"] * r["price"] * (-1 if r["side"] == "buy" else 1) - r["costs"]["total"] for r in orders)
        cash += event.get("dividend_gross_yuan", 0) - event.get("dividend_tax_yuan", 0)
        assert math.isclose(cash, event["net_pnl_yuan"], abs_tol=1e-8)
        assert math.isclose(cash / 10000, event["fixed_budget_return"], abs_tol=1e-12)


def run(output: Path, progress=print):
    output = output.resolve()
    if not output.is_relative_to(ROOT):
        raise ValueError("liquidity_output_outside_project")
    started, ledger = output.with_suffix(".started.json"), output.with_suffix(".events.jsonl")
    if any(p.exists() for p in (output, started, ledger, output.with_suffix(".failed.json"))):
        raise FileExistsError("liquidity attempt exists; do not overwrite")
    if sha(PRIMARY) != PRIMARY_SHA or sha(PANEL) != PANEL_SHA or sha(COVERAGE) != COVERAGE_SHA:
        raise ValueError("liquidity_primary_input_identity_mismatch")
    prior = json.loads(PRIMARY.read_text())
    refs = prior["bindings"] + [prior["selection"], prior["event_ledger"]]
    refs += [{"path": str(p.relative_to(ROOT)), "sha256": h} for p, h in (
        (PRIMARY, PRIMARY_SHA), (PANEL, PANEL_SHA), (COVERAGE, COVERAGE_SHA))]
    refs += [{"path": p, "sha256": sha(ROOT / p)} for p in (
        "mom_trading/suspension_liquidity.py", "research/run_crowding_suspension_liquidity_replay.py")]
    refs = list({r["path"]: r for r in refs}.values())
    for ref in refs:
        if sha(ROOT / ref["path"]) != ref["sha256"]:
            raise ValueError(f"liquidity_binding_mismatch:{ref['path']}")
    write_new(started, {"status": "suspension_input_repair_started", "spec": SPEC, "bindings": refs})
    try:
        by_code = defaultdict(list)
        with (ROOT / prior["event_ledger"]["path"]).open() as stream:
            for line in stream:
                event = json.loads(line)
                if event["reason"] in FAILURES:
                    if event["status"] != "unresolved":
                        raise ValueError("ADV_missing_without_unresolved_status")
                    by_code[event["code"]].append(event)
        all_days = json.loads(INPUTS[2][0].read_text())["trading_dates"]
        sessions = [d for d in all_days if "2021-01-04" <= d <= "2025-12-31"]
        calendar_index = {pd.Timestamp(d): i for i, d in enumerate(all_days)}
        state_rows = json.loads(MARKET_STATE_PATH.read_text())["rows"]
        states = {r["trade_date"]: (r.get("features") or {}).get("confirmed_state", "UNKNOWN") for r in state_rows}
        special = pd.read_csv(INPUTS[3][0], dtype=str)
        normalize = lambda c: c.split(".")[-1] if c.startswith(("sh.", "sz.")) else c.split(".")[0]
        special_keys = {(normalize(r.code), pd.Timestamp(r.date)) for r in special.loc[special.verification_status.eq("verified")].itertuples(index=False)}
        action_map = defaultdict(dict)
        for action in json.loads(COVERAGE.read_text())["action_evidence"].values():
            action_map[action["code"]][action["ex_date"]] = action
        revised, transitions = {}, Counter()
        comparison_keys = ("status", "reason", "entry_day", "exit_day", "entry_shares", "remaining_shares", "entry_cash_yuan", "cost_yuan", "net_pnl_yuan", "execution_events")
        for n, (code, stock) in enumerate(market_chunks(sorted(by_code), sessions, calendar_index, special_keys)):
            symbol = f"{code}.{'SH' if code.startswith('6') else 'SZ'}"
            canonical = pd.read_parquet(PANEL, filters=[("ts_code", "=", symbol)], columns=[
                "ts_code", "trade_date", "stable_instrument_id", "trade_status", "amount", "vol", "open"])
            raw = EventMarket(stock, sessions, states)
            cash_view, proofs = cash_market_view(raw, action_map[code])
            repaired, receipts = suspension_adv_view(cash_view, canonical)
            for old in by_code[code]:
                baseline = replay_with_cash(old, cash_view, proofs, action_map[code], bps=old["slippage_bps"])
                if any(baseline.get(k) != old.get(k) for k in comparison_keys):
                    raise ValueError("liquidity_prior_failure_not_reproduced")
                event = replay_with_cash(old, repaired, proofs, action_map[code], bps=old["slippage_bps"])
                end = min(repaired.index[old["signal_day"]]+11, len(sessions)-1)
                relevant = [r for d, r in sorted(receipts.items()) if old["signal_day"] < d <= (event["exit_day"] or sessions[end])]
                event.update(quintile=old["quintile"], prior_status=old["status"], prior_reason=old["reason"],
                             liquidity_repairs=relevant, liquidity_repair_schema=SPEC["schema"])
                audit_changed(event, canonical, sessions)
                revised[old["signal_day"], code, old["slippage_bps"]] = event
                transitions[old["slippage_bps"], old["reason"], event["status"], event["reason"]] += 1
            if n % 20 == 0:
                progress(json.dumps({"phase": "suspension_input_replay", "instruments_done": n+1, "paths": len(revised)}), flush=True)
        compact, unchanged, checked = [], 0, Counter()
        keep = ["signal_day", "code", "factor", "signal_emotion", "slippage_bps", "status", "reason", "fixed_budget_return"]
        with (ROOT / prior["event_ledger"]["path"]).open() as source, ledger.open("x") as target:
            for line in source:
                old = json.loads(line)
                key = old["signal_day"], old["code"], old["slippage_bps"]
                event = revised.get(key)
                if event is None:
                    target.write(line)  # Byte-for-byte preserve all unaffected events.
                    event, unchanged = old, unchanged+1
                else:
                    for field in ("signal_day", "code", "factor", "signal_emotion", "slippage_bps", "quintile"):
                        if event[field] != old[field]:
                            raise ValueError("liquidity_event_identity_changed")
                    target.write(json.dumps(event, ensure_ascii=False, allow_nan=False)+"\n")
                    checked[key] += 1
                compact.append({k: event[k] for k in keep})
        if len(compact) != prior["event_path_count"] or set(checked) != set(revised) or any(n != 1 for n in checked.values()):
            raise ValueError("liquidity_full_population_changed")
        signals = pd.read_csv(ROOT / prior["selection"]["path"], dtype={"code": str})
        cohorts = freeze_quintiles(signals)
        expected = set(signals.loc[signals.quintile.isin(["top", "bottom"]), ["signal_day", "code", "quintile"]].itertuples(index=False, name=None))
        if set(cohorts[["signal_day", "code", "quintile"]].itertuples(index=False, name=None)) != expected:
            raise ValueError("liquidity_original_cohorts_changed")
        events = pd.DataFrame(compact)
        weeks = weekly_metrics(events, cohorts)
        protocol = json.loads((ROOT / "research/RECENT_LIMIT_HIT_CROWDING_V1_PROTOCOL.json").read_text())
        folds = {name: protocol["evaluation"][key] for name, key in (
            ("development", "development_diagnostic_no_tuning"), ("validation", "validation"), ("sealed_test", "sealed_test"))}
        for ref in refs:
            if sha(ROOT / ref["path"]) != ref["sha256"]:
                raise ValueError("liquidity_source_changed_during_replay")
        result = {"status": "suspension_input_repair_completed_not_admitted", "spec": SPEC, "bindings": refs,
                  "signal_count": len(signals), "event_path_count": len(events), "selected_members": len(cohorts),
                  "selection": prior["selection"], "event_ledger": {"path": str(ledger.relative_to(ROOT)), "sha256": sha(ledger)},
                  "independent_checks": {"replayed_paths": len(revised), "old_failures_reproduced": len(revised),
                                         "window_and_cash_arithmetic_passed": len(revised), "unchanged_event_bytes": unchanged},
                  "transitions": [{"slippage_bps": bps, "before": before, "status": status, "reason": reason, "count": count}
                                  for (bps, before, status, reason), count in sorted(transitions.items())],
                  "execution_by_cost": [{"slippage_bps": int(bps), "status_counts": dict(Counter(group.status)),
                                         "reason_counts": dict(Counter(group.reason))} for bps, group in events.groupby("slippage_bps")],
                  "weekly_metrics": weeks, "emotion_fold_metrics": emotion_fold_metrics(weeks, folds),
                  "historical_qualification_restored": False, "production_eligible": False, "generates_alert": False,
                  "portfolio_return": None, "limitations": [
                      "Historical implementation repair, not fresh out-of-sample evidence.",
                      "Confirmed source suspension means zero turnover; active missing data never filled.",
                      "Market data shares the frozen canonical panel lineage; this is not an independent data vendor audit."]}
        write_new(output, result)
        return {k: v for k, v in result.items() if k not in {"bindings", "weekly_metrics", "emotion_fold_metrics"}}
    except Exception as exc:
        write_new(output.with_suffix(".failed.json"), {"status": "suspension_input_repair_failed", "error": str(exc)})
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(run(parser.parse_args().output), ensure_ascii=False, indent=2))
