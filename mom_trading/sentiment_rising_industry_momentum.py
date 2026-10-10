"""PIT industry momentum routed by the audited market-state series.

This module builds signals only.  Trade simulation remains in
``factor_system.backtest`` so A-share lots, costs, T+1 timing, price limits,
suspensions, liquidity and terminal liquidation use the shared engine.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STOCK_FEATURES_PATH = PROJECT_ROOT / (
    "research/data/state_conditioned_mainline_swing_v1/stock_features.parquet"
)
MARKET_STATE_PATH = PROJECT_ROOT / (
    "research/data/state_conditioned_mainline_swing_v1/market_state_inputs.json"
)
EXPECTED_STOCK_FEATURES_SHA256 = (
    "8162b2d46851c991fbb68ee789182d21587ca7207659a69696f28ed9a7955c95"
)
EXPECTED_MARKET_STATE_SHA256 = (
    "9f2e1afe0ba6bc3da76981bf0838a31da688fb66bb655687a1a65479d6b9e17c"
)

STATE_EXPOSURE_CAPS = {
    "UNKNOWN": 0.0,
    "RISK_OFF": 0.0,
    "STABILIZING": 0.4,
    "RISK_ON": 0.8,
    "OVERHEATED": 0.4,
}
ELIGIBLE_BOARDS = {"MAIN_SH", "MAIN_SZ", "GEM"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_inputs() -> tuple[pd.DataFrame, dict]:
    if _sha256(STOCK_FEATURES_PATH) != EXPECTED_STOCK_FEATURES_SHA256:
        raise ValueError("PIT stock feature input SHA-256 mismatch")
    if _sha256(MARKET_STATE_PATH) != EXPECTED_MARKET_STATE_SHA256:
        raise ValueError("market-state input SHA-256 mismatch")
    columns = [
        "stable_instrument_id", "code", "trade_date", "adj_close", "adv20",
        "median_amount20", "float_mv", "board", "is_st", "trade_status",
        "adjustment_trusted", "history_sessions", "industry_code",
        "industry_name",
    ]
    stocks = pd.read_parquet(STOCK_FEATURES_PATH, columns=columns)
    states = json.loads(MARKET_STATE_PATH.read_text(encoding="utf-8"))
    return stocks, states


def build_signal_and_exposure(
    panel_dates: pd.DatetimeIndex,
    panel_codes: pd.Index,
    *,
    stocks: pd.DataFrame | None = None,
    states: dict | None = None,
) -> tuple[pd.DataFrame, pd.Series, dict]:
    """Build a daily close signal and a previous-close exposure schedule.

    A non-null cross-section contains one liquid representative per eligible
    PIT industry.  The shared backtester ranks that cross-section and funds the
    five highest-scoring industries.  Signals exist only after a RISK_ON close.
    """
    if stocks is None or states is None:
        verified_stocks, verified_states = _verified_inputs()
        stocks = verified_stocks if stocks is None else stocks
        states = verified_states if states is None else states

    required = {
        "stable_instrument_id", "code", "trade_date", "adj_close", "adv20",
        "median_amount20", "float_mv", "board", "is_st", "trade_status",
        "adjustment_trusted", "history_sessions", "industry_code",
        "industry_name",
    }
    missing = required.difference(stocks.columns)
    if missing:
        raise ValueError(f"stock features missing columns: {sorted(missing)}")

    frame = stocks.loc[:, sorted(required)].copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.normalize()
    frame["code"] = frame["code"].astype(str).str.extract(r"(\d{6})", expand=False)
    frame = frame.sort_values(
        ["stable_instrument_id", "trade_date"], kind="mergesort"
    )
    frame["ret126"] = frame.groupby(
        "stable_instrument_id", sort=False, observed=True
    )["adj_close"].pct_change(126, fill_method=None)

    dates = pd.DatetimeIndex(panel_dates).normalize()
    codes = pd.Index(panel_codes).astype(str)
    code_map = pd.DataFrame({"panel_code": codes})
    code_map["code"] = code_map["panel_code"].str.extract(r"(\d{6})", expand=False)
    if code_map["code"].isna().any() or code_map["code"].duplicated().any():
        raise ValueError("Panel code identity must map one-to-one to six-digit A-share codes")
    panel_code_by_code = code_map.set_index("code")["panel_code"]
    distinct_dates = pd.Series(frame["trade_date"].drop_duplicates().sort_values().to_numpy())
    month_ends = distinct_dates.groupby(distinct_dates.dt.to_period("M")).max()

    eligible = (
        frame["board"].isin(ELIGIBLE_BOARDS)
        & ~frame["is_st"].fillna(True).astype(bool)
        & frame["trade_status"].eq(1)
        & frame["adjustment_trusted"].eq(True)
        & frame["history_sessions"].ge(400)
        & frame["median_amount20"].ge(50_000_000)
        & frame["float_mv"].ge(2_000_000_000)
        & frame["industry_code"].notna()
        & frame["ret126"].notna()
        & np.isfinite(frame["ret126"])
        & frame["adv20"].gt(0)
    )
    month_frame = frame.loc[
        eligible & frame["trade_date"].isin(set(month_ends)),
        ["trade_date", "code", "industry_code", "industry_name", "ret126", "adv20"],
    ].copy()

    industry = month_frame.groupby(
        ["trade_date", "industry_code"], observed=True
    ).agg(
        industry_name=("industry_name", "first"),
        constituent_count=("code", "nunique"),
        industry_momentum=("ret126", "median"),
    ).reset_index()
    industry = industry.loc[industry["constituent_count"].ge(12)].copy()
    eligible_pairs = industry[["trade_date", "industry_code"]]
    representatives = month_frame.merge(
        eligible_pairs, on=["trade_date", "industry_code"], how="inner"
    )
    representatives = representatives.sort_values(
        ["trade_date", "industry_code", "adv20", "code"],
        ascending=[True, True, False, True],
        kind="mergesort",
    ).drop_duplicates(["trade_date", "industry_code"], keep="first")
    representatives = representatives.merge(
        industry,
        on=["trade_date", "industry_code"],
        how="inner",
        suffixes=("", "_industry"),
    )
    representatives = representatives.loc[
        representatives["code"].isin(panel_code_by_code.index)
    ].copy()
    representatives["panel_code"] = representatives["code"].map(panel_code_by_code)

    state_rows = (states or {}).get("rows")
    if not isinstance(state_rows, list):
        raise ValueError("market-state input rows missing")
    state_by_date = {
        pd.Timestamp(row["trade_date"]).normalize(): str(
            (row.get("features") or {}).get("confirmed_state") or "UNKNOWN"
        )
        for row in state_rows
        if isinstance(row, dict) and row.get("trade_date")
    }

    rep_by_month = {
        pd.Timestamp(month_date): group.sort_values(
            ["industry_momentum", "code"], ascending=[False, True], kind="mergesort"
        )
        for month_date, group in representatives.groupby("trade_date", sort=True)
    }
    available_months = pd.DatetimeIndex(sorted(rep_by_month))
    signal = pd.DataFrame(np.nan, index=dates, columns=codes, dtype=float)
    signal_days = 0
    for date in dates:
        if state_by_date.get(date, "UNKNOWN") != "RISK_ON":
            continue
        usable = available_months[available_months <= date]
        if usable.empty:
            continue
        cross_section = rep_by_month[pd.Timestamp(usable[-1])]
        if len(cross_section) < 30:
            continue
        values = cross_section.set_index("panel_code")["industry_momentum"].astype(float)
        # Stable sub-epsilon tie break preserves the economic score ordering.
        tie = pd.Series(
            np.arange(len(values), 0, -1, dtype=float) * 1e-12,
            index=values.index,
        )
        signal.loc[date, values.index] = values + tie
        signal_days += 1

    state_series = pd.Series(
        [state_by_date.get(date, "UNKNOWN") for date in dates],
        index=dates,
        dtype="object",
    )
    # Execution at today's open may use only the prior completed close state.
    exposure = state_series.shift(1).fillna("UNKNOWN").map(STATE_EXPOSURE_CAPS)
    exposure = exposure.astype(float).rename("state_route_exposure")
    diagnostics = {
        "signal_days": int(signal_days),
        "industry_months": int(len(rep_by_month)),
        "minimum_representatives": (
            int(min(map(len, rep_by_month.values()))) if rep_by_month else 0
        ),
        "maximum_representatives": (
            int(max(map(len, rep_by_month.values()))) if rep_by_month else 0
        ),
        "state_counts": {
            str(key): int(value) for key, value in state_series.value_counts().items()
        },
        "signal_non_null_values": int(signal.notna().sum().sum()),
        "timing": "T_close_signal_and_state_to_T_plus_1_open",
    }
    return signal, exposure, diagnostics
