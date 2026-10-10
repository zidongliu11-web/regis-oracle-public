"""Apply one independently transcribed original notice to a native action census.

This is a pinned manual source adapter, not OCR or a data/admission certificate.
The old census and its conflicts remain unchanged. No prices or economic runs.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

from research.m24_strategy_execution_chain import _physical
from factor_system.native_snapshot import _native_root, _no_links, _identity, _path_identity

ROOT = Path(__file__).resolve().parents[1]
PRIOR = ROOT / 'research/ETF_ACTION_CASH_COMPLETION_RESULT_20261002.json'
SOURCE = ROOT / 'research/ETF_2018_NOTICE_SOURCE_20261005.json'
PRIOR_SHA = 'b7a33137f7a57c6243cdec50f05447ca522d956f877681d823116d399ef18d21'
SOURCE_SHA = '1ae8a15e8e3d98130f145b48e61e0d7875494d01179cb3b9391e7a7332242a04'
PDF_SHA = 'df5a12bb20f45fe4850d4a33273d048d7a481ddf3f93050f1093231e232902a5'
EVENT = '510300.SH:2018-01-22'
EXPECTED_DATES = {
    'announcement_date': '2018-01-17', 'record_date': '2018-01-22',
    'ex_date': '2018-01-23', 'declared_cash_payment_date': '2018-01-26',
}


class NoticeOverlayError(ValueError):
    pass


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _pinned_json(path, expected):
    # Source/census are small tracked public-action documents, never market prices.
    with path.open('rb') as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024 or _hash(raw) != expected:
        raise NoticeOverlayError('notice_tracked_input_changed')
    return json.loads(raw)


def _apply_dates(prior, source):
    if (source.get('schema') != 'regis.etf.original_notice_dates.v1'
            or source.get('fund_code') != '510300'
            or source.get('fund_name') != '华泰柏瑞沪深300交易型开放式指数证券投资基金'
            or source.get('event_key') != EVENT
            or source.get('source_kind') != 'original_designated_newspaper_notice'
            or source.get('ex_date_role') != 'single_original_notice_secondary_market_schedule'
            or source.get('source_pdf', {}).get('sha256') != PDF_SHA
            or source.get('pdf_page') != 1):
        raise NoticeOverlayError('notice_identity_or_role_mismatch')
    if any(source.get(k) != v for k, v in EXPECTED_DATES.items()):
        raise NoticeOverlayError('notice_date_mismatch')
    limits = source.get('limits', {})
    if (limits.get('historical_available_at') is not None
            or limits.get('actual_personal_credit_at') is not None
            or any(limits.get(k) is not False for k in (
                'cash_amount_reverified_by_fixed_reviewer', 'all_off_exchange_dates_certified',
                'complete_action_history', 'price_source_qualified', 'model_admitted'))):
        raise NoticeOverlayError('notice_scope_expansion')
    rows = prior.get('events', [])
    keys = [r['event_key'] for r in rows]
    missing = sorted(r['event_key'] for r in rows if r.get('candidate_ex_date') is None)
    if (len(rows) != 14 or len(set(keys)) != 14 or missing != [EVENT]
            or prior.get('missing_ex_date_event_keys') != missing
            or prior.get('source_supported_secondary_ex_dates') != 13
            or prior.get('all_14_secondary_ex_dates_verified') is not False):
        raise NoticeOverlayError('notice_prior_census_mismatch')
    if any(prior.get(k) is not False for k in (
            'historical_vintage_admitted', 'other_action_history_complete',
            'actual_personal_cash_credit_proven', 'model_admitted',
            'full_L1', 'full_L2', 'full_L3', 'full_L4')):
        raise NoticeOverlayError('notice_prior_admission_expansion')
    output = deepcopy(prior)
    row = next(r for r in output['events'] if r['event_key'] == EVENT)
    if (row['issuer_table_record_date'] != source['record_date']
            or row['issuer_table_pay_or_reinvestment_date'] != source['declared_cash_payment_date']):
        raise NoticeOverlayError('notice_prior_date_conflict')
    row['candidate_ex_date'] = source['ex_date']
    row['candidate_announcement_date'] = source['announcement_date']
    row['ex_date_source_id'] = 'original_cs_20180117_510300_notice'
    row['source_quality_status'] = 'original_notice_dates_verified_no_price_or_PIT_admission'
    output['source_supported_secondary_ex_dates'] = sum(
        r.get('candidate_ex_date') is not None for r in output['events'])
    output['missing_ex_date_event_keys'] = sorted(
        r['event_key'] for r in output['events'] if r.get('candidate_ex_date') is None)
    output['all_14_secondary_ex_dates_verified'] = True
    output['original_2018_date_resolution'] = {
        'prior_census_sha256': PRIOR_SHA, 'source_metadata_sha256': SOURCE_SHA,
        'source_pdf_sha256': PDF_SHA, 'field_method': source['field_method'],
        'single_notice_ex_date_role': source['ex_date_role'],
        'old_interim_record_and_on_off_exchange_conflict_retained': True,
        'historical_publication_time_certified': False,
        'actual_personal_credit_certified': False,
        'full_action_or_price_admission': False,
    }
    return output


def build_verified_census(native_root):
    """Verify original native bytes/review and preserve a new immutable census."""
    base = _native_root(native_root)
    source = _pinned_json(SOURCE, SOURCE_SHA)
    prior = _pinned_json(PRIOR, PRIOR_SHA)
    # Reuse the existing same-descriptor/native-path integrity reader. Success
    # proves byte identity; the fixed review provides limited visual evidence.
    _physical(base, source['source_pdf'])
    if (base / source['source_pdf']['relative']).stat().st_size != source['source_bytes']:
        raise NoticeOverlayError('notice_pdf_size_mismatch')
    for crop in source['crops']:
        _physical(base, {k: crop[k] for k in ('relative', 'sha256')})
    review = _physical(base, source['fixed_review'], decode=True)
    if (review.get('fixed_agent') != '/root/fixed_reviewer_m24'
            or review.get('limited_action_source_passed') is not True
            or review.get('bound_pdf_sha256') != PDF_SHA
            or review.get('observed') != {
                'fund_code': '510300', 'announcement_date': '2018-01-17',
                'record_date': '2018-01-22', 'ex_date': '2018-01-23',
                'cash_payment_date': '2018-01-26'}
            or review.get('current_P0_P1_P2') != {'P0': 0, 'P1': 0, 'P2': 0}):
        raise NoticeOverlayError('notice_fixed_review_mismatch')
    output = _apply_dates(prior, source)
    raw = (json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
    name = 'verified_2018_action_census.json'
    dest = base / name
    if dest.exists():
        _no_links(base, Path(name))
        if dest.read_bytes() != raw:
            raise NoticeOverlayError('notice_output_already_differs')
        reused = True
    else:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        reused = False
    # Tracked source/census must still be the declared physical versions.
    _pinned_json(SOURCE, SOURCE_SHA); _pinned_json(PRIOR, PRIOR_SHA)
    _physical(base, source['source_pdf'])
    for crop in source['crops']:
        _physical(base, {k: crop[k] for k in ('relative', 'sha256')})
    _physical(base, source['fixed_review'], decode=True)
    _physical(base, {'relative': name, 'sha256': _hash(raw)}, decode=True)
    return {
        'state': 'original_2018_dates_applied_to_native_census',
        'census_rows': len(output['events']), 'supported_ex_dates': output['source_supported_secondary_ex_dates'],
        'remaining_ex_date_gaps': len(output['missing_ex_date_event_keys']),
        'native_census_relative': name, 'native_census_sha256': _hash(raw), 'reused': reused,
        'prior_census_unchanged': True, 'prior_conflicts_preserved': True,
        'new_economic_runs': 0, 'price_rows_read': 0, 'model_admitted': False,
        'full_ETF_admitted': False, 'historical_vintage_admitted': False,
    }


def compare_retained_dividend_dates():
    """Compare only <=14 real action-date rows; no price, cash or volume reads."""
    import sys
    sys.path.insert(0, str(ROOT / 'research'))
    import inspect_short_rq_full as reader
    from recover_rq_etf_history import safe_dataset
    receipt = _pinned_json(ROOT / 'research/RQ_ETF_HISTORY_RECOVERY_RECEIPT_20261002.json',
                           'cd752d56d1b38c200bac26c5dd6ecd01ad7c5c50d650361e6285f9d2062cca87')
    base = _native_root(Path(receipt['raw_directory']))
    member = next(x for x in receipt['members'] if x['name'] == 'dividends.h5')
    ref = {'relative': 'dividends.h5', 'sha256': member['sha256']}
    _physical(base, ref)
    h5py, np = reader.load_dependencies()
    path = base / 'dividends.h5'
    fields = ('book_closure_date', 'ex_dividend_date', 'payable_date')
    # Hash and HDF5 decode use the same descriptor. The retained archive is
    # source provenance, not historical vintage or a unit/admission proof.
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        if hashlib.file_digest(stream, 'sha256').hexdigest() != ref['sha256']:
            raise NoticeOverlayError('notice_retained_actions_changed')
        stream.seek(0)
        with h5py.File(stream, 'r', rdcc_nbytes=1024 * 1024) as file:
            ds = safe_dataset(file, '510300.XSHG', h5py, np)
            if not 1 <= ds.shape[0] <= 14:
                raise NoticeOverlayError('notice_retained_action_row_cap')
            for field in fields:
                if field not in (ds.dtype.names or ()) or ds.dtype.fields[field][0].kind not in 'iu':
                    raise NoticeOverlayError('notice_retained_date_dtype_invalid')
            dates = ds.fields(fields)[:]
        if (_identity(os.fstat(stream.fileno())) != _identity(before)
                or _path_identity(os.lstat(path)) != _path_identity(before)):
            raise NoticeOverlayError('notice_retained_actions_changed')
        stream.seek(0)
        if hashlib.file_digest(stream, 'sha256').hexdigest() != ref['sha256']:
            raise NoticeOverlayError('notice_retained_actions_changed')
    _physical(base, ref)
    normalized = [{key: int(r[key]) // 1000000 if int(r[key]) > 100000000 else int(r[key])
                   for key in fields} for r in dates]
    target = [r for r in normalized if r['book_closure_date'] == 20180122]
    if len(target) != 1:
        raise NoticeOverlayError('notice_retained_target_not_unique')
    matches = {
        'record_date_match': target[0]['book_closure_date'] == 20180122,
        'ex_date_match': target[0]['ex_dividend_date'] == 20180123,
        'declared_cash_payment_date_match': target[0]['payable_date'] == 20180126,
    }
    if not all(matches.values()):
        raise NoticeOverlayError('notice_retained_target_date_mismatch')
    return {
        'state': 'retained_real_dividend_dates_match_original_notice',
        'source_sha256': ref['sha256'], 'native_action_date_rows_read': len(normalized),
        'target_events_compared': 1, **matches,
        'cash_amount_reverified': False, 'price_rows_read': 0,
        'historical_vintage_admitted': False, 'model_admitted': False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--native-root', required=True)
    args = parser.parse_args(argv)
    try:
        result = build_verified_census(args.native_root)
        result['retained_action_date_comparison'] = compare_retained_dividend_dates()
    except Exception:
        # Keep filesystem paths and original exceptions off the external output.
        print(json.dumps({'state': 'source_integrity_or_scope_rejected', 'model_admitted': False}))
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
