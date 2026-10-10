"""Freshly retrieved after-close boards for preparation, never intraday PIT."""
from datetime import date, datetime, time, timedelta
import fcntl
import hashlib
import math
import os
from pathlib import Path
import shutil
from uuid import uuid4

from .cloud_research_worker import ROOT, publish, read_json
from .market_intelligence import MarketIntelligence
from .market_sector_strategy_adapter import SH_TZ, _sector_rows, protocol_identity_valid
from .market_state_daily import _atomic_publish, _canonical_bytes, _sha256

SCHEMA = "cloud_sector_close.v1"
BOUNDARY = {"research_only": True, "historical_only": True, "generates_alert": False,
            "changes_trade_authority": False, "intraday_confirmation": False,
            "forward_sample": False, "returns_read": False}
IMPLEMENTATION_SHA256 = _sha256(Path(__file__))


def contract():
    return {name: _sha256(Path(__file__).parent / name) for name in (
        "cloud_sector_close.py", "market_intelligence.py", "market_sector_strategy_adapter.py")}


def _source_contract_valid(actual, expected, *, allow_retired=False):
    # A retired identity permits a NEW network capture only. Readers still
    # require the exact current source; old observations are never re-labelled.
    return (isinstance(actual, dict) and actual.keys() == expected.keys()
            and all(isinstance(value, str) and len(value) == 64
                    and not set(value)-set('0123456789abcdef') for value in actual.values())
            and (actual == expected or allow_retired))


def _stamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("timezone_required")
    return stamp.astimezone(SH_TZ)


def assess(snapshot, day, observed):
    """Keep supplier and retrieval clocks separate; reuse frozen observation rules."""
    reference = date.fromisoformat(day)
    if observed.tzinfo is None or not protocol_identity_valid():
        raise ValueError("clock_or_protocol_invalid")
    if (not isinstance(snapshot, dict) or snapshot.get("status") not in {"ok", "degraded"}
            or snapshot.get("health_status") not in {"verified", "delayed"} or snapshot.get("error")
            or not isinstance(snapshot.get("source"), str) or not snapshot["source"].strip()):
        raise ValueError("source_unavailable")
    fetched = _stamp(snapshot["fetched_at"])
    if snapshot.get("served_from_cache") is not False or not 0 <= (observed-fetched).total_seconds() <= 120:
        raise ValueError("retrieval_not_fresh")
    meta, stamps = snapshot["meta"], []
    if not isinstance(meta, dict) or any(meta.get(key) is not True for key in (
            "coverage_complete", "industry_complete", "concept_complete", "timestamp_complete", "breadth_count_complete")):
        raise ValueError("coverage_incomplete")
    for key, count in (("industries", "industry_reported_total"), ("concepts", "concept_reported_total")):
        rows = snapshot[key]
        if (not isinstance(rows, list) or type(meta.get(count)) is not int
                or not 0 < meta[count] <= 10000 or len(rows) != meta[count]):
            raise ValueError("coverage_incomplete")
        seen = set()
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("code"), str)
                    or not row["code"].strip() or row["code"] in seen
                    or not isinstance(row.get("name"), str) or not row["name"].strip()):
                raise ValueError("sector_identity_invalid")
            seen.add(row["code"])
            if (type(row.get("change_pct")) not in (int, float) or not math.isfinite(row["change_pct"])
                    or any(type(row.get(k)) is not int or row[k] < 0 for k in ("up_count", "down_count"))):
                raise ValueError("sector_values_invalid")
            stamp = _stamp(row["data_as_of"])
            if stamp.date() != reference or stamp.time() < time(15) or stamp > fetched:
                raise ValueError("not_reference_after_close")
            stamps.append(stamp)
    if ((max(stamps)-min(stamps)).total_seconds() > 180
            or _stamp(snapshot["data_as_of"]) != min(stamps)
            or _stamp(snapshot["latest_data_as_of"]) != max(stamps)):
        raise ValueError("source_clocks_inconsistent")
    rows, _ = _sector_rows(snapshot)
    eligible = [r for r in rows if r["constituent_count"] >= 5 and r["advance_coverage_pct"] is not None]
    for row in eligible:
        values = [r["change_pct"] for r in eligible if r["kind"] == row["kind"]]
        row["change_rank_pct"] = round(100*(sum(v < row["change_pct"] for v in values)
                                                + .5*sum(v == row["change_pct"] for v in values))/len(values), 3)
    selected = [r for r in eligible if r["change_pct"] > 0 and r["advance_coverage_pct"] >= 60 and r["change_rank_pct"] >= 90]
    selected.sort(key=lambda r: (-r["change_rank_pct"], -r["advance_coverage_pct"], -r["change_pct"], r["kind"], r["code"]))
    return {"rows": selected[:3], "boards": len(rows), "source_as_of": min(stamps).isoformat()}


def _load(root, day, now, *, allow_retired_source=False):
    if date.fromisoformat(day).isoformat() != day or now.tzinfo is None:
        raise ValueError("reference_invalid")
    directory = root / "reports/cloud_sector_close" / day
    pointer = directory / "latest.json"
    # reports itself is the deployment-managed runtime alias, not a receipt.
    if any(p.is_symlink() for p in (directory, directory.parent, pointer)):
        raise ValueError("receipt_symlink")
    manifest = read_json(pointer)
    filename = manifest["file"]
    if not isinstance(filename, str) or not filename.startswith("snapshot.") or Path(filename).name != filename:
        raise ValueError("receipt_path_invalid")
    path = directory / filename
    if path.is_symlink() or _sha256(path) != manifest["sha256"]:
        raise ValueError("receipt_hash_invalid")
    packet = read_json(path)
    identity = dict(packet)
    packet_id = identity.pop("packet_id", None)
    if (hashlib.sha256(_canonical_bytes(identity)).hexdigest() != packet_id
            or packet.get("schema") != SCHEMA
            or not _source_contract_valid(packet.get("source_contract"), contract(), allow_retired=allow_retired_source)
            or packet.get("reference_day") != day or any(packet.get(k) != v for k, v in BOUNDARY.items())):
        raise ValueError("receipt_identity_invalid")
    if not _stamp(packet["started_at"]) <= _stamp(packet["observed_at"]) <= now:
        raise ValueError("receipt_clock_invalid")
    return packet


def read_packet(root, day, now):
    packet = _load(root, day, now)
    if packet["error"]:
        raise ValueError("last_capture_rejected")
    return packet, assess(packet["snapshot"], day, _stamp(packet["observed_at"]))


def _result(packet, assessment=None):
    return {"status": "complete" if assessment is not None else "waiting_inputs",
            "reference_day": packet["reference_day"], "observed_at": packet["observed_at"],
            "source_as_of": assessment["source_as_of"] if assessment else None,
            "boards": assessment["boards"] if assessment else 0,
            "watch_count": len(assessment["rows"]) if assessment else 0,
            "implementation_sha256": IMPLEMENTATION_SHA256, **BOUNDARY}


def run(*, root=ROOT, now=None, fetch=None):
    if os.environ.get("REGIS_EXECUTION_ROLE") != "cloud":
        return {"status": "local_unchanged", **BOUNDARY}
    now = now or (lambda: datetime.now(SH_TZ))
    start = _stamp(now().isoformat())
    base = {"implementation_sha256": IMPLEMENTATION_SHA256, **BOUNDARY}
    if start.weekday() < 5 and time(9, 30) <= start.time() < time(16):
        return {**base, "status": "scheduled"}
    try:
        from .next_session_sectors import session_dates
        day, _ = session_dates(root, start)
    except (OSError, ValueError, TypeError, KeyError):
        return {**base, "status": "waiting_calendar"}
    if shutil.disk_usage(root).free < 2*1024**3:
        return {**base, "status": "waiting_storage"}
    directory = root / "reports/cloud_sector_close" / day
    if any(p.is_symlink() for p in (directory, directory.parent,
                                   directory / '.lock', directory / 'latest.json')):
        return {**base, "status": "invalid_receipt"}
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**base, "status": "owned_elsewhere"}
        supersedes = None
        if (directory / "latest.json").exists():
            try:
                packet = _load(root, day, start, allow_retired_source=True)
                if packet['source_contract'] != contract():
                    supersedes = packet['packet_id']
                elif not packet["error"]:
                    return _result(packet, assess(packet["snapshot"], day, _stamp(packet["observed_at"])))
                elif start < _stamp(packet["observed_at"])+timedelta(minutes=15):
                    return _result(packet)
            except (OSError, ValueError, KeyError, TypeError):
                return {**base, "status": "invalid_receipt"}
        snapshot, error = {}, None
        try:
            snapshot = fetch() if fetch else MarketIntelligence(None, root / "unused_sector_close", lambda _: ([], {})).market_sectors(force=True)
        except Exception:
            error = "source_request_failed"
        finish = _stamp(now().isoformat())
        if (finish < start or session_dates(root, finish)[0] != day
                or (finish.weekday() < 5 and time(9, 30) <= finish.time() < time(16))):
            return {**base, "status": "clock_changed"}
        assessment = None
        if error is None:
            try:
                assessment = assess(snapshot, day, finish)
                # The provider records whole seconds; the request clock retains microseconds.
                if _stamp(snapshot["fetched_at"]) < start.replace(microsecond=0):
                    raise ValueError("retrieval_precedes_request")
            except (ValueError, KeyError, TypeError):
                error, assessment = "source_checks_failed", None
        packet = {"schema": SCHEMA, "reference_day": day, "started_at": start.isoformat(),
                  "observed_at": finish.isoformat(), "source_contract": contract(), "snapshot": snapshot,
                  "error": error, **BOUNDARY}
        if supersedes:
            packet['supersedes_packet_id'] = supersedes
        try:
            _canonical_bytes(snapshot)
        except (ValueError, TypeError):
            # Retain invalid supplier values as diagnostic text, never valid JSON inputs.
            packet.update(snapshot=None, rejected_snapshot_repr=repr(snapshot), error="snapshot_not_json")
            assessment = None
        packet["packet_id"] = hashlib.sha256(_canonical_bytes(packet)).hexdigest()
        path = directory / f"snapshot.{uuid4().hex}.json"
        _atomic_publish(path, _canonical_bytes(packet))
        publish(directory / "latest.json", {"file": path.name, "sha256": _sha256(path)})
        return _result(packet, assessment)


def health_header(status):
    jobs = status.get("jobs")
    parent = jobs.get("sector_observations") if isinstance(jobs, dict) else None
    job = parent.get("after_close") if isinstance(parent, dict) else None
    states = {"complete", "waiting_inputs", "waiting_calendar", "waiting_storage", "scheduled",
              "owned_elsewhere", "invalid_receipt", "clock_changed", "failed"}
    if (not status.get("online") or not isinstance(job, dict) or not isinstance(job.get("status"), str)
            or job["status"] not in states or job.get("implementation_sha256") != IMPLEMENTATION_SHA256):
        return "unverified"
    result = f"{job['status']};implementation={IMPLEMENTATION_SHA256}"
    if job["status"] != "complete":
        return result
    try:
        day = date.fromisoformat(job["reference_day"]).isoformat()
        source, observed = _stamp(job["source_as_of"]), _stamp(job["observed_at"])
        boards, count = job["boards"], job["watch_count"]
        if (source.date().isoformat() != day or source > observed or type(boards) is not int
                or type(count) is not int or not 0 <= count <= min(3, boards) or not 0 < boards <= 20000):
            raise ValueError("invalid_counts")
        return f"{result};day={day};boards={boards};watch={count};source={source.isoformat()};captured={observed.isoformat()}"
    except (ValueError, KeyError, TypeError):
        return "unverified"
