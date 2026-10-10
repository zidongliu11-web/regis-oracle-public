"""Lightweight, time-window orchestration for forward research artifacts."""

from __future__ import annotations

from typing import Any

from .forward_capture_integrity import capture_h1_if_ready
from .h1_deployment_gate import verify_h1_capture_runtime
from .h3_execution_forward import capture_h3_forward_observation
from .intraday_market_emotion_forward import capture_intraday_emotion_from_source
from .manual_opportunity_forward import refresh_manual_opportunity_forward
from .market_news_live_adapter import capture_news_live_snapshot
from .market_sector_constituent_forward import fetch_constituent_page
from .market_sector_constituent_recovery import (
    capture_complete_sector_constituents,
)
from .market_sector_route_forward import (
    capture_market_sector_route_input,
    capture_slot as market_sector_route_capture_slot,
)
from .market_sector_route_gate import verify_market_sector_route_runtime
from .market_sector_route_shadow import (
    PROTOCOL_PATH as BASE_SHADOW_ROUTE_PROTOCOL_PATH,
    capture_shadow_route_decision,
)
from .market_state_forward import capture_forward_decision_packet
from .system_route_joint_v2_forward import capture_system_route_joint_v2_forward


SHADOW_ROUTE_PROTOCOL_PATH = BASE_SHADOW_ROUTE_PROTOCOL_PATH.with_name(
    "MARKET_SECTOR_ROUTE_SHADOW_DECISION_PROTOCOL_V1_AMENDMENT_3.json"
)
SHADOW_ROUTE_PROTOCOL_SHA256 = (
    "0e69444b46f734ecf9052fd2d692cd6edc74c089d10705cf23fbd74dfa1ff66c"
)


def capture_forward_chain(
    snapshot: dict[str, Any], candidates: list[dict[str, Any]],
    account_snapshot: dict[str, Any], *, market_intelligence: Any,
    candle_provider: Any,
) -> dict[str, Any]:
    """Capture from a prepared ranking snapshot; never recompute that ranking."""
    # ``candle_provider`` stays in the public signature because callers share
    # one capture contract.  The retired legacy 30-minute candidate branch was
    # its only consumer and must not be reactivated from this orchestrator.
    _ = candle_provider
    h1_runtime = verify_h1_capture_runtime()
    route_runtime = verify_market_sector_route_runtime()
    route_slot = market_sector_route_capture_slot()
    snapshot["system_route_joint_v2_forward_capture"] = {
        "status": "waiting_for_verified_same_slot_inputs",
        "captured": False,
        "research_only": True,
    }
    try:
        snapshot["market_state_forward_capture"] = capture_h1_if_ready(
            snapshot, candidates, h1_runtime=h1_runtime,
            route_runtime=route_runtime, route_slot=route_slot,
            capture=capture_forward_decision_packet,
        )
    except Exception as exc:
        snapshot["market_state_forward_capture"] = {
            "status": "error", "captured": False,
            "error": f"{type(exc).__name__}: {exc}",
            "orders_or_returns_computed": False,
        }
    try:
        snapshot["h3_execution_forward_capture"] = capture_h3_forward_observation(
            snapshot["market_state_forward_capture"]
        )
    except Exception as exc:
        snapshot["h3_execution_forward_capture"] = {
            "status": "error", "captured": False,
            "error": f"{type(exc).__name__}: {exc}",
            "applied_to_score_position_or_alerts": False,
        }
    if route_slot is not None:
        snapshot["intraday_emotion_forward_capture"] = (
            capture_intraday_emotion_from_source(market_intelligence)
        )
        try:
            snapshot["market_news_forward_capture"] = capture_news_live_snapshot(
                market_intelligence.news_live()
            )
        except Exception as exc:
            snapshot["market_news_forward_capture"] = {
                "status": "error", "captured": False,
                "error": f"{type(exc).__name__}: {exc}",
                "eligible_for_market_emotion": False,
            }
    if route_slot is None:
        snapshot["intraday_emotion_forward_capture"] = {
            "status": "outside_capture_slot", "captured": False,
        }
        snapshot["market_news_forward_capture"] = {
            "status": "outside_capture_slot", "captured": False,
            "eligible_for_market_emotion": False,
        }
        snapshot["market_sector_route_forward_capture"] = {
            "status": "outside_capture_slot", "captured": False,
            "changes_trade_count_or_position": False,
        }
        snapshot["system_route_joint_v2_forward_capture"] = {
            "status": "outside_capture_slot", "captured": False,
            "research_only": True,
        }
    elif route_runtime.get("valid") is not True:
        snapshot["market_sector_route_forward_capture"] = {
            "status": "identity_invalid", "captured": False,
            "errors": route_runtime.get("errors") or [],
            "changes_trade_count_or_position": False,
        }
    elif snapshot["market_state_forward_capture"].get("captured") is not True:
        snapshot["market_sector_route_forward_capture"] = {
            "status": "waiting_for_verified_h1_packet", "captured": False,
            "changes_trade_count_or_position": False,
        }
    else:
        try:
            sector_snapshot = market_intelligence.market_sectors(force=True)
            route = capture_market_sector_route_input(
                snapshot["market_state_forward_capture"], sector_snapshot,
                h3_capture=snapshot["h3_execution_forward_capture"],
            )
            snapshot["market_sector_route_forward_capture"] = route
            route["runtime_admission"] = {
                "admission_id": route_runtime.get("admission_id"),
                "admission_sha256": route_runtime.get("admission_sha256"),
            }
            if route.get("captured") is True:
                snapshot["market_sector_route_shadow_decision"] = (
                    capture_shadow_route_decision(
                        route,
                        protocol_path=SHADOW_ROUTE_PROTOCOL_PATH,
                        expected_protocol_sha256=SHADOW_ROUTE_PROTOCOL_SHA256,
                    )
                )
            shadow = snapshot.get("market_sector_route_shadow_decision") or {}
            if shadow.get("captured") is True:
                snapshot["market_sector_constituent_forward_capture"] = (
                    capture_complete_sector_constituents(
                        shadow, fetch_constituent_page,
                    )
                )
            constituent = snapshot.get("market_sector_constituent_forward_capture") or {}
            if constituent.get("captured") is True:
                try:
                    snapshot["system_route_joint_v2_forward_capture"] = (
                        capture_system_route_joint_v2_forward(
                            snapshot, account_snapshot,
                        )
                    )
                except Exception as exc:
                    snapshot["system_route_joint_v2_forward_capture"] = {
                        "status": "error", "captured": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "research_only": True,
                    }
        except Exception as exc:
            snapshot["market_sector_route_forward_capture"] = {
                "status": "error", "captured": False,
                "error": f"{type(exc).__name__}: {exc}",
                "changes_trade_count_or_position": False,
            }
    try:
        snapshot["manual_forward_evidence"] = refresh_manual_opportunity_forward()
    except Exception as exc:
        snapshot["manual_forward_evidence"] = {
            "status": "error", "error": f"{type(exc).__name__}: {exc}",
            "stable_profit_proven": False, "production_unlock": False,
        }
    return snapshot


__all__ = ["capture_forward_chain"]
