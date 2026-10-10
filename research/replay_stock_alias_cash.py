"""Native-only training cash replay; publish aggregates, never quote arrays."""
import argparse,json,time
from pathlib import Path
from collections import Counter
from functools import lru_cache
import inspect_short_rq_full as base
from recover_rq_etf_history import safe_dataset
from read_instruments_static import parse,date_int
from inspect_rq_stock_history import event_dates
from audit_stock_action_roles import normalize
from stock_event_cash_book import replay,UnresolvedAction,dec,D
from stock_event_dependencies import potential_split_dependencies
from stock_reversal_alias_intents import identifier_bounds
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/STOCK_ALIAS_CASH_PROTOCOL_20261003.json'
RESULT=ROOT/'research/STOCK_ALIAS_CASH_RESULT_20261003.json'
def check_inputs(spec):
    for p,h in spec['preserved_original_native_hashes'].items():
        if base.digest(p)!=h:raise ValueError('preserved original native ledger changed')
    for p,h in spec['input_hashes'].items():
        if base.digest(ROOT/p)!=h:raise ValueError('frozen cash input changed')
    cap=json.loads((ROOT/spec['acquisition_receipt']).read_text(encoding='utf-8'));local=Path(cap['raw_directory'])
    for item in cap['retained']:
        if base.digest(local/item['name'])!=item['sha256']:raise ValueError('native cash input changed')
    bindings=json.loads((ROOT/spec['notice_bindings']).read_text(encoding='utf-8'))
    for e in bindings['events']:
        if base.digest(ROOT/e['native_pdf'])!=e['pdf_sha256']:raise ValueError('issuer PDF changed')
        for page,h in e['page_png_sha256'].items():
            png=e.get('page_png_paths',{}).get(page,str(Path(e['native_pdf']).parent/(e['instrument_id'][:6]+'-p'+page+'.png')))
            if base.digest(ROOT/png)!=h:raise ValueError('issuer visual evidence changed')
    event_binding=json.loads((ROOT/spec['event_bindings']).read_text(encoding='utf-8'))
    for source in event_binding['sources']:
        if base.digest(ROOT/source['native_pdf'])!=source['sha256']:raise ValueError('rights/merger original changed')
        for page,h in source['page_png_sha256'].items():
            if base.digest(ROOT/source['page_png_paths'][page])!=h:raise ValueError('rights/merger visual evidence changed')
    return local,bindings
def run(spec):
    started=base.utc();start=time.monotonic();peak=0
    def checkpoint():
        nonlocal peak
        peak=max(peak,base.peak_rss())
        if time.monotonic()-start>spec['limits']['seconds'] or peak>spec['limits']['peak_rss_bytes']:raise ValueError('cash replay resource bound')
    local,bindings=check_inputs(spec);h5py,np=base.load_dependencies();checkpoint()
    alias_spec=json.loads((ROOT/spec['alias_interval_protocol']).read_text(encoding='utf-8'));bykey={k:p for p in alias_spec['pairs'] for k in [p['old'],p['new']]}
    r=json.loads((ROOT/spec['intent_result']).read_text(encoding='utf-8'));native=Path(r['local_details']['path'])
    if base.digest(native)!=r['local_details']['sha256']:raise ValueError('native intent lineage changed')
    intents=json.loads(native.read_text(encoding='utf-8'))['intents'];selected=sorted({x['instrument_id'] for x in intents})
    event_binding=json.loads((ROOT/spec['event_bindings']).read_text(encoding='utf-8'))
    events_by_key={}
    for e in event_binding['events']:events_by_key.setdefault(e['instrument_id'],[]).append(e)
    # A successor is needed to preserve an already held asset. It does not
    # enter the candidate ranking or create a new signal.
    selected=sorted(set(selected)|{e['successor_id'] for e in event_binding['events'] if e['type']=='automatic_share_transformation'})
    if len(selected)>spec['limits']['keys']:raise ValueError('cash key bound')

    source_calendar=[int(d) for d in base.read_calendar(local/'trading_dates.npy',np)];calendar=[d for d in source_calendar if spec['start_date']<=d<=spec['end_date']]
    ids,_=parse((local/'instruments.pk').read_bytes(),checkpoint=checkpoint);ids={x['order_book_id']:x for x in ids if x.get('type')=='CS'}
    details=json.loads((local/'stock_action_role_audit.json').read_text(encoding='utf-8'))
    action_result=json.loads((ROOT/spec['action_role_result']).read_text(encoding='utf-8'))
    if base.digest(local/'stock_action_role_audit.json')!=action_result['local_details']['sha256']:raise ValueError('native action audit changed')
    known={(x['instrument_id'],x['ex_date']):x for x in bindings['events']};matched=[];actions={}
    with h5py.File(local/'dividends.h5','r') as dividends,h5py.File(local/'split_factor.h5','r') as splits:
        for key in selected:
            checkpoint();cash=[];split=[]
            if key in dividends:
                rows=safe_dataset(dividends,key,h5py,np)[:];ann=normalize(rows['announcement_date'],np,True);book=normalize(rows['book_closure_date'],np);ex=normalize(rows['ex_dividend_date'],np);pay=normalize(rows['payable_date'],np);freq=Counter(map(int,ex))
                for j,row in enumerate(rows):
                    gross=dec(float(row['dividend_cash_before_tax']))/dec(float(row['round_lot']))
                    valid=bool(ann[j]>0 and ann[j]<=book[j]<ex[j]<=pay[j] and freq[int(ex[j])]==1 and gross>=0)
                    cash.append({'record_date':int(book[j]),'ex_date':int(ex[j]),'pay_date':int(pay[j]),'gross_cash_per_share':str(gross),'valid':valid})
            if key in splits:
                rows=safe_dataset(splits,key,h5py,np)[:];ex=normalize(rows['ex_date'],np)
                for j,row in enumerate(rows):
                    e={'ex_date':int(ex[j]),'ratio':str(dec(float(row['split_factor']))),'record_date':None,'credit_date':None,'trading_date':None};b=known.get((key,e['ex_date']))
                    if b:
                        if abs(dec(e['ratio'])-dec(b['post_split_total_share_ratio']))>D('0.000001'):raise ValueError('native split ratio contradicts original issuer')
                        group=[x for x in cash if x['ex_date']==e['ex_date']];expected_value=b['cash_CNY_per_pre_split_share'];cash_match=None
                        if expected_value is not None:
                            expected=dec(expected_value)
                            cash_match=not ((expected==0 and group) or (expected>0 and (len(group)!=1 or abs(dec(group[0]['gross_cash_per_share'])-expected)>D('0.000001') or group[0]['record_date']!=b['record_date'] or group[0]['pay_date']!=b['cash_pay_date'])))
                            if not cash_match:raise ValueError('native cash/date contradicts original issuer')
                        e.update(ratio=b['post_split_total_share_ratio'],record_date=b['record_date'],credit_date=b['share_credit_date'],trading_date=b['unrestricted_share_trading_start_date'],bonus_share_per_pre_share=b['bonus_share_per_pre_share'],distribution_type_uncertain=b['distribution_type_uncertain'])
                        matched.append({'instrument_id':key,'ex_date':e['ex_date'],'native_split_ratio_match':True,'native_cash_and_dates_match':cash_match,'visual_fields_verified':bool(b['visually_checked_pages']),'cash_amount_original_verified':expected_value is not None})
                    split.append(e)
            actions[key]={'cash':cash,'split':split,'unknown_factor_dates':details[key]['factor_change_dates_without_cash_or_split_record'],'delist_date':date_int(ids[key].get('de_listed_date'))}
    for key,evs in events_by_key.items():
        if key not in actions:raise ValueError('bound event identity missing')
        for e in evs:
            if e['type']=='rights_issue':
                if e['ex_date'] not in actions[key]['unknown_factor_dates']:raise ValueError('bound rights ex not in original unknown factor census')
                actions[key].setdefault('rights',[]).append(e)
                actions[key]['unknown_factor_dates'].remove(e['ex_date'])
            elif e['type']=='automatic_share_transformation':
                if actions[key]['delist_date']!=e['effective_date'] or e['successor_id'] not in ids or date_int(ids[e['successor_id']].get('listed_date'))!=e['trading_date']:raise ValueError('merger effective/successor identity contradicts original')
                actions[key].setdefault('transform',[]).append(e);actions[key]['delist_date']=None
            else:raise ValueError('unknown bound action type')
    if len(matched)!=len(known):raise ValueError('not all bound issuer events matched native actions')

    pricefields=['open','close','high','low','volume','limit_up','limit_down']
    summaries=[];native_cases={};diagnostic_keys=set()
    with h5py.File(local/'stocks.h5','r',rdcc_nbytes=1048576) as prices,h5py.File(local/'st_stock_days.h5','r') as st,h5py.File(local/'suspended_days.h5','r') as halts:
        @lru_cache(maxsize=96)
        def stock(key):
            checkpoint();ds=safe_dataset(prices,key,h5py,np);dates=normalize(ds.fields('datetime')[:],np);a=int(np.searchsorted(dates,spec['start_date']));b=int(np.searchsorted(dates,spec['end_date'],side='right'))
            rows=ds.fields(pricefields)[a:b];d=dates[a:b];return {int(x):i for i,x in enumerate(d)},rows
        @lru_cache(maxsize=3000)
        def states(key):
            ss,si=event_dates(safe_dataset(st,key,h5py,np)[:],np) if key in st else ([],0);hs,hi=event_dates(safe_dataset(halts,key,h5py,np)[:],np) if key in halts else ([],0)
            if si or hi:raise ValueError('cash state date invalid')
            return set(map(int,ss)),set(map(int,hs))
        def bar(key,day):
            idx,rows=stock(key);i=idx.get(day)
            if i is None:return None
            row=rows[i]
            if any(not np.isfinite(row[f]) for f in pricefields):raise ValueError('cash bar nonfinite')
            s,h=states(key);identity=ids[key];listed,delisted=identifier_bounds(identity,bykey)
            return {**{f:str(float(row[f])) for f in pricefields},'st':day in s,'halted':day in h,'outside_lifetime':listed is None or day<listed or (delisted is not None and day>=delisted)}
        for scenario in spec['scenarios']:
            checkpoint()
            try:
                summary,detail=replay(calendar,intents,bar,actions,scenario,initial=spec['initial_CNY'],lot=spec['lot_size'],name_cap=spec['name_allocation_target'],gross_cap=spec['gross_allocation_target'],max_positions=spec['max_positions'],tax=spec['fixed_dividend_tax_reserve_rate'],rights_policy=spec['rights_issue_election'])
                potential=potential_split_dependencies(source_calendar,detail['held_dates'],actions)
                if potential:
                    # This is a source-dependency lookback, not a candidate
                    # filter or a claim that record dates are within20 days.
                    financial_keys=['terminal_marked_nav_CNY','terminal_cash_CNY','maximum_marked_drawdown','maximum_close_gross_fraction','maximum_close_name_fraction','total_fees_CNY','accrued_net_dividend_CNY','paid_net_dividend_CNY','annual_marked_results']
                    native_cases[scenario['id']]={'summary_before_dependency_review':summary,'details':detail,'potential_unresolved_actions':potential}
                    summary={k:v for k,v in summary.items() if k not in financial_keys};summary.update(state='cash_book_computed_potential_share_entitlements_unresolved',financial_totals_published=False,potential_unresolved_actions=potential)
                else:
                    summary.update(financial_totals_published=True,potential_unresolved_actions=[],full_historical_action_coverage_certified=False);native_cases[scenario['id']]={'summary':summary,'details':detail}
                summaries.append(summary)
            except UnresolvedAction as e:
                checkpoint_state=e.native_checkpoint
                summaries.append({'id':scenario['id'],'state':'unresolved_actual_account_action','dependency':e.dependency,'counts_before_failure':checkpoint_state['counts'],'completed_sessions_before_failure':len(checkpoint_state['daily_aggregate_ledger']),'financial_totals_published':False,'model_admitted':False});native_cases[scenario['id']]={'failure_dependency':e.dependency,'native_partial_account_checkpoint':checkpoint_state}
    checkpoint();check_inputs(spec);checkpoint()
    name=spec['native_result_filename']
    if Path(name).name!=name or not name.endswith('.json'):raise ValueError('native output filename invalid')
    output=local/name
    if output.exists():raise ValueError('native cash output already exists; preserve attempts')
    output.write_text(json.dumps(native_cases,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    result={'schema':'regis.stock.daily_cash_result.v1','state':'conditional_cash_replay_attempted','started_utc':started,'ended_utc':base.utc(),'elapsed_seconds':round(time.monotonic()-start,3),'peak_rss_bytes':peak,'protocol_sha256':base.digest(PROTOCOL),'implementation_sha256':base.digest(__file__),'core_sha256':base.digest(ROOT/'research/stock_event_cash_book.py'),'matched_original_issuer_split_events':matched,'scenarios':summaries,'local_details':{'path':str(output),'sha256':base.digest(output),'contains_quote_arrays':False,'contains_native_only_account_money_and_quantities':True},'OOS_computed':False,'original_M24_changed_or_enabled':False,'model_admitted':False,'full_goal_complete':False,'live_orders_or_alerts_generated':False}
    result.update(distinct_corrected_reference_intents=True,identifier_valid_from_source_applied_to_bars=True,original_intent_result_sha256=base.digest(ROOT/'research/STOCK_REVERSAL_INTENT_RESULT_20261003.json'),corrected_intent_result_sha256=base.digest(ROOT/spec['intent_result']),cash_engine_unchanged=True,source_9limit_down_gaps_or_2extra_cash_records_repaired=False,old_identifier_hold_conversion_implicitly_enabled=False)
    RESULT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n');print(json.dumps({**{k:result[k] for k in ['state','elapsed_seconds','peak_rss_bytes','scenarios','OOS_computed','model_admitted']},'matched_original_issuer_event_count':len(matched),'visual_event_count':sum(x['visual_fields_verified'] for x in matched),'cash_original_match_count':sum(x['native_cash_and_dates_match'] is True for x in matched)},ensure_ascii=False))
def verify(spec):
    r=json.loads(RESULT.read_text(encoding='utf-8'));check_inputs(spec)
    if r['state']!='conditional_cash_replay_attempted' or r['protocol_sha256']!=base.digest(PROTOCOL) or r['implementation_sha256']!=base.digest(__file__) or r['core_sha256']!=base.digest(ROOT/'research/stock_event_cash_book.py'):raise ValueError('cash evidence changed')
    if base.digest(r['local_details']['path'])!=r['local_details']['sha256']:raise ValueError('native cash ledger changed')
    if len(r['scenarios'])!=4 or any(r[k] for k in ['OOS_computed','original_M24_changed_or_enabled','model_admitted','full_goal_complete','live_orders_or_alerts_generated']):raise ValueError('cash scope changed')
    if not all(r.get(k) is True for k in ['distinct_corrected_reference_intents','identifier_valid_from_source_applied_to_bars','cash_engine_unchanged']):raise ValueError('distinct cash identity scope changed')
    if r['source_9limit_down_gaps_or_2extra_cash_records_repaired'] or r['old_identifier_hold_conversion_implicitly_enabled']:raise ValueError('unbound source repaired')
    if r['corrected_intent_result_sha256']!=base.digest(ROOT/spec['intent_result']) or r['original_intent_result_sha256']!=base.digest(ROOT/'research/STOCK_REVERSAL_INTENT_RESULT_20261003.json'):raise ValueError('intent lineage changed')
    print(json.dumps({'cash_attempt_verified':True,'scenarios':4,'model_admitted':False,'new_corrected_identifier_cash_replay':True}))
def repair_dependencies(spec):
    """Reuse the already computed account; repair date audit, no quote reads."""
    started=base.utc();start=time.monotonic();local,bindings=check_inputs(spec)
    lineage=spec['date_only_repair'];old_spec=json.loads((ROOT/lineage['original_protocol']).read_text(encoding='utf-8'));old=json.loads((ROOT/lineage['original_result']).read_text(encoding='utf-8'))
    if base.digest(ROOT/lineage['original_protocol'])!=lineage['original_protocol_sha256'] or base.digest(ROOT/lineage['original_result'])!=lineage['original_result_sha256']:raise ValueError('original event account evidence changed')
    allowed={'research/replay_stock_event_cash.py','research/test_stock_event_cash_book.py','research/stock_event_dependencies.py'}
    if any(spec['input_hashes'].get(p)!=h for p,h in old_spec['input_hashes'].items() if p not in allowed):raise ValueError('economic inputs changed during date-only repair')
    if old['core_sha256']!=base.digest(ROOT/'research/stock_event_cash_book.py') or old_spec['scenarios']!=spec['scenarios'] or old_spec['rights_issue_election']!=spec['rights_issue_election']:raise ValueError('account rule changed during date-only repair')
    if base.digest(old['local_details']['path'])!=old['local_details']['sha256']:raise ValueError('original native account changed')
    prior=json.loads(Path(old['local_details']['path']).read_text(encoding='utf-8'));h5py,np=base.load_dependencies();calendar=[int(d) for d in base.read_calendar(local/'trading_dates.npy',np)]
    known={(x['instrument_id'],x['ex_date']):x['record_date'] for x in bindings['events']};cases={};summaries=[];delta=[]
    financial_keys=['terminal_marked_nav_CNY','terminal_cash_CNY','maximum_marked_drawdown','maximum_close_gross_fraction','maximum_close_name_fraction','total_fees_CNY','accrued_net_dividend_CNY','paid_net_dividend_CNY','annual_marked_results']
    with h5py.File(local/'split_factor.h5','r') as source:
        for previous in old['scenarios']:
            ident=previous['id'];case=prior[ident]
            if 'summary_before_dependency_review' not in case:
                cases[ident]=case;summaries.append(previous);continue
            details=case['details'];actions={}
            for key in details['held_dates']:
                ex=normalize(safe_dataset(source,key,h5py,np)[:]['ex_date'],np) if key in source else []
                actions[key]={'split':[{'ex_date':int(d),'record_date':known.get((key,int(d)))} for d in ex]}
            potential=potential_split_dependencies(calendar,details['held_dates'],actions)
            delta.append({'id':ident,'prior_potential_count':len(case['potential_unresolved_actions']),'repaired_potential_count':len(potential),'removed_far_future_dates':[e for e in case['potential_unresolved_actions'] if e not in potential]})
            full=case['summary_before_dependency_review']
            if potential:
                summary={k:v for k,v in full.items() if k not in financial_keys};summary.update(state='cash_book_computed_potential_share_entitlements_unresolved',financial_totals_published=False,potential_unresolved_actions=potential)
                cases[ident]={'summary_before_dependency_review':full,'details':details,'potential_unresolved_actions':potential}
            else:
                summary=dict(full,financial_totals_published=True,potential_unresolved_actions=[],full_historical_action_coverage_certified=False);cases[ident]={'summary':summary,'details':details}
            summaries.append(summary)
    check_inputs(spec);name=spec['native_result_filename']
    if Path(name).name!=name or not name.endswith('.json') or (local/name).exists():raise ValueError('date-repair native output invalid or exists')
    output=local/name;output.write_text(json.dumps(cases,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    result=dict(old,started_utc=started,ended_utc=base.utc(),elapsed_seconds=round(time.monotonic()-start,3),peak_rss_bytes=base.peak_rss(),protocol_sha256=base.digest(PROTOCOL),implementation_sha256=base.digest(__file__),scenarios=summaries,local_details={'path':str(output),'sha256':base.digest(output),'contains_quote_arrays':False,'contains_native_only_account_money_and_quantities':True},date_only_repair={'new_economic_runs':0,'quote_arrays_read':False,'original_result_sha256':lineage['original_result_sha256'],'original_native':old['local_details'],'deltas':delta},original_economic_run_elapsed_seconds=old['elapsed_seconds'])
    RESULT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({'state':result['state'],'new_economic_runs':0,'quote_arrays_read':False,'elapsed_seconds':result['elapsed_seconds'],'deltas':delta,'scenario_states':[{'id':s['id'],'state':s['state'],'financial_totals_published':s['financial_totals_published'],'dependency':s.get('dependency'),'potential_count':len(s.get('potential_unresolved_actions',[]))} for s in summaries]},ensure_ascii=False))
if __name__=='__main__':
    p=argparse.ArgumentParser();g=p.add_mutually_exclusive_group(required=True);g.add_argument('--run',action='store_true');g.add_argument('--verify',action='store_true');a=p.parse_args();spec=json.loads(PROTOCOL.read_text(encoding='utf-8'))
    if a.verify:verify(spec)
    else:
        if json.loads(RESULT.read_text(encoding='utf-8'))['state']!='not_run':raise ValueError('Cash attempt already recorded; archive and refreeze before repair')
        started=base.utc()
        try:
            run(spec)
        except Exception as e:
            RESULT.write_text(json.dumps({'schema':'regis.stock.daily_cash_result.v1','state':'failed','started_utc':started,'ended_utc':base.utc(),'protocol_sha256':base.digest(PROTOCOL),'failure':{'type':type(e).__name__,'reason':str(e)},'model_admitted':False,'OOS_computed':False},indent=2)+'\n',encoding='utf-8');print(json.dumps({'state':'failed','type':type(e).__name__,'reason':str(e)}));raise SystemExit(1)
