"""Shared units and transaction-cost facts used by the decision site."""

from __future__ import annotations

import math


# THS collector stores ``net_yi`` in 亿元 already.  A historical extra /5 had
# no source contract and silently corrupted display and decision inputs.
FLOW_MAGNITUDE_SCALE = 1.0
STAMP_TAX_RATE = 0.0005  # 卖出单向 0.05%

# 全站唯一的账户风险上限；是联合约束，不是收益最优建议。
MAX_POSITIONS = 5
MAX_OPEN_POSITIONS = MAX_POSITIONS  # 语义别名，供持仓/确认路由使用
MAX_SINGLE_POSITION_PCT = 20
MAX_TOTAL_EXPOSURE_PCT = 80
MIN_CASH_RESERVE_PCT = 20
# 新开仓的“到计划风险线损失”上限。它是保守的风险政策起点，
# 不是收益最优或 Kelly 仓位；必须在完整系统回放中与旧分配法对照。
MAX_PLANNED_LOSS_PER_NEW_POSITION_PCT = 0.5
# 组合计划风险上限由“最多持仓数 × 单笔计划损失上限”直接推导，避免再造
# 一套互相矛盾的仓位参数。它约束的是到各自风险线的计划损失，不是 VaR。
MAX_PORTFOLIO_OPEN_RISK_PCT = (
    MAX_OPEN_POSITIONS * MAX_PLANNED_LOSS_PER_NEW_POSITION_PCT
)
# 与风险报告现有的高集中警戒线保持同一口径。该值是风险上限，不是收益最优权重。
MAX_INDUSTRY_EXPOSURE_PCT = 25


def normalize_fund_flow_yi(raw_value: object) -> float | None:
    """Validate the THS collector's already-normalised 亿元 value."""
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None
