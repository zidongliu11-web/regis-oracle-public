"""Offline, fail-closed quality gate for a proposed three-exchange quote source.

This module never calls a supplier or changes the trading router. A complete
response only proves coverage of the *supplied* listing, not of all A shares.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from zoneinfo import ZoneInfo


SH_TZ = ZoneInfo("Asia/Shanghai")
INPUT_SCHEMA = "mainline_quote_pilot_input.v1"
REPORT_SCHEMA = "mainline_quote_pilot_report.v1"
STATUS_SCHEMA = "mainline_quote_pilot_status.v1"
EXCHANGES = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}
CODE_RE = re.compile(r"^[0-9]{6}\.(SH|SZ|BJ)$")
MAX_PACKET_BYTES = 16 * 1024 * 1024
MAX_REPORT_BYTES = 64 * 1024
MAX_ROWS_PER_EXCHANGE = 6000
MAX_QUOTE_BATCH = 500
MAX_BATCHES = 80
MAX_QUOTE_AGE_SECONDS = 180
BOUNDARY = {"research_only": True, "full_a_mainline_validated": False,
            "economic_target_A_validated": False, "rl_reward_verified": False,
            "production_eligible": False, "generates_alert": False}


def _bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Tushare documents trade_time as a string without an offset. Interpret its
    # calendar clock in Shanghai; never interpret a naive time as host local.
    return (stamp.replace(tzinfo=SH_TZ) if stamp.tzinfo is None
            else stamp.astimezone(SH_TZ))


def _aware_time(value: object) -> datetime | None:
    stamp = _time(value)
    return stamp if stamp is not None and isinstance(value, str) and (
        value.endswith("Z") or re.search(r"[+-][0-9]{2}:[0-9]{2}$", value)
    ) else None


def _secret_key(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if any(word in str(key).lower() for word in ("token", "password", "secret", "api_key")):
                return True
            if _secret_key(item):
                return True
    elif isinstance(value, list):
        return any(_secret_key(item) for item in value)
    return False


def _number(value: object, positive: bool = False) -> bool:
    return (type(value) in (int, float) and math.isfinite(value)
            and (value > 0 if positive else value >= 0))


def assess(packet: object) -> dict[str, Any]:
    """Assess exact-code quote coverage; a source-success flag is never trade authority."""
    base: dict[str, Any] = {"schema": REPORT_SCHEMA, "provider": "Tushare",
                            "source_kind": "rt_k_realtime_daily_cumulative_snapshot",
                            "quote_age_limit_seconds": MAX_QUOTE_AGE_SECONDS,
                            "supplied_list_quote_complete": False,
                            "listing_independently_reconciled": False,
                            "sector_membership_pit_verified": False,
                            "true_minute_bars_verified": False,
                            "provider_rights_verified": False, **BOUNDARY}
    if not isinstance(packet, dict) or packet.get("schema") != INPUT_SCHEMA or packet.get("provider") != "Tushare":
        return {**base, "status": "invalid_source_packet", "reasons": ["identity_invalid"]}
    if _secret_key(packet):
        return {**base, "status": "invalid_source_packet", "reasons": ["secret_field_forbidden"]}
    observed = _aware_time(packet.get("observed_at"))
    if observed is None:
        return {**base, "status": "invalid_source_packet", "reasons": ["observed_time_invalid"]}
    listings = packet.get("listing_requests")
    quotes = packet.get("quote_requests")
    if (not isinstance(listings, list) or len(listings) != 3
            or not isinstance(quotes, list) or not 1 <= len(quotes) <= MAX_BATCHES):
        return {**base, "status": "invalid_source_packet", "reasons": ["request_structure_invalid"]}
    reasons: set[str] = set()
    expected: dict[str, set[str]] = {exchange: set() for exchange in EXCHANGES}
    rows_by_exchange: dict[str, int] = {exchange: 0 for exchange in EXCHANGES}
    listing_seen: set[str] = set()

    listing_completed: list[datetime] = []
    quote_started: list[datetime] = []

    def window(request: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
        started = _aware_time(request.get("started_at"))
        completed = _aware_time(request.get("completed_at"))
        if (started is None or completed is None or not started <= completed <= observed
                or started.date() != observed.date()):
            reasons.add("request_time_invalid")
        return started, completed

    for item in listings:
        if not isinstance(item, dict):
            reasons.add("listing_request_invalid")
            continue
        exchange = item.get("exchange")
        if exchange not in EXCHANGES or exchange in listing_seen:
            reasons.add("exchange_scope_invalid")
            continue
        listing_seen.add(exchange)
        _, completed = window(item)
        if completed is not None:
            listing_completed.append(completed)
        if item.get("endpoint") != "stock_basic" or item.get("list_status") != "L" or item.get("error"):
            reasons.add("listing_request_invalid")
        rows = item.get("rows")
        if not isinstance(rows, list) or not rows or len(rows) >= MAX_ROWS_PER_EXCHANGE:
            reasons.add("listing_rows_missing_or_at_cap")
            continue
        rows_by_exchange[exchange] = len(rows)
        for row in rows:
            if not isinstance(row, dict):
                reasons.add("listing_row_invalid")
                continue
            code = row.get("ts_code")
            if (not isinstance(code, str) or not CODE_RE.fullmatch(code)
                    or not code.endswith("." + EXCHANGES[exchange])
                    or row.get("exchange") != exchange or row.get("list_status") != "L"):
                reasons.add("listing_row_invalid")
                continue
            if code in expected[exchange]:
                reasons.add("listing_duplicate_code")
            expected[exchange].add(code)
    if listing_seen != set(EXCHANGES) or any(not expected[key] for key in EXCHANGES):
        reasons.add("three_exchange_listing_missing")

    requested: set[str] = set()
    returned: set[str] = set()
    fresh: set[str] = set()
    no_source_time: set[str] = set()
    stale: set[str] = set()
    invalid_quote: set[str] = set()
    for item in quotes:
        if not isinstance(item, dict):
            reasons.add("quote_request_invalid")
            continue
        started, completed = window(item)
        if started is not None:
            quote_started.append(started)
        if item.get("endpoint") != "rt_k" or item.get("error"):
            reasons.add("quote_request_invalid")
        codes, rows = item.get("requested_codes"), item.get("rows")
        if (not isinstance(codes, list) or not 1 <= len(codes) <= MAX_QUOTE_BATCH
                or not isinstance(rows, list) or len(rows) > len(codes)):
            reasons.add("quote_batch_invalid")
            continue
        if (any(not isinstance(code, str) or not CODE_RE.fullmatch(code) for code in codes)
                or len(set(codes)) != len(codes) or requested.intersection(codes)):
            reasons.add("quote_request_identity_invalid")
        requested.update(code for code in codes if isinstance(code, str))
        for row in rows:
            if not isinstance(row, dict):
                reasons.add("quote_row_invalid")
                continue
            code = row.get("ts_code")
            if not isinstance(code, str) or not CODE_RE.fullmatch(code) or code not in codes:
                reasons.add("quote_row_identity_invalid")
                continue
            if code in returned:
                reasons.add("quote_duplicate_code")
            returned.add(code)
            if not (_number(row.get("close"), True) and _number(row.get("pre_close"), True)
                    and _number(row.get("vol")) and _number(row.get("amount"))):
                invalid_quote.add(code)
            source_time = _time(row.get("trade_time"))
            if source_time is None:
                no_source_time.add(code)
            elif completed is None or source_time > completed:
                reasons.add("source_time_after_request")
                stale.add(code)
            elif (source_time.date() != observed.date()
                  or not 0 <= (observed - source_time).total_seconds() <= MAX_QUOTE_AGE_SECONDS):
                stale.add(code)
            else:
                fresh.add(code)

    if listing_completed and quote_started and max(listing_completed) > min(quote_started):
        reasons.add("listing_after_quote_request")
    all_expected = set().union(*expected.values())
    if requested != all_expected:
        reasons.add("requested_universe_mismatch")
    if returned != all_expected:
        reasons.add("quote_coverage_gap")
    if no_source_time:
        reasons.add("source_time_missing")
    if stale:
        reasons.add("source_time_stale_or_future")
    if invalid_quote:
        reasons.add("quote_values_invalid")
    by_exchange = {}
    for exchange, suffix in EXCHANGES.items():
        exp = expected[exchange]
        got = {code for code in returned if code.endswith("." + suffix)}
        by_exchange[exchange] = {
            "listed": len(exp), "listing_rows": rows_by_exchange[exchange],
            "requested": len(requested & exp), "returned": len(got & exp),
            "fresh_with_source_time": len(fresh & exp),
            "missing": len(exp - got),
            "missing_examples": sorted(exp - got)[:10],
            "source_time_missing": len(no_source_time & exp),
            "stale_or_future": len(stale & exp),
            "invalid_values": len(invalid_quote & exp),
        }
    complete = not reasons and requested == returned == fresh == all_expected
    return {**base, "status": ("supplied_list_quote_complete_research_only" if complete
                                else "coverage_or_source_gaps"),
            "observed_at": observed.isoformat(), "reasons": sorted(reasons),
            "supplied_list_quote_complete": complete,
            "listed_total": len(all_expected), "returned_total": len(returned),
            "exchange_coverage": by_exchange,
            "note": "仅核供应商提供的上市清单；未独立与三交易所对账，非全 A 主线或分钟 K 线验证。"}


def _bounded(path: Path, limit: int) -> bytes:
    if path.is_symlink():
        raise ValueError("symlink_refused")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("size_limit_exceeded")
    return raw


def _directory(root: Path) -> Path:
    base = Path(root).resolve()
    reports = base / "reports"
    directory = reports / "mainline_quote_pilot"
    # Reject both POSIX symlinks and Windows junctions/ancestor redirects. The
    # licensed raw packet must not escape the intended ignored report tree.
    if reports.resolve() != reports or directory.resolve() != directory:
        raise ValueError("output_directory_redirected")
    return directory


def publish_input(path: Path, *, root: Path) -> dict[str, Any]:
    """Explicit offline import. Raw licensed rows stay under ignored reports/."""
    raw = _bounded(Path(path), MAX_PACKET_BYTES)
    packet = json.loads(raw)
    if _secret_key(packet):
        raise ValueError("secret_field_forbidden")
    canonical = _bytes(packet)
    if len(canonical) > MAX_PACKET_BYTES:
        raise ValueError("size_limit_exceeded")
    result = assess(packet)
    digest = _sha(canonical)
    directory = _directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    saved = directory / f"{digest}.json"
    if saved.exists():
        if _bounded(saved, MAX_PACKET_BYTES) != canonical:
            raise ValueError("raw_receipt_collision_or_tamper")
    else:
        with saved.open("xb") as stream:
            stream.write(canonical)
    report = {**result, "raw_sha256": digest, "raw_name": saved.name}
    report["receipt_sha256"] = _sha(_bytes(report))
    with tempfile.NamedTemporaryFile(mode="wb", dir=directory, delete=False,
                                     prefix="latest.", suffix=".tmp") as stream:
        stream.write(_bytes(report))
        temporary = Path(stream.name)
    os.replace(temporary, directory / "latest.json")
    return report


def current_status(*, root: Path, now: datetime | None = None) -> dict[str, Any]:
    """Reassess bounded raw bytes; no supplier call from an HTTP request."""
    base = {"schema": STATUS_SCHEMA, "status": "awaiting_authorised_packet",
            "source_current": False, "supplied_list_quote_complete": False, **BOUNDARY}
    try:
        directory = _directory(root)
        report = json.loads(_bounded(directory / "latest.json", MAX_REPORT_BYTES))
        if not isinstance(report, dict):
            raise ValueError("report_invalid")
        report_body = dict(report)
        receipt_sha = report_body.pop("receipt_sha256")
        if receipt_sha != _sha(_bytes(report_body)):
            raise ValueError("report_hash_invalid")
        digest, name = report["raw_sha256"], report["raw_name"]
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or name != digest + ".json"):
            raise ValueError("raw_identity_invalid")
        raw = _bounded(directory / name, MAX_PACKET_BYTES)
        if _sha(raw) != digest or assess(json.loads(raw)) != {
            key: value for key, value in report_body.items()
            if key not in ("raw_sha256", "raw_name")
        }:
            raise ValueError("raw_content_invalid")
        observed = _aware_time(report.get("observed_at"))
        current = now or datetime.now(SH_TZ)
        if current.tzinfo is None or observed is None:
            raise ValueError("current_time_invalid")
        seconds = (current.astimezone(SH_TZ) - observed).total_seconds()
        source_current = 0 <= seconds <= MAX_QUOTE_AGE_SECONDS
        status = report["status"] if source_current else "historical_pilot_receipt"
        return {**base, "status": status, "source_current": source_current,
                "supplied_list_quote_complete": (source_current and report["supplied_list_quote_complete"]),
                "exchange_coverage": report.get("exchange_coverage"),
                "reasons": report.get("reasons"), "observed_at": report.get("observed_at"),
                "raw_sha256": digest, "receipt_sha256": receipt_sha,
                "listing_independently_reconciled": False,
                "sector_membership_pit_verified": False,
                "true_minute_bars_verified": False,
                "provider_rights_verified": False}
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return base


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Offline source-fit import; never contacts Tushare")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(publish_input(args.input, root=args.root), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
