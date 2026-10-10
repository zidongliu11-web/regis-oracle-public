"""External liveness watchdog for the 24x7 factor daemon.

The daemon has an in-process progress watchdog; this separate LaunchAgent also
recovers a total process/GIL hang where no Python watchdog thread can run.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from . import config


CHECK_SECONDS = max(10, int(os.environ.get("FACTOR_WATCHDOG_CHECK_SECONDS", "30")))
HEARTBEAT_TIMEOUT_SECONDS = max(
    120, int(os.environ.get("FACTOR_WATCHDOG_HEARTBEAT_SECONDS", "180")),
)
PROGRESS_TIMEOUT_SECONDS = max(
    900, int(os.environ.get("FACTOR_DAEMON_MAX_ROUND_SECONDS", "21600")),
)
STARTUP_GRACE_SECONDS = max(
    120, int(os.environ.get("FACTOR_WATCHDOG_STARTUP_SECONDS", "300")),
)
RESUME_GRACE_SECONDS = max(
    60, int(os.environ.get("FACTOR_WATCHDOG_RESUME_SECONDS", "120")),
)
RESTART_COOLDOWN_SECONDS = max(
    STARTUP_GRACE_SECONDS,
    int(os.environ.get("FACTOR_WATCHDOG_RESTART_COOLDOWN_SECONDS", "600")),
)
SCHEDULER_GAP_SECONDS = max(CHECK_SECONDS * 2, CHECK_SECONDS + 15)
LOCK_PATH = config.STATE_DIR / "daemon_watchdog.lock"
DAEMON_LOCK_PATH = config.STATE_DIR / "daemon.lock"
DAEMON_STATE_PATH = config.STATE_DIR / "daemon.json"


def _timestamp(value: Any) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _age_seconds(value: Any, now: datetime.datetime) -> int | None:
    observed = _timestamp(value)
    if observed is None:
        return None
    if observed.tzinfo is not None and now.tzinfo is None:
        now = now.astimezone()
    elif observed.tzinfo is None and now.tzinfo is not None:
        observed = observed.replace(tzinfo=now.tzinfo)
    return max(0, int((now - observed).total_seconds()))


def _deadline_active(value: Any, now: datetime.datetime) -> bool:
    deadline = _timestamp(value)
    if deadline is None:
        return False
    if deadline.tzinfo is not None and now.tzinfo is None:
        now = now.astimezone()
    elif deadline.tzinfo is None and now.tzinfo is not None:
        deadline = deadline.replace(tzinfo=now.tzinfo)
    return now < deadline


def _resume_grace_deadline(
    previous_check_at: datetime.datetime | None,
    now: datetime.datetime,
) -> datetime.datetime | None:
    """Grant one recovery window after sleep or severe scheduler suspension."""
    gap = _age_seconds(previous_check_at, now)
    if gap is None or gap <= SCHEDULER_GAP_SECONDS:
        return None
    return now + datetime.timedelta(seconds=RESUME_GRACE_SECONDS)


def restart_reason(
    state: dict[str, Any], *, acquired_at: Any, now: datetime.datetime,
    resume_grace_until: Any = None, last_restart_at: Any = None,
) -> str | None:
    """Return the fail-closed reason after startup, resume and storm guards."""
    startup_age = _age_seconds(acquired_at, now)
    if startup_age is not None and startup_age < STARTUP_GRACE_SECONDS:
        return None
    if _deadline_active(resume_grace_until, now):
        return None
    restart_age = _age_seconds(last_restart_at, now)
    if restart_age is not None and restart_age < RESTART_COOLDOWN_SECONDS:
        return None
    heartbeat_age = _age_seconds(
        state.get("heartbeat_at") or state.get("updated_at"), now,
    )
    if heartbeat_age is None or heartbeat_age > HEARTBEAT_TIMEOUT_SECONDS:
        return f"heartbeat stale: {heartbeat_age}"
    try:
        progress_age = int(state.get("progress_age_seconds"))
    except (TypeError, ValueError):
        progress_age = 0
    if progress_age > PROGRESS_TIMEOUT_SECONDS:
        return f"progress stalled: {progress_age}"
    return None


def _daemon_lock_owner() -> tuple[int | None, str | None]:
    try:
        text = DAEMON_LOCK_PATH.read_text(encoding="utf-8")
    except OSError:
        return None, None
    pid_match = re.search(r"(?:^|\s)pid=(\d+)(?:\s|$)", text)
    at_match = re.search(r"(?:^|\s)acquired_at=([^\s]+)", text)
    return (
        int(pid_match.group(1)) if pid_match else None,
        at_match.group(1) if at_match else None,
    )


def _read_state() -> dict[str, Any]:
    try:
        value = json.loads(DAEMON_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _is_factor_daemon(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "command="],
            check=True, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "-m factor_system.daemon" in result.stdout


def _terminate(pid: int) -> None:
    if not _is_factor_daemon(pid):
        return
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 20
    while _is_factor_daemon(pid) and time.monotonic() < deadline:
        time.sleep(0.25)
    if _is_factor_daemon(pid):
        os.kill(pid, signal.SIGKILL)


def main() -> int:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return 0
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_args: stop.set())
    signal.signal(signal.SIGINT, lambda *_args: stop.set())
    try:
        previous_check_at: datetime.datetime | None = None
        resume_grace_until: datetime.datetime | None = None
        last_restart_at: datetime.datetime | None = None
        while not stop.is_set():
            now = datetime.datetime.now()
            resume_deadline = _resume_grace_deadline(previous_check_at, now)
            if resume_deadline is not None:
                resume_grace_until = resume_deadline
            previous_check_at = now
            pid, acquired_at = _daemon_lock_owner()
            if _is_factor_daemon(pid):
                reason = restart_reason(
                    _read_state(), acquired_at=acquired_at,
                    now=now, resume_grace_until=resume_grace_until,
                    last_restart_at=last_restart_at,
                )
                if reason:
                    print(
                        f"[factor-watchdog] PID {pid} {reason}; requesting restart",
                        flush=True,
                    )
                    last_restart_at = now
                    _terminate(int(pid))
            stop.wait(CHECK_SECONDS)
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
