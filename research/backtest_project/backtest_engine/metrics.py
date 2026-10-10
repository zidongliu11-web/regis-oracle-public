#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
metrics.py — 业绩指标（任务书第八部分）

收益类：年化、超额(沪深300)、单笔最大盈利
风险类：最大回撤、夏普、索提诺、卡尔马
交易类：胜率、盈亏比、平均持仓天数
稳定性：月度胜率、变异系数(跨回测汇总用)、样本内外差异

短窗口注意：E5 仅 6 个交易日，年化数字会极端放大，报告须同时给出区间收益与年化。
"""
from __future__ import annotations
from typing import List, Optional

import numpy as np
import pandas as pd

TRADING_DAYS = 250


def compute_metrics(equity: pd.Series, trades: List[dict],
                    benchmark_total_return: Optional[float] = None,
                    rf: float = 0.0) -> dict:
    """
    equity: 日度净值 Series(升序, 第一点为初始资本)
    trades: 成交记录列表（含 pnl/held_days 的已平仓明细由 portfolio 附加）
    """
    eq = equity.dropna()
    m = {}
    n = len(eq) - 1
    if n <= 0:
        return {'error': 'no_equity'}
    total_ret = eq.iloc[-1] / eq.iloc[0] - 1
    ann_ret = (1 + total_ret) ** (TRADING_DAYS / n) - 1 if n > 0 else np.nan
    rets = eq.pct_change().dropna()
    ann_vol = rets.std(ddof=0) * np.sqrt(TRADING_DAYS)
    sharpe = (ann_ret - rf) / ann_vol if ann_vol and ann_vol > 0 else np.nan
    down = rets[rets < (rf / TRADING_DAYS)]
    down_vol = down.std(ddof=0) * np.sqrt(TRADING_DAYS)
    sortino = (ann_ret - rf) / down_vol if down_vol and down_vol > 0 else np.nan
    dd_series = eq / eq.cummax() - 1
    max_dd = dd_series.min()
    calmar = ann_ret / abs(max_dd) if max_dd and max_dd < 0 else np.nan

    # 月度胜率
    try:
        monthly = eq.resample('ME').last().pct_change().dropna()
        monthly_win = float((monthly > 0).mean()) if len(monthly) else np.nan
        n_months = int(len(monthly))
    except Exception:
        monthly_win, n_months = np.nan, 0

    # 交易统计
    closed = [t for t in trades if t.get('side') == 'sell' and t.get('pnl') is not None]
    wins = [t for t in closed if t['pnl'] > 0]
    losses = [t for t in closed if t['pnl'] <= 0]
    win_rate = len(wins) / len(closed) if closed else np.nan
    avg_win = float(np.mean([t['pnl'] for t in wins])) if wins else np.nan
    avg_loss = float(np.mean([t['pnl'] for t in losses])) if losses else np.nan
    profit_loss_ratio = (avg_win / abs(avg_loss)) if (losses and avg_loss != 0) else np.nan
    hold_days = [t.get('held_days') for t in closed if t.get('held_days') is not None]
    avg_hold_days = float(np.mean(hold_days)) if hold_days else np.nan

    total_cost = float(sum(t.get('cost_total', 0.0) for t in trades))
    traded_amount = sum(t.get('amount', 0.0) for t in trades if t.get('side') == 'buy')
    turnover = traded_amount / eq.iloc[0] if eq.iloc[0] else np.nan
    max_single_win = float(max((t['pnl'] for t in wins), default=np.nan))
    max_single_loss = float(min((t['pnl'] for t in losses), default=np.nan))

    excess = (total_ret - benchmark_total_return) if benchmark_total_return is not None else np.nan

    m.update({
        'period_days': n,
        'total_return': float(total_ret),
        'annualized_return': float(ann_ret) if not np.isnan(ann_ret) else None,
        'annualized_vol': float(ann_vol) if not np.isnan(ann_vol) else None,
        'excess_return': float(excess) if not np.isnan(excess) else None,
        'max_drawdown': float(max_dd) if not np.isnan(max_dd) else None,
        'sharpe': float(sharpe) if not np.isnan(sharpe) else None,
        'sortino': float(sortino) if not np.isnan(sortino) else None,
        'calmar': float(calmar) if not np.isnan(calmar) else None,
        'monthly_win_rate': monthly_win, 'n_months': n_months,
        'win_rate': float(win_rate) if not np.isnan(win_rate) else None,
        'profit_loss_ratio': float(profit_loss_ratio) if not np.isnan(profit_loss_ratio) else None,
        'avg_hold_days': float(avg_hold_days) if not np.isnan(avg_hold_days) else None,
        'n_trades': len(trades), 'n_closed': len(closed),
        'total_cost_pct': float(total_cost / eq.iloc[0]),
        'turnover': float(turnover) if not np.isnan(turnover) else None,
        'max_single_win': max_single_win, 'max_single_loss': max_single_loss,
        'benchmark_return': float(benchmark_total_return) if benchmark_total_return is not None else None,
    })
    return m


def summarize_runs(metrics_list: List[dict], key: str = 'annualized_return') -> dict:
    """50 次回测汇总：均值/标准差/变异系数/正次数。"""
    vals = [m.get(key) for m in metrics_list if m.get(key) is not None]
    if not vals:
        return {'mean': None, 'std': None, 'cv': None, 'n_positive': 0, 'n': 0}
    arr = np.array(vals)
    mu = arr.mean()
    sd = arr.std(ddof=1) if len(arr) > 1 else 0.0
    cv = sd / abs(mu) if mu != 0 else np.nan
    return {'mean': float(mu), 'std': float(sd), 'cv': float(cv) if not np.isnan(cv) else None,
            'n_positive': int((arr > 0).sum()), 'n': int(len(arr))}


def oos_vs_is(in_sample: List[float], out_of_sample: List[float]) -> dict:
    """样本内 vs 样本外差异检验（任务书：差异>30% → 过拟合警告）。"""
    if not in_sample:
        return {}
    is_mean = float(np.mean(in_sample))
    oos_mean = float(np.mean(out_of_sample)) if out_of_sample else None
    if is_mean == 0:
        diff = None
    else:
        diff = (is_mean - oos_mean) / abs(is_mean)
    return {
        'in_sample_mean': is_mean,
        'out_of_sample_mean': oos_mean,
        'relative_diff_pct': diff * 100 if diff is not None else None,
        'overfit_flag': bool(diff is not None and diff > 0.30),
    }
