"""因子度量（IC / IC_IR / 分层利差 / t 统计 / p 值）。

IC = 每交易日横截面 Spearman 秩相关（因子值 vs 未来 H 日收益），
然后对全部交易日取均值与标准差得到 IC_IR。
实现只依赖 pandas 向量化 + 小循环，符合 8GB 单机约束。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _cross_sectional_ic_precise(factor, fwd, dates, min_cs):
    """Rare integer batches keep original scalar dtype before paired ranking."""
    values, index = [], []
    for day in dates:
        f, r = factor.loc[day], fwd.loc[day]
        valid = f.notna() & r.notna() & np.isfinite(f) & np.isfinite(r)
        if int(valid.sum()) < min_cs:
            continue
        value = f[valid].rank().corr(r[valid].rank())
        if pd.notna(value):
            values.append(float(value))
            index.append(day)
    return pd.Series(values, index=index, dtype=float)


def cross_sectional_ic(factor: pd.DataFrame, fwd: pd.DataFrame,
                       dates: list[pd.Timestamp], min_cs: int = 20) -> pd.Series:
    """共同有限样本上的日截面 Spearman，按请求顺序每批最多 64 日。

    保留平均并列秩、最小截面、缺因子日期跳过、缺标签日期报错和重复请求。
    输入轴必须唯一；不用整张全 A 面板的秩/掩码副本换取速度。
    """
    if not (factor.index.is_unique and fwd.index.is_unique
            and factor.columns.is_unique and fwd.columns.is_unique):
        raise ValueError("IC input indices and columns must be unique")
    selected = [day for day in dates if day in factor.index]
    columns = factor.columns.intersection(fwd.columns, sort=False)
    values, index = [], []
    factor_integers = any(pd.api.types.is_integer_dtype(t) for t in factor.dtypes)
    return_integers = any(pd.api.types.is_integer_dtype(t) for t in fwd.dtypes)
    for start in range(0, len(selected), 64):
        requested = selected[start:start + 64]
        # loc deliberately preserves the original error for missing label dates.
        f = factor.loc[requested, columns].to_numpy(dtype=float, na_value=np.nan)
        r = fwd.loc[requested, columns].to_numpy(dtype=float, na_value=np.nan)
        # Float64 cannot preserve all integer ordering above 2**53. Use the
        # original dtype on these rare batches rather than manufacturing ties.
        if ((factor_integers and np.any(np.abs(f) >= 2**53))
                or (return_integers and np.any(np.abs(r) >= 2**53))):
            precise = _cross_sectional_ic_precise(factor, fwd, requested, min_cs)
            values.extend(precise.to_list())
            index.extend(precise.index.to_list())
            continue
        valid = np.isfinite(f) & np.isfinite(r)
        counts = valid.sum(axis=1)
        eligible = counts >= max(2, min_cs)
        if not eligible.any():
            continue
        valid = valid[eligible]
        counts = counts[eligible]
        # Ranking both legs on the same observations is essential under missingness.
        fr = pd.DataFrame(np.where(valid, f[eligible], np.nan)).rank(
            axis=1, method="average", na_option="keep").to_numpy(dtype=float)
        rr = pd.DataFrame(np.where(valid, r[eligible], np.nan)).rank(
            axis=1, method="average", na_option="keep").to_numpy(dtype=float)
        rank_mean = (counts[:, None] + 1.0) / 2.0
        fr = np.where(valid, fr - rank_mean, 0.0)
        rr = np.where(valid, rr - rank_mean, 0.0)
        numerator = np.einsum("ij,ij->i", fr, rr)
        denominator = np.sqrt(np.einsum("ij,ij->i", fr, fr)
                              * np.einsum("ij,ij->i", rr, rr))
        nonconstant = denominator > 0.0
        correlations = np.divide(numerator, denominator,
                                 out=np.full(len(counts), np.nan), where=nonconstant)
        correlations = np.clip(correlations, -1.0, 1.0)
        for day, value in zip(np.asarray(requested)[eligible], correlations):
            if np.isfinite(value):
                values.append(float(value))
                index.append(day)
    return pd.Series(values, index=index, dtype=float)


def ic_stats(ic: pd.Series, hold: int = 1) -> dict:
    """IC 描述统计 + 与前向持有期一致的保守显著性检验。

    H 日前向收益逐日滚动时，相邻 H 个 IC 共享大部分收益窗口，不能把每日 IC
    当独立样本做 ``sqrt(n)`` t 检验。显著性改为每隔 H 日抽取一个不重叠相位，
    并在所有起始相位中取最大的双侧 p 值。各相位均值必须与整体均值严格同向；
    正负相位分别显著不能证明共同方向，方向冲突或零均值时整体 p=1。
    原始相位检验与描述统计仍完整保留；任一相位证据不足都会 fail-closed。
    描述性的 IC mean/std/IR 仍使用全部日期。
    """
    x = pd.Series(ic).dropna().astype(float)
    n = int(len(x))
    if n < 2:
        return {"n_dates": n, "ic_mean": np.nan, "ic_ir": np.nan,
                "ic_std": np.nan, "t_stat": np.nan, "p_value": 1.0,
                "n_effective": n, "p_value_method": "insufficient",
                "p_value_direction_consistent": False,
                "p_value_direction_reason": "insufficient_observations"}
    mu = float(x.mean())
    # A constant decimal-valued float can acquire a rounding-only residual
    # after subtracting its computed mean. Its mathematical variance is zero.
    # Use exact equality, not an epsilon that erases genuinely small variation.
    sd = 0.0 if x.min() == x.max() else float(x.std(ddof=1))
    horizon = max(1, int(hold or 1))
    from scipy.stats import t as student_t
    phase_tests = []
    for offset in range(min(horizon, n)):
        sample = x.iloc[offset::horizon]
        n_eff = int(len(sample))
        phase_mean = float(sample.mean())
        if n_eff < 10:
            t_value, p_value, effective_n, hac_lags = 0.0, 1.0, n_eff, 0
        elif sample.min() == sample.max():
            t_value, p_value, effective_n, hac_lags = 0.0, 1.0, 1, 0
        else:
            residual = sample.to_numpy(dtype=float) - phase_mean
            gamma0 = float(np.dot(residual, residual) / (n_eff - 1))
            hac_lags = min(
                n_eff - 2,
                max(1, int(np.floor(4.0 * (n_eff / 100.0) ** (2.0 / 9.0)))),
            )
            long_run = gamma0
            for lag in range(1, hac_lags + 1):
                weight = 1.0 - lag / (hac_lags + 1.0)
                covariance = float(
                    np.dot(residual[lag:], residual[:-lag]) / n_eff
                )
                long_run += 2.0 * weight * covariance
            # 负样本自相关不应让“严格”检验比 IID 更激进；HAC 长期方差至少取
            # 当期方差，下界同时避免小样本协方差噪声制造额外显著性。
            long_run = max(long_run, gamma0)
            if (not np.isfinite(long_run) or long_run <= 0 or
                    not np.isfinite(gamma0) or gamma0 <= 0):
                t_value, p_value, effective_n = 0.0, 1.0, 1
            else:
                standard_error = float(np.sqrt(long_run / n_eff))
                t_value = float(phase_mean / standard_error)
                effective_n = int(max(1, min(n_eff, round(n_eff * gamma0 / long_run))))
                p_value = (float(2.0 * student_t.sf(abs(t_value), df=effective_n - 1))
                           if effective_n >= 2 else 1.0)
        phase_tests.append(
            (p_value, t_value, effective_n, offset, hac_lags, n_eff, phase_mean)
        )
    p, t, n_effective, phase, hac_lags, phase_n, _ = max(
        phase_tests, key=lambda row: row[0]
    )
    phase_means = np.array([row[6] for row in phase_tests], dtype=float)
    direction_consistent = bool(
        np.isfinite(mu) and np.isfinite(phase_means).all()
        and ((mu > 0 and (phase_means > 0).all())
             or (mu < 0 and (phase_means < 0).all()))
    )
    if direction_consistent:
        direction_reason = "consistent_phase_direction"
    elif (phase_means > 0).any() and (phase_means < 0).any():
        direction_reason = "conflicting_phase_directions"
    else:
        direction_reason = "zero_or_unverified_mean_direction"
    # The two-sided phase p values retain their original meaning. Aggregation
    # requires a common signed alternative; never select or discard a phase.
    phase_max_p = p
    if not direction_consistent:
        p = 1.0
    phase_sensitivity = [{
        "phase": int(offset),
        "p_value": float(p_value),
        "t_stat": float(t_value),
        "n_effective": int(effective_n),
        "n_observations": int(sample_n),
        "hac_lags": int(lags),
        "mean": float(phase_mean),
    } for p_value, t_value, effective_n, offset, lags, sample_n, phase_mean in phase_tests]
    return {"n_dates": n, "ic_mean": mu, "ic_std": sd, "ic_ir": mu / sd if sd > 0 else np.nan,
            "t_stat": t, "p_value": p, "n_effective": n_effective,
            "p_value_method": ("newey_west_hac" if horizon == 1
                               else "non_overlapping_phase_max_hac"),
            "p_value_hold": horizon, "p_value_phase": phase,
            "p_value_hac_lags": hac_lags, "p_value_phase_n": phase_n,
            "p_value_direction_consistent": direction_consistent,
            "p_value_direction_reason": direction_reason,
            "p_value_phase_max_before_direction_check": phase_max_p,
            "p_value_phase_sensitivity": phase_sensitivity}


def quantile_spread_annualized(factor: pd.DataFrame, fwd: pd.DataFrame,
                               dates: list[pd.Timestamp], hold: int,
                               q: int = 5) -> dict:
    """按因子值分 q 组，多空(顶组−底组)平均未来收益，年化。"""
    top_ret, bot_ret, top_n, bot_n = [], [], 0, 0
    for d in dates:
        if d not in factor.index:
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
        top = rr[grp == grp.max()]
        bot = rr[grp == grp.min()]
        if len(top) == 0 or len(bot) == 0:
            continue
        top_ret.append(float(top.mean()))
        bot_ret.append(float(bot.mean()))
        top_n += 1
        bot_n += 1
    if not top_ret:
        return {"long_short_annualized": np.nan, "top_q_mean": np.nan, "bottom_q_mean": np.nan,
                "monotonic": np.nan, "n_days": 0}
    top = float(np.mean(top_ret))
    bot = float(np.mean(bot_ret))
    ls = (top - bot) * (252.0 / max(1, hold))
    return {"long_short_annualized": ls, "top_q_mean": top, "bottom_q_mean": bot,
            "monotonic": bool(top > bot), "n_days": len(top_ret)}
