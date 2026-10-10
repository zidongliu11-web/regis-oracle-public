"""Reusable, full-cross-section research inputs; no outcome or admission run."""
from collections import defaultdict
import gc
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from .conditioned_research import STATES, DIRECTIONS, market_context
from .emotion_pattern_research import FACTORS, REQUIRED, compute_features
from .research_artifact_paths import research_artifact_path
from research.run_emotion_pattern_replay import read_window, sha, write_new

KIND = "emotion_universe_feature_cache"
MANIFEST = ("research/data/state_conditioned_mainline_swing_v1/input_manifest.json",
            "0019e609fd94e9e5a81668a9caf471b361a758edb945c42d1e8ce4ab9418354a")
SOURCES = (
    "mom_trading/emotion_universe_features.py", "mom_trading/research_workbench.py",
    "mom_trading/emotion_pattern_research.py",
    "mom_trading/conditioned_research.py", "mom_trading/emotion_factor_attribution.py",
    "mom_trading/research_artifact_paths.py", "research/run_emotion_pattern_replay.py",
    "requirements.lock", ".python-version", "scripts/verify_runtime_dependencies.py",
)
CONTRACT = {
    "schema": "emotion_universe_feature_cache.v1",
    "date_range": ["2021-01-04", "2025-12-31"],
    "scope": "MAIN_SH_MAIN_SZ_GEM",
    "formation": "completed_daily_close_only; same_day_cross_section; 61_consecutive_trusted_sessions",
    "universe": "every_input_stock_day; retain_ineligible_and_missing_factor_rows",
    "selection": "no_pattern_or_leading_sector_filter; no_selection_from_outcomes",
    "industry": "existing_frozen_point_in_time_industry_assignment; missing_industry_not_filled",
    "factors": [f["id"] for f in FACTORS],
    "contexts": "frozen_signal_close_emotion_and_same_day_median_return_direction",
    "rankable_input_day": "at_least_5_eligible_finite_names_and_2_distinct_values; not_admission",
    "storage": "bounded_20_session_chunks_with_60_session_warmup; immutable_attempt_parquet",
    "research_only": True, "historical_reuse": True, "unseen_out_of_sample": False,
    "outcomes_computed": False, "candidate_selected": False, "production_eligible": False,
    "portfolio_return": None, "generates_alert": False, "automatic_trading": False,
}


def panel_schema():
    return pa.schema([
        ("stable_instrument_id", pa.string()), ("code", pa.string()),
        ("trade_date", pa.timestamp("ns")), ("session_index", pa.int64()),
        ("industry_code", pa.string()), ("history_ready", pa.bool_()),
        ("eligible", pa.bool_()), ("sector_confirmed", pa.bool_()),
        ("signal_emotion", pa.string()), ("market_direction", pa.string()),
        ("emotion_episode", pa.string()), ("market_row_present", pa.bool_()),
        *[(f["id"], pa.float64()) for f in FACTORS],
    ])


def summarize_days(days):
    grouped = defaultdict(list)
    for row in days:
        for direction in ("ALL", row["market_direction"]):
            grouped[row["signal_emotion"], direction].append(row)
    result = []
    for state in STATES:
        for direction in DIRECTIONS:
            rows = grouped[state, direction]
            for factor in FACTORS:
                values = [r["factors"][factor["id"]] for r in rows]
                result.append({
                    "signal_emotion": state, "market_direction": direction,
                    "factor_id": factor["id"], "factor_name": factor["name"],
                    "market_sessions": len(rows),
                    "input_stock_days": sum(r["input_names"] for r in rows),
                    "eligible_stock_days": sum(r["eligible_names"] for r in rows),
                    "finite_stock_days": sum(v["finite_names"] for v in values),
                    "rankable_sessions": sum(v["rankable"] for v in values),
                    "minimum_finite_names": min((v["finite_names"] for v in values), default=None),
                    "maximum_finite_names": max((v["finite_names"] for v in values), default=None),
                    "status": "input_geometry_observed" if rows else "no_market_session",
                })
    return result


def build(output: Path, *, input_root: Path, references, progress):
    """Bind inputs before/after writing; failed attempts never publish a ready panel."""
    paths = [research_artifact_path(r["path"], project_root=input_root) for r in references]
    if len(paths) != 4:
        raise ValueError("universe_input_contract_invalid")
    for path, ref in zip(paths, references):
        if sha(path) != ref["sha256"]:
            raise ValueError("research_input_hash_mismatch")
    stock, state_path, calendar_path, manifest_path = paths
    manifest = json.loads(manifest_path.read_text())
    if (manifest.get("schema") != "state_conditioned_mainline_input_manifest.v1"
            or manifest.get("outputs", {}).get("stock_features", {}).get("sha256") != references[0]["sha256"]
            or manifest.get("outputs", {}).get("market_state_inputs", {}).get("sha256") != references[1]["sha256"]
            or manifest.get("bindings", {}).get("calendar", {}).get("sha256") != references[2]["sha256"]
            or not manifest.get("bindings", {}).get("point_in_time_industry_intervals", {}).get("sha256")):
        raise ValueError("universe_dataset_provenance_mismatch")
    state_document = json.loads(state_path.read_text())
    if state_document.get("scope") != CONTRACT["scope"]:
        raise ValueError("universe_market_scope_mismatch")
    all_sessions = json.loads(calendar_path.read_text())["trading_dates"]
    contexts = market_context(state_document["rows"], all_sessions)
    start, end = CONTRACT["date_range"]
    sessions = [d for d in all_sessions if start <= d <= end]
    if not sessions:
        raise ValueError("universe_calendar_empty")
    indexes = {pd.Timestamp(d): i for i, d in enumerate(all_sessions)}
    partial = output.with_name("features.partial.parquet")
    panel = output.with_name("features.parquet")
    if any(p.exists() or p.is_symlink() for p in (output, partial, panel)):
        raise FileExistsError("universe_attempt_artifact_exists")
    days, identities, total, eligible_total = [], {}, 0, 0
    writer = pq.ParquetWriter(partial, panel_schema(), compression="zstd")
    try:
        for offset in range(0, len(sessions), 20):
            first, last = pd.Timestamp(sessions[offset]), pd.Timestamp(sessions[min(offset + 19, len(sessions) - 1)])
            warmup = pd.Timestamp(all_sessions[max(0, indexes[first] - 60)])
            raw = read_window(stock, sorted(REQUIRED | {"stable_instrument_id"}),
                              (ds.field("trade_date") >= warmup) & (ds.field("trade_date") <= last))
            if not raw.trade_date.map(indexes).eq(raw.session_index).all():
                raise ValueError("universe_session_identity_mismatch")
            if raw.stable_instrument_id.isna().any():
                raise ValueError("universe_instrument_identity_missing")
            for code, instrument in raw[["code", "stable_instrument_id"]].drop_duplicates().itertuples(index=False, name=None):
                if code in identities and identities[code] != instrument:
                    raise ValueError("universe_code_identity_changed")
                identities[code] = instrument
            features = compute_features(raw, as_of=f"{last.date()}T15:05:00+08:00")
            chunk = features.loc[features.trade_date.ge(first)].copy()
            for name in ("signal_emotion", "market_direction", "emotion_episode", "market_row_present"):
                chunk[name] = [contexts[str(d.date())][name] for d in chunk.trade_date]
            for day in sessions[offset:offset + 20]:
                group = chunk.loc[chunk.trade_date.eq(pd.Timestamp(day))]
                factor_counts = {}
                for f in FACTORS:
                    values = group.loc[group.eligible & np.isfinite(group[f["id"]]), f["id"]]
                    distinct = int(values.nunique())
                    factor_counts[f["id"]] = {"finite_names": len(values), "distinct_values": distinct,
                                               "rankable": len(values) >= 5 and distinct >= 2}
                days.append({"trade_date": day, **contexts[day], "input_names": len(group),
                             "eligible_names": int(group.eligible.sum()),
                             "ineligible_names": int((~group.eligible).sum()),
                             "factors": factor_counts})
            writer.write_table(pa.Table.from_pandas(chunk[panel_schema().names], schema=panel_schema(), preserve_index=False))
            total += len(chunk)
            eligible_total += int(chunk.eligible.sum())
            progress(json.dumps({"phase": "universe_features", "period": f"{first.date()}—{last.date()}",
                                 "stock_days": total, "completed_sessions": min(offset + 20, len(sessions))}))
            del raw, features, chunk
            gc.collect()
            pa.default_memory_pool().release_unused()
    finally:
        writer.close()
    for path, ref in zip(paths, references):
        if sha(path) != ref["sha256"]:
            raise ValueError("universe_input_changed_during_build")
    if total != manifest.get("counts", {}).get("stock_feature_rows"):
        raise ValueError("universe_source_rows_not_preserved")
    panel_sha = sha(partial)
    partial.rename(panel)
    result = {
        "schema": CONTRACT["schema"], "lane": "universe_features", "contract": CONTRACT,
        "inputs": references, "dataset_bindings": manifest["bindings"],
        "artifact": {"name": panel.name, "sha256": panel_sha, "bytes": panel.stat().st_size,
                     "rows": total, "columns": panel_schema().names},
        "input_stock_days": total, "eligible_stock_days": eligible_total,
        "ineligible_stock_days": total - eligible_total, "instruments": len(identities),
        "market_sessions": len(sessions), "daily_coverage": days,
        "coverage": summarize_days(days), "state_labels": STATES, "direction_labels": DIRECTIONS,
        "interpretation": "完整输入股票池的五项历史特征已保存；未按战法筛选，未计算未来收益，不授予因子或策略资格。",
        "admission_blockers": ["历史数据已使用，不能称为新盲测", "尚需事前假设、基线、成本后评价及试验家族校正",
                               "研究覆盖沪深主板与创业板，不包含科创板和北交所", "可分组只说明输入数量，不能证明预测能力"],
        **{k: CONTRACT[k] for k in ("research_only", "historical_reuse", "unseen_out_of_sample", "outcomes_computed",
                                   "candidate_selected", "production_eligible", "portfolio_return", "generates_alert", "automatic_trading")},
    }
    write_new(output, result)
    return {k: v for k, v in result.items() if k not in {"daily_coverage", "inputs", "dataset_bindings"}}
