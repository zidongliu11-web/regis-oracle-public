"""Bounded per-symbol history versus two frozen BaoStock bulk days.

Source geometry only. Raw rows stay in ignored local files; no trading signal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from research.baostock_bulk_daily_gate import (
    DAILY_SCHEMA, FROZEN_DAYS, GateError, _daily_rows, _source,
)


SCHEMA = "baostock_history_route_probe.v1"
RAW_SCHEMA = "baostock_symbol_history_raw.v1"
MANIFEST_SCHEMA = "m10_symbol_history_manifest.v1"
CODES = ("sh.600000", "sh.600084", "sh.601059")
START_DATE = "2018-01-01"
END_DATE = "2026-09-28"
FIELDS = (
    "date", "code", "open", "high", "low", "close", "preclose", "volume",
    "amount", "adjustflag", "turn", "tradestatus", "pctChg", "isST",
)
NUMERIC = {"open", "high", "low", "close", "preclose", "volume", "amount", "turn", "pctChg"}


def _canonical(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _number(value: str, label: str, *, allow_negative: bool = False) -> Decimal:
    if not isinstance(value, str) or not value:
        raise GateError(f"{label}: missing numeric field")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise GateError(f"{label}: invalid number") from None
    if not result.is_finite() or (result < 0 and not allow_negative):
        raise GateError(f"{label}: invalid number")
    return result


def _history_rows(payload: dict, code: str) -> dict[str, dict[str, str]]:
    if (payload.get("code") != code or payload.get("start_date") != START_DATE
            or payload.get("end_date") != END_DATE or tuple(payload["fields"]) != FIELDS):
        raise GateError("history: identity, range or fields mismatch")
    elapsed = payload.get("elapsed_seconds")
    if not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed <= 0:
        raise GateError("history: elapsed time invalid")
    rows: dict[str, dict[str, str]] = {}
    last_day = ""
    for values in payload["rows"]:
        if (not isinstance(values, list) or len(values) != len(FIELDS)
                or any(not isinstance(v, str) for v in values)):
            raise GateError("history: malformed row")
        row = dict(zip(FIELDS, values))
        day = row["date"]
        try:
            parsed = date.fromisoformat(day)
        except ValueError:
            raise GateError("history: invalid date") from None
        if (parsed.isoformat() != day or not START_DATE <= day <= END_DATE
                or day <= last_day or row["code"] != code):
            raise GateError("history: date order/range or code mismatch")
        last_day = day
        if row["adjustflag"] != "3" or row["tradestatus"] not in {"0", "1"} or row["isST"] not in {"0", "1"}:
            raise GateError("history: adjustment or state invalid")
        if row["tradestatus"] == "0":
            for key in ("volume", "amount"):
                if row[key] and _number(row[key], key) > 0:
                    raise GateError("history: suspended row has turnover")
        else:
            for key in ("open", "high", "low", "close", "preclose", "volume", "amount", "turn"):
                _number(row[key], key)
            _number(row["pctChg"], "pctChg", allow_negative=True)
            prices = {key: _number(row[key], key) for key in
                      ("open", "high", "low", "close", "preclose")}
            if (min(prices.values()) <= 0 or prices["low"] > min(prices["open"], prices["close"])
                    or prices["high"] < max(prices["open"], prices["close"])):
                raise GateError("history: active OHLC invalid")
            if (_number(row["volume"], "volume") == 0) != (_number(row["amount"], "amount") == 0):
                raise GateError("history: volume/amount contradiction")
        rows[day] = row
    if not rows:
        raise GateError("history: empty response")
    return rows


def _same_value(left: str, right: str, field: str) -> bool:
    if field not in NUMERIC or not left or not right:
        return left == right
    try:
        return Decimal(left) == Decimal(right)
    except InvalidOperation:
        return False


def audit(root: Path, manifest: dict) -> dict:
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise GateError("manifest schema invalid")
    if set(manifest.get("histories", {})) != set(CODES):
        raise GateError("manifest three symbols mismatch")
    if set(manifest.get("bulk_days", {})) != FROZEN_DAYS:
        raise GateError("manifest frozen bulk days mismatch")
    bulk = {}
    for day, ref in manifest["bulk_days"].items():
        source = _source(root, ref, DAILY_SCHEMA, f"bulk {day}")
        statuses, _ = _daily_rows(source, day)
        bulk[day] = ({row[1]: dict(zip(source["fields"], row)) for row in source["rows"]},
                     source["client_version"], statuses)
    rows_by_code = {}
    results = []
    versions = {item[1] for item in bulk.values()}
    for code in CODES:
        ref = manifest["histories"][code]
        source = _source(root, ref, RAW_SCHEMA, code)
        versions.add(source.get("client_version"))
        history = _history_rows(source, code)
        rows_by_code[code] = history
        results.append({
            "code": code, "source_sha256": ref["sha256"],
            "rows": len(history), "first": next(iter(history)),
            "last": next(reversed(history)),
            "elapsed_seconds": source["elapsed_seconds"],
            "suspended_rows": sum(row["tradestatus"] == "0" for row in history.values()),
        })
    if len(versions) != 1 or not next(iter(versions)):
        raise GateError("source client version mismatch")
    comparisons = []
    for day in sorted(FROZEN_DAYS):
        bulk_rows = bulk[day][0]
        for code in CODES:
            left = rows_by_code[code].get(day)
            right = bulk_rows.get(code)
            if (left is None) != (right is None):
                raise GateError(f"history/bulk membership mismatch {day} {code}")
            if left is None:
                comparisons.append({"day": day, "code": code, "state": "both_absent"})
                continue
            if left["tradestatus"] != right["tradestatus"]:
                raise GateError(f"history/bulk trade status mismatch {day} {code}")
            for field in FIELDS:
                if not _same_value(left[field], right[field], field):
                    raise GateError(f"history/bulk field mismatch {day} {code} {field}")
            comparisons.append({"day": day, "code": code, "state": "fields_equal",
                                "tradestatus": left["tradestatus"]})
    return {
        "schema": SCHEMA, "status": "three_symbol_cross_interface_fit",
        "query_start": START_DATE, "query_end": END_DATE,
        "client_version": next(iter(versions)), "symbols": results,
        "frozen_day_comparisons": comparisons,
        "elapsed_seconds_sum": round(sum(x["elapsed_seconds"] for x in results), 3),
        "full_a_verified": False, "historical_pit_verified": False,
        "source_rights_verified": False, "economic_admitted": False,
        "generates_alert": False, "raw_rows_in_git": False,
    }


def fetch(root: Path, m09_manifest: dict) -> dict:
    import baostock as bs

    if set(m09_manifest.get("days", {})) != FROZEN_DAYS:
        raise GateError("M09 source manifest frozen days mismatch")
    root = root.resolve()
    manifest = {"schema": MANIFEST_SCHEMA, "bulk_days":
                {day: m09_manifest["days"][day]["daily"] for day in sorted(FROZEN_DAYS)},
                "histories": {}}
    result = bs.login()
    if result.error_code != "0":
        raise GateError(f"provider login failed {result.error_code}")
    try:
        for code in CODES:
            started = time.monotonic()
            query = bs.query_history_k_data_plus(code, ",".join(FIELDS),
                                                 start_date=START_DATE, end_date=END_DATE,
                                                 frequency="d", adjustflag="3")
            if query.error_code != "0":
                raise GateError(f"provider query failed {code} {query.error_code}")
            rows = []
            while query.next():
                rows.append(query.get_row_data())
            if query.error_code != "0":
                raise GateError(f"provider iteration failed {code} {query.error_code}")
            payload = {"schema": RAW_SCHEMA, "client_version": getattr(bs, "__version__", ""),
                       "code": code, "start_date": START_DATE, "end_date": END_DATE,
                       "fields": list(query.fields), "rows": rows,
                       "observed_at_utc": datetime.now(timezone.utc).isoformat(),
                       "elapsed_seconds": round(time.monotonic() - started, 3)}
            raw = _canonical(payload)
            digest = hashlib.sha256(raw).hexdigest()
            relative = Path("reports/local_closure/m10_history_raw") / f"{code.replace('.', '_')}_{digest}.json"
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + f".{os.getpid()}.tmp")
            temporary.write_bytes(raw)
            temporary.replace(target)
            manifest["histories"][code] = {"path": relative.as_posix(), "sha256": digest}
    finally:
        bs.logout()
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("fetch", "audit"))
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--m09-manifest", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "fetch":
        if args.m09_manifest is None:
            parser.error("fetch requires --m09-manifest")
        older = json.loads(args.m09_manifest.read_text(encoding="utf-8"))
        manifest = fetch(args.root, older)
        args.manifest.write_bytes(_canonical(manifest))
        print(json.dumps({"symbols": len(manifest["histories"]), "manifest": str(args.manifest)}))
        return 0
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    report = audit(args.root, manifest)
    if args.output is None:
        parser.error("audit requires --output")
    args.output.write_bytes(_canonical(report))
    print(json.dumps({"status": report["status"], "symbols": report["symbols"],
                      "comparisons": report["frozen_day_comparisons"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
