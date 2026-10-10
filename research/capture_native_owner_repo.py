"""One repo-directory GET with existing noninteractive Git credential context."""
import hashlib,json,os,subprocess,sys,time
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R))
P=R/'research/NATIVE_OWNER_REPO_PROTOCOL_20261003.json';O=R/'research/NATIVE_OWNER_REPO_RESULT_20261003.json'
URL='https://api.github.com/user/repos?affiliation=owner&per_page=100&page=1&sort=created&direction=desc'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def project(data,s):
 if not isinstance(data,list) or len(data)>100:raise ValueError('metadata_shape_or_cap')
 owned=[];unresolved=0;outside=0
 for row in data:
  fullname=row.get('full_name') if isinstance(row,dict) else None
  if not isinstance(fullname,str) or fullname.count('/')!=1:unresolved+=1;continue
  owner,name=fullname.split('/')
  if owner.lower()!=s['owner_filter'].lower():outside+=1;continue
  if len(fullname)>256 or any(w in name.lower() for w in s['excluded_name_words']):unresolved+=1;continue
  owned.append({k:row.get(k) for k in ['full_name','private','archived','default_branch','created_at','pushed_at']})
 candidates=[z['full_name'] for z in owned if z['full_name'].lower()!=s['current_repository'].lower() and any(w in z['full_name'].split('/')[1].lower() for w in s['candidate_name_words'])]
 return {'returned_repo_count':len(data),'owner_matched_count':len(owned),'outside_owner_suppressed':outside,'unresolved_identity_count':unresolved,'repos_observed':owned,'other_quant_name_candidates':candidates,'full_user_repository_or_original_data_absence_certified':False,'original_dataset_recovered':0,'new_economic_runs':0,'model_admitted':False}
def gate(s):
 from scripts.workflow_guard import contract_hash
 t=read(R/'WORKFLOW_TASK.json');g=s['execution_gate'];idx={a['path']:a['sha256'] for a in t['artifacts']}
 assert t['status']=='in_progress' and t['id']==s['parent_task'] and t['owner_session']==g['owner_session'] and t['contract_sha256']==g['expected_contract_sha256']==contract_hash(t)
 assert len(t['artifacts'])==938 and len(t['checks'])==77 and read(P)==s and s['url']==URL
 assert sha(P)==idx[P.relative_to(R).as_posix()] and sha(__file__)==idx[Path(__file__).resolve().relative_to(R).as_posix()]
 assert {p:sha(R/p) for p in s['input_hashes']}==s['input_hashes']
 return t
def persist_terminal(s,D,c):
 errors=[]
 def attempt(label,action):
  try:return True,action()
  except Exception as e:errors.append({'stage':label,'error_type':type(e).__name__});return False,None
 c['terminal_recording_errors']=errors;attempt('receipt_write',lambda:write(D/'receipt.json',c))
 hs={key:attempt(key,lambda path=path:sha(path))[1] for key,path in [('protocol_sha256',P),('implementation_sha256',Path(__file__)),('native_receipt_sha256',D/'receipt.json'),('native_protocol_sha256',D/'protocol.json'),('native_task_sha256',D/'frozen_task.json')]}
 failed=c['failure_type'] is not None or bool(c.get('session_cleanup_error')) or bool(errors)
 r=dict(schema='regis.native_owner_repo_result.v1',state='actual_native_metadata_failure_preserved' if failed else 'actual_existing_Git_auth_repo_metadata_only',**hs,summary=c.get('summary'),failure_type=c['failure_type'],terminal_recording_errors=errors,new_economic_runs=0,model_admitted=False,full_goal_complete=False)
 if not attempt('result_write',lambda:write(O,r))[0]:
  r['state']='actual_native_metadata_failure_preserved';attempt('fallback_write',lambda:write(D/'terminal_failure.json',r))
 return r
def terminal_failure_tests():
 ow,oh=globals()['write'],globals()['sha'];count=0
 try:
  for failed in ['receipt.json','sha_read',O.name]:
   seen=[];saved={}
   def sink(path,value):
    seen.append(Path(path).name)
    if Path(path).name==failed:raise PermissionError('synthetic_record_failure')
    saved[Path(path).name]=value
   def hash_sink(path):
    if failed=='sha_read':raise PermissionError('synthetic_hash_failure')
    return 'f'*64
   globals()['write'],globals()['sha']=sink,hash_sink
   r=persist_terminal({},Path('synthetic_native_directory'),{'failure_type':None})
   assert r['state']=='actual_native_metadata_failure_preserved' and 'receipt.json' in seen and O.name in seen and r['terminal_recording_errors']
   if failed==O.name:assert 'terminal_failure.json' in saved
   count+=1
 finally:globals()['write'],globals()['sha']=ow,oh
 return {'receipt_hash_result_failures_remain_failed_and_all_records_attempted':count,'credential_GET_process_file_calls_in_tests':0}
def capture(s):
 t=gate(s);assert read(O)['state']=='not_run';D=Path(s['native_directory']);assert 'onedrive' not in str(D.resolve()).lower() and not D.exists();D.mkdir()
 write(D/'protocol.json',s);write(D/'frozen_task.json',t)
 receipt={'started_utc':datetime.now(timezone.utc).isoformat(),'url':URL,'native_GETs_initiated':0,'retries':0,'redirects':0,'status':None,'failure_type':None,'script_explicit_login_or_privilege_mutations':0,'credential_helper_internal_refresh_or_store_behavior':'unverified','credentials_to_files_AI_logs_or_Git':False,'broker_account_profile_setting_secret_log_code_file_body_API_reads':0,'scope':'One repo directory GET; select owner/name/project metadata only, not profile/account APIs or code/data bodies'}
 start=time.monotonic();env=dict(os.environ,GIT_TERMINAL_PROMPT='0',GCM_INTERACTIVE='never');flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0;session=None;credentials={};token=None;auth=None
 try:
  remote=subprocess.run(['git','remote','get-url','origin'],cwd=R,env=env,capture_output=True,timeout=10,creationflags=flags);assert remote.returncode==0
  u=urlsplit(remote.stdout.decode().strip());assert u.scheme=='https' and u.hostname=='github.com' and u.path.lstrip('/').removesuffix('.git')==s['current_repository']
  context=('protocol=https\nhost=github.com\npath='+s['current_repository']+'.git\n\n').encode()
  auth=subprocess.run(['git','credential','fill'],cwd=R,env=env,input=context,capture_output=True,timeout=10,creationflags=flags);assert auth.returncode==0 and len(auth.stdout)<=4096
  credentials=dict(line.split('=',1) for line in auth.stdout.decode().splitlines() if '=' in line);token=credentials.get('password');assert token
  import requests
  session=requests.Session();session.headers.update({'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2026-03-10'})
  receipt['native_GETs_initiated']=1;write(D/'receipt.json',receipt)
  data=bytearray()
  with session.get(URL,stream=True,allow_redirects=False,timeout=(6,6)) as response:
   receipt['status']=response.status_code;receipt['has_next_page_link']='rel="next"' in response.headers.get('Link','');receipt['headers']={k:response.headers.get(k) for k in ['Content-Type','Content-Length','Date','ETag','X-GitHub-Request-Id']}
   for chunk in response.iter_content(8192):
    assert time.monotonic()-start<=47 and len(data)+len(chunk)<=s['raw_byte_cap'];data.extend(chunk)
  raw=D/'metadata_response.json';raw.write_bytes(data);receipt.update(raw_path=str(raw),raw_bytes=len(data),raw_sha256=sha(raw));assert receipt['status']==200
  receipt['summary']=project(json.loads(data),s)
 except Exception as e:receipt['failure_type']=type(e).__name__
 finally:
  if session:
   try:session.close()
   except Exception as e:receipt['session_cleanup_error']=type(e).__name__
  credentials.clear();token=None;auth=None
  receipt['ended_utc']=datetime.now(timezone.utc).isoformat();receipt['elapsed_seconds']=round(time.monotonic()-start,3);r=persist_terminal(s,D,receipt)
 print(json.dumps({'state':r['state'],'status':receipt['status'],'native_GETs_initiated':receipt['native_GETs_initiated'],'summary':r['summary'],'failure_type':r['failure_type']}))
def main():
 s=read(P)
 if '--capture' in sys.argv:capture(s);return
 assert '--verify' in sys.argv;r=read(O);D=Path(s['native_directory']);c=read(D/'receipt.json');t=read(D/'frozen_task.json');from scripts.workflow_guard import contract_hash
 assert sha(P)==r['protocol_sha256']==sha(D/'protocol.json') and read(D/'protocol.json')==s and sha(__file__)==r['implementation_sha256'] and sha(D/'receipt.json')==r['native_receipt_sha256'] and sha(D/'frozen_task.json')==r['native_task_sha256']
 assert t['contract_sha256']==s['execution_gate']['expected_contract_sha256']==contract_hash(t) and c['url']==URL and c['native_GETs_initiated']<=1 and c['retries']==0 and c['redirects']==0 and not c['credentials_to_files_AI_logs_or_Git']
 if r['state']=='actual_existing_Git_auth_repo_metadata_only':
  assert c['status']==200 and c['failure_type'] is None and not r['terminal_recording_errors'] and not c['terminal_recording_errors'] and sha(c['raw_path'])==c['raw_sha256'] and c['raw_bytes']<=s['raw_byte_cap'];assert c['summary']==r['summary']==project(read(c['raw_path']),s)
 else:assert r['state']=='actual_native_metadata_failure_preserved' and (c['failure_type'] or c.get('session_cleanup_error') or r['terminal_recording_errors'])
 assert project([],s)['owner_matched_count']==0 and not project([],s)['full_user_repository_or_original_data_absence_certified']
 assert project([{'full_name':'synthetic_other/quant-data'}],s)['outside_owner_suppressed']==1
 assert c['script_explicit_login_or_privilege_mutations']==0 and c['credential_helper_internal_refresh_or_store_behavior']=='unverified'
 print(json.dumps({'native_method_failure_or_metadata_evidence_verified':True,'native_GETs':c['native_GETs_initiated'],'credential_values_not_recorded':True,'helper_internal_refresh_store_unverified':True,'terminal_failure_tests':terminal_failure_tests(),'original_dataset_recovered':0,'model_admitted':False}))
if __name__=='__main__':
 try:main()
 except Exception as e:print(json.dumps({'error_type':type(e).__name__,'credential_or_private_response_not_emitted':True}));raise SystemExit(1)
