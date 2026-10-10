"""Physical-share settlement for a fixed historical event, never a live account.

The caller binds implementation notices to immutable source artifacts. Ratios
and dates are checked again here against the original daily observations.
"""
from datetime import date
from decimal import Decimal, ROUND_DOWN
import math

from .cash_dividend_evidence import cent, number
from .control_corporate_evidence import distribution_terms, iso_day
from .crowding_cash_settlement import cash_market_view, dividend_tax_rate
from .emotion_pattern_replay import CONTRACT, DistributionInputGap, replay_event

SPEC = {
    "schema": "physical_distribution_settlement.v1",
    "purpose": "historical_accounting_correction_not_strategy_admission",
    "inventory": "record_close_lots_receive_entitlements; physical_shares_not_virtual_adjusted_shares",
    "availability": "original_T_plus_1_and_announced_unrestricted_new_share_listing_date",
    "thresholds": "divide_entry_cap_stop_and_2R_price_units_by_known_share_ratio; retain_original_cash_nominal_policy",
    "cash": "announced_payout_date_no_reinvestment; receivable_outside_horizon_unresolved",
    "tax": "FIFO_original_lots_and_each_sale_holding_duration; no_tax_on_unentitled_new_lot",
    "fractional": "no_rounding_without_actual_allocation_evidence",
    "new_lot_holding_date": "unknown_unless_explicitly_proven; later_taxable_distribution_may_remain_unresolved",
    "independent_audit_scope": "one_implementation_action_per_repaired_execution_window",
    "boundaries": "unknown_actions_invalid_OHLC_and_unsupported_inventory_remain_unresolved",
    "summary_gap": "explicit_source_bound_issuer_only_non_cash_transfer; retain_empty_summary_and_disclose_missing_confirmation",
    "differentiated_cash": "source_bound_A_share_repurchase_exclusion; exact_published_six_decimal_truncation; actual_cash_and_tax_unchanged",
    "new_blind_trial": False, "production_eligible": False, "generates_alert": False,
    "sources": [
        "https://www.chinatax.gov.cn/n810341/n810755/c1797427/content.html",
        "https://www.chinatax.gov.cn/chinatax/n810341/n810765/n812151/n812386/c1082457/content.html",
        "https://shanghai.chinatax.gov.cn/zcfw/zcfgk/grsds/200402/t288656.html",
    ],
}


def differentiated_cash_reference(notice, cash):
    """One explicit issuer formula, not a tolerance for unexplained factor jumps."""
    if (notice.get('reference_policy') != 'a_share_cash_excluding_repurchase'
            or notice.get('reference_share_class') != 'A'
            or not str(notice.get('code', '')).startswith(('0', '3', '6'))
            or type(notice.get('reference_precision')) is not int or notice['reference_precision'] != 6
            or notice.get('reference_rounding') != 'ROUND_DOWN'):
        return None
    total, a, b, excluded, eligible = (notice.get(k) for k in
        ('total_shares', 'a_shares', 'b_shares', 'excluded_repurchase_a_shares', 'eligible_a_shares'))
    if (any(type(v) is not int or v < 0 for v in (total, a, b, excluded, eligible))
            or not 0 < excluded < a or a + b != total or eligible != a - excluded):
        return None
    amount, reference = number(notice.get('a_cash_total_yuan')), number(notice.get('reference_cash_per_share'))
    if amount != cent(eligible * cash) or reference is None or not 0 < reference < cash:
        return None
    expected = (amount / a).quantize(Decimal('0.000001'), rounding=ROUND_DOWN)
    return reference if reference == expected else None


def reconcile_distribution(previous, current, action, notice, *, previous_session):
    """A source-bound notice cannot silently override contradictory summary facts."""
    invalid = lambda reason: {"verified": False, "reason": reason}
    if (previous['trade_date'] != previous_session or previous['code'] != current['code']
            or not previous.get('stable_instrument_id')
            or previous['stable_instrument_id'] != current.get('stable_instrument_id')):
        return invalid('distribution_market_identity_missing')
    if any(action.get(k) != notice.get(k) for k in ('code','announcement_date','record_date','ex_date','payout_date')):
        return invalid('distribution_notice_summary_conflict')
    if (action.get('code') != current['code'] or action.get('ex_date') != current['trade_date']
            or action.get('record_date') != previous_session or not iso_day(action.get('announcement_date'))
            or action['announcement_date'] > previous_session):
        return invalid('distribution_dates_or_security_invalid')
    terms = distribution_terms(action)
    if terms is None or any(number(notice.get(k)) != v for k, v in terms.items()):
        return invalid('distribution_proportions_not_proven')
    bonus, transfer, cash = (terms[k] / 10 for k in ('bonus_ratio','transfer_ratio','cash_dividend_per10'))
    listing = notice.get('unrestricted_new_share_listing_date')
    reference_cash, reference_evidence = cash, {}
    if notice.get('differentiated_distribution') is True:
        reference_cash = differentiated_cash_reference(notice, cash)
        if (bonus or transfer or cash <= 0 or reference_cash is None or listing is not None
                or action.get('share_arrival_date') is not None or notice.get('new_share_holding_start_date') is not None):
            return invalid('distribution_differentiated_reference_unsupported')
        reference_evidence = {k: notice[k] for k in (
            'reference_policy', 'reference_share_class', 'reference_cash_per_share', 'reference_precision',
            'reference_rounding', 'total_shares', 'a_shares', 'b_shares',
            'excluded_repurchase_a_shares', 'eligible_a_shares', 'a_cash_total_yuan')}
    elif notice.get('differentiated_distribution') is not False:
        return invalid('distribution_differentiated_reference_unsupported')
    elif bonus + transfer <= 0 or not iso_day(listing) or listing < current['trade_date']:
        return invalid('distribution_new_share_listing_missing')
    if action.get('share_arrival_date') is not None and action['share_arrival_date'] != listing:
        return invalid('distribution_arrival_listing_requires_separate_evidence')
    if cash and (not iso_day(notice.get('payout_date')) or notice['payout_date'] < current['trade_date']):
        return invalid('distribution_cash_payment_missing')
    taxable_bonus = number(notice.get('bonus_taxable_value_per_new_share'))
    taxable_transfer = number(notice.get('transfer_taxable_value_per_new_share'))
    if ((bonus and (taxable_bonus is None or taxable_bonus <= 0))
            or (transfer and (taxable_transfer is None or taxable_transfer < 0))
            or (transfer and taxable_transfer == 0 and notice.get('transfer_source') != 'share_premium')):
        return invalid('distribution_taxable_basis_missing')
    close, pf, factor = [number(v) for v in (previous.get('close'),previous.get('adj_factor'),current.get('adj_factor'))]
    if (any(v is None or v <= 0 for v in (close,pf,factor))
            or previous.get('adjustment_trusted') is not True or current.get('adjustment_trusted') is not True
            or number(current.get('prev_close')) != close):
        return invalid('distribution_raw_price_identity_invalid')
    theoretical, implied = cent((close-reference_cash)/(1+bonus+transfer)), cent(close*pf/factor)
    if theoretical <= 0 or theoretical != implied:
        return {**invalid('distribution_reference_does_not_reconcile'),
                'theoretical_reference':float(theoretical),'factor_implied_reference':float(implied)}
    holding_start = notice.get('new_share_holding_start_date')
    if holding_start is not None and (not iso_day(holding_start) or not previous_session < holding_start <= listing):
        return invalid('distribution_new_lot_holding_date_invalid')
    return {**reference_evidence,'verified':True,'reason':'distribution_notice_and_market_reconciled',
            'code':current['code'],'record_date':previous_session,'ex_date':current['trade_date'],
            'announcement_date':action['announcement_date'],'payout_date':notice.get('payout_date'),
            'unrestricted_new_share_listing_date':listing,'new_share_holding_start_date':holding_start,
            'share_ratio':str(bonus+transfer),'cash_per_share':str(cash),
            'taxable_per_original_share':str(cash+bonus*(taxable_bonus or 0)+transfer*(taxable_transfer or 0)),
            'theoretical_reference':float(theoretical),'factor_implied_reference':float(implied),
            'notice_sources':notice['source_artifacts']}


def distribution_market_view(market, cash_evidence, notices):
    """Remove only independently rechecked jumps; raw daily OHLC stays intact."""
    view, cash_proofs = cash_market_view(market, cash_evidence)
    actions = {}
    for day, notice in notices.items():
        row = market.rows.get(day)
        if row is None or market.index[day] == 0:
            raise ValueError('distribution_notice_market_day_missing')
        evidence = cash_evidence.get(day, {})
        raw = evidence.get('official_actions', [])
        issuer_only = notice.get('summary_gap') is not None
        if issuer_only:
            # A cash-only summary can omit a pure transfer. This is a separately
            # bound issuer source, not fabricated independent confirmation.
            if (notice['summary_gap'] != 'non_cash_transfer_omitted'
                    or evidence.get('official_actions') != []
                    or evidence.get('verified') is not False or evidence.get('factor_changed') is not True
                    or evidence.get('reason') != 'cash_action_missing_or_not_unique'
                    or evidence.get('code') != row['code'] or evidence.get('ex_date') != day
                    or number(notice.get('cash_dividend_per10')) != 0
                    or number(notice.get('bonus_ratio')) != 0):
                continue
            action = notice
        elif len(raw) == 1:
            action = raw[0]
        else:
            continue
        previous_day = market.sessions[market.index[day]-1]
        previous = market.rows.get(previous_day)
        if previous is None:
            continue
        proof = reconcile_distribution(previous, row, action, notice, previous_session=previous_day)
        if proof['verified']:
            if issuer_only:
                proof.update(reason='issuer_only_transfer_and_market_reconciled',
                             summary_confirmation='missing_non_cash_action')
            actions[day] = proof
            view.rows[day]['prev_close'] = proof['theoretical_reference']
    # The existing private view already removed proven cash-only jumps.
    previous, normalized = None, None
    for day in sorted(view.rows):
        row = view.rows[day]
        factor = row['adj_factor']
        if isinstance(factor,bool) or not isinstance(factor,(int,float)) or not math.isfinite(factor) or factor <= 0:
            previous, normalized = None, None
            continue
        if previous is None:
            normalized = factor
        elif day not in actions:
            normalized *= factor/previous
        row['adj_factor'], previous = normalized, factor
    for day, proof in cash_proofs.items():
        actions.setdefault(day, {**proof,'share_ratio':'0','taxable_per_original_share':str(proof['cash_per_share']),
                        'unrestricted_new_share_listing_date':None,'new_share_holding_start_date':None})
    return view, actions


class DistributionLedger:
    """One-entry FIFO lots with separate ownership, sale availability and tax."""
    def __init__(self, actions, sessions):
        self.actions, self.sessions = actions, sessions
        self.lots, self.snapshots, self.entitlements, self.thresholds = [], {}, [], []
        self.entry_day = None

    def entry_terms(self, candidate, day):
        result = dict(candidate)
        ratio = Decimal(1)
        signal = str(candidate['trade_date'])[:10]
        if date.fromisoformat(signal).isoformat() != signal:
            raise ValueError('distribution_signal_date_invalid')
        for ex, proof in sorted(self.actions.items()):
            if signal < ex <= day:
                ratio *= 1+Decimal(proof['share_ratio'])
        for key in ('risk_line','maximum_entry_open'):
            result[key] = float(Decimal(str(result[key]))/ratio)
        if ratio != 1:
            self.thresholds.append({'day':day,'phase':'before_entry','share_multiplier':str(ratio),
                                    'risk_before':candidate['risk_line'],'risk_after':result['risk_line'],
                                    'cap_before':candidate['maximum_entry_open'],'cap_after':result['maximum_entry_open']})
        return result

    def bought(self, day, quantity):
        if self.lots or type(quantity) is not int or quantity <= 0:
            raise ValueError('distribution_initial_inventory_invalid')
        self.entry_day = day
        self.lots.append({'shares':quantity,'holding_start':day,'available_from':day,'taxes':[]})

    def open_day(self, day, inventory, price, risk):
        proof = self.actions.get(day)
        if proof is None:
            return inventory, price, risk
        record = proof['record_date']
        entitled = [] if record < self.entry_day else self.snapshots.get(record)
        if entitled is None:
            raise DistributionInputGap('distribution_record_inventory_missing')
        quantity = sum(q for _, q in entitled)
        if quantity == 0:
            return inventory, price, risk
        ratio = Decimal(proof['share_ratio'])
        exact = Decimal(quantity)*ratio
        if exact != exact.to_integral_value():
            raise DistributionInputGap('distribution_fractional_allocation_missing')
        new = int(exact)
        cash = cent(Decimal(quantity)*Decimal(str(proof['cash_per_share'])))
        receipt = {**proof,'entitled_original_shares':quantity,'new_shares':new,
                   'gross_yuan':float(cash),'tax_yuan':0.,'sale_tax_liabilities':[],
                   'actual_cash_received':False,'actual_share_credit':False}
        self.entitlements.append(receipt)
        for index, count in entitled:
            if self.lots[index]['shares'] != count:
                raise ValueError('distribution_record_lot_quantity_changed')
            self.lots[index]['taxes'].append(receipt)
        if new:
            self.lots.append({'shares':new,'holding_start':proof['new_share_holding_start_date'],
                              'available_from':proof['unrestricted_new_share_listing_date'],'taxes':[]})
            next_price, next_risk = price/float(1+ratio), risk/float(1+ratio)
            self.thresholds.append({'day':day,'entry_reference_before':price,'entry_reference_after':next_price,
                                    'risk_before':risk,'risk_after':next_risk,'share_ratio':str(ratio)})
            price, risk = next_price, next_risk
        return inventory+new, price, risk

    def available(self, day):
        return sum(lot['shares'] for lot in self.lots if lot['available_from'] <= day)

    def sold(self, day, quantity):
        if type(quantity) is not int or quantity <= 0 or quantity > self.available(day):
            raise ValueError('distribution_sale_exceeds_available')
        # Compute first, then mutate; a missing tax date leaves the prior ledger intact.
        allocations, remaining = [], quantity
        for lot in self.lots:
            count = min(remaining,lot['shares']) if lot['available_from'] <= day else 0
            if not count:
                continue
            taxes = []
            for receipt in lot['taxes']:
                basis = Decimal(receipt['taxable_per_original_share'])
                if basis and lot['holding_start'] is None:
                    raise DistributionInputGap('distribution_tax_holding_start_missing')
                rate = dividend_tax_rate(lot['holding_start'],day) if basis else 0.
                due = cent(Decimal(count)*basis*Decimal(str(rate)))
                taxes.append((receipt,{'day':day,'shares':count,'holding_start':lot['holding_start'],
                                        'rate':rate,'taxable_per_share':str(basis),'tax_yuan':float(due)}))
            allocations.append((lot,count,taxes)); remaining -= count
            if not remaining:
                break
        for lot, count, taxes in allocations:
            lot['shares'] -= count
            for receipt, liability in taxes:
                receipt['sale_tax_liabilities'].append(liability)
                receipt['tax_yuan'] = float(sum((Decimal(str(r['tax_yuan'])) for r in receipt['sale_tax_liabilities']),Decimal(0)))

    def close_day(self, day, inventory):
        if sum(lot['shares'] for lot in self.lots) != inventory:
            raise ValueError('distribution_inventory_does_not_reconcile')
        self.snapshots[day] = [(i,lot['shares']) for i,lot in enumerate(self.lots) if lot['shares']]

    def finish(self, event):
        event.update(distribution_schema=SPEC['schema'],distribution_entitlements=self.entitlements,
                     corporate_threshold_adjustments=self.thresholds,cash_settlement_complete=False,
                     fixed_budget_return=None,cash_entitlements=self.entitlements)
        if event['status'] != 'closed':
            return event
        if any(lot['shares'] for lot in self.lots):
            raise ValueError('distribution_closed_with_inventory')
        for receipt in self.entitlements:
            if sum(r['shares'] for r in receipt['sale_tax_liabilities']) != receipt['entitled_original_shares']:
                raise ValueError('distribution_tax_entitlement_quantity_mismatch')
        payment = max([event['exit_day']]+[r['payout_date'] for r in self.entitlements if r['gross_yuan']])
        event['cash_settlement_day'] = payment
        if payment > self.sessions[-1]:
            event.update(status='unresolved',reason='distribution_receivable_outside_input_horizon',
                         net_pnl_yuan=None,net_return_on_entry_cash=None)
            return event
        gross = sum((Decimal(str(r['gross_yuan'])) for r in self.entitlements),Decimal(0))
        tax = sum((Decimal(str(r['tax_yuan'])) for r in self.entitlements),Decimal(0))
        entry_cash = sum(o['shares']*o['price']+o['costs']['total'] for o in event['execution_events'] if o['side']=='buy')
        event.update(price_only_net_pnl_yuan=event['net_pnl_yuan'],entry_cash_yuan=entry_cash,
                     dividend_gross_yuan=float(gross),dividend_tax_yuan=float(tax),
                     total_cost_including_dividend_tax_yuan=event['cost_yuan']+float(tax))
        event['net_pnl_yuan'] += float(gross-tax)
        event.update(net_return_on_entry_cash=event['net_pnl_yuan']/entry_cash,
                     fixed_budget_return=event['net_pnl_yuan']/CONTRACT['event_cash_yuan'],cash_settlement_complete=True)
        return event


def replay_distribution_event(candidate, market, evidence, notices, *, bps, source_complete):
    """Explicit accounting amendment; the default frozen replay stays unchanged."""
    view, actions = distribution_market_view(market, evidence, notices)
    ledger = DistributionLedger(actions, market.sessions)
    event = replay_event(candidate, view, slippage_bps=bps, distributions=ledger)
    problem = None
    if not source_complete:
        problem = 'official_action_response_missing'
    elif event['status'] == 'closed' and any(
            event['signal_day'] < day <= event['exit_day'] and day not in actions for day in evidence):
        problem = 'unverified_action_in_execution_window'
    if problem:
        event.update(status='unresolved',reason=problem,net_pnl_yuan=None,net_return_on_entry_cash=None,
                     fixed_budget_return=None,cash_settlement_complete=False)
    return event


def audit_closed_distribution(event, market, actions):
    """Reconstruct one action from orders, without calling the producer's ledger.

    This independent checker intentionally does not admit a multi-action path.
    FIFO tax and source-date checks use original shares, not final sale volume.
    """
    from .cash_conditioned_research import same_money
    from .emotion_pattern_replay import historical_costs
    from quantqc.ashare import constrain_share_quantity, opening_auction_executability

    relevant = {d:p for d,p in actions.items() if event['signal_day'] < d <= event['exit_day']}
    if (len(relevant) != 1 or event['status'] != 'closed' or event.get('cash_settlement_complete') is not True
            or event.get('production_eligible') is not False or event.get('generates_alert') is not False):
        raise ValueError('distribution_independent_scope_invalid')
    ex, proof = next(iter(relevant.items()))
    if (proof.get('verified') is not True or (Decimal(proof['share_ratio']) <= 0
            and proof.get('reference_policy') != 'a_share_cash_excluding_repurchase')):
        raise ValueError('distribution_independent_action_unverified')
    if proof.get('reference_policy') == 'a_share_cash_excluding_repurchase':
        cash_per_share = number(proof['cash_per_share'])
        if (Decimal(proof['share_ratio']) != 0 or cash_per_share is None or cash_per_share <= 0
                or number(proof['taxable_per_original_share']) != cash_per_share
                or proof['unrestricted_new_share_listing_date'] is not None
                or differentiated_cash_reference(proof, cash_per_share) is None):
            raise ValueError('distribution_independent_cash_reference_invalid')
    orders = event['execution_events']
    buys = [o for o in orders if o['side']=='buy']
    if (len(buys) != 1 or buys[0] != orders[0] or buys[0]['day'] != event['entry_day']
            or buys[0]['shares'] != event['simulated_entry_shares'] or buys[0]['shares'] % 100
            or buys[0]['day'] != market.sessions[market.index[event['signal_day']]+1]):
        raise ValueError('distribution_independent_entry_invalid')
    owned = sum(o['shares']*(1 if o['side']=='buy' else -1) for o in orders if o['day'] <= proof['record_date'])
    exact = Decimal(owned)*Decimal(proof['share_ratio'])
    if owned < 0 or exact != exact.to_integral_value():
        raise ValueError('distribution_independent_entitlement_invalid')
    added = int(exact)
    inventory, original, credited = 0, 0, False
    cash, fees, entry_cash = Decimal(0), Decimal(0), Decimal(0)
    tax_rows, previous, sales = [], event['signal_day'], []
    for order in orders:
        day, side, qty = order['day'], order['side'], order['shares']
        if (day not in market.sessions or day not in market.rows or day < previous or day <= event['signal_day']
                or side not in {'buy','sell'} or type(qty) is not int or qty < 0):
            raise ValueError('distribution_independent_order_invalid')
        previous = day
        if not credited and day >= ex:
            inventory += added; credited = True
        if not qty:
            if side != 'sell' or order.get('costs') or order.get('price'):
                raise ValueError('distribution_independent_failed_order_invalid')
            continue
        row = market.rows[day]
        price = Decimal(str(order['price']))
        if price != number(row['open']):
            raise ValueError('distribution_independent_raw_price_changed')
        reference = proof['theoretical_reference'] if day == ex else row['prev_close']
        gate = opening_auction_executability(code=event['code'],trade_date=day,side=side,
            open_price=float(price),prev_close=reference,tradable=row['trade_status']==1,
            is_risk_warning=row['is_st']==1,no_price_limit=row['no_price_limit'])
        if not gate['executable'] or row['no_price_limit'] is not False:
            raise ValueError('distribution_independent_untradable_fill')
        expected_costs = historical_costs(qty*float(price),side,day,row['lagged_adv'],event['slippage_bps'])
        if set(order['costs']) != set(expected_costs):
            raise ValueError('distribution_independent_cost_fields_invalid')
        for key, value in expected_costs.items():
            same_money(order['costs'][key],value)
        cost = Decimal(str(order['costs']['total']))
        fees += cost
        cap = int(row['lagged_adv']*.01/float(price))
        if side == 'buy':
            if qty <= 0 or qty > cap:
                raise ValueError('distribution_independent_buy_capacity_invalid')
            inventory += qty; original += qty
            entry_cash = qty*price+cost; cash -= entry_cash
        else:
            available = inventory if not added or day >= proof['unrestricted_new_share_listing_date'] else original
            accepted = constrain_share_quantity(inventory,side='sell',available_to_sell=available,
                full_liquidation=cap>=available,liquidity_cap_shares=cap)['accepted_shares']
            if day <= event['entry_day'] or qty != accepted:
                raise ValueError('distribution_independent_sale_inventory_invalid')
            taxed = min(original,qty)
            if owned and day >= ex and taxed:
                basis = Decimal(proof['taxable_per_original_share'])
                rate = dividend_tax_rate(event['entry_day'],day) if basis else 0.
                tax_rows.append({'day':day,'shares':taxed,'holding_start':event['entry_day'],
                                 'rate':rate,'taxable_per_share':str(basis),
                                 'tax_yuan':float(cent(taxed*basis*Decimal(str(rate))))})
            original -= taxed; inventory -= qty
            cash += qty*price-cost; sales.append(order)
        if inventory < 0 or original < 0:
            raise ValueError('distribution_independent_negative_inventory')
    if (inventory or event['remaining_shares'] or not sales or sales[-1]['day'] != event['exit_day']
            or entry_cash <= 0 or entry_cash > CONTRACT['event_cash_yuan']):
        raise ValueError('distribution_independent_inventory_or_budget_invalid')
    gross = cent(owned*Decimal(str(proof['cash_per_share'])))
    tax = sum((Decimal(str(r['tax_yuan'])) for r in tax_rows),Decimal(0))
    expected = [{**proof,'entitled_original_shares':owned,'new_shares':added,'gross_yuan':float(gross),
                 'tax_yuan':float(tax),'sale_tax_liabilities':tax_rows,
                 'actual_cash_received':False,'actual_share_credit':False}] if owned else []
    if event['distribution_entitlements'] != expected or event['cash_entitlements'] != expected:
        raise ValueError('distribution_independent_receipt_mismatch')
    payment = max(event['exit_day'],proof['payout_date']) if gross else event['exit_day']
    if payment > market.sessions[-1] or event.get('cash_settlement_day') != payment:
        raise ValueError('distribution_independent_payment_invalid')
    for field, value in {'price_only_net_pnl_yuan':cash,'entry_cash_yuan':entry_cash,'cost_yuan':fees,
                        'dividend_gross_yuan':gross,'dividend_tax_yuan':tax,
                        'total_cost_including_dividend_tax_yuan':fees+tax,'net_pnl_yuan':cash+gross-tax,
                        'fixed_budget_return':(cash+gross-tax)/Decimal(str(CONTRACT['event_cash_yuan'])),
                        'net_return_on_entry_cash':(cash+gross-tax)/entry_cash}.items():
        same_money(event.get(field),value)
    return float((cash+gross-tax)/Decimal(str(CONTRACT['event_cash_yuan'])))
