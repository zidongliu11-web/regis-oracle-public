"""Validate a bounded first-party page/asset investigation, never admit market data."""
import hashlib,json
from pathlib import Path
from urllib.parse import urljoin,urlparse
R=Path(__file__).resolve().parents[1]
P=R/'research/SSE_PUBLIC_DOWNLOAD_LOCATOR_PROTOCOL_20261002.json'
S=R/'research/SSE_PUBLIC_DOWNLOAD_LOCATOR_RECEIPT_20261002.json'
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def main():
 p=json.loads(P.read_text(encoding='utf-8'));s=json.loads(S.read_text(encoding='utf-8'))
 assert s['protocol_sha256']==sha(P)
 assert p['max_requests']==4 and p['max_page_specific_scripts']==2 and p['retries']==p['redirects']==0
 assert s['retries']==s['redirects']==s['market_quote_requests']==s['account_or_order_requests']==0
 assert p['process_wall_limit_seconds']==55 and p['worker_wall_limit_seconds']==49
 assert p['byte_cap_per_object']==524288
 assert s['source_admitted'] is s['economic_run'] is s['model_admitted'] is s['raw_data_or_quote_values_to_AI'] is False
 assert s.get('public_download_dataset_obtained',False) is False
 rows=s['requests'];assert len(rows)<=4
 first=p['initial_objects'];assert len(first)==2
 observed=[];files=[]
 for i,row in enumerate(rows):
  assert row['role'] and row['url']
  if i<2:assert row['url']==first[i]['url'] and row['role']==first[i]['role']
  else:
   assert row['url'] in observed,'Script URL must have been actually observed, not invented'
   u=urlparse(row['url']);assert u.scheme=='https' and u.hostname in p['allowed_asset_hosts']
   assert u.path.lower().endswith('.js') and ('funddetail' in u.path.lower() or '/fundlist/' in u.path.lower())
  if row.get('raw_path'):
   raw=Path(row['raw_path']);assert raw.resolve().parent==Path(p['raw_directory']).resolve()
   assert 'onedrive' not in str(raw.resolve()).lower()
   assert row['status']==200 and row.get('failure') is None and row['complete_within_cap'] is True
   assert raw.stat().st_size==row['raw_bytes']<=p['byte_cap_per_object'] and sha(raw)==row['raw_sha256']
  observed+=row.get('script_urls_observed',[]);files+=row.get('explicit_file_download_urls_observed',[])
 body_count=sum(bool(r.get('raw_sha256')) for r in rows)
 ep=R/'research/SSE_OBSERVED_PROFILE_HANDLER_PROTOCOL_20261002.json'
 er=R/'research/SSE_OBSERVED_PROFILE_HANDLER_RECEIPT_20261002.json'
 extra=json.loads(ep.read_text(encoding='utf-8'));result=json.loads(er.read_text(encoding='utf-8'))
 assert extra['ancestor_receipt_sha256']==sha(S)==result['ancestor_receipt_sha256']
 assert result['protocol_sha256']==sha(ep) and result['url']==extra['url'] and extra['url'] in observed
 assert extra['max_requests']==1 and extra['aggregate_native_cap_with_ancestor']==4
 assert result['requests_initiated']==1 and len(rows)+result['requests_initiated']<=4
 assert result['retries']==result['redirects']==result['market_quote_account_order_calls']==0
 assert result['source_admitted'] is result['economic_run'] is result['model_admitted'] is False
 assert extra['public_data_or_paid_API_admission'] is False and extra['allowed_host']=='etf.sse.com.cn'
 if result.get('raw_sha256'):
  raw=Path(extra['raw_path']);assert raw.resolve().parent==Path(p['raw_directory']).resolve()
  assert result['status']==200 and not result.get('failure') and result['body_complete_within_cap'] is True
  assert raw.stat().st_size==result['raw_bytes']<=extra['byte_cap']==524288 and sha(raw)==result['raw_sha256']
  body_count+=1
 print(json.dumps({'bounded_request_receipt_verified':True,'requests_initiated':len(rows)+result['requests_initiated'],'raw_doc_asset_bodies':body_count,'observed_explicit_file_links':len(files),'all_required_initial_bodies_captured':s.get('all_required_initial_bodies_captured',False),'public_download_dataset_obtained':False,'source_admitted':False,'economic_run':False,'not_a_data_quality_or_rights_admission':True}))
if __name__=='__main__':main()
