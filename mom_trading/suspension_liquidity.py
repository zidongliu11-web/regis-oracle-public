"""Recover execution ADV only where explicit no-trade rows explain missingness.

The frozen signal features and cohorts are not rebuilt. This is an execution
input correction: 20 preceding market sessions, including confirmed zero-turnover
suspensions, never a shorter window or today's volume.
"""
from __future__ import annotations

from copy import copy
import math

import pandas as pd


SPEC = {
    "schema": "confirmed_suspension_execution_ADV.v1",
    "window": "20_complete_market_sessions_strictly_before_execution_day",
    "zero": "explicit_trade_status_0_and_volume_amount_missing_or_zero_only",
    "unknown": "active_missing_amount_status_conflict_or_missing_session_stays_unknown",
    "existing_positive_ADV": "never_replaced",
    "signal_features_and_cohorts_changed": False,
    "new_blind_trial": False, "production_eligible": False, "generates_alert": False,
}


def valid_number(value):
    return not isinstance(value, bool) and isinstance(value, (float, int)) and math.isfinite(value)


def execution_amount(row: dict) -> tuple[float | None, bool]:
    """None means unknown, not a zero-volume session."""
    status, amount, volume = row.get("trade_status"), row.get("amount"), row.get("vol")
    if isinstance(status, bool) or status not in (0, 1):
        return None, False
    if status == 0:
        missing_or_zero = lambda x: x is None or (isinstance(x, float) and math.isnan(x)) or (valid_number(x) and x == 0)
        if missing_or_zero(amount) and missing_or_zero(volume):
            return 0., True
        return None, False  # A suspension with positive turnover is conflicting evidence.
    if valid_number(amount) and amount >= 0 and valid_number(volume) and volume >= 0:
        if (amount == 0) == (volume == 0):
            return float(amount), False
    return None, False


def suspension_adv_view(market, canonical: pd.DataFrame):
    """Create a private view and leave every previously valid input unchanged."""
    required = {"ts_code", "trade_date", "stable_instrument_id", "trade_status", "amount", "vol"}
    if not required.issubset(canonical.columns):
        raise ValueError("execution_liquidity_canonical_columns_missing")
    frame = canonical.copy()
    frame["day"] = pd.to_datetime(frame.trade_date).dt.strftime("%Y-%m-%d")
    if frame.day.duplicated().any() or frame.ts_code.nunique() != 1:
        raise ValueError("execution_liquidity_source_identity_not_unique")
    rows = {r["day"]: r for r in frame.to_dict("records")}
    for day, row in market.rows.items():
        raw = rows.get(day)
        if (raw is None or raw["ts_code"].split(".")[0] != row["code"]
                or not raw["stable_instrument_id"] or raw["stable_instrument_id"] != row["stable_instrument_id"]
                or raw["trade_status"] != row["trade_status"]):
            raise ValueError("execution_liquidity_market_identity_mismatch")
    view = copy(market)
    view.rows = {day: dict(row) for day, row in market.rows.items()}
    receipts = {}
    for day, row in market.rows.items():
        old = row["lagged_adv"]
        if old is not None and not (isinstance(old, float) and math.isnan(old)):
            continue  # Neither known values nor invalid infinities are missing observations.
        index = market.index[day]
        if index < 20:
            continue
        history = market.sessions[index-20:index]
        values, zeros = [], []
        for previous in history:
            raw = rows.get(previous)
            if raw is None or raw["stable_instrument_id"] != row["stable_instrument_id"]:
                break
            amount, zero = execution_amount(raw)
            if amount is None:
                break
            values.append(amount)
            if zero:
                zeros.append(previous)
        if len(values) != 20 or not zeros:
            continue
        adv = math.fsum(values) / 20
        if adv <= 0:
            continue  # No liquidity; do not invent a positive capacity.
        view.rows[day]["lagged_adv"] = adv
        receipts[day] = {"execution_day": day, "window_start": history[0], "window_end": history[-1],
                         "denominator": 20, "confirmed_no_trade_days": zeros, "ADV_yuan": adv}
    return view, receipts
