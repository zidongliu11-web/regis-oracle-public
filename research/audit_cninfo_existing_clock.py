"""Offline date/time granularity of five already captured public directory samples; no new access."""
import hashlib,json,sys,time
from pathlib import Path
from datetime import datetime,timedelta,timezone
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R));P=R/'research/CNINFO_EXISTING_CLOCK_PROTOCOL_20261003.json';O=R/'research/CNINFO_EXISTING_CLOCK_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def summarize(raw,spec):
 out={'code':spec['code'],'parsed_JSON':False,'schema_and_clock_fields_valid':False,'timestamp_is_first_publication':False,'UTCplus8_epoch_ms_is_verified_source_semantics':False}
 try:j=json.loads(raw.decode('utf-8-sig'))
 except (ValueError,UnicodeError):return out
 out['parsed_JSON']=True
 if not isinstance(j,dict) or not isinstance(j.get('announcements'),list):return out
 rows=j['announcements'];out['rows']=len(rows)
 if len(rows)>30:return out
 start,end=spec['query_date_window'].split('~');valid=clocks=midnight=subsec=window=0;ids=set();versions={}
 for row in rows:
  if not isinstance(row,dict):continue
  key=row.get('announcementId');epoch=row.get('announcementTime')
  if row.get('secCode')!=spec['code'] or not isinstance(key,(str,int)) or isinstance(key,bool) or not str(key).strip():continue
  valid+=1;ids.add(str(key))
  if not isinstance(epoch,int) or isinstance(epoch,bool) or not 100000000000<=epoch<=100000000000000:continue
  try:d=datetime.fromtimestamp(epoch//1000,timezone(timedelta(hours=8)))+timedelta(milliseconds=epoch%1000)
  except (ValueError,OverflowError,OSError):continue
  clocks+=1;window+=start<=d.date().isoformat()<=end;midnight+=d.hour==d.minute==d.second==d.microsecond==0;subsec+=d.microsecond!=0;versions.setdefault(str(key),[]).append(epoch)
 total=j.get('totalAnnouncement');total_ok=isinstance(total,int) and not isinstance(total,bool) and total>=len(rows) and total<=100000000
 out.update(exact_code_nonempty_scalar_id_rows=valid,unique_ids=len(ids),valid_epoch_ms_under_assumption_rows=clocks,selected_window_under_assumption_rows=window,exact_midnight_under_assumption_rows=midnight,nonmidnight_under_assumption_rows=clocks-midnight,subsecond_under_assumption_rows=subsec,duplicate_id_groups=sum(len(v)>1 for v in versions.values()),same_id_conflicting_epoch_groups=sum(len(set(v))>1 for v in versions.values()),declared_total=total if total_ok else None,all_pages_returned_under_declared_metadata=total_ok and total==len(rows) and j.get('hasMore') is False,schema_and_clock_fields_valid=valid==clocks==window==len(rows) and len(ids)==len(rows) and total_ok)
 return out
def inputs(s):
 assert {p:sha(R/p) for p in s['input_hashes']}==s['input_hashes'] and {p:sha(p) for p in s['native_input_hashes']}==s['native_input_hashes']
def gate(s):
 from scripts.workflow_guard import contract_hash
 t=read(R/'WORKFLOW_TASK.json');idx={a['path']:a['sha256'] for a in t['artifacts']}
 assert t['status']=='in_progress' and t['id']==s['parent_task'] and t['owner_session']==s['execution_gate']['owner_session'] and t['contract_sha256']==s['execution_gate']['expected_contract_sha256']==contract_hash(t)
 assert read(P)==s and sha(P)==idx[P.relative_to(R).as_posix()] and sha(__file__)==idx[Path(__file__).resolve().relative_to(R).as_posix()]
 assert [x['code'] for x in s['specs']]==['002237','002654','002919','300184','300374'] and s['max_new_network_calls']==0 and s['max_source_decode_attempts']==5
 inputs(s);return t
def build(s):
 inputs(s);out=[]
 for spec in s['specs']:
  raw=Path(spec['raw_path']).read_bytes();assert len(raw)<=65536 and hashlib.sha256(raw).hexdigest()==spec['raw_sha256'];old=read(spec['receipt_path']);assert old['status']==200 and old['sha256']==spec['raw_sha256'] and old['code']==spec['code']
  part=summarize(raw,spec);assert part.get('rows')==old['returned'];out.append(part)
 return {'sources':out,'sources_decoded':len(out),'total_returned_rows':sum(x.get('rows',0) for x in out),'total_exact_midnight_rows_under_assumption':sum(x.get('exact_midnight_under_assumption_rows',0) for x in out),'total_nonmidnight_rows_under_assumption':sum(x.get('nonmidnight_under_assumption_rows',0) for x in out),'source_selection_unbiased_news_corpus':False,'raw_source_JSON_parsed_in_native_process':True,'title_body_or_financial_fields_semantically_analyzed':False,'new_HTTP_POSTs':0,'new_economic_runs':0,'first_visible_immutable_vintage_or_allA_news_certified':False,'model_admitted':False}
def persist(D,c):
 errors=[]
 def attempt(label,fn):
  try:
   v=fn();return True if v is None else v
  except Exception as e:errors.append({'stage':label,'error_type':type(e).__name__});return None
 c['terminal_recording_errors']=errors;attempt('receipt_write',lambda:write(D/'receipt.json',c))
 hs={key:attempt(key,lambda p=p:sha(p)) for key,p in [('protocol_sha256',P),('implementation_sha256',Path(__file__)),('native_protocol_sha256',D/'protocol.json'),('native_task_sha256',D/'frozen_task.json'),('native_receipt_sha256',D/'receipt.json')]}
 r={'schema':'regis.existing_directory_clock_result.v1','state':'actual_offline_existing_directory_clock_audit' if not c.get('failure_type') and not errors else 'actual_offline_failure_preserved',**hs,'summary':c.get('summary'),'failure_type':c.get('failure_type'),'terminal_recording_errors':errors,'new_network_calls':0,'new_economic_runs':0,'model_admitted':False,'full_goal_complete':False}
 if attempt('result_write',lambda:write(O,r)) is None:r['state']='actual_offline_failure_preserved';attempt('fallback_write',lambda:write(D/'terminal_failure.json',r))
 return r
def compute(s):
 t=gate(s);assert read(O)['state']=='not_run';D=Path(s['native_directory']);assert not D.exists() and 'onedrive' not in str(D).lower();D.mkdir();write(D/'protocol.json',s);write(D/'frozen_task.json',t);start=time.monotonic();c={'started_utc':datetime.now(timezone.utc).isoformat(),'failure_type':None,'summary':None}
 try:c['summary']=build(s)
 except Exception as e:c['failure_type']=type(e).__name__
 c.update(ended_utc=datetime.now(timezone.utc).isoformat(),elapsed_seconds=round(time.monotonic()-start,3));r=persist(D,c);print(json.dumps({'state':r['state'],'summary':r['summary'],'failure_type':r['failure_type'],'new_network_calls':0}))
def verify(s):
 inputs(s);r=read(O);D=Path(s['native_directory']);c=read(D/'receipt.json');t=read(D/'frozen_task.json');from scripts.workflow_guard import contract_hash
 assert r['protocol_sha256']==sha(P)==r['native_protocol_sha256']==sha(D/'protocol.json') and read(D/'protocol.json')==s and r['implementation_sha256']==sha(__file__)
 assert r['native_receipt_sha256']==sha(D/'receipt.json') and r['native_task_sha256']==sha(D/'frozen_task.json') and t['contract_sha256']==s['execution_gate']['expected_contract_sha256']==contract_hash(t)
 if not r['failure_type']:assert build(s)==r['summary']==c['summary']
 assert not r['new_network_calls'] and not r['new_economic_runs'] and not r['model_admitted'] and not r['full_goal_complete'];print(json.dumps({'actual_offline_clock_audit_verified':True,'new_network_calls':0}))
def counterexamples():
 spec={'code':'002237','query_date_window':'2015-03-01~2015-03-31'};epoch=int(datetime(2015,3,2,tzinfo=timezone(timedelta(hours=8))).timestamp()*1000);row={'secCode':'002237','announcementId':'synthetic','announcementTime':epoch}
 def obj(rows):return {'announcements':rows,'totalAnnouncement':len(rows),'hasMore':False}
 def run(j):return summarize(json.dumps(j).encode(),spec)
 assert run(obj([row]))['schema_and_clock_fields_valid'] and run(obj([row]))['exact_midnight_under_assumption_rows']==1
 cases=[[],{'announcements':None},obj([row]*31),obj([{**row,'secCode':'000001'}]),obj([{**row,'announcementId':True}]),obj([{**row,'announcementId':float('nan')}]),obj([{**row,'announcementId':float('inf')}]),obj([{**row,'announcementId':0.25}]),obj([{**row,'announcementTime':True}]),obj([{**row,'announcementTime':epoch+86400000*40}]),obj([row,row])]
 for j in cases:assert not run(j)['schema_and_clock_fields_valid']
 assert not summarize(b'invalid',spec)['parsed_JSON'];fraction=run(obj([{**row,'announcementTime':epoch+1}]));assert fraction['exact_midnight_under_assumption_rows']==0 and fraction['subsecond_under_assumption_rows']==1
 print(json.dumps({'schema_precision_clock_cases':len(cases)+3,'new_network_calls':0}))
if __name__=='__main__':
 try:
  if '--counterexamples' in sys.argv:counterexamples()
  elif '--compute' in sys.argv:compute(read(P))
  elif '--verify' in sys.argv:verify(read(P))
  else:raise ValueError('mode')
 except Exception as e:print(json.dumps({'error_type':type(e).__name__,'raw_not_emitted':True}));raise SystemExit(1)
