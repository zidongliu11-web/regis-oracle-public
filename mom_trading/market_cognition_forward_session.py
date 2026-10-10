"""Assemble strict-forward replay sessions from immutable evidence refs only."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from .market_state_daily import PROJECT_ROOT, _atomic_publish, _canonical_bytes, _sha256
from .strict_joint_forward_adapter import verify_strict_joint_forward_audit


SESSION_DIR = Path("research/data/market_cognition_system_replay/v1/strict_forward_sessions")
INPUT_DIR = Path("research/data/market_cognition_system_replay/v1/strict_forward_inputs")
_ELIGIBLE_AUDIT_STATUSES = {"audit_complete", "expected_no_trade_zero_signal"}


def _identity(value: dict[str, Any], field: str) -> str:
    body = copy.deepcopy(value)
    body.pop(field, None)
    return hashlib.sha256(_canonical_bytes(body)).hexdigest()


def _authority() -> dict[str, bool]:
    return {
        "research_only": True,
        "changes_live_score": False,
        "changes_live_trade_count": False,
        "changes_live_position_or_quantity": False,
        "generates_buy_or_sell_alert": False,
        "updates_verified_paper_state": False,
        "production_strategy": False,
        "automatic_trading": False,
        "stable_profit_claim": False,
    }


def _project_path(value: object, project_root: Path) -> Path:
    raw = Path(str(value or ""))
    resolved = (raw if raw.is_absolute() else project_root / raw).resolve()
    resolved.relative_to(project_root.resolve())
    return resolved


def _ref(path: Path, project_root: Path) -> dict[str, str]:
    resolved = path.resolve()
    resolved.relative_to(project_root.resolve())
    return {
        "path": str(resolved.relative_to(project_root.resolve())),
        "sha256": _sha256(resolved),
    }


def _load_ref(
    value: object, *, project_root: Path, label: str,
) -> tuple[dict[str, Any], dict[str, str], Path]:
    row = value if isinstance(value, dict) else {}
    try:
        path = _project_path(row.get("path"), project_root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label}:path_invalid") from exc
    expected = str(row.get("sha256") or "")
    if not path.is_file() or not expected or _sha256(path) != expected:
        raise ValueError(f"{label}:sha256_invalid")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}:json_invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label}:object_required")
    return payload, _ref(path, project_root), path


def _time(value: object, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label}:time_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label}:timezone_required")
    return parsed


def _finite_price(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}:price_invalid") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label}:price_invalid")
    return number


def _verify_protocol(
    *, project_root: Path, protocol_path: Path, expected_sha256: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    try:
        resolved = protocol_path.resolve()
        resolved.relative_to(project_root.resolve())
        actual_sha = _sha256(resolved)
        protocol = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("session_protocol_unreadable") from exc
    if actual_sha != expected_sha256:
        raise ValueError("session_protocol_sha256_mismatch")
    if (
        not isinstance(protocol, dict)
        or protocol.get("status") != "frozen_before_first_strict_forward_session"
        or protocol.get("no_backfill") is not True
        or protocol.get("allowed_mode") != "strict_forward"
        or protocol.get("historical_PIT_admitted") is not False
        or protocol.get("synthetic_uses_forward_assembler") is not False
    ):
        raise ValueError("session_protocol_boundary_invalid")
    authority = protocol.get("authority") or {}
    if authority.get("research_only") is not True or any(
        value is not False for key, value in authority.items()
        if key != "research_only"
    ):
        raise ValueError("session_protocol_authority_invalid")
    return protocol, _ref(resolved, project_root)


def _mode_error(mode: str) -> str | None:
    if mode == "strict_forward":
        return None
    if mode == "historical_PIT":
        return "historical_PIT_not_admitted"
    if mode == "synthetic_contract":
        return "synthetic_contract_bypasses_forward_assembler"
    return "mode_not_supported"


def _packet_identity(value: dict[str, Any], *, label: str) -> None:
    if value.get("packet_id") != _identity(value, "packet_id"):
        raise ValueError(f"{label}:packet_id_invalid")


def _verified_execution_truth(
    audit: dict[str, Any], *, project_root: Path,
) -> tuple[dict[str, Any], dict[str, str], str, str]:
    execution, execution_ref, _path = _load_ref(
        audit.get("source_execution"), project_root=project_root,
        label="verified_execution",
    )
    if (
        execution.get("schema") != "unified_regime_route_execution_day.v1"
        or execution.get("no_backfill") is not True
    ):
        raise ValueError("verified_execution:contract_invalid")
    _packet_identity(execution, label="verified_execution")
    state = execution.get("state_after")
    if not isinstance(state, dict) or state.get("state_id") != _identity(state, "state_id"):
        raise ValueError("verified_execution:state_identity_invalid")
    trade_date = str(execution.get("trade_date") or "")
    if not trade_date or state.get("last_trade_date") != trade_date:
        raise ValueError("verified_execution:trade_date_invalid")
    return execution, execution_ref, str(state["state_id"]), trade_date


def _support_packet(
    ref_value: object, *, project_root: Path, label: str, schema: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    packet, normalized_ref, _path = _load_ref(
        ref_value, project_root=project_root, label=label,
    )
    if packet.get("schema") != schema or packet.get("no_backfill") is not True:
        raise ValueError(f"{label}:contract_invalid")
    _packet_identity(packet, label=label)
    return packet, normalized_ref


def _validate_mark(
    mark: dict[str, Any], *, audit: dict[str, Any], execution: dict[str, Any],
    execution_ref: dict[str, str], state_id: str, trade_date: str,
    project_root: Path,
) -> dict[str, dict[str, str]]:
    if (
        mark.get("schema") != "market_cognition_after_session_valuation.v1"
        or mark.get("no_backfill") is not True
        or mark.get("completed_session") is not True
        or mark.get("outcome_role") != "post_decision_valuation_only"
        or mark.get("price_semantics")
        != "unadjusted_completed_session_close_with_explicit_corporate_actions"
    ):
        raise ValueError("after_session_mark:contract_invalid")
    _packet_identity(mark, label="after_session_mark")
    strict_result = audit.get("strict_result") or {}
    if mark.get("strict_result_id") != strict_result.get("result_id"):
        raise ValueError("after_session_mark:strict_result_chain_mismatch")
    if mark.get("verified_execution_ref") != execution_ref:
        raise ValueError("after_session_mark:execution_ref_mismatch")
    if mark.get("state_id") != state_id or mark.get("trade_date") != trade_date:
        raise ValueError("after_session_mark:portfolio_state_chain_mismatch")

    raw, raw_ref = _support_packet(
        mark.get("price_source_ref"), project_root=project_root,
        label="price_source",
        schema="market_cognition_completed_session_prices.v1",
    )
    calendar, calendar_ref = _support_packet(
        mark.get("calendar_ref"), project_root=project_root,
        label="calendar",
        schema="market_cognition_trading_calendar_session.v1",
    )
    actions, action_ref = _support_packet(
        mark.get("corporate_action_ref"), project_root=project_root,
        label="corporate_action",
        schema="market_cognition_corporate_action_snapshot.v1",
    )
    if mark.get("price_source_ref") != raw_ref:
        raise ValueError("after_session_mark:price_source_ref_not_canonical")
    if mark.get("calendar_ref") != calendar_ref:
        raise ValueError("after_session_mark:calendar_ref_not_canonical")
    if mark.get("corporate_action_ref") != action_ref:
        raise ValueError("after_session_mark:corporate_action_ref_not_canonical")

    if (
        raw.get("trade_date") != trade_date
        or raw.get("completed_session") is not True
        or calendar.get("trade_date") != trade_date
        or calendar.get("is_trading_day") is not True
        or calendar.get("session_completed") is not True
        or actions.get("trade_date") != trade_date
        or not isinstance(actions.get("actions"), list)
    ):
        raise ValueError("after_session_mark:supporting_session_identity_invalid")

    source_as_of = _time(mark.get("source_as_of"), "after_session_mark:source_as_of")
    first_seen = _time(mark.get("first_seen_at"), "after_session_mark:first_seen_at")
    marked_at = _time(mark.get("marked_at"), "after_session_mark:marked_at")
    raw_source = _time(raw.get("source_as_of"), "price_source:source_as_of")
    raw_seen = _time(raw.get("first_seen_at"), "price_source:first_seen_at")
    session_close = _time(calendar.get("session_close_at"), "calendar:session_close_at")
    calendar_seen = _time(calendar.get("first_seen_at"), "calendar:first_seen_at")
    action_source = _time(actions.get("source_as_of"), "corporate_action:source_as_of")
    action_seen = _time(actions.get("first_seen_at"), "corporate_action:first_seen_at")
    execution_at = _time(
        (((audit.get("strict_input_bundle") or {}).get("packets") or {})
         .get("A_share_execution_cost") or {}).get("source_as_of"),
        "verified_execution:execution_at",
    )
    if not (
        execution_at < session_close <= source_as_of == raw_source
        <= first_seen == raw_seen <= marked_at
    ):
        raise ValueError("after_session_mark:price_chronology_invalid")
    if calendar_seen > marked_at or action_source > action_seen or action_seen > marked_at:
        raise ValueError("after_session_mark:support_chronology_invalid")
    if any(value.date().isoformat() != trade_date for value in (
        source_as_of, first_seen, marked_at, session_close,
    )):
        raise ValueError("after_session_mark:not_execution_trade_date")

    mark_prices = mark.get("prices_yuan")
    raw_prices = raw.get("prices_yuan")
    positions = (execution.get("state_after") or {}).get("positions")
    if not isinstance(mark_prices, dict) or not isinstance(raw_prices, dict) or not isinstance(positions, dict):
        raise ValueError("after_session_mark:prices_or_positions_invalid")
    if set(mark_prices) != set(positions):
        raise ValueError("after_session_mark:position_price_coverage_invalid")
    for code, marked in mark_prices.items():
        marked_price = _finite_price(marked, f"after_session_mark:{code}")
        raw_price = _finite_price(raw_prices.get(code), f"price_source:{code}")
        if marked_price != raw_price:
            raise ValueError(f"after_session_mark:price_source_mismatch:{code}")
    return {
        "price_source_ref": raw_ref,
        "calendar_ref": calendar_ref,
        "corporate_action_ref": action_ref,
    }


def _publish_once(path: Path, value: dict[str, Any]) -> bool:
    payload = _canonical_bytes(value)
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError("first_write_conflict")
        return False
    _atomic_publish(path, payload)
    return True


def verify_strict_forward_session(
    path: Path, *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        resolved = path.resolve()
        resolved.relative_to(project_root.resolve())
        session = json.loads(resolved.read_text(encoding="utf-8"))
        if not isinstance(session, dict):
            raise ValueError("session_object_required")
        if session.get("schema") != "market_cognition_system_replay_session.v1":
            raise ValueError("session_schema_invalid")
        if session.get("mode") != "strict_forward" or session.get("no_backfill") is not True:
            raise ValueError("session_mode_or_backfill_invalid")
        if session.get("session_id") != _identity(session, "session_id"):
            raise ValueError("session_id_invalid")
        if session.get("returns_or_labels_read") is not False or session.get("performance_computed") is not False:
            raise ValueError("session_performance_boundary_invalid")
        if session.get("authority") != _authority():
            raise ValueError("session_authority_invalid")
        protocol, protocol_ref, protocol_path = _load_ref(
            session.get("assembly_protocol"), project_root=project_root,
            label="assembly_protocol",
        )
        _verify_protocol(
            project_root=project_root, protocol_path=protocol_path,
            expected_sha256=protocol_ref["sha256"],
        )
        audit, audit_ref, audit_path = _load_ref(
            session.get("strict_audit_ref"), project_root=project_root,
            label="strict_audit",
        )
        checked = verify_strict_joint_forward_audit(
            audit_path, project_root=project_root,
        )
        if checked.get("valid") is not True or audit.get("status") not in _ELIGIBLE_AUDIT_STATUSES:
            raise ValueError("strict_audit_not_session_eligible")
        execution, execution_ref, state_id, trade_date = _verified_execution_truth(
            audit, project_root=project_root,
        )
        mark, mark_ref, _mark_path = _load_ref(
            session.get("after_session_mark_ref"), project_root=project_root,
            label="after_session_mark",
        )
        evidence = _validate_mark(
            mark, audit=audit, execution=execution, execution_ref=execution_ref,
            state_id=state_id, trade_date=trade_date, project_root=project_root,
        )
        if session.get("assembly_protocol") != protocol_ref:
            raise ValueError("assembly_protocol_ref_mismatch")
        if session.get("strict_audit_ref") != audit_ref:
            raise ValueError("strict_audit_ref_mismatch")
        if session.get("after_session_mark_ref") != mark_ref:
            raise ValueError("after_session_mark_ref_mismatch")
        if session.get("verified_execution_ref") != execution_ref:
            raise ValueError("verified_execution_ref_mismatch")
        truth = session.get("verified_portfolio_state_truth") or {}
        if truth != {
            "role": "portfolio_state_truth",
            "source": "verified_execution",
            "execution_ref": execution_ref,
            "state_id": state_id,
            "trade_date": trade_date,
        }:
            raise ValueError("verified_portfolio_state_truth_invalid")
        if session.get("strict_result_role") != "economic_consistency_audit_only":
            raise ValueError("strict_result_role_invalid")
        bundle = audit.get("strict_input_bundle")
        result = audit.get("strict_result")
        if session.get("strict_bundle") != bundle or session.get("strict_result") != result:
            raise ValueError("strict_audit_inline_identity_mismatch")
        if session.get("strict_bundle_sha256") != hashlib.sha256(
            _canonical_bytes(bundle)
        ).hexdigest():
            raise ValueError("strict_bundle_sha256_invalid")
        if session.get("after_session_mark") != mark or session.get("mark_evidence") != evidence:
            raise ValueError("after_session_mark_inline_identity_mismatch")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(str(exc))
        session = None
    return {
        "valid": not errors, "errors": errors,
        "session": session, "session_id": session.get("session_id") if session else None,
    }


def assemble_strict_forward_session(
    audit_ref: dict[str, Any], after_session_mark_ref: dict[str, Any], *,
    project_root: Path = PROJECT_ROOT, protocol_path: Path,
    expected_protocol_sha256: str, session_dir: Path = SESSION_DIR,
    mode: str = "strict_forward",
) -> dict[str, Any]:
    error = _mode_error(mode)
    if error:
        return {"status": "fail_closed", "errors": [error], "authority": _authority()}
    try:
        _protocol, protocol_ref = _verify_protocol(
            project_root=project_root, protocol_path=protocol_path,
            expected_sha256=expected_protocol_sha256,
        )
        audit, normalized_audit_ref, audit_path = _load_ref(
            audit_ref, project_root=project_root, label="strict_audit",
        )
        checked = verify_strict_joint_forward_audit(
            audit_path, project_root=project_root,
        )
        if checked.get("valid") is not True:
            raise ValueError("strict_audit_invalid:" + ",".join(checked.get("errors") or []))
        if audit.get("status") not in _ELIGIBLE_AUDIT_STATUSES:
            raise ValueError("strict_audit_not_session_eligible")
        result_status = (audit.get("strict_result") or {}).get("status")
        expected_result = (
            "replay_complete" if audit.get("status") == "audit_complete"
            else "zero_signal"
        )
        if result_status != expected_result:
            raise ValueError("strict_result_status_mismatch")
        execution, execution_ref, state_id, trade_date = _verified_execution_truth(
            audit, project_root=project_root,
        )
        mark, normalized_mark_ref, _mark_path = _load_ref(
            after_session_mark_ref, project_root=project_root,
            label="after_session_mark",
        )
        mark_evidence = _validate_mark(
            mark, audit=audit, execution=execution, execution_ref=execution_ref,
            state_id=state_id, trade_date=trade_date, project_root=project_root,
        )
        bundle = copy.deepcopy(audit["strict_input_bundle"])
        result = copy.deepcopy(audit["strict_result"])
        session = {
            "schema": "market_cognition_system_replay_session.v1",
            "mode": "strict_forward",
            "decision_day": audit["decision_day"],
            "slot_id": audit["slot_id"],
            "trade_date": trade_date,
            "no_backfill": True,
            "assembly_protocol": protocol_ref,
            "strict_audit_ref": normalized_audit_ref,
            "verified_execution_ref": execution_ref,
            "verified_portfolio_state_truth": {
                "role": "portfolio_state_truth",
                "source": "verified_execution",
                "execution_ref": execution_ref,
                "state_id": state_id,
                "trade_date": trade_date,
            },
            "strict_result_role": "economic_consistency_audit_only",
            "strict_bundle": bundle,
            "strict_bundle_sha256": hashlib.sha256(_canonical_bytes(bundle)).hexdigest(),
            "strict_result": result,
            "after_session_mark_ref": normalized_mark_ref,
            "after_session_mark": copy.deepcopy(mark),
            "mark_evidence": mark_evidence,
            "returns_or_labels_read": False,
            "performance_computed": False,
            "authority": _authority(),
        }
        session["session_id"] = _identity(session, "session_id")
        output = project_root / session_dir / (
            f"{session['decision_day']}-{session['slot_id']}-{session['session_id'][:16]}.json"
        )
        created = _publish_once(output, session)
        verified = verify_strict_forward_session(output, project_root=project_root)
        if verified.get("valid") is not True:
            raise RuntimeError("strict_forward_session_post_write_invalid")
        return {
            "status": "session_assembled" if created else "already_assembled",
            "session_ref": _ref(output, project_root),
            "session_id": session["session_id"],
            "decision_day": session["decision_day"],
            "trade_date": trade_date,
            "performance_computed": False,
            "authority": _authority(),
        }
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "fail_closed", "errors": [f"{type(exc).__name__}:{exc}"],
            "performance_computed": False, "authority": _authority(),
        }


def verify_strict_forward_input(
    path: Path, *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        resolved = path.resolve()
        resolved.relative_to(project_root.resolve())
        payload = json.loads(resolved.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("input_object_required")
        if (
            payload.get("schema") != "market_cognition_system_replay_input.v1"
            or payload.get("mode") != "strict_forward"
            or payload.get("no_backfill") is not True
        ):
            raise ValueError("input_contract_invalid")
        if payload.get("input_id") != _identity(payload, "input_id"):
            raise ValueError("input_id_invalid")
        if payload.get("returns_or_labels_read") is not False or payload.get("performance_computed") is not False:
            raise ValueError("input_performance_boundary_invalid")
        if payload.get("authority") != _authority():
            raise ValueError("input_authority_invalid")
        protocol, protocol_ref, protocol_path = _load_ref(
            payload.get("assembly_protocol"), project_root=project_root,
            label="assembly_protocol",
        )
        _verify_protocol(
            project_root=project_root, protocol_path=protocol_path,
            expected_sha256=protocol_ref["sha256"],
        )
        if payload.get("assembly_protocol") != protocol_ref:
            raise ValueError("input_protocol_ref_mismatch")
        refs = payload.get("session_refs")
        sessions = payload.get("sessions")
        if not isinstance(refs, list) or not refs or not isinstance(sessions, list) or len(refs) != len(sessions):
            raise ValueError("input_sessions_missing")
        loaded: list[dict[str, Any]] = []
        normalized_refs: list[dict[str, str]] = []
        for index, ref_value in enumerate(refs):
            session, normalized_ref, session_path = _load_ref(
                ref_value, project_root=project_root, label=f"session:{index}",
            )
            checked = verify_strict_forward_session(
                session_path, project_root=project_root,
            )
            if checked.get("valid") is not True:
                raise ValueError(f"session:{index}:invalid")
            if session.get("assembly_protocol") != protocol_ref:
                raise ValueError(f"session:{index}:protocol_mismatch")
            loaded.append(session)
            normalized_refs.append(normalized_ref)
        if payload.get("session_refs") != normalized_refs or payload.get("sessions") != loaded:
            raise ValueError("input_session_inline_identity_mismatch")
        chronology = [
            (str(row.get("decision_day") or ""), str(row.get("trade_date") or ""), str(row.get("session_id") or ""))
            for row in loaded
        ]
        if chronology != sorted(chronology) or len(set(chronology)) != len(chronology):
            raise ValueError("input_session_chronology_invalid")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(str(exc))
        payload = None
    return {
        "valid": not errors, "errors": errors,
        "input": payload, "input_id": payload.get("input_id") if payload else None,
    }


def assemble_strict_forward_input(
    session_refs: list[dict[str, Any]], *, project_root: Path = PROJECT_ROOT,
    protocol_path: Path, expected_protocol_sha256: str,
    input_dir: Path = INPUT_DIR, mode: str = "strict_forward",
) -> dict[str, Any]:
    error = _mode_error(mode)
    if error:
        return {"status": "fail_closed", "errors": [error], "authority": _authority()}
    try:
        _protocol, protocol_ref = _verify_protocol(
            project_root=project_root, protocol_path=protocol_path,
            expected_sha256=expected_protocol_sha256,
        )
        if not isinstance(session_refs, list) or not session_refs:
            raise ValueError("session_refs_missing")
        rows: list[tuple[tuple[str, str, str], dict[str, str], dict[str, Any]]] = []
        for index, value in enumerate(session_refs):
            session, normalized_ref, session_path = _load_ref(
                value, project_root=project_root, label=f"session:{index}",
            )
            checked = verify_strict_forward_session(
                session_path, project_root=project_root,
            )
            if checked.get("valid") is not True:
                raise ValueError(f"session:{index}:invalid")
            if session.get("assembly_protocol") != protocol_ref:
                raise ValueError(f"session:{index}:protocol_mismatch")
            key = (
                str(session.get("decision_day") or ""),
                str(session.get("trade_date") or ""),
                str(session.get("session_id") or ""),
            )
            rows.append((key, normalized_ref, session))
        rows.sort(key=lambda row: row[0])
        if len({row[0] for row in rows}) != len(rows):
            raise ValueError("duplicate_session_identity")
        payload = {
            "schema": "market_cognition_system_replay_input.v1",
            "mode": "strict_forward",
            "no_backfill": True,
            "assembly_protocol": protocol_ref,
            "session_refs": [row[1] for row in rows],
            "sessions": [copy.deepcopy(row[2]) for row in rows],
            "returns_or_labels_read": False,
            "performance_computed": False,
            "authority": _authority(),
        }
        payload["input_id"] = _identity(payload, "input_id")
        last_trade_date = rows[-1][0][1]
        output = project_root / input_dir / (
            f"through-{last_trade_date}-{payload['input_id'][:16]}.json"
        )
        created = _publish_once(output, payload)
        checked = verify_strict_forward_input(output, project_root=project_root)
        if checked.get("valid") is not True:
            raise RuntimeError("strict_forward_input_post_write_invalid")
        return {
            "status": "input_assembled" if created else "already_assembled",
            "input_ref": _ref(output, project_root),
            "input_id": payload["input_id"],
            "session_count": len(rows),
            "performance_computed": False,
            "authority": _authority(),
        }
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "fail_closed", "errors": [f"{type(exc).__name__}:{exc}"],
            "performance_computed": False, "authority": _authority(),
        }


__all__ = [
    "assemble_strict_forward_input", "assemble_strict_forward_session",
    "verify_strict_forward_input", "verify_strict_forward_session",
]
