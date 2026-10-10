"""Publish source-bound decision indexes and verified closed-market calendar days.

This job repairs the missing cloud decision inputs. It never infers a trading
session from a weekday and never promotes research prices into a trade signal.
"""
from __future__ import annotations

import csv
from datetime import date, datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from .cloud_daily_epochs import _calendar as source_calendar
from .cloud_pattern_inputs import provider_deadline, SDK_PAGE_SIZE
from .cloud_research_worker import ROOT, read_json
from .market_state_daily import _atomic_publish, _canonical_bytes


SCHEMA = "cloud_decision_inputs.v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")
INDEXES = {
    "sh000001": "sh.000001",  # 上证指数
    "sh000300": "sh.000300",  # 沪深300
    "sh000905": "sh.000905",  # 中证500
    "sh000852": "sh.000852",  # 中证1000
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _replace(path: Path, data: bytes) -> None:
    """Replace a current pointer only after its immutable source is stored."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def refresh_closed_calendar(root: Path, *, target: str, client, now: datetime) -> dict:
    path = root / "reports/current_market_calendar.json"
    current_bytes = path.read_bytes()
    current = json.loads(current_bytes)
    base = current.get("coverage_end")
    if (current.get("source") != "BaoStock query_trade_dates"
            or not isinstance(base, str) or date.fromisoformat(base).isoformat() != base
            or not isinstance(current.get("trading_dates"), list)
            or current["trading_dates"] != sorted(set(current["trading_dates"]))
            or not current["trading_dates"] or current["trading_dates"][-1] > base):
        raise ValueError("decision_calendar_basis_invalid")
    today = now.astimezone(SHANGHAI).date().isoformat()
    if today <= base:
        return {"status": "unchanged", "coverage_end": base}
    if (date.fromisoformat(today) - date.fromisoformat(base)).days > 31:
        raise ValueError("decision_calendar_gap_requires_review")
    # The existing source helper retains a content-bound raw provider receipt.
    observed, receipt = source_calendar(root, base, today, client, lambda: now)
    if (base in current["trading_dates"]) != (base in observed):
        raise ValueError("decision_calendar_overlap_mismatch")
    observed_set = set(observed)
    covered = base
    additions = []
    for ordinal in range(date.fromisoformat(base).toordinal() + 1,
                         date.fromisoformat(today).toordinal() + 1):
        day = date.fromordinal(ordinal).isoformat()
        # A newly open session is published only by the full daily-price path.
        if day in observed_set and day > target:
            break
        covered = day
        if day in observed_set:
            additions.append(day)
    if covered == base:
        return {"status": "waiting_daily_prices", "coverage_end": base}
    archive = root / "reports/cloud_decision_inputs/calendar_versions" / (_sha(current_bytes) + ".json")
    _atomic_publish(archive, current_bytes)
    if path.read_bytes() != current_bytes:
        raise ValueError("decision_calendar_changed_during_refresh")
    updated = {**current, "generated_at": now.isoformat(), "coverage_end": covered,
               "trading_dates": current["trading_dates"] + additions,
               "source_receipt": receipt, "price_basis_date": target}
    _replace(path, _canonical_bytes(updated))
    return {"status": "extended", "coverage_end": covered,
            "added_trading_days": len(additions), "source_receipt": receipt}


def _query_index(client, code: str, dates: list[str], now: datetime) -> dict:
    request = {"code": code, "fields": "date,code,close", "start_date": dates[0],
               "end_date": dates[-1], "frequency": "d"}
    with provider_deadline() as guard:
        result = client.query_history_k_data_plus(**request)
        rows = []
        while not guard["expired"] and result.error_code == "0" and result.next():
            rows.append(result.get_row_data())
            if len(rows) > len(dates):
                raise ValueError("decision_index_extra_rows")
    if (guard["expired"] or result.error_code != "0"
            or list(result.fields or []) != ["date", "code", "close"]
            or len(rows) != len(dates) or [row[0] for row in rows] != dates
            or any(len(row) != 3 or row[1] != code for row in rows)
            or type(result.cur_row_num) is not int or result.cur_row_num != len(rows)
            or len(result.data) != len(rows) or len(rows) >= SDK_PAGE_SIZE):
        raise ValueError("decision_index_source_incomplete")
    for row in rows:
        try:
            close = float(row[2])
        except (TypeError, ValueError):
            raise ValueError("decision_index_close_invalid") from None
        if not math.isfinite(close) or close <= 0:
            raise ValueError("decision_index_close_invalid")
    return {"schema": SCHEMA, "provider": "BaoStock", "endpoint": "query_history_k_data_plus",
            "request": request, "observed_at": now.isoformat(), "fields": list(result.fields),
            "rows": rows, "error_code": str(result.error_code),
            "pagination": {"page_rows": len(result.data), "cursor": result.cur_row_num,
                           "page_number": str(result.cur_page_num)},
            "use": "decision_index_source_only_not_strategy_admission"}


def _indexes_current(root: Path, target: str) -> bool:
    previous = read_json(root / "reports/current_market_index_update.json")
    if previous.get("schema") != SCHEMA or previous.get("data_as_of") != target:
        return False
    rows = previous.get("decision_indexes") or {}
    if set(rows) != set(INDEXES):
        return False
    for name in INDEXES:
        path = root / "research/data/current" / (name + ".csv")
        if not path.is_file() or rows[name].get("sha256") != _sha(path.read_bytes()):
            return False
    return True


def refresh_indexes(root: Path, *, target: str, client, now: datetime) -> dict:
    calendar = read_json(root / "reports/current_market_calendar.json")
    dates = [day for day in calendar.get("trading_dates", []) if day <= target]
    if len(dates) < 80 or dates[-1] != target:
        raise ValueError("decision_index_calendar_insufficient")
    dates = dates[-160:]
    pointer = root / "reports/current_market_index_update.json"
    previous = read_json(pointer)
    current_files = {name: root / "research/data/current" / (name + ".csv") for name in INDEXES}
    if _indexes_current(root, target):
        return {"status": "unchanged", "data_as_of": target, "count": len(INDEXES)}
    if any(path.is_file() for path in current_files.values()) and previous.get("schema") != SCHEMA:
        raise ValueError("decision_index_existing_files_unowned")
    reports = root / "reports/cloud_decision_inputs/index_sources"
    epochs = root / "research/data/current/epochs"
    outputs = {}
    prepared = {}
    for name, code in INDEXES.items():
        source = _query_index(client, code, dates, now)
        raw = _canonical_bytes(source)
        raw_path = reports / (_sha(raw) + ".json")
        _atomic_publish(raw_path, raw)
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("date", "close"))
        writer.writerows((row[0], row[2]) for row in source["rows"])
        data = stream.getvalue().encode("utf-8")
        archive = epochs / (_sha(data) + ".csv")
        _atomic_publish(archive, data)
        prepared[name] = data
        outputs[name] = {"code": code, "output": current_files[name].relative_to(root).as_posix(),
                         "sha256": _sha(data), "rows": len(dates),
                         "latest_available_date": target,
                         "source_current_for_requested_end": True,
                         "source_receipt": {"path": raw_path.relative_to(root).as_posix(), "sha256": _sha(raw)},
                         "archive": {"path": archive.relative_to(root).as_posix(), "sha256": _sha(data)}}
    for name, data in prepared.items():
        _replace(current_files[name], data)
    receipt = {"schema": SCHEMA, "generated_at": now.isoformat(),
               "source": "BaoStock query_history_k_data_plus",
               "data_as_of": target, "decision_indexes": outputs,
               "source_only": True, "strategy_admitted": False}
    _replace(pointer, _canonical_bytes(receipt))
    return {"status": "complete", "data_as_of": target, "count": len(outputs)}


def run(*, root: Path = ROOT, client=None, now: datetime | None = None) -> dict:
    if os.environ.get("REGIS_EXECUTION_ROLE") != "cloud":
        return {"status": "local_unchanged"}
    now = now or datetime.now(SHANGHAI)
    daily = read_json(root / "reports/cloud_market_data.json")
    target = daily.get("target_date")
    if (daily.get("audited") is not True or not isinstance(target, str)
            or date.fromisoformat(target).isoformat() != target):
        return {"status": "waiting_daily_audit", "summary": "云端日线尚无已审计截止日。"}
    if client is None:
        import baostock as client
    calendar = refresh_closed_calendar(root, target=target, client=client, now=now)
    if _indexes_current(root, target):
        return {"status": "complete", "data_as_of": target,
                "calendar_coverage_end": calendar["coverage_end"], "index_count": len(INDEXES),
                "summary": f"交易日历核至{calendar['coverage_end']}；四项指数日线核至{target}。策略与分钟行情另行验证。"}
    logged_in = False
    try:
        with provider_deadline() as guard:
            logged_in = client.login().error_code == "0" and not guard["expired"]
        if not logged_in:
            raise ValueError("decision_index_provider_login_failed")
        indexes = refresh_indexes(root, target=target, client=client, now=now)
    finally:
        if logged_in:
            with provider_deadline():
                client.logout()
    return {"status": "complete", "data_as_of": target,
            "calendar_coverage_end": calendar["coverage_end"],
            "index_count": indexes["count"],
            "summary": f"交易日历核至{calendar['coverage_end']}；四项指数收盘日线核至{target}。策略准入与分钟行情另行验证。"}
