"""Evidence-backed cash entitlements around the unchanged five-session engine."""
from __future__ import annotations

import calendar
from copy import copy
from datetime import date
from decimal import Decimal
import math

from .cash_dividend_evidence import cent, reconcile_cash_reference
from .crowding_factor_replay import replay_fixed_horizon


SPEC = {
    "schema": "crowding_cash_entitlement_settlement.v1",
    "strategy_rules_changed": False, "new_blind_trial": False,
    "market_view": "remove_only_proven_cash_adjustment_jumps; raw_OHLC_and_shares_unchanged",
    "entitlement": "inventory_at_record_day_close_not_entry_or_exit_shares",
    "cash": "announced_payout_date; no_reinvestment; future_receivable_not_zero",
    "tax": "2015_rules_by_each_sale_holding_duration; liability_reserved_at_sale",
    "rounding": "hypothetical_cash_and_each_tax_liability_half_up_to_cent_not_broker_statement",
    "unresolved": "retain_unknown_share_actions_and_unpaid_receivables",
    "production_eligible": False, "generates_alert": False,
    "tax_sources": [
        "https://www.chinatax.gov.cn/n810341/n810755/c1797427/content.html",
        "https://shanghai.chinatax.gov.cn/zcfw/zcjd/201211/t400944.html",
    ],
}


def dividend_tax_rate(entry_day: str, sale_day: str) -> float:
    entry, sale = date.fromisoformat(entry_day), date.fromisoformat(sale_day)
    if not date(2021, 1, 1) <= entry < sale <= date(2025, 12, 31):
        raise ValueError("dividend_tax_date_outside_replay_scope")
    year, month = entry.year + (entry.month == 12), entry.month % 12 + 1
    month_end = date(year, month, min(entry.day, calendar.monthrange(year, month)[1]))
    year_end = date(entry.year + 1, entry.month, min(entry.day, calendar.monthrange(entry.year + 1, entry.month)[1]))
    return .2 if sale <= month_end else .1 if sale <= year_end else 0.


def cash_market_view(market, evidence: dict):
    """The private view removes jumps only after rechecking the attached facts.

    Normalizing a proven cash-only adjustment jump is NOT a share entitlement.
    The raw price/volume/status/identity stays unchanged; cash is booked below.
    Unsupported jumps still reach the frozen engine and keep it unresolved.
    """
    view = copy(market)
    view.rows = {day: dict(row) for day, row in market.rows.items()}
    verified = {}
    previous_factor, normalized_factor = None, None
    for day in sorted(market.rows):
        row = market.rows[day]
        proof = evidence.get(day)
        if proof and proof.get("verified"):
            i = market.index[day]
            previous_day = market.sessions[i-1] if i > 0 else None
            previous = market.rows.get(previous_day)
            if previous is None:
                raise ValueError("cash_proof_previous_row_missing")
            checked = reconcile_cash_reference(previous, row, proof["official_actions"], previous_session=previous_day)
            if not checked["verified"] or checked["theoretical_reference"] != proof["theoretical_reference"]:
                raise ValueError("cash_proof_does_not_match_market")
            verified[day] = checked
            view.rows[day]["prev_close"] = checked["theoretical_reference"]
        factor = row["adj_factor"]
        if not isinstance(factor, (int, float)) or not math.isfinite(factor) or factor <= 0:
            previous_factor, normalized_factor = None, None
            continue
        if previous_factor is None:
            normalized_factor = factor
        elif day not in verified:
            normalized_factor *= factor / previous_factor
        view.rows[day]["adj_factor"] = normalized_factor
        previous_factor = factor
    return view, verified


def replay_with_cash(signal: dict, view, proofs: dict, evidence: dict, *, bps: int) -> dict:
    event = replay_fixed_horizon(signal, view, bps=bps)
    event.update(cash_entitlements=[], dividend_gross_yuan=0., dividend_tax_yuan=0.,
                 cash_settlement_schema=SPEC["schema"], cash_settlement_complete=False)
    if event["status"] != "closed":
        return event
    start, exit_day = event["entry_day"], event["exit_day"]
    relevant = {day: proof for day, proof in evidence.items() if signal["signal_day"] < day <= exit_day}
    if any(not proof.get("verified") for proof in relevant.values()):
        event.update(status="unresolved", reason="unverified_cash_or_share_action_in_execution_window",
                     net_pnl_yuan=None, fixed_budget_return=None, invested_cash_return=None)
        return event
    gross, tax = Decimal(0), Decimal(0)
    payment_dates = [exit_day]
    orders = event["execution_events"]
    for day in sorted(relevant):
        proof = proofs[day]
        record = proof["record_date"]
        quantity = sum(r["shares"] * (1 if r["side"] == "buy" else -1) for r in orders if r["day"] <= record)
        if not quantity:
            continue  # Entry after record never receives that dividend.
        if quantity < 0 or record < start:
            raise ValueError("cash_entitled_inventory_invalid")
        cash_per_share = Decimal(str(proof["cash_per_share"]))
        payout = proof["payout_date"]
        credit = cent(quantity * cash_per_share)
        sold, liability = 0, Decimal(0)
        sale_taxes = []
        for order in orders:
            if order["side"] != "sell" or order["day"] <= record or not order["shares"]:
                continue
            rate = dividend_tax_rate(start, order["day"])
            due = cent(order["shares"] * cash_per_share * Decimal(str(rate)))
            sold += order["shares"]
            liability += due
            sale_taxes.append({"sale_day": order["day"], "shares": order["shares"], "rate": rate, "tax_liability_yuan": float(due)})
        if sold != quantity:
            raise ValueError("cash_entitled_shares_and_sales_do_not_reconcile")
        event["cash_entitlements"].append({**proof, "entitled_shares": quantity,
                                            "gross_yuan": float(credit), "tax_yuan": float(liability),
                                            "sale_tax_liabilities": sale_taxes, "actual_cash_received": False})
        gross += credit
        tax += liability
        payment_dates.append(payout)
    event.update(dividend_gross_yuan=float(gross), dividend_tax_yuan=float(tax),
                 cash_settlement_day=max(payment_dates), total_cost_including_dividend_tax_yuan=event["cost_yuan"] + float(tax))
    if max(payment_dates) > view.sessions[-1]:
        event.update(status="unresolved", reason="dividend_receivable_outside_input_horizon",
                     net_pnl_yuan=None, fixed_budget_return=None, invested_cash_return=None)
        return event
    event["price_only_net_pnl_yuan"] = event["net_pnl_yuan"]
    event["net_pnl_yuan"] += float(gross - tax)
    event["fixed_budget_return"] = event["net_pnl_yuan"] / 10000
    event["invested_cash_return"] = event["net_pnl_yuan"] / event["entry_cash_yuan"]
    event["cash_settlement_complete"] = True
    return event
