"""First anonymous official metadata-only access probes; raw values stay native."""
import hashlib,json,re,sys,time
from datetime import datetime,timezone
from pathlib import Path
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R))
P=R/'research/CNINFO_NEWS_ACCESS_PROTOCOL_20261003.json';O=R/'research/CNINFO_NEWS_ACCESS_RESULT_20261003.json'
SPECS=[{'id':'announcement','url':'https://webapi.cninfo.com.cn/api/info/p_info3015','params':{'scode':'000001','sdate':'2015-03-02','edate':'2015-03-02','page':'1','pagesize':'5','format':'json','@limit':'5','@column':'TEXTID,RECID,SECCODE,F001D,RECTIME,F009V'}},
 {'id':'news','url':'https://webapi.cninfo.com.cn/api/info/p_info3030','params':{'scode':'000001','sdate':'2015-03-02','edate':'2015-03-02','format':'json','@limit':'5','@column':'TEXTID,SECCODE,DECLAREDATE,F001V'}}]
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def summarize(raw,spec,status):
 out={'HTTP_status':status,'bytes':len(raw),'parsed_JSON':False,'records_count':None,'numeric_resultcode':None,'permission_or_token_message_predicate':False,'rows_schema_and_limit_accepted':False,'historical_available_at_or_coverage_certified':False}
 try:j=json.loads(raw.decode('utf-8-sig'))
 except (ValueError,UnicodeError):return out
 out['parsed_JSON']=True
 if not isinstance(j,dict):out['root_is_expected_object']=False;return out
 out['root_is_expected_object']=True;value=j.get('resultcode',j.get('code'))
 if isinstance(value,(str,int)) and not isinstance(value,bool) and re.fullmatch(r'-?\d{1,10}',str(value)):out['numeric_resultcode']=int(value)
 message=str(j.get('resultmsg',j.get('message',''))).casefold();out['permission_or_token_message_predicate']=any(w in message for w in ('token','授权','权限','认证','unauthorized','authentication','access denied'))
 records=j.get('records')
 if not isinstance(records,list):return out
 out['records_count']=len(records)
 if status!=200 or out['numeric_resultcode']!=200 or len(records)>5 or out['permission_or_token_message_predicate']:return out
 allowed=set(spec['params']['@column'].split(','));valid=0;extra=0;clocks=0;ids=set()
 for row in records:
  if not isinstance(row,dict):continue
  keys={str(k).upper() for k in row};extra+=bool(keys-allowed)
  if keys-allowed:continue
  normalized={str(k).upper():v for k,v in row.items()};code=normalized.get('SECCODE');identity=normalized.get('TEXTID')
  if not isinstance(code,str) or code!='000001' or identity is None or isinstance(identity,(dict,list,bool)) or not str(identity).strip():continue
  valid+=1;ids.add(str(identity))
  clock=normalized.get('RECTIME' if spec['id']=='announcement' else 'DECLAREDATE')
  if isinstance(clock,str):
   try:
    d=datetime.fromisoformat(clock.replace('Z','+00:00'));clocks+=d.date().isoformat()=='2015-03-02'
   except ValueError:pass
 out.update(valid_code_and_textid_rows=valid,unique_textid_count=len(ids),unexpected_field_rows=extra,valid_in_selected_day_datetime_rows=clocks,rows_schema_and_limit_accepted=valid==len(records) and extra==0)
 return out
def gate(s):
 from scripts.workflow_guard import contract_hash
 t=read(R/'WORKFLOW_TASK.json');idx={a['path']:a['sha256'] for a in t['artifacts']}
 assert t['id']==s['parent_task'] and t['status']=='in_progress' and t['owner_session']==s['execution_gate']['owner_session'] and t['contract_sha256']==s['execution_gate']['expected_contract_sha256']==contract_hash(t)
 assert read(P)==s and s['specs']==SPECS and s['max_method_GETs']==2 and s['response_byte_cap']==131072 and s['row_cap']==5
 assert sha(P)==idx[P.relative_to(R).as_posix()] and sha(__file__)==idx[Path(__file__).resolve().relative_to(R).as_posix()]
 assert {p:sha(R/p) for p in s['input_hashes']}==s['input_hashes'] and {p:sha(p) for p in s['native_input_hashes']}==s['native_input_hashes']
 return t
def persist(D,c):
 errors=[]
 def attempt(label,action):
  try:
   value=action();return True if value is None else value
  except Exception as e:errors.append({'stage':label,'error_type':type(e).__name__});return None
 c['terminal_recording_errors']=errors;attempt('receipt_write',lambda:write(D/'receipt.json',c))
 hs={key:attempt(key,lambda path=path:sha(path)) for key,path in [('protocol_sha256',P),('implementation_sha256',Path(__file__)),('native_protocol_sha256',D/'protocol.json'),('native_task_sha256',D/'frozen_task.json'),('native_receipt_sha256',D/'receipt.json')]}
 state='actual_anonymous_metadata_access_probe' if not c.get('failure_type') and not c.get('cleanup_error') and not errors else 'actual_probe_failure_preserved'
 result={'schema':'regis.cninfo_anonymous_access_result.v1','state':state,**hs,'summary':c.get('summary'),'failure_type':c.get('failure_type'),'terminal_recording_errors':errors,'new_economic_runs':0,'model_admitted':False,'full_goal_complete':False}
 if attempt('result_write',lambda:write(O,result)) is None:
  result['state']='actual_probe_failure_preserved';attempt('fallback_write',lambda:write(D/'terminal_failure.json',result))
 return result
def persist_counterexamples():
 original_write,original_sha=globals()['write'],globals()['sha'];sink={}
 def recorded(path,value):sink[Path(path).name]=json.loads(json.dumps(value))
 try:
  globals()['write'],globals()['sha']=recorded,lambda path:'a'*64
  result=persist(Path('synthetic'),{'failure_type':None,'summary':{}})
  assert result['state']=='actual_anonymous_metadata_access_probe' and O.name in sink and 'terminal_failure.json' not in sink
  for failed in ['receipt.json',O.name]:
   sink.clear()
   def failing(path,value):
    if Path(path).name==failed:raise PermissionError('synthetic write')
    recorded(path,value)
   globals()['write']=failing;result=persist(Path('synthetic'),{'failure_type':None,'summary':{}})
   assert result['state']=='actual_probe_failure_preserved' and result['terminal_recording_errors']
   if failed==O.name:assert 'terminal_failure.json' in sink
 finally:globals()['write'],globals()['sha']=original_write,original_sha
 return 3

def begin_get(session,c,D,spec):
 write(D/'receipt.json',c);c['method_GETs']+=1
 return session.get(spec['url'],params=spec['params'],stream=True,allow_redirects=False,timeout=(6,8))
def capture(s):
 t=gate(s);D=Path(s['native_directory']);assert not D.exists() and read(O)['state']=='not_run' and 'onedrive' not in str(D.resolve()).lower();D.mkdir();write(D/'protocol.json',s);write(D/'frozen_task.json',t)
 c={'started_utc':datetime.now(timezone.utc).isoformat(),'method_GETs':0,'requests':[],'failure_type':None,'retries':0,'redirects':0,'credentials_or_login_or_contacts_or_purchases':False,'article_body_requests':0,'raw_to_AI_Git_cloud':False};session=None
 try:
  import requests
  session=requests.Session();session.trust_env=False
  for spec in s['specs']:
   item={'id':spec['id'],'status':None,'failure_type':None,'complete':False};c['requests'].append(item);raw=bytearray();start=time.monotonic()
   try:
    with begin_get(session,c,D,spec) as response:
     item['status']=response.status_code
     for chunk in response.iter_content(4096):
      assert len(raw)+len(chunk)<=131072 and time.monotonic()-start<=35;raw.extend(chunk)
     item['response_stream_complete']=True
    path=D/(spec['id']+'.bin');path.write_bytes(raw);item.update(raw_path=str(path),raw_sha256=sha(path),bytes=len(raw),summary=summarize(raw,spec,item['status']),complete=True)
   except Exception as e:
    item['failure_type']=type(e).__name__
    if raw:
     path=D/(spec['id']+'_partial.bin');path.write_bytes(raw);item.update(raw_path=str(path),raw_sha256=sha(path),bytes=len(raw))
   item['elapsed_seconds']=round(time.monotonic()-start,3)
  c['summary']={'method_GETs':c['method_GETs'],'requests':[{'id':x['id'],'status':x['status'],'complete':x['complete'],'bytes':x.get('bytes'),'failure_type':x['failure_type'],'summary':x.get('summary')} for x in c['requests']],'credentials_supplied':False,'historical_coverage_or_PIT_certified':False,'new_economic_runs':0,'model_admitted':False}
 except Exception as e:c['failure_type']=type(e).__name__
 finally:
  if session:
   try:session.close()
   except Exception as e:c['cleanup_error']=type(e).__name__
  c['ended_utc']=datetime.now(timezone.utc).isoformat();r=persist(D,c)
 print(json.dumps({'state':r['state'],'summary':r['summary'],'failure_type':r['failure_type']}))
def counterexamples():
 def b(v):return json.dumps(v).encode()
 spec=SPECS[1]
 denied=summarize(b({'resultcode':401,'resultmsg':'synthetic token denied'}),spec,200);assert denied['permission_or_token_message_predicate'] and not denied['rows_schema_and_limit_accepted']
 row={'TEXTID':'synthetic','SECCODE':'000001','DECLAREDATE':'2015-03-02 09:00:00','F001V':'synthetic'}
 ok=summarize(b({'resultcode':200,'records':[row]}),spec,200);assert ok['rows_schema_and_limit_accepted'] and ok['valid_in_selected_day_datetime_rows']==1 and not ok['historical_available_at_or_coverage_certified']
 for changed in [dict(row,SECCODE='000002'),dict(row,TEXTID=None),dict(row,UNREQUESTED_BODY='synthetic')]:assert not summarize(b({'resultcode':200,'records':[changed]}),spec,200)['rows_schema_and_limit_accepted']
 assert not summarize(b({'resultcode':200,'records':[row]*6}),spec,200)['rows_schema_and_limit_accepted']
 assert not summarize(b({'records':[row]}),spec,200)['rows_schema_and_limit_accepted'];assert not summarize(b({'resultcode':200,'records':[row]}),spec,403)['rows_schema_and_limit_accepted']
 assert not summarize(b'not JSON',spec,200)['parsed_JSON'];assert not summarize(b([]),spec,200)['root_is_expected_object']
 original_write=globals()['write'];calls=[]
 class SyntheticSession:
  def get(self,*args,**kwargs):calls.append(1);raise AssertionError('unexpected_network_call')
 def failed_receipt(path,value):raise PermissionError('synthetic predispatch write')
 c={'method_GETs':0}
 try:
  globals()['write']=failed_receipt
  try:begin_get(SyntheticSession(),c,Path('synthetic'),spec)
  except PermissionError:pass
  else:raise AssertionError('predispatch_failure_not_raised')
 finally:globals()['write']=original_write
 assert not calls and c['method_GETs']==0
 return {'access_schema_limit_identity_permission_and_nonadmission_counterexamples':10,'pre_request_failed_receipt_zero_GET_counterexample':1,'successful_and_failed_terminal_write_counterexamples':persist_counterexamples(),'actual_network_calls':0}
def verify(s):
 D=Path(s['native_directory']);r=read(O);c=read(D/'receipt.json');t=read(D/'frozen_task.json');from scripts.workflow_guard import contract_hash
 assert sha(P)==r['protocol_sha256']==r['native_protocol_sha256']==sha(D/'protocol.json') and read(D/'protocol.json')==s and sha(__file__)==r['implementation_sha256'] and sha(D/'receipt.json')==r['native_receipt_sha256'] and sha(D/'frozen_task.json')==r['native_task_sha256'] and t['contract_sha256']==s['execution_gate']['expected_contract_sha256']==contract_hash(t)
 assert c['method_GETs']<=2 and not c['credentials_or_login_or_contacts_or_purchases'] and not c['raw_to_AI_Git_cloud'] and c['article_body_requests']==0
 assert {p:sha(p) for p in s['native_input_hashes']}==s['native_input_hashes']
 for item,spec in zip(c['requests'],s['specs']):
  assert item['id']==spec['id']
  if item.get('raw_path'):assert sha(item['raw_path'])==item['raw_sha256']
  if item['complete']:assert item['summary']==summarize(Path(item['raw_path']).read_bytes(),spec,item['status'])
 assert r['summary']==c.get('summary');print(json.dumps({'actual_native_probe_verified':True,'new_network_calls':0,'model_or_Goal_admitted':False}))
if __name__=='__main__':
 try:
  if '--counterexamples' in sys.argv:print(json.dumps(counterexamples()))
  elif '--capture' in sys.argv:capture(read(P))
  elif '--verify' in sys.argv:verify(read(P))
  else:raise ValueError('mode')
 except Exception as e:print(json.dumps({'error_type':type(e).__name__,'raw_payload_not_emitted':True}));raise SystemExit(1)
