"""First bounded public exchange interaction homepage access; no question API or content analysis."""
import hashlib,json,sys,time
from pathlib import Path
from datetime import datetime,timezone
from html.parser import HTMLParser
from urllib.parse import urljoin,urlsplit
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R))
P=R/'research/EXCHANGE_ATTENTION_ACCESS_PROTOCOL_20261003.json';O=R/'research/EXCHANGE_ATTENTION_ACCESS_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
class Routes(HTMLParser):
 def __init__(self):super().__init__();self.scripts=[]
 def handle_starttag(self,tag,attrs):
  if tag=='script':
   for k,v in attrs:
    if k=='src' and v:self.scripts.append(v)
def summarize(raw,status,url,content_type,complete):
 out={'HTTP_status':status,'captured_bytes':len(raw),'complete_under_application_cap':complete,'HTML_accepted':False,'anonymous_question_metadata_access_certified':False,'historical_available_at_or_license_certified':False}
 if status!=200 or not complete or not isinstance(content_type,str) or 'text/html' not in content_type.lower():return out
 try:text=raw.decode('utf-8-sig')
 except UnicodeError:return out
 p=Routes();p.feed(text);origin=urlsplit(url)
 same=0
 for value in p.scripts:
  parsed=urlsplit(urljoin(url,value))
  same+=parsed.scheme=='https' and parsed.netloc==origin.netloc and parsed.username is None and parsed.password is None
 out.update(HTML_accepted=True,script_links=len(p.scripts),same_origin_https_script_links=same)
 return out
def inputs(s):assert {p:sha(R/p) for p in s['input_hashes']}==s['input_hashes'] and {p:sha(p) for p in s['native_input_hashes']}==s['native_input_hashes']
def gate(s):
 from scripts.workflow_guard import contract_hash
 t=read(R/'WORKFLOW_TASK.json');idx={a['path']:a['sha256'] for a in t['artifacts']}
 assert t['id']==s['parent_task'] and t['status']=='in_progress' and t['owner_session']==s['execution_gate']['owner_session'] and t['contract_sha256']==s['execution_gate']['expected_contract_sha256']==contract_hash(t)
 assert read(P)==s and sha(P)==idx[P.relative_to(R).as_posix()] and sha(__file__)==idx[Path(__file__).resolve().relative_to(R).as_posix()]
 assert s['urls']==['https://irm.cninfo.com.cn/','https://sns.sseinfo.com/'] and s['method_GET_cap']==2 and s['byte_cap_per_response']==262144 and s['question_API_requests']==0
 inputs(s);return t
def persist(D,c):
 errors=[]
 def attempt(label,fn):
  try:v=fn();return True if v is None else v
  except Exception as e:errors.append({'stage':label,'error_type':type(e).__name__});return None
 c['terminal_recording_errors']=errors;attempt('receipt_write',lambda:write(D/'receipt.json',c))
 hs={key:attempt(key,lambda p=p:sha(p)) for key,p in [('protocol_sha256',P),('implementation_sha256',Path(__file__)),('native_protocol_sha256',D/'protocol.json'),('native_task_sha256',D/'frozen_task.json'),('native_receipt_sha256',D/'receipt.json')]}
 r={'schema':'regis.exchange_attention_access_result.v1','state':'actual_bounded_public_homepage_access' if not errors and not c.get('recording_failure_type') else 'actual_recording_failure_preserved',**hs,'method_GETs':c['method_GETs'],'summaries':[{k:e.get(k) for k in ['source','summary','failure_type']} for e in c['entries']],'terminal_recording_errors':errors,'new_economic_runs':0,'model_admitted':False,'full_goal_complete':False,'question_API_requests':0}
 if attempt('result_write',lambda:write(O,r)) is None:r['state']='actual_recording_failure_preserved';attempt('fallback_write',lambda:write(D/'terminal_failure.json',r))
 return r
def begin_get(session,url,c,D):
 get=session.get;headers={'User-Agent':'RegisOracle-public-source-feasibility/1.0'}
 write(D/'receipt.json',c);c['method_GETs']+=1
 return get(url,headers=headers,stream=True,allow_redirects=False,timeout=(6,8))
def capture(s):
 t=gate(s);assert read(O)['state']=='not_run';D=Path(s['native_directory']);assert not D.exists() and 'onedrive' not in str(D).lower();D.mkdir();write(D/'protocol.json',s);write(D/'frozen_task.json',t)
 c={'started_utc':datetime.now(timezone.utc).isoformat(),'method_GETs':0,'entries':[],'question_API_requests':0,'SDK_requests':0,'new_economic_runs':0,'raw_to_AI_Git_cloud':False};start=time.monotonic()
 for i,url in enumerate(s['urls']):
  e={'source':'SZSE' if i==0 else 'SSE','failure_type':None,'summary':None};c['entries'].append(e);response=session=None;data=bytearray();complete=False
  try:
   import requests
   session=requests.Session();session.trust_env=False;session.cookies.clear();session.mount('https://',requests.adapters.HTTPAdapter(max_retries=0))
   response=begin_get(session,url,c,D);e.update(status=response.status_code,content_type=response.headers.get('Content-Type',''))
   for chunk in response.iter_content(2048):
    if time.monotonic()-start>45:raise TimeoutError('application_elapsed_limit')
    remaining=s['byte_cap_per_response']+1-len(data);data.extend(chunk[:remaining])
    if len(data)>s['byte_cap_per_response']:raise ValueError('application_byte_cap_exceeded')
   complete=True
  except Exception as exc:e['failure_type']=type(exc).__name__
  finally:
   for obj in [response,session]:
    if obj is not None:
     try:obj.close()
     except Exception as exc:e['cleanup_error_type']=type(exc).__name__
  try:
   if data:
    p=D/('homepage_'+str(i)+'.bin');p.write_bytes(data);e.update(raw_path=str(p),raw_sha256=sha(p))
   e.update(summary=summarize(bytes(data),e.get('status'),url,e.get('content_type'),complete),complete=complete);write(D/'receipt.json',c)
  except Exception as exc:c['recording_failure_type']=type(exc).__name__;break
 c.update(ended_utc=datetime.now(timezone.utc).isoformat(),elapsed_seconds=round(time.monotonic()-start,3));r=persist(D,c)
 print(json.dumps({'state':r['state'],'method_GETs':r['method_GETs'],'summaries':r['summaries'],'question_API_requests':0,'new_economic_runs':0}))
def verify(s):
 inputs(s);r=read(O);D=Path(s['native_directory']);c=read(D/'receipt.json');t=read(D/'frozen_task.json');from scripts.workflow_guard import contract_hash
 assert r['implementation_sha256']==sha(__file__) and r['protocol_sha256']==sha(P)==r['native_protocol_sha256']==sha(D/'protocol.json') and read(D/'protocol.json')==s
 assert r['native_receipt_sha256']==sha(D/'receipt.json') and r['native_task_sha256']==sha(D/'frozen_task.json') and t['contract_sha256']==s['execution_gate']['expected_contract_sha256']==contract_hash(t)
 assert c['method_GETs']==r['method_GETs']<=2 and len(c['entries'])<=2
 for i,e in enumerate(c['entries']):
  data=Path(e['raw_path']).read_bytes() if e.get('raw_path') else b''
  if e.get('raw_path'):assert sha(e['raw_path'])==e['raw_sha256'] and len(data)<=s['byte_cap_per_response']+1
  assert summarize(data,e.get('status'),s['urls'][i],e.get('content_type'),e['complete'])==e['summary']
 assert r['summaries']==[{k:e.get(k) for k in ['source','summary','failure_type']} for e in c['entries']]
 assert not c['raw_to_AI_Git_cloud'] and not c['question_API_requests'] and not c['SDK_requests'] and not r['new_economic_runs'] and not r['model_admitted'] and not r['full_goal_complete']
 print(json.dumps({'actual_native_access_verified':True,'new_network_requests':0,'PIT_or_model_admitted':False}))
def counterexamples():
 raw=b'<html><script src="/app.js"></script><script src="https://other.invalid/a.js"></script></html>';url='https://irm.cninfo.com.cn/'
 assert summarize(raw,200,url,'text/html',True)['same_origin_https_script_links']==1
 for data,status,ctype,complete in [(raw,401,'text/html',True),(raw,200,'application/json',True),(raw,200,'text/html',False),(b'\xff',200,'text/html',True)]:assert not summarize(data,status,url,ctype,complete)['HTML_accepted']
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
 print(json.dumps({'pure_HTML_route_cases':5,'predispatch_failure_stub_GETs':0,'network_calls':0}))
if __name__=='__main__':
 try:
  if '--capture' in sys.argv:capture(read(P))
  elif '--verify' in sys.argv:verify(read(P))
  elif '--counterexamples' in sys.argv:counterexamples()
  else:raise ValueError('mode')
 except Exception as exc:print(json.dumps({'error_type':type(exc).__name__,'raw_not_emitted':True}));raise SystemExit(1)
