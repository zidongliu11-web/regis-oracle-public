"""Match captured object fingerprints to known archived artifact declarations."""
import hashlib,json,re,sys
from datetime import datetime,timezone
from pathlib import Path
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R))
P=R/'research/GIT_BLOB_IDENTITY_PROTOCOL_20261003.json';O=R/'research/GIT_BLOB_IDENTITY_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def allowed(path,s):return isinstance(path,str) and len(path)<=s['path_character_cap'] and not any(w in path.lower() for w in s['excluded_path_words']) and not Path(path).is_absolute() and '..' not in Path(path).parts
def derive(s):
 assert len(s['known_Task_files'])<=128 and sum(Path(p).stat().st_size for p in s['known_Task_files'])<=33554432 and all(Path(p).stat().st_size<=1048576 for p in s['known_Task_files'])
 index={};records=0;invalid=0;private=0
 for p,h in s['known_Task_files'].items():
  assert sha(p)==h
  task=read(p)
  for a in task.get('artifacts',[]):
   records+=1;assert records<=100000
   if not isinstance(a,dict) or not isinstance(a.get('sha256'),str) or not re.fullmatch(r'[0-9a-f]{64}',a['sha256']):invalid+=1;continue
   if not allowed(a.get('path'),s):private+=1;continue
   index.setdefault(a['sha256'],set()).add(a['path'])
 assert sha(s['native_predicate_rows_path'])==s['native_predicate_rows_sha256']
 rows=read(s['native_predicate_rows_path']);assert len(rows)==102
 matches=[{'oid':z['oid'],'sha256':z['sha256'],'bytes':z['bytes'],'declared_paths':sorted(index[z['sha256']])} for z in rows if z['sha256'] in index]
 return {'known_Task_file_count':len(s['known_Task_files']),'artifact_metadata_records_examined':records,'unique_declared_sha256':len(index),'invalid_hash_records_skipped':invalid,'private_or_invalid_paths_suppressed':private,'object_fingerprints_compared':len(rows),'matched_object_count':len(matches),'unmatched_object_count':len(rows)-len(matches),'matched_path_suffix_counts':{suffix:sum(any(Path(p).suffix.lower()==suffix for p in z['declared_paths']) for z in matches) for suffix in ['.py','.json','.md','.csv','.parquet','.db','.sqlite','.h5']},'specific_missing_target_matches':{p:any(p in z['declared_paths'] for z in matches) for p in s['specific_missing_targets']},'matches':matches,'new_Git_commands':0,'unknown_blob_body_reads_or_field_decodes':0,'known_Task_artifact_path_sha_metadata_fields_decoded':True,'account_or_market_value_fields_selected':0,'original_dataset_recovered':0,'recorded_path_does_not_certify_source_rights_or_PIT':True,'model_admitted':False,'full_goal_complete':False}
def active_gate(s):
 from scripts.workflow_guard import contract_hash
 t=read(R/'WORKFLOW_TASK.json');g=s['execution_gate'];idx={a['path']:a['sha256'] for a in t['artifacts']}
 assert t['status']=='in_progress' and t['id']==s['parent_task'] and t['owner_session']==g['owner_session'] and t['contract_sha256']==g['expected_contract_sha256']==contract_hash(t)
 assert len(t['artifacts'])==931 and len(t['checks'])==75 and read(P)==s
 assert sha(P)==idx[P.relative_to(R).as_posix()] and sha(__file__)==idx[Path(__file__).resolve().relative_to(R).as_posix()]
 assert {p:sha(R/p) for p in s['input_hashes']}==s['input_hashes']
 return t
def main():
 s=read(P);D=Path(s['native_directory'])
 if '--verify' in sys.argv:
  r=read(O);assert r['protocol_sha256']==sha(P) and r['implementation_sha256']==sha(__file__) and r['summary']==derive(s)
  assert sha(D/'result.json')==r['native_result_sha256'] and read(D/'result.json')==r['summary'] and read(D/'protocol.json')==s and sha(D/'protocol.json')==r['native_protocol_sha256']
  assert sha(D/'frozen_task.json')==r['native_task_sha256'];t=read(D/'frozen_task.json');from scripts.workflow_guard import contract_hash
  assert t['contract_sha256']==s['execution_gate']['expected_contract_sha256']==contract_hash(t)
  assert not r['model_admitted'] and not r['full_goal_complete'];print(json.dumps({k:r['summary'][k] for k in ['known_Task_file_count','artifact_metadata_records_examined','matched_object_count','unmatched_object_count','matched_path_suffix_counts','specific_missing_target_matches','new_Git_commands','unknown_blob_body_reads_or_field_decodes']}));return
 assert '--derive' in sys.argv and read(O)['state']=='not_run';t=active_gate(s);assert 'onedrive' not in str(D.resolve()).lower() and not D.exists();D.mkdir()
 write(D/'protocol.json',s);write(D/'frozen_task.json',t);f=derive(s);write(D/'result.json',f)
 r={'schema':'regis.git_blob_identity_result.v1','state':'actual_known_metadata_SHA_derivation_only','created_utc':datetime.now(timezone.utc).isoformat(),'protocol_sha256':sha(P),'implementation_sha256':sha(__file__),'native_result_sha256':sha(D/'result.json'),'native_protocol_sha256':sha(D/'protocol.json'),'native_task_sha256':sha(D/'frozen_task.json'),'summary':f,'model_admitted':False,'full_goal_complete':False};write(O,r)
 print(json.dumps({k:f[k] for k in ['known_Task_file_count','artifact_metadata_records_examined','matched_object_count','unmatched_object_count','matched_path_suffix_counts','specific_missing_target_matches','new_Git_commands','unknown_blob_body_reads_or_field_decodes']}))
if __name__=='__main__':
 try:main()
 except Exception as e:print(json.dumps({'error_type':type(e).__name__,'unknown_body_not_emitted':True}));raise SystemExit(1)
