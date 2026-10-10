from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_PATH = PROJECT_ROOT / "app.py"
REPORT_DIR = PROJECT_ROOT / "reports"
INSTANCE_DIR = PROJECT_ROOT / "instance"
STATUS_PATH = REPORT_DIR / "web_service_status.json"
LOG_PATH = REPORT_DIR / "web_service.log"
LOCK_PATH = INSTANCE_DIR / "web_supervisor.lock"
STOP_PATH = INSTANCE_DIR / "web_supervisor.stop"
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 3


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


_last_pipeline_health = 0.0
_PIPELINE_HEALTH_INTERVAL = 300  # 5 minutes


def _maybe_refresh_pipeline_health() -> None:
    """Run pipeline_health_check.py if 5 minutes have elapsed since last run."""
    global _last_pipeline_health
    now = time.time()
    if now - _last_pipeline_health < _PIPELINE_HEALTH_INTERVAL:
        return
    _last_pipeline_health = now
    try:
        subprocess.run(
            [sys.executable, "-m", "research.pipeline_health_check"],
            capture_output=True,
            timeout=30,
            cwd=str(PROJECT_ROOT),
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def atomic_write(value: dict[str, Any]) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATUS_PATH.with_suffix(STATUS_PATH.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(STATUS_PATH)


def rotate_log_if_needed(
    path: Path = LOG_PATH,
    *,
    max_bytes: int = LOG_MAX_BYTES,
    backups: int = LOG_BACKUPS,
) -> bool:
    """Bound an append-only child log with a small copy-truncate rotation."""
    try:
        if path.stat().st_size <= max_bytes:
            return False
    except FileNotFoundError:
        return False
    if max_bytes < 1 or backups < 1:
        raise ValueError("log rotation limits must be positive")

    oldest = path.with_name(f"{path.name}.{backups}")
    oldest.unlink(missing_ok=True)
    for index in range(backups - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.exists():
            os.replace(source, path.with_name(f"{path.name}.{index + 1}"))

    backup = path.with_name(f"{path.name}.1")
    temporary = path.with_name(f".{path.name}.rotate.{os.getpid()}.tmp")
    try:
        with path.open("rb+") as source, temporary.open("xb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
            os.replace(temporary, backup)
            source.seek(0)
            source.truncate(0)
            source.flush()
            os.fsync(source.fileno())
    finally:
        temporary.unlink(missing_ok=True)
    return True


class WebSupervisor:
    def __init__(self) -> None:
        self.stop_requested = False
        self.child: subprocess.Popen | None = None
        self.restart_count = 0

    def handle_signal(self, signum: int, _frame: Any) -> None:
        self.stop_requested = True
        if self.child is not None and self.child.poll() is None:
            self.child.terminate()

    def status(
        self,
        *,
        state: str,
        message: str,
        last_exit_code: int | None = None,
    ) -> dict[str, Any]:
        return {
            "status": state,
            "supervisor_pid": os.getpid(),
            "web_pid": (
                self.child.pid
                if self.child is not None
                and self.child.poll() is None
                else None
            ),
            "heartbeat_at": now_iso(),
            "restart_count": self.restart_count,
            "last_exit_code": last_exit_code,
            "url": "http://127.0.0.1:5100/",
            "message": message,
        }

    def run(self) -> None:
        INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
        lock_stream = LOCK_PATH.open("a+", encoding="utf-8")
        try:
            fcntl.flock(
                lock_stream.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            return
        lock_stream.seek(0)
        lock_stream.truncate()
        lock_stream.write(str(os.getpid()))
        lock_stream.flush()
        STOP_PATH.unlink(missing_ok=True)
        signal.signal(signal.SIGTERM, self.handle_signal)
        signal.signal(signal.SIGINT, self.handle_signal)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        rotate_log_if_needed()

        last_exit_code: int | None = None
        try:
            while not self.stop_requested and not STOP_PATH.exists():
                with LOG_PATH.open("a", encoding="utf-8") as stream:
                    stream.write(
                        f"\n[{now_iso()}] 启动网站服务"
                        f"（第{self.restart_count + 1}次）\n"
                    )
                    stream.flush()
                    self.child = subprocess.Popen(
                        [sys.executable, str(APP_PATH)],
                        stdin=subprocess.DEVNULL,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    atomic_write(self.status(
                        state="running",
                        message="网站服务在线；监督器会在异常退出后自动重启。",
                    ))
                    while (
                        self.child.poll() is None
                        and not self.stop_requested
                        and not STOP_PATH.exists()
                    ):
                        atomic_write(self.status(
                            state="running",
                            message=(
                                "网站服务在线；监督器会在异常退出后"
                                "自动重启。"
                            ),
                        ))
                        # Refresh pipeline health every 5 minutes
                        _maybe_refresh_pipeline_health()
                        rotate_log_if_needed()
                        time.sleep(5)
                    if self.child.poll() is None:
                        self.child.terminate()
                        try:
                            self.child.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            self.child.kill()
                    last_exit_code = self.child.poll()

                if self.stop_requested or STOP_PATH.exists():
                    break
                self.restart_count += 1
                atomic_write(self.status(
                    state="restarting",
                    message="网站进程已退出，3秒后自动重新启动。",
                    last_exit_code=last_exit_code,
                ))
                time.sleep(3)
        finally:
            STOP_PATH.unlink(missing_ok=True)
            atomic_write(self.status(
                state="stopped",
                message="网站监督服务已停止。",
                last_exit_code=last_exit_code,
            ))


def main() -> None:
    WebSupervisor().run()


if __name__ == "__main__":
    main()
