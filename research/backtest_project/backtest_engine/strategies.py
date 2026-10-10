#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
strategies.py — 策略库（任务书 Step 1 · Phase2）

统一接口：Strategy.score(F, codes) → 综合得分 DataFrame(trade_date × ts_code)，
由 runner 在 run_one 中替代 comp 生成，其余执行（PIT/成本/涨跌停/流动性）复用现有引擎。

PIT 纪律：所有策略仅用 t 及之前数据（rolling/zscore 均横截面或因果操作），无前视。
参数固定（不针对时段搜索），策略内部权重固定。

S1 等权多因子：14 个初始因子等权合成（main_flow 数据源不可用，实际用可用 13 个，如实标注）。
S2 动量：mom20/mom60 横截面 z 均分。
S3 低波动：vol20/vol60 的反向横截面 z 均分（score 高=波动低）。
S4 量价：量比/换手与中期动量均分；只有量能与价格方向同向才给高分。
S5 流动性：20 日换手与成交额横截面均分；用于避免不可交易的小流动性标的。

反转与价值保留为明确命名的附加研究策略，不占任务书 S1-S5 的编号。

组合层（members = 上述 5 个单策略）：
  combo_equal   等权组合：5 策略 score 横截面等权平均后再次标准化
  combo_volinv  波动率加权：各策略权重 ∝ 1/自身 score 尾随波动（因果，shrunk 下限）
  combo_corr    相关性权衡：尾随协方差（带对角线收缩）求逆 → 逆方差风险权重，
                惩罚高相关冗余策略（相关性权衡，经典均值-方差风险分配）
组合权重全部仅用 d 之前数据（滚动窗口），d 日决策、T+1 成交，无前视。
"""
from __future__ import annotations
import os
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .signal_generator import _zscore_wide, composite_score, ACTIVE_FACTORS

WINDOW = 20          # 组合权重尾随窗口（交易日）
SHRINK = 0.5         # 协方差收缩系数（0=不收缩, 1=完全对角）


class Strategy:
    name: str = 'base'
    description: str = ''

    def score(self, F: Dict[str, pd.DataFrame], codes: List[str]) -> pd.DataFrame:
        """返回 trade_date × ts_code 综合得分（建议已横截面标准化）。"""
        raise NotImplementedError

    def _dedupe_codes(self, codes: List[str]) -> List[str]:
        seen = set()
        out = []
        for c in codes:
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out


def _mean_zscore(names: List[str], F: Dict[str, pd.DataFrame], codes: List[str],
                 restandardize: bool = True, sign: float = 1.0) -> pd.DataFrame:
    """对指定因子做横截面 z-score 等权平均（可选再标准化）。sign=-1 用于低波动偏好。"""
    parts = []
    for n in names:
        if n not in F:
            continue
        df = F[n]
        if df.notna().sum().sum() == 0:
            continue
        parts.append(sign * _zscore_wide(df))
    if not parts:
        return pd.DataFrame()
    comp = pd.concat(parts).groupby(level=0).mean()
    if restandardize and comp.shape[0] and comp.std(axis=1).replace(0, np.nan).notna().any():
        comp = _zscore_wide(comp)
    comp = comp[[c for c in codes if c in comp.columns]]
    return comp


# ── 单策略 ──

class EqualWeightMultiFactor(Strategy):
    """S1 等权多因子：可用因子族等权合成（main_flow 数据源不可用，剔除）。"""
    name = 's1_multifactor'
    description = '等权多因子（13 个可用初始因子等权）'

    def score(self, F, codes):
        return composite_score(F, codes, restandardize=True)


class Momentum(Strategy):
    """S2 动量：20/60 日动量横截面排序。"""
    name = 's2_momentum'
    description = '动量（mom20+mom60）'

    def score(self, F, codes):
        return _mean_zscore(['mom20', 'mom60'], F, codes)


class LowVolatility(Strategy):
    """S3 低波动：低波动偏好（波动率倒数方向）。"""
    name = 's3_lowvol'
    description = '低波动（-vol20-vol60）'

    def score(self, F, codes):
        return _mean_zscore(['vol20', 'vol60'], F, codes, sign=-1.0)


class VolumePrice(Strategy):
    """S4 量价：价格动量与量能确认，固定等权而非事后调参。"""
    name = 's4_volume_price'
    description = '量价（mom20+volratio+turnover20）'

    def score(self, F, codes):
        return _mean_zscore(['mom20', 'volratio', 'turnover20'], F, codes)


class Liquidity(Strategy):
    """S5 流动性：换手和成交额共同确认，避免只看单日异常放量。"""
    name = 's5_liquidity'
    description = '流动性（turnover20+amount20）'

    def score(self, F, codes):
        return _mean_zscore(['turnover20', 'amount20'], F, codes)


class Reversal(Strategy):
    """附加研究策略：反转不属于任务书 S1-S5。"""
    name = 'research_reversal'
    description = '研究·反转（rev5+rev20）'

    def score(self, F, codes):
        return _mean_zscore(['rev5', 'rev20'], F, codes)


class Value(Strategy):
    """附加研究策略：价值不属于任务书 S1-S5。"""
    name = 'research_value'
    description = '研究·价值（EP+BP）'

    def score(self, F, codes):
        return _mean_zscore(['ep', 'bp'], F, codes)


SINGLE_STRATEGIES = [EqualWeightMultiFactor, Momentum, LowVolatility, VolumePrice, Liquidity]
RESEARCH_STRATEGIES = [Reversal, Value]


# ── 组合层 ──

class _Combination(Strategy):
    """组合基类：先算各成员 score（全切片），再按 mode 因果加权合成。"""
    name = 'combo'
    members: List[Strategy] = None  # type: ignore

    def __init__(self, members: Optional[List[Strategy]] = None):
        self.members = members or [cls() for cls in SINGLE_STRATEGIES]

    def member_comps(self, F, codes) -> Dict[str, pd.DataFrame]:
        return {m.name: m.score(F, codes) for m in self.members}

    def _weights(self, comps: Dict[str, pd.DataFrame], d: pd.Timestamp) -> Dict[str, float]:
        raise NotImplementedError

    def score(self, F, codes):
        codes = self._dedupe_codes(codes)
        comps = self.member_comps(F, codes)
        index = sorted(set().union(*[set(c.index) for c in comps.values()]))
        index = [pd.Timestamp(i) for i in index]
        rows = {}
        for d in index:
            w = self._weights(comps, d)
            vals = []
            for name, comp in comps.items():
                if name in w and d in comp.index:
                    v = comp.loc[d]
                    if v.notna().any():
                        vals.append(w[name] * v)
            if not vals:
                rows[d] = pd.Series(np.nan, index=codes)
                continue
            out = pd.concat(vals, axis=1).sum(axis=1) / max(sum(w.values()), 1e-9)
            rows[d] = out
        comp = pd.DataFrame(rows).T
        if comp.shape[0] and comp.std(axis=1).replace(0, np.nan).notna().any():
            comp = _zscore_wide(comp)
        return comp[[c for c in codes if c in comp.columns]]


class EqualCombination(_Combination):
    """等权组合：5 策略等权。"""
    name = 'combo_equal'
    description = '组合·等权'

    def _weights(self, comps, d):
        return {name: 1.0 for name in comps}


class VolInvCombination(_Combination):
    """波动率加权：权重 ∝ 1/自身 score 尾随波动（因果，[WINDOW, d-1]，min 5）。"""
    name = 'combo_volinv'
    description = '组合·波动率加权'

    def _weights(self, comps, d):
        w = {}
        for name, comp in comps.items():
            hist = comp.loc[comp.index < pd.Timestamp(d)].tail(WINDOW)
            if len(hist) < 5:
                w[name] = 1.0
                continue
            v = hist.std(axis=1).replace(0, np.nan).mean()
            w[name] = 1.0 / v if (v is not None and not np.isnan(v) and v > 0) else 1.0
        return w


class CorrRiskCombination(_Combination):
    """相关性权衡：尾随协方差（对角收缩 λ=SHRINK）求逆 → 逆方差风险权重，惩罚高相关冗余。"""
    name = 'combo_corr'
    description = '组合·相关性权衡'

    def _weights(self, comps, d):
        names = list(comps.keys())
        # 各策略尾随每日截面 std 序列（[WINDOW, d-1]）
        hist = {}
        for name, comp in comps.items():
            sub = comp.loc[comp.index < pd.Timestamp(d)].tail(WINDOW)
            stds = sub.std(axis=1).replace(0, np.nan)
            hist[name] = stds
        common = pd.DataFrame(hist).dropna()
        if len(common) < max(5, len(names)):
            return {n: 1.0 for n in names}
        cov = common.cov()                        # 策略×策略协方差
        var = np.diag(np.diag(cov.values))
        shrunk = (1 - SHRINK) * cov.values + SHRINK * var
        try:
            inv = np.linalg.pinv(shrunk)
            w = inv.sum(axis=1) / (inv.sum() + 1e-12)   # 逆方差权重（Σ⁻¹ 1 归一）
            if not np.all(np.isfinite(w)) or (w <= 0).all():
                return {n: 1.0 for n in names}
        except Exception:
            return {n: 1.0 for n in names}
        return dict(zip(names, w))


STRATEGY_REGISTRY = {cls().name: cls for cls in
                     SINGLE_STRATEGIES + RESEARCH_STRATEGIES + [EqualCombination, VolInvCombination, CorrRiskCombination]}


def get_strategy(name: Optional[str] = None) -> Strategy:
    """按 BT_STRATEGY 环境变量或 name 取策略；缺省 S1。"""
    name = name or os.environ.get('BT_STRATEGY', '') or 's1_multifactor'
    cls = STRATEGY_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f'未知策略: {name}，可选 {sorted(STRATEGY_REGISTRY)}')
    return cls()


def list_strategies() -> List[dict]:
    return [{'name': cls().name, 'description': cls().description} for cls in STRATEGY_REGISTRY.values()]
