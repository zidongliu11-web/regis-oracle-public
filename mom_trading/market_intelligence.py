"""Source-labelled global shock radar and A-share market panorama.

This module is deliberately display-only: no output is consumed by the stock
scorer, portfolio allocator, broker, or order workflow.  A source failure is
returned as a failure; cached data is labelled as a snapshot, never as live.
"""
from __future__ import annotations

import math
import hashlib
import json
import logging
import re
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import subprocess
import time as _time
import certifi
import requests

from trade_assistant.market import MarketDataUnavailable, QuoteProvider
from trade_assistant.candles import CandleDataUnavailable, TencentCandleProvider
from .trading_calendar import audited_trading_day_age


SH_TZ = ZoneInfo("Asia/Shanghai")
_LOG = logging.getLogger(__name__)
_HTTP_HEADERS = {"User-Agent": "Ashare-Market-Intelligence/1.0"}
# 官网日历页需要完整浏览器 UA 才能放行（BLS/FOMC/BOJ/NBS 等）。
_HTML_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
_ALL_INDUSTRIES = (
    "农林牧渔", "石油石化", "煤炭", "有色金属", "电力设备", "基础化工", "钢铁", "电子", "家用电器",
    "食品饮料", "纺织服饰", "轻工制造", "医药生物", "公用事业", "交通运输", "房地产",
    "商贸零售", "社会服务", "综合", "建筑材料", "建筑装饰", "国防军工",
    "计算机", "传媒", "通信", "银行", "非银金融", "汽车", "机械设备", "环保", "美容护理",
)

# ── 全球资产注册表：每资产显式列出可达来源，第一可用者胜出 ────────────
# 2026-08-18 实测：Yahoo Finance 对本网络返回 403/429 全线封禁；CoinGecko 直连超时。
# 指数改用 东方财富 push2delay（100.*，延迟源，含日 K 走势线）+ 腾讯（hkHSTECH）；
# 商品用 新浪期货 hf_*；汇率用 新浪外汇 fx_*；美债/VIX 用 FRED 每日序列；
# 加密用 Binance Vision（data-api.binance.vision，可直连）主源 + Gate.io 兜底。
# providers 为空 = 当前网络无可达公开源，显式占位（expected_unavailable），不伪造数字。
_GLOBAL_ASSETS = (
    ("dow", "道琼斯", "美股", 1.0, (("eastmoney", "100.DJIA"),)),
    ("sp500", "标普500", "美股", 1.0, (("eastmoney", "100.SPX"),)),
    ("nasdaq", "纳斯达克", "美股", 1.0, (("eastmoney", "100.NDX"),)),
    ("russell2000", "罗素2000", "美股", 1.0, ()),
    ("dax", "德国DAX", "欧股", 1.0, (("eastmoney", "100.GDAXI"),)),
    ("ftse100", "英国富时100", "欧股", 1.0, (("eastmoney", "100.FTSE"),)),
    ("cac40", "法国CAC40", "欧股", 1.0, (("eastmoney", "100.FCHI"),)),
    ("nikkei", "日经225", "亚太", 1.0, (("eastmoney", "100.N225"),)),
    ("kospi", "韩国KOSPI", "亚太", 1.0, (("eastmoney", "100.KS11"),)),
    ("hangseng", "恒生指数", "亚太", 1.0, (("eastmoney", "100.HSI"),)),
    ("hangsengtech", "恒生科技", "亚太", 1.0, (("tencent", "hkHSTECH"),)),
    ("dxy", "美元指数", "外汇", 0.3, (("eastmoney", "100.UDI"),)),
    ("usdcnh", "离岸人民币", "外汇", 0.3, (("sina_fx", "fx_susdcnh"),)),
    ("wti", "WTI原油", "大宗商品", 1.5, (("sina_hf", "hf_CL"),)),
    ("brent", "布伦特原油", "大宗商品", 1.5, (("sina_hf", "hf_OIL"),)),
    ("gold", "黄金", "大宗商品", 1.5, (("sina_hf", "hf_GC"),)),
    ("copper", "铜", "大宗商品", 1.5, (("sina_hf", "hf_CAD"),)),
    ("iron_ore", "铁矿石", "大宗商品", 1.5, ()),
    ("vix", "VIX恐慌指数", "波动率", 5.0, (("fred", "vix:VIXCLS"),)),
    ("bitcoin", "比特币", "加密资产", 5.0, (("crypto", "BTC"),)),
    ("ethereum", "以太坊", "加密资产", 5.0, (("crypto", "ETH"),)),
    ("us2y", "美国2年期国债", "美债收益率", 10.0, (("fred", "bond:DGS2"),)),
    ("us10y", "美国10年期国债", "美债收益率", 10.0, (("fred", "bond:DGS10"),)),
    ("ftsea50", "富时中国A50", "亚太", 1.0, ()),
)
_EASTMONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}
_EASTMONEY_UT = "b2884a393a59ad64002292a3e90d46a5"
_MARKET_FLOW_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
_NORTHBOUND_HISTORY_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
# push2 直连被服务器指纹拦截（RemoteDisconnected），push2delay 同管线延迟源可直连（2026-08-15 实测）。
_EASTMONEY_SECTORS_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
_EASTMONEY_FAST_NEWS_URL = "https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
_EASTMONEY_NEWS_COLUMNS_URL = "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
_EASTMONEY_ANNOUNCEMENTS_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
_SINA_LIVE_URL = "https://zhibo.sina.com.cn/api/zhibo/feed"
_SECTORS_FIELDS = "f2,f3,f12,f14,f62,f66,f72,f78,f84,f104,f105,f124,f184"
_SECTORS_CACHE_SECONDS = 60
_NEWS_CACHE_SECONDS = 60
_NEWS_ITEM_LIMIT = 24
_NEWS_SUMMARY_MAX_CHARS = 240
_SOURCE_REQUEST_ATTEMPTS = 2
_SOURCE_REQUEST_TIMEOUT_SECONDS = 3
_SINGLEFLIGHT_WAIT_SECONDS = 30
_MARKET_SNAPSHOT_BUDGET_SECONDS = 12
_JINA_READER = "https://r.jina.ai/"
_INDUSTRY_MAP_PATH = Path(__file__).resolve().parent.parent / "research" / "industry_map.json"
_INDUSTRY_MAP_CACHE: dict[str, str] | None = None
# These are style/status labels in the local mapping, not comparable industries.
_NON_INDUSTRY_LABELS = {"其它行业", "次新股"}

# These keyword matches are reproducible attention filters, not claims that a
# story is economically material.  Every match is returned to the UI so a user
# can audit why a label was attached.
_NEWS_MATERIALITY_TERMS = (
    "降准", "降息", "加息", "关税", "制裁", "停牌", "退市", "立案", "处罚",
    "重大资产重组", "并购", "收购", "破产", "违约", "回购", "增持", "减持",
    "业绩预告", "业绩快报", "分红", "中标", "重大合同", "控制权变更",
)
_NEWS_A_SHARE_TERMS = (
    "A股", "沪深", "上证", "深证", "创业板", "科创板", "北交所", "证监会",
    "上交所", "深交所", "人民币", "中国央行", "中国人民银行",
)

_FLOW_PAGE_SCHEMA = {
    "version": "eastmoney_market_flow_page_v1",
    "columns": {
        "date": 0, "sh_close": 1, "sh_change_pct": 2,
        "sz_close": 3, "sz_change_pct": 4,
        "main_net": 5, "main_net_pct": 6, "super_large_net": 7,
    },
    "header_patterns": {
        0: r"日期", 1: r"上证.*收盘", 2: r"上证.*涨跌",
        3: r"深证.*收盘", 4: r"深证.*涨跌",
        5: r"主力.*(?:净流入|净额)", 6: r"主力.*(?:净占比|占比)",
        7: r"超大单.*(?:净流入|净额)",
    },
}

# The local file mixes several legacy/CSRC labels. Collapse every known label
# into one disclosed broad level before aggregation; an unknown label makes the
# whole ranking unavailable instead of silently mixing parent and child levels.
_INDUSTRY_LEVEL1_ALIASES = {
    "石油石化": "能源", "石油行业": "能源", "石油和天然气开采业": "能源",
    "煤炭": "能源", "煤炭行业": "能源",
    "有色金属": "原材料", "有色金属冶炼和压延加工业": "原材料", "钢铁": "原材料",
    "钢铁行业": "原材料", "基础化工": "原材料", "化工行业": "原材料",
    "农药化肥": "原材料", "化纤行业": "原材料", "塑料制品": "原材料",
    "橡胶和塑料制品业": "原材料", "化学原料和化学制品制造业": "原材料",
    "建筑材料": "原材料", "建筑建材": "原材料", "水泥行业": "原材料",
    "玻璃行业": "原材料", "陶瓷行业": "原材料", "非金属矿物制品业": "原材料",
    "机械设备": "工业", "机械行业": "工业", "仪器仪表": "工业", "纺织机械": "工业",
    "专用设备制造业": "工业", "建筑装饰": "工业", "交通运输": "工业",
    "公路桥梁": "工业", "船舶制造": "工业", "飞机制造": "工业", "国防军工": "工业",
    "汽车": "可选消费", "汽车制造": "可选消费", "汽车制造业": "可选消费",
    "摩托车": "可选消费", "家用电器": "可选消费", "家电行业": "可选消费",
    "电器行业": "可选消费", "电气机械和器材制造业": "可选消费", "家具行业": "可选消费",
    "纺织服饰": "可选消费", "纺织行业": "可选消费", "服装鞋类": "可选消费",
    "社会服务": "可选消费", "酒店旅游": "可选消费", "美容护理": "可选消费",
    "商贸零售": "可选消费", "商业百货": "可选消费", "物资外贸": "可选消费",
    "食品饮料": "日常消费", "食品行业": "日常消费", "酿酒行业": "日常消费",
    "农林牧渔": "日常消费",
    "医药生物": "医疗保健", "生物制药": "医疗保健", "医疗器械": "医疗保健",
    "研究和试验发展": "医疗保健",
    "银行": "金融", "非银金融": "金融", "金融行业": "金融", "货币金融服务": "金融",
    "资本市场服务": "金融", "其他金融业": "金融",
    "电子": "信息技术", "电子器件": "信息技术", "电子信息": "信息技术",
    "计算机": "信息技术", "软件和信息技术服务业": "信息技术",
    "计算机、通信和其他电子设备制造业": "信息技术",
    "通信": "通信服务", "传媒": "通信服务", "传媒娱乐": "通信服务",
    "电信、广播电视和卫星传输服务": "通信服务",
    "公用事业": "公用事业", "电力行业": "公用事业", "供水供气": "公用事业",
    "电力、热力生产和供应业": "公用事业", "环保": "公用事业", "环保行业": "公用事业",
    "电力设备": "工业", "发电设备": "工业",
    "房地产": "房地产", "开发区": "房地产",
    "轻工制造": "轻工制造", "造纸行业": "轻工制造", "印刷包装": "轻工制造",
    "综合": "综合", "综合行业": "综合",
}

# Rules are disclosed, deterministic, and observation-only until a separate
# preregistered historical validation establishes their hit rate.
_RULES = (
    {
        "id": "oil-up",
        "asset": "wti",
        "metric": "change_pct",
        "op": ">=",
        "threshold": 3.0,
        "effects": (("石油石化", "利好"), ("交通运输", "利空")),
        "strength": "中",
        "path": "直接成本与利润传导",
    },
    {
        "id": "dxy-up",
        "asset": "dxy",
        "metric": "change_pct",
        "op": ">=",
        "threshold": 0.5,
        "effects": (("银行", "利空"), ("非银金融", "利空")),
        "strength": "弱",
        "path": "汇率与跨境风险偏好传导",
    },
    {
        "id": "nasdaq-down",
        "asset": "nasdaq",
        "metric": "change_pct",
        "op": "<=",
        "threshold": -2.0,
        "effects": (("电子", "利空"), ("计算机", "利空"), ("通信", "利空")),
        "strength": "中",
        "path": "全球科技风险偏好传导",
    },
    {
        "id": "gold-up",
        "asset": "gold",
        "metric": "change_pct",
        "op": ">=",
        "threshold": 2.0,
        "effects": (("有色金属", "利好"),),
        "strength": "中",
        "path": "商品价格直接传导",
    },
    {
        "id": "vix-up",
        "asset": "vix",
        "metric": "absolute_change",
        "op": ">=",
        "threshold": 5.0,
        "effects": (("计算机", "利空"), ("电子", "利空"), ("传媒", "利空")),
        "strength": "中",
        "path": "风险偏好与高贝塔估值传导",
    },
)


def _now() -> datetime:
    return datetime.now(SH_TZ)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=SH_TZ) if parsed.tzinfo is None else parsed.astimezone(SH_TZ)


def _as_of_from_epoch(value: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), tz=SH_TZ)
    except (TypeError, ValueError, OSError):
        return None


def _health_for(
    as_of: datetime | None,
    error: str | None = None,
    *,
    daily: bool = False,
    calendar: str | None = None,
) -> tuple[str, int | None]:
    if error:
        return "upstream_error", None
    if as_of is None:
        return "missing", None
    age = int((_now() - as_of).total_seconds())
    if age < -60:
        return "upstream_error", age
    age = max(0, age)
    # 每日序列（FRED 美债/VIX）天然到下一交易日才更新：周末/假日滞后数天是正常状态，
    # 超过一周无更新才判上游异常。盘中源仍用 48h 上限。
    if age > (7 * 24 * 3600 if daily else 48 * 3600):
        if calendar == "a_share":
            try:
                session_age = audited_trading_day_age(as_of, now=_now())
            except (OSError, TypeError, ValueError):
                session_age = {"status": "unknown"}
            # A weekend or an audited exchange holiday must not turn the last
            # completed A-share observation into an upstream failure.
            if session_age.get("status") == "fresh":
                return "delayed", age
        return "upstream_error", age
    if daily:
        return ("verified" if age < 26 * 3600 else "delayed"), age
    return ("verified" if age < 300 else "delayed"), age


def _source(
    source: str,
    as_of: datetime | None,
    *,
    error: str | None = None,
    cached: bool = False,
    decision_eligible: bool = False,
    daily: bool = False,
    calendar: str | None = None,
) -> dict[str, Any]:
    health, latency = _health_for(as_of, error, daily=daily, calendar=calendar)
    if health == "upstream_error" and error is None:
        error = "source_future" if latency is not None and latency < -60 else "source_stale"
    return {
        "source": source,
        "as_of": _iso(as_of),
        "latency_seconds": latency,
        "health_status": health,
        "error": error,
        "cached_snapshot": cached,
        "health_calendar": calendar or ("elapsed_daily" if daily else "elapsed_continuous"),
        "participates_in_decision": decision_eligible,
    }


def _refresh_health(record: dict[str, Any], *, daily: bool = False) -> None:
    """Re-evaluate cached evidence against its immutable source timestamp."""
    error = record.get("error")
    if error in {"source_future", "source_stale"}:
        error = None
    calendar = record.get("health_calendar")
    health, latency = _health_for(
        _parse_iso(record.get("as_of")), error, daily=daily,
        calendar="a_share" if calendar == "a_share" else None,
    )
    record["health_status"], record["latency_seconds"] = health, latency
    record["error"] = error or (
        "source_future" if health == "upstream_error" and latency is not None and latency < -60
        else "source_stale" if health == "upstream_error"
        else None
    )


def _served_source(
    source: dict[str, Any], fetched_at: datetime, served_at: datetime, *, cached: bool,
) -> dict[str, Any]:
    """Apply the same immutable-data / acquisition / response-time contract."""
    result = dict(source)
    result.setdefault("data_as_of", result.get("as_of"))
    result["fetched_at"] = _iso(fetched_at)
    result["served_at"] = _iso(served_at)
    result["served_from_cache"] = cached
    result["cached_snapshot"] = cached
    return result


def _safe_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class EastmoneyFetchError(RuntimeError):
    """Only bounded transport categories, never URLs, parameters or response bodies."""

    def __init__(self, failures: list[str]) -> None:
        self.failures = tuple(failures[:_SOURCE_REQUEST_ATTEMPTS])
        super().__init__("eastmoney_unavailable:" + ",".join(self.failures))


class _SectorRows(list):
    """Rows and request failures from one page-collection invocation only."""

    def __init__(self, rows: list[dict[str, Any]], request_errors: list[dict[str, Any]]) -> None:
        super().__init__(rows)
        self.request_errors = list(request_errors)


def _source_request_failure(exc: Exception) -> str:
    if isinstance(exc, requests.HTTPError):
        status = getattr(exc.response, "status_code", None)
        return f"http_{status}" if type(status) is int and 400 <= status <= 599 else "http_error"
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.ConnectionError):
        return "connection"
    if isinstance(exc, (requests.exceptions.JSONDecodeError, ValueError)):
        return "invalid_payload"
    return "request_failed"


def _direct_eastmoney_json(url: str, params: dict[str, str]) -> dict[str, Any]:
    """Read Eastmoney without inheriting a workstation proxy that drops these hosts."""
    errors: list[str] = []
    for _ in range(_SOURCE_REQUEST_ATTEMPTS):
        session = requests.Session()
        try:
            session.trust_env = False
            response = session.get(
                url, params=params, headers=_EASTMONEY_HEADERS,
                timeout=_SOURCE_REQUEST_TIMEOUT_SECONDS,
                verify=certifi.where(),
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                return payload
            raise ValueError("payload_not_object")
        except Exception as exc:
            errors.append(_source_request_failure(exc))
        finally:
            session.close()
    raise EastmoneyFetchError(errors)


def _direct_get(url: str, *, headers: dict[str, str], timeout: float, **kwargs: object):
    """绕过工作站代理（127.0.0.1:17891）读公开财经源——该代理会丢这些主机（CONNECT
    一直等不到上游、15s 超时甚至无限阻塞）。实测直连 qt.gtimg.cn ~0.2s、hq.sinajs.cn
    ~0.1s（需 Referer）。与 _direct_eastmoney_json 同一依据：公网财经接口本就不需要代理。
    """
    session = requests.Session()
    try:
        session.trust_env = False
        return session.get(url, headers=headers,
                           timeout=min(timeout, _SOURCE_REQUEST_TIMEOUT_SECONDS),
                           verify=certifi.where(), **kwargs)
    finally:
        session.close()


def _tencent_symbol(code: str) -> str:
    return ("sh" if code.startswith(("5", "6")) else "bj" if code.startswith(("4", "8", "9")) else "sz") + code


_REFERENCE_GROUPS = (
    ("sh_main", "沪市主板代码段"), ("star", "科创板代码段"),
    ("sz_main", "深市主板代码段"), ("chinext", "创业板代码段"),
    ("bj", "北交所代码段"),
)


def _reference_group(code: str) -> str | None:
    """Syntactic observation buckets; never a listing or permission check."""
    if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
        return None
    if code.startswith(("688", "689")):
        return "star"
    if code.startswith(("600", "601", "603", "605")):
        return "sh_main"
    if code.startswith("30"):
        return "chinext"
    if code.startswith(("000", "001", "002", "003")):
        return "sz_main"
    if code.startswith(("4", "8", "920")):
        return "bj"
    return None


def _reference_frame_error(source: dict[str, Any]) -> str | None:
    """A later code list cannot describe the earlier quote-day population."""
    captured_raw, quote_raw = source.get("reference_universe_as_of"), source.get("earliest_as_of") or source.get("as_of")
    if not captured_raw or not quote_raw:
        return "reference_frame_time_unverified" if source.get("reference_universe_source") else None
    try:
        captured, quoted = datetime.fromisoformat(captured_raw), datetime.fromisoformat(quote_raw)
        if captured.tzinfo is None or quoted.tzinfo is None:
            return "reference_frame_time_unverified"
        if captured > quoted:
            return "reference_frame_captured_after_quotes"
    except (TypeError, ValueError):
        return "reference_frame_time_unverified"
    return None


def _quote_window_error(source: dict[str, Any]) -> str | None:
    first_raw, last_raw = source.get("earliest_as_of"), source.get("latest_as_of")
    if not first_raw and not last_raw and not source.get("reference_universe_source"):
        return None  # Legacy direct callers; the live reference source always declares a window.
    try:
        first, last = datetime.fromisoformat(first_raw), datetime.fromisoformat(last_raw)
        if first.tzinfo is None or last.tzinfo is None:
            return "quote_window_time_unverified"
        span = (last - first).total_seconds()
        if first.date() != last.date() or not 0 <= span <= 180:
            return "quote_window_not_coherent"
    except (TypeError, ValueError):
        return "quote_window_time_unverified"
    return None


def _linear_percentile(sorted_values: list[float], percentile: float) -> float:
    offset = (len(sorted_values) - 1) * percentile
    lower = math.floor(offset)
    upper = math.ceil(offset)
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (offset - lower)


def _market_structure(quotes: list[dict[str, Any]], source: dict[str, Any]) -> dict[str, Any]:
    """Separate, fail-closed breadth/distribution observation; no route authority."""
    base = {"status": "unavailable", "reason": "参考池与报价时点、代码段或分母未通过核验。",
            "groups": [], "distribution": None, "research_only": True,
            "participates_in_decision": False,
            "definition": "代码前缀分组，不等于交易所当日上市全集；涨跌幅分布和成交集中度只描述同一参考池。"}
    if not source.get("reference_universe_as_of") or not (source.get("earliest_as_of") or source.get("as_of")):
        return base
    frame_error = _reference_frame_error(source)
    if frame_error:
        return {**base, "status": "lookahead_reference" if frame_error == "reference_frame_captured_after_quotes" else "unavailable",
                "reason": "参考代码清单晚于报价，不能回填该日市场结构。" if frame_error == "reference_frame_captured_after_quotes" else base["reason"]}
    window_error = _quote_window_error(source)
    if window_error:
        return {**base, "status": "incoherent_quote_window",
                "reason": "逐证券报价最早与最晚时点未核或超过 180 秒，不构成同一市场横截面。"}
    expected = source.get("reference_universe_code_groups")
    if (not isinstance(expected, dict) or set(expected) != {key for key, _ in _REFERENCE_GROUPS}
            or any(type(value) is not int or value < 1 for value in expected.values())
            or sum(expected.values()) != source.get("coverage_expected")
            or source.get("coverage_returned") != len(quotes)
            or len(quotes) != source.get("coverage_expected")
            or source.get("partial_batch_errors") or source.get("timestamp_complete") is not True
            or source.get("timestamp_same_date") is not True
            or source.get("future_quote_count") != 0
            or source.get("duplicate_quote_count") != 0):
        return base
    health = source.get("health_status")
    archived = health == "upstream_error" and source.get("error") == "source_stale"
    if health not in {"verified", "delayed"} and not archived:
        return base
    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in _REFERENCE_GROUPS}
    seen: set[str] = set()
    for row in quotes:
        code = row.get("code")
        group = _reference_group(code)
        change = _safe_number(row.get("change_pct"))
        if group is None or code in seen or change is None:
            return base
        seen.add(code)
        grouped[group].append(row)
    if any(len(grouped[key]) != expected[key] for key in grouped):
        return base
    changes = sorted(float(row["change_pct"]) for row in quotes)
    groups = []
    for key, label in _REFERENCE_GROUPS:
        values = [float(row["change_pct"]) for row in grouped[key]]
        groups.append({"id": key, "label": label, "count": len(values),
                       "up": sum(value > .01 for value in values),
                       "down": sum(value < -.01 for value in values),
                       "flat": sum(-.01 <= value <= .01 for value in values),
                       "median_change_pct": round(statistics.median(values), 3)})
    amounts = [_safe_number(row.get("amount_yi")) for row in quotes]
    amount_complete = all(value is not None and value >= 0 for value in amounts)
    total_amount = sum(amounts) if amount_complete else 0
    top_count = max(1, math.ceil(len(quotes) * .05))
    distribution = {"count": len(changes), "median_change_pct": round(statistics.median(changes), 3),
                    "p10_change_pct": round(_linear_percentile(changes, .1), 3),
                    "p90_change_pct": round(_linear_percentile(changes, .9), 3),
                    "top_5pct_amount_share_pct": (round(sum(sorted(amounts, reverse=True)[:top_count]) / total_amount * 100, 3)
                                                    if total_amount > 0 else None),
                    "amount_observation_count": len(amounts) if amount_complete else sum(value is not None and value >= 0 for value in amounts),
                    "top_amount_stock_count": top_count}
    return {**base, "status": "archived" if archived else "observed",
            "reason": "报价已过期，仅作历史参考池观察。" if archived else "完整参考池当时观察；交易所全集覆盖仍未知。",
            "groups": groups, "distribution": distribution}


def _tencent_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{14}", value):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=SH_TZ)
    except ValueError:
        return None


def _tencent_market_quotes(codes: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Use Tencent's public batch quote protocol; the input code universe is disclosed."""
    if not codes:
        source = _source(
            "腾讯财经公开行情（参考代码清单批量）", None,
            error="empty_universe", calendar="a_share",
        )
        source.update({
            "coverage_scope": "requested_reference_universe",
            "reference_universe_source": "调用方提供的证券代码清单",
            "reference_universe_expected": 0,
            "reference_universe_returned": 0,
            "reference_coverage_ratio": None,
            "exchange_universe_expected": None,
            "exchange_coverage_ratio": None,
            "exchange_coverage_status": "unknown",
        })
        return [], source
    requested = {_tencent_symbol(code): code for code in codes}
    quotes: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    duplicate_quote_count = 0
    errors: list[str] = []
    deadline = _time.monotonic() + _MARKET_SNAPSHOT_BUDGET_SECONDS
    for start in range(0, len(codes), 800):
        if start and _time.monotonic() >= deadline:
            errors.append("deadline_exceeded")
            break
        symbols = [_tencent_symbol(code) for code in codes[start:start + 800]]
        try:
            response = _direct_get(
                "https://qt.gtimg.cn/q=" + ",".join(symbols),
                headers=_HTTP_HEADERS, timeout=_SOURCE_REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            for line in response.content.decode("gb18030", errors="replace").splitlines():
                match = re.match(r"v_([a-z0-9]+)=\"(.*)\";?", line)
                if match is None:
                    continue
                symbol, raw = match.groups()
                code = requested.get(symbol)
                fields = raw.split("~")
                if code is None or len(fields) < 47 or not fields[1]:
                    continue
                if symbol in seen_symbols:
                    duplicate_quote_count += 1
                    continue
                seen_symbols.add(symbol)
                quotes.append({
                    "code": code,
                    "name": fields[1],
                    "price": _safe_number(fields[3]),
                    "previous_close": _safe_number(fields[4]),
                    "bid_price_1": _safe_number(fields[9]),
                    "bid_volume_1": _safe_number(fields[10]),
                    "ask_price_1": _safe_number(fields[19]),
                    "ask_volume_1": _safe_number(fields[20]),
                    "change_pct": _safe_number(fields[32]),
                    "amount_yi": (_safe_number(fields[37]) / 10000 if _safe_number(fields[37]) is not None else None),
                    "turnover_pct": _safe_number(fields[38]),
                    "pe_ttm": _safe_number(fields[39]),
                    "float_market_cap_yi": _safe_number(fields[44]),
                    "market_cap_yi": _safe_number(fields[45]),
                    "pb": _safe_number(fields[46]),
                    # Tencent publishes the effective upper/lower price for
                    # this exact security and session.  Using those fields
                    # automatically covers ST, board and initial-listing
                    # exceptions; an empty pair means the limit is unknown or
                    # not applicable and is excluded from the count.
                    "limit_up_price": _safe_number(fields[47]) if len(fields) > 47 else None,
                    "limit_down_price": _safe_number(fields[48]) if len(fields) > 48 else None,
                    "as_of": _tencent_time(fields[30]),
                })
        except Exception as exc:
            errors.append(type(exc).__name__)
    as_of_values = [row["as_of"] for row in quotes if row.get("as_of") is not None]
    timestamp_complete = len(as_of_values) == len(quotes)
    timestamp_same_date = timestamp_complete and bool(as_of_values) and len({value.date() for value in as_of_values}) == 1
    future_cutoff = _now() + timedelta(seconds=60)
    future_quote_count = sum(value > future_cutoff for value in as_of_values)
    time_error = (
        "quote_timestamp_incomplete" if not timestamp_complete else
        "quote_cross_date" if not timestamp_same_date else
        "quote_timestamp_future" if future_quote_count else None
    ) if quotes else None
    source = _source(
        "腾讯财经公开行情（参考代码清单批量）",
        min(as_of_values) if as_of_values else None,
        error=time_error or ("quote_duplicate_symbol" if duplicate_quote_count else None)
        or (("batch_errors:" + ",".join(errors)) if errors and not quotes else None),
        decision_eligible=False,
        calendar="a_share",
    )
    source.update({
        "coverage_expected": len(codes),
        "coverage_returned": len(quotes),
        "coverage_ratio": round(len(quotes) / len(codes) * 100, 2),
        "coverage_scope": "requested_reference_universe",
        "reference_universe_source": "调用方提供的证券代码清单",
        "reference_universe_expected": len(codes),
        "reference_universe_returned": len(quotes),
        "reference_coverage_ratio": round(len(quotes) / len(codes) * 100, 2),
        "exchange_universe_expected": None,
        "exchange_coverage_ratio": None,
        "exchange_coverage_status": "unknown",
        "earliest_as_of": _iso(min(as_of_values)) if as_of_values else None,
        "latest_as_of": _iso(max(as_of_values)) if as_of_values else None,
        "timestamp_returned": len(as_of_values),
        "timestamp_complete": timestamp_complete,
        "timestamp_same_date": timestamp_same_date,
        "future_quote_count": future_quote_count,
        "duplicate_quote_count": duplicate_quote_count,
    })
    if errors:
        # A partial batch is disclosed even when the other batches did return
        # usable quotes. Coverage alone must not hide a transport failure.
        source["partial_batch_errors"] = errors
    return quotes, source


def tencent_stock_depth(code: str) -> dict[str, Any] | None:
    """个股盘口明细（Level-1 五档 + 量比 + 换手率 + 委差 + 均价 + 最近一笔）。

    供个股工作台「盘口与流动性」面板展示真实盘口。失败返回 None——调用方保持
    诚实"未接入/不可计算"状态，**绝不伪造盘口**。字段口径来自腾讯 qt.gtimg.cn：
      f[7]/f[8] 外盘/内盘(手) · f[9..18] 买1-5(价/量 交替) · f[19..28] 卖1-5(价/量) ·
      f[29] 最近一笔成交（收盘后为空） · f[30] 数据时间 YYYYMMDDHHMMSS ·
      f[38] 换手率% · f[49] 量比 · f[50] 委差 · f[51] 均价。
    逐笔成交（完整成交明细）为 Level-2 数据，公开行情仅有"最近一笔"，不冒充。
    """
    if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
        return None
    try:
        response = _direct_get(
            "https://qt.gtimg.cn/q=" + _tencent_symbol(code),
            headers=_HTTP_HEADERS, timeout=8,
        )
        response.raise_for_status()
        line = next(
            (ln for ln in response.content.decode("gb18030", errors="replace").splitlines()
             if "=\"" in ln),
            None,
        )
        if line is None:
            return None
        raw = line.split("\"", 1)[1].rsplit("\"", 1)[0]
        fields = raw.split("~")
        if len(fields) < 52 or not fields[1]:
            return None

        def _level(price_idx: int, volume_idx: int) -> dict[str, Any] | None:
            price = _safe_number(fields[price_idx])
            if price is None or price <= 0:
                return None
            return {"price": price, "volume": _safe_number(fields[volume_idx])}

        buy = [lv for i in range(5) if (lv := _level(9 + i * 2, 10 + i * 2))]
        sell = [lv for i in range(5) if (lv := _level(19 + i * 2, 20 + i * 2))]
        return {
            "name": fields[1],
            "price": _safe_number(fields[3]),
            "previous_close": _safe_number(fields[4]),
            "depth": {"buy": buy, "sell": sell},
            "outer_volume": _safe_number(fields[7]),      # 外盘（手）
            "inner_volume": _safe_number(fields[8]),      # 内盘（手）
            "volume_ratio": _safe_number(fields[49]),     # 量比
            "turnover_rate": _safe_number(fields[38]),    # 换手率 %
            "avg_price": _safe_number(fields[51]),        # 均价
            "bid_ask_delta": _safe_number(fields[50]),    # 委差
            "latest_tick": (fields[29] or "").strip() or None,  # 最近一笔成交
            "data_time": _iso(_tencent_time(fields[30])),
        }
    except Exception:
        return None


def _market_cross_section(quotes: list[dict[str, Any]], source: dict[str, Any]) -> dict[str, Any]:
    changes = [row["change_pct"] for row in quotes if row.get("change_pct") is not None]
    up = sum(value > 0.01 for value in changes)
    down = sum(value < -0.01 for value in changes)
    flat = len(changes) - up - down
    limit_reference_count = 0
    limit_rows = []
    for row in quotes:
        price = _safe_number(row.get("price"))
        upper = _safe_number(row.get("limit_up_price"))
        lower = _safe_number(row.get("limit_down_price"))
        if price is not None:
            limit_reference_count += 1
        if price is not None and lower is not None and upper is not None and 0 < lower < upper:
            limit_rows.append(row)
    # One price tick of tolerance avoids classifying a rounded display value as
    # below/above the source-published band.  We never derive a band from the
    # code prefix or name.
    limit_up_count = sum(
        math.isclose(float(row["price"]), float(row["limit_up_price"]), rel_tol=0, abs_tol=0.0011)
        for row in limit_rows
    )
    limit_down_count = sum(
        math.isclose(float(row["price"]), float(row["limit_down_price"]), rel_tol=0, abs_tol=0.0011)
        for row in limit_rows
    )
    amount_values = [row["amount_yi"] for row in quotes if row.get("amount_yi") is not None]
    amount_yi = round(sum(amount_values), 2) if amount_values else None
    weighted_turnover_num = sum(
        row["turnover_pct"] * row["float_market_cap_yi"]
        for row in quotes
        if row.get("turnover_pct") is not None and row.get("float_market_cap_yi") not in (None, 0)
    )
    weighted_turnover_den = sum(
        row["float_market_cap_yi"] for row in quotes
        if row.get("turnover_pct") is not None and row.get("float_market_cap_yi") not in (None, 0)
    )

    def aggregate_valuation(field: str) -> tuple[float | None, float | None, int]:
        accepted = [
            row for row in quotes
            if row.get("market_cap_yi") not in (None, 0)
            and row.get(field) is not None and 0 < row[field] < (500 if field == "pe_ttm" else 100)
        ]
        if not accepted:
            return None, None, 0
        cap = sum(row["market_cap_yi"] for row in accepted)
        denominator = sum(row["market_cap_yi"] / row[field] for row in accepted)
        return (round(cap / denominator, 2) if denominator else None,
                round(float(statistics.median(row[field] for row in accepted)), 2), len(accepted))

    aggregate_pe, median_pe, pe_count = aggregate_valuation("pe_ttm")
    aggregate_pb, median_pb, pb_count = aggregate_valuation("pb")
    expected = source.get("coverage_expected")
    returned = source.get("coverage_returned")
    aggregate_issue = (
        "参考代码清单晚于报价或时点未核，不能回填该日市场总体。"
        if _reference_frame_error(source) else
        "逐证券报价时窗未核或跨度超过180秒，不能计算同一截面温度。"
        if _quote_window_error(source) else
        "参考池报价不完整或批次失败，不能将已返回证券外推为参考池温度。"
        if not isinstance(expected, int) or not isinstance(returned, int)
        or expected <= 0 or returned != expected or returned != len(quotes)
        or source.get("partial_batch_errors") else
        "逐证券报价时点、重码或有效涨跌幅不足，温度不评估。"
        if source.get("timestamp_complete") is not True
        or source.get("timestamp_same_date") is not True
        or source.get("future_quote_count") != 0
        or source.get("duplicate_quote_count") != 0
        or len(changes) != returned else
        "报价已过期或来源状态未核，温度不评估。"
        if source.get("health_status") not in {"verified", "delayed"} else None
    )
    up_ratio = round(up / len(changes) * 100, 2) if aggregate_issue is None and changes else None
    limit_up = limit_up_count if limit_rows else None
    limit_down = limit_down_count if limit_rows else None
    limit_temperature = (
        round(limit_up_count / (limit_up_count + limit_down_count) * 100, 2)
        if aggregate_issue is None and limit_rows and limit_up_count + limit_down_count
        else (50.0 if aggregate_issue is None and limit_rows else None)
    )
    temperature = (
        round(0.7 * up_ratio + 0.3 * limit_temperature, 1)
        if aggregate_issue is None and up_ratio is not None and limit_temperature is not None else None
    )
    if temperature is None:
        temperature_label = "未评估"
    elif temperature < 20:
        temperature_label = "冰点"
    elif temperature < 40:
        temperature_label = "谨慎"
    elif temperature <= 70:
        temperature_label = "中性"
    elif temperature <= 90:
        temperature_label = "亢奋"
    else:
        temperature_label = "极度亢奋"
    return {
        "status": (
            "ok" if len(changes) >= 4500 and source.get("exchange_coverage_status") == "complete"
            else "partial"
        ),
        "up": up, "down": down, "flat": flat, "sample_size": len(changes),
        "aggregate_issue": aggregate_issue,
        "aggregate_eligible": aggregate_issue is None,
        "up_ratio": up_ratio, "limit_up": limit_up, "limit_down": limit_down,
        "price_limit": {
            "rule_version": "tencent_effective_limit_prices_v1",
            "basis": "腾讯逐证券逐时点涨停价/跌停价；不按代码前缀推导。",
            "source_fields": ["limit_up_price", "limit_down_price"],
            "as_of": source.get("as_of"),
            "reference_count": limit_reference_count,
            "evaluable_count": len(limit_rows),
            "unevaluable_count": limit_reference_count - len(limit_rows),
            "coverage_ratio": round(len(limit_rows) / limit_reference_count * 100, 2) if limit_reference_count else None,
            "covers_st_and_board_rules": True,
            "initial_listing_without_effective_band": "excluded",
        },
        "total_amount_yi": amount_yi if aggregate_issue is None else None,
        "total_amount_observation_count": len(amount_values),
        "weighted_turnover_pct": (round(weighted_turnover_num / weighted_turnover_den, 2)
                                  if aggregate_issue is None and weighted_turnover_den else None),
        "valuation": {
            "aggregate_pe_ttm": aggregate_pe if aggregate_issue is None else None,
            "median_pe_ttm": median_pe if aggregate_issue is None else None,
            "pe_coverage": pe_count,
            "aggregate_pb": aggregate_pb if aggregate_issue is None else None,
            "median_pb": median_pb if aggregate_issue is None else None,
            "pb_coverage": pb_count,
            "method": "市值加权 PE/PB = 市值总和 ÷ Σ(市值/个股PE或PB)；剔除非正与异常值。",
        },
        "sentiment": {
            "score": temperature, "label": temperature_label,
            "method": "0.7×上涨家数占比 + 0.3×涨跌停平衡度；仅描述当日横截面温度，不是预测或交易信号。",
            "components": {"up_ratio": up_ratio, "limit_temperature": limit_temperature},
        },
        "structure": _market_structure(quotes, source),
        **source,
    }


def _industry_map() -> dict[str, str]:
    """Load the local mapping once; it is explicitly not an official SW map."""
    global _INDUSTRY_MAP_CACHE
    if _INDUSTRY_MAP_CACHE is not None:
        return _INDUSTRY_MAP_CACHE
    try:
        raw = json.loads(_INDUSTRY_MAP_PATH.read_text(encoding="utf-8"))
        _INDUSTRY_MAP_CACHE = {
            str(code): str(industry).strip()
            for code, industry in raw.items()
            if re.fullmatch(r"[0368]\d{5}", str(code)) and str(industry).strip()
        }
    except (OSError, ValueError, json.JSONDecodeError):
        _INDUSTRY_MAP_CACHE = {}
    return _INDUSTRY_MAP_CACHE


def _industry_live_ranking(quotes: list[dict[str, Any]], source: dict[str, Any]) -> dict[str, Any]:
    """Aggregate one quote snapshot after normalising to one broad level."""
    if (_reference_frame_error(source) or _quote_window_error(source)
            or isinstance(source.get("coverage_expected"), int)
            and isinstance(source.get("coverage_returned"), int)
            and (source["coverage_returned"] != source["coverage_expected"]
                 or source["coverage_returned"] != len(quotes))
            or source.get("partial_batch_errors")
            or source.get("health_status") not in {"verified", "delayed"}
            or source.get("error") in {"quote_cross_date", "quote_timestamp_future",
                                       "quote_timestamp_incomplete", "quote_duplicate_symbol"}
            or source.get("timestamp_complete") is False
            or source.get("timestamp_same_date") is False
            or source.get("future_quote_count", 0) != 0
            or source.get("duplicate_quote_count", 0) != 0):
        return {
            "status": "unavailable", "items": [],
            "reason": "参考池逐证券报价覆盖、时点或来源状态未核，行业排名停止排序。",
            **source,
        }
    groups: dict[str, list[dict[str, Any]]] = {}
    mapping = _industry_map()
    reference = [row for row in quotes if _safe_number(row.get("change_pct")) is not None]
    unknown_labels: set[str] = set()
    for row in quotes:
        raw_industry = mapping.get(str(row.get("code") or ""))
        change = _safe_number(row.get("change_pct"))
        if not raw_industry or raw_industry in _NON_INDUSTRY_LABELS or change is None:
            continue
        industry = _INDUSTRY_LEVEL1_ALIASES.get(raw_industry)
        if industry is None:
            unknown_labels.add(raw_industry)
            continue
        groups.setdefault(industry, []).append(row)

    mapped_stock_count = sum(len(rows) for rows in groups.values())
    reference_stock_count = len(reference)
    mapped_ratio = round(mapped_stock_count / reference_stock_count * 100, 2) if reference_stock_count else None
    disclosure = {
        "mapping": "本机宽口径一级行业归一（非官方）",
        "taxonomy_level": "broad_level_1",
        "reference_stock_count": reference_stock_count,
        "mapped_stock_count": mapped_stock_count,
        "mapped_reference_ratio": mapped_ratio,
        "mapping_coverage_complete": mapped_stock_count == reference_stock_count,
        "mapped_industry_count": len(groups),
        "unknown_industry_labels": sorted(unknown_labels),
        "taxonomy_status": "incompatible" if unknown_labels else "normalized",
    }
    if unknown_labels or mapped_stock_count < 100 or len(groups) < 5:
        return {
            "status": "unavailable",
            "reason": (
                "本机行业映射出现未归一标签，可能混入父子层级；已停止行业排名。"
                if unknown_labels else
                "当前参考行情与本机一级行业映射的有效交集不足，不能输出可靠行业排名。"
            ),
            **disclosure,
            **source,
        }

    items = []
    for industry, rows in groups.items():
        changes = [float(row["change_pct"]) for row in rows]
        amount_values = [float(row["amount_yi"]) for row in rows if row.get("amount_yi") is not None]
        items.append({
            "industry": industry,
            "change_pct": round(statistics.fmean(changes), 2),
            "up": sum(value > 0.01 for value in changes),
            "down": sum(value < -0.01 for value in changes),
            "stock_count": len(rows),
            "amount_yi": round(sum(amount_values), 2) if amount_values else None,
            "amount_observation_count": len(amount_values),
        })
    items.sort(key=lambda item: (item["change_pct"], item["stock_count"]), reverse=True)
    for rank, item in enumerate(items, 1):
        item["rank"] = rank

    health = source.get("health_status")
    mapping_complete = mapped_stock_count == reference_stock_count
    return {
        "status": (
            "ok" if health == "verified" and mapping_complete
            else "delayed" if health == "delayed" and mapping_complete
            else "partial"
        ),
        "reason": "按同一行情快照内已映射股票的等权平均涨跌幅排序；仅作看盘观察，不参与任何交易决策。",
        "mapping": "本机宽口径一级行业归一（非官方）",
        "method": "先把已知旧行业/细行业归一到同一宽口径一级，再按已映射股票等权平均；未知层级会停止输出。",
        "items": items,
        **disclosure,
        **source,
    }


# ── 全球资产提供方 ──────────────────────────────────────────────
# 每个提供方返回 (asset | None, error)。asset 非 None 即视为该源可用（不再尝试后继源）；
# 全链失败 → _fetch_global_asset 返回错误资产（health=upstream_error）；
# 显式空链（providers=()）→ expected_unavailable 占位；与传输失败分开统计，但仍计入用户可见缺失。
# 2026-08-18 实测网络：Yahoo 403/429、CoinGecko 直连超时、push2his 指纹拦截（RemoteDisconnected）、
# Jina Reader 不可达；push2delay / Tencent qt.gtimg.cn+ifzq / Sina hf_+fx_ / FRED / Binance vision 可直连。

# 迷你走势线：东财指数用腾讯 ifzq 日 K（东财 kline 端点被指纹拦截）；无对应符号 → 空走势线（前端降级为 —）。
_IFZQ_KLINES = {
    "100.DJIA": "usDJI", "100.SPX": "usINX", "100.NDX": "usIXIC",
    "100.HSI": "hkHSI", "100.UDI": "usUDI", "hkHSTECH": "hkHSTECH",
}


def _ifzq_kline(symbol: str) -> list[float]:
    """腾讯 ifzq 日 K 收盘序列（近 40 日）；失败返回空（前端降级为无走势线）。"""
    try:
        response = _direct_get(
            f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,40,qfq",
            headers=_HTTP_HEADERS, timeout=8,
        )
        response.raise_for_status()
        node = response.json().get("data", {}).get(symbol, {})
        if not isinstance(node, dict):
            return []
        days = node.get("day") or node.get("qfqday") or []
        closes = [_safe_number(row[2]) for row in days if isinstance(row, list) and len(row) >= 3]
        closes = [value for value in closes if value is not None]
        # 若腾讯只返回单根（如美股指数历史未开放），退化为空走势线（前端显示 —）。
        return [round(value, 4) for value in closes[-40:]] if len(closes) >= 2 else []
    except Exception:
        return []


def _eastmoney_global_quote(
    asset_id: str, name: str, category: str, threshold: float, secid: str,
) -> tuple[dict[str, Any] | None, str]:
    """东方财富 push2delay 全球指数（延迟源，100.*）。失败返回 (None, error) 以便链式兜底。"""
    try:
        payload = _direct_eastmoney_json(
            "https://push2delay.eastmoney.com/api/qt/stock/get",
            {"secid": secid, "fields": "f43,f44,f45,f46,f57,f58,f60,f86,f169,f170",
             "fltt": "2", "invt": "2", "ut": _EASTMONEY_UT},
        )
        data = payload.get("data") or {}
        price = _safe_number(data.get("f43"))
        change_pct = _safe_number(data.get("f170"))
        if price is None or change_pct is None:
            raise ValueError("quote_fields_missing")
        as_of = _as_of_from_epoch(data.get("f86"))
        return {
            "id": asset_id, "name": name, "category": category, "symbol": secid,
            "price": price, "previous_close": _safe_number(data.get("f60")),
            "change_pct": change_pct, "absolute_change": _safe_number(data.get("f169")),
            "return_basis": "previous_session_close_to_current",
            "threshold_pct": threshold,
            "spark": _ifzq_kline(_IFZQ_KLINES[secid]) if secid in _IFZQ_KLINES else [],
            **_source("东方财富全球指数（push2delay 延迟源）", as_of, decision_eligible=False),
        }, ""
    except Exception as exc:
        return None, f"eastmoney_{type(exc).__name__}"


def _parse_sina_datetime(date_value: str, time_value: str) -> datetime | None:
    if not date_value or not time_value:
        return None
    try:
        return datetime.strptime(f"{date_value} {time_value}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=SH_TZ)
    except ValueError:
        return None


def _sina_hf_quote(
    asset_id: str, name: str, category: str, threshold: float, symbol: str,
) -> tuple[dict[str, Any] | None, str]:
    """新浪期货 hf_*（纽约金/原油/布伦特/伦铜）。字段以逗号分隔，需 Referer。"""
    try:
        response = _direct_get(
            "https://hq.sinajs.cn/list=" + symbol,
            headers={"Referer": "https://finance.sina.com.cn/", **_HTTP_HEADERS}, timeout=8,
        )
        response.raise_for_status()
        match = re.search(r'="(.*)"', response.content.decode("gb18030", errors="replace"))
        if match is None:
            raise ValueError("hf_no_match")
        fields = match.group(1).split(",")
        if len(fields) < 14 or not fields[0]:
            raise ValueError("hf_fields_short")
        price = _safe_number(fields[0])
        previous = _safe_number(fields[7])  # 昨结算价
        if price is None or previous in (None, 0):
            raise ValueError("hf_price_missing")
        as_of = _parse_sina_datetime(fields[12], fields[6])
        return {
            "id": asset_id, "name": name, "category": category, "symbol": symbol,
            "price": price, "previous_close": previous,
            "change_pct": round((price / previous - 1) * 100, 3),
            "absolute_change": round(price - previous, 3),
            "return_basis": "previous_settlement_to_current",
            "threshold_pct": threshold, "spark": [],
            **_source("新浪期货公开行情（hf_*）", as_of, decision_eligible=False),
        }, ""
    except Exception as exc:
        return None, f"sina_hf_{type(exc).__name__}"


def _sina_fx_quote(
    asset_id: str, name: str, category: str, threshold: float, symbol: str,
) -> tuple[dict[str, Any] | None, str]:
    """新浪外汇 fx_susdcnh 等（离岸人民币）。字段以逗号分隔，需 Referer。"""
    try:
        response = _direct_get(
            "https://hq.sinajs.cn/list=" + symbol,
            headers={"Referer": "https://finance.sina.com.cn/", **_HTTP_HEADERS}, timeout=8,
        )
        response.raise_for_status()
        match = re.search(r'="(.*)"', response.content.decode("gb18030", errors="replace"))
        if match is None:
            raise ValueError("fx_no_match")
        fields = match.group(1).split(",")
        if len(fields) < 18 or not fields[8]:
            raise ValueError("fx_fields_short")
        price = _safe_number(fields[8])
        previous = _safe_number(fields[5])
        if price is None:
            raise ValueError("fx_price_missing")
        as_of = _parse_sina_datetime(fields[17], fields[0])
        return {
            "id": asset_id, "name": name, "category": category, "symbol": symbol,
            "price": price, "previous_close": previous,
            "change_pct": round((price / previous - 1) * 100, 3) if previous not in (None, 0) else None,
            "absolute_change": round(price - previous, 6) if previous is not None else None,
            "return_basis": "previous_session_close_to_current",
            "threshold_pct": threshold, "spark": [],
            **_source("新浪外汇公开行情（fx_*）", as_of, decision_eligible=False),
        }, ""
    except Exception as exc:
        return None, f"sina_fx_{type(exc).__name__}"


def _fred_fetch(series: str) -> tuple[list[tuple[str, float]], dict[str, Any]]:
    """FRED 公开图表 CSV（每日序列，无 API key；周末自然滞后 → daily 健康口径）。

    2026-08-18 实测：本机 Python `requests` TLS 握手到 fred.stlouisfed.org 会随机
    挂死（ReadTimeout，`verify`/代理无关），而 `curl` 稳定 ~2s 返回。故用系统
    `curl` 子进程读取（series 为本模块硬编码常量，非用户输入）；失败判
    upstream_error，绝不伪造数据。
    """
    errors: list[str] = []
    for attempt in range(2):
        try:
            completed = subprocess.run(
                ["curl", "-s", "-m", str(_SOURCE_REQUEST_TIMEOUT_SECONDS),
                 "--max-filesize", "5000000",
                 f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"],
                capture_output=True, timeout=_SOURCE_REQUEST_TIMEOUT_SECONDS + 1,
            )
            if completed.returncode != 0:
                raise ValueError(f"curl_exit_{completed.returncode}")
            rows = [line.split(",") for line in
                    completed.stdout.decode("utf-8", "replace").splitlines()[1:]
                    if "," in line]
            valid = [(date, _safe_number(value)) for date, value, *_ in rows]
            valid = [(date, value) for date, value in valid if value is not None]
            if len(valid) < 2:
                raise ValueError("valid_observations_missing")
            as_of = datetime.fromisoformat(valid[-1][0]).replace(tzinfo=SH_TZ)
            return valid, _source("FRED（每日序列）", as_of, decision_eligible=False, daily=True)
        except Exception as exc:
            errors.append(type(exc).__name__)
            if attempt < 1:
                _time.sleep(1.0)
    return [], _source("FRED（每日序列）", None, error=",".join(errors), daily=True)


def _fred_provider(
    asset_id: str, name: str, category: str, threshold: float, symbol: str,
) -> tuple[dict[str, Any] | None, str]:
    """FRED 提供方：symbol 形如 bond:DGS10（美债收益率，change_bp）或 vix:VIXCLS（点数变化）。"""
    kind, _, series = symbol.partition(":")
    valid, source = _fred_fetch(series)
    if len(valid) < 2:
        base = {
            "id": asset_id, "name": name, "category": category, "symbol": series,
            "price": None, "threshold_pct": threshold, "spark": [], **source,
        }
        return base, f"fred_{source.get('error') or 'short'}"
    _, value = valid[-1]
    _, previous = valid[-2]
    if kind == "bond":
        return {
            "id": asset_id, "name": name, "category": category, "symbol": series,
            "price": value, "change_bp": round((value - previous) * 100, 2),
            "return_basis": "previous_daily_observation_to_latest",
            "threshold_bp": threshold,
            "spark": [round(v, 4) for _, v in valid[-40:]],
            **source,
        }, ""
    return {
        "id": asset_id, "name": name, "category": category, "symbol": series,
        "price": value, "previous_close": previous,
        "change_pct": round((value / previous - 1) * 100, 3) if previous else None,
        "absolute_change": round(value - previous, 3),
        "return_basis": "previous_daily_observation_to_latest",
        "threshold_pct": threshold,
        "spark": [round(v, 4) for _, v in valid[-40:]],
        **source,
    }, ""


def _binance_vision_kline(symbol_pair: str) -> list[float]:
    try:
        response = _direct_get(
            "https://data-api.binance.vision/api/v3/klines",
            params={"symbol": symbol_pair, "interval": "1d", "limit": "40"},
            headers=_HTTP_HEADERS, timeout=8,
        )
        response.raise_for_status()
        closes = [_safe_number(row[4]) for row in response.json() if isinstance(row, list) and len(row) >= 5]
        closes = [value for value in closes if value is not None]
        return [round(value, 4) for value in closes[-40:]]
    except Exception:
        return []


def _gateio_crypto_quote(
    asset_id: str, name: str, category: str, threshold: float, symbol: str, *, fallback_from: str,
) -> tuple[dict[str, Any] | None, str]:
    """Gate.io 兜底（需逐资产请求）。"""
    gate_symbol = symbol + "_USDT"
    try:
        response = _direct_get(
            f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={gate_symbol}",
            headers=_HTTP_HEADERS, timeout=8,
        )
        response.raise_for_status()
        rows = response.json()
        row = rows[0] if isinstance(rows, list) and rows else {}
        price = _safe_number(row.get("last"))
        change_pct = _safe_number(row.get("change_percentage"))
        if price is None:
            raise ValueError("gateio_price_missing")
        as_of = _as_of_from_epoch(float(row["timestamp"])) if row.get("timestamp") else None
        return {
            "id": asset_id, "name": name, "category": category, "symbol": gate_symbol,
            "price": price, "change_pct": change_pct, "absolute_change": None,
            "return_basis": "rolling_24h",
            "threshold_pct": threshold, "spark": [],
            **_source("Gate.io 公开行情", as_of, decision_eligible=False),
        }, ""
    except Exception as exc:
        return {
            "id": asset_id, "name": name, "category": category, "symbol": gate_symbol,
            "price": None, "change_pct": None, "absolute_change": None,
            "return_basis": "rolling_24h",
            "threshold_pct": threshold, "spark": [],
            **_source("Binance Vision → Gate.io 兜底", None,
                      error=f"{fallback_from};gateio_{type(exc).__name__}"),
        }, ""


def _crypto_quote(
    asset_id: str, name: str, category: str, threshold: float, symbol: str,
) -> tuple[dict[str, Any] | None, str]:
    """Binance Vision 24hr 行情主源（可直连），Gate.io 兜底。symbol 形如 BTC。"""
    symbol_pair = symbol + "USDT"
    try:
        response = _direct_get(
            "https://data-api.binance.vision/api/v3/ticker/24hr",
            params={"symbol": symbol_pair}, headers=_HTTP_HEADERS, timeout=8,
        )
        response.raise_for_status()
        data = response.json()
        price = _safe_number(data.get("lastPrice"))
        change_pct = _safe_number(data.get("priceChangePercent"))
        if price is None:
            raise ValueError("crypto_price_missing")
        as_of = _as_of_from_epoch(float(data["closeTime"]) / 1000) if data.get("closeTime") else None
        return {
            "id": asset_id, "name": name, "category": category, "symbol": symbol_pair,
            "price": price, "change_pct": change_pct, "absolute_change": None,
            "return_basis": "rolling_24h",
            "threshold_pct": threshold, "spark": _binance_vision_kline(symbol_pair),
            **_source("Binance Vision 公开行情（data-api.binance.vision）", as_of, decision_eligible=False),
        }, ""
    except Exception as exc:
        return _gateio_crypto_quote(
            asset_id, name, category, threshold, symbol,
            fallback_from=f"binance_vision:{type(exc).__name__}",
        )


def _tencent_provider(
    asset_id: str, name: str, category: str, threshold: float, symbol: str,
) -> tuple[dict[str, Any] | None, str]:
    """腾讯 qt.gtimg.cn 全球行情（恒生科技 hkHSTECH 等 ~ 分隔）。成功时补 ifzq 走势线。"""
    asset = _tencent_hk_index_asset(asset_id, name, category, symbol, threshold)
    if asset.get("health_status") in ("upstream_error", "missing"):
        return None, f"tencent_{asset.get('error') or asset.get('health_status')}"
    asset["spark"] = _ifzq_kline(_IFZQ_KLINES[symbol]) if symbol in _IFZQ_KLINES else []
    asset["source"] = "腾讯财经公开行情（qt.gtimg.cn）"
    return asset, ""


_PROVIDER_FNS = {
    "eastmoney": _eastmoney_global_quote,
    "sina_hf": _sina_hf_quote,
    "sina_fx": _sina_fx_quote,
    "fred": _fred_provider,
    "crypto": _crypto_quote,
    "tencent": _tencent_provider,
}


def _fetch_global_asset(spec: tuple) -> tuple[dict[str, Any], str]:
    """沿 _GLOBAL_ASSETS 的提供方链逐源尝试；全链失败 → 错误资产，空链 → expected_unavailable 占位。"""
    asset_id, name, category, threshold, providers = spec
    last_error = "no_reachable_source"
    for kind, symbol in providers:
        provider = _PROVIDER_FNS[kind]
        try:
            asset, err = provider(asset_id, name, category, threshold, symbol)
        except Exception as exc:
            asset, err = None, f"{kind}_{type(exc).__name__}"
        if asset is not None:
            return asset, ""
        last_error = err or last_error
    if not providers:
        return {
            "id": asset_id, "name": name, "category": category,
            "symbol": "", "price": None, "previous_close": None,
            "change_pct": None, "absolute_change": None,
            "threshold_pct": threshold, "spark": [], "expected_unavailable": True,
            "return_basis": None,
            **_source("（当前网络无可达公开源）", None, error="no_reachable_source"),
        }, "no_reachable_source"
    return {
        "id": asset_id, "name": name, "category": category,
        "symbol": providers[0][1], "price": None, "previous_close": None,
        "change_pct": None, "absolute_change": None,
        "threshold_pct": threshold, "spark": [],
        "return_basis": None,
        **_source("（多来源均不可达）", None, error=last_error),
    }, last_error


def _triggered(value: float | None, op: str, threshold: float) -> bool:
    if value is None:
        return False
    return value >= threshold if op == ">=" else value <= threshold


def evaluate_rules(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Evaluate only transparent observation rules; never emit a trade signal."""
    by_id = {asset["id"]: asset for asset in assets}
    events: list[dict[str, Any]] = []
    for rule in _RULES:
        asset = by_id.get(rule["asset"])
        if (
            not asset
            or asset.get("health_status") != "verified"
            or not _triggered(asset.get(rule["metric"]), rule["op"], rule["threshold"])
        ):
            continue
        events.append({
            "id": rule["id"],
            "event_type": "价格异动",
            "timestamp": asset.get("as_of"),
            "trigger_asset": asset["name"],
            "trigger_value": asset.get(rule["metric"]),
            "trigger_rule": f"{rule['metric']} {rule['op']} {rule['threshold']}",
            "source": asset.get("source"),
            "as_of": asset.get("as_of"),
            "latency_seconds": asset.get("latency_seconds"),
            "health_status": asset.get("health_status"),
            "effects": [
                {
                    "industry": industry,
                    "direction": "未验证",
                    "strength": "未验证",
                }
                for industry, _direction in rule["effects"]
            ],
            "transmission_path": rule["path"],
            "evidence_strength": "未验证",
            "impact_status": "neutral_unverified",
            "historical_hit_rate": None,
            "historical_validation": "尚未完成独立历史验证；仅观察。",
            "participates_in_decision": False,
        })
    return sorted(events, key=lambda item: item.get("timestamp") or "", reverse=True)


def heatmap(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hypotheses = {industry: 0 for industry in _ALL_INDUSTRIES}
    for event in events:
        for effect in event["effects"]:
            hypotheses[effect["industry"]] = hypotheses.get(effect["industry"], 0) + 1
    return [
        {
            "industry": industry,
            "score": 0,
            "direction": "中性/未验证",
            "impact_status": "neutral_unverified",
            "hypothesis_count": count,
            "participates_in_decision": False,
        }
        for industry, count in sorted(hypotheses.items())
    ]


def _quote_snapshot(provider: QuoteProvider) -> list[dict[str, Any]]:
    codes = ("sh000001", "sz399001", "sz399006")
    names = {"sh000001": "上证综指", "sz399001": "深证成指", "sz399006": "创业板指"}
    try:
        quotes = provider.get_quotes(list(codes))
    except MarketDataUnavailable as exc:
        return [{
            "code": code,
            "name": names[code],
            "price": None,
            "change_pct": None,
            **_source("5200 A股报价提供方", None, error=type(exc).__name__, calendar="a_share"),
        } for code in codes]
    snapshot = []
    for code in codes:
        quote = quotes.get(code)
        if quote is None:
            snapshot.append({
                "code": code, "name": names[code], "price": None, "change_pct": None,
                **_source("5200 A股报价提供方", None, error="quote_missing", calendar="a_share"),
            })
            continue
        as_of = _parse_iso(quote.exchange_data_time) or _parse_iso(quote.data_time)
        source = _source(quote.source, as_of, decision_eligible=False, calendar="a_share")
        if quote.freshness == "stale":
            source["health_status"] = "delayed"
        snapshot.append({
            "code": code,
            "name": names[code],
            "price": quote.price,
            "change_pct": quote.change_pct,
            "open_price": quote.open_price,
            "high": quote.high,
            "low": quote.low,
            "amount": quote.amount,
            "market_phase": quote.freshness,
            **source,
        })
    return snapshot


def _asof_from_date(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    now = _now()
    if parsed.date() > now.date() or (
        parsed.date() == now.date() and now.time() < time(15, 5)
    ):
        return None
    return parsed.replace(hour=15, tzinfo=SH_TZ)


def _completed_daily_rows(
    rows: list[dict[str, Any]], now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Keep only daily bars whose A-share close is already observable."""
    current = (now or _now()).astimezone(SH_TZ)
    cutoff = current.date()
    include_today = current.time() >= time(15, 5)
    completed: list[dict[str, Any]] = []
    for row in rows:
        try:
            day = datetime.fromisoformat(str(row.get("date"))[:10]).date()
        except (TypeError, ValueError):
            continue
        if day < cutoff or (day == cutoff and include_today):
            completed.append(row)
    return completed


def _month_metrics(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return MTD from prior month close and true path maximum drawdown."""
    if not rows:
        return None
    latest_month = rows[-1]["date"][:7]
    first_index = next(
        (index for index, row in enumerate(rows) if row["date"].startswith(latest_month)),
        None,
    )
    if first_index is None or first_index == 0:
        return None
    month_rows = rows[first_index:]
    previous_close = rows[first_index - 1]["close"]
    if previous_close in (None, 0) or not month_rows:
        return None
    peak = previous_close
    max_drawdown = 0.0
    for row in month_rows:
        close = row["close"]
        peak = max(peak, close)
        max_drawdown = min(max_drawdown, close / peak - 1)
    last = month_rows[-1]
    return {
        "month": latest_month,
        "close": last["close"],
        "mtd_change_pct": round((last["close"] / previous_close - 1) * 100, 2),
        "month_high": max(row["high"] for row in month_rows),
        "month_low": min(row["low"] for row in month_rows),
        "amplitude_pct": round(
            (max(row["high"] for row in month_rows) / min(row["low"] for row in month_rows) - 1) * 100,
            2,
        ),
        "monthly_drawdown_pct": round(max_drawdown * 100, 2),
        "mtd_baseline_date": rows[first_index - 1]["date"],
        "mtd_baseline_close": previous_close,
    }


def _series_history(code: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Use the documented AKShare daily index path; failure stays visible."""
    try:
        import akshare as ak
        frame = ak.index_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=(_now() - timedelta(days=1500)).strftime("%Y%m%d"),
            end_date=_now().strftime("%Y%m%d"),
        )
        required = {"日期", "收盘", "最高", "最低"}
        if frame.empty or not required.issubset(frame.columns):
            raise ValueError("required_columns_missing")
        rows = []
        for _, row in frame.iterrows():
            close = _safe_number(row.get("收盘"))
            high = _safe_number(row.get("最高"))
            low = _safe_number(row.get("最低"))
            if close is not None and high is not None and low is not None:
                rows.append({
                    "date": str(row["日期"])[:10],
                    "close": close,
                    "high": high,
                    "low": low,
                    "amount": _safe_number(row.get("成交额")),
                })
        rows = _completed_daily_rows(rows)
        if len(rows) < 2:
            raise ValueError("insufficient_rows")
        return rows, _source(
            "AKShare / 东方财富指数日线", _asof_from_date(rows[-1]["date"]),
            daily=True, calendar="a_share",
        )
    except Exception as exc:
        return _yahoo_index_history(code, f"AKShare / 东方财富不可用：{type(exc).__name__}")


def _yahoo_index_history(code: str, fallback_from: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """A labelled fallback for index history only; never silently replaces AKShare."""
    symbol = {"000001": "000001.SS", "399001": "399001.SZ", "399006": "399006.SZ"}.get(code)
    if symbol is None:
        return [], _source("A股指数日线", None, error="unsupported_index_code", daily=True, calendar="a_share")
    try:
        response = _direct_get(
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            + symbol + "?range=5y&interval=1d",
            headers=_HTTP_HEADERS,
            timeout=10,
        )
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
        quote = result["indicators"]["quote"][0]
        rows = []
        for timestamp, close, high, low, volume in zip(
            result.get("timestamp", []),
            quote.get("close", []),
            quote.get("high", []),
            quote.get("low", []),
            quote.get("volume", []),
        ):
            close, high, low = _safe_number(close), _safe_number(high), _safe_number(low)
            if close is not None and high is not None and low is not None:
                rows.append({
                    "date": _as_of_from_epoch(timestamp).date().isoformat(),
                    "close": close,
                    "high": high,
                    "low": low,
                    "amount": _safe_number(volume),
                })
        rows = _completed_daily_rows(rows)
        if len(rows) < 250:
            raise ValueError("insufficient_history_rows")
        as_of = _asof_from_date(rows[-1]["date"])
        source = _source(
            "Yahoo Finance / A股指数日线（AKShare 降级来源）", as_of,
            daily=True, calendar="a_share",
        )
        source["fallback_from"] = fallback_from
        return rows, source
    except Exception as exc:
        return [], _source(
            "AKShare / 东方财富指数日线；Yahoo Finance 降级来源",
            None,
            error=f"{fallback_from}；Yahoo:{type(exc).__name__}",
            daily=True,
            calendar="a_share",
        )


def _adx(rows: list[dict[str, Any]], period: int = 14) -> float | None:
    if len(rows) < period * 2 + 1:
        return None
    trs: list[float] = []
    plus: list[float] = []
    minus: list[float] = []
    for prev, current in zip(rows, rows[1:]):
        up = current["high"] - prev["high"]
        down = prev["low"] - current["low"]
        trs.append(max(current["high"] - current["low"], abs(current["high"] - prev["close"]), abs(current["low"] - prev["close"])))
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
    dx: list[float] = []
    for index in range(period - 1, len(trs)):
        tr = sum(trs[index - period + 1:index + 1])
        if tr == 0:
            continue
        pdi = 100 * sum(plus[index - period + 1:index + 1]) / tr
        mdi = 100 * sum(minus[index - period + 1:index + 1]) / tr
        if pdi + mdi:
            dx.append(100 * abs(pdi - mdi) / (pdi + mdi))
    return round(sum(dx[-period:]) / len(dx[-period:]), 2) if dx else None


def _trend(rows: list[dict[str, Any]]) -> dict[str, Any]:
    close = rows[-1]["close"]
    def average(size: int) -> float | None:
        return round(sum(row["close"] for row in rows[-size:]) / size, 2) if len(rows) >= size else None
    ma20, ma60, ma250 = average(20), average(60), average(250)
    if None in (ma20, ma60, ma250):
        label = "样本不足"
    elif close > ma20 > ma60 > ma250:
        label = "上升趋势"
    elif close < ma20 < ma60 < ma250:
        label = "下降趋势"
    else:
        label = "震荡/纠缠"
    return {
        "close": close, "ma20": ma20, "ma60": ma60, "ma250": ma250,
        "adx14": _adx(rows), "label": label,
    }


def _tencent_index_history(code: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        candles = TencentCandleProvider().get_candles(code, 600, interval="1d")
        rows = [
            {
                "date": str(candle.time)[:10], "close": candle.close,
                "high": candle.high, "low": candle.low, "amount": candle.amount,
            }
            for candle in candles
            if candle.close is not None and candle.high is not None and candle.low is not None
        ]
        rows = _completed_daily_rows(rows)
        if len(rows) < 250:
            raise ValueError("insufficient_history_rows")
        return rows, _source(
            "腾讯财经公开前复权指数日线", _asof_from_date(rows[-1]["date"]),
            daily=True, calendar="a_share",
        )
    except (CandleDataUnavailable, ValueError) as exc:
        compact = code[2:] if code.startswith(("sh", "sz")) else code
        rows, fallback = _series_history(compact)
        fallback["fallback_from"] = f"腾讯财经指数日线不可用：{type(exc).__name__}"
        return rows, fallback


def _eastmoney_market_flow_history() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    params = {
        "lmt": "300", "klt": "101", "secid": "1.000001", "secid2": "0.399001",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "ut": _EASTMONEY_UT,
    }
    try:
        payload = _direct_eastmoney_json(_MARKET_FLOW_URL, params)
        raw_rows = payload.get("data", {}).get("klines") or []
        rows: list[dict[str, Any]] = []
        for line in raw_rows:
            parts = str(line).split(",")
            if len(parts) < 15 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[0]):
                continue
            main_net, super_large_net = _safe_number(parts[1]), _safe_number(parts[5])
            rows.append({
                "date": parts[0], "main_net_yi": main_net / 1e8 if main_net is not None else None,
                "main_net_pct": _safe_number(parts[6]), "super_large_net_yi": super_large_net / 1e8 if super_large_net is not None else None,
                "sh_close": _safe_number(parts[11]), "sh_change_pct": _safe_number(parts[12]),
                "sz_close": _safe_number(parts[13]), "sz_change_pct": _safe_number(parts[14]),
                "value_schema": "eastmoney_fflow_daykline_v1",
                "main_net_raw": main_net,
                "super_large_net_raw": super_large_net,
                "raw_unit": "CNY",
                "normalized_unit": "CNY_100M",
                "normalization_divisor": 1e8,
                "normalization_reconciled": (
                    all(
                        value is None
                        or math.isclose((value / 1e8) * 1e8, value, rel_tol=1e-12, abs_tol=0.01)
                        for value in (main_net, super_large_net)
                    )
                ),
            })
        if not rows:
            raise ValueError("missing_klines")
        rows.sort(key=lambda row: row["date"])
        source = _source(
            "东方财富公开大盘资金流日线", _asof_from_date(rows[-1]["date"]),
            daily=True, calendar="a_share",
        )
        source.update({
            "value_schema": "eastmoney_fflow_daykline_v1",
            "raw_unit": "CNY",
            "normalized_unit": "CNY_100M",
            "normalization_divisor": 1e8,
            "normalization_reconciliation": {
                "status": "pass" if all(row["normalization_reconciled"] for row in rows) else "failed",
                "checked_rows": len(rows),
                "mismatch_rows": sum(not row["normalization_reconciled"] for row in rows),
            },
        })
        return rows, source
    except Exception as exc:
        # The public JSON endpoint intermittently rejects this runtime's
        # network route. The source page exposes the same dated table; use it
        # only as an explicit transport fallback, never a local fixture.
        return _eastmoney_market_flow_page_fallback(type(exc).__name__)


def _cn_amount(value: str) -> tuple[float | None, str | None, float | None]:
    text = value.strip().replace(",", "")
    matched = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)(亿|万)?", text)
    if matched is None:
        return None, None, None
    number = _safe_number(matched.group(1))
    if number is None:
        return None, None, None
    unit = "CNY_10K" if matched.group(2) == "万" else "CNY_100M" if matched.group(2) == "亿" else None
    normalized = number / 10000 if unit == "CNY_10K" else number if unit == "CNY_100M" else None
    return number, unit, normalized
def _eastmoney_market_flow_page_fallback(direct_error: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse dated flows from the public source page when its JSON route fails."""
    original_url = "https://data.eastmoney.com/zjlx/dpzjlx.html"
    try:
        response = _direct_get(
            _JINA_READER + original_url, headers=_HTTP_HEADERS, timeout=20,
        )
        response.raise_for_status()
        table_lines = [line for line in response.text.splitlines() if line.startswith("|")]
        header_rows = [
            [re.sub(r"\s+", "", cell) for cell in line.strip().strip("|").split("|")]
            for line in table_lines
            if not re.search(r"\|\s*\d{4}-\d{2}-\d{2}\s*\|", line)
        ]
        header_patterns = _FLOW_PAGE_SCHEMA["header_patterns"]
        schema_matched = any(
            len(cells) > max(header_patterns)
            and all(re.search(pattern, cells[index]) for index, pattern in header_patterns.items())
            for cells in header_rows
        )
        if not schema_matched:
            raise ValueError("page_table_schema_unrecognized")
        columns = _FLOW_PAGE_SCHEMA["columns"]
        rows: list[dict[str, Any]] = []
        for line in table_lines:
            if not line.startswith("|"):
                continue
            parts = [part.strip() for part in line.strip().strip("|").split("|")]
            if len(parts) <= max(columns.values()) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[columns["date"]]):
                continue
            main_raw, main_raw_unit, main_yi = _cn_amount(parts[columns["main_net"]])
            super_raw, super_raw_unit, super_yi = _cn_amount(parts[columns["super_large_net"]])
            if main_raw_unit is None or super_raw_unit is None:
                continue
            rows.append({
                "date": parts[columns["date"]], "main_net_yi": main_yi,
                "main_net_pct": _safe_number(parts[columns["main_net_pct"]].rstrip("%")),
                "super_large_net_yi": super_yi,
                "sh_close": _safe_number(parts[columns["sh_close"]]),
                "sh_change_pct": _safe_number(parts[columns["sh_change_pct"]].rstrip("%")),
                "sz_close": _safe_number(parts[columns["sz_close"]]),
                "sz_change_pct": _safe_number(parts[columns["sz_change_pct"]].rstrip("%")),
                "value_schema": _FLOW_PAGE_SCHEMA["version"],
                "main_net_raw": main_raw,
                "main_net_raw_unit": main_raw_unit,
                "super_large_net_raw": super_raw,
                "super_large_net_raw_unit": super_raw_unit,
                "normalized_unit": "CNY_100M",
                "normalization_reconciled": all(value is not None for value in (main_yi, super_yi)),
            })
        if not rows:
            raise ValueError("page_table_missing")
        rows.sort(key=lambda row: row["date"])
        source = _source(
            "东方财富公开大盘资金流日线（原站，经 Jina Reader 读取）",
            _asof_from_date(rows[-1]["date"]),
            daily=True,
            calendar="a_share",
        )
        source.update({
            "source_url": original_url,
            "transport": "Jina Reader",
            "fallback_from": f"东方财富公开 JSON 端点不可用：{direct_error}",
            "value_schema": _FLOW_PAGE_SCHEMA["version"],
            "schema_validation": "matched_required_headers",
            "header_patterns": {
                str(index): pattern for index, pattern in header_patterns.items()
            },
            "raw_units": sorted({
                unit for row in rows
                for unit in (row.get("main_net_raw_unit"), row.get("super_large_net_raw_unit"))
                if unit
            }),
            "normalized_unit": "CNY_100M",
            "normalization_reconciliation": {
                "status": "pass" if all(row["normalization_reconciled"] for row in rows) else "failed",
                "checked_rows": len(rows),
                "mismatch_rows": sum(not row["normalization_reconciled"] for row in rows),
            },
        })
        return rows, source
    except Exception as fallback_exc:
        fallback_error = (
            str(fallback_exc)
            if str(fallback_exc) in {"page_table_schema_unrecognized", "page_table_missing"}
            else type(fallback_exc).__name__
        )
        return [], _source(
            "东方财富公开大盘资金流日线", None,
            error=f"direct_{direct_error};page_{fallback_error}",
            daily=True,
            calendar="a_share",
        )


def _eastmoney_northbound_history() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    params = {
        "sortColumns": "TRADE_DATE", "sortTypes": "-1", "pageSize": "300", "pageNumber": "1",
        "reportName": "RPT_MUTUAL_DEAL_HISTORY", "columns": "ALL", "source": "WEB", "client": "WEB",
        "filter": '(MUTUAL_TYPE="005")',
    }
    try:
        payload = _direct_eastmoney_json(_NORTHBOUND_HISTORY_URL, params)
        raw_rows = payload.get("result", {}).get("data") or []
        rows = []
        for row in raw_rows:
            date = str(row.get("TRADE_DATE") or "")[:10]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                continue
            net_buy = _safe_number(row.get("NET_DEAL_AMT"))
            rows.append({
                "date": date, "net_buy_yi": net_buy / 100 if net_buy is not None else None,
                "net_buy_available": net_buy is not None,
                "connect_deal_amount": _safe_number(row.get("DEAL_AMT")),
                "value_schema": "eastmoney_mutual_deal_history_v1",
                "net_buy_raw": net_buy,
                "net_buy_raw_unit": "CNY_10K",
                "normalized_unit": "CNY_100M",
                "normalization_divisor": 100,
                "normalization_reconciled": (
                    net_buy is None
                    or math.isclose((net_buy / 100) * 100, net_buy, rel_tol=1e-12, abs_tol=0.01)
                ),
            })
        if not rows:
            raise ValueError("missing_rows")
        rows.sort(key=lambda row: row["date"])
        source = _source(
            "东方财富公开沪深港通历史", _asof_from_date(rows[-1]["date"]),
            daily=True, calendar="a_share",
        )
        source.update({
            "value_schema": "eastmoney_mutual_deal_history_v1",
            "raw_unit": "CNY_10K",
            "normalized_unit": "CNY_100M",
            "normalization_divisor": 100,
            "normalization_reconciliation": {
                "status": "pass" if all(row["normalization_reconciled"] for row in rows) else "failed",
                "checked_rows": len(rows),
                "mismatch_rows": sum(not row["normalization_reconciled"] for row in rows),
            },
        })
        if not any(row["net_buy_available"] for row in rows):
            source["field_notice"] = "上游当前不再返回北向成交净买额；零值未被解释为零流入。"
        return rows, source
    except Exception as exc:
        return [], _source(
            "东方财富公开沪深港通历史", None, error=type(exc).__name__,
            daily=True, calendar="a_share",
        )


def _return_pct(rows: list[dict[str, Any]], lookback: int) -> float | None:
    if len(rows) <= lookback or rows[-lookback - 1].get("close") in (None, 0):
        return None
    return round((rows[-1]["close"] / rows[-lookback - 1]["close"] - 1) * 100, 2)


def _annual_risk(rows: list[dict[str, Any]]) -> dict[str, Any]:
    window = rows[-251:]
    returns = [
        row["close"] / previous["close"] - 1
        for previous, row in zip(window, window[1:]) if previous["close"] not in (None, 0)
    ]
    peak = -math.inf
    max_drawdown = 0.0
    for row in window:
        peak = max(peak, row["close"])
        if peak > 0:
            max_drawdown = min(max_drawdown, row["close"] / peak - 1)
    return {
        "annualized_volatility_pct": round(statistics.pstdev(returns) * math.sqrt(252) * 100, 2) if len(returns) >= 20 else None,
        "one_year_max_drawdown_pct": round(max_drawdown * 100, 2) if len(window) >= 20 else None,
        "one_year_return_pct": _return_pct(rows, 250),
    }


def _fetch_source_text(url: str, name: str) -> tuple[str, dict[str, Any]]:
    """官方日历页直读（2026-08-18 实测 Jina Reader 对本网络不可达，Python `requests`
    对本机偶发 TLS 握手挂死，而系统 `curl` 子进程稳定返回——与 _fred_fetch 同一依据；
    故走 curl，`-f` 令 HTTP 4xx/5xx（如 BLS 403）判失败而非伪装成功）。"""
    errors: list[str] = []
    for attempt in range(2):
        try:
            completed = subprocess.run(
                ["curl", "-s", "-f", "-L", "-m", "15", "--max-filesize", "5000000",
                 "--cacert", certifi.where(),
                 "-A", _HTML_HEADERS["User-Agent"], url],
                capture_output=True, timeout=20,
            )
            text = completed.stdout.decode("utf-8", "replace")
            if completed.returncode != 0 or not text:
                raise ValueError(f"curl_exit_{completed.returncode}")
            source = _source(f"{name}（官网直读）", _now(), decision_eligible=False)
            source.update({"source_url": url, "transport": "官网直连 HTML"})
            return text, source
        except Exception as exc:
            errors.append(type(exc).__name__)
            if attempt < 1:
                _time.sleep(1.0)
    source = _source(f"{name}（官网直读）", None, error=",".join(errors))
    source.update({"source_url": url, "transport": "官网直连 HTML"})
    return "", source


_MONTH_NUM = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "Jun": 6, "Jul": 7, "Aug": 8,
    "Sept": 9, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _calendar_item(
    date_value: datetime,
    *, title: str, region: str, category: str, importance: str,
    source: dict[str, Any], time_known: bool = True,
) -> dict[str, Any]:
    channels = {
        "美国宏观": ["美元指数", "美债收益率", "全球风险偏好"],
        "货币政策": ["汇率", "利率预期", "成长股估值"],
        "中国宏观": ["政策预期", "顺周期行业", "市场风险偏好"],
    }
    return {
        "timestamp": _iso(date_value), "time_known": time_known, "title": title,
        "region": region, "category": category, "importance": importance,
        "a_share_channels": channels.get(category, ["全球风险偏好"]),
        "impact_direction": "不预判方向；仅标记潜在传导渠道。",
        **source,
    }


def _future_week(items: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    end = now.date() + timedelta(days=7)
    output = []
    for item in items:
        stamp = _parse_iso(item.get("timestamp"))
        if stamp is None or stamp.date() >= end:
            continue
        if item.get("time_known") and stamp < now:
            continue
        if not item.get("time_known") and stamp.date() < now.date():
            continue
        output.append(item)
    return sorted(output, key=lambda item: item["timestamp"])


def _news_assessment(item: dict[str, Any]) -> dict[str, Any]:
    """Attach auditable attention labels without pretending to know impact."""
    text = " ".join((str(item.get("title") or ""), str(item.get("summary") or "")))
    folded = text.casefold()
    material_terms = [term for term in _NEWS_MATERIALITY_TERMS if term.casefold() in folded]
    relevance_terms = [term for term in _NEWS_A_SHARE_TERMS if term.casefold() in folded]
    stocks = [str(code) for code in (item.get("stocks") or []) if re.fullmatch(r"\d{6}", str(code))]
    relevance_status = (
        "direct_security_reference" if stocks
        else "keyword_match_candidate" if relevance_terms
        else "unassessed"
    )
    materiality_status = "keyword_match_candidate" if material_terms else "unassessed"
    return {
        "materiality": {
            "status": materiality_status,
            "rule_version": "news_materiality_terms_v1",
            "matched_terms": material_terms,
            "basis": (
                "标题或摘要命中可复核重大性词；仅为注意力候选，不代表已验证市场影响。"
                if material_terms else "没有可审计的影响幅度或重大性命中，重大性未评估。"
            ),
        },
        "a_share_relevance": {
            "status": relevance_status,
            "rule_version": "news_a_share_relevance_v1",
            "matched_terms": relevance_terms,
            "security_codes": stocks,
            "basis": (
                "新闻条目直接附带境内证券代码。" if stocks
                else "标题或摘要命中可复核 A 股关联词。" if relevance_terms
                else "没有证券代码或 A 股关联词，A 股相关性未评估。"
            ),
        },
        "attention_class": (
            "material_a_share_candidate"
            if material_terms and relevance_status != "unassessed"
            else "unassessed"
        ),
    }


def _parse_bls_calendar(text: str, source: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    from zoneinfo import ZoneInfo
    allowed = ("Employment Situation", "Consumer Price Index", "Producer Price Index", "Job Openings", "Import and Export Price")
    events: list[dict[str, Any]] = []
    for raw_date, raw_time, raw_title in re.findall(r"\|\s*([A-Za-z]+,\s+[A-Za-z]+\s+\d{1,2},\s+\d{4})\s*\|\s*([0-9:]+\s+[AP]M)?\s*\|\s*(.*?)\s*\|", text):
        title = re.sub(r"[*_`]+", "", raw_title)
        if not any(name in title for name in allowed):
            continue
        try:
            value = datetime.strptime(raw_date + " " + raw_time, "%A, %B %d, %Y %I:%M %p").replace(tzinfo=ZoneInfo("America/New_York")).astimezone(SH_TZ)
        except ValueError:
            continue
        events.append(_calendar_item(value, title="美国 " + title, region="美国", category="美国宏观", importance="高", source=source))
    return _future_week(events, now)


def _parse_fomc_html(text: str, source: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """federalreserve.gov fomccalendars.htm：<h4>...2026 FOMC Meetings</h4> 下的
    fomc-meeting__month（<strong>January</strong>）+ fomc-meeting__date（27-28）。"""
    marker = f">{now.year} FOMC Meetings</a>"
    idx = text.find(marker)
    if idx < 0:
        return []
    # 从标记之后找下一个年份标题，避免把标记自身当作边界（会把段落截成空）。
    section = text[idx + len(marker):]
    nxt = re.search(r">\d{4} FOMC Meetings</a>", section)
    if nxt:
        section = section[:nxt.start()]
    events: list[dict[str, Any]] = []
    for month_name, day_range in re.findall(
        r'class="fomc-meeting__month[^"]*">\s*<strong>\s*([A-Za-z]+)\s*</strong>\s*</div>\s*'
        r'<div class="fomc-meeting__date[^"]*">\s*(\d{1,2}-\d{1,2})\s*</div>',
        section,
    ):
        month = _MONTH_NUM.get(month_name.capitalize())
        if month is None:
            continue
        days = re.findall(r"\d{1,2}", day_range)
        if not days:
            continue
        try:
            event_date = datetime(now.year, month, int(days[-1]), tzinfo=SH_TZ)
        except ValueError:
            continue
        events.append(_calendar_item(
            event_date, title="美联储 FOMC 议息会议（声明日）", region="美国",
            category="货币政策", importance="高", source=source, time_known=False,
        ))
    return _future_week(events, now)


def _nbs_cell_text(body: str) -> str:
    """去 HTML 标签并归一化空白；&nbsp; 视为普通空格。"""
    text = re.sub(r"<[^>]+>", "", body)
    text = text.replace("&nbsp;", " ")
    text = text.replace("&amp;", "&").replace("&#39;", "'")
    return re.sub(r"\s+", " ", text).strip()


def _append_nbs_entry(events: list[dict[str, Any]], entry: dict[str, Any], now: datetime, source: dict[str, Any]) -> None:
    title = entry["title"]
    for month_idx, slot in entry["slots"].items():
        day = slot.get("day")
        if not day:
            continue
        hour, minute = 0, 0
        time_known = bool(slot.get("time"))
        if time_known:
            try:
                hour, minute = (int(part) for part in slot["time"].split(":"))
            except (ValueError, TypeError):
                hour, minute = 0, 0
                time_known = False
        try:
            event_date = datetime(now.year, month_idx + 1, day, hour, minute, tzinfo=SH_TZ)
        except ValueError:
            continue
        events.append(_calendar_item(
            event_date, title=title, region="中国", category="中国宏观",
            importance="高", source=source, time_known=time_known,
        ))


def _parse_nbs_html(text: str, source: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """stats.gov.cn 发布日历（Word 导出 HTML，No.|Content|Jan..Dec 表）。
    内容标题与 No. 带 rowspan=2，第二行放 "10:00" 时间；日期格形如 "19/Mon"。"""
    events: list[dict[str, Any]] = []
    entry: dict[str, Any] | None = None
    pending: dict[int, int] = {}  # 上方 rowspan 仍占用的列 → 剩余行数
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", text, flags=re.S):
        cells = re.findall(r"<td([^>]*)>(.*?)</td>", row, flags=re.S)
        if not cells:
            continue
        col = 0
        placed: dict[int, str] = {}
        for attrs, body in cells:
            while pending.get(col, 0) > 0:
                pending[col] = pending.get(col, 0) - 1
                col += 1
            placed[col] = _nbs_cell_text(body)
            match_rs = re.search(r"rowspan=['\"](\d+)", attrs)
            rowspan = int(match_rs.group(1)) if match_rs else 1
            if rowspan > 1:
                pending[col] = rowspan - 1
            col += 1
        title = placed.get(1, "")
        if title:
            if entry is not None:
                _append_nbs_entry(events, entry, now, source)
            entry = {"title": title, "slots": {}}
        if entry is None:
            continue
        for month_idx in range(12):
            cell = placed.get(2 + month_idx, "")
            match_day = re.match(r"^(\d{1,2})/(Mon|Tues|Wed|Thurs|Fri|Sat|Sun)$", cell)
            if match_day:
                entry["slots"].setdefault(month_idx, {})["day"] = int(match_day.group(1))
                continue
            match_time = re.match(r"^(\d{1,2}):(\d{2})$", cell)
            if match_time and month_idx in entry["slots"]:
                entry["slots"][month_idx]["time"] = f"{match_time.group(1)}:{match_time.group(2)}"
    if entry is not None:
        _append_nbs_entry(events, entry, now, source)
    return _future_week(events, now)


def _parse_ecb_html(text: str, source: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """ECB 治理理事会议日历页；本网络 2026-08-18 实测时好时坏（HTTP 000），
    解析不到即返回空（诚实标记来源健康度，不补写事件）。"""
    events = []
    for raw_date, title in re.findall(r"(\d{2}/\d{2}/\d{4})\s+([^\n]*monetary policy[^\n]*)", text, flags=re.I):
        try:
            event_date = datetime.strptime(raw_date, "%d/%m/%Y").replace(tzinfo=SH_TZ)
        except ValueError:
            continue
        events.append(_calendar_item(event_date, title="欧洲央行 " + title.strip(), region="欧元区", category="货币政策", importance="中", source=source, time_known=False))
    return _future_week(events, now)


def _parse_boj_html(text: str, source: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """boj.or.jp 议息日程表：<caption class="non-caption">Table : 2026</caption> 下
    tbody 每行首格为 Date of MPM（如 "Jan. 22 (Thurs.), 23 (Fri.)"）；只取议息日，
    不把文件发布日重复计入。"""
    table = text.split(f"<caption class=\"non-caption\">Table : {now.year}</caption>", 1)
    if len(table) != 2:
        return []
    section = re.split(
        r"<caption class=\"non-caption\">Table : \d{4}</caption>",
        table[1],
        maxsplit=1,
    )[0]
    events: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for row in re.findall(r"<tr>\s*<td>(.*?)</td>", section, flags=re.S):
        cell_text = re.sub(r"<[^>]+>", "", row)
        cell_text = re.sub(r"\s+", " ", cell_text).strip()
        match_month = re.match(r"([A-Z][a-z]{2,9})\.?\s+(.*)", cell_text)
        if not match_month:
            continue
        month = _MONTH_NUM.get(match_month.group(1))
        if month is None:
            continue
        days = [int(value) for value in re.findall(r"\d{1,2}", match_month.group(2)) if int(value) <= 31]
        if not days:
            continue
        meeting_day = max(days)  # 会议声明日取最后一天（区间末）
        if (month, meeting_day) in seen:
            continue
        seen.add((month, meeting_day))
        try:
            event_date = datetime(now.year, month, meeting_day, tzinfo=SH_TZ)
        except ValueError:
            continue
        events.append(_calendar_item(event_date, title="日本央行货币政策会议（议息日）", region="日本", category="货币政策", importance="中", source=source, time_known=False))
    return _future_week(events, now)


def _official_macro_calendar(now: datetime) -> dict[str, Any]:
    urls = {
        "bls": (f"https://www.bls.gov/schedule/{now.year}/home.htm", "美国劳工统计局 BLS"),
        "fomc": ("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", "美联储 FOMC"),
        "nbs_index": ("https://www.stats.gov.cn/english/PressRelease/ReleaseCalendar/", "中国国家统计局 NBS"),
        "ecb": ("https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html", "欧洲中央银行 ECB"),
        "boj": ("https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm", "日本银行 BOJ"),
    }
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {name: executor.submit(_fetch_source_text, url, label) for name, (url, label) in urls.items()}
        raw = {name: future.result() for name, future in futures.items()}
    # NBS 索引页链接到当年发布日历页，需跟随链接再读一次（目标为 Word 导出 HTML 表格）。
    nbs_text, nbs_source = raw["nbs_index"]
    nbs_match = re.search(
        rf'<a[^>]+href="([^"]+)"[^>]*>\s*1\.Regular Press Release Calendar of NBS in {now.year}',
        nbs_text, flags=re.I,
    )
    if nbs_match:
        href = nbs_match.group(1)
        nbs_url = (
            href if href.startswith("http")
            else "https://www.stats.gov.cn/english/PressRelease/ReleaseCalendar/" + href.lstrip("./")
        )
        nbs_text, nbs_source = _fetch_source_text(nbs_url, "中国国家统计局 NBS")
    parsed = {
        "bls": _parse_bls_calendar(*raw["bls"], now),
        "fomc": _parse_fomc_html(*raw["fomc"], now),
        "nbs": _parse_nbs_html(nbs_text, nbs_source, now),
        "ecb": _parse_ecb_html(*raw["ecb"], now),
        "boj": _parse_boj_html(*raw["boj"], now),
    }
    sources = [raw[name][1] for name in ("bls", "fomc", "ecb", "boj")] + [nbs_source]
    for name, source in zip(("bls", "fomc", "ecb", "boj", "nbs"), sources):
        if source.get("health_status") == "verified" and not parsed[name]:
            source.update({"health_status": "upstream_error", "error": "parser_empty"})
    items = sum(parsed.values(), [])
    healthy = sum(source["health_status"] == "verified" for source in sources)
    status = "ok" if healthy == len(sources) else ("partial" if healthy else "unavailable")
    source_times = [value for source in sources if (value := _parse_iso(source.get("as_of"))) is not None]
    return {
        "status": status,
        "health_status": "verified" if status == "ok" else "upstream_error",
        "as_of": _iso(min(source_times)) if source_times else None,
        "data_as_of": _iso(min(source_times)) if source_times else None,
        "latest_data_as_of": _iso(max(source_times)) if source_times else None,
        "fetched_at": _iso(now),
        "served_at": _iso(now),
        "served_from_cache": False,
        "window": {"from": now.date().isoformat(), "to": (now.date() + timedelta(days=7)).isoformat()},
        "items": _future_week(items, now), "sources": sources,
        "reason": "仅列出未来7天可从官方日历核验的高影响宏观发布与议息安排；未接入/不可达的事件不补写。",
        "participates_in_decision": False,
    }


def _tencent_hk_index_asset(
    asset_id: str, name: str, category: str, tencent_symbol: str, threshold: float | None,
) -> dict[str, Any]:
    """恒生科技 Yahoo ^HSTECH 返回 404 → 腾讯 hkHSTECH 回退（2026-08-15 实测）。

    腾讯符号区分大小写（q=hkHSTECH 有效，q=hkhstech 返回 v_pv_none_match），
    故由调用方传入腾讯符号而非 Yahoo 符号。HK 行情字符串内含换行，须以
    DOTALL 捕获整个记录再按 ~ 切分；字段口径：f[1]名称 f[3]现价 f[4]昨收
    f[30]时间 f[32]涨跌幅%。
    """
    url = "https://qt.gtimg.cn/q=" + tencent_symbol
    try:
        response = _direct_get(url, headers=_HTTP_HEADERS, timeout=8)
        response.raise_for_status()
        raw = response.content.decode("gb18030", errors="replace")
        match = re.search(r'="(.*?)"', raw, re.S)
        if match is None:
            raise ValueError("hst_no_match")
        fields = match.group(1).split("~")
        if len(fields) < 33 or not fields[1]:
            raise ValueError("hst_fields_short")
        price = _safe_number(fields[3])
        previous = _safe_number(fields[4])
        change_pct = _safe_number(fields[32])
        as_of = None
        try:
            as_of = datetime.strptime(fields[30], "%Y/%m/%d %H:%M:%S").replace(tzinfo=SH_TZ)
        except (ValueError, IndexError):
            pass
        return {
            "id": asset_id,
            "name": name,
            "category": category,
            "symbol": tencent_symbol,
            "price": price,
            "previous_close": previous,
            "change_pct": change_pct,
            "absolute_change": round(price - previous, 3) if price is not None and previous is not None else None,
            "return_basis": "previous_session_close_to_current",
            "threshold_pct": threshold,
            "spark": [],
            **_source("腾讯财经公开行情（恒生科技 hkHSTECH 回退）", as_of, decision_eligible=False),
        }
    except Exception as exc:
        return {
            "id": asset_id,
            "name": name,
            "category": category,
            "symbol": tencent_symbol,
            "price": None,
            "previous_close": None,
            "change_pct": None,
            "absolute_change": None,
            "return_basis": "previous_session_close_to_current",
            "threshold_pct": threshold,
            "spark": [],
            **_source("腾讯财经公开行情（恒生科技 hkHSTECH 回退）", None, error=f"{type(exc).__name__}"),
        }


def _singleflight_safe(method: Callable[..., Any]) -> Callable[..., Any]:
    """Release every refresh owned by this thread if a leader unwinds."""
    @wraps(method)
    def wrapped(self: "MarketIntelligence", *args: object, **kwargs: object) -> Any:
        try:
            return method(self, *args, **kwargs)
        except BaseException:
            self._release_owned_refreshes(threading.get_ident())
            raise

    return wrapped


class MarketIntelligence:
    """Independent source and evidence layer for the two market-intelligence pages."""

    def __init__(
        self,
        quote_provider: QuoteProvider,
        fund_flow_path: Path,
        fund_flow_loader: Callable[[Path], tuple[list[dict[str, Any]], dict[str, Any]]],
    ) -> None:
        self.quote_provider = quote_provider
        self.fund_flow_path = fund_flow_path
        self.fund_flow_loader = fund_flow_loader
        self._lock = threading.Lock()
        self._global_cache: dict[str, Any] | None = None
        self._global_fetched_at: datetime | None = None
        self._history_cache: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
        self._history_fetched_at: dict[str, datetime] = {}
        self._market_cache: tuple[list[dict[str, Any]], dict[str, Any]] | None = None
        self._market_fetched_at: datetime | None = None
        self._flow_cache: tuple[list[dict[str, Any]], dict[str, Any]] | None = None
        self._flow_fetched_at: datetime | None = None
        self._northbound_cache: tuple[list[dict[str, Any]], dict[str, Any]] | None = None
        self._northbound_fetched_at: datetime | None = None
        self._calendar_cache: dict[str, Any] | None = None
        self._calendar_fetched_at: datetime | None = None
        self._calendar_refreshing = False
        self._last_trusted_global_at: datetime | None = None
        self._worker_started = False
        self._worker_thread: threading.Thread | None = None
        self._calendar_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._worker_last_success_at: datetime | None = None
        self._worker_last_error: str | None = None
        self._worker_iteration_at: datetime | None = None
        self._calendar_last_error: str | None = None
        self._sectors_cache: dict[str, Any] | None = None
        self._sectors_fetched_at: datetime | None = None
        self._news_cache: dict[str, Any] | None = None
        self._news_fetched_at: datetime | None = None
        self._spark_cache: dict[str, Any] | None = None
        self._spark_fetched_at: datetime | None = None
        self._refresh_flights: dict[str, tuple[int, threading.Event, int]] = {}
        self._refresh_generations: dict[str, int] = {}

    def _claim_refresh(self, key: str) -> tuple[bool, int, threading.Event]:
        """Coalesce one source refresh; the generation guards late commits."""
        with self._lock:
            current = self._refresh_flights.get(key)
            if current is not None:
                return False, current[0], current[1]
            generation = self._refresh_generations.get(key, 0) + 1
            event = threading.Event()
            self._refresh_generations[key] = generation
            self._refresh_flights[key] = (generation, event, threading.get_ident())
            return True, generation, event

    def _release_owned_refreshes(self, owner: int) -> None:
        with self._lock:
            for key, (generation, event, current_owner) in list(self._refresh_flights.items()):
                if current_owner != owner:
                    continue
                self._refresh_generations[key] = generation + 1
                self._refresh_flights.pop(key, None)
                event.set()

    def _wait_refresh(self, key: str, generation: int, event: threading.Event) -> None:
        if event.wait(_SINGLEFLIGHT_WAIT_SECONDS):
            return
        with self._lock:
            current = self._refresh_flights.get(key)
            if current is None or current[0] != generation:
                return
            self._refresh_generations[key] = generation + 1
            self._refresh_flights.pop(key, None)
            current[1].set()
        raise TimeoutError(f"{key}_refresh_timeout")

    def _complete_refresh(
        self,
        key: str,
        generation: int,
        commit: Callable[[], None] | None = None,
    ) -> bool:
        """Publish only the current generation and always release its waiters."""
        with self._lock:
            accepted = self._refresh_generations.get(key) == generation
            flight = self._refresh_flights.get(key)
            try:
                if accepted and commit is not None:
                    commit()
            finally:
                if flight is not None and flight[0] == generation:
                    self._refresh_flights.pop(key, None)
                    flight[1].set()
            return accepted

    def _refresh_seconds(self) -> int:
        now = _now()
        weekday = now.weekday() < 5
        minutes = now.hour * 60 + now.minute
        if weekday and ((570 <= minutes <= 690) or (780 <= minutes <= 900)):
            return 60
        if weekday and (420 <= minutes < 570 or 690 < minutes < 780):
            return 300
        if 900 < minutes <= 1440:
            return 600
        return 1800

    def start(self) -> bool:
        with self._lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return False
            self._stop_event.clear()
            self._worker_started = True
            self._calendar_refreshing = True
            worker = threading.Thread(target=self._run, name="market-intelligence", daemon=True)
            calendar = threading.Thread(
                target=self._refresh_macro_calendar, name="macro-calendar", daemon=True,
            )
            self._worker_thread = worker
            self._calendar_thread = calendar
        try:
            worker.start()
            calendar.start()
        except Exception:
            self._stop_event.set()
            for thread in (worker, calendar):
                if thread.is_alive():
                    thread.join(1.0)
            with self._lock:
                self._worker_started = False
                self._worker_thread = None
                self._calendar_thread = None
                self._calendar_refreshing = False
            raise
        return True

    def stop(self, timeout_seconds: float = 5.0) -> None:
        self._stop_event.set()
        with self._lock:
            threads = (self._worker_thread, self._calendar_thread)
        deadline = _time.monotonic() + max(0.0, timeout_seconds)
        for thread in threads:
            if thread is not None:
                thread.join(max(0.0, deadline - _time.monotonic()))
        with self._lock:
            if not self._worker_thread or not self._worker_thread.is_alive():
                self._worker_started = False
                self._worker_thread = None
            if not self._calendar_thread or not self._calendar_thread.is_alive():
                self._calendar_thread = None

    def _warm_once(self) -> None:
        self.global_radar()
        # Keep independent evidence warm without relying on a browser tab.
        # Each getter owns its own cache/rate limit; weekends do not cause
        # synthetic refresh traffic or change the dated source snapshot.
        if self._refresh_seconds() <= 600:
            self._market_snapshot()
            self._market_flow_history()
            self._northbound_history()

    def _run_cycle(self) -> bool:
        try:
            self._warm_once()
        except Exception as exc:
            with self._lock:
                self._worker_last_error = type(exc).__name__
                self._worker_iteration_at = _now()
            _LOG.exception("market intelligence warm cycle failed")
            return False
        with self._lock:
            self._worker_last_success_at = _now()
            self._worker_last_error = None
            self._worker_iteration_at = self._worker_last_success_at
        return True

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._run_cycle()
            # Wake every 30 seconds; _refresh_seconds controls actual source work.
            if self._stop_event.wait(30):
                break

    def health_snapshot(self) -> dict[str, Any]:
        """Return cheap process/cache health without triggering any source I/O."""
        now = _now()
        with self._lock:
            worker_alive = bool(self._worker_thread and self._worker_thread.is_alive())
            global_status = (self._global_cache or {}).get("status")
            cache_available = global_status in {"ok", "degraded"}
            worker_age = (
                int((now - self._worker_last_success_at).total_seconds())
                if self._worker_last_success_at is not None else None
            )
            global_age = (
                int((now - self._global_fetched_at).total_seconds())
                if self._global_fetched_at is not None else None
            )
            worker_deadline = 120
            global_deadline = max(120, self._refresh_seconds() + 60)
            worker_success_fresh = worker_age is not None and 0 <= worker_age <= worker_deadline
            global_cache_fresh = global_age is not None and 0 <= global_age <= global_deadline
            overdue = [
                name for name, fresh in (
                    ("worker_last_success", worker_success_fresh),
                    ("global_cache", global_cache_fresh),
                ) if not fresh
            ]
            healthy = (
                worker_alive and cache_available and self._worker_last_error is None
                and not overdue
            )
            return {
                "healthy": healthy,
                "worker_alive": worker_alive,
                "worker_started": self._worker_started,
                "worker_last_success_at": _iso(self._worker_last_success_at),
                "worker_last_success_age_seconds": worker_age,
                "worker_last_success_deadline_seconds": worker_deadline,
                "worker_iteration_at": _iso(self._worker_iteration_at),
                "worker_last_error": self._worker_last_error,
                "global_status": global_status or "missing",
                "global_fetched_at": _iso(self._global_fetched_at),
                "global_cache_age_seconds": global_age,
                "global_cache_deadline_seconds": global_deadline,
                "overdue_components": overdue,
                "calendar_fetched_at": _iso(self._calendar_fetched_at),
                "calendar_refreshing": self._calendar_refreshing,
                "calendar_last_error": self._calendar_last_error,
                "checked_at": _iso(now),
            }

    def _refresh_macro_calendar(self) -> None:
        try:
            payload = _official_macro_calendar(_now())
            with self._lock:
                self._calendar_cache = payload
                self._calendar_fetched_at = _now()
                self._calendar_last_error = None
        except Exception as exc:
            with self._lock:
                self._calendar_last_error = type(exc).__name__
            _LOG.exception("macro calendar refresh failed")
        finally:
            with self._lock:
                self._calendar_refreshing = False

    def _macro_calendar(self) -> dict[str, Any]:
        now = _now()
        with self._lock:
            cached = self._calendar_cache
            fetched_at = self._calendar_fetched_at
            refresh = (
                cached is None or fetched_at is None
                or now - fetched_at >= timedelta(hours=6)
            )
            if (refresh and not self._calendar_refreshing
                    and not self._stop_event.is_set()):
                self._calendar_refreshing = True
                thread = threading.Thread(
                    target=self._refresh_macro_calendar,
                    name="macro-calendar", daemon=True,
                )
                self._calendar_thread = thread
                try:
                    thread.start()
                except Exception as exc:
                    self._calendar_thread = None
                    self._calendar_refreshing = False
                    self._calendar_last_error = type(exc).__name__
                    _LOG.exception("macro calendar worker start failed")
            refreshing = self._calendar_refreshing
        if cached is None:
            return {
                "status": "refreshing" if refreshing else "unavailable",
                "items": [], "sources": [],
                "reason": (
                    "正在从官方宏观日历读取未来7天安排；加载完成前不展示推测事件。"
                    if refreshing else "宏观日历刷新任务未启动；稍后重试。"
                ),
                "participates_in_decision": False, "health_status": "missing",
                "as_of": None, "data_as_of": None, "latest_data_as_of": None,
                "fetched_at": None, "served_at": _iso(now), "served_from_cache": False,
            }
        payload = json.loads(json.dumps(cached))
        for source in payload.get("sources", []):
            _refresh_health(source, daily=True)
        for item in payload.get("items", []):
            _refresh_health(item, daily=True)
        states = {source.get("health_status") for source in payload.get("sources", [])}
        payload["status"] = "ok" if states == {"verified"} else ("partial" if states - {"upstream_error", "missing"} else "unavailable")
        payload["health_status"] = "verified" if payload["status"] == "ok" else "upstream_error"
        payload["fetched_at"] = _iso(fetched_at)
        payload["snapshot_fetched_at"] = _iso(fetched_at)
        payload["served_at"] = _iso(now)
        payload["served_from_cache"] = True
        return payload

    @_singleflight_safe
    def global_radar(self, force: bool = False) -> dict[str, Any]:
        now = _now()
        with self._lock:
            fresh = (
                not force
                and self._global_cache is not None
                and self._global_fetched_at is not None
                and (now - self._global_fetched_at).total_seconds() < self._refresh_seconds()
            )
            if fresh:
                cached = json.loads(json.dumps(self._global_cache))
                fetched_at = self._global_fetched_at
        if fresh:
            for asset in cached.get("assets", []):
                _refresh_health(asset, daily="FRED" in str(asset.get("source") or ""))
            available = sum(asset.get("health_status") in {"verified", "delayed"} for asset in cached.get("assets", []))
            verified = sum(asset.get("health_status") == "verified" for asset in cached.get("assets", []))
            delayed = available - verified
            expected = sum(bool(asset.get("expected_unavailable")) for asset in cached.get("assets", []))
            failed = len(cached.get("assets", [])) - available - expected
            cached["health"] = {
                "verified": verified, "delayed": delayed,
                "upstream_error_or_missing": failed, "expected_unavailable": expected,
                "user_visible_missing": failed + expected, "available": available,
                "total": len(cached.get("assets", [])),
            }
            cached["status"] = "ok" if failed + expected == 0 else ("degraded" if available else "unavailable")
            cached["events"] = evaluate_rules(cached.get("assets", []))
            cached["industry_heatmap"] = heatmap(cached["events"])
            trusted_times = [
                parsed for asset in cached.get("assets", [])
                if asset.get("health_status") in {"verified", "delayed"}
                and (parsed := _parse_iso(asset.get("as_of"))) is not None
            ]
            cached["as_of"] = cached["data_as_of"] = _iso(min(trusted_times)) if trusted_times else None
            cached["latest_data_as_of"] = _iso(max(trusted_times)) if trusted_times else None
            cached["served_from_cache"] = True
            cached["fetched_at"] = _iso(fetched_at)
            cached["snapshot_fetched_at"] = _iso(fetched_at)
            cached["served_at"] = _iso(now)
            cached["server_time"] = _iso(now)
            # 宏观日历是独立异步刷新：即使雷达快照未过期，也实时重读日历缓存，
            # 否则"加载中"状态会被嵌入旧快照而滞留整个雷达 TTL（夜间最长 30 分钟）。
            # （此调用需再取 self._lock，故须在锁外执行，避免非可重入锁自锁。）
            cached["macro_calendar"] = self._macro_calendar()
            return cached

        leader, generation, flight = self._claim_refresh("global")
        if not leader:
            self._wait_refresh("global", generation, flight)
            return self.global_radar(force=False)

        # 每资产沿其提供方链（_GLOBAL_ASSETS）逐源尝试：第一可用者胜出；全链失败 →
        # 错误资产（health=upstream_error）；显式空链（罗素2000/铁矿石/富时A50）→
        # expected_unavailable 占位不冒充传输错误，但仍计入用户可见缺失；绝不显示数字。
        try:
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(_fetch_global_asset, spec) for spec in _GLOBAL_ASSETS]
                assets = [future.result()[0] for future in as_completed(futures)]
        except BaseException:
            self._complete_refresh("global", generation)
            raise
        assets.sort(key=lambda item: (item["category"], item["name"]))
        events = evaluate_rules(assets)
        verified = sum(asset["health_status"] == "verified" for asset in assets)
        delayed = sum(asset["health_status"] == "delayed" for asset in assets)
        expected_unavailable = sum(
            1 for asset in assets if asset.get("expected_unavailable")
        )
        failed = len(assets) - verified - delayed - expected_unavailable
        user_visible_missing = failed + expected_unavailable
        trusted_times = [
            parsed for asset in assets
            if asset.get("health_status") in {"verified", "delayed"}
            and (parsed := _parse_iso(asset.get("as_of"))) is not None
        ]
        last_trusted_at = max(trusted_times) if trusted_times else self._last_trusted_global_at
        payload = {
            "status": "ok" if user_visible_missing == 0 else ("degraded" if verified + delayed else "unavailable"),
            "server_time": _iso(now),
            "as_of": _iso(min(trusted_times)) if trusted_times else None,
            "data_as_of": _iso(min(trusted_times)) if trusted_times else None,
            "latest_data_as_of": _iso(max(trusted_times)) if trusted_times else None,
            "fetched_at": _iso(now),
            "snapshot_fetched_at": _iso(now),
            "served_at": _iso(now),
            "served_from_cache": False,
            "refresh_policy_seconds": self._refresh_seconds(),
            "last_trusted_at": _iso(last_trusted_at),
            "assets": assets,
            "health": {"verified": verified, "delayed": delayed, "upstream_error_or_missing": failed,
                       "expected_unavailable": expected_unavailable,
                       "user_visible_missing": user_visible_missing,
                       "available": verified + delayed,
                       "total": len(assets)},
            "events": events,
            "industry_heatmap": heatmap(events),
            "macro_calendar": self._macro_calendar(),
            "decision_policy": "全球雷达仅供人工观察；所有规则命中率尚未独立验证，不参与买卖评分。",
        }
        self._complete_refresh(
            "global", generation,
            lambda: (
                setattr(self, "_global_cache", payload),
                setattr(self, "_global_fetched_at", now),
                setattr(self, "_last_trusted_global_at", last_trusted_at),
            ),
        )
        return payload

    # ── B1 板块实时（东方财富，push2delay 延迟源）───────────────────────

    def _eastmoney_sector_page(self, fs: str, page: int) -> dict[str, Any] | None:
        # A changing return rank can move boards across pages mid-request.
        # Retrieve by stable identity; rank the complete display locally.
        params: dict[str, str] = {
            "pn": str(page), "pz": "100", "po": "1", "np": "1", "ut": _EASTMONEY_UT,
            "fltt": "2", "invt": "2", "fid": "f12", "fs": fs, "fields": _SECTORS_FIELDS,
        }
        try:
            payload = _direct_eastmoney_json(_EASTMONEY_SECTORS_URL, params)
        except RuntimeError as exc:
            failures = (list(exc.failures) if isinstance(exc, EastmoneyFetchError)
                        else ["request_failed"])
            return {"_source_request_failures": failures}
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("diff"), list):
            return {"_source_request_failures": ["invalid_payload"]}
        return {"total": data.get("total"), "diff": data["diff"]}

    def _collect_sector_pages(self, fs: str, cap: int = 520) -> tuple[_SectorRows, int | None]:
        """Stable-code pages; bounded parallelism avoids a partial serial deadline.

        At most six pages per kind and three requests at once. Each request
        retains the shared native timeout and retry ceiling.
        """
        errors: list[dict[str, Any]] = []

        def unwrap(page: int, result: dict[str, Any] | None) -> dict[str, Any] | None:
            if isinstance(result, dict) and "_source_request_failures" in result:
                errors.append({"page": page, "attempts": result["_source_request_failures"]})
                return None
            return result

        first = unwrap(1, self._eastmoney_sector_page(fs, 1))
        if first is None or type(first["total"]) is not int or first["total"] <= 0:
            return _SectorRows([], errors), None
        total = first["total"]
        rows: list[dict[str, Any]] = []
        page_count = min(6, (total+99)//100, (cap+99)//100)
        with ThreadPoolExecutor(max_workers=3) as executor:
            pending = {page: executor.submit(self._eastmoney_sector_page, fs, page) for page in range(2, page_count+1)}
            results = [(1, first)] + [(page, unwrap(page, future.result()))
                                    for page, future in pending.items()]
        for page, result in results:
            if result is None:
                return _SectorRows(rows, errors), total
            raw_total = result["total"]
            if type(raw_total) is not int or raw_total <= 0:
                return _SectorRows(rows, errors), None
            if raw_total != total:
                return _SectorRows(rows, errors), None
            total = raw_total
            batch = result["diff"]
            rows.extend(batch)
            if len(batch) != min(100, total-(page-1)*100):
                return _SectorRows(rows, errors), None
        return _SectorRows(rows, errors), total

    @_singleflight_safe
    def market_sectors(self, force: bool = False) -> dict[str, Any]:
        now = _now()
        with self._lock:
            if (
                not force
                and self._sectors_cache is not None
                and self._sectors_fetched_at is not None
                and (now - self._sectors_fetched_at).total_seconds() < _SECTORS_CACHE_SECONDS
            ):
                cached = json.loads(json.dumps(self._sectors_cache))
                _refresh_health(cached)
                complete = cached.get("meta", {}).get("coverage_complete") is True
                if cached.get("health_status") in {"upstream_error", "missing"}:
                    cached["status"] = "unavailable"
                elif complete and cached.get("health_status") == "verified" and cached.get("industries") and cached.get("concepts"):
                    cached["status"] = "ok"
                elif cached.get("industries") or cached.get("concepts"):
                    cached["status"] = "degraded"
                cached["served_from_cache"] = True
                cached["served_at"] = now.isoformat(timespec="microseconds")
                cached["server_time"] = cached["served_at"]
                return cached

        leader, generation, flight = self._claim_refresh("sectors")
        if not leader:
            self._wait_refresh("sectors", generation, flight)
            return self.market_sectors(force=False)

        def normalize(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            seen: set[str] = set()
            for it in raw:
                if not isinstance(it, dict):
                    continue
                code = str(it.get("f12") or "").strip()
                name = str(it.get("f14") or "").strip()
                if not code or not name or code in seen:
                    continue
                seen.add(code)
                main_net_yi = _safe_number(it.get("f62"))
                up_count = _safe_number(it.get("f104"))
                down_count = _safe_number(it.get("f105"))
                data_as_of = _as_of_from_epoch(it.get("f124"))
                out.append({
                    "code": code,
                    "name": name,
                    "change_pct": _safe_number(it.get("f3")),
                    "main_net_yi": round(main_net_yi / 1e8, 3) if main_net_yi is not None else None,
                    "main_net_pct": _safe_number(it.get("f184")),
                    "up_count": int(up_count) if up_count is not None else None,
                    "down_count": int(down_count) if down_count is not None else None,
                    "data_as_of": _iso(data_as_of),
                })
            return sorted(out, key=lambda row: (row["change_pct"] is None, -(row["change_pct"] or 0), row["code"]))

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                industry_future = executor.submit(self._collect_sector_pages, "m:90+t:2")
                concept_future = executor.submit(self._collect_sector_pages, "m:90+t:3")
                industry_raw, industry_total = industry_future.result()
                concept_raw, concept_total = concept_future.result()
        except BaseException:
            self._complete_refresh("sectors", generation)
            raise
        source_request_errors = [
            {"kind": kind, **entry}
            for kind, rows in (("industry", industry_raw), ("concept", concept_raw))
            for entry in getattr(rows, "request_errors", [])[:6]
        ]
        industries = normalize(industry_raw)
        concepts = normalize(concept_raw)
        source_times = [
            _parse_iso(row.get("data_as_of"))
            for row in industries + concepts
            if row.get("data_as_of")
        ]
        source_times = [value for value in source_times if value is not None]
        earliest_data_as_of = min(source_times) if source_times else None
        latest_data_as_of = max(source_times) if source_times else None
        ok_industry, ok_concept = bool(industries), bool(concepts)
        # A repeated page or dropped malformed row is not full cross-section coverage.
        industry_complete = (type(industry_total) is int and industry_total > 0
                             and len(industry_raw) == len(industries) == industry_total)
        concept_complete = (type(concept_total) is int and concept_total > 0
                            and len(concept_raw) == len(concepts) == concept_total)
        timestamp_complete = len(source_times) == len(industries) + len(concepts)
        up_count_returned = sum(row.get("up_count") is not None for row in industries + concepts)
        down_count_returned = sum(row.get("down_count") is not None for row in industries + concepts)
        breadth_count_complete = (
            up_count_returned == down_count_returned == len(industries) + len(concepts)
        )
        coverage_complete = (
            industry_complete and concept_complete and timestamp_complete
            and breadth_count_complete
        )
        # A fresh last page must not hide an old/missing earlier page.
        source_health = _source(
            "东方财富板块实时（push2delay 延迟源）", earliest_data_as_of,
            calendar="a_share",
        )
        if source_health["health_status"] in {"upstream_error", "missing"}:
            status = "unavailable"
        elif ok_industry and ok_concept and coverage_complete and source_health["health_status"] == "verified":
            status = "ok"
        elif ok_industry or ok_concept:
            status = "degraded"
        else:
            status = "unavailable"
        # Retrieval completion is distinct from both the entry clock and the
        # supplier clocks. Retain fractions so a same-second caller can verify
        # this attempt without rounding a real fetch back before its start.
        fetched_at = _now()
        payload = {
            "status": status,
            "source": "东方财富板块实时（push2delay 延迟源）",
            "as_of": _iso(earliest_data_as_of),
            "data_as_of": _iso(earliest_data_as_of),
            "latest_data_as_of": _iso(latest_data_as_of),
            "fetch_started_at": now.isoformat(timespec="microseconds"),
            "fetched_at": fetched_at.isoformat(timespec="microseconds"),
            "served_at": fetched_at.isoformat(timespec="microseconds"),
            "server_time": fetched_at.isoformat(timespec="microseconds"),
            "refresh_policy_seconds": _SECTORS_CACHE_SECONDS,
            "served_from_cache": False,
            "health_status": source_health["health_status"],
            "health_calendar": source_health["health_calendar"],
            "latency_seconds": source_health["latency_seconds"],
            "error": source_health["error"],
            "participates_in_decision": False,
            "source_request_errors": source_request_errors,
            "industries": industries,
            "concepts": concepts,
            "meta": {
                "industry_reported_total": industry_total,
                "concept_reported_total": concept_total,
                "industry_fetched": len(industry_raw),
                "concept_fetched": len(concept_raw),
                "industry_unique_valid": len(industries),
                "concept_unique_valid": len(concepts),
                "industry_complete": industry_complete,
                "concept_complete": concept_complete,
                "timestamp_complete": timestamp_complete,
                "timestamp_returned": len(source_times),
                "breadth_count_complete": breadth_count_complete,
                "up_count_returned": up_count_returned,
                "down_count_returned": down_count_returned,
                "coverage_complete": coverage_complete,
                "earliest_data_as_of": _iso(earliest_data_as_of),
                "latest_data_as_of": _iso(latest_data_as_of),
            },
            "note": "板块聚合自延迟行情源；请求失败类别见 source_request_errors，网关归属未核。热力图颜色=涨跌幅 红涨绿跌，与资金流热力图（绿正红负）分开。",
        }
        self._complete_refresh(
            "sectors", generation,
            lambda: (
                setattr(self, "_sectors_cache", payload),
                setattr(self, "_sectors_fetched_at", fetched_at),
            ),
        )
        return payload

    # ── B2 实时新闻（东方财富快讯/要闻/公告，新浪 7×24 兜底）───────────

    @staticmethod
    def _news_time(value: object) -> str:
        if isinstance(value, str):
            text = value.strip()
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    return datetime.strptime(text, fmt).replace(tzinfo=SH_TZ).isoformat()
                except ValueError:
                    continue
            return text
        return ""

    @staticmethod
    def _eastmoney_stock_codes(stock_list: object) -> list[str]:
        """快讯 stockList 形如 ['0.160628','1.512200'] → ['160628','512200']。"""
        codes: list[str] = []
        if isinstance(stock_list, list):
            for entry in stock_list:
                code = str(entry or "").split(".")[-1].strip()
                if code and code not in codes:
                    codes.append(code)
        return codes

    @staticmethod
    def _eastmoney_fast_news() -> list[dict[str, Any]]:
        params = {
            "client": "web", "biz": "web_724", "fastColumn": "102",
            "sortEnd": "", "pageSize": "20", "req_trace": "1", "pagesize": "20",
        }
        payload = _direct_eastmoney_json(_EASTMONEY_FAST_NEWS_URL, params)
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("fastNewsList"), list):
            raise RuntimeError("fast_news_schema")
        return data["fastNewsList"]

    @staticmethod
    def _eastmoney_headline_news() -> list[dict[str, Any]]:
        params = {
            "client": "web", "biz": "web_724", "column": "350", "order": "1",
            "needInteractData": "0", "page_index": "1", "page_size": "20", "req_trace": "1",
        }
        payload = _direct_eastmoney_json(_EASTMONEY_NEWS_COLUMNS_URL, params)
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise RuntimeError("headline_news_schema")
        return data["list"]

    @staticmethod
    def _eastmoney_announcements() -> list[dict[str, Any]]:
        params = {
            "sr": "-1", "page_size": "20", "page_index": "1", "ann_type": "A",
            "client_source": "web", "f_node": "0", "s_node": "0",
        }
        payload = _direct_eastmoney_json(_EASTMONEY_ANNOUNCEMENTS_URL, params)
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise RuntimeError("announcements_schema")
        return data["list"]

    @staticmethod
    def _sina_live_items() -> list[dict[str, Any]]:
        """新浪 7×24 兜底。周末 feed 可能为空；schema 一旦变更最坏只返回空，绝不伪造。"""
        params = {"page": 1, "page_size": 20, "zhibo_id": 152, "tag_id": 0, "dire": "f", "dpc": 1, "type": 0}
        session = requests.Session()
        try:
            session.trust_env = False
            response = session.get(
                _SINA_LIVE_URL, params=params,
                timeout=_SOURCE_REQUEST_TIMEOUT_SECONDS, verify=certifi.where(),
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(type(exc).__name__) from exc
        finally:
            session.close()
        feed = (((payload.get("result") or {}).get("data") or {}).get("feed") or {}).get("list") or []
        items: list[dict[str, Any]] = []
        for entry in feed:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("rich_text") or "").strip()
            if not title:
                continue
            items.append({
                "title": title,
                "summary": "",
                "time": MarketIntelligence._news_time(entry.get("create_time")),
                "source": "新浪财经·7×24",
                "tag": "快讯",
                "url": str(entry.get("url") or ""),
                "stocks": [],
            })
        return items

    @_singleflight_safe
    def news_live(self, force: bool = False) -> dict[str, Any]:
        now = _now()
        with self._lock:
            if (
                not force
                and self._news_cache is not None
                and self._news_fetched_at is not None
                and (now - self._news_fetched_at).total_seconds() < _NEWS_CACHE_SECONDS
            ):
                cached = json.loads(json.dumps(self._news_cache))
                _refresh_health(cached)
                if cached.get("health_status") in {"upstream_error", "missing"}:
                    cached["status"] = "unavailable"
                elif (
                    cached.get("meta", {}).get("source_failures")
                    or cached.get("meta", {}).get("future_timestamp_count")
                    or cached.get("meta", {}).get("timestamp_complete") is not True
                ):
                    cached["status"] = "partial"
                elif cached.get("items") and cached.get("health_status") == "verified":
                    cached["status"] = "ok"
                elif cached.get("items"):
                    cached["status"] = "degraded"
                cached["served_from_cache"] = True
                cached["served_at"] = _iso(now)
                cached["server_time"] = _iso(now)
                return cached

        leader, generation, flight = self._claim_refresh("news")
        if not leader:
            self._wait_refresh("news", generation, flight)
            return self.news_live(force=False)

        items: list[dict[str, Any]] = []
        source_failures: dict[str, str] = {}

        def load(name: str, loader: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
            try:
                return loader()
            except Exception as exc:
                source_failures[name] = type(exc).__name__
                return []

        loaders = {
            "fast_news": self._eastmoney_fast_news,
            "headline_news": self._eastmoney_headline_news,
            "announcements": self._eastmoney_announcements,
        }
        with ThreadPoolExecutor(max_workers=len(loaders)) as executor:
            futures = {name: executor.submit(loader) for name, loader in loaders.items()}
            batches = {name: load(name, future.result) for name, future in futures.items()}

        for entry in batches["fast_news"][:20]:
            code = str(entry.get("code") or "")
            items.append({
                "title": str(entry.get("title") or "").strip(),
                "summary": str(entry.get("summary") or "").strip(),
                "time": self._news_time(entry.get("showTime")),
                "source": "东方财富·7×24快讯",
                "tag": "快讯",
                "url": f"https://finance.eastmoney.com/a/{code}.html" if code else "",
                "stocks": self._eastmoney_stock_codes(entry.get("stockList")),
            })
        for entry in batches["headline_news"][:10]:
            code = str(entry.get("code") or "")
            items.append({
                "title": str(entry.get("title") or "").strip(),
                "summary": str(entry.get("summary") or "").strip(),
                "time": self._news_time(entry.get("showTime")),
                "source": "东方财富·要闻",
                "tag": "要闻",
                "url": f"https://finance.eastmoney.com/a/{code}.html" if code else "",
                "stocks": [],
            })
        for entry in batches["announcements"][:8]:
            companies = [
                f"{c.get('short_name') or ''} {c.get('stock_code') or ''}".strip()
                for c in (entry.get("codes") or []) if isinstance(c, dict)
            ]
            title = str(entry.get("title") or "").strip()
            if not title:
                title = f"{'、'.join(companies)} 公告" if companies else "上市公司公告"
            first_stock = companies[0].split(" ")[-1] if companies else ""
            art_code = str(entry.get("art_code") or "")
            items.append({
                "title": title,
                "summary": "、".join(companies) if companies else "",
                "time": self._news_time(entry.get("display_time")),
                "effective_date": str(entry.get("notice_date") or "").strip() or None,
                "source": "东方财富·公告",
                "tag": "公告",
                "url": f"https://data.eastmoney.com/notices/detail/{first_stock}/{art_code}.html"
                if first_stock and art_code else "",
                "stocks": [c.get("stock_code") or "" for c in (entry.get("codes") or []) if isinstance(c, dict)],
            })

        source_counts = {
            "快讯": sum(1 for it in items if it["tag"] == "快讯"),
            "要闻": sum(1 for it in items if it["tag"] == "要闻"),
            "公告": sum(1 for it in items if it["tag"] == "公告"),
        }
        source_label = "东方财富（快讯/要闻/公告）"
        if not items:
            fallback = load("sina_fallback", self._sina_live_items)
            items = fallback
            source_counts = {"sina_fallback": len(fallback)}
            source_label = "新浪财经·7×24兜底"

        raw_count = len(items)
        deduplicated: list[dict[str, Any]] = []
        seen: set[str] = set()
        future_timestamp_count = 0
        for item in sorted(items, key=lambda value: str(value.get("time") or ""), reverse=True):
            item_time = _parse_iso(item.get("time"))
            if item_time is not None and item_time > now + timedelta(seconds=60):
                future_timestamp_count += 1
                continue
            title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
            if not title:
                continue
            dedupe_material = re.sub(r"\s+", "", title).casefold()
            dedupe_key = hashlib.sha256(dedupe_material.encode("utf-8")).hexdigest()[:20]
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            summary = re.sub(r"\s+", " ", str(item.get("summary") or "")).strip()
            summary_truncated = len(summary) > _NEWS_SUMMARY_MAX_CHARS
            cleaned = dict(item)
            cleaned.update({
                "id": dedupe_key,
                "dedupe_key": dedupe_key,
                "title": title,
                "summary": summary[:_NEWS_SUMMARY_MAX_CHARS],
                "summary_truncated": summary_truncated,
                "source_confidence": (
                    "disclosure_aggregator" if item.get("tag") == "公告" else "publisher_feed"
                ),
                "participates_in_decision": False,
                **_news_assessment(cleaned),
            })
            deduplicated.append(cleaned)

        returned = deduplicated[:_NEWS_ITEM_LIMIT]
        data_times = [
            parsed for item in returned
            if (parsed := _parse_iso(item.get("time"))) is not None
        ]
        earliest_data_as_of = min(data_times) if data_times else None
        latest_data_as_of = max(data_times) if data_times else None
        health_status, latency_seconds = _health_for(earliest_data_as_of)
        timestamp_complete = len(data_times) == len(returned)
        payload = {
            "status": (
                "unavailable" if health_status in {"upstream_error", "missing"}
                else "partial" if returned and (source_failures or future_timestamp_count or not timestamp_complete)
                else "ok" if returned and timestamp_complete and health_status == "verified"
                else "degraded" if returned and timestamp_complete and health_status == "delayed"
                else "degraded" if returned else "unavailable"
            ),
            "source": source_label,
            "as_of": _iso(earliest_data_as_of),
            "data_as_of": _iso(earliest_data_as_of),
            "latest_data_as_of": _iso(latest_data_as_of),
            "fetched_at": _iso(now),
            "served_at": _iso(now),
            "server_time": _iso(now),
            "refresh_policy_seconds": _NEWS_CACHE_SECONDS,
            "served_from_cache": False,
            "health_status": health_status,
            "latency_seconds": latency_seconds,
            "participates_in_decision": False,
            "items": returned,
            "meta": {
                **source_counts,
                "raw_count": raw_count,
                "deduplicated_count": len(deduplicated),
                "returned_count": len(returned),
                "limit": _NEWS_ITEM_LIMIT,
                "truncated_count": max(0, len(deduplicated) - len(returned)),
                "summary_max_chars": _NEWS_SUMMARY_MAX_CHARS,
                "timestamp_complete": timestamp_complete,
                "timestamp_returned": len(data_times),
                "future_timestamp_count": future_timestamp_count,
                "earliest_data_as_of": _iso(earliest_data_as_of),
                "latest_data_as_of": _iso(latest_data_as_of),
                "source_failures": source_failures,
                "material_a_share_candidates": sum(
                    item.get("attention_class") == "material_a_share_candidate"
                    for item in returned
                ),
                "unassessed_attention_items": sum(
                    item.get("attention_class") == "unassessed" for item in returned
                ),
            },
            "note": "新闻逐条披露发布时间与公告生效日期；未来发布时间会隔离。重大性与 A 股相关性未命中即标未评估，不冒充重大新闻。财联社/北向官方源不可达未接入；新浪 7×24 仅作兜底。",
        }
        self._complete_refresh(
            "news", generation,
            lambda: (
                setattr(self, "_news_cache", payload),
                setattr(self, "_news_fetched_at", now),
            ),
        )
        return payload

    _INDEX_SPARK_SPECS = (
        ("sh000001", "上证综指"), ("sz399001", "深证成指"), ("sz399006", "创业板指"),
        ("sh000300", "沪深300"), ("sh000905", "中证500"), ("sh000852", "中证1000"),
        ("sh000688", "科创50"), ("sh000016", "上证50"),
    )

    @_singleflight_safe
    def index_sparks(self, force: bool = False) -> dict[str, Any]:
        """八大指数实时报价 + 近1月收盘序列（迷你走势线），300s 缓存。

        实时报价走 quote_provider，走势线复用 _history 的腾讯指数日线 30min 缓存；
        任一指数失败不影响其余，缺失项如实缺省。"""
        now = _now()
        with self._lock:
            if (
                not force
                and self._spark_cache is not None
                and self._spark_fetched_at is not None
                and (now - self._spark_fetched_at).total_seconds() < 300
            ):
                cached = json.loads(json.dumps(self._spark_cache))
                for item in cached.get("items", []):
                    _refresh_health(item)
                usable = sum(bool(item.get("spark")) or item.get("price") is not None for item in cached.get("items", []))
                complete = sum(
                    bool(item.get("spark")) and item.get("price") is not None
                    and item.get("health_status") in {"verified", "delayed"}
                    for item in cached.get("items", [])
                )
                cached["status"] = "ok" if complete == len(self._INDEX_SPARK_SPECS) else ("degraded" if usable else "unavailable")
                cached["coverage_returned"] = usable
                cached["coverage_complete"] = complete
                cached["served_from_cache"] = True
                cached["served_at"] = _iso(now)
                cached["server_time"] = _iso(now)
                return cached
        leader, generation, flight = self._claim_refresh("spark")
        if not leader:
            self._wait_refresh("spark", generation, flight)
            return self.index_sparks(force=False)
        try:
            quotes = self.quote_provider.get_quotes(list(code for code, _ in self._INDEX_SPARK_SPECS))
        except MarketDataUnavailable:
            quotes = {}
        except BaseException:
            self._complete_refresh("spark", generation)
            raise
        items: list[dict[str, Any]] = []
        for code, name in self._INDEX_SPARK_SPECS:
            quote = (quotes or {}).get(code)
            rows, history_source = self._history(code)
            history_as_of = _parse_iso(history_source.get("as_of"))
            item = {
                "code": code,
                "name": name,
                "price": getattr(quote, "price", None),
                "change_pct": getattr(quote, "change_pct", None),
                "spark": [round(row["close"], 3) for row in rows[-40:]],
            }
            if quote is not None:
                quote_as_of = _parse_iso(getattr(quote, "exchange_data_time", None)) or _parse_iso(getattr(quote, "data_time", None))
                component_times = [value for value in (quote_as_of, history_as_of) if value is not None]
                item.update(_source(
                    "腾讯财经公开指数日线 + 实时报价",
                    min(component_times) if component_times else None,
                    calendar="a_share",
                ))
                item.update({
                    "quote_as_of": _iso(quote_as_of),
                    "history_as_of": _iso(history_as_of),
                    "latest_data_as_of": _iso(max(component_times)) if component_times else None,
                })
                if getattr(quote, "freshness", None) == "stale" and item["health_status"] == "verified":
                    item["health_status"] = "delayed"
            else:
                item.update(_source(
                    "腾讯财经公开指数日线 + 实时报价", None,
                    error="quote_missing", calendar="a_share",
                ))
                item.update({"quote_as_of": None, "history_as_of": _iso(history_as_of), "latest_data_as_of": _iso(history_as_of)})
            items.append(item)
        usable = sum(bool(item.get("spark")) or item.get("price") is not None for item in items)
        complete = sum(
            bool(item.get("spark")) and item.get("price") is not None
            and item.get("health_status") in {"verified", "delayed"}
            for item in items
        )
        data_times = [parsed for item in items if (parsed := _parse_iso(item.get("as_of"))) is not None]
        latest_data_times = [
            parsed for item in items
            if (parsed := _parse_iso(item.get("latest_data_as_of"))) is not None
        ]
        payload = {
            "status": "ok" if complete == len(self._INDEX_SPARK_SPECS) else ("degraded" if usable else "unavailable"),
            "source": "腾讯财经公开指数日线 + 实时报价",
            "as_of": _iso(min(data_times)) if data_times else None,
            "data_as_of": _iso(min(data_times)) if data_times else None,
            "latest_data_as_of": _iso(max(latest_data_times)) if latest_data_times else None,
            "fetched_at": _iso(now),
            "served_at": _iso(now),
            "server_time": _iso(now),
            "refresh_policy_seconds": 300,
            "served_from_cache": False,
            "coverage_expected": len(self._INDEX_SPARK_SPECS),
            "coverage_returned": usable,
            "coverage_complete": complete,
            "items": items,
        }
        self._complete_refresh(
            "spark", generation,
            lambda: (
                setattr(self, "_spark_cache", payload),
                setattr(self, "_spark_fetched_at", now),
            ),
        )
        return payload

    @_singleflight_safe
    def _history(self, code: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        now = _now()
        with self._lock:
            if (
                code in self._history_cache
                and code in self._history_fetched_at
                and now - self._history_fetched_at[code] < timedelta(minutes=30)
            ):
                rows, source = self._history_cache[code]
                source = dict(source)
                _refresh_health(source, daily=True)
                return rows, _served_source(
                    source, self._history_fetched_at[code], now, cached=True,
                )
        key = f"history:{code}"
        leader, generation, flight = self._claim_refresh(key)
        if not leader:
            self._wait_refresh(key, generation, flight)
            return self._history(code)
        try:
            rows, source = _tencent_index_history(code)
            result = (rows, _served_source(source, now, now, cached=False))
        except BaseException:
            self._complete_refresh(key, generation)
            raise

        def commit() -> None:
            self._history_cache[code] = result
            self._history_fetched_at[code] = now

        self._complete_refresh(key, generation, commit)
        return result

    @_singleflight_safe
    def _market_snapshot(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        now = _now()
        with self._lock:
            if self._market_cache and self._market_fetched_at and now - self._market_fetched_at < timedelta(seconds=self._refresh_seconds()):
                rows, source = self._market_cache
                source = dict(source)
                _refresh_health(source)
                return rows, _served_source(source, self._market_fetched_at, now, cached=True)
        leader, generation, flight = self._claim_refresh("market")
        if not leader:
            self._wait_refresh("market", generation, flight)
            return self._market_snapshot()
        reference_meta = {
            "reference_universe_name": "本机资金流历史快照证券代码清单",
            "reference_universe_source": "本机资金流历史快照的 stocks 键",
            "reference_universe_as_of": None,
        }
        reference_failure_stage = "fund_flow_reference_read"
        try:
            try:
                raw = json.loads(self.fund_flow_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                # Fund flow is an independent observation, not a prerequisite
                # for a quote reference universe on a new workstation.
                from .market_reference_universe import load_snapshot

                reference_meta = {
                    "reference_universe_name": "独立沪深京 A 股参考代码清单",
                    "reference_universe_source": "AKShare stock_zh_a_spot (Sina A-share snapshot)",
                    "reference_universe_as_of": None,
                }
                reference_failure_stage = "independent_reference_read"
                reference = load_snapshot(
                    self.fund_flow_path.parents[1] / "reports" / "market_reference_universe.json"
                )
                codes = [symbol[2:] for symbol in reference["symbols"]]
                group_counts = {key: 0 for key, _ in _REFERENCE_GROUPS}
                for code in codes:
                    group_counts[_reference_group(code)] = group_counts.get(_reference_group(code), 0) + 1
                reference_meta = {
                    "reference_universe_name": "独立沪深京 A 股参考代码清单",
                    "reference_universe_source": reference["provider"],
                    "reference_universe_as_of": reference["captured_at"],
                    "reference_universe_sha256": reference["symbols_sha256"],
                    "reference_universe_segments": reference["segments"],
                    "reference_universe_code_groups": group_counts,
                    "universe_code_source": "仅用于请求逐证券报价；抓取时间不是报价时间，未与交易所上市全集逐项核对。",
                    "universe_code_as_of": reference["captured_at"],
                }
                reference_failure_stage = "quote_batches"
            else:
                if not isinstance(raw, dict) or not isinstance(raw.get("stocks"), dict):
                    raise ValueError("fund_flow_reference_schema_invalid")
                from .market_reference_universe import is_a_share_numeric_code

                codes = sorted(str(code) for code in raw["stocks"] if is_a_share_numeric_code(str(code)))
                group_counts = {key: 0 for key, _ in _REFERENCE_GROUPS}
                for code in codes:
                    group_counts[_reference_group(code)] = group_counts.get(_reference_group(code), 0) + 1
                reference_meta = {
                    "reference_universe_name": "本机资金流历史快照证券代码清单",
                    "reference_universe_source": "本机资金流历史快照的 stocks 键",
                    "reference_universe_as_of": raw.get("ts"),
                    "reference_universe_code_groups": group_counts,
                    "universe_code_source": "本机历史快照仅提供参考证券代码清单；不读取其资金字段，也不代表交易所完整上市证券全集。",
                    "universe_code_as_of": raw.get("ts"),
                }
                reference_failure_stage = "quote_batches"
            quotes, source = _tencent_market_quotes(codes)
            source.update({
                **reference_meta,
                "exchange_coverage_status": "unknown",
                "exchange_universe_expected": None,
                "exchange_coverage_ratio": None,
            })
        except Exception as exc:
            quotes, source = [], _source(
                "腾讯财经公开行情（参考代码清单批量）", None,
                error=type(exc).__name__, calendar="a_share",
            )
            source.update({
                "coverage_scope": "requested_reference_universe",
                **reference_meta,
                "reference_failure_stage": reference_failure_stage,
                "reference_failure_type": type(exc).__name__,
                "exchange_coverage_status": "unknown",
                "exchange_universe_expected": None,
                "exchange_coverage_ratio": None,
            })
        result = (quotes, _served_source(source, now, now, cached=False))
        self._complete_refresh(
            "market", generation,
            lambda: (
                setattr(self, "_market_cache", result),
                setattr(self, "_market_fetched_at", now),
            ),
        )
        return result

    @_singleflight_safe
    def _market_flow_history(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        now = _now()
        with self._lock:
            if self._flow_cache and self._flow_fetched_at and now - self._flow_fetched_at < timedelta(minutes=10):
                rows, source = self._flow_cache
                source = dict(source)
                _refresh_health(source, daily=True)
                return rows, _served_source(source, self._flow_fetched_at, now, cached=True)
        leader, generation, flight = self._claim_refresh("flow")
        if not leader:
            self._wait_refresh("flow", generation, flight)
            return self._market_flow_history()
        try:
            rows, source = _eastmoney_market_flow_history()
            result = (rows, _served_source(source, now, now, cached=False))
        except BaseException:
            self._complete_refresh("flow", generation)
            raise
        self._complete_refresh(
            "flow", generation,
            lambda: (
                setattr(self, "_flow_cache", result),
                setattr(self, "_flow_fetched_at", now),
            ),
        )
        return result

    @_singleflight_safe
    def _northbound_history(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        now = _now()
        with self._lock:
            if self._northbound_cache and self._northbound_fetched_at and now - self._northbound_fetched_at < timedelta(minutes=30):
                rows, source = self._northbound_cache
                source = dict(source)
                _refresh_health(source, daily=True)
                return rows, _served_source(source, self._northbound_fetched_at, now, cached=True)
        leader, generation, flight = self._claim_refresh("northbound")
        if not leader:
            self._wait_refresh("northbound", generation, flight)
            return self._northbound_history()
        try:
            rows, source = _eastmoney_northbound_history()
            result = (rows, _served_source(source, now, now, cached=False))
        except BaseException:
            self._complete_refresh("northbound", generation)
            raise
        self._complete_refresh(
            "northbound", generation,
            lambda: (
                setattr(self, "_northbound_cache", result),
                setattr(self, "_northbound_fetched_at", now),
            ),
        )
        return result

    def _flow_summary(self) -> dict[str, Any]:
        rows, source = self._market_flow_history()
        source = dict(source)
        _refresh_health(source, daily=True)
        valid = sorted(
            (row for row in rows if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(row.get("date") or "")) and _safe_number(row.get("main_net_yi")) is not None),
            key=lambda row: row["date"],
        )
        if not valid:
            return {**source, "status": "unavailable", "reason": "东方财富公开大盘资金流未返回有效观察。"}
        latest = valid[-1]
        current_month = _now().strftime("%Y-%m")
        month_rows = [row for row in valid if row["date"].startswith(current_month)]
        recent = valid[-5:]
        mtd = round(sum(row["main_net_yi"] for row in month_rows), 2) if month_rows else None
        five_day = round(sum(row["main_net_yi"] for row in recent), 2) if len(recent) == 5 else None
        if mtd is None or five_day is None:
            label = "数据不足"
        elif mtd > 0 and five_day > 0:
            label = "持续流入"
        elif mtd < 0 and five_day < 0:
            label = "持续流出"
        else:
            label = "流向分化"
        north_rows, north_source = self._northbound_history()
        north_available = [row for row in north_rows if row.get("net_buy_available")]
        complete = mtd is not None and five_day is not None and source.get("health_status") in {"verified", "delayed"}
        if not complete:
            label = "数据不足"
        return {
            **source,
            "status": "ok" if complete else "partial",
            "as_of_date": latest["date"], "main_net_yi": latest.get("main_net_yi"),
            "main_net_pct": latest.get("main_net_pct"), "five_day_main_net_yi": five_day,
            "mtd_main_net_yi": mtd, "label": label, "history_rows": len(rows),
            "five_day_observation_count": len(recent),
            "mtd_observation_count": len(month_rows),
            "mtd_month": current_month,
            "northbound": {
                "status": "ok" if north_available else "unavailable",
                "net_buy_yi": north_available[-1]["net_buy_yi"] if north_available else None,
                "reason": None if north_available else "上游当前未返回北向成交净买额；不会将缺值解释为零。",
                **north_source,
            },
        }

    def _fund_flow(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not self.fund_flow_path.exists():
            return [], {
                "source_name": "本机资金流快照",
                "source_time": None,
                "freshness": {"state": "missing"},
                "error": "snapshot_missing",
            }
        try:
            return self.fund_flow_loader(self.fund_flow_path)
        except Exception as exc:
            return [], {
                "source_name": "本机资金流快照",
                "source_time": None,
                "freshness": {"state": "invalid"},
                "error": type(exc).__name__,
            }

    @staticmethod
    def _breadth_dimension(cross: dict[str, Any]) -> dict[str, Any]:
        if not cross.get("sample_size"):
            return {**cross, "status": "unavailable", "reason": "参考代码清单未返回有效涨跌幅。"}
        ratio = cross.get("up_ratio")
        if cross.get("aggregate_issue"):
            label = "参考样本方向未评估"
        elif ratio is None:
            label = "未评估"
        elif ratio >= 55:
            label = "健康扩散"
        elif ratio <= 45:
            label = "参考样本走弱"
        else:
            label = "局部抱团/分化"
        return {
            "status": cross.get("status", "partial"), "label": label,
            "adl_available": False,
            "reason": (cross.get("aggregate_issue") or "")
                + "ADL 需要日度广度历史；当前仅展示来源可追溯的参考代码清单横截面，交易所全集覆盖未知。",
            **cross,
        }

    @staticmethod
    def _valuation_dimension(cross: dict[str, Any]) -> dict[str, Any]:
        if cross.get("aggregate_issue"):
            return {
                **{key: value for key, value in cross.items() if key != "valuation"},
                "status": "unavailable", "label": "未评估",
                "reason": cross["aggregate_issue"], "historical_percentile": None,
                "aggregate_pe_ttm": None, "median_pe_ttm": None, "pe_coverage": 0,
                "aggregate_pb": None, "median_pb": None, "pb_coverage": 0,
            }
        value = cross.get("valuation") or {}
        pe, pb = value.get("aggregate_pe_ttm"), value.get("aggregate_pb")
        if pe is None or pb is None:
            return {**cross, "status": "unavailable", "reason": "参考代码清单的有效 PE/PB 覆盖不足。"}
        if pe <= 15 and pb <= 1.5:
            label = "当前截面偏低"
        elif pe >= 30 or pb >= 4:
            label = "当前截面偏高"
        else:
            label = "当前截面中性"
        return {
            "status": cross.get("status", "partial"), "label": label,
            "historical_percentile": None,
            "reason": "基于当日市值加权 PE/PB 的固定描述阈值；历史分位序列尚未接入，绝不伪造分位。",
            **value, **{key: cross.get(key) for key in (
                "source", "as_of", "latency_seconds", "health_status", "participates_in_decision",
                "coverage_expected", "coverage_returned", "coverage_ratio", "coverage_scope",
                "reference_universe_expected", "reference_universe_returned", "reference_coverage_ratio",
                "exchange_universe_expected", "exchange_coverage_ratio", "exchange_coverage_status",
            )},
        }

    @staticmethod
    def _sentiment_dimension(cross: dict[str, Any]) -> dict[str, Any]:
        value = cross.get("sentiment") or {}
        if value.get("score") is None:
            return {**cross, "status": "unavailable", "reason": cross.get("aggregate_issue") or "上涨家数或涨跌停横截面缺失。"}
        return {
            "status": cross.get("status", "partial"),
            "reason": "仅由可追溯的当日广度与上游逐证券有效涨跌停价计算；缺限价（含无法确认的上市初期状态）即不评估，不按代码前缀猜测。",
            **value, **{key: cross.get(key) for key in (
                "source", "as_of", "latency_seconds", "health_status", "participates_in_decision",
                "coverage_expected", "coverage_returned", "coverage_ratio", "coverage_scope",
                "reference_universe_expected", "reference_universe_returned", "reference_coverage_ratio",
                "exchange_universe_expected", "exchange_coverage_ratio", "exchange_coverage_status",
            )},
        }

    def _alert_catalog(
        self, indices: list[dict[str, Any]], cross: dict[str, Any], flow: dict[str, Any],
        history: list[dict[str, Any]], valuation: dict[str, Any], sentiment: dict[str, Any],
    ) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []

        def add(identifier: str, title: str, value: float | int | None, threshold: str, active: bool | None, source: dict[str, Any], severity: str = "中") -> None:
            if source.get("health_status") in {"upstream_error", "missing", "invalid"}:
                active = None
            state = "active" if active is True else "normal" if active is False else "not_evaluable"
            alerts.append({
                "id": identifier, "type": title, "value": value, "threshold": threshold,
                "state": state, "severity": severity if active else "提示",
                "message": (f"触发：{title}，当前 {value}，阈值 {threshold}" if active else (f"未触发：{title}，当前 {value}，阈值 {threshold}" if active is False else f"未评估：{title}，缺少所需可审计数据")),
                **{key: source.get(key) for key in ("source", "as_of", "latency_seconds", "health_status", "participates_in_decision")},
            })

        daily = min((index.get("change_pct") for index in indices if index.get("change_pct") is not None), default=None)
        add("daily-index-drawdown", "单日指数跌幅", daily, "≤ -2%", daily <= -2 if daily is not None else None, indices[0] if indices else {})
        five_day = _return_pct(history, 5)
        add("five-day-drawdown", "5日累计跌幅", five_day, "≤ -5%", five_day <= -5 if five_day is not None else None, _source("腾讯财经公开前复权指数日线", _asof_from_date(history[-1]["date"]), daily=True, calendar="a_share") if history else {})
        month = _month_metrics(history)
        month_change = month.get("mtd_change_pct") if month else None
        add("monthly-drawdown", "月度跌幅", month_change, "≤ -8%", month_change <= -8 if month_change is not None else None, _source("腾讯财经公开前复权指数日线", _asof_from_date(history[-1]["date"]), daily=True, calendar="a_share") if history else {})
        score = sentiment.get("score")
        add("sentiment-extreme", "情绪极值", score, "≤10 或 ≥90", score <= 10 or score >= 90 if score is not None else None, sentiment)
        percentile = valuation.get("historical_percentile")
        add("valuation-extreme", "估值历史分位极值", percentile, "≤10% 或 ≥90%", percentile <= 10 or percentile >= 90 if percentile is not None else None, valuation)
        fund_value = flow.get("main_net_yi")
        add("main-fund-outflow", "主力资金单日净流出", fund_value, "≤ -50亿", fund_value <= -50 if fund_value is not None else None, flow, "高")
        limit_up = cross.get("limit_up") if cross.get("aggregate_eligible") else None
        add("limit-temperature", "涨停家数温度", limit_up, "<20 或 >200", limit_up < 20 or limit_up > 200 if limit_up is not None else None, cross)
        return alerts

    def _today(self) -> dict[str, Any]:
        indices = _quote_snapshot(self.quote_provider)
        quotes, quote_source = self._market_snapshot()
        cross = _market_cross_section(quotes, quote_source)
        breadth = self._breadth_dimension(cross)
        sentiment = self._sentiment_dimension(cross)
        flow = self._flow_summary()
        history, _ = self._history("sh000001")
        valuation = self._valuation_dimension(cross)
        alerts = self._alert_catalog(indices, cross, flow, history, valuation, sentiment)
        return {
            "status": "ok" if (quotes and flow.get("status") != "unavailable"
                                 and cross.get("aggregate_eligible")
                                 and cross.get("exchange_coverage_status") == "complete"
                                 and sentiment.get("status") != "unavailable") else "partial",
            "dimension": "today",
            "title": "本日 A 股市场",
            "indices": indices,
            "breadth": breadth,
            "fund_flow": flow,
            "sectors": _industry_live_ranking(quotes, quote_source),
            "sentiment": sentiment,
            "risk": {"alerts": alerts, "volatility": None, "reason": "分钟级风险指标待接入完整分时序列；日线预警已独立计算。"},
        }

    def _month(self) -> dict[str, Any]:
        index_specs = (("上证综指", "sh000001"), ("深证成指", "sz399001"), ("创业板指", "sz399006"))
        summaries: list[dict[str, Any]] = []
        base_rows: list[dict[str, Any]] = []
        source: dict[str, Any] = _source("腾讯财经公开前复权指数日线", None)
        for name, code in index_specs:
            rows, current_source = self._history(code)
            if not rows:
                continue
            metrics = _month_metrics(rows)
            if metrics is None:
                continue
            summaries.append({
                "name": name, **metrics,
                **current_source,
            })
            if code == "sh000001":
                base_rows, source = rows, current_source
        if not summaries:
            return {
                "dimension": "month", "title": "本月 A 股市场",
                "status": "unavailable", "indices": [],
                "reason": "指数日线来源不可用，月度结论不输出。", "source": source,
            }
        quotes, quote_source = self._market_snapshot()
        cross = _market_cross_section(quotes, quote_source)
        flow = self._flow_summary()
        sentiment = self._sentiment_dimension(cross)
        return {
            "dimension": "month", "title": "本月 A 股市场",
            "status": "ok" if (flow.get("status") != "unavailable"
                                 and cross.get("aggregate_eligible")
                                 and cross.get("exchange_coverage_status") == "complete") else "partial",
            "indices": summaries,
            "breadth": {"status": "partial", "current_snapshot": self._breadth_dimension(cross), "reason": "已接入当日参考代码清单广度；交易所全集覆盖未知，月度历史缺口不回填。"},
            "fund_flow": flow,
            "sectors": {"status": "unavailable", "reason": "没有月度申万行业序列。"},
            "sentiment": {"status": "partial", "current_snapshot": sentiment, "reason": "月度情绪序列从首次运行起积累；当前不以单日温度代替月度平均。"},
            "risk": {"status": "ok", "monthly_drawdown_pct": next((item["monthly_drawdown_pct"] for item in summaries if item["name"] == "上证综指"), None)},
            "market_state": _market_state({
                "trend": {"status": "ok", **_trend(base_rows), **source} if base_rows else {
                    "status": "unavailable", "label": "样本不足",
                    "reason": "上证综指日线缺失，月度趋势维度不评估。",
                },
                "valuation": self._valuation_dimension(cross), "sentiment": sentiment,
                "funds": flow, "breadth": self._breadth_dimension(cross),
            }, source),
        }

    def _overall(self) -> dict[str, Any]:
        rows, source = self._history("sh000001")
        if not rows:
            return {
                "dimension": "overall", "title": "总体 A 股市场",
                "status": "unavailable", "trend": None,
                "reason": "指数日线来源不可用；总体结论不输出。", "source": source,
                "market_state": _market_state({}, source),
            }
        quotes, quote_source = self._market_snapshot()
        cross = _market_cross_section(quotes, quote_source)
        valuation = self._valuation_dimension(cross)
        breadth = self._breadth_dimension(cross)
        sentiment = self._sentiment_dimension(cross)
        flow = self._flow_summary()
        style_rows = {code: self._history(code)[0] for code in ("sh000300", "sh000905", "sz399006", "sh000922")}
        large, small = style_rows["sh000300"], style_rows["sh000905"]
        growth, value = style_rows["sz399006"], style_rows["sh000922"]

        def relative_return(left: list[dict[str, Any]], right: list[dict[str, Any]], window: int) -> float | None:
            left_return, right_return = _return_pct(left, window), _return_pct(right, window)
            return round(left_return - right_return, 2) if left_return is not None and right_return is not None else None

        style_values = {
            "large_vs_small_1m_pct": relative_return(large, small, 20),
            "large_vs_small_1y_pct": relative_return(large, small, 250),
            "growth_vs_dividend_1m_pct": relative_return(growth, value, 20),
            "growth_vs_dividend_1y_pct": relative_return(growth, value, 250),
        }
        style = {
            "status": "ok" if all(value is not None for value in style_values.values()) else "partial",
            **style_values,
            "method": "沪深300-中证500近1月/1年收益差；创业板指-中证红利收益差。仅描述风格相对强弱。",
            **source,
        }
        long_term_flow = {**flow, "reason": "主力资金日线可提供近期与年内累计方向；北向成交净买额字段若缺失则明确不评估。"}
        trend = {"status": "ok", **_trend(rows), **source}
        state_dimensions = {"trend": trend, "valuation": valuation, "sentiment": sentiment, "funds": flow, "breadth": breadth}
        return {
            "dimension": "overall", "title": "总体 A 股市场",
            "status": "ok" if (flow.get("status") != "unavailable"
                                 and cross.get("aggregate_eligible")
                                 and cross.get("exchange_coverage_status") == "complete") else "partial",
            "trend": trend, "valuation": valuation, "long_term_flow": long_term_flow,
            "style": style, "sentiment": sentiment, "breadth": breadth,
            "risk": {"status": "ok", **_annual_risk(rows), "method": "上证综指近250个交易日简单日收益率年化波动率与滚动峰值回撤。"},
            "source": source,
            "market_state": _market_state(state_dimensions, source),
        }

    def panorama(self, dimension: str) -> dict[str, Any]:
        if dimension == "today":
            payload = self._today()
        elif dimension == "month":
            payload = self._month()
        elif dimension == "overall":
            payload = self._overall()
        else:
            return {
                "status": "invalid_request", "error": "dimension 必须是 today、month 或 overall",
                "server_time": _iso(_now()),
            }
        payload["server_time"] = _iso(_now())
        payload["decision_policy"] = "市场全景为人工复盘与观察工具，不产生买卖建议。"
        return payload

    def market_status(self) -> dict[str, Any]:
        today = self._today()
        overall = self._overall()
        dimensions = {
            "trend": overall.get("trend"),
            "valuation": overall.get("valuation"),
            "sentiment": today.get("sentiment"),
            "funds": today.get("fund_flow"),
            "breadth": today.get("breadth"),
        }
        source = overall.get("source") or _source("市场全景", None)
        return {
            "server_time": _iso(_now()),
            "dimensions": dimensions,
            "market_state": _market_state(dimensions, source),
            "participates_in_decision": False,
        }

    def alerts(self) -> dict[str, Any]:
        today = self._today()
        return {
            "server_time": _iso(_now()), "status": today.get("status"),
            "alerts": today.get("risk", {}).get("alerts", []),
            "participates_in_decision": False,
        }


def _market_state(dimensions: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """A disclosed descriptive state, never a trading signal or performance claim."""
    required = ("trend", "valuation", "sentiment", "funds", "breadth")
    missing = [
        name for name in required
        if not isinstance(dimensions.get(name), dict)
        or dimensions[name].get("status") in {"unavailable", "missing"}
        or dimensions[name].get("health_status") not in {"verified", "delayed"}
        or not dimensions[name].get("source")
        or not dimensions[name].get("as_of")
    ]
    if missing:
        return {
            "status": "insufficient_evidence", "label": "证据不足，不输出综合市场结论",
            "reason": "以下五维缺少可审计输入：" + "、".join(missing) + "。",
            "source": source.get("source"), "as_of": source.get("as_of"),
            "health_status": source.get("health_status"), "participates_in_decision": False,
            "dimension_health": {
                name: {
                    "status": (dimensions.get(name) or {}).get("status", "missing"),
                    "health_status": (dimensions.get(name) or {}).get("health_status", "missing"),
                    "source": (dimensions.get(name) or {}).get("source"),
                    "as_of": (dimensions.get(name) or {}).get("as_of"),
                }
                for name in required
            },
        }
    trend, valuation = dimensions["trend"], dimensions["valuation"]
    sentiment, funds, breadth = dimensions["sentiment"], dimensions["funds"], dimensions["breadth"]
    score = sentiment.get("score")
    if score is not None and (score <= 10 or score >= 90):
        label = "极端状态"
    elif trend.get("label") == "下降趋势" and funds.get("label") == "持续流出" and breadth.get("label") == "参考样本走弱":
        label = "弱势调整"
    elif trend.get("label") == "上升趋势" and valuation.get("label") != "当前截面偏高" and funds.get("label") == "持续流入" and breadth.get("label") == "健康扩散":
        label = "强势上攻"
    else:
        label = "结构性行情"
    return {
        "status": "observation_only", "label": label,
        "reason": "五维固定规则的描述性归类，未经过独立历史验证，不参与买卖评分。",
        "dimensions": {
            "trend": trend.get("label"), "valuation": valuation.get("label"),
            "sentiment": sentiment.get("label"), "funds": funds.get("label"),
            "breadth": breadth.get("label"),
        },
        "dimension_health": {
            name: {
                "status": data.get("status"), "health_status": data.get("health_status"),
                "source": data.get("source"), "as_of": data.get("as_of"),
            }
            for name, data in dimensions.items() if isinstance(data, dict)
        },
        "source": source.get("source"), "as_of": source.get("as_of"),
        "health_status": source.get("health_status"), "participates_in_decision": False,
    }
