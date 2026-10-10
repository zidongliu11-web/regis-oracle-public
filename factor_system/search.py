"""自动组合与搜索引擎（任务书第五部分）。

三种搜索方法：
  1. 模板参数扫描   —— 模板库 × 参数网格展开（方法一）
  2. 跨族模板组合   —— 2-3 个模板的 rank 组合（方法一扩展，含经济逻辑）
  3. 遗传编程       —— 轻量 GP：表达式树 + 交叉变异 + IS 适应度（方法二）

候选统一为 Candidate 规格，compute_factor() 计算其因子面板。
遗传用固定种子，结果可复现。
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field

import numpy as np

import pandas as pd

from . import config
from . import operators as opx
from .data import Panel, mask_signal_eligible
from .metrics import cross_sectional_ic, ic_stats, quantile_spread_annualized
from .preprocess import demean_industry, industry_exposures
from .templates import TEMPLATES, iter_param_candidates
from .validate import l0_hard_filter


@dataclass
class Candidate:
    factor_id: str
    name: str
    family: str
    formula: str
    description: str
    direction: str
    data_requires: tuple[str, ...]
    kind: str  # template | combo | genetic
    compute: callable
    params: dict = field(default_factory=dict)
    research_record: dict = field(default_factory=dict)
    research_record_sha256: str = ""


def _by_id(tpl_id: str):
    return next(t for t in TEMPLATES if t.tpl_id == tpl_id)


def _rank(df: pd.DataFrame) -> pd.DataFrame:
    return df.rank(axis=1, pct=True)


# ════════════════════════════════════════════════════════════════
# 方法一：模板参数扫描
# ════════════════════════════════════════════════════════════════

def generate_template_candidates() -> list[Candidate]:
    out = []
    for spec in iter_param_candidates():
        t = spec["template"]
        params = dict(spec["params"])
        params["_tpl_id"] = t.tpl_id   # 配方字段：回测区按 tpl_id 重建
        p = dict(params)
        out.append(Candidate(
            factor_id=spec["factor_id"],
            name=f"{t.name}", family=t.family,
            formula=t.formula, description=t.description,
            direction=t.direction, data_requires=t.data_requires,
            kind="template", compute=lambda pan, q=t, pp=p: q.compute(pan, pp),
            params=params,
        ))
    return out


# ════════════════════════════════════════════════════════════════
# 方法二：跨族组合（带经济逻辑的配对）
# ════════════════════════════════════════════════════════════════

def _combo_mom_lowvol(p: Panel):
    a = _by_id("A1").compute(p, {"n": 20})
    c = _by_id("C1").compute(p, {"n": 20})
    return _rank(a) - _rank(c)


def _combo_value_momentum(p: Panel):
    if p.pe_ttm is None:
        return None
    d = _by_id("D1").compute(p, {})
    a = _by_id("A3").compute(p, {"skip": 5, "n": 60})
    return _rank(d) + _rank(a)


def _combo_reversal_lowturn(p: Panel):
    b = _by_id("B1").compute(p, {"n": 5})
    f = _by_id("F1").compute(p, {"n": 5})
    return _rank(b) - _rank(f)   # 反转 + 低换手


def _combo_divergence_momentum(p: Panel):
    g = _by_id("G4").compute(p, {"n": 5})
    a = _by_id("A1").compute(p, {"n": 20})
    return _rank(g) + _rank(a)


def _combo_value_lowvol(p: Panel):
    if p.pe_ttm is None:
        return None
    d = _by_id("D1").compute(p, {})
    c = _by_id("C1").compute(p, {"n": 60})
    return _rank(d) - _rank(c)


_COMBOS = [
    ("combo_mom_lowvol", "动量+低波", "A_momentum×C_volatility",
     "rank(mom_20) - rank(vol_20)", "动量叠加低波动：趋势向上且波动温和的组合更稳。",
     "higher_is_better", _combo_mom_lowvol, ("close",)),
    ("combo_value_momentum", "价值+动量", "D_value×A_momentum",
     "rank(EP) + rank(mom_60_skip5)", "低估且中期趋势向上：价值与动量共振。",
     "higher_is_better", _combo_value_momentum, ("pe_ttm", "close")),
    ("combo_reversal_lowturn", "反转+低换手", "B_reversal×F_liquidity",
     "rank(-mom_5) - rank(turnover_5)", "短期超跌且交投冷清：反转与低关注度共振。",
     "higher_is_better", _combo_reversal_lowturn, ("close", "turnover")),
    ("combo_divergence_momentum", "背离修正动量", "G_pvf×A_momentum",
     "rank(量价背离_5) + rank(mom_20)", "量价背离修正趋势：放量上攻确认动量。",
     "higher_is_better", _combo_divergence_momentum, ("close", "vol")),
    ("combo_value_lowvol", "价值+低波", "D_value×C_volatility",
     "rank(EP) - rank(vol_60)", "低估且低波：经典低风险价值组合。",
     "higher_is_better", _combo_value_lowvol, ("pe_ttm", "close")),
]


def generate_combo_candidates() -> list[Candidate]:
    out = []
    for fid, name, family, formula, desc, direction, fn, req in _COMBOS:
        out.append(Candidate(
            factor_id=fid, name=name, family=family, formula=formula,
            description=desc, direction=direction, data_requires=tuple(req),
            kind="combo", compute=fn,
        ))
    return out


# ════════════════════════════════════════════════════════════════
# 已冻结来源候选：PriceDelayRsq
# ════════════════════════════════════════════════════════════════

_PRICE_DELAY_ID = "fresh_pricedelayrsq_v3"
_PRICE_DELAY_PARAMS = {
    "lags": 4,
    "minimum_observations": 26,
    "risk_free_daily": 0.0,
    "window_start_month_day": "07-01",
    "window_end_month_day": "06-30",
    "availability_after_month_day": "07-31",
}
_PRICE_DELAY_FORMULA = (
    "1-r2(stock_excess~market_excess)/"
    "r2(stock_excess~market_excess+market_excess_lag1..4)"
)

_LOW_TURNOVER_ID = "fresh_low_turnover_20_v1"
_LOW_TURNOVER_PARAMS = {
    "window": 20,
    "minimum_complete_observations": 20,
}
_LOW_TURNOVER_FORMULA = "-mean(turnover[d-19:d])"

_FREEFLOAT_ABTURN_ID = "fresh_freefloat_abturn_daily_20_250_v2"
_FREEFLOAT_ABTURN_PARAMS = {
    "short_window": 20,
    "long_window": 250,
    "size_exclusion_fraction": 0.30,
    "size_rank_method": "average",
    "size_keep_rule": "strictly_greater_than",
}
_FREEFLOAT_ABTURN_EXECUTION_SPEC = {"hold_days": 20}
_FREEFLOAT_ABTURN_VALIDATION_SPEC = {
    "expected_direction": -1.0,
    "apply_neutralization": False,
}
_FREEFLOAT_ABTURN_FORMULA = (
    "mean(turnover[T-19:T])/mean(turnover[T-249:T])"
)

_MAXRET_ID = "fresh_maxret_daily_20_60_v1"
_MAXRET_PARAMS = {
    "lookback": 20,
    "size_exclusion_fraction": 0.30,
    "size_rank_method": "average",
    "size_keep_rule": "strictly_greater_than",
}
_MAXRET_EXECUTION_SPEC = {"hold_days": 60}
_MAXRET_VALIDATION_SPEC = {
    "expected_direction": -1.0,
    "apply_neutralization": False,
}
_MAXRET_FORMULA = "max(ret[T-19:T])"

_SHORT_REVERSAL_ID = "fresh_short_reversal_daily_5_5_v1"
_SHORT_REVERSAL_PARAMS = {
    "lookback": 5,
    "size_exclusion_fraction": 0.30,
    "size_rank_method": "average",
    "size_keep_rule": "strictly_greater_than",
}
_SHORT_REVERSAL_EXECUTION_SPEC = {"hold_days": 5}
_SHORT_REVERSAL_VALIDATION_SPEC = {
    "expected_direction": -1.0,
    "apply_neutralization": False,
}
_SHORT_REVERSAL_FORMULA = "prod(1+ret[T-4:T])-1"


def _low_turnover_20(panel: Panel, params: dict | None = None) -> pd.DataFrame:
    """完整 20 个有效交易日的低换手信号；不缩窗、不填停牌。"""
    spec = {**_LOW_TURNOVER_PARAMS, **(params or {})}
    if spec != _LOW_TURNOVER_PARAMS:
        raise ValueError("[low_turnover_recipe_drift] 冻结参数漂移")
    turnover = pd.DataFrame(panel.turnover, dtype=float)
    is_st = getattr(panel, "is_st_panel", None)
    tradable = getattr(panel, "tradable", None)
    if not isinstance(is_st, pd.DataFrame) or not isinstance(
        tradable, pd.DataFrame
    ):
        return pd.DataFrame(np.nan, index=turnover.index, columns=turnover.columns)
    is_st = is_st.reindex_like(turnover).fillna(True).astype(bool)
    tradable = tradable.reindex_like(turnover).fillna(False).astype(bool)
    observed = (
        turnover.notna()
        & np.isfinite(turnover)
        & turnover.gt(0)
        & tradable
        & ~is_st
    )
    window = int(spec["window"])
    complete = observed.astype(np.int8).rolling(
        window, min_periods=window,
    ).sum().eq(window)
    mean_turnover = turnover.where(observed).rolling(
        window, min_periods=window,
    ).mean()
    return -mean_turnover.where(complete)


def _freefloat_abturn_daily_20_250(
    panel: Panel, params: dict | None = None,
) -> pd.DataFrame:
    """冻结日频 20/250 原始比率；历史窗口只由当时换手率决定。"""
    if params is not None and params != _FREEFLOAT_ABTURN_PARAMS:
        raise ValueError("[freefloat_abturn_recipe_drift] 冻结参数漂移")
    spec = _FREEFLOAT_ABTURN_PARAMS
    raw_turnover = getattr(panel, "turnover", None)
    if not isinstance(raw_turnover, pd.DataFrame):
        raise ValueError("[freefloat_abturn_input_missing] turnover 必须为 DataFrame")
    if (
        raw_turnover.index.has_duplicates
        or not raw_turnover.index.is_monotonic_increasing
        or raw_turnover.columns.has_duplicates
    ):
        raise ValueError("[freefloat_abturn_axis_invalid] 日期或代码轴无序/重复")

    turnover = raw_turnover.astype(float)
    output = pd.DataFrame(
        np.nan, index=turnover.index, columns=turnover.columns, dtype=float,
    )
    aligned: dict[str, pd.DataFrame] = {}
    for field_name in (
        "float_mv", "signal_eligible", "tradable", "is_st_panel",
    ):
        frame = getattr(panel, field_name, None)
        if not isinstance(frame, pd.DataFrame):
            return output
        if (
            frame.index.has_duplicates
            or not frame.index.is_monotonic_increasing
            or frame.columns.has_duplicates
        ):
            raise ValueError(
                f"[freefloat_abturn_axis_invalid] {field_name} 轴无序/重复"
            )
        aligned[field_name] = frame.reindex(
            index=turnover.index, columns=turnover.columns,
        )

    observed = turnover.notna() & np.isfinite(turnover) & turnover.ge(0)
    long_window = int(spec["long_window"])
    short_window = int(spec["short_window"])
    complete = observed.astype(np.int16).rolling(
        long_window, min_periods=long_window,
    ).sum().eq(long_window)
    usable_turnover = turnover.where(observed)
    short_mean = usable_turnover.rolling(
        short_window, min_periods=short_window,
    ).mean()
    long_mean = usable_turnover.rolling(
        long_window, min_periods=long_window,
    ).mean()
    ratio = short_mean.div(long_mean.where(long_mean.gt(0)))
    ratio = ratio.where(complete & np.isfinite(ratio))

    signal_eligible = aligned["signal_eligible"].fillna(False).astype(bool)
    tradable = aligned["tradable"].fillna(False).astype(bool)
    st_state = aligned["is_st_panel"].astype("boolean")
    current_eligible = (
        signal_eligible
        & tradable
        & st_state.notna()
        & st_state.eq(False).fillna(False).astype(bool)
    )
    lagged_float_mv = aligned["float_mv"].astype(float).shift(1)
    valid_size = (
        lagged_float_mv.notna()
        & np.isfinite(lagged_float_mv)
        & lagged_float_mv.gt(0)
    )
    pre_size = ratio.notna() & current_eligible & valid_size
    size_percentile = lagged_float_mv.where(pre_size).rank(
        axis=1, method=str(spec["size_rank_method"]), pct=True,
    )
    keep = size_percentile.gt(float(spec["size_exclusion_fraction"]))
    return ratio.where(pre_size & keep)


def _maxret_daily_20_60(
    panel: Panel, params: dict | None = None,
) -> pd.DataFrame:
    """冻结日频 simple MAX；只发布完整可信窗口与精确 T-1 市值截面。"""
    if params is not None and params != _MAXRET_PARAMS:
        raise ValueError("[maxret_recipe_drift] 冻结参数漂移")
    raw_return = getattr(panel, "ret", None)
    if not isinstance(raw_return, pd.DataFrame):
        raise ValueError("[maxret_input_missing] ret 必须为 DataFrame")
    if (
        raw_return.index.has_duplicates
        or not raw_return.index.is_monotonic_increasing
        or raw_return.columns.has_duplicates
    ):
        raise ValueError("[maxret_axis_invalid] 日期或代码轴无序/重复")

    returns = raw_return.astype(float)
    output = pd.DataFrame(
        np.nan, index=returns.index, columns=returns.columns, dtype=float,
    )
    aligned: dict[str, pd.DataFrame] = {}
    for field_name in (
        "float_mv", "signal_eligible", "tradable", "is_st_panel",
    ):
        frame = getattr(panel, field_name, None)
        if not isinstance(frame, pd.DataFrame):
            return output
        if (
            frame.index.has_duplicates
            or not frame.index.is_monotonic_increasing
            or frame.columns.has_duplicates
        ):
            raise ValueError(f"[maxret_axis_invalid] {field_name} 轴无序/重复")
        aligned[field_name] = frame.reindex(
            index=returns.index, columns=returns.columns,
        )

    observed = returns.notna() & np.isfinite(returns)
    lookback = int(_MAXRET_PARAMS["lookback"])
    complete = observed.astype(np.int8).rolling(
        lookback, min_periods=lookback,
    ).sum().eq(lookback)
    raw_max = returns.where(observed).rolling(
        lookback, min_periods=lookback,
    ).max().where(complete)

    signal_eligible = aligned["signal_eligible"].fillna(False).astype(bool)
    tradable = aligned["tradable"].fillna(False).astype(bool)
    st_state = aligned["is_st_panel"].astype("boolean")
    current_eligible = (
        signal_eligible
        & tradable
        & st_state.notna()
        & st_state.eq(False).fillna(False).astype(bool)
    )
    lagged_float_mv = aligned["float_mv"].astype(float).shift(1)
    valid_size = (
        lagged_float_mv.notna()
        & np.isfinite(lagged_float_mv)
        & lagged_float_mv.gt(0)
    )
    pre_size = raw_max.notna() & current_eligible & valid_size
    size_percentile = lagged_float_mv.where(pre_size).rank(
        axis=1, method=str(_MAXRET_PARAMS["size_rank_method"]), pct=True,
    )
    keep = size_percentile.gt(float(_MAXRET_PARAMS["size_exclusion_fraction"]))
    return raw_max.where(pre_size & keep)


def _short_reversal_daily_5_5(
    panel: Panel, params: dict | None = None,
) -> pd.DataFrame:
    """冻结五日复合收益；只发布完整可信窗口与精确 T-1 市值截面。"""
    if params is not None and params != _SHORT_REVERSAL_PARAMS:
        raise ValueError("[short_reversal_recipe_drift] 冻结参数漂移")
    raw_return = getattr(panel, "ret", None)
    if not isinstance(raw_return, pd.DataFrame):
        raise ValueError("[short_reversal_input_missing] ret 必须为 DataFrame")
    if (
        raw_return.index.has_duplicates
        or not raw_return.index.is_monotonic_increasing
        or raw_return.columns.has_duplicates
    ):
        raise ValueError("[short_reversal_axis_invalid] 日期或代码轴无序/重复")

    returns = raw_return.astype(float)
    output = pd.DataFrame(
        np.nan, index=returns.index, columns=returns.columns, dtype=float,
    )
    aligned: dict[str, pd.DataFrame] = {}
    for field_name in (
        "float_mv", "signal_eligible", "tradable", "is_st_panel",
    ):
        frame = getattr(panel, field_name, None)
        if not isinstance(frame, pd.DataFrame):
            return output
        if (
            frame.index.has_duplicates
            or not frame.index.is_monotonic_increasing
            or frame.columns.has_duplicates
        ):
            raise ValueError(
                f"[short_reversal_axis_invalid] {field_name} 轴无序/重复"
            )
        aligned[field_name] = frame.reindex(
            index=returns.index, columns=returns.columns,
        )

    observed = returns.notna() & np.isfinite(returns)
    lookback = int(_SHORT_REVERSAL_PARAMS["lookback"])
    complete = observed.astype(np.int8).rolling(
        lookback, min_periods=lookback,
    ).sum().eq(lookback)
    compounded = (
        returns.where(observed).add(1.0).rolling(
            lookback, min_periods=lookback,
        ).apply(np.prod, raw=True).sub(1.0).where(complete)
    )

    signal_eligible = aligned["signal_eligible"].fillna(False).astype(bool)
    tradable = aligned["tradable"].fillna(False).astype(bool)
    st_state = aligned["is_st_panel"].astype("boolean")
    current_eligible = (
        signal_eligible
        & tradable
        & st_state.notna()
        & st_state.eq(False).fillna(False).astype(bool)
    )
    lagged_float_mv = aligned["float_mv"].astype(float).shift(1)
    valid_size = (
        lagged_float_mv.notna()
        & np.isfinite(lagged_float_mv)
        & lagged_float_mv.gt(0)
    )
    pre_size = compounded.notna() & current_eligible & valid_size
    size_percentile = lagged_float_mv.where(pre_size).rank(
        axis=1, method=str(_SHORT_REVERSAL_PARAMS["size_rank_method"]), pct=True,
    )
    keep = size_percentile.gt(
        float(_SHORT_REVERSAL_PARAMS["size_exclusion_fraction"])
    )
    return compounded.where(pre_size & keep)


def _lagged_float_market_return(panel: Panel) -> pd.Series:
    """PIT 宽基：昨日可见流通市值加权今日可观察复权收盘收益。"""
    close = pd.DataFrame(panel.close, dtype=float)
    float_mv = pd.DataFrame(panel.float_mv, dtype=float).reindex_like(close)
    observed_return = getattr(panel, "ret", None)
    if not isinstance(observed_return, pd.DataFrame):
        return pd.Series(np.nan, index=close.index, dtype=float)
    returns = observed_return.reindex_like(close).astype(float)
    weights = float_mv.shift(1)
    weights = weights.where(np.isfinite(weights) & weights.gt(0))
    valid = returns.notna() & np.isfinite(returns) & weights.notna()
    denominator = weights.where(valid).sum(axis=1, min_count=1)
    numerator = returns.mul(weights).where(valid).sum(axis=1, min_count=1)
    return numerator.div(denominator.where(denominator.gt(0)))


def _ols_r_squared(y: np.ndarray, x: np.ndarray) -> float:
    design = np.column_stack([np.ones(len(y), dtype=float), x])
    coefficients, _, rank, _ = np.linalg.lstsq(design, y, rcond=None)
    if rank != design.shape[1]:
        return float("nan")
    centered = y - y.mean()
    total = float(centered @ centered)
    if not np.isfinite(total) or total <= np.finfo(float).eps:
        return float("nan")
    residual = y - design @ coefficients
    return float(1.0 - (residual @ residual) / total)


def _price_delay_statistic(stock_excess: pd.Series,
                           market_excess: pd.Series, *,
                           lags: int, minimum_observations: int,
                           required_last_month: int | None = None) -> float:
    regressors = pd.concat(
        [market_excess.shift(lag) for lag in range(lags + 1)], axis=1,
    )
    joined = pd.concat([stock_excess.rename("stock"), regressors], axis=1)
    joined = joined.replace([np.inf, -np.inf], np.nan).dropna()
    if len(joined) < minimum_observations:
        return float("nan")
    if required_last_month is not None:
        if (not isinstance(joined.index, pd.DatetimeIndex)
                or int(joined.index.max().month) != int(required_last_month)):
            return float("nan")
    y = joined.iloc[:, 0].to_numpy(dtype=float)
    x = joined.iloc[:, 1:].to_numpy(dtype=float)
    restricted = _ols_r_squared(y, x[:, :1])
    unrestricted = _ols_r_squared(y, x)
    if (not np.isfinite(restricted) or not np.isfinite(unrestricted)
            or unrestricted <= np.finfo(float).eps):
        return float("nan")
    value = 1.0 - restricted / unrestricted
    if -1e-10 <= value <= 1.0 + 1e-10:
        value = min(1.0, max(0.0, value))
    return float(value) if 0.0 <= value <= 1.0 else float("nan")


def _price_delay_rsq(panel: Panel, params: dict | None = None) -> pd.DataFrame:
    """冻结口径：每年六月估计，完整滞后一月后才在下一交易日可用。"""
    if params is not None:
        frozen_dates = (
            "window_start_month_day",
            "window_end_month_day",
            "availability_after_month_day",
        )
        drift = [
            key for key in frozen_dates
            if params.get(key) != _PRICE_DELAY_PARAMS[key]
        ]
        if drift:
            raise ValueError(
                "[price_delay_recipe_drift] 冻结日期参数漂移: "
                + ",".join(drift)
            )
    spec = {**_PRICE_DELAY_PARAMS, **(params or {})}
    dates = pd.DatetimeIndex(panel.close.index)
    columns = pd.Index(panel.close.columns)
    output = pd.DataFrame(np.nan, index=dates, columns=columns, dtype=float)
    if dates.empty or dates.has_duplicates or not dates.is_monotonic_increasing:
        return output

    observed_return = getattr(panel, "ret", None)
    if not isinstance(observed_return, pd.DataFrame):
        return output
    # 形成期使用当时已公开的收益历史，不让 MIN_BARS/当日准入抹掉历史。
    # 最终可发布截面仍由 compute_factor/rebuild_factor 统一套 signal_eligible。
    stock_return = observed_return.reindex(index=dates, columns=columns).astype(float)
    risk_free = float(spec["risk_free_daily"])
    market_excess = _lagged_float_market_return(panel).sub(risk_free)
    stock_excess = stock_return.sub(risk_free)
    lags = int(spec["lags"])
    minimum = int(spec["minimum_observations"])

    for year in range(int(dates.min().year), int(dates.max().year) + 1):
        window_start = pd.Timestamp(year=year - 1, month=7, day=1)
        window_end = pd.Timestamp(year=year, month=6, day=30)
        # 全局输入本身必须覆盖完整的一年；个股仍按冻结的 26 行最低要求。
        if dates.min() > window_start:
            continue
        window_dates = dates[(dates >= window_start) & (dates <= window_end)]
        if len(window_dates) < minimum:
            continue
        available_after = pd.Timestamp(year=year, month=7, day=31)
        available = dates[dates > available_after]
        if available.empty:
            continue
        values = {
            code: _price_delay_statistic(
                stock_excess.loc[window_dates, code],
                market_excess.loc[window_dates],
                lags=lags,
                minimum_observations=minimum,
                required_last_month=6,
            )
            for code in columns
        }
        output.loc[output.index >= available[0], :] = (
            pd.Series(values).reindex(columns).to_numpy()
        )
    return output


def generate_source_candidates() -> list[Candidate]:
    return [Candidate(
        factor_id=_PRICE_DELAY_ID,
        name="Fresh PriceDelayRsq",
        family="L_market_friction",
        formula=_PRICE_DELAY_FORMULA,
        description="市场信息进入个股价格的延迟；高值表示更多解释力来自市场滞后收益。",
        direction="higher_is_better",
        data_requires=("close", "ret", "float_mv", "signal_eligible"),
        kind="source",
        compute=lambda panel: _price_delay_rsq(panel, _PRICE_DELAY_PARAMS),
        params=dict(_PRICE_DELAY_PARAMS),
    ), Candidate(
        factor_id=_LOW_TURNOVER_ID,
        name="Fresh Low Turnover 20",
        family="F_liquidity",
        formula=_LOW_TURNOVER_FORMULA,
        description="完整 20 个有效交易日的平均换手率取负；高值表示低换手。",
        direction="higher_is_better",
        data_requires=(
            "turnover", "tradable", "is_st_panel", "signal_eligible",
        ),
        kind="source",
        compute=lambda panel: _low_turnover_20(panel, _LOW_TURNOVER_PARAMS),
        params=dict(_LOW_TURNOVER_PARAMS),
    ), Candidate(
        factor_id=_FREEFLOAT_ABTURN_ID,
        name="Fresh Daily Free-Float Abnormal Turnover 20/250 v2",
        family="F_liquidity",
        formula=_FREEFLOAT_ABTURN_FORMULA,
        description=(
            "日频自由流通换手率 20/250 原始比率；仅发布通过当日准入和"
            "精确 T-1 流通市值筛选的截面，低值更优。"
        ),
        direction="lower_is_better",
        data_requires=(
            "turnover", "float_mv", "signal_eligible", "tradable",
            "is_st_panel",
        ),
        kind="source",
        compute=lambda panel: _freefloat_abturn_daily_20_250(
            panel, _FREEFLOAT_ABTURN_PARAMS,
        ),
        params=dict(_FREEFLOAT_ABTURN_PARAMS),
    ), Candidate(
        factor_id=_MAXRET_ID,
        name="Fresh Daily MAXRET 20/60 v1",
        family="A_extreme_return",
        formula=_MAXRET_FORMULA,
        description=(
            "最新 20 个完整可信交易日的最大复权收益；仅发布通过当日准入和"
            "精确 T-1 流通市值筛选的截面，低值更优。"
        ),
        direction="lower_is_better",
        data_requires=(
            "ret", "float_mv", "signal_eligible", "tradable",
            "is_st_panel",
        ),
        kind="source",
        compute=lambda panel: _maxret_daily_20_60(panel, _MAXRET_PARAMS),
        params=dict(_MAXRET_PARAMS),
    ), Candidate(
        factor_id=_SHORT_REVERSAL_ID,
        name="Fresh Daily Short Reversal 5/5 v1",
        family="A_short_reversal",
        formula=_SHORT_REVERSAL_FORMULA,
        description=(
            "最新 5 个完整可信交易日的复合复权收益；仅发布通过当日准入和"
            "精确 T-1 流通市值筛选的截面，低值更优。"
        ),
        direction="lower_is_better",
        data_requires=(
            "ret", "float_mv", "signal_eligible", "tradable",
            "is_st_panel",
        ),
        kind="source",
        compute=lambda panel: _short_reversal_daily_5_5(
            panel, _SHORT_REVERSAL_PARAMS,
        ),
        params=dict(_SHORT_REVERSAL_PARAMS),
    )]


# ════════════════════════════════════════════════════════════════
# 方法三：遗传编程（轻量 GP）
# ════════════════════════════════════════════════════════════════

_ATOMS: list[tuple[str, callable]] = [
    ("mom20", lambda p: _by_id("A1").compute(p, {"n": 20})),
    ("mom60", lambda p: _by_id("A1").compute(p, {"n": 60})),
    ("rev5", lambda p: _by_id("B1").compute(p, {"n": 5})),
    ("vol20", lambda p: _by_id("C1").compute(p, {"n": 20})),
    ("volr60_5", lambda p: _by_id("C3").compute(p, {})),
    ("turn20", lambda p: _by_id("F1").compute(p, {"n": 20})),
    ("amihud20", lambda p: _by_id("F2").compute(p, {"n": 20})),
    ("rsi14", lambda p: _by_id("H1").compute(p, {"n": 14})),
    ("macd12", lambda p: _by_id("H2").compute(p, {"fast": 12, "slow": 26, "signal": 9})),
    ("kdjj9", lambda p: _by_id("H5").compute(p, {"n": 9})),
    ("ep", lambda p: _by_id("D1").compute(p, {}) if p.pe_ttm is not None else None),
    ("bp", lambda p: _by_id("D2").compute(p, {}) if p.pb_mrq is not None else None),
]


# ════════════════════════════════════════════════════════════════
# M1.2 GP 精选算子子集
# ════════════════════════════════════════════════════════════════

# 算子窗口（固定代表值；参数化变体走模板族，深度保持 3 防算力爆炸）
GP_W = 10


def _gp_rank(f):
    return f.rank(axis=1, pct=True) if f is not None else None


def _gp_add(a, b):
    return _gp_rank(a) + _gp_rank(b)


def _gp_sub(a, b):
    return _gp_rank(a) - _gp_rank(b)


def _gp_mul(a, b):
    return _gp_rank(a) * _gp_rank(b)


def _gp_div(a, b):
    return _gp_rank(a).div(_gp_rank(b).add(1e-9))


def _gp_corr(a, b):
    return opx.ts_corr(a, b, GP_W)


# (显示名, 元数, fn(args, ctx)) —— 0/1/2 与旧编码一致，旧配方兼容
_GP_OPS = {
    0: ("+", 2, lambda a, ctx: _gp_add(a[0], a[1])),
    1: ("-", 2, lambda a, ctx: _gp_sub(a[0], a[1])),
    2: ("*", 2, lambda a, ctx: _gp_mul(a[0], a[1])),
    3: ("/", 2, lambda a, ctx: _gp_div(a[0], a[1])),
    4: (f"ts_corr{GP_W}", 2, lambda a, ctx: _gp_corr(a[0], a[1])),
    5: (f"ts_delta{GP_W}", 1, lambda a, ctx: opx.ts_delta(a[0], GP_W)),
    6: (f"ts_mean{GP_W}", 1, lambda a, ctx: opx.ts_mean(a[0], GP_W)),
    7: (f"ts_std{GP_W}", 1, lambda a, ctx: opx.ts_std(a[0], GP_W)),
    8: (f"ts_decay{GP_W}", 1, lambda a, ctx: opx.ts_decay_exp(a[0], GP_W)),
    9: ("xs_rank", 1, lambda a, ctx: opx.xs_rank(a[0])),
    10: ("xs_zscore", 1, lambda a, ctx: opx.xs_zscore(a[0])),
    11: ("ind_neut", 1, lambda a, ctx: demean_industry(a[0], ctx) if ctx else None),
}
_GP_BINARY = [k for k, v in _GP_OPS.items() if v[1] == 2]
_GP_UNARY = [k for k, v in _GP_OPS.items() if v[1] == 1]


def _industry_ctx(panel: Panel) -> dict | None:
    """GP/回测重建用的轻量行业暴露上下文（行业数据不可用 → None，ind_neut 算子诚实失败）。"""
    try:
        return industry_exposures(panel)
    except Exception:
        return None


def _tree_str(nodes: list) -> str:
    """arity 感知的人类可读公式（旧 0/1/2 二元算子继续可读）。"""
    stack = []
    for x in nodes:
        if isinstance(x, int):
            name, arity, _ = _GP_OPS[x]
            args = [stack.pop() for _ in range(arity)][::-1]
            if arity == 2:
                stack.append(f"({args[0]}{name}{args[1]})")
            else:
                stack.append(f"{name}({args[0]})")
        else:
            stack.append(x)
    return stack[0] if stack else ""


def _canonical_tree(nodes: list) -> list | None:
    """把 GP 后缀树规范化，等价表达式只保留一个稳定表示。

    仅做确定等价的化简：交换律算子固定左右顺序、重复的一元幂等算子折叠，
    以及 ``x-x`` / ``x/x`` / ``corr(x,x)`` 这类退化树直接丢弃。历史配方仍按
    原节点重建；规范化只影响新探索。
    """
    stack: list[tuple] = []
    for token in nodes:
        if isinstance(token, int):
            spec = _GP_OPS.get(token)
            if spec is None:
                return None
            arity = spec[1]
            if len(stack) < arity:
                return None
            args = tuple(stack[-arity:])
            del stack[-arity:]
            if arity == 1 and token in (9, 10, 11):
                child = args[0]
                if child[0] == "op" and child[1] == token:
                    stack.append(child)
                    continue
            if arity == 2:
                left, right = args
                if left == right and token in (1, 3, 4):
                    return None
                if token in (0, 2):
                    # +/* 同时满足交换律和结合律：先摊平成项集，再排序并按固定左结合重建。
                    # 因此 (a+b)+c、a+(c+b)、c+(b+a) 都只有一个规范表示。
                    terms: list[tuple] = []

                    def collect(node: tuple) -> None:
                        if node[0] == "op" and node[1] == token:
                            for child in node[2]:
                                collect(child)
                        else:
                            terms.append(node)

                    collect(left)
                    collect(right)
                    terms.sort(key=repr)
                    combined = terms[0]
                    for term in terms[1:]:
                        combined = ("op", token, (combined, term))
                    stack.append(combined)
                    continue
            stack.append(("op", token, args))
        else:
            stack.append(("atom", str(token)))
    if len(stack) != 1:
        return None

    out: list = []

    def emit(node: tuple) -> None:
        if node[0] == "atom":
            out.append(node[1])
            return
        for child in node[2]:
            emit(child)
        out.append(node[1])

    emit(stack[0])
    return out


def _tree_key(nodes: list) -> str | None:
    canonical = _canonical_tree(nodes)
    if canonical is None:
        return None
    return json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))


def _tree_eval(nodes: list, atom_frames: dict, ctx: dict | None = None) -> pd.DataFrame | None:
    """arity 感知的栈求值。旧配方（0/1/2 二元）继续有效；ctx 供行业中性化算子用。"""
    stack = []
    for x in nodes:
        if isinstance(x, int):
            name, arity, fn = _GP_OPS[x]
            if len(stack) < arity:
                return None
            args = [stack.pop() for _ in range(arity)][::-1]
            if any(a is None for a in args):
                stack.append(None)
                continue
            try:
                stack.append(fn(args, ctx))
            except Exception:
                stack.append(None)
        else:
            stack.append(atom_frames.get(x))
    return stack[0] if stack else None


def _random_tree(rng: random.Random, atoms: list[str], max_internal: int = 3) -> list:
    """生成 arity 平衡的**后缀**表达式列表（操作数在前、算子在后，与 _tree_eval 栈求值匹配）。

    旧配方（如 ["mom20","rev5",0]）同为后缀序，天然兼容。递归生成，总内部节点数
    受 max_internal 上限约束 → 叶子数 ≤ 1+max_internal，深度有界、必然终止。
    """
    internal = {"n": 0}

    def build() -> list:
        if internal["n"] >= max_internal or rng.random() < 0.5:
            return [rng.choice(atoms)]
        internal["n"] += 1
        if rng.random() < 0.35 and _GP_UNARY:
            op = rng.choice(_GP_UNARY)
            return build() + [op]               # 一元：operand 后缀 + op
        op = rng.choice(_GP_BINARY)
        return build() + build() + [op]         # 二元：left + right + op

    return build()


def _crossover(a: list, b: list, rng: random.Random, atom_ids: list[str]) -> list:
    """单点交叉（保留两树前缀段）+ 叶子位点变异。无效后代由 _tree_eval 过滤。"""
    if len(a) > 2 and len(b) > 2 and rng.random() < 0.6:
        p1 = rng.randint(1, len(a) - 1)
        p2 = rng.randint(1, len(b) - 1)
        child = a[:p1] + b[p2:]
    else:
        child = list(a)
    if rng.random() < 0.4:
        leaf_pos = [i for i, x in enumerate(child) if isinstance(x, str)]
        if leaf_pos:
            child[rng.choice(leaf_pos)] = rng.choice(atom_ids)
    return child


def _fitness(nodes: list, atom_frames: dict, is_dates: list, fwd: pd.DataFrame,
             ctx: dict | None = None, hold: int = config.HOLD_DAYS,
             signal_eligible: pd.DataFrame | None = None) -> tuple[float, pd.DataFrame | None]:
    fr = _tree_eval(nodes, atom_frames, ctx)
    if fr is None:
        return -1e9, None
    # 搜索期先执行与 L0 同口径的廉价覆盖/恒定检查，避免把大量明显必败候选
    # 送进后续中性化、诊断和 OOS 验证。严格 L0-L3 仍由 pipeline 再跑一遍。
    l0 = l0_hard_filter(
        fr, is_dates, signal_eligible=signal_eligible, require_min_days=False,
    )
    if not l0["passed"]:
        return -1e9, fr
    ic = cross_sectional_ic(fr, fwd, is_dates)
    s = ic_stats(ic)
    score = abs(s["ic_ir"]) if pd.notna(s["ic_ir"]) else -1e9
    # 2026-08-17 组合层对齐（anti-重复）：加「方向感知多头腿 IS 毛利年化」奖励。
    # 背景：仅优化 |IC_IR| 会让 GP 持续产出秩相关强但所选腿不赚钱的因子——这类因子
    # 在科学闸门 net_annual>0 / beats_shanghai 处必死，是「重复产被拒因子」的根源。
    # 方向感知：ic_mean<0（反转族）的多头腿是 raw 因子底组（bottom_q_mean）。
    # 只奖励正毛利（max 截断）：A股长多不能靠做空腿赚，负毛利腿不给任何奖励。
    # 成本：只子采样 is_dates[::4] 做分位数毛利（~+12% fitness 耗时），不算全日期。
    try:
        sub = list(is_dates)[::4]
        qs = quantile_spread_annualized(fr, fwd, sub, hold=hold, q=config.QUANTILE)
        if qs["n_days"] >= 30 and np.isfinite(qs["top_q_mean"]) and np.isfinite(qs["bottom_q_mean"]):
            ann = 252.0 / max(1, hold)
            long_leg = (qs["top_q_mean"] if s["ic_mean"] >= 0 else qs["bottom_q_mean"]) * ann
            score += 0.5 * max(0.0, float(long_leg))
    except Exception:
        pass
    # 复杂度惩罚：叶子越多分越低
    leaves = sum(1 for x in nodes if isinstance(x, str))
    score -= 0.05 * max(0, leaves - 2)
    return score, fr


def generate_genetic_candidates(panel: Panel, is_dates: list, fwd: pd.DataFrame,
                                budget: int = 80,
                                ctx: dict | None = None,
                                seed: int | None = None,
                                exclude_formulas: set[str] | None = None,
                                search_stats: dict | None = None,
                                atom_frames: dict | None = None) -> list[Candidate]:
    """轻量 GP：固定种子，种群→适应度→交叉→适应度，输出 top budget 个。

    M1.2：精选算子子集（+/-/*//、ts_corr/ts_delta/ts_mean/ts_std/ts_decay、xs_rank/
    xs_zscore、行业中性化）；禁用 ts_argmax/ts_argmin/ts_product/ts_regress_slope
    （逐格 apply 慢）。算子窗口固定 GP_W=10，参数化变体走模板族。ctx 供行业中性化
    算子使用（由 batch 层一次构建传入；缺省时轻量自建）。

    seed：M2.3 24h 常驻探索用时间种子（默认 config.GEN_SEED 保证历史批次可复现）。
    """
    rng = random.Random(seed if seed is not None else config.GEN_SEED)
    atom_ids = [a for a, _ in _ATOMS]
    atom_frames = atom_frames if atom_frames is not None else build_atom_frames(panel)
    valid_atoms = [a for a in atom_ids if atom_frames.get(a) is not None]
    if len(valid_atoms) < 2:
        return []
    if ctx is None:
        ctx = _industry_ctx(panel)

    excluded = exclude_formulas or set()
    stats = search_stats if search_stats is not None else {}
    stats.update({
        "archive_skipped": 0,
        "canonical_duplicates": 0,
        "degenerate_skipped": 0,
        "fitness_evaluated": 0,
        "fitness_cache_hits": 0,
    })
    fitness_cache: dict[str, float] = {}

    def canonical_candidate(nodes: list) -> tuple[list | None, str | None]:
        canonical = _canonical_tree(nodes)
        if canonical is None:
            stats["degenerate_skipped"] += 1
            return None, None
        formula = _tree_str(canonical)
        if formula in excluded:
            stats["archive_skipped"] += 1
            return None, None
        return canonical, formula

    def score_tree(nodes: list) -> float:
        key = _tree_key(nodes)
        if key in fitness_cache:
            stats["fitness_cache_hits"] += 1
            return fitness_cache[key]
        score, frame = _fitness(
            nodes, atom_frames, is_dates, fwd, ctx,
            signal_eligible=getattr(panel, "signal_eligible", None),
        )
        stats["fitness_evaluated"] += 1
        fitness_cache[key] = score
        del frame
        return score

    pop = []
    pop_keys: set[str] = set()
    attempts = 0
    while len(pop) < config.GEN_POPULATION and attempts < config.GEN_POPULATION * 12:
        attempts += 1
        raw = _random_tree(rng, valid_atoms, max_internal=config.GEN_DEPTH)
        nodes, _formula = canonical_candidate(raw)
        if nodes is None:
            continue
        key = _tree_key(nodes)
        if key in pop_keys:
            stats["canonical_duplicates"] += 1
            continue
        pop_keys.add(key)
        pop.append(nodes)

    scored = []
    for nodes in pop:
        scored.append((score_tree(nodes), nodes))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        stats["output_candidates"] = 0
        stats["unique_trees_evaluated"] = 0
        return []

    # 交叉生成下一代
    next_gen = list(scored[: max(8, config.GEN_POPULATION // 2)])
    for _ in range(config.GEN_GENERATIONS - 1):
        offspring = []
        pool = [s[1] for s in scored[: max(6, len(scored) // 2)]]
        offspring_keys: set[str] = set()
        for _ in range(config.GEN_POPULATION // 2):
            a = rng.choice(pool)
            b = rng.choice(pool)
            raw = _crossover(a, b, rng, valid_atoms)
            child, _formula = canonical_candidate(raw)
            if child is None:
                continue
            key = _tree_key(child)
            if key in offspring_keys:
                stats["canonical_duplicates"] += 1
                continue
            offspring_keys.add(key)
            offspring.append(child)
        new_scored = []
        for nodes in offspring:
            new_scored.append((score_tree(nodes), nodes))
        new_scored.sort(key=lambda x: -x[0])
        merged = sorted(scored + new_scored, key=lambda x: -x[0])
        unique = []
        unique_keys: set[str] = set()
        for row in merged:
            key = _tree_key(row[1])
            if key in unique_keys:
                stats["canonical_duplicates"] += 1
                continue
            unique_keys.add(key)
            unique.append(row)
        scored = unique[: config.GEN_POPULATION]
        next_gen = scored

    out = []
    seen = set()
    for score, nodes in next_gen:
        if score <= 0.01:
            continue
        tree_s = _tree_str(nodes)
        # ID 只由规范化表达式决定；时间种子只负责探索，不再制造同公式多 ID。
        # 96-bit SHA-256 prefix keeps the public ID compact while avoiding the
        # 40-bit collision space of the former truncated MD5 identifier.
        digest = hashlib.sha256(tree_s.encode()).hexdigest()[:24]
        fid = f"gen_{digest}"
        if fid in seen:
            continue
        seen.add(fid)
        out.append(Candidate(
            factor_id=fid, name="遗传表达式", family="genetic",
            formula=tree_s, description="遗传编程挖掘（M1.2 精选算子：动量/波动/量价/时序衰减/截面/行业中性化组合）。",
            direction="bidirectional", data_requires=tuple(valid_atoms),
            kind="genetic", compute=lambda pan, nodes=nodes, c=ctx: _tree_eval(nodes, atom_frames, c),
            params={"_nodes": nodes},
        ))
        if len(out) >= budget:
            break
    stats["output_candidates"] = len(out)
    stats["unique_trees_evaluated"] = len(fitness_cache)
    return out


# ════════════════════════════════════════════════════════════════
# 统一入口
# ════════════════════════════════════════════════════════════════

def generate_candidates(panel: Panel, is_dates: list, fwd: pd.DataFrame,
                        include_genetic: bool = False,
                        quick: bool = False,
                        ctx: dict | None = None,
                        gen_seed: int | None = None,
                        exclude_formulas: set[str] | None = None,
                        search_stats: dict | None = None,
                        atom_frames: dict | None = None) -> list[Candidate]:
    if include_genetic:
        raise ValueError(
            "[unsourced_gp_disabled] 未经来源登记和预注册的 GP 候选已禁用"
        )
    cands = (generate_source_candidates() + generate_template_candidates()
             + generate_combo_candidates())
    # 单批上限（8GB 串行）
    if len(cands) > config.MAX_CANDIDATES_PER_RUN:
        cands = cands[: config.MAX_CANDIDATES_PER_RUN]
    return cands


def compute_factor(panel: Panel, cand: Candidate) -> pd.DataFrame | None:
    """计算候选因子面板；失败返回 None（数据缺口等），由管道诚实记录。"""
    try:
        fr = cand.compute(panel)
        if fr is None:
            return None
        if isinstance(fr, pd.DataFrame) and fr.index.size > 0:
            return mask_signal_eligible(panel, fr)
        return None
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════
# 配方（recipe）：把候选序列化为可重建的规格，供回测区离线重建
# ════════════════════════════════════════════════════════════════

def build_atom_frames(panel: Panel) -> dict:
    frames = {}
    for aid, fn in _ATOMS:
        try:
            fr = fn(panel)
            if fr is not None and fr.notna().sum().sum() > 0:
                frames[aid] = mask_signal_eligible(panel, fr)
            else:
                frames[aid] = None
        except Exception:
            frames[aid] = None
    return frames


def _freefloat_abturn_recipe() -> dict:
    return {
        "kind": "source",
        "id": _FREEFLOAT_ABTURN_ID,
        "params": dict(_FREEFLOAT_ABTURN_PARAMS),
        "execution_spec": dict(_FREEFLOAT_ABTURN_EXECUTION_SPEC),
        "validation_spec": dict(_FREEFLOAT_ABTURN_VALIDATION_SPEC),
    }


def _maxret_recipe() -> dict:
    return {
        "kind": "source",
        "id": _MAXRET_ID,
        "params": dict(_MAXRET_PARAMS),
        "execution_spec": dict(_MAXRET_EXECUTION_SPEC),
        "validation_spec": dict(_MAXRET_VALIDATION_SPEC),
    }


def _short_reversal_recipe() -> dict:
    return {
        "kind": "source",
        "id": _SHORT_REVERSAL_ID,
        "params": dict(_SHORT_REVERSAL_PARAMS),
        "execution_spec": dict(_SHORT_REVERSAL_EXECUTION_SPEC),
        "validation_spec": dict(_SHORT_REVERSAL_VALIDATION_SPEC),
    }


def to_recipe(cand: Candidate) -> dict:
    if cand.kind == "template":
        p = {k: v for k, v in cand.params.items() if k != "_tpl_id"}
        return {"kind": "template", "tpl_id": cand.params.get("_tpl_id"), "params": p}
    if cand.kind == "combo":
        return {"kind": "combo", "id": cand.factor_id}
    if cand.kind == "genetic":
        return {"kind": "genetic", "formula": cand.formula,
                "nodes": cand.params.get("_nodes")}
    if cand.kind == "source" and cand.factor_id == _FREEFLOAT_ABTURN_ID:
        if (
            cand.params != _FREEFLOAT_ABTURN_PARAMS
            or cand.formula != _FREEFLOAT_ABTURN_FORMULA
            or cand.direction != "lower_is_better"
        ):
            raise ValueError("[freefloat_abturn_recipe_drift] 候选规格漂移")
        return _freefloat_abturn_recipe()
    if cand.kind == "source" and cand.factor_id == _MAXRET_ID:
        if (
            cand.params != _MAXRET_PARAMS
            or cand.formula != _MAXRET_FORMULA
            or cand.direction != "lower_is_better"
        ):
            raise ValueError("[maxret_recipe_drift] 候选规格漂移")
        return _maxret_recipe()
    if cand.kind == "source" and cand.factor_id == _SHORT_REVERSAL_ID:
        if (
            cand.params != _SHORT_REVERSAL_PARAMS
            or cand.formula != _SHORT_REVERSAL_FORMULA
            or cand.direction != "lower_is_better"
        ):
            raise ValueError("[short_reversal_recipe_drift] 候选规格漂移")
        return _short_reversal_recipe()
    if cand.kind == "source":
        return {"kind": "source", "id": cand.factor_id,
                "params": dict(cand.params)}
    return {"kind": "unknown"}


def rebuild_factor(panel: Panel, recipe: dict,
                   atom_frames: dict | None = None) -> pd.DataFrame | None:
    """按配方重建因子面板（回测区用）。"""
    try:
        kind = recipe.get("kind")
        factor = None
        if kind == "template":
            t = _by_id(recipe["tpl_id"])
            factor = t.compute(panel, recipe.get("params", {}))
        elif kind == "combo":
            cand = next((c for c in generate_combo_candidates() if c.factor_id == recipe.get("id")), None)
            factor = cand.compute(panel) if cand else None
        elif kind == "genetic":
            nodes = recipe.get("nodes")
            if nodes is None:
                return None
            frames = atom_frames if atom_frames is not None else build_atom_frames(panel)
            factor = _tree_eval(nodes, frames, _industry_ctx(panel))
        elif kind == "source" and recipe.get("id") == _PRICE_DELAY_ID:
            factor = _price_delay_rsq(panel, recipe.get("params"))
        elif kind == "source" and recipe.get("id") == _LOW_TURNOVER_ID:
            factor = _low_turnover_20(panel, recipe.get("params"))
        elif kind == "source" and recipe.get("id") == _FREEFLOAT_ABTURN_ID:
            if recipe != _freefloat_abturn_recipe():
                return None
            factor = _freefloat_abturn_daily_20_250(
                panel, recipe.get("params"),
            )
        elif kind == "source" and recipe.get("id") == _MAXRET_ID:
            if recipe != _maxret_recipe():
                return None
            factor = _maxret_daily_20_60(panel, recipe.get("params"))
        elif kind == "source" and recipe.get("id") == _SHORT_REVERSAL_ID:
            if recipe != _short_reversal_recipe():
                return None
            factor = _short_reversal_daily_5_5(panel, recipe.get("params"))
        if isinstance(factor, pd.DataFrame):
            return mask_signal_eligible(panel, factor)
        return None
    except Exception:
        return None
