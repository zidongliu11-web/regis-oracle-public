"""Freeze one research-only strategy route for each verified forward input."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .market_sector_route_forward import (
    capture_slot,
    verify_market_sector_route_packet,
)
from .market_sector_strategy_adapter import (
    REQUIRED_PERCENTILES,
    adapt_market_observation,
    adapt_sector_snapshot,
)
from .market_state_daily import PROJECT_ROOT, _atomic_publish, _canonical_bytes, _sha256
from .strategy_router import build_research_strategy_route
from .research_artifact_paths import research_artifact_path


SH_TZ = ZoneInfo("Asia/Shanghai")
PROTOCOL_PATH = (
    PROJECT_ROOT / "research/MARKET_SECTOR_ROUTE_SHADOW_DECISION_PROTOCOL_V1.json"
)
PROTOCOL_SHA256 = "b81823d1910d8dd97382d93a2d856f0c1a6c1a92646c9f992600bca4523bf0df"
PACKET_DIR = (
    PROJECT_ROOT / "research/data/market_sector_route_forward/v1/route_decisions"
)


def _resolve_project_path(raw: object, *, project_root: Path) -> Path:
    return research_artifact_path(raw, project_root=project_root)


def _activation_error(protocol: dict, captured_at: object) -> str | None:
    if (protocol.get("activation_boundary") or {}).get("applies_only_to_packets_captured_after_frozen_at") is not True:
        return None
    try:
        frozen = datetime.fromisoformat(protocol["frozen_at"])
        captured = datetime.fromisoformat(str(captured_at))
        if frozen.tzinfo is None or captured.tzinfo is None:
            raise ValueError("timezone_missing")
    except (KeyError, TypeError, ValueError):
        return "protocol_activation_time_invalid"
    return "raw_input_not_after_protocol_freeze" if captured <= frozen else None


def _raw_time_error(packet: dict, observed: datetime | None = None) -> str | None:
    try:
        captured = datetime.fromisoformat(str(packet.get("captured_at")))
        if captured.tzinfo is None or (observed is not None and captured > observed):
            return "raw_input_time_invalid"
        local = captured.astimezone(SH_TZ)
        slot = capture_slot(local)
        if (local.date().isoformat() != packet.get("decision_day")
                or slot is None or slot != packet.get("slot_id")):
            return "raw_input_time_identity_mismatch"
    except (TypeError, ValueError):
        return "raw_input_time_invalid"
    return None


def verify_shadow_protocol(
    *, project_root: Path = PROJECT_ROOT, protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str | None = PROTOCOL_SHA256,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        protocol_sha = _sha256(protocol_path)
        if expected_protocol_sha256 and protocol_sha != expected_protocol_sha256:
            errors.append("protocol_sha256_mismatch")
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        if protocol.get("status") != "frozen_before_first_forward_packet_no_outcome_access":
            errors.append("protocol_status_invalid")
        for name, binding in (protocol.get("bindings") or {}).items():
            path = _resolve_project_path(binding.get("path"), project_root=project_root)
            if not path.is_file() or _sha256(path) != binding.get("sha256"):
                errors.append(f"binding_invalid:{name}")
        authority = protocol.get("authority") or {}
        if authority.get("research_only") is not True or any(
            authority.get(key) is not False for key in (
                "changes_unified_score", "changes_trade_count",
                "changes_position_or_quantity", "generates_buy_or_sell_alert",
                "selects_production_candidate", "production_strategy",
                "automatic_trading",
            )
        ):
            errors.append("protocol_authority_invalid")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"protocol_unreadable:{exc}")
        protocol_sha = None
        protocol = {}
    return {
        "valid": not errors,
        "errors": errors,
        "protocol": protocol,
        "protocol_sha256": protocol_sha,
    }


def _bound_feature_row(
    h1_packet: dict[str, Any], *, project_root: Path,
) -> dict[str, Any]:
    treatment = h1_packet.get("treatment_observation") or {}
    identity = treatment.get("feature_identity") or {}
    if identity.get("daily_feature_path"):
        path_key, sha_key = "daily_feature_path", "daily_feature_sha256"
    else:
        path_key, sha_key = "path", "sha256"
    path = _resolve_project_path(identity.get(path_key), project_root=project_root)
    if not path.is_file() or _sha256(path) != identity.get(sha_key):
        raise ValueError("h1_feature_identity_invalid")
    document = json.loads(path.read_text(encoding="utf-8"))
    rows = [document.get("row")] if isinstance(document.get("row"), dict) else (
        document.get("rows") or []
    )
    state_as_of = str(treatment.get("state_as_of") or "")
    matches = [row for row in rows if row.get("trade_date") == state_as_of]
    if len(matches) != 1:
        raise ValueError("h1_feature_row_not_unique_for_state_as_of")
    row = matches[0]
    features = row.get("features") or {}
    if (
        features.get("confirmed_state") != treatment.get("confirmed_state")
        or features.get("raw_state") != treatment.get("raw_state")
        or features.get("applied_to_live_positions") is not False
    ):
        raise ValueError("h1_feature_row_mismatch")
    return row


def _market_observation(
    raw_packet: dict[str, Any], *, project_root: Path,
) -> dict[str, Any]:
    h1_ref = raw_packet.get("h1_packet") or {}
    h1_path = _resolve_project_path(h1_ref.get("path"), project_root=project_root)
    h1 = json.loads(h1_path.read_text(encoding="utf-8"))
    row = _bound_feature_row(h1, project_root=project_root)
    features = row["features"]
    percentiles: dict[str, float | None] = {}
    for name in REQUIRED_PERCENTILES:
        value = features.get(f"{name}_percentile")
        percentiles[name] = round(float(value) * 100, 3) if isinstance(
            value, (int, float)
        ) else None
    return adapt_market_observation({
        "status": "audited_diagnostic_not_validated",
        "state": features.get("confirmed_state"),
        "raw_state": features.get("raw_state"),
        "profitability_tested": False,
        "applied_to_live_positions": False,
        "percentiles_pct": percentiles,
    }, as_of=row.get("trade_date"))


def _sector_observation(raw_packet: dict[str, Any]) -> dict[str, Any]:
    source = raw_packet.get("sector_source") or {}
    rows = raw_packet.get("sector_rows") or []
    snapshot = {
        "status": source.get("source_status"),
        "health_status": source.get("source_health_status"),
        "data_as_of": source.get("source_data_as_of"),
        "meta": source.get("meta") or {},
        "industries": [row for row in rows if row.get("kind") == "industry"],
        "concepts": [row for row in rows if row.get("kind") == "concept"],
    }
    captured_at = datetime.fromisoformat(str(raw_packet["captured_at"]))
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=SH_TZ)
    return adapt_sector_snapshot(snapshot, now=captured_at.astimezone(SH_TZ))


def _build_shadow_decision(
    raw_path: Path, raw_packet: dict[str, Any], *, project_root: Path,
    protocol_path: Path, protocol_sha256: str,
) -> dict[str, Any]:
    market = _market_observation(raw_packet, project_root=project_root)
    sector = _sector_observation(raw_packet)
    route = build_research_strategy_route(market, sector, {
        "cash_user_recorded": False,
        "valuation_status": "not_bound_for_research_route",
        "account_snapshot_sha256": None,
    })
    return {
        "schema": "market_sector_route_shadow_decision.v1",
        "decision_day": raw_packet.get("decision_day"),
        "slot_id": raw_packet.get("slot_id"),
        "decision_at": raw_packet.get("captured_at"),
        "no_backfill": True,
        "protocol": {
            "path": str(protocol_path.relative_to(project_root)),
            "sha256": protocol_sha256,
        },
        "raw_input_packet": {
            "path": str(raw_path.relative_to(project_root)),
            "sha256": _sha256(raw_path),
            "packet_id": raw_packet.get("packet_id"),
            "source_capture_status": raw_packet.get("source_capture_status"),
        },
        "market_input": market,
        "sector_input": sector,
        "route": route,
        "public_method_distillation": {
            "path": "research/PUBLIC_TRADER_METHOD_DISTILLATION_V2.json",
            "sha256": (
                "d101df7771decf7d33b821c730be473209fc475fb25f8c99d16efbe95ad18bac"
            ),
            "named_person_signal_input": False,
        },
        "returns_or_labels_read": False,
        "orders_fills_returns_or_nav_computed": False,
        "authority": {
            "research_only": True,
            "changes_unified_score": False,
            "changes_trade_count": False,
            "changes_position_or_quantity": False,
            "generates_buy_or_sell_alert": False,
            "selects_production_candidate": False,
            "production_strategy": False,
            "automatic_trading": False,
        },
    }


def verify_shadow_decision_packet(
    path: Path, *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        resolved = _resolve_project_path(path, project_root=project_root)
        packet = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"packet_unreadable:{exc}"]}
    if packet.get("schema") != "market_sector_route_shadow_decision.v1":
        errors.append("schema_invalid")
    identity = dict(packet)
    packet_id = str(identity.pop("packet_id", ""))
    if hashlib.sha256(_canonical_bytes(identity)).hexdigest() != packet_id:
        errors.append("packet_id_invalid")
    if packet.get("no_backfill") is not True:
        errors.append("no_backfill_invalid")
    if packet.get("returns_or_labels_read") is not False or packet.get(
        "orders_fills_returns_or_nav_computed"
    ) is not False:
        errors.append("outcome_boundary_invalid")
    authority = packet.get("authority") or {}
    if authority.get("research_only") is not True or any(
        authority.get(key) is not False for key in (
            "changes_unified_score", "changes_trade_count",
            "changes_position_or_quantity", "generates_buy_or_sell_alert",
            "selects_production_candidate", "production_strategy",
            "automatic_trading",
        )
    ):
        errors.append("authority_invalid")
    protocol = packet.get("protocol") or {}
    protocol_path: Path | None = None
    protocol_check: dict[str, Any] = {}
    try:
        protocol_path = _resolve_project_path(
            protocol.get("path"), project_root=project_root,
        )
        protocol_check = verify_shadow_protocol(
            project_root=project_root,
            protocol_path=protocol_path,
            expected_protocol_sha256=protocol.get("sha256"),
        )
        if protocol_check.get("valid") is not True:
            errors.append("protocol_invalid")
        activation_error = _activation_error(protocol_check.get("protocol") or {}, packet.get("decision_at"))
        if activation_error:
            errors.append(activation_error)
    except (OSError, ValueError):
        errors.append("protocol_invalid")
    raw = packet.get("raw_input_packet") or {}
    raw_path: Path | None = None
    raw_packet: dict[str, Any] | None = None
    try:
        raw_path = _resolve_project_path(raw.get("path"), project_root=project_root)
        raw_check = verify_market_sector_route_packet(
            raw_path, project_root=project_root,
        )
        candidate_raw = raw_check.get("packet") or {}
        if (
            raw_check.get("valid") is not True
            or _sha256(raw_path) != raw.get("sha256")
            or candidate_raw.get("packet_id") != raw.get("packet_id")
            or candidate_raw.get("decision_day")
            != packet.get("decision_day")
            or candidate_raw.get("slot_id") != packet.get("slot_id")
        ):
            errors.append("raw_input_identity_invalid")
        else:
            raw_packet = candidate_raw
            time_error = _raw_time_error(raw_packet)
            if time_error:
                errors.append(time_error)
    except (OSError, ValueError):
        errors.append("raw_input_identity_invalid")
    if (
        protocol_path is not None and protocol_check.get("valid") is True
        and raw_path is not None and raw_packet is not None
    ):
        try:
            expected = _build_shadow_decision(
                raw_path, raw_packet, project_root=project_root,
                protocol_path=protocol_path,
                protocol_sha256=str(protocol_check.get("protocol_sha256") or ""),
            )
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            errors.append("deterministic_recompute_failed")
        else:
            actual = dict(packet)
            actual.pop("packet_id", None)
            if actual != expected:
                errors.append("deterministic_recompute_mismatch")
    route_authority = (packet.get("route") or {}).get("authority") or {}
    if route_authority.get("research_only") is not True or any(
        route_authority.get(key) is not False for key in (
            "changes_unified_score", "changes_trade_count",
            "changes_position_or_quantity", "generates_buy_or_sell_alert",
            "production_strategy", "automatic_trading",
        )
    ):
        errors.append("route_authority_invalid")
    return {
        "valid": not errors,
        "errors": errors,
        "packet_id": packet_id,
        "packet": packet,
    }


def capture_shadow_route_decision(
    raw_capture: dict[str, Any], *, now: datetime | None = None,
    project_root: Path = PROJECT_ROOT, protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str | None = PROTOCOL_SHA256,
    packet_dir: Path = PACKET_DIR,
) -> dict[str, Any]:
    observed = (now or datetime.now(SH_TZ)).astimezone(SH_TZ)
    protocol_check = verify_shadow_protocol(
        project_root=project_root,
        protocol_path=protocol_path,
        expected_protocol_sha256=expected_protocol_sha256,
    )
    if protocol_check.get("valid") is not True:
        return {
            "status": "identity_invalid", "captured": False,
            "errors": protocol_check.get("errors") or [],
        }
    try:
        raw_path = _resolve_project_path(raw_capture.get("path"), project_root=project_root)
    except ValueError:
        return {"status": "raw_input_invalid", "captured": False}
    raw_check = verify_market_sector_route_packet(raw_path, project_root=project_root)
    if raw_check.get("valid") is not True:
        return {
            "status": "raw_input_invalid", "captured": False,
            "errors": raw_check.get("errors") or [],
        }
    raw_packet = raw_check["packet"]
    activation_error = _activation_error(protocol_check["protocol"], raw_packet.get("captured_at"))
    if activation_error:
        return {"status": activation_error, "captured": False}
    time_error = _raw_time_error(raw_packet, observed)
    if time_error:
        return {"status": time_error, "captured": False}
    if (
        observed.date().isoformat() != raw_packet.get("decision_day")
        or capture_slot(observed) != raw_packet.get("slot_id")
    ):
        return {"status": "outside_original_capture_slot", "captured": False}
    try:
        packet = _build_shadow_decision(
            raw_path, raw_packet,
            project_root=project_root,
            protocol_path=protocol_path,
            protocol_sha256=protocol_check["protocol_sha256"],
        )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return {
            "status": "input_adaptation_failed", "captured": False,
            "reason": f"{type(exc).__name__}:{exc}",
        }
    packet["packet_id"] = hashlib.sha256(_canonical_bytes(packet)).hexdigest()
    path = _resolve_project_path(packet_dir / f"{packet['decision_day']}-{packet['slot_id']}.json", project_root=project_root)
    if path.exists():
        verified = verify_shadow_decision_packet(path, project_root=project_root)
        return {
            "status": "already_captured" if verified.get("valid") else "existing_packet_invalid",
            "captured": verified.get("valid") is True,
            "path": str(path),
            "packet_id": verified.get("packet_id"),
            "errors": verified.get("errors") or [],
        }
    _atomic_publish(path, _canonical_bytes(packet))
    verified = verify_shadow_decision_packet(path, project_root=project_root)
    if verified.get("valid") is not True:
        raise RuntimeError("shadow route packet failed post-write verification")
    return {
        "status": "captured", "captured": True,
        "path": str(path), "packet_id": packet["packet_id"],
        "route_status": packet["route"]["status"],
        "market_state": packet["route"]["market_state"],
        "changes_trade_count_or_position": False,
    }


__all__ = [
    "capture_shadow_route_decision", "verify_shadow_decision_packet",
    "verify_shadow_protocol",
]
