"""One exact archived blob and its directory metadata; no member payloads extracted."""
import hashlib,json,os,re,subprocess,sys,time
from pathlib import Path
from datetime import datetime,timezone
from urllib.parse import urlsplit
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R))
P=R/'research/OWNER_CANDIDATE_ARCHIVE_PROTOCOL_20261003.json';O=R/'research/OWNER_CANDIDATE_ARCHIVE_RESULT_20261003.json'
REPOS=['vex-override-97779s-research']
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def hexsha(x):return isinstance(x,str) and re.fullmatch('[0-9a-f]{40}',x) is not None
import io,zipfile
def gitblobsha(raw):return hashlib.sha1(('blob '+str(len(raw))+'\0').encode('ascii')+raw).hexdigest()
def summarize(raw,status,complete,spec):
 out={'HTTP_status':status,'captured_bytes':len(raw),'complete_under_application_cap':complete,'exact_listed_blob_bytes_verified':False,'ZIP_directory_accepted':False,'archive_member_payload_reads':0,'original_dataset_recovered':0,'license_or_full_history_absence_certified':False}
 if status!=200 or not complete:return out
 out['exact_listed_blob_bytes_verified']=len(raw)==spec['listed_size'] and gitblobsha(raw)==spec['blob_sha']
 if not out['exact_listed_blob_bytes_verified']:return out
 try:
  with zipfile.ZipFile(io.BytesIO(raw),'r') as z:
   infos=z.infolist()
   if len(infos)>256:return {**out,'directory_entry_cap_exceeded':True}
   formats={e:0 for e in ('.csv','.parquet','.h5','.hdf5','.db','.sqlite','.json','.png','.jpg','.jpeg','.webp','.gif','.svg','.pdf','.py','.cpp','.java','.js','.txt','.md','other')}
   files=dirs=encrypted=markers=quant=duplicates=unsafe=0;seen=set()
   for item in infos:
    name=item.filename;dirs+=item.is_dir();encrypted+=bool(item.flag_bits&1);duplicates+=name in seen;seen.add(name)
    unsafe+=name.startswith(('/','\\')) or any(p=='..' for p in name.replace('\\','/').split('/'))
    if item.is_dir():continue
    files+=1;lower=name.casefold();ext=next((x for x in formats if x!='other' and lower.endswith(x)),'other');formats[ext]+=1
    markers+=any(x in lower for x in ('regis-oracle','factor_system','registry.db','pit_panel','quantqc','m24_accounting'))
    quant+=any(x in lower for x in ('rqalpha','ricequant','tushare','baostock','cninfo','jqdata','joinquant','quantqc','pit_panel','factor_system','stock','equity','finance','ohlc','kline','backtest','行情','股票','量化','因子','回测','策略'))
   out.update(ZIP_directory_accepted=True,entries=len(infos),files=files,directories=dirs,format_counts=formats,encrypted_metadata_entries=encrypted,duplicate_name_entries=duplicates,unsafe_path_metadata_entries=unsafe,project_marker_member_candidates=markers,quant_member_filename_clue_rows=quant,member_contents_or_CRC_validated=False,metadata_names_prove_contents=False)
 except (zipfile.BadZipFile,NotImplementedError,UnicodeError,ValueError):out['ZIP_directory_error_preserved']=True
 return out
def inputs(s):assert {p:sha(R/p) for p in s['input_hashes']}==s['input_hashes'] and {p:sha(p) for p in s['native_input_hashes']}==s['native_input_hashes']
def gate(s):
 from scripts.workflow_guard import contract_hash
 t=read(R/'WORKFLOW_TASK.json');idx={a['path']:a['sha256'] for a in t['artifacts']}
 assert t['id']==s['parent_task'] and t['status']=='in_progress' and t['owner_session']==s['execution_gate']['owner_session'] and t['contract_sha256']==s['execution_gate']['expected_contract_sha256']==contract_hash(t)
 assert read(P)==s and sha(P)==idx[P.relative_to(R).as_posix()] and sha(__file__)==idx[Path(__file__).resolve().relative_to(R).as_posix()]
 assert len(s['specs'])==1 and s['specs'][0]['repo']==REPOS[0]
 pre=read(s['selection_preflight_path']);spec=s['specs'][0]
 assert spec['url']==pre['exact_blob_url'] and spec['blob_sha']==pre['selection']['blob_sha'] and spec['listed_size']==pre['selection']['size']==503848 and pre['selection']['format']=='.zip'
 assert s['method_GET_cap']==1 and s['byte_cap_per_response']==524288 and s['archive_member_payload_reads']==0 and s['directory_entry_cap']==256;inputs(s);return t

def persist(D,c):
 errors=[]
 def attempt(label,fn):
  try:v=fn();return True if v is None else v
  except Exception as e:errors.append({'stage':label,'error_type':type(e).__name__});return None
 c['terminal_recording_errors']=errors;attempt('receipt_write',lambda:write(D/'receipt.json',c))
 hs={k:attempt(k,lambda p=p:sha(p)) for k,p in [('protocol_sha256',P),('implementation_sha256',Path(__file__)),('native_protocol_sha256',D/'protocol.json'),('native_task_sha256',D/'frozen_task.json'),('native_receipt_sha256',D/'receipt.json')]}
 r={'schema':'regis.owner_candidate_archive_result.v1','state':'actual_bounded_owner_archive_directory' if not errors and not c.get('failure_type') else 'actual_failure_preserved',**hs,'method_GETs':c['method_GETs'],'outer_archive_blob_requests':c['method_GETs'],'summaries':[{k:e.get(k) for k in ['repo','summary','failure_type']} for e in c['entries']],'failure_type':c.get('failure_type'),'terminal_recording_errors':errors,'new_economic_runs':0,'model_admitted':False,'full_goal_complete':False,'archive_member_payload_reads':0}
 if attempt('result_write',lambda:write(O,r)) is None:r['state']='actual_failure_preserved';attempt('fallback_write',lambda:write(D/'terminal_failure.json',r))
 return r
def begin_get(session,url,c,D):
 get=session.get;write(D/'receipt.json',c);c['method_GETs']+=1
 return get(url,stream=True,allow_redirects=False,timeout=(6,6))
def capture(s):
 t=gate(s);assert read(O)['state']=='not_run';D=Path(s['native_directory']);assert not D.exists() and 'onedrive' not in str(D.resolve()).lower()
 c={'started_utc':datetime.now(timezone.utc).isoformat(),'method_GETs':0,'entries':[],'archive_member_payload_reads':0,'raw_or_credentials_to_AI_Git_cloud':False,'credential_helper_internal_refresh_or_store':'unverified','explicit_new_login_or_permission_mutations':0};start=time.monotonic();session=response=auth=token=None;credentials={}
 try:
  D.mkdir();write(D/'protocol.json',s);write(D/'frozen_task.json',t)
  env=dict(os.environ,GIT_TERMINAL_PROMPT='0',GCM_INTERACTIVE='never');flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
  remote=subprocess.run(['git','remote','get-url','origin'],cwd=R,env=env,capture_output=True,timeout=10,creationflags=flags);assert remote.returncode==0
  u=urlsplit(remote.stdout.decode().strip());assert u.scheme=='https' and u.hostname=='github.com' and u.username is None and u.password is None and u.path.lstrip('/').removesuffix('.git')=='zidongliu11-web/regis-oracle'
  context=b'protocol=https\nhost=github.com\npath=zidongliu11-web/regis-oracle.git\n\n'
  auth=subprocess.run(['git','credential','fill'],cwd=R,env=env,input=context,capture_output=True,timeout=10,creationflags=flags);assert auth.returncode==0 and len(auth.stdout)<=4096
  credentials=dict(line.split('=',1) for line in auth.stdout.decode().splitlines() if '=' in line);token=credentials.get('password');assert token
  import requests
  session=requests.Session();session.trust_env=False;session.mount('https://',requests.adapters.HTTPAdapter(max_retries=0));session.headers.update({'Authorization':'Bearer '+token,'Accept':'application/vnd.github.raw+json','X-GitHub-Api-Version':'2026-03-10'})
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
    p=D/('candidate_archive.bin');p.write_bytes(data);e.update(raw_path=str(p),raw_sha256=sha(p))
   e.update(complete=complete,summary=summarize(bytes(data),e.get('status'),complete,spec));write(D/'receipt.json',c)
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
 assert r['method_GETs']==c['method_GETs']<=1 and len(c['entries'])<=1
 for e in c['entries']:
  raw=Path(e['raw_path']).read_bytes() if e.get('raw_path') else b''
  if e.get('raw_path'):assert sha(e['raw_path'])==e['raw_sha256'] and len(raw)<=s['byte_cap_per_response']+1
  if e.get('summary') is not None:assert e['summary']==summarize(raw,e.get('status'),e['complete'],s['specs'][0])
 assert r['summaries']==[{k:e.get(k) for k in ['repo','summary','failure_type']} for e in c['entries']]
 assert not c['raw_or_credentials_to_AI_Git_cloud'] and not c['archive_member_payload_reads'] and not c['explicit_new_login_or_permission_mutations'] and not r['new_economic_runs'] and not r['model_admitted'] and not r['full_goal_complete']
 print(json.dumps({'actual_archive_directory_verified':True,'new_network_or_credential_calls':0,'dataset_recovered':0}))
def counterexamples():
 def make(names):
  f=io.BytesIO()
  with zipfile.ZipFile(f,'w') as z:
   for name in names:z.writestr(name,b'synthetic not financial source data')
  return f.getvalue()
 raw=make(['images/picture.png','factor_system/registry.db']);spec={'listed_size':len(raw),'blob_sha':gitblobsha(raw)}
 out=summarize(raw,200,True,spec);assert out['ZIP_directory_accepted'] and out['files']==2 and out['project_marker_member_candidates']==1 and not out['original_dataset_recovered']
 assert not summarize(raw,403,True,spec)['ZIP_directory_accepted'] and not summarize(raw,200,False,spec)['exact_listed_blob_bytes_verified']
 assert not summarize(raw,200,True,{**spec,'blob_sha':'a'*40})['exact_listed_blob_bytes_verified']
 bad=b'not a zip';assert summarize(bad,200,True,{'listed_size':len(bad),'blob_sha':gitblobsha(bad)})['ZIP_directory_error_preserved']
 many=make([str(i)+'.csv' for i in range(257)]);assert summarize(many,200,True,{'listed_size':len(many),'blob_sha':gitblobsha(many)})['directory_entry_cap_exceeded']
 unsafe=make(['../secret.csv']);assert summarize(unsafe,200,True,{'listed_size':len(unsafe),'blob_sha':gitblobsha(unsafe)})['unsafe_path_metadata_entries']==1
 old={k:getattr(zipfile.ZipFile,k) for k in ('open','read','extract','extractall','testzip')}
 try:
  for k in old:setattr(zipfile.ZipFile,k,lambda *a,**kw:(_ for _ in ()).throw(AssertionError('member_payload_read_forbidden')))
  assert summarize(raw,200,True,spec)['ZIP_directory_accepted']
 finally:
  for k,v in old.items():setattr(zipfile.ZipFile,k,v)
 print(json.dumps({'pure_archive_cases':8,'actual_network_or_credentials_or_member_payload_reads':0,'filenameclues_are_not_datasets_or_license':True}))
if __name__=='__main__':
 try:
  if '--capture' in sys.argv:capture(read(P))
  elif '--verify' in sys.argv:verify(read(P))
  elif '--counterexamples' in sys.argv:counterexamples()
  else:raise ValueError('mode')
 except Exception as exc:print(json.dumps({'error_type':type(exc).__name__,'credentials_and_private_payload_not_emitted':True}));raise SystemExit(1)
