"""One corrected replay of the known crowding study; never overwrite an attempt."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from mom_trading.crowding_factor_validation import freeze_quintiles
from mom_trading.crowding_factor_replay import SPEC, EventMarket, replay_fixed_horizon, weekly_metrics, emotion_fold_metrics
from research import recent_limit_hit_crowding_v1 as original
from research.run_emotion_pattern_replay import INPUTS, MARKET_STATE_PATH, sha, write_new
from research.run_emotion_pattern_controls import market_chunks

AUDIT = ROOT / "output/research/recent_limit_crowding_cohort_audit_v1.json"
AUDIT_SHA = "3e25d3fccb77c464387ea4008a83e4f4bdfe6a29c02368f065a6378edc381907"
PRIOR_ATTEMPT = [
    ("output/research/recent_limit_crowding_cash_share_v1.started.json", "726671648582513d35b59a3b355611e0307230f7d0f4cdcac710fe953eb9c2d0"),
    ("output/research/recent_limit_crowding_cash_share_v1.failed.json", "25893fee92c37beccd7bbb22cae36e804a2373385771132dfb58f56109d6be50"),
    ("output/research/recent_limit_crowding_cash_share_v1.failed_engine.py", "0a06211b00cd878a58a0cf6412de1f06f1a9bbe192cd42900bc2ddb157b06dc3"),
    ("output/research/recent_limit_crowding_cash_share_v1.failed_runner.py", "a91d7ba69ccda7ab64592824917f7d5480fb28cfa120e855c15198f9a57992c1"),
    ("output/research/recent_limit_crowding_cash_share_v1.events.jsonl", "eea30eaba82ff99325c2cb5b408bf2f74f4c625c103c825b1f8123d25f8a10f8"),
]


def run(output: Path, progress=print) -> dict:
    output = output.resolve()
    if not output.is_relative_to(ROOT.resolve()):
        raise ValueError("crowding_output_outside_project")
    started = output.with_suffix(".started.json")
    ledger = output.with_suffix(".events.jsonl")
    selection = output.with_suffix(".signals.csv")
    frozen = output.with_suffix(".cohorts.json")
    failed = output.with_suffix(".failed.json")
    if any(p.exists() for p in (output, started, ledger, selection, frozen, failed)):
        raise FileExistsError("Crowding attempt exists; do not overwrite or silently rerun.")
    if sha(AUDIT) != AUDIT_SHA:
        raise ValueError("crowding_prior_audit_identity_mismatch")
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    refs = audit["bindings"] + [{"path": str(AUDIT.relative_to(ROOT)), "sha256": AUDIT_SHA}]
    refs += [{"path": path, "sha256": digest} for path, digest in PRIOR_ATTEMPT]
    refs += [{"path": str(p.relative_to(ROOT)), "sha256": digest} for p, digest in INPUTS]
    refs += [{"path": path, "sha256": sha(ROOT / path)} for path in (
        "mom_trading/crowding_factor_replay.py", "research/run_crowding_cash_share_diagnostic.py",
        "mom_trading/emotion_pattern_replay.py", "mom_trading/emotion_pattern_research.py",
        "research/run_emotion_pattern_replay.py", "research/run_emotion_pattern_controls.py",
        "research/limit_up_excluded_momentum_v1.py", "quantqc/ashare.py", "quantqc/costs.py",
    )]
    for ref in refs:
        if sha(ROOT / ref["path"]) != ref["sha256"]:
            raise ValueError(f"crowding_binding_mismatch:{ref['path']}")
    protocol = original._load_protocol()
    folds = {name: protocol["evaluation"][key] for name, key in (
        ("development", "development_diagnostic_no_tuning"), ("validation", "validation"), ("sealed_test", "sealed_test"))}
    write_new(started, {"status": "corrected_diagnostic_started_known_history", "spec": SPEC, "folds": folds, "bindings": refs})
    try:
        frame = original.add_crowding_factor(original._load_daily(protocol))
        candidates, original_sessions = original._candidate_rows(frame, protocol)
        signals = candidates.rename(columns={"trade_date": "signal_day", "negative_limit_hit_count_20_v1": "factor"})[
            ["signal_day", "code", "factor"]].copy()
        del frame, candidates
        cohorts = freeze_quintiles(signals)
        if len(signals) != audit["signal_count"] or len(cohorts) != audit["comparison"]["expected_selected_rows"]:
            raise ValueError("crowding_frozen_population_not_reproduced")
        document = json.loads(MARKET_STATE_PATH.read_text(encoding="utf-8"))
        states = pd.DataFrame([{"signal_day": r["trade_date"], "signal_emotion": (r.get("features") or {}).get("confirmed_state", "UNKNOWN")}
                               for r in document["rows"]])
        if states.signal_day.duplicated().any():
            raise ValueError("crowding_state_date_duplicate")
        state_map = dict(zip(states.signal_day, states.signal_emotion))
        signals["signal_emotion"] = signals.signal_day.map(state_map).fillna("UNKNOWN")
        signals = signals.merge(cohorts[["signal_day", "code", "quintile"]], how="left", on=["signal_day", "code"], validate="one_to_one")
        signals["quintile"] = signals.quintile.fillna("middle")
        signals.to_csv(selection, index=False, mode="x")
        selection_ref = {"path": str(selection.relative_to(ROOT)), "sha256": sha(selection)}
        write_new(frozen, {"status": "all_cohorts_frozen_before_replay", "signals": len(signals),
                           "selected_members": len(cohorts), "selection": selection_ref})
        progress(json.dumps({"phase": "cohorts_frozen", "signals": len(signals), "selected_members": len(cohorts)}), flush=True)
        all_days = json.loads(INPUTS[2][0].read_text(encoding="utf-8"))["trading_dates"]
        if all_days != sorted(set(all_days)):
            raise ValueError("crowding_calendar_invalid")
        sessions = [day for day in all_days if "2021-01-04" <= day <= "2025-12-31"]
        if sessions != original_sessions:
            raise ValueError("crowding_original_calendar_not_reproduced")
        calendar_index = {pd.Timestamp(day): i for i, day in enumerate(all_days)}
        special = pd.read_csv(INPUTS[3][0], dtype=str)
        def normalize(code):
            return code.split(".")[-1] if code.startswith(("sh.", "sz.")) else code.split(".")[0]
        special_keys = {(normalize(r.code), pd.Timestamp(r.date)) for r in special.loc[special.verification_status.eq("verified")].itertuples(index=False)}
        by_code = {code: group for code, group in signals.groupby("code", sort=True)}
        compact = []
        keep = ["signal_day", "code", "factor", "signal_emotion", "slippage_bps", "status", "reason", "fixed_budget_return"]
        with ledger.open("x", encoding="utf-8") as stream:
            for n, (code, stock) in enumerate(market_chunks(sorted(by_code), sessions, calendar_index, special_keys)):
                market = EventMarket(stock, sessions, state_map)
                for signal in by_code[code].to_dict("records"):
                    for bps in SPEC["cost_scenarios_bps"]:
                        event = replay_fixed_horizon(signal, market, bps=bps)
                        event["quintile"] = signal["quintile"]
                        stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
                        compact.append({k: event[k] for k in keep})
                if n % 100 == 0:
                    progress(json.dumps({"phase": "cash_share_paths", "instruments_done": n + 1, "paths": len(compact)}), flush=True)
        if len(compact) != len(signals) * len(SPEC["cost_scenarios_bps"]):
            raise ValueError("crowding_event_population_missing")
        events = pd.DataFrame(compact)
        expected = set(signals[["signal_day", "code"]].itertuples(index=False, name=None))
        for bps in SPEC["cost_scenarios_bps"]:
            actual = events.loc[events.slippage_bps.eq(bps)]
            if set(actual[["signal_day", "code"]].itertuples(index=False, name=None)) != expected:
                raise ValueError("crowding_scenario_population_mismatch")
        weeks = weekly_metrics(events, cohorts)
        for ref in refs + [selection_ref]:
            if sha(ROOT / ref["path"]) != ref["sha256"]:
                raise ValueError("crowding_input_source_or_cohort_changed_during_replay")
        result = {
            "schema": SPEC["schema"], "status": "corrected_diagnostic_computed_not_admitted",
            "spec": SPEC, "bindings": refs, "signal_count": len(signals), "selected_members": len(cohorts),
            "event_path_count": len(events), "selection": selection_ref,
            "event_ledger": {"path": str(ledger.relative_to(ROOT)), "sha256": sha(ledger)},
            "execution_by_cost": [{"slippage_bps": int(bps), "status_counts": dict(Counter(group.status)),
                                   "reason_counts": dict(Counter(group.reason))} for bps, group in events.groupby("slippage_bps")],
            "weekly_metrics": weeks, "emotion_fold_metrics": emotion_fold_metrics(weeks, folds),
            "historical_qualification_restored": False, "portfolio_return": None,
            "production_eligible": False, "generates_alert": False,
            "limitations": ["已看历史的实现更正，不是新盲测。", "分组未读取收益；分组内未结算导致完整指标为空。",
                            "现金预算含费用，使用原始价格及股数，收益口径与旧复权名义金额不同。",
                            "独立事件可能重叠，不代表账户净值；公司行动权益缺失仍需补齐。",
                            "沿用原股票会话滚动因子定义；额外的因子源数据时点审计和真实前向证据未完成。"],
        }
        write_new(output, result)
        return {k: v for k, v in result.items() if k not in {"bindings", "weekly_metrics", "emotion_fold_metrics"}}
    except Exception as exc:
        write_new(failed, {"status": "corrected_crowding_diagnostic_failed", "error": str(exc)})
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(run(parser.parse_args().output), ensure_ascii=False, indent=2, allow_nan=False))
