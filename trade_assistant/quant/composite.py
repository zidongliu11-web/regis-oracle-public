"""因子复合构建模块.

将多个因子按权重合成复合因子，支持:
- 截面 Z-score 标准化
- IC 加权合成
- 等权合成
- 跨组分散选因子
"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .logger import get_logger

logger = get_logger()


def zscore_cross_section(df: pd.DataFrame) -> pd.DataFrame:
    """截面 Z-score 标准化.

    Args:
        df: (date × stock) DataFrame

    Returns:
        标准化后的 DataFrame
    """
    mu = df.mean(axis=1)
    sigma = df.std(axis=1).replace(0, 1.0)
    return df.sub(mu, axis=0).div(sigma, axis=0)


def build_composite(
    factors: Dict[str, pd.DataFrame],
    selected: List[str],
    weights: Optional[Dict[str, float]] = None,
) -> Optional[pd.DataFrame]:
    """构建复合因子.

    对每个选中因子做截面 Z-score 标准化后按权重合成。

    Args:
        factors: {factor_name: DataFrame(date×stock)}
        selected: 选中因子名称列表
        weights: {factor_name: weight}，None 则等权

    Returns:
        复合因子 DataFrame (date×stock)，或 None
    """
    if not selected:
        return None

    if weights is None:
        weights = {s: 1.0 / len(selected) for s in selected}

    # Z-score 并收集
    z_scored = {}
    for name in selected:
        if name not in factors:
            logger.warning(f"因子 {name} 不在因子库中，跳过")
            continue
        df = factors[name].copy()
        z_scored[name] = zscore_cross_section(df)

    if not z_scored:
        return None

    # 合成
    first = next(iter(z_scored.values()))
    composite = pd.DataFrame(0.0, index=first.index, columns=first.columns)

    for name, df_z in z_scored.items():
        w = weights.get(name, 1.0 / len(z_scored))
        composite += df_z * w

    return composite


def select_factors_cross_group(
    ic_results: list,
    max_per_group: int = 2,
    max_total: int = 8,
    min_tier: str = "t2_good",
) -> List[str]:
    """跨组分散选因子.

    从 IC 排名中选取因子，每组最多 max_per_group 个，总数不超过 max_total。

    Args:
        ic_results: ICResult 对象列表（已按 composite_ic 排好序）
        max_per_group: 每组最多选几个
        max_total: 总共最多选几个
        min_tier: 最低接受的分级

    Returns:
        选中因子名称列表
    """
    tier_rank = {"t1_excellent": 0, "t2_good": 1, "t3_marginal": 2, "t4_weak": 3}
    min_tier_rank = tier_rank.get(min_tier, 2)

    # 推断每个因子的分组
    group_counts: Dict[str, int] = {}
    selected: List[str] = []

    for r in ic_results:
        if tier_rank.get(r.tier, 3) > min_tier_rank:
            continue
        if r.composite_ic <= 0:
            continue

        g = _infer_group(r.factor)
        if group_counts.get(g, 0) >= max_per_group:
            continue

        selected.append(r.factor)
        group_counts[g] = group_counts.get(g, 0) + 1

        if len(selected) >= max_total:
            break

    # 如果选不够，从头部补
    if len(selected) < 3:
        for r in ic_results:
            if r.factor not in selected and r.composite_ic > 0:
                selected.append(r.factor)
            if len(selected) >= 5:
                break

    return selected


def select_factors_top_n(
    ic_results: list,
    n: int = 5,
    min_composite_ic: float = 0.005,
) -> List[str]:
    """纯 IC 排名 Top-N 选因子.

    Args:
        ic_results: ICResult 对象列表（已按 composite_ic 排好序）
        n: 选几个
        min_composite_ic: 最低综合 IC 阈值

    Returns:
        选中因子名称列表
    """
    selected = []
    for r in ic_results[:n]:
        if r.composite_ic > min_composite_ic:
            selected.append(r.factor)
    return selected if selected else [r.factor for r in ic_results[:n]]


def compute_ic_weights(
    ic_results: list,
    selected: List[str],
) -> Dict[str, float]:
    """基于 IC 计算因子权重.

    Args:
        ic_results: ICResult 对象列表
        selected: 选中因子名称列表

    Returns:
        {factor_name: weight}
    """
    # 构建 name -> composite_ic 映射
    ic_map = {r.factor: r.composite_ic for r in ic_results}

    total = sum(abs(ic_map.get(s, 0.01)) for s in selected) or 1.0
    return {s: round(abs(ic_map.get(s, 0.01)) / total, 4) for s in selected}


_FH_GROUPS = frozenset({
    "low_vol", "reversal", "momentum", "value", "liquidity", "quality",
    "size", "technical", "money_flow", "regime", "growth", "other",
})


def _infer_group(name: str) -> str:
    """根据因子名称推断所属分组."""
    name_lower = name.lower()
    # FinHack 生成因子名 fh_<group>_<idx>：直接读组 token（保证跨族分散生效）
    if name_lower.startswith("fh_"):
        parts = name_lower.split("_")
        if len(parts) >= 3 and parts[1] in _FH_GROUPS:
            return parts[1]
    # 更具体的子串先判（避免 dollar_volume 含 "vol" 误入低波组）
    if any(x in name_lower for x in ['turnover', 'amihud', 'illiq', 'dollar', 'amt_',
                                     'vol_price', 'vol_dry', 'volume_price']):
        return 'liquidity'
    if any(x in name_lower for x in ['vwap', 'obv', 'mfi']):
        return 'money_flow'
    if any(x in name_lower for x in ['_vol_', 'idio', 'amplitude', 'ffc', 'downside', 'var_']):
        return 'low_vol'
    if any(x in name_lower for x in ['reversal', 'rsi', 'stoch', 'overnight', 'intraday']):
        return 'reversal'
    if any(x in name_lower for x in ['mom_', 'ma_xover', 'macd', 'breakout', 'veloc', 'hl_trend', 'new_high']):
        return 'momentum'
    if any(x in name_lower for x in ['np_growth', 'rev_growth', 'ded_np', 'yoy', 'profit_growth', 'rev_growth']):
        return 'growth'
    if any(x in name_lower for x in ['bp_', 'ep_', 'ey_', 'bp_real', 'cash_yield', 'book_to', 'pct_52', 'pct_200', 'dist_ma', 'boll_', 'price_to']):
        return 'value'
    if any(x in name_lower for x in ['roe_', 'gm_level', 'nm_level', 'margin', 'low_debt', 'debt_', 'cash_yield', 'quality']):
        return 'quality'
    if any(x in name_lower for x in ['skew', 'kurt', 'up_down', 'pos_day', 'stability', 'streak']):
        return 'quality'
    if any(x in name_lower for x in ['size', 'midcap']):
        return 'size'
    if any(x in name_lower for x in ['atr', 'tr_', 'support', 'hammer', 'gap_up', 'gap_fill']):
        return 'technical'
    if any(x in name_lower for x in ['regime', 'rel_strength']):
        return 'regime'
    return 'other'
