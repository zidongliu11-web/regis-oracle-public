"""
Regis Oracle v3.1 — 实时风控引擎

VaR · Beta暴露 · 行业暴露 · 因子暴露 · 最大回撤追踪。
基于可用数据（量化快照+资金流+持仓）进行实用级风控计算。
"""
from __future__ import annotations

import functools
import json
import math
import os
import statistics
import time
from collections import defaultdict
from contextvars import ContextVar
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .trading_calendar import audited_trading_day_age, last_completed_trading_day, market_regime

_PROJECT = Path(__file__).resolve().parent.parent
_INDUSTRY_MAP = _PROJECT / "research" / "industry_map.json"
_DAILY_DIR = _PROJECT / "research" / "data" / "v4_daily"
_CSI300 = _PROJECT / "research" / "data" / "current" / "sh000300.csv"
_SH_TZ = ZoneInfo("Asia/Shanghai")
_DAILY_HISTORY_EVIDENCE: ContextVar[dict | None] = ContextVar("risk_daily_history", default=None)


def _record_daily_evidence(code, evidence):
    _DAILY_HISTORY_EVIDENCE.set({**(_DAILY_HISTORY_EVIDENCE.get() or {}), code: evidence})


def _public_daily_fallback_enabled() -> bool:
    return os.environ.get("REGIS_PUBLIC_DAILY_FALLBACK") == "1"


@functools.lru_cache(maxsize=256)
def _runtime_public_daily_bars(
    code: str, limit: int, five_minute_bucket: int,
) -> tuple[tuple[str, float, float, float], ...]:
    """Bounded Tencent fallback used only by the cloud deployment."""

    del five_minute_bucket
    from trade_assistant.candles import TencentCandleProvider

    candles = TencentCandleProvider().get_candles(
        code, max(35, min(limit, 1200)), interval="1d",
    )
    rows = []
    previous = ""
    for candle in candles:
        day = str(candle.time or "")
        values = (candle.high, candle.low, candle.close)
        if (
            date.fromisoformat(day).isoformat() != day or day <= previous
            or not all(math.isfinite(float(value)) for value in values)
            or min(values) <= 0
            or candle.high < candle.close
            or candle.low > candle.close
        ):
            raise ValueError("公开历史日线无效，不跳过坏行")
        rows.append((day, float(candle.high), float(candle.low), float(candle.close)))
        previous = day
    return tuple(rows[-limit:])


def daily_history_evidence(codes: list[str]) -> dict[str, Any]:
    facts = _DAILY_HISTORY_EVIDENCE.get() or {}
    sources = {
        str(code): facts.get(str(code), {}).get("source", "unavailable")
        for code in codes if str(code)
    }
    unique = sorted(set(sources.values()))
    return {
        "status": "complete" if sources and "unavailable" not in unique else "incomplete",
        "sources": sources,
        "details": {code: facts.get(code, {"status": "unavailable", "note": "本次尚未核验历史输入"}) for code in sources},
        "evidence_tier": (
            "audited_local" if unique == ["audited_raw_v4_daily"]
            else "runtime_validated_public" if unique == ["tencent_qfq_runtime_validated"]
            else "unavailable" if not sources or unique == ["unavailable"] else "mixed"
        ),
        "execution_price_eligible": False,
        "note": (
            "云端公开前复权日线仅用于历史风险统计，不作为执行价格或生产策略准入证据。"
            if "tencent_qfq_runtime_validated" in unique else
            "历史风险输入逐股核验；缺失不代表市场无风险，历史日线不授予实时执行价格权限。"
        ),
    }


def _current_price(position: dict) -> float | None:
    """Current valuation price only; entry price is never a quote fallback."""
    try:
        price = float(position.get("current_price"))
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def _position_value(position: dict) -> float:
    price = _current_price(position)
    return price * int(position.get("shares") or 0) if price is not None else 0.0


def _daily_returns(code: str, limit: int = 252) -> dict[str, float]:
    rows = _daily_bars(code, limit + 1)
    returns: dict[str, float] = {}
    previous = None
    for row in rows:
        if row.get("tradable") is False:
            previous = None
            continue
        if row.get("price_basis") == "audited_raw":
            # Provider preclose identity is audited. Raw adjacent closes can
            # jump on ex-rights dates; this is not an investor total return.
            returns[str(row["date"])] = float(row["pctChg"]) / 100
            continue
        try:
            close = float(row.get("close") or 0)
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        if previous:
            returns[str(row.get("date") or "")] = close / previous - 1
        previous = close
    return dict(list(returns.items())[-limit:])


def _daily_bars(code: str, limit: int = 400) -> list[dict[str, float | str]]:
    from .audited_risk_history import HistoryUnavailable, load

    cutoff = last_completed_trading_day()
    _record_daily_evidence(code, {"status": "unavailable", "source": "unavailable", "execution_price_eligible": False,
                                  "note": "已完成交易日日历不可用"})
    if cutoff is None or type(limit) is not int or not 1 <= limit <= 1200:
        return []
    try:
        rows, evidence = load(_PROJECT, code, cutoff)
        _record_daily_evidence(code, evidence)
        return rows[-limit:]
    except FileNotFoundError:
        reason = "发布清单、审计或日线文件缺失；等待后台采集与审计"
        allow_public = (not (_PROJECT / "reports/v4_daily_download_manifest.json").exists()
                        and not (_DAILY_DIR / f"{code}.csv.gz").exists())
    except (OSError, EOFError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        reason = str(exc) if isinstance(exc, HistoryUnavailable) else "日线发布或审计输入无法核验"
        allow_public = False
    _record_daily_evidence(code, {"status": "unavailable", "source": "unavailable", "execution_price_eligible": False, "note": reason})
    if allow_public and _public_daily_fallback_enabled():
        try:
            public_rows = _runtime_public_daily_bars(
                code, limit, int(time.time() // 300),
            )
        except Exception:
            _record_daily_evidence(code, {"status": "unavailable", "source": "unavailable", "execution_price_eligible": False,
                                          "note": reason + "；公开历史回退也未通过"})
            return []
        rows = [
            {"date": day, "high": high, "low": low, "close": close, "price_basis": "public_qfq"}
            for day, high, low, close in public_rows
            if day <= cutoff.isoformat()
        ]
        if rows:
            _record_daily_evidence(code, {"status": "reference_only", "source": "tencent_qfq_runtime_validated",
                "data_as_of": rows[-1]["date"], "execution_price_eligible": False,
                "note": reason + "；公开前复权历史仅供统计，不与账户成本混算绝对风险线"})
        return rows
    return []


def _benchmark_returns(limit: int = 252) -> dict[str, float]:
    try:
        frame = pd.read_csv(_CSI300, usecols=["date", "close"])
    except (OSError, KeyError, ValueError):
        return _daily_returns("sh000300", limit)
    frame["date"] = pd.to_datetime(frame["date"])
    cutoff = last_completed_trading_day()
    if cutoff is None:
        return {}
    frame = frame[
        frame["date"].dt.date <= cutoff
    ].dropna().drop_duplicates("date").sort_values("date").tail(limit + 1)
    returns = frame["close"].astype(float).pct_change()
    return {
        day.date().isoformat(): float(value)
        for day, value in zip(frame["date"], returns)
        if pd.notna(value)
    }


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _get_industry(code: str) -> str:
    """获取行业简称."""
    industry_map = _load_json(_INDUSTRY_MAP)
    raw = industry_map.get(code, "") or ""
    # 使用 signal_engine 的映射逻辑简化版
    short_map = {
        "电力、热力生产和供应业": "公用事业", "煤炭开采和洗选业": "能源",
        "医药制造业": "医药生物", "计算机、通信和其他电子设备制造业": "电子",
        "电气机械和器材制造业": "电力设备", "汽车制造业": "汽车",
        "软件和信息技术服务业": "计算机", "食品制造业": "食品饮料",
        "化学原料和化学制品制造业": "化工", "专用设备制造业": "机械设备",
        "有色金属矿采选业": "有色", "房地产业": "房地产",
        "货币金融服务": "银行", "资本市场服务": "非银金融",
        "保险业": "非银金融", "建筑业": "建筑装饰",
        "黑色金属冶炼和压延加工业": "钢铁",
        "交通运输、仓储和邮政业": "交通运输",
        "纺织业": "纺织服装", "互联网和相关服务": "计算机",
        "批发业": "商贸零售", "零售业": "商贸零售",
        "通用设备制造业": "机械设备", "非金属矿物制品业": "建材",
    }
    return short_map.get(raw, raw)


def calculate_portfolio_var(positions: list[dict]) -> dict[str, Any]:
    """Historical-simulation VaR from audited daily closes and real shares."""
    if not positions:
        return {"var_95": 0, "var_99": 0, "var_95_pct": 0, "var_99_pct": 0,
                "total_value": 0, "position_count": 0, "available": True,
                "method": "historical_simulation"}
    missing_quotes = [p.get("code") for p in positions if _current_price(p) is None]
    if missing_quotes:
        return {
            "var_95": None, "var_99": None, "var_95_pct": None, "var_99_pct": None,
            "total_value": None, "position_count": len(positions),
            "portfolio_daily_vol_pct": None, "available": False,
            "reason": f"{len(missing_quotes)} 个持仓缺少有效现价，无法计算组合VaR。",
            "missing_quote_codes": missing_quotes,
            "coverage_pct": round((len(positions) - len(missing_quotes)) / len(positions) * 100, 1),
            "method": "historical_simulation",
        }
    values: dict[str, float] = defaultdict(float)
    for position in positions:
        value = _position_value(position)
        if value > 0:
            values[position["code"]] += value
    histories = {code: _daily_returns(code) for code in values}
    usable = {code: rows for code, rows in histories.items() if len(rows) >= 20}
    total_value = sum(values.values())
    missing_history = sorted(set(values) - set(usable))
    if missing_history:
        covered_value = sum(values[code] for code in usable)
        return {
            "var_95": None, "var_99": None, "var_95_pct": None, "var_99_pct": None,
            "total_value": round(total_value, 2), "position_count": len(positions),
            "portfolio_daily_vol_pct": None, "available": False,
            "reason": f"{len(missing_history)} 个持仓缺少至少20个交易日的审计日线，不得从组合VaR中省略。",
            "missing_history_codes": missing_history,
            "coverage_pct": round(covered_value / total_value * 100, 1) if total_value else 0,
            "method": "historical_simulation",
        }
    if not usable:
        return {
            "var_95": None, "var_99": None, "var_95_pct": None, "var_99_pct": None,
            "total_value": round(total_value, 2), "position_count": len(positions),
            "portfolio_daily_vol_pct": None, "available": False,
            "reason": "缺少至少20个共同交易日的审计日线，无法计算历史VaR。",
            "coverage_pct": 0, "method": "historical_simulation",
        }
    common_dates = sorted(set.intersection(*(set(rows) for rows in usable.values())))
    if len(common_dates) < 20:
        return {
            "var_95": None, "var_99": None, "var_95_pct": None, "var_99_pct": None,
            "total_value": round(total_value, 2), "position_count": len(positions),
            "portfolio_daily_vol_pct": None, "available": False,
            "reason": "持仓之间缺少至少20个共同交易日，无法计算历史VaR。",
            "coverage_pct": 0, "method": "historical_simulation",
        }
    covered_value = sum(values[code] for code in usable)
    weights = {code: values[code] / covered_value for code in usable}
    portfolio_returns = [
        sum(weights[code] * usable[code][day] for code in usable)
        for day in common_dates
    ]
    losses = sorted(-value for value in portfolio_returns)
    percentile = lambda level: max(0.0, losses[min(len(losses) - 1, int(level * len(losses)))])
    loss95, loss99 = percentile(0.95), percentile(0.99)
    return {
        "var_95": round(covered_value * loss95, 2),
        "var_99": round(covered_value * loss99, 2),
        "var_95_pct": round(loss95 * 100, 2),
        "var_99_pct": round(loss99 * 100, 2),
        "total_value": round(total_value, 2),
        "position_count": len(positions),
        "portfolio_daily_vol_pct": round(statistics.stdev(portfolio_returns) * 100, 2),
        "available": True,
        "coverage_pct": round(covered_value / total_value * 100, 1) if total_value else 0,
        "observations": len(portfolio_returns),
        "data_as_of": common_dates[-1],
        "method": "historical_simulation",
    }


def calculate_volatility_managed_exposure(
    positions: list[dict],
    *,
    current_exposure_pct: float | None,
    market_state_cap_pct: float | None,
) -> dict[str, Any]:
    """Past-only volatility overlay for current-account risk reduction.

    This is deliberately not a stock signal.  It uses current market-value
    weights and completed daily returns, and can only preserve or reduce the
    account's current exposure.  Exact sell quantities remain a separate
    execution problem because T+1 inventory and current tradability are not
    represented here.
    """
    method = "vmp_retreat_v1_past_only_current_weights"
    if not positions:
        return {
            "available": True,
            "status": "not_applicable_no_positions",
            "method": method,
            "current_exposure_pct": 0.0,
            "target_exposure_pct": 0.0,
            "reduction_need_pct_points": 0.0,
            "can_increase_exposure": False,
            "exact_share_quantity": None,
            "research_only": True,
        }

    try:
        current_exposure = float(current_exposure_pct)
        state_cap = float(market_state_cap_pct)
    except (TypeError, ValueError):
        current_exposure = state_cap = math.nan
    if (
        not math.isfinite(current_exposure)
        or not 0 <= current_exposure <= 100
        or not math.isfinite(state_cap)
        or not 0 <= state_cap <= 100
    ):
        return {
            "available": False,
            "status": "unavailable_invalid_exposure_inputs",
            "reason": "当前总仓位或市场状态仓位上限缺失/越界，波动缩放不计算。",
            "method": method,
            "target_exposure_pct": None,
            "exact_share_quantity": None,
            "can_increase_exposure": False,
            "research_only": True,
        }

    values: dict[str, float] = defaultdict(float)
    invalid_positions = []
    for position in positions:
        code = str(position.get("code") or "")
        shares = int(position.get("shares") or 0)
        price = _current_price(position)
        if not code or shares <= 0 or price is None:
            invalid_positions.append(code or "unknown")
            continue
        values[code] += shares * price
    if invalid_positions or len(values) != len({
        str(position.get("code") or "") for position in positions
    }):
        return {
            "available": False,
            "status": "unavailable_invalid_positions",
            "reason": "存在代码、股数或现价不完整的持仓；不得省略后计算组合波动。",
            "invalid_position_codes": sorted(set(invalid_positions)),
            "method": method,
            "target_exposure_pct": None,
            "exact_share_quantity": None,
            "can_increase_exposure": False,
            "research_only": True,
        }
    total_value = sum(values.values())
    if total_value <= 0:
        return {
            "available": False,
            "status": "unavailable_non_positive_market_value",
            "reason": "当前持仓总市值无效，波动缩放不计算。",
            "method": method,
            "target_exposure_pct": None,
            "exact_share_quantity": None,
            "can_increase_exposure": False,
            "research_only": True,
        }

    histories = {code: _daily_returns(code, limit=400) for code in values}
    missing_history = sorted(code for code, rows in histories.items() if len(rows) < 272)
    if missing_history:
        return {
            "available": False,
            "status": "unavailable_insufficient_history",
            "reason": "持仓缺少至少272个交易日的完整日线，不能省略后计算波动仓位。",
            "missing_history_codes": missing_history,
            "minimum_common_observations": 272,
            "method": method,
            "target_exposure_pct": None,
            "exact_share_quantity": None,
            "can_increase_exposure": False,
            "research_only": True,
        }
    common_dates = sorted(set.intersection(*(set(rows) for rows in histories.values())))
    if len(common_dates) < 272:
        return {
            "available": False,
            "status": "unavailable_insufficient_common_history",
            "reason": "全部持仓之间缺少至少272个共同交易日，波动仓位不计算。",
            "common_observations": len(common_dates),
            "minimum_common_observations": 272,
            "method": method,
            "target_exposure_pct": None,
            "exact_share_quantity": None,
            "can_increase_exposure": False,
            "research_only": True,
        }

    common_dates = common_dates[-272:]
    weights = {code: value / total_value for code, value in values.items()}
    portfolio_returns = [
        sum(weights[code] * histories[code][day] for code in weights)
        for day in common_dates
    ]

    def realized_variance(end_index: int) -> float:
        window = portfolio_returns[end_index - 19:end_index + 1]
        return 252.0 / 20.0 * sum(value * value for value in window)

    current_rv = realized_variance(len(portfolio_returns) - 1)
    baseline_rvs = [
        realized_variance(end_index)
        for end_index in range(len(portfolio_returns) - 253, len(portfolio_returns) - 1)
    ]
    baseline_rv = statistics.median(baseline_rvs)
    multiplier = 1.0 if current_rv == 0 else max(0.0, min(1.0, baseline_rv / current_rv))
    base_exposure = min(current_exposure, state_cap, 80.0)
    target_exposure = min(current_exposure, base_exposure * multiplier)
    return {
        "available": True,
        "status": "ready_research_risk_overlay",
        "method": method,
        "data_as_of": common_dates[-1],
        "common_observations": len(common_dates),
        "baseline_observations": len(baseline_rvs),
        "current_exposure_pct": round(current_exposure, 4),
        "market_state_cap_pct": round(state_cap, 4),
        "base_exposure_pct": round(base_exposure, 4),
        "current_realized_variance_20": round(current_rv, 10),
        "past_only_median_realized_variance_20": round(baseline_rv, 10),
        "current_annualized_volatility_pct": round(math.sqrt(current_rv) * 100, 4),
        "baseline_annualized_volatility_pct": round(math.sqrt(baseline_rv) * 100, 4),
        "volatility_multiplier": round(multiplier, 6),
        "target_exposure_pct": round(target_exposure, 4),
        "reduction_need_pct_points": round(max(0.0, current_exposure - target_exposure), 4),
        "can_increase_exposure": False,
        "exact_share_quantity": None,
        "execution_authority": False,
        "research_only": True,
        "profitability_claim": False,
    }


def calculate_beta_exposure(positions: list[dict]) -> dict[str, Any]:
    """Actual market-value-weighted regression beta against audited CSI 300."""
    if not positions:
        return {"beta": None, "exposure": "无持仓", "details": [], "available": True}
    missing_quotes = [p.get("code") for p in positions if _current_price(p) is None]
    if missing_quotes:
        return {
            "beta": None,
            "exposure": f"{len(missing_quotes)} 个持仓缺少有效现价，Beta暂不可计算",
            "details": [], "benchmark": "沪深300", "available": False,
            "missing_quote_codes": missing_quotes,
        }
    benchmark = _benchmark_returns()
    values: dict[str, float] = defaultdict(float)
    for position in positions:
        value = _position_value(position)
        if value > 0:
            values[position["code"]] += value
    histories = {code: _daily_returns(code) for code in values}
    usable = {code: rows for code, rows in histories.items() if len(rows) >= 20}
    if not benchmark or not usable:
        return {"beta": None, "exposure": "缺少至少20个共同交易日，Beta暂不可计算",
                "details": [], "benchmark": "沪深300", "available": False}
    missing_history = sorted(set(values) - set(usable))
    if missing_history:
        covered = sum(values[code] for code in usable)
        total = sum(values.values())
        return {
            "beta": None,
            "exposure": f"{len(missing_history)} 个持仓缺少至少20个交易日的审计日线，不得从组合Beta中省略",
            "details": [], "benchmark": "沪深300", "available": False,
            "missing_history_codes": missing_history,
            "coverage_pct": round(covered / total * 100, 1) if total else 0,
        }
    common = sorted(set(benchmark).intersection(*(set(rows) for rows in usable.values())))
    if len(common) < 20:
        return {"beta": None, "exposure": "持仓与沪深300缺少至少20个共同交易日",
                "details": [], "benchmark": "沪深300", "available": False}
    freshness = audited_trading_day_age(common[-1])
    if freshness.get("status") != "fresh":
        age = freshness.get("days")
        age_text = f"，已落后 {age} 个已完成交易日" if isinstance(age, int) else ""
        return {
            "beta": None,
            "exposure": f"持仓与沪深300共同日线截至 {common[-1]}{age_text}，Beta停用",
            "details": [],
            "benchmark": "沪深300",
            "available": False,
            "status": "unavailable_stale_common_history",
            "data_as_of": common[-1],
            "freshness": freshness,
            "coverage_pct": 100.0,
        }
    covered = sum(values[code] for code in usable)
    weights = {code: values[code] / covered for code in usable}
    market = [benchmark[day] for day in common]
    variance = statistics.variance(market)
    if variance <= 0:
        return {"beta": None, "exposure": "沪深300方差无效，Beta暂不可计算",
                "details": [], "benchmark": "沪深300", "available": False}
    details = []
    portfolio_returns = [sum(weights[code] * usable[code][day] for code in usable) for day in common]
    for code in usable:
        stock = [usable[code][day] for day in common]
        details.append({"code": code, "beta": round(statistics.covariance(stock, market) / variance, 3),
                        "weight_pct": round(weights[code] * 100, 2)})
    beta = statistics.covariance(portfolio_returns, market) / variance
    return {
        "beta": round(beta, 3),
        "exposure": "高于市场" if beta > 1.2 else "低于市场" if beta < 0.8 else "接近市场",
        "details": details,
        "benchmark": "沪深300",
        "available": True,
        "observations": len(common),
        "data_as_of": common[-1],
        "coverage_pct": round(covered / sum(values.values()) * 100, 1),
        "method": "OLS beta = covariance(portfolio, CSI300) / variance(CSI300)",
    }


def _audited_return_panel(
    positions: list[dict], *, minimum_observations: int = 60,
) -> dict[str, Any]:
    """Build one value-weighted, same-date return panel without dropping holdings."""
    if not positions:
        return {
            "available": True, "status": "not_applicable_no_positions",
            "values": {}, "weights": {}, "histories": {}, "dates": [],
        }
    missing_quotes = sorted({
        str(position.get("code") or "") for position in positions
        if _current_price(position) is None
    })
    if missing_quotes:
        return {
            "available": False,
            "status": "unavailable_missing_quotes",
            "reason": "持仓现价不完整，不能计算组合相关性或历史压力。",
            "missing_quote_codes": missing_quotes,
        }
    values: dict[str, float] = defaultdict(float)
    for position in positions:
        value = _position_value(position)
        if value > 0:
            values[str(position.get("code") or "")] += value
    histories = {code: _daily_returns(code) for code in values}
    missing_history = sorted(
        code for code, rows in histories.items()
        if len(rows) < minimum_observations
    )
    if missing_history:
        return {
            "available": False,
            "status": "unavailable_insufficient_history",
            "reason": (
                f"{len(missing_history)} 个持仓缺少至少"
                f"{minimum_observations}个交易日，不能省略后计算组合依赖。"
            ),
            "missing_history_codes": missing_history,
        }
    if not histories:
        return {
            "available": False, "status": "unavailable_no_positive_positions",
            "reason": "没有可计算的正持仓市值。",
        }
    common_dates = sorted(set.intersection(*(
        set(rows) for rows in histories.values()
    )))
    if len(common_dates) < minimum_observations:
        return {
            "available": False,
            "status": "unavailable_insufficient_common_history",
            "reason": (
                f"持仓之间缺少至少{minimum_observations}个共同交易日，"
                "不能计算组合依赖。"
            ),
            "observations": len(common_dates),
        }
    total_value = sum(values.values())
    return {
        "available": True,
        "status": "ready",
        "values": dict(values),
        "weights": {code: value / total_value for code, value in values.items()},
        "histories": histories,
        "dates": common_dates,
        "total_value": total_value,
        "observations": len(common_dates),
        "data_as_of": common_dates[-1],
    }


def calculate_portfolio_dependency(positions: list[dict]) -> dict[str, Any]:
    """Decompose sample covariance risk instead of counting ticker symbols."""
    panel = _audited_return_panel(positions)
    if panel.get("available") is not True:
        return {
            **panel, "risk_contributions": [], "high_correlation_pairs": [],
            "method": "sample_covariance_marginal_variance_contribution_v1",
        }
    codes = sorted(panel["weights"])
    if not codes:
        return {
            "available": True, "status": "not_applicable_no_positions",
            "risk_contributions": [], "high_correlation_pairs": [],
            "effective_risk_bets": 0, "largest_risk_contribution_pct": 0,
            "method": "sample_covariance_marginal_variance_contribution_v1",
        }
    dates = panel["dates"]
    series = {
        code: [panel["histories"][code][day] for day in dates]
        for code in codes
    }
    variances = {code: statistics.variance(series[code]) for code in codes}
    covariance = {
        (left, right): (
            variances[left] if left == right
            else statistics.covariance(series[left], series[right])
        )
        for left in codes for right in codes
    }
    weights = panel["weights"]
    portfolio_variance = sum(
        weights[left] * weights[right] * covariance[left, right]
        for left in codes for right in codes
    )
    if portfolio_variance <= 0 or not math.isfinite(portfolio_variance):
        return {
            "available": False, "status": "unavailable_nonpositive_variance",
            "reason": "共同样本的组合方差无效，不能计算风险贡献。",
            "risk_contributions": [], "high_correlation_pairs": [],
            "observations": panel["observations"],
            "data_as_of": panel["data_as_of"],
            "method": "sample_covariance_marginal_variance_contribution_v1",
        }
    contributions = []
    raw_contributions: dict[str, float] = {}
    for code in codes:
        contribution = (
            weights[code]
            * sum(covariance[code, other] * weights[other] for other in codes)
            / portfolio_variance
        )
        raw_contributions[code] = contribution
        contributions.append({
            "code": code,
            "market_value_weight_pct": round(weights[code] * 100, 4),
            "risk_contribution_pct": round(contribution * 100, 4),
            "daily_volatility_pct": round(math.sqrt(variances[code]) * 100, 4),
        })
    standalone_weighted_risk = sum(
        weights[code] * math.sqrt(variances[code]) for code in codes
    )
    effective_bets = min(
        float(len(codes)),
        standalone_weighted_risk * standalone_weighted_risk / portfolio_variance,
    )
    pairs = []
    weighted_corr_sum = 0.0
    weighted_corr_denominator = 0.0
    for index, left in enumerate(codes):
        for right in codes[index + 1:]:
            denominator = math.sqrt(variances[left] * variances[right])
            if denominator <= 0:
                continue
            correlation = max(-1.0, min(1.0, covariance[left, right] / denominator))
            pair_weight = weights[left] * weights[right]
            weighted_corr_sum += pair_weight * correlation
            weighted_corr_denominator += pair_weight
            if correlation >= 0.8:
                pairs.append({
                    "left": left, "right": right,
                    "correlation": round(correlation, 4),
                })
    contributions.sort(
        key=lambda row: row["risk_contribution_pct"], reverse=True,
    )
    return {
        "available": True,
        "status": "ready",
        "risk_contributions": contributions,
        "largest_risk_contribution_pct": contributions[0]["risk_contribution_pct"],
        "dominant_code": contributions[0]["code"],
        "effective_risk_bets": round(effective_bets, 4) if effective_bets else None,
        "weighted_average_correlation": (
            round(weighted_corr_sum / weighted_corr_denominator, 4)
            if weighted_corr_denominator > 0 else None
        ),
        "maximum_pair_correlation": max(
            (row["correlation"] for row in pairs), default=None,
        ),
        "high_correlation_pairs": sorted(
            pairs, key=lambda row: row["correlation"], reverse=True,
        ),
        "observations": panel["observations"],
        "data_as_of": panel["data_as_of"],
        "method": "sample_covariance_marginal_variance_contribution_v1",
        "profit_optimality_claim": False,
    }


def calculate_candidate_dependency(
    positions: list[dict], candidate_code: str,
) -> dict[str, Any]:
    """Measure a candidate's same-date correlation to the rest of the account."""
    code = str(candidate_code or "").strip()
    others = [
        position for position in positions
        if str(position.get("code") or "") != code
    ]
    if not others:
        return {
            "available": True, "status": "not_applicable_no_other_positions",
            "candidate_code": code, "portfolio_correlation": None,
            "maximum_position_correlation": None,
            "method": "candidate_to_existing_value_weighted_portfolio_correlation_v1",
        }
    panel = _audited_return_panel(others)
    if panel.get("available") is not True:
        return {
            **panel, "candidate_code": code, "portfolio_correlation": None,
            "maximum_position_correlation": None,
            "method": "candidate_to_existing_value_weighted_portfolio_correlation_v1",
        }
    candidate = _daily_returns(code)
    common_dates = sorted(set(candidate).intersection(panel["dates"]))
    if len(common_dates) < 60:
        return {
            "available": False,
            "status": "unavailable_insufficient_candidate_history",
            "candidate_code": code,
            "portfolio_correlation": None,
            "maximum_position_correlation": None,
            "observations": len(common_dates),
            "reason": "候选与现有持仓缺少至少60个共同交易日。",
            "method": "candidate_to_existing_value_weighted_portfolio_correlation_v1",
        }
    candidate_series = [candidate[day] for day in common_dates]
    portfolio_series = [
        sum(
            panel["weights"][held_code]
            * panel["histories"][held_code][day]
            for held_code in panel["weights"]
        )
        for day in common_dates
    ]
    candidate_variance = statistics.variance(candidate_series)
    portfolio_variance = statistics.variance(portfolio_series)
    if candidate_variance <= 0 or portfolio_variance <= 0:
        return {
            "available": False, "status": "unavailable_nonpositive_variance",
            "candidate_code": code, "portfolio_correlation": None,
            "maximum_position_correlation": None,
            "reason": "候选或组合方差无效，相关性不可计算。",
            "method": "candidate_to_existing_value_weighted_portfolio_correlation_v1",
        }
    portfolio_correlation = statistics.covariance(
        candidate_series, portfolio_series,
    ) / math.sqrt(candidate_variance * portfolio_variance)
    position_correlations = []
    for held_code in panel["weights"]:
        held_series = [panel["histories"][held_code][day] for day in common_dates]
        held_variance = statistics.variance(held_series)
        if held_variance <= 0:
            continue
        correlation = statistics.covariance(
            candidate_series, held_series,
        ) / math.sqrt(candidate_variance * held_variance)
        position_correlations.append({
            "code": held_code,
            "correlation": round(max(-1.0, min(1.0, correlation)), 4),
        })
    position_correlations.sort(key=lambda row: row["correlation"], reverse=True)
    return {
        "available": True,
        "status": "ready",
        "candidate_code": code,
        "portfolio_correlation": round(
            max(-1.0, min(1.0, portfolio_correlation)), 4,
        ),
        "maximum_position_correlation": (
            position_correlations[0]["correlation"]
            if position_correlations else None
        ),
        "position_correlations": position_correlations,
        "observations": len(common_dates),
        "data_as_of": common_dates[-1],
        "method": "candidate_to_existing_value_weighted_portfolio_correlation_v1",
        "profit_optimality_claim": False,
    }


def calculate_historical_stress(positions: list[dict]) -> dict[str, Any]:
    """Observed portfolio tail losses; no distribution or crisis scenario invented."""
    panel = _audited_return_panel(positions)
    if panel.get("available") is not True:
        return {
            **panel,
            "method": "same_date_historical_portfolio_tail_observation_v1",
        }
    if not panel["weights"]:
        return {
            "available": True, "status": "not_applicable_no_positions",
            "worst_1d_pct": 0, "worst_5d_pct": 0,
            "expected_shortfall_95_pct": 0,
            "method": "same_date_historical_portfolio_tail_observation_v1",
        }
    portfolio_returns = [
        sum(
            panel["weights"][code] * panel["histories"][code][day]
            for code in panel["weights"]
        )
        for day in panel["dates"]
    ]
    worst_index = min(range(len(portfolio_returns)), key=portfolio_returns.__getitem__)
    rolling_five = []
    for index in range(4, len(portfolio_returns)):
        compounded = math.prod(
            1.0 + value for value in portfolio_returns[index - 4:index + 1]
        ) - 1.0
        rolling_five.append((compounded, panel["dates"][index]))
    worst_five, worst_five_end = min(rolling_five, default=(0.0, None))
    tail_count = max(1, math.ceil(len(portfolio_returns) * 0.05))
    expected_shortfall = -statistics.mean(sorted(portfolio_returns)[:tail_count])
    return {
        "available": True,
        "status": "ready",
        "worst_1d_pct": round(portfolio_returns[worst_index] * 100, 4),
        "worst_1d_date": panel["dates"][worst_index],
        "worst_5d_pct": round(worst_five * 100, 4),
        "worst_5d_end_date": worst_five_end,
        "expected_shortfall_95_pct": round(expected_shortfall * 100, 4),
        "observations": panel["observations"],
        "data_as_of": panel["data_as_of"],
        "method": "same_date_historical_portfolio_tail_observation_v1",
        "scenario_invented": False,
        "future_loss_bound_claim": False,
    }


def build_manual_nav_curve(
    ledger: list[dict], initial_capital: float, *,
    initial_equity_as_of: str | None = None,
    current_account_equity: float | None = None,
    current_account_equity_as_of: str | None = None,
    account_nav_snapshots: list[dict] | None = None,
    account_cashflows: list[dict] | None = None,
) -> list[dict[str, Any]]:
    """Build observed NAV; external flows use time-weighted Modified Dietz."""
    if initial_equity_as_of and current_account_equity_as_of:
        try:
            baseline_at = datetime.fromisoformat(
                str(initial_equity_as_of).replace("Z", "+00:00")
            )
            current_at = datetime.fromisoformat(
                str(current_account_equity_as_of).replace("Z", "+00:00")
            )
        except ValueError:
            baseline_at = current_at = None
        if (
            baseline_at is not None and current_at is not None
            and baseline_at.tzinfo is not None and current_at.tzinfo is not None
            and initial_capital > 0
            and current_account_equity is not None
            and math.isfinite(float(current_account_equity))
            and float(current_account_equity) > 0
            and current_at >= baseline_at
        ):
            points = [{
                "at": baseline_at,
                "raw_equity_yuan": round(float(initial_capital), 2),
                "source": "user_supplied_equity_baseline",
            }]
            for snapshot in account_nav_snapshots or []:
                try:
                    snapshot_at = datetime.fromisoformat(
                        str(snapshot.get("valuation_as_of") or "").replace(
                            "Z", "+00:00",
                        )
                    )
                    equity = float(snapshot.get("account_equity_yuan"))
                except (TypeError, ValueError):
                    continue
                if (
                    snapshot_at.tzinfo is None or snapshot_at <= baseline_at
                    or not math.isfinite(equity) or equity <= 0
                ):
                    continue
                points.append({
                    "at": snapshot_at, "raw_equity_yuan": round(equity, 2),
                    "source": "daily_account_nav_snapshot",
                    "snapshot_id": snapshot.get("id"),
                    "valuation_basis": snapshot.get("valuation_basis"),
                })
            latest_stored_day = max(
                (
                    point["at"].date() for point in points
                    if point["source"] == "daily_account_nav_snapshot"
                ),
                default=None,
            )
            if latest_stored_day != current_at.date():
                points.append({
                    "at": current_at,
                    "raw_equity_yuan": round(float(current_account_equity), 2),
                    "source": "current_account_cash_plus_marked_positions",
                })
            points.sort(key=lambda point: point["at"])
            # One daily snapshot per day is authoritative; if a provisional
            # observation shares that day, retain the frozen snapshot only.
            daily: dict[str, dict] = {}
            for point in points:
                day = point["at"].date().isoformat()
                if (
                    day not in daily
                    or point["source"] == "daily_account_nav_snapshot"
                ):
                    daily[day] = point
            points = sorted(daily.values(), key=lambda point: point["at"])

            flows = []
            for event in account_cashflows or []:
                try:
                    flow_at = datetime.fromisoformat(
                        str(event.get("effective_at") or "").replace(
                            "Z", "+00:00",
                        )
                    )
                    amount = float(event.get("amount_yuan"))
                except (TypeError, ValueError):
                    continue
                if (
                    flow_at.tzinfo is None or not math.isfinite(amount)
                    or amount <= 0 or flow_at <= baseline_at
                ):
                    continue
                signed = amount if event.get("flow_type") == "deposit" else -amount
                if event.get("flow_type") not in {"deposit", "withdrawal"}:
                    continue
                flows.append({"at": flow_at, "signed_yuan": signed})

            observed = [{
                "date": points[0]["at"].date().isoformat(),
                "valuation_as_of": points[0]["at"].isoformat(),
                "value": round(float(initial_capital), 2),
                "raw_equity_yuan": round(float(initial_capital), 2),
                "source": points[0]["source"],
                "net_external_flow_yuan": 0.0,
                "period_return_pct": None,
                "cashflow_method": "modified_dietz_recorded_events_only",
            }]
            cumulative = 1.0
            for previous, point in zip(points, points[1:]):
                seconds = (point["at"] - previous["at"]).total_seconds()
                if seconds <= 0:
                    continue
                interval_flows = [
                    flow for flow in flows
                    if previous["at"] < flow["at"] <= point["at"]
                ]
                net_flow = sum(flow["signed_yuan"] for flow in interval_flows)
                weighted_flow = sum(
                    flow["signed_yuan"]
                    * max(
                        0.0,
                        min(
                            1.0,
                            (point["at"] - flow["at"]).total_seconds()
                            / seconds,
                        ),
                    )
                    for flow in interval_flows
                )
                denominator = previous["raw_equity_yuan"] + weighted_flow
                period_pnl = (
                    point["raw_equity_yuan"]
                    - previous["raw_equity_yuan"] - net_flow
                )
                if denominator <= 0:
                    return []
                period_return = period_pnl / denominator
                cumulative *= 1.0 + period_return
                observed.append({
                    "date": point["at"].date().isoformat(),
                    "valuation_as_of": point["at"].isoformat(),
                    "value": round(float(initial_capital) * cumulative, 2),
                    "raw_equity_yuan": point["raw_equity_yuan"],
                    "source": point["source"],
                    "net_external_flow_yuan": round(net_flow, 2),
                    "period_return_pct": round(period_return * 100, 6),
                    "cashflow_method": "modified_dietz_recorded_events_only",
                    **(
                        {"snapshot_id": point.get("snapshot_id")}
                        if point.get("snapshot_id") is not None else {}
                    ),
                })
            return observed
    if not ledger or initial_capital <= 0:
        return []
    dated = [row for row in ledger if row.get("created_at")]
    if not dated:
        return []
    benchmark_dates = sorted(_benchmark_returns(limit=800))
    start = min(str(row["created_at"])[:10] for row in dated)
    dates = [day for day in benchmark_dates if day >= start]
    if not dates:
        return []
    closes = {row["code"]: {bar["date"]: bar["close"] for bar in _daily_bars(row["code"], 900)} for row in dated}
    cash = float(initial_capital)
    holdings: dict[int, dict] = {}
    curve = []
    for day in dates:
        for row in dated:
            row_id = int(row.get("id") or id(row))
            if str(row.get("created_at"))[:10] == day and row_id not in holdings:
                shares = int(row.get("initial_shares") or row.get("shares") or 0)
                price = float(row.get("price") or 0)
                cash -= shares * price + float(row.get("buy_cost") or 0)
                holdings[row_id] = {"row": row, "shares": shares}
            sell_events = row.get("sell_events") or []
            if sell_events:
                for event in sell_events:
                    if str(event.get("sold_at") or "")[:10] != day or row_id not in holdings:
                        continue
                    cash += float(event.get("amount") or 0) - float(event.get("sell_cost") or 0)
                    holdings[row_id]["shares"] = max(
                        0,
                        int(holdings[row_id]["shares"]) - int(event.get("shares") or 0),
                    )
                    if holdings[row_id]["shares"] == 0:
                        holdings.pop(row_id, None)
            elif str(row.get("sold_at") or "")[:10] == day and row_id in holdings:
                # Legacy rows pre-date immutable sell events.  Preserve their
                # recorded fill while using any migrated cost fields available.
                shares = int(holdings[row_id]["shares"])
                cash += shares * float(row.get("sold_price") or 0) - float(row.get("sell_cost") or 0)
                holdings.pop(row_id, None)
        value = cash
        for holding in holdings.values():
            row = holding["row"]
            code = row["code"]
            available = [d for d in closes.get(code, {}) if d <= day]
            mark = closes[code][max(available)] if available else float(row.get("price") or 0)
            value += int(holding["shares"]) * mark
        curve.append({"date": day, "value": round(value, 2)})
    return curve


def calculate_industry_exposure(positions: list[dict]) -> dict[str, Any]:
    """计算行业暴露热力图数据."""
    if not positions:
        return {"exposures": [], "concentration_risk": "无持仓",
                "max_single": 0, "hhi": 0, "available": True}

    missing_quotes = [p.get("code") for p in positions if _current_price(p) is None]
    if missing_quotes:
        return {
            "exposures": [], "concentration_risk": "现价缺失，行业暴露不可计算",
            "max_single": None, "hhi": None, "available": False,
            "missing_quote_codes": missing_quotes,
        }

    industries = {
        str(p.get("code") or ""): (p.get("industry") or _get_industry(p["code"]) or "")
        for p in positions
    }
    missing_industries = sorted(code for code, industry in industries.items() if not industry)
    if missing_industries:
        return {
            "exposures": [], "concentration_risk": "行业归属缺失，行业暴露不可计算",
            "max_single": None, "hhi": None, "available": False,
            "missing_industry_codes": missing_industries,
        }

    total_value = 0
    industry_values: dict[str, float] = defaultdict(float)

    for p in positions:
        val = _position_value(p)
        if val <= 0:
            continue
        total_value += val
        ind = industries[str(p.get("code") or "")]
        industry_values[ind] += val

    exposures = []
    for ind, val in sorted(industry_values.items(), key=lambda x: -x[1]):
        pct = round(val / total_value * 100, 1) if total_value > 0 else 0
        risk_level = "high" if pct > 25 else "medium" if pct > 15 else "low"
        exposures.append({
            "industry": ind, "value": round(val, 0),
            "pct": pct, "risk": risk_level,
        })

    # HHI 集中度指数
    hhi = sum((e["pct"] / 100) ** 2 for e in exposures) * 10000
    if hhi > 2500:
        concentration = "⚠️ 高度集中"
    elif hhi > 1500:
        concentration = "中等集中"
    else:
        concentration = "分散良好"

    return {
        "exposures": exposures,
        "concentration_risk": concentration,
        "max_single": max((e["pct"] for e in exposures), default=0),
        "hhi": round(hhi, 0),
        "industry_count": len(exposures),
        "available": True,
    }


def calculate_drawdown(
    positions: list[dict], realized_pnl: float | None = 0,
    nav_curve: list[dict] | None = None, *,
    current_account_equity: float | None = None,
    cashflow_reconciliation: dict | None = None,
) -> dict[str, Any]:
    """Calculate drawdown only from a reconstructed manual-ledger NAV curve."""
    missing_quotes = [p.get("code") for p in positions if _current_price(p) is None]
    valuation_available = not missing_quotes
    marked_position_value = (
        sum(_position_value(p) for p in positions)
        if valuation_available else None
    )
    total_value = (
        float(current_account_equity)
        if current_account_equity is not None
        and math.isfinite(float(current_account_equity))
        and float(current_account_equity) > 0
        else marked_position_value
    )
    total_cost = sum(float(p.get("entry_price") or 0) * int(p.get("shares") or 0) for p in positions)

    unrealized_pnl = (
        marked_position_value - total_cost
        if marked_position_value is not None else None
    )
    unrealized_pnl_pct = (
        round(unrealized_pnl / total_cost * 100, 2)
        if unrealized_pnl is not None and total_cost > 0 else None
    )

    values = [float(row["value"]) for row in (nav_curve or []) if row.get("value") is not None]
    raw_values = [
        float(row.get("raw_equity_yuan", row["value"]))
        for row in (nav_curve or []) if row.get("value") is not None
    ]
    net_external_flow = sum(
        float(row.get("net_external_flow_yuan") or 0)
        for row in (nav_curve or [])
    )
    stored_daily_observations = sum(
        row.get("source") == "daily_account_nav_snapshot"
        for row in (nav_curve or [])
    )
    explicit_baseline = bool(
        available_baseline := next((
            float(row["value"])
            for row in (nav_curve or [])
            if row.get("source") == "user_supplied_equity_baseline"
            and row.get("value") is not None
        ), None)
    )
    peak = max(values) if len(values) >= 2 else None
    current_drawdown = (values[-1] / peak - 1) * 100 if peak else None
    running_peak = 0.0
    max_drawdown = 0.0
    for value in values:
        running_peak = max(running_peak, value)
        if running_peak:
            max_drawdown = min(max_drawdown, value / running_peak - 1)
    available = len(values) >= 2
    return {
        "current_drawdown_pct": round(current_drawdown, 2) if available else None,
        "max_drawdown_pct": round(max_drawdown * 100, 2) if available else None,
        "status": (
            "显式账户基准至当前估值的观测回撤可用"
            if available and any(
                row.get("source") == "user_supplied_equity_baseline"
                for row in (nav_curve or [])
            )
            else "净值曲线可用" if available
            else "手工账本净值历史不足2个交易日，回撤暂不可计算"
        ),
        "peak_value": round(peak, 2) if available else None,
        "current_value": round(total_value, 2) if total_value is not None else None,
        "unrealized_pnl": round(unrealized_pnl, 2) if unrealized_pnl is not None else None,
        "unrealized_pnl_pct": unrealized_pnl_pct,
        "realized_pnl": round(realized_pnl, 2) if realized_pnl is not None else None,
        "total_pnl": (
            round(raw_values[-1] - raw_values[0] - net_external_flow, 2)
            if explicit_baseline and values else
            round(realized_pnl + unrealized_pnl, 2)
            if unrealized_pnl is not None and realized_pnl is not None else None
        ),
        "total_pnl_scope": (
            "since_explicit_account_equity_baseline"
            if explicit_baseline else "all_recorded_ledger_fills"
        ),
        "account_change_since_baseline_pct": (
            round((raw_values[-1] / raw_values[0] - 1) * 100, 4)
            if explicit_baseline and raw_values and raw_values[0] > 0 else None
        ),
        "cashflow_adjusted_return_pct": (
            round((values[-1] / values[0] - 1) * 100, 4)
            if explicit_baseline and len(values) >= 2 and values[0] > 0 else None
        ),
        "net_external_flow_yuan": round(net_external_flow, 2),
        "cashflow_adjustment_status": (
            "verified_complete_through_confirmation"
            if explicit_baseline
            and (cashflow_reconciliation or {}).get("verified_complete") is True
            else "recorded_events_only_unverified_completeness"
            if explicit_baseline else "not_applicable_without_explicit_baseline"
        ),
        "cashflow_reconciliation": cashflow_reconciliation,
        "ledger_realized_pnl_all_time": round(realized_pnl, 2) if realized_pnl is not None else None,
        "available": available,
        "valuation_available": valuation_available,
        "missing_quote_codes": missing_quotes,
        "observations": len(values),
        "nav_curve": (nav_curve or [])[-90:],
        "method": (
            "observed_account_nav_modified_dietz_recorded_cashflows_v1"
            if explicit_baseline else "manual_ledger_reconstructed_eod_nav"
        ),
        "coverage_status": (
            "explicit_baseline_to_current_observed_snapshots"
            if available and any(
                row.get("source") == "user_supplied_equity_baseline"
                for row in (nav_curve or [])
            ) else "ledger_reconstructed_daily"
        ),
        "intraperiod_path_complete": False if available and any(
            row.get("source") == "user_supplied_equity_baseline"
            for row in (nav_curve or [])
        ) else available,
        "stored_daily_nav_observations": stored_daily_observations,
        "daily_close_path_ready": stored_daily_observations >= 2,
        "risk_use": "reduce_only_never_expand_position",
    }


def _ma(values: list[float], period: int) -> float | None:
    if not values or len(values) < period:
        return None
    return sum(values[-period:]) / period


def _quant_sell_signals(bars: list[dict]) -> list[dict[str, Any]]:
    """量化技术观察（全部基于真实日K，只加注不编造）：
    趋势破位 / 动量转弱 / 支撑破位 / 超买回落 / 新高回落。
    不构成卖出指令，也不单独触发"离场复核/减仓复核"提醒。
    """
    signals: list[dict[str, Any]] = []
    closes = [float(b["close"]) for b in bars]
    if len(closes) < 30:
        return signals
    ma5 = _ma(closes, 5)
    ma20 = _ma(closes, 20)
    ma20_prev = _ma(closes[:-1], 20)
    close = closes[-1]

    # 1) 趋势破位：收盘跌破 MA20 且短均线下穿（MA5<MA20），MA20 走平/向下
    if ma5 is not None and ma20 is not None and ma20_prev is not None:
        if close < ma20 and ma5 < ma20 and ma20 <= ma20_prev * 1.001:
            signals.append({
                "signal": "trend_break", "label": "趋势破位",
                "detail": f"收盘跌破MA20({ma20:.2f})且MA5({ma5:.2f})<MA20，短线结构转弱",
                "severity": "high",
            })
    # 2) 动量转弱：MACD 近5根内死叉（DIF 下穿 DEA）
    difs: list[float | None] = []
    e12 = e26 = None
    k12, k26 = 2 / 13, 2 / 27
    for c in closes:
        e12 = c if e12 is None else c * k12 + e12 * (1 - k12)
        e26 = c if e26 is None else c * k26 + e26 * (1 - k26)
        difs.append(e12 - e26 if e12 is not None and e26 is not None else None)
    dea: list[float | None] = []
    e9 = None
    k9 = 2 / 10
    for d in difs:
        if d is None:
            dea.append(None)
            continue
        e9 = d if e9 is None else d * k9 + e9 * (1 - k9)
        dea.append(e9)
    for i in range(max(1, len(difs) - 5), len(difs)):
        if difs[i] is not None and difs[i - 1] is not None and dea[i] is not None and dea[i - 1] is not None:
            if difs[i - 1] >= dea[i - 1] and difs[i] < dea[i]:
                signals.append({
                    "signal": "macd_dead_cross", "label": "动量转弱",
                    "detail": "MACD近5根内死叉，短线动能转弱",
                    "severity": "medium",
                })
                break
    # 3) 支撑破位：跌破最近20日最低收盘（无更高分数优先时作为离场理由）
    if len(closes) >= 21:
        support20 = min(closes[-21:-1])
        if close < support20:
            signals.append({
                "signal": "support_break", "label": "支撑破位",
                "detail": f"收盘跌破近20日最低收盘 ¥{support20:.2f}",
                "severity": "high",
            })
    # 4) 超买回落：RSI(14) 前值>70 回落跌破 60（涨多后动能衰减）
    rsi_vals: list[float | None] = [None] * len(closes)
    if len(closes) > 15:
        gains = losses = 0.0
        for i in range(1, 15):
            diff = closes[i] - closes[i - 1]
            gains += max(diff, 0)
            losses += max(-diff, 0)
        ag, al = gains / 14, losses / 14
        rsi_vals[14] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
        for i in range(15, len(closes)):
            diff = closes[i] - closes[i - 1]
            ag = (ag * 13 + max(diff, 0)) / 14
            al = (al * 13 + max(-diff, 0)) / 14
            rsi_vals[i] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
        for i in range(len(closes) - 5, len(closes)):
            prev_r = rsi_vals[i - 1]
            cur_r = rsi_vals[i]
            if prev_r is not None and cur_r is not None and prev_r > 70 and cur_r < 60:
                signals.append({
                    "signal": "overbought_rollover", "label": "超买回落",
                    "detail": "RSI由超买区(>70)回落跌破60，涨多后动能衰减",
                    "severity": "medium",
                })
                break
    # 5) 高位滞涨：5日内曾创120日新高但最新收盘回落 >1×ATR
    if len(bars) >= 5 and len(closes) >= 26:
        trs: list[float] = []
        prev_c = None
        for b in bars:
            h, l, c = float(b["high"]), float(b["low"]), float(b["close"])
            trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)) if prev_c else h - l)
            prev_c = c
        atr = sum(trs[-14:]) / 14
        high120 = max(float(b["high"]) for b in bars)
        recent5_high = max(float(b["high"]) for b in bars[-5:])
        if recent5_high >= high120 * 0.999 and close < high120 - atr:
            signals.append({
                "signal": "high_wave_stall", "label": "高位滞涨",
                "detail": "近期曾创120日新高但已从高点回落超过1×ATR，冲高乏力",
                "severity": "medium",
            })
    return signals


def _absolute_price_history(rows):
    """Keep only the current, continuous raw-price basis for absolute lines."""
    result = []
    for row in rows:
        if row.get("price_basis") != "audited_raw" or row.get("tradable") is False:
            result = []
            continue
        if row.get("price_basis_break"):
            result = []
        result.append(row)
    return result


def position_risk_actions(
    positions: list[dict], *, market_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """量化卖出风控：止损/移动止盈 + 趋势·动量·支撑·超买回落·高位滞涨。
    现价与风险线用实时行情判定，量化结构信号用真实日K判定（只加注不编造）。"""
    regime = market_state if isinstance(market_state, dict) else market_regime()
    actions = []
    for position in positions:
        entry = float(position.get("entry_price") or 0)
        current = _current_price(position)
        code = position["code"]
        history = _daily_bars(code, 160)
        bars = _absolute_price_history(history)
        history_evidence = (_DAILY_HISTORY_EVIDENCE.get() or {}).get(code, {})
        entry_day = str(position.get("entry_date") or "")[:10]
        entry_day_valid = False
        if entry_day:
            try:
                entry_day_valid = datetime.fromisoformat(entry_day).date().isoformat() == entry_day
            except ValueError:
                entry_day_valid = False
        # A loss-control line must be knowable when the position is opened or
        # imported.  Never let a later volatile bar push the line downward.
        risk_bars = [
            bar for bar in bars if entry_day_valid and str(bar["date"]) < entry_day
        ]
        true_ranges = []
        previous = None
        for bar in risk_bars:
            high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
            true_ranges.append(max(high - low, abs(high - previous), abs(low - previous)) if previous else high - low)
            previous = close
        atr = sum(true_ranges[-14:]) / 14 if len(true_ranges) >= 14 else None
        atr_as_of = str(risk_bars[-1]["date"]) if atr is not None else None
        percentage_floor = entry * 0.92 if entry else None
        volatility_floor = entry - 2 * atr if entry and atr else None
        hard_stop_candidates = [
            value for value in (percentage_floor, volatility_floor)
            if value is not None
        ]
        hard_stop = max(hard_stop_candidates) if hard_stop_candidates else None
        high_since = max(
            [current or entry] + [
                float(bar["high"]) for bar in bars
                if entry_day_valid and str(bar["date"]) >= entry_day
            ]
        )
        trailing_active = bool(entry and high_since >= entry * 1.15)
        trailing_stop = max(entry, high_since - 2 * atr) if trailing_active and atr else None
        effective_stop = max(x for x in (hard_stop, trailing_stop) if x is not None) if (hard_stop or trailing_stop) else None
        # Use the exact rows already checked. A date-only cache can retain
        # signals after a same-date file repair or an audit revocation.
        quant = _quant_sell_signals(bars)
        high_sev = next((s for s in quant if s["severity"] == "high"), None)
        med_sev = next((s for s in quant if s["severity"] == "medium"), None)

        if current is None:
            action, reason = "风险未知", "当前行情缺失，未用成本价冒充现价，暂不判定止损或持有"
        elif effective_stop is not None and current <= effective_stop:
            action, reason = "离场复核", f"现价已触发风险线 ¥{effective_stop:.2f}"
        elif high_sev:
            action, reason = "技术观察", f"高优先技术观察：{high_sev['label']}（{high_sev['detail']}）"
        elif med_sev:
            action, reason = "技术观察", f"技术观察：{med_sev['label']}（{med_sev['detail']}）"
        elif regime["risk_off"]:
            action, reason = "市场风险观察", regime["reason"]
        elif not bars:
            action, reason = "历史风险待核验", "审计历史或一致价格基准不可用；仅保留账户成本比例参考，不能确认持有安全"
        else:
            action, reason = "持有", "未触发止损/趋势破位/动量转弱，且大盘择时允许持仓"
        risk_levels = []
        if hard_stop is not None:
            risk_levels.append({
                "kind": "hard_stop",
                "value": round(hard_stop, 2),
                "source": "entry_price_and_pre_entry_completed_daily_atr14" if atr is not None else "entry_price_percentage_reference_only",
                "formula": "max(entry_price*0.92, entry_price-2*ATR14)" if atr is not None else "entry_price*0.92",
                "data_as_of": atr_as_of,
                "calibration_status": "rule_based_unvalidated",
                "priority": 2 if trailing_stop is not None else 1,
                "active": trailing_stop is None,
                "executable": False,
                "semantics": "冻结损失复核线；建仓后不向下放宽；未校准，非最佳卖点",
            })
        if trailing_stop is not None:
            risk_levels.append({
                "kind": "trailing_stop",
                "value": round(trailing_stop, 2),
                "source": "entry_high_water_and_audited_daily_atr14",
                "priority": 1,
                "active": True,
                "executable": False,
                "semantics": "浮盈启动后的移动风险线；只供人工复核",
            })
        risk_levels.sort(key=lambda item: item["priority"])
        actions.append({
            "position_id": position.get("id"),
            "code": code, "name": position.get("name") or code,
            "action": action, "reason": reason, "current_price": current,
            "entry_price": entry, "atr14": round(atr, 3) if atr is not None else None,
            "atr14_as_of": atr_as_of,
            "hard_stop": round(hard_stop, 2) if hard_stop is not None else None,
            "trailing_active": trailing_active,
            "trailing_stop": round(trailing_stop, 2) if trailing_stop is not None else None,
            "effective_stop": round(effective_stop, 2) if effective_stop is not None else None,
            "risk_levels": risk_levels,
            "daily_history_evidence": history_evidence,
            "absolute_price_history_observations": len(bars),
            "effective_risk_level": next(
                (item for item in risk_levels if item["active"]), None,
            ),
            "quant_signals": quant,
            "market_regime": regime,
        })
    return actions


def get_risk_report(
    positions: list[dict], realized_pnl: float | None = 0,
    ledger: list[dict] | None = None, initial_capital: float = 0, *,
    initial_equity_as_of: str | None = None,
    current_account_equity: float | None = None,
    current_account_equity_as_of: str | None = None,
    account_nav_snapshots: list[dict] | None = None,
    account_cashflows: list[dict] | None = None,
    cashflow_reconciliation: dict | None = None,
) -> dict[str, Any]:
    """生成完整风控报告."""
    var_report = calculate_portfolio_var(positions)
    beta_report = calculate_beta_exposure(positions)
    industry_report = calculate_industry_exposure(positions)
    dependency_report = calculate_portfolio_dependency(positions)
    stress_report = calculate_historical_stress(positions)
    fee_error = any(row.get("fee_correction_error") for row in ledger or [])
    nav_curve = [] if fee_error else build_manual_nav_curve(
        ledger or [], initial_capital,
        initial_equity_as_of=initial_equity_as_of,
        current_account_equity=current_account_equity,
        current_account_equity_as_of=current_account_equity_as_of,
        account_nav_snapshots=account_nav_snapshots,
        account_cashflows=account_cashflows,
    )
    drawdown_report = calculate_drawdown(
        positions, realized_pnl, nav_curve,
        current_account_equity=current_account_equity,
        cashflow_reconciliation=cashflow_reconciliation,
    )
    if fee_error:
        drawdown_report.update(status="原始成交与费用更正不一致，不能重建可信收益与回撤", available=False)

    account_equity = (
        float(current_account_equity)
        if current_account_equity is not None
        and math.isfinite(float(current_account_equity))
        and float(current_account_equity) > 0 else None
    )
    invested_value = sum(
        max(0.0, _position_value(position)) for position in positions
    )
    account_exposure_pct = (
        invested_value / account_equity * 100
        if account_equity is not None else None
    )
    if account_equity is not None:
        var_report["var_95_account_pct"] = round(
            float(var_report.get("var_95") or 0) / account_equity * 100, 4,
        ) if var_report.get("available") is True else None
        var_report["var_99_account_pct"] = round(
            float(var_report.get("var_99") or 0) / account_equity * 100, 4,
        ) if var_report.get("available") is True else None
        raw_daily_vol = var_report.get("portfolio_daily_vol_pct")
        var_report["portfolio_daily_vol_account_pct"] = (
            round(
                float(raw_daily_vol) * (account_exposure_pct or 0) / 100,
                4,
            )
            if var_report.get("available") is True
            and raw_daily_vol is not None else None
        )
        var_report["account_equity_yuan"] = round(account_equity, 2)
        var_report["percentage_basis"] = (
            "position_book_and_total_account_equity_both_reported"
        )
        raw_beta = beta_report.get("beta")
        beta_report["account_beta"] = round(
            float(raw_beta) * (account_exposure_pct or 0) / 100, 4,
        ) if beta_report.get("available") is True and raw_beta is not None else None
        beta_report["account_exposure_pct"] = round(
            account_exposure_pct or 0, 4,
        )
        for row in industry_report.get("exposures") or []:
            row["account_exposure_pct"] = round(
                float(row.get("value") or 0) / account_equity * 100, 4,
            )
        industry_report["max_account_exposure_pct"] = max(
            (
                float(row.get("account_exposure_pct") or 0)
                for row in industry_report.get("exposures") or []
            ),
            default=0.0,
        )
        dependency_report["dominant_account_exposure_pct"] = round(
            (account_exposure_pct or 0)
            * float(dependency_report.get("largest_risk_contribution_pct") or 0)
            / 100,
            4,
        ) if dependency_report.get("available") is True else None
        for key in ("worst_1d_pct", "worst_5d_pct", "expected_shortfall_95_pct"):
            value = stress_report.get(key)
            stress_report[key.replace("_pct", "_account_pct")] = (
                round(float(value) * (account_exposure_pct or 0) / 100, 4)
                if stress_report.get("available") is True and value is not None
                else None
            )
        stress_report["account_exposure_pct"] = round(
            account_exposure_pct or 0, 4,
        )

    # 综合风险评分 (0-100, 越低越安全)
    risk_score = 0
    alerts = []

    # VaR 检查
    if var_report.get("available") is False:
        alerts.append({"level": "info", "msg": var_report["reason"]})
    else:
        var_for_score = (
            var_report.get("var_95_account_pct")
            if account_equity is not None else var_report.get("var_95_pct")
        )
        var_for_score = float(var_for_score or 0)
        if var_for_score > 5:
            risk_score += 30
            alerts.append({
                "level": "danger",
                "msg": f"账户日VaR(95%) {var_for_score:.2f}%，超过5%警戒线",
            })
        elif var_for_score > 3:
            risk_score += 15
            alerts.append({
                "level": "warning",
                "msg": f"账户日VaR(95%) {var_for_score:.2f}%，需要注意",
            })
        elif account_equity is not None and float(var_report.get("var_95_pct") or 0) > 5:
            alerts.append({
                "level": "info",
                "msg": (
                    f"持仓内日VaR(95%) {float(var_report['var_95_pct']):.2f}%，"
                    f"折合账户权益 {var_for_score:.2f}%"
                ),
            })

    # Beta 检查
    if beta_report.get("available") is False:
        alerts.append({"level": "info", "msg": beta_report["exposure"]})
    elif (
        beta_report.get("account_beta")
        if account_equity is not None else beta_report.get("beta")
    ) is not None and float(
        beta_report.get("account_beta")
        if account_equity is not None else beta_report.get("beta")
    ) > 1.3:
        risk_score += 20
        alerts.append({
            "level": "warning",
            "msg": (
                f"账户Beta {float(beta_report.get('account_beta') or beta_report['beta']):.3f}，"
                "市场敏感度高"
            ),
        })

    # 行业集中度
    if industry_report.get("available") is False:
        alerts.append({"level": "info", "msg": industry_report["concentration_risk"]})
    else:
        industry_for_score = float(
            industry_report.get("max_account_exposure_pct")
            if account_equity is not None else industry_report.get("max_single")
            or 0
        )
        if industry_for_score > 25:
            risk_score += 25
            alerts.append({
                "level": "danger",
                "msg": f"单行业账户暴露 {industry_for_score:.2f}%，集中度风险高",
            })
        elif industry_for_score > 15:
            risk_score += 10
            alerts.append({
                "level": "info",
                "msg": f"单行业账户暴露 {industry_for_score:.2f}%，适度集中",
            })

    # 回撤检查
    if drawdown_report.get("available") is False:
        alerts.append({"level": "info", "msg": drawdown_report["status"]})
    elif drawdown_report["current_drawdown_pct"] <= -10:
        risk_score += 25
        alerts.append({"level": "danger", "msg": f"当前回撤 {drawdown_report['current_drawdown_pct']}%"})
    elif drawdown_report["current_drawdown_pct"] <= -5:
        risk_score += 10
        alerts.append({"level": "warning", "msg": f"当前回撤 {drawdown_report['current_drawdown_pct']}%"})

    # 集中风险按协方差边际风险贡献判断，不再把“股票只数”当成分散度。
    # 两只高度负相关的持仓可能比五只同涨同跌的持仓更分散；反之亦然。
    if dependency_report.get("available") is True and positions:
        largest_contribution = dependency_report.get(
            "largest_risk_contribution_pct"
        )
        dominant_for_score = (
            dependency_report.get("dominant_account_exposure_pct")
            if account_equity is not None else largest_contribution
        )
        danger_threshold = 25 if account_equity is not None else 70
        warning_threshold = 15 if account_equity is not None else 50
        if (
            dominant_for_score is not None
            and dominant_for_score >= danger_threshold
        ):
            risk_score += 20
            alerts.append({
                "level": "danger",
                "msg": (
                    f"最大风险来源折合账户暴露 {dominant_for_score:.1f}%，"
                    if account_equity is not None else
                    f"最大单一风险贡献 {dominant_for_score:.1f}%，"
                ) + (
                    "组合风险由少数暴露主导"
                ),
            })
        elif (
            dominant_for_score is not None
            and dominant_for_score >= warning_threshold
        ):
            risk_score += 10
            alerts.append({
                "level": "warning",
                "msg": (
                    f"最大风险来源折合账户暴露 {dominant_for_score:.1f}%，"
                    if account_equity is not None else
                    f"最大单一风险贡献 {dominant_for_score:.1f}%，"
                ) + "集中风险偏高",
            })
        elif (
            account_equity is not None and largest_contribution is not None
            and largest_contribution >= 70
        ):
            alerts.append({
                "level": "info",
                "msg": (
                    f"持仓风险贡献集中于单一标的 {largest_contribution:.1f}%，"
                    f"但仅折合账户暴露 {float(dominant_for_score or 0):.2f}%"
                ),
            })

    missing_metrics = []
    if positions:
        if var_report.get("available") is False:
            missing_metrics.append("var")
        if beta_report.get("available") is False:
            missing_metrics.append("beta")
        if industry_report.get("available") is False:
            missing_metrics.append("industry_exposure")
        if dependency_report.get("available") is False:
            missing_metrics.append("portfolio_dependency")
        if stress_report.get("available") is False:
            missing_metrics.append("historical_stress")
        if drawdown_report.get("available") is False:
            missing_metrics.append("drawdown")
        if drawdown_report.get("valuation_available") is False:
            missing_metrics.append("current_valuation")

    observed_risk_score = risk_score
    if not positions:
        assessment_status = "not_applicable"
        risk_score = None
        risk_level = "无持仓"
    elif missing_metrics:
        assessment_status = "insufficient"
        risk_score = None
        risk_level = "⚪ 风险未知"
    elif risk_score < 20:
        assessment_status = "complete"
        risk_level = "🟢 低风险"
    elif risk_score < 50:
        assessment_status = "complete"
        risk_level = "🟡 中等风险"
    elif risk_score < 75:
        assessment_status = "complete"
        risk_level = "🟠 高风险"
    else:
        assessment_status = "complete"
        risk_level = "🔴 极高风险"

    return {
        "var": var_report,
        "beta": beta_report,
        "industry": industry_report,
        "dependency": dependency_report,
        "historical_stress": stress_report,
        "drawdown": drawdown_report,
        "risk_score": risk_score,
        "risk_alert_index": risk_score,
        "observed_risk_score": observed_risk_score,
        "risk_level": risk_level,
        "risk_assessment_status": assessment_status,
        "missing_metrics": missing_metrics,
        "alerts": alerts,
        "position_actions": position_risk_actions(positions),
        "calculated_at": datetime.now().isoformat(),
        "completed_daily_data_as_of": (
            completed_day.isoformat()
            if (completed_day := last_completed_trading_day()) is not None
            else None
        ),
        "methodology": (
            "历史模拟VaR（真实股数）· 样本协方差风险贡献 · 候选对现有组合相关性 "
            "· 同期历史尾部损失 · 行业HHI；任何持仓缺失时明确停算"
        ),
        "risk_score_direction": "仅在关键指标完整时越低越安全；缺失时风险等级为 unknown",
        "risk_alert_index_semantics": (
            "规则化风险警报指数，不是亏损概率、收益预测或最优仓位证明；"
            "已录入真实账户权益时按账户风险金额归一化；集中度使用边际风险贡献，"
            "不按股票只数计分"
        ),
    }
