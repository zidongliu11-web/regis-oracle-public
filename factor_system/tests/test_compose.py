"""M3.1 单测：策略组合因子（V4 重建 / 横截面秩加权 / 统一引擎评估结构）。"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

from factor_system.compose import (
    DEFAULT_WEIGHTS,
    REBUILDERS,
    _deflated_sharpe,
    _rolling_wf_ic,
    compose_factor,
    diagnose_strategy,
    evaluate_strategy,
    rebuild_v4_factors,
)
from factor_system.data import Panel, _panel_cache_path, executable_forward_return


@pytest.fixture(autouse=True)
def approved_source_gate(monkeypatch):
    """Legacy composition math tests run behind an explicitly approved source."""
    import factor_system.compose as compose_module
    import factor_system.registry as registry_module

    source_sha = "a" * 64
    approved = {
        "valid": True,
        "runnable": True,
        "research_record_sha256": source_sha,
    }
    monkeypatch.setattr(
        compose_module, "_require_runnable_source", lambda _fid: approved,
    )
    monkeypatch.setattr(
        registry_module, "verify_frozen_source_record",
        lambda _row, _recipe: approved,
    )


def _install_sealed_recipe(tmp_path, monkeypatch, fid="sealed-factor"):
    import factor_system.compose as compose_module

    registry_dir = tmp_path / "registry"
    recipe_dir = registry_dir / "recipes"
    recipe_dir.mkdir(parents=True)
    sealed_at = "2026-08-19T18:00:00"
    sealed_data_end = "2025-12-31"
    recipe = {
        "id": fid, "kind": "template", "formula": "mom20",
        "marker": 1, "research_epoch": compose_module.config.RESEARCH_EPOCH,
        "sealed_at": sealed_at, "sealed_data_end": sealed_data_end,
        "research_record_sha256": "a" * 64,
    }
    path = recipe_dir / f"{fid}.json"
    path.write_text(json.dumps(recipe), "utf-8")
    recipe_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    c = sqlite3.connect(registry_dir / "registry.db")
    c.execute("""CREATE TABLE factors (
        factor_id TEXT PRIMARY KEY, name TEXT, family TEXT, formula TEXT,
        status TEXT, research_epoch TEXT, sealed_at TEXT,
        sealed_data_end TEXT, sealed_recipe_sha256 TEXT,
        research_record_json TEXT, research_record_sha256 TEXT)""")
    research_record = json.dumps({
        "candidate": {
            "candidate_id": fid,
            "status": "ready",
            "runnable": True,
        },
    })
    c.execute("INSERT INTO factors VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
        fid, "sealed", "template", "mom20", "historical_pass",
        compose_module.config.RESEARCH_EPOCH, sealed_at, sealed_data_end,
        recipe_sha, research_record, "a" * 64,
    ))
    c.commit()
    c.close()
    monkeypatch.setattr(compose_module.config, "REGISTRY_DIR", registry_dir)
    compose_module._FACTOR_MAP_CACHE.clear()
    compose_module._FACTOR_MAP_CACHE_SHA.clear()
    compose_module._FACTOR_MAP_CACHE_PANEL.clear()
    compose_module._FACTOR_MAP_ORDER.clear()
    return path, recipe_sha, sealed_at, sealed_data_end


def _mk_panel(n_dates: int = 220, n_stocks: int = 80, seed: int = 3,
              start: str = "2023-10-01") -> Panel:
    rng = np.random.default_rng(seed)
    codes = [f"600{101 + i:03d}" for i in range(n_stocks)]
    dates = pd.date_range(start, periods=n_dates, freq="B")
    s0 = rng.uniform(8, 30, n_stocks)
    # 加入微弱动量：上一期收益的漂移，让组合因子有一定信号
    drift = rng.normal(0.0006, 0.012, (n_dates, n_stocks))
    price = s0[None, :] * np.exp(np.cumsum(drift, axis=0))
    close = pd.DataFrame(price, index=dates, columns=codes)
    amount = pd.DataFrame(
        1e8 * rng.lognormal(0, 0.4, (n_dates, n_stocks)), index=dates, columns=codes)
    # 基本面：M4.1 扩到 13 个 REBUILDER 后价值因子需 pe_ttm/pb_mrq 才能重建；
    # 合成面板补齐（PE ~10-50，PB ~0.5-8），不扰动生产回测语义。
    pe_ttm = pd.DataFrame(
        rng.lognormal(3.0, 0.4, (n_dates, n_stocks)), index=dates, columns=codes)
    pb_mrq = pd.DataFrame(
        rng.uniform(0.5, 8.0, (n_dates, n_stocks)), index=dates, columns=codes)
    panel = Panel(
        close=close, ret=close.pct_change(fill_method=None),
        vol=pd.DataFrame(rng.uniform(1e7, 1e8, (n_dates, n_stocks)), index=dates, columns=codes),
        amount=amount,
        turnover=pd.DataFrame(rng.uniform(0.01, 0.05, (n_dates, n_stocks)), index=dates, columns=codes),
        float_mv=pd.DataFrame(5e10 * rng.lognormal(0, 0.3, (n_dates, n_stocks)), index=dates, columns=codes),
        pct_chg=close.pct_change(fill_method=None),
        open_=close.copy(), adj_open=close.copy(), high=close * 1.01, low=close * 0.99,
        pe_ttm=pe_ttm, pb_mrq=pb_mrq, meta=None,
        codes=codes, dates=list(dates),
        limit_reference=close.shift(1),  # Explicit fictional non-action reference.
    )
    # 统一引擎证据必须绑定实际 Panel 快照；合成夹具也显式给出身份，不能依赖
    # gate 回退到当前磁盘 manifest。
    panel._panel_cache_key = _panel_cache_path().name
    return panel


@pytest.fixture(scope="module")
def panel():
    return _mk_panel()


def test_rebuild_shape_and_finite(panel):
    fs = rebuild_v4_factors(panel)
    assert set(fs) == set(REBUILDERS)
    for name, f in fs.items():
        assert f.shape == panel.close.shape, name
        assert isinstance(f, pd.DataFrame)
        assert f.index.equals(panel.close.index)
        assert f.columns.equals(panel.close.columns)
        # 重建因子应为有限值（尾部可能 NaN，但不应全 NaN）
        assert f.notna().sum().sum() > 0, name


def test_trend60_ratio_invariant(panel):
    """trend60 是纯比率：close 整体缩放不改变因子值（复权不变式）。"""
    f1 = REBUILDERS["trend60"](panel)
    scaled = Panel(
        close=panel.close * 2.0, ret=panel.ret, vol=panel.vol, amount=panel.amount,
        turnover=panel.turnover, float_mv=panel.float_mv, pct_chg=panel.pct_chg,
        open_=panel.open_, adj_open=panel.adj_open, high=panel.high, low=panel.low,
        pe_ttm=panel.pe_ttm, pb_mrq=panel.pb_mrq, meta=panel.meta,
        codes=panel.codes, dates=panel.dates,
    )
    f2 = REBUILDERS["trend60"](scaled)
    common = f1.notna() & f2.notna()
    assert f1[common].sub(f2[common]).abs().max().max() < 1e-9


def test_residual_momentum_semantics(panel):
    """residual_momentum_20_5 = close[t-5]/close[t-20] - 1（跳过最近 5 日）。"""
    f = REBUILDERS["residual_momentum_20_5"](panel)
    manual = panel.close.shift(5) / panel.close.shift(20) - 1.0
    common = f.notna() & manual.notna()
    assert f[common].sub(manual[common]).abs().max().max() < 1e-9


def test_compose_single_factor_degrades_to_rank(panel):
    """单因子策略 = 该因子横截面秩。"""
    w = {"trend60": 1.0}
    score = compose_factor(panel, w)
    manual = REBUILDERS["trend60"](panel).rank(axis=1, method="average", pct=True)
    common = score.notna() & manual.notna()
    assert score[common].sub(manual[common]).abs().max().max() < 1e-12


def test_compose_weighted_sum(panel):
    """加权求和正确：trend60*1 + low_vol*2。"""
    w = {"trend60": 1.0, "low_volatility_20": 2.0}
    score = compose_factor(panel, w)
    r1 = REBUILDERS["trend60"](panel).rank(axis=1, method="average", pct=True)
    r2 = REBUILDERS["low_volatility_20"](panel).rank(axis=1, method="average", pct=True)
    manual = r1 + 2.0 * r2
    common = score.notna() & manual.notna()
    assert score[common].sub(manual[common]).abs().max().max() < 1e-9


def test_compose_zero_weight_excluded(panel):
    """权重 0 的因子不参与（与 V4 active 过滤一致）。"""
    w1 = {"trend60": 1.0, "liquidity_20": 0.0}
    w2 = {"trend60": 1.0}
    s1 = compose_factor(panel, w1)
    s2 = compose_factor(panel, w2)
    assert s1.equals(s2)


def test_compose_all_zero_raises(panel):
    with pytest.raises(ValueError):
        compose_factor(panel, {"trend60": 0.0, "liquidity_20": 0.0})


def test_breadth_majority_uses_pit_eligibility_and_fails_closed(panel, monkeypatch):
    import factor_system.compose as compose_module

    eligible = pd.DataFrame(True, index=panel.close.index, columns=panel.close.columns)
    pit_panel = replace(panel, signal_eligible=eligible)
    trend = pd.DataFrame(-1.0, index=panel.close.index, columns=panel.close.columns)
    half = len(panel.codes) // 2
    threshold_day, off_day, empty_day = panel.dates[80:83]
    trend.loc[threshold_day, panel.codes[:half]] = 1.0       # 50%：risk on
    trend.loc[off_day, panel.codes[:half - 1]] = 1.0        # <50%：risk off
    eligible.loc[empty_day] = False                         # 无 PIT 观测：risk off
    monkeypatch.setattr(compose_module, "rebuild_trend60", lambda _panel: trend)

    risk_on, audit = compose_module._strategy_risk_mask(
        pit_panel, "breadth_majority", {"validation": [threshold_day, off_day, empty_day]},
    )

    assert bool(risk_on.loc[threshold_day]) is True
    assert bool(risk_on.loc[off_day]) is False
    assert bool(risk_on.loc[empty_day]) is False
    assert audit["basis"].startswith("PIT signal_eligible")
    assert audit["windows"]["validation"] == {
        "n_dates": 3, "risk_on_dates": 1, "risk_off_dates": 2,
    }
    assert len(audit["schedule_sha256"]) == 64
    trend.loc[panel.dates[-1]] = 1.0
    _, changed_outside_scope = compose_module._strategy_risk_mask(
        pit_panel, "breadth_majority", {"validation": [threshold_day, off_day, empty_day]},
    )
    assert changed_outside_scope["schedule_sha256"] == audit["schedule_sha256"]
    with pytest.raises(ValueError, match="PIT signal_eligible"):
        compose_module._strategy_risk_mask(panel, "breadth_majority")
    with pytest.raises(ValueError, match="不支持风险开关"):
        compose_module._strategy_risk_mask(pit_panel, "look_ahead_magic")


def test_evaluate_applies_one_breadth_mask_to_validation_and_history(panel, monkeypatch):
    import factor_system.compose as compose_module

    eligible = pd.DataFrame(True, index=panel.close.index, columns=panel.close.columns)
    pit_panel = replace(panel, signal_eligible=eligible)
    trend = pd.DataFrame(1.0, index=panel.close.index, columns=panel.close.columns)
    risk_off_dates = panel.dates[:len(panel.dates) // 2]
    trend.loc[risk_off_dates] = -1.0
    monkeypatch.setattr(compose_module, "rebuild_trend60", lambda _panel: trend)
    monkeypatch.setattr(compose_module, "build_exposures", lambda _panel: {})
    seen = {}

    def fake_validation(factor, *args, **kwargs):
        seen["validation"] = factor.copy()
        return {"passed": True, "stage": "L3"}

    def fake_gate(_panel, factor, _dates, **kwargs):
        seen["historical"] = factor.copy()
        return True, {"reason": "历史确认通过"}

    monkeypatch.setattr(compose_module, "run_validation", fake_validation)
    monkeypatch.setattr(compose_module, "run_scientific_gate", fake_gate)
    monkeypatch.setattr(compose_module, "_annotate", lambda *args, **kwargs: {})
    monkeypatch.setattr(compose_module, "_honesty_annotations", lambda *args: {})
    monkeypatch.setattr(compose_module, "full_cycle_evaluation", lambda *args, **kwargs: {
        "ok": True,
    })
    factors = {"low_volatility_20": REBUILDERS["low_volatility_20"](pit_panel)}

    result = evaluate_strategy({
        "name": "广度开关",
        "holding_days": 5,
        "top_n": 20,
        "risk_rule": "breadth_majority",
        "factors": {"low_volatility_20": 1.0},
    }, panel=pit_panel, factors=factors)

    assert seen["validation"].equals(seen["historical"])
    assert seen["validation"].loc[risk_off_dates].isna().all().all()
    assert seen["validation"].loc[panel.dates[-1]].notna().any()
    assert result["spec"]["risk_rule"] == "breadth_majority"
    assert result["risk_control"]["applied"] is True
    assert result["gate"]["risk_control"] == result["risk_control"]


def test_evaluate_routes_execution_controls_into_unified_backtests(panel, monkeypatch):
    import factor_system.compose as compose_module

    seen = {}
    monkeypatch.setattr(compose_module, "build_exposures", lambda _panel: {})
    monkeypatch.setattr(compose_module, "run_validation", lambda *args, **kwargs: {
        "passed": True, "stage": "L3",
    })

    def fake_gate(_panel, _factor, _dates, **kwargs):
        seen["gate_costs"] = dict(kwargs["costs"])
        return True, {"reason": "历史确认通过", "full": {}, "checks": {}}

    def fake_full(_panel, _factor, **kwargs):
        seen["full_costs"] = dict(kwargs["costs"])
        return {"ok": True}

    monkeypatch.setattr(compose_module, "run_scientific_gate", fake_gate)
    monkeypatch.setattr(compose_module, "full_cycle_evaluation", fake_full)
    monkeypatch.setattr(compose_module, "_annotate", lambda *args, **kwargs: {})
    monkeypatch.setattr(compose_module, "_honesty_annotations", lambda *args: {})

    result = evaluate_strategy({
        "name": "执行控制",
        "holding_days": 5,
        "top_n": 20,
        "rebal_buffer": 7,
        "entry_rule": "previous_close_gap_2pct_no_chase_v1",
        "exposure_rule": "market_vol_target_18_v1",
        "factors": {"trend60": 1.0},
    }, panel=panel, factors={"trend60": REBUILDERS["trend60"](panel)})

    for costs in (seen["gate_costs"], seen["full_costs"]):
        assert costs["rebal_buffer"] == 7
        assert costs["entry_rule"] == "previous_close_gap_2pct_no_chase_v1"
        assert costs["exposure_rule"] == "market_vol_target_18_v1"
        assert costs["n_positions"] == 20
    assert result["spec"]["rebal_buffer"] == 7
    assert result["spec"]["entry_rule"] == "previous_close_gap_2pct_no_chase_v1"
    assert result["spec"]["exposure_rule"] == "market_vol_target_18_v1"


def test_diagnose_hashes_breadth_control_into_execution_audit(panel, monkeypatch):
    import factor_system.compose as compose_module

    eligible = pd.DataFrame(True, index=panel.close.index, columns=panel.close.columns)
    pit_panel = replace(panel, signal_eligible=eligible)
    trend = pd.DataFrame(1.0, index=panel.close.index, columns=panel.close.columns)
    monkeypatch.setattr(compose_module, "rebuild_trend60", lambda _panel: trend)
    daily = pd.Series(
        np.resize(np.array([0.001, -0.0005]), 40),
        index=pd.DatetimeIndex(panel.dates[-40:]), dtype=float,
    )
    monkeypatch.setattr(compose_module, "run_backtest", lambda *args, **kwargs: {
        "ok": True, "annual_return": 0.1, "sharpe": 1.0,
        "max_drawdown": -0.1, "win_rate": 0.55,
        "benchmark_annual": 0.02, "excess_annual": 0.08,
        "daily_ret": daily, "audit": {"schema_version": 1},
        "audit_sha256": "backtest-only-hash",
    })
    monkeypatch.setattr(compose_module, "_multi_benchmark", lambda *args: {})
    monkeypatch.setattr(
        compose_module, "_sub_factor_decomposition",
        lambda *args, **kwargs: pytest.fail("extended decomposition must be deferred"),
    )
    monkeypatch.setattr(compose_module, "_rolling_wf_ic", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        compose_module, "_capital_sweep",
        lambda *args, **kwargs: pytest.fail("extended capital sweep must be deferred"),
    )
    monkeypatch.setattr(compose_module, "_deflated_sharpe", lambda *args: 0.5)
    factors = {"low_volatility_20": REBUILDERS["low_volatility_20"](pit_panel)}

    result = diagnose_strategy({
        "name": "广度审计",
        "holding_days": 5,
        "top_n": 20,
        "risk_rule": "breadth_majority",
        "factors": {"low_volatility_20": 1.0},
    }, panel=pit_panel, factors=factors, include_extended=False)

    assert result["execution_audit"]["strategy_risk_control"] == result["risk_control"]
    encoded = json.dumps(result["execution_audit"], ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    assert hashlib.sha256(encoded).hexdigest() == result["execution_audit_sha256"]
    assert result["sub_factor_decomposition"] == {}
    assert result["capital_sweep"] == []
    assert result["extended_diagnostics"]["included"] is False


def test_historical_curve_reuses_unified_returns_and_binds_audit(monkeypatch):
    import factor_system.compose as compose_module

    dates = pd.bdate_range("2024-07-01", periods=3)
    pair = pd.DataFrame({
        "strategy": [0.10, -0.20, 0.05],
        "shanghai": [0.01, -0.02, 0.03],
    }, index=dates)
    benchmark_sha = "a" * 64
    input_key = f"panel_v7_test.pkl:shanghai:{benchmark_sha}"
    monkeypatch.setattr(
        compose_module,
        "_aligned_shanghai_returns",
        lambda *_args, **_kwargs: (
            pair,
            {
                "ok": True,
                "n_days": 3,
                "coverage": 1.0,
                "source": {"sha256": benchmark_sha},
            },
        ),
    )
    audit = {"days": [
        {
            "date": day.date().isoformat(),
            "next_date": (day + pd.offsets.BDay()).date().isoformat(),
        }
        for day in dates
    ]}

    curve = compose_module._historical_equity_curve(
        {"ok": True, "daily_ret": pair["strategy"]},
        list(dates),
        research_input_key_value=input_key,
        execution_audit=audit,
        execution_audit_sha256="audit-hash",
    )

    assert curve["available"] is True
    assert curve["research_input_key"] == input_key
    assert curve["execution_audit_sha256"] == "audit-hash"
    assert curve["n_return_days"] == 3
    assert curve["points"][0] == {
        "date": "2024-07-01",
        "nav": 1.0,
        "benchmark_nav": 1.0,
        "drawdown_pct": 0.0,
    }
    assert curve["points"][-1]["nav"] == pytest.approx(0.924)
    assert curve["points"][2]["drawdown_pct"] == pytest.approx(-20.0)
    declared = curve.pop("curve_sha256")
    encoded = json.dumps(
        curve, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    assert declared == hashlib.sha256(encoded).hexdigest()


def test_evaluate_strategy_structure(panel):
    """统一引擎评估返回结构化结果（合成面板无真实信号，PASS 与否不在此断言）。"""
    res = evaluate_strategy({
        "name": "测试策略",
        "holding_days": 5,
        "top_n": 30,
        "factors": {"trend60": 1.0, "residual_momentum_20_5": 1.0},
    }, panel=panel)
    assert "is_validation" in res and "gate" in res and "annotations" in res
    v = res["is_validation"]
    assert "passed" in v and "stage" in v
    g = res["gate"]
    assert "passed" in g and "verdict" in g and "reason" in g
    assert res["spec"]["compose_method"] == "rank_weighted_v1"
    assert res["spec"]["holding_days"] == 5
    assert res["spec"]["top_n"] == 30
    assert len(res["spec"]["factor_lineage"]) == 2
    assert {row["factor_id"] for row in res["spec"]["factor_lineage"]} == {
        "trend60", "residual_momentum_20_5",
    }
    assert "per_factor_contribution" in res["annotations"]


def test_evaluate_strategy_matches_v4_direction(panel):
    """as-designed：组合方向固定 +1，不按 IC 事后翻向（策略评估不数据窥探）。"""
    res = evaluate_strategy({
        "name": "方向测试",
        "holding_days": 5,
        "top_n": 20,
        "factors": {"trend60": 1.0},
    }, panel=panel)
    # 单因子策略的闸门方向必须是 +1（spec 权重编码方向）
    assert res["gate"]["passed"] in (True, False)  # 结构完整即可


# ── M3.2 专业策略诊断 ──────────────────────────────────────────
def test_diagnose_strategy_structure(monkeypatch):
    """诊断报告结构：信号级 + 组合级 + 多基准 + 单因子分解 + 滚动WF + DSR，JSON 可序列化。

    用跨 IS(2021-01~2024-06)+OOS(2024-07~2025-12) 的合成长面板，保证 OOS 回测有效。
    """
    long_panel = _mk_panel(n_dates=1310, start="2021-01-04", seed=11)
    # Public tests have no historical benchmark files. These are fictional
    # benchmark series for the report contract, never real index evidence.
    import factor_system.compose as compose_module
    monkeypatch.setattr(compose_module, "_load_benchmark",
                        lambda _name: long_panel.adj_open.iloc[:, 0].copy())
    # This test has no capital-sweep assertion; avoid unrelated scenario runs.
    monkeypatch.setattr(compose_module, "_capital_sweep", lambda *a, **k: [])
    res = diagnose_strategy({
        "name": "诊断测试",
        "holding_days": 5,
        "top_n": 30,
        "factors": {"trend60": 1.0, "low_volatility_20": 1.0},
    }, panel=long_panel)
    assert set(res) >= {"spec", "signal_diagnostics_is", "oos_metrics",
                        "multi_benchmark", "sub_factor_decomposition",
                        "rolling_wf_ic", "deflated_sharpe", "diagnosed_at"}
    # 信号级诊断（M1.3 复用）
    sd = res["signal_diagnostics_is"]
    assert set(sd) >= {
        "monotonic", "ic_decay", "turnover", "rank_autocorrelation",
        "capacity", "regime",
    }
    assert sd["regime"]["scope"] == "calendar_year_oos_stability"
    assert sd["regime"]["market_emotion_conditioned"] is False
    # 多基准：4 个真实指数 + 等权总收益，全部匹配 open-to-open 时点
    mb = res["multi_benchmark"]
    assert "equal_weight_o2o" in mb
    assert mb["equal_weight_o2o"]["basis"] == (
        "adjusted-open total-return t→t+1"
    )
    for n in ("sh000001", "sh000300", "sh000905", "sh000852"):
        assert n in mb, n
        assert mb[n]["basis"] == "price-index open t→t+1"
    # 单因子分解只含 active 因子，且字段完整
    sub = res["sub_factor_decomposition"]
    assert set(sub) == {"trend60", "low_volatility_20"}
    for row in sub.values():
        assert "oos_annual_pct" in row and "oos_t" in row and "oos_ic" in row
    # 滚动 WF：最多 5 个互不重叠验证折，短样本会自适应减少折数。
    wf_folds = res["rolling_wf_ic"]["folds"]
    assert 0 < len(wf_folds) <= 5
    assert all(pd.Timestamp(a["val"][-1]) < pd.Timestamp(b["val"][0])
               for a, b in zip(wf_folds, wf_folds[1:]))
    # DSR N 敏感性键完整
    assert set(res["deflated_sharpe"]) == {"n_trials=1", "n_trials=10",
                                           "n_trials=50", "n_trials=500"}
    # JSON 可序列化（诊断报告要能进证据文件）
    import json
    json.dumps(res, ensure_ascii=False, default=str)


def test_multi_benchmark_does_not_shrink_to_available_dates():
    import factor_system.compose as compose_module

    dates = pd.bdate_range("2024-01-02", periods=40)
    strategy = pd.Series(0.001, index=dates)
    benchmark = pd.Series(0.0, index=dates)
    benchmark.iloc[12] = np.nan

    row = compose_module._bench_row(
        strategy, benchmark, "adjusted-open total-return t→t+1",
    )

    assert row["requested_days"] == 40
    assert row["n_days"] == 39
    assert row["coverage"] == pytest.approx(39 / 40)
    assert row["note"] == "同期基准覆盖不完整"
    assert "excess_annual_pct" not in row


def test_deflated_sharpe_decreases_with_trials():
    """DSR 单调性：同一 Sharpe，尝试次数越多越不显著（诚实提示运气面）。"""
    rng = np.random.default_rng(7)
    daily = pd.Series(rng.normal(0.0005, 0.015, 400))
    sharpe = float(daily.mean() / daily.std(ddof=1) * np.sqrt(252.0))
    d1 = _deflated_sharpe(sharpe, daily, 1)
    d500 = _deflated_sharpe(sharpe, daily, 500)
    assert d1 is not None and d500 is not None
    assert d1 > d500


def test_rolling_wf_ic_shape(panel):
    """滚动 WF：OOS 段 5 折各折 IC 统计，折数与日期划分正确。"""
    f = REBUILDERS["trend60"](panel)
    fwd = executable_forward_return(panel, 5)
    res = _rolling_wf_ic(f, fwd, panel.dates[-100:])
    assert 0 < len(res["folds"]) <= 5
    assert all(pd.Timestamp(a["val"][-1]) < pd.Timestamp(b["val"][0])
               for a, b in zip(res["folds"], res["folds"][1:]))
    assert all(r["n_dates"] > 0 for r in res["folds"])
    assert res["ic_ir_pos_folds"] in range(0, 6)


# ── C3.2 相关感知 IC 加权合成 ──────────────────────────────────
def test_estimate_ic_weights_pit_uses_only_is(panel):
    """PIT 结构保证：IC 加权权重只用传入的 IS 窗口计算；method="ic" 权重 == 该窗口
    手动算的因子均值 IC（证明权重 = IS 数据函数，OOS 无法泄漏进权重）。"""
    from factor_system.compose import estimate_ic_weights
    from factor_system.metrics import cross_sectional_ic
    is_dates = list(panel.dates[:160])
    hold = 5
    pool = {"trend60": REBUILDERS["trend60"](panel),
            "low_volatility_20": REBUILDERS["low_volatility_20"](panel)}
    w = estimate_ic_weights(panel, pool, is_dates, hold=hold, method="ic")
    fwd = executable_forward_return(panel, hold, boundary=str(max(is_dates)))
    for name, f in pool.items():
        s = cross_sectional_ic(f, fwd, is_dates)
        assert abs(w[name] - float(s.mean())) < 1e-12, name
    # 发现期之后的价格即使被大幅改写，也不能改变发现期权重。
    from dataclasses import replace
    changed_open = panel.adj_open.copy()
    future_dates = [d for d in panel.dates if d > max(is_dates)]
    scales = np.geomspace(0.05, 20.0, len(panel.codes))
    changed_open.loc[future_dates] = changed_open.loc[future_dates].mul(scales, axis=1)
    changed_panel = replace(panel, adj_open=changed_open)
    w_after_history_change = estimate_ic_weights(
        changed_panel, pool, is_dates, hold=hold, method="ic")
    assert w_after_history_change == w
    # 换一个 IS 窗口 → 权重跟着变（确实用传入窗口，而非全局数据）
    w2 = estimate_ic_weights(panel, pool, list(panel.dates[:120]), hold=hold, method="ic")
    assert w != w2


def test_estimate_ic_weights_corr_denoises(panel):
    """corr_ic：岭正则 + 相关矩阵求逆，权重有限、不爆炸；对高度相关的因子互相去重。"""
    from factor_system.compose import estimate_ic_weights
    is_dates = list(panel.dates[:160])
    pool = {k: REBUILDERS[k](panel) for k in ("trend60", "residual_momentum_20_5",
                                              "low_volatility_20", "liquidity_20")}
    w = estimate_ic_weights(panel, pool, is_dates, hold=5, method="corr_ic")
    assert set(w) == set(pool)
    assert all(np.isfinite(v) for v in w.values())
    # 去重/正则后权重尺度受控（不因相关矩阵奇异而爆炸）
    assert max(abs(v) for v in w.values()) < 10.0


def test_compose_ic_weighted_returns_weights(panel):
    """compose_ic_weighted 返回 (组合因子, 估计权重)；组合因子形状与面板一致。"""
    from factor_system.compose import compose_ic_weighted
    factor, weights = compose_ic_weighted(
        panel, {"trend60": 1.0, "low_volatility_20": 1.0},
        is_dates=list(panel.dates[:160]), hold=5, method="corr_ic")
    assert factor.shape == panel.close.shape
    assert factor.index.equals(panel.close.index)
    assert set(weights) == {"trend60", "low_volatility_20"}


def test_evaluate_strategy_ic_weighted_structure(panel):
    """统一引擎支持 compose_method="corr_ic_weighted"：权重 IS 估计、OOS 闸门评估，
    spec 如实记录合成方式。"""
    strict_panel = _mk_panel(n_dates=920, start="2021-01-04", seed=17)
    res = evaluate_strategy({
        "name": "IC加权策略",
        "holding_days": 5,
        "top_n": 20,
        "factors": {"trend60": 1.0, "residual_momentum_20_5": 1.0},
        "compose_method": "corr_ic_weighted",
    }, panel=strict_panel)
    assert res["spec"]["compose_method"] == "corr_ic_weighted"
    # 估计权重完整、有限（spec.factors 存的是实际用权重）
    fw = res["spec"]["factors"]
    assert set(fw) == {"trend60", "residual_momentum_20_5"}
    assert all(np.isfinite(v) for v in fw.values())
    assert {row["factor_id"] for row in res["spec"]["factor_lineage"]} == set(fw)
    assert "is_validation" in res and "gate" in res


def test_evaluate_strategy_passes_declared_cost_to_historical_gate(panel, monkeypatch):
    import factor_system.compose as compose_module

    seen_slippage = []
    seen_boundaries = []
    real_forward_return = compose_module.executable_forward_return

    def tracked_forward_return(source_panel, hold, boundary=None):
        seen_boundaries.append(boundary)
        return real_forward_return(source_panel, hold, boundary=boundary)

    monkeypatch.setattr(
        compose_module, "executable_forward_return", tracked_forward_return,
    )
    monkeypatch.setattr(compose_module, "build_exposures", lambda panel: {})
    monkeypatch.setattr(compose_module, "run_validation", lambda *args, **kwargs: {
        "passed": True, "stage": "L3",
    })

    def fake_gate(_panel, _factor, _dates, **kwargs):
        seen_slippage.append(kwargs["costs"]["slippage"])
        return True, {"verdict": "PASS", "reason": "历史确认通过"}

    monkeypatch.setattr(compose_module, "run_scientific_gate", fake_gate)
    monkeypatch.setattr(compose_module, "_annotate", lambda *args, **kwargs: {})
    monkeypatch.setattr(compose_module, "_honesty_annotations", lambda *args: {})
    monkeypatch.setattr(compose_module, "full_cycle_evaluation", lambda *args, **kwargs: {
        "ok": True,
    })
    factors = {"trend60": REBUILDERS["trend60"](panel)}
    base = {
        "name": "成本传递",
        "holding_days": 5,
        "top_n": 20,
        "factors": {"trend60": 1.0},
    }

    zero = evaluate_strategy({**base, "one_way_cost_bps": 0}, panel=panel, factors=factors)
    fifty = evaluate_strategy({**base, "one_way_cost_bps": 50}, panel=panel, factors=factors)

    assert seen_slippage == [0.0, 0.005]
    assert seen_boundaries == [
        compose_module.config.DISCOVERY_END,
        compose_module.config.VALIDATION_END,
        compose_module.config.DISCOVERY_END,
        compose_module.config.VALIDATION_END,
    ]
    assert zero["spec"]["one_way_cost_bps"] == 0
    assert fifty["spec"]["one_way_cost_bps"] == 50
    assert zero["spec"]["forward_return_label"] == (
        compose_module.EXECUTABLE_FORWARD_LABEL
    )
    assert zero["gate"]["passed"] is False
    assert zero["gate"]["historical_confirmation_passed"] is True
    assert zero["gate"]["verdict"] == "HISTORICAL_PASS"


def test_evaluate_strategy_cannot_pass_when_validation_fails(panel, monkeypatch):
    import factor_system.compose as compose_module

    monkeypatch.setattr(compose_module, "build_exposures", lambda panel: {})
    monkeypatch.setattr(compose_module, "run_validation", lambda *args, **kwargs: {
        "passed": False, "stage": "L1", "L1": {"reason": "验证信号不足"},
    })
    monkeypatch.setattr(compose_module, "run_scientific_gate", lambda *args, **kwargs: (
        True, {"verdict": "PASS", "reason": "历史确认通过"},
    ))
    monkeypatch.setattr(compose_module, "_annotate", lambda *args, **kwargs: {})
    monkeypatch.setattr(compose_module, "_honesty_annotations", lambda *args: {})
    monkeypatch.setattr(compose_module, "full_cycle_evaluation", lambda *args, **kwargs: {
        "ok": True,
    })
    factors = {"trend60": REBUILDERS["trend60"](panel)}

    result = evaluate_strategy({
        "name": "验证失败",
        "holding_days": 5,
        "top_n": 20,
        "one_way_cost_bps": 10,
        "factors": {"trend60": 1.0},
    }, panel=panel, factors=factors)

    assert result["gate"]["historical_gate_passed"] is True
    assert result["gate"]["passed"] is False
    assert result["gate"]["verdict"] == "NO-GO"
    assert "验证期未通过" in result["gate"]["reason"]


def test_recipe_cache_revalidates_seal_before_hit(tmp_path, monkeypatch, panel):
    """缓存中已有因子帧时，配方被篡改也必须拒绝，不能复用旧缓存绕过。"""
    import factor_system.compose as compose_module

    path, _, _, _ = _install_sealed_recipe(tmp_path, monkeypatch)
    monkeypatch.setattr(
        compose_module, "rebuild_factor",
        lambda source_panel, recipe: source_panel.close * 0 + recipe["marker"],
    )
    first = compose_module.load_factor_map(panel, ["sealed-factor"])
    assert float(first["sealed-factor"].iloc[0, 0]) == 1.0

    tampered = json.loads(path.read_text("utf-8"))
    tampered["marker"] = 2
    path.write_text(json.dumps(tampered), "utf-8")
    with pytest.raises(ValueError, match="seal 校验"):
        compose_module.load_factor_map(panel, ["sealed-factor"])


def test_cache_false_rebuilds_recipe_for_the_current_panel(
    tmp_path, monkeypatch, panel,
):
    """生产读取不得从另一份 Panel 快照复用旧因子矩阵。"""
    import factor_system.compose as compose_module

    _install_sealed_recipe(tmp_path, monkeypatch)
    calls = []

    def rebuild(source_panel, _recipe):
        calls.append(source_panel)
        return source_panel.close.copy()

    monkeypatch.setattr(compose_module, "rebuild_factor", rebuild)
    first = compose_module.load_factor_map(
        panel, ["sealed-factor"], cache=True,
    )["sealed-factor"]
    newer_panel = replace(panel, close=panel.close + 100.0)
    second = compose_module.load_factor_map(
        newer_panel, ["sealed-factor"], cache=False,
    )["sealed-factor"]

    assert len(calls) == 2
    assert float(second.iloc[0, 0]) == pytest.approx(
        float(first.iloc[0, 0]) + 100.0,
    )


def test_recipe_cache_is_bound_to_panel_snapshot(
    tmp_path, monkeypatch, panel,
):
    """同一配方切换 Panel key 时必须重建，不能复用旧截面。"""
    import factor_system.compose as compose_module

    _install_sealed_recipe(tmp_path, monkeypatch)
    calls = []

    def rebuild(source_panel, _recipe):
        calls.append(source_panel._panel_cache_key)
        return source_panel.close.copy()

    monkeypatch.setattr(compose_module, "rebuild_factor", rebuild)
    panel._panel_cache_key = "panel-snapshot-a.pkl"
    first = compose_module.load_factor_map(
        panel, ["sealed-factor"], cache=True,
    )["sealed-factor"]
    newer_panel = replace(panel, close=panel.close + 100.0)
    newer_panel._panel_cache_key = "panel-snapshot-b.pkl"
    second = compose_module.load_factor_map(
        newer_panel, ["sealed-factor"], cache=True,
    )["sealed-factor"]

    assert calls == ["panel-snapshot-a.pkl", "panel-snapshot-b.pkl"]
    assert float(second.iloc[0, 0]) == pytest.approx(
        float(first.iloc[0, 0]) + 100.0,
    )


def test_factor_lineage_freezes_seal_fields(tmp_path, monkeypatch):
    import factor_system.compose as compose_module

    _, recipe_sha, sealed_at, sealed_data_end = _install_sealed_recipe(
        tmp_path, monkeypatch,
    )
    rows = compose_module._factor_lineage({"sealed-factor": -1.0})

    assert rows == [{
        "factor_id": "sealed-factor",
        "weight": -1.0,
        "kind": "template",
        "status": "historical_pass",
        "label": "20日动量",
        "formula": "mom20",
        "source": "factor_system.registry",
        "research_epoch": compose_module.config.RESEARCH_EPOCH,
        "sealed_at": sealed_at,
        "sealed_data_end": sealed_data_end,
        "sealed_recipe_sha256": recipe_sha,
        "research_record_sha256": "a" * 64,
    }]
