"""Observe industry membership by capture day, independently of bar backfill.

Bulk BaoStock pagination can stop at 2,000 rows with error_code still zero.
Use bounded, resumable single-security requests and keep their original times.
These observations are not historical membership or intraday sector confirmation.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import fcntl
import os
from pathlib import Path
import shutil
from uuid import uuid4

from research.download_v4_daily_baostock import ALLOWED_PREFIXES
from .cloud_pattern_inputs import SCHEMA as RECEIPT_SCHEMA, _source_universe, assess_receipt, provider_deadline, query_receipt, sha, valid_day
from .cloud_research_worker import ROOT, publish, read_json

SCHEMA = "cloud_industry_daily.v1"
SHANGHAI = timezone(timedelta(hours=8))
IMPLEMENTATION_SHA256 = sha(Path(__file__))
BOUNDARY = {"production_eligible": False, "generates_alert": False,
            "historical_membership_backfilled": False, "intraday_sector_confirmation": False}


def context(root, day):
    target, master_sha, _ = _source_universe(root)
    codes = []
    with (root / "research/data/stock_basic_baostock.csv").open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if (row["type"] == "1" and row["code"].startswith(ALLOWED_PREFIXES)
                    and row["ipo_date"] <= day and (not row["out_date"] or row["out_date"] >= day)):
                codes.append(row["code"])
    if not codes or len(codes) != len(set(codes)) or not valid_day(day):
        raise ValueError("industry_population_invalid")
    return target, master_sha, sorted(codes)


def inspect_receipt(payload, *, code, day, now):
    start, finish = (datetime.fromisoformat(payload[key]) for key in ("observed_at", "completed_at"))
    request = {"endpoint": "query_stock_industry", "code": code, "date": "", "observation_date": day}
    if (payload.get("schema") != RECEIPT_SCHEMA or payload.get("request") != request
            or start.tzinfo is None or finish.tzinfo is None or not start <= finish <= now
            or start.astimezone(SHANGHAI).date().isoformat() != day
            or finish.astimezone(SHANGHAI).date().isoformat() != day):
        raise ValueError("industry_receipt_date_or_request_mismatch")
    assessment = assess_receipt(payload)
    if assessment != payload.get("assessment"):
        raise ValueError("industry_receipt_assessment_mismatch")
    known = False
    if assessment == "captured_not_validated":
        if len(payload["rows"]) != 1:
            raise ValueError("industry_receipt_row_count_invalid")
        row = dict(zip(payload["fields"], payload["rows"][0]))
        known = bool(row["industry"].strip() and row["industryClassification"].strip())
    return assessment, known


def bound_receipt(root, entry, directory):
    path = root / entry["path"]
    if (path.is_symlink() or path.parent.resolve() != directory.resolve()
            or not path.is_file() or sha(path) != entry.get("sha256")):
        raise ValueError("industry_receipt_file_mismatch")
    return path


def load_entries(root, day, *, now):
    """Load same-day raw receipts only; partial coverage never implies a sector."""
    directory = root / "reports/cloud_industry_daily" / day
    path = directory / "manifest.json"
    if not path.exists():
        return {}
    state = read_json(path)
    _, master_sha, codes = context(root, day)
    entries = state.get("entries")
    if (state.get("schema") != SCHEMA or state.get("observation_date") != day
            or state.get("master_sha256") != master_sha or state.get("target") != len(codes)
            or not isinstance(entries, dict) or not set(entries) <= set(codes)):
        raise ValueError("industry_daily_manifest_mismatch")
    usable = {}
    for code, entry in entries.items():
        payload = read_json(bound_receipt(root, entry, directory / "receipts"))
        assessment, known = inspect_receipt(payload, code=code, day=day, now=now)
        if entry.get("assessment") != assessment or entry.get("classification_known") is not known:
            raise ValueError("industry_daily_entry_mismatch")
        if assessment == "captured_not_validated":
            usable[code] = entry
    return usable


def run(*, root=ROOT, batch_limit=200, client=None, now=None):
    if os.environ.get("REGIS_EXECUTION_ROLE") != "cloud":
        return {"status": "local_unchanged", "summary": "逐日行业采集只在云端执行，不改开发电脑数据。", **BOUNDARY}
    if type(batch_limit) is not int or not 1 <= batch_limit <= 500:
        raise ValueError("industry_batch_limit_invalid")
    now = now or (lambda: datetime.now(timezone.utc))
    current = now()
    if current.tzinfo is None:
        raise ValueError("industry_observation_timezone_missing")
    day = current.astimezone(SHANGHAI).date().isoformat()
    try:
        daily_target, master_sha, codes = context(root, day)
    except (OSError, ValueError, KeyError) as exc:
        return {"status": "waiting_inputs", "reason": str(exc), "summary": "证券范围未核验，未开始逐日行业采集。", **BOUNDARY}
    if shutil.disk_usage(root).free < 2*1024**3:
        return {"status": "waiting_storage", "summary": "剩余空间不足2GB，保留已有行业原件。", **BOUNDARY}
    directory = root / "reports/cloud_industry_daily" / day
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "owned_elsewhere", "summary": "同日行业采集已有进程，不重复查询。", **BOUNDARY}
        state_path = directory / "manifest.json"
        state = read_json(state_path)
        if state_path.exists():
            load_entries(root, day, now=current)  # Validate failed as well as successful receipts.
        else:
            state = {"schema": SCHEMA, "observation_date": day, "master_sha256": master_sha,
                     "target": len(codes), "entries": {}}
        receipts = directory / "receipts"
        receipts.mkdir(exist_ok=True)

        def save(status=None):
            if sha(root / "research/data/stock_basic_baostock.csv") != master_sha:
                raise ValueError("industry_master_changed_during_capture")
            publish(state_path, state)
            entries = list(state["entries"].values())
            captured = sum(e["assessment"] == "captured_not_validated" for e in entries)
            classified = sum(e["classification_known"] for e in entries)
            report = {"schema": SCHEMA, "observation_date": day, "target": len(codes),
                "status": status or ("complete" if classified == len(codes) else "collecting"),
                "captured": captured, "classified": classified, "adopted": sum("adopted_from" in e for e in entries),
                "implementation_sha256": IMPLEMENTATION_SHA256, "job_run_id": os.environ.get("REGIS_RESEARCH_JOB_ID"),
                "universe_scope": "master_listed_shsz_main_chinext_on_observation_day", "full_a_share_coverage": False,
                "summary": f"{day}行业原件 {captured}/{len(codes)}，归属非空 {classified}；按实际采集日续传，与日线补齐日期分开。同日收盘后才能关联价格，不是盘中主线确认或策略准入。",
                **BOUNDARY}
            publish(root / "reports/cloud_industry_daily_progress.json", report)
            return report

        def store(code, payload, path, *, adopted_from=None):
            assessment, known = inspect_receipt(payload, code=code, day=day, now=now())
            attempts = state["entries"].get(code, {}).get("attempts", 0) + (0 if adopted_from else 1)
            state["entries"][code] = {"path": str(path.relative_to(root)), "sha256": sha(path),
                "assessment": assessment, "classification_known": known, "attempts": attempts,
                "observed_at": payload["observed_at"],
                "retry_after": (now()+timedelta(minutes=min(60, 2**min(attempts, 6)))).isoformat(),
                **({"adopted_from": adopted_from} if adopted_from else {})}

        # Reuse today's already-observed receipts, byte for byte. Their older
        # bar-target directory is not their observation date. Never relabel it.
        legacy_dir = root / "reports/cloud_pattern_inputs" / daily_target
        legacy = read_json(legacy_dir / "manifest.json")
        if (legacy.get("schema") == RECEIPT_SCHEMA and legacy.get("master_sha256") == master_sha
                and legacy.get("target_date") == daily_target):
            for code in codes:
                source = (legacy.get("industries") or {}).get(code)
                if code in state["entries"] or not source:
                    continue
                path = bound_receipt(root, source, legacy_dir / "receipts")
                payload = read_json(path)
                if (payload.get("request") or {}).get("observation_date") != day:
                    continue
                inspect_receipt(payload, code=code, day=day, now=now())
                copied = receipts / (code+"."+uuid4().hex+".json")
                shutil.copyfile(path, copied)
                if sha(copied) != source["sha256"]:
                    raise ValueError("industry_adoption_copy_mismatch")
                store(code, payload, copied, adopted_from=source)
            save()
        pending = [code for code in codes if not state["entries"].get(code)
                   or (not state["entries"][code]["classification_known"]
                       and now() >= datetime.fromisoformat(state["entries"][code]["retry_after"]))]
        pending.sort(key=lambda code: (state["entries"].get(code, {}).get("observed_at", ""), code))
        if not pending:
            return save("retry_pending" if len(state["entries"]) < len(codes) or any(not e["classification_known"] for e in state["entries"].values()) else None)
        if state.get("login_retry_after") and now() < datetime.fromisoformat(state["login_retry_after"]):
            return save("retry_pending")
        if client is None:
            import baostock as client
        logged_in = False
        try:
            with provider_deadline() as deadline:
                login = client.login()
            if deadline["expired"] or login.error_code != "0":
                state["login_retry_after"] = (now()+timedelta(minutes=2)).isoformat()
                return save("retry_pending")
            logged_in = True
            for code in pending[:batch_limit]:
                if now().astimezone(SHANGHAI).date().isoformat() != day:
                    return save("date_rolled_over")
                payload = query_receipt(client, {"endpoint": "query_stock_industry", "code": code,
                    "date": "", "observation_date": day}, now)
                path = receipts / (code+"."+uuid4().hex+".json")
                publish(path, payload)
                # A query crossing midnight is kept as raw evidence, not adopted.
                store(code, payload, path)
                save()
                if payload["assessment"] in {"provider_error", "pagination_unverified"}:
                    return save("retry_pending")
        finally:
            if logged_in:
                with provider_deadline():
                    client.logout()
        return save()
