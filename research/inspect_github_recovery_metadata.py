"""Check limited GitHub metadata discovery; absence in this window is not global absence."""
import hashlib,json
from pathlib import Path
R=Path(__file__).resolve().parents[1]
P=R/'research/GITHUB_RECOVERY_METADATA_PROTOCOL_20261002.json'
S=R/'research/GITHUB_RECOVERY_METADATA_RECEIPT_20261002.json'
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def raw_result(path,base,size,digest,cap=None):
 raw=Path(path);assert raw.resolve().parent==Path(base).resolve()
 assert 'onedrive' not in str(raw.resolve()).lower() and raw.stat().st_size==size
 assert sha(raw)==digest and (cap is None or size<=cap)
 return json.loads(raw.read_bytes())
def main():
 p=json.loads(P.read_text(encoding='utf-8'));s=json.loads(S.read_text(encoding='utf-8'))
 assert s['protocol_sha256']==sha(P) and s['repository']==p['repository']
 assert len(s['calls'])<=p['max_tool_invocations']==5
 assert s['raw_or_binary_downloads']==s['job_log_reads']==s['secret_or_account_api_reads']==s['outbound_messages']==0
 assert s['data_admitted'] is s['economic_run'] is s['model_admitted'] is s['all_GitHub_originals_absent_certified'] is False
 for i,c in enumerate(s['calls']):
  if i<2:assert c['tool']==p['initial_calls'][i]['tool'] and c['args']==p['initial_calls'][i]['args']
  else:
   assert c['tool']=='mcp__codex_apps__github_fetch_workflow_run_artifacts'
   assert c['args']['repo_full_name']==p['repository'] and c['args']['run_id'] in s['selected_run_ids']
  payload=raw_result(c['raw_result_path'],p['raw_directory'],c['raw_result_bytes'],c['raw_result_sha256'])
  assert payload.get('isError',False)==c['tool_is_error']
  if c['tool_is_error']:
   assert c['parse_succeeded'] is False and c['failure_class']=='connector_error_not_empty_collection'
 assert len(s['selected_run_ids'])<=3 and len(set(s['selected_run_ids']))==len(s['selected_run_ids'])
 np=R/'research/GITHUB_EXISTING_AUTH_METADATA_PROTOCOL_20261002.json'
 ns=R/'research/GITHUB_EXISTING_AUTH_METADATA_RECEIPT_20261002.json'
 n=json.loads(np.read_text(encoding='utf-8'));v=json.loads(ns.read_text(encoding='utf-8'))
 assert n['ancestor_receipt_sha256']==sha(S)==v['ancestor_receipt_sha256']
 assert v['protocol_sha256']==sha(np) and v['repository']==n['repository']==p['repository']
 assert [x['url'] for x in n['objects']]==[x['args']['url'] for x in p['initial_calls']]
 assert len(v['requests'])<=n['max_native_GETs']==2
 assert v['retries']==n['retries']==v['redirects']==n['redirects']==0
 assert v['new_login_or_privilege_change'] is v['credentials_to_files_AI_logs_or_Git'] is False
 assert v['account_secret_log_raw_payload_reads']==0
 assert v['data_admitted'] is v['economic_run'] is v['model_admitted'] is False
 for i,row in enumerate(v['requests']):
  assert row['url']==n['objects'][i]['url'] and row['collection']==n['objects'][i]['collection']
  if i:assert not v['requests'][i-1].get('failure'),'request after stop condition'
  if not row.get('complete_within_cap'):
   assert row.get('failure') or v.get('process_timeout');assert 'collection_count' not in row
   continue
  payload=raw_result(row['raw_path'],n['raw_directory'],row['raw_bytes'],row['raw_sha256'],n['byte_cap_per_object'])
  if row['status']!=200:
   assert row['failure']=='HTTP_not_200' and 'collection_count' not in row
   continue
  if row.get('failure'):
   assert 'collection_count' not in row
   continue
  if row['collection']=='releases':
   assert isinstance(payload,list) and row['collection_count']==len(payload)
   assets=[{'id':a['id'],'name':a['name'],'size':a['size']} for x in payload for a in x.get('assets',[])]
   assert row['asset_names']==assets and row['release_assets_observed']==len(assets)
  else:
   assert isinstance(payload,dict) and isinstance(payload.get('workflow_runs'),list)
   assert row['collection_count']==len(payload['workflow_runs']) and row['total_count']==payload.get('total_count')
   assert row['runs_observed']==[{k:x.get(k) for k in ['id','name','path','created_at','head_sha','status','conclusion']} for x in payload['workflow_runs']]
 ap=R/'research/GITHUB_ARTIFACT_INVENTORY_PROTOCOL_20261002.json'
 ar=R/'research/GITHUB_ARTIFACT_INVENTORY_RECEIPT_20261002.json'
 a=json.loads(ap.read_text(encoding='utf-8'));av=json.loads(ar.read_text(encoding='utf-8'))
 assert a['ancestor_receipt_sha256']==sha(ns)==av['ancestor_receipt_sha256']
 assert av['protocol_sha256']==sha(ap) and av['repository']==a['repository']==p['repository']
 assert a['objects']==[{'collection':'repository_artifacts','url':'https://api.github.com/repos/'+p['repository']+'/actions/artifacts?per_page=100&page=1'}]
 assert len(av['requests'])<=a['max_native_GETs']==1
 assert av['retries']==a['retries']==av['redirects']==a['redirects']==0
 assert av['new_login_or_privilege_change'] is av['credentials_to_files_AI_logs_or_Git'] is False
 assert av['account_secret_log_raw_payload_reads']==0 and av['data_admitted'] is av['economic_run'] is av['model_admitted'] is False
 for row in av['requests']:
  assert row['url']==a['objects'][0]['url'] and row['collection']=='repository_artifacts'
  if not row.get('complete_within_cap'):
   assert row.get('failure') or av.get('process_timeout');assert 'collection_count' not in row
   continue
  payload=raw_result(row['raw_path'],a['raw_directory'],row['raw_bytes'],row['raw_sha256'],a['byte_cap_per_object'])
  if row['status']!=200:
   assert row['failure']=='HTTP_not_200' and 'collection_count' not in row
   continue
  if row.get('failure'):
   assert 'collection_count' not in row
   continue
  assert isinstance(payload,dict) and isinstance(payload.get('artifacts'),list)
  assert row['collection_count']==len(payload['artifacts']) and row['total_count']==payload.get('total_count')
  assert row['artifacts_observed']==[{k:x.get(k) for k in ['id','name','size_in_bytes','expired','created_at','updated_at','expires_at','digest','workflow_run']} for x in payload['artifacts']]
 print(json.dumps({'bounded_metadata_receipt_verified':True,'actual_app_tool_invocations':len(s['calls']),'app_calls_with_parsed_collections':sum(c.get('parse_succeeded',False) for c in s['calls']),'actual_native_GETs':len(v['requests'])+len(av['requests']),'native_statuses':[x.get('status') for x in v['requests']+av['requests']],'native_collection_counts':[x.get('collection_count') for x in v['requests']+av['requests']],'native_failure':v.get('failure') or av.get('failure'),'repository_artifact_total_count':av['requests'][0].get('total_count') if av['requests'] else None,'candidate_original_payloads_downloaded':0,'data_admitted':False,'economic_run':False,'partial_window_only':True}))
if __name__=='__main__':main()
