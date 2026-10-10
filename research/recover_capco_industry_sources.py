"""Two producer-site pages and observed attachments; native-only source rows."""
import argparse,hashlib,json,time,urllib.request,urllib.parse,urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from html.parser import HTMLParser
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/CAPCO_INDUSTRY_SOURCE_PROTOCOL_20261003.json';RESULT=ROOT/'research/CAPCO_INDUSTRY_SOURCE_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
class Anchors(HTMLParser):
 def __init__(self):super().__init__();self.links=[];self.current=None
 def handle_starttag(self,tag,attrs):
  if tag.lower()=='a':self.current={'href':dict(attrs).get('href',''),'text':''}
 def handle_data(self,text):
  if self.current is not None:self.current['text']+=text
 def handle_endtag(self,tag):
  if tag.lower()=='a' and self.current is not None:self.links.append(self.current);self.current=None
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None
def request(url,path,cap):
 start=datetime.now(timezone.utc).isoformat();receipt={'url':url,'attempts':1,'retries':0,'max_bytes':cap,'started_utc':start};data=b''
 try:
  req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.capco.org.cn/'})
  with urllib.request.build_opener(NoRedirect).open(req,timeout=15) as r:
   data=r.read(cap+1);receipt.update(status=r.status,content_type=r.headers.get('Content-Type'),within_cap=len(data)<=cap,last_modified=r.headers.get('Last-Modified'))
   if len(data)>cap:data=data[:cap]
 except urllib.error.HTTPError as e:receipt.update(status=e.code,error_type='HTTPError',redirect=e.headers.get('Location'))
 except Exception as e:receipt.update(error_type=type(e).__name__,error_reason=str(e))
 if data:path.write_bytes(data)
 receipt.update(bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),body_path=str(path),ended_utc=datetime.now(timezone.utc).isoformat());write(path.with_suffix('.receipt.json'),receipt);return receipt
def capture(job,s,local):
 d=local/job['quarter'];d.mkdir();html=d/'producer-page.html';hr=request(job['page_url'],html,s['html_cap']);result={'quarter':job['quarter'],'page_GETs':1,'attachment_GETs':0,'historical_publication_metadata':job['publication_date'],'first_source_rows_activated':False}
 if hr.get('status')!=200 or not hr.get('within_cap'):result['state']='actual_producer_HTML_failed_preserved';return result
 text=html.read_bytes().decode('utf-8',errors='strict');parser=Anchors();parser.feed(text);matches=[]
 for a in parser.links:
  if ''.join(a['text'].split())!=job['attachment_title']:continue
  url=urllib.parse.urljoin(job['page_url'],a['href']);parts=urllib.parse.urlsplit(url)
  if parts.scheme!='https' or parts.netloc!='www.capco.org.cn' or not parts.path.lower().endswith(job['suffix']) or '..' in parts.path:raise ValueError('unsafe_observed_producer_attachment')
  matches.append(url)
 if len(set(matches))!=1:result.update(state='no_unique_actual_attachment_no_guess',exact_title_matches=len(set(matches)));return result
 url=matches[0];path=d/('producer-original'+job['suffix']);result.update(attachment_GETs=1,observed_attachment_url=url);br=request(url,path,s['attachment_cap'])
 if br.get('status')!=200 or not br.get('within_cap'):result['state']='actual_attachment_failed_preserved';return result
 data=path.read_bytes();pdf=data.startswith(b'%PDF-');ole=data.startswith(bytes.fromhex('d0cf11e0a1b11ae1'));zipfile=data.startswith(b'PK\x03\x04')
 result.update(state='actual_independent_producer_original_retained',native_original=str(path),native_original_sha256=sha(path),bytes=len(data),actual_format='PDF' if pdf else 'OLE' if ole else 'ZIP' if zipfile else 'unknown',exact_bytes_same_as_original_regulator=sha(path)==job['regulator_sha256'],original_regulator_sha256=job['regulator_sha256'],old_parse_quarantines_preserved=True,full_PIT_certified=False)
 if pdf:
  from pypdf import PdfReader
  p=PdfReader(path,strict=False);result['pages']=len(p.pages)
  if len(p.pages)>s['pdf_page_cap']:raise ValueError('producer_pdf_page_cap')
  page=p.pages[job['conflict_page']-1];target=(page.extract_text() or '');detail=d/'specific_conflict_page.txt';detail.write_text(target,encoding='utf-8',newline='\n');result.update(specific_conflict_page=job['conflict_page'],conflict_page_native_text=str(detail),conflict_page_text_sha256=sha(detail),targeted_code_603026_occurrences=target.count('603026'),targeted_code_603626_occurrences=target.count('603626'),targeted_company_present='科森科技' in ''.join(target.split()),code_or_class_corrected=False)
 return result
def run(s):
 if read(RESULT)['state']!='not_run':raise ValueError('first_producer_source_attempt_preserved')
 for p,h in s['input_hashes'].items():
  if sha(ROOT/p)!=h:raise ValueError('producer_frozen_input_changed')
 local=Path(s['native_directory']);assert not local.exists() and 'onedrive' not in str(local).lower();local.mkdir(parents=True);start=time.monotonic()
 def kept(job):
  try:return capture(job,s,local)
  except Exception as e:
   return {'quarter':job['quarter'],'state':'actual_producer_source_worker_failure_preserved','error_type':type(e).__name__,'error_reason':str(e),'attachment_GETs':int((local/job['quarter']/('producer-original'+job['suffix'])).with_suffix('.receipt.json').exists()),'page_GETs':1,'first_source_rows_activated':False}
 with ThreadPoolExecutor(max_workers=2) as pool:events=list(pool.map(kept,s['jobs']))
 manifest=local/'producer_source_manifest.json';write(manifest,{'files':{str(p):sha(p) for p in local.rglob('*') if p.is_file()}})
 r={'state':'actual_two_independent_producer_source_attempts','observed_utc':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':round(time.monotonic()-start,3),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),'page_GETs':2,'attachment_GETs':sum(x['attachment_GETs'] for x in events),'events':events,'local_details':{'path':str(manifest),'sha256':sha(manifest)},'member_classes_activated':0,'original2012unknown2017quarantines_preserved':True,'quote_arrays_read':False,'economic_runs':0,'model_admitted':False,'full_goal_complete':False};write(RESULT,r);print(json.dumps({'state':r['state'],'page_GETs':2,'attachment_GETs':r['attachment_GETs'],'events':events},ensure_ascii=False))
def verify(s):
 r=read(RESULT)
 if r['state']!='actual_two_independent_producer_source_attempts' or r['protocol_sha256']!=sha(PROTOCOL) or r['implementation_sha256']!=sha(__file__) or sha(r['local_details']['path'])!=r['local_details']['sha256']:raise ValueError('producer_source_evidence_changed')
 for p,h in s['input_hashes'].items():
  if sha(ROOT/p)!=h:raise ValueError('producer_frozen_input_changed')
 for p,h in read(r['local_details']['path'])['files'].items():
  if sha(p)!=h:raise ValueError('producer_native_source_changed')
 for e in r['events']:
  if 'native_original' not in e:continue
  job=next(x for x in s['jobs'] if x['quarter']==e['quarter']);p=Path(e['native_original']);body=p.read_bytes()
  if sha(p)!=e['native_original_sha256'] or (sha(p)==job['regulator_sha256'])!=e['exact_bytes_same_as_original_regulator']:raise ValueError('producer_byte_relationship_not_rebuilt')
  if e['actual_format']=='PDF':
   from pypdf import PdfReader
   pages=PdfReader(p,strict=False).pages;text=pages[job['conflict_page']-1].extract_text() or ''
   if len(pages)!=e['pages'] or text.encode()!=Path(e['conflict_page_native_text']).read_bytes() or text.count('603026')!=e['targeted_code_603026_occurrences'] or text.count('603626')!=e['targeted_code_603626_occurrences'] or ('科森科技' in ''.join(text.split()))!=e['targeted_company_present']:raise ValueError('producer_specific_conflict_not_rebuilt')
 if r['member_classes_activated'] or r['quote_arrays_read'] or r['economic_runs'] or r['model_admitted'] or r['full_goal_complete']:raise ValueError('producer_scope_changed')
 print(json.dumps({'two_producer_capture_and_byte_relations_verified':True,'new_source_rows_activated':0,'network_retries':0,'model_admitted':False}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();m=ap.add_mutually_exclusive_group(required=True);m.add_argument('--run',action='store_true');m.add_argument('--verify',action='store_true');a=ap.parse_args();run(read(PROTOCOL)) if a.run else verify(read(PROTOCOL))
