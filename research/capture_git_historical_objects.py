"""Bounded local Git history names/types/sizes; never read candidate blob content."""
import hashlib,json,os,subprocess,sys,time
from datetime import datetime,timezone
from pathlib import Path
R=Path(__file__).resolve().parents[1]
P=R/'research/GIT_HISTORICAL_OBJECTS_PROTOCOL_20261002.json'
O=R/'research/GIT_HISTORICAL_OBJECTS_RECEIPT_20261002.json'
def now():return datetime.now(timezone.utc).isoformat()
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def digest(b):return hashlib.sha256(b).hexdigest()
def save(v):O.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def candidates(data,p):
 rows=[];excluded=0
 for line in data.decode('utf-8',errors='strict').splitlines():
  oid,sep,name=line.partition(' ')
  if not sep or Path(name).suffix.lower() not in p['eligible_suffixes']:continue
  if any(x in name.lower() for x in p['excluded_path_words']):excluded+=1;continue
  rows.append({'object_id':oid,'path':name})
 return rows,excluded
def verify():
 p=json.loads(P.read_text(encoding='utf-8'));v=json.loads(O.read_text(encoding='utf-8'))
 assert v['protocol_sha256']==sha(P) and v['blob_content_reads']==v['market_or_financial_account_rows_read']==0
 assert v['data_admitted'] is v['economic_run'] is v['model_admitted'] is v['all_historical_data_absent_certified'] is False
 assert len(v['commands'])<=p['max_git_commands']==5
 refs_argv=['git','for-each-ref','--format=%(refname) %(objectname)']
 prefix=[refs_argv,['git','rev-parse','--is-shallow-repository'],p['objects_inventory_argv']]
 for c,expected in zip(v['commands'],prefix):assert c['argv']==expected
 for c in v['commands']:
  if 'raw_path' not in c:assert c.get('failure') or v.get('process_timeout');continue
  raw=Path(c['raw_path']);assert raw.resolve().parent==Path(p['raw_directory']).resolve() and 'onedrive' not in str(raw.resolve()).lower()
  assert raw.stat().st_size==c['raw_bytes']<=p['metadata_byte_cap_per_command'] and sha(raw)==c['raw_sha256']
 if not v.get('failure') and not v.get('process_timeout'):
  expected=prefix+([p['object_metadata_argv']] if v.get('candidate_metadata') else [])+[refs_argv]
  assert [c['argv'] for c in v['commands']]==expected and all(c['returncode']==0 for c in v['commands'])
  assert v['shallow_repository']==(Path(v['commands'][1]['raw_path']).read_text(encoding='utf-8').strip()=='true')
  assert v['refs_before_sha256']==v['refs_after_sha256']==p['refs_stdout_sha256']
  rows,excluded=candidates(Path(v['commands'][2]['raw_path']).read_bytes(),p)
  assert len(rows)==v['candidate_object_names_count'] and excluded==v['excluded_sensitive_named_paths_count']
  assert rows[:p['candidate_metadata_limit']]==[{k:x[k] for k in ['object_id','path']} for x in v['candidate_metadata']]
  out=Path(v['commands'][3]['raw_path']).read_text(encoding='utf-8').splitlines() if rows else []
  assert len(out)==len(v['candidate_metadata'])
  for row,line in zip(v['candidate_metadata'],out):
   oid,typ,size=line.split();assert oid==row['object_id'] and typ==row['object_type'] and int(size)==row['object_bytes']
  assert v['specific_missing_targets_observed']=={x:any(row['path']==x for row in rows) for x in p['specific_missing_targets']}
 print(json.dumps({'local_history_metadata_receipt_verified':True,'candidate_names':v.get('candidate_object_names_count'),'metadata_window':len(v.get('candidate_metadata',[])),'shallow':v.get('shallow_repository'),'failure':v.get('failure'),'blob_content_reads':0,'data_admitted':False}))
if len(sys.argv)>1 and sys.argv[1]=='--verify':verify();raise SystemExit(0)
p=json.loads(P.read_text(encoding='utf-8'))
if len(sys.argv)==1:
 assert not O.exists(),'one-shot receipt exists'
 try:r=subprocess.run([sys.executable,'-X','utf8',__file__,'worker'],cwd=R,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=55,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
 except subprocess.TimeoutExpired:
  v=json.loads(O.read_text(encoding='utf-8')) if O.exists() else {'protocol_sha256':sha(P),'commands':[]}
  v.update({'process_timeout':True,'failure':'worker_wall_limit','ended_utc':now(),'data_admitted':False});save(v);print(json.dumps({'process_timeout':True,'data_admitted':False}));raise SystemExit(1)
 print(r.stdout.decode('utf-8',errors='replace').strip());raise SystemExit(r.returncode)
v={'schema':'regis.git.historical_objects_receipt.v1','protocol_sha256':sha(P),'started_utc':now(),'commands':[],'blob_content_reads':0,'market_or_financial_account_rows_read':0,'data_admitted':False,'economic_run':False,'model_admitted':False,'all_historical_data_absent_certified':False,'process_timeout':False};save(v)
deadline=time.monotonic()+p['worker_wall_limit_seconds']
def git(args,data=None):
 assert len(v['commands'])<p['max_git_commands'] and time.monotonic()<deadline
 c={'argv':['git',*args],'started_utc':now()};v['commands'].append(c);save(v)
 try:
  r=subprocess.run(c['argv'],cwd=R,input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=min(15,max(1,deadline-time.monotonic())),env=dict(os.environ,GIT_TERMINAL_PROMPT='0',GCM_INTERACTIVE='never',GIT_NO_LAZY_FETCH='1'),creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
  c['returncode']=r.returncode;assert r.returncode==0,'git_metadata_command_failed'
  assert len(r.stdout)<=p['metadata_byte_cap_per_command'],'metadata_byte_cap_exceeded'
  raw=Path(p['raw_directory'])/('command_'+str(len(v['commands'])-1)+'.txt');raw.write_bytes(r.stdout)
  c.update({'raw_path':str(raw),'raw_bytes':len(r.stdout),'raw_sha256':sha(raw),'ended_utc':now()});save(v);return r.stdout
 except Exception as e:c['failure']=str(e) if isinstance(e,AssertionError) else type(e).__name__;save(v);raise
try:
 refs=git(['for-each-ref','--format=%(refname) %(objectname)']);v['refs_before_sha256']=digest(refs);assert digest(refs)==p['refs_stdout_sha256'],'refs_changed_before_read'
 v['shallow_repository']=git(['rev-parse','--is-shallow-repository']).decode().strip()=='true'
 data=git(p['objects_inventory_argv'][1:]);rows,excluded=candidates(data,p)
 v['candidate_object_names_count']=len(rows);v['excluded_sensitive_named_paths_count']=excluded
 v['specific_missing_targets_observed']={x:any(row['path']==x for row in rows) for x in p['specific_missing_targets']}
 picked=rows[:p['candidate_metadata_limit']];v['candidate_metadata']=picked
 if picked:
  result=git(p['object_metadata_argv'][1:],''.join(x['object_id']+'\n' for x in picked).encode()).decode().splitlines();assert len(result)==len(picked),'batch_metadata_count'
  for row,line in zip(picked,result):
   oid,typ,size=line.split();assert oid==row['object_id'],'batch_metadata_identity';row.update({'object_type':typ,'object_bytes':int(size)})
 v['refs_after_sha256']=digest(git(['for-each-ref','--format=%(refname) %(objectname)']));assert v['refs_after_sha256']==v['refs_before_sha256'],'refs_changed_during_read'
except Exception as e:v['failure']=str(e) if isinstance(e,AssertionError) else type(e).__name__
v['ended_utc']=now();save(v)
print(json.dumps({'candidate_object_names_count':v.get('candidate_object_names_count'),'metadata_window':len(v.get('candidate_metadata',[])),'specific_missing_targets_observed':v.get('specific_missing_targets_observed'),'failure':v.get('failure'),'blob_content_reads':0,'data_admitted':False}))
