"""Short-lived browser login backed by the existing remote access secret.

The cookie contains only a signed marker, never the remote access token.
Rotating that token invalidates every browser cookie immediately.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from urllib.parse import unquote, urlsplit

from itsdangerous import BadSignature, URLSafeTimedSerializer


AUTH_COOKIE = "__Host-regis_auth"
LOGIN_CSRF_COOKIE = "__Host-regis_login_csrf"
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60


def _serializer(remote_token: str) -> URLSafeTimedSerializer:
    key = hmac.new(
        remote_token.encode("utf-8"), b"regis-browser-login-key-v1", hashlib.sha256
    ).digest()
    return URLSafeTimedSerializer(
        key,
        salt="regis-browser-login-v1",
        signer_kwargs={"digest_method": hashlib.sha256},
    )


def issue_session(remote_token: str) -> str:
    if not remote_token:
        raise ValueError("remote authentication is not configured")
    return _serializer(remote_token).dumps({"role": "regis-browser", "version": 1})


def valid_session(
    remote_token: str, cookie: str | None, *, max_age: int = SESSION_MAX_AGE_SECONDS
) -> bool:
    if not remote_token or not cookie:
        return False
    try:
        payload = _serializer(remote_token).loads(cookie, max_age=max_age)
    except (BadSignature, ValueError, TypeError):
        return False
    return payload == {"role": "regis-browser", "version": 1}


def new_login_csrf() -> str:
    return secrets.token_urlsafe(32)


def valid_login_csrf(submitted: str | None, cookie: str | None) -> bool:
    return bool(submitted and cookie) and hmac.compare_digest(submitted, cookie)


def safe_next(raw: str | None) -> str:
    """Allow only local paths after login; never redirect to another host."""
    value = str(raw or "")
    decoded = value
    for _ in range(3):
        if not decoded.startswith("/") or decoded.startswith("//"):
            return "/"
        if "\\" in decoded or any(ord(char) < 32 for char in decoded):
            return "/"
        updated = unquote(decoded)
        if updated == decoded:
            break
        decoded = updated
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "/"
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path == "/login":
        return "/"
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")
