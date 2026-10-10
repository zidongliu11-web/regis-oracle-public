"""P2-5 因子相关性去重（修「7 个 production 实为 1 个低波因子」）。

同族因子按 **IC 序列相关** >0.9（abs）折叠为代表：
  代表 = 相关组内 OOS |IC_IR| 最高者；其余冻结，原因注明「与代表因子 X 相关 >0.9，同族去重」。
用 abs 相关 → 反向克隆（同信号取负）也会被正确折叠。

注意与 quantqc.stats.dedupe_correlated 的关系：本模块在 registry 层消费注册表里的
production 因子；统计计算复用 quantqc.stats（不改三处引擎骨架）。

CLI: python -m factor_system.dedupe [--dry-run]
"""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

from . import config
from . import registry as reg
from .data import (EXECUTABLE_FORWARD_LABEL, executable_forward_return,
                   load_panel, split_research_windows)
from .metrics import cross_sectional_ic, ic_stats
from .search import rebuild_factor

from quantqc.stats import factor_correlation_matrix, dedupe_correlated


def ic_panel_for_factors(panel, factors: dict[str, pd.DataFrame],
                         sealed_data_end: dict[str, str] | None = None) -> pd.DataFrame:
    """在全部参与因子的共同 seal 之后构造可比较的前向 IC 面板。"""
    dates = split_research_windows(panel.dates)["forward_shadow"]
    if sealed_data_end:
        try:
            common_start = max(pd.Timestamp(sealed_data_end[fid]) for fid in factors)
        except (KeyError, TypeError, ValueError):
            return pd.DataFrame()
        dates = [pd.Timestamp(date) for date in dates
                 if pd.Timestamp(date) > common_start]
    if not dates:
        return pd.DataFrame()
    fwd = executable_forward_return(
        panel, config.HOLD_DAYS, boundary=str(dates[-1].date()),
    )
    series = {}
    for fid, f in factors.items():
        ics = cross_sectional_ic(f, fwd, dates)
        if ics.notna().sum() >= 30:
            series[fid] = ics
    if not series:
        return pd.DataFrame()
    df = pd.DataFrame(series)
    return df.sort_index()


def dedupe_production_factors(dry_run: bool = False, panel=None) -> dict:
    """对注册表 production 因子做 IC 相关性去重。返回报告。

    真实执行：重复因子置 frozen（保留证据），代表因子保持 production。
    缺失配方/重建失败的 production 因子 → frozen（诚实标注，不装作仍有效）。

    panel 可选：M2.2 回测批用瘦子面板复用，避免重复加载全量面板。
    """
    c = reg._conn()
    prod = reg.factors_by_status(c, "production")
    report = {"production_before": len(prod), "groups": [], "frozen": [], "kept": [], "errors": []}
    if not prod:
        c.close()
        report["production_after"] = 0
        return report

    panel = panel if panel is not None else load_panel()
    factors: dict[str, pd.DataFrame] = {}
    fid_to_row = {}
    for row in prod:
        fid = row["factor_id"]
        if not row.get("sealed_data_end"):
            report["errors"].append({"fid": fid, "err": "缺少 sealed_data_end"})
            continue
        recipe_path = config.REGISTRY_DIR / "recipes" / f"{fid}.json"
        if not recipe_path.exists():
            report["errors"].append({"fid": fid, "err": "配方缺失"})
            continue
        try:
            recipe = json.loads(recipe_path.read_text("utf-8"))
            f = rebuild_factor(panel, recipe)
            if f is None:
                report["errors"].append({"fid": fid, "err": "因子重建失败"})
                continue
            factors[fid] = f
            fid_to_row[fid] = row
        except Exception as e:
            report["errors"].append({"fid": fid, "err": str(e)[:120]})

    # 重建失败的 production → 冻结（诚实：无法复核 ≠ 继续有效）
    for e in report["errors"]:
        if not dry_run:
            reg.freeze(c, e["fid"], f"去重复核：{e['err']}，无法证明仍有效 → 冻结")
        report["frozen"].append(e["fid"])

    if len(factors) < 2:
        c.commit()
        c.close()
        report["production_after"] = len(prod) - len(report["frozen"])
        return report

    seal_ends = {fid: str(fid_to_row[fid]["sealed_data_end"]) for fid in factors}
    common_start = max(pd.Timestamp(value) for value in seal_ends.values())
    ic_panel = ic_panel_for_factors(panel, factors, seal_ends)
    report["comparison_window"] = {
        "start_exclusive": str(common_start.date()),
        "n_ic_dates": int(len(ic_panel.index)),
        "basis": "all_members_post_seal_common_window",
        "forward_return_label": EXECUTABLE_FORWARD_LABEL,
    }
    if ic_panel.shape[1] < 2:
        c.commit()
        c.close()
        report["production_after"] = len(prod) - len(report["frozen"])
        report["note"] = "共同 seal 后有效 IC 不足 30 日，未做相关去重"
        return report

    corr = factor_correlation_matrix(ic_panel)
    # 评分 = OOS |IC_IR|（未来预测力的稳健代理）；缺失 → 0
    scores = {}
    for fid in ic_panel.columns:
        st = ic_stats(ic_panel[fid], hold=config.HOLD_DAYS)
        scores[fid] = abs(st["ic_ir"]) if st["ic_ir"] is not None and np.isfinite(st["ic_ir"]) else 0.0
    score_series = pd.Series(scores, index=ic_panel.columns)
    retained = dedupe_correlated(ic_panel, threshold=0.9, score=score_series)

    # 分组报告（与 dedupe_correlated 同序贪心：score 降序，首个未归簇列即代表）
    order = sorted(ic_panel.columns, key=lambda c: score_series.get(c, -1.0), reverse=True)
    rep_of: dict[str, str] = {}
    grouped = []
    for rep in order:
        if rep in rep_of:
            continue
        members = [rep] + [d for d in ic_panel.columns
                           if d != rep and abs(corr.loc[rep, d]) > 0.9]
        for m in members:
            rep_of[m] = rep
        grouped.append({"representative": rep, "members": members})
    report["groups"] = grouped

    for fid in ic_panel.columns:
        if fid not in retained:
            rep = rep_of.get(fid)
            reason = f"同族去重：与代表 {rep} IC 相关 >0.9，重复挖掘 → 冻结"
            if not dry_run:
                reg.freeze(c, fid, reason)
            report["frozen"].append(fid)
        else:
            report["kept"].append(fid)

    report["production_after"] = len(prod) - len(report["frozen"])
    if dry_run:
        c.rollback()
    else:
        c.commit()
    c.close()
    return report


def _main():
    dry = "--dry-run" in sys.argv
    report = dedupe_production_factors(dry_run=dry)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
