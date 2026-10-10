"""RQ bundle native daily records. This adapter cannot authorize an economic run.

Trade date, exact-snapshot knowledge and historical publication are separate.
Provider values remain native: neither a field name nor a plausible VWAP proves
currency/quantity units, listing-instance identity or historical availability.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

SH = ZoneInfo('Asia/Shanghai')
FIELDS = frozenset({'order_book_id', 'trading_date', 'open', 'high', 'low',
                    'close', 'volume', 'total_turnover'})
VALUES = ('open', 'high', 'low', 'close', 'volume', 'total_turnover')
SHA = re.compile(r'[0-9a-f]{64}\Z')


class SourceAdmissionError(ValueError):
    """Safe error code only; never embeds a quote or source record."""


def fail(code):
    raise SourceAdmissionError(code)


def utc(value):
    if not isinstance(value, str) or len(value) > 64:
        fail('timestamp_required')
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        fail('invalid_timestamp')
    if dt.tzinfo is None or dt.utcoffset() is None:
        fail('timezone_required')
    return dt.astimezone(timezone.utc)


def day(value):
    # Daily date only: no implicit epoch/timestamp truncation or time inference.
    if type(value) is not int or not re.fullmatch(r'\d{8}', str(value)):
        fail('daily_date_integer_required')
    try:
        return datetime.strptime(str(value), '%Y%m%d').date()
    except ValueError:
        fail('invalid_daily_date')


def decimal(value):
    if type(value) not in (int, float, Decimal):
        fail('native_numeric_required')
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError):
        fail('invalid_native_number')
    if not n.is_finite() or (n and n.adjusted() > 30) or len(n.as_tuple().digits) > 128:
        fail('nonfinite_or_unbounded_native_number')
    return n


def _object(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            fail('duplicate_JSON_key')
        obj[k] = v
    return obj


def load_native_sample(path: Path, *, expected_sha256: str, max_bytes: int):
    if not isinstance(expected_sha256, str) or not SHA.fullmatch(expected_sha256):
        fail('invalid_sample_SHA')
    if type(max_bytes) is not int or not 0 < max_bytes <= 65536:
        fail('invalid_byte_bound')
    # Read is bounded even if the file grows after stat().
    if path.stat().st_size > max_bytes:
        fail('sample_byte_bound')
    with path.open('rb') as f:
        raw = f.read(max_bytes + 1)
    if len(raw) > max_bytes:
        fail('sample_byte_bound')
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        fail('sample_SHA_mismatch')
    def nonfinite(_):
        fail('nonfinite_JSON')
    try:
        rows = json.loads(raw, parse_float=Decimal, object_pairs_hook=_object,
                          parse_constant=nonfinite)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        fail('invalid_sample_JSON')
    if not isinstance(rows, list):
        fail('sample_array_required')
    return rows


def adapt_native_daily(records, *, sample_sha256: str, snapshot_known_at: str,
                       selection: dict, sessions: list, max_rows: int = 50):
    if not isinstance(sample_sha256, str) or not SHA.fullmatch(sample_sha256):
        fail('invalid_sample_SHA')
    known = utc(snapshot_known_at)
    if (type(max_rows) is not int or not 0 < max_rows <= 50 or
            not isinstance(records, list) or not 0 < len(records) <= max_rows):
        fail('sample_row_bound')
    if (not isinstance(selection, dict) or not selection or
            any(not isinstance(k, str) or not re.fullmatch(r'\d{6}\.(XSHG|XSHE)', k)
                or v not in {'stock', 'etf'} for k, v in selection.items())):
        fail('selection_invalid')
    if not isinstance(sessions, list) or not sessions:
        fail('frozen_session_list_required')
    dates = [day(d) for d in sessions]
    if dates != sorted(set(dates)):
        fail('frozen_sessions_duplicate_or_unsorted')
    expected = {(k, d) for k in selection for d in dates}
    if len(expected) > max_rows:
        fail('expected_row_bound')
    rows, seen = [], set()
    for item in records:
        if not isinstance(item, dict) or set(item) != FIELDS:
            fail('native_fields_mismatch')
        code = item['order_book_id']
        if not isinstance(code, str) or code not in selection:
            fail('instrument_outside_selection')
        trade_day = day(item['trading_date'])
        key = (code, trade_day)
        if key in seen:
            fail('duplicate_instrument_date')
        if key not in expected:
            fail('date_outside_frozen_sessions')
        seen.add(key)
        end = datetime.combine(trade_day, time(15), SH).astimezone(timezone.utc)
        if known < end:
            fail('completed_daily_bar_captured_before_close')
        v = {field: decimal(item[field]) for field in VALUES}
        if not (0 < v['low'] <= min(v['open'], v['close']) <=
                max(v['open'], v['close']) <= v['high']):
            fail('native_OHLC_invalid')
        if v['volume'] < 0 or v['total_turnover'] < 0:
            fail('negative_native_activity')
        if (v['volume'] == 0) != (v['total_turnover'] == 0):
            fail('inconsistent_zero_activity')
        # Fixed same-base-unit hypothesis only; no unit selection or rescaling.
        hypothesis = None if not v['volume'] else (
            v['low'] * v['volume'] <= v['total_turnover'] <= v['high'] * v['volume'])
        rows.append({
            'provider_key': code, 'canonical_code_key': code.replace('.XSHG', '.SH').replace('.XSHE', '.SZ'),
            'asset_class_selection_claim': selection[code],
            'permanent_instrument_id': None, 'listing_instance_evidence': None,
            'trade_date': trade_day.isoformat(), 'frequency': '1d',
            'observation_end_at': end.isoformat(), 'snapshot_known_at': known.isoformat(),
            'historical_available_at': None, 'earliest_snapshot_use_at': known.isoformat(),
            'source_sha256': sample_sha256,
            'native': {field: str(v[field]) for field in VALUES},
            'unit_state': 'provider_native_unverified', 'price_basis': 'none_document_supported_not_snapshot_certified',
            'price_yuan': None, 'volume_units': None, 'amount_yuan': None,
            'pre_close': None, 'pct_chg': None,
            'same_base_unit_VWAP_hypothesis_consistent': hypothesis,
            'historical_PIT_admitted': False, 'economic_admitted': False})
    if seen != expected:
        fail('frozen_key_coverage_incomplete')
    return {'schema': 'regis.rq_daily_native.v1',
            'rows': sorted(rows, key=lambda r: (r['trade_date'], r['provider_key'])),
            'snapshot_known_at': known.isoformat(), 'source_sha256': sample_sha256,
            'authority': {'local_format_research_only': True, 'historical_PIT_admitted': False,
                          'economic_admitted': False, 'model_admitted': False}}


def snapshot_visible_rows(bundle, *, decision_at: str):
    """Temporal diagnostic only. Visible here never implies economic admission."""
    decision = utc(decision_at)
    known = utc(bundle['snapshot_known_at'])
    # One bundle-wide snapshot floor prevents caller-added old per-row dates
    # from laundering a newly downloaded vintage into a historical decision.
    if decision < known:
        return []
    if any(utc(r['snapshot_known_at']) != known or
           r['source_sha256'] != bundle['source_sha256'] for r in bundle['rows']):
        fail('inconsistent_snapshot_identity')
    return [r for r in bundle['rows']
            if decision >= max(known, utc(r['earliest_snapshot_use_at']), utc(r['observation_end_at']))]


def to_m24_economic_bars(_bundle):
    # No editable status flag or claimed timestamp can authorize this source.
    # A future approved adapter must separately bind units, stable identity,
    # actions, historical vintage and execution-role evidence.
    fail('RQ_native_source_not_admitted_to_M24_economic_replay')
