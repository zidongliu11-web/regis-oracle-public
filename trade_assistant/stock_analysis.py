from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

from .candles import Candle, research_signals, technical_snapshot, technical_values

_QUANT_SNAPSHOT_PATH = (
    Path(__file__).resolve().parent.parent / "research" / "quant_stock_snapshot.json"
)
_KRONOS_SNAPSHOT_PATH = (
    Path(__file__).resolve().parent.parent / "research" / "kronos_stock_snapshot.json"
)
_QUANT_CACHE: dict = {
    "lock": threading.Lock(),
    "expires_at": 0.0,
    "data": None,
}
_KRONOS_CACHE: dict = {
    "lock": threading.Lock(),
    "expires_at": 0.0,
    "data": None,
}


def _load_quant_snapshot() -> dict:
    """读取全市场量化快照（64KB 小文件，带 60s TTL 缓存）。

    快照由 research/build_quant_snapshot.py 预计算；缺失或过期时返回空 dict。
    """
    now = time.monotonic()
    with _QUANT_CACHE["lock"]:
        if _QUANT_CACHE["data"] is not None and now < _QUANT_CACHE["expires_at"]:
            return _QUANT_CACHE["data"]
        data: dict = {}
        try:
            if _QUANT_SNAPSHOT_PATH.exists():
                data = json.loads(_QUANT_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        _QUANT_CACHE["data"] = data
        _QUANT_CACHE["expires_at"] = now + 60.0
        return data


def _load_kronos_snapshot() -> dict:
    """读取 Kronos 预测快照（小 JSON，60s TTL 缓存）。

    快照由 research/kronos_snapshot.py 预计算（Kronos-small 批量外推）。
    """
    now = time.monotonic()
    with _KRONOS_CACHE["lock"]:
        if _KRONOS_CACHE["data"] is not None and now < _KRONOS_CACHE["expires_at"]:
            return _KRONOS_CACHE["data"]
        data: dict = {}
        try:
            if _KRONOS_SNAPSHOT_PATH.exists():
                data = json.loads(_KRONOS_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        _KRONOS_CACHE["data"] = data
        _KRONOS_CACHE["expires_at"] = now + 60.0
        return data


_FUNDFLOW_PATH = (
    Path(__file__).resolve().parent.parent / "research" / "fund_flow_live.json"
)
_FUNDFLOW_FETCHER = (
    Path(__file__).resolve().parent.parent / "research" / "ths_fund_flow.py"
)
_FUNDFLOW_CACHE: dict = {
    "lock": threading.Lock(),
    "expires_at": 0.0,
    "data": None,
    "fetching": False,
}


def _refresh_fundflow_bg() -> None:
    """后台线程：用子进程抓 THS 实时主力资金流并刷新 fund_flow_live.json."""
    try:
        import subprocess
        subprocess.run(
            [sys.executable, str(_FUNDFLOW_FETCHER), "--out", str(_FUNDFLOW_PATH)],
            timeout=90, capture_output=True,
        )
    except Exception:
        pass
    with _FUNDFLOW_CACHE["lock"]:
        _FUNDFLOW_CACHE["fetching"] = False
        _FUNDFLOW_CACHE["expires_at"] = time.monotonic() + 900.0  # 15 分钟
        try:
            if _FUNDFLOW_PATH.exists():
                _FUNDFLOW_CACHE["data"] = json.loads(
                    _FUNDFLOW_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass


def _load_fundflow() -> dict:
    """THS 实时主力资金流（15 分钟 TTL）。文件缺失/过旧时后台重抓，不阻塞请求。"""
    now = time.monotonic()
    with _FUNDFLOW_CACHE["lock"]:
        fresh = _FUNDFLOW_CACHE["data"] is not None and now < _FUNDFLOW_CACHE["expires_at"]
        if not fresh and not _FUNDFLOW_CACHE["fetching"]:
            # 先读现有文件（可能是定时任务刚刷新的）→ 视为 15 分钟内新鲜
            if _FUNDFLOW_PATH.exists():
                try:
                    _FUNDFLOW_CACHE["data"] = json.loads(
                        _FUNDFLOW_PATH.read_text(encoding="utf-8"))
                    _FUNDFLOW_CACHE["expires_at"] = now + 900.0
                except (OSError, ValueError):
                    pass
            # 仍无数据/确实太旧 → 后台子进程重抓
            if _FUNDFLOW_CACHE["data"] is None or now >= _FUNDFLOW_CACHE["expires_at"]:
                _FUNDFLOW_CACHE["fetching"] = True
                threading.Thread(target=_refresh_fundflow_bg, daemon=True).start()
        return dict(_FUNDFLOW_CACHE["data"] or {})


def kronos_stock_view(code: str) -> dict:
    """给定 6 位代码，返回 Kronos 未来K线预测视角。

    与量化快照并列供分析页展示；股票未覆盖时 available=False。
    """
    snap = _load_kronos_snapshot()
    stock = (snap.get("stocks") or {}).get(code)
    if not stock:
        return {
            "available": False,
            "snapshot_date": snap.get("snapshot_date"),
            "message": "该股票不在当前 Kronos 快照宇宙内。",
        }
    return {
        "available": True,
        "snapshot_date": snap.get("snapshot_date"),
        "model": snap.get("model"),
        "pred_len": snap.get("pred_len"),
        "last_close": stock.get("last_close"),
        "pred_close_20d": stock.get("pred_close_20d"),
        "pred_ret_5d": stock.get("pred_ret_5d"),
        "pred_ret_10d": stock.get("pred_ret_10d"),
        "pred_ret_20d": stock.get("pred_ret_20d"),
        "pred_ret_30d": stock.get("pred_ret_30d"),
        "direction": stock.get("direction"),
    }


def quant_stock_view(code: str) -> dict:
    """给定 6 位代码，返回只读的历史量化研究快照。

    ``quant_stock_snapshot.json`` 是旧模型留档，不是全站统一评分、真实账户
    或当前 production 策略。接口在同一边界降级，防止任何消费者把旧 top-k
    标志解释为当前持仓或买卖资格。
    """
    snap = _load_quant_snapshot()
    stock = None
    for item in snap.get("stocks", []):
        if item.get("code") == code:
            stock = item
            break
    if not stock:
        return {
            "available": False,
            "snapshot_date": snap.get("snapshot_date"),
            "data_as_of": snap.get("data_asof"),
            "model_status": "historical_research_snapshot",
            "evidence_tier": "research",
            "score_semantics": "legacy_model_rank_not_unified_stock_score",
            "decision_eligible": False,
            "production_eligible": False,
            "message": "该股票不在当前量化研究宇宙内（约300只样本）。",
            "kronos": kronos_stock_view(code),
        }
    selected = snap.get("selected_factors", [])
    factor_scores = []
    factors = stock.get("factors") or {}
    for entry in selected:
        fname = entry.get("factor")
        if fname in factors and factors[fname] is not None:
            factor_scores.append({
                "factor": fname,
                "weight": round(float(entry.get("weight", 0)), 4),
                "zscore": factors[fname],
            })
    fund_flow = (_load_fundflow().get("stocks") or {}).get(code)
    return {
        "available": True,
        "snapshot_date": snap.get("snapshot_date"),
        "data_as_of": snap.get("data_asof"),
        "model_status": "historical_research_snapshot",
        "evidence_tier": "research",
        "score_semantics": "legacy_model_rank_not_unified_stock_score",
        "decision_eligible": False,
        "production_eligible": False,
        "composite_score": stock.get("composite"),
        "rank": stock.get("rank"),
        "rank_total": snap.get("universe"),
        "legacy_in_portfolio": bool(stock.get("in_portfolio")),
        "in_portfolio": False,
        "fund_flow": fund_flow,
        "factor_scores": factor_scores,
        "strategy": {
            "status": "historical_research_only",
            "decision_eligible": False,
            "production_eligible": False,
            "method": "decay k20 · 波动率目标25% · 点时宇宙top300",
            "annualized_pct": (snap.get("headline") or {}).get("annualized_pct"),
            "sharpe": (snap.get("headline") or {}).get("sharpe"),
            "max_dd_pct": (snap.get("headline") or {}).get("max_dd_pct"),
            "calmar": (snap.get("headline") or {}).get("calmar"),
            "years_positive": (snap.get("headline") or {}).get("years_positive"),
            "years_total": (snap.get("headline") or {}).get("years_total"),
            "portfolio_ic": snap.get("portfolio_ic"),
        },
        "kronos": kronos_stock_view(code),
    }


def build_stock_analysis(
    *,
    code: str,
    name: str,
    candles: list[Candle],
    quote: dict | None,
    interval: str,
    market_regime: dict | None,
    research_gate_passed: bool,
) -> dict:
    """Build an explainable, research-only intraday decision map.

    The levels are deterministic scenarios derived from the latest completed
    candles. They are deliberately not promoted to executable signals while
    the historical research gate is closed.
    """

    if len(candles) < 35:
        raise ValueError("K线历史不足，无法生成个股分析")
    values = technical_values(candles)
    latest = candles[-1]
    recent = candles[-20:]
    atr = max(float(values["atr14"]), latest.close * 0.002)
    recent_high = max(item.high for item in recent[:-1])
    recent_low = min(item.low for item in recent[:-1])
    breakout = max(recent_high, latest.close + 0.20 * atr)
    buy_zone_high = breakout + 0.35 * atr
    protective_stop = max(recent_low, breakout - 1.50 * atr)
    first_target = breakout + 2.00 * atr

    reasons: list[str] = []
    cautions: list[str] = []
    score = 0
    if values["ema5"] > values["ema20"]:
        score += 1
        reasons.append("EMA5位于EMA20上方")
    else:
        score -= 1
        cautions.append("EMA5未站上EMA20")
    if values["macd_histogram"] > 0:
        score += 1
        reasons.append("MACD柱值为正")
    else:
        score -= 1
        cautions.append("MACD动量为负")
    if 45 <= values["rsi14"] <= 68:
        score += 1
        reasons.append("RSI处于偏强但未极端区间")
    elif values["rsi14"] >= 72:
        score -= 1
        cautions.append("RSI处于高位，追价风险较高")
    elif values["rsi14"] <= 30:
        cautions.append("RSI偏低，但超卖不等于反转")
    intraday_volume_ratio = values["intraday_volume_ratio5"]
    volume_confirmation = (
        intraday_volume_ratio
        if intraday_volume_ratio is not None else values["volume_ratio20"]
    )
    volume_confirmation_label = (
        "盘中5日量比"
        if intraday_volume_ratio is not None else "最新K线相对前20根成交量"
    )
    if volume_confirmation is not None and volume_confirmation >= 1.2:
        score += 1
        reasons.append(f"{volume_confirmation_label}达到1.20")
    else:
        cautions.append(f"{volume_confirmation_label}未达到1.20")

    if score >= 3:
        state = "偏强，等待突破确认"
        state_code = "prepare"
    elif score <= -2:
        state = "偏弱，优先回避"
        state_code = "avoid"
    else:
        state = "震荡观察，条件不足"
        state_code = "wait"

    quote_freshness = (quote or {}).get("freshness") or "unknown"
    data_live = quote_freshness == "live"
    execution_allowed = False
    action = "仅研究观察，禁止买入"
    gate_reason = "本系统没有真钱授权能力；所有价位仅是不可执行的研究场景"
    if not data_live:
        gate_reason += "；当前不是可确认的实时交易行情"

    latest_signals = research_signals(candles)
    return {
        "code": code,
        "name": name,
        "interval": interval,
        "information_time": latest.time,
        "quote": quote,
        "quant": quant_stock_view(code),
        "market_regime": market_regime or {
            "status": "unavailable",
            "label": "市场环境待数据",
            "message": "指数数据不足",
        },
        "state": {
            "code": state_code,
            "label": state,
            "score": score,
            "score_range": [-4, 4],
            "reasons": reasons,
            "cautions": cautions,
        },
        "decision": {
            "action": action,
            "execution_allowed": execution_allowed,
            "research_gate_passed": bool(research_gate_passed),
            "gate_reason": gate_reason,
            "buy_scenario": {
                "label": "突破后候选买入区间",
                "range": [
                    round(breakout, 3),
                    round(buy_zone_high, 3),
                ],
                "trigger": (
                    f"{interval}收盘价站上 {breakout:.3f}，"
                    f"且{volume_confirmation_label}达到1.20以上；下一根K线仍需确认"
                ),
                "status": "unverified_research_scenario",
            },
            "sell_scenario": {
                "protective_stop": round(protective_stop, 3),
                "first_target": round(first_target, 3),
                "conditions": [
                    f"{interval}收盘价跌破 {protective_stop:.3f}",
                    f"EMA5下穿EMA20（当前EMA20为 {values['ema20']:.3f}）",
                    "触及第一目标后只提示复核，不自动卖出",
                ],
                "status": "unverified_research_scenario",
            },
        },
        "technical": {
            "values": {
                key: round(float(value), 6) if value is not None else None
                for key, value in values.items()
            },
            "snapshot": technical_snapshot(candles),
        },
        "candles": [asdict(item) for item in candles],
        "signals": latest_signals,
        "safety": {
            "auto_trading": False,
            "manual_confirmation": True,
            "optimal_point_claim": False,
            "message": (
                "研究访问保持可用，但不产生买卖授权；系统不保证收益，也不自动下单。"
            ),
        },
    }


def build_ai_stock_prompt(analysis: dict) -> str:
    compact = {
        "code": analysis["code"],
        "name": analysis["name"],
        "interval": analysis["interval"],
        "information_time": analysis["information_time"],
        "quote": analysis["quote"],
        "market_regime": analysis["market_regime"],
        "local_state": analysis["state"],
        "quant_strategy": analysis["quant"],
        "decision_scenarios": analysis["decision"],
        "technical_values": analysis["technical"]["values"],
        "recent_candles": analysis["candles"][-40:],
        "research_signals": analysis["signals"][-8:],
        "safety": analysis["safety"],
    }
    return """请对下面这份A股个股快照做独立、审慎的研究解释。
只能使用JSON里明确给出的数字与时间，不得补造财务数据、新闻、资金流或胜率。
quant_strategy 是旧量化模型的只读历史研究快照，不是全站统一评分、真实持仓或
当前 production 策略：factor_scores 是旧模型因子 zscore，rank 是旧研究宇宙排名，
legacy_in_portfolio 只表示快照生成时进入旧模型 top-20，不能解释为当前持仓；
strategy 是历史回测记录，decision_eligible=false 时不得参与操作判断。
请把这些量化信号与本地技术面结合，解释多空证据、触发条件、失效条件。
只能解释不可执行的研究触发与失效条件，不得给出买入、卖出或仓位操作建议。
必须明确说明本系统没有真钱授权能力，历史结果不构成收益保证。
只返回一个JSON对象，不要Markdown代码块，字段必须是：
summary（不超过80字）、market_context、bull_case（数组）、bear_case（数组）、
buy_trigger_review、sell_trigger_review、risk_notes（数组）、
confidence（low/medium/high）、data_time、gate_conclusion。

本地快照：
""" + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def parse_ai_stock_response(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        return {
            "summary": text,
            "market_context": "模型未返回结构化JSON",
            "bull_case": [],
            "bear_case": [],
            "buy_trigger_review": "以本地确定性条件为准",
            "sell_trigger_review": "以本地确定性条件为准",
            "risk_notes": ["模型输出格式异常，不能提升为交易信号"],
            "confidence": "low",
            "data_time": None,
            "gate_conclusion": "门禁已解锁，本次输出格式异常仅供参考",
        }
    return {
        "summary": str(result.get("summary", ""))[:500],
        "market_context": str(result.get("market_context", ""))[:1000],
        "bull_case": _string_list(result.get("bull_case")),
        "bear_case": _string_list(result.get("bear_case")),
        "buy_trigger_review": str(
            result.get("buy_trigger_review", "")
        )[:1000],
        "sell_trigger_review": str(
            result.get("sell_trigger_review", "")
        )[:1000],
        "risk_notes": _string_list(result.get("risk_notes")),
        "confidence": (
            result.get("confidence")
            if result.get("confidence") in {"low", "medium", "high"}
            else "low"
        ),
        "data_time": result.get("data_time"),
        "gate_conclusion": str(
            result.get("gate_conclusion", "")
        )[:1000],
    }


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:500] for item in value[:8]]
