"""Legacy Keychain cleanup for the now-disabled paid model integration."""

from __future__ import annotations

import subprocess
from typing import Callable


DEEPSEEK_SERVICE = "AshareTradingAssistant.DeepSeek"
DEEPSEEK_ACCOUNT = "local-research-worker"


def _run_security(
    arguments: list[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> subprocess.CompletedProcess[str] | None:
    try:
        active_runner = runner or subprocess.run
        return active_runner(
            ["/usr/bin/security", *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def load_deepseek_api_key() -> str | None:
    """Paid credentials are never read into a process."""
    return None


def save_deepseek_api_key(api_key: str) -> bool:
    """Paid credentials cannot be stored through this application."""
    del api_key
    return False


def delete_deepseek_api_key() -> bool:
    result = _run_security([
        "delete-generic-password",
        "-s", DEEPSEEK_SERVICE,
        "-a", DEEPSEEK_ACCOUNT,
    ])
    # Treat an absent item as already disconnected.
    return bool(result is not None and result.returncode in {0, 44})


def deepseek_keychain_available() -> bool:
    return False
