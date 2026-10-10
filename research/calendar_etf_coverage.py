"""Audit three BaoStock stock histories against its calendar and an ETF candidate.

Same-provider source fitness only. Missing ETF years block the executable
benchmark; no index or NAV substitution and no trade signal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from research.baostock_bulk_daily_gate import GateError, _source
from research.baostock_history_route_probe import (
    CODES, END_DATE, FIELDS as STOCK_FIELDS, RAW_SCHEMA as STOCK_SCHEMA,
    START_DATE, _history_rows,
)
from research.bounded_baostock_universe_probe import SCHEMA as BASIC_SCHEMA, validate_rows


SCHEMA = "calendar_etf_coverage.v1"
MANIFEST_SCHEMA = "m11_calendar_etf_manifest.v1"
CALENDAR_SCHEMA = "baostock_trade_dates_raw.v1"
ETF_SCHEMA = "baostock_symbol_etf_raw.v1"
ETF_CODE = "sh.510300"
CALENDAR_FIELDS = ("calendar_date", "is_trading_day")
ETF_FIELDS = ("date", "code", "open", "high", "low", "close", "volume", "amount", "adjustflag")
OFFICIAL_SPOT_CHECKS = {"2026-09-25": "0", "2026-09-28": "1"}


def _canonical(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _money(value: str, label: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str) or not value:
        raise GateError(f"{label}: numeric missing")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise GateError(f"{label}: numeric invalid") from None
    if not number.is_finite() or number < 0 or (positive and number == 0):
        raise GateError(f"{label}: numeric nonpositive/nonfinite")
    return number


def _calendar(payload: dict) -> list[str]:
    if (payload.get("start_date") != START_DATE or payload.get("end_date") != END_DATE
            or tuple(payload["fields"]) != CALENDAR_FIELDS):
        raise GateError("calendar: identity or fields mismatch")
    rows = payload["rows"]
    first, last = date.fromisoformat(START_DATE), date.fromisoformat(END_DATE)
    if len(rows) != (last - first).days + 1:
        raise GateError("calendar: natural-day count mismatch")
    open_days = []
    for index, values in enumerate(rows):
        if not isinstance(values, list) or len(values) != 2 or any(not isinstance(x, str) for x in values):
            raise GateError("calendar: malformed row")
        day = first + timedelta(days=index)
        text_day, status = values
        if text_day != day.isoformat() or status not in {"0", "1"}:
            raise GateError("calendar: missing/duplicate date or invalid status")
        if day.weekday() >= 5 and status != "0":
            raise GateError("calendar: weekend marked open")
        if text_day in OFFICIAL_SPOT_CHECKS and status != OFFICIAL_SPOT_CHECKS[text_day]:
            raise GateError("calendar: official 2026 closure spot check mismatch")
        if status == "1":
            open_days.append(text_day)
    return open_days


def _etf_rows(payload: dict) -> dict[str, dict[str, str]]:
    if (payload.get("code") != ETF_CODE or payload.get("start_date") != START_DATE
            or payload.get("end_date") != END_DATE or tuple(payload["fields"]) != ETF_FIELDS):
        raise GateError("etf: identity, range or fields mismatch")
    rows = {}
    last_day = ""
    for values in payload["rows"]:
        if (not isinstance(values, list) or len(values) != len(ETF_FIELDS)
                or any(not isinstance(x, str) for x in values)):
            raise GateError("etf: malformed row")
        row = dict(zip(ETF_FIELDS, values))
        day = row["date"]
        try:
            parsed = date.fromisoformat(day)
        except ValueError:
            raise GateError("etf: invalid date") from None
        if (parsed.isoformat() != day or not START_DATE <= day <= END_DATE
                or day <= last_day or row["code"] != ETF_CODE or row["adjustflag"] != "3"):
            raise GateError("etf: date order/range, code or adjustment invalid")
        last_day = day
        prices = {key: _money(row[key], f"etf.{key}", positive=True) for key in
                  ("open", "high", "low", "close")}
        if (prices["low"] > min(prices["open"], prices["close"])
                or prices["high"] < max(prices["open"], prices["close"])):
            raise GateError("etf: OHLC invalid")
        for key in ("volume", "amount"):
            _money(row[key], f"etf.{key}")
        rows[day] = row
    return rows


def audit(root: Path, manifest: dict) -> dict:
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise GateError("manifest schema invalid")
    if set(manifest.get("histories", {})) != set(CODES):
        raise GateError("manifest three symbols mismatch")
    cal = _source(root, manifest.get("calendar"), CALENDAR_SCHEMA, "calendar")
    etf = _source(root, manifest.get("etf"), ETF_SCHEMA, "etf")
    basic = _source(root, manifest.get("stock_basic"), BASIC_SCHEMA, "stock_basic")
    if basic.get("query") != "stock_basic" or basic.get("query_day") is not None:
        raise GateError("stock_basic query identity mismatch")
    try:
        basic_rows = validate_rows("stock_basic", basic["fields"], basic["rows"])
    except ValueError as exc:
        raise GateError(f"stock_basic invalid: {exc}") from None
    master = {row["code"]: row for row in basic_rows}
    versions = {cal.get("client_version"), etf.get("client_version"), basic.get("client_version")}
    open_days = _calendar(cal)
    open_set = set(open_days)
    stock_reports = []
    for code in CODES:
        source = _source(root, manifest["histories"][code], STOCK_SCHEMA, code)
        versions.add(source.get("client_version"))
        observed = _history_rows(source, code)
        record = master.get(code)
        if not record or record["type"] != "1" or not record["ipoDate"]:
            raise GateError(f"stock_basic: missing type1/IPO {code}")
        ipo = record["ipoDate"]
        expected = {day for day in open_days if day >= ipo}
        actual = set(observed)
        if actual != expected:
            raise GateError(f"stock calendar mismatch {code} missing={len(expected-actual)} extra={len(actual-expected)}")
        stock_reports.append({"code": code, "ipo_date_from_current_basic": ipo,
                              "rows": len(observed), "same_source_open_days": len(expected),
                              "missing_days": 0, "extra_days": 0,
                              "source_sha256": manifest["histories"][code]["sha256"]})
    if len(versions) != 1 or not next(iter(versions)):
        raise GateError("source client version mismatch")
    etf_rows = _etf_rows(etf)
    invalid_days = set(etf_rows) - open_set
    if invalid_days:
        raise GateError(f"etf: rows on closed days count={len(invalid_days)}")
    missing_etf = open_set - set(etf_rows)
    years = {str(year): sum(day.startswith(str(year)) for day in etf_rows)
             for year in range(2018, 2027)}
    return {
        "schema": SCHEMA, "status": "calendar_fit_benchmark_gap" if missing_etf else "same_source_calendar_fit",
        "calendar_source_sha256": manifest["calendar"]["sha256"],
        "stock_basic_source_sha256": manifest["stock_basic"]["sha256"],
        "client_version": next(iter(versions)),
        "natural_days": len(cal["rows"]), "same_source_open_days": len(open_days),
        "official_2026_closure_spot_check": True,
        "stocks": stock_reports, "etf": {
            "candidate_code": ETF_CODE, "official_listed_date": "2012-05-28",
            "source_sha256": manifest["etf"]["sha256"],
            "rows": len(etf_rows), "first": next(iter(etf_rows), None),
            "last": next(reversed(etf_rows), None) if etf_rows else None,
            "rows_by_year": years, "missing_same_source_open_days": len(missing_etf),
            "calendar_complete_for_window": not missing_etf,
            "target_a_historical_benchmark_ready": False,
        },
        "historical_pit_verified": False, "full_a_verified": False,
        "independent_full_calendar_verified": False,
        "source_rights_verified": False, "economic_admitted": False,
        "generates_alert": False, "raw_rows_in_git": False,
    }


def _freeze(root: Path, name: str, payload: dict) -> dict:
    raw = _canonical(payload)
    digest = hashlib.sha256(raw).hexdigest()
    relative = Path("reports/local_closure/m11_calendar_etf_raw") / f"{name}_{digest}.json"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + f".{os.getpid()}.tmp")
    temporary.write_bytes(raw)
    temporary.replace(target)
    return {"path": relative.as_posix(), "sha256": digest}


def fetch(root: Path, m10_manifest: dict, m09_manifest: dict) -> dict:
    import baostock as bs

    if set(m10_manifest.get("histories", {})) != set(CODES):
        raise GateError("M10 histories missing")
    basic = m09_manifest.get("days", {}).get("2018-01-02", {}).get("stock_basic")
    if basic is None:
        raise GateError("M09 basic reference missing")
    root = root.resolve()
    result = bs.login()
    if result.error_code != "0":
        raise GateError(f"provider login failed {result.error_code}")
    refs = {}
    try:
        for kind in ("calendar", "etf"):
            started = time.monotonic()
            if kind == "calendar":
                query = bs.query_trade_dates(start_date=START_DATE, end_date=END_DATE)
                schema = CALENDAR_SCHEMA
            else:
                query = bs.query_history_k_data_plus(
                    ETF_CODE, ",".join(ETF_FIELDS), start_date=START_DATE,
                    end_date=END_DATE, frequency="d", adjustflag="3")
                schema = ETF_SCHEMA
            if query.error_code != "0":
                raise GateError(f"provider {kind} query failed {query.error_code}")
            rows = []
            while query.next():
                rows.append(query.get_row_data())
            if query.error_code != "0":
                raise GateError(f"provider {kind} iteration failed {query.error_code}")
            payload = {"schema": schema, "client_version": getattr(bs, "__version__", ""),
                       "start_date": START_DATE, "end_date": END_DATE,
                       "fields": list(query.fields), "rows": rows,
                       "observed_at_utc": datetime.now(timezone.utc).isoformat(),
                       "elapsed_seconds": round(time.monotonic() - started, 3)}
            if kind == "etf":
                payload["code"] = ETF_CODE
            refs[kind] = _freeze(root, kind, payload)
    finally:
        bs.logout()
    return {"schema": MANIFEST_SCHEMA, "calendar": refs["calendar"],
            "etf": refs["etf"], "stock_basic": basic,
            "histories": m10_manifest["histories"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("fetch", "audit"))
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--m10-manifest", type=Path)
    parser.add_argument("--m09-manifest", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "fetch":
        if args.m10_manifest is None or args.m09_manifest is None:
            parser.error("fetch requires --m10-manifest and --m09-manifest")
        m10 = json.loads(args.m10_manifest.read_text(encoding="utf-8"))
        m09 = json.loads(args.m09_manifest.read_text(encoding="utf-8"))
        manifest = fetch(args.root, m10, m09)
        args.manifest.write_bytes(_canonical(manifest))
        print(json.dumps({"status": "fetched", "sources": 2}))
        return 0
    if args.output is None:
        parser.error("audit requires --output")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    result = audit(args.root, manifest)
    args.output.write_bytes(_canonical(result))
    print(json.dumps({"status": result["status"], "stocks": result["stocks"],
                      "etf": result["etf"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
