"""Diagnose observed bar/quote roles without rebuilding trades or editing bars.

The comparison interval is frozen as (previous minute, minute end]. The opening
candidate uses the last positive snapshot in [09:25,09:30]; it is only a candidate.
Equal timestamp snapshots with different latest prices make it ambiguous.
"""
from __future__ import annotations
import numpy as np
from .quote_quality import ohlc_quality


def opening_bar_quote_roles(opening, high, low, close, bar_clock, quote_clocks, quote_last):
    arrays = [np.asarray(quote_clocks), np.asarray(quote_last)]
    if (arrays[0].ndim != 1 or arrays[1].ndim != 1
            or arrays[0].shape != arrays[1].shape or arrays[0].dtype.kind not in 'iu'
            or arrays[1].dtype != np.dtype('float64')):
        raise ValueError('opening_role_quote_shape_or_dtype')
    clocks, latest = arrays
    if (not isinstance(bar_clock, (int, np.integer)) or isinstance(bar_clock, bool)
            or int(bar_clock) not in (93100, 93200)
            or len(clocks) and (np.diff(clocks.astype('int64')) < 0).any()):
        raise ValueError('opening_role_clock_invalid')
    prices = [np.asarray([x], dtype='float64') for x in (opening, high, low, close)]
    flags = ohlc_quality(*prices)
    if flags['malformed'][0]:
        raise ValueError('opening_role_malformed_bar')
    scale = max(abs(float(x)) for x in (opening, high, low, close))
    tol = 8 * np.spacing(scale)
    relevant = (clocks >= 92500000) & (clocks <= int(bar_clock) * 1000)
    finite = np.isfinite(latest) & (latest > 0)
    if not np.isfinite(tol) or ((~finite) & relevant).any():
        raise ValueError('opening_role_malformed_quote')
    high_low_close_consistent = (low <= high and low - tol <= close <= high + tol)
    left = (int(bar_clock) - 100) * 1000
    right = int(bar_clock) * 1000
    window = latest[(clocks > left) & (clocks <= right)]
    auction_mask = (clocks >= 92500000) & (clocks <= 93000000)
    available = bool(auction_mask.any())
    unambiguous = False
    matches = False
    if available:
        candidate_time = clocks[auction_mask][-1]
        group = latest[clocks == candidate_time]
        unambiguous = bool((group == group[0]).all())
        matches = unambiguous and bool(abs(float(group[0]) - opening) <= tol)
    return {
        'strict_OHLC_violation': bool(flags['strict_order_violation'][0]),
        'beyond_frozen_float64_policy': bool(flags['beyond_roundoff'][0]),
        'HLC_consistent': bool(high_low_close_consistent),
        'opening_only_range_violation': bool(high_low_close_consistent and (opening < low - tol or opening > high + tol)),
        'auction_candidate_available': available,
        'auction_candidate_unambiguous': unambiguous,
        'opening_matches_auction_candidate': matches,
        'window_quote_rows': len(window),
        'close_present_in_window': bool(len(window) and (np.abs(window - close) <= tol).any()),
        'observed_quote_outside_minute_HL': bool(len(window) and ((window < low - tol) | (window > high + tol)).any()),
        'observed_window_extrema_equal_HL': bool(len(window) and abs(float(window.min()) - low) <= tol and abs(float(window.max()) - high) <= tol),
        'source_or_execution_admitted': False,
    }
