"""双独立运行区状态（任务书第十部分·诚实适配）。

两个独立状态文件，供 5200 仪表盘读取，互不干扰：
  state/mining.json     挖掘区
  state/backtest.json   回测区

字段（任务书 10.2）：
  zone, status(running/error), phase, started_at, finished_at,
  current_batch, last_completed, last_success, throughput, queue_depth, success_rate,
  resources, last_error, detail
"""

from __future__ import annotations

import json
import os
import datetime
import threading

from . import config

_LOCK = threading.Lock()

_FAILURE_ACTIONS = {
    "compute_fail": "修复失败配方的数据字段或计算实现；有限值检查通过后再运行。",
    "l0_fail": "优先修复点时覆盖与横截面变化来源；L0 门槛保持不变。",
    "l1_fail": "终止当前规格并预注册不同经济机制；不得按 discovery 结果下调 IC 门槛。",
    "l2_fail": "封存当前假设；仅用未触碰的新机制建立下一实验，不复用 validation 调参。",
    "dup_ic_fail": "改用与现有因子不同的经济机制，不以参数微调绕过相关性去重。",
    "incr_fail": "停止该冗余候选，预注册能提供独立增量信息的新机制。",
    "novelty_reject": "从公开来源预注册不同经济机制的配方，不重跑已归档公式。",
    "l3_fail": "保留完整 FDR 失败族；下一轮只加入预注册新假设，不删失败项或放宽 q。",
}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _read(path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text("utf-8"))
        except Exception:
            return {}
    return {}


def _write(path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, path)


def _wall_seconds(started_at: str | None, completed_at: str) -> float | None:
    if not started_at:
        return None
    try:
        start = datetime.datetime.fromisoformat(started_at)
        end = datetime.datetime.fromisoformat(completed_at)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, (end - start).total_seconds()), 3)


def _mining_completion_metrics(metrics: dict, wall_seconds: float | None) -> dict:
    """Freeze the completed candidate funnel and one next experiment action."""
    out = dict(metrics)
    counts = dict(out.get("counts") or {})
    evaluated = max(0, int(out.get("candidates") or 0))
    compute_fail = max(0, int(counts.get("compute_fail") or 0))
    novelty_fail = max(0, int(counts.get("novelty_reject") or 0))
    novel = max(0, evaluated - compute_fail - novelty_fail)
    l0_pass = max(0, novel - int(counts.get("l0_fail") or 0))
    l1_pass = max(0, l0_pass - int(counts.get("l1_fail") or 0))
    l2_pass = max(0, l1_pass - sum(
        int(counts.get(key) or 0)
        for key in ("l2_fail", "dup_ic_fail", "incr_fail")
    ))
    out["funnel"] = {
        "evaluated": evaluated,
        "novel": novel,
        "l0_pass": l0_pass,
        "l1_pass": l1_pass,
        "l2_pass": l2_pass,
        "l3_pass": max(0, int(counts.get("validated") or 0)),
    }
    if wall_seconds is not None:
        out["wall_seconds"] = wall_seconds

    families = {
        key: max(0, int(counts.get(key) or 0))
        for key in _FAILURE_ACTIONS
        if int(counts.get(key) or 0) > 0
    }
    if families:
        dominant = max(
            _FAILURE_ACTIONS,
            key=lambda key: (families.get(key, 0), -list(_FAILURE_ACTIONS).index(key)),
        )
        next_action = _FAILURE_ACTIONS[dominant]
    elif evaluated == 0:
        dominant = "no_runnable_candidate"
        next_action = "等待新的预注册来源候选或真实输入身份变化；不得重跑同一假设制造吞吐。"
    else:
        dominant = None
        next_action = "将 L3 通过候选送入固定历史确认；继续沿用成本、FDR 与前向门禁。"
    out["failure_feedback"] = {
        "families": families,
        "dominant_family": dominant,
        "next_action": next_action,
        "gate_policy": "unchanged",
    }
    return out


def set_zone_status(zone: str, status: str, phase: str = "", detail: str = "",
                    batch_id: str = "", queue_depth: int = 0,
                    throughput: float = 0.0, success_rate: float = 0.0,
                    last_error: str = "", metrics: dict | None = None):
    """更新单区状态；旧 idle 调用统一收敛为 running/pacing。"""
    path = config.STATE_DIR / f"{zone}.json"
    with _LOCK:
        st = _read(path)
        stamp = _now()
        new_batch = bool(batch_id and batch_id != st.get("current_batch"))
        if new_batch:
            st["batch_started_at"] = stamp
        if status == "idle":
            status = "running"
            if phase in ("", "idle", "done"):
                phase = "pacing"
        if status == "running":
            st.update({
                "zone": zone, "status": "running", "phase": phase,
                "started_at": st.get("started_at", stamp),
                "finished_at": None,
                "detail": detail,
            })
        elif status in {"blocked", "error"}:
            st.update({
                "zone": zone, "status": status, "phase": phase,
                "finished_at": stamp, "detail": detail,
            })
        if batch_id:
            st["current_batch"] = batch_id
        st["queue_depth"] = queue_depth
        # A completed phase must preserve the measured rate until mark_success
        # freezes the full-batch rate; 0 here means "no live sample", not zero work.
        if new_batch or throughput > 0 or phase not in {"done", "completed", "pacing"}:
            st["throughput"] = throughput
        st["success_rate"] = success_rate
        if metrics is not None:
            measured = dict(metrics)
            wall = _wall_seconds(
                st.get("batch_started_at") or st.get("started_at"), stamp,
            )
            if zone == "mining":
                measured = _mining_completion_metrics(measured, wall)
            st["last_metrics"] = measured
            if wall is not None:
                st["last_batch_wall_seconds"] = wall
        if last_error:
            st["last_error"] = last_error
            st["last_error_at"] = stamp
        st["updated_at"] = stamp
        st["resources"] = _resources()
        _write(path, st)


def mark_success(zone: str, batch_id: str, detail: str = "", success_rate: float = 0.0,
                 metrics: dict | None = None):
    """记录轮次完成；只有新合格产物才推进 last_success。"""
    path = config.STATE_DIR / f"{zone}.json"
    with _LOCK:
        st = _read(path)
        completed_at = _now()
        measured = dict(metrics or {})
        wall = _wall_seconds(
            st.get("batch_started_at") or st.get("started_at"), completed_at,
        )
        if zone == "mining":
            measured = _mining_completion_metrics(measured, wall)
        if "produced" in measured:
            produced = bool(measured["produced"])
        elif zone == "mining":
            produced = int((measured.get("counts") or {}).get("validated", 0) or 0) > 0
        elif zone == "backtest":
            produced = (
                int(measured.get("historical_pass", 0) or 0) > 0
                or int(measured.get("production", 0) or 0) > 0
            )
        else:
            produced = False
        st.update({
            "zone": zone, "status": "running", "phase": "pacing",
            "finished_at": None,
            "current_batch": batch_id,
            "last_completed": completed_at,
            "last_completed_batch": batch_id,
            "last_batch_produced": produced,
            "detail": detail,
            "last_error": "",
        })
        if produced:
            st["last_success"] = completed_at
            st["last_success_batch"] = batch_id
        if metrics is not None:
            st["last_metrics"] = measured
        if wall is not None:
            st["last_batch_wall_seconds"] = wall
        if zone == "mining" and wall is not None:
            evaluated = int((measured.get("funnel") or {}).get("evaluated") or 0)
            st["throughput"] = round(evaluated / max(1.0, wall), 3)
        st["success_rate"] = success_rate
        st["updated_at"] = _now()
        st["resources"] = _resources()
        _write(path, st)
        return measured


def read_zone(zone: str) -> dict:
    return _read(config.STATE_DIR / f"{zone}.json")


def set_daemon_status(status: str, phase: str = "", detail: str = "",
                      current_zone: str = "", started_at: str = "",
                      mem_pct: float = 0.0, mem_gb: float = 0.0,
                      rounds: int = 0, errors: int = 0,
                      novelty_today: int = 0, next_plan: str = "",
                      last_error: str = "", wait_reason: str = "",
                      wait_identity: dict | None = None,
                      zero_info_rounds: int = 0):
    """M2.4 daemon 心跳（state/daemon.json，仪表盘展示）。"""
    path = config.STATE_DIR / "daemon.json"
    with _LOCK:
        st = _read(path)
        if status == "idle":
            status = "running"
            phase = "pacing"
        if current_zone == "sleep":
            current_zone = "source_research"
        st.update({
            "daemon": status,            # running / error / stopped
            "phase": phase,
            "detail": detail,
            "current_zone": current_zone,
            "started_at": started_at or st.get("started_at", _now()),
            "updated_at": _now(),
            "heartbeat_at": _now(),
            "mem_pct": round(mem_pct, 1),
            "mem_gb": round(mem_gb, 2),
            # 兼容旧看板读取，每次心跳都显式清除旧低功耗态。
            "idle_since": "",
            "low_power": False,
            "rounds": rounds,
            "errors": errors,
            "novelty_today": novelty_today,
            "next_plan": next_plan,
            "wait_reason": wait_reason,
            "wait_identity": wait_identity or {},
            "zero_info_rounds": max(0, int(zero_info_rounds)),
            "resources": _resources(),
        })
        if last_error:
            st["last_error"] = last_error
            st["last_error_at"] = _now()
        _write(path, st)


def read_daemon() -> dict:
    return _read(config.STATE_DIR / "daemon.json")


def touch_daemon_heartbeat(*, progress_at: str, progress_age_seconds: int) -> None:
    """独立 watchdog 心跳；不改写当前任务/阶段事实。"""
    path = config.STATE_DIR / "daemon.json"
    with _LOCK:
        st = _read(path)
        st["heartbeat_at"] = _now()
        st["progress_at"] = progress_at
        st["progress_age_seconds"] = max(0, int(progress_age_seconds))
        _write(path, st)


def _resources() -> dict:
    try:
        import psutil
        mem = psutil.virtual_memory()
        return {
            "cpu_pct": psutil.cpu_percent(interval=None),
            "mem_pct": mem.percent,
            "mem_used_gb": round(mem.used / 2 ** 30, 2),
            "mem_total_gb": round(mem.total / 2 ** 30, 2),
        }
    except Exception:
        try:
            # 无 psutil 时降级
            return {"mem_note": "psutil unavailable"}
        except Exception:
            return {}


def default_state() -> dict:
    """无区状态文件时等待常驻 supervisor 首轮心跳。"""
    return {
        "zone": "mining", "status": "running", "phase": "pacing",
        "detail": "等待常驻 supervisor 调度首个真实批次",
    }
