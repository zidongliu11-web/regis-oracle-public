"""Strict PIT orchestration for the full research route-to-portfolio replay.

This module intentionally owns no thresholds and selects no playbook.  It only
verifies already-frozen causal packets, then delegates A-share fills, costs and
state transitions to the existing joint execution engine.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from quantqc import ashare

from .market_state_daily import PROJECT_ROOT, _canonical_bytes, _sha256
from .unified_regime_route_replay import replay_joint_execution_day


SH_TZ = ZoneInfo("Asia/Shanghai")
INPUT_STAGE_ORDER = (
    "market_state",
    "exposure_budget",
    "sector_confirmation",
    "fixed_playbook",
    "stock_candidate",
    "A_share_execution_cost",
)
STAGE_ORDER = INPUT_STAGE_ORDER + ("portfolio",)
SCHEMAS = {
    "market_state": "strict_pit_market_state.v1",
    "exposure_budget": "strict_pit_exposure_budget.v1",
    "sector_confirmation": "strict_pit_sector_confirmation.v1",
    "fixed_playbook": "strict_pit_fixed_playbook.v1",
    "stock_candidate": "strict_pit_stock_candidate.v1",
    "A_share_execution_cost": "strict_pit_A_share_execution_cost.v1",
}
_OUTCOME_KEYS = {
    "future_return", "future_returns", "label", "labels", "outcome", "outcomes",
    "strategy_return", "annualized_return", "max_drawdown", "sharpe", "nav", "pnl",
}


def _identity(value: dict[str, Any], field: str = "packet_id") -> str:
    body = copy.deepcopy(value)
    body.pop(field, None)
    return hashlib.sha256(_canonical_bytes(body)).hexdigest()


def _time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SH_TZ)
    return parsed.astimezone(SH_TZ)


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    number = _finite(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    return int(number)


def _project_path(value: object, *, project_root: Path) -> Path:
    raw = Path(str(value or ""))
    resolved = (raw if raw.is_absolute() else project_root / raw).resolve()
    resolved.relative_to(project_root.resolve())
    return resolved


def _binding_errors(
    ref: object, *, label: str, project_root: Path,
) -> tuple[list[str], Path | None]:
    row = ref if isinstance(ref, dict) else {}
    try:
        path = _project_path(row.get("path"), project_root=project_root)
    except (OSError, ValueError):
        return [f"{label}:frozen_ref_path_invalid"], None
    if not path.is_file():
        return [f"{label}:frozen_ref_missing"], path
    if not str(row.get("sha256") or "") or _sha256(path) != row.get("sha256"):
        return [f"{label}:frozen_ref_sha256_mismatch"], path
    return [], path


def _outcome_keys(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in _OUTCOME_KEYS:
                found.append(str(key))
            found.extend(_outcome_keys(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_outcome_keys(child))
    return found


def _common_packet_errors(
    name: str, packet: object, bundle: dict[str, Any], *, project_root: Path,
) -> list[str]:
    if not isinstance(packet, dict):
        return [f"{name}:packet_missing"]
    errors: list[str] = []
    if packet.get("schema") != SCHEMAS[name]:
        errors.append(f"{name}:schema_invalid")
    if packet.get("packet_id") != _identity(packet):
        errors.append(f"{name}:packet_id_invalid")
    if packet.get("no_backfill") is not True:
        errors.append(f"{name}:no_backfill_required")
    if packet.get("returns_or_labels_read") is not False:
        errors.append(f"{name}:outcome_boundary_invalid")
    errors.extend(
        f"{name}:outcome_field_forbidden:{key}"
        for key in sorted(set(_outcome_keys(packet)))
    )
    errors.extend(_binding_errors(
        packet.get("frozen_ref"), label=name, project_root=project_root,
    )[0])
    for key in ("decision_day", "slot_id", "decision_at"):
        if not packet.get(key):
            errors.append(f"{name}:field_missing:{key}")
        elif packet.get(key) != bundle.get(key):
            errors.append(f"{name}:decision_identity_mismatch:{key}")
    if name != "A_share_execution_cost":
        decision_at = _time(packet.get("decision_at"))
        source_as_of = _time(packet.get("source_as_of"))
        if decision_at is None or source_as_of is None:
            errors.append(f"{name}:PIT_time_invalid")
        elif source_as_of > decision_at:
            errors.append(f"{name}:future_source_data")
    return errors


def _authority() -> dict[str, bool]:
    return {
        "research_only": True,
        "changes_live_score": False,
        "changes_live_trade_count": False,
        "changes_live_position_or_quantity": False,
        "generates_buy_or_sell_alert": False,
        "production_strategy": False,
        "automatic_trading": False,
        "stable_profit_claim": False,
    }


def _finish(
    *, status: str, bundle: dict[str, Any], trace: list[dict[str, Any]],
    reasons: list[str], execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    seen = list(dict.fromkeys(reasons))
    completed = {row["stage"] for row in trace}
    if status == "zero_signal":
        for stage in STAGE_ORDER:
            if stage not in completed:
                trace.append({"stage": stage, "status": "skipped_fail_closed"})
    result = {
        "schema": "strict_joint_strategy_portfolio_replay_result.v1",
        "status": status,
        "mode": bundle.get("mode"),
        "decision_day": bundle.get("decision_day"),
        "slot_id": bundle.get("slot_id"),
        "decision_at": bundle.get("decision_at"),
        "stage_order": list(STAGE_ORDER),
        "stage_trace": trace,
        "zero_signal_reasons": seen,
        "execution": copy.deepcopy(execution),
        "returns_or_labels_read": False,
        "performance_metrics_computed": False,
        "performance_metrics": None,
        "authority": _authority(),
    }
    result["result_id"] = _identity(result, "result_id")
    return result


def _fail(
    name: str, errors: list[str], *, bundle: dict[str, Any],
    trace: list[dict[str, Any]],
) -> dict[str, Any]:
    trace.append({"stage": name, "status": "rejected", "errors": errors})
    return _finish(status="zero_signal", bundle=bundle, trace=trace, reasons=errors)


def _factor_contract_ready(packet: dict[str, Any]) -> bool:
    evidence = packet.get("factor_evidence") or {}
    factor_ids = evidence.get("admitted_factor_ids")
    return bool(
        (
            evidence.get("status") == "strategy_formula_only_no_admitted_factor"
            and factor_ids == []
            and evidence.get("factor_score_applied") is False
        )
        or (
            evidence.get("status") == "production_factors_applied"
            and isinstance(factor_ids, list) and bool(factor_ids)
            and evidence.get("factor_score_applied") is True
            and evidence.get("production_gate_valid") is True
        )
    )


def _plan_errors(
    candidate: dict[str, Any], budget: dict[str, Any], playbook: dict[str, Any],
) -> list[str]:
    entries = candidate.get("entry_plans")
    exits = candidate.get("exit_plans")
    if not isinstance(entries, list) or not isinstance(exits, list):
        return ["stock_candidate:plans_missing"]
    errors: list[str] = []
    maximum = _nonnegative_int(budget.get("maximum_new_trades"))
    if maximum is None:
        errors.append("exposure_budget:maximum_new_trades_invalid")
    elif len(entries) > maximum:
        errors.append("stock_candidate:entry_count_exceeds_frozen_budget")
    if entries and playbook.get("allows_new_entries") is not True:
        errors.append("fixed_playbook:new_entries_not_allowed")
    ids: list[str] = []
    for kind, plans in (("entry", entries), ("exit", exits)):
        for index, plan in enumerate(plans):
            label = f"stock_candidate:{kind}_plan_{index}"
            if not isinstance(plan, dict):
                errors.append(f"{label}:invalid")
                continue
            plan_id = str(plan.get("plan_id") or plan.get("order_id") or "")
            ids.append(plan_id)
            shares = _nonnegative_int(plan.get("shares"))
            price = _finite(plan.get("limit_price_yuan"))
            expected_side = "buy" if kind == "entry" else "sell"
            if not plan_id:
                errors.append(f"{label}:identity_missing")
            if plan.get("side") != expected_side:
                errors.append(f"{label}:side_invalid")
            if not str(plan.get("stock_code") or ""):
                errors.append(f"{label}:stock_code_missing")
            if shares is None or shares <= 0:
                errors.append(f"{label}:quantity_invalid")
            if price is None or price <= 0:
                errors.append(f"{label}:limit_price_invalid")
            if kind == "entry" and (
                plan.get("order_session") != "next_trading_day_opening_auction"
                or plan.get("order_type") != "limit"
            ):
                errors.append(f"{label}:opening_limit_contract_invalid")
            if kind == "exit" and (
                plan.get("reason") not in {"strategy_exit", "risk_reduction"}
                or not isinstance(plan.get("full_liquidation"), bool)
            ):
                errors.append(f"{label}:exit_authorization_invalid")
    if any(not item for item in ids) or len(ids) != len(set(ids)):
        errors.append("stock_candidate:plan_identity_duplicate_or_missing")
    if not entries and not exits:
        errors.append("stock_candidate:no_executable_plan")
    return errors


def _quote_errors(
    code: str, quote: object, execution: dict[str, Any], *,
    decision_day: str, trade_date: str, captured_at: datetime,
    liquidity_sha: str, orders: list[dict[str, Any]],
) -> list[str]:
    label = f"A_share_execution_cost:{code}"
    if not isinstance(quote, dict):
        return [f"{label}:quote_missing"]
    errors: list[str] = []
    source_as_of = _time(quote.get("source_as_of"))
    if source_as_of is None or source_as_of > captured_at:
        errors.append(f"{label}:quote_time_invalid_or_future")
    elif source_as_of.date().isoformat() != trade_date:
        errors.append(f"{label}:quote_not_from_execution_day")
    if str(quote.get("lagged_adv_as_of") or "") != decision_day:
        errors.append(f"{label}:lagged_ADV_not_from_decision_day")
    if quote.get("liquidity_model_sha256") != liquidity_sha:
        errors.append(f"{label}:liquidity_identity_mismatch")
    for field in ("open_price", "prev_close", "lagged_adv_yuan"):
        value = _finite(quote.get(field))
        if value is None or value <= 0:
            errors.append(f"{label}:{field}_missing_or_invalid")
    for field in ("tradable", "is_risk_warning", "no_price_limit"):
        if not isinstance(quote.get(field), bool):
            errors.append(f"{label}:unknown_{field}_identity")
    if quote.get("tradable") is False:
        errors.append(f"{label}:suspended_or_untradable")
    if quote.get("no_price_limit") is True:
        errors.append(f"{label}:unsupported_no_price_limit_day")
    for field in ("effective_limit_up_price", "effective_limit_down_price"):
        value = _finite(quote.get(field))
        if value is None or value <= 0:
            errors.append(f"{label}:unknown_price_limit:{field}")
    if errors:
        return errors
    expected_limits = {
        "effective_limit_up_price": ashare.opening_auction_executability(
            code=code, trade_date=trade_date, side="buy",
            open_price=quote["open_price"], prev_close=quote["prev_close"],
            tradable=True, is_risk_warning=quote["is_risk_warning"],
            no_price_limit=False,
        ).get("limit_price"),
        "effective_limit_down_price": ashare.opening_auction_executability(
            code=code, trade_date=trade_date, side="sell",
            open_price=quote["open_price"], prev_close=quote["prev_close"],
            tradable=True, is_risk_warning=quote["is_risk_warning"],
            no_price_limit=False,
        ).get("limit_price"),
    }
    for field, expected in expected_limits.items():
        if expected is None or abs(float(quote[field]) - float(expected)) > 0.001:
            errors.append(f"{label}:price_limit_identity_mismatch:{field}")
    for plan in orders:
        market = ashare.opening_limit_order_executability(
            code=code, trade_date=trade_date, side=str(plan.get("side") or ""),
            order_price=plan.get("limit_price_yuan"),
            open_price=quote.get("open_price"), prev_close=quote.get("prev_close"),
            tradable=quote.get("tradable"),
            is_risk_warning=quote.get("is_risk_warning"),
            no_price_limit=quote.get("no_price_limit"),
        )
        if market.get("executable") is not True:
            errors.append(f"{label}:order_fail_closed:{market.get('reason')}")
    return errors


def _sellable_errors(
    state: dict[str, Any], exits: list[dict[str, Any]], trade_date: str,
) -> list[str]:
    errors: list[str] = []
    positions = state.get("positions") if isinstance(state.get("positions"), dict) else {}
    for plan in exits:
        code = str(plan.get("stock_code") or "")
        position = positions.get(code)
        if not isinstance(position, dict):
            errors.append(f"A_share_execution_cost:{code}:position_missing_for_exit")
            continue
        base = _nonnegative_int(position.get("sellable_shares"))
        total = _nonnegative_int(position.get("shares_total"))
        requested = _nonnegative_int(plan.get("shares"))
        locked = position.get("locked_buys")
        if base is None or total is None or not isinstance(locked, list):
            errors.append(f"A_share_execution_cost:{code}:T_plus_1_sellable_missing")
            continue
        unlocked = 0
        for lot in locked:
            shares = _nonnegative_int((lot or {}).get("shares") if isinstance(lot, dict) else None)
            buy_date = str((lot or {}).get("buy_trade_date") or "") if isinstance(lot, dict) else ""
            if shares is None or not buy_date:
                errors.append(f"A_share_execution_cost:{code}:T_plus_1_lot_invalid")
                continue
            if buy_date < trade_date:
                unlocked += shares
        sellable = base + unlocked
        full = plan.get("full_liquidation") is True
        if requested is None or requested > sellable:
            errors.append(f"A_share_execution_cost:{code}:T_plus_1_sellable_insufficient")
        elif full and (requested != total or sellable != total):
            errors.append(f"A_share_execution_cost:{code}:full_liquidation_not_fully_sellable")
        elif not full and requested % 100:
            errors.append(f"A_share_execution_cost:{code}:odd_lot_partial_sell_invalid")
    return errors


def _exposure_plan_errors(
    state: dict[str, Any], budget: dict[str, Any], market_day: dict[str, Any],
    entries: list[dict[str, Any]], exits: list[dict[str, Any]],
) -> list[str]:
    """Require a frozen exact reduction before any new entry when over target."""
    cash = _finite(state.get("cash_yuan"))
    target_pct = _finite(budget.get("target_exposure_pct"))
    positions = state.get("positions") if isinstance(state.get("positions"), dict) else {}
    quotes = market_day.get("quotes") if isinstance(market_day.get("quotes"), dict) else {}
    if cash is None or target_pct is None:
        return ["portfolio:cash_or_target_missing"]
    values: dict[str, float] = {}
    for code, position in positions.items():
        shares = _nonnegative_int((position or {}).get("shares_total") if isinstance(position, dict) else None)
        price = _finite((quotes.get(code) or {}).get("prev_close"))
        if shares is None or price is None or price <= 0:
            return [f"portfolio:{code}:valuation_input_missing"]
        values[code] = shares * price
    equity = cash + sum(values.values())
    reduction = max(0.0, sum(values.values()) - equity * target_pct / 100.0)
    if reduction <= 0.005:
        return []
    errors: list[str] = []
    if entries:
        errors.append("portfolio:risk_reduction_must_precede_new_entries")
    planned = 0.0
    for plan in exits:
        code = str(plan.get("stock_code") or "")
        shares = _nonnegative_int(plan.get("shares"))
        price = _finite((quotes.get(code) or {}).get("prev_close"))
        if shares is not None and price is not None:
            planned += shares * price
    if planned + 0.005 < reduction:
        errors.append("portfolio:exact_risk_reduction_plan_missing_or_insufficient")
    return errors


def run_strict_joint_replay(
    bundle: dict[str, Any], *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Verify the causal chain and run one research-only portfolio transition."""
    source = copy.deepcopy(bundle) if isinstance(bundle, dict) else {}
    trace: list[dict[str, Any]] = []
    top_errors: list[str] = []
    if source.get("schema") != "strict_joint_strategy_portfolio_replay_input.v1":
        top_errors.append("bundle:schema_invalid")
    if source.get("mode") not in {"historical_replay", "forward_replay"}:
        top_errors.append("bundle:mode_invalid")
    if source.get("no_backfill") is not True:
        top_errors.append("bundle:no_backfill_required")
    if source.get("returns_or_labels_read") is not False:
        top_errors.append("bundle:outcome_boundary_invalid")
    decision_at = _time(source.get("decision_at"))
    if decision_at is None:
        top_errors.append("bundle:decision_at_invalid")
    elif decision_at.date().isoformat() != str(source.get("decision_day") or ""):
        top_errors.append("bundle:decision_at_day_mismatch")
    if not str(source.get("slot_id") or ""):
        top_errors.append("bundle:slot_id_missing")
    if top_errors:
        return _fail("market_state", top_errors, bundle=source, trace=trace)

    packets = source.get("packets") if isinstance(source.get("packets"), dict) else {}
    market = packets.get("market_state")
    errors = _common_packet_errors("market_state", market, source, project_root=project_root)
    if isinstance(market, dict):
        if not str(market.get("market_state") or ""):
            errors.append("market_state:value_missing")
        if market.get("completed_inputs_only") is not True:
            errors.append("market_state:completed_inputs_only_required")
    if errors:
        return _fail("market_state", errors, bundle=source, trace=trace)
    trace.append({"stage": "market_state", "status": "verified", "packet_id": market["packet_id"]})

    budget = packets.get("exposure_budget")
    errors = _common_packet_errors("exposure_budget", budget, source, project_root=project_root)
    if isinstance(budget, dict):
        if budget.get("market_state_packet_id") != market.get("packet_id"):
            errors.append("exposure_budget:market_state_chain_mismatch")
        target = _finite(budget.get("target_exposure_pct"))
        if target is None or not 0 <= target <= 100:
            errors.append("exposure_budget:target_exposure_invalid")
        if _nonnegative_int(budget.get("maximum_new_trades")) is None:
            errors.append("exposure_budget:maximum_new_trades_invalid")
    if errors:
        return _fail("exposure_budget", errors, bundle=source, trace=trace)
    trace.append({"stage": "exposure_budget", "status": "verified", "packet_id": budget["packet_id"]})

    sector = packets.get("sector_confirmation")
    errors = _common_packet_errors("sector_confirmation", sector, source, project_root=project_root)
    if isinstance(sector, dict):
        if sector.get("market_state_packet_id") != market.get("packet_id"):
            errors.append("sector_confirmation:market_state_chain_mismatch")
        if sector.get("exposure_budget_packet_id") != budget.get("packet_id"):
            errors.append("sector_confirmation:exposure_budget_chain_mismatch")
        if sector.get("status") not in {"confirmed", "not_required_by_frozen_playbook"}:
            errors.append("sector_confirmation:not_confirmed")
    if errors:
        return _fail("sector_confirmation", errors, bundle=source, trace=trace)
    trace.append({"stage": "sector_confirmation", "status": "verified", "packet_id": sector["packet_id"]})

    playbook = packets.get("fixed_playbook")
    errors = _common_packet_errors("fixed_playbook", playbook, source, project_root=project_root)
    if isinstance(playbook, dict):
        if playbook.get("sector_confirmation_packet_id") != sector.get("packet_id"):
            errors.append("fixed_playbook:sector_chain_mismatch")
        if not str(playbook.get("playbook_id") or ""):
            errors.append("fixed_playbook:playbook_id_missing")
        if playbook.get("selected_before_outcomes") is not True:
            errors.append("fixed_playbook:ex_ante_selection_required")
        required = playbook.get("sector_confirmation_required") is True
        if required and sector.get("status") != "confirmed":
            errors.append("fixed_playbook:required_sector_not_confirmed")
        if playbook.get("named_person_signal_used") is not False:
            errors.append("fixed_playbook:named_person_signal_forbidden")
    if errors:
        return _fail("fixed_playbook", errors, bundle=source, trace=trace)
    trace.append({"stage": "fixed_playbook", "status": "verified", "packet_id": playbook["packet_id"]})

    candidate = packets.get("stock_candidate")
    errors = _common_packet_errors("stock_candidate", candidate, source, project_root=project_root)
    if isinstance(candidate, dict):
        if candidate.get("fixed_playbook_packet_id") != playbook.get("packet_id"):
            errors.append("stock_candidate:playbook_chain_mismatch")
        if candidate.get("playbook_id") != playbook.get("playbook_id"):
            errors.append("stock_candidate:playbook_id_mismatch")
        if candidate.get("named_person_signal_used") is not False:
            errors.append("stock_candidate:named_person_signal_forbidden")
        if candidate.get("selected_before_outcomes") is not True:
            errors.append("stock_candidate:ex_ante_selection_required")
        errors.extend(_binding_errors(
            candidate.get("formula_ref"), label="stock_candidate:formula",
            project_root=project_root,
        )[0])
        if not _factor_contract_ready(candidate):
            errors.append("stock_candidate:factor_contract_not_admitted")
        errors.extend(_plan_errors(candidate, budget, playbook))
    if errors:
        return _fail("stock_candidate", errors, bundle=source, trace=trace)
    trace.append({"stage": "stock_candidate", "status": "verified", "packet_id": candidate["packet_id"]})

    execution = packets.get("A_share_execution_cost")
    errors = _common_packet_errors(
        "A_share_execution_cost", execution, source, project_root=project_root,
    )
    if not isinstance(execution, dict):
        return _fail("A_share_execution_cost", errors, bundle=source, trace=trace)
    if execution.get("stock_candidate_packet_id") != candidate.get("packet_id"):
        errors.append("A_share_execution_cost:candidate_chain_mismatch")
    captured_at = _time(execution.get("source_as_of"))
    if captured_at is None:
        errors.append("A_share_execution_cost:source_time_invalid")
    market_day = execution.get("market_day") if isinstance(execution.get("market_day"), dict) else {}
    decision_day = str(source.get("decision_day") or "")
    trade_date = str(market_day.get("trade_date") or "")
    if market_day.get("previous_trade_date") != decision_day or trade_date <= decision_day:
        errors.append("A_share_execution_cost:trade_date_chain_invalid")
    if captured_at is not None and (
        captured_at.date().isoformat() != trade_date
        or (_time(source.get("decision_at")) is not None
            and captured_at <= _time(source.get("decision_at")))
    ):
        errors.append("A_share_execution_cost:execution_capture_time_invalid")
    if market_day.get("no_backfill") is not True:
        errors.append("A_share_execution_cost:market_no_backfill_required")
    if market_day.get("market_snapshot_id") != _identity(market_day, "market_snapshot_id"):
        errors.append("A_share_execution_cost:market_snapshot_id_invalid")
    cost_errors, _ = _binding_errors(
        execution.get("cost_model_ref"), label="A_share_execution_cost:cost_model",
        project_root=project_root,
    )
    liquidity_errors, _ = _binding_errors(
        execution.get("liquidity_model_ref"), label="A_share_execution_cost:liquidity_model",
        project_root=project_root,
    )
    errors.extend(cost_errors + liquidity_errors)
    config = execution.get("execution_config")
    if not isinstance(config, dict) or execution.get("execution_config_sha256") != hashlib.sha256(
        _canonical_bytes(config if isinstance(config, dict) else {})
    ).hexdigest():
        errors.append("A_share_execution_cost:execution_config_identity_invalid")
    state_errors, state_path = _binding_errors(
        execution.get("previous_state_ref"), label="A_share_execution_cost:previous_state",
        project_root=project_root,
    )
    errors.extend(state_errors)
    previous_state: dict[str, Any] = {}
    if state_path is not None and not state_errors:
        try:
            previous_state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            errors.append("A_share_execution_cost:previous_state_unreadable")
    if previous_state.get("state_id") != _identity(previous_state, "state_id"):
        errors.append("A_share_execution_cost:previous_state_id_invalid")
    if previous_state.get("last_trade_date") != decision_day:
        errors.append("A_share_execution_cost:previous_state_date_mismatch")
    cost_sha = str((execution.get("cost_model_ref") or {}).get("sha256") or "")
    if previous_state.get("cost_model_id") != cost_sha:
        errors.append("A_share_execution_cost:state_cost_model_identity_mismatch")
    entries = candidate.get("entry_plans") or []
    exits = candidate.get("exit_plans") or []
    orders_by_code: dict[str, list[dict[str, Any]]] = {}
    for plan in entries + exits:
        orders_by_code.setdefault(str(plan.get("stock_code") or ""), []).append(plan)
    position_codes = set((previous_state.get("positions") or {}).keys())
    liquidity_sha = str((execution.get("liquidity_model_ref") or {}).get("sha256") or "")
    if captured_at is not None:
        for code in sorted(set(orders_by_code) | position_codes):
            errors.extend(_quote_errors(
                code, (market_day.get("quotes") or {}).get(code), execution,
                decision_day=decision_day, trade_date=trade_date,
                captured_at=captured_at, liquidity_sha=liquidity_sha,
                orders=orders_by_code.get(code, []),
            ))
    errors.extend(_sellable_errors(previous_state, exits, trade_date))
    errors.extend(_exposure_plan_errors(
        previous_state, budget, market_day, entries, exits,
    ))
    if errors:
        return _fail("A_share_execution_cost", errors, bundle=source, trace=trace)

    decision = {
        "schema": "strict_joint_strategy_execution_decision.v1",
        "decision_day": decision_day,
        "slot_id": source.get("slot_id"),
        "no_backfill": True,
        "upstream_packet_ids": {
            name: packets[name]["packet_id"] for name in INPUT_STAGE_ORDER[:-1]
        },
        "decision": {
            "target_exposure_pct": budget.get("target_exposure_pct"),
            "maximum_new_trades": budget.get("maximum_new_trades"),
            "pre_execution_plans": copy.deepcopy(entries),
            "execution_replay_complete": False,
            "returns_or_labels_read": False,
            "authority": _authority(),
        },
    }
    decision["packet_id"] = _identity(decision)
    normalized_exits = []
    for plan in exits:
        normalized_exits.append({
            "order_id": plan.get("order_id") or plan.get("plan_id"),
            "source_packet_id": decision["packet_id"],
            "stock_code": plan.get("stock_code"),
            "side": "sell", "requested_shares": plan.get("shares"),
            "limit_price_yuan": plan.get("limit_price_yuan"),
            "valid_trade_date": trade_date,
            "order_session": "next_trading_day_opening_auction",
            "order_type": "limit", "time_in_force": "carry_until_executable",
            "reason": plan.get("reason"),
            "full_liquidation": plan.get("full_liquidation") is True,
        })
    executed = replay_joint_execution_day(
        previous_state, [decision], market_day, normalized_exits,
        execution_config=config,
    )
    if executed.get("executed") is not True:
        replay_errors = [
            f"A_share_execution_cost:engine:{error}"
            for error in (executed.get("errors") or ["execution_failed"])
        ]
        return _fail("A_share_execution_cost", replay_errors, bundle=source, trace=trace)
    trace.append({
        "stage": "A_share_execution_cost", "status": "verified_and_replayed",
        "packet_id": execution["packet_id"],
        "execution_packet_id": executed["packet"]["packet_id"],
    })
    state_after = executed["packet"].get("state_after") or {}
    if state_after.get("state_id") != _identity(state_after, "state_id"):
        return _fail(
            "portfolio", ["portfolio:state_id_invalid"], bundle=source, trace=trace,
        )
    trace.append({
        "stage": "portfolio", "status": "verified",
        "state_id": state_after.get("state_id"),
    })
    return _finish(
        status="replay_complete", bundle=source, trace=trace, reasons=[],
        execution=executed["packet"],
    )


__all__ = ["INPUT_STAGE_ORDER", "STAGE_ORDER", "run_strict_joint_replay"]
