"""Primary historical L1 product/price documentation, no market data requests."""
import hashlib,json,re,sys
from pathlib import Path
from datetime import datetime,timezone
R=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(R))
from research.check_source_coverage import Tables
P=R/'research/HISTORICAL_L1_SOURCE_PROTOCOL_20261003.json'
O=R/'research/HISTORICAL_L1_SOURCE_RESULT_20261003.json'
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def read(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def rebuild(s):
 for p,h in s['input_hashes'].items():assert sha(R/p)==h,p
 for p,h in s['native_input_hashes'].items():assert sha(p)==h,p
 D=Path(s['native_directory']);m=read(D/'manifest.json');assert m['actual_GET_attempts']==4 and m['market_quote_calls']==m['dataset_archive_downloads']==0 and not m['credential_read']
 texts={};tables={};sources=[]
 for rec in m['receipts']:
  assert rec['attempts']==1 and not rec['auth_used']
  sources.append({k:rec.get(k) for k in ['id','url','status','bytes','sha256','saved','error_type']})
  if rec['id']=='szse_data_services':
   assert not rec['saved'] and rec['error_type']=='URLError' and rec.get('status') is None;continue
  assert rec['saved'] and rec['status']==200 and rec['within_byte_cap']
  path=D/(rec['id']+'.source');assert sha(path)==rec['sha256'] and path.stat().st_size==rec['bytes'];p=Tables();p.feed(path.read_text(encoding='utf-8'));texts[rec['id']]=' '.join(' '.join(p.text).split());tables[rec['id']]=p.tables
 h=texts['sse_historical'];l=texts['sse_level1'];prices=texts['sse_price']
 assert '3秒或5秒' in h and '相邻快照如果完全相同就保留第一幅' in h
 histrows=[r for table in tables['sse_historical'] for r in table if r and r[0]=='上证所Level-1行情'];assert len(histrows)==1 and len(histrows[0])==6
 # Publisher's icon marks three supported L1 products. Empty tick/call-auction cells stay empty.
 row=histrows[0];assert not row[1] and row[2] and not row[3] and row[4] and row[5]
 assert '实时最高五个买入申报价格和数量' in l and '实时最低五个卖出申报价格和数量' in l
 matches=[r for table in tables['sse_price'] for r in table if r and r[0]=='上证智能-历史数据'];assert len(matches)==1
 value=matches[0][1];assert re.search(r'Level-1\s*行情：3\s*万元/年',value) and re.search(r'Level-2\s*行情：12\s*万元/年',value) and '单购分钟 K 线定价为 2 万元/年' in value and '10 年次' in value
 assert '历史数据服务以数据接口形式提供给用户' in prices
 return {'actual_public_doc_GETs':4,'successful_GETs':3,'failed_GETs':1,'market_API_calls':0,'quote_arrays_acquired':False,'credentials_or_account_read':False,'sources':sources,
 'SSE':{'historical_L1_snapshot_product_advertised':True,'historical_L1_minute_daily_products_advertised':True,'L1_historical_tick_and_opening_call_auction_cells_empty':True,'snapshot_interval_seconds_advertised':[3,5],'identical_consecutive_snapshots_first_retained':True,'L1_product_five_bid_ask_price_and_size_fields_advertised':True,'historical_snapshot_exact_schema_years_SZSSE_coverage_or_sample_verified':False,'public_historical_L1_price_number_CNY':30000,'publisher_price_unit':'元/年','public_historical_L2_price_number_CNY':120000,'public_minute_only_price_number_CNY':20000,'annual_budget_equivalent_CNY':2400,'direct_catalog_price_number_exceeds_one_year_budget_equivalent':True,'subscription_vs_data_year_purchase_basis_or_proration_confirmed':False,'business_display_licence_150000_not_applied_to_personal_research':True,'retail_reseller_lower_bound_or_all_market_price_claimed':False,'cloud_AI_retention_or_non_display_grant_certified':False,'actual_source_sample_requested':False,'historic_fill_or_queue_proven':False},
 'SZSE':{'native_document_GET_failed_preserved':True,'failure_class':'URLError','native_http_status_observed':False,'primary_web_search_read_available':True,'search_read_not_native_GET_success':True,'historical_depth_schema_dates_fees_or_rights_certified':False,'alternate_endpoint_retry_or_contact':False},
 'decision':{'official_SH_historical_L1_route_identified_not_acquired_or_admitted':True,'no_budget_purchase_access_change_or_vendor_contact':True,'no_minutes_to_orderbook_substitution':True,'no_claim_all_budget_routes_impossible':True,'source_classes_activated':0,'new_economic_runs':0,'model_admitted':False,'full_goal_complete':False}}
def main():
 s=read(P);f=rebuild(s)
 if '--verify' in sys.argv:
  r=read(O);assert r['findings']==f and r['protocol_sha256']==sha(P) and r['implementation_sha256']==sha(__file__);print(json.dumps({'actual_offline_rebuild':True,'source_GETs':4,'native_failure_preserved':True,'raw_quote_requests':0,'model_admitted':False}));return
 assert '--build' in sys.argv and read(O)['state']=='not_run'
 r={'schema':'regis.historical_l1_source_result.v1','state':'actual_official_L1_product_catalog_route_not_admitted','observed_utc':datetime.now(timezone.utc).isoformat(),'protocol_sha256':sha(P),'implementation_sha256':sha(__file__),'findings':f,'source_classes_activated':0,'new_economic_runs':0,'model_admitted':False,'full_goal_complete':False}
 O.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n');print(json.dumps({'state':r['state'],'public_GETs':4,'successful_GETs':3,'failed_GETs':1,'direct_price_CNY_per_year_literal':30000,'quote_requests':0,'admission':False}))
if __name__=='__main__':main()
