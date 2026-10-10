"""Constituent breadth behind the independent board observations, not orders.

Reuse the supplier's code-ordered pagination. Preserve pages and recompute all
display metrics on read; a board ranking alone cannot identify a leader.
"""
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
from statistics import median
import time

from . import cloud_sector_observations as boards
from .cloud_research_worker import ROOT, read_json
from .market_sector_constituent_forward import _fetch_sector_constituents, fetch_constituent_page
from .market_state_daily import _atomic_publish, _canonical_bytes, _sha256

SCHEMA = "cloud_sector_constituents.v2"
BOUNDARY = boards.BOUNDARY


def contract():
    return {**boards.contract(), **{name: _sha256(Path(__file__).parent / name) for name in (
        "cloud_sector_constituents.py", "market_sector_constituent_forward.py", "next_session_sectors.py",
    )}}


def _source_contract_matches(actual):
    current = contract()
    if actual == current:
        return True
    # One reviewed path-only transition. Future code changes cannot inherit
    # compatibility merely because these old file hashes still appear here.
    root = Path(__file__).resolve().parents[1]
    path = root / "research/CLOUD_SECTOR_CONSTITUENT_PATH_COMPATIBILITY_V1.json"
    keys = {"cloud_sector_constituents.py", "market_sector_constituent_forward.py"}
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            return False
        rule = json.loads(path.read_text(encoding="utf-8"))
        return (rule["scope"] == "unchanged_constituent_observation_with_trusted_path_repair"
                and set(rule["current"]) == set(rule["previous"]) == keys
                and rule["current"] == {key: current.get(key) for key in keys}
                and actual == {**current, **rule["previous"]})
    except (OSError, ValueError, TypeError, KeyError):
        return False


def assess(sector, pages, observed):
    """Metrics at one board/observation grain, including unchanged constituents."""
    parsed = _fetch_sector_constituents(sector, lambda fs, page: pages[page-1] if page <= len(pages) else None,
                                        capture_date=observed.date())
    errors = list(parsed["errors"])
    if len(pages) != len(parsed["pagination"]["page_counts"]):
        # A failed request is stored as None and is an explicit attempted page.
        if not (pages and pages[-1] is None and len(pages) == len(parsed["pagination"]["page_counts"])+1):
            errors.append("page_sequence_invalid")
    for page in pages:
        batch = page.get("diff") if isinstance(page, dict) else None
        for raw in batch if isinstance(batch, list) else []:
            if isinstance(raw, dict) and any(isinstance(raw.get(k), bool) for k in (
                    "f2", "f3", "f5", "f6", "f8", "f20", "f21", "f124")):
                errors.append("boolean_numeric_field")
    for row in parsed["rows"]:
        if row["price"] is None or row["price"] <= 0 or any(
                row[k] is None or row[k] < 0 for k in ("volume", "amount", "turnover_rate", "total_market_cap", "float_market_cap")):
            errors.append("constituent_numeric_domain")
        try:
            stamp = datetime.fromisoformat(row["data_as_of"])
            if (stamp.tzinfo is None or stamp.astimezone(boards.SH_TZ).date() != observed.date()
                    or not 0 <= (observed-stamp).total_seconds() <= 180):
                raise ValueError("stale")
        except (ValueError, TypeError):
            errors.append("constituent_time_invalid")
    rows = parsed["rows"]
    if not rows:
        errors.append("eligible_constituents_empty")
    result = {"sector": parsed["sector"], "reported_total": parsed["reported_total"],
              "fetched_count": parsed["fetched_count"],
              "excluded_future_listing_count": parsed["excluded_future_listing_count"],
              "errors": sorted(set(errors)), "metrics": None}
    if errors:
        return result
    ranked = sorted(rows, key=lambda r: (-r["amount"], r["stock_code"]))
    core = ranked[:3]
    others = ranked[3:]
    amount = sum(r["amount"] for r in rows)
    if not math.isfinite(amount):
        result["errors"] = ["aggregate_not_finite"]
        return result
    up, down = sum(r["change_pct"] > 0 for r in rows), sum(r["change_pct"] < 0 for r in rows)
    result["metrics"] = {
        "count": len(rows), "up": up, "down": down, "flat": len(rows)-up-down,
        "advance_pct": up/len(rows)*100, "median_change_pct": median(r["change_pct"] for r in rows),
        "top_three_amount_share_pct": sum(r["amount"] for r in core)/amount*100 if amount > 0 else None,
        "ex_core_advance_pct": sum(r["change_pct"] > 0 for r in others)/len(others)*100 if others and amount > 0 else None,
        "core_observations": [{key: row[key] for key in ("stock_code", "stock_name", "change_pct", "data_as_of")} for row in core] if amount > 0 else [],
        "source_as_of_min": min(r["data_as_of"] for r in rows),
        "source_as_of_max": max(r["data_as_of"] for r in rows),
    }
    if any(not math.isfinite(value) for value in result["metrics"].values() if type(value) in (int, float)):
        result.update(errors=["aggregate_not_finite"], metrics=None)
    return result


def parent(root, day, index):
    from .next_session_sectors import _preparation_pool, _market_cohort
    packet = boards.read_packet(root / "reports/cloud_sector_observations" / day / f"{index:02}.json", day)
    if packet["errors"]:
        raise ValueError("board_observation_rejected")
    adapted = boards.inspect(packet["snapshot"], datetime.fromisoformat(packet["observed_at"]))[1]
    # Preserve the frozen parent ranking. This independent research collector
    # follows actual themes; yesterday's limit cohorts cannot consume all slots.
    pool = _preparation_pool(packet["snapshot"], adapted["observed_sectors"])
    themes = [row for row in pool if not _market_cohort(row)][:3]
    return packet, [{k: r[k] for k in ("kind", "code", "name")} for r in themes]


def read_packet(path, *, root, day):
    if path.is_symlink():
        raise ValueError("constituent_symlink")
    packet = read_json(path)
    identity = dict(packet)
    packet_id = identity.pop("packet_id", None)
    if (hashlib.sha256(_canonical_bytes(identity)).hexdigest() != packet_id
            or packet.get("schema") != SCHEMA or not _source_contract_matches(packet.get("source_contract"))
            or any(packet.get(k) != v for k, v in BOUNDARY.items())):
        raise ValueError("constituent_identity_invalid")
    start, finish = (datetime.fromisoformat(packet[k]) for k in ("started_at", "observed_at"))
    index = packet["bucket"]
    if (type(index) is not int or start.tzinfo is None or finish.tzinfo is None
            or start.astimezone(boards.SH_TZ).date().isoformat() != day
            or finish.astimezone(boards.SH_TZ).date().isoformat() != day
            or not 0 <= (finish-start).total_seconds() <= 90 or boards.bucket(start) != index
            or boards.bucket(finish) != index or path.name != f"{index:02}.json"):
        raise ValueError("constituent_clock_invalid")
    board, sectors = parent(root, day, index)
    if (board["packet_id"] != packet.get("board_packet_id")
            or not 0 <= (start-datetime.fromisoformat(board["observed_at"])).total_seconds() <= 180
            or [row.get("sector") for row in packet["captures"]] != sectors):
        raise ValueError("constituent_board_binding_invalid")
    packet["assessments"] = [assess(row["sector"], row["pages"], finish) for row in packet["captures"]]
    return packet


def run(*, root=ROOT, now=None, fetch_page=None):
    if os.environ.get("REGIS_EXECUTION_ROLE") != "cloud":
        return {"status": "local_unchanged", **BOUNDARY}
    now = now or (lambda: datetime.now(boards.SH_TZ))
    start = now()
    index = boards.bucket(start)
    start = start.astimezone(boards.SH_TZ)
    base = {"implementation_sha256": _sha256(Path(__file__)), **BOUNDARY}
    if index is None:
        return {**base, "status": "scheduled", "summary": "等待盘中同桶板块原件后独立采集成分，不等待情绪、策略或账户准入。"}
    day = start.date().isoformat()
    try:
        board, sectors = parent(root, day, index)
        if not 0 <= (start-datetime.fromisoformat(board["observed_at"])).total_seconds() <= 180:
            raise ValueError("board_not_fresh")
    except (OSError, ValueError, KeyError, TypeError):
        return {**base, "status": "waiting_inputs", "summary": "当时观察桶没有有效板块原件，不补录过去的成分行情。"}
    if shutil.disk_usage(root).free < 2*1024**3:
        return {**base, "status": "waiting_inputs", "summary": "存储不足，保留已有原件。"}
    directory = root / "reports/cloud_sector_constituents" / day
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**base, "status": "owned_elsewhere"}
        path = directory / f"{index:02}.json"
        if not path.exists():
            deadline = time.monotonic()+40
            captures = []
            for sector in sectors:
                pages = []
                def fetch(fs, page):
                    try:
                        raw = (fetch_page or fetch_constituent_page)(fs, page) if time.monotonic() < deadline else None
                    except Exception:
                        raw = None  # No raw upstream exception/credentials in runtime packets.
                    pages.append(raw)
                    return raw
                _fetch_sector_constituents(sector, fetch, capture_date=start.date())
                captures.append({"sector": sector, "pages": pages})
            finish = now()
            if (finish.tzinfo is None or finish.astimezone(boards.SH_TZ).date() != start.date()
                    or boards.bucket(finish) != index or not 0 <= (finish-start).total_seconds() <= 90):
                return {**base, "status": "waiting_inputs", "summary": "请求跨越观察桶或时限，不把晚到的成分冒充当时证据。"}
            finish = finish.astimezone(boards.SH_TZ)
            packet = {"schema": SCHEMA, "source_contract": contract(), "bucket": index,
                      "started_at": start.isoformat(), "observed_at": finish.isoformat(),
                      "board_packet_id": board["packet_id"], "captures": captures, **BOUNDARY}
            packet["packet_id"] = hashlib.sha256(_canonical_bytes(packet)).hexdigest()
            _atomic_publish(path, _canonical_bytes(packet))
        packet = read_packet(path, root=root, day=day)
        accepted = sum(not r["errors"] for r in packet["assessments"])
        return {**base, "status": "complete" if accepted == len(sectors) else "waiting_inputs",
                "requested_sectors": len(sectors), "accepted_sectors": accepted, "observed_at": packet["observed_at"],
                "summary": f"成分证据完整{accepted}/{len(sectors)}个观察板块；只解释扩散与集中度，不授予买卖权限。"}


def observation(*, root=ROOT, now=None):
    now = (now or datetime.now(boards.SH_TZ)).astimezone(boards.SH_TZ)
    day = now.date().isoformat()
    empty = {"status": "unavailable", "rows": [], "summary": "同日成分证据尚未就绪；不能判断板块是普涨还是少数股票拉动。", **BOUNDARY}
    try:
        # Latest board wins: a failed/missing new constituent attempt clears old success.
        paths = sorted((root / "reports/cloud_sector_observations" / day).glob("[0-4][0-9].json"))
        if not paths:
            return empty
        packet = read_packet(root / "reports/cloud_sector_constituents" / day / paths[-1].name, root=root, day=day)
        age = (now-datetime.fromisoformat(packet["observed_at"])).total_seconds()
        if age < 0:
            return empty
        rows = packet["assessments"]
        return {**BOUNDARY, "status": "observed" if all(not r["errors"] for r in rows) else "partial",
                "rows": rows, "observed_at": packet["observed_at"], "board_packet_id": packet["board_packet_id"],
                "historical_only": age > 180 or boards.bucket(now) is None,
                "summary": "逐页完整性与同日180秒时效核验；上涨占比分母包含平盘，核心观察按当时成交额前3定义，非龙头认定或买点。"}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return empty
