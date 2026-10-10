"""Rebuild bounded primary document findings; never reads market quote arrays."""
import hashlib,json,re,sys
from datetime import datetime,timezone
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
R=Path(__file__).resolve().parents[1]
P=R/'research/SOURCE_COVERAGE_PROTOCOL_20261003.json'
O=R/'research/SOURCE_COVERAGE_RESULT_20261003.json'
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def read(p):return json.loads(Path(p).read_text(encoding='utf-8'))
class Tables(HTMLParser):
 def __init__(self):super().__init__();self.tables=[];self.table=None;self.row=None;self.cell=None;self.text=[];self.skip=0
 def handle_starttag(self,tag,attrs):
  if tag in ('script','style','pre'):self.skip+=1
  if tag=='table':self.table=[]
  elif tag=='tr' and self.table is not None:self.row=[]
  elif tag in ('td','th') and self.row is not None:self.cell=[]
 def handle_endtag(self,tag):
  if tag in ('script','style','pre'):self.skip=max(0,self.skip-1)
  if tag in ('td','th') and self.cell is not None:self.row.append(' '.join(' '.join(self.cell).split()));self.cell=None
  elif tag=='tr' and self.row is not None:self.table.append(self.row);self.row=None
  elif tag=='table' and self.table is not None:self.tables.append(self.table);self.table=None
 def handle_data(self,data):
  if not self.skip:self.text.append(data)
  if self.cell is not None:self.cell.append(data)
def findings(s):
 for p,h in s['input_hashes'].items():assert sha(R/p)==h,p
 for p,h in s['native_input_hashes'].items():assert sha(p)==h,p
 D=Path(s['native_directory']);manifest=read(D/'manifest.json');assert manifest['actual_GET_attempts']==10 and manifest['market_quote_calls']==0 and manifest['dataset_archive_downloads']==0 and not manifest['credential_read']
 docs={}
 for rec in manifest['receipts']:
  assert rec['attempts']==1 and rec['status']==200 and rec['saved'] and rec['within_byte_cap'] and not rec['auth_used']
  path=D/(rec['id']+'.source');assert path.stat().st_size==rec['bytes'] and sha(path)==rec['sha256'];docs[rec['id']]=path.read_text(encoding='utf-8')
 parsers={}
 for name in ['tushare_price','tushare_minute_faq','tushare_stock_minute','tushare_etf_minute','tushare_service']:
  p=Tables();p.feed(docs[name]);parsers[name]=p
 rows=[row for table in parsers['tushare_price'].tables for row in table]
 def row(key):
  matches=[r for r in rows if r and r[0]==key];assert len(matches)==1,key;return matches[0]
 hist=row('历史分钟');credit2=row('2000以上');credit5=row('5000以上');live=row('实时分钟')
 assert hist[2]=='2009年' and re.fullmatch(r'单独2000元',hist[3]) and credit2[-1]=='200' and credit5[-1]=='500' and live[3]=='1000/月'
 def output_fields(name):
  matches=[table for table in parsers[name].tables if table and table[0]==['名称','类型','默认显示','描述']];assert len(matches)==1
  fields=[r[0] for r in matches[0][1:]];assert fields==['ts_code','trade_time','open','close','high','low','vol','amount'];assert matches[0][-2][-1] in ['成交量(股)','成交量（股）'] and matches[0][-1][-1]=='成交金额（元）';return fields
 stock_fields=output_fields('tushare_stock_minute');etf_fields=output_fields('tushare_etf_minute')
 faq=' '.join(parsers['tushare_minute_faq'].text);service=' '.join(parsers['tushare_service'].text)
 assert '数据只供策略研究和学习使用' in faq and '不允许作为商业目的' in faq
 assert '仅可为非商业目的使用' in service and '仅可用作个人查看使用' in service
 assert 'official dataset is disabled temporarily' in docs['qlib_readme'] and 'cn_data_1min' in docs['qlib_readme']
 assert 'Apache License' in docs['community_license'] and 'Version 2.0' in docs['community_license']
 assert 'w(wind)' in docs['community_readme'] and 'c(caihui)' in docs['community_readme'] and 'Tushare data source' in docs['community_readme']
 assert 'daily update is only using tushare data source' in docs['community_eod'] and 'eod' in docs['community_readme']
 bao=Path(s['baostock_home_path']).read_text(encoding='utf-8');section=bao.split('##### 数据范围说明',1)[1].split('####',1)[0]
 stock_section=section.split('股票数据',1)[1].split('ETF数据',1)[0];etf_section=section.split('ETF数据',1)[1].split('指数数据',1)[0]
 assert '近5年' in stock_section and '2020-01-03' in stock_section and '2026-01-05' in etf_section and '不提供分钟K线数据' in section
 release=json.loads(docs['community_release_metadata']);assets=release['assets']
 canonical=[a for a in assets if a['name']=='qlib_bin.tar.gz'];m=[a for a in assets if a['name']=='qlib_bin.manifest.json'];assert len(canonical)==len(m)==1
 canonical=canonical[0];m=m[0];mp=read(D/'community_manifest.plan.json');mr=read(D/'community_manifest.receipt.json');mv=read(D/'community_manifest.source')
 assert mr['saved'] and mr['status']==200 and mr['bytes']==m['size']==511 and mr['sha256']==sha(D/'community_manifest.source') and m['digest']=='sha256:'+mr['sha256']
 assert mv['release_tag']==release['tag_name'] and mv['archive_size_bytes']==canonical['size'] and mv['archive_sha256']==canonical['digest'] and mp['GET_cap']==1 and mp['archive_GETs']==0
 minute=read(R/'research/BAOSTOCK_MINUTE_PROBE_RESULT_20261003.json');assert minute['predicates']['minute_row_count']==0 and minute['predicates']['daily_row_count']==1
 def money(annual):return {'annual_upfront_CNY':annual,'monthly_equivalent_CNY':str((Decimal(annual)/12).quantize(Decimal('.01'))),'within_2400_annual_equivalent':annual<=2400,'includes_cloud_storage_or_live_feed':False,'purchased':False}
 return {
  'public_document_GETs':11,'market_API_calls':0,'dataset_archive_GETs':0,'SDK_executed':False,'credential_read':False,
  'baostock':{'stock_minute_advertised_start':'2020-01-03','stock_minute_advertised_retention_label':'近5年','ETF_day_and_minute_advertised_start':'2026-01-05','supported_minute_frequencies':[5,15,30,60],'index_minute_advertised':False,'retention_label_not_recomputed_as_2021':True,'old_2012_2013_update_log_not_current_coverage_certification':True,'actual_fixed_training_date':'2013-01-04','actual_five_minute_rows':0,'actual_daily_rows':1,'current_range_not_actually_probed':True,'complete_2013_2020_training_or_ETF_recovered':False},
  'qlib_official':{'current_notice_dataset_disabled':True,'old_one_minute_command_still_present':True,'download_not_attempted_or_bypassed':True,'MIT_software_not_market_data_grant':True},
  'community':{'advertised_final_price_pipeline':'end_of_day','contributors_named_in_docs':['Wind','Caihui','Tushare','Akshare','Yahoo','Baostock'],'software_license':'Apache-2.0','underlying_provider_redistribution_rights_certified':False,'release_tag':release['tag_name'],'release_published_utc':release['published_at'],'archive_size_bytes':canonical['size'],'archive_sha256':canonical['digest'],'manifest_bytes':511,'manifest_sha256':mr['sha256'],'provenance_matches_release_metadata':True,'target_trade_date':mv['target_trade_date'],'future_calendar_start':mv['future_start_date'],'future_calendar_end':mv['future_end_date'],'archive_downloaded_or_members_verified':False,'actual_frequency_coverage_or_PIT_certified':False,'one_minute_replacement_admitted':False},
  'tushare':{'advertised_stock_minute_start_year':2009,'stock_API':'stk_mins','ETF_API':'etf_mins','stock_and_ETF_documented_fields':stock_fields,'ETF_fields_match':stock_fields==etf_fields,'documented_units':{'vol':'shares','amount':'CNY'},'listed_bid_ask_or_side_size_fields':False,'exact_shared_stock_ETF_entitlement_confirmed':False,'actual_endpoint_or_coverage_probed':False,'historic_only':money(2000),'historic_plus_optional_2000_points':money(2200),'historic_plus_optional_5000_points':money(2500),'live_minute_monthly_CNY':1000,'live_minute_within_budget':False,'5000_points_not_automatically_required_for_every_baseline':True,'minute_study_research_statement':True,'service_personal_viewing_noncommercial_statement':True,'long_retention_AI_processing_cloud_display_rights_certified':False,'existing_consultation_repolled':False},
  'decision':{'no_current_source_admitted_for_complete_historical_minute_execution':True,'minute_bars_do_not_supply_frozen_M24_historical_side_size':True,'no_contract_threshold_date_frequency_window_or_tolerance_relaxed':True,'no_new_economic_trial':True,'original_native_E49_failure_preserved':True,'model_admitted':False,'full_goal_complete':False}
 }
def main():
 s=read(P);rebuilt=findings(s)
 if '--verify' in sys.argv:
  r=read(O);assert r['findings']==rebuilt and r['protocol_sha256']==sha(P) and r['implementation_sha256']==sha(__file__);print(json.dumps({'actual_offline_rebuild':True,'public_document_GETs':11,'original_empty_training_minute_preserved':True,'model_admitted':False}));return
 assert '--build' in sys.argv and read(O)['state']=='not_run'
 r={'schema':'regis.source_coverage_result.v1','state':'actual_primary_documents_and_release_metadata_checked','observed_utc':datetime.now(timezone.utc).isoformat(),'protocol_sha256':sha(P),'implementation_sha256':sha(__file__),'findings':rebuilt,'native_corpus_only':True,'source_classes_activated':0,'new_economic_runs':0,'model_admitted':False,'full_goal_complete':False}
 O.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n');print(json.dumps({'state':r['state'],'document_GETs':11,'stock_minute_start':rebuilt['baostock']['stock_minute_advertised_start'],'ETF_minute_start':rebuilt['baostock']['ETF_day_and_minute_advertised_start'],'raw_archives':0,'economic_runs':0}))
if __name__=='__main__':main()
