"""Read-only, independent candidate capacity; never an allocation or an order.

Use the portfolio already read by the HTTP handler. No ledger reload, quotes,
factor scores, research calculation or persistence belongs in this projection.
"""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
import hashlib
import json
from math import isfinite
import re

from quantqc.ashare import board_of
from quantqc.costs import COSTS, MIN_COMMISSION_YUAN, one_side_cost_breakdown
from .account_snapshot import build_account_snapshot_identity
from .data_semantics import (
    MAX_OPEN_POSITIONS, MAX_SINGLE_POSITION_PCT, MAX_TOTAL_EXPOSURE_PCT,
    MIN_CASH_RESERVE_PCT, MAX_INDUSTRY_EXPOSURE_PCT,
    MAX_PLANNED_LOSS_PER_NEW_POSITION_PCT, MAX_PORTFOLIO_OPEN_RISK_PCT,
)

SCHEMA = "planning_account_review.v1"
BOUNDARY = dict(research_only=True, production_eligible=False, generates_alert=False,
                automatic_trading=False, suggested_shares=None, simultaneous_allocation=False)
RULE_SOURCES = [
    "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml",
    "https://edu.sse.com.cn/tib/qa/",
    "https://docs.static.szse.cn/www/lawrules/rule/trade/W020260424690713155663.pdf",
]
UNRESOLVED = [
    "战法尚未取得扣成本、样本外和前向准入；本结果不授予买入资格。",
    "下一交易日情绪、主线、实时可成交价格及除权、停牌、涨跌停须重新核验。",
    "波动率、ADV20、组合相关性、尾部压力与完整回撤约束未在本次容量核算中验证。",
    "佣金与滑点是现有模型假设，未按券商交割单校准；冲击与不能成交的损失未包含。",
    "各候选按同一账户独立计算，不能相加；跳空、跌停或流动性不足可使实际损失超过计划。",
]


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)): return None
    return float(value) if isfinite(value) else None


def timestamp(value):
    try:
        value = datetime.fromisoformat(value)
        return value if value.tzinfo is not None else None
    except (TypeError, ValueError): return None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def boundary(value):
    return all(value.get(k) is v for k, v in BOUNDARY.items() if k != "simultaneous_allocation")


def quantity_rule(code):
    """2026-07-06 effective competitive LIMIT BUY units, not historic replay.

    Only the main/GEM/STAR universe supported by the current stock source.
    STAR's minimum is 200, but its increment is ONE, not another 200 shares.
    """
    board = board_of(code)
    if board == "gemb": board = "gem"
    if board not in {"main", "gem", "star"}: return None
    return {"board": board, "minimum": 200 if board == "star" else 100,
            "increment": 1 if board == "star" else 100,
            "maximum": {"main": 1_000_000, "gem": 300_000, "star": 100_000}[board],
            "order_type": "competitive_limit_buy", "effective_from": "2026-07-06"}


def estimate(price, risk, shares):
    buy = one_side_cost_breakdown(price * shares, "buy")
    sell = one_side_cost_breakdown(risk * shares, "sell")
    return {"shares": shares, "notional_yuan": price * shares,
            "buy_cost": buy, "risk_exit_cost": sell,
            "cash_required_yuan": price * shares + buy["total"],
            "planned_loss_yuan": (price-risk)*shares + buy["total"] + sell["total"]}


def _account(portfolio, regime, risk_context, now):
    identity = build_account_snapshot_identity(portfolio)
    gaps = []
    cash, equity, held = (number(portfolio.get(k)) for k in ("available_cash", "account_equity", "market_value"))
    if portfolio.get("cash_user_recorded") is not True: gaps.append("尚未明确录入真实现金。")
    if portfolio.get("valuation_status") != "complete": gaps.append("持仓估值不完整，不能用零估值代替。")
    if portfolio.get("account_snapshot_concurrent_change") is True: gaps.append("读取期间账本发生变化，须重新读取。")
    if identity.get("account_snapshot_status") != "ready":
        gaps.append("账户快照身份未齐备；请核对持仓、当日可卖数量及账本版本，不等同于未录现金。")
    if identity.get("immutable_account_snapshot_sha256") != portfolio.get("immutable_account_snapshot_sha256"):
        gaps.append("账户快照身份与本次读取内容不一致。")
    decision = timestamp(portfolio.get("decision_as_of")); cash_at = timestamp(portfolio.get("cash_as_of"))
    if not decision or not 0 <= (now-decision).total_seconds() <= 120: gaps.append("账户读取时点无效或已超过两分钟。")
    if not cash_at or not 0 <= (now-cash_at).total_seconds() < 86400: gaps.append("现金确认时点缺失、晚于现在或已超过24小时。")
    if any(x is None or x < 0 for x in (cash, equity, held)) or not equity or abs(cash+held-equity) > .02:
        gaps.append("现金、持仓市值与权益金额无效或不一致。")
    rows = portfolio.get("positions")
    if not isinstance(rows, list): rows = []; gaps.append("持仓事实列表缺失。")
    industries, unknown, total, held_codes = {}, 0.0, 0.0, set()
    for row in rows:
        value, shares, price = (number(row.get(k)) for k in ("market_value", "shares", "current_price"))
        quote_at = timestamp(row.get("quote_as_of"))
        if (not row.get("code") or value is None or value < 0 or shares is None or shares <= 0
                or not shares.is_integer() or price is None or price <= 0 or abs(shares*price-value) > .02
                or not quote_at or quote_at > now):
            gaps.append("持仓数量、价格、估值或报价时点无效。")
            continue
        held_codes.add(row["code"]); total += value
        industry = row.get("industry")
        if isinstance(industry, str) and industry.strip(): industries[industry] = industries.get(industry, 0) + value
        else: unknown += value
    if held is None or abs(total-held) > .02 or portfolio.get("position_count") != len(rows):
        gaps.append("持仓明细与账户汇总不一致。")
    ceiling = number(regime.get("observed_target_exposure_pct"))
    if ceiling is None or not 0 <= ceiling <= 100: gaps.append("市场风险仓位上限缺失，不用研究默认仓位代替。")
    open_risk = number(risk_context.get("existing_open_risk_yuan"))
    if risk_context.get("status") != "complete" or risk_context.get("open_risk_unknown_codes") or open_risk is None or open_risk < 0:
        gaps.append("已有持仓的有效风险线与组合计划损失未核全。")
    if risk_context.get("priority_exit_review") is True:
        gaps.append("已有持仓触发减仓或离场复核，须先处理风险，不推算新仓容量。")
    budgets = None
    if not gaps:
        budgets = {
            "cash_after_reserve_yuan": max(0, cash-equity*MIN_CASH_RESERVE_PCT/100),
            "total_exposure_headroom_yuan": max(0, equity*min(ceiling, MAX_TOTAL_EXPOSURE_PCT)/100-held),
            "single_position_ceiling_yuan": equity*MAX_SINGLE_POSITION_PCT/100,
            "industry_worst_case_headroom_yuan": max(0, equity*MAX_INDUSTRY_EXPOSURE_PCT/100-max(industries.values(), default=0)-unknown),
            "new_position_loss_ceiling_yuan": equity*MAX_PLANNED_LOSS_PER_NEW_POSITION_PCT/100,
            "portfolio_remaining_loss_yuan": max(0, equity*MAX_PORTFOLIO_OPEN_RISK_PCT/100-open_risk),
        }
    cash_deadline = cash_at+timedelta(hours=24) if cash_at else None
    until = min([now+timedelta(seconds=60)] + ([cash_deadline] if cash_deadline and cash_deadline > now else []))
    return identity, list(dict.fromkeys(gaps)), budgets, held_codes, until


def build(scan, portfolio, regime, risk_context, *, now):
    identity, gaps, budgets, held_codes, until = _account(portfolio, regime, risk_context, now)
    if not regime.get("date") or regime["date"] != (scan.get("emotion") or {}).get("data_as_of"):
        gaps.append("市场仓位证据与候选情绪参考日未对齐，不沿用其他日期的风险预算。")
        budgets = None
    meta = scan.get("scan_snapshot") or {}
    ttl = number(meta.get("valid_for_seconds"))
    result = {"schema": SCHEMA, "checked_at": now.isoformat(), "account_identity": identity,
              "account_gaps": gaps, "budgets": budgets, "rows": [], "rule_sources": RULE_SOURCES,
              "market_reference_day": regime.get("date"),
              "cost_assumptions": {**COSTS, "minimum_commission_yuan": MIN_COMMISSION_YUAN,
                                   "broker_calibrated": False, "market_impact_modeled": False},
              "unresolved": UNRESOLVED, **BOUNDARY}
    valid = (scan.get("schema") == "playbook_candidate_scan.v1" and meta.get("current") is True
             and ttl is not None and ttl > 0 and boundary(scan))
    if not valid:
        return {**result, "status": "scan_unavailable", "valid_for_seconds": 0, "valid_until": now.isoformat()}
    until = min(until, now+timedelta(seconds=ttl))
    for version in scan.get("versions", []):
        if (version.get("status") != "checked" or version.get("scope_complete") is not True
                or version.get("checked_members") != version.get("total_members")
                or not all(re.fullmatch(r"[a-f0-9]{64}", str(version.get(k) or "")) for k in ("spec_id", "rule_identity"))
                or not boundary(version.get("environment") or {})
                or (version.get("environment") or {}).get("emotion_day") != scan.get("emotion", {}).get("data_as_of")
                or (version.get("environment") or {}).get("status") != "environment_matched_shape_pending"): continue
        for row in version.get("rows", []):
            plan = row.get("research_plan") or {}; terms = plan.get("entry_terms") or {}
            expiry = timestamp(plan.get("expires_at"))
            if (row.get("checked") is not True or row.get("shape") is not True
                    or row.get("status") != "research_conditions_met_execution_pending"
                    or plan.get("status") != "conditional_research_not_order"
                    or not boundary(plan)
                    or plan.get("account_evaluated") is not False
                    or not row.get("price_input_identity") or plan.get("input_identity") != row["price_input_identity"]
                    or plan.get("spec_id") != version.get("spec_id") or plan.get("rule_identity") != version.get("rule_identity")
                    or row.get("signal_day") != scan.get("emotion", {}).get("data_as_of")
                    or plan.get("signal_day") != row.get("signal_day")
                    or (row.get("timing") or {}).get("status") != "next_open_pending"
                    or (row.get("timing") or {}).get("expires_at") != plan.get("expires_at")
                    or not expiry or expiry <= now): continue
            rule = quantity_rule(row["stock_code"])
            interval = terms.get("entry_range") or {}
            upper, risk, cap = number(interval.get("upper_inclusive")), number(terms.get("risk_line")), number(terms.get("maximum_entry_open"))
            if (not rule or now.date().isoformat() < rule["effective_from"]
                    or terms.get("status") != "within_signal_risk_boundary"
                    or upper is None or risk is None or cap is None or not 0 < risk < upper <= cap
                    or number(interval.get("lower_exclusive")) != risk or not isfinite(upper*rule["maximum"])): continue
            # A boundary can lie between valid ticks: round DOWN, never pay above it.
            price = float(Decimal(str(upper)).quantize(Decimal("0.01"), rounding=ROUND_FLOOR))
            if price <= risk: continue
            minimum = estimate(price, risk, rule["minimum"])
            binding = {"spec_id": version["spec_id"], "rule_identity": version["rule_identity"],
                       "stock_code": row["stock_code"], "input_identity": plan["input_identity"],
                       "expires_at": plan["expires_at"], "entry_terms": terms}
            blocked = list(gaps)
            if row["stock_code"] in held_codes: blocked.append("已经持有该股，当前战法没有经过验证的加仓协议。")
            if len(held_codes) >= MAX_OPEN_POSITIONS: blocked.append("已达持仓标的数量上限。")
            def exceeded(shares):
                cost = estimate(price, risk, shares)
                return [label for label, value, limit in (
                    ("含买入成本的现金储备约束", cost["cash_required_yuan"], budgets["cash_after_reserve_yuan"]),
                    ("市场总仓位余量", cost["notional_yuan"], budgets["total_exposure_headroom_yuan"]),
                    ("单股仓位上限", cost["notional_yuan"], budgets["single_position_ceiling_yuan"]),
                    ("行业最坏情形预留", cost["notional_yuan"], budgets["industry_worst_case_headroom_yuan"]),
                    ("单笔含费计划损失", cost["planned_loss_yuan"], budgets["new_position_loss_ceiling_yuan"]),
                    ("组合剩余计划损失", cost["planned_loss_yuan"], budgets["portfolio_remaining_loss_yuan"]),
                ) if value > limit]
            capacity = None
            if budgets is not None and not blocked:
                lo, hi = -1, (rule["maximum"]-rule["minimum"])//rule["increment"]
                while lo < hi:
                    mid = (lo+hi+1)//2
                    if not exceeded(rule["minimum"]+mid*rule["increment"]): lo = mid
                    else: hi = mid-1
                capacity = 0 if lo < 0 else rule["minimum"]+lo*rule["increment"]
            limiting = ([] if capacity is None else ["单笔限价申报数量上限"] if capacity == rule["maximum"]
                        else exceeded(rule["minimum"] if capacity == 0 else capacity+rule["increment"]))
            result["rows"].append({"binding": binding, "status": "account_gap" if blocked else "capacity_calculated",
                "account_gaps": blocked, "quantity_rule": rule, "reference_entry_price": price,
                "limiting_constraints": limiting,
                "minimum_order": minimum, "independent_capacity_shares": capacity,
                "capacity_estimate": estimate(price, risk, capacity) if capacity is not None else None,
                "capacity_exposure_pct": price*capacity/portfolio["account_equity"]*100 if capacity is not None else None,
                "review_identity": digest([identity, binding, budgets, blocked, result["cost_assumptions"], rule]), **BOUNDARY})
            until = min(until, expiry)
    return {**result, "status": "account_gap" if gaps else "calculated_research_only",
            "valid_until": until.isoformat(), "valid_for_seconds": max(0, (until-now).total_seconds())}
