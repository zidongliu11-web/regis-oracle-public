"""Small HTTP boundary helpers shared by every mom-trading route."""

from __future__ import annotations

import secrets
import time

from flask import g, jsonify, request


def begin_request() -> None:
    g.request_id = secrets.token_hex(12)
    g.csp_nonce = secrets.token_urlsafe(18)
    g.request_started = time.perf_counter()


def request_nonce() -> str:
    return str(getattr(g, "csp_nonce", ""))


def internal_error_response(
    app, public_message: str, exc: object, *, status: int = 500,
    extra: dict | None = None,
):
    """Log full internals with correlation, return only a stable public error."""
    request_id = log_internal_error(app, exc)
    payload = {"ok": False, "error": public_message, "request_id": request_id}
    if extra:
        payload.update(extra)
    return jsonify(payload), status


def log_internal_error(app, exc: object, *, event: str = "api_failure") -> str:
    request_id = str(getattr(g, "request_id", "untracked"))
    error = exc if isinstance(exc, Exception) else RuntimeError(str(exc))
    app.logger.error(
        "%s request_id=%s error_type=%s",
        event, request_id, type(error).__name__,
        exc_info=(type(error), error, error.__traceback__),
    )
    return request_id


def finish_response(response, app):
    request_id = str(getattr(g, "request_id", "untracked"))
    elapsed_ms = max(
        0.0,
        (time.perf_counter() - float(getattr(g, "request_started", 0.0)))
        * 1000,
    )
    nonce = request_nonce()
    response.headers["X-Request-ID"] = request_id
    response.headers["Server-Timing"] = f"app;dur={elapsed_ms:.1f}"
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "; ".join((
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: https:",
            "font-src 'self' data:",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        )),
    )
    app.logger.info(
        "http_request request_id=%s method=%s path=%s status=%s duration_ms=%.1f",
        request_id, request.method, request.path, response.status_code, elapsed_ms,
    )
    return response
