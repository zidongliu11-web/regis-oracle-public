"""One connected owner-filtered repo directory page, metadata only."""
import hashlib,json,sys
from datetime import datetime,timezone
from pathlib import Path
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R))
P=R/'research/OWNER_REPO_METADATA_PROTOCOL_20261003.json';O=R/'research/OWNER_REPO_METADATA_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def gate(s):
 from scripts.workflow_guard import contract_hash
 t=read(R/'WORKFLOW_TASK.json');g=s['execution_gate'];idx={a['path']:a['sha256'] for a in t['artifacts']}
 assert t['status']=='in_progress' and t['id']==s['parent_task'] and t['owner_session']==g['owner_session'] and t['contract_sha256']==g['expected_contract_sha256']==contract_hash(t)
 assert len(t['artifacts'])==935 and len(t['checks'])==76 and read(P)==s
 assert sha(P)==idx[P.relative_to(R).as_posix()] and sha(__file__)==idx[Path(__file__).resolve().relative_to(R).as_posix()]
 assert {p:sha(R/p) for p in s['input_hashes']}==s['input_hashes']
 assert s['tool_name']=='mcp__codex_apps__github_list_repositories' and s['tool_args']=={'owner':'zidongliu11-web','page_size':100,'page_offset':0,'include_search_index_status':False}
 return t
def parse(raw,s):
 roots=[]
 if isinstance(raw,dict):
  if raw.get('isError') or raw.get('actual_tool_result_absent') or raw.get('metadata_result_character_cap_exceeded'):return {'parse_succeeded':False,'failure_class':'connector_error_or_missing_or_oversized_response_not_empty_collection','repos_observed':None}
  if raw.get('structuredContent') is not None:roots.append(raw['structuredContent'])
  for item in raw.get('content',[]):
   if item.get('type')=='text':
    try:roots.append(json.loads(item['text']))
    except (ValueError,TypeError):pass
 else:roots.append(raw)
 def collection(v,depth=0):
  if depth>4:return None
  if isinstance(v,list):return v
  if isinstance(v,dict):
   for key in ['repositories','repos','items','data','result']:
    if key in v:
     z=collection(v[key],depth+1)
     if z is not None:return z
  return None
 rows=next((z for root in roots if (z:=collection(root)) is not None),None)
 if rows is None or len(rows)>100:return {'parse_succeeded':False,'failure_class':'unrecognized_or_over_cap_collection_not_empty_result','repos_observed':None}
 owned=[];unresolved=0;outside=0
 for row in rows:
  if not isinstance(row,dict):unresolved+=1;continue
  fullname=row.get('full_name') or row.get('repository_full_name');owner=row.get('owner');owner=owner.get('login') if isinstance(owner,dict) else owner
  if not fullname and isinstance(owner,str) and isinstance(row.get('name'),str):fullname=owner+'/'+row['name']
  if not isinstance(fullname,str) or fullname.count('/')!=1:unresolved+=1;continue
  owner,name=fullname.split('/')
  if owner.lower()!=s['tool_args']['owner'].lower():outside+=1;continue
  if len(fullname)>256 or any(w in name.lower() for w in s['excluded_name_words']):unresolved+=1;continue
  owned.append({'full_name':fullname,'private':row.get('private'),'archived':row.get('archived'),'default_branch':row.get('default_branch'),'created_at':row.get('created_at'),'pushed_at':row.get('pushed_at'),'name_only_quant_keyword_candidate':any(w in name.lower() for w in s['candidate_name_words']) and fullname.lower()!=s['current_repository'].lower()})
 return {'parse_succeeded':True,'returned_collection_count':len(rows),'owner_matched_count':len(owned),'outside_owner_suppressed':outside,'unresolved_identity_count':unresolved,'repos_observed':owned,'other_quant_name_candidates':[z['full_name'] for z in owned if z['name_only_quant_keyword_candidate']],'page_limit_reached':len(rows)==100,'all_user_repositories_or_historical_data_absent_certified':False,'scope':'Only connected accessible owner-filtered first page; no code/body or uninstalled/private-inaccessible repo assertion','original_dataset_recovered':0,'new_economic_runs':0,'model_admitted':False,'full_goal_complete':False}
def parse_tests(s):
 error=parse({'isError':True,'content':[{'type':'text','text':'[]'}]},s);assert not error['parse_succeeded'] and error['repos_observed'] is None
 unknown=parse({'content':[{'type':'text','text':'unknown shape'}]},s);assert not unknown['parse_succeeded']
 empty=parse({'structuredContent':{'repositories':[]}},s);assert empty['parse_succeeded'] and empty['owner_matched_count']==0 and not empty['all_user_repositories_or_historical_data_absent_certified']
 outside=parse({'structuredContent':{'repositories':[{'full_name':'synthetic_other/quant-data'},{'full_name':'zidongliu11-web/quant-synthetic'},{'name':'missing-owner'}]}},s);assert outside['outside_owner_suppressed']==1 and outside['owner_matched_count']==1 and outside['unresolved_identity_count']==1
 return {'connector_error_unknown_shape_empty_page_outside_owner_missing_identity_boundaries':5,'network_calls_in_tests':0}
def main():
 s=read(P);D=Path(s['native_directory'])
 if '--prepare' in sys.argv:
  t=gate(s);assert read(O)['state']=='not_run' and 'onedrive' not in str(D.resolve()).lower() and not D.exists();D.mkdir()
  write(D/'frozen_protocol.json',s);write(D/'frozen_task.json',t);write(D/'ready.json',{'created_utc':datetime.now(timezone.utc).isoformat(),'protocol_sha256':sha(P),'implementation_sha256':sha(__file__),'task_sha256':sha(D/'frozen_task.json'),'tool_name':s['tool_name'],'tool_args':s['tool_args'],'issued_connector_calls_before_ready':0})
  print(json.dumps({'prepared_before_API':True,'native_directory':str(D),'tool_name':s['tool_name'],'tool_args':s['tool_args']}));return
 if '--ingest' in sys.argv:
  gate(s);assert read(O)['state']=='not_run';a=read(D/'ready.json');assert a['protocol_sha256']==sha(P) and a['implementation_sha256']==sha(__file__)
  rawpath=D/'raw_connector_result.json';assert rawpath.stat().st_size<=s['raw_result_byte_cap'];raw=read(rawpath);f=parse(raw,s)
  receipt={'recorded_utc':datetime.now(timezone.utc).isoformat(),'tool_name':s['tool_name'],'tool_args':s['tool_args'],'issued_connector_calls':1,'backend_HTTP_attempt_count':'unknown','raw_path':str(rawpath),'raw_bytes':rawpath.stat().st_size,'raw_sha256':sha(rawpath),'ready_sha256':sha(D/'ready.json'),'frozen_protocol_sha256':sha(D/'frozen_protocol.json'),'frozen_task_sha256':sha(D/'frozen_task.json'),'summary':f,'new_login_or_privilege_change':False,'secret_profile_broker_account_log_code_blob_or_file_body_reads':0,'retries_issued':0,'model_admitted':False};write(D/'receipt.json',receipt)
  r={'schema':'regis.owner_repo_metadata_result.v1','state':'actual_connected_owner_repo_metadata_only' if f['parse_succeeded'] else 'actual_connector_or_schema_failure_preserved','protocol_sha256':sha(P),'implementation_sha256':sha(__file__),'native_receipt_sha256':sha(D/'receipt.json'),'summary':f,'new_economic_runs':0,'model_admitted':False,'full_goal_complete':False};write(O,r);print(json.dumps({'state':r['state'],'summary':f}));return
 assert '--verify' in sys.argv;r=read(O);c=read(D/'receipt.json');a=read(D/'ready.json');t=read(D/'frozen_task.json');from scripts.workflow_guard import contract_hash
 assert r['protocol_sha256']==sha(P)==c['frozen_protocol_sha256']==a['protocol_sha256'] and r['implementation_sha256']==sha(__file__)==a['implementation_sha256']
 assert sha(D/'receipt.json')==r['native_receipt_sha256'] and sha(D/'raw_connector_result.json')==c['raw_sha256'] and sha(D/'ready.json')==c['ready_sha256'] and sha(D/'frozen_task.json')==c['frozen_task_sha256']==a['task_sha256']
 assert read(D/'frozen_protocol.json')==s and t['contract_sha256']==s['execution_gate']['expected_contract_sha256']==contract_hash(t)
 assert c['issued_connector_calls']==1 and c['retries_issued']==0 and c['tool_name']==s['tool_name'] and c['tool_args']==s['tool_args'] and c['raw_bytes']<=s['raw_result_byte_cap']
 assert r['summary']==c['summary']==parse(read(D/'raw_connector_result.json'),s) and c['secret_profile_broker_account_log_code_blob_or_file_body_reads']==0
 print(json.dumps({'native_metadata_evidence_verified':True,'parse_tests':parse_tests(s),'issued_connector_calls':1,'backend_HTTP_attempt_count':'unknown','model_admitted':False}))
if __name__=='__main__':
 try:main()
 except Exception as e:print(json.dumps({'error_type':type(e).__name__,'private_raw_not_emitted':True}));raise SystemExit(1)
