"""One retained-source amount discrepancy calculation; no tolerance repair."""
from pathlib import Path
from decimal import Decimal
import argparse,json,sys
ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from research.capture_completed_daily_pair import read,write,sha,clock
from research.compute_retained_current_lookback import read_verified_source
from research.current_minute_source import analyse,values,FIELDS,CODE,DATE
P=ROOT/'research/MINUTE_AMOUNT_DIAGNOSIS_PROTOCOL_20261009.json'
O=ROOT/'research/MINUTE_AMOUNT_DIAGNOSIS_RESULT_20261009.json'

def gate(p):
    from scripts.workflow_guard import contract_hash
    t=read(ROOT/'WORKFLOW_TASK.json');idx={a['path']:a['sha256'] for a in t['artifacts']}
    assert t['id']==p['parent_task'] and t['owner_session']==p['owner_session'] and t['status']=='in_progress'
    assert t['contract_sha256']==contract_hash(t) and idx[P.relative_to(ROOT).as_posix()]==sha(P)
    for f,h in p['input_hashes'].items():assert sha(ROOT/f)==h
    for ref in p['native_sources'].values():
        assert sha(ref['path'])==ref['sha256'] and Path(ref['path']).stat().st_size==ref['bytes']
    assert 'onedrive' not in str(Path(p['native_directory']).resolve()).lower()
    return t

def calculate(p):
    minute=read_verified_source(p['native_sources']['minute'])
    journal=read_verified_source(p['native_sources']['daily'])
    assert minute['schema']=='regis.current_minute_raw.v1' and minute['fields']==FIELDS
    assert minute['code']==CODE and minute['date']==DATE and minute['frequency']=='5' and minute['adjustflag']=='3'
    rows=minute['rows'];dailyrows=journal['observations']['stock']['rows']
    matches=[r for r in dailyrows if r['code']==CODE and r['date']==DATE]
    assert len(matches)==1
    daily=matches[0];comparison=analyse(rows,daily)
    previous=read(ROOT/'research/CURRENT_MINUTE_SOURCE_RESULT_20261009.json')
    assert previous['state']=='current_minute_source_failure_preserved'
    assert previous['diagnostics']['validation_label']=='minute_daily_disagreement'
    assert comparison==previous['diagnostics']['summary']
    parsed=[values(r) for r in rows];d=values(daily)
    amount=sum(r['amount'] for r in parsed);delta=amount-d['amount']
    # Independent integer scaling is a representation cross-check, not a new
    # tolerance or a certification of provider rounding mode.
    rawamount=[r['amount'] for r in parsed]+[d['amount']]
    scaled_integral=all((v*10000)==(v*10000).to_integral_value() for v in rawamount)
    integer_agree=None
    if scaled_integral:
        delta_integer=sum(int(r['amount']*10000) for r in parsed)-int(d['amount']*10000)
        integer_agree=Decimal(delta_integer)/10000==delta
        assert integer_agree
    inconsistent=0;zero_volume_nonzero_amount=0
    for r in parsed:
        if r['volume']==0:
            zero_volume_nonzero_amount+=int(r['amount']!=0)
        else:
            vwap=r['amount']/r['volume']
            eps=Decimal('0.00005')+Decimal('0.00005')/r['volume']
            inconsistent+=int(vwap<r['low']-eps or vwap>r['high']+eps)
    return {'rows':len(rows),'comparison_reproduces_original':True,
        'original_comparison':comparison,'minute_sum_minus_daily_CNY':str(delta),
        'absolute_gap_CNY':str(abs(delta)),
        'doc4decimals_nearest_rounding_conditional_bound_CNY':'0.00245',
        'gap_exceeds_that_conditional_bound':abs(delta)>Decimal('0.00245'),
        'original_amount_tolerance_CNY':'0.0025','original_amount_check_remains_failed':not comparison['daily_agreement']['amount_sum'],
        'all_reported_amounts_fit_4decimal_scaled_integers':scaled_integral,
        'integer_scaled_difference_agrees':integer_agree,
        'all_minute_amounts_are_numerically_integral':all(r['amount']==r['amount'].to_integral_value() for r in parsed),
        'interval_amount_volume_OHLC_inconsistent_rows_conditional_on_doc_rounding':inconsistent,
        'zero_volume_nonzero_amount_rows':zero_volume_nonzero_amount,
        'actual_provider_rounding_mode_or_interval_definition_verified':False,
        'minute_amount_field_qualified':False,'whole_source_L1_qualified':False,
        'minute_price_volume_facts_only':'Original fixed-day observed code/date/raw/OHLC/volume and labels passed; no completeness/PIT/fills authority.',
        'source_receipt_utc':minute['observed_at_utc'],
        'model_admitted':False,'orders_enabled':False,'new_formulas':0,'economic_runs':0}

def run(p):
    t=gate(p);ph,ih=sha(P),sha(__file__)
    out=Path(p['native_directory'])/'calculation';out.mkdir(exist_ok=False)
    write(out/'protocol.json',p,exclusive=True);write(out/'frozen_task.json',t,exclusive=True)
    write(out/'dispatched.json',{'claimed_utc':clock(),'calculation_batches_max':1},exclusive=True)
    r={'schema':'regis.minute_amount_diagnosis_result.v1','started_utc':clock(),
       'local_calculation_dispatches':1,'fresh_source_queries':0,'SDK_executions':0,
       'source_retries':0,'tolerance_changes':0,'new_formulas':0,'economic_runs':0,
       'raw_rows_or_prices_exported':False,'derived_gap_summary_exported':True,
       'failure_type':None,'minute_amount_qualified':False,'model_admitted':False,'full_goal_complete':False}
    phase='retained_discrepancy_calculation'
    try:
        r['summary']=calculate(p);phase='post_calculation_binding';gate(p)
        r['state']='retained_amount_discrepancy_measured_original_failure_not_repaired'
    except Exception as exc:
        r.update(state='amount_discrepancy_diagnosis_failure_preserved',failure_type=type(exc).__name__,failure_phase=phase)
        if isinstance(exc,ValueError):r['validation_label']=str(exc)[:180]
        if phase=='post_calculation_binding':r['summary_invalidated_by_binding_failure']=True
    r.update(ended_utc=clock(),contract_sha256=t['contract_sha256'],protocol_sha256=ph,implementation_sha256=ih,
             source_known_at_role='actual receipt preserved, not historical bar publication')
    write(O,r);write(out/'terminal.json',r,exclusive=True);print(json.dumps(r))
    return 0 if r['failure_type'] is None else 1

if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--validate-plan',action='store_true');args=ap.parse_args();p=read(P)
    if args.validate_plan:gate(p);print(json.dumps({'frozen_plan_checked':True,'calculation_runs':0,'source_queries':0}))
    else:raise SystemExit(run(p))
