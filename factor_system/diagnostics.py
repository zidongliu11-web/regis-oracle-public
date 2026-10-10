"""M1.3 专业因子诊断（报告层，非硬门槛）+ 池化分层单调 soft gate。

按专业方法学（Harvey-Liu-Zhu / Qlib Alpha158 / A 股实务）补齐因子画像：
  - 池化分层单调：全 IS 期每日 q 组均值 → 逐组平均 → 组序 Spearman（soft，warn 不否决）。
    理由：A 股动量/反转/低波极端组非单调是常态，硬卡系统性误杀真信号。
  - IC 衰减曲线 + 半衰期（fwd 1..20）：信号持续性。
  - 信号换手率：顶分位成员跨再平衡日的换手。
  - 因子秩自相关：相隔持有期的全横截面排序稳定性。
  - 容量估算：信号日前 20 日成交额 ADV × 参与率，并披露平方根冲击压力。
  - 分年度 IC：仅检查跨年份稳定性，不冒充市场情绪分格。
除单调性外全部「只报告不否决」——样本短噪声大，点估计类 KPI 首版不做硬门槛
（见 melodic-chasing-shore.md M1.3，积累分布后再设硬下界）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config
from .metrics import cross_sectional_ic, ic_stats


def pooled_monotonicity(factor: pd.DataFrame, fwd: pd.DataFrame,
                        dates: list[pd.Timestamp], hold: int = 1,
                        q: int = 5, direction: float = 1.0) -> dict:
    """池化分层单调：全期每日 q 组均值，逐组平均后与组序 Spearman（方向对齐）。

    direction = IS IC 符号（+1 因子越高未来收益越高）。aligned = score × direction，
    aligned≥0.6 视为单调（soft 标记，不否决）。
    """
    from scipy.stats import spearmanr
    sums = np.zeros(q)
    cnt = np.zeros(q)
    n_days = 0
    for d in dates:
        if d not in factor.index or d not in fwd.index:
            continue
        f = factor.loc[d]
        r = fwd.loc[d]
        mask = f.notna() & r.notna() & np.isfinite(f) & np.isfinite(r)
        n = int(mask.sum())
        if n < max(30, q * 5):
            continue
        fr = f[mask]
        rr = r[mask]
        try:
            grp = pd.qcut(fr, q, labels=False, duplicates="drop")
        except ValueError:
            continue
        for g in range(q):
            sub = rr[grp == g]
            if len(sub):
                sums[g] += float(sub.mean())
                cnt[g] += 1
        n_days += 1
    if n_days == 0 or (cnt == 0).any():
        return {"monotonic_score": None, "monotonic_aligned": None,
                "layer_means": None, "n_days": n_days,
                "monotonic_ok": False, "monotonic_warn": True}
    layer_means = (sums / np.maximum(cnt, 1)).tolist()
    corr = spearmanr(np.arange(1, q + 1), layer_means).correlation
    if corr is None or not np.isfinite(corr):
        corr = 0.0
    aligned = float(corr) * (1.0 if direction >= 0 else -1.0)
    return {
        "monotonic_score": round(float(corr), 3),
        "monotonic_aligned": round(float(aligned), 3),
        "layer_means": [round(float(x), 5) for x in layer_means],
        "n_days": n_days,
        "monotonic_ok": bool(aligned >= 0.6),
        "monotonic_warn": bool(aligned < 0.6),
    }


def ic_decay_curve(factor: pd.DataFrame, close: pd.DataFrame,
                   dates: list[pd.Timestamp], hold_max: int = 20) -> dict:
    """各前视窗口 IC 均值曲线 + 半衰期（|IC| 衰减到峰值一半的首个窗口）。"""
    curve = []
    for h in range(1, hold_max + 1):
        fh = close.shift(-h) / close - 1.0
        ic = cross_sectional_ic(factor, fh, dates)
        curve.append({"h": h,
                      "ic_mean": round(float(ic.mean()), 5) if len(ic) else None})
    vals = [c["ic_mean"] for c in curve if c["ic_mean"] is not None]
    peak = max(abs(v) for v in vals) if vals else 0.0
    half_life = None
    if peak > 0:
        for c in curve:
            if c["ic_mean"] is not None and abs(c["ic_mean"]) <= 0.5 * peak:
                half_life = c["h"]
                break
    return {"curve": curve, "peak_ic": round(peak, 5), "half_life": half_life}


def signal_turnover(factor: pd.DataFrame, dates: list[pd.Timestamp],
                    top_frac: float = 0.2, step: int = 10,
                    direction: float = 1.0,
                    max_positions: int | None = None) -> dict:
    """信号换手率：每 step 日再平衡，实际做多分位成员的替换率。"""
    tos = []
    prev_top = None
    for i, d in enumerate(dates):
        if i % step != 0 or d not in factor.index:
            continue
        f = factor.loc[d]
        f = f[f.notna() & np.isfinite(f)]
        n = len(f)
        if n < 30:
            continue
        k = max(1, int(round(top_frac * n)))
        if max_positions is not None:
            k = min(k, max(1, int(max_positions)))
        top = set(f.sort_values(ascending=(direction < 0)).index[:k])
        if prev_top is not None:
            inter = len(top & prev_top)
            tos.append(1.0 - inter / max(1, len(prev_top)))
        prev_top = top
    return {"turnover_ratio": round(float(np.mean(tos)), 3) if tos else None,
            "n_rebalance": len(tos), "top_frac": top_frac,
            "selection_direction": ("bottom" if direction < 0 else "top"),
            "max_positions": (int(max_positions)
                              if max_positions is not None else None)}


def factor_rank_autocorrelation(
    factor: pd.DataFrame,
    dates: list[pd.Timestamp],
    lag_sessions: int = 1,
    min_overlap: int = 30,
) -> dict:
    """相隔 ``lag_sessions`` 的横截面 Spearman 秩相关，仅描述信号稳定性。"""
    lag = max(1, int(lag_sessions))
    ordered = [date for date in dates if date in factor.index]
    correlations = []
    for index in range(lag, len(ordered)):
        previous = factor.loc[ordered[index - lag]]
        current = factor.loc[ordered[index]]
        mask = (
            previous.notna() & current.notna()
            & np.isfinite(previous) & np.isfinite(current)
        )
        if int(mask.sum()) < int(min_overlap):
            continue
        correlation = previous[mask].rank().corr(current[mask].rank())
        if pd.notna(correlation):
            correlations.append(float(correlation))
    return {
        "mean_rank_autocorrelation": (
            round(float(np.mean(correlations)), 4) if correlations else None
        ),
        "median_rank_autocorrelation": (
            round(float(np.median(correlations)), 4) if correlations else None
        ),
        "n_pairs": len(correlations),
        "lag_sessions": lag,
        "min_overlap": int(min_overlap),
        "available": bool(correlations),
    }


def capacity_estimate(factor: pd.DataFrame, amount: pd.DataFrame,
                      dates: list[pd.Timestamp], top_frac: float = 0.2,
                      participation_rate: float = 0.05,
                      adv_window: int = 20,
                      impact_coefficient_bps: float = 25.0,
                      direction: float = 1.0,
                      max_positions: int | None = 10) -> dict:
    """可成交容量：仅用信号日前 ADV，限制组合单日成交参与率。

    容量是实际做多分位内前 ``max_positions`` 只股票
    ``ADV20 × participation`` 的合计中位数；平方根冲击
    只作为参数化压力披露，不冒充有分钟成交数据校准出的精确冲击成本。
    缺少完整历史时跳过该日，完全无证据则 fail-closed 返回 unavailable。
    """
    if not 0 < participation_rate <= 1 or adv_window < 2:
        raise ValueError("容量参数无效")
    adv = amount.where(np.isfinite(amount) & (amount > 0)).rolling(
        adv_window, min_periods=adv_window,
    ).mean().shift(1)
    caps = []
    selected_adv = []
    n_top = None
    n_evaluated = 0
    missing_adv_dates = 0
    ready_dates = set(adv.index[max(0, int(adv_window)):])
    for d in dates:
        if d not in factor.index or d not in adv.index or d not in ready_dates:
            continue
        f = factor.loc[d]
        mask = f.notna() & np.isfinite(f)
        n = int(mask.sum())
        if n < 30:
            continue
        n_evaluated += 1
        ff = f[mask]
        k = max(1, int(round(top_frac * n)))
        if max_positions is not None:
            k = min(k, max(1, int(max_positions)))
        idx = ff.sort_values(ascending=(direction < 0)).index[:k]
        daily_adv = adv.loc[d]
        selected = daily_adv.reindex(idx)
        valid_adv = selected.notna() & np.isfinite(selected) & (selected > 0)
        # 不允许因高排名股 ADV 缺失就暗中换成次位股；该日容量证据拒绝。
        if not bool(valid_adv.all()):
            missing_adv_dates += 1
            continue
        adv_total = float(selected.sum())
        selected_adv.append(adv_total)
        caps.append(adv_total * participation_rate)
        n_top = k                                        # 只在有效日更新
    yi = 1e8  # 元 → 亿元
    impact_bps = impact_coefficient_bps * np.sqrt(participation_rate)
    coverage = len(caps) / n_evaluated if n_evaluated else 0.0
    available = bool(caps) and coverage >= 0.80
    return {"capacity_estimate_yi": (round(float(np.median(caps)) / yi, 2)
                                      if available else None),
            "available": available,
            "reason": ("OK" if available else
                       f"ADV完整覆盖不足({len(caps)}/{n_evaluated})"),
            "method": "prior_20d_adv_participation_sqrt_impact_v1",
            "adv_window": int(adv_window),
            "adv_lag_days": 1,
            "participation_rate": float(participation_rate),
            "impact_coefficient_bps": float(impact_coefficient_bps),
            "impact_at_limit_bps": round(float(impact_bps), 3),
            "selection_direction": ("bottom" if direction < 0 else "top"),
            "max_positions": (int(max_positions)
                              if max_positions is not None else None),
            "selected_adv_yi": (round(float(np.median(selected_adv)) / yi, 2)
                                if selected_adv else None),
            "n_top": n_top,
            "n_dates": len(caps),
            "n_dates_evaluated": n_evaluated,
            "missing_adv_dates": missing_adv_dates,
            "coverage": float(coverage)}


def regime_ic(factor: pd.DataFrame, fwd: pd.DataFrame,
              dates: list[pd.Timestamp]) -> dict:
    """分年度 IC：逐年 IC 均值/IR/样本日，只检查跨年份稳定性。

    兼容既有 ``regime`` 输出键，但年份不是市场情绪状态。情绪适用性由
    情绪×战法×因子联合回放单独评估。
    """
    ic = cross_sectional_ic(factor, fwd, dates)
    rows = []
    for year, sub in ic.groupby(pd.DatetimeIndex(ic.index).year):
        st = ic_stats(sub)
        rows.append({
            "year": int(year),
            "ic_mean": round(float(st["ic_mean"]), 4),
            "ic_ir": round(float(st["ic_ir"]), 3) if pd.notna(st["ic_ir"]) else None,
            "n_dates": int(st["n_dates"]),
        })
    return {
        "scope": "calendar_year_oos_stability",
        "market_emotion_conditioned": False,
        "role": "oos_split_only_not_tactic_routing",
        "yearly": rows,
    }


def compute_diagnostics(factor: pd.DataFrame, fwd: pd.DataFrame,
                        dates: list[pd.Timestamp], panel=None,
                        hold: int = 1, direction: float = 1.0,
                        max_positions: int = 10) -> dict | None:
    """一次性打包 IS 期诊断（monotonic soft gate + 报告层 KPI）。

    需 panel（close/float_mv）才能算衰减曲线与容量；panel 缺失返回 None（不否决）。
    """
    if panel is None:
        return None
    diag = {
        "monotonic": pooled_monotonicity(factor, fwd, dates, hold=hold,
                                         q=5, direction=direction),
        "ic_decay": ic_decay_curve(factor, panel.close, dates),
        "turnover": signal_turnover(
            factor, dates, top_frac=0.2, step=hold, direction=direction,
            max_positions=max_positions,
        ),
        "rank_autocorrelation": factor_rank_autocorrelation(
            factor, dates, lag_sessions=hold,
        ),
        "capacity": capacity_estimate(
            factor, panel.amount, dates, top_frac=0.2,
            participation_rate=float(config.COSTS.get("max_adv_participation", 0.05)),
            adv_window=int(config.COSTS.get("adv_window", 20)),
            impact_coefficient_bps=float(
                config.COSTS.get("impact_coefficient_bps", 25.0)
            ),
            direction=direction,
            max_positions=max_positions,
        ),
        "regime": regime_ic(factor, fwd, dates),
    }
    return diag
