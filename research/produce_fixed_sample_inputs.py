"""Finite original daily input production for two previously fixed sample stocks.

Use pinned anonymous SDK history and metadata, preserve decoded strings Native,
then calculate existing reversal inputs. No universe selection or economic trial.
"""
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import argparse, json, socket, subprocess, sys
ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from research.capture_completed_daily_pair import read,write,sha,clock,first_response,STOCK_FIELDS,completed_cutoff,claim_child_dispatch
from research.compute_retained_current_lookback import read_verified_source
from factor_system.sample_quality_isolation import evaluate_history
from research.retained_daily_diagnostic import day
from factor_system.current_reversal_input import build_input
from research.sdk_socket_guard import GuardedSocket
P=ROOT/'research/FIXED_SAMPLE_INPUT_PRODUCTION_PROTOCOL_20261009.json'
O=ROOT/'research/FIXED_SAMPLE_INPUT_PRODUCTION_RESULT_20261009.json'
BASIC_FIELDS=['code','code_name','ipoDate','outDate','type','status']
CODES=['sh.600084','sh.601059']


def validate_identity(rows,code,asof_date):
    if len(rows)!=1 or set(rows[0])!=set(BASIC_FIELDS):
        raise ValueError('exact_single_current_identity_required')
    r=rows[0]
    if r['code']!=code or r['type']!='1' or r['status'] not in ('0','1') or not r['code_name'].strip():
        raise ValueError('current_identity_code_type_or_status')
    ipo,asof=day(r['ipoDate']),day(asof_date)
    if ipo>asof:raise ValueError('listing_after_receipt')
    if r['outDate']:
        out=day(r['outDate'])
        if out<ipo or out>asof or r['status']=='1':raise ValueError('listing_outdate_state')
    elif r['status']=='0':raise ValueError('inactive_without_outdate')
    return {'rows':1,'provider_current_active':r['status']=='1',
            'historical_identity_PIT_verified':False,'permanent_identity_verified':False}


def gate(p):
    from scripts.workflow_guard import contract_hash
    t=read(ROOT/'WORKFLOW_TASK.json')
    assert t['id']==p['parent_task'] and t['owner_session']==p['owner_session'] and t['status']=='in_progress'
    assert t['contract_sha256']==contract_hash(t)
    a={v['path']:v['sha256'] for v in t['artifacts']}
    assert a[P.relative_to(ROOT).as_posix()]==sha(P)
    assert p.get('stock_quality_failure_isolation') is True  # Old frozen source protocol cannot execute revised driver.
    assert p['codes']==CODES and p['start']=='2026-09-02' and p['end']=='2026-10-08'
    assert p['socket_seconds']==20 and p['child_seconds']==100 and p['outer_guard_seconds']==120
    assert p['API_queries_max']==4 and p['next_queries']==0 and p['retries']==0
    for f,h in p['input_hashes'].items():assert sha(ROOT/f)==h
    for f,h in p['native_sdk_hashes'].items():assert sha(f)==h
    for ref in p['calendar_refs'].values():read_verified_source(ref)
    assert 'onedrive' not in str(Path(p['native_directory']).resolve()).lower()
    completed_cutoff(clock(),p['end'])
    return t


def child(p):
    gate(p);claim_child_dispatch(p);socket.setdefaulttimeout(20)
    def audit(event,args):
        if event=='socket.connect' and args[1]!=('public-api.baostock.com',10030):raise PermissionError('unexpected_target')
        if event in ('subprocess.Popen','os.system'):raise PermissionError('unexpected_child_spawn')
    sys.addaudithook(audit);sys.path.insert(0,p['sdk_directory'])
    import baostock as bs
    import baostock.common.context as context
    import baostock.common.contants as constants
    out=Path(p['native_directory'])/'capture';journal=out/'journal.json'
    j={'started_utc':clock(),'guest_attempts':0,'API_query_attempts':0,'next_attempts':0,'automatic_retries':0,
       'logout_attempts':0,'roles':[],'dispositions':[],'inputs':[],'source_refs':[],'failure_type':None,'phase':'guest'}
    guard=None
    def emit(stats):j['transport']=stats;write(journal,j)
    def request(code,role,fn,fields,cap):
        if j['API_query_attempts']>=4:raise ValueError('query_cap')
        j['API_query_attempts']+=1
        j['phase']=role
        a={'code':code,'role':role,'sent_utc':clock(),'returned_utc':None,'SDK_error_code':None,'fields_count':None,'cached_rows':None}
        j['roles'].append(a);write(journal,j)
        rs=fn();a.update(returned_utc=clock(),SDK_error_code=str(rs.error_code),fields_count=len(rs.fields),cached_rows=len(rs.data) if isinstance(rs.data,list) else None)
        write(journal,j)
        # Persist only bounded SDK-decoded objects, never raw packets or user IDs.
        if isinstance(rs.data,list) and len(rs.data)<=cap:
            f=out/(code.replace('.','_')+'_'+role+'_decoded.json')
            payload={'schema':'regis.fixed_sample_SDK_decoded_response.v1','source_code':code,'fields':list(rs.fields),'rows':rs.data,'SDK_error_code':str(rs.error_code),'observed_at_utc':a['returned_utc']}
            write(f,payload,exclusive=True);j['source_refs'].append({'code':code,'role':role,'path':str(f),'sha256':sha(f),'rows':len(rs.data),'SDK_decoded_payload_only':True,'original_source_shape_QA_passed':False});write(journal,j)
        rows=first_response(rs,fields,cap)
        return rows,a['returned_utc']
    try:
        j['guest_attempts']=1;write(journal,j);login=bs.login()
        j['guest_error_code']=str(login.error_code);write(journal,j)
        if str(login.error_code)!='0':raise ValueError('guest_handshake_failed')
        guard=GuardedSocket(context.default_socket,emit);context.default_socket=guard
        calendar=read_verified_source(p['calendar_refs']['history'])
        current_calendar=read_verified_source(p['calendar_refs']['current'])['observations']['calendar']
        for code in CODES:
            bars,bar_clock=request(code,'history',lambda code=code:bs.query_history_k_data_plus(code,','.join(STOCK_FIELDS),start_date=p['start'],end_date=p['end'],frequency='d',adjustflag='3'),STOCK_FIELDS,40)
            # One source response must cover all 21 specified sessions; no paging.
            history={'schema':'baostock_symbol_history_raw.v1','fields':STOCK_FIELDS,'rows':[[r[k] for k in STOCK_FIELDS] for r in bars],'observed_at_utc':bar_clock,'client_version':str(constants.BAOSTOCK_CLIENT_VERSION)}
            current={'calendar':current_calendar,'stock':{'rows':[]}}
            native,disposition=evaluate_history(p['end'],history,current,calendar,source_code=code)
            j['dispositions'].append(disposition)
            if native is None:
                write(journal,j)
                continue  # Valid decoded response, failed stock QA: retain it without identity call or imputation.
            summary=disposition['lookback_summary']
            assert len(bars)==21 and [r['date'] for r in bars]==[r['date'] for r in native['window_rows']]
            for ref in j['source_refs']:
                if ref['code']==code and ref['role']=='history':ref['original_source_shape_QA_passed']=True
            computation=clock()
            native.update(schema='regis.original_M24_lookback_source_input.v1',available_at=computation,computed_at_utc=computation,
                          source_receipt_clocks={'history':bar_clock,'calendar_history':calendar['observed_at_utc'],'calendar_current':current_calendar['observed_at_utc']},
                          historical_publication_clocks='unknown_not_backfilled',source_role='one_new_original_raw_flag3_response_all21sessions')
            # Current metadata is deliberately separate from historical PIT/ID.
            rows,identity_clock=request(code,'identity',lambda code=code:bs.query_stock_basic(code=code),BASIC_FIELDS,1)
            identity={'schema':'regis.single_symbol_current_metadata_raw.v1','fields':BASIC_FIELDS,'rows':rows,'observed_at_utc':identity_clock,'query_code':code}
            observed_date=datetime.fromisoformat(identity_clock).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
            identity_summary=validate_identity(rows,code,observed_date)
            for ref in j['source_refs']:
                if ref['code']==code and ref['role']=='identity':ref['current_metadata_shape_QA_passed']=True
            actual_computed=clock()
            feature,public=build_input(native,identity,computed_at=actual_computed)
            feature.update(source_refs=[r for r in j['source_refs'] if r['code']==code],calendar_refs=p['calendar_refs'],source_receipt_clocks=native['source_receipt_clocks'])
            f=out/(code.replace('.','_')+'_original_reversal_input.json');write(f,feature,exclusive=True)
            disposition['factor_input_computed']=True
            j['inputs'].append({'code':code,'lookback_summary':summary,'identity_summary':identity_summary,'input_summary':public,'native_feature_ref':{'path':str(f),'sha256':sha(f)},'source_daily_rows':len(bars)})
            write(journal,j)
        j['phase']='fixed_sample_inputs_processed'
    except Exception as exc:
        j['failure_type']=type(exc).__name__
        if guard is not None:guard.invalidate('source_response_not_passed')
    finally:
        if guard is not None:j['transport_before_logout']=dict(guard.stats)
        if j.get('guest_error_code')=='0' and (guard is None or not guard.stats['connection_unusable']):
            j['logout_attempts']=1;write(journal,j)
            try:j['logout_error_code']=str(bs.logout().error_code)
            except Exception as exc:j['logout_failure_type']=type(exc).__name__
        elif j.get('guest_error_code')=='0':j['logout_skipped_unusable_connection']=True
        if hasattr(context,'default_socket'):
            try:context.default_socket.close()
            except OSError:pass
        j['ended_utc']=clock();write(journal,j)
    return 0 if j['failure_type'] is None else 1


def run(p):
    t=gate(p)
    protocol_hash,implementation_hash=sha(P),sha(__file__)
    out=Path(p['native_directory'])/'capture';out.mkdir(exist_ok=False)
    write(out/'protocol.json',p,exclusive=True);write(out/'frozen_task.json',t,exclusive=True)
    r={'schema':'regis.fixed_sample_input_production_result.v1','started_utc':clock(),'child_returncode':None,'child_timed_out':False,'failure_type':None}
    try:
        q=subprocess.run([sys.executable,'-I','-X','utf8',str(Path(__file__).resolve()),'--child'],cwd=out,capture_output=True,timeout=100,creationflags=0x08000000 if sys.platform=='win32' else 0)
        r['child_returncode']=q.returncode;(out/'stdout.log').write_bytes(q.stdout);(out/'stderr.log').write_bytes(q.stderr)
    except subprocess.TimeoutExpired as exc:
        r.update(child_timed_out=True,failure_type='TimeoutExpired');(out/'stdout.log').write_bytes(exc.stdout or b'');(out/'stderr.log').write_bytes(exc.stderr or b'')
    except Exception as exc:r['failure_type']=type(exc).__name__
    j=read(out/'journal.json') if (out/'journal.json').exists() else {}
    r['failure_type']=r['failure_type'] or j.get('failure_type')
    try:gate(p)
    except Exception as exc:r['post_source_binding_failed']=True;r['failure_type']=r['failure_type'] or type(exc).__name__
    processed=r['child_returncode']==0 and not r['child_timed_out'] and not r['failure_type'] and [d['source_code'] for d in j.get('dispositions',[])]==CODES
    success=processed and len(j.get('inputs',[]))==2
    quarantine_count=sum(d['disposition']=='quarantined_original_data_QA' for d in j.get('dispositions',[]))
    state='two_fixed_original_factor_inputs_computed_research_only' if success else ('fixed_sample_processing_completed_with_quarantined_inputs' if processed else 'fixed_sample_partial_or_failure_preserved_no_retry')
    r.update(state=state,batch_processing_completed=processed,source_quality_passed=success,quarantined_sample_count=quarantine_count,ended_utc=clock(),
             protocol_sha256=protocol_hash,implementation_sha256=implementation_hash,contract_sha256=t['contract_sha256'],diagnostics=j,
             new_fixed_input_rows=len(j.get('inputs',[])),factor_values_computed=len(j.get('inputs',[])),distinct_original_formulas=1,new_formulas=0,
             economic_runs=0,selection_executed=False,selected_count=None,source_dispatches=1,
             market_values_exported=False,raw_decoded_objects_native_only=True,current_market_data=False,
             clock_role='actual_source_receipt_and_computation_not_backfilled_historical_publication',
             full_A_verified=False,historical_PIT_verified=False,market_gate_ready=False,model_admitted=False,orders_enabled=False,full_goal_complete=False)
    write(O,r);write(out/'terminal.json',r,exclusive=True);print(json.dumps(r))
    return 0 if processed else 1


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);g=ap.add_mutually_exclusive_group();g.add_argument('--validate-plan',action='store_true');g.add_argument('--child',action='store_true')
    args=ap.parse_args();p=read(P)
    if args.validate_plan:gate(p);print(json.dumps({'frozen_scope_checked':True,'source_calls':0}))
    else:raise SystemExit(child(p) if args.child else run(p))
