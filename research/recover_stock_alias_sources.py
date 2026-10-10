"""Retain four bounded issuer/exchange originals; no alias or class activation."""
import argparse,hashlib,json,os,subprocess,sys,time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from pathlib import Path
from pypdf import PdfReader
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/STOCK_ALIAS_SOURCE_PROTOCOL_20261003.json';RESULT=ROOT/'research/STOCK_ALIAS_SOURCE_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def check(s):
 for p,h in s['input_hashes'].items():
  if sha(ROOT/p)!=h:raise ValueError('frozen_alias_source_input_changed')
def facts(path,target,local,persist=True):
 raw=Path(path).read_bytes()
 if not raw.startswith(b'%PDF-') or b'%%EOF' not in raw[-4096:]:raise ValueError('source_not_complete_PDF')
 pdf=PdfReader(str(path),strict=True)
 if len(pdf.pages)>32:raise ValueError('source_page_cap')
 page_facts=[]
 for i,page in enumerate(pdf.pages,1):
  if len(page.get_contents().get_data())>4000000:raise ValueError('source_page_stream_cap')
  text=page.extract_text() or '';dest=local/(target['id']+f'.page{i}.txt')
  if persist:dest.write_text(text,encoding='utf-8')
  text_sha=hashlib.sha256(text.encode('utf-8')).hexdigest()
  if not persist and sha(dest)!=text_sha:raise ValueError('retained_page_text_differs_from_reconstruction')
  compact=''.join(text.split());page_facts.append({'page':i,'old_code_present':target['old'].split('.')[0] in compact,'new_code_present':target['new'].split('.')[0] in compact,'expected_effective_date_literal_present':target['date_literal'] in compact,'implementation_term_present':'实施' in compact,'text_path':str(dest),'text_sha256':text_sha})
 return {'pages':len(pdf.pages),'page_predicates':page_facts,'source_text_or_maintext_emitted':False}
def run(s):
 check(s)
 if read(RESULT)['state']!='not_run':raise ValueError('first_alias_capture_preserved')
 local=Path(s['native_directory']);assert not local.exists() and 'onedrive' not in str(local).lower();local.mkdir(parents=True);clock=time.monotonic()
 def acquire(target):
  dest=local/(target['id']+'.pdf');receipt=local/(target['id']+'.receipt.json')
  try:
   p=subprocess.run([sys.executable,'-X','utf8',str(ROOT/'research/stock_alias_fetch_one.py'),target['url'],str(dest),str(receipt),str(s['max_bytes_per_source'])],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
   r=read(receipt) if receipt.exists() else {'url':target['url'],'state':'failed','error_type':'NoReceipt','child_returncode':p.returncode}
  except subprocess.TimeoutExpired:
   r=read(receipt) if receipt.exists() else {'url':target['url']};r.update(state='failed',error_type='ParentWallDeadline');write(receipt,r)
  out={'id':target['id'],'old':target['old'],'new':target['new'],'expected_effective_date':target['expected_effective_date'],'state':'source_capture_failed','fetch':r,'native_path':str(dest),'receipt':str(receipt)}
  if r['state']=='captured':
   try:out.update(facts(dest,target,local),state='PDF_captured_claims_not_adjudicated')
   except Exception as e:out.update(state='captured_source_PDF_validation_failed',error_type=type(e).__name__)
  print(json.dumps({'id':out['id'],'state':out['state'],'pages':out.get('pages')},ensure_ascii=False),flush=True);return out
 with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(acquire,s['targets']))
 if time.monotonic()-clock>s['max_seconds']:raise TimeoutError('bounded_alias_source_wall_limit')
 check(s);packet=local/'source_packet.json';write(packet,{'sources':results,'native_only':True});manifest=local/'native_source_manifest.json';write(manifest,{'files':{str(p):sha(p) for p in local.iterdir() if p.is_file()}})
 r={'schema':'regis.stock.alias_source_result.v1','state':'actual_bounded_four_alias_source_capture','observed_utc':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':round(time.monotonic()-clock,3),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),'actual_GETs':len(results),'successful_PDFs':sum(x['state']=='PDF_captured_claims_not_adjudicated' for x in results),'sources':[{'id':x['id'],'old':x['old'],'new':x['new'],'state':x['state'],'pages':x.get('pages'),'URL':x['fetch']['url'],'sha256':x['fetch'].get('sha256'),'bytes':x['fetch'].get('bytes'),'predicate_pages':[{k:v for k,v in p.items() if k not in ['text_path','text_sha256']} for p in x.get('page_predicates',[])]} for x in results],'local_details':{'path':str(manifest),'sha256':sha(manifest)},'mapping_applied':False,'source_classes_backfilled':False,'native_price_or_cash_reads':False,'economic_runs':0,'model_admitted':False,'full_goal_complete':False,'limitations':s['limitations']};write(RESULT,r)
 print(json.dumps({'actual_GETs':r['actual_GETs'],'successful_PDFs':r['successful_PDFs'],'elapsed_seconds':r['elapsed_seconds'],'mapping_applied':False}))
def verify(s):
 check(s);r=read(RESULT)
 if r['state']!='actual_bounded_four_alias_source_capture' or r['protocol_sha256']!=sha(PROTOCOL) or r['implementation_sha256']!=sha(__file__) or sha(r['local_details']['path'])!=r['local_details']['sha256']:raise ValueError('source_evidence_changed')
 manifest=read(r['local_details']['path'])
 for p,h in manifest['files'].items():
  if sha(p)!=h:raise ValueError('native_original_changed')
 sources=read(Path(r['local_details']['path']).parent/'source_packet.json')['sources']
 if len(sources)!=len(s['targets']) or r['actual_GETs']!=4:raise ValueError('source_scope_changed')
 for target,x in zip(s['targets'],sources):
  if x['fetch']['url']!=target['url'] or x['old']!=target['old'] or x['new']!=target['new']:raise ValueError('source_target_changed')
  if x['state']=='PDF_captured_claims_not_adjudicated':
   rebuilt=facts(x['native_path'],target,Path(x['native_path']).parent,persist=False)
   if rebuilt['page_predicates']!=x['page_predicates'] or rebuilt['pages']!=x['pages']:raise ValueError('source_predicates_changed')
 if any(r[k] for k in ['mapping_applied','source_classes_backfilled','native_price_or_cash_reads','economic_runs','model_admitted','full_goal_complete']):raise ValueError('source_capture_scope_escalated')
 print(json.dumps({'alias_originals_verified':True,'original_capture_failures_preserved':True,'mapping_applied':False}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--run',action='store_true');ap.add_argument('--verify',action='store_true');a=ap.parse_args()
 if a.run==a.verify:raise SystemExit('select_one_mode')
 run(read(PROTOCOL)) if a.run else verify(read(PROTOCOL))
