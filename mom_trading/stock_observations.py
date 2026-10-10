"""Public per-stock display observations; independent of scoring and PIT panels."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, time as clock_time, timedelta
import math
import re
import threading
import time
from zoneinfo import ZoneInfo

import requests
from .bounded_runtime import bounded_call

_SH = ZoneInfo('Asia/Shanghai')
_URL = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
_SOURCE = '东方财富公开个股估值（PE_TTM / PB_MRQ）'
_NOTE = '公开日度估值观察；数据日期与抓取时点分别记录，历史PIT及全市场分位未验证，不纳入评分。'
_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict]] = {}
_INFLIGHT: set[str] = set()
# Optional display requests have their own circuit, independent of quotes.
_SLOTS = threading.BoundedSemaphore(2)
_CIRCUIT_LOCK = threading.Lock()
_TIMED_OUT: set[int] = set()
_TELEMETRY: dict = {}


def _unavailable(note: str = '公开估值数据暂不可用。') -> dict:
    return {'status':'unavailable', 'pe_ttm':None, 'pb_mrq':None,
            'as_of':None, 'observed_at':None, 'source':_SOURCE, 'note':note,
            'role':'public_observation_only', 'decision_eligible':False,
            'score_eligible':False, 'historical_pit_verified':False}


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number != 0 else None
    except (ValueError, TypeError):
        return None


def _completed_through(observed: datetime):
    day = observed.date()
    return day if observed.time() >= clock_time(15, 0, 5) else day - timedelta(days=1)


def parse_valuation_rows(code: str, rows: list, *, observed_at: datetime) -> dict:
    """Bind one provider row; never splice periods or infer PIT status."""
    if not re.fullmatch(r'\d{6}', code or '') or observed_at.tzinfo is None:
        return _unavailable('证券代码或观察时钟无法核验。')
    observed = observed_at.astimezone(_SH)
    cutoff = _completed_through(observed)
    candidates = {}
    for row in rows or []:
        if not isinstance(row, dict) or str(row.get('SECURITY_CODE')) != code:
            continue
        try:
            day = datetime.fromisoformat(str(row.get('TRADE_DATE'))).date()
        except (ValueError, TypeError):
            continue
        if day > cutoff:
            continue
        values = (_number(row.get('PE_TTM')), _number(row.get('PB_MRQ')))
        if day in candidates and candidates[day] != values:
            return _unavailable('同日估值记录冲突，暂不展示。')
        candidates[day] = values
    if not candidates:
        return _unavailable()
    day = max(candidates)
    pe, pb = candidates[day]
    if pe is None and pb is None:
        return _unavailable('最近完成日期的估值字段缺失。')
    result = _unavailable(_NOTE)
    result.update(pe_ttm=pe, pb_mrq=pb, as_of=day.isoformat(),
                  observed_at=observed_at.isoformat(timespec='seconds'),
                  status='available' if pe is not None and pb is not None else 'partial')
    if (observed.date() - day).days > 14:
        result.update(status='stale', note='估值记录距观察日期超过14个自然日；保留原始日期，仅展示历史观察，不纳入评分。')
    return result


def _run_bounded(callback, timeout_seconds):
    return bounded_call(callback, timeout_seconds, slots=_SLOTS,
                        lock=_CIRCUIT_LOCK, timed_out=_TIMED_OUT, telemetry=_TELEMETRY)


def clear_cache():
    """Isolated test helper; does not reset provider circuits."""
    with _LOCK:
        _CACHE.clear()
        _INFLIGHT.clear()


def load_public_valuation(code: str, *, observed_at: datetime | None = None) -> dict:
    """Bounded request on a user read, with per-code cache and deduplication."""
    observed = observed_at or datetime.now(_SH)
    if not re.fullmatch(r'\d{6}', code or '') or observed.tzinfo is None:
        return _unavailable('证券代码或观察时钟无法核验。')
    now = time.monotonic()
    with _LOCK:
        cached = _CACHE.get(code)
        if cached:
            fetched, result = cached
            ttl = 300 if result['status'] != 'unavailable' else 30
            data_day = result.get('as_of')
            fetched_at = result.get('observed_at')
            valid_clock = not fetched_at or datetime.fromisoformat(fetched_at) <= observed
            valid_day = not data_day or data_day <= _completed_through(observed.astimezone(_SH)).isoformat()
            if now - fetched < ttl and valid_clock and valid_day:
                return deepcopy(result)
        if code in _INFLIGHT:
            return _unavailable('估值数据正在读取，请稍后刷新。')
        _INFLIGHT.add(code)

    def fetch():
        try:
            params = {'sortColumns':'TRADE_DATE', 'sortTypes':'-1',
                      'pageSize':'10', 'pageNumber':'1',
                      'reportName':'RPT_VALUEANALYSIS_DET', 'columns':'ALL',
                      'quoteColumns':'', 'source':'WEB', 'client':'WEB',
                      'filter':f'(SECURITY_CODE="{code}")'}
            response = requests.get(_URL, params=params, timeout=(3.05, 6))
            response.raise_for_status()
            payload = response.json()
            result = parse_valuation_rows(code, payload['result']['data'], observed_at=observed)
        except Exception:
            result = _unavailable('公开估值来源请求或格式校验失败，请稍后重试。')
        with _LOCK:
            _CACHE[code] = (time.monotonic(), deepcopy(result))
            while len(_CACHE) > 100:
                del _CACHE[next(iter(_CACHE))]
            _INFLIGHT.discard(code)
        return result

    try:
        ok, result, error = _run_bounded(fetch, 4.0)
    except Exception:
        # A scheduler/thread-start failure has no worker to release this claim.
        # Optional display data must not break the rest of the stock response.
        with _LOCK:
            _INFLIGHT.discard(code)
        return _unavailable('估值数据请求暂不可用，请稍后重试。')
    if ok and isinstance(result, dict):
        return result
    if not isinstance(error, TimeoutError):
        with _LOCK:
            _INFLIGHT.discard(code)
    return _unavailable('估值数据请求超时，请稍后重试。' if isinstance(error, TimeoutError)
                        else '估值数据请求暂不可用，请稍后重试。')
