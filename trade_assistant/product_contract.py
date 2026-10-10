"""Privacy-safe product contract derived from the local research service.

The product contract deliberately separates a research subscription from a
trade-signal product.  It exposes timestamps, quality gates, bounded market
snapshots and reproducible aggregate metrics, but never factor formulas,
weights, thresholds, raw trades, prompts, credentials or broker actions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


PLAN_CAPABILITIES = {
    "standard": (
        "market_snapshot",
        "single_stock_research",
        "technical_state",
        "research_summary",
        "data_as_of",
        "forward_progress",
    ),
    "pro": (
        "market_snapshot",
        "single_stock_research",
        "technical_state",
        "research_summary",
        "data_as_of",
        "forward_progress",
        "validation_matrix",
        "version_comparison",
        "quality_audit_summary",
        "data_lineage_summary",
        "candidate_bound_reproduction_status",
        "cost_model_summary",
    ),
}


def _feed(live: dict[str, Any], feed_id: str) -> dict[str, Any]:
    freshness = live.get("data_freshness") or {}
    for value in freshness.get("feeds") or []:
        if isinstance(value, dict) and value.get("id") == feed_id:
            return value
    return {}


def build_product_readiness(
    live: dict[str, Any], *, token_configured: bool,
) -> dict[str, Any]:
    worker = live.get("worker") or {}
    stable = live.get("stable_target_campaign") or {}
    freshness = live.get("data_freshness") or {}
    universe = _feed(live, "full_universe_daily")
    worker_live = bool(worker.get("live"))
    universe_ready = bool(
        universe.get("status") == "fresh"
        and universe.get("integrity_ok")
        and universe.get("quality_audit_ready")
        and universe.get("quality_matches_download_manifest")
        and int(universe.get("physical_file_count") or 0) > 0
        and int(universe.get("quality_passed_files") or 0)
        == int(universe.get("physical_file_count") or 0)
    )
    stable_report_available = bool(
        stable.get("status") == "completed"
        and stable.get("closest_candidate") is not None
    )
    # The product safety boundary is the real-money gate, not whether a
    # research candidate has completed its validation campaign.  A validated
    # signal may become publishable while broker execution remains locked.
    safety_locked = stable.get("real_money_gate_passed") is False
    research_ready = bool(
        token_configured
        and worker_live
        and universe_ready
        and stable_report_available
        and safety_locked
    )
    signal_ready = bool(
        token_configured
        and worker_live
        and universe_ready
        and stable.get("stable_target_completed") is True
        and int(stable.get("historical_stable_pass_count") or 0) > 0
        and stable.get("independent_reproduction_passed") is True
        and int(stable.get("forward_completed_trades") or 0) >= 30
    )
    closest = stable.get("closest_candidate") or {}
    validation = closest.get("validation") or {}
    observed = closest.get("observed_test") or {}
    current_feeds_fresh = bool(freshness.get("all_required_feeds_fresh"))
    blockers = []
    if not token_configured:
        blockers.append("product_api_token_not_configured")
    if not worker_live:
        blockers.append("research_worker_not_live")
    if not universe_ready:
        blockers.append("daily_universe_quality_not_ready")
    if not stable_report_available:
        blockers.append("stable_target_report_unavailable")
    if int(stable.get("historical_stable_pass_count") or 0) < 1:
        blockers.append("no_historical_stable_candidate")
    if stable.get("independent_reproduction_passed") is not True:
        blockers.append("same_candidate_independent_reproduction_missing")
    if int(stable.get("forward_completed_trades") or 0) < 30:
        blockers.append("same_candidate_forward_below_30")
    if not current_feeds_fresh:
        blockers.append("current_feeds_degraded")
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "service": {
            "workerLive": worker_live,
            "workerStatus": worker.get("status"),
            "workerStep": worker.get("current_step"),
            "heartbeatAgeSeconds": worker.get("heartbeat_age_seconds"),
            "dataAsOf": universe.get("as_of"),
            "universeSymbols": int(universe.get("physical_file_count") or 0),
            "qualityPassedFiles": int(universe.get("quality_passed_files") or 0),
            "allRequiredFeedsFresh": current_feeds_fresh,
            "overallFreshness": freshness.get("overall_status"),
        },
        "readiness": {
            "researchProductReady": research_ready,
            "tradeSignalProductReady": signal_ready,
            "mode": (
                "trade_signal_ready"
                if signal_ready
                else "research_only_ready"
                if research_ready
                else "not_ready"
            ),
            "blockers": blockers,
        },
        "targets": {
            "winRatePct": 60.0,
            "costInclusiveAnnualizedReturnPct": 15.0,
            "minimumTradesPerHistoricalPeriod": 100,
            "forwardCompletedTrades": 30,
            "forwardMinimumCalendarWeeks": 6,
        },
        "currentEvidence": {
            "candidateVersion": closest.get("version"),
            "validation": {
                key: validation.get(key)
                for key in ("trades", "wins", "win_rate_pct", "annualized_return_pct")
            },
            "observedTest": {
                key: observed.get(key)
                for key in ("trades", "wins", "win_rate_pct", "annualized_return_pct")
            },
            "historicalStablePassCount": int(
                stable.get("historical_stable_pass_count") or 0
            ),
            "sameCandidateIndependentReproductionPassed": bool(
                stable.get("independent_reproduction_passed")
            ),
            "sameCandidateForwardCompletedTrades": int(
                stable.get("forward_completed_trades") or 0
            ),
            "stableTargetCompleted": bool(stable.get("stable_target_completed")),
        },
        "plans": {
            plan: {"capabilities": list(capabilities)}
            for plan, capabilities in PLAN_CAPABILITIES.items()
        },
        "safety": {
            "researchOnly": not signal_ready,
            "brokerOrdersEnabled": False,
            "automaticTradingEnabled": False,
            "realMoneyGatePassed": False,
            "factorFormulasExposed": False,
            "weightsOrThresholdsExposed": False,
            "rawTradesExposed": False,
        },
    }


def product_dashboard_payload(
    raw: dict[str, Any], readiness: dict[str, Any], *, plan: str,
) -> dict[str, Any]:
    signal_ready = bool(
        (readiness.get("readiness") or {}).get("tradeSignalProductReady")
    )
    return {
        "market": raw.get("market") or {},
        "opportunities": (raw.get("opportunities") or [])[:3] if signal_ready else [],
        "observations": (raw.get("observations") or [])[:20],
        "portfolio": raw.get("portfolio") or {},
        "paper": raw.get("paper") or {},
        "research": {
            "status": (readiness.get("readiness") or {}).get("mode"),
            "historical_strategy_gate_passed": signal_ready,
            "updated_at": readiness.get("generatedAt"),
            "message": (
                "同一冻结策略已完成稳定历史、独立复现和前向门禁。"
                if signal_ready
                else "研究产品可展示证据与市场快照；交易信号门禁尚未通过。"
            ),
        },
        "wecom": {"configured": bool((raw.get("wecom") or {}).get("configured"))},
        "productReadiness": readiness,
        "plan": plan,
    }


def product_stock_payload(
    raw: dict[str, Any], readiness: dict[str, Any], *, plan: str,
) -> dict[str, Any]:
    signal_ready = bool(
        (readiness.get("readiness") or {}).get("tradeSignalProductReady")
    )
    state = raw.get("state") or {}
    technical = raw.get("technical") or {}
    snapshot = technical.get("snapshot") or {}
    values = technical.get("values") or {}
    lens_limit = 10 if plan == "pro" else 5
    return {
        "code": raw.get("code"),
        "name": raw.get("name"),
        "interval": raw.get("interval"),
        "information_time": raw.get("information_time"),
        "source": raw.get("source") or {},
        "quote": raw.get("quote") or {},
        "candles": (raw.get("candles") or [])[-240:],
        "market_regime": raw.get("market_regime") or {},
        "state": {
            "code": state.get("code"),
            "label": state.get("label"),
            "score": state.get("score") if plan == "pro" else None,
            "score_range": state.get("score_range") if plan == "pro" else [],
            "reasons": (state.get("reasons") or [])[:lens_limit],
            "cautions": (state.get("cautions") or [])[:lens_limit],
        },
        "technical": {
            "snapshot": {
                "information_time": snapshot.get("information_time"),
                "status": snapshot.get("status"),
                "research_status": snapshot.get("research_status"),
                "message": snapshot.get("message"),
                "source_boundary": snapshot.get("source_boundary"),
                "lenses": (snapshot.get("lenses") or [])[:lens_limit],
            },
            "values": values if plan == "pro" else {
                key: values.get(key)
                for key in ("ema5", "ema20", "rsi14", "atr14_pct")
            },
        },
        "decision": {
            "research_gate_passed": signal_ready,
            "buy_scenario": None,
            "sell_scenario": None,
            "message": (
                "稳定交易信号门禁已通过；仍只供人工复核。"
                if signal_ready
                else "稳定交易信号门禁未通过，不提供买点、卖点或目标价。"
            ),
        },
        "safety": {
            "manual_confirmation": True,
            "automatic_trading": False,
            "broker_orders_enabled": False,
        },
        "productReadiness": readiness,
        "plan": plan,
    }


def product_quote_payload(raw: dict[str, Any]) -> dict[str, Any]:
    quote = raw.get("quote") or {}
    return {
        "code": raw.get("code") or quote.get("code"),
        "quote": quote,
        "server_time": raw.get("server_time"),
        "market_session": raw.get("market_session"),
        "message": raw.get("message"),
    }
