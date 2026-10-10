"""Single conditional table interpretation of retained main text; no decoder or prices."""
import argparse,hashlib,json,os,re,sys
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from research.csrc_industry_members import parse_cell_tables,STOCK
PROTO=ROOT/'research/CSRC_2012_TEXT_TABLE_PROTOCOL_20261003.json'
RESULT=ROOT/'research/CSRC_2012_TEXT_TABLE_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def utc():return datetime.now(timezone.utc).isoformat()
def require(ok,msg):
 if not ok:raise ValueError(msg)
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def atomic(p,v):
 tmp=Path(str(p)+'.tmp');write(tmp,v);tmp.replace(p)
def interpret(text):
 require(len(text)<=100000 and text.endswith('\r'),'main text bounds/end')
 require(not any(ord(x)<32 and x not in '\r\n\t\x07\x0c' for x in text),'undeclared control')
 pieces=text.split('\x07');require(len(pieces)>6 and (len(pieces)-1)%6==0,'six-mark frame remainder')
 require(not pieces[-1].strip(),'nonempty post-table remainder')
 titles=re.findall(r'(?<!\d)(20\d{2})\s*年?\s*(?:第\s*)?([1-4一二三四])\s*季度',pieces[0])
 titles=[(year,{'一':'1','二':'2','三':'3','四':'4'}.get(quarter,quarter)) for year,quarter in titles]
 require(titles==[('2012','4')],'single declared quarter title missing or mixed')
 frames=[];positions=[];cursor=0
 for k in range(0,len(pieces)-1,6):
  frame=pieces[k:k+6];require(not frame[5].strip(),'sixth mark not empty row boundary')
  starts=[]
  for cell in frame:
   starts.append(cursor);cursor+=len(cell)+1
  frames.append(frame[:5]);positions.append(starts[:5])
 headers=[re.sub(r'\s+','',x) for x in frames[0]]
 sc=[i for i,x in enumerate(headers) if '上市公司' in x and '代码' in x]
 mc=[i for i,x in enumerate(headers) if '行业大类代码' in x]
 require(len(sc)==len(mc)==1 and sc[0]!=mc[0],'ambiguous first header roles')
 stock_col,major_col=sc[0],mc[0]
 # Only the header whitespace is normalized; numerical cells retain their syntax.
 frames[0]=headers
 rows,summary=parse_cell_tables([frames])
 require(summary['header_rows']==1 and len(rows)==len(frames)-1,'non-one-header/data frame')
 tokens=STOCK.findall(text);require(len(tokens)==len(set(tokens))==len(rows),'full token/unique row mismatch')
 cp=[0]
 for x in text:cp.append(cp[-1]+(2 if ord(x)>0xffff else 1))
 group=None;label=None;group_rows=0
 for i,cells in enumerate(frames[1:],1):
  raw_group=cells[major_col].strip()
  if raw_group:
   require(re.fullmatch(r'\d{1,2}(?:\.0)?',raw_group),'invalid group syntax')
   group=str(int(float(raw_group))).zfill(2);group_rows+=1
   raw=pieces[i*6+major_col];a=positions[i][major_col]+len(raw)-len(raw.lstrip())
   label={'row':i+1,'char_begin':a,'char_end':a+len(raw_group),'main_UTF16_cp_begin':cp[a]}
  code=cells[stock_col].strip();require(code in rows and rows[code]['source_industry_major_numeric']==group,'row interpretation mismatch')
  raw=pieces[i*6+stock_col];a=positions[i][stock_col]+len(raw)-len(raw.lstrip())
  require(text[a:a+6]==code and label is not None,'source locator mismatch')
  rows[code]['source_locator'].update(native_text_char_begin=a,native_text_char_end=a+6,main_UTF16_cp_begin=cp[a],group_label=label.copy())
 return rows,{'frames':len(frames),'data_rows':len(rows),'header_rows':1,'columns':5,'row_end_marks':len(frames),'six_digit_tokens':len(tokens),'duplicate_tokens':0,'explicit_group_rows':group_rows,'group_counts':dict(sorted(Counter(v['source_industry_major_numeric'] for v in rows.values()).items())),'text_code_units':cp[-1]}
def fixture(data=None):
 rows=[['2012年第四季度\r行业门类名称','行业大类名称','行业大类代码','上市公司代码','上市公司简称'],*(data or [['采矿业','煤炭','06','000001','甲'],['','','','000002','乙']])]
 return ''.join('\x07'.join([*(c+'\r' for c in r),'\r'])+'\x07' for r in rows)+'\r'
def counterexamples():
 good=fixture();rows,m=interpret(good);require(len(rows)==2 and m['explicit_group_rows']==1 and rows['000002']['source_locator']['group_label']['row']==2,'blank group/locator failed')
 changed=fixture([['采矿业','煤炭','06','000001','甲'],['制造业','化学','13','000002','乙']]);require(interpret(changed)[1]['group_counts']=={'06':1,'13':1},'class transition failed')
 for title in ['2012年4季度','2012年第4季度']:require(len(interpret(good.replace('2012年第四季度',title))[0])==2,'declared title syntax rejected')
 bad=[good.replace('2012年第四季度','2013年第一季度'),good+'123456',good.replace('\x07','',1),good.replace('000002','000001'),good.replace('000002','00002'),good.replace('06\r','xx\r',1),good.replace('06\r','00\r',1),good.replace('上市公司代码','行业名称'),good.replace('行业大类代码','行业名称'),good.replace('甲','甲123456'),good.replace('06\r','\r',1),good.replace('\x07\r\x07','\x07非空\r\x07',1),good.replace('000001','000\r001'),good.replace('甲','甲\x00')]
 bad.extend(good.replace('2012年第四季度',title) for title in ['2012年第四季度与2013年第一季度','2012年第四季度与2012年第三季度','2012年第四季度2012年第四季度'])
 for x in bad:
  try:interpret(x)
  except ValueError:pass
  else:raise AssertionError('invalid conditional table accepted')
 print(json.dumps({'pure_table_cases':21,'passed':True,'real_text_reads':0,'source_decode_calls':0,'network_calls':0}))
def check(s):
 for p,h in s['input_hashes'].items():require(sha(ROOT/p)==h,'formal frozen input changed')
 for p,h in s['native_input_hashes'].items():require(sha(p)==h,'native frozen input changed')
def authorize(s):
 from scripts.workflow_guard import contract_hash
 require(s==read(PROTO),'execution parameters differ from frozen protocol')
 t=read(ROOT/'WORKFLOW_TASK.json');g=s['execution_gate'];f=read(ROOT/g['fixed_precheck'])
 require(os.environ.get('CODEX_THREAD_ID')==g['owner_session'],'actual calling session mismatch')
 require(t['owner_session']==g['owner_session'] and t['id']==s['parent_task'] and t['status']=='in_progress','owner/Task mismatch')
 require(contract_hash(t)==t['contract_sha256']==g['expected_contract_sha256'],'contract mismatch')
 require(f['passed'] and f['fixed_agent']=='/root/fixed_reviewer_m24' and f['contract']==t['contract_sha256'],'actual fixed success absent')
 require(f['code_sha256']==sha(__file__) and f['protocol_sha256']==sha(PROTO) and f['precheck_recorded_before_source'],'precheck versions mismatch')
 require(t['current_checkpoint'].get('E68_fixed_precheck')==f,'Task precheck missing')
 require(f.get('recorded_utc') is not None,'successful record timestamp missing')
 entries=t['artifacts'];require(len({a['path'] for a in entries})==len(entries),'duplicate artifact index')
 indexed={a['path']:a['sha256'] for a in entries}
 required=[*s['input_hashes'],str(PROTO.relative_to(ROOT)).replace('\\','/'),str(RESULT.relative_to(ROOT)).replace('\\','/')]
 for rel in required:require(indexed.get(rel)==sha(ROOT/rel),'Task direct input/code/protocol/result index mismatch')
def terminal_write_failed(r,d,error):
 r.update(intended_terminal_state=r['state'],state='terminal_write_failed_retained',phase='terminal_record_unpersisted',terminal_record_persisted=False,persistence_error_type=type(error).__name__)
 fallback=d/'terminal-write-failure.json';saved=False
 try:
  with fallback.open('x',encoding='utf-8',newline='\n') as h:h.write(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
  saved=True
 except BaseException:pass
 # Even if both locations reject writes, owned stdout retains the real call count.
 print(json.dumps({**{k:v for k,v in r.items() if k!='local_details'},'independent_failure_receipt_saved':saved},ensure_ascii=False))
 raise SystemExit(1)
def run(s):
 require(read(RESULT)['state']=='not_run','preserve first attempt');authorize(s);check(s)
 d=Path(s['native_directory']);require(not d.exists() and 'onedrive' not in str(d).lower(),'native output exists/custody')
 r={'state':'attempt_started_not_completed','phase':'before_native_create','started_utc':utc(),'text_interpretation_calls':0,'source_decode_calls':0,'source_GETs':0,'dependency_GETs':0,'price_arrays_read':False,'economic_runs':0,'industry_member_classes_activated':0,'PIT_admitted':False,'model_admitted':False,'full_goal_complete':False,'implementation_sha256':sha(__file__),'protocol_sha256':sha(PROTO)}
 try:atomic(RESULT,r)
 except BaseException as e:terminal_write_failed(r,d,e)
 failure=None
 try:
  d.mkdir();write(d/'protocol.snapshot.json',s);write(d/'precheck.snapshot.json',read(ROOT/s['execution_gate']['fixed_precheck']));write(d/'task.snapshot.json',read(ROOT/'WORKFLOW_TASK.json'))
  r['phase']='interpret_retained_main_text';atomic(RESULT,r)
  payload=Path(s['native_text']).read_bytes();require(len(payload)<=300000,'text byte ceiling')
  (d/'input.main-text.txt').write_bytes(payload)
  try:rows,counts=interpret(payload.decode('utf-8',errors='strict'))
  finally:r['text_interpretation_calls']+=1
  require(counts['six_digit_tokens']==s['prior_token_census'] and counts['text_code_units']==s['prior_text_code_units'],'prior independent token/CP census mismatch')
  write(d/'conditional.members.json',rows);write(d/'conditional.table.counts.json',counts)
  r.update(state='conditional_retained_text_table_recovered_not_activated',facts=counts,quarter='2012Q4',nonconforming_FIB_retained=True,table_layout_certified=False,known_at_certified=False)
 except BaseException as e:failure=(r['phase'],type(e).__name__)
 try:
  check(s)
  if d.exists():
   m=d/'manifest.json';write(m,{'files':{str(p):sha(p) for p in d.iterdir() if p.is_file()}});r['local_details']={'path':str(m),'sha256':sha(m)}
 except BaseException as e:failure=failure or ('final_integrity_or_manifest',type(e).__name__)
 if failure:r.update(state='attempt_failed_retained',failed_phase=failure[0],error_type=failure[1])
 r.update(phase='terminal_record',ended_utc=utc())
 try:atomic(RESULT,r)
 except BaseException as e:terminal_write_failed(r,d,e)
 print(json.dumps({k:v for k,v in r.items() if k!='local_details'},ensure_ascii=False))
def verify(s):
 check(s);r=read(RESULT);require(r['phase']=='terminal_record','terminal missing')
 require(r['implementation_sha256']==sha(__file__) and r['protocol_sha256']==sha(PROTO),'terminal versions changed')
 require(r['text_interpretation_calls']<=1 and not any(r[k] for k in ['source_decode_calls','source_GETs','dependency_GETs','price_arrays_read','economic_runs','industry_member_classes_activated','PIT_admitted','model_admitted','full_goal_complete']),'scope/count escalated')
 if 'local_details' in r:
  m=r['local_details'];require(sha(m['path'])==m['sha256'],'manifest changed')
  for p,h in read(m['path'])['files'].items():require(sha(p)==h,'native output changed')
 print(json.dumps({'finite_table_interpretation_evidence_verified':True,'state':r['state'],'classes_activated':0,'economic_runs':0}))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--run',action='store_true');p.add_argument('--verify',action='store_true');p.add_argument('--counterexamples',action='store_true');a=p.parse_args()
 if a.counterexamples:counterexamples()
 elif a.run:run(read(PROTO))
 elif a.verify:verify(read(PROTO))
 else:p.error('choose one mode')
