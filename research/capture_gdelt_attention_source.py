"""Bounded, native-only GKG source feasibility; never emits document content."""
import hashlib,io,json,math,re,sys,time,zipfile
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
R=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(R))
P=R/'research/GDELT_ATTENTION_SOURCE_PROTOCOL_20261003.json'
O=R/'research/GDELT_ATTENTION_SOURCE_RESULT_20261003.json'
URL='https://data.gdeltproject.org/gdeltv2/20150302010000.gkg.csv.zip'
MEMBER='20150302010000.gkg.csv'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def gate(s):
 from scripts.workflow_guard import contract_hash
 t=read(R/'WORKFLOW_TASK.json');idx={a['path']:a['sha256'] for a in t['artifacts']};g=s['execution_gate']
 assert t['status']=='in_progress' and t['id']==s['parent_task'] and t['owner_session']==g['owner_session'] and t['contract_sha256']==g['expected_contract_sha256']==contract_hash(t)
 assert read(P)==s and s['url']==URL and s['member_name']==MEMBER
 assert sha(P)==idx[P.relative_to(R).as_posix()] and sha(__file__)==idx[Path(__file__).resolve().relative_to(R).as_posix()]
 assert {p:sha(R/p) for p in s['input_hashes']}==s['input_hashes']
 assert {k:s[k] for k in ['max_GETs','compressed_byte_cap','uncompressed_byte_cap','row_cap','line_byte_cap']}=={'max_GETs':1,'compressed_byte_cap':8388608,'uncompressed_byte_cap':67108864,'row_cap':20000,'line_byte_cap':1048576}
 return t
def census(raw,s):
 c=Counter();seen_ids=set();seen_documents=set();lexical={w:0 for w in s['lexical_probe_words']};exhausted=False
 with zipfile.ZipFile(io.BytesIO(raw)) as z:
  members=z.infolist();assert len(members)==1
  info=members[0];assert info.filename==MEMBER and not info.flag_bits&1 and info.compress_type in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED)
  assert 0<info.file_size<=s['uncompressed_byte_cap'] and info.file_size<=max(1,info.compress_size)*200
  with z.open(info) as stream:
   for _ in range(s['row_cap']):
    line=stream.readline(s['line_byte_cap']+1)
    if not line:exhausted=True;break
    assert len(line)<=s['line_byte_cap'];c['uncompressed_bytes_processed']+=len(line);assert c['uncompressed_bytes_processed']<=s['uncompressed_byte_cap'];c['rows_processed']+=1
    try:fields=line.rstrip(b'\r\n').decode('utf-8').split('\t')
    except UnicodeDecodeError:c['invalid_utf8_rows']+=1;continue
    if len(fields)!=27:c['non27_field_rows']+=1;continue
    c['schema27_rows']+=1
    record,date,collection,common,document=fields[:5]
    if not re.fullmatch(r'\d{14}-(?:T)?\d+',record):c['record_id_invalid']+=1
    if record[:14]!='20150302010000':c['record_batch_filename_mismatch']+=1
    if '-T' in record:c['translated_record_ids']+=1
    if record in seen_ids:c['duplicate_record_ids']+=1
    seen_ids.add(record)
    try:datetime.strptime(date,'%Y%m%d%H%M%S')
    except ValueError:c['invalid_date_field']+=1
    if date!='20150302010000':c['date_field_filename_mismatch']+=1
    if collection=='1':c['web_collection_rows']+=1
    if document in seen_documents:c['duplicate_document_identifiers']+=1
    seen_documents.add(document)
    if common.lower().endswith('.cn'):c['source_common_name_cn_suffix_rows']+=1
    if fields[13]:c['organization_field_nonempty_rows']+=1
    if fields[14]:c['enhanced_organization_field_nonempty_rows']+=1
    if fields[25]:c['translation_info_nonempty_rows']+=1
    organizations={v.strip().casefold() for v in fields[13].split(';')}
    for word in lexical:
     if word in organizations:lexical[word]+=1
    try:
     tone=[float(v) for v in fields[15].split(',')];assert len(tone)==7 and all(math.isfinite(v) for v in tone) and -100<=tone[0]<=100;c['finite_seven_dimension_tone_rows']+=1
    except (ValueError,AssertionError):c['invalid_tone_rows']+=1
   if not exhausted:
    exhausted=stream.read(1)==b''
 c['unique_record_ids']=len(seen_ids);c['unique_document_identifiers']=len(seen_documents)
 keys=['rows_processed','schema27_rows','invalid_utf8_rows','non27_field_rows','record_id_invalid','record_batch_filename_mismatch','translated_record_ids','duplicate_record_ids','invalid_date_field','date_field_filename_mismatch','web_collection_rows','duplicate_document_identifiers','source_common_name_cn_suffix_rows','organization_field_nonempty_rows','enhanced_organization_field_nonempty_rows','translation_info_nonempty_rows','finite_seven_dimension_tone_rows','invalid_tone_rows','unique_record_ids','unique_document_identifiers','uncompressed_bytes_processed']
 return {'counts':{k:c[k] for k in keys},'lexical_probe_row_counts':lexical,'member_declared_uncompressed_bytes':info.file_size,'member_declared_CRC32':f'{info.CRC:08x}','source_file_fully_scanned':exhausted,'lexical_match_is_verified_A_share_identity':False,'first_publication_or_historical_availability_certified':False,'whole_A_coverage_certified':False,'prices_or_returns_used':False,'new_economic_runs':0,'model_admitted':False}
def persist(D,c):
 errors=[]
 def attempt(label,action):
  try:return action()
  except Exception as e:errors.append({'stage':label,'error_type':type(e).__name__});return None
 c['terminal_recording_errors']=errors;attempt('receipt_write',lambda:write(D/'receipt.json',c))
 hs={k:attempt(k,lambda path=path:sha(path)) for k,path in [('protocol_sha256',P),('implementation_sha256',Path(__file__)),('native_protocol_sha256',D/'protocol.json'),('native_task_sha256',D/'frozen_task.json'),('native_receipt_sha256',D/'receipt.json')]}
 state='actual_source_failure_preserved' if c.get('failure_type') or c.get('cleanup_error') or errors else 'actual_native_news_metadata_sample'
 r=dict(schema='regis.gdelt_attention_source_result.v1',state=state,**hs,summary=c.get('summary'),failure_type=c.get('failure_type'),terminal_recording_errors=errors,full_goal_complete=False,model_admitted=False,new_economic_runs=0)
 if attempt('result_write',lambda:write(O,r)) is None and errors and errors[-1]['stage']=='result_write':
  r['state']='actual_source_failure_preserved';attempt('fallback_write',lambda:write(D/'terminal_failure.json',r))
 return r
def anonymous_session(requests):
 session=requests.Session();session.trust_env=False
 return session
def begin_get(session,c,D):
 write(D/'receipt.json',c)
 c['GETs_initiated']+=1
 return session.get(URL,stream=True,allow_redirects=False,timeout=(6,8))
def pre_request_failure_test():
 ow,oh=globals()['write'],globals()['sha'];saved={};attempts=[]
 class SyntheticSession:
  calls=0
  def get(self,*args,**kwargs):self.calls+=1;raise AssertionError('must_not_call')
 session=SyntheticSession();c={'GETs_initiated':0,'failure_type':None}
 def fail_before_get(path,value):
  attempts.append(value['GETs_initiated']);raise PermissionError('synthetic_pre_request_write')
 def sink(path,value):saved[Path(path).name]=value
 try:
  globals()['write']=fail_before_get
  try:begin_get(session,c,Path('synthetic_native_directory'))
  except PermissionError as e:c['failure_type']=type(e).__name__
  else:raise AssertionError('pre_request_failure_not_preserved')
  globals()['write'],globals()['sha']=sink,lambda p:'f'*64
  result=persist(Path('synthetic_native_directory'),c)
  assert attempts==[0] and c['GETs_initiated']==session.calls==0 and result['state']=='actual_source_failure_preserved' and 'receipt.json' in saved and O.name in saved
 finally:globals()['write'],globals()['sha']=ow,oh
 return 1
def capture(s):
 t=gate(s);D=Path(s['native_directory']);assert not D.exists() and 'onedrive' not in str(D.resolve()).lower() and read(O)['state']=='not_run'
 D.mkdir();write(D/'protocol.json',s);write(D/'frozen_task.json',t)
 c={'started_utc':datetime.now(timezone.utc).isoformat(),'GETs_initiated':0,'url':URL,'status':None,'retries':0,'redirects':0,'failure_type':None,'raw_complete':False,'news_article_body_GETs':0,'raw_to_AI_Git_OneDrive_cloud':False,'credentials_or_new_login_or_contacts':False}
 start=time.monotonic();raw=bytearray();session=None
 try:
  import requests
  session=anonymous_session(requests)
  with begin_get(session,c,D) as response:
   c['status']=response.status_code;c['headers']={k:response.headers.get(k) for k in ['Content-Type','Content-Length','Date','Last-Modified','ETag']}
   for chunk in response.iter_content(8192):
    assert time.monotonic()-start<=47 and len(raw)+len(chunk)<=s['compressed_byte_cap'];raw.extend(chunk)
   c['raw_complete']=True
  path=D/'source.zip';path.write_bytes(raw);c.update(raw_path=str(path),raw_bytes=len(raw),raw_sha256=sha(path));assert c['status']==200
  c['summary']=census(raw,s)
 except Exception as e:
  c['failure_type']=type(e).__name__
  if raw and 'raw_path' not in c:
   try:
    path=D/'partial_response.bin';path.write_bytes(raw);c.update(raw_path=str(path),raw_bytes=len(raw),raw_sha256=sha(path))
   except Exception as e:c['partial_record_error']=type(e).__name__
 finally:
  if session:
   try:session.close()
   except Exception as e:c['cleanup_error']=type(e).__name__
  c['ended_utc']=datetime.now(timezone.utc).isoformat();c['elapsed_seconds']=round(time.monotonic()-start,3);r=persist(D,c)
 print(json.dumps({'state':r['state'],'status':c['status'],'GETs_initiated':c['GETs_initiated'],'raw_bytes':c.get('raw_bytes'),'summary':r['summary'],'failure_type':r['failure_type']}))
def counterexamples(s):
 fields=['']*27;fields[:5]=['20150302010000-T1','20150302010000','1','synthetic.cn','https://synthetic.invalid/a'];fields[13]='ping an bank';fields[15]='0,0,0,0,0,0,1'
 def package(name,rows):
  b=io.BytesIO()
  with zipfile.ZipFile(b,'w',zipfile.ZIP_DEFLATED) as z:z.writestr(name,rows)
  return b.getvalue()
 line=('\t'.join(fields)+'\n').encode();a=census(package(MEMBER,line*2),s)
 assert a['counts']['duplicate_record_ids']==1 and a['counts']['duplicate_document_identifiers']==1 and a['lexical_probe_row_counts']['ping an bank']==2 and not a['lexical_match_is_verified_A_share_identity']
 assert not a['first_publication_or_historical_availability_certified'] and not a['whole_A_coverage_certified']
 bad=census(package(MEMBER,b'a\tb\n'),s);assert bad['counts']['non27_field_rows']==1 and bad['counts']['schema27_rows']==0
 invalid=list(fields);invalid[1]='invalid';invalid[15]='NaN';b=census(package(MEMBER,('\t'.join(invalid)+'\n').encode()),s);assert b['counts']['invalid_date_field']==1 and b['counts']['invalid_tone_rows']==1
 try:census(package('../not_allowed.csv',line),s)
 except AssertionError:pass
 else:raise AssertionError('wrong_member_accepted')
 ow,oh=globals()['write'],globals()['sha'];terminal=0
 try:
  for failed in ['receipt.json','sha_read',O.name]:
   seen=[];saved={}
   def sink(path,value):
    seen.append(Path(path).name)
    if Path(path).name==failed:raise PermissionError('synthetic')
    saved[Path(path).name]=value
   def hash_sink(path):
    if failed=='sha_read':raise PermissionError('synthetic')
    return 'f'*64
   globals()['write'],globals()['sha']=sink,hash_sink
   r=persist(Path('synthetic_native_directory'),{'failure_type':None})
   assert r['state']=='actual_source_failure_preserved' and 'receipt.json' in seen and O.name in seen and r['terminal_recording_errors']
   if failed==O.name:assert 'terminal_failure.json' in saved
   terminal+=1
 finally:globals()['write'],globals()['sha']=ow,oh
 class SyntheticRequests:
  class Session:trust_env=True
 assert anonymous_session(SyntheticRequests).trust_env is False
 return {'schema_date_tone_duplicate_member_and_nonadmission_cases':4,'terminal_failure_cases':terminal,'pre_request_write_failure_GET_and_counter_zero':pre_request_failure_test(),'anonymous_session_trust_env_disabled':True,'network_process_actual_file_calls':0}
def main():
 s=read(P)
 if '--capture' in sys.argv:capture(s);return
 if '--counterexamples' in sys.argv:print(json.dumps(counterexamples(s)));return
 assert '--verify' in sys.argv;r=read(O);D=Path(s['native_directory']);c=read(D/'receipt.json');t=read(D/'frozen_task.json');from scripts.workflow_guard import contract_hash
 assert sha(P)==r['protocol_sha256']==r['native_protocol_sha256']==sha(D/'protocol.json') and read(D/'protocol.json')==s and sha(__file__)==r['implementation_sha256'] and sha(D/'receipt.json')==r['native_receipt_sha256'] and sha(D/'frozen_task.json')==r['native_task_sha256']
 assert t['contract_sha256']==s['execution_gate']['expected_contract_sha256']==contract_hash(t) and c['url']==URL and c['GETs_initiated']<=1 and c['retries']==c['redirects']==0 and c['news_article_body_GETs']==0 and not c['raw_to_AI_Git_OneDrive_cloud']
 if c.get('raw_path'):assert sha(c['raw_path'])==c['raw_sha256'] and c['raw_bytes']<=s['compressed_byte_cap']
 if r['state']=='actual_native_news_metadata_sample':
  assert c['status']==200 and c['raw_complete'] and c['failure_type'] is None and not r['terminal_recording_errors'] and not c['terminal_recording_errors'];assert census(Path(c['raw_path']).read_bytes(),s)==r['summary']==c['summary']
 else:assert r['state']=='actual_source_failure_preserved' and (c['failure_type'] or c.get('cleanup_error') or r['terminal_recording_errors'])
 print(json.dumps({'native_source_evidence_verified':True,'GETs':c['GETs_initiated'],'counterexamples':counterexamples(s),'whole_A_PIT_model_Goal_admitted':False}))
if __name__=='__main__':
 try:main()
 except Exception as e:print(json.dumps({'error_type':type(e).__name__,'raw_news_fields_not_emitted':True}));raise SystemExit(1)
