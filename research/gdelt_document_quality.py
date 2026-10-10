"""Offline, exact-document grouping; emits counts only, never news metadata text."""
import hashlib,json,math,sys,zipfile
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R))
P=R/'research/GDELT_DOCUMENT_QUALITY_PROTOCOL_20261003.json';O=R/'research/GDELT_DOCUMENT_QUALITY_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def add(groups,counts,fields,cohort):
 counts['rows']+=1
 if len(fields)!=27:counts['schema_invalid_rows']+=1;return
 if not fields[2] or not fields[4]:counts['empty_document_key_component_rows']+=1;return
 counts['identified_rows']+=1;key=(fields[2],fields[4]);g=groups.setdefault(key,{'n':0,'cohorts':set(),'languages':set(),'tones':set(),'organizations':set()});g['n']+=1;g['cohorts'].add(cohort)
 codes=[x.partition(':')[2].strip().lower() for x in fields[25].split(';') if x.partition(':')[0].strip().lower()=='srclc']
 language='chinese_tag' if len(codes)==1 and codes[0] in ('chi','zho') else ('non_chinese_tag' if len(codes)==1 and codes[0] in ('fra','fre','eng') else 'unresolved_tag')
 g['languages'].add(language)
 try:
  tone=tuple(float(v) for v in fields[15].split(','));assert len(tone)==7 and all(math.isfinite(v) for v in tone)
 except (ValueError,AssertionError):tone=None;counts['invalid_tone_rows']+=1
 g['tones'].add(tone);g['organizations'].add(frozenset(x.strip().casefold() for x in fields[13].split(';') if x.strip()))
def summarize(groups,counts):
 sizes=Counter(g['n'] for g in groups.values());c=Counter(counts)
 c['exact_nonempty_document_keys']=len(groups);c['records_collapsed_by_exact_document_key']=c['identified_rows']-len(groups)
 for g in groups.values():
  if g['n']>1:c['duplicate_document_groups']+=1
  if len(g['cohorts'])>1:c['cross_stream_document_groups']+=1
  if len(g['languages'])>1:c['mixed_language_status_groups']+=1
  if 'chinese_tag' in g['languages']:c['document_groups_with_any_Chinese_tag']+=1
  if g['languages']=={'chinese_tag'}:c['document_groups_with_only_Chinese_tags']+=1
  if len(g['tones'])>1:c['tone_conflicting_groups']+=1
  if len(g['organizations'])>1:c['organization_set_conflicting_groups']+=1
  if g['languages']=={'chinese_tag'} and len(g['tones'])==len(g['organizations'])==1 and None not in g['tones']:c['only_Chinese_tag_and_consistent_fields_groups']+=1
 keys=['rows','schema_invalid_rows','empty_document_key_component_rows','identified_rows','invalid_tone_rows','exact_nonempty_document_keys','records_collapsed_by_exact_document_key','duplicate_document_groups','cross_stream_document_groups','mixed_language_status_groups','document_groups_with_any_Chinese_tag','document_groups_with_only_Chinese_tags','tone_conflicting_groups','organization_set_conflicting_groups','only_Chinese_tag_and_consistent_fields_groups']
 return {'counts':{k:c[k] for k in keys},'records_per_exact_document_key_histogram':{str(k):v for k,v in sorted(sizes.items())},'max_records_per_exact_document_key':max(sizes,default=0),'exact_key_is_independently_unique_economic_news_event':False,'Chinese_tag_is_verified_A_share_company':False,'allA_or_historical_available_at_certified':False,'new_economic_runs':0,'model_admitted':False}
def compute(s):
 groups={};counts=Counter();sources=[]
 for source in s['sources_native']:
  zpath=Path(source['raw_path']);assert sha(zpath)==source['raw_sha256'] and zpath.stat().st_size<=source['compressed_byte_cap']
  before=counts['rows'];expanded=0
  with zipfile.ZipFile(zpath) as z:
   entries=z.infolist();assert len(entries)==1;info=entries[0];assert info.filename==source['member_name'] and info.file_size<=67108864 and not info.flag_bits&1
   with z.open(info) as stream:
    for _ in range(20000):
     line=stream.readline(1048577)
     if not line:break
     assert len(line)<=1048576;expanded+=len(line);assert expanded<=67108864
     add(groups,counts,line.rstrip(b'\r\n').decode('utf-8').split('\t'),source['cohort'])
    else:assert stream.read(1)==b''
   assert expanded==info.file_size and counts['rows']-before==source['expected_rows']
  sources.append({'cohort':source['cohort'],'rows':counts['rows']-before,'uncompressed_bytes':expanded,'raw_sha256':source['raw_sha256']})
 result=summarize(groups,counts);result['sources']=sources;return result
def gate(s):
 from scripts.workflow_guard import contract_hash
 t=read(R/'WORKFLOW_TASK.json');idx={a['path']:a['sha256'] for a in t['artifacts']};g=s['execution_gate']
 assert t['status']=='in_progress' and t['id']==s['parent_task'] and t['owner_session']==g['owner_session'] and t['contract_sha256']==g['expected_contract_sha256']==contract_hash(t) and read(P)==s
 assert sha(P)==idx[P.relative_to(R).as_posix()] and sha(__file__)==idx[Path(__file__).resolve().relative_to(R).as_posix()]
 assert {p:sha(R/p) for p in s['input_hashes']}==s['input_hashes'] and {p:sha(p) for p in s['native_input_hashes']}==s['native_input_hashes']
 return t
def counterexamples():
 fields=['']*27;fields[2]='1';fields[4]='https://synthetic.invalid/a';fields[13]='synthetic organization';fields[15]='0,0,0,0,0,0,1';fields[25]='srclc:chi'
 groups={};counts=Counter();add(groups,counts,fields,'translated');add(groups,counts,fields,'translated')
 changed=list(fields);changed[15]='1,1,0,1,0,0,1';changed[13]='other synthetic organization';changed[25]='';add(groups,counts,changed,'english');x=summarize(groups,counts)['counts']
 assert x['rows']==3 and x['exact_nonempty_document_keys']==1 and x['records_collapsed_by_exact_document_key']==2 and x['cross_stream_document_groups']==x['tone_conflicting_groups']==x['organization_set_conflicting_groups']==x['mixed_language_status_groups']==1 and x['only_Chinese_tag_and_consistent_fields_groups']==0
 changed=list(fields);changed[4]='https://synthetic.invalid/b';add(groups,counts,changed,'translated');empty=list(fields);empty[4]='';add(groups,counts,empty,'translated');add(groups,counts,['invalid'],'english');x=summarize(groups,counts)['counts']
 assert x['rows']==6 and x['exact_nonempty_document_keys']==2 and x['empty_document_key_component_rows']==x['schema_invalid_rows']==1 and x['only_Chinese_tag_and_consistent_fields_groups']==1
 empty_collection=list(fields);empty_collection[2]='';add(groups,counts,empty_collection,'english')
 empty_both=list(fields);empty_both[2]=empty_both[4]='';add(groups,counts,empty_both,'translated');x=summarize(groups,counts)['counts']
 assert x['rows']==8 and x['identified_rows']==4 and x['exact_nonempty_document_keys']==2 and x['empty_document_key_component_rows']==3 and x['cross_stream_document_groups']==1
 return {'duplicate_cross_stream_conflict_empty_components_schema_and_nonadmission_cases':8,'network_or_actual_news_file_calls':0}
def main():
 if '--counterexamples' in sys.argv:print(json.dumps(counterexamples()));return
 s=read(P);D=Path(s['native_directory'])
 if '--compute' in sys.argv:
  t=gate(s);assert read(O)['state']=='not_run' and not D.exists() and 'onedrive' not in str(D.resolve()).lower();D.mkdir();write(D/'protocol.json',s);write(D/'frozen_task.json',t)
  c={'started_utc':datetime.now(timezone.utc).isoformat(),'network_calls':0,'failure_type':None,'raw_news_metadata_emitted_to_AI_or_Git':False}
  try:c['summary']=compute(s)
  except Exception as e:c['failure_type']=type(e).__name__
  c['ended_utc']=datetime.now(timezone.utc).isoformat()
  import runpy
  m=runpy.run_path(str(R/'research/capture_gdelt_attention_source.py'));f=m['persist'];f.__globals__.update(P=P,O=O,__file__=__file__)
  r=f(D,c);print(json.dumps({'state':r['state'],'summary':r['summary'],'failure_type':r['failure_type'],'new_network_calls':0}));return
 assert '--verify' in sys.argv;r=read(O);c=read(D/'receipt.json');t=read(D/'frozen_task.json');from scripts.workflow_guard import contract_hash
 assert sha(P)==r['protocol_sha256']==r['native_protocol_sha256']==sha(D/'protocol.json') and read(D/'protocol.json')==s and sha(__file__)==r['implementation_sha256'] and sha(D/'receipt.json')==r['native_receipt_sha256'] and sha(D/'frozen_task.json')==r['native_task_sha256'] and t['contract_sha256']==s['execution_gate']['expected_contract_sha256']==contract_hash(t) and c['network_calls']==0 and not c['raw_news_metadata_emitted_to_AI_or_Git']
 assert {p:sha(p) for p in s['native_input_hashes']}==s['native_input_hashes']
 if r['state']=='actual_native_news_metadata_sample':assert c['failure_type'] is None and not r['terminal_recording_errors'] and c['summary']==r['summary']==compute(s)
 else:assert r['state']=='actual_source_failure_preserved' and (c['failure_type'] or r['terminal_recording_errors'])
 print(json.dumps({'offline_document_quality_evidence_verified':True,'network_calls':0,'counterexamples':counterexamples(),'model_or_Goal_admitted':False}))
if __name__=='__main__':
 try:main()
 except Exception as e:print(json.dumps({'error_type':type(e).__name__,'raw_news_text_not_emitted':True}));raise SystemExit(1)
