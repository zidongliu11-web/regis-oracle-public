from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from math import isfinite
import re
import threading
from typing import Protocol
from zoneinfo import ZoneInfo

import requests


class MarketDataUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Quote:
    code: str
    name: str
    price: float | None
    change_pct: float | None
    amount: float | None
    open_price: float | None
    high: float | None
    low: float | None
    previous_close: float | None
    data_time: str
    source: str = "unknown"
    exchange_data_time: str | None = None
    freshness: str = "unknown"
    volume: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class QuoteProvider(Protocol):
    def get_quotes(self, codes: list[str]) -> dict[str, Quote]: ...


class SinaBatchQuoteProvider:
    """按自选代码批量读取新浪公开行情，避免每分钟下载整个 A 股市场。"""

    endpoint = "https://hq.sinajs.cn/list="
    timeout_contract = {
        "native_timeout_enforced": True,
        "connect_seconds": 3.05,
        "read_seconds": 7.0,
    }

    def get_quotes(self, codes: list[str]) -> dict[str, Quote]:
        if not codes:
            return {}
        requested = {_sina_symbol(code): code for code in dict.fromkeys(codes)}
        try:
            session = requests.Session()
            # 绕过工作站代理（127.0.0.1:17891）：它丢这些公网财经主机（CONNECT 悬停超时）。
            # 实测直连 hq.sinajs.cn ~0.1s（需 Referer）；与 mom_trading.market_intelligence 同因。
            session.trust_env = False
            response = session.get(
                self.endpoint + ",".join(requested),
                headers={
                    "Referer": "https://finance.sina.com.cn/",
                    "User-Agent": "Mozilla/5.0 A-Share-Trading-Assistant/1.0",
                },
                timeout=(3.05, 7.0),
            )
            response.raise_for_status()
            text = response.content.decode("gb18030", errors="replace")
        except Exception as exc:
            raise MarketDataUnavailable(f"新浪公开行情不可用: {type(exc).__name__}") from exc

        collected_at = datetime.now().astimezone()
        quotes: dict[str, Quote] = {}
        for line in text.splitlines():
            if '="' not in line:
                continue
            symbol = line.split("hq_str_", 1)[-1].split("=", 1)[0]
            requested_code = requested.get(symbol)
            if requested_code is None:
                continue
            payload = line.split('="', 1)[-1].rsplit('"', 1)[0]
            fields = payload.split(",")
            if len(fields) < 32 or not fields[0]:
                continue
            open_price = _number(fields[1])
            previous_close = _number(fields[2])
            price = _number(fields[3])
            date_text, time_text = fields[30], fields[31]
            exchange_time = f"{date_text}T{time_text}+08:00" if date_text and time_text else None
            change_pct = (
                (price / previous_close - 1) * 100
                if price is not None and previous_close not in (None, 0)
                else None
            )
            quotes[requested_code] = Quote(
                code=requested_code,
                name=fields[0],
                price=price,
                change_pct=round(change_pct, 3) if change_pct is not None else None,
                amount=_number(fields[9]),
                open_price=open_price,
                high=_number(fields[4]),
                low=_number(fields[5]),
                previous_close=previous_close,
                data_time=collected_at.isoformat(timespec="seconds"),
                source="新浪财经公开行情",
                exchange_data_time=exchange_time,
                freshness=_freshness(exchange_time, collected_at),
                volume=_number(fields[8]),
            )
        if not quotes:
            raise MarketDataUnavailable("新浪公开行情未返回有效数据")
        return quotes


class TencentBatchQuoteProvider:
    """按指定代码批量读取腾讯公开行情。"""

    endpoint = "https://qt.gtimg.cn/q="
    timeout_contract = {
        "native_timeout_enforced": True,
        "connect_seconds": 3.05,
        "read_seconds": 7.0,
    }

    def get_quotes(self, codes: list[str]) -> dict[str, Quote]:
        if not codes:
            return {}
        requested = {
            _tencent_symbol(code): code.split(".")[0]
            for code in dict.fromkeys(codes)
        }
        try:
            session = requests.Session()
            session.trust_env = False
            response = session.get(
                self.endpoint + ",".join(requested),
                headers={"User-Agent": "Mozilla/5.0 A-Share-Trading-Assistant/1.0"},
                timeout=(3.05, 7.0),
            )
            response.raise_for_status()
            text = response.content.decode("gb18030", errors="replace")
        except Exception as exc:
            raise MarketDataUnavailable(f"腾讯公开行情不可用: {type(exc).__name__}") from exc

        collected_at = datetime.now().astimezone()
        quotes: dict[str, Quote] = {}
        for line in text.splitlines():
            match = re.match(r'v_([a-z0-9]+)="(.*)";?', line)
            if match is None:
                continue
            symbol, payload = match.groups()
            requested_code = requested.get(symbol)
            fields = payload.split("~")
            if requested_code is None or len(fields) < 38 or not fields[1]:
                continue
            exchange_time = _tencent_exchange_time(fields[30])
            amount = None
            trade_summary = fields[35].split("/")
            if len(trade_summary) >= 3:
                amount = _number(trade_summary[2])
            volume_lots = _number(fields[36])
            quotes[requested_code] = Quote(
                code=requested_code,
                name=fields[1],
                price=_number(fields[3]),
                change_pct=_number(fields[32]),
                amount=amount,
                open_price=_number(fields[5]),
                high=_number(fields[33]),
                low=_number(fields[34]),
                previous_close=_number(fields[4]),
                data_time=collected_at.isoformat(timespec="seconds"),
                source="腾讯财经公开行情",
                exchange_data_time=exchange_time,
                freshness=_freshness(exchange_time, collected_at),
                volume=volume_lots * 100 if volume_lots is not None else None,
            )
        if not quotes:
            raise MarketDataUnavailable("腾讯公开行情未返回有效数据")
        return quotes


class AkshareQuoteProvider:
    """从 AKShare 获取公开 A 股现货；失败时显式报错，绝不回退到演示数据。"""

    timeout_contract = {
        "native_timeout_enforced": False,
        "reason": "AKShare 封装未暴露逐请求 connect/read timeout",
    }

    def get_quotes(self, codes: list[str]) -> dict[str, Quote]:
        if not codes:
            return {}
        try:
            import akshare as ak
            frame = ak.stock_zh_a_spot_em()
        except Exception as exc:  # 外部公开接口可能限流或休市
            raise MarketDataUnavailable(f"公开行情接口不可用: {type(exc).__name__}") from exc
        if "代码" not in frame.columns:
            raise MarketDataUnavailable("公开行情接口返回字段不完整")
        wanted = {code.split(".")[0] for code in codes}
        frame = frame.loc[frame["代码"].astype(str).isin(wanted)]
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        quotes: dict[str, Quote] = {}
        for _, row in frame.iterrows():
            code = str(row["代码"])
            quotes[code] = Quote(
                code=code,
                name=str(row.get("名称") or ""),
                price=_number(row.get("最新价")),
                change_pct=_number(row.get("涨跌幅")),
                amount=_number(row.get("成交额")),
                volume=_number(row.get("成交量")),
                open_price=_number(row.get("今开")),
                high=_number(row.get("最高")),
                low=_number(row.get("最低")),
                previous_close=_number(row.get("昨收")),
                data_time=now,
                source="AKShare / 东方财富公开现货接口",
                exchange_data_time=None,
                freshness="unknown",
            )
        return quotes


class FallbackQuoteProvider:
    def __init__(self, providers: list[QuoteProvider]):
        self.providers = providers

    def get_quotes(self, codes: list[str]) -> dict[str, Quote]:
        errors = []
        for provider in self.providers:
            try:
                quotes = provider.get_quotes(codes)
                if quotes:
                    return quotes
            except MarketDataUnavailable as exc:
                errors.append(str(exc))
        raise MarketDataUnavailable("；".join(errors) or "所有公开行情接口均不可用")

    def timeout_contracts(self) -> list[dict[str, object]]:
        return [
            {
                "provider": type(provider).__name__,
                **dict(getattr(provider, "timeout_contract", {})),
            }
            for provider in self.providers
        ]


class CachedQuoteProvider:
    def __init__(self, provider: QuoteProvider, ttl_seconds: int = 30):
        self.provider = provider
        self.ttl = timedelta(seconds=ttl_seconds)
        self._lock = threading.Lock()
        self.cached_at: dict[str, datetime] = {}
        self.cached: dict[str, Quote] = {}
        self._inflight: dict[str, threading.Event] = {}
        self.last_success_at: datetime | None = None
        self.last_exchange_data_at: datetime | None = None
        self.last_freshness: tuple[str, ...] = ()
        self.last_error_type: str | None = None

    def health_snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "last_success_at": (
                    self.last_success_at.isoformat(timespec="seconds")
                    if self.last_success_at else None
                ),
                "last_exchange_data_at": (
                    self.last_exchange_data_at.isoformat(timespec="seconds")
                    if self.last_exchange_data_at else None
                ),
                "last_freshness": list(self.last_freshness),
                "last_error_type": self.last_error_type,
                "inflight": len(self._inflight),
                "native_timeout_contracts": (
                    self.provider.timeout_contracts()
                    if hasattr(self.provider, "timeout_contracts")
                    else [{
                        "provider": type(self.provider).__name__,
                        **dict(getattr(self.provider, "timeout_contract", {})),
                    }]
                ),
            }

    def get_quotes(self, codes: list[str]) -> dict[str, Quote]:
        return self._get_quotes(codes, self.ttl)

    def get_quotes_fresh(
        self, codes: list[str], *, maximum_age_seconds: float = 5.0,
    ) -> dict[str, Quote]:
        """Use the shared cache, but cap its age for account monitoring."""
        maximum_age = timedelta(seconds=max(0.0, float(maximum_age_seconds)))
        return self._get_quotes(codes, min(self.ttl, maximum_age))

    def _get_quotes(
        self, codes: list[str], maximum_age: timedelta,
    ) -> dict[str, Quote]:
        wanted = {code.split(".")[0] for code in codes}
        if not wanted:
            return {}
        with self._lock:
            now = datetime.now().astimezone()
            stale = {
                code for code in wanted
                if code not in self.cached
                or code not in self.cached_at
                or now - self.cached_at[code] >= maximum_age
            }
            if not stale:
                return {code: self.cached[code] for code in wanted}
            claimed = stale.difference(self._inflight)
            waiting = stale.intersection(self._inflight)
            for code in claimed:
                self._inflight[code] = threading.Event()
            wait_events = [self._inflight[code] for code in waiting]

        if claimed:
            fetched: dict[str, Quote] = {}
            error_type: str | None = None
            try:
                # Network I/O stays outside the cache lock.  Unrelated symbols
                # can refresh concurrently while overlapping symbols still
                # share one in-flight request.
                fetched = self.provider.get_quotes(sorted(claimed))
            except Exception as exc:
                error_type = type(exc).__name__
                raise
            finally:
                with self._lock:
                    completed_at = datetime.now().astimezone()
                    missing = claimed.difference(fetched)
                    for code in missing:
                        self.cached.pop(code, None)
                        self.cached_at.pop(code, None)
                    self.cached.update(fetched)
                    self.cached_at.update({code: completed_at for code in fetched})
                    if fetched:
                        self.last_success_at = completed_at
                        exchange_times = [
                            _parse_exchange_time(quote.exchange_data_time)
                            for quote in fetched.values()
                        ]
                        usable_times = [value for value in exchange_times if value]
                        self.last_exchange_data_at = (
                            max(usable_times) if usable_times else None
                        )
                        self.last_freshness = tuple(sorted({
                            str(quote.freshness or "unknown")
                            for quote in fetched.values()
                        }))
                        self.last_error_type = None
                    elif error_type:
                        self.last_error_type = error_type
                    for code in claimed:
                        event = self._inflight.pop(code, None)
                        if event is not None:
                            event.set()

        # Provider calls have their own hard read/connect timeouts.  A waiter
        # never returns a stale value if an implementation violates that
        # contract and remains stuck.
        for event in wait_events:
            event.wait(timeout=15)

        with self._lock:
            return {
                code: self.cached[code]
                for code in wanted
                if code in self.cached
                and code in self.cached_at
                and datetime.now().astimezone() - self.cached_at[code] < maximum_age
            }


def _number(value):
    try:
        number = None if value is None else float(value)
        return number if number is not None and isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _parse_exchange_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result.astimezone()


def _sina_symbol(code: str) -> str:
    normalized = code.strip().lower()
    if normalized.startswith(("sh", "sz")):
        return normalized
    return f"sh{normalized}" if normalized.startswith("6") else f"sz{normalized}"


def _tencent_symbol(code: str) -> str:
    normalized = code.strip().lower().split(".")[0]
    if normalized.startswith(("sh", "sz", "bj")):
        return normalized
    if normalized.startswith(("4", "8", "9")):
        return f"bj{normalized}"
    return f"sh{normalized}" if normalized.startswith(("5", "6")) else f"sz{normalized}"


def _tencent_exchange_time(value: str) -> str | None:
    if not re.fullmatch(r"\d{14}", value or ""):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S").replace(
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ).isoformat(timespec="seconds")
    except ValueError:
        return None


def _freshness(exchange_time: str | None, collected_at: datetime) -> str:
    if not exchange_time:
        return "unknown"
    try:
        exchange_at = datetime.fromisoformat(exchange_time)
    except ValueError:
        return "unknown"
    if collected_at.tzinfo is None or exchange_at.tzinfo is None:
        return "unknown"
    exchange_zone = ZoneInfo("Asia/Shanghai")
    collected_at = collected_at.astimezone(exchange_zone)
    exchange_at = exchange_at.astimezone(exchange_zone)
    if exchange_at > collected_at:
        return "unknown"
    if collected_at.weekday() >= 5:
        return "closed_snapshot"
    if exchange_at.date() != collected_at.date():
        return "closed_snapshot"
    minutes = collected_at.hour * 60 + collected_at.minute
    if not (570 <= minutes < 690 or 780 <= minutes < 897):
        return "closed_snapshot"
    return "live" if collected_at - exchange_at <= timedelta(minutes=3) else "stale"
