"""Current-site safety interpretation of frozen v1 reference-cohort emotion.

The v1 collector and replay hashes remain unchanged. No v2 full-exchange
reconciliation receipt exists yet, so v1 input cannot authorize new buys.
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from .intraday_market_emotion_forward import build_intraday_emotion_observation
from .strict_alert_binding import strict_route_alert_context


def scope_v1_emotion_observation(
    observation: dict[str, Any] | None, *, breadth: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep v1 counts but refuse to promote its reference list to full A."""
    result = copy.deepcopy(observation) if isinstance(observation, dict) else {}
    if not result:
        return result
    sample = breadth if isinstance(breadth, dict) else {}
    raw = result.get("raw_observation")
    raw = raw if isinstance(raw, dict) else {}
    prior_scope = result.get("scope")
    prior_scope = prior_scope if isinstance(prior_scope, dict) else {}
    old_confirmation = result.get("confirmation")
    prior_direction = result.get("reference_cohort_direction")
    result["reference_cohort_direction"] = (
        prior_direction if prior_direction in {"supports", "contradicts", "not_evaluable"}
        else {"confirmed": "supports", "contradicted": "contradicts"}.get(
            old_confirmation, "not_evaluable"
        )
    )
    result["scope"] = {
        "schema": "intraday_emotion_scope.v2",
        "protocol": "intraday_market_emotion_forward_input.v1",
        "reference_universe_expected": raw.get("reference_universe_expected"),
        "reference_universe_returned": raw.get("reference_universe_returned"),
        "exchange_coverage_status_reported": sample.get(
            "exchange_coverage_status", prior_scope.get("exchange_coverage_status_reported")
        ),
        "full_a_coverage_verified": False,
        "independent_exchange_reconciliation": False,
    }
    result["confirmation"] = "not_evaluable"
    result["confirmation_blockers"] = [
        "full_a_exchange_universe_unverified", *(result.get("source_capture_errors") or []),
    ]
    result["research_budget_hypothesis"] = {
        "status": "hold_new_trades",
        "maximum_new_trades": 0,
        "target_exposure_range_pct": [0, 0],
        "existing_position_risk_reduction_allowed": True,
        "applied_to_execution": False,
    }
    if result.get("status") == "intraday_emotion_ready":
        result["status"] = "reference_cohort_only"
    state_label = str(result.get("summary") or "状态未知").split(" · ", 1)[0]
    ratio = raw.get("up_ratio")
    try:
        ratio_text = f" · 参考样本上涨{float(ratio):.1f}%" if ratio is not None else ""
    except (TypeError, ValueError):
        ratio_text = ""
    result["summary"] = f"{state_label} · 全 A 待核验{ratio_text} · 研究不开新仓"
    scope_reason = (
        "v1 只核参考代码清单；没有独立沪深京当时上市全集及来源时点收据，"
        "即使参考覆盖 100% 也不能确认全 A 情绪。"
    )
    result["reason"] = (
        scope_reason if not result.get("source_capture_errors") else
        "参考输入未通过时效或完整性检查。" + scope_reason
    )
    return result


def build_scoped_intraday_emotion_observation(
    route: dict[str, Any], today_market: dict[str, Any] | None, *,
    now: datetime | None = None,
) -> dict[str, Any]:
    legacy = build_intraday_emotion_observation(route, today_market, now=now)
    breadth = (today_market or {}).get("breadth") if isinstance(today_market, dict) else None
    return scope_v1_emotion_observation(legacy, breadth=breadth)


def scoped_strict_route_alert_context(
    forward_status: object, *, decision_day: str,
    reference_time: object = None,
) -> dict[str, Any]:
    """Reject legacy strict buy eligibility until independently verifiable v2 exists."""
    result = strict_route_alert_context(
        forward_status, decision_day=decision_day,
        reference_time=reference_time,
    )
    return enforce_current_full_a_entry_scope(result)


def enforce_current_full_a_entry_scope(context: object) -> dict[str, Any]:
    """Final writer gate; caller-provided flags cannot supply absent v2 evidence."""
    result = copy.deepcopy(context) if isinstance(context, dict) else {}
    result["alert_ready"] = False
    result["maximum_new_trades"] = 0
    result["playbook_id"] = None
    result["strategy_ref"] = None
    result["full_a_emotion_scope_valid"] = False
    if result.get("route_valid"):
        result["reason"] = (
            "旧 v1 同槽情绪仅核参考清单，缺少独立全 A 范围原件；"
            "当前站点禁止由此生成买入提醒。"
        )
    return result


__all__ = [
    "build_scoped_intraday_emotion_observation",
    "scope_v1_emotion_observation",
    "scoped_strict_route_alert_context",
    "enforce_current_full_a_entry_scope",
]
