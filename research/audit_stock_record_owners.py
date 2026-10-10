"""Reuse native account dates; publish limited conditional aggregate summaries."""
import argparse,json,hashlib,time
from pathlib import Path
from datetime import datetime,timezone
from stock_record_owner_audit import audit_record_owners
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/STOCK_RECORD_OWNER_PROTOCOL_20261003.json'
RESULT=ROOT/'research/STOCK_RECORD_OWNER_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def utc():return datetime.now(timezone.utc).isoformat()
def inputs(spec):
    for p,h in spec['input_hashes'].items():
        if sha(ROOT/p)!=h:raise ValueError('frozen record-audit input changed')
    result=read(ROOT/spec['account_result']);native=Path(result['local_details']['path'])
    if sha(native)!=result['local_details']['sha256']:raise ValueError('native account lineage changed')
    bindings=read(ROOT/spec['record_bindings'])
    for e in bindings['events']:
        if sha(ROOT/e['native_pdf'])!=e['pdf_sha256'] or sha(ROOT/e['native_png'])!=e['png_sha256']:raise ValueError('record source evidence changed')
    return result,native,bindings
def run(spec):
    if read(RESULT)['state']!='not_run':raise ValueError('retain recorded audit; no overwrite')
    start=time.monotonic();started=utc();result,native,bindings=inputs(spec);cases=read(native);out=[]
    financial=['terminal_marked_nav_CNY','terminal_cash_CNY','maximum_marked_drawdown','maximum_close_gross_fraction','maximum_close_name_fraction','total_fees_CNY','accrued_net_dividend_CNY','paid_net_dividend_CNY','annual_marked_results','residual_positions','unpaid_cash_entitlements','uncredited_share_entitlements','counts','actual_action_events']
    for s in result['scenarios']:
        case=cases[s['id']];potential=s.get('potential_unresolved_actions',[])
        row={'id':s['id'],'prior_state':s['state'],'economic_run_reused':True,'new_economic_runs':0,'financial_totals_admitted':False,'model_admitted':False}
        if potential:
            if 'summary_before_dependency_review' not in case:raise ValueError('completed native summary missing')
            checks=audit_record_owners(potential,case['details']['held_dates'],bindings['events'])
            unresolved=[e for e in checks if not e['specific_dependency_resolved_no_owner']]
            row.update(state='specific_record_dependencies_resolved_conditional_aggregate_only' if not unresolved else 'record_ownership_still_unresolved',dependency_checks=checks,unresolved_count=len(unresolved),specific_no_owner_count=len(checks)-len(unresolved))
            if not unresolved:row['conditional_daily_financial_summary']={k:case['summary_before_dependency_review'][k] for k in financial}
        elif 'failure_dependency' in case:
            row.update(state='prior_actual_account_failure_retained',dependency=case['failure_dependency'],conditional_daily_financial_summary=None)
        else:
            row.update(state='prior_cash_counterexample_retained',conditional_daily_financial_summary={k:case['summary'][k] for k in financial})
        out.append(row)
    inputs(spec)
    value={'schema':'regis.stock.record_owner_result.v1','state':'specific_record_owner_audit_computed','started_utc':started,'ended_utc':utc(),'elapsed_seconds':round(time.monotonic()-start,3),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),'core_sha256':sha(ROOT/'research/stock_record_owner_audit.py'),'original_account_result_sha256':sha(ROOT/spec['account_result']),'original_native_account_sha256':sha(native),'native_account_unchanged':True,'quote_arrays_read':False,'new_economic_runs':0,'record_pages_visually_checked':len(bindings['events']),'scenarios':out,'limits':spec['limits'],'original_M24_changed_or_enabled':False,'full_historical_action_coverage_certified':False,'PIT_certified':False,'OOS_computed':False,'model_admitted':False,'full_goal_complete':False}
    RESULT.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({'state':value['state'],'elapsed_seconds':value['elapsed_seconds'],'new_economic_runs':0,'scenarios':[{'id':s['id'],'state':s['state'],'specific_no_owner_count':s.get('specific_no_owner_count'),'unresolved_count':s.get('unresolved_count'),'conditional_aggregate':s.get('conditional_daily_financial_summary'),'dependency':s.get('dependency')} for s in out],'model_admitted':False},ensure_ascii=False))
def verify(spec):
    value=read(RESULT);_,native,_=inputs(spec)
    if value['state']!='specific_record_owner_audit_computed' or value['protocol_sha256']!=sha(PROTOCOL) or value['implementation_sha256']!=sha(__file__) or value['core_sha256']!=sha(ROOT/'research/stock_record_owner_audit.py') or value['original_native_account_sha256']!=sha(native):raise ValueError('record audit evidence changed')
    if value['new_economic_runs'] or value['quote_arrays_read'] or any(value[k] for k in ['original_M24_changed_or_enabled','full_historical_action_coverage_certified','PIT_certified','OOS_computed','model_admitted','full_goal_complete']):raise ValueError('record audit scope changed')
    print(json.dumps({'record_audit_verified':True,'scenarios':len(value['scenarios']),'new_economic_runs':0,'model_admitted':False}))
if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--run',action='store_true');ap.add_argument('--verify',action='store_true');args=ap.parse_args();spec=read(PROTOCOL)
    if args.run==args.verify:raise SystemExit('Select one mode')
    run(spec) if args.run else verify(spec)
