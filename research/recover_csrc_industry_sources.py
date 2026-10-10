"""Recover a finite government industry source packet, never quote feeds."""
import argparse,json,hashlib,re,time,subprocess,sys,os
from pathlib import Path
from datetime import datetime,timezone
from urllib.parse import urljoin,urlsplit
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor
from pypdf import PdfReader
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/CSRC_INDUSTRY_SOURCE_PROTOCOL_20261003.json'
RESULT=ROOT/'research/CSRC_INDUSTRY_SOURCE_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def utc():return datetime.now(timezone.utc).isoformat()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
class Links(HTMLParser):
 def __init__(self):super().__init__();self.a=None;self.links=[];self.text=[]
 def handle_starttag(self,t,a):
  if t=='a':self.a=[dict(a).get('href',''),'']
 def handle_data(self,d):
  self.text.append(d)
  if self.a:self.a[1]+=d
 def handle_endtag(self,t):
  if t=='a' and self.a:self.links.append(self.a);self.a=None
def check(spec):
 for p,h in spec['input_hashes'].items():
  if sha(ROOT/p)!=h:raise ValueError('frozen CSRC source input changed')
def parse_index(html,url,source_sha,quarters):
 out=[]
 for block in re.findall(r'<li\b[^>]*>([\s\S]*?)</li>',html,re.I):
  parser=Links();parser.feed(block);text=' '.join(parser.text);m=re.search(r'(20\d{2})\s*年\s*([1-4])\s*季度上市公司行业分类结果',text)
  if not m:continue
  year,quarter=map(int,m.groups());key=f'{year}Q{quarter}'
  if key not in quarters:continue
  dates=re.findall(r'\d{4}-\d{2}-\d{2}',text)
  if len(dates)!=1 or len(parser.links)!=1:raise ValueError('quarter index identity/date ambiguous')
  out.append({'quarter':key,'title':m.group(0),'index_url':url,'index_sha256':source_sha,'index_declared_publication_date':dates[0],'article_url':urljoin(url,parser.links[0][0])})
 return out
def metadata(html,url,index_date):
 parser=Links();parser.feed(html);text=' '.join(parser.text);dates=re.findall(r'日期\s*[:：]\s*(\d{4}-\d{2}-\d{2})',text)
 links=list(dict.fromkeys(urljoin(url,u) for u,t in parser.links if re.search(r'\.(pdf|doc|docx|xls|xlsx)(?:\?|$)',u,re.I)))
 if len(dates)!=1 or len(links)!=1:raise ValueError('article date or attachment ambiguous')
 # These are website declared dates, never proof of original byte vintage.
 from datetime import date
 date.fromisoformat(index_date);date.fromisoformat(dates[0])
 return {'article_declared_publication_date':dates[0],'declared_publication_date':max(index_date,dates[0]),'publication_date_conflict':dates[0]!=index_date,'attachment_url':links[0]}
def run(spec):
 check(spec)
 if read(RESULT)['state']!='not_run':raise ValueError('preserve finite source packet, no rerun')
 local=Path(spec['native_directory']);assert 'onedrive' not in str(local).lower() and not local.exists();local.mkdir(parents=True);started=utc();clock=time.monotonic();fetches=[]
 def get(url,name,cap=4000000):
  if urlsplit(url).hostname not in spec['allowed_hosts']:raise ValueError('unconfirmed source host')
  dest=local/name;receipt=local/(name+'.receipt.json');command=[sys.executable,'-X','utf8',str(Path(__file__).parent/'csrc_industry_fetch_one.py'),url,str(dest),str(receipt),str(cap)]
  try:
   p=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
   r=read(receipt) if receipt.exists() else {'url':url,'state':'failed','error_type':'NoReceipt','child_returncode':p.returncode}
  except subprocess.TimeoutExpired:
   r=read(receipt) if receipt.exists() else {'url':url};r.update(state='failed',error_type='ParentWallDeadline',ended_utc=utc())
   if dest.exists():r.update(bytes=dest.stat().st_size,sha256=sha(dest))
   write(receipt,r)
  r['native_path']=str(dest);r['native_receipt']=str(receipt);fetches.append(r);return r
 support=[]
 for i,url in enumerate(spec['support_urls']):support.append(get(url,'support-'+str(i)+'.html',1000000))
 index=[]
 for i,url in enumerate(spec['index_urls']):
  r=get(url,'index-'+str(i)+'.html',1000000)
  if r['state']!='captured':raise ValueError('government source index request failed')
  html=Path(r['native_path']).read_text(encoding='utf-8',errors='replace')
  index.extend(parse_index(html,url,r['sha256'],spec['quarters']))
 if sorted(e['quarter'] for e in index)!=sorted(spec['quarters']):raise ValueError('expected32quarter index coverage mismatch')
 write(local/'quarter_index.json',index)
 def acquire(e):
  if time.monotonic()-clock>spec['total_seconds']:return dict(e,state='not_requested_global_deadline')
  article=get(e['article_url'],e['quarter']+'.html',1000000);out=dict(e,article=article,state='article_failed')
  if article['state']!='captured':return out
  try:meta=metadata(Path(article['native_path']).read_text(encoding='utf-8',errors='replace'),e['article_url'],e['index_declared_publication_date'])
  except ValueError:return dict(out,state='article_date_or_attachment_ambiguous')
  out.update(meta,historical_byte_vintage_certified=False,publication_hour_unknown=True,earliest_model_use='source trading session strictly after latest verified declared publication date; other conflicting primary dates require reconciliation')
  url=meta['attachment_url'];suffix=Path(urlsplit(url).path).suffix.lower();body=get(url,e['quarter']+suffix);out.update(attachment=body,state='attachment_failed')
  if body['state']!='captured':return out
  if suffix=='.pdf':
   try:
    raw=Path(body['native_path']).read_bytes()
    if not raw.startswith(b'%PDF-') or b'%%EOF' not in raw[-4096:]:raise ValueError('PDF completeness markers absent')
    pdf=PdfReader(body['native_path'],strict=True);out.update(state='PDF_integrity_captured',pages=len(pdf.pages),PDF_metadata={k:str(v)[:150] for k,v in (pdf.metadata or {}).items() if k in ['/CreationDate','/ModDate','/Producer']})
   except Exception as error:out.update(state='PDF_integrity_failed',error_type=type(error).__name__)
  else:out.update(state='non_PDF_original_captured_semantics_pending')
  return out
 results=[]
 with ThreadPoolExecutor(max_workers=2) as pool:
  for i,value in enumerate(pool.map(acquire,index),1):
   results.append(value)
   if i%4==0 or i==1:print(json.dumps({'quarters_processed':i,'sources_integrity_or_capture_success':sum(v['state'] in ['PDF_integrity_captured','non_PDF_original_captured_semantics_pending'] for v in results),'semantic_membership_admitted':False}),flush=True)
 check(spec);assert len(fetches)<=spec['max_GETs'];details=local/'source_packet.json';write(details,{'quarter_sources':results,'fetches':fetches,'supporting_sources':support})
 value={'schema':'regis.csrc.industry_sources.v1','state':'finite_source_packet_recovery_attempted','started_utc':started,'ended_utc':utc(),'elapsed_seconds':round(time.monotonic()-clock,3),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),'fetch_helper_sha256':sha(Path(__file__).parent/'csrc_industry_fetch_one.py'),'quarter_targets':len(index),'actual_GET_attempts':len(fetches),'captured_GETs':sum(r['state']=='captured' for r in fetches),'HTTP_failures':sum(r['state']!='captured' for r in fetches),'quarter_states':[{k:v.get(k) for k in ['quarter','state','index_declared_publication_date','article_declared_publication_date','declared_publication_date','publication_date_conflict','pages','article_url']} for v in results],'original_source_bytes':sum(r.get('bytes',0) for r in fetches),'local_details':{'path':str(details),'sha256':sha(details),'native_directory':str(local),'contains_quote_arrays':False},'PIT_historical_byte_vintage_certified':False,'membership_rows_parsed':0,'economic_runs':0,'model_admitted':False,'full_goal_complete':False,'rights_scope':spec['rights_scope'],'missing_inputs_remaining':spec['limits']}
 write(RESULT,value);print(json.dumps({k:value[k] for k in ['state','elapsed_seconds','quarter_targets','actual_GET_attempts','captured_GETs','HTTP_failures','membership_rows_parsed','model_admitted']},ensure_ascii=False))
def verify(spec):
 check(spec);r=read(RESULT)
 if r['state']!='finite_source_packet_recovery_attempted' or r['protocol_sha256']!=sha(PROTOCOL) or r['implementation_sha256']!=sha(__file__) or r['fetch_helper_sha256']!=sha(Path(__file__).parent/'csrc_industry_fetch_one.py') or r['local_details']['sha256']!=sha(r['local_details']['path']):raise ValueError('source packet evidence changed')
 details=read(r['local_details']['path'])
 for request in details['fetches']:
  if request.get('sha256') and sha(request['native_path'])!=request['sha256']:raise ValueError('original government source bytes changed')
 if r['economic_runs'] or r['membership_rows_parsed'] or r['PIT_historical_byte_vintage_certified'] or r['model_admitted'] or r['full_goal_complete']:raise ValueError('source only scope changed')
 print(json.dumps({'industry_source_packet_verified':True,'quarter_targets':r['quarter_targets'],'model_admitted':False}))
def preserve_fatal_attempt(spec,error):
 local=Path(spec['native_directory'])
 if not local.is_dir():raise error
 fetches=[]
 for p in sorted(local.glob('*.receipt.json')):
  r=read(p);r.update(native_receipt=str(p),native_path=str(p)[:-len('.receipt.json')]);fetches.append(r)
 details=local/'source_packet_failure.json'
 if details.exists():raise error
 write(details,{'quarter_sources':[],'fetches':fetches,'fatal_error_type':type(error).__name__,'error':str(error)[:180]})
 value={'schema':'regis.csrc.industry_sources.v1','state':'finite_source_packet_recovery_attempted','ended_utc':utc(),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),'fetch_helper_sha256':sha(Path(__file__).parent/'csrc_industry_fetch_one.py'),'quarter_targets':len(spec['quarters']),'actual_GET_attempts':len(fetches),'captured_GETs':sum(r['state']=='captured' for r in fetches),'HTTP_failures':sum(r['state']!='captured' for r in fetches),'fatal_error_type':type(error).__name__,'error':str(error)[:180],'quarter_states':[],'local_details':{'path':str(details),'sha256':sha(details),'native_directory':str(local),'contains_quote_arrays':False},'PIT_historical_byte_vintage_certified':False,'membership_rows_parsed':0,'economic_runs':0,'model_admitted':False,'full_goal_complete':False,'rights_scope':spec['rights_scope'],'missing_inputs_remaining':spec['limits']}
 write(RESULT,value);print(json.dumps({'fatal_attempt_preserved':True,'actual_GET_attempts':len(fetches),'error_type':type(error).__name__}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--run',action='store_true');ap.add_argument('--verify',action='store_true');args=ap.parse_args();spec=read(PROTOCOL)
 if args.run==args.verify:raise SystemExit('Select one mode')
 if args.verify:verify(spec)
 else:
  try:run(spec)
  except Exception as error:
   preserve_fatal_attempt(spec,error);raise SystemExit(2)
