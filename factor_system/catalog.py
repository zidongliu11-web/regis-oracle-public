"""M7 因子宇宙：13 内置实证因子 + 156 用户挖掘配方（策略实验室可选项）。

配方 = `registry/recipes/*.json`（`search.rebuild_factor` 可重建的事实集）；
evidence = 每次挖掘留存的证据（raw_ic / is_ic / oos_ic / ic_retention /
monotonic / neutralize_mode）。

本模块只做元数据 + 展示翻译，不执行重建。重建统一走 `compose.load_factor_map`。
导入图：catalog → compose / search（无环）；compose 内函数级懒 import catalog。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config
from .compose import REBUILDERS

# 组标签（编辑器分组依据）
GROUP_BUILTIN = "内置实证因子"
GROUP_GENETIC = "我的因子·遗传挖掘"
GROUP_TEMPLATE = "我的因子·模板"
GROUP_COMBO = "我的因子·组合"

# ── 配方公式 → 中文 轻量翻译（未知 token 原样保留，绝不臆造）─────────
_ATOM_LABEL = {
    "mom20": "20日动量", "mom60": "60日动量", "rev5": "5日反转",
    "vol20": "20日波动", "volr60_5": "60/5日波动比", "turn20": "20日换手",
    "amihud20": "20日Amihud非流动性", "rsi14": "RSI14", "macd12": "MACD12",
    "kdjj9": "KDJ-J9", "ep": "1/PE", "bp": "1/PB",
}
_OP_LABEL = {
    "ts_std10": "10日标准差", "ts_mean10": "10日均值", "ts_delta10": "10日差分",
    "ts_decay10": "10日衰减", "ts_corr10": "10日相关", "ts_zscore10": "10日Z值",
    "ts_rank10": "10日时序排名", "ts_skew10": "10日偏度", "ts_kurt10": "10日峰度",
    "ts_ir10": "10日IC比", "ts_max10": "10日最大", "ts_min10": "10日最小",
    "ts_sum10": "10日求和", "ts_median10": "10日中位", "ts_quantile10": "10日分位",
    "ts_product10": "10日连乘", "ts_argmax10": "10日峰位", "ts_argmin10": "10日谷位",
    "xs_rank": "截面排名", "xs_zscore": "截面Z值", "xs_scale": "截面缩放",
    "xs_demean": "截面去均值", "xs_winsorize": "截面去极值", "xs_sigmoid": "S形化",
    "ind_neut": "行业中性",
}
# 长 token 先替换，避免 `ts_std10` 被 `ts_std` 前缀吞掉
_OP_TOKENS = sorted(_OP_LABEL, key=len, reverse=True)
_ATOM_TOKENS = sorted(_ATOM_LABEL, key=len, reverse=True)


def factor_label(recipe: dict) -> str:
    """配方公式 → 中文短标签（≤48 字符）；翻译不出的部分原样保留。"""
    formula = str(recipe.get("formula") or recipe.get("id") or "因子")
    out = formula
    for tok in _ATOM_TOKENS:
        out = re.sub(rf"\b{tok}\b", _ATOM_LABEL[tok], out)
    for tok in _OP_TOKENS:
        out = re.sub(rf"\b{tok}\b", _OP_LABEL[tok], out)
    out = out.replace("*", "×").replace("/", "÷").replace("(", "(").replace(")", ")")
    out = out.replace("(", "（").replace(")", "）")
    return out[:48]


def _load_recipes() -> dict[str, dict]:
    """读 registry/recipes/*.json → {id: recipe}。失败只跳过单个文件（诚实降级）。"""
    d = config.REGISTRY_DIR / "recipes"
    out: dict[str, dict] = {}
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.json")):
        try:
            r = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(r, dict):
                r.setdefault("id", path.stem)
                out[path.stem] = r
        except Exception:
            continue
    return out


_RECIPES: dict[str, dict] | None = None
_RECIPES_VERSION: tuple[int, int] | None = None


def _recipe_version() -> tuple[int, int]:
    paths = list(config.REGISTRY_DIR.joinpath("recipes").glob("*.json"))
    latest = max((p.stat().st_mtime_ns for p in paths), default=0)
    return len(paths), latest


def _recipe_lookup() -> dict[str, dict]:
    """按目录版本懒加载；24h 新配方无需重启网站即可进入策略实验室。"""
    global _RECIPES, _RECIPES_VERSION
    version = _recipe_version()
    if _RECIPES is None or version != _RECIPES_VERSION:
        _RECIPES = _load_recipes()
        _RECIPES_VERSION = version
    return _RECIPES


def _group_of(recipe: dict) -> str:
    kind = recipe.get("kind")
    if kind == "template":
        return GROUP_TEMPLATE
    if kind == "combo":
        return GROUP_COMBO
    return GROUP_GENETIC


def _evidence_chips(recipe: dict) -> dict:
    """抽取展示用证据（缺项 → None，绝不编造）。"""
    ev = recipe.get("evidence") or {}
    diag = ev.get("diag") or {}
    mono = (diag.get("monotonic") or {}) if isinstance(diag, dict) else {}
    return {
        "raw_ic_mean": ev.get("raw_ic_mean"),
        "raw_ic_ir": ev.get("raw_ic_ir"),
        "is_ic_mean": ev.get("is_ic_mean"),
        "is_ic_ir": ev.get("is_ic_ir"),
        "oos_ic_mean": ev.get("oos_ic_mean"),
        "oos_ic_ir": ev.get("oos_ic_ir"),
        "ic_retention": ev.get("ic_retention"),
        "monotonic_aligned": mono.get("monotonic_aligned"),
        "monotonic_ok": mono.get("monotonic_ok"),
        "neutralize_mode": ev.get("neutralize_mode"),
    }


def _registry_rows() -> dict[str, dict]:
    """读取注册表真实状态；失败时目录仍可降级展示配方证据。"""
    try:
        from . import registry as reg
        c = reg._conn(readonly=True)
        rows = {r["factor_id"]: dict(r) for r in c.execute("SELECT * FROM factors")}
        c.close()
        return rows
    except Exception:
        return {}


def recipe_meta() -> dict[str, dict]:
    """156 配方 → {id: {id,kind,formula,group,label,description,evidence,direction_hint}}。"""
    out: dict[str, dict] = {}
    registry = _registry_rows()
    status_labels = {
        "production": "投产", "registered": "待回测", "validated": "已检验",
        "backtesting": "回测中", "rejected": "淘汰", "frozen": "冻结",
        "candidate": "候选",
    }
    for rid, recipe in _recipe_lookup().items():
        formula = str(recipe.get("formula") or "")
        chips = _evidence_chips(recipe)
        row = registry.get(rid) or {}
        for key in ("raw_ic_mean", "raw_ic_ir", "is_ic_mean", "is_ic_ir",
                    "oos_ic_mean", "oos_ic_ir", "ic_retention",
                    "monotonic_aligned", "neutralize_mode"):
            if row.get(key) is not None:
                chips[key] = row[key]
        ic = chips.get("is_ic_mean")
        if ic is not None:
            hint = ("正 IC 证据 → 正权重=买高分位" if ic >= 0
                    else "负 IC 证据 → 负权重=买低分位（A股反转）")
        else:
            hint = "证据不足，方向未知——请结合单因子诊断判断"
        real_status = row.get("status") or "research"
        out[rid] = {
            "id": rid,
            "kind": recipe.get("kind", "unknown"),
            "formula": formula,
            "group": _group_of(recipe),
            "label": factor_label(recipe),
            "description": (f"公式 {formula}；状态：{status_labels.get(real_status, real_status)}"
                            if formula else "配方因子（研究态）"),
            "evidence": chips,
            "direction_hint": hint,
            "status": real_status,
            "status_reason": row.get("status_reason"),
            "backtest_fold_pass_rate": row.get("backtest_fold_pass_rate"),
            "backtest_annual_return": row.get("backtest_annual_return"),
            "backtest_excess_annual": row.get("backtest_excess_annual"),
        }
    return out


def FACTOR_META() -> dict[str, dict]:
    """统一因子宇宙 = 13 内置实证因子 + 156 配方。

    内置因子含 label/description（沿用 FACTOR_CATALOG 文案）+ group + direction_hint
    （无配方证据 → 按经济假设给方向注释），前端证据芯片对内置因子显示「内置」。
    """
    out: dict[str, dict] = {}
    for rid, meta in recipe_meta().items():
        out[rid] = meta
    for fid in REBUILDERS:
        out[fid] = {
            "id": fid,
            "kind": "builtin",
            "formula": "",
            "group": GROUP_BUILTIN,
            "label": fid,
            "description": "内置实证因子（V4 / M4.1 因子族）",
            "evidence": None,
            "direction_hint": "方向 = 权重符号（正权重买高分位）；由策略自行编码",
            "status": "builtin",
        }
    return out
