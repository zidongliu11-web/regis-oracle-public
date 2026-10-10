"""Bounded local object/index discovery, with native-only command transcripts."""
import hashlib,json,os,re,subprocess,sys,time
from pathlib import Path
from datetime import datetime,timezone
R=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(R))
P=R/'research/GIT_ORPHAN_METADATA_PROTOCOL_20261003.json'
O=R/'research/GIT_ORPHAN_METADATA_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def eligible(name,s):return Path(name).suffix.lower() in s['eligible_suffixes'] and not any(w in name.lower() for w in s['excluded_path_words'])
def require_capture_gate(task,s,actual_inputs,versions):
 from scripts.workflow_guard import contract_hash
 gate=s['capture_execution_gate']
 assert task['status']=='in_progress' and task['owner_session']==gate['owner_session'] and task['id']==s['parent_task']
 assert task['contract_sha256']==gate['expected_contract_sha256']==contract_hash(task)
 assert len(task['artifacts'])==gate['artifact_count'] and len(task['checks'])==gate['check_count']
 assert actual_inputs==s['input_hashes']
 indexed={a['path']:a['sha256'] for a in task['artifacts']}
 assert versions['protocol_sha256']==indexed[P.relative_to(R).as_posix()]
 assert versions['implementation_sha256']==indexed[Path(__file__).relative_to(R).as_posix()]
 assert versions['loaded_protocol']==s
 assert actual_inputs[Path(__file__).relative_to(R).as_posix()]==versions['implementation_sha256']

def current_versions():
 return {'protocol_sha256':sha(P),'implementation_sha256':sha(__file__),'loaded_protocol':read(P)}
def before_capture(s):
 require_capture_gate(read(R/'WORKFLOW_TASK.json'),s,{p:sha(R/p) for p in s['input_hashes']},current_versions())
def capture_gate_counterexamples(s):
 import copy
 gate=s['capture_execution_gate'];fixture=Path(gate['frozen_test_task_path']);assert sha(fixture)==gate['frozen_test_task_sha256']
 task=read(fixture);hs={p:sha(R/p) for p in s['input_hashes']};versions=current_versions()
 # The archived task supplies immutable scope/owner/contract. Version fields
 # below form a synthetic positive fixture independent of future task scopes.
 for a in task['artifacts']:
  if a['path']==P.relative_to(R).as_posix():a['sha256']=versions['protocol_sha256']
  if a['path']==Path(__file__).relative_to(R).as_posix():a['sha256']=versions['implementation_sha256']
 require_capture_gate(task,s,hs,versions)
 rejected=0
 stale=copy.deepcopy(task);stale['contract_sha256']='0'*64
 unsealed=copy.deepcopy(task);unsealed['request']+=' synthetic changed request'
 wrong=copy.deepcopy(task);wrong['owner_session']='synthetic_other_session'
 for candidate in [stale,unsealed,wrong]:
  try:require_capture_gate(candidate,s,hs,versions)
  except AssertionError:rejected+=1
  else:raise AssertionError('unsafe capture gate accepted')
 changed=dict(hs);changed[next(iter(changed))]='0'*64
 try:require_capture_gate(task,s,changed,versions)
 except AssertionError:rejected+=1
 else:raise AssertionError('changed input accepted')
 assert rejected==4
 methods=[]
 command=copy.deepcopy(s);command['fsck_argv']=['fsck','--lost-found'];methods.append(command)
 limit=copy.deepcopy(s);limit['candidate_limit']+=1;methods.append(limit)
 for candidate in methods:
  try:require_capture_gate(task,candidate,hs,versions)
  except AssertionError:rejected+=1
  else:raise AssertionError('changed loaded method accepted')
 for field in ['protocol_sha256','implementation_sha256']:
  wrongversion=copy.deepcopy(versions);wrongversion[field]='0'*64
  try:require_capture_gate(task,s,hs,wrongversion)
  except AssertionError:rejected+=1
  else:raise AssertionError('unsealed file version accepted')
 assert rejected==8
 return {'synthetic_version_bound_archived_scope_accepted':True,'stale_contract_unsealed_contract_wrong_owner_changed_input_rejected':4,'unsealed_loaded_command_limit_and_method_file_versions_rejected':4,'total_rejected':8,'actual_capture_commands_in_tests':0,'historical_capture_sequence_not_repaired':True}

def execute(s):
 before_capture(s)
 D=Path(s['native_directory']);assert 'onedrive' not in str(D.resolve()).lower()
 assert not D.exists();D.mkdir(parents=True)
 write(D/'frozen_protocol.json',s)
 write(D/'implementation.py.sha256.json',{'sha256':sha(__file__)})
 receipts=[];totalstart=time.monotonic()
 env=dict(os.environ,GIT_TERMINAL_PROMPT='0',GCM_INTERACTIVE='never',GIT_LFS_SKIP_SMUDGE='1')
 def call(args):
  assert len(receipts)<s['max_commands']
  n=len(receipts);out=D/f'{n:02d}.stdout';err=D/f'{n:02d}.stderr';start=time.monotonic();why=None;killrc=None
  with out.open('xb') as fo,err.open('xb') as fe:
   p=subprocess.Popen(['git',*args],cwd=R,env=env,stdout=fo,stderr=fe,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
   while p.poll() is None:
    if out.stat().st_size+err.stat().st_size>s['metadata_byte_cap_per_command']:why='metadata_byte_cap'
    if time.monotonic()-start>s['command_wall_seconds'] or time.monotonic()-totalstart>s['total_wall_seconds']:why=why or 'wall_clock_cap'
    if why:
     if os.name=='nt':killrc=subprocess.run(['taskkill','/PID',str(p.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5,creationflags=subprocess.CREATE_NO_WINDOW).returncode
     else:p.kill()
     if p.poll() is None:p.kill()
     break
    time.sleep(.05)
   rc=p.wait(timeout=5)
  size=out.stat().st_size+err.stat().st_size
  if size>s['metadata_byte_cap_per_command']:why=why or 'metadata_byte_cap'
  item={'index':n,'argv':['git',*args],'actual_returncode':rc,'limit_failure':why,'owned_process_tree_kill_returncode':killrc,'stdout_path':str(out),'stdout_sha256':sha(out),'stdout_bytes':out.stat().st_size,'stderr_path':str(err),'stderr_sha256':sha(err),'stderr_bytes':err.stat().st_size,'elapsed_seconds':round(time.monotonic()-start,3),'attempts':1}
  receipts.append(item);write(D/'command_receipts.json',receipts)
  return None if rc!=0 or why else out.read_bytes()
 fsck=call(s['fsck_argv']);reflog=call(s['reflog_argv']);lfs=call(s['lfs_argv'])
 objects=[];unparsed=0
 if fsck is not None:
  for line in fsck.decode('utf-8',errors='replace').splitlines():
   m=re.fullmatch(r'(unreachable|dangling) (commit|tree|blob|tag) ([0-9a-f]{40,64})',line)
   if m:objects.append({'classification':m[1],'type':m[2],'oid':m[3]})
   elif line.strip():unparsed+=1
 refs=[];refinvalid=0
 if reflog is not None:
  for line in reflog.decode('ascii',errors='replace').splitlines():
   if re.fullmatch(r'[0-9a-f]{40,64}',line):refs.append(line)
   elif line.strip():refinvalid+=1
 commits=sorted(set(x['oid'] for x in objects if x['type']=='commit'))
 scan=commits[:s['commit_tree_limit']];candidates=[];treefail=0;scanned=0;excluded=0;truncated=False
 for oid in scan:
  if time.monotonic()-totalstart>s['total_wall_seconds']:break
  data=call(['ls-tree','-r','-l','-z',oid])
  if data is None:treefail+=1;continue
  scanned+=1
  for part in data.split(b'\0'):
   if not part:continue
   metadata,name=part.split(b'\t',1);fields=metadata.split();name=name.decode('utf-8',errors='replace')
   if not eligible(name,s):excluded+=1;continue
   if len(candidates)>=s['candidate_limit']:truncated=True;continue
   candidates.append({'commit':oid,'path':name,'object_type':fields[1].decode(),'oid':fields[2].decode(),'bytes':int(fields[3]) if fields[3]!=b'-' else None})
 lfsrows=[];lfsunparsed=0
 if lfs is not None:
  for line in lfs.decode('utf-8',errors='replace').splitlines():
   m=re.fullmatch(r'([0-9a-f]{64}) ([*-]) (.+)',line)
   if not m:
    if line.strip():lfsunparsed+=1
    continue
   if not eligible(m[3],s):excluded+=1;continue
   if len(lfsrows)>=s['candidate_limit']:truncated=True;continue
   lfsrows.append({'oid_sha256':m[1],'local_payload_marker':m[2],'path':m[3]})
 summary={'fsck_command_succeeded':fsck is not None,'fsck_unparsed_lines':unparsed,'unreachable_object_counts':{typ:len({x['oid'] for x in objects if x['type']==typ}) for typ in ['commit','tree','blob','tag']},'commit_trees_found':len(commits),'commit_trees_scanned':scanned,'tree_command_failures':treefail,'commit_tree_limit':s['commit_tree_limit'],'all_unreachable_commit_trees_scanned':len(commits)==scanned and fsck is not None,'reflog_command_succeeded':reflog is not None,'reflog_entry_count':len(refs),'reflog_unique_commit_count':len(set(refs)),'reflog_invalid_lines':refinvalid,'reflog_unreachable_commit_intersection':len(set(refs)&set(commits)),'eligible_tree_candidates':candidates,'eligible_LFS_pointer_candidates':lfsrows,'LFS_command_succeeded':lfs is not None,'LFS_unparsed_lines':lfsunparsed,'private_or_ineligible_paths_excluded':excluded,'candidate_list_truncated':truncated,'specific_target_metadata_found':{p:any(x['path']==p for x in candidates+lfsrows) for p in s['specific_missing_targets']},'unnamed_unreachable_blobs_not_read_or_certified':True,'original_payloads_recovered':0,'quote_or_account_rows_emitted':0,'fetch_commands':0,'lost_found_or_reflog_mutations':0,'licence_or_PIT_admitted':False,'new_economic_runs':0,'model_admitted':False,'full_goal_complete':False}
 write(D/'summary.json',summary)
 return {'summary':summary,'receipts':receipts,'native_protocol_sha256':sha(D/'frozen_protocol.json'),'native_summary_sha256':sha(D/'summary.json'),'native_receipts_sha256':sha(D/'command_receipts.json')}
def verify(s,r):
 assert r['protocol_sha256']==sha(P) and r['implementation_sha256']==sha(__file__)
 D=Path(s['native_directory']);assert read(D/'frozen_protocol.json')==s.get('original_capture_protocol',s)
 assert sha(D/'frozen_protocol.json')==r['capture']['native_protocol_sha256']
 assert sha(D/'summary.json')==r['capture']['native_summary_sha256'] and sha(D/'command_receipts.json')==r['capture']['native_receipts_sha256']
 assert read(D/'summary.json')==r['capture']['summary'] and read(D/'command_receipts.json')==r['capture']['receipts']
 receipts=r['capture']['receipts'];assert 3<=len(receipts)<=s['max_commands']
 for i,v in enumerate(receipts):
  assert v['index']==i and v['attempts']==1 and sha(v['stdout_path'])==v['stdout_sha256'] and sha(v['stderr_path'])==v['stderr_sha256']
  assert v['argv'][0]=='git' and v['argv'][1] in ['fsck','reflog','lfs','ls-tree']
  if i<3:assert v['argv'][1:]==s[['fsck_argv','reflog_argv','lfs_argv'][i]]
  else:assert v['argv'][1:4]==['ls-tree','-r','-l'] and v['argv'][4]=='-z' and re.fullmatch(r'[0-9a-f]{40,64}',v['argv'][5])
 f=r['capture']['summary'];assert f['original_payloads_recovered']==0 and f['new_economic_runs']==0 and not f['model_admitted']
 def raw(i):
  v=receipts[i];return None if v['actual_returncode']!=0 or v['limit_failure'] else Path(v['stdout_path']).read_bytes()
 fsck=raw(0);ref=raw(1);lfs=raw(2);objects=[];bad=0
 for line in ([] if fsck is None else fsck.decode('utf-8',errors='replace').splitlines()):
  m=re.fullmatch(r'(unreachable|dangling) (commit|tree|blob|tag) ([0-9a-f]{40,64})',line)
  if m:objects.append({'type':m[2],'oid':m[3]})
  elif line.strip():bad+=1
 assert f['fsck_command_succeeded']==(fsck is not None) and f['fsck_unparsed_lines']==bad
 assert f['unreachable_object_counts']=={typ:len({x['oid'] for x in objects if x['type']==typ}) for typ in ['commit','tree','blob','tag']}
 commits=sorted({x['oid'] for x in objects if x['type']=='commit'})
 assert f['commit_trees_found']==len(commits) and len(receipts[3:])<=s['commit_tree_limit']
 assert [v['argv'][5] for v in receipts[3:]]==commits[:len(receipts[3:])]
 scanned=sum(v['actual_returncode']==0 and not v['limit_failure'] for v in receipts[3:])
 assert f['commit_trees_scanned']==scanned and f['tree_command_failures']==len(receipts[3:])-scanned
 assert f['all_unreachable_commit_trees_scanned']==(len(commits)==scanned and fsck is not None)
 lines=[] if ref is None else ref.decode('ascii',errors='replace').splitlines();valid=[x for x in lines if re.fullmatch(r'[0-9a-f]{40,64}',x)]
 assert f['reflog_entry_count']==len(valid) and f['reflog_unique_commit_count']==len(set(valid)) and f['reflog_invalid_lines']==sum(bool(x.strip()) and x not in valid for x in lines)
 assert f['reflog_command_succeeded']==(ref is not None) and f['reflog_unreachable_commit_intersection']==len(set(valid)&set(commits))
 expected=[];lfsbad=0
 for line in ([] if lfs is None else lfs.decode('utf-8',errors='replace').splitlines()):
  m=re.fullmatch(r'([0-9a-f]{64}) ([*-]) (.+)',line)
  if not m:
   if line.strip():lfsbad+=1
  elif eligible(m[3],s) and len(expected)<s['candidate_limit']:expected.append({'oid_sha256':m[1],'local_payload_marker':m[2],'path':m[3]})
 assert f['eligible_LFS_pointer_candidates']==expected and f['LFS_unparsed_lines']==lfsbad and f['LFS_command_succeeded']==(lfs is not None)
 expected=[]
 for i,v in enumerate(receipts[3:],3):
  data=raw(i)
  if data is None:continue
  for part in data.split(b'\0'):
   if not part:continue
   metadata,name=part.split(b'\t',1);fields=metadata.split();name=name.decode('utf-8',errors='replace')
   if eligible(name,s) and len(expected)<s['candidate_limit']:expected.append({'commit':v['argv'][5],'path':name,'object_type':fields[1].decode(),'oid':fields[2].decode(),'bytes':int(fields[3]) if fields[3]!=b'-' else None})
 assert f['eligible_tree_candidates']==expected
 assert f['specific_target_metadata_found']=={p:any(x['path']==p for x in f['eligible_tree_candidates']+f['eligible_LFS_pointer_candidates']) for p in s['specific_missing_targets']}
 assert all(eligible(v['path'],s) for v in f['eligible_tree_candidates']+f['eligible_LFS_pointer_candidates'])
 assert r['parent_status']=='in_progress' and not r['full_goal_complete']
 if 'metadata_size_supplement' in s:
  q=s['metadata_size_supplement'];z=r['metadata_size_supplement'];E=Path(q['native_directory'])
  for p,h in s['post_capture_native_input_sha256'].items():assert sha(p)==h
  assert read(E/'protocol.json')==q and sha(E/'receipt.json')==z['receipt_sha256'] and sha(E/'sizes.json')==z['sizes_sha256']
  receipt=read(E/'receipt.json');assert receipt['argv']==['git',*q['argv']] and receipt['attempts']==1 and receipt['actual_returncode']==0 and sha(receipt['stdout_path'])==receipt['stdout_sha256'] and sha(receipt['stderr_path'])==receipt['stderr_sha256']
  rows=[line.split() for line in Path(receipt['stdout_path']).read_text(encoding='ascii').splitlines()]
  sizes=[{'oid':v[0],'type':v[1],'bytes':int(v[2])} for v in rows]
  assert [v['oid'] for v in sizes]==q['oids'] and all(v['type']=='blob' for v in sizes) and sizes==read(E/'sizes.json')
  expected=size_summary(sizes);assert z['summary']==expected and len(receipts)+1<=s['max_commands']
def size_summary(sizes):
 n=[v['bytes'] for v in sizes]
 return {'object_count':len(n),'min_bytes':min(n) if n else None,'max_bytes':max(n) if n else None,'objects_at_least_4096bytes':sum(z>=4096 for z in n),'objects_at_least_1MiB':sum(z>=1048576 for z in n),'objects_matching_SQLite_mainfile_page_length_necessary_condition':sum(z>=512 and z%512==0 for z in n),'SQLite_header_read_or_format_identified':False,'compressed_truncated_or_other_formats_not_ruled_out':True,'new_Git_commands':1,'blob_payload_reads':0,'original_payloads_recovered':0,'format_or_data_contents_identified':False}
def size_supplement(s):
 before_capture(s)
 r=read(O);assert 'metadata_size_supplement' not in r
 q=s['metadata_size_supplement'];E=Path(q['native_directory']);assert not E.exists();E.mkdir()
 for p,h in s['input_hashes'].items():assert sha(R/p)==h
 for p,h in s['post_capture_native_input_sha256'].items():assert sha(p)==h
 assert len(q['oids'])<=q['object_limit'] and all(re.fullmatch(r'[0-9a-f]{40,64}',x) for x in q['oids']) and len(r['capture']['receipts'])+1<=s['max_commands']
 write(E/'protocol.json',q);out=E/'stdout';err=E/'stderr';payload=('\n'.join(q['oids'])+'\n').encode('ascii');assert hashlib.sha256(payload).hexdigest()==q['oid_input_sha256']
 start=time.monotonic()
 with out.open('xb') as fo,err.open('xb') as fe:
  p=subprocess.run(['git',*q['argv']],cwd=R,input=payload,stdout=fo,stderr=fe,timeout=q['wall_seconds'],env=dict(os.environ,GIT_TERMINAL_PROMPT='0',GCM_INTERACTIVE='never'),creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
 receipt={'argv':['git',*q['argv']],'actual_returncode':p.returncode,'attempts':1,'elapsed_seconds':round(time.monotonic()-start,3),'stdout_path':str(out),'stdout_sha256':sha(out),'stderr_path':str(err),'stderr_sha256':sha(err),'stdout_bytes':out.stat().st_size,'stderr_bytes':err.stat().st_size}
 write(E/'receipt.json',receipt);assert p.returncode==0 and out.stat().st_size+err.stat().st_size<=q['metadata_byte_cap']
 rows=[line.split() for line in out.read_text(encoding='ascii').splitlines()];assert all(len(v)==3 and re.fullmatch(r'[0-9a-f]{40,64}',v[0]) and v[1]=='blob' and v[2].isdigit() for v in rows)
 sizes=[{'oid':v[0],'type':v[1],'bytes':int(v[2])} for v in rows];assert [v['oid'] for v in sizes]==q['oids'];write(E/'sizes.json',sizes)
 r.update(protocol_sha256=sha(P),implementation_sha256=sha(__file__),metadata_size_supplement={'observed_utc':datetime.now(timezone.utc).isoformat(),'receipt_sha256':sha(E/'receipt.json'),'sizes_sha256':sha(E/'sizes.json'),'summary':size_summary(sizes)},native_path_derivation_summary={k:v for k,v in read(Path(q['derivation_result_path'])).items() if k!='mapped_paths_metadata'})
 write(O,r);verify(s,r);print(json.dumps(r['metadata_size_supplement']['summary']))
def main():
 s=read(P)
 if '--size-supplement' in sys.argv:size_supplement(s);return
 if '--verify' in sys.argv:
  r=read(O);verify(s,r);cases=capture_gate_counterexamples(s);print(json.dumps({'native_transcript_hashes_verified':True,'commands_not_reexecuted':True,'payloads_recovered':0,'model_admitted':False,'capture_gate_counterexamples':cases}));return
 assert '--capture' in sys.argv and read(O)['state']=='not_run'
 for p,h in s['input_hashes'].items():assert sha(R/p)==h
 c=execute(s);r={'schema':'regis.git_orphan_metadata_result.v1','state':'actual_local_metadata_discovery_only','created_utc':datetime.now(timezone.utc).isoformat(),'protocol_sha256':sha(P),'implementation_sha256':sha(__file__),'capture':c,'parent_status':'in_progress','full_goal_complete':False,'model_admitted':False}
 write(O,r);verify(s,r)
 f=c['summary'];print(json.dumps({k:f[k] for k in ['unreachable_object_counts','reflog_entry_count','LFS_command_succeeded','specific_target_metadata_found','original_payloads_recovered']}))
if __name__=='__main__':main()
