"""Reproduce unresolved corporate paths and expose missing settlement evidence.

This is an input audit, not a split-adjusted replay. Announced proportions do
not establish credited whole shares, their sellable date, or their tax basis.
"""
from collections import Counter
from datetime import date
from decimal import Decimal
import hashlib
import json
import re

from .cash_dividend_evidence import number
from .crowding_cash_settlement import cash_market_view
from .emotion_pattern_replay import _price_problem
from research.run_emotion_control_cash_settlement import repair_control

REASON = "corporate_action_cash_and_share_ledger_missing"
SPEC = {"schema": "control_corporate_evidence_audit.v1",
        "population": "all_unresolved_corporate_cash_paths_all_three_costs",
        "baseline": "exact_original_price_and_cash_reproduction_before_diagnosis",
        "action_grain": "security_and_first_blocking_ex_date",
        "entitlement": "record_close_inventory_from_unchanged_order_prefix",
        "missing_dates": "never_substitute_ex_date_for_share_listing_date",
        "fractional_shares": "no_rounding_without_allocation_evidence",
        "tax": "no_assumed_tax_free_transfer_or_bonus_face_value",
        "repaired_returns_computed": False, "production_eligible": False, "generates_alert": False}
REASONS = {
    "not_explicit_cash_only_distribution": "涉及送股、转增或未能确认的分配结构",
    "announced_cash_does_not_reconcile_price_factor": "公告现金与价格因子不一致",
    "cash_action_missing_or_not_unique": "公司行动摘要缺失或不唯一",
}
REQUIREMENTS = {
    "action_identity": "核实唯一的实施公告、证券与登记日身份",
    "distribution_terms": "补齐送股、转增及税前现金比例，核对完整分配说明",
    "reference_price": "核对除权参考价与原始价格因子；不能只因接近而放行",
    "share_listing": "补充新增无限售股份上市日期；不能用除权日代替",
    "implementation_and_tax": "核对实施公告原件、上市安排、转增来源或送股计税依据",
    "cash_payment": "核对现金红利实际发放安排",
    "fractional_allocation": "补齐零碎股分配依据；不能逐账户自行四舍五入",
    "share_replay": "完成原始股数、可卖库存、税费与价格边界的独立复算",
}


def iso_day(value):
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except (ValueError, TypeError):
        return False


def distribution_terms(action):
    """Nullable fields are known zero only when a complete description proves it."""
    text = str(action.get("description") or "").replace("（", "(").replace("）", ")")
    value = r"([0-9]+(?:\.[0-9]+)?)"
    match = re.fullmatch(r"10(?:送" + value + r"股)?(?:转增" + value + r"股)?(?:派" + value + r"元\(含税\))?", text)
    if not match or not any(match.groups()):
        return None
    fields = ("bonus_ratio", "transfer_ratio", "cash_dividend_per10")
    parsed = {key: Decimal(raw or "0") for key, raw in zip(fields, match.groups())}
    if not any(parsed.values()):
        return None
    for key, expected in parsed.items():
        observed = action.get(key)
        # A positive announced quantity must also match the numeric source.
        if (observed is None and expected != 0) or (observed is not None and number(observed) != expected):
            return None
    return parsed


def audit_event(old_price, old_cash, market, evidence, response_ref):
    if old_cash.get("status") != "unresolved" or old_cash.get("reason") != REASON:
        raise ValueError("corporate_audit_not_unresolved_action")
    if repair_control(old_price, market, evidence, source_complete=True) != old_cash:
        raise ValueError("corporate_audit_prior_cash_not_reproduced")
    view, _ = cash_market_view(market, evidence)
    source = view.rows[old_cash["signal_day"]]
    for day in view.sessions[view.index[old_cash["signal_day"]] + 1:]:
        if day not in view.rows:
            raise ValueError("corporate_audit_missing_prior_market_day")
        reason = _price_problem(view.rows[day], source)
        if reason:
            break
    else:
        raise ValueError("corporate_audit_blocking_day_not_found")
    if reason != REASON or day not in evidence:
        raise ValueError("corporate_audit_blocking_identity_mismatch")
    proof = evidence[day]
    rows = proof["official_actions"]
    action = rows[0] if len(rows) == 1 else {}
    previous = view.sessions[view.index[day] - 1]
    valid_identity = (action.get("code") == old_cash["code"] and action.get("ex_date") == day
                      and iso_day(action.get("announcement_date"))
                      and action.get("record_date") == previous
                      and action["announcement_date"] <= previous)
    terms = distribution_terms(action) if valid_identity else None
    requirements = []
    if not valid_identity:
        requirements.append("action_identity")
    if terms is None:
        requirements.append("distribution_terms")
    share_action = terms is not None and terms["bonus_ratio"] + terms["transfer_ratio"] > 0
    arrival = action.get("share_arrival_date")
    if share_action:
        if not iso_day(arrival) or arrival < day:
            requirements.append("share_listing")
        requirements.extend(["implementation_and_tax", "share_replay"])
    if terms is not None and terms["cash_dividend_per10"] > 0:
        payout = action.get("payout_date")
        if not iso_day(payout) or payout < day:
            requirements.append("cash_payment")
    if proof.get("reason") == "announced_cash_does_not_reconcile_price_factor":
        requirements.append("reference_price")
    inventory = None
    if valid_identity:
        inventory = sum(order["shares"] * (1 if order["side"] == "buy" else -1)
                        for order in old_cash["execution_events"] if order["day"] <= previous)
        if type(inventory) is not int or inventory < 0 or inventory != old_cash["remaining_shares"]:
            raise ValueError("corporate_audit_record_inventory_mismatch")
    announced_new = (Decimal(inventory) * (terms["bonus_ratio"] + terms["transfer_ratio"]) / 10
                     if share_action and inventory is not None else None)
    fractional = announced_new is not None and announced_new != announced_new.to_integral_value()
    if fractional:
        requirements.append("fractional_allocation")
    entered = old_cash["entry_day"] is not None
    if not entered and (old_cash["execution_events"] or old_cash["remaining_shares"] or day != view.sessions[view.index[old_cash["signal_day"]] + 1]):
        raise ValueError("corporate_audit_pre_entry_identity_invalid")
    record = {key: old_cash[key] for key in ("track", "code", "signal_day", "playbook_id", "playbook_name", "signal_emotion", "slippage_bps")}
    record.update(action_key=old_cash["code"] + ":" + day, failure_day=day,
                  phase="held_inventory" if entered else "before_entry", record_inventory_shares=inventory,
                  announced_new_shares_exact=str(announced_new) if announced_new is not None else None,
                  fractional_entitlement=fractional, requirements=sorted(set(requirements)),
                  original_cash_path_sha256=hashlib.sha256(json.dumps(old_cash, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest())
    action_record = {"action_key": record["action_key"], "code": old_cash["code"], "ex_date": day,
                     "reason": proof["reason"], "reason_label": REASONS.get(proof["reason"], "公司行动证据未核验"),
                     "proof": proof, "response": response_ref,
                     "announced_terms_per10": {k: str(v) for k, v in terms.items()} if terms else None,
                     "reported_share_arrival_date": arrival, "has_share_distribution": share_action if terms else None,
                     "settlement_ready": False}
    return record, action_record


def summarize(records, actions):
    # Cross-track and cost-scenario duplicates are explicitly not independent samples.
    rows = [r for r in records if r["slippage_bps"] == 20]
    reasons = Counter(actions[r["action_key"]]["reason"] for r in rows)
    gaps = Counter(gap for r in rows for gap in r["requirements"])
    return {"spec": SPEC, "cost_paths": len(records), "reference_slippage_bps": 20,
            "cross_track_records": len(rows), "unique_actions": len(actions),
            "before_entry_records": sum(r["phase"] == "before_entry" for r in rows),
            "held_records": sum(r["phase"] == "held_inventory" for r in rows),
            "reason_counts": [{"reason": reason, "label": REASONS.get(reason, "公司行动证据未核验"), "records": count} for reason, count in sorted(reasons.items())],
            "requirement_counts": [{"requirement": gap, "label": REQUIREMENTS[gap], "records": count} for gap, count in sorted(gaps.items())],
            "interpretation": "只复现与定位缺口；尚未入场不等于零收益，已持仓不等于新增股份可卖。仍不计算修正收益、不新增策略或因子资格。"}
