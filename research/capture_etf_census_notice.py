"""One predeclared public disclosure GET; no quote or account API."""
import datetime, hashlib, json, os, urllib.request, urllib.error
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/ETF_ACTION_CENSUS_PROTOCOL_20261002.json'
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None

def capture():
    p=json.loads(PROTOCOL.read_text(encoding='utf-8'))
    assert p['additional_notice']['url']=='https://www.sse.com.cn/disclosure/fund/announcement/c/new/2023-01-09/510300_20230109_0ED5.pdf'
    base=(Path(os.environ['LOCALAPPDATA'])/'RegisOracle-local-research/etf-component-20261002').resolve()
    dest=base/'2023-notice.pdf';assert not dest.exists()
    r={'schema':'regis.etf.census.notice_receipt.v1','protocol_sha256':hashlib.sha256(PROTOCOL.read_bytes()).hexdigest(),
       'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'url':p['additional_notice']['url'],
       'GET_attempts':1,'retries':0,'max_bytes':2097152,'body_path':dest.as_posix(),'new_quote_API_requests':0}
    body=b''
    try:
        with urllib.request.build_opener(NoRedirect).open(urllib.request.Request(r['url'],headers={'User-Agent':'Mozilla/5.0'}),timeout=15) as response:
            r.update(http_status=response.status,content_type=response.headers.get('Content-Type'),last_modified=response.headers.get('Last-Modified'))
            body=response.read(2097153);r['complete_within_cap']=len(body)<=2097152
            if not r['complete_within_cap']:body=body[:2097152]
    except urllib.error.HTTPError as e:r.update(http_status=e.code,error_type='HTTPError',location=e.headers.get('Location'))
    except Exception as e:r.update(http_status=None,error_type=type(e).__name__,error=str(e))
    if body:dest.write_bytes(body)
    r.update(bytes=len(body),sha256=hashlib.sha256(body).hexdigest(),ended_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
    (ROOT/'research/ETF_ACTION_CENSUS_NOTICE_RECEIPT_20261002.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(r,ensure_ascii=False))
if __name__=='__main__':capture()
