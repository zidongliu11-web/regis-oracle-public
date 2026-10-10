"""Reuse pinned files; permit module imports while operations stay denied."""
import argparse,hashlib,json,os,shutil,subprocess,time
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/CSRC_INDEPENDENT_DECODER_V2_PROTOCOL_20261003.json';RESULT=ROOT/'research/CSRC_INDEPENDENT_DECODER_V2_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def inputs(s):
 for p,h in s['input_hashes'].items():
  if sha(ROOT/p)!=h:raise ValueError('integration_frozen_input_changed')
 for p,h in s['native_input_hashes'].items():
  if sha(p)!=h:raise ValueError('preserved_first_native_changed')
 prior=read(ROOT/s['first_result']);manifest=read(prior['local_details']['path'])
 for p,h in manifest['files'].items():
  if sha(p)!=h:raise ValueError('first_decoder_native_evidence_changed')
 return prior
def run(s):
 if read(RESULT)['state']!='not_run':raise ValueError('first_integration_attempt_preserved')
 prior=inputs(s);local=Path(s['native_directory']);assert not local.exists() and 'onedrive' not in str(local).lower();local.mkdir();start=time.monotonic();old=Path(prior['local_details']['path']).parent
 shutil.copytree(old/'node_modules',local/'node_modules')
 copied={}
 for p in (old/'node_modules').rglob('*'):
  if p.is_file():
   q=local/p.relative_to(old)
   if sha(p)!=sha(q):raise ValueError('pinned_module_copy_changed')
   copied[str(q)]=sha(q)
 wrapper=local/'decode-original-v2.mjs';wrapper.write_bytes((ROOT/s['node_wrapper']).read_bytes());output=local/'decoded-content.json';receipt=local/'decoder-receipt.json'
 argv=[s['node_executable'],'--permission','--allow-fs-read='+str(local),'--allow-fs-read='+str(Path(s['native_source']).parent),'--allow-fs-write='+str(local),'--max-old-space-size=256',str(wrapper),s['native_source'],str(output),str(receipt)]
 env={'SystemRoot':os.environ.get('SystemRoot',r'C:\Windows'),'PATH':str(Path(s['node_executable']).parent),'NODE_NO_WARNINGS':'1'}
 try:
  p=subprocess.run(argv,cwd=local,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=45,creationflags=subprocess.CREATE_NO_WINDOW);(local/'decoder.stdout.log').write_bytes(p.stdout);(local/'decoder.stderr.log').write_bytes(p.stderr);decoder=read(receipt) if receipt.exists() else {'state':'actual_integration_node_process_failed_preserved','returncode':p.returncode}
 except subprocess.TimeoutExpired as e:
  (local/'decoder.stdout.log').write_bytes(e.stdout or b'');(local/'decoder.stderr.log').write_bytes(e.stderr or b'');decoder={'state':'actual_integration_node_timed_out_preserved','process_terminated_by_run':True}
 inputs(s);manifest=local/'integration_decoder_manifest.json';write(manifest,{'files':{str(p):sha(p) for p in local.rglob('*') if p.is_file()}})
 r={'state':'actual_zero_GET_independent_reader_integration_repair','observed_utc':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':round(time.monotonic()-start,3),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),'first_actual_result_sha256':sha(ROOT/s['first_result']),'first_blocked_import_preserved':True,'first_blocked_import_not_claimed_source_format_failure':True,'network_GETs':0,'pinned_package_files_reused':len(copied),'pinned_package_bytes_changed':False,'decoder':decoder,'local_details':{'path':str(manifest),'sha256':sha(manifest)},'original_source_or_package_or_FIB_modified':False,'member_classes_activated':0,'Office_COM_or_macro_executed':False,'quote_arrays_read':False,'economic_runs':0,'model_admitted':False,'full_goal_complete':False};write(RESULT,r);print(json.dumps({'state':r['state'],'elapsed_seconds':r['elapsed_seconds'],'network_GETs':0,'pinned_package_files_reused':len(copied),'decoder':decoder,'member_classes_activated':0},ensure_ascii=False))
def verify(s):
 prior=inputs(s);r=read(RESULT)
 if r['state']!='actual_zero_GET_independent_reader_integration_repair' or r['protocol_sha256']!=sha(PROTOCOL) or r['implementation_sha256']!=sha(__file__) or sha(r['local_details']['path'])!=r['local_details']['sha256']:raise ValueError('integration_evidence_changed')
 for p,h in read(r['local_details']['path'])['files'].items():
  if sha(p)!=h:raise ValueError('integration_native_changed')
 old=Path(prior['local_details']['path']).parent;new=Path(r['local_details']['path']).parent;count=0
 for p in (old/'node_modules').rglob('*'):
  if p.is_file():
   if sha(p)!=sha(new/p.relative_to(old)):raise ValueError('integration_package_bytes_changed')
   count+=1
 if count!=r['pinned_package_files_reused'] or r['network_GETs'] or r['pinned_package_bytes_changed'] or any(r[k] for k in ['original_source_or_package_or_FIB_modified','member_classes_activated','Office_COM_or_macro_executed','quote_arrays_read','economic_runs','model_admitted','full_goal_complete']):raise ValueError('integration_scope_changed')
 print(json.dumps({'integration_reused_original_packages_and_source_verified':True,'source_classes_activated':0,'network_GETs':0,'model_admitted':False}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();m=ap.add_mutually_exclusive_group(required=True);m.add_argument('--run',action='store_true');m.add_argument('--verify',action='store_true');a=ap.parse_args();run(read(PROTOCOL)) if a.run else verify(read(PROTOCOL))
