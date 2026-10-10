"""Acquire a real BaoStock calendar for a disposable local research checkout.

Raw responses stay in ignored reports/. This is a source receipt, not a
provider-signed attestation or evidence that sector/price feeds are healthy.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import socket


SCHEMA = "local_calendar_acquisition.v1"
ROOT = Path(__file__).resolve().parents[1]
SOURCE = "BaoStock query_trade_dates"
FIELDS = ["calendar_date", "is_trading_day"]


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("calendar_day_invalid")
    return date.fromisoformat(value)


def _stamp(clock):
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("calendar_clock_invalid")
    return value.astimezone(timezone.utc)


def _create(path, raw):
    if path.is_symlink():
        raise ValueError("calendar_output_symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw)


def capture(*, root=ROOT, start_day, end_day, client=None,
            clock=lambda: datetime.now(timezone.utc), socket_timeout=12):
    """First-write runtime calendar, refusing incomplete or pre-existing data."""
    root = Path(root)
    first, last = _day(start_day), _day(end_day)
    if first > last or (last - first).days >= 512:
        raise ValueError("calendar_range_invalid")
    if type(socket_timeout) not in (int, float) or not 1 <= socket_timeout <= 30:
        raise ValueError("calendar_timeout_invalid")
    days = [(first + timedelta(days=i)).isoformat()
            for i in range((last - first).days + 1)]
    pointer = root / "reports/current_market_calendar.json"
    if pointer.exists() or pointer.is_symlink():
        raise FileExistsError("runtime_calendar_exists")
    if client is None:
        import baostock as client
    started = _stamp(clock)
    previous_timeout = socket.getdefaulttimeout()
    logged_in = False
    try:
        socket.setdefaulttimeout(socket_timeout)
        login = client.login()
        logged_in = getattr(login, "error_code", None) == "0"
        if not logged_in:
            raise ValueError("calendar_login_failed")
        result = client.query_trade_dates(start_date=start_day, end_date=end_day)
        if getattr(result, "error_code", None) != "0" or list(getattr(result, "fields", [])) != FIELDS:
            raise ValueError("calendar_source_response_invalid")
        rows = []
        while result.next():
            rows.append(result.get_row_data())
            if len(rows) > len(days):
                raise ValueError("calendar_extra_rows")
        if getattr(result, "error_code", None) != "0":
            raise ValueError("calendar_source_response_invalid")
        page_rows = len(result.data)
        cursor = result.cur_row_num
        page_number = str(result.cur_page_num)
    finally:
        if logged_in:
            try:
                client.logout()
            finally:
                socket.setdefaulttimeout(previous_timeout)
        else:
            socket.setdefaulttimeout(previous_timeout)
    completed = _stamp(clock)
    if completed < started:
        raise ValueError("calendar_clock_reversed")
    if (len(rows) != len(days) or any(not isinstance(row, list) or len(row) != 2
                                     or row[1] not in {"0", "1"} for row in rows)
            or [row[0] for row in rows] != days or page_rows != len(days)
            or cursor != len(days) or page_number != "1"):
        raise ValueError("calendar_rows_incomplete")
    receipt = {"schema": SCHEMA,
               "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "provider": "BaoStock", "endpoint": "query_trade_dates",
               "start_date": start_day, "end_date": end_day,
               "started_at": started.isoformat(), "completed_at": completed.isoformat(),
               "fields": FIELDS, "rows": rows, "error_code": "0",
               "page_rows": page_rows, "cursor": cursor, "page_number": page_number,
               "deadline_expired": False,
               "transport_limit": f"socket_default_timeout_{socket_timeout}s"}
    receipt_raw = _bytes(receipt)
    receipt_sha = hashlib.sha256(receipt_raw).hexdigest()
    relative = Path("reports/cloud_daily_epochs/calendars") / (
        f"local_{start_day}_{end_day}.{receipt_sha}.json")
    receipt_path = root / relative
    if receipt_path.exists():
        if receipt_path.is_symlink() or receipt_path.read_bytes() != receipt_raw:
            raise ValueError("calendar_receipt_collision")
    else:
        _create(receipt_path, receipt_raw)
    calendar = {"version": 1, "generated_at": completed.isoformat(),
                "status": "current", "source": SOURCE,
                "coverage_start": start_day, "coverage_end": end_day,
                "trading_dates": [row[0] for row in rows if row[1] == "1"],
                "source_receipt": {"path": relative.as_posix(), "sha256": receipt_sha}}
    _create(pointer, _bytes(calendar))
    return {"status": "captured", "calendar_sha256": hashlib.sha256(pointer.read_bytes()).hexdigest(),
            "receipt_sha256": receipt_sha, "coverage_start": start_day,
            "coverage_end": end_day, "calendar_days": len(rows),
            "trading_days": len(calendar["trading_dates"]),
            "generated_at": calendar["generated_at"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--start", default="2025-08-05")
    parser.add_argument("--end", default="2026-10-31")
    args = parser.parse_args(argv)
    print(json.dumps(capture(root=args.root, start_day=args.start,
                             end_day=args.end), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
