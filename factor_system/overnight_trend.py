"""Outcome-blind construction for the monthly A-share overnight-trend factor.

This module only builds the paper's sparse month-end signal.  It deliberately
does not register a candidate, read future returns, backtest, or forward-fill a
monthly value into daily rows.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


LOOKBACK_MONTHS = 12
SKIP_RECENT_MONTHS = 1
USED_MONTHS = LOOKBACK_MONTHS - SKIP_RECENT_MONTHS


def daily_overnight_return(panel: object) -> pd.DataFrame:
    """Return adjusted close-to-open returns using only information at day t."""
    adj_open = getattr(panel, "adj_open", None)
    close = getattr(panel, "close", None)
    if not isinstance(adj_open, pd.DataFrame) or not isinstance(close, pd.DataFrame):
        raise TypeError("overnight trend requires adj_open and adjusted close frames")
    if not adj_open.index.equals(close.index) or not adj_open.columns.equals(close.columns):
        raise ValueError("overnight trend price frames must share identical axes")

    prior_close = close.shift(1)
    valid = (
        np.isfinite(adj_open)
        & np.isfinite(prior_close)
        & adj_open.gt(0)
        & prior_close.gt(0)
    )
    return (adj_open / prior_close - 1.0).where(valid)


def monthly_overnight_return(panel: object) -> pd.DataFrame:
    """Compound observed adjusted overnight returns within each calendar month."""
    daily = daily_overnight_return(panel)
    periods = daily.index.to_period("M")
    values: list[pd.Series] = []
    dates: list[pd.Timestamp] = []
    for _, frame in daily.groupby(periods, sort=True):
        observed = frame.notna().sum(axis=0)
        compounded = (1.0 + frame).prod(axis=0, min_count=1) - 1.0
        values.append(compounded.where(observed.gt(0)))
        dates.append(pd.Timestamp(frame.index[-1]))
    if not values:
        return pd.DataFrame(index=pd.DatetimeIndex([]), columns=daily.columns, dtype=float)
    return pd.DataFrame(values, index=pd.DatetimeIndex(dates)).reindex(columns=daily.columns)


def overnight_trend_monthly_12_1(panel: object) -> pd.DataFrame:
    """Build Eq. (4.1) for J=12 on sparse month-end rows only.

    At month t the signal uses monthly overnight returns t-12 through t-2,
    skips t-1, and divides the 11-month cumulative return by J-1.  The current
    month t is never used.  Missing any required month keeps the signal missing.
    """
    monthly = monthly_overnight_return(panel)

    def _paper_formula(window: np.ndarray) -> float:
        if not np.isfinite(window).all():
            return np.nan
        return float(np.prod(1.0 + window) - 1.0) / USED_MONTHS

    return (
        monthly.rolling(USED_MONTHS, min_periods=USED_MONTHS)
        .apply(_paper_formula, raw=True)
        .shift(SKIP_RECENT_MONTHS + 1)
    )


__all__ = [
    "LOOKBACK_MONTHS",
    "SKIP_RECENT_MONTHS",
    "USED_MONTHS",
    "daily_overnight_return",
    "monthly_overnight_return",
    "overnight_trend_monthly_12_1",
]
