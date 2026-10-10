"""Predeclared emotion/shape ablations of the reused historical event study.

The original outcome is already known. These are diagnostic controls, never new
production strategies or fresh blind trials. Original rules and fills stay frozen.
"""
from __future__ import annotations

import argparse
from copy import copy
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from mom_trading.emotion_pattern_research import REQUIRED, FACTORS, PLAYBOOKS, compute_features, build_research_candidates
from mom_trading.emotion_pattern_replay import EventMarket, replay_event, summarize_events
from research.run_emotion_pattern_replay import INPUTS, STOCK_FEATURES_PATH, MARKET_STATE_PATH, sha, write_new, factor_diagnostics

PRIMARY = ROOT / "output/research/emotion_pattern_event_diagnostic_v1.json"
PRIMARY_SHA = "ed542d5e04d6a012a300df8143832d03364b9c1461b6115217fa56c44336df55"
SPEC = {
    "schema": "emotion_pattern_diagnostic_controls.v1",
    "original_result_already_seen": True, "new_blind_trial": False,
    "tracks": {
        "original_routed": "原规则，复用已冻结逐事件结果",
        "same_entries_no_emotion_exit": "相同信号及入场条件，不因情绪变化退出",
        "no_emotion": "相同形态、板块及成交约束，不限制入场情绪且不因情绪变化退出",
        "same_dates_no_shape": "仅原战法信号日、原适用情绪与板块，取消形态筛选；其余风险线和退出条件不变",
    },
    "selection_uses_outcomes": False, "state_override_only_in_counterfactual_copy": True,
    "cost_scenarios_bps": [10, 20, 30], "parameter_tuning": False,
    "statistical_inference": "descriptive_only_overlap_family_tests_and_unresolved_paths_remain",
    "production_eligible": False, "generates_alert": False,
}


def signal_key(row: dict) -> tuple:
    return row["playbook_id"], pd.Timestamp(row.get("signal_day", row.get("trade_date"))).date().isoformat(), row["code"]


def select_controls(features: pd.DataFrame, states: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Select all fixed controls before looking at any counterfactual returns."""
    routed = build_research_candidates(features, states)
    routed = routed.loc[routed.kind.eq("entry_research")].copy()
    actual_states = states.copy()
    actual_states["trade_date"] = pd.to_datetime(actual_states.trade_date).dt.normalize()
    if actual_states.trade_date.duplicated().any():
        raise ValueError("control_state_identity_duplicate")
    observed = actual_states.set_index("trade_date").confirmed_state
    dates = sorted(features.trade_date.unique())
    ungated = []
    for allowed_state in ("RISK_ON", "STABILIZING"):
        counterfactual_states = pd.DataFrame({"trade_date": dates, "confirmed_state": allowed_state})
        candidates = build_research_candidates(features, counterfactual_states)
        candidates = candidates.loc[candidates.kind.eq("entry_research")].copy()
        candidates["confirmed_state"] = candidates.trade_date.map(observed).fillna("UNKNOWN")
        ungated.append(candidates)
    no_emotion = pd.concat(ungated, ignore_index=True)
    if no_emotion.duplicated(["playbook_id", "trade_date", "code"]).any():
        raise ValueError("control_candidate_duplicate")
    unfiltered = features.copy()
    for playbook in PLAYBOOKS:
        if playbook["kind"] == "entry_research":
            unfiltered[playbook["id"]] = True
    no_shape = build_research_candidates(unfiltered, actual_states)
    signal_dates = set(zip(routed.playbook_id, routed.trade_date))
    no_shape = no_shape.loc[[pair in signal_dates for pair in zip(no_shape.playbook_id, no_shape.trade_date)]].copy()
    return {"original_routed": routed, "no_emotion": no_emotion, "same_dates_no_shape": no_shape}


def replay_control(candidate: dict, market: EventMarket, *, bps: int, track: str) -> dict:
    if track not in {"no_emotion", "same_entries_no_emotion_exit", "same_dates_no_shape"}:
        raise ValueError("unknown_diagnostic_control")
    actual = market.states.get(pd.Timestamp(candidate["trade_date"]).date().isoformat(), "UNKNOWN")
    if candidate["confirmed_state"] != actual:
        raise ValueError("control_observed_state_mismatch")
    event = dict(candidate)
    observation = market
    disabled = track in {"no_emotion", "same_entries_no_emotion_exit"}
    if disabled:
        # A constant dummy route disables ONLY the two emotion predicates in the
        # unchanged v1 engine. Never mutate real state observations or live routing.
        allowed = next(p["states"][0] for p in PLAYBOOKS if p["id"] == event["playbook_id"])
        observation = copy(market)
        observation.states = dict.fromkeys(market.sessions, allowed)
        event["confirmed_state"] = allowed
    result = replay_event(event, observation, slippage_bps=bps)
    result.update(track=track, signal_emotion=actual, emotion_predicates_disabled=disabled,
                  observed_signal_emotion=actual, diagnostic_control_only=True)
    return result


def compare_same_entries(events: list[dict]) -> list[dict]:
    keys = lambda e: (*signal_key(e), e["slippage_bps"])
    original = {keys(e): e for e in events if e["track"] == "original_routed"}
    control = {keys(e): e for e in events if e["track"] == "same_entries_no_emotion_exit"}
    if len(original) != sum(e["track"] == "original_routed" for e in events) or len(control) != sum(e["track"] == "same_entries_no_emotion_exit" for e in events):
        raise ValueError("same_entry_duplicate_event")
    if set(original) != set(control):
        raise ValueError("same_entry_control_does_not_cover_original_events")
    groups = {}
    for key, source in original.items():
        other = control[key]
        if source["entry_day"] != other["entry_day"] or source["simulated_entry_shares"] != other["simulated_entry_shares"]:
            raise ValueError("same_entry_control_changed_entry")
        group = groups.setdefault((source["playbook_name"], source["slippage_bps"]), {"pairs": 0, "paired_closed": [], "incomplete": 0})
        group["pairs"] += 1
        if source["status"] == other["status"] == "closed":
            group["paired_closed"].append(other["net_return_on_entry_cash"] - source["net_return_on_entry_cash"])
        elif "unresolved" in (source["status"], other["status"]):
            group["incomplete"] += 1
    return [{"playbook_name": name, "slippage_bps": bps, "paired_signals": g["pairs"],
             "both_closed": len(g["paired_closed"]), "unresolved_pairs": g["incomplete"],
             "closed_pair_mean_delta_no_emotion_exit_minus_original": statistics.fmean(g["paired_closed"]) if g["paired_closed"] else None,
             "all_pair_effect": None, "causal_or_production_claim": False}
            for (name, bps), g in sorted(groups.items())]


def market_chunks(codes: list[str], sessions: list[str], calendar_index: dict, special_keys: set):
    columns = sorted((EventMarket.REQUIRED - {"no_price_limit", "lagged_adv"}) | {"adv20", "history_sessions", "session_index"})
    for offset in range(0, len(codes), 100):
        frame = pd.read_parquet(STOCK_FEATURES_PATH, columns=columns, filters=[("code", "in", codes[offset:offset+100])])
        frame = frame.sort_values(["code", "trade_date"])
        if not frame.trade_date.map(calendar_index).eq(frame.session_index).all():
            raise ValueError("control_market_calendar_mismatch")
        grouped = frame.groupby("code", sort=False)
        frame["lagged_adv"] = grouped.adv20.shift(1).where((frame.session_index - grouped.session_index.shift(1)).eq(1))
        frame["no_price_limit"] = [h <= 5 or (c, d) in special_keys for c, d, h in zip(frame.code, frame.trade_date, frame.history_sessions)]
        for code, stock in frame.groupby("code", sort=True):
            yield code, stock


def run(output: Path, progress=print) -> dict:
    output = output.resolve()
    if not output.is_relative_to(ROOT.resolve()):
        raise ValueError("control_output_must_be_inside_project")
    started, ledger = output.with_suffix(".started.json"), output.with_suffix(".events.jsonl")
    if any(p.exists() for p in (output, started, ledger)):
        raise FileExistsError("Control attempt exists; never overwrite observed results.")
    if sha(PRIMARY) != PRIMARY_SHA:
        raise ValueError("primary_result_identity_mismatch")
    original = json.loads(PRIMARY.read_text(encoding="utf-8"))
    refs = original["inputs"] + original["source_code"] + [
        {"path": str(PRIMARY.relative_to(ROOT)), "sha256": PRIMARY_SHA},
        {"path": "research/run_emotion_pattern_controls.py", "sha256": sha(Path(__file__))},
    ]
    for ref in refs:
        if sha(ROOT / ref["path"]) != ref["sha256"]:
            raise ValueError(f"control_binding_mismatch:{ref['path']}")
    write_new(started, {"status": "diagnostic_control_started_after_original_result", "spec": SPEC, "bindings": refs})
    try:
        document = json.loads(MARKET_STATE_PATH.read_text(encoding="utf-8"))
        states = pd.DataFrame([{"trade_date": r["trade_date"], "confirmed_state": (r.get("features") or {}).get("confirmed_state", "UNKNOWN")}
                               for r in document["rows"]])
        state_map = dict(zip(states.trade_date, states.confirmed_state))
        selected = {track: [] for track in ("original_routed", "no_emotion", "same_dates_no_shape")}
        for quarter in pd.period_range("2021Q1", "2025Q4", freq="Q"):
            start, end = quarter.start_time.normalize(), quarter.end_time.normalize()
            rows = pd.read_parquet(STOCK_FEATURES_PATH, columns=sorted(REQUIRED), filters=[
                ("trade_date", ">=", start - pd.Timedelta(days=180)), ("trade_date", "<=", end)])
            features = compute_features(rows, as_of=f"{end.date()}T15:05:00+08:00")
            controls = select_controls(features.loc[features.trade_date.ge(start)], states)
            for track, frame in controls.items():
                selected[track].append(frame)
            progress(json.dumps({"phase": "control_candidates", "quarter": str(quarter), "counts": {k: len(v) for k, v in controls.items()}}), flush=True)
        selections = {k: pd.concat(v, ignore_index=True) for k, v in selected.items()}
        primary_keys = {signal_key(e) for e in original["events"]}
        rebuilt_keys = {signal_key(r) for r in selections["original_routed"].to_dict("records")}
        if primary_keys != rebuilt_keys:
            raise ValueError("original_candidate_set_not_reproduced")
        candidates = pd.concat([frame.assign(track=track) for track, frame in selections.items() if track != "original_routed"], ignore_index=True)
        by_code = {code: stock for code, stock in candidates.groupby("code", sort=True)}
        all_days = json.loads(INPUTS[2][0].read_text(encoding="utf-8"))["trading_dates"]
        if all_days != sorted(set(all_days)):
            raise ValueError("control_calendar_invalid")
        sessions = [day for day in all_days if "2021-01-04" <= day <= "2025-12-31"]
        calendar_index = {pd.Timestamp(day): i for i, day in enumerate(all_days)}
        special = pd.read_csv(INPUTS[3][0], dtype=str)
        def normalize(c):
            return c.split(".")[-1] if c.startswith(("sh.", "sz.")) else c.split(".")[0]
        special_keys = {(normalize(r.code), pd.Timestamp(r.date)) for r in special.loc[special.verification_status.eq("verified")].itertuples(index=False)}
        compact = []
        keep = ("track", "playbook_id", "playbook_name", "signal_day", "signal_emotion", "slippage_bps", "code",
                "status", "reason", "entry_day", "simulated_entry_shares", "net_pnl_yuan", "net_return_on_entry_cash", "signal_factors")
        with ledger.open("x", encoding="utf-8") as stream:
            def record(event):
                stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
                compact.append({k: event[k] for k in keep})
            for event in original["events"]:
                record({**event, "track": "original_routed", "diagnostic_control_only": False})
            for n, (code, stock) in enumerate(market_chunks(sorted(by_code), sessions, calendar_index, special_keys)):
                market = EventMarket(stock, sessions, state_map)
                for candidate in by_code[code].to_dict("records"):
                    for bps in SPEC["cost_scenarios_bps"]:
                        result = replay_control(candidate, market, bps=bps, track=candidate["track"])
                        result["signal_factors"] = {f["id"]: float(candidate[f["id"]]) if pd.notna(candidate[f["id"]]) else None for f in FACTORS}
                        record(result)
                        if candidate["track"] == "no_emotion" and signal_key(candidate) in primary_keys:
                            record({**result, "track": "same_entries_no_emotion_exit"})
                if n % 100 == 0:
                    progress(json.dumps({"phase": "control_paths", "instruments_done": n+1, "paths": len(compact)}), flush=True)
        summaries, factors = [], []
        for track in SPEC["tracks"]:
            rows = [r for r in compact if r["track"] == track]
            summaries.extend({**s, "track": track} for s in summarize_events(rows))
            factors.extend({**s, "track": track} for s in factor_diagnostics(rows))
        for ref in refs:
            if sha(ROOT / ref["path"]) != ref["sha256"]:
                raise ValueError("control_inputs_or_sources_changed_during_run")
        result = {"schema": SPEC["schema"], "status": "diagnostic_controls_computed_not_admitted", "spec": SPEC, "bindings": refs,
                  "signal_counts": {track: len(frame) for track, frame in selections.items()}, "path_count": len(compact),
                  "event_ledger": {"path": str(ledger.relative_to(ROOT)), "sha256": sha(ledger)},
                  "summaries": summaries, "same_entry_pairs": compare_same_entries(compact), "factor_diagnostics": factors,
                  "portfolio_return": None, "production_eligible": False, "generates_alert": False}
        write_new(output, result)
        return {k: v for k, v in result.items() if k not in {"bindings", "factor_diagnostics", "summaries"}}
    except Exception as exc:
        write_new(output.with_suffix(".failed.json"), {"status": "diagnostic_control_failed", "error": str(exc)})
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.output), ensure_ascii=False, indent=2, allow_nan=False))
