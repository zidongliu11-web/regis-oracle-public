"""Cash-only settlement of the unchanged emotion-pattern event rules.

The original engine and its raw-price entry cap, stop and 2R thresholds stay
frozen. This repairs cash accounting, not the rule's dividend sensitivity.
"""
from decimal import Decimal

from .cash_dividend_evidence import cent
from .crowding_cash_settlement import cash_market_view, dividend_tax_rate
from .emotion_pattern_replay import CONTRACT, replay_event


SPEC = {
    "schema": "emotion_pattern_cash_settlement.v1",
    "scope": "cash_only_no_share_distribution_or_successor",
    "signal_population": "unchanged_original_routed_events_all_cost_scenarios",
    "threshold_policy": "original_nominal_raw_price_cap_stop_and_2R_unchanged",
    "entitlement": "record_day_close_inventory_not_final_sale_quantity",
    "payment": "announced_payout_date_no_reinvestment_reserve_sale_tax",
    "return_denominator": "original_event_budget_20000_yuan",
    "new_blind_trial": False, "production_eligible": False,
    "generates_alert": False, "portfolio_return": None,
}


def replay_cash_event(candidate, market, evidence, *, bps, source_complete):
    view, proofs = cash_market_view(market, evidence)
    event = replay_event(candidate, view, slippage_bps=bps)
    event.update(cash_settlement_schema=SPEC["schema"], cash_entitlements=[],
                 cash_settlement_complete=False, fixed_budget_return=None)
    if not source_complete:
        event.update(status="unresolved", reason="official_action_response_missing",
                     net_pnl_yuan=None, net_return_on_entry_cash=None)
        return event
    # No fabricated cash/dividends or terminal mark for paths whose execution
    # remains unresolved. They retain the engine's actual residual inventory.
    if event["status"] != "closed":
        return event
    relevant = {day: proof for day, proof in evidence.items()
                if event["signal_day"] < day <= event["exit_day"]}
    if any(day not in proofs for day in relevant):
        event.update(status="unresolved", reason="unverified_action_in_execution_window",
                     net_pnl_yuan=None, net_return_on_entry_cash=None)
        return event
    orders = event["execution_events"]
    gross, tax = Decimal(0), Decimal(0)
    payment_dates = [event["exit_day"]]
    for day in sorted(relevant):
        proof = proofs[day]
        record = proof["record_date"]
        quantity = sum(order["shares"] * (1 if order["side"] == "buy" else -1)
                       for order in orders if order["day"] <= record)
        if quantity == 0:
            continue
        if quantity < 0 or record < event["entry_day"]:
            raise ValueError("pattern_cash_entitled_inventory_invalid")
        cash_per_share = Decimal(str(proof["cash_per_share"]))
        credit = cent(quantity * cash_per_share)
        sales = []
        for order in orders:
            if order["side"] != "sell" or order["day"] <= record or order["shares"] == 0:
                continue
            rate = dividend_tax_rate(event["entry_day"], order["day"])
            liability = cent(order["shares"] * cash_per_share * Decimal(str(rate)))
            sales.append({"day": order["day"], "shares": order["shares"],
                          "rate": rate, "tax_yuan": float(liability)})
        if sum(row["shares"] for row in sales) != quantity:
            raise ValueError("pattern_cash_sales_do_not_reconcile")
        liability = sum((Decimal(str(row["tax_yuan"])) for row in sales), Decimal(0))
        gross += credit
        tax += liability
        payment_dates.append(proof["payout_date"])
        event["cash_entitlements"].append({**proof, "entitled_shares": quantity,
                                           "gross_yuan": float(credit), "tax_yuan": float(liability),
                                           "sale_tax_liabilities": sales, "actual_cash_received": False})
    entry_cash = sum(order["shares"] * order["price"] + order["costs"]["total"]
                     for order in orders if order["side"] == "buy")
    event.update(dividend_gross_yuan=float(gross), dividend_tax_yuan=float(tax),
                 entry_cash_yuan=entry_cash, cash_settlement_day=max(payment_dates),
                 total_cost_including_dividend_tax_yuan=event["cost_yuan"] + float(tax))
    if max(payment_dates) > market.sessions[-1]:
        event.update(status="unresolved", reason="dividend_receivable_outside_input_horizon",
                     net_pnl_yuan=None, net_return_on_entry_cash=None)
        return event
    event["price_only_net_pnl_yuan"] = event["net_pnl_yuan"]
    event["net_pnl_yuan"] += float(gross - tax)
    event["net_return_on_entry_cash"] = event["net_pnl_yuan"] / entry_cash
    event["fixed_budget_return"] = event["net_pnl_yuan"] / CONTRACT["event_cash_yuan"]
    event["cash_settlement_complete"] = True
    return event
