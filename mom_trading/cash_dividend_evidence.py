"""Strict cash-only action evidence for historical research settlement.

Missing numeric bonus fields are not zero: a complete, cash-only announcement
description must independently establish that no shares are distributed.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re


SPEC = {
    "schema": "cash_dividend_evidence.v1",
    "scope": "cash_only_no_rights_bonus_conversion_or_successor",
    "description": "exact_10_cash_per10_yuan_before_tax_statement",
    "dates": "announcement_on_or_before_record; record_previous_market_session; payout_not_before_ex",
    "price_identity": "announced_cash_and_adjustment_implied_reference_round_to_same_cent",
    "price_caveat": "cross_source_reconciliation_not_independent_exchange_reference_quote",
    "production_eligible": False,
    "sources": [
        "https://www.chinatax.gov.cn/n810341/n810755/c1797427/content.html",
        "https://docs.static.szse.cn/www/lawrules/rule/trade/W020230217564423808793.pdf",
    ],
}


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def cent(value):
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def cash_action_identity(action: dict, *, code: str, ex_day: str, previous_session: str) -> dict:
    result = {"verified": False, "reason": None}
    if action.get("code") != code or action.get("ex_date") != ex_day:
        return {**result, "reason": "action_security_or_ex_date_mismatch"}
    # Full match excludes mixed distributions and vague summaries. It also
    # prevents silently treating nullable bonus/transfer fields as known zeros.
    description = str(action.get("description") or "").replace("（", "(").replace("）", ")")
    match = re.fullmatch(r"10派([0-9]+(?:\.[0-9]+)?)元\(含税\)", description)
    if not match:
        return {**result, "reason": "not_explicit_cash_only_distribution"}
    for field in ("bonus_ratio", "transfer_ratio"):
        value = action.get(field)
        if value is not None and number(value) != 0:
            return {**result, "reason": "cash_description_conflicts_with_share_distribution"}
    cash = number(action.get("cash_dividend_per10"))
    if cash is None or cash <= 0 or cash != number(match[1]):
        return {**result, "reason": "cash_amount_missing_or_description_mismatch"}
    dates = [action.get(key) for key in ("announcement_date", "record_date", "ex_date", "payout_date")]
    from datetime import date
    try:
        if any(not isinstance(day, str) or date.fromisoformat(day).isoformat() != day for day in dates):
            raise ValueError
    except (ValueError, TypeError):
        return {**result, "reason": "action_dates_missing_or_invalid"}
    announcement, record, ex_date, payout = dates
    if not (announcement <= record == previous_session < ex_date <= payout):
        return {**result, "reason": "action_chronology_or_record_session_invalid"}
    return {"verified": True, "reason": "explicit_cash_only_identity",
            "code": code, "ex_date": ex_date, "record_date": record, "payout_date": payout,
            "announcement_date": announcement, "cash_per_share": float(cash / 10)}


def reconcile_cash_reference(previous: dict, current: dict, actions: list[dict], *, previous_session: str) -> dict:
    if len(actions) != 1:
        return {"verified": False, "reason": "cash_action_missing_or_not_unique", "matching_actions": len(actions)}
    identity = cash_action_identity(actions[0], code=current["code"], ex_day=current["trade_date"], previous_session=previous_session)
    if not identity["verified"]:
        return identity
    if (previous["trade_date"] != previous_session or previous["code"] != current["code"]
            or not previous.get("stable_instrument_id") or previous.get("stable_instrument_id") != current.get("stable_instrument_id")):
        return {"verified": False, "reason": "cash_action_market_identity_gap"}
    prior_close, prior_factor, factor = [number(value) for value in (previous.get("close"), previous.get("adj_factor"), current.get("adj_factor"))]
    if (any(value is None or value <= 0 for value in (prior_close, prior_factor, factor))
            or previous.get("adjustment_trusted") is not True or current.get("adjustment_trusted") is not True
            or number(current.get("prev_close")) != prior_close):
        return {"verified": False, "reason": "cash_action_price_or_adjustment_identity_invalid"}
    theoretical = cent(prior_close - number(identity["cash_per_share"]))
    implied = cent(prior_close * prior_factor / factor)
    if theoretical <= 0 or implied != theoretical:
        return {"verified": False, "reason": "announced_cash_does_not_reconcile_price_factor",
                "theoretical_reference": float(theoretical), "factor_implied_reference": float(implied)}
    return {**identity, "reason": "cash_identity_and_reference_reconciled",
            "theoretical_reference": float(theoretical), "factor_implied_reference": float(implied),
            "original_factor": float(factor), "previous_factor": float(prior_factor)}
