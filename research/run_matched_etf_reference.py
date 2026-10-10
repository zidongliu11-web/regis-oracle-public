"""Fixed matched-period baseline; native quote and individual ledger stays local."""
import argparse,json,time
from pathlib import Path
from datetime import datetime,timezone
import inspect_short_rq_full as base
from run_etf_daily_reference import load,reconcile
from matched_etf_cash_book import replay_matched_etf
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/MATCHED_ETF_REFERENCE_PROTOCOL_20261003.json'
RESULT=ROOT/'research/MATCHED_ETF_REFERENCE_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return base.digest(p)
def utc():return datetime.now(timezone.utc).isoformat()
def inputs(spec):
    for p,h in spec['input_hashes'].items():
        if sha(ROOT/p)!=h:raise ValueError('Matched baseline frozen input changed')
    cap=read(ROOT/spec['acquisition_receipt']);local=Path(cap['raw_directory'])
    for e in cap['retained']:
        if sha(local/e['name'])!=e['sha256']:raise ValueError('Native ETF source changed')
    return local
def run(spec):
    if read(RESULT)['state']!='not_run':raise ValueError('Preserve actual matched baseline; no overwrite')
    started=utc();start=time.monotonic();local=inputs(spec);bars,dates,dividends=load(spec);matches=reconcile(dividends,spec)
    if any(not x.get('issuer_census_row_present') or not x.get('cash_per_unit_match') or not x.get('pay_date_match') or x.get('ex_date_match') is False for x in matches):raise ValueError('ETF public census mismatch')
    fields=['open','close','low','high','limit_up','limit_down','volume'];rows=[{field:str(float(row[field])) for field in fields} for row in bars]
    stock=read(ROOT/spec['stock_result']);cases={s['id']:s for s in stock['scenarios']};out=[];native={}
    for scenario in spec['scenarios']:
        summary,ledger=replay_matched_etf(rows,dates,dividends,scenario,initial=spec['initial_CNY'],lot=spec['lot_size']);summary['ETF_action_source_matches']=matches
        prior=cases[scenario['id']];full=prior.get('conditional_daily_financial_summary')
        comparison={'id':scenario['id'],'state':'limited_conditional_comparison_available' if full else 'stock_arm_actual_failure_no_comparison','ETF':summary,'cash_terminal_CNY':spec['initial_CNY'],'stock_failure_dependency':prior.get('dependency'),'stock':full,'financial_totals_admitted':False,'OOS_computed':False,'model_admitted':False}
        if full:
            stocknav=base_decimal(full['terminal_marked_nav_CNY']);etfnav=base_decimal(summary['terminal_marked_nav_CNY']);capital=base_decimal(spec['initial_CNY'])
            comparison.update(stock_minus_ETF_terminal_CNY=str((stocknav-etfnav).quantize(base_decimal('.01'))),stock_minus_ETF_total_return=str((stocknav-etfnav)/capital),stock_net_total_return=str(stocknav/capital-1),ETF_net_total_return=str(etfnav/capital-1),stock_under_cash=stocknav<capital,stock_under_ETF=stocknav<etfnav)
        out.append(comparison);native[scenario['id']]={'daily_aggregate_ledger':ledger,'summary':summary}
    inputs(spec)
    filename=spec['native_result_filename'];dest=local/filename
    if Path(filename).name!=filename or dest.exists():raise ValueError('Native baseline filename or preservation violation')
    dest.write_text(json.dumps(native,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    value={'schema':'regis.matched_etf_reference.v1','state':'fixed_matched_period_ETF_reference_computed','started_utc':started,'ended_utc':utc(),'elapsed_seconds':round(time.monotonic()-start,3),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),'core_sha256':sha(ROOT/'research/matched_etf_cash_book.py'),'start_session':dates[0],'end_session':dates[-1],'session_count':len(dates),'interval_dividends':len(dividends),'scenarios':out,'local_details':{'path':str(dest),'sha256':sha(dest),'contains_quote_arrays':False,'native_only_cash_ledger':True},'limits':spec['limits'],'full_historical_action_coverage_certified':False,'PIT_certified':False,'original_M24_changed_or_enabled':False,'OOS_computed':False,'model_admitted':False,'full_goal_complete':False}
    RESULT.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({'state':value['state'],'elapsed_seconds':value['elapsed_seconds'],'session_count':len(dates),'scenarios':[{'id':s['id'],'state':s['state'],'ETF_terminal_marked_CNY':s['ETF']['terminal_marked_nav_CNY'],'ETF_daily_max_drawdown':s['ETF']['maximum_marked_drawdown'],'stock_minus_ETF_terminal_CNY':s.get('stock_minus_ETF_terminal_CNY'),'stock_failure_dependency':s['stock_failure_dependency']} for s in out],'model_admitted':False},ensure_ascii=False))
def base_decimal(value):
    from decimal import Decimal
    return Decimal(str(value))
def verify(spec):
    r=read(RESULT);inputs(spec)
    if r['state']!='fixed_matched_period_ETF_reference_computed' or r['protocol_sha256']!=sha(PROTOCOL) or r['implementation_sha256']!=sha(__file__) or r['core_sha256']!=sha(ROOT/'research/matched_etf_cash_book.py') or r['local_details']['sha256']!=sha(r['local_details']['path']):raise ValueError('Matched baseline evidence changed')
    if len(r['scenarios'])!=4 or any(r[k] for k in ['full_historical_action_coverage_certified','PIT_certified','original_M24_changed_or_enabled','OOS_computed','model_admitted','full_goal_complete']):raise ValueError('Matched baseline scope changed')
    print(json.dumps({'matched_baseline_verified':True,'scenarios':4,'model_admitted':False}))
if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--run',action='store_true');ap.add_argument('--verify',action='store_true');args=ap.parse_args();spec=read(PROTOCOL)
    if args.run==args.verify:raise SystemExit('Select one mode')
    run(spec) if args.run else verify(spec)
