"""Two predeclared issuer-disclosure date/title queries, at most two primary PDFs."""
import argparse,hashlib,json,time,urllib.parse,urllib.request,urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from pathlib import Path
from pypdf import PdfReader
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/STOCK_ALIAS_DIVIDEND_SOURCE_PROTOCOL_20261003.json';RESULT=ROOT/'research/STOCK_ALIAS_DIVIDEND_SOURCE_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def date_predicates(job,texts):
 compact=''.join(''.join(texts).split())
 predicates={'issuer_code':'601313' in compact,'implementation_title':job['title_fragment'] in compact,'issuer_company':job['issuer_company'] in compact}
 for role in ['record_date','ex_date','pay_date']:
  predicates[role]=any(fragment in compact for fragment in job['expected_date_claim_fragments'][role])
 return predicates
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None
def request(url,body,limit,dest):
 headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.cninfo.com.cn/','Accept':'application/json' if body else 'application/pdf'}
 if body:headers['Content-Type']='application/x-www-form-urlencoded; charset=UTF-8'
 receipt={'url':url,'method':'POST' if body else 'GET','started_utc':datetime.now(timezone.utc).isoformat(),'attempts':1,'retries':0,'byte_limit':limit};data=b''
 try:
  with urllib.request.build_opener(NoRedirect).open(urllib.request.Request(url,data=body,headers=headers),timeout=15) as response:
   data=response.read(limit+1);receipt.update(status=response.status,content_type=response.headers.get('Content-Type'),last_modified=response.headers.get('Last-Modified'),within_cap=len(data)<=limit)
   if len(data)>limit:data=data[:limit]
 except urllib.error.HTTPError as e:receipt.update(status=e.code,error_type='HTTPError',redirect=e.headers.get('Location'))
 except Exception as e:receipt.update(error_type=type(e).__name__,error_reason=str(e))
 if data:dest.write_bytes(data)
 receipt.update(bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),body_path=str(dest),ended_utc=datetime.now(timezone.utc).isoformat());write(dest.with_suffix('.receipt.json'),receipt);return receipt
def event(job,local,s):
 directory=local/job['id'];directory.mkdir();form=dict(s['query_form']);form['seDate']=job['announcement_date']+'~'+job['announcement_date'];body=urllib.parse.urlencode(form).encode('utf-8');query=directory/'query.json';qr=request(s['query_url'],body,s['query_max_bytes'],query)
 result={'id':job['id'],'query_receipt':str(query.with_suffix('.receipt.json')),'primary_query_success':False,'primary_PDF_GET_attempts':0,'notice_role':'issuer_implementation_disclosure','native_cash_values_exported':False,'PIT_certified':False}
 if qr.get('status')!=200 or not qr.get('within_cap'):result['state']='actual_primary_query_failed_preserved';return result
 try:data=read(query)
 except Exception:result['state']='actual_query_not_JSON_preserved';return result
 announcements=data.get('announcements')
 if not isinstance(announcements,list) or len(announcements)>int(s['query_form']['pageSize']):result['state']='query_schema_or_row_cap_failed';return result
 result.update(primary_query_success=True,returned_rows=len(announcements),has_more=bool(data.get('hasMore')))
 matches=[]
 for a in announcements:
  title=''.join(str(a.get('announcementTitle','')).replace('<em>','').replace('</em>','').split())
  if job['title_fragment'] in title and a.get('secCode') in ['601313','601360']:
   adj=str(a.get('adjunctUrl',''));parts=urllib.parse.urlsplit(adj)
   if parts.scheme or parts.netloc or '..' in adj or not adj.startswith('finalpage/'+job['announcement_date']+'/') or not adj.lower().endswith('.pdf'):raise ValueError('untrusted_primary_adjunct_path')
   matches.append({'title':title,'secCode':a['secCode'],'announcementId':str(a.get('announcementId')),'announcementTime':a.get('announcementTime'),'url':'https://static.cninfo.com.cn/'+adj})
 result['exact_title_matches']=len(matches)
 if len(matches)!=1:result['state']='no_unique_exact_issuer_title_no_PDF_guess';return result
 locator=matches[0];result['locator']=locator;pdf=directory/'issuer-notice.pdf';result['primary_PDF_GET_attempts']=1;pr=request(locator['url'],None,s['PDF_max_bytes'],pdf)
 if pr.get('status')!=200 or not pr.get('within_cap') or not pdf.read_bytes().startswith(b'%PDF-'):result['state']='actual_primary_PDF_response_failed_preserved';return result
 reader=PdfReader(pdf,strict=False)
 if len(reader.pages)>s['max_pages']:result['state']='actual_page_limit_preserved';return result
 textpages=[];texts=[]
 # Retain short original notices native; never emit financial text or values.
 for i,page in enumerate(reader.pages):
  text=page.extract_text() or '';texts.append(text);p=directory/('page'+str(i+1)+'.txt');p.write_text(text,encoding='utf-8',newline='\n');textpages.append({'page':i+1,'path':str(p),'sha256':sha(p)})
 predicates=date_predicates(job,texts)
 result.update(state='primary_issuer_notice_captured_targeted_date_predicates',native_PDF=str(pdf),pdf_sha256=sha(pdf),pages=len(reader.pages),native_text_pages=textpages,predicates=predicates,dates_independently_bound=all(predicates.values()),cash_amount_or_entitlement_reconciled=False)
 return result
def run(s):
 if read(RESULT)['state']!='not_run':raise ValueError('first_alias_cash_source_batch_preserved')
 for p,h in s['input_hashes'].items():
  if sha(ROOT/p)!=h:raise ValueError('frozen_cash_source_input_changed')
 local=Path(s['native_directory'])
 if local.exists() or 'onedrive' in str(local).lower():raise ValueError('unsafe_cash_source_native_directory')
 local.mkdir(parents=True);start=time.monotonic()
 def retained_event(job):
  try:return event(job,local,s)
  except Exception as e:
   # Capture worker failure without discarding earlier response bytes.
   directory=local/job['id'];qreceipt=directory/'query.receipt.json';pdfreceipt=directory/'issuer-notice.receipt.json'
   return {'id':job['id'],'state':'actual_event_failure_preserved','failure_type':type(e).__name__,'failure_reason':str(e),'query_receipt':str(qreceipt) if qreceipt.exists() else None,'primary_PDF_GET_attempts':int(pdfreceipt.exists()),'dates_independently_bound':False,'native_cash_values_exported':False}
 with ThreadPoolExecutor(max_workers=2) as pool:events=list(pool.map(retained_event,s['events']))
 manifest=local/'cash_source_manifest.json';write(manifest,{'files':{str(p):sha(p) for p in local.rglob('*') if p.is_file()}})
 r={'state':'actual_two_primary_issuer_disclosure_query_batch','observed_utc':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':round(time.monotonic()-start,3),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),'POSTs':2,'PDF_GETs':sum(x['primary_PDF_GET_attempts'] for x in events),'events':events,'local_details':{'path':str(manifest),'sha256':sha(manifest)},'native_cash_values_exported':False,'cash_records_repaired':False,'book_replayed':False,'PIT_certified':False,'model_admitted':False,'full_goal_complete':False};write(RESULT,r);print(json.dumps({'state':r['state'],'POSTs':2,'PDF_GETs':r['PDF_GETs'],'events':[{'id':x['id'],'state':x['state'],'dates_independently_bound':x.get('dates_independently_bound',False)} for x in events]}))
def verify(s):
 r=read(RESULT)
 if r['state']!='actual_two_primary_issuer_disclosure_query_batch' or r['protocol_sha256']!=sha(PROTOCOL) or r['implementation_sha256']!=sha(__file__) or sha(r['local_details']['path'])!=r['local_details']['sha256']:raise ValueError('cash_source_evidence_changed')
 for p,h in s['input_hashes'].items():
  if sha(ROOT/p)!=h:raise ValueError('cash_source_input_changed')
 for p,h in read(r['local_details']['path'])['files'].items():
  if sha(p)!=h:raise ValueError('cash_source_native_file_changed')
 # Rebuild extracted native page bytes; no network retries or writes.
 for e in r['events']:
  if 'native_PDF' not in e:continue
  reader=PdfReader(e['native_PDF'],strict=False);texts=[]
  for pg in e['native_text_pages']:
   expected=(reader.pages[pg['page']-1].extract_text() or '').encode('utf-8')
   if Path(pg['path']).read_bytes()!=expected:raise ValueError('issuer_notice_text_not_rebuilt')
   texts.append(expected.decode('utf-8'))
  job=next(x for x in s['events'] if x['id']==e['id']);predicates=date_predicates(job,texts)
  if predicates!=e['predicates'] or all(predicates.values())!=e['dates_independently_bound']:raise ValueError('issuer_notice_date_predicates_not_rebuilt')
 if any(r[k] for k in ['native_cash_values_exported','cash_records_repaired','book_replayed','PIT_certified','model_admitted','full_goal_complete']):raise ValueError('cash_source_scope_changed')
 print(json.dumps({'two_primary_queries_and_retained_source_bytes_verified':True,'dates_passed':sum(e.get('dates_independently_bound',False) for e in r['events']),'GET_or_POST_retries':0,'PIT_or_economic_admission':False}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();m=ap.add_mutually_exclusive_group(required=True);m.add_argument('--run',action='store_true');m.add_argument('--verify',action='store_true');a=ap.parse_args();run(read(PROTOCOL)) if a.run else verify(read(PROTOCOL))
