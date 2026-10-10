"""One existing CRAN-distributed standalone reader, original bytes, native-only output."""
import argparse,gzip,hashlib,io,json,os,re,struct,subprocess,time,urllib.request,urllib.error,zipfile
from pathlib import Path
from datetime import datetime,timezone
from xml.etree import ElementTree as ET
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/CSRC_ANTIWORD_READER_PROTOCOL_20261003.json';RESULT=ROOT/'research/CSRC_ANTIWORD_READER_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def inputs(s):
 for p,h in s['input_hashes'].items():
  if sha(ROOT/p)!=h:raise ValueError('frozen_reader_input_changed')
 for p,h in s['native_input_hashes'].items():
  if sha(p)!=h:raise ValueError('original_or_preflight_changed')
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None
def get(url,dest,cap):
 receipt={'url':url,'started_utc':datetime.now(timezone.utc).isoformat(),'attempts':1,'retries':0,'byte_cap':cap};data=b''
 try:
  with urllib.request.build_opener(NoRedirect).open(urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=15) as response:
   data=response.read(cap+1);receipt.update(status=response.status,content_type=response.headers.get('Content-Type'),within_cap=len(data)<=cap)
   if len(data)>cap:data=data[:cap]
 except urllib.error.HTTPError as e:receipt.update(status=e.code,error_type='HTTPError')
 except Exception as e:receipt.update(error_type=type(e).__name__,error_message_exported=False)
 if data:dest.write_bytes(data)
 receipt.update(bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),ended_utc=datetime.now(timezone.utc).isoformat());write(dest.with_suffix(dest.suffix+'.receipt.json'),receipt)
 if receipt.get('status')!=200 or not receipt.get('within_cap'):raise ValueError('actual_reader_download_failed_preserved')
 return data
def imports(path):
 b=Path(path).read_bytes();assert b[:2]==b'MZ';pe=struct.unpack_from('<I',b,60)[0];assert b[pe:pe+4]==b'PE\0\0'
 count=struct.unpack_from('<H',b,pe+6)[0];optsize=struct.unpack_from('<H',b,pe+20)[0];opt=pe+24;magic=struct.unpack_from('<H',b,opt)[0];dd=opt+(112 if magic==0x20b else 96);rva,size=struct.unpack_from('<II',b,dd+8)
 sections=[]
 for i in range(count):
  at=opt+optsize+i*40;vsize,va,rawsize,rawptr=struct.unpack_from('<IIII',b,at+8);sections.append((va,max(vsize,rawsize),rawptr))
 def offset(v):
  for va,extent,ptr in sections:
   if va<=v<va+extent:return ptr+v-va
  raise ValueError('reader_PE_RVA_unresolved')
 dll=[];at=offset(rva)
 for i in range(min(size//20+1,128)):
  desc=struct.unpack_from('<IIIII',b,at+i*20)
  if not any(desc):break
  name=offset(desc[3]);end=b.index(b'\0',name);dll.append(b[name:end].decode('ascii').lower())
 return sorted(set(dll))
def summary(xml):
 tree=ET.fromstring(xml);tags={}
 for node in tree.iter():tags[node.tag]=tags.get(node.tag,0)+1
 texts=list(tree.itertext());codes=[c for text in texts for c in re.findall(r'(?<!\d)\d{6}(?!\d)',text)]
 return {'XML_tag_counts':tags,'table_count':tags.get('table',0)+tags.get('informaltable',0),'row_count':tags.get('row',0),'entry_count':tags.get('entry',0),'six_digit_occurrences':len(codes),'unique_six_digit_occurrences':len(set(codes)),'table_cells_or_merges_certified':False}
def run(s):
 assert read(RESULT)['state']=='not_run';inputs(s);local=Path(s['native_directory']);assert not local.exists() and 'onedrive' not in str(local).lower();local.mkdir();start=time.monotonic();details={};state='actual_reader_attempt_failed_preserved'
 try:
  index=get(s['index_url'],local/'PACKAGES.gz',s['index_cap']);plain=gzip.decompress(index);assert len(plain)<=s['index_uncompressed_cap'];(local/'PACKAGES.txt').write_bytes(plain)
  paragraphs=plain.decode('utf-8').split('\n\n');rows=[dict(line.split(': ',1) for line in paragraph.splitlines() if ': ' in line) for paragraph in paragraphs if paragraph.startswith('Package: antiword\n')]
  assert len(rows)==1 and rows[0]['Version']==s['version'] and re.fullmatch('[0-9a-f]{32}',rows[0]['MD5sum']);write(local/'selected_distribution_metadata.json',rows[0])
  archive=get(s['binary_url'],local/'antiword.zip',s['zip_cap']);assert hashlib.md5(archive).hexdigest()==rows[0]['MD5sum'];details['published_distribution_MD5_matched']=True;details['zip_sha256']=sha(local/'antiword.zip');details['distribution_license']=rows[0].get('License')
  package=local/'package';package.mkdir();total=0;count=0
  with zipfile.ZipFile(io.BytesIO(archive)) as z:
   for member in z.infolist():
    parts=Path(member.filename.replace('\\','/')).parts
    assert parts and not member.filename.startswith(('/','\\')) and ':' not in member.filename and '..' not in parts and ((member.external_attr>>16)&0o170000)!=0o120000
    total+=member.file_size;count+=1;assert total<=s['unpacked_cap'] and count<=s['file_cap'];target=package.joinpath(*parts)
    if member.is_dir():target.mkdir(parents=True,exist_ok=True)
    else:target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(z.read(member))
  description=package/'antiword'/'DESCRIPTION';assert description.is_file();text=description.read_text(encoding='utf-8');assert 'Version: '+s['version'] in text and 'License: GPL-2' in text
  licence=get(s['license_url'],local/'GPL-2.0.txt',s['license_cap']);assert b'GNU GENERAL PUBLIC LICENSE' in licence and b'Version 2, June 1991' in licence
  bins=[p for p in (package/'antiword'/'bin').rglob('*') if p.is_file() and p.name.lower() in ['antiword','antiword.exe'] and p.read_bytes().startswith(b'MZ')];assert len(bins)==1;exe=bins[0]
  dlls=imports(exe);details.update(package_files=count,package_uncompressed_bytes=total,PE_import_DLLs=dlls,standalone_binary=str(exe),standalone_binary_sha256=sha(exe),R_or_global_install_used=False)
  # Imports alone do not establish a generic network sandbox; this standalone tool has no macros, R bootstrap or install scripts.
  assert not any(x.startswith('r.dll') or x in ['wininet.dll','winhttp.dll','ws2_32.dll','urlmon.dll'] for x in dlls)
  mapping=package/'antiword'/'share'/'antiword';assert (mapping/'UTF-8.txt').is_file();env={'SystemRoot':os.environ.get('SystemRoot',r'C:\Windows'),'PATH':str(exe.parent),'ANTIWORDHOME':str(mapping),'LC_ALL':'en_US.UTF-8'}
  with (local/'help.stdout.log').open('wb') as out,(local/'help.stderr.log').open('wb') as err:
   details['actual_help_invocations']=1
   help_process=subprocess.run([str(exe),'-h'],cwd=local,env=env,stdout=out,stderr=err,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
  details['actual_help_returncode']=help_process.returncode;details['actual_help_invocations']=1
  # Exactly one original-source invocation. Stdout and errors go directly to native files.
  with (local/'original-docbook.xml').open('wb') as out,(local/'original.stderr.log').open('wb') as err:
   details['actual_source_invocations']=1
   p=subprocess.run([str(exe),'-m','UTF-8.txt','-x','db',s['native_source']],cwd=local,env=env,stdout=out,stderr=err,timeout=45,creationflags=subprocess.CREATE_NO_WINDOW)
  details.update(actual_source_invocations=1,actual_source_returncode=p.returncode,native_output_sha256=sha(local/'original-docbook.xml'),native_output_bytes=(local/'original-docbook.xml').stat().st_size)
  assert (local/'original-docbook.xml').stat().st_size<=s['decoded_cap']
  if p.returncode==0:
   details['native_XML_summary']=summary((local/'original-docbook.xml').read_bytes());state='actual_standalone_antiword_native_XML_retained'
  else:state='actual_standalone_antiword_source_rejected_preserved'
 except Exception as e:
  # Error type only; original content or monetary values never emitted via exceptions.
  import traceback
  (local/'reader_failure.txt').write_text(traceback.format_exc(),encoding='utf-8')
  details.update(failure_type=type(e).__name__,failure_message_exported=False)
  if isinstance(e,subprocess.TimeoutExpired):details['timed_out_process_terminated_by_subprocess_run']=True
 inputs(s);manifest=local/'antiword_original_reader_manifest.json';write(manifest,{'files':{str(p):sha(p) for p in local.rglob('*') if p.is_file()}})
 result={'state':state,'observed_utc':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':round(time.monotonic()-start,3),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),'reader':details,'actual_GET_receipts':len(list(local.glob('*.receipt.json'))),'local_details':{'path':str(manifest),'sha256':sha(manifest)},'source_original_or_FIB_modified':False,'prior_reader_failures_preserved':True,'source_classes_activated':0,'quote_arrays_read':False,'economic_runs':0,'model_admitted':False,'full_goal_complete':False};write(RESULT,result);print(json.dumps({k:result[k] for k in ['state','elapsed_seconds','actual_GET_receipts','reader','source_classes_activated']}))
def verify(s):
 inputs(s);r=read(RESULT);assert r['protocol_sha256']==sha(PROTOCOL) and r['implementation_sha256']==sha(__file__) and sha(r['local_details']['path'])==r['local_details']['sha256']
 local=Path(r['local_details']['path']).parent
 for p,h in read(r['local_details']['path'])['files'].items():assert sha(p)==h
 if 'published_distribution_MD5_matched' in r['reader']:
  metadata=read(local/'selected_distribution_metadata.json');assert hashlib.md5((local/'antiword.zip').read_bytes()).hexdigest()==metadata['MD5sum']
 if 'standalone_binary' in r['reader']:assert imports(r['reader']['standalone_binary'])==r['reader']['PE_import_DLLs']
 if 'native_XML_summary' in r['reader']:assert summary((local/'original-docbook.xml').read_bytes())==r['reader']['native_XML_summary']
 assert not any(r[k] for k in ['source_original_or_FIB_modified','source_classes_activated','quote_arrays_read','economic_runs','model_admitted','full_goal_complete'])
 print(json.dumps({'native_original_reader_evidence_verified':True,'source_classes_activated':0,'network_or_parser_reruns':0,'model_admitted':False}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();m=ap.add_mutually_exclusive_group(required=True);m.add_argument('--run',action='store_true');m.add_argument('--verify',action='store_true');a=ap.parse_args();run(read(PROTOCOL)) if a.run else verify(read(PROTOCOL))
