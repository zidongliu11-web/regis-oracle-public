"""After-close constituent receipts for preparation, never historical membership.

Keep the actual retrieval clock and the quoted session separate. Reuse the
existing pagination, quality checks and descriptive metrics, not a new selector.
"""
from datetime import date, datetime, time, timedelta
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import time as monotonic_time
from uuid import uuid4

from . import cloud_sector_close as close
from . import cloud_sector_constituents as members
from .cloud_research_worker import ROOT, read_json, publish
from .market_state_daily import _atomic_publish, _canonical_bytes, _sha256
from .next_session_sectors import session_dates, _preparation_pool, _market_cohort

SCHEMA = "cloud_sector_close_members.v1"
BOUNDARY = {**close.BOUNDARY, "historical_membership_verified": False,
            "production_eligible": False, "suggested_shares": None}
NOTE = "成员属于实际获取时点，行情属于所标注交易日；不是历史成分还原、盘中同刻证据或交易所最终收盘认证。成交额前三仅为核心观察，不是龙头或买入名单。"


def contract():
    root = Path(__file__).parent
    return {**close.contract(), **members.contract(), **{name: _sha256(root / name) for name in (
        "cloud_sector_close_members.py", "next_session_sectors.py")}}


def parent(root, day, now):
    packet, assessment = close.read_packet(root, day, now)
    pool = _preparation_pool(packet["snapshot"], assessment["rows"])
    sectors = [{key: row[key] for key in ("kind", "code", "name")}
               for row in pool if not _market_cohort(row)][:3]
    return packet, sectors


def assess(sector, pages, day, retrieved_at):
    # The frozen intraday checker compares every quote with the latest source
    # clock. This is a consistency check, NOT a substituted retrieval time.
    parsed = members._fetch_sector_constituents(sector, lambda fs, page: pages[page-1] if page <= len(pages) else None,
                                               capture_date=date.fromisoformat(day))
    clocks = [close._stamp(row["data_as_of"]) for row in parsed["rows"] if row.get("data_as_of")]
    ceiling = max(clocks) if clocks else retrieved_at
    result = members.assess(sector, pages, ceiling)
    errors = set(result["errors"]) | set(parsed["errors"])
    if (retrieved_at.tzinfo is None or not clocks or len(clocks) != len(parsed["rows"])
            or any(stamp.date().isoformat() != day or stamp.time() < time(15)
                   or stamp > retrieved_at for stamp in clocks)):
        errors.add("not_reference_after_close")
    result["errors"] = sorted(errors)
    if errors:
        result["metrics"] = None
    return result


def _load(root, day, now, *, allow_retired_source=False):
    board, sectors = parent(root, day, now)
    directory = root / "reports/cloud_sector_close_members" / day / board["packet_id"]
    pointer = directory / "latest.json"
    if any(path.is_symlink() for path in (directory, directory.parent, directory.parent.parent, pointer)):
        raise ValueError("member_receipt_symlink")
    reference = read_json(pointer)
    filename = reference["file"]
    if not isinstance(filename, str) or not filename.startswith("snapshot.") or Path(filename).name != filename:
        raise ValueError("member_receipt_path_invalid")
    path = directory / filename
    if path.is_symlink() or _sha256(path) != reference["sha256"]:
        raise ValueError("member_receipt_hash_invalid")
    packet = read_json(path)
    identity = dict(packet)
    packet_id = identity.pop("packet_id", None)
    if (hashlib.sha256(_canonical_bytes(identity)).hexdigest() != packet_id
            or packet.get("schema") != SCHEMA
            or not close._source_contract_valid(packet.get("source_contract"), contract(), allow_retired=allow_retired_source)
            or packet.get("reference_day") != day or packet.get("board_packet_id") != board["packet_id"]
            or any(packet.get(key) != value for key, value in BOUNDARY.items())
            or [row["sector"] for row in packet["captures"]] != sectors):
        raise ValueError("member_receipt_identity_invalid")
    start, finish = close._stamp(packet["started_at"]), close._stamp(packet["observed_at"])
    if (not close._stamp(board["observed_at"]) <= start <= finish <= now
            or (finish-start).total_seconds() > 90):
        raise ValueError("member_receipt_clock_invalid")
    return packet


def read_packet(root, day, now):
    packet = _load(root, day, now)
    packet['assessments'] = [assess(row['sector'], row['pages'], day, close._stamp(packet['observed_at']))
                             for row in packet['captures']]
    return packet


def _result(packet):
    rows = packet["assessments"]
    return {"status": "complete" if all(not row["errors"] for row in rows) else "waiting_inputs",
            "reference_day": packet["reference_day"], "observed_at": packet["observed_at"],
            "board_packet_id": packet["board_packet_id"], "packet_id": packet["packet_id"],
            "requested_sectors": len(rows), "accepted_sectors": sum(not row["errors"] for row in rows),
            "summary": NOTE, **BOUNDARY}


def run(*, root=ROOT, now=None, fetch_page=None):
    if os.environ.get("REGIS_EXECUTION_ROLE") != "cloud":
        return {"status": "local_unchanged", **BOUNDARY}
    now = now or (lambda: datetime.now(close.SH_TZ))
    start = close._stamp(now().isoformat())
    if start.weekday() < 5 and time(9, 30) <= start.time() < time(16):
        return {"status": "scheduled", **BOUNDARY}
    try:
        day, _ = session_dates(root, start)
        board, sectors = parent(root, day, start)
    except (OSError, ValueError, TypeError, KeyError):
        return {"status": "waiting_board", "summary": "等待上一完成交易日的有效盘后板块原件；不借用旧名单。", **BOUNDARY}
    if shutil.disk_usage(root).free < 2*1024**3:
        return {"status": "waiting_storage", **BOUNDARY}
    directory = root / "reports/cloud_sector_close_members" / day / board["packet_id"]
    if any(path.is_symlink() for path in (directory, directory.parent, directory.parent.parent, directory / ".lock", directory / "latest.json")):
        return {"status": "invalid_receipt", **BOUNDARY}
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "owned_elsewhere", **BOUNDARY}
        supersedes = None
        if (directory / "latest.json").exists():
            try:
                previous = _load(root, day, start, allow_retired_source=True)
                if previous['source_contract'] != contract():
                    supersedes = previous['packet_id']
                else:
                    result = _result(read_packet(root, day, start))
                    if result["status"] == "complete" or start < close._stamp(previous["observed_at"])+timedelta(minutes=15):
                        return result
            except (OSError, ValueError, KeyError, TypeError):
                return {"status": "invalid_receipt", **BOUNDARY}
        captures = []
        deadline = monotonic_time.monotonic()+40
        for sector in sectors:
            pages = []
            def fetch(fs, page):
                try:
                    raw = (fetch_page or members.fetch_constituent_page)(fs, page) if monotonic_time.monotonic() < deadline else None
                    _canonical_bytes(raw)
                except Exception:
                    raw = None  # Retain the failed page, never upstream secrets or fake empty membership.
                pages.append(raw)
                return raw
            members._fetch_sector_constituents(sector, fetch, capture_date=date.fromisoformat(day))
            captures.append({"sector": sector, "pages": pages})
        finish = close._stamp(now().isoformat())
        if (not 0 <= (finish-start).total_seconds() <= 90 or session_dates(root, finish)[0] != day
                or (finish.weekday() < 5 and time(9, 30) <= finish.time() < time(16))
                or parent(root, day, finish)[0]["packet_id"] != board["packet_id"]):
            return {"status": "inputs_or_clock_changed", **BOUNDARY}
        packet = {"schema": SCHEMA, "reference_day": day, "started_at": start.isoformat(),
                  "observed_at": finish.isoformat(), "source_contract": contract(),
                  "board_packet_id": board["packet_id"], "captures": captures, **BOUNDARY}
        if supersedes:
            packet['supersedes_packet_id'] = supersedes
        packet["packet_id"] = hashlib.sha256(_canonical_bytes(packet)).hexdigest()
        path = directory / f"snapshot.{uuid4().hex}.json"
        _atomic_publish(path, _canonical_bytes(packet))
        publish(directory / "latest.json", {"file": path.name, "sha256": _sha256(path)})
        return _result(read_packet(root, day, finish))
