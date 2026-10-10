"""P-C 单测：科学闸门（2026-08-17 起显著性=OOS IC，daily_t 仅报告）/ 严格 A 股回测
（整手 + 涨跌停）/ THS 成本路径 / walk-forward L2。

运行：`python -m pytest factor_system/tests -q`（项目根目录）。
全部用合成面板（固定种子，无真实数据，无未来函数）；涨跌停买卖侧为函数级直测，
整手与成本为 quantqc 单一真相源 + 合成面板回测冒烟（集成 carry 逻辑随冒烟执行）。

2026-08-20 显著性口径校准：组合层绝对收益 daily_t>=2 仅作报告项
（hold=10/370 天下需 Sharpe≥5.2 数学不可达），统计显著改由 OOS IC 最大相位/HAC p + 跑赢上证
折数 + 超额为正共同判定。本模块测试锁定新口径。
"""
from __future__ import annotations

import hashlib
import json
import numpy as np
import pandas as pd
import pytest

import factor_system.backtest as backtest_module
import factor_system.gate as gate_module
from factor_system.backtest import (
    _entry_unbuyable, _exit_unsellable, _market_vol_exposure_scale,
    _round_trip_cost, run_backtest,
)
from factor_system.data import Panel
from factor_system.gate import (
    _capital_deployable, _excess_vs_shanghai, _sensitivity_specs,
    daily_t_stat, run_scientific_gate,
)
from factor_system.metrics import ic_stats
from factor_system.validate import l2_walkforward_forward

from quantqc import costs as qc


def _mk_panel(n_dates: int = 120, n_stocks: int = 40, seed: int = 7) -> tuple[Panel, pd.DatetimeIndex]:
    """合成面板：无除权除息（复权因子=1），主板 10% 涨跌停口径。"""
    rng = np.random.default_rng(seed)
    codes = [f"600{101 + i:03d}" for i in range(n_stocks)]
    dates = pd.date_range("2023-10-01", periods=n_dates, freq="B")
    s0 = rng.uniform(8, 30, n_stocks)
    ret = rng.normal(0.0004, 0.012, (n_dates, n_stocks))
    price = s0[None, :] * np.exp(np.cumsum(ret, axis=0))
    close = pd.DataFrame(price, index=dates, columns=codes)
    open_ = close * (1 + rng.normal(0, 0.003, (n_dates, n_stocks)))
    adj_open = open_.copy()  # 复权=未复权
    dummy = close * 0 + 1.0
    panel = Panel(
        close=close,
        ret=close.pct_change(fill_method=None),
        vol=dummy, amount=dummy, turnover=dummy, float_mv=dummy,
        pct_chg=close.pct_change(fill_method=None),
        open_=open_, adj_open=adj_open,
        high=open_ * 1.01, low=open_ * 0.99,
        pe_ttm=None, pb_mrq=None,
        codes=codes, dates=list(dates), limit_reference=close.shift(1),
    )
    return panel, dates


def _flat_execution_fixture(n_dates: int = 90, n_stocks: int = 40,
                            daily_return: float = 0.0) -> tuple[Panel, pd.DatetimeIndex]:
    """固定价格路径夹具：把收益差异限定在成交和费用，避免随机行情干扰断言。"""
    panel, dates = _mk_panel(n_dates=n_dates, n_stocks=n_stocks, seed=31)
    path = 10.0 * np.power(1.0 + daily_return, np.arange(n_dates, dtype=float))
    prices = pd.DataFrame(np.repeat(path[:, None], n_stocks, axis=1),
                          index=dates, columns=panel.codes)
    panel.close = prices.copy()
    panel.open_ = prices.copy()
    panel.adj_open = prices.copy()
    panel.ret = prices.pct_change(fill_method=None)
    panel.pct_chg = panel.ret.copy()
    panel.high = prices * 1.01
    panel.low = prices * 0.99
    panel.limit_reference = prices.shift(1)  # Explicitfictionalnonactionreference
    return panel, dates


def _factor_with_selected_groups(dates: list[pd.Timestamp], codes: list[str],
                                 groups: list[list[str]]) -> pd.DataFrame:
    """每日将指定代码组稳定放入顶分位，其余代码保持唯一低分。"""
    rows = []
    for i, _date in enumerate(dates):
        selected = groups[min(i, len(groups) - 1)]
        values = pd.Series(np.arange(len(codes), dtype=float), index=codes)
        for rank, code in enumerate(selected):
            values.loc[code] = 1_000.0 + rank
        rows.append(values.values)
    return pd.DataFrame(rows, index=dates, columns=codes)


def _single_signal_factor(dates: list[pd.Timestamp], codes: list[str],
                          selected: list[str], signal_pos: int = 0) -> pd.DataFrame:
    """只在一个 T 日生成排名，便于证明取消委托不会在 T+2 重试。"""
    factor = pd.DataFrame(np.nan, index=dates, columns=codes, dtype=float)
    values = pd.Series(np.arange(len(codes), dtype=float), index=codes)
    for rank, code in enumerate(selected):
        values.loc[code] = 1_000.0 + rank
    factor.loc[dates[signal_pos]] = values
    return factor


def _attach_execution_masks(panel: Panel) -> None:
    """为兼容夹具显式补齐新执行状态；生产面板由 data._build_panel 生成。"""
    shape = panel.close.shape
    panel.signal_eligible = pd.DataFrame(True, index=panel.close.index, columns=panel.close.columns)
    panel.tradable = pd.DataFrame(True, index=panel.close.index, columns=panel.close.columns)
    panel.is_st_panel = pd.DataFrame(False, index=panel.close.index, columns=panel.close.columns)
    panel.is_delisted_panel = pd.Series(False, index=panel.close.columns, dtype=bool)
    panel.last_date = pd.Series(pd.Timestamp(panel.close.index[-1]), index=panel.close.columns)
    # This helper owns constructed quote assumptions, never productionfallback.
    raw = (panel.raw_close if isinstance(panel.raw_close, pd.DataFrame) else
           panel.close / (panel.adj_open / panel.open_.replace(0, np.nan)))
    panel.limit_reference = raw.ffill().shift(1)
    assert panel.signal_eligible.shape == shape


# ── 1. daily_t_stat：√hold 折减 ───────────────────────────────────
def test_daily_t_stat_overlap_reduction():
    rng = np.random.default_rng(2)
    x = pd.Series(rng.normal(0.002, 0.01, 120))
    t1 = daily_t_stat(x, hold=1)
    t10 = daily_t_stat(x, hold=10)
    assert t1 == pytest.approx(t10 * np.sqrt(10))   # 折减关系精确成立
    assert t10 < 2.0                                # 折减后不显著 → 诚实门槛生效
    y = pd.Series(rng.normal(0.0, 0.01, 120))
    assert abs(daily_t_stat(y, hold=1)) < 2.0       # 零均值 → 不显著


def test_overlapping_ic_p_value_uses_conservative_non_overlapping_phases():
    # 12 个独立块各重复 10 次：朴素 sqrt(120) 会把重复观测误当成新证据。
    blocks = np.array([
        0.05, 0.03, 0.04, -0.02, 0.02, 0.06,
        -0.01, 0.05, -0.03, 0.03, 0.04, -0.02,
    ])
    repeated = pd.Series(np.repeat(blocks, 10))
    naive = ic_stats(repeated, hold=1)
    strict = ic_stats(repeated, hold=10)

    assert naive["p_value"] < 0.01
    assert strict["p_value_method"] == "non_overlapping_phase_max_hac"
    assert strict["n_effective"] == 12
    assert strict["p_value"] > 0.05
    assert strict["p_value_hac_lags"] >= 1


def test_hac_phase_max_does_not_inflate_false_positives_on_serial_null():
    rng = np.random.default_rng(20260819)
    false_positives = 0
    true_positives = 0
    trials = 100
    for _ in range(trials):
        innovation = rng.normal(size=400)
        null = np.zeros(400)
        signal = np.zeros(400)
        for index in range(1, len(null)):
            null[index] = 0.65 * null[index - 1] + innovation[index]
            signal[index] = 0.65 * signal[index - 1] + innovation[index] + 0.30
        null_stats = ic_stats(pd.Series(null), hold=10)
        signal_stats = ic_stats(pd.Series(signal), hold=10)
        false_positives += null_stats["p_value"] < 0.05
        true_positives += signal_stats["p_value"] < 0.05
        assert len(null_stats["p_value_phase_sensitivity"]) == 10

    assert false_positives / trials <= 0.05
    assert true_positives / trials >= 0.70


def test_execution_fingerprint_detects_duplicate_sensitivity_paths():
    from factor_system.gate import _execution_fingerprint

    daily = pd.Series([0.01, -0.02], index=pd.date_range("2025-01-01", periods=2))
    base = {"ok": True, "daily_ret": daily, "n_days": 2,
            "total_fees": 12.3, "traded_notional": 456.0}
    assert _execution_fingerprint(base) == _execution_fingerprint({**base})
    assert _execution_fingerprint({**base, "total_fees": 12.4}) != _execution_fingerprint(base)


# ── 2. 科学闸门（2026-08-17 显著性口径）：弱因子被拒，daily_t 仅报告 ──
def test_gate_rejects_weak_factor():
    """噪声因子仍被拒——但不是因为 daily_t>=2（2026-08-17 已移除），而是 OOS IC
    不显著/组合层不跑赢上证。t_stat 降级为报告项，留在证据里诚实披露。"""
    panel, dates = _mk_panel(seed=11)
    oos = list(dates[40:])                          # 80 个 OOS 交易日
    rng = np.random.default_rng(9)
    n, m = len(oos), len(panel.codes)
    factor = pd.DataFrame(rng.normal(0, 1, (n, m)), index=oos, columns=panel.codes)
    passed, ev = run_scientific_gate(
        panel, factor, oos, hold=10, q=5,
        costs={"capital": 200_000, "n_positions": 10, "slippage": 0.001})
    assert passed is False
    assert "daily_t>=2" not in ev["checks"]         # 2026-08-17 已从闸门移除
    assert "daily_t>=2" not in ev["reason"]
    # 噪声因子显著性由 OOS IC 把关（横截面秩相关）：噪声 → |IC t| 小 → 不显著
    assert ev["checks"]["oos_ic_significant"] is False
    # t_stat 作为报告项仍存在（供看板/诊断披露），带注释字段说明降级
    assert "t_stat" in ev["full"]
    assert "t_stat_note" in ev["full"]
    # 早退诚实性：fold/敏感未运行 → not_run 标注，而不是补 False 占位（避免看板误读成失败）
    assert ev.get("not_run") == ["fold_pass_rate>=60%", "sensitivity>=60%"]
    assert ev.get("early_exit")


def test_gate_tstat_is_report_only_not_gating():
    """锁定新口径：即使组合层绝对收益日 t 远小于 2，checks 里也没有 daily_t 项；
    显著性完全由 oos_ic_significant 承担，杜绝「IC 强但被绝对收益 t 误杀」。"""
    panel, dates = _mk_panel(seed=12)
    oos = list(dates[40:])
    rng = np.random.default_rng(9)
    n, m = len(oos), len(panel.codes)
    factor = pd.DataFrame(rng.normal(0, 1, (n, m)), index=oos, columns=panel.codes)
    passed, ev = run_scientific_gate(
        panel, factor, oos, hold=10, q=5,
        costs={"capital": 200_000, "n_positions": 10, "slippage": 0.001})
    t_rep = ev["full"]["t_stat"]
    assert t_rep is not None and abs(t_rep) < 2.0    # 噪声组合：报告 t 不显著（如实披露）
    for key in ("daily_t>=2",):
        assert key not in ev["checks"]               # 报告值不进入闸门判定
    assert ev["checks"]["oos_ic_significant"] is False
    assert passed is False


@pytest.mark.parametrize("entry_rule", [
    "previous_close_limit_no_chase_v1",
    "previous_close_gap_2pct_no_chase_v1",
])
def test_gate_evidence_freezes_previous_close_entry_rule(entry_rule):
    panel, dates = _mk_panel(n_dates=90, seed=47)
    _attach_execution_masks(panel)
    oos = list(dates[10:])
    rng = np.random.default_rng(19)
    factor = pd.DataFrame(
        rng.normal(size=(len(oos), len(panel.codes))),
        index=oos, columns=panel.codes,
    )

    _passed, evidence = run_scientific_gate(
        panel, factor, oos, hold=5, q=5,
        costs={
            "capital": 200_000, "n_positions": 8, "slippage": 0.001,
            "entry_rule": entry_rule,
        },
        capture_audit=True,
    )

    assert evidence["backtest_spec"]["entry_rule"] == entry_rule
    assert evidence["full"]["entry_rule"] == entry_rule
    assert evidence["audit"]["portfolio_construction"]["entry_rule"] == entry_rule


# ── 2b. 2026-08-14 强制基准：上证指数（sh000001）必跑赢 + fail-closed ──
def test_gate_requires_shanghai_benchmark(monkeypatch):
    """闸门含 beats_shanghai 硬检查 + shanghai_bench 证据（基准口径已改为上证）。"""
    panel, dates = _mk_panel(seed=12)
    benchmark = pd.Series(np.linspace(3000, 3200, len(dates)), index=dates)
    monkeypatch.setattr(
        "factor_system.gate._shanghai_snapshot",
        lambda: (benchmark, {"path": "fixture", "sha256": "fixture", "size": 1}),
    )
    oos = list(dates[40:])
    rng = np.random.default_rng(9)
    n, m = len(oos), len(panel.codes)
    factor = pd.DataFrame(rng.normal(0, 1, (n, m)), index=oos, columns=panel.codes)
    passed, ev = run_scientific_gate(
        panel, factor, oos, hold=10, q=5,
        costs={"capital": 200_000, "n_positions": 10, "slippage": 0.001})
    assert "beats_shanghai" in ev["checks"]          # 强制基准检查存在
    assert "beats_matched_total_return" in ev["checks"]
    assert "shanghai_bench" in ev                    # 上证证据块存在
    assert ev["shanghai_bench"]["ok"] is True        # 合成日期在真实 sh000001 覆盖内 → 可比较
    assert "shanghai_annual" in ev["full"]           # 全证据带上证年化
    assert passed is False                           # 弱因子整体仍被拒（不因基准变更放行）


def test_gate_shanghai_fail_closed_when_data_missing(monkeypatch):
    """上证数据缺失 → beats_shanghai 强制 False（fail-closed），绝不静默跳过强制基准。"""
    panel, dates = _mk_panel(seed=13)
    oos = list(dates[40:])
    rng = np.random.default_rng(9)
    n, m = len(oos), len(panel.codes)
    factor = pd.DataFrame(rng.normal(0, 1, (n, m)), index=oos, columns=panel.codes)
    monkeypatch.setattr(
        "factor_system.gate._shanghai_snapshot",
        lambda: (None, {"path": "missing", "sha256": None, "size": None}),
    )
    passed, ev = run_scientific_gate(
        panel, factor, oos, hold=10, q=5,
        costs={"capital": 200_000, "n_positions": 10, "slippage": 0.001})
    assert ev["shanghai_bench"]["ok"] is False
    assert ev["shanghai_bench"]["reason"] == "上证基准数据缺失"
    assert ev["checks"]["beats_shanghai"] is False   # fail-closed
    assert passed is False


def test_scientific_gate_binds_one_benchmark_snapshot_to_frozen_input(monkeypatch):
    panel, dates = _mk_panel(seed=14)
    panel._panel_cache_key = gate_module._panel_cache_path().name
    oos = list(dates[40:])
    benchmark = pd.Series(np.linspace(3000, 3200, len(dates)), index=dates)
    calls = {"count": 0}

    def snapshot():
        calls["count"] += 1
        return benchmark, {"path": "fixture", "sha256": "stable", "size": 1}

    monkeypatch.setattr(gate_module, "_shanghai_snapshot", snapshot)
    expected = f"{gate_module._panel_cache_path().name}:shanghai:stable"
    rng = np.random.default_rng(3)
    factor = pd.DataFrame(
        rng.normal(size=(len(oos), len(panel.codes))),
        index=oos, columns=panel.codes,
    )

    _passed, evidence = run_scientific_gate(
        panel, factor, oos, hold=10, q=5,
        costs={"capital": 200_000, "n_positions": 10, "slippage": 0.001},
        expected_input_key=expected,
    )

    assert calls["count"] == 1
    assert evidence["research_input"]["data_key"] == expected

    with pytest.raises(RuntimeError, match="输入已变化"):
        run_scientific_gate(
            panel, factor, oos, hold=10, q=5,
            costs={"capital": 200_000, "n_positions": 10, "slippage": 0.001},
            expected_input_key=expected.replace("stable", "old"),
        )

    panel._panel_cache_key = "panel-old.pkl"
    with pytest.raises(RuntimeError, match="输入已变化"):
        run_scientific_gate(
            panel, factor, oos, hold=10, q=5,
            costs={"capital": 200_000, "n_positions": 10, "slippage": 0.001},
            expected_input_key=expected,
        )


def test_expected_input_rejects_unidentified_panel(monkeypatch):
    panel, dates = _mk_panel(seed=15)
    benchmark = pd.Series(np.linspace(3000, 3200, len(dates)), index=dates)
    monkeypatch.setattr(
        gate_module, "_shanghai_snapshot",
        lambda: (benchmark, {"path": "fixture", "sha256": "stable", "size": 1}),
    )
    factor = pd.DataFrame(
        0.0, index=dates, columns=panel.codes,
    )

    with pytest.raises(RuntimeError, match="缺少 _panel_cache_key"):
        run_scientific_gate(
            panel, factor, list(dates[40:]),
            expected_input_key="panel-frozen.pkl:shanghai:stable",
        )


def test_shanghai_existing_but_stale_window_fails_closed(monkeypatch):
    source_dates = pd.bdate_range("2024-01-02", periods=40)
    monkeypatch.setattr(
        "factor_system.gate._shanghai_series",
        lambda: pd.Series(np.linspace(3000, 3100, 40), index=source_dates),
    )
    strategy_dates = pd.bdate_range("2026-01-05", periods=30)
    result = _excess_vs_shanghai(
        pd.Series(0.001, index=strategy_dates), list(strategy_dates),
    )
    assert result["ok"] is False
    assert result["n_days"] == 0
    assert result["coverage"] == 0.0


def test_shanghai_return_is_aligned_to_same_t_to_t_plus_one(monkeypatch):
    source_dates = pd.bdate_range("2024-01-02", periods=31)
    closes = np.full(31, 100.0)
    closes[-1] = 200.0
    monkeypatch.setattr(
        "factor_system.gate._shanghai_series",
        lambda: pd.Series(closes, index=source_dates),
    )
    # daily_ret[d] 是 d 日开盘到下一交易日开盘；基准也必须使用 d→下一日。
    strategy = pd.Series(np.r_[np.zeros(29), 1.0], index=source_dates[:-1])
    result = _excess_vs_shanghai(strategy, list(source_dates[:-1]))
    assert result["ok"] is True
    assert result["coverage"] == 1.0
    assert result["strategy_annual"] == pytest.approx(result["shanghai_annual"])
    assert result["excess_annual"] == pytest.approx(0.0)
    assert result["basis"] == (
        "strategy=open t→t+1 · shanghai_price_index=open t→t+1"
    )


# ── 3. THS 成本路径（quantqc 单一真相源）─────────────────────────
def test_round_trip_cost_matches_quantqc_ths():
    rc = _round_trip_cost({"capital": 200_000, "n_positions": 10, "slippage": 0.001})
    assert rc == pytest.approx(qc.cost_rate_for_portfolio(200_000, 10, 0.001))
    # 印花税仅卖出：往返 = 买边 + 卖边（卖边含 0.05% 印花税）
    assert rc == pytest.approx(
        qc.one_side_cost_rate(20_000, "buy", 0.001) + qc.one_side_cost_rate(20_000, "sell", 0.001))
    # 最低 ¥5 佣金：小额单笔成本率被抬高（5k 名义 vs 20k 名义）
    small = _round_trip_cost({"capital": 5_000, "n_positions": 1, "slippage": 0.001})
    large = _round_trip_cost({"capital": 200_000, "n_positions": 10, "slippage": 0.001})
    assert small > large


def test_max_drawdown_includes_initial_nav_before_first_loss(monkeypatch):
    returns = pd.Series(
        np.r_[-0.40, 1.00, np.zeros(28)],
        index=pd.bdate_range("2024-01-02", periods=30),
    )
    monkeypatch.setattr(backtest_module, "_baseline_daily", lambda *_a, **_k: returns)

    result = backtest_module.run_market_baseline(None, list(returns.index))

    assert result["ok"] is True
    assert result["max_drawdown"] == pytest.approx(-0.40)
    assert result["max_drawdown"] < -0.35


def _uncached_float_mv_baseline(panel, dates, annual_cost):
    open_ = panel.adj_open
    o2o = (open_.shift(-1) / open_).astype(np.float64) - 1.0
    weights = panel.float_mv.astype(np.float64)
    usable_weights = weights.mask(~(weights.notna() & o2o.notna()))
    weight_sum = usable_weights.sum(axis=1).replace(0, np.nan)
    gross = o2o.multiply(usable_weights, axis=0).sum(axis=1) / weight_sum
    days = [day for day in dates if day in open_.index]
    return (gross.loc[days] - annual_cost / 252.0).dropna()


def test_float_mv_baseline_cache_reuses_full_series_across_windows(monkeypatch):
    panel, dates = _mk_panel(n_dates=100, seed=61)
    panel._panel_cache_key = "panel-snapshot-a.pkl"
    first_window = list(dates[5:55])
    second_window = list(dates[35:95])
    annual_cost = 0.008
    original = backtest_module._build_float_mv_baseline_daily
    builds = {"count": 0}

    def counted_build(value, cost):
        builds["count"] += 1
        return original(value, cost)

    monkeypatch.setattr(
        backtest_module, "_build_float_mv_baseline_daily", counted_build,
    )
    first = backtest_module._baseline_daily(
        panel, first_window, weight="float_mv", annual_cost=annual_cost,
    )
    second = backtest_module._baseline_daily(
        panel, second_window, weight="float_mv", annual_cost=annual_cost,
    )

    pd.testing.assert_series_equal(
        first, _uncached_float_mv_baseline(panel, first_window, annual_cost),
        check_exact=True,
    )
    pd.testing.assert_series_equal(
        second, _uncached_float_mv_baseline(panel, second_window, annual_cost),
        check_exact=True,
    )
    assert builds["count"] == 1


def test_float_mv_baseline_cache_isolated_by_panel_snapshot_matrices_and_cost(
    monkeypatch,
):
    first_panel, first_dates = _mk_panel(n_dates=90, seed=62)
    second_panel, second_dates = _mk_panel(n_dates=90, seed=63)
    first_panel._panel_cache_key = "panel-snapshot-a.pkl"
    second_panel._panel_cache_key = "panel-snapshot-b.pkl"
    window = list(first_dates[10:80])
    original = backtest_module._build_float_mv_baseline_daily
    builds = []

    def counted_build(value, cost):
        builds.append((id(value), value._panel_cache_key, float(cost)))
        return original(value, cost)

    monkeypatch.setattr(
        backtest_module, "_build_float_mv_baseline_daily", counted_build,
    )

    base = backtest_module._baseline_daily(first_panel, window, annual_cost=0.008)
    base_again = backtest_module._baseline_daily(
        first_panel, list(first_dates[20:70]), annual_cost=0.008,
    )
    higher_cost = backtest_module._baseline_daily(
        first_panel, window, annual_cost=0.012,
    )
    first_panel._panel_cache_key = "panel-snapshot-a-v2.pkl"
    new_snapshot = backtest_module._baseline_daily(
        first_panel, window, annual_cost=0.008,
    )
    first_panel.adj_open = first_panel.adj_open.copy()
    new_open_matrix = backtest_module._baseline_daily(
        first_panel, window, annual_cost=0.008,
    )
    first_panel.float_mv = first_panel.float_mv.copy()
    new_weight_matrix = backtest_module._baseline_daily(
        first_panel, window, annual_cost=0.008,
    )
    other_panel = backtest_module._baseline_daily(
        second_panel, list(second_dates[10:80]), annual_cost=0.008,
    )
    other_panel_again = backtest_module._baseline_daily(
        second_panel, list(second_dates[20:70]), annual_cost=0.008,
    )

    pd.testing.assert_series_equal(
        base, _uncached_float_mv_baseline(first_panel, window, 0.008),
        check_exact=True,
    )
    pd.testing.assert_series_equal(
        higher_cost, _uncached_float_mv_baseline(first_panel, window, 0.012),
        check_exact=True,
    )
    pd.testing.assert_series_equal(
        new_snapshot, _uncached_float_mv_baseline(first_panel, window, 0.008),
        check_exact=True,
    )
    pd.testing.assert_series_equal(
        new_open_matrix, _uncached_float_mv_baseline(first_panel, window, 0.008),
        check_exact=True,
    )
    pd.testing.assert_series_equal(
        new_weight_matrix, _uncached_float_mv_baseline(first_panel, window, 0.008),
        check_exact=True,
    )
    pd.testing.assert_series_equal(
        other_panel,
        _uncached_float_mv_baseline(
            second_panel, list(second_dates[10:80]), 0.008,
        ),
        check_exact=True,
    )
    assert len(base_again) > 0 and len(other_panel_again) > 0
    assert len(builds) == 6
    assert builds[-1][0] == id(second_panel)


def _uncached_execution_panel_frames(panel):
    adj_open = panel.adj_open
    o2o = (adj_open.shift(-1) / adj_open).astype(np.float64) - 1.0
    ratio = (adj_open / panel.open_.replace(0, np.nan)).astype(np.float64)
    raw_close = (panel.close / ratio).replace([np.inf, -np.inf], np.nan)
    return (
        o2o,
        raw_close,
        adj_open.ffill(),
        panel.open_.ffill(),
        raw_close.ffill(),
    )


def test_execution_panel_frames_cache_is_exact_and_snapshot_isolated(monkeypatch):
    panel, _ = _mk_panel(n_dates=90, seed=64)
    other_panel, _ = _mk_panel(n_dates=90, seed=65)
    panel._panel_cache_key = "execution-panel-a.pkl"
    other_panel._panel_cache_key = "execution-panel-b.pkl"
    original = backtest_module._build_execution_panel_frames
    builds = []

    def counted_build(value):
        builds.append(backtest_module._execution_panel_frames_cache_key(value))
        return original(value)

    def assert_exact(value, frames):
        reference = _uncached_execution_panel_frames(value)
        for actual, expected in zip(frames, reference):
            pd.testing.assert_frame_equal(actual, expected, check_exact=True)

    monkeypatch.setattr(
        backtest_module, "_build_execution_panel_frames", counted_build,
    )

    first = backtest_module._execution_panel_frames(panel)
    again = backtest_module._execution_panel_frames(panel)
    assert_exact(panel, first)
    assert all(left is right for left, right in zip(first, again))
    assert len(builds) == 1

    panel._panel_cache_key = "execution-panel-a-v2.pkl"
    assert_exact(panel, backtest_module._execution_panel_frames(panel))
    panel.adj_open = panel.adj_open.copy()
    assert_exact(panel, backtest_module._execution_panel_frames(panel))
    panel.open_ = panel.open_.copy()
    assert_exact(panel, backtest_module._execution_panel_frames(panel))
    panel.close = panel.close.copy()
    assert_exact(panel, backtest_module._execution_panel_frames(panel))
    assert_exact(other_panel, backtest_module._execution_panel_frames(other_panel))
    other_again = backtest_module._execution_panel_frames(other_panel)

    assert len(builds) == 6
    assert all(left is right for left, right in zip(
        other_again, backtest_module._execution_panel_frames(other_panel),
    ))
    assert len(builds) == 6
    assert builds[-1][0] == id(other_panel)


def test_execution_panel_frames_cache_preserves_backtest_and_audit_sha(monkeypatch):
    panel, dates = _flat_execution_fixture(n_dates=90, daily_return=0.001)
    _attach_execution_masks(panel)
    panel._panel_cache_key = "execution-audit-panel.pkl"
    oos = list(dates[10:])
    rng = np.random.default_rng(66)
    factor = pd.DataFrame(
        rng.normal(size=(len(oos), len(panel.codes))),
        index=oos,
        columns=panel.codes,
    )
    costs = {
        "capital": 200_000,
        "n_positions": 10,
        "slippage": 0.001,
        "rebal_buffer": 3,
    }
    cached_helper = backtest_module._execution_panel_frames

    monkeypatch.setattr(
        backtest_module, "_execution_panel_frames", _uncached_execution_panel_frames,
    )
    reference = backtest_module.run_backtest(
        panel, factor, oos, hold=10, q=5, costs=costs, capture_audit=True,
    )
    monkeypatch.setattr(
        backtest_module, "_execution_panel_frames", cached_helper,
    )
    cached = backtest_module.run_backtest(
        panel, factor, oos, hold=10, q=5, costs=costs, capture_audit=True,
    )

    assert reference["ok"] is True and cached["ok"] is True
    pd.testing.assert_series_equal(
        cached["daily_ret"], reference["daily_ret"], check_exact=True,
    )
    pd.testing.assert_series_equal(
        cached["equity"], reference["equity"], check_exact=True,
    )
    assert cached["audit"] == reference["audit"]
    assert cached["audit_sha256"] == reference["audit_sha256"]
    assert cached.keys() == reference.keys()
    for key in cached.keys() - {"daily_ret", "equity", "audit"}:
        actual, expected = cached[key], reference[key]
        if isinstance(expected, (float, np.floating)) and np.isnan(expected):
            assert isinstance(actual, (float, np.floating)) and np.isnan(actual)
        else:
            assert actual == expected


# ── 4. 整手执行（quantqc.lots_from_yuan）─────────────────────────
def test_lots_are_round_lots():
    assert qc.lots_from_yuan(50_000, 12.34) == 4000     # 4000 股 × 12.34 = 49,360 ≤ 50,000
    assert qc.lots_from_yuan(50_000, 12.34) % 100 == 0  # 恒为 100 股整数倍
    assert qc.lots_from_yuan(10_000, 30.0) == 300
    assert qc.lots_from_yuan(1_000, 30.0) == 0          # 现金不足 1 手 → 买不进（如实 0）


def test_run_backtest_smoke_with_whole_lot_costs():
    panel, dates = _mk_panel(seed=3)
    oos = list(dates[40:])
    rng = np.random.default_rng(9)
    n, m = len(oos), len(panel.codes)
    factor = pd.DataFrame(rng.normal(0, 1, (n, m)), index=oos, columns=panel.codes)
    res = run_backtest(panel, factor, oos, hold=10, q=5,
                       costs={"capital": 200_000, "n_positions": 10, "slippage": 0.001})
    assert res["ok"] is True
    assert len(res["equity"]) >= 30
    assert np.isfinite(res["annual_return"])
    assert 0 < res["daily_cost"] < res["round_trip_cost"]
    assert res["round_trip_cost"] == pytest.approx(
        qc.cost_rate_for_portfolio(200_000, 10, 0.001))


def test_overlapping_cohorts_accumulate_repeat_selection_weight():
    """同一股票被两个活跃 cohort 选中时，权重必须是只入选一次股票的两倍。

    这个断言能区分 1/H cohort 目标权重代理与旧的「H 日布尔并集后整体等权」；
    独立 lot 持有不在本引擎声称范围内。
    """
    panel, dates = _flat_execution_fixture(n_dates=70)
    oos = list(dates[5:])
    repeated, first_only, second_only = panel.codes[:3]
    factor = _factor_with_selected_groups(
        oos, panel.codes,
        [[repeated, first_only], [repeated, second_only]],
    )

    result = run_backtest(
        panel, factor, oos, hold=2, q=5,
        costs={"capital": 200_000, "n_positions": 2, "slippage": 0.0},
        capture_audit=True,
    )

    assert result["ok"]
    assert result["holding_model"] == "daily_rebalanced_overlapping_cohort_target_weights"
    assert result["audit"]["portfolio_construction"]["lot_accounting"] == (
        "no_independent_cohort_lots_daily_nav_rebalance"
    )
    assert result["cohort_budget_fraction"] == pytest.approx(0.5)
    second_execution = dates[7].date().isoformat()
    targets = {
        row["code"]: row for row in result["audit"]["targets"]
        if row["date"] == second_execution
    }
    assert targets[repeated]["cohort_target_weight"] == pytest.approx(0.50)
    assert targets[first_only]["cohort_target_weight"] == pytest.approx(0.25)
    assert targets[second_only]["cohort_target_weight"] == pytest.approx(0.25)
    assert targets[repeated]["target_notional"] == pytest.approx(
        2 * targets[first_only]["target_notional"], abs=1_000.0)


def test_run_backtest_capped_positions_investable():
    """整手 + 持仓上限（max_pos=10）：信号因子可整手建仓、组合净收益为正（非退化空仓）。

    合成夹具：因子 = 未来收益 + 噪声（仅用于测执行机制，非可部署因子）。
    """
    n_dates, n_stocks = 120, 60
    panel, dates = _mk_panel(n_dates=n_dates, n_stocks=n_stocks, seed=13)
    oos = list(dates[40:])
    hold = 10
    fwd = panel.close.shift(-hold) / panel.close - 1.0
    rng = np.random.default_rng(4)
    fac = fwd.loc[oos].values + rng.normal(0, 0.15, (len(oos), n_stocks))
    factor = pd.DataFrame(fac, index=oos, columns=panel.codes)
    res = run_backtest(panel, factor, oos, hold=hold, q=5,
                       costs={"capital": 200_000, "n_positions": 10, "slippage": 0.001})
    assert res["ok"] is True
    assert res["annual_return"] > 0                     # 顶分位前 10 名整手可投 → 正收益
    assert len(res["equity"]) >= 30


def test_top30_200k_reports_lot_cash_drag_instead_of_paper_positions():
    """20 万 Top30 在高价股上无法买一手时，必须显示空仓拖累并拒绝部署。"""
    panel, dates = _flat_execution_fixture(n_dates=90, n_stocks=200)
    oos = list(dates[10:])
    selected = panel.codes[-30:]
    factor = _factor_with_selected_groups(oos, panel.codes, [selected])
    costs = {"capital": 200_000, "n_positions": 30, "slippage": 0.0}

    affordable = run_backtest(panel, factor, oos, hold=5, q=5, costs=costs)
    assert affordable["ok"]
    assert affordable["average_funded_positions"] >= 15
    assert _capital_deployable(affordable, 30)

    expensive_panel, expensive_dates = _flat_execution_fixture(
        n_dates=90, n_stocks=200,
    )
    expensive_panel.open_.loc[:, :] = 150.0
    expensive_panel.adj_open.loc[:, :] = 150.0
    expensive_panel.close.loc[:, :] = 150.0
    expensive_panel.limit_reference.loc[:, :] = 150.0
    expensive_panel.ret.loc[:, :] = 0.0
    expensive_factor = _factor_with_selected_groups(
        list(expensive_dates[10:]), expensive_panel.codes,
        [expensive_panel.codes[-30:]],
    )
    expensive = run_backtest(
        expensive_panel, expensive_factor, list(expensive_dates[10:]),
        hold=5, q=5, costs=costs,
    )
    assert expensive["ok"]
    assert expensive["average_desired_positions"] > 0
    assert expensive["average_funded_positions"] == pytest.approx(0.0)
    assert expensive["lot_target_fill_rate"] == pytest.approx(0.0)
    assert expensive["gross_exposure_fill_rate"] == pytest.approx(0.0)
    assert _capital_deployable(expensive, 30) is False


def test_adv_participation_and_impact_are_enforced_from_prior_history():
    panel, dates = _flat_execution_fixture(n_dates=100, daily_return=0.001)
    panel.amount.loc[:, :] = 100_000_000.0
    oos = list(dates[25:])
    factor = _factor_with_selected_groups(oos, panel.codes, [panel.codes[-8:]])
    base_costs = {
        "capital": 200_000, "n_positions": 8, "slippage": 0.0,
        "adv_window": 20, "max_adv_participation": 0.05,
    }
    no_impact = run_backtest(
        panel, factor, oos, hold=5, q=5,
        costs={**base_costs, "impact_coefficient_bps": 0.0},
    )
    impacted = run_backtest(
        panel, factor, oos, hold=5, q=5,
        costs={**base_costs, "impact_coefficient_bps": 100.0},
    )
    assert no_impact["ok"] and impacted["ok"]
    assert impacted["liquidity_model"] == "prior_adv_participation_sqrt_impact_v1"
    assert impacted["total_market_impact"] > 0
    assert impacted["total_return"] < no_impact["total_return"]

    illiquid = panel.amount.copy()
    illiquid.loc[:, :] = 1_000.0
    panel.amount = illiquid
    blocked = run_backtest(
        panel, factor, oos, hold=5, q=5,
        costs={**base_costs, "impact_coefficient_bps": 25.0},
    )
    assert blocked["ok"]
    assert blocked["adv_limited_order_count"] > 0
    assert blocked["gross_exposure_fill_rate"] == pytest.approx(0.0)
    assert blocked["average_funded_positions"] == pytest.approx(0.0)


def test_total_return_benchmark_requires_every_strategy_open_interval():
    panel, dates = _flat_execution_fixture(n_dates=90)
    oos = list(dates[10:])
    factor = pd.DataFrame(np.nan, index=oos, columns=panel.codes)
    # 策略无持仓，因此价格缺口不会触发持仓估值拒绝；它只针对基准同期性。
    panel.adj_open.loc[dates[50], :] = np.nan

    result = run_backtest(
        panel, factor, oos, hold=5, q=5,
        costs={"capital": 200_000, "n_positions": 8, "slippage": 0.0},
    )

    assert result["ok"]
    assert result["benchmark_coverage"] < 1.0
    assert result["benchmark_n_days"] < result["n_days"]
    assert np.isnan(result["benchmark_annual"])
    assert np.isnan(result["excess_annual"])


def test_capital_deployable_rejects_cash_drag_and_single_name_concentration():
    healthy = {
        "gross_exposure_fill_rate": 0.95,
        "lot_target_fill_rate": 0.90,
        "average_funded_positions": 20,
        "maximum_single_weight": 0.08,
    }
    assert _capital_deployable(healthy, 30) is True
    assert _capital_deployable({**healthy, "gross_exposure_fill_rate": 0.50}, 30) is False
    assert _capital_deployable({**healthy, "maximum_single_weight": 0.30}, 30) is False


def test_actual_slippage_0_10_50bps_makes_net_return_monotonic():
    """相同成交和正收益路径下，单边滑点越高，净收益必须严格下降。"""
    panel, dates = _flat_execution_fixture(daily_return=0.001)
    oos = list(dates[10:])
    selected = [panel.codes[-8:]]
    factor = _factor_with_selected_groups(oos, panel.codes, selected)

    results = [
        run_backtest(panel, factor, oos, hold=5, q=5,
                     costs={"capital": 200_000, "n_positions": 8, "slippage": bps / 10_000})
        for bps in (0, 10, 50)
    ]
    assert all(item["ok"] for item in results)
    assert results[0]["total_return"] > results[1]["total_return"] > results[2]["total_return"]
    assert results[0]["total_fees"] < results[1]["total_fees"] < results[2]["total_fees"]


def test_terminal_liquidation_charges_final_sell_cost_and_ends_in_cash():
    """平价路径也必须支付买卖双边真实费用，末日不能留下未计费持仓。"""
    panel, dates = _flat_execution_fixture(n_dates=90)
    _attach_execution_masks(panel)
    oos = list(dates[10:])
    factor = _factor_with_selected_groups(oos, panel.codes, [panel.codes[-8:]])
    capital = 200_000.0

    result = run_backtest(
        panel, factor, oos, hold=5, q=5,
        costs={"capital": capital, "n_positions": 8, "slippage": 0.001},
        capture_audit=True,
    )

    assert result["ok"] is True
    assert result["terminal_liquidated"] is True
    assert result["ending_cash"] == pytest.approx(
        capital * (1.0 + result["total_return"]), abs=1e-8,
    )
    assert result["total_return"] == pytest.approx(
        -result["total_fees"] / capital, abs=1e-12,
    )
    terminal = result["audit"]["days"][-1]["terminal_liquidation"]
    assert terminal["ok"] is True
    assert terminal["sell_notional"] > 0
    assert terminal["fees"]["stamp"] > 0
    final_day = result["audit"]["days"][-1]
    assert final_day["holdings"]
    assert final_day["cash_weight"] == pytest.approx(
        final_day["cash"] / final_day["nav_after_trade"], abs=1e-12,
    )
    assert terminal["ending_holdings"] == []
    assert terminal["ending_actual_gross_exposure"] == 0.0
    assert terminal["ending_cash_weight"] == 1.0
    assert result["audit"]["summary"]["ending_nav"] == pytest.approx(
        result["ending_cash"], abs=1e-8,
    )


def test_sensitivity_grid_varies_real_portfolio_parameters_without_duplicates():
    specs = _sensitivity_specs(hold=10, costs={"n_positions": 20})
    assert len(specs) == 9
    assert len({(row["hold"], row["n_positions"]) for row in specs}) == 9
    assert {row["hold"] for row in specs} == {8, 10, 12}
    assert {row["n_positions"] for row in specs} == {16, 20, 24}


def test_actual_turnover_drives_notional_and_fees():
    """价格不变时，交替换仓应比稳定持仓产生更多实际成交和费用。"""
    panel, dates = _flat_execution_fixture()
    oos = list(dates[10:])
    group_a = panel.codes[:8]
    group_b = panel.codes[-8:]
    stable = _factor_with_selected_groups(oos, panel.codes, [group_a])
    rotating_groups = [group_a if i % 2 == 0 else group_b for i in range(len(oos))]
    rotating = _factor_with_selected_groups(oos, panel.codes, rotating_groups)
    cost = {"capital": 200_000, "n_positions": 8, "slippage": 0.001}

    stable_result = run_backtest(panel, stable, oos, hold=1, q=5, costs=cost)
    rotating_result = run_backtest(panel, rotating, oos, hold=1, q=5, costs=cost)

    assert stable_result["ok"] and rotating_result["ok"]
    assert rotating_result["traded_notional"] > stable_result["traded_notional"] * 5
    assert rotating_result["total_fees"] > stable_result["total_fees"] * 5
    assert rotating_result["signal_churn"] > stable_result["signal_churn"]


def test_tail_signals_only_exist_when_full_holding_window_fits():
    """T 收盘信号必须能在窗内完成 T+1 入场和 H 个 o2o 持有期。"""
    panel, dates = _flat_execution_fixture(n_dates=80)
    _attach_execution_masks(panel)
    oos = list(dates[10:])
    hold = 5
    factor = _factor_with_selected_groups(
        oos, panel.codes, [panel.codes[-8:]],
    )

    result = run_backtest(
        panel, factor, oos, hold=hold, q=5,
        costs={"capital": 200_000, "n_positions": 8,
               "slippage": 0.0, "rebal_buffer": 0,
               "entry_rule": "previous_close_limit_no_chase_v1"},
        capture_audit=True,
    )

    assert result["ok"]
    signals = result["audit"]["signals"]
    assert signals
    positions = {
        day.date().isoformat(): index for index, day in enumerate(panel.close.index)
    }
    terminal = positions[oos[-1].date().isoformat()]
    for row in signals:
        signal_pos = positions[row["signal_date"]]
        execution_pos = positions[row["execution_date"]]
        assert execution_pos == signal_pos + 1
        assert signal_pos + hold + 1 <= terminal

    latest_valid = panel.close.index[terminal - hold - 1].date().isoformat()
    assert max(row["signal_date"] for row in signals) == latest_valid
    assert not [
        row for row in signals
        if row["execution_date"] == oos[-1].date().isoformat()
    ]
    assert result["entry_rule"] == "previous_close_limit_no_chase_v1"
    assert all(row["status"] == "selected" for row in signals)


def test_raw_factor_history_cannot_trade_before_signal_eligibility() -> None:
    panel, dates = _flat_execution_fixture(n_dates=80)
    _attach_execution_masks(panel)
    oos = list(dates[10:])
    first_eligible = oos[12]
    panel.signal_eligible.loc[:oos[11], :] = False
    factor = _factor_with_selected_groups(
        oos, panel.codes, [panel.codes[-8:]],
    )

    result = run_backtest(
        panel, factor, oos, hold=5, q=5,
        costs={
            "capital": 200_000,
            "n_positions": 8,
            "slippage": 0.0,
            "entry_rule": "previous_close_limit_no_chase_v1",
        },
        capture_audit=True,
    )

    assert result["ok"]
    signals = result["audit"]["signals"]
    assert signals
    assert min(row["signal_date"] for row in signals) >= (
        first_eligible.date().isoformat()
    )


def test_previous_close_limit_cancels_without_replacement_or_retry():
    """T+1 高开取消后留现金，不选下一名，也不在 T+2 追单。"""
    panel, dates = _flat_execution_fixture(n_dates=80)
    _attach_execution_masks(panel)
    oos = list(dates[10:])
    signal_pos = 2
    signal_date = oos[signal_pos]
    execution_date = oos[signal_pos + 1]
    accepted, cancelled = panel.codes[:2]
    replacement = panel.codes[-1]
    factor = _single_signal_factor(
        oos, panel.codes, [accepted, cancelled], signal_pos=signal_pos,
    )
    panel.open_.loc[execution_date, cancelled] = 10.05
    panel.adj_open.loc[execution_date, cancelled] = 10.05

    result = run_backtest(
        panel, factor, oos, hold=5, q=5,
        costs={
            "capital": 2_000_000, "n_positions": 2, "slippage": 0.0,
            "entry_rule": "previous_close_limit_no_chase_v1",
        },
        capture_audit=True,
    )

    assert result["ok"]
    rows = {
        row["code"]: row for row in result["audit"]["signals"]
        if row["signal_date"] == signal_date.date().isoformat()
    }
    assert set(rows) == {accepted, cancelled}
    assert rows[accepted]["status"] == "selected"
    assert rows[accepted]["fill_price"] == pytest.approx(10.0)
    assert rows[cancelled]["status"] == "unfilled"
    assert rows[cancelled]["reason"] == "previous_close_limit_not_filled"
    assert rows[cancelled]["limit_price"] == pytest.approx(10.0)
    assert rows[cancelled]["execution_open"] == pytest.approx(10.05)
    assert replacement not in rows
    assert not [
        trade for trade in result["audit"]["trades"]
        if trade["code"] in {cancelled, replacement}
    ]
    accepted_trade = next(
        trade for trade in result["audit"]["trades"]
        if trade["code"] == accepted and trade["side"] == "buy"
    )
    assert accepted_trade["date"] == execution_date.date().isoformat()
    accepted_exit = [
        trade for trade in result["audit"]["trades"]
        if trade["code"] == accepted and trade["side"] == "sell"
    ][-1]
    assert accepted_exit["date"] == oos[
        signal_pos + 5 + 1
    ].date().isoformat()
    # 原始 2 名委托各占 cohort 的一半；取消部分留现金。
    target = next(
        row for row in result["audit"]["targets"]
        if row["date"] == execution_date.date().isoformat()
        and row["code"] == accepted
    )
    assert target["cohort_target_weight"] == pytest.approx(0.1)
    assert result["previous_close_limit_cancelled_count"] == 1
    assert result["entry_order_count"] == 2
    assert result["entry_order_filled_count"] == 1
    assert result["entry_order_fill_rate"] == pytest.approx(0.5)
    params = result["audit"]["portfolio_construction"]["entry_rule_parameters"]
    assert params["cancel_policy"] == "no_replacement_no_T_plus_2_retry"


def test_previous_close_gap_2pct_only_cancels_above_frozen_gap_cap():
    """2% 阈值是单一预注册参数；边界可成交，超过才取消。"""
    panel, dates = _flat_execution_fixture(n_dates=80)
    _attach_execution_masks(panel)
    oos = list(dates[10:])
    signal_pos = 2
    signal_date = oos[signal_pos]
    execution_date = oos[signal_pos + 1]
    below, boundary, above = panel.codes[:3]
    factor = _single_signal_factor(
        oos, panel.codes, [below, boundary, above], signal_pos=signal_pos,
    )
    panel.open_.loc[execution_date, [below, boundary, above]] = [10.19, 10.20, 10.21]
    panel.adj_open.loc[execution_date, [below, boundary, above]] = [10.19, 10.20, 10.21]

    result = run_backtest(
        panel, factor, oos, hold=5, q=5,
        costs={
            "capital": 2_000_000, "n_positions": 3, "slippage": 0.0,
            "entry_rule": "previous_close_gap_2pct_no_chase_v1",
        },
        capture_audit=True,
    )

    assert result["ok"]
    rows = {
        row["code"]: row for row in result["audit"]["signals"]
        if row["signal_date"] == signal_date.date().isoformat()
    }
    assert rows[below]["status"] == "selected"
    assert rows[boundary]["status"] == "selected"
    assert rows[above]["status"] == "unfilled"
    assert rows[above]["reason"] == "previous_close_gap_2pct_not_filled"
    assert rows[above]["limit_price"] == pytest.approx(10.20)
    assert rows[above]["limit_price_source"] == "signal_day_raw_close_plus_2pct"
    assert rows[above]["signal_day_raw_close"] == pytest.approx(10.0)
    assert rows[above]["overnight_gap_bps"] == pytest.approx(210.0)
    assert result["previous_close_limit_cancelled_count"] == 1
    params = result["audit"]["portfolio_construction"]["entry_rule_parameters"]
    assert params["maximum_overnight_gap_bps"] == 200
    assert params["cancel_policy"] == "no_replacement_no_T_plus_2_retry"


def test_previous_close_limit_uses_raw_prices_across_adjustment_ratio():
    """限价和开盘判定必须同为 raw；adj close=20 不能让 raw 10.10 误成交。"""
    panel, dates = _flat_execution_fixture(n_dates=80)
    _attach_execution_masks(panel)
    oos = list(dates[10:])
    signal_pos = 2
    signal_date = oos[signal_pos]
    execution_date = oos[signal_pos + 1]
    gap_down, gap_up = panel.codes[:2]
    factor = _single_signal_factor(
        oos, panel.codes, [gap_down, gap_up], signal_pos=signal_pos,
    )
    # T 日复权比例=2：adj close 20 / ratio 2 = raw close 10。
    panel.adj_open.loc[signal_date, [gap_down, gap_up]] = 20.0
    panel.close.loc[signal_date, [gap_down, gap_up]] = 20.0
    panel.open_.loc[execution_date, [gap_down, gap_up]] = [9.90, 10.10]
    panel.adj_open.loc[execution_date, [gap_down, gap_up]] = [9.90, 10.10]

    result = run_backtest(
        panel, factor, oos, hold=5, q=5,
        costs={
            "capital": 2_000_000, "n_positions": 2, "slippage": 0.0,
            "entry_rule": "previous_close_limit_no_chase_v1",
        },
        capture_audit=True,
    )

    assert result["ok"]
    rows = {row["code"]: row for row in result["audit"]["signals"]}
    assert rows[gap_down]["limit_price"] == pytest.approx(10.0)
    assert rows[gap_down]["execution_open"] == pytest.approx(9.90)
    assert rows[gap_down]["status"] == "selected"
    assert rows[gap_up]["limit_price"] == pytest.approx(10.0)
    assert rows[gap_up]["execution_open"] == pytest.approx(10.10)
    assert rows[gap_up]["reason"] == "previous_close_limit_not_filled"


def test_previous_close_limit_decision_does_not_read_future_prices():
    """T+1 开盘决定固定后，改 T+1 收盘/高低与 T+2 以后不改成交决定。"""
    panel, dates = _flat_execution_fixture(n_dates=80)
    _attach_execution_masks(panel)
    oos = list(dates[10:])
    signal_pos = 2
    execution_date = oos[signal_pos + 1]
    selected = panel.codes[:2]
    factor = _single_signal_factor(
        oos, panel.codes, selected, signal_pos=signal_pos,
    )
    panel.open_.loc[execution_date, selected] = 10.05
    panel.adj_open.loc[execution_date, selected] = 10.05
    baseline = run_backtest(
        panel, factor, oos, hold=5, q=5,
        costs={
            "capital": 2_000_000, "n_positions": 2, "slippage": 0.0,
            "entry_rule": "previous_close_limit_no_chase_v1",
        }, capture_audit=True,
    )

    panel.close.loc[execution_date:, selected] = np.linspace(
        1.0, 100.0, len(panel.close.loc[execution_date:]),
    )[:, None]
    panel.high.loc[execution_date:, selected] = 999.0
    panel.low.loc[execution_date:, selected] = 0.01
    changed = run_backtest(
        panel, factor, oos, hold=5, q=5,
        costs={
            "capital": 2_000_000, "n_positions": 2, "slippage": 0.0,
            "entry_rule": "previous_close_limit_no_chase_v1",
        }, capture_audit=True,
    )

    assert baseline["ok"] and changed["ok"]
    assert baseline["audit"]["signals"] == changed["audit"]["signals"]
    assert baseline["audit"]["unfilled"] == changed["audit"]["unfilled"]
    assert baseline["traded_notional"] == changed["traded_notional"] == 0.0


def test_previous_close_limit_preserves_st_suspension_and_limit_up_blocks():
    panel, dates = _flat_execution_fixture(n_dates=80)
    _attach_execution_masks(panel)
    oos = list(dates[10:])
    signal_pos = 2
    execution_date = oos[signal_pos + 1]
    st_code, suspended_code, limit_up_code, no_chase_code = panel.codes[:4]
    factor = _single_signal_factor(
        oos, panel.codes,
        [st_code, suspended_code, limit_up_code, no_chase_code],
        signal_pos=signal_pos,
    )
    panel.is_st_panel.loc[execution_date, st_code] = True
    panel.tradable.loc[execution_date, suspended_code] = False
    panel.open_.loc[execution_date, suspended_code] = np.nan
    panel.adj_open.loc[execution_date, suspended_code] = np.nan
    panel.open_.loc[execution_date, limit_up_code] = 11.00
    panel.adj_open.loc[execution_date, limit_up_code] = 11.00
    panel.open_.loc[execution_date, no_chase_code] = 10.05
    panel.adj_open.loc[execution_date, no_chase_code] = 10.05

    result = run_backtest(
        panel, factor, oos, hold=5, q=5,
        costs={
            "capital": 2_000_000, "n_positions": 4, "slippage": 0.0,
            "entry_rule": "previous_close_limit_no_chase_v1",
        }, capture_audit=True,
    )

    assert result["ok"]
    reasons = {row["code"]: row["reason"] for row in result["audit"]["signals"]}
    assert reasons == {
        st_code: "st_restricted",
        suspended_code: "suspended_or_untradable",
        limit_up_code: "limit_up",
        no_chase_code: "previous_close_limit_not_filled",
    }


def test_backtest_rejects_unknown_entry_rule():
    panel, dates = _flat_execution_fixture(n_dates=50)
    factor = _factor_with_selected_groups(
        list(dates[5:]), panel.codes, [panel.codes[:2]],
    )
    with pytest.raises(ValueError, match="买点规则"):
        run_backtest(
            panel, factor, list(dates[5:]),
            costs={"entry_rule": "profit_take_grid"},
        )


def test_rebal_buffer_retains_incumbents_without_exceeding_top_n():
    """N+B 是保留候选带，不是额外持仓名额。"""
    panel, dates = _flat_execution_fixture(n_dates=80, n_stocks=100)
    _attach_execution_masks(panel)
    oos = list(dates[10:])
    top_n = 10
    incumbents = list(panel.codes[:top_n])
    newcomers = list(panel.codes[top_n:2 * top_n])

    def ranked_row(order: list[str]) -> np.ndarray:
        values = pd.Series(
            np.arange(len(panel.codes), dtype=float), index=panel.codes,
        )
        for rank, code in enumerate(order):
            values.loc[code] = 1_000.0 - rank
        return values.to_numpy()

    rows = [ranked_row(incumbents)]
    rows.extend(
        ranked_row(newcomers + incumbents[:3]) for _ in oos[1:]
    )
    factor = pd.DataFrame(rows, index=oos, columns=panel.codes)
    common_costs = {
        "capital": 200_000, "n_positions": top_n, "slippage": 0.001,
    }
    baseline = run_backtest(
        panel, factor, oos, hold=5, q=5,
        costs={**common_costs, "rebal_buffer": 0}, capture_audit=True,
    )
    buffered = run_backtest(
        panel, factor, oos, hold=5, q=5,
        costs={**common_costs, "rebal_buffer": 3}, capture_audit=True,
    )

    assert baseline["ok"] and buffered["ok"]

    def selected_on(result: dict, day: pd.Timestamp) -> list[str]:
        day_text = day.date().isoformat()
        return [
            row["code"] for row in result["audit"]["signals"]
            if row["signal_date"] == day_text and row["status"] == "selected"
        ]

    assert set(selected_on(baseline, oos[1])) == set(newcomers)
    assert set(selected_on(buffered, oos[1])) == set(
        incumbents[:3] + newcomers[:7]
    )
    for result in (baseline, buffered):
        signal_days = {row["signal_date"] for row in result["audit"]["signals"]}
        assert all(len(selected_on(result, pd.Timestamp(day))) <= top_n
                   for day in signal_days)

    assert buffered["traded_notional"] < baseline["traded_notional"]
    assert buffered["total_fees"] < baseline["total_fees"]
    assert buffered["signal_churn"] < baseline["signal_churn"]


def test_market_vol_exposure_scale_is_causal_and_has_fixed_bounds():
    panel, dates = _flat_execution_fixture(n_dates=70)
    _attach_execution_masks(panel)

    def set_annualized_vol(target: float) -> None:
        # 20 个等量正负收益的样本标准差恰为目标日波动率。
        amplitude = target / np.sqrt(252.0) * np.sqrt(19.0 / 20.0)
        daily = np.zeros(len(dates), dtype=float)
        daily[1:] = np.where(np.arange(len(dates) - 1) % 2 == 0,
                             amplitude, -amplitude)
        path = 10.0 * np.cumprod(1.0 + daily)
        panel.close = pd.DataFrame(
            np.repeat(path[:, None], len(panel.codes), axis=1),
            index=dates, columns=panel.codes,
        )

    set_annualized_vol(0.30)
    scale, observed = _market_vol_exposure_scale(panel)
    assert observed.loc[dates[21]] == pytest.approx(0.30, rel=1e-10)
    assert scale.loc[dates[21]] == pytest.approx(0.60, rel=1e-10)
    assert float(scale.min()) >= 0.40 and float(scale.max()) <= 1.00

    frozen_prefix = scale.loc[:dates[40]].copy()
    panel.close.loc[dates[41]:] *= np.linspace(
        1.0, 2.0, len(dates) - 41,
    )[:, None]
    changed, _ = _market_vol_exposure_scale(panel)
    pd.testing.assert_series_equal(changed.loc[:dates[40]], frozen_prefix)

    set_annualized_vol(0.10)
    low_scale, low_observed = _market_vol_exposure_scale(panel)
    assert low_observed.loc[dates[21]] == pytest.approx(0.10, rel=1e-10)
    assert low_scale.loc[dates[21]] == pytest.approx(1.00)


def test_market_vol_target_reduces_real_position_and_records_sell_cost(
    monkeypatch,
):
    panel, dates = _flat_execution_fixture(n_dates=85, n_stocks=40)
    _attach_execution_masks(panel)
    oos = list(dates[10:])
    factor = _factor_with_selected_groups(oos, panel.codes, [panel.codes[:8]])
    scale = pd.Series(1.0, index=dates, dtype=float)
    scale.loc[dates[40]:] = 0.60
    observed = pd.Series(0.10, index=dates, dtype=float)
    observed.loc[dates[40]:] = 0.30
    monkeypatch.setattr(
        backtest_module, "_market_vol_exposure_scale",
        lambda _panel: (scale.copy(), observed.copy()),
    )
    common = {
        "capital": 2_000_000, "n_positions": 8, "slippage": 0.001,
        "rebal_buffer": 0,
    }
    baseline = run_backtest(
        panel, factor, oos, hold=5, q=5,
        costs={**common, "exposure_rule": "none"}, capture_audit=True,
    )
    scaled = run_backtest(
        panel, factor, oos, hold=5, q=5,
        costs={**common, "exposure_rule": "market_vol_target_18_v1"},
        capture_audit=True,
    )

    assert baseline["ok"] and scaled["ok"]
    assert scaled["average_actual_gross_exposure"] < baseline[
        "average_actual_gross_exposure"
    ]
    assert scaled["average_cash_weight"] > baseline["average_cash_weight"]
    drop_day = dates[40].date().isoformat()
    row = next(day for day in scaled["audit"]["days"] if day["date"] == drop_day)
    assert row["exposure_scale"] == pytest.approx(0.60)
    assert row["desired_target_gross_exposure"] == pytest.approx(0.60)
    assert row["actual_gross_exposure"] <= 0.61
    assert row["sell_notional"] > 0
    assert row["fees"]["total"] > 0
    assert scaled["audit"]["portfolio_construction"]["exposure_rule"] == (
        "market_vol_target_18_v1"
    )
    assert scaled["audit"]["summary"]["minimum_cash_weight"] > 0


def test_old_carry_and_new_expiry_are_both_checked_on_same_day():
    """旧跌停 carry 不能让当日新到期持仓跳过卖出检查。"""
    panel, dates = _flat_execution_fixture(n_dates=70)
    oos = list(dates[5:])
    group_a = panel.codes[:8]
    group_b = panel.codes[8:16]
    group_c = panel.codes[16:24]
    group_d = panel.codes[24:32]
    factor = _factor_with_selected_groups(oos, panel.codes,
                                          [group_a, group_b, group_c, group_d])

    # hold=2 使 A/B 在 d2 曾同时真实持有；d3 A 到期跌停成为 carry；
    # d4 旧 A 仍跌停，同日 B 新到期也跌停。raw close 保持 10。
    d3 = dates[8]
    d4 = dates[9]
    panel.open_.loc[d3, group_a] = 9.0
    panel.adj_open.loc[d3, group_a] = 9.0
    panel.open_.loc[d4, group_a + group_b] = 9.0
    panel.adj_open.loc[d4, group_a + group_b] = 9.0

    result = run_backtest(
        panel, factor, oos, hold=2, q=5,
        costs={"capital": 200_000, "n_positions": 8, "slippage": 0.001},
        capture_audit=True,
    )
    assert result["ok"]
    on_d4 = [row for row in result["audit"]["unfilled"]
             if row["date"] == d4.date().isoformat() and row["side"] == "sell"
             and row["reason"] == "limit_down"]
    assert {row["source"] for row in on_d4} >= {"old_carry", "new_expiry"}
    assert set(group_a) <= {row["code"] for row in on_d4 if row["source"] == "old_carry"}
    assert set(group_b) <= {row["code"] for row in on_d4 if row["source"] == "new_expiry"}


def test_capture_audit_is_json_serializable_and_hash_reproducible():
    panel, dates = _flat_execution_fixture()
    oos = list(dates[10:])
    factor = _factor_with_selected_groups(oos, panel.codes, [panel.codes[-8:]])
    result = run_backtest(
        panel, factor, oos, hold=5, q=5,
        costs={"capital": 200_000, "n_positions": 8, "slippage": 0.001},
        capture_audit=True,
    )

    audit = result["audit"]
    encoded = json.dumps(audit, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    assert hashlib.sha256(encoded).hexdigest() == result["audit_sha256"]
    assert audit["signals"] and audit["targets"] and audit["trades"] and audit["days"]
    assert audit["summary"]["total_fees"] == pytest.approx(result["total_fees"])
    assert max(pd.Timestamp(row["next_date"]) for row in audit["days"]) <= pd.Timestamp(oos[-1])
    assert max(pd.Timestamp(row["execution_date"]) for row in audit["signals"]) <= pd.Timestamp(oos[-1])
    # 账本逐笔费用与 quantqc 单边口径独立复核，买入无印花税。
    first_trade = audit["trades"][0]
    expected = first_trade["notional"] * qc.one_side_cost_rate(
        first_trade["notional"], first_trade["side"], 0.001)
    assert first_trade["total"] == pytest.approx(expected)
    if first_trade["side"] == "buy":
        assert first_trade["stamp"] == 0.0


def test_suspended_holding_uses_last_valuation_cannot_trade_and_audit_has_no_nan():
    panel, dates = _flat_execution_fixture(n_dates=75)
    _attach_execution_masks(panel)
    oos = list(dates[5:])
    selected = panel.codes[-8:]
    factor = _factor_with_selected_groups(oos, panel.codes, [selected])
    suspended_date = dates[20]
    code = selected[0]
    panel.signal_eligible.loc[suspended_date, code] = False
    panel.tradable.loc[suspended_date, code] = False
    panel.open_.loc[suspended_date, code] = np.nan
    panel.adj_open.loc[suspended_date, code] = np.nan

    result = run_backtest(
        panel, factor, oos, hold=5, q=5,
        costs={"capital": 200_000, "n_positions": 8, "slippage": 0.001},
        capture_audit=True,
    )
    assert result["ok"]
    # 严格 JSON + 哈希必须成功，停牌仓位不能从 NAV 中消失。
    json.dumps(result["audit"], ensure_ascii=False, allow_nan=False)
    rows = {row["date"]: row for row in result["audit"]["days"]}
    event = rows[suspended_date.date().isoformat()]
    previous = rows[dates[19].date().isoformat()]
    assert event["nav_before"] == pytest.approx(previous["nav_next"], abs=1e-8)
    holding = next(row for row in event["holdings"] if row["code"] == code)
    assert holding["valuation_price"] == pytest.approx(10.0)
    assert holding["valuation_source"] == "last_available_adj_open"
    assert not [row for row in result["audit"]["trades"]
                if row["date"] == suspended_date.date().isoformat() and row["code"] == code]


def test_st_cannot_enter_and_existing_st_exit_uses_five_percent_limit():
    panel, dates = _flat_execution_fixture(n_dates=75)
    _attach_execution_masks(panel)
    oos = list(dates[5:])
    selected = panel.codes[-8:]
    factor = _factor_with_selected_groups(oos, panel.codes, [selected])
    st_signal_date = dates[7]
    st_exit_date = dates[8]
    panel.is_st_panel.loc[st_signal_date:, selected] = True
    panel.signal_eligible.loc[st_signal_date:, selected] = False
    # d7 开始 ST；hold=2 使 d5 的正常建仓仍在 d7 持有，d8 到期时精确触及5%跌停。
    panel.open_.loc[st_exit_date, selected] = 9.50
    panel.adj_open.loc[st_exit_date, selected] = 9.50

    result = run_backtest(
        panel, factor, oos, hold=2, q=5,
        costs={"capital": 200_000, "n_positions": 8, "slippage": 0.001},
        capture_audit=True,
    )
    assert result["ok"]
    late_signals = [row for row in result["audit"]["signals"]
                    if row["signal_date"] >= st_signal_date.date().isoformat()]
    assert set(selected).isdisjoint({row["code"] for row in late_signals})
    blocked = [row for row in result["audit"]["unfilled"]
               if row["date"] == st_exit_date.date().isoformat()
               and row["side"] == "sell" and row["reason"] == "limit_down"]
    assert set(selected) <= {row["code"] for row in blocked}
    assert all(row["limit_pct"] == pytest.approx(0.05) for row in blocked)


def test_delisted_holding_is_written_off_after_last_date_with_audit_event():
    panel, dates = _flat_execution_fixture(n_dates=75)
    _attach_execution_masks(panel)
    oos = list(dates[5:])
    delisted = panel.codes[-8:]
    replacement = panel.codes[:8]
    factor = _factor_with_selected_groups(oos, panel.codes, [delisted, replacement])
    last_date = dates[6]
    writeoff_date = dates[7]
    panel.is_delisted_panel.loc[delisted] = True
    panel.last_date.loc[delisted] = last_date
    panel.signal_eligible.loc[writeoff_date:, delisted] = False
    panel.tradable.loc[writeoff_date:, delisted] = False
    panel.open_.loc[writeoff_date:, delisted] = np.nan
    panel.adj_open.loc[writeoff_date:, delisted] = np.nan
    panel.close.loc[writeoff_date:, delisted] = np.nan

    result = run_backtest(
        panel, factor, oos, hold=5, q=5,
        costs={"capital": 200_000, "n_positions": 8, "slippage": 0.001},
        capture_audit=True,
    )
    assert result["ok"]
    json.dumps(result["audit"], ensure_ascii=False, allow_nan=False)
    events = [row for row in result["audit"]["events"]
              if row["event"] == "delisted_writeoff"
              and row["date"] == writeoff_date.date().isoformat()]
    assert set(delisted) == {row["code"] for row in events}
    assert all(row["last_date"] == last_date.date().isoformat() for row in events)
    assert all(row["recovery_rate"] == 0.0 and row["loss_notional"] > 0 for row in events)
    day = next(row for row in result["audit"]["days"]
               if row["date"] == writeoff_date.date().isoformat())
    assert day["delisted_writeoff_loss"] == pytest.approx(
        sum(row["loss_notional"] for row in events))
    assert set(delisted).isdisjoint({row["code"] for row in day["holdings"]})
    # 该股组只来自 1 个未到期 cohort，hold=5 时损失约为净值的 20%，
    # 不应因布尔并集后虚构满仓而显示 >50% 损失。
    assert -0.25 < day["net_return"] < -0.15


def test_corporate_action_raw_price_jump_does_not_create_false_loss():
    """未复权价格 2:1 跳变、复权价不变时，持仓净值不得凭空腰斩。"""
    panel, dates = _flat_execution_fixture(n_dates=90)
    # 110→55：每仓目标约 2.2 手→4.5 手，整手目标恰好 200→400，
    # 不会把除权日变成一次额外再平衡。
    panel.close.loc[:, :] = 110.0
    panel.open_.loc[:, :] = 110.0
    panel.adj_open.loc[:, :] = 110.0
    split_date = dates[45]
    panel.open_.loc[split_date:, :] = 55.0
    # The fictional split has an explicit execution-date adjusted limit reference.
    panel.limit_reference.loc[:, :] = 110.0
    panel.limit_reference.loc[split_date:, :] = 55.0
    # close/adj_open 保持 110：表示总收益复权路径无涨跌。

    oos = list(dates[10:])
    selected = panel.codes[-8:]
    factor = _factor_with_selected_groups(oos, panel.codes, [selected])
    result = run_backtest(
        panel, factor, oos, hold=5, q=5,
        costs={"capital": 200_000, "n_positions": 8, "slippage": 0.0},
        capture_audit=True,
    )
    assert result["ok"]
    rows = {row["date"]: row for row in result["audit"]["days"]}
    event = rows[split_date.date().isoformat()]
    previous_date = dates[44].date().isoformat()
    previous = rows[previous_date]

    assert event["nav_before"] == pytest.approx(previous["nav_next"], abs=1e-8)
    assert event["gross_return"] == pytest.approx(0.0, abs=1e-12)
    assert event["net_return"] == pytest.approx(0.0, abs=1e-12)
    assert not [trade for trade in result["audit"]["trades"]
                if trade["date"] == split_date.date().isoformat()]
    before_holding = {row["code"]: row for row in previous["holdings"]}
    event_holding = {row["code"]: row for row in event["holdings"]}
    for code in selected:
        assert event_holding[code]["synthetic_share_equivalent"] == pytest.approx(
            before_holding[code]["synthetic_share_equivalent"] * 2)
        assert event_holding[code]["adjusted_units"] == pytest.approx(
            before_holding[code]["adjusted_units"])
        assert event_holding[code]["notional"] == pytest.approx(before_holding[code]["notional"])


# ── 5. 涨跌停可交易性（买/卖两侧，主板 10% / 双创 20%）───────────
def test_entry_unbuyable_limit_up_10pct():
    idx = pd.Index(["600001", "600002", "600003"])
    prev = pd.Series([10.0, 10.0, 10.0], index=idx)
    entry = pd.Series([11.00, 10.99, 9.50], index=idx)
    unbuy = _entry_unbuyable(
        entry, prev, idx, trade_date=pd.Timestamp("2025-01-02"),
    )
    assert bool(unbuy["600001"]) is True
    assert bool(unbuy["600002"]) is False
    assert bool(unbuy["600003"]) is False


def test_entry_unbuyable_chi_board_20pct():
    idx = pd.Index(["300001", "688001"])
    prev = pd.Series([10.0, 10.0], index=idx)
    entry = pd.Series([12.00, 11.99], index=idx)
    unbuy = _entry_unbuyable(
        entry, prev, idx, trade_date=pd.Timestamp("2025-01-02"),
    )
    assert bool(unbuy["300001"]) is True
    assert bool(unbuy["688001"]) is False


def test_exit_unsellable_limit_down():
    idx = pd.Index(["600001", "600002", "600003"])
    prev = pd.Series([10.0, 10.0, 10.0], index=idx)
    exit_open = pd.Series([9.00, 9.01, 8.99], index=idx)
    stuck = _exit_unsellable(
        exit_open, prev, idx, trade_date=pd.Timestamp("2025-01-02"),
    )
    assert bool(stuck["600001"]) is True
    assert bool(stuck["600002"]) is False
    assert bool(stuck["600003"]) is True


# ── 6. L2 walk-forward（前移多折，多数折同向 + IC_IR≥0.3）─────────
def test_l2_walkforward_majority_gate():
    n_dates, n_stocks = 220, 40
    panel, dates = _mk_panel(n_dates=n_dates, n_stocks=n_stocks, seed=21)
    oos = list(dates[20:])                              # 200 个 OOS 日 → 4 折 × 40 日
    hold = 10
    rng = np.random.default_rng(5)
    fwd = panel.close.shift(-hold) / panel.close - 1.0
    noise = rng.normal(0, 0.15, (len(oos), n_stocks))
    fac = fwd.loc[oos].values + noise
    factor_pos = pd.DataFrame(fac, index=oos, columns=panel.codes)
    res = l2_walkforward_forward(factor_pos, fwd, oos, is_direction=1.0, is_ic_mean=0.05)
    assert res["passed"] is True
    assert res["fold_pass"]["n_folds"] >= 3             # 确实切成多折
    assert res["fold_pass"]["gate_ok"] is True

    factor_neg = pd.DataFrame(-fac, index=oos, columns=panel.codes)
    res2 = l2_walkforward_forward(factor_neg, fwd, oos, is_direction=1.0, is_ic_mean=0.05)
    assert res2["passed"] is False                       # 方向相反 → 拒绝


def test_l2_enforces_configured_oos_ic_degrade(monkeypatch):
    import factor_system.validate as validate_module

    dates = list(pd.bdate_range("2024-01-02", periods=250))
    factor = pd.DataFrame(index=dates)
    fwd = pd.DataFrame(index=dates)
    monkeypatch.setattr(
        validate_module, "cross_sectional_ic",
        lambda _factor, _fwd, subset: pd.Series(0.024, index=subset),
    )
    monkeypatch.setattr(
        validate_module, "ic_stats",
        lambda series, hold=1: {
            "ic_mean": float(series.iloc[0]), "ic_ir": 0.6,
            "n_dates": len(series), "n_effective": len(series),
            "p_value": 0.01, "p_value_method": "fixture",
        },
    )

    rejected = l2_walkforward_forward(
        factor, fwd, dates, is_direction=1.0, is_ic_mean=0.05,
    )
    passed = l2_walkforward_forward(
        factor, fwd, dates, is_direction=1.0, is_ic_mean=0.048,
    )
    assert rejected["ic_retention"] == pytest.approx(0.48)
    assert rejected["minimum_ic_retention"] == pytest.approx(0.50)
    assert rejected["ic_retention_ok"] is False and rejected["passed"] is False
    assert passed["ic_retention_ok"] is True and passed["passed"] is True


def test_walkforward_validation_folds_never_overlap():
    """真实 OOS 长度 368 日时，旧 ceil+末折回拉会让最后两折重叠 2 日。"""
    from quantqc.stats import walk_forward_folds

    dates = list(pd.date_range("2024-07-01", periods=368, freq="B"))
    folds = walk_forward_folds(dates, n_folds=5)

    assert len(folds) == 5
    seen: set[pd.Timestamp] = set()
    for fold in folds:
        lo, hi = map(pd.Timestamp, fold["val"])
        current = {d for d in dates if lo <= d <= hi}
        assert current
        assert seen.isdisjoint(current)
        train_hi = pd.Timestamp(fold["train"][1])
        assert train_hi < lo
        seen.update(current)


def test_online_lond_and_dependent_batch_fdr_are_conservative():
    from quantqc.stats import bh_fdr, by_fdr, online_lond_alpha

    first = online_lond_alpha(1, discoveries_before=0, q=0.10)
    later = online_lond_alpha(20, discoveries_before=0, q=0.10)
    rewarded = online_lond_alpha(20, discoveries_before=3, q=0.10)
    independent = online_lond_alpha(20, discoveries_before=0, q=0.10,
                                    dependent=False)
    assert 0 < later < rewarded < first < 0.10
    assert later < independent

    p_values = [0.001, 0.02, 0.04, 0.8]
    bh = bh_fdr(p_values, q=0.10)
    by = by_fdr(p_values, q=0.10)
    assert set(by["significant"]) <= set(bh["significant"])
    assert by["method"] == "BY"
    assert by["effective_q"] < by["target_q"]
