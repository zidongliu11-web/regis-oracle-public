"""Append-only strict-forward V2 research decision capture."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from .research_artifact_paths import research_artifact_path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .market_sector_route_forward import (
    PROTOCOL_PATH as MARKET_SECTOR_PROTOCOL_PATH,
    PROTOCOL_SHA256 as MARKET_SECTOR_PROTOCOL_SHA256,
    capture_slot,
)
from .market_state_daily import (
    PROJECT_ROOT,
    SnapshotConflict,
    _atomic_publish,
    _canonical_bytes,
)
from .system_route_account_packet_v2 import (
    build_system_route_account_packet_v2,
    verify_system_route_account_packet_v2,
)
from .system_route_joint_v2_gate import (
    ADMISSION_PATH,
    verify_system_route_joint_v2_runtime,
)
from .system_route_joint_v2_adapter import (
    ADAPTER_PROTOCOL_PATH,
    ADAPTER_PROTOCOL_SHA256,
    adapt_system_route_forward_packets,
)
from .trading_calendar import next_trading_day_after, trading_day_status
from .research_artifact_paths import research_artifact_path


SH_TZ = ZoneInfo("Asia/Shanghai")
PROTOCOL_PATH = (
    PROJECT_ROOT
    / "research/SYSTEM_ROUTE_JOINT_V2_FORWARD_PERSISTENCE_PROTOCOL_V2_AMENDMENT_15.json"
)
PROTOCOL_SHA256 = "3d8ebc5f6b2b45ce5418ae7c0d0c26d7b21be2ce7f96ee54510d9a120ea87647"
ACCOUNT_DIR = (
    PROJECT_ROOT / "research/data/system_route_joint_replay/v2/account_snapshots"
)
DECISION_DIR = (
    PROJECT_ROOT / "research/data/system_route_joint_replay/v2/strict_forward_decisions"
)
_CAPTURE_INPUTS = {
    "H1_market_state": "market_state_forward_capture",
    "market_sector": "market_sector_route_forward_capture",
    "shadow_decision": "market_sector_route_shadow_decision",
    "sector_constituents": "market_sector_constituent_forward_capture",
    "intraday_emotion": "intraday_emotion_forward_capture",
}
_ADAPTER_INPUTS = (*_CAPTURE_INPUTS, "account_snapshot")


def _authority() -> dict[str, bool]:
    return {
        "research_only": True,
        "changes_live_score": False,
        "changes_live_trade_count": False,
        "changes_live_position_or_quantity": False,
        "generates_buy_or_sell_alert": False,
        "connects_broker": False,
        "executes_orders": False,
        "computes_performance": False,
        "historical_return_run": False,
        "production_strategy": False,
        "automatic_trading": False,
        "stable_profit_claim": False,
    }


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _identity(value: dict[str, Any], field: str) -> str:
    payload = copy.deepcopy(value)
    payload.pop(field, None)
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(SH_TZ)


def _unavailable_slot(reason: str) -> dict[str, Any]:
    return {
        "requires_new_native_same_slot_inputs": True,
        "prior_day_inputs_may_not_be_reused": True,
        "available": False,
        "reason": reason,
        "not_before": None,
        "capture_until": None,
    }


def _strict_windows() -> tuple[tuple[str, time, time], ...] | None:
    try:
        raw = MARKET_SECTOR_PROTOCOL_PATH.read_bytes()
        if hashlib.sha256(raw).hexdigest() != MARKET_SECTOR_PROTOCOL_SHA256:
            return None
        document = json.loads(raw)
        schedule = document.get("schedule") or {}
        if (
            document.get("status") != "frozen_capture_only_not_a_strategy"
            or schedule.get("timezone") != "Asia/Shanghai"
        ):
            return None
        windows = tuple(
            (
                str(row["id"]),
                time.fromisoformat(str(row["capture_from"])),
                time.fromisoformat(str(row["capture_until"])),
            )
            for row in schedule.get("slots") or []
        )
        if (
            not windows
            or any(start > end for _, start, end in windows)
            or any(windows[index - 1][2] >= windows[index][1] for index in range(1, len(windows)))
        ):
            return None
        return windows
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _next_real_slot_boundary(now: datetime | None = None) -> dict[str, Any]:
    if now is not None and now.tzinfo is None:
        return _unavailable_slot("explicit_time_requires_timezone")
    current = now or datetime.now(SH_TZ)
    current = current.astimezone(SH_TZ).replace(microsecond=0)
    windows = _strict_windows()
    if windows is None:
        return _unavailable_slot("strict_capture_schedule_identity_invalid")
    try:
        protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
        forward_start = date.fromisoformat(str(protocol["forward_start"]))
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return _unavailable_slot("forward_start_unavailable")
    day = current.date()
    if day < forward_start:
        slot_day = next_trading_day_after(forward_start - timedelta(days=1))
        slot_id, start, end = windows[0]
    elif trading_day_status(day).get("is_trading_day") is True:
        current_time = current.timetz().replace(tzinfo=None)
        for slot_id, start, end in windows:
            if current_time <= end:
                slot_day = day
                break
        else:
            slot_day = next_trading_day_after(day)
            slot_id, start, end = windows[0]
    else:
        slot_day = next_trading_day_after(day)
        slot_id, start, end = windows[0]
    base = {
        "requires_new_native_same_slot_inputs": True,
        "prior_day_inputs_may_not_be_reused": True,
    }
    if slot_day is None:
        return _unavailable_slot("audited_trading_calendar_has_no_later_session")
    return {
        **base,
        "available": True,
        "slot_id": slot_id,
        "not_before": datetime.combine(slot_day, start, SH_TZ).isoformat(),
        "capture_until": datetime.combine(slot_day, end, SH_TZ).isoformat(),
    }


def _protocol_errors() -> list[str]:
    try:
        raw = PROTOCOL_PATH.read_bytes()
        protocol = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [f"persistence_protocol_unreadable:{type(exc).__name__}"]
    errors: list[str] = []
    if hashlib.sha256(raw).hexdigest() != PROTOCOL_SHA256:
        errors.append("persistence_protocol_sha256_mismatch")
    if protocol.get("status") != (
        "calendar_v15_audited_future_session_frozen"
    ):
        errors.append("persistence_protocol_status_invalid")
    successor_by_path: dict[str, dict[str, Any]] = {}
    runtime = verify_system_route_joint_v2_runtime()
    if runtime.get("valid") is True:
        try:
            successor = json.loads(ADMISSION_PATH.read_text(encoding="utf-8"))
            successor_by_path = {
                str(binding.get("path") or ""): binding
                for binding in (successor.get("bindings") or {}).values()
                if isinstance(binding, dict)
            }
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            successor_by_path = {}
    bindings = {
        "predecessor": protocol.get("predecessor") or {},
        **(protocol.get("bindings") or {}),
    }
    for name, binding in bindings.items():
        try:
            path = research_artifact_path(binding.get("path"), project_root=PROJECT_ROOT)
        except ValueError:
            errors.append(f"persistence_binding_outside_project:{name}")
            continue
        current_sha = _sha(path) if path.is_file() else None
        successor_binding = successor_by_path.get(
            str(binding.get("path") or "")
        )
        successor_rebinds_current_bytes = bool(
            current_sha is not None
            and successor_binding
            and successor_binding.get("sha256") == current_sha
        )
        if (
            current_sha is None
            or (
                current_sha != binding.get("sha256")
                and not successor_rebinds_current_bytes
            )
        ):
            errors.append(f"persistence_binding_invalid:{name}")
    return errors


def _calendar_errors(decision_day: object) -> list[str]:
    try:
        protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
        day = date.fromisoformat(str(decision_day or ""))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return ["decision_day_or_protocol_invalid"]
    if day.isoformat() < str(protocol.get("forward_start") or ""):
        return ["decision_day_before_forward_start"]
    calendar = trading_day_status(day)
    if (
        calendar.get("source") != "audited_baostock_calendar"
        or calendar.get("date") != day.isoformat()
        or calendar.get("is_trading_day") is not True
    ):
        return ["decision_day_not_in_audited_trading_calendar"]
    return []


def _runtime_errors() -> tuple[dict[str, Any], list[str]]:
    runtime = verify_system_route_joint_v2_runtime()
    errors = list(runtime.get("errors") or [])
    if runtime.get("valid") is not True:
        errors.insert(0, "successor_runtime_admission_invalid")
    return runtime, list(dict.fromkeys(errors))


def _output_base(
    value: Path, *, project_root: Path,
) -> Path:
    return research_artifact_path(value, project_root=project_root)


def _capture_paths(
    captures: dict[str, Any], *, project_root: Path,
) -> tuple[dict[str, Path], list[str]]:
    paths: dict[str, Path] = {}
    errors: list[str] = []
    for input_name, capture_name in _CAPTURE_INPUTS.items():
        capture = captures.get(capture_name)
        if not isinstance(capture, dict) or capture.get("captured") is not True:
            errors.append(f"required_capture_missing:{input_name}")
            continue
        try:
            path = research_artifact_path(capture.get("path"), project_root=project_root)
        except ValueError:
            errors.append(f"required_capture_path_invalid:{input_name}")
            continue
        if not path.is_file():
            errors.append(f"required_capture_file_missing:{input_name}")
            continue
        paths[input_name] = path
    return paths, errors


def verify_system_route_joint_v2_forward_packet(
    path: Path, *, project_root: Path = PROJECT_ROOT,
    decision_dir: Path | None = None,
) -> dict[str, Any]:
    errors = _protocol_errors()
    runtime, runtime_errors = _runtime_errors()
    errors.extend(runtime_errors)
    try:
        resolved = research_artifact_path(path, project_root=project_root)
        packet = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"forward_packet_unreadable:{exc}"]}
    if not isinstance(packet, dict):
        return {"valid": False, "errors": ["forward_packet_object_required"]}
    if packet.get("schema") != "system_route_joint_v2_forward_packet.v2":
        errors.append("forward_packet_schema_invalid")
    if packet.get("packet_id") != _identity(packet, "packet_id"):
        errors.append("forward_packet_id_invalid")
    if packet.get("persistence_protocol") != {
        "path": str(PROTOCOL_PATH.relative_to(PROJECT_ROOT)),
        "sha256": PROTOCOL_SHA256,
    }:
        errors.append("forward_packet_protocol_identity_invalid")
    if packet.get("adapter_protocol") != {
        "path": str(ADAPTER_PROTOCOL_PATH.relative_to(PROJECT_ROOT)),
        "sha256": ADAPTER_PROTOCOL_SHA256,
    }:
        errors.append("forward_packet_adapter_protocol_invalid")
    if packet.get("runtime_admission") != {
        "admission_id": runtime.get("admission_id"),
        "admission_sha256": runtime.get("admission_sha256"),
    }:
        errors.append("forward_packet_runtime_admission_invalid")
    if (
        packet.get("no_backfill") is not True
        or packet.get("returns_or_labels_read") is not False
        or packet.get("execution_performed") is not False
        or packet.get("performance_computed") is not False
        or packet.get("authority") != _authority()
    ):
        errors.append("forward_packet_boundary_invalid")
    decision_at = _time(packet.get("decision_at"))
    captured_at = _time(packet.get("captured_at"))
    first_seen_at = _time(packet.get("first_seen_at"))
    errors.extend(_calendar_errors(packet.get("decision_day")))
    expected_dir: Path | None = None
    try:
        if project_root.resolve() == PROJECT_ROOT.resolve():
            expected_dir = _output_base(DECISION_DIR, project_root=project_root)
        elif decision_dir is not None:
            expected_dir = _output_base(
                decision_dir, project_root=project_root.resolve(),
            )
    except ValueError:
        errors.append("forward_packet_decision_directory_invalid")
    if (
        decision_at is None or captured_at is None or first_seen_at is None
        or captured_at != first_seen_at or decision_at > captured_at
        or decision_at.date().isoformat() != packet.get("decision_day")
        or capture_slot(decision_at) != packet.get("slot_id")
        or capture_slot(captured_at) != packet.get("slot_id")
        or resolved.name
        != f"{packet.get('decision_day')}-{packet.get('slot_id')}.json"
    ):
        errors.append("forward_packet_time_or_path_identity_invalid")
    if expected_dir is not None and resolved.parent != expected_dir:
        errors.append("forward_packet_not_in_canonical_decision_directory")
    adapter_result = packet.get("adapter_result")
    if not isinstance(adapter_result, dict) or adapter_result.get("valid") is not True:
        errors.append("forward_packet_adapter_result_missing_or_invalid")
    else:
        refs = adapter_result.get("input_refs") or {}
        input_paths = {
            name: str((refs.get(name) or {}).get("path") or "")
            for name in _ADAPTER_INPUTS
        }
        try:
            reproduced = adapt_system_route_forward_packets(
                input_paths, project_root=project_root,
            )
        except (OSError, TypeError, ValueError) as exc:
            errors.append(f"forward_packet_reproduction_failed:{type(exc).__name__}")
        else:
            if reproduced != adapter_result:
                errors.append("forward_packet_adapter_reproduction_mismatch")
            if packet.get("decision_at") != adapter_result.get("decision_at"):
                errors.append("forward_packet_decision_clock_mismatch")
    return {
        "valid": not errors,
        "errors": list(dict.fromkeys(errors)),
        "packet": packet,
        "packet_id": packet.get("packet_id"),
    }


def capture_system_route_joint_v2_forward(
    captures: dict[str, Any], account_snapshot: dict[str, Any], *,
    now: datetime | None = None, project_root: Path = PROJECT_ROOT,
    decision_dir: Path = DECISION_DIR, account_dir: Path = ACCOUNT_DIR,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Persist one verified same-slot research decision; never execute it."""
    root = project_root.resolve()
    live_root = root == PROJECT_ROOT.resolve()
    try:
        decision_base = _output_base(decision_dir, project_root=root)
        account_base = _output_base(account_dir, project_root=root)
    except ValueError:
        return {"status": "output_path_outside_project", "captured": False}
    if live_root and (
        now is not None
        or clock is not None
        or decision_base != _output_base(DECISION_DIR, project_root=root)
        or account_base != _output_base(ACCOUNT_DIR, project_root=root)
    ):
        return {"status": "live_clock_or_output_override_forbidden", "captured": False}
    observed = (now or (clock() if clock else datetime.now(SH_TZ))).astimezone(SH_TZ).replace(
        microsecond=0,
    )
    slot_id = capture_slot(observed)
    if slot_id is None:
        return {"status": "outside_original_capture_slot", "captured": False}
    protocol_errors = _protocol_errors()
    if protocol_errors:
        return {
            "status": "identity_invalid", "captured": False,
            "errors": protocol_errors,
        }
    decision_day = observed.date().isoformat()
    calendar_errors = _calendar_errors(decision_day)
    if calendar_errors:
        return {
            "status": "calendar_or_forward_start_invalid", "captured": False,
            "errors": calendar_errors, "research_only": True,
        }
    runtime, runtime_errors = _runtime_errors()
    if runtime_errors:
        return {
            "status": "runtime_admission_invalid", "captured": False,
            "errors": runtime_errors, "research_only": True,
        }
    output = decision_base / f"{decision_day}-{slot_id}.json"
    if output.exists():
        checked = verify_system_route_joint_v2_forward_packet(
            output, project_root=root, decision_dir=decision_base,
        )
        return {
            "status": (
                "already_captured" if checked.get("valid") is True
                else "existing_packet_invalid"
            ),
            "captured": checked.get("valid") is True,
            "valid": checked.get("valid") is True,
            "path": str(output),
            "packet_id": checked.get("packet_id"),
            "errors": checked.get("errors") or [],
            "research_only": True,
        }
    input_paths, capture_errors = _capture_paths(captures, project_root=root)
    if capture_errors:
        return {
            "status": "required_capture_incomplete", "captured": False,
            "errors": capture_errors, "research_only": True,
        }
    try:
        account_packet = build_system_route_account_packet_v2(
            account_snapshot, observed_at=observed,
        )
        account_payload = _canonical_bytes(account_packet)
        account_path = account_base / f"{account_packet['packet_id']}.json"
        _atomic_publish(account_path, account_payload)
        account_checked = verify_system_route_account_packet_v2(
            account_path, project_root=root,
        )
        if account_checked.get("valid") is not True:
            raise ValueError("account_snapshot_post_write_invalid")
    except (OSError, SnapshotConflict, TypeError, ValueError) as exc:
        return {
            "status": "account_snapshot_invalid", "captured": False,
            "errors": [str(exc)], "research_only": True,
        }
    input_paths["account_snapshot"] = account_path
    adapted = adapt_system_route_forward_packets(
        input_paths, project_root=root,
    )
    if adapted.get("valid") is not True:
        return {
            "status": "adapter_blocked", "captured": False,
            "errors": adapted.get("errors") or ["adapter_result_invalid"],
            "decision_at": adapted.get("decision_at"),
            "research_only": True,
        }
    decision_at = _time(adapted.get("decision_at"))
    if (
        decision_at is None or decision_at > observed
        or decision_at.date().isoformat() != decision_day
        or capture_slot(decision_at) != slot_id
    ):
        return {
            "status": "decision_clock_invalid", "captured": False,
            "errors": ["adapter_decision_time_not_available_in_capture_slot"],
            "research_only": True,
        }
    committed = (
        observed if now is not None
        else (clock() if clock else datetime.now(SH_TZ)).astimezone(SH_TZ).replace(
            microsecond=0,
        )
    )
    if (
        committed.date().isoformat() != decision_day
        or capture_slot(committed) != slot_id
        or committed < observed
    ):
        return {
            "status": "capture_finished_outside_original_slot",
            "captured": False,
            "errors": ["final_precommit_clock_outside_original_slot"],
            "research_only": True,
        }
    packet = {
        "schema": "system_route_joint_v2_forward_packet.v2",
        "decision_day": decision_day,
        "slot_id": slot_id,
        "decision_at": decision_at.isoformat(timespec="seconds"),
        "captured_at": committed.isoformat(timespec="seconds"),
        "first_seen_at": committed.isoformat(timespec="seconds"),
        "no_backfill": True,
        "persistence_protocol": {
            "path": str(PROTOCOL_PATH.relative_to(PROJECT_ROOT)),
            "sha256": PROTOCOL_SHA256,
        },
        "adapter_protocol": {
            "path": str(ADAPTER_PROTOCOL_PATH.relative_to(PROJECT_ROOT)),
            "sha256": ADAPTER_PROTOCOL_SHA256,
        },
        "runtime_admission": {
            "admission_id": runtime.get("admission_id"),
            "admission_sha256": runtime.get("admission_sha256"),
        },
        "adapter_result": adapted,
        "returns_or_labels_read": False,
        "execution_performed": False,
        "performance_computed": False,
        "authority": _authority(),
    }
    packet["packet_id"] = _identity(packet, "packet_id")
    try:
        _atomic_publish(output, _canonical_bytes(packet))
        checked = verify_system_route_joint_v2_forward_packet(
            output, project_root=root, decision_dir=decision_base,
        )
        if checked.get("valid") is not True:
            raise ValueError(
                "forward_packet_post_write_invalid:"
                + ",".join(checked.get("errors") or [])
            )
    except (OSError, SnapshotConflict, TypeError, ValueError) as exc:
        return {
            "status": "forward_packet_publish_invalid", "captured": False,
            "valid": False, "errors": [str(exc)], "research_only": True,
        }
    return {
        "status": "captured", "captured": True, "valid": True,
        "path": str(output), "packet_id": packet["packet_id"],
        "decision_at": packet["decision_at"],
        "decision_status": (adapted.get("decision") or {}).get("status"),
        "research_only": True,
    }


def system_route_joint_v2_forward_status(
    *, project_root: Path = PROJECT_ROOT, decision_dir: Path = DECISION_DIR,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a lightweight truthful status and verify only the latest packet."""
    try:
        base = _output_base(decision_dir, project_root=project_root.resolve())
    except ValueError:
        return {"status": "output_path_outside_project", "packet_count": 0}
    runtime, runtime_errors = _runtime_errors()
    try:
        protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        protocol = {}
    paths = sorted(base.glob("*.json")) if base.is_dir() else []
    verified: list[tuple[datetime, datetime, Path, dict[str, Any]]] = []
    invalid_count = 0
    for path in paths:
        checked = verify_system_route_joint_v2_forward_packet(
            path, project_root=project_root, decision_dir=base,
        )
        packet = checked.get("packet") or {}
        decision_at = _time(packet.get("decision_at"))
        captured_at = _time(packet.get("captured_at"))
        if (
            checked.get("valid") is True
            and decision_at is not None and captured_at is not None
        ):
            verified.append((decision_at, captured_at, path, checked))
        else:
            invalid_count += 1
    latest = None
    if verified:
        _decision_at, _captured_at, latest_path, checked = max(
            verified, key=lambda row: (row[0], row[1]),
        )
        packet = checked.get("packet") or {}
        decision = (packet.get("adapter_result") or {}).get("decision") or {}
        route = decision.get("route") or {}
        market = decision.get("market") or {}
        sector = decision.get("sector") or {}
        budget = decision.get("market_budget") or {}
        account_plan = decision.get("account_plan") or {}
        candidates = decision.get("candidates") or {}
        latest = {
            "valid": checked.get("valid") is True,
            "errors": checked.get("errors") or [],
            "path": str(latest_path),
            "packet_id": packet.get("packet_id"),
            "decision_day": packet.get("decision_day"),
            "slot_id": packet.get("slot_id"),
            "decision_at": packet.get("decision_at"),
            "decision_status": decision.get("status"),
            "market_state": route.get("market_state") or market.get("state"),
            "market_state_as_of": route.get("market_state_as_of") or market.get("as_of"),
            "market_confidence": route.get("market_confidence") or market.get("confidence"),
            "market_confidence_semantics": (
                route.get("market_confidence_semantics")
                or market.get("confidence_semantics")
            ),
            "evidence": list(route.get("evidence") or market.get("evidence") or []),
            "contradictions": list(
                route.get("contradictions") or market.get("contradictions") or []
            ),
            "market_budget": budget,
            "target_exposure_pct": budget.get("target_exposure_pct"),
            "maximum_new_trades": budget.get("maximum_new_trades"),
            "eligible_playbooks": list(budget.get("eligible_playbooks") or []),
            "intraday_confirmation": budget.get("intraday_confirmation"),
            "confirmed_sectors": list(sector.get("confirmed_sectors") or []),
            "candidate_count": candidates.get("selected_count"),
            "selected_candidates": list(candidates.get("selected") or []),
            "account_snapshot_sha256": decision.get(
                "account_snapshot_sha256"
            ),
            "account_plan_status": account_plan.get("status"),
            "existing_exposure_pct": account_plan.get("existing_exposure_pct"),
            "new_buy_capacity_yuan": account_plan.get("new_buy_capacity_yuan"),
            "execution_applied": False,
            "research_only": True,
        }
    return {
        "status": (
            "runtime_admission_invalid" if runtime_errors
            else "capturing_strict_forward" if latest and latest.get("valid")
            else "awaiting_first_real_packet" if not paths
            else "no_verified_packet"
        ),
        "packet_count": len(verified),
        "observed_file_count": len(paths),
        "verified_packet_count": len(verified),
        "invalid_packet_count": invalid_count,
        "latest": latest,
        "runtime_admission": {
            "valid": not runtime_errors,
            "admission_id": runtime.get("admission_id"),
            "admission_sha256": runtime.get("admission_sha256"),
            "errors": runtime_errors,
        },
        "morning_incident": protocol.get("morning_incident") or {},
        "next_real_slot_boundary": _next_real_slot_boundary(now),
        "strict_forward_only": True,
        "historical_pit_ready": False,
        "performance_ready": False,
        "production_strategy": False,
    }


__all__ = [
    "PROTOCOL_PATH", "PROTOCOL_SHA256",
    "capture_system_route_joint_v2_forward",
    "system_route_joint_v2_forward_status",
    "verify_system_route_joint_v2_forward_packet",
]
