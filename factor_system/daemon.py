"""M2.3 24h 常驻 supervisor（launchd KeepAlive，单进程顺序执行）。

8GB 单机不真并行：来源审查 / 复现 / 回测按需顺序切换，面板只载一份。

循环决策：
  1. 面板 staleness：cache key 变化 → 重载新数据并只重验一次
  2. 内存守卫：当前 RSS > DAEMON_MEM_LIMIT_GB → 暂缓并记日志
  3. 来源目录与预注册审查：只允许已冻结的公开研究候选进入复现
  4. 回测消费：registered 队列非空即触发（不等待日历时刻）
  5. 前向复检：新数据身份到达且存在 historical_pass → 每个身份触发一次
  6. 无可运行候选时持续复核来源目录和实现队列，不制造随机公式

公开资料只生成候选，不生成 production 资格。目录中没有新预注册假设时，
supervisor 保持 24×7 心跳；连续三轮零候选且失败分布不变后，
等待新数据、新搜索空间版本或人工新假设，不会为算力利用率重跑同一搜索。
nice 降权对 5200 与 autonomous_worker 让路。

CLI: python -m factor_system.daemon [--once] [--rounds N]
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import gc
import hashlib
import json
import os
import signal
import sys
import threading
import time
import datetime
from pathlib import Path

from . import config
from . import status
from .data import load_last_good_panel, load_panel, _panel_cache_path
from .gate import research_input_key
from .novelty import NoveltyArchive
from .preprocess import build_exposures
from . import registry as reg


_STOP = False
_RUNTIME_FILE = config.STATE_DIR / "daemon_runtime.json"
_WATCHDOG_HEARTBEAT_SECONDS = 30
_WATCHDOG_TIMEOUT_SECONDS = max(
    900, int(os.environ.get("FACTOR_DAEMON_MAX_ROUND_SECONDS", "21600")),
)
_PROGRESS_LOCK = threading.Lock()
_PROGRESS_MONOTONIC = time.monotonic()
_PROGRESS_AT = ""

# 2026-08-14 延迟整改：交易时段对 5200 更让路。nice 单调只升不降（非 root 无法降回来），
# 交易时段升到 _NICE_TRADING=20，非交易时段保持 _NICE_BASE=10（升过一次后维持 20，无害——
# 挖掘无面向用户的截止时间，降权只会更让路）。
_NICE_BASE = 10
_NICE_TRADING = 20
_CUR_NICE: int = 0


def _ensure_nice(level: int) -> None:
    """单调调优：只升不降。"""
    global _CUR_NICE
    if level <= _CUR_NICE:
        return
    try:
        os.nice(level - _CUR_NICE)
    except Exception:
        pass
    _CUR_NICE = level


def _trading_session() -> bool:
    """A股交易时段（周一~五 09:15–15:00 北京时间）。"""
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 555 <= minutes <= 900  # 09:15=555, 15:00=900


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _mark_progress() -> None:
    global _PROGRESS_MONOTONIC, _PROGRESS_AT
    with _PROGRESS_LOCK:
        _PROGRESS_MONOTONIC = time.monotonic()
        _PROGRESS_AT = _now()


def _watchdog_loop(stop_event: threading.Event) -> None:
    """主循环长时间无进度时退出，由 launchd KeepAlive 拉起干净实例。"""
    while not stop_event.wait(_WATCHDOG_HEARTBEAT_SECONDS):
        with _PROGRESS_LOCK:
            age = max(0, int(time.monotonic() - _PROGRESS_MONOTONIC))
            progress_at = _PROGRESS_AT
        status.touch_daemon_heartbeat(
            progress_at=progress_at, progress_age_seconds=age,
        )
        if age <= _WATCHDOG_TIMEOUT_SECONDS:
            continue
        detail = (
            f"主循环 {age}s 无进度，超过 {_WATCHDOG_TIMEOUT_SECONDS}s；"
            "watchdog 退出进程并交由 launchd 重启"
        )
        status.set_daemon_status(
            "error", phase="watchdog_restart", current_zone="watchdog",
            detail=detail, last_error=detail,
        )
        _log(detail)
        os._exit(75)


def _acquire_daemon_lock(path=None):
    """非阻塞取得跨进程单例锁；已被占用时返回 None。"""
    lock_path = Path(path) if path is not None else config.STATE_DIR / "daemon.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()}\nacquired_at={_now()}\n")
    handle.flush()
    return handle


def _release_daemon_lock(handle) -> None:
    """释放由 _acquire_daemon_lock 返回的锁句柄。"""
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _runtime() -> dict:
    if _RUNTIME_FILE.exists():
        try:
            return json.loads(_RUNTIME_FILE.read_text("utf-8"))
        except Exception:
            return {}
    return {}


def _save_runtime(rt: dict):
    try:
        _RUNTIME_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _RUNTIME_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(rt, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, _RUNTIME_FILE)
    except Exception:
        pass


_ZERO_INFO_COUNT_KEYS = (
    "compute_fail", "l0_fail", "l1_fail", "l2_fail", "dup_ic_fail",
    "incr_fail", "novelty_reject", "l3_fail", "validated",
)


def _source_inputs_fingerprint() -> str:
    """Hash the catalog plus declared admission/implementation artifacts."""
    try:
        catalog = json.loads(config.SOURCE_CATALOG.read_text("utf-8"))
        candidates = catalog.get("candidates") or []
        paths: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            preregistration = candidate.get("preregistration") or {}
            if isinstance(preregistration, dict):
                paths.update(
                    value for key, value in preregistration.items()
                    if key.endswith("_path") and isinstance(value, str)
                )
            feasibility = candidate.get("input_feasibility") or {}
            if isinstance(feasibility, dict) and isinstance(
                feasibility.get("path"), str,
            ):
                paths.add(feasibility["path"])
            implementation = candidate.get("implementation") or {}
            if isinstance(implementation, dict):
                paths.update(
                    implementation[key] for key in ("path", "audit_path")
                    if isinstance(implementation.get(key), str)
                )
                paths.update(
                    item["path"] for item in (implementation.get("files") or [])
                    if isinstance(item, dict) and isinstance(item.get("path"), str)
                )

        root = config.PROJECT_ROOT.resolve()
        artifacts = {}
        for relative in sorted(paths):
            candidate_path = Path(relative)
            if candidate_path.is_absolute() or ".." in candidate_path.parts:
                artifacts[relative] = "invalid_path"
                continue
            path = (root / candidate_path).resolve()
            if not path.is_relative_to(root):
                artifacts[relative] = "invalid_path"
                continue
            try:
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                artifacts[relative] = digest.hexdigest()
            except Exception as exc:
                artifacts[relative] = f"{type(exc).__name__}"
        payload = {"catalog": catalog, "artifacts": artifacts}
    except Exception as exc:
        payload = {"catalog_error": f"{type(exc).__name__}:{exc}"}
    body = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _research_identity(data_key: str) -> dict:
    """Inputs whose real change is allowed to wake zero-information search."""
    return {
        "data_key": str(data_key or ""),
        "atom_version": config.DAEMON_ATOM_VERSION,
        "grammar_version": config.DAEMON_GRAMMAR_VERSION,
        "source_inputs_sha256": _source_inputs_fingerprint(),
    }


def _zero_info_observation(result: dict) -> tuple[int, dict, str]:
    """Return output count and a stable, batch-id-free failure distribution."""
    search = result.get("search") if isinstance(result.get("search"), dict) else {}
    raw_output = search.get("output_candidates", result.get("n_candidates", 0))
    try:
        output_candidates = max(0, int(raw_output or 0))
    except (TypeError, ValueError):
        output_candidates = 0
    source = (
        result.get("source_gate")
        if isinstance(result.get("source_gate"), dict) else {}
    )
    distribution = {
        "blocked": bool(result.get("blocked")),
        "status": str(result.get("status") or ""),
        "reason": str(result.get("reason") or ""),
        "counts": {
            key: int(result.get(key, 0) or 0)
            for key in _ZERO_INFO_COUNT_KEYS
        },
        "search": {
            str(key): value
            for key, value in sorted(search.items())
            if key != "output_candidates"
            and isinstance(value, (str, int, float, bool, type(None)))
        },
        "source_gate": {
            key: source.get(key)
            for key in (
                "candidate_count", "declared_candidate_count",
                "runnable_count", "blocked_candidate_count",
                "implementation_pending_count", "data_pending_count",
                "blind_rejected_count",
                "source_runnable", "source_blocked",
            )
            if key in source
        },
    }
    signature = hashlib.sha256(json.dumps(
        distribution, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=True,
    ).encode("utf-8")).hexdigest()
    return output_candidates, distribution, signature


def _clear_zero_info_state(rt: dict) -> None:
    for key in (
        "zero_info_rounds", "zero_info_identity", "zero_info_failure_sha256",
        "zero_info_failure_distribution", "zero_info_phase",
        "zero_info_wait_since", "zero_info_next_action",
    ):
        rt.pop(key, None)


def _record_zero_info_round(rt: dict, identity: dict, result: dict) -> str | None:
    """Persist consecutive identical zero-output rounds and return wait phase."""
    output, distribution, signature = _zero_info_observation(result)
    if output > 0:
        _clear_zero_info_state(rt)
        return None
    source = result.get("source_gate") if isinstance(result.get("source_gate"), dict) else {}
    same = (
        rt.get("zero_info_identity") == identity
        and rt.get("zero_info_failure_sha256") == signature
    )
    rt["zero_info_rounds"] = int(rt.get("zero_info_rounds", 0)) + 1 if same else 1
    rt["zero_info_identity"] = identity
    rt["zero_info_failure_sha256"] = signature
    rt["zero_info_failure_distribution"] = distribution
    feedback = (
        result.get("failure_feedback")
        if isinstance(result.get("failure_feedback"), dict) else {}
    )
    action = feedback.get("next_action")
    if isinstance(action, str) and action.strip():
        rt["zero_info_next_action"] = action.strip()
    else:
        rt.pop("zero_info_next_action", None)
    terminal_exhausted = (
        int(source.get("blind_rejected_count", 0) or 0) > 0
        and int(source.get(
            "runnable_count", source.get("source_runnable", 0),
        ) or 0) == 0
        and sum(int(source.get(key, 0) or 0) for key in (
            "implementation_pending_count", "data_pending_count",
        )) == 0
    )
    if (not terminal_exhausted
            and rt["zero_info_rounds"] < config.DAEMON_ZERO_INFO_ROUNDS):
        rt.pop("zero_info_phase", None)
        rt.pop("zero_info_wait_since", None)
        return None
    runnable = source.get("runnable_count", source.get("source_runnable"))
    pending = sum(
        int(source.get(key, 0) or 0)
        for key in ("implementation_pending_count", "data_pending_count")
    )
    phase = (
        "needs_new_hypothesis"
        if runnable is not None and int(runnable or 0) == 0 and pending == 0
        else "await_new_input"
    )
    rt["zero_info_phase"] = phase
    rt.setdefault("zero_info_wait_since", _now())
    return phase


def _zero_info_wait_phase(rt: dict, identity: dict | None) -> str | None:
    if not identity or rt.get("zero_info_identity") != identity:
        return None
    if int(rt.get("zero_info_rounds", 0)) < config.DAEMON_ZERO_INFO_ROUNDS:
        return None
    phase = rt.get("zero_info_phase")
    return phase if phase in {"await_new_input", "needs_new_hypothesis"} else None


def _zero_info_wait_detail(rt: dict) -> str:
    detail = (
        f"连续 {int(rt.get('zero_info_rounds', 0))} 轮候选产出为 0，"
        "失败分布和研究输入均未变；不再重跑搜索"
    )
    action = str(rt.get("zero_info_next_action") or "").strip()
    return f"{detail}；下一实验：{action}" if action else detail


def _wait_log_due(rt: dict, phase: str, rounds: int) -> bool:
    """Log a stable wait state once on entry and then at most hourly."""
    last_phase = rt.get("last_wait_log_phase")
    last_round = int(rt.get("last_wait_log_round", -60) or -60)
    if phase == last_phase and rounds - last_round < 60:
        return False
    rt["last_wait_log_phase"] = phase
    rt["last_wait_log_round"] = rounds
    return True


def _sync_source_review_queue(rt: dict, queue_syncer=None) -> None:
    """Synchronize the lightweight source-only queue without network access."""
    if queue_syncer is None:
        from . import source_review_queue, source_scout

        queue_syncer = lambda: source_review_queue.sync(
            source_state_path=source_scout.STATE_PATH,
        )
    try:
        queue = queue_syncer()
        summary = queue.get("summary") or {}
        rt["source_review_queue"] = {
            key: int(summary.get(key, 0) or 0) for key in (
                "task_count", "pending_count", "overdue_count",
                "resolved_count", "paper_pending_count",
                "repository_pending_count",
            )
        }
        rt.pop("source_review_queue_error", None)
    except Exception as exc:
        rt["source_review_queue_error"] = f"{type(exc).__name__}: {exc}"[:300]


def _source_scout_if_due(
    rt: dict, phase: str, runner=None, queue_syncer=None,
) -> dict | None:
    """Run the due network scout; source-only queue sync is independently callable."""
    from . import source_scout

    # An operator may run one authoritative pass outside the daemon. Production
    # scheduling therefore also honors the persisted attempt; injected test
    # runners remain isolated from workstation state.
    state_path = source_scout.STATE_PATH if runner is None else None
    result = None
    if source_scout.due(rt, state_path=state_path):
        attempted_at = _now()
        rt["last_source_scout_attempt_at"] = attempted_at
        rt["last_source_scout_zone"] = phase
        try:
            result = (runner or source_scout.run)()
        except Exception as exc:
            result = {
                "ok": False,
                "status": "unavailable",
                "last_attempt_at": attempted_at,
                "new_count": 0,
                "unreviewed_count": 0,
                "repository_discovery_unreviewed_count": 0,
                "errors": [{"error": f"{type(exc).__name__}: {exc}"[:300]}],
            }
        rt["last_source_scout_result"] = {
            key: result.get(key) for key in (
                "status", "last_attempt_at", "last_success_at", "next_due_at",
                "items_scanned", "new_count", "unreviewed_count",
                "repository_discovery_unreviewed_count", "errors",
            )
        }
        if result.get("ok") is True:
            rt["last_source_scout_success_at"] = (
                result.get("last_success_at") or attempted_at
            )

    # Compatibility for isolated callers/tests. The daemon loop invokes queue
    # sync separately so a network scout can never sit ahead of priority work.
    if queue_syncer is not None:
        _sync_source_review_queue(rt, queue_syncer)
    return result


def _source_scout_for_zone(
    rt: dict, zone: str, *, priority_work_done: bool, runner=None,
) -> dict | None:
    """Run waiting-zone scouts immediately, work-zone scouts only after work."""
    waiting = zone in {"await_new_input", "needs_new_hypothesis"}
    work = zone in {"source_research", "backtest", "revalidate"}
    if waiting or (work and priority_work_done):
        return _source_scout_if_due(rt, zone, runner=runner)
    return None


def _log_source_scout_result(scout: dict | None) -> None:
    if scout is None:
        return
    pending_sources = (
        int(scout.get("unreviewed_count", 0) or 0)
        + int(scout.get("repository_discovery_unreviewed_count", 0) or 0)
    )
    _log(
        "公开来源侦察: "
        f"{scout.get('status', 'unknown')} · "
        f"扫描{int(scout.get('items_scanned', 0) or 0)} · "
        f"新增{int(scout.get('new_count', 0) or 0)} · "
        f"待来源审查{pending_sources}；"
        "只进入持久来源审查队列，不注册因子或触发回测"
    )


def _mem_guard() -> tuple[bool, float]:
    """当前 RSS（GB）。超限 → (True, gb)。psutil 不可用降级 maxrss。"""
    try:
        import psutil
        rss = psutil.Process(os.getpid()).memory_info().rss / 2 ** 30
        return rss > config.DAEMON_MEM_LIMIT_GB, rss
    except Exception:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 ** 2)
        return rss / 1024 > config.DAEMON_MEM_LIMIT_GB, rss / 1024


def _registry_probe(query: str, params: tuple = ()):
    """执行只读调度探针；数据库异常必须阻止调度，不能伪装成空队列。"""
    connection = None
    try:
        connection = reg._conn(readonly=True)
        return connection.execute(query, params).fetchone()
    except Exception as exc:
        raise RuntimeError(f"因子注册表调度探针失败: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def _count_registered() -> int:
    row = _registry_probe(
            "SELECT COUNT(*) AS n FROM factors "
            "WHERE status IN ('registered','backtesting') "
            "AND research_epoch=?",
            (config.RESEARCH_EPOCH,),
    )
    return int(row["n"])


def _has_historical_pass() -> bool:
    """当前 epoch 有等待新数据前向终测的因子时返回 True。"""
    row = _registry_probe(
            "SELECT 1 FROM factors "
            "WHERE status='historical_pass' AND research_epoch=? LIMIT 1",
            (config.RESEARCH_EPOCH,),
    )
    return row is not None


def _has_current_production() -> bool:
    """当前 epoch 有 production 因子时才需要数据/评估器重验。"""
    row = _registry_probe(
            "SELECT 1 FROM factors "
            "WHERE status='production' AND research_epoch=? LIMIT 1",
            (config.RESEARCH_EPOCH,),
    )
    return row is not None


def _preflight_current_production_sources() -> None:
    """复核 production 冻结配方与来源，必须先于任何 Panel I/O。"""
    from .production import _verified_recipe

    connection = reg._conn(readonly=True)
    try:
        rows = connection.execute(
            "SELECT * FROM factors WHERE status='production' AND research_epoch=?",
            (config.RESEARCH_EPOCH,),
        ).fetchall()
    finally:
        connection.close()
    for row in rows:
        _verified_recipe(dict(row))


def _has_pending_mining() -> bool:
    """只读判断是否有必须先恢复/保守终结的挖掘候选族。"""
    row = _registry_probe(
            "SELECT 1 FROM mining_batches "
            "WHERE status='pending' AND research_epoch=? LIMIT 1",
            (config.RESEARCH_EPOCH,),
    )
    return row is not None


def _has_pending_observation() -> bool:
    """共享 LOND 有孤儿/待恢复观察时，强制调度回测恢复器。"""
    prefix = f"{config.RESEARCH_EPOCH}:%"
    row = _registry_probe(
            "SELECT 1 FROM test_observation_latches "
            "WHERE epoch LIKE ? LIMIT 1", (prefix,),
    )
    return row is not None


def _source_research_round() -> dict:
    """复核公开来源目录，且只运行已预注册的精确复现候选。"""
    from .pipeline import run_mining
    return run_mining(
        include_genetic=False,
        template_grid=True,
        exposures=None,
        novelty=None,
        atom_frames=None,
        panel=None,
    )


def _backtest_round() -> dict:
    from .pipeline import run_backtesting
    # watchdog 仍在同一进程时也要恢复上一轮已提交的 backtesting 状态；
    # 不能只在 daemon 冷启动时恢复，否则任务会永久离开 registered 队列。
    reg.recover_interrupted_backtests()
    return run_backtesting()


def _record_successful_backtest(rt: dict, data_key: str, result: dict) -> None:
    """仅在回测完整成功后消费该数据身份。"""
    if not data_key or not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("回测批未确认成功，不推进数据身份")
    rt["last_backtest"] = _now()
    if int(result.get("forward_remaining_eligible") or 0) > 0:
        # 单批限额只控制每轮耗时，不能让第 201 个已成熟因子等到下一份数据。
        # 保留旧 key 后，调度器下一轮会继续消费剩余前向终测队列。
        return
    rt["last_backtest_data_key"] = data_key


def _revalidate_round(panel, expected_input_key: str | None = None) -> dict:
    """新数据到达后执行一次：科学闸门重验 → 生命周期 → 相关去重。"""
    from .dedupe import dedupe_production_factors
    from .lifecycle import check_freeze_production, revalidate_production
    from .production import refresh_authorized_catalog
    # 三项都是 production 健康检查的必需步骤。任一失败必须让整轮抛错，主循环
    # 才不会写入 last_revalidate_data_key；下一轮会对同一数据版本重试。
    rv = revalidate_production(
        panel=panel, expected_input_key=expected_input_key,
    )
    if rv.get("errors"):
        raise RuntimeError(
            f"production 科学重验未完整完成: {len(rv['errors'])} 个错误"
        )
    lc = check_freeze_production(panel=panel)
    if lc.get("errors"):
        raise RuntimeError(
            f"production 生命周期监测未完整完成: {len(lc['errors'])} 个错误"
        )
    dd = dedupe_production_factors(panel=panel)
    if dd.get("errors"):
        raise RuntimeError(
            f"production 相关去重未完整完成: {len(dd['errors'])} 个错误"
        )
    authorization = refresh_authorized_catalog(panel, expected_input_key)
    if authorization.get("ok") is not True:
        raise RuntimeError("production 授权目录未确认刷新成功")
    return {
        "ok": True,
        "revalidate": {"checked": len(rv.get("checked", [])),
                       "passed": rv.get("passed", []),
                       "no_go": rv.get("no_go", [])},
        "lifecycle": {"checked": len(lc.get("checked", [])),
                      "frozen": lc.get("frozen", []),
                      "kept": lc.get("kept", [])},
        "dedupe": {"frozen": dd.get("frozen", []),
                   "groups": len(dd.get("groups", []))},
        "production_catalog": authorization,
    }


def _log(msg: str):
    line = f"[daemon {_now()}] {msg}"
    print(line, flush=True)


def _signal_handler(signum, _frame):
    global _STOP
    _STOP = True
    _log(f"收到信号 {signum}，优雅退出")


def main(once: bool = False, max_rounds: int | None = None):
    """取得 daemon 单例所有权，恢复中断任务，再进入常驻循环。"""
    lock = _acquire_daemon_lock()
    if lock is None:
        _log(f"已有 daemon 实例持有 {config.STATE_DIR / 'daemon.lock'}，本实例退出")
        return 0
    try:
        # 必须先以只读方式证明严格 epoch 已完成可恢复迁移，再允许任何 registry
        # 恢复、DDL 或挖掘写入。局部 import 避免 epoch_migration 复用锁工具时循环导入。
        try:
            from .epoch_migration import verify_daemon_epoch_ready
            epoch_guard = verify_daemon_epoch_ready()
        except Exception as exc:
            detail = f"严格 epoch 启动校验失败: {exc}"
            status.set_daemon_status(
                "error", phase="epoch_guard", current_zone="startup",
                detail=detail, errors=1, last_error=str(exc)[:300],
            )
            _log(f"{detail}；未恢复任务、未迁表、未启动挖掘")
            return 2
        _log(
            f"严格 epoch 启动校验通过 · {epoch_guard['research_epoch']} · "
            f"归档 {epoch_guard['archive_dir']}"
        )
        recovered = reg.recover_interrupted_backtests()
        _log(
            f"单例锁已获取 · 启动恢复检查：backtesting 遗留任务 "
            f"{recovered['count']} 个"
        )
        _mark_progress()
        watchdog_stop = threading.Event()
        watchdog = threading.Thread(
            target=_watchdog_loop,
            args=(watchdog_stop,),
            name="factor-daemon-watchdog",
            daemon=True,
        )
        watchdog.start()
        try:
            return _run_daemon(once=once, max_rounds=max_rounds)
        finally:
            watchdog_stop.set()
            watchdog.join(timeout=2)
    finally:
        _release_daemon_lock(lock)


def _run_daemon(once: bool = False, max_rounds: int | None = None):
    global _STOP
    _STOP = False
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # 对 5200 / autonomous_worker 让路（launchd 启动即降权；交易时段再升到 20）
    _ensure_nice(_NICE_BASE)

    rt = _runtime()
    rt.setdefault("started_at", _now())
    rt.setdefault("rounds", 0)
    rt.setdefault("errors", 0)
    rt.setdefault("last_source_audit", "")
    rt.setdefault("last_backtest", "")
    rt.setdefault("last_backtest_data_key", "")
    rt.setdefault("last_revalidate", "")
    rt.setdefault("last_revalidate_data_key", "")
    rt.setdefault("last_revalidate_evaluator_fingerprint", "")

    from .production import (
        evaluator_fingerprint as _evaluator_fingerprint,
        process_evaluator_fingerprint as _process_evaluator_fingerprint,
    )
    process_evaluator_fingerprint = _process_evaluator_fingerprint()

    _log("daemon 启动")
    _mark_progress()
    panel = None
    loaded_panel_key = None
    current_panel_key = _panel_cache_path().name
    loaded_key = research_input_key(panel_key=current_panel_key)
    archive = NoveltyArchive()
    _log(
        f"当前输入 {loaded_key} · Panel 按任务惰性加载 · "
        f"新颖性归档 {archive.total_explored()} 条"
    )
    _mark_progress()

    def ensure_panel():
        """Only data-consuming zones materialize and validate the full Panel."""
        nonlocal panel, loaded_panel_key, loaded_key
        requested_key = _panel_cache_path().name
        if panel is not None and loaded_panel_key == requested_key:
            loaded_key = research_input_key(panel_key=loaded_panel_key)
            return panel
        if panel is not None:
            load_panel.cache_clear()
            gc.collect()
        try:
            candidate = load_panel()
            candidate_key = getattr(candidate, "_panel_cache_key", None)
            if not candidate_key:
                raise RuntimeError("loaded Panel 缺少实际数据快照 key")
            post_load_key = _panel_cache_path().name
            if candidate_key != post_load_key:
                raise RuntimeError("Panel 加载期间数据源持续变化")
        except Exception as current_error:
            if panel is None:
                try:
                    panel = load_last_good_panel(exclude=requested_key)
                except Exception as fallback_error:
                    detail = (
                        f"当前 Panel 不可信且没有历史可信快照: {current_error}; "
                        f"fallback={fallback_error}"
                    )
                    status.set_daemon_status(
                        "error", phase="ingestion_blocked", current_zone="startup",
                        detail=detail, errors=int(rt.get("errors", 0)) + 1,
                        last_error=detail[:300],
                    )
                    raise RuntimeError(detail) from current_error
            loaded_panel_key = getattr(panel, "_panel_cache_key", None)
            loaded_key = research_input_key(panel_key=loaded_panel_key)
            rt["ingestion_blocker"] = {
                "code": "panel_ingestion_blocked",
                "message": f"新数据快照校验失败，继续使用最近可信 Panel: {current_error}",
                "at": _now(),
                "requested_panel_key": requested_key,
                "using_panel_key": loaded_panel_key,
            }
            _save_runtime(rt)
            _log(rt["ingestion_blocker"]["message"])
            return panel
        panel = candidate
        loaded_panel_key = candidate_key
        loaded_key = research_input_key(panel_key=loaded_panel_key)
        rt.pop("ingestion_blocker", None)
        _save_runtime(rt)
        _log(f"Panel {loaded_key} 已按需加载（{len(panel.codes)} 只）")
        _mark_progress()
        return panel

    loop_round = 0
    while not _STOP:
        _mark_progress()
        if _evaluator_fingerprint() != process_evaluator_fingerprint:
            detail = "production evaluator 源码已更新，退出并由 launchd 重启后重新加载"
            status.set_daemon_status(
                "running", phase="maintenance", current_zone="evaluator_reload",
                detail=detail, rounds=int(rt.get("rounds", 0)),
                errors=int(rt.get("errors", 0)),
                novelty_today=archive.count_novel_today(),
                next_plan="重启后立即重验 production 并刷新授权目录",
            )
            _log(detail)
            return 75
        loop_round += 1
        # 交易时段提升 nice（对 5200 waitress / Chrome 让路），非交易时段保持 10
        _ensure_nice(_NICE_TRADING if _trading_session() else _NICE_BASE)
        rounds = int(rt.get("rounds", 0)) + 1
        rt["rounds"] = rounds
        rt["updated_at"] = _now()
        _save_runtime(rt)

        # ── 输入 staleness：先看身份，只有实际消费数据的 zone 才加载 Panel ──
        current_panel_key = _panel_cache_path().name
        current_input_key = research_input_key(panel_key=current_panel_key)
        if current_input_key != loaded_key:
            _log("检测到研究输入身份更新，按调度任务决定是否加载数据")
            loaded_key = current_input_key
        current_research_identity = _research_identity(loaded_key)

        # ── 内存守卫 ──
        over_mem, rss_gb = _mem_guard()
        if over_mem:
            # 释放可回收对象；若 RSS 仍无法回到上限内，就退出并交给
            # launchd KeepAlive 拉起干净进程。
            gc.collect()
            over_mem, rss_gb = _mem_guard()
        if over_mem:
            status.set_daemon_status(
                "running", phase="maintenance", current_zone="memory_recovery",
                detail=(f"内存 {rss_gb:.2f}GB 超过上限 "
                        f"{config.DAEMON_MEM_LIMIT_GB}GB，释放缓存后仍超限，"
                        "由 launchd 立即重启干净进程"),
                mem_gb=rss_gb, rounds=rounds,
                errors=int(rt.get("errors", 0)),
                novelty_today=archive.count_novel_today(),
                next_plan="重启后立即重新调度真实任务",
            )
            _log(f"内存守卫：RSS {rss_gb:.2f}GB 释放缓存后仍超限，退出交由 launchd 重启")
            return 75
        status.set_daemon_status(
            "running", phase="scheduling", current_zone="guard",
            detail=f"内存 {rss_gb:.2f}GB / 上限 {config.DAEMON_MEM_LIMIT_GB}GB",
            mem_gb=rss_gb, rounds=rounds, errors=int(rt.get("errors", 0)),
            novelty_today=archive.count_novel_today(),
            next_plan="立即选择下一个真实研究任务",
        )

        # ── 决策并执行下一区 ──
        # 注册表探针也是调度的一部分；读取失败必须进入明确 error 并短重试，
            # 不能把“数据库不可读”误解成“没有队列”后继续来源复现。
        zone = "registry_read"
        round_failed = False
        _mark_progress()
        try:
            zone = _decide_zone(
                rt, archive, loaded_key, process_evaluator_fingerprint,
                research_identity=current_research_identity,
            )
            if zone == "revalidate":
                _preflight_current_production_sources()
                panel = ensure_panel()
                current_research_identity = _research_identity(loaded_key)
                zone = _decide_zone(
                    rt, archive, loaded_key, process_evaluator_fingerprint,
                    research_identity=current_research_identity,
                )
            next_plan = _plan_str(zone, archive)
            waiting_phase = zone if zone in {
                "await_new_input", "needs_new_hypothesis",
            } else None
            status.set_daemon_status(
                "running", phase=waiting_phase or "work", current_zone=zone,
                detail=next_plan, mem_gb=rss_gb, rounds=rounds,
                errors=int(rt.get("errors", 0)),
                novelty_today=archive.count_novel_today(), next_plan=next_plan,
                wait_reason=_zero_info_wait_detail(rt) if waiting_phase else "",
                wait_identity=(current_research_identity if waiting_phase else None),
                zero_info_rounds=(int(rt.get("zero_info_rounds", 0))
                                  if waiting_phase else 0))
            _sync_source_review_queue(rt)
            _log_source_scout_result(_source_scout_for_zone(
                rt, zone, priority_work_done=False,
            ))
            if zone == "source_research":
                res = _source_research_round()
                rt["last_source_audit"] = _now()
                waiting_phase = _record_zero_info_round(
                    rt, current_research_identity, res,
                )
                source = res.get("source_gate") or {}
                _log(
                    f"来源审查轮 {res.get('batch_id')}: "
                    f"目录{source.get('candidate_count', 0)} 条 · "
                    f"可运行{source.get('runnable_count', 0)} · "
                     f"本轮候选{res.get('n_candidates', 0)} · "
                     f"入库{res.get('validated', 0)}"
                )
            elif waiting_phase:
                res = None
                if _wait_log_due(rt, waiting_phase, rounds):
                    _log(_zero_info_wait_detail(rt))
            elif zone == "backtest":
                res = _backtest_round()
                # 只有整轮成功（含 historical_pass 前向扫描）才消费
                # 该数据身份；锁冲突或任何异常会在下轮原样重试。
                _record_successful_backtest(rt, loaded_key, res)
                _log(f"回测批 {res.get('batch_id')}: 队列{res.get('n_queued')} "
                     f"投产{res.get('n_production', 0)} 拒{res.get('n_rejected', 0)}")
            elif zone == "revalidate":
                res = _revalidate_round(
                    panel, expected_input_key=loaded_key,
                )
                authorization = res.get("production_catalog") or {}
                if authorization.get("evaluator_fingerprint") != process_evaluator_fingerprint:
                    raise RuntimeError("production 授权 evaluator 身份在重验期间发生变化")
                rt["last_revalidate"] = _now()
                rt["last_revalidate_data_key"] = loaded_key
                rt["last_revalidate_evaluator_fingerprint"] = process_evaluator_fingerprint
                rv = res.get("revalidate") or {}
                lc = res.get("lifecycle") or {}
                dd = res.get("dedupe") or {}
                # C4 看板：持久化本轮再验证结果（健康/衰减监测展示）
                # 注意 _revalidate_round 已把 checked 转为 len()（int），passed/no_go 是 list
                rt["last_revalidate_result"] = {
                    "checked": int(rv.get("checked", 0) or 0),
                    "passed": len(rv.get("passed", []) or []),
                    "no_go": len(rv.get("no_go", []) or []),
                    "frozen": len(lc.get("frozen", []) or []),
                    "dedupe_frozen": len(dd.get("frozen", []) or []),
                }
                _log(f"衰减再验证: 重验{rv.get('checked', 0)} PASS{len(rv.get('passed', []))} "
                     f"NO-GO{len(rv.get('no_go', []))} 冻结{len(lc.get('frozen', []))} "
                     f"去重冻结{len(dd.get('frozen', []))}")
            else:
                raise RuntimeError(f"未知调度区: {zone}")
            if zone == "source_research" and res:
                ss = res.get("search") or {}
                source = res.get("source_gate") or {}
                detail = (
                    f"来源目录{source.get('candidate_count', 0)} · "
                    f"已预注册可复现{source.get('runnable_count', 0)} · "
                    f"本轮候选{res.get('n_candidates', 0)} · "
                    f"L3入库{res.get('validated', 0)}"
                )
                metrics = {
                    "candidates": res.get("n_candidates", 0),
                    "counts": {k: res.get(k, 0) for k in (
                        "compute_fail", "l0_fail", "l1_fail", "l2_fail",
                        "dup_ic_fail", "incr_fail", "novelty_reject",
                        "l3_fail", "validated")},
                    "search": ss, "source_gate": source,
                }
                if res.get("blocked"):
                    status.set_zone_status(
                        "mining", "blocked", phase="source_gate",
                        batch_id=res.get("batch_id", zone), detail=detail,
                        metrics=metrics,
                    )
                else:
                    status.mark_success(
                        "mining", res.get("batch_id", zone), detail=detail,
                        success_rate=float(res.get("success_rate", 0.0) or 0.0),
                        metrics=metrics,
                    )
            elif zone == "backtest" and res:
                status.mark_success(
                    "backtest", res.get("batch_id", zone),
                    detail=(f"回测{res.get('n_queued', 0)} · 投产{res.get('n_production', 0)} · "
                            f"淘汰{res.get('n_rejected', 0)}"),
                    success_rate=(float(res.get("n_production", 0)) /
                                  max(1, int(res.get("n_queued", 0)))),
                    metrics={"queued": res.get("n_queued", 0),
                             "historical_pass": res.get("n_historical_pass", 0),
                             "production": res.get("n_production", 0),
                             "rejected": res.get("n_rejected", 0)})
            if waiting_phase:
                wait_detail = _zero_info_wait_detail(rt)
                status.set_zone_status(
                    "mining", "running", phase=waiting_phase,
                    detail=wait_detail,
                )
                status.set_daemon_status(
                    "running", phase=waiting_phase, current_zone=waiting_phase,
                    detail=wait_detail, mem_gb=rss_gb, rounds=rounds,
                    errors=int(rt.get("errors", 0)),
                    novelty_today=archive.count_novel_today(),
                    next_plan=_plan_str(waiting_phase, archive),
                    wait_reason=wait_detail,
                    wait_identity=current_research_identity,
                    zero_info_rounds=int(rt.get("zero_info_rounds", 0)),
                )
            _mark_progress()
        except Exception as e:
            round_failed = True
            # watchdog：单轮崩溃不整组退出。完整 traceback 进日志便于下轮定位
            # （曾见 1/75 轮广播错误 "shapes (82,) (84,)"，仅记 str(e) 无法定位）。
            import traceback as _tb
            _tb_str = _tb.format_exc()[-1500:]
            rt["errors"] = int(rt.get("errors", 0)) + 1
            rt["last_error"] = str(e)[:300]
            rt["last_error_tb"] = _tb_str
            _save_runtime(rt)
            status.set_daemon_status(
                "error", phase="failed", current_zone=zone, detail=f"轮次异常: {e}",
                mem_gb=rss_gb, rounds=rounds, errors=rt["errors"],
                novelty_today=archive.count_novel_today(), last_error=str(e)[:200])
            _log(f"轮次异常[{zone}]: {str(e)[:200]}（已记 daemon.json，继续运行）\n{_tb_str}")
        finally:
            if zone in {"source_research", "backtest", "revalidate"}:
                _log_source_scout_result(_source_scout_for_zone(
                    rt, zone, priority_work_done=True,
                ))
            gc.collect()
            _save_runtime(rt)

        if once or (max_rounds and rounds >= max_rounds):
            _log("达到轮数上限/once，退出")
            break

        # 轮间只保留短 pacing；成功轮提前公布下一个真实任务。
        # 失败轮保持 error 心跳，短间隔后由下轮重试。
        if not round_failed:
            waiting_phase = _zero_info_wait_phase(
                rt, current_research_identity,
            )
            if waiting_phase:
                wait_detail = _zero_info_wait_detail(rt)
                status.set_daemon_status(
                    "running", phase=waiting_phase,
                    current_zone=waiting_phase, detail=wait_detail,
                    mem_gb=rss_gb, rounds=rounds,
                    errors=int(rt.get("errors", 0)),
                    novelty_today=archive.count_novel_today(),
                    next_plan=_plan_str(waiting_phase, archive),
                    wait_reason=wait_detail,
                    wait_identity=current_research_identity,
                    zero_info_rounds=int(rt.get("zero_info_rounds", 0)),
                )
            else:
                status.set_daemon_status(
                    "running", phase="pacing", current_zone="guard",
                    detail=f"轮间短间隔 {config.DAEMON_SLEEP_SHORT}s",
                    mem_gb=rss_gb, rounds=rounds,
                    errors=int(rt.get("errors", 0)),
                    novelty_today=archive.count_novel_today(),
                    next_plan="短间隔后立即重新读取队列并选择真实任务",
                )
                _log(
                    f"常驻 pacing {config.DAEMON_SLEEP_SHORT}s → "
                    "重新读取队列并选择真实任务"
                )
        else:
            _log(f"异常后短间隔 {config.DAEMON_SLEEP_SHORT}s 重试")
        time.sleep(config.DAEMON_SLEEP_SHORT)

    _log("daemon 退出")
    status.set_daemon_status("stopped", phase="exit", detail="进程退出",
                             rounds=int(rt.get("rounds", 0)),
                             errors=int(rt.get("errors", 0)),
                             novelty_today=archive.count_novel_today())
    return 0


def _decide_zone(rt: dict, archive: NoveltyArchive,
                 panel_key: str | None = None,
                 evaluator_key: str | None = None,
                 research_identity: dict | None = None) -> str:
    """顺序：恢复 → 新数据再验证 → 队列回测
    → historical_pass 新数据前向复检 → 来源目录审查/复现。

    同一静态面板重复跑完整科学闸门不会产生新证据，只浪费大量回测；因此
    revalidate 和 historical_pass 前向扫描分别只在 ``panel_key`` 变化
    后成功运行一次。任何异常都不推进对应 data key。
    """
    # pending family 已经读取过 discovery/validation，必须在任何可能改变
    # production 参考库的 revalidate/backtest 之前恢复；run_mining 会在输入
    # 漂移时保守 p=1/整族 NO-GO，因而不会把 daemon 永久锁死。
    if _has_pending_mining():
        return "source_research"
    if _has_pending_observation():
        return "backtest"
    has_production = _has_current_production()
    if (has_production and evaluator_key
            and rt.get("last_revalidate_evaluator_fingerprint") != evaluator_key):
        return "revalidate"

    if (has_production and panel_key
            and rt.get("last_revalidate_data_key") != panel_key):
        return "revalidate"
    if _count_registered() > 0:
        return "backtest"
    if (panel_key and rt.get("last_backtest_data_key") != panel_key
            and _has_historical_pass()):
        return "backtest"
    waiting_phase = _zero_info_wait_phase(rt, research_identity)
    if waiting_phase:
        return waiting_phase
    return "source_research"


def _plan_str(zone: str, archive: NoveltyArchive) -> str:
    if zone == "source_research":
        return "复核论文/GitHub 来源、许可和预注册；只复现已冻结候选"
    if zone == "revalidate":
        return "数据更新后一次性衰减再验证"
    if zone == "backtest":
        return "历史确认队列及新数据前向终测"
    if zone == "await_new_input":
        return "等待 Panel/目录/状态/准入/实现或搜索空间身份变化"
    if zone == "needs_new_hypothesis":
        return "每日一次公开论文元数据侦察；候选仅按明确公式、许可与PIT缺口推进"
    raise ValueError(f"未知调度区: {zone}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只跑一轮后退出")
    ap.add_argument("--rounds", type=int, default=None, help="最多跑 N 轮")
    args = ap.parse_args()
    sys.exit(main(once=args.once, max_rounds=args.rounds))
