"""Profit-oriented opportunity and manual-portfolio calculations.

The module is intentionally free of Flask and network ownership.  Callers pass
the audited snapshot loader and the bounded provider helper they already use.
"""
from __future__ import annotations

import copy
import math
from datetime import datetime, time
from typing import Callable

from quantqc.costs import one_side_cost_rate, round_trip_cost_rate

from .data_semantics import (
    MAX_INDUSTRY_EXPOSURE_PCT,
    MAX_OPEN_POSITIONS,
    MAX_PLANNED_LOSS_PER_NEW_POSITION_PCT,
    MAX_PORTFOLIO_OPEN_RISK_PCT,
    MAX_SINGLE_POSITION_PCT,
    MAX_TOTAL_EXPOSURE_PCT,
    MIN_CASH_RESERVE_PCT,
)
from .signal_engine import ACCOUNT_CAPITAL
from .risk_control import calculate_candidate_dependency
from .timing_engine import get_account_cash, list_positions
from .trade_plan import build_trade_plan
from .trading_calendar import last_completed_trading_day, trading_day_status


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _planned_loss_yuan(
    entry_price: float, risk_price: float, shares: int,
) -> float:
    """Planned loss to the stated risk line, including round-trip costs.

    This is not a guaranteed maximum: A-share T+1, suspension and limit-down
    gaps can make the realised loss larger than the pre-trade plan.
    """
    if shares <= 0:
        return 0.0
    buy_notional = entry_price * shares
    sell_notional = risk_price * shares
    buy_cost = buy_notional * one_side_cost_rate(buy_notional, "buy")
    sell_cost = sell_notional * one_side_cost_rate(sell_notional, "sell")
    return (entry_price - risk_price) * shares + buy_cost + sell_cost


def _planned_loss_budget(
    item: dict, *, entry_price: float, planning_equity: float,
    maximum_target_shares: int, risk_scale: float = 1.0,
    portfolio_remaining_yuan: float | None = None,
) -> dict:
    """Cap a candidate by fixed-fraction loss to its explicit risk line."""
    level = ((item.get("levels") or {}).get("effective_risk_level") or {})
    risk_price = _finite(level.get("value"))
    bounded_scale = max(0.5, min(1.0, float(risk_scale)))
    base_risk_budget = (
        planning_equity * MAX_PLANNED_LOSS_PER_NEW_POSITION_PCT / 100.0
    )
    risk_budget = base_risk_budget * bounded_scale
    if portfolio_remaining_yuan is not None:
        risk_budget = min(risk_budget, max(0.0, portfolio_remaining_yuan))
    base = {
        "method": "portfolio_heat_planned_loss_v2",
        "policy_status": "conservative_unoptimized_risk_ceiling",
        "base_risk_budget_pct": MAX_PLANNED_LOSS_PER_NEW_POSITION_PCT,
        "risk_budget_pct": round(
            risk_budget / planning_equity * 100.0, 6,
        ) if planning_equity > 0 else 0.0,
        "risk_budget_yuan": round(risk_budget, 2),
        "drawdown_risk_scale": round(bounded_scale, 6),
        "portfolio_remaining_before_yuan": (
            round(portfolio_remaining_yuan, 2)
            if portfolio_remaining_yuan is not None else None
        ),
        "risk_line_price": risk_price,
        "risk_line_kind": level.get("kind"),
        "actual_loss_can_exceed_plan": True,
        "profit_optimality_claim": False,
    }
    if (
        risk_price is None or risk_price <= 0 or risk_price >= entry_price
        or level.get("active") is False
    ):
        return {
            **base, "status": "unavailable_invalid_risk_line",
            "max_shares": None,
        }
    shares = max(0, int(maximum_target_shares) // 100 * 100)
    while (
        shares >= 100
        and _planned_loss_yuan(entry_price, risk_price, shares)
        > risk_budget + 1e-9
    ):
        shares -= 100
    return {
        **base,
        "status": "ready",
        "max_shares": shares,
        "risk_line_distance_pct": round(
            (entry_price - risk_price) / entry_price * 100.0, 4,
        ),
    }


def _drawdown_risk_scale(risk_context: dict | None) -> dict:
    """Monotonic recovery brake; never increases risk above the base budget.

    The base budget already scales with current equity.  This second, bounded
    adjustment is deliberately conservative and must be compared in the full
    system replay before anyone describes it as return-optimal.
    """
    drawdown = _finite(((risk_context or {}).get("drawdown") or {}).get(
        "current_drawdown_pct"
    ))
    if drawdown is None:
        return {
            "status": "unavailable_not_applied", "current_drawdown_pct": None,
            "scale": 1.0, "method": "bounded_linear_recovery_brake_v1",
        }
    drawdown_loss = max(0.0, -drawdown)
    return {
        "status": "applied" if drawdown_loss > 0 else "complete_no_reduction",
        "current_drawdown_pct": round(drawdown, 4),
        "scale": round(max(0.5, 1.0 - drawdown_loss / 20.0), 6),
        "method": "bounded_linear_recovery_brake_v1",
    }


def _portfolio_market_risk_scale(risk_context: dict | None) -> dict:
    """Reduce new-trade risk when audited portfolio VaR or beta is elevated.

    The thresholds reuse the existing risk report's interpretation bands.  The
    result is deliberately monotonic: missing or low-risk data never increases
    the base risk budget, while high values can only reduce it.
    """
    var_report = (risk_context or {}).get("var") or {}
    beta_report = (risk_context or {}).get("beta") or {}
    var_95_pct = (
        _finite(
            var_report.get("var_95_account_pct")
            if var_report.get("var_95_account_pct") is not None
            else var_report.get("var_95_pct")
        )
        if var_report.get("available") is True else None
    )
    beta = (
        _finite(
            beta_report.get("account_beta")
            if beta_report.get("account_beta") is not None
            else beta_report.get("beta")
        )
        if beta_report.get("available") is True else None
    )
    account_normalized = bool(
        var_report.get("var_95_account_pct") is not None
        or beta_report.get("account_beta") is not None
    )
    scales: list[float] = []
    reasons: list[str] = []
    if var_95_pct is not None and var_95_pct >= 0:
        var_scale = min(1.0, max(0.5, 3.0 / var_95_pct)) if var_95_pct > 3 else 1.0
        scales.append(var_scale)
        if var_scale < 1:
            reasons.append(
                f"账户日VaR95 {var_95_pct:.2f}%高于3%关注线"
                if account_normalized else
                f"持仓日VaR95 {var_95_pct:.2f}%高于3%关注线"
            )
    if beta is not None:
        absolute_beta = abs(beta)
        beta_scale = (
            min(1.0, max(0.5, 1.2 / absolute_beta))
            if absolute_beta > 1.2 else 1.0
        )
        scales.append(beta_scale)
        if beta_scale < 1:
            reasons.append(
                f"账户Beta {beta:.2f}高于1.2关注线"
                if account_normalized else
                f"组合Beta {beta:.2f}高于1.2关注线"
            )
    scale = min(scales, default=1.0)
    return {
        "status": (
            "unavailable_not_applied" if not scales
            else "applied" if scale < 1 else "complete_no_reduction"
        ),
        "scale": round(scale, 6),
        "var_95_pct": var_95_pct,
        "beta": beta,
        "account_normalized": account_normalized,
        "reasons": reasons,
        "method": "audited_var_beta_new_risk_brake_v1",
        "profit_optimality_claim": False,
    }


def _candidate_dependency_risk_scale(dependency: dict | None) -> dict:
    """Conservatively reduce, never increase, a duplicated account exposure."""
    report = dependency if isinstance(dependency, dict) else {}
    correlations = [
        value for value in (
            _finite(report.get("portfolio_correlation")),
            _finite(report.get("maximum_position_correlation")),
        )
        if value is not None
    ]
    if report.get("available") is not True or not correlations:
        return {
            "status": (
                "not_applicable" if report.get("available") is True
                else "unavailable_not_applied"
            ),
            "scale": 1.0,
            "observed_correlation": None,
            "high_correlation_threshold": 0.8,
            "method": "candidate_duplicate_exposure_brake_v1",
            "reason": report.get("reason"),
            "profit_optimality_claim": False,
        }
    correlation = max(correlations)
    scale = (
        max(0.5, 1.0 - (correlation - 0.8) * 2.5)
        if correlation > 0.8 else 1.0
    )
    return {
        "status": "applied" if scale < 1 else "complete_no_reduction",
        "scale": round(scale, 6),
        "observed_correlation": round(correlation, 4),
        "portfolio_correlation": _finite(report.get("portfolio_correlation")),
        "maximum_position_correlation": _finite(
            report.get("maximum_position_correlation")
        ),
        "high_correlation_threshold": 0.8,
        "observations": report.get("observations"),
        "data_as_of": report.get("data_as_of"),
        "method": "candidate_duplicate_exposure_brake_v1",
        "reason": (
            f"候选与账户已有风险暴露相关系数 {correlation:.2f}，"
            "仅下调新增计划亏损预算"
            if scale < 1 else "未达到高重复暴露关注线"
        ),
        "profit_optimality_claim": False,
    }


def _portfolio_open_risk_context(
    portfolio: dict | None, risk_context: dict | None, planning_equity: float,
) -> dict:
    positions = (portfolio or {}).get("positions") or []
    maximum = planning_equity * MAX_PORTFOLIO_OPEN_RISK_PCT / 100.0
    if not positions:
        existing = 0.0
        status = "complete_empty_portfolio"
        unknown_codes: list[str] = []
    else:
        raw_existing = _finite((risk_context or {}).get(
            "existing_open_risk_yuan"
        ))
        unknown_codes = list((risk_context or {}).get(
            "open_risk_unknown_codes"
        ) or [])
        if raw_existing is None:
            existing = None
            status = "unavailable_not_applied"
        else:
            existing = max(0.0, raw_existing)
            status = "partial" if unknown_codes else "complete"
    # A partial total is not a usable risk budget.  Keep the known amount for
    # display, but do not let an unknown holding risk line create fictitious
    # room for another exact buy quantity.
    remaining = (
        max(0.0, maximum - existing)
        if existing is not None and not unknown_codes else None
    )
    return {
        "status": status,
        "method": "risk_line_open_risk_heat_v1",
        "maximum_pct_of_equity": MAX_PORTFOLIO_OPEN_RISK_PCT,
        "maximum_yuan": round(maximum, 2),
        "existing_open_risk_yuan": (
            round(existing, 2) if existing is not None else None
        ),
        "remaining_before_new_positions_yuan": (
            round(remaining, 2) if remaining is not None else None
        ),
        "open_risk_unknown_codes": unknown_codes,
        "profit_optimality_claim": False,
    }


def _position_industry(position: dict) -> str:
    return str(
        position.get("industry") or position.get("_industry") or ""
    ).strip()


def _candidate_industry(item: dict) -> str:
    assessment = item.get("assessment") or {}
    company = item.get("company") or {}
    return str(
        item.get("industry") or item.get("_industry")
        or company.get("industry") or assessment.get("industry") or ""
    ).strip()


def build_position_sizing_risk_context(
    portfolio: dict | None, risk_report: dict | None,
) -> dict:
    """Translate the existing risk report into inputs used by exact sizing.

    Existing open risk is measured from the current quote to the active risk
    line plus estimated exit cost. Missing quotes or risk lines remain
    explicit; they are never replaced by the entry price or zero.
    """
    positions = (portfolio or {}).get("positions") or []
    actions = (risk_report or {}).get("position_actions") or []
    by_id = {
        int(row.get("position_id")): row for row in actions
        if isinstance(row, dict) and row.get("position_id") is not None
    }
    by_code = {
        str(row.get("code") or ""): row for row in actions
        if isinstance(row, dict) and row.get("code")
    }
    existing_open_risk = 0.0
    unknown_codes: list[str] = []
    for position in positions:
        code = str(position.get("code") or "")
        try:
            position_id = int(position.get("id"))
        except (TypeError, ValueError):
            position_id = None
        action = by_id.get(position_id) if position_id is not None else None
        action = action or by_code.get(code) or {}
        current_price = _finite(position.get("current_price"))
        risk_price = _finite(action.get("effective_stop"))
        shares = int(_finite(position.get("shares")) or 0)
        if (
            current_price is None or current_price <= 0
            or risk_price is None or risk_price <= 0 or shares <= 0
        ):
            unknown_codes.append(code)
            continue
        exit_notional = risk_price * shares
        exit_cost = exit_notional * one_side_cost_rate(exit_notional, "sell")
        existing_open_risk += (
            max(0.0, current_price - risk_price) * shares + exit_cost
        )
    return {
        "status": "partial" if unknown_codes else "complete",
        "existing_open_risk_yuan": round(existing_open_risk, 2),
        "open_risk_unknown_codes": unknown_codes,
        "drawdown": copy.deepcopy((risk_report or {}).get("drawdown") or {}),
        "industry": copy.deepcopy((risk_report or {}).get("industry") or {}),
        "var": copy.deepcopy((risk_report or {}).get("var") or {}),
        "beta": copy.deepcopy((risk_report or {}).get("beta") or {}),
        "dependency": copy.deepcopy((risk_report or {}).get("dependency") or {}),
        "historical_stress": copy.deepcopy(
            (risk_report or {}).get("historical_stress") or {}
        ),
        "method": "existing_risk_report_to_position_sizing_v1",
    }


def allocate_capital_with_risk(
    items: list,
    capital: float,
    market_target_exposure_pct: float | None = None,
    portfolio: dict | None = None,
    entry_context: dict | None = None,
    *,
    risk_report_loader: Callable[..., dict],
    position_ledger_loader: Callable[[], list[dict]],
    default_account_capital: float,
) -> dict:
    """Build the manual allocation plan with the audited account risk context.

    The HTTP layer supplies its live loaders so tests and runtime ownership stay
    unchanged.  Missing risk evidence remains explicit and never invents a
    smaller risk line or a larger buy quantity.
    """
    risk_context = None
    positions = (portfolio or {}).get("positions") or []
    try:
        ledger = position_ledger_loader()
        explicit_initial_equity = _finite(
            (portfolio or {}).get("initial_equity_yuan")
        )
        has_closed_ledger = any(
            isinstance(row, dict) and row.get("status") == "closed"
            for row in ledger
        )
        if explicit_initial_equity is not None and explicit_initial_equity > 0:
            initial_capital = explicit_initial_equity
            initial_equity_status = "complete_explicit"
        elif not has_closed_ledger:
            initial_capital = float(
                (portfolio or {}).get("account_capital")
                or default_account_capital
            )
            initial_equity_status = "complete_pre_first_close_baseline"
        else:
            initial_capital = 0.0
            initial_equity_status = "missing_after_realized_history"
        risk_report = risk_report_loader(
            positions=positions,
            realized_pnl=float((portfolio or {}).get("realized_pnl") or 0),
            ledger=ledger,
            initial_capital=initial_capital,
            initial_equity_as_of=(portfolio or {}).get(
                "initial_equity_as_of"
            ),
            current_account_equity=_finite(
                (portfolio or {}).get("account_equity")
            ),
            current_account_equity_as_of=(
                (portfolio or {}).get("valuation_as_of")
                or (portfolio or {}).get("cash_as_of")
            ),
        )
        risk_context = build_position_sizing_risk_context(
            portfolio, risk_report,
        )
        risk_context["initial_equity_status"] = initial_equity_status
    except Exception as exc:
        risk_context = {
            "status": "unavailable_not_applied",
            "existing_open_risk_yuan": None,
            "open_risk_unknown_codes": [
                str(row.get("code") or "") for row in positions
            ],
            "error_type": type(exc).__name__,
        }
    plan = allocate_capital(
        items, capital, market_target_exposure_pct, portfolio, entry_context,
        risk_context,
    )
    plan["risk_budget_policy"] = {
        "method": "portfolio_heat_planned_loss_v2",
        "maximum_planned_loss_per_new_position_pct": (
            MAX_PLANNED_LOSS_PER_NEW_POSITION_PCT
        ),
        "maximum_portfolio_open_risk_pct": MAX_PORTFOLIO_OPEN_RISK_PCT,
        "maximum_industry_exposure_pct": MAX_INDUSTRY_EXPOSURE_PCT,
        "drawdown_can_only_reduce_risk": True,
        "historical_var_and_beta_can_only_reduce_risk": True,
        "candidate_dependency_can_only_reduce_risk": True,
        "candidate_dependency_minimum_common_days": 60,
        "requires_valid_risk_line_for_exact_buy_quantity": True,
        "includes_round_trip_costs": True,
        "actual_loss_can_exceed_plan": True,
        "requires_stable_initial_equity_after_first_realized_trade": True,
        "initial_equity_status": (
            (risk_context or {}).get("initial_equity_status")
        ),
        "profit_optimality_claim": False,
    }
    return plan


def _timestamp(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo is not None else None


def _sellable_as_of_is_current(value, reference: datetime | None) -> bool:
    if reference is None or not isinstance(value, str) or not value.strip():
        return False
    raw = value.strip()
    if len(raw) == 10:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date() == reference.date()
        except ValueError:
            return False
    parsed = _timestamp(raw)
    return bool(
        parsed is not None
        and parsed <= reference
        and parsed.date() == reference.date()
    )


def _sellable_inventory_missing_codes(
    positions: list[dict], reference_as_of,
) -> list[str]:
    if not positions:
        return []
    reference = _timestamp(reference_as_of)
    missing: list[str] = []
    for row in positions:
        code = str(row.get("code") or "")
        shares = _finite(row.get("shares"))
        sellable = _finite(row.get("sellable_shares_snapshot"))
        valid = bool(
            reference is not None
            and shares is not None and shares >= 0 and shares.is_integer()
            and sellable is not None and sellable >= 0 and sellable.is_integer()
            and sellable <= shares
            and _sellable_as_of_is_current(row.get("sellable_as_of"), reference)
        )
        if not valid:
            missing.append(code)
    return sorted(set(missing))


ENTRY_RULE_ID = "previous_close_gap_2pct_no_chase_v1"
ENTRY_GAP_CAP_PCT = 0.02


def entry_timing_status(
    *, current_price, open_price, previous_close, session_executable: bool,
) -> dict[str, object]:
    """Return the one shared, backtest-aligned manual entry condition."""
    current = _finite(current_price)
    opening = _finite(open_price)
    reference = _finite(previous_close)
    limit_price = (
        math.floor(reference * (1.0 + ENTRY_GAP_CAP_PCT) * 100.0 + 1e-9) / 100.0
        if reference is not None and reference > 0 else None
    )
    base = {
        "rule": ENTRY_RULE_ID,
        "maximum_overnight_gap_pct": ENTRY_GAP_CAP_PCT * 100.0,
        "previous_close": reference,
        "open_price": opening,
        "current_price": current,
        "limit_price": limit_price,
        "actionable": False,
    }
    if not session_executable:
        return {**base, "status": "awaiting_best_window",
                "reason": "非预定买入窗口，不生成买入动作"}
    if limit_price is None or opening is None or opening <= 0 or current is None or current <= 0:
        return {**base, "status": "market_data_unavailable",
                "reason": "未复权昨收、今开或当前价不完整"}
    opening_gap_pct = (opening / reference - 1.0) * 100.0
    base["opening_gap_pct"] = opening_gap_pct
    if opening > limit_price:
        return {**base, "status": "cancelled_gap_up",
                "reason": "开盘高开超过2%，当日不追价、不顺延"}
    if current > limit_price:
        return {**base, "status": "cancelled_price_above_cap",
                "reason": "当前价已超过2%买入上限，不追价"}
    return {**base, "status": "price_condition_met", "actionable": True,
            "reason": "开盘与当前价均未超过昨收2%上限"}


def opportunity_market_evidence(
    candidate: dict,
    price: float | None = None,
    account_capital: float = ACCOUNT_CAPITAL,
    *,
    snapshot_loader: Callable,
    unavailable_exception: type[Exception] | tuple[type[Exception], ...],
) -> dict[str, object]:
    """Rank one candidate using audited returns, risk, cost and liquidity."""
    code = str(candidate.get("code") or "")
    candles, metadata = [], {}
    try:
        candles, metadata = snapshot_loader(code, limit=80)
    except unavailable_exception:
        pass

    raw_closes = [float(row.close) for row in candles if _finite(row.close) is not None]
    return_index = metadata.get("return_index_closes")
    closes = (
        [float(value) for value in return_index if _finite(value) is not None]
        if isinstance(return_index, list) else raw_closes
    )
    trailing_20d = (
        (closes[-1] / closes[-21] - 1.0) * 100.0
        if len(closes) >= 21 and closes[-21] > 0 else None
    )
    recent = closes[-60:]
    drawdown_60d = None
    if recent:
        peak, max_drawdown = recent[0], 0.0
        for close in recent:
            peak = max(peak, close)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - close) / peak)
        drawdown_60d = max_drawdown * 100.0

    returns = [
        recent[index] / recent[index - 1] - 1.0
        for index in range(1, len(recent)) if recent[index - 1] > 0
    ]
    annualized_volatility = None
    if len(returns) >= 2:
        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
        annualized_volatility = math.sqrt(max(0.0, variance)) * math.sqrt(252.0) * 100.0

    amounts = [
        float(row.amount) for row in candles[-20:]
        if _finite(row.amount) is not None and float(row.amount) > 0
    ]
    avg_amount_20d = (
        sum(amounts) / len(amounts) if amounts
        else _finite(candidate.get("avg_amount_20d"))
    )
    comparison_price = _finite(price)
    if comparison_price is None or comparison_price <= 0:
        comparison_price = closes[-1] if closes else _finite(candidate.get("close"))
    deployable_pct = min(
        float(MAX_TOTAL_EXPOSURE_PCT), 100.0 - float(MIN_CASH_RESERVE_PCT),
    )
    target_notional = account_capital * min(
        float(MAX_SINGLE_POSITION_PCT) / 100.0,
        deployable_pct / 100.0 / max(1, int(MAX_OPEN_POSITIONS)),
    )
    comparison_shares = (
        int(target_notional // (comparison_price * 100)) * 100
        if comparison_price is not None and comparison_price > 0 else 0
    )
    comparison_notional = comparison_shares * comparison_price if comparison_shares else None
    cost_pct = round_trip_cost_rate(comparison_notional) * 100.0 if comparison_notional else None
    participation = (
        comparison_notional / avg_amount_20d * 100.0
        if comparison_notional and avg_amount_20d and avg_amount_20d > 0 else None
    )

    predicted = _finite(candidate.get("pred_ret_20d"))
    expected_valid = bool(
        candidate.get("source", "kronos") == "kronos"
        and candidate.get("validation_status") == "validated"
        and candidate.get("freshness_status") == "fresh"
        and candidate.get("decision_eligible") is True
        and candidate.get("research_only") is False
        and predicted is not None
    )
    expected = predicted if expected_valid else None
    clamp = lambda value: max(0.0, min(100.0, value))
    # A realised 20-day move is useful context, but it is not an expected
    # return. Keep forecast evidence neutral unless independently validated,
    # while scoring the realised path separately with an explicit overheat
    # penalty so 50%+ runners cannot dominate by momentum alone.
    return_score = (
        clamp(50.0 + 4.0 * expected) if expected is not None else 50.0
    )
    if trailing_20d is None:
        trend_score = 50.0
    elif trailing_20d <= 20.0:
        trend_score = clamp(50.0 + 2.0 * trailing_20d)
    else:
        trend_score = clamp(90.0 - 2.0 * (trailing_20d - 20.0))
    drawdown_score = clamp(100.0 - 3.0 * drawdown_60d) if drawdown_60d is not None else 50.0
    volatility_score = clamp(100.0 - 1.5 * annualized_volatility) if annualized_volatility is not None else 50.0
    if avg_amount_20d and avg_amount_20d > 0:
        amount_score = clamp((math.log10(avg_amount_20d) - 6.0) * 25.0)
        participation_score = clamp(100.0 - participation * 50.0) if participation is not None else 50.0
        tradability_score = (amount_score + participation_score) / 2.0
    else:
        tradability_score = 50.0
    cost_score = clamp(100.0 - cost_pct * 50.0) if cost_pct is not None else 50.0
    factor_signal = candidate.get("factor_signal") if isinstance(candidate.get("factor_signal"), dict) else {}
    factor_z = _finite(factor_signal.get("factor_z"))
    factor_score = clamp(50.0 + 20.0 * factor_z) if factor_z is not None else 50.0
    weights = {
        "return_evidence": 20, "trend_quality": 10,
        "drawdown": 20, "volatility": 10,
        "tradability": 20, "round_trip_cost": 10, "production_factor": 10,
    }
    scores = {
        "return_evidence": return_score, "trend_quality": trend_score,
        "drawdown": drawdown_score,
        "volatility": volatility_score, "tradability": tradability_score,
        "round_trip_cost": cost_score, "production_factor": factor_score,
    }
    labels = {
        "return_evidence": (
            "已验证20日预期收益" if expected_valid
            else "无已验证收益预测（中性，不参与追涨排序）"
        ),
        "trend_quality": "20日走势质量（过热惩罚）",
        "drawdown": "历史60日最大回撤", "volatility": "历史年化波动",
        "tradability": "20日成交额与统一名义仓位参与率",
        "round_trip_cost": "统一名义仓位往返全成本", "production_factor": "已验证生产因子信号",
    }
    components = {
        key: {
            "label": labels[key], "score": round(scores[key], 2),
            "weight_pct": weight,
            "contribution": round(scores[key] * weight / 100.0, 2),
        }
        for key, weight in weights.items()
    }
    rank_score = sum(row["contribution"] for row in components.values())
    return {
        "method": "opportunity_quality_v2_balanced", "stage": "market_pre_rank",
        "rank_score": round(rank_score, 2),
        "expected_return_20d_pct": round(expected, 2) if expected is not None else None,
        "expected_return_status": (
            "validated" if expected_valid else "unavailable_unvalidated"
            if candidate.get("source", "kronos") == "kronos" and predicted is not None
            else "unavailable_no_validated_forecast"
        ),
        "ranking_return_basis": (
            "validated_expected_return" if expected_valid
            else "unvalidated_return_neutral"
        ),
        "trailing_20d_pct": round(trailing_20d, 2) if trailing_20d is not None else None,
        "max_drawdown_60d_pct": round(drawdown_60d, 2) if drawdown_60d is not None else None,
        "annualized_volatility_pct": round(annualized_volatility, 2) if annualized_volatility is not None else None,
        "avg_amount_20d_yuan": round(avg_amount_20d, 2) if avg_amount_20d is not None else None,
        "comparison_role": "fixed_research_cost_liquidity_baseline_not_user_account",
        "comparison_capital_yuan": round(float(account_capital), 2),
        "comparison_shares": comparison_shares,
        "comparison_notional_yuan": round(comparison_notional, 2) if comparison_notional else None,
        "round_trip_cost_pct": round(cost_pct, 4) if cost_pct is not None else None,
        "participation_pct": round(participation, 4) if participation is not None else None,
        "data_as_of": metadata.get("as_of") or candidate.get("source_data_as_of"),
        "data_source": metadata.get("source") or candidate.get("source"),
        "price_return_basis": (
            metadata.get("return_index_basis") or "raw_close_unadjusted"
        ),
        "corporate_action_safe_returns": bool(
            metadata.get("corporate_action_safe_returns")
        ),
        "soft_evidence_missing": not bool(candles),
        "production_factor_signal": ({
            key: factor_signal.get(key) for key in (
                "action", "score", "factor_z", "momentum_z", "factor_as_of",
                "execution_eligible", "reason",
            ) if key in factor_signal
        } if factor_signal else None),
        "components": components,
    }


def refine_opportunity_quality(quality: dict, assessment: dict | None) -> dict:
    result = copy.deepcopy(quality)
    market_score = _finite(result.get("rank_score")) or 50.0
    professional_score = _finite(
        ((((assessment or {}).get("gates") or {}).get("composite") or {})
         .get("composite"))
    )
    if professional_score is None:
        rank_score = market_score
        deep_components = {
            "market_evidence": {
                "score": round(market_score, 2),
                "role": "temporary_pre_rank_until_professional_assessment",
            },
        }
        method = "opportunity_quality_v2_balanced"
    else:
        # 全市场轻筛只决定深评顺序。深评完成后，全站排名和仓位只使用
        # 同一个专业综合分；市场证据继续约束成本、波动与流动性容量。
        rank_score = professional_score
        deep_components = {
            "market_evidence": {
                "score": round(market_score, 2),
                "role": "diagnostic_and_capacity_constraints_not_total_score",
            },
            "professional_composite": {
                "score": round(professional_score, 2), "weight_pct": 100,
                "method": "professional_composite_v4_profit_sign_aware",
            },
        }
        method = "professional_composite_v4_profit_sign_aware"
    result.update({
        "stage": "deep_assessment_rank", "pre_rank_score": round(market_score, 2),
        "market_diagnostic_score": round(market_score, 2),
        "method": method,
        "rank_score": round(rank_score, 2),
        "deep_components": deep_components,
    })
    return result


def remaining_buy_cost(row: dict) -> float | None:
    if row.get("fee_correction_error"):
        return None
    if row.get("fee_correction_applied"):
        return round(max(0.0, float(row["buy_cost"]) - sum(float(e["allocated_buy_cost"]) for e in row.get("sell_events", []))), 2)
    shares = _finite(row.get("shares"))
    initial = _finite(row.get("initial_shares")) or shares
    entry = _finite(row.get("price"))
    if shares is None or initial is None or initial <= 0 or entry is None:
        return None
    total = _finite(row.get("buy_cost"))
    if total is None:
        amount = entry * initial
        total = amount * one_side_cost_rate(amount, "buy", slippage=0.0)
    return max(0.0, total * shares / initial)


def ledger_position_cost(row: dict) -> float | None:
    amount = _finite(row.get("amount"))
    if amount is None:
        price, shares = _finite(row.get("price")), _finite(row.get("shares"))
        amount = price * shares if price is not None and shares is not None else None
    buy_cost = remaining_buy_cost(row)
    return amount + buy_cost if amount is not None and amount >= 0 and buy_cost is not None else None


def manual_portfolio_status(
    quote_provider, *, bounded_call: Callable,
    open_rows: list[dict] | None = None,
    closed_rows: list[dict] | None = None,
    maximum_quote_age_seconds: float | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    open_rows = list_positions("open") if open_rows is None else open_rows
    closed_rows = list_positions("closed") if closed_rows is None else closed_rows
    codes = [row["code"] for row in open_rows]
    fresh_getter = getattr(quote_provider, "get_quotes_fresh", None)
    if callable(fresh_getter) and maximum_quote_age_seconds is None:
        maximum_quote_age_seconds = 20.0
    quote_call = (
        (lambda: fresh_getter(codes, maximum_age_seconds=5.0))
        if callable(fresh_getter)
        else (lambda: quote_provider.get_quotes(codes))
    )
    quote_ok, quotes, quote_error = (
        bounded_call(quote_call, 2.0)
        if codes else (True, {}, None)
    )
    quotes = quotes if quote_ok and isinstance(quotes, dict) else {}
    observed_at = now or datetime.now().astimezone()
    positions, missing, stale = [], [], []
    market_value = principal = gross = buy_cost_total = exit_cost_total = 0.0
    for row in open_rows:
        q = quotes.get(row["code"])
        entry, shares = float(row.get("price") or 0), int(row.get("shares") or 0)
        raw_current = _finite(getattr(q, "price", None))
        quote_as_of = (
            getattr(q, "exchange_data_time", None)
            or getattr(q, "data_time", None)
        )
        quote_at = _timestamp(quote_as_of)
        transport_at = _timestamp(getattr(q, "data_time", None))
        quote_age_seconds = (
            (observed_at - quote_at.astimezone(observed_at.tzinfo)).total_seconds()
            if quote_at is not None and observed_at.tzinfo is not None else None
        )
        transport_age_seconds = (
            (observed_at - transport_at.astimezone(observed_at.tzinfo)).total_seconds()
            if transport_at is not None and observed_at.tzinfo is not None else None
        )
        intraday_quote_valid = (
            maximum_quote_age_seconds is None
            or (
                quote_age_seconds is not None
                and -5.0 <= quote_age_seconds <= maximum_quote_age_seconds
            )
        )
        # After the close, an actively fetched same-day Sina snapshot is a
        # valid closing valuation even when a quiet stock's last trade is more
        # than 20 seconds old.  This exception is valuation-only: callers still
        # suppress alerts and execution outside the audited trading session.
        same_day_closing_snapshot_valid = bool(
            maximum_quote_age_seconds is not None
            and str(getattr(q, "freshness", "")) == "closed_snapshot"
            and observed_at.hour >= 15
            and quote_at is not None
            and quote_at.astimezone(observed_at.tzinfo).date() == observed_at.date()
            and transport_age_seconds is not None
            and -5.0 <= transport_age_seconds <= maximum_quote_age_seconds
        )
        # During the A-share lunch recess the exchange is intentionally quiet:
        # a freshly fetched 11:30 snapshot is the latest completed market fact,
        # not a broken/stale feed.  It may value the account and inform a risk
        # review, but it must never become an executable intraday quote.
        local_clock = observed_at.timetz().replace(tzinfo=None)
        same_day_midday_snapshot_valid = bool(
            maximum_quote_age_seconds is not None
            and time(11, 30) <= local_clock < time(13, 0)
            and quote_at is not None
            and quote_at.astimezone(observed_at.tzinfo).date() == observed_at.date()
            and time(11, 29) <= quote_at.astimezone(
                observed_at.tzinfo
            ).timetz().replace(tzinfo=None) <= time(11, 31)
            and transport_age_seconds is not None
            and -5.0 <= transport_age_seconds <= maximum_quote_age_seconds
        )
        # Before the next audited session opens, the latest completed close is
        # the causal account mark.  It is valid for exposure/risk planning only;
        # exact intraday actions still require a fresh executable quote below.
        completed_day = last_completed_trading_day(observed_at)
        before_open = bool(
            trading_day_status(observed_at).get("is_trading_day") is True
            and observed_at.timetz().replace(tzinfo=None) < time(9, 30)
        )
        non_trading_day = (
            trading_day_status(observed_at).get("is_trading_day") is False
        )
        prior_completed_close_valid = bool(
            maximum_quote_age_seconds is not None
            and str(getattr(q, "freshness", "")) == "closed_snapshot"
            and (before_open or non_trading_day)
            and completed_day is not None
            and quote_at is not None
            and quote_at.astimezone(observed_at.tzinfo).date() == completed_day
            and quote_at.astimezone(observed_at.tzinfo).date() < observed_at.date()
            and transport_age_seconds is not None
            and -5.0 <= transport_age_seconds <= maximum_quote_age_seconds
        )
        non_executable_snapshot_valid = (
            same_day_midday_snapshot_valid
            or same_day_closing_snapshot_valid
            or prior_completed_close_valid
        )
        quote_time_valid = intraday_quote_valid or non_executable_snapshot_valid
        current = raw_current if quote_time_valid else None
        if current is None or current <= 0:
            current = None
            missing.append(row["code"])
            if raw_current is not None and raw_current > 0 and not quote_time_valid:
                stale.append(row["code"])
        cost = entry * shares
        buy_cost = remaining_buy_cost(row)
        value = current * shares if current is not None else None
        principal += cost
        buy_cost_total += buy_cost or 0.0
        if value is not None:
            market_value += value
            gross += value - cost
            exit_cost = value * one_side_cost_rate(value, "sell")
            exit_cost_total += exit_cost
            net = None if row.get("fee_correction_error") else value - cost - (buy_cost or 0.0) - exit_cost
        else:
            exit_cost = net = None
        basis = cost + (buy_cost or 0.0)
        positions.append({
            "id": row.get("id"), "code": row["code"],
            "name": row.get("name") or row["code"], "shares": shares,
            "entry_price": entry, "current_price": current,
            "last_quote_price": raw_current,
            "previous_close": _finite(getattr(q, "previous_close", None)),
            "quote_as_of": quote_as_of,
            "quote_source": getattr(q, "source", None),
            "quote_freshness": (
                "midday_completed_snapshot"
                if same_day_midday_snapshot_valid and current is not None
                else
                "previous_completed_close_snapshot"
                if prior_completed_close_valid and current is not None
                else "closing_snapshot"
                if same_day_closing_snapshot_valid and current is not None
                else "fresh" if quote_time_valid and current is not None
                else "stale_or_invalid"
            ),
            "valuation_basis": (
                "same_day_midday_snapshot"
                if same_day_midday_snapshot_valid
                else "previous_completed_close_snapshot"
                if prior_completed_close_valid
                else "same_day_closing_snapshot"
                if same_day_closing_snapshot_valid
                else "intraday_fresh_quote" if quote_time_valid and current is not None
                else "unavailable"
            ),
            "valuation_executable": (
                not non_executable_snapshot_valid and current is not None
            ),
            "quote_age_seconds": (
                round(quote_age_seconds, 3)
                if quote_age_seconds is not None else None
            ),
            "market_value": round(value, 2) if value is not None else None,
            "entry_date": row.get("created_at"), "industry": row.get("industry") or "",
            "sellable_shares_snapshot": row.get("sellable_shares_snapshot"),
            "sellable_as_of": row.get("sellable_as_of"),
            "gross_unrealized_pnl": round(value - cost, 2) if value is not None else None,
            "remaining_buy_cost_yuan": round(buy_cost, 2) if buy_cost is not None else None,
            "estimated_exit_cost_yuan": round(exit_cost, 2) if exit_cost is not None else None,
            "unrealized_pnl": round(net, 2) if net is not None else None,
            "unrealized_pnl_pct": round(net / basis * 100, 2) if net is not None and basis > 0 else None,
            "pnl_semantics": "net_after_estimated_buy_and_liquidation_fees",
        })
    fee_error = any(row.get("fee_correction_error") for row in [*open_rows, *closed_rows])
    realized = None if fee_error else round(sum(float(row.get("pnl") or 0) for row in [*open_rows, *closed_rows]), 2)
    account_cash = get_account_cash()
    fee_sources = {
        str(row.get("buy_cost_source") or "unknown")
        for row in [*open_rows, *closed_rows]
    } | {
        str(row.get("sell_cost_source") or "unknown")
        for row in closed_rows
    }
    actual_fee_only = (
        not fee_error and not open_rows and bool(closed_rows)
        and fee_sources == {"actual_user_recorded"}
    )
    available = not missing and not fee_error
    net_unrealized = round(gross - buy_cost_total - exit_cost_total, 2) if available else None
    reason = (
        "费用更正引用的原始成交不一致，不能计算完整净收益与精确仓位" if fee_error else
        str(quote_error) if quote_error is not None else
        f"{len(stale)} 个持仓行情超过 {maximum_quote_age_seconds:g} 秒或时间无效"
        if stale and maximum_quote_age_seconds is not None else
        f"{len(missing)} 个持仓缺少有效现价" if missing else None
    )
    ledger_derived_cash = None if fee_error else ACCOUNT_CAPITAL + realized - principal - buy_cost_total
    available_cash = (
        float(account_cash["cash_balance"])
        if account_cash is not None else None
    )
    account_equity = (
        available_cash + market_value
        if available and available_cash is not None else None
    )
    ledger_derived_equity = (
        ledger_derived_cash + market_value if available else None
    )
    sellable_missing = _sellable_inventory_missing_codes(
        positions, observed_at,
    )
    quote_times = [
        parsed for position in positions
        if (parsed := _timestamp(position.get("quote_as_of"))) is not None
    ]
    quote_ages = [
        float(position["quote_age_seconds"])
        for position in positions
        if position.get("quote_age_seconds") is not None
    ]
    valuation_bases = {
        str(position.get("valuation_basis") or "unavailable")
        for position in positions
    }
    return {
        "positions": positions, "position_count": len(positions),
        "closed_count": len(closed_rows),
        "market_value": round(market_value, 2) if available else None,
        "cost_value": round(principal, 2),
        "cash_committed_value": round(principal + buy_cost_total, 2),
        "remaining_buy_cost_yuan": round(buy_cost_total, 2),
        "estimated_exit_cost_yuan": round(exit_cost_total, 2) if available else None,
        "gross_unrealized_pnl": round(gross, 2) if available else None,
        "unrealized_pnl": net_unrealized,
        "unrealized_pnl_pct": (
            round(net_unrealized / (principal + buy_cost_total) * 100, 2)
            if net_unrealized is not None and principal + buy_cost_total else None
        ),
        "realized_pnl": realized,
        "available_cash": (
            round(available_cash, 2) if available_cash is not None else None
        ),
        "cash_source": (
            account_cash.get("source") if account_cash is not None
            else "not_recorded"
        ),
        "cash_as_of": account_cash.get("updated_at") if account_cash is not None else None,
        "cash_user_recorded": account_cash is not None,
        "initial_equity_yuan": (
            round(float(account_cash["initial_equity_yuan"]), 2)
            if account_cash is not None
            and account_cash.get("initial_equity_yuan") is not None else None
        ),
        "initial_equity_as_of": (
            account_cash.get("initial_equity_as_of")
            if account_cash is not None else None
        ),
        "initial_equity_source": (
            account_cash.get("initial_equity_source")
            if account_cash is not None else None
        ),
        "initial_equity_recorded_at": (
            account_cash.get("initial_equity_recorded_at")
            if account_cash is not None else None
        ),
        "account_equity": round(account_equity, 2) if account_equity is not None else None,
        "account_capital": (
            round(available_cash + principal + buy_cost_total, 2)
            if account_cash is not None and not fee_error else None
        ),
        "ledger_derived_cash_yuan": (
            round(ledger_derived_cash, 2) if account_cash is None and ledger_derived_cash is not None else None
        ),
        "ledger_derived_account_equity_yuan": (
            round(ledger_derived_equity, 2)
            if account_cash is None and ledger_derived_equity is not None else None
        ),
        "legacy_nominal_initial_capital_yuan": (
            ACCOUNT_CAPITAL if account_cash is None else None
        ),
        "pnl_semantics": (
            "unavailable_inconsistent_fee_facts" if fee_error else
            "net_after_recorded_fills_and_actual_fees"
            if actual_fee_only
            else "net_after_recorded_fills_and_estimated_fees"
        ),
        "fee_source_status": (
            "inconsistent" if fee_error else
            "actual" if actual_fee_only else
            "unavailable" if not open_rows and not closed_rows else
            "estimated_or_mixed"
        ),
        "source": "manual_timing_positions",
        "decision_as_of": observed_at.isoformat(timespec="seconds"),
        "valuation_status": "complete" if available else "insufficient",
        "missing_quote_codes": missing,
        "stale_quote_codes": stale,
        "valuation_as_of": (
            min(quote_times).isoformat(timespec="seconds")
            if quote_times else None
        ),
        "maximum_quote_age_seconds": maximum_quote_age_seconds,
        "max_quote_age_seconds": round(max(quote_ages), 3) if quote_ages else None,
        "sellable_inventory_status": (
            "complete" if not sellable_missing else "incomplete"
        ),
        "sellable_inventory_missing_codes": sellable_missing,
        "quote_coverage_pct": round((len(open_rows) - len(missing)) / len(open_rows) * 100, 1) if open_rows else 100.0,
        "valuation_basis": (
            "same_day_midday_snapshot"
            if available and valuation_bases == {"same_day_midday_snapshot"}
            else "same_day_closing_snapshot"
            if available and valuation_bases == {"same_day_closing_snapshot"}
            else "previous_completed_close_snapshot"
            if available and valuation_bases == {"previous_completed_close_snapshot"}
            else "intraday_fresh_quote"
            if available and valuation_bases == {"intraday_fresh_quote"}
            else "mixed_or_incomplete"
        ),
        "valuation_executable": bool(
            available and valuation_bases == {"intraday_fresh_quote"}
        ),
        "quote_health": {
            "status": "healthy" if available else "degraded", "reason": reason,
            "fallback": None if available else "行情缺失或过期；未使用旧价或手工成交价冒充现价，组合估值与精确动作不可用",
        },
    }


def portfolio_exposure_pct(portfolio: dict) -> float | None:
    if portfolio.get("valuation_status") != "complete":
        return None
    market_value = _finite(portfolio.get("market_value"))
    available_cash = _finite(portfolio.get("available_cash"))
    if market_value is None or available_cash is None:
        return None
    equity = available_cash + market_value
    # Keep calculation precision here. Rounding before converting the target
    # percentage back to yuan can manufacture a tiny over-allocation and make
    # the whole-lot reducer propose selling 100 shares for a sub-yuan gap.
    # Presentation layers may round the returned percentage for display.
    return market_value / equity * 100 if math.isfinite(equity) and equity > 0 else None


def position_trade_plan(
    position: dict, risk_action: dict | None, current_price: float | None,
    *, quote_as_of=None, valid_until=None, inventory_reference_at=None,
    risk_observation: dict | None = None,
) -> dict[str, object]:
    risk_action = risk_action if isinstance(risk_action, dict) else {}
    shares, current = int(position.get("shares") or 0), _finite(current_price)
    stop = _finite(risk_action.get("effective_stop"))
    effective_level = (
        risk_action.get("effective_risk_level")
        if isinstance(risk_action.get("effective_risk_level"), dict) else {}
    )
    hard = current is not None and stop is not None and current <= stop
    risk_observation = (
        risk_observation if isinstance(risk_observation, dict) else {}
    )
    imported_snapshot = bool(
        position.get("action") == "position_snapshot"
        or position.get("entry_origin") == "manual_position_snapshot"
    )
    episode_kind = str(risk_observation.get("episode_kind") or "")
    # A position imported below its risk line has no observable crossing time.
    # Keep the risk fact visible, but do not manufacture a timed sell action.
    hard_actionable = bool(
        hard and not (
            imported_snapshot
            and episode_kind in {"", "existing_breach_discovered_after_import"}
        )
    )
    decision_time = _timestamp(valid_until)
    quote_time = _timestamp(quote_as_of)
    inventory_time = _timestamp(inventory_reference_at) or decision_time
    manual_target = _finite(position.get("manual_exit_target_shares"))
    manual_effective_raw = position.get("manual_exit_effective_date")
    manual_effective_date = None
    if isinstance(manual_effective_raw, str):
        try:
            manual_effective_date = datetime.strptime(
                manual_effective_raw.strip(), "%Y-%m-%d",
            ).date()
        except ValueError:
            manual_effective_date = None
    manual_intent_configured = bool(
        manual_target is not None
        and manual_target.is_integer()
        and 0 <= manual_target < shares
        and manual_effective_date is not None
    )
    desired_target = int(manual_target) if manual_intent_configured else None
    manual_intent_active = bool(
        manual_intent_configured
        and decision_time is not None
        and decision_time.date() >= manual_effective_date
    )
    exit_review_triggered = hard_actionable or manual_intent_active
    sellable = _finite(position.get("sellable_shares_snapshot"))
    sellable_inventory_current = bool(
        inventory_time is not None
        and sellable is not None
        and sellable.is_integer()
        and 0 <= sellable <= shares
        and _sellable_as_of_is_current(
            position.get("sellable_as_of"), inventory_time,
        )
    )
    sellable_is_valid = bool(
        sellable_inventory_current
        and current is not None
        and current > 0
        and decision_time is not None
        and quote_time is not None
        and -5 <= (decision_time - quote_time).total_seconds() <= 180
    )
    executable_sellable = int(sellable) if sellable_is_valid else None
    observations = [{
        "kind": signal.get("signal") or "technical_observation",
        "label": signal.get("label"), "detail": signal.get("detail"),
        "source": "audited_daily_technical_observation",
    } for signal in risk_action.get("quant_signals") or [] if isinstance(signal, dict)]
    if risk_action.get("action") == "减仓复核":
        observations.append({
            "kind": "reduction_without_target_quantity",
            "detail": risk_action.get("reason"), "source": "position_risk_action",
        })
    if manual_intent_configured:
        observations.append({
            "kind": "user_directed_exit_intent",
            "detail": (
                f"用户计划自{manual_effective_date.isoformat()}起将持仓降至"
                f"{desired_target}股；这会触发实时人工复核，不代表已预测全天最高价"
            ),
            "source": "audited_manual_exit_intent",
            "actionable": False,
        })
    if hard and not hard_actionable:
        observations.append({
            "kind": "existing_risk_breach_after_import",
            "detail": (
                "持仓导入时已经低于风险线，无法确认首次跌破时点；"
                "仅显示风险事实，不生成紧急卖出动作"
            ),
            "source": "position_risk_episode_audit",
            "actionable": False,
        })
    if exit_review_triggered and executable_sellable is None:
        observations.append({
            "kind": "sellable_inventory_unavailable",
            "detail": "交易时段、180秒内行情或当日可卖库存不完整，不生成精确卖出数量",
            "source": "manual_broker_sellable_snapshot",
            "actionable": False,
        })
    elif exit_review_triggered and executable_sellable == 0:
        observations.append({
            "kind": "no_sellable_inventory_today",
            "detail": "券商快照显示今日可卖为0，保留离场复核但不生成卖出数量",
            "source": "manual_broker_sellable_snapshot",
            "actionable": False,
        })
    elif (
        exit_review_triggered and executable_sellable is not None
        and executable_sellable < shares
    ):
        observations.append({
            "kind": "partial_sellable_inventory",
            "detail": f"总持仓{shares}股，今日最多可卖{executable_sellable}股",
            "source": "manual_broker_sellable_snapshot",
            "actionable": False,
        })
    amount = current * shares if current is not None and shares > 0 else 0.0
    plan_target = (
        desired_target if manual_intent_active
        else 0 if hard_actionable else None
    )
    manual_method = "manual_exit_intent_v1" if manual_intent_active else None
    plan = build_trade_plan({
        "current_shares": shares, "target_shares": plan_target,
        "entry_price": position.get("price") or position.get("entry_price"),
        "current_price": current,
        "hard_risk_triggered": hard_actionable and not manual_intent_active,
        "strategy_exit_triggered": bool(
            manual_intent_active and desired_target == 0
        ), "limit_price": current,
        "valid_until": str(valid_until or "") or None,
        "cancel_condition": "持仓数量、行情或有效风险线变化后重新计算；不自动下单",
        "estimated_cost": amount * one_side_cost_rate(amount, "sell") if amount > 0 else None,
        "evidence_identity": {
            "method": manual_method or "position_risk_action_v2",
            "position_id": position.get("id"),
            "code": position.get("code"), "effective_stop": stop,
            "risk_method_id": "frozen_pre_entry_cost_volatility_risk_line_v2",
            "risk_formula": effective_level.get("formula"),
            "risk_calibration_status": effective_level.get("calibration_status"),
            "risk_timing_role": (
                "user_directed_exit_not_system_timing"
                if manual_method else
                "loss_containment_review_not_best_exit_point"
            ),
            "risk_episode_kind": episode_kind or None,
            "atr14": risk_action.get("atr14"),
            "atr14_as_of": risk_action.get("atr14_as_of"),
            "quote_as_of": quote_as_of,
            "sellable_shares_snapshot": executable_sellable,
            "sellable_as_of": position.get("sellable_as_of"),
            "manual_exit_target_shares": desired_target,
            "manual_exit_effective_date": (
                manual_effective_date.isoformat()
                if manual_effective_date is not None else None
            ),
            "profit_optimality_claim": False,
        },
        "playbook_id": (
            "user_directed_exit" if manual_method else "retreat_defense"
        ),
        "strategy_id": manual_method or "position_risk_action_v2",
        "strategy_version": "1" if manual_method else "2",
        "evidence_grade": (
            "user_recorded_intent" if manual_method else "hard_risk_rule"
        ),
        "as_of": quote_as_of,
        "trailing_risk_activated": risk_action.get("trailing_active") is True,
        "observations": observations,
    })
    action = plan.get("action")
    if exit_review_triggered and isinstance(action, dict):
        if executable_sellable is None or executable_sellable <= 0:
            plan["action"] = None
        else:
            desired_reduction = shares - int(plan_target or 0)
            action_shares = min(executable_sellable, desired_reduction)
            if action_shares <= 0:
                plan["action"] = None
            else:
                action["shares"] = action_shares
                action["target_shares"] = shares - action_shares
            amount = current * action_shares
            action["estimated_cost"] = (
                amount * one_side_cost_rate(amount, "sell")
            )
    plan["sellable_inventory"] = {
        "status": (
            "current" if sellable_inventory_current else "missing_or_stale"
        ),
        "total_shares": shares,
        "sellable_shares": (
            int(sellable) if sellable_inventory_current else None
        ),
        "as_of": position.get("sellable_as_of"),
    }
    plan["manual_exit_intent"] = {
        "configured": manual_intent_configured,
        "active": manual_intent_active,
        "target_shares": desired_target,
        "effective_date": (
            manual_effective_date.isoformat()
            if manual_effective_date is not None else None
        ),
        "note": position.get("manual_exit_note"),
        "automatic_order": False,
    }
    plan["risk_timing_authority"] = {
        "risk_line_breached": hard,
        "timed_action_authorized": hard_actionable,
        "episode_kind": episode_kind or None,
        "best_exit_validated": False,
        "role": (
            "existing_breach_review_only"
            if hard and not hard_actionable else
            "fresh_risk_cross_review"
            if hard_actionable else "not_triggered"
        ),
    }
    if exit_review_triggered and plan.get("action") is None:
        plan["reason"] = (
            f"{risk_action.get('reason') or '用户退出计划已生效'}；"
            "等待交易时段、实时行情与当日可卖库存完整后再生成数量"
        )
    elif manual_intent_active:
        plan["reason"] = (
            f"用户自{manual_effective_date.isoformat()}起计划将目标持仓降至"
            f"{desired_target}股；当前仅生成实时人工复核，不自动下单，"
            "也不宣称当前价格是全天最佳卖点"
        )
    elif manual_intent_configured:
        plan["reason"] = (
            f"已记录{manual_effective_date.isoformat()}起将目标持仓降至"
            f"{desired_target}股；生效前继续监控"
        )
    elif hard and not hard_actionable:
        plan["reason"] = (
            "该持仓导入时已低于风险线，系统无法证明首次跌破时刻；"
            "仅列为风险复核，不生成卖出数量，也不宣称是最佳卖点"
        )
    else:
        plan["reason"] = risk_action.get("reason") or "当前没有带精确数量的退出依据，继续持有并观察"
    return plan


def _existing_exposure_reduction(
    *, positions: list[dict], existing_market_value: float,
    available_cash: float, deployable: float, capital: float,
    planning_equity: float, deployable_pct: float,
    entry_context: dict | None = None,
) -> dict | None:
    """Return a market-cap reduction review without inventing sellability.

    The desired portfolio target can be calculated outside trading hours, but
    an exact sell action additionally requires a fresh quote and the user's
    current-day sellable-inventory fact.  This keeps T+1 and stale snapshots
    from becoming apparently executable quantities.
    """
    existing_value_cents = round(existing_market_value * 100)
    deployable_cents = round(deployable * 100)
    if existing_value_cents <= 0 or existing_value_cents <= deployable_cents:
        return None
    context = entry_context if isinstance(entry_context, dict) else {}
    decision_time = _timestamp(context.get("decision_time"))
    valid_until = context.get("market_valid_until")
    session_executable = context.get("market_session_executable") is True
    items, desired_target_value = [], 0.0
    executable_target_value = 0.0
    sell_amount_total = sell_cost_total = 0.0
    desired_position_count = 0
    executable_position_count = 0
    executable_actions = 0
    blocked_codes: list[str] = []
    for position in positions:
        current_shares = int(_finite(position.get("shares")) or 0)
        market_value = _finite(position.get("market_value"))
        price = _finite(position.get("current_price"))
        if price is None and market_value is not None and current_shares > 0:
            price = market_value / current_shares
        if current_shares <= 0 or price is None or price <= 0:
            continue
        desired_target_shares = (
            current_shares * deployable_cents
            // (existing_value_cents * 100) * 100
        )
        desired_target_shares = max(
            0, min(current_shares, desired_target_shares),
        )
        desired_reduce_shares = current_shares - desired_target_shares
        desired_target_amount = desired_target_shares * price
        desired_target_value += desired_target_amount
        if desired_target_shares > 0:
            desired_position_count += 1
        if desired_reduce_shares <= 0:
            executable_target_value += current_shares * price
            executable_position_count += 1
            continue
        quote_time = _timestamp(position.get("quote_as_of"))
        sellable = _finite(position.get("sellable_shares_snapshot"))
        sellable_is_current = bool(
            session_executable and decision_time is not None
            and quote_time is not None
            and -5 <= (decision_time - quote_time).total_seconds() <= 180
            and sellable is not None and sellable.is_integer()
            and 0 <= sellable <= current_shares
            and _sellable_as_of_is_current(
                position.get("sellable_as_of"), decision_time,
            )
        )
        executable_reduce_shares = 0
        if sellable_is_current:
            executable_reduce_shares = min(
                desired_reduce_shares, int(sellable),
            )
            if executable_reduce_shares < current_shares:
                executable_reduce_shares = (
                    executable_reduce_shares // 100
                ) * 100
        target_shares = current_shares - executable_reduce_shares
        target_amount = target_shares * price
        executable_target_value += target_amount
        if target_shares > 0:
            executable_position_count += 1
        sell_amount = executable_reduce_shares * price
        sell_cost = sell_amount * one_side_cost_rate(sell_amount, "sell")
        sell_amount_total += sell_amount
        sell_cost_total += sell_cost
        if executable_reduce_shares > 0:
            executable_actions += 1
        else:
            blocked_codes.append(str(position.get("code") or ""))
        evidence = {
            "method": "portfolio_exposure_rebalance_v1",
            "position_id": position.get("id"),
            "code": position.get("code"),
            "current_shares": current_shares,
            "desired_target_shares": desired_target_shares,
            "target_shares": target_shares,
            "market_target_exposure_pct": round(deployable_pct, 6),
            "quote_as_of": position.get("quote_as_of"),
            "sellable_shares_snapshot": (
                int(sellable) if sellable_is_current else None
            ),
            "sellable_as_of": position.get("sellable_as_of"),
        }
        observations = [{
            "kind": "portfolio_exposure_rebalance",
            "detail": (
                "市场风险上限下降，按现有持仓比例计算目标；"
                "仅供人工复核，不自动下单"
            ),
        }]
        if not sellable_is_current:
            observations.append({
                "kind": "sellable_inventory_unavailable",
                "detail": (
                    "交易时段、180秒内行情或当日可卖库存不完整；"
                    "只展示目标差额，不生成卖出数量"
                ),
            })
        elif executable_reduce_shares < desired_reduce_shares:
            observations.append({
                "kind": "partial_sellable_inventory",
                "detail": (
                    f"目标需减{desired_reduce_shares}股，当前最多生成"
                    f"{executable_reduce_shares}股人工复核计划"
                ),
            })
        trade_plan = build_trade_plan({
            "current_shares": current_shares,
            "target_shares": target_shares,
            "current_price": price,
            "strategy_exit_triggered": (
                target_shares == 0 and executable_reduce_shares > 0
            ),
            "limit_price": price,
            "valid_until": valid_until if executable_reduce_shares > 0 else None,
            "cancel_condition": "市场目标暴露、现价或实际持仓变化后重新计算",
            "estimated_cost": sell_cost if executable_reduce_shares > 0 else None,
            "evidence_identity": evidence,
            "playbook_id": "retreat_defense",
            "strategy_id": "portfolio_exposure_rebalance_v1",
            "strategy_version": "1",
            "evidence_grade": "account_risk_rule",
            "as_of": position.get("quote_as_of"),
            "observations": observations,
        })
        trade_plan["sellable_inventory"] = {
            "status": "current" if sellable_is_current else "missing_or_stale",
            "total_shares": current_shares,
            "sellable_shares": int(sellable) if sellable_is_current else None,
            "as_of": position.get("sellable_as_of"),
        }
        trade_plan["desired_target_shares"] = desired_target_shares
        trade_plan["reason"] = (
            f"组合总仓上限降至{deployable_pct:g}%；当前可人工复核卖出"
            f"{executable_reduce_shares}股，卖后{target_shares}股"
            if executable_reduce_shares > 0 else
            f"组合总仓上限降至{deployable_pct:g}%，目标为"
            f"{desired_target_shares}股；等待交易时段、实时行情和当日可卖库存"
        )
        items.append({
            "code": position.get("code"),
            "name": position.get("name") or position.get("code"),
            "price": round(price, 4),
            "shares": target_shares,
            "amount": round(target_amount, 2),
            "target_weight_pct": round(target_amount / planning_equity * 100, 2),
            "held_shares": current_shares,
            "held_ok": False,
            "add_shares": 0,
            "desired_target_shares": desired_target_shares,
            "desired_reduce_shares": desired_reduce_shares,
            "reduce_shares": executable_reduce_shares,
            "estimated_buy_cost_yuan": 0.0,
            "estimated_sell_cost_yuan": round(sell_cost, 2),
            "estimated_sell_proceeds_yuan": round(sell_amount - sell_cost, 2),
            "sellable_inventory": copy.deepcopy(
                trade_plan["sellable_inventory"]
            ),
            "trade_plan": trade_plan,
        })
    if not items:
        return None
    projected_cash = available_cash + sell_amount_total - sell_cost_total
    return {
        "capital": capital,
        "planning_equity": round(planning_equity, 2),
        "items": items,
        "skipped": [],
        "desired_total_used": round(desired_target_value, 2),
        "total_used": round(executable_target_value, 2),
        "estimated_buy_cost_yuan": 0.0,
        "estimated_new_buy_cash_yuan": 0.0,
        "estimated_sell_cost_yuan": round(sell_cost_total, 2),
        "estimated_sell_proceeds_yuan": round(
            sell_amount_total - sell_cost_total, 2,
        ),
        "used_pct": round(executable_target_value / planning_equity * 100, 1) if planning_equity else 0.0,
        "current_cash_buffer": round(available_cash, 2),
        "cash_buffer": round(projected_cash, 2),
        "cash_buffer_pct": round(projected_cash / planning_equity * 100, 2) if planning_equity else 0.0,
        "market_target_exposure_pct": round(deployable_pct, 2),
        "position_count": executable_position_count,
        "desired_position_count": desired_position_count,
        "existing_position_count": len(positions),
        "existing_positions": positions,
        "portfolio_valuation_status": "complete",
        "plan_status": (
            "rebalance_review" if executable_actions
            else "rebalance_blocked_sellable_inventory"
        ),
        "actionable_sell_now": executable_actions > 0,
        "blocked_sellable_codes": blocked_codes,
        "max_positions": MAX_OPEN_POSITIONS,
        "rebalance_method": "pro_rata_current_market_value_rounded_to_board_lot",
        "rebalance_method_status": "conservative_transparent_not_risk_optimal",
        "note": (
            "现有仓位高于市场总仓上限；目标差额已按比例和整手计算。"
            + (
                "只有具备180秒内行情与当日可卖库存的部分生成了精确减仓复核计划。"
                if executable_actions else
                "当前缺少交易时段、实时行情或当日可卖库存，未生成卖出数量。"
            )
            + "本站不连接券商，不会自动下单。"
        ),
    }


def allocate_capital(
    items: list, capital: float,
    market_target_exposure_pct: float | None = None,
    portfolio: dict | None = None,
    entry_context: dict | None = None,
    risk_context: dict | None = None,
) -> dict:
    """根据资金 + 今日机会评分分配持仓结构，算每只最佳持股数量（A股整手100）。

    规则：
    - 有统一评估时只采纳 APPROVE，优先按机会质量分并用历史波动做柔性缩放
    - 总仓位、单股和最低现金统一使用 data_semantics 的账户风险上限
    - 可选市场目标暴露只会下调总仓位，不会突破账户上限
    - 流动性容量只在有 ADV20 时柔性压低目标；精确行动计划缺失时拒绝
    - 行业已知时限制组合单行业暴露；未知时进入最坏情形预留桶
    - 单笔计划损失同时受账户回撤缩放与组合风险余量约束
    - 持股数量 = floor(目标金额 / (现价×100)) × 100 股
    - 单笔金额低于 1 手成本的股票提示"资金不足一手"，从计划中剔除并回流资金
    - 买入费用含最低佣金、过户费和滑点；同时披露往返全成本
    - 已持仓股票：标注"已持有 N 股"，只建议补仓到目标仓位，不重复全额分配
    - 传入完整组合估值时，现有市值计入总暴露，新增买入现金（含费用）不超过账本可用现金
    """
    portfolio_positions = []
    portfolio_by_code = {}
    existing_market_value = 0.0
    available_cash = float(capital)
    slot_skipped = []
    if portfolio is not None:
        portfolio_positions = portfolio.get("positions")
        existing_market_value = _finite(portfolio.get("market_value"))
        if (
            portfolio.get("valuation_status") == "complete"
            and portfolio.get("cash_user_recorded") is False
        ):
            return {
                "error": "尚未录入真实剩余资金，资金计划暂不可用；未生成任何交易动作",
                "plan_status": "missing_account_cash",
                "items": None,
                "capital": capital,
                "total_used": None,
                "used_pct": None,
                "cash_buffer": None,
                "cash_buffer_pct": None,
                "cash_user_recorded": False,
                "account_fact_gaps": ["真实剩余可用资金"],
                "position_count": len(portfolio_positions) if isinstance(portfolio_positions, list) else 0,
                "max_positions": MAX_OPEN_POSITIONS,
                "portfolio_valuation_status": "complete",
                "missing_quote_codes": [],
            }
        available_cash = _finite(portfolio.get("available_cash"))
        valuation_complete = (
            portfolio.get("valuation_status") == "complete"
            and isinstance(portfolio_positions, list)
            and existing_market_value is not None
            and existing_market_value >= 0
            and available_cash is not None
        )
        if valuation_complete:
            for position in portfolio_positions:
                code = str(position.get("code") or "")
                shares = _finite(position.get("shares"))
                market_value = _finite(position.get("market_value"))
                if not code or shares is None or shares < 0 or market_value is None or market_value < 0:
                    valuation_complete = False
                    break
                held = portfolio_by_code.setdefault(code, {"shares": 0, "market_value": 0.0})
                held["shares"] += int(shares)
                held["market_value"] += market_value
        if not valuation_complete:
            return {
                "error": "持仓估值不完整，资金计划暂不可用；未生成任何交易动作",
                "plan_status": "unavailable_portfolio_valuation",
                "items": None,
                "capital": capital,
                "total_used": None,
                "used_pct": None,
                "cash_buffer": None,
                "cash_buffer_pct": None,
                "position_count": len(portfolio_positions) if isinstance(portfolio_positions, list) else 0,
                "max_positions": MAX_OPEN_POSITIONS,
                "portfolio_valuation_status": portfolio.get("valuation_status") or "insufficient",
                "missing_quote_codes": portfolio.get("missing_quote_codes") or [],
            }

    cands = []
    for raw in items:
        has_assessment = "assessment" in raw
        assessment = raw.get("assessment") or {}
        if assessment and assessment.get("verdict") != "APPROVE":
            continue
        quality = raw.get("opportunity_quality") or {}
        quality_score = _finite(quality.get("rank_score"))
        composite = (
            ((assessment.get("gates") or {}).get("composite") or {})
            .get("composite")
        )
        if quality_score is None:
            quality_score = _finite(composite)
        # Legacy callers without a unified assessment may still provide a
        # standalone research score.  Once an assessment exists, however,
        # its missing professional score must fail closed: ``raw.score`` is
        # also used by timing responses and must never become a sizing input.
        if quality_score is None and not has_assessment:
            quality_score = _finite(raw.get("score"))
        if (
            (raw.get("price") or 0) <= 0
            or quality_score is None
            or (not assessment and quality_score < 40)
        ):
            continue
        volatility = _finite(quality.get("annualized_volatility_pct"))
        volatility_scale = (
            max(0.65, min(1.0, 30.0 / volatility))
            if volatility is not None and volatility > 0 else 1.0
        )
        cands.append({
            **raw,
            "_quality_score": max(0.1, quality_score),
            "_annualized_volatility_pct": volatility,
            "_volatility_scale": volatility_scale,
            "_allocation_score": max(0.1, quality_score * volatility_scale),
        })
    cands = sorted(
        cands, key=lambda item: item["_allocation_score"], reverse=True,
    )
    if portfolio is not None:
        existing_codes = set(portfolio_by_code)
        held_candidates = [
            item for item in cands if str(item.get("code")) in existing_codes
        ]
        new_candidates = [
            item for item in cands if str(item.get("code")) not in existing_codes
        ]
        remaining_slots = max(0, MAX_OPEN_POSITIONS - len(existing_codes))
        slot_skipped = [{
            "code": item.get("code"), "name": item.get("name") or item.get("code"),
            "reason": f"现有持仓已占{len(existing_codes)}个席位，仅剩{remaining_slots}个新开仓席位",
        } for item in new_candidates[remaining_slots:]]
        cands = held_candidates + new_candidates[:remaining_slots]
    else:
        cands = cands[:MAX_OPEN_POSITIONS]
    deployable_pct = min(
        float(MAX_TOTAL_EXPOSURE_PCT),
        100.0 - float(MIN_CASH_RESERVE_PCT),
    )
    requested_exposure = _finite(market_target_exposure_pct)
    if requested_exposure is not None:
        deployable_pct = min(deployable_pct, max(0.0, requested_exposure))
    planning_equity = (
        available_cash + existing_market_value
        if portfolio is not None else float(capital)
    )
    drawdown_policy = _drawdown_risk_scale(risk_context)
    portfolio_market_risk_policy = _portfolio_market_risk_scale(risk_context)
    combined_risk_scale = min(
        float(drawdown_policy["scale"]),
        float(portfolio_market_risk_policy["scale"]),
    )
    portfolio_risk = _portfolio_open_risk_context(
        portfolio, risk_context, planning_equity,
    )
    remaining_portfolio_risk = _finite(
        portfolio_risk.get("remaining_before_new_positions_yuan")
    )
    projected_industry_values: dict[str, float] = {}
    industry_unknown_codes: list[str] = []
    unknown_industry_value = 0.0
    for position in portfolio_positions:
        industry = _position_industry(position)
        market_value = _finite(position.get("market_value"))
        if not industry:
            industry_unknown_codes.append(str(position.get("code") or ""))
            if market_value is not None and market_value >= 0:
                unknown_industry_value += market_value
        elif market_value is not None and market_value >= 0:
            projected_industry_values[industry] = (
                projected_industry_values.get(industry, 0.0) + market_value
            )
    current_unknown_industry_value = unknown_industry_value
    deployable = planning_equity * deployable_pct / 100
    if portfolio is not None:
        reduction = _existing_exposure_reduction(
            positions=portfolio_positions,
            existing_market_value=existing_market_value,
            available_cash=available_cash,
            deployable=deployable,
            capital=capital,
            planning_equity=planning_equity,
            deployable_pct=deployable_pct,
            entry_context=entry_context,
        )
        if reduction is not None:
            return reduction
    if not cands:
        if portfolio is None:
            return {
                "error": "当前没有机会质量通过的股票，目标仓位保持现金",
                "items": [], "capital": capital,
                "max_positions": MAX_OPEN_POSITIONS,
                "drawdown_risk_policy": drawdown_policy,
                "portfolio_market_risk_policy": portfolio_market_risk_policy,
                "portfolio_risk_heat": portfolio_risk,
            }
        used_pct = existing_market_value / planning_equity * 100 if planning_equity else 0
        return {
            "capital": capital,
            "planning_equity": round(planning_equity, 2),
            "items": [],
            "skipped": slot_skipped,
            "total_used": round(existing_market_value, 2),
            "estimated_buy_cost_yuan": 0.0,
            "estimated_new_buy_cash_yuan": 0.0,
            "used_pct": round(used_pct, 1),
            "cash_buffer": round(available_cash, 2),
            "cash_buffer_pct": round(available_cash / planning_equity * 100, 2) if planning_equity else 0.0,
            "market_target_exposure_pct": round(deployable_pct, 2),
            "position_count": len(portfolio_by_code),
            "existing_position_count": len(portfolio_positions),
            "existing_positions": portfolio_positions,
            "portfolio_valuation_status": "complete",
            "plan_status": "portfolio_only" if portfolio_positions else "cash_only",
            "max_positions": MAX_OPEN_POSITIONS,
            "drawdown_risk_policy": drawdown_policy,
            "portfolio_market_risk_policy": portfolio_market_risk_policy,
            "portfolio_risk_heat": portfolio_risk,
            "industry_concentration_policy": {
                "status": (
                    "bounded_with_unknown_reserve"
                    if industry_unknown_codes else "complete"
                ),
                "method": "known_industry_plus_unknown_worst_case_reserve_v1",
                "maximum_industry_exposure_pct": MAX_INDUSTRY_EXPOSURE_PCT,
                "unknown_position_codes": industry_unknown_codes,
                "current_unknown_industry_market_value_yuan": round(
                    current_unknown_industry_value, 2,
                ),
                "projected_unknown_industry_market_value_yuan": round(
                    unknown_industry_value, 2,
                ),
                "unknown_industry_market_value_yuan": round(
                    unknown_industry_value, 2,
                ),
                "unknown_exposure_reserved_against_every_candidate": True,
                "missing_candidate_industry_does_not_invent_a_classification": True,
                "profit_optimality_claim": False,
            },
            "note": (
                "当前没有新的机会质量通过股票；现有持仓与账本可用现金已如实计入，"
                "未生成新增买入动作。"
                if portfolio_positions else "当前无持仓且没有新的机会质量通过股票，保持现金等待。"
            ),
        }
    total_score = sum(it["_allocation_score"] for it in cands)
    max_single = planning_equity * float(MAX_SINGLE_POSITION_PCT) / 100
    allocations = []
    total_used = existing_market_value
    total_buy_cost = 0.0
    total_new_buy_cash = 0.0
    skipped = list(slot_skipped)
    for it in cands:
        price = float(it["price"])
        code, name = it["code"], it.get("name") or it["code"]
        portfolio_holding = portfolio_by_code.get(str(code)) if portfolio is not None else None
        held = (
            portfolio_holding.get("shares", 0)
            if portfolio_holding is not None
            else (it.get("position") or {}).get("shares") or 0
        )
        replaced_market_value = (
            portfolio_holding.get("market_value", 0.0)
            if portfolio_holding is not None else 0.0
        )
        share = it["_allocation_score"] / total_score
        target = min(deployable * share, max_single)
        quality = it.get("opportunity_quality") or {}
        avg_amount = _finite(quality.get("avg_amount_20d_yuan"))
        liquidity_capacity = avg_amount * 0.005 if avg_amount and avg_amount > 0 else None
        if liquidity_capacity is not None:
            target = min(target, liquidity_capacity)
        candidate_industry = _candidate_industry(it)
        held_position = next((
            row for row in portfolio_positions
            if str(row.get("code") or "") == str(code)
        ), {})
        held_industry = _position_industry(held_position)
        unknown_reserve_before = max(
            0.0,
            unknown_industry_value
            - (replaced_market_value if portfolio_holding is not None
               and not held_industry else 0.0),
        )
        current_industry_value = (
            projected_industry_values.get(candidate_industry, 0.0)
            if candidate_industry else 0.0
        )
        if (
            portfolio_holding is not None
            and held_industry == candidate_industry
            and candidate_industry
        ):
            current_industry_value = max(
                0.0, current_industry_value - replaced_market_value,
            )
        # Missing classifications are one explicit uncertainty reserve, not a
        # made-up industry.  Reserving that value against every candidate is
        # conservative and still lets the system operate when coverage is
        # incomplete instead of silently ignoring concentration risk.
        industry_capacity = max(
            0.0,
            planning_equity * MAX_INDUSTRY_EXPOSURE_PCT / 100.0
            - current_industry_value - unknown_reserve_before,
        )
        target = min(target, industry_capacity)
        lots = int(target // (price * 100))      # 整手数（floor）
        shares = lots * 100
        held_int = max(0, int(held) if held else 0)
        requested_add_shares = max(0, shares - held_int)
        if portfolio_positions:
            try:
                candidate_dependency = calculate_candidate_dependency(
                    portfolio_positions, str(code),
                )
            except Exception as exc:
                candidate_dependency = {
                    "available": False,
                    "status": "unavailable_calculation_error",
                    "reason": f"候选相关性计算失败：{type(exc).__name__}",
                }
        else:
            candidate_dependency = {
                "available": True,
                "status": "not_applicable_no_existing_positions",
                "portfolio_correlation": None,
                "maximum_position_correlation": None,
            }
        dependency_policy = _candidate_dependency_risk_scale(
            candidate_dependency,
        )
        execution_sizing_gaps: list[str] = []
        if (
            entry_context and entry_context.get("executable") is True
            and requested_add_shares > 0
        ):
            if portfolio_positions and portfolio_risk.get("status") not in {
                "complete", "complete_empty_portfolio",
            }:
                execution_sizing_gaps.append(
                    "已有持仓缺少有效风险线，组合计划风险余量不可核验"
                )
            if liquidity_capacity is None:
                execution_sizing_gaps.append(
                    "缺少ADV20日均成交额，流动性容量不可核验"
                )
            decision_time = _timestamp(entry_context.get("decision_time"))
            quote_time = _timestamp(it.get("quote_as_of"))
            if (
                entry_context.get("market_session_executable") is not True
                or decision_time is None or quote_time is None
                or not -5 <= (decision_time - quote_time).total_seconds() <= 180
            ):
                execution_sizing_gaps.append(
                    "缺少180秒内实时行情时点，精确买入数量不可核验"
                )
            cash_time = _timestamp(entry_context.get("cash_as_of"))
            if (
                decision_time is None or cash_time is None
                or cash_time > decision_time
                or (decision_time - cash_time).total_seconds() > 86400
            ):
                execution_sizing_gaps.append(
                    "剩余现金快照超过24小时、缺少时点或晚于决策时点"
                )
            candidate_volatility = _finite(
                it.get("_annualized_volatility_pct")
            )
            if candidate_volatility is None or candidate_volatility <= 0:
                execution_sizing_gaps.append(
                    "缺少候选历史波动率，波动缩放不可核验"
                )
            has_account_history = bool(
                portfolio_positions
                or int((portfolio or {}).get("closed_count") or 0) > 0
            )
            if (
                has_account_history
                and (risk_context or {}).get("initial_equity_status")
                == "missing_after_realized_history"
            ):
                execution_sizing_gaps.append(
                    "账户已有实现盈亏但未记录稳定起始权益，净值与回撤不可复原"
                )
            if (
                has_account_history
                and drawdown_policy.get("status") == "unavailable_not_applied"
            ):
                execution_sizing_gaps.append(
                    "账户净值历史不足，回撤制动不可核验"
                )
            if (
                portfolio_positions
                and ((risk_context or {}).get("var") or {}).get("available")
                is not True
            ):
                execution_sizing_gaps.append(
                    "已有持仓缺少VaR，组合尾部风险不可核验"
                )
            if (
                portfolio_positions
                and ((risk_context or {}).get("beta") or {}).get("available")
                is not True
            ):
                execution_sizing_gaps.append(
                    "已有持仓缺少Beta，市场敏感度不可核验"
                )
            if (
                portfolio_positions
                and dependency_policy.get("status")
                == "unavailable_not_applied"
            ):
                execution_sizing_gaps.append(
                    "候选与现有持仓相关性不可核验"
                )
        if execution_sizing_gaps:
            if held_int > 0:
                shares = min(shares, held_int)
                requested_add_shares = 0
            else:
                skipped.append({
                    "code": code, "name": name, "price": price,
                    "quality_score": it["_quality_score"],
                    "reason": "；".join(execution_sizing_gaps),
                    "risk_budget_status": "execution_evidence_incomplete",
                    "execution_sizing_gaps": execution_sizing_gaps,
                })
                continue
        candidate_risk_scale = min(
            combined_risk_scale, float(dependency_policy["scale"]),
        )
        planned_risk = _planned_loss_budget(
            it, entry_price=price, planning_equity=planning_equity,
            maximum_target_shares=requested_add_shares,
            risk_scale=candidate_risk_scale,
            portfolio_remaining_yuan=remaining_portfolio_risk,
        )
        if requested_add_shares <= 0:
            planned_risk = {
                **planned_risk,
                "status": "not_applicable_no_new_shares",
                "max_shares": 0,
                "risk_budget_pct": 0.0,
                "risk_budget_yuan": 0.0,
            }
        elif planned_risk["status"] == "ready":
            shares = min(
                shares, held_int + int(planned_risk["max_shares"] or 0),
            )
        elif entry_context and entry_context.get("executable") is True:
            if held_int > 0:
                # Existing inventory is already counted in portfolio heat.  A
                # missing risk line blocks only the proposed add, not a hold or
                # reduction of shares that already exist.
                shares = min(shares, held_int)
            else:
                skipped.append({
                    "code": code, "name": name, "price": price,
                    "quality_score": it["_quality_score"],
                    "reason": "缺少低于入场价的有效风险线，无法计算计划亏损与精确股数",
                    "risk_budget_status": planned_risk["status"],
                })
                continue
        while shares >= 100:
            amount = shares * price
            add_shares = max(0, shares - held_int) if held_int else shares
            add_shares = (add_shares // 100) * 100
            buy_amount = add_shares * price
            buy_cost = (
                buy_amount * one_side_cost_rate(buy_amount, "buy")
                if buy_amount else 0.0
            )
            projected_exposure = total_used - replaced_market_value + amount
            exposure_with_cost = (
                projected_exposure
                if portfolio is not None
                else projected_exposure + total_buy_cost + buy_cost
            )
            projected_new_buy_cash = total_new_buy_cash + buy_amount + buy_cost
            if (
                exposure_with_cost <= deployable + 1e-9
                and projected_new_buy_cash <= max(0.0, available_cash) + 1e-9
            ):
                break
            shares -= 100
        if shares < 100:
            risk_reason = (
                f"{candidate_industry}行业的组合暴露已达到"
                f"{MAX_INDUSTRY_EXPOSURE_PCT:g}%风险上限，剩余额度不足1手"
                if candidate_industry and industry_capacity is not None
                and industry_capacity < price * 100 else
                f"到风险线的单笔计划损失上限为账户权益的"
                f"{MAX_PLANNED_LOSS_PER_NEW_POSITION_PCT:g}%（¥{planned_risk['risk_budget_yuan']:,.0f}），不足1手"
                if planned_risk["status"] == "ready" else
                f"¥{price:.2f}/股，1手需¥{price*100:,.0f}，费用或容量后的分配¥{target:,.0f}不够一手"
            )
            skipped.append({"code": code, "name": name, "price": price,
                            "score": it.get("score"), "quality_score": it["_quality_score"],
                            "reason": risk_reason,
                            "risk_budget_status": planned_risk["status"]})
            continue
        amount = shares * price
        total_used = total_used - replaced_market_value + amount
        held = int(held) if held else 0
        if held > 0:
            add_shares = max(0, shares - held)   # 补仓到目标（含已持仓）
            add_shares = (add_shares // 100) * 100  # 整手
            held_ok = held >= shares
        else:
            add_shares = shares
            held_ok = False
        buy_amount = add_shares * price
        buy_cost = (
            buy_amount * one_side_cost_rate(buy_amount, "buy")
            if buy_amount else 0.0
        )
        total_buy_cost += buy_cost
        total_new_buy_cash += buy_amount + buy_cost
        if held_industry:
            projected_industry_values[held_industry] = max(
                0.0,
                projected_industry_values.get(held_industry, 0.0)
                - replaced_market_value,
            )
        elif portfolio_holding is not None:
            unknown_industry_value = max(
                0.0, unknown_industry_value - replaced_market_value,
            )
        if candidate_industry:
            projected_industry_values[candidate_industry] = (
                projected_industry_values.get(candidate_industry, 0.0) + amount
            )
        else:
            unknown_industry_value += amount
        reduce_shares = max(0, held - shares)
        reduce_amount = reduce_shares * price
        entry_timing = entry_timing_status(
            current_price=price,
            open_price=it.get("open_price"),
            previous_close=it.get("prev_close") or it.get("previous_close"),
            session_executable=bool(
                entry_context and entry_context.get("executable") is True
            ),
        )
        strategy_ref = it.get("strategy_ref")
        if not (
            isinstance(strategy_ref, dict)
            and strategy_ref.get("production_eligible") is True
        ):
            strategy_ref = None
        allocation_evidence = {
            "method": "opportunity_quality_portfolio_risk_allocation_v3",
            "code": code,
            "quality_data_as_of": quality.get("data_as_of"),
            "rank_score": round(it["_quality_score"], 6),
            "market_target_exposure_pct": round(deployable_pct, 6),
            "entry_timing": entry_timing,
            "planned_loss_budget": copy.deepcopy(planned_risk),
            "candidate_dependency_policy": copy.deepcopy(dependency_policy),
        }
        if strategy_ref is not None:
            allocation_evidence["strategy_ref"] = copy.deepcopy(strategy_ref)
        entry_executable = bool(
            entry_context
            and entry_context.get("executable") is True
            and (it.get("assessment") or {}).get("manual_actionable") is True
            and held == 0
            and add_shares > 0
            and entry_timing["actionable"] is True
        )
        trade_plan = build_trade_plan({
            "current_shares": held,
            "target_shares": shares,
            "current_price": price,
            "entry_status": "EXECUTABLE" if entry_executable else None,
            "entry_price": price if entry_executable else None,
            "limit_price": price,
            "valid_until": (
                entry_context.get("valid_until")
                if entry_executable else quality.get("data_as_of")
            ),
            "cancel_condition": (
                "开盘或当前价超过未复权昨收2%上限；"
                "机会质量、市场暴露或实际持仓变化后重新计算"
            ),
            "estimated_cost": (
                reduce_amount * one_side_cost_rate(reduce_amount, "sell")
                if reduce_amount > 0 else buy_cost if entry_executable else None
            ),
            "evidence_identity": allocation_evidence,
            "observations": [{
                "kind": "target_allocation",
                "detail": (
                    "按机会质量、波动、流动性、整手与现金共同计算；"
                    + str(entry_timing["reason"])
                ),
            }],
        })
        planned_loss = (
            _planned_loss_yuan(
                price, float(planned_risk["risk_line_price"]), add_shares,
            )
            if planned_risk["status"] == "ready" else None
        )
        if planned_loss is not None and remaining_portfolio_risk is not None:
            remaining_portfolio_risk = max(
                0.0, remaining_portfolio_risk - planned_loss,
            )
        industry_after = (
            projected_industry_values.get(candidate_industry)
            if candidate_industry else unknown_industry_value
        )
        allocations.append({
            "code": code, "name": name, "price": price, "score": it.get("score"),
            "quality_score": round(it["_quality_score"], 2),
            "volatility_scale": round(it["_volatility_scale"], 4),
            "allocation_score": round(it["_allocation_score"], 2),
            "risk_budget_status": planned_risk["status"],
            "risk_budget_method": planned_risk["method"],
            "planned_loss_budget_pct": planned_risk["risk_budget_pct"],
            "planned_loss_budget_yuan": planned_risk["risk_budget_yuan"],
            "planned_loss_at_risk_line_yuan": (
                round(planned_loss, 2) if planned_loss is not None else None
            ),
            "planned_loss_pct_of_equity": (
                round(planned_loss / planning_equity * 100.0, 4)
                if planned_loss is not None and planning_equity > 0 else None
            ),
            "risk_line_price": planned_risk["risk_line_price"],
            "risk_line_distance_pct": planned_risk.get("risk_line_distance_pct"),
            "actual_loss_can_exceed_plan": True,
            "drawdown_risk_scale": drawdown_policy["scale"],
            "portfolio_market_risk_scale": portfolio_market_risk_policy["scale"],
            "candidate_dependency_status": candidate_dependency.get("status"),
            "candidate_portfolio_correlation": candidate_dependency.get(
                "portfolio_correlation"
            ),
            "candidate_maximum_position_correlation": candidate_dependency.get(
                "maximum_position_correlation"
            ),
            "candidate_dependency_risk_scale": dependency_policy["scale"],
            "candidate_dependency_reason": dependency_policy.get("reason"),
            "candidate_dependency_observations": dependency_policy.get(
                "observations"
            ),
            "candidate_dependency_data_as_of": dependency_policy.get("data_as_of"),
            "combined_new_trade_risk_scale": candidate_risk_scale,
            "industry": candidate_industry or None,
            "industry_risk_bucket": (
                candidate_industry or "未分类风险桶"
            ),
            "industry_exposure_cap_pct": MAX_INDUSTRY_EXPOSURE_PCT,
            "unknown_industry_reserve_before_yuan": round(
                unknown_reserve_before, 2,
            ),
            "industry_capacity_before_yuan": (
                round(industry_capacity, 2)
                if industry_capacity is not None else None
            ),
            "industry_exposure_after_pct": (
                round(industry_after / planning_equity * 100.0, 4)
                if industry_after is not None and planning_equity > 0 else None
            ),
            "score_pct": round(share * 100, 1),
            "shares": shares, "amount": round(amount, 2),
            "target_weight_pct": round(amount / planning_equity * 100, 2) if planning_equity else 0,
            "levels": it.get("levels") or {},
            "commission_sensitive": buy_amount < 10000 if buy_amount else False,
            "estimated_buy_cost_yuan": round(buy_cost, 2),
            "estimated_buy_cash_yuan": round(buy_amount + buy_cost, 2),
            "round_trip_cost_pct": round(round_trip_cost_rate(amount) * 100, 4),
            "avg_amount_20d_yuan": round(avg_amount, 2) if avg_amount else None,
            "liquidity_capacity_yuan": (
                round(liquidity_capacity, 2) if liquidity_capacity else None
            ),
            "participation_pct": (
                round(amount / avg_amount * 100, 4)
                if avg_amount and avg_amount > 0 else None
            ),
            "held_shares": held, "held_ok": held_ok,
            "add_shares": add_shares if held > 0 else None,
            "reduce_shares": (
                trade_plan.get("action", {}).get("shares")
                if trade_plan.get("state") == "REDUCE_REVIEW"
                and isinstance(trade_plan.get("action"), dict) else None
            ),
            "entry_timing": entry_timing,
            "strategy_ref": copy.deepcopy(strategy_ref),
            "trade_plan": trade_plan,
        })
    # 汇总：真实占用 vs 目标
    used_pct = total_used / planning_equity * 100 if planning_equity else 0
    projected_cash = (
        available_cash - total_new_buy_cash
        if portfolio is not None else capital - total_used - total_buy_cost
    )
    planned_new_risk = sum(
        float(row.get("planned_loss_at_risk_line_yuan") or 0.0)
        for row in allocations
    )
    portfolio_risk["planned_new_risk_yuan"] = round(planned_new_risk, 2)
    portfolio_risk["remaining_after_plan_yuan"] = (
        round(remaining_portfolio_risk, 2)
        if remaining_portfolio_risk is not None else None
    )
    portfolio_risk["projected_total_risk_yuan"] = (
        round(
            float(portfolio_risk.get("existing_open_risk_yuan") or 0.0)
            + planned_new_risk,
            2,
        ) if portfolio_risk.get("existing_open_risk_yuan") is not None else None
    )
    return {
        "capital": capital,
        "planning_equity": round(planning_equity, 2),
        "items": allocations,
        "skipped": skipped,
        "total_used": round(total_used, 2),
        "estimated_buy_cost_yuan": round(total_buy_cost, 2),
        "estimated_new_buy_cash_yuan": round(total_new_buy_cash, 2),
        "used_pct": round(used_pct, 1),
        "cash_buffer": round(projected_cash, 2),
        "cash_buffer_pct": round(projected_cash / planning_equity * 100, 2) if planning_equity else 0.0,
        "market_target_exposure_pct": round(deployable_pct, 2),
        "position_count": (
            len(set(portfolio_by_code) | {str(row["code"]) for row in allocations})
            if portfolio is not None else len(allocations)
        ),
        "existing_position_count": len(portfolio_positions) if portfolio is not None else 0,
        "existing_positions": portfolio_positions if portfolio is not None else [],
        "portfolio_valuation_status": "complete" if portfolio is not None else None,
        "plan_status": "ready",
        "max_positions": MAX_OPEN_POSITIONS,
        "drawdown_risk_policy": drawdown_policy,
        "portfolio_market_risk_policy": portfolio_market_risk_policy,
        "portfolio_risk_heat": portfolio_risk,
        "industry_concentration_policy": {
            "status": (
                "bounded_with_unknown_reserve"
                if industry_unknown_codes else "complete"
            ),
            "method": "known_industry_plus_unknown_worst_case_reserve_v1",
            "maximum_industry_exposure_pct": MAX_INDUSTRY_EXPOSURE_PCT,
            "unknown_position_codes": industry_unknown_codes,
            "current_unknown_industry_market_value_yuan": round(
                current_unknown_industry_value, 2,
            ),
            "projected_unknown_industry_market_value_yuan": round(
                unknown_industry_value, 2,
            ),
            "unknown_industry_market_value_yuan": round(
                unknown_industry_value, 2,
            ),
            "unknown_exposure_reserved_against_every_candidate": True,
            "missing_candidate_industry_does_not_invent_a_classification": True,
            "profit_optimality_claim": False,
        },
        "note": (
            f"总仓位上限{deployable_pct:g}%、单股上限{MAX_SINGLE_POSITION_PCT:g}%、"
            f"最低现金{MIN_CASH_RESERVE_PCT:g}%；整手取整后实际占用可能更低；"
            f"每笔新仓到明确风险线的计划损失不超过账户权益{MAX_PLANNED_LOSS_PER_NEW_POSITION_PCT:g}%，"
            "并计入买卖费用；该上限不是最优仓位，跳空、跌停或停牌时实际损失可能更大；"
            "机会质量按历史波动只减不增；精确买入计划要求ADV20完整，"
            "研究测算有值时单股不超过0.5%日均成交额；"
            f"组合计划风险不超过权益{MAX_PORTFOLIO_OPEN_RISK_PCT:g}%，账户回撤只下调单笔风险，"
            f"单行业不超过权益{MAX_INDUSTRY_EXPOSURE_PCT:g}%，"
            "行业缺失时按未知风险桶对所有候选预留；"
            "候选与现有持仓有至少60个共同交易日且高度同向时，只下调新增风险预算；"
            "买入费用已计最低5元佣金、过户费和滑点。"
            "已持仓股票自动识别，建议为补仓数量。"
        ),
    }


__all__ = [
    "allocate_capital", "allocate_capital_with_risk",
    "build_position_sizing_risk_context",
    "ledger_position_cost", "manual_portfolio_status",
    "entry_timing_status",
    "opportunity_market_evidence", "portfolio_exposure_pct",
    "position_trade_plan", "refine_opportunity_quality", "remaining_buy_cost",
]
