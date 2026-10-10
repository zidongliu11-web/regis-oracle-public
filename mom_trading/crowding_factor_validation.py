"""Freeze crowding-factor comparison groups before any outcome is joined."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def freeze_quintiles(signals: pd.DataFrame) -> pd.DataFrame:
    """Use the complete signal universe, including subsequently unresolved names.

    Only signal-date identities and factor values are read. Other columns, if
    present, cannot influence membership, quintile size or tie-breaking.
    """
    required = ["signal_day", "code", "factor"]
    if not set(required).issubset(signals.columns):
        raise ValueError("crowding_signal_columns_missing")
    frame = signals[required].copy()
    if frame[required].isna().any().any():
        raise ValueError("crowding_signal_identity_missing")
    if not frame.code.map(lambda value: isinstance(value, str)).all() or not frame.code.str.fullmatch(r"\d{6}").all():
        raise ValueError("crowding_security_identity_invalid")
    if frame.duplicated(["signal_day", "code"]).any():
        raise ValueError("crowding_duplicate_signal_identity")
    frame["factor"] = pd.to_numeric(frame.factor, errors="raise").astype(float)
    if not np.isfinite(frame.factor.to_numpy()).all():
        raise ValueError("crowding_factor_not_finite")
    dates = pd.to_datetime(frame.signal_day, errors="raise").dt.strftime("%Y-%m-%d")
    if not dates.eq(frame.signal_day).all():
        raise ValueError("crowding_signal_date_not_canonical")
    result = []
    for day, group in frame.groupby("signal_day", sort=True):
        if len(group) < 20 or group.factor.nunique() < 2:
            continue
        count = math.ceil(len(group) * .20)
        ordered = group.sort_values(["factor", "code"], ascending=[False, True], kind="mergesort")
        for label, selected in (("top", ordered.head(count)), ("bottom", ordered.tail(count))):
            result.append(selected.assign(quintile=label, signal_universe_count=len(group)))
    return pd.concat(result, ignore_index=True) if result else pd.DataFrame(
        columns=required + ["quintile", "signal_universe_count"]
    )


def cohort_difference(expected: pd.DataFrame, saved: pd.DataFrame) -> dict:
    columns = ["signal_day", "code", "quintile"]
    for frame in (expected, saved):
        if not set(columns).issubset(frame.columns) or frame.duplicated(columns).any():
            raise ValueError("crowding_selected_identity_invalid")
    expected_keys = set(expected[columns].itertuples(index=False, name=None))
    saved_keys = set(saved[columns].itertuples(index=False, name=None))
    missing, extra = expected_keys - saved_keys, saved_keys - expected_keys
    return {
        "expected_selected_rows": len(expected_keys), "saved_selected_rows": len(saved_keys),
        "missing_pre_outcome_members": [dict(zip(columns, row)) for row in sorted(missing)],
        "extra_post_outcome_members": [dict(zip(columns, row)) for row in sorted(extra)],
        "affected_signal_days": sorted({row[0] for row in missing | extra}),
        "same_cohorts": not missing and not extra,
    }
