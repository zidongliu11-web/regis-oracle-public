"""Build input-only state and stock features for the first routed strategy.

The builder deliberately stops before candidate selection, fills, PnL or any
performance statistic.  Every rolling feature at date T uses T and earlier;
the market-state percentiles use sessions strictly before T.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from research.build_market_state_feature_epoch_v2 import _apply_calendar_features


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = ROOT / "research/STATE_CONDITIONED_MAINLINE_SWING_V1_PROTOCOL.json"
PROTOCOL_SHA256 = "990e0cdad9196d9be07de8d70c787e9125c794ead0b96df166121d5526ff77b3"
AMENDMENT_PATH = (
    ROOT / "research/STATE_CONDITIONED_MAINLINE_SWING_V1_PROTOCOL_AMENDMENT_1.json"
)
PANEL_PATH = ROOT / "research/backtest_project/data/universe_daily.parquet"
INDUSTRY_PATH = ROOT / "research/data/pit_industry_intervals_v1/pit_industry_intervals.csv"
CALENDAR_PATH = ROOT / "reports/v4_market_calendar.json"
SPECIAL_EVENT_PATH = ROOT / "research/data/special_price_limit_events.csv"
OUTPUT_DIR = ROOT / "research/data/state_conditioned_mainline_swing_v1"

PANEL_COLUMNS = [
    "ts_code", "trade_date", "open", "high", "low", "close", "amount",
    "turnover_rate", "pct_chg", "is_st", "trade_status", "adj_factor",
    "adj_close", "float_mv", "board", "adjustment_trusted",
    "stable_instrument_id", "source_snapshot_sha256",
]
SUPPORTED_BOARDS = {"MAIN_SH", "MAIN_SZ", "GEM"}


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


def _normalize_code(value: object) -> str:
    raw = str(value or "").strip()
    if raw.startswith(("sh.", "sz.")):
        return raw.split(".", 1)[1]
    return raw.split(".", 1)[0]


def _midrank(values: pd.Series, *, ascending: bool = True) -> pd.Series:
    if values.empty:
        return pd.Series(dtype="float64", index=values.index)
    ranked = values.rank(method="average", ascending=ascending)
    if len(values) == 1:
        return pd.Series(1.0, index=values.index, dtype="float64")
    return (ranked - 1.0) / (len(values) - 1.0)


def _verify_frozen_inputs() -> dict[str, dict[str, str]]:
    amendment_sha = _sha256(AMENDMENT_PATH)
    amendment = json.loads(AMENDMENT_PATH.read_text(encoding="utf-8"))
    if _sha256(PROTOCOL_PATH) != PROTOCOL_SHA256:
        raise RuntimeError("strategy protocol sha256 mismatch")
    if amendment.get("status") != (
        "immutable_panel_market_state_rebuild_frozen_before_input_build_or_outcome_read"
    ):
        raise RuntimeError("strategy protocol amendment status invalid")
    bindings = {
        "protocol": {"path": str(PROTOCOL_PATH.relative_to(ROOT)), "sha256": PROTOCOL_SHA256},
        "amendment": {"path": str(AMENDMENT_PATH.relative_to(ROOT)), "sha256": amendment_sha},
    }
    for name, binding in (amendment.get("additional_bindings") or {}).items():
        path = ROOT / str(binding.get("path") or "")
        if not path.is_file() or _sha256(path) != binding.get("sha256"):
            raise RuntimeError(f"frozen input binding invalid: {name}")
        bindings[name] = {"path": str(path.relative_to(ROOT)), "sha256": _sha256(path)}
    predecessor = amendment.get("predecessor") or {}
    if predecessor.get("sha256") != PROTOCOL_SHA256:
        raise RuntimeError("strategy predecessor binding invalid")
    if _sha256(INDUSTRY_PATH) != json.loads(
        PROTOCOL_PATH.read_text(encoding="utf-8")
    )["bindings"]["point_in_time_industry_intervals"]["sha256"]:
        raise RuntimeError("PIT industry binding invalid")
    bindings["point_in_time_industry_intervals"] = {
        "path": str(INDUSTRY_PATH.relative_to(ROOT)), "sha256": _sha256(INDUSTRY_PATH),
    }
    return bindings


def _calendar() -> tuple[list[str], dict[pd.Timestamp, int]]:
    document = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
    dates = [str(value) for value in (document.get("trading_dates") or [])]
    if not dates or dates != sorted(set(dates)):
        raise RuntimeError("audited trading calendar invalid")
    return dates, {pd.Timestamp(day): index for index, day in enumerate(dates)}


def _special_event_keys() -> set[tuple[str, pd.Timestamp]]:
    events = pd.read_csv(SPECIAL_EVENT_PATH, dtype=str)
    events = events.loc[events["verification_status"].eq("verified")].copy()
    return {
        (_normalize_code(code), pd.Timestamp(day))
        for code, day in zip(events["code"], events["date"], strict=True)
    }


def _load_panel() -> pd.DataFrame:
    schema = pq.ParquetFile(PANEL_PATH).schema_arrow
    missing = sorted(set(PANEL_COLUMNS) - set(schema.names))
    if missing:
        raise RuntimeError(f"daily universe columns missing: {missing}")
    frame = pq.read_table(PANEL_PATH, columns=PANEL_COLUMNS).to_pandas()
    frame = frame.loc[frame["board"].isin(SUPPORTED_BOARDS)].copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.normalize()
    frame["code"] = frame["ts_code"].map(_normalize_code)
    frame.sort_values(["stable_instrument_id", "trade_date"], inplace=True)
    frame.reset_index(drop=True, inplace=True)
    if frame.duplicated(["stable_instrument_id", "trade_date"]).any():
        raise RuntimeError("duplicate stable-instrument daily identity")
    return frame


def _stock_features(
    frame: pd.DataFrame, calendar_index: dict[pd.Timestamp, int],
    special_events: set[tuple[str, pd.Timestamp]],
) -> pd.DataFrame:
    output = frame.copy()
    output["session_index"] = output["trade_date"].map(calendar_index)
    if output["session_index"].isna().any():
        raise RuntimeError("daily universe contains dates outside audited calendar")
    output["session_index"] = output["session_index"].astype("int32")
    grouped = output.groupby("stable_instrument_id", sort=False, observed=True)
    output["history_sessions"] = grouped.cumcount().add(1).astype("int32")
    output["prev_close"] = grouped["close"].shift(1)
    output["prev_adj_factor"] = grouped["adj_factor"].shift(1)
    output["factor_changed"] = (
        output["prev_adj_factor"].notna()
        & output["adj_factor"].notna()
        & ~np.isclose(output["adj_factor"], output["prev_adj_factor"], rtol=0, atol=1e-12)
    )
    rolling_close = grouped["adj_close"].rolling(20, min_periods=20)
    output["sma20"] = rolling_close.mean().reset_index(level=0, drop=True)
    output["high20"] = rolling_close.max().reset_index(level=0, drop=True)
    output["low20"] = rolling_close.min().reset_index(level=0, drop=True)
    output["ret20"] = output["adj_close"].div(grouped["adj_close"].shift(20)).sub(1.0)
    rolling_amount = grouped["amount"].rolling(20, min_periods=20)
    output["median_amount20"] = rolling_amount.median().reset_index(level=0, drop=True)
    output["adv20"] = rolling_amount.mean().reset_index(level=0, drop=True)

    true_range = pd.concat([
        output["high"].sub(output["low"]).abs(),
        output["high"].sub(output["prev_close"]).abs(),
        output["low"].sub(output["prev_close"]).abs(),
    ], axis=1).max(axis=1)
    true_range = true_range.mask(output["factor_changed"])
    output["atr14"] = true_range.groupby(
        output["stable_instrument_id"], sort=False, observed=True,
    ).rolling(14, min_periods=14).mean().reset_index(level=0, drop=True)

    previous_session = grouped["session_index"].shift(1)
    output["consecutive_session"] = output["session_index"].eq(previous_session.add(1))
    rate = np.where(output["board"].eq("GEM"), 0.20, 0.10)
    raw_upper = output["prev_close"].to_numpy(dtype="float64") * (1.0 + rate)
    output["upper_limit"] = np.floor(raw_upper * 100.0 + 0.500000001) / 100.0
    event_keys = pd.MultiIndex.from_tuples(
        sorted(special_events), names=["code", "trade_date"],
    )
    row_keys = pd.MultiIndex.from_arrays(
        [output["code"], output["trade_date"]], names=["code", "trade_date"],
    )
    special_no_limit = row_keys.isin(event_keys)
    output["limit_evaluable"] = (
        output["trade_status"].eq(1)
        & output["is_st"].eq(0)
        & output["prev_close"].gt(0)
        & output["high"].notna()
        & output["close"].notna()
        & output["history_sessions"].gt(5)
        & ~output["factor_changed"]
        & ~special_no_limit
    )
    output["limit_touched"] = (
        output["limit_evaluable"]
        & output["high"].ge(output["upper_limit"].sub(0.0051))
    )
    output["limit_closed"] = (
        output["limit_evaluable"]
        & output["close"].sub(output["upper_limit"]).abs().le(0.0051)
    )
    previous_closed = grouped["limit_closed"].shift(1).fillna(False).astype(bool)
    output["prior_limit_continuation_evaluable"] = (
        output["limit_evaluable"] & output["consecutive_session"] & previous_closed
    )
    continuation = output["limit_closed"] & output["consecutive_session"] & previous_closed
    run_start = ~continuation
    run_id = run_start.groupby(output["stable_instrument_id"], sort=False).cumsum()
    output["limit_streak"] = (
        output["limit_closed"].astype("int16")
        .groupby([output["stable_instrument_id"], run_id], sort=False)
        .cumsum().astype("int16")
    )
    return output


def _market_state_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for day, daily in frame.groupby("trade_date", sort=True, observed=True):
        active = daily.loc[daily["trade_status"].eq(1) & daily["pct_chg"].notna()]
        ma20 = active.loc[active["sma20"].notna() & active["adj_close"].notna()]
        limit_rows = daily.loc[daily["limit_evaluable"]]
        continuation = daily.loc[daily["prior_limit_continuation_evaluable"]]
        amounts = active.loc[active["amount"].notna() & active["amount"].ge(0), "amount"]
        turnovers = active.loc[
            active["turnover_rate"].notna() & active["turnover_rate"].ge(0),
            "turnover_rate",
        ]
        up = int(active["pct_chg"].gt(0).sum())
        down = int(active["pct_chg"].lt(0).sum())
        flat = int(active["pct_chg"].eq(0).sum())
        usable = up + down + flat
        touch = int(limit_rows["limit_touched"].sum())
        closed = int(limit_rows["limit_closed"].sum())
        failed = int((limit_rows["limit_touched"] & ~limit_rows["limit_closed"]).sum())
        sorted_amounts = amounts.sort_values(ascending=False)
        top_count = max(1, math.ceil(len(sorted_amounts) * 0.05)) if len(sorted_amounts) else 0
        total_amount = float(sorted_amounts.sum())

        def pct(numerator: float, denominator: float) -> float | None:
            return round(float(numerator) / float(denominator) * 100.0, 3) if denominator else None

        rows.append({
            "trade_date": day.date().isoformat(),
            "B": {
                "usable": usable, "up_count": up, "down_count": down, "flat_count": flat,
                "up_ratio_pct": pct(up, usable),
                "median_return_pct": round(float(active["pct_chg"].median()), 4) if usable else None,
                "ma20_usable": int(len(ma20)),
                "above_ma20_count": int(ma20["adj_close"].gt(ma20["sma20"]).sum()),
                "above_ma20_ratio_pct": pct(ma20["adj_close"].gt(ma20["sma20"]).sum(), len(ma20)),
                "new_high_20d_count": int(ma20["adj_close"].ge(ma20["high20"]).sum()),
                "new_low_20d_count": int(ma20["adj_close"].le(ma20["low20"]).sum()),
                "new_high_minus_low_ratio_pct": pct(
                    ma20["adj_close"].ge(ma20["high20"]).sum()
                    - ma20["adj_close"].le(ma20["low20"]).sum(), usable,
                ),
                "st_count": int(active["is_st"].eq(1).sum()),
            },
            "L": {
                "first_board_count": int(limit_rows["limit_streak"].eq(1).sum()),
                "second_board_count": int(limit_rows["limit_streak"].eq(2).sum()),
                "third_board_count": int(limit_rows["limit_streak"].eq(3).sum()),
                "four_plus_board_count": int(limit_rows["limit_streak"].ge(4).sum()),
                "highest_board": int(limit_rows["limit_streak"].max()) if len(limit_rows) else 0,
                "yesterday_limit_continuation_denominator": int(len(continuation)),
                "yesterday_limit_continuation_numerator": int(continuation["limit_closed"].sum()),
                "yesterday_limit_continuation_ratio_pct": pct(
                    continuation["limit_closed"].sum(), len(continuation),
                ),
                "closed_limit_ratio_pct": pct(closed, usable),
            },
            "F": {
                "limit_touch_count": touch, "closed_limit_count": closed,
                "eod_failed_limit_count": failed,
                "eod_failed_limit_ratio_pct": pct(failed, touch),
            },
            "C": {
                "total_amount_yi": round(total_amount / 100_000_000.0, 3),
                "top_5pct_amount_share_pct": pct(sorted_amounts.iloc[:top_count].sum(), total_amount),
                "turnover_median_pct": round(float(turnovers.median()) * 100.0, 4) if len(turnovers) else None,
                "turnover_p95_pct": round(float(turnovers.quantile(0.95)) * 100.0, 4) if len(turnovers) else None,
                "limit_stock_amount_share_pct": pct(
                    limit_rows.loc[limit_rows["limit_closed"], "amount"].sum(), total_amount,
                ),
                "cross_section_return_dispersion_pct": round(float(active["pct_chg"].std(ddof=0)), 4) if usable > 1 else None,
            },
        })
    _apply_calendar_features(rows)
    return rows


def _assign_pit_industries(frame: pd.DataFrame) -> pd.DataFrame:
    intervals = pd.read_csv(INDUSTRY_PATH, dtype={"code": str, "canonical_industry_code": str})
    required = {"code", "effective_from", "effective_to", "canonical_industry_code", "canonical_industry_name"}
    if not required.issubset(intervals.columns):
        raise RuntimeError("PIT industry interval schema invalid")
    intervals["code"] = intervals["code"].str.zfill(6)
    # Effective dates are Shanghai civil dates.  Converting +08:00 to UTC before
    # normalizing would move midnight back one day and leak the new identity.
    intervals["effective_from"] = pd.to_datetime(
        intervals["effective_from"], utc=False,
    ).dt.tz_localize(None)
    intervals["effective_to"] = pd.to_datetime(
        intervals["effective_to"], utc=False,
    ).dt.tz_localize(None)
    by_code = {
        code: group.sort_values("effective_from")
        for code, group in intervals.groupby("code", sort=False, observed=True)
    }
    industry_code = np.full(len(frame), None, dtype=object)
    industry_name = np.full(len(frame), None, dtype=object)
    for code, indices in frame.groupby("code", sort=False, observed=True).indices.items():
        source = by_code.get(str(code))
        if source is None or source.empty:
            continue
        positions = np.asarray(indices, dtype=np.int64)
        # A daily decision is made after the 15:00 completed close, so a
        # same-day official publication at 00:00 is available to that decision.
        dates = (
            frame.loc[positions, "trade_date"] + pd.Timedelta(hours=15)
        ).to_numpy(dtype="datetime64[ns]")
        starts = source["effective_from"].to_numpy(dtype="datetime64[ns]")
        ends = source["effective_to"].to_numpy(dtype="datetime64[ns]")
        matched = np.searchsorted(starts, dates, side="right") - 1
        valid = matched >= 0
        clipped = np.maximum(matched, 0)
        # Source contract is half-open: effective_from <= T < effective_to.
        valid &= dates < ends[clipped]
        if not valid.any():
            continue
        target = positions[valid]
        source_positions = clipped[valid]
        industry_code[target] = source["canonical_industry_code"].to_numpy()[source_positions]
        industry_name[target] = source["canonical_industry_name"].to_numpy()[source_positions]
    result = frame.copy()
    result["industry_code"] = industry_code
    result["industry_name"] = industry_name
    return result


def build_input_epoch() -> dict[str, Any]:
    bindings = _verify_frozen_inputs()
    calendar, calendar_index = _calendar()
    panel = _stock_features(_load_panel(), calendar_index, _special_event_keys())
    market_rows = _market_state_rows(panel)
    panel = _assign_pit_industries(panel)
    panel = panel.loc[panel["trade_date"].ge(pd.Timestamp("2021-01-01"))].copy()
    output_columns = [
        "stable_instrument_id", "ts_code", "code", "trade_date", "session_index",
        "open", "high", "low", "close", "prev_close", "amount", "adv20",
        "median_amount20", "pct_chg", "is_st", "trade_status", "adj_factor",
        "adj_close", "sma20", "ret20", "atr14", "float_mv", "board",
        "adjustment_trusted", "factor_changed", "history_sessions",
        "limit_evaluable", "limit_touched", "limit_closed", "limit_streak",
        "industry_code", "industry_name", "source_snapshot_sha256",
    ]
    panel = panel[output_columns]
    panel.sort_values(["stable_instrument_id", "trade_date"], inplace=True)
    panel.reset_index(drop=True, inplace=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    market_path = OUTPUT_DIR / "market_state_inputs.json"
    stock_path = OUTPUT_DIR / "stock_features.parquet"
    _atomic_bytes(market_path, _canonical_bytes({
        "schema": "state_conditioned_mainline_market_state_inputs.v1",
        "scope": "MAIN_SH_MAIN_SZ_GEM",
        "source_panel_sha256": bindings["daily_universe"]["sha256"],
        "contains_strategy_returns": False,
        "applied_to_live_positions": False,
        "rows": market_rows,
    }))
    descriptor, temporary = tempfile.mkstemp(prefix=f".{stock_path.name}.", dir=stock_path.parent)
    os.close(descriptor)
    try:
        table = pa.Table.from_pandas(panel, preserve_index=False)
        pq.write_table(table, temporary, compression="zstd", use_dictionary=True)
        Path(temporary).replace(stock_path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise
    matched = int(panel["industry_code"].notna().sum())
    manifest = {
        "schema": "state_conditioned_mainline_input_manifest.v1",
        "status": "input_epoch_built_no_strategy_outcome_read",
        "bindings": bindings,
        "outputs": {
            "market_state_inputs": {"path": str(market_path.relative_to(ROOT)), "sha256": _sha256(market_path)},
            "stock_features": {"path": str(stock_path.relative_to(ROOT)), "sha256": _sha256(stock_path)},
        },
        "counts": {
            "market_sessions": len(market_rows), "stock_feature_rows": int(len(panel)),
            "stable_instruments": int(panel["stable_instrument_id"].nunique()),
            "PIT_industry_matched_rows": matched,
            "PIT_industry_match_pct": round(matched / len(panel) * 100.0, 6) if len(panel) else 0.0,
            "state_counts": pd.Series([
                row["features"]["confirmed_state"] for row in market_rows
            ]).value_counts().sort_index().to_dict(),
        },
        "date_range": {
            "market_start": market_rows[0]["trade_date"], "market_end": market_rows[-1]["trade_date"],
            "stock_start": panel["trade_date"].min().date().isoformat(),
            "stock_end": panel["trade_date"].max().date().isoformat(),
        },
        "outcome_boundary": {
            "candidate_selection_run": False, "future_holding_return_computed": False,
            "orders_or_fills_generated": False, "strategy_performance_computed": False,
            "live_state_or_position_changed": False,
        },
    }
    manifest_path = OUTPUT_DIR / "input_manifest.json"
    _atomic_bytes(manifest_path, _canonical_bytes(manifest))
    return manifest | {
        "manifest_path": str(manifest_path.relative_to(ROOT)),
        "manifest_sha256": _sha256(manifest_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build input-only routed-strategy epoch")
    parser.add_argument("command", choices=("build",))
    parser.parse_args()
    result = build_input_epoch()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
