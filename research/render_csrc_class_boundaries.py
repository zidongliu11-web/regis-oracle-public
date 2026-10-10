"""Render only frozen publisher pages covering known class disagreements."""
import argparse,hashlib,json,subprocess
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/CSRC_CLASS_BOUNDARY_PROTOCOL_20261003.json';RESULT=ROOT/'research/CSRC_CLASS_BOUNDARY_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def check(s):
 for p,h in s['input_hashes'].items():
  if sha(ROOT/p)!=h:raise ValueError('frozen boundary input changed')
 for p,h in s['native_input_hashes'].items():
  if sha(p)!=h:raise ValueError('first native/source evidence changed')
def run(s):
 check(s)
 if read(RESULT)['state']!='not_run':raise ValueError('preserve first bounded render')
 local=Path(s['native_directory']);assert not local.exists() and 'onedrive' not in str(local).lower();local.mkdir(parents=True);captured=[]
 if len(s['targets'])!=8:raise ValueError('exact eight source pages only')
 for t in s['targets']:
  stem=local/(t['quarter']+'-page'+str(t['page']));args=[s['pdftoppm'],'-f',str(t['page']),'-l',str(t['page']),'-scale-to','1600','-png','-singlefile',t['native_pdf'],str(stem)]
  timed_out=False
  try:p=subprocess.run(args,capture_output=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
  except subprocess.TimeoutExpired:timed_out=True;p=None
  image=stem.with_suffix('.png');entry=dict(t,returncode=p.returncode if p is not None else None,timed_out=timed_out,native_image=str(image),native_image_sha256=sha(image) if image.exists() else None,actual_visual_viewed=False)
  if timed_out or p.returncode or not image.exists():entry['state']='source_page_render_failed_retained'
  else:entry['state']='source_page_rendered_visual_adjudication_pending'
  captured.append(entry)
 check(s);manifest=local/'boundary_render_manifest.json';write(manifest,{'files':{str(p):sha(p) for p in local.iterdir() if p.is_file()}})
 r={'schema':'regis.csrc.class_boundary_result.v1','state':'actual_bounded_eight_page_render','observed_utc':datetime.now(timezone.utc).isoformat(),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),'pages':captured,'local_details':{'path':str(manifest),'sha256':sha(manifest)},'actual_visual_adjudication_pending':True,'class_assignments_changed':0,'coverage_replayed':False,'price_arrays_read':False,'economic_runs':0,'model_admitted':False,'full_goal_complete':False}
 write(RESULT,r);print(json.dumps({'state':r['state'],'rendered':sum(p['state']=='source_page_rendered_visual_adjudication_pending' for p in captured),'class_assignments_changed':0}))
def verify(s):
 check(s);r=read(RESULT)
 if r['state']!='actual_bounded_eight_page_render' or r['protocol_sha256']!=sha(PROTOCOL) or r['implementation_sha256']!=sha(__file__) or sha(r['local_details']['path'])!=r['local_details']['sha256']:raise ValueError('bounded render identity changed')
 for p,h in read(r['local_details']['path'])['files'].items():
  if sha(p)!=h:raise ValueError('native publisher image changed')
 if any(r[k] for k in ['class_assignments_changed','coverage_replayed','price_arrays_read','economic_runs','model_admitted','full_goal_complete']):raise ValueError('render scope escalated')
 print(json.dumps({'eight_original_pages_and_first_inputs_verified':True,'adjudication_pending':True,'economic_runs':0}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--run',action='store_true');ap.add_argument('--verify',action='store_true');a=ap.parse_args();s=read(PROTOCOL)
 if a.run==a.verify:raise SystemExit('Select one mode')
 run(s) if a.run else verify(s)
