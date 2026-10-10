"""Durable, bounded historical experiments using the existing replay engine.

This queue never edits production registries, accounts, source protocols or
trial admission. A completed diagnostic may conclude that inputs are unresolved.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

from flask import Blueprint, current_app, jsonify, request, send_file
from .workbench_artifact_paths import research_artifact_path

ROOT = Path(__file__).resolve().parents[1]
KIND = "emotion_pattern_event_diagnostic"
UNIVERSE_KIND = "emotion_universe_feature_cache"
CASH_KIND = "emotion_cash_conditioned_diagnostic"
CONTROL_KIND = "emotion_control_increment_diagnostic"
LIQUIDITY_KIND = "emotion_control_liquidity_repair"
DISTRIBUTION_KIND = "emotion_control_distribution_repair"
DERIVED_KINDS = ("emotion_conditioned_diagnostic", "emotion_factor_increment_diagnostic")
KINDS = {KIND: "情绪战法 · 成交路径回放",
         "emotion_conditioned_diagnostic": "情绪与当日涨跌 · 完整信号评价",
         "emotion_factor_increment_diagnostic": "五项因子 · 分情绪增量诊断",
         UNIVERSE_KIND: "完整股票池 · 五项因子研究输入",
         CASH_KIND: "现金结算后 · 分情绪战法与五项因子评价",
         CONTROL_KIND: "完整对照组 · 情绪、形态与因子增量",
         LIQUIDITY_KIND: "完整对照组 · 停牌输入与公司行动核验",
         DISTRIBUTION_KIND: "完整对照组 · 实施公告与真实股数结算"}
SOURCE_FILES = (
    "mom_trading/research_workbench.py", "mom_trading/emotion_pattern_research.py",
    "mom_trading/research_artifact_paths.py", "mom_trading/workbench_artifact_paths.py",
    "mom_trading/emotion_pattern_replay.py", "research/run_emotion_pattern_replay.py",
    "mom_trading/sentiment_rising_industry_momentum.py", "quantqc/ashare.py", "quantqc/costs.py",
    "requirements.lock", ".python-version", "scripts/verify_runtime_dependencies.py",
)
ANALYSIS_SOURCES = ("mom_trading/research_workbench.py", "mom_trading/conditioned_research.py",
                    "mom_trading/emotion_factor_attribution.py", "mom_trading/emotion_pattern_research.py",
                    "mom_trading/research_artifact_paths.py", "mom_trading/workbench_artifact_paths.py", "requirements.lock", ".python-version",
                    "scripts/verify_runtime_dependencies.py")
CASH_SOURCES = ANALYSIS_SOURCES + (
    "mom_trading/cash_conditioned_research.py", "mom_trading/cash_dividend_evidence.py",
    "mom_trading/crowding_cash_settlement.py", "mom_trading/emotion_pattern_cash_settlement.py",
    "research/run_emotion_pattern_replay.py", "research/run_emotion_control_cash_settlement.py",
)
CONTROL_SOURCES = CASH_SOURCES + ("mom_trading/control_increment_research.py", "research/run_emotion_pattern_controls.py")
LIQUIDITY_SOURCES = tuple(dict.fromkeys(CONTROL_SOURCES + SOURCE_FILES + (
    "mom_trading/control_liquidity_research.py", "mom_trading/suspension_liquidity.py",
    "mom_trading/control_corporate_evidence.py",
    "research/run_emotion_pattern_cash_settlement.py")))
DISTRIBUTION_SOURCES = LIQUIDITY_SOURCES + ("mom_trading/distribution_settlement.py",
                                           "mom_trading/control_distribution_research.py")
BOUNDARY = {"research_only": True, "historical_reuse": True, "unseen_out_of_sample": False,
            "production_eligible": False, "generates_alert": False, "automatic_trading": False,
            "portfolio_return": None}
WRITABLE_ENDPOINTS = frozenset({"research_workbench.submit", "research_workbench.retry"})
blueprint = Blueprint("research_workbench", __name__)


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def body(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def storage(root):
    return root / "reports" / "research_workbench"


def input_location(root):
    configured = os.environ.get("REGIS_RESEARCH_INPUT_ROOT", "").strip()
    if configured and not Path(configured).is_absolute():
        raise ValueError("research_input_root_must_be_absolute")
    return Path(configured).resolve() if configured else root


def specification(kind=KIND, parent=None):
    from research.run_emotion_pattern_replay import INPUTS, ROOT as code_root
    from .emotion_pattern_research import research_catalog
    from .emotion_pattern_replay import CONTRACT, contract_identity

    if kind not in KINDS:
        raise ValueError("research_kind_invalid")
    if kind == DISTRIBUTION_KIND:
        from .control_distribution_research import ARTIFACTS, SPEC
        original = specification(LIQUIDITY_KIND)
        return {**original,"kind":kind,"title":KINDS[kind],
                "analysis_contract":{**original["analysis_contract"],"distribution_repair":SPEC},
                "inputs":original["inputs"]+[{"path":p,"sha256":sha} for p,sha in ARTIFACTS],
                "source_code":[{"path":p,"sha256":digest(ROOT/p)} for p in DISTRIBUTION_SOURCES]}
    if kind == LIQUIDITY_KIND:
        from .control_liquidity_research import ARTIFACTS, SPEC
        original = specification(CONTROL_KIND)
        return {**original, "kind": kind, "title": KINDS[kind],
                "analysis_contract": {**original["analysis_contract"], "execution_input_repair": SPEC},
                "inputs": original["inputs"] + [{"path": p, "sha256": sha} for p, sha in ARTIFACTS],
                "source_code": [{"path": p, "sha256": digest(ROOT / p)} for p in LIQUIDITY_SOURCES]}
    if kind in {CASH_KIND, CONTROL_KIND}:
        if kind == CASH_KIND:
            from .cash_conditioned_research import ARTIFACTS, SPEC
        else:
            from .control_increment_research import ARTIFACTS, SPEC
        return {"schema": "research_experiment.v4", "kind": kind, "title": KINDS[kind],
                "date_range": ["2021-01-04", "2025-12-31"], "analysis_contract": SPEC,
                "inputs": [{"path": path, "sha256": expected} for path, expected in ARTIFACTS]
                          + [{"path": str(path.relative_to(code_root)), "sha256": expected}
                             for path, expected in INPUTS[1:3]],
                "source_code": [{"path": name, "sha256": digest(ROOT / name)}
                                for name in (CASH_SOURCES if kind == CASH_KIND else CONTROL_SOURCES)],
                **BOUNDARY}
    if kind == UNIVERSE_KIND:
        from .emotion_universe_features import CONTRACT as cache_contract, MANIFEST, SOURCES

        return {"schema": "research_experiment.v3", "kind": kind, "title": KINDS[kind],
                "date_range": cache_contract["date_range"], "input_contract": cache_contract,
                "inputs": [{"path": str(path.relative_to(code_root)), "sha256": expected}
                           for path, expected in INPUTS[:3]] + [{"path": MANIFEST[0], "sha256": MANIFEST[1]}],
                "source_code": [{"path": name, "sha256": digest(ROOT / name)} for name in SOURCES],
                **BOUNDARY}
    if kind in DERIVED_KINDS:
        from .conditioned_research import SPEC
        if parent is None or parent["status"] != "completed" or parent["spec"]["kind"] != KIND:
            raise ValueError("research_completed_replay_required")
        return {"schema": "research_experiment.v2", "kind": kind, "title": KINDS[kind],
                "date_range": parent["spec"]["date_range"], "analysis_contract": SPEC,
                "parent": {"run_id": parent["run_id"], "attempt": parent["attempt"], "result_sha": parent["result_sha"]},
                "catalog": parent["spec"]["catalog"], "inputs": parent["spec"]["inputs"][1:3],
                "source_code": [{"path": name, "sha256": digest(ROOT / name)} for name in ANALYSIS_SOURCES],
                **BOUNDARY}
    return {"schema": "research_experiment.v1", "kind": KIND,
            "title": "情绪战法与基础特征 · 固定历史路径诊断",
            "date_range": ["2021-01-04", "2025-12-31"],
            "hypothesis_scope": "既有三类入场假设的工程复现；保留关联失败家族，不登记新的独立盲测",
            "catalog": research_catalog(), "execution_contract": CONTRACT,
            "contract_sha256": contract_identity(),
            "inputs": [{"path": str(path.relative_to(code_root)), "sha256": expected}
                       for path, expected in INPUTS],
            "source_code": [{"path": name, "sha256": digest(ROOT / name)} for name in SOURCE_FILES],
            **BOUNDARY}


def preflight(root, spec=None):
    """Presence is cheap enough for GET; full hashing belongs to the worker."""
    spec = specification() if spec is None else spec
    base = input_location(root)
    inputs = []
    for ref in spec["inputs"]:
        path = research_artifact_path(ref["path"], project_root=base)
        inputs.append({**ref, "storage_path": str(path.relative_to(base.resolve())),
                       "present": path.is_file(), "bytes": path.stat().st_size if path.is_file() else None})
    return {"inputs": inputs, "all_inputs_present": all(row["present"] for row in inputs),
            "content_verified": False,
            "next_action": "可登记固定历史诊断；执行器先核验全部输入摘要。" if all(r["present"] for r in inputs)
            else "补齐清单中的原始研究输入；不把当前日线或合成夹具当成冻结历史数据。",
            "requires_mined_factor_pool": False, **BOUNDARY}


@contextmanager
def database(root):
    directory = storage(root)
    directory.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(directory / "experiments.sqlite3", timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("""CREATE TABLE IF NOT EXISTS experiments (
        run_id TEXT PRIMARY KEY, spec TEXT NOT NULL, status TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
        progress TEXT NOT NULL DEFAULT '{}', reason TEXT, result_sha TEXT, summary TEXT
    )""")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def record(row):
    return {key: (json.loads(row[key]) if row[key] else None) if key in {"spec", "progress", "summary"}
            else row[key] for key in row.keys()}


@contextmanager
def readonly_database(root):
    """Read the live index without mkdir, DDL, or a writable database handle.

    mode=ro retains WAL visibility; immutable would be unsafe for a live queue.
    SQLite may manage existing WAL sidecars. The PB01 metadata endpoint below
    never opens this queue and therefore has no such filesystem side effects.
    """
    path = storage(root) / "experiments.sqlite3"
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


def experiments(root):
    if not (storage(root) / "experiments.sqlite3").is_file():
        return []
    with readonly_database(root) as connection:
        return [record(row) for row in connection.execute(
            "SELECT * FROM experiments ORDER BY created_at DESC, run_id DESC LIMIT 30")]


def get(root, run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{24}", run_id):
        raise ValueError("research_run_id_invalid")
    if not (storage(root) / "experiments.sqlite3").is_file():
        raise ValueError("research_run_not_found")
    with readonly_database(root) as connection:
        row = connection.execute("SELECT * FROM experiments WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ValueError("research_run_not_found")
    return record(row)


def register(root, kind=KIND, parent_run_id=None):
    parent = get(root, parent_run_id) if parent_run_id is not None else None
    if kind not in DERIVED_KINDS and parent is not None:
        raise ValueError("research_replay_has_no_parent")
    spec = specification(kind, parent)
    if parent is not None:
        verified_result(root, spec["parent"])
    encoded = body(spec)
    run_id = hashlib.sha256(encoded.encode()).hexdigest()[:24]
    now = stamp()
    with database(root) as connection:
        connection.execute("INSERT OR IGNORE INTO experiments(run_id,spec,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                           (run_id, encoded, "queued", now, now))
    return get(root, run_id)


def current_spec(root, spec):
    parent = get(root, spec["parent"]["run_id"]) if "parent" in spec else None
    return specification(spec["kind"], parent)


def verified_result(root, reference):
    parent = get(root, reference["run_id"])
    if parent["status"] != "completed" or any(parent[k] != reference[k] for k in ("attempt", "result_sha")):
        raise ValueError("research_parent_identity_mismatch")
    path = storage(root) / parent["run_id"] / f"attempt-{parent['attempt']}" / "result.json"
    if digest(path) != reference["result_sha"]:
        raise ValueError("research_parent_result_hash_mismatch")
    return path


def verified_feature_panel(root, reference):
    """Resolve only the immutable, completed input panel, never a caller's path."""
    from .emotion_universe_features import CONTRACT, panel_schema
    import pyarrow.parquet as pq

    result_path = verified_result(root, reference)
    job = get(root, reference['run_id'])
    result = json.loads(result_path.read_text(encoding='utf-8'))
    if (job['spec']['kind'] != UNIVERSE_KIND or result.get('contract') != job['spec'].get('input_contract')
            or result.get('schema') != CONTRACT['schema'] or result.get('lane') != 'universe_features'
            or result.get('inputs') != job['spec']['inputs']
            or result.get('outcomes_computed') is not False or result.get('production_eligible') is not False):
        raise ValueError('research_universe_result_contract_invalid')
    artifact = result.get('artifact', {})
    if artifact.get('name') != 'features.parquet':
        raise ValueError('research_universe_artifact_name_invalid')
    path = result_path.with_name('features.parquet')
    if path.is_symlink() or digest(path) != artifact.get('sha256'):
        raise ValueError('research_universe_artifact_hash_mismatch')
    parquet = pq.ParquetFile(path)
    if (parquet.metadata.num_rows != artifact.get('rows') or path.stat().st_size != artifact.get('bytes')
            or not parquet.schema_arrow.equals(panel_schema(), check_metadata=False)):
        raise ValueError('research_universe_artifact_schema_mismatch')
    return path


def run_analysis(root, spec, output, base, progress):
    from .conditioned_research import analyze
    from research.run_emotion_pattern_replay import write_new

    path = verified_result(root, spec["parent"])
    prior = json.loads(path.read_text(encoding="utf-8"))
    parent = get(root, spec["parent"]["run_id"])
    if (prior.get("schema") != "emotion_pattern_event_diagnostic.v1"
            or prior.get("inputs") != parent["spec"]["inputs"]
            or prior.get("contract") != parent["spec"]["execution_contract"]
            or prior["contract"].get("event_cash_yuan") != 20000
            or prior.get("production_eligible") is not False
            or prior.get("candidate_count", -1) * 3 != len(prior["events"])
            or prior.get("costed_event_count") != len(prior["events"])):
        raise ValueError("research_parent_contract_invalid")
    state_path, calendar_path = [research_artifact_path(ref["path"], project_root=base) for ref in spec["inputs"]]
    state_document = json.loads(state_path.read_text(encoding="utf-8"))
    states = state_document["rows"]
    sessions = json.loads(calendar_path.read_text(encoding="utf-8"))["trading_dates"]
    progress(body({"phase": "conditional_evaluation", "events": len(prior["events"])}))
    result = analyze(prior["events"], states, sessions, spec["catalog"]["playbooks"],
                     lane="conditional" if spec["kind"] == "emotion_conditioned_diagnostic" else "factor")
    result.update(parent=spec["parent"], inputs=spec["inputs"], source_code=spec["source_code"],
                  market_scope=state_document.get("scope", "not_declared"), **BOUNDARY)
    # Recheck both the cached ledger and state inputs before publishing analysis.
    verified_result(root, spec["parent"])
    if any(digest(research_artifact_path(ref["path"], project_root=base)) != ref["sha256"] for ref in spec["inputs"]):
        raise ValueError("research_analysis_input_changed")
    write_new(output, result)
    return {k: v for k, v in result.items() if k not in {"daily_metrics", "frozen_factor_groups", "inputs", "source_code"}}


def update(root, run_id, **values):
    allowed = {"status", "attempt", "progress", "reason", "result_sha", "summary"}
    if not values or not set(values).issubset(allowed):
        raise ValueError("research_update_fields_invalid")
    values = {key: body(value) if key in {"progress", "summary"} else value for key, value in values.items()}
    values["updated_at"] = stamp()
    with database(root) as connection:
        connection.execute("UPDATE experiments SET " + ",".join(f"{key}=?" for key in values) + " WHERE run_id=?",
                           [*values.values(), run_id])


def retry_run(root, run_id):
    with database(root) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM experiments WHERE run_id=?", (run_id,)).fetchone()
        if row is None or row["status"] not in {"blocked", "failed", "interrupted"}:
            raise ValueError("research_retry_requires_incomplete_attempt")
        if row["spec"] != body(current_spec(root, json.loads(row["spec"]))):
            raise ValueError("research_implementation_changed_register_new_version")
        connection.execute("UPDATE experiments SET status='queued', reason=NULL, updated_at=? WHERE run_id=?",
                           (stamp(), run_id))
    return get(root, run_id)


def acquire_worker_lock(lock):
    """Nonblocking process lock; importing read-only routes also works on Windows."""
    if os.name == "nt":
        import msvcrt
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise BlockingIOError("research_worker_owned_elsewhere") from exc
            raise
    else:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def run_pending(root):
    """One worker owns the queue; interruptions keep their immutable attempt files."""
    from research.run_emotion_pattern_replay import run
    from scripts.verify_runtime_dependencies import verify

    directory = storage(root)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "worker.lock").open("a+") as lock:
        try:
            acquire_worker_lock(lock)
        except BlockingIOError:
            return {"status": "owned_elsewhere", "summary": "实验执行器已占用，不重复启动。"}
        with database(root) as connection:
            connection.execute("UPDATE experiments SET status='interrupted', reason='worker_interrupted', updated_at=? WHERE status='running'",
                               (stamp(),))
            row = connection.execute("SELECT * FROM experiments WHERE status='queued' ORDER BY created_at,run_id LIMIT 1").fetchone()
        if row is None:
            return {"status": "waiting_hypothesis", "summary": "尚无待执行的已登记实验。"}
        job = record(row)
        run_id, attempt = job["run_id"], job["attempt"] + 1
        attempt_dir = directory / run_id / f"attempt-{attempt}"
        update(root, run_id, status="running", attempt=attempt, reason=None,
               progress={"phase": "verify_inputs", "completed_inputs": 0})
        try:
            attempt_dir.mkdir(parents=True, exist_ok=False)
            (attempt_dir / "manifest.json").write_text(body({"run_id": run_id, "attempt": attempt,
                                                          "started_at": stamp(), "python_version": list(sys.version_info[:3]),
                                                          "spec": job["spec"]}) + "\n")
            if job["spec"] != current_spec(root, job["spec"]):
                raise ValueError("research_implementation_changed_register_new_version")
            if verify():
                raise ValueError("research_runtime_dependency_mismatch")
            base = input_location(root)
            for index, ref in enumerate(job["spec"]["inputs"]):
                path = research_artifact_path(ref["path"], project_root=base)
                if not path.is_file():
                    raise FileNotFoundError("research_input_missing:" + ref["path"])
                if digest(path) != ref["sha256"]:
                    raise ValueError("research_input_hash_mismatch:" + ref["path"])
                update(root, run_id, progress={"phase": "verify_inputs", "completed_inputs": index + 1})

            def progress(message, **_):
                value = json.loads(message)
                update(root, run_id, progress={key: value[key] for key in
                       ("phase", "period", "candidates", "instruments_done", "events", "stock_days", "completed_sessions") if key in value})

            output = attempt_dir / "result.json"
            if job["spec"]["kind"] in {CASH_KIND, CONTROL_KIND, LIQUIDITY_KIND, DISTRIBUTION_KIND}:
                if job["spec"]["kind"] == CASH_KIND:
                    from .cash_conditioned_research import run as cash_analysis
                elif job["spec"]["kind"] == LIQUIDITY_KIND:
                    from .control_liquidity_research import run as cash_analysis
                elif job["spec"]["kind"] == DISTRIBUTION_KIND:
                    from .control_distribution_research import run as cash_analysis
                else:
                    from .control_increment_research import run as cash_analysis
                summary = cash_analysis(output, input_root=base, references=job["spec"]["inputs"],
                                        source_code=job["spec"]["source_code"], progress=progress)
            elif job["spec"]["kind"] == UNIVERSE_KIND:
                from .emotion_universe_features import build
                summary = build(output, input_root=base, references=job["spec"]["inputs"], progress=progress)
            elif job["spec"]["kind"] == KIND:
                summary = run(output, progress=progress, input_root=base)
                summary["interpretation"] = "固定历史路径诊断已完成；每个事件单独分配研究预算，重叠事件不能合成为账户收益，也不代表战法通过验证。"
            else:
                summary = run_analysis(root, job["spec"], output, base, progress)
            if job["spec"] != current_spec(root, job["spec"]):
                raise ValueError("research_implementation_changed_during_run")
            summary.update(BOUNDARY)
            update(root, run_id, status="completed", progress={"phase": "completed"},
                   result_sha=digest(output), summary=summary)
            return {"status": "complete", "run_id": run_id, "summary": "固定历史实验完成；结果保留未成交与未解决路径，不授予策略资格。"}
        except (FileNotFoundError, ValueError) as exc:
            reason = str(exc)
            # The engine may include file paths, but only known manifest paths
            # and stable failure codes are returned to the browser.
            prefix = reason.split(":", 1)[0]
            safe = prefix if re.fullmatch(r"[a-z][a-z0-9_]{1,100}", prefix) else "research_input_or_contract_invalid"
            update(root, run_id, status="blocked", reason=safe,
                   progress={"phase": "blocked", "next_action": "查看输入清单；修复数据或契约后显式重试。"})
            return {"status": "waiting_inputs", "run_id": run_id, "summary": "实验被输入或契约阻塞：" + safe}
        except Exception as exc:
            update(root, run_id, status="failed", reason="research_execution_failed:" + type(exc).__name__)
            raise


def app_root():
    return Path(current_app.config.get("RESEARCH_WORKBENCH_ROOT", ROOT))


@blueprint.get("/api/research-workbench/catalogue")
def catalogue_status():
    """Existing source-bound metadata only; no registration or execution."""
    from .research_catalogue import read_catalogue
    from research.pb01_pipeline import BOUNDARY as catalogue_boundary
    try:
        response = jsonify(read_catalogue(app_root()))
    except (OSError, ValueError, TypeError, KeyError):
        response = jsonify({"status": "unavailable", "error": "研究登记缺失、不可读或来源校验未通过。",
                            **catalogue_boundary})
        response.status_code = 503
    response.headers["Cache-Control"] = "no-store"
    return response


@blueprint.get("/api/research-workbench/pb01")
def pb01_status():
    """Fixed project metadata only: no queue, registration, market rows or account."""
    from research.pb01_pipeline import current_preflight
    try:
        root = Path(current_app.config.get("PB01_PROJECT_ROOT", ROOT))
        return jsonify(current_preflight(root))
    except (OSError, ValueError, TypeError, KeyError):
        return jsonify({"error": "PB01 研究证据配置不可读；准入保持关闭。",
                        "economic_admitted": False, "production_eligible": False}), 503


@blueprint.get("/api/research-workbench")
def status():
    try:
        root = app_root()
    except (OSError, ValueError, TypeError):
        return jsonify({"error": "研究输入配置不可用，请检查运行目录配置。", **BOUNDARY}), 503
    payload = {"kind": KIND, "kinds": KINDS, **BOUNDARY}
    available = []
    for name, kind in (("preflight", KIND), ("universe_preflight", UNIVERSE_KIND),
                       ("cash_preflight", CASH_KIND), ("control_preflight", CONTROL_KIND),
                       ("liquidity_preflight", LIQUIDITY_KIND), ("distribution_preflight", DISTRIBUTION_KIND)):
        try:
            data = preflight(root, specification(kind))
            if not isinstance(data, dict) or not isinstance(data.get("inputs"), list):
                raise ValueError("invalid_preflight_response")
            payload[name] = {**data, "available": True}
            available.append(True)
        except (OSError, ValueError, TypeError, KeyError, ImportError, sqlite3.Error):
            payload[name] = {"available": False, "inputs": [], "all_inputs_present": False,
                             "content_verified": False, "error": "此项输入清单读取失败，未确认文件是否齐备。",
                             "next_action": "恢复此项来源后刷新；其他输入和历史目录分别核对。", **BOUNDARY}
            available.append(False)
    try:
        jobs = experiments(root)
        if not isinstance(jobs, list): raise ValueError("invalid_experiments_response")
        payload.update(experiments=jobs, experiments_state={"available": True})
        available.append(True)
    except (OSError, ValueError, TypeError, KeyError, ImportError, sqlite3.Error):
        payload.update(experiments=[], experiments_state={"available": False,
                       "error": "历史实验目录读取失败，不能解释为没有历史实验。"})
        available.append(False)
    # Failed observation is not missing input, and neither admits a strategy.
    # POST/result/features retain their independent authorization and checks.
    payload.update(status="available" if all(available) else "partial" if any(available) else "unavailable",
                   capabilities={"register": all(available), "retry_incomplete": all(available),
                                 "arbitrary_code": False, "account_write": False})
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response, 200 if any(available) else 503


@blueprint.post("/api/research-workbench/experiments")
def submit():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("kind"), str) or data["kind"] not in KINDS:
        return jsonify({"error": "请选择已列出的研究任务。"}), 400
    expected = {"kind", "parent_run_id"} if data["kind"] in DERIVED_KINDS else {"kind"}
    if set(data) != expected:
        return jsonify({"error": "研究任务参数不符合固定契约。"}), 400
    try:
        return jsonify({"experiment": register(app_root(), data["kind"], data.get("parent_run_id")), **BOUNDARY}), 202
    except (ValueError, OSError):
        return jsonify({"error": "研究规格或来源不可用；派生分析需要已完成且摘要正确的成交路径实验，请刷新核对来源。"}), 409


@blueprint.post("/api/research-workbench/experiments/<run_id>/retry")
def retry(run_id):
    if request.get_json(silent=True) != {}:
        return jsonify({"error": "重试不接受修改规格或输入路径。"}), 400
    try:
        return jsonify({"experiment": retry_run(app_root(), run_id), **BOUNDARY}), 202
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409


@blueprint.get("/api/research-workbench/experiments/<run_id>/result")
def result(run_id):
    try:
        job = get(app_root(), run_id)
        if job["status"] != "completed":
            return jsonify({"error": "实验尚未完成，不能读取成功结果。"}), 409
        path = storage(app_root()) / run_id / f"attempt-{job['attempt']}" / "result.json"
        if digest(path) != job["result_sha"]:
            return jsonify({"error": "结果摘要校验失败，停止展示。"}), 409
        return send_file(path, mimetype="application/json", as_attachment=True,
                         download_name=f"research-{run_id}.json")
    except (ValueError, OSError, sqlite3.Error):
        return jsonify({"error": "实验结果不存在或不可读。"}), 404


@blueprint.get("/api/research-workbench/experiments/<run_id>/features")
def features(run_id):
    try:
        job = get(app_root(), run_id)
        path = verified_feature_panel(app_root(), {key: job[key] for key in ('run_id', 'attempt', 'result_sha')})
        return send_file(path, mimetype='application/vnd.apache.parquet', as_attachment=True,
                         download_name=f'research-features-{run_id}.parquet')
    except (ValueError, OSError, sqlite3.Error):
        return jsonify({'error': '完整股票池因子输入尚未完成或完整性校验未通过。'}), 409


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--register", action="store_true", help="Register the fixed diagnostic before executing")
    args = parser.parse_args()
    if args.register:
        print(body({"registered": register(args.root)["run_id"]}), flush=True)
    print(body(run_pending(args.root)), flush=True)
