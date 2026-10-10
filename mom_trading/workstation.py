#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
workstation.py — Step 4 机构级看盘工作台：数据聚合 / 七维画像 / 综合评分 / 一票否决 / 9段式规则化总结

聚合真实数据（行情/K线/公司档案/资金流/技术指标），全部带来源与时点，不可得即"数据不足"标注。
综合评分只排序个股：成长22% + 质量22% + 估值16%(反向) + 技术20% + 风险20%(反向)；
第三方资金流仅作观察证据，盘中条件仅作执行复核，二者不重复进入个股总分；
缺失维度不计入加权（权重归一化）。评级 S/A/B/C/D；一票否决直接降至 D。
9段式总结为**本地规则化生成**（无外部 AI 调用），诚实标注生成方式。

不自动下单、不连接券商；所有输出仅研究展示。
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import certifi
import pandas as pd

from .data_semantics import FLOW_MAGNITUDE_SCALE, normalize_fund_flow_yi
from .trading_calendar import audited_trading_day_age
import requests

_PROJECT = Path(__file__).resolve().parent.parent

# ── 统一专业评分权重（V3：个股 Alpha 排序，不混入市场状态或执行时机） ──
# 缺失维度不计入加权（权重归一化），并诚实披露 confidence / data_gap。
PROFESSIONAL_WEIGHTS = {
    "growth": 0.22,    # 成长（个股 Alpha）
    "quality": 0.22,   # 质量（个股 Alpha）
    "valuation": 0.16, # 估值（个股 Alpha，反向）
    "risk": 0.20,      # 个股风险（反向）
    "technical": 0.20, # 已完成K线技术结构
}
_COMPANY_DIMS = ("growth", "quality", "valuation", "risk")
# 旧执行兼容分档（替代更旧的 S/A/B/C/D）。第一方研究页使用
# ``RESEARCH_SCORE_BANDS``，不把横截面分数直接呈现成买卖建议。
# “卖出”只能由真实持仓风险/策略退出链产生，不能由未持仓个股的横截面评分产生。
PROFESSIONAL_RATING = [
    (80, "强烈买入"), (65, "买入"), (50, "中性"), (35, "回避"), (0, "排除"),
]
RESEARCH_SCORE_BANDS = [
    (80, "高分研究候选"), (65, "较高分研究候选"),
    (50, "中性研究候选"), (35, "低分研究候选"),
    (0, "排除级研究候选"),
]
# 兼容旧调用方（保留别名，避免外部 import 崩）
SCORE_WEIGHTS = PROFESSIONAL_WEIGHTS
RATING_BANDS = PROFESSIONAL_RATING

# 研究规则：商誉超过总资产 20% 触发否决。阈值随结果显式返回，避免隐藏口径。
GOODWILL_TOTAL_ASSET_VETO_PCT = 20.0

_MAJOR_EVENT_VETO_KEYWORDS = (
    "财务造假", "虚假记载", "伪造财务", "重大诉讼", "重大仲裁",
    "立案告知书", "立案调查", "行政处罚决定书", "重大违法", "纪律处分",
    "公开谴责", "监管措施", "警示函", "责令改正", "退市风险警示",
    "强制退市", "终止上市",
)

_PIT_PANEL_PATH = _PROJECT / "research" / "backtest_project" / "data" / "pit_fundamentals.parquet"
_STOCK_META_PATH = _PROJECT / "research" / "backtest_project" / "data" / "stock_meta.parquet"
_ARCHIVED_FUNDAMENTALS_DB = _PROJECT / "research" / "data" / "v12_fundamentals.sqlite3"
_FUND_FLOW_JSON = _PROJECT / "research" / "fund_flow_live.json"
_QUANT_DATA_DB = _PROJECT / "data" / "quant_data" / "quant_data.sqlite3"
_TRADING_CALENDAR_PATH = _PROJECT / "reports" / "v4_market_calendar.json"
_SINA_FUND_FLOW_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "MoneyFlow.ssl_qsfx_lscjfb"
)
_SINA_FUND_FLOW_SOURCE = "新浪财经公开资金流·历史成交分布"
_SINA_FUND_FLOW_METHOD = (
    "Level-1成交方向分类；r0_net=单笔成交额≥100万元的特大单净额，"
    "非账户持仓识别；仅按正负判断"
)
_SINA_FUND_FLOW_TTL = 6 * 60 * 60

# ── 估值面板（PIT PE/PB）惰性加载缓存 ─────────────
_pit_cache: dict | None = None
_meta_cache: dict | None = None
_val_dist_cache: dict | None = None   # PIT 回退分布缓存（市场 + 行业）
_fund_snapshot_cache: list | None = None  # [time.monotonic(), parsed_fund_json]
_valuation_cs_lock = threading.Lock()
_valuation_cs_cache: dict | None = None  # {fetched: monotonic, section: dict | None}
_VALUATION_CS_TTL = 300                  # 实时估值跨截面 TTL（盘中 5 分钟）
_sina_fund_flow_lock = threading.Lock()
_sina_fund_flow_cache: dict[str, tuple[float, list[dict]]] = {}


def _suffix(code: str) -> str:
    """6位代码 → 交易所后缀（沪市 .SH 除 3/4/8/2 开头；深市 .SZ）。"""
    if code[:1] in ("6", "9", "5", "4", "8"):
        return "SH"
    return "SZ"


def _load_pit_latest() -> dict:
    """PIT 估值面板 → {code: {pe_ttm, pb_mrq, as_of}}，每只取最近 trade_date。"""
    global _pit_cache
    if _pit_cache is not None:
        return _pit_cache
    _pit_cache = {}
    try:
        df = pd.read_parquet(_PIT_PANEL_PATH, columns=["ts_code", "trade_date", "pe_ttm", "pb_mrq"])
    except Exception:
        return _pit_cache
    if df.empty:
        return _pit_cache
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values("trade_date").groupby("ts_code").tail(1)
    for _, row in df.iterrows():
        code6 = str(row["ts_code"]).split(".")[0]
        _pit_cache[code6] = {
            "pe_ttm": None if pd.isna(row["pe_ttm"]) else round(float(row["pe_ttm"]), 2),
            "pb_mrq": None if pd.isna(row["pb_mrq"]) else round(float(row["pb_mrq"]), 2),
            "as_of": row["trade_date"].strftime("%Y-%m-%d"),
        }
    return _pit_cache


def _load_stock_meta() -> dict:
    """stock_meta → {code: {industry, first_date, last_date}}。"""
    global _meta_cache
    if _meta_cache is not None:
        return _meta_cache
    _meta_cache = {}
    try:
        df = pd.read_parquet(_STOCK_META_PATH)
    except Exception:
        return _meta_cache
    for _, row in df.iterrows():
        code6 = str(row["ts_code"]).split(".")[0]
        _meta_cache[code6] = {
            "industry": str(row.get("industry") or "").strip(),
            "first_date": row.get("first_date"),
            "last_date": row.get("last_date"),
        }
    return _meta_cache


# ── 工具函数 ─────────────────────────────────────

def _score_from_value(value, lo, hi, invert=False) -> float | None:
    """把数值线性映射到 0-100。lo/hi 为"满分→零分"锚点；invert=True 时反向（越大越低分）。"""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if lo == hi:
        return None
    if invert:
        # 值越小越好：value<=lo → 100，value>=hi → 0
        v = (hi - value) / (hi - lo)
    else:
        v = (value - lo) / (hi - lo)
    return round(max(0.0, min(100.0, v * 100)), 1)


def _pct(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(v, nd=2) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def _archived_financial_metrics(code: str) -> dict:
    """Read the local point-in-time financial archive before asking a live source."""
    market_code = ("sh" if code.startswith(("5", "6", "9")) else "sz") + "." + code
    try:
        with sqlite3.connect(_ARCHIVED_FUNDAMENTALS_DB.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT pub_date, stat_date, net_profit_margin, net_profit,
                          main_business_revenue, cfo_to_net_profit
                   FROM fundamentals WHERE code = ?
                   ORDER BY stat_date DESC, pub_date DESC""",
                (market_code,),
            ).fetchall()
    except sqlite3.Error:
        return {}
    if not rows:
        return {}
    latest = rows[0]
    period = str(latest["stat_date"])
    prior_period = str(int(period[:4]) - 1) + period[4:]
    prior = next((row for row in rows[1:] if str(row["stat_date"]) == prior_period), None)

    def amount_yi(value):
        return round(float(value) / 100_000_000, 2) if value is not None else None

    def yoy(current, previous):
        if current is None or previous in (None, 0):
            return None
        return round((float(current) / float(previous) - 1) * 100, 2)

    margin = _pct(latest["net_profit_margin"])
    return {
        "period": period,
        "data_as_of": latest["pub_date"],
        "revenue": amount_yi(latest["main_business_revenue"]),
        "net_profit": amount_yi(latest["net_profit"]),
        "revenue_yoy": yoy(latest["main_business_revenue"], prior["main_business_revenue"] if prior else None),
        "np_yoy": yoy(latest["net_profit"], prior["net_profit"] if prior else None),
        "net_margin": round(margin * 100, 2) if margin is not None else None,
        "ocf_to_net_profit": _pct(latest["cfo_to_net_profit"]),
        "source": "本机已归档季度基本面（V12，按披露日）",
    }


def _select_financial_record(archived: dict, online: dict,
                             online_source: dict | None = None) -> dict:
    """Choose one PIT financial record; never splice fields across periods.

    Online summaries currently expose a report period but not a disclosure date.
    Their fetch timestamp is therefore labelled ``observed_at`` and must not be
    presented as the report's disclosure date.
    """
    archived = dict(archived or {})
    online = {key: value for key, value in (online or {}).items() if value is not None}
    online_source = online_source or {}
    if online:
        online["source"] = online_source.get("source") or "同花顺财务摘要"
        if online_source.get("fetched_at"):
            online["observed_at"] = online_source["fetched_at"]
    if not archived:
        return online
    if not online:
        return archived
    archived_period = str(archived.get("period") or "")
    online_period = str(online.get("period") or "")
    if online_period and (not archived_period or online_period >= archived_period):
        return online
    return archived


def _load_fund_flow_history(code: str, limit: int = 20) -> list[dict]:
    """Return one latest audited snapshot per trading day from local storage."""
    if not _QUANT_DATA_DB.is_file():
        return []
    try:
        with sqlite3.connect(_QUANT_DATA_DB) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT trade_date, main_net, source, as_of FROM (
                       SELECT trade_date, main_net, source, as_of,
                              ROW_NUMBER() OVER (
                                  PARTITION BY trade_date ORDER BY as_of DESC, version DESC
                              ) AS row_rank
                         FROM fund_flow
                        WHERE code = ? AND main_net IS NOT NULL
                   ) WHERE row_rank = 1
                   ORDER BY trade_date DESC LIMIT ?""",
                (code, max(1, int(limit))),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in reversed(rows)]


def _fetch_sina_fund_flow_history(code: str, limit: int = 100) -> list[dict]:
    """Fetch one internally consistent Sina r0_net series; reject a dirty series whole."""
    if not re.fullmatch(r"\d{6}", code):
        return []
    try:
        calendar_doc = json.loads(_TRADING_CALENDAR_PATH.read_text(encoding="utf-8"))
        trading_dates = set(calendar_doc["trading_dates"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return []
    request_limit = max(20, min(100, int(limit)))
    market = "sh" if code.startswith(("5", "6", "9")) else "bj" if code.startswith(("4", "8")) else "sz"
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(
            _SINA_FUND_FLOW_URL,
            params={
                "daima": market + code,
                "page": 1,
                "num": request_limit,
                "sort": "opendate",
                "asc": 0,
            },
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
                "Referer": "https://money.finance.sina.com.cn/",
            },
            timeout=12,
            verify=certifi.where(),
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return []
    finally:
        session.close()
    if not isinstance(payload, list):
        return []

    fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
    by_date: dict[str, dict] = {}
    for raw in payload:
        if not isinstance(raw, dict):
            return []
        trade_date = str(raw.get("opendate") or "")
        main_net = _pct(raw.get("r0_net"))
        trade = _pct(raw.get("trade"))
        turnover = _pct(raw.get("turnover"))
        net_amount = _pct(raw.get("netamount"))
        category_nets = [_pct(raw.get(field)) for field in ("r0_net", "r1_net", "r2_net", "r3_net")]
        if (
            trade_date not in trading_dates
            or main_net is None
            or trade is None
            or turnover is None
            or net_amount is None
            or any(value is None or not math.isfinite(value) for value in category_nets)
            or not all(math.isfinite(value) for value in (main_net, trade, turnover, net_amount))
            or trade <= 0
            or turnover <= 0
            or all(value == 0 for value in category_nets)
            or not math.isclose(sum(category_nets), net_amount, rel_tol=0.0, abs_tol=0.05)
            or trade_date in by_date
        ):
            return []
        by_date[trade_date] = {
            "trade_date": trade_date,
            "main_net": main_net,
            "source": _SINA_FUND_FLOW_SOURCE,
            "as_of": fetched_at,
            "method": _SINA_FUND_FLOW_METHOD,
            "unit": "元",
        }
    rows = [by_date[trade_date] for trade_date in sorted(by_date)]
    return rows[-request_limit:] if len(rows) >= 20 else []


def _load_veto_fund_flow_history(code: str, limit: int = 20) -> list[dict]:
    """Prefer a complete Sina series; never splice it with local provider rows."""
    now = time.monotonic()
    with _sina_fund_flow_lock:
        cached = _sina_fund_flow_cache.get(code)
        if cached is None or now - cached[0] >= _SINA_FUND_FLOW_TTL:
            rows = _fetch_sina_fund_flow_history(code, 100)
            _sina_fund_flow_cache[code] = (now, rows)
        else:
            rows = cached[1]
    required = max(1, int(limit))
    if len(rows) >= required:
        return [dict(row) for row in rows[-required:]]
    return _load_fund_flow_history(code, required)


def _verified_sina_fund_flow_window(history: list[dict], veto_item: dict) -> dict | None:
    """Map the already-gated Sina series into one auditable profile contract."""
    rows = list(history or [])[-20:]
    dates = [str(row.get("trade_date") or "") for row in rows]
    values = [_pct(row.get("main_net")) for row in rows]
    if (
        len(rows) != 20
        or dates != sorted(dates)
        or len(set(dates)) != 20
        or any(value is None or not math.isfinite(value) for value in values)
        or any(row.get("source") != _SINA_FUND_FLOW_SOURCE for row in rows)
        or any(row.get("method") != _SINA_FUND_FLOW_METHOD for row in rows)
        or any(row.get("unit") != "元" for row in rows)
        or veto_item.get("status") not in ("observation_clear", "observation_triggered")
    ):
        return None
    latest_value = values[-1]
    return {
        "status": "complete",
        "history_verified": True,
        "fund_net_yi": round(latest_value / 100_000_000, 8),
        "latest_main_net_yi": round(latest_value / 100_000_000, 8),
        "latest_trade_date": dates[-1],
        "window_start": dates[0],
        "window_end": dates[-1],
        "window_days": 20,
        "negative_streak_days": veto_item.get("negative_streak"),
        "continuous_outflow_status": veto_item.get("status"),
        "continuous_outflow_triggered": veto_item.get("triggered"),
        "history_source": _SINA_FUND_FLOW_SOURCE,
        "history_method": _SINA_FUND_FLOW_METHOD,
        "history_as_of": veto_item.get("as_of"),
        "history_unit": "元",
    }


def _price_risk_metrics(candles) -> dict:
    closes = []
    for candle in candles or []:
        value = candle.get("close") if isinstance(candle, dict) else getattr(candle, "close", None)
        if _pct(value) is not None:
            closes.append(float(value))
    closes = closes[-60:]
    if len(closes) < 20:
        return {}
    peak, max_drawdown = closes[0], 0.0
    for close in closes:
        peak = max(peak, close)
        max_drawdown = max(max_drawdown, (1 - close / peak) * 100 if peak else 0.0)
    returns = [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes)) if closes[index - 1] > 0]
    if len(returns) < 2:
        return {"max_drawdown_pct": round(max_drawdown, 2)}
    average = sum(returns) / len(returns)
    daily_std = math.sqrt(sum((value - average) ** 2 for value in returns) / (len(returns) - 1))
    return {
        "max_drawdown_pct": round(max_drawdown, 2),
        "annualized_volatility_pct": round(daily_std * math.sqrt(252) * 100, 2),
        "sample_size": len(closes),
    }


# ── P1b 专业评分工具：连续锚点 / 横截面分位 / 资金流快照 ──

def _tier_score(value, tiers):
    """按冻结锚点做分段线性评分，避免阶梯分制造大面积同分。"""
    if value is None:
        return None
    value = float(value)
    if value >= tiers[0][0]:
        return float(tiers[0][1])
    for (upper_x, upper_score), (lower_x, lower_score) in zip(tiers, tiers[1:]):
        if value < lower_x:
            continue
        if lower_x <= -1e8:
            return float(lower_score)
        span = upper_x - lower_x
        if span <= 0:
            return float(lower_score)
        ratio = (value - lower_x) / span
        return round(lower_score + ratio * (upper_score - lower_score), 2)
    return float(tiers[-1][1])


def _weighted_score(parts) -> float:
    """按各 part['weight'] 加权平均（缺失项不参与，权重归一化）。"""
    scored = [p for p in parts if p.get("score") is not None]
    if not scored:
        return None
    total_w = sum(p.get("weight", 1.0) for p in scored)
    return round(sum(p["score"] * p.get("weight", 1.0) for p in scored) / total_w, 1)


def _data_gap_status(dims: dict) -> tuple:
    """公司侧四维（成长/质量/估值/风险）缺失数 → 数据完整度门槛：
    ≥2 缺 → critical（不授予投资评级）；恰 1 缺 → moderate（评级上限中性）。
    返回 (level, missing_list)。"""
    missing = [k for k in _COMPANY_DIMS if (dims.get(k) or {}).get("score") is None]
    if len(missing) >= 2:
        return "critical", missing
    if len(missing) == 1:
        return "moderate", missing
    return None, missing


def _pctile_rank(value, values) -> float | None:
    """value 在 values 横截面中的百分位（0-100，含自身 ≤ 计数）。"""
    if not values or value is None:
        return None
    return round(sum(1 for v in values if v <= value) / len(values) * 100, 1)


def _load_market_valuation_cross_section() -> dict | None:
    """实时全市场估值跨截面（腾讯批量行情，网络主源）。

    宇宙 = 本机历史资金流快照的证券代码清单（与全市场快照同源，仅借代码清单，
    不读取资金字段）；批量拉 PE_TTM/PB。TTL 缓存 + 锁；网络失败/空宇宙返回 None
    （调用方回退 PIT 面板），绝不虚构数据。"""
    global _valuation_cs_cache
    now = time.monotonic()
    cached = _valuation_cs_cache
    if cached is not None and now - cached["fetched"] < _VALUATION_CS_TTL:
        return cached["section"]
    with _valuation_cs_lock:
        now = time.monotonic()
        cached = _valuation_cs_cache
        if cached is not None and now - cached["fetched"] < _VALUATION_CS_TTL:
            return cached["section"]
        section = None
        try:
            try:
                from .market_intelligence import _tencent_market_quotes
            except ImportError:
                from market_intelligence import _tencent_market_quotes
            raw = json.loads(_FUND_FLOW_JSON.read_text(encoding="utf-8"))
            codes = sorted(str(c) for c in (raw.get("stocks") or {}) if re.fullmatch(r"[0368]\d{5}", str(c)))
            if codes:
                quotes, meta = _tencent_market_quotes(codes)
                if quotes:
                    section = _build_valuation_section(quotes, meta)
        except Exception:
            section = None  # 网络失败 → 诚实回退，不伪造
        _valuation_cs_cache = {"fetched": now, "section": section}
        return section


def _build_valuation_section(quotes: list, meta: dict) -> dict:
    """批量行情 → 统一跨截面结构 {source, as_of, coverage, stocks, market, by_industry}。
    负 PE 剔除（估值负无意义，改用 PB）；PE≤300/PB≤50 过滤异常值。"""
    stock_meta = _load_stock_meta()
    stocks: dict = {}
    pe_all, pb_all = [], []
    by_industry: dict = {}
    as_of_values = []
    for q in quotes:
        code = str(q.get("code") or "")
        if not code:
            continue
        pe = _pct(q.get("pe_ttm"))
        pb = _pct(q.get("pb"))
        stocks[code] = {"pe_ttm": pe, "pb": pb, "as_of": q.get("as_of")}
        as_of = q.get("as_of")
        if as_of is not None:
            as_of_values.append(as_of)
        ind = (stock_meta.get(code) or {}).get("industry") or ""
        if pe is not None and 0 < pe <= 300:
            pe_all.append(pe)
            if ind:
                by_industry.setdefault(ind, {"pe": [], "pb": []})["pe"].append(pe)
        if pb is not None and 0 < pb <= 50:
            pb_all.append(pb)
            if ind:
                by_industry.setdefault(ind, {"pe": [], "pb": []})["pb"].append(pb)
    latest = None
    if as_of_values:
        try:
            latest = max(as_of_values)
        except TypeError:
            latest = None
    return {
        "source": "腾讯财经实时批量行情",
        "as_of": latest.isoformat() if latest is not None else None,
        "coverage": len(stocks),
        "expected": (meta or {}).get("coverage_expected"),
        "stocks": stocks,
        "market": {"pe": pe_all, "pb": pb_all},
        "by_industry": by_industry,
    }


def _pit_valuation_distribution() -> dict:
    """PIT 面板跨截面分布（回退源，惰性缓存）：{pe:[...], pb:[...], by_industry:...}。
    用各股最新一期 PE_TTM/PB（PIT 口径），负 PE 剔除（估值负无意义，改用 PB）。"""
    global _val_dist_cache
    if _val_dist_cache is not None:
        return _val_dist_cache
    panel = _load_pit_latest()
    meta = _load_stock_meta()
    pe_all, pb_all = [], []
    by_industry: dict = {}
    for code, p in panel.items():
        pe = _pct(p.get("pe_ttm"))
        pb = _pct(p.get("pb_mrq"))
        if pe is not None and 0 < pe <= 300:
            pe_all.append(pe)
        if pb is not None and 0 < pb <= 50:
            pb_all.append(pb)
        ind = (meta.get(code) or {}).get("industry") or ""
        if ind:
            bucket = by_industry.setdefault(ind, {"pe": [], "pb": []})
            if pe is not None and 0 < pe <= 300:
                bucket["pe"].append(pe)
            if pb is not None and 0 < pb <= 50:
                bucket["pb"].append(pb)
    _val_dist_cache = {"pe": pe_all, "pb": pb_all, "by_industry": by_industry}
    return _val_dist_cache


def _load_valuation_distribution() -> dict:
    """统一估值跨截面（主源=腾讯实时；回退=PIT 面板）。
    返回 {source, as_of, stocks, pe, pb, by_industry}；调用方据 source 诚实标注口径。"""
    live = _load_market_valuation_cross_section()
    if live is not None:
        return {
            "source": live["source"], "as_of": live["as_of"],
            "coverage": live["coverage"], "stocks": live["stocks"],
            "pe": live["market"]["pe"], "pb": live["market"]["pb"],
            "by_industry": live["by_industry"],
        }
    pit = _pit_valuation_distribution()
    return {
        "source": "PIT面板", "as_of": _pit_asof_hint(), "coverage": len(_load_pit_latest()),
        "stocks": {}, "pe": pit["pe"], "pb": pit["pb"], "by_industry": pit["by_industry"],
    }


def _fund_snapshot(code: str) -> tuple:
    """最近审计资金流快照 → (规范化亿元, 换手率%, 源时点)。
    文件 30s 内缓存解析结果，避免扫描池逐只重复读盘。"""
    global _fund_snapshot_cache
    now = time.monotonic()
    data = None
    if _fund_snapshot_cache is not None and now - _fund_snapshot_cache[0] < 30:
        data = _fund_snapshot_cache[1]
    else:
        try:
            data = json.loads(_FUND_FLOW_JSON.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        _fund_snapshot_cache = (now, data)
    info = (data.get("stocks") or {}).get(code) or {}
    if not isinstance(info, dict):
        return None, None, None
    freshness = audited_trading_day_age(
        data.get("ts") or data.get("date"), allow_current_session=True,
    )
    if freshness.get("status") != "fresh":
        return None, None, data.get("ts") or data.get("date")
    raw_net = info.get("raw_net_yi", info.get("net_yi"))
    turn = info.get("turnover")
    return (
        normalize_fund_flow_yi(raw_net),
        float(turn) if turn is not None else None,
        data.get("ts") or data.get("date"),
    )


# ─────────────────────────────────────────────
# 六维评分（每维 0-100，缺失返回 None 计入"数据不足"）
# ─────────────────────────────────────────────

def score_growth(metrics: dict) -> dict:
    """成长：净利同比40% + 扣非同比35% + 营收同比25%。
    冻结锚点间线性插值并对极端值封顶；利润仍为负时，同比改善最多计中性分。"""
    parts = []
    for key, name, w in (("revenue_yoy", "营收同比", 0.25),
                         ("np_yoy", "净利同比", 0.40),
                         ("deducted_yoy", "扣非同比", 0.35)):
        v = _pct(metrics.get(key))
        if v is not None:
            score = _tier_score(
                v, [(50,100),(30,85),(20,70),(10,60),(0,50),(-10,35),(-30,20),(-1e9,5)],
            )
            basis = f"{_fmt(v,1)}%"
            level_key = {"np_yoy": "net_profit", "deducted_yoy": "deducted"}.get(key)
            level = _pct(metrics.get(level_key)) if level_key else None
            if level_key and level is not None and level <= 0:
                score = min(score, 50.0)
                basis += "（当期仍亏损，最高按中性50分）"
            parts.append({
                "name": name, "value": v, "basis": basis,
                "weight": w, "score": score,
            })
    if not parts:
        return {"score": None, "parts": [], "note": "财务同比数据缺失"}
    return {"score": _weighted_score(parts), "parts": parts,
            "note": "净利40%+扣非35%+营收25%连续锚点；利润仍亏损时同比改善最高按中性50分；缺失通道权重归一化"}


def score_quality(metrics: dict) -> dict:
    """质量：ROE35% + 毛利率25% + 净利率20% + 经营现金流覆盖20%。
    负债率已移出（归风险维度，修复旧版双重计权）。锚点间连续插值，缺失权重归一化。"""
    parts = []
    roe = _pct(metrics.get("roe"))
    if roe is not None:
        parts.append({"name": "ROE", "value": roe, "basis": f"{_fmt(roe,1)}%", "weight": 0.35,
                      "score": _tier_score(roe, [(20,100),(15,90),(10,75),(8,65),(5,50),(0,30),(-1e9,10)])})
    gm = _pct(metrics.get("gross_margin"))
    if gm is not None:
        parts.append({"name": "毛利率", "value": gm, "basis": f"{_fmt(gm,1)}%", "weight": 0.25,
                      "score": _tier_score(gm, [(50,100),(40,90),(30,75),(20,60),(10,40),(0,20),(-1e9,5)])})
    nm = _pct(metrics.get("net_margin"))
    if nm is not None:
        parts.append({"name": "净利率", "value": nm, "basis": f"{_fmt(nm,1)}%", "weight": 0.20,
                      "score": _tier_score(nm, [(20,100),(15,90),(10,75),(5,60),(0,40),(-5,20),(-1e9,5)])})
    ocf_to_net_profit = _pct(metrics.get("ocf_to_net_profit"))
    ratio = ocf_to_net_profit
    ratio_name = "经营现金流/净利"
    if ratio is None:
        ocf_ps, eps = _pct(metrics.get("ocf_ps")), _pct(metrics.get("eps"))
        if ocf_ps is not None and eps:
            ratio, ratio_name = ocf_ps / eps, "现金流/每股收益"
    if ratio is not None:
        parts.append({"name": ratio_name, "value": ratio, "basis": _fmt(ratio, 2), "weight": 0.20,
                      "score": _tier_score(ratio, [(1.5,100),(1.0,90),(0.8,75),(0.5,60),(0,45),(-0.5,25),(-1e9,5)])})
    if not parts:
        return {"score": None, "parts": [], "note": "财务质量数据缺失"}
    return {"score": _weighted_score(parts), "parts": parts,
            "note": "ROE35/毛利率25/净利率20/现金流20 连续锚点；负债率在风险维计分，本维不再重复"}


def score_valuation(code: str, pit: dict | None = None) -> dict:
    """估值（反向）：跨截面分位（越便宜分越高）。
    主源=腾讯实时批量行情（全市场当前 PE_TTM/PB，TTL 缓存）；行业样本 ≥30 用行业分位，
    否则用市场分位（诚实标注口径）；负 PE 改用 PB 分位。实时截面无该股（停牌/未匹配）
    或实时源不可用 → 回退 PIT 面板该股最新期 + PIT 分布，并如实标注数据时点。"""
    dist = _load_valuation_distribution()
    if dist.get("source") == "腾讯财经实时批量行情":
        entry = (dist.get("stocks") or {}).get(code)
        if entry is not None:
            pe = _pct(entry.get("pe_ttm"))
            pb = _pct(entry.get("pb"))
            if pe is not None or pb is not None:
                return _valuation_from_cross_section(
                    code, pe, pb, dist, as_of=dist.get("as_of"),
                    note="实时跨截面分位（腾讯行情，越便宜分越高）；行业≥30样本用行业分位，否则市场分位；负PE用PB")
    # 回退：PIT 面板（该股无实时行情，或实时源不可用）——口径内部一致（PIT值+PIT分布）
    pit = pit if pit is not None else _load_pit_latest().get(code)
    pe = _pct(pit.get("pe_ttm")) if pit else None
    pb = _pct(pit.get("pb_mrq")) if pit else None
    if not pit or (pe is None and pb is None):
        return {"score": None, "parts": [], "source": None,
                "note": f"估值数据不足：实时行情与 PIT 面板均无 PE/PB（PIT 面板 as_of 最新 {_pit_asof_hint()}）",
                "as_of": pit.get("as_of") if pit else None}
    pit_dist = _pit_valuation_distribution()
    meta = _load_stock_meta().get(code, {})
    industry = str(meta.get("industry") or "").strip()
    parts = []
    use_pe = pe is not None and pe > 0
    if use_pe:
        scope = pit_dist["pe"]
        scope_label = "全市场"
        if industry and len((pit_dist["by_industry"].get(industry) or {}).get("pe") or []) >= 30:
            scope, scope_label = pit_dist["by_industry"][industry]["pe"], f"行业「{industry}」"
        rank = _pctile_rank(pe, scope)
        if rank is not None:
            parts.append({"name": "PE_TTM分位", "value": pe,
                          "basis": f"PE {_fmt(pe,1)} → 分位 {rank:.0f}%（{scope_label}）",
                          "weight": 0.6, "score": round(100 - rank, 1)})
    if pb is not None and pb > 0:
        scope = pit_dist["pb"]
        scope_label = "全市场"
        if industry and len((pit_dist["by_industry"].get(industry) or {}).get("pb") or []) >= 30:
            scope, scope_label = pit_dist["by_industry"][industry]["pb"], f"行业「{industry}」"
        rank = _pctile_rank(pb, scope)
        if rank is not None:
            parts.append({"name": "PB分位", "value": pb,
                          "basis": f"PB {_fmt(pb,2)} → 分位 {rank:.0f}%（{scope_label}）",
                          "weight": 0.4 if use_pe else 1.0, "score": round(100 - rank, 1)})
    scored = [p for p in parts if p.get("score") is not None]
    if not scored:
        return {"score": None, "parts": parts, "source": "PIT面板",
                "note": "估值数据异常", "as_of": pit.get("as_of")}
    return {"score": _weighted_score(parts), "parts": scored, "source": "PIT面板",
            "note": "PIT 面板跨截面分位（数据滞后，见 as_of）；行业≥30样本用行业分位，否则市场分位；负PE用PB",
            "as_of": pit.get("as_of")}


def _valuation_from_cross_section(code: str, pe, pb, dist: dict, as_of=None, note: str = "") -> dict:
    """用统一跨截面结构做分位评分（主路径）。pe/pb 为该股当前值；dist 提供横截面。"""
    meta = _load_stock_meta().get(code, {})
    industry = str(meta.get("industry") or "").strip()
    parts = []
    use_pe = pe is not None and pe > 0
    if use_pe:
        scope = dist["pe"]
        scope_label = "全市场"
        if industry and len((dist["by_industry"].get(industry) or {}).get("pe") or []) >= 30:
            scope, scope_label = dist["by_industry"][industry]["pe"], f"行业「{industry}」"
        rank = _pctile_rank(pe, scope)
        if rank is not None:
            parts.append({"name": "PE_TTM分位", "value": pe,
                          "basis": f"PE {_fmt(pe,1)} → 分位 {rank:.0f}%（{scope_label}）",
                          "weight": 0.6, "score": round(100 - rank, 1)})
    if pb is not None and pb > 0:
        scope = dist["pb"]
        scope_label = "全市场"
        if industry and len((dist["by_industry"].get(industry) or {}).get("pb") or []) >= 30:
            scope, scope_label = dist["by_industry"][industry]["pb"], f"行业「{industry}」"
        rank = _pctile_rank(pb, scope)
        if rank is not None:
            parts.append({"name": "PB分位", "value": pb,
                          "basis": f"PB {_fmt(pb,2)} → 分位 {rank:.0f}%（{scope_label}）",
                          "weight": 0.4 if use_pe else 1.0, "score": round(100 - rank, 1)})
    scored = [p for p in parts if p.get("score") is not None]
    if not scored:
        return {"score": None, "parts": parts, "source": dist.get("source"),
                "note": "估值数据异常（实时截面无有效 PE/PB）", "as_of": as_of}
    return {"score": _weighted_score(parts), "parts": scored,
            "source": dist.get("source"), "note": note, "as_of": as_of}


def score_technical(snap: dict) -> dict:
    """技术（7 透镜加权）：趋势30 + 动量25 + RSI20 + 布林15 + 量比10（K线5 + 盘中5）。
    ATR14波动仅作观察，不参与评分。缺失透镜权重归一化；快照时点以 K 线最新 bar 为准。"""
    if not snap or snap.get("status") == "insufficient_history" or not snap.get("lenses"):
        return {"score": None, "parts": [], "note": "历史K线不足，技术维度不评分"}
    _TECH_WEIGHTS = {"ema_trend": 0.30, "macd_momentum": 0.25, "rsi14": 0.20,
                     "bollinger_position": 0.15, "volume_ratio20": 0.05, "intraday_volume_ratio5": 0.05}

    def lens_score(lens: dict):
        lid = lens.get("id", "")
        state = lens.get("state", "")
        raw = str(lens.get("value", ""))
        if lid == "ema_trend":
            return 75 if state == "up" else 25
        if lid == "macd_momentum":
            return 75 if state == "positive" else 25
        if lid == "rsi14":
            try:
                rsi = float(raw)
            except (TypeError, ValueError):
                rsi = 50.0
            return round(100 - abs(rsi - 50) * 2, 1)  # 50 中性最优（均值回归）
        if lid == "bollinger_position":
            try:
                pos = float(raw.replace("%", "")) / 100.0
            except (TypeError, ValueError):
                pos = 0.5
            return round(100 - abs(pos - 0.5) * 200, 1)  # 0.5 中性满分，两端扣分
        if lid == "volume_ratio20":
            try:
                vr = float(raw)
            except (TypeError, ValueError):
                vr = 1.0
            return 90 if vr >= 2.0 else 75 if vr >= 1.2 else 50
        if lid == "intraday_volume_ratio5":
            if state == "unavailable":
                return None
            try:
                vr = float(raw)
            except (TypeError, ValueError):
                return None
            return 85 if vr >= 1.5 else 70 if vr >= 1.2 else 50
        return None

    parts = []
    atr = None
    for lens in snap["lenses"]:
        lid = lens.get("id", "")
        if lid == "atr14":
            atr = lens  # 仅观察：波动上下文，不参与评分
            continue
        w = _TECH_WEIGHTS.get(lid)
        if not w:
            continue
        s = lens_score(lens)
        if s is None:
            continue
        parts.append({"name": lens.get("name", lid), "value": lens.get("value", ""),
                      "basis": lens.get("interpretation", ""), "weight": w, "score": s})
    if not parts:
        return {"score": None, "parts": [], "note": "技术指标不可用"}
    return {"score": _weighted_score(parts), "parts": parts,
            "note": "7透镜加权（趋势30/动量25/RSI20/布林15/量比10）；ATR14仅观察不评分；缺失透镜权重归一化",
            "atr": atr}


def score_funds(net_yi: float | None, turnover: float | None,
                amount: float | None = None, *,
                fund_as_of: str | None = None,
                amount_as_of: str | None = None) -> dict:
    """Return third-party flow as observation evidence, never as stock Alpha.

    THS ``net_yi`` is already 亿元 at collection time.  Its "主力" trade-size
    buckets are not identified investors and have no admitted predictive test,
    so this function deliberately returns ``score=None``.  Turnover remains a
    visible liquidity observation but is not silently promoted into this score.
    """
    source_days_provided = bool(fund_as_of or amount_as_of)
    same_source_day = bool(
        fund_as_of and amount_as_of
        and str(fund_as_of)[:10] == str(amount_as_of)[:10]
    )
    comparable_amount = (
        amount if not source_days_provided or same_source_day else None
    )
    observations = []
    if net_yi is not None:
        net_canonical = _pct(net_yi)
        if net_canonical is None:
            return {"score": None, "parts": [], "observations": [],
                    "role": "research_observation_only", "note": "资金流数值无效"}
        amount_yi = (
            _pct(comparable_amount) / 1e8
            if comparable_amount is not None else None
        )
        if amount_yi is not None and amount_yi > 0:
            ratio = net_canonical / amount_yi * 100  # %
            observations.append({"name": "第三方大单净额/成交额", "value": ratio,
                                 "basis": f"{_fmt(ratio,1)}%"})
        else:
            observations.append({"name": "第三方大单净额", "value": net_canonical,
                                 "basis": f"{_fmt(net_canonical,2)}亿"})
    if turnover is not None:
        t = _pct(turnover)
        if t is not None:
            observations.append({"name": "换手率", "value": t, "basis": f"{_fmt(t,2)}%"})
    if not observations:
        return {"score": None, "parts": [], "observations": [],
                "role": "research_observation_only", "note": "资金流/换手数据缺失"}
    amount_note = (
        "资金流与成交额为同一交易日，净流入/成交额占比60%"
        if same_source_day else
        "资金流与成交额时点不同或时点不完整，不计算占比；规范化净额绝对档60%"
        if source_days_provided else
        "未提供时点合同，按规范化净额绝对档60%"
    )
    return {"score": None, "parts": [], "observations": observations,
            "role": "research_observation_only", "decision_eligible": False,
            "note": amount_note + "；仅作可追溯观察，不参与综合评分、买点或仓位"}


def _is_financial_industry(industry: str | None) -> bool:
    value = str(industry or "").strip().upper()
    return bool(
        value.startswith(("J66", "J67", "J68", "J69"))
        or any(token in value for token in ("金融", "银行", "保险", "证券"))
    )


def score_risk(gates: dict, metrics: dict, market_risk: dict | None = None,
               industry: str | None = None) -> dict:
    """风险（反向，分越高=风险越低）：ST/质押/负债率/流动比率/事件红黄灯/近60日回撤与波动，等权。
    流动比率 <1 视为流动性风险；治理灯号已有具体指标时不再重复计分（防双重计权）。"""
    parts = []
    gov = (gates.get("governance") or {}).get("metrics") or {}
    if gov.get("is_st"):
        parts.append({"name": "ST 退市风险", "value": "是", "basis": "证券简称含 ST", "score": 0})
    mt = _pct(gov.get("max_total_pledge_pct"))
    if mt is not None:
        # 质押低=好：5%→100 分，50%→0 分
        parts.append({"name": "质押占总股本", "value": mt, "basis": f"{_fmt(mt,1)}%",
                      "score": _score_from_value(mt, 5, 50, invert=True)})
    audit_category = str(gov.get("audit_opinion_category") or "")
    audit_scores = {
        "standard_unqualified": 100,
        "unqualified_with_emphasis": 45,
        "qualified": 15,
        "disclaimer": 0,
        "adverse": 0,
    }
    if audit_category in audit_scores:
        parts.append({
            "name": "最近年度审计意见",
            "value": gov.get("audit_opinion_label") or audit_category,
            "basis": (
                f"{gov.get('audit_report_year') or '最近年度'}年 · "
                f"{gov.get('audit_opinion_label') or audit_category}"
            ),
            "score": audit_scores[audit_category],
        })
    # 扫描池只传公司档案 gates；个股工作台另传 meta.industry。两条链都从同一
    # 行业事实判断，避免银行在机会榜被工业企业负债率口径扣分、个股页却不扣。
    gate_industry = ((gates.get("industry") or {}).get("metrics") or {}).get("industry")
    financial_industry = _is_financial_industry(industry or gate_industry)
    if not financial_industry:
        dr = _pct(metrics.get("debt_ratio"))
        if dr is not None:
            # 低负债=好：40%→100 分，90%→0 分
            parts.append({"name": "资产负债率", "value": dr, "basis": f"{_fmt(dr,1)}%",
                          "score": _score_from_value(dr, 40, 90, invert=True)})
        cr = _pct(metrics.get("current_ratio"))
        if cr is not None:
            s = 0 if cr < 1 else 50 if cr < 1.5 else 90 if cr < 3 else 70
            parts.append({"name": "流动比率", "value": cr, "basis": f"{_fmt(cr,2)}",
                          "score": s})
    market_risk = market_risk or {}
    max_drawdown = _pct(market_risk.get("max_drawdown_pct"))
    annualized_volatility = _pct(market_risk.get("annualized_volatility_pct"))
    if max_drawdown is not None:
        parts.append({"name": "近60根最大回撤", "value": max_drawdown,
                      "basis": f"{_fmt(max_drawdown,1)}%",
                      "score": _score_from_value(max_drawdown, 5, 35, invert=True)})
    if annualized_volatility is not None:
        parts.append({"name": "近60根年化波动", "value": annualized_volatility,
                      "basis": f"{_fmt(annualized_volatility,1)}%",
                      "score": _score_from_value(annualized_volatility, 15, 70, invert=True)})
    for gate_name, label in (("governance", "治理"), ("fundamentals", "基本面"),
                             ("events", "事件"), ("industry", "行业")):
        g = gates.get(gate_name) or {}
        if gate_name == "governance" and (g.get("metrics") or {}):
            continue  # ST/质押已单独计分，避免重复
        if g.get("light") == "red":
            parts.append({"name": f"{label}红灯", "value": "触发", "basis": "；".join(g.get("reasons", [])[:1]), "score": 0})
        elif g.get("light") == "yellow":
            parts.append({"name": f"{label}黄灯", "value": "注意", "basis": "；".join(g.get("reasons", [])[:1]), "score": 50})
    if not parts:
        return {"score": None, "parts": [], "note": "风险数据不足（治理信息不完整），风险维度不计分"}
    note = (
        "金融行业不套用工业企业资产负债率/流动比率；当前按ST、质押、审计意见、事件与近60日价格风险计分，"
        "资本充足率与资产质量未接入时不作推断"
        if financial_industry else
        "风险维度反向计分：分越高风险越低；ST/质押/负债率/流动比率/事件灯号/近60日价格风险等权"
    )
    return {"score": _weighted_score(parts), "parts": parts, "note": note}


# ─────────────────────────────────────────────
# 综合评分 + 评级 + 一票否决
# ─────────────────────────────────────────────

def _pit_asof_hint() -> str:
    pit = _load_pit_latest()
    asofs = {v["as_of"] for v in pit.values() if v.get("as_of")}
    return max(asofs) if asofs else "未知"


_COMPOSITE_FRESHNESS_LIMIT_DAYS = {
    "growth": 180,
    "quality": 180,
    "valuation": 10,
    "risk": 180,
    "technical": 3,
}


def _composite_as_of_audit(as_of_by_dimension: dict | None) -> dict | None:
    """Build a comparable, user-visible time audit for a mixed-input score.

    Calendar-day limits are intentionally explicit.  Inputs outside their
    limit (or without a parseable source date) are excluded from the score;
    observing or regenerating a stale snapshot cannot make it fresh.
    """
    if as_of_by_dimension is None:
        return None
    today = datetime.now().astimezone().date()
    inputs: dict[str, dict] = {}
    valid_ages: list[int] = []
    for key, limit in _COMPOSITE_FRESHNESS_LIMIT_DAYS.items():
        raw = as_of_by_dimension.get(key)
        source_day = None
        try:
            source_day = datetime.fromisoformat(str(raw)[:10]).date() if raw else None
        except (TypeError, ValueError):
            source_day = None
        raw_age_days = (today - source_day).days if source_day else None
        future_dated = raw_age_days is not None and raw_age_days < 0
        age_days = raw_age_days if raw_age_days is not None and not future_dated else None
        if age_days is not None:
            valid_ages.append(age_days)
        within_limit = age_days is not None and age_days <= limit and not future_dated
        inputs[key] = {
            "as_of": str(raw) if raw else None,
            "age_calendar_days": age_days,
            "freshness_limit_calendar_days": limit,
            "within_limit": within_limit if source_day else None,
            "status": (
                "invalid_future" if future_dated else
                "eligible" if within_limit else "stale" if source_day else "unknown"
            ),
        }
    excluded = [key for key, row in inputs.items() if row["within_limit"] is not True]
    return {
        "inputs": inputs,
        "max_input_age_calendar_days": max(valid_ages) if valid_ages else None,
        "comparability_status": "comparable" if not excluded else "limited",
        "excluded_dimensions": excluded,
        "rule": "超过各维度明确日历日上限或缺少源日期的输入不参与综合评分",
    }


def compute_composite(dims: dict, timing_score: float | None = None,
                      veto: bool = False,
                      as_of_by_dimension: dict | None = None) -> dict:
    """统一专业个股综合评分（V3 唯一合成入口）。
    权重：成长22/质量22/估值16/个股风险20/已完成K线技术20。
    ``timing_score`` 仅保留兼容调用；盘中条件与第三方资金流不进入个股总分，
    分别由执行复核层和观察证据层承载。缺失维度不计入加权（权重归一化）。
    返回 composite / research_band / 兼容 rating / data_gap / confidence / weight_used 等。
    一票否决与 data_gap 的评级修正由调用方（build_workstation / _composite_grade）统一处理。"""
    time_audit = _composite_as_of_audit(as_of_by_dimension)
    avail = []
    for key, w in PROFESSIONAL_WEIGHTS.items():
        s = (dims.get(key) or {}).get("score")
        if s is None:
            continue
        if time_audit is not None:
            freshness = time_audit["inputs"].get(key) or {}
            if freshness.get("within_limit") is not True:
                continue
        avail.append((key, w, float(s)))
    if not avail:
        return {"composite": None, "rating": None, "detail": [], "weight_used": 0.0,
                "data_gap": None, "data_gap_missing": [], "confidence": "low",
                "coverage_status": "insufficient", "coverage_pct": 0.0,
                "as_of_audit": time_audit,
                "note": "全部维度数据不足，无法合成综合评分"}
    total_w = sum(w for _, w, _ in avail)
    composite = round(sum(w * s for _, w, s in avail) / total_w, 1)
    rating = "排除"
    for lo, r in PROFESSIONAL_RATING:
        if composite >= lo:
            rating = r
            break
    research_band = next(
        label for lower, label in RESEARCH_SCORE_BANDS if composite >= lower
    )
    gap_level, gap_missing = _data_gap_status(dims)
    if total_w >= 0.85 and gap_level is None:
        confidence = "high"
    elif total_w >= 0.60:
        confidence = "medium"
    else:
        confidence = "low"
    coverage_status = "complete" if total_w >= 0.999 else ("partial" if total_w >= 0.60 else "insufficient")
    notes = []
    if gap_level == "critical":
        notes.append("公司侧数据缺口critical（基本面不可核验），评级上限回避，不授予投资评级")
    elif gap_level == "moderate":
        notes.append("公司侧数据缺1维，评级上限中性")
    notes.append("权重：个股成长22%+质量22%+估值16%+风险20%+已完成K线技术20%；盘中条件与第三方资金流不进入个股总分；缺失权重归一化")
    if time_audit and time_audit["excluded_dimensions"]:
        notes.append(
            "时点不可比或超出上限的维度已排除："
            + "、".join(time_audit["excluded_dimensions"])
        )
    used_keys = {key for key, _, _ in avail}
    return {
        "methodology_version": "professional_composite_v4_profit_sign_aware",
        "score_semantics": "个股总分只加权可用且时点合格的个股Alpha维度；分数档仅作研究排序，不是买卖建议；市场状态管总仓位，盘中条件管执行复核，第三方资金流只观察",
        "composite": composite,
        "research_band": research_band,
        "score_rating": rating,
        "rating": rating,
        "detail": [{"key": k, "weight": w, "base_weight": w,
                    "effective_weight": round(w / total_w, 4), "score": s,
                    "basis": [
                        (
                            f"{part.get('name') or '分项'}: "
                            f"{part.get('basis') or part.get('value') or '输入已核验'}"
                            + (
                                f" → {float(part.get('score')):.1f}分"
                                if part.get("score") is not None else ""
                            )
                        )
                        for part in ((dims.get(k) or {}).get("parts") or [])
                        if isinstance(part, dict)
                    ] or (
                        [
                            f"盘中条件匹配基线: {s:.1f}分"
                            if s == 50 else
                            f"盘中条件匹配分: {s:.1f}分"
                        ]
                        if k == "timing" else []
                    ),
                    "dimension_note": (dims.get(k) or {}).get("note"),
                    "as_of": ((time_audit or {}).get("inputs", {}).get(k) or {}).get("as_of"),
                    "age_calendar_days": ((time_audit or {}).get("inputs", {}).get(k) or {}).get("age_calendar_days")}
                   for k, w, s in avail],
        "weight_used": round(total_w, 2),
        "coverage_pct": round(total_w * 100, 1),
        "coverage_status": coverage_status,
        "missing": [k for k in PROFESSIONAL_WEIGHTS if k not in used_keys],
        "excluded_for_freshness": (
            time_audit.get("excluded_dimensions", []) if time_audit else []
        ),
        "as_of_audit": time_audit,
        "data_gap": gap_level,
        "data_gap_missing": gap_missing,
        "confidence": confidence,
        "note": "；".join(notes),
    }


def apply_rating_overrides(composite: dict, veto: dict | None = None) -> dict:
    """统一动作修正：保留分数评级，真实风险只改动作评级。

    data_gap critical → 上限回避；moderate → 上限中性。
    ``score_rating`` 永远由同一个综合分区间产生；兼容字段 ``rating``
    仍表示经过风险/覆盖修正后的动作评级。"""
    if composite.get("composite") is None:
        return composite
    c = dict(composite)
    c.setdefault("score_rating", c.get("rating"))
    gap = c.get("data_gap")
    if veto and veto.get("vetoed"):
        reasons = "；".join(veto.get("reasons") or [])
        c["rating"] = "排除"
        c["note"] = f"{c.get('note','')}；一票否决生效：{reasons or '排除新开仓'}"
        c["veto_note"] = reasons
    elif veto and veto.get("verification_complete") is False:
        c["confidence"] = "low"
        c["veto_verification_status"] = "incomplete"
        missing = "、".join(veto.get("unverified_rules") or [])
        c["note"] = (
            f"{c.get('note','')}；关键风险证据待更新（{missing or '来源缺失'}），"
            "仅降低置信度；未确认触发，不作为否决"
        )
    elif gap == "critical":
        c["rating"] = "回避"
        c["note"] = f"{c.get('note','')}；基本面数据缺口critical，不授予投资评级（上限回避）"
    elif c.get("coverage_status") == "insufficient":
        c["rating"] = "回避"
        c["note"] = f"{c.get('note','')}；有效权重低于60%，覆盖不足，评级上限回避"
    elif gap == "moderate" and c["rating"] in ("强烈买入", "买入"):
        c["rating"] = "中性"
    c["action_rating"] = c.get("rating")
    return c


def profile_fundamental_scoring(code: str, co: dict | None = None,
                                amount: float | None = None,
                                amount_as_of: str | None = None,
                                technical_snap: dict | None = None,
                                technical_as_of: str | None = None,
                                market_risk: dict | None = None,
                                *, load_veto_history: bool = True) -> dict | None:
    """live-scan / 今日机会 用的轻量公司侧六维评分。
    数据来源：归档财务（V12 sqlite）+ PIT 估值分位 + 档案闸门灯号 + 当日资金流快照。
    调用方传入已有审计日K技术快照时复用，不另拉行情；异常返回 None。
    返回 {dims, data_gap, data_gap_missing, veto, company_score, note}。"""
    try:
        co = co or {}
        fin = _archived_financial_metrics(code)
        # 合并在线档案基本面指标（roe/毛利率/负债率/流动比率/扣非…），
        # 与 build_workstation 同口径：只覆盖非空值，避免空在线响应抹掉本机归档结果。
        online = co.get("fundamental_metrics") or {}
        fin = {**fin, **{k: v for k, v in online.items() if v is not None}}
        pit = _load_pit_latest().get(code)
        lights = co.get("lights") or {}
        reasons = co.get("reasons") or {}
        gates_light = {
            key: dict(value) for key, value in (co.get("gates") or {}).items()
            if isinstance(value, dict)
        }
        for key, light in lights.items():
            gate = gates_light.setdefault(key, {})
            gate.setdefault("light", light)
            gate.setdefault("reasons", (reasons.get(key) or [])[:1])
        fundamentals = gates_light.setdefault("fundamentals", {})
        fundamentals["metrics"] = {
            **(fundamentals.get("metrics") or {}), **fin,
        }
        net, turn, fund_as_of = _fund_snapshot(code)
        valuation = score_valuation(code, pit)
        dims = {
            "growth": score_growth(fin),
            "quality": score_quality(fin),
            "valuation": valuation,
            "risk": score_risk(gates_light, fin, market_risk),
            "technical": (
                score_technical(technical_snap)
                if technical_snap else {
                    "score": None,
                    "note": "扫描池审计日K技术快照不可用，本轮技术维度不纳入",
                }
            ),
            "funds": score_funds(
                net, turn, amount,
                fund_as_of=fund_as_of, amount_as_of=amount_as_of,
            ),
        }
        gap_level, gap_missing = _data_gap_status(dims)
        veto_audit = one_vote_veto(
            gates_light,
            technical_snap or {},
            pit or {},
            (
                _load_veto_fund_flow_history(code, 20)
                if load_veto_history else []
            ),
        )
        if co.get("veto") and not veto_audit.get("vetoed"):
            veto_audit = {
                **veto_audit,
                "vetoed": True,
                "reasons": list(veto_audit.get("reasons") or [])
                + ["公司档案五闸门已触发否决"],
            }
        return {
            "dims": dims,
            "as_of_by_dimension": {
                "growth": fin.get("data_as_of") or fin.get("period"),
                "quality": fin.get("data_as_of") or fin.get("period"),
                "valuation": valuation.get("as_of"),
                "risk": fin.get("data_as_of") or fin.get("period"),
                "technical": technical_as_of,
                "funds": fund_as_of,
            },
            "data_gap": gap_level,
            "data_gap_missing": gap_missing,
            "veto": bool(co.get("veto")),
            "veto_audit": veto_audit,
            "company_score": co.get("company_score"),
            "note": (
                "扫描池口径：归档财务+PIT分位+档案灯号+最近审计资金流+本机审计日K技术快照"
                if technical_snap else
                "扫描池口径：归档财务+PIT分位+档案灯号+最近审计资金流；技术快照不可用"
            ),
        }
    except Exception:
        return None


def compact_professional_preview(
    code: str,
    *,
    amount: float | None,
    amount_as_of: str | None,
    technical_snap: dict | None,
    technical_as_of: str | None,
    market_risk: dict | None,
) -> dict:
    """Compute the shared professional score from local PIT inputs only.

    This preview is for full-universe comparison before the bounded online
    company-profile refresh.  It deliberately performs no network request and
    never fills missing evidence with a neutral score.  The final stock page
    may add fresher governance/event evidence and remains authoritative for
    actionability; both paths use the same dimension functions and composite
    formula.
    """
    try:
        from .company_profile import get_cached_summary
        cached_profile = get_cached_summary(code) or {}
    except Exception:
        cached_profile = {}
    scoring = profile_fundamental_scoring(
        code,
        cached_profile,
        amount,
        amount_as_of=amount_as_of,
        technical_snap=technical_snap,
        technical_as_of=technical_as_of,
        market_risk=market_risk,
        load_veto_history=False,
    )
    if not scoring:
        return {
            "status": "unavailable_local_inputs",
            "professional_score": None,
            "decision_eligible": False,
            "network_reads": False,
        }
    result = apply_rating_overrides(
        compute_composite(
            scoring.get("dims") or {},
            timing_score=50.0,
            veto=False,
            as_of_by_dimension=scoring.get("as_of_by_dimension") or {},
        ),
        scoring.get("veto_audit") or {},
    )
    score = _pct(result.get("composite"))
    comparable = bool(
        score is not None
        and result.get("coverage_status") in {"complete", "partial"}
    )
    raw_action = str(result.get("action_rating") or result.get("rating") or "")
    if raw_action in {"排除", "卖出"}:
        review_state = "排除新开仓"
    elif raw_action == "回避":
        review_state = "风险复核"
    elif raw_action == "中性":
        review_state = "继续观察"
    else:
        review_state = "研究筛选通过"
    research_band = str(result.get("research_band") or "评分数据不足")
    return {
        "status": "available" if comparable else "unavailable_incomplete_professional_score",
        "method": "professional_composite_v4_profit_sign_aware",
        "evidence_scope": "local_archived_PIT_and_completed_daily_no_network",
        "professional_score": round(score, 2) if comparable else None,
        "coverage_pct": result.get("coverage_pct"),
        "coverage_status": result.get("coverage_status"),
        "data_gap": result.get("data_gap"),
        "data_gap_missing": result.get("data_gap_missing") or [],
        "excluded_for_freshness": result.get("excluded_for_freshness") or [],
        "confidence": result.get("confidence"),
        "score_band_rating": research_band,
        "score_rating": research_band,
        "action_rating": review_state,
        "rating_scope": "research_only_not_trade_recommendation",
        "trade_recommendation": False,
        "veto_verification_complete": bool(
            (scoring.get("veto_audit") or {}).get("verification_complete") is True
        ),
        "unverified_veto_rules": (
            (scoring.get("veto_audit") or {}).get("unverified_rules") or []
        ),
        "as_of_by_dimension": scoring.get("as_of_by_dimension") or {},
        "decision_eligible": False,
        "network_reads": False,
    }


def one_vote_veto(gates: dict, snap: dict, pit: dict,
                  fund_history: list[dict] | None = None) -> dict:
    """一票否决（7.6）：任一项触发 → 直接降至 D 级。返回 {vetoed, items, reasons}."""
    items = []
    gov = (gates.get("governance") or {}).get("metrics") or {}
    # ① 退市风险 / ST
    st_triggered = gov.get("is_st") if gov.get("is_st") in (True, False) else None
    items.append({
        "rule": "退市风险/ST",
        "triggered": st_triggered,
        "status": "triggered" if st_triggered is True else "clear" if st_triggered is False else "unverifiable",
        "basis": "证券简称含 ST" if st_triggered else "证券简称未见 ST" if st_triggered is False else "证券简称状态不可核验",
    })
    # ② 股权质押>50%（占总股本）
    mt = _pct(gov.get("max_total_pledge_pct"))
    pledge_unknown = gov.get("data_unavailable") is True or mt is None
    pledge_triggered = None if pledge_unknown else mt > 50
    pledge_source = str(gov.get("pledge_source") or "").strip()
    items.append({
        "rule": "股权质押 >50%",
        "triggered": pledge_triggered,
        "status": "unverifiable" if pledge_triggered is None else "triggered" if pledge_triggered else "clear",
        "basis": (
            "质押来源不可用，无法核验"
            if pledge_triggered is None else (
                f"截至 {gov.get('pledge_as_of') or '—'} 质押占总股本 {_fmt(mt,2)}%"
                + (f" · 来源：{pledge_source}" if pledge_source else "")
            )
        ),
        "data_as_of": gov.get("pledge_as_of"),
        "source": gov.get("pledge_source"),
    })
    # ③ 财务造假/重大诉讼 → 事件红/黄灯中识别负面关键词
    ev = gates.get("events") or {}
    event_unavailable = ev.get("light") is None or ev.get("verification_status") == "unavailable"
    event_evidence = [
        item for item in (ev.get("evidence") or []) if isinstance(item, dict)
    ]
    full_window_risks = ev.get("major_risk_evidence")
    if isinstance(full_window_risks, list):
        matched_events = [
            item for item in full_window_risks
            if isinstance(item, dict) and item.get("major_risk") is True
        ]
    else:
        # 兼容升级前缓存；只接受巨潮正式公告和明确重大风险词，不用 D 级媒体
        # 或“诉讼/处罚/冻结”等宽泛字样触发一票否决。
        matched_events = [
            item for item in event_evidence
            if item.get("source") == "巨潮公告" and any(
                keyword in str(item.get("title") or "")
                for keyword in _MAJOR_EVENT_VETO_KEYWORDS
            )
        ]
    event_triggered = None if event_unavailable else bool(matched_events)
    event_coverage = ev.get("coverage") or {}
    event_source = str(event_coverage.get("source") or "巨潮公告")
    event_lookback = int(event_coverage.get("lookback_days") or 60)
    event_as_of = event_coverage.get("newest_date")
    if event_triggered:
        event_basis = "；".join(
            f"{item.get('date') or '—'} {item.get('title') or '重大风险公告'}"
            for item in matched_events[:2]
        )
    elif event_triggered is False:
        event_basis = (
            f"近{event_lookback}日{event_source}标题已核验，"
            "未匹配财务造假、重大诉讼或监管风险关键词"
        )
    else:
        event_basis = "公告/事件来源不可用，无法核验"
    items.append({
        "rule": "财务造假/重大诉讼/监管风险",
        "triggered": event_triggered,
        "status": "unverifiable" if event_triggered is None else "triggered" if event_triggered else "clear",
        "basis": event_basis,
        "data_as_of": event_as_of,
        "source": event_source if not event_unavailable else None,
        "lookback_days": event_lookback,
        "evidence_scope": event_coverage.get("scope"),
        "matched_events": matched_events[:2],
    })
    # ④ 商誉占比过高：使用同报告期商誉/总资产；缺任一字段才是未知态。
    fin = (gates.get("fundamentals") or {}).get("metrics") or {}
    goodwill = _pct(fin.get("goodwill"))
    total_assets = _pct(fin.get("total_assets"))
    goodwill_official_evidence = fin.get("goodwill_official_evidence") or {}
    goodwill_reporting_status = str(fin.get("goodwill_reporting_status") or "")
    if goodwill_reporting_status == "inferred_blank_zero_unverified":
        goodwill = None
    goodwill_missing = [
        key for key, value in (("goodwill", goodwill), ("total_assets", total_assets))
        if value is None
    ]
    if not goodwill_missing and total_assets is not None and total_assets > 0:
        goodwill_ratio_pct = round(goodwill / total_assets * 100, 4)
        goodwill_triggered = goodwill_ratio_pct > GOODWILL_TOTAL_ASSET_VETO_PCT
        goodwill_status = "triggered" if goodwill_triggered else "clear"
        goodwill_basis = (
            f"报告期 {fin.get('period') or '—'}：商誉占总资产 {goodwill_ratio_pct:.4f}%"
            f"，策略阈值 {GOODWILL_TOTAL_ASSET_VETO_PCT:.1f}%"
            f"，{'超过阈值' if goodwill_triggered else '未超过阈值'}"
        )
    else:
        goodwill_ratio_pct = None
        goodwill_triggered = None
        goodwill_status = "unverifiable"
        goodwill_basis = (
            "同报告期第三方标准化商誉字段为空，尚无官方报告/XBRL零余额证据，当前无法核验"
            if goodwill_reporting_status == "inferred_blank_zero_unverified" else
            "缺少同报告期商誉或总资产，当前无法核验"
        )
        if total_assets is not None and total_assets <= 0:
            goodwill_missing.append("positive_total_assets")
    items.append({
        "rule": "商誉占比过高",
        "triggered": goodwill_triggered,
        "status": goodwill_status,
        "basis": goodwill_basis,
        "threshold_pct": GOODWILL_TOTAL_ASSET_VETO_PCT,
        "goodwill": goodwill,
        "total_assets": total_assets,
        "goodwill_ratio_pct": goodwill_ratio_pct,
        "period": fin.get("period"),
        "data_as_of": fin.get("data_as_of"),
        "source": fin.get("balance_sheet_source"),
        "source_url": goodwill_official_evidence.get("source_url"),
        "source_sha256": goodwill_official_evidence.get("pdf_sha256"),
        "source_page": goodwill_official_evidence.get("page"),
        "goodwill_reporting_status": goodwill_reporting_status or None,
        "missing_fields": goodwill_missing,
    })
    # ⑤ 大额成交净额代理观察：无法识别真实资金主体，不参与硬否决。
    # 按交易日去重，只取每个交易日最后一版审计快照。规则名保持不变以兼容已有 API。
    history = [row for row in (fund_history or []) if _pct(row.get("main_net")) is not None][-20:]
    history_sources = sorted({str(row.get("source")) for row in history if row.get("source")})
    history_methods = sorted({str(row.get("method")) for row in history if row.get("method")})
    history_units = sorted({str(row.get("unit")) for row in history if row.get("unit")})
    history_as_of = max((str(row.get("as_of")) for row in history if row.get("as_of")), default=None)
    negative_streak = 0
    for row in reversed(history):
        if float(row["main_net"]) >= 0:
            break
        negative_streak += 1
    if len(history) >= 20:
        triggered = negative_streak >= 20
        status = "observation_triggered" if triggered else "observation_clear"
        basis = (f"大额成交净额代理已核验 {history[0].get('trade_date')} 至 {history[-1].get('trade_date')} "
                 f"共20个交易日：{'全部为净流出' if triggered else f'当前连续净流出 {negative_streak} 日，未达20日'}")
    elif history and negative_streak < len(history):
        triggered = False
        status = "observation_clear"
        basis = f"大额成交净额代理当前连续净流出 {negative_streak} 日，已核验的近期交易日存在非净流出"
    else:
        triggered = None
        status = "observation_unavailable"
        basis = (f"大额成交净额代理仅有 {len(history)} 个不同交易日"
                 + (f"（{history[0].get('trade_date')} 至 {history[-1].get('trade_date')}）" if history else "")
                 + "，不足20日，保持不可用观察")
    items.append({"rule": "主力连续20日净流出", "triggered": triggered,
                  "status": status, "basis": basis, "observed_days": len(history),
                  "negative_streak": negative_streak, "required_days": 20,
                  "decision_effect": "risk_review_only",
                  "proxy_identity": "single_trade_amount_ge_1m_not_identifiable_main_force",
                  "source": "；".join(history_sources) or None,
                  "method": "；".join(history_methods) or "逐交易日大额成交净额代理正负连续性",
                  "unit": "；".join(history_units) or None,
                  "as_of": history_as_of})
    # ⑥ 布林下轨 + RSI低位只能描述技术弱势；当前没有成交量
    # 证据，不得冒充“放量破位”或参与硬否决。规则名保持不变以兼容已有 API。
    boll = None
    for lens in (snap.get("lenses") or []) if snap else []:
        if lens.get("name") == "布林带位置" and lens.get("state") == "below":
            boll = "跌破布林下轨"
    rsi_low = None
    for lens in (snap.get("lenses") or []) if snap else []:
        if lens.get("name") == "RSI14" and lens.get("state") == "low":
            rsi_low = "RSI 低位"
    technical_available = bool(snap and snap.get("lenses"))
    if not technical_available:
        items.append({
            "rule": "跌破关键支撑且放量",
            "triggered": None,
            "status": "observation_unavailable",
            "basis": "缺少已审计日线技术快照，技术弱势观察不可用",
            "decision_effect": "risk_review_only",
            "proxy_identity": "bollinger_below_and_rsi_low_without_volume_confirmation",
        })
    elif boll and rsi_low:
        items.append({"rule": "跌破关键支撑且放量", "triggered": True,
                      "status": "observation_triggered",
                      "basis": f"技术弱势观察：{boll} + {rsi_low}；未核验放量，不触发否决",
                      "decision_effect": "risk_review_only",
                      "proxy_identity": "bollinger_below_and_rsi_low_without_volume_confirmation"})
    else:
        items.append({
            "rule": "跌破关键支撑且放量",
            "triggered": False,
            "status": "observation_clear",
            "basis": "已审计技术快照未同时触发布林下轨与RSI低位观察",
            "decision_effect": "risk_review_only",
            "proxy_identity": "bollinger_below_and_rsi_low_without_volume_confirmation",
        })
    for item in items:
        if "status" not in item:
            item["status"] = "triggered" if item.get("triggered") is True else "clear"
    hard_items = [i for i in items if i.get("decision_effect") != "risk_review_only"]
    vetoed = any(i.get("triggered") is True for i in hard_items)
    unverifiable = [i for i in hard_items if i.get("status") == "unverifiable"]
    return {
        "vetoed": vetoed,
        "items": items,
        "reasons": [i["basis"] for i in hard_items if i.get("triggered") is True],
        "verification_complete": not unverifiable,
        "unverified_rules": [i["rule"] for i in unverifiable],
        "honesty": "硬否决项采用触发/已核验通过/无法核验三态；大额成交净额与未含成交量的技术弱势组合仅作风险观察，不参与否决、评分、仓位或提醒",
    }


# ─────────────────────────────────────────────
# 9 段式规则化 AI 总结（无外部 AI，本地生成）
# ─────────────────────────────────────────────

def _summary_section(title: str, body: str) -> dict:
    return {"title": title, "body": body}


def _valuation_observation(data: dict | None) -> dict:
    """Return the actual PE/PB observation used by the valuation score.

    The current cross-section can be newer than the PIT fallback panel.  The
    narrative and dimension card must describe the same observation that was
    scored instead of claiming valuation is missing.
    """
    data = data or {}
    score_evidence = data.get("score_evidence") or {}
    scored_values = {"pe_ttm": None, "pb_mrq": None}
    for part in score_evidence.get("parts") or []:
        name = str(part.get("name") or "")
        if scored_values["pe_ttm"] is None and name.startswith("PE_TTM"):
            scored_values["pe_ttm"] = _pct(part.get("value"))
        elif scored_values["pb_mrq"] is None and name.startswith("PB"):
            scored_values["pb_mrq"] = _pct(part.get("value"))
    # Select an entire observation. Missing fields remain missing rather than
    # borrowing values or a timestamp from another period/source.
    if any(value is not None for value in scored_values.values()):
        values = {**scored_values, "as_of": score_evidence.get("as_of"),
                  "source": score_evidence.get("source"), "note": score_evidence.get("note")}
    else:
        values = {"pe_ttm": _pct(data.get("pe_ttm")), "pb_mrq": _pct(data.get("pb_mrq")),
                  "as_of": data.get("as_of"),
                  "source": data.get("source") or ("PIT面板" if data.get("as_of") else None),
                  "note": data.get("note")}
    if values["pe_ttm"] is None and values["pb_mrq"] is None:
        display = data.get("display_observation") or {}
        if display.get("role") == "public_observation_only":
            return dict(display)
    return values


def build_nine_part_summary(data: dict) -> dict:
    """基于真实聚合数据本地规则化生成 9 段式总结。诚实标注：非外部 AI."""
    q = data.get("quote") or {}
    name = q.get("name") or data.get("name") or data.get("code", "")
    meta = _load_stock_meta().get(data.get("code", ""), {})
    profile = data.get("profile") or {}
    score = data.get("composite") or {}
    veto = data.get("veto") or {}
    snap = data.get("technical") or {}
    fund = data.get("funds") or {}
    gate_summary = profile.get("gate_summary") or {}
    metrics = ((profile.get("gates") or {}).get("fundamentals") or {}).get("metrics") or {}
    pit = _valuation_observation(data.get("valuation_data"))
    price = q.get("price")
    chg = q.get("change_pct")
    industry = meta.get("industry") or "—"

    # ① 公司概览
    valuation_bits = []
    if pit.get("pe_ttm") is not None:
        valuation_bits.append(f"PE_TTM={_fmt(pit.get('pe_ttm'))}")
    if pit.get("pb_mrq") is not None:
        valuation_bits.append(f"PB={_fmt(pit.get('pb_mrq'))}")
    valuation_intro = (
        f"估值来源 {pit.get('source') or '估值数据'} as_of {pit.get('as_of') or '—'}："
        + "，".join(valuation_bits) + "。"
        if valuation_bits else ""
    )
    s1 = (
        f"{name}（{data.get('code','')}），行业：{industry}。现价 {_fmt(price)}，"
        + (f"涨跌 {chg:+.2f}%。" if chg is not None else "行情不可得。")
        + valuation_intro
        + " 数据来源：腾讯行情 / 新浪统一数据层 / PIT 基本面面板。"
    )

    # ② 核心竞争力（护城河）
    gm = _pct(metrics.get("gross_margin"))
    roe = _pct(metrics.get("roe"))
    moat = []
    if gm is not None:
        moat.append(f"毛利率 {_fmt(gm,1)}%（{'较高，具备定价能力' if gm >= 35 else '一般' if gm >= 20 else '偏低，竞争力待核验'}）")
    if roe is not None:
        moat.append(f"ROE {_fmt(roe,1)}%（{'优秀' if roe >= 15 else '稳健' if roe >= 8 else '偏弱'}）")
    s2 = ("；".join(moat) if moat else "毛利率/ROE 数据不足，护城河无法从本机数据核验。") + \
         " 核心竞争力判断基于财务质量指标近似，不构成深度研究。"

    # ③ 成长逻辑
    rev_yoy = _pct(metrics.get("revenue_yoy"))
    np_yoy = _pct(metrics.get("np_yoy"))
    if rev_yoy is None and np_yoy is None:
        s3 = "营收/净利同比数据缺失，成长逻辑暂无法从本机数据核验。"
    else:
        parts_ = []
        if rev_yoy is not None:
            parts_.append(f"营收同比 {_fmt(rev_yoy,1)}%")
        if np_yoy is not None:
            parts_.append(f"净利同比 {_fmt(np_yoy,1)}%")
        s3 = "；".join(parts_) + "。" + (" 呈扩张态势。" if (rev_yoy or 0) > 10 and (np_yoy or 0) > 0 else
                                        " 呈收缩/停滞。" if (rev_yoy or 0) < -10 else " 增长平稳。")

    # ④ 财务深度
    fin_bits = []
    if _pct(metrics.get("net_profit")) is not None:
        fin_bits.append(f"净利 {_fmt(metrics.get('net_profit'),1)}亿")
    if _pct(metrics.get("debt_ratio")) is not None:
        fin_bits.append(f"负债率 {_fmt(metrics.get('debt_ratio'),1)}%")
    if _pct(metrics.get("ocf_ps")) is not None:
        fin_bits.append(f"每股经营现金流 {_fmt(metrics.get('ocf_ps'),2)}")
    s4 = "；".join(fin_bits) + "。" if fin_bits else "财务深度指标缺失（同花顺财务摘要未返回）。" + \
         " 现金流/负债/净利来自最新财报，带披露时点，不构成预测。"

    # ⑤ 估值判断
    pe, pb = pit.get("pe_ttm"), pit.get("pb_mrq")
    if pe is None and pb is None:
        s5 = "实时横截面与 PIT 面板均无该股 PE/PB，估值判断无法给出（数据不足）。"
    elif pit.get("role") == "public_observation_only":
        s5 = (
            f"公开估值观察：PE_TTM {_fmt(pe,2)}，PB {_fmt(pb,2)}。"
            f"来源：{pit.get('source') or '公开估值'}，数据日期 {pit.get('as_of') or '—'}。"
            + (pit.get("note") or "历史PIT与横截面分位缺失，不纳入评分或作高低估判断。")
        )
    else:
        val_bits = []
        if pe is not None:
            val_bits.append(f"PE_TTM {_fmt(pe,1)}（{'偏低' if pe < 20 else '合理' if pe < 40 else '偏高'}）")
        if pb is not None:
            val_bits.append(f"PB {_fmt(pb,2)}（{'偏低' if pb < 2 else '合理' if pb < 5 else '偏高'}）")
        s5 = (
            "；".join(val_bits)
            + f"。来源：{pit.get('source') or '估值数据'}，as_of {pit.get('as_of') or '—'}。"
        )

    # ⑥ 资金筹码
    net = fund.get("net_yi")
    turn = fund.get("turnover")
    if net is None and turn is None:
        s6 = "资金流快照无该股数据，资金筹码无法核验。"
    else:
        bits6 = []
        if net is not None:
            bits6.append(f"第三方资金净额观察 {_fmt(net,2)}亿（{'净流入' if net >= 0 else '净流出'}）")
        if turn is not None:
            bits6.append(f"换手 {_fmt(turn,2)}%（{'活跃' if 1.5 <= turn <= 15 else '清淡/过热'}）")
        s6 = "；".join(bits6) + "。数据为当日资金流快照（as_of 见快照），不构成资金持续性证据。"

    # ⑦ 技术研判
    if snap and snap.get("status") != "insufficient_history" and snap.get("lenses"):
        t_bits = []
        for lens in snap["lenses"]:
            t_bits.append(f"{lens.get('name')}：{lens.get('value')}（{lens.get('state')}）")
        s7 = "；".join(t_bits[:4]) + "。技术信号仅观察参考，不自动下单。"
    else:
        s7 = "历史K线不足，技术研判暂不可用。"

    # ⑧ 风险提示
    risk_bits = []
    for gname, glabel in (("fundamentals", "基本面"), ("governance", "治理"), ("events", "事件"), ("tradeable", "交易")):
        lg = gate_summary.get(gname)
        if lg == "red":
            risk_bits.append(f"{glabel}红灯")
        elif lg == "yellow":
            risk_bits.append(f"{glabel}黄灯")
    if veto.get("vetoed"):
        risk_bits.append("一票否决触发")
    if not risk_bits:
        s8 = (
            "本机可核验风险维度（ST/质押/负债/事件）未见红灯；"
            "实控人/高管/审计意见结构化字段尚未接入，问询、处罚与诉讼由巨潮公告事件闸门单独核验。"
        )
    else:
        s8 = "；".join(risk_bits) + "。风险提示基于本机已有数据，治理深度维度缺失，如实标注。"

    # ⑨ 综合评级
    comp = score.get("composite")
    rating = score.get("rating")
    research_band = score.get("research_band")
    gap = score.get("data_gap")
    conf = score.get("confidence")
    if comp is None:
        s9 = "综合评分数据不足，无法评级。"
    else:
        veto_note = "；一票否决生效，排除新开仓；已有持仓需进入独立风险复核" if veto.get("vetoed") else ""
        gap_note = ("；基本面数据缺口，评级上限受限" if gap == "critical"
                    else "；公司侧数据缺1维，评级上限中性" if gap == "moderate" else "")
        s9 = f"综合评分 {comp:.1f}/100，{research_band or '研究分档未知'}（分档仅作研究排序，不是买卖建议）{veto_note}{gap_note}。" \
             + " 权重：成长22%+质量22%+估值16%+风险20%+已完成K线技术20%；盘中条件与第三方资金流不进入个股总分；缺失维度不计入（权重归一化）。" \
             + (f" 数据置信度 {conf}。" if conf else "")

    return {
        "segments": [
            _summary_section("公司概览", s1),
            _summary_section("核心竞争力（护城河）", s2),
            _summary_section("成长逻辑", s3),
            _summary_section("财务深度", s4),
            _summary_section("估值判断", s5),
            _summary_section("资金筹码", s6),
            _summary_section("技术研判", s7),
            _summary_section("风险提示", s8),
            _summary_section("综合评级", s9),
        ],
        "generator": "本地规则化生成（无外部 AI 调用）",
        "honesty": "所有字段来自真实数据源，缺失项如实标注'数据不足'，不编造。",
    }


# ─────────────────────────────────────────────
# 主入口：聚合工作台数据
# ─────────────────────────────────────────────

def build_workstation(code: str, quote_obj=None, candles_daily=None,
                      profile=None, fund=None, snap=None,
                      intraday=None, timing_score: float | None = None,
                      display_observations: dict | None = None) -> dict:
    """聚合工作台全部数据。参数均可选：外部已取好的数据直接传入（避免重复拉取），
    未传的用默认路径惰性补取。返回完整 payload 供 /api/workstation 消费。

    timing_score：盘中条件匹配分（0-100）。产品质量评分调用方传入中性 50 基线；
    实时值只用于入场复核，不应随页面打开时点改写股票质量。

    诚实验证：行情/因子/估值全部带来源与 as_of；失败维度标 degraded。
    """
    t0 = time.time()
    meta = _load_stock_meta().get(code, {})
    pit = _load_pit_latest().get(code)
    if fund is not None:
        raw_net_yi = fund.get("raw_net_yi")
        normalized_net_yi = fund.get("normalized_net_yi", fund.get("net_yi"))
        if normalized_net_yi is None and raw_net_yi is not None:
            normalized_net_yi = normalize_fund_flow_yi(raw_net_yi)
        fund = {
            **fund,
            "net_yi": normalized_net_yi,
            "raw_net_yi": raw_net_yi,
            "normalized_net_yi": normalized_net_yi,
            "magnitude_scale": FLOW_MAGNITUDE_SCALE,
            "unit": "亿元（normalized）",
            "role": "research_observation_only",
            "decision_effect": "none",
            "decision_eligible": False,
        }
    fund_decision_eligible = False

    q = None
    if quote_obj is not None:
        q = {
            "name": getattr(quote_obj, "name", None),
            "price": getattr(quote_obj, "price", None),
            "change_pct": getattr(quote_obj, "change_pct", None),
            "open": getattr(quote_obj, "open_price", None),
            "high": getattr(quote_obj, "high", None),
            "low": getattr(quote_obj, "low", None),
            "prev_close": getattr(quote_obj, "previous_close", None),
            "amount": getattr(quote_obj, "amount", None),
            "data_time": getattr(quote_obj, "data_time", None),
            "source": getattr(quote_obj, "source", "未知"),
            "freshness": getattr(quote_obj, "freshness", "unknown"),
        }

    # 七维画像（基础信息/财务健康/估值/股东资金/机构研报/公告事件/技术筹码）
    profile = profile or {}
    gates = profile.get("gates") or {}
    online_fin_metrics = (gates.get("fundamentals") or {}).get("metrics") or {}
    archived_fin_metrics = _archived_financial_metrics(code)
    profile_sources = profile.get("data_sources") or {}
    fin_metrics = _select_financial_record(
        archived_fin_metrics,
        online_fin_metrics,
        profile_sources.get("financials") or {},
    )
    if fin_metrics and not gates.get("fundamentals"):
        gates = {**gates, "fundamentals": {
            "light": None,
            "metrics": fin_metrics,
            "reasons": ["本机归档基本面已接入；未接入的治理字段不作推断"],
        }}
    gov_metrics = (gates.get("governance") or {}).get("metrics") or {}
    price_risk = _price_risk_metrics(candles_daily)
    research_reports = list(profile.get("research_reports") or [])
    chip_distribution = profile.get("chip_distribution")
    valuation_score = score_valuation(code, pit)
    display_observations = display_observations or {}
    valuation_data = {**(pit or {}), "score_evidence": valuation_score,
                      "display_observation": display_observations.get("valuation")}
    valuation_observation = _valuation_observation({
        **valuation_data,
    })
    display_technical = (
        snap if snap and snap.get("lenses") else
        display_observations.get("technical") or snap or {}
    )

    # 七维画像与一票否决必须消费同一个历史序列，不能让前者继续停留在旧的当日快照。
    fund_history = _load_veto_fund_flow_history(code, 20)
    veto = one_vote_veto(gates, snap, pit, fund_history)
    fund_flow_veto = next(
        (item for item in veto.get("items", []) if item.get("rule") == "主力连续20日净流出"),
        {},
    )
    verified_fund_window = _verified_sina_fund_flow_window(fund_history, fund_flow_veto)
    if fund is not None:
        history_sources = sorted({str(row.get("source")) for row in fund_history if row.get("source")})
        history_methods = sorted({str(row.get("method")) for row in fund_history if row.get("method")})
        fund = {**fund, "history": fund_history,
                "history_days": len(fund_history),
                "history_source": "；".join(history_sources) or "quant_data 本机审计库（每交易日最后版本）",
                "history_method": "；".join(history_methods) or "每交易日最后审计版本"}

    dims_profile = {
        "basic": {
            "name": q.get("name") if q else (profile.get("name") or code),
            "code": code, "industry": meta.get("industry") or "—",
            "price": q.get("price") if q else None,
            "board": "创业板/科创板" if code[:3] in ("300", "301", "688", "689") else "主板",
            "data_time": q.get("data_time") if q else None,
        },
        "financial_health": {
            "period": fin_metrics.get("period"),
            "metrics": {k: fin_metrics.get(k) for k in
                        ("revenue", "net_profit", "revenue_yoy", "np_yoy", "deducted", "gross_margin", "net_margin",
                         "roe", "debt_ratio", "ocf_ps", "eps", "current_ratio", "goodwill", "total_assets",
                         "goodwill_ratio_pct")
                        if fin_metrics.get(k) is not None},
            "data_as_of": fin_metrics.get("data_as_of"),
            "observed_at": fin_metrics.get("observed_at"),
            "cashflow_to_profit": fin_metrics.get("ocf_to_net_profit"),
            "goodwill_source": fin_metrics.get("balance_sheet_source"),
            "note": fin_metrics.get("source") or ("来源：同花顺财务摘要（PIT 披露口径）" if fin_metrics else "财务数据缺失"),
        },
        "valuation": {
            **valuation_observation,
            "note": valuation_observation.get("note") or "实时与 PIT 估值均无数据",
        },
        "shareholder_funds": {
            "status": (
                "complete" if verified_fund_window
                else "partial" if any(gov_metrics.get(key) is not None for key in
                                       ("max_total_pledge_pct", "max_holder_pledge_pct"))
                else "unavailable"
            ),
            "max_total_pledge_pct": gov_metrics.get("max_total_pledge_pct"),
            "max_holder_pledge_pct": gov_metrics.get("max_holder_pledge_pct"),
            "pledge_as_of": gov_metrics.get("pledge_as_of"),
            "pledge_source": gov_metrics.get("pledge_source"),
            "fund_net_yi": (
                verified_fund_window["fund_net_yi"] if verified_fund_window
                else fund.get("normalized_net_yi") if fund_decision_eligible else None
            ),
            "fund_observed_normalized_yi": fund.get("normalized_net_yi") if fund else None,
            "fund_raw_net_yi": fund.get("raw_net_yi") if fund else None,
            "fund_magnitude_scale": fund.get("magnitude_scale") if fund else None,
            "turnover": fund.get("turnover") if fund_decision_eligible else None,
            "freshness_status": fund.get("freshness_status") if fund else "unavailable",
            "data_staleness_days": fund.get("data_staleness_days") if fund else None,
            "decision_eligible": fund_decision_eligible,
            **(verified_fund_window or {
                "history_verified": False,
                "window_days": len(fund_history),
                "continuous_outflow_status": "observation_unavailable",
            }),
            "note": (
                "质押优先中国结算周度官方行，官方不可达时才使用东财明确降级证据；新浪大额成交净额为单笔成交额代理，无法识别真实资金主体；仅作风险观察，不纳入评分、否决、买点、仓位或提醒"
                if verified_fund_window else
                "新浪大额成交净额20交易日同源序列未通过完整性校验；保持不可用观察，不拼接当日快照"
            ),
        },
        "research": {
            "status": "available" if research_reports else "unavailable",
            "count": len(research_reports),
            "latest_date": research_reports[0].get("date") if research_reports else None,
            "reports": research_reports[:10],
            "note": (
                "来源：东方财富个股研报元数据；仅展示机构、评级、日期与原始PDF链接"
                if research_reports else "公开个股研报元数据暂不可用"
            ),
        },
        "announcements": {
            "counts": (gates.get("events") or {}).get("counts"),
            "evidence_level": (gates.get("events") or {}).get("evidence_level"),
            "reasons": (gates.get("events") or {}).get("reasons", [])[:3],
            "note": "来源：巨潮公告 + 中证网事件",
        },
        "technical_chips": {
            **display_technical,
            "status": display_technical.get("status") or "insufficient_history",
            "lenses": (display_technical.get("lenses") or [])[:5],
            "market_risk": display_technical.get("market_risk") or price_risk,
            "chip_distribution": chip_distribution,
            "note": (display_technical.get("note") or (
                "技术指标来源：腾讯日K本地计算；筹码为BaoStock真实日K/换手率的120日换手衰减估算，非股东真实持仓"
                if chip_distribution else "技术指标来源：腾讯日K本地计算；筹码估算数据暂不可用"
            )),
        },
    }

    fund_as_of = (
        fund.get("source_data_as_of") or fund.get("as_of")
        or fund.get("ts") or fund.get("date")
    ) if fund else None
    quote_as_of = q.get("data_time") if q else None

    # 六维评分（P1b 专业口径）+ 统一综合合成（含量化时机 30%）
    dims = {
        "growth": score_growth(fin_metrics),
        "quality": score_quality(fin_metrics),
        "valuation": valuation_score,
        "technical": score_technical(snap),
        "funds": score_funds(
            fund.get("normalized_net_yi") if fund_decision_eligible else None,
            fund.get("turnover") if fund_decision_eligible else None,
            q.get("amount") if q else None,
            fund_as_of=fund_as_of, amount_as_of=quote_as_of,
        ),
        "risk": score_risk(gates, fin_metrics, price_risk, meta.get("industry")),
    }
    # 合并档案五闸门否决（P1b 全站同步）：档案闸门红灯（财务/治理/估值/公告/可交易）触发时，
    # 个股页与 live-scan/今日机会保持同一“排除新开仓”判定；已有持仓是否卖出由独立持仓风险链决定。
    profile_vetoed = bool(profile.get("veto"))
    profile_veto_gates = list(profile.get("vetoed_gates") or [])
    if profile_vetoed:
        if not veto.get("vetoed"):
            veto = {**veto, "vetoed": True}
        items = list(veto.get("items") or [])
        items.append({
            "rule": f"档案五闸门否决（{'、'.join(profile_veto_gates) or '红灯闸门'}）",
            "triggered": True,
            "basis": f"档案闸门红灯：{'、'.join(profile_veto_gates) or '见档案闸门'}",
        })
        veto = {
            **veto,
            "items": items,
            "reasons": list(veto.get("reasons") or []) + profile_veto_gates,
            "veto_sources": ["一票否决(技术/治理)", "档案五闸门"],
        }
    last_candle_as_of = None
    if candles_daily:
        last = candles_daily[-1]
        last_candle_as_of = (
            last.get("time") if isinstance(last, dict) else getattr(last, "time", None)
        )
    financial_as_of = fin_metrics.get("data_as_of") or fin_metrics.get("period")
    composite = compute_composite(
        dims,
        timing_score=timing_score,
        veto=veto.get("vetoed"),
        as_of_by_dimension={
            "growth": financial_as_of,
            "quality": financial_as_of,
            "valuation": valuation_score.get("as_of"),
            "risk": financial_as_of,
            "technical": last_candle_as_of,
            "funds": fund_as_of,
            "timing": quote_as_of,
        },
    )
    composite = apply_rating_overrides(composite, veto)

    # 雷达图：六维 + 量化时机（专业评分组成部分）
    radar = {k: (d.get("score")) for k, d in dims.items()}
    if timing_score is not None:
        radar["timing"] = timing_score

    payload = {
        "code": code,
        "name": q.get("name") if q else (profile.get("name") or code),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "latency_s": round(time.time() - t0, 2),
        "quote": q,
        "profile": {"gate_summary": {k: g["light"] for k, g in gates.items()},
                    "gates": gates, "dims": dims_profile,
                    "company_score": profile.get("company_score"),
                    "veto_gates": profile.get("vetoed_gates", []),
                    "status": profile.get("status") or ("ready" if profile.get("ready") else "partial"),
                    "ready": bool(profile.get("ready")),
                    "generated_at": profile.get("generated_at"),
                    "data_sources": profile_sources,
                    "refresh_endpoint": profile.get("refresh_endpoint"),
                    "note": profile.get("note")},
        "composite": composite,
        "veto": veto,
        "radar": radar,
        "valuation_data": valuation_data,
        "funds": fund or {},
        "technical": snap or {"status": "insufficient_history"},
        "intraday": intraday,
        "candles_daily": candles_daily,  # 供前端画K线（串行化在 app 层做）
        "summary": build_nine_part_summary({
            "quote": q, "name": q.get("name") if q else (profile.get("name") or code),
            "code": code, "profile": profile, "composite": composite, "veto": veto,
            "technical": snap, "funds": fund,
            "valuation_data": valuation_data,
        }),
        "honesty": {
            "data_sources": {
                "quote": (q.get("source") if q else None),
                "valuation": (
                    f"{valuation_observation.get('source') or '估值源不可用'} as_of "
                    f"{valuation_observation.get('as_of') or '—'}"
                ),
                "profile": "同花顺/东财/巨潮（见档案 data_sources）",
                "funds": "最近审计资金流快照",
                "technical": display_technical.get("source") or "腾讯日K本地计算",
            },
            "auto_order": False,
            "note": "工作台仅研究展示，不自动下单；缺失维度如实标注'数据不足'",
        },
    }
    return payload


def serialize_candles(candles) -> list[dict]:
    """Candle 列表 → JSON 安全的 dict 列表（时间统一 ISO / YYYY-MM-DD）。"""
    out = []
    for c in candles:
        t = c.time
        if hasattr(t, "isoformat"):
            t = t.isoformat()
        out.append({
            "time": str(t),
            "open": c.open, "high": c.high, "low": c.low, "close": c.close,
            "volume": c.volume,
            "amount": c.amount,
        })
    return out
