"""Versioned tail-row repairs from bound provider receipts; never rewrite raw bars."""
import csv
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import io
import math
import os
from pathlib import Path
import shutil
import tempfile

import numpy as np

from .cloud_daily_gap_probe import SCHEMA as PROBE_SCHEMA, IMPLEMENTATION_SHA256 as PROBE_SHA
from .cloud_pattern_inputs import _source_universe, assess_receipt, sha, valid_day
from .cloud_pattern_features import _bound, digest
from .cloud_research_worker import ROOT, read_json, publish
from .market_state_daily import _atomic_publish, _canonical_bytes
from research.download_v4_daily_baostock import FIELDS

SCHEMA = "cloud_daily_revisions.v1"
IMPLEMENTATION_SHA256 = sha(Path(__file__))


def _time(value, now):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or now.tzinfo is None or stamp > now:
        raise ValueError("revision_timestamp_invalid")
    return stamp


def load_files(root, raw_files, target, *, now):
    """One shared, read-only resolver for emotion, price and joined evidence."""
    directory = root / "reports/cloud_daily_revisions" / target
    index_path = directory / "index.json"
    if not index_path.exists():
        return raw_files
    index = read_json(index_path)
    daily = read_json(root / "reports/cloud_market_data.json")
    producer = index.get("implementation_sha256")
    if (index_path.is_symlink() or index.get("schema") != SCHEMA or index.get("target_date") != target
            or not isinstance(producer, str) or len(producer) != 64 or any(c not in "0123456789abcdef" for c in producer)
            or index.get("master_sha256") != sha(root / "research/data/stock_basic_baostock.csv")
            or index.get("calendar_sha256") != digest(daily.get("calendar_dates"))
            or not isinstance(index.get("items"), dict) or len(index["items"]) > 10):
        raise ValueError("daily_revision_index_invalid")
    resolved = dict(raw_files)
    for symbol, ref in index["items"].items():
        record = read_json(_bound(root, ref, directory))
        # Schema v1 is the validation contract. Retain the actual producer's
        # hash; a retry/diagnostic-only code update must not relabel old records.
        if (record.get("schema") != SCHEMA or record.get("implementation_sha256") != ref.get("implementation_sha256", producer)
                or record.get("target_date") != target or record.get("original") != raw_files.get(symbol)
                or record.get("master_sha256") != index["master_sha256"]
                or record.get("calendar_sha256") != index["calendar_sha256"]
                or record.get("replacement", {}).get("code") != symbol):
            raise ValueError("daily_revision_source_changed")
        _time(record["published_at"], now)
        _time(record["source_observed_at"], now)
        _bound(root, record["original"], root / "research/data/v4_daily")
        _bound(root, record["receipt"], root / "reports/cloud_daily_gap_probe" / target)
        _bound(root, record["basis_snapshot"], root / "reports/cloud_market_emotion" / target)
        if record.get("adjustment_receipt"):
            _bound(root, record["adjustment_receipt"], root / "reports/cloud_pattern_inputs" / target / "receipts")
        if record.get("tail_receipt"):
            _bound(root, record["tail_receipt"], root / "reports/cloud_daily_tail_probe" / target)
        _bound(root, record["replacement"], root / "research/data/v4_daily")
        resolved[symbol] = {**record["replacement"], "revision": ref}
    return resolved


def _candidate(root, report, gap, reference, *, raw_files, calendar, master_sha, now, clock, client):
    from .cloud_market_emotion import stock_history, COLUMNS

    target, code = report["data_as_of"], gap["code"]
    symbol = code.split(".")[1]
    original = raw_files[symbol]
    if original != gap["raw_reference"] or gap["data_as_of"] != target:
        raise ValueError("revision_raw_identity_changed")
    snapshot_dir = root / "reports/cloud_market_emotion" / target
    snapshot_ref = read_json(snapshot_dir / "manifest.json")["snapshot"]
    if snapshot_ref["sha256"] != report["input_diagnostics"]["snapshot_sha256"]:
        raise ValueError("revision_basis_snapshot_changed")
    snapshot = read_json(_bound(root, snapshot_ref, snapshot_dir))
    if (snapshot["data_as_of"] != target or snapshot["source_contract"]["master_sha256"] != master_sha
            or snapshot["source_contract"]["calendar_sha256"] != digest(calendar)
            or snapshot["source_references"][code]["source"] != original):
        raise ValueError("revision_basis_identity_invalid")
    packet = read_json(_bound(root, reference, root / "reports/cloud_daily_gap_probe" / target))
    body = {k: v for k, v in packet.items() if k != "packet_id"}
    request = packet["request"]
    page = packet.get("pagination") or {}
    started, completed = _time(packet["started_at"], now), _time(packet["completed_at"], now)
    if (packet.get("schema") != PROBE_SCHEMA or packet.get("packet_id") != hashlib.sha256(_canonical_bytes(body)).hexdigest()
            or request.get("implementation_sha256") != PROBE_SHA or request.get("code") != code
            or request.get("target") != target or request.get("raw_sha256") != original["sha256"]
            or request.get("snapshot_sha256") != snapshot_ref["sha256"]
            or packet.get("provider") != "BaoStock" or packet.get("endpoint") != "query_history_k_data_plus"
            or packet.get("start_date") != target or packet.get("end_date") != target or packet.get("code") != code
            or packet.get("adjustflag") != "3" or packet.get("frequency") != "d" or packet.get("requested_fields") != FIELDS
            or packet.get("fields") != FIELDS.split(",") or packet.get("error_code") != "0"
            or packet.get("outcome") != "row_returned_not_admitted" or packet.get("deadline_expired") is not False
            or set(page) != {"page_rows", "cursor", "page_number"}
            or any(type(page.get(k)) is not int or page[k] != 1 for k in ("page_rows", "cursor"))
            or str(page.get("page_number")) != "1"
            or len(packet.get("rows", [])) != 1 or len(packet["rows"][0]) != len(FIELDS.split(","))
            or not _time(snapshot["observed_at"], now) <= started <= completed
            or started < datetime.fromisoformat(target+"T18:00:00+08:00")):
        raise ValueError("revision_receipt_invalid")
    added = dict(zip(packet["fields"], packet["rows"][0]))
    if added["date"] != target or added["code"] != code or added["adjustflag"] != "3":
        raise ValueError("revision_row_identity_invalid")
    raw_path = _bound(root, original, root / "research/data/v4_daily")
    with gzip.open(raw_path, "rt", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        rows, fields = list(reader), reader.fieldnames
    if not rows or rows[-1]["date"] not in calendar or rows[-1]["date"] >= target or set(fields or []) != set(added):
        raise ValueError("revision_tail_source_invalid")
    missing_days = calendar[calendar.index(rows[-1]["date"])+1:]
    additions, tail_ref, source_observed = [added], None, packet["completed_at"]
    if len(missing_days) > 1:
        from .cloud_daily_tail_probe import capture
        additions, tail_ref, source_observed = capture(root, code=code, days=missing_days, basis=reference,
            basis_observed_at=packet["completed_at"], raw=original, calendar_sha=digest(calendar), clock=clock, client=client)
        if additions[-1] != added:
            raise ValueError("revision_conflicting_source_receipts")
        now = clock()
    with (root / "research/data/stock_basic_baostock.csv").open(encoding="utf-8-sig", newline="") as stream:
        security = next(row for row in csv.DictReader(stream) if row["code"] == code)
    args = dict(code=code, ipo=security["ipo_date"], out_date=security["out_date"], calendar=calendar, special_events={})
    text = io.StringIO(newline="")
    writer = csv.DictWriter(text, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows+additions)
    content = gzip.compress(text.getvalue().encode(), mtime=0)
    # The same pure validator checks all prices, statuses and reported changes.
    # This is only input repair; actual emotion production still applies its
    # current special-event evidence and unchanged classification rules.
    with tempfile.TemporaryDirectory(prefix="regis-daily-revision-") as temporary:
        candidate = Path(temporary) / "candidate.gz"
        candidate.write_bytes(content)
        before, _ = stock_history(raw_path, **args)
        after, _ = stock_history(candidate, **args)
    missing = COLUMNS.index("input_missing")
    count = len(additions)
    if (not np.all(before[-count:, missing] == 1) or not np.all(after[-count:, missing] == 0)
            or not np.array_equal(before[:-count], after[:-count], equal_nan=True)):
        raise ValueError("revision_changes_prior_history")
    adjustment_ref = None
    for previous, current in zip([rows[-1]]+additions[:-1], additions):
        if previous["tradestatus"] != "1" or current["tradestatus"] != "1":
            continue
        previous_close, preclose = float(previous["close"]), float(current["preclose"])
        if not math.isclose(previous_close, preclose, rel_tol=0.000001, abs_tol=0.0001):
            inputs_dir = root / "reports/cloud_pattern_inputs" / target
            adjustment_ref = read_json(inputs_dir / "manifest.json")["adjustments"][code]
            adjustment = read_json(_bound(root, adjustment_ref, inputs_dir / "receipts"))
            request = adjustment.get("request") or {}
            if (assess_receipt(adjustment) != "captured_not_validated" or request.get("code") != code
                    or request.get("endpoint") != "query_adjust_factor" or request.get("end_date") != target
                    or request.get("start_date", "z") > previous["date"]
                    or not _time(adjustment["observed_at"], now) <= _time(adjustment["completed_at"], now)):
                raise ValueError("revision_adjustment_identity_invalid")
            events = sorted((dict(zip(adjustment["fields"], row)) for row in adjustment["rows"]), key=lambda row: row["dividOperateDate"])
            factors = [float([event for event in events if event["dividOperateDate"] <= day][-1]["backAdjustFactor"])
                       for day in (previous["date"], current["date"])]
            if (any(not math.isfinite(f) or f <= 0 for f in factors)
                    or not math.isclose(previous_close*factors[0]/factors[1], preclose, rel_tol=0.000001, abs_tol=0.0001)):
                raise ValueError("revision_preclose_chain_mismatch")
    content_sha = hashlib.sha256(content).hexdigest()
    path = raw_path.parent / (symbol+"."+content_sha+".revision.gz")
    replacement = {**original, "path": str(path.relative_to(root)), "sha256": content_sha,
                   "bytes": len(content), "rows": len(rows)+count, "end": target}
    record = {"schema": SCHEMA, "implementation_sha256": IMPLEMENTATION_SHA256, "target_date": target,
              "master_sha256": master_sha, "calendar_sha256": digest(calendar), "original": original,
              "replacement": replacement, "receipt": reference, "basis_snapshot": snapshot_ref,
              "adjustment_receipt": adjustment_ref, "tail_receipt": tail_ref,
              "source_observed_at": source_observed, "published_at": clock().isoformat(),
              "added_rows": count, "prior_rows_unchanged": True, "historical_reconstruction": True,
              "forward_evidence": False, "production_eligible": False, "generates_alert": False}
    return path, content, record


def _pending_report(root, now):
    """Recover the work from immutable evidence after a failed job result."""
    from .cloud_emotion_diagnostics import missing_securities

    if not (root / "reports/cloud_daily_gap_probe").is_dir():
        return {}
    target, _, _ = _source_universe(root)
    directory = root / "reports/cloud_market_emotion" / target
    snapshot_ref = read_json(directory / "manifest.json").get("snapshot")
    if not snapshot_ref:
        return {}
    snapshot = read_json(_bound(root, snapshot_ref, directory))
    _time(snapshot["observed_at"], now)
    gaps = missing_securities(snapshot, root)
    by_code = {row["code"]: row for row in gaps}
    candidates = {}
    for path in sorted((root / "reports/cloud_daily_gap_probe" / target).glob("*.json")):
        if path.is_symlink():
            raise ValueError("revision_receipt_path_invalid")
        packet = read_json(path)
        request = packet.get("request") or {}
        code = request.get("code")
        if (packet.get("outcome") != "row_returned_not_admitted" or code not in by_code
                or request.get("implementation_sha256") != PROBE_SHA or request.get("target") != target
                or request.get("snapshot_sha256") != snapshot_ref["sha256"]
                or request.get("raw_sha256") != by_code[code]["raw_reference"]["sha256"]):
            continue
        previous = candidates.get(code)
        if previous and (previous[0].get("rows") != packet.get("rows") or previous[0].get("fields") != packet.get("fields")):
            raise ValueError("revision_conflicting_source_receipts")
        if not previous or _time(packet["completed_at"], now) < _time(previous[0]["completed_at"], now):
            candidates[code] = (packet, {"code": code, "path": str(path.relative_to(root)), "sha256": sha(path),
                                          "outcome": "row_returned_not_admitted"})
    if not candidates:
        return {}
    return {"status": "complete", "data_as_of": target,
            "input_diagnostics": {"gap_details_status": "available", "gap_securities": gaps, "snapshot_sha256": snapshot_ref["sha256"]},
            "source_gap_probe": {"status": "complete", "implementation_sha256": PROBE_SHA,
                                 "receipts": [item[1] for _, item in sorted(candidates.items())]}}


def prepare(report, *, root=ROOT, now=None, client=None):
    """Publish explicit new versions before the existing emotion job consumes them."""
    clock = (lambda: datetime.now(timezone.utc)) if now is None else (lambda: now)
    current_time = clock()
    if os.environ.get("REGIS_EXECUTION_ROLE") != "cloud":
        return {"status": "not_needed", "new_versions": 0}
    try:
        diagnostics, probe = report.get("input_diagnostics") or {}, report.get("source_gap_probe") or {}
        if (report.get("status") != "complete" or diagnostics.get("gap_details_status") != "available"
                or probe.get("status") != "complete" or probe.get("implementation_sha256") != PROBE_SHA):
            report = _pending_report(root, current_time)
            diagnostics, probe = report.get("input_diagnostics") or {}, report.get("source_gap_probe") or {}
        candidates = [ref for ref in probe.get("receipts", []) if ref.get("outcome") == "row_returned_not_admitted"]
        if not candidates:
            return {"status": "not_needed", "new_versions": 0}
        target, master_sha, _ = _source_universe(root)
        if (target != report["data_as_of"] or not 0 < len(candidates) <= 10 or current_time.tzinfo is None
                or len({ref["code"] for ref in candidates}) != len(candidates)):
            raise ValueError("revision_report_identity_invalid")
        if shutil.disk_usage(root).free < 2*1024**3:
            raise ValueError("revision_storage_insufficient")
        calendar = read_json(root / "reports/cloud_market_data.json")["calendar_dates"]
        if not calendar or calendar[-1] != target or calendar != sorted(set(calendar)) or any(not valid_day(d) for d in calendar):
            raise ValueError("revision_calendar_invalid")
        manifest = read_json(root / "reports/v4_daily_download_manifest.json")
        raw_files = {row["code"]: row for row in manifest["files"]}
        if (len(raw_files) != len(manifest["files"]) or manifest["query"]["end_date"] != target
                or manifest["universe_security_master"]["sha256"] != master_sha):
            raise ValueError("revision_manifest_invalid")
        directory = root / "reports/cloud_daily_revisions" / target
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / ".lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            load_files(root, raw_files, target, now=clock())
            index_path = directory / "index.json"
            index = read_json(index_path) if index_path.exists() else {
                "schema": SCHEMA, "implementation_sha256": IMPLEMENTATION_SHA256, "target_date": target,
                "master_sha256": master_sha, "calendar_sha256": digest(calendar), "items": {}}
            gaps = {row["code"]: row for row in diagnostics["gap_securities"]}
            pending = []
            for reference in candidates:
                code = reference["code"]
                if code.split(".")[1] in index["items"]:
                    continue
                pending.append(_candidate(root, report, gaps[code], reference, raw_files=raw_files,
                                          calendar=calendar, master_sha=master_sha, now=clock(), clock=clock, client=client))
            for path, content, record in pending:
                _atomic_publish(path, content)
                record_path = directory / (digest(record)+".json")
                _atomic_publish(record_path, _canonical_bytes(record))
                index["items"][record["replacement"]["code"]] = {"path": str(record_path.relative_to(root)), "sha256": sha(record_path),
                                                                 "implementation_sha256": IMPLEMENTATION_SHA256}
            # Rebind original sources before the single publication point.
            current_daily = read_json(root / "reports/cloud_market_data.json")
            if (sha(root / "research/data/stock_basic_baostock.csv") != master_sha
                    or current_daily.get("target_date") != target or current_daily.get("calendar_dates") != calendar
                    or read_json(root / "reports/v4_daily_download_manifest.json") != manifest):
                raise ValueError("revision_inputs_changed_during_validation")
            for _, _, record in pending:
                _bound(root, record["original"], root / "research/data/v4_daily")
                _bound(root, record["receipt"], root / "reports/cloud_daily_gap_probe" / target)
                _bound(root, record["basis_snapshot"], root / "reports/cloud_market_emotion" / target)
                _bound(root, record["replacement"], root / "research/data/v4_daily")
                if record["adjustment_receipt"]:
                    _bound(root, record["adjustment_receipt"], root / "reports/cloud_pattern_inputs" / target / "receipts")
                if record.get("tail_receipt"):
                    _bound(root, record["tail_receipt"], root / "reports/cloud_daily_tail_probe" / target)
            if pending:
                publish(index_path, index)
            load_files(root, raw_files, target, now=clock())
        return {"status": "complete", "new_versions": len(pending), "active_versions": len(index["items"]),
                "summary": f"已校验并版本化接入{len(index['items'])}只缺失尾部日线；原件与旧快照保留，不代表策略准入。"}
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration) as exc:
        reason = str(exc)
        check = ("single_tail_required" if reason in {"revision_tail_source_invalid", "revision_tail_range_requires_review"} else
                 "price_chain" if reason.startswith(("revision_preclose", "revision_adjustment", "market_history_")) else
                 "timestamp" if reason == "revision_timestamp_invalid" else
                 "receipt" if reason in {"revision_receipt_invalid", "revision_row_identity_invalid", "revision_conflicting_source_receipts", "revision_tail_receipt_invalid"} else
                 "schema" if isinstance(exc, (KeyError, TypeError, IndexError, StopIteration)) else
                 "storage" if reason == "revision_storage_insufficient" else "source_identity")
        return {"status": "waiting_inputs", "reason": reason, "revision_check": check,
                "summary": "日线修订校验未完成，保留原件与回执，不覆盖或默认接受缺失行。"}
