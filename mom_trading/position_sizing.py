"""
Regis Oracle v3.1 — 资金-仓位规模计算引擎

根据本金展示站点唯一的持仓风险上限。
A股硬约束：最低100股/手，最低佣金5元/笔，T+1交收。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .data_semantics import (
    MAX_POSITIONS,
    MAX_SINGLE_POSITION_PCT,
    MAX_TOTAL_EXPOSURE_PCT,
    MIN_CASH_RESERVE_PCT,
    STAMP_TAX_RATE,
)

# ── A股硬约束 ──────────────────────────────────
LOT_SIZE = 100           # 最低买入单位（1手=100股）
MIN_COMMISSION = 5.0     # 最低佣金（元）
COMMISSION_RATE = 0.00025  # 佣金率（万2.5）


@dataclass
class PositionPlan:
    """单一级别的仓位方案."""
    capital: float
    tier: str                    # "微型" / "小型" / "中型" / "大型"
    max_positions: int
    per_position_capital: float
    max_stock_price: float
    suggested_positions: int     # 兼容字段：风险上限内的最多持仓数
    cash_buffer_pct: float       # 现金缓冲比例
    min_diversified_positions: int  # 最低分散要求
    max_single_pct: float        # 单只最大占比
    max_industry_pct: float      # 单行业最大占比
    max_total_exposure_pct: float  # 总持仓市值/账户权益上限
    notes: list[str] = field(default_factory=list)


def calculate_plan(capital: float) -> PositionPlan:
    """Return the site's single frozen risk limit, not an invented optimum."""
    capital = float(capital)
    if not math.isfinite(capital) or capital <= 0:
        raise ValueError("capital must be a positive finite number")
    per_position = capital * MAX_SINGLE_POSITION_PCT / 100
    commission = max(MIN_COMMISSION, per_position * COMMISSION_RATE)
    tier = "微型" if capital < 5_000 else "小型" if capital < 50_000 else "中型" if capital < 200_000 else "大型"
    return PositionPlan(
        capital=capital,
        tier=tier,
        max_positions=MAX_POSITIONS,
        per_position_capital=round(per_position, 2),
        max_stock_price=round(per_position / LOT_SIZE, 2),
        suggested_positions=MAX_POSITIONS,
        cash_buffer_pct=MIN_CASH_RESERVE_PCT,
        min_diversified_positions=1,
        max_single_pct=MAX_SINGLE_POSITION_PCT,
        max_industry_pct=40.0,
        max_total_exposure_pct=MAX_TOTAL_EXPOSURE_PCT,
        notes=[
            "联合风险上限：最多5仓、单仓不超过20%、总暴露不超过80%、现金不低于20%。",
            "四项上限必须同时满足；最多持仓数不等于每仓都可用20%，不是收益最优建议。",
            "审计大盘目标暴露若低于80%，取更低值作为当前上限。",
            f"按单仓上限估算买入佣金约 {commission:.2f} 元（最低5元）。",
        ],
    )


def get_all_plans() -> list[dict[str, Any]]:
    """返回所有标准资金级别的仓位方案（用于前端展示）."""
    tiers = [3_000, 5_000, 10_000, 20_000, 30_000, 50_000,
             100_000, 200_000, 500_000, 1_000_000]
    plans = []
    for capital in tiers:
        p = calculate_plan(capital)
        plans.append({
            "capital": p.capital,
            "tier": p.tier,
            "suggested_positions": p.suggested_positions,
            "max_positions": p.max_positions,
            "per_position": p.per_position_capital,
            "max_stock_price": p.max_stock_price,
            "max_single_pct": p.max_single_pct,
            "max_industry_pct": p.max_industry_pct,
            "max_total_exposure_pct": p.max_total_exposure_pct,
            "cash_buffer_pct": p.cash_buffer_pct,
            "notes": p.notes,
        })
    return plans


def sizing_summary(capital: float) -> dict[str, Any]:
    """获取仓位建议摘要（API 返回格式）."""
    plan = calculate_plan(capital)
    all_plans = get_all_plans()

    # 当前级别在全部级别中的位置
    tier_order = {"微型": 0, "小型": 1, "中型": 2, "大型": 3}
    current_tier_idx = tier_order.get(plan.tier, 0)

    # 下一级别的资金门槛
    next_tier = None
    for p in all_plans:
        if p["capital"] > capital:
            next_tier = {
                "capital": p["capital"],
                "tier": p["tier"],
                "positions": p["suggested_positions"],
                "gap": p["capital"] - capital,
            }
            break

    return {
        "capital": plan.capital,
        "tier": plan.tier,
        "tier_index": current_tier_idx,
        "suggested_positions": plan.suggested_positions,
        "max_positions": plan.max_positions,
        "min_diversified_positions": plan.min_diversified_positions,
        "per_position_capital": plan.per_position_capital,
        "max_stock_price": plan.max_stock_price,
        "max_single_pct": plan.max_single_pct,
        "max_industry_pct": plan.max_industry_pct,
        "max_total_exposure_pct": plan.max_total_exposure_pct,
        "min_cash_reserve_pct": MIN_CASH_RESERVE_PCT,
        "cash_buffer_pct": plan.cash_buffer_pct,
        "lot_size": LOT_SIZE,
        "min_commission": MIN_COMMISSION,
        "commission_rate": COMMISSION_RATE,
        "stamp_tax_rate_sell_only": STAMP_TAX_RATE,
        "policy_status": "risk_limit_only",
        "not_investment_recommendation": True,
        "notes": plan.notes,
        "all_tiers": all_plans,
        "next_tier": next_tier,
    }
