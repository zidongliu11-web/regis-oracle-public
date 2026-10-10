"""Strict-forward raw packet adapter for the frozen V2 system replay."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .intraday_market_emotion_forward import (
    build_intraday_emotion_observation,
    verify_intraday_emotion_packet,
)
from .market_sector_constituent_forward import verify_constituent_packet
from .market_sector_route_forward import capture_slot, verify_market_sector_route_packet
from .market_sector_route_shadow import (
    REQUIRED_PERCENTILES,
    _bound_feature_row,
    verify_shadow_decision_packet,
)
from .market_sector_strategy_adapter import adapt_market_observation
from .market_state_daily import PROJECT_ROOT, _canonical_bytes
from .research_artifact_paths import research_artifact_path
from .market_state_forward import verify_forward_packet
from .system_route_account_packet_v2 import (
    verify_system_route_account_packet_v2,
)
from .system_route_replay_driver import (
    PROTOCOL_PATH as REPLAY_PROTOCOL_PATH,
    PROTOCOL_SHA256 as REPLAY_PROTOCOL_SHA256,
    build_system_route_replay,
)


SH_TZ = ZoneInfo("Asia/Shanghai")
ADAPTER_PROTOCOL_PATH = (
    PROJECT_ROOT / "research/SYSTEM_ROUTE_JOINT_V2_ADAPTER_PROTOCOL_V14.json"
)
ADAPTER_PROTOCOL_SHA256 = (
    "0070dacf872ea702156fec2b4027e254a6aab2bcc6ef7e5f4b24ab8c24cff2fa"
)
_SPECS: dict[str, Callable[..., dict[str, Any]]] = {
    "H1_market_state": verify_forward_packet,
    "market_sector": verify_market_sector_route_packet,
    "shadow_decision": verify_shadow_decision_packet,
    "sector_constituents": verify_constituent_packet,
    "intraday_emotion": verify_intraday_emotion_packet,
    "account_snapshot": verify_system_route_account_packet_v2,
}
_OUTCOME_KEYS = {
    "future_return", "future_returns", "labels", "outcome",
    "outcomes", "strategy_return", "annualized_return", "max_drawdown",
    "sharpe", "nav", "pnl",
}
_OUTCOME_FLAGS = {
    "returns_or_labels_read", "post_decision_returns_or_labels_read",
    "execution_performed", "performance_computed", "historical_return_run",
}


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


def _time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(SH_TZ)


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _walk(value: object, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = (*path, str(key))
            yield child_path, child
            yield from _walk(child, child_path)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child, (*path, "[]"))


def _path_matches(pattern: str, path: tuple[str, ...]) -> bool:
    expected = tuple(pattern.split("."))
    return len(expected) == len(path) and all(
        wanted == "*" or wanted == actual
        for wanted, actual in zip(expected, path)
    )


def _slot_contains(
    observed: datetime, slot_id: str, rows: object,
) -> bool:
    clock = observed.timetz().replace(tzinfo=None)
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("id") != slot_id:
            continue
        try:
            start = time.fromisoformat(str(row.get("capture_from")))
            end = time.fromisoformat(str(row.get("capture_until")))
        except ValueError:
            return False
        return start <= clock <= end
    return False


def _bound_schedule(protocol: dict[str, Any], name: str, key: str) -> object:
    binding = (protocol.get("bindings") or {}).get(name) or {}
    path = PROJECT_ROOT / str(binding.get("path") or "")
    document = json.loads(path.read_text(encoding="utf-8"))
    return (document.get(key) or {}).get("slots")


def _protocol() -> tuple[dict[str, Any], list[str]]:
    try:
        raw = ADAPTER_PROTOCOL_PATH.read_bytes()
        protocol = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {}, [f"adapter_protocol_unreadable:{type(exc).__name__}"]
    errors: list[str] = []
    if hashlib.sha256(raw).hexdigest() != ADAPTER_PROTOCOL_SHA256:
        errors.append("adapter_protocol_sha256_mismatch")
    if protocol.get("status") != (
        "unified_route_mapping_frozen_before_next_native_forward_packet"
    ):
        errors.append("adapter_protocol_status_invalid")
    for name, binding in (protocol.get("bindings") or {}).items():
        path = (PROJECT_ROOT / str(binding.get("path") or "")).resolve()
        try:
            path.relative_to(PROJECT_ROOT.resolve())
        except ValueError:
            errors.append(f"adapter_protocol_binding_outside_project:{name}")
            continue
        if not path.is_file() or _sha(path) != binding.get("sha256"):
            errors.append(f"adapter_protocol_binding_invalid:{name}")
    return protocol, errors


def _path(value: object, *, project_root: Path) -> Path:
    return research_artifact_path(value, project_root=project_root)


def _load_inputs(
    input_paths: dict[str, str | Path], *, project_root: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, str]], list[str]]:
    if set(input_paths) != set(_SPECS):
        return {}, {}, ["required_input_path_set_mismatch"]
    packets: dict[str, dict[str, Any]] = {}
    refs: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    for name, verifier in _SPECS.items():
        try:
            path = _path(input_paths[name], project_root=project_root)
            checked = verifier(path, project_root=project_root)
            packet = checked.get("packet") if isinstance(checked, dict) else None
            if checked.get("valid") is not True or not isinstance(packet, dict):
                errors.append(f"{name}:dedicated_verifier_failed")
                continue
            packets[name] = packet
            refs[name] = {
                "path": str(path.relative_to(project_root.resolve())),
                "sha256": _sha(path),
                "packet_id": str(packet.get("packet_id") or ""),
            }
        except (OSError, TypeError, ValueError):
            errors.append(f"{name}:input_unreadable_or_outside_project")
    return packets, refs, errors


def _core(value: object) -> dict[str, Any]:
    row = value if isinstance(value, dict) else {}
    return {
        "path": row.get("path"), "sha256": row.get("sha256"),
        "packet_id": row.get("packet_id"),
    }


def _input_errors(
    packets: dict[str, dict[str, Any]], refs: dict[str, dict[str, str]],
    protocol: dict[str, Any],
) -> tuple[str | None, str | None, datetime | None, list[str]]:
    errors: list[str] = []
    identities = {
        (packet.get("decision_day"), packet.get("slot_id"))
        for packet in packets.values()
    }
    if len(identities) != 1:
        errors.append("decision_day_or_slot_mismatch")
        decision_day, slot_id = None, None
    else:
        decision_day, slot_id = next(iter(identities))
    if any(packet.get("no_backfill") is not True for packet in packets.values()):
        errors.append("no_backfill_required")
    pairs = (
        (packets["market_sector"].get("h1_packet"), refs["H1_market_state"], "sector_to_H1"),
        (packets["shadow_decision"].get("raw_input_packet"), refs["market_sector"], "shadow_to_sector"),
        (packets["sector_constituents"].get("shadow_decision"), refs["shadow_decision"], "constituents_to_shadow"),
    )
    for left, right, label in pairs:
        if _core(left) != _core(right):
            errors.append(f"cross_binding_mismatch:{label}")
    prerequisite_at = _time(packets["H1_market_state"].get("decision_at"))
    synchronous_receipts = {
        "market_sector": _time(packets["market_sector"].get("captured_at")),
        "shadow_decision": _time(packets["shadow_decision"].get("decision_at")),
        "sector_constituents": _time(packets["sector_constituents"].get("captured_at")),
        "intraday_emotion": _time(packets["intraday_emotion"].get("captured_at")),
        "account_snapshot": _time(packets["account_snapshot"].get("observed_at")),
    }
    receipts = {"H1_market_state": prerequisite_at, **synchronous_receipts}
    if any(value is None for value in receipts.values()):
        errors.append("native_receipt_time_missing")
        final_at = None
    else:
        final_at = max(value for value in receipts.values() if value is not None)
        if (
            capture_slot(final_at) != slot_id
            or any(value.date().isoformat() != decision_day for value in receipts.values() if value)
            or any(
                capture_slot(value) != slot_id
                for value in synchronous_receipts.values() if value
            )
        ):
            errors.append("native_receipt_outside_original_slot")
        try:
            h1_slots = _bound_schedule(protocol, "h1_native_schedule", "decision_schedule")
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            h1_slots = None
        if not _slot_contains(prerequisite_at, str(slot_id), h1_slots):
            errors.append("prerequisite_receipt_outside_native_slot")
        market_sector_at = synchronous_receipts["market_sector"]
        if prerequisite_at > market_sector_at:
            errors.append("prerequisite_receipt_after_dependent_capture")
    allowed_label_paths = (
        protocol.get("descriptive_label_contract") or {}
    ).get("allowed_paths") or []
    for path, child in _walk(packets):
        lowered = path[-1].lower()
        if lowered in _OUTCOME_KEYS:
            errors.append(f"outcome_field_forbidden:{lowered}")
        if lowered == "label":
            if not any(
                _path_matches(str(pattern), path)
                for pattern in allowed_label_paths
            ):
                errors.append("descriptive_label_path_forbidden")
            elif not isinstance(child, str) or not child.strip():
                errors.append("descriptive_label_value_invalid")
        if lowered in _OUTCOME_FLAGS and child is not False:
            errors.append(f"outcome_boundary_invalid:{lowered}")
        if lowered == "authority" and (
            not isinstance(child, dict)
            or child.get("research_only") is not True
            or any(value is not False for name, value in child.items() if name != "research_only")
        ):
            errors.append("unsafe_authority")
    return str(decision_day) if decision_day else None, str(slot_id) if slot_id else None, final_at, list(dict.fromkeys(errors))


def _market_input(
    h1: dict[str, Any], *, project_root: Path, decision_day: str, slot_id: str,
) -> tuple[dict[str, Any], str]:
    row = _bound_feature_row(h1, project_root=project_root)
    features = row.get("features") or {}
    percentiles = {
        name: (
            round(float(features[f"{name}_percentile"]) * 100, 3)
            if _finite(features.get(f"{name}_percentile")) is not None else None
        )
        for name in REQUIRED_PERCENTILES
    }
    source_as_of = str(row.get("trade_date") or "")
    value = {
        "decision_day": decision_day, "slot_id": slot_id,
        "source_as_of": f"{source_as_of}T15:00:00+08:00",
        "available_at": h1.get("decision_at"), "no_backfill": True,
        "status": "audited_diagnostic_not_validated",
        "state": features.get("confirmed_state"),
        "raw_state": features.get("raw_state"),
        "profitability_tested": False,
        "applied_to_live_positions": False,
        "percentiles_pct": percentiles,
    }
    return value, source_as_of


def _sector_input(
    packet: dict[str, Any], *, decision_day: str, slot_id: str,
) -> dict[str, Any]:
    source = packet.get("sector_source") or {}
    rows = packet.get("sector_rows") or []
    return {
        "decision_day": decision_day, "slot_id": slot_id,
        "source_as_of": source.get("source_data_as_of"),
        "available_at": packet.get("captured_at"), "no_backfill": True,
        "status": source.get("source_status"),
        "health_status": source.get("source_health_status"),
        "data_as_of": source.get("source_data_as_of"),
        "meta": copy.deepcopy(source.get("meta") or {}),
        "industries": [copy.deepcopy(row) for row in rows if row.get("kind") == "industry"],
        "concepts": [copy.deepcopy(row) for row in rows if row.get("kind") == "concept"],
    }


def _constituent_input(
    packet: dict[str, Any], *, decision_day: str, slot_id: str,
) -> dict[str, Any]:
    value = copy.deepcopy(packet)
    times = [
        _time(row.get("data_as_of"))
        for capture in packet.get("sector_captures") or []
        for row in (capture.get("rows") or []) if isinstance(row, dict)
    ]
    source_at = max((time for time in times if time is not None), default=None)
    value.update({
        "decision_day": decision_day, "slot_id": slot_id,
        "source_as_of": (
            source_at.isoformat(timespec="seconds") if source_at
            else packet.get("captured_at")
        ),
        "available_at": packet.get("captured_at"), "no_backfill": True,
    })
    return value


def _account_input(
    packet: dict[str, Any], *, decision_day: str, slot_id: str,
) -> dict[str, Any]:
    portfolio = copy.deepcopy(packet.get("portfolio") or {})
    positions = []
    for raw in portfolio.get("positions") or []:
        row = copy.deepcopy(raw)
        row["available_to_sell_shares"] = row.get(
            "available_to_sell_shares", row.get("sellable_shares_snapshot")
        )
        positions.append(row)
    return {
        **portfolio,
        "decision_day": decision_day, "slot_id": slot_id,
        # The replay driver's source clock is the complete account valuation
        # observation.  The user's unchanged cash provenance remains separate
        # in ``cash_source_as_of`` and is never rewritten to this receipt time.
        "source_as_of": packet.get("observed_at"),
        "available_at": packet.get("first_seen_at"), "no_backfill": True,
        "equity": portfolio.get("account_equity"),
        "positions": positions,
        "account_snapshot_sha256": packet.get(
            "source_account_identity_sha256"
        ),
    }


def _apply_terminal_playbook_retirement(
    decision: dict[str, Any], protocol: dict[str, Any],
) -> dict[str, Any]:
    """Remove a terminally failed entry implementation from strict research.

    The frozen replay remains useful as a market-state diagnostic, but its
    failed entry implementation must not keep producing candidate stocks or
    planned quantities in later forward packets.
    """
    contract = protocol.get("terminal_playbook_retirement") or {}
    retired = {
        str(value) for value in contract.get("retired_entry_playbooks") or []
        if str(value)
    }
    if not retired:
        return decision
    updated = copy.deepcopy(decision)
    budget = updated.get("market_budget") or {}
    eligible = [str(value) for value in budget.get("eligible_playbooks") or []]
    removed = [value for value in eligible if value in retired]
    if not removed:
        return updated
    risk_only = {
        str(value) for value in contract.get("risk_only_playbooks") or []
        if str(value)
    }
    retained = [value for value in eligible if value not in retired]
    entry_remaining = [value for value in retained if value not in risk_only]
    budget.update({
        "maximum_new_trades": 0,
        "eligible_playbooks": retained,
        "entry_playbook_available": bool(entry_remaining),
        "terminally_retired_playbooks": removed,
    })
    updated["market_budget"] = budget

    route_budget = ((updated.get("route") or {}).get(
        "research_budget_hypothesis"
    ) or {})
    route_budget["maximum_new_trades"] = 0
    route_budget["allowed_playbooks"] = [
        row for row in route_budget.get("allowed_playbooks") or []
        if str(row.get("id") if isinstance(row, dict) else row) not in retired
    ]

    candidates = updated.get("candidates") or {}
    withheld_count = len(candidates.get("selected") or [])
    candidates.update({
        "status": "terminal_playbook_retired_no_entry_candidate",
        "selected": [],
        "selected_count": 0,
    })
    updated["candidates"] = candidates

    plan = updated.get("account_plan") or {}
    plan.update({
        "status": "research_active_no_admitted_entry_playbook",
        "maximum_new_trades": 0,
        "candidate_contract_ready": False,
        "pre_execution_plans": [],
        "research_orders": [],
        "executed_orders": [],
        "new_buy_capacity_yuan": 0.0,
        "remaining_unallocated_buy_capacity_yuan": 0.0,
        "research_hold_reason": "terminal_entry_playbook_retired",
    })
    for step in plan.get("causal_steps") or []:
        if step.get("name") == "maximum_new_trades":
            step["value"] = 0
        elif step.get("name") == "eligible_playbooks":
            step["value"] = retained
        elif step.get("name") == "factor_and_stock_candidates":
            step["value"] = {
                "status": "terminal_entry_playbook_retired",
                "stock_codes": [],
            }
    updated["account_plan"] = plan
    updated.update({
        "status": "research_no_trade",
        "active_no_trade": True,
        "terminal_playbook_retirement": {
            "status": "applied",
            "retired_entry_playbooks": removed,
            "retained_risk_playbooks": [
                value for value in retained if value in risk_only
            ],
            "withheld_candidate_count": withheld_count,
            "source": contract.get("source"),
            "historical_packet_rewritten": False,
            "returns_or_labels_read": False,
        },
    })
    updated.pop("replay_id", None)
    updated["replay_id"] = hashlib.sha256(_canonical_bytes(updated)).hexdigest()
    return updated


def adapt_system_route_forward_packets(
    input_paths: dict[str, str | Path], *, mode: str = "strict_forward",
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Verify, normalize and recompute one V2 research decision; never persist it."""
    authority = _authority()
    base = {
        "schema": "system_route_joint_v2_adapter_result.v2",
        "status": "blocked", "valid": False, "errors": [],
        "mode": mode, "decision_at": None, "shadow_decision_at": None,
        "input_refs": {}, "normalized_inputs_sha256": {}, "decision": None,
        "returns_or_labels_read": False, "execution_performed": False,
        "performance_computed": False, "authority": authority,
    }
    if mode != "strict_forward":
        base["errors"] = ["historical_or_nonforward_mode_forbidden"]
        return base
    protocol, errors = _protocol()
    packets, refs, load_errors = _load_inputs(input_paths, project_root=project_root)
    errors.extend(load_errors)
    if errors or len(packets) != len(_SPECS):
        base["errors"] = list(dict.fromkeys(errors or ["verified_inputs_incomplete"]))
        return base
    decision_day, slot_id, final_at, input_errors = _input_errors(
        packets, refs, protocol,
    )
    if (
        input_errors or decision_day is None or slot_id is None or final_at is None
        or decision_day < str(protocol.get("forward_start") or "")
    ):
        if decision_day and decision_day < str(protocol.get("forward_start") or ""):
            input_errors.append("before_frozen_forward_start")
        base["errors"] = list(dict.fromkeys(input_errors))
        return base
    try:
        market, market_source_day = _market_input(
            packets["H1_market_state"], project_root=project_root,
            decision_day=decision_day, slot_id=slot_id,
        )
        sector = _sector_input(
            packets["market_sector"], decision_day=decision_day, slot_id=slot_id,
        )
        constituents = _constituent_input(
            packets["sector_constituents"], decision_day=decision_day, slot_id=slot_id,
        )
        account = _account_input(
            packets["account_snapshot"], decision_day=decision_day, slot_id=slot_id,
        )
        market_ready = adapt_market_observation(market, as_of=market_source_day)
        replay_raw = REPLAY_PROTOCOL_PATH.read_bytes()
        if hashlib.sha256(replay_raw).hexdigest() != REPLAY_PROTOCOL_SHA256:
            raise ValueError("replay_protocol_identity_invalid")
        replay_protocol = json.loads(replay_raw)
        state = str(market_ready.get("state") or "unknown")
        policy = (replay_protocol.get("portfolio_router") or {}).get("states", {}).get(state)
        if not isinstance(policy, dict):
            raise ValueError("replay_state_policy_missing")
        emotion_packet = packets["intraday_emotion"]
        captured_at = _time(emotion_packet.get("captured_at"))
        emotion = build_intraday_emotion_observation(
            packets["shadow_decision"].get("route") or {},
            {"breadth": emotion_packet.get("raw_observation") or {}},
            now=captured_at,
        )
        confirmed = emotion.get("confirmation") == "confirmed"
        emotion["research_budget_hypothesis"] = {
            "status": "confirmed_base_route" if confirmed else "hold_new_trades",
            "maximum_new_trades": int(policy["maximum_new_trades"]) if confirmed else 0,
            "target_exposure_range_pct": [
                policy["target_exposure_pct"], policy["target_exposure_pct"],
            ],
            "existing_position_risk_reduction_allowed": True,
            "applied_to_execution": False,
        }
        summary_prefix, separator, _old_budget = str(
            emotion.get("summary") or ""
        ).partition(" · 研究")
        if not separator:
            raise ValueError("intraday_emotion_summary_contract_invalid")
        budget = emotion["research_budget_hypothesis"]
        budget_summary = (
            f"研究新增≤{budget['maximum_new_trades']}笔/"
            f"仓位{policy['target_exposure_pct']}–{policy['target_exposure_pct']}%"
            if confirmed else "研究不开新仓"
        )
        emotion["summary"] = f"{summary_prefix} · {budget_summary}"
        emotion.update({
            "decision_day": decision_day, "slot_id": slot_id,
            "source_as_of": (emotion_packet.get("raw_observation") or {}).get("as_of"),
            "available_at": emotion_packet.get("captured_at"), "no_backfill": True,
        })
        normalized = {
            "market": market, "sector": sector, "constituents": constituents,
            "account": account, "intraday_emotion": emotion,
        }
        decision = build_system_route_replay(
            decision_at=final_at,
            market_observation=market, sector_snapshot=sector,
            constituent_packet=constituents, account=account,
            intraday_emotion=emotion,
            retired_entry_playbooks=tuple((protocol.get("terminal_playbook_retirement") or {}).get("retired_entry_playbooks") or []),
        )
        decision = _apply_terminal_playbook_retirement(decision, protocol)
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        base["errors"] = [f"normalization_failed:{type(exc).__name__}:{exc}"]
        return base
    base.update({
        "status": "adapted" if decision.get("status") != "blocked" else "blocked",
        "valid": decision.get("status") != "blocked",
        "errors": list(decision.get("blocked_reasons") or []) if decision.get("status") == "blocked" else [],
        "decision_at": final_at.isoformat(timespec="seconds"),
        "shadow_decision_at": packets["shadow_decision"].get("decision_at"),
        "input_refs": refs,
        "normalized_inputs_sha256": {
            name: hashlib.sha256(_canonical_bytes(value)).hexdigest()
            for name, value in normalized.items()
        },
        "decision": decision,
        "adapter_protocol": {
            "path": str(ADAPTER_PROTOCOL_PATH.relative_to(PROJECT_ROOT)),
            "sha256": ADAPTER_PROTOCOL_SHA256,
        },
    })
    identity = copy.deepcopy(base)
    base["adapter_id"] = hashlib.sha256(_canonical_bytes(identity)).hexdigest()
    return base


__all__ = [
    "ADAPTER_PROTOCOL_PATH", "ADAPTER_PROTOCOL_SHA256",
    "adapt_system_route_forward_packets",
]
