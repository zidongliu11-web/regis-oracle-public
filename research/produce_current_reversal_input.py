"""One actual local adapter computation for the already registered reversal."""
from pathlib import Path
import argparse,json,sys
ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from research.capture_completed_daily_pair import read,write,sha,clock
from research.compute_retained_current_lookback import read_verified_source
from factor_system.current_reversal_input import build_input
P=ROOT/'research/CURRENT_REVERSAL_INPUT_PROTOCOL_20261009.json'
O=ROOT/'research/CURRENT_REVERSAL_INPUT_RESULT_20261009.json'

def gate(p):
    from scripts.workflow_guard import contract_hash
    t=read(ROOT/'WORKFLOW_TASK.json')
    assert t['id']==p['parent_task'] and t['owner_session']==p['owner_session'] and t['status']=='in_progress'
    assert t['contract_sha256']==contract_hash(t)
    idx={a['path']:a['sha256'] for a in t['artifacts']}
    assert idx[P.relative_to(ROOT).as_posix()]==sha(P)
    for f,h in p['input_hashes'].items():assert sha(ROOT/f)==h
    for r in p['source_refs'].values():assert sha(r['path'])==r['sha256']
    registered=read(ROOT/'research/GOAL_FIRST_FACTOR_REGISTRATION_20261002.json')
    factor=next(x for x in registered['factors'] if x['id']=='m24_negative_daily_return_rank')
    assert factor['formula_variants']==1 and factor['decision']=='evidence_insufficient' and factor['production_eligible'] is False
    assert p['factor_id']==factor['id'] and p['factor_formula']=='-pct_chg/100'
    assert 'onedrive' not in str(Path(p['native_directory']).resolve()).lower()
    return t

def run(p):
    t=gate(p);ph,ih=sha(P),sha(__file__)
    out=Path(p['native_directory'])/'calculation';out.mkdir(exist_ok=False)
    write(out/'protocol.json',p,exclusive=True);write(out/'frozen_task.json',t,exclusive=True)
    write(out/'dispatched.json',{'started_utc':clock(),'computation_attempts':1,'fresh_source_queries':0},exclusive=True)
    def audit(event,args):
        if event.startswith('socket.') or event in ('subprocess.Popen','os.system'):
            raise PermissionError('offline_adapter_network_or_spawn')
    sys.addaudithook(audit)
    r={'schema':'regis.current_reversal_input_result.v1','started_utc':clock(),'failure_type':None,
       'actual_computation_attempts':1,'fresh_source_queries':0,'new_formula_variants':0,'economic_runs':0,
       'raw_values_exported':False,'model_admitted':False,'generates_alert':False,'orders_enabled':False,'full_goal_complete':False}
    phase='original_input_join_and_clock'
    try:
        objects={k:read_verified_source(v) for k,v in p['source_refs'].items()}
        native,summary=build_input(objects['lookback'],objects['current_identity'],computed_at=clock())
        native['source_refs']=p['source_refs'];native['factor_registration_binding']=p['factor_registration_binding']
        path=out/'current_original_reversal_input.json';write(path,native,exclusive=True)
        r.update(summary=summary,native_input={'path':str(path),'sha256':sha(path)})
        phase='post_computation_binding';gate(p)
        r['state']='original_current_reversal_input_computed_selector_blocked'
    except Exception as exc:
        r.update(state='current_reversal_input_failure_preserved',failure_type=type(exc).__name__,failure_phase=phase)
        if isinstance(exc,ValueError):r['validation_label']=str(exc)[:180]
        if phase=='post_computation_binding':r['native_input_invalidated_by_binding_failure']=True
    r.update(ended_utc=clock(),protocol_sha256=ph,implementation_sha256=ih,contract_sha256=t['contract_sha256'])
    write(O,r);write(out/'terminal.json',r,exclusive=True);print(json.dumps(r))
    return 0 if r['failure_type'] is None else 1

if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--validate-plan',action='store_true')
    args=ap.parse_args();p=read(P)
    if args.validate_plan:gate(p);print(json.dumps({'frozen_scope_checked':True,'source_queries':0}))
    else:raise SystemExit(run(p))
