"""P2-4 科学部署闸门（替代 pipeline.run_backtesting 的弱门槛）。

闸门五端 + 显著性（2026-08-17 显著性口径修正，用户拍板）：
  a. OOS 通过 —— 全 OOS 回测净收益为正，且同时跑赢上证 open-to-open 价格指数
     与同窗口复权 open-to-open 全市场总收益基准（成本后）
  b. 全折通过率 ≥60% —— OOS 切 5 折 walk-forward，每折净收益为正且跑赢同段上证
  c. 扣除 A 股费用后净收益为正 —— 收益序列本身已含 quantqc 成本
  d. 风控达标 —— 最大回撤 ≥ -35%（不出现深坑）
  e. PIT/前视 —— 数据层已用复权价 + 时点构建；OOS IC 只用
     T+1 复权开盘入场→T+H+1 复权开盘退出的可执行收益复核
  显著性（2026-08-20 校准）—— 统计显著以 OOS IC 判定：IC 同向 +
    |IC_IR|≥0.3 + 最大相位/HAC 双侧 p≤0.05 + n≥60。
    原「组合层绝对收益 daily_t≥2」降级为报告项（ev.full.t_stat）：在 hold=10/OOS 370 天下，
    t=(mean/sd)·√N/√hold 需年化 Sharpe≥5.2，数学上不可达（最强实测 F2__n60 Sharpe 1.31、t=0.499），
    且绝对收益 t 被市场 beta 主导，测的并非「因子是否可靠跑赢上证」；跑赢上证已由
    组合层由同时跑赢上证与复权总收益基准、5 折通过率、net_annual>0、
    资金填仓率与 ADV 可执行性共同把关；HAC p 负责统计显著。

基准口径变更（2026-08-14，用户强制要求「所有策略必须强制跑赢上证基准」）：
  - 上证价格指数改为与策略相同的 open-to-open 时点；同时强制跑赢内部复权
    open-to-open 总收益基准，避免价格指数漏股息造成虚假超额。
  - 上证数据缺失/对齐样本不足 → 该检查 fail-closed（NO-GO），绝不静默跳过强制基准。
  - 复权等权全市场总收益基准是强制闸门（benchmark_annual / excess_annual）。

任一不满足 → NO-GO（rejected，绝不 set_production）。全部通过才可投产。
部署端多重检验（BH-FDR）在 batch 层对全体候选的 OOS IC HAC p 值统一校正。
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import config
from .backtest import run_backtest
from .data import (EXECUTABLE_FORWARD_LABEL, Panel, _panel_cache_path,
                   executable_forward_return, mask_signal_eligible)
from .diagnostics import pooled_monotonicity
from .metrics import cross_sectional_ic, ic_stats

from quantqc.stats import equal_folds, fold_pass_rate


def daily_t_stat(daily_ret: pd.Series, hold: int = 1) -> float:
    """日收益序列 t 统计（H0: 均值=0），|t|≥2 视为显著。

    P2-8 修正：重叠持仓（hold>1）的日收益序列强自相关，朴素 t 会高估显著性约 √hold 倍。
    按 hold 折减有效样本量（t_eff ≈ t_naive / √hold），诚实反映真实证据强度。
    """
    x = pd.Series(daily_ret).dropna()
    if len(x) < 10:
        return 0.0
    sd = x.std(ddof=1)
    if not np.isfinite(sd) or sd <= 0:
        return 0.0
    t = x.mean() / sd * np.sqrt(len(x))
    if hold and hold > 1:
        t = t / np.sqrt(hold)
    return float(t)


def daily_p_value(t_stat: float, n: int) -> float:
    """日收益 t 检验双侧 p 值（Student-t）。"""
    if not np.isfinite(t_stat) or n < 3:
        return 1.0
    from scipy.stats import t as tdist
    return float(2 * tdist.sf(abs(t_stat), df=max(1, n - 1)))


# ═══ 用户强制基准：上证价格指数（open-to-open）+ 复权总收益基准 ═══
_SHANGHAI_PATH = (Path(__file__).resolve().parent.parent
                  / "research" / "data" / "current" / "sh000001.csv")
_BENCHMARK_UNSET = object()


def _shanghai_series() -> pd.Series | None:
    """读 research/data/current/sh000001.csv → open 序列（DatetimeIndex）。"""
    try:
        if not _SHANGHAI_PATH.exists():
            return None
        df = pd.read_csv(_SHANGHAI_PATH, usecols=["date", "open"])
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date")["open"].astype(float)
    except Exception:
        return None


def _shanghai_source_evidence() -> dict:
    if not _SHANGHAI_PATH.is_file():
        return {"path": str(_SHANGHAI_PATH), "sha256": None, "size": None}
    digest = hashlib.sha256()
    with _SHANGHAI_PATH.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    try:
        source_path = str(_SHANGHAI_PATH.relative_to(config.PROJECT_ROOT))
    except ValueError:
        source_path = str(_SHANGHAI_PATH)
    return {
        "path": source_path,
        "sha256": digest.hexdigest(),
        "size": int(_SHANGHAI_PATH.stat().st_size),
    }


def _shanghai_snapshot() -> tuple[pd.Series | None, dict]:
    """一次读取得到基准序列与同一字节的 SHA，避免闸门内跨版本混用。"""
    if not _SHANGHAI_PATH.is_file():
        return None, {"path": str(_SHANGHAI_PATH), "sha256": None, "size": None}
    try:
        payload = _SHANGHAI_PATH.read_bytes()
        frame = pd.read_csv(io.BytesIO(payload), usecols=["date", "open"])
        frame["date"] = pd.to_datetime(frame["date"])
        series = frame.set_index("date")["open"].astype(float)
        try:
            source_path = str(_SHANGHAI_PATH.relative_to(config.PROJECT_ROOT))
        except ValueError:
            source_path = str(_SHANGHAI_PATH)
        return series, {
            "path": source_path,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
    except Exception as exc:
        return None, {
            "path": str(_SHANGHAI_PATH), "sha256": None, "size": None,
            "error": str(exc),
        }


def _input_key_from_benchmark_source(source: dict,
                                     panel_key: str | None = None) -> str:
    key = panel_key or _panel_cache_path().name
    return f"{key}:shanghai:{source.get('sha256') or 'missing'}"


def research_input_key(panel_key: str | None = None) -> str:
    """冻结历史 gate 的完整输入身份：实际股票面板 + 上证文件字节。

    ``panel_key`` 缺省时仍表示磁盘上当前 manifest；已经持有 ``Panel`` 的长
    进程必须传它实际加载的 ``_panel_cache_key``，避免数据更新瞬间把旧内存
    面板误贴成新 manifest。
    """
    source = _shanghai_source_evidence()
    return _input_key_from_benchmark_source(source, panel_key=panel_key)


def _aligned_shanghai_returns(strategy_daily: pd.Series, dates: list,
                              *, benchmark_series=_BENCHMARK_UNSET,
                              benchmark_source: dict | None = None):
    """严格对齐策略 ``t→t+1`` 收益与上证同一期 open 收益。"""
    source = benchmark_source or _shanghai_source_evidence()
    sh = (_shanghai_series() if benchmark_series is _BENCHMARK_UNSET
          else benchmark_series)
    if sh is None:
        return None, {"ok": False, "reason": "上证基准数据缺失", "source": source}
    strategy = pd.Series(strategy_daily, dtype=float).copy()
    strategy.index = pd.DatetimeIndex(strategy.index)
    requested = pd.DatetimeIndex(sorted(set(pd.to_datetime(dates))))
    strategy = strategy.loc[strategy.index.intersection(requested)].sort_index()
    if strategy.index.has_duplicates:
        return None, {"ok": False, "reason": "策略收益日期重复", "n_days": 0,
                      "source": source}
    if strategy.empty:
        return None, {"ok": False, "reason": "策略收益无可对齐日期", "n_days": 0,
                      "source": source}

    benchmark = pd.Series(sh, dtype=float).sort_index()
    benchmark.index = pd.DatetimeIndex(benchmark.index)
    if benchmark.index.has_duplicates:
        return None, {"ok": False, "reason": "上证基准日期重复", "n_days": 0,
                      "source": source}
    # 必须先在完整源序列上算 d 日开盘到下一交易日开盘的收益，再按策略收益
    # 的起始日 d 对齐。reindex 后再 pct_change 会错成 d-1→d，ffill 则会把
    # 越过数据尾部的前向终测伪造成 0% 基准收益。
    benchmark = benchmark.where(np.isfinite(benchmark) & (benchmark > 0))
    forward_oo = benchmark.shift(-1).div(benchmark).sub(1.0)
    aligned = forward_oo.reindex(strategy.index)
    valid = strategy.notna() & aligned.notna() & np.isfinite(strategy) & np.isfinite(aligned)
    n_requested = int(len(strategy))
    n_valid = int(valid.sum())
    coverage = n_valid / n_requested if n_requested else 0.0
    if n_valid < 30 or n_valid != n_requested:
        return None, {
            "ok": False,
            "reason": (
                f"上证同期对齐不足({n_valid}/{n_requested})；"
                "每个策略 open t→t+1 收益都必须有同日起点及下一交易日基准开盘"
            ),
            "n_days": n_valid,
            "requested_days": n_requested,
            "coverage": float(coverage),
            "source": source,
        }
    pair = pd.concat(
        [strategy.loc[valid], aligned.loc[valid]], axis=1,
        keys=["strategy", "shanghai"],
    )
    return pair, {
        "ok": True, "n_days": n_valid, "requested_days": n_requested,
        "coverage": float(coverage), "source": source,
    }


def _excess_vs_shanghai(strategy_daily: pd.Series, dates: list, *,
                        benchmark_series=_BENCHMARK_UNSET,
                        benchmark_source: dict | None = None) -> dict:
    """策略净日收益 vs 上证 open-to-open，同区间年化与超额。

    策略与上证价格指数使用同一开盘窗口；另由 run_backtest 的复权全市场
    benchmark_annual 提供同窗口总收益门槛。数据不足 → fail-closed。
    """
    both, alignment = _aligned_shanghai_returns(
        strategy_daily, dates, benchmark_series=benchmark_series,
        benchmark_source=benchmark_source,
    )
    if both is None:
        return alignment
    n = len(both)
    s_ann = (1.0 + both["strategy"]).prod() ** (252.0 / n) - 1.0
    b_ann = (1.0 + both["shanghai"]).prod() ** (252.0 / n) - 1.0
    return {
        **alignment,
        "n_days": n,
        "strategy_annual": float(s_ann),
        "shanghai_annual": float(b_ann),
        "excess_annual": float(s_ann - b_ann),
        "basis": "strategy=open t→t+1 · shanghai_price_index=open t→t+1",
    }


def full_cycle_evaluation(panel: Panel, factor: pd.DataFrame,
                          hold: int = config.HOLD_DAYS,
                          q: int = config.QUANTILE,
                          costs: dict | None = None,
                          direction: float = 1.0,
                          expected_input_key: str | None = None) -> dict:
    """全周期（面板全窗口 2021-01~2025-12）走查 vs 上证——研究视角，非投产闸门。

    与 OOS 科学闸门完全同引擎同口径（run_backtest + _excess_vs_shanghai），
    只把窗口换成整个面板。用途：诚实展示"全周期跑不跑赢上证"。

    诚实边界（2026-08-16）：
      - 非盲测：窗口包含 IS；因子方向按 A 股研究预先编码（M4.1 反转/小市值/低换手族），
        不事后翻向——仍是研究性对照，不是部署证据。
      - 投产判定只认 OOS 科学闸门；本函数只加注不替换门槛。
    """
    benchmark_series, benchmark_source = _shanghai_snapshot()
    panel_key = getattr(panel, "_panel_cache_key", None)
    if expected_input_key is not None and not panel_key:
        raise RuntimeError(
            "科学回测要求绑定实际 Panel 快照，但 Panel 缺少 _panel_cache_key"
        )
    actual_input_key = _input_key_from_benchmark_source(
        benchmark_source, panel_key,
    )
    if expected_input_key is not None and actual_input_key != expected_input_key:
        raise RuntimeError(
            "科学回测输入已变化，拒绝把新基准结果记入旧假设: "
            f"{expected_input_key} -> {actual_input_key}"
        )
    factor = mask_signal_eligible(panel, factor)

    # 2026+ 是从未参与研发的 forward shadow，任何策略构造/诊断都不能读取。
    # “全周期”固定指当前已污染的历史研究截止日，而不是随面板增长偷偷扩窗。
    historical_end = pd.Timestamp(config.HISTORICAL_TEST_END)
    full_dates = [d for d in panel.dates if pd.Timestamp(d) <= historical_end]
    bt = run_backtest(panel, factor, full_dates, hold=hold, q=q, costs=costs,
                      direction=direction)
    if not bt.get("ok"):
        return {"ok": False, "reason": bt.get("reason", "全周期回测失败")}
    n_days = int(bt.get("n_days", 0))
    sh = _excess_vs_shanghai(
        bt["daily_ret"], full_dates, benchmark_series=benchmark_series,
        benchmark_source=benchmark_source,
    )
    # 逐年分解（策略净年收益 vs 上证 open-to-open 价格指数）
    yearly: list[dict] = []
    pair, alignment = _aligned_shanghai_returns(
        bt["daily_ret"], full_dates, benchmark_series=benchmark_series,
        benchmark_source=benchmark_source,
    )
    if pair is not None and sh.get("ok") and alignment.get("ok"):
        for y, grp in pair.groupby(pair.index.year):
            if len(grp) < 20:
                continue
            s_ret = float(np.prod(1 + grp["strategy"].values) - 1)
            b_ret = float(np.prod(1 + grp["shanghai"].values) - 1)
            # 与 gate.full 同约定：小数（0.1448 = 14.48%），前端 fpct ×100
            yearly.append({
                "year": int(y),
                "strategy_annual": _r(s_ret),
                "shanghai_annual": _r(b_ret),
                "excess_annual": _r(s_ret - b_ret),
            })
    return {
        "ok": True,
        "annual_return": _r(bt.get("annual_return")),
        "sharpe": _r(bt.get("sharpe")),
        "max_drawdown": _r(bt.get("max_drawdown")),
        "calmar": _r(bt.get("calmar")),
        "realized_volatility": _r(bt.get("realized_volatility")),
        "average_actual_gross_exposure": _r(
            bt.get("average_actual_gross_exposure")
        ),
        "average_cash_weight": _r(bt.get("average_cash_weight")),
        "minimum_cash_weight": _r(bt.get("minimum_cash_weight")),
        "maximum_single_weight": _r(bt.get("maximum_single_weight")),
        "win_rate": _r(bt.get("win_rate")),
        "entry_rule": str(bt.get("entry_rule", "none")),
        "entry_order_fill_rate": _r(bt.get("entry_order_fill_rate")),
        "entry_order_count": int(bt.get("entry_order_count", 0)),
        "entry_order_cancelled_count": int(
            bt.get("entry_order_cancelled_count", 0)
        ),
        "previous_close_limit_cancelled_count": int(
            bt.get("previous_close_limit_cancelled_count", 0)
        ),
        "n_days": n_days,
        "shanghai_annual": _r(sh.get("shanghai_annual")),
        "shanghai_excess_annual": _r(sh.get("excess_annual")),
        "basis": sh.get("basis"),
        "source": sh.get("source"),
        "research_input_key": actual_input_key,
        "yearly": yearly,
        "window": (f"{pd.Timestamp(full_dates[0]).date()}~"
                   f"{pd.Timestamp(full_dates[-1]).date()}" if full_dates else None),
        "note": ("全周期走查（含 IS+OOS），非盲测；因子方向按 A 股研究预先编码，"
                 "不事后翻向。投产仍以 OOS 科学闸门为准。"),
    }


def _capital_deployable(bt: dict, n_positions: int) -> bool:
    """整手后仍能按目标分散部署；避免名义 Top-N 实际大额闲置/单名集中。"""
    n_positions = max(1, int(n_positions))
    return bool(
        float(bt.get("gross_exposure_fill_rate", 0.0) or 0.0) >= 0.80
        and float(bt.get("lot_target_fill_rate", 0.0) or 0.0) >= 0.80
        and float(bt.get("average_funded_positions", 0.0) or 0.0)
        >= min(5.0, n_positions * 0.5)
        and float(bt.get("maximum_single_weight", 1.0))
        <= max(0.20, 2.0 / n_positions)
    )


def run_scientific_gate(panel: Panel, factor: pd.DataFrame,
                        oos_dates: list[pd.Timestamp],
                        hold: int = config.HOLD_DAYS,
                        q: int = config.QUANTILE,
                        costs: dict | None = None,
                        is_direction: float = 1.0,
                        capture_audit: bool = False,
                        expected_input_key: str | None = None) -> tuple[bool, dict]:
    """返回 (passed, evidence)。evidence 为可序列化 dict，存 registry.backtest_evidence。

    2026-08-17 改动：
      1. 组合层「绝对收益 daily_t≥2」只作报告项（ev.full.t_stat）。
         原检查在 hold=10/OOS 370 天下需 Sharpe≥5.2，数学不可达；绝对收益 t 被 beta
         主导，测的不是「因子是否可靠跑赢上证」。显著性改由 oos_ic_significant 判定
         （IC 同向 + |IC_IR|≥0.3 + 最大相位/HAC p≤0.05），组合层经济性由 net_annual>0 +
         beats_shanghai + beats_matched_total_return + fold_pass_rate≥60% +
         资金填仓/ADV 可执行性把关。
      2. 执行序重排（只省算力、不换判据）：廉价检查先行——1 次全 OOS 回测 + IC →
         失败即 NO-GO（省 5 折）；过了才跑 5 折 walk-forward；全过才跑参数敏感（省 9 次
         回测）。90% 左右的弱势候选只花 1 次回测就退，不再白跑 15 次。
    """
    benchmark_series, benchmark_source = _shanghai_snapshot()
    panel_key = getattr(panel, "_panel_cache_key", None)
    if expected_input_key is not None and not panel_key:
        raise RuntimeError(
            "科学回测要求绑定实际 Panel 快照，但 Panel 缺少 _panel_cache_key"
        )
    actual_input_key = _input_key_from_benchmark_source(
        benchmark_source, panel_key,
    )
    if expected_input_key is not None and actual_input_key != expected_input_key:
        raise RuntimeError(
            "科学回测输入已变化，拒绝把新基准结果记入旧假设: "
            f"{expected_input_key} -> {actual_input_key}"
        )
    factor = mask_signal_eligible(panel, factor)

    resolved_costs = dict(config.COSTS if costs is None else costs)
    ev: dict = {
        "research_input": {
            "data_key": actual_input_key,
            "benchmark_source": benchmark_source,
            "forward_return_label": EXECUTABLE_FORWARD_LABEL,
        },
        "backtest_spec": {
            "hold": int(hold), "q": int(q), "direction": float(is_direction),
            "capital": float(resolved_costs.get("capital", 200_000.0)),
            "n_positions": int(resolved_costs.get("n_positions", 10)),
            "slippage": float(resolved_costs.get("slippage", 0.0)),
            "rebal_buffer": int(resolved_costs.get("rebal_buffer", 0)),
            "entry_rule": str(resolved_costs.get("entry_rule", "none")),
            "exposure_rule": str(resolved_costs.get("exposure_rule", "none")),
            "adv_window": int(resolved_costs.get("adv_window", 0) or 0),
            "max_adv_participation": resolved_costs.get("max_adv_participation"),
            "impact_coefficient_bps": float(
                resolved_costs.get("impact_coefficient_bps", 0.0) or 0.0
            ),
            "window_start": (str(pd.Timestamp(oos_dates[0]).date()) if oos_dates else None),
            "window_end": (str(pd.Timestamp(oos_dates[-1]).date()) if oos_dates else None),
        },
    }

    # ── ① 全 OOS 回测（1 次，廉价先判）──
    bt = run_backtest(panel, factor, oos_dates, hold=hold, q=q, costs=costs,
                      direction=is_direction, capture_audit=capture_audit)
    if not bt.get("ok"):
        return False, {**ev, "checks": {}, "verdict": "NO-GO",
                       "reason": f"OOS 回测失败: {bt.get('reason', '')}"}
    t = daily_t_stat(bt["daily_ret"], hold)      # 报告项，不再否决（2026-08-17）
    n_days = int(bt.get("n_days", 0))
    sh = _excess_vs_shanghai(
        bt["daily_ret"], oos_dates, benchmark_series=benchmark_series,
        benchmark_source=benchmark_source,
    )
    ev["shanghai_bench"] = {
        "ok": bool(sh.get("ok")),
        "n_days": sh.get("n_days"),
        "reason": sh.get("reason"),
        "shanghai_annual": _r(sh.get("shanghai_annual")),
        "shanghai_excess_annual": _r(sh.get("excess_annual")),
        "basis": sh.get("basis"),
        "shanghai_source": sh.get("source"),
    }
    ev["full"] = {
        "annual_return": _r(bt.get("annual_return")),
        "sharpe": _r(bt.get("sharpe")),
        "max_drawdown": _r(bt.get("max_drawdown")),
        "calmar": _r(bt.get("calmar")),
        "realized_volatility": _r(bt.get("realized_volatility")),
        "average_actual_gross_exposure": _r(
            bt.get("average_actual_gross_exposure")
        ),
        "average_cash_weight": _r(bt.get("average_cash_weight")),
        "minimum_cash_weight": _r(bt.get("minimum_cash_weight")),
        "maximum_single_weight": _r(bt.get("maximum_single_weight")),
        "gross_exposure_fill_rate": _r(bt.get("gross_exposure_fill_rate")),
        "lot_target_fill_rate": _r(bt.get("lot_target_fill_rate")),
        "average_desired_positions": _r(bt.get("average_desired_positions")),
        "average_funded_positions": _r(bt.get("average_funded_positions")),
        "liquidity_model": bt.get("liquidity_model"),
        "adv_limited_order_count": int(bt.get("adv_limited_order_count", 0)),
        "adv_missing_order_count": int(bt.get("adv_missing_order_count", 0)),
        "total_market_impact": _r(bt.get("total_market_impact")),
        "win_rate": _r(bt.get("win_rate")),
        "entry_rule": str(bt.get("entry_rule", "none")),
        "entry_order_fill_rate": _r(bt.get("entry_order_fill_rate")),
        "entry_order_count": int(bt.get("entry_order_count", 0)),
        "entry_order_cancelled_count": int(
            bt.get("entry_order_cancelled_count", 0)
        ),
        "previous_close_limit_cancelled_count": int(
            bt.get("previous_close_limit_cancelled_count", 0)
        ),
        "excess_annual": _r(bt.get("excess_annual")),          # 复权等权总收益（强制门槛）
        "benchmark_annual": _r(bt.get("benchmark_annual")),    # 复权等权总收益（同期）
        "benchmark_n_days": int(bt.get("benchmark_n_days", 0)),
        "benchmark_coverage": _r(bt.get("benchmark_coverage")),
        "benchmark_basis": bt.get("benchmark_basis"),
        "shanghai_annual": _r(sh.get("shanghai_annual")),      # 上证 open-to-open 价格指数
        "shanghai_excess_annual": _r(sh.get("excess_annual")), # 策略净年化 − 上证年化
        "n_days": n_days,
        # 2026-08-17 起 t_stat 为报告项：重叠折减 √hold 后，hold=10/370 天需 Sharpe≥5.2
        # 才可能 ≥2，数学不可达；显著性以 ev.oos_ic 为准。此值仅供看板/诊断披露。
        "t_stat": round(t, 3),
        "p_value": round(daily_p_value(t, n_days), 4),
        "t_stat_note": ("绝对收益日 t（√hold 折减自相关）。2026-08-17 起不再作为投产否决，"
                        "显著性以 OOS IC 最大相位/HAC p≤0.05 为准（见 ev.oos_ic）。"),
        "round_trip_cost": _r(bt.get("round_trip_cost")),
        "daily_cost": _r(bt.get("daily_cost")),
        # 组合层换手 KPI（2026-08-14 C3.1）：日均换手率 + >15% 净收益杀手预警。
        # 换手=成本拖累的直接来源（净收益=毛利−换手×成本），闸门证据里如实报告，
        # 不参与门槛判定（预警级），供看板与 promotion 展示。
        "signal_churn": _r(bt.get("signal_churn")),
        "churn_flag": bool(bt.get("churn_flag", False)),
        "total_fees": _r(bt.get("total_fees")),
        "traded_notional": _r(bt.get("traded_notional")),
    }
    if capture_audit:
        ev["audit"] = bt.get("audit")
        ev["audit_sha256"] = bt.get("audit_sha256")

    # ── ② OOS IC（T 收盘信号、T+1 开盘入场、T+H+1 开盘退出）──
    # 终测段尾部标签只能落在本次冻结窗口内。未来面板加入 2026+ 数据后，
    # 历史确认 2025-12 尾部也不得借用前向观察价格。
    test_boundary = max(pd.Timestamp(d) for d in oos_dates)
    fwd = executable_forward_return(
        panel, hold, boundary=str(test_boundary.date()),
    )
    ic_oos = cross_sectional_ic(factor, fwd, oos_dates)
    st = ic_stats(ic_oos, hold=hold)
    # 显著性使用与在线 FDR 相同的 max-phase/HAC 双侧 p 值，而不是把
    # 固定 |t|>=2 当成所有有效样本量下都相同的阈值。
    ic_ok = bool(st["n_dates"] >= 60 and st["ic_mean"] * is_direction > 0
                 and abs(st["ic_ir"]) >= 0.3
                 and st.get("p_value") is not None
                 and float(st["p_value"]) <= 0.05)
    ev["oos_ic"] = {k: _r(st.get(k)) for k in (
        "n_dates", "n_effective", "ic_mean", "ic_ir", "t_stat", "p_value",
        "p_value_method", "p_value_hold", "p_value_phase",
        "p_value_phase_sensitivity",
    )}
    ev["oos_ic"]["forward_return_label"] = EXECUTABLE_FORWARD_LABEL

    # ── M1.3：OOS 池化分层单调（soft 报告，不否决）──
    # 极端组非单调是 A 股常态，硬卡系统性误杀；仅披露 aligned 分值与层均值供看板。
    ev["oos_monotonic"] = pooled_monotonicity(factor, fwd, oos_dates, hold=hold,
                                              q=5, direction=is_direction)

    # ── ③ 廉价检查先行（1 次回测 + IC，失败即退）──
    # 跑赢基准 = 上证指数（2026-08-14 用户强制）：上证数据缺失/样本不足 → fail-closed False。
    # daily_t>=2 已于 2026-08-17 移除（数学不可达，显著性由 oos_ic_significant 承担）。
    n_positions = max(1, int(resolved_costs.get("n_positions", 10)))
    capital_deployable = _capital_deployable(bt, n_positions)
    liquidity_capacity_ok = bool(
        bt.get("liquidity_model") == "prior_adv_participation_sqrt_impact_v1"
        and int(bt.get("adv_missing_order_count", 0)) == 0
    )
    checks = {
        "net_annual>0": bool(pd.notna(bt.get("annual_return")) and bt["annual_return"] > 0),
        "beats_shanghai": bool(sh.get("ok") and sh.get("excess_annual", -1) > 0),
        "beats_matched_total_return": bool(
            float(bt.get("benchmark_coverage", 0.0) or 0.0) == 1.0
            and pd.notna(bt.get("excess_annual"))
            and bt.get("excess_annual", -1) > 0
        ),
        "capital_deployable": capital_deployable,
        "liquidity_capacity_model": liquidity_capacity_ok,
        "mdd>=-35%": bool(pd.notna(bt.get("max_drawdown")) and bt["max_drawdown"] >= -0.35),
        "oos_ic_significant": ic_ok,
    }
    failed = [k for k, v in checks.items() if not v]
    if failed:
        # 未跑 5 折/参数敏感即判 NO-GO（AND 闸门，早退等价）。fold/敏感不在 checks
        # 里补 False 占位——看板会把「未运行」误读成「失败」；用 not_run 如实标注。
        ev["checks"] = checks
        ev["not_run"] = ["fold_pass_rate>=60%", "sensitivity>=60%"]
        ev["early_exit"] = "廉价检查失败，未跑 5 折与参数敏感性"
        ev["verdict"] = "NO-GO"
        ev["reason"] = "、".join(failed)
        return False, ev

    # ── ④ 5 折等分 walk-forward：静态因子已在 IS 训练，验证整段 OOS 的每折 ──
    # 折判定 = 净收益为正 且 跑赢同段上证（2026-08-14 强制基准）
    folds = equal_folds([str(d) for d in oos_dates], n_folds=5)
    fold_rows = []
    for fd in folds:
        # 返回 {val:[start,end]} 区间标记，展开为实际交易日
        val_lo, val_hi = pd.Timestamp(fd["val"][0]), pd.Timestamp(fd["val"][-1])
        val_dates = [d for d in oos_dates if val_lo <= d <= val_hi]
        btf = run_backtest(panel, factor, val_dates, hold=hold, q=q, costs=costs,
                           direction=is_direction)
        shf = (_excess_vs_shanghai(
            btf["daily_ret"], val_dates,
            benchmark_series=benchmark_series,
            benchmark_source=benchmark_source,
        )
               if btf.get("ok") else {"ok": False, "reason": "回测失败"})
        fok = bool(btf.get("ok") and btf.get("annual_return", -1) > 0
                   and btf.get("excess_annual", -1) > 0
                   and shf.get("ok") and shf.get("excess_annual", -1) > 0)
        fold_rows.append({
            "ok": fok,
            "val": fd["val"],
            "annual_return": _r(btf.get("annual_return")),
            "excess_annual": _r(btf.get("excess_annual")),
            "shanghai_annual": _r(shf.get("shanghai_annual")),
            "shanghai_excess_annual": _r(shf.get("excess_annual")),
            "n_days": btf.get("n_days"),
        })
    fp = fold_pass_rate([r["ok"] for r in fold_rows])
    ev["folds"] = fold_rows
    ev["fold_pass"] = fp
    checks["fold_pass_rate>=60%"] = bool(fp["gate_ok"])
    failed = [k for k, v in checks.items() if not v]
    if failed:
        ev["checks"] = checks
        ev["verdict"] = "NO-GO"
        ev["reason"] = "、".join(failed)
        return False, ev

    # ── ⑤ 参数敏感性：持有期 × 持仓数邻域，净收益为正且跑赢上证占比 ≥60% ──
    # q 只定义先取哪一条分位带，随后仍会取全局 top-N；只要 top-N 落在目标带内，
    # q±1 就不会改变任何持仓。旧九宫格因此常是 3 个实验重复三遍。这里改测真正会
    # 改变交易的两个用户参数：holding_days 与 n_positions(top_n)。
    sens = []
    sensitivity_specs = _sensitivity_specs(hold, costs)
    for spec in sensitivity_specs:
        h_try = spec["hold"]
        n_try = spec["n_positions"]
        costs_try = dict(config.COSTS if costs is None else costs)
        costs_try["n_positions"] = n_try
        bt_s = run_backtest(panel, factor, oos_dates, hold=h_try, q=q,
                            costs=costs_try, direction=is_direction)
        shs = (_excess_vs_shanghai(
            bt_s["daily_ret"], oos_dates,
            benchmark_series=benchmark_series,
            benchmark_source=benchmark_source,
        )
               if bt_s.get("ok") else {"ok": False})
        ok_s = bool(bt_s.get("ok") and bt_s.get("annual_return", -1) > 0
                    and bt_s.get("excess_annual", -1) > 0
                    and shs.get("ok") and shs.get("excess_annual", -1) > 0)
        fingerprint = _execution_fingerprint(bt_s)
        sens.append({"hold": h_try, "q": q, "n_positions": n_try, "ok": ok_s,
                     "annual_return": _r(bt_s.get("annual_return")),
                     "shanghai_excess_annual": _r(shs.get("excess_annual")),
                     "total_fees": _r(bt_s.get("total_fees")),
                     "traded_notional": _r(bt_s.get("traded_notional")),
                     "signal_churn": _r(bt_s.get("signal_churn")),
                     "n_days": bt_s.get("n_days"),
                     "execution_fingerprint": fingerprint})
    first_by_path: dict[str, int] = {}
    for index, row in enumerate(sens):
        fingerprint = row.get("execution_fingerprint")
        if not fingerprint:
            continue
        if fingerprint in first_by_path:
            row["duplicate_of"] = first_by_path[fingerprint]
        else:
            first_by_path[fingerprint] = index
    ev["sensitivity"] = sens
    ev["sensitivity_axes"] = ["holding_days", "n_positions"]
    passed_paths = {row["execution_fingerprint"] for row in sens
                    if row["ok"] and row.get("execution_fingerprint")}
    n_ok_s = len(passed_paths)
    ev["sensitivity_unique_paths"] = len(first_by_path)
    ev["sensitivity_duplicate_specs"] = len(sens) - len(first_by_path)
    # 重复成交路径不能重复投票；九个扰动规格至少需要六条不同路径通过。
    sens_ok = bool(sens) and n_ok_s / len(sens) >= 0.6
    ev["sensitivity_pass"] = sens_ok
    checks["sensitivity>=60%"] = sens_ok
    ev["checks"] = checks

    if not sens_ok:
        ev["verdict"] = "NO-GO"
        ev["reason"] = (f"参数敏感性不足（{n_ok_s}/{len(sens)} 条独立执行路径通过；"
                        f"重复规格 {ev['sensitivity_duplicate_specs']} 条）")
        return False, ev

    ev["verdict"] = "PASS"
    ev["reason"] = "科学闸门全端通过"
    return True, ev


def _sensitivity_specs(hold: int, costs: dict | None) -> list[dict[str, int]]:
    """生成九个互不重复、且会改变实际组合定义的邻域规格。"""
    base_positions = int((costs or {}).get("n_positions", 10)) or 10

    def neighbours(center: int, floor: int) -> list[int]:
        center = max(floor, int(center))
        step = max(1, int(round(center * 0.2)))
        values = {center, max(floor, center - step), center + step}
        candidate = center + step
        while len(values) < 3:
            candidate += step
            values.add(candidate)
        return sorted(values)

    return [
        {"hold": h, "n_positions": n}
        for h in neighbours(hold, 3)
        for n in neighbours(base_positions, 1)
    ]


def _execution_fingerprint(backtest: dict) -> str | None:
    """轻量成交路径指纹；相同收益/成交/费用路径在敏感性中只投一票。"""
    if not backtest.get("ok"):
        return None
    daily = pd.Series(backtest.get("daily_ret", pd.Series(dtype=float))).dropna()
    payload = {
        "dates": [pd.Timestamp(d).date().isoformat() for d in daily.index],
        "daily_ret": [round(float(value), 12) for value in daily.to_numpy()],
        "total_fees": round(float(backtest.get("total_fees") or 0.0), 8),
        "traded_notional": round(float(backtest.get("traded_notional") or 0.0), 8),
        "n_days": int(backtest.get("n_days") or len(daily)),
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _r(v):
    """None/NaN → None（JSON 安全）。"""
    try:
        if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
            return None
    except Exception:
        pass
    return v


__all__ = ["run_scientific_gate", "daily_t_stat", "daily_p_value"]
