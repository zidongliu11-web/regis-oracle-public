"""Shared A-share calendar and causal market-regime facts.

The local audited index files are the source of truth. Dates beyond their
coverage fail closed instead of guessing that every weekday is tradable.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import functools
import json
import hashlib
import math
import os
from pathlib import Path
import time as time_module
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .data_semantics import MAX_TOTAL_EXPOSURE_PCT


_SH_TZ = ZoneInfo("Asia/Shanghai")

_INDEX_DIR = Path(__file__).resolve().parents[1] / "research" / "data" / "current"
_INDEX_FILES = {
    "沪深300": _INDEX_DIR / "sh000300.csv",
    "上证指数": _INDEX_DIR / "sh000001.csv",
}
_INDEX_CODES = {"沪深300": "sh000300", "上证指数": "sh000001"}
_CALENDAR_PATH = Path(__file__).resolve().parents[1] / "reports" / "current_market_calendar.json"


def _calendar() -> dict[str, Any]:
    try:
        value = json.loads(_CALENDAR_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


@functools.lru_cache(maxsize=8)
def _runtime_public_index_closes(
    code: str, five_minute_bucket: int,
) -> tuple[tuple[str, float], ...]:
    del five_minute_bucket
    from trade_assistant.candles import TencentCandleProvider

    candles = TencentCandleProvider().get_candles(code, 240, interval="1d")
    now = datetime.now(_SH_TZ)
    cutoff = now.date() if now.time() >= time(16, 0) else (now - timedelta(days=1)).date()
    return tuple(
        (str(candle.time)[:10], float(candle.close))
        for candle in candles
        if str(candle.time)[:10] <= cutoff.isoformat()
        and math.isfinite(float(candle.close)) and float(candle.close) > 0
    )


def _index_closes() -> dict[str, pd.Series]:
    series: dict[str, pd.Series] = {}
    for name, path in _INDEX_FILES.items():
        try:
            frame = pd.read_csv(path, usecols=["date", "close"])
            frame["date"] = pd.to_datetime(frame["date"])
            series[name] = frame.dropna().drop_duplicates("date").set_index("date")["close"].astype(float).sort_index()
        except (OSError, KeyError, ValueError):
            continue
    if os.environ.get("REGIS_PUBLIC_DAILY_FALLBACK") == "1":
        for name, code in _INDEX_CODES.items():
            if name in series and len(series[name]) >= 60:
                continue
            try:
                rows = _runtime_public_index_closes(
                    code, int(time_module.time() // 300),
                )
                frame = pd.DataFrame(rows, columns=["date", "close"])
                frame["date"] = pd.to_datetime(frame["date"])
                public = frame.dropna().drop_duplicates("date").set_index("date")["close"].astype(float).sort_index()
                if len(public) >= 60:
                    series[name] = public
            except Exception:
                continue
    return series


def index_data_evidence() -> dict[str, Any]:
    """Expose the exact decision-index files and coverage used by the site."""
    closes = _index_closes()
    files = {}
    for name, path in _INDEX_FILES.items():
        series = closes.get(name)
        runtime_fallback = not path.is_file() and series is not None and len(series) > 0
        files[name] = {
            "path": str(path),
            "exists": path.is_file(),
            "runtime_fallback": runtime_fallback,
            "source": (
                "本机审计指数日线" if path.is_file()
                else "腾讯公开指数日线（云端运行时校验）" if runtime_fallback
                else "unavailable"
            ),
            "rows": int(len(series)) if series is not None else 0,
            "first_date": (
                series.index.min().date().isoformat()
                if series is not None and len(series) else None
            ),
            "last_date": (
                series.index.max().date().isoformat()
                if series is not None and len(series) else None
            ),
            "sha256": (
                hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_file() else None
            ),
        }
    complete = len(closes) == len(_INDEX_FILES) and all(len(value) for value in closes.values())
    return {
        "ready": complete,
        "source": (
            "本机审计指数日线"
            if all(path.is_file() for path in _INDEX_FILES.values())
            else "腾讯公开指数日线（云端运行时校验，非本机审计）"
            if complete else "unavailable"
        ),
        "common_as_of": (
            min(value.index.max().date() for value in closes.values()).isoformat()
            if complete else None
        ),
        "files": files,
    }


def trading_day_status(value: date | datetime | None = None) -> dict[str, Any]:
    day = (value.date() if isinstance(value, datetime) else value) or date.today()
    calendar = _calendar()
    coverage_start = str(calendar.get("coverage_start") or "")
    coverage_end = str(calendar.get("coverage_end") or "")
    trading_dates = set(calendar.get("trading_dates") or [])
    if not coverage_start or not coverage_end or not trading_dates:
        return {
            "is_trading_day": False,
            "date": day.isoformat(),
            "source": "unavailable",
            "data_as_of": None,
            "reason": "交易日历数据不可用，按安全策略静默。",
        }
    if not coverage_start <= day.isoformat() <= coverage_end:
        return {
            "is_trading_day": False,
            "date": day.isoformat(),
            "source": "audited_baostock_calendar",
            "data_as_of": coverage_end,
            "coverage_start": coverage_start,
            "coverage_end": coverage_end,
            "reason": f"交易日历仅覆盖 {coverage_start} 至 {coverage_end}，超出范围时不生成建议。",
        }
    is_open = day.isoformat() in trading_dates
    return {
        "is_trading_day": is_open,
        "date": day.isoformat(),
        "source": "audited_baostock_calendar",
        "data_as_of": coverage_end,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "reason": "交易日" if is_open else "非交易日，不生成买卖建议。",
    }


def next_trading_day_after(value: date | datetime) -> date | None:
    """Return the first audited trading date strictly after ``value``."""

    day = value.date() if isinstance(value, datetime) else value
    calendar = _calendar()
    coverage_end = str(calendar.get("coverage_end") or "")
    if not coverage_end or day.isoformat() >= coverage_end:
        return None
    for raw in sorted(calendar.get("trading_dates") or []):
        if isinstance(raw, str) and raw > day.isoformat():
            try:
                return date.fromisoformat(raw)
            except ValueError:
                return None
    return None


def last_completed_trading_day(value: datetime | None = None) -> date | None:
    """Return the last audited session safe for completed-daily-bar analytics."""
    local_now = value or datetime.now(_SH_TZ)
    local_now = (
        local_now.replace(tzinfo=_SH_TZ)
        if local_now.tzinfo is None else local_now.astimezone(_SH_TZ)
    )
    cutoff = (
        local_now.date()
        if local_now.timetz().replace(tzinfo=None) >= time(16, 0)
        else local_now.date() - timedelta(days=1)
    )
    sessions = sorted(
        date.fromisoformat(raw)
        for raw in _calendar().get("trading_dates") or []
        if isinstance(raw, str)
    )
    eligible = [session for session in sessions if session <= cutoff]
    return eligible[-1] if eligible else None


def audited_trading_day_age(
    as_of: date | datetime | str,
    now: datetime | None = None,
    *,
    allow_current_session: bool = False,
) -> dict[str, Any]:
    """Count completed audited sessions after ``as_of``; unknown never means fresh."""
    try:
        source_day = (
            as_of.date() if isinstance(as_of, datetime)
            else as_of if isinstance(as_of, date)
            else date.fromisoformat(str(as_of)[:10])
        )
    except (TypeError, ValueError):
        return {"status": "unknown", "days": None, "reason": "数据日期无效"}

    local_now = now or datetime.now(_SH_TZ)
    local_now = (
        local_now.replace(tzinfo=_SH_TZ)
        if local_now.tzinfo is None else local_now.astimezone(_SH_TZ)
    )
    cutoff = local_now.date() if local_now.timetz().replace(tzinfo=None) >= time(16, 0) else local_now.date() - timedelta(days=1)
    calendar = _calendar()
    coverage_start = str(calendar.get("coverage_start") or "")
    coverage_end = str(calendar.get("coverage_end") or "")
    sessions = sorted(
        date.fromisoformat(raw)
        for raw in calendar.get("trading_dates") or []
        if isinstance(raw, str)
    )
    if not coverage_start or not coverage_end or not sessions:
        return {"status": "unknown", "days": None, "reason": "交易日历数据不可用"}
    if (
        allow_current_session and source_day == local_now.date()
        and source_day in sessions
    ):
        cutoff = source_day
    if source_day > cutoff:
        return {
            "status": "invalid_future", "days": None,
            "reason": f"数据日期 {source_day.isoformat()} 晚于已完成的交易日 {cutoff.isoformat()}",
            "as_of": source_day.isoformat(), "completed_through": cutoff.isoformat(),
            "coverage_start": coverage_start, "coverage_end": coverage_end,
        }
    if source_day.isoformat() < coverage_start or cutoff.isoformat() > coverage_end:
        return {
            "status": "unknown", "days": None,
            "reason": f"交易日历仅覆盖 {coverage_start} 至 {coverage_end}",
            "coverage_start": coverage_start, "coverage_end": coverage_end,
        }
    days = sum(source_day < session <= cutoff for session in sessions)
    return {
        "status": "fresh" if days == 0 else "stale",
        "days": days,
        "as_of": source_day.isoformat(),
        "completed_through": cutoff.isoformat(),
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
    }


def market_regime(
    value: date | datetime | None = None,
    current_exposure_pct: float | None = None,
) -> dict[str, Any]:
    """Return a causal 100%/50%/0% exposure scale from MA20/MA60 facts."""
    requested = (value.date() if isinstance(value, datetime) else value) or date.today()
    calendar = _calendar()
    coverage_start = str(calendar.get("coverage_start") or "")
    coverage_end = str(calendar.get("coverage_end") or "")
    sessions = sorted(
        date.fromisoformat(value)
        for value in calendar.get("trading_dates") or []
        if isinstance(value, str)
    )
    now = datetime.now(_SH_TZ)
    completed_cutoff = requested
    if requested >= now.date() and now.timetz().replace(tzinfo=None) < time(16, 0):
        completed_cutoff = now.date()
        sessions_before_today = [session for session in sessions if session < now.date()]
        expected_session = sessions_before_today[-1] if sessions_before_today else None
    else:
        eligible_sessions = [session for session in sessions if session <= completed_cutoff]
        expected_session = eligible_sessions[-1] if eligible_sessions else None
    closes = _index_closes()
    details = []
    for name, series in closes.items():
        eligible = series[series.index.date <= completed_cutoff]
        if len(eligible) < 60:
            continue
        close = float(eligible.iloc[-1])
        ma20 = float(eligible.iloc[-20:].mean())
        ma60 = float(eligible.iloc[-60:].mean())
        above20, above60 = close > ma20, close > ma60
        details.append({
            "name": name,
            "as_of": eligible.index[-1].date().isoformat(),
            "close": round(close, 2),
            "ma20": round(ma20, 2),
            "ma60": round(ma60, 2),
            "above_ma20": above20,
            "above_ma60": above60,
            "above_both": above20 and above60,
            "below_both": not above20 and not above60,
        })
    expected_as_of = expected_session.isoformat() if expected_session else None
    calendar_covers_requested = bool(
        coverage_start <= requested.isoformat() <= coverage_end
    )
    fresh = bool(details) and calendar_covers_requested and expected_as_of is not None and all(
        row["as_of"] == expected_as_of for row in details
    )
    complete = len(details) == len(_INDEX_FILES)
    if not complete or not fresh:
        exposure_scale = 0.0
    elif all(row["above_both"] for row in details):
        exposure_scale = 1.0
    elif all(row["below_both"] for row in details):
        exposure_scale = 0.0
    else:
        exposure_scale = 0.5
    regime_target_exposure_pct = int(exposure_scale * 100)
    target_exposure_pct = min(regime_target_exposure_pct, MAX_TOTAL_EXPOSURE_PCT)
    try:
        normalized_exposure = (
            float(current_exposure_pct) if current_exposure_pct is not None else None
        )
        if normalized_exposure is not None and not math.isfinite(normalized_exposure):
            normalized_exposure = None
    except (TypeError, ValueError):
        normalized_exposure = None
    current_exposure_pct = normalized_exposure
    if target_exposure_pct == 0:
        allowed = False
        capacity_status = "complete"
        remaining_exposure_pct = 0.0
    elif current_exposure_pct is None:
        allowed = None
        capacity_status = "unknown"
        remaining_exposure_pct = None
    else:
        current_exposure_pct = max(0.0, current_exposure_pct)
        remaining_exposure_pct = max(0.0, target_exposure_pct - current_exposure_pct)
        allowed = remaining_exposure_pct > 0
        capacity_status = "complete"
    if not complete:
        reason = "指数数据不完整，按安全策略目标暴露0%，暂停新开仓并复核持仓。"
    elif not fresh:
        as_of = min(row["as_of"] for row in details)
        reason = (
            f"指数数据仅覆盖至 {as_of}，应覆盖至 {expected_as_of or '未知'}；"
            "按安全策略目标暴露0%，暂停新开仓并复核持仓。"
        )
    elif exposure_scale == 1:
        reason = (
            "两项指数均站上20日、60日均线，市场状态暴露100%；"
            f"受账户硬上限约束，实际目标上限为{MAX_TOTAL_EXPOSURE_PCT}%。"
        )
    elif exposure_scale == 0:
        reason = "两项指数均跌破20日、60日均线，目标暴露0%，暂停新开仓并复核持仓。"
    else:
        reason = "指数位于20日/60日均线混合区，目标暴露降至50%。"
    if capacity_status == "unknown":
        reason += " 当前组合暴露未知，暂不能判定是否仍可开新仓。"
    local_index_epoch = all(path.is_file() for path in _INDEX_FILES.values())
    return {
        "allow_new_positions": allowed,
        "risk_off": exposure_scale < 1,
        "regime_target_exposure_pct": regime_target_exposure_pct,
        "target_exposure_pct": target_exposure_pct,
        "current_exposure_pct": current_exposure_pct,
        "remaining_exposure_pct": remaining_exposure_pct,
        "capacity_status": capacity_status,
        "exposure_unit": "percent_of_account_equity",
        "date": requested.isoformat(),
        "source": (
            "audited_index_close_ma20_ma60"
            if local_index_epoch else
            "tencent_public_index_close_ma20_ma60_runtime_validated"
        ),
        "evidence_tier": (
            "audited_local" if local_index_epoch else "runtime_validated_public"
        ),
        "details": details,
        "reason": reason,
    }


def backtest_market_allow(dates: list[pd.Timestamp]) -> pd.Series:
    """Causal 100%/50%/0% exposure scale used by the historical engine."""
    index = pd.DatetimeIndex(dates)
    states = []
    for series in _index_closes().values():
        ma20 = series.rolling(20, min_periods=20).mean()
        ma60 = series.rolling(60, min_periods=60).mean()
        above_both = (series > ma20) & (series > ma60)
        below_both = (series < ma20) & (series < ma60)
        state = pd.Series(0.5, index=series.index)
        state.loc[above_both] = 1.0
        state.loc[below_both] = 0.0
        state.loc[ma60.isna()] = 0.0
        states.append(state.reindex(index).ffill().fillna(0.0))
    if len(states) != len(_INDEX_FILES):
        return pd.Series(0.0, index=index)
    both_above = (states[0] == 1.0) & (states[1] == 1.0)
    both_below = (states[0] == 0.0) & (states[1] == 0.0)
    result = pd.Series(0.5, index=index)
    result.loc[both_above] = 1.0
    result.loc[both_below] = 0.0
    return result
