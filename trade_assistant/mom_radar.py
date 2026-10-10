"""Read-only A-share event radar for the Mom dashboard.

The radar deliberately separates facts (quoted market data and linked
headlines) from a small, explainable keyword classification.  It never emits
orders or presents a news item as a trading instruction.
"""

from __future__ import annotations

import html
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable

import requests


EASTMONEY_INDEX_URL = (
    "https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2&"
    "secids=1.000001%2C0.399001%2C0.399006&fields=f12%2Cf14%2Cf2%2Cf3"
)
EASTMONEY_SECTOR_URL = (
    "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=12&po=1&np=1&"
    "fltt=2&invt=2&fid=f3&fs=m%3A90%2Bt%3A2&fields=f12%2Cf14%2Cf2%2Cf3%2Cf62"
)
CHINA_SECURITIES_URL = "https://www.cs.com.cn/"
EASTMONEY_NEWS_URL = "https://finance.eastmoney.com/a/cywjh.html"

HEADERS = {"User-Agent": "Mozilla/5.0 (AshareMomRadar/1.0; +local-research)"}
NEWS_CACHE_SECONDS = 60
_HTTP = requests.Session()
# This local service should reach public sources directly.  Inherited desktop
# proxy variables are not a data source and can otherwise turn a healthy
# source into a misleading "no data" card.
_HTTP.trust_env = False

SECTOR_RULES = {
    "新能源": ("光伏", "锂", "储能", "新能源", "风电", "电池", "充电桩"),
    "半导体": ("芯片", "半导体", "算力", "AI", "人工智能", "服务器", "PCB"),
    "医药": ("医药", "创新药", "医疗", "疫苗", "药品"),
    "银行金融": ("银行", "券商", "保险", "降准", "降息", "利率"),
    "资源周期": ("原油", "黄金", "铜", "稀土", "煤炭", "有色", "镍", "钴"),
    "国防军工": ("军工", "国防", "卫星", "航天", "装备"),
    "消费家电": ("消费", "白酒", "家电", "旅游", "零售"),
}
POSITIVE_WORDS = ("增持", "回购", "中标", "订单", "增长", "盈利", "利好", "降准", "降息")
NEGATIVE_WORDS = ("减持", "立案", "处罚", "亏损", "下滑", "风险", "警示", "召回")
HIGH_IMPACT_WORDS = ("降准", "降息", "关税", "制裁", "战争", "原油", "汇率", "监管", "停牌")
MAJOR_ANNOUNCEMENT_WORDS = ("业绩预告", "重组", "增持", "减持", "处罚", "停牌", "复牌")
INDEX_FALLBACK_CODES = ("sh000001", "sz399001", "sz399006")
SECTOR_ETF_PROXIES = {
    "半导体": "sh512480",
    "医药": "sh512010",
    "券商": "sh512000",
    "新能源车": "sh515030",
    "军工": "sh512660",
    "消费": "sh512690",
}

_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": None}
_LOCK = threading.Lock()


def _fetch(url: str, timeout: int = 8) -> str:
    response = _HTTP.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    # Some Chinese news pages omit a charset header and requests defaults to
    # ISO-8859-1.  Decode from the response's detected encoding so titles stay
    # readable rather than silently turning into mojibake.
    return response.content.decode(response.apparent_encoding or response.encoding or "utf-8", errors="replace")


def _strip_markup(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(re.sub(r"\s+", " ", value)).strip()


def _is_a_share_relevant(title: str) -> bool:
    words = (
        "a股", "上市", "证券", "股市", "资本市场", "ipo", "业绩", "财报", "基金",
        "监管", "央行", "汇率", "利率", "经济", "公司", *POSITIVE_WORDS,
        *NEGATIVE_WORDS, *HIGH_IMPACT_WORDS,
        *(word for sector_words in SECTOR_RULES.values() for word in sector_words),
    )
    lower = title.lower()
    return any(word.lower() in lower for word in words)


def _headline_rows(source: str, source_url: str, markup: str, limit: int = 14) -> list[dict[str, str]]:
    """Extract linked titles only; full articles are never copied or stored."""
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    pattern = re.compile(r"<a(?P<attrs>[^>]*)>(?P<body>.*?)</a>", re.I | re.S)
    for match in pattern.finditer(markup):
        attrs = match.group("attrs")
        href = re.search(r'''\bhref\s*=\s*["']([^"']+)["']''', attrs, re.I)
        title_attr = re.search(r'''\btitle\s*=\s*["']([^"']+)["']''', attrs, re.I)
        if not href:
            continue
        title = _strip_markup((title_attr.group(1) if title_attr else "") or match.group("body"))
        url = html.unescape(href.group(1))
        if not title or len(title) < 6 or title in seen or not _is_a_share_relevant(title):
            continue
        if url.startswith("/"):
            root = re.match(r"https?://[^/]+", source_url)
            url = (root.group(0) if root else source_url) + url
        if not url.startswith("http"):
            continue
        seen.add(title)
        rows.append({"title": title[:100], "url": url, "source": source})
        if len(rows) >= limit:
            break
    return rows


def _classify(title: str) -> dict[str, Any]:
    lower = title.lower()
    sectors = [name for name, words in SECTOR_RULES.items() if any(word.lower() in lower for word in words)]
    positive = sum(word.lower() in lower for word in POSITIVE_WORDS)
    negative = sum(word.lower() in lower for word in NEGATIVE_WORDS)
    score = positive - negative
    impact = "偏多" if score > 0 else "偏空" if score < 0 else "待确认"
    priority = "高" if any(word.lower() in lower for word in HIGH_IMPACT_WORDS) else ("中" if sectors or score else "低")
    return {
        "impact": impact,
        "priority": priority,
        "sectors": sectors or ["全市场观察"],
        "reason": "关键词初筛，需人工查看原文确认；不构成买卖建议。",
    }


def _market_summary(indices: list[dict[str, Any]], sectors: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, str]:
    changes = [float(item.get("change_pct") or 0) for item in indices]
    index_average = sum(changes) / len(changes) if changes else 0.0
    event_score = sum(1 if item["impact"] == "偏多" else -1 if item["impact"] == "偏空" else 0 for item in events)
    leader = sectors[0]["name"] if sectors else "板块数据暂不可用"
    if index_average >= 0.6 and event_score >= 0:
        state, action = "结构偏暖", "可在上午完成核对后，仅观察强势板块；不追涨。"
    elif index_average <= -0.6 or event_score <= -3:
        state, action = "风险偏高", "今天以持仓风控和观察为主，暂不新增人工买入。"
    else:
        state, action = "分化观望", "指数与热点分化，等待个股、新闻与量能三项同时确认。"
    return {
        "state": state,
        "action": action,
        "leader": leader,
        "method": "指数实时涨跌 + 热门板块榜 + 新闻标题关键词的可解释汇总，不预测收益。",
    }


def _request_json(url: str, getter: Callable[[str], str]) -> dict[str, Any]:
    try:
        return __import__("json").loads(getter(url))
    except Exception:
        return {}


def _quote_fallback(quote_provider: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Use the app's live quote provider when a public board API is unavailable.

    Sector values are explicitly ETF proxies, never relabelled as board-index
    values.  That preserves continuity during a public-source outage without
    making up a sector quote.
    """
    if quote_provider is None:
        return [], []
    try:
        quotes = quote_provider.get_quotes([*INDEX_FALLBACK_CODES, *SECTOR_ETF_PROXIES.values()]) or {}
    except Exception:
        return [], []
    indices = []
    for code in INDEX_FALLBACK_CODES:
        quote = quotes.get(code)
        if quote is not None:
            indices.append({"code": code, "name": getattr(quote, "name", code), "price": getattr(quote, "price", None), "change_pct": getattr(quote, "change_pct", 0)})
    sectors = []
    for name, code in SECTOR_ETF_PROXIES.items():
        quote = quotes.get(code)
        if quote is not None:
            sectors.append({"code": code, "name": name, "change_pct": getattr(quote, "change_pct", 0), "net_flow": None})
    return indices, sorted(sectors, key=lambda item: float(item.get("change_pct") or 0), reverse=True)


def build_mom_radar_snapshot(
    *,
    getter: Callable[[str], str] = _fetch,
    now: datetime | None = None,
    quote_provider: Any = None,
) -> dict[str, Any]:
    """Build one live, source-linked radar snapshot with a short cache."""
    current = now or datetime.now()
    now_epoch = time.time()
    with _LOCK:
        if getter is _fetch and _CACHE["payload"] and now_epoch < _CACHE["expires_at"]:
            return _CACHE["payload"]

    index_raw = _request_json(EASTMONEY_INDEX_URL, getter)
    sector_raw = _request_json(EASTMONEY_SECTOR_URL, getter)
    indices = [
        {"code": row.get("f12", ""), "name": row.get("f14", "指数"), "price": row.get("f2"), "change_pct": row.get("f3", 0)}
        for row in (index_raw.get("data") or {}).get("diff", [])
    ]
    sectors = [
        {"code": row.get("f12", ""), "name": row.get("f14", "板块"), "change_pct": row.get("f3", 0), "net_flow": row.get("f62")}
        for row in (sector_raw.get("data") or {}).get("diff", [])
    ]
    fallback_indices, fallback_sectors = _quote_fallback(quote_provider)
    index_source = "东方财富实时指数"
    sector_source = "东方财富实时板块"
    if not indices and fallback_indices:
        indices = fallback_indices
        index_source = "本机实时行情"
    if not sectors and fallback_sectors:
        sectors = fallback_sectors
        sector_source = "实时板块ETF代理"

    raw_events: list[dict[str, str]] = []
    source_status: list[dict[str, str]] = []
    for source, url in (("中证网", CHINA_SECURITIES_URL), ("东方财富资讯", EASTMONEY_NEWS_URL)):
        try:
            raw_events.extend(_headline_rows(source, url, getter(url)))
            source_status.append({"name": source, "url": url, "status": "在线"})
        except Exception:
            source_status.append({"name": source, "url": url, "status": "暂不可用"})
    events: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for row in raw_events:
        if row["title"] in seen_titles:
            continue
        seen_titles.add(row["title"])
        events.append({**row, **_classify(row["title"]), "collected_at": current.strftime("%H:%M")})
        if len(events) >= 10:
            break

    payload = {
        "generated_at": current.strftime("%Y-%m-%d %H:%M:%S"),
        "refresh_after_seconds": NEWS_CACHE_SECONDS,
        "indices": indices,
        "sectors": sectors,
        "market_sources": {"indices": index_source, "sectors": sector_source},
        "events": events,
        "sources": source_status,
        "market": _market_summary(indices, sectors, events),
        "mode": "manual_confirmation_only",
        "disclaimer": "事件雷达仅汇总公开标题与行情，不自动下单；任何买入均需人工核对原文、K线、仓位和风险。",
    }
    if getter is _fetch:
        with _LOCK:
            _CACHE.update({"payload": payload, "expires_at": now_epoch + NEWS_CACHE_SECONDS})
    return payload


def build_major_event_notification(
    positions: list[dict[str, Any]],
    quotes: dict[str, dict[str, Any]],
    events: list[dict[str, Any]],
    announcements: dict[str, list[dict[str, Any]]] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Merge all qualifying afternoon events into at most one traceable notice."""
    current = now or datetime.now()
    announcements = announcements or {}
    triggered: list[dict[str, Any]] = []
    for position in positions:
        code = str(position.get("code") or "")
        name = str(position.get("name") or code)
        quote = quotes.get(code) or {}
        change = quote.get("change_pct")
        intraday_drop = quote.get("intraday_drop_pct")
        volume_ratio = quote.get("volume_ratio")
        if change is not None and float(change) <= -5:
            triggered.append({"type": "持仓跌幅", "title": f"{name}单日跌幅 {float(change):.2f}%",
                              "source": quote.get("source") or "行情源", "url": quote.get("url"), "impact": "利空"})
        if intraday_drop is not None and float(intraday_drop) <= -4:
            triggered.append({"type": "盘中急跌", "title": f"{name}盘中急跌 {float(intraday_drop):.2f}%",
                              "source": quote.get("source") or "行情源", "url": quote.get("url"), "impact": "利空"})
        if volume_ratio is not None and float(volume_ratio) >= 5:
            triggered.append({"type": "放量异动", "title": f"{name}量比 {float(volume_ratio):.1f}",
                              "source": quote.get("source") or "行情源", "url": quote.get("url"), "impact": "中性"})
        for item in announcements.get(code, []):
            title = str(item.get("title") or "")
            if any(word in title for word in MAJOR_ANNOUNCEMENT_WORDS):
                triggered.append({"type": "重大公告", "title": f"{name}：{title}",
                                  "source": item.get("source") or "巨潮公告",
                                  "url": item.get("url"), "impact": item.get("sentiment") or "中性"})
    for event in events:
        title = str(event.get("title") or "")
        if event.get("priority") == "高" or any(word in title for word in HIGH_IMPACT_WORDS):
            impact = {"偏多": "利多", "偏空": "利空"}.get(event.get("impact"), "中性")
            triggered.append({"type": "宏观事件", "title": title,
                              "source": event.get("source"), "url": event.get("url"), "impact": impact})
    unique = []
    seen = set()
    for item in triggered:
        key = (item["type"], item["title"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    afternoon = current.hour >= 13
    return {
        "major": bool(unique),
        "afternoon_quiet": afternoon and not unique,
        "notification_count": 1 if unique else 0,
        "notification": ({
            "title": f"重大事件合并提醒（{len(unique)}项）",
            "items": unique,
            "generated_at": current.isoformat(),
            "action": "仅人工复核，不自动下单",
        } if unique else None),
        "suppressed_regular_notifications": afternoon,
        "rule": "跌幅≥-5%/盘中急跌≥-4%/重大公告/宏观高影响/量比≥5；午后最多一条合并通知",
    }
