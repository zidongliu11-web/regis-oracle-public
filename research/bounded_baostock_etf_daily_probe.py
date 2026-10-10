"""One-shot, non-persisting ETF presence probe for three frozen historical days.

No market row, price, account value, or token is printed or stored. This probe
does not establish market-data usage rights, full-history coverage or Target A.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import socket
import sys


ROOT = Path(__file__).resolve().parents[1]
DATES = ("2018-01-02", "2021-06-01", "2026-09-28")
TARGET = "sh.510300"
MAX_ROWS_PER_DATE = 5000


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor-root", required=True)
    args = parser.parse_args()
    vendor_root = (ROOT / args.vendor_root).resolve()
    if not vendor_root.is_relative_to(ROOT) or not vendor_root.is_dir():
        raise SystemExit("vendor root missing or outside project")
    sys.path.insert(0, str(vendor_root))
    import baostock as bs  # type: ignore[import-not-found]  # noqa: E402
    from baostock.common import contants as bs_constants  # type: ignore[import-not-found]  # noqa: E402

    package_file = Path(bs.__file__).resolve()
    if not package_file.is_relative_to(vendor_root):
        raise SystemExit("unexpected BaoStock package path")
    sdk_file = package_file.parent / "security" / "history.py"
    resultset_file = package_file.parent / "data" / "resultset.py"
    if not sdk_file.is_file() or not resultset_file.is_file():
        raise SystemExit("BaoStock SDK source missing")
    metadata_file = vendor_root / "baostock-0.9.3.dist-info" / "METADATA"
    if not metadata_file.is_file():
        raise SystemExit("BaoStock package metadata missing")
    version_lines = [line for line in metadata_file.read_text(encoding="utf-8").splitlines() if line.startswith("Version: ")]
    if version_lines != ["Version: 0.9.3"]:
        raise SystemExit("BaoStock SDK version differs from pinned version")
    page_size = bs_constants.BAOSTOCK_PER_PAGE_COUNT
    if type(page_size) is not int or page_size <= 0:
        raise SystemExit("invalid BaoStock page size")
    result = {
        "schema": "bounded_baostock_etf_daily_probe.v1",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "sdk_version": "0.9.3",
        "sdk_metadata_sha256": hashlib.sha256(metadata_file.read_bytes()).hexdigest(),
        "sdk_history_sha256": hashlib.sha256(sdk_file.read_bytes()).hexdigest(),
        "sdk_resultset_sha256": hashlib.sha256(resultset_file.read_bytes()).hexdigest(),
        "page_size": page_size,
        "api": "query_daily_history_k_ETF",
        "target_code": TARGET,
        "frozen_dates": list(DATES),
        "max_rows_per_date": MAX_ROWS_PER_DATE,
        "market_rows_saved": False,
        "data_rights_verified": False,
        "full_history_verified": False,
        "target_a_admitted": False,
        "status": "not_started",
        "days": [],
    }
    socket.setdefaulttimeout(15)
    connected = False
    try:
        with redirect_stdout(io.StringIO()):
            login = bs.login()
        result["login_error_code"] = str(login.error_code)
        if login.error_code != "0":
            result["status"] = "login_rejected"
        else:
            connected = True
            result["status"] = "probing"
            for day in DATES:
                entry = {"date": day, "status": "requested"}
                result["days"].append(entry)
                with redirect_stdout(io.StringIO()):
                    response = bs.query_daily_history_k_ETF(date=day)
                entry["provider_error_code"] = str(response.error_code)
                if response.error_code != "0":
                    entry["status"] = "provider_error"
                    result["status"] = "partial_or_failed"
                    break
                fields = list(response.fields or [])
                entry["fields"] = fields
                if fields.count("date") != 1 or fields.count("code") != 1:
                    entry["status"] = "unexpected_schema"
                    result["status"] = "partial_or_failed"
                    break
                date_idx, code_idx = fields.index("date"), fields.index("code")
                rows = response.data
                if not isinstance(rows, list) or len(rows) >= page_size:
                    entry["status"] = "page_boundary_inconclusive"
                    result["status"] = "partial_or_failed"
                    break
                count = matches = 0
                for row in rows:
                    count += 1
                    if count > MAX_ROWS_PER_DATE or not isinstance(row, list) or len(row) != len(fields) or row[date_idx] != day:
                        entry["status"] = "invalid_or_excess_rows"
                        result["status"] = "partial_or_failed"
                        break
                    matches += row[code_idx] == TARGET
                entry["rows"] = count
                entry["target_matches"] = matches
                entry["duplicate_target"] = matches > 1
                if entry["status"] == "invalid_or_excess_rows":
                    break
                if response.error_code != "0":
                    entry["status"] = "provider_stream_error"
                    result["status"] = "partial_or_failed"
                    break
                entry["status"] = "received"
            else:
                result["status"] = "three_dates_received"
    except Exception as exc:
        result["status"] = "exception"
        result["error_class"] = type(exc).__name__
    finally:
        if connected:
            try:
                with redirect_stdout(io.StringIO()):
                    bs.logout()
            except Exception:
                result["logout_failed"] = True
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["status"] == "three_dates_received" else 1


if __name__ == "__main__":
    raise SystemExit(main())
