"""Research-only market-state strategy router.

This module freezes the shape of the proposed state -> sector -> playbook
route.  It deliberately has no access to quotes, orders, returns, the account
database, or the production signal registry.  Its budgets are hypotheses for
later joint validation, never live trading authority.
"""

from __future__ import annotations

from math import isfinite
from typing import Any


ROUTE_VERSION = "market_state_strategy_router_v2_research_only"
MARKET_STATES = (
    "risk_closed",
    "weak_repair",
    "sentiment_rising",
    "crowded_climax",
    "retreat",
)
OBSERVATION_SLOTS = {
    "morning_1030": "10:30-10:45 Asia/Shanghai",
    "afternoon_1400": "14:00-14:15 Asia/Shanghai",
}
PLAYBOOKS = {
    "mainline_start_confirmation": {
        "label": "主线启动确认",
        "role": "research_entry_bucket",
    },
    "leader_divergence_restrengthening": {
        "label": "龙头分歧转强",
        "role": "research_deferred_bucket",
    },
    "retreat_defense": {
        "label": "退潮防守",
        "role": "research_risk_bucket",
    },
}
_STATE_ROUTES = {
    "risk_closed": {
        "new_trade_budget": 0,
        "exposure_range_pct": [20, 20],
        "playbooks": ["retreat_defense"],
    },
    "weak_repair": {
        "new_trade_budget": 1,
        "exposure_range_pct": [40, 40],
        "playbooks": ["mainline_start_confirmation", "retreat_defense"],
    },
    "sentiment_rising": {
        "new_trade_budget": 2,
        "exposure_range_pct": [80, 80],
        "playbooks": ["mainline_start_confirmation", "retreat_defense"],
    },
    "crowded_climax": {
        "new_trade_budget": 0,
        "exposure_range_pct": [40, 40],
        "playbooks": ["retreat_defense"],
    },
    "retreat": {
        "new_trade_budget": 0,
        "exposure_range_pct": [20, 20],
        "playbooks": ["retreat_defense"],
    },
}


def _finite_probability(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(number) or not 0 <= number <= 1:
        return None
    return number


def _market_contract(market: dict[str, Any]) -> tuple[bool, list[str]]:
    missing: list[str] = []
    state = str(market.get("state") or "")
    if state not in MARKET_STATES:
        missing.append("market_state")
    if not market.get("as_of"):
        missing.append("market_as_of")
    confidence = _finite_probability(market.get("confidence"))
    if confidence is None:
        missing.append("market_confidence")
    if not market.get("confidence_semantics"):
        missing.append("market_confidence_semantics")
    probabilities = market.get("state_probabilities")
    if not isinstance(probabilities, dict):
        missing.append("market_state_probabilities")
    else:
        parsed = [_finite_probability(probabilities.get(item)) for item in MARKET_STATES]
        if any(value is None for value in parsed) or abs(sum(parsed) - 1.0) > 1e-6:
            missing.append("market_state_probabilities")
    if not market.get("state_probabilities_semantics"):
        missing.append("market_state_probabilities_semantics")
    if not isinstance(market.get("evidence"), list) or not market["evidence"]:
        missing.append("market_evidence")
    if not isinstance(market.get("contradictions"), list):
        missing.append("market_contradictions")
    if market.get("completed_data_only") is not True:
        missing.append("completed_market_data_contract")
    return not missing, missing


def _sector_contract(sector: dict[str, Any]) -> tuple[bool, list[str]]:
    missing: list[str] = []
    if sector.get("status") != "confirmed":
        missing.append("sector_confirmation")
    if sector.get("observation_slot") not in OBSERVATION_SLOTS:
        missing.append("sector_observation_slot")
    for key in (
        "as_of",
        "source_as_of",
        "confirmation_rule_id",
        "coverage_pct",
        "advance_coverage_pct",
        "change_rank_pct",
        "constituent_count",
    ):
        if sector.get(key) is None:
            missing.append(f"sector_{key}")
    if not isinstance(sector.get("confirmed_sectors"), list) or not sector[
        "confirmed_sectors"
    ]:
        missing.append("sector_confirmed_sectors")
    if sector.get("no_backfill") is not True:
        missing.append("sector_no_backfill_contract")
    if sector.get("point_in_time_snapshot_only") is not True:
        missing.append("sector_point_in_time_snapshot_contract")
    return not missing, missing


def build_research_strategy_route(
    market: dict[str, Any] | None,
    sector: dict[str, Any] | None,
    account: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a transparent route hypothesis without granting trade authority."""

    market = market if isinstance(market, dict) else {}
    sector = sector if isinstance(sector, dict) else {}
    account = account if isinstance(account, dict) else {}
    market_ready, market_missing = _market_contract(market)
    requested_state = str(market.get("state") or "unknown")
    sector_required = requested_state in {"weak_repair", "sentiment_rising"}
    sector_ready, sector_missing = _sector_contract(sector)
    route_ready = market_ready and (sector_ready or not sector_required)
    state = requested_state if route_ready else "unknown"
    route = _STATE_ROUTES.get(state) or {
        "new_trade_budget": 0,
        "exposure_range_pct": [0, 0],
        "playbooks": ["retreat_defense"],
    }
    cash_recorded = account.get("cash_user_recorded") is True
    valuation_complete = account.get("valuation_status") == "complete"
    account_snapshot_bound = bool(account.get("account_snapshot_sha256"))
    missing = list(market_missing)
    if sector_required:
        missing.extend(sector_missing)
    if not cash_recorded:
        missing.append("user_recorded_cash")
    if not valuation_complete:
        missing.append("complete_portfolio_valuation")
    if not account_snapshot_bound:
        missing.append("account_snapshot_sha256")
    missing = list(dict.fromkeys(missing))

    return {
        "schema_version": 1,
        "route_version": ROUTE_VERSION,
        "status": "research_route_ready" if route_ready else "research_inputs_incomplete",
        "requested_market_state": requested_state,
        "market_state": state,
        "market_state_as_of": market.get("as_of"),
        "market_confidence": market.get("confidence") if market_ready else None,
        "market_confidence_semantics": market.get("confidence_semantics"),
        "state_probabilities": market.get("state_probabilities") if market_ready else None,
        "state_probabilities_semantics": market.get(
            "state_probabilities_semantics"
        ),
        "evidence": market.get("evidence") if market_ready else [],
        "contradictions": market.get("contradictions") if market_ready else [],
        "sector_confirmation": {
            "required": sector_required,
            "ready": sector_ready if sector_required else None,
            "observation_slot": sector.get("observation_slot"),
            "as_of": sector.get("as_of"),
            "source_as_of": sector.get("source_as_of"),
            "confirmation_rule_id": sector.get("confirmation_rule_id"),
            "confirmed_sectors": sector.get("confirmed_sectors") or [],
            "observation_ready": sector.get("observation_ready") is True,
            "observed_sectors": sector.get("observed_sectors") or [],
            "observed_only_not_route_confirmation": (
                sector.get("observed_only_not_route_confirmation") is True
            ),
            "unavailable_enrichment_metrics": sector.get(
                "unavailable_enrichment_metrics"
            ) or [],
        },
        "research_budget_hypothesis": {
            "maximum_new_trades": route["new_trade_budget"],
            "target_exposure_range_pct": route["exposure_range_pct"],
            "allowed_playbooks": [
                {"id": item, **PLAYBOOKS[item]} for item in route["playbooks"]
            ],
            "validated": False,
            "applied_to_execution": False,
        },
        "account_evidence": {
            "cash_user_recorded": cash_recorded,
            "valuation_complete": valuation_complete,
            "account_snapshot_bound": account_snapshot_bound,
        },
        "missing_inputs": missing,
        "authority": {
            "research_only": True,
            "changes_unified_score": False,
            "changes_trade_count": False,
            "changes_position_or_quantity": False,
            "generates_buy_or_sell_alert": False,
            "production_strategy": False,
            "automatic_trading": False,
        },
        "public_trader_style_role": {
            "identity": "anonymous_public_claim_hypotheses_only",
            "named_person_signal_input": False,
            "narrative_or_quote_directly_scored": False,
            "requires_source_grade_and_oos_validation": True,
        },
        "next_action": (
            "Collect immutable forward packets from 2026-08-31, then jointly "
            "validate state, routing, stock selection and A-share execution costs."
        ),
    }
