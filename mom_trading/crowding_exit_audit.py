"""Separate failed liquidation from missing evidence, without changing returns."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import math


SPEC = {
    "schema": "crowding_fixed_deadline_exit_audit.v1",
    "scope": "unchanged_entry_plus_5_through_plus_10_open_exit_attempts",
    "independence": "recompute_blocked_open_from_canonical_status_and_cent_price_bounds",
    "source_caveat": "same_canonical_data_lineage_not_an_independent_exchange_feed",
    "outcome": "known_open_inventory_is_not_missing_data_or_zero_recovery",
    "valuation": "not_performed; remaining_shares_retained; no_fabricated_sale",
    "frozen_admission": "any_selected_deadline_inventory_fails_all_selected_exits_resolved",
    "historical_results_already_seen": True, "new_blind_trial": False,
    "production_eligible": False, "generates_alert": False,
}


def evidence_class(event: dict) -> str:
    status, reason = event["status"], event["reason"]
    if status == "closed":
        return "settled_cash"
    if status == "unfilled":
        return "known_no_entry"
    if status != "unresolved":
        raise ValueError("exit_audit_unknown_event_status")
    if reason == "exit_deadline_with_inventory":
        return "known_open_inventory"
    if reason == "corporate_action_cash_and_share_ledger_missing":
        return "missing_corporate_entitlements"
    return "other_unresolved_evidence"


def audit_blocked_exits(event: dict, canonical: dict, sessions: list[str], cash_proofs: dict) -> dict:
    """Narrow audit of wholly unsold positions; partial fills need another audit.

    Never call the production auction gate to prove its own outcome. The runner
    separately reproduces the original path, including cash and ADV repairs.
    """
    if evidence_class(event) != "known_open_inventory":
        raise ValueError("exit_audit_not_deadline_inventory")
    start = sessions.index(event["signal_day"]) + 1
    days = sessions[start+5:start+11]
    orders = event["execution_events"]
    if (len(days) != 6 or event["entry_day"] != sessions[start] or event["exit_day"] is not None
            or len(orders) != 7 or orders[0]["side"] != "buy" or orders[0]["day"] != sessions[start]
            or event["remaining_shares"] != event["entry_shares"] or event["entry_shares"] <= 0
            or orders[0]["shares"] != event["entry_shares"]
            or any(o["side"] != "sell" or o["shares"] != 0 for o in orders[1:])
            or [o["day"] for o in orders[1:]] != days):
        raise ValueError("exit_audit_inventory_or_deadline_mismatch")
    if any(event.get(k) is not None for k in ("net_pnl_yuan", "fixed_budget_return", "invested_cash_return")):
        raise ValueError("exit_audit_unliquidated_return_present")
    entry = canonical[sessions[start]]
    stable = entry["stable_instrument_id"]
    code = event["code"]
    for day in sessions[start:start+11]:
        row = canonical[day]
        if (not stable or row["stable_instrument_id"] != stable
                or row["ts_code"].split(".")[0] != code):
            raise ValueError("exit_audit_canonical_identity_mismatch")
    buy = orders[0]
    costs = buy["costs"]
    if (entry["trade_status"] != 1 or buy["price"] != entry["open"]
            or not math.isclose(costs["total"], sum(v for k, v in costs.items() if k != "total"), abs_tol=1e-9)
            or not math.isclose(event["cost_yuan"], costs["total"], abs_tol=1e-9)
            or not math.isclose(event["entry_cash_yuan"], buy["price"]*buy["shares"]+costs["total"], abs_tol=1e-8)):
        raise ValueError("exit_audit_entry_cash_mismatch")
    attempts = []
    for offset, order in enumerate(orders[1:]):
        day = days[offset]
        row = canonical[day]
        if row["trade_status"] == 0:
            if any(value is not None and not math.isnan(value) and value != 0 for value in (row["vol"], row["amount"])):
                raise ValueError("exit_audit_suspension_conflicts_with_trades")
            checked = {"day": day, "reason": "suspended_or_untradable", "lower_limit_yuan": None}
        elif row["trade_status"] == 1:
            if row["is_st"] not in (0, 1) or not "2021-01-01" <= day <= "2025-12-31":
                raise ValueError("exit_audit_price_rule_scope_unknown")
            if code.startswith(("300", "301")):
                rate = Decimal("0.20")
            elif code.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
                rate = Decimal("0.05" if row["is_st"] == 1 else "0.10")
            else:
                raise ValueError("exit_audit_board_identity_unknown")
            previous = canonical[sessions[start+4+offset]]["close"]
            if day in cash_proofs:
                previous = cash_proofs[day]["theoretical_reference"]
            if any(not math.isfinite(v) or v <= 0 for v in (previous, row["open"])):
                raise ValueError("exit_audit_price_missing")
            lower = (Decimal(str(previous))*(1-rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if Decimal(str(row["open"])) > lower:
                raise ValueError("exit_audit_recorded_block_not_supported_by_price")
            checked = {"day": day, "reason": "limit_down_no_fill", "lower_limit_yuan": float(lower)}
        else:
            raise ValueError("exit_audit_trading_status_unknown")
        if order["reason"] != checked["reason"]:
            raise ValueError("exit_audit_recorded_reason_mismatch")
        attempts.append(checked)
    return {
        "signal_day": event["signal_day"], "code": code, "signal_emotion": event["signal_emotion"],
        "slippage_bps": event["slippage_bps"], "quintile": event["quintile"],
        "deadline": days[-1], "classification": "known_open_inventory",
        "remaining_shares": event["remaining_shares"], "entry_cash_yuan": event["entry_cash_yuan"],
        "exit_attempts": attempts, "realized_net_return": None, "inventory_value_yuan": None,
        "valuation_not_performed": True, "actual_fill": False, "production_eligible": False,
    }
