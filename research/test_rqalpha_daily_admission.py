"""Adverse semantic boundaries, synthetic fixtures only; no market downloads."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile

from rqalpha_daily_adapter import (SourceAdmissionError, adapt_native_daily,
    load_native_sample, snapshot_visible_rows, to_m24_economic_bars, utc)

SHA = 'a'*64
KNOWN = '2026-10-02T02:40:38.146352+00:00'
fixture = [{'order_book_id':'600000.XSHG','trading_date':20260930,
            'open':10,'high':11,'low':9,'close':10,'volume':100,'total_turnover':1000}]
kwargs = dict(sample_sha256=SHA, snapshot_known_at=KNOWN,
              selection={'600000.XSHG':'stock'}, sessions=[20260930])
passed = []

def check(name, fn):
    fn()
    passed.append(name)

def reject(fn, code):
    try:
        fn()
    except SourceAdmissionError as e:
        assert str(e) == code, (str(e), code)
    else:
        raise AssertionError('rejection_missing')

def modified(field, value):
    rows = deepcopy(fixture)
    rows[0][field] = value
    return rows

bundle = adapt_native_daily(fixture, **kwargs)
def timestamps():
    assert not snapshot_visible_rows(bundle, decision_at='2026-09-30T15:00:00+08:00')
    assert not snapshot_visible_rows(bundle, decision_at=(utc(KNOWN)-timedelta(microseconds=1)).isoformat())
    assert len(snapshot_visible_rows(bundle, decision_at=KNOWN)) == 1
    assert len(snapshot_visible_rows(bundle, decision_at='2026-10-02T10:40:38.146352+08:00')) == 1
check('historical_pre_capture_boundary_and_timezone_equivalence', timestamps)

def no_laundering():
    b = deepcopy(bundle)
    b['rows'][0]['historical_available_at'] = '2026-09-30T15:00:00+08:00'
    b['rows'][0]['earliest_snapshot_use_at'] = '2026-09-30T15:00:00+08:00'
    assert not snapshot_visible_rows(b,decision_at='2026-09-30T16:00:00+08:00')
    b['authority']['economic_admitted'] = True
    b['rows'][0]['unit_state'] = 'caller_claimed_verified'
    reject(lambda:to_m24_economic_bars(b), 'RQ_native_source_not_admitted_to_M24_economic_replay')
check('old_row_timestamp_and_editable_flags_cannot_enable_replay', no_laundering)

for name, rows, expected in [
 ('duplicate_keys',fixture+fixture,'duplicate_instrument_date'),
 ('missing_keys',[],'sample_row_bound'),
 ('extra_field',modified('available_at',KNOWN),'native_fields_mismatch'),
 ('outside_selection',modified('order_book_id','000001.XSHE'),'instrument_outside_selection'),
 ('outside_sessions',modified('trading_date',20260929),'date_outside_frozen_sessions'),
 ('datetime_as_date',modified('trading_date',20260930000000),'daily_date_integer_required'),
 ('boolean_date',modified('trading_date',True),'daily_date_integer_required'),
 ('invalid_day',modified('trading_date',20260931),'invalid_daily_date'),
 ('nan_quote',modified('open',float('nan')),'nonfinite_or_unbounded_native_number'),
 ('boolean_quote',modified('close',True),'native_numeric_required'),
 ('string_quote',modified('close','10'),'native_numeric_required'),
 ('bad_OHLC',modified('close',12),'native_OHLC_invalid'),
 ('negative_volume',modified('volume',-1),'negative_native_activity'),
 ('zero_activity_mismatch',modified('volume',0),'inconsistent_zero_activity')]:
    check(name, lambda rows=rows,expected=expected:reject(lambda:adapt_native_daily(rows,**kwargs),expected))

check('capture_before_completed_close', lambda:reject(lambda:adapt_native_daily(fixture,**{**kwargs,'snapshot_known_at':'2026-09-30T14:59:00+08:00'}),'completed_daily_bar_captured_before_close'))
check('timezone_required',lambda:reject(lambda:snapshot_visible_rows(bundle,decision_at='2026-10-02T02:40:38'),'timezone_required'))
check('SHA_required',lambda:reject(lambda:adapt_native_daily(fixture,**{**kwargs,'sample_sha256':'not-sha'}),'invalid_sample_SHA'))
check('complete_expected_key_set',lambda:reject(lambda:adapt_native_daily(fixture,**{**kwargs,'sessions':[20260929,20260930]}),'frozen_key_coverage_incomplete'))

def zero_activity():
    rows = modified('volume',0)
    rows[0]['total_turnover']=0
    b=adapt_native_daily(rows,**kwargs)
    assert b['rows'][0]['same_base_unit_VWAP_hypothesis_consistent'] is None
    assert b['rows'][0]['amount_yuan'] is None
check('zero_activity_no_division_or_unit_guess',zero_activity)

def inconsistent_ratio():
    b=adapt_native_daily(modified('total_turnover',100000),**kwargs)
    assert b['rows'][0]['same_base_unit_VWAP_hypothesis_consistent'] is False
    assert b['rows'][0]['unit_state']=='provider_native_unverified'
    assert b['rows'][0]['volume_units'] is None
    reject(lambda:to_m24_economic_bars(b),'RQ_native_source_not_admitted_to_M24_economic_replay')
check('ratio_mismatch_does_not_fit_unit_scaling',inconsistent_ratio)

def tamper():
    b=deepcopy(bundle)
    b['snapshot_known_at']='2026-09-30T15:00:00+08:00'
    reject(lambda:snapshot_visible_rows(b,decision_at=KNOWN),'inconsistent_snapshot_identity')
check('root_clock_tamper_inconsistent_with_rows',tamper)

def json_boundaries():
    base=(Path(os.environ['LOCALAPPDATA'])/'RegisOracle-local-research').resolve()
    assert 'onedrive' not in str(base).lower()
    temp=tempfile.TemporaryDirectory(dir=base,prefix='rq-semantic-test-')
    actual=Path(temp.name).resolve()
    # Verify target before TemporaryDirectory's recursive cleanup.
    assert actual.is_relative_to(base) and actual != base
    try:
        p=actual/'synthetic.json'
        cases=[(b'{"a":1,"a":2}', 'duplicate_JSON_key'),
               (b'[NaN]','nonfinite_JSON'),(b'{}','sample_array_required')]
        for raw,code in cases:
            p.write_bytes(raw)
            reject(lambda:load_native_sample(p,expected_sha256=hashlib.sha256(raw).hexdigest(),max_bytes=65536),code)
        raw=json.dumps(fixture).encode()
        p.write_bytes(raw)
        reject(lambda:load_native_sample(p,expected_sha256='b'*64,max_bytes=65536),'sample_SHA_mismatch')
        reject(lambda:load_native_sample(p,expected_sha256=SHA,max_bytes=10),'sample_byte_bound')
    finally:
        temp.cleanup()
check('duplicate_JSON_nonfinite_SHA_and_byte_limits',json_boundaries)

print(json.dumps({'synthetic_adverse_checks_passed':len(passed),'checks':passed,
                  'real_quote_rows_read':0,'network_requests':0,'economic_runs':0}))
