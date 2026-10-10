"""Complete date-qualified issuer identities in the full fixed crowding ledger."""
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

from mom_trading.crowding_alias_identity import SPEC, CANONICAL, RULE_KEY, ALIAS_SHA, alias_execution_view, restore_exchange_identity
from mom_trading.crowding_cash_settlement import cash_market_view, replay_with_cash
from mom_trading.crowding_factor_replay import EventMarket, replay_fixed_horizon, weekly_metrics, emotion_fold_metrics
from mom_trading.crowding_factor_validation import freeze_quintiles
from mom_trading.suspension_liquidity import suspension_adv_view
from research.run_emotion_pattern_replay import INPUTS, MARKET_STATE_PATH, sha, write_new
from research.run_emotion_pattern_controls import market_chunks
from research.run_crowding_suspension_liquidity_replay import PANEL, PANEL_SHA, COVERAGE, COVERAGE_SHA, audit_changed

PRIMARY = ROOT / "output/research/crowding_suspension_liquidity_replay_v1.json"
PRIMARY_SHA = "392c17800d887576475aebfa45f79468a15bf896914397a48dffee7583052e46"
ALIAS_ROOT = "research/data/pricedelay_v4_inputs/stable_identity/"
SOURCES = [
    (ALIAS_ROOT + "effective_dated_aliases.csv", ALIAS_SHA),
    (ALIAS_ROOT + "300114_302132_official_announcement.pdf", "90427f29e153de4a4ebd236e25f226ac1b2a72911e0577a667a70076ad495f03"),
    (ALIAS_ROOT + "300114_302132_implementation_20250214.pdf", "dd68049c48df826848f361fd9e7b23dd20b6805144a2e5bc36e54db638611488"),
]


def run(output: Path):
    output = output.resolve()
    if not output.is_relative_to(ROOT):
        raise ValueError("alias_output_outside_project")
    started, ledger = output.with_suffix(".started.json"), output.with_suffix(".events.jsonl")
    if any(p.exists() for p in (output, started, ledger, output.with_suffix(".failed.json"))):
        raise FileExistsError("alias attempt exists; do not overwrite")
    if sha(PRIMARY) != PRIMARY_SHA or sha(PANEL) != PANEL_SHA or sha(COVERAGE) != COVERAGE_SHA:
        raise ValueError("alias_primary_identity_mismatch")
    prior = json.loads(PRIMARY.read_text())
    refs = prior["bindings"] + [prior["selection"], prior["event_ledger"], {"path": str(PRIMARY.relative_to(ROOT)), "sha256": PRIMARY_SHA}]
    refs += [{"path": p, "sha256": h} for p, h in SOURCES]
    refs += [{"path": p, "sha256": sha(ROOT / p)} for p in ("mom_trading/crowding_alias_identity.py", "research/run_crowding_alias_replay.py")]
    refs = list({r["path"]: r for r in refs}.values())
    for ref in refs:
        if sha(ROOT / ref["path"]) != ref["sha256"]:
            raise ValueError(f"alias_binding_mismatch:{ref['path']}")
    write_new(started, {"status": "continuous_issuer_reconstruction_started", "spec": SPEC, "bindings": refs})
    try:
        selected = []
        with (ROOT / prior["event_ledger"]["path"]).open() as stream:
            for line in stream:
                event = json.loads(line)
                if event["code"] == CANONICAL:
                    selected.append(event)
        all_days = json.loads(INPUTS[2][0].read_text())["trading_dates"]
        sessions = [d for d in all_days if "2021-01-04" <= d <= "2025-12-31"]
        calendar_index = {pd.Timestamp(d): i for i, d in enumerate(all_days)}
        state_rows = json.loads(MARKET_STATE_PATH.read_text())["rows"]
        states = {r["trade_date"]: (r.get("features") or {}).get("confirmed_state", "UNKNOWN") for r in state_rows}
        special = pd.read_csv(INPUTS[3][0], dtype=str)
        normalize = lambda c: c.split(".")[-1] if c.startswith(("sh.", "sz.")) else c.split(".")[0]
        special_keys = {(normalize(r.code), pd.Timestamp(r.date)) for r in special.loc[special.verification_status.eq("verified")].itertuples(index=False)}
        _, stock = next(market_chunks([CANONICAL], sessions, calendar_index, special_keys))
        raw = EventMarket(stock, sessions, states)
        canonical = pd.read_parquet(PANEL, filters=[("ts_code", "=", "302132.SZ")])
        aliases = pd.read_csv(ROOT / SOURCES[0][0], dtype=str)
        actions = {r["ex_date"]: r for r in json.loads(COVERAGE.read_text())["action_evidence"].values() if r["code"] == CANONICAL}
        cash_view, proofs = cash_market_view(raw, actions)
        liquid_view, liquidity_receipts = suspension_adv_view(cash_view, canonical)
        view, exchange_codes = alias_execution_view(liquid_view, canonical, aliases)
        revised, transitions = {}, Counter()
        for old in selected:
            original = replay_fixed_horizon(old, raw, bps=old["slippage_bps"])
            if old["status"] != original["status"] or old["reason"] != original["reason"] or old["reason"] != "historical_code_price_limit_identity_unverified":
                raise ValueError("alias_prior_failure_not_reproduced")
            event = replay_with_cash({**old, "code": RULE_KEY}, view, proofs, actions, bps=old["slippage_bps"])
            event = restore_exchange_identity(event, old, exchange_codes)
            end = min(raw.index[old["signal_day"]]+11, len(sessions)-1)
            event["liquidity_repairs"] = [r for d, r in sorted(liquidity_receipts.items()) if old["signal_day"] < d <= (event["exit_day"] or sessions[end])]
            audit_changed(event, canonical, sessions)
            for order in event["execution_events"]:
                expected = "300114.SZ" if pd.Timestamp(order["day"]) < pd.Timestamp("2025-02-17") else "302132.SZ"
                if order["exchange_code"] != expected:
                    raise ValueError("alias_order_exchange_date_mismatch")
            key = old["signal_day"], old["code"], old["slippage_bps"]
            if key in revised:
                raise ValueError("alias_duplicate_event_identity")
            revised[key] = event
            transitions[old["slippage_bps"], event["status"], event["reason"]] += 1
        compact, unchanged, checked = [], 0, set()
        keep = ["signal_day", "code", "factor", "signal_emotion", "slippage_bps", "status", "reason", "fixed_budget_return"]
        with (ROOT / prior["event_ledger"]["path"]).open() as source, ledger.open("x") as target:
            for line in source:
                old = json.loads(line)
                key = old["signal_day"], old["code"], old["slippage_bps"]
                event = revised.get(key)
                if event is None:
                    target.write(line)
                    event, unchanged = old, unchanged+1
                else:
                    if key in checked or any(event[k] != old[k] for k in ("signal_day", "code", "factor", "signal_emotion", "slippage_bps", "quintile")):
                        raise ValueError("alias_factor_population_identity_changed")
                    checked.add(key)
                    target.write(json.dumps(event, ensure_ascii=False, allow_nan=False)+"\n")
                compact.append({k: event[k] for k in keep})
        if len(compact) != prior["event_path_count"] or checked != set(revised) or not checked:
            raise ValueError("alias_full_population_changed")
        signals = pd.read_csv(ROOT / prior["selection"]["path"], dtype={"code": str})
        cohorts = freeze_quintiles(signals)
        expected = set(signals.loc[signals.quintile.isin(["top", "bottom"]), ["signal_day", "code", "quintile"]].itertuples(index=False, name=None))
        if set(cohorts[["signal_day", "code", "quintile"]].itertuples(index=False, name=None)) != expected:
            raise ValueError("alias_predefined_cohorts_changed")
        events = pd.DataFrame(compact)
        weeks = weekly_metrics(events, cohorts)
        protocol = json.loads((ROOT / "research/RECENT_LIMIT_HIT_CROWDING_V1_PROTOCOL.json").read_text())
        folds = {name: protocol["evaluation"][key] for name, key in (
            ("development", "development_diagnostic_no_tuning"), ("validation", "validation"), ("sealed_test", "sealed_test"))}
        for ref in refs:
            if sha(ROOT / ref["path"]) != ref["sha256"]:
                raise ValueError("alias_source_changed_during_replay")
        result = {"status": "continuous_issuer_replay_completed_not_admitted", "spec": SPEC, "bindings": refs,
                  "signal_count": len(signals), "event_path_count": len(events), "selected_members": len(cohorts),
                  "selection": prior["selection"], "event_ledger": {"path": str(ledger.relative_to(ROOT)), "sha256": sha(ledger)},
                  "checks": {"old_failures_reproduced": len(revised), "cash_share_and_dated_order_identity_passed": len(revised), "unchanged_event_bytes": unchanged},
                  "transitions": [{"slippage_bps": bps, "status": status, "reason": reason, "count": count} for (bps, status, reason), count in sorted(transitions.items())],
                  "execution_by_cost": [{"slippage_bps": int(bps), "status_counts": dict(Counter(group.status)), "reason_counts": dict(Counter(group.reason))} for bps, group in events.groupby("slippage_bps")],
                  "weekly_metrics": weeks, "emotion_fold_metrics": emotion_fold_metrics(weeks, folds),
                  "historical_qualification_restored": False, "production_eligible": False, "generates_alert": False, "portfolio_return": None,
                  "limitations": ["Vendor canonical cohort key stays frozen; order symbols use their actual effective dates.",
                                  "Retrospective source identity reconstruction, not contemporaneous source capture or a new blind trial.",
                                  "No other 302 code or corporate-action entitlement is inferred from this issuer's name change."]}
        write_new(output, result)
        return {k: v for k, v in result.items() if k not in {"bindings", "weekly_metrics", "emotion_fold_metrics"}}
    except Exception as exc:
        write_new(output.with_suffix(".failed.json"), {"status": "continuous_issuer_replay_failed", "error": str(exc)})
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(run(parser.parse_args().output), ensure_ascii=False, indent=2))
