"""Network binding rules shared by the local trading terminal runtime."""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit


_REMOTE_REQUIRED_ENV = (
    "OLYMPUS_REMOTE_MODE", "OLYMPUS_BEHIND_TLS_PROXY",
    "OLYMPUS_TRUSTED_ORIGINS", "OLYMPUS_REMOTE_AUTH_TOKEN",
)


def is_loopback_host(host: str) -> bool:
    value = (host or "").strip().strip("[]").casefold()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def validated_bind_host(environ: dict[str, str] | None = None) -> str:
    """Default to loopback; reject remote listeners without TLS controls."""
    env = os.environ if environ is None else environ
    host = str(env.get("OLYMPUS_HOST") or "127.0.0.1").strip()
    if is_loopback_host(host):
        return host
    missing = [
        name for name in _REMOTE_REQUIRED_ENV
        if not str(env.get(name) or "").strip()
    ]
    trusted = [value.strip() for value in str(
        env.get("OLYMPUS_TRUSTED_ORIGINS") or ""
    ).split(",") if value.strip()]
    token = str(env.get("OLYMPUS_REMOTE_AUTH_TOKEN") or "")
    if (
        missing
        or env.get("OLYMPUS_REMOTE_MODE") != "1"
        or env.get("OLYMPUS_BEHIND_TLS_PROXY") != "1"
        or len(token) < 32
        or not trusted
        or any(urlsplit(origin).scheme != "https" for origin in trusted)
    ):
        raise RuntimeError(
            "拒绝非回环监听：远程模式必须显式配置 TLS 反向代理、HTTPS "
            "trusted origins 和至少 32 字符的认证令牌"
        )
    return host


__all__ = ["is_loopback_host", "validated_bind_host"]
