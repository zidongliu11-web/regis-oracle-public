"""Process-lifetime helpers for the 5200 service."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from research.web_supervisor import rotate_log_if_needed


_ROTATOR_LOCK = threading.Lock()
_ROTATORS: set[Path] = set()
_ROTATOR_WORKERS: dict[Path, tuple[threading.Event, threading.Thread]] = {}


class BackgroundThreadGroup:
    """Own the recurring 5200 threads and stop them within one deadline."""

    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self, target, *, name: str) -> threading.Thread:
        thread = threading.Thread(
            target=target, args=(self.stop_event,), daemon=True, name=name,
        )
        try:
            thread.start()
        except Exception:
            self.stop()
            raise
        self.threads.append(thread)
        return thread

    def stop(self, timeout_seconds: float = 5.0) -> None:
        self.stop_event.set()
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        for thread in self.threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    def health_snapshot(self) -> dict[str, object]:
        """Expose owned thread liveness without leaking thread objects."""
        workers = {
            thread.name: bool(thread.is_alive())
            for thread in self.threads
        }
        return {
            "stopping": self.stop_event.is_set(),
            "workers": workers,
            "alive_count": sum(1 for alive in workers.values() if alive),
            "worker_count": len(workers),
        }


def stop_app_background(app, timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    workers = (
        app.extensions.get("background_threads"),
        app.extensions.get("quant_data_scheduler"),
        app.config.get("MARKET_INTELLIGENCE"),
    )
    for worker in workers:
        stop = getattr(worker, "stop", None)
        if stop:
            stop(timeout_seconds=max(0.0, deadline - time.monotonic()))


def start_log_rotator(
    path: Path,
    *,
    interval_seconds: float = 60.0,
    max_bytes: int = 10 * 1024 * 1024,
    backups: int = 3,
) -> bool:
    """Start at most one copy-truncate rotator per process and log path."""
    target = path.resolve()
    with _ROTATOR_LOCK:
        if target in _ROTATORS:
            return False
        _ROTATORS.add(target)

    stop_event = threading.Event()

    def run() -> None:
        while not stop_event.is_set():
            try:
                rotate_log_if_needed(
                    target, max_bytes=max_bytes, backups=backups,
                )
            except OSError:
                # A transient filesystem error must not take down the web app.
                pass
            if stop_event.wait(interval_seconds):
                break

    thread = threading.Thread(
        target=run, daemon=True, name="olympus-log-rotator",
    )
    try:
        thread.start()
    except Exception:
        with _ROTATOR_LOCK:
            _ROTATORS.discard(target)
        raise
    with _ROTATOR_LOCK:
        _ROTATOR_WORKERS[target] = (stop_event, thread)
    return True


def stop_log_rotators(timeout_seconds: float = 2.0) -> None:
    with _ROTATOR_LOCK:
        workers = list(_ROTATOR_WORKERS.values())
        _ROTATOR_WORKERS.clear()
        _ROTATORS.clear()
    for stop_event, _thread in workers:
        stop_event.set()
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    for _stop_event, thread in workers:
        thread.join(max(0.0, deadline - time.monotonic()))
