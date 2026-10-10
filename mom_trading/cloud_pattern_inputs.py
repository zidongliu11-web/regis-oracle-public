"""Cloud-owned, resumable source receipts for the fixed pattern/factor research.

Acquisition is not validation. In particular, an observed industry snapshot is
not a historical membership table, and adjustment factors are not cash returns.
"""
from __future__ import annotations

from contextlib import contextmanager
import csv
from datetime import date, datetime, timedelta, timezone
import fcntl
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import signal
from uuid import uuid4

from .cloud_research_worker import ROOT, publish, read_json


SCHEMA = "cloud_pattern_input_receipts.v1"
PROGRESS_NAME = "cloud_pattern_inputs_progress.json"
ADJUST_FIELDS = {"code", "dividOperateDate", "foreAdjustFactor", "backAdjustFactor", "adjustFactor"}
INDUSTRY_FIELDS = {"updateDate", "code", "code_name", "industry", "industryClassification"}
SDK_PAGE_SIZE = 2000  # BaoStock ResultData.next() pagination contract.
SOURCE_SCOPE = {
    "provider": "BaoStock", "adjustment_use": "price_features_only_not_cash_or_share_entitlements",
    "industry_use": "observed_snapshot_only_not_backfilled_historical_membership",
    "independent_strategy_validation": False, "production_eligible": False, "generates_alert": False,
}


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def valid_day(value):
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


@contextmanager
def provider_deadline(seconds=20):
    # The worker invokes each job in a single-threaded child process. A native
    # alarm bounds the SDK's socket wait without leaving abandoned threads.
    previous = signal.getsignal(signal.SIGALRM)
    deadline = {"expired": False}
    def expired(*_):
        deadline["expired"] = True
        raise TimeoutError("pattern_input_provider_timeout")
    signal.signal(signal.SIGALRM, expired)
    timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        # The SDK catches socket exceptions, including TimeoutError. Retain an
        # independent flag rather than trusting its unchanged success code.
        yield deadline
    finally:
        signal.setitimer(signal.ITIMER_REAL, *timer)
        signal.signal(signal.SIGALRM, previous)


def query_receipt(client, request, now):
    payload = {"schema": SCHEMA, "request": request, "observed_at": now().isoformat(), **SOURCE_SCOPE}
    fields, rows = [], []
    deadline = {"expired": False}
    try:
        from baostock.common.contants import BAOSTOCK_PER_PAGE_COUNT
        if BAOSTOCK_PER_PAGE_COUNT != SDK_PAGE_SIZE:
            raise ValueError("provider_pagination_contract_changed")
        with provider_deadline() as deadline:
            if request["endpoint"] == "query_adjust_factor":
                result = client.query_adjust_factor(code=request["code"], start_date=request["start_date"], end_date=request["end_date"])
                cap = 1000
            else:
                result = client.query_stock_industry(code=request["code"], date=request["date"])
                cap = 1
            fields, rows = list(result.fields or []), []
            while not deadline["expired"] and result.error_code == "0" and result.next():
                rows.append(result.get_row_data())
                if len(rows) > cap:
                    raise ValueError("pattern_input_response_limit")
            payload.update(error_code=str(result.error_code), fields=fields, rows=rows)
            page_rows, cursor = len(result.data), result.cur_row_num
            payload["pagination"] = {"page_rows": page_rows, "cursor": cursor,
                                     "page_number": result.cur_page_num, "page_size": SDK_PAGE_SIZE}
    except Exception as exc:
        payload.update(error_code=type(exc).__name__, fields=fields, rows=rows)
    payload["deadline_expired"] = deadline["expired"]
    if deadline["expired"]:
        payload["error_code"] = "TimeoutError"
    payload["completed_at"] = now().isoformat()
    payload["assessment"] = assess_receipt(payload)
    return payload


def assess_receipt(payload):
    """Validate source structure/identity only; never infer missing identities."""
    if payload.get("error_code") != "0" or payload.get("deadline_expired") is not False:
        return "provider_error"
    page = payload.get("pagination") or {}
    # next() returning False with a full final page can mean a failed fetch of
    # the following page. Only an exhausted short/empty page proves termination.
    if (page.get("page_size") != SDK_PAGE_SIZE or type(page.get("page_rows")) is not int
            or not 0 <= page["page_rows"] < SDK_PAGE_SIZE
            or type(page.get("cursor")) is not int or page["cursor"] != page["page_rows"]):
        return "pagination_unverified"
    request, fields, rows = payload["request"], payload["fields"], payload["rows"]
    adjust = request["endpoint"] == "query_adjust_factor"
    expected = ADJUST_FIELDS if adjust else INDUSTRY_FIELDS
    if (not isinstance(fields, list) or not all(isinstance(f, str) for f in fields)
            or set(fields) != expected or len(fields) != len(expected) or not isinstance(rows, list)
            or any(not isinstance(row, (list, tuple)) or len(row) != len(fields)
                   or not all(isinstance(value, str) for value in row) for row in rows)):
        return "schema_mismatch"
    if not rows:
        return "empty_requires_review"
    seen = set()
    for values in rows:
        row = dict(zip(fields, values))
        if not re.fullmatch(r"(?:sh|sz|bj)\.\d{6}", row["code"]):
            return "security_identity_mismatch"
        if adjust:
            day = row["dividOperateDate"]
            if row["code"] != request["code"] or not valid_day(day) or not request["start_date"] <= day <= request["end_date"]:
                return "security_or_date_mismatch"
            try:
                if any(not math.isfinite(float(row[f])) or float(row[f]) <= 0 for f in ADJUST_FIELDS-{"code", "dividOperateDate"}):
                    return "invalid_adjustment_factor"
            except (ValueError, TypeError):
                return "invalid_adjustment_factor"
            key = row["code"], day
        else:
            if row["code"] != request["code"]:
                return "security_identity_mismatch"
            limit = request["date"] or request["observation_date"]
            if not valid_day(row["updateDate"]) or row["updateDate"] > limit:
                return "industry_date_mismatch"
            key = row["code"]
        if key in seen:
            return "duplicate_source_identity"
        seen.add(key)
    return "captured_not_validated"


def _source_universe(root):
    from research.download_v4_daily_baostock import ALLOWED_PREFIXES, START_DATE
    daily = read_json(root / "reports/cloud_market_data.json")
    target = daily.get("target_date")
    master = root / "research/data/stock_basic_baostock.csv"
    if daily.get("schema") != "cloud_owned_daily_inputs.v1" or not valid_day(target) or not master.is_file():
        raise ValueError("daily_source_identity_missing")
    if sha(master) != daily.get("master_sha256"):
        raise ValueError("daily_security_master_mismatch")
    codes = {}
    with master.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            code = row.get("code", "")
            if row.get("type") != "1" or not code.startswith(ALLOWED_PREFIXES):
                continue
            ipo, end = row.get("ipo_date"), row.get("out_date")
            if not re.fullmatch(r"(?:sh|sz)\.\d{6}", code) or not valid_day(ipo) or (end and not valid_day(end)):
                raise ValueError("security_master_dates_invalid")
            if ipo > target or (end and end < START_DATE):
                continue
            if code in codes:
                raise ValueError("security_master_duplicate")
            codes[code] = ipo
    if not codes:
        raise ValueError("daily_security_universe_empty")
    return target, daily["master_sha256"], dict(sorted(codes.items()))


def progress(state):
    entries = list(state.get("adjustments", {}).values())
    captured = sum(e["assessment"] == "captured_not_validated" for e in entries)
    empty = sum(e["assessment"] == "empty_requires_review" for e in entries)
    industries = list(state.get("industries", {}).values())
    industry_count = sum(e["assessment"] == "captured_not_validated" for e in industries)
    industry_empty = sum(e["assessment"] == "empty_requires_review" for e in industries)
    complete = captured+empty == state["target"] and industry_count == state["target"]
    return {"status": "complete" if complete else "collecting", "schema": SCHEMA,
            "data_as_of": state["target_date"], "captured": captured, "empty": empty, "target": state["target"],
            "industry_rows": industry_count, "industry_empty": industry_empty,
            "industry_captured": industry_count == state["target"], "research_inputs_ready": False,
            "summary": f"复权事件有效响应 {captured}/{state['target']}，空响应 {empty} 项待核验；逐证券行业归属 {industry_count}/{state['target']}，空响应 {industry_empty}。原始证据不等于复权审计、历史行业归属或策略通过。",
            **SOURCE_SCOPE}


def run(*, root=ROOT, batch_limit=20, client=None, now=None):
    if os.environ.get("REGIS_EXECUTION_ROLE") != "cloud":
        return {"status": "local_unchanged", "summary": "云端研究输入采集不修改开发电脑的已有研究数据。"}
    if type(batch_limit) is not int or not 1 <= batch_limit <= 100:
        raise ValueError("pattern_input_batch_limit_invalid")
    now = now or (lambda: datetime.now(timezone.utc))
    try:
        target, master_sha, codes = _source_universe(root)
    except (OSError, ValueError) as exc:
        return {"status": "waiting_daily_inputs", "reason": str(exc), "summary": "云端日线批次与证券身份尚未就绪，未启动复权及行业采集。"}
    if target > now().astimezone(timezone(timedelta(hours=8))).date().isoformat():
        return {"status": "waiting_identity", "summary": "日线目标在未来，保留已有研究输入。"}
    if shutil.disk_usage(root).free < 2 * 1024**3:
        return {"status": "waiting_storage", "summary": "可用磁盘不足2GB，保留研究输入，暂不新增响应文件。"}
    directory = root / "reports/cloud_pattern_inputs" / target
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "owned_elsewhere", "summary": "本日研究输入采集已有进程持锁，不重复启动。"}
        state_path = directory / "manifest.json"
        state = read_json(state_path)
        if state_path.exists() and (state.get("schema") != SCHEMA or state.get("master_sha256") != master_sha or state.get("target_date") != target):
            return {"status": "waiting_identity", "summary": "研究输入批次身份改变，保留旧批次，不自动覆盖。"}
        if not state:
            state = {"schema": SCHEMA, "target_date": target, "master_sha256": master_sha,
                     "target": len(codes), "created_at": now().isoformat(), "adjustments": {}, "industries": {}, **SOURCE_SCOPE}
        if state.get("target") != len(codes) or any(not isinstance(state.get(kind), dict) or not set(state[kind]) <= set(codes) for kind in ("adjustments", "industries")):
            return {"status": "waiting_identity", "summary": "研究证券范围与批次不一致，保留原件。"}
        receipts = directory / "receipts"
        receipts.mkdir(exist_ok=True)
        entries = [(kind, code, entry) for kind in ("adjustments", "industries") for code, entry in state[kind].items()]
        for kind, code, entry in entries:
            path = root / entry["path"]
            if path.is_symlink() or path.parent.resolve() != receipts.resolve() or not path.is_file() or sha(path) != entry["sha256"]:
                return {"status": "waiting_identity", "summary": "已保存原始响应的路径或指纹不匹配，保留原件，不覆盖重取。"}
            receipt = read_json(path)
            observed = datetime.fromisoformat(receipt["observed_at"])
            if observed.tzinfo is None or observed > now():
                return {"status": "waiting_identity", "summary": "原始响应缺少有效采集时点，保留原件。"}
            observation_day = observed.astimezone(timezone(timedelta(hours=8))).date().isoformat()
            request = {"endpoint": "query_stock_industry", "code": code, "date": "", "observation_date": observation_day} if kind == "industries" else {
                "endpoint": "query_adjust_factor", "code": code, "start_date": codes[code], "end_date": target}
            if (receipt.get("schema") != SCHEMA or receipt.get("request") != request
                    or receipt.get("assessment") != entry.get("assessment")
                    or assess_receipt(receipt) != entry["assessment"]):
                return {"status": "waiting_identity", "summary": "原始响应与请求或校验身份不符，保留原件。"}
        def save():
            publish(state_path, state)
            report = progress(state)
            report["job_run_id"] = os.environ.get("REGIS_RESEARCH_JOB_ID")
            publish(root / "reports" / PROGRESS_NAME, report)
            return report
        def due(entry, *, allow_empty=True):
            terminal = {"captured_not_validated", "empty_requires_review"} if allow_empty else {"captured_not_validated"}
            return not entry or (entry["assessment"] not in terminal
                                 and now().isoformat() >= entry["retry_after"])
        pending = [(code, ipo) for code, ipo in codes.items() if due(state["adjustments"].get(code)) or due(state["industries"].get(code), allow_empty=False)]
        # A repeatedly failing early ticker must not starve every later ticker.
        pending.sort(key=lambda item: (
            max((state[kind].get(item[0], {}).get("observed_at", "") for kind in ("adjustments", "industries"))), item[0],
        ))
        pending = pending[:batch_limit]
        if not pending:
            report = save()
            return {**report, "status": "retry_pending"} if report["status"] != "complete" else report
        if client is None:
            import baostock as client
        logged_in = False
        try:
            with provider_deadline() as deadline:
                login = client.login()
            if deadline["expired"] or login.error_code != "0":
                return {**save(), "status": "retry_pending", "summary": "研究输入源连接失败，保留已保存响应，下轮重试。"}
            logged_in = True
            def capture(request, previous):
                payload = query_receipt(client, request, now)
                path = receipts / (uuid4().hex + ".json")
                publish(path, payload)
                attempts = int((previous or {}).get("attempts", 0)) + 1
                return {"path": str(path.relative_to(root)), "sha256": sha(path), "assessment": payload["assessment"],
                        "row_count": len(payload["rows"]), "observed_at": payload["observed_at"], "attempts": attempts,
                        "retry_after": (now()+timedelta(minutes=min(60, 2**min(attempts, 6)))).isoformat()}
            for code, ipo in pending:
                # Ask for the currently reported mapping, not a historical
                # mapping inferred from the daily-bar cutoff. Its observation
                # date stays explicit and is never backdated to that cutoff.
                observation_day = now().astimezone(timezone(timedelta(hours=8))).date().isoformat()
                requests = (
                    ("adjustments", {"endpoint": "query_adjust_factor", "code": code, "start_date": ipo, "end_date": target}),
                    ("industries", {"endpoint": "query_stock_industry", "code": code, "date": "", "observation_date": observation_day}),
                )
                for kind, request in requests:
                    previous = state[kind].get(code)
                    if not due(previous, allow_empty=kind == "adjustments"):
                        continue
                    state[kind][code] = capture(request, previous)
                    save()
                    if state[kind][code]["assessment"] in {"provider_error", "pagination_unverified"}:
                        return {**progress(state), "status": "retry_pending", "summary": "数据源请求或分页失败，已保留本轮响应；结束连接，下轮从未处理证券续传。"}
        finally:
            if logged_in:
                with provider_deadline():
                    client.logout()
        if sha(root / "research/data/stock_basic_baostock.csv") != master_sha:
            return {"status": "waiting_identity", "summary": "采集过程中证券主表发生变化，保留响应，暂不采用本批次。"}
        return save()
