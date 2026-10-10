"""One fixed current dated provider catalogue; no universe/PIT admission."""
from pathlib import Path
import argparse,json,re,socket,subprocess,sys
ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from research.capture_completed_daily_pair import read,write,sha,clock,first_response,claim_child_dispatch,completed_cutoff
P=ROOT/'research/CURRENT_DATED_LIST_PROTOCOL_20261009.json'
O=ROOT/'research/CURRENT_DATED_LIST_RESULT_20261009.json'
FIELDS=['code','tradeStatus','code_name'];DAY='2026-10-08'

def validate_list(rows):
    if not isinstance(rows,list) or not 1<=len(rows)<=12000:raise ValueError('dated_list_nonempty_cached_cap')
    for r in rows:
        if not isinstance(r,dict) or set(r)!=set(FIELDS) or not all(isinstance(v,str) for v in r.values()):
            raise ValueError('dated_list_fields')
        if not re.fullmatch(r'(?:sh|sz)\.[0-9]{6}',r['code']) or r['tradeStatus'] not in ('0','1'):
            raise ValueError('dated_list_code_or_status')
    codes=[r['code'] for r in rows]
    if len(codes)!=len(set(codes)):raise ValueError('dated_list_duplicate_code')
    return {'rows':len(rows),'codes_unique':True,'provider_trade_status_1_rows':sum(r['tradeStatus']=='1' for r in rows),
            'provider_trade_status_0_rows':sum(r['tradeStatus']=='0' for r in rows),
            'empty_name_rows':sum(not r['code_name'].strip() for r in rows),
            'SDK_equal_page_block_2000_requires_more_data':len(rows)==2000,
            'provider_full_population_verified':False,'full_A_including_BSE_verified':False,
            'stock_ETF_index_types_verified':False,'permanent_identity_verified':False,
            'historical_universe_PIT_verified':False,'market_gate_ready':False,
            'model_admitted':False,'orders_enabled':False}

def gate(p):
    from scripts.workflow_guard import contract_hash
    t=read(ROOT/'WORKFLOW_TASK.json');idx={a['path']:a['sha256'] for a in t['artifacts']}
    assert t['id']==p['parent_task'] and t['owner_session']==p['owner_session'] and t['status']=='in_progress'
    assert t['contract_sha256']==contract_hash(t) and idx[P.relative_to(ROOT).as_posix()]==sha(P)
    assert p['day']==DAY and p['API_call']=='query_all_stock(day=2026-10-08)'
    for f,h in p['input_hashes'].items():assert sha(ROOT/f)==h
    for f,h in p['native_sdk_hashes'].items():assert sha(f)==h
    completed_cutoff(clock(),DAY)
    assert 'onedrive' not in str(Path(p['native_directory']).resolve()).lower()
    return t

def child(p):
    gate(p);claim_child_dispatch(p);socket.setdefaulttimeout(8)
    def audit(event,args):
        if event=='socket.connect' and args[1]!=('public-api.baostock.com',10030):raise PermissionError('unexpected_target')
        if event in ('subprocess.Popen','os.system'):raise PermissionError('child_spawn')
    sys.addaudithook(audit);sys.path.insert(0,p['sdk_directory'])
    import baostock as bs
    import baostock.common.context as context
    import baostock.common.contants as constants
    assert constants.BAOSTOCK_PER_PAGE_COUNT==2000
    out=Path(p['native_directory'])/'capture';path=out/'journal.json'
    j={'started_utc':clock(),'guest_handshake_attempts':0,'semantic_query_attempts':0,
       'next_calls':0,'logout_attempts':0,'stage':'before_guest','failure_type':None}
    try:
        j['guest_handshake_attempts']=1;write(path,j);lg=bs.login();j['handshake_error_code']=str(lg.error_code);write(path,j)
        if str(lg.error_code)!='0':raise ValueError('anonymous_guest_handshake_failed')
        j.update(stage='fixed_current_dated_list',semantic_query_attempts=1,sent_at_utc=clock());write(path,j)
        rs=bs.query_all_stock(day=DAY)
        j.update(returned_at_utc=clock(),error_code=str(rs.error_code),fields=list(rs.fields),response_day=str(getattr(rs,'day','')),
                 cached_rows=len(rs.data) if isinstance(rs.data,list) else None);write(path,j)
        rows=first_response(rs,FIELDS,12000)
        payload={'schema':'regis.current_dated_list_raw.v1','fields':FIELDS,'rows':rows,
                 'observed_at_utc':j['returned_at_utc'],'client_identifier':str(constants.BAOSTOCK_CLIENT_VERSION),
                 'SDK_distribution_version':p['SDK_distribution_version'],'query_day':DAY,'response_day':j['response_day']}
        f=out/'source.json';write(f,payload,exclusive=True);j['source_ref']={'path':str(f),'sha256':sha(f)};write(path,j)
        if j['response_day']!=DAY:raise ValueError('dated_list_response_day_mismatch')
        j['summary']=validate_list(rows);j['stage']='dated_provider_catalogue_observed'
        # No next/page retry. Exact2000 means a known unresolved page boundary.
        # Other lengths still cannot certify fullA, types or PIT completeness.
    except Exception as exc:
        j['failure_type']=type(exc).__name__
        if isinstance(exc,ValueError):j['validation_label']=str(exc)[:180]
    finally:
        if j.get('handshake_error_code')=='0':
            j['logout_attempts']=1;write(path,j)
            try:j['logout_error_code']=str(bs.logout().error_code)
            except Exception as exc:j['logout_failure_type']=type(exc).__name__
        if hasattr(context,'default_socket'):
            try:context.default_socket.close()
            except Exception:pass
        j['ended_utc']=clock();write(path,j)
    return 0 if j['failure_type'] is None else 1

def run(p):
    t=gate(p);protocol_hash,implementation_hash=sha(P),sha(__file__)
    out=Path(p['native_directory'])/'capture';out.mkdir(exist_ok=False)
    write(out/'protocol.json',p,exclusive=True);write(out/'frozen_task.json',t,exclusive=True)
    r={'schema':'regis.current_dated_list_result.v1','started_utc':clock(),'child_returncode':None,
       'child_timed_out':False,'failure_type':None,'fresh_source_dispatches':1,'retries':0,
       'economic_runs':0,'new_formulas':0,'raw_rows_exported':False,'model_admitted':False,
       'full_goal_complete':False,'historical_universe_PIT_verified':False,'market_gate_ready':False}
    phase='single_child_source_capture'
    try:
        q=subprocess.run([sys.executable,'-I','-X','utf8',str(Path(__file__).resolve()),'--child'],
                         cwd=out,capture_output=True,timeout=55,creationflags=0x08000000 if sys.platform=='win32' else 0)
        r['child_returncode']=q.returncode;(out/'stdout.log').write_bytes(q.stdout);(out/'stderr.log').write_bytes(q.stderr)
        j=read(out/'journal.json');r['diagnostics']=j;r['failure_type']=j.get('failure_type')
        if q.returncode!=0 or r['failure_type']:raise ValueError('current_dated_list_source_not_passed')
        phase='post_source_binding';gate(p);r['state']='fixed_current_dated_catalogue_source_facts_observed'
    except subprocess.TimeoutExpired as exc:
        r.update(child_timed_out=True,failure_type='TimeoutExpired',failure_phase=phase)
        (out/'stdout.log').write_bytes(exc.stdout or b'');(out/'stderr.log').write_bytes(exc.stderr or b'')
    except Exception as exc:
        r['failure_type']=r['failure_type'] or type(exc).__name__;r['failure_phase']=phase
        if phase=='post_source_binding':r['native_source_invalidated_by_binding_failure']=True
    if 'diagnostics' not in r:
        try:r['diagnostics']=read(out/'journal.json')
        except (OSError,ValueError):r['diagnostics']={'partial_journal_unavailable':True}
    if r['failure_type']:r['state']='current_dated_list_failure_preserved'
    r.update(ended_utc=clock(),protocol_sha256=protocol_hash,implementation_sha256=implementation_hash,
             contract_sha256=t['contract_sha256'],source_clock_role='actual_receipt_not_historical_PIT',
             source_identifier_role='SDK_client_protocol_identifier_not_server_release',
             SDK_internal_wire_bytes_or_retry_counts='unknown',full_A_verified=False,orders_enabled=False)
    write(O,r);write(out/'terminal.json',r,exclusive=True);print(json.dumps(r))
    return 0 if r['failure_type'] is None else 1

if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);g=ap.add_mutually_exclusive_group()
    g.add_argument('--validate-plan',action='store_true');g.add_argument('--child',action='store_true')
    args=ap.parse_args();p=read(P)
    if args.validate_plan:gate(p);print(json.dumps({'frozen_scope_checked':True,'source_calls':0}))
    else:raise SystemExit(child(p) if args.child else run(p))
