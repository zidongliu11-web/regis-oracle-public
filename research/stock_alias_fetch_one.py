"""One bounded public issuer/exchange GET, no authentication or retry."""
import hashlib,json,sys,time,urllib.request,urllib.parse
from pathlib import Path
from datetime import datetime,timezone
HOSTS={'static.cninfo.com.cn','disc.static.szse.cn','static.sse.com.cn'}
def utc():return datetime.now(timezone.utc).isoformat()
url,destination,receipt,cap=sys.argv[1:];dest=Path(destination);log=Path(receipt);cap=int(cap)
def write(v):log.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
class PublicRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,req,fp,code,msg,headers,newurl):
  if urllib.parse.urlsplit(newurl).hostname not in HOSTS:raise ValueError('external_redirect_refused')
  return super().redirect_request(req,fp,code,msg,headers,newurl)
assert urllib.parse.urlsplit(url).hostname in HOSTS and not dest.exists() and not log.exists()
r={'url':url,'started_utc':utc(),'state':'request_started','max_bytes':cap,'parent_wall_deadline_seconds':30};write(r)
try:
 opener=urllib.request.build_opener(PublicRedirect());req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'})
 with opener.open(req,timeout=10) as f:
  r.update(status=f.status,final_url=f.url,content_type=f.headers.get('Content-Type'),content_length=f.headers.get('Content-Length'),last_modified=f.headers.get('Last-Modified'),state='reading');write(r);size=0;deadline=time.monotonic()+20
  with dest.open('xb') as out:
   while True:
    if time.monotonic()>deadline:raise TimeoutError('native_read_deadline')
    chunk=f.read1(min(65536,cap+1-size))
    if not chunk:break
    out.write(chunk);size+=len(chunk)
    if size>cap:raise ValueError('source_byte_cap')
  if r['content_length'] is not None and size!=int(r['content_length']):raise ValueError('content_length_mismatch')
 r.update(state='captured',bytes=dest.stat().st_size,sha256=hashlib.sha256(dest.read_bytes()).hexdigest())
except Exception as e:r.update(state='failed',error_type=type(e).__name__)
if dest.exists():r.update(bytes=dest.stat().st_size,sha256=hashlib.sha256(dest.read_bytes()).hexdigest())
r['ended_utc']=utc();write(r)
print(json.dumps({k:r.get(k) for k in ['state','status','bytes','error_type']}))
