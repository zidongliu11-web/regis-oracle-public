"""Reuse native account dates; publish limited conditional aggregate summaries."""
import argparse,json,hashlib,time
from pathlib import Path
from datetime import datetime,timezone
from stock_record_owner_audit import audit_record_owners
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/STOCK_ALIAS_BOOK_EVIDENCE_PROTOCOL_20261003.json'
RESULT=ROOT/'research/STOCK_ALIAS_BOOK_EVIDENCE_RESULT_20261003.json'
FINANCIAL=['terminal_marked_nav_CNY','terminal_cash_CNY','maximum_marked_drawdown','maximum_close_gross_fraction','maximum_close_name_fraction','total_fees_CNY','accrued_net_dividend_CNY','paid_net_dividend_CNY','annual_marked_results','residual_positions','unpaid_cash_entitlements','uncredited_share_entitlements','counts','actual_action_events']
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
    financial=FINANCIAL
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
    value.update(source_date_evidence=rebuild_dividend_date_evidence(spec),alias_book_diagnostics=alias_book_diagnostics(spec,out,cases),distinct_corrected_account=True)
    RESULT.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({'state':value['state'],'elapsed_seconds':value['elapsed_seconds'],'new_economic_runs':0,'scenarios':[{'id':s['id'],'state':s['state'],'specific_no_owner_count':s.get('specific_no_owner_count'),'unresolved_count':s.get('unresolved_count'),'conditional_aggregate':s.get('conditional_daily_financial_summary'),'dependency':s.get('dependency')} for s in out],'model_admitted':False},ensure_ascii=False))
def verify(spec):
    value=read(RESULT);current,native,bindings=inputs(spec)
    verify_new_owners(spec,value,current,native,bindings)
    if value['state']!='specific_record_owner_audit_computed' or value['protocol_sha256']!=sha(PROTOCOL) or value['implementation_sha256']!=sha(__file__) or value['core_sha256']!=sha(ROOT/'research/stock_record_owner_audit.py') or value['original_native_account_sha256']!=sha(native):raise ValueError('record audit evidence changed')
    if value['new_economic_runs'] or value['quote_arrays_read'] or any(value[k] for k in ['original_M24_changed_or_enabled','full_historical_action_coverage_certified','PIT_certified','OOS_computed','model_admitted','full_goal_complete']):raise ValueError('record audit scope changed')
    print(json.dumps({'record_audit_verified':True,'scenarios':len(value['scenarios']),'new_economic_runs':0,'model_admitted':False}))
"""Native corrected-book diagnostics; no per-name money or prices emitted."""
import io
from decimal import Decimal
from pypdf import PdfReader
import pypdfium2 as pdfium
def rebuild_dividend_date_evidence(spec):
    b=read(ROOT/spec['dividend_date_bindings']);source=read(ROOT/spec['dividend_source_result']);events={e['id']:e for e in source['events']}
    output=[]
    for row in b['events']:
        e=events[row['id']]
        if sha(e['native_PDF'])!=e['pdf_sha256'] or row['pdf_sha256']!=e['pdf_sha256']:raise ValueError('dividend source changed')
        if row['id'].endswith('2016'):
            if not e['dates_independently_bound'] or not all(e['predicates'].values()):raise ValueError('labelled original dates unbound')
            output.append({'id':row['id'],'bound_roles':3,'basis':'preservedE39labellednativePDFpredicates','cash_amount_certified':False})
        else:
            rr=read(row['native_render_receipt'])
            if sha(row['native_render_receipt'])!=row['render_receipt_sha256'] or sha(rr['png'])!=rr['png_sha256'] or rr['source_sha256']!=e['pdf_sha256']:raise ValueError('date table evidence changed')
            doc=pdfium.PdfDocument(e['native_PDF']);pg=doc[rr['page']-1];tp=pg.get_textpage();meta=[]
            for x in rr['date_search_rectangles']:
                search=tp.search(x['date_literal']);boxes=[]
                while True:
                    hit=search.get_next()
                    if hit is None:break
                    index,count=hit;rects=[tp.get_charbox(index+i) for i in range(count)];boxes.append([min(v[0] for v in rects),min(v[1] for v in rects),max(v[2] for v in rects),max(v[3] for v in rects)])
                search.close();meta.append({'date_literal':x['date_literal'],'rectangles':boxes})
            if meta!=rr['date_search_rectangles']:raise ValueError('date cell positions not rebuilt')
            image=pg.render(scale=rr['scale']).to_pil().crop(rr['crop_pixel_rectangle']);buf=io.BytesIO();image.save(buf,format='PNG')
            if buf.getvalue()!=Path(rr['png']).read_bytes():raise ValueError('date table crop not rebuilt')
            text=''.join((PdfReader(e['native_PDF']).pages[0].extract_text() or '').split())
            if not all(x in text for x in ['股权登记日','除权（息）日','现金红利发放日','2017/5/10','2017/5/11']):raise ValueError('date table labels absent')
            if not row['date_table_visually_checked'] or row['record_date']!=20170510 or row['ex_date']!=20170511 or row['pay_date']!=20170511:raise ValueError('visual row role binding changed')
            tp.close();pg.close();doc.close();output.append({'id':row['id'],'bound_roles':3,'basis':'page1 threeAshare tablecells visuallyread/rebuiltPNG/datepositions; firstE39labelpredicatefalse retained','cash_amount_certified':False})
    return output

def alias_book_diagnostics(spec,out,cases):
    source=read(ROOT/spec['account_result']);oldaudit=read(ROOT/spec['prior_owner_audit_result']);oldrows={s['id']:s for s in oldaudit['scenarios']};datebind=read(ROOT/spec['dividend_date_bindings']);gap=[];delta=[]
    for s in out:
        case=cases[s['id']];details=case.get('details',case.get('native_partial_account_checkpoint'));ledger=details['daily_aggregate_ledger'];completed={v['date'] for v in ledger};held=details['held_dates']
        for event in datebind['events']:
            record,ex=event['record_date'],event['ex_date'];complete=record in completed and ex in completed
            rh=record in held.get('601313.XSHG',[]);eh=ex in held.get('601313.XSHG',[])
            gap.append({'scenario':s['id'],'event':event['id'],'old_ID':'601313.XSHG','record_date':record,'ex_date':ex,'dates_reached_by_actual_book':complete,'record_day_pre_exit_held':rh if complete else None,'ex_day_pre_exit_held':eh if complete else None,'specific_no_owner_proved':complete and not rh and not eh,'partial_after_failure_not_proved':not complete,'cash_amount_reconciled':False})
        current=s.get('conditional_daily_financial_summary');prior=oldrows[s['id']].get('conditional_daily_financial_summary')
        row={'scenario':s['id'],'same_four_cost_rules':True,'partial_first_failure_retained':s.get('dependency')==oldrows[s['id']].get('dependency') if not current else None,'financial_difference_available':bool(current and prior)}
        if current and prior:
            row['aggregate_changes']={k:str(Decimal(current[k])-Decimal(prior[k])) for k in ['terminal_marked_nav_CNY','terminal_cash_CNY','maximum_marked_drawdown','maximum_close_gross_fraction','maximum_close_name_fraction','total_fees_CNY','accrued_net_dividend_CNY','paid_net_dividend_CNY']}
            row['count_changes']={k:current['counts'].get(k,0)-prior['counts'].get(k,0) for k in sorted(set(current['counts'])|set(prior['counts'])) if current['counts'].get(k,0)!=prior['counts'].get(k,0)}
        delta.append(row)
    return {'two_missing_old_cash_record_owner_date_checks':gap,'aggregate_changes_from_original_book':delta,'missing_cash_rows_copied_or_repaired':False,'native_individual_money_or_qty_exported':False,'quote_arrays_read':False,'old_and_new_reference_already_observed_training_not_OOS':True}

def verify_new_owners(spec,value,result,native,bindings):
    cases=read(native);rows={s['id']:s for s in value['scenarios']}
    for s in result['scenarios']:
        case=cases[s['id']];row=rows[s['id']];potential=s.get('potential_unresolved_actions',[])
        if potential:
            checks=audit_record_owners(potential,case['details']['held_dates'],bindings['events']);unresolved=[x for x in checks if not x['specific_dependency_resolved_no_owner']]
            if checks!=row['dependency_checks'] or len(unresolved)!=row['unresolved_count'] or len(checks)-len(unresolved)!=row['specific_no_owner_count']:raise ValueError('new native record ownership not rebuilt')
            if not unresolved and row['conditional_daily_financial_summary']!={k:case['summary_before_dependency_review'][k] for k in FINANCIAL}:raise ValueError('new native aggregate not rebuilt')
        elif 'failure_dependency' in case:
            if row['dependency']!=case['failure_dependency'] or row['conditional_daily_financial_summary'] is not None:raise ValueError('partial failure not retained')
        elif row['conditional_daily_financial_summary']!={k:case['summary'][k] for k in FINANCIAL}:raise ValueError('cash-only aggregate not rebuilt')
    if value['source_date_evidence']!=rebuild_dividend_date_evidence(spec) or value['alias_book_diagnostics']!=alias_book_diagnostics(spec,value['scenarios'],cases):raise ValueError('date or aggregate difference not rebuilt')

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--run',action='store_true');ap.add_argument('--verify',action='store_true');args=ap.parse_args();spec=read(PROTOCOL)
    if args.run==args.verify:raise SystemExit('Select one mode')
    run(spec) if args.run else verify(spec)
