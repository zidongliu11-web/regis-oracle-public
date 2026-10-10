"""M1.4 挖掘期前置筛查：IC 序列相关预筛 + 增量 IC（残差学习）。

去重不再滞后到 production 层。L2 通过后立即与既有已入库因子比较：
  - IC 序列相关：候选 IC vs 参考库 IC 逐列 Spearman，|corr|>0.7 → 拒
    （「与 <fid> IC 高相关」）。是 IC 序列相关，不是因子值相关（与 dedupe 口径一致）。
  - 增量 IC：对参考库 dedupe 后代表因子（≤7）逐日横截面回归残差化，重算 IC；
    边际 IC 为负 / 近零（|inc|<0.02）→ 拒（「无增量信息」）。残差学习。
参考库一批只建一次（run_mining 内缓存），避免逐候选重建。8GB 单机串行。
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from . import config
from . import registry as reg
from .data import (EXECUTABLE_FORWARD_LABEL, Panel,
                   executable_forward_return)
from .metrics import cross_sectional_ic, ic_stats
from .search import build_atom_frames, rebuild_factor

from quantqc.stats import dedupe_correlated, factor_correlation_matrix

# M1.4 门槛（首版明确写出，随样本积累可标定）
IC_CORR_REJECT = 0.7      # |IC 相关| 超此即视为重复挖掘
MAX_REPRESENTATIVES = 7   # 增量 IC 回归用的代表因子数（dedupe 后取 ≤7）
INC_IC_FLOOR = 0.02       # 增量 IC |值| 低于此视为近零（无边际信息）
KEEP_STATUSES = (
    "validated", "registered", "historical_pass", "production", "frozen",
)


def _require_executable_label(fwd: pd.DataFrame) -> None:
    label = fwd.attrs.get("factor_system_label", {}).get("id")
    if label != EXECUTABLE_FORWARD_LABEL:
        raise ValueError(
            "预筛只接受可执行 T+1 open→T+H+1 open 收益标签"
        )


def build_reference_library(panel: Panel, dates: list[pd.Timestamp],
                            fwd: pd.DataFrame | None = None,
                            max_factors: int = 40,
                            atom_frames: dict | None = None) -> dict:
    """构建参考因子库：既有已入库因子 → IC 序列面板 + 因子值 + dedupe 代表。

    返回 {"ic_panel": DF(index=date, columns=fid),
          "factors": {fid: factor_df},
          "representatives": [fid,...],   # IC 相关去重后代表（≤MAX_REPRESENTATIVES）
          "skipped": [{"fid","err"}]}
    空库（当前 0 入库因子）时 ic_panel 为空 → prescreen 全部放行（诚实：无先例可比）。
    """
    if fwd is None:
        boundary = str(pd.Timestamp(max(dates)).date()) if dates else None
        fwd = executable_forward_return(
            panel, config.HOLD_DAYS, boundary=boundary,
        )
    _require_executable_label(fwd)
    c = reg._conn()
    rows = reg.factors_by_status(c, *KEEP_STATUSES)[:max_factors]
    c.close()

    factors: dict[str, pd.DataFrame] = {}
    skipped = []
    shared_atoms = atom_frames
    for row in rows:
        fid = row["factor_id"]
        recipe_path = config.REGISTRY_DIR / "recipes" / f"{fid}.json"
        if not recipe_path.exists():
            skipped.append({"fid": fid, "err": "配方缺失"})
            continue
        try:
            recipe = json.loads(recipe_path.read_text("utf-8"))
            if recipe.get("kind") == "genetic" and shared_atoms is None:
                shared_atoms = build_atom_frames(panel)
            f = rebuild_factor(panel, recipe, atom_frames=shared_atoms)
            if f is None:
                skipped.append({"fid": fid, "err": "因子重建失败"})
                continue
            factors[fid] = f
        except Exception as e:
            skipped.append({"fid": fid, "err": str(e)[:120]})

    if not factors:
        return {"ic_panel": pd.DataFrame(), "factors": {},
                "representatives": [], "skipped": skipped}

    series = {}
    for fid, f in factors.items():
        ics = cross_sectional_ic(f, fwd, dates)
        if ics.notna().sum() >= 30:
            series[fid] = ics
    if not series:
        return {"ic_panel": pd.DataFrame(), "factors": factors,
                "representatives": [], "skipped": skipped}
    ic_panel = pd.DataFrame(series).sort_index()

    # 代表因子：IC 相关去重（score=|IC_IR|），保留 ≤MAX_REPRESENTATIVES
    reps = sorted(ic_panel.columns, key=lambda f: -_abs_ir(ic_panel[f]))
    corr = factor_correlation_matrix(ic_panel) if len(ic_panel.columns) > 1 else None
    kept = []
    for fid in reps:
        if corr is not None and any(abs(corr.loc[fid, k]) > 0.9 for k in kept):
            continue
        kept.append(fid)
        if len(kept) >= MAX_REPRESENTATIVES:
            break
    return {"ic_panel": ic_panel, "factors": factors,
            "representatives": kept, "skipped": skipped}


def _abs_ir(s: pd.Series) -> float:
    st = ic_stats(s)
    ir = st.get("ic_ir")
    return abs(float(ir)) if ir is not None and np.isfinite(ir) else 0.0


def prescreen_ic_correlation(cand_ic: pd.Series, ic_panel: pd.DataFrame,
                             threshold: float = IC_CORR_REJECT) -> tuple[bool, str | None]:
    """候选 IC 序列 vs 参考库 IC：首个 |相关|>threshold → (False, reason)。"""
    if ic_panel is None or ic_panel.shape[1] == 0 or len(cand_ic) < 30:
        return True, None
    for fid in ic_panel.columns:
        ref = ic_panel[fid].reindex(cand_ic.index)
        df = pd.DataFrame({"a": cand_ic, "b": ref}).dropna()
        if len(df) < 30:
            continue
        r = float(df["a"].corr(df["b"], method="spearman"))
        if not np.isfinite(r):
            continue
        if abs(r) > threshold:
            return False, f"与 {fid} IC 序列相关 {r:.2f}>{threshold}，重复挖掘"
    return True, None


def residualize_factor(factor: pd.DataFrame, ref_factors: dict[str, pd.DataFrame],
                       dates: list[pd.Timestamp]) -> pd.DataFrame:
    """逐日横截面 OLS 残差：候选因子 ~ [1, 代表因子]，返回残差 DataFrame。"""
    codes = factor.columns
    out = pd.DataFrame(np.nan, index=factor.index, columns=codes, dtype=float)
    ref_names = list(ref_factors.keys())
    for d in dates:
        if d not in factor.index:
            continue
        df = pd.DataFrame({"y": factor.loc[d]})
        for name in ref_names:
            df[name] = ref_factors[name].loc[d]   # 当日行 Series（index=codes）
        df = df.replace([np.inf, -np.inf], np.nan).dropna()
        if len(df) < 50:
            continue
        X = df[ref_names].values
        yy = df["y"].values
        A = np.column_stack([np.ones(len(df)), X])
        try:
            beta, *_ = np.linalg.lstsq(A, yy, rcond=None)
            resid = yy - A @ beta
        except np.linalg.LinAlgError:
            continue
        # 残差数值噪声级（候选被参考因子完全张成）→ 置 NaN，不产生伪增量 IC
        sd_y = float(np.std(yy))
        sd_r = float(np.std(resid))
        if sd_y > 1e-9 and sd_r < 1e-6 * sd_y:
            continue
        out.loc[d, df.index] = resid
    return out


def prescreen_incremental(factor: pd.DataFrame, representatives: list[str],
                          ref_factors: dict[str, pd.DataFrame],
                          fwd: pd.DataFrame, dates: list[pd.Timestamp],
                          base_ic_mean: float,
                          *, require_base_direction: bool = True) -> tuple[bool, dict]:
    """增量 IC：残差化后重算 IC。

    晋升预筛要求增量与已通过 L1 的单因子方向一致；探索阶段的单因子方向
    尚未得到统计支持，因此只要求条件增量不为零，并显式返回可组合方向。
    """
    if not representatives:
        return True, {"incremental_ic": None, "note": "无代表因子可比"}
    refs = {k: ref_factors[k] for k in representatives if k in ref_factors}
    if len(refs) < 1:
        return True, {"incremental_ic": None, "note": "代表因子缺失"}
    _require_executable_label(fwd)
    resid = residualize_factor(factor, refs, dates)
    inc = cross_sectional_ic(resid, fwd, dates)
    st = ic_stats(inc)
    inc_ic = st["ic_mean"] if st["n_dates"] >= 30 else None
    base_dir = 1.0 if base_ic_mean >= 0 else -1.0
    if inc_ic is None or not np.isfinite(inc_ic):
        ok = False
        reason = "增量 IC 无法计算（残差截面不足）"
    elif abs(inc_ic) < INC_IC_FLOOR:
        ok = False
        reason = f"增量 IC {inc_ic:.4f} 近零，无边际信息"
    elif require_base_direction and inc_ic * base_dir < 0:
        ok = False
        reason = f"增量 IC {inc_ic:.4f} 与基础方向相反，信号已被参考因子吸收"
    else:
        ok = True
        reason = "OK"
    return ok, {
        "incremental_ic": round(float(inc_ic), 4) if inc_ic is not None else None,
        "incremental_ic_ir": round(float(st["ic_ir"]), 3) if st.get("ic_ir") is not None and np.isfinite(st["ic_ir"]) else None,
        "n_dates": st["n_dates"], "ok": ok, "reason": reason,
        "conditional_direction": (
            "higher_is_better" if inc_ic is not None and inc_ic >= 0
            else "lower_is_better"
        ),
        "promotion_direction_required": bool(require_base_direction),
    }
