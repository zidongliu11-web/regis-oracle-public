"""Finite existing symbol ETF route; reuse source predicates/hidden dispatch.

Only the unresolved ETF input is queried. No repeated calendar/stock/bulk calls,
no retry or economic run. SDK raw values stay in Native storage.
"""
from pathlib import Path
import argparse,json,socket,subprocess,sys
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from research.capture_completed_daily_pair import read,write,sha,clock,completed_cutoff,first_response,bar_checks,claim_child_dispatch,ETF_FIELDS
P=ROOT/'research/SYMBOL_ETF_CURRENT_PROTOCOL_20261008.json'
O=ROOT/'research/SYMBOL_ETF_CURRENT_RESULT_20261008.json'

def gate(p):
    from scripts.workflow_guard import contract_hash
    t=read(ROOT/'WORKFLOW_TASK.json');idx={a['path']:a['sha256'] for a in t['artifacts']}
    assert t['id']==p['parent_task'] and t['status']=='in_progress' and t['owner_session']==p['owner_session'] and t['contract_sha256']==contract_hash(t)
    assert idx[P.relative_to(ROOT).as_posix()]==sha(P)
    for path,h in p['input_hashes'].items():assert sha(ROOT/path)==h
    for path,h in p['native_sdk_hashes'].items():assert sha(path)==h
    assert p['code']=='sh.510300' and p['start']=='2026-09-29' and p['end']=='2026-10-08'
    assert p['expected_dates']==['2026-09-29','2026-09-30','2026-10-08']
    completed_cutoff(clock(),p['end'])
    return t

def child(p):
    gate(p);claim_child_dispatch(p);socket.setdefaulttimeout(8)
    def audit(e,a):
        if e=='socket.connect' and a[1]!=('public-api.baostock.com',10030):raise PermissionError('unexpected_target')
        if e in ('subprocess.Popen','os.system'):raise PermissionError('child_spawn')
    sys.addaudithook(audit);sys.path.insert(0,p['sdk_directory'])
    import baostock as bs
    import baostock.common.context as ctx
    out=Path(p['native_directory'])/'capture';j={'handshake_attempts':0,'query_attempts':0,'logout_attempts':0,'failure_type':None,'stage':'before_handshake','started_utc':clock()};path=out/'journal.json'
    try:
        j['handshake_attempts']=1;write(path,j);lg=bs.login();j['handshake_error_code']=str(lg.error_code);write(path,j)
        if lg.error_code!='0':raise ValueError('guest_handshake_failed')
        j.update(query_attempts=1,stage='query',sent_at_utc=clock());write(path,j)
        rs=bs.query_history_k_data_plus(p['code'],','.join(ETF_FIELDS),start_date=p['start'],end_date=p['end'],frequency='d',adjustflag='3')
        j.update(returned_at_utc=clock(),error_code=str(rs.error_code),returned_fields=list(rs.fields),first_response_type=type(rs.data).__name__,first_response_rows=len(rs.data) if isinstance(rs.data,list) else None,stage='first_response_validation');write(path,j)
        rows=first_response(rs,ETF_FIELDS,10);j['rows']=rows;j['stage']='bar_validation';write(path,j)
        j['checks']=bar_checks(rows,p['expected_dates'],p['code'],stock=False);j['stage']='predicates_complete'
    except Exception as e:
        j['failure_type']=type(e).__name__
        if type(e) is ValueError:j['validation_label']=str(e)[:96]
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
    r={'schema':'regis.symbol_ETF_current_result.v1','started_utc':clock(),'returncode':None,'timed_out':False,'failure_type':None}
    try:
        c=subprocess.run([sys.executable,'-I','-X','utf8',str(Path(__file__).resolve()),'--child'],cwd=out,capture_output=True,timeout=30,creationflags=0x08000000 if sys.platform=='win32' else 0)
        r['returncode']=c.returncode;(out/'stdout.log').write_bytes(c.stdout);(out/'stderr.log').write_bytes(c.stderr)
    except subprocess.TimeoutExpired as e:
        r.update(timed_out=True,failure_type='TimeoutExpired');(out/'stdout.log').write_bytes(e.stdout or b'');(out/'stderr.log').write_bytes(e.stderr or b'')
    except Exception as e:r['failure_type']=type(e).__name__
    gate(p);j=read(out/'journal.json') if (out/'journal.json').exists() else {};r['ended_utc']=clock()
    r.update(protocol_sha256=sha(P),implementation_sha256=sha(__file__),contract_sha256=t['contract_sha256'],diagnostics={k:v for k,v in j.items() if k not in ('rows','checks')},checks=j.get('checks'),native_files={str(x):sha(x) for x in out.iterdir() if x.is_file()},source_clock_role='actual_receipt_only_not_historical_PIT_or_intraday_available',stock_calendar_bulk_queries=0,retries=0,economic_runs=0,model_admitted=False,full_A_coverage=False,forward_admission=False,full_goal_complete=False,raw_values_exported=False)
    r['failure_type']=r['failure_type'] or j.get('failure_type');r['state']='symbol_ETF_current_pair_input_obtained' if r['returncode']==0 and not r['timed_out'] and not r['failure_type'] and r['checks'] else 'source_failure_preserved'
    write(O,r);write(out/'terminal.json',r,exclusive=True)
    print(json.dumps({k:r[k] for k in ['state','returncode','timed_out','failure_type','diagnostics','checks','model_admitted']}))
    return 0 if r['state']=='symbol_ETF_current_pair_input_obtained' else 1

if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);g=ap.add_mutually_exclusive_group();g.add_argument('--child',action='store_true');g.add_argument('--validate-plan',action='store_true');args=ap.parse_args();p=read(P)
    if args.validate_plan:gate(p);print(json.dumps({'frozen_scope_checked':True,'source_calls':0}))
    else:raise SystemExit(child(p) if args.child else run(p))
