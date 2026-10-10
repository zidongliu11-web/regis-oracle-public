"""Bounded BaoStock source probe. Validates source geometry, not tradability."""

from __future__ import annotations

import contextlib
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import socket
from typing import Any


DAY_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,tradestatus"
MINUTE_FIELDS = "date,time,code,open,high,low,close,volume,amount,adjustflag"
PAIR = (("sz.000001", "stock"), ("sh.510300", "stock_etf"))


def expected_times(day: str) -> list[str]:
    """BaoStock timestamps are bar END times in the verified A-share sessions."""
    prefix = day.replace("-", "")
    morning = [h * 60 + m for h in (9, 10, 11) for m in range(0, 60, 5)
               if 9 * 60 + 35 <= h * 60 + m <= 11 * 60 + 30]
    afternoon = [h * 60 + m for h in (13, 14, 15) for m in range(0, 60, 5)
                 if 13 * 60 + 5 <= h * 60 + m <= 15 * 60]
    return [f"{prefix}{n // 60:02d}{n % 60:02d}00000" for n in morning + afternoon]


def _decimal(row: dict[str, str], field: str) -> Decimal:
    try:
        value = Decimal(str(row[field]))
    except (KeyError, InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid {field}") from exc
    if not value.is_finite():
        raise ValueError(f"nonfinite {field}")
    return value


def _bar_errors(row: dict[str, str], label: str) -> list[str]:
    try:
        op, hi, lo, cl = (_decimal(row, k) for k in ("open", "high", "low", "close"))
        vol, amt = (_decimal(row, k) for k in ("volume", "amount"))
        errors = []
        if lo <= 0 or lo > min(op, cl) or hi < max(op, cl):
            errors.append(f"{label}:invalid_ohlc")
        if vol < 0 or amt < 0:
            errors.append(f"{label}:negative_volume_or_amount")
        return errors
    except ValueError as exc:
        return [f"{label}:{exc}"]


def inspect_pair(day: str, code: str, kind: str,
                 daily: list[dict[str, str]], minute: list[dict[str, str]]) -> dict[str, Any]:
    """Pure source checks. A pass never certifies PIT availability, fills, or rights."""
    errors: list[str] = []
    try:
        date.fromisoformat(day)
    except ValueError:
        errors.append("invalid_day")
    if (code, kind) not in PAIR:
        errors.append("unexpected_instrument")
    if len(daily) != 1:
        errors.append(f"daily_count:{len(daily)}")
    expected = expected_times(day)
    if len(minute) != len(expected):
        errors.append(f"minute_count:{len(minute)}")
    for i, row in enumerate(daily):
        label = f"daily[{i}]"
        if row.get("date") != day or row.get("code") != code:
            errors.append(f"{label}:identity")
        if row.get("adjustflag") != "3":
            errors.append(f"{label}:adjustflag")
        if row.get("tradestatus") != "1":
            errors.append(f"{label}:trade_status")
        errors.extend(_bar_errors(row, label))
        try:
            if _decimal(row, "preclose") <= 0:
                errors.append(f"{label}:nonpositive_preclose")
        except ValueError as exc:
            errors.append(f"{label}:{exc}")
    actual_times = []
    for i, row in enumerate(minute):
        label = f"minute[{i}]"
        actual_times.append(row.get("time"))
        if row.get("date") != day or row.get("code") != code:
            errors.append(f"{label}:identity")
        if row.get("adjustflag") != "3":
            errors.append(f"{label}:adjustflag")
        errors.extend(_bar_errors(row, label))
    if actual_times != expected:
        errors.append("minute_session_grid")
    if len(daily) == 1:
        try:
            if sum((_decimal(row, "volume") for row in minute), Decimal(0)) != _decimal(daily[0], "volume"):
                errors.append("volume_sum_mismatch")
            if sum((_decimal(row, "amount") for row in minute), Decimal(0)) != _decimal(daily[0], "amount"):
                errors.append("amount_sum_mismatch")
            if minute and _decimal(minute[-1], "close") != _decimal(daily[0], "close"):
                errors.append("last_close_mismatch")
        except ValueError as exc:
            errors.append(str(exc))
    cross_resolution_ohlc = None
    ohlc_reconciled = False
    if len(daily) == 1 and minute:
        try:
            open_daily = _decimal(daily[0], "open")
            high_daily = _decimal(daily[0], "high")
            low_daily = _decimal(daily[0], "low")
            close_daily = _decimal(daily[0], "close")
            open_minute = _decimal(minute[0], "open")
            high_minute = max(_decimal(row, "high") for row in minute)
            low_minute = min(_decimal(row, "low") for row in minute)
            close_minute = _decimal(minute[-1], "close")
            cross_resolution_ohlc = {
                "daily": {"open": str(open_daily), "high": str(high_daily),
                          "low": str(low_daily), "close": str(close_daily)},
                "minute": {"open": str(open_minute), "high": str(high_minute),
                           "low": str(low_minute), "close": str(close_minute)},
            }
            ohlc_reconciled = (open_daily, high_daily, low_daily, close_daily) == (
                open_minute, high_minute, low_minute, close_minute)
        except ValueError:
            pass
    return {
        "code": code, "kind": kind, "day": day, "source_geometry_pass": not errors,
        "errors": errors, "daily_rows": len(daily), "minute_rows": len(minute),
        "expected_bar_end_times": len(expected), "bar_end_time_semantics": "provider_label_only",
        "cross_resolution_ohlc": cross_resolution_ohlc,
        "cross_resolution_ohlc_reconciled": ohlc_reconciled,
        "intraday_price_path_admitted": False,
        "first_available_at": None, "historical_point_in_time_verified": False,
        "market_data_retention_display_rights_verified": False,
        "minute_fill_verified": False, "full_a_coverage_verified": False,
        "target_a_economics_verified": False, "production_admitted": False,
    }


def _rows(result: Any, expected_fields: str, limit: int) -> list[dict[str, str]]:
    if str(getattr(result, "error_code", "")) != "0":
        raise RuntimeError(f"BaoStock K error: {getattr(result, 'error_code', None)}")
    fields = getattr(result, "fields", None)
    if not isinstance(fields, (list, tuple)) or list(fields) != expected_fields.split(","):
        raise RuntimeError(f"BaoStock field mismatch: {fields}")
    rows = []
    while result.next():
        if len(rows) >= limit:
            raise RuntimeError("BaoStock response exceeds bounded row cap")
        values = result.get_row_data()
        if len(values) != len(fields):
            raise RuntimeError("BaoStock row width mismatch")
        rows.append(dict(zip(fields, values)))
    if str(getattr(result, "error_code", "")) != "0":
        raise RuntimeError(f"BaoStock K iteration error: {getattr(result, 'error_code', None)}")
    return rows


def capture(day: str, root: Path, client: Any = None, *, socket_timeout: int = 12) -> dict[str, Any]:
    """Fetch precisely four K series, persist raw locally and bounded metadata."""
    observed = date.fromisoformat(day)
    if observed >= datetime.now(timezone.utc).date():
        raise ValueError("probe day must be completed before current UTC day")
    if client is None:
        import baostock as client  # Optional SDK; never imported by the website.
    root = Path(root).resolve()
    old_timeout = socket.getdefaulttimeout()
    started = datetime.now(timezone.utc).isoformat()
    payload: dict[str, Any] = {"source": "BaoStock", "day": day, "retrieved_at_utc": started, "pairs": []}
    try:
        socket.setdefaulttimeout(socket_timeout)
        with contextlib.redirect_stdout(io.StringIO()):
            login = client.login()
        if str(getattr(login, "error_code", "")) != "0":
            raise RuntimeError(f"BaoStock login failed: {getattr(login, 'error_code', None)}")
        try:
            for code, kind in PAIR:
                per: dict[str, Any] = {"code": code, "kind": kind}
                for label, fields, frequency, cap in (
                    ("daily", DAY_FIELDS, "d", 2), ("minute", MINUTE_FIELDS, "5", 49)
                ):
                    result = client.query_history_k_data_plus(
                        code, fields, start_date=day, end_date=day,
                        frequency=frequency, adjustflag="3"
                    )
                    per[label] = _rows(result, fields, cap)
                payload["pairs"].append(per)
        finally:
            with contextlib.suppress(Exception), contextlib.redirect_stdout(io.StringIO()):
                client.logout()
    finally:
        socket.setdefaulttimeout(old_timeout)
    summaries = [inspect_pair(day, p["code"], p["kind"], p["daily"], p["minute"])
                 for p in payload["pairs"]]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    raw_sha = hashlib.sha256(raw).hexdigest()
    raw_dir = root / "reports" / "local_closure" / "baostock_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{day}_{raw_sha}.json"
    if raw_path.exists():
        if raw_path.read_bytes() != raw:
            raise RuntimeError("raw hash-path collision")
    else:
        raw_path.write_bytes(raw)
    report = {
        "schema": 1, "source": "BaoStock", "probe_day": day,
        "retrieved_at_utc": started, "sdk_version": getattr(client, "__version__", None),
        "scope": "one_stock_one_stock_etf_one_completed_day_four_K_requests",
        "raw_local_path": raw_path.relative_to(root).as_posix(), "raw_sha256": raw_sha,
        "source_file_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "pairs": summaries, "source_geometry_pass": all(s["source_geometry_pass"] for s in summaries),
        "cross_resolution_ohlc_reconciled": all(s["cross_resolution_ohlc_reconciled"] for s in summaries),
        "intraday_price_path_admitted": False,
        "historical_point_in_time_verified": False,
        "market_data_retention_display_rights_verified": False,
        "full_a_coverage_verified": False, "minute_fill_verified": False,
        "target_a_economics_verified": False, "production_admitted": False,
    }
    report_path = root / "reports" / "local_closure" / "baostock_source_probe_20260929.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
