"""One tree metadata request for each of three previously observed owned repository branches."""
import hashlib,json,os,re,subprocess,sys,time
from pathlib import Path
from datetime import datetime,timezone
from urllib.parse import urlsplit
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R))
P=R/'research/OWNER_OTHER_TREE_PROTOCOL_20261003.json';O=R/'research/OWNER_OTHER_TREE_RESULT_20261003.json'
REPOS=['codex-agent-notes','douyin-suibi','vex-override-97779s-research']
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def hexsha(x):return isinstance(x,str) and re.fullmatch('[0-9a-f]{40}',x) is not None
def summarize(raw,status,complete):
 out={'HTTP_status':status,'captured_bytes':len(raw),'complete_under_application_cap':complete,'valid_tree_metadata':False,'full_all_branch_history_or_old_data_absence_certified':False,'original_dataset_recovered':0}
 if status!=200 or not complete:return out
 try:j=json.loads(raw.decode('utf-8-sig'))
 except (ValueError,UnicodeError):return out
 if not isinstance(j,dict) or not hexsha(j.get('sha')) or not isinstance(j.get('truncated'),bool) or not isinstance(j.get('tree'),list) or len(j['tree'])>5000:return out
 paths=set();blobs=valid=large=formats=markers=empty=links=0
 for row in j['tree']:
  if not isinstance(row,dict):continue
  path=row.get('path');kind=row.get('type');size=row.get('size')
  if not isinstance(path,str) or not path or len(path)>2048 or path.startswith('/') or '\x00' in path or any(x in ('','..','.') for x in path.split('/')) or path in paths or kind not in ('blob','tree','commit') or not hexsha(row.get('sha')):continue
  if kind=='blob' and (not isinstance(size,int) or isinstance(size,bool) or size<0):continue
  paths.add(path);valid+=1
  if kind=='blob':
   blobs+=1;empty+=size==0;large+=size>=1048576;links+=row.get('mode')=='120000'
   lower=path.lower();formats+=any(lower.endswith(x) for x in ('.parquet','.h5','.hdf5','.sqlite','.sqlite3','.db','.csv','.csv.gz','.zip','.7z'))
   markers+=any(x in lower for x in ('regis-oracle','factor_system','registry.db','pit_panel','quantqc','m24_accounting'))
 out.update(tree_sha=j['sha'],entries=len(j['tree']),valid_entries=valid,blobs=blobs,empty_blobs=empty,at_least_1MiB_blobs=large,extension_only_data_or_archive_candidates=formats,project_marker_path_candidates=markers,symlink_blobs=links,truncated=j['truncated'],valid_tree_metadata=valid==len(j['tree']),complete_declared_single_tree=valid==len(j['tree']) and not j['truncated'],candidate_extension_is_quant_data_proof=False,file_or_blob_content_reads=0)
 return out
def inputs(s):assert {p:sha(R/p) for p in s['input_hashes']}==s['input_hashes'] and {p:sha(p) for p in s['native_input_hashes']}==s['native_input_hashes']
def gate(s):
 from scripts.workflow_guard import contract_hash
 t=read(R/'WORKFLOW_TASK.json');idx={a['path']:a['sha256'] for a in t['artifacts']}
 assert t['id']==s['parent_task'] and t['status']=='in_progress' and t['owner_session']==s['execution_gate']['owner_session'] and t['contract_sha256']==s['execution_gate']['expected_contract_sha256']==contract_hash(t)
 assert read(P)==s and sha(P)==idx[P.relative_to(R).as_posix()] and sha(__file__)==idx[Path(__file__).resolve().relative_to(R).as_posix()]
 assert [x['repo'] for x in s['specs']]==REPOS and all(x['branch']=='main' and x['url']=='https://api.github.com/repos/zidongliu11-web/'+x['repo']+'/git/trees/main?recursive=1' for x in s['specs'])
 assert s['method_GET_cap']==3 and s['byte_cap_per_response']==1048576 and s['file_or_blob_content_reads']==0;inputs(s);return t
def persist(D,c):
 errors=[]
 def attempt(label,fn):
  try:v=fn();return True if v is None else v
  except Exception as e:errors.append({'stage':label,'error_type':type(e).__name__});return None
 c['terminal_recording_errors']=errors;attempt('receipt_write',lambda:write(D/'receipt.json',c))
 hs={k:attempt(k,lambda p=p:sha(p)) for k,p in [('protocol_sha256',P),('implementation_sha256',Path(__file__)),('native_protocol_sha256',D/'protocol.json'),('native_task_sha256',D/'frozen_task.json'),('native_receipt_sha256',D/'receipt.json')]}
 r={'schema':'regis.owner_other_tree_result.v1','state':'actual_bounded_owner_tree_metadata' if not errors and not c.get('failure_type') else 'actual_failure_preserved',**hs,'method_GETs':c['method_GETs'],'summaries':[{k:e.get(k) for k in ['repo','summary','failure_type']} for e in c['entries']],'failure_type':c.get('failure_type'),'terminal_recording_errors':errors,'new_economic_runs':0,'model_admitted':False,'full_goal_complete':False,'file_or_blob_content_reads':0}
 if attempt('result_write',lambda:write(O,r)) is None:r['state']='actual_failure_preserved';attempt('fallback_write',lambda:write(D/'terminal_failure.json',r))
 return r
def begin_get(session,url,c,D):
 get=session.get;write(D/'receipt.json',c);c['method_GETs']+=1
 return get(url,stream=True,allow_redirects=False,timeout=(6,6))
def capture(s):
 t=gate(s);assert read(O)['state']=='not_run';D=Path(s['native_directory']);assert not D.exists() and 'onedrive' not in str(D.resolve()).lower()
 c={'started_utc':datetime.now(timezone.utc).isoformat(),'method_GETs':0,'entries':[],'file_or_blob_content_reads':0,'raw_or_credentials_to_AI_Git_cloud':False,'credential_helper_internal_refresh_or_store':'unverified','explicit_new_login_or_permission_mutations':0};start=time.monotonic();session=response=auth=token=None;credentials={}
 try:
  D.mkdir();write(D/'protocol.json',s);write(D/'frozen_task.json',t)
  env=dict(os.environ,GIT_TERMINAL_PROMPT='0',GCM_INTERACTIVE='never');flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
  remote=subprocess.run(['git','remote','get-url','origin'],cwd=R,env=env,capture_output=True,timeout=10,creationflags=flags);assert remote.returncode==0
  u=urlsplit(remote.stdout.decode().strip());assert u.scheme=='https' and u.hostname=='github.com' and u.username is None and u.password is None and u.path.lstrip('/').removesuffix('.git')=='zidongliu11-web/regis-oracle'
  context=b'protocol=https\nhost=github.com\npath=zidongliu11-web/regis-oracle.git\n\n'
  auth=subprocess.run(['git','credential','fill'],cwd=R,env=env,input=context,capture_output=True,timeout=10,creationflags=flags);assert auth.returncode==0 and len(auth.stdout)<=4096
  credentials=dict(line.split('=',1) for line in auth.stdout.decode().splitlines() if '=' in line);token=credentials.get('password');assert token
  import requests
  session=requests.Session();session.trust_env=False;session.mount('https://',requests.adapters.HTTPAdapter(max_retries=0));session.headers.update({'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2026-03-10'})
  for i,spec in enumerate(s['specs']):
   e={'repo':spec['repo'],'summary':None,'failure_type':None};c['entries'].append(e);data=bytearray();complete=False;response=None
   try:
    session.cookies.clear();response=begin_get(session,spec['url'],c,D);e['status']=response.status_code
    for chunk in response.iter_content(8192):
     if time.monotonic()-start>70:raise TimeoutError('application_elapsed_limit')
     remaining=s['byte_cap_per_response']+1-len(data);data.extend(chunk[:remaining])
     if len(data)>s['byte_cap_per_response']:raise ValueError('application_byte_cap_exceeded')
    complete=True
   except Exception as exc:e['failure_type']=type(exc).__name__
   finally:
    if response is not None:
     try:response.close();response=None
     except Exception as exc:e['cleanup_error_type']=type(exc).__name__;c['failure_type']=c.get('failure_type') or type(exc).__name__
   if data:
    p=D/('tree_'+str(i)+'.json.bin');p.write_bytes(data);e.update(raw_path=str(p),raw_sha256=sha(p))
   e.update(complete=complete,summary=summarize(bytes(data),e.get('status'),complete));write(D/'receipt.json',c)
   if e.get('status') in (401,403) or e.get('cleanup_error_type'):break
 except Exception as exc:c['failure_type']=type(exc).__name__
 finally:
  if response is not None:
   try:response.close()
   except Exception as exc:c['response_final_cleanup_error_type']=type(exc).__name__;c['failure_type']=c.get('failure_type') or type(exc).__name__
  if session is not None:
   for label,action in [('headers',lambda:session.headers.clear()),('cookies',lambda:session.cookies.clear()),('close',lambda:session.close())]:
    try:action()
    except Exception as exc:c.setdefault('session_cleanup_errors',[]).append({'stage':label,'error_type':type(exc).__name__});c['failure_type']=c.get('failure_type') or type(exc).__name__
  credentials.clear();token=auth=session=response=None
 c.update(ended_utc=datetime.now(timezone.utc).isoformat(),elapsed_seconds=round(time.monotonic()-start,3));r=persist(D,c);print(json.dumps({'state':r['state'],'method_GETs':r['method_GETs'],'summaries':r['summaries'],'failure_type':r['failure_type'],'original_dataset_recovered':0}))
def verify(s):
 inputs(s);r=read(O);D=Path(s['native_directory']);c=read(D/'receipt.json');t=read(D/'frozen_task.json');from scripts.workflow_guard import contract_hash
 assert r['implementation_sha256']==sha(__file__) and r['protocol_sha256']==sha(P)==r['native_protocol_sha256']==sha(D/'protocol.json') and read(D/'protocol.json')==s
 assert r['native_receipt_sha256']==sha(D/'receipt.json') and r['native_task_sha256']==sha(D/'frozen_task.json') and t['contract_sha256']==s['execution_gate']['expected_contract_sha256']==contract_hash(t)
 assert r['method_GETs']==c['method_GETs']<=3 and len(c['entries'])<=3
 for e in c['entries']:
  raw=Path(e['raw_path']).read_bytes() if e.get('raw_path') else b''
  if e.get('raw_path'):assert sha(e['raw_path'])==e['raw_sha256'] and len(raw)<=s['byte_cap_per_response']+1
  if e.get('summary') is not None:assert e['summary']==summarize(raw,e.get('status'),e['complete'])
 assert r['summaries']==[{k:e.get(k) for k in ['repo','summary','failure_type']} for e in c['entries']]
 assert not c['raw_or_credentials_to_AI_Git_cloud'] and not c['file_or_blob_content_reads'] and not c['explicit_new_login_or_permission_mutations'] and not r['new_economic_runs'] and not r['model_admitted'] and not r['full_goal_complete']
 print(json.dumps({'actual_tree_metadata_verified':True,'new_network_or_credential_calls':0,'dataset_recovered':0}))
def counterexamples():
 row={'path':'factor_system/registry/registry.db','type':'blob','mode':'100644','size':0,'sha':'a'*40};j={'sha':'b'*40,'tree':[row],'truncated':False}
 def run(x,code=200,full=True):return summarize(json.dumps(x).encode(),code,full)
 assert run(j)['valid_tree_metadata'] and run(j)['empty_blobs']==1 and run(j)['original_dataset_recovered']==0
 cases=[[],{**j,'sha':'bad'},{**j,'truncated':None},{**j,'tree':[row,row]},{**j,'tree':[{**row,'size':True}]},{**j,'tree':[{**row,'path':'../secret'}]},{**j,'tree':[row]*5001}]
 for x in cases:assert not run(x)['valid_tree_metadata']
 assert not run(j,401)['valid_tree_metadata'] and not run(j,200,False)['valid_tree_metadata'] and not run({**j,'truncated':True})['complete_declared_single_tree']
 class Stub:
  calls=0
  def get(self,*args,**kwargs):self.calls+=1
 old=begin_get.__globals__['write'];stub=Stub();c={'method_GETs':0}
 try:
  begin_get.__globals__['write']=lambda *args:(_ for _ in ()).throw(PermissionError('synthetic'))
  try:begin_get(stub,'synthetic',c,Path('synthetic'))
  except PermissionError:pass
  assert stub.calls==c['method_GETs']==0
 finally:begin_get.__globals__['write']=old
 print(json.dumps({'pure_tree_metadata_cases':len(cases)+4,'predispatch_failure_stub_GETs':0,'credential_or_network_calls':0}))
if __name__=='__main__':
 try:
  if '--capture' in sys.argv:capture(read(P))
  elif '--verify' in sys.argv:verify(read(P))
  elif '--counterexamples' in sys.argv:counterexamples()
  else:raise ValueError('mode')
 except Exception as exc:print(json.dumps({'error_type':type(exc).__name__,'credentials_and_private_payload_not_emitted':True}));raise SystemExit(1)
