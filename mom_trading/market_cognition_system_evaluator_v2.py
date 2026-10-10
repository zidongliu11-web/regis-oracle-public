"""Cost-after evaluation of formally assembled market-cognition sessions.

The evaluator is deliberately downstream-only: verified execution owns the
portfolio state, the strict replay remains an economic audit, and missing
sessions or independent scenarios are never inferred.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import date, datetime
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any

from .market_cognition_forward_session import (
    INPUT_DIR as FORWARD_INPUT_DIR,
    SESSION_DIR as FORWARD_SESSION_DIR,
    verify_strict_forward_input,
)
from .market_cognition_forward_completion_runtime import (
    EVIDENCE_DIR as FORWARD_COMPLETION_EVIDENCE_DIR,
    SESSION_CLOSE,
    SH_TZ,
    _verify_manifest as verify_forward_completion_manifest,
    _verify_source_evidence as verify_forward_completion_source_evidence,
)
from .market_state_daily import PROJECT_ROOT, _canonical_bytes, _sha256
from .strict_joint_forward_adapter import (
    PROTOCOL_SHA256 as STRICT_AUDIT_PROTOCOL_SHA256,
    _build_bundle as _rebuild_strict_bundle,
    _compare_execution as _compare_strict_execution,
    _verify_protocol as _verify_strict_audit_protocol,
    verify_strict_joint_forward_audit,
)
from .strict_joint_strategy_replay import run_strict_joint_replay
from .verified_portfolio_execution_forward import verify_forward_execution_packet


FORWARD_SESSION_PROTOCOL = Path(
    "research/MARKET_COGNITION_FORWARD_SESSION_PROTOCOL_V1.json"
)
FORWARD_SESSION_PROTOCOL_SHA256 = (
    "8fb761b0cac5a359428b5453aefc38844856e8251bb8725401ef9ee17ef34e46"
)
REPLAY_PROTOCOL = Path(
    "research/NONCONVENTIONAL_MARKET_COGNITION_REPLAY_PROTOCOL_V2.json"
)
REPLAY_PROTOCOL_SHA256 = (
    "90141b329c730f9e35e2809a7a84f383a3dbf94f6ba8cd283b3a39221eb28724"
)
EVALUATION_PROTOCOL = Path(
    "research/MARKET_COGNITION_SYSTEM_EVALUATION_PROTOCOL_V2_AMENDMENT_1.json"
)
EVALUATION_PROTOCOL_SHA256 = (
    "223c34675b9fa9ff4cdfe720af776c6813c91842f90a7d448eec09a2e8d82b6b"
)
STRICT_AUDIT_PROTOCOL = Path(
    "research/STRICT_JOINT_FORWARD_ADAPTER_PROTOCOL_V1_AMENDMENT_1.json"
)


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


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _day(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError:
        return None


def _timestamp(value: object, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label}:timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label}:timezone_required")
    return parsed.astimezone(SH_TZ)


def _project_path(value: object, project_root: Path) -> Path:
    raw = Path(str(value or ""))
    path = (raw if raw.is_absolute() else project_root / raw).resolve()
    path.relative_to(project_root.resolve())
    return path


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


def _load_binary_ref(
    value: object, *, project_root: Path, label: str,
) -> tuple[bytes, dict[str, str], Path]:
    row = value if isinstance(value, dict) else {}
    try:
        path = _project_path(row.get("path"), project_root)
        raw = path.read_bytes()
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label}:unreadable_or_path_invalid") from exc
    expected = str(row.get("sha256") or "")
    if not expected or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError(f"{label}:sha256_invalid")
    normalized = _ref(path, project_root)
    if row != normalized:
        raise ValueError(f"{label}:ref_not_canonical")
    return raw, normalized, path


def _fixed_protocols(
    project_root: Path,
) -> tuple[
    dict[str, Any], dict[str, Any], dict[str, str], dict[str, str], dict[str, str],
]:
    forward_path = (project_root / FORWARD_SESSION_PROTOCOL).resolve()
    replay_path = (project_root / REPLAY_PROTOCOL).resolve()
    evaluation_path = (project_root / EVALUATION_PROTOCOL).resolve()
    if not forward_path.is_file() or _sha256(forward_path) != FORWARD_SESSION_PROTOCOL_SHA256:
        raise ValueError("formal_forward_session_protocol_sha256_invalid")
    if not replay_path.is_file() or _sha256(replay_path) != REPLAY_PROTOCOL_SHA256:
        raise ValueError("formal_replay_v2_protocol_sha256_invalid")
    if (
        not evaluation_path.is_file()
        or _sha256(evaluation_path) != EVALUATION_PROTOCOL_SHA256
    ):
        raise ValueError("formal_evaluation_protocol_sha256_invalid")
    try:
        replay = json.loads(replay_path.read_text(encoding="utf-8"))
        evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("formal_evaluation_protocols_unreadable") from exc
    if (
        replay.get("status")
        != "successor_frozen_before_first_real_system_replay_or_performance_evaluation"
    ):
        raise ValueError("formal_replay_v2_protocol_status_invalid")
    completion = evaluation.get("completion") if isinstance(evaluation, dict) else None
    independent = (
        evaluation.get("independent_evidence")
        if isinstance(evaluation, dict) else None
    )
    if (
        evaluation.get("status") != "source_bound_accounting_runtime_admitted"
        or not isinstance(completion, dict)
        or completion.get("maximum_status") != "accounting_engine_only"
        or completion.get("evaluation_complete") is not False
        or completion.get("admission_ready") is not False
        or not isinstance(independent, dict)
        or independent.get("scenario_verifier") != "not_implemented"
        or independent.get("walk_forward_verifier") != "not_implemented"
    ):
        raise ValueError("formal_evaluation_protocol_boundary_invalid")
    for label, binding in (evaluation.get("bindings") or {}).items():
        path = _project_path((binding or {}).get("path"), project_root)
        if (
            not path.is_file()
            or _sha256(path) != str((binding or {}).get("sha256") or "")
        ):
            raise ValueError(f"formal_evaluation_binding_invalid:{label}")
    return (
        replay,
        evaluation,
        _ref(forward_path, project_root),
        _ref(replay_path, project_root),
        _ref(evaluation_path, project_root),
    )


def _canonical_verified_execution(
    execution: dict[str, Any], *, project_root: Path, label: str,
) -> dict[str, Any]:
    checked = verify_forward_execution_packet(
        execution, project_root=project_root,
    )
    verified = checked.get("packet") if isinstance(checked, dict) else None
    if checked.get("valid") is not True or not isinstance(verified, dict):
        errors = ",".join(str(value) for value in (checked.get("errors") or []))
        raise ValueError(
            f"{label}:recursive_forward_execution_verification_failed:{errors}"
        )
    if _canonical_bytes(verified) != _canonical_bytes(execution):
        raise ValueError(f"{label}:canonical_forward_execution_payload_mismatch")
    return verified


def _reverify_strict_audit_from_source(
    audit: dict[str, Any], audit_path: Path, execution: dict[str, Any],
    execution_ref: dict[str, str], *, project_root: Path, label: str,
) -> str:
    checked = verify_strict_joint_forward_audit(
        audit_path, project_root=project_root,
    )
    if checked.get("valid") is not True or checked.get("audit") != audit:
        errors = ",".join(str(value) for value in (checked.get("errors") or []))
        raise ValueError(f"{label}:stored_audit_invalid:{errors}")
    if audit.get("source_execution") != execution_ref:
        raise ValueError(f"{label}:source_execution_ref_mismatch")

    protocol_path = (project_root / STRICT_AUDIT_PROTOCOL).resolve()
    protocol, protocol_ref = _verify_strict_audit_protocol(
        project_root=project_root,
        protocol_path=protocol_path,
        expected_sha256=STRICT_AUDIT_PROTOCOL_SHA256,
    )
    if audit.get("protocol") != protocol_ref:
        raise ValueError(f"{label}:formal_strict_audit_protocol_required")

    sidecar_ref = audit.get("state_sidecar")
    sidecar_row = sidecar_ref if isinstance(sidecar_ref, dict) else {}
    sidecar_path = _project_path(sidecar_row.get("path"), project_root)
    expected_sidecar = sidecar_path.parent / (
        f"{audit.get('decision_day')}-{audit.get('slot_id')}.json"
    )
    if sidecar_path != expected_sidecar or not sidecar_path.is_file():
        raise ValueError(f"{label}:state_sidecar_path_invalid")
    state_dir = sidecar_path.parent.relative_to(project_root.resolve())
    rebuilt_bundle, rebuilt_state_ref = _rebuild_strict_bundle(
        execution,
        execution_ref,
        protocol,
        project_root=project_root,
        state_dir=state_dir,
    )
    if (
        rebuilt_state_ref != sidecar_ref
        or _canonical_bytes(rebuilt_bundle)
        != _canonical_bytes(audit.get("strict_input_bundle"))
    ):
        raise ValueError(f"{label}:strict_bundle_not_rebuilt_from_source")

    replayed = run_strict_joint_replay(
        copy.deepcopy(rebuilt_bundle), project_root=project_root,
    )
    if (
        not isinstance(replayed, dict)
        or _canonical_bytes(replayed)
        != _canonical_bytes(audit.get("strict_result"))
    ):
        raise ValueError(f"{label}:strict_result_recomputation_mismatch")
    if replayed.get("status") == "replay_complete" and isinstance(
        replayed.get("execution"), dict,
    ):
        expected_comparison = _compare_strict_execution(
            replayed["execution"], execution,
        )
        expected_status = (
            "audit_complete"
            if expected_comparison.get("all_match") is True
            else "execution_mismatch_fail_closed"
        )
    elif replayed.get("status") == "zero_signal":
        candidate = rebuilt_bundle["packets"]["stock_candidate"]
        no_plans = not candidate.get("entry_plans") and not candidate.get(
            "exit_plans"
        )
        source_no_fills = not execution.get("ledger") and not execution.get(
            "unfilled"
        )
        expected_no_trade = bool(
            no_plans
            and source_no_fills
            and "stock_candidate:no_executable_plan"
            in (replayed.get("zero_signal_reasons") or [])
        )
        expected_comparison = {
            "status": (
                "expected_no_trade_no_strict_execution"
                if expected_no_trade else "strict_replay_rejected"
            ),
            "all_match": False,
            "field_matches": {},
            "verified_only_FIFO_metadata_compared": False,
        }
        expected_status = (
            "expected_no_trade_zero_signal"
            if expected_no_trade else "strict_replay_rejected_fail_closed"
        )
    else:
        raise ValueError(f"{label}:strict_replay_status_invalid")
    if (
        audit.get("status") != expected_status
        or audit.get("execution_comparison") != expected_comparison
    ):
        raise ValueError(f"{label}:self_declared_audit_status_or_comparison_rejected")
    if expected_status not in {"audit_complete", "expected_no_trade_zero_signal"}:
        raise ValueError(f"{label}:strict_audit_not_session_eligible_after_recompute")
    return expected_status


def _evaluation_mark_evidence(
    session: dict[str, Any], execution: dict[str, Any], *,
    project_root: Path, label: str,
) -> dict[str, Any]:
    mark = session.get("after_session_mark")
    if not isinstance(mark, dict):
        raise ValueError(f"{label}:after_session_mark_missing")
    price_source, price_ref, _price_path = _load_ref(
        mark.get("price_source_ref"), project_root=project_root,
        label=f"{label}:price_source",
    )
    trade_date = str(mark.get("trade_date") or "")
    calendar_source, calendar_ref, _calendar_path = _load_ref(
        mark.get("calendar_ref"), project_root=project_root,
        label=f"{label}:calendar",
    )
    action_source, action_ref, _action_path = _load_ref(
        mark.get("corporate_action_ref"), project_root=project_root,
        label=f"{label}:corporate_action",
    )
    actions = action_source.get("actions")
    if isinstance(actions, list) and actions:
        raise ValueError(
            f"{label}:corporate_action_transformer_not_implemented"
        )
    source_admission_ref = price_source.get("source_admission_ref")
    if not isinstance(source_admission_ref, dict):
        raise ValueError(f"{label}:source_admission_ref_missing")
    try:
        verify_forward_completion_source_evidence(
            {
                "source_admission_ref": source_admission_ref,
                "price_source_ref": price_ref,
                "calendar_ref": calendar_ref,
                "corporate_action_ref": action_ref,
            },
            root=project_root,
            trade_date=trade_date,
            execution=execution,
            required_source_admission_ref=source_admission_ref,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"{label}:formal_completion_source_evidence_invalid:{exc}"
        ) from exc
    decision_date = _day(trade_date)
    if decision_date is None:
        raise ValueError(f"{label}:mark_trade_date_invalid")
    close_at = datetime.combine(decision_date, SESSION_CLOSE, tzinfo=SH_TZ)
    price_as_of = _timestamp(
        price_source.get("source_as_of"), f"{label}:price_source_as_of",
    )
    price_seen = _timestamp(
        price_source.get("first_seen_at"), f"{label}:price_first_seen",
    )
    calendar_seen = _timestamp(
        calendar_source.get("first_seen_at"), f"{label}:calendar_first_seen",
    )
    action_as_of = _timestamp(
        action_source.get("source_as_of"), f"{label}:action_source_as_of",
    )
    action_seen = _timestamp(
        action_source.get("first_seen_at"), f"{label}:action_first_seen",
    )
    marked_at = _timestamp(mark.get("marked_at"), f"{label}:marked_at")
    if (
        any(
            value.date() != decision_date
            for value in (
                price_as_of, price_seen, calendar_seen, action_as_of,
                action_seen, marked_at,
            )
        )
        or not (close_at <= price_as_of <= price_seen <= marked_at)
        or not (close_at <= calendar_seen <= marked_at)
        or not (action_as_of <= action_seen <= marked_at)
    ):
        raise ValueError(f"{label}:mark_evidence_chronology_invalid")
    return {
        "price_source_ref": price_ref,
        "provider": price_source["provider"],
        "parser_identity": price_source["parser_identity"],
        "parser_ref": price_source["parser_ref"],
        "source_admission_ref": source_admission_ref,
        "raw_response_ref": price_source["raw_response_ref"],
        "raw_response_sha256": price_source["raw_response_sha256"],
        "first_seen_at": price_seen.isoformat(timespec="seconds"),
        "calendar_ref": calendar_ref,
        "calendar_raw_ref": calendar_source["raw_calendar_ref"],
        "calendar_raw_sha256": calendar_source["raw_calendar_sha256"],
        "corporate_action_ref": action_ref,
        "corporate_action_raw_ref": action_source["raw_response_ref"],
        "corporate_action_raw_sha256": action_source["raw_response_sha256"],
        "corporate_actions_applied": False,
        "corporate_actions_observed": 0,
    }


def _validate_state(state: object, label: str) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise ValueError(f"{label}:object_required")
    if (
        state.get("schema") != "joint_execution_portfolio_state.v1"
        or state.get("state_id") != _identity(state, "state_id")
    ):
        raise ValueError(f"{label}:identity_invalid")
    cash = _finite(state.get("cash_yuan"))
    if cash is None or cash < 0:
        raise ValueError(f"{label}:cash_invalid")
    positions = state.get("positions")
    if not isinstance(positions, dict):
        raise ValueError(f"{label}:positions_invalid")
    for code, position in positions.items():
        if not isinstance(position, dict):
            raise ValueError(f"{label}:position_invalid:{code}")
        total = position.get("shares_total")
        sellable = position.get("sellable_shares")
        lots = position.get("locked_buys")
        if (
            isinstance(total, bool) or not isinstance(total, int) or total < 0
            or isinstance(sellable, bool) or not isinstance(sellable, int)
            or sellable < 0 or not isinstance(lots, list)
        ):
            raise ValueError(f"{label}:position_values_invalid:{code}")
        locked = 0
        lot_keys: list[tuple[str, str, int]] = []
        for lot in lots:
            shares = (lot or {}).get("shares") if isinstance(lot, dict) else None
            bought = str((lot or {}).get("buy_trade_date") or "")
            if (
                isinstance(shares, bool) or not isinstance(shares, int)
                or shares <= 0 or _day(bought) is None
            ):
                raise ValueError(f"{label}:FIFO_lot_invalid:{code}")
            locked += shares
            lot_keys.append((bought, str(lot.get("lot_id") or ""), shares))
        if lot_keys != sorted(lot_keys) or sellable + locked != total:
            raise ValueError(f"{label}:FIFO_lineage_invalid:{code}")
    costs = state.get("cumulative_costs")
    if not isinstance(costs, dict):
        raise ValueError(f"{label}:costs_invalid")
    for key in ("total", "commission", "transfer", "stamp", "slippage", "market_impact"):
        number = _finite(costs.get(key))
        if number is None or number < 0:
            raise ValueError(f"{label}:cost_invalid:{key}")
    for key in ("cumulative_buy_notional", "cumulative_sell_notional"):
        number = _finite(state.get(key))
        if number is None or number < 0:
            raise ValueError(f"{label}:{key}_invalid")
    return state


def _state_cost(state: dict[str, Any]) -> float:
    return float(state["cumulative_costs"]["total"])


def _state_notional(state: dict[str, Any]) -> float:
    return float(state["cumulative_buy_notional"]) + float(
        state["cumulative_sell_notional"]
    )


def _value_state(
    state: dict[str, Any], prices: object, *, label: str,
) -> float:
    rows = prices if isinstance(prices, dict) else {}
    equity = float(state["cash_yuan"])
    for code, position in state["positions"].items():
        price = _finite(rows.get(code))
        if price is None or price <= 0:
            raise ValueError(f"{label}:held_price_missing:{code}")
        equity += int(position["shares_total"]) * price
    if equity <= 0:
        raise ValueError(f"{label}:equity_invalid")
    return equity


def _order_rows(execution: dict[str, Any]) -> list[dict[str, Any]]:
    wrapper = execution.get("verified_wrapper")
    if not isinstance(wrapper, dict) or wrapper.get("schema") != "verified_portfolio_execution.a6":
        raise ValueError("verified_execution:wrapper_invalid")
    budgeted = wrapper.get("budgeted_decision_packet")
    decision = budgeted.get("decision") if isinstance(budgeted, dict) else None
    entries = decision.get("pre_execution_plans") if isinstance(decision, dict) else None
    risk = wrapper.get("risk_reduction_orders")
    state = wrapper.get("execution_input_state")
    pending = state.get("pending_exit_orders") if isinstance(state, dict) else None
    if not isinstance(entries, list) or not isinstance(risk, list) or not isinstance(pending, list):
        raise ValueError("verified_execution:order_contract_invalid")
    unique: dict[str, dict[str, Any]] = {}
    for source, values in (("entry", entries), ("risk", risk), ("pending", pending)):
        for index, row in enumerate(values):
            if not isinstance(row, dict):
                raise ValueError(f"verified_execution:order_invalid:{source}:{index}")
            key = str(row.get("order_id") or row.get("plan_id") or "")
            if not key:
                key = hashlib.sha256(_canonical_bytes([source, index, row])).hexdigest()
            unique.setdefault(key, row)
    return list(unique.values())


def _classify_session(
    execution: dict[str, Any], audit_status: str,
) -> dict[str, Any]:
    orders = _order_rows(execution)
    ledger = execution.get("ledger")
    unfilled = execution.get("unfilled")
    if not isinstance(ledger, list) or not isinstance(unfilled, list):
        raise ValueError("verified_execution:ledger_contract_invalid")
    positive = []
    for index, event in enumerate(ledger):
        filled = _finite((event or {}).get("filled_shares") if isinstance(event, dict) else None)
        if filled is None or filled <= 0 or not filled.is_integer():
            raise ValueError(f"verified_execution:nonpositive_ledger_fill:{index}")
        positive.append(event)
    for index, event in enumerate(unfilled):
        filled = _finite((event or {}).get("filled_shares") if isinstance(event, dict) else None)
        if filled is None or filled < 0 or not filled.is_integer():
            raise ValueError(f"verified_execution:unfilled_contract_invalid:{index}")
    if positive:
        if not orders or audit_status != "audit_complete":
            raise ValueError("session_classification:executed_without_verified_order")
        action = "executed"
    elif not orders and not ledger and not unfilled:
        if audit_status != "expected_no_trade_zero_signal":
            raise ValueError("session_classification:no_trade_not_explicit")
        action = "active_no_trade"
    elif orders and not ledger and unfilled and all(
        int(float(row.get("filled_shares") or 0)) == 0
        for row in unfilled if isinstance(row, dict)
    ):
        if audit_status != "audit_complete":
            raise ValueError("session_classification:unavailable_audit_invalid")
        action = "execution_unavailable"
    else:
        raise ValueError("session_classification:ambiguous")
    filled_order_ids = {
        str(row.get("order_id") or index)
        for index, row in enumerate(positive)
    }
    return {
        "action": action,
        "order_count": len(orders),
        "filled_order_count": len(filled_order_ids),
        "unfilled_order_events": len(unfilled),
        "trade_count": len(positive),
    }


def _gap_manifest(
    ref_value: object, *, project_root: Path, input_ref: dict[str, str],
    input_id: str, observed_dates: list[str], fixed_epoch_start: str,
) -> tuple[
    dict[str, Any], dict[str, str], list[str], str, str, dict[str, str],
]:
    manifest, normalized, _path = _load_ref(
        ref_value, project_root=project_root, label="gap_manifest",
    )
    if (
        manifest.get("schema") != "market_cognition_expected_session_gap_manifest.v1"
        or manifest.get("no_backfill") is not True
        or manifest.get("manifest_id") != _identity(manifest, "manifest_id")
        or manifest.get("strict_forward_input_ref") != input_ref
        or manifest.get("strict_forward_input_id") != input_id
    ):
        raise ValueError("gap_manifest:contract_or_input_binding_invalid")
    epoch_start = str(manifest.get("fixed_forward_epoch_start") or "")
    cutoff = str(manifest.get("evaluation_cutoff") or "")
    if (
        epoch_start != fixed_epoch_start
        or _day(epoch_start) is None
        or _day(cutoff) is None
        or cutoff < epoch_start
    ):
        raise ValueError("gap_manifest:epoch_or_cutoff_invalid")
    calendar_raw, calendar_ref, calendar_path = _load_binary_ref(
        manifest.get("audited_calendar_ref"),
        project_root=project_root,
        label="gap_manifest:audited_calendar",
    )
    expected_calendar_dir = (
        project_root
        / FORWARD_COMPLETION_EVIDENCE_DIR
        / "raw"
        / "trading_calendars"
    ).resolve()
    if (
        calendar_path.parent != expected_calendar_dir
        or calendar_path.name != f"{calendar_ref['sha256']}.bin"
    ):
        raise ValueError("gap_manifest:audited_calendar_not_canonical_snapshot")
    try:
        calendar = json.loads(calendar_raw)
    except json.JSONDecodeError as exc:
        raise ValueError("gap_manifest:audited_calendar_json_invalid") from exc
    trading_dates = (
        calendar.get("trading_dates") if isinstance(calendar, dict) else None
    )
    if (
        manifest.get("audited_calendar_ref") != calendar_ref
        or not isinstance(calendar, dict)
        or calendar.get("version") != 1
        or calendar.get("status") != "current"
        or calendar.get("source") != "BaoStock query_trade_dates"
        or not isinstance(trading_dates, list)
        or not trading_dates
        or trading_dates != sorted(set(trading_dates))
        or any(_day(value) is None for value in trading_dates)
        or calendar.get("coverage_start") != trading_dates[0]
        or calendar.get("coverage_end") != trading_dates[-1]
        or epoch_start < trading_dates[0]
        or cutoff > trading_dates[-1]
    ):
        raise ValueError("gap_manifest:audited_calendar_invalid")
    calendar_expected = [
        value for value in trading_dates if epoch_start <= value <= cutoff
    ]
    expected = manifest.get("expected_trading_dates")
    gaps = manifest.get("gaps")
    if not isinstance(expected, list) or not expected or not isinstance(gaps, list):
        raise ValueError("gap_manifest:dates_missing")
    if expected != calendar_expected:
        raise ValueError("gap_manifest:expected_dates_not_calendar_exact")
    if (
        any(_day(value) is None for value in expected)
        or expected != sorted(set(expected))
        or observed_dates != sorted(set(observed_dates))
    ):
        raise ValueError("gap_manifest:date_identity_invalid")
    gap_dates: list[str] = []
    for index, row in enumerate(gaps):
        trade_date = str((row or {}).get("trade_date") or "") if isinstance(row, dict) else ""
        reason = str((row or {}).get("reason") or "") if isinstance(row, dict) else ""
        if _day(trade_date) is None or not reason:
            raise ValueError(f"gap_manifest:gap_invalid:{index}")
        gap_dates.append(trade_date)
    if gap_dates != sorted(set(gap_dates)):
        raise ValueError("gap_manifest:gap_identity_invalid")
    if set(observed_dates) & set(gap_dates) or set(expected) != set(observed_dates) | set(gap_dates):
        raise ValueError("gap_manifest:coverage_not_exact")
    if manifest.get("observed_session_dates") != observed_dates:
        raise ValueError("gap_manifest:observed_dates_mismatch")
    return (
        manifest, normalized, gap_dates, epoch_start, cutoff, calendar_ref,
    )


def _require_all_formal_sessions(
    payload: dict[str, Any], input_ref: dict[str, str], input_path: Path, *,
    project_root: Path, epoch_start: str, cutoff: str,
    observed_dates: list[str], audited_calendar_ref: dict[str, str],
) -> None:
    formal_input_dir = (project_root / FORWARD_INPUT_DIR).resolve()
    formal_session_dir = (project_root / FORWARD_SESSION_DIR).resolve()
    if (
        input_path.resolve().parent != formal_input_dir
        or input_ref != _ref(input_path, project_root)
    ):
        raise ValueError("strict_forward_input_not_in_formal_directory")
    supplied_refs = payload.get("session_refs")
    if not isinstance(supplied_refs, list):
        raise ValueError("strict_forward_session_refs_missing")
    normalized_supplied: list[dict[str, str]] = []
    for index, ref_value in enumerate(supplied_refs):
        row = ref_value if isinstance(ref_value, dict) else {}
        path = _project_path(row.get("path"), project_root)
        if path.parent != formal_session_dir:
            raise ValueError(
                f"strict_forward_session_not_in_formal_directory:{index}"
            )
        normalized_supplied.append(_ref(path, project_root))
    if supplied_refs != normalized_supplied:
        raise ValueError("strict_forward_session_refs_not_canonical")

    completion_dir = (
        project_root / FORWARD_COMPLETION_EVIDENCE_DIR / "completions"
    ).resolve()
    if not completion_dir.is_dir():
        raise ValueError("formal_forward_completion_directory_missing")
    committed: list[dict[str, Any]] = []
    for path in sorted(completion_dir.glob("*.json")):
        filename_date = path.stem
        if _day(filename_date) is None:
            raise ValueError("formal_forward_completion_filename_invalid")
        if not (epoch_start <= filename_date <= cutoff):
            continue
        checked = verify_forward_completion_manifest(
            path, root=project_root,
        )
        manifest = checked.get("manifest") if isinstance(checked, dict) else None
        if checked.get("valid") is not True or not isinstance(manifest, dict):
            errors = ",".join(str(value) for value in (checked.get("errors") or []))
            raise ValueError(
                "formal_forward_completion_recursive_verification_failed:"
                + errors
            )
        if manifest.get("trade_date") != filename_date:
            raise ValueError("formal_forward_completion_filename_date_mismatch")
        calendar_packet, calendar_ref, _calendar_path = _load_ref(
            manifest.get("calendar_ref"),
            project_root=project_root,
            label="formal_completion_calendar",
        )
        raw_calendar_ref = calendar_packet.get("raw_calendar_ref")
        if manifest.get("calendar_ref") != calendar_ref or not isinstance(
            raw_calendar_ref, dict
        ):
            raise ValueError("formal_forward_completion_calendar_ref_invalid")
        committed.append({
            "trade_date": filename_date,
            "session_ref": manifest.get("session_ref"),
            "input_ref": manifest.get("input_ref"),
            "raw_calendar_ref": raw_calendar_ref,
        })
    committed_dates = [row["trade_date"] for row in committed]
    if (
        not committed
        or committed_dates != sorted(set(committed_dates))
        or committed_dates != observed_dates
    ):
        raise ValueError("strict_forward_observed_sessions_not_equal_committed_set")
    existing_refs = [row["session_ref"] for row in committed]
    supplied_keys = {
        (str(row.get("path") or ""), str(row.get("sha256") or ""))
        for row in normalized_supplied
    }
    existing_keys = {
        (str(row.get("path") or ""), str(row.get("sha256") or ""))
        for row in existing_refs
    }
    if supplied_keys != existing_keys:
        raise ValueError("strict_forward_input_omits_commit_or_includes_orphan_session")
    latest = committed[-1]
    if latest["input_ref"] != input_ref:
        raise ValueError("strict_forward_input_not_latest_committed_through_cutoff")
    if latest["raw_calendar_ref"] != audited_calendar_ref:
        raise ValueError("gap_calendar_not_bound_to_latest_committed_completion")


def _metric(value: object = None, reason: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"value": value, "reason": reason, **extra}


def _drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


def _conditional(
    rows: list[dict[str, Any]], key: str, *, complete_path: bool,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get(key) or "none"), []).append(row)
    result: dict[str, dict[str, Any]] = {}
    for name, values in sorted(grouped.items()):
        orders = sum(row["order_count"] for row in values)
        fills = sum(row["filled_order_count"] for row in values)
        if complete_path:
            returns = [row["session_return"] for row in values]
            compounded = math.prod(1.0 + value for value in returns) - 1.0
            path = [1.0]
            for value in returns:
                path.append(path[-1] * (1.0 + value))
            return_metric = _metric(round(compounded * 100.0, 8))
            drawdown_metric = _metric(round(_drawdown(path) * 100.0, 8))
        else:
            return_metric = _metric(None, "expected_session_gaps_present")
            drawdown_metric = _metric(None, "expected_session_gaps_present")
        result[name] = {
            "sessions": len(values),
            "executed_sessions": sum(row["action"] == "executed" for row in values),
            "active_no_trade_sessions": sum(
                row["action"] == "active_no_trade" for row in values
            ),
            "execution_unavailable_sessions": sum(
                row["action"] == "execution_unavailable" for row in values
            ),
            "order_count": orders,
            "filled_order_count": fills,
            "fill_rate_pct": _metric(
                round(fills / orders * 100.0, 8) if orders else None,
                None if orders else "no_orders_in_group",
            ),
            "cost_after_compounded_return_pct": return_metric,
            "maximum_drawdown_pct": drawdown_metric,
        }
    return result


def _performance_metrics(
    rows: list[dict[str, Any]], initial_equity: float, gap_dates: list[str],
    *, annual_cash_rate: float | None,
) -> dict[str, Any]:
    complete = not gap_dates
    returns = [float(row["session_return"]) for row in rows]
    equities = [initial_equity, *[float(row["equity_yuan"]) for row in rows]]
    months = sorted({str(row["trade_date"])[:7] for row in rows})
    enough_cagr = len(rows) >= 60 and len(months) >= 3
    start = _day(rows[0]["previous_trade_date"])
    finish = _day(rows[-1]["trade_date"])
    elapsed_days = (finish - start).days if start and finish else 0
    path_reason = "expected_session_gaps_present" if not complete else None

    total_return = equities[-1] / equities[0] - 1.0
    maximum_drawdown = _drawdown(equities)
    if complete:
        total_metric = _metric(round(total_return * 100.0, 8))
        drawdown_metric = _metric(round(maximum_drawdown * 100.0, 8))
    else:
        total_metric = _metric(None, path_reason)
        drawdown_metric = _metric(None, path_reason)

    if not complete:
        cagr = None
        cagr_reason = path_reason
    elif not enough_cagr or elapsed_days <= 0:
        cagr = None
        cagr_reason = "requires_60_completed_sessions_spanning_3_calendar_months"
    else:
        cagr = (equities[-1] / equities[0]) ** (365.2425 / elapsed_days) - 1.0
        cagr_reason = None
    cagr_metric = _metric(
        round(cagr * 100.0, 8) if cagr is not None else None, cagr_reason,
    )

    if not complete:
        volatility = None
        volatility_reason = path_reason
    elif len(returns) < 20:
        volatility = None
        volatility_reason = "requires_20_completed_sessions"
    else:
        volatility = pstdev(returns) * math.sqrt(252.0)
        volatility_reason = None
    volatility_metric = _metric(
        round(volatility * 100.0, 8) if volatility is not None else None,
        volatility_reason,
    )

    if annual_cash_rate is None:
        sharpe = None
        sharpe_reason = "declared_annual_cash_rate_required"
    elif not complete:
        sharpe = None
        sharpe_reason = path_reason
    elif len(returns) < 20:
        sharpe = None
        sharpe_reason = "requires_20_completed_sessions"
    else:
        daily_cash = (1.0 + annual_cash_rate) ** (1.0 / 252.0) - 1.0
        deviation = pstdev(returns)
        if deviation <= 0:
            sharpe = None
            sharpe_reason = "zero_session_return_volatility"
        else:
            sharpe = fmean(value - daily_cash for value in returns) / deviation * math.sqrt(252.0)
            sharpe_reason = None
    sharpe_metric = _metric(
        round(sharpe, 8) if sharpe is not None else None, sharpe_reason,
        declared_annual_cash_rate=annual_cash_rate,
    )

    if cagr is None:
        calmar_metric = _metric(None, cagr_reason)
    elif maximum_drawdown >= 0:
        calmar_metric = _metric(None, "no_observed_drawdown")
    else:
        calmar_metric = _metric(round(cagr / abs(maximum_drawdown), 8))

    if not complete:
        rolling_metric = _metric(None, path_reason)
    elif len(returns) < 20:
        rolling_metric = _metric(None, "requires_20_completed_sessions")
    else:
        windows = [
            math.prod(1.0 + value for value in returns[index:index + 20]) - 1.0
            for index in range(len(returns) - 19)
        ]
        rolling_metric = _metric(round(min(windows) * 100.0, 8))

    year_rows: dict[str, list[float]] = {}
    for row in rows:
        year_rows.setdefault(str(row["trade_date"])[:4], []).append(row["session_return"])
    complete_years = {
        year: values for year, values in year_rows.items() if len(values) >= 200
    }
    if not complete:
        worst_year_metric = _metric(None, path_reason)
    elif not complete_years:
        worst_year_metric = _metric(None, "requires_at_least_200_sessions_in_one_calendar_year")
    else:
        yearly = {
            year: math.prod(1.0 + value for value in values) - 1.0
            for year, values in complete_years.items()
        }
        worst = min(yearly, key=yearly.get)
        worst_year_metric = _metric({
            "year": worst, "return_pct": round(yearly[worst] * 100.0, 8),
        })

    costs = sum(float(row["cost_delta_yuan"]) for row in rows)
    turnover = sum(float(row["turnover_delta_yuan"]) for row in rows)
    average_equity = fmean(equities)
    return {
        "cost_after_total_return_pct": total_metric,
        "cost_after_CAGR_pct": cagr_metric,
        "maximum_drawdown_pct": drawdown_metric,
        "Calmar": calmar_metric,
        "annualized_volatility_pct": volatility_metric,
        "Sharpe_with_declared_cash_rate": sharpe_metric,
        "total_cost_yuan": _metric(
            round(costs, 6), coverage_complete=complete,
        ),
        "turnover_pct_of_average_equity": _metric(
            round(turnover / average_equity * 100.0, 8),
            coverage_complete=complete,
        ),
        "worst_calendar_year": worst_year_metric,
        "worst_rolling_20_session_return_pct": rolling_metric,
    }


def _scenario_evidence(
    refs: object, *, project_root: Path, input_id: str,
    required: list[str],
) -> dict[str, Any]:
    supplied = refs if isinstance(refs, dict) else {}
    provided = sorted(
        scenario_id for scenario_id in required if scenario_id in supplied
    )
    return {
        "required": required,
        "provided_but_unverified": provided,
        "verified": [],
        "missing": list(required),
        "invalid": {},
        "complete": False,
        "reason": "independent_verifier_not_implemented",
        "base_input_id": input_id,
    }


def _walk_forward_evidence(
    ref_value: object, *, project_root: Path, input_id: str,
) -> dict[str, Any]:
    return {
        "verified": False,
        "reason": "independent_verifier_not_implemented",
        "provided_but_unverified": isinstance(ref_value, dict),
        "base_input_id": input_id,
    }


def _completion_status(
    metrics: dict[str, Any], conditional: dict[str, Any],
    sample_gate: dict[str, Any], scenarios: dict[str, Any],
    walk_forward: dict[str, Any],
) -> dict[str, Any]:
    required_scalar_metrics = [
        "cost_after_CAGR_pct",
        "maximum_drawdown_pct",
        "Calmar",
        "annualized_volatility_pct",
        "Sharpe_with_declared_cash_rate",
        "total_cost_yuan",
        "turnover_pct_of_average_equity",
        "worst_calendar_year",
        "worst_rolling_20_session_return_pct",
        "capacity_and_failed_fill_sensitivity",
    ]
    scalar_complete = all(
        isinstance(metrics.get(name), dict)
        and metrics[name].get("value") is not None
        for name in required_scalar_metrics
    )
    classification = metrics.get("session_classification")
    classification_complete = bool(
        isinstance(classification, dict)
        and isinstance(classification.get("counts"), dict)
        and isinstance(classification.get("ratios_pct"), dict)
        and isinstance(classification.get("trade_count"), int)
    )
    conditional_complete = True
    for dimension in ("market_state", "playbook"):
        groups = conditional.get(dimension)
        if not isinstance(groups, dict) or not groups:
            conditional_complete = False
            continue
        for row in groups.values():
            if not isinstance(row, dict):
                conditional_complete = False
                continue
            for name in (
                "cost_after_compounded_return_pct", "maximum_drawdown_pct",
            ):
                if not isinstance(row.get(name), dict) or row[name].get("value") is None:
                    conditional_complete = False
    diagnostic_complete = bool(
        sample_gate.get("passed") is True
        and scalar_complete
        and classification_complete
        and conditional_complete
    )
    incomplete_reasons: list[str] = []
    if not diagnostic_complete:
        incomplete_reasons.append("required_diagnostic_metrics_incomplete")
    if scenarios.get("complete") is not True:
        incomplete_reasons.append("independent_scenario_verifier_not_implemented")
    if walk_forward.get("verified") is not True:
        incomplete_reasons.append("independent_walk_forward_verifier_not_implemented")
    incomplete_reasons.append("quantitative_admission_protocol_not_frozen")
    return {
        "maximum_status": "accounting_engine_only",
        "diagnostic_metrics_complete": diagnostic_complete,
        "evaluation_complete": False,
        "admission_ready": False,
        "incomplete_reasons": list(dict.fromkeys(incomplete_reasons)),
    }


def _finish(
    *, status: str, mode: str, errors: list[str],
    input_ref: dict[str, str] | None = None,
    protocols: dict[str, Any] | None = None,
    **values: Any,
) -> dict[str, Any]:
    result = {
        "schema": "market_cognition_system_evaluation.v2",
        "status": status,
        "mode": mode,
        "errors": list(dict.fromkeys(errors)),
        "input_ref": input_ref,
        "protocols": protocols,
        "authority": _authority(),
        **values,
    }
    result["evaluation_complete"] = False
    result["admission_ready"] = False
    result["production_ready"] = False
    result["stable_profit_proven"] = False
    result["result_id"] = _identity(result, "result_id")
    return result


def evaluate_market_cognition_system_v2(
    input_ref: dict[str, Any] | None, *, mode: str,
    gap_manifest_ref: dict[str, Any] | None = None,
    annual_cash_rate: float | None = None,
    scenario_refs: dict[str, dict[str, Any]] | None = None,
    walk_forward_ref: dict[str, Any] | None = None,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Evaluate one immutable strict-forward input or preserve a tier boundary."""
    if mode == "historical_PIT":
        return _finish(
            status="fail_closed", mode=mode,
            errors=["historical_PIT_forced_fail_closed_by_v2_protocol"],
            metrics=None, session_rows=[],
        )
    if mode == "synthetic_contract":
        return _finish(
            status="contract_only", mode=mode, errors=[], metrics=None,
            session_rows=[], performance_claim=False,
            contract_note="synthetic fixtures cannot support admission or impersonate strict-forward evidence",
        )
    if mode != "strict_forward":
        return _finish(
            status="fail_closed", mode=mode, errors=["mode_not_supported"],
            metrics=None, session_rows=[],
        )

    normalized_input_ref: dict[str, str] | None = None
    try:
        annual_rate = _finite(annual_cash_rate)
        if annual_cash_rate is not None and (annual_rate is None or annual_rate <= -1.0):
            raise ValueError("annual_cash_rate_invalid")
        (
            replay_protocol,
            evaluation_protocol,
            formal_forward_ref,
            replay_ref,
            evaluation_ref,
        ) = _fixed_protocols(project_root)
        payload, normalized_input_ref, input_path = _load_ref(
            input_ref, project_root=project_root, label="strict_forward_input",
        )
        if input_ref != normalized_input_ref:
            raise ValueError("strict_forward_input_ref_not_canonical")
        checked = verify_strict_forward_input(input_path, project_root=project_root)
        if checked.get("valid") is not True:
            raise ValueError(
                "strict_forward_input_invalid:" + ",".join(checked.get("errors") or [])
            )
        payload = checked["input"]
        if payload.get("mode") != "strict_forward":
            raise ValueError("strict_forward_mode_impersonation")
        if payload.get("assembly_protocol") != formal_forward_ref:
            raise ValueError("formal_forward_session_protocol_ref_required")

        sessions = payload["sessions"]
        observed_dates = [str(session.get("trade_date") or "") for session in sessions]
        if observed_dates != sorted(set(observed_dates)):
            raise ValueError("strict_forward_trade_dates_not_unique_and_sorted")
        fixed_epoch_start = str(
            (evaluation_protocol.get("coverage_contract") or {}).get(
                "forward_evaluation_epoch_start"
            ) or ""
        )
        (
            manifest,
            normalized_manifest_ref,
            gap_dates,
            epoch_start,
            evaluation_cutoff,
            audited_calendar_ref,
        ) = _gap_manifest(
            gap_manifest_ref, project_root=project_root,
            input_ref=normalized_input_ref, input_id=payload["input_id"],
            observed_dates=observed_dates,
            fixed_epoch_start=fixed_epoch_start,
        )
        _require_all_formal_sessions(
            payload,
            normalized_input_ref,
            input_path,
            project_root=project_root,
            epoch_start=epoch_start,
            cutoff=evaluation_cutoff,
            observed_dates=observed_dates,
            audited_calendar_ref=audited_calendar_ref,
        )

        rows: list[dict[str, Any]] = []
        previous_after: dict[str, Any] | None = None
        previous_equity: float | None = None
        initial_equity: float | None = None
        for index, session in enumerate(sessions):
            audit, audit_ref, audit_path = _load_ref(
                session.get("strict_audit_ref"), project_root=project_root,
                label=f"session:{index}:strict_audit",
            )
            execution, execution_ref, _execution_path = _load_ref(
                session.get("verified_execution_ref"), project_root=project_root,
                label=f"session:{index}:verified_execution",
            )
            if (
                execution_ref != session.get("verified_execution_ref")
                or audit.get("source_execution") != execution_ref
                or execution.get("schema") != "unified_regime_route_execution_day.v1"
                or execution.get("no_backfill") is not True
                or execution.get("packet_id") != _identity(execution, "packet_id")
            ):
                raise ValueError(f"session:{index}:verified_execution_identity_invalid")
            execution = _canonical_verified_execution(
                execution,
                project_root=project_root,
                label=f"session:{index}:verified_execution",
            )
            if audit_ref != session.get("strict_audit_ref"):
                raise ValueError(f"session:{index}:strict_audit_ref_not_canonical")
            audit_status = _reverify_strict_audit_from_source(
                audit,
                audit_path,
                execution,
                execution_ref,
                project_root=project_root,
                label=f"session:{index}:strict_audit",
            )
            mark_evidence = _evaluation_mark_evidence(
                session,
                execution,
                project_root=project_root,
                label=f"session:{index}",
            )
            wrapper = execution.get("verified_wrapper")
            if not isinstance(wrapper, dict):
                raise ValueError(f"session:{index}:verified_wrapper_missing")
            before = _validate_state(
                wrapper.get("execution_input_state"),
                f"session:{index}:execution_input_state",
            )
            after = _validate_state(
                execution.get("state_after"), f"session:{index}:execution_state_after",
            )
            if (
                execution.get("previous_state_id") != before["state_id"]
                or after.get("previous_state_id") != before["state_id"]
                or after.get("last_trade_date") != session.get("trade_date")
                or (session.get("verified_portfolio_state_truth") or {}).get("state_id")
                != after["state_id"]
            ):
                raise ValueError(f"session:{index}:verified_state_transition_invalid")
            if previous_after is not None and _canonical_bytes(before) != _canonical_bytes(previous_after):
                raise ValueError(f"session:{index}:verified_full_state_FIFO_lineage_broken")

            market = wrapper.get("market_snapshot")
            quotes = market.get("quotes") if isinstance(market, dict) else None
            previous_trade_date = str(
                market.get("previous_trade_date") if isinstance(market, dict) else ""
            )
            if (
                not isinstance(quotes, dict) or _day(previous_trade_date) is None
                or market.get("trade_date") != session.get("trade_date")
            ):
                raise ValueError(f"session:{index}:verified_market_snapshot_invalid")
            if previous_equity is None:
                initial_prices = {
                    code: (quotes.get(code) or {}).get("prev_close")
                    for code in before["positions"]
                }
                previous_equity = _value_state(
                    before, initial_prices, label=f"session:{index}:initial_valuation",
                )
                initial_equity = previous_equity
            equity = _value_state(
                after, session["after_session_mark"].get("prices_yuan"),
                label=f"session:{index}:source_bound_mark",
            )
            before_cost, after_cost = _state_cost(before), _state_cost(after)
            before_notional, after_notional = _state_notional(before), _state_notional(after)
            if after_cost + 1e-9 < before_cost or after_notional + 1e-9 < before_notional:
                raise ValueError(f"session:{index}:cumulative_accounting_regressed")
            classified = _classify_session(execution, audit_status)
            packets = (session.get("strict_bundle") or {}).get("packets") or {}
            row = {
                "session_id": session.get("session_id"),
                "decision_day": session.get("decision_day"),
                "previous_trade_date": previous_trade_date,
                "trade_date": session.get("trade_date"),
                "market_state": (packets.get("market_state") or {}).get("market_state"),
                "playbook_id": (packets.get("fixed_playbook") or {}).get("playbook_id"),
                **classified,
                "cost_delta_yuan": after_cost - before_cost,
                "turnover_delta_yuan": after_notional - before_notional,
                "equity_yuan": equity,
                "session_return": equity / previous_equity - 1.0,
                "verified_input_state_id": before["state_id"],
                "verified_state_after_id": after["state_id"],
                "strict_state_used_as_portfolio_truth": False,
                "mark_evidence": mark_evidence,
            }
            rows.append(row)
            previous_after = copy.deepcopy(after)
            previous_equity = equity
        if initial_equity is None:
            raise ValueError("strict_forward_sessions_missing")

        metrics = _performance_metrics(
            rows, initial_equity, gap_dates, annual_cash_rate=annual_rate,
        )
        expected_count = len(manifest["expected_trading_dates"])
        counts = {
            "executed": sum(row["action"] == "executed" for row in rows),
            "active_no_trade": sum(row["action"] == "active_no_trade" for row in rows),
            "execution_unavailable": sum(
                row["action"] == "execution_unavailable" for row in rows
            ),
            "data_incomplete": len(gap_dates),
        }
        if sum(counts.values()) != expected_count:
            raise ValueError("session_classification:not_mutually_exclusive_and_exhaustive")
        orders = sum(row["order_count"] for row in rows)
        filled_orders = sum(row["filled_order_count"] for row in rows)
        unfilled_events = sum(row["unfilled_order_events"] for row in rows)
        metrics["session_classification"] = {
            "counts": counts,
            "ratios_pct": {
                key: round(value / expected_count * 100.0, 8)
                for key, value in counts.items()
            },
            "trade_count": sum(row["trade_count"] for row in rows),
            "order_count": orders,
            "filled_order_count": filled_orders,
            "fill_rate_pct": _metric(
                round(filled_orders / orders * 100.0, 8) if orders else None,
                None if orders else "no_orders",
            ),
            "failed_fill_event_rate_pct": _metric(
                round(unfilled_events / orders * 100.0, 8) if orders else None,
                None if orders else "no_orders",
            ),
        }
        metrics["capacity_and_failed_fill_sensitivity"] = _metric(
            None, "independent_verifier_not_implemented",
        )
        complete_path = not gap_dates
        conditional = {
            "market_state": _conditional(rows, "market_state", complete_path=complete_path),
            "playbook": _conditional(rows, "playbook_id", complete_path=complete_path),
            "interpretation": "descriptive cost-after accounting; not causal attribution or a profit claim",
        }

        gate = replay_protocol["evaluation_completion_gate"]
        required_scenarios = [
            str(gate["required_cost_stress"]),
            *[str(value) for value in gate["required_counterfactuals"]],
        ]
        scenarios = _scenario_evidence(
            scenario_refs, project_root=project_root,
            input_id=payload["input_id"], required=required_scenarios,
        )
        walk_forward = _walk_forward_evidence(
            walk_forward_ref, project_root=project_root, input_id=payload["input_id"],
        )
        months = {str(row["trade_date"])[:7] for row in rows}
        sample_gate = {
            "minimum_completed_sessions": 60,
            "minimum_calendar_months": 3,
            "completed_sessions": len(rows),
            "calendar_months": len(months),
            "no_expected_session_gaps": not gap_dates,
            "passed": len(rows) >= 60 and len(months) >= 3 and not gap_dates,
        }
        completion = _completion_status(
            metrics,
            conditional,
            sample_gate,
            scenarios,
            walk_forward,
        )
        output_rows = []
        for row in rows:
            public_row = copy.deepcopy(row)
            public_row["session_return_pct"] = round(
                public_row.pop("session_return") * 100.0, 8
            )
            public_row["cost_delta_yuan"] = round(public_row["cost_delta_yuan"], 6)
            public_row["turnover_delta_yuan"] = round(
                public_row["turnover_delta_yuan"], 6
            )
            public_row["equity_yuan"] = round(public_row["equity_yuan"], 6)
            output_rows.append(public_row)
        result = _finish(
            status="evaluated", mode=mode, errors=[], input_ref=normalized_input_ref,
            protocols={
                "forward_session": formal_forward_ref,
                "replay_v2": replay_ref,
                "evaluation": evaluation_ref,
            },
            gap_manifest_ref=normalized_manifest_ref,
            audited_calendar_ref=audited_calendar_ref,
            fixed_forward_epoch_start=epoch_start,
            evaluation_cutoff=evaluation_cutoff,
            source_bound_cost_after=True,
            portfolio_state_source="verified_wrapper.execution_input_state_to_execution.state_after",
            strict_result_role="economic_consistency_audit_only",
            missing_session_policy="visible_data_incomplete_never_zero_return_or_silent_drop",
            session_rows=output_rows,
            metrics=metrics,
            conditional_performance=conditional,
            sample_gate=sample_gate,
            scenario_evidence=scenarios,
            walk_forward_evidence=walk_forward,
            completion=completion,
            maximum_status=completion["maximum_status"],
            diagnostic_metrics_complete=completion[
                "diagnostic_metrics_complete"
            ],
            evaluation_complete=False,
            admission_ready=False,
        )
        return result
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return _finish(
            status="fail_closed", mode=mode,
            errors=[f"{type(exc).__name__}:{exc}"],
            input_ref=normalized_input_ref, metrics=None, session_rows=[],
        )


__all__ = ["evaluate_market_cognition_system_v2"]
