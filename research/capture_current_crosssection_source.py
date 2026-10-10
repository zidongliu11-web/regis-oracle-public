"""Bounded current provider stock cross-section; reuse the existing daily audit.

Three source roles, one fixed completed date, no economics or signal generation.
Metadata basic/list pagination is explicit and capped; SDK wire bytes are unknown.
Raw arrays stay Native, and no source/account/environment configuration is read.
"""
from pathlib import Path
import argparse,json,socket,subprocess,sys
ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from research.capture_completed_daily_pair import read,write,sha,clock,completed_cutoff,first_response,claim_child_dispatch
from research.baostock_bulk_daily_gate import audit_day,DAILY_FIELDS
P=ROOT/'research/CURRENT_CROSSSECTION_SOURCE_PROTOCOL_20261008.json'
O=ROOT/'research/CURRENT_CROSSSECTION_SOURCE_RESULT_20261008.json'
BASIC_FIELDS=['code','code_name','ipoDate','outDate','type','status']
LIST_FIELDS=['code','tradeStatus','code_name']

def gate(p):
    from scripts.workflow_guard import contract_hash
    t=read(ROOT/'WORKFLOW_TASK.json');idx={a['path']:a['sha256'] for a in t['artifacts']}
    assert t['id']==p['parent_task'] and t['status']=='in_progress' and t['owner_session']==p['owner_session'] and t['contract_sha256']==contract_hash(t)
    assert idx[P.relative_to(ROOT).as_posix()]==sha(P) and p['day']=='2026-10-08'
    for f,h in p['input_hashes'].items():assert sha(ROOT/f)==h
    for f,h in p['native_sdk_hashes'].items():assert sha(f)==h
    completed_cutoff(clock(),p['day']);return t

def child(p):
    gate(p);claim_child_dispatch(p);socket.setdefaulttimeout(8)
    def audit(e,a):
        if e=='socket.connect' and a[1]!=('public-api.baostock.com',10030):raise PermissionError('unexpected_target')
        if e in ('subprocess.Popen','os.system'):raise PermissionError('child_spawn')
    sys.addaudithook(audit);sys.path.insert(0,p['sdk_directory'])
    import baostock as bs
    import baostock.common.context as ctx
    import baostock.common.contants as constants
    assert constants.BAOSTOCK_PER_PAGE_COUNT==2000
    out=Path(p['native_directory'])/'capture';path=out/'journal.json'
    j={'started_utc':clock(),'handshake_attempts':0,'query_attempts':0,'page_next_attempts':0,'logout_attempts':0,'attempts':[],'refs':{},'failure_type':None,'stage':'before_handshake'}
    def query(label,fn,fields,paginated):
        j['query_attempts']+=1;a={'label':label,'sent_at_utc':clock(),'returned_at_utc':None,'pages':[]};j['attempts'].append(a);j['stage']=label;write(path,j)
        rs=fn();a['returned_at_utc']=clock();a['error_code']=str(rs.error_code);a['fields']=list(rs.fields);write(path,j)
        rows=[]
        for page in range(6 if paginated else 1):
            a['pages'].append({'index':page+1,'row_type':type(rs.data).__name__,'rows':len(rs.data) if isinstance(rs.data,list) else None,'error_code':str(rs.error_code),'received_at_utc':clock()});write(path,j)
            current=first_response(rs,fields,12000 if paginated else 10000)
            rows.extend([r[f] for f in fields] for r in current)
            if len(rows)>12000:raise ValueError('total_source_row_cap')
            # Use the pinned SDK constant 2000, not older comment text. SDK
            # treats a response smaller or larger than its page constant as
            # terminal, including non-paged bulk responses. Equal-to-2000
            # metadata responses require a next page; cursor is consumed here.
            if not paginated or len(current)!=constants.BAOSTOCK_PER_PAGE_COUNT:break
            if page==5:raise ValueError('metadata_page_cap_incomplete')
            j['page_next_attempts']+=1;write(path,j);rs.cur_row_num=len(rs.data)
            if not rs.next():
                if str(rs.error_code)!='0':raise ValueError('pagination_provider_error')
                # A false next can also mean a silent receive error. Certify
                # exhaustion only when the SDK actually installed an empty page.
                if len(rs.data)!=0:raise ValueError('pagination_exhaustion_unknown')
                break
        a['completed_at_utc']=clock();write(path,j);return rows
    try:
        j['handshake_attempts']=1;write(path,j);lg=bs.login();j['handshake_error_code']=str(lg.error_code);write(path,j)
        if lg.error_code!='0':raise ValueError('guest_handshake_failed')
        queries=[('stock_basic',lambda:bs.query_stock_basic(),BASIC_FIELDS,True),('all_stock',lambda:bs.query_all_stock(day=p['day']),LIST_FIELDS,True),('daily',lambda:bs.query_daily_history_k_AStock(date=p['day']),list(DAILY_FIELDS),False)]
        for label,fn,fields,paginated in queries:
            rows=query(label,fn,fields,paginated)
            payload={'schema':'bulk_astock_exploration.v1' if label=='daily' else 'bounded_baostock_universe_probe.v1','client_version':str(constants.BAOSTOCK_CLIENT_VERSION),'fields':fields,'rows':rows,'received_at_utc':j['attempts'][-1]['completed_at_utc']}
            if label=='daily':payload['day']=p['day']
            else:payload.update(query=label,query_day=p['day'] if label=='all_stock' else None)
            f=out/(label+'.json');write(f,payload,exclusive=True);j['refs'][label]={'path':f.name,'sha256':sha(f),'persisted_at_utc':clock()};write(path,j)
        j['stage']='existing_audit_day';j['audit']=audit_day(out,p['day'],j['refs']);write(path,j)
        daily=read(out/'daily.json');codes=[r[1] for r in daily['rows']]
        j['prefix_counts']={k:sum(c.startswith(k) for c in codes) for k in ['sh.600','sh.601','sh.603','sh.605','sh.688','sz.000','sz.001','sz.002','sz.003','sz.300','sz.301','sz.302','sh.900','sz.200','bj.']}
        j['stage']='predicates_complete'
    except Exception as e:
        j['failure_type']=type(e).__name__
        if isinstance(e,ValueError):j['validation_label']=str(e)[:180]
    finally:
        if j.get('handshake_error_code')=='0':
            j['logout_attempts']=1;write(path,j)
            try:j['logout_error_code']=str(bs.logout().error_code)
            except Exception as e:j['logout_failure_type']=type(e).__name__
        if hasattr(ctx,'default_socket'):
            try:ctx.default_socket.close()
            except Exception:pass
        j['ended_utc']=clock();write(path,j)
    return 0 if not j['failure_type'] else 1

def run(p):
    t=gate(p);out=Path(p['native_directory'])/'capture';assert 'onedrive' not in str(out).lower();out.mkdir(exist_ok=False)
    write(out/'protocol.json',p,exclusive=True);write(out/'frozen_task.json',t,exclusive=True)
    r={'schema':'regis.current_crosssection_source_result.v1','started_utc':clock(),'returncode':None,'timed_out':False,'failure_type':None}
    try:
        c=subprocess.run([sys.executable,'-I','-X','utf8',str(Path(__file__).resolve()),'--child'],cwd=out,capture_output=True,timeout=55,creationflags=0x08000000 if sys.platform=='win32' else 0)
        r['returncode']=c.returncode;(out/'stdout.log').write_bytes(c.stdout);(out/'stderr.log').write_bytes(c.stderr)
    except subprocess.TimeoutExpired as e:
        r.update(timed_out=True,failure_type='TimeoutExpired');(out/'stdout.log').write_bytes(e.stdout or b'');(out/'stderr.log').write_bytes(e.stderr or b'')
    except Exception as e:r['failure_type']=type(e).__name__
    gate(p);j=read(out/'journal.json') if (out/'journal.json').exists() else {};r['ended_utc']=clock()
    r.update(protocol_sha256=sha(P),implementation_sha256=sha(__file__),contract_sha256=t['contract_sha256'],diagnostics={k:v for k,v in j.items() if k!='audit'},audit=j.get('audit'),native_files={str(x):sha(x) for x in out.iterdir() if x.is_file()},source_clock_role='actual_receipt_only_not_historical_PIT_or_intraday_available',SDK_client_identifier_role='pinnedSDKprotocolidentifier_not_verified_server_distribution',SDK_wire_bytes_and_internal_retry_counts='unknown',repeated_pair_calendar_ETF_queries=0,automatic_retries=0,economic_runs=0,new_factors_or_strategies=0,model_admitted=False,full_A_coverage=False,forward_admission=False,full_goal_complete=False,raw_values_exported=False)
    r['failure_type']=r['failure_type'] or j.get('failure_type');r['state']='current_provider_membership_and_daily_shape_reconciled' if r['returncode']==0 and not r['timed_out'] and not r['failure_type'] and r['audit'] else 'source_failure_preserved'
    write(O,r);write(out/'terminal.json',r,exclusive=True)
    print(json.dumps({k:r[k] for k in ['state','returncode','timed_out','failure_type','diagnostics','audit','model_admitted']}))
    return 0 if r['state']=='current_provider_membership_and_daily_shape_reconciled' else 1

if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);g=ap.add_mutually_exclusive_group();g.add_argument('--child',action='store_true');g.add_argument('--validate-plan',action='store_true');args=ap.parse_args();p=read(P)
    if args.validate_plan:gate(p);print(json.dumps({'frozen_scope_checked':True,'source_calls':0}))
    else:raise SystemExit(child(p) if args.child else run(p))
