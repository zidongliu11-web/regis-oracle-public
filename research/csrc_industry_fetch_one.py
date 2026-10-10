"""One public read; parent enforces an independent30-second wall deadline."""
import json,hashlib,urllib.request,urllib.parse,sys,time
from pathlib import Path
from datetime import datetime,timezone
def utc():return datetime.now(timezone.utc).isoformat()
url,destination,receipt,cap=sys.argv[1:];dest=Path(destination);log=Path(receipt);cap=int(cap)
def write(v):log.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
class GovernmentRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,req,fp,code,msg,headers,newurl):
  if urllib.parse.urlsplit(newurl).hostname not in ['www.csrc.gov.cn','csrc.gov.cn']:raise ValueError('external redirect refused')
  return super().redirect_request(req,fp,code,msg,headers,newurl)
assert urllib.parse.urlsplit(url).hostname in ['www.csrc.gov.cn','csrc.gov.cn'] and not dest.exists() and not log.exists()
r={'url':url,'started_utc':utc(),'state':'request_started','max_bytes':cap,'parent_wall_deadline_seconds':30};write(r)
try:
 opener=urllib.request.build_opener(GovernmentRedirect());req=urllib.request.Request(urllib.parse.quote(url,safe='/:?=&%'),headers={'User-Agent':'Mozilla/5.0'})
 with opener.open(req,timeout=10) as f:
  r.update(status=f.status,final_url=f.url,content_type=f.headers.get('Content-Type'),content_length=f.headers.get('Content-Length'),last_modified=f.headers.get('Last-Modified'),state='reading');write(r);size=0;deadline=time.monotonic()+20
  with dest.open('xb') as output:
   while True:
    if time.monotonic()>deadline:raise TimeoutError('native read deadline')
    chunk=f.read1(min(65536,cap+1-size))
    if not chunk:break
    output.write(chunk);size+=len(chunk)
    if size>cap:raise ValueError('source byte cap exceeded')
  if r['content_length'] is not None and size!=int(r['content_length']):raise ValueError('source Content-Length mismatch')
 r.update(state='captured',bytes=dest.stat().st_size,sha256=hashlib.sha256(dest.read_bytes()).hexdigest())
except Exception as e:r.update(state='failed',error_type=type(e).__name__,error=str(e)[:150])
if dest.exists():r.update(bytes=dest.stat().st_size,sha256=hashlib.sha256(dest.read_bytes()).hexdigest())
r['ended_utc']=utc();write(r);print(json.dumps({k:r.get(k) for k in ['state','status','bytes','error_type']},ensure_ascii=False))
