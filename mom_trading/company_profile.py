"""公司全景档案：五道闸门 + 证据分级 + 风险否决。

把"技术面选股"升级为"公司研究与风险否决系统"。核心原则：
- 所有指标挂真实数据源与 as_of 时点，禁止编造
- 拿不到的数据源标记"数据源暂不可用"，绝不用假数据冒充
- 任何一道闸门红灯 → 风险否决（veto），禁止生成交易草稿
- 黄灯 → 降低优先级

五道闸门：
  1. 基本面质量  —— THS 财报（利润持续性 / 现金流 / 盈利质量 / 偿债）
  2. 治理风险    —— 中国结算周度质押 + ST 检测（官方不可达时明确降级）
  3. 产业验证    —— 东财主营构成 + 行业分类 + 资金流
  4. 事件证据    —— 巨潮公告（A/B 级）+ 中证网资讯（D 级），证据分级 A/B/C/D/E
  5. 交易可行性  —— 腾讯行情（现价/流动性/涨跌停/停牌）

结论区不产生"总分"，只输出五闸门灯号 + 证据等级 + 风险否决清单，
是否构成买入由系统评分决定。
"""

from __future__ import annotations

import hashlib
import io
import json
import html as html_lib
import math
import re
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from .data_semantics import FLOW_MAGNITUDE_SCALE, normalize_fund_flow_yi
from .trading_calendar import audited_trading_day_age

_SH_TZ = ZoneInfo("Asia/Shanghai")
_CACHE_DIR = Path(__file__).resolve().parent.parent / "research"
_CACHE_FILE = _CACHE_DIR / "company_profiles_cache.json"
_CACHE_LOCK = threading.RLock()
_CACHE_READY: dict[Path, tuple[Path, int, int] | None] = {}
_LEGACY_IMPORT_ATTEMPTED: set[tuple[Path, int, int]] = set()

# get_cached_summary 的进程内摘要缓存：底层已是按股票读取的 SQLite；这里继续复用闸门计算结果，
# 避免扫描(49 只/轮)与机会轮询(20 只/轮)重复解析该股票来源并重算闸门。摘要仅几百字节。
# 键 = (code, quote 指纹)：不同实时行情不得复用旧交易闸门。
# TTL 60s：监控口径下 1 分钟新鲜度与 5 分钟 K 线粒度一致；档案新构建后最多延迟 60s 可见。
_SUMMARY_CACHE: dict = {}
_SUMMARY_CACHE_TTL = 60.0
_BAOSTOCK_LOCK = threading.Lock()
_PLEDGE_REPORT_CACHE: dict[tuple[type, str], tuple[float, str]] = {}
_PLEDGE_REPORT_CACHE_LOCK = threading.Lock()
_PLEDGE_REPORT_CACHE_TTL = 30 * 60
_FINANCIAL_PARSER_VERSION = "ths_amount_units_v2"

# 数据缓存 TTL（秒）：按源的业务变化速度设定，而不是沿用盘中行情 TTL。
# 财务、主营和质押都是日/周/报告期数据；若仅6小时后就失效，同一交易日
# 晚上会把已成功核验的证据错报为“缺失”。公告/研报/筹码保持日级刷新；
# 财务/主营/质押一周刷新。过期后仍 fail-closed，不会把无法刷新的旧证据当作当前事实。
_TTL = {
    "financials": 7 * 24 * 3600,
    "balance_sheet": 7 * 24 * 3600,
    "research_reports": 24 * 3600,
    "chip_distribution": 24 * 3600,
    "business": 7 * 24 * 3600,
    "pledge": 7 * 24 * 3600,
    "announcements": 24 * 3600,
    "audit_opinion": 30 * 24 * 3600,
    "events": 900,
}

# 涨停幅度：创业板/科创板 20%，其余 10%
_BOARD_LIMIT_PREFIXES = ("300", "301", "688", "689")


def _now() -> str:
    return datetime.now(_SH_TZ).isoformat(timespec="seconds")


def _log(msg: str) -> None:
    print(f"[company_profile] {msg}")


def _cache_db_file() -> Path:
    """Return the row-store next to the legacy JSON path (also test-friendly)."""
    return _CACHE_FILE.with_suffix(".sqlite3")


def _cache_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(_cache_db_file(), timeout=30)
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _legacy_fingerprint() -> tuple[Path, int, int] | None:
    try:
        stat = _CACHE_FILE.stat()
    except OSError:
        return None
    return (_CACHE_FILE, stat.st_mtime_ns, stat.st_size)


def _ensure_cache_storage() -> None:
    """Create the row store and import the old whole-file JSON at most once.

    The legacy file is deliberately left untouched as a last-good recovery copy.
    A failed import rolls back and is retried only if that file changes.
    """
    db_file = _cache_db_file()
    with _CACHE_LOCK:
        fingerprint = _legacy_fingerprint()
        if (
            db_file in _CACHE_READY
            and _CACHE_READY[db_file] == fingerprint
            and db_file.exists()
        ):
            return
        db_file.parent.mkdir(parents=True, exist_ok=True)
        with closing(_cache_connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS source_cache (
                    code TEXT NOT NULL,
                    source TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (code, source)
                ) WITHOUT ROWID
                """
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS cache_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            migrated = connection.execute(
                "SELECT value FROM cache_meta WHERE key = 'legacy_json_migrated'"
            ).fetchone()
            if not migrated and fingerprint and fingerprint not in _LEGACY_IMPORT_ATTEMPTED:
                _LEGACY_IMPORT_ATTEMPTED.add(fingerprint)
                try:
                    legacy = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
                    if not isinstance(legacy, dict):
                        raise ValueError("legacy cache root is not an object")
                    rows = [
                        (str(code), str(source), json.dumps(payload, ensure_ascii=False))
                        for code, entry in legacy.items()
                        if isinstance(entry, dict)
                        for source, payload in entry.items()
                    ]
                    connection.executemany(
                        "INSERT OR IGNORE INTO source_cache(code, source, payload_json) VALUES (?, ?, ?)",
                        rows,
                    )
                    connection.execute(
                        "INSERT OR REPLACE INTO cache_meta(key, value) VALUES ('legacy_json_migrated', ?)",
                        (f"{fingerprint[1]}:{fingerprint[2]}",),
                    )
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    connection.rollback()
                    _log(f"旧 JSON 缓存迁移失败，保留原文件: {exc}")
        _CACHE_READY[db_file] = fingerprint


def _legacy_cache_entry(code: str) -> dict:
    """Failure-only fallback; normal reads never parse the whole legacy file."""
    try:
        cache = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        entry = cache.get(code, {}) if isinstance(cache, dict) else {}
        return entry if isinstance(entry, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _load_cache_entry(code: str) -> dict:
    try:
        _ensure_cache_storage()
        with _CACHE_LOCK, closing(_cache_connect()) as connection, connection:
            rows = connection.execute(
                "SELECT source, payload_json FROM source_cache WHERE code = ?", (code,)
            ).fetchall()
        return {source: json.loads(payload) for source, payload in rows}
    except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        _log(f"缓存读取失败，回退旧 JSON: {exc}")
        return _legacy_cache_entry(code)


def _load_cache_item(code: str, source: str) -> dict | None:
    try:
        _ensure_cache_storage()
        with _CACHE_LOCK, closing(_cache_connect()) as connection, connection:
            row = connection.execute(
                "SELECT payload_json FROM source_cache WHERE code = ? AND source = ?",
                (code, source),
            ).fetchone()
        payload = json.loads(row[0]) if row else None
        return payload if isinstance(payload, dict) else None
    except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        _log(f"{code}/{source} 缓存读取失败，回退旧 JSON: {exc}")
        payload = _legacy_cache_entry(code).get(source)
        return payload if isinstance(payload, dict) else None


def _save_cache_item(code: str, source: str, payload: dict) -> bool:
    """Atomically replace one source row; a failed write leaves last-good intact."""
    try:
        _ensure_cache_storage()
        encoded = json.dumps(payload, ensure_ascii=False)
        with _CACHE_LOCK, closing(_cache_connect()) as connection, connection:
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                """
                INSERT INTO source_cache(code, source, payload_json) VALUES (?, ?, ?)
                ON CONFLICT(code, source) DO UPDATE SET payload_json = excluded.payload_json
                """,
                (code, source, encoded),
            )
            if source == "pledge":
                data_as_of = _pledge_data_as_of(payload.get("data"))
                if data_as_of:
                    connection.execute(
                        """
                        INSERT INTO cache_meta(key, value)
                        VALUES ('pledge_latest_as_of', ?)
                        ON CONFLICT(key) DO UPDATE SET value = CASE
                            WHEN excluded.value > cache_meta.value THEN excluded.value
                            ELSE cache_meta.value
                        END
                        """,
                        (data_as_of,),
                    )
        return True
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        _log(f"{code}/{source} 缓存写入失败，保留上一版本: {exc}")
        return False


def get_cached_source_data(code: str, source: str):
    """Read one cached source without network access (used by background consumers)."""
    item = _load_cache_item(code, source)
    if (
        source == "financials"
        and isinstance(item, dict)
        and item.get("parser_version") != _FINANCIAL_PARSER_VERSION
    ):
        return None
    return item.get("data") if item else None


def _load_cache() -> dict:
    """Compatibility snapshot for diagnostics; hot paths use row-level readers."""
    try:
        _ensure_cache_storage()
        with _CACHE_LOCK, closing(_cache_connect()) as connection, connection:
            rows = connection.execute(
                "SELECT code, source, payload_json FROM source_cache"
            ).fetchall()
        cache: dict = {}
        for code, source, payload in rows:
            cache.setdefault(code, {})[source] = json.loads(payload)
        return cache
    except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        _log(f"缓存快照读取失败，回退旧 JSON: {exc}")
        try:
            value = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}


def _save_cache(cache: dict) -> None:
    """Compatibility bulk replace, transactionally; normal writes are per source."""
    try:
        _ensure_cache_storage()
        rows = [
            (str(code), str(source), json.dumps(payload, ensure_ascii=False))
            for code, entry in cache.items()
            if isinstance(entry, dict)
            for source, payload in entry.items()
        ]
        with _CACHE_LOCK, closing(_cache_connect()) as connection, connection:
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("DELETE FROM source_cache")
            connection.executemany(
                "INSERT INTO source_cache(code, source, payload_json) VALUES (?, ?, ?)", rows,
            )
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        _log(f"缓存批量写入失败，保留上一版本: {exc}")


def _cache_item_status(item: object, ttl: float, now: datetime | None = None) -> dict:
    """Describe one cached source without refreshing it; stale never means ready."""
    if not isinstance(item, dict) or "data" not in item or item.get("data") is None:
        return {"status": "unavailable", "fetched_at": None, "age_seconds": None}
    fetched_at = item.get("fetched_at")
    try:
        fetched = datetime.fromisoformat(str(fetched_at))
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=_SH_TZ)
        age = ((now or datetime.now(_SH_TZ)) - fetched.astimezone(_SH_TZ)).total_seconds()
    except (TypeError, ValueError):
        return {"status": "stale", "fetched_at": fetched_at, "age_seconds": None}
    status = "available" if -300 <= age < ttl else "stale"
    return {
        "status": status,
        "fetched_at": fetched.isoformat(timespec="seconds"),
        "age_seconds": round(age),
        "ttl_seconds": ttl,
    }


def _financial_cache_item_status(
    item: object, ttl: float, now: datetime | None = None,
) -> dict:
    """Reject cached THS rows produced before amount-unit normalization."""
    result = _cache_item_status(item, ttl, now)
    if (
        result["status"] == "available"
        and isinstance(item, dict)
        and item.get("parser_version") != _FINANCIAL_PARSER_VERSION
    ):
        result["status"] = "stale"
        result["stale_reason"] = "financial_amount_unit_parser_outdated"
    return result


def _normalized_score_ready_financial_payload(data: object) -> list[dict] | None:
    """Return period-sorted rows only when the row consumed by scoring is ready."""
    if not isinstance(data, list):
        return None

    def finite(row: dict, key: str) -> bool:
        value = row.get(key)
        return bool(
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
        )

    periods = []
    for row in data:
        period = str(row.get("period") or "")[:10] if isinstance(row, dict) else ""
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", period):
            return None
        periods.append(period)
    if not periods or len(periods) != len(set(periods)):
        return None
    rows = [row for _, row in sorted(zip(periods, data), key=lambda item: item[0])]
    latest = rows[-1]
    growth_ready = any(
        finite(latest, key) for key in ("revenue_yoy", "np_yoy", "deducted_yoy")
    )
    eps = latest.get("eps")
    quality_ready = any(
        finite(latest, key) for key in ("roe", "gross_margin", "net_margin")
    ) or (
        finite(latest, "ocf_ps") and finite(latest, "eps")
        and float(eps) != 0.0
    )
    return rows if growth_ready and quality_ready else None


def _financial_payload_score_ready(data: object) -> bool:
    normalized = _normalized_score_ready_financial_payload(data)
    return normalized is not None and normalized == data


def cached_financial_status(
    code: str, *, now: datetime | None = None,
) -> dict:
    """Return the exact cached-financial identity without a network request.

    The coverage worker uses this narrow reader to distinguish a missing row
    from a legacy parser row.  It deliberately does not build a company
    profile or treat stale/empty payloads as professional-score evidence.
    """
    item = _load_cache_item(str(code), "financials")
    parser_version = item.get("parser_version") if isinstance(item, dict) else None
    data = item.get("data") if isinstance(item, dict) else None
    status = _financial_cache_item_status(item, _TTL["financials"], now)
    if not isinstance(item, dict) or data is None:
        cache_state = "absent"
    elif parser_version != _FINANCIAL_PARSER_VERSION:
        cache_state = "legacy_parser"
    elif status.get("status") != "available":
        cache_state = "stale_ttl"
    elif not _financial_payload_score_ready(data):
        cache_state = "incomplete_v2"
    else:
        cache_state = "fresh_v2"
    return {
        "code": str(code),
        "cache_state": cache_state,
        "status": status.get("status"),
        "parser_version": parser_version,
        "expected_parser_version": _FINANCIAL_PARSER_VERSION,
        "fetched_at": status.get("fetched_at"),
        "row_count": len(data) if isinstance(data, list) else 0,
    }


def refresh_financial_cache(code: str) -> dict:
    """Refresh only the THS financial source and preserve last-good on failure."""
    code = str(code)
    before = cached_financial_status(code)
    try:
        data = fetch_financials(code)
    except Exception as exc:
        return {
            "code": code,
            "status": "failure",
            "reason": f"{type(exc).__name__}: {str(exc)[:300]}",
            "before": before,
            "after": cached_financial_status(code),
        }
    normalized = _normalized_score_ready_financial_payload(data)
    if normalized is None:
        return {
            "code": code,
            "status": "failure",
            "reason": "financial_source_missing_growth_or_quality_rows",
            "before": before,
            "after": cached_financial_status(code),
        }
    payload = {
        "data": normalized,
        "fetched_at": _now(),
        "parser_version": _FINANCIAL_PARSER_VERSION,
    }
    if not _save_cache_item(code, "financials", payload):
        return {
            "code": code,
            "status": "failure",
            "reason": "financial_cache_write_failed",
            "before": before,
            "after": cached_financial_status(code),
        }
    with _CACHE_LOCK:
        _invalidate_summary_cache(code)
    after = cached_financial_status(code)
    return {
        "code": code,
        "status": "success" if after["cache_state"] == "fresh_v2" else "failure",
        "reason": None if after["cache_state"] == "fresh_v2" else "fresh_v2_not_observed",
        "before": before,
        "after": after,
    }


def _pledge_data_as_of(data: object) -> str | None:
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None
    value = str(data[0].get("as_of") or "")[:10]
    return value if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else None


def _latest_cached_pledge_as_of() -> str | None:
    """Return the newest verified weekly identity already present in the row store."""
    try:
        _ensure_cache_storage()
        with _CACHE_LOCK, closing(_cache_connect()) as connection, connection:
            row = connection.execute(
                "SELECT value FROM cache_meta WHERE key = 'pledge_latest_as_of'"
            ).fetchone()
            if row and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(row[0])):
                return str(row[0])
            rows = connection.execute(
                "SELECT payload_json FROM source_cache WHERE source = 'pledge'"
            ).fetchall()
            dates = []
            for (raw,) in rows:
                try:
                    payload = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                value = _pledge_data_as_of(
                    payload.get("data") if isinstance(payload, dict) else None
                )
                if value:
                    dates.append(value)
            latest = max(dates) if dates else None
            if latest:
                connection.execute(
                    "INSERT OR REPLACE INTO cache_meta(key, value) VALUES ('pledge_latest_as_of', ?)",
                    (latest,),
                )
            return latest
    except (OSError, sqlite3.Error):
        return None


def _pledge_cache_item_status(
    item: object, ttl: float, now: datetime | None = None,
) -> dict:
    """Apply both fetch-time TTL and weekly business-date freshness to pledge rows."""
    current = now or datetime.now(_SH_TZ)
    result = _cache_item_status(item, ttl, current)
    data = item.get("data") if isinstance(item, dict) else None
    data_as_of = _pledge_data_as_of(data)
    latest_as_of = _latest_cached_pledge_as_of()
    result.update({"data_as_of": data_as_of, "latest_data_as_of": latest_as_of})
    try:
        observed = datetime.fromisoformat(str(data_as_of)).date()
        business_age_days = (current.date() - observed).days
    except (TypeError, ValueError):
        business_age_days = None
    result["business_age_days"] = business_age_days
    if result["status"] == "available" and (
        business_age_days is None
        or business_age_days < -1
        or business_age_days > 14
        or (latest_as_of is not None and data_as_of != latest_as_of)
    ):
        result["status"] = "stale"
        result["stale_reason"] = "pledge_business_date_not_latest"
    return result


def _quote_summary_key(quote: dict | None) -> tuple | None:
    """Return a bounded identity for the quote facts used by the tradeable gate."""
    if not isinstance(quote, dict):
        return None
    return tuple(
        str(quote.get(key)) if quote.get(key) is not None else None
        for key in (
            "price", "prev_close", "previous_close", "change_pct", "board_limit_pct",
            "amount", "amount_yi", "as_of", "exchange_as_of", "exchange_data_time",
            "data_time", "fetched_at",
            "freshness", "name",
        )
    )


def _summary_sources_still_fresh(summary: object, now: datetime) -> bool:
    """Do not let the 60s process cache extend an underlying source TTL."""
    if not isinstance(summary, dict):
        return False
    sources = summary.get("data_sources")
    if not isinstance(sources, dict):
        return False
    for source_key, source in sources.items():
        if not isinstance(source, dict) or source.get("status") != "available":
            continue
        fetched_at = source.get("fetched_at")
        ttl = source.get("ttl_seconds")
        try:
            fetched = datetime.fromisoformat(str(fetched_at))
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=_SH_TZ)
            ttl_seconds = float(ttl)
        except (TypeError, ValueError):
            return False
        if (now - fetched.astimezone(_SH_TZ)).total_seconds() >= ttl_seconds:
            return False
        if source_key == "pledge" and (
            source.get("data_as_of") != _latest_cached_pledge_as_of()
        ):
            return False
    return True


def _invalidate_summary_cache(code: str) -> None:
    """Drop both no-quote and all quote-specific summaries for one stock."""
    for key in [key for key in _SUMMARY_CACHE if key[0] == code]:
        _SUMMARY_CACHE.pop(key, None)


def _cached_get(
    code: str, key: str, fetcher, ttl: float, *, force_refresh: bool = False,
) -> tuple[object, str]:
    """带 TTL 的缓存读取。返回 (数据, 状态)；status ∈ available/stale/unavailable."""
    now = datetime.now(_SH_TZ)
    entry = _load_cache_item(code, key)
    status_reader = (
        _pledge_cache_item_status if key == "pledge"
        else _financial_cache_item_status if key == "financials"
        else _cache_item_status
    )
    if (
        not force_refresh
        and status_reader(entry, ttl, now)["status"] == "available"
    ):
        return entry["data"], "available"
    # 缓存过期或无缓存 → 拉新
    try:
        data = fetcher()
    except Exception as e:
        # 拉取失败：有过期缓存则降级为 stale，否则 unavailable
        if entry and entry.get("data") is not None:
            return entry["data"], "stale"
        _log(f"{code}/{key} 拉取失败: {str(e)[:120]}")
        return None, "unavailable"
    if data is None:
        if entry and entry.get("data") is not None:
            return entry["data"], "stale"
        return None, "unavailable"
    payload = {"data": data, "fetched_at": _now()}
    if key == "financials":
        payload["parser_version"] = _FINANCIAL_PARSER_VERSION
    if _save_cache_item(code, key, payload):
        with _CACHE_LOCK:
            _invalidate_summary_cache(code)
    return data, "available"


def _to_float(v) -> float | None:
    """安全转 float。处理 A 股接口常见的中文单位与百分号：
    '59.57亿'→59.57，'29.14%'→29.14，'-12.03'→-12.03，'False'/空→None。
    只取第一个数字，不编造."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if f != f else f
    s = str(v).strip()
    if not s:
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    if not m:
        return None
    return float(m.group())


def _to_yi(v) -> float | None:
    """Normalize a disclosed monetary amount to 亿元 without guessing units."""
    value = _to_float(v)
    if value is None or isinstance(v, (int, float)):
        return value
    text = str(v).strip()
    if "万亿" in text:
        return value * 10_000
    if "亿" in text:
        return value
    if "百万" in text:
        return value / 100
    if "万" in text:
        return value / 10_000
    if "千" in text:
        return value / 100_000
    if "元" in text:
        return value / 100_000_000
    return value


def _format_yi_amount(value: float) -> str:
    """Keep small disclosed amounts readable while preserving the 亿元 contract."""
    if abs(value) < 0.1:
        return f"{value * 10_000:.1f}万"
    return f"{value:.1f}亿"


# ─────────────────────────────────────────────
# 数据源：THS 财务
# ─────────────────────────────────────────────
def fetch_financials(code: str) -> list[dict]:
    """同花顺财务摘要（季度）。返回按报告期升序的最近记录列表."""
    import akshare as ak

    df = ak.stock_financial_abstract_ths(symbol=code)
    if df is None or len(df) == 0:
        return []
    cols = {
        "period": "报告期", "net_profit": "净利润", "np_yoy": "净利润同比增长率",
        "deducted": "扣非净利润", "deducted_yoy": "扣非净利润同比增长率",
        "revenue": "营业总收入", "revenue_yoy": "营业总收入同比增长率",
        "eps": "基本每股收益", "bps": "每股净资产", "ocf_ps": "每股经营现金流",
        "gross_margin": "销售毛利率", "net_margin": "销售净利率",
        "roe": "净资产收益率", "debt_ratio": "资产负债率",
        "current_ratio": "流动比率", "quick_ratio": "速动比率",
        "inv_turnover_days": "存货周转天数", "ar_turnover_days": "应收账款周转天数",
    }
    rows = []
    for _, r in df.iterrows():
        d = {"period": str(r.get("报告期", ""))}
        ok = True
        for out, src in cols.items():
            if out == "period":
                continue
            parser = _to_yi if out in {"net_profit", "deducted", "revenue"} else _to_float
            d[out] = parser(r.get(src))
        rows.append(d)
    rows.sort(key=lambda x: x["period"])  # 旧→新
    return rows


def _statement_report_title(period: str) -> str | None:
    year = str(period or "")[:4]
    suffix = str(period or "")[5:10]
    if not re.fullmatch(r"\d{4}", year):
        return None
    return {
        "03-31": f"{year}年第一季度报告",
        "06-30": f"{year}年半年度报告",
        "09-30": f"{year}年第三季度报告",
        "12-31": f"{year}年年度报告",
    }.get(suffix)


def _number_tokens(text: str) -> list[float]:
    values = []
    for token in re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?", str(text or "")):
        try:
            values.append(float(token.replace(",", "")))
        except ValueError:
            continue
    return values


def _parse_official_goodwill_page(
    text: str, *, expected_total_assets: float,
) -> dict | None:
    """Confirm a current-period goodwill amount from one official statement page.

    The asset total anchors both the period column and the statement unit.  A
    blank goodwill line is accepted as nil only when it sits in the same local
    balance-sheet window as that anchored total; narrative mentions never pass.
    """
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(text or "").splitlines()]
    expected = _to_float(expected_total_assets)
    if expected is None or expected <= 0:
        return None
    for index, line in enumerate(lines):
        if not re.fullmatch(r"商誉(?:\s+.*)?", line) or "减值" in line:
            continue
        window = lines[max(0, index - 12):min(len(lines), index + 20)]
        asset_lines = [item for item in window if item.startswith("资产总计")]
        multiplier = None
        for asset_line in asset_lines:
            for observed in _number_tokens(asset_line[len("资产总计"):]):
                for candidate_multiplier in (1.0, 10_000.0, 100_000_000.0):
                    scaled = observed * candidate_multiplier
                    if abs(scaled - expected) <= max(1.0, expected * 0.00001):
                        multiplier = candidate_multiplier
                        break
                if multiplier is not None:
                    break
            if multiplier is not None:
                break
        if multiplier is None:
            continue
        amounts = _number_tokens(line[len("商誉"):])
        amount = amounts[0] * multiplier if amounts else 0.0
        return {
            "goodwill": round(amount, 2),
            "line_status": "explicit_amount" if amounts else "official_statement_blank_nil",
            "asset_total_anchor_yuan": expected,
            "unit_multiplier": int(multiplier),
        }
    return None


def fetch_official_goodwill_evidence(
    code: str, *, period: str, expected_total_assets: float,
) -> dict:
    """Read one exact-period CNINFO report and verify its balance-sheet goodwill row."""
    report_title = _statement_report_title(period)
    org_id = _cninfo_org_ids().get(code)
    if not report_title or not org_id:
        raise RuntimeError("cninfo_goodwill_report_identity_missing")
    period_date = datetime.fromisoformat(period).date()
    end_date = datetime.now(_SH_TZ).date()
    session = requests.Session()
    session.trust_env = False
    headers = {
        "User-Agent": "A-share-manual-decision-support/1.0",
        "Referer": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
    }
    response = session.post(
        "https://www.cninfo.com.cn/new/hisAnnouncement/query",
        data={
            "pageNum": "1", "pageSize": "30", "column": "szse",
            "tabName": "fulltext", "plate": "", "stock": f"{code},{org_id}",
            "searchkey": report_title, "secid": "", "category": "", "trade": "",
            "seDate": f"{period_date.isoformat()}~{end_date.isoformat()}",
            "sortName": "", "sortType": "", "isHLtitle": "true",
        },
        headers=headers, timeout=(3.05, 10),
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("announcements") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("cninfo_goodwill_announcements_schema")
    exact = []
    for raw in rows:
        if not isinstance(raw, dict) or str(raw.get("secCode") or "") != code:
            continue
        title = re.sub(r"<[^>]+>", "", str(raw.get("announcementTitle") or ""))
        title = re.sub(r"\s+", "", title)
        if report_title not in title or "摘要" in title:
            continue
        adjunct = str(raw.get("adjunctUrl") or "").strip().lstrip("/")
        if adjunct.lower().endswith(".pdf"):
            exact.append((float(raw.get("announcementTime") or 0), raw, adjunct))
    if not exact:
        raise RuntimeError("cninfo_exact_period_report_not_found")
    _, raw, adjunct = max(exact, key=lambda item: item[0])
    pdf_url = f"https://static.cninfo.com.cn/{adjunct}"
    pdf_response = session.get(pdf_url, headers=headers, timeout=(3.05, 25))
    pdf_response.raise_for_status()
    pdf_bytes = pdf_response.content
    if len(pdf_bytes) > 30 * 1024 * 1024 or not pdf_bytes.startswith(b"%PDF"):
        raise RuntimeError("cninfo_goodwill_report_pdf_invalid")
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        if "商誉" not in text or "资产总计" not in text:
            continue
        parsed = _parse_official_goodwill_page(
            text, expected_total_assets=expected_total_assets,
        )
        if parsed:
            published = datetime.fromtimestamp(
                float(raw.get("announcementTime")) / 1000, tz=_SH_TZ,
            ).isoformat(timespec="seconds")
            return {
                **parsed,
                "period": period,
                "page": page_number,
                "published_at": published,
                "announcement_id": str(raw.get("announcementId") or ""),
                "source": "巨潮资讯定期报告原文资产负债表",
                "source_url": pdf_url,
                "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
                "pdf_size_bytes": len(pdf_bytes),
            }
    raise RuntimeError("cninfo_goodwill_balance_line_not_verified")


def fetch_balance_sheet(
    code: str, *, verify_official_blank: bool = False,
) -> list[dict]:
    """东方财富资产负债表；显式或空白商誉均可由官方同表交叉核验。"""
    import akshare as ak

    df = ak.stock_balance_sheet_by_report_em(symbol=f"{_market_prefix(code)}{code}")
    if df is None or len(df) == 0:
        return []
    valid_periods = [
        str(value or "")[:10] for value in df.get("REPORT_DATE", [])
        if str(value or "")[:10]
    ]
    latest_period = max(valid_periods) if valid_periods else ""
    rows = []
    for _, row in df.iterrows():
        period = str(row.get("REPORT_DATE") or "")[:10]
        if not period:
            continue
        goodwill = _to_float(row.get("GOODWILL"))
        total_assets = _to_float(row.get("TOTAL_ASSETS"))
        goodwill_line_blank = bool(
            "GOODWILL" in df.columns
            and goodwill is None
            and total_assets is not None
            and total_assets > 0
            and any(
                _to_float(row.get(field)) is not None
                for field in ("FIXED_ASSET", "INTANGIBLE_ASSET")
            )
        )
        official_evidence = None
        if (
            verify_official_blank
            and period == latest_period
            and total_assets is not None
            and total_assets > 0
        ):
            try:
                official_evidence = fetch_official_goodwill_evidence(
                    code, period=period, expected_total_assets=total_assets,
                )
            except Exception as exc:
                _log(f"{code}/goodwill_official 核验失败: {str(exc)[:120]}")
            if official_evidence is not None:
                official_goodwill = _to_float(official_evidence.get("goodwill"))
                if official_goodwill is None:
                    official_evidence = None
                elif goodwill is not None and abs(official_goodwill - goodwill) > max(
                    1.0, total_assets * 1e-9,
                ):
                    _log(
                        f"{code}/goodwill_official 与标准化金额不一致："
                        f"official={official_goodwill}, standardized={goodwill}"
                    )
                    official_evidence = None
                else:
                    goodwill = official_goodwill
        ratio = (
            round(goodwill / total_assets * 100, 4)
            if goodwill is not None and total_assets is not None and total_assets > 0
            else None
        )
        result = {
            "period": period,
            "data_as_of": str(row.get("NOTICE_DATE") or "")[:10] or None,
            "goodwill": goodwill,
            "total_assets": total_assets,
            "goodwill_ratio_pct": ratio,
            "balance_sheet_source": (
                "巨潮资讯定期报告原文资产负债表（同报告期商誉行已核验）"
                if official_evidence is not None else
                "东方财富财务分析资产负债表（标准化商誉字段为空；未核验为0）"
                if goodwill_line_blank else
                "东方财富财务分析资产负债表（上市公司披露口径）"
            ),
        }
        if official_evidence is not None:
            result["goodwill_reporting_status"] = official_evidence["line_status"]
            result["goodwill_official_evidence"] = official_evidence
        elif goodwill_line_blank:
            # 相邻科目有值只能证明标准化报表并非整行缺失，不能证明第三方
            # GOODWILL 空值等于上市公司正式披露的零余额。硬否决必须继续
            # fail closed，直至官方报告/XBRL 对同报告期给出可复核证据。
            result["goodwill_reporting_status"] = "inferred_blank_zero_unverified"
            result["goodwill_inferred_zero"] = 0.0
            result["goodwill_zero_basis"] = (
                "同报告期总资产及其他长期资产科目有效，但第三方标准化商誉字段为空"
            )
        rows.append(result)
    rows.sort(key=lambda item: item["period"])
    return rows


def _merge_financial_balance(financials: list[dict] | None,
                             balance_sheet: list[dict] | None) -> list[dict]:
    """仅在报告期完全相同时合并，禁止跨期拼接财务字段。"""
    financials = [dict(item) for item in (financials or [])]
    balance_by_period = {
        str(item.get("period") or ""): item
        for item in (balance_sheet or [])
        if item.get("period")
    }
    if not financials:
        return [dict(item) for item in (balance_sheet or [])]
    for item in financials:
        balance = balance_by_period.get(str(item.get("period") or ""))
        if balance:
            item.update({key: value for key, value in balance.items() if key != "period"})
    return financials


def fetch_research_reports(code: str) -> list[dict]:
    """东财公开个股研报元数据；不抓取或转存受版权保护的 PDF 正文。"""
    import akshare as ak

    try:
        df = ak.stock_research_report_em(symbol=code)
    except (KeyError, TypeError, ValueError):
        # AkShare 在 API 正常返回零条记录时仍会读取 infoCode，继而抛 KeyError。
        # 直接读取同一公开元数据接口，区分“已核验为空”和“来源不可用”。
        return _fetch_research_reports_eastmoney(code)
    if df is None or len(df) == 0:
        return []
    rows = []
    forecast_columns = [column for column in df.columns if "盈利预测" in str(column)]
    for _, row in df.head(100).iterrows():
        title = str(row.get("报告名称") or "").strip()
        if not title:
            continue
        forecasts = {
            str(column): value
            for column in forecast_columns
            if (value := _to_float(row.get(column))) is not None
        }
        pdf_url = str(row.get("报告PDF链接") or "").strip()
        rows.append({
            "title": title,
            "institution": str(row.get("机构") or "").strip(),
            "rating": str(row.get("东财评级") or "").strip(),
            "date": str(row.get("日期") or "")[:10],
            "industry": str(row.get("行业") or "").strip(),
            "pdf_url": pdf_url if pdf_url.startswith(("http://", "https://")) else "",
            "forecasts": forecasts,
        })
    rows.sort(key=lambda item: item["date"], reverse=True)
    return rows


def _fetch_research_reports_eastmoney(code: str) -> list[dict]:
    session = requests.Session()
    session.trust_env = False
    response = session.get(
        "https://reportapi.eastmoney.com/report/list",
        params={
            "industryCode": "*", "pageSize": "100", "industry": "*",
            "rating": "*", "ratingChange": "*", "beginTime": "2000-01-01",
            "endTime": f"{datetime.now(_SH_TZ).year + 1}-01-01", "pageNo": "1",
            "fields": "", "qType": "0", "orgCode": "", "code": code,
            "rcode": "", "p": "1", "pageNum": "1", "pageNumber": "1",
        },
        headers={"User-Agent": "A-share-manual-decision-support/1.0"},
        timeout=8,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("data") or [], list):
        raise RuntimeError("eastmoney_research_reports_schema")
    current_year = int(payload.get("currentYear") or datetime.now(_SH_TZ).year)
    rows = []
    for raw in (payload.get("data") or [])[:100]:
        if not isinstance(raw, dict) or str(raw.get("stockCode") or "") != code:
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        info_code = str(raw.get("infoCode") or "").strip()
        forecasts = {}
        for offset, raw_key in enumerate(
            ("predictThisYearEps", "predictNextYearEps", "predictNextTwoYearEps")
        ):
            year = current_year + offset
            for suffix, key in (("收益", raw_key), ("市盈率", raw_key.replace("Eps", "Pe"))):
                value = _to_float(raw.get(key))
                if value is not None:
                    forecasts[f"{year}-盈利预测-{suffix}"] = value
        rows.append({
            "title": title,
            "institution": str(raw.get("orgSName") or "").strip(),
            "rating": str(raw.get("emRatingName") or "").strip(),
            "date": str(raw.get("publishDate") or "")[:10],
            "industry": str(raw.get("indvInduName") or "").strip(),
            "pdf_url": f"https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf" if info_code else "",
            "forecasts": forecasts,
        })
    rows.sort(key=lambda item: item["date"], reverse=True)
    return rows


def _estimate_chip_distribution(bars: list[dict]) -> dict | None:
    """以真实 OHLC 与换手率做 120 日换手衰减估算；不是股东真实持仓。"""
    bars = list(bars or [])[-120:]
    if not bars:
        return None
    low = min(float(item["low"]) for item in bars)
    high = max(float(item["high"]) for item in bars)
    if low <= 0 or high < low:
        return None
    bins = 150
    accuracy = max(0.01, (high - low) / (bins - 1))
    chips = [0.0] * bins
    for item in bars:
        open_, close = float(item["open"]), float(item["close"])
        bar_high, bar_low = float(item["high"]), float(item["low"])
        turnover = min(1.0, max(0.0, float(item.get("turnover_pct") or 0) / 100))
        chips = [value * (1 - turnover) for value in chips]
        average = (open_ + close + bar_high + bar_low) / 4
        high_index = min(bins - 1, max(0, math.floor((bar_high - low) / accuracy)))
        low_index = min(bins - 1, max(0, math.ceil((bar_low - low) / accuracy)))
        if abs(bar_high - bar_low) < 1e-12:
            point = min(bins - 1, max(0, math.floor((average - low) / accuracy)))
            chips[point] += turnover / 2
            continue
        scale = 2 / (bar_high - bar_low)
        for index in range(low_index, high_index + 1):
            price = low + accuracy * index
            if price <= average:
                weight = 1.0 if abs(average - bar_low) < 1e-12 else (price - bar_low) / (average - bar_low)
            else:
                weight = 1.0 if abs(bar_high - average) < 1e-12 else (bar_high - price) / (bar_high - average)
            chips[index] += max(0.0, weight) * scale * turnover
    total = sum(chips)
    if total <= 0:
        return None

    def quantile(fraction: float) -> float:
        target, cumulative = total * fraction, 0.0
        for index, value in enumerate(chips):
            cumulative += value
            if cumulative >= target:
                return round(low + index * accuracy, 2)
        return round(low + (bins - 1) * accuracy, 2)

    def band(fraction: float) -> tuple[float, float, float]:
        lower = quantile((1 - fraction) / 2)
        upper = quantile((1 + fraction) / 2)
        concentration = 0.0 if lower + upper == 0 else (upper - lower) / (lower + upper)
        return lower, upper, round(concentration, 6)

    close = float(bars[-1]["close"])
    profit = sum(
        value for index, value in enumerate(chips)
        if low + index * accuracy <= close
    ) / total
    low90, high90, concentration90 = band(0.9)
    low70, high70, concentration70 = band(0.7)
    return {
        "as_of": str(bars[-1].get("date") or ""),
        "sample_days": len(bars),
        "adjust_type": "raw",
        "profit_ratio": round(profit, 6),
        "average_cost": quantile(0.5),
        "cost_90_low": low90,
        "cost_90_high": high90,
        "concentration_90": concentration90,
        "cost_70_low": low70,
        "cost_70_high": high70,
        "concentration_70": concentration70,
        "method": "120交易日真实OHLC与换手率的换手衰减三角分布估算",
        "source": "BaoStock不复权日K与换手率；本站本地估算",
        "is_estimate": True,
        "is_actual_holder_distribution": False,
    }


def fetch_chip_distribution(code: str) -> dict | None:
    """从无需账号的 BaoStock 获取真实日K/换手率，再做透明的本地筹码估算。"""
    import baostock as bs

    end = datetime.now(_SH_TZ).date()
    start = end - timedelta(days=450)
    symbol = ("sh." if code[:1] in ("5", "6", "9") else "sz.") + code
    bars = []
    with _BAOSTOCK_LOCK:
        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"BaoStock login failed: {login.error_code}")
        try:
            result = bs.query_history_k_data_plus(
                symbol,
                "date,open,high,low,close,turn",
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                frequency="d",
                adjustflag="3",
            )
            while result.error_code == "0" and result.next():
                date, open_, high, low, close, turn = result.get_row_data()
                try:
                    bars.append({
                        "date": date, "open": float(open_), "high": float(high),
                        "low": float(low), "close": float(close),
                        "turnover_pct": float(turn or 0),
                    })
                except (TypeError, ValueError):
                    continue
            if result.error_code != "0":
                raise RuntimeError(f"BaoStock query failed: {result.error_code}")
        finally:
            bs.logout()
    return _estimate_chip_distribution(bars)


def _parse_pledge(df) -> list[dict]:
    """解析东财质押明细为干净结构."""
    out = []
    for _, r in df.iterrows():
        holder = str(r.get("股东名称", "")).strip()
        if not holder:
            continue
        out.append({
            "holder": holder,
            "pledge_shares": _to_float(r.get("质押股份数量")),
            "pct_of_holding": _to_float(r.get("占所持股份比例")),
            "pct_of_total": _to_float(r.get("占总股本比例")),
            "pledge_org": str(r.get("质押机构", "")),
        })
    return out


def _parse_chinaclear_pledge_html(
    body: str, *, code: str, expected_date: str,
) -> dict | None:
    """Parse one official ChinaClear weekly pledge row without a new dependency."""
    for raw_row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", body or "", flags=re.I | re.S):
        cells = [
            html_lib.unescape(re.sub(r"<[^>]+>", "", cell)).strip()
            for cell in re.findall(
                r"<td\b[^>]*>(.*?)</td>", raw_row, flags=re.I | re.S,
            )
        ]
        if len(cells) < 8 or cells[1] != code:
            continue
        observed_date = cells[0]
        if re.fullmatch(r"\d{8}", observed_date):
            observed_date = (
                f"{observed_date[:4]}-{observed_date[4:6]}-{observed_date[6:]}"
            )
        if observed_date != expected_date:
            continue
        unrestricted_10k = _to_float(cells[4])
        restricted_10k = _to_float(cells[5])
        ratio = _to_float(cells[7])
        if ratio is None or unrestricted_10k is None or restricted_10k is None:
            raise RuntimeError("中国结算股票质押行缺少比例或质押数量")
        return {
            "holder": "全部质押汇总",
            "pledge_shares": round(
                (unrestricted_10k + restricted_10k) * 10_000, 2,
            ),
            "pct_of_holding": None,
            "pct_of_total": ratio,
            "pledge_count": _to_float(cells[3]),
            "as_of": observed_date,
            "source": "中国结算单一股票质押比例（周度官方口径）",
            "source_url": "https://biz.chinaclear.cn/gpzy/pledgeRepo",
        }
    return None


def _fetch_chinaclear_pledge(
    session: requests.Session, *, code: str, trade_date: str,
) -> dict | None:
    response = session.post(
        "https://biz.chinaclear.cn/gpzy/queryStockPledgeInfo",
        data={
            "queryDate": trade_date.replace("-", "."),
            "secCde": code,
            "page": "1",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=8,
    )
    response.raise_for_status()
    return _parse_chinaclear_pledge_html(
        response.text, code=code, expected_date=trade_date,
    )


def _latest_pledge_report_date(
    session: requests.Session, params: dict,
) -> str:
    """Return the verified latest weekly report date, cached per client/day."""
    today = datetime.now(_SH_TZ).date().isoformat()
    key = (type(session), today)
    now = datetime.now(_SH_TZ).timestamp()
    with _PLEDGE_REPORT_CACHE_LOCK:
        cached = _PLEDGE_REPORT_CACHE.get(key)
        if cached and now - cached[0] < _PLEDGE_REPORT_CACHE_TTL:
            return cached[1]
    probe_response = session.get(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        params={**params, "pageSize": "1", "filter": ""}, timeout=8,
    )
    probe_response.raise_for_status()
    probe = probe_response.json()
    rows = (probe.get("result") or {}).get("data") or []
    latest = (
        str(rows[0].get("TRADE_DATE") or "")[:10]
        if probe.get("success") is True and rows else ""
    )
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", latest):
        raise RuntimeError("东方财富质押全市场最新周报表不可核验")
    report_age_days = (datetime.now(_SH_TZ).date() - datetime.fromisoformat(latest).date()).days
    if report_age_days < -1 or report_age_days > 14:
        raise RuntimeError("东方财富质押全市场最新周日期超出当前核验窗口")
    with _PLEDGE_REPORT_CACHE_LOCK:
        _PLEDGE_REPORT_CACHE[key] = (now, latest)
    return latest


def fetch_pledge(code: str) -> list[dict]:
    """中国结算周度单股质押比例；东财仅用于发现最新周并作降级备份。

    旧的单笔质押明细接口已经返回空结果；而否决规则需要的是公司全部质押占总股本
    比例，并非最大单笔占比。先从聚合报表取得最新周身份，再用中国结算同周同代码
    官方行复核；官方返回冲突时 fail closed，不能静默采用第三方值。
    """
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "sortColumns": "TRADE_DATE",
        "sortTypes": "-1",
        "pageSize": "1",
        "pageNumber": "1",
        "reportName": "RPT_CSDC_LIST",
        "columns": "ALL",
        "source": "WEB",
        "client": "WEB",
        "filter": f'(SECURITY_CODE="{code}")',
    }
    session = requests.Session()
    session.trust_env = False
    response = session.get(url, params=params, timeout=8)
    response.raise_for_status()
    payload = response.json()
    rows = (payload.get("result") or {}).get("data") or []
    if payload.get("success") is not True or not rows:
        # 东财对“该股票在质押汇总中无记录”返回 code=9201，而不是成功空数组。
        # 只有在同一报表的全市场探针仍能返回最新周数据时，才把个股缺席解释为
        # “最新周度汇总无质押记录”；报表本身异常时继续 fail closed。
        if payload.get("code") == 9201 and "返回数据为空" in str(payload.get("message") or ""):
            probe_as_of = _latest_pledge_report_date(session, params)
            if probe_as_of:
                return [{
                    "holder": "最新周度汇总无质押记录",
                    "pledge_shares": 0.0,
                    "pct_of_holding": 0.0,
                    "pct_of_total": 0.0,
                    "pledge_count": 0.0,
                    "as_of": probe_as_of,
                    "source": (
                        "东方财富上市公司质押比例（中登周度汇总口径；"
                        "全市场报表健康且个股无记录）"
                    ),
                }]
        raise RuntimeError(
            "东方财富上市公司质押比例未返回可核验记录: "
            + str(payload.get("message") or payload.get("code") or "unknown")
        )
    row = rows[0]
    ratio = _to_float(row.get("PLEDGE_RATIO"))
    trade_date = str(row.get("TRADE_DATE") or "")[:10] or None
    if ratio is None or trade_date is None:
        raise RuntimeError("东方财富上市公司质押比例缺少比例或交易日期")
    latest_report_date = _latest_pledge_report_date(session, params)
    if trade_date > latest_report_date:
        raise RuntimeError("个股质押日期晚于全市场最新周，拒绝生成治理结论")
    if trade_date < latest_report_date:
        return [{
            "holder": "最新周度汇总无质押记录",
            "pledge_shares": 0.0,
            "pct_of_holding": 0.0,
            "pct_of_total": 0.0,
            "pledge_count": 0.0,
            "as_of": latest_report_date,
            "source": (
                "东方财富上市公司质押比例（中登周度汇总口径；"
                f"全市场最新周健康，个股最近历史记录为{trade_date}且本周无记录）"
            ),
        }]
    official = None
    try:
        official = _fetch_chinaclear_pledge(
            session, code=code, trade_date=trade_date,
        )
    except requests.RequestException:
        official = None
    if official is not None:
        if abs(float(official["pct_of_total"]) - ratio) > 0.005:
            raise RuntimeError(
                "中国结算与东方财富质押比例不一致，拒绝生成治理结论"
            )
        return [official]
    return [{
        "holder": "全部质押汇总",
        "pledge_shares": _to_float(row.get("REPURCHASE_BALANCE")),
        "pct_of_holding": None,
        "pct_of_total": ratio,
        "pledge_count": _to_float(row.get("PLEDGE_DEAL_NUM")),
        "as_of": trade_date,
        "source": "东方财富上市公司质押比例（中国结算官方直连暂不可用的降级证据）",
    }]


# ─────────────────────────────────────────────
# 数据源：主营构成
# ─────────────────────────────────────────────
def _market_prefix(code: str) -> str:
    return "SH" if code[:1] in ("6", "9") else "SZ"


def fetch_business(code: str) -> dict:
    """东财主营构成。返回 {period, product:[], region:[], industry:[]}."""
    import akshare as ak

    df = ak.stock_zygc_em(symbol=f"{_market_prefix(code)}{code}")
    if df is None or len(df) == 0:
        return {}
    latest = df["报告日期"].max()  # datetime.date 对象
    sub = df[df["报告日期"] == latest]
    def _rows(cat: str) -> list[dict]:
        out = []
        for _, r in sub[sub["分类类型"] == cat].iterrows():
            name = str(r.get("主营构成", "")).strip()
            if not name or name == "其他(补充)":
                continue
            gm = _to_float(r.get("毛利率"))
            # akshare 主营毛利率是小数比例（0.2876），统一为百分比展示
            if gm is not None and gm < 1:
                gm = round(gm * 100, 2)
            out.append({
                "name": name,
                "revenue": _to_float(r.get("主营收入")),
                "rev_pct": _to_float(r.get("收入比例")),
                "profit_pct": _to_float(r.get("利润比例")),
                "gross_margin": gm,
            })
        return out
    return {
        "period": str(latest),
        "product": _rows("按产品分类"),
        "industry": _rows("按行业分类"),
        "region": _rows("按地区分类"),
    }


# ─────────────────────────────────────────────
# 数据源：巨潮公告
# ─────────────────────────────────────────────
_EVENT_KEYS = {
    "利好": ["回购", "增持", "中标", "业绩预增", "预盈", "分红", "派息", "签", "订单", "合同",
             "认证", "量产", "获批", "批文", "注册", "突破", "创新高", "净利润增长", "扭亏"],
    "利空": [
        "减持", "诉讼", "仲裁", "立案", "调查", "问询", "处罚", "违规",
        "造假", "重大违法", "纪律处分", "公开谴责", "通报批评", "监管措施",
        "警示函", "责令改正", "审计意见", "刑事", "亏损", "预亏", "变脸",
        "退市", "风险警示", "ST", "冻结", "拍卖", "失信", "违约", "终止",
    ],
    # 质押风险已按占总股本比例在治理维单独量化。公告标题中的“质押”本身
    # 只保留为治理观察标签；冻结、拍卖等明确恶化词仍会独立判为利空。
    "治理观察": ["质押"],
}
_LEVEL_BY_TYPE = {
    "回购": "A", "增持": "A", "中标": "A", "订单": "A", "合同": "A",
    "业绩预告": "A", "股权激励": "A", "重组": "A", "分红": "A", "减持": "A",
}

# 一票否决只接受标题中可明确识别的重大风险事实。普通“诉讼进展”、
# “调查报告”或股份“冻结”仍可作为黄灯观察，但不能仅凭宽泛词直接否决。
_MAJOR_EVENT_RISK_TERMS = {
    "financial_fraud": ("财务造假", "虚假记载", "伪造财务"),
    "major_litigation": ("重大诉讼", "重大仲裁"),
    "regulatory": (
        "立案告知书", "立案调查", "行政处罚决定书", "纪律处分",
        "公开谴责", "重大违法", "责令改正", "警示函", "监管措施",
        "退市风险警示", "强制退市", "终止上市",
    ),
}
_MAJOR_EVENT_RELIEF_TERMS = (
    "撤销行政处罚", "撤销处罚", "不予处罚", "免予处罚", "免于处罚",
    "撤诉", "诉讼终结", "仲裁终结", "终止调查", "调查终结",
)


def _major_event_risk_types(title: str) -> list[str]:
    """返回标题可直接支持的重大风险类型；解除/撤销类标题不触发。"""
    normalized = re.sub(r"\s+", "", str(title or ""))
    if not normalized or any(term in normalized for term in _MAJOR_EVENT_RELIEF_TERMS):
        return []
    return [
        risk_type for risk_type, terms in _MAJOR_EVENT_RISK_TERMS.items()
        if any(term in normalized for term in terms)
    ]


def _classify_event(title: str) -> dict:
    """把公告/资讯标题分类为 {sentiment, level, tags}."""
    tags = []
    sentiment = "中性"
    level = None
    for tag in _EVENT_KEYS["利好"]:
        if tag in title:
            tags.append(tag)
    for tag in _EVENT_KEYS["利空"]:
        if tag in title:
            tags.append(tag)
    for tag in _EVENT_KEYS["治理观察"]:
        if tag in title:
            tags.append(tag)
    if any(t in _EVENT_KEYS["利空"] for t in tags):
        sentiment = "利空"
    elif any(t in _EVENT_KEYS["利好"] for t in tags):
        sentiment = "利好"
    for k, lv in _LEVEL_BY_TYPE.items():
        if k in title:
            level = lv
            break
    major_risk_types = _major_event_risk_types(title)
    return {
        "sentiment": sentiment,
        "tags": tags,
        "level": level,
        "major_risk": bool(major_risk_types),
        "major_risk_types": major_risk_types,
    }


def fetch_announcements(code: str, days: int = 60) -> list[dict]:
    """巨潮资讯正式公告（近 N 天）。A 级证据源."""
    import akshare as ak

    end = datetime.now(_SH_TZ)
    start = end - timedelta(days=days)
    try:
        df = ak.stock_zh_a_disclosure_report_cninfo(
            symbol=code, start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"))
    except (KeyError, TypeError, ValueError):
        # AkShare 的巨潮适配器在 totalAnnouncement=0 时会选择不存在的列。
        # 用巨潮原接口确认是真空集合；网络或结构异常继续向上抛出并标为 unavailable。
        return _fetch_announcements_cninfo(code, start, end)
    if df is None or len(df) == 0:
        return []
    out = []
    for _, r in df.iterrows():
        title = str(r.get("公告标题", "")).strip()
        if not title:
            continue
        info = _classify_event(title)
        out.append({
            "title": title,
            "date": str(r.get("公告时间", ""))[:10],
            "url": str(r.get("公告链接", "")),
            "sentiment": info["sentiment"],
            "tags": info["tags"],
            "level": info["level"] or "A",
        })
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


@lru_cache(maxsize=1)
def _cninfo_org_ids() -> dict[str, str]:
    session = requests.Session()
    session.trust_env = False
    response = session.get(
        "http://www.cninfo.com.cn/new/data/szse_stock.json",
        headers={"User-Agent": "A-share-manual-decision-support/1.0"},
        timeout=8,
    )
    response.raise_for_status()
    payload = response.json()
    stock_list = payload.get("stockList") if isinstance(payload, dict) else None
    if not isinstance(stock_list, list):
        raise RuntimeError("cninfo_stock_map_schema")
    return {
        str(item.get("code") or ""): str(item.get("orgId") or "")
        for item in stock_list if isinstance(item, dict) and item.get("code") and item.get("orgId")
    }


def _fetch_announcements_cninfo(
    code: str, start: datetime, end: datetime,
) -> list[dict]:
    org_id = _cninfo_org_ids().get(code)
    if not org_id:
        raise RuntimeError("cninfo_stock_identity_missing")
    session = requests.Session()
    session.trust_env = False
    headers = {
        "User-Agent": "A-share-manual-decision-support/1.0",
        "Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
    }
    params = {
        "pageNum": "1", "pageSize": "30", "column": "szse",
        "tabName": "fulltext", "plate": "", "stock": f"{code},{org_id}",
        "searchkey": "", "secid": "", "category": "", "trade": "",
        "seDate": f"{start.date().isoformat()}~{end.date().isoformat()}",
        "sortName": "", "sortType": "", "isHLtitle": "true",
    }
    announcements = []
    expected_total = None
    page_num = 1
    while True:
        params["pageNum"] = str(page_num)
        response = session.post(
            "http://www.cninfo.com.cn/new/hisAnnouncement/query",
            data=params, headers=headers, timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
        page_rows = payload.get("announcements") if isinstance(payload, dict) else None
        total = payload.get("totalAnnouncement") if isinstance(payload, dict) else None
        if page_rows is None and total == 0:
            page_rows = []
        if not isinstance(page_rows, list) or not isinstance(total, int) or total < 0:
            raise RuntimeError("cninfo_announcements_schema")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise RuntimeError("cninfo_announcements_total_changed_during_pagination")
        announcements.extend(page_rows)
        if len(announcements) >= expected_total:
            break
        if not page_rows or page_num >= 100:
            raise RuntimeError("cninfo_announcements_incomplete_pagination")
        page_num += 1
    if len(announcements) != expected_total:
        raise RuntimeError("cninfo_announcements_count_mismatch")
    out = []
    for raw in announcements:
        if not isinstance(raw, dict) or str(raw.get("secCode") or "") != code:
            continue
        title = re.sub(r"<[^>]+>", "", str(raw.get("announcementTitle") or "")).strip()
        if not title:
            continue
        info = _classify_event(title)
        published = raw.get("announcementTime")
        try:
            published_date = datetime.fromtimestamp(float(published) / 1000, tz=_SH_TZ).date().isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            published_date = ""
        announcement_id = str(raw.get("announcementId") or "")
        raw_org_id = str(raw.get("orgId") or org_id)
        out.append({
            "title": title,
            "date": published_date,
            "url": (
                "http://www.cninfo.com.cn/new/disclosure/detail?"
                f"stockCode={code}&announcementId={announcement_id}&orgId={raw_org_id}"
            ) if announcement_id else "",
            "sentiment": info["sentiment"],
            "tags": info["tags"],
            "level": info["level"] or "A",
        })
    out.sort(key=lambda item: item["date"], reverse=True)
    return out


_AUDIT_OPINION_LABELS = {
    "standard_unqualified": "标准无保留意见",
    "unqualified_with_emphasis": "带强调事项或持续经营重大不确定性的无保留意见",
    "qualified": "保留意见",
    "disclaimer": "无法表示意见",
    "adverse": "否定意见",
}


def _classify_audit_opinion_text(text: str) -> dict:
    """Classify a signed audit report using explicit opinion headings only."""
    compact = re.sub(r"\s+", "", str(text or ""))
    if not compact:
        raise RuntimeError("cninfo_audit_report_text_empty")
    if re.search(r"一、(?:无法表示意见|无法发表意见)", compact):
        category = "disclaimer"
        evidence = "审计报告正文标题明确为无法表示意见"
    elif "一、否定意见" in compact:
        category = "adverse"
        evidence = "审计报告正文标题明确为否定意见"
    elif "一、保留意见" in compact or "形成保留意见的基础" in compact:
        category = "qualified"
        evidence = "审计报告正文标题明确为保留意见"
    elif "一、审计意见" in compact and "我们认为" in compact:
        has_emphasis = bool(re.search(
            r"[三四五六七八]、(?:强调事项|与持续经营相关的重大不确定性)",
            compact,
        ))
        category = "unqualified_with_emphasis" if has_emphasis else "standard_unqualified"
        evidence = (
            "审计报告发表无保留意见，并包含强调事项或持续经营重大不确定性段"
            if has_emphasis else
            "审计报告发表无保留意见，未识别强调事项或持续经营重大不确定性段"
        )
    else:
        raise RuntimeError("cninfo_audit_opinion_unclassified")
    firm_match = re.search(
        r"([\u4e00-\u9fffA-Za-z0-9·]{2,30}会计师事务所"
        r"(?:（特殊普通合伙）|（普通合伙）|\(特殊普通合伙\)|\(普通合伙\)))",
        compact,
    )
    return {
        "category": category,
        "label": _AUDIT_OPINION_LABELS[category],
        "evidence": evidence,
        "audit_firm": firm_match.group(1) if firm_match else None,
        "parser_version": "cninfo_audit_opinion_headings_v1",
    }


def fetch_audit_opinion(code: str, days: int = 500) -> dict:
    """Fetch and classify the latest exact annual audit report from CNINFO."""
    org_id = _cninfo_org_ids().get(code)
    if not org_id:
        raise RuntimeError("cninfo_stock_identity_missing")
    end = datetime.now(_SH_TZ)
    start = end - timedelta(days=days)
    session = requests.Session()
    session.trust_env = False
    headers = {
        "User-Agent": "A-share-manual-decision-support/1.0",
        "Referer": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
    }
    response = session.post(
        "https://www.cninfo.com.cn/new/hisAnnouncement/query",
        data={
            "pageNum": "1", "pageSize": "30", "column": "szse",
            "tabName": "fulltext", "plate": "",
            "stock": f"{code},{org_id}", "searchkey": "年度审计报告",
            "secid": "", "category": "", "trade": "",
            "seDate": f"{start.date().isoformat()}~{end.date().isoformat()}",
            "sortName": "", "sortType": "", "isHLtitle": "true",
        },
        headers=headers, timeout=(3.05, 10),
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("announcements") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("cninfo_audit_announcements_schema")
    exact = []
    for raw in rows:
        if not isinstance(raw, dict) or str(raw.get("secCode") or "") != code:
            continue
        title = re.sub(
            r"<[^>]+>", "", str(raw.get("announcementTitle") or ""),
        ).replace(" ", "").strip()
        if not re.fullmatch(r"\d{4}年?年度(?:财务报表)?审计报告", title):
            continue
        adjunct = str(raw.get("adjunctUrl") or "").strip().lstrip("/")
        if adjunct.lower().endswith(".pdf"):
            exact.append((float(raw.get("announcementTime") or 0), raw, title, adjunct))
    if not exact:
        raise RuntimeError("cninfo_exact_annual_audit_report_not_found")
    _, raw, title, adjunct = max(exact, key=lambda item: item[0])
    pdf_url = f"https://static.cninfo.com.cn/{adjunct}"
    pdf_response = session.get(pdf_url, headers=headers, timeout=(3.05, 20))
    pdf_response.raise_for_status()
    pdf_bytes = pdf_response.content
    if len(pdf_bytes) > 30 * 1024 * 1024:
        raise RuntimeError("cninfo_audit_report_too_large")
    if not pdf_bytes.startswith(b"%PDF"):
        raise RuntimeError("cninfo_audit_report_not_pdf")
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = "\n".join((page.extract_text() or "") for page in reader.pages[:12])
    parsed = _classify_audit_opinion_text(text)
    try:
        published_at = datetime.fromtimestamp(
            float(raw.get("announcementTime")) / 1000, tz=_SH_TZ,
        ).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        raise RuntimeError("cninfo_audit_announcement_time_invalid")
    return {
        **parsed,
        "report_year": int(title[:4]),
        "published_at": published_at,
        "announcement_id": str(raw.get("announcementId") or ""),
        "source": "巨潮资讯年度审计报告原文",
        "source_url": pdf_url,
        "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "pdf_size_bytes": len(pdf_bytes),
    }


# ─────────────────────────────────────────────
# 数据源：中证网事件（mom_radar 通道）
# ─────────────────────────────────────────────
def fetch_events(code: str) -> list[dict]:
    """中证网资讯中与个股/其行业相关的事件（D 级媒体证据，仅供观察）."""
    from trade_assistant import mom_radar

    snap = mom_radar.build_mom_radar_snapshot()
    if not isinstance(snap, dict):
        return []
    out = []
    for ev in snap.get("events", []):
        title = str(ev.get("title", ""))
        if code in title:
            out.append({
                "title": title,
                "url": str(ev.get("url", "")),
                "source": str(ev.get("source", "")),
                "date": str(ev.get("collected_at", "")),
                "sentiment": str(ev.get("impact", "中性")),
                "tags": ["媒体资讯"],
                "level": "D",
            })
    return out


# ─────────────────────────────────────────────
# 数据源：腾讯行情（交易可行性）
# ─────────────────────────────────────────────
def fetch_quote(code: str) -> dict:
    """腾讯日K+快照。返回 {name, price, prev_close, change_pct, volume, amount,
    high, low, board_limit_pct}；行情不可得返回空 dict."""
    prefix = "sh" if code[:1] == "6" else "sz"
    try:
        # 2026-08-18：绕工作站代理（127.0.0.1:17891）直连腾讯——该代理丢这些主机，
        # 与 market_intelligence._direct_get 同一依据（公网财经接口本就不需要代理）。
        import requests
        session = requests.Session()
        session.trust_env = False
        resp = session.get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": f"{prefix}{code},day,,,3,qfq"}, timeout=8)
        data = resp.json().get("data", {}).get(f"{prefix}{code}", {})
    except Exception:
        return {}
    qt = data.get("qt", {}).get(f"{prefix}{code}") if isinstance(data.get("qt"), dict) else None
    price = _to_float(qt[3]) if isinstance(qt, list) and len(qt) > 3 else None
    prev_close = _to_float(qt[4]) if isinstance(qt, list) and len(qt) > 4 else None
    name = qt[1] if isinstance(qt, list) and len(qt) > 1 else ""
    days = data.get("qfqday") or data.get("day") or []
    volume = None
    if days and isinstance(days[-1], list) and len(days[-1]) > 5:
        volume = _to_float(days[-1][5])  # 单位：手
    pct = (price / prev_close - 1) * 100 if price and prev_close else None
    as_of = None
    if isinstance(qt, list) and len(qt) > 30 and qt[30]:
        try:
            as_of = datetime.strptime(str(qt[30]), "%Y%m%d%H%M%S").replace(
                tzinfo=_SH_TZ,
            ).isoformat(timespec="seconds")
        except ValueError:
            as_of = None
    return {
        "name": name,
        "price": price,
        "prev_close": prev_close,
        "change_pct": round(pct, 2) if pct is not None else None,
        "volume_hands": volume,
        "amount": round(volume * 100 * price, 2) if volume and price else None,
        "board_limit_pct": 20.0 if code[:3] in _BOARD_LIMIT_PREFIXES else 10.0,
        "as_of": as_of,
        "fetched_at": _now(),
        "source": "腾讯财经公开行情",
    }


# ─────────────────────────────────────────────
# 行业分类（research 快照）
# ─────────────────────────────────────────────
def fetch_industry(code: str) -> str | None:
    for fname in ("industry_map_ths.json", "industry_map.json"):
        p = _CACHE_DIR / fname
        if not p.exists():
            continue
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(m, dict):
                continue
            v = m.get(code)
            if isinstance(v, str) and v:
                return v
            if isinstance(v, dict):
                for key in ("industry", "industry_l1", "industry_ths"):
                    if v.get(key):
                        return str(v[key])
        except (OSError, json.JSONDecodeError):
            continue
    return None


# ─────────────────────────────────────────────
# 资金流方向
# ─────────────────────────────────────────────
def fetch_fund_flow(code: str) -> dict | None:
    """Return both audited raw and normalized 主力净流入 values."""
    p = _CACHE_DIR / "fund_flow_live.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        info = d.get("stocks", {}).get(code)
        if isinstance(info, dict) and info.get("net_yi") is not None:
            raw = _to_float(info["net_yi"])
            as_of = d.get("ts") or d.get("date")
            # This file is an intraday snapshot, not a completed daily bar.  A
            # current-session timestamp is therefore causal and may be used;
            # completed-session-only semantics would incorrectly label every
            # live trading-day snapshot as future-dated before 16:00.
            freshness = audited_trading_day_age(
                as_of, allow_current_session=True,
            ) if as_of else {
                "status": "unknown", "days": None, "reason": "资金流快照时点缺失",
            }
            return {
                "net_yi": normalize_fund_flow_yi(raw),
                "raw_net_yi": raw,
                "magnitude_scale": FLOW_MAGNITUDE_SCALE,
                "unit": "亿元（normalized）",
                "as_of": as_of,
                "freshness_status": freshness.get("status"),
                "data_staleness_days": freshness.get("days"),
                "decision_eligible": raw is not None and freshness.get("status") == "fresh",
                "source": d.get("source_name") or "THS资金流快照",
            }
    except (OSError, json.JSONDecodeError):
        return None
    return None


# ─────────────────────────────────────────────
# 五道闸门判定
# ─────────────────────────────────────────────
def _gate_fundamentals(fin: list[dict], industry: str | None = None) -> dict:
    """基本面质量：利润持续性、现金流跟上与否、盈利质量、偿债。返回 {light, reasons, metrics}."""
    if not fin:
        return {"light": None, "reasons": ["财务数据源暂不可用（THS 未返回）"], "metrics": {}}
    latest = fin[-1]
    prev = fin[-2] if len(fin) >= 2 else None
    last_year = None
    for f in fin:
        if f["period"][:4] == str(int(latest["period"][:4]) - 1) and f["period"][5:7] == latest["period"][5:7]:
            last_year = f
    m = {
        "period": latest["period"],
        "data_as_of": latest.get("data_as_of"),
        "revenue": latest.get("revenue"),
        "net_profit": latest.get("net_profit"),
        "deducted": latest.get("deducted"),
        "revenue_yoy": latest.get("revenue_yoy"),
        "np_yoy": latest.get("np_yoy"),
        "deducted_yoy": latest.get("deducted_yoy"),
        "gross_margin": latest.get("gross_margin"),
        "net_margin": latest.get("net_margin"),
        "roe": latest.get("roe"),
        "debt_ratio": latest.get("debt_ratio"),
        "ocf_ps": latest.get("ocf_ps"),
        "eps": latest.get("eps"),
        "current_ratio": latest.get("current_ratio"),
        "goodwill": latest.get("goodwill"),
        "total_assets": latest.get("total_assets"),
        "goodwill_ratio_pct": latest.get("goodwill_ratio_pct"),
        "balance_sheet_source": latest.get("balance_sheet_source"),
        "goodwill_reporting_status": latest.get("goodwill_reporting_status"),
        "goodwill_official_evidence": latest.get("goodwill_official_evidence"),
        "prev_gross_margin": prev.get("gross_margin") if prev else None,
        "prev_net_margin": prev.get("net_margin") if prev else None,
    }
    reasons = []
    light = "green"

    # 1) 利润持续性：扣非 vs 净利
    np, ddt = latest.get("net_profit"), latest.get("deducted")
    if np and ddt is not None:
        ratio = ddt / np if np else 0
        if ratio < 0.5:
            light = "yellow"
            reasons.append(
                f"扣非利润仅占净利润 {ratio:.0%}（净利 {_format_yi_amount(np)} "
                f"vs 扣非 {_format_yi_amount(ddt)}）→ 利润依赖非经常性损益"
            )
        else:
            reasons.append(f"扣非利润占净利润 {ratio:.0%}，利润结构较扎实")

    # 2) 现金流是否跟上利润：现金是流量，单季度/半年度 OCF 强季节性（白酒 Q1 预缴税费、
    #    工程承包商 Q1 垫资、半导体 Q1 备货），且 ratio=ocf/eps 在 eps 很小时会被放大成巨大负值。
    #    专业口径——OCF 红（一票否决级）只看最近全年报告期(YYYY-12-31)：
    #    全年每股净流出有意义(ocf_ps < -0.2) 且 相对利润深度失血(ratio < -0.5) 才判红；
    #    只有季度报时（无全年期）负 OCF 一律黄灯跟踪，由 quality 维与综合分持续体现，不进否决。
    annual = None
    for f in reversed(fin):
        if str(f.get("period", ""))[5:] == "12-31":
            annual = f
            break
    ocf = annual.get("ocf_ps") if annual else None
    eps = annual.get("eps") if annual else None
    annual_period = str(annual.get("period") or "") if annual else ""
    annual_label = f"{annual_period[:4]}年全年" if len(annual_period) >= 4 else "最近全年"
    if ocf is not None and eps:
        cf_ratio = ocf / eps
        if cf_ratio < 0.3:
            if ocf < -0.2 and cf_ratio < -0.5:
                if light != "red":
                    light = "red"
                reasons.append(f"{annual_label}每股经营现金流为负（{ocf:.2f}，现金流/利润 = {cf_ratio:.2f}）→ 现金流深度失血，利润未转化为现金")
            elif cf_ratio < 0:
                if light != "red":
                    light = "yellow"
                reasons.append(f"{annual_label}每股经营现金流 {ocf:.2f}（现金流/利润 = {cf_ratio:.2f}）→ 现金流弱于利润，需跟踪")
            else:
                if light != "red":
                    light = "yellow"
                reasons.append(f"{annual_label}经营现金流/每股收益 = {cf_ratio:.2f} → 现金回款弱于利润")
        else:
            reasons.append(f"{annual_label}每股经营现金流 {ocf:.2f}，现金对利润覆盖充分")
    elif latest.get("ocf_ps") is not None:
        reasons.append(f"最近一期为非年度报告期（{latest.get('period','')}），阶段性现金流具季节性，不据此否决（留待全年报告核验）")

    # 3) 盈利质量趋势
    if m["gross_margin"] is not None:
        if m["prev_gross_margin"] is not None and m["gross_margin"] < m["prev_gross_margin"]:
            reasons.append(f"毛利率回落 {m['prev_gross_margin']:.1f}% → {m['gross_margin']:.1f}%")
        else:
            reasons.append(f"毛利率 {m['gross_margin']:.1f}%")

    # 4) 偿债
    dr = latest.get("debt_ratio")
    financial_balance_sheet = any(
        keyword in str(industry or "")
        for keyword in ("金融", "银行", "保险", "证券")
    )
    if dr is not None:
        if financial_balance_sheet:
            reasons.append(
                f"资产负债率 {dr:.1f}%（{industry or '金融业'}采用金融机构资本/资产质量口径，"
                "不套用工业企业负债率红线）"
            )
        elif dr > 85:
            light = "red"
            reasons.append(f"资产负债率 {dr:.1f}% 过高")
        elif dr > 70:
            light = "yellow"
            reasons.append(f"资产负债率 {dr:.1f}% 偏高")
        else:
            reasons.append(f"资产负债率 {dr:.1f}% 稳健")

    # 5) 增长同步性
    if latest.get("np_yoy") is not None and latest.get("revenue_yoy") is not None:
        if latest["np_yoy"] > 5 and latest["revenue_yoy"] < -10:
            light = "yellow"
            reasons.append(f"营收同比 {latest['revenue_yoy']:.0f}% 但净利同比 {latest['np_yoy']:.0f}% → 利润增长与营收脱节")
    required = ("revenue", "net_profit", "gross_margin", "debt_ratio")
    missing_fields = [key for key in required if m.get(key) is None]
    m["missing_fields"] = missing_fields
    m["verification_status"] = "complete" if not missing_fields else "partial"
    if missing_fields and light != "red":
        m["observed_light"] = light
        light = None
        reasons.append(
            "关键财务字段缺失（" + "、".join(missing_fields) + "），不作基本面通过断言"
        )
    return {"light": light, "reasons": reasons, "metrics": m}


def _gate_governance(
    pledge: list[dict], name: str, status_ok: bool,
    audit_opinion: dict | None = None, audit_status_ok: bool = False,
) -> dict:
    """治理风险：质押 + ST + 最新年度审计意见 + 信息完整度."""
    reasons = []
    light = "green"
    metrics = {"is_st": False}
    if name and "ST" in name.upper():
        light = "red"
        metrics["is_st"] = True
        reasons.append(f"证券简称含 ST（{name}）→ 退市风险警示")
    if not status_ok:
        reasons.append("质押数据源暂不可用（中国结算/东财均未返回），无法核验股权质押风险")
        if light == "green":
            light = None
        metrics["data_unavailable"] = True
    elif not pledge:
        metrics["pledge_count"] = 0
        metrics["max_holder_pledge_pct"] = 0.0
        metrics["max_total_pledge_pct"] = 0.0
        reasons.append("质押来源已核验，未返回有效质押记录")
    else:
        max_holding_pct = 0.0
        max_total_pct = 0.0
        for p in pledge:
            max_holding_pct = max(max_holding_pct, p.get("pct_of_holding") or 0)
            max_total_pct = max(max_total_pct, p.get("pct_of_total") or 0)
        metrics["max_holder_pledge_pct"] = round(max_holding_pct, 1)
        metrics["max_total_pledge_pct"] = round(max_total_pct, 2)
        latest = max(pledge, key=lambda item: str(item.get("as_of") or ""))
        count = latest.get("pledge_count")
        metrics["pledge_count"] = int(count if count is not None else len(pledge))
        metrics["pledge_as_of"] = latest.get("as_of")
        metrics["pledge_source"] = latest.get("source")
        # 质押风险核心看「占总股本」暴露度：占总股本 >30% 的高比例质押才判红/黄；
        # 单股东质押占所持高但占总股本 ≤30% 属 A 股常见现象 → 通过，保留提示不掩盖事实。
        if max_total_pct > 30 and max_holding_pct >= 50:
            light = "red"
            reasons.append(f"质押占总股本 {max_total_pct:.0f}% 且股东质押占所持 {max_holding_pct:.0f}% → 高比例质押，平仓风险大")
        elif max_total_pct > 30:
            light = "yellow"
            reasons.append(f"质押占总股本 {max_total_pct:.0f}% → 整体质押偏高")
        elif max_holding_pct >= 80:
            reasons.append(f"有股东质押占其持股 {max_holding_pct:.0f}%（占总股本 {max_total_pct:.0f}%）→ 单股东高比例质押，但质押占总股本未超过30%")
        elif max_holding_pct >= 50:
            light = "yellow"
            reasons.append(f"有股东质押占其持股 {max_holding_pct:.0f}% → 质押比例偏高")
        else:
            reasons.append(
                f"截至 {latest.get('as_of') or '—'} 质押占总股本 {max_total_pct:.2f}%"
                "，未超过风险阈值"
            )
    info_gaps = ["实际控制人", "高管变动"]
    if audit_status_ok and audit_opinion:
        category = str(audit_opinion.get("category") or "")
        metrics.update({
            "audit_opinion_category": category,
            "audit_opinion_label": audit_opinion.get("label"),
            "audit_report_year": audit_opinion.get("report_year"),
            "audit_published_at": audit_opinion.get("published_at"),
            "audit_firm": audit_opinion.get("audit_firm"),
            "audit_source": audit_opinion.get("source"),
            "audit_source_url": audit_opinion.get("source_url"),
            "audit_pdf_sha256": audit_opinion.get("pdf_sha256"),
        })
        reasons.append(
            f"{audit_opinion.get('report_year') or '最近年度'}审计意见："
            f"{audit_opinion.get('label') or category}"
        )
        if category in {"disclaimer", "adverse"}:
            light = "red"
            reasons.append("审计师无法表示或否定财务报表意见 → 严重财务报告风险")
        elif category in {"qualified", "unqualified_with_emphasis"} and light == "green":
            light = "yellow"
    else:
        info_gaps.append("审计意见")
        reasons.append("最新年度审计意见原文暂不可用，不作审计质量推断")
    # 监管问询、处罚和诉讼由独立 events 闸门消费巨潮公告核验，不能在治理维
    # 重复声称“数据源不可用”。这里仅保留尚未接入结构化来源的治理字段。
    metrics["info_gaps"] = info_gaps
    metrics["verification_status"] = "partial"
    reasons.append("实控人/高管结构化字段暂不可用；监管问询、处罚与诉讼由巨潮公告事件闸门单独核验")
    metrics["observed_pledge_st_light"] = light
    if light != "red":
        light = None
    return {"light": light, "reasons": reasons, "metrics": metrics}


def _gate_industry(biz: dict, industry: str | None, fund: dict | None) -> dict:
    """产业验证：主营结构 + 行业 + 资金流方向。返回 {light, reasons, metrics}."""
    reasons = []
    light = "green"
    metrics = {"industry": industry or "未知"}
    missing_evidence = []
    if industry:
        reasons.append(f"所属行业：{industry}")
    else:
        missing_evidence.append("industry")
    if not biz or not biz.get("product"):
        light = "yellow"
        reasons.append("主营构成数据暂不可用，无法核验收入结构")
        metrics["data_unavailable"] = True
        missing_evidence.append("business")
    else:
        products = biz["product"]
        top = products[0]
        metrics["top_business"] = top["name"]
        metrics["top_business_pct"] = top.get("rev_pct")
        metrics["top_margin"] = top.get("gross_margin")
        metrics["period"] = biz["period"]
        if top.get("rev_pct") is not None:
            if top["rev_pct"] > 0.85:
                light = "yellow"
                reasons.append(f"最大业务「{top['name']}」占收入 {top['rev_pct']:.0%} → 收入高度集中，单一业务依赖")
            else:
                reasons.append(f"最大业务「{top['name']}」占收入 {top['rev_pct']:.0%}")
        if top.get("gross_margin") is not None:
            reasons.append(f"核心业务毛利率 {top['gross_margin']:.1f}%")
    if fund and fund.get("decision_eligible") is True:
        net = fund.get("net_yi")
        if net is not None:
            if net > 0:
                reasons.append(f"主力资金净流入 {net:.2f} 亿（统一折算口径）")
            else:
                reasons.append(f"主力资金净流出 {abs(net):.2f} 亿（统一折算口径）")
            metrics["fund_net_yi"] = round(net, 2)
            metrics["fund_raw_net_yi"] = fund.get("raw_net_yi")
            metrics["fund_magnitude_scale"] = fund.get("magnitude_scale")
            metrics["fund_as_of"] = fund.get("as_of")
    elif fund:
        metrics["fund_as_of"] = fund.get("as_of")
        metrics["fund_freshness_status"] = fund.get("freshness_status") or "unknown"
        reasons.append("资金流快照过期或时效无法核验，不参与评分")
        missing_evidence.append("fund_flow_freshness")
    else:
        reasons.append("资金流数据暂不可用")
        missing_evidence.append("fund_flow")
    metrics["missing_evidence"] = missing_evidence
    metrics["verification_status"] = "complete" if not missing_evidence else "partial"
    if missing_evidence:
        metrics["observed_light"] = light
        light = None
    return {"light": light, "reasons": reasons, "metrics": metrics}


def _gate_events(
    anns: list[dict],
    media_events: list[dict],
    *,
    evidence_available: bool = True,
) -> dict:
    """事件证据：A/B 级正式公告 + D 级媒体，含证据分级与反证。返回 {light, reasons, evidence}."""
    evidence = []
    reasons = []
    # 正式公告 → A/B 级。展示保持紧凑，但一票否决扫描完整 60 日输入，
    # 防止公告较多的公司把较早重大风险挤出最近 15 条展示窗口。
    for a in anns[:15]:
        risk_types = _major_event_risk_types(a.get("title"))
        evidence.append({
            "level": a.get("level") or "A",
            "date": a.get("date"),
            "title": a.get("title"),
            "url": a.get("url"),
            "sentiment": a.get("sentiment"),
            "source": "巨潮公告",
            "tags": a.get("tags", []),
            "major_risk": bool(risk_types),
            "major_risk_types": risk_types,
        })
    # 媒体事件 → D 级（仅供观察）
    for e in media_events[:5]:
        evidence.append({
            "level": "D", "date": e.get("date"), "title": e.get("title"),
            "url": e.get("url"), "sentiment": e.get("sentiment"),
            "source": e.get("source", "中证网"), "tags": e.get("tags", []),
            "major_risk": False, "major_risk_types": [],
        })
    major_risk_evidence = []
    for a in anns:
        risk_types = _major_event_risk_types(a.get("title"))
        if not risk_types:
            continue
        major_risk_evidence.append({
            "level": a.get("level") or "A",
            "date": a.get("date"),
            "title": a.get("title"),
            "url": a.get("url"),
            "sentiment": a.get("sentiment"),
            "source": "巨潮公告",
            "tags": a.get("tags", []),
            "major_risk": True,
            "major_risk_types": risk_types,
        })
    coverage = {
        "lookback_days": 60,
        "source": "巨潮公告",
        "newest_date": max(
            (str(item.get("date") or "") for item in anns if item.get("date")),
            default=None,
        ),
        "oldest_date": min(
            (str(item.get("date") or "") for item in anns if item.get("date")),
            default=None,
        ),
        "scope": "完整60日公告标题重大风险筛查；不等同法律事实全文审查",
        "official_announcements_scanned": len(anns),
        "official_announcements_displayed": min(len(anns), 15),
    }
    if not evidence:
        if not evidence_available:
            return {
                "light": None,
                "reasons": ["公告/资讯来源不可用，无法核验近 60 日事件"],
                "evidence": [],
                "evidence_level": None,
                "verification_status": "unavailable",
                "coverage": coverage,
            }
        return {
            "light": "green",
            "reasons": ["公告/资讯来源已核验，近 60 日未发现匹配事件"],
            "evidence": [],
            "evidence_level": None,
            "verification_status": "verified_empty",
            "coverage": coverage,
        }
    # 汇总证据等级：取最高级（A 最高）
    level_order = {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1}
    best = max(evidence, key=lambda e: level_order.get(e.get("level"), 0))
    good = [e for e in evidence if e["sentiment"] == "利好"]
    bad = [e for e in evidence if e["sentiment"] == "利空"]
    displayed_titles = {str(e.get("title") or "") for e in evidence}
    bad.extend(
        e for e in major_risk_evidence
        if str(e.get("title") or "") not in displayed_titles
    )
    if bad:
        light = "yellow"
        reasons.append(f"存在利空信号 {len(bad)} 条（{' / '.join(e['tags'][0] if e['tags'] else '利空' for e in bad[:3])}）→ 需复核反证")
    else:
        light = "green"
    return {
        "light": light, "reasons": reasons, "evidence": evidence,
        "major_risk_evidence": major_risk_evidence,
        "evidence_level": best.get("level"),
        "verification_status": "verified",
        "coverage": coverage,
        "counts": {"a_b": sum(1 for e in evidence if e["level"] in ("A", "B")),
                   "d": sum(1 for e in evidence if e["level"] == "D")},
    }


def _gate_tradeable(q: dict, name: str) -> dict:
    """交易可行性：现价/流动性/涨跌停/停牌。返回 {light, reasons, metrics}."""
    reasons = []
    light = "green"
    metrics = {}
    price = _to_float(q.get("price"))
    if price is None or price <= 0:
        return {"light": "red", "reasons": ["行情不可得，可能停牌或代码无效 → 不可交易"], "metrics": {"no_quote": True}}
    prev = _to_float(q.get("prev_close"))
    metrics["price"] = price
    metrics["name"] = q.get("name") or name
    raw_as_of = q.get("as_of") or q.get("exchange_as_of") or q.get("data_time")
    try:
        as_of = datetime.fromisoformat(str(raw_as_of)) if raw_as_of else None
        if as_of is not None and as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=_SH_TZ)
        elif as_of is not None:
            as_of = as_of.astimezone(_SH_TZ)
    except ValueError:
        as_of = None
    metrics["as_of"] = as_of.isoformat(timespec="seconds") if as_of else None
    if as_of is None:
        metrics["quote_status"] = "unknown"
        return {
            "light": None,
            "reasons": ["行情价格存在但交易所时点缺失，交易可行性无法核验"],
            "metrics": metrics,
        }
    now = datetime.now(_SH_TZ)
    raw_age = (now - as_of).total_seconds()
    if raw_age < -60.0:
        metrics["quote_status"] = "invalid_future"
        metrics["age_seconds"] = round(raw_age)
        return {
            "light": None,
            "reasons": [f"行情时点 {metrics['as_of']} 晚于本机时间，不能核验当前可交易性"],
            "metrics": metrics,
        }
    age = max(0.0, raw_age)
    metrics["age_seconds"] = round(age)
    if as_of.date() != now.date() or age > 15 * 60:
        metrics["quote_status"] = "stale"
        return {
            "light": None,
            "reasons": [f"行情时点 {metrics['as_of']} 已过期，不能核验当前可交易性"],
            "metrics": metrics,
        }
    metrics["quote_status"] = "fresh"
    change_pct = _to_float(q.get("change_pct"))
    missing_fields = []
    if prev and change_pct is not None:
        metrics["change_pct"] = change_pct
        limit = _to_float(q.get("board_limit_pct")) or 10
        # 接近涨停/跌停（预留 0.5% 缓冲）
        if change_pct >= limit - 0.5:
            light = "red"
            reasons.append(f"已接近涨停（{change_pct:+.1f}%）→ 按现价买不进")
        elif change_pct <= -(limit - 0.5):
            light = "red"
            reasons.append(f"已接近跌停（{change_pct:+.1f}%）→ 流动性枯竭")
        else:
            reasons.append(f"现价 {price:.2f}（{change_pct:+.2f}%）")
    else:
        reasons.append("涨跌幅字段缺失，未用0%代替")
        missing_fields.append("change_pct_or_previous_close")
    amount = _to_float(q.get("amount"))
    if amount is not None:
        metrics["amount"] = round(amount, 2)
        if amount < 5_000_000:
            light = "yellow"
            reasons.append(f"成交额仅 {amount/1e4:.0f} 万 → 流动性偏低")
        else:
            reasons.append(f"成交额 {amount/1e8:.2f} 亿")
    else:
        reasons.append("成交量数据缺失")
        missing_fields.append("amount")
    metrics["missing_fields"] = missing_fields
    metrics["verification_status"] = "complete" if not missing_fields else "partial"
    if missing_fields and light != "red":
        metrics["observed_light"] = light
        light = None
    return {"light": light, "reasons": reasons, "metrics": metrics}


# ─────────────────────────────────────────────
# 公司侧评分（0-30 分制，避雷原始分）：五闸门 metrics → 分数化，供综合评分使用。
# 注意：这是"避雷强度"的绝对度量，非综合分内的直接得分。综合评分在 app.py
# _composite_grade 按 _COMPANY_WEIGHT 归一化（默认 15%，即 0-15 贡献分），
# 量化侧 score_timing 按 _QUANT_WEIGHT 缩放（默认 85%，即 ×0.85 → 0-85）。
# ─────────────────────────────────────────────
def score_gates(gates: dict) -> dict:
    """五闸门判定结果 → 公司侧避雷原始分（0-30）. 返回 {company_score, score_parts, note}."""
    parts: list[str] = []
    score = 0.0

    # ① 基本面质量（12 分）
    fundamental_gate = gates.get("fundamentals") or {}
    fin = fundamental_gate.get("metrics") or {}
    if not fin or fundamental_gate.get("light") is None:
        parts.append("财务数据缺失 · 基本面计 0")
    else:
        np_, ddt = fin.get("net_profit"), fin.get("deducted")
        if np_ and ddt is not None:
            ratio = ddt / np_ if np_ else 0
            s = 4 if ratio >= 0.8 else 3 if ratio >= 0.5 else 2 if ratio >= 0.3 else 0
            score += s; parts.append(f"利润结构(扣非/净利 {ratio:.0%}) +{s}")
        ocf, eps = fin.get("ocf_ps"), fin.get("eps")
        if ocf is not None and eps:
            cr = ocf / eps if eps else None
            s = 4 if (cr is not None and cr >= 0.5) else 3 if (cr is not None and cr >= 0) else 2 if (cr is not None and cr >= -0.1) else 0
            score += s; parts.append(f"现金流(ocf/eps={cr:.2f}) +{s}")
        gm, pgm = fin.get("gross_margin"), fin.get("prev_gross_margin")
        if gm is not None:
            s = 2 if (pgm is None or gm >= pgm) else 1
            score += s; parts.append(f"毛利率({gm:.1f}%{' 回落' if pgm is not None and gm < pgm else ''}) +{s}")
        dr = fin.get("debt_ratio")
        if dr is not None:
            s = 2 if dr <= 60 else 1.5 if dr <= 70 else 0.5 if dr <= 85 else 0
            score += s; parts.append(f"负债率({dr:.1f}%) +{s}")

    # ② 治理风险（6 分）
    gov = (gates.get("governance") or {}).get("metrics") or {}
    is_st = gov.get("is_st")
    mh, mt = gov.get("max_holder_pledge_pct"), gov.get("max_total_pledge_pct")
    if is_st:
        parts.append("ST 风险警示 · 治理计 0")
    elif gov.get("data_unavailable"):
        parts.append("质押数据源暂不可用，无法核验 · 治理计 0")
    elif gov.get("info_gaps"):
        parts.append("实控人/审计/监管等关键治理证据缺失 · 治理计 0")
    elif (mt or 0) > 30 and (mh or 0) >= 50:
        parts.append("质押占总股本>30% 且占所持≥50% · 治理计 0")
    elif (mt or 0) > 30:
        score += 2; parts.append(f"质押占总股本 {mt:.0f}% · 治理 +2")
    elif (mh or 0) >= 80:
        score += 4; parts.append(f"单股东质押占所持 {mh:.0f}%（总股本 {mt or 0:.0f}%）· 治理 +4")
    elif (mh or 0) >= 50:
        score += 4; parts.append(f"质押占所持 {mh:.0f}% · 治理 +4")
    else:
        score += 6; parts.append("治理结构清晰 · 治理 +6")

    # ③ 产业验证（6 分）
    industry_gate = gates.get("industry") or {}
    ind = industry_gate.get("metrics") or {}
    industry_verified = industry_gate.get("light") is not None
    top_pct = ind.get("top_business_pct")
    if not industry_verified or top_pct is None:
        parts.append("主营构成缺失 · 集中度计 0")
    else:
        s = 3 if top_pct <= 0.60 else 2 if top_pct <= 0.85 else 1 if top_pct <= 0.95 else 0.5
        score += s; parts.append(f"主营集中度({top_pct:.0%}) +{s}")
    fnet = ind.get("fund_net_yi") if industry_verified else None
    if fnet is None:
        parts.append("资金流缺失 · 资金流计 0")
    else:
        s = 2 if fnet > 0 else 1 if fnet >= -2 else 0.5
        score += s; parts.append(f"资金流({'流入' if fnet > 0 else '流出'}{abs(fnet):.1f}亿) +{s}")
    industry_point = bool(
        industry_verified and ind.get("industry") and ind.get("industry") != "未知"
    )
    score += 1 if industry_point else 0
    parts.append(f"行业可识别{' +1' if industry_point else ' +0'}")

    # ④ 事件证据（3 分）
    ev = gates.get("events") or {}
    level_order = {"A": 3, "B": 2, "C": 1, "D": 1}
    lvl = ev.get("evidence_level")
    s = level_order.get(lvl, 0)
    bad = sum(1 for e in (ev.get("evidence") or []) if e.get("sentiment") == "利空")
    if bad:
        s = max(0, s - 1)
        parts.append(f"含 {bad} 条利空事件 · 事件 {lvl or '无'} -1 → +{s}")
    else:
        parts.append(f"证据等级 {lvl or '无'} · 事件 +{s}")
    score += s

    # ⑤ 交易可行性（3 分）
    tr = gates.get("tradeable")
    if tr is None:
        parts.append("行情分未纳入 · 交易计 0")
    elif tr.get("light") is None:
        parts.append("行情时效无法核验 · 交易计 0")
    else:
        tm = (tr.get("metrics") or {})
        if tm.get("no_quote"):
            parts.append("行情不可得 · 交易计 0")
        elif tr.get("light") == "red":
            parts.append("接近涨/跌停 · 交易计 0")
        elif tm.get("amount") is not None and tm["amount"] < 5_000_000:
            score += 1; parts.append(f"成交额偏低({tm['amount']/1e4:.0f}万) · 交易 +1")
        else:
            score += 3; parts.append("交易顺畅 · 交易 +3")

    unverified = [key for key, gate in gates.items() if gate.get("light") is None]
    return {
        "company_score": round(score, 1),
        "score_parts": parts,
        "score_status": "partial" if unverified else "complete",
        "unverified_gates": unverified,
    }


# ─────────────────────────────────────────────
# 只读摘要：供推荐/轮询流程快速标注，绝不触发数据拉取
# ─────────────────────────────────────────────
def get_cached_summary(code: str, name: str = "", quote: dict | None = None) -> dict | None:
    """从缓存里读档案并给出轻量摘要（四闸门灯号 + 否决），无缓存返回 None.
    只读，不触发任何网络拉取，供 10s 轮询的推荐流程安全调用.
    quote：可选的实时行情 dict（price/change_pct/amount/as_of…）；
    缺少行情时不纳入交易分，也不得标记 decision_ready."""
    quote_key = _quote_summary_key(quote)
    key = (code, quote_key)
    now = datetime.now(_SH_TZ)
    with _CACHE_LOCK:
        hit = _SUMMARY_CACHE.get(key)
        if (
            hit
            and (now - hit[0]).total_seconds() < _SUMMARY_CACHE_TTL
            and _summary_sources_still_fresh(hit[1], now)
        ):
            return hit[1]
        if hit:
            _SUMMARY_CACHE.pop(key, None)
    entry = _load_cache_entry(code)
    source_names = {
        "financials": "同花顺财务摘要",
        "balance_sheet": "东方财富财务分析资产负债表",
        "research_reports": "东方财富个股研报元数据",
        "chip_distribution": "BaoStock日K/换手率 + 本站换手衰减估算",
        "business": "东财主营构成",
        "pledge": "东方财富上市公司质押比例（中登周度汇总口径）",
        "announcements": "巨潮公告",
        "audit_opinion": "巨潮资讯年度审计报告原文",
    }
    data_sources = {}
    for source_key, source_name in source_names.items():
        status_reader = (
            _pledge_cache_item_status
            if source_key == "pledge" else _cache_item_status
        )
        if source_key == "financials":
            status_reader = _financial_cache_item_status
        data_sources[source_key] = {
            **status_reader(entry.get(source_key), _TTL[source_key], now),
            "source": source_name,
        }
    cached_pledge_rows = (entry.get("pledge") or {}).get("data") or []
    if cached_pledge_rows and isinstance(cached_pledge_rows[0], dict):
        data_sources["pledge"]["source"] = (
            cached_pledge_rows[0].get("source") or data_sources["pledge"]["source"]
        )
    announcement_rows = (entry.get("announcements") or {}).get("data") or []
    announcement_dates = [
        str(item.get("date") or "")
        for item in announcement_rows if isinstance(item, dict) and item.get("date")
    ]
    data_sources["announcements"].update({
        "data_as_of": max(announcement_dates) if announcement_dates else None,
        "verification_checked_at": (
            data_sources["announcements"].get("fetched_at")
            if data_sources["announcements"]["status"] == "available" else None
        ),
    })

    def fresh_data(source_key: str):
        return (
            (entry.get(source_key) or {}).get("data")
            if data_sources[source_key]["status"] == "available"
            else None
        )

    fin = _merge_financial_balance(
        fresh_data("financials"),
        fresh_data("balance_sheet"),
    )
    biz = fresh_data("business")
    plg = fresh_data("pledge")
    ann = fresh_data("announcements")
    audit_opinion = fresh_data("audit_opinion")
    research_reports = fresh_data("research_reports") or []
    chip_distribution = fresh_data("chip_distribution")
    if not entry:
        return None
    industry = fetch_industry(code)
    gates = {
        "fundamentals": _gate_fundamentals(fin if fin else [], industry),
        "governance": _gate_governance(
            plg if plg else [], name,
            data_sources["pledge"]["status"] == "available",
            audit_opinion if isinstance(audit_opinion, dict) else None,
            data_sources["audit_opinion"]["status"] == "available",
        ),
        "industry": _gate_industry(biz if biz else {}, industry, fetch_fund_flow(code)),
        "events": _gate_events(
            ann if ann else [], [],
            evidence_available=data_sources["announcements"]["status"] == "available",
        ),
    }
    if quote:
        gates["tradeable"] = _gate_tradeable(quote, name)
    vetoed = [k for k, g in gates.items() if g["light"] == "red"]
    warnings = [k for k, g in gates.items() if g["light"] == "yellow"]
    missing_gates = [k for k, g in gates.items() if g["light"] is None]
    cs = score_gates(gates)
    fetched = [source["fetched_at"] for source in data_sources.values() if source.get("fetched_at")]
    decision_sources = ("financials", "balance_sheet", "business", "pledge", "announcements")
    complete = all(data_sources[source]["status"] == "available" for source in decision_sources)
    profile_ready = complete and not [gate for gate in missing_gates if gate != "tradeable"]
    decision_ready = profile_ready and quote is not None and not missing_gates
    source_status = "stale" if any(
        data_sources[source]["status"] == "stale" for source in decision_sources
    ) else "partial"
    summary = {
        "ready": decision_ready if quote is not None else profile_ready,
        "decision_ready": decision_ready,
        "status": "ready" if complete and not missing_gates else source_status,
        "generated_at": now.isoformat(timespec="seconds"),
        "oldest_source_at": min(fetched) if fetched else None,
        "newest_source_at": max(fetched) if fetched else None,
        "data_sources": data_sources,
        "gates": gates,
        "lights": {k: g["light"] for k, g in gates.items()},
        "veto": bool(vetoed),
        "vetoed_gates": vetoed,
        "warning_gates": warnings,
        "missing_gates": missing_gates,
        "company_score": cs["company_score"],
        "score_parts": cs["score_parts"],
        "score_status": cs["score_status"],
        "reasons": {k: g["reasons"][:3] for k, g in gates.items()},
        # 供统一专业评分复用：基本面指标（roe/毛利率/负债率/流动比率…），
        # 与 build_workstation 的 gates.fundamentals.metrics 同一口径（PIT 披露期）。
        "fundamental_metrics": (gates.get("fundamentals") or {}).get("metrics") or {},
        "research_reports": research_reports[:20],
        "chip_distribution": chip_distribution,
        "note": "只读缓存；过期来源仅供溯源，不进入闸门或正向评分。decision_ready 还要求带真实时点的有效行情。",
    }
    with _CACHE_LOCK:
        if quote_key is not None:
            # Keep at most one quote-bearing summary per code. This prevents a
            # live feed from growing the process cache on every price tick.
            for old_key in [
                old_key for old_key in _SUMMARY_CACHE
                if old_key[0] == code and old_key[1] is not None
            ]:
                _SUMMARY_CACHE.pop(old_key, None)
        _SUMMARY_CACHE[key] = (now, summary)
    return summary


# ─────────────────────────────────────────────
# 主入口：构建公司全景档案
# ─────────────────────────────────────────────
def build_company_profile(
    code: str, name: str = "", *, force_refresh: bool = False,
    include_audit_opinion: bool = True,
) -> dict:
    """构建单只股票的公司全景档案。所有数据带来源与时点，不可得即降级."""
    fin, fin_st = _cached_get(
        code, "financials", lambda: fetch_financials(code), _TTL["financials"],
        force_refresh=force_refresh,
    )
    balance, balance_st = _cached_get(
        code, "balance_sheet", lambda: fetch_balance_sheet(
            code, verify_official_blank=bool(force_refresh and include_audit_opinion),
        ), _TTL["balance_sheet"],
        force_refresh=force_refresh,
    )
    research_reports, research_st = _cached_get(
        code, "research_reports", lambda: fetch_research_reports(code), _TTL["research_reports"],
        force_refresh=force_refresh,
    )
    chip_distribution, chip_st = _cached_get(
        code, "chip_distribution", lambda: fetch_chip_distribution(code), _TTL["chip_distribution"],
        force_refresh=force_refresh,
    )
    biz, biz_st = _cached_get(
        code, "business", lambda: fetch_business(code), _TTL["business"],
        force_refresh=force_refresh,
    )
    plg, plg_st = _cached_get(
        code, "pledge", lambda: fetch_pledge(code), _TTL["pledge"],
        force_refresh=force_refresh,
    )
    ann, ann_st = _cached_get(
        code, "announcements", lambda: fetch_announcements(code), _TTL["announcements"],
        force_refresh=force_refresh,
    )
    if include_audit_opinion:
        audit_opinion, audit_st = _cached_get(
            code, "audit_opinion", lambda: fetch_audit_opinion(code),
            _TTL["audit_opinion"], force_refresh=force_refresh,
        )
    else:
        # Full-market profile warming must not synchronously download and parse
        # hundreds of annual-report PDFs. Reuse an already valid audit record,
        # while an explicitly opened stock page may request the supplementary
        # source through the normal bounded refresh endpoint.
        audit_item = _load_cache_item(code, "audit_opinion")
        audit_st = _cache_item_status(
            audit_item, _TTL["audit_opinion"],
        )["status"]
        audit_opinion = (
            audit_item.get("data")
            if audit_st == "available" and isinstance(audit_item, dict)
            else None
        )

    quote = fetch_quote(code)
    industry = fetch_industry(code)
    fund = fetch_fund_flow(code)
    try:
        media_events = fetch_events(code)
    except Exception:
        media_events = []

    merged_fin = _merge_financial_balance(
        fin if fin_st == "available" else None,
        balance if balance_st == "available" else None,
    )
    gates = {
        "fundamentals": _gate_fundamentals(merged_fin, industry),
        "governance": _gate_governance(
            plg if plg_st == "available" and plg else [],
            name or quote.get("name") or code,
            plg_st == "available",
            audit_opinion if audit_st == "available" and isinstance(audit_opinion, dict) else None,
            audit_st == "available",
        ),
        "industry": _gate_industry(biz if biz_st == "available" and biz else {}, industry, fund),
        "events": _gate_events(
            ann if ann_st == "available" and ann else [], media_events,
            evidence_available=ann_st == "available",
        ),
        "tradeable": _gate_tradeable(quote, name),
    }

    # 风险否决：任何红灯 → veto
    vetoed = [k for k, g in gates.items() if g["light"] == "red"]
    warnings = [k for k, g in gates.items() if g["light"] == "yellow"]
    # 灯号颜色缺失(None)视为信息不完整，不参与否决但提示
    missing = [k for k, g in gates.items() if g["light"] is None]

    # 公司侧评分（0-50，供综合评分 100 分制）
    cs = score_gates(gates)
    critical_statuses = (fin_st, balance_st, biz_st, plg_st, ann_st)
    profile_ready = all(status == "available" for status in critical_statuses) and not [
        gate for gate in missing if gate != "tradeable"
    ]
    decision_ready = profile_ready and gates["tradeable"].get("light") in {"green", "yellow"}

    cached_entry = _load_cache_entry(code)

    def cached_source(key: str, status: str, source: str) -> dict:
        return {
            **_cache_item_status(cached_entry.get(key), _TTL[key]),
            "status": status,
            "source": source,
        }

    announcement_dates = [
        str(item.get("date") or "")
        for item in (ann or []) if isinstance(item, dict) and item.get("date")
    ]
    announcement_source = cached_source("announcements", ann_st, "巨潮公告")
    announcement_source.update({
        "data_as_of": max(announcement_dates) if announcement_dates else None,
        "verification_checked_at": (
            announcement_source.get("fetched_at") if ann_st == "available" else None
        ),
    })

    return {
        "code": code,
        "name": name or quote.get("name") or code,
        "generated_at": _now(),
        "ready": profile_ready,
        "decision_ready": decision_ready,
        "status": "ready" if decision_ready else "partial",
        "gates": gates,
        "company_score": cs["company_score"],
        "score_parts": cs["score_parts"],
        "score_status": cs["score_status"],
        "veto": bool(vetoed),
        "vetoed_gates": vetoed,
        "warning_gates": warnings,
        "missing_gates": missing,
        "research_reports": (research_reports or [])[:20],
        "chip_distribution": chip_distribution,
        "data_sources": {
            "financials": cached_source("financials", fin_st, "同花顺财务摘要"),
            "balance_sheet": cached_source("balance_sheet", balance_st, "东方财富财务分析资产负债表"),
            "research_reports": cached_source("research_reports", research_st, "东方财富个股研报元数据"),
            "chip_distribution": {
                **cached_source(
                    "chip_distribution", chip_st,
                    "BaoStock不复权日K/换手率 + 本站换手衰减估算",
                ),
                "is_estimate": True,
            },
            "business": cached_source("business", biz_st, "东财主营构成"),
            "pledge": cached_source(
                "pledge", plg_st,
                (
                    plg[0].get("source")
                    if plg and isinstance(plg[0], dict) and plg[0].get("source")
                    else "中国结算单一股票质押比例（周度官方优先）"
                ),
            ),
            "announcements": announcement_source,
            "audit_opinion": cached_source(
                "audit_opinion", audit_st, "巨潮资讯年度审计报告原文",
            ),
            "quote": {
                "status": (gates["tradeable"].get("metrics") or {}).get("quote_status") or (
                    "unavailable" if not quote.get("price") else "unknown"
                ),
                "source": quote.get("source") or "腾讯行情",
                "as_of": quote.get("as_of"),
                "fetched_at": quote.get("fetched_at"),
            },
            "industry": {"status": "available" if industry else "unavailable", "source": "行业快照"},
            "fund_flow": {"status": "available" if fund else "unavailable", "source": "资金流快照"},
        },
        "conclusion": {
            "technical_candidate": None,  # 由调用方/前端结合评分填写
            "gate_summary": {k: g["light"] for k, g in gates.items()},
            "verdict": (
                "风险否决：是" if vetoed
                else "风险否决：无法完整核验" if missing
                else "风险否决：未触发"
            ),
            "note": "任何红灯闸门 → 禁止生成交易草稿；黄灯 → 降低优先级。数据来源见 data_sources。",
        },
    }
