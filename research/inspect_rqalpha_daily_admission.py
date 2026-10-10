"""Offline real-sample semantic preflight. Never prints or commits quote values."""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

from rqalpha_daily_adapter import (SourceAdmissionError, adapt_native_daily,
                                  load_native_sample, snapshot_visible_rows,
                                  to_m24_economic_bars, utc)

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT/'research/RQ_DAILY_SEMANTICS_PROTOCOL_20261002.json'
FINDINGS = ROOT/'research/RQ_DAILY_SOURCE_FINDINGS_20261002.json'
RESULT = ROOT/'research/RQ_DAILY_ADMISSION_RESULT_20261002.json'

def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def read(p):
    return json.loads(p.read_text(encoding='utf-8'))

def inspect():
    spec, findings = read(PROTOCOL), read(FINDINGS)
    receipt = read(ROOT/'research/SHORT_RQ_FULL_RECEIPT_20261001.json')
    pointer = read(ROOT/'.codex-local/short_rq_sample_20261001/manifest.json')
    path = Path(pointer['sample_path']).resolve()
    allowed = (Path(os.environ['LOCALAPPDATA'])/'RegisOracle-local-research/short-rq-20261001/sample.json').resolve()
    if path != allowed or path.is_relative_to(ROOT) or 'onedrive' in str(path).lower():
        raise SourceAdmissionError('external_sample_custody_invalid')
    if (pointer['sha256'] != spec['sample_sha256'] or receipt['sample_sha256'] != spec['sample_sha256']
            or pointer['rows'] != spec['sample_rows'] or receipt['sample_rows'] != spec['sample_rows']):
        raise SourceAdmissionError('capture_manifest_identity_mismatch')
    for p, expected in spec['frozen_files_sha256'].items():
        if digest(ROOT/p) != expected:
            raise SourceAdmissionError('original_frozen_input_changed')
    for source in findings['sources']:
        if digest(ROOT/source['body_path']) != source['sha256']:
            raise SourceAdmissionError('official_source_body_changed')
    summaries = receipt['sample_summaries']
    sessions = summaries[0]['dates']
    if any(x['dates'] != sessions for x in summaries):
        raise SourceAdmissionError('capture_session_alignment_invalid')
    if set(x['order_book_id'] for x in summaries) != set(spec['selection']):
        raise SourceAdmissionError('capture_selection_invalid')
    if sessions[0] != 20260901 or sessions[-1] != 20260930:
        raise SourceAdmissionError('capture_window_invalid')
    bundle = adapt_native_daily(load_native_sample(path, expected_sha256=spec['sample_sha256'],
          max_bytes=spec['maximum_sample_bytes']), sample_sha256=spec['sample_sha256'],
          snapshot_known_at=receipt['ended_utc'], selection=spec['selection'], sessions=sessions,
          max_rows=spec['maximum_rows'])
    if len(bundle['rows']) != spec['sample_rows']:
        raise SourceAdmissionError('actual_row_count_mismatch')
    # Deterministic local output, outside workspace/OneDrive. Idempotent same bytes.
    out = allowed.parent/'normalized-native-v1.json'
    payload = (json.dumps(bundle, ensure_ascii=True, sort_keys=True, allow_nan=False)+'\n').encode('utf-8')
    if out.exists():
        if out.read_bytes() != payload:
            raise SourceAdmissionError('normalized_output_conflict')
    else:
        with out.open('xb') as f:
            f.write(payload)
    known = utc(receipt['ended_utc'])
    before = snapshot_visible_rows(bundle, decision_at=(known-timedelta(microseconds=1)).isoformat())
    at = snapshot_visible_rows(bundle, decision_at=known.isoformat())
    historical = snapshot_visible_rows(bundle, decision_at='2026-09-30T15:00:00+08:00')
    blocked = False
    try:
        to_m24_economic_bars(bundle)
    except SourceAdmissionError:
        blocked = True
    if before or historical or len(at) != spec['sample_rows'] or not blocked:
        raise SourceAdmissionError('actual_admission_boundary_failure')
    counts = {}
    for code in spec['selection']:
        rows = [r for r in bundle['rows'] if r['provider_key'] == code]
        flags = [r['same_base_unit_VWAP_hypothesis_consistent'] for r in rows]
        counts[code] = {'rows': len(rows), 'hypothesis_consistent': sum(x is True for x in flags),
                        'hypothesis_inconsistent': sum(x is False for x in flags),
                        'hypothesis_undefined': sum(x is None for x in flags)}
    return {'schema': 'regis.rq_daily_admission_result.v1',
        'sample_sha256': spec['sample_sha256'], 'actual_rows_adapted': len(bundle['rows']),
        'normalized_local_sha256': hashlib.sha256(payload).hexdigest(),
        'normalized_bytes': len(payload), 'normalized_outside_OneDrive_workspace': True,
        'normalized_row_values_to_AI_Git': False, 'new_quote_requests': 0,
        'snapshot_known_at': known.isoformat(), 'historical_visible_rows': len(historical),
        'visible_one_microsecond_before_capture': len(before), 'snapshot_visible_at_capture': len(at),
        'visibility_is_economic_admission': False, 'M24_economic_accessor_rejected': blocked,
        'per_instrument_native_hypothesis': counts, 'unit_conversion_performed': False,
        'unit_hypothesis_is_unit_proof': False,
        'missing_fields': {'canonical_yuan_quantity': len(bundle['rows']),
          'historical_available_at': len(bundle['rows']), 'permanent_identity_binding': len(bundle['rows']),
          'pre_close_and_pct_chg': len(bundle['rows'])},
        'sources_sha256': digest(FINDINGS), 'protocol_sha256': digest(PROTOCOL),
        'implementation_sha256': digest(ROOT/'research/rqalpha_daily_adapter.py'),
        'runner_sha256': digest(Path(__file__)),
        'blockers': ['unit_proof_and_snapshot_generation_binding_unverified',
          'listing_instance_and_historical_status_binding_missing',
          'historical_vintage_publication_missing_current_snapshot_cannot_be_backdated',
          'actions_all_A_denominator_market_state_and_real_minute_execution_roles_missing'],
        'authority': bundle['authority'], 'frozen_five_unchanged': True}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--write-summary', action='store_true')
    args = parser.parse_args()
    try:
        result = inspect()
        if args.write_summary:
            RESULT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
        print(json.dumps(result,ensure_ascii=True))
        return 0
    except SourceAdmissionError as exc:
        print(json.dumps({'status':'rejected','safe_error_code':str(exc),'raw_values_printed':False}))
        return 1
    except Exception as exc:
        # Raw JSON decode errors and filesystem exceptions may disclose data/path.
        print(json.dumps({'status':'failed','error_type':type(exc).__name__,'raw_values_printed':False}))
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
