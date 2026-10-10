"""One declared observed public JS asset; never invoke an endpoint found in it."""
import hashlib,json,os,subprocess,sys,time
from datetime import datetime,timezone
from pathlib import Path
R=Path(__file__).resolve().parents[1]
P=R/'research/SSE_OBSERVED_PROFILE_HANDLER_PROTOCOL_20261002.json'
O=R/'research/SSE_OBSERVED_PROFILE_HANDLER_RECEIPT_20261002.json'
def now():return datetime.now(timezone.utc).isoformat()
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(v):O.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
p=json.loads(P.read_text(encoding='utf-8'))
if len(sys.argv)==1:
 assert not O.exists(),'one-shot receipt exists'
 try:r=subprocess.run([sys.executable,'-X','utf8',__file__,'worker'],cwd=R,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=26,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
 except subprocess.TimeoutExpired:
  v=json.loads(O.read_text(encoding='utf-8')) if O.exists() else {'protocol_sha256':sha(P),'requests_initiated':0}
  v.update({'process_timeout':True,'ended_utc':now(),'failure':'worker_wall_limit','source_admitted':False});save(v);print(json.dumps({'process_timeout':True,'source_admitted':False}));raise SystemExit(1)
 print(r.stdout.decode('utf-8',errors='replace').strip());raise SystemExit(r.returncode)
import requests
v={'schema':'regis.sse.observed_profile_handler_receipt.v1','protocol_sha256':sha(P),'ancestor_receipt_sha256':sha(R/p['ancestor_receipt']),'url':p['url'],'started_utc':now(),'requests_initiated':1,'retries':0,'redirects':0,'market_quote_account_order_calls':0,'source_admitted':False,'economic_run':False,'model_admitted':False,'process_timeout':False};save(v)
try:
 deadline=time.monotonic()+23
 with requests.get(p['url'],timeout=(6,6),allow_redirects=False,stream=True) as response:
  v['status']=response.status_code;v['headers']={k:response.headers.get(k) for k in ('Content-Type','Content-Length','Date','Last-Modified','ETag')};assert response.status_code==200,'HTTP_not_200'
  b=bytearray()
  for part in response.iter_content(8192):
   assert time.monotonic()<deadline,'worker_wall_limit';b.extend(part);assert len(b)<=p['byte_cap'],'byte_cap_exceeded'
 raw=Path(p['raw_path']);raw.write_bytes(b);v.update({'raw_bytes':len(b),'raw_sha256':sha(raw),'body_complete_within_cap':True})
 text=b.decode('utf-8',errors='replace');v['static_markers']={m:m in text for m in ['scale-download','sg-download','funddetail','fundDetail','history','download','export','jsonp','getJSON','ajax']}
except Exception as e:v['failure']=str(e) if isinstance(e,AssertionError) else type(e).__name__
v['ended_utc']=now();save(v)
print(json.dumps({'status':v.get('status'),'bytes':v.get('raw_bytes'),'sha256':v.get('raw_sha256'),'failure':v.get('failure'),'source_admitted':False,'endpoint_calls':0}))
