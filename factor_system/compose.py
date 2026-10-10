"""M3.1 策略组合因子：V4 因子在 factor_system Panel 重建 → 组合 → 统一引擎评估。

目标：策略开发与因子挖掘走**同一套引擎与闸门**——组合因子经
`run_validation`（IS L0-L3，行业+市值中性化 + raw/neutralized 双 IC）与
`run_scientific_gate`（OOS 科学闸门：多折≥60% / 净收益正 / 同时跑赢上证与
复权总收益基准 / 回撤≥-35% / OOS IC 最大相位/HAC p≤0.05+IR≥0.3+同向 /
资金填仓与 ADV 可执行性 / 参数敏感性）判定。策略层只加注证据，不替换门槛。

V4 原口径（research/v4_factor_pipeline.py compute_security_factors）：
    return_index = cumprod(1 + pctChg/100)，amount 仅 tradestatus==1 日计入
    trend60                   = RI / MA(RI, 60) - 1
    residual_momentum_20_5    = RI.shift(5) / RI.shift(20) - 1
    low_volatility_20         = -pctChg/100.rolling(20).std(ddof=1)
    price_volume_confirmation = ret5 * log(avg_amount_5 / avg_amount_20)
    liquidity_20              = log(avg_amount_20)

factor_system Panel 重建（口径差异如实声明，对账不掩盖）：
    close  = 复权收盘价（≈ return_index 的比例缩放——纯比率因子
             trend60 / residual_momentum_20_5 为不变式，逐日一致）
    ret    = close.pct_change（复权日收益；V4 用 raw pctChg，除权日有微小差）
    amount = 成交额矩阵（面板已做宇宙/可交易过滤；V4 另按 tradestatus==1 过滤）

组合语义（与 V4 `groupby(trade_date)[f].rank(method="average", pct=True)` 一致）：
    横截面 pct-rank × 权重 加权求和 → 组合因子（高 = 买，权重已编码方向）。
    v1 仅手填权重（不引入 ML/最大夏普权重，避免过拟合面；v2 配 deflated Sharpe 再上）。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from . import config
from . import source_gate
from .backtest import run_backtest
from .data import (
    EXECUTABLE_FORWARD_LABEL, Panel, executable_forward_return,
    load_thin_panel, mask_signal_eligible, split_research_windows, split_windows,
)
from .diagnostics import compute_diagnostics
from .gate import (
    _aligned_shanghai_returns, daily_t_stat, full_cycle_evaluation,
    research_input_key,
    run_scientific_gate,
)
from .metrics import cross_sectional_ic, ic_stats
from .preprocess import build_exposures
from .search import rebuild_factor          # M7：用户挖掘配方重建入口（无环）
from .validate import run_validation


# ── V4 因子重建 ──────────────────────────────────────────────
def rebuild_trend60(panel: Panel) -> pd.DataFrame:
    c = panel.close
    return c / c.rolling(60, min_periods=60).mean() - 1.0


def rebuild_residual_momentum_20_5(panel: Panel) -> pd.DataFrame:
    c = panel.close
    return c.shift(5) / c.shift(20) - 1.0


def rebuild_low_volatility_20(panel: Panel) -> pd.DataFrame:
    return -panel.ret.rolling(20, min_periods=20).std(ddof=1)


def rebuild_price_volume_confirmation(panel: Panel) -> pd.DataFrame:
    c = panel.close
    ret5 = c / c.shift(5) - 1.0
    a5 = panel.amount.rolling(5, min_periods=5).mean()
    a20 = panel.amount.rolling(20, min_periods=20).mean()
    return ret5 * np.log(a5 / a20)


def rebuild_liquidity_20(panel: Panel) -> pd.DataFrame:
    return np.log(panel.amount.rolling(20, min_periods=20).mean())


# ── M4.1 A股实证因子族（2026-08-15 Web 研究新增）──────────────────
# 方向编码约定：因子值高 = 该因子经济假设下的「好票」，正权重即买它（高=买）。
# 依据（六源 Web 研究汇总，详见 research/2026-08-15_quant_research_strategy.md）：
#   A股横截面图谱与美股差异大——中期动量(12-1)基本不存在甚至反向（T+1 隔夜折价
#   制度根源，多空月频年化约 -34%）；短期反转最强（周频 264 组 97% 显著反转、
#   国信 UMR IC 0.114/ICIR 5.04）；小市值溢价极强但极不稳定（2017/2020 失效、
#   2024-01 微盘 -40% 流动性危机）→ 只能小仓卫星；低波/红利低波稳健偏防守；
#   价值 2019 后显著失效（PB-ROE 相关上升，需残差改造）；非流动性溢价存在但与
#   小市值高度共线；彩票(MAX)偏好负预期。
def rebuild_short_term_reversal_5(panel: Panel) -> pd.DataFrame:
    """短期反转（A股周频反转最强）：过去 5 日收益的相反数，高=近期输家。

    实证：国信 UMR 复合月度 IC 0.114/年化 ICIR 5.04；太平洋 1 月反转多空年化 18.7%。
    ⚠ 高换手成本敏感；下跌市反转更强（月均 0.69% vs 3.42%）。方向已编码。
    """
    c = panel.close
    return -(c.shift(1) / c.shift(6) - 1.0)


def rebuild_medium_term_reversal_25_5(panel: Panel) -> pd.DataFrame:
    """中期反转（跳过近 5 日）：过去 25 日收益的相反数，高=中期输家。

    实证：A股月度 J=6/K=1 反转最强（多空月 -2.82%，做多赢家做空输家大幅亏损）。
    方向已编码。
    """
    c = panel.close
    return -(c.shift(5) / c.shift(30) - 1.0)


def rebuild_size_small(panel: Panel) -> pd.DataFrame:
    """小市值代理：log(流通市值) 相反数，高=市值小。

    实证：万得微盘 2010-2024 年化 28.78% vs 沪深300 0.75%，但 2017/2020 失效、
    2024-01 微盘 -40% 流动性危机（雪球敲入/DMA 强平正反馈）→ 容量极小、尾部大，
    只能小仓卫星+严格风控；本质是风格 beta 而非 alpha。方向已编码。
    """
    mv = panel.float_mv.replace(0, np.nan)
    return -np.log(mv)


def rebuild_illiquidity_amihud_20(panel: Panel) -> pd.DataFrame:
    """Amihud 非流动性：20 日均 |ret|/amount，高=非流动性强（溢价正）。

    实证：梁丽珍/孔东民 2008 Amihud 为 A股最合适流动性定价因子；非流动性溢价是
    小市值超额的重要构成，二者高度共线——勿与小市值同权重叠加过重。方向已编码。
    """
    am = panel.ret.abs() / panel.amount
    return am.rolling(20, min_periods=20).mean()


def rebuild_low_turnover_20(panel: Panel) -> pd.DataFrame:
    """低换手：20 日均换手率的相反数，高=低换手（低换手溢价）。

    实证：换手率与预期收益显著负相关（李一红/吴世农 2003）；2024 全年低换手占优
    但 2 月/9-10 月短暂反转。换手 0（停牌无成交）剔除。方向已编码。
    """
    tn = panel.turnover.replace(0, np.nan)
    return -tn.rolling(20, min_periods=20).mean()


def rebuild_lottery_max_return_20(panel: Panel) -> pd.DataFrame:
    """彩票偏好（MAX 效应）：20 日最大日收益，高=彩票股（实证负预期）。

    A股彩票偏好/MAX 效应为负（高博彩票股未来收益低）；因子保持原始方向，
    模板/用户用**负权重**=买非彩票股。⚠ 方向由权重符号决定，非正权重。
    """
    return panel.ret.rolling(20, min_periods=20).max()


def rebuild_value_earnings_yield(panel: Panel) -> pd.DataFrame:
    """价值·盈利收益率：1/PE_TTM，高=盈利收益率高（便宜）。

    实证：PE<10 组合 2000-2019 中位收益 18%，但 2019 后显著失效（A股国际化使
    PB 与 ROE 相关性上升，盈利取代便宜成为主要驱动），需 PB-ROE 残差/EP 改造。
    ⚠ PIT 基本面数据仅覆盖 2021-09~2024-12 → 2025 全年 NaN，OOS 后段无值，
    价值类结论仅为研究假设。pe<=0（亏损）→ 收益率为负，rank 低位。方向已编码。
    """
    pe = panel.pe_ttm.replace(0, np.nan)
    return 1.0 / pe


def rebuild_value_book_yield(panel: Panel) -> pd.DataFrame:
    """价值·账面收益率：1/PB，高=便宜（PB-ROE 残差改造前的原始口径）。

    同 earnings_yield：2019 后失效，PIT 数据仅 2021-09~2024-12，仅研究假设。
    """
    pb = panel.pb_mrq.replace(0, np.nan)
    return 1.0 / pb


REBUILDERS = {
    "trend60": rebuild_trend60,
    "residual_momentum_20_5": rebuild_residual_momentum_20_5,
    "low_volatility_20": rebuild_low_volatility_20,
    "price_volume_confirmation": rebuild_price_volume_confirmation,
    "liquidity_20": rebuild_liquidity_20,
    # M4.1 A股实证因子族
    "short_term_reversal_5": rebuild_short_term_reversal_5,
    "medium_term_reversal_25_5": rebuild_medium_term_reversal_25_5,
    "size_small": rebuild_size_small,
    "illiquidity_amihud_20": rebuild_illiquidity_amihud_20,
    "low_turnover_20": rebuild_low_turnover_20,
    "lottery_max_return_20": rebuild_lottery_max_return_20,
    "value_earnings_yield": rebuild_value_earnings_yield,
    "value_book_yield": rebuild_value_book_yield,
}

DEFAULT_WEIGHTS = {
    "trend60": 1.0,
    "residual_momentum_20_5": 1.0,
    "low_volatility_20": 1.0,
    "price_volume_confirmation": 0.0,
    "liquidity_20": 0.25,
}


def rebuild_v4_factors(panel: Panel) -> dict[str, pd.DataFrame]:
    """重建全部可组合因子（同一份面板，供组合与单因子诊断复用）。

    含 V4 原 5 因子 + M4.1 A股实证因子族 8 个。DEFAULT_WEIGHTS 只激活 V4 原
    5 因子（新因子默认权重 0 不参与），存量策略/基线语义不受影响。
    """
    return load_factor_map(panel, list(REBUILDERS), cache=False)


# ── M7 因子宇宙重建：内置 13 + 用户挖掘 156 配方 ──────────────────
# 8GB 单机内存护栏：配方因子每张 ~5086×1212 float32 ≈ 25MB，LRU 上限 8 张
# （~200MB 峰值）。只缓存配方重建结果，内置因子即时重建（廉价）不占缓存。
_FACTOR_MAP_CACHE: dict[str, pd.DataFrame] = {}
_FACTOR_MAP_CACHE_SHA: dict[str, str] = {}
_FACTOR_MAP_CACHE_PANEL: dict[str, str] = {}
_FACTOR_MAP_ORDER: list[str] = []
_FACTOR_MAP_MAX = 8


def _require_runnable_source(fid: str) -> dict:
    result = source_gate.verify_candidate(
        fid, source_gate.load_catalog(config.SOURCE_CATALOG),
    )
    if not result["valid"] or not result["runnable"]:
        detail = ",".join(result["errors"] or ["candidate_not_runnable"])
        raise ValueError(f"[source_record_invalid] {fid}: {detail}")
    return result


def _sealed_recipe_record(fid: str) -> dict:
    """读取并验证当前 epoch 的不可变配方；任何缺项都 fail-closed。"""
    from . import registry as reg

    c = reg._conn(readonly=True)
    try:
        row = c.execute(
            """SELECT factor_id,name,family,formula,status,research_epoch,
                      sealed_at,sealed_data_end,sealed_recipe_sha256,
                      research_record_json,research_record_sha256
                 FROM factors WHERE factor_id=? AND research_epoch=?""",
            (fid, config.RESEARCH_EPOCH),
        ).fetchone()
    finally:
        c.close()
    if row is None:
        raise ValueError(f"因子不属于当前研究 epoch: {fid}")
    record = dict(row)
    seal_keys = ("sealed_at", "sealed_data_end", "sealed_recipe_sha256")
    if any(not record.get(key) for key in seal_keys):
        raise ValueError(f"因子缺少不可变 seal: {fid}")
    if (not record.get("research_record_json")
            or not record.get("research_record_sha256")):
        raise ValueError(f"[source_record_missing] 因子缺少来源冻结记录: {fid}")

    path = config.REGISTRY_DIR / "recipes" / f"{fid}.json"
    try:
        raw = path.read_bytes()
        recipe = json.loads(raw)
    except Exception as exc:
        raise ValueError(f"因子配方缺失或损坏: {fid}") from exc
    actual_sha = hashlib.sha256(raw).hexdigest()
    if (not isinstance(recipe, dict)
            or actual_sha != record["sealed_recipe_sha256"]
            or recipe.get("research_epoch") != config.RESEARCH_EPOCH
            or recipe.get("sealed_at") != record["sealed_at"]
            or recipe.get("sealed_data_end") != record["sealed_data_end"]
            or recipe.get("research_record_sha256")
            != record["research_record_sha256"]):
        raise ValueError(f"因子 seal/配方哈希校验失败: {fid}")
    reg.verify_frozen_source_record(record, recipe)
    return {**record, "recipe": recipe, "recipe_sha256": actual_sha}


def _preflight_factor_sources(factor_ids: list[str]) -> None:
    """Validate every requested source before any strategy data is loaded."""
    for fid in dict.fromkeys(factor_ids):
        if fid in REBUILDERS:
            _require_runnable_source(fid)
        else:
            _sealed_recipe_record(fid)


def load_factor_map(panel: Panel, factor_ids: list[str] | None = None,
                    cache: bool = True) -> dict[str, pd.DataFrame]:
    """按需重建因子图 {id: DataFrame}：内置 REBUILDERS 即时、配方走 LRU 缓存。

    M7 目标：策略实验室可用「我的因子」回测——本函数把 13 内置 + 156 配方统一
    到同一张因子图，`compose_factor`/`_annotate`/`_sub_factor_decomposition` 全复用。
    方向编码不变：`rank × weight`，权重符号即方向，评估期永不事后翻向。

    factor_ids=None → 全部内置 REBUILDERS（向后兼容 rebuild_v4_factors）。
    任一 active 配方 seal 校验或重建失败 → ValueError（不静默改变组合）。
    """
    if factor_ids is None:
        factor_ids = list(REBUILDERS)
    panel_key = str(getattr(panel, "_panel_cache_key", "") or "")
    out: dict[str, pd.DataFrame] = {}
    failed: list[str] = []
    for fid in factor_ids:
        if fid in REBUILDERS:
            _require_runnable_source(fid)
            out[fid] = REBUILDERS[fid](panel)
            continue
        # seal 校验必须发生在缓存命中之前；缓存也绑定实际配方哈希。
        try:
            record = _sealed_recipe_record(fid)
            cached_df = _FACTOR_MAP_CACHE.get(fid)
            if (cache and cached_df is not None
                    and panel_key
                    and _FACTOR_MAP_CACHE_SHA.get(fid) == record["recipe_sha256"]
                    and _FACTOR_MAP_CACHE_PANEL.get(fid) == panel_key):
                out[fid] = cached_df
                continue
            df = rebuild_factor(panel, record["recipe"])
            if df is None or not df.notna().any().any():
                failed.append(fid)
                continue
            df = df.astype("float32")
            # A cached recipe matrix is meaningful only for the exact Panel
            # snapshot it was built from.  Panels without an identity remain
            # usable, but are deliberately rebuilt on every call.
            if cache and panel_key:
                _FACTOR_MAP_CACHE[fid] = df
                _FACTOR_MAP_CACHE_SHA[fid] = record["recipe_sha256"]
                _FACTOR_MAP_CACHE_PANEL[fid] = panel_key
                if fid in _FACTOR_MAP_ORDER:
                    _FACTOR_MAP_ORDER.remove(fid)
                _FACTOR_MAP_ORDER.append(fid)
                if len(_FACTOR_MAP_ORDER) > _FACTOR_MAP_MAX:
                    old = _FACTOR_MAP_ORDER.pop(0)
                    _FACTOR_MAP_CACHE.pop(old, None)
                    _FACTOR_MAP_CACHE_SHA.pop(old, None)
                    _FACTOR_MAP_CACHE_PANEL.pop(old, None)
            out[fid] = df
        except Exception:
            failed.append(fid)
    if failed:
        raise ValueError("策略因子无法通过 seal 校验或重建: " + ", ".join(failed))
    return out


def compose_factor(panel: Panel, weights: dict | None = None,
                   factors: dict[str, pd.DataFrame] | None = None) -> pd.DataFrame:
    """横截面 pct-rank × 权重 加权求和 → 组合因子（高 = 买）。

    与 V4 `panel.groupby(trade_date)[factor].rank(method="average", pct=True)`
    语义一致：`df.rank(axis=1, method="average", pct=True)`。
    权重为 0 的因子不参与；仅剩一个因子时退化为该因子（策略也可评估单因子）。
    """
    active = {k: float(w) for k, w in (weights or DEFAULT_WEIGHTS).items()
              if float(w)}
    if factors is None:
        factors = load_factor_map(panel, list(active))
    for name in active:
        _require_runnable_source(name)
    score: pd.DataFrame | None = None
    for name, w in active.items():
        f = factors.get(name)
        if f is None:
            continue
        r = f.rank(axis=1, method="average", pct=True)
        term = r * w
        score = term if score is None else score.add(term)
    if score is None:
        raise ValueError("策略无有效因子权重（全部为 0 或因子缺失）")
    return score


# ── C3.2 相关感知 IC 加权合成（v2）─────────────────────────────
def estimate_ic_weights(panel: Panel, factors: dict[str, pd.DataFrame],
                        is_dates: list[pd.Timestamp], hold: int = config.HOLD_DAYS,
                        method: str = "corr_ic", ridge: float = 1.0) -> dict:
    """发现期估计组合权重（严格 PIT：后续验证/历史确认数据不参与定权）。

    Web 研究结论（提升组合 IR 的关键杠杆）：
      - 纯 IC 加权   v_i ∝ IC_i：简单、对 IC 估计噪声敏感度中等。
      - 相关感知 IC 加权 w ∝ (C + ridge·I)⁻¹ · IC：对 IC 时间序列相关矩阵求逆，
        高相关因子互相去重、低相关因子加权，组合方差最小化——报告 IR 2.68→3.23
        的来源（Qian, Hua & Sorensen《Quantitative Equity Portfolio Management》）。
      ridge 岭正则防 IC 相关矩阵近奇异导致权重爆炸（IS 样本有限时相关估计噪声大）。
    负 IC → 负权重：rank × 负权 → 买该因子底分位（A 股反转做多，方向已编码，
    run_backtest 恒 is_direction=+1，不在评估期事后翻向——那是数据窥探）。
    """
    if not is_dates:
        raise ValueError("发现期无交易日，无法估计 IC 权重")
    fwd = executable_forward_return(panel, hold, boundary=str(max(is_dates)))
    ic_mean: dict[str, float] = {}
    ic_series: dict[str, pd.Series] = {}
    for name, f in factors.items():
        f = mask_signal_eligible(panel, f)
        s = cross_sectional_ic(f, fwd, is_dates)
        if s is None:
            continue
        s = s.dropna()
        mean = float(s.mean()) if len(s) >= 60 else np.nan
        if not np.isfinite(mean):
            continue
        ic_mean[name] = mean
        ic_series[name] = s
    if not ic_mean:
        raise ValueError("IS 期无可估权重的因子（IC 观测不足 60 日）")
    names = list(ic_mean.keys())
    vec = np.array([ic_mean[k] for k in names], dtype=float)
    if method == "ic":
        wv = vec
    else:
        mat = pd.concat([ic_series[k] for k in names], axis=1, sort=False).dropna()
        if len(mat) < 30:
            wv = vec  # 共同观测不足 → 降级纯 IC 加权
        else:
            C = np.corrcoef(mat.to_numpy(dtype=float).T)
            C = np.nan_to_num(C, nan=0.0, posinf=0.0, neginf=0.0)
            try:
                wv = np.linalg.solve(C + ridge * np.eye(len(names)), vec)
            except np.linalg.LinAlgError:
                wv = vec
    return {k: float(wv[i]) for i, k in enumerate(names)}


def compose_ic_weighted(panel: Panel, weights: dict | None = None,
                        factors: dict[str, pd.DataFrame] | None = None,
                        is_dates: list | None = None,
                        hold: int = config.HOLD_DAYS,
                        method: str = "corr_ic") -> tuple[pd.DataFrame, dict]:
    """C3.2 IC 加权合成：weights 只作「启用掩码」（非零才参与），实际权重由
    estimate_ic_weights 在 IS 期估计。返回 (组合因子, 估计权重)。"""
    active = {k: float(w) for k, w in (weights or DEFAULT_WEIGHTS).items() if float(w)}
    if factors is None:
        factors = load_factor_map(panel, list(active))
    for name in active:
        _require_runnable_source(name)
    pool = {k: f for k, f in factors.items() if k in active}
    if is_dates is None:
        is_dates = split_research_windows(panel.dates)["discovery"]
    est = estimate_ic_weights(panel, pool, list(is_dates), hold=hold, method=method)
    factor = compose_factor(panel, est, pool)
    return factor, est


def _factor_lineage(weights: dict) -> list[dict]:
    """策略报告中的因子血缘：明确记录选股信号来自哪个注册表配方。"""
    try:
        from .catalog import FACTOR_META
        meta = FACTOR_META()
    except Exception:
        meta = {}
    rows: list[dict] = []
    for fid, weight in weights.items():
        if not float(weight):
            continue
        item = meta.get(fid) or {}
        source_record = _require_runnable_source(fid)
        sealed = None if fid in REBUILDERS else _sealed_recipe_record(fid)
        recipe = (sealed or {}).get("recipe") or {}
        rows.append({
            "factor_id": fid,
            "weight": float(weight),
            "kind": recipe.get("kind", item.get("kind", "builtin")),
            "status": ((sealed or {}).get("status")
                       or item.get("status", "builtin")),
            "label": item.get("label", (sealed or {}).get("name", fid)),
            "formula": recipe.get("formula", item.get("formula", "")),
            "source": ("factor_system.registry" if sealed
                       else "factor_system.builtin"),
            "research_epoch": (sealed or {}).get("research_epoch"),
            "sealed_at": (sealed or {}).get("sealed_at"),
            "sealed_data_end": (sealed or {}).get("sealed_data_end"),
            "sealed_recipe_sha256": (sealed or {}).get("recipe_sha256"),
            "research_record_sha256": (sealed or {}).get(
                "research_record_sha256"
            ) or source_record.get("research_record_sha256"),
        })
    return rows


def _strategy_risk_mask(panel: Panel, risk_rule: str,
                        windows: dict[str, list] | None = None) -> tuple[pd.Series, dict]:
    """用信号日可见数据生成策略级市场开关；未知/缺失 PIT 数据一律拒绝。"""
    rule = str(risk_rule or "none")
    if rule not in {"none", "breadth_majority"}:
        raise ValueError(f"统一引擎不支持风险开关: {rule}")

    index = panel.close.index
    research_windows = {
        name: dates for name, dates in (windows or {}).items()
        if name in {"discovery", "validation", "historical_test"}
    }
    scope = (index.intersection(pd.DatetimeIndex([
        day for dates in research_windows.values() for day in dates
    ])) if windows is not None else index)
    if rule == "none":
        risk_on = pd.Series(index.isin(scope), index=index, dtype=bool)
        eligible_count = pd.Series(0, index=index, dtype=int)
        positive_count = pd.Series(0, index=index, dtype=int)
    else:
        if not isinstance(panel.signal_eligible, pd.DataFrame):
            raise ValueError("breadth_majority 需要 PIT signal_eligible 面板")
        trend = rebuild_trend60(panel).reindex(index=index, columns=panel.close.columns)
        eligible = (panel.signal_eligible.reindex(
            index=index, columns=panel.close.columns).fillna(False).astype(bool)
                    & trend.notna() & np.isfinite(trend))
        eligible_count = eligible.sum(axis=1).astype(int)
        positive_count = ((trend > 0) & eligible).sum(axis=1).astype(int)
        breadth = positive_count.div(eligible_count.where(eligible_count > 0))
        risk_on = (eligible_count > 0) & breadth.ge(0.5) & index.isin(scope)

    schedule = [
        [pd.Timestamp(day).date().isoformat(), int(eligible_count.loc[day]),
         int(positive_count.loc[day]), bool(risk_on.loc[day])]
        for day in scope
    ]
    window_counts = {}
    for name, dates in research_windows.items():
        dates_in_panel = index.intersection(pd.DatetimeIndex(dates))
        on = int(risk_on.reindex(dates_in_panel).fillna(False).sum())
        window_counts[name] = {
            "n_dates": int(len(dates_in_panel)),
            "risk_on_dates": on,
            "risk_off_dates": int(len(dates_in_panel) - on),
        }
    audit = {
        "rule": rule,
        "applied": rule != "none",
        "threshold": 0.5 if rule == "breadth_majority" else None,
        "basis": ("PIT signal_eligible 中 trend60 有效股票的正值占比"
                  if rule == "breadth_majority" else "disabled"),
        "signal_timing": "signal_date_close_then_t_plus_1_open",
        "execution_semantics": "new_cohort_entry_gate_existing_cohorts_expire_naturally",
        "liquidates_existing_positions": False,
        "risk_on_dates": int(risk_on.reindex(scope).sum()),
        "risk_off_dates": int(len(scope) - risk_on.reindex(scope).sum()),
        "no_observation_dates": (int((eligible_count.reindex(scope) == 0).sum())
                                 if rule != "none" else 0),
        "windows": window_counts,
        "schedule_sha256": hashlib.sha256(json.dumps(
            schedule, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest(),
    }
    return risk_on, audit


# ── 统一引擎评估 ──────────────────────────────────────────────
def evaluate_strategy(spec: dict, panel: Panel | None = None,
                      factors: dict | None = None,
                      expected_input_key: str | None = None) -> dict:
    """全引擎评估一个策略规格。

    spec: {"name", "holding_days", "top_n", "factors": {name: weight},
           "account_capital": 200000, ...}
    发现期 → 定权与 L0-L1；验证期 → run_validation 的 L2；
    历史确认期 → run_scientific_gate（hold/成本/concentration 按策略规格）。

    组合方向固定 is_direction=+1（as-designed）：策略权重已编码方向，
    事后按 IC 翻向属于数据窥探，策略评估不做。V4 原引擎同样按 score 买 top-N。

    M7：factors 可预建内置因子图；注册表配方始终按 seal 校验后重建，不能由调用方
    注入的同名 DataFrame 绕过。None 时按 active 因子统一加载。
    """
    weights = spec.get("factors", DEFAULT_WEIGHTS)
    active_ids = [k for k, v in weights.items() if float(v)] or list(DEFAULT_WEIGHTS)
    _preflight_factor_sources(active_ids)
    frozen_input_key = expected_input_key or research_input_key()
    panel = panel if panel is not None else load_thin_panel()
    windows = split_research_windows(panel.dates)
    discovery_dates = windows["discovery"]
    validation_dates = windows["validation"]
    historical_dates = windows["historical_test"]
    compose_method = spec.get("compose_method", "rank_weighted_v1")
    risk_rule = str(spec.get("risk_rule") or "none")
    hold = int(spec.get("holding_days", config.HOLD_DAYS))
    top_n = int(spec.get("top_n", 30))
    capital = float(spec.get("account_capital", 200_000.0))
    one_way_cost_bps = float(spec.get(
        "one_way_cost_bps", float(config.COSTS.get("slippage", 0.0)) * 10_000.0))
    rebal_buffer = int(spec.get(
        "rebal_buffer", int(config.COSTS.get("rebal_buffer", 0))))
    entry_rule = str(spec.get("entry_rule") or "none")
    exposure_rule = str(spec.get("exposure_rule") or "none")

    controlled = load_factor_map(panel, active_ids)
    factors = controlled if factors is None else {**factors, **controlled}
    factors = {
        name: mask_signal_eligible(panel, frame)
        for name, frame in factors.items()
    }
    risk_on, risk_control = _strategy_risk_mask(panel, risk_rule, windows)
    if compose_method in ("ic_weighted", "corr_ic_weighted"):
        # C3.2 IC 加权：权重只在发现期估计，后续窗口用固定权重评估。
        method = "corr_ic" if compose_method == "corr_ic_weighted" else "ic"
        factor, weights_used = compose_ic_weighted(
            panel, weights, factors,
            [d for d in discovery_dates if bool(risk_on.get(d, False))],
            hold=hold, method=method)
    else:
        factor = compose_factor(panel, weights, factors)
        weights_used = {k: float(v) for k, v in weights.items() if float(v)}
    factor = factor.where(risk_on.reindex(factor.index, fill_value=False), axis=0)
    factor_lineage = _factor_lineage(weights_used)
    discovery_fwd = executable_forward_return(
        panel, hold, boundary=config.DISCOVERY_END)
    validation_fwd = executable_forward_return(
        panel, hold, boundary=config.VALIDATION_END)
    exposures = build_exposures(panel)

    # 发现期只负责构造/定权，验证期负责 L2；两段标签都在各自边界截断。
    v = run_validation(factor, validation_fwd, discovery_dates, validation_dates,
                       hold=hold, exposures=exposures, is_fwd=discovery_fwd)

    # 历史确认闸门：q 保持默认（顶 20% 带），concentration 由 n_positions=top_n 控制——
    # run_backtest 在分位带内取因子值最高 max_pos 名（P-C 现实持仓），
    # top_n 落在带内时即"按 score 取 top-N"，与 V4 语义一致。
    costs = {**config.COSTS, "slippage": one_way_cost_bps / 10_000.0,
             "n_positions": top_n, "capital": capital,
             "rebal_buffer": rebal_buffer, "entry_rule": entry_rule,
             "exposure_rule": exposure_rule}
    historical_passed, ev = run_scientific_gate(
        panel, factor, historical_dates, hold=hold, costs=costs, is_direction=1.0,
        expected_input_key=frozen_input_key)
    final_passed = bool(v.get("passed")) and bool(historical_passed)
    validation_stage = v.get("stage")
    validation_detail = v.get(validation_stage) if validation_stage else None
    validation_reason = (validation_detail or {}).get("reason") if isinstance(
        validation_detail, dict) else None
    final_reason = (ev.get("reason") if v.get("passed") else
                    f"验证期未通过{f'：{validation_reason}' if validation_reason else ''}")

    # 策略层加注（只加注不替换门槛）：分层 / 单因子贡献 / OOS IC / 诚实披露
    annotation = _annotate(panel, factor, weights_used, hold,
                           discovery_dates, historical_dates,
                           factors=factors, risk_on=risk_on)
    annotation["honesty"] = _honesty_annotations(
        panel, discovery_dates, historical_dates)

    # 全周期 2021-2025 走查 vs 上证（研究视角，非投产闸门；只加注不替换 OOS 门禁）
    try:
        full_cycle = full_cycle_evaluation(panel, factor, hold=hold, costs=costs,
                                           direction=1.0,
                                           expected_input_key=frozen_input_key)
    except Exception as _exc:
        full_cycle = {"ok": False, "reason": str(_exc)[:80]}

    return {
        "spec": {
            "name": spec.get("name", "未命名策略"),
            "holding_days": hold,
            "top_n": top_n,
            "capital": capital,
            "one_way_cost_bps": one_way_cost_bps,
            "rebal_buffer": rebal_buffer,
            "entry_rule": entry_rule,
            "exposure_rule": exposure_rule,
            "factors": weights_used,
            "factor_lineage": factor_lineage,
            "compose_method": compose_method,
            "risk_rule": risk_rule,
            "research_input_key": frozen_input_key,
            "forward_return_label": EXECUTABLE_FORWARD_LABEL,
        },
        "risk_control": risk_control,
        "is_validation": _v_serial(v),
        "gate": {
            # 这里只跑到 2024-07~2025-12 历史确认；没有策略前向 runner，
            # 因而绝不能把历史通过写成可投产 passed/PASS。
            "passed": False,
            "historical_confirmation_passed": bool(final_passed),
            "historical_gate_passed": bool(historical_passed),
            "window_role": "historical_confirmation",
            "verdict": "HISTORICAL_PASS" if final_passed else "NO-GO",
            "reason": final_reason,
            "full": ev.get("full"),
            "full_cycle": full_cycle,
            "oos_ic": ev.get("oos_ic"),
            "fold_pass": ev.get("fold_pass"),
            "sensitivity_pass": ev.get("sensitivity_pass"),
            "sensitivity": ev.get("sensitivity"),
            "checks": ev.get("checks"),
            "risk_control": risk_control,
        },
        "annotations": annotation,
        "evaluated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }


def _annotate(panel: Panel, factor: pd.DataFrame, weights: dict,
              hold: int, is_dates: list, oos_dates: list,
              factors: dict | None = None,
              risk_on: pd.Series | None = None) -> dict:
    """策略层诊断（只加注不替换门槛）。"""
    boundary = str(max(oos_dates)) if oos_dates else config.HISTORICAL_TEST_END
    fwd = executable_forward_return(panel, hold, boundary=boundary)
    out: dict = {}
    # OOS IC（复用闸门证据口径）
    try:
        from .metrics import cross_sectional_ic, ic_stats
        st = ic_stats(cross_sectional_ic(factor, fwd, oos_dates), hold=hold)
        out["oos_ic_stats"] = {k: (round(float(st[k]), 4) if pd.notna(st[k])
                                   else None)
                               for k in ("ic_mean", "ic_ir", "t_stat", "n_dates")}
    except Exception:
        out["oos_ic_stats"] = None
    # 单因子贡献：各自 OOS rank-IC（中性化后），识别正/负贡献
    per_factor: dict[str, dict] = {}
    for name, w in (weights or {}).items():
        if not float(w):
            continue
        try:
            f = (factors or {}).get(name)
            if f is None:
                _require_runnable_source(name)
                f = REBUILDERS[name](panel)
            if risk_on is not None:
                f = f.where(risk_on.reindex(f.index, fill_value=False), axis=0)
            f_neut = f
            # 中性化后的 IC（与门槛同口径）
            from .preprocess import neutralize
            f_neut = neutralize(f, build_exposures(panel))
            stf = ic_stats(cross_sectional_ic(f_neut, fwd, oos_dates), hold=hold)
            per_factor[name] = {
                "weight": float(w),
                "neutralized_oos_ic_ir": (round(float(stf["ic_ir"]), 3)
                                          if pd.notna(stf["ic_ir"]) else None),
                "neutralized_oos_ic_mean": (round(float(stf["ic_mean"]), 4)
                                            if pd.notna(stf["ic_mean"]) else None),
            }
        except Exception as _e:
            per_factor[name] = {"weight": float(w), "error": str(_e)[:80]}
    out["per_factor_contribution"] = per_factor
    return out


def _honesty_annotations(panel: Panel, is_dates: list, oos_dates: list) -> dict:
    """诚实披露：历史确认窗噪声级 + 基准口径 + 回测→实盘衰减。

    依据 2026-08-15 六源 Web 研究（因子防伪 / 跑不赢基准根因）：
      - OOS 12-24 个月对月度 IC 几乎不显著（t≈1.1-2.2），≥36-60 个月才够证据级；
      - 上证是价格指数（不含分红），上证全收益较其年化高约 +2.2%；
      - McLean & Pontiff (2016) 已发表因子 OOS 平均低 26%、发表后低 58%。
    """
    months = sorted({(d.year, d.month) for d in oos_dates})
    n_months = len(months)
    return {
        "oos_window": {
            "n_months": n_months,
            "role": "historical_confirmation",
            "note": (f"历史确认窗共 {n_months} 个月，旧系统已反复查看，不能视为盲测 OOS。"
                     "月度 IC 显著性 t≈mean·√T/σ，此样本量"
                     " t≈1.1-2.2 属噪声级（防伪研究：≥36-60 个月才够证据级）。"
                     " 单年/单折结论降级为参考，不视为可投证据。"),
        },
        "benchmark": {
            "note": ("基准为上证 sh000001 价格指数：总股本加权 + 不含分红。"
                     "上证全收益(000888)较价格指数年化高约 +2.2%（2020-07 基日累计 "
                     "+8.2%，雪球/证券日报）——跑赢上证价格指数需再扣回此折扣才是真实超额。"),
        },
        "backtest_to_live": {
            "note": ("回测→实盘系统性衰减：已发表因子样本外平均低 26%、发表后低 58%"
                     "（McLean & Pontiff 2016）。任何 PASS 均为研究假说，非实盘承诺。"),
        },
    }


def _v_serial(v: dict) -> dict:
    """run_validation 结果序列化（JSON 安全，去掉不可序列化对象）。"""
    out: dict = {
        "passed": bool(v.get("passed")),
        "stage": v.get("stage"),
    }
    for key in ("L0", "L1", "L2", "IC", "neutralized_ic_mean", "neutralized_ic_ir",
                "raw_ic_mean", "raw_ic_ir", "neutralize_mode", "ls_annualized",
                "ic_half_life", "turnover", "capacity", "monotonic"):
        if key in v:
            val = v[key]
            if isinstance(val, (float, np.floating)):
                val = None if (np.isnan(val) or np.isinf(val)) else round(float(val), 4)
            elif isinstance(val, (np.integer,)):
                val = int(val)
            out[key] = val
    # L2 保留率等嵌套 dict 尽量扁平化
    for key in ("L2",):
        if isinstance(v.get(key), dict):
            for k2, v2 in v[key].items():
                if isinstance(v2, (float, np.floating)) and pd.notna(v2):
                    out[f"L2_{k2}"] = round(float(v2), 4)
    return out


# ════════════════════════════════════════════════════════════════
# M3.2 专业策略诊断报告（只加注不替换门槛）
# ════════════════════════════════════════════════════════════════
_BENCH_DIR = Path(__file__).resolve().parent.parent / "research" / "data" / "current"
_BENCH_NAMES = ["sh000001", "sh000300", "sh000905", "sh000852"]  # 上证/沪深300/中证500/中证1000


def _load_benchmark(name: str) -> pd.Series | None:
    """读 research/data/current/{name}.csv → open 序列（DatetimeIndex）。"""
    p = _BENCH_DIR / f"{name}.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p, usecols=["date", "open"])
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")["open"].astype(float)


def _bench_row(strategy: pd.Series, bench: pd.Series, basis: str) -> dict:
    """一行多基准对比。basis 如实标注价格指数或复权总收益口径。"""
    strategy = pd.Series(strategy, dtype=float).sort_index()
    bench = pd.Series(bench, dtype=float).reindex(strategy.index)
    valid = (strategy.notna() & bench.notna()
             & np.isfinite(strategy) & np.isfinite(bench))
    requested = int(len(strategy))
    n = int(valid.sum())
    coverage = n / requested if requested else 0.0
    if n < 30 or n != requested:
        return {
            "basis": basis, "n_days": n, "requested_days": requested,
            "coverage": float(coverage),
            "note": ("样本不足" if n < 30 else "同期基准覆盖不完整"),
        }
    both = pd.concat(
        [strategy.loc[valid], bench.loc[valid]], axis=1,
        keys=["strategy", "bench"],
    )
    s, b = both["strategy"], both["bench"]
    s_ann = (1.0 + s).prod() ** (252.0 / n) - 1.0
    b_ann = (1.0 + b).prod() ** (252.0 / n) - 1.0
    s_eq = (1.0 + s).cumprod()
    b_eq = (1.0 + b).cumprod()
    return {
        "basis": basis,
        "n_days": n,
        "requested_days": requested,
        "coverage": float(coverage),
        "strategy_annualized_pct": round(float(s_ann) * 100, 2),
        "benchmark_annualized_pct": round(float(b_ann) * 100, 2),
        "excess_annual_pct": round(float(s_ann - b_ann) * 100, 2),
        "corr_strategy_bench": round(float(np.corrcoef(s, b)[0, 1]), 3),
        "strategy_max_drawdown_pct": round(float(((s_eq - s_eq.cummax()) / s_eq.cummax()).min()) * 100, 2),
        "benchmark_max_drawdown_pct": round(float(((b_eq - b_eq.cummax()) / b_eq.cummax()).min()) * 100, 2),
    }


def _multi_benchmark(panel: Panel, strategy_daily: pd.Series, costs: dict) -> dict:
    """OOS 策略 vs 多基准；全部严格对齐 open t→t+1。"""
    out: dict = {}
    o2o = (panel.adj_open.shift(-1) / panel.adj_open) - 1.0
    ew = o2o.mean(axis=1).reindex(strategy_daily.index)
    out["equal_weight_o2o"] = _bench_row(
        strategy_daily, ew, "adjusted-open total-return t→t+1",
    )
    for name in _BENCH_NAMES:
        open_price = _load_benchmark(name)
        if open_price is None:
            continue
        oo = open_price.shift(-1).div(open_price).sub(1.0).reindex(
            strategy_daily.index
        )
        out[name] = _bench_row(strategy_daily, oo, "price-index open t→t+1")
    return out


def _sub_factor_decomposition(panel: Panel, weights: dict, hold: int, top_n: int,
                              oos_dates: list, costs: dict,
                              factors: dict | None = None,
                              risk_on: pd.Series | None = None) -> dict:
    """组合归因（组合级）：每个 active 因子单独做多跑同一 OOS 组合口径，看谁在驱动组合。

    M7：factors 可含用户挖掘配方因子——单因子组合用同一张因子图重建。
    """
    out: dict = {}
    boundary = str(max(oos_dates)) if oos_dates else config.HISTORICAL_TEST_END
    fwd = executable_forward_return(panel, hold, boundary=boundary)
    for name, w in (weights or {}).items():
        if not float(w):
            continue
        f = compose_factor(panel, {name: 1.0}, factors)
        if risk_on is not None:
            f = f.where(risk_on.reindex(f.index, fill_value=False), axis=0)
        bt = run_backtest(panel, f, oos_dates, hold=hold, q=config.QUANTILE,
                          costs=costs, direction=1.0)
        st = ic_stats(cross_sectional_ic(f, fwd, oos_dates), hold=hold)
        ok = bool(bt.get("ok"))
        t = daily_t_stat(bt.get("daily_ret", pd.Series(dtype=float)), hold) if ok else None
        out[name] = {
            "weight": float(w),
            "oos_annual_pct": (round(float(bt["annual_return"]) * 100, 2)
                               if ok and pd.notna(bt.get("annual_return")) else None),
            "oos_t": round(float(t), 3) if (ok and t is not None) else None,
            "oos_excess_pct": (round(float(bt["excess_annual"]) * 100, 2)
                               if ok and pd.notna(bt.get("excess_annual")) else None),
            "oos_ic": round(float(st["ic_mean"]), 4) if pd.notna(st["ic_mean"]) else None,
            "oos_ic_ir": round(float(st["ic_ir"]), 3) if pd.notna(st["ic_ir"]) else None,
        }
    return out


def _rolling_wf_ic(factor: pd.DataFrame, fwd: pd.DataFrame,
                   oos_dates: list, hold: int = config.HOLD_DAYS) -> dict:
    """OOS 5 折 walk-forward：每折内 OOS IC / IC_IR / t（信号时间稳定性）。"""
    from quantqc.stats import walk_forward_folds
    folds = walk_forward_folds([str(d) for d in oos_dates], n_folds=5)
    rows = []
    irs = []
    for fd in folds:
        val_lo, val_hi = pd.Timestamp(fd["val"][0]), pd.Timestamp(fd["val"][-1])
        vd = [d for d in oos_dates if val_lo <= d <= val_hi]
        st = ic_stats(cross_sectional_ic(factor, fwd, vd), hold=hold)
        ir = float(st["ic_ir"]) if pd.notna(st["ic_ir"]) else None
        if ir is not None:
            irs.append(ir)
        rows.append({
            "val": fd["val"],
            "n_dates": int(st["n_dates"]),
            "ic_mean": round(float(st["ic_mean"]), 4) if pd.notna(st["ic_mean"]) else None,
            "ic_ir": round(ir, 3) if ir is not None else None,
            "t_stat": round(float(st["t_stat"]), 2) if pd.notna(st["t_stat"]) else None,
        })
    return {
        "folds": rows,
        "ic_ir_std": round(float(np.std(irs)), 3) if len(irs) > 1 else None,
        "ic_ir_pos_folds": int(sum(1 for x in irs if x > 0)),
    }


def _deflated_sharpe(sharpe_ann: float, daily: pd.Series, n_trials: int) -> float | None:
    """Bailey & López de Prado (2014) DSR：N 次实验下的期望最大 Sharpe 折减后显著性。

    N 为「此前尝试过的实验数」假设——研究管道试得越多，观测 Sharpe 越可能是运气，
    报告多个 N 的敏感性，不替用户选 N（诚实提示，非门槛）。
    """
    from scipy.stats import norm
    if daily is None or len(daily) < 30 or sharpe_ann is None or not np.isfinite(sharpe_ann):
        return None
    sr = sharpe_ann / np.sqrt(252.0)
    n = len(daily)
    z = sr * np.sqrt(n - 1)
    if n_trials <= 1:
        e_max = 0.0
    else:
        ln = np.log(float(n_trials))
        e_max = np.sqrt(2 * ln) - (np.log(ln) + np.log(4 * np.pi)) / (2 * np.sqrt(2 * ln))
    skew = float(pd.Series(daily).skew())
    kurt = float(pd.Series(daily).kurtosis())
    v = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if not np.isfinite(v) or v <= 0:
        return None
    return round(float(norm.cdf((z - e_max) / np.sqrt(v))), 4)


# 资金敏感性维度（M9 续 2026-08-17）：同一因子/持有/topN，只换资金档，
# 量化回测从不同资金维度回——小市值/低换手类在 20 万会因 100 股整手退化空仓。
_CAPITAL_SWEEP_LEVELS = (200_000, 500_000, 2_000_000, 5_000_000, 10_000_000, 50_000_000)


def _capital_sweep(panel: Panel, factor: pd.DataFrame, hold: int, top_n: int,
                   costs: dict, capitals: tuple = _CAPITAL_SWEEP_LEVELS,
                   expected_input_key: str | None = None) -> list[dict]:
    """同一组合在 6 档资金下的全周期表现（与主报告同引擎同口径）。

    只加注不替换门槛——资金档结果不参与任何晋升判定；20 万档的退化
    （100 股整手买不起小市值 → 空仓 → 负年化）是如实展示，不是参数。
    """
    rows: list[dict] = []
    base = dict(costs)
    for cap in capitals:
        c = {**base, "capital": float(cap)}
        out = full_cycle_evaluation(panel, factor, hold=hold, q=base.get("q", config.QUANTILE),
                                    costs=c, direction=1.0,
                                    expected_input_key=expected_input_key)
        rows.append({
            "label": f"{cap // 10000}万",
            "annual_return": (round(float(out["annual_return"]), 4)
                              if out.get("ok") and pd.notna(out.get("annual_return")) else None),
            "excess_annual": (round(float(out["shanghai_excess_annual"]), 4)
                              if out.get("ok") and pd.notna(out.get("shanghai_excess_annual")) else None),
            "max_drawdown": (round(float(out["max_drawdown"]), 4)
                             if out.get("ok") and pd.notna(out.get("max_drawdown")) else None),
            "sharpe": (round(float(out["sharpe"]), 3)
                       if out.get("ok") and pd.notna(out.get("sharpe")) else None),
            "ok": bool(out.get("ok")),
            "reason": None if out.get("ok") else out.get("reason", "回测失败"),
        })
    return rows


def _historical_equity_curve(
    bt: dict,
    historical_dates: list,
    *,
    research_input_key_value: str,
    execution_audit: dict | None,
    execution_audit_sha256: str | None,
) -> dict:
    """Serialize the exact unified-backtest NAV, benchmark and drawdown."""
    if not bt.get("ok"):
        return {
            "schema": "unified_historical_curve_v1",
            "available": False,
            "reason": bt.get("reason", "统一历史回测失败"),
        }
    pair, alignment = _aligned_shanghai_returns(
        bt.get("daily_ret", pd.Series(dtype=float)), historical_dates,
    )
    if pair is None:
        return {
            "schema": "unified_historical_curve_v1",
            "available": False,
            "reason": alignment.get("reason", "上证基准无法严格对齐"),
            "alignment": alignment,
        }
    source = alignment.get("source") or {}
    benchmark_sha = str(source.get("sha256") or "")
    if not benchmark_sha or not research_input_key_value.endswith(
        f":shanghai:{benchmark_sha}"
    ):
        return {
            "schema": "unified_historical_curve_v1",
            "available": False,
            "reason": "曲线基准文件与冻结 research_input_key 不一致",
            "alignment": alignment,
        }
    days = execution_audit.get("days") if isinstance(
        execution_audit, dict
    ) else None
    if not isinstance(days, list) or not days:
        return {
            "schema": "unified_historical_curve_v1",
            "available": False,
            "reason": "统一执行审计缺少逐日结算日期",
            "alignment": alignment,
        }
    next_dates: dict[pd.Timestamp, str] = {}
    for day in days:
        if not isinstance(day, dict):
            continue
        try:
            signal_date = pd.Timestamp(day.get("date")).normalize()
            next_date = pd.Timestamp(day.get("next_date")).date().isoformat()
        except (TypeError, ValueError):
            continue
        if signal_date in next_dates and next_dates[signal_date] != next_date:
            return {
                "schema": "unified_historical_curve_v1",
                "available": False,
                "reason": "统一执行审计同一收益日起点存在冲突结算日",
                "alignment": alignment,
            }
        next_dates[signal_date] = next_date
    settlement_dates = [next_dates.get(pd.Timestamp(day).normalize()) for day in pair.index]
    if any(day is None for day in settlement_dates) or settlement_dates != sorted(
        settlement_dates
    ) or len(set(settlement_dates)) != len(settlement_dates):
        return {
            "schema": "unified_historical_curve_v1",
            "available": False,
            "reason": "统一执行审计与策略收益结算日期无法一一对应",
            "alignment": alignment,
        }

    strategy_nav = pair["strategy"].add(1.0).cumprod()
    benchmark_nav = pair["shanghai"].add(1.0).cumprod()
    drawdown = strategy_nav.div(strategy_nav.cummax().clip(lower=1.0)).sub(1.0)
    first_date = pd.Timestamp(pair.index[0]).date().isoformat()
    points = [{
        "date": first_date,
        "nav": 1.0,
        "benchmark_nav": 1.0,
        "drawdown_pct": 0.0,
    }]
    points.extend({
        "date": str(settlement_date),
        "nav": round(float(nav), 10),
        "benchmark_nav": round(float(benchmark), 10),
        "drawdown_pct": round(float(dd) * 100.0, 6),
    } for settlement_date, nav, benchmark, dd in zip(
        settlement_dates, strategy_nav, benchmark_nav, drawdown,
    ))
    payload = {
        "schema": "unified_historical_curve_v1",
        "available": True,
        "window_role": "historical_confirmation",
        "basis": "strategy=open t→t+1 net of full costs · shanghai_price_index=open t→t+1",
        "research_input_key": research_input_key_value,
        "execution_audit_sha256": execution_audit_sha256,
        "alignment": alignment,
        "n_return_days": int(len(pair)),
        "start": points[0]["date"],
        "end": points[-1]["date"],
        "points": points,
    }
    payload["curve_sha256"] = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    return payload


def diagnose_strategy(spec: dict, panel: Panel | None = None,
                      factors: dict | None = None,
                      expected_input_key: str | None = None,
                      include_extended: bool = True) -> dict:
    """M3.2 专业策略诊断报告：信号级（复用 M1.3 diagnostics）+ 组合级 + 多基准 + 滚动 WF + DSR。

    只加注不替换门槛——本报告的任一数字都不参与晋升判定（晋升只认
    evaluate_strategy 的 run_scientific_gate），避免诊断层偷偷放宽/收紧。

    M7：注册表配方始终按 seal 校验后重建（同一因子图贯穿组合/单因子/归因）。
    """
    weights = spec.get("factors", DEFAULT_WEIGHTS)
    active_ids = [k for k, v in weights.items() if float(v)] or list(DEFAULT_WEIGHTS)
    _preflight_factor_sources(active_ids)
    frozen_input_key = expected_input_key or research_input_key()
    if research_input_key() != frozen_input_key:
        raise RuntimeError("策略诊断输入已变化，拒绝写入旧假设证据")
    panel = panel if panel is not None else load_thin_panel()
    windows = split_research_windows(panel.dates)
    discovery_dates = windows["discovery"]
    historical_dates = windows["historical_test"]
    compose_method = spec.get("compose_method", "rank_weighted_v1")
    risk_rule = str(spec.get("risk_rule") or "none")
    hold = int(spec.get("holding_days", config.HOLD_DAYS))
    top_n = int(spec.get("top_n", 30))
    capital = float(spec.get("account_capital", 200_000.0))
    one_way_cost_bps = float(spec.get(
        "one_way_cost_bps", float(config.COSTS.get("slippage", 0.0)) * 10_000.0))
    rebal_buffer = int(spec.get(
        "rebal_buffer", int(config.COSTS.get("rebal_buffer", 0))))
    entry_rule = str(spec.get("entry_rule") or "none")
    exposure_rule = str(spec.get("exposure_rule") or "none")
    costs = {**config.COSTS, "slippage": one_way_cost_bps / 10_000.0,
             "n_positions": top_n, "capital": capital,
             "rebal_buffer": rebal_buffer, "entry_rule": entry_rule,
             "exposure_rule": exposure_rule}

    controlled = load_factor_map(panel, active_ids)
    factors = controlled if factors is None else {**factors, **controlled}
    factors = {
        name: mask_signal_eligible(panel, frame)
        for name, frame in factors.items()
    }
    risk_on, risk_control = _strategy_risk_mask(panel, risk_rule, windows)
    if compose_method in ("ic_weighted", "corr_ic_weighted"):
        method = "corr_ic" if compose_method == "corr_ic_weighted" else "ic"
        factor, weights_used = compose_ic_weighted(
            panel, weights, factors,
            [d for d in discovery_dates if bool(risk_on.get(d, False))],
            hold=hold, method=method)
    else:
        factor = compose_factor(panel, weights, factors)
        weights_used = {k: float(v) for k, v in weights.items() if float(v)}
    factor = factor.where(risk_on.reindex(factor.index, fill_value=False), axis=0)
    factor_lineage = _factor_lineage(weights_used)
    discovery_fwd = executable_forward_return(
        panel, hold, boundary=config.DISCOVERY_END)
    historical_boundary = (str(max(historical_dates)) if historical_dates
                           else config.HISTORICAL_TEST_END)
    historical_fwd = executable_forward_return(
        panel, hold, boundary=historical_boundary)
    bt = run_backtest(panel, factor, historical_dates, hold=hold, q=config.QUANTILE,
                      costs=costs, direction=1.0, capture_audit=True)
    ok = bool(bt.get("ok"))
    signal_diagnostics = (compute_diagnostics(
        factor, discovery_fwd, discovery_dates, panel=panel,
        hold=hold, direction=1.0, max_positions=top_n) if discovery_dates else {
            "available": False, "reason": "面板没有发现期交易日",
        })
    execution_audit = bt.get("audit") if ok else None
    execution_audit_sha256 = bt.get("audit_sha256") if ok else None
    if execution_audit is not None:
        execution_audit = {**execution_audit,
                           "strategy_risk_control": risk_control}
        execution_audit_sha256 = hashlib.sha256(json.dumps(
            execution_audit, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
    historical_curve = _historical_equity_curve(
        bt,
        historical_dates,
        research_input_key_value=frozen_input_key,
        execution_audit=execution_audit,
        execution_audit_sha256=execution_audit_sha256,
    )
    return {
        "spec": {
            "name": spec.get("name", "未命名策略"),
            "holding_days": hold,
            "top_n": top_n,
            "capital": capital,
            "one_way_cost_bps": one_way_cost_bps,
            "rebal_buffer": rebal_buffer,
            "entry_rule": entry_rule,
            "exposure_rule": exposure_rule,
            "factors": weights_used,
            "factor_lineage": factor_lineage,
            "compose_method": compose_method,
            "risk_rule": risk_rule,
            "research_input_key": frozen_input_key,
            "forward_return_label": EXECUTABLE_FORWARD_LABEL,
        },
        "risk_control": risk_control,
        "signal_diagnostics_is": signal_diagnostics,
        "oos_metrics": {
            "annual_return_pct": (round(float(bt["annual_return"]) * 100, 2)
                                  if ok and pd.notna(bt.get("annual_return")) else None),
            "sharpe": round(float(bt.get("sharpe")), 3) if ok and pd.notna(bt.get("sharpe")) else None,
            "max_drawdown_pct": round(float(bt.get("max_drawdown")) * 100, 2)
                                if ok and pd.notna(bt.get("max_drawdown")) else None,
            "win_rate_pct": round(float(bt.get("win_rate")) * 100, 2)
                            if ok and pd.notna(bt.get("win_rate")) else None,
            "t_stat": round(float(daily_t_stat(bt.get("daily_ret", pd.Series(dtype=float)), hold)), 3)
                      if ok else None,
            "internal_equal_weight_bench_pct": (round(float(bt.get("benchmark_annual")) * 100, 2)
                                                if ok and pd.notna(bt.get("benchmark_annual")) else None),
            "internal_excess_pct": (round(float(bt.get("excess_annual")) * 100, 2)
                                    if ok and pd.notna(bt.get("excess_annual")) else None),
            "calmar": (round(float(bt.get("calmar")), 3)
                       if ok and bt.get("calmar") is not None else None),
            "realized_volatility_pct": (
                round(float(bt.get("realized_volatility")) * 100, 2)
                if ok and pd.notna(bt.get("realized_volatility")) else None
            ),
            "average_gross_exposure_pct": (
                round(float(bt.get("average_actual_gross_exposure")) * 100, 2)
                if ok and pd.notna(bt.get("average_actual_gross_exposure")) else None
            ),
            "average_cash_weight_pct": (
                round(float(bt.get("average_cash_weight")) * 100, 2)
                if ok and pd.notna(bt.get("average_cash_weight")) else None
            ),
            "minimum_cash_weight_pct": (
                round(float(bt.get("minimum_cash_weight")) * 100, 2)
                if ok and pd.notna(bt.get("minimum_cash_weight")) else None
            ),
            "maximum_single_weight_pct": (
                round(float(bt.get("maximum_single_weight")) * 100, 2)
                if ok and pd.notna(bt.get("maximum_single_weight")) else None
            ),
            "average_turnover_pct": (
                round(float(bt.get("signal_churn")) * 100, 2)
                if ok and pd.notna(bt.get("signal_churn")) else None
            ),
            "total_fees": (round(float(bt.get("total_fees")), 2)
                           if ok and pd.notna(bt.get("total_fees")) else None),
            "traded_notional": (round(float(bt.get("traded_notional")), 2)
                                if ok and pd.notna(bt.get("traded_notional")) else None),
            "entry_order_fill_rate_pct": (
                round(float(bt.get("entry_order_fill_rate")) * 100, 2)
                if ok and bt.get("entry_order_fill_rate") is not None else None
            ),
            "entry_order_count": (
                int(bt.get("entry_order_count", 0)) if ok else None
            ),
            "entry_order_cancelled_count": (
                int(bt.get("entry_order_cancelled_count", 0)) if ok else None
            ),
            "previous_close_limit_cancelled_count": (
                int(bt.get("previous_close_limit_cancelled_count", 0))
                if ok else None
            ),
        },
        "execution_audit": execution_audit,
        "execution_audit_sha256": execution_audit_sha256,
        "historical_curve": historical_curve,
        "multi_benchmark": _multi_benchmark(panel, bt["daily_ret"], costs) if ok else {"error": bt.get("reason", "回测失败")},
        "sub_factor_decomposition": (_sub_factor_decomposition(
            panel, weights_used, hold, top_n, historical_dates, costs, factors,
            risk_on=risk_on) if include_extended else {}),
        "rolling_wf_ic": _rolling_wf_ic(
            factor, historical_fwd, historical_dates, hold=hold),
        "deflated_sharpe": {
            f"n_trials={n}": _deflated_sharpe(bt.get("annual_return"), bt.get("daily_ret"), n)
            for n in (1, 10, 50, 500)
        } if ok else None,
        "capital_sweep": (_capital_sweep(
            panel, factor, hold, top_n, costs,
            expected_input_key=frozen_input_key)
                          if ok and include_extended else []),
        "extended_diagnostics": {
            "included": bool(include_extended),
            "deferred": [] if include_extended else [
                "sub_factor_decomposition", "capital_sweep",
            ],
            "reason": None if include_extended else (
                "不参与晋升的重复全周期回测已移出交互请求关键路径"
            ),
        },
        "diagnosed_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }


__all__ = [
    "REBUILDERS", "DEFAULT_WEIGHTS",
    "rebuild_v4_factors", "compose_factor",
    "estimate_ic_weights", "compose_ic_weighted",
    "evaluate_strategy", "diagnose_strategy",
    "load_factor_map",
]
