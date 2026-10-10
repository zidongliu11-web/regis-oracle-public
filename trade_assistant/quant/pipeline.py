"""策略回测流水线.

一键运行: 数据加载 → 因子计算 → IC测试 → 因子选择 → 复合构建 → 回测 → 报告.

用法:
    from trade_assistant.quant import StrategyPipeline

    pipeline = StrategyPipeline(config)
    report = pipeline.run(panel, factors)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from .ic_tester import run_ic_tests, detect_ic_decay
from .composite import (
    build_composite,
    select_factors_cross_group,
    select_factors_top_n,
    compute_ic_weights,
)
from .logger import get_logger

logger = get_logger()


# ——— 默认配置 ———
DEFAULT_PIPELINE_CONFIG = {
    "start": "2021-01-01",
    "end": "2026-07-31",
    "horizons": [5, 10, 20],
    "min_stocks": 30,
    "cash": 100_000,
    "top_n": 5,
    "holding_days": 10,
    "commission": 0.00025,
    "stamp_tax": 0.001,
    "slippage": 0.0005,
    "stop_loss": 0.08,
    "max_position": 0.20,
    "output_dir": "reports",
}


@dataclass
class PipelineReport:
    """回测流水线报告."""
    generated_at: str = ""
    elapsed_seconds: float = 0.0
    data_summary: Dict = field(default_factory=dict)
    ic_summary: Dict = field(default_factory=dict)
    ic_ranking: List[Dict] = field(default_factory=list)
    strategies: List[Dict] = field(default_factory=list)
    verdict: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "generated_at": self.generated_at,
            "elapsed_seconds": self.elapsed_seconds,
            "data_summary": self.data_summary,
            "ic_summary": self.ic_summary,
            "ic_ranking": self.ic_ranking,
            "strategies": self.strategies,
            "verdict": self.verdict,
        }


class StrategyPipeline:
    """策略回测流水线.

    封装完整回测流程，可被 CLI 或 Web 调用。

    Example:
        >>> pipeline = StrategyPipeline({"top_n": 5})
        >>> report = pipeline.run(panel, factors_grouped)
    """

    def __init__(self, config: Optional[Dict] = None):
        self.config = {**DEFAULT_PIPELINE_CONFIG, **(config or {})}
        self._t0 = 0.0

    # ------------------------------------------------------------------
    # 数据接口
    # ------------------------------------------------------------------

    @staticmethod
    def long_to_panel(long_df: pd.DataFrame, min_stocks: int = 30) -> Dict[str, pd.DataFrame]:
        """将长格式数据转为面板格式.

        Args:
            long_df: columns=[date, code, open, high, low, close, volume, amount]
            min_stocks: 某天至少需要的股票数

        Returns:
            {"open": DataFrame, "high": ..., "low": ..., "close": ...,
             "volume": ..., "amount": ...}
        """
        required = ["date", "code", "open", "high", "low", "close", "volume"]
        for col in required:
            if col not in long_df.columns:
                raise ValueError(f"缺少必要列: {col}")

        long_df = long_df.copy()
        long_df["date"] = pd.to_datetime(long_df["date"])

        panels = {}
        for col in ["open", "high", "low", "close", "volume"]:
            panel = long_df.pivot_table(
                index="date", columns="code", values=col, aggfunc="last"
            )
            panel = panel.sort_index().sort_index(axis=1)
            # 过滤数据少的日期
            valid = panel.notna().sum(axis=1) >= min_stocks
            panels[col] = panel.loc[valid]

        if "amount" in long_df.columns:
            amt = long_df.pivot_table(
                index="date", columns="code", values="amount", aggfunc="last"
            )
            amt = amt.sort_index().sort_index(axis=1)
            valid = amt.notna().sum(axis=1) >= min_stocks
            panels["amount"] = amt.loc[valid]
        else:
            panels["amount"] = panels["close"] * panels["volume"]

        return panels

    @staticmethod
    def compute_forward_returns(
        close: pd.DataFrame,
        horizons: List[int],
    ) -> Dict[int, pd.DataFrame]:
        """计算前瞻收益.

        Args:
            close: 收盘价 DataFrame (date × stock)
            horizons: 前瞻期列表 [5, 10, 20]

        Returns:
            {horizon: fwd_return DataFrame}
        """
        fwd = {}
        for h in horizons:
            fwd[h] = close.shift(-h) / close - 1.0
        return fwd

    # ------------------------------------------------------------------
    # 回测引擎（向量化）
    # ------------------------------------------------------------------

    @staticmethod
    def _vectorized_backtest(
        panel: Dict[str, pd.DataFrame],
        composite: pd.DataFrame,
        config: Dict,
    ) -> Dict:
        """向量化回测引擎.

        Args:
            panel: 面板数据
            composite: 复合因子 DataFrame (date × stock)
            config: 回测配置
        """
        cfg = config
        close = panel["close"]

        dates = composite.index.intersection(close.index)
        stocks = composite.columns.intersection(close.columns)

        if len(dates) < 60:
            return {"error": "Not enough data days"}

        cash = cfg["cash"]
        positions: Dict[str, Dict] = {}
        nav_log = []
        trades = []
        daily_rets = []

        signal = composite.loc[dates]
        rebal_dates = set(dates[::cfg["holding_days"]])

        for i, date in enumerate(dates):
            prices = close.loc[date].reindex(stocks)

            # 止损检查
            for s in list(positions.keys()):
                pos = positions[s]
                if s in prices.index and not pd.isna(prices[s]):
                    entry = pos["cost"] / pos["shares"]
                    if (prices[s] - entry) / entry < -cfg["stop_loss"]:
                        proceeds = (pos["shares"] * prices[s] *
                                   (1 - cfg["commission"] - cfg["stamp_tax"] - cfg["slippage"]))
                        cash += proceeds
                        trades.append({
                            "date": str(date)[:10], "stock": s, "action": "SELL_STOP",
                            "price": float(prices[s]),
                            "pnl_pct": round(float((prices[s] - entry) / entry * 100), 2),
                            "reason": "止损",
                        })
                        del positions[s]

            # NAV
            pos_val = sum(
                pos["shares"] * (
                    prices.get(s, pos["cost"] / pos["shares"])
                    if s in prices.index and not pd.isna(prices.get(s))
                    else pos["cost"] / pos["shares"]
                )
                for s, pos in positions.items()
            )
            nav = cash + pos_val
            nav_log.append({"date": str(date)[:10], "nav": round(float(nav), 2)})

            if i > 0:
                prev = nav_log[-2]["nav"]
                daily_rets.append(float((nav - prev) / prev) if prev > 0 else 0.0)

            # 调仓
            if date in rebal_dates and date in signal.index:
                sig = signal.loc[date].dropna().sort_values(ascending=False)
                targets = sig.index[:cfg["top_n"]].tolist()

                # 卖出不在目标中的
                for s in list(positions.keys()):
                    if s not in targets and s in prices.index and not pd.isna(prices[s]):
                        pos = positions[s]
                        entry = pos["cost"] / pos["shares"]
                        ret = (prices[s] - entry) / entry
                        proceeds = (pos["shares"] * prices[s] *
                                   (1 - cfg["commission"] - cfg["stamp_tax"] - cfg["slippage"]))
                        cash += proceeds
                        trades.append({
                            "date": str(date)[:10], "stock": s, "action": "SELL",
                            "price": float(prices[s]),
                            "pnl_pct": round(float(ret * 100), 2),
                            "reason": "调仓卖出",
                        })
                        del positions[s]

                # 买入新标的
                n_cur = len(positions)
                new_stocks = [s for s in targets if s not in positions]
                n_buy = len(targets) - n_cur

                if n_buy > 0 and new_stocks and cash > 0:
                    per_stock = cash / n_buy
                    max_pos = nav * cfg["max_position"]
                    for s in new_stocks[:n_buy]:
                        if s in prices.index and not pd.isna(prices[s]) and prices[s] > 0:
                            alloc = min(per_stock, max_pos)
                            shares = int(alloc / prices[s] / 100) * 100
                            if shares > 0:
                                cost = shares * prices[s] * (1 + cfg["commission"] + cfg["slippage"])
                                if cost <= cash:
                                    cash -= cost
                                    positions[s] = {"shares": shares, "cost": cost}
                                    trades.append({
                                        "date": str(date)[:10], "stock": s, "action": "BUY",
                                        "price": float(prices[s]),
                                        "pnl_pct": 0, "reason": "买入",
                                    })

        # ——— 统计指标 ———
        final_nav = nav_log[-1]["nav"]
        total_ret = (final_nav - cfg["cash"]) / cfg["cash"]
        n_days = len(daily_rets)
        annual_ret = (1 + total_ret) ** (252 / n_days) - 1 if n_days > 0 else 0.0

        ret_s = pd.Series(daily_rets)
        sharpe = float(ret_s.mean() / ret_s.std() * np.sqrt(252)) if ret_s.std() > 0 else 0.0

        cum = (1 + ret_s).cumprod()
        rolling_max = cum.cummax()
        max_dd = float(((cum - rolling_max) / rolling_max).min())

        sells = [t for t in trades if t["action"] in ("SELL", "SELL_STOP")]
        wins = [t for t in sells if t["pnl_pct"] > 0]
        wr = len(wins) / len(sells) if sells else 0.0

        gross_profit = sum(t["pnl_pct"] for t in sells if t["pnl_pct"] > 0)
        gross_loss = abs(sum(t["pnl_pct"] for t in sells if t["pnl_pct"] <= 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else 999.0

        # 基准：等权全股
        bench_ret = close.pct_change().mean(axis=1)
        bench_total = float((1 + bench_ret.loc[dates]).prod() - 1)
        bench_annual = float((1 + bench_total) ** (252 / n_days) - 1) if n_days > 0 else 0.0
        alpha = annual_ret - bench_annual

        return {
            "metrics": {
                "total_return_pct": round(total_ret * 100, 2),
                "annualized_pct": round(annual_ret * 100, 2),
                "sharpe": round(sharpe, 3),
                "max_dd_pct": round(max_dd * 100, 1),
                "win_rate_pct": round(wr * 100, 1),
                "profit_factor": round(pf, 2),
                "n_trades": len(trades),
                "n_sells": len(sells),
            },
            "benchmark": {
                "total_pct": round(bench_total * 100, 2),
                "annual_pct": round(bench_annual * 100, 2),
            },
            "alpha_pct": round(alpha * 100, 2),
            "nav_log": nav_log,
            "trades": trades,
        }

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def run(
        self,
        panel: Dict[str, pd.DataFrame],
        factors: Dict[str, pd.DataFrame],
    ) -> PipelineReport:
        """运行完整回测流水线.

        Args:
            panel: 面板数据 {"open": ..., "close": ..., ...}
            factors: 因子数据 {factor_name: DataFrame(date×stock)}

        Returns:
            PipelineReport
        """
        self._t0 = time.time()
        close = panel["close"]
        n_dates = len(close.index)
        n_stocks = len(close.columns)

        logger.info(f"流水线启动: {n_dates}天 × {n_stocks}只股票, {len(factors)}个因子")

        # ——— 1. 前瞻收益 ———
        fwd_rets = self.compute_forward_returns(close, self.config["horizons"])

        # ——— 2. IC 测试 ———
        logger.info("开始IC测试...")
        ic_results = run_ic_tests(factors, fwd_rets, n_stocks=n_stocks)

        t1 = sum(1 for r in ic_results if r.tier == "t1_excellent")
        t2 = sum(1 for r in ic_results if r.tier == "t2_good")
        logger.info(f"IC测试完成: T1={t1}, T2={t2}")

        # ——— 3. 策略构建 ———
        # 策略A: 跨组分散
        factors_a = select_factors_cross_group(ic_results, max_per_group=2, max_total=8)
        weights_a = compute_ic_weights(ic_results, factors_a)

        # 策略B: 纯 IC Top 5
        factors_b = select_factors_top_n(ic_results, n=5)
        weights_b = compute_ic_weights(ic_results, factors_b)

        strategies = [
            ("策略A (跨组分散)", factors_a, weights_a),
            ("策略B (IC Top5)", factors_b, weights_b),
        ]

        # ——— 4. 回测 ———
        strategy_results = []
        for name, f_list, w_dict in strategies:
            logger.info(f"回测: {name} ({f_list})")
            composite = build_composite(factors, f_list, w_dict)
            if composite is None:
                logger.warning(f"  {name}: 复合因子构建失败")
                continue

            result = self._vectorized_backtest(panel, composite, self.config)
            if "error" in result:
                logger.warning(f"  {name}: {result['error']}")
                continue

            result["name"] = name
            result["factors"] = f_list
            result["weights"] = w_dict

            m = result["metrics"]
            logger.info(
                f"  {name}: 年化{m['annualized_pct']:.1f}% "
                f"Alpha{result['alpha_pct']:+.1f}% "
                f"Sharpe{m['sharpe']:.3f} DD{m['max_dd_pct']:.1f}%"
            )
            strategy_results.append(result)

        # ——— 5. 生成报告 ———
        elapsed = time.time() - self._t0

        verdict = self._make_verdict(strategy_results, ic_results)

        report = PipelineReport(
            generated_at=datetime.now().astimezone().isoformat(),
            elapsed_seconds=round(elapsed, 1),
            data_summary={"n_dates": n_dates, "n_stocks": n_stocks},
            ic_summary={
                "total_factors": len(ic_results),
                "t1_excellent": t1,
                "t2_good": t2,
            },
            ic_ranking=[
                {
                    "factor": r.factor,
                    "composite_ic": r.composite_ic,
                    "best_horizon": r.best_horizon,
                    "best_ic": r.best_ic,
                    "tier": r.tier,
                    "ic": {
                        str(h): {k: v for k, v in info.items() if k != "ics"}
                        for h, info in r.ic_by_horizon.items()
                    },
                }
                for r in ic_results
            ],
            strategies=strategy_results,
            verdict=verdict,
        )

        logger.info(f"流水线完成，耗时 {elapsed:.0f}s")
        return report

    def run_quick(
        self,
        long_df: pd.DataFrame,
        factor_func,
    ) -> PipelineReport:
        """从长格式数据一键运行.

        Args:
            long_df: 长格式日线数据
            factor_func: 因子计算函数，签名为 func(panel) -> {name: DataFrame}

        Returns:
            PipelineReport
        """
        panel = self.long_to_panel(long_df, min_stocks=self.config["min_stocks"])
        factors = factor_func(panel)
        return self.run(panel, factors)

    @staticmethod
    def _make_verdict(strategy_results: List[Dict], ic_results: list) -> Dict:
        """生成诊断结论."""
        messages = []
        status = "ok"

        if not strategy_results:
            return {"status": "error", "messages": ["无有效策略结果"]}

        best = max(strategy_results, key=lambda r: r.get("alpha_pct", -999))
        best_alpha = best.get("alpha_pct", 0)
        best_sharpe = best["metrics"]["sharpe"]
        best_dd = abs(best["metrics"]["max_dd_pct"])

        if best_alpha > 5:
            status = "excellent"
            messages.append(f"优秀: {best['name']} 年化超额 {best_alpha:.1f}%")
        elif best_alpha > 2:
            status = "good"
            messages.append(f"良好: {best['name']} 年化超额 {best_alpha:.1f}%")
        elif best_alpha > -2:
            status = "marginal"
            messages.append(f"一般: {best['name']} 年化超额 {best_alpha:.1f}%")
        else:
            status = "poor"
            messages.append(f"不佳: {best['name']} 年化超额 {best_alpha:.1f}%")

        if best_sharpe > 1.0:
            messages.append(f"夏普 {best_sharpe:.2f} — 良好")
        elif best_sharpe > 0.5:
            messages.append(f"夏普 {best_sharpe:.2f} — 尚可")

        if best_dd > 25:
            messages.append(f"⚠ 最大回撤偏高 ({best_dd:.1f}%) — 考虑加仓位上限制")

        t1 = sum(1 for r in ic_results if r.tier == "t1_excellent")
        if t1 >= 10:
            messages.append(f"因子质量优: {t1} 个tier-1因子")
        elif t1 < 5:
            messages.append(f"⚠ 因子质量不足: 仅 {t1} 个tier-1因子")

        return {"status": status, "messages": messages}

    def save_report(self, report: PipelineReport, output_dir: Optional[str] = None):
        """保存报告到文件."""
        out = Path(output_dir or self.config.get("output_dir", "reports"))
        out.mkdir(parents=True, exist_ok=True)

        json_path = out / "pipeline_report.json"
        json_path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"报告已保存: {json_path}")
        return json_path
