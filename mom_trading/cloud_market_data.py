"""Build cloud-owned daily inputs from the provider, never from a laptop DB."""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from zoneinfo import ZoneInfo
from uuid import uuid4

from .cloud_research_worker import ROOT, publish, read_json

STATE = ROOT / "reports/cloud_market_data.json"


def completed_date(now=None):
    local = (now or datetime.now(ZoneInfo("Asia/Shanghai"))).astimezone(ZoneInfo("Asia/Shanghai"))
    return (local.date() if local.hour >= 18 else local.date() - timedelta(days=1)).isoformat()


def _cli(main, *arguments):
    # Each job has its own process; preserve the existing collectors' CLI and
    # frozen formulas instead of forking a second downloader implementation.
    previous = sys.argv
    try:
        sys.argv = ["cloud-market-data", *arguments]
        main()
    finally:
        sys.argv = previous


def inspect_existing(download, master_path):
    """Read-only adoption check. Never infer ownership from a filename alone."""
    paths = list(download.OUTPUT_DIR.glob("*.csv.gz"))
    manifest = read_json(download.REPORT_PATH)
    result = {"status": "waiting_adoption", "downloaded": len(paths), "target": 0}

    def reject(reason, message):
        return {**result, "reason": reason, "summary": message + "；保留已有日线，不覆盖。"}

    query = manifest.get("query") or {}
    target = query.get("end_date", "")
    try:
        valid_date = datetime.strptime(target, "%Y-%m-%d").date().isoformat() == target
    except (ValueError, TypeError):
        valid_date = False
    if not valid_date or not download.START_DATE <= target <= completed_date():
        return reject("manifest_missing", "已有资产缺少有效的日线截止日期清单")
    if (not str(manifest.get("source", "")).startswith("BaoStock ")
            or query.get("start_date") != download.START_DATE
            or query.get("frequency") != "d" or query.get("adjustflag") != "3 (不复权)"
            or query.get("fields") != download.FIELDS):
        return reject("source_mismatch", "已有资产的来源或未复权日线口径不匹配")
    if not master_path.is_file() or (manifest.get("universe_security_master") or {}).get("sha256") != download.sha256(master_path):
        return reject("master_mismatch", "已有资产的证券主表指纹未匹配")
    codes = set(download.eligible_basic_rows(end_date=target)["code"])
    symbols = {code.split(".")[1] for code in codes}
    result["target"] = len(codes)
    entries = manifest.get("files") or []
    listed = {entry.get("code") for entry in entries}
    if (not codes or len(entries) != len(listed) or not listed <= symbols
            or listed != {p.name.removesuffix(".csv.gz") for p in paths}
            or manifest.get("target_symbols") != len(codes)):
        return reject("coverage_mismatch", "已有文件、清单和证券范围未能逐项对应")
    for entry in entries:
        path = download.OUTPUT_DIR / (entry["code"] + ".csv.gz")
        # Permit the deployment's directory symlink, but not per-file escapes.
        relative = Path(str(entry.get("path", "")))
        if (relative != Path("research/data/v4_daily") / path.name
                or path.is_symlink() or not path.is_file()
                or entry.get("bytes") != path.stat().st_size
                or entry.get("sha256") != download.sha256(path)):
            return reject("file_mismatch", "已有日线存在路径、大小或内容指纹不匹配")
    complete = manifest.get("ready_for_factor_research") is True and listed == symbols
    if not complete:
        progress = read_json(download.PROGRESS_PATH)
        reusable = {row.get("code") for row in progress.get("completed", [])
                    if row.get("status") in {"downloaded", "existing"}
                    and (row.get("requested_end") == target or row.get("end") == target)}
        if (progress.get("query") or {}).get("end_date") != target or reusable != {code for code in codes if code.split(".")[1] in listed}:
            return reject("resume_mismatch", "已有日线缺少可核验的同批次续传记录")
    return {**result, "status": "adoptable", "target_date": target, "bootstrap_complete": complete,
            "master_sha256": download.sha256(master_path), "manifest_sha256": download.sha256(download.REPORT_PATH)}


def run(*, batch_limit=40):
    if type(batch_limit) is not int or not 1 <= batch_limit <= 200:
        raise ValueError("daily batch must contain 1 to 200 symbols")
    if os.environ.get("REGIS_EXECUTION_ROLE") != "cloud":
        return {"status": "local_unchanged", "summary": "云端日线任务不改动开发电脑已有数据。"}
    from research import build_point_in_time_universe as universe
    from research import download_v4_daily_baostock as download
    from research import incremental_v4_daily as incremental
    from research import audit_v4_daily as audit

    state = read_json(STATE)
    if state and state.get("schema") != "cloud_owned_daily_inputs.v1":
        return {"status": "waiting_identity", "summary": "云端日线批次身份无效，不覆盖已有数据。"}
    if not state and download.REPORT_PATH.exists() and not any(download.OUTPUT_DIR.glob("*.csv.gz")):
        # A source-only deployment may retain metadata without any daily files.
        # Archive it byte-for-byte before a fresh source query; never reuse its
        # apparent completion counts or overwrite an unknown batch of bars.
        if shutil.disk_usage(ROOT).free < 2 * 1024 ** 3:
            return {"status": "waiting_storage", "summary": "空日线环境可用磁盘不足 2 GB；旧元数据保持不变。"}
        archive = ROOT / "reports" / ("daily_input_metadata_before_bootstrap_" + uuid4().hex)
        archive.mkdir(parents=True)
        for path in (download.REPORT_PATH, download.PROGRESS_PATH, universe.DATA_PATH):
            if path.is_file():
                shutil.copy2(path, archive / path.name)
        universe.main()
        publish(STATE, {"schema": "cloud_owned_daily_inputs.v1",
                        "master_sha256": download.sha256(universe.DATA_PATH),
                        "metadata_backup": str(archive.relative_to(ROOT))})
        state = read_json(STATE)
    if not state and (download.REPORT_PATH.exists() or any(download.OUTPUT_DIR.glob("*.csv.gz"))):
        adoption = inspect_existing(download, universe.DATA_PATH)
        if adoption["status"] != "adoptable":
            return adoption
        login = incremental.bs.login()
        if login.error_code != "0":
            return {"status": "retry_pending", "summary": "旧日线指纹匹配，但交易日历连接失败；未修改旧文件，下轮重试。"}
        try:
            dates = incremental.query_official_market_dates(download.START_DATE, adoption["target_date"])
        finally:
            incremental.bs.logout()
        if not dates or dates[-1] != adoption["target_date"]:
            return {"status": "waiting_adoption", "reason": "calendar_mismatch", "summary": "旧日线截止日尚未匹配源交易日历；保留原件。"}
        state = {"schema": "cloud_owned_daily_inputs.v1", "master_sha256": adoption["master_sha256"],
                 "adopted_manifest_sha256": adoption["manifest_sha256"], "target_date": adoption["target_date"],
                 "calendar_dates": dates, "calendar_checked_through": adoption["target_date"],
                 "bootstrap_complete": adoption["bootstrap_complete"], "audited": False}
        publish(STATE, state)
    if shutil.disk_usage(ROOT).free < 2 * 1024 ** 3:
        return {"status": "waiting_storage", "summary": "可用磁盘不足 2 GB，暂停新增日线，保留已完成文件。"}
    if not universe.DATA_PATH.exists():
        universe.main()
    master_sha = download.sha256(universe.DATA_PATH)
    if state.get("master_sha256") not in (None, master_sha):
        return {"status": "waiting_identity", "summary": "证券主表身份发生变化，停止自动续传，保留旧批次。"}

    cutoff = completed_date()
    # Pin the initial collection date across restarts. A moving target would
    # repeatedly restart thousands of symbols before the first audit finishes.
    if not state.get("target_date"):
        login = incremental.bs.login()
        if login.error_code != "0":
            return {"status": "retry_pending", "summary": f"BaoStock 日线连接失败（{login.error_code}），下轮重试；不替换已有数据。"}
        try:
            dates = incremental.query_official_market_dates(download.START_DATE, cutoff)
            target = incremental.require_upstream_market_date(dates, published_end=state.get("target_date", ""))
        finally:
            incremental.bs.logout()
        state.update(
            schema="cloud_owned_daily_inputs.v1", master_sha256=master_sha,
            calendar_dates=dates, calendar_checked_through=cutoff,
            audited=state.get("audited", False) and target == state.get("target_date"),
            target_date=target,
        )
        publish(STATE, state)

    target = state["target_date"]
    manifest = read_json(download.REPORT_PATH)
    if state.get("bootstrap_complete") and manifest.get("ready_for_factor_research") is True:
        from .cloud_daily_epochs import advance
        try:
            advanced = advance(ROOT, cutoff=cutoff, batch_limit=batch_limit)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            return {"status": "retry_pending", "data_as_of": target,
                    "summary": "新版日线采集/发布未完成；保留旧批次与来源回执，下轮续传。",
                    "reason": str(exc), "audited": False}
        if advanced is not None:
            return advanced
    audit_unchanged = audit.REPORT_PATH.is_file() and state.get("audit_sha256") == download.sha256(audit.REPORT_PATH)
    ready = (manifest.get("ready_for_factor_research") is True
             and (manifest.get("query") or {}).get("end_date") == target
             and (not state.get("metadata_backup") or state.get("bootstrap_started")))
    if not ready:
        if state.get("bootstrap_complete"):
            return {"status": "waiting_identity", "data_as_of": target,
                    "summary": "已发布日线与清单不一致，不调用会替换旧文件的增量程序。"}
        _cli(download.main, "--end-date", target, "--limit", str(batch_limit))
        state["bootstrap_started"] = True
        publish(STATE, state)
    manifest = read_json(download.REPORT_PATH)
    completed = int(read_json(download.PROGRESS_PATH).get("completed_symbols") or 0)
    total = int(manifest.get("target_symbols") or 0)
    if manifest.get("ready_for_factor_research") is not True or (manifest.get("query") or {}).get("end_date") != target:
        return {
            "status": "collecting", "summary": f"云端已下载 {completed}/{total} 只未复权日线，目标 {target}；尚未通过质量审计。",
            "downloaded": completed, "target": total, "data_as_of": target,
        }
    # The source calendar becomes an input only after the full download closes.
    state["bootstrap_complete"] = True
    state["audited"] = False  # Never retain a prior pass if new source verification raises.
    publish(STATE, state)
    incremental.publish_market_calendar(state["calendar_dates"], published_end=target)
    revision_index = ROOT / "reports/cloud_daily_revisions" / target / "index.json"
    revision_sha = download.sha256(revision_index) if revision_index.exists() else None
    from .cloud_price_limit_events import load_events
    event_identity = load_events(ROOT)[1]
    auditor_sha = hashlib.sha256((Path(__file__).read_bytes() + Path(audit.__file__).read_bytes())).hexdigest()
    fingerprint = [download.sha256(download.REPORT_PATH), master_sha,
                   download.sha256(audit.SPECIAL_EVENTS_PATH) if audit.SPECIAL_EVENTS_PATH.exists() else None,
                   hashlib.sha256(json.dumps(state["calendar_dates"]).encode()).hexdigest(),
                   revision_sha, auditor_sha, event_identity]
    # Reuse the exact same validated replacement resolver as emotion/features.
    # Do not let the original manifest hide a newly published revision from the audit.
    revised_files = audit.resolved_cloud_files(manifest) if revision_sha else None
    if state.get("last_audit_inputs") != fingerprint or not audit_unchanged:
        if revised_files is not None:
            audit.main(input_paths=[ROOT / row["path"] for row in revised_files], resolve_revisions=True)
        else:
            # Even bootstrap/adopted files need content-bound audit receipts.
            # A directory glob and a manifest hash alone do not prove that
            # the auditor checked the bytes the risk reader will consume.
            audit.main(input_paths=[ROOT / row["path"] for row in manifest["files"]])
        state["last_audit_inputs"] = fingerprint
    quality = read_json(audit.REPORT_PATH)
    state["audit_sha256"] = download.sha256(audit.REPORT_PATH) if audit.REPORT_PATH.is_file() else None
    bindings = quality.get("input_fingerprints") or {}
    state["audited"] = (
        quality.get("ready_for_factor_research") is True
        and bindings.get("download_manifest_sha256") == fingerprint[0]
        and bindings.get("security_master_sha256") == master_sha
        and bindings.get("revision_index_sha256") == revision_sha
        and bindings.get("cloud_price_events_sha256") == event_identity
        and (quality.get("query_window") or {}).get("end_date") == target
    )
    publish(STATE, state)
    return {
        "status": "complete" if state["audited"] else "waiting_audit",
        "summary": f"日线下载 {total}/{total}；审计通过 {quality.get('passed_files', 0)}，失败 {quality.get('failed_files', 0)}。" + ("行情输入已就绪，策略准入单独验证。" if state["audited"] else "仍有缺失或特殊交易规则待核验，不进入策略验证。"),
        "downloaded": total, "target": total, "data_as_of": target,
        "audited": state["audited"],
        "auditor_implementation_sha256": auditor_sha,
        "audit_passed_files": quality.get("passed_files"),
        "audit_failed_files": quality.get("failed_files"),
        "audit_missing_sessions": quality.get("missing_market_dates"),
        "audit_unresolved_limits": quality.get("ordinary_limit_violations"),
        "audit_revised_files": bindings.get("revised_files", 0),
    }
