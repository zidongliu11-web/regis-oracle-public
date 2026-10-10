"""Deep, read-only integrity checks for the nine forward-capture artifacts.

The individual collectors remain immutable.  This module verifies their
cross-file identities and point-in-time boundaries before observability calls a
slot complete.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .h3_execution_forward import verify_h3_forward_packet
from .intraday_market_emotion_forward import verify_intraday_emotion_packet
from .intraday_route_candidate_forward import verify_intraday_packet
from .mainline_start_candidates import verify_candidate_packet
from .market_sector_constituent_forward import verify_constituent_packet
from .market_sector_route_forward import verify_market_sector_route_packet
from .market_sector_route_shadow import verify_shadow_decision_packet
from .market_state_forward import verify_forward_packet
from .research_artifact_paths import research_artifact_path
from .unified_regime_route_replay import verify_joint_replay_packet


SH_TZ = ZoneInfo("Asia/Shanghai")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_SOURCE_AGE_SECONDS = 180

_VERIFIERS: dict[str, Callable[..., dict[str, Any]]] = {
    "market_state": verify_forward_packet,
    "attention_crowding": verify_h3_forward_packet,
    "intraday_emotion": verify_intraday_emotion_packet,
    "market_sector": verify_market_sector_route_packet,
    "route_decision": verify_shadow_decision_packet,
    "sector_constituents": verify_constituent_packet,
    "stock_candidates": verify_candidate_packet,
    "joint_account_replay": verify_joint_replay_packet,
    "candidate_30m": verify_intraday_packet,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _time(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SH_TZ)
    return parsed.astimezone(SH_TZ)


def _project_path(value: object, project_root: Path) -> Path:
    # Compare physical identities only after checking the configured data alias.
    return research_artifact_path(value, project_root=project_root).resolve()


def _file_binding_errors(
    binding: object, *, project_root: Path, label: str,
    expected_path: Path | None = None, expected_packet_id: object = None,
) -> list[str]:
    row = binding if isinstance(binding, dict) else {}
    errors: list[str] = []
    try:
        path = _project_path(row.get("path"), project_root)
    except (OSError, ValueError):
        return [f"{label}_path_invalid"]
    if expected_path is not None and path != expected_path.resolve():
        errors.append(f"{label}_path_mismatch")
    if not path.is_file() or _sha256(path) != row.get("sha256"):
        errors.append(f"{label}_sha256_invalid")
    if expected_packet_id is not None and row.get("packet_id") != expected_packet_id:
        errors.append(f"{label}_packet_id_mismatch")
    return errors


def _same_decision_identity(
    packet: dict[str, Any], decision_day: str, slot_id: str,
) -> list[str]:
    errors = []
    if packet.get("decision_day") != decision_day:
        errors.append("decision_day_mismatch")
    if packet.get("slot_id") != slot_id:
        errors.append("slot_id_mismatch")
    return errors


def _source_time_errors(
    value: object, *, captured_at: object, decision_day: str,
    label: str, maximum_age_seconds: int = MAX_SOURCE_AGE_SECONDS,
    maximum_future_skew_seconds: int = 0,
) -> list[str]:
    source = _time(value)
    captured = _time(captured_at)
    if source is None or captured is None:
        return [f"{label}_time_invalid"]
    errors = []
    if source.date().isoformat() != decision_day:
        errors.append(f"{label}_not_same_decision_day")
    age = (captured - source).total_seconds()
    if age > maximum_age_seconds:
        errors.append(f"{label}_stale")
    if age < -maximum_future_skew_seconds:
        errors.append(f"{label}_future")
    return errors


def _protocol_errors(packet: dict[str, Any], project_root: Path) -> list[str]:
    return _file_binding_errors(
        packet.get("protocol"), project_root=project_root, label="protocol",
    )


def _market_state_errors(packet: dict[str, Any], project_root: Path) -> list[str]:
    errors: list[str] = []
    manifest_ref = packet.get("selection_manifest") or {}
    try:
        manifest_path = _project_path(manifest_ref.get("path"), project_root)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return ["selection_manifest_unreadable"]
    if manifest_ref.get("manifest_id") != manifest.get("manifest_id"):
        errors.append("selection_manifest_id_mismatch")
    quotes = (manifest.get("selection_provenance") or {}).get(
        "quote_cross_section"
    ) or {}
    if quotes.get("coverage_complete") is not True:
        errors.append("selection_quote_coverage_incomplete")
    errors.extend(_source_time_errors(
        quotes.get("as_of"), captured_at=packet.get("decision_at"),
        decision_day=str(packet.get("decision_day") or ""),
        label="selection_quote",
    ))
    return errors


def verify_forward_selection_snapshot(
    snapshot: object, *, observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Reject stale full-market quote evidence before first-wins H1 writes."""
    source = snapshot if isinstance(snapshot, dict) else {}
    provenance = source.get("selection_provenance") or {}
    quotes = provenance.get("quote_cross_section") or {}
    observed = (observed_at or datetime.now(SH_TZ)).astimezone(SH_TZ)
    errors: list[str] = []
    if provenance.get("status") != "verified":
        errors.append("selection_provenance_not_verified")
    if quotes.get("coverage_complete") is not True:
        errors.append("selection_quote_coverage_incomplete")
    errors.extend(_source_time_errors(
        quotes.get("as_of"), captured_at=observed,
        decision_day=observed.date().isoformat(), label="selection_quote",
    ))
    return {"valid": not errors, "errors": list(dict.fromkeys(errors))}


def capture_h1_if_ready(
    snapshot: dict[str, Any], candidates: list[dict[str, Any]], *,
    h1_runtime: dict[str, Any], route_runtime: dict[str, Any],
    route_slot: str | None, capture: Callable[..., dict[str, Any]],
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Run the first-wins H1 write only inside the unified route sub-slot."""
    observed = (observed_at or datetime.now(SH_TZ)).astimezone(SH_TZ)
    if observed.date().isoformat() < str(route_runtime.get("forward_start") or ""):
        return {
            "status": "waiting_forward_start", "captured": False,
            "forward_start": route_runtime.get("forward_start"),
            "orders_or_returns_computed": False,
        }
    if route_slot is None:
        return {
            "status": "outside_capture_slot", "captured": False,
            "orders_or_returns_computed": False,
        }
    if h1_runtime.get("valid") is not True:
        return {
            "status": "identity_invalid", "captured": False,
            "reason": "h1_runtime_admission_invalid",
            "errors": h1_runtime.get("errors") or [],
            "orders_or_returns_computed": False,
        }
    selection = verify_forward_selection_snapshot(snapshot, observed_at=observed)
    if selection.get("valid") is not True:
        return {
            "status": "selection_provenance_invalid", "captured": False,
            "errors": selection.get("errors") or [],
            "orders_or_returns_computed": False,
        }
    result = capture(snapshot, candidates, now=observed)
    result["runtime_admission"] = {
        "admission_id": h1_runtime.get("admission_id"),
        "admission_sha256": h1_runtime.get("admission_sha256"),
    }
    return result


def forward_capture_wake_policy(
    schedule_status: dict[str, Any], *, observed_at: datetime | None = None,
    ordinary_refresh_seconds: int = 60,
    prepared_snapshot: dict[str, Any] | None = None,
) -> dict[str, object]:
    """Protect a three-minute capture slot from an ordinary heavy refresh."""
    observed = (observed_at or datetime.now(SH_TZ)).astimezone(SH_TZ)
    open_slots = [
        row for row in schedule_status.get("slots") or []
        if row.get("phase") == "window_open"
    ]
    capture_needed = any(
        row.get("missing_artifacts")
        or row.get("strict_forward_v2_missing") is True
        for row in open_slots
    )
    pre_slot_guard = False
    freshness_at = observed
    if schedule_status.get("next_slot"):
        try:
            next_slot = datetime.fromisoformat(
                str(schedule_status["next_slot"])
            ).astimezone(SH_TZ)
            seconds_to_slot = (next_slot - observed).total_seconds()
            pre_slot_guard = 0 < seconds_to_slot <= 180
            if pre_slot_guard:
                freshness_at = next_slot
        except ValueError:
            pass
    context_valid = False
    if pre_slot_guard or capture_needed:
        context_valid = verify_forward_selection_snapshot(
            prepared_snapshot or {}, observed_at=freshness_at,
        ).get("valid") is True
    return {
        "capture_needed": capture_needed, "pre_slot_guard": pre_slot_guard,
        "capture_context_valid": context_valid,
        "prepare_needed": bool((pre_slot_guard or capture_needed) and not context_valid),
        "wait_seconds": 5 if capture_needed or pre_slot_guard else ordinary_refresh_seconds,
    }


def _h3_errors(packet: dict[str, Any], project_root: Path) -> list[str]:
    errors = _protocol_errors(packet, project_root)
    observed_at = packet.get("observed_at")
    decision_day = str(packet.get("decision_day") or "")
    for index, row in enumerate(packet.get("observations") or []):
        if isinstance(row, dict) and row.get("status") == "observed":
            errors.extend(_source_time_errors(
                row.get("source_data_time"), captured_at=observed_at,
                decision_day=decision_day, label=f"h3_source_{index}",
                maximum_future_skew_seconds=30,
            ))
    return errors


def _market_sector_errors(packet: dict[str, Any], project_root: Path) -> list[str]:
    errors = _protocol_errors(packet, project_root)
    h3 = packet.get("h3_packet") or {}
    if h3.get("packet_id") or h3.get("path"):
        try:
            h3_path = _project_path(h3.get("path"), project_root)
            h3_packet = json.loads(h3_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            errors.append("h3_reference_invalid")
        else:
            if h3_packet.get("packet_id") != h3.get("packet_id"):
                errors.append("h3_reference_packet_id_mismatch")
            if (
                h3_packet.get("decision_day") != packet.get("decision_day")
                or h3_packet.get("slot_id") != packet.get("slot_id")
            ):
                errors.append("h3_reference_decision_identity_mismatch")
    return errors


def _constituent_errors(packet: dict[str, Any], project_root: Path) -> list[str]:
    errors = _protocol_errors(packet, project_root)
    captured_at = packet.get("captured_at")
    decision_day = str(packet.get("decision_day") or "")
    for capture_index, capture in enumerate(packet.get("sector_captures") or []):
        for row_index, row in enumerate((capture or {}).get("rows") or []):
            errors.extend(_source_time_errors(
                (row or {}).get("data_as_of"), captured_at=captured_at,
                decision_day=decision_day,
                label=f"constituent_{capture_index}_{row_index}",
            ))
    return errors


def _candidate_errors(packet: dict[str, Any], project_root: Path) -> list[str]:
    errors = _file_binding_errors(
        packet.get("formula"), project_root=project_root, label="formula",
    )
    for key in ("constituent_packet", "shadow_decision"):
        binding = packet.get(key) or {}
        try:
            path = _project_path(binding.get("path"), project_root)
            upstream = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            errors.append(f"{key}_unreadable")
            continue
        errors.extend(_file_binding_errors(
            binding, project_root=project_root, label=key,
            expected_path=path, expected_packet_id=upstream.get("packet_id"),
        ))
        if (
            upstream.get("decision_day") != packet.get("decision_day")
            or upstream.get("slot_id") != packet.get("slot_id")
        ):
            errors.append(f"{key}_decision_identity_mismatch")
    return errors


def _intraday_30m_errors(packet: dict[str, Any], project_root: Path) -> list[str]:
    errors = _protocol_errors(packet, project_root)
    binding = packet.get("candidate_packet") or {}
    try:
        path = _project_path(binding.get("path"), project_root)
        candidate = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return errors + ["candidate_packet_unreadable"]
    errors.extend(_file_binding_errors(
        binding, project_root=project_root, label="candidate_packet",
        expected_path=path, expected_packet_id=candidate.get("packet_id"),
    ))
    checked = verify_candidate_packet(path, project_root=project_root)
    if checked.get("valid") is not True:
        errors.append("candidate_packet_verification_failed")
        return errors
    if (
        candidate.get("decision_day") != packet.get("decision_day")
        or candidate.get("slot_id") != packet.get("slot_id")
    ):
        errors.append("candidate_packet_decision_identity_mismatch")
    selected = (candidate.get("research_result") or {}).get("selected") or []
    expected_codes = [
        str(row.get("stock_code") or "")
        for row in selected[:2] if isinstance(row, dict)
    ]
    captures = packet.get("candidate_captures")
    if not isinstance(captures, list):
        return errors + ["candidate_30m_captures_invalid"]
    actual_codes = [
        str(row.get("stock_code") or "")
        for row in captures if isinstance(row, dict)
    ]
    if len(actual_codes) != len(captures) or actual_codes != expected_codes:
        errors.append("candidate_30m_code_sequence_mismatch")
    if len(set(actual_codes)) != len(actual_codes):
        errors.append("candidate_30m_code_duplicate")
    if expected_codes:
        if packet.get("valid_empty_candidate_observation") is not False:
            errors.append("candidate_30m_empty_semantics_invalid")
        if packet.get("capture_status") != "complete" or any(
            row.get("complete") is not True or bool(row.get("errors"))
            for row in captures if isinstance(row, dict)
        ):
            errors.append("candidate_30m_capture_incomplete")
    elif (
        captures
        or packet.get("valid_empty_candidate_observation") is not True
        or packet.get("capture_status") != "complete"
    ):
        errors.append("candidate_30m_empty_semantics_invalid")
    return errors


_EXTRA_CHECKS: dict[str, Callable[[dict[str, Any], Path], list[str]]] = {
    "market_state": _market_state_errors,
    "attention_crowding": _h3_errors,
    "intraday_emotion": _protocol_errors,
    "market_sector": _market_sector_errors,
    "sector_constituents": _constituent_errors,
    "stock_candidates": _candidate_errors,
    "candidate_30m": _intraday_30m_errors,
}


def verify_capture_artifact(
    name: str, path: Path, *, decision_day: str, slot_id: str,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Verify one artifact plus cross-file and point-in-time invariants."""
    verifier = _VERIFIERS.get(name)
    if verifier is None:
        return {"valid": False, "errors": ["unknown_artifact"]}
    try:
        path = research_artifact_path(path, project_root=project_root)
    except (OSError, ValueError):
        return {"valid": False, "errors": ["artifact_path_invalid"]}
    try:
        result = verifier(path, project_root=project_root)
        packet = result.get("packet") if isinstance(result.get("packet"), dict) else {}
        errors = list(result.get("errors") or [])
        if result.get("valid") is not True:
            errors = errors or ["artifact_verification_failed"]
        errors.extend(_same_decision_identity(packet, decision_day, slot_id))
        extra = _EXTRA_CHECKS.get(name)
        # Never follow references from an unverified packet.
        if not errors and extra is not None:
            errors.extend(extra(packet, project_root))
        digest = _sha256(path) if path.is_file() else None
        if digest is None:
            errors.append("artifact_missing")
        errors = list(dict.fromkeys(errors))
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        # A corrupt/read-failed artifact is a visible gap, not a failed schedule
        # request or permission to promote the other incomplete artifacts.
        return {"valid": False, "errors": ["artifact_verification_error"],
                "error_type": type(exc).__name__}
    return {
        "valid": not errors,
        "errors": errors,
        "packet_id": packet.get("packet_id"),
        "captured_at": (
            packet.get("captured_at") or packet.get("decision_at")
            or packet.get("observed_at") or packet.get("created_at")
        ),
        "sha256": digest,
    }


def verify_forward_capture_bundle(
    *, decision_day: str, slot_id: str, project_root: Path = PROJECT_ROOT,
    packet_paths: dict[str, Path],
) -> dict[str, Any]:
    expected = set(_VERIFIERS)
    provided = set(packet_paths)
    artifacts = {
        name: verify_capture_artifact(
            name, packet_paths[name], decision_day=decision_day, slot_id=slot_id,
            project_root=project_root,
        )
        if name in packet_paths and packet_paths[name].is_file()
        else {"valid": False, "errors": ["artifact_missing"]}
        for name in sorted(expected)
    }
    for name in sorted(provided - expected):
        artifacts[name] = {"valid": False, "errors": ["unknown_artifact"]}
    invalid = [name for name, row in artifacts.items() if row.get("valid") is not True]
    return {
        "valid": not invalid, "decision_day": decision_day, "slot_id": slot_id,
        "invalid_artifacts": invalid, "artifacts": artifacts,
    }


__all__ = [
    "verify_capture_artifact", "verify_forward_capture_bundle",
    "verify_forward_selection_snapshot", "capture_h1_if_ready",
    "forward_capture_wake_policy",
]
