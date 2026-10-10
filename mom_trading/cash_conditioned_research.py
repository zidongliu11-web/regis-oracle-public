"""Join a frozen cash-accounting repair to the unchanged signal-first analysis.

This consumes existing research artifacts, not new trials or account facts.
The original price-only evaluator and every source result remain unchanged.
"""
from collections import Counter
from decimal import Decimal
import json
import math

from .cash_dividend_evidence import cash_action_identity, cent, number
from .crowding_cash_settlement import dividend_tax_rate
from .emotion_pattern_cash_settlement import SPEC as CASH_SPEC
from . import conditioned_research as conditioned
from .emotion_factor_attribution import freeze_factor_groups
from .research_artifact_paths import research_artifact_path


ARTIFACTS = (
    ("output/research/emotion_pattern_event_diagnostic_v1.json",
     "ed542d5e04d6a012a300df8143832d03364b9c1461b6115217fa56c44336df55"),
    ("output/research/emotion_pattern_cash_settlement_v1.json",
     "28004e3504698a2c8d482b5b434e90688db736f358e875d50aa717a5e679d0b8"),
)
SPEC = {
    "schema": "cash_conditioned_research.v1",
    "scope": "existing_original_routed_cash_settlement_only; controls_unchanged",
    "accounting": CASH_SPEC, "analysis": conditioned.SPEC,
    "signal_identity": "all_original_keys_emotions_and_factors_unchanged_across_three_costs",
    "ledger_check": "price_cash_plus_record_day_entitlement_minus_sale_tax",
    "source_boundary": "frozen_producer_evidence; not_a_new_independent_price_or_exchange_audit",
    "historical_reuse": True, "unseen_out_of_sample": False, "new_blind_trial": False,
    "production_eligible": False, "generates_alert": False, "portfolio_return": None,
}
EVENT_KEYS = ("code", "signal_day", "playbook_id", "slippage_bps")


def same_money(actual, expected):
    value = number(actual)
    if value is None or not math.isclose(float(value), float(expected), rel_tol=0, abs_tol=1e-7):
        raise ValueError("cash_conditioned_amount_does_not_reconcile")


def reconcile_closed(event, evidence, sessions):
    """Independently total the saved orders, record inventory and reserved tax."""
    if event.get("cash_settlement_complete") is not True or event["code"] not in evidence:
        raise ValueError("cash_conditioned_settlement_incomplete")
    cash, entry_cash, fees, inventory = Decimal(0), Decimal(0), Decimal(0), 0
    orders = event["execution_events"]
    buys, sells = [], []
    previous = event["signal_day"]
    for order in orders:
        qty, side, day = order["shares"], order["side"], order["day"]
        # A recorded failed sale has zero shares and deliberately no price or
        # cost fields. Retain that failed attempt; it is not a cash transaction.
        cost = number(order.get("costs", {}).get("total", 0) if qty == 0 else order["costs"]["total"])
        if (type(qty) is not int or qty < 0 or side not in {"buy", "sell"}
                or day not in sessions or day < previous or day <= event["signal_day"]
                or cost is None or cost < 0 or (not qty and cost)):
            raise ValueError("cash_conditioned_order_invalid")
        previous = day
        if not qty:
            continue
        price = number(order["price"])
        if price is None or price <= 0:
            raise ValueError("cash_conditioned_order_invalid")
        fees += cost
        if side == "buy":
            buys.append(order)
            entry_cash += qty * price + cost
            inventory += qty
            cash -= qty * price + cost
        else:
            sells.append(order)
            inventory -= qty
            cash += qty * price - cost
        if inventory < 0:
            raise ValueError("cash_conditioned_inventory_invalid")
    if (inventory or event["remaining_shares"] or len(buys) != 1 or not sells
            or buys[0]["shares"] != event["simulated_entry_shares"]
            or buys[0]["day"] != event["entry_day"] or sells[-1]["day"] != event["exit_day"]
            or any(o["day"] <= event["entry_day"] for o in sells)
            or entry_cash > 20000):
        raise ValueError("cash_conditioned_inventory_invalid")
    gross, tax, entitlements = Decimal(0), Decimal(0), []
    payment_dates = [event["exit_day"]]
    for ex_day, proof in sorted(evidence[event["code"]].items()):
        if not event["signal_day"] < ex_day <= event["exit_day"]:
            continue
        if proof.get("verified") is not True or ex_day not in sessions or sessions.index(ex_day) == 0:
            raise ValueError("cash_conditioned_action_unverified")
        actions = proof.get("official_actions", [])
        if len(actions) != 1:
            raise ValueError("cash_conditioned_action_unverified")
        identity = cash_action_identity(actions[0], code=event["code"], ex_day=ex_day,
                                        previous_session=sessions[sessions.index(ex_day)-1])
        if not identity["verified"] or any(proof.get(k) != v for k, v in identity.items() if k != "reason"):
            raise ValueError("cash_conditioned_action_identity_mismatch")
        record = identity["record_date"]
        quantity = sum(o["shares"] * (1 if o["side"] == "buy" else -1) for o in orders if o["day"] <= record)
        if not quantity:
            continue
        per_share = number(identity["cash_per_share"])
        liabilities = []
        for order in sells:
            if order["day"] > record:
                rate = dividend_tax_rate(event["entry_day"], order["day"])
                liabilities.append({"day": order["day"], "shares": order["shares"], "rate": rate,
                                    "tax_yuan": float(cent(order["shares"] * per_share * number(rate)))})
        if sum(o["shares"] for o in liabilities) != quantity:
            raise ValueError("cash_conditioned_entitlement_inventory_invalid")
        credit = cent(quantity * per_share)
        liability = sum((number(o["tax_yuan"]) for o in liabilities), Decimal(0))
        entitlements.append({**identity, "entitled_shares": quantity, "gross_yuan": float(credit),
                             "tax_yuan": float(liability), "sale_tax_liabilities": liabilities,
                             "actual_cash_received": False})
        gross += credit
        tax += liability
        payment_dates.append(identity["payout_date"])
    saved = event.get("cash_entitlements")
    if not isinstance(saved, list) or len(saved) != len(entitlements):
        raise ValueError("cash_conditioned_entitlement_missing_or_duplicate")
    for actual, expected in zip(saved, entitlements):
        if any(actual.get(k) != v for k, v in expected.items() if k != "reason"):
            raise ValueError("cash_conditioned_entitlement_does_not_reconcile")
    if max(payment_dates) > min(sessions[-1], "2025-12-31") or event.get("cash_settlement_day") != max(payment_dates):
        raise ValueError("cash_conditioned_payment_unsettled")
    for field, expected in {
        "price_only_net_pnl_yuan": cash, "entry_cash_yuan": entry_cash, "cost_yuan": fees,
        "dividend_gross_yuan": gross, "dividend_tax_yuan": tax,
        "total_cost_including_dividend_tax_yuan": fees + tax, "net_pnl_yuan": cash + gross - tax,
        "fixed_budget_return": (cash + gross - tax) / 20000,
        "net_return_on_entry_cash": (cash + gross - tax) / entry_cash,
    }.items():
        same_money(event.get(field), expected)
    return float((cash + gross - tax) / 20000)


def prepare(original, settled, states, sessions):
    from research.run_emotion_control_cash_settlement import cash_outcomes

    if (original.get("schema") != "emotion_pattern_event_diagnostic.v1"
            or original.get("contract", {}).get("event_cash_yuan") != 20000
            or settled.get("spec") != CASH_SPEC
            or settled.get("status") != "original_pattern_cash_diagnostic_complete_not_admitted"
            or settled.get("all_original_paths_reproduced") is not True
            or settled.get("source_results_unchanged") is not True
            or any(doc.get("production_eligible") is not False or doc.get("generates_alert") is not False
                   for doc in (original, settled))):
        raise ValueError("cash_conditioned_source_contract_invalid")
    old, new = ({tuple(e[k] for k in EVENT_KEYS): e for e in doc["events"]} for doc in (original, settled))
    if (len(old) != len(original["events"]) or len(new) != len(settled["events"]) or old.keys() != new.keys()
            or len(old) != original.get("candidate_count", -1) * 3
            or len(old) != original.get("costed_event_count")
            or len(new) != settled.get("signal_count", -1) * 3 or len(new) != settled.get("cost_path_count")):
        raise ValueError("cash_conditioned_signal_population_changed")
    price_legs, ordered = [], []
    for key in old:
        event = new[key]
        if (any(event[k] != old[key][k] for k in ("playbook_name", "signal_emotion", "signal_factors"))
                or event.get("prior_status") != old[key]["status"] or event.get("prior_reason") != old[key]["reason"]
                or event.get("cash_settlement_schema") != CASH_SPEC["schema"]):
            raise ValueError("cash_conditioned_signal_identity_changed")
        if event["status"] == "closed":
            if event["code"] in settled["response_missing_codes"]:
                raise ValueError("cash_conditioned_official_response_missing")
            reconcile_closed(event, settled["action_evidence"], sessions)
            price_legs.append({**event, "net_pnl_yuan": event["price_only_net_pnl_yuan"]})
        else:
            if event.get("cash_settlement_complete") is not False or event.get("fixed_budget_return") is not None:
                raise ValueError("cash_conditioned_unknown_return_filled")
            price_legs.append(event)
        ordered.append(event)
    # Reuse the existing cash-label adapter. Its price projection verifies
    # orders only; the actual analysis receives the full cash-inclusive labels.
    inputs, outcomes = cash_outcomes(
        [{**e, "track": "original_routed"} for e in original["events"]],
        [{**e, "track": "original_routed"} for e in ordered])
    checked, _, contexts = conditioned.prepare(price_legs, states, sessions)
    if not inputs.equals(checked):
        raise ValueError("cash_conditioned_input_population_changed")
    return inputs, outcomes, contexts


def run(output, *, input_root, references, source_code, progress):
    from .research_workbench import digest
    from .emotion_pattern_research import PLAYBOOKS
    from research.run_emotion_pattern_replay import write_new

    paths = [research_artifact_path(ref["path"], project_root=input_root) for ref in references]
    def verify_inputs():
        if any(digest(path) != ref["sha256"] for path, ref in zip(paths, references)):
            raise ValueError("cash_conditioned_input_identity_changed")
    verify_inputs()
    original, settled, state_doc, calendar = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    if original.get("inputs", [])[1:3] != references[2:] or any(ref not in settled.get("bindings", []) for ref in [references[0], *references[2:]]):
        raise ValueError("cash_conditioned_source_binding_mismatch")
    progress(json.dumps({"phase": "cash_reconciliation", "events": len(settled["events"])}))
    inputs, outcomes, contexts = prepare(original, settled, state_doc["rows"], calendar["trading_dates"])
    groups = freeze_factor_groups(inputs)
    groups_path = output.with_name("factor-groups.json")
    write_new(groups_path, {"spec": SPEC, "inputs": references, "groups": groups})
    result = {"schema": SPEC["schema"], "spec": SPEC, "lane": "cash_conditioned",
              "candidate_count": len(inputs), "costed_event_count": len(settled["events"]),
              **conditioned.conditional_summary(inputs, outcomes, contexts, PLAYBOOKS),
              **conditioned.factor_summary(groups, outcomes, contexts),
              "frozen_factor_groups": groups, "group_file_sha256": digest(groups_path),
              "state_labels": conditioned.STATES, "direction_labels": conditioned.DIRECTIONS,
              "market_scope": state_doc.get("scope", "not_declared"), "inputs": references, "source_code": source_code,
              "accounting_audit": {"population_and_signal_inputs_unchanged": True,
                  "original_status_counts": dict(Counter(e["status"] for e in original["events"])),
                  "settled_status_counts": dict(Counter(e["status"] for e in settled["events"])),
                  "cash_entitled_cost_paths": sum(bool(e["cash_entitlements"]) for e in settled["events"])},
              "interpretation": "原有信号完成现金分红与税费核账后，按信号日情绪评价三类战法与五项辅助因子。未成交保留零值，未知结果不填充。",
              "admission_blockers": ["已见历史的会计修正，不是新样本外或前向验证", "本任务只读取原路由信号；另有对照组结算档案，尚未接入本任务比较",
                                     "仍需试验家族校正、完整基线对照与真实前向验证", "事件各自使用两万元预算，重叠事件不能拼成真实账户收益"],
              "production_eligible": False, "generates_alert": False, "portfolio_return": None}
    verify_inputs()
    write_new(output, result)
    return {k: v for k, v in result.items() if k not in {"daily_metrics", "frozen_factor_groups", "inputs", "source_code"}}
