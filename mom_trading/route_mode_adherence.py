"""Research-only observation of adherence to a frozen strategy route."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .market_state_daily import PROJECT_ROOT, _canonical_bytes, _sha256


PROTOCOL_PATH = (
    PROJECT_ROOT
    / "research/ANONYMOUS_ROUTE_MODE_ADHERENCE_FORWARD_V2_AMENDMENT_2.json"
)
PROTOCOL_SHA256 = "3a13c6c7d294a34a4cb96228c72c660a4e091e63b18784e169ed706bc7e0cd7e"
SUPPORTED_STREAM = "mainline_start_confirmation"
UNAVAILABLE_STREAMS = {
    "leader_divergence_restrengthening",
    "retreat_defense",
}
FALSE_AUTHORITY_KEYS = (
    "changes_unified_score",
    "changes_trade_count",
    "changes_position_or_quantity",
    "generates_buy_or_sell_alert",
    "selects_production_candidate",
    "production_strategy",
    "automatic_trading",
)
ROUTE_FALSE_AUTHORITY_KEYS = tuple(
    key for key in FALSE_AUTHORITY_KEYS if key != "selects_production_candidate"
)
AUTHORITY = {
    "research_only": True,
    "changes_unified_score": False,
    "changes_trade_count": False,
    "changes_position_or_quantity": False,
    "generates_buy_or_sell_alert": False,
    "generates_recommendation": False,
    "selects_production_candidate": False,
    "reads_or_computes_returns": False,
    "production_strategy": False,
    "automatic_trading": False,
    "stable_profit_claim": False,
}


def _project_path(raw: object, *, project_root: Path) -> Path:
    path = Path(str(raw or ""))
    resolved = (path if path.is_absolute() else project_root / path).resolve()
    resolved.relative_to(project_root.resolve())
    return resolved


def verify_protocol(
    *, project_root: Path = PROJECT_ROOT, protocol_path: Path = PROTOCOL_PATH,
    expected_sha256: str | None = PROTOCOL_SHA256,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        protocol_sha = _sha256(protocol_path)
        if expected_sha256 and protocol_sha != expected_sha256:
            errors.append("protocol_sha256_mismatch")
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        if protocol.get("status") != "frozen_before_first_observation_no_outcome_access":
            errors.append("protocol_status_invalid")
        for name, binding in (protocol.get("bindings") or {}).items():
            path = _project_path(binding.get("path"), project_root=project_root)
            if not path.is_file() or _sha256(path) != binding.get("sha256"):
                errors.append(f"binding_invalid:{name}")
        authority = protocol.get("authority") or {}
        if authority.get("research_only") is not True or any(
            authority.get(key) is not False for key in (
                *FALSE_AUTHORITY_KEYS,
                "generates_recommendation",
                "reads_or_computes_returns",
                "stable_profit_claim",
            )
        ):
            errors.append("authority_invalid")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        protocol_sha, protocol = None, {}
        errors.append(f"protocol_unreadable:{exc}")
    return {
        "valid": not errors,
        "errors": errors,
        "protocol": protocol,
        "protocol_sha256": protocol_sha,
    }


def _packet_id_valid(packet: dict[str, Any]) -> bool:
    identity = dict(packet)
    packet_id = str(identity.pop("packet_id", ""))
    try:
        actual = hashlib.sha256(_canonical_bytes(identity)).hexdigest()
    except (TypeError, ValueError):
        return False
    return bool(packet_id) and actual == packet_id


def _authority_safe(
    value: object, *, false_keys: tuple[str, ...] = FALSE_AUTHORITY_KEYS,
) -> bool:
    authority = value if isinstance(value, dict) else {}
    return authority.get("research_only") is True and all(
        authority.get(key) is False for key in false_keys
    )


def _aware_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _as_of_not_after(value: object, decision_at: datetime) -> bool | None:
    if not value:
        return None
    text = str(value)
    if len(text) == 10:
        try:
            return date.fromisoformat(text) <= decision_at.date()
        except ValueError:
            return None
    parsed = _aware_datetime(text)
    if parsed is None:
        return None
    return parsed <= decision_at


def _result(
    *, status: str, decision_day: object, slot_id: object, subject_id: object,
    playbook_id: object, missing: list[str], violations: list[str],
) -> dict[str, Any]:
    inside = status == "inside_mode"
    outside = status == "outside_mode"
    return {
        "schema": "anonymous_route_mode_adherence_forward.v1",
        "status": status,
        "observation_key": {
            "decision_day": decision_day,
            "slot_id": slot_id,
            "plan_or_candidate_id": subject_id,
        },
        "playbook_id": playbook_id,
        "inside_mode": inside if not missing else None,
        "outside_mode": outside if not missing else None,
        "missing_inputs": list(dict.fromkeys(missing)),
        "violations": list(dict.fromkeys(violations)),
        "no_backfill": True,
        "returns_or_labels_read": False,
        "authority": dict(AUTHORITY),
    }


def evaluate_route_mode_adherence(
    route_packet: dict[str, Any] | None,
    candidate_packet: dict[str, Any] | None,
    manual_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify one frozen plan/candidate observation without trade authority."""

    route_packet = route_packet if isinstance(route_packet, dict) else {}
    candidate_packet = candidate_packet if isinstance(candidate_packet, dict) else {}
    plan_supplied = manual_plan is not None
    manual_plan = manual_plan if isinstance(manual_plan, dict) else {}
    result = candidate_packet.get("research_result")
    result = result if isinstance(result, dict) else {}
    playbook = manual_plan.get("playbook_id") if plan_supplied else result.get("playbook_id")
    subject_id = manual_plan.get("plan_id") if plan_supplied else candidate_packet.get("packet_id")
    decision_day = route_packet.get("decision_day")
    slot_id = route_packet.get("slot_id")

    if playbook in UNAVAILABLE_STREAMS:
        return _result(
            status="missing", decision_day=decision_day, slot_id=slot_id,
            subject_id=subject_id, playbook_id=playbook,
            missing=[f"candidate_stream_unavailable:{playbook}"], violations=[],
        )

    missing: list[str] = []
    if verify_protocol().get("valid") is not True:
        missing.append("protocol_identity")
    if not route_packet:
        missing.append("route_packet")
    if not candidate_packet:
        missing.append("candidate_packet")
    if plan_supplied and not manual_plan:
        missing.append("manual_plan")
    if playbook != SUPPORTED_STREAM:
        missing.append("supported_playbook_id")
    if route_packet and not _packet_id_valid(route_packet):
        missing.append("route_packet_identity")
    if candidate_packet and not _packet_id_valid(candidate_packet):
        missing.append("candidate_packet_identity")
    if route_packet and not _authority_safe(route_packet.get("authority")):
        missing.append("route_packet_authority")
    if candidate_packet and not _authority_safe(candidate_packet.get("authority")):
        missing.append("candidate_packet_authority")

    route = route_packet.get("route") if isinstance(route_packet.get("route"), dict) else {}
    if route and not _authority_safe(
        route.get("authority"), false_keys=ROUTE_FALSE_AUTHORITY_KEYS,
    ):
        missing.append("route_authority")
    budget = route.get("research_budget_hypothesis") if isinstance(
        route.get("research_budget_hypothesis"), dict
    ) else {}
    sector = route.get("sector_confirmation") if isinstance(
        route.get("sector_confirmation"), dict
    ) else {}
    selected = result.get("selected") if isinstance(result.get("selected"), list) else []
    required = {
        "route_schema": route_packet.get("schema"),
        "decision_day": decision_day,
        "slot_id": slot_id,
        "decision_at": route_packet.get("decision_at"),
        "route_status": route.get("status"),
        "market_state_as_of": route.get("market_state_as_of"),
        "allowed_playbooks": budget.get("allowed_playbooks"),
        "sector_observation_slot": sector.get("observation_slot"),
        "sector_as_of": sector.get("as_of"),
        "sector_source_as_of": sector.get("source_as_of"),
        "sector_confirmed_sectors": sector.get("confirmed_sectors"),
        "candidate_schema": candidate_packet.get("schema"),
        "candidate_decision_day": candidate_packet.get("decision_day"),
        "candidate_slot_id": candidate_packet.get("slot_id"),
        "candidate_shadow_packet_id": (
            candidate_packet.get("shadow_decision") or {}
        ).get("packet_id") if isinstance(
            candidate_packet.get("shadow_decision"), dict
        ) else None,
        "candidate_status": result.get("status"),
        "candidate_playbook_id": result.get("playbook_id"),
        "candidate_selected": selected,
    }
    missing.extend(name for name, value in required.items() if value is None or value == "")
    if not selected:
        missing.append("candidate_selected")
    if any(not isinstance(row, dict) or not row.get("data_as_of") for row in selected):
        missing.append("candidate_data_as_of")
    if plan_supplied:
        for key in (
            "plan_id", "decision_day", "slot_id", "created_at", "playbook_id",
            "candidate_id", "candidate_packet_id", "no_backfill",
        ):
            if manual_plan.get(key) is None or manual_plan.get(key) == "":
                missing.append(f"manual_plan_{key}")
    decision_at = _aware_datetime(route_packet.get("decision_at"))
    if decision_at is None:
        missing.append("decision_at_timezone")
    if missing:
        return _result(
            status="missing", decision_day=decision_day, slot_id=slot_id,
            subject_id=subject_id, playbook_id=playbook,
            missing=missing, violations=[],
        )

    allowed = {
        str(item.get("id")) for item in budget["allowed_playbooks"]
        if isinstance(item, dict) and item.get("id")
    }
    violations: list[str] = []
    if route_packet.get("schema") != "market_sector_route_shadow_decision.v1":
        violations.append("route_schema_invalid")
    if candidate_packet.get("schema") != "mainline_start_candidate_research.v1":
        violations.append("candidate_schema_invalid")
    if route.get("status") != "research_route_ready":
        violations.append("route_not_ready")
    if playbook not in allowed:
        violations.append("playbook_not_allowed")
    if result.get("status") != "candidate_research_ready":
        violations.append("candidate_not_ready")
    if result.get("playbook_id") != playbook:
        violations.append("candidate_playbook_mismatch")
    if candidate_packet.get("decision_day") != decision_day:
        violations.append("candidate_decision_day_mismatch")
    if candidate_packet.get("slot_id") != slot_id:
        violations.append("candidate_slot_mismatch")
    shadow_reference = candidate_packet.get("shadow_decision")
    shadow_reference = shadow_reference if isinstance(shadow_reference, dict) else {}
    if shadow_reference.get("packet_id") != route_packet.get("packet_id"):
        violations.append("candidate_route_identity_mismatch")
    if route_packet.get("no_backfill") is not True or candidate_packet.get("no_backfill") is not True:
        violations.append("no_backfill_contract_violated")
    if route_packet.get("returns_or_labels_read") is not False or candidate_packet.get("returns_or_labels_read") is not False:
        violations.append("outcome_boundary_violated")
    if route_packet.get("orders_fills_returns_or_nav_computed") is not False:
        violations.append("route_outcome_boundary_violated")
    if decision_day != decision_at.date().isoformat():
        violations.append("decision_day_mismatch")
    if sector.get("required") is not True or sector.get("ready") is not True:
        violations.append("sector_confirmation_not_ready")
    if sector.get("observation_slot") != slot_id:
        violations.append("sector_slot_mismatch")

    confirmed = {
        (str(item.get("kind")), str(item.get("code")))
        for item in sector.get("confirmed_sectors") or [] if isinstance(item, dict)
    }
    candidate_rows = selected
    if plan_supplied:
        candidate_rows = [
            row for row in selected
            if str(row.get("stock_code")) == str(manual_plan.get("candidate_id"))
        ]
        if not candidate_rows:
            violations.append("manual_plan_candidate_not_selected")
        if manual_plan.get("candidate_packet_id") != candidate_packet.get("packet_id"):
            violations.append("manual_plan_candidate_packet_mismatch")
        if manual_plan.get("decision_day") != decision_day or manual_plan.get("slot_id") != slot_id:
            violations.append("manual_plan_decision_identity_mismatch")
        if manual_plan.get("no_backfill") is not True:
            violations.append("manual_plan_no_backfill_violated")
        created_at = _aware_datetime(manual_plan.get("created_at"))
        if created_at is None:
            return _result(
                status="missing", decision_day=decision_day, slot_id=slot_id,
                subject_id=subject_id, playbook_id=playbook,
                missing=["manual_plan_created_at_timezone"], violations=[],
            )
        if created_at < decision_at or created_at.date() != decision_at.date():
            violations.append("manual_plan_created_at_outside_decision_day")

    for row in candidate_rows:
        memberships = {
            (str(item.get("kind")), str(item.get("code")))
            for item in row.get("sectors") or [] if isinstance(item, dict)
        }
        if not memberships.intersection(confirmed):
            violations.append("candidate_not_in_confirmed_sector")
    for name, value in (
        ("market_state_as_of", route.get("market_state_as_of")),
        ("sector_as_of", sector.get("as_of")),
        ("sector_source_as_of", sector.get("source_as_of")),
        *[(f"candidate_data_as_of:{row.get('stock_code')}", row.get("data_as_of")) for row in candidate_rows],
    ):
        available = _as_of_not_after(value, decision_at)
        if available is None:
            return _result(
                status="missing", decision_day=decision_day, slot_id=slot_id,
                subject_id=subject_id, playbook_id=playbook,
                missing=[name], violations=[],
            )
        if available is False:
            violations.append(f"as_of_after_decision:{name}")

    return _result(
        status="outside_mode" if violations else "inside_mode",
        decision_day=decision_day, slot_id=slot_id, subject_id=subject_id,
        playbook_id=playbook, missing=[], violations=violations,
    )


__all__ = ["evaluate_route_mode_adherence", "verify_protocol"]
