#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
executor.py — 成交执行（滑点、交易成本、涨跌停、停牌、流动性）

日线粒度实现（历史分钟不可得的诚实降级）：
  - 成交价 = 次日收盘价 × (1 ± 滑点)   [任务书分钟模型"下一分钟收盘价±滑点"的日线对应]
  - 涨跌停：主板 ±9.9%、创业板/科创板 ±19.9%、ST ±5%（任务书 9.9%/5% 为主板口径，
    创业板/科创板为实际 20% 涨跌幅限制，如实实现并标注）
  - 停牌（trade_status=0）不可成交
  - 流动性：单笔成交额 ≤ 当日成交额的 10%（任务书"≤5分钟成交量10%"的日线代理，标注）
  - 成本：佣金 0.025% 最低5元(双边)、印花税 0.05%(卖出)、过户费 0.001%(沪市双边)、滑点 0.1%(双边)
"""
from __future__ import annotations
import math
from typing import Optional, Tuple

import numpy as np
import pandas as pd

BOARD_LIMIT = {'MAIN_SH': 0.099, 'MAIN_SZ': 0.099, 'STAR': 0.199, 'GEM': 0.199,
               'BJ': 0.299, 'OTHER': 0.099}


def board_of(ts_code: str) -> str:
    if ts_code.endswith('.SH'):
        return 'STAR' if ts_code.startswith('68') else 'MAIN_SH'
    if ts_code.endswith('.SZ'):
        return 'GEM' if ts_code.startswith('30') else 'MAIN_SZ'
    if ts_code.endswith('.BJ'):
        return 'BJ'
    return 'OTHER'


def limit_threshold(ts_code: str, is_st: int) -> float:
    """当日涨跌停阈值（ST ±5%，其余按板块）。"""
    if is_st:
        return 0.05
    return BOARD_LIMIT.get(board_of(ts_code), 0.099)


def check_fillable(ts_code: str, is_st: int, pct_chg: float, trade_status: int,
                   side: str) -> Tuple[bool, str]:
    """检查 T+1 日能否成交。side ∈ {'buy','sell'}。

    ⚠ 2026-08-08 修复单位 bug：面板 pct_chg 为**百分制**（0.95=0.95%），此前与**小数**涨停阈值
    （0.099=9.9%）直接比较 → 任何上涨≥0.1% 的买入都被误判涨停、下跌≥0.1% 的卖出都被误判跌停，
    导致全部历史回测成交只在平/跌日买入、平/涨日卖出（严重失真）。现统一换算为小数比较。
    """
    if side not in {'buy', 'sell'}:
        return False, '无效交易方向'
    try:
        status = float(trade_status)
        st = float(is_st)
        pct = float(pct_chg)
    except (TypeError, ValueError):
        return False, '交易输入缺失'
    if not all(map(math.isfinite, (status, st, pct))):
        return False, '交易输入缺失'
    if status == 0:
        return False, '停牌'
    if status != 1 or st not in (0, 1):
        return False, '交易状态异常'
    lim = limit_threshold(ts_code, int(st))
    pct_dec = pct / 100.0            # 百分制 → 小数
    if side == 'buy' and pct_dec >= lim:
        return False, f'涨停(+{pct:.1f}%≥{lim*100:.0f}%)'
    if side == 'sell' and pct_dec <= -lim:
        return False, f'跌停({pct:.1f}%≤-{lim*100:.0f}%)'
    return True, ''


def compute_costs(side: str, amount: float, ts_code: str,
                  commission=0.00025, stamp=0.0005, transfer=0.00001, min_comm=5.0) -> dict:
    """计算交易成本。amount 为成交金额（含滑点）。返回各项与合计。"""
    comm = max(min_comm, amount * commission)
    trf = amount * transfer if ts_code.endswith('.SH') else 0.0
    stp = amount * stamp if side == 'sell' else 0.0
    return {'commission': float(comm), 'stamp': float(stp), 'transfer': float(trf),
            'total': float(comm + stp + trf)}


def fill_price(adj_close: float, side: str, slippage: float = 0.001) -> float:
    if side == 'buy':
        return adj_close * (1 + slippage)
    return adj_close * (1 - slippage)


def liquidity_cap(daily_amount: float, order_amount: float, ratio: float = 0.10) -> float:
    """单笔成交额 ≤ 当日成交额的 ratio；缺数和非正值一律拒绝。"""
    try:
        amount, order, share = float(daily_amount), float(order_amount), float(ratio)
    except (TypeError, ValueError):
        return 0.0
    if not all(map(math.isfinite, (amount, order, share))) or amount <= 0 or order <= 0 or not 0 < share <= 1:
        return 0.0
    return min(order, amount * share)
