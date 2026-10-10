"""Finite independent PDF table extraction. Source cells remain in native storage."""
import argparse,hashlib,importlib.util,importlib.metadata,json,os,re,subprocess,sys,zipfile
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1]
PROTO=ROOT/'research/CSRC_PDF_INDEPENDENT_PROTOCOL_20261003.json'
RESULT=ROOT/'research/CSRC_PDF_INDEPENDENT_RESULT_20261003.json'
CODE='research/check_csrc_pdf_independent_reader.py'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def utc():return datetime.now(timezone.utc).isoformat()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def require(ok,msg):
 if not ok:raise ValueError(msg)
def compact(v):return '' if v is None else re.sub(r'\s+','',str(v))

def add_table(page,number,cells,out,context):
 """Use table cell anchors, not text-order positions of centered merged labels."""
 sc=ic=None;count=0;headers=0
 for row,values in enumerate(cells,1):
  texts=[compact(x) for x in values]
  hs=[i for i,x in enumerate(texts) if x=='上市公司代码'];hc=[i for i,x in enumerate(texts) if x=='行业大类代码']
  if hs or hc:
   require(len(hs)==len(hc)==1,'ambiguous table header');sc,ic=hs[0],hc[0];headers+=1;continue
  if sc is None:
   require(not any(re.fullmatch('[0-9]{6}',x) for x in texts),'stock cells outside declared header');continue
  require(len(values)>max(sc,ic),'shifted table columns')
  if texts[ic]:
   require(re.fullmatch('[0-9]{1,2}',texts[ic]) is not None and 1<=int(texts[ic])<=90,'invalid class anchor')
   context['major']=f'{int(texts[ic]):02d}'
  code=texts[sc]
  if not code:continue
  require(re.fullmatch('[0-9]{6}',code) is not None,'invalid source identity')
  require(context.get('major') is not None and code not in out,'missing class or duplicate identity')
  out[code]={'class':context['major'],'page':page,'table':number,'row':row};count+=1
 require(headers>0 if count else True,'unheaded membership table')
 return count,headers

def differences(old,new):
 common=set(old)&set(new)
 return {'missing':len(set(old)-set(new)),'extra':len(set(new)-set(old)),
         'class_disagreement':sum(old[c]['class']!=new[c]['class'] for c in common),
         'page_disagreement':sum(old[c]['page']!=new[c]['page'] for c in common)}

def counterexamples():
 h=['行业大类\n代码','上市公司代码'];o={};ctx={}
 add_table(1,1,[h,['01','000001'],[None,'000002']],o,ctx)
 add_table(2,1,[h,[None,'000003'],['02','000004']],o,ctx)
 require(o['000002']['class']==o['000003']['class']=='01' and o['000004']['class']=='02','cell rowspan/page carry')
 rejected=0
 for body in [[h,['01','000001'],[None,'000001']],[h,[None,'000001']],[h,['00','000001']],[h,['01','00001']],[['代码'],['000001']]]:
  try:add_table(1,1,body,{},{} )
  except ValueError:rejected+=1
 require(rejected==5,'invalid table accepted')
 changed={k:dict(v) for k,v in o.items()};changed['000001']['class']='03'
 require(differences(o,changed)['class_disagreement']==1,'class difference hidden')
 print(json.dumps({'pure_table_cases':7,'passed':True,'real_PDF_reads':0}))

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
 require(a.get(str(PROTO.relative_to(ROOT)).replace('\\','/'))==sha(PROTO)==f['protocol_sha256'],'protocol not approved')
 require(t['current_checkpoint'].get('E65_fixed_precheck')==f,'precheck not recorded in Task')
 return t

def worker(s,index):
 authorize(s);local=Path(s['native_directory']);q=s['quarters'][index]
 for name,source in [('frozen_WORKFLOW_TASK.json',ROOT/'WORKFLOW_TASK.json'),('frozen_protocol.json',PROTO),('fixed_precheck.json',ROOT/s['execution_gate']['fixed_precheck'])]:
  require((local/name).read_bytes()==source.read_bytes(),'native frozen approval mismatch')
 active=read(RESULT);require(active['phase']=='worker_dispatch_pending' and active['pending_quarter']==q['quarter'],'worker not awaiting approved dispatch')
 check(s)
 for name,spec in s['runtime'].items():
  require(importlib.metadata.version(spec['distribution'])==spec['version'],'bundled reader version mismatch')
  require(str(Path(importlib.util.find_spec(name).origin).resolve())==spec['module_init'],'wrong bundled import origin')
 import pdfplumber
 rows={};context={};page_counts=[];all_tables=[];headers=0;stock_words=0
 with pdfplumber.open(q['source'],strict_metadata=True) as doc:
  require(len(doc.pages)==q['pages'] and len(doc.pages)<=100,'original page count changed')
  title_seen=False
  for page in doc.pages:
   text=page.extract_text(x_tolerance=3,y_tolerance=3,layout=False) or ''
   if page.page_number<=2:title_seen|=q['title'] in compact(text)
   words=page.extract_words(x_tolerance=3,y_tolerance=3,use_text_flow=False)
   stock_words+=sum(re.fullmatch('[0-9]{6}',w['text']) is not None for w in words)
   tables=page.find_tables(table_settings=s['table_settings']);require(len(tables)<=4,'page table bound')
   before=len(rows)
   for table_no,table in enumerate(tables,1):
    cells=table.extract(x_tolerance=3,y_tolerance=3)
    require(len(cells)<=200 and max(map(len,cells),default=0)<=16,'table size bound')
    n,h=add_table(page.page_number,table_no,cells,rows,context);headers+=h
    all_tables.append({'page':page.page_number,'table':table_no,'bbox':table.bbox,'cell_bboxes':table.cells,'values':cells,'source_stock_rows':n})
   page_counts.append(len(rows)-before);page.close()
  require(title_seen and headers>0 and len(rows)>0,'source title/header/rows missing')
 # Do not use reference classes to populate or repair the independent extraction.
 dump=local/(q['quarter']+'.independent_tables.json');write(dump,{'quarter':q['quarter'],'members':rows,'pages':page_counts,'tables':all_tables})
 old0=read(q['old_members'])['members'];old={k:{'class':v['source_industry_major_numeric'],'page':v['source_locator']['page']} for k,v in old0.items()}
 reference={k:dict(v) for k,v in old.items()};ledger=read(s['correction_ledger']);entries=ledger['rows'] if isinstance(ledger,dict) else ledger
 repairs=0
 for entry in entries:
  if entry['quarter']!=q['quarter']:continue
  code=entry['source_stock_code'];require(reference[code]['class']==entry['original_major'],'old correction reference changed')
  reference[code]['class']=entry['corrected_major'];repairs+=1
 diff=differences(reference,rows)
 observed={'quarter':q['quarter'],'state':'independent_extraction_compared','pages':len(page_counts),'parsed_members':len(rows),'reference_members':len(reference),'source_word_stock_tokens':stock_words,'table_stock_rows':sum(page_counts),'header_rows':headers,'targeted_prior_visual_repairs':repairs,'original_unrepaired_comparison':differences(old,rows),'latest_reference_comparison':diff,'table_tokens_reconciled':stock_words==len(rows),'members_sha256':sha(dump),'independent_member_classes_agree':not any(diff.values()) and stock_words==len(rows)}
 write(local/(q['quarter']+'.comparison.json'),observed)

def run(s):
 require(read(RESULT)['state']=='not_run','first attempt already retained');authorize(s);check(s)
 sys.path.insert(0,str(ROOT));from scripts.workflow_guard import atomic_json
 local=Path(s['native_directory']);require(not local.exists() and 'onedrive' not in str(local).lower(),'native directory not fresh')
 summary={'state':'attempt_started_not_completed','started_utc':utc(),'phase':'before_native_create','worker_method_calls':0,'summaries':[],'source_GETs':0,'dependency_GETs':0,'economic_runs':0,'PIT_admitted':False,'model_admitted':False,'full_goal_complete':False,'implementation_sha256':sha(__file__),'protocol_sha256':sha(PROTO)}
 def progress(phase):summary['phase']=phase;atomic_json(RESULT,summary);print(json.dumps({'phase':phase,'worker_method_calls':summary['worker_method_calls']}),flush=True)
 progress('before_native_create');failure=None
 try:
  local.mkdir();progress('copy_frozen_inputs')
  for name,source in [('frozen_WORKFLOW_TASK.json',ROOT/'WORKFLOW_TASK.json'),('frozen_protocol.json',PROTO),('fixed_precheck.json',ROOT/s['execution_gate']['fixed_precheck'])]:(local/name).write_bytes(source.read_bytes())
  for index,q in enumerate(s['quarters']):
   summary['pending_quarter']=q['quarter'];progress('worker_dispatch_pending')
   state={'quarter':q['quarter'],'state':'owned_worker_failed_retained'}
   try:
    try:p=subprocess.run([sys.executable,'-X','utf8',str(Path(__file__).resolve()),'--worker',str(index)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=s['worker_seconds_each'],creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    except subprocess.TimeoutExpired as e:(local/(q['quarter']+'.stdout.partial.log')).write_bytes(e.stdout or b'');(local/(q['quarter']+'.stderr.partial.log')).write_bytes(e.stderr or b'');raise
    finally:summary['worker_method_calls']+=1
    (local/(q['quarter']+'.stdout.log')).write_bytes(p.stdout);(local/(q['quarter']+'.stderr.log')).write_bytes(p.stderr)
    require(p.returncode==0,'owned PDF reader failed');state=read(local/(q['quarter']+'.comparison.json'))
   except BaseException as e:state.update(error_type=type(e).__name__)
   summary['summaries'].append(state);progress('quarter_terminal_record');check(s)
  summary['state']='all_four_independent_member_classes_agree' if all(x.get('independent_member_classes_agree') for x in summary['summaries']) else 'finite_independent_PDF_disagreements_or_failures_retained'
 except BaseException as e:failure=(summary['phase'],type(e).__name__)
 try:progress('post_input_verification');check(s)
 except BaseException as e:failure=failure or ('post_input_verification',type(e).__name__)
 try:
  progress('manifest');m=local/'manifest.json';write(m,{'files':{str(p):sha(p) for p in local.rglob('*') if p.is_file()}});summary['local_details']={'path':str(m),'sha256':sha(m)}
 except BaseException as e:failure=failure or ('manifest',type(e).__name__)
 if failure:summary.update(state='attempt_failed_retained',failed_phase=failure[0],error_type=failure[1])
 summary.update(phase='terminal_record',ended_utc=utc())
 try:atomic_json(RESULT,summary)
 except BaseException as e:print(json.dumps({'state':'terminal_result_write_failed','error_type':type(e).__name__}),flush=True);raise SystemExit(1)
 print(json.dumps({k:v for k,v in summary.items() if k!='local_details'},ensure_ascii=False))

def verify(s):
 check(s);r=read(RESULT);require(r.get('phase')=='terminal_record' and r['state']!='not_run','terminal evidence missing')
 require(r['implementation_sha256']==sha(__file__) and r['protocol_sha256']==sha(PROTO),'actual execution versions changed')
 require(sha(r['local_details']['path'])==r['local_details']['sha256'],'native manifest changed')
 for p,h in read(r['local_details']['path'])['files'].items():require(sha(p)==h,'native evidence changed')
 require(r['worker_method_calls']<=4 and not any(r[k] for k in ['source_GETs','dependency_GETs','economic_runs','PIT_admitted','model_admitted','full_goal_complete']),'scope escalated')
 print(json.dumps({'native_PDF_attempt_verified':True,'state':r['state'],'economic_runs':0}))

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--run',action='store_true');p.add_argument('--verify',action='store_true');p.add_argument('--counterexamples',action='store_true');p.add_argument('--worker');a=p.parse_args();require(sum(bool(v) for v in vars(a).values())==1,'select one mode')
 if a.counterexamples:counterexamples()
 else:
  s=read(PROTO)
  if a.worker:worker(s,int(a.worker))
  elif a.run:run(s)
  else:verify(s)
