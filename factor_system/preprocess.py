"""M1.1 预处理链路：去极值 → 标准化 → 中性化（行业+市值回归残差）。

专业因子研究（WorldQuant Alpha101 / Qlib Alpha158 / A 股实务）的标准做法：
  原始因子 → 横截面去极值(winsorize) → 标准化(z/rank) → 中性化(行业/市值回归残差)
  → 再进 IC 分析。

M1.1 聚焦**中性化**链路。实现用 Frisch-Waugh 定理：把「行业哑变量」换成
「行业内去均值」——行业内去均值等价于吸收全部行业固定效应，再对去均值后的
log(流通市值) 做一元回归取残差，两步合成即「行业哑变量 + log_mv」的联合 OLS
残差（无需构造 84 维哑变量矩阵）。逐日纯 numpy 向量化，8GB 单机友好。

诚实口径（披露要点）：
  - 中性化只在候选因子输出层做一次，再进 IC；同时输出 raw IC 与 neutralized IC
    双列证据——行业色重的因子（换手/流动性/规模代理）中性化可能杀真信号，必须双报告。
  - 行业缺失，或 meta 只有无生效日期的当前快照 → 仅做市值中性化；
    不拿滚动波动率回归候选，避免机械消掉低波动因子本身。
  - 单成员行业截面内无变异：行业内去均值后该成员残差为 0，不产生伪 alpha。
  - Rank IC（Spearman）对单调变换不变，故去极值/标准化对 IC 无影响；本模块的
    winsorize_cs / standardize_cs 供非秩口径或下游换手/容量估计使用。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .data import Panel


# ════════════════════════════════════════════════════════════════
# 去极值 / 标准化（供非秩口径使用；Rank IC 下对显著性无影响）
# ════════════════════════════════════════════════════════════════

def winsorize_cs(df: pd.DataFrame, lower: float = 0.01, upper: float = 0.99) -> pd.DataFrame:
    """逐日横截面去极值：每行（每个交易日）在分位数处截断。"""
    return df.clip(
        lower=df.quantile(lower, axis=1), upper=df.quantile(upper, axis=1), axis=0)


def standardize_cs(df: pd.DataFrame) -> pd.DataFrame:
    """逐日横截面 z-score（跨截面均值 0 / 标准差 1）。"""
    mu = df.mean(axis=1)
    sd = df.std(axis=1, ddof=0)
    return df.sub(mu, axis=0).div(sd.replace(0, np.nan), axis=0)


# ════════════════════════════════════════════════════════════════
# 中性化
# ════════════════════════════════════════════════════════════════

def industry_exposures(panel: Panel) -> dict:
    """轻量行业暴露（GP/模板 neutralize 算子用；不建 log_mv/s_tilde，~1MB 级）。

    返回 dict（与 build_exposures 共用结构）：
      mode       "industry+size" | "none"
      codes      pd.Index  panel.codes
      dates      pd.Index  panel.dates
      date_pos   {Timestamp: int}
      ind_labs   ndarray(n_codes,) 行业组标签（mode=="none" 时为 None）
      n_groups   int 行业组数
      industry_coverage float 有时间依据且覆盖整个研究窗口的行业比例
    """
    codes = pd.Index(panel.codes)
    dates = pd.Index(panel.dates)
    d2p = {d: i for i, d in enumerate(dates)}
    ind_labs = None
    n_groups = 0
    coverage = 0.0
    meta = panel.meta
    if (meta is not None and {"ts_code", "industry"}.issubset(meta.columns)
            and len(meta) > 0 and len(dates) > 0
            and not meta["ts_code"].duplicated().any()):
        # 当前引擎只能表达「整个研究窗口内不变」的行业标签。
        # 无生效日的当前快照、窗口内变更记录都 fail-closed，不复制到历史。
        start_col = ("effective_date" if "effective_date" in meta.columns else
                     "start_date" if "start_date" in meta.columns else None)
        if start_col is not None:
            aligned = meta.set_index("ts_code").reindex(codes)
            ind = aligned["industry"].fillna("").astype(str)
            start = pd.to_datetime(aligned[start_col], errors="coerce")
            first, last = pd.Timestamp(dates.min()), pd.Timestamp(dates.max())
            valid = start.notna() & start.le(first)
            if "end_date" in aligned.columns:
                end = pd.to_datetime(aligned["end_date"], errors="coerce")
                valid &= end.isna() | end.ge(last)
            ind = ind.where(valid, "")
            coverage = float((ind != "").sum()) / max(1, len(codes))
            if coverage >= 0.5:
                # 空字符串/缺失 → 归入"未知行业"独立组
                labs, _ = pd.factorize(ind)
                ind_labs = labs.astype(np.int64)
                n_groups = int(ind_labs.max()) + 1
    return {
        "mode": "industry+size" if ind_labs is not None else "none",
        "codes": codes, "dates": dates, "date_pos": d2p,
        "ind_labs": ind_labs, "n_groups": n_groups,
        "industry_coverage": coverage,
    }


def build_exposures(panel: Panel, vol_window: int = 20) -> dict:
    """预计算中性化协变量（候选无关，一批只建一次，避免逐候选重复构建）。

    返回 dict：
      mode            "industry+size" | "size_only"（降级，看板如实展示）
      codes           pd.Index  panel.codes（对齐基准）
      dates           pd.Index  panel.dates
      date_pos        {Timestamp: int} 日期→行位置
      ind_labs        ndarray(n_codes,) 行业组标签（仅 industry+size 模式）
      n_groups        int 行业组数
      s_tilde         ndarray(n_dates, n_codes) 行业内去均值后的 log10(float_mv)
                      （industry+size 模式预计算；候选无关，仅与数据相关）
      log_mv          DataFrame  log10(float_mv)（size_only 降级模式用）
      industry_coverage float   有时间依据且覆盖整个窗口的行业比例
    """
    base = industry_exposures(panel)
    codes, dates, d2p = base["codes"], base["dates"], base["date_pos"]
    ind_labs, n_groups = base["ind_labs"], base["n_groups"]
    coverage = base["industry_coverage"]

    log_mv = np.log10(panel.float_mv.clip(lower=1e6)).replace([np.inf, -np.inf], np.nan)

    if base["mode"] == "industry+size":
        sv = log_mv.values
        s_tilde = np.empty_like(sv)
        for i in range(len(dates)):
            row = sv[i]
            m = np.isfinite(row)
            if m.sum() == 0:
                s_tilde[i] = row
                continue
            labs = ind_labs[m]
            yy = row[m]
            cnt = np.bincount(labs, minlength=n_groups)
            with np.errstate(divide="ignore", invalid="ignore"):
                mean = np.bincount(labs, weights=yy) / np.maximum(cnt, 1)
            rt = row.copy()
            rt[m] = yy - mean[labs]
            s_tilde[i] = rt
        return {
            "mode": "industry+size",
            "codes": codes, "dates": dates, "date_pos": d2p,
            "ind_labs": ind_labs, "n_groups": n_groups,
            "s_tilde": s_tilde, "log_mv": log_mv, "vol20": None,
            "industry_coverage": coverage,
        }

    # ── 降级：仅市值。波动率本身是合法候选，不能作为默认控制变量。 ──
    return {
        "mode": "size_only",
        "codes": codes, "dates": dates, "date_pos": d2p,
        "ind_labs": None, "n_groups": 0,
        "s_tilde": None, "log_mv": log_mv, "vol20": None,
        "industry_coverage": coverage,
    }


def neutralize(factor: pd.DataFrame, exposures: dict) -> pd.DataFrame:
    """按协变量对因子逐日做横截面中性化，返回同形状残差面板。

    factor 列/索引需与 panel.codes / panel.dates 对齐（compute_factor 产物天然对齐；
    未对齐列/日用整数 get_indexer 定位，-1 直接置 NaN，绝不张冠李戴）。
    """
    if exposures["mode"] == "industry+size":
        return _neutralize_industry_size(factor, exposures)
    return _neutralize_size(factor, exposures)


def _neutralize_industry_size(factor: pd.DataFrame, exp: dict) -> pd.DataFrame:
    """行业 + log_mv 联合 OLS 残差（Frisch-Waugh：行业内去均值吸收行业固定效应）。

    resid = y_tilde − β·s_tilde，其中 s_tilde = 行业内去均值后的 log_mv，
    β = Σ(y_tilde·s_tilde)/Σ(s_tilde²)（逐日）。残差与行业哑变量及 log_mv 均正交。
    """
    d2p = exp["date_pos"]
    ci = exp["codes"].get_indexer(factor.columns)      # 列对齐（-1 = 不在面板）
    ind_labs = exp["ind_labs"]
    s_tilde = exp["s_tilde"]
    n_groups = exp["n_groups"]
    out = np.full(factor.shape, np.nan, dtype=np.float64)
    vals = factor.values
    for i, d in enumerate(factor.index):
        p = d2p.get(d, -1)
        if p < 0:
            continue
        y = vals[i]
        ss = s_tilde[p, ci]
        m = np.isfinite(y) & np.isfinite(ss) & (ci >= 0)
        if m.sum() < 30:
            continue
        labs = ind_labs[ci[m]]
        yy = y[m]
        ssv = ss[m]
        cnt = np.bincount(labs, minlength=n_groups)
        with np.errstate(divide="ignore", invalid="ignore"):
            mean_y = np.bincount(labs, weights=yy) / np.maximum(cnt, 1)
        y_tilde = yy - mean_y[labs]
        denom = float(ssv @ ssv)
        beta = float(y_tilde @ ssv) / denom if denom > 1e-12 else 0.0
        resid = y_tilde - beta * ssv
        out[i, m] = resid
    return pd.DataFrame(out, index=factor.index, columns=factor.columns)


def _neutralize_size(factor: pd.DataFrame, exp: dict) -> pd.DataFrame:
    """行业数据不可用时，仅对 log_mv 做横截面 OLS 残差化。"""
    d2p = exp["date_pos"]
    ci = exp["codes"].get_indexer(factor.columns)
    log_mv = exp["log_mv"]
    out = np.full(factor.shape, np.nan, dtype=np.float64)
    vals = factor.values
    for i, d in enumerate(factor.index):
        p = d2p.get(d, -1)
        if p < 0:
            continue
        y = vals[i]
        ss = log_mv.values[p][ci]
        m = np.isfinite(y) & np.isfinite(ss) & (ci >= 0)
        if m.sum() < 30:
            continue
        yc = y[m]
        yc = yc - yc.mean()
        sc = ss[m] - ss[m].mean()
        denom = float(sc @ sc)
        beta = float(yc @ sc) / denom if denom > 1e-12 else 0.0
        resid = yc - beta * sc
        out[i, m] = resid
    return pd.DataFrame(out, index=factor.index, columns=factor.columns)


def demean_industry(factor: pd.DataFrame, exposures: dict) -> pd.DataFrame:
    """行业内去均值残差（仅行业固定效应，无市值项）——GP/模板中性化算子的快速版。

    exposures 须来自 build_exposures 且 mode=="industry+size"（否则原样返回）。
    列与 panel.codes 对齐（未对齐列置 NaN）。比 preprocess.neutralize 少一个市值
    回归步骤，适合在搜索/遗传评估中按算子重复调用（~0.1s/次）。
    """
    if exposures.get("mode") != "industry+size":
        return factor
    d2p = exposures["date_pos"]
    ci = exposures["codes"].get_indexer(factor.columns)
    ind_labs = exposures["ind_labs"]
    n_groups = exposures["n_groups"]
    out = np.full(factor.shape, np.nan, dtype=np.float64)
    vals = factor.values
    for i, d in enumerate(factor.index):
        p = d2p.get(d, -1)
        if p < 0:
            continue
        y = vals[i]
        m = np.isfinite(y) & (ci >= 0)
        if m.sum() < 10:
            continue
        labs = ind_labs[ci[m]]
        yy = y[m]
        cnt = np.bincount(labs, minlength=n_groups)
        with np.errstate(divide="ignore", invalid="ignore"):
            mean = np.bincount(labs, weights=yy) / np.maximum(cnt, 1)
        out[i, m] = yy - mean[labs]
    return pd.DataFrame(out, index=factor.index, columns=factor.columns)


def neutralize_mode_label(mode: str | None) -> str:
    if mode == "industry+size":
        return "行业+市值"
    if mode == "size_only":
        return "仅市值（行业证据缺失降级）"
    return "未中性化"


__all__ = [
    "winsorize_cs", "standardize_cs",
    "build_exposures", "neutralize", "neutralize_mode_label",
]
