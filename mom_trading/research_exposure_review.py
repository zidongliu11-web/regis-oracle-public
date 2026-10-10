"""Shadow account exposure review for a frozen research route."""

from __future__ import annotations

from typing import Any


def build_research_exposure_review(
    route: dict[str, Any], portfolio: dict[str, Any] | None,
) -> dict[str, Any]:
    portfolio = portfolio if isinstance(portfolio, dict) else {}
    authority = {
        "research_only": True, "applied_to_execution": False,
        "changes_position_or_quantity": False, "generates_alert": False,
    }
    budget = route.get("research_budget_hypothesis") or {}
    target = budget.get("target_exposure_range_pct")
    if route.get("status") != "research_route_ready" or not (
        isinstance(target, list) and len(target) == 2
    ):
        return {"status": "route_input_incomplete", "authority": authority}
    try:
        equity = float(portfolio["account_equity"])
        market_value = float(portfolio["market_value"])
    except (KeyError, TypeError, ValueError):
        equity = market_value = -1.0
    if not (
        portfolio.get("cash_user_recorded") is True
        and portfolio.get("valuation_status") == "complete"
        and equity > 0 and market_value >= 0
    ):
        return {
            "status": "account_input_incomplete",
            "reason": "录入真实剩余现金并完成持仓估值后，才计算研究仓位差额。",
            "target_exposure_range_pct": target,
            "authority": authority,
        }
    low, high = max(0.0, float(target[0])), min(100.0, float(target[1]))
    current = market_value / equity * 100.0
    maximum_new_trades = budget.get("maximum_new_trades")
    if current > high:
        action = "reduce_review"
        delta = market_value - equity * high / 100.0
        hypothetical_delta = delta
    elif maximum_new_trades == 0:
        # A research target is not permission to increase real exposure.  When
        # the routed state explicitly allows zero new trades, keep the target
        # visible as a hypothesis but never label the gap as add capacity.
        action = "hold_or_reduce_only"
        delta = 0.0
        hypothetical_delta = max(0.0, equity * low / 100.0 - market_value)
    elif current < low:
        action = "increase_capacity_review"
        delta = equity * low / 100.0 - market_value
        hypothetical_delta = delta
    else:
        action, delta = "within_research_range", 0.0
        hypothetical_delta = 0.0
    return {
        "status": "research_exposure_review_ready",
        "market_state": route.get("market_state"),
        "current_exposure_pct": round(current, 4),
        "target_exposure_range_pct": [low, high],
        "review_action": action,
        "review_delta_yuan": round(max(0.0, delta), 2),
        "hypothetical_increase_gap_yuan": round(
            max(0.0, hypothetical_delta), 2,
        ),
        "maximum_new_trades": maximum_new_trades,
        "new_risk_allowed": maximum_new_trades != 0,
        "profitability_claim": False,
        "authority": authority,
    }
