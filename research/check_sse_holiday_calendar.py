"""Compare a declared weekday/official-notice calendar; no prices or trades."""
import argparse,hashlib,importlib.metadata,importlib.util,json,os,re,sys
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
PROTO=ROOT/'research/SSE_CALENDAR_COMPARISON_PROTOCOL_20261003.json'
RESULT=ROOT/'research/SSE_CALENDAR_COMPARISON_RESULT_20261003.json'
CODE='research/check_sse_holiday_calendar.py'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def require(ok,msg):
 if not ok:raise ValueError(msg)
def utc():return datetime.now(timezone.utc).isoformat()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def encode(d):return int(d.strftime('%Y%m%d'))
def interval(text,year):
 ds=re.findall(r'(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日',text)
 require(len(ds) in (1,2),'ambiguous closed interval')
 vals=[date(int(y or year),int(m),int(d)) for y,m,d in ds]
 a,b=vals[0],vals[-1];require(a<=b and (b-a).days<=10,'oversize or reversed interval')
 require(date(year-1,12,1)<=a<=b<=date(year,12,31),'wrong closure year')
 return a,b
def notice_ranges(source):
 year=source['year'];text=source.get('previous_primary_domain_search_body',source['article_excerpt_returned'])
 rows=[]
 if source['role']=='extension':
  require('2020-01-27' in text,'extension publication missing')
  matches=re.findall(r'延长2020年春节休市至([^，。]+)',text)
  require(len(matches)==1,'extension ambiguous')
  a,b=interval(matches[0],2020);require(a==b==date(2020,2,2),'extension changed')
  # Annual notice ends Jan30; closure extension is the following Jan31-Feb2.
  return [(date(2020,1,31),b)]
 for line in text.splitlines():
  line=re.sub(r'^L\d+:\s*','',line).strip()
  pat=r'[（(][一二三四五六七][）)][^：:]+[：:](.+)' if source['role']=='annual' else r'一、休市安排[：:](.+)'
  m=re.search(pat,line)
  if m and '休市' in m[1]:rows.append(interval(m[1].split('休市',1)[0],year))
 require(len(rows)==source['expected_closed_intervals'],'notice closure body incomplete or duplicated')
 require(len(set(rows))==len(rows),'duplicate closed intervals')
 return rows
def compile_dates(ranges,start,end):
 closed=set()
 for a,b in ranges:
  require(a<=b and (b-a).days<=10,'invalid closure range')
  while a<=b:closed.add(a);a+=timedelta(days=1)
 out=[];d=start
 while d<=end:
  if d.weekday()<5 and d not in closed:out.append(encode(d))
  d+=timedelta(days=1)
 return out
def derive_notice_calendar(s):
 packet=read(s['notice_packet']);require(len(packet['sources'])==len(s['source_roles'])==11,'wrong source count')
 ranges=[];per=[]
 for ref,spec in zip(packet['sources'],s['source_roles']):
  require(ref['url']==spec['url'],'notice ordering changed');obj={**ref,**spec};parts=notice_ranges(obj);ranges+=parts;per.append({'url':ref['url'],'year':spec['year'],'role':spec['role'],'closed_ranges':[[encode(a),encode(b)] for a,b in parts]})
 require(len(per)==11,'source role list incomplete')
 require([20200124,20200130] in next(x['closed_ranges'] for x in per if x['year']==2020 and x['role']=='annual'),'extension annual predecessor changed')
 return compile_dates(ranges,date(2013,1,1),date(2020,12,31)),per
def differences(old,new):return {'reference_only_dates':sorted(set(old)-set(new)),'notice_calendar_only_dates':sorted(set(new)-set(old))}
def counterexamples():
 src={'year':2019,'role':'annual','expected_closed_intervals':1,'article_excerpt_returned':'（一）元旦：2018年12月30日（星期日）至2019年1月1日（星期二）休市，1月2日起照常开市。'}
 require(notice_ranges(src)==[(date(2018,12,30),date(2019,1,1))],'cross year')
 require(compile_dates(notice_ranges(src),date(2019,1,1),date(2019,1,3))==[20190102,20190103],'weekdays')
 ext={'year':2020,'role':'extension','article_excerpt_returned':'2020-01-27\n一、延长2020年春节休市至2月2日（星期日），2月3日（星期一）正常开市。'}
 require(compile_dates(notice_ranges(ext),date(2020,1,30),date(2020,2,3))==[20200130,20200203],'extension weekend')
 require(differences([20200131],[20200203])=={'reference_only_dates':[20200131],'notice_calendar_only_dates':[20200203]},'mismatch masked')
 rejected=0
 for value in ['5月4日至5月1日','1月1日至1月20日','未知','2020年1月1日']:
  try:interval(value,2019)
  except ValueError:rejected+=1
 require(rejected==4,'bad interval admitted')
 bad=dict(src,expected_closed_intervals=2)
 try:notice_ranges(bad)
 except ValueError:pass
 else:raise ValueError('truncated notice admitted')
 sources=[];roles=[]
 for y in range(2013,2021):
  sources.append({'url':str(y),'article_excerpt_returned':('（一）春节：1月24日至1月30日休市。' if y==2020 else '（一）元旦：1月1日休市。')})
  roles.append({'url':str(y),'year':y,'role':'annual','expected_closed_intervals':1})
 for y in [2015,2019]:
  sources.append({'url':str(y)+'extra','article_excerpt_returned':'一、休市安排：9月3日至9月5日休市。'})
  roles.append({'url':str(y)+'extra','year':y,'role':'addition','expected_closed_intervals':1})
 sources.append({'url':'extension','article_excerpt_returned':ext['article_excerpt_returned']});roles.append({'url':'extension','year':2020,'role':'extension','expected_closed_intervals':1})
 global read
 original_read=read;read=lambda _: {'sources':sources}
 try:
  days,per=derive_notice_calendar({'notice_packet':'purememory','source_roles':roles})
  require(len(per)==11 and days and 20200131 not in days and 20200203 in days,'full eleven-source smoke')
  rejected=0
  for truncated in [roles[:10],roles+roles[:1]]:
   try:derive_notice_calendar({'notice_packet':'purememory','source_roles':truncated})
   except ValueError:rejected+=1
  require(rejected==2,'zip truncation accepted')
 finally:read=original_read
 print(json.dumps({'pure_calendar_cases':12,'passed':True,'real_native_calendar_reads':0,'real_notice_packet_reads':0,'network_calls':0}))
def check(s):
 for p,h in s['input_hashes'].items():require(sha(ROOT/p)==h,'repository input changed')
 for p,h in s['native_input_hashes'].items():require(sha(p)==h,'native input changed')
def authorize(s):
 sys.path.insert(0,str(ROOT));from scripts.workflow_guard import contract_hash
 t=read(ROOT/'WORKFLOW_TASK.json');g=s['execution_gate'];f=read(ROOT/g['fixed_precheck'])
 require(t['owner_session']==g['owner_session']==os.environ.get('CODEX_THREAD_ID'),'wrong owner/session')
 require(t['contract_sha256']==contract_hash(t)==g['expected_contract_sha256']==f['contract'],'unsealed contract')
 require(f['passed'] and f['fixed_agent']=='/root/fixed_reviewer_m24' and f.get('precheck_recorded_before_source') is True,'actual fixed approval missing')
 a={x['path']:x['sha256'] for x in t['artifacts']}
 require(a.get(CODE)==sha(__file__)==s['input_hashes'][CODE]==f['code_sha256'],'implementation not approved')
 require(a.get(PROTO.relative_to(ROOT).as_posix())==sha(PROTO)==f['protocol_sha256'],'protocol not approved')
 require(t['current_checkpoint'].get('E66_fixed_precheck')==f,'precheck not recorded in Task')
 return t
def run(s):
 require(read(RESULT)['state']=='not_run','attempt already retained');authorize(s);check(s)
 sys.path.insert(0,str(ROOT));from scripts.workflow_guard import atomic_json
 local=Path(s['native_directory']);require(not local.exists() and 'onedrive' not in str(local).lower(),'native directory not fresh')
 result={'state':'attempt_started_not_completed','started_utc':utc(),'phase':'before_native_create','native_calendar_load_calls':0,'notice_derivations':0,'source_GETs':0,'economic_runs':0,'signal_calendar_PIT_admitted':False,'model_admitted':False,'full_goal_complete':False,'implementation_sha256':sha(__file__),'protocol_sha256':sha(PROTO)}
 def progress(phase):result['phase']=phase;atomic_json(RESULT,result)
 progress('before_native_create');failure=None
 try:
  local.mkdir();progress('copy_frozen_inputs')
  for name,source in [('frozen_WORKFLOW_TASK.json',ROOT/'WORKFLOW_TASK.json'),('frozen_protocol.json',PROTO),('fixed_precheck.json',ROOT/s['execution_gate']['fixed_precheck'])]:(local/name).write_bytes(source.read_bytes())
  for name,spec in s['runtime'].items():
   require(importlib.metadata.version(spec['distribution'])==spec['version'],'bundled version changed')
   require(str(Path(importlib.util.find_spec(name).origin).resolve())==spec['module_init'],'wrong import origin')
  progress('derive_declared_notice_calendar')
  try:dates,per=derive_notice_calendar(s)
  finally:result['notice_derivations']+=1
  write(local/'notice_calendar.json',{'source_ranges':per,'dates':dates,'weekdays_only_assumption':True,'role':'ex_post_realized_session_comparison_not_signal_knownAt'})
  progress('read_native_calendar');import numpy as np
  from research.inspect_short_rq_full import read_calendar
  import inspect
  require(Path(inspect.getfile(read_calendar)).resolve()==(ROOT/'research/inspect_short_rq_full.py').resolve(),'wrong bounded calendar helper')
  comparisons=[]
  for p in s['calendars']:
   try:values=read_calendar(p,np)
   finally:result['native_calendar_load_calls']+=1
   ints=[int(d) for d in values];require(ints==sorted(set(ints)),'native calendar duplicate or unsorted')
   # Validate YYYYMMDD before selecting, never reinterpret malformed days.
   for d in ints:date(d//10000,d//100%100,d%100)
   selected=[d for d in ints if 20130101<=d<=20201231];diff=differences(selected,dates)
   write(local/(Path(p).parent.name+'.differences.json'),diff)
   comparisons.append({'native_calendar_sha256':sha(p),'selected_dates':len(selected),'first':selected[0] if selected else None,'last':selected[-1] if selected else None,'reference_only_count':len(diff['reference_only_dates']),'notice_only_count':len(diff['notice_calendar_only_dates'])})
  result.update(state='declared_notice_calendar_matches_native' if all(not(x['reference_only_count'] or x['notice_only_count']) for x in comparisons) else 'declared_notice_calendar_mismatch_retained',official_notice_documents=11,notice_dates=len(dates),annual_counts={str(y):sum(d//10000==y for d in dates) for y in range(2013,2021)},comparisons=comparisons,weekdays_only_assumption=True,other_unlisted_exceptions_certified_absent=False,original_HTTP_bytes_captured=False,signal_calendar_knownAt_certified=False)
 except BaseException as e:failure=(result['phase'],type(e).__name__)
 try:progress('post_input_verification');check(s)
 except BaseException as e:failure=failure or ('post_input_verification',type(e).__name__)
 try:
  progress('manifest');m=local/'manifest.json';write(m,{'files':{str(p):sha(p) for p in local.rglob('*') if p.is_file()}});result['local_details']={'path':str(m),'sha256':sha(m)}
 except BaseException as e:failure=failure or ('manifest',type(e).__name__)
 if failure:result.update(state='attempt_failed_retained',failed_phase=failure[0],error_type=failure[1])
 result.update(phase='terminal_record',ended_utc=utc())
 try:atomic_json(RESULT,result)
 except BaseException as e:print(json.dumps({'state':'terminal_write_failed','error_type':type(e).__name__}));raise SystemExit(1)
 print(json.dumps({k:v for k,v in result.items() if k!='local_details'}))
def verify(s):
 check(s);r=read(RESULT);require(r['phase']=='terminal_record','terminal evidence missing')
 require(r['implementation_sha256']==sha(__file__) and r['protocol_sha256']==sha(PROTO),'actual versions changed')
 require(sha(r['local_details']['path'])==r['local_details']['sha256'],'manifest changed')
 for p,h in read(r['local_details']['path'])['files'].items():require(sha(p)==h,'native evidence changed')
 require(r['native_calendar_load_calls']<=2 and r['notice_derivations']<=1,'attempt cap exceeded')
 require(not any(r[k] for k in ['source_GETs','economic_runs','signal_calendar_PIT_admitted','model_admitted','full_goal_complete']),'scope escalated')
 print(json.dumps({'finite_calendar_attempt_verified':True,'state':r['state'],'economic_runs':0}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--run',action='store_true');ap.add_argument('--verify',action='store_true');ap.add_argument('--counterexamples',action='store_true');a=ap.parse_args();require(sum(vars(a).values())==1,'select one mode')
 if a.counterexamples:counterexamples()
 elif a.run:run(read(PROTO))
 else:verify(read(PROTO))
