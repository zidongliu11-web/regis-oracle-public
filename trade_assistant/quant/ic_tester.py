"""因子 IC 测试模块.

对因子库中所有因子计算截面 Rank IC (Spearman)，支持：
- 多前瞻期 IC 测试 (5d/10d/20d)
- 自适应最小样本数（根据股票数量调整）
- 综合 IC 评分（加权平均）
- 因子分级 (T1-T4)
- IC 衰减检测
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from scipy import stats

from .logger import get_logger

logger = get_logger()


@dataclass
class ICResult:
    """单个因子的 IC 测试结果."""
    factor: str
    ic_by_horizon: Dict[int, Dict] = field(default_factory=dict)
    composite_ic: float = 0.0
    best_horizon: Optional[int] = None
    best_ic: float = 0.0
    tier: str = "t4_weak"
    n_obs: int = 0


def compute_rank_ic(
    factor: pd.DataFrame,
    fwd_ret: pd.DataFrame,
    min_samples: int = 10,
    min_dates: int = 30,
) -> dict:
    """计算截面 Rank IC (Spearman).

    Args:
        factor: 因子值 DataFrame (date × stock)，index 为日期，columns 为股票代码
        fwd_ret: 前瞻收益 DataFrame (date × stock)
        min_samples: 每天至少需要的股票数（自适应）
        min_dates: 至少需要的日期数

    Returns:
        {"mean_ic": float|None, "icir": float|None, "n": int, "ics": list}
    """
    common_dates = factor.index.intersection(fwd_ret.index)
    if len(common_dates) < min_dates:
        return {"mean_ic": None, "icir": None, "n": 0, "ics": []}

    ics = []
    for d in common_dates:
        f = factor.loc[d].dropna()
        r = fwd_ret.loc[d].dropna()
        common = f.index.intersection(r.index)
        if len(common) < min_samples:
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=RuntimeWarning)
                ic, _ = stats.spearmanr(f[common], r[common])
            if not np.isnan(ic):
                ics.append(float(ic))
        except Exception:
            continue

    if len(ics) < min_dates:
        return {"mean_ic": None, "icir": None, "n": len(ics), "ics": ics}

    mean_ic = float(np.mean(ics))
    std_ic = float(np.std(ics, ddof=1))
    icir = mean_ic / std_ic if std_ic > 0 else 0.0

    return {
        "mean_ic": round(mean_ic, 6),
        "icir": round(float(icir), 4),
        "ic_pos_ratio": round(sum(1 for x in ics if x > 0) / len(ics), 4),
        "ic_std": round(std_ic, 6),
        "n": len(ics),
        "ics": [round(x, 6) for x in ics],
    }


def compute_composite_ic(ic_by_horizon: Dict[int, Dict]) -> float:
    """计算综合 IC（加权平均，偏重长周期）.

    Args:
        ic_by_horizon: {horizon: ic_result_dict}

    Returns:
        加权平均 |IC|
    """
    weights = {5: 0.3, 10: 0.3, 20: 0.4}
    weighted = 0.0
    for h, w in weights.items():
        info = ic_by_horizon.get(h, {})
        mean_ic = info.get("mean_ic")
        if mean_ic is not None:
            weighted += abs(mean_ic) * w
    return round(weighted, 6)


def classify_tier(composite_ic: float) -> str:
    """根据综合 IC 分级.

    A股因子分级标准（基于学术研究 + 实证）:
    - T1: |IC| > 0.03  — 优秀
    - T2: |IC| > 0.015 — 良好
    - T3: |IC| > 0.005 — 一般
    - T4: |IC| ≤ 0.005 — 弱
    """
    if composite_ic > 0.03:
        return "t1_excellent"
    elif composite_ic > 0.015:
        return "t2_good"
    elif composite_ic > 0.005:
        return "t3_marginal"
    return "t4_weak"


def run_ic_tests(
    factors: Dict[str, pd.DataFrame],
    fwd_rets: Dict[int, pd.DataFrame],
    n_stocks: Optional[int] = None,
) -> List[ICResult]:
    """对因子库中所有因子跑 IC 测试.

    Args:
        factors: {factor_name: DataFrame(date×stock)}
        fwd_rets: {horizon_days: DataFrame(date×stock)}
        n_stocks: 股票总数，用于自适应最小样本数

    Returns:
        按 composite_ic 降序排列的 ICResult 列表
    """
    if n_stocks is None:
        # 从第一个因子推断
        first = next(iter(factors.values()), None)
        n_stocks = len(first.columns) if first is not None else 80

    # 自适应参数
    min_samples = max(5, min(n_stocks, 10))
    min_dates = max(20, min(60, n_stocks * 3))

    results = []
    total = len(factors)

    for i, (name, df) in enumerate(factors.items()):
        ic_by_h = {}
        best_abs = 0.0
        best_h = None

        for h, fwd in fwd_rets.items():
            r = compute_rank_ic(df, fwd, min_samples=min_samples, min_dates=min_dates)
            ic_by_h[h] = r
            if r["mean_ic"] is not None and abs(r["mean_ic"]) > best_abs:
                best_abs = abs(r["mean_ic"])
                best_h = h

        composite_ic = compute_composite_ic(ic_by_h)
        tier = classify_tier(composite_ic)

        results.append(ICResult(
            factor=name,
            ic_by_horizon=ic_by_h,
            composite_ic=composite_ic,
            best_horizon=best_h,
            best_ic=round(best_abs, 6),
            tier=tier,
            n_obs=sum(r.get("n", 0) for r in ic_by_h.values()),
        ))

        if (i + 1) % 20 == 0:
            logger.info(f"IC测试: {i+1}/{total} 因子完成")

    results.sort(key=lambda x: x.composite_ic, reverse=True)
    return results


def detect_ic_decay(
    factor_name: str,
    ic_history: List[float],
    window: int = 60,
    decay_threshold: float = 0.3,
) -> Dict:
    """检测因子 IC 衰减.

    Args:
        factor_name: 因子名称
        ic_history: IC 时间序列
        window: 滚动窗口大小
        decay_threshold: 衰减阈值（IC 下降比例）

    Returns:
        {"decaying": bool, "current_ic": float, "rolling_avg": float,
         "trend": float, "severity": str}
    """
    if len(ic_history) < window:
        return {"decaying": False, "current_ic": 0, "rolling_avg": 0,
                "trend": 0, "severity": "insufficient_data"}

    ic_series = pd.Series(ic_history)
    rolling_avg = float(ic_series.rolling(window).mean().iloc[-1])
    overall_avg = float(ic_series.mean())
    current_ic = float(ic_series.iloc[-1])

    # 滚动均值 vs 总体均值
    drop_ratio = (overall_avg - rolling_avg) / abs(overall_avg) if overall_avg != 0 else 0

    severity = "none"
    if drop_ratio > 0.5:
        severity = "severe"
    elif drop_ratio > decay_threshold:
        severity = "moderate"
    elif drop_ratio > 0.1:
        severity = "mild"

    return {
        "decaying": drop_ratio > decay_threshold,
        "current_ic": round(current_ic, 6),
        "rolling_avg": round(rolling_avg, 6),
        "overall_avg": round(overall_avg, 6),
        "drop_ratio": round(drop_ratio, 4),
        "severity": severity,
    }
