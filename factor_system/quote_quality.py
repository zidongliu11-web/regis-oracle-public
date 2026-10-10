"""Vectorized OHLC validity without altering source prices.

Exact ordering and floating representation tolerance are reported separately.
This only checks numeric structure. It grants no units, data rights, publication
timing, executable volume, source qualification or strategy admission.
"""
from __future__ import annotations

import numpy as np


def ohlc_quality(opening, high, low, close, *, max_ulps=8):
    """Return native masks; the caller must keep numerical arrays off AI/Git.

    The fixed eight-ULP bound permits floating representation differences,
    not a price tick, fee allowance or economic tolerance. Raw inputs remain
    unchanged. A larger discrepancy or malformed price remains unusable.
    """
    if max_ulps != 8:
        raise ValueError('ohlc_roundoff_policy_not_frozen')
    arrays = [np.asarray(x) for x in (opening, high, low, close)]
    if any(x.ndim != 1 or x.dtype != np.dtype('float64') for x in arrays):
        raise ValueError('ohlc_requires_scalar_float64_arrays')
    if len({x.shape for x in arrays}) != 1:
        raise ValueError('ohlc_shapes_differ')
    opening, high, low, close = arrays
    finite = np.logical_and.reduce([np.isfinite(x) for x in arrays])
    positive = np.logical_and.reduce([x > 0 for x in arrays])
    with np.errstate(invalid='ignore', over='ignore'):
        gap = np.maximum.reduce([low - np.minimum(opening, close),
                                 np.maximum(opening, close) - high,
                                 np.zeros(len(opening), dtype='float64')])
        scale = np.maximum.reduce([np.abs(x) for x in arrays])
        tolerance = max_ulps * np.spacing(scale)
    well_formed = finite & positive & np.isfinite(gap) & np.isfinite(tolerance)
    strict_bad = well_formed & (gap > 0)
    beyond_roundoff = well_formed & (gap > tolerance)
    return {'malformed': ~well_formed, 'strict_order_violation': strict_bad,
            'roundoff_only': strict_bad & ~beyond_roundoff,
            'beyond_roundoff': beyond_roundoff,
            'usable_numeric_structure': well_formed & ~beyond_roundoff}
