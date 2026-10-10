"""P2-6 激活 frozen 生命周期（STOP_FREEZE_DAYS 连续 IC 转负 → 冻结）。

历史缺陷：frozen 状态在 SCHEMA/状态机里定义了，但从未有代码触发。
本模块把「连续 N 日 IC 转负」检查挂到 backtest 轮（launchd 双轮之一内）：
  每个 production 因子，取最近 max(STOP_FREEZE_DAYS*3, 60) 交易日的逐日 IC，
  若尾部连续 STOP_FREEZE_DAYS 日方向化 IC 全为负（因子失效/反向）→ 冻结，从生产摘除。
方向化 IC = IC × 挖掘方向（负方向因子用正 IC 即失效）。

CLI: python -m factor_system.lifecycle [--dry-run]
"""
from __future__ import annotations

import json
import hashlib
import sys

import numpy as np
import pandas as pd

from . import config
from . import registry as reg
from .data import executable_forward_return, load_panel, split_research_windows
from .metrics import cross_sectional_ic
from .search import rebuild_factor


def _signed_ic_series(panel, factor: pd.DataFrame, direction: float,
                      window: int | None = None,
                      sealed_data_end: str | None = None) -> pd.Series:
    """仅用正式投产后的前向观察做方向化逐日 IC 监控。"""
    forward_dates = split_research_windows(panel.dates)["forward_shadow"]
    if sealed_data_end:
        boundary = pd.Timestamp(sealed_data_end)
        forward_dates = [d for d in forward_dates if pd.Timestamp(d) > boundary]
    if not forward_dates:
        return pd.Series(dtype=float)
    fwd = executable_forward_return(
        panel, config.HOLD_DAYS, boundary=str(forward_dates[-1].date()),
    )
    tail_dates = [d for d in forward_dates if d in fwd.index and d in factor.index]
    signed = (cross_sectional_ic(factor, fwd, tail_dates) * direction).dropna()
    return signed.iloc[-window:] if window else signed


def _sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_recipe(row: dict) -> dict:
    """读取并核对 production 因子的不可变配方 seal。"""
    fid = row["factor_id"]
    recipe_path = config.REGISTRY_DIR / "recipes" / f"{fid}.json"
    if not recipe_path.is_file():
        raise RuntimeError("配方缺失")
    for key in ("sealed_at", "sealed_data_end", "sealed_recipe_sha256"):
        if not row.get(key):
            raise RuntimeError("production 缺少不可变 seal")
    if row.get("research_epoch") != config.RESEARCH_EPOCH:
        raise RuntimeError("production 不属于当前 research epoch")
    if _sha256(recipe_path) != row["sealed_recipe_sha256"]:
        raise RuntimeError("配方哈希与 seal 不符")
    recipe = json.loads(recipe_path.read_text("utf-8"))
    if (recipe.get("research_epoch") != row["research_epoch"]
            or recipe.get("sealed_at") != row["sealed_at"]
            or recipe.get("sealed_data_end") != row["sealed_data_end"]):
        raise RuntimeError("配方内容与 seal 不符")
    return recipe


def check_freeze_production(dry_run: bool = False, panel=None) -> dict:
    """检查全部 production 因子，连续 STOP_FREEZE_DAYS 日 IC 转负 → 冻结。

    panel 可选：M2.2 回测批用瘦子面板复用，避免重复加载全量面板。
    """
    freeze_days = config.STOP_FREEZE_DAYS
    c = reg._conn()
    prod = reg.factors_by_status(c, "production")
    report = {"freeze_days": freeze_days, "checked": [], "frozen": [], "kept": [], "errors": []}
    if not prod:
        c.close()
        return report

    panel = panel if panel is not None else load_panel()
    for row in prod:
        fid = row["factor_id"]
        entry = {"fid": fid, "tail_neg_days": 0, "action": "kept"}
        direction = (1.0 if row["is_ic_mean"] >= 0 else -1.0) \
            if pd.notna(row["is_ic_mean"]) else 1.0
        try:
            recipe = _verified_recipe(row)
            f = rebuild_factor(panel, recipe)
            if f is None:
                entry.update({"action": "error", "reason": "因子重建失败 → 冻结"})
                report["errors"].append(fid)
                if not dry_run:
                    reg.freeze(c, fid, f"生命周期监测：重建失败，无法证明仍有效 → 冻结")
                    report["frozen"].append(fid)
                report["checked"].append(entry)
                continue
            signed = _signed_ic_series(
                panel, f, direction, sealed_data_end=row["sealed_data_end"],
            )
            # 尾部连续负 IC 天数
            tail_neg = 0
            for v in signed.iloc[::-1].tolist():
                if pd.notna(v) and v < 0:
                    tail_neg += 1
                else:
                    break
            entry["tail_neg_days"] = tail_neg
            if tail_neg >= freeze_days:
                entry["action"] = "frozen"
                if not dry_run:
                    reg.freeze(c, fid,
                               f"生命周期监测：尾部连续 {tail_neg} 日 IC 转负（≥{freeze_days}）→ 冻结停用")
                    report["frozen"].append(fid)
            else:
                report["kept"].append(fid)
        except Exception as e:
            entry.update({"action": "error", "reason": str(e)[:120] + " → 冻结"})
            report["errors"].append(fid)
            if not dry_run:
                reg.freeze(c, fid, "生命周期监测失败，无法证明仍有效 → 冻结: " + str(e)[:100])
                report["frozen"].append(fid)
        report["checked"].append(entry)

    if dry_run:
        c.rollback()
    else:
        c.commit()
    c.close()
    report["production_after"] = len(prod) - len(report["frozen"])
    return report


def revalidate_production(dry_run: bool = False, panel=None,
                          expected_input_key: str | None = None) -> dict:
    """P2-8 科学闸门重验：把现有 production 因子重新跑一遍 run_scientific_gate。

    这是「科学闸门上线」后必须做的一次诚实复核——旧弱门槛投产的因子不自动延续：
      通过 → 保留 production（更新科学证据列）；
      失败 → NO-GO（rejected，完整列出具体失败项），绝不留在 production。

    panel 可选：M2.2 回测批用瘦子面板复用，避免重复加载全量面板。
    """
    from .gate import run_scientific_gate

    c = reg._conn()
    prod = reg.factors_by_status(c, "production")
    panel = panel if panel is not None else load_panel()
    report = {"checked": [], "passed": [], "no_go": [], "errors": []}
    from .production import freeze_evaluator_drifted_production
    c.close()
    evaluator_drift = freeze_evaluator_drifted_production(
        panel, expected_input_key, dry_run=dry_run,
    )
    if evaluator_drift:
        report["evaluator_drift"] = evaluator_drift
        report["production_after"] = len(prod) - evaluator_drift["count"]
        return report
    c = reg._conn()
    all_forward_dates = split_research_windows(panel.dates)["forward_shadow"]

    for row in prod:
        fid = row["factor_id"]
        entry = {"fid": fid}
        try:
            recipe = _verified_recipe(row)
            sealed_end = pd.Timestamp(row["sealed_data_end"])
            oos_dates = [
                d for d in all_forward_dates if pd.Timestamp(d) > sealed_end
            ]
            required = config.FORWARD_SHADOW_MIN_DAYS + config.HOLD_DAYS + 1
            if len(oos_dates) < required:
                raise RuntimeError(
                    f"seal 后完整前向样本不足({max(0, len(oos_dates) - config.HOLD_DAYS - 1)}/"
                    f"{config.FORWARD_SHADOW_MIN_DAYS})"
                )
            factor = rebuild_factor(panel, recipe)
            if factor is None:
                entry.update({"verdict": "NO-GO", "reason": "因子重建失败 → 移出生产"})
                report["no_go"].append(fid)
                if not dry_run:
                    reg.set_status(c, fid, "rejected", "科学闸门重验：重建失败")
                report["checked"].append(entry)
                continue
            is_direction = (1.0 if row["is_ic_mean"] >= 0 else -1.0) \
                if pd.notna(row["is_ic_mean"]) else 1.0
            passed, evidence = run_scientific_gate(
                panel, factor, oos_dates,
                hold=config.HOLD_DAYS, q=config.QUANTILE,
                is_direction=is_direction,
                expected_input_key=expected_input_key)
            full = evidence.get("full") or {}
            metrics = {k: full.get(k) for k in
                       ("annual_return", "sharpe", "max_drawdown", "win_rate",
                        "excess_annual", "benchmark_annual")}
            if passed:
                entry.update({"verdict": "PASS", "reason": "科学闸门全端通过"})
                report["passed"].append(fid)
                if not dry_run:
                    # 健康复核不是新的冻结终测，不能清空/覆盖首次 production 的
                    # epoch、不可变证据路径和哈希。只更新生命周期状态说明。
                    reg.set_status(
                        c, fid, "production",
                        "生命周期复核通过；保留首次冻结前向终测证据",
                    )
            else:
                entry.update({"verdict": "NO-GO", "reason": evidence.get("reason", "")})
                report["no_go"].append(fid)
                if not dry_run:
                    # 即使健康复核失败，也保留原始投产证据供审计；状态变更即可摘除。
                    reg.set_status(
                        c, fid, "rejected",
                        "生命周期复核 NO-GO: " + evidence.get("reason", ""),
                    )
        except Exception as e:
            entry.update({"verdict": "ERROR", "reason": str(e)[:150]})
            report["errors"].append(fid)
            if not dry_run:
                reg.freeze(
                    c, fid,
                    "科学闸门健康重验失败，无法证明仍有效 → 冻结: " + str(e)[:100],
                )
        report["checked"].append(entry)

    if dry_run:
        c.rollback()
    else:
        c.commit()
    c.close()
    report["production_after"] = len(prod) - len(report["no_go"]) - len(report["errors"])
    return report


def _main():
    dry = "--dry-run" in sys.argv
    report = check_freeze_production(dry_run=dry)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
