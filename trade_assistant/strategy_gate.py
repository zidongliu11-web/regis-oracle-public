"""Independent, fail-closed historical strategy gate.

Research reports are evidence, not authority.  This module recalculates the
gate from recorded out-of-sample metrics so a stale or AI-edited boolean
cannot unlock recommendations.  Passing means that the *historical evidence*
met the registered thresholds; it is never a promise of future profit.
"""

from __future__ import annotations

import math
from typing import Any


MIN_OOS_TRADING_DAYS = 400
MIN_COMPLETED_TRADES = 100
MIN_OBSERVED_WIN_RATE_PCT = 60.0
MIN_PROFIT_FACTOR = 1.20
MAX_DRAWDOWN_PCT = 15.0
MIN_POSITIVE_FOLD_RATIO_PCT = 60.0
MIN_WIN_RATE_CI_LOW_PCT = 50.0


GATE_LABELS = {
    "oos_trading_days_at_least_400": "样本外至少400个交易日",
    "completed_trades_at_least_100": "样本外至少100笔完成交易",
    "observed_win_rate_at_least_60": "样本外胜率至少60%",
    "win_rate_wilson_95_ci_low_above_50": "胜率95%区间下界高于50%",
    "observed_net_return_positive": "样本外扣全成本净收益为正",
    "observed_annualized_return_positive": "样本外年化收益为正",
    "observed_profit_factor_at_least_1_20": "样本外盈利因子至少1.20",
    "observed_max_drawdown_at_most_15": "样本外最大回撤不超过15%",
    "positive_fold_ratio_at_least_60": "样本外正收益分段至少60%",
    "validation_10bp_net_return_positive": "验证期扣10BP净收益为正",
    "observed_10bp_net_return_positive": "测试期扣10BP净收益为正",
    "observed_20bp_net_return_non_negative": "测试期扣20BP净收益非负",
    "full_cost_model_recorded": "已记录最低佣金、税费和滑点",
    "no_open_positions_at_end": "样本末无未结持仓",
}


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _scenario(report: dict[str, Any], split: str, name: str) -> dict[str, Any]:
    value = ((report.get("results") or {}).get(split) or {}).get(name) or {}
    return value if isinstance(value, dict) else {}


def _primary_evidence(report: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return observed 10BP, validation 10BP, observed 20BP, and metadata."""

    observed_10 = _scenario(report, "observed_test", "round_trip_10bp")
    if observed_10:
        return (
            observed_10,
            _scenario(report, "validation", "round_trip_10bp"),
            _scenario(report, "observed_test", "round_trip_20bp"),
            {"evidence_layout": "nested_cost_stress"},
        )
    # Legacy reports expose one already-costed OOS result at the top level.
    return report, report, report, {"evidence_layout": "top_level_costed"}


def wilson_lower_bound_pct(wins: int, trades: int, z: float = 1.959963984540054) -> float | None:
    if trades <= 0 or wins < 0 or wins > trades:
        return None
    p = wins / trades
    denominator = 1 + z * z / trades
    centre = p + z * z / (2 * trades)
    adjustment = z * math.sqrt((p * (1 - p) + z * z / (4 * trades)) / trades)
    return (centre - adjustment) / denominator * 100


def _positive_fold_ratio(evidence: dict[str, Any]) -> float | None:
    recorded = _number(evidence.get("positive_fold_ratio_pct"))
    if recorded is not None:
        return recorded
    folds = evidence.get("folds") or []
    if not folds:
        return None
    positive = sum((_number(item.get("return_pct")) or 0) > 0 for item in folds)
    return positive / len(folds) * 100


def evaluate_historical_strategy_gate(report: dict[str, Any] | None) -> dict[str, Any]:
    """Recompute the strict historical gate and expose every failed condition."""

    if not isinstance(report, dict) or not report:
        conditions = {key: False for key in GATE_LABELS}
        return {
            "passed": False,
            "conditions": conditions,
            "failed_gates": list(GATE_LABELS.values()),
            "metrics": {},
            "reason": "尚无可复算的含成本样本外策略报告。",
        }

    observed, validation, stress20, metadata = _primary_evidence(report)
    metrics = observed.get("metrics") or {}
    validation_metrics = validation.get("metrics") or {}
    stress20_metrics = stress20.get("metrics") or {}
    trades = int(_number(metrics.get("trades")) or 0)
    wins_value = _number(metrics.get("wins"))
    win_rate = _number(metrics.get("win_rate_pct"))
    wins = int(wins_value) if wins_value is not None else int(round(trades * (win_rate or 0) / 100))
    ci_low = wilson_lower_bound_pct(wins, trades)
    trading_days = int(
        _number(metrics.get("oos_trading_days"))
        or _number(metrics.get("trading_days"))
        or 0
    )
    positive_fold_ratio = _positive_fold_ratio(observed)
    total_return = _number(metrics.get("total_return_pct"))
    annualized_return = _number(metrics.get("annualized_return_pct"))
    profit_factor = _number(metrics.get("profit_factor"))
    max_drawdown = _number(metrics.get("max_drawdown_pct"))

    nested = metadata["evidence_layout"] == "nested_cost_stress"
    cost_model = report.get("cost_model") or report.get("data") or {}
    transaction_costs = (
        _number(metrics.get("transaction_fees"))
        if metrics.get("transaction_fees") is not None
        else _number(metrics.get("estimated_transaction_costs"))
    )
    full_cost_recorded = bool(cost_model) and transaction_costs is not None
    open_positions = (observed.get("order_stats") or {}).get("open_positions_end")
    if open_positions is None:
        open_positions = report.get("open_positions_end")
    no_open_positions = open_positions == 0 or open_positions == []

    validation_return = _number(validation_metrics.get("total_return_pct"))
    stress20_return = _number(stress20_metrics.get("total_return_pct"))
    conditions = {
        "oos_trading_days_at_least_400": trading_days >= MIN_OOS_TRADING_DAYS,
        "completed_trades_at_least_100": trades >= MIN_COMPLETED_TRADES,
        "observed_win_rate_at_least_60": win_rate is not None and win_rate >= MIN_OBSERVED_WIN_RATE_PCT,
        "win_rate_wilson_95_ci_low_above_50": ci_low is not None and ci_low > MIN_WIN_RATE_CI_LOW_PCT,
        "observed_net_return_positive": total_return is not None and total_return > 0,
        "observed_annualized_return_positive": annualized_return is not None and annualized_return > 0,
        "observed_profit_factor_at_least_1_20": profit_factor is not None and profit_factor >= MIN_PROFIT_FACTOR,
        "observed_max_drawdown_at_most_15": max_drawdown is not None and max_drawdown <= MAX_DRAWDOWN_PCT,
        "positive_fold_ratio_at_least_60": positive_fold_ratio is not None and positive_fold_ratio >= MIN_POSITIVE_FOLD_RATIO_PCT,
        "validation_10bp_net_return_positive": (
            validation_return is not None and validation_return > 0
        ),
        "observed_10bp_net_return_positive": total_return is not None and total_return > 0,
        "observed_20bp_net_return_non_negative": (
            stress20_return is not None and stress20_return >= 0
        ) if nested else False,
        "full_cost_model_recorded": full_cost_recorded,
        "no_open_positions_at_end": no_open_positions,
    }
    failed = [GATE_LABELS[key] for key, passed in conditions.items() if not passed]
    return {
        "passed": all(conditions.values()),
        "conditions": conditions,
        "failed_gates": failed,
        "metrics": {
            "trading_days": trading_days,
            "trades": trades,
            "wins": wins,
            "win_rate_pct": win_rate,
            "win_rate_wilson_95_ci_low_pct": round(ci_low, 3) if ci_low is not None else None,
            "total_return_pct": total_return,
            "annualized_return_pct": annualized_return,
            "profit_factor": profit_factor,
            "max_drawdown_pct": max_drawdown,
            "positive_fold_ratio_pct": positive_fold_ratio,
            "validation_10bp_total_return_pct": validation_return,
            "observed_20bp_total_return_pct": stress20_return,
        },
        "thresholds": {
            "minimum_trades": MIN_COMPLETED_TRADES,
            "minimum_win_rate_pct": MIN_OBSERVED_WIN_RATE_PCT,
            "minimum_profit_factor": MIN_PROFIT_FACTOR,
            "maximum_drawdown_pct": MAX_DRAWDOWN_PCT,
            "minimum_positive_fold_ratio_pct": MIN_POSITIVE_FOLD_RATIO_PCT,
        },
        "evidence_layout": metadata["evidence_layout"],
        "reason": (
            "历史样本外严格门禁通过；这仍不是未来盈利保证。"
            if all(conditions.values())
            else "历史样本外严格门禁未通过，不能生成可执行买入推荐。"
        ),
    }
