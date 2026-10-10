"""Offline identity-key join against the actual 42-row snapshot; no price output."""
from __future__ import annotations
import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
from security_identity_evidence import (IdentityEvidenceError, digest, require,
    strict_utc, verified_catalog, join_identity_keys, require_historical_or_economic_identity)

ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT/'research/IDENTITY_CHECK_RESULT_20261002.json'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def inspect():
    protocol_path = ROOT/'research/IDENTITY_SOURCE_PROTOCOL_20261002.json'
    receipt_path = ROOT/'research/IDENTITY_SOURCE_RECEIPT_20261002.json'
    supplement_path = ROOT/'research/IDENTITY_STOCK_SOURCE_PROTOCOL_20261002.json'
    spec, first, extra = read(protocol_path), read(receipt_path), read(supplement_path)
    stock = read(ROOT/'research/IDENTITY_STOCK_SOURCE_RECEIPT_20261002.json')
    final_path = ROOT/'research/IDENTITY_CNINFO_SOURCE_PROTOCOL_20261002.json'
    final = read(final_path)
    cninfo_path = ROOT/'research/IDENTITY_CNINFO_SOURCE_RECEIPT_20261002.json'
    cninfo = read(cninfo_path)
    require(first['protocol_sha256'] == digest(protocol_path), 'original_protocol_changed')
    require(extra['original_protocol_sha256'] == digest(protocol_path)
            and extra['original_receipt_sha256'] == digest(receipt_path), 'original_source_capture_changed')
    require(stock['protocol_sha256'] == digest(supplement_path) and stock['url'] == extra['url']
            and stock['id'] == extra['id'], 'stock_source_binding_changed')
    require(final['original_protocol_sha256'] == digest(protocol_path)
            and final['first_receipt_sha256'] == digest(receipt_path)
            and final['second_receipt_sha256'] == digest(ROOT/'research/IDENTITY_STOCK_SOURCE_RECEIPT_20261002.json'), 'prior_capture_changed')
    require(cninfo['protocol_sha256'] == digest(final_path) and cninfo['url'] == final['url']
            and cninfo['id'] == final['id'], 'cninfo_source_binding_changed')
    sources = first['sources'] + [stock, cninfo]
    require(len(sources) == len({x['id'] for x in sources}), 'duplicate_source_id')
    expected = {x['id']: x['url'] for x in spec['issuer_requests']}
    for src in first['sources']:
        if src['id'] in expected:
            require(src['url'] == expected[src['id']], 'source_url_changed')
        else:
            require(src['id'] == 'stock_report_2026' and src['attempts'] == 0, 'unplanned_source')
    for src in sources:
        body = (ROOT/src['body_path']).resolve()
        require(body.is_relative_to(ROOT/'.codex-local/identity_sources_20261002'), 'body_custody_invalid')
        require(body.stat().st_size == src['bytes'] and digest(body) == src['sha256'], 'native_body_changed')
        require(src.get('retries', 0) == 0, 'request_budget_changed')
    gets = sum(s['attempts'] for s in sources)
    require(first['native_GETs'] + stock['attempts'] == 5 and 5 <= extra['cumulative_native_GET_limit'], 'prior_request_budget_changed')
    require(gets == first['native_GETs'] + stock['attempts'] + cninfo['attempts'] == 6
            and gets <= final['cumulative_native_GET_limit'], 'request_budget_exceeded')
    require(sum(s['bytes'] for s in sources) <= final['cumulative_body_limit'], 'body_budget_exceeded')
    for p, sha in spec['frozen_files_sha256'].items():
        require(digest(ROOT/p) == sha, 'frozen_input_changed')
    index_path = ROOT/'research/IDENTITY_OBSERVATIONS_20261002.json'
    index = read(index_path)
    require(index['selection'] == spec['selection'] and set(index['selection_roles']) == set(spec['selection']), 'selection_changed')
    require(index['selection_roles'] == {'600000.XSHG':'A_SHARE','510300.XSHG':'ETF'}, 'selection_roles_changed')
    observations = verified_catalog(ROOT, index, sources)
    # The same unchanged external raw file is read locally. Only key/date/hash
    # metadata leave this function; prices/volumes/turnover never reach stdout.
    pointer = read(ROOT/'.codex-local/short_rq_sample_20261001/manifest.json')
    path = Path(pointer['sample_path']).resolve()
    allowed = (Path(os.environ['LOCALAPPDATA'])/'RegisOracle-local-research/short-rq-20261001/sample.json').resolve()
    require(path == allowed and not path.is_relative_to(ROOT) and 'onedrive' not in str(path).lower(), 'quote_custody_invalid')
    require(path.stat().st_size <= 524288, 'sample_size_exceeded')
    require(pointer['sha256'] == spec['raw_input_sha256'] == digest(path), 'sample_identity_changed')
    prior = read(ROOT/'research/RQ_DAILY_ADMISSION_RESULT_20261002.json')
    require(prior['sample_sha256'] == pointer['sha256'], 'prior_sample_binding_changed')
    floor = prior['snapshot_known_at']
    raw = read(path)
    require(isinstance(raw, list) and len(raw) == pointer['rows'] == 42, 'actual_row_count_changed')
    keys = []
    for row in raw:
        require(isinstance(row, dict) and isinstance(row.get('order_book_id'), str)
                and type(row.get('trading_date')) is int, 'sample_key_invalid')
        s = str(row['trading_date'])
        require(len(s) == 8, 'sample_date_invalid')
        keys.append((row['order_book_id'], f'{s[:4]}-{s[4:6]}-{s[6:]}'))
    del raw
    clock = max(strict_utc(s['ended_utc']) for s in sources if s.get('ended_utc')).isoformat()
    def joined(at):
        return join_identity_keys(keys, observations, selection_roles=index['selection_roles'],
                                  snapshot_known_at=floor, decision_at=at)
    now = joined(clock)
    historical = joined('2026-09-30T15:00:00+08:00')
    capture = joined(floor)
    before = joined((strict_utc(floor)-timedelta(microseconds=1)).isoformat())
    require(sum(v['source_rows'] for v in now.values()) == 42
            and now['600000.XSHG']['filing_candidate_rows'] == 21
            and now['510300.XSHG']['filing_candidate_rows'] == 21, 'actual_join_outcome_changed')
    require(now['510300.XSHG']['operation_mode_conflict'] is True, 'document_discrepancy_lost')
    require(all(v['filing_candidate_rows'] == 0 for group in [historical, capture, before] for v in group.values()), 'capture_backdated')
    try:
        require_historical_or_economic_identity(observations)
    except IdentityEvidenceError:
        blocked = True
    else:
        blocked = False
    require(blocked, 'economic_gate_opened')
    return {'schema':'regis.identity_check_result.v1',
        'scope':'filing observations for both securities and actual key join only; no historical registry',
        'sample_sha256':pointer['sha256'], 'actual_rows_checked':42,
        'snapshot_known_at_unchanged':floor, 'assessment_at':clock,
        'successful_PDFs':sum(s.get('http_status') == 200 and s.get('complete_within_cap') is True for s in sources),
        'source_observations':len(observations), 'native_GETs':gets,
        'source_outcomes':[{'id':s['id'],'attempts':s['attempts'],'http_status':s.get('http_status'),
                           'bytes':s['bytes'],'sha256':s['sha256'],'error_type':s.get('error_type')} for s in sources],
        'per_security':now, 'unmatched_source_rows':sum(v['source_rows']-v['filing_candidate_rows'] for v in now.values()),
        'historical_filing_candidate_rows':sum(v['filing_candidate_rows'] for v in historical.values()),
        'candidate_rows_at_original_quote_capture':sum(v['filing_candidate_rows'] for v in capture.values()),
        'candidate_rows_before_original_quote_capture':sum(v['filing_candidate_rows'] for v in before.values()),
        'historical_economic_accessor_rejected':blocked,
        'all_selected_securities_have_original_identity_evidence':True,
        'historical_registry_complete':False, 'provider_units_verified':False,
        'N4_data_admitted':False,'N5_N8_complete':False,'model_admitted':False,
        'new_quote_requests':0, 'raw_quote_values_to_AI_Git':False, 'unit_conversion_performed':False,
        'permanent_ID_or_historical_valid_interval_inferred':False,'frozen_five_unchanged':True,
        'provenance_sha256':{str(p.relative_to(ROOT)).replace('\\','/'):digest(p) for p in [
            protocol_path, receipt_path, supplement_path, ROOT/'research/IDENTITY_STOCK_SOURCE_RECEIPT_20261002.json',
            final_path, cninfo_path, index_path, ROOT/'research/security_identity_evidence.py',Path(__file__)]},
        'blockers':['ETF_operation_mode_discrepancy_unresolved',
                    'historical_security_continuity_and_vintage_missing','provider_numeric_units_and_version_binding_missing',
                    'all_A_denominator_actions_status_and_real_minute_execution_missing']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--write-summary', action='store_true')
    args = parser.parse_args()
    try:
        result = inspect()
        if args.write_summary:
            RESULT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except IdentityEvidenceError as exc:
        print(json.dumps({'status':'rejected','safe_error_code':str(exc),'raw_quote_values_printed':False}))
        return 1
    except Exception as exc:
        print(json.dumps({'status':'failed','error_type':type(exc).__name__,'raw_quote_values_printed':False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
