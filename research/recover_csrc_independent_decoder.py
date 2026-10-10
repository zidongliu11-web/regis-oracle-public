"""Pinned existing MS-DOC reader; no macro/COM/source edits or npm scripts."""
import argparse,base64,hashlib,io,json,os,subprocess,tarfile,time,urllib.request,urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path,PurePosixPath
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/CSRC_INDEPENDENT_DECODER_PROTOCOL_20261003.json';RESULT=ROOT/'research/CSRC_INDEPENDENT_DECODER_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*a,**k):return None
def get(url,dest,limit):
 receipt={'url':url,'started_utc':datetime.now(timezone.utc).isoformat(),'attempts':1,'retries':0,'max_bytes':limit};data=b''
 try:
  with urllib.request.build_opener(NoRedirect).open(urllib.request.Request(url,headers={'User-Agent':'RegisOracle-native-source-reader'}),timeout=15) as r:
   data=r.read(limit+1);receipt.update(status=r.status,within_cap=len(data)<=limit)
   if len(data)>limit:data=data[:limit]
 except urllib.error.HTTPError as e:receipt.update(status=e.code,error_type='HTTPError',redirect=e.headers.get('Location'))
 except Exception as e:receipt.update(error_type=type(e).__name__)
 if data:dest.write_bytes(data)
 receipt.update(body=str(dest),bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),ended_utc=datetime.now(timezone.utc).isoformat());write(dest.with_suffix('.receipt.json'),receipt)
 if receipt.get('status')!=200 or not receipt.get('within_cap'):raise ValueError('actual_dependency_GET_failed_preserved')
 return data
def package(job,s,local):
 name,version=job['name'],job['version'];meta=local/(name+'.metadata.json');raw=get('https://registry.npmjs.org/'+name+'/'+version,meta,65536);r=json.loads(raw)
 if r['name']!=name or r['version']!=version or r.get('license')!='MIT' or (r.get('dependencies') or {})!=job['dependencies']:raise ValueError('pinned_package_identity_license_or_graph_changed')
 url=r['dist']['tarball'];expected='https://registry.npmjs.org/'+name+'/-/'+name+'-'+version+'.tgz'
 if url!=expected:raise ValueError('pinned_package_tarball_location_changed')
 tgz=local/(name+'-'+version+'.tgz');data=get(url,tgz,s['tarball_cap']);integrity=r['dist']['integrity'];alg,digest=integrity.split('-',1)
 if alg!='sha512' or base64.b64encode(hashlib.sha512(data).digest()).decode()!=digest:raise ValueError('published_package_integrity_failed')
 output=local/'node_modules'/name;output.mkdir(parents=True);files={};size=0;licences=[]
 with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as tar:
  for member in tar:
   path=PurePosixPath(member.name)
   if not path.parts or path.parts[0]!='package' or path.is_absolute() or '..' in path.parts or any(':' in x or '\\' in x for x in path.parts):raise ValueError('unsafe_package_member_path')
   rel=Path(*path.parts[1:]);target=output/rel
   if member.isdir():target.mkdir(parents=True,exist_ok=True);continue
   if not member.isfile() or member.size<0:raise ValueError('package_link_or_special_member_refused')
   if target.suffix.lower() in ['.exe','.dll','.node','.so','.bat','.cmd']:raise ValueError('native_or_lifecycle_binary_refused')
   size+=member.size
   if size>s['unpacked_package_cap'] or len(files)>=s['package_file_cap']:raise ValueError('package_unpacked_cap')
   payload=tar.extractfile(member).read(member.size+1)
   if len(payload)!=member.size or target.exists():raise ValueError('package_member_length_or_collision')
   target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(payload);files[str(target)]=sha(target)
   if target.name.upper().startswith(('LICENSE','LICENCE','COPYING')):licences.append({'path':str(target),'sha256':sha(target)})
 if not licences:raise ValueError('actual_package_license_file_missing')
 manifest=local/(name+'.files.json');write(manifest,{'files':files});return {'name':name,'version':version,'license_metadata':'MIT','metadata_sha256':sha(meta),'tarball_sha256':sha(tgz),'published_SHA512_integrity_match':True,'unpacked_bytes':size,'files':len(files),'licences':licences,'native_manifest':str(manifest),'native_manifest_sha256':sha(manifest),'lifecycle_scripts_executed':False}
def check(s):
 for p,h in s['input_hashes'].items():
  if sha(ROOT/p)!=h:raise ValueError('independent_reader_frozen_input_changed')
 for p,h in s['native_input_hashes'].items():
  if sha(p)!=h:raise ValueError('independent_reader_original_source_changed')
def run(s):
 if read(RESULT)['state']!='not_run':raise ValueError('first_independent_decoder_attempt_preserved')
 check(s);local=Path(s['native_directory']);assert not local.exists() and 'onedrive' not in str(local).lower();local.mkdir(parents=True);start=time.monotonic();packs=[];failure=None
 def kept(job):
  try:return package(job,s,local)
  except Exception as e:return {'name':job['name'],'version':job['version'],'state':'actual_dependency_failure_preserved','error_type':type(e).__name__,'error_code':str(e) if isinstance(e,ValueError) else 'native_details_retained'}
 with ThreadPoolExecutor(max_workers=2) as pool:packs=list(pool.map(kept,s['packages']))
 decoder=None
 if not any(x.get('state') for x in packs):
  wrapper=local/'decode-original.mjs';wrapper.write_bytes((ROOT/s['node_wrapper']).read_bytes());output=local/'decoded-content.json';receipt=local/'decoder-receipt.json'
  argv=[s['node_executable'],'--permission','--allow-fs-read='+str(local),'--allow-fs-read='+str(Path(s['native_source']).parent),'--allow-fs-write='+str(local),'--max-old-space-size=256',str(wrapper),s['native_source'],str(output),str(receipt)]
  env={'SystemRoot':os.environ.get('SystemRoot',r'C:\Windows'),'PATH':str(Path(s['node_executable']).parent),'NODE_NO_WARNINGS':'1'}
  try:
   p=subprocess.run(argv,cwd=local,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=45,creationflags=subprocess.CREATE_NO_WINDOW);(local/'decoder.stdout.log').write_bytes(p.stdout);(local/'decoder.stderr.log').write_bytes(p.stderr)
   decoder=read(receipt) if receipt.exists() else {'state':'actual_native_node_process_failed_preserved','returncode':p.returncode,'native_error_log':str(local/'decoder.stderr.log')}
  except subprocess.TimeoutExpired as e:
   (local/'decoder.stdout.log').write_bytes(e.stdout or b'');(local/'decoder.stderr.log').write_bytes(e.stderr or b'');decoder={'state':'actual_native_node_process_timed_out_preserved','timeout_seconds':45,'process_terminated_by_run':True}
 check(s);manifest=local/'independent_decoder_manifest.json';write(manifest,{'files':{str(p):sha(p) for p in local.rglob('*') if p.is_file()}})
 result={'state':'actual_pinned_independent_original_format_reader_attempt','observed_utc':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':round(time.monotonic()-start,3),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),'dependency_packages':packs,'decoder':decoder,'actual_GET_receipts':len(list(local.glob('*.receipt.json'))),'local_details':{'path':str(manifest),'sha256':sha(manifest)},'source_original_modified':False,'old_first_failures_preserved':True,'local_global_installs_or_lifecycle_scripts':False,'table_membership_activated':False,'macro_or_Office_COM_executed':False,'quote_arrays_read':False,'economic_runs':0,'model_admitted':False,'full_goal_complete':False};write(RESULT,result);print(json.dumps({'state':result['state'],'elapsed_seconds':result['elapsed_seconds'],'packages':[{'name':x['name'],'version':x['version'],'state':x.get('state','pinned_integrity_licences_retained')} for x in packs],'decoder':decoder,'actual_GET_receipts':result['actual_GET_receipts'],'member_classes_activated':0},ensure_ascii=False))
def verify(s):
 check(s);r=read(RESULT)
 if r['state']!='actual_pinned_independent_original_format_reader_attempt' or r['protocol_sha256']!=sha(PROTOCOL) or r['implementation_sha256']!=sha(__file__) or sha(r['local_details']['path'])!=r['local_details']['sha256']:raise ValueError('independent_decoder_evidence_changed')
 for p,h in read(r['local_details']['path'])['files'].items():
  if sha(p)!=h:raise ValueError('independent_decoder_native_file_changed')
 for package in r['dependency_packages']:
  if package.get('state'):continue
  if sha(package['native_manifest'])!=package['native_manifest_sha256']:raise ValueError('dependency_manifest_changed')
  for p,h in read(package['native_manifest'])['files'].items():
   if sha(p)!=h:raise ValueError('dependency_file_changed')
 if any(r[k] for k in ['source_original_modified','local_global_installs_or_lifecycle_scripts','table_membership_activated','macro_or_Office_COM_executed','quote_arrays_read','economic_runs','model_admitted','full_goal_complete']):raise ValueError('independent_decoder_scope_changed')
 print(json.dumps({'pinned_existing_decoder_attempt_and_native_hashes_verified':True,'new_source_classes':0,'new_GETs':0,'economic_or_model_admission':False}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();m=ap.add_mutually_exclusive_group(required=True);m.add_argument('--run',action='store_true');m.add_argument('--verify',action='store_true');a=ap.parse_args();run(read(PROTOCOL)) if a.run else verify(read(PROTOCOL))
