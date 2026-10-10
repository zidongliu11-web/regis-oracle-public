"""Replay every fixed routed pattern on the reused 2021-2025 historical inputs.

This is a path diagnostic, not a new blind trial or account backtest. All dates,
three cost scenarios, unresolved events and family-overlap caveats are retained.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from mom_trading.emotion_pattern_research import REQUIRED, FACTORS, compute_features, build_research_candidates, research_catalog
from mom_trading.emotion_pattern_replay import CONTRACT, EventMarket, contract_identity, replay_event, summarize_events
from mom_trading.sentiment_rising_industry_momentum import (
    STOCK_FEATURES_PATH, MARKET_STATE_PATH, EXPECTED_STOCK_FEATURES_SHA256, EXPECTED_MARKET_STATE_SHA256,
)

INPUTS = [
    (STOCK_FEATURES_PATH, EXPECTED_STOCK_FEATURES_SHA256),
    (MARKET_STATE_PATH, EXPECTED_MARKET_STATE_SHA256),
    (ROOT / "research/data/overnight_trend_monthly_12_1_v1/source_snapshots/v4_market_calendar-0f72159d770d87b0ec6197a673bc67717c0bf4e92e961dbd3d1726cf511a7795.json",
     "0f72159d770d87b0ec6197a673bc67717c0bf4e92e961dbd3d1726cf511a7795"),
    (ROOT / "research/data/special_price_limit_events.csv", "1059fbc5e78eec32d5ed215dc7acf1ef61e26dbd4f8179077ce9b2326842ea38"),
]


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_new(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def read_window(path: Path, columns: list[str], predicate) -> pd.DataFrame:
    # Bounded scanner readahead prevents parallel decompression of the entire
    # input from exceeding the cloud worker's memory budget.
    parquet = ds.ParquetFileFormat(default_fragment_scan_options=ds.ParquetFragmentScanOptions(
        pre_buffer=False, use_buffered_stream=True, buffer_size=65536,
    ))
    scanner = ds.dataset(path, format=parquet).scanner(
        columns=columns, filter=predicate, batch_size=32768,
        batch_readahead=1, fragment_readahead=1, use_threads=False,
    )
    return scanner.to_table().to_pandas()


def factor_diagnostics(events: list[dict]) -> list[dict]:
    # The same fixed event set, not five separately optimised trading strategies.
    rows = [{**{k: e[k] for k in ("playbook_name", "signal_emotion", "signal_day", "net_return_on_entry_cash")},
             **e["signal_factors"]} for e in events if e["status"] == "closed" and e["slippage_bps"] == 10]
    if not rows:
        return []
    frame = pd.DataFrame(rows)
    output = []
    for (playbook, state), group in frame.groupby(["playbook_name", "signal_emotion"]):
        for factor in FACTORS:
            daily = []
            for _, day in group.groupby("signal_day"):
                pairs = day[[factor["id"], "net_return_on_entry_cash"]].dropna()
                if len(pairs) >= 5 and pairs.nunique().min() > 1:
                    daily.append(float(pairs.rank().corr().iloc[0, 1]))
            output.append({"playbook_name": playbook, "signal_emotion": state, "factor_name": factor["name"],
                           "eligible_signal_days": len(daily),
                           "closed_only_daily_rank_correlation_mean": float(np.mean(daily)) if daily else None,
                           "direction_selected": False, "standalone_factor_validation": False,
                           "caveat": "仅已闭环事件的形态内排序诊断；重叠事件、缺失路径与多重检验未解决，不能视为已验证因子。"})
    return output


def run(output: Path, progress=print, *, input_root: Path | None = None) -> dict:
    # Research inputs are read from explicitly selected storage; executable
    # sources always come from this checkout. Never infer a new sample identity.
    input_root = ROOT if input_root is None else input_root.resolve()
    bound_inputs = [(input_root / path.relative_to(ROOT), expected) for path, expected in INPUTS]
    stock_features_path, market_state_path = [pair[0] for pair in bound_inputs[:2]]
    started = output.with_suffix(".started.json")
    if output.exists() or started.exists():
        raise FileExistsError("Diagnostic attempt already exists; do not overwrite or silently rerun.")
    for path, expected in bound_inputs:
        if sha(path) != expected:
            raise ValueError(f"research_input_hash_mismatch:{path.name}")
    sources = [ROOT / path for path in ("mom_trading/emotion_pattern_research.py", "mom_trading/emotion_pattern_replay.py",
                                       "research/run_emotion_pattern_replay.py", "quantqc/ashare.py", "quantqc/costs.py")]
    references = [{"path": str(p.relative_to(input_root)), "sha256": digest} for p, digest in bound_inputs]
    source_refs = [{"path": str(p.relative_to(ROOT)), "sha256": sha(p)} for p in sources]
    contract = json.loads(json.dumps(CONTRACT))
    catalog = research_catalog()
    write_new(started, {"status": "diagnostic_started_not_new_blind_trial", "contract": contract,
                        "contract_sha256": contract_identity(), "catalog": catalog,
                        "inputs": references, "source_code": source_refs})
    try:
        states_doc = json.loads(market_state_path.read_text(encoding="utf-8"))
        states = pd.DataFrame([{"trade_date": r["trade_date"], "confirmed_state": (r.get("features") or {}).get("confirmed_state", "UNKNOWN")}
                               for r in states_doc["rows"]])
        state_map = dict(zip(states.trade_date, states.confirmed_state))
        if states.trade_date.duplicated().any():
            raise ValueError("state_date_duplicate")
        calendar_doc = json.loads(bound_inputs[2][0].read_text(encoding="utf-8"))
        all_sessions = calendar_doc["trading_dates"]
        if all_sessions != sorted(set(all_sessions)):
            raise ValueError("bound_calendar_invalid")
        sessions = [day for day in all_sessions if "2021-01-04" <= day <= "2025-12-31"]
        index = {pd.Timestamp(day): i for i, day in enumerate(all_sessions)}
        selected = []
        for offset in range(0, len(sessions), 20):
            start = pd.Timestamp(sessions[offset])
            end = pd.Timestamp(sessions[min(offset + 19, len(sessions) - 1)])
            # The longest dependency is 60 preceding sessions. Keep the entire
            # cross-section in each window so sector medians/ranks are unchanged.
            warmup = pd.Timestamp(all_sessions[max(0, index[start] - 60)])
            rows = read_window(stock_features_path, sorted(REQUIRED),
                               (ds.field("trade_date") >= warmup) & (ds.field("trade_date") <= end))
            features = compute_features(rows, as_of=f"{end.date()}T15:05:00+08:00")
            candidates = build_research_candidates(features.loc[features.trade_date.ge(start)], states)
            candidates = candidates.loc[candidates.kind.eq("entry_research")].copy()
            selected.append(candidates)
            progress(json.dumps({"phase": "fixed_candidates", "period": f"{start.date()}—{end.date()}", "candidates": len(candidates)}), flush=True)
            del rows, features
            gc.collect()
            pa.default_memory_pool().release_unused()
        candidates = pd.concat(selected, ignore_index=True)
        codes = sorted(candidates.code.unique())
        columns = sorted((EventMarket.REQUIRED - {"no_price_limit", "lagged_adv"}) | {"adv20", "history_sessions", "session_index"})
        special = pd.read_csv(bound_inputs[3][0], dtype=str)
        special = special.loc[special.verification_status.eq("verified")]
        def code6(code):
            return code.split(".")[-1] if code.startswith(("sh.", "sz.")) else code.split(".")[0]
        special_keys = {(code6(r.code), pd.Timestamp(r.date)) for r in special.itertuples(index=False)}
        events = []
        candidates_by_code = {code: frame for code, frame in candidates.groupby("code", sort=True)}
        for batch_start in range(0, len(codes), 32):
            market_rows = read_window(stock_features_path, columns, ds.field("code").isin(codes[batch_start:batch_start + 32]))
            if market_rows.empty:
                raise ValueError("candidate_market_rows_missing")
            market_rows = market_rows.sort_values(["code", "trade_date"])
            grouped = market_rows.groupby("code", sort=False)
            market_rows["lagged_adv"] = grouped.adv20.shift(1).where((market_rows.session_index - grouped.session_index.shift(1)).eq(1))
            if not market_rows.trade_date.map(index).eq(market_rows.session_index).all():
                raise ValueError("bound_calendar_market_session_mismatch")
            market_rows["no_price_limit"] = [h <= 5 or (c, d) in special_keys for c, d, h in
                                             zip(market_rows.code, market_rows.trade_date, market_rows.history_sessions)]
            for code, frame in market_rows.groupby("code", sort=True):
                market = EventMarket(frame, sessions, state_map)
                for candidate in candidates_by_code[code].to_dict("records"):
                    for bps in contract["slippage_scenarios_bps"]:
                        result = replay_event(candidate, market, slippage_bps=bps)
                        result["signal_factors"] = {f["id"]: float(candidate[f["id"]]) if pd.notna(candidate[f["id"]]) else None for f in FACTORS}
                        events.append(result)
            progress(json.dumps({"phase": "costed_paths", "instruments_done": min(batch_start + 32, len(codes)), "events": len(events)}), flush=True)
            del market_rows, grouped, frame, market
            gc.collect()
            pa.default_memory_pool().release_unused()
        for base, refs in ((input_root, references), (ROOT, source_refs)):
            for ref in refs:
                if sha(base / ref["path"]) != ref["sha256"]:
                    raise ValueError("input_or_implementation_changed_during_replay")
        result = {
            "schema": "emotion_pattern_event_diagnostic.v1", "status": "diagnostic_computed_not_strategy_admission",
            "date_range": ["2021-01-04", "2025-12-31"], "inputs": references, "source_code": source_refs,
            "contract": contract, "catalog_rule_sha256": catalog["rule_sha256"],
            "input_rows": pq.ParquetFile(stock_features_path).metadata.num_rows,
            "candidate_count": len(candidates), "costed_event_count": len(events),
            "summary_by_pattern_and_emotion": summarize_events(events), "factor_diagnostics": factor_diagnostics(events),
            "events": events, "portfolio_return": None, "production_eligible": False, "generates_alert": False,
            "limitations": ["历史输入已复用，非未见样本外或前向验证。", "事件预算独立，重叠事件不能拼成组合净值。",
                            "未解决事件不能删除；已闭环子集的收益不可代表全体。", "未完成无情绪及无形态基线对照和家族多重检验。",
                            "复权变化需实际现金与股份权益明细；缺失时不伪造完整账本。"],
        }
        write_new(output, result)
        return {k: v for k, v in result.items() if k not in {"events", "inputs", "source_code", "catalog_rule_sha256"}}
    except Exception as exc:
        write_new(output.with_suffix(".failed.json"), {"status": "diagnostic_failed", "error": str(exc), "production_eligible": False})
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.output), ensure_ascii=False, indent=2, allow_nan=False))
