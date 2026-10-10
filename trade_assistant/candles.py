from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time as clock_time, timedelta, timezone
from math import isfinite
from pathlib import Path
import threading
from typing import Protocol

import pandas as pd


class CandleDataUnavailable(RuntimeError):
    pass


# P4-2b：日K数据来源/新鲜度（进程内最近一次读取的记录，供 API meta 展示）。
# key = 归一化代码；value = {"source", "path", "mtime", "last_date", ...}
_DAILY_ORIGIN: dict[str, dict] = {}
_DAILY_ORIGIN_LOCK = threading.Lock()
_AKSHARE_PROVIDER_LOCK = threading.RLock()
_CHINA_TZ = timezone(timedelta(hours=8))


def _record_daily_origin(code: str, origin: dict | None) -> None:
    with _DAILY_ORIGIN_LOCK:
        if origin is None:
            _DAILY_ORIGIN.pop(code, None)
        else:
            _DAILY_ORIGIN[code] = origin


def daily_frame_origin(code: str) -> dict | None:
    """返回该代码最近一次日K读取的数据来源与新鲜度标签（进程内）。"""
    with _DAILY_ORIGIN_LOCK:
        return _DAILY_ORIGIN.get(code.strip().lower())


@dataclass(frozen=True)
class Candle:
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float | None = None
    # Bind provenance to these prices; a later read for the same code must not
    # change the basis of rows already held in a cache or response.
    source: str | None = None
    adjust_type: str | None = None

    def to_dict(self) -> dict:
        payload = asdict(self)
        # Preserve the established seven-field chart/strategy serialization.
        payload.pop("source")
        payload.pop("adjust_type")
        return payload


def candle_price_origin(candles: list[Candle]) -> dict | None:
    """Return a single actual source/basis only when every returned row agrees."""
    if not candles:
        return None
    sources = {getattr(row, "source", None) for row in candles}
    bases = {getattr(row, "adjust_type", None) for row in candles}
    if len(sources) != 1 or not next(iter(sources)) or len(bases) != 1:
        return None
    basis = next(iter(bases))
    if basis not in {"raw", "qfq", "hfq"}:
        return None
    return {"source": next(iter(sources)), "adjust_type": basis,
            "last_date": str(candles[-1].time)[:10]}


def completed_signal_candles(
    candles: list[Candle],
    interval: str,
    *,
    observed_at: datetime | None = None,
    settlement_delay_seconds: int = 5,
) -> list[Candle]:
    """Return only candles that were complete at ``observed_at``.

    Minute sources label a row with its bar end time.  Daily rows become
    eligible after the A-share 15:00 close plus a short provider settlement
    delay.  Invalid or future timestamps fail closed for signal computation;
    callers may still retain the unfiltered rows for chart display.
    """

    if interval != "1d" and not (
        interval.endswith("m") and interval[:-1].isdigit()
    ):
        raise ValueError(f"不支持的信号K线周期：{interval}")

    local_now = observed_at or datetime.now(_CHINA_TZ)
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=_CHINA_TZ)
    else:
        local_now = local_now.astimezone(_CHINA_TZ)
    delay = timedelta(seconds=max(0, settlement_delay_seconds))

    if interval == "1d":
        market_close = datetime.combine(
            local_now.date(),
            clock_time(15, 0),
            tzinfo=local_now.tzinfo,
        ) + delay
        latest_date = (
            local_now.date()
            if local_now >= market_close
            else local_now.date() - timedelta(days=1)
        )

        def is_complete(candle: Candle) -> bool:
            parsed = _parse_signal_candle_time(candle.time, local_now)
            return parsed is not None and parsed.date() <= latest_date

    else:
        cutoff = local_now - delay

        def is_complete(candle: Candle) -> bool:
            parsed = _parse_signal_candle_time(candle.time, local_now)
            return parsed is not None and parsed <= cutoff

    return [candle for candle in candles if is_complete(candle)]


def _parse_signal_candle_time(value: str, local_now: datetime) -> datetime | None:
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=local_now.tzinfo)
    return parsed.astimezone(local_now.tzinfo)


def market_state(now: datetime | None = None) -> dict:
    """A股界面时段；交易日事实只来自站内审计日历。"""
    from mom_trading.trading_calendar import trading_day_status

    current = now or datetime.now(_CHINA_TZ)
    current = (
        current.replace(tzinfo=_CHINA_TZ)
        if current.tzinfo is None else current.astimezone(_CHINA_TZ)
    )
    calendar = trading_day_status(current)
    day = current.date().isoformat()
    covered = bool(
        calendar.get("source") == "audited_baostock_calendar"
        and str(calendar.get("coverage_start") or "") <= day
        <= str(calendar.get("coverage_end") or "")
    )
    if not covered:
        return {
            "phase": "unknown", "label": "交易状态未知", "is_open": None,
            "reason": calendar.get("reason") or "审计交易日历未覆盖当前日期",
        }
    if calendar.get("is_trading_day") is not True:
        return {
            "phase": "closed", "label": "休市", "is_open": False,
            "reason": calendar.get("reason") or "非交易日",
        }

    minutes = current.hour * 60 + current.minute
    phase = "closed"
    label = "已收盘"
    if minutes < 555:      # 09:15 集合竞价前
        label = "未开盘"
    elif minutes < 570:      # 09:15–09:30 集合竞价
        phase = "auction"
        label = "集合竞价"
    elif minutes < 690:      # 09:30–11:30 早盘
        phase = "open"
        label = "交易中"
    elif minutes < 780:      # 11:30–13:00 午休
        phase = "lunch"
        label = "午间休市"
    elif minutes < 900:      # 13:00–15:00 午盘
        phase = "open"
        label = "交易中"
    else:
        label = "已收盘"
    return {
        "phase": phase, "label": label, "is_open": phase == "open",
        "reason": "审计交易日历与上海连续竞价时段",
    }


class CandleProvider(Protocol):
    def get_candles(
        self,
        code: str,
        limit: int = 160,
        interval: str = "5m",
    ) -> list[Candle]: ...

    def get_intraday(self, code: str) -> dict: ...


class AkshareFiveMinuteProvider:
    """通过 AKShare 读取真实分钟K线与日K线。"""

    timeout_contract = {
        "native_timeout_enforced": False,
        "reason": "AKShare 封装未暴露逐请求 connect/read timeout",
    }

    def get_candles(
        self,
        code: str,
        limit: int = 160,
        interval: str = "5m",
    ) -> list[Candle]:
        periods = {"1m": "1", "5m": "5", "15m": "15", "30m": "30"}
        if interval == "1d":
            return self._get_daily_candles(code, limit)
        if interval not in periods:
            raise CandleDataUnavailable(f"不支持的K线周期：{interval}")
        try:
            import akshare as ak

            frame = ak.stock_zh_a_minute(
                symbol=_sina_symbol(code),
                period=periods[interval],
                adjust="",
            )
        except Exception as exc:
            raise CandleDataUnavailable(
                f"{interval} K线接口不可用: {type(exc).__name__}"
            ) from exc
        required = {"day", "open", "high", "low", "close", "volume"}
        if frame.empty or not required.issubset(frame.columns):
            raise CandleDataUnavailable("5分钟K线没有返回完整字段")
        frame = _closed_minute_frame(frame)
        candles = []
        for _, row in frame.tail(max(30, min(limit, 2500))).iterrows():
            values = [_number(row.get(name)) for name in ("open", "high", "low", "close", "volume")]
            if any(value is None for value in values):
                continue
            candles.append(
                Candle(
                    time=str(row["day"]),
                    open=values[0],
                    high=values[1],
                    low=values[2],
                    close=values[3],
                    volume=values[4],
                    amount=_number(row.get("amount")),
                    source="akshare_minute",
                    adjust_type="raw",
                )
            )
        if len(candles) < 30:
            raise CandleDataUnavailable(f"有效{interval} K线不足30根")
        return candles

    def get_intraday(self, code: str) -> dict:
        """当前交易日的真实分时数据：1分钟收盘价 + 均价(VWAP) + 昨收 + 数据时间。

        与 get_candles 的滚动窗口不同，这里只保留「今天」的分钟线，用于绘制
        传统分时图（昨收基线 / 均价线 / 量能），并给出真实数据时间戳与市场状态。
        """
        import akshare as ak

        frame = ak.stock_zh_a_minute(symbol=_sina_symbol(code), period="1", adjust="")
        frame = _closed_minute_frame(frame)
        parsed = pd.to_datetime(frame["day"], errors="coerce")
        today = datetime.now().astimezone().date()
        today_frame = frame.loc[parsed.dt.date == today].copy()
        today_frame = today_frame.sort_values("day")

        prev_close = None
        prev_close_reason = None
        try:
            daily = self._get_daily_candles(code, 10)
            if daily:
                prev_close = (
                    daily[-2].close
                    if len(daily) > 1
                    and str(daily[-1].time)[:10] == today.isoformat()
                    else daily[-1].close
                )
            else:
                prev_close_reason = "日K无有效K线，无法推算昨收"
        except Exception as exc:
            # P4-2a：不再静默吞异常——记录原因供前端显示健康度而非无标注横杠
            prev_close = None
            prev_close_reason = (
                f"昨收推算失败（{type(exc).__name__}: {exc}）；"
                "昨收基线不可用，涨跌色按现价相对上一笔分时处理。"
            )

        candles = []
        cum_amount = 0.0
        cum_volume = 0.0
        for _, row in today_frame.iterrows():
            close = _number(row.get("close"))
            if close is None:
                continue
            volume = _number(row.get("volume")) or 0.0
            amount = _number(row.get("amount"))
            if amount is None:
                amount = close * volume
            cum_amount += amount
            cum_volume += volume
            vwap = (cum_amount / cum_volume) if cum_volume > 0 else close
            candles.append({
                "time": str(row["day"]),
                "close": close,
                "volume": volume,
                "amount": round(amount, 2),
                "vwap": round(vwap, 3),
            })

        return {
            "code": code,
            "previous_close": prev_close,
            "previous_close_reason": prev_close_reason,
            "data_time": candles[-1]["time"] if candles else None,
            "market": market_state(),
            "source": "AKShare公开1分钟行情",
            "volume_basis": "每分钟成交量",
            "candles": candles,
        }

    def _get_daily_candles(
        self,
        code: str,
        limit: int,
    ) -> list[Candle]:
        """读取真实不复权日K；本地审计库优先，近期分钟线补齐。"""

        today = datetime.now().astimezone().date()
        start = today - timedelta(days=max(730, limit * 3))
        normalized = code.strip().lower()
        origin = None
        frame, origin = self._load_local_daily_frame(normalized)
        if not frame.empty:
            before_last = str(frame["date"].max())
            frame = self._append_recent_minute_days(frame, normalized)
            after_last = str(frame["date"].max())
            if origin is not None:
                origin = {**origin, "source": "local_csv", "adjust_type": "raw"}
                if after_last > before_last:
                    origin["source"] = "local_csv+minute"
                    origin["appended_through"] = after_last
        else:
            try:
                import akshare as ak

                if normalized.startswith(("sh", "sz")):
                    frame = ak.index_zh_a_hist(
                        symbol=normalized[2:],
                        period="daily",
                        start_date=start.strftime("%Y%m%d"),
                        end_date=today.strftime("%Y%m%d"),
                    )
                else:
                    frame = ak.stock_zh_a_hist(
                        symbol=normalized,
                        period="daily",
                        start_date=start.strftime("%Y%m%d"),
                        end_date=today.strftime("%Y%m%d"),
                        adjust="",
                    )
                frame = frame.rename(columns={
                    "日期": "date",
                    "开盘": "open",
                    "最高": "high",
                    "最低": "low",
                    "收盘": "close",
                    "成交量": "volume",
                    "成交额": "amount",
                })
                origin = {"source": "akshare_fallback", "adjust_type": "raw"}
            except Exception as exc:
                raise CandleDataUnavailable(
                    f"日K接口不可用且本地审计库无该代码: {type(exc).__name__}"
                ) from exc
        _record_daily_origin(code.strip().lower(), origin)

        required = {"date", "open", "high", "low", "close", "volume"}
        if frame.empty or not required.issubset(frame.columns):
            raise CandleDataUnavailable("日K没有返回完整字段")
        frame = _closed_daily_frame(frame)

        candles: list[Candle] = []
        for _, row in frame.tail(max(35, min(limit, 1200))).iterrows():
            values = [
                _number(row.get(name))
                for name in ("open", "high", "low", "close", "volume")
            ]
            if any(value is None for value in values):
                continue
            candles.append(Candle(
                time=str(row["date"]),
                open=values[0],
                high=values[1],
                low=values[2],
                close=values[3],
                volume=values[4],
                amount=_number(row.get("amount")),
                source=(origin or {}).get("source"),
                adjust_type=(origin or {}).get("adjust_type"),
            ))
        if len(candles) < 35:
            raise CandleDataUnavailable("有效日K不足35根")
        return candles

    @staticmethod
    def _load_local_daily_frame(code: str) -> tuple[pd.DataFrame, dict | None]:
        """读取本地日K审计库；返回 (frame, meta)。

        meta（无数据/解析失败时为 None）携带数据来源与新鲜度标签：
          {"path", "mtime", "last_date", "source": "local_csv"}
        供 API 层展示健康度，避免“静默返回旧本地 CSV”无任何标注。
        """
        data_root = Path(__file__).resolve().parent.parent / "research" / "data"
        if code.startswith(("sh", "sz")):
            candidates = [
                data_root / "current" / f"{code}.csv",
                data_root / "daily" / f"{code}.csv",
            ]
        else:
            candidates = [
                data_root / "v4_daily" / f"{code}.csv.gz",
                data_root / "daily" / f"{code}.csv",
            ]
        for path in candidates:
            if not path.exists():
                continue
            try:
                frame = pd.read_csv(path)
            except (OSError, ValueError):
                continue
            if "date" in frame.columns:
                meta = {
                    "path": str(path.relative_to(data_root)) if path.is_relative_to(data_root) else str(path),
                    "mtime": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
                    "last_date": str(frame["date"].max()),
                    "source": "local_csv",
                }
                return frame, meta
        return pd.DataFrame(), None

    @staticmethod
    def _append_recent_minute_days(
        daily: pd.DataFrame,
        code: str,
        *,
        observed_at: datetime | None = None,
    ) -> pd.DataFrame:
        """用已收盘的新浪30分钟线补齐最近完整交易日。"""

        try:
            import akshare as ak

            minute = ak.stock_zh_a_minute(
                symbol=_sina_symbol(code),
                period="30",
                adjust="",
            )
        except Exception:
            return daily
        required = {"day", "open", "high", "low", "close", "volume"}
        if minute.empty or not required.issubset(minute.columns):
            return daily
        local_now = observed_at or datetime.now().astimezone()
        minute = _closed_minute_frame(
            minute,
            observed_at=local_now,
        ).copy()
        parsed_dates = pd.to_datetime(minute["day"], errors="coerce")
        minute = minute[parsed_dates.notna()].copy()
        minute["date"] = parsed_dates[parsed_dates.notna()].dt.date.astype(str)
        market_close = datetime.combine(
            local_now.date(),
            clock_time(15, 0, 5),
            tzinfo=local_now.tzinfo,
        )
        if local_now < market_close:
            minute = minute[minute["date"] != local_now.date().isoformat()]
        if minute.empty:
            return daily
        aggregations = {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
        if "amount" in minute.columns:
            aggregations["amount"] = "sum"
        recent = minute.groupby("date", as_index=False).agg(aggregations)
        combined = pd.concat([daily, recent], ignore_index=True, sort=False)
        combined["date"] = combined["date"].astype(str)
        return (
            combined.drop_duplicates(subset=["date"], keep="last")
            .sort_values("date")
            .reset_index(drop=True)
        )


def _closed_minute_frame(
    frame: pd.DataFrame,
    *,
    observed_at: datetime | None = None,
    settlement_delay_seconds: int = 5,
) -> pd.DataFrame:
    """Keep only bars whose source-labelled end time has passed.

    Sina labels an intraday bar with its end time.  A 13:35 row observed at
    13:32 is still forming and must not enter indicators, signals or cached
    API responses.  The short settlement delay also avoids treating a row as
    closed while the provider may still be updating it.
    """

    if frame.empty or "day" not in frame.columns:
        return frame.iloc[0:0].copy()
    local_now = observed_at or datetime.now().astimezone()
    parsed = pd.to_datetime(frame["day"], errors="coerce")
    try:
        if parsed.dt.tz is not None:
            parsed = parsed.dt.tz_convert(local_now.tzinfo).dt.tz_localize(None)
    except (AttributeError, TypeError):
        pass
    cutoff = pd.Timestamp(
        local_now.replace(tzinfo=None)
        - timedelta(seconds=max(0, settlement_delay_seconds))
    )
    return frame.loc[parsed.notna() & (parsed <= cutoff)].copy()


def _closed_daily_frame(
    frame: pd.DataFrame,
    *,
    observed_at: datetime | None = None,
    settlement_delay_seconds: int = 5,
) -> pd.DataFrame:
    """Exclude future dates and the current trading day before its close."""

    if frame.empty or "date" not in frame.columns:
        return frame.iloc[0:0].copy()
    local_now = observed_at or datetime.now().astimezone()
    parsed = pd.to_datetime(frame["date"], errors="coerce")
    market_close = datetime.combine(
        local_now.date(),
        clock_time(15, 0),
        tzinfo=local_now.tzinfo,
    ) + timedelta(seconds=max(0, settlement_delay_seconds))
    latest_allowed = (
        local_now.date()
        if local_now >= market_close
        else local_now.date() - timedelta(days=1)
    )
    return frame.loc[
        parsed.notna() & (parsed.dt.date <= latest_allowed)
    ].copy()


TENCENT_MKLINE_RAW_TIMESTAMP_SEMANTICS = (
    "unverified_conservatively_treated_as_bar_start"
)


def _tencent_mk_time(compact: str, interval_minutes: int) -> str:
    """Normalize an undocumented Tencent minute label to a safe bar-end time.

    Tencent does not publish a stable contract proving whether ``mkline`` labels
    are bar starts or bar ends.  Treating a start label as an end can admit a
    forming candle, so the execution path conservatively adds one full interval.
    If the upstream label was already an end this only delays eligibility; it
    cannot introduce future information.
    """
    if not compact or len(compact) < 12:
        return compact
    try:
        raw = datetime.strptime(compact[:12], "%Y%m%d%H%M")
    except ValueError:
        return compact
    return (raw + timedelta(minutes=max(1, interval_minutes))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


class TencentCandleProvider:
    """腾讯行情分钟/日K提供者（Sina/eastmoney 不可达时的兜底源）。

    数据源：
    - 分钟K:  https://ifzq.gtimg.cn/appstock/app/kline/mkline   （m1/m5/m15/m30）
    - 日K:    https://ifzq.gtimg.cn/appstock/app/fqkline/get    （前复权）
    - 分时:   https://web.ifzq.gtimg.cn/appstock/app/minute/query（1分钟）
    返回接口与 AkshareFiveMinuteProvider 一致（get_candles / get_intraday）。
    """

    _MKLINE = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
    _FQKLINE = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
    _MINUTE = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
    _HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
    timeout_contract = {
        "native_timeout_enforced": True,
        "connect_seconds": 3.05,
        "read_seconds": 10.0,
    }

    def _get_json(self, url: str) -> dict:
        # 2026-08-18 根因修复：绕过工作站代理（127.0.0.1:17891）直连腾讯——该代理丢
        # 这些主机（CONNECT 等不到上游、15s 超时甚至无限阻塞）。此前 _timing_signals
        # _scoring 每只票取 5m K 线 + 分时都要各烧一个 15s 代理超时，把 kronos 预填
        # _assess_candidate 拖到 25s 有界超时之外 → 全部 fail-closed「统一评估超时」。
        # 实测直连 ifzq.gtimg.cn ~0.3s。与 market_intelligence._direct_get /
        # SinaBatchQuoteProvider 同一依据：公网财经接口本就不需要代理。
        import requests
        session = requests.Session()
        session.trust_env = False
        resp = session.get(url, headers=self._HEADERS, timeout=(3.05, 10.0))
        resp.raise_for_status()
        return resp.json()

    def get_candles(
        self, code: str, limit: int = 160, interval: str = "5m",
    ) -> list[Candle]:
        periods = {"1m": "m1", "5m": "m5", "15m": "m15", "30m": "m30"}
        if interval == "1d":
            return self._get_daily(code, limit)
        if interval not in periods:
            raise CandleDataUnavailable(f"不支持的K线周期：{interval}")
        sym = _sina_symbol(code)
        n = min(limit, 540)
        try:
            data = self._get_json(
                f"{self._MKLINE}?param={sym},{periods[interval]},,{n}"
            )
            node = (data.get("data", {}).get(sym) or {})
            rows = node.get(periods[interval]) or []
        except Exception as exc:
            raise CandleDataUnavailable(
                f"腾讯{interval}K线不可用: {type(exc).__name__}"
            ) from exc
        candles: list[Candle] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            try:
                open_ = float(row[1])
                close = float(row[2])
                high = float(row[3])
                low = float(row[4])
                vol = float(row[5])
            except (TypeError, ValueError):
                continue
            vol_shares = vol * 100  # 手 → 股
            candles.append(Candle(
                time=_tencent_mk_time(
                    str(row[0]), int(interval.removesuffix("m")),
                ),
                open=open_, high=high, low=low, close=close,
                volume=vol_shares,
                amount=round(close * vol_shares, 2),
                source="tencent_mkline",
                adjust_type="raw",
            ))
        if len(candles) < 30:
            raise CandleDataUnavailable(f"腾讯{interval}K线不足30根")
        return candles

    def _get_daily(self, code: str, limit: int) -> list[Candle]:
        sym = _sina_symbol(code)
        n = min(limit, 1200)
        try:
            data = self._get_json(f"{self._FQKLINE}?param={sym},day,,,{n},qfq")
            node = (data.get("data", {}).get(sym) or {})
            if node.get("qfqday"):
                rows, basis = node["qfqday"], "qfq"
            else:
                rows, basis = node.get("day") or [], "raw"
        except Exception as exc:
            raise CandleDataUnavailable(
                f"腾讯日K不可用: {type(exc).__name__}"
            ) from exc
        candles: list[Candle] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            try:
                open_ = float(row[1])
                close = float(row[2])
                high = float(row[3])
                low = float(row[4])
                vol = float(row[5])
            except (TypeError, ValueError):
                continue
            vol_shares = vol * 100  # 手 → 股
            candles.append(Candle(
                time=str(row[0])[:10],
                open=open_, high=high, low=low, close=close,
                volume=vol_shares,
                amount=round(close * vol_shares, 2),
                source="tencent_fqkline",
                adjust_type=basis,
            ))
        if len(candles) < 35:
            raise CandleDataUnavailable("腾讯日K不足35根")
        _record_daily_origin(code.strip().lower(), {
            "source": "tencent_fqkline",
            "adjust_type": basis,
            "last_date": str(candles[-1].time)[:10],
            "note": "腾讯前复权日K（qfqday）" if basis == "qfq" else "腾讯不复权日K（day）",
        })
        return candles

    def get_intraday(self, code: str) -> dict:
        sym = _sina_symbol(code)
        try:
            data = self._get_json(f"{self._MINUTE}?code={sym}")
            node = data.get("data", {}).get(sym) or {}
            inner = node.get("data", {}) or {}
            rows = inner.get("data") or []
            date_str = str(inner.get("date") or "")
            qt = (node.get("qt", {}).get(sym) or []) if isinstance(node.get("qt"), dict) else []
        except Exception as exc:
            raise CandleDataUnavailable(
                f"腾讯分时不可用: {type(exc).__name__}"
            ) from exc

        prev_close = None
        prev_close_reason = None
        try:
            if len(qt) > 4 and qt[4]:
                prev_close = float(qt[4])
            else:
                prev_close_reason = "上游分时快照未返回昨收字段（qt[4] 缺失）；昨收基线不可用。"
        except (TypeError, ValueError) as exc:
            prev_close = None
            prev_close_reason = (
                f"上游昨收字段解析失败（{type(exc).__name__}）；昨收基线不可用。"
            )

        candles = []
        previous_cumulative_amount = 0.0
        previous_cumulative_volume = 0.0
        for row in rows:
            parts = str(row).split()
            if len(parts) < 4:
                continue
            hhmm, price_s, vol_s, amt_s = parts[:4]
            try:
                price = float(price_s)
                vol_shares = float(vol_s) * 100
                amt = float(amt_s)
            except (TypeError, ValueError):
                continue
            # Tencent's minute feed labels volume and amount as day-to-date
            # cumulative values.  The chart needs per-minute bars, while VWAP
            # needs the cumulative pair exactly once.
            minute_volume = max(0.0, vol_shares - previous_cumulative_volume)
            minute_amount = max(0.0, amt - previous_cumulative_amount)
            previous_cumulative_volume = vol_shares
            previous_cumulative_amount = amt
            vwap = (amt / vol_shares) if vol_shares > 0 else price
            if len(date_str) == 8:
                t = (f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]} "
                     f"{hhmm[:2]}:{hhmm[2:]}:00")
            else:
                t = f"{hhmm[:2]}:{hhmm[2:]}:00"
            candles.append({
                "time": t,
                "close": price,
                "volume": minute_volume,
                "amount": round(minute_amount, 2),
                "cumulative_volume": vol_shares,
                "cumulative_amount": round(amt, 2),
                "vwap": round(vwap, 3),
            })
        return {
            "code": code,
            "previous_close": prev_close,
            "previous_close_reason": prev_close_reason,
            "data_time": candles[-1]["time"] if candles else None,
            "market": market_state(),
            "source": "腾讯证券公开分时",
            "volume_basis": "每分钟成交量（由源累计量差分）",
            "candles": candles,
        }


class FallbackCandleProvider:
    """按顺序尝试多个K线源，全部失败才抛错。"""

    def __init__(self, providers: list):
        self.providers = providers

    def timeout_contracts(self) -> list[dict[str, object]]:
        return [
            {
                "provider": type(provider).__name__,
                **dict(getattr(provider, "timeout_contract", {})),
            }
            for provider in self.providers
        ]

    def get_candles(
        self, code: str, limit: int = 160, interval: str = "5m",
    ) -> list[Candle]:
        last: Exception | None = None
        for provider in self.providers:
            try:
                if isinstance(provider, AkshareFiveMinuteProvider):
                    with _AKSHARE_PROVIDER_LOCK:
                        return provider.get_candles(code, limit, interval)
                return provider.get_candles(code, limit, interval)
            except Exception as exc:
                last = exc
        raise CandleDataUnavailable("所有K线源均不可用") from last

    def get_intraday(self, code: str) -> dict:
        last: Exception | None = None
        for provider in self.providers:
            try:
                if isinstance(provider, AkshareFiveMinuteProvider):
                    with _AKSHARE_PROVIDER_LOCK:
                        return provider.get_intraday(code)
                return provider.get_intraday(code)
            except Exception as exc:
                last = exc
        raise CandleDataUnavailable("所有分时源均不可用") from last


class CachedCandleProvider:
    def __init__(self, provider: CandleProvider, ttl_seconds: int = 30):
        self.provider = provider
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self.cache: dict[tuple[str, str], tuple[datetime, object]] = {}
        self._inflight: dict[tuple[str, str], threading.Event] = {}

    def health_snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "inflight": len(self._inflight),
                "native_timeout_contracts": (
                    self.provider.timeout_contracts()
                    if hasattr(self.provider, "timeout_contracts") else []
                ),
            }

    def _cached_or_owner(
        self, cache_key: tuple[str, str], ttl: int, minimum_rows: int | None = None,
    ) -> tuple[object | None, threading.Event, bool]:
        with self._lock:
            now = datetime.now().astimezone()
            cached = self.cache.get(cache_key)
            if (
                cached and (now - cached[0]).total_seconds() < ttl
                and (minimum_rows is None or len(cached[1]) >= minimum_rows)
            ):
                value = cached[1]
                return value, threading.Event(), False
            event = self._inflight.get(cache_key)
            if event is None:
                event = threading.Event()
                self._inflight[cache_key] = event
                return None, event, True
            return None, event, False

    def _publish(
        self, cache_key: tuple[str, str], event: threading.Event,
        value: object | None,
    ) -> None:
        with self._lock:
            if value is not None:
                self.cache[cache_key] = (datetime.now().astimezone(), value)
            if self._inflight.get(cache_key) is event:
                self._inflight.pop(cache_key, None)
            event.set()

    def get_candles(
        self,
        code: str,
        limit: int = 160,
        interval: str = "5m",
        cache_ttl_seconds: int | None = None,
    ) -> list[Candle]:
        cache_key = (code, interval)
        ttl = self.ttl_seconds if cache_ttl_seconds is None else max(
            0, cache_ttl_seconds
        )
        while True:
            cached, event, owner = self._cached_or_owner(
                cache_key, ttl, minimum_rows=limit,
            )
            if cached is not None:
                return cached[-limit:]
            if owner:
                break
            if not event.wait(30):
                raise CandleDataUnavailable("同一标的K线请求超时")
        candles = None
        try:
            candles = self.provider.get_candles(
                code,
                max(limit, 160),
                interval=interval,
            )
            return candles[-limit:]
        finally:
            self._publish(cache_key, event, candles)

    def get_intraday(self, code: str, ttl_seconds: int = 15) -> dict:
        cache_key = ("intraday", code)
        while True:
            cached, event, owner = self._cached_or_owner(
                cache_key, ttl_seconds,
            )
            if cached is not None:
                return cached
            if owner:
                break
            if not event.wait(30):
                raise CandleDataUnavailable("同一标的分时请求超时")
        data = None
        try:
            data = self.provider.get_intraday(code)
            return data
        finally:
            self._publish(cache_key, event, data)


def research_signals(candles: list[Candle]) -> list[dict]:
    """未验证的技术观察：EMA5/EMA20 交叉，向上交叉额外要求放量。

    这些标记只用于后续回测研究，不能进入强信号或企业微信买入提醒。
    """

    if len(candles) < 25:
        return []
    closes = [candle.close for candle in candles]
    volumes = [candle.volume for candle in candles]
    ema_fast = _ema(closes, 5)
    ema_slow = _ema(closes, 20)
    signals = []
    for index in range(20, len(candles)):
        crossed_up = ema_fast[index - 1] <= ema_slow[index - 1] and ema_fast[index] > ema_slow[index]
        crossed_down = ema_fast[index - 1] >= ema_slow[index - 1] and ema_fast[index] < ema_slow[index]
        average_volume = sum(volumes[index - 20:index]) / 20
        volume_ratio = volumes[index] / average_volume if average_volume > 0 else 0
        if crossed_up and volume_ratio >= 1.2:
            signals.append({
                "time": candles[index].time,
                "price": candles[index].close,
                "action": "research_bullish_observation",
                "direction": "bullish",
                "signal_type": "technical_observation",
                "label": "EMA向上交叉观察",
                "reason": f"EMA5上穿EMA20，最新K线相对前20根成交量 {volume_ratio:.2f}",
                "research_status": "unverified",
                "signal_price_basis": "bar_close",
                "confirmation": "仅在该K线收盘后确认",
            })
        elif crossed_down:
            signals.append({
                "time": candles[index].time,
                "price": candles[index].close,
                "action": "research_bearish_observation",
                "direction": "bearish",
                "signal_type": "technical_observation",
                "label": "EMA向下交叉观察",
                "reason": "EMA5下穿EMA20",
                "research_status": "unverified",
                "signal_price_basis": "bar_close",
                "confirmation": "仅在该K线收盘后确认",
            })
    return signals


def technical_snapshot(candles: list[Candle]) -> dict:
    """Return explainable, non-actionable technical diagnostics.

    Formula families are cross-checked against the isolated MIT ``ta``
    project. Values are calculated locally so the website runtime does not
    import third-party source trees.
    """

    if len(candles) < 35:
        return {
            "status": "insufficient_history",
            "research_status": "unverified",
            "lenses": [],
        }
    values = _technical_values(candles)
    ema5 = values["ema5"]
    ema20 = values["ema20"]
    macd_histogram = values["macd_histogram"]
    rsi14 = values["rsi14"]
    middle = values["bollinger_middle"]
    upper = values["bollinger_upper"]
    lower = values["bollinger_lower"]
    band_position = values["bollinger_position"]
    atr_pct = values["atr14_pct"]
    volume_ratio = values["volume_ratio20"]
    intraday_volume_ratio = values["intraday_volume_ratio5"]

    lenses = [
        {
            "id": "ema_trend",
            "name": "短趋势",
            "value": f"EMA5 {ema5:.3f} / EMA20 {ema20:.3f}",
            "state": "up" if ema5 > ema20 else "down",
            "interpretation": (
                "短均线位于长均线上方"
                if ema5 > ema20 else "短均线未站上长均线"
            ),
        },
        {
            "id": "macd_momentum",
            "name": "MACD动量",
            "value": f"柱值 {macd_histogram:+.4f}",
            "state": "positive" if macd_histogram > 0 else "negative",
            "interpretation": (
                "当前动量为正" if macd_histogram > 0 else "当前动量为负"
            ),
        },
        {
            "id": "rsi14",
            "name": "RSI14",
            "value": f"{rsi14:.2f}",
            "state": (
                "high" if rsi14 >= 70 else "low" if rsi14 <= 30 else "neutral"
            ),
            "interpretation": (
                "处于高位区，不等于立即卖出"
                if rsi14 >= 70 else
                "处于低位区，不等于立即买入"
                if rsi14 <= 30 else "处于中性区"
            ),
        },
        {
            "id": "bollinger_position",
            "name": "布林带位置",
            "value": f"{band_position * 100:.1f}%",
            "state": (
                "above" if band_position > 1 else
                "below" if band_position < 0 else "inside"
            ),
            "interpretation": (
                f"下轨 {lower:.3f} / 中轨 {middle:.3f} / 上轨 {upper:.3f}"
            ),
        },
        {
            "id": "atr14",
            "name": "ATR14波动",
            "value": f"{atr_pct:.2f}%",
            "state": "observed",
            "interpretation": "最近14根K线真实波幅占最新价比例",
        },
        {
            "id": "volume_ratio20",
            "name": "K线相对量20",
            "value": f"{volume_ratio:.2f}",
            "state": "expanded" if volume_ratio >= 1.2 else "normal",
            "interpretation": (
                "最新K线相对前20根放量"
                if volume_ratio >= 1.2 else "最新K线未显著放量"
            ),
        },
        {
            "id": "intraday_volume_ratio5",
            "name": "盘中量比（5日）",
            "value": (
                f"{intraday_volume_ratio:.2f}"
                if intraday_volume_ratio is not None else "不可计算"
            ),
            "state": (
                "expanded"
                if intraday_volume_ratio is not None and intraday_volume_ratio >= 1.2
                else "normal" if intraday_volume_ratio is not None else "unavailable"
            ),
            "interpretation": (
                "当日累计成交量/已开市分钟，除以前5个交易日平均每分钟成交量；"
                "当前为按K线粒度计算的等价值"
                if intraday_volume_ratio is not None else
                "分钟K线覆盖不足当前交易日加前5个完整交易日，不能计算真实盘中量比"
            ),
        },
    ]
    return {
        "status": "calculated",
        "research_status": "unverified",
        "information_time": candles[-1].time,
        "source_boundary": (
            "本地公式；指标族参考隔离的MIT ta项目，未导入第三方运行时"
        ),
        "lenses": lenses,
        "message": (
            "这些指标用于解释当前K线状态，不是独立买卖条件；"
            "新增或组合前必须在真实A股数据上预注册并回测。"
        ),
    }


def technical_values(candles: list[Candle]) -> dict[str, float | None]:
    """Expose locally calculated indicator values for explainable research UI."""

    if len(candles) < 35:
        raise ValueError("至少需要35根K线才能计算技术状态")
    return _technical_values(candles)


def _technical_values(candles: list[Candle]) -> dict[str, float | None]:
    """Calculate values using the same definitions as the isolated MIT ta repo."""

    close = pd.Series([candle.close for candle in candles], dtype=float)
    high = pd.Series([candle.high for candle in candles], dtype=float)
    low = pd.Series([candle.low for candle in candles], dtype=float)
    volume = pd.Series([candle.volume for candle in candles], dtype=float)

    ema5_series = close.ewm(
        span=5, min_periods=5, adjust=False
    ).mean()
    ema20_series = close.ewm(
        span=20, min_periods=20, adjust=False
    ).mean()
    ema12 = close.ewm(
        span=12, min_periods=12, adjust=False
    ).mean()
    ema26 = close.ewm(
        span=26, min_periods=26, adjust=False
    ).mean()
    macd = ema12 - ema26
    macd_signal = macd.ewm(
        span=9, min_periods=9, adjust=False
    ).mean()

    difference = close.diff(1)
    upward = difference.where(difference > 0, 0.0)
    downward = -difference.where(difference < 0, 0.0)
    average_up = upward.ewm(
        alpha=1 / 14, min_periods=14, adjust=False
    ).mean()
    average_down = downward.ewm(
        alpha=1 / 14, min_periods=14, adjust=False
    ).mean()
    relative_strength = average_up / average_down
    rsi = 100 - (100 / (1 + relative_strength))
    rsi = rsi.where(average_down != 0, 100.0)

    bollinger_middle = close.rolling(20, min_periods=20).mean()
    bollinger_deviation = close.rolling(
        20, min_periods=20
    ).std(ddof=0)
    bollinger_upper = bollinger_middle + 2 * bollinger_deviation
    bollinger_lower = bollinger_middle - 2 * bollinger_deviation
    bollinger_width = bollinger_upper - bollinger_lower
    bollinger_position = (
        (close - bollinger_lower) / bollinger_width.where(
            bollinger_width != 0
        )
    ).fillna(0.5)

    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = [0.0] * len(close)
    atr[13] = float(true_range.iloc[:14].mean())
    for index in range(14, len(atr)):
        atr[index] = (
            atr[index - 1] * 13 + float(true_range.iloc[index])
        ) / 14

    previous_volume = float(volume.iloc[-21:-1].mean())
    volume_ratio = (
        float(volume.iloc[-1]) / previous_volume
        if previous_volume > 0 else 0.0
    )
    intraday_volume_ratio = _intraday_volume_ratio_5d(candles)
    latest_close = float(close.iloc[-1])
    return {
        "ema5": float(ema5_series.iloc[-1]),
        "ema20": float(ema20_series.iloc[-1]),
        "macd_histogram": float(
            (macd - macd_signal).iloc[-1]
        ),
        "rsi14": float(rsi.iloc[-1]),
        "bollinger_middle": float(bollinger_middle.iloc[-1]),
        "bollinger_upper": float(bollinger_upper.iloc[-1]),
        "bollinger_lower": float(bollinger_lower.iloc[-1]),
        "bollinger_position": float(bollinger_position.iloc[-1]),
        "atr14": float(atr[-1]),
        "atr14_pct": (
            float(atr[-1]) / latest_close * 100
            if latest_close else 0.0
        ),
        "volume_ratio20": volume_ratio,
        "intraday_volume_ratio5": intraday_volume_ratio,
    }


def _intraday_volume_ratio_5d(candles: list[Candle]) -> float | None:
    """Calculate the Chinese intraday volume ratio when its inputs exist.

    ``current cumulative volume / elapsed session minutes`` is compared with
    the mean per-minute volume of the five *completed* prior sessions.  The
    provider gives fixed-size bars, so per-bar averages are equivalent to
    per-minute averages for a single requested interval.  A partial history is
    deliberately rejected rather than silently falling back to a daily proxy.
    """

    frame = pd.DataFrame({
        "time": pd.to_datetime(
            [candle.time for candle in candles], errors="coerce"
        ),
        "volume": [candle.volume for candle in candles],
    }).dropna(subset=["time", "volume"])
    frame = frame[frame["volume"] >= 0].copy()
    if frame.empty:
        return None
    frame["session"] = frame["time"].dt.date
    sessions = list(frame["session"].drop_duplicates())
    if len(sessions) < 6:
        return None
    current_session = sessions[-1]
    prior_sessions = sessions[-6:-1]
    current = frame[frame["session"] == current_session]
    prior = frame[frame["session"].isin(prior_sessions)]
    if len(current) < 2:
        return None
    current_per_bar = float(current["volume"].sum()) / len(current)
    prior_per_bar = prior.groupby("session")["volume"].mean()
    average_prior_per_bar = float(prior_per_bar.mean()) if not prior_per_bar.empty else 0.0
    if average_prior_per_bar <= 0:
        return None
    return current_per_bar / average_prior_per_bar


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def _sina_symbol(code: str) -> str:
    normalized = code.strip().lower()
    if normalized.startswith(("sh", "sz")):
        return normalized
    return f"sh{normalized}" if normalized.startswith("6") else f"sz{normalized}"


def _number(value) -> float | None:
    try:
        number = float(value)
        return number if isfinite(number) else None
    except (TypeError, ValueError):
        return None
