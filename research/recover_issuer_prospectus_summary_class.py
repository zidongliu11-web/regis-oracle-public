"""One observed primary prospectus; identity and exact industry paragraph only."""
import argparse,hashlib,json,re,time,urllib.request,urllib.error
from pathlib import Path
from datetime import datetime,timezone
from pypdf import PdfReader
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/ISSUER_PROSPECTUS_SUMMARY_CLASS_PROTOCOL_20261003.json';RESULT=ROOT/'research/ISSUER_PROSPECTUS_SUMMARY_CLASS_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def inputs(s):
 for p,h in s['input_hashes'].items():
  if sha(ROOT/p)!=h:raise ValueError('frozen_prospectus_input_changed')
 for p,h in s['native_input_hashes'].items():
  if sha(p)!=h:raise ValueError('retained_source_changed')
def claims(s,texts):
 rows=read(s['locator_native_query_path'])['announcements'];bound=[]
 for row in rows:
  title=''.join(str(row.get('announcementTitle','')).replace('<em>','').replace('</em>','').split())
  if row.get('secCode')==s['issuer_code'] and title==s['document_title'] and str(row.get('announcementId'))==s['announcement_id'] and 'https://static.cninfo.com.cn/'+str(row.get('adjunctUrl',''))==s['observed_PDF_URL']:bound.append(row)
 assert len(bound)==1
 compact=[''.join(t.split()) for t in texts];header=''.join(compact[:5]);hits=[]
 for i,t in enumerate(compact):
  for m in re.finditer('上市公司行业分类指引',t):
   excerpt=t[max(0,m.start()-40):m.end()+220]
   if all(x in excerpt for x in ['2012','本公司','C33','金属制品业']):hits.append({'page':i+1,'start_character_compact':max(0,m.start()-40),'end_character_compact':m.end()+220,'claim_excerpt_SHA256':hashlib.sha256(excerpt.encode()).hexdigest(),'actual_class_code':'C33','actual_class_name':'金属制品业','issuer_self_declaration':True})
 return {'issuer_name_first5pages':s['issuer_name'] in header,'prospectus_title_first5pages':'招股说明书' in header,'native_query_secCode_bound':len(bound)==1,'exact_2012_guideline_self_class_claims':hits,'claims_present':bool(hits)}
def prior_failure(s):
 d=Path(s['prior_full_native_directory']);receipt=read(d/'prospectus-get.receipt.json');p=d/'issuer-prospectus.pdf';first=read(ROOT/'research/ISSUER_PROSPECTUS_CLASS_PROTOCOL_20261003.json')
 pages=len(PdfReader(p,strict=False).pages);assert receipt['status']==200 and receipt['within_cap'] and pages>first['page_cap'] and not list(d.glob('page-*.txt'))
 return {'actual_GET_status':receipt['status'],'within_byte_cap':True,'bytes':receipt['bytes'],'PDF_sha256':sha(p),'actual_pages':pages,'unchanged_page_cap':first['page_cap'],'native_text_pages':0,'specific_failure_stage':'page_count_exceeded_before_text_extraction','original_generic_failure_state_preserved':True,'old_verify_predicates_flag_not_identity_class_certification':True,'old_protocol_SHA256':sha(ROOT/'research/ISSUER_PROSPECTUS_CLASS_PROTOCOL_20261003.json'),'old_result_SHA256':sha(ROOT/'research/ISSUER_PROSPECTUS_CLASS_RESULT_20261003.json')}

class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None
def run(s):
 assert read(RESULT)['state']=='not_run';inputs(s);local=Path(s['native_directory']);assert not local.exists() and 'onedrive' not in str(local).lower();local.mkdir();start=time.monotonic();p=local/'issuer-prospectus.pdf';receipt={'url':s['observed_PDF_URL'],'started_utc':datetime.now(timezone.utc).isoformat(),'attempts':1,'retries':0,'byte_cap':s['PDF_cap']};data=b'';details={};state='actual_prospectus_download_failed_preserved'
 try:
  with urllib.request.build_opener(NoRedirect).open(urllib.request.Request(s['observed_PDF_URL'],headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.cninfo.com.cn/'}),timeout=15) as response:
   data=response.read(s['PDF_cap']+1);receipt.update(status=response.status,within_cap=len(data)<=s['PDF_cap'],content_type=response.headers.get('Content-Type'));p.write_bytes(data[:s['PDF_cap']]);assert receipt['within_cap'] and data.startswith(b'%PDF-')
  reader=PdfReader(p,strict=False);assert len(reader.pages)<=s['page_cap'];texts=[];pages=[]
  for i,page in enumerate(reader.pages[:s['page_scan_cap']]):
   text=page.extract_text() or '';native=local/('page-'+str(i+1)+'.txt');native.write_text(text,encoding='utf-8',newline='\n');texts.append(text);pages.append({'page':i+1,'path':str(native),'sha256':sha(native)})
  pred=claims(s,texts);details.update(native_PDF=str(p),PDF_sha256=sha(p),pages=len(reader.pages),scanned_pages=len(texts),native_pages=pages,predicates=pred);state='actual_primary_prospectus_summary_specific_class_claim_native_only'
 except urllib.error.HTTPError as e:receipt.update(status=e.code,error_type='HTTPError')
 except Exception as e:details.update(error_type=type(e).__name__,error_message_exported=False)
 receipt.update(bytes=min(len(data),s['PDF_cap']),sha256=sha(p) if p.exists() else hashlib.sha256(b'').hexdigest(),ended_utc=datetime.now(timezone.utc).isoformat());write(local/'prospectus-get.receipt.json',receipt);inputs(s);manifest=local/'prospectus_class_manifest.json';write(manifest,{'files':{str(p):sha(p) for p in local.rglob('*') if p.is_file()}})
 r={'state':state,'observed_utc':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':round(time.monotonic()-start,3),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),'actual_PDF_GETs':1,'announcement_date':s['announcement_date'],'issuer_code':s['issuer_code'],'details':details,'local_details':{'path':str(manifest),'sha256':sha(manifest)},'prior_full_source_failure':prior_failure(s),'source_classes_activated':0,'2017_quarterly_quarantines_preserved':True,'historic_hour_or_original_vintage_certified':False,'quote_arrays_read':False,'economic_runs':0,'model_admitted':False,'full_goal_complete':False};write(RESULT,r);print(json.dumps({'state':state,'elapsed_seconds':r['elapsed_seconds'],'actual_PDF_GETs':1,'specific_class_claims':len(details.get('predicates',{}).get('exact_2012_guideline_self_class_claims',[])),'source_classes_activated':0}))
def verify(s):
 inputs(s);r=read(RESULT);assert r['protocol_sha256']==sha(PROTOCOL) and r['implementation_sha256']==sha(__file__) and sha(r['local_details']['path'])==r['local_details']['sha256']
 for p,h in read(r['local_details']['path'])['files'].items():assert sha(p)==h
 assert prior_failure(s)==r['prior_full_source_failure']
 d=r['details']
 if 'native_pages' in d:
  reader=PdfReader(d['native_PDF'],strict=False);texts=[]
  for pg in d['native_pages']:
   text=reader.pages[pg['page']-1].extract_text() or '';assert Path(pg['path']).read_bytes()==text.encode();texts.append(text)
  assert claims(s,texts)==d['predicates']
 assert not any(r[k] for k in ['source_classes_activated','historic_hour_or_original_vintage_certified','quote_arrays_read','economic_runs','model_admitted','full_goal_complete'])
 print(json.dumps({'summary_identity_class_predicates_rebuilt':'native_pages' in d,'prior_full_page_cap_failure_rebuilt':True,'network_reruns':0,'source_classes_activated':0,'model_admitted':False}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();m=ap.add_mutually_exclusive_group(required=True);m.add_argument('--run',action='store_true');m.add_argument('--verify',action='store_true');a=ap.parse_args();run(read(PROTOCOL)) if a.run else verify(read(PROTOCOL))
