"""One offline retained bad-sample replay and reference-only input consolidation."""
from pathlib import Path
from datetime import datetime,timezone
import argparse,json,sys
ROOT=Path(__file__).resolve().parents[1];sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from research.capture_completed_daily_pair import read,write,sha,clock,STOCK_FIELDS
from research.compute_retained_current_lookback import read_verified_source
from factor_system.sample_quality_isolation import evaluate_history
P=ROOT/'research/SAMPLE_QUALITY_ISOLATION_PROTOCOL_20261009.json'
O=ROOT/'research/SAMPLE_QUALITY_ISOLATION_RESULT_20261009.json'

def gate(p):
    from scripts.workflow_guard import contract_hash
    t=read(ROOT/'WORKFLOW_TASK.json');a={v['path']:v['sha256'] for v in t['artifacts']}
    assert t['id']==p['parent_task'] and t['owner_session']==p['owner_session'] and t['status']=='in_progress'
    assert t['contract_sha256']==contract_hash(t) and a[P.relative_to(ROOT).as_posix()]==sha(P)
    assert p['source_queries']==0 and p['formal_batches']==4 and p['new_unit_tests']==5
    assert p['original_formula']=='-pct_chg/100' and p['new_formulas']==0
    for f,h in p['input_hashes'].items():assert sha(ROOT/f)==h
    for ref in list(p['calendar_refs'].values())+[p['bad_history_ref']]+p['existing_input_refs']:read_verified_source(ref)
    assert not read(ROOT/'research/FIXED_SAMPLE_INPUT_PRODUCTION_PROTOCOL_20261009.json').get('stock_quality_failure_isolation',False)
    assert 'onedrive' not in str(Path(p['native_directory']).resolve()).lower()
    return t

def run(p):
    t=gate(p);protocol_hash,implementation_hash=sha(P),sha(__file__)
    out=Path(p['native_directory'])/'retained';out.mkdir(exist_ok=False)
    write(out/'protocol.json',p,exclusive=True);write(out/'frozen_task.json',t,exclusive=True)
    def audit(event,args):
        if event.startswith('socket.') or event in ('subprocess.Popen','os.system'):raise PermissionError('offline_sample_isolation_network_or_spawn_denied')
    sys.addaudithook(audit)
    r={'schema':'regis.sample_quality_isolation_result.v1','started_utc':clock(),'failure_type':None}
    try:
        src=read_verified_source(p['bad_history_ref'])
        assert src['schema']=='regis.fixed_sample_SDK_decoded_response.v1' and src['source_code']=='sh.601059'
        assert src['SDK_error_code']=='0' and src['fields']==STOCK_FIELDS and len(src['rows'])==21
        observed=datetime.fromisoformat(src['observed_at_utc']);assert observed.tzinfo and observed<=datetime.now(timezone.utc)
        old=read_verified_source(p['calendar_refs']['history'])
        current={'calendar':read_verified_source(p['calendar_refs']['current'])['observations']['calendar'],'stock':{'rows':[]}}
        history={'schema':'baostock_symbol_history_raw.v1','fields':STOCK_FIELDS,'rows':src['rows']}
        native,disposition=evaluate_history('2026-10-08',history,current,old,source_code='sh.601059')
        assert native is None and disposition['reason']=='original_numeric_parser_invalid' and not disposition['factor_input_computed']
        refs=[]
        for ref in p['existing_input_refs']:
            feature=read_verified_source(ref)
            assert feature['schema']=='regis.current_original_reversal_input.v1' and feature['factor_id']=='m24_negative_daily_return_rank' and feature['formula']==p['original_formula']
            assert feature['selector_input_fields']['code']==ref['source_code'][3:] and feature['selector_input_fields']['trade_date']=='2026-10-08'
            assert feature['selector_status']=='blocked_missing_required_evidence' and not feature['selection_executed'] and feature['selected'] is None
            assert len(feature['missing_selector_fields'])==8
            assert feature['selector_input_fields']['available_at']==feature['computed_at_utc']
            computed=datetime.fromisoformat(feature['computed_at_utc']);assert computed.tzinfo and computed<=datetime.now(timezone.utc)
            for key in ['production_eligible','model_admitted','generates_alert','orders_enabled','historical_PIT_verified','full_A_verified','market_gate_ready','permanent_identity_verified']:assert feature['authority'][key] is False
            refs.append({'source_code':ref['source_code'],'original_input_ref':ref,'computed_at_utc':feature['computed_at_utc'],'missing_selector_fields':feature['missing_selector_fields'],'additional_blockers':feature['additional_blockers'],'reused_without_numerical_recomputation':True,'disposition':'original_input_present_research_only'})
        assert [q['source_code'] for q in refs]==['sh.600000','sh.600084']
        manifest={'schema':'regis.original_current_input_reference_manifest.v1','assembled_utc':clock(),'factor_id':'m24_negative_daily_return_rank','signal_day':'2026-10-08','original_inputs':refs,'quarantined_sources':[{'source_ref':p['bad_history_ref'],'disposition':disposition}],'research_only':True,'selection_executed':False,'selected_count':None,'model_admitted':False}
        write(out/'merged_input_reference_manifest.json',manifest,exclusive=True)
        r.update(state='bad_stock_isolated_existing_inputs_retained_no_source_or_recalculation',existing_original_input_rows=2,quarantined_sample_rows=1,retained_source_history_rows=21,
            retained_bad_history_replay_attempts=1,successful_lookback_recomputations=0,quarantine_disposition=disposition,
            existing_input_refs=p['existing_input_refs'],reference_manifest={'path':str(out/'merged_input_reference_manifest.json'),'sha256':sha(out/'merged_input_reference_manifest.json')},
            existing_input_rows_are_previous_computations_not_new_values=True,current_fixed_sample_coverage=3,sample_is_not_full_universe_or_independent_financial_case=True,
            revised_producer_live_source_enabled=False,old_frozen_source_protocol_requires_separate_new_freeze=True,
            processing_vs_source_quality='Offline cases validate continuation, retained source is quarantined, no new live mixed-source batch has been executed',source_quality_passed=False)
        gate(p)
    except Exception as exc:r.update(state='sample_isolation_failure_preserved',failure_type=type(exc).__name__)
    r.update(ended_utc=clock(),protocol_sha256=protocol_hash,implementation_sha256=implementation_hash,contract_sha256=t['contract_sha256'],source_queries=0,source_dispatches=0,SDK_calls=0,
        new_factor_values_computed=0,new_formulas=0,economic_runs=0,selected_count=None,selection_executed=False,
        original_source_or_status_imputation=False,raw_market_values_exported=False,full_A_verified=False,historical_PIT_verified=False,market_gate_ready=False,
        original_107_quarantines_unchanged=True,model_admitted=False,orders_enabled=False,full_goal_complete=False)
    write(O,r);write(out/'terminal.json',r,exclusive=True);print(json.dumps(r));return 0 if r['failure_type'] is None else 1

if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--validate-plan',action='store_true');args=ap.parse_args();p=read(P)
    if args.validate_plan:gate(p);print(json.dumps({'frozen_scope_checked':True,'source_queries':0,'revised_producer_source_enabled':False}))
    else:raise SystemExit(run(p))
