"""Research-only adapter from verified paper execution to strict joint replay."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from quantqc import ashare

from .market_state_daily import PROJECT_ROOT, _atomic_publish, _canonical_bytes, _sha256
from .strict_joint_strategy_replay import run_strict_joint_replay
from .verified_portfolio_execution_forward import verify_forward_execution_packet


PROTOCOL_PATH = (
    PROJECT_ROOT
    / "research/STRICT_JOINT_FORWARD_ADAPTER_PROTOCOL_V1_AMENDMENT_1.json"
)
PROTOCOL_SHA256 = "6e9c2532acbcf9477bdcbb2ef55eab719dd51dc65732a272a950793980a8205a"
AUDIT_DIR = Path("research/data/strict_joint_strategy_replay/v1/audit_results")
STATE_DIR = Path("research/data/strict_joint_strategy_replay/v1/state_inputs")
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
) -> tuple[dict[str, Any], dict[str, str]]:
    normalized = _normalize_ref(value, project_root=project_root, label=label)
    path = _project_path(normalized["path"], project_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}:json_invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label}:object_required")
    return payload, normalized


def _normalize_ref(
    value: object, *, project_root: Path, label: str,
) -> dict[str, str]:
    row = value if isinstance(value, dict) else {}
    try:
        path = _project_path(row.get("path"), project_root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label}:path_invalid") from exc
    expected = str(row.get("sha256") or "")
    if not path.is_file() or not expected or _sha256(path) != expected:
        raise ValueError(f"{label}:sha256_invalid")
    return _ref(path, project_root)


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_time(
    value: object, label: str, *, allow_date_only: bool = False,
) -> str:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label}:time_invalid") from exc
    if parsed.tzinfo is None and not (
        allow_date_only and text == parsed.date().isoformat()
    ):
        raise ValueError(f"{label}:timezone_required")
    return text


def _verify_protocol(
    *, project_root: Path, protocol_path: Path,
    expected_sha256: str | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    try:
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("adapter_protocol_unreadable") from exc
    if not isinstance(protocol, dict):
        raise ValueError("adapter_protocol_object_required")
    actual_sha = _sha256(protocol_path)
    if expected_sha256 and actual_sha != expected_sha256:
        raise ValueError("adapter_protocol_sha256_mismatch")
    if protocol.get("status") != "frozen_before_first_real_strict_joint_forward_audit":
        raise ValueError("adapter_protocol_status_invalid")
    bindings = protocol.get("bindings")
    if not isinstance(bindings, dict) or "playbook_library" not in bindings:
        raise ValueError("adapter_protocol_bindings_invalid")
    for name, binding in bindings.items():
        try:
            path = _project_path((binding or {}).get("path"), project_root)
        except (OSError, ValueError) as exc:
            raise ValueError(f"adapter_protocol_binding_invalid:{name}") from exc
        if not path.is_file() or _sha256(path) != (binding or {}).get("sha256"):
            raise ValueError(f"adapter_protocol_binding_invalid:{name}")
    authority = protocol.get("authority") or {}
    if authority.get("research_only") is not True or any(
        value is not False for key, value in authority.items()
        if key != "research_only"
    ):
        raise ValueError("adapter_protocol_authority_invalid")
    return protocol, _ref(protocol_path, project_root)


def _strict_packet(
    schema: str, *, decision_day: str, slot_id: str, decision_at: str,
    source_as_of: str, frozen_ref: dict[str, str], **values: Any,
) -> dict[str, Any]:
    packet = {
        "schema": schema,
        "decision_day": decision_day,
        "slot_id": slot_id,
        "decision_at": decision_at,
        "source_as_of": source_as_of,
        "no_backfill": True,
        "frozen_ref": copy.deepcopy(frozen_ref),
        "returns_or_labels_read": False,
        **values,
    }
    packet["packet_id"] = _identity(packet, "packet_id")
    return packet


def _load_bound_packet(
    owner: dict[str, Any], name: str, *, project_root: Path,
) -> tuple[dict[str, Any], dict[str, str]]:
    packet, ref = _load_ref(owner.get(name), project_root=project_root, label=name)
    expected_id = (owner.get(name) or {}).get("packet_id")
    if expected_id and packet.get("packet_id") != expected_id:
        raise ValueError(f"{name}:packet_id_mismatch")
    return packet, ref


def _state_sidecar(
    state: dict[str, Any], *, path: Path, project_root: Path,
) -> dict[str, str]:
    normalized = copy.deepcopy(state)
    normalized["state_id"] = _identity(normalized, "state_id")
    payload = _canonical_bytes(normalized)
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError("state_sidecar_first_write_conflict")
    else:
        _atomic_publish(path, payload)
    return _ref(path, project_root)


def _normalize_exit_plans(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("risk_reduction_orders_invalid")
    result: list[dict[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            raise ValueError(f"risk_reduction_order_invalid:{index}")
        result.append({
            "plan_id": row.get("order_id") or row.get("plan_id"),
            "order_id": row.get("order_id") or row.get("plan_id"),
            "stock_code": row.get("stock_code"),
            "side": "sell",
            "shares": row.get("requested_shares", row.get("shares")),
            "limit_price_yuan": row.get("limit_price_yuan"),
            "reason": row.get("reason"),
            "full_liquidation": row.get("full_liquidation"),
        })
    return result


def _market_day(
    source: dict[str, Any], *, decision_day: str,
    liquidity_ref: dict[str, str],
) -> tuple[dict[str, Any], str]:
    trade_date = str(source.get("trade_date") or "")
    if source.get("previous_trade_date") != decision_day or trade_date <= decision_day:
        raise ValueError("execution_market_date_chain_invalid")
    captured_at = _parse_time(source.get("captured_at"), "execution_capture")
    rows = source.get("quotes")
    if not isinstance(rows, dict):
        raise ValueError("execution_quotes_invalid")
    quotes: dict[str, dict[str, Any]] = {}
    for code, raw in rows.items():
        if not isinstance(raw, dict):
            raise ValueError(f"execution_quote_invalid:{code}")
        source_as_of = _parse_time(
            raw.get("exchange_data_time"), f"execution_quote:{code}",
        )
        risk_warning = raw.get("is_risk_warning")
        no_price_limit = raw.get("no_price_limit")
        tradable = raw.get("tradable")
        if not all(isinstance(value, bool) for value in (
            risk_warning, no_price_limit, tradable,
        )):
            raise ValueError(f"execution_quote_identity_incomplete:{code}")
        open_price = _finite(raw.get("open_price"))
        previous_close = _finite(raw.get("prev_close"))
        lagged_adv = _finite(raw.get("lagged_adv_yuan"))
        if any(value is None or value <= 0 for value in (
            open_price, previous_close, lagged_adv,
        )):
            raise ValueError(f"execution_quote_values_invalid:{code}")
        limit_up = ashare.opening_auction_executability(
            code=str(code), trade_date=trade_date, side="buy",
            open_price=open_price, prev_close=previous_close,
            tradable=True, is_risk_warning=risk_warning,
            no_price_limit=no_price_limit,
        ).get("limit_price")
        limit_down = ashare.opening_auction_executability(
            code=str(code), trade_date=trade_date, side="sell",
            open_price=open_price, prev_close=previous_close,
            tradable=True, is_risk_warning=risk_warning,
            no_price_limit=no_price_limit,
        ).get("limit_price")
        quotes[str(code)] = {
            "source_as_of": source_as_of,
            "open_price": open_price,
            "prev_close": previous_close,
            "lagged_adv_yuan": lagged_adv,
            "lagged_adv_as_of": decision_day,
            "liquidity_model_sha256": liquidity_ref["sha256"],
            "tradable": tradable,
            "is_risk_warning": risk_warning,
            "no_price_limit": no_price_limit,
            "effective_limit_up_price": limit_up,
            "effective_limit_down_price": limit_down,
        }
    market = {
        "trade_date": trade_date,
        "previous_trade_date": decision_day,
        "no_backfill": True,
        "quotes": quotes,
    }
    market["market_snapshot_id"] = _identity(market, "market_snapshot_id")
    return market, captured_at


def _build_bundle(
    execution: dict[str, Any], execution_ref: dict[str, str],
    protocol: dict[str, Any], *, project_root: Path, state_dir: Path,
) -> tuple[dict[str, Any], dict[str, str]]:
    wrapper = execution.get("verified_wrapper")
    if (
        execution.get("schema") != "unified_regime_route_execution_day.v1"
        or execution.get("no_backfill") is not True
        or not isinstance(wrapper, dict)
        or wrapper.get("schema") != "verified_portfolio_execution.a6"
        or wrapper.get("research_only") is not True
    ):
        raise ValueError("verified_execution_contract_invalid")
    refs = wrapper.get("decision_refs")
    selected_slot = str(wrapper.get("selected_slot_id") or "")
    selected_id = str(wrapper.get("selected_source_packet_id") or "")
    if not isinstance(refs, list) or not selected_slot or not selected_id:
        raise ValueError("selected_joint_decision_identity_missing")
    joint = joint_ref = None
    for index, value in enumerate(refs):
        candidate, normalized_ref = _load_ref(
            value, project_root=project_root, label=f"decision_ref:{index}",
        )
        if candidate.get("packet_id") == selected_id:
            joint, joint_ref = candidate, normalized_ref
    if joint is None or joint_ref is None or joint.get("slot_id") != selected_slot:
        raise ValueError("selected_joint_decision_not_found")
    decision_day = str(joint.get("decision_day") or "")
    slot_id = str(joint.get("slot_id") or "")
    if not decision_day or not slot_id:
        raise ValueError("joint_decision_identity_invalid")
    candidate, candidate_ref = _load_bound_packet(
        joint, "candidate_packet", project_root=project_root,
    )
    shadow, shadow_ref = _load_bound_packet(
        joint, "shadow_decision", project_root=project_root,
    )
    if any(
        packet.get("decision_day") != decision_day
        or packet.get("slot_id") != slot_id
        for packet in (candidate, shadow)
    ):
        raise ValueError("upstream_decision_identity_mismatch")
    decision_at = _parse_time(shadow.get("decision_at"), "shadow_decision")
    route = shadow.get("route")
    research = candidate.get("research_result")
    decision = joint.get("decision")
    budgeted = wrapper.get("budgeted_decision_packet")
    budgeted_decision = (
        budgeted.get("decision") if isinstance(budgeted, dict) else None
    )
    if not all(isinstance(value, dict) for value in (
        route, research, decision, budgeted, budgeted_decision,
    )):
        raise ValueError("route_candidate_or_decision_missing")
    if (
        budgeted.get("verified_source_packet_id") != joint.get("packet_id")
        or budgeted.get("packet_id") != _identity(budgeted, "packet_id")
    ):
        raise ValueError("budgeted_decision_identity_mismatch")
    targets = [
        _finite(decision.get("target_exposure_pct")),
        _finite(budgeted_decision.get("target_exposure_pct")),
        _finite(wrapper.get("target_exposure_pct")),
    ]
    if any(value is None for value in targets) or len(set(targets)) != 1:
        raise ValueError("target_exposure_cross_check_failed")
    maximum_new_trades = budgeted_decision.get("maximum_new_trades")
    if (
        isinstance(maximum_new_trades, bool)
        or not isinstance(maximum_new_trades, int)
        or maximum_new_trades < 0
    ):
        raise ValueError("budgeted_maximum_new_trades_invalid")
    entries = budgeted_decision.get("pre_execution_plans")
    if not isinstance(entries, list):
        raise ValueError("budgeted_pre_execution_plans_invalid")
    exits = _normalize_exit_plans(wrapper.get("risk_reduction_orders"))
    if entries and exits:
        raise ValueError("mixed_entry_and_risk_reduction_unsupported")
    market_states = {
        str(route.get("market_state") or ""),
        str(research.get("market_state") or ""),
        str(decision.get("market_state") or ""),
    }
    if "" in market_states or len(market_states) != 1:
        raise ValueError("market_state_cross_check_failed")
    candidate_playbook_id = str(research.get("playbook_id") or "")
    eligible_playbooks = decision.get("eligible_playbooks")
    if not isinstance(eligible_playbooks, list) or any(
        not isinstance(value, str) for value in eligible_playbooks
    ):
        raise ValueError("eligible_playbooks_invalid")
    if entries:
        if candidate_playbook_id not in eligible_playbooks:
            raise ValueError("entry_playbook_not_prefrozen_as_eligible")
        playbook_id = candidate_playbook_id
    elif exits:
        if "retreat_defense" not in eligible_playbooks:
            raise ValueError("risk_exit_without_prefrozen_retreat_playbook")
        playbook_id = "retreat_defense"
    elif candidate_playbook_id in eligible_playbooks:
        playbook_id = candidate_playbook_id
    elif eligible_playbooks == ["retreat_defense"]:
        playbook_id = "retreat_defense"
    else:
        raise ValueError("no_unique_prefrozen_playbook_for_no_trade_day")
    library_ref = protocol["bindings"]["playbook_library"]
    library, library_ref = _load_ref(
        library_ref, project_root=project_root, label="playbook_library",
    )
    playbook_rows = {
        str(row.get("id") or ""): row
        for row in (library.get("playbooks") or [])
        if isinstance(row, dict)
    }
    if not playbook_id or playbook_id not in playbook_rows:
        raise ValueError("fixed_playbook_not_in_frozen_library")
    playbook_kind = playbook_rows[playbook_id].get("kind")
    if playbook_kind not in {
        "research_entry_playbook", "research_risk_playbook",
    }:
        raise ValueError("fixed_playbook_kind_invalid")
    if entries and playbook_kind != "research_entry_playbook":
        raise ValueError("entry_plan_with_non_entry_playbook")
    if exits and playbook_kind != "research_risk_playbook":
        raise ValueError("risk_exit_with_non_risk_playbook")
    if research.get("named_person_signal_used") is not False:
        raise ValueError("named_person_signal_forbidden")
    market_input = shadow.get("market_input")
    sector_input = route.get("sector_confirmation")
    if not isinstance(market_input, dict) or not isinstance(sector_input, dict):
        raise ValueError("market_or_sector_input_missing")
    market_state_as_of = _parse_time(
        route.get("market_state_as_of"), "market_state_source",
        allow_date_only=True,
    )
    playbook_requires_sector = playbook_kind == "research_entry_playbook"
    route_requires_sector = sector_input.get("required")
    if not isinstance(route_requires_sector, bool):
        raise ValueError("sector_requirement_identity_missing")
    if playbook_requires_sector:
        sector_source_as_of = _parse_time(
            sector_input.get("source_as_of"), "sector_source",
        )
        sector_status = (
            "confirmed" if route_requires_sector
            and sector_input.get("ready") is True else "unconfirmed"
        )
    else:
        sector_source_as_of = decision_at
        sector_status = "not_required_by_frozen_playbook"
    market = _strict_packet(
        "strict_pit_market_state.v1", decision_day=decision_day,
        slot_id=slot_id, decision_at=decision_at,
        source_as_of=market_state_as_of, frozen_ref=shadow_ref,
        market_state=route.get("market_state"),
        completed_inputs_only=market_input.get("completed_data_only") is True,
    )
    budget = _strict_packet(
        "strict_pit_exposure_budget.v1", decision_day=decision_day,
        slot_id=slot_id, decision_at=decision_at,
        source_as_of=decision_at, frozen_ref=execution_ref,
        market_state_packet_id=market["packet_id"],
        target_exposure_pct=targets[0],
        maximum_new_trades=maximum_new_trades,
    )
    sector = _strict_packet(
        "strict_pit_sector_confirmation.v1", decision_day=decision_day,
        slot_id=slot_id, decision_at=decision_at,
        source_as_of=sector_source_as_of, frozen_ref=shadow_ref,
        market_state_packet_id=market["packet_id"],
        exposure_budget_packet_id=budget["packet_id"], status=sector_status,
    )
    playbook = _strict_packet(
        "strict_pit_fixed_playbook.v1", decision_day=decision_day,
        slot_id=slot_id, decision_at=decision_at,
        source_as_of=decision_at, frozen_ref=library_ref,
        sector_confirmation_packet_id=sector["packet_id"],
        playbook_id=playbook_id,
        sector_confirmation_required=playbook_requires_sector,
        selected_before_outcomes=True, named_person_signal_used=False,
        allows_new_entries=playbook_kind == "research_entry_playbook",
    )
    formula_ref = candidate.get("formula")
    _formula, formula_ref = _load_ref(
        formula_ref, project_root=project_root, label="candidate_formula",
    )
    stock = _strict_packet(
        "strict_pit_stock_candidate.v1", decision_day=decision_day,
        slot_id=slot_id, decision_at=decision_at,
        source_as_of=decision_at, frozen_ref=execution_ref,
        fixed_playbook_packet_id=playbook["packet_id"],
        playbook_id=playbook_id, formula_ref=formula_ref,
        named_person_signal_used=False, selected_before_outcomes=True,
        factor_evidence=copy.deepcopy(research.get("factor_evidence") or {}),
        entry_plans=copy.deepcopy(entries),
        exit_plans=exits,
    )
    state = wrapper.get("execution_input_state")
    if not isinstance(state, dict) or state.get("last_trade_date") != decision_day:
        raise ValueError("execution_input_state_invalid")
    if state.get("pending_exit_orders"):
        raise ValueError("existing_pending_exit_orders_unsupported_by_strict_v1")
    cost_ref = _normalize_ref(
        wrapper.get("cost_model_ref"), project_root=project_root,
        label="cost_model",
    )
    frozen_cost_ref = _normalize_ref(
        protocol["bindings"]["cost_model"], project_root=project_root,
        label="frozen_cost_model",
    )
    if cost_ref != frozen_cost_ref:
        raise ValueError("cost_model_not_equal_to_frozen_protocol")
    liquidity_ref = _normalize_ref(
        protocol["bindings"]["joint_execution_engine"],
        project_root=project_root,
        label="liquidity_model",
    )
    market_day, captured_at = _market_day(
        wrapper.get("market_snapshot") or {}, decision_day=decision_day,
        liquidity_ref=liquidity_ref,
    )
    plan_codes = {
        str(row.get("stock_code") or "") for row in entries + exits
        if isinstance(row, dict)
    }
    relevant_codes = set((state.get("positions") or {}).keys()) | plan_codes
    for code in sorted(relevant_codes):
        quote = (market_day.get("quotes") or {}).get(code) or {}
        if quote.get("tradable") is not True:
            raise ValueError(f"held_or_ordered_untradable_unsupported:{code}")
        if quote.get("no_price_limit") is True:
            raise ValueError(f"no_price_limit_day_unsupported:{code}")
    config = wrapper.get("execution_config")
    if not isinstance(config, dict):
        raise ValueError("execution_config_invalid")
    state_path = project_root / state_dir / f"{decision_day}-{slot_id}.json"
    state_ref = _state_sidecar(
        state, path=state_path, project_root=project_root,
    )
    execution_packet = _strict_packet(
        "strict_pit_A_share_execution_cost.v1", decision_day=decision_day,
        slot_id=slot_id, decision_at=decision_at,
        source_as_of=captured_at, frozen_ref=execution_ref,
        stock_candidate_packet_id=stock["packet_id"],
        previous_state_ref=state_ref, cost_model_ref=cost_ref,
        liquidity_model_ref=liquidity_ref, market_day=market_day,
        execution_config=copy.deepcopy(config),
        execution_config_sha256=hashlib.sha256(
            _canonical_bytes(config)
        ).hexdigest(),
    )
    bundle = {
        "schema": "strict_joint_strategy_portfolio_replay_input.v1",
        "mode": "forward_replay",
        "decision_day": decision_day,
        "slot_id": slot_id,
        "decision_at": decision_at,
        "no_backfill": True,
        "returns_or_labels_read": False,
        "packets": {
            "market_state": market,
            "exposure_budget": budget,
            "sector_confirmation": sector,
            "fixed_playbook": playbook,
            "stock_candidate": stock,
            "A_share_execution_cost": execution_packet,
        },
    }
    bundle["bundle_id"] = _identity(bundle, "bundle_id")
    return bundle, state_ref


def _economic_position(value: object) -> dict[str, Any]:
    row = value if isinstance(value, dict) else {}
    locked = [
        {
            "buy_trade_date": item.get("buy_trade_date"),
            "shares": item.get("shares"),
        }
        for item in (row.get("locked_buys") or []) if isinstance(item, dict)
    ]
    return {
        "shares_total": row.get("shares_total"),
        "sellable_shares": row.get("sellable_shares"),
        "locked_buys": sorted(
            locked, key=lambda item: (
                str(item.get("buy_trade_date") or ""), item.get("shares") or 0,
            ),
        ),
    }


def _economic_state(value: object) -> dict[str, Any]:
    state = value if isinstance(value, dict) else {}
    positions = state.get("positions") if isinstance(state.get("positions"), dict) else {}
    pending = []
    for row in state.get("pending_exit_orders") or []:
        if isinstance(row, dict):
            pending.append({
                "stock_code": row.get("stock_code"), "side": row.get("side"),
                "requested_shares": row.get("requested_shares"),
                "limit_price_yuan": row.get("limit_price_yuan"),
                "valid_trade_date": row.get("valid_trade_date"),
                "reason": row.get("reason"),
                "full_liquidation": row.get("full_liquidation"),
            })
    return {
        "last_trade_date": state.get("last_trade_date"),
        "cash_yuan": state.get("cash_yuan"),
        "positions": {
            str(code): _economic_position(row)
            for code, row in sorted(positions.items())
        },
        "pending_exit_orders": sorted(
            pending, key=lambda row: (
                str(row.get("stock_code") or ""),
                str(row.get("side") or ""),
                row.get("requested_shares") or 0,
            ),
        ),
        "cumulative_costs": copy.deepcopy(state.get("cumulative_costs")),
        "cumulative_buy_notional": state.get("cumulative_buy_notional"),
        "cumulative_sell_notional": state.get("cumulative_sell_notional"),
    }


def _economic_ledger(value: object) -> list[dict[str, Any]]:
    rows = value if isinstance(value, list) else []
    result = []
    for row in rows:
        if isinstance(row, dict):
            result.append({
                "stock_code": row.get("stock_code"), "side": row.get("side"),
                "requested_shares": row.get("requested_shares"),
                "filled_shares": row.get("filled_shares"),
                "unfilled_shares": row.get("unfilled_shares"),
                "status": row.get("status"), "reason": row.get("reason"),
                "fill_price_yuan": row.get("fill_price_yuan"),
                "notional_yuan": row.get("notional_yuan"),
                "actual_ADV_participation": row.get(
                    "actual_ADV_participation"
                ),
                "costs": copy.deepcopy(row.get("costs")),
            })
    return sorted(result, key=lambda row: (
        str(row.get("stock_code") or ""), str(row.get("side") or ""),
        row.get("requested_shares") or 0, row.get("filled_shares") or 0,
    ))


def _compare_execution(
    strict_execution: dict[str, Any], verified_execution: dict[str, Any],
) -> dict[str, Any]:
    strict_state = _economic_state(strict_execution.get("state_after"))
    verified_state = _economic_state(verified_execution.get("state_after"))
    fields = {
        key: strict_state.get(key) == verified_state.get(key)
        for key in strict_state
    }
    fields["ledger"] = _economic_ledger(
        strict_execution.get("ledger")
    ) == _economic_ledger(verified_execution.get("ledger"))
    fields["unfilled"] = _economic_ledger(
        strict_execution.get("unfilled")
    ) == _economic_ledger(verified_execution.get("unfilled"))
    fields["daily_costs"] = strict_execution.get(
        "daily_costs"
    ) == verified_execution.get("daily_costs")
    return {
        "status": "match" if all(fields.values()) else "mismatch",
        "all_match": all(fields.values()),
        "field_matches": fields,
        "compared_state_fields": list(strict_state),
        "verified_only_FIFO_metadata_compared": False,
    }


def verify_strict_joint_forward_audit(
    path: Path, *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    try:
        resolved = path.resolve()
        resolved.relative_to(project_root.resolve())
        audit = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"audit_unreadable:{exc}"]}
    errors: list[str] = []
    if audit.get("schema") != "strict_joint_forward_audit.v1":
        errors.append("schema_invalid")
    if audit.get("audit_id") != _identity(audit, "audit_id"):
        errors.append("audit_id_invalid")
    if audit.get("no_backfill") is not True:
        errors.append("no_backfill_invalid")
    if audit.get("returns_or_labels_read") is not False:
        errors.append("outcome_boundary_invalid")
    if audit.get("performance_metrics_computed") is not False:
        errors.append("performance_boundary_invalid")
    if audit.get("authority") != _authority():
        errors.append("authority_invalid")
    status = audit.get("status")
    if status not in {
        "audit_complete", "execution_mismatch_fail_closed",
        "expected_no_trade_zero_signal", "strict_replay_rejected_fail_closed",
    }:
        errors.append("status_invalid")
    comparison = audit.get("execution_comparison")
    if not isinstance(comparison, dict):
        errors.append("execution_comparison_missing")
    elif (
        status == "audit_complete" and comparison.get("all_match") is not True
    ) or (
        status != "audit_complete" and comparison.get("all_match") is not False
    ):
        errors.append("execution_comparison_status_mismatch")
    for name in ("source_execution", "protocol", "state_sidecar"):
        try:
            _payload, _normalized = _load_ref(
                audit.get(name), project_root=project_root, label=name,
            )
        except ValueError as exc:
            errors.append(str(exc))
    result = audit.get("strict_result")
    if not isinstance(result, dict) or result.get("result_id") != _identity(
        result if isinstance(result, dict) else {}, "result_id",
    ):
        errors.append("strict_result_identity_invalid")
    elif (
        result.get("returns_or_labels_read") is not False
        or result.get("performance_metrics_computed") is not False
        or (result.get("authority") or {}).get("research_only") is not True
        or any(
            (result.get("authority") or {}).get(key) is not False
            for key in (
                "changes_live_score", "changes_live_trade_count",
                "changes_live_position_or_quantity", "generates_buy_or_sell_alert",
                "production_strategy", "automatic_trading", "stable_profit_claim",
            )
        )
    ):
        errors.append("strict_result_authority_invalid")
    return {
        "valid": not errors, "errors": list(dict.fromkeys(errors)),
        "audit": audit, "audit_id": audit.get("audit_id"),
    }


def capture_strict_joint_forward_audit(
    execution_ref: dict[str, Any], *, project_root: Path = PROJECT_ROOT,
    protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str | None = PROTOCOL_SHA256,
    audit_dir: Path = AUDIT_DIR, state_dir: Path = STATE_DIR,
    execution_verifier: Callable[..., dict[str, Any]] = verify_forward_execution_packet,
    replay_runner: Callable[..., dict[str, Any]] = run_strict_joint_replay,
) -> dict[str, Any]:
    """Persist one independent audit; never mutate or authorize the paper state."""
    try:
        protocol, protocol_ref = _verify_protocol(
            project_root=project_root, protocol_path=protocol_path,
            expected_sha256=expected_protocol_sha256,
        )
        raw, normalized_execution_ref = _load_ref(
            execution_ref, project_root=project_root, label="source_execution",
        )
        checked = execution_verifier(raw, project_root=project_root)
        execution = checked.get("packet") if isinstance(checked, dict) else None
        if checked.get("valid") is not True or not isinstance(execution, dict):
            return {
                "status": "source_execution_invalid", "captured": False,
                "errors": (checked.get("errors") or ["verified_packet_required"]),
                "authority": _authority(),
            }
        bundle, state_ref = _build_bundle(
            execution, normalized_execution_ref, protocol,
            project_root=project_root, state_dir=state_dir,
        )
        output = project_root / audit_dir / (
            f"{bundle['decision_day']}-{bundle['slot_id']}.json"
        )
        if output.exists():
            existing = verify_strict_joint_forward_audit(
                output, project_root=project_root,
            )
            source = (existing.get("audit") or {}).get("source_execution")
            same_source = source == normalized_execution_ref
            return {
                "status": (
                    "already_captured" if existing.get("valid") and same_source
                    else "existing_audit_invalid"
                ),
                "captured": existing.get("valid") is True and same_source,
                "path": str(output), "audit_id": existing.get("audit_id"),
                "errors": existing.get("errors") or (
                    [] if same_source else ["source_execution_ref_mismatch"]
                ),
                "authority": _authority(),
            }
        result = replay_runner(bundle, project_root=project_root)
        if not isinstance(result, dict) or result.get("result_id") != _identity(
            result if isinstance(result, dict) else {}, "result_id",
        ):
            raise ValueError("strict_replay_result_invalid")
        if result.get("status") == "replay_complete" and isinstance(
            result.get("execution"), dict,
        ):
            comparison = _compare_execution(result["execution"], execution)
            status = (
                "audit_complete" if comparison["all_match"]
                else "execution_mismatch_fail_closed"
            )
        elif result.get("status") == "zero_signal":
            candidate_stage = bundle["packets"]["stock_candidate"]
            no_plans = not candidate_stage.get("entry_plans") and not candidate_stage.get(
                "exit_plans"
            )
            source_no_fills = not execution.get("ledger") and not execution.get(
                "unfilled"
            )
            expected_no_trade = bool(
                no_plans and source_no_fills
                and "stock_candidate:no_executable_plan" in (
                    result.get("zero_signal_reasons") or []
                )
            )
            comparison = {
                "status": (
                    "expected_no_trade_no_strict_execution"
                    if expected_no_trade else "strict_replay_rejected"
                ),
                "all_match": False, "field_matches": {},
                "verified_only_FIFO_metadata_compared": False,
            }
            status = (
                "expected_no_trade_zero_signal" if expected_no_trade
                else "strict_replay_rejected_fail_closed"
            )
        else:
            raise ValueError("strict_replay_status_invalid")
        audit = {
            "schema": "strict_joint_forward_audit.v1",
            "status": status,
            "decision_day": bundle["decision_day"],
            "slot_id": bundle["slot_id"],
            "no_backfill": True,
            "source_execution": normalized_execution_ref,
            "protocol": protocol_ref,
            "state_sidecar": state_ref,
            "strict_input_bundle": bundle,
            "strict_result": result,
            "execution_comparison": comparison,
            "returns_or_labels_read": False,
            "performance_metrics_computed": False,
            "performance_metrics": None,
            "authority": _authority(),
        }
        audit["audit_id"] = _identity(audit, "audit_id")
        _atomic_publish(output, _canonical_bytes(audit))
        verified = verify_strict_joint_forward_audit(
            output, project_root=project_root,
        )
        if verified.get("valid") is not True:
            raise RuntimeError("strict forward audit failed post-write verification")
        return {
            "status": status, "captured": True, "path": str(output),
            "audit_id": audit["audit_id"],
            "strict_status": result.get("status"),
            "execution_match": comparison.get("all_match") is True,
            "authority": _authority(),
        }
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "mapping_invalid", "captured": False,
            "errors": [f"{type(exc).__name__}:{exc}"],
            "authority": _authority(),
        }


__all__ = [
    "capture_strict_joint_forward_audit", "verify_strict_joint_forward_audit",
]
