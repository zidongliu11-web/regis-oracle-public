"""Reuse the retained pinned wheel with both exact licenses; no fetch or COM."""
import argparse,json,re,sys,time,zipfile
from pathlib import Path
from collections import Counter
from datetime import datetime,timezone
from csrc_doc_piece_probe import extract,read,sha,write,u16
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/CSRC_DOC_PIECE_NATIVE_PROTOCOL_20261003.json'
RESULT=ROOT/'research/CSRC_DOC_PIECE_NATIVE_RESULT_20261003.json'
def check(s):
 for p,h in s['input_hashes'].items():
  if sha(ROOT/p)!=h:raise ValueError('frozen native piece wrapper changed')
 for p,h in s['native_input_hashes'].items():
  if sha(p)!=h:raise ValueError('native first source/wheel/failedprobe changed')
def decode(s):
 wheel=Path(s['native_wheel'])
 if sha(wheel)!=s['wheel_sha256']:raise ValueError('retained wheel hash changed')
 with zipfile.ZipFile(wheel) as z:
  if z.testzip() is not None:raise ValueError('retained wheel CRC')
  licences={p:z.read(p) for p in s['exact_licences']}
  if any(__import__('hashlib').sha256(b).hexdigest()!=s['exact_licences'][p] for p,b in licences.items()):raise ValueError('exact existing licence identities')
 sys.path.insert(0,str(wheel));import olefile
 if olefile.__version__!='0.47':raise ValueError('retained wheel runtime version')
 with olefile.OleFileIO(s['native_source'],raise_defects=olefile.DEFECT_INCORRECT,write_mode=False) as ole:
  word=ole.openstream('WordDocument').read();table=ole.openstream('1Table' if u16(word,10)&0x200 else '0Table').read()
  issues=[{'exception_class':x[0].__name__,'message':str(x[1])[:120]} for x in ole.parsing_issues]
 text,structure=extract(word,table)
 tokens=re.findall(r'(?<!\d)\d{6}(?!\d)',text);controls=Counter(ord(c) for c in text if ord(c)<32)
 facts={'main_text_recovered':True,'six_digit_token_occurrences':len(tokens),'unique_six_digit_tokens':len(set(tokens)),
        'duplicate_token_occurrences':len(tokens)-len(set(tokens)),'control_character_counts':{str(k):v for k,v in sorted(controls.items())},
        'main_document_year_title_present':bool(re.search('2012.{0,20}(?:四季度|4季度|第4季度|第四季度)',text,re.S)),
        'FIB_structure':{k:v for k,v in structure.items() if k!='pieces'},'ole_parse_issues':issues,'olefile_version':olefile.__version__}
 return text,structure,facts,licences
def run(s):
 check(s)
 if read(RESULT)['state']!='not_run':raise ValueError('preserve first native-only wrapper attempt')
 local=Path(s['native_directory']);assert not local.exists() and 'onedrive' not in str(local).lower();local.mkdir(parents=True)
 start=time.monotonic();state='native_only_nonCOM_piece_probe_failed';facts={}
 try:
  text,structure,facts,licences=decode(s)
  (local/'2012Q4.main-text.txt').write_text(text,encoding='utf-8',newline='')
  write(local/'2012Q4.piece-locators.json',structure)
  for i,(name,data) in enumerate(licences.items(),1):(local/('olefile-licence-'+str(i)+'.txt')).write_bytes(data)
  if time.monotonic()-start>s['max_seconds']:raise TimeoutError('native piece decoding ceiling')
  state='actual_native_only_nonCOM_main_text_recovered_classes_not_activated'
 except Exception as e:facts.update(error_type=type(e).__name__,error=str(e)[:180])
 check(s);manifest=local/'native_piece_manifest.json';write(manifest,{'files':{str(p):sha(p) for p in local.iterdir() if p.is_file()}})
 r={'schema':'regis.csrc.native_piece_result.v1','state':state,'observed_utc':datetime.now(timezone.utc).isoformat(),
    'elapsed_seconds':round(time.monotonic()-start,3),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),
    'strict_FIB_core_sha256':sha(ROOT/'research/csrc_doc_piece_probe.py'),'facts':facts,
    'local_details':{'path':str(manifest),'sha256':sha(manifest)},'new_GETs':0,'Office_COM_started':False,
    'source_snapshot_replaced':False,'industry_member_classes_activated':0,'price_arrays_read':False,'economic_runs':0,
    'model_admitted':False,'full_goal_complete':False,'limits':s['limits']}
 write(RESULT,r);print(json.dumps({'state':state,'elapsed_seconds':r['elapsed_seconds'],'unique_six_digit_tokens':facts.get('unique_six_digit_tokens'),'token_occurrences':facts.get('six_digit_token_occurrences'),'error_type':facts.get('error_type'),'error':facts.get('error'),'classes_activated':0},ensure_ascii=False))
def verify(s):
 check(s);r=read(RESULT)
 if r['state']=='not_run' or r['protocol_sha256']!=sha(PROTOCOL) or r['implementation_sha256']!=sha(__file__) or sha(r['local_details']['path'])!=r['local_details']['sha256']:raise ValueError('native piece first capture changed')
 for p,h in read(r['local_details']['path'])['files'].items():
  if sha(p)!=h:raise ValueError('native first text/licence evidence changed')
 if r['state']=='actual_native_only_nonCOM_main_text_recovered_classes_not_activated':
  text,structure,facts,licences=decode(s);local=Path(r['local_details']['path']).parent
  with (local/'2012Q4.main-text.txt').open(encoding='utf-8',newline='') as f:retained=f.read()
  if text!=retained or structure!=read(local/'2012Q4.piece-locators.json') or facts!=r['facts']:raise ValueError('byte piece reconstruction/metadata replay failed')
  for i,data in enumerate(licences.values(),1):
   if (local/('olefile-licence-'+str(i)+'.txt')).read_bytes()!=data:raise ValueError('two exact licence copies changed')
 if any(r[k] for k in ['new_GETs','Office_COM_started','source_snapshot_replaced','industry_member_classes_activated','price_arrays_read','economic_runs','model_admitted','full_goal_complete']):raise ValueError('native piece scope escalated')
 print(json.dumps({'native_nonCOM_first_capture_and_exact_source_replay_verified':True,'classes_not_activated':True,'economic_runs':0}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--run',action='store_true');ap.add_argument('--verify',action='store_true');a=ap.parse_args();s=read(PROTOCOL)
 if a.run==a.verify:raise SystemExit('Select one mode')
 run(s) if a.run else verify(s)
