#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
overfit_checker.py — 过拟合检测（任务书 3.8 / 第四部分）

  1. 样本外(E5) vs 样本内(E1-E4) 差异 > 30% → 过拟合警告
  2. 50 次回测变异系数（标准差/均值）> 0.5 → 不稳定
  3. 50 次中至少 40 次核心指标为正；每环境 10 次中至少 8 次为正
  4. 参数敏感性：关键参数 ±20% 后业绩是否崩塌（由 runner 跑变体并调用本模块判定）
"""
from __future__ import annotations
from typing import Dict, List, Optional

import numpy as np


def oos_vs_is(oos_means: Dict[str, float], is_means: Dict[str, float]) -> dict:
    """
    oos_means / is_means: {指标名: 均值}。核心指标按任务书：年化、回撤、夏普。
    返回逐指标差异与过拟合判定。
    """
    out = {}
    for key in oos_means:
        oos = oos_means.get(key)
        ins = is_means.get(key)
        if oos is None or ins is None or ins == 0:
            out[key] = {'in_sample': ins, 'out_of_sample': oos, 'diff_pct': None, 'flag': None}
            continue
        # 对回撤（负值更差），差异方向取绝对值意义上的劣化
        diff = (ins - oos) / abs(ins)
        flag = bool(diff > 0.30)
        out[key] = {'in_sample': ins, 'out_of_sample': oos,
                    'diff_pct': diff * 100, 'overfit_flag': flag}
    return out


def stability_summary(metrics_list: List[dict], key: str = 'annualized_return',
                      n_positive_min: int = 40) -> dict:
    vals = [m.get(key) for m in metrics_list if m.get(key) is not None]
    n = len(vals)
    arr = np.array(vals) if vals else np.array([])
    mu = float(arr.mean()) if n else None
    sd = float(arr.std(ddof=1)) if n > 1 else None
    cv = sd / abs(mu) if mu and mu != 0 else None
    pos = int((arr > 0).sum()) if n else 0
    return {
        'key': key, 'n': n, 'mean': mu, 'std': sd, 'cv': cv,
        'n_positive': pos, 'n_positive_required': n_positive_min,
        'positive_flag': bool(n and pos >= n_positive_min),
        'cv_flag': bool(cv is not None and cv <= 0.5),
    }


def sensitivity_verdict(base: float, perturbed: dict, threshold: float = 0.5) -> dict:
    """参数 ±20% 敏感性：|变化| / |基准| > 阈值 → 崩塌。perturbed: {param: 该变体下的核心指标均值}"""
    out = {}
    for param, val in perturbed.items():
        if base is None or base == 0 or val is None:
            out[param] = {'perturbed': val, 'rel_change': None, 'collapse': None}
            continue
        rel = (val - base) / abs(base)
        out[param] = {'perturbed': val, 'rel_change': rel * 100,
                      'collapse': bool(abs(rel) > threshold)}
    return out
