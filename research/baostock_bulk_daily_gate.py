"""Check two frozen BaoStock daily batches against same-day source membership.

This is source fitness, not historical PIT certification or trading admission.
Raw files remain local; output contains counts and digests only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from research.bounded_baostock_universe_probe import validate_rows


SCHEMA = "baostock_bulk_daily_gate.v1"
LIST_SCHEMA = "bounded_baostock_universe_probe.v1"
DAILY_SCHEMA = "bulk_astock_exploration.v1"
DAILY_FIELDS = (
    "date", "code", "open", "high", "low", "close", "preclose", "volume",
    "amount", "adjustflag", "turn", "tradestatus", "pctChg", "peTTM",
    "pbMRQ", "psTTM", "pcfNcfTTM", "isST",
)
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
CODE = re.compile(r"(?:sh|sz)\.[0-9]{6}\Z")
MAX_SOURCE_BYTES = 10 * 1024 * 1024
FROZEN_DAYS = frozenset({"2018-01-02", "2026-09-28"})


class GateError(ValueError):
    pass


def _decimal(value: str, label: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str) or not value:
        raise GateError(f"{label}: missing numeric field")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise GateError(f"{label}: invalid number") from None
    if not number.is_finite() or number < 0 or (positive and number == 0):
        raise GateError(f"{label}: nonfinite or nonpositive")
    return number


def _source(root: Path, ref: dict, schema: str, label: str) -> dict:
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
        raise GateError(f"{label}: source reference missing")
    digest = ref.get("sha256")
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise GateError(f"{label}: source digest invalid")
    relative = Path(ref["path"])
    base = root.resolve()
    path = (base / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(base):
        raise GateError(f"{label}: source path unsafe")
    try:
        if path.stat().st_size > MAX_SOURCE_BYTES:
            raise GateError(f"{label}: oversized")
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, ValueError) as exc:
        if isinstance(exc, GateError):
            raise
        raise GateError(f"{label}: source unreadable") from None
    if hashlib.sha256(raw).hexdigest() != digest:
        raise GateError(f"{label}: source hash mismatch")
    if not isinstance(payload, dict) or payload.get("schema") != schema:
        raise GateError(f"{label}: source schema mismatch")
    if not isinstance(payload.get("fields"), list) or not isinstance(payload.get("rows"), list):
        raise GateError(f"{label}: source rows missing")
    return payload


def _daily_rows(payload: dict, day: str) -> tuple[dict[str, str], dict]:
    if payload.get("day") != day or tuple(payload["fields"]) != DAILY_FIELDS:
        raise GateError("daily: date or fields mismatch")
    rows = payload["rows"]
    if not rows:
        raise GateError("daily: empty response")
    seen: set[str] = set()
    trade_status: dict[str, str] = {}
    counts = Counter()
    for values in rows:
        if (not isinstance(values, list) or len(values) != len(DAILY_FIELDS)
                or any(not isinstance(value, str) for value in values)):
            raise GateError("daily: malformed row")
        row = dict(zip(DAILY_FIELDS, values))
        code = row["code"]
        if row["date"] != day or not CODE.fullmatch(code) or code in seen:
            raise GateError("daily: wrong date, invalid or duplicate code")
        seen.add(code)
        if row["adjustflag"] != "3" or row["tradestatus"] not in {"0", "1"}:
            raise GateError("daily: adjustment or trade status invalid")
        trade_status[code] = row["tradestatus"]
        if row["isST"] not in {"0", "1"}:
            raise GateError("daily: ST status invalid")
        counts["st" if row["isST"] == "1" else "non_st"] += 1
        if row["tradestatus"] == "0":
            counts["suspended"] += 1
            if row["close"]:
                counts["suspended_with_close"] += 1
            if not row["amount"]:
                counts["suspended_missing_amount"] += 1
            # A row marked suspended with positive turnover is ambiguous.
            for key in ("volume", "amount"):
                if row[key] and _decimal(row[key], f"{code}.{key}") > 0:
                    raise GateError("daily: suspended row has turnover")
            continue
        counts["trading_status"] += 1
        prices = {key: _decimal(row[key], f"{code}.{key}", positive=True)
                  for key in ("open", "high", "low", "close", "preclose")}
        if (prices["low"] > min(prices["open"], prices["close"])
                or prices["high"] < max(prices["open"], prices["close"])
                or prices["high"] < prices["low"]):
            raise GateError("daily: OHLC relation invalid")
        volume = _decimal(row["volume"], f"{code}.volume")
        amount = _decimal(row["amount"], f"{code}.amount")
        _decimal(row["turn"], f"{code}.turn")
        # pctChg can be negative; validate it separately.
        try:
            change = Decimal(row["pctChg"])
        except (InvalidOperation, TypeError):
            raise GateError("daily: pctChg invalid") from None
        if not change.is_finite():
            raise GateError("daily: pctChg nonfinite")
        if (volume == 0) != (amount == 0):
            raise GateError("daily: volume/amount contradiction")
        if volume == 0:
            counts["trading_status_without_turnover"] += 1
        else:
            counts["active_with_turnover"] += 1
    return trade_status, dict(sorted(counts.items()))


def audit_day(root: Path, day: str, refs: dict) -> dict:
    try:
        parsed_day = date.fromisoformat(day)
    except (TypeError, ValueError):
        raise GateError("day invalid") from None
    if parsed_day.isoformat() != day or not isinstance(refs, dict):
        raise GateError("day or references invalid")
    basic = _source(root, refs.get("stock_basic"), LIST_SCHEMA, "stock_basic")
    listed = _source(root, refs.get("all_stock"), LIST_SCHEMA, "all_stock")
    daily = _source(root, refs.get("daily"), DAILY_SCHEMA, "daily")
    versions = [payload.get("client_version") for payload in (basic, listed, daily)]
    if (not all(isinstance(v, str) and v for v in versions)
            or any(v != versions[0] for v in versions[1:])):
        raise GateError("source client version mismatch")
    if (basic.get("query") != "stock_basic" or basic.get("query_day") is not None
            or listed.get("query") != "all_stock" or listed.get("query_day") != day):
        raise GateError("list/basic query identity mismatch")
    try:
        basic_rows = validate_rows("stock_basic", basic["fields"], basic["rows"])
        list_rows = validate_rows("all_stock", listed["fields"], listed["rows"])
    except ValueError as exc:
        raise GateError(f"list/basic invalid: {exc}") from None
    stocks = {row["code"] for row in basic_rows if row["type"] == "1"}
    expected = {row["code"] for row in list_rows} & stocks
    daily_status, counts = _daily_rows(daily, day)
    actual = set(daily_status)
    if actual != expected:
        raise GateError(f"daily: membership set mismatch missing={len(expected-actual)} extra={len(actual-expected)}")
    listed_status = {row["code"]: row["tradeStatus"] for row in list_rows}
    mismatched = [code for code in expected if daily_status[code] != listed_status[code]]
    if mismatched:
        raise GateError(f"daily: trade status mismatch count={len(mismatched)}")
    if counts.get("trading_status", 0) + counts.get("suspended", 0) != len(expected):
        raise GateError("daily: status accounting mismatch")
    return {
        "day": day,
        "client_version": daily["client_version"],
        "source_digest": {key: refs[key]["sha256"] for key in
                          ("stock_basic", "all_stock", "daily")},
        "all_security_rows": len(list_rows),
        "current_basic_type1_rows": len(stocks),
        "observed_stock_rows": len(expected),
        "daily_rows": len(actual),
        "daily_membership_exact_match": True,
        "counts": counts,
        "suspended_prices_excluded_from_returns": True,
        "historical_pit_verified": False,
        "full_a_verified": False,
        "source_rights_verified": False,
        "economic_admitted": False,
    }


def audit_batch(root: Path, manifest: dict) -> dict:
    if not isinstance(manifest, dict) or manifest.get("schema") != "m09_bulk_source_manifest.v1":
        raise GateError("manifest schema invalid")
    days = manifest.get("days")
    if not isinstance(days, dict) or not days:
        raise GateError("manifest days missing")
    results, failures = [], []
    if set(days) != FROZEN_DAYS:
        failures.append({"day": None, "reason": "manifest frozen days mismatch"})
    for day, refs in sorted(days.items()):
        try:
            results.append(audit_day(root, day, refs))
        except (GateError, OSError, TypeError, KeyError) as exc:
            failures.append({"day": day, "reason": f"{type(exc).__name__}: {exc}"})
    return {
        "schema": SCHEMA,
        "status": "two_day_source_fit" if not failures and len(results) == 2 else "incomplete",
        "days": results,
        "failures": failures,
        "raw_rows_in_git": False,
        "historical_pit_verified": False,
        "full_a_verified": False,
        "source_rights_verified": False,
        "economic_admitted": False,
        "generates_alert": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    report = audit_batch(args.root, manifest)
    args.output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "days": report["days"],
                      "failures": report["failures"]}, ensure_ascii=False))
    return 0 if report["status"] == "two_day_source_fit" else 1


if __name__ == "__main__":
    raise SystemExit(main())
