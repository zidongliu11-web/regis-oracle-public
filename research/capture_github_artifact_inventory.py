"""Read one repository artifact inventory using existing noninteractive Git authentication."""
import hashlib,json,os,subprocess,sys,time
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit
R=Path(__file__).resolve().parents[1]
P=R/'research/GITHUB_ARTIFACT_INVENTORY_PROTOCOL_20261002.json'
O=R/'research/GITHUB_ARTIFACT_INVENTORY_RECEIPT_20261002.json'
def now():return datetime.now(timezone.utc).isoformat()
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(v):O.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
p=json.loads(P.read_text(encoding='utf-8'))
if len(sys.argv)==1:
 assert not O.exists(),'one-shot receipt exists'
 try:r=subprocess.run([sys.executable,'-X','utf8',__file__,'worker'],cwd=R,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=55,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
 except subprocess.TimeoutExpired:
  v=json.loads(O.read_text(encoding='utf-8')) if O.exists() else {'protocol_sha256':sha(P),'requests':[]}
  v.update({'process_timeout':True,'ended_utc':now(),'failure':'worker_wall_limit','data_admitted':False});save(v);print(json.dumps({'process_timeout':True,'data_admitted':False}));raise SystemExit(1)
 print(r.stdout.decode('utf-8',errors='replace').strip());raise SystemExit(r.returncode)
import requests
env=dict(os.environ,GIT_TERMINAL_PROMPT='0',GCM_INTERACTIVE='never')
flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
v={'schema':'regis.github.artifact_inventory_receipt.v1','protocol_sha256':sha(P),'ancestor_receipt_sha256':sha(R/p['ancestor_receipt']),'repository':p['repository'],'started_utc':now(),'requests':[],'process_timeout':False,'retries':0,'redirects':0,'new_login_or_privilege_change':False,'credentials_to_files_AI_logs_or_Git':False,'account_secret_log_raw_payload_reads':0,'data_admitted':False,'economic_run':False,'model_admitted':False};save(v)
try:
 deadline=time.monotonic()+47
 remote=subprocess.run(['git','remote','get-url','origin'],cwd=R,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10,creationflags=flags)
 assert remote.returncode==0,'origin_metadata_unavailable'
 u=urlsplit(remote.stdout.decode().strip());assert u.scheme=='https' and u.hostname=='github.com' and u.path.lstrip('/').removesuffix('.git')==p['repository'],'origin_context_mismatch'
 context=('protocol=https\nhost=github.com\npath='+p['repository']+'.git\n\n').encode()
 auth_result=subprocess.run(['git','credential','fill'],cwd=R,env=env,input=context,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10,creationflags=flags)
 assert auth_result.returncode==0,'existing_noninteractive_credential_unavailable'
 credentials=dict(line.split('=',1) for line in auth_result.stdout.decode().splitlines() if '=' in line)
 token=credentials.get('password');assert token,'existing_noninteractive_credential_missing'
 # Credentials stay in this process and are sent only to the declared HTTPS GitHub API resources.
 session=requests.Session();session.headers.update({'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2026-03-10'})
 for i,item in enumerate(p['objects']):
  parsed=urlsplit(item['url']);assert parsed.scheme=='https' and parsed.hostname=='api.github.com' and parsed.path.startswith('/repos/'+p['repository']+'/'),'API_scope_mismatch'
  row={'collection':item['collection'],'url':item['url'],'started_utc':now()};v['requests'].append(row);save(v)
  try:
   with session.get(item['url'],stream=True,allow_redirects=False,timeout=(6,6)) as response:
    row['status']=response.status_code;row['headers']={k:response.headers.get(k) for k in ['Content-Type','Content-Length','Date','ETag','X-GitHub-Request-Id']}
    data=bytearray()
    for chunk in response.iter_content(8192):
     assert time.monotonic()<deadline,'worker_wall_limit';data.extend(chunk);assert len(data)<=p['byte_cap_per_object'],'byte_cap_exceeded'
   raw=Path(p['raw_directory'])/('artifact_inventory_'+str(i)+'.json');raw.write_bytes(data)
   row.update({'raw_path':str(raw),'raw_bytes':len(data),'raw_sha256':sha(raw),'complete_within_cap':True})
   if row['status']!=200:row['failure']='HTTP_not_200'
   else:
    payload=json.loads(data)
    assert isinstance(payload,dict) and isinstance(payload.get('artifacts'),list),'artifact_collection_shape'
    row['collection_count']=len(payload['artifacts']);row['total_count']=payload.get('total_count')
    row['artifacts_observed']=[{k:x.get(k) for k in ['id','name','size_in_bytes','expired','created_at','updated_at','expires_at','digest','workflow_run']} for x in payload['artifacts']]
  except Exception as e:row['failure']=str(e) if isinstance(e,AssertionError) else type(e).__name__
  row['ended_utc']=now();save(v)
  if row.get('failure'):break
 session.close();credentials.clear();token=None;auth_result=None
except Exception as e:v['failure']=str(e) if isinstance(e,AssertionError) else type(e).__name__
v['ended_utc']=now();save(v)
print(json.dumps({'native_requests_initiated':len(v['requests']),'statuses':[x.get('status') for x in v['requests']],'collection_counts':[x.get('collection_count') for x in v['requests']],'failure':v.get('failure'),'data_admitted':False,'economic_run':False}))
