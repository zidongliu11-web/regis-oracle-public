"""A 股费用模型（同花顺口径，可审计常量）。

费率（2026 当前口径）：
  - 佣金    : 0.025%（万2.5）双边，**最低 ¥5/笔**
  - 印花税  : 0.05%（0.0005）**仅卖出**单边
  - 过户费  : 0.001%（0.00001）双边
  - 滑点    : 默认 0.1%（10bp）双边，可配置

单笔成交成本率 = (买佣金 + 卖佣金 + 卖印花 + 过户×2 + 滑点×2) / 名义金额，
佣金用 max(¥5, 名义×费率) 建模。全部数字可审计、可复核。
"""
from __future__ import annotations

import math

BROKER_COMMISSION_ALL_IN_RATE = 0.00025
BROKER_COMMISSION_INCLUDES_REGULATORY_FEES = True
SECURITIES_MANAGEMENT_FEE_RATE = 0.00002
EXCHANGE_HANDLING_FEE_RATE = 0.0000341

COSTS = {
    "stamp": 0.0005,        # 印花税：仅卖出
    "commission": BROKER_COMMISSION_ALL_IN_RATE,  # 含证管费/经手费，不重复叠加
    "transfer": 0.00001,    # 过户费：双边
    "slippage": 0.001,      # 滑点：双边，默认 0.1%
}
MIN_COMMISSION_YUAN = 5.0   # 单笔最低佣金
LOT_SIZE = 100              # A 股整手 = 100 股
DEFAULT_POSITION_YUAN = 20_000.0   # 默认单标的资金量（20万账户/约10仓）

# 佣金费率档：费用随名义金额变化的分段（最小起收点）
def _commission_yuan(notional: float) -> float:
    """单笔佣金（元）：max(¥5, 名义×万2.5)。"""
    return max(MIN_COMMISSION_YUAN, notional * COSTS["commission"])


def one_side_cost_rate(notional: float, side: str, slippage: float | None = None) -> float:
    """单边成本率（占名义金额比例）。
    side: 'buy' | 'sell'。佣金双边；印花税仅 sell；过户费双边；滑点双边。
    """
    if notional <= 0:
        return 0.0
    sl = COSTS["slippage"] if slippage is None else slippage
    commission = _commission_yuan(notional) / notional
    transfer = COSTS["transfer"]
    stamp = COSTS["stamp"] if side == "sell" else 0.0
    return commission + transfer + stamp + sl


def one_side_cost_breakdown(
    notional: float, side: str, slippage: float | None = None,
    market_impact_rate: float = 0.0,
) -> dict[str, float | bool | str]:
    """可审计的单边成本；佣金按 all-in 口径，规费不重复计提。"""
    if side not in {"buy", "sell"}:
        raise ValueError("side 必须是 buy 或 sell")
    try:
        amount = float(notional)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("notional 必须是有限非负数") from exc
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("notional 必须是有限非负数")
    sl = COSTS["slippage"] if slippage is None else float(slippage)
    impact_rate = float(market_impact_rate)
    if not math.isfinite(sl) or sl < 0:
        raise ValueError("slippage 必须是有限非负数")
    if not math.isfinite(impact_rate) or impact_rate < 0:
        raise ValueError("market_impact_rate 必须是有限非负数")
    if amount == 0:
        return {
            "commission": 0.0, "transfer": 0.0, "stamp": 0.0,
            "slippage": 0.0, "market_impact": 0.0, "total": 0.0,
            "broker_commission_includes_regulatory_fees": True,
            "commission_calibration": "default_assumption_needs_user_broker_statement",
        }
    commission = _commission_yuan(amount)
    transfer = amount * COSTS["transfer"]
    stamp = amount * COSTS["stamp"] if side == "sell" else 0.0
    slippage_yuan = amount * sl
    market_impact = amount * impact_rate
    return {
        "commission": commission,
        "transfer": transfer,
        "stamp": stamp,
        "slippage": slippage_yuan,
        "market_impact": market_impact,
        "total": commission + transfer + stamp + slippage_yuan + market_impact,
        "broker_commission_includes_regulatory_fees": True,
        "commission_calibration": "default_assumption_needs_user_broker_statement",
    }


def round_trip_cost_rate(notional: float, slippage: float | None = None) -> float:
    """一次完整建仓+清仓（买+卖）成本率（占单笔名义金额比例，同花顺口径）。"""
    return one_side_cost_rate(notional, "buy", slippage) + \
           one_side_cost_rate(notional, "sell", slippage)


def cost_rate_for_portfolio(capital: float = 200_000.0,
                            n_positions: int = 10,
                            slippage: float | None = None) -> float:
    """按账户资金/持仓数推出单标的资金量，再算往返成本率。
    因子回测/组合级回测用它把成本折算成每持仓名目的比例，避免每笔单独计价的量级依赖。"""
    per = capital / max(1, n_positions)
    return round_trip_cost_rate(per, slippage)


def lots_from_yuan(cash: float, price: float) -> int:
    """按价格与可用资金计算可买整手（100 股/手，向下取整）。"""
    if price is None or price <= 0 or cash is None or cash <= 0:
        return 0
    return int(cash / (price * LOT_SIZE)) * LOT_SIZE


def commission_yuan_for_lot(price: float, shares: int) -> float:
    """整手买卖单笔佣金（元），含最低 ¥5。"""
    return _commission_yuan(price * shares)


__all__ = [
    "COSTS", "MIN_COMMISSION_YUAN", "LOT_SIZE", "DEFAULT_POSITION_YUAN",
    "BROKER_COMMISSION_ALL_IN_RATE", "BROKER_COMMISSION_INCLUDES_REGULATORY_FEES",
    "SECURITIES_MANAGEMENT_FEE_RATE", "EXCHANGE_HANDLING_FEE_RATE",
    "one_side_cost_rate", "round_trip_cost_rate", "cost_rate_for_portfolio",
    "one_side_cost_breakdown", "lots_from_yuan", "commission_yuan_for_lot",
]
