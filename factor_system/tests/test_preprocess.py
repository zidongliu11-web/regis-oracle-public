"""M1.1 单测：中性化链路（行业+市值 Frisch-Waugh 残差）+ raw/neutralized 双 IC 列。

运行：`python -m pytest factor_system/tests -q`（项目根目录）。
全部用合成面板（固定种子，无真实数据，无未来函数）。行业/市值结构显式构造，
验证：纯行业暴露被杀死、纯规模暴露被杀死、混合信号残留与行业/市值正交、双 IC
证据列存在、行业缺失降级、无 panel 向后兼容。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from factor_system.data import Panel
from factor_system.preprocess import build_exposures, industry_exposures, neutralize
from factor_system.validate import run_validation

IND_NAMES = ["消费", "科技", "金融"]


def _mk_panel(n_dates: int = 340, n_stocks: int = 90, seed: int = 7,
              with_meta: bool = True) -> tuple[Panel, pd.DatetimeIndex]:
    """合成面板：3 个行业（大/中/小市值分层），复权=未复权。"""
    rng = np.random.default_rng(seed)
    codes = [f"600{101 + i:03d}" for i in range(n_stocks)]
    dates = pd.date_range("2023-10-01", periods=n_dates, freq="B")
    s0 = rng.uniform(8, 30, n_stocks)
    ret = rng.normal(0.0004, 0.012, (n_dates, n_stocks))
    price = s0[None, :] * np.exp(np.cumsum(ret, axis=0))
    close = pd.DataFrame(price, index=dates, columns=codes)
    open_ = close * (1 + rng.normal(0, 0.003, (n_dates, n_stocks)))
    adj_open = open_.copy()
    dummy = close * 0 + 1.0
    # 流通市值：行业分层（大/中/小）+ 对数正态噪声（市值带行业色）
    third = n_stocks // 3
    base_mv = np.concatenate([
        np.full(third, 5e11), np.full(third, 5e10), np.full(third, 5e9)])
    float_mv = pd.DataFrame(base_mv[None, :] * rng.lognormal(0, 0.3, (n_dates, n_stocks)),
                            index=dates, columns=codes)

    meta = pd.DataFrame()
    if with_meta:
        industries = [IND_NAMES[i // third] if i // third < 3 else IND_NAMES[2]
                      for i in range(n_stocks)]
        meta = pd.DataFrame({
            "ts_code": codes,
            "industry": industries,
            # 明确证明这组行业标签在整个合成研究窗口前已生效。
            "effective_date": dates[0] - pd.Timedelta(days=1),
        })

    panel = Panel(
        close=close,
        ret=close.pct_change(fill_method=None),
        vol=dummy, amount=dummy, turnover=dummy, float_mv=float_mv,
        pct_chg=close.pct_change(fill_method=None),
        open_=open_, adj_open=adj_open,
        high=open_ * 1.01, low=open_ * 0.99,
        pe_ttm=None, pb_mrq=None, meta=meta,
        codes=codes, dates=list(dates),
    )
    return panel, dates


# ── 1. 纯行业暴露被杀死 ─────────────────────────────────────────
def test_neutralize_kills_pure_industry_factor():
    panel, _ = _mk_panel(seed=5)
    exp = build_exposures(panel)
    assert exp["mode"] == "industry+size"
    ind = np.broadcast_to(exp["ind_labs"][None, :].astype(float),
                          (len(panel.dates), len(panel.codes)))
    factor = pd.DataFrame(ind, index=panel.dates, columns=panel.codes)
    res = neutralize(factor, exp)
    # 行业内去均值 → 行业内恒定因子的残差恒为 0
    assert np.nanmax(np.abs(res.values)) < 1e-9


# ── 2. 纯规模暴露被杀死 ─────────────────────────────────────────
def test_neutralize_kills_pure_size_factor():
    panel, _ = _mk_panel(seed=5)
    exp = build_exposures(panel)
    factor = np.log10(panel.float_mv.clip(lower=1e6)).replace([np.inf, -np.inf], np.nan)
    res = neutralize(factor, exp)
    # log_mv 与自身残差化 → β=1 → 残差≈0（与行业/市值完全共线）
    assert np.nanmax(np.abs(res.values)) < 1e-6


# ── 3. 混合信号：残差与行业/市值正交，信号保留 ──────────────────
def test_neutralize_orthogonality_with_mixed_signal():
    panel, _ = _mk_panel(seed=11)
    exp = build_exposures(panel)
    rng = np.random.default_rng(3)
    n_d, n_s = len(panel.dates), len(panel.codes)
    industry_effect = np.tile(exp["ind_labs"], (n_d, 1)).astype(float) * 0.4
    size_effect = np.log10(panel.float_mv.clip(lower=1e6)).values * 0.01
    noise = rng.normal(0, 1, (n_d, n_s))
    factor = pd.DataFrame(industry_effect + size_effect + noise,
                          index=panel.dates, columns=panel.codes)
    res = neutralize(factor, exp)
    for d in (panel.dates[0], panel.dates[100], panel.dates[-1]):
        row = res.loc[d]
        mv = panel.float_mv.loc[d]
        m = row.notna() & mv.notna()
        tmp = pd.DataFrame({"res": row[m], "ind": exp["ind_labs"][m]})
        ind_mean = tmp.groupby("ind")["res"].mean()
        assert np.allclose(ind_mean.values, 0.0, atol=1e-6)          # 行业内均值≈0
        c = pd.concat([row[m], np.log10(mv[m])], axis=1).corr().iloc[0, 1]
        assert abs(c) < 0.02                                         # 残差与市值正交
    # 信号仍在：残差与噪声逐列相关 ≈1（行业/市值投影掉的是小部分）
    noise_df = pd.DataFrame(noise, index=panel.dates, columns=panel.codes)
    assert res.corrwith(noise_df).mean() > 0.9


# ── 4. run_validation 双 IC 证据列 ───────────────────────────────
def test_run_validation_dual_ic_columns():
    panel, dates = _mk_panel(seed=17)
    hold = 5
    fwd = panel.close.shift(-hold) / panel.close - 1.0
    is_dates = list(dates[:140])
    oos_dates = list(dates[140:])
    rng = np.random.default_rng(5)
    factor = pd.DataFrame(fwd.loc[is_dates].values + rng.normal(0, 0.05, (140, len(panel.codes))),
                          index=is_dates, columns=panel.codes)
    res = run_validation(factor, fwd, is_dates, oos_dates, hold=hold, panel=panel)
    assert res["neutralized"] is True
    assert res["neutralize_mode"] == "industry+size"
    assert res["raw_ic_mean"] is not None
    assert res["neutralized_ic_mean"] is not None
    # 真信号与行业/市值弱相关 → 中性化不杀光（raw/neut IC 同号且接近）
    assert res["raw_ic_mean"] * res["neutralized_ic_mean"] > 0
    assert res["raw_ic_mean"] == pytest.approx(res["neutralized_ic_mean"], abs=0.25)


# ── 5. 行业缺失/覆盖不足 → 降级 ─────────────────────────────────
def test_degradation_when_meta_missing():
    panel, _ = _mk_panel(with_meta=False, seed=31)
    exp = build_exposures(panel)
    assert exp["mode"] == "size_only"
    assert exp["industry_coverage"] == 0.0


def test_static_industry_snapshot_never_enters_historical_neutralization():
    panel, _ = _mk_panel(seed=37)
    panel.meta = panel.meta.drop(columns="effective_date")

    assert industry_exposures(panel)["mode"] == "none"
    exp = build_exposures(panel)
    assert exp["mode"] == "size_only"
    assert exp["ind_labs"] is None
    assert exp["industry_coverage"] == 0.0


def test_degradation_when_industry_coverage_low():
    panel, _ = _mk_panel(seed=23)
    # 篡改 meta：70% 行业置空 → 覆盖率 0.3 < 0.5 → 降级
    panel.meta.loc[panel.meta.index[: int(0.7 * len(panel.meta))], "industry"] = ""
    exp = build_exposures(panel)
    assert exp["mode"] == "size_only"
    assert exp["industry_coverage"] < 0.5


# ── 6. 无 panel 向后兼容（旧调用路径）────────────────────────────
def test_backwards_compat_no_panel():
    panel, dates = _mk_panel(seed=29)
    hold = 5
    fwd = panel.close.shift(-hold) / panel.close - 1.0
    is_dates = list(dates[:140])
    oos_dates = list(dates[140:])
    rng = np.random.default_rng(6)
    factor = pd.DataFrame(fwd.loc[is_dates].values + rng.normal(0, 0.05, (140, len(panel.codes))),
                          index=is_dates, columns=panel.codes)
    res = run_validation(factor, fwd, is_dates, oos_dates, hold=hold)
    assert res["neutralized"] is False
    assert res["neutralize_mode"] is None
    assert res["raw_ic_mean"] is not None        # 未中性化时 raw==门槛 IC
    assert res["neutralized_ic_mean"] is None


def test_missing_industry_does_not_regress_low_volatility_factor_on_itself():
    panel, _ = _mk_panel(with_meta=False, seed=41)
    factor = panel.ret.rolling(20, min_periods=5).std()
    result = neutralize(factor, build_exposures(panel))

    aligned = result.stack().dropna()
    assert len(aligned) > 0
    assert aligned.abs().max() > 1e-6
    assert aligned.std() > 1e-6
