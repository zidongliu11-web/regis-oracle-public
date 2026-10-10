"""Observable fail-closed guard for provider calls that run in Python threads."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from itertools import count
from typing import Callable


_SEQUENCE = count(1)


def bounded_call(
    callback: Callable[[], object],
    timeout_seconds: float,
    *,
    slots: threading.BoundedSemaphore,
    lock: threading.Lock,
    timed_out: set[int],
    telemetry: dict[str, object],
):
    """Return on time without pretending Python can cancel the provider thread."""
    with lock:
        if timed_out:
            telemetry["circuit_rejections"] = int(
                telemetry.get("circuit_rejections") or 0
            ) + 1
            return False, None, RuntimeError(
                "上游调用熔断中：上一超时任务仍未退出，请稍后重试"
            )
    if not slots.acquire(blocking=False):
        with lock:
            telemetry["capacity_rejections"] = int(
                telemetry.get("capacity_rejections") or 0
            ) + 1
        return False, None, RuntimeError("上游调用已达并发上限，请稍后重试")

    token = next(_SEQUENCE)
    state: dict[str, object] = {}
    done = threading.Event()
    with lock:
        active = telemetry.setdefault("active", {})
        if isinstance(active, dict):
            active[token] = time.monotonic()

    def run() -> None:
        try:
            state["value"] = callback()
        except Exception as exc:
            state["error"] = exc
        finally:
            with lock:
                recovered = token in timed_out
                timed_out.discard(token)
                active = telemetry.get("active")
                if isinstance(active, dict):
                    active.pop(token, None)
                if recovered:
                    telemetry["last_recovered_at"] = _now()
                done.set()
            slots.release()

    try:
        threading.Thread(
            target=run, daemon=True, name="bounded-provider-call",
        ).start()
    except Exception:
        with lock:
            active = telemetry.get("active")
            if isinstance(active, dict):
                active.pop(token, None)
        slots.release()
        raise

    if not done.wait(timeout_seconds):
        with lock:
            if not done.is_set():
                timed_out.add(token)
                telemetry["timeouts_total"] = int(
                    telemetry.get("timeouts_total") or 0
                ) + 1
                telemetry["last_timeout_at"] = _now()
                return False, None, TimeoutError(
                    f"上游数据源超过 {timeout_seconds:g} 秒未返回"
                )
    if "error" in state:
        return False, None, state["error"]
    return True, state.get("value"), None


def health_snapshot(
    *, lock: threading.Lock, timed_out: set[int], telemetry: dict[str, object],
) -> dict[str, object]:
    with lock:
        active = telemetry.get("active")
        active_count = len(active) if isinstance(active, dict) else 0
        stuck = len(timed_out)
        return {
            "ready": stuck == 0,
            "status": "ready" if stuck == 0 else "circuit_open",
            "active_calls": active_count,
            "timed_out_calls_still_running": stuck,
            "timeouts_total": int(telemetry.get("timeouts_total") or 0),
            "capacity_rejections": int(
                telemetry.get("capacity_rejections") or 0
            ),
            "circuit_rejections": int(
                telemetry.get("circuit_rejections") or 0
            ),
            "last_timeout_at": telemetry.get("last_timeout_at"),
            "last_recovered_at": telemetry.get("last_recovered_at"),
            "hard_cancel_supported": False,
            "native_timeout_required": True,
            "unresolved_boundary": (
                "Python 线程不能安全强制终止；未暴露原生 connect/read timeout "
                "的 provider 超时后会保持熔断，直至该调用自行退出。"
            ),
        }


def native_timeout_health(*providers: object) -> dict[str, object]:
    contracts = []
    for provider in providers:
        snapshot = (
            provider.health_snapshot()
            if provider is not None and hasattr(provider, "health_snapshot")
            else {}
        )
        contracts.extend(snapshot.get("native_timeout_contracts") or [])
    unresolved = [
        row for row in contracts
        if row.get("native_timeout_enforced") is not True
    ]
    return {
        "ready": bool(contracts) and not unresolved,
        "contracts": contracts,
        "unresolved": unresolved,
    }


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
