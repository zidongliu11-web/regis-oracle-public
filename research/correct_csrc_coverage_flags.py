"""Separate derivative correcting admission flags; first capture stays immutable."""
import argparse,json,hashlib
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/CSRC_COVERAGE_FLAG_REPAIR_PROTOCOL_20261003.json';RESULT=ROOT/'research/CSRC_COVERAGE_FLAG_REPAIR_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def check(s):
 for p,h in s['input_hashes'].items():
  if sha(ROOT/p)!=h:raise ValueError('first capture source changed')
 for p,h in s['native_input_hashes'].items():
  if sha(p)!=h:raise ValueError('first native coverage changed')
def normalize(first):
 result=json.loads(json.dumps(first));changed=[]
 for snap in result['snapshots']:
  snap['first_capture_structural_flag_value']=snap['source_snapshot_complete']
  if snap['state']=='unknown_source_member_marker':
   if snap['members']:raise ValueError('unknown original snapshot has members')
   if snap['source_snapshot_complete']:changed.append(snap['quarter'])
   snap['source_snapshot_complete']=False
 result['derivative_scope']='Admission flag correction only; original preserved separately; no new class assignment'
 return result,changed
def run(s):
 check(s)
 if read(RESULT)['state']!='not_run':raise ValueError('retain first flag correction')
 local=Path(s['native_directory']);assert not local.exists() and 'onedrive' not in str(local).lower();local.mkdir(parents=True)
 result,changed=normalize(read(s['first_native_sequence']))
 if changed!=s['expected_flag_corrections']:raise ValueError('unexpected original flag mismatches')
 target=local/'explicit_snapshot_sequence_flags_corrected.json';write(target,result);check(s)
 r={'schema':'regis.csrc.coverage_flag_repair.v1','state':'actual_separate_admission_flags_corrected','observed_utc':datetime.now(timezone.utc).isoformat(),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),'first_capture_unchanged':True,'corrected_quarters':changed,'unknown_quarter_markers':sum(x['state']=='unknown_source_member_marker' for x in result['snapshots']),'published_marker_count':len(result['snapshots']),'native_corrected_sequence':str(target),'native_corrected_sha256':sha(target),'first_source_rows_semantically_admitted':False,'class_assignments_changed':0,'coverage_numbers_changed':0,'price_arrays_read':False,'sector_factor_computed':False,'economic_runs':0,'model_admitted':False,'full_goal_complete':False}
 write(RESULT,r);print(json.dumps({k:r[k] for k in ['state','corrected_quarters','published_marker_count','coverage_numbers_changed']}))
def verify(s):
 check(s);r=read(RESULT)
 if r['state']!='actual_separate_admission_flags_corrected' or r['protocol_sha256']!=sha(PROTOCOL) or r['implementation_sha256']!=sha(__file__) or sha(r['native_corrected_sequence'])!=r['native_corrected_sha256']:raise ValueError('flag correction identity changed')
 expected,changed=normalize(read(s['first_native_sequence']))
 if read(r['native_corrected_sequence'])!=expected or changed!=s['expected_flag_corrections'] or r['corrected_quarters']!=changed:raise ValueError('corrected sequence differs from bounded normalization')
 if any(r[k] for k in ['class_assignments_changed','coverage_numbers_changed','price_arrays_read','sector_factor_computed','economic_runs','model_admitted','full_goal_complete']):raise ValueError('flag correction scope escalated')
 print(json.dumps({'first_capture_and_separate_flags_verified':True,'changed_flags':len(changed),'economic_runs':0}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--run',action='store_true');ap.add_argument('--verify',action='store_true');a=ap.parse_args();s=read(PROTOCOL)
 if a.run==a.verify:raise SystemExit('Select one mode')
 run(s) if a.run else verify(s)
