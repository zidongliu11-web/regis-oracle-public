"""One bounded original-SDK paginated current catalogue capture."""
from pathlib import Path
import argparse,json,re,socket,subprocess,sys
ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from research.capture_completed_daily_pair import read,write,sha,clock,claim_child_dispatch,completed_cutoff
from research.capture_current_dated_list import validate_list
from research.provider_pagination import collect_pages
P=ROOT/'research/CURRENT_PAGED_CATALOGUE_PROTOCOL_20261009.json'
O=ROOT/'research/CURRENT_PAGED_CATALOGUE_RESULT_20261009.json'
DAY='2026-10-08';LIST_FIELDS=['code','tradeStatus','code_name'];BASIC_FIELDS=['code','code_name','ipoDate','outDate','type','status']

def validate_basic(rows):
    if not rows or len(rows)>12000:raise ValueError('current_basic_nonempty_cap')
    for r in rows:
        if set(r)!=set(BASIC_FIELDS) or not all(isinstance(v,str) for v in r.values()):raise ValueError('current_basic_fields')
        if not re.fullmatch(r'(sh|sz)\.[0-9]{6}',r['code']) or r['type'] not in ('1','2','3') or r['status'] not in ('0','1'):
            raise ValueError('current_basic_identifier_or_flags')
    if len({r['code'] for r in rows})!=len(rows):raise ValueError('current_basic_duplicate_code')
    return {'rows':len(rows),'codes_unique':True,'provider_type1_rows':sum(r['type']=='1' for r in rows),
            'provider_type2_rows':sum(r['type']=='2' for r in rows),'provider_type3_rows':sum(r['type']=='3' for r in rows),
            'provider_status1_rows':sum(r['status']=='1' for r in rows),'permanent_id_or_historical_PIT_verified':False,'full_A_verified':False}

def gate(p):
    from scripts.workflow_guard import contract_hash
    t=read(ROOT/'WORKFLOW_TASK.json');idx={a['path']:a['sha256'] for a in t['artifacts']}
    assert t['id']==p['parent_task'] and t['owner_session']==p['owner_session'] and t['status']=='in_progress'
    assert t['contract_sha256']==contract_hash(t) and idx[P.relative_to(ROOT).as_posix()]==sha(P)
    assert p['day']==DAY and p['bounds']['socket_seconds']==20 and p['bounds']['source_child_seconds']==100
    assert p['dated_list_case_attempt_number']==3 and p['emptycode_basic_case_attempt_number']==2
    for f,h in p['input_hashes'].items():assert sha(ROOT/f)==h
    for f,h in p['native_sdk_hashes'].items():assert sha(f)==h
    assert 'onedrive' not in str(Path(p['native_directory']).resolve()).lower()
    completed_cutoff(clock(),DAY)
    return t

def child(p):
    gate(p);claim_child_dispatch(p);socket.setdefaulttimeout(20)
    def audit(event,args):
        if event=='socket.connect' and args[1]!=('public-api.baostock.com',10030):raise PermissionError('unexpected_target')
        if event in ('subprocess.Popen','os.system'):raise PermissionError('child_spawn')
    sys.addaudithook(audit);sys.path.insert(0,p['sdk_directory'])
    import baostock as bs
    import baostock.common.context as context
    import baostock.common.contants as constants
    assert constants.BAOSTOCK_PER_PAGE_COUNT==2000
    out=Path(p['native_directory'])/'capture';jp=out/'journal.json'
    j={'started_utc':clock(),'stage':'before_guest','guest_handshake_attempts':0,'initial_query_attempts':0,
       'page_fetch_next_attempts':0,'cached_next_calls_completed_before_fetch':0,'logout_attempts':0,
       'within_child_retries':0,'refs':{},'roles':{},'pages':[],'failure_type':None}
    try:
        j['guest_handshake_attempts']=1;write(jp,j);lg=bs.login();j['handshake_error_code']=str(lg.error_code);write(jp,j)
        if str(lg.error_code)!='0':raise ValueError('anonymous_guest_handshake_failed')
        all_rows={}
        for role,fields in [('dated_list',LIST_FIELDS),('current_basic',BASIC_FIELDS)]:
            j.update(stage=role,initial_query_attempts=j['initial_query_attempts']+1);j['roles'][role]={'sent_at_utc':clock()};write(jp,j)
            rs=bs.query_all_stock(day=DAY) if role=='dated_list' else bs.query_stock_basic(code='')
            info=j['roles'][role];info.update(returned_first_at_utc=clock(),first_error_code=str(rs.error_code),first_fields=list(rs.fields))
            if role=='dated_list':
                info['initial_response_day']=str(getattr(rs,'day',''))
                if info['initial_response_day']!=DAY:write(jp,j);raise ValueError('initial_dated_response_mismatch')
            write(jp,j)
            def emit(number,rows,meta):
                f=out/(role+'_page_'+str(number)+'.json')
                write(f,{'schema':'regis.provider_catalogue_page_raw.v1','role':role,'fields':fields,'rows':rows,'observed_at_utc':clock(),'requested_day':DAY if role=='dated_list' else None,'subsequent_day_or_field_headers_exposed_by_SDK_next':False},exclusive=True)
                j['pages'].append({**meta,'role':role,'raw_ref':{'path':str(f),'sha256':sha(f)},'observed_at_utc':clock()});write(jp,j)
            def before_fetch(meta):
                j['page_fetch_next_attempts']+=1;j['cached_next_calls_completed_before_fetch']+=2000
                info['last_page_fetch_sent_at_utc']=clock();info['next_page_requested']=meta['next_page'];write(jp,j)
            rows,paging=collect_pages(rs,fields,page_cap=6,row_cap=12000,emit=emit,before_fetch=before_fetch)
            info['paging']=paging
            info['validation']=validate_list(rows) if role=='dated_list' else validate_basic(rows)
            f=out/(role+'_complete_or_partial.json')
            write(f,{'schema':'regis.current_provider_catalogue_raw.v1','role':role,'fields':fields,'rows':rows,'observed_at_utc':clock(),'paging':paging,'source_date_role':'initial_date_checked_and_fixed_request_in_SDK_next_not_per_page_response_date_proof','client_identifier':str(constants.BAOSTOCK_CLIENT_VERSION)},exclusive=True)
            j['refs'][role]={'path':str(f),'sha256':sha(f)};all_rows[role]=rows;write(jp,j)
            if not paging['provider_pagination_complete']:raise ValueError('provider_catalogue_partial_page_budget')
        listed=all_rows['dated_list'];basic={r['code']:r for r in all_rows['current_basic']}
        j['join_summary']={'dated_rows':len(listed),'current_basic_rows':len(basic),'listed_missing_current_basic':sum(r['code'] not in basic for r in listed),
            'listed_provider_type1_rows':sum(basic.get(r['code'],{}).get('type')=='1' for r in listed),
            'types_are_current_provider_labels_not_signal_date_PIT':True,'research_full_A_including_BSE_verified':False,
            'market_gate_ready':False,'factor_or_model_admitted':False}
        j['stage']='current_provider_catalogues_pagination_observed'
    except Exception as exc:
        j['failure_type']=type(exc).__name__
        if isinstance(exc,ValueError):j['validation_label']=str(exc)[:180]
    finally:
        if j.get('handshake_error_code')=='0':
            j['logout_attempts']=1;write(jp,j)
            try:j['logout_error_code']=str(bs.logout().error_code)
            except Exception as exc:j['logout_failure_type']=type(exc).__name__
        if hasattr(context,'default_socket'):
            try:context.default_socket.close()
            except Exception:pass
        j['ended_utc']=clock();write(jp,j)
    return 0 if j['failure_type'] is None else 1

def run(p):
    t=gate(p);ph,ih=sha(P),sha(__file__);out=Path(p['native_directory'])/'capture';out.mkdir(exist_ok=False)
    write(out/'protocol.json',p,exclusive=True);write(out/'frozen_task.json',t,exclusive=True)
    r={'schema':'regis.current_paged_catalogue_result.v1','started_utc':clock(),'child_returncode':None,'child_timed_out':False,
       'failure_type':None,'fresh_child_dispatches':1,'automatic_retries':0,'dated_list_case_attempt_number':3,
       'emptycode_basic_case_attempt_number':2,'economic_runs':0,'new_formulas':0,'raw_rows_exported':False,'model_admitted':False,'full_goal_complete':False}
    phase='bounded_original_SDK_catalogue_capture'
    try:
        q=subprocess.run([sys.executable,'-I','-X','utf8',str(Path(__file__).resolve()),'--child'],cwd=out,capture_output=True,timeout=100,creationflags=0x08000000 if sys.platform=='win32' else 0)
        r['child_returncode']=q.returncode;(out/'stdout.log').write_bytes(q.stdout);(out/'stderr.log').write_bytes(q.stderr)
        j=read(out/'journal.json');r['diagnostics']=j;r['failure_type']=j.get('failure_type')
        if q.returncode!=0 or r['failure_type']:raise ValueError('current_paged_catalogue_not_passed')
        phase='post_capture_binding';gate(p);r['state']='current_provider_paged_catalogues_source_facts_only'
    except subprocess.TimeoutExpired as exc:
        r.update(child_timed_out=True,failure_type='TimeoutExpired',failure_phase=phase)
        (out/'stdout.log').write_bytes(exc.stdout or b'');(out/'stderr.log').write_bytes(exc.stderr or b'')
    except Exception as exc:
        r['failure_type']=r['failure_type'] or type(exc).__name__;r['failure_phase']=phase
        if phase=='post_capture_binding':r['native_source_invalidated_by_binding_failure']=True
    if 'diagnostics' not in r:
        try:r['diagnostics']=read(out/'journal.json')
        except (OSError,ValueError):r['diagnostics']={'partial_journal_unavailable':True}
    if r['failure_type']:r['state']='current_paged_catalogue_partial_failure_preserved'
    r.update(ended_utc=clock(),protocol_sha256=ph,implementation_sha256=ih,contract_sha256=t['contract_sha256'],
             socket_seconds=20,SDK_internal_wire_bytes_or_retries='unknown',full_A_verified=False,historical_PIT_verified=False,market_gate_ready=False,orders_enabled=False)
    write(O,r);write(out/'terminal.json',r,exclusive=True);print(json.dumps(r))
    return 0 if r['failure_type'] is None else 1
if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);g=ap.add_mutually_exclusive_group();g.add_argument('--validate-plan',action='store_true');g.add_argument('--child',action='store_true')
    a=ap.parse_args();p=read(P)
    if a.validate_plan:gate(p);print(json.dumps({'frozen_scope_checked':True,'source_queries':0}))
    else:raise SystemExit(child(p) if a.child else run(p))
