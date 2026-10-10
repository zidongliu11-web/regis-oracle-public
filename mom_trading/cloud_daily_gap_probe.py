"""Bounded, append-only source rechecks. Never replace bars or admit a trade."""
import csv
from datetime import datetime, timezone, timedelta
import fcntl
import gzip
import hashlib
import os
from pathlib import Path
import re
import shutil

from .cloud_pattern_features import _bound
from .cloud_pattern_inputs import provider_deadline, SDK_PAGE_SIZE, sha, valid_day
from .cloud_research_worker import ROOT, read_json
from .market_state_daily import _atomic_publish, _canonical_bytes
from research.download_v4_daily_baostock import FIELDS

SCHEMA = "cloud_daily_gap_probe.v1"
IMPLEMENTATION_SHA256 = sha(Path(__file__))
SHANGHAI = timezone(timedelta(hours=8))
OUTCOMES = {"raw_row_present", "row_returned_not_admitted", "complete_empty", "source_unavailable", "response_invalid"}


def query(client, code, day, now):
    receipt = {"provider": "BaoStock", "endpoint": "query_history_k_data_plus", "code": code,
               "start_date": day, "end_date": day, "adjustflag": "3", "frequency": "d",
               "requested_fields": FIELDS, "started_at": now().isoformat()}
    guard, rows, fields = {"expired": False}, [], []
    try:
        from baostock.common.contants import BAOSTOCK_PER_PAGE_COUNT
        if BAOSTOCK_PER_PAGE_COUNT != SDK_PAGE_SIZE:
            raise ValueError("provider_page_contract_changed")
        with provider_deadline() as guard:
            result = client.query_history_k_data_plus(code, FIELDS, start_date=day, end_date=day, frequency="d", adjustflag="3")
            fields = list(result.fields or [])
            while not guard["expired"] and result.error_code == "0" and result.next():
                rows.append(result.get_row_data())
                if len(rows) > 1:
                    raise ValueError("unexpected_daily_rows")
            page = {"page_rows": len(result.data), "cursor": result.cur_row_num, "page_number": result.cur_page_num}
            complete = not guard["expired"] and result.error_code == "0" and page["page_rows"] < SDK_PAGE_SIZE and page["cursor"] == page["page_rows"]
            outcome = "source_unavailable"
            if complete:
                valid = fields == FIELDS.split(",") and all(len(row) == len(fields) and row[0] == day and row[1] == code for row in rows)
                outcome = ("row_returned_not_admitted" if rows else "complete_empty") if valid else "response_invalid"
            receipt.update(error_code=str(result.error_code), pagination=page, outcome=outcome)
    except Exception as exc:
        receipt.update(outcome="source_unavailable", error_code=type(exc).__name__)
    if guard["expired"]:
        receipt.update(outcome="source_unavailable", error_code="TimeoutError")
    return {**receipt, "fields": fields, "rows": rows, "deadline_expired": guard["expired"], "completed_at": now().isoformat()}


def enrich(report, *, root=None, client=None, now=None):
    from .cloud_emotion_diagnostics import IMPLEMENTATION_SHA256 as diagnostic_sha

    d = report.get("input_diagnostics") or {}
    if (os.environ.get("REGIS_EXECUTION_ROLE") != "cloud" or report.get("status") != "complete"
            or d.get("implementation_sha256") != diagnostic_sha or d.get("gap_details_status") != "available"
            or not d.get("gap_securities")):
        return report
    root = root or ROOT
    now = now or (lambda: datetime.now(timezone.utc))
    current = now()
    target = report.get("data_as_of")
    gaps = d["gap_securities"]
    result = {"implementation_sha256": IMPLEMENTATION_SHA256, "data_as_of": target,
              "research_only": True, "changes_trade_authority": False, "changes_raw_bars": False}
    if (current.tzinfo is None or not valid_day(target) or current < datetime.fromisoformat(target+"T18:00:00+08:00")
            or not 0 < len(gaps) <= 10 or len(gaps) != d.get("missing_listed_sessions")
            or any(not re.fullmatch(r"(?:sh|sz)\.\d{6}", str(row.get("code", ""))) or row.get("data_as_of") != target for row in gaps)
            or shutil.disk_usage(root).free < 2*1024**3):
        return {**report, "source_gap_probe": {**result, "status": "waiting_inputs"}}
    observation_day = current.astimezone(SHANGHAI).date().isoformat()
    directory = root / "reports/cloud_daily_gap_probe" / target
    directory.mkdir(parents=True, exist_ok=True)
    outcomes, references = [], []
    logged_in = False
    try:
        with (directory / ".lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for gap in gaps:
                raw_path = _bound(root, gap["raw_reference"], root / "research/data/v4_daily")
                request = {"code": gap["code"], "target": target, "snapshot_sha256": d["snapshot_sha256"],
                           "raw_sha256": gap["raw_reference"]["sha256"], "observation_day": observation_day,
                           "implementation_sha256": IMPLEMENTATION_SHA256}
                identity = hashlib.sha256(_canonical_bytes(request)).hexdigest()
                # Source failures may retry in a later 30-minute bucket. Old
                # receipts remain immutable; no return or price ranking is read.
                slot = current.astimezone(SHANGHAI).hour*2 + current.minute//30
                path = directory / (identity+f".{slot:02}.json")
                previous = sorted(directory.glob(identity+".*.json"))
                if previous:
                    latest = read_json(previous[-1])
                    if latest.get("outcome") in {"raw_row_present", "row_returned_not_admitted"}:
                        path = previous[-1]
                if path.exists():
                    packet = read_json(path)
                    body = {k: v for k, v in packet.items() if k != "packet_id"}
                    if (path.is_symlink() or packet.get("schema") != SCHEMA or packet.get("request") != request
                            or packet.get("packet_id") != hashlib.sha256(_canonical_bytes(body)).hexdigest()
                            or datetime.fromisoformat(packet["completed_at"]) > current or packet.get("outcome") not in OUTCOMES):
                        raise ValueError("gap_probe_receipt_invalid")
                else:
                    with gzip.open(raw_path, "rt", encoding="utf-8-sig", newline="") as stream:
                        raw_rows = [row for row in csv.DictReader(stream) if row.get("date") == target]
                    if raw_rows:
                        receipt = {"outcome": "raw_row_present", "completed_at": now().isoformat()}
                    else:
                        if client is None:
                            import baostock as client
                        if not logged_in:
                            with provider_deadline() as guard:
                                logged_in = client.login().error_code == "0" and not guard["expired"]
                            if not logged_in:
                                raise RuntimeError("gap_probe_login_failed")
                        receipt = query(client, gap["code"], target, now)
                    packet = {"schema": SCHEMA, "request": request, **receipt}
                    packet["packet_id"] = hashlib.sha256(_canonical_bytes(packet)).hexdigest()
                    _atomic_publish(path, _canonical_bytes(packet))
                _bound(root, gap["raw_reference"], root / "research/data/v4_daily")
                outcomes.append(packet["outcome"])
                references.append({"code": gap["code"], "path": str(path.relative_to(root)), "sha256": sha(path), "outcome": packet["outcome"]})
        result.update(status="complete", counts={key: outcomes.count(key) for key in sorted(OUTCOMES)}, receipts=references)
        summary = f" 缺失行源复查：原件已含行{outcomes.count('raw_row_present')}，供应商返回行但未准入{outcomes.count('row_returned_not_admitted')}，完整空响应{outcomes.count('complete_empty')}，来源失败/响应无效{sum(value in {'source_unavailable', 'response_invalid'} for value in outcomes)}；没有覆盖原行情或修复交易权限。"
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        result.update(status="unavailable")
        summary = " 缺失行源复查未完成；保留原件，不解释为供应商无数据。"
    finally:
        if logged_in:
            try:
                with provider_deadline() as guard:
                    client.logout()
                result["logout_status"] = "timed_out" if guard["expired"] else "complete"
            except Exception as exc:
                result["logout_status"] = type(exc).__name__
            if result["logout_status"] != "complete":
                summary += " 数据源退出未完成；已保存的复查回执不变。"
    return {**report, "source_gap_probe": result, "summary": report.get("summary", "")+summary}


def header(job):
    d = job.get("source_gap_probe") or {}
    counts = d.get("counts") or {}
    if (job.get("status") != "complete" or d.get("status") != "complete" or d.get("data_as_of") != job.get("data_as_of")
            or d.get("implementation_sha256") != IMPLEMENTATION_SHA256 or set(counts) != OUTCOMES
            or any(type(v) is not int or not 0 <= v <= 10 for v in counts.values()) or not 0 < sum(counts.values()) <= 10):
        return ""
    return (f";probe={IMPLEMENTATION_SHA256};raw_present={counts['raw_row_present']};returned={counts['row_returned_not_admitted']}"
            f";empty={counts['complete_empty']};source_failures={counts['source_unavailable']+counts['response_invalid']}")
