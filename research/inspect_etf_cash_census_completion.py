"""Reconcile the remaining census cells; field gaps never become zeroes."""
from decimal import Decimal
import hashlib
from html import unescape
import json
import os
from pathlib import Path
import re
import unicodedata

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]

def read(path):
    return json.loads((ROOT / path).read_text(encoding='utf-8'))

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def compact(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))

def extract_etf_notice_block(text):
    """Bind all fields to the unique 510300 notice in a multi-fund paper."""
    text = compact(text)
    title = '华泰柏瑞沪深300交易型开放式指数证券投资基金收益分配公告'
    assert text.count(title) == 1, 'missing or ambiguous target notice title'
    begin = text.index(title)
    # Native newspaper extraction interleaves different headings/footer layouts.
    # Sections 1 and 2 contain all required cells; stop at this notice's section 3.
    end_marker = '3其他需要提示的事项'
    end = text.index(end_marker, begin) + len(end_marker)
    block = text[begin:end]
    assert re.findall(r'基金主代码(\d+)', block) == ['510300'], 'notice code ambiguity'
    assert '基金名称华泰柏瑞沪深300交易型开放式指数证券投资基金' in block
    return block

def extract_cash_per_10_units(block):
    """Read one complete cash cell, never a prefix or a neighboring scheme."""
    block = compact(block)
    assert block.count('本次分红方案') == 1, 'missing or ambiguous cash scheme'
    cell = block.split('本次分红方案', 1)[1].split('有关年度分红次数', 1)
    assert len(cell) == 2, 'cash cell boundary missing'
    match = re.fullmatch(r'\(单位:元/10份基金份额\)(\d+(?:\.\d+)?)', cell[0])
    assert match is not None, 'cash amount or per-ten-unit denomination ambiguous'
    return Decimal(match.group(1))

def validate_notice_fields(block, record, ex, pay, amount):
    """All dates and the exact decimal amount must belong to the same block."""
    block = compact(block)
    assert re.findall(r'基金主代码(\d+)', block) == ['510300']
    assert '基金名称华泰柏瑞沪深300交易型开放式指数证券投资基金' in block
    assert extract_cash_per_10_units(block) == Decimal(amount), 'target cash amount mismatch'
    starts = list(re.finditer(r'2\.?与分红相关的其他信息', block))
    assert len(starts) == 1, 'missing or ambiguous date section'
    dates = re.split(r'3\.?其他需要提示的事项', block[starts[0].end():], maxsplit=1)
    assert len(dates) == 2, 'date section boundary missing'
    for label, iso in [('权益登记日', record), ('除息日', ex), ('现金红利发放日', pay)]:
        year, month, day = map(int, iso.split('-'))
        found = re.findall(re.escape(label) + r'(\d{4}年\d{1,2}月\d{1,2}日)', dates[0])
        assert found == [f'{year}年{month}月{day}日'], 'missing, wrong or ambiguous ' + label

def inspect_2018_locator_integrity():
    """Transport success and byte-cap completion do not certify a PDF."""
    receipt = read('research/ETF_2018_LOCATOR_RECEIPT_20261002.json')
    protocol = ROOT/'research/ETF_2018_LOCATOR_PROTOCOL_20261002.json'
    assert receipt['protocol_sha256'] == sha(protocol)
    assert receipt['GET_attempts'] == 1 and receipt['retries'] == 0
    path = Path(receipt['body_path']).resolve()
    base = (Path(os.environ['LOCALAPPDATA'])/'RegisOracle-local-research/etf-component-20261002').resolve()
    assert path.is_relative_to(base)
    body = path.read_bytes()
    assert len(body) == receipt['bytes'] and sha(path) == receipt['sha256']
    error = None
    try:
        PdfReader(path)
    except Exception as exc:
        error = {'type': type(exc).__name__, 'message': str(exc)}
    # This exact body is an inadmissible source, not an absent dividend or zero.
    assert body.startswith(b'%PDF-') and b'%%EOF' not in body[-2048:]
    assert error == {'type': 'PdfStreamError', 'message': 'Stream has ended unexpectedly'}
    return {'schema': 'regis.etf.locator_integrity_result.v1',
        'protocol_sha256': sha(protocol), 'source_sha256': receipt['sha256'],
        'bytes': len(body), 'transport_status': receipt['http_status'],
        'body_complete_within_cap_claim_is_not_PDF_integrity': True,
        'PDF_header_present': True, 'PDF_EOF_present': False,
        'PDF_startxref_in_tail_present': b'startxref' in body[-4096:],
        'PDF_parser_error': error, 'PDF_integrity_passed': False,
        'source_admitted': False, 'secondary_ex_date_provided': False,
        'socket_timeout_is_not_end_to_end_time_certification': True,
        'recorded_request_start_utc': receipt['started_utc'],
        'recorded_request_end_utc': receipt['ended_utc'],
        'raw_body_repaired_or_retried': False, 'new_economic_runs': 0,
        'scope': 'One inferred URI probe; no indexed exact source discovery proof, no further blind variants'}

def inspect():
    prior = read('research/ETF_ACTION_RECONCILIATION_RESULT_20261002.json')
    census = read('research/ETF_ACTION_CENSUS_RESULT_20261002.json')
    events = {event['event_key']: dict(event) for event in census['events']}
    supported = dict(prior['census_events_with_source_supported_ex_dates'])
    protocol = ROOT / 'research/ETF_ACTION_CASH_COMPLETION_PROTOCOL_20261002.json'
    receipt = read('research/ETF_ACTION_CASH_COMPLETION_RECEIPT_20261002.json')
    assert receipt['protocol_sha256'] == sha(protocol) and receipt['GET_attempts'] == 7
    base = (Path(os.environ['LOCALAPPDATA']) / 'RegisOracle-local-research/etf-component-20261002').resolve()
    expected = {
        'notice_2014': ('2014-01-20', '2014-01-21', '2014-01-27', '0.48'),
        'newspaper_notice_2015': ('2015-01-19', '2015-01-20', '2015-01-23', '0.35'),
        'notice_2016': ('2016-01-19', '2016-01-20', '2016-01-25', '0.51'),
        'notice_2017': ('2017-01-20', '2017-01-23', '2017-01-26', '0.55'),
        'notice_2022': ('2022-01-18', '2022-01-19', '2022-01-24', '0.75'),
    }
    observations = {}
    for source in receipt['sources']:
        sid = source['id']
        path = Path(source['body_path']).resolve()
        assert path.is_relative_to(base) and sha(path) == source['sha256']
        assert path.stat().st_size == source['bytes'] and source['http_status'] == 200 and source['complete_within_cap']
        if sid == 'exchange_ex_notice_2024':
            assert source['content_type'].startswith('text/html')
            text = compact(unescape(re.sub(r'<[^>]+>', ' ', path.read_text(encoding='utf-8'))))
            assert '根据华泰柏瑞基金管理有限公司公告' in text and '2024年1月18日除息' in text
            assert '沪深300' in text
            observations[sid] = {'event_key': '510300.SH:2024-01-17', 'source_sha256': source['sha256'],
                'source_format': 'HTML; preserved .pdf body filename is not a PDF format assertion',
                'ex_date': '2024-01-18', 'scope': 'exchange option adjustment corroborates ETF ex-date only',
                'record_date_verified_by_this_source': False, 'cash_amount_or_pay_verified_by_this_source': False}
            supported['510300.SH:2024-01-17'] = {'ex_date': '2024-01-18', 'source_id': sid}
            continue
        doc = PdfReader(path)
        if sid == 'interim_2018':
            assert len(doc.pages) == 66
            section = compact(doc.pages[32].extract_text().split('6.4.11', 1)[1])
            assert all(term in section for term in ['权益登记日', '场内场外', '2018年1月23日', '0.46'])
            observations[sid] = {'source_sha256': source['sha256'], 'cash_table_pdf_page': 33,
                'record_date_as_printed': '2018-01-23', 'on_exchange_ex_date_as_printed': '-',
                'off_exchange_ex_date_as_printed': '2018-01-23', 'CNY_per_10_fund_units': '0.46',
                'issuer_record_date': '2018-01-22', 'record_conflict_preserved': True,
                'off_exchange_date_not_admitted_as_on_exchange_ex_date': True}
            # This table does NOT supply the required secondary-market ex-date.
            # Preserve the apparent reversed/misaligned on/off-exchange cells.
            continue
        assert sid in expected
        record, ex, pay, amount = expected[sid]
        text = compact(''.join(page.extract_text() for page in doc.pages))
        if sid == 'newspaper_notice_2015':
            assert len(doc.pages) == 1
            block = extract_etf_notice_block(text)
        else:
            assert len(doc.pages) <= 4
            block = text
        validate_notice_fields(block, record, ex, pay, amount)
        key = '510300.SH:' + record
        assert events[key]['issuer_table_record_date'] == record
        assert events[key]['issuer_table_pay_or_reinvestment_date'] == pay
        assert Decimal(events[key]['CNY_per_10_fund_units']) == Decimal(amount)
        observations[sid] = {'event_key': key, 'source_sha256': source['sha256'],
            'record_date': record, 'ex_date': ex, 'declared_investor_pay_date': pay,
            'CNY_per_10_fund_units': amount, 'actual_personal_credit_at': None,
            'historical_available_at': None, 'cash_cell_visual_review': 'crop only',
            'newspaper_multi_fund_binding': sid == 'newspaper_notice_2015',
            'amount_and_dates_bound_to_same_unique_fund_notice': True,
            'cash_amount_exact_decimal_and_per_ten_unit_cell_verified': True}
        supported[key] = {'ex_date': ex, 'source_id': sid}
    missing = sorted(set(events) - set(supported))
    assert len(supported) == 13 and missing == ['510300.SH:2018-01-22']
    for key, event in events.items():
        event['candidate_ex_date'] = supported.get(key, {}).get('ex_date')
        event['ex_date_source_id'] = supported.get(key, {}).get('source_id')
        event['source_quality_status'] = 'candidate_fields_only_no_L1'
    ledger = read('research/ETF_DISCLOSURE_EXPOSURE_LEDGER_20261002.json')
    assert ledger['prior_exposure_history'] == 'unknown_not_reset'
    assert ledger['zero_economic_runs_does_not_mean_zero_exposure']
    assert ledger['historical_windows_must_not_be_called_completely_blind']
    integrity = inspect_2018_locator_integrity()
    (ROOT/'research/ETF_2018_LOCATOR_INTEGRITY_RESULT_20261002.json').write_text(
        json.dumps(integrity, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return {'schema': 'regis.etf.cash_census_fields.v1', 'events': list(events.values()),
        'census_rows': 14, 'source_supported_secondary_ex_dates': 13, 'missing_ex_date_event_keys': missing,
        'new_source_observations': observations, 'prior_period_quality_issues_preserved': prior['conflicts'],
        'prior_2023_record_conflict_preserved': True, 'new_2018_record_and_on_off_exchange_conflict_preserved': True,
        'field_counts_are_not_complete_action_certification': True,
        'all_14_secondary_ex_dates_verified': False, 'historical_vintage_admitted': False,
        'other_action_history_complete': False, 'actual_personal_cash_credit_proven': False,
        'prelisting_fold_not_reapplied_to_secondary_market_purchase': True,
        'exposure_ledger_sha256': sha(ROOT / 'research/ETF_DISCLOSURE_EXPOSURE_LEDGER_20261002.json'),
        '2018_inferred_URI_probe': {'source_integrity_passed': False, 'source_admitted': False,
            'result_path': 'research/ETF_2018_LOCATOR_INTEGRITY_RESULT_20261002.json'},
        'raw_quote_API_calls': 0, 'economic_runs': 0, 'model_admitted': False,
        'full_L1': False, 'full_L2': False, 'full_L3': False, 'full_L4': False,
        'controlled_cloud_raw_custody_verified': False}

if __name__ == '__main__':
    result = inspect()
    (ROOT / 'research/ETF_ACTION_CASH_COMPLETION_RESULT_20261002.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'actual_census_rows': 14, 'source_supported_secondary_ex_dates': 13,
        'remaining_ambiguous_ex_date_events': 1, 'record_and_on_off_exchange_conflict_preserved': True,
        'economic_runs': 0, 'model_admitted': False}))
