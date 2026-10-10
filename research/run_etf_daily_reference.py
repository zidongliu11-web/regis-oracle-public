"""Fixed cash/ETF historical accounting reference, conditional on declared models.

Reads retained native data locally. Exports aggregates and quality comparisons,
never prices. This does not implement the original minute trading experiment.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path

import inspect_short_rq_full as base
from recover_rq_etf_history import safe_dataset

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / 'research/ETF_DAILY_REFERENCE_PROTOCOL_20261002.json'
RESULT = ROOT / 'research/ETF_DAILY_REFERENCE_RESULT_20261002.json'
D = lambda x: Decimal(str(x))
CENT = Decimal('0.01')
TICK = Decimal('0.001')

def money(x):
    return x.quantize(CENT, rounding=ROUND_HALF_UP)
def normdate(x):
    n=int(x)
    return n//1000000 if n>100000000 else n
def sha(path):
    return base.digest(path)
def write(value):
    RESULT.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')

def load(spec):
    for item in spec['source_basis']:
        if item.get('native_path') and sha(ROOT/item['native_path'])!=item['sha256']:
            raise ValueError('Declared source document changed')
    receipt=json.loads((ROOT/spec['acquisition_receipt']).read_text(encoding='utf-8'))
    if receipt['state']!='captured' or not receipt['history_inspection']['price_structural_passed']:
        raise ValueError('Actual history acquisition not structurally complete')
    if sha(ROOT/spec['acquisition_receipt'])!=spec['acquisition_receipt_sha256']:
        raise ValueError('Acquisition version mismatch')
    local=Path(receipt['raw_directory'])
    for item in receipt['retained']:
        if sha(local/item['name'])!=item['sha256']:
            raise ValueError('Retained input changed')
    h5py,np=base.load_dependencies()
    key=spec['order_book_id']
    output={}
    for member in ['funds.h5','dividends.h5','split_factor.h5']:
        with h5py.File(local/member,'r',rdcc_nbytes=1048576) as f:
            ds=safe_dataset(f,key,h5py,np)
            output[member]=ds[:]
    bars=output['funds.h5']
    dates=[normdate(x) for x in bars['datetime']]
    selected=[i for i,x in enumerate(dates) if spec['start_date']<=x<=spec['end_date']]
    bars=bars[selected];dates=[dates[i] for i in selected]
    if len(dates)!=spec['expected_sessions'] or dates!=sorted(set(dates)):
        raise ValueError('Frozen interval coverage mismatch')
    if any(spec['start_date']<=normdate(x)<=spec['end_date'] for x in output['split_factor.h5']['ex_date']):
        raise ValueError('In-interval split requires separate position adjustment')
    dividends=[]
    for row in output['dividends.h5']:
        if D(row['round_lot'])<=0 or D(row['dividend_cash_before_tax'])<0:
            raise ValueError('Invalid dividend units')
        item={'record_date':normdate(row['book_closure_date']),
              'ex_date':normdate(row['ex_dividend_date']),
              'pay_date':normdate(row['payable_date']),
              'announcement_date':normdate(row['announcement_date']),
              'cash_per_unit':D(row['dividend_cash_before_tax'])/D(row['round_lot'])}
        if not item['announcement_date']<=item['record_date']<item['ex_date']<=item['pay_date']:
            raise ValueError('Invalid dividend chronology')
        if spec['start_date']<=item['record_date']<=spec['end_date']:
            dividends.append(item)
    if len(dividends)!=spec['expected_interval_dividends']:
        raise ValueError('Frozen dividend count mismatch')
    return bars,dates,dividends

def reconcile(dividends,spec):
    census=json.loads((ROOT/spec['issuer_census']).read_text(encoding='utf-8'))
    if sha(ROOT/spec['issuer_census'])!=spec['issuer_census_sha256']:
        raise ValueError('Issuer census version mismatch')
    known={int(e['issuer_table_record_date'].replace('-','')):e for e in census['events']}
    result=[]
    for e in dividends:
        row=known.get(e['record_date'])
        if row is None:
            result.append({'record_date':e['record_date'],'issuer_census_row_present':False})
            continue
        cash=D(row['derived_CNY_per_fund_unit'])
        pay=int(row['issuer_table_pay_or_reinvestment_date'].replace('-',''))
        ex=row.get('candidate_ex_date')
        result.append({'record_date':e['record_date'],'issuer_census_row_present':True,
                       'cash_per_unit_match':abs(cash-e['cash_per_unit'])<=D(spec['cash_comparison_tolerance_CNY']),
                       'pay_date_match':pay==e['pay_date'],
                       'documented_ex_date_present':bool(ex),
                       'ex_date_match':int(ex.replace('-',''))==e['ex_date'] if ex else None})
    return result

def run_scenario(s,bars,dates,dividends,spec):
    capital=D(spec['initial_CNY']);lot=int(spec['lot_size'])
    entry_index=s['entry_session_index'];entry_date=dates[entry_index]
    rate=D(s['all_in_fee_rate']);slip=D(s['slippage_rate']);minimum=D(s['minimum_fee_CNY'])
    if capital<=0 or lot!=100 or not 0<=D(s['fill_fraction'])<=1 or not 0<=rate<1 or not 0<=slip<1 or minimum<0:
        raise ValueError('Invalid capital/fill/cost model')
    raw_entry=D(bars[s['entry_price_field']][entry_index])
    entry=(raw_entry*(1+slip)/TICK).to_integral_value(rounding=ROUND_CEILING)*TICK
    fee=lambda gross:money(max(minimum,gross*rate)) if gross else D(0)
    target=int((capital/(entry*lot)).to_integral_value(rounding=ROUND_FLOOR))*lot
    while target and money(entry*target)+fee(money(entry*target))>capital:
        target-=lot
    qty=int((D(target)*D(s['fill_fraction'])/lot).to_integral_value(rounding=ROUND_FLOOR))*lot
    cost=money(entry*qty);entry_fee=fee(cost);cash=capital-cost-entry_fee
    if cash<0 or qty%lot:
        raise ValueError('Cash/lot constraint violation')
    entitlement_events=[e for e in dividends if entry_date<=e['record_date']<=dates[-1]]
    nav=[];cash_paid=D(0);fee_sum=entry_fee
    for i,date in enumerate(dates):
        if i<entry_index:
            nav.append(capital);continue
        payable=sum((money(e['cash_per_unit']*qty) for e in entitlement_events if e['pay_date']<=date),D(0))
        receivable=sum((money(e['cash_per_unit']*qty) for e in entitlement_events if e['ex_date']<=date<e['pay_date']),D(0))
        value=cash+payable+receivable+D(bars['close'][i])*qty
        if i==len(dates)-1 and qty:
            if date<=entry_date:
                raise ValueError('T+1 terminal sale constraint violation')
            exit_price=(D(bars['close'][i])*(1-slip)/TICK).to_integral_value(rounding=ROUND_FLOOR)*TICK
            gross=money(exit_price*qty);exit_fee=fee(gross);fee_sum+=exit_fee
            value=cash+payable+receivable+gross-exit_fee
        nav.append(money(value));cash_paid=payable
    peak=capital;drawdown=D(0)
    for value in nav:
        peak=max(peak,value);drawdown=max(drawdown,1-value/peak)
    years=(datetime.strptime(str(dates[-1]),'%Y%m%d')-datetime.strptime(str(dates[0]),'%Y%m%d')).days/365.25
    annual=[];previous=capital
    for year in range(dates[0]//10000,dates[-1]//10000+1):
        vals=[nav[i] for i,date in enumerate(dates) if date//10000==year]
        if vals:
            annual.append({'year':year,'return':float(vals[-1]/previous-1),'ending_CNY':str(vals[-1])})
            previous=vals[-1]
    return {'scenario_id':s['id'],'entry_date':entry_date,'terminal_date':dates[-1],
            'initial_CNY':str(capital),'filled_fund_units':qty,'cancelled_fund_units':target-qty,
            'fill_fraction_model':s['fill_fraction'],'book_shared_cash_violation':False,
            'entry_uninvested_CNY':str(cash),'model_dividend_cash_paid_CNY':str(cash_paid),
            'model_transaction_fees_CNY':str(fee_sum),'terminal_CNY':str(nav[-1]),
            'total_return':float(nav[-1]/capital-1),'CAGR':float((float(nav[-1]/capital))**(1/years)-1),
            'max_drawdown_on_daily_marks':float(drawdown),'annual_results':annual,
            'executed_actual_orders':0,'modeled_entry_fills':int(qty>0),'modeled_terminal_sales':int(qty>0),
            'not_actual_account_or_minute_execution':True}

def calculate(spec):
    bars,dates,dividends=load(spec)
    comparisons=reconcile(dividends,spec)
    if any(e.get('issuer_census_row_present') is not True or e.get('cash_per_unit_match') is not True or
           e.get('pay_date_match') is not True or e.get('ex_date_match') is False for e in comparisons):
        raise ValueError('Issuer/provider action conflict; do not silently compute a total-return reference')
    result={'schema':'regis.etf.daily_reference_result.v1','state':'computed_conditional_reference',
            'started_utc':datetime.now(timezone.utc).isoformat(),
            'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),
            'acquisition_receipt_sha256':sha(ROOT/spec['acquisition_receipt']),
            'price_sessions':len(dates),'first_date':dates[0],'last_date':dates[-1],
            'interval_dividend_events':len(dividends),'issuer_comparisons':comparisons,
            'cash_control':{'initial_CNY':spec['initial_CNY'],'terminal_CNY':spec['initial_CNY'],
                            'interest_assumed_CNY':'0','total_return':0},
            'scenarios':[run_scenario(s,bars,dates,dividends,spec) for s in spec['scenarios']],
            'source_price_units':'CNY per fund unit per declared producer-consumer convention; source-specific study contract, not signed source attestation',
            'dividend_treatment':'gross before personal tax, record-date holdings determine entitlement, receivable recognized on ex date, scheduled payment model, cash not reinvested',
            'costs_are_frozen_sensitivity_models':True,'price_only_not_original_minutes':True,
            'raw_prices_exported':False,'model_admitted':False,'full_L1_L4_passed':False,
            'conditions_and_limits':spec['conditions_and_limits']}
    result['ended_utc']=datetime.now(timezone.utc).isoformat()
    write(result)
    print(json.dumps({'state':result['state'],'price_sessions':len(dates),'dividend_events':len(dividends),
                      'issuer_matches':sum(e.get('cash_per_unit_match') is True and e.get('pay_date_match') is True for e in comparisons),
                      'scenarios':[{k:s[k] for k in ['scenario_id','terminal_CNY','total_return','max_drawdown_on_daily_marks']}
                                   for s in result['scenarios']], 'model_admitted':False}))

def verify(spec):
    result=json.loads(RESULT.read_text(encoding='utf-8'))
    if result.get('state')!='computed_conditional_reference' or result['protocol_sha256']!=sha(PROTOCOL) or result['implementation_sha256']!=sha(__file__):
        raise ValueError('Reference identity not current')
    if result['raw_prices_exported'] or result['model_admitted'] or result['full_L1_L4_passed']:
        raise ValueError('Reference scope overstated')
    if [s['scenario_id'] for s in result['scenarios']]!=[s['id'] for s in spec['scenarios']]:
        raise ValueError('Scenario set changed')
    if len(result['scenarios'])>4 or not any(s['fill_fraction_model']=='0' and s['total_return']==0 for s in result['scenarios']):
        raise ValueError('Missing no-fill control or exceeded frozen family ceiling')
    bars,dates,dividends=load(spec)
    actual=[run_scenario(s,bars,dates,dividends,spec) for s in spec['scenarios']]
    if actual!=result['scenarios'] or reconcile(dividends,spec)!=result['issuer_comparisons']:
        raise ValueError('Recomputed scenario or action comparison disagrees')
    print(json.dumps({'verified':True,'scenarios':len(result['scenarios']),'price_sessions':result['price_sessions'],
                      'models_and_actual_orders_admitted':False}))

if __name__=='__main__':
    p=argparse.ArgumentParser();m=p.add_mutually_exclusive_group(required=True)
    m.add_argument('--calculate',action='store_true');m.add_argument('--verify',action='store_true')
    args=p.parse_args();spec=json.loads(PROTOCOL.read_text(encoding='utf-8'))
    calculate(spec) if args.calculate else verify(spec)
