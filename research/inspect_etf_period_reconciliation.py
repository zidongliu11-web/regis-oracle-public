"""Audit acquired ETF disclosure fields and period conflicts; no price replay."""
from datetime import date
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]

def read(path):
    return json.loads((ROOT / path).read_text(encoding='utf-8'))

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def compact(text):
    return re.sub(r'\s+', '', text)

def inspect():
    base = (Path(os.environ['LOCALAPPDATA']) / 'RegisOracle-local-research/etf-component-20261002').resolve()
    old = read('research/ETF_ACTION_CENSUS_RESULT_20261002.json')
    events = {event['event_key']: event for event in old['events']}
    assert len(events) == 14 and not old['full_ETF_action_history_admitted']
    lp = ROOT / 'research/ETF_ACTION_LEGACY_ROLLUP_PROTOCOL_20261002.json'
    legacy = read('research/ETF_ACTION_LEGACY_ROLLUP_RECEIPT_20261002.json')
    rp = ROOT / 'research/ETF_ACTION_RECONCILIATION_PROTOCOL_20261002.json'
    receipt = read('research/ETF_ACTION_RECONCILIATION_RECEIPT_20261002.json')
    assert legacy['protocol_sha256'] == sha(lp) and receipt['protocol_sha256'] == sha(rp)
    assert legacy['GET_attempts'] == 1 and receipt['GET_attempts'] == 3

    def document(source):
        assert source['http_status'] == 200 and source['complete_within_cap']
        path = Path(source['body_path']).resolve()
        assert path.is_relative_to(base) and path.stat().st_size == source['bytes']
        assert sha(path) == source['sha256']
        return PdfReader(path)

    doc = document(legacy)
    assert len(doc.pages) == 180
    text = doc.pages[96].extract_text()
    table = text.split('2、基金的收益分配情况', 1)[1].split('3、自基金合同', 1)[0]
    rows = []
    for line in table.splitlines():
        m = re.fullmatch(r'\s*(20\d\d)\s+([\d.]+)\s+(20\d\d)\s*年\s*(\d+)\s*月\s*(\d+)\s*日实施\s*', line)
        if m:
            income_year, amount, year, month, day = m.groups()
            rows.append({'reported_year_label': income_year, 'CNY_per_10_fund_units': amount,
                         'implementation_date_as_printed': date(int(year), int(month), int(day)).isoformat()})
    assert len(rows) == 9 and '2020 - -' in table and '合计 3.89' in table
    displayed_sum = sum((Decimal(row['CNY_per_10_fund_units']) for row in rows), Decimal(0))
    assert displayed_sum == Decimal('4.61')
    keys_by_year = {
        '2012': '510300.SH:2012-12-17', '2013': '510300.SH:2014-01-20',
        '2014': '510300.SH:2015-01-19', '2015': '510300.SH:2016-01-19',
        '2016': '510300.SH:2017-01-20', '2017': '510300.SH:2018-01-22',
        '2018': '510300.SH:2019-01-15', '2019': '510300.SH:2019-12-10',
        '2021': '510300.SH:2021-01-15',
    }
    # Association is an explicit inspected-source correspondence, not a generic
    # join by income year or a correction of the original implementation cell.
    for row in rows:
        key = keys_by_year[row['reported_year_label']]
        assert Decimal(row['CNY_per_10_fund_units']) == Decimal(events[key]['CNY_per_10_fund_units'])
        row['associated_census_event_key'] = key
        row['association_basis'] = 'inspected table amount and period correspondence; original year/date labels preserved'

    observations = {}
    for source in receipt['sources']:
        doc = document(source)
        sid = source['id']
        if sid == 'annual_2019':
            assert len(doc.pages) == 79
            info = compact(doc.pages[5].extract_text())
            distribution = compact(doc.pages[44].extract_text().split('7.4.11', 1)[1].split('7.4.12', 1)[0])
            assert all(x in info for x in ['基金主代码510300', '2012年5月28日', '上海证券交易所'])
            assert all(x in distribution for x in ['2019年12月10日2019年12月11日0.62',
                       '2019年1月15日2019年1月16日0.59', '合计1.21'])
            observations[sid] = {'source_sha256': source['sha256'], 'semantic_pages': [6, 45],
                'reported_secondary_listing_date': '2012-05-28',
                'events': [{'event_key': '510300.SH:2019-12-10', 'record_date': '2019-12-10',
                            'ex_date': '2019-12-11', 'CNY_per_10_fund_units': '0.62'},
                           {'event_key': '510300.SH:2019-01-15', 'record_date': '2019-01-15',
                            'ex_date': '2019-01-16', 'CNY_per_10_fund_units': '0.59'}],
                'historical_available_at': None, 'declared_personal_pay_date': None}
        elif sid == 'annual_2012':
            assert len(doc.pages) == 60
            info = compact(doc.pages[4].extract_text())
            distribution = compact(doc.pages[35].extract_text().split('7.4.11', 1)[1])
            assert all(x in info for x in ['基金主代码510300', '上市日期2012-05-28'])
            assert '2012年12月17日2012年12月18日0.33' in distribution
            observations[sid] = {'source_sha256': source['sha256'],
                'text_semantic_pages': [5, 6, 36, 37], 'visually_verified_relevant_pages': [5, 36],
                'reported_secondary_listing_date': '2012-05-28',
                'events': [{'event_key': '510300.SH:2012-12-17', 'record_date': '2012-12-17',
                            'ex_date': '2012-12-18', 'CNY_per_10_fund_units': '0.33'}],
                'historical_available_at': None, 'declared_personal_pay_date': None}
        elif sid == 'notice_2021':
            assert len(doc.pages) == 3
            alltext = compact(''.join(page.extract_text() for page in doc.pages))
            assert all(x in alltext for x in ['权益登记日2021年1月15日', '除息日2021年1月18日',
                       '现金红利发放日2021年1月21日', '0.72', '2020年第一次分红',
                       '2021年1月19日', '2021年1月20日'])
            observations[sid] = {'source_sha256': source['sha256'], 'semantic_pages': [1, 2, 3],
                'announcement_date': '2021-01-11', 'record_date': '2021-01-15', 'ex_date': '2021-01-18',
                'CNY_per_10_fund_units': '0.72', 'declared_investor_pay_date': '2021-01-21',
                'declared_issuer_to_clearing_date': '2021-01-19', 'declared_clearing_to_broker_date': '2021-01-20',
                'income_year_statement': '2020年第一次分红', 'historical_available_at': None,
                'actual_personal_credit_at': None}
        else:
            raise ValueError('unexpected source')

    supported = {}
    for source_id in ['annual_2019', 'annual_2012']:
        for action in observations[source_id]['events']:
            key = action['event_key']
            assert Decimal(action['CNY_per_10_fund_units']) == Decimal(events[key]['CNY_per_10_fund_units'])
            assert action['record_date'] == events[key]['issuer_table_record_date']
            supported[key] = {**action, 'source_id': source_id}
    n = observations['notice_2021']
    key = '510300.SH:' + n['record_date']
    assert n['declared_investor_pay_date'] == events[key]['issuer_table_pay_or_reinvestment_date']
    supported[key] = {'event_key': key, 'ex_date': n['ex_date'], 'source_id': 'notice_2021'}
    prior_2025 = read('research/GOAL_ETF_ACTION_EVIDENCE_20261001.json')['announced_fields']
    supported['510300.SH:2025-06-17'] = {'ex_date': prior_2025['ex_date'], 'source_id': 'prior_2025_notice'}
    supported['510300.SH:2023-01-13'] = {'ex_date': old['2023_notice_observation']['ex_date'], 'source_id': 'prior_2023_notice'}
    supported['510300.SH:2026-01-16'] = {'ex_date': old['period_source_observations']['latest_notice_2026']['ex_date'], 'source_id': 'prior_2026_notice'}
    missing = sorted(set(events) - set(supported))
    assert len(supported) == 7 and len(missing) == 7
    conflicts = [
        {'field': 'sum_CNY_per_10_units', 'source': 'legacy_prospectus_2022',
         'computed_from_nine_printed_positive_rows': str(displayed_sum), 'printed_total': '3.89',
         'resolution': 'preserved; no total-return use or silent correction'},
        {'event_key': '510300.SH:2019-12-10', 'field': 'implementation_date',
         'legacy_as_printed': '2019-02-10', 'annual_record': '2019-12-10', 'annual_ex': '2019-12-11',
         'resolution': 'legacy label cannot be matched to record or ex; original preserved'},
        {'event_key': '510300.SH:2021-01-15', 'field': 'implementation_date_is_not_uniform_record_date',
         'legacy_as_printed': '2021-01-18', 'notice_record': '2021-01-15', 'notice_ex': '2021-01-18',
         'resolution': 'implementation equals ex here; never map all implementation dates to record'},
    ]
    return {
        'schema': 'regis.etf.period_reconciliation.v1',
        'legacy_source_sha256': legacy['sha256'], 'legacy_table_pdf_page': 97,
        'legacy_positive_rows': rows, 'legacy_dash_row': {'year_label': '2020', 'printed_amount': '-',
            'amount_numeric': None, 'meaning': 'source year label is not implementation year; no zero-income or zero-calendar-event inference'},
        'legacy_printed_total': '3.89', 'computed_positive_row_total': str(displayed_sum),
        'row_totals_reconciled': False, 'source_observations': observations, 'conflicts': conflicts,
        'income_year_implementation_record_ex_pay_are_separate': True,
        'notice_2021_income_2020_does_not_mean_calendar2020_event': True,
        'retrospective_initial_fold_before_reported_secondary_listing': True,
        'fold_date': old['period_source_observations']['period_prospectus_2025']['fold_result']['reported_fold_date'],
        'reported_secondary_listing_date': '2012-05-28',
        'prelisting_fold_must_not_be_reapplied_to_later_secondary_purchase': True,
        'census_events_with_source_supported_ex_dates': supported,
        'source_supported_ex_date_count': len(supported), 'ex_date_missing_event_keys': missing,
        'all_14_events_complete': False, 'other_action_history_complete': False,
        'permanent_identity_or_historical_vintage_admitted': False,
        'historical_available_at_assigned': False, 'actual_personal_cash_credit_proven': False,
        'raw_quote_API_calls': 0, 'raw_quotes_to_AI_Git': False, 'economic_runs': 0,
        'full_L1': False, 'full_L2': False, 'full_L3': False, 'full_L4': False,
        'model_admitted': False, 'controlled_cloud_raw_custody_verified': False,
    }

if __name__ == '__main__':
    result = inspect()
    (ROOT / 'research/ETF_ACTION_RECONCILIATION_RESULT_20261002.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'real_positive_rollup_rows': len(result['legacy_positive_rows']),
        'preserved_new_conflicts': len(result['conflicts']), 'source_supported_ex_dates': 7,
        'remaining_ex_dates': 7, 'total_reconciled': False, 'economic_runs': 0, 'model_admitted': False}))
