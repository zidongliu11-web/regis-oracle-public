"""多级检验管道（任务书第六部分 L0-L3）。

L0 硬性过滤 → L1 统计检验(IS) → L2 前瞻验证(OOS，严格时间分离) → L3 多次检验修正(FDR-BH)。

所有产出必须走完 L0-L3 才可入库；任一级不过即淘汰。淘汰带具体原因，
供挖掘区状态看板展示，杜绝「只报好看时段」。
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from . import config
from .data import Panel, mask_signal_eligible
from .metrics import cross_sectional_ic, ic_stats, quantile_spread_annualized
from .preprocess import build_exposures, neutralize
from .diagnostics import compute_diagnostics

from quantqc.stats import fold_pass_rate, walk_forward_folds


L0_CONTRACT_VERSION = config.L0_CONTRACT_VERSION
L0_MIN_ELIGIBLE_CROSS_SECTION = 2
L0_MIN_CANDIDATE_CROSS_SECTION = 20


def l0_hard_filter(
    factor: pd.DataFrame,
    is_dates: list[pd.Timestamp],
    signal_eligible: pd.DataFrame | None = None,
    *,
    require_min_days: bool = True,
) -> dict:
    """L0：只在当日可产生新信号的股票格子上检查覆盖率与截面变化。"""
    sub = factor.reindex(index=is_dates).replace([np.inf, -np.inf], np.nan)
    if isinstance(signal_eligible, pd.DataFrame):
        eligible = signal_eligible.reindex(
            index=sub.index, columns=sub.columns,
        ).fillna(False).astype(bool)
    else:
        eligible = pd.DataFrame(True, index=sub.index, columns=sub.columns)
    sub = sub.where(eligible)
    total = int(eligible.to_numpy(dtype=bool).sum())
    valid = int(sub.notna().to_numpy(dtype=bool).sum())
    coverage = valid / total if total else 0.0
    eligible_by_day = eligible.sum(axis=1)
    finite_by_day = sub.notna().sum(axis=1)
    eligible_days = eligible_by_day.ge(L0_MIN_ELIGIBLE_CROSS_SECTION)
    candidate_days = finite_by_day.ge(L0_MIN_CANDIDATE_CROSS_SECTION)
    eligible_cross_section_days = int(eligible_days.sum())
    candidate_cross_section_days = int(candidate_days.sum())
    required_variable_days = max(
        1, int(0.5 * eligible_cross_section_days),
    )
    # 截面方差至少需要两个合资格观测；IC 日数另按 20 只计。
    cs_std = sub.std(axis=1, ddof=0)
    variable_days = int(((cs_std > 1e-12) & eligible_days).sum())
    n_days = len(is_dates)
    passed = (
        coverage >= 0.60 and
        variable_days >= required_variable_days and
        (not require_min_days or
         candidate_cross_section_days >= config.MIN_IC_DATES)
    )
    return {
        "l0_contract_version": L0_CONTRACT_VERSION,
        "stage": "L0",
        "passed": bool(passed),
        "coverage": coverage,
        "valid_observations": valid,
        "eligible_observations": total,
        "variable_days": variable_days,
        "required_variable_days": required_variable_days,
        "eligible_cross_section_days": eligible_cross_section_days,
        "candidate_cross_section_days": candidate_cross_section_days,
        "total_days": n_days,
        "reason": "OK" if passed else (
            "覆盖率不足" if coverage < 0.60 else
            "截面近似恒定" if variable_days < required_variable_days else
            "有效截面日不足"),
    }


def _tstat_pvalue(ic_ir: float, n: int) -> float:
    if ic_ir is None or not np.isfinite(ic_ir) or n < 2:
        return 1.0
    from scipy.stats import norm
    t = ic_ir * np.sqrt(n)
    return float(2.0 * (1.0 - norm.cdf(abs(t))))


def l1_statistical(ic: pd.Series, spread: dict,
                   hold: int = config.HOLD_DAYS,
                   expected_direction: float | None = None) -> dict:
    """L1：样本内 |IC| 门槛 + IC_IR 门槛 + 多空分层利差。"""
    s = ic_stats(ic, hold=hold)
    mu = s["ic_mean"]
    ics = s["ic_ir"]
    ls = spread["long_short_annualized"]
    if expected_direction is None:
        ok_ic = abs(mu) >= config.IC_THRESHOLD if pd.notna(mu) else False
        ok_ir = abs(ics) >= config.IC_IR_THRESHOLD if pd.notna(ics) else False
        ok_ls = pd.notna(ls) and abs(ls) >= config.MIN_QUANTILE_SPREAD
        wrong_direction = False
    else:
        ok_ic = bool(pd.notna(mu) and mu * expected_direction >= config.IC_THRESHOLD)
        ok_ir = bool(pd.notna(ics) and ics * expected_direction >= config.IC_IR_THRESHOLD)
        ok_ls = bool(pd.notna(ls) and ls * expected_direction >= config.MIN_QUANTILE_SPREAD)
        wrong_direction = bool(
            (pd.notna(mu) and mu * expected_direction <= 0)
            or (pd.notna(ics) and ics * expected_direction <= 0)
            or (pd.notna(ls) and ls * expected_direction <= 0)
        )
    passed = bool(ok_ic and ok_ir and ok_ls)
    return {
        "stage": "L1",
        "passed": passed,
        "ic_mean": mu, "ic_ir": ics, "ic_std": s["ic_std"], "n_dates": s["n_dates"],
        "n_effective": s.get("n_effective"),
        "p_value_method": s.get("p_value_method"),
        "p_value": s.get("p_value", 1.0),
        "long_short_annualized": ls,
        "expected_direction": expected_direction,
        "reason": "OK" if passed else (
            "样本内方向与预注册相反" if wrong_direction else
            "|IC|<门槛" if not ok_ic else "|IC_IR|<门槛" if not ok_ir else "分层利差不足"),
    }


def _nn(v):
    """None/NaN → None（JSON 安全）。"""
    try:
        if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
            return None
    except Exception:
        pass
    return v


def l2_walkforward_forward(factor: pd.DataFrame, fwd: pd.DataFrame,
                           oos_dates: list[pd.Timestamp], is_direction: float,
                           is_ic_mean: float = 0.0,
                           hold: int = config.HOLD_DAYS) -> dict:
    """L2：样本外前瞻验证（walk-forward 前移多折）。

    P-C 增强：用 walk_forward_folds 把 OOS 切成前移滚动验证窗（训练段在前，
    仅作区间界定——因子为静态配方，不随折重训），每折验证窗内 OOS IC 须与
    IS 同向且 |IC_IR|≥0.3、观测日≥40；多数折通过（fold_pass_rate≥60%）才放行。
    替代旧「仅整段 OOS IC」单窗口判断，防止单段行情偶然达标。整段 OOS IC 仍
    同时报告供看板对比——与部署端科学闸门（多折 + IC_IR 门槛）同一科学口径。
    """
    if not oos_dates:
        return {"stage": "L2", "passed": False,
                "is_ic_mean": _nn(is_ic_mean), "oos_ic_mean": None, "oos_ic_ir": None,
                "oos_n_dates": 0, "fold_pass": {"n_folds": 0, "passed": 0,
                                                "pass_rate": None, "gate_ok": False},
                "folds": [], "ic_retention": None,
                "validation_p_value": 1.0,
                "validation_p_value_method": "non_overlapping_phase_max_hac",
                "validation_p_value_n_effective": 0,
                "reason": "样本外无观测日"}

    # Static recipes have the same IC on a date in every validation window.
    # Compute once; each window still performs its own original phase/HAC test.
    oos_ic = cross_sectional_ic(factor, fwd, oos_dates)

    def _stat(val_lo, val_hi):
        window = (oos_ic if oos_ic.empty else
                  oos_ic[(oos_ic.index >= val_lo) & (oos_ic.index <= val_hi)])
        return ic_stats(window, hold=hold)

    folds = walk_forward_folds([str(d) for d in oos_dates], n_folds=5)
    fold_rows = []
    for fd in folds:
        val_lo, val_hi = pd.Timestamp(fd["val"][0]), pd.Timestamp(fd["val"][-1])
        st = _stat(val_lo, val_hi)
        same = bool(pd.notna(st["ic_mean"]) and st["ic_mean"] * is_direction > 0)
        ok_ir = bool(pd.notna(st["ic_ir"]) and abs(st["ic_ir"]) >= 0.3)
        ok_n = bool(st["n_dates"] >= 40)
        fold_rows.append({
            "val": fd["val"],
            "ok": same and ok_ir and ok_n,
            "ic_mean": _nn(st["ic_mean"]),
            "ic_ir": _nn(st["ic_ir"]),
            "n_dates": st["n_dates"],
            "n_effective": st.get("n_effective"),
        })
    fp = fold_pass_rate([r["ok"] for r in fold_rows])

    full = ic_stats(oos_ic, hold=hold)
    same_full = bool(pd.notna(full["ic_mean"]) and full["ic_mean"] * is_direction > 0)
    ok_full_ir = bool(pd.notna(full["ic_ir"]) and abs(full["ic_ir"]) >= 0.3)
    ok_n_full = bool(full["n_dates"] >= max(60, config.MIN_IC_DATES // 2))
    passed = bool(fp["gate_ok"] and same_full and ok_full_ir and ok_n_full)
    retain = None
    if pd.notna(full["ic_mean"]) and abs(is_ic_mean) > 1e-12:
        retain = abs(full["ic_mean"]) / abs(is_ic_mean)
    min_retention = 1.0 - float(config.OOS_MAX_IC_DEGRADE)
    retention_ok = bool(retain is not None and np.isfinite(retain)
                        and retain >= min_retention)
    passed = bool(passed and retention_ok)
    return {
        "stage": "L2",
        "passed": passed,
        "is_ic_mean": _nn(is_ic_mean),
        "oos_ic_mean": _nn(full["ic_mean"]),
        "oos_ic_ir": _nn(full["ic_ir"]),
        "oos_n_dates": full["n_dates"],
        "fold_pass": fp,
        "folds": fold_rows,
        "ic_retention": _nn(retain),
        "minimum_ic_retention": min_retention,
        "ic_retention_ok": retention_ok,
        "validation_p_value": _nn(full.get("p_value", 1.0)),
        "validation_p_value_method": full.get("p_value_method"),
        "validation_p_value_n_effective": full.get("n_effective"),
        "reason": "OK" if passed else (
            "多数折样本外IC方向/强度不足" if not fp["gate_ok"] else
            "样本外整段方向相反" if not same_full else
            "样本外整段IC_IR不足" if not ok_full_ir else
            "样本外观测日不足" if not ok_n_full else
            (f"样本外IC保留率不足({retain!r}<{min_retention:.2f})")),
    }


def l3_fdr(pairs: list[dict], q: float = config.FDR_Q) -> dict:
    """L3：对批量候选的 IS p 值做 Benjamini-Hochberg 校正，控制假阳性。

    pairs: [{"factor_id", "p_value"}]（进入 L3 的候选）。
    返回 {factor_id: {"reject": bool, "p_adj_threshold": float, "p_value": float}}。
    """
    n = len(pairs)
    out = {}
    if n == 0:
        return out
    for row in pairs:
        out[row["factor_id"]] = {"p_value": row["p_value"], "p_adj_threshold": np.nan, "reject": False}
    pv = sorted((float(x["p_value"]) for x in pairs), key=lambda v: v if np.isfinite(v) else 1.0)
    threshold = 0.0
    for i, p in enumerate(pv, start=1):
        cand = q * i / n
        if p <= cand:
            threshold = max(threshold, p)   # BH：最大满足 p<=q·i/n 的 p 为整体拒绝阈
    for fid in out:
        p = out[fid]["p_value"]
        out[fid]["reject"] = bool(np.isfinite(p) and p <= threshold)
        out[fid]["p_adj_threshold"] = threshold
    return out


def _dual_ic(res_l1: dict, ic_raw: pd.Series | None,
             hold: int = config.HOLD_DAYS) -> dict:
    """raw / neutralized 双 IC 证据列。

    中性化未启用（ic_raw=None）时 raw IC 即门槛 IC（同一序列），neutralized 列 None；
    启用时 raw IC 来自原始因子、neutralized IC 为门槛（中性化后因子）IC。
    """
    if ic_raw is not None:
        s = ic_stats(ic_raw, hold=hold)
        raw_mean, raw_ir = s["ic_mean"], s["ic_ir"]
    else:
        raw_mean, raw_ir = res_l1["ic_mean"], res_l1["ic_ir"]
    neut_mean = res_l1["ic_mean"] if ic_raw is not None else None
    neut_ir = res_l1["ic_ir"] if ic_raw is not None else None
    return {
        "raw_ic_mean": raw_mean,
        "raw_ic_ir": raw_ir,
        "neutralized_ic_mean": neut_mean,
        "neutralized_ic_ir": neut_ir,
    }


def run_validation(factor: pd.DataFrame, fwd: pd.DataFrame,
                   is_dates: list[pd.Timestamp], oos_dates: list[pd.Timestamp],
                   hold: int = config.HOLD_DAYS,
                   panel: Panel | None = None,
                   exposures: dict | None = None,
                   is_fwd: pd.DataFrame | None = None,
                   expected_direction: float | None = None,
                   apply_neutralization: bool = True) -> dict:
    """完整 L0→L1→L2，返回结构结果。L3 在 batch 层对多个候选统一执行。

    M1.1：apply_neutralization=True 且 exposures 非空时做行业+市值中性化，
    门槛走中性化后因子，同时输出 raw IC 与 neutralized IC 双列证据——中性化会杀
    行业/规模色重的真信号（换手、流动性等），必须双报告。panel 传入而 exposures
    缺省时懒构建（测试便捷）；生产调用应由 batch 层一次构建 exposures 传入，
    避免逐候选重复构建协变量。
    """
    if isinstance(expected_direction, bool):
        raise ValueError("expected_direction 必须是 -1、1 或 None")
    if expected_direction is not None:
        try:
            expected_direction = float(expected_direction)
        except (TypeError, ValueError) as exc:
            raise ValueError("expected_direction 必须是 -1、1 或 None") from exc
        if not np.isfinite(expected_direction) or expected_direction not in (-1.0, 1.0):
            raise ValueError("expected_direction 必须是 -1、1 或 None")
    if not isinstance(apply_neutralization, bool):
        raise ValueError("apply_neutralization 必须是 bool")
    policy = {
        "hold_days": int(hold),
        "expected_direction": expected_direction,
        "apply_neutralization": apply_neutralization,
        "direction_source": (
            "predeclared" if expected_direction is not None else "discovery_sample"
        ),
    }

    # 向后兼容旧调用；挖掘管道显式传入 boundary=IS_END 的 is_fwd。
    is_fwd = fwd if is_fwd is None else is_fwd
    if panel is not None:
        factor = mask_signal_eligible(panel, factor)

    # 覆盖掉 IS/OOS 之外的日期（factor 面板可能包含全区间）
    is_dates_ok = [d for d in is_dates if d in factor.index]
    oos_dates_ok = [d for d in oos_dates if d in factor.index]

    # ── M1.1 中性化（可选）：门槛因子 = 中性化残差，同时保留 raw 双列证据 ──
    if apply_neutralization and exposures is None and panel is not None:
        exposures = build_exposures(panel)
    factor_gate = factor
    neutralize_mode = None
    if apply_neutralization and exposures is not None:
        factor_gate = neutralize(factor, exposures)
        neutralize_mode = exposures.get("mode")
    neut = {"neutralized": neutralize_mode is not None, "neutralize_mode": neutralize_mode}

    eligible = getattr(panel, "signal_eligible", None) if panel is not None else None
    res_l0 = l0_hard_filter(
        factor_gate, is_dates_ok, signal_eligible=eligible,
    )
    if not res_l0["passed"]:
        return {"passed": False, "stage": "L0", "L0": res_l0, **policy, **neut}

    spread = quantile_spread_annualized(factor_gate, is_fwd, is_dates_ok, hold)
    ic_is = cross_sectional_ic(factor_gate, is_fwd, is_dates_ok)
    ic_raw = None
    if neutralize_mode is not None:
        ic_raw = cross_sectional_ic(factor, is_fwd, is_dates_ok)
    if ic_is.notna().sum() < config.MIN_IC_DATES:
        res_l0["passed"] = False
        res_l0["reason"] = "有效IC观测日不足"
        return {"passed": False, "stage": "L0", "L0": res_l0, **policy, **neut}

    res_l1 = l1_statistical(
        ic_is, spread, hold=hold, expected_direction=expected_direction,
    )
    if not res_l1["passed"]:
        return {"passed": False, "stage": "L1", "L0": res_l0, "L1": res_l1,
                "is_ic_mean": res_l1["ic_mean"], "is_ic_ir": res_l1["ic_ir"],
                **policy, **neut, **(_dual_ic(res_l1, ic_raw, hold=hold))}

    direction = (expected_direction if expected_direction is not None else
                 (1.0 if res_l1["ic_mean"] > 0 else -1.0))
    res_l2 = l2_walkforward_forward(factor_gate, fwd, oos_dates_ok, direction,
                                    is_ic_mean=res_l1["ic_mean"], hold=hold)
    if not res_l2["passed"]:
        return {"passed": False, "stage": "L2", "L0": res_l0, "L1": res_l1, "L2": res_l2,
                "is_ic_mean": res_l1["ic_mean"], "is_ic_ir": res_l1["ic_ir"],
                "validation_p_value": res_l2.get("validation_p_value", 1.0),
                "validation_p_value_method": res_l2.get("validation_p_value_method"),
                "validation_p_value_n_effective": res_l2.get("validation_p_value_n_effective"),
                **policy, **neut, **(_dual_ic(res_l1, ic_raw, hold=hold))}

    # L3 在 batch 层执行；只能校正独立 validation 段的 p，禁止复用 discovery/L1 p。
    # M1.3：幸存者诊断（monotonic soft gate + 衰减/换手/容量/跨年份稳定性）只报告不否决。
    # 年份只作 OOS 稳定性切分；情绪适用性由联合情绪路由回放评估。
    # diagnostics.ic_decay_curve 会从 panel.close 重算多周期前向收益；截断 close，
    # 保证最长诊断窗口也不能读取 IS 之后的价格。
    diag_panel = (replace(panel, close=panel.close.loc[:max(is_dates_ok)])
                  if panel is not None and is_dates_ok else panel)
    diag = compute_diagnostics(factor_gate, is_fwd, is_dates_ok, panel=diag_panel,
                               hold=hold, direction=direction)
    return {
        "passed": True,
        "stage": "L3",
        "L0": res_l0, "L1": res_l1, "L2": res_l2,
        "is_ic_mean": res_l1["ic_mean"], "is_ic_ir": res_l1["ic_ir"],
        "is_ic_std": res_l1["ic_std"], "is_n_dates": res_l1["n_dates"],
        "oos_ic_mean": res_l2["oos_ic_mean"], "oos_ic_ir": res_l2["oos_ic_ir"],
        "oos_n_dates": res_l2["oos_n_dates"], "ic_retention": res_l2["ic_retention"],
        "long_short_annualized": res_l1["long_short_annualized"],
        "validation_p_value": res_l2.get("validation_p_value", 1.0),
        "validation_p_value_method": res_l2.get("validation_p_value_method"),
        "validation_p_value_n_effective": res_l2.get("validation_p_value_n_effective"),
        # 兼容 registry 列名；语义已明确切换为 validation p。
        "p_value": res_l2.get("validation_p_value", 1.0),
        "p_value_method": res_l2.get("validation_p_value_method"),
        "p_value_n_effective": res_l2.get("validation_p_value_n_effective"),
        "diag": diag,
        **policy, **neut, **(_dual_ic(res_l1, ic_raw, hold=hold)),
    }
