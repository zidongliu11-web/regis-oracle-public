"""Validate fixed current five-minute source bars without inferring fills."""
from datetime import datetime
from decimal import Decimal
from research.retained_daily_diagnostic import day, numeric

FIELDS = ['date', 'time', 'code', 'open', 'high', 'low', 'close', 'volume', 'amount', 'adjustflag']
CODE = 'sh.600000'
DATE = '2026-10-08'


def values(row):
    if row['code'] != CODE or day(row['date']) != DATE or row['adjustflag'] != '3':
        raise ValueError('fixed_minute_code_date_or_adjustment')
    v = {k: numeric(row[k], positive=True) for k in ('open', 'high', 'low', 'close')}
    if not v['low'] <= min(v['open'], v['close']) <= max(v['open'], v['close']) <= v['high']:
        raise ValueError('minute_OHLC_shape')
    for k in ('volume', 'amount'):
        v[k] = numeric(row[k])
        if v[k] < 0:
            raise ValueError('minute_negative_quantity')
    if v['volume'] != v['volume'].to_integral_value():
        raise ValueError('minute_fractional_shares')
    return v


def label(text):
    # Publisher format is YYYYMMDDHHMMSSsss. Endpoint meaning remains unknown.
    if not isinstance(text, str) or len(text) != 17 or not text.isascii() or not text.isdigit():
        raise ValueError('minute_timestamp_format')
    stamp = datetime.strptime(text[:14], '%Y%m%d%H%M%S')
    if stamp.date().isoformat() != DATE:
        raise ValueError('minute_timestamp_date')
    hhmm = text[8:12]
    if text[12:] != '00000' or stamp.minute % 5 or not ('0930' <= hhmm <= '1130' or '1300' <= hhmm <= '1500'):
        raise ValueError('minute_timestamp_session_grid')
    return text


def analyse(rows, daily):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 48:
        raise ValueError('minute_nonempty_cached_row_cap')
    if any(not isinstance(r, dict) or set(r) != set(FIELDS) for r in rows):
        raise ValueError('minute_field_shape')
    stamps = [label(r['time']) for r in rows]
    if len(stamps) != len(set(stamps)) or stamps != sorted(stamps):
        raise ValueError('minute_timestamp_duplicate_or_order')
    data = [values(r) for r in rows]
    d = values(daily)
    # Four decimal currency fields: 48 separately rounded intervals plus one
    # rounded daily amount have a theoretical 0.00245 CNY absolute error bound.
    checks = {
        'first_open': abs(data[0]['open'] - d['open']) <= Decimal('0.0001'),
        'last_close': abs(data[-1]['close'] - d['close']) <= Decimal('0.0001'),
        'maximum_high': abs(max(r['high'] for r in data) - d['high']) <= Decimal('0.0001'),
        'minimum_low': abs(min(r['low'] for r in data) - d['low']) <= Decimal('0.0001'),
        'volume_sum': sum(r['volume'] for r in data) == d['volume'],
        'amount_sum': abs(sum(r['amount'] for r in data) - d['amount']) <= Decimal('0.0025'),
    }
    return {
        'rows': len(rows), 'fixed_code_date_rawflag_matched': True,
        'unique_ascending_exchange_labels': True, 'OHLC_quantities_validated': True,
        'count_is_48': len(rows) == 48,
        'contains_0935_label': any(t[8:12] == '0935' for t in stamps),
        'contains_1455_label': any(t[8:12] == '1455' for t in stamps),
        'daily_agreement': checks, 'same_source_daily_agreement': all(checks.values()),
        'price_absolute_tolerance_CNY': '0.0001', 'amount_absolute_tolerance_CNY': '0.0025',
        'volume_absolute_tolerance_shares': 0,
        'time_label_endpoint_meaning_verified': False,
        'complete_intraday_population_verified': False,
        'historical_publication_PIT_verified': False,
        'quote_side_size_or_queue_available': False,
        'manual_latency_or_fill_verified': False,
        'model_admitted': False, 'orders_enabled': False,
    }
