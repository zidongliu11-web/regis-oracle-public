"""One bounded independent XLS comparison; all source cells stay in native storage."""
import argparse, hashlib, json, math, os, re, subprocess, sys, time, urllib.request, zipfile
from pathlib import Path, PurePosixPath
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[1]
PROTO=ROOT/'research/CSRC_XLS_INDEPENDENT_PROTOCOL_20261003.json'
RESULT=ROOT/'research/CSRC_XLS_INDEPENDENT_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def utc():return datetime.now(timezone.utc).isoformat()
def require(ok,reason):
 if not ok:raise ValueError(reason)

def canonical(v):
 if v is None or v=='':return ['empty']
 if isinstance(v,bool):return ['bool',v]
 if isinstance(v,(int,float)):
  require(math.isfinite(v),'nonfinite source cell')
  return ['number',format(float(v),'.17g')]
 if isinstance(v,str):return ['text',v]
 raise ValueError('unsupported cell type')

def members(tables):
 """Independent header/anchor walk, not the old parser or its normalizer."""
 out={};headers=0
 for sheet,table in enumerate(tables,1):
  sc=ic=None;group=None
  for row,cells in enumerate(table,1):
   text=['' if v is None else str(v).strip() for v in cells]
   hs=[i for i,v in enumerate(text) if '上市公司' in v and '代码' in v]
   hi=[i for i,v in enumerate(text) if '行业大类代码' in v]
   if hs or hi:
    require(len(hs)==len(hi)==1,'ambiguous source header')
    sc,ic=hs[0],hi[0];headers+=1;continue
   if sc is None:continue
   require(len(cells)>max(sc,ic),'shifted source columns')
   if text[ic]:
    require(re.fullmatch(r'\d{1,2}(?:\.0)?',text[ic]) is not None,'invalid class anchor')
    n=int(float(text[ic]));require(1<=n<=90,'class range');group=f'{n:02d}'
   v=cells[sc]
   if v is None or str(v).strip()=='':continue
   require(not isinstance(v,bool),'boolean stock identifier')
   if isinstance(v,(float,int)):
    require(math.isfinite(v) and int(v)==v and 0<=v<=999999,'fractional identifier')
    code=f'{int(v):06d}'
   else:
    code=str(v).strip();require(re.fullmatch(r'\d{6}',code) is not None,'identifier width')
   require(group is not None and code not in out,'missing class or duplicate identifier')
   out[code]={'class':group,'sheet':sheet,'row':row}
 require(headers>0 and len(out)>0,'empty membership')
 return out

def compare(old,new):
 oldkeys=set(old);newkeys=set(new)
 same=oldkeys&newkeys
 return {'missing':len(oldkeys-newkeys),'extra':len(newkeys-oldkeys),
         'class_disagreement':sum(old[c]['class']!=new[c]['class'] for c in same),
         'locator_disagreement':sum((old[c]['sheet'],old[c]['row'])!=(new[c]['sheet'],new[c]['row']) for c in same)}

def counterexamples():
 h=['行业大类代码','上市公司代码'];ok=members([[h,[1,1.0],['','000002']]])
 require(ok['000001']['class']=='01' and ok['000002']['class']=='01','anchor/zero preservation')
 rejected=0
 for body in [[[h,[1,1.2]]],[[h,[1,'001']]],[[h,[1,'000001'],['','000001']]],[[h,['','000001']]],[[['上市公司代码'],['000001']]]]:
  try:members(body)
  except ValueError:rejected+=1
 require(rejected==5,'malformed cells accepted')
 changed={k:dict(v) for k,v in ok.items()};changed['000001']['class']='02'
 require(compare(ok,changed)['class_disagreement']==1,'class mismatch masked')
 require(canonical(None)==canonical('') and canonical(1)==canonical(1.0) and canonical('000001')!=canonical(1),'canonical type coercion')
 print(json.dumps({'pure_counterexample_cases':8,'passed':True,'source_reads':0}))

def check(s):
 for p,h in s['input_hashes'].items():require(sha(ROOT/p)==h,'repository input changed')
 for p,h in s['native_input_hashes'].items():require(sha(p)==h,'native input changed')

class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):raise ValueError('redirect forbidden')
def fetch(url,target,cap,called):
 require(url.startswith('https://'),'nonHTTPS dependency')
 opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
 request=urllib.request.Request(url,headers={'User-Agent':'RegisOracle-local-reader/1'})
 try:response=opener.open(request,timeout=8)
 finally:called()  # Count this actual method invocation, even when it raises.
 with response:
  require(response.status==200 and response.geturl()==url,'unexpected dependency response')
  b=response.read(cap+1)
 require(len(b)<=cap,'dependency response cap')
 target.write_bytes(b)

def wheel(s,local,progress,called):
 spec=s['calamine'];meta=local/'pypi-metadata.json'
 progress('metadata_GET');fetch(spec['metadata_url'],meta,2097152,called)
 values=read(meta);require(values['info']['version']==spec['version'],'package version changed')
 files=[x for x in values['urls'] if x['filename']==spec['filename']]
 require(len(files)==1,'pinned platform wheel missing');f=files[0]
 require(f['digests']['sha256']==spec['sha256'] and f['size']<=1048576,'wheel hash/size changed')
 require(f['url'].startswith('https://files.pythonhosted.org/packages/'),'unexpected wheel host')
 archive=local/spec['filename'];progress('wheel_GET');fetch(f['url'],archive,1048576,called)
 require(archive.stat().st_size==f['size'] and sha(archive)==spec['sha256'],'wheel bytes mismatch')
 progress('package_extract');target=local/'reader';target.mkdir()
 with zipfile.ZipFile(archive) as z:
  entries=z.infolist();require(len(entries)<=64 and sum(x.file_size for x in entries)<=8388608,'wheel contents cap')
  license_seen=False
  for x in entries:
   name=PurePosixPath(x.filename)
   require(not name.is_absolute() and '..' not in name.parts and '\\' not in x.filename and ':' not in x.filename,'unsafe wheel member')
   require(not ((x.external_attr>>16)&0o170000)==0o120000,'wheel symlink')
   if x.is_dir():continue
   b=z.read(x)
   if 'licenses/' in x.filename and 'Permission is hereby granted, free of charge' in b.decode('utf-8',errors='ignore'):license_seen=True
   p=target.joinpath(*name.parts);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b)
  require(license_seen,'packaged MIT license not found')
  md=z.read('python_calamine-0.8.2.dist-info/METADATA').decode('utf-8')
  require('Version: 0.8.2' in md and 'Requires-Python: >=3.10' in md,'package metadata mismatch')
 return target

def worker(s,local,target):
 authorize(s)
 require(target==local/'reader' and target.is_dir() and not target.is_symlink() and target.resolve().is_relative_to(local.resolve()),'unapproved reader directory')
 require((local/'frozen_WORKFLOW_TASK.json').read_bytes()==(ROOT/'WORKFLOW_TASK.json').read_bytes(),'frozen Task mismatch')
 require((local/'frozen_protocol.json').read_bytes()==PROTO.read_bytes(),'frozen protocol mismatch')
 require((local/'fixed_precheck.json').read_bytes()==(ROOT/s['execution_gate']['fixed_precheck']).read_bytes(),'frozen approval mismatch')
 active=read(RESULT);require(active['phase']=='owned_worker_dispatch_pending' and not active['worker_launched'],'worker not awaiting approved dispatch')
 archive=local/s['calamine']['filename'];require(sha(archive)==s['calamine']['sha256'],'worker wheel changed')
 with zipfile.ZipFile(archive) as z:
  for entry in z.infolist():
   if entry.is_dir():continue
   p=target/entry.filename;require(p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(target.resolve()) and p.read_bytes()==z.read(entry),'extracted reader differs from pinned wheel')
 check(s)
 sys.path.insert(0,str(target));sys.path.insert(0,s['xlrd_wheel'])
 import xlrd
 from python_calamine import CalamineWorkbook
 require(xlrd.__version__=='2.0.2','old reader version mismatch')
 summaries=[]
 for q in s['quarters']:
  source=Path(q['source']);b=CalamineWorkbook.from_path(str(source))
  try:
   names=b.sheet_names;require(len(names)<=8,'sheet bound')
   ct=[b.get_sheet_by_name(n).to_python(skip_empty_area=False) for n in names]
  finally:
   close=getattr(b,'close',None)
   if close is not None:close()
  with (local/(q['quarter']+'.xlrd.log')).open('w',encoding='utf-8') as log:
   xb=xlrd.open_workbook(str(source),formatting_info=True,ignore_workbook_corruption=False,logfile=log)
  try:
   require(names==xb.sheet_names(),'sheet ordering mismatch')
   xt=[[[sh.cell_value(r,c) for c in range(sh.ncols)] for r in range(sh.nrows)] for sh in xb.sheets()]
   require(all(len(t)<=10000 and max(map(len,t),default=0)<=32 for t in ct+xt),'table dimensions bound')
   require(any(q['title'] in ''.join(str(v) for row in t[:10] for v in row).replace(' ','') for t in ct),'quarter title mismatch')
   # Pad only empty used-range borders; compare exact raw values without merged expansion.
   mismatch=0;checked=0;border=[]
   for a,c in zip(xt,ct):
    nr=max(len(a),len(c));nc=max(max(map(len,a),default=0),max(map(len,c),default=0));border.append({'xlrd_rows':len(a),'calamine_rows':len(c),'columns':nc})
    for row in range(nr):
     for col in range(nc):
      av=a[row][col] if row<len(a) and col<len(a[row]) else ''
      cv=c[row][col] if row<len(c) and col<len(c[row]) else ''
      mismatch+=canonical(av)!=canonical(cv);checked+=1
   new=members(ct);prior=read(q['old_members'])
   old={code:{'class':v['source_industry_major_numeric'],'sheet':v['source_locator']['table_or_sheet'],'row':v['source_locator']['row']} for code,v in prior['members'].items()}
   diff=compare(old,new)
   data=local/(q['quarter']+'.calamine-members.json');write(data,{'quarter':q['quarter'],'source_sha256':sha(source),'members':new,'comparison':diff})
   summary={'quarter':q['quarter'],'rows':len(new),'old_rows':len(old),'expected_rows':2492,'sheets':len(names),'raw_cells_compared':checked,'raw_cell_disagreements':mismatch,'range_borders':border,**diff}
   summary['independent_readers_agree']=mismatch==0 and not any(diff.values()) and len(old)==len(new)==2492
   summaries.append(summary)
  finally:xb.release_resources()
 write(local/'comparison.json',{'summaries':summaries,'all_two_agree':all(x['independent_readers_agree'] for x in summaries),'not_PIT_or_fullA_admission':True})

def versions_bound(t,s,review,code_h,proto_h):
 index={x['path']:x['sha256'] for x in t['artifacts']}
 require(index.get('research/check_csrc_xls_independent_reader.py')==code_h==s['input_hashes']['research/check_csrc_xls_independent_reader.py']==review['code_sha256'],'loaded implementation version not approved')
 require(index.get('research/CSRC_XLS_INDEPENDENT_PROTOCOL_20261003.json')==proto_h==review['protocol_sha256'],'loaded protocol version not approved')
 require(t['current_checkpoint'].get('E64_fixed_precheck')==review and review.get('precheck_recorded_before_source') is True,'successful precheck not recorded in active Task')

def authorize(s):
 sys.path.insert(0,str(ROOT));from scripts.workflow_guard import contract_hash
 t=read(ROOT/'WORKFLOW_TASK.json');gate=s['execution_gate']
 require(t['owner_session']==gate['owner_session'] and os.environ.get('CODEX_THREAD_ID')==gate['owner_session'],'owner/session mismatch')
 require(t['contract_sha256']==contract_hash(t)==gate['expected_contract_sha256'],'contract unsealed')
 review=read(ROOT/gate['fixed_precheck']);require(review['passed'] and review['fixed_agent']=='/root/fixed_reviewer_m24' and review['contract']==t['contract_sha256'],'actual fixed precheck missing')
 versions_bound(t,s,review,sha(__file__),sha(PROTO))
 return t

def run(s):
 require(read(RESULT)['state']=='not_run','source attempt already retained')
 authorize(s);check(s)
 local=Path(s['native_directory']);require('onedrive' not in str(local).lower() and not local.exists(),'native directory already exists or cloud path')
 from scripts.workflow_guard import atomic_json
 started=utc();summary={'state':'attempt_started_not_completed','started_utc':started,'phase':'before_native_create','dependency_GET_budget':2,'dependency_GET_attempts':0,'worker_launched':False,'new_original_source_GETs':0,'source_rows_to_AI_or_Git':0,'economic_runs':0,'PIT_admitted':False,'model_admitted':False,'full_goal_complete':False,'protocol_sha256':sha(PROTO),'implementation_sha256':sha(__file__)}
 def progress(phase):
  summary['phase']=phase
  atomic_json(RESULT,summary)
  print(json.dumps({'attempt_phase':phase,'dependency_GET_attempts':summary['dependency_GET_attempts']},ensure_ascii=False),flush=True)
 # Separate start record precedes all local creation, copies, network and source parsing.
 progress('before_native_create')
 def called():summary['dependency_GET_attempts']+=1
 failure=None
 try:
  local.mkdir(parents=True);progress('copy_frozen_inputs')
  (local/'frozen_WORKFLOW_TASK.json').write_bytes((ROOT/'WORKFLOW_TASK.json').read_bytes());(local/'frozen_protocol.json').write_bytes(PROTO.read_bytes());(local/'fixed_precheck.json').write_bytes((ROOT/s['execution_gate']['fixed_precheck']).read_bytes())
  target=wheel(s,local,progress,called)
  # Timeout monitors only this owned helper; no Office processes/global installation.
  progress('owned_worker_dispatch_pending')
  try:
   p=subprocess.run([sys.executable,'-X','utf8',str(Path(__file__).resolve()),'--worker',str(target)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
  except subprocess.TimeoutExpired as e:
   (local/'worker.stdout.partial.log').write_bytes(e.stdout or b'');(local/'worker.stderr.partial.log').write_bytes(e.stderr or b'');raise
  finally:summary['worker_launched']=True
  (local/'worker.stdout.log').write_bytes(p.stdout);(local/'worker.stderr.log').write_bytes(p.stderr)
  require(p.returncode==0,'owned comparison helper failed')
  observed=read(local/'comparison.json');summary.update(state='independent_reader_agreement' if observed['all_two_agree'] else 'independent_reader_disagreement_retained',summaries=observed['summaries'])
 except BaseException as e:failure=(summary['phase'],type(e).__name__)
 try:progress('post_input_verification');check(s)
 except BaseException as e:failure=failure or ('post_input_verification',type(e).__name__)
 summary['ended_utc']=utc()
 try:
  progress('manifest')
  manifest=local/'manifest.json';write(manifest,{'files':{str(p):sha(p) for p in local.rglob('*') if p.is_file()},'scope':'Original inputs unchanged; wheel/package and per-source comparisons native only.'})
  summary['local_details']={'path':str(manifest),'sha256':sha(manifest)}
 except BaseException as e:failure=failure or ('manifest',type(e).__name__)
 if failure:summary.update(state='independent_reader_attempt_failed_retained',failed_phase=failure[0],error_type=failure[1])
 summary['phase']='terminal_record'
 try:atomic_json(RESULT,summary)
 except BaseException as e:
  # Tool/caller must retain this actual terminal output and rc; do not claim a result file exists.
  print(json.dumps({'state':'terminal_result_write_failed','error_type':type(e).__name__,'prior_summary':{k:v for k,v in summary.items() if k!='local_details'}}),flush=True);raise SystemExit(1)
 print(json.dumps({k:v for k,v in summary.items() if k!='local_details'},ensure_ascii=False))

def verify(s):
 check(s);r=read(RESULT);require(r['state']!='not_run' and r.get('phase')=='terminal_record','terminal attempt missing')
 require(r['protocol_sha256']==sha(PROTO) and r['implementation_sha256']==sha(__file__),'capture version changed')
 require(sha(r['local_details']['path'])==r['local_details']['sha256'],'native manifest changed')
 for p,h in read(r['local_details']['path'])['files'].items():require(sha(p)==h,'native evidence changed')
 require(not any(r[k] for k in ['new_original_source_GETs','source_rows_to_AI_or_Git','economic_runs','PIT_admitted','model_admitted','full_goal_complete']),'scope escalated')
 print(json.dumps({'attempt_verified':True,'state':r['state'],'economic_runs':0}))

if __name__=='__main__':
 a=argparse.ArgumentParser();a.add_argument('--run',action='store_true');a.add_argument('--verify',action='store_true');a.add_argument('--counterexamples',action='store_true');a.add_argument('--worker');v=a.parse_args()
 require(sum(bool(x) for x in vars(v).values())==1,'select one mode')
 if v.counterexamples:counterexamples()
 else:
  s=read(PROTO)
  if v.worker:worker(s,Path(s['native_directory']),Path(v.worker));check(s)
  elif v.run:run(s)
  else:verify(s)
