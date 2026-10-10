"""Bounded same-slot recovery for immutable sector-constituent captures."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .market_sector_constituent_forward import (
    PACKET_DIR,
    PROTOCOL_PATH as CONSTITUENT_PROTOCOL_PATH,
    PROTOCOL_SHA256 as CONSTITUENT_PROTOCOL_SHA256,
    PROJECT_ROOT,
    SH_TZ,
    capture_confirmed_sector_constituents,
    verify_constituent_packet,
)
from .market_sector_route_forward import capture_slot
from .market_state_daily import _atomic_publish, _sha256
from .research_artifact_paths import research_artifact_path


RECOVERY_PROTOCOL_PATH = (
    PROJECT_ROOT
    / "research/MARKET_SECTOR_CONSTITUENT_CAPTURE_RECOVERY_PROTOCOL_V3.json"
)
RECOVERY_PROTOCOL_SHA256 = (
    "332947257ae12680b9f79ac013a1fa42a8f2d00a601fbe527e285547a8ecd0e0"
)
ATTEMPT_DIR = (
    PROJECT_ROOT
    / "research/data/market_sector_route_forward/v1/constituent_attempts"
)
MAX_ATTEMPTS = 3


def _project_path(raw: object, *, project_root: Path) -> Path:
    path = Path(str(raw or ""))
    resolved = (path if path.is_absolute() else project_root / path).resolve()
    resolved.relative_to(project_root.resolve())
    return resolved


def verify_recovery_protocol(
    *, project_root: Path = PROJECT_ROOT,
    protocol_path: Path = RECOVERY_PROTOCOL_PATH,
    expected_protocol_sha256: str | None = RECOVERY_PROTOCOL_SHA256,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        protocol_path = _project_path(protocol_path, project_root=project_root)
        protocol_sha = _sha256(protocol_path)
        if expected_protocol_sha256 and protocol_sha != expected_protocol_sha256:
            errors.append("protocol_sha256_mismatch")
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        if protocol.get("status") != "frozen_before_next_native_slot_no_outcome_access":
            errors.append("protocol_status_invalid")
        for name, binding in (protocol.get("bindings") or {}).items():
            path = _project_path(binding.get("path"), project_root=project_root)
            if not path.is_file() or _sha256(path) != binding.get("sha256"):
                errors.append(f"binding_invalid:{name}")
        recovery = protocol.get("recovery") or {}
        if (
            recovery.get("maximum_full_snapshot_attempts") != MAX_ATTEMPTS
            or recovery.get("incomplete_attempt_occupies_canonical_slot") is not False
            or recovery.get("only_complete_verified_packet_can_be_published_canonical")
            is not True
            or recovery.get("rows_are_not_merged_across_attempts") is not True
            or recovery.get("historical_backfill") is not False
        ):
            errors.append("recovery_policy_invalid")
        authority = protocol.get("authority") or {}
        if authority.get("research_only") is not True or any(
            authority.get(key) is not False for key in (
                "changes_unified_score", "changes_trade_count",
                "changes_position_or_quantity", "generates_buy_or_sell_alert",
                "selects_production_candidate", "production_strategy",
                "automatic_trading", "stable_profit_claim",
            )
        ):
            errors.append("authority_invalid")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        protocol_sha, protocol = None, {}
        errors.append(f"protocol_unreadable:{exc}")
    return {
        "valid": not errors, "errors": errors, "protocol": protocol,
        "protocol_sha256": protocol_sha,
    }


def _existing_result(
    path: Path, *, project_root: Path,
    packet_verifier: Callable[..., dict[str, Any]],
    shadow_verifier: Callable[..., dict[str, Any]] | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"project_root": project_root}
    if shadow_verifier is not None:
        kwargs["shadow_verifier"] = shadow_verifier
    verified = packet_verifier(path, **kwargs)
    packet = verified.get("packet") or {}
    if verified.get("valid") is not True:
        return {
            "status": "existing_packet_invalid", "captured": False,
            "path": str(path), "errors": verified.get("errors") or [],
        }
    if packet.get("capture_status") != "complete":
        return {
            "status": "existing_incomplete_packet_fail_closed",
            "captured": False, "capture_status": packet.get("capture_status"),
            "path": str(path), "packet_id": verified.get("packet_id"),
            "errors": ["complete_constituent_packet_required"],
        }
    return {
        "status": "already_captured", "captured": True,
        "capture_status": "complete", "path": str(path),
        "packet_id": verified.get("packet_id"), "errors": [],
    }


def capture_complete_sector_constituents(
    shadow_capture: dict[str, Any],
    fetch_page: Callable[[str, int], dict[str, Any] | None], *,
    now: datetime | None = None, project_root: Path = PROJECT_ROOT,
    constituent_protocol_path: Path = CONSTITUENT_PROTOCOL_PATH,
    expected_constituent_protocol_sha256: str | None = CONSTITUENT_PROTOCOL_SHA256,
    recovery_protocol_path: Path = RECOVERY_PROTOCOL_PATH,
    expected_recovery_protocol_sha256: str | None = RECOVERY_PROTOCOL_SHA256,
    packet_dir: Path = PACKET_DIR, attempt_dir: Path = ATTEMPT_DIR,
    shadow_verifier: Callable[..., dict[str, Any]] | None = None,
    packet_verifier: Callable[..., dict[str, Any]] = verify_constituent_packet,
    capture_fn: Callable[..., dict[str, Any]] = capture_confirmed_sector_constituents,
) -> dict[str, Any]:
    """Publish the first complete attempt; preserve incomplete attempts separately."""
    protocol_check = verify_recovery_protocol(
        project_root=project_root, protocol_path=recovery_protocol_path,
        expected_protocol_sha256=expected_recovery_protocol_sha256,
    )
    if protocol_check.get("valid") is not True:
        return {
            "status": "recovery_identity_invalid", "captured": False,
            "errors": protocol_check.get("errors") or [],
        }
    observed = (now or datetime.now(SH_TZ)).astimezone(SH_TZ)
    slot_id = capture_slot(observed)
    if slot_id is None:
        return {"status": "outside_capture_slot", "captured": False}
    try:
        packet_dir = research_artifact_path(packet_dir, project_root=project_root)
        attempt_dir = research_artifact_path(attempt_dir, project_root=project_root)
        canonical = research_artifact_path(
            packet_dir / f"{observed.date().isoformat()}-{slot_id}.json", project_root=project_root,
        )
    except ValueError:
        return {"status": "capture_path_invalid", "captured": False}
    verifier_kwargs: dict[str, Any] = {"project_root": project_root}
    if shadow_verifier is not None:
        verifier_kwargs["shadow_verifier"] = shadow_verifier
    if canonical.exists():
        return _existing_result(
            canonical, project_root=project_root,
            packet_verifier=packet_verifier,
            shadow_verifier=verifier_kwargs.get("shadow_verifier"),
        )

    attempts: list[dict[str, Any]] = []
    for attempt_number in range(1, MAX_ATTEMPTS + 1):
        attempt_observed = observed if now is not None else datetime.now(SH_TZ)
        isolated_dir = (
            attempt_dir / attempt_observed.date().isoformat()
            / attempt_observed.strftime("%H%M%S%f")
            / f"attempt-{attempt_number}"
        )
        capture_kwargs: dict[str, Any] = {
            "now": attempt_observed,
            "project_root": project_root,
            "protocol_path": constituent_protocol_path,
            "expected_protocol_sha256": expected_constituent_protocol_sha256,
            "packet_dir": isolated_dir,
        }
        if shadow_verifier is not None:
            capture_kwargs["shadow_verifier"] = shadow_verifier
        result = capture_fn(shadow_capture, fetch_page, **capture_kwargs)
        if result.get("captured") is not True:
            return {
                **result, "attempt_count": attempt_number,
                "attempts": attempts,
            }
        path = Path(str(result.get("path") or ""))
        verified = packet_verifier(path, **verifier_kwargs)
        packet = verified.get("packet") or {}
        attempt_row = {
            "attempt": attempt_number, "path": str(path),
            "packet_id": verified.get("packet_id"),
            "valid": verified.get("valid") is True,
            "capture_status": packet.get("capture_status"),
            "errors": verified.get("errors") or [],
        }
        attempts.append(attempt_row)
        if verified.get("valid") is not True:
            return {
                "status": "attempt_packet_invalid", "captured": False,
                "attempt_count": attempt_number, "attempts": attempts,
                "errors": verified.get("errors") or [],
            }
        if packet.get("capture_status") != "complete":
            continue
        canonical = packet_dir / f"{packet['decision_day']}-{packet['slot_id']}.json"
        if canonical.exists():
            return _existing_result(
                canonical, project_root=project_root,
                packet_verifier=packet_verifier,
                shadow_verifier=verifier_kwargs.get("shadow_verifier"),
            )
        _atomic_publish(canonical, path.read_bytes())
        canonical_verified = packet_verifier(canonical, **verifier_kwargs)
        if canonical_verified.get("valid") is not True:
            raise RuntimeError("canonical constituent packet failed verification")
        return {
            "status": "captured",
            "captured": True, "capture_status": "complete",
            "path": str(canonical),
            "packet_id": canonical_verified.get("packet_id"),
            "attempt_count": attempt_number, "attempts": attempts,
            "recovered_after_incomplete_attempt": attempt_number > 1,
            "confirmed_sector_count": packet.get("confirmed_sector_count"),
            "constituent_count": sum(
                int(row.get("fetched_count") or 0)
                for row in packet.get("sector_captures") or []
            ),
            "changes_trade_count_or_position": False,
        }
    return {
        "status": "incomplete_retryable_exhausted", "captured": False,
        "capture_status": "incomplete", "attempt_count": len(attempts),
        "attempts": attempts,
        "errors": ["complete_constituent_packet_required"],
        "changes_trade_count_or_position": False,
    }


__all__ = [
    "capture_complete_sector_constituents", "verify_recovery_protocol",
]
