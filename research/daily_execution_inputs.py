"""Versioned native daily inputs, distinct from permission to replay a portfolio.

Rows remain in process/native storage. No network, persistence, signal selection,
state imputation, price adjustment, unit conversion or economic experiment.
The retained source cannot yet supply a qualified cashbook callback.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import math
import re

from mom_trading.local_research_history import (
    BOUNDARY, HistoryReader, _date, _require, _safe_dataset, _signature, parameters,
)
from research.inspect_short_rq_full import validate_bar_dtype

SCHEMA = 'regis.daily_execution_inputs.v1'
EXTRA_FIELDS = ('prev_close', 'limit_up', 'limit_down')
PRICE_FIELDS = ('open', 'high', 'low', 'close', *EXTRA_FIELDS)
STATE_FIELDS = ('st', 'halted', 'outside_lifetime')
UNRESOLVED = (
    'historical_known_at_not_certified', 'units_and_price_basis_not_certified',
    'state_and_lifetime_coverage_not_certified', 'full_corporate_actions_not_certified',
    'historical_limit_regime_not_certified', 'execution_and_manual_delay_not_certified',
)


class ExecutionInputUnavailable(ValueError):
    """Typed dependency failure, never a cashbook no-fill or stale-price fallback."""
    def __init__(self, reasons):
        self.reasons = tuple(reasons)
        super().__init__('execution_input_unavailable')


def number(value, *, zero=False):
    # Reject bool, NaN, Infinity, implicit objects and excessive decimal tokens.
    if type(value) not in (int, float, str, Decimal):
        raise ExecutionInputUnavailable(('invalid_numeric_type',))
    token = str(value)
    if len(token) > 64 or not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d{1,3})?', token):
        raise ExecutionInputUnavailable(('invalid_numeric_value',))
    try:
        out = Decimal(token)
        valid = out.is_finite() and (out >= 0 if zero else out > 0) and abs(out.adjusted()) <= 100
    except InvalidOperation:
        valid = False
    if not valid:
        raise ExecutionInputUnavailable(('invalid_numeric_value',))
    return out


def validate_cashbook_bar(bar):
    """Structural validation only; callers still own rights/PIT/action evidence.

    State booleans must already be established by the caller's evidence. In
    particular, E114's `not_reported` cannot be converted into False here.
    prev_close is required for lineage even though the old engine doesn't use it.
    """
    if not isinstance(bar, dict) or not all(type(bar.get(k)) is bool for k in STATE_FIELDS):
        raise ExecutionInputUnavailable(('state_unknown_or_not_boolean',))
    values = {k: number(bar.get(k), zero=k in ('volume', 'total_turnover'))
              for k in (*PRICE_FIELDS, 'volume', 'total_turnover')}
    if not (values['limit_down'] < values['limit_up']
            and values['limit_down'] <= values['low'] <= min(values['open'], values['close'])
            <= max(values['open'], values['close']) <= values['high'] <= values['limit_up']):
        raise ExecutionInputUnavailable(('inconsistent_ohlc_or_limits',))
    return {**{k: str(v) for k, v in values.items()}, **{k: bar[k] for k in STATE_FIELDS}}


def callback_key(key, day):
    if (not isinstance(key, str)
            or not re.fullmatch(r'(?:000|001|002|003|300|301)\d{3}\.XSHE|(?:600|601|603|605)\d{3}\.XSHG', key)):
        raise ExecutionInputUnavailable(('invalid_callback_identity',))
    try:
        _date(HistoryReader._iso(day))
    except (TypeError, ValueError):
        raise ExecutionInputUnavailable(('invalid_callback_date',)) from None
    return key, day


class StrictCashbookCallback:
    """Low-level callback for already established bars; not a data admission gate.

    Only synthetic fixtures use this interface in this revision. The captured
    reader deliberately exposes no override to manufacture qualified states.
    Every missing row raises, including after entry; it must not return None.
    """
    def __init__(self, records):
        if not isinstance(records, dict) or len(records) > 250:
            raise ExecutionInputUnavailable(('invalid_callback_records',))
        self._bars = {}
        for key, bar in records.items():
            if not isinstance(key, tuple) or len(key) != 2:
                raise ExecutionInputUnavailable(('invalid_callback_identity',))
            key = callback_key(*key)
            self._bars[key] = validate_cashbook_bar(bar)

    def __call__(self, key, day):
        key, day = callback_key(key, day)
        try:
            return dict(self._bars[(key, day)])
        except KeyError:
            raise ExecutionInputUnavailable(('missing_callback_row',)) from None


class DailyExecutionInputs:
    def __init__(self, stock, context=None):
        _require(stock is not None and stock.instrument == 'stock')
        self.stock, self.context = stock, context

    def _check(self):
        s = self.stock
        _require(not s.path.is_symlink() and not s.calendar_path.is_symlink()
                 and _signature(s.path) == s.signature and _signature(s.calendar_path) == s.calendar_signature)

    def read(self, code, start, end, limit):
        code, start, end, limit = parameters({'code': code, 'start': HistoryReader._iso(start),
                                             'end': HistoryReader._iso(end), 'limit': str(limit)})
        _require(limit <= 250)
        self._check()
        base = self.stock.read(code, start, end, limit)
        days = [_date(row['time']) for row in base['rows']]
        h5, np = self.stock.h5py, self.stock.np
        key = code + ('.XSHG' if code.startswith('6') else '.XSHE')
        with h5.File(self.stock.path, 'r', rdcc_nbytes=1048576) as file:
            ds = _safe_dataset(file, key, h5, np)
            present = [f for f in EXTRA_FIELDS if f in (ds.dtype.names or ())]
            validate_bar_dtype(ds.dtype, ['datetime', *present])
            # The base reader validates every timestamp/order/calendar member.
            # Read the selected extra fields only; no copying the full OHLC file.
            times = ds.fields('datetime')[:]
            dates = np.where(times > 100000000, times // 1000000, times).astype(np.int64)
            indices = np.searchsorted(dates, days)
            _require(all(i < len(dates) and int(dates[i]) == day for i, day in zip(indices, days)))
            extra = ds.fields(present)[int(indices[0]):int(indices[-1])+1] if days and present else []
            rows = []
            for j, row in enumerate(base['rows']):
                values, states = {}, {}
                for field in EXTRA_FIELDS:
                    value = float(extra[j][field]) if field in present else None
                    if value is None:
                        state = 'missing_field'
                    elif not math.isfinite(value):
                        state, value = 'nonfinite', None
                    elif value <= 0:
                        state = 'unsupported_nonpositive'
                    else:
                        state = 'positive_source_value'
                    values[field], states[field] = value, state
                band = 'unknown'
                if all(states[f] == 'positive_source_value' for f in ('limit_down', 'limit_up')):
                    band = ('consistent_source_band' if values['limit_down'] < values['limit_up']
                            and values['limit_down'] <= row['low'] <= row['high'] <= values['limit_up']
                            else 'inconsistent_source_band')
                rows.append({**row, **values, 'field_status': states, 'limit_band_status': band,
                             'execution_eligible': False})
        contexts, context_meta = self.context.read(code, days) if self.context else ({}, {'status': 'unavailable'})
        for row, day in zip(rows, days):
            row['context'] = deepcopy(contexts.get(day))
        self._check()
        return {**base, 'schema': SCHEMA, 'rows': rows, 'context_source': context_meta,
                'execution_eligible': False, 'callback_available': False,
                'unresolved_dependencies': list(UNRESOLVED), 'extra_fields': list(EXTRA_FIELDS),
                'field_semantics': 'Source values only; no inferred limits, unit conversion or yesterday-close substitution.',
                'volume_semantics': 'Positive-value condition only; no capacity or fill probability.', **BOUNDARY}

    def cashbook_callback(self):
        # Intentionally no caller flags/overrides: this source version has no
        # evidence adapter establishing the missing dependencies listed above.
        raise ExecutionInputUnavailable(UNRESOLVED)
