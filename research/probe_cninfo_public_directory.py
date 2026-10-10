"""Single public website announcement directory feasibility sample, native bodies only."""
import hashlib,json,re,runpy,sys,time
from pathlib import Path
from datetime import datetime,timezone,timedelta
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R))
P=R/'research/CNINFO_PUBLIC_DIRECTORY_PROTOCOL_20261003.json';O=R/'research/CNINFO_PUBLIC_DIRECTORY_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def summarize(raw,status):
 out={'HTTP_status':status,'bytes':len(raw),'parsed_JSON':False,'directory_schema_accepted':False,'publication_first_visible_clock_certified':False,'historical_vintage_certified':False}
 try:j=json.loads(raw.decode('utf-8-sig'))
 except (ValueError,UnicodeError):return out
 out['parsed_JSON']=True
 if not isinstance(j,dict):return out
 rows=j.get('announcements');total=j.get('totalAnnouncement');more=j.get('hasMore');valid_total=isinstance(total,int) and not isinstance(total,bool) and 0<=total<=100000000
 out.update(returned_rows=len(rows) if isinstance(rows,list) else None,declared_total=total if valid_total else None,hasMore=more if isinstance(more,bool) else None)
 if status!=200 or not isinstance(rows,list) or len(rows)>10 or not valid_total or total<len(rows):return out
 good=clocks=midnight=0;ids=set()
 for row in rows:
  if not isinstance(row,dict):continue
  key=row.get('announcementId');epoch=row.get('announcementTime')
  if row.get('secCode')!='000001' or not isinstance(key,(str,int)) or isinstance(key,bool) or not str(key).strip():continue
  good+=1;ids.add(str(key))
  if isinstance(epoch,int) and not isinstance(epoch,bool) and 100000000000<=epoch<=100000000000000:
   try:d=datetime.fromtimestamp(epoch//1000,timezone(timedelta(hours=8)))+timedelta(milliseconds=epoch%1000)
   except (ValueError,OverflowError,OSError):continue
   clocks+=d.date().isoformat()=='2015-03-02';midnight+=d.hour==d.minute==d.second==d.microsecond==0
 out.update(exact_code_and_nonempty_id_rows=good,unique_announcement_ids=len(ids),selected_day_rows_under_epochms_UTCplus8_assumption=clocks,midnight_rows_under_same_assumption=midnight,directory_schema_accepted=good==clocks==len(rows) and len(ids)==len(rows),all_pages_recovered=total==len(rows) and more is False)
 return out
def gate(s):
 from scripts.workflow_guard import contract_hash
 t=read(R/'WORKFLOW_TASK.json');idx={a['path']:a['sha256'] for a in t['artifacts']}
 assert t['status']=='in_progress' and t['id']==s['parent_task'] and t['owner_session']==s['execution_gate']['owner_session'] and t['contract_sha256']==s['execution_gate']['expected_contract_sha256']==contract_hash(t)
 assert read(P)==s and sha(P)==idx[P.relative_to(R).as_posix()] and sha(__file__)==idx[Path(__file__).resolve().relative_to(R).as_posix()]
 assert s['url']=='https://www.cninfo.com.cn/new/hisAnnouncement/query' and s['method_POST_cap']==1 and s['byte_cap']==131072 and s['row_cap']==10
 q=s['params'];assert q['pageNum']==1 and q['pageSize']==10 and q['seDate']=='2015-03-02~2015-03-02' and q['stock']=='000001,'+s['orgId'] and q['searchkey']=='' and q['column']=='szse'
 assert {p:sha(R/p) for p in s['input_hashes']}==s['input_hashes'] and {p:sha(p) for p in s['native_input_hashes']}==s['native_input_hashes']
 return t
def persist(D,c):
 m=runpy.run_path(str(R/'research/probe_cninfo_news_access.py'));fn=m['persist'];fn.__globals__.update(P=P,O=O,__file__=__file__);return fn(D,c)
def begin_post(session,s,c,D):
 post=session.post;url=s['url'];params=s['params'];headers={'User-Agent':'RegisOracle-public-directory-feasibility/1.0','Content-Type':'application/x-www-form-urlencoded; charset=UTF-8','Referer':s['referer']}
 write(D/'receipt.json',c);c['method_POSTs']+=1
 return post(url,data=params,headers=headers,stream=True,allow_redirects=False,timeout=(6,8))
def capture(s):
 t=gate(s);assert read(O)['state']=='not_run';D=Path(s['native_directory']);assert not D.exists() and 'onedrive' not in str(D).lower();D.mkdir();write(D/'protocol.json',s);write(D/'frozen_task.json',t)
 c={'started_utc':datetime.now(timezone.utc).isoformat(),'method_POSTs':0,'summary':None,'failure_type':None,'credentials_or_login_or_contacts_or_purchases':False,'raw_to_AI_Git_cloud':False,'body_document_GETs':0};start=time.monotonic();response=session=None
 try:
  import requests
  session=requests.Session();session.trust_env=False;session.cookies.clear();session.mount('https://',requests.adapters.HTTPAdapter(max_retries=0));response=begin_post(session,s,c,D);c['HTTP_status']=response.status_code;data=bytearray()
  for chunk in response.iter_content(2048):
   assert time.monotonic()-start<=35;data.extend(chunk);assert len(data)<=s['byte_cap']
  p=D/'response.bin';p.write_bytes(data);c.update(raw_path=str(p),raw_sha256=sha(p),summary=summarize(bytes(data),response.status_code))
 except Exception as e:c['failure_type']=type(e).__name__
 finally:
  for obj in [response,session]:
   if obj is not None:
    try:obj.close()
    except Exception as e:c['cleanup_error']=type(e).__name__
 c.update(ended_utc=datetime.now(timezone.utc).isoformat(),elapsed_seconds=round(time.monotonic()-start,3));r=persist(D,c);print(json.dumps({'state':r['state'],'summary':r['summary'],'failure_type':r['failure_type'],'new_economic_runs':0,'model_admitted':False}))
def counterexamples():
 epoch=int(datetime(2015,3,2,tzinfo=timezone(timedelta(hours=8))).timestamp()*1000);row={'secCode':'000001','announcementId':'synthetic','announcementTime':epoch}
 def check(j,status=200):return summarize(json.dumps(j).encode(),status)['directory_schema_accepted']
 def obj(rows):return {'announcements':rows,'totalAnnouncement':len(rows),'hasMore':False}
 assert check(obj([row])) and check(obj([]))
 for j,status in [([],200),({'announcements':None,'totalAnnouncement':0},200),(obj([row]*11),200),(obj([{**row,'secCode':'600000'}]),200),(obj([{**row,'announcementTime':True}]),200),(obj([{**row,'announcementTime':epoch+86400000}]),200),(obj([row,row]),200),(obj([row]),401),(obj([{**row,'announcementId':None}]),200),({'announcements':[],'totalAnnouncement':True},200)]:assert not check(j,status)
 assert not summarize(b'invalid',200)['parsed_JSON']
 for key in [float('nan'),float('inf'),0.25]:assert not check(obj([{**row,'announcementId':key}]))
 assert summarize(json.dumps(obj([{**row,'announcementTime':epoch+1}])).encode(),200)['midnight_rows_under_same_assumption']==0
 class Stub:
  calls=0
  def post(self,*args,**kwargs):self.calls+=1
 old=begin_post.__globals__['write'];stub=Stub();c={'method_POSTs':0}
 try:
  begin_post.__globals__['write']=lambda *args:(_ for _ in ()).throw(PermissionError('synthetic'))
  try:begin_post(stub,{'url':'synthetic','params':{},'referer':'synthetic'},c,Path('synthetic'))
  except PermissionError:pass
  assert stub.calls==c['method_POSTs']==0
 finally:begin_post.__globals__['write']=old
 print(json.dumps({'pure_schema_and_clock_counterexamples':17,'predispatch_failure_actual_POST_stubs':0,'network_calls':0}))
def verify(s):
 r=read(O);D=Path(s['native_directory']);c=read(D/'receipt.json');t=read(D/'frozen_task.json');from scripts.workflow_guard import contract_hash
 assert r['implementation_sha256']==sha(__file__) and r['protocol_sha256']==sha(P)==r['native_protocol_sha256']==sha(D/'protocol.json') and read(D/'protocol.json')==s
 assert sha(D/'receipt.json')==r['native_receipt_sha256'] and sha(D/'frozen_task.json')==r['native_task_sha256'] and t['contract_sha256']==s['execution_gate']['expected_contract_sha256']==contract_hash(t)
 assert {p:sha(R/p) for p in s['input_hashes']}==s['input_hashes'] and {p:sha(p) for p in s['native_input_hashes']}==s['native_input_hashes']
 if c.get('raw_path'):assert sha(c['raw_path'])==c['raw_sha256'] and summarize(Path(c['raw_path']).read_bytes(),c['HTTP_status'])==r['summary']
 assert r['summary']==c['summary'] and c['method_POSTs']<=1 and c['body_document_GETs']==0 and not c['raw_to_AI_Git_cloud'] and not c['credentials_or_login_or_contacts_or_purchases'] and not r['new_economic_runs'] and not r['model_admitted'] and not r['full_goal_complete']
 print(json.dumps({'actual_native_directory_verified':True,'network_reruns':0,'PIT_or_model_admitted':False}))
if __name__=='__main__':
 try:
  if '--counterexamples' in sys.argv:counterexamples()
  elif '--capture' in sys.argv:capture(read(P))
  elif '--verify' in sys.argv:verify(read(P))
  else:raise ValueError('mode')
 except Exception as e:print(json.dumps({'error_type':type(e).__name__,'raw_not_emitted':True}));raise SystemExit(1)
