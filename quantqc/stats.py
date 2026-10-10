"""统计检验（IC / t 统计 / walk-forward / BH-FDR / 因子相关性去重）。

全部返回可序列化 dict 或原生类型，供 factor_system 看板 / 回测中心 / 策略研发
直接渲染；不含任何"好看优先"的取舍——只算，不美化。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats as sps


# ---------------------------------------------------------------------------
# IC / t 统计
# ---------------------------------------------------------------------------
def cross_sectional_ic(factor: pd.Series, forward_ret: pd.Series) -> float:
    """单期截面 Spearman 秩相关（IC）。两者须同 index（多为 code）。"""
    df = pd.DataFrame({"f": factor, "r": forward_ret}).dropna()
    if len(df) < 20:
        return float("nan")
    return float(df["f"].corr(df["r"], method="spearman"))


def ic_series(factor_df: pd.DataFrame, ret_df: pd.DataFrame,
              dates: list | pd.Index | None = None) -> pd.Series:
    """因子面板 × 收益面板 → 每日 IC 序列（按日期）。"""
    if dates is None:
        dates = factor_df.index.intersection(ret_df.index)
    out = {}
    for d in dates:
        if d in factor_df.index and d in ret_df.index:
            ic = cross_sectional_ic(factor_df.loc[d], ret_df.loc[d])
            if not np.isnan(ic):
                out[d] = ic
    return pd.Series(out)


def ic_stats(ic: pd.Series, label: str = "") -> dict:
    """IC 序列统计：均值 / IR / t 统计 / p 值（Student-t, H0: IC=0）/ 样本量。"""
    x = ic.dropna()
    n = int(len(x))
    if n < 10:
        return {"label": label, "ic_mean": None, "ic_ir": None, "t_stat": None,
                "p_value": None, "n": n, "ic_positive_ratio": None}
    mean = float(x.mean())
    std = float(x.std(ddof=1))
    t_stat = float(mean / (std / np.sqrt(n))) if std > 0 else 0.0
    p_value = float(2 * sps.t.sf(abs(t_stat), df=n - 1))
    return {
        "label": label,
        "ic_mean": round(mean, 4),
        "ic_ir": round(mean / std, 3) if std > 0 else None,
        "t_stat": round(t_stat, 3),
        "p_value": round(p_value, 4),
        "n": n,
        "ic_positive_ratio": round(float((x > 0).mean()), 3),
    }


# ---------------------------------------------------------------------------
# 多折 walk-forward（前段训练后段验证，防时间前视）
# ---------------------------------------------------------------------------
def walk_forward_folds(dates: list, n_folds: int = 5,
                       min_validation_size: int = 40) -> list[dict]:
    """把按时间升序的日期序列切成 n 折滚动窗口：
      每折 = {'train': [start, end], 'val': [start, end]}，训练在前、验证在后且不重叠。
    验证段互相不重叠；训练段随折数后移（含前面所有历史 → walk-forward 扩张窗）。
    """
    dates = sorted(dates)
    n = len(dates)
    if n_folds <= 1 or n < n_folds * 2:
        # 样本太小：单折（全部训练、最后 20% 验证），如实标注退化
        cut = max(1, int(n * 0.8))
        return [{"train": [str(dates[0]), str(dates[cut - 1])],
                 "val": [str(dates[cut]), str(dates[-1])],
                 "degraded": True}]
    # 先留一块作初始训练窗，后面最多切成 n_folds 个验证块。
    # 旧实现用 ceil(n/n_folds) 后把末折 val_start 强行回拉，
    # n 不能整除时会与前一折重叠（真实 OOS=368 日时重叠 2 日）。
    # 样本较短时少切几折，避免为了凑折数把每折压到统计下限以下。
    max_by_size = max(1, n // max(1, min_validation_size) - 1)
    validation_folds = min(n_folds, max_by_size)
    blocks = [x for x in np.array_split(np.arange(n), validation_folds + 1) if len(x)]
    folds = []
    for k in range(1, len(blocks)):
        val_start = int(blocks[k][0])
        val_end = int(blocks[k][-1]) + 1
        folds.append({
            "train": [str(dates[0]), str(dates[val_start - 1])],
            "val": [str(dates[val_start]), str(dates[val_end - 1])],
            "degraded": False,
        })
    return folds


def equal_folds(dates, n_folds: int = 5) -> list[dict]:
    """把日期序列切成 n_folds 个等宽连续验证段（全覆盖，无训练段保留）。
    用于「训练已在更早窗口完成」的静态因子/策略复核——验证整段 OOS，
    每折 = {'val': [start, end]} 区间标记。n_folds 取不到整数时末折含余数。
    """
    dates = sorted(dates)
    n = len(dates)
    if n < n_folds:
        return [{"val": [str(dates[0]), str(dates[-1])], "degraded": True}] if n else []
    fold_size = int(np.ceil(n / n_folds))
    folds = []
    for k in range(n_folds):
        lo = k * fold_size
        hi = min(lo + fold_size, n)
        if lo >= n:
            break
        folds.append({"val": [str(dates[lo]), str(dates[hi - 1])], "degraded": False})
    return folds


def fold_pass_rate(pass_bools: list[bool]) -> dict:
    """全折通过率：≥60%（5 折 ≥3 折）才算部署候选。"""
    if not pass_bools:
        return {"n_folds": 0, "passed": 0, "pass_rate": None, "gate_ok": False}
    n = len(pass_bools)
    passed = int(sum(bool(b) for b in pass_bools))
    rate = passed / n
    return {"n_folds": n, "passed": passed, "pass_rate": round(rate, 3),
            "gate_ok": rate >= 0.6}


# ---------------------------------------------------------------------------
# 多重检验校正（BH-FDR）
# ---------------------------------------------------------------------------
def bh_fdr(p_values: list[float], q: float = 0.10) -> dict:
    """Benjamini–Hochberg FDR：返回显著标记、adjusted 阈值、各 p 是否通过。"""
    p = np.asarray(p_values, dtype=float)
    n = len(p)
    if n == 0:
        return {"n": 0, "significant": [], "threshold": None, "rejected": []}
    order = np.argsort(p)
    ranked = p[order]
    n = len(ranked)
    thresholds = np.array([(i + 1) * q / n for i in range(n)])
    below = ranked <= thresholds
    # BH: 最大的 i 满足 p_(i) <= (i/n) q，则前 i 个都显著
    idx = np.where(below)[0]
    if len(idx) == 0:
        k = 0
    else:
        k = int(idx.max()) + 1
    rejected = [False] * n
    for i in range(k):
        rejected[order[i]] = True
    return {
        "n": n,
        "q": q,
        "threshold": round(float(q / n), 5),
        "significant": [i for i, r in enumerate(rejected) if r],
        "rejected": rejected,
    }


def by_fdr(p_values: list[float], q: float = 0.10) -> dict:
    """Benjamini–Yekutieli 任意相关修正。

    策略网格共享因子和持仓，p 值显著相关；直接用 BH 的独立/PRDS
    前提不够保守。BY 用 q/H_m 调用同一 BH 实现，对任意相关仍控制 FDR。
    """
    m = len(p_values)
    harmonic = sum(1.0 / i for i in range(1, m + 1)) if m else 1.0
    effective_q = q / harmonic
    out = bh_fdr(p_values, q=effective_q)
    out.update({
        "method": "BY",
        "target_q": q,
        "effective_q": effective_q,
        "harmonic": harmonic,
    })
    return out


def online_lond_alpha(test_index: int, discoveries_before: int,
                      q: float = 0.10, dependent: bool = True) -> float:
    """24h 无限假设流的 LOND 当次显著性预算。

    ``gamma_i = 6 / (pi² i²)`` 非负且总和为 1；标准 LOND 使用
    ``alpha_i = q * gamma_i * (D_{i-1}+1)``。因子 p 值相关时，
    再除以谐和数 H_i，使用论文给出的任意依赖修正。

    返回值只依赖当前索引和之前的拒绝数，不看未来 p 值，
    适合不知总测试数的常驻挖掘机。
    """
    if test_index < 1:
        raise ValueError("test_index 必须 >= 1")
    if discoveries_before < 0:
        raise ValueError("discoveries_before 不能为负")
    if not 0 < q < 1:
        raise ValueError("q 必须介于 0 和 1 之间")
    gamma = 6.0 / (np.pi ** 2 * test_index ** 2)
    alpha = q * gamma * (discoveries_before + 1)
    if dependent:
        alpha /= sum(1.0 / i for i in range(1, test_index + 1))
    return float(min(q, max(0.0, alpha)))


# ---------------------------------------------------------------------------
# 因子相关性去重（同族折叠为代表）
# ---------------------------------------------------------------------------
def factor_correlation_matrix(factor_panel: pd.DataFrame) -> pd.DataFrame:
    """多因子面板 → 相关系数矩阵（按日期两两 Spearman 平均，稳健）。"""
    cols = list(factor_panel.columns)
    n = len(cols)
    corr = pd.DataFrame(np.eye(n), index=cols, columns=cols)
    for i in range(n):
        for j in range(i + 1, n):
            ci = factor_panel[cols[i]]
            cj = factor_panel[cols[j]]
            df = pd.DataFrame({"a": ci, "b": cj}).dropna()
            if len(df) >= 30:
                r = df["a"].corr(df["b"], method="spearman")
                corr.loc[cols[i], cols[j]] = r
                corr.loc[cols[j], cols[i]] = r
    return corr


def dedupe_correlated(factor_panel: pd.DataFrame, threshold: float = 0.9,
                      score: pd.Series | None = None) -> list[str]:
    """按 IC 相关性把 |corr|>threshold 的因子折叠为代表（保留 score 最高者）。

    贪心聚类：按 score 降序处理，第一个未归簇列即新代表，把与之相关>threshold
    的其余列全部并入该簇（弃）。每个列恰好属于一个簇，确定性、可复现。
    用 abs 相关 → 反向克隆（同信号取负）也会被正确折叠。
    """
    if factor_panel.shape[1] <= 1:
        return list(factor_panel.columns)
    corr = factor_correlation_matrix(factor_panel)
    cols = list(corr.columns)
    if score is None:
        score = pd.Series(index=cols, data=[float(len(cols) - i) for i in range(len(cols))])
    order = sorted(cols, key=lambda c: score.get(c, -1.0), reverse=True)
    retained: list[str] = []
    assigned: set[str] = set()
    for rep in order:
        if rep in assigned:
            continue
        retained.append(rep)
        assigned.add(rep)
        for d in cols:
            if d in assigned:
                continue
            if abs(corr.loc[rep, d]) > threshold:
                assigned.add(d)   # 与代表高相关 → 并入本簇，弃
    return retained


__all__ = [
    "cross_sectional_ic", "ic_series", "ic_stats", "walk_forward_folds",
    "fold_pass_rate", "bh_fdr", "by_fdr", "online_lond_alpha",
    "factor_correlation_matrix", "dedupe_correlated",
]
