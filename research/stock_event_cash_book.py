"""Shared cash/T+1/action accounting for a conditional daily research book.

Inputs are callbacks, so native quotes stay in the local process. No live orders.
"""
from decimal import Decimal,ROUND_FLOOR,ROUND_CEILING
from collections import Counter,defaultdict

D=Decimal
def dec(x):return x if isinstance(x,D) else D(str(x))
def fee(notional,rate,minimum):return max(dec(minimum),notional*dec(rate)) if notional>0 else D(0)
def modeled_price(bar,side,scenario):
    base=dec(bar[scenario['entry_field'] if side=='buy' else 'close'])
    factor=D(1)+dec(scenario['slippage']) if side=='buy' else D(1)-dec(scenario['slippage'])
    rounding=ROUND_CEILING if side=='buy' else ROUND_FLOOR
    return (base*factor/D('0.01')).to_integral_value(rounding=rounding)*D('0.01')
def buy_quantity(price,budget,cash,scenario,lot):
    budget=min(dec(budget),dec(cash));rate=dec(scenario['buy_all_in_fee_rate'])
    if price<=0 or budget<=0:return 0
    q=int((budget/(price*(D(1)+rate))/lot).to_integral_value(rounding=ROUND_FLOOR))*lot
    while q>0 and price*q+fee(price*q,rate,scenario['minimum_fee'])>budget:q-=lot
    q=int((dec(q)*dec(scenario['entry_fill_fraction'])/lot).to_integral_value(rounding=ROUND_FLOOR))*lot
    return q
class UnresolvedAction(ValueError):
    def __init__(self,key,day,kind):
        self.dependency={'instrument_id':key,'date':day,'kind':kind};super().__init__('unresolved_account_action')

def replay(calendar,intents,bar_getter,actions,scenario,initial='150000',lot=100,name_cap='0.2',gross_cap='0.4',max_positions=2,tax='0.2',rights_policy='unresolved'):
    index={d:i for i,d in enumerate(calendar)};entries=defaultdict(list);counts=Counter();events=[]
    for intent in intents:
        start=index[intent['signal_date']]+1+int(scenario['entry_delay_sessions'])
        if start>=len(calendar):counts['entry_censored_outside_window']+=1;continue
        entries[calendar[start]].append((intent,start))
    for v in entries.values():v.sort(key=lambda x:x[0]['rank'])
    cash=dec(initial);prior_nav=cash;peak=cash;max_dd=D(0);max_gross=D(0);max_name=D(0);positions={};cash_rights=[];share_rights=[];last_closes={};ledger=[];annual={};year_start_nav=cash
    held_history=defaultdict(set);actual_action_events=set();accrued_net_div=D(0);paid_net_div=D(0);total_fees=D(0)
    def fail(key,day,kind):
        e=UnresolvedAction(key,day,kind)
        e.native_checkpoint={'scenario':scenario['id'],'counts':dict(counts),'cash_CNY':str(cash),'positions_without_prices':{k:dict(v) for k,v in positions.items()},'daily_aggregate_ledger':list(ledger),'fill_events_without_prices':list(events),'held_dates':{k:sorted(v) for k,v in held_history.items()}}
        return e
    cash_records=defaultdict(list);cash_ex=defaultdict(list);split_records=defaultdict(list);split_ex=defaultdict(list);unknown_days=defaultdict(list)
    for key,a in actions.items():
        for e in a.get('cash',[]):cash_records[e['record_date']].append((key,e));cash_ex[e['ex_date']].append((key,e))
        for e in a.get('split',[]):
            split_ex[e['ex_date']].append((key,e))
            if e.get('record_date'):split_records[e['record_date']].append((key,e))
        for day in a.get('unknown_factor_dates',[]):unknown_days[day].append((key,'unmatched_factor_action'))
        if a.get('delist_date'):unknown_days[a['delist_date']].append((key,'delist_or_transformation_exit'))
    rights_records=defaultdict(list);rights_ex=defaultdict(list);rights_owners={}
    transform_records=defaultdict(list);transform_days=defaultdict(list);transform_owners={}
    for key,a in actions.items():
        for e in a.get('rights',[]):
            rights_records[e['record_date']].append((key,e));rights_ex[e['ex_date']].append((key,e))
        for e in a.get('transform',[]):
            transform_records[e['record_date']].append((key,e));transform_days[e['effective_date']].append((key,e))
    for day in calendar:
        i=index[day];today_fees=D(0)
        # A confirmed merger preserves the asset; it is neither last-price
        # liquidation nor an ordinary delisting. Unknown odd shares still stop.
        for key,e in transform_days[day]:
            if key not in positions:continue
            p=positions[key];q=transform_owners.get((key,day));ratio=dec(e['ratio']);dest=e['successor_id']
            if q is None or q!=p['qty'] or p['pending'] or ratio<=0 or e['trading_date']!=day:raise fail(key,day,'transformation_ownership_or_availability_unresolved')
            converted=dec(q)*ratio
            if converted!=converted.to_integral_value():raise fail(key,day,'transformation_fractional_share_allocation_unresolved')
            if dest in positions:raise fail(key,day,'transformation_destination_lot_merging_unresolved')
            if key not in last_closes:raise fail(key,day,'transformation_reference_missing')
            oldmark=last_closes.pop(key);positions.pop(key);positions[dest]=dict(p,qty=int(converted));last_closes[dest]=oldmark/ratio
            counts['confirmed_share_transformations']+=1;actual_action_events.add((key,day,'share_transformation'))
            events.append({'date':day,'instrument_id':key,'successor_id':dest,'kind':'share_transformation','qty':int(converted),'execution_assumption':'automatic original-issuer integer security conversion; not discretionary purchase'})
        for key,e in rights_ex[day]:
            if key not in positions:continue
            if rights_policy!='waive_all':raise fail(key,day,'rights_issue_election_unresolved')
            if e['record_date'] not in index or e['subscription_end_date']>=day:raise fail(key,day,'rights_issue_chronology_unresolved')
            if (key,day) in rights_owners:
                counts['rights_entitlements_waived']+=1;actual_action_events.add((key,day,'rights_waived'))
            # Subscription is deliberately absent: no cash payment or free
            # shares. A stale reference alone reflects declared actual dilution.
            if key in last_closes:
                ratio=dec(e['actual_issue_ratio']);price=dec(e['subscription_price'])
                if ratio<=0 or price<=0:raise fail(key,day,'rights_issue_reference_invalid')
                last_closes[key]=(last_closes[key]+ratio*price)/(1+ratio)
        # Unknown actions never remove candidates. A filled/exposed book stops
        # with explicit uncertainty instead of treating the action as zero.
        for key,kind in unknown_days[day]:
            if key in positions:raise fail(key,day,kind)
        # On a halted ex-day a stale prior close needs an action reference
        # adjustment. Gross cash is removed before the split ratio, while the
        # separately taxed receivable is counted once.
        for key,e in cash_ex[day]:
            if key in positions and not e['valid']:raise fail(key,day,'cash_ex_identity_or_chronology_unresolved')
            if key in last_closes:last_closes[key]-=dec(e['gross_cash_per_share'])
        for key,e in split_ex[day]:
            if key in positions and not e.get('record_date'):raise fail(key,day,'split_record_and_share_availability_missing')
            for right in share_rights:
                if right['key']==key and right['ex_date']==day and not right['accrued']:
                    right['accrued']=True;positions.setdefault(key,{'qty':0,'entry_index':right['entry_index'],'exit_index':right['exit_index'],'pending':0})['pending']+=right['bonus_qty']
                    if key in last_closes:last_closes[key]/=dec(e['ratio'])
                    counts['split_accrual_events']+=1;actual_action_events.add((key,day,'split'))
        for right in share_rights:
            if right['accrued'] and not right['credited'] and right['credit_date']<=day:
                p=positions[right['key']];p['qty']+=right['bonus_qty'];p['pending']-=right['bonus_qty'];right['credited']=True;counts['split_tradable_share_release_events']+=1
        for right in cash_rights:
            if right['ex_date']==day and not right['accrued']:
                right['accrued']=True;accrued_net_div+=right['net_amount'];counts['cash_ex_accrual_events']+=1;actual_action_events.add((right['key'],day,'cash'))
            if right['accrued'] and not right['paid'] and right['pay_date']<=day:
                cash+=right['net_amount'];paid_net_div+=right['net_amount'];right['paid']=True;counts['cash_payment_events']+=1
        # Lagged NAV defines fixed allocations. Today's open is an execution
        # reference for exposure only; no close/high used in standard sizing.
        exposure=D(0)
        for key,p in positions.items():
            b=bar_getter(key,day)
            tradable_reference=b and not b.get('halted') and dec(b['volume'])>0
            mark=dec(b['open']) if tradable_reference else last_closes.get(key)
            if mark is None:raise fail(key,day,'missing_position_mark')
            exposure+=dec(p['qty']+p['pending'])*mark
        for intent,start in entries[day]:
            key=intent['instrument_id'];counts['entry_attempts']+=1
            if key in positions:counts['entry_cancel_already_held']+=1;continue
            if len(positions)>=max_positions:counts['entry_cancel_position_slots']+=1;continue
            b=bar_getter(key,day)
            if not b or b.get('halted') or b.get('st') or b.get('outside_lifetime') or dec(b['volume'])<=0:counts['entry_no_fill_state']+=1;continue
            price=modeled_price(b,'buy',scenario)
            if not (dec(b['low'])<=price<=dec(b['high']) and dec(b['limit_down'])<price<dec(b['limit_up'])):counts['entry_no_fill_price_boundary']+=1;continue
            budget=min(cash,dec(name_cap)*prior_nav,max(D(0),dec(gross_cap)*prior_nav-exposure))
            q=buy_quantity(price,budget,cash,scenario,lot)
            if not q:counts['entry_no_fill_budget_or_fraction']+=1;continue
            charge=fee(price*q,scenario['buy_all_in_fee_rate'],scenario['minimum_fee']);cash-=price*q+charge;assert cash>=0
            total_fees+=charge;today_fees+=charge;positions[key]={'qty':q,'entry_index':i,'exit_index':i+1,'pending':0};last_closes[key]=price;exposure+=price*q;counts['entry_fills']+=1
            if dec(scenario['entry_fill_fraction'])<1:counts['partial_entry_fills']+=1
            events.append({'date':day,'instrument_id':key,'kind':'entry_fill','qty':q,'execution_assumption':scenario['id']})
        # At close, an exit only sells old, credited shares. Halts, limits and
        # partial fills retain residual ownership and block new allocations.
        for key in list(positions):
            p=positions[key];b=bar_getter(key,day);held_history[key].add(day)
            if p['qty'] and i>=p['exit_index']:
                counts['exit_attempts']+=1
                if i<=p['entry_index']:raise AssertionError('T+1 violated')
                if not b or b.get('halted') or dec(b['volume'])<=0:counts['exit_no_fill_state']+=1
                else:
                    price=modeled_price(b,'sell',scenario)
                    if not (dec(b['low'])<=price<=dec(b['high']) and dec(b['limit_down'])<price<=dec(b['limit_up'])):counts['exit_no_fill_price_boundary']+=1
                    else:
                        fraction=dec(scenario['exit_fill_fraction']);q=p['qty']
                        if fraction<=0:
                            counts['exit_no_fill_fraction']+=1
                            continue
                        if fraction<D(1) and q>=lot:q=min(q,max(lot,int((dec(q)*fraction/lot).to_integral_value(rounding=ROUND_FLOOR))*lot))
                        charge=fee(price*q,scenario['sell_all_in_fee_rate'],scenario['minimum_fee']);cash+=price*q-charge;assert cash>=0
                        total_fees+=charge;today_fees+=charge;p['qty']-=q;counts['exit_fills']+=1
                        if p['qty']:counts['partial_exit_residuals']+=1
                        events.append({'date':day,'instrument_id':key,'kind':'exit_fill','qty':q,'execution_assumption':scenario['id']})
            # Record-day close ownership is checked after close sales.
            if not p['qty'] and not p['pending']:del positions[key]
        for key,e in cash_records[day]:
            if key not in positions or not positions[key]['qty']:continue
            if not e['valid']:raise fail(key,day,'cash_identity_or_chronology_unresolved')
            q=positions[key]['qty'];amount=dec(q)*dec(e['gross_cash_per_share'])*(D(1)-dec(tax))
            cash_rights.append({'key':key,'ex_date':e['ex_date'],'pay_date':e['pay_date'],'net_amount':amount,'accrued':False,'paid':False});counts['cash_record_entitlements']+=1
        for key,e in split_records[day]:
            if key not in positions or not positions[key]['qty']:continue
            p=positions[key];bonus=dec(p['qty'])*(dec(e['ratio'])-D(1))
            if bonus!=bonus.to_integral_value():raise fail(key,e['ex_date'],'fractional_share_allocation_unresolved')
            if e.get('distribution_type_uncertain') or dec(e.get('bonus_share_per_pre_share','0'))>0:raise fail(key,e['ex_date'],'bonus_share_tax_or_distribution_type_unresolved')
            trade=e.get('trading_date');credit=e.get('credit_date')
            if trade is None or trade<e['ex_date'] or (credit is not None and credit<e['ex_date']):raise fail(key,e['ex_date'],'split_chronology_unresolved')
            # The ex-day share claim remains pending until a public trading
            # date and any explicitly known later credit date both permit sale.
            # An unstated broker credit day is not invented: availability is a
            # stated research convention at the confirmed public listing day.
            available=max(trade,credit) if credit is not None else trade
            share_rights.append({'key':key,'ex_date':e['ex_date'],'credit_date':available,'bonus_qty':int(bonus),'entry_index':p['entry_index'],'exit_index':p['exit_index'],'accrued':False,'credited':False,'actual_credit_day_unknown':credit is None});counts['split_record_entitlements']+=1
        for key,e in rights_records[day]:
            if key in positions and positions[key]['qty']:
                rights_owners[(key,e['ex_date'])]=positions[key]['qty'];counts['rights_record_entitlements']+=1
        for key,e in transform_records[day]:
            if key in positions:
                transform_owners[(key,e['effective_date'])]=positions[key]['qty'];counts['transformation_record_entitlements']+=1
        marked=D(0)
        for key,p in positions.items():
            b=bar_getter(key,day)
            if b and not b.get('halted') and dec(b['volume'])>0:last_closes[key]=dec(b['close'])
            if key not in last_closes:raise fail(key,day,'missing_close_mark')
            marked+=dec(p['qty']+p['pending'])*last_closes[key]
        receivable=sum((r['net_amount'] for r in cash_rights if r['accrued'] and not r['paid']),D(0))
        nav=cash+marked+receivable;assert nav>=0 and cash>=0 and len(positions)<=max_positions
        gross_fraction=marked/nav if nav else D(0);name_fraction=max((dec(p['qty']+p['pending'])*last_closes[k]/nav for k,p in positions.items()),default=D(0)) if nav else D(0)
        max_gross=max(max_gross,gross_fraction);max_name=max(max_name,name_fraction)
        if gross_fraction>dec(gross_cap):counts['close_gross_above_target']+=1
        if name_fraction>dec(name_cap):counts['close_name_above_target']+=1
        peak=max(peak,nav);max_dd=max(max_dd,D(1)-nav/peak);prior_nav=nav
        ledger.append({'date':day,'nav_CNY':str(nav),'cash_CNY':str(cash),'marked_positions_CNY':str(marked),'cash_receivable_CNY':str(receivable),'fees_CNY':str(today_fees),'position_count':len(positions)})
        if i==len(calendar)-1 or calendar[i+1]//10000!=day//10000:
            annual[str(day//10000)]={'net_return':str(nav/year_start_nav-D(1)),'end_nav_CNY':str(nav)};year_start_nav=nav
    summary={'id':scenario['id'],'state':'conditional_daily_book_computed','terminal_marked_nav_CNY':str(prior_nav.quantize(D('.01'))),'terminal_cash_CNY':str(cash.quantize(D('.01'))),'maximum_marked_drawdown':str(max_dd),'maximum_close_gross_fraction':str(max_gross),'maximum_close_name_fraction':str(max_name),'total_fees_CNY':str(total_fees.quantize(D('.01'))),'accrued_net_dividend_CNY':str(accrued_net_div.quantize(D('.01'))),'paid_net_dividend_CNY':str(paid_net_div.quantize(D('.01'))),'residual_positions':len(positions),'unpaid_cash_entitlements':sum(not r['paid'] for r in cash_rights),'uncredited_share_entitlements':sum(not r['credited'] for r in share_rights),'counts':dict(counts),'annual_marked_results':annual,'actual_action_events':[{'instrument_id':k,'date':d,'kind':kind} for k,d,kind in sorted(actual_action_events)],'liquidated_terminal_cash':False,'tax_is_fixed_conservative_assumption':True,'model_admitted':False}
    return summary,{'daily_aggregate_ledger':ledger,'fill_events_without_prices':events,'held_dates':{k:sorted(v) for k,v in held_history.items()},'action_events':summary['actual_action_events']}
