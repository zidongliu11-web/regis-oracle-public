"""Small HTTP adapter for the cached company-profile read model."""

from __future__ import annotations

from typing import Any, Callable, Pattern

from flask import jsonify, request

from .company_profile import get_cached_summary


def register_company_profile_routes(
    app: Any, *,
    code_pattern: Pattern[str],
    public_score_roles: Callable[[dict], dict],
    strict_json_payload: Callable[..., dict],
    ensure_company_profile: Callable[..., bool],
    audited_stock_names: Callable[[], dict[str, str]],
    company_profile_preheat_status: Callable[[], dict],
) -> None:
    """Register read and explicit-refresh profile endpoints."""

    @app.get("/api/company-profile/<code>/summary")
    def company_profile_summary_api(code: str):
        if not code_pattern.match(code):
            return jsonify({"error": "无效代码"}), 400
        workers = company_profile_preheat_status()
        running = code in workers["inflight"]
        failed = code in workers.get("last_errors", {})
        refresh = {
            "running": running,
            "status": "running" if running else "failed" if failed else "idle",
            "message": (
                "公司档案刷新失败，请重新载入个股重试；缺失来源仍保持不可用。"
                if failed and not running else None
            ),
        }
        summary = get_cached_summary(code)
        if not summary:
            return jsonify({
                "code": code, "ready": False,
                "message": "档案尚未生成，请先生成",
                "refresh": refresh,
            })
        return jsonify(public_score_roles({"code": code, **summary, "refresh": refresh}))

    @app.post("/api/company-profile/<code>/refresh")
    def company_profile_refresh_api(code: str):
        if not code_pattern.fullmatch(code):
            return jsonify({"error": "无效代码"}), 400
        try:
            if request.content_length:
                strict_json_payload(allowed=set())
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        started = ensure_company_profile(
            code, audited_stock_names().get(code, code), force=True,
        )
        running = started or code in company_profile_preheat_status()["inflight"]
        return jsonify({
            "ok": running, "started": started, "running": running,
            "idempotent_replay": not started,
            "status_endpoint": f"/api/company-profile/{code}/summary",
        }), 202 if running else 503


__all__ = ["register_company_profile_routes"]
