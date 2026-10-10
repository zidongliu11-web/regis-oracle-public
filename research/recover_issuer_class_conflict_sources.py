"""Two issuer reports: native pages and specific identity/class-claim predicates only."""
import argparse,hashlib,json,re,time,urllib.request,urllib.parse,urllib.error
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from pypdf import PdfReader
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/ISSUER_CLASS_CONFLICT_SOURCE_PROTOCOL_20261003.json';RESULT=ROOT/'research/ISSUER_CLASS_CONFLICT_SOURCE_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def inputs(s):
 for p,h in s['input_hashes'].items():
  if sha(ROOT/p)!=h:raise ValueError('frozen_class_source_changed')
 for p,h in s['native_input_hashes'].items():
  if sha(p)!=h:raise ValueError('native_earlier_evidence_changed')
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None
def request(url,body,dest,cap):
 headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.cninfo.com.cn/','Accept':'application/json' if body else 'application/pdf'}
 if body:headers['Content-Type']='application/x-www-form-urlencoded; charset=UTF-8'
 receipt={'url':url,'method':'POST' if body else 'GET','started_utc':datetime.now(timezone.utc).isoformat(),'attempts':1,'retries':0,'byte_cap':cap};data=b''
 try:
  with urllib.request.build_opener(NoRedirect).open(urllib.request.Request(url,data=body,headers=headers),timeout=15) as response:
   data=response.read(cap+1);receipt.update(status=response.status,content_type=response.headers.get('Content-Type'),within_cap=len(data)<=cap)
   if len(data)>cap:data=data[:cap]
 except urllib.error.HTTPError as e:receipt.update(status=e.code,error_type='HTTPError')
 except Exception as e:receipt.update(error_type=type(e).__name__,error_message_exported=False)
 if data:dest.write_bytes(data)
 receipt.update(bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),ended_utc=datetime.now(timezone.utc).isoformat());write(dest.with_suffix(dest.suffix+'.receipt.json'),receipt);return receipt
def predicates(job,texts):
 compact=[''.join(t.split()) for t in texts];first=''.join(compact[:5]);matches=[]
 # Require explicit issuer declaration of the CSRC2012 guideline, not incidental C33/customer mention.
 for i,text in enumerate(compact):
  for match in re.finditer(r'上市公司行业分类指引',text):
   fragment=text[max(0,match.start()-100):match.end()+240]
   if '2012' in fragment and (job['class_code'] in fragment or job['class_name'] in fragment):matches.append(i+1);break
 return {'issuer_code_first5pages':job['issuer_code'] in first,'issuer_name_first5pages':job['issuer_name'] in first,'annual_title_first5pages':'2016年年度报告' in first,'explicit_guideline_and_class_claim_pages':matches,'explicit_claim_present':bool(matches)}
def event(job,s,local):
 d=local/job['issuer_code'];d.mkdir();form=dict(s['query_form']);form.update(searchkey=job['query_name'],seDate=job['announcement_date']+'~'+job['announcement_date']);q=d/'query.json';qr=request(s['query_url'],urllib.parse.urlencode(form).encode(),q,s['query_cap']);e={'issuer_code':job['issuer_code'],'ann_date':job['announcement_date'],'POSTs':1,'PDF_GETs':0,'state':'actual_query_failed_preserved','native_financial_or_fulltext_exported':False,'classification_corrected':False}
 if qr.get('status')!=200 or not qr.get('within_cap'):return e
 data=read(q);ann=data.get('announcements');assert isinstance(ann,list) and len(ann)<=30;matches=[]
 for a in ann:
  title=''.join(str(a.get('announcementTitle','')).replace('<em>','').replace('</em>','').split())
  if a.get('secCode')==job['issuer_code'] and title.endswith('2016年年度报告'):
   adj=str(a.get('adjunctUrl',''));assert adj.startswith('finalpage/'+job['announcement_date']+'/') and adj.lower().endswith('.pdf') and '..' not in adj and not urllib.parse.urlsplit(adj).scheme
   matches.append({'title':title,'announcementId':str(a.get('announcementId')),'announcementTime':a.get('announcementTime'),'url':'https://static.cninfo.com.cn/'+adj})
 e.update(returned_rows=len(ann),has_more=bool(data.get('hasMore')),exact_issuer_report_matches=len(matches))
 if len(matches)!=1:e['state']='no_unique_primary_report_no_PDF_guess';return e
 locator=matches[0];pdf=d/'issuer-original.pdf';e.update(locator=locator,PDF_GETs=1);pr=request(locator['url'],None,pdf,s['PDF_cap'])
 if pr.get('status')!=200 or not pr.get('within_cap') or not pdf.read_bytes().startswith(b'%PDF-'):e['state']='actual_report_download_failed_preserved';return e
 reader=PdfReader(pdf,strict=False)
 if len(reader.pages)>s['page_cap']:e.update(state='actual_report_page_cap_failed_preserved',pages=len(reader.pages));return e
 texts=[];pages=[]
 for i,page in enumerate(reader.pages[:s['page_scan_cap']]):
  text=page.extract_text() or '';p=d/('page-'+str(i+1)+'.txt');p.write_text(text,encoding='utf-8',newline='\n');texts.append(text);pages.append({'page':i+1,'path':str(p),'sha256':sha(p)})
 pred=predicates(job,texts);e.update(state='actual_primary_issuer_annual_targeted_claims_retained',native_PDF=str(pdf),pdf_sha256=sha(pdf),pages=len(reader.pages),scanned_pages=len(texts),native_pages=pages,predicates=pred,claim_identity_predicates_all=pred['issuer_code_first5pages'] and pred['issuer_name_first5pages'] and pred['annual_title_first5pages'] and pred['explicit_claim_present'],CSRC_duplicate_rows_repaired=False,historical_hour_or_original_vintage_certified=False)
 return e
def run(s):
 assert read(RESULT)['state']=='not_run';inputs(s);local=Path(s['native_directory']);assert not local.exists() and 'onedrive' not in str(local).lower();local.mkdir();start=time.monotonic()
 def retained(job):
  try:return event(job,s,local)
  except Exception as e:
   d=local/job['issuer_code'];return {'issuer_code':job['issuer_code'],'state':'actual_issuer_claim_attempt_failed_preserved','error_type':type(e).__name__,'error_message_exported':False,'POSTs':int((d/'query.json.receipt.json').exists()),'PDF_GETs':int((d/'issuer-original.pdf.receipt.json').exists()),'classification_corrected':False}
 with ThreadPoolExecutor(max_workers=2) as p:events=list(p.map(retained,s['jobs']))
 inputs(s);manifest=local/'issuer_class_conflict_manifest.json';write(manifest,{'files':{str(p):sha(p) for p in local.rglob('*') if p.is_file()}})
 r={'state':'actual_two_dated_primary_issuer_class_source_attempts','observed_utc':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':round(time.monotonic()-start,3),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),'events':events,'actual_POSTs':sum(e['POSTs'] for e in events),'actual_PDF_GETs':sum(e['PDF_GETs'] for e in events),'local_details':{'path':str(manifest),'sha256':sha(manifest)},'two_source_conflicts_quarantined':True,'CSRC_original_rows_or_class_modified':False,'source_classes_activated':0,'full_original_vintage_or_hourly_PIT_certified':False,'quote_arrays_read':False,'economic_runs':0,'model_admitted':False,'full_goal_complete':False};write(RESULT,r);print(json.dumps({'state':r['state'],'actual_POSTs':r['actual_POSTs'],'actual_PDF_GETs':r['actual_PDF_GETs'],'specific_identity_class_claims_passed':sum(e.get('claim_identity_predicates_all',False) for e in events),'source_classes_activated':0}))
def verify(s):
 inputs(s);r=read(RESULT);assert r['protocol_sha256']==sha(PROTOCOL) and r['implementation_sha256']==sha(__file__) and sha(r['local_details']['path'])==r['local_details']['sha256']
 for p,h in read(r['local_details']['path'])['files'].items():assert sha(p)==h
 for e in r['events']:
  if 'native_pages' not in e:continue
  reader=PdfReader(e['native_PDF'],strict=False);texts=[]
  for page in e['native_pages']:
   text=reader.pages[page['page']-1].extract_text() or '';assert Path(page['path']).read_bytes()==text.encode('utf-8');texts.append(text)
  job=next(j for j in s['jobs'] if j['issuer_code']==e['issuer_code']);assert predicates(job,texts)==e['predicates']
 assert not any(r[k] for k in ['CSRC_original_rows_or_class_modified','source_classes_activated','full_original_vintage_or_hourly_PIT_certified','quote_arrays_read','economic_runs','model_admitted','full_goal_complete'])
 print(json.dumps({'two_targeted_native_issuer_claims_rebuilt':True,'GET_POST_or_economic_reruns':0,'source_classes_activated':0,'model_admitted':False}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();m=ap.add_mutually_exclusive_group(required=True);m.add_argument('--run',action='store_true');m.add_argument('--verify',action='store_true');a=ap.parse_args();run(read(PROTOCOL)) if a.run else verify(read(PROTOCOL))
