"""Two registered daily arithmetic inputs, with independent failure masks.

Reproduce existing formulas only. Values are descriptive inputs, never source
qualification, alpha tests, selected stocks, signals or admission decisions.
"""
from __future__ import annotations
import numpy as np

IDS = ('close_acceptance', 'pullback_volume_3_20')
REASONS = ('valid', 'insufficient_history', 'calendar_gap', 'nonpositive_amount',
           'numeric_range', 'zero_range', 'invalid_price', 'reported_halt')
DTYPE = np.dtype([('date', '<u4'), (IDS[0], '<f8'), (IDS[1], '<f8'),
                  ('close_reason', 'u1'), ('amount_reason', 'u1'),
                  ('reported_halt', '?'), ('halt_dataset_present', '?')])


def calculate_arrays(high, low, close, amount, sessions, *, reported_halt=None):
    """O(23*N), bounded per instrument; amount windows exclude current day.

    Sessions are indices in the supplied source calendar. No sorting, filling,
    adjustment or skipping gaps. A reported halt blocks the affected inputs;
    missing event datasets still require a separate unknown-state annotation.
    Common price scale and constant amount scale are engineering assumptions.
    """
    fields = [np.asarray(x) for x in (high, low, close, amount, sessions)]
    if (any(x.ndim != 1 or x.dtype.kind not in 'iuf' for x in fields)
            or len({len(x) for x in fields}) != 1 or len(fields[0]) > 30000):
        raise ValueError('daily_input_arrays_invalid')
    if (fields[-1].dtype.kind not in 'iu' or np.any(fields[-1] < 0)
            or np.any(fields[-1] > 30000)
            or np.any(np.diff(fields[-1].astype(np.int64)) <= 0)):
        raise ValueError('daily_input_sessions_invalid')
    high, low, close, amount = [x.astype(np.float64, copy=False) for x in fields[:4]]
    sessions = fields[-1].astype(np.int64, copy=False)
    n = len(close)
    halted = np.zeros(n, dtype=bool) if reported_halt is None else np.asarray(reported_halt)
    if halted.shape != (n,) or halted.dtype.kind != 'b':
        raise ValueError('daily_input_halt_mask_invalid')
    location, pullback = np.full(n, np.nan), np.full(n, np.nan)
    price_reason = np.full(n, 6, dtype=np.uint8)
    amount_reason = np.full(n, 1, dtype=np.uint8)
    valid = (np.isfinite(high) & np.isfinite(low) & np.isfinite(close)
             & (low > 0) & (high >= close) & (close >= low))
    price_reason[valid & (high == low)] = 5
    price_reason[halted] = 7
    valid &= (high > low) & ~halted
    # Positive ordered prices make these subtractions finite without rescaling
    # or rounding away a representable narrow range.
    location[valid] = (close[valid] - low[valid]) / (high[valid] - low[valid])
    price_reason[valid] = 0
    if n >= 24:
        window = np.lib.stride_tricks.sliding_window_view(amount, 23)[:-1]
        contiguous = sessions[23:] - sessions[:-23] == 23
        halted_window = np.lib.stride_tricks.sliding_window_view(halted, 24).any(axis=1)
        positive = np.isfinite(window).all(axis=1) & (window > 0).all(axis=1)
        reason = np.full(n - 23, 3, dtype=np.uint8)
        reason[~contiguous] = 2
        reason[contiguous & halted_window] = 7
        good = contiguous & ~halted_window & positive
        selected = window[good]
        if len(selected):
            # Scaling per fixed window prevents overflow of finite sums.
            scaled = selected / selected.max(axis=1)[:, None]
            denominator = scaled[:, :20].mean(axis=1)
            numerator = scaled[:, 20:].mean(axis=1)
            with np.errstate(over='ignore', divide='ignore', invalid='ignore', under='ignore'):
                values = -numerator / denominator
            finite = np.isfinite(values) & (denominator > 0) & (numerator > 0) & (values < 0)
            indices = np.flatnonzero(good)
            pullback[indices[finite] + 23] = values[finite]
            reason[indices] = np.where(finite, 0, 4)
        amount_reason[23:] = reason
    return location, pullback, price_reason, amount_reason


def calculate_rows(rows, calendar):
    """Keep the existing interactive preview response schema."""
    sessions = {int(day): i for i, day in enumerate(sorted(calendar))}
    days = [int(r['time'].replace('-', '')) for r in rows]
    if days != sorted(set(days)) or any(day not in sessions for day in days):
        raise ValueError('daily_input_dates_invalid')
    h, l, c, a = [[r[k] for r in rows] for k in ('high', 'low', 'close', 'total_turnover')]
    values = calculate_arrays(h, l, c, a, np.array([sessions[d] for d in days], dtype=np.int64))
    result = []
    for i, row in enumerate(rows):
        reasons = [int(values[2][i]), int(values[3][i])]
        result.append({'date': row['time'],
                       **{key: float(values[j][i]) if reasons[j] == 0 else None for j, key in enumerate(IDS)},
                       'missing': {key: None if reasons[j] == 0 else REASONS[reasons[j]]
                                   for j, key in enumerate(IDS)}})
    return result
