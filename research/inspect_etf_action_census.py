"""Inspect actual issuer action rows and existing filings; never replay money."""
from pathlib import Path
from html.parser import HTMLParser
from datetime import datetime
from decimal import Decimal
import hashlib,json,os,re
from pypdf import PdfReader

ROOT=Path(__file__).resolve().parents[1]
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def read(p):return json.loads((ROOT/p).read_text(encoding='utf-8'))
class Table(HTMLParser):
    def __init__(self):super().__init__();self.cell=False;self.row=None;self.rows=[];self.buffer=[]
    def handle_starttag(self,tag,attrs):
        if tag=='tr':self.row=[]
        if tag in ('td','th'):self.cell=True;self.buffer=[]
    def handle_endtag(self,tag):
        if tag in ('td','th') and self.cell:
            self.cell=False
            if self.row is not None:self.row.append(''.join(self.buffer).strip())
        if tag=='tr' and self.row is not None:self.rows.append(self.row);self.row=None
    def handle_data(self,text):
        if self.cell:self.buffer.append(text)
def inspect():
    protocol=read('research/ETF_ACTION_CENSUS_PROTOCOL_20261002.json')
    custody=read('.codex-local/etf_component_discovery_20261002/custody.json')
    base=(Path(os.environ['LOCALAPPDATA'])/'RegisOracle-local-research/etf-component-20261002').resolve()
    ref=custody['action_fragment'];fragment=Path(ref['path']).resolve()
    assert fragment.is_relative_to(base) and digest(fragment)==ref['sha256'] and fragment.stat().st_size==ref['bytes']<16384
    fullref=next(r for r in custody['files'] if r['current_local_path'].endswith('/product_full.response'))
    full=Path(fullref['current_local_path']).resolve()
    assert full.is_relative_to(base) and digest(full)==fullref['sha256']==protocol['source_full_sha256']
    assert full.stat().st_size==fullref['bytes']==1347356
    fulltext=full.read_text(encoding='utf-8');start=re.search(r'id=[\x22\x27]fhxx[\x22\x27]',fulltext).start()
    assert fulltext[start:fulltext.index('</table>',start)+8].encode()==fragment.read_bytes()
    t=Table();t.feed(fragment.read_text(encoding='utf-8'))
    assert t.rows[0]==['分红年度','权益登记日','红利发放日/再投资日','每10份基金份额分红（元）']
    assert len(t.rows)-1==len(protocol['expected_event_keys'])==14
    events=[]
    for year,record,pay,cash in t.rows[1:]:
        assert datetime.strptime(record,'%Y-%m-%d').strftime('%Y-%m-%d')==record
        assert datetime.strptime(pay,'%Y-%m-%d').strftime('%Y-%m-%d')==pay and pay>=record
        amount=Decimal(cash);assert amount.is_finite() and amount>0
        events.append({'event_key':'510300.SH:'+record,'issuer_year_label':year,'issuer_table_record_date':record,
                       'issuer_table_pay_or_reinvestment_date':pay,'CNY_per_10_fund_units':cash,
                       'derived_CNY_per_fund_unit':format(amount/10,'f'),'announcement_date':None,'ex_date':None,
                       'historical_available_at':None,'actual_personal_credit_at':None,'status':'candidate_census_not_admitted'})
    keys=[e['event_key'] for e in events];assert keys==protocol['expected_event_keys'] and len(set(keys))==14
    old=read('research/GOAL_ETF_ACTION_EVIDENCE_20261001.json');a=old['announced_fields']
    e=next(x for x in events if x['issuer_table_record_date']==a['record_date'])
    assert Decimal(e['CNY_per_10_fund_units'])==Decimal(a['amount_CNY_per_10_units']) and e['issuer_table_pay_or_reinvestment_date']==a['declared_investor_pay_date']
    reportref=next(x for x in read('research/IDENTITY_SOURCE_RECEIPT_20261002.json')['sources'] if x['id']=='etf_interim_2023')
    report=ROOT/reportref['body_path'];assert digest(report)==reportref['sha256'];pdf=PdfReader(report);assert len(pdf.pages)==185
    text=pdf.pages[144].extract_text();assert '沪深' in text and '300ETF' in text
    distribution=text.split('6.4.11')[1];assert all(v in distribution for v in ['2023','0.6400','16'])
    # Explicit observations are visually checked in the journal. Do not silently
    # normalize the report's Jan16 record cell to the issuer table's Jan13.
    report_observation={'source_sha256':reportref['sha256'],'pages':[13,145,184],'record_date_as_printed':'2023-01-16',
                        'ex_date_as_printed':'2023-01-16','CNY_per_10_units_as_printed':'0.6400',
                        'notice_disclosure_date_as_printed':'2023-01-09','historical_available_at':None}
    conflicts=[{'event_key':'510300.SH:2023-01-13','field':'record_date','issuer_table':'2023-01-13',
                'interim_report':'2023-01-16','resolution':'pending_original_notice_crosscheck_no_overwrite'}]
    notice=read('research/ETF_ACTION_CENSUS_NOTICE_RECEIPT_20261002.json')
    notice_observation=None
    if notice.get('http_status')==200 and notice.get('complete_within_cap'):
        path=Path(notice['body_path']).resolve();assert path.is_relative_to(base) and digest(path)==notice['sha256']
        pages=PdfReader(path);assert len(pages.pages)<=4
        texts=[p.extract_text() for p in pages.pages];compact=re.sub(r'\s+','',''.join(texts))
        assert all(v in compact for v in ['510300','权益登记日2023年1月13日','除息日2023年1月16日','现金红利发放日2023年1月19日','0.6400'])
        notice_observation={'source_sha256':notice['sha256'],'pages':len(pages.pages),'announcement_date':'2023-01-09',
                            'record_date':'2023-01-13','ex_date':'2023-01-16','declared_investor_pay_date':'2023-01-19',
                            'CNY_per_10_units':'0.6400','historical_available_at':None,'actual_credit':None}
        conflicts[0]['resolution']='notice_supports_issuer_table_original_report_conflict_preserved_not_full_L1'
    period=read('research/ETF_ACTION_PERIOD_RECEIPT_20261002.json')
    assert period['protocol_sha256']==digest(ROOT/'research/ETF_ACTION_PERIOD_PROTOCOL_20261002.json')
    period_observations={}
    for source in period['sources']:
        if source.get('http_status')!=200 or not source.get('complete_within_cap'):
            period_observations[source['id']]={'status':'source_not_acquired','receipt_failure_preserved':True}
            continue
        path=Path(source['body_path']).resolve();assert path.is_relative_to(base) and digest(path)==source['sha256']
        doc=PdfReader(path)
        if source['id']=='period_prospectus_2025':
            assert len(doc.pages)==134
            # Local heading scan locates evidence; only 1/34/35 were visually
            # inspected. Do not claim the whole prospectus was semantically read.
            heading_count=0
            for page in doc.pages:
                text=re.sub(r'\s+','',page.extract_text())
                heading_count+=int('基金的收益分配情况' in text or '每10份基金份额分红数' in text)
            fold=re.sub(r'\s+','',doc.pages[34].extract_text()).split('（四）基金份额折算结果')[1]
            assert all(x in fold for x in ['2012年5月11日','0.37094933','2012年5月14日'])
            period_observations[source['id']]={'source_sha256':source['sha256'],'semantic_pages_inspected':[1,34,35],
                'local_header_scan_pages':134,'target_cash_rollup_heading_matches':heading_count,
                'historical_cash_rollup_rows_recovered':0,
                'scope':'this2025version did not yield targetcashrollup; not proof allprospectuses lackit',
                'fold_result':{'reported_fold_date':'2012-05-11','reported_registration_date':'2012-05-14',
                    'reported_new_units_per_old_unit':'0.37094933','rounding':'nearest_integer_per_holder',
                    'example_ratio_0_26671819_is_not_an_actual_action':True,'historical_available_at':None},
                'other_action_history_complete':False}
        elif source['id']=='latest_notice_2026':
            assert len(doc.pages)==3
            compact=re.sub(r'\s+','',''.join(p.extract_text() for p in doc.pages))
            assert all(x in compact for x in ['510300','权益登记日2026年1月16日','除息日2026年1月19日',
                       '现金红利发放日2026年1月27日','1.2300'])
            latest=next(e for e in events if e['issuer_table_record_date']=='2026-01-16')
            assert Decimal(latest['CNY_per_10_fund_units'])==Decimal('1.2300') and latest['issuer_table_pay_or_reinvestment_date']=='2026-01-27'
            period_observations[source['id']]={'event_key':latest['event_key'],'source_sha256':source['sha256'],'pages':[1,2,3],
                'announcement_date':'2026-01-12','record_date':'2026-01-16','ex_date':'2026-01-19',
                'CNY_per_10_fund_units':'1.2300','declared_investor_pay_date':'2026-01-27',
                'declared_issuer_to_clearing':'2026-01-23','declared_clearing_to_broker':'2026-01-26',
                'historical_available_at':None,'actual_personal_credit':None}
        else:raise ValueError('unexpected_period_source')
    result={'schema':'regis.etf.action_census.v1','purpose':'14-row issuer census and per-source evidence, not complete action admission',
            'protocol_sha256':digest(ROOT/'research/ETF_ACTION_CENSUS_PROTOCOL_20261002.json'),
            'issuer_url':protocol['issuer_url'],'source_full_sha256':protocol['source_full_sha256'],'action_fragment':ref,
            'source_full_HTTP_status':'lost_after_collector_parse_error_not_reconstructed','events':events,
            'current_census_rows':14,'duplicates':0,'two_events_in_2019':True,
            'years_without_rows_are_unknown':['2013','2020'],'split_merger_other_action_completeness':False,
            'shared_pay_reinvestment_header_not_personal_cash':True,'matched_2025_fields':['record_date','payable_date','cash_unit_and_amount'],
            'reused_2025_notice_ref':{'path':'research/GOAL_ETF_ACTION_EVIDENCE_20261001.json','sha256':digest(ROOT/'research/GOAL_ETF_ACTION_EVIDENCE_20261001.json')},
            '2023_report_observation':report_observation,'2023_notice_observation':notice_observation,'conflicts':conflicts,
            'period_source_observations':period_observations,
            'full_ETF_action_history_admitted':False,'full_N4_passed':False,'economic_runs':0,'model_admitted':False,
            'raw_quote_AI_or_Git_sent':False,'source_cloud_custody_verified':False}
    return result
if __name__=='__main__':
    result=inspect();(ROOT/'research/ETF_ACTION_CENSUS_RESULT_20261002.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'actual_census_rows':14,'actual_notice_crosschecks':1+int(result['2023_notice_observation'] is not None)+int('event_key' in result['period_source_observations'].get('latest_notice_2026',{})),
                      'preserved_conflicts':len(result['conflicts']),'full_ETF_action_history_admitted':False,'economic_runs':0}))
