"""Fixed buy-and-hold marked ETF book under declared daily execution assumptions."""
from decimal import Decimal,ROUND_FLOOR,ROUND_CEILING
from stock_event_cash_book import buy_quantity,fee,dec,D
def replay_matched_etf(bars,dates,dividends,scenario,initial='150000',lot=100,tick='0.001'):
    if dates!=sorted(set(dates)) or len(dates)<2:raise ValueError('ETF calendar invalid')
    if not 0<=dec(scenario['entry_fill_fraction'])<=1 or not 0<=dec(scenario['buy_all_in_fee_rate'])<1 or not 0<=dec(scenario['slippage'])<1 or dec(scenario['minimum_fee'])<0:raise ValueError('ETF model invalid')
    entry_index=1+scenario['entry_delay_sessions'];capital=dec(initial);cash=capital;q=0;charge=D(0);reason='entry_censored';entry_day=None
    if entry_index<len(dates):
        entry_day=dates[entry_index];b=bars[entry_index]
        price=(dec(b['open'])*(1+dec(scenario['slippage']))/dec(tick)).to_integral_value(rounding=ROUND_CEILING)*dec(tick)
        if dec(b['volume'])<=0:reason='source_volume_not_positive'
        elif not(dec(b['low'])<=price<=dec(b['high']) and dec(b['limit_down'])<price<dec(b['limit_up'])):reason='price_boundary_no_fill'
        else:
            q=buy_quantity(price,capital,capital,scenario,lot);charge=fee(price*q,scenario['buy_all_in_fee_rate'],scenario['minimum_fee']);cash-=price*q+charge;reason='conditional_fill' if q else 'budget_or_fraction_no_fill'
    if cash<0 or q%lot:raise AssertionError('ETF shared cash or lot violation')
    eligible=[e for e in dividends if entry_day is not None and entry_day<=e['record_date']<=dates[-1]]
    ledger=[];peak=capital;max_dd=D(0);annual={};year_start=capital;paid=D(0);receivable=D(0)
    for i,day in enumerate(dates):
        if i<entry_index:nav=capital;paid=D(0);receivable=D(0)
        else:
            paid=sum((e['cash_per_unit']*q for e in eligible if e['pay_date']<=day),D(0))
            receivable=sum((e['cash_per_unit']*q for e in eligible if e['ex_date']<=day<e['pay_date']),D(0))
            nav=cash+paid+receivable+dec(bars[i]['close'])*q
        if nav<0:raise AssertionError('ETF negative NAV')
        peak=max(peak,nav);max_dd=max(max_dd,1-nav/peak);ledger.append({'date':day,'nav_CNY':str(nav),'cash_CNY':str(cash+paid if i>=entry_index else capital),'cash_receivable_CNY':str(receivable)})
        if i==len(dates)-1 or dates[i+1]//10000!=day//10000:
            annual[str(day//10000)]={'net_return':str(nav/year_start-1),'end_nav_CNY':str(nav)};year_start=nav
    return {'id':scenario['id'],'state':'conditional_matched_period_ETF_book_computed','terminal_marked_nav_CNY':str(nav.quantize(D('.01'))),'terminal_cash_CNY':str((cash+paid).quantize(D('.01'))),'maximum_marked_drawdown':str(max_dd),'total_fees_CNY':str(charge.quantize(D('.01'))),'paid_dividend_CNY':str(paid.quantize(D('.01'))),'unpaid_dividend_CNY':str(receivable.quantize(D('.01'))),'annual_marked_results':annual,'conditional_entry_reason':reason,'modeled_entry_fills':int(q>0),'initial_allocation_target':'1.0','fund_dividend_pretax':True,'terminal_sale_executed':False,'sell_fee_preset_unused_as_no_terminal_sale':True,'real_trades_executed':0,'model_admitted':False},ledger
