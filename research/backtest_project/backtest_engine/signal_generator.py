#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
signal_generator.py — 等权多因子合成 + 信号生成

合成规则（任务书 3.4）：
  1. 每个因子值按日横截面 z-score 标准化
  2. 综合得分 = 各因子 z-score 的等权平均（缺省因子跳过，不充 0，不编造）
  3. 综合得分按日横截面**再标准化**（任务书同时给 ">1.5" 与 "横截面 top 10%" 两个口径，
     二者只在 composite 呈标准正态时一致；此处对 composite 每日再 z-score，
     使 ">1.5"≈"top ~7%" 与 "top 10%" 一致 —— 该选择已写入 README）
  4. 买入：composite > +1.5（≈ top 10% 档）；卖出：composite < -1.5 或止损/止盈
  5. 信号在 T 日生成，T+1 日成交（防未来函数）

主力净流入因子数据源不可用（全 NaN）→ 自动从合成中剔除。
"""
from __future__ import annotations
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

ACTIVE_FACTORS = ['mom20', 'mom60', 'rev5', 'rev20', 'vol20', 'vol60',
                  'ep', 'bp', 'turnover20', 'volratio', 'rsi14', 'macd', 'boll']
# main_flow 数据源不可用，不参与合成


def _zscore_wide(df: pd.DataFrame) -> pd.DataFrame:
    """按日（行）横截面 z-score。"""
    mu = df.mean(axis=1)
    sd = df.std(axis=1, ddof=0)
    sd = sd.replace(0, np.nan)
    out = df.sub(mu, axis=0).div(sd, axis=0)
    return out


def composite_score(factors: Dict[str, pd.DataFrame], codes: List[str],
                    restandardize: bool = True) -> pd.DataFrame:
    """
    等权多因子综合得分（DataFrame: trade_date × ts_code）。
    factors: {因子名: 宽表(date×code)}。仅用 ACTIVE_FACTORS 且覆盖可用的因子。
    """
    parts = []
    for name in ACTIVE_FACTORS:
        if name not in factors:
            continue
        df = factors[name]
        avail = df.notna().sum().sum()
        if avail == 0:
            continue          # 数据源不可用（如 main_flow）→ 剔除
        parts.append(_zscore_wide(df))
    if not parts:
        return pd.DataFrame()
    comp = pd.concat(parts).groupby(level=0).mean()   # 等权平均
    if restandardize:
        comp = _zscore_wide(comp)
    comp = comp[[c for c in codes if c in comp.columns]]
    return comp


def _row_spearman(a: pd.DataFrame, b: pd.DataFrame) -> pd.Series:
    """逐日（行）Spearman：对 rank(pct=True) 后的两宽表逐行 Pearson。"""
    ra = a.rank(axis=1, pct=True)
    rb = b.rank(axis=1, pct=True)
    common = ra.notna() & rb.notna()
    ra = ra.where(common); rb = rb.where(common)
    out = {}
    for idx in ra.index:
        x = ra.loc[idx].dropna(); y = rb.loc[idx].dropna()
        if len(x) < 30:
            out[idx] = np.nan
            continue
        xm = x - x.mean(); ym = y - y.mean()
        denom = np.sqrt((xm ** 2).sum() * (ym ** 2).sum())
        out[idx] = float(np.dot(xm, ym) / denom) if denom > 0 else np.nan
    return pd.Series(out)


def daily_ic_series(factors: Dict[str, pd.DataFrame], close: pd.DataFrame) -> Dict[str, pd.Series]:
    """
    每个因子 → 日度横截面 IC 序列（t 日 IC = Spearman(因子值_t, 次日收益_t)，次日收益=close_{t+1}/close_t-1）。
    仅用于**历史方向估计**；决策日 d 只使用 t ≤ d-1 的 IC（见 dynamic_direction_score）。
    """
    fwd = close.shift(-1) / close - 1
    out = {}
    for name, f in factors.items():
        if f.notna().sum().sum() == 0:
            continue
        out[name] = _row_spearman(f, fwd)
    return out


def dynamic_direction_score(factors: Dict[str, pd.DataFrame], codes: List[str],
                            close: pd.DataFrame, dates: List[pd.Timestamp],
                            ic_tdays: int = 60, min_obs: int = 20) -> pd.DataFrame:
    """
    walk-forward 因子方向动态化（新策略族，用户明确授权解禁的历史 IC 方向的严格 PIT 实现）：
      每决策日 d：对每个因子取 [d-ic_tdays 交易日, d-1] 的日度 IC 均值符号 → 方向 s_i=±1；
      得分 = mean_i( s_i × zscore(因子_i at d) )，再横截面标准化。
    严格因果：IC 窗口截止 d-1（该日收益 t→t+1=d 在 d 收盘已知），信号 d 收盘生成、d+1 成交。
    与"历史 IC 选因子"被否方向的关键区别：方向在**滚动窗口**内在线估计并即时使用，
    不整段样本内选因子后对同一区间回测（后者即前视泄漏，已被推翻）。
    """
    z = {name: _zscore_wide(f) for name, f in factors.items() if f.notna().sum().sum() > 0}
    ic = daily_ic_series({k: factors[k] for k in z}, close)
    rows = {}
    for d in dates:
        parts = []
        for name, fz in z.items():
            hist = ic[name][ic[name].index < d].tail(ic_tdays).dropna()
            if len(hist) < min_obs:
                continue
            s = 1.0 if hist.mean() >= 0 else -1.0
            if d in fz.index:
                parts.append(s * fz.loc[d].reindex(codes))
        if not parts:
            rows[d] = pd.Series(np.nan, index=codes)
            continue
        comp = pd.concat(parts, axis=1).mean(axis=1)
        sd = comp.std(ddof=0)
        if not np.isnan(sd) and sd > 0:
            comp = (comp - comp.mean()) / sd
        rows[d] = comp.reindex(codes)
    return pd.DataFrame(rows).T


def buy_sell(comp_day: pd.Series, buy_threshold: float = 1.5,
             sell_threshold: float = -1.5) -> tuple:
    """
    给定某日综合得分 Series(code→composite)，返回 (候选买入 codes, 候选卖出 codes)。
    触发条件：买入 comp > buy_threshold 且非 NaN；卖出 comp < sell_threshold。
    """
    buys = [c for c, v in comp_day.items()
            if v is not None and not (isinstance(v, float) and np.isnan(v)) and v > buy_threshold]
    sells = [c for c, v in comp_day.items()
             if v is not None and not (isinstance(v, float) and np.isnan(v)) and v < sell_threshold]
    return buys, sells
