"""Bounded direct access check of two newly located official catalogue pages.

Published pages are captured only in Native storage. No redirects, scripts,
login, cookies, proxies, quotes, list extraction or data admission.
"""
from pathlib import Path
import argparse,http.client,json,subprocess,sys
ROOT=Path(__file__).resolve().parents[1];sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from research.capture_completed_daily_pair import read,write,sha,clock,claim_child_dispatch
P=ROOT/'research/OFFICIAL_CATALOGUE_ACCESS_PROTOCOL_20261009.json'
O=ROOT/'research/OFFICIAL_CATALOGUE_ACCESS_RESULT_20261009.json'
TARGETS=[('SSE_stock_list','www.sse.com.cn','/assortment/stock/list/'),('BSE_code_mapping','www.bse.cn','/service/code_mapping.html')]
CAP=2*1024*1024

def gate(p):
    from scripts.workflow_guard import contract_hash
    t=read(ROOT/'WORKFLOW_TASK.json');a={v['path']:v['sha256'] for v in t['artifacts']}
    assert t['id']==p['parent_task'] and t['owner_session']==p['owner_session'] and t['status']=='in_progress'
    assert t['contract_sha256']==contract_hash(t) and a[P.relative_to(ROOT).as_posix()]==sha(P)
    assert p['targets']==[list(q) for q in TARGETS]
    assert p['document_requests_max']==2 and p['socket_seconds']==20 and p['child_seconds']==90
    assert p['retries']==p['redirects_followed']==p['scripts_executed']==p['SDK_calls']==0
    for f,h in p['input_hashes'].items():assert sha(ROOT/f)==h
    assert 'onedrive' not in str(Path(p['native_directory']).resolve()).lower()
    return t

def child(p):
    gate(p);claim_child_dispatch(p)
    def audit(event,args):
        if event in ('subprocess.Popen','os.system'):raise PermissionError('unexpected_spawn')
    sys.addaudithook(audit)
    out=Path(p['native_directory'])/'capture';journal=out/'journal.json'
    j={'started_utc':clock(),'document_request_attempts':0,'automatic_retries':0,'redirects_followed':0,'records':[],'failure_type':None}
    write(journal,j)
    for role,host,path in TARGETS:
        record={'role':role,'url':'https://'+host+path,'attempt_number_for_exact_role':1,'started_utc':clock(),'ended_utc':None,'HTTP_status':None,'content_type':None,'body_bytes':None,'body_ref':None,'failure_type':None,'complete_bounded_HTTP_200_body':False}
        j['records'].append(record);j['document_request_attempts']+=1;write(journal,j)
        conn=http.client.HTTPSConnection(host,timeout=20)
        try:
            conn.request('GET',path,headers={'User-Agent':'RegisOracle-personal-research/1.0','Accept':'text/html','Accept-Encoding':'identity','Connection':'close'})
            response=conn.getresponse();record.update(HTTP_status=response.status,content_type=response.getheader('Content-Type',''))
            data=response.read(CAP+1);truncated=len(data)>CAP
            target=out/(role+'.body');target.write_bytes(data[:CAP])
            record.update(body_bytes=len(data),body_ref={'path':str(target),'bytes':min(len(data),CAP),'sha256':sha(target),'truncated_for_capture':truncated},complete_bounded_HTTP_200_body=response.status==200 and not truncated)
            if truncated:record['failure_type']='BodyCaptureCapExceeded'
            elif response.status!=200:record['failure_type']='HTTPStatusNot200'
        except Exception as exc:record['failure_type']=type(exc).__name__
        finally:
            conn.close();record['ended_utc']=clock();write(journal,j)
    j.update(ended_utc=clock(),completed_HTTP_200_bodies=sum(r['complete_bounded_HTTP_200_body'] for r in j['records']))
    write(journal,j);return 0 if j['completed_HTTP_200_bodies']==2 else 1

def run(p):
    t=gate(p);protocol_hash,implementation_hash=sha(P),sha(__file__)
    out=Path(p['native_directory'])/'capture';out.mkdir(exist_ok=False)
    write(out/'protocol.json',p,exclusive=True);write(out/'frozen_task.json',t,exclusive=True)
    r={'schema':'regis.official_catalogue_access_result.v1','started_utc':clock(),'child_returncode':None,'child_timed_out':False,'failure_type':None}
    try:
        q=subprocess.run([sys.executable,'-I','-X','utf8',str(Path(__file__).resolve()),'--child'],cwd=out,capture_output=True,timeout=90,creationflags=0x08000000 if sys.platform=='win32' else 0)
        r['child_returncode']=q.returncode;(out/'stdout.log').write_bytes(q.stdout);(out/'stderr.log').write_bytes(q.stderr)
    except subprocess.TimeoutExpired as exc:
        r.update(child_timed_out=True,failure_type='TimeoutExpired');(out/'stdout.log').write_bytes(exc.stdout or b'');(out/'stderr.log').write_bytes(exc.stderr or b'')
    except Exception as exc:r['failure_type']=type(exc).__name__
    j=read(out/'journal.json') if (out/'journal.json').exists() else {}
    try:gate(p)
    except Exception as exc:r['post_capture_binding_failed']=True;r['failure_type']=r['failure_type'] or type(exc).__name__
    complete=r['child_returncode']==0 and not r['child_timed_out'] and not r['failure_type'] and j.get('completed_HTTP_200_bodies')==2
    r.update(state='two_official_page_bodies_retained_not_datasets_or_rights' if complete else 'official_page_access_partial_or_failure_preserved_no_retry',ended_utc=clock(),
        protocol_sha256=protocol_hash,implementation_sha256=implementation_hash,contract_sha256=t['contract_sha256'],diagnostics=j,
        public_document_capture_batches=1,SDK_calls=0,market_quote_queries=0,source_dataset_extractions=0,derived_market_values=0,
        document_bodies_to_AI_or_Git=False,body_access_is_not_semantic_source_validity=True,redirects_followed=0,scripts_executed=0,
        rights_verified=False,current_full_A_verified=False,historical_PIT_verified=False,permanent_identity_verified=False,
        market_gate_ready=False,model_admitted=False,orders_enabled=False,new_formulas=0,economic_runs=0,full_goal_complete=False)
    write(O,r);write(out/'terminal.json',r,exclusive=True);print(json.dumps(r));return 0 if complete else 1

if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);group=ap.add_mutually_exclusive_group();group.add_argument('--validate-plan',action='store_true');group.add_argument('--child',action='store_true');args=ap.parse_args();p=read(P)
    if args.validate_plan:gate(p);print(json.dumps({'frozen_scope_checked':True,'document_requests':0,'market_quote_queries':0}))
    else:raise SystemExit(child(p) if args.child else run(p))
