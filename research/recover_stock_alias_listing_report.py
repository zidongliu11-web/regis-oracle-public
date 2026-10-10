"""One distinct issuer listing report after the retained SSE HTML failure."""
import argparse,hashlib,json,os,subprocess,sys,time
from pathlib import Path
from datetime import datetime,timezone
from pypdf import PdfReader
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/STOCK_ALIAS_LISTING_SOURCE_PROTOCOL_20261003.json';RESULT=ROOT/'research/STOCK_ALIAS_LISTING_SOURCE_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def check(s):
 for p,h in s['input_hashes'].items():
  if sha(ROOT/p)!=h:raise ValueError('listing_source_frozen_input_changed')
def derive(s,dest):
 raw=Path(dest).read_bytes()
 if not raw.startswith(b'%PDF-') or b'%%EOF' not in raw[-4096:]:raise ValueError('listing_source_not_complete_PDF')
 pdf=PdfReader(str(dest),strict=True)
 if len(pdf.pages)!=42:raise ValueError('expected_exact42page_report_changed')
 texts={}
 for page in [6,9]:
  p=pdf.pages[page-1]
  if len(p.get_contents().get_data())>4000000:raise ValueError('listing_source_stream_cap')
  texts[page]=p.extract_text() or ''
 c6=''.join(texts[6].split());c9=''.join(texts[9].split())
 predicates={'old601313_and_original_issuer_on_page6':'原名“江南嘉捷电梯股份有限公司”，股票代码601313' in c6,'effective_date_and_new601360_on_page6':'更自2018年2月28日起变更为601360' in c6,'current601360_on_page9':'股票代码：601360' in c9,'Shanghai_listing_venue_on_page9':'股票上市地：上海证券交易所' in c9,'issuer_name_on_page9':'三六零安全科技股份有限公司' in c9}
 return texts,predicates
def run(s):
 check(s)
 if read(RESULT)['state']!='not_run':raise ValueError('first_listing_capture_preserved')
 local=Path(s['native_directory']);assert not local.exists() and 'onedrive' not in str(local).lower();local.mkdir(parents=True);clock=time.monotonic();dest=local/'601313-to-601360.listing-report.pdf';receipt=local/'GET.receipt.json'
 try:
  subprocess.run([sys.executable,'-X','utf8',str(ROOT/'research/stock_alias_fetch_one.py'),s['url'],str(dest),str(receipt),str(s['max_bytes'])],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
  get=read(receipt);state='source_capture_failed';predicates={}
  if get['state']=='captured':
   try:
    texts,predicates=derive(s,dest)
    for page,text in texts.items():(local/f'page{page}.txt').write_text(text,encoding='utf-8',newline='\n')
    state='listing_source_PDF_and_targeted_claim_predicates_captured'
   except Exception as e:state='captured_listing_PDF_validation_failed';get['validation_error_type']=type(e).__name__
 except subprocess.TimeoutExpired:
  get=read(receipt) if receipt.exists() else {'url':s['url']};get.update(state='failed',error_type='ParentWallDeadline');write(receipt,get);state='source_capture_failed';predicates={}
 check(s);manifest=local/'native_listing_source_manifest.json';write(manifest,{'files':{str(p):sha(p) for p in local.iterdir() if p.is_file()}})
 r={'schema':'regis.stock.alias_listing_source_result.v1','state':state,'observed_utc':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':round(time.monotonic()-clock,3),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),'actual_GETs':1,'source_url':s['url'],'source_sha256':get.get('sha256'),'source_bytes':get.get('bytes'),'original_source_pages':42 if predicates else None,'targeted_text_pages':[6,9] if predicates else [],'predicates':predicates,'native_source':str(dest),'local_details':{'path':str(manifest),'sha256':sha(manifest)},'prior_SSE_HTML_capture_unchanged':True,'mapping_applied':False,'quote_or_cash_reads':False,'economic_runs':0,'model_admitted':False,'full_goal_complete':False,'limits':s['limits']};write(RESULT,r)
 print(json.dumps({'state':state,'actual_GETs':1,'all_targeted_predicates':bool(predicates) and all(predicates.values()),'elapsed_seconds':r['elapsed_seconds']}))
def verify(s):
 check(s);r=read(RESULT)
 if r['protocol_sha256']!=sha(PROTOCOL) or r['implementation_sha256']!=sha(__file__) or sha(r['local_details']['path'])!=r['local_details']['sha256']:raise ValueError('listing_source_evidence_changed')
 for p,h in read(r['local_details']['path'])['files'].items():
  if sha(p)!=h:raise ValueError('native_listing_source_changed')
 if r['state']=='listing_source_PDF_and_targeted_claim_predicates_captured':
  texts,predicates=derive(s,r['native_source'])
  if predicates!=r['predicates']:raise ValueError('listing_source_predicates_changed')
  for page,text in texts.items():
   if Path(Path(r['native_source']).parent/f'page{page}.txt').read_bytes()!=text.encode('utf-8'):raise ValueError('listing_source_LF_page_bytes_changed')
 if r['actual_GETs']!=1 or any(r[k] for k in ['mapping_applied','quote_or_cash_reads','economic_runs','model_admitted','full_goal_complete']):raise ValueError('listing_source_scope_changed')
 print(json.dumps({'listing_source_capture_verified':True,'identity_mapping_still_pending':True}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--run',action='store_true');ap.add_argument('--verify',action='store_true');a=ap.parse_args()
 if a.run==a.verify:raise SystemExit('select_one_mode')
 run(read(PROTOCOL)) if a.run else verify(read(PROTOCOL))
