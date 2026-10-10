#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
factor_library.py — 14 个初始固定因子（固定参数，不做参数搜索，不用历史 IC 筛选）

PIT 纪律：所有因子仅用 t 及之前数据（rolling/ewm/shift 均为因果操作），无前视。
主力净流入（f11）：历史大单净流入本机不可得（THS 仅实时）→ 标记数据源不可用，
返回全 NaN，报告标注"❌ 数据源不可用"，不参与等权合成。

返回结构：每个因子 → DataFrame(trade_date × ts_code)。
"""
from __future__ import annotations
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# (因子名, 类别, 说明, 数据源状态)
FACTOR_SPECS = [
    ('mom20',    '动量',   '20日动量: close_today/close_20d_ago - 1', 'OK'),
    ('mom60',    '动量',   '60日动量: close_today/close_60d_ago - 1', 'OK'),
    ('rev5',     '反转',   '-1 × (5日涨跌幅)',                        'OK'),
    ('rev20',    '反转',   '-1 × (20日涨跌幅)',                       'OK'),
    ('vol20',    '波动',   '20日收益波动率 × sqrt(250)',              'OK'),
    ('vol60',    '波动',   '60日收益波动率 × sqrt(250)',              'OK'),
    ('ep',       '价值',   '1 / PE_TTM',                             'OK(baostock PIT)'),
    ('bp',       '价值',   '1 / PB_MRQ',                             'OK(baostock PIT)'),
    ('turnover20','流动性','20日均换手率',                            'OK'),
    ('amount20',  '流动性','20日平均成交额',                          'OK'),
    ('volratio', '流动性', '当日量 / 5日均量',                        'OK'),
    ('main_flow', '资金',  '主力净流入(20日均值)',                    '❌ 数据源不可用'),
    ('rsi14',    '技术',   'RSI(14) 标准计算',                        'OK'),
    ('macd',     '技术',   'DIF - DEA (12,26,9)',                     'OK'),
    ('boll',     '技术',   '(close-MA20)/(2×std20)',                  'OK'),
]
FACTOR_NAMES = [s[0] for s in FACTOR_SPECS]


def _to_wide(panel: pd.DataFrame, col: str, codes: List[str],
             dates: Optional[np.ndarray] = None) -> pd.DataFrame:
    """长表 → 宽表 (trade_date × ts_code)。"""
    sub = panel[['trade_date', 'ts_code', col]].dropna(subset=[col])
    w = sub.pivot(index='trade_date', columns='ts_code', values=col)
    if dates is not None:
        w = w.reindex(dates)
    w = w[sorted(set(codes) & set(w.columns))]
    return w


def compute_factors(panel: pd.DataFrame, codes: List[str],
                    need_pe_pb: bool = True) -> Dict[str, pd.DataFrame]:
    """
    在切片面板上计算 14 个因子。panel 需含 adj_close/vol/amount/turnover_rate/pct_chg/
    pe_ttm/pb_mrq（后两者缺时 ep/bp 返回全 NaN，标注）。
    返回 {因子名: DataFrame(date×code)}。
    """
    panel = panel[panel['ts_code'].isin(codes)].sort_values(['ts_code', 'trade_date'])
    dates = np.sort(panel['trade_date'].unique())

    close_w = _to_wide(panel, 'adj_close', codes, dates)     # 复权收盘
    ret_w = close_w.pct_change()
    vol_w = _to_wide(panel, 'vol', codes, dates)
    turn_w = _to_wide(panel, 'turnover_rate', codes, dates)
    pct_w = _to_wide(panel, 'pct_chg', codes, dates)

    F = {}

    # 动量
    F['mom20'] = close_w / close_w.shift(20) - 1
    F['mom60'] = close_w / close_w.shift(60) - 1
    # 反转
    F['rev5'] = -(close_w / close_w.shift(5) - 1)
    F['rev20'] = -(close_w / close_w.shift(20) - 1)
    # 波动率（年化）
    F['vol20'] = ret_w.rolling(20).std() * np.sqrt(250)
    F['vol60'] = ret_w.rolling(60).std() * np.sqrt(250)
    # 价值（PIT PE/PB）
    if need_pe_pb and 'pe_ttm' in panel.columns:
        pe_w = _to_wide(panel, 'pe_ttm', codes, dates)
        pb_w = _to_wide(panel, 'pb_mrq', codes, dates)
        with np.errstate(divide='ignore', invalid='ignore'):
            F['ep'] = np.where(pe_w > 0, 1.0 / pe_w, np.nan)
            F['bp'] = np.where(pb_w > 0, 1.0 / pb_w, np.nan)
        F['ep'] = pd.DataFrame(F['ep'], index=pe_w.index, columns=pe_w.columns)
        F['bp'] = pd.DataFrame(F['bp'], index=pb_w.index, columns=pb_w.columns)
    else:
        F['ep'] = pd.DataFrame(np.nan, index=dates, columns=codes)
        F['bp'] = pd.DataFrame(np.nan, index=dates, columns=codes)
    # 流动性
    F['turnover20'] = turn_w.rolling(20).mean()
    amount_w = _to_wide(panel, 'amount', codes, dates)
    F['amount20'] = amount_w.rolling(20).mean()
    F['volratio'] = vol_w / vol_w.rolling(5).mean()
    # 主力净流入：数据源不可用
    F['main_flow'] = pd.DataFrame(np.nan, index=dates, columns=codes)
    # 技术
    F['rsi14'] = _rsi(close_w, 14)
    F['macd'] = _macd(close_w)
    F['boll'] = (close_w - close_w.rolling(20).mean()) / (2 * close_w.rolling(20).std())
    return F


def _rsi(close: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi


def _macd(close: pd.DataFrame) -> pd.DataFrame:
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    return dif - dea


def factor_availability(factors: Dict[str, pd.DataFrame], codes: List[str]) -> Dict[str, dict]:
    """返回每个因子在 codes 上的可用性统计（非 NaN 覆盖）。"""
    out = {}
    for name in FACTOR_NAMES:
        df = factors[name]
        avail = df.notna().mean().mean()
        out[name] = {'spec': dict(zip(FACTOR_NAMES, FACTOR_SPECS)).get(name, ('','','',''))[3],
                     'frac_non_null': float(avail)}
    return out
