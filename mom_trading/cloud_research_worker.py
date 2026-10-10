"""Bounded research jobs, independent of the browser and developer computer."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4

from .research_release_guard import source_lease

ROOT = Path(__file__).resolve().parents[1]
STATUS = ROOT / "reports/cloud_research_status.json"
JOBS = {
    "sector_observations": "板块连续观察留档",
    "industry_daily": "当日行业归属采集与续传",
    "price_events": "历史限价异常的官方公告核验",
    "market_data": "云端研究日线采集与审计",
    "decision_inputs": "决策日历与指数日线来源核验",
    "pattern_inputs": "战法与五因子输入采集",
    "pattern_features": "战法与因子价格特征计算",
    "market_emotion": "完整市场情绪历史与战法适用性",
    "pattern_route": "情绪与主线战法联合筛选",
    "research_experiments": "已登记情绪战法实验执行",
    "audit": "情绪战法与研究输入检查",
    "factors": "已登记因子验证",
    "strategies": "未完成策略批次恢复",
    "sources": "公开论文与研究代码搜索",
}


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def publish(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def public_status(path=STATUS, *, now=None):
    data = read_json(path)
    now = now or datetime.now(timezone.utc)
    try:
        age = (now - datetime.fromisoformat(data["heartbeat_at"])).total_seconds()
        pid = int(data["pid"])
        command = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="], capture_output=True,
            text=True, timeout=2, check=False,
        ).stdout
        alive = 0 <= age <= 90 and "-m mom_trading.cloud_research_worker" in command
    except (KeyError, TypeError, ValueError, OSError, subprocess.TimeoutExpired):
        alive = False
    if data.get("sector_timer_managed") is True and data.get("execution_role") == "cloud":
        from .cloud_sector_timer import current_status
        data.setdefault("jobs", {})["sector_observations"] = {
            **current_status(path.parent / "cloud_sector_timer_status.json", now=now),
            "label": JOBS["sector_observations"],
        }
    events = (data.get("jobs") or {}).get("price_events")
    if isinstance(events, dict):
        events["failure_counts"] = price_event_failure_counts(read_json(path.parent / "cloud_price_limit_events/attempts.json"))
    return {
        "online": alive and data.get("status") == "running",
        "execution_role": data.get("execution_role", "unavailable"),
        "heartbeat_at": data.get("heartbeat_at"),
        "current_job": data.get("current_job"),
        "last_cycle_at": data.get("last_cycle_at"),
        "jobs": data.get("jobs", {}),
        "automatic_trading": False,
        "automatic_code_generation": False,
        "scope": "云端日线与审计、复权和行业采集、价格特征与完整市场情绪历史计算、来源搜索及冻结研究恢复；情绪标签和适用配方不等于通过验证的策略，不自动投产。",
    }


def price_event_failure_counts(attempts):
    """Read source failures as categories only; no securities, URLs or raw errors."""
    counts = {}
    if len(attempts) > 1000:
        return {"invalid": 1}
    for row in attempts.values():
        outcome = row.get("outcome", "") if isinstance(row, dict) else ""
        if not isinstance(outcome, str):
            outcome = ""
        if outcome == "verified_disclosure":
            continue
        kind = "other"
        if outcome.startswith("HTTPError:"):
            kind = next((f"http{code}" for code in (403, 404, 412, 429, 502, 503) if f"{code} " in outcome), "http_other")
        elif outcome.startswith(("ConnectionError:", "ConnectTimeout:", "ReadTimeout:", "TimeoutError:")):
            kind = "network"
        elif outcome == "ValueError: no_supported_pre_event_disclosure":
            kind = "no_rule"
        elif outcome.startswith("ValueError: disclosure_org_identity"):
            kind = "organization"
        elif outcome.startswith("ValueError: disclosure_query"):
            kind = "query"
        elif outcome.startswith("ValueError: disclosure_pdf"):
            kind = "pdf"
        elif outcome.startswith("ValueError: disclosure_"):
            kind = "document"
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def market_data_health_header(status):
    """Only public-market acquisition counts; never publish job/account details."""
    job = (status.get("jobs") or {}).get("market_data") or {}
    value = job.get("status")
    allowed = {"running", "collecting", "complete", "waiting_audit", "retry_pending",
               "waiting_identity", "waiting_adoption", "waiting_storage", "failed"}
    value = value if value in allowed else "unverified"
    counts = []
    for name in ("downloaded", "target"):
        count = job.get(name)
        counts.append(count if type(count) is int and 0 <= count <= 100000 else 0)
    reasons = {"manifest_missing", "source_mismatch", "master_mismatch", "coverage_mismatch",
               "file_mismatch", "resume_mismatch", "calendar_mismatch"}
    reason = f";reason={job['reason']}" if value == "waiting_adoption" and job.get("reason") in reasons else ""
    publisher = job.get("publisher_implementation_sha256")
    identity = (";publisher="+publisher if value != "running" and isinstance(publisher, str) and len(publisher) == 64
                and all(c in "0123456789abcdef" for c in publisher) else "")
    transport = job.get("tail_transport")
    transport = transport if isinstance(transport, dict) else {}
    tail_sha = transport.get("implementation_sha256")
    if value != "running" and isinstance(tail_sha, str) and len(tail_sha) == 64 and all(c in "0123456789abcdef" for c in tail_sha):
        identity += ";tail_impl="+tail_sha
        for key in ("queries", "logins", "logouts"):
            count = transport.get(key)
            identity += f";tail_{key}={count if type(count) is int and 0 <= count <= 1000 else 'unknown'}"
        logout = transport.get("logout_status")
        identity += ";tail_close="+(logout if logout in {"complete", "not_opened", "timed_out", "provider_error"} else "unverified")
    auditor = job.get("auditor_implementation_sha256")
    audit = ""
    if value in {"complete", "waiting_audit"} and isinstance(auditor, str) and len(auditor) == 64 and all(c in "0123456789abcdef" for c in auditor):
        audit = ";auditor="+auditor
        for public, key in (("passed", "audit_passed_files"), ("failed", "audit_failed_files"),
                            ("missing", "audit_missing_sessions"), ("limits", "audit_unresolved_limits"),
                            ("revised", "audit_revised_files")):
            count = job.get(key)
            audit += f";{public}={count if type(count) is int and 0 <= count <= 100000000 else 'unknown'}"
    events = (status.get("jobs") or {}).get("price_events") or {}
    event_sha = events.get("implementation_sha256")
    event_health = ""
    if isinstance(event_sha, str) and len(event_sha) == 64 and all(c in "0123456789abcdef" for c in event_sha):
        event_health = ";events_impl=" + event_sha
        for name in ("captured", "target", "remaining"):
            count = events.get(name)
            event_health += f";events_{name}={count if type(count) is int and 0 <= count <= 1000 else 'unknown'}"
        failures = events.get("failure_counts")
        failures = failures if isinstance(failures, dict) else {}
        for kind in ("http403", "http404", "http412", "http429", "http502", "http503", "http_other",
                     "network", "no_rule", "organization", "query", "pdf", "document", "other", "invalid"):
            count = failures.get(kind)
            if type(count) is int and 0 < count <= 1000:
                event_health += f";events_{kind}={count}"
    return f"{value};downloaded={counts[0]};target={counts[1]}{reason}{identity}{audit}{event_health}"


def pattern_input_health_header(status):
    """Expose bounded public-source counters only, not receipts or job text."""
    job = (status.get("jobs") or {}).get("pattern_inputs") or {}
    allowed = {"running", "collecting", "complete", "retry_pending", "waiting_daily_inputs",
               "waiting_identity", "waiting_storage", "owned_elsewhere", "failed"}
    value = job.get("status") if status.get("online") and job.get("status") in allowed else "unverified"
    counts = [job.get(name) for name in ("captured", "target", "empty", "industry_rows")]
    counts = [n if type(n) is int and 0 <= n <= 100000 else 0 for n in counts]
    return f"{value};captured={counts[0]};target={counts[1]};empty={counts[2]};industry={counts[3]}"


def industry_daily_health_header(status):
    job = (status.get("jobs") or {}).get("industry_daily") or {}
    allowed = {"running", "collecting", "complete", "retry_pending", "waiting_inputs", "waiting_storage",
               "owned_elsewhere", "date_rolled_over", "failed"}
    value = job.get("status") if status.get("online") and job.get("status") in allowed else "unverified"
    counts = [job.get(name) for name in ("captured", "classified", "target", "adopted")]
    counts = [n if type(n) is int and 0 <= n <= 100000 else 0 for n in counts]
    day = job.get("observation_date")
    try:
        day = datetime.strptime(day, "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError):
        day = "unknown"
    implementation = job.get("implementation_sha256")
    identity = (";implementation="+implementation if isinstance(implementation, str)
                and len(implementation) == 64 and all(c in "0123456789abcdef" for c in implementation) else "")
    return f"{value};captured={counts[0]};classified={counts[1]};target={counts[2]};adopted={counts[3]};day={day}{identity}"


def pattern_feature_health_header(status):
    job = (status.get("jobs") or {}).get("pattern_features") or {}
    allowed = {"running", "collecting", "complete", "waiting_inputs", "waiting_identity", "waiting_storage", "owned_elsewhere", "failed"}
    value = job.get("status") if status.get("online") and job.get("status") in allowed else "unverified"
    counts = [job.get("price_windows"), job.get("target"), (job.get("factor_finite_counts") or {}).get("行业相对强度")]
    counts = [n if type(n) is int and 0 <= n <= 100000 else 0 for n in counts]
    implementation = job.get("implementation_sha256")
    identity = (";implementation="+implementation if isinstance(implementation, str)
                and len(implementation) == 64 and all(c in "0123456789abcdef" for c in implementation) else "")
    # These are existing producer diagnostics, not a new calculation identity.
    # A running/failed job must not present a previous run's gaps as current.
    detail = ""
    day, cross, review = (job.get(k) for k in ("data_as_of", "cross_section_status", "review_required"))
    try:
        valid_day = isinstance(day, str) and datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") == day
    except ValueError:
        valid_day = False
    if (value in {"collecting", "waiting_inputs", "complete"} and valid_day
            and isinstance(cross, str) and cross in {"waiting_price_coverage", "waiting_price_checks", "waiting_same_day_industry", "complete"}
            and type(review) is int and 0 <= review <= counts[1]-counts[0]
            and type(job.get("target")) is int and type(job.get("price_windows")) is int
            and 0 <= job["price_windows"] <= job["target"] <= 100000):
        detail = f";day={day};cross={cross};review={review}"
    return f"{value};windows={counts[0]};target={counts[1]};industry={counts[2]}{identity}{detail}"


def market_emotion_health_header(status):
    job = (status.get("jobs") or {}).get("market_emotion") or {}
    allowed = {"running", "collecting", "complete", "waiting_inputs", "waiting_identity", "waiting_storage", "owned_elsewhere", "failed"}
    value = job.get("status") if status.get("online") and job.get("status") in allowed else "unverified"
    counts = [job.get(name) for name in ("histories_computed", "target", "classified_sessions")]
    counts = [n if type(n) is int and 0 <= n <= 100000 else 0 for n in counts]
    state = job.get("latest_state") if value == "complete" else "UNKNOWN"
    if state not in {"UNKNOWN", "RISK_OFF", "STABILIZING", "RISK_ON", "OVERHEATED"}:
        state = "UNKNOWN"
    implementation = job.get("implementation_sha256")
    identity = (";implementation="+implementation if isinstance(implementation, str)
                and len(implementation) == 64 and all(c in "0123456789abcdef" for c in implementation) else "")
    from .cloud_emotion_diagnostics import header
    from .cloud_daily_gap_probe import header as probe_header
    detail = header(job)+probe_header(job) if value == "complete" else ""
    check = job.get("revision_check")
    if value == "waiting_inputs" and check in {"single_tail_required", "price_chain", "timestamp", "receipt", "schema", "storage", "source_identity"}:
        detail += ";revision_check="+check
    revised = job.get("revised_histories")
    if value in {"complete", "collecting", "running"} and type(revised) is int and 0 <= revised <= counts[1]:
        detail += f";revised={revised}"
    for name, label in (("histories_reused", "reused"), ("histories_computed_this_run", "computed_now")):
        count = job.get(name)
        if value in {"complete", "collecting"} and type(count) is int and 0 <= count <= counts[0]:
            detail += f";{label}={count}"
    pit_data = job.get("pit_archive")
    pit = pit_data.get("status") if isinstance(pit_data, dict) else None
    if value == "complete" and pit in {"captured", "unchanged", "source_revision_conflict",
                                        "archive_invalid_preserved", "outside_same_day_capture_window",
                                        "archive_check_failed"}:
        detail += ";pit=" + pit
    return f"{value};histories={counts[0]};target={counts[1]};sessions={counts[2]};state={state}{identity}{detail}"


def pattern_route_health_header(status):
    job = (status.get("jobs") or {}).get("pattern_route") or {}
    value = job.get("status") if status.get("online") and job.get("status") in {
        "running", "complete", "waiting_inputs", "owned_elsewhere", "failed",
    } else "unverified"
    count = job.get("matched_conditions")
    count = str(count) if value == "complete" and type(count) is int and 0 <= count <= 100000 else "unknown"
    identity = job.get("implementation_sha256")
    suffix = (";implementation="+identity if isinstance(identity, str) and len(identity) == 64
              and all(c in "0123456789abcdef" for c in identity) else "")
    if value == "complete" and job.get("route_mode") in {"defense", "exit_review", "entry_research"}:
        suffix += ";mode=" + job["route_mode"]
    return f"{value};matched={count}{suffix}"


def run_job(name):
    if name == "research_experiments":
        from .research_workbench import run_pending

        return run_pending(ROOT)
    if name == "sector_observations":
        from .cloud_sector_observations import run

        return run()
    if name == "pattern_route":
        from .cloud_pattern_route import run

        return run()
    if name == "industry_daily":
        from .cloud_industry_daily import run

        return run(batch_limit=200)
    if name == "price_events":
        from .cloud_price_limit_events import run

        return run(batch_limit=20)
    if name == "market_data":
        from .cloud_market_data import run
        from .cloud_daily_epochs import IMPLEMENTATION_SHA256
        from .cloud_daily_tail_probe import reuse_provider_session, IMPLEMENTATION_SHA256 as tail_sha

        with reuse_provider_session() as session:
            result = run(batch_limit=200)
        return {**result, "publisher_implementation_sha256": IMPLEMENTATION_SHA256,
                "tail_transport": {"implementation_sha256": tail_sha,
                    **{key: session[key] for key in ("logins", "logouts", "queries", "logout_status")}}}
    if name == "decision_inputs":
        from .cloud_decision_inputs import run

        return run()
    if name == "pattern_inputs":
        from .cloud_pattern_inputs import run

        return run(batch_limit=100)
    if name == "pattern_features":
        from .cloud_pattern_features import run

        return run(batch_limit=100)
    if name == "market_emotion":
        from .cloud_market_emotion import run
        from .cloud_emotion_diagnostics import enrich
        from .cloud_daily_gap_probe import enrich as probe
        from .cloud_daily_revisions import prepare
        from .emotion_pit_archive import capture

        revision = prepare(read_json(ROOT / "reports/cloud_research_market_emotion.json"), root=ROOT)
        if revision["status"] == "waiting_inputs":
            return revision
        report = probe(enrich(run(batch_limit=500)))
        if report.get("status") != "complete":
            return report
        try:
            archive = capture(ROOT, report)
        except (OSError, ValueError, KeyError, TypeError) as error:
            archive = {"status": "archive_check_failed", "reason": str(error)}
        return {**report, "pit_archive": archive}
    from factor_system import source_gate

    if name == "audit":
        catalog = source_gate.load_catalog()
        verification = source_gate.verify_catalog(catalog)
        candidates = [source_gate.verify_candidate(row, catalog) for row in catalog["candidates"]]
        return {
            "status": "complete" if verification["valid"] else "waiting_inputs",
            "summary": f"来源目录{'通过' if verification['valid'] else '缺少完整证据'}；候选 {len(candidates)} 项，可运行 {sum(row['runnable'] for row in candidates)} 项。按情绪选择战法，因子仅辅助排序。",
            "input_errors": verification["errors"],
        }
    if name == "sources":
        from factor_system import source_scout, source_review_queue

        if source_scout.STATE_PATH.is_file():
            source_review_queue.sync(source_state_path=source_scout.STATE_PATH)
        if not source_scout.due({}, state_path=source_scout.STATE_PATH):
            return {"status": "scheduled", "summary": "本轮搜索尚未到期；沿用持久化的每日频率和失败退避。"}
        result = source_scout.run()
        source_review_queue.sync(source_state_path=source_scout.STATE_PATH)
        return {
            "status": "complete" if result.get("ok") else "retry_pending",
            "summary": f"本轮扫描 {result.get('items_scanned', 0)} 项来源，新增 {result.get('new_count', 0)} 项；新来源须完成机制与输入审核。",
            "next_due_at": result.get("next_due_at"),
        }
    if name == "factors":
        # The existing engine owns all admission, once-only tests and recovery.
        # A clean server is not allowed to invent the missing migration history.
        from factor_system.epoch_migration import verify_daemon_epoch_ready

        try:
            verify_daemon_epoch_ready()
        except (RuntimeError, OSError, ValueError):
            return {"status": "waiting_inputs", "summary": "严格研究批次尚未完成云端初始化与证据核验，因子计算未启动。"}
        if not source_gate.verify_catalog()["valid"]:
            return {"status": "waiting_inputs", "summary": "来源目录证据不完整，因子计算未启动。"}
        from factor_system import daemon

        code = daemon.main(once=True)
        return {"status": "complete" if code == 0 else "failed", "summary": f"因子调度轮结束，退出码 {code}；计算与验证结论见因子研究明细。"}
    if name == "strategies":
        from mom_trading import user_strategy_lab as lab

        state = lab.batch_status()
        if state.get("running"):
            return {"status": "owned_elsewhere", "summary": "已有进程执行策略批次，本执行器不重复启动。"}
        if state.get("status") != "interrupted":
            return {"status": "waiting_hypothesis", "summary": "没有待恢复的冻结策略批次；新战法需先登记适用情绪、规则、成本和验证方案。"}
        result = lab.start_batch_generation()
        if not result.get("ok"):
            return {"status": "waiting_inputs", "summary": "冻结策略批次未能恢复，需检查输入身份或研究写锁。"}
        while lab._BATCH_ACTIVE.is_set():
            time.sleep(1)
        completed = lab.batch_status()
        return {"status": "complete" if completed.get("batch_complete") else "interrupted", "summary": f"冻结批次已处理 {completed.get('done', 0)}/{completed.get('total', 0)} 项；投产仍需完整证据。"}
    raise ValueError("unknown research job")


def main(*, once=False):
    directory = ROOT / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    lock = (directory / "cloud_research.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return 0
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    state = read_json(STATUS)
    state.update(status="running", pid=os.getpid(), execution_role=os.environ.get("REGIS_EXECUTION_ROLE", "local"))
    state["sector_timer_managed"] = (state["execution_role"] == "cloud"
                                     and os.environ.get("REGIS_SECTOR_TIMER_MANAGED") == "1")
    state.setdefault("jobs", {})

    def heartbeat():
        if state.get("current_job") and state["current_job"] == JOBS.get("industry_daily"):
            progress = read_json(directory / "cloud_industry_daily_progress.json")
            if (progress.get("schema") == "cloud_industry_daily.v1"
                    and progress.get("job_run_id") == state["jobs"]["industry_daily"].get("job_run_id")):
                state["jobs"]["industry_daily"].update({k: progress[k] for k in (
                    "captured", "classified", "target", "adopted", "observation_date", "implementation_sha256", "summary",
                ) if k in progress})
        if state.get("current_job") and state.get("current_job") == JOBS.get("market_data"):
            progress = read_json(directory / "v4_daily_download_progress.json")
            inputs = read_json(directory / "cloud_market_data.json")
            done, total = progress.get("completed_symbols"), progress.get("target_symbols")
            if (inputs.get("schema") == "cloud_owned_daily_inputs.v1"
                and inputs.get("bootstrap_complete") is not True
                and inputs.get("target_date") == (progress.get("query") or {}).get("end_date")
                and type(done) is int and type(total) is int and 0 <= done <= total):
                state["jobs"]["market_data"].update(
                    downloaded=done, target=total,
                    summary=f"已下载 {done}/{total} 只日线，目标 {inputs['target_date']}；下载进度不代表质量审计通过。",
                )
        if state.get("current_job") and state["current_job"] == JOBS.get("pattern_inputs"):
            progress = read_json(directory / "cloud_pattern_inputs_progress.json")
            if (progress.get("schema") == "cloud_pattern_input_receipts.v1"
                    and progress.get("job_run_id") == state["jobs"]["pattern_inputs"].get("job_run_id")):
                state["jobs"]["pattern_inputs"].update({k: progress[k] for k in (
                    "captured", "empty", "target", "industry_rows", "industry_empty", "industry_captured", "data_as_of", "summary",
                ) if k in progress})
        if state.get("current_job") and state["current_job"] == JOBS.get("pattern_features"):
            progress = read_json(directory / "cloud_pattern_features_progress.json")
            if (progress.get("schema") == "cloud_pattern_features.v1"
                    and progress.get("job_run_id") == state["jobs"]["pattern_features"].get("job_run_id")):
                state["jobs"]["pattern_features"].update({k: progress[k] for k in (
                    "price_windows", "target", "factor_finite_counts", "cross_section_status", "data_as_of", "summary",
                ) if k in progress})
        if state.get("current_job") and state["current_job"] == JOBS.get("market_emotion"):
            progress = read_json(directory / "cloud_market_emotion_progress.json")
            if (progress.get("schema") == "cloud_market_emotion.v1"
                    and progress.get("job_run_id") == state["jobs"]["market_emotion"].get("job_run_id")):
                state["jobs"]["market_emotion"].update({k: progress[k] for k in (
                    "histories_computed", "target", "classified_sessions", "latest_state", "data_as_of", "summary",
                ) if k in progress})
        state["heartbeat_at"] = stamp()
        publish(STATUS, state)

    child = None
    try:
        while not stop.is_set():
            publication_wait = False
            for job, label in JOBS.items():
                if stop.is_set():
                    break
                if job == "sector_observations" and state["sector_timer_managed"]:
                    continue
                with source_lease() as descriptors:
                    if descriptors is None:
                        publication_wait = True
                        break
                    previous = state["jobs"].get(job, {})
                    run_id = uuid4().hex
                    state["current_job"] = label
                    state["jobs"][job] = {**state["jobs"].get(job, {}), "label": label, "status": "running", "started_at": stamp(), "job_run_id": run_id}
                    heartbeat()
                    output = directory / f"cloud_research_{job}.json"
                    child = subprocess.Popen(
                        [sys.executable, "-m", "mom_trading.cloud_research_worker", "--job", job],
                        cwd=ROOT, stdin=subprocess.DEVNULL, pass_fds=descriptors,
                        env={**os.environ, "REGIS_RESEARCH_JOB_ID": run_id},
                    )
                    deadline = time.monotonic() + 1800
                    while child.poll() is None and not stop.wait(10):
                        heartbeat()
                        if time.monotonic() >= deadline:
                            break
                    if child.poll() is None:
                        child.terminate()
                        try:
                            child.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait()
                    result = read_json(output) if child.returncode == 0 else {
                        "status": "failed", "summary": f"任务中断或超时，退出码 {child.returncode}；下轮按原有证据与断点核验。",
                    }
                    if child.returncode == 0 and result.get("job_run_id") != run_id:
                        result = {"status": "failed", "summary": "本轮任务未发布匹配的结果身份，不复用旧成功记录。"}
                    completed = result.get("status") == "complete"
                    state["jobs"][job] = {
                        **result, "label": label, "checked_at": stamp(),
                        "finished_at": stamp() if completed else previous.get("finished_at"),
                        "last_completed_summary": result.get("summary") if completed else previous.get("last_completed_summary"),
                    }
                    heartbeat()
            if not publication_wait:
                state["last_cycle_at"] = stamp()
            state["current_job"] = "等待版本安全切换；已完成任务保留" if publication_wait else "等待下一轮输入检查"
            heartbeat()
            if once:
                break
            collecting = any((state["jobs"].get(name) or {}).get("status") == "collecting"
                             for name in ("industry_daily", "market_data", "pattern_inputs", "pattern_features", "market_emotion"))
            interval_steps = 1 if publication_wait else (3 if collecting else 30)
            for _ in range(interval_steps):
                if stop.wait(10):
                    break
                heartbeat()
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
        state.update(status="stopped", current_job=None)
        heartbeat()
        lock.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--job", choices=JOBS)
    args = parser.parse_args()
    if args.job:
        try:
            result = run_job(args.job)
        except Exception as exc:
            result = {"status": "failed", "summary": f"任务执行失败：{type(exc).__name__}；详见服务日志。"}
            import traceback
            traceback.print_exc()
        result["job_run_id"] = os.environ.get("REGIS_RESEARCH_JOB_ID") or uuid4().hex
        publish(ROOT / "reports" / f"cloud_research_{args.job}.json", result)
    else:
        raise SystemExit(main(once=args.once))
