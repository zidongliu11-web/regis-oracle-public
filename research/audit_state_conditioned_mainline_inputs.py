"""Independent input audit for the first state-routed strategy candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from collections import deque
from pathlib import Path
from statistics import fmean
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "research/data/state_conditioned_mainline_swing_v1/input_manifest.json"
MANIFEST_SHA256 = "0019e609fd94e9e5a81668a9caf471b361a758edb945c42d1e8ce4ab9418354a"
RAW_PANEL_PATH = ROOT / "research/backtest_project/data/universe_daily.parquet"
INDUSTRY_PATH = ROOT / "research/data/pit_industry_intervals_v1/pit_industry_intervals.csv"
REPORT_PATH = ROOT / "research/STATE_CONDITIONED_MAINLINE_SWING_V1_INPUT_AUDIT.json"
RAW_NAMES = (
    "up_ratio_pct", "above_ma20_ratio_pct", "new_high_minus_low_ratio_pct",
    "yesterday_limit_continuation_ratio_pct", "highest_board",
    "closed_limit_ratio_pct", "eod_failed_limit_ratio_pct",
    "top_5pct_amount_share_pct",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _raw(row: dict[str, Any]) -> dict[str, float | None]:
    return {
        "up_ratio_pct": row["B"]["up_ratio_pct"],
        "above_ma20_ratio_pct": row["B"]["above_ma20_ratio_pct"],
        "new_high_minus_low_ratio_pct": row["B"]["new_high_minus_low_ratio_pct"],
        "yesterday_limit_continuation_ratio_pct": row["L"]["yesterday_limit_continuation_ratio_pct"],
        "highest_board": float(row["L"]["highest_board"]),
        "closed_limit_ratio_pct": row["L"]["closed_limit_ratio_pct"],
        "eod_failed_limit_ratio_pct": row["F"]["eod_failed_limit_ratio_pct"],
        "top_5pct_amount_share_pct": row["C"]["top_5pct_amount_share_pct"],
    }


def _midrank(prior: deque[float | None], value: float | None) -> float | None:
    valid = sorted(float(item) for item in prior if item is not None)
    if value is None or len(valid) < 252:
        return None
    less = sum(item < value for item in valid)
    equal = sum(item == value for item in valid)
    return round((less + 0.5 * equal) / len(valid), 6)


def _state(features: dict[str, Any]) -> dict[str, Any]:
    percentiles = [features.get(f"{name}_percentile") for name in RAW_NAMES]
    if any(value is None for value in percentiles):
        return {
            "B_strength": None, "L_strength": None, "F_stress": None,
            "C_stress": None, "directional_strength": None,
            "raw_state": "UNKNOWN", "extreme_risk_off": False,
        }
    b_strength = fmean([
        features["up_ratio_pct_percentile"],
        features["above_ma20_ratio_pct_percentile"],
        features["new_high_minus_low_ratio_pct_percentile"],
    ])
    l_strength = fmean([
        features["yesterday_limit_continuation_ratio_pct_percentile"],
        features["highest_board_percentile"],
        features["closed_limit_ratio_pct_percentile"],
    ])
    f_stress = features["eod_failed_limit_ratio_pct_percentile"]
    c_stress = features["top_5pct_amount_share_pct_percentile"]
    directional = fmean([b_strength, l_strength, 1.0 - f_stress])
    extreme = f_stress >= 0.95 or directional <= 0.10
    if f_stress >= 0.90 or directional <= 0.33:
        raw_state = "RISK_OFF"
    elif directional >= 0.67 and c_stress >= 0.90:
        raw_state = "OVERHEATED"
    elif directional >= 0.67:
        raw_state = "RISK_ON"
    else:
        raw_state = "STABILIZING"
    return {
        "B_strength": round(b_strength, 6), "L_strength": round(l_strength, 6),
        "F_stress": round(f_stress, 6), "C_stress": round(c_stress, 6),
        "directional_strength": round(directional, 6),
        "raw_state": raw_state, "extreme_risk_off": extreme,
    }


def independently_recompute_states(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    windows = {name: deque(maxlen=504) for name in RAW_NAMES}
    previous_raw = "UNKNOWN"
    confirmed = "UNKNOWN"
    exposures = {"UNKNOWN": 0, "RISK_OFF": 20, "STABILIZING": 40, "RISK_ON": 80, "OVERHEATED": 40}
    results: list[dict[str, Any]] = []
    for row in rows:
        raw = _raw(row)
        features: dict[str, Any] = dict(raw)
        for name in RAW_NAMES:
            features[f"{name}_percentile"] = _midrank(windows[name], raw[name])
        state = _state(features)
        raw_state = state["raw_state"]
        if raw_state == "UNKNOWN":
            confirmed = "UNKNOWN"
        elif state["extreme_risk_off"]:
            confirmed = "RISK_OFF"
        elif raw_state == previous_raw:
            confirmed = raw_state
        features.update(state)
        features["confirmed_state"] = confirmed
        features["research_exposure_hypothesis_pct"] = exposures[confirmed]
        features["applied_to_live_positions"] = False
        results.append(features)
        for name in RAW_NAMES:
            windows[name].append(raw[name])
        previous_raw = raw_state
    return results


def _ordered_stock_identity(path: Path) -> dict[str, Any]:
    file = pq.ParquetFile(path)
    prior: tuple[pd.Timestamp, str] | None = None
    rows = 0
    instruments: set[str] = set()
    matched = 0
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None
    errors: list[str] = []
    for batch in file.iter_batches(
        batch_size=131_072,
        columns=["stable_instrument_id", "trade_date", "industry_code"],
    ):
        frame = batch.to_pandas()
        if frame[["stable_instrument_id", "trade_date"]].isna().any().any():
            errors.append("stock_identity_null")
            continue
        identities = list(zip(frame["stable_instrument_id"].astype(str), frame["trade_date"], strict=True))
        for identity in identities:
            if prior is not None and identity <= prior:
                errors.append("stock_identity_not_strictly_ordered_or_duplicate")
                break
            prior = identity
        instruments.update(frame["stable_instrument_id"].astype(str).unique())
        matched += int(frame["industry_code"].notna().sum())
        batch_start, batch_end = frame["trade_date"].min(), frame["trade_date"].max()
        start = batch_start if start is None else min(start, batch_start)
        end = batch_end if end is None else max(end, batch_end)
        rows += len(frame)
    return {
        "errors": sorted(set(errors)), "rows": rows,
        "stable_instruments": len(instruments), "PIT_industry_matched_rows": matched,
        "start": start.date().isoformat() if start is not None else None,
        "end": end.date().isoformat() if end is not None else None,
        "sample_instruments": sorted(instruments)[:16],
    }


def _rolling_sample_errors(stock_path: Path, sample_ids: list[str]) -> list[str]:
    columns = [
        "stable_instrument_id", "trade_date", "high", "low", "close", "prev_close",
        "amount", "adj_factor", "adj_close", "sma20", "ret20", "median_amount20",
        "adv20", "atr14", "factor_changed", "code", "industry_code", "industry_name",
    ]
    output = pq.read_table(
        stock_path, columns=columns,
        filters=[("stable_instrument_id", "in", sample_ids)],
    ).to_pandas()
    raw = pq.read_table(
        RAW_PANEL_PATH,
        columns=[
            "stable_instrument_id", "trade_date", "high", "low", "close", "amount",
            "adj_factor", "adj_close",
        ],
        filters=[("stable_instrument_id", "in", sample_ids)],
    ).to_pandas()
    output.sort_values(["stable_instrument_id", "trade_date"], inplace=True)
    raw.sort_values(["stable_instrument_id", "trade_date"], inplace=True)
    grouped = raw.groupby("stable_instrument_id", sort=False, observed=True)
    raw["expected_prev_close"] = grouped["close"].shift(1)
    raw["expected_factor_changed"] = (
        grouped["adj_factor"].shift(1).notna()
        & ~np.isclose(raw["adj_factor"], grouped["adj_factor"].shift(1), rtol=0, atol=1e-12)
    )
    raw["expected_sma20"] = grouped["adj_close"].rolling(20, min_periods=20).mean().reset_index(level=0, drop=True)
    raw["expected_ret20"] = raw["adj_close"].div(grouped["adj_close"].shift(20)).sub(1.0)
    raw["expected_median_amount20"] = grouped["amount"].rolling(20, min_periods=20).median().reset_index(level=0, drop=True)
    raw["expected_adv20"] = grouped["amount"].rolling(20, min_periods=20).mean().reset_index(level=0, drop=True)
    true_range = pd.concat([
        raw["high"].sub(raw["low"]).abs(),
        raw["high"].sub(raw["expected_prev_close"]).abs(),
        raw["low"].sub(raw["expected_prev_close"]).abs(),
    ], axis=1).max(axis=1).mask(raw["expected_factor_changed"])
    raw["expected_atr14"] = true_range.groupby(
        raw["stable_instrument_id"], sort=False, observed=True,
    ).rolling(14, min_periods=14).mean().reset_index(level=0, drop=True)
    raw = raw.loc[raw["trade_date"].ge(pd.Timestamp("2021-01-01"))]
    joined = output.merge(raw, on=["stable_instrument_id", "trade_date"], how="left", validate="one_to_one")
    errors: list[str] = []
    for actual, expected in (
        ("prev_close", "expected_prev_close"), ("sma20", "expected_sma20"),
        ("ret20", "expected_ret20"), ("median_amount20", "expected_median_amount20"),
        ("adv20", "expected_adv20"), ("atr14", "expected_atr14"),
    ):
        equal = np.isclose(joined[actual], joined[expected], rtol=1e-10, atol=1e-10, equal_nan=True)
        if not bool(equal.all()):
            errors.append(f"sample_{actual}_recompute_mismatch")
    if not joined["factor_changed"].astype(bool).equals(joined["expected_factor_changed"].astype(bool)):
        errors.append("sample_factor_changed_recompute_mismatch")

    intervals = pd.read_csv(INDUSTRY_PATH, dtype={"code": str, "canonical_industry_code": str})
    intervals["code"] = intervals["code"].str.zfill(6)
    intervals["start"] = pd.to_datetime(
        intervals["effective_from"], utc=False,
    ).dt.tz_localize(None)
    intervals["end"] = pd.to_datetime(
        intervals["effective_to"], utc=False,
    ).dt.tz_localize(None)
    interval_map = {code: group for code, group in intervals.groupby("code", sort=False)}
    for row in output.loc[output["industry_code"].notna()].itertuples(index=False):
        candidates = interval_map.get(str(row.code))
        if candidates is None:
            errors.append("sample_industry_interval_missing")
            break
        decision_at = row.trade_date + pd.Timedelta(hours=15)
        matched = candidates.loc[
            candidates["start"].le(decision_at) & candidates["end"].gt(decision_at)
        ]
        if len(matched) != 1 or str(matched.iloc[0]["canonical_industry_code"]) != str(row.industry_code):
            errors.append("sample_industry_interval_mismatch")
            break
    return sorted(set(errors))


def run_audit() -> dict[str, Any]:
    errors: list[str] = []
    if _sha256(MANIFEST_PATH) != MANIFEST_SHA256:
        errors.append("input_manifest_sha256_mismatch")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for name, binding in (manifest.get("bindings") or {}).items():
        path = ROOT / str(binding.get("path") or "")
        if not path.is_file() or _sha256(path) != binding.get("sha256"):
            errors.append(f"binding_invalid:{name}")
    output_paths: dict[str, Path] = {}
    for name, binding in (manifest.get("outputs") or {}).items():
        path = ROOT / str(binding.get("path") or "")
        output_paths[name] = path
        if not path.is_file() or _sha256(path) != binding.get("sha256"):
            errors.append(f"output_invalid:{name}")

    market = json.loads(output_paths["market_state_inputs"].read_text(encoding="utf-8"))
    rows = market.get("rows") if isinstance(market.get("rows"), list) else []
    dates = [str(row.get("trade_date") or "") for row in rows]
    if dates != sorted(set(dates)):
        errors.append("market_dates_not_unique_and_ordered")
    recomputed = independently_recompute_states(rows)
    if len(recomputed) != len(rows) or any(
        expected != row.get("features") for expected, row in zip(recomputed, rows, strict=True)
    ):
        errors.append("market_state_independent_recompute_mismatch")
    if market.get("contains_strategy_returns") is not False:
        errors.append("market_output_outcome_boundary_invalid")

    stock_path = output_paths["stock_features"]
    identity = _ordered_stock_identity(stock_path)
    errors.extend(identity["errors"])
    errors.extend(_rolling_sample_errors(stock_path, identity["sample_instruments"]))
    counts = manifest.get("counts") or {}
    expected = {
        "market_sessions": len(rows), "stock_feature_rows": identity["rows"],
        "stable_instruments": identity["stable_instruments"],
        "PIT_industry_matched_rows": identity["PIT_industry_matched_rows"],
    }
    for key, value in expected.items():
        if counts.get(key) != value:
            errors.append(f"manifest_count_mismatch:{key}")
    forbidden = {"future_return", "holding_return", "label", "pnl", "strategy_return", "performance"}
    if forbidden.intersection(pq.ParquetFile(stock_path).schema_arrow.names):
        errors.append("stock_output_contains_outcome_column")
    boundary = manifest.get("outcome_boundary") or {}
    if any(boundary.get(key) is not False for key in (
        "candidate_selection_run", "future_holding_return_computed", "orders_or_fills_generated",
        "strategy_performance_computed", "live_state_or_position_changed",
    )):
        errors.append("manifest_outcome_boundary_invalid")

    report = {
        "schema_version": 1,
        "audit_id": "state_conditioned_mainline_swing_v1_input_audit",
        "status": "input_integrity_ready" if not errors else "input_integrity_failed",
        "bindings": {
            "input_manifest": {"path": str(MANIFEST_PATH.relative_to(ROOT)), "sha256": _sha256(MANIFEST_PATH)},
            "market_state_inputs": manifest["outputs"]["market_state_inputs"],
            "stock_features": manifest["outputs"]["stock_features"],
        },
        "verified": {
            "errors": sorted(set(errors)), "market_sessions": len(rows),
            "market_state_exact_independent_recompute": "market_state_independent_recompute_mismatch" not in errors,
            "stock_feature_rows": identity["rows"],
            "stable_instruments": identity["stable_instruments"],
            "stock_identity_strictly_ordered_and_unique": not identity["errors"],
            "rolling_feature_sample_instruments": len(identity["sample_instruments"]),
            "PIT_industry_matched_rows": identity["PIT_industry_matched_rows"],
            "PIT_industry_match_pct": round(
                identity["PIT_industry_matched_rows"] / identity["rows"] * 100.0, 6
            ) if identity["rows"] else 0.0,
            "date_range": [identity["start"], identity["end"]],
        },
        "permissions": {
            "freeze_strategy_implementation": not errors,
            "run_real_backtest_now": False,
            "change_live_score_or_position": False,
            "production": False,
            "automatic_trading": False,
            "stable_profit_claim": False
        }
    }
    _atomic_bytes(REPORT_PATH, _canonical_bytes(report))
    return report | {"path": str(REPORT_PATH.relative_to(ROOT)), "sha256": _sha256(REPORT_PATH)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit routed-strategy input epoch")
    parser.add_argument("command", choices=("audit",))
    parser.parse_args()
    report = run_audit()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "input_integrity_ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
