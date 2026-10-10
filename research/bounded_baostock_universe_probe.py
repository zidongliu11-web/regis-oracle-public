"""Bounded, local-only source geometry probe; never a PIT or trading admission."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path


SCHEMA = "bounded_baostock_universe_probe.v1"
CODE = re.compile(r"(?:sh|sz|bj)\.[0-9]{6}\Z")
QUERIES = {"all_stock": ("code", "tradeStatus", "code_name"),
           "stock_basic": ("code", "code_name", "ipoDate", "outDate", "type", "status")}
DEFAULT_DAYS = ("2018-01-02", "2026-09-28")


class ProbeError(ValueError):
    pass


def _canonical(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n").encode("utf-8")


def _atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".probe-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_rows(kind: str, fields: list[str], rows: list[list[str]]) -> list[dict[str, str]]:
    expected = QUERIES.get(kind)
    if expected is None or tuple(fields) != expected:
        raise ProbeError(f"{kind}: unexpected fields")
    if not rows:
        raise ProbeError(f"{kind}: empty response")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if len(row) != len(fields) or not all(isinstance(x, str) for x in row):
            raise ProbeError(f"{kind}: malformed row")
        item = dict(zip(fields, row))
        code = item["code"]
        if not CODE.fullmatch(code) or code in seen:
            raise ProbeError(f"{kind}: invalid or duplicate code")
        seen.add(code)
        if kind == "all_stock" and item["tradeStatus"] not in {"0", "1"}:
            raise ProbeError(f"{kind}: invalid tradeStatus")
        if kind == "stock_basic":
            if item["type"] not in {"1", "2", "3", "4", "5"} or item["status"] not in {"0", "1"}:
                raise ProbeError(f"{kind}: invalid type or status")
            for key in ("ipoDate", "outDate"):
                value = item[key]
                if value:
                    try:
                        parsed = date.fromisoformat(value)
                    except ValueError:
                        raise ProbeError(f"{kind}: invalid {key}") from None
                    if parsed.isoformat() != value:
                        raise ProbeError(f"{kind}: noncanonical {key}")
        result.append(item)
    return result


def summarize(day: str, all_rows: list[dict[str, str]],
              basic_rows: list[dict[str, str]]) -> dict:
    try:
        current = date.fromisoformat(day)
    except ValueError:
        raise ProbeError("invalid day") from None
    if current.isoformat() != day:
        raise ProbeError("noncanonical day")
    basic = {row["code"]: row for row in basic_rows}
    observed = {row["code"] for row in all_rows}
    stock_master = {code for code, row in basic.items() if row["type"] == "1"}
    stock_candidates = observed & stock_master
    # The present-day basic table may have revised metadata. This intersection
    # is a source-observed candidate set, not certified historical membership.
    dated_candidates = {
        code for code in stock_candidates
        if basic[code]["ipoDate"] and basic[code]["ipoDate"] <= day
        and (not basic[code]["outDate"] or basic[code]["outDate"] > day)
    }
    return {
        "day": day,
        "all_security_rows": len(all_rows),
        "all_security_prefixes": dict(sorted(Counter(
            row["code"].split(".", 1)[0] for row in all_rows).items())),
        "basic_rows_current_master": len(basic_rows),
        "basic_type1_rows_current_master": len(stock_master),
        "observed_type1_intersection": len(stock_candidates),
        "dated_type1_intersection_current_master": len(dated_candidates),
        "dated_type1_prefixes": dict(sorted(Counter(
            code.split(".", 1)[0] for code in dated_candidates).items())),
        "basic_missing_for_observed": len(observed - set(basic)),
        "bse_code_rows_in_all_stock": sum(row["code"].startswith("bj.")
                                           for row in all_rows),
        "historical_pit_membership_verified": False,
        "full_a_coverage_verified": False,
    }


def freeze_response(root: Path, kind: str, day: str | None, fields: list[str],
                    rows: list[list[str]], *, provider_version: str,
                    observed_at_utc: str) -> dict:
    # Validate before writing a success receipt; callers record failures separately.
    validate_rows(kind, fields, rows)
    record = {
        "schema": SCHEMA,
        "provider": "BaoStock",
        "client_version": provider_version,
        "query": kind,
        "query_day": day,
        "received_at_utc": observed_at_utc,
        "fields": fields,
        "rows": rows,
    }
    raw = _canonical(record)
    digest = hashlib.sha256(raw).hexdigest()
    suffix = day or "current"
    path = root / "reports" / "local_closure" / "m08_baostock_raw" / (
        f"{kind}_{suffix}_{digest}.json")
    if path.exists():
        if path.read_bytes() != raw:
            raise ProbeError("raw receipt collision")
    else:
        _atomic(path, raw)
    return {"path": str(path.relative_to(root)).replace("\\", "/"),
            "sha256": digest, "bytes": len(raw), "rows": len(rows),
            "received_at_utc": observed_at_utc}


def verify_receipt(root: Path, receipt: dict) -> list[dict[str, str]]:
    relative = Path(receipt["path"])
    base = root.resolve()
    path = (base / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(base):
        raise ProbeError("receipt path escapes root")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
        raise ProbeError("receipt hash mismatch")
    record = json.loads(raw)
    if _canonical(record) != raw or record.get("schema") != SCHEMA:
        raise ProbeError("receipt content mismatch")
    if len(record["rows"]) != receipt["rows"]:
        raise ProbeError("receipt row count mismatch")
    return validate_rows(record["query"], record["fields"], record["rows"])


def _read_query(result, kind: str) -> tuple[list[str], list[list[str]]]:
    if result.error_code != "0":
        raise ProbeError(f"{kind}: provider error {result.error_code}: {result.error_msg}")
    rows = []
    while result.next():
        rows.append(result.get_row_data())
    if result.error_code != "0":
        raise ProbeError(f"{kind}: provider error after iteration {result.error_code}: {result.error_msg}")
    return list(result.fields), rows


def run(root: Path, days: tuple[str, ...] = DEFAULT_DAYS) -> dict:
    import baostock as bs

    root = root.resolve()
    for day in days:
        date.fromisoformat(day)
    observed = datetime.now(timezone.utc).isoformat()
    report = {
        "schema": SCHEMA,
        "started_at_utc": observed,
        "provider": "BaoStock",
        "client_version": getattr(bs, "__version__", "0.9.3_package_unversioned"),
        "days": list(days),
        "receipts": {},
        "results": [],
        "failures": [],
        "raw_redistribution_authorized": False,
        "historical_pit_membership_verified": False,
        "full_a_coverage_verified": False,
        "target_a_admitted": False,
    }
    try:
        login = bs.login()
        if login.error_code != "0":
            raise ProbeError(f"login: provider error {login.error_code}: {login.error_msg}")
        try:
            fields, rows = _read_query(bs.query_stock_basic(), "stock_basic")
            receipt = freeze_response(root, "stock_basic", None, fields, rows,
                                      provider_version=report["client_version"],
                                      observed_at_utc=datetime.now(timezone.utc).isoformat())
            report["receipts"]["stock_basic"] = receipt
            basic = verify_receipt(root, receipt)
            for day in days:
                try:
                    fields, rows = _read_query(bs.query_all_stock(day=day), "all_stock")
                    receipt = freeze_response(root, "all_stock", day, fields, rows,
                                              provider_version=report["client_version"],
                                              observed_at_utc=datetime.now(timezone.utc).isoformat())
                    report["receipts"][day] = receipt
                    report["results"].append(summarize(
                        day, verify_receipt(root, receipt), basic))
                except Exception as exc:
                    report["failures"].append({"query": "all_stock", "day": day,
                                               "reason": f"{type(exc).__name__}: {exc}"})
        finally:
            try:
                bs.logout()
            except Exception as exc:
                report["failures"].append({"query": "logout",
                                           "reason": f"{type(exc).__name__}: {exc}"})
    except Exception as exc:
        report["failures"].append({"query": "stock_basic_or_login",
                                   "reason": f"{type(exc).__name__}: {exc}"})
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["status"] = "bounded_observation" if not report["failures"] else "incomplete"
    path = root / "reports" / "local_closure" / "m08_baostock_probe_20260929.json"
    raw_report = _canonical(report)
    _atomic(path, raw_report)
    _atomic(root / "QUANT_RESEARCH_M08_SOURCE_SUMMARY_20260929.json", raw_report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--days", nargs="+", default=list(DEFAULT_DAYS))
    args = parser.parse_args()
    report = run(args.root, tuple(args.days))
    print(json.dumps({key: report[key] for key in
                      ("status", "results", "failures", "receipts")},
                     ensure_ascii=False, indent=2))
    return 0 if report["status"] == "bounded_observation" else 1


if __name__ == "__main__":
    raise SystemExit(main())
