"""基础算子库（任务书第三部分）。

所有算子以「日期 × 代码」DataFrame 为输入，返回同构 DataFrame。
  - ts_* ：沿时间轴（axis=0）的滚动算子
  - xs_* ：沿横截面（axis=1）的截面算子
  - 少量算术/逻辑辅助

性能：优先使用 pandas/scipy 向量化实现，避免逐格滚动 apply（8GB 单机）。
ts_rank 用 rolling().rank()，ts_quantile 用 rolling().quantile()，
ts_decay_linear 用 shift-加权求和（O(w) 次向量化遍历）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ════════════════════════════════════════════════════════════════
# 时序算子（axis=0）
# ════════════════════════════════════════════════════════════════

def ts_mean(df: pd.DataFrame, w: int) -> pd.DataFrame:
    return df.rolling(w, min_periods=max(2, w // 2)).mean()


def ts_std(df: pd.DataFrame, w: int) -> pd.DataFrame:
    return df.rolling(w, min_periods=max(2, w // 2)).std()


def ts_sum(df: pd.DataFrame, w: int) -> pd.DataFrame:
    return df.rolling(w, min_periods=max(2, w // 2)).sum()


def ts_delta(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """ts_delta(x,n) = x - x.shift(n)，n>0。"""
    return df - df.shift(n)


def ts_lag(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """严格滞后，n>0，杜绝未来函数。"""
    return df.shift(n)


def ts_max(df: pd.DataFrame, w: int) -> pd.DataFrame:
    return df.rolling(w, min_periods=max(2, w // 2)).max()


def ts_min(df: pd.DataFrame, w: int) -> pd.DataFrame:
    return df.rolling(w, min_periods=max(2, w // 2)).min()


def ts_median(df: pd.DataFrame, w: int) -> pd.DataFrame:
    return df.rolling(w, min_periods=max(2, w // 2)).median()


def ts_rank(df: pd.DataFrame, w: int) -> pd.DataFrame:
    """窗口内当前值的分位秩（0~1），rolling().rank() 向量化。"""
    return df.rolling(w, min_periods=max(2, w // 2)).rank(pct=True)


def ts_quantile(df: pd.DataFrame, w: int, q: float = 0.5) -> pd.DataFrame:
    return df.rolling(w, min_periods=max(2, w // 2)).quantile(q)


def ts_zscore(df: pd.DataFrame, w: int) -> pd.DataFrame:
    mu = ts_mean(df, w)
    sd = ts_std(df, w)
    return (df - mu) / sd.replace(0, np.nan)


def ts_skew(df: pd.DataFrame, w: int) -> pd.DataFrame:
    return df.rolling(w, min_periods=max(4, w // 2)).skew()


def ts_kurt(df: pd.DataFrame, w: int) -> pd.DataFrame:
    return df.rolling(w, min_periods=max(4, w // 2)).kurt()


def ts_ir(df: pd.DataFrame, w: int) -> pd.DataFrame:
    """信息比率式：窗口均值 / 窗口标准差。"""
    mu = ts_mean(df, w)
    sd = ts_std(df, w)
    return mu / sd.replace(0, np.nan)


def ts_decay_linear(df: pd.DataFrame, w: int) -> pd.DataFrame:
    """线性衰减：权重 1..w，归一化。O(w) 次向量化遍历。"""
    w = max(2, int(w))
    denom = w * (w + 1) / 2
    out = None
    for i in range(1, w + 1):
        term = df.shift(w - i).mul(i)
        out = term if out is None else out.add(term)
    return out / denom


def ts_decay_exp(df: pd.DataFrame, w: int) -> pd.DataFrame:
    """指数衰减（EWMA），halflife = w。"""
    alpha = 1 - 0.5 ** (1.0 / max(1, w))
    return df.ewm(alpha=alpha, min_periods=max(2, w // 2), adjust=False).mean()


def ts_corr(a: pd.DataFrame, b: pd.DataFrame, w: int) -> pd.DataFrame:
    return a.rolling(w, min_periods=max(5, w // 2)).corr(b)


def ts_cov(a: pd.DataFrame, b: pd.DataFrame, w: int) -> pd.DataFrame:
    return a.rolling(w, min_periods=max(5, w // 2)).cov(b)


def ts_product(df: pd.DataFrame, w: int) -> pd.DataFrame:
    return df.rolling(w, min_periods=max(2, w // 2)).apply(np.prod, raw=True)


def ts_argmax(df: pd.DataFrame, w: int) -> pd.DataFrame:
    """窗口内最大值出现的位置（0=最近）。较慢，模板慎用。"""
    return df.rolling(w, min_periods=max(2, w // 2)).apply(
        lambda x: float(np.argmax(x[::-1])), raw=True)


def ts_argmin(df: pd.DataFrame, w: int) -> pd.DataFrame:
    return df.rolling(w, min_periods=max(2, w // 2)).apply(
        lambda x: float(np.argmin(x[::-1])), raw=True)


def ts_regress_slope(y: pd.DataFrame, x: pd.DataFrame, w: int) -> pd.DataFrame:
    """滚动单变量回归斜率（分母固定为时间序列，可向量化）。"""
    xm = ts_mean(x, w)
    ym = ts_mean(y, w)
    cov_xy = ts_cov(x, y, w)
    var_x = ts_std(x, w) ** 2
    return cov_xy / var_x.replace(0, np.nan)


# ════════════════════════════════════════════════════════════════
# 横截面算子（axis=1）
# ════════════════════════════════════════════════════════════════

def xs_rank(df: pd.DataFrame) -> pd.DataFrame:
    return df.rank(axis=1, pct=True)


def xs_zscore(df: pd.DataFrame) -> pd.DataFrame:
    mu = df.mean(axis=1)
    sd = df.std(axis=1)
    return df.sub(mu, axis=0).div(sd.replace(0, np.nan), axis=0)


def xs_scale(df: pd.DataFrame) -> pd.DataFrame:
    return df.div(df.abs().sum(axis=1).replace(0, np.nan), axis=0)


def xs_demean(df: pd.DataFrame) -> pd.DataFrame:
    return df.sub(df.mean(axis=1), axis=0)


def xs_winsorize(df: pd.DataFrame, lo: float = 0.01, hi: float = 0.99) -> pd.DataFrame:
    lo_q = df.quantile(lo, axis=1)
    hi_q = df.quantile(hi, axis=1)
    return df.clip(lower=lo_q, upper=hi_q, axis=0)


def xs_sigmoid(df: pd.DataFrame) -> pd.DataFrame:
    return 1.0 / (1.0 + np.exp(-df))


def xs_cross_sectional_rank_of_change(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    """rank(a) - rank(b)：量价背离等使用。"""
    return xs_rank(a) - xs_rank(b)


def neutralize_industry(factor: pd.DataFrame, industry_of: pd.Series) -> pd.DataFrame:
    """行业中性化：同日同行业内去均值（简单横截面残差）。

    industry_of: index=code, value=industry 名。无行业信息的代码置 NaN。
    """
    codes = factor.columns
    ind = industry_of.reindex(codes)
    long = factor.stack().to_frame("v")
    long.index.names = ["trade_date", "ts_code"]   # 显式命名，兼容未命名索引/列
    long = long.reset_index()
    long["industry"] = long["ts_code"].map(ind)
    long = long.dropna(subset=["industry"])
    long["v_adj"] = long["v"] - long.groupby(["trade_date", "industry"])["v"].transform("mean")
    out = long.pivot(index="trade_date", columns="ts_code", values="v_adj")
    return out.reindex(index=factor.index, columns=codes)


# ════════════════════════════════════════════════════════════════
# 算术/逻辑辅助
# ════════════════════════════════════════════════════════════════

def safe_div(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    return a / b.replace(0, np.nan)


def log_plus_one(df: pd.DataFrame) -> pd.DataFrame:
    return np.log1p(df.clip(lower=0))


def sign(df: pd.DataFrame) -> pd.DataFrame:
    return np.sign(df)


def abs_(df: pd.DataFrame) -> pd.DataFrame:
    return df.abs()


def neg(df: pd.DataFrame) -> pd.DataFrame:
    return -df


def clip(df: pd.DataFrame, lo: float, hi: float) -> pd.DataFrame:
    return df.clip(lo, hi)


# 名称 → 算子
OPERATORS = {
    "ts_mean": ts_mean, "ts_std": ts_std, "ts_sum": ts_sum,
    "ts_delta": ts_delta, "ts_lag": ts_lag, "ts_max": ts_max,
    "ts_min": ts_min, "ts_median": ts_median, "ts_rank": ts_rank,
    "ts_quantile": ts_quantile, "ts_zscore": ts_zscore,
    "ts_skew": ts_skew, "ts_kurt": ts_kurt, "ts_ir": ts_ir,
    "ts_decay_linear": ts_decay_linear, "ts_decay_exp": ts_decay_exp,
    "ts_corr": ts_corr, "ts_cov": ts_cov, "ts_product": ts_product,
    "ts_argmax": ts_argmax, "ts_argmin": ts_argmin,
    "ts_regress_slope": ts_regress_slope,
    "xs_rank": xs_rank, "xs_zscore": xs_zscore, "xs_scale": xs_scale,
    "xs_demean": xs_demean, "xs_winsorize": xs_winsorize, "xs_sigmoid": xs_sigmoid,
}
