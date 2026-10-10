"""One-day, local, research-only forward audit of the existing sector feed.

This is deliberately separate from cloud_sector_observations: it cannot mint a
cloud receipt, cannot backfill a missed bucket, and never changes the router.
Raw supplier packets stay under ignored reports/, not in GitHub.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Callable

from .market_sector_strategy_adapter import SH_TZ, adapt_sector_snapshot
from .market_state_daily import PROJECT_ROOT


SCHEMA = "local_sector_forward_probe.v1"
TARGET_DAY = "2026-09-28"  # SSE 2026 Mid-Autumn notice; extend only after a new calendar audit.
SOURCE_NOTICE = "https://www.sse.com.cn/disclosure/announcement/general/c/c_20260915_10832273.shtml"
EXPECTED_SOURCE = "东方财富板块实时（push2delay 延迟源）"
MAX_STATUS_AUDIT_BYTES = 32_000_000
BOUNDARY = {"research_only": True, "forward_sample": True,
            "economic_result_validated": False, "generates_alert": False,
            "changes_trade_authority": False}


def _bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: object) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _contract() -> dict[str, str]:
    directory = Path(__file__).parent
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in ("local_sector_probe.py", "market_intelligence.py",
                         "market_sector_strategy_adapter.py")}


def bucket(stamp: datetime) -> int | None:
    if stamp.tzinfo is None:
        raise ValueError("timezone_required")
    local = stamp.astimezone(SH_TZ)
    if local.date().isoformat() != TARGET_DAY:
        return None
    minute = local.hour * 60 + local.minute
    if 570 <= minute < 690:
        return (minute - 570) // 5
    if 780 <= minute < 900:
        return 24 + (minute - 780) // 5
    return None


def _stamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.astimezone(SH_TZ) if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def inspect(snapshot: object, started: datetime, observed: datetime) -> tuple[list[str], dict[str, Any]]:
    """Require a new fetch in this attempt and every row's own source clock."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("meta"), dict):
        return ["source_missing"], {}
    errors: list[str] = []
    meta = snapshot["meta"]
    if snapshot.get("source") != EXPECTED_SOURCE:
        errors.append("supplier_identity_invalid")
    fetched = _stamp(snapshot.get("fetched_at"))
    if snapshot.get("served_from_cache") is not False:
        errors.append("cache_or_unknown_delivery")
    if (fetched is None or not started <= fetched <= observed
            or (observed - fetched).total_seconds() > 180):
        errors.append("fetch_time_invalid")
    if snapshot.get("status") != "ok" or snapshot.get("health_status") != "verified":
        errors.append("source_unverified")
    for key in ("coverage_complete", "industry_complete", "concept_complete",
                "timestamp_complete", "breadth_count_complete"):
        if meta.get(key) is not True:
            errors.append(key)
    identities: list[tuple[str, str, str]] = []
    source_stamps: list[datetime] = []
    for kind, field, total in (("industry", "industries", "industry_reported_total"),
                               ("concept", "concepts", "concept_reported_total")):
        rows = snapshot.get(field)
        expected = meta.get(total)
        if not isinstance(rows, list) or type(expected) is not int or expected <= 0 or len(rows) != expected:
            errors.append("population_incomplete")
            continue
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                errors.append("row_invalid")
                continue
            code, name = row.get("code"), row.get("name")
            if not isinstance(code, str) or not code or code in seen or not isinstance(name, str) or not name:
                errors.append("identity_invalid")
            if isinstance(code, str):
                seen.add(code)
                identities.append((kind, code, str(name)))
            change, up, down = row.get("change_pct"), row.get("up_count"), row.get("down_count")
            if type(change) not in (int, float) or not math.isfinite(change):
                errors.append("change_invalid")
            if any(type(n) is not int or n < 0 for n in (up, down)):
                errors.append("breadth_invalid")
            stamp = _stamp(row.get("data_as_of"))
            if stamp is None or stamp.date() != observed.date() or not 0 <= (observed - stamp).total_seconds() <= 180:
                errors.append("row_time_invalid")
            else:
                source_stamps.append(stamp)
    if errors:
        return sorted(set(errors)), {}
    adapted = adapt_sector_snapshot(snapshot, now=observed)
    if adapted.get("observation_ready") is not True:
        return ["adapter_rejected"], {}
    return [], {"source_as_of_min": min(source_stamps).isoformat(),
                "source_as_of_max": max(source_stamps).isoformat(),
                "supplier_source": snapshot.get("source"),
                "board_identity_sha256": _sha(sorted(identities)),
                "industry_count": len(snapshot["industries"]),
                "concept_count": len(snapshot["concepts"]),
                "observed_board_count": len(adapted.get("observed_sectors") or []),
                "observed_boards": [{"kind": row["kind"], "code": row["code"], "name": row["name"]}
                                    for row in (adapted.get("observed_sectors") or [])[:3]]}


def _directory(root: Path) -> Path:
    return root / "reports" / "local_sector_probe" / TARGET_DAY


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink() or path.stat().st_size > 3_000_000:
        raise ValueError("packet_path_invalid")
    packet = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(packet, dict):
        raise ValueError("packet_invalid")
    body = dict(packet)
    identity = body.pop("packet_id", None)
    if (identity != _sha(body) or packet.get("schema") != SCHEMA
            or any(packet.get(key) != value for key, value in BOUNDARY.items())
            or packet.get("day") != TARGET_DAY
            or packet.get("bucket") != int(path.stem)):
        raise ValueError("packet_identity_invalid")
    started = _stamp(packet.get("started_at"))
    observed = _stamp(packet.get("observed_at"))
    if (started is None or observed is None or observed < started
            or bucket(started) != packet["bucket"]
            or observed.date().isoformat() != TARGET_DAY):
        raise ValueError("packet_time_invalid")
    contract = packet.get("source_contract")
    if (not isinstance(contract, dict) or set(contract) != {
            "local_sector_probe.py", "market_intelligence.py",
            "market_sector_strategy_adapter.py"}
            or any(not isinstance(value, str) or len(value) != 64
                   or any(c not in "0123456789abcdef" for c in value)
                   for value in contract.values())):
        raise ValueError("packet_contract_invalid")
    # The current implementation can re-evaluate its own packets. Retired code
    # is retained as historical identity only, never reclassified as current.
    if contract == _contract():
        errors, summary = inspect(packet.get("snapshot"), started, observed)
        if packet.get("source_error_type"):
            errors = ["source_request_failed"]
        if bucket(observed) != packet["bucket"]:
            errors = sorted(set(errors + ["capture_crossed_bucket"]))
        if packet.get("errors") != errors or packet.get("summary") != (summary if not errors else {}):
            raise ValueError("packet_assessment_changed")
    return packet


def _request_diagnostics(snapshot: object) -> list[dict[str, Any]]:
    """Expose only the transport categories allowed by the probe status API."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("source_request_errors"), list):
        return []
    allowed = {"timeout", "connection", "invalid_payload", "request_failed", "http_error"}
    result: list[dict[str, Any]] = []
    for entry in snapshot["source_request_errors"][:12]:
        if not isinstance(entry, dict):
            continue
        kind, page, attempts = entry.get("kind"), entry.get("page"), entry.get("attempts")
        if kind not in {"industry", "concept"} or type(page) is not int or not 1 <= page <= 6:
            continue
        if not isinstance(attempts, list):
            continue
        safe = [item for item in attempts[:2]
                if isinstance(item, str) and (item in allowed or
                    (len(item) == 8 and item.startswith("http_") and item[5:].isdigit()
                     and 400 <= int(item[5:]) <= 599))]
        if safe:
            result.append({"kind": kind, "page": page, "attempts": safe})
    return result


def _fetch(root: Path) -> dict[str, Any]:
    from .market_intelligence import MarketIntelligence
    return MarketIntelligence(None, root / "unused_local_probe_cache", lambda _: ([], {})).market_sectors(force=True)


def run_once(*, root: Path = PROJECT_ROOT, now: Callable[[], datetime] | None = None,
             fetch: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    now = now or (lambda: datetime.now(SH_TZ))
    start = now()
    if start.tzinfo is None:
        raise ValueError("timezone_required")
    start = start.astimezone(SH_TZ)
    index = bucket(start)
    if index is None:
        return {"status": "outside_registered_session", "target_day": TARGET_DAY, **BOUNDARY}
    directory = _directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{index:02}.json"
    if path.exists():
        return {"status": "already_attempted", "packet_id": _read(path)["packet_id"],
                "bucket": index, **BOUNDARY}
    claim = directory / f"{index:02}.claim"
    try:
        with claim.open("x", encoding="utf-8") as handle:
            handle.write(_bytes({"started_at": start.isoformat(), "pid": os.getpid()}).decode("utf-8"))
    except FileExistsError:
        return {"status": "claimed_or_interrupted", "bucket": index, **BOUNDARY}
    # A crash leaves .claim, intentionally preventing a later fake retry of this slot.
    source_error = None
    try:
        snapshot = (fetch or (lambda: _fetch(root)))()
    except Exception as exc:
        snapshot, source_error = {}, type(exc).__name__
    observed = now()
    if observed.tzinfo is None:
        raise ValueError("timezone_required")
    observed = observed.astimezone(SH_TZ)
    errors, summary = inspect(snapshot, start, observed)
    if source_error:
        errors = ["source_request_failed"]
    if bucket(observed) != index or observed < start:
        errors = sorted(set(errors + ["capture_crossed_bucket"]))
    packet = {"schema": SCHEMA, "day": TARGET_DAY, "bucket": index,
              "started_at": start.isoformat(), "observed_at": observed.isoformat(),
              "source_contract": _contract(), "source_error_type": source_error,
              "snapshot": snapshot, "errors": errors, "summary": summary if not errors else {},
              **BOUNDARY}
    packet["packet_id"] = _sha(packet)
    temporary = directory / f"{index:02}.{os.getpid()}.tmp"
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(_bytes(packet).decode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    claim.unlink()
    return {"status": "accepted" if not errors else "rejected", "bucket": index,
            "packet_id": packet["packet_id"], "errors": errors, **BOUNDARY}


def current_status(*, root: Path = PROJECT_ROOT, now: datetime | None = None) -> dict[str, Any]:
    observed = (now or datetime.now(SH_TZ)).astimezone(SH_TZ)
    directory = _directory(root)
    base = {"schema": "local_sector_probe_status.v1", "target_day": TARGET_DAY,
            "source_notice": SOURCE_NOTICE, "checked_at": observed.isoformat(),
            "attempts": 0, "accepted": 0, "rejected": 0, "unrecovered_claims": 0,
            "continuity_buckets": 0, "latest": None, "current_mainline_validated": False,
            "strategy_profit_validated": False, "source_sequence_valid_now": False,
            "external_time_attested": False, "immutable_archive_attested": False,
            **BOUNDARY}
    if not directory.exists():
        return {**base, "status": "awaiting_registered_session" if observed.date().isoformat() <= TARGET_DAY
                else "no_capture"}
    paths = sorted(directory.glob("[0-4][0-9].json"))[:48]
    try:
        audit_bytes = sum(path.stat().st_size for path in paths)
    except OSError:
        return {**base, "status": "audit_unavailable", "reason": "packet_stat_failed"}
    if audit_bytes > MAX_STATUS_AUDIT_BYTES:
        return {**base, "status": "audit_unavailable",
                "reason": "archive_exceeds_read_budget", "archive_bytes": audit_bytes,
                "audit_byte_budget": MAX_STATUS_AUDIT_BYTES}
    packets = []
    invalid = 0
    for path in paths:
        try:
            original = _read(path)
            # Keep only the verified assessment, never 48 raw supplier snapshots
            # in the web worker after this packet has been checked.
            packet = {key: original[key] for key in ("bucket", "observed_at", "started_at",
                                                     "errors", "summary", "source_contract", "packet_id")}
            packet["source_request_errors"] = (_request_diagnostics(original.get("snapshot"))
                                               if packet["errors"] else [])
            del original
            if _stamp(packet.get("observed_at")) is None or _stamp(packet["observed_at"]) > observed:
                raise ValueError("future_packet")
            packets.append(packet)
        except (OSError, ValueError, json.JSONDecodeError):
            invalid += 1
    claims = len(list(directory.glob("[0-4][0-9].claim")))
    accepted = [packet for packet in packets if not packet["errors"]]
    latest = packets[-1] if packets else None
    continuity = 0
    if latest and not latest["errors"]:
        last = None
        for packet in reversed(packets):
            summary = packet.get("summary") or {}
            earlier_time = _stamp(summary.get("source_as_of_min"))
            later_time = _stamp((last.get("summary") or {}).get("source_as_of_min")) if last else None
            if (packet["errors"] or (last and (last["bucket"] != packet["bucket"] + 1
                    or last["source_contract"] != packet["source_contract"]
                    or last["summary"].get("supplier_source") != summary.get("supplier_source")
                    or last["summary"].get("board_identity_sha256") != summary.get("board_identity_sha256")
                    or earlier_time is None or later_time is None
                    or later_time <= earlier_time))):
                break
            continuity += 1
            last = packet
    latest_summary = (latest.get("summary") or {}) if latest else {}
    source_at = _stamp(latest_summary.get("source_as_of_min"))
    current = bool(latest and not latest["errors"] and continuity >= 2
                   and latest["source_contract"] == _contract()
                   and observed.date().isoformat() == TARGET_DAY
                   and bucket(observed) is not None and source_at
                   and 0 <= (observed - source_at).total_seconds() <= 180)
    status = "local_source_sequence_qualified" if current else (
        "source_contract_changed" if latest and latest["source_contract"] != _contract() else
        "last_capture_rejected" if latest and latest["errors"] else
        "single_or_stale_observation" if latest else "no_capture")
    return {**base, "status": status, "attempts": len(packets),
            "accepted": len(accepted), "rejected": len(packets) - len(accepted) + invalid,
            "unrecovered_claims": claims, "continuity_buckets": continuity,
            "current_mainline_validated": False,
            "latest": ({"bucket": latest["bucket"], "observed_at": latest["observed_at"],
                        "source_as_of": latest_summary.get("source_as_of_min"),
                        "source": latest_summary.get("supplier_source"),
                        "industry_count": latest_summary.get("industry_count"),
                        "concept_count": latest_summary.get("concept_count"),
                        "observed_boards": latest_summary.get("observed_boards") or [],
                        "errors": latest["errors"],
                        "source_request_errors": latest["source_request_errors"],
                        "packet_id": latest["packet_id"]}
                       if latest else None),
            "source_sequence_valid_now": current}


def main() -> None:
    parser = argparse.ArgumentParser(description="Local research-only sector source probe")
    parser.add_argument("mode", choices=("once", "loop", "status"))
    args = parser.parse_args()
    if args.mode == "status":
        print(json.dumps(current_status(), ensure_ascii=False))
    elif args.mode == "once":
        print(json.dumps(run_once(), ensure_ascii=False))
    else:
        attempted_bucket = None
        while True:
            current = datetime.now(SH_TZ)
            if current.date().isoformat() > TARGET_DAY or (current.date().isoformat() == TARGET_DAY
                    and current.hour * 60 + current.minute >= 905):
                break
            active_bucket = bucket(current)
            if active_bucket is not None and active_bucket != attempted_bucket:
                print(json.dumps(run_once(), ensure_ascii=False), flush=True)
                attempted_bucket = active_bucket
            time.sleep(10)
        print(json.dumps(current_status(), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
