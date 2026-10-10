"""Read-only reconstruction of first public-source capture including Windows newlines."""
import argparse,hashlib,json,os
from pathlib import Path
from datetime import datetime,timezone
from pypdf import PdfReader
import recover_stock_alias_sources as first
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/STOCK_ALIAS_TEXT_VERIFY_PROTOCOL_20261003.json'
RESULT=ROOT/'research/STOCK_ALIAS_TEXT_VERIFY_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def derive(s):
 for p,h in s['input_hashes'].items():
  if sha(ROOT/p)!=h:raise ValueError('frozen_text_verify_input_changed')
 source_spec=read(ROOT/s['original_protocol']);first.check(source_spec);r=read(ROOT/s['original_result'])
 if r['protocol_sha256']!=sha(ROOT/s['original_protocol']) or r['implementation_sha256']!=sha(first.__file__) or sha(r['local_details']['path'])!=r['local_details']['sha256']:raise ValueError('original_capture_changed')
 files=read(r['local_details']['path'])['files']
 for p,h in files.items():
  if sha(p)!=h:raise ValueError('original_native_capture_changed')
 failure=read(ROOT/s['first_failed_receipt'])
 if failure['returncode']!=1 or failure['timed_out'] or 'retained_page_text_differs_from_reconstruction' not in failure['stderr'] or failure['inputs_before']!=failure['inputs_after'] or failure['contract_sha256']!=s['first_contract']:raise ValueError('first_failed_receipt_not_preserved')
 packet=read(Path(r['local_details']['path']).parent/'source_packet.json')['sources'];pages=[];failed=[]
 if len(packet)!=4 or r['actual_GETs']!=4:raise ValueError('capture_scope_changed')
 for target,x,y in zip(source_spec['targets'],packet,r['sources']):
  if x['id']!=target['id'] or x['fetch']['url']!=target['url'] or x['old']!=target['old'] or x['new']!=target['new']:raise ValueError('capture_target_changed')
  if x['state']!='PDF_captured_claims_not_adjudicated':
   if y['state']!=x['state']:raise ValueError('source_failure_changed')
   failed.append({'id':x['id'],'state':x['state'],'native_sha256':sha(x['native_path']) if Path(x['native_path']).exists() else None});continue
  raw=Path(x['native_path']).read_bytes()
  if not raw.startswith(b'%PDF-') or b'%%EOF' not in raw[-4096:] or sha(x['native_path'])!=x['fetch']['sha256']:raise ValueError('PDF_original_integrity_changed')
  pdf=PdfReader(x['native_path'],strict=True)
  if len(pdf.pages)!=x['pages'] or len(pdf.pages)>32:raise ValueError('PDF_page_scope_changed')
  for i,(page,pred) in enumerate(zip(pdf.pages,x['page_predicates']),1):
   if len(page.get_contents().get_data())>4000000:raise ValueError('source_page_stream_cap')
   text=page.extract_text() or '';logical=hashlib.sha256(text.encode('utf-8')).hexdigest();retained=Path(pred['text_path']).read_bytes();expected=text.replace('\n','\r\n').encode('utf-8')
   if logical!=pred['text_sha256'] or retained!=expected or files[pred['text_path']]!=hashlib.sha256(retained).hexdigest():raise ValueError('first_logicalLF_and_actualCRLF_not_reconstructed')
   compact=''.join(text.split());observed={'page':i,'old_code_present':target['old'].split('.')[0] in compact,'new_code_present':target['new'].split('.')[0] in compact,'expected_effective_date_literal_present':target['date_literal'] in compact,'implementation_term_present':'实施' in compact}
   if any(pred[k]!=v for k,v in observed.items()):raise ValueError('original_page_predicate_changed')
   pages.append({'id':x['id'],'page':i,'logical_LF_text_sha256':logical,'actual_retained_CRLF_text_sha256':hashlib.sha256(retained).hexdigest(),'first_hash_field_was_logical_not_actual_native_bytes':logical!=hashlib.sha256(retained).hexdigest(),'source_predicates_unchanged':True})
 return {'pages':pages,'original_source_failures':failed,'new_GETs':0,'original_native_files_changed':0,'original_code_protocol_result_changed':False,'mapping_applied':False,'new_price_or_cash_reads':False,'economic_runs':0}
def run(s):
 if read(RESULT)['state']!='not_run':raise ValueError('first_verify_reconciliation_preserved')
 facts=derive(s);r={'schema':'regis.stock.alias_text_verify_result.v1','state':'actual_original_LF_hash_and_native_CRLF_bytes_separately_reconstructed','observed_utc':datetime.now(timezone.utc).isoformat(),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),'facts':facts,'P2_first_hash_label_error_preserved':True,'P1_identity_risk_resolved':False,'model_admitted':False,'full_goal_complete':False};write(RESULT,r)
 print(json.dumps({'pages_reconstructed':len(facts['pages']),'first_hash_label_error_preserved':True,'new_GETs':0,'source_failures':len(facts['original_source_failures'])}))
def verify(s):
 r=read(RESULT)
 if r['state']!='actual_original_LF_hash_and_native_CRLF_bytes_separately_reconstructed' or r['protocol_sha256']!=sha(PROTOCOL) or r['implementation_sha256']!=sha(__file__) or derive(s)!=r['facts']:raise ValueError('source_reconstruction_changed')
 print(json.dumps({'alias_source_and_text_bytes_verified':True,'first_LF_hash_label_failure_preserved':True,'native_changed':False,'mapping_applied':False}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--run',action='store_true');ap.add_argument('--verify',action='store_true');a=ap.parse_args()
 if a.run==a.verify:raise SystemExit('select_one_mode')
 run(read(PROTOCOL)) if a.run else verify(read(PROTOCOL))
