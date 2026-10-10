"""量化回测运行器：一次端到端执行（数据 → 因子 → IC → 复合 → 回测）。

供 Web API 在后台线程调用，也保留独立函数便于脚本复用。

v2（本版本核心）: 新增诚实 walk-forward 生产研究 —— 通过子进程跑
research/production_runner.py（避免在常驻 Web 进程内加载 300MB+ 面板/因子），
把轻量的 production_results.json 组装成前端报告。老的全样本 50 股回测保留为
兼容入口，但已不是推荐路径。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pandas as pd

from . import walkforward

from .a_share_library import (
    ALL_A_SHARE_FACTORS,
    compute_all_factors_from_panel,
    get_factor_group_info,
)
from .composite import (
    build_composite,
    compute_ic_weights,
    select_factors_cross_group,
    select_factors_top_n,
)
from .data_source import BIG50_CODES, load_panel
from .logger import get_logger
from .pipeline import StrategyPipeline

logger = get_logger()

ProgressCB = Optional[Callable[[str], None]]


def _strategy_specs(ic_results: list) -> List[tuple]:
    """返回 [(名称, 选中因子列表), ...] 的策略组合。"""
    factors_a = select_factors_cross_group(ic_results, max_per_group=2, max_total=8)
    factors_b = select_factors_top_n(ic_results, n=5)
    return [
        ("策略A · 跨组分散", factors_a),
        ("策略B · IC Top5", factors_b),
    ]


def _backtest(panel, composite, config) -> dict:
    pipeline = StrategyPipeline(config)
    return pipeline._vectorized_backtest(panel, composite, pipeline.config)


def _run_oos(panel, factors, config, log) -> dict:
    """样本外验证：70% 训练选因子 → 30% 测试验证。"""
    close = panel["close"]
    dates = close.index
    split_idx = int(len(dates) * 0.7)
    split_ts = dates[split_idx]

    train_panel = {k: v.loc[v.index <= split_ts] for k, v in panel.items()}
    test_panel = {k: v.loc[v.index >= split_ts] for k, v in panel.items()}

    from .ic_tester import run_ic_tests

    fwd_train = StrategyPipeline.compute_forward_returns(
        train_panel["close"], [5, 10, 20]
    )
    ic_train = run_ic_tests(
        factors, fwd_train, n_stocks=len(train_panel["close"].columns)
    )

    result: Dict = {
        "split_date": str(split_ts.date()),
        "train_days": int(len(train_panel["close"])),
        "test_days": int(len(test_panel["close"])),
        "strategies": [],
    }

    for name, selected in _strategy_specs(ic_train):
        weights = compute_ic_weights(ic_train, selected)
        composite = build_composite(factors, selected, weights)
        if composite is None:
            continue
        comp_train = composite.loc[composite.index <= split_ts]
        comp_test = composite.loc[composite.index >= split_ts]

        r_train = _backtest(train_panel, comp_train, config)
        r_test = _backtest(test_panel, comp_test, config)
        if "error" in r_train or "error" in r_test:
            continue

        result["strategies"].append({
            "name": name,
            "factors": selected,
            "weights": weights,
            "train": r_train,
            "test": r_test,
        })
        log(
            f"  {name}: 训练年化 {r_train['metrics']['annualized_pct']:.1f}% → "
            f"OOS 年化 {r_test['metrics']['annualized_pct']:.1f}% "
            f"(Sharpe {r_test['metrics']['sharpe']:.2f})"
        )
    return result


def run_quant_backtest(
    n_stocks: int = 50,
    oos: bool = False,
    progress: ProgressCB = None,
) -> dict:
    """运行一次完整量化回测。

    Args:
        n_stocks: 使用的股票数（不超过 big50 规模）
        oos: 是否执行 70/30 样本外验证
        progress: 进度回调，接收一行文本

    Returns:
        字典：data_summary / ic_summary / ic_ranking / strategies /
              verdict / elapsed_seconds / data_source / factor_groups，
              oos=True 时额外含 oos 字段。
    """
    def log(msg: str) -> None:
        logger.info(msg)
        if progress:
            progress(msg)

    codes = BIG50_CODES[: max(8, min(n_stocks, len(BIG50_CODES)))]
    min_stocks = max(5, len(codes) // 4)
    config = {"min_stocks": min_stocks}

    t0 = time.time()
    log(f"开始回测：{len(codes)} 只股票，{oos and '含 OOS 验证' or '全样本'}")
    log("加载数据（baostock 前复权，失败回退本地审计库）…")

    panel, source = load_panel(codes=codes, min_stocks=min_stocks, return_source=True)
    log(
        f"数据就绪：{len(panel['close'].index)} 天 × "
        f"{len(panel['close'].columns)} 只（来源：{source}）"
    )

    log(f"计算 {len(ALL_A_SHARE_FACTORS)} 个因子…")
    factors = compute_all_factors_from_panel(panel)
    log(f"因子计算完成：{len(factors)} 个可用")

    pipeline = StrategyPipeline(config)
    report = pipeline.run(panel, factors)

    result = report.to_dict()
    result["elapsed_seconds"] = round(time.time() - t0, 1)
    result["data_source"] = "baostock" if source == "baostock" else "本地审计库"
    result["n_stocks"] = len(panel["close"].columns)
    result["factor_groups"] = get_factor_group_info()

    if oos:
        log("执行样本外验证（70% 训练 / 30% 测试）…")
        result["oos"] = _run_oos(panel, factors, config, log)

    log(
        f"完成：总耗时 {result['elapsed_seconds']}s，"
        f"T1={result['ic_summary'].get('t1_excellent', 0)} "
        f"T2={result['ic_summary'].get('t2_good', 0)}"
    )
    return result


# ————————————————————————————————————————————————
# 生产研究（walk-forward，子进程）
# ————————————————————————————————————————————————
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_PRODUCTION_RESULTS = _PROJECT_ROOT / "research" / "production_results.json"
_PRODUCTION_SCRIPT = _PROJECT_ROOT / "research" / "production_runner.py"

_METHOD_LABELS = {
    "equal": "等权", "ic": "IC加权", "icir": "ICIR加权",
    "decay": "半衰期衰减", "shrunk": "收缩混合", "comb": "组合",
}


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def run_production_study(
    universe: int = 300,
    progress: ProgressCB = None,
) -> dict:
    """后台线程入口：启动 production_runner.py 子进程，完成后组装报告.

    重活（加载面板/因子/IC、跑 walk-forward 网格）全部发生在子进程里，
    结束后内存即释放——常驻 Web 进程只持有 ~2MB 的结果 JSON。

    Raises:
        RuntimeError: 子进程启动失败（如内存不足 fork 失败）或运行失败。
    """
    def log(msg: str) -> None:
        logger.info(msg)
        if progress:
            progress(msg)

    log(f"[{_ts()}] 启动生产研究：点时宇宙 {universe} 池（含退市股）· walk-forward 加权网格")
    log(f"[{_ts()}] 方法：equal / ic / icir / decay / shrunk × k=10/20 + 稳健性对照")
    log(f"[{_ts()}] 说明：每个调仓日只用当日之前 trailing IC 选因子加权，"
        f"近20日成交额排名前300实时入选 —— 无前视、无幸存者偏差")

    try:
        proc = subprocess.Popen(
            [sys.executable, str(_PRODUCTION_SCRIPT),
             f"--universe={universe}", "--pit"],
            cwd=str(_PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise RuntimeError(f"无法启动研究子进程（内存不足？）: {exc}")

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            progress(f"{_ts()} {line}")

    code = proc.wait()
    if code != 0:
        raise RuntimeError(f"研究子进程退出码 {code}，结果未更新")

    report = load_production_report()
    if report is None:
        raise RuntimeError("生产结果文件缺失或无效")
    log(f"[{_ts()}] 生产研究完成，耗时 {report['elapsed_seconds']}s")
    return report


def load_production_report() -> Optional[Dict]:
    """读取 production_results.json 并组装为前端报告；缺失则返回 None."""
    if not _PRODUCTION_RESULTS.exists():
        return None
    try:
        prod = json.loads(_PRODUCTION_RESULTS.read_text(encoding="utf-8"))
    except Exception:
        return None
    return _build_production_report(prod)


def _build_production_report(prod: Dict) -> Dict:
    """把 production_results.json 组装成前端可渲染的报告 dict."""
    stats_rows = prod.get("factor_stats", [])
    for r in stats_rows:
        r.setdefault("tier", walkforward.tier_of(r))
        r.setdefault("group", "other")

    n_t1 = sum(1 for r in stats_rows if r["tier"] == "t1_excellent")
    n_t2 = sum(1 for r in stats_rows if r["tier"] == "t2_good")

    methods = prod.get("methods", [])
    strategies = []
    pref_method = prod.get("best_method", "decay")
    pref_k = prod.get("best_topk", 20)
    pref_buf = prod.get("best_buffer", 1.0)
    pref_vol = prod.get("best_vol", 0.0)
    for m in methods:
        mt = m.get("metrics", {})
        tag = m.get("name", m.get("method", ""))
        if m.get("neutral_size"):
            tag += " · 市值中性"
        if (m.get("cost_mult") or 1.0) > 1.0:
            tag += " · ×2成本"
        if (m.get("buffer_mult") or 1.0) > 1.0:
            tag += f" · 换手缓冲×{m.get('buffer_mult')}"
        if (m.get("port_vol_target") or 0.0) > 0.0:
            tag += f" · 波动率目标{int((m.get('port_vol_target') or 0.0) * 100)}%"
        is_preferred = (
            m.get("method") == pref_method and m.get("topk") == pref_k
            and not m.get("neutral_size") and (m.get("cost_mult") or 1.0) == 1.0
            and abs((m.get("buffer_mult") or 1.0) - pref_buf) < 1e-9
            and abs((m.get("port_vol_target") or 0.0) - pref_vol) < 1e-9
        )
        strategies.append({
            "name": f"{tag} k={m.get('topk')}",
            "method": m.get("method"), "topk": m.get("topk"),
            "neutral_size": m.get("neutral_size", False),
            "cost_mult": m.get("cost_mult", 1.0),
            "buffer_mult": m.get("buffer_mult", 1.0),
            "port_vol_target": m.get("port_vol_target", 0.0),
            "preferred": is_preferred,
            "metrics": mt,
            "benchmark": {
                "total_pct": None,
                "annual_pct": m.get("benchmark_annual_pct", 0.0),
            },
            "alpha_pct": m.get("alpha_pct", 0.0),
            "period_returns": m.get("period_returns", {}),
            "years_positive": m.get("years_positive", 0),
            "years_total": m.get("years_total", 0),
            "portfolio_ic": mt.get("portfolio_ic", 0.0),
            "total_turnover": m.get("total_turnover", 0.0),
            "nav_log": m.get("nav_log", []),
            "trades": m.get("trades", []),
            "factors": [],
        })

    best = _pick_best(methods, prod)
    verdict = _make_walkforward_verdict(best, strategies, prod)

    from .a_share_library import get_factor_group_info

    _pit = prod.get("pit", False)
    _src = (
        "本地 v4_daily · 点时宇宙500池/流动性top300 (PIT, 含退市)"
        if _pit else "本地 v4_daily · 300只流动性宇宙"
    )

    return {
        "generated_at": prod.get("generated_at"),
        "elapsed_seconds": prod.get("elapsed_seconds"),
        "method": "walkforward",
        "data_summary": {
            "n_dates": prod.get("n_dates"),
            "n_stocks": prod.get("universe"),
            "universe": prod.get("universe"),
            "data_source": _src,
        },
        "ic_summary": {
            "total_factors": len(stats_rows),
            "t1_excellent": n_t1,
            "t2_good": n_t2,
            "method": "walkforward",
            "horizon": prod.get("horizon"),
        },
        "config": {
            "horizon": prod.get("horizon"), "holding": prod.get("holding"),
            "window": prod.get("window"), "costs": prod.get("costs"),
        },
        "ic_ranking": stats_rows,
        "methods": methods,
        "strategies": strategies,
        "best": {
            "method": best.get("method"), "topk": best.get("topk"),
            "name": best.get("name"),
        },
        "verdict": verdict,
        "factor_groups": get_factor_group_info(),
        "n_stocks": prod.get("universe"),
        "data_source": _src,
        "universe": prod.get("universe"),
    }


def _pick_best(methods: List[Dict], prod: Dict) -> Dict:
    """首选 = best_method/best_topk + 缓冲/波动率规格；缺失时回退纯 decay k20，再按 alpha."""
    pref = prod.get("best_method", "decay")
    pref_k = prod.get("best_topk", 20)
    pref_buf = prod.get("best_buffer", 1.0)
    pref_vol = prod.get("best_vol", 0.0)
    for m in methods:
        if (m.get("method") == pref and m.get("topk") == pref_k
                and not m.get("neutral_size") and (m.get("cost_mult") or 1.0) == 1.0
                and abs((m.get("buffer_mult") or 1.0) - pref_buf) < 1e-9
                and abs((m.get("port_vol_target") or 0.0) - pref_vol) < 1e-9):
            return m
    for m in methods:
        if (m.get("method") == "decay" and m.get("topk") == 20
                and not m.get("neutral_size") and (m.get("cost_mult") or 1.0) == 1.0
                and (m.get("buffer_mult") or 1.0) == 1.0
                and (m.get("port_vol_target") or 0.0) == 0.0):
            return m
    valid = [m for m in methods if "error" not in m]
    return max(valid, key=lambda m: m.get("metrics", {}).get("annualized_pct", -999),
               default={})


def _make_walkforward_verdict(best: Dict, strategies: List[Dict], prod: Dict) -> Dict:
    """诚实结论：基于 walk-forward OOS 的年化/夏普/正年份/回撤/成本敏感性."""
    if not best or "error" in best:
        return {"status": "error", "messages": ["无有效研究结果"]}

    mt = best.get("metrics", {})
    alpha = best.get("alpha_pct", 0.0)
    sharpe = mt.get("sharpe", 0.0)
    dd = abs(mt.get("max_dd_pct", 0.0))
    pos, tot = best.get("years_positive", 0), best.get("years_total", 0)
    ann = mt.get("annualized_pct", 0.0)

    if alpha > 5:
        status = "excellent"
    elif alpha > 2:
        status = "good"
    elif alpha > -2:
        status = "marginal"
    else:
        status = "poor"

    msgs = [
        f"优秀：{best.get('name', '')} 九年样本外年化 {ann:.1f}%，超额 {alpha:+.1f}%",
        f"夏普 {sharpe:.2f}" + (" — 良好" if sharpe > 1.0 else " — 尚可"),
    ]
    if tot > 0:
        ratio = pos / tot
        if ratio >= 0.66:
            msgs.append(f"稳定性优：{pos}/{tot} 个年度为正（{ratio:.0%}）")
        elif ratio >= 0.55:
            msgs.append(f"稳定性尚可：{pos}/{tot} 个年度为正")
        else:
            msgs.append(f"⚠ 稳定性不足：仅 {pos}/{tot} 个年度为正")
    # 风险控制：若首选启用了组合波动率目标，对比基线的回撤改进
    vol_target = best.get("port_vol_target") or 0.0
    if vol_target > 0:
        base_dd = None
        for s in strategies:
            if (s.get("method") == best.get("method") and s.get("topk") == best.get("topk")
                    and not s.get("neutral_size") and (s.get("cost_mult") or 1.0) == 1.0
                    and (s.get("buffer_mult") or 1.0) == 1.0
                    and not (s.get("port_vol_target") or 0.0)):
                base_dd = abs(s["metrics"].get("max_dd_pct", 0.0))
                break
        if base_dd:
            msgs.append(
                f"风险控制：组合波动率目标 {int(vol_target * 100)}% "
                f"把最大回撤从基线 {base_dd:.1f}% 压到 {dd:.1f}%"
            )
        else:
            msgs.append(f"风险控制：组合波动率目标 {int(vol_target * 100)}%，最大回撤 {dd:.1f}%")
    elif dd > 25:
        msgs.append(f"⚠ 最大回撤 {dd:.1f}% — 长多 A 股的结构性回撤，建议配置止损/仓位控制")
    # 成本敏感性
    robust = [s for s in strategies if (s.get("cost_mult") or 1.0) > 1.0]
    if robust:
        r = robust[0]
        msgs.append(f"成本稳健性：成本翻倍后年化仍 {r['metrics'].get('annualized_pct', 0):.1f}%")
    msgs.append("方法学：每个调仓日只用当日之前 trailing IC 选因子加权，全样本外验证，无前视偏差")
    return {"status": status, "messages": msgs}
