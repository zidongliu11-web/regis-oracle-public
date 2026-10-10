"""Native streaming format triage. No body storage, field decode or export."""
import hashlib,io,json,os,re,subprocess,sys,threading,time
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R))
P=R/'research/GIT_BLOB_FORMAT_PROTOCOL_20261003.json'
O=R/'research/GIT_BLOB_FORMAT_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def versions():return {'protocol_sha256':sha(P),'implementation_sha256':sha(__file__),'loaded_protocol':read(P)}
def gate(task,s,hs,v):
 from scripts.workflow_guard import contract_hash
 g=s['capture_execution_gate'];index={a['path']:a['sha256'] for a in task['artifacts']}
 assert task['status']=='in_progress' and task['id']==s['parent_task'] and task['owner_session']==g['owner_session']
 assert task['contract_sha256']==g['expected_contract_sha256']==contract_hash(task)
 assert len(task['artifacts'])==g['artifact_count'] and len(task['checks'])==g['check_count']
 assert hs==s['input_hashes'] and v['loaded_protocol']==s
 assert v['protocol_sha256']==index[P.relative_to(R).as_posix()]
 assert v['implementation_sha256']==index[Path(__file__).relative_to(R).as_posix()]==hs[Path(__file__).relative_to(R).as_posix()]
 assert s['argv']==['cat-file','--batch'] and s['object_limit']==102 and s['object_byte_cap']==262144 and s['total_body_byte_cap']==16777216
 assert len(s['objects'])==102 and len({z['oid'] for z in s['objects']})==102
 assert all(z['type']=='blob' and re.fullmatch(r'[0-9a-f]{40}',z['oid']) and 0<z['bytes']<=s['object_byte_cap'] for z in s['objects'])
 assert sum(z['bytes'] for z in s['objects'])<=s['total_body_byte_cap']
def predicates(head,tail,n):
 sqlmagic=head[:16]==b'SQLite format 3\0';page=int.from_bytes(head[16:18],'big') if len(head)>=18 else 0
 page=65536 if page==1 else page
 sqlite=sqlmagic and page in [512,1024,2048,4096,8192,16384,32768,65536] and n>=page and n%page==0
 parmagic=head[:4]==b'PAR1' and tail[-4:]==b'PAR1'
 metalen=int.from_bytes(tail[:4],'little') if len(tail)==8 else 0
 parquet=parmagic and n>=12 and 0<metalen<=n-12
 gzip=head[:3]==b'\x1f\x8b\x08' and len(head)>=10 and head[3]&0xe0==0 and n>=18
 lead=head[3:] if head.startswith(b'\xef\xbb\xbf') else head
 return {'SQLite_magic_and_page_length_plausible':sqlite,'SQLite_magic_only':sqlmagic,'Parquet_magic_and_footer_length_plausible':parquet,'Parquet_magic_only':parmagic,'gzip_header_length_plausible':gzip,'JSON_container_prefix_only':lead.lstrip(b' \t\r\n')[:1] in [b'{',b'[']}
def stream_object(pipe,z,cap):
 header=pipe.readline(200);parts=header.rstrip(b'\n').split(b' ')
 if not header.endswith(b'\n') or len(parts)!=3:raise ValueError('frame_header')
 if parts[0].decode('ascii')!=z['oid'] or parts[1]!=b'blob' or not parts[2].isdigit() or int(parts[2])!=z['bytes']:raise ValueError('frame_identity_type_size')
 n=z['bytes']
 if n>cap:raise ValueError('body_cap')
 gitsha=hashlib.sha1(b'blob '+str(n).encode('ascii')+b'\0');digest=hashlib.sha256();head=b'';tail=b'';remaining=n
 while remaining:
  chunk=pipe.read(min(32768,remaining))
  if not chunk:raise ValueError('truncated_body')
  remaining-=len(chunk);gitsha.update(chunk);digest.update(chunk)
  if len(head)<64:head=(head+chunk)[:64]
  tail=(tail+chunk)[-8:]
 if pipe.read(1)!=b'\n':raise ValueError('frame_terminator')
 if gitsha.hexdigest()!=z['oid']:raise ValueError('object_digest')
 return {'oid':z['oid'],'bytes':n,'sha256':digest.hexdigest(),'git_object_hash_verified':True,'predicates':predicates(head,tail,n)}
def boundary_tests():
 def obj(body):return {'oid':hashlib.sha1(b'blob '+str(len(body)).encode()+b'\0'+body).hexdigest(),'type':'blob','bytes':len(body)}
 def frame(z,body):return (z['oid']+' blob '+str(z['bytes'])+'\n').encode()+body+b'\n'
 b=b'synthetic-not-market-record';z=obj(b);assert stream_object(io.BytesIO(frame(z,b)),z,256)['git_object_hash_verified']
 failures=0
 for raw,cap in [(frame(z,b)[:-2],256),(frame(z,b)[:-1]+b'x',256),(frame(z,b.replace(b'synthetic',b'alteredxx')),256),(frame(z,b),4)]:
  try:stream_object(io.BytesIO(raw),z,cap)
  except ValueError:failures+=1
  else:raise AssertionError('bad synthetic frame accepted')
 assert failures==4
 assert not predicates(b'PAR1',b'PAR1',4)['Parquet_magic_and_footer_length_plausible']
 assert not predicates(b'PAR1',b'\xff\xff\xff\xffPAR1',16)['Parquet_magic_and_footer_length_plausible']
 assert predicates(b'PAR1',b'\x04\0\0\0PAR1',16)['Parquet_magic_and_footer_length_plausible']
 assert not predicates(b'SQLite format 3\0\x10\0',b'0'*8,4095)['SQLite_magic_and_page_length_plausible']
 assert predicates(b'SQLite format 3\0\x10\0',b'0'*8,4096)['SQLite_magic_and_page_length_plausible']
 assert not predicates(b'\x1f\x8b\x08\xe0'+b'0'*6,b'0'*8,18)['gzip_header_length_plausible']
 return {'bad_stream_frames_rejected':4,'magic_layout_boundary_assertions':6,'actual_Git_commands_in_tests':0}
def gate_tests(s,task):
 import copy
 hs={p:sha(R/p) for p in s['input_hashes']};v=versions();gate(task,s,hs,v);count=0
 attempts=[]
 stale=copy.deepcopy(task);stale['contract_sha256']='0'*64;attempts.append((stale,s,hs,v))
 changed=copy.deepcopy(task);changed['request']+=' changed';attempts.append((changed,s,hs,v))
 owner=copy.deepcopy(task);owner['owner_session']='changed';attempts.append((owner,s,hs,v))
 ih=dict(hs);ih[next(iter(ih))]='0'*64;attempts.append((task,s,ih,v))
 for key,value in [('argv',['fsck','--lost-found']),('object_limit',103)]:
  method=copy.deepcopy(s);method[key]=value;attempts.append((task,method,hs,v))
 for key in ['protocol_sha256','implementation_sha256']:
  fileversion=copy.deepcopy(v);fileversion[key]='0'*64;attempts.append((task,s,hs,fileversion))
 for args in attempts:
  try:gate(*args)
  except AssertionError:count+=1
  else:raise AssertionError('unsafe gate accepted')
 assert count==8
 return {'version_bound_native_frozen_Task_accepted':True,'invalid_contract_owner_input_method_versions_rejected':8,'actual_Git_commands_in_tests':0}
def summary(rows):
 counts={k:sum(z['predicates'][k] for z in rows) for k in predicates(b'',b'',0)}
 return {'objects_streamed':len(rows),'body_bytes_streamed_native_memory':sum(z['bytes'] for z in rows),'all_streamed_object_hashes_verified':all(z['git_object_hash_verified'] for z in rows),'predicate_counts':counts,'binary_format_candidates':sum(any(z['predicates'][k] for k in ['SQLite_magic_only','Parquet_magic_only','gzip_header_length_plausible']) for z in rows),'existing_archived_Task_fingerprint_matches':sum(bool(z.get('known_metadata_file_sha_matches')) for z in rows),'format_or_dataset_identity_certified':False,'CSV_other_compressed_encrypted_or_truncated_formats_not_excluded':True,'body_or_prefix_text_exported':0,'body_files_written':0,'field_values_decoded':0,'original_dataset_recovered':0,'new_economic_runs':0,'model_admitted':False,'full_goal_complete':False}
def cleanup_process(p,timer):
 errors=[]
 def attempt(label,action):
  try:return action()
  except Exception as e:errors.append({'stage':label,'error_type':type(e).__name__});return None
 if timer:attempt('timer_cancel',timer.cancel)
 if p:
  live=attempt('poll',p.poll)
  if live is None:attempt('kill',p.kill)
  attempt('wait',lambda:p.wait(timeout=5))
  if p.stdout:attempt('stdout_close',p.stdout.close)
  if p.stdin and not p.stdin.closed:attempt('stdin_close',p.stdin.close)
 return errors
def persist_terminal(s,D,rows,receipt):
 errors=[]
 def attempt(stage,action):
  try:return True,action()
  except Exception as e:errors.append({'stage':stage,'error_type':type(e).__name__});return False,None
 attempt('predicate_rows_write',lambda:write(D/'predicate_rows.json',rows))
 receipt['predicate_rows_sha256']=attempt('predicate_rows_sha',lambda:sha(D/'predicate_rows.json'))[1]
 receipt['frozen_protocol_sha256']=attempt('frozen_protocol_sha',lambda:sha(D/'frozen_protocol.json'))[1]
 receipt['frozen_task_sha256']=attempt('frozen_task_sha',lambda:sha(D/'frozen_task.json'))[1]
 receipt['stderr_sha256']=attempt('stderr_sha',lambda:sha(D/'stderr'))[1]
 receipt['implementation_sha256']=attempt('implementation_sha',lambda:sha(__file__))[1]
 receipt['recording_errors']=errors
 attempt('receipt_write',lambda:write(D/'receipt.json',receipt))
 receipt_sha=attempt('receipt_sha',lambda:sha(D/'receipt.json'))[1]
 protocol_sha=attempt('protocol_sha',lambda:sha(P))[1];implementation_sha=attempt('implementation_sha',lambda:sha(__file__))[1]
 failed=receipt['failure_type'] is not None or bool(receipt['cleanup_errors']) or bool(receipt['watchdog_errors']) or receipt['wall_cap_reached'] or bool(errors)
 r={'schema':'regis.git_blob_format_result.v1','state':'failed_partial_native_format_triage' if failed else 'actual_native_format_triage_only','created_utc':datetime.now(timezone.utc).isoformat(),'protocol_sha256':protocol_sha,'implementation_sha256':implementation_sha,'native_receipt_sha256':receipt_sha,'terminal_recording_errors':errors,'summary':summary(rows),'parent_status':'in_progress','model_admitted':False,'full_goal_complete':False}
 saved=attempt('result_write',lambda:write(O,r))[0]
 if not saved:
  r['state']='failed_partial_native_format_triage'
  attempt('fallback_terminal_write',lambda:write(D/'terminal_failure.json',r))
 return r
def recording_failure_test():
 original_write,original_sha=globals()['write'],globals()['sha'];cases=[]
 try:
  for failed_name in ['predicate_rows.json','receipt.json',O.name,'sha_read']:
   attempted=[];saved={}
   def sink(path,value):
    attempted.append(Path(path).name)
    if Path(path).name==failed_name:raise PermissionError('synthetic_recording_failure')
    saved[Path(path).name]=value
   def hash_sink(path):
    if failed_name=='sha_read':raise PermissionError('synthetic_hash_read_failure')
    return 'e'*64
   globals()['write']=sink;globals()['sha']=hash_sink
   c={'failure_type':None,'cleanup_errors':[],'watchdog_errors':[],'wall_cap_reached':False}
   r=persist_terminal({},Path('synthetic_native_directory'),[],c)
   assert 'receipt.json' in attempted and O.name in attempted and r['state']=='failed_partial_native_format_triage' and r['terminal_recording_errors']
   if failed_name==O.name:assert 'terminal_failure.json' in saved
   cases.append(failed_name)
 finally:globals()['write'],globals()['sha']=original_write,original_sha
 return {'independent_predicate_receipt_result_write_and_hash_failures_kept_failed':len(cases),'all_terminal_record_paths_attempted':True,'actual_processes_or_files_created':0}
def cleanup_failure_test():
 class BadPipe:
  closed=False
  def close(self):raise PermissionError('synthetic_pipe_cleanup')
 class BadProcess:
  stdout=BadPipe();stdin=BadPipe()
  def poll(self):return None
  def kill(self):raise PermissionError('synthetic_kill')
  def wait(self,timeout):raise subprocess.TimeoutExpired('synthetic_owned_process',timeout)
 errors=cleanup_process(BadProcess(),None)
 assert {z['stage'] for z in errors}=={'kill','wait','stdout_close','stdin_close'}
 # Exercise the real record writer with in-memory sinks and hashes. No real
 # subprocess, file or candidate body is involved in this failure test.
 original_write,original_sha=globals()['write'],globals()['sha'];saved={}
 try:
  globals()['write']=lambda path,value:saved.__setitem__(str(path),value)
  globals()['sha']=lambda path:'f'*64
  c={'failure_type':'ValueError','cleanup_errors':errors,'watchdog_errors':[],'wall_cap_reached':False}
  r=persist_terminal({},Path('synthetic_native_directory'),[],c)
  assert 'synthetic_native_directory/receipt.json' in saved or str(Path('synthetic_native_directory')/'receipt.json') in saved
  assert str(O) in saved and r['state']=='failed_partial_native_format_triage'
 finally:globals()['write'],globals()['sha']=original_write,original_sha
 return {'pipeline_plus_four_cleanup_errors_recorded':True,'terminal_receipt_and_failed_result_attempted':True,'actual_processes_or_files_created':0}
def capture(s):
 task=read(R/'WORKFLOW_TASK.json');hs={p:sha(R/p) for p in s['input_hashes']};gate(task,s,hs,versions())
 for path,h in s['native_inputs'].items():assert sha(path)==h
 for path,h in s['known_metadata_reference_sha256'].items():assert sha(R/path)==h
 assert read(O)['state']=='not_run';D=Path(s['native_directory']);assert 'onedrive' not in str(D.resolve()).lower() and not D.exists();D.mkdir()
 write(D/'frozen_protocol.json',s);write(D/'frozen_task.json',task);write(D/'implementation_hash.json',{'sha256':sha(__file__)})
 payload=''.join(z['oid']+'\n' for z in s['objects']).encode('ascii');assert hashlib.sha256(payload).hexdigest()==s['oid_input_sha256']
 rows=[];failure=None;start=time.monotonic();timed_out=[];watchdog_errors=[];err=D/'stderr';p=None;timer=None
 try:
  with err.open('xb') as fe:
   p=subprocess.Popen(['git',*s['argv']],cwd=R,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=fe,env=dict(os.environ,GIT_TERMINAL_PROMPT='0',GCM_INTERACTIVE='never',GIT_NO_LAZY_FETCH='1'),creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
   def stop():
    try:
     if p.poll() is None:timed_out.append(True);p.kill()
    except Exception as e:watchdog_errors.append(type(e).__name__)
   timer=threading.Timer(s['wall_seconds'],stop);timer.daemon=True;timer.start()
   for z in s['objects']:
    p.stdin.write((z['oid']+'\n').encode('ascii'));p.stdin.flush()
    row=stream_object(p.stdout,z,s['object_byte_cap']);row['known_metadata_file_sha_matches']=[path for path,h in s['known_metadata_reference_sha256'].items() if row['sha256']==h];rows.append(row)
   p.stdin.close()
   if p.stdout.read(1)!=b'':raise ValueError('unexpected_trailing_output')
   if p.wait(timeout=5)!=0:raise ValueError('nonzero_Git_returncode')
 except Exception as e:failure=type(e).__name__
 finally:
  cleanup_errors=cleanup_process(p,timer)
  receipt={'argv':['git',*s['argv']],'attempts':1,'actual_returncode':p.returncode if p else None,'failure_type':failure,'cleanup_errors':cleanup_errors,'watchdog_errors':watchdog_errors,'wall_cap_reached':bool(timed_out),'elapsed_seconds':round(time.monotonic()-start,3),'stdin_sha256':hashlib.sha256(payload).hexdigest(),'stderr_path':str(err),'completed_objects':len(rows),'body_files_written':0,'raw_bytes_streamed_not_metadata_only':True}
  r=persist_terminal(s,D,rows,receipt)
 print(json.dumps({'state':r['state'],'summary':r['summary']}))
 return 0 if r['state']=='actual_native_format_triage_only' else 1
def verify(s,r):
 D=Path(s['native_directory']);c=read(D/'receipt.json');rows=read(D/'predicate_rows.json');task=read(D/'frozen_task.json')
 assert sha(P)==r['protocol_sha256']==c['frozen_protocol_sha256'] and sha(__file__)==r['implementation_sha256']==c['implementation_sha256']
 assert sha(D/'receipt.json')==r['native_receipt_sha256'] and sha(D/'frozen_protocol.json')==c['frozen_protocol_sha256'] and read(D/'frozen_protocol.json')==s
 assert sha(D/'frozen_task.json')==c['frozen_task_sha256'] and sha(D/'predicate_rows.json')==c['predicate_rows_sha256'] and sha(D/'stderr')==c['stderr_sha256']
 assert c['argv']==['git','cat-file','--batch'] and c['attempts']==1 and c['actual_returncode']==0 and c['failure_type'] is None and not c['wall_cap_reached'] and not c['cleanup_errors'] and not c['watchdog_errors'] and not c['recording_errors'] and not r['terminal_recording_errors']
 assert c['stdin_sha256']==s['oid_input_sha256'] and c['completed_objects']==102 and len(rows)==102 and c['body_files_written']==0 and c['raw_bytes_streamed_not_metadata_only']
 assert [z['oid'] for z in rows]==[z['oid'] for z in s['objects']] and [z['bytes'] for z in rows]==[z['bytes'] for z in s['objects']]
 assert r['summary']==summary(rows) and all(re.fullmatch(r'[0-9a-f]{64}',z['sha256']) for z in rows)
 for p,h in s['native_inputs'].items():assert sha(p)==h
 for p,h in s['known_metadata_reference_sha256'].items():assert sha(R/p)==h
 for z in rows:assert z['known_metadata_file_sha_matches']==[p for p,h in s['known_metadata_reference_sha256'].items() if z['sha256']==h]
 assert task['checkpoint_status']=='E54_native_format_frozen' and read(D/'implementation_hash.json')['sha256']==sha(__file__)
 return {'native_receipts_verified':True,'boundary_tests':boundary_tests(),'cleanup_failure_test':cleanup_failure_test(),'recording_failure_test':recording_failure_test(),'capture_gate_tests':gate_tests(s,task),'original_dataset_recovered':0,'new_economic_runs':0,'model_admitted':False}
def main():
 s=read(P)
 if '--verify' in sys.argv:print(json.dumps(verify(s,read(O))));return 0
 assert '--capture' in sys.argv;return capture(s)
if __name__=='__main__':
 try:raise SystemExit(main())
 except Exception as e:print(json.dumps({'error_type':type(e).__name__,'raw_content_not_emitted':True}));raise SystemExit(1)
