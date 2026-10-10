"""One anonymous public current metadata query for fixed retained sh.600000."""
from pathlib import Path
import argparse,json,socket,subprocess,sys
from datetime import datetime
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from research.capture_completed_daily_pair import read,write,sha,clock,first_response,claim_child_dispatch
from research.retained_daily_diagnostic import day
P=ROOT/'research/SINGLE_SYMBOL_IDENTITY_PROTOCOL_20261008.json'
O=ROOT/'research/SINGLE_SYMBOL_IDENTITY_RESULT_20261008.json'
FIELDS=['code','code_name','ipoDate','outDate','type','status']

def validate_basic(rows,asof_date):
    if not isinstance(rows,list) or len(rows)!=1:
        raise ValueError('fixed_identity_requires_exactly_one_row')
    r=rows[0]
    if not isinstance(r,dict) or set(r)!=set(FIELDS) or r['code']!='sh.600000' or r['type']!='1':
        raise ValueError('fixed_identity_code_fields_or_stock_type')
    if not isinstance(r['code_name'],str) or not r['code_name'].strip() or r['status'] not in ('0','1'):
        raise ValueError('fixed_identity_name_or_state')
    ipo=day(r['ipoDate']);asof=day(asof_date)
    if ipo>asof:raise ValueError('fixed_identity_listing_after_observation')
    if r['outDate']:
        out=day(r['outDate'])
        if out<ipo or out>asof or r['status']=='1':raise ValueError('fixed_identity_outdate_state_conflict')
    elif r['status']=='0':raise ValueError('fixed_identity_inactive_without_outdate')
    return {'rows':1,'fixed_code_matched':True,'provider_type_stock':True,
            'provider_current_active':r['status']=='1','listing_date_not_future':True,
            'historical_identity_PIT_verified':False,'full_A_universe_verified':False,
            'metadata_only':True,'model_admitted':False,'selector_ready':False}

def gate(p):
    from scripts.workflow_guard import contract_hash
    t=read(ROOT/'WORKFLOW_TASK.json');idx={a['path']:a['sha256'] for a in t['artifacts']}
    assert t['id']==p['parent_task'] and t['owner_session']==p['owner_session'] and t['status']=='in_progress'
    assert t['contract_sha256']==contract_hash(t) and idx[P.relative_to(ROOT).as_posix()]==sha(P)
    assert p['code']=='sh.600000' and p['API_call']=='query_stock_basic(code=sh.600000)'
    for f,h in p['input_hashes'].items():assert sha(ROOT/f)==h
    for f,h in p['native_sdk_hashes'].items():assert sha(f)==h
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
    out=Path(p['native_directory'])/'capture';path=out/'journal.json'
    j={'started_utc':clock(),'guest_handshake_attempts':0,'semantic_query_attempts':0,
       'next_calls':0,'logout_attempts':0,'stage':'before_guest','failure_type':None}
    try:
        j['guest_handshake_attempts']=1;write(path,j);lg=bs.login();j['handshake_error_code']=str(lg.error_code);write(path,j)
        if str(lg.error_code)!='0':raise ValueError('anonymous_guest_handshake_failed')
        j.update(stage='fixed_symbol_metadata',semantic_query_attempts=1,sent_at_utc=clock());write(path,j)
        rs=bs.query_stock_basic(code='sh.600000')
        j.update(returned_at_utc=clock(),error_code=str(rs.error_code),fields=list(rs.fields),
                 cached_rows=len(rs.data) if isinstance(rs.data,list) else None)
        write(path,j);rows=first_response(rs,FIELDS,1)
        # Keep original decoded source strings in Native. No paging/next/retry.
        payload={'schema':'regis.single_symbol_current_metadata_raw.v1','fields':FIELDS,'rows':rows,
                 'observed_at_utc':j['returned_at_utc'],'client_identifier':str(constants.BAOSTOCK_CLIENT_VERSION),
                 'SDK_distribution_version':p['SDK_distribution_version'],'query_code':'sh.600000'}
        f=out/'source.json';write(f,payload,exclusive=True)
        j['source_ref']={'path':str(f),'sha256':sha(f)};write(path,j)
        asof=datetime.fromisoformat(j['returned_at_utc']).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
        j['asof_date_shanghai']=asof;j['summary']=validate_basic(rows,asof);j['stage']='current_metadata_validated'
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
    r={'schema':'regis.single_symbol_identity_result.v1','started_utc':clock(),'child_returncode':None,
       'child_timed_out':False,'failure_type':None,'fresh_source_dispatches':1,'retries':0,
       'economic_runs':0,'new_formulas':0,'raw_metadata_exported':False,'model_admitted':False,
       'full_goal_complete':False,'historical_identity_PIT_verified':False,'selector_ready':False}
    phase='single_child_source_capture'
    try:
        q=subprocess.run([sys.executable,'-I','-X','utf8',str(Path(__file__).resolve()),'--child'],
                         cwd=out,capture_output=True,timeout=55,creationflags=0x08000000 if sys.platform=='win32' else 0)
        r['child_returncode']=q.returncode;(out/'stdout.log').write_bytes(q.stdout);(out/'stderr.log').write_bytes(q.stderr)
        j=read(out/'journal.json');r['diagnostics']=j
        r['failure_type']=j.get('failure_type')
        if q.returncode!=0 or r['failure_type']:raise ValueError('fixed_symbol_source_not_passed')
        phase='post_source_binding';gate(p)
        r['state']='provider_current_fixed_symbol_metadata_observed'
    except subprocess.TimeoutExpired as exc:
        r.update(child_timed_out=True,failure_type='TimeoutExpired',failure_phase=phase)
        (out/'stdout.log').write_bytes(exc.stdout or b'');(out/'stderr.log').write_bytes(exc.stderr or b'')
    except Exception as exc:
        r['failure_type']=r['failure_type'] or type(exc).__name__;r['failure_phase']=phase
        if phase=='post_source_binding':r['native_source_invalidated_by_binding_failure']=True
    if 'diagnostics' not in r:
        try:r['diagnostics']=read(out/'journal.json')
        except (OSError,ValueError):r['diagnostics']={'partial_journal_unavailable':True}
    if r['failure_type']:r['state']='single_symbol_source_failure_preserved'
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
