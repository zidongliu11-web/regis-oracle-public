"""Registered local after-close inputs; no cloud receipt or trading authority."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, time
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time as clock

from .cloud_sector_close import assess
from .local_sector_probe import EXPECTED_SOURCE, TARGET_DAY, _request_diagnostics
from .market_intelligence import MarketIntelligence
from .market_sector_strategy_adapter import SH_TZ
from .market_state_daily import PROJECT_ROOT
from .research_artifact_paths import research_artifact_path
from .sector_forward_ledger import (
    MAX_CALENDAR_BYTES, MAX_CALENDAR_RECEIPT_BYTES, _bounded_file, _calendar)

SCHEMA = "local_sector_close_capture.v1"
STATUS_SCHEMA = "local_sector_close_status.v1"
WINDOWS = ((time(16), time(16, 5)), (time(16, 15), time(16, 20)))
MAX_PACKET_BYTES = 4 * 1024 * 1024
BOUNDARY = {"execution_location": "local", "research_only": True,
            "cloud_backup_verified": False, "external_time_attested": False,
            "immutable_storage_attested": False, "full_market_verified": False,
            "current_mainline_validated": False, "leader_validated": False,
            "economic_target_A_validated": False, "generates_alert": False,
            "changes_trade_authority": False, "forecast_validated": False,
            "sequential_rl_trained": False}


def _code_identity():
    folder = Path(__file__).parent
    return {name: hashlib.sha256((folder / name).read_bytes()).hexdigest() for name in (
        "local_sector_close.py", "market_intelligence.py", "cloud_sector_close.py",
        "market_sector_strategy_adapter.py", "sector_forward_ledger.py")}


# Bind the imported implementation, not a new disk version while this job waits.
LOADED_SOURCES = _code_identity()


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _stamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(SH_TZ)


def window(stamp):
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("timezone_required")
    local = stamp.astimezone(SH_TZ)
    if local.date().isoformat() != TARGET_DAY:
        return None
    return next((i for i, (start, end) in enumerate(WINDOWS)
                 if start <= local.time() < end), None)


def _redirected(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _directory(root):
    root = Path(root).absolute()
    directory = root / "reports/local_sector_close" / TARGET_DAY
    for path in (root, root / "reports", directory.parent, directory):
        if _redirected(path):
            raise ValueError("local_archive_redirected")
    return directory


def _read(path):
    if _redirected(path) or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("local_packet_path_invalid")
    with path.open("rb") as stream:
        raw = stream.read(MAX_PACKET_BYTES + 1)
    if len(raw) > MAX_PACKET_BYTES:
        raise ValueError("local_packet_too_large")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("local_packet_duplicate_key")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=unique)


def _assessment(snapshot, started, observed, slot):
    errors = []
    if (observed < started or window(started) != slot or window(observed) != slot):
        errors.append("capture_crossed_registered_window")
    if not isinstance(snapshot, dict) or snapshot.get("source") != EXPECTED_SOURCE:
        errors.append("supplier_identity_invalid")
    try:
        fetch_started = _stamp(snapshot["fetch_started_at"])
        fetched = _stamp(snapshot["fetched_at"])
        if not started <= fetch_started <= fetched <= observed:
            errors.append("fetch_time_invalid")
    except (KeyError, ValueError, TypeError, OverflowError):
        errors.append("fetch_time_invalid")
    try:
        result = assess(snapshot, TARGET_DAY, observed)
    except (KeyError, ValueError, TypeError, OverflowError):
        # No exception text or endpoint URLs are exposed in the site or CLI.
        result = None
        errors.append("source_assessment_rejected")
    return sorted(set(errors)), result if not errors else None


def _session_window(sessions):
    index = sessions.index(TARGET_DAY)
    return sessions[index:index + 6]


def _freeze_calendar(root, started):
    root = Path(root)
    path = root / "reports/current_market_calendar.json"
    if _redirected(path):
        raise ValueError("calendar_file_redirected")
    raw = _bounded_file(path, MAX_CALENDAR_BYTES, "calendar_file")
    calendar = json.loads(raw)
    receipt_path = research_artifact_path(calendar["source_receipt"]["path"], project_root=root)
    if _redirected(receipt_path):
        raise ValueError("calendar_receipt_redirected")
    receipt_raw = _bounded_file(receipt_path, MAX_CALENDAR_RECEIPT_BYTES,
                                "calendar_source_receipt")
    sessions, sha = _calendar(root, started, frozen_raw=(raw, receipt_raw))
    _session_window(sessions)
    return sha, {"calendar_b64": base64.b64encode(raw).decode("ascii"),
                 "source_receipt_b64": base64.b64encode(receipt_raw).decode("ascii")}


def _verify_calendar(packet, root, started, current_sessions):
    bundle = packet["calendar_bundle"]
    if not isinstance(bundle, dict) or set(bundle) != {"calendar_b64", "source_receipt_b64"}:
        raise ValueError("local_calendar_bundle_invalid")
    originals = []
    for key, limit in (("calendar_b64", MAX_CALENDAR_BYTES),
                       ("source_receipt_b64", MAX_CALENDAR_RECEIPT_BYTES)):
        encoded = bundle[key]
        if not isinstance(encoded, str) or len(encoded) > 4 * ((limit + 2) // 3):
            raise ValueError("local_calendar_bundle_invalid")
        originals.append(base64.b64decode(encoded, validate=True))
    sessions, sha = _calendar(Path(root), started, frozen_raw=tuple(originals))
    frozen_window = _session_window(sessions)
    if (packet["calendar_sha256"] != sha
            or _session_window(current_sessions)[:len(frozen_window)] != frozen_window):
        raise ValueError("local_calendar_identity_changed")


def read_packet(path, slot, *, now, root, current_sessions):
    packet = _read(path)
    fields = {"schema", "reference_day", "window_index", "started_at", "observed_at",
              "source_contract", "calendar_sha256", "calendar_bundle", "snapshot", "source_error_type",
              "errors", "packet_id", *BOUNDARY}
    if not isinstance(packet, dict) or set(packet) != fields:
        raise ValueError("local_packet_schema_invalid")
    identity = dict(packet)
    packet_id = identity.pop("packet_id")
    if (packet_id != hashlib.sha256(_bytes(identity)).hexdigest()
            or packet["schema"] != SCHEMA or packet["reference_day"] != TARGET_DAY
            or type(packet["window_index"]) is not int or packet["window_index"] != slot
            or packet["source_contract"] != LOADED_SOURCES
            or any(type(packet.get(key)) is not type(value) or packet[key] != value
                   for key, value in BOUNDARY.items())
            or not isinstance(packet["calendar_sha256"], str)
            or len(packet["calendar_sha256"]) != 64
            or set(packet["calendar_sha256"]) - set("0123456789abcdef")):
        raise ValueError("local_packet_identity_invalid")
    started, observed = _stamp(packet["started_at"]), _stamp(packet["observed_at"])
    if not started <= observed <= now or window(started) != slot:
        raise ValueError("local_packet_clock_invalid")
    _verify_calendar(packet, root, started, current_sessions)
    errors, result = _assessment(packet["snapshot"], started, observed, slot)
    if packet["source_error_type"] is not None:
        if (not isinstance(packet["source_error_type"], str)
                or packet["snapshot"] is not None or len(packet["source_error_type"]) > 80):
            raise ValueError("local_packet_error_invalid")
        errors, result = ["source_request_failed"], None
    if packet["errors"] != errors:
        raise ValueError("local_packet_assessment_changed")
    return packet, result


def current_status(*, root=PROJECT_ROOT, now=None):
    current = now or datetime.now(SH_TZ)
    current = _stamp(current.isoformat())
    base = {"schema": STATUS_SCHEMA, "reference_day": TARGET_DAY,
            "registered_windows": [f"{TARGET_DAY}T{a.isoformat()}+08:00/{b.isoformat()}"
                                   for a, b in WINDOWS], "attempts": 0, "accepted": 0,
            "rejected": 0, "unrecovered_claims": 0, "latest": None, **BOUNDARY}
    try:
        if _code_identity() != LOADED_SOURCES:
            raise ValueError("loaded_source_changed")
        sessions, _ = _calendar(Path(root), current)
        if TARGET_DAY not in sessions:
            raise ValueError("registered_day_not_in_calendar")
        directory = _directory(root)
        packets = []
        for slot in range(len(WINDOWS)):
            path = directory / f"{slot:02}.json"
            claim = directory / f"{slot:02}.claim"
            if _redirected(claim):
                raise ValueError("local_claim_redirected")
            base["unrecovered_claims"] += int(claim.exists())
            if path.exists() or _redirected(path):
                packets.append(read_packet(path, slot, now=current, root=root, current_sessions=sessions))
        if len(packets) > 1 and not packets[0][0]["errors"]:
            raise ValueError("capture_after_first_success")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return {**base, "status": "evidence_unavailable",
                "summary": "本机盘后日历、源码或原件复核未通过；不沿用旧名单。"}
    base["attempts"] = len(packets)
    base["accepted"] = sum(not packet["errors"] for packet, _ in packets)
    base["rejected"] = len(packets) - base["accepted"]
    if packets:
        packet, result = next(((p, r) for p, r in packets if not p["errors"]), packets[-1])
        base["latest"] = {"observed_at": packet["observed_at"], "packet_id": packet["packet_id"],
                          "errors": packet["errors"], "boards": result["boards"] if result else None,
                          "source_as_of": result["source_as_of"] if result else None,
                          "source_request_errors": _request_diagnostics(packet["snapshot"])}
    if base["accepted"]:
        return {**base, "status": "local_complete", "summary":
                "本机盘后供应商板块原件通过完整性核验；尚无全A成员、主流、成交收益或预测证据，云备份未核。"}
    end = _stamp(f"{TARGET_DAY}T16:20:00+08:00")
    if current >= end:
        status = "rejected" if packets else "expired_missing"
    else:
        status = "rejected" if packets else "scheduled"
    return {**base, "status": status, "summary":
            f"本机盘后研究登记{TARGET_DAY} 16:00/16:15上海时间，最多两次；"
            f"已取{len(packets)}次、通过0次。未取得有效盘后输入；错过窗口不补写，不生成买卖提示。"}


def _fetch(root):
    return MarketIntelligence(None, Path(root) / "unused_local_close_cache",
                              lambda _: ([], {})).market_sectors(force=True)


def run_once(*, root=PROJECT_ROOT, now=None, fetch=None):
    if os.environ.get("REGIS_EXECUTION_ROLE") != "local":
        return {"status": "local_role_required", **BOUNDARY}
    now = now or (lambda: datetime.now(SH_TZ))
    started = _stamp(now().isoformat())
    status = current_status(root=root, now=started)
    if status["status"] in {"evidence_unavailable", "local_complete"}:
        return status
    slot = window(started)
    if slot is None:
        return status
    directory = _directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    path, claim = directory / f"{slot:02}.json", directory / f"{slot:02}.claim"
    if path.exists() or _redirected(path):
        return {"status": "already_attempted", "window_index": slot, **BOUNDARY}
    try:
        with claim.open("x", encoding="utf-8") as stream:
            stream.write(started.isoformat())
    except FileExistsError:
        return {"status": "claim_preserved", "window_index": slot, **BOUNDARY}
    # An interrupted claim is retained, never converted into a later first write.
    calendar_sha, calendar_bundle = _freeze_calendar(root, started)
    error_type = None
    try:
        snapshot = (fetch or (lambda: _fetch(root)))()
        raw = _bytes(snapshot)
        if len(raw) > MAX_PACKET_BYTES - len(_bytes(calendar_bundle)) - 16_000:
            raise ValueError("source_packet_too_large")
    except Exception as error:
        snapshot, error_type = None, type(error).__name__
    observed = _stamp(now().isoformat())
    errors, _ = _assessment(snapshot, started, observed, slot)
    if error_type:
        errors = ["source_request_failed"]
    if _code_identity() != LOADED_SOURCES:
        # Preserve the claim rather than assert a new on-disk implementation ran.
        return {"status": "loaded_source_changed_claim_preserved", **BOUNDARY}
    packet = {"schema": SCHEMA, "reference_day": TARGET_DAY, "window_index": slot,
              "started_at": started.isoformat(), "observed_at": observed.isoformat(),
              "source_contract": LOADED_SOURCES, "calendar_sha256": calendar_sha,
              "calendar_bundle": calendar_bundle,
              "snapshot": snapshot, "source_error_type": error_type, "errors": errors, **BOUNDARY}
    packet["packet_id"] = hashlib.sha256(_bytes(packet)).hexdigest()
    temporary = None
    try:
        packet_raw = _bytes(packet)
        if len(packet_raw) > MAX_PACKET_BYTES:
            raise ValueError("local_packet_too_large")
        with tempfile.NamedTemporaryFile(dir=directory, prefix=f".{slot:02}-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(packet_raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        claim.unlink()
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"status": "accepted" if not errors else "rejected",
            "window_index": slot, "packet_id": packet["packet_id"], "errors": errors, **BOUNDARY}


def main():
    parser = argparse.ArgumentParser(description="Finite local after-close source capture")
    parser.add_argument("mode", choices=("once", "status", "loop"))
    args = parser.parse_args()
    if args.mode == "status":
        print(json.dumps(current_status(), ensure_ascii=False))
    elif args.mode == "once":
        print(json.dumps(run_once(), ensure_ascii=False))
    else:
        last_slot = None
        while datetime.now(SH_TZ) < _stamp(f"{TARGET_DAY}T16:20:00+08:00"):
            slot = window(datetime.now(SH_TZ))
            if slot is not None and slot != last_slot:
                result = run_once()
                print(json.dumps(result, ensure_ascii=False), flush=True)
                last_slot = slot
                if result.get("status") in {"accepted", "local_complete", "evidence_unavailable"}:
                    break
            clock.sleep(10)
        print(json.dumps(current_status(), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
