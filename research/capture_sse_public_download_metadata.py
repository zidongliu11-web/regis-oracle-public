import hashlib,json,os,subprocess,sys,time
from datetime import datetime,timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin,urlparse
R=Path.cwd();P=R/'research/SSE_PUBLIC_DOWNLOAD_LOCATOR_PROTOCOL_20261002.json';OUT=R/'research/SSE_PUBLIC_DOWNLOAD_LOCATOR_RECEIPT_20261002.json'
def now():return datetime.now(timezone.utc).isoformat()
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(v):OUT.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
protocol=json.loads(P.read_text(encoding='utf-8'))
if len(sys.argv)==1:
 assert not OUT.exists(),'one-shot receipt already exists'
 try:
  run=subprocess.run([sys.executable,'-X','utf8',__file__,'worker'],cwd=R,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=protocol['process_wall_limit_seconds'],creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
 except subprocess.TimeoutExpired:
  v=json.loads(OUT.read_text(encoding='utf-8')) if OUT.exists() else {'schema':'regis.sse.public_download_locator_receipt.v1','protocol_sha256':sha(P),'requests':[]}
  v.update({'ended_utc':now(),'process_timeout':True,'failure':'worker_wall_limit','source_admitted':False,'economic_run':False});save(v);print(json.dumps({'process_timeout':True,'requests_initiated':len(v['requests'])}));raise SystemExit(1)
 print(run.stdout.decode('utf-8',errors='replace').strip());raise SystemExit(run.returncode)
import requests
class Markup(HTMLParser):
 def __init__(self):super().__init__();self.scripts=[];self.downloads=[];self.parts=[];self.skip=0
 def handle_starttag(self,tag,attrs):
  a=dict(attrs)
  if tag in ('script','style'):self.skip+=1
  if tag=='script' and a.get('src'):self.scripts.append(a['src'])
  if tag=='a' and a.get('href') and ('download' in a or urlparse(a['href']).path.lower().endswith(('.csv','.xls','.xlsx','.zip','.txt'))):self.downloads.append(a['href'])
 def handle_endtag(self,tag):
  if tag in ('script','style'):self.skip=max(0,self.skip-1)
 def handle_data(self,v):
  if not self.skip:self.parts.append(v)
receipt={'schema':'regis.sse.public_download_locator_receipt.v1','protocol_sha256':sha(P),'started_utc':now(),'requests':[],'retries':0,'redirects':0,'market_quote_requests':0,'account_or_order_requests':0,'source_admitted':False,'economic_run':False,'model_admitted':False,'process_timeout':False,'raw_data_or_quote_values_to_AI':False}
save(receipt);deadline=time.monotonic()+protocol['worker_wall_limit_seconds']
def fetch(item):
 assert time.monotonic()<deadline,'worker_wall_limit'
 row={'role':item['role'],'url':item['url'],'started_utc':now(),'status':None,'failure':None};receipt['requests'].append(row);save(receipt)
 body=None
 try:
  with requests.get(item['url'],stream=True,timeout=(6,6),allow_redirects=False) as response:
   row['status']=response.status_code;row['headers']={k:response.headers.get(k) for k in ('Content-Type','Content-Length','Date','Last-Modified','ETag')}
   assert response.status_code==200,'HTTP_not_200'
   value=bytearray()
   for chunk in response.iter_content(8192):
    assert time.monotonic()<deadline,'worker_wall_limit';value.extend(chunk);assert len(value)<=protocol['byte_cap_per_object'],'byte_cap_exceeded'
   path=Path(protocol['raw_directory'])/(item['role']+'.response');path.write_bytes(value)
   row.update({'raw_path':str(path),'raw_bytes':len(value),'raw_sha256':sha(path),'complete_within_cap':True});body=bytes(value)
 except Exception as e:row['failure']=str(e) if isinstance(e,AssertionError) else type(e).__name__
 row['ended_utc']=now();save(receipt);return body,row
script_candidates=[]
for item in protocol['initial_objects']:
 body,row=fetch(item)
 if body is None:continue
 try:
  parser=Markup();parser.feed(body.decode('utf-8'))
  compact=''.join(''.join(parser.parts).split())
  if item['role']=='sse_chinese_legal':
   row['body_markers']={m:m in compact for m in ['法律声明','非商业目的浏览、下载','不得以向他人出售牟利为目的','本网站提供的所有资料']}
   row['terms_interpretation']='Chinese public noncommercial browsing/download only; no assumed undocumented API, paid historical product, AI/cloud/public redistribution right.'
  else:
   row['page_markers']={m:m in compact for m in ['基金档案','基金代码','开盘价','昨收价','成交量','成交额','开始时间','结束时间','下载']}
   row['script_urls_observed']=[urljoin(item['url'],s) for s in parser.scripts]
   row['explicit_file_download_urls_observed']=[urljoin(item['url'],s) for s in parser.downloads]
   for source in row['script_urls_observed']:
    parsed=urlparse(source);low=parsed.path.lower()
    if parsed.scheme=='https' and parsed.hostname in protocol['allowed_asset_hosts'] and ('funddetail' in low or '/fundlist/' in low) and low.endswith('.js'):
     if source not in script_candidates:script_candidates.append(source)
 except Exception as e:row['markup_failure']=type(e).__name__
 save(receipt)
for i,url in enumerate(script_candidates[:protocol['max_page_specific_scripts']]):
 body,row=fetch({'role':'observed_funddetail_script_'+str(i+1),'url':url})
 if body is None:continue
 text=body.decode('utf-8',errors='replace')
 row['locator_markers']={m:m in text for m in ['getQuery','fundCode','FUNDID','fundId','export','download','history','jsonp','ajax','getJSON']}
 row['script_inspection_only']=True
 # No endpoint text is called. Exact source bytes are held for local static inspection.
 save(receipt)
receipt['ended_utc']=now();receipt['conditional_scripts_candidates_found']=len(script_candidates)
receipt['public_download_dataset_obtained']=False;receipt['all_required_initial_bodies_captured']=all(r.get('status')==200 and not r.get('failure') for r in receipt['requests'][:len(protocol['initial_objects'])])
save(receipt)
print(json.dumps({'requests_initiated':len(receipt['requests']),'successful_bodies':sum(r.get('status')==200 and not r.get('failure') for r in receipt['requests']),'conditional_script_bodies':max(0,len(receipt['requests'])-len(protocol['initial_objects'])),'public_download_dataset_obtained':False,'source_admitted':False,'economic_run':False}))
