"""Chronological, research-only replay of the complete market-cognition path.

The engine consumes frozen strict-joint inputs/results.  It never reconstructs
signals from incomplete historical raw data and never owns strategy rules.
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

from .market_state_daily import PROJECT_ROOT, _canonical_bytes, _sha256
from .strict_joint_strategy_replay import run_strict_joint_replay
from .unified_regime_route_replay import replay_joint_execution_day


PROTOCOL_PATH = PROJECT_ROOT / "research/NONCONVENTIONAL_MARKET_COGNITION_REPLAY_PROTOCOL_V1.json"
PROTOCOL_SHA256 = "e2eec8cff5badbab4813c7f00a91ec1eea5b72ac272b0e8e483424a2b57eb75e"
_OUTCOME_KEYS = {
    "future_return", "future_returns", "label", "labels", "outcome", "outcomes",
    "strategy_return", "annualized_return", "max_drawdown", "sharpe", "nav", "pnl",
}


def _identity(value: dict[str, Any], field: str) -> str:
    body = copy.deepcopy(value)
    body.pop(field, None)
    return hashlib.sha256(_canonical_bytes(body)).hexdigest()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError:
        return None


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _outcome_paths(value: object, prefix: str = "bundle") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}"
            if str(key).lower() in _OUTCOME_KEYS:
                found.append(path)
            found.extend(_outcome_paths(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_outcome_paths(child, f"{prefix}[{index}]"))
    return found


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


def _protocol_errors() -> list[str]:
    if not PROTOCOL_PATH.is_file() or _sha256(PROTOCOL_PATH) != PROTOCOL_SHA256:
        return ["protocol:sha256_mismatch_or_missing"]
    try:
        protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["protocol:unreadable"]
    binding = (protocol.get("bindings") or {}).get("one_day_strict_joint_replay") or {}
    strict_path = Path(__file__).with_name("strict_joint_strategy_replay.py")
    errors: list[str] = []
    if protocol.get("status") != "frozen_before_strategy_return_evaluation":
        errors.append("protocol:status_invalid")
    if binding.get("sha256") != _sha256(strict_path):
        errors.append("protocol:strict_runner_binding_invalid")
    return errors


def _read_ref(ref: object, *, project_root: Path, label: str) -> tuple[dict[str, Any] | None, list[str]]:
    row = ref if isinstance(ref, dict) else {}
    try:
        raw = Path(str(row.get("path") or ""))
        path = (raw if raw.is_absolute() else project_root / raw).resolve()
        path.relative_to(project_root.resolve())
    except (OSError, ValueError):
        return None, [f"{label}:path_invalid"]
    if not path.is_file() or not row.get("sha256") or _sha256(path) != row.get("sha256"):
        return None, [f"{label}:sha256_mismatch_or_missing"]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, [f"{label}:json_invalid"]
    return (value, []) if isinstance(value, dict) else (None, [f"{label}:object_required"])


def _ref_identity_errors(ref: object, *, project_root: Path, label: str) -> list[str]:
    row = ref if isinstance(ref, dict) else {}
    try:
        raw = Path(str(row.get("path") or ""))
        path = (raw if raw.is_absolute() else project_root / raw).resolve()
        path.relative_to(project_root.resolve())
    except (OSError, ValueError):
        return [f"{label}:path_invalid"]
    if not path.is_file() or not row.get("sha256") or _sha256(path) != row.get("sha256"):
        return [f"{label}:sha256_mismatch_or_missing"]
    return []


def _execution_envelope_errors(
    bundle: dict[str, Any], execution: dict[str, Any], *, project_root: Path,
) -> list[str]:
    packets = bundle.get("packets") or {}
    market_day = execution.get("market_day") or {}
    config = execution.get("execution_config")
    errors: list[str] = []
    if execution.get("schema") != "strict_pit_A_share_execution_cost.v1":
        errors.append("execution:schema_invalid")
    if execution.get("packet_id") != _identity(execution, "packet_id"):
        errors.append("execution:packet_id_invalid")
    if execution.get("no_backfill") is not True or market_day.get("no_backfill") is not True:
        errors.append("execution:no_backfill_required")
    if execution.get("returns_or_labels_read") is not False:
        errors.append("execution:outcome_boundary_invalid")
    for field in ("decision_day", "slot_id", "decision_at"):
        if execution.get(field) != bundle.get(field):
            errors.append(f"execution:decision_identity_mismatch:{field}")
    if execution.get("stock_candidate_packet_id") != (packets.get("stock_candidate") or {}).get("packet_id"):
        errors.append("execution:candidate_chain_mismatch")
    if market_day.get("market_snapshot_id") != _identity(market_day, "market_snapshot_id"):
        errors.append("execution:market_snapshot_id_invalid")
    if not isinstance(config, dict) or execution.get("execution_config_sha256") != _digest(config or {}):
        errors.append("execution:config_identity_invalid")
    errors.extend(_ref_identity_errors(
        execution.get("cost_model_ref"), project_root=project_root,
        label="execution:cost_model",
    ))
    errors.extend(_ref_identity_errors(
        execution.get("liquidity_model_ref"), project_root=project_root,
        label="execution:liquidity_model",
    ))
    return errors


def _state_cost(state: dict[str, Any]) -> float | None:
    return _finite((state.get("cumulative_costs") or {}).get("total"))


def _state_notional(state: dict[str, Any]) -> float | None:
    buy = _finite(state.get("cumulative_buy_notional"))
    sell = _finite(state.get("cumulative_sell_notional"))
    return None if buy is None or sell is None else buy + sell


def _value_state(state: dict[str, Any], prices: object, *, label: str) -> tuple[float | None, list[str]]:
    cash = _finite(state.get("cash_yuan"))
    positions = state.get("positions")
    rows = prices if isinstance(prices, dict) else {}
    errors: list[str] = []
    if cash is None or cash < 0:
        errors.append(f"{label}:cash_invalid")
    if not isinstance(positions, dict):
        errors.append(f"{label}:positions_invalid")
        positions = {}
    total = cash or 0.0
    for code, position in positions.items():
        shares = _finite((position or {}).get("shares_total") if isinstance(position, dict) else None)
        price = _finite(rows.get(code))
        if shares is None or shares < 0 or not shares.is_integer():
            errors.append(f"{label}:{code}:shares_invalid")
        elif price is None or price <= 0:
            errors.append(f"{label}:{code}:price_missing_or_invalid")
        else:
            total += shares * price
    return (None, errors) if errors else (float(total), [])


def _initial_prices(bundle: dict[str, Any], state: dict[str, Any]) -> dict[str, float]:
    execution = ((bundle.get("packets") or {}).get("A_share_execution_cost") or {})
    quotes = ((execution.get("market_day") or {}).get("quotes") or {})
    return {
        str(code): (quote or {}).get("prev_close")
        for code, quote in quotes.items()
        if code in (state.get("positions") or {})
    }


def _valid_no_trade_transition(
    bundle: dict[str, Any], previous_state: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    packets = bundle.get("packets") or {}
    candidate = packets.get("stock_candidate") or {}
    execution = packets.get("A_share_execution_cost") or {}
    budget = packets.get("exposure_budget") or {}
    market_day = execution.get("market_day") or {}
    errors: list[str] = []
    if candidate.get("entry_plans") != [] or candidate.get("exit_plans") != []:
        errors.append("no_trade:plans_not_empty")
    if previous_state.get("pending_exit_orders") != []:
        errors.append("no_trade:pending_exit_orders_require_execution")
    prices = {
        str(code): (quote or {}).get("prev_close")
        for code, quote in (market_day.get("quotes") or {}).items()
    }
    equity, valuation_errors = _value_state(previous_state, prices, label="no_trade_precheck")
    errors.extend(valuation_errors)
    target = _finite(budget.get("target_exposure_pct"))
    invested = 0.0
    if equity is not None:
        invested = equity - float(previous_state.get("cash_yuan") or 0.0)
    if target is None or not 0 <= target <= 100:
        errors.append("no_trade:target_exposure_invalid")
    elif equity is not None and invested > equity * target / 100.0 + 0.005:
        errors.append("no_trade:mandatory_risk_reduction_missing")
    if errors:
        return None, errors
    replayed = replay_joint_execution_day(
        previous_state, [], market_day, [],
        execution_config=execution.get("execution_config"),
    )
    if replayed.get("executed") is not True or (replayed.get("packet") or {}).get("ledger") != []:
        return None, [
            f"no_trade:state_carry_failed:{item}"
            for item in (replayed.get("errors") or ["unexpected_execution"])
        ]
    return replayed["packet"]["state_after"], []


def _mark_errors(
    mark: object, *, result_id: str, state: dict[str, Any], trade_date: str,
    execution_at: datetime,
) -> tuple[float | None, list[str]]:
    packet = mark if isinstance(mark, dict) else {}
    errors: list[str] = []
    if packet.get("schema") != "market_cognition_after_session_valuation.v1":
        errors.append("mark:schema_invalid")
    if packet.get("packet_id") != _identity(packet, "packet_id"):
        errors.append("mark:packet_id_invalid")
    if packet.get("no_backfill") is not True:
        errors.append("mark:no_backfill_required")
    if packet.get("outcome_role") != "post_decision_valuation_only":
        errors.append("mark:outcome_role_invalid")
    if packet.get("strict_result_id") != result_id:
        errors.append("mark:strict_result_chain_mismatch")
    if packet.get("state_id") != state.get("state_id"):
        errors.append("mark:state_chain_mismatch")
    if packet.get("trade_date") != trade_date:
        errors.append("mark:trade_date_mismatch")
    source_as_of = _time(packet.get("source_as_of"))
    marked_at = _time(packet.get("marked_at"))
    if source_as_of is None or marked_at is None:
        errors.append("mark:time_invalid_or_timezone_missing")
    elif not execution_at < source_as_of <= marked_at:
        errors.append("mark:decision_execution_outcome_order_invalid")
    elif source_as_of.date().isoformat() != trade_date:
        errors.append("mark:not_from_execution_date")
    equity, valuation_errors = _value_state(
        state, packet.get("prices_yuan"), label="mark",
    )
    errors.extend(valuation_errors)
    return (None, errors) if errors else (equity, [])


def _conditional(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row[key]), []).append(row)
    result: dict[str, dict[str, Any]] = {}
    for name, values in sorted(grouped.items()):
        compounded = math.prod(1.0 + row["session_return"] for row in values) - 1.0
        result[name] = {
            "sessions": len(values),
            "traded_sessions": sum(row["trade_count"] > 0 for row in values),
            "no_trade_sessions": sum(row["action"] == "no_trade" for row in values),
            "trade_count": sum(row["trade_count"] for row in values),
            "cost_yuan": round(sum(row["cost_delta"] for row in values), 6),
            "compounded_return_pct": round(compounded * 100.0, 8),
            "mean_session_return_pct": round(fmean(row["session_return"] for row in values) * 100.0, 8),
        }
    return result


def _finish(
    *, status: str, source: dict[str, Any], errors: list[str],
    trace: list[dict[str, Any]], curve: list[dict[str, Any]] | None = None,
    metrics: dict[str, Any] | None = None,
    conditional: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "schema": "market_cognition_system_replay_result.v1",
        "status": status,
        "mode": source.get("mode"),
        "errors": list(dict.fromkeys(errors)),
        "session_trace": trace,
        "equity_curve": curve if status == "replay_complete" else [],
        "metrics": metrics if status == "replay_complete" else None,
        "conditional_performance": conditional if status == "replay_complete" else None,
        "strict_bundles_required": True,
        "historical_raw_signals_generated": False,
        "missing_historical_raw_policy": "fail_closed_never_infer_or_fill",
        "cost_after": status == "replay_complete",
        "authority": _authority(),
    }
    result["result_id"] = _identity(result, "result_id")
    return result


def run_market_cognition_system_replay(
    payload: dict[str, Any], *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Replay a frozen multi-session system path or fail closed without metrics."""
    source = copy.deepcopy(payload) if isinstance(payload, dict) else {}
    errors = _protocol_errors()
    if source.get("schema") != "market_cognition_system_replay_input.v1":
        errors.append("input:schema_invalid")
    if source.get("mode") not in {"synthetic_contract", "strict_forward", "historical_PIT"}:
        errors.append("input:mode_invalid")
    if source.get("no_backfill") is not True:
        errors.append("input:no_backfill_required")
    sessions = source.get("sessions")
    if not isinstance(sessions, list) or not sessions:
        errors.append("input:frozen_sessions_missing_no_historical_signal_reconstruction")
    if errors:
        return _finish(status="fail_closed", source=source, errors=errors, trace=[])

    trace: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    prior_state: dict[str, Any] | None = None
    prior_decision_day: str | None = None
    prior_trade_date: str | None = None
    previous_equity: float | None = None
    initial_cost: float | None = None
    initial_notional: float | None = None
    peak: float | None = None
    max_drawdown = 0.0

    for index, session in enumerate(sessions):
        label = f"session:{index}"
        if not isinstance(session, dict) or session.get("schema") != "market_cognition_system_replay_session.v1":
            return _finish(status="fail_closed", source=source, errors=[f"{label}:schema_invalid"], trace=trace)
        bundle = session.get("strict_bundle")
        frozen_result = session.get("strict_result")
        if not isinstance(bundle, dict) or session.get("strict_bundle_sha256") != _digest(bundle):
            return _finish(status="fail_closed", source=source, errors=[f"{label}:strict_bundle_identity_invalid"], trace=trace)
        outcomes = _outcome_paths(bundle)
        if outcomes:
            return _finish(status="fail_closed", source=source, errors=[f"{label}:decision_outcome_field_forbidden:{path}" for path in outcomes], trace=trace)
        decision_day = str(bundle.get("decision_day") or "")
        decision_at = _time(bundle.get("decision_at"))
        execution = ((bundle.get("packets") or {}).get("A_share_execution_cost") or {})
        market_day = execution.get("market_day") or {}
        trade_date = str(market_day.get("trade_date") or "")
        execution_at = _time(execution.get("source_as_of"))
        if (
            _date(decision_day) is None or _date(trade_date) is None
            or decision_at is None or execution_at is None
            or not decision_at < execution_at
            or execution_at.date().isoformat() != trade_date
            or market_day.get("previous_trade_date") != decision_day
        ):
            return _finish(status="fail_closed", source=source, errors=[f"{label}:decision_execution_order_invalid"], trace=trace)
        envelope_errors = _execution_envelope_errors(
            bundle, execution, project_root=project_root,
        )
        if envelope_errors:
            return _finish(
                status="fail_closed", source=source,
                errors=[f"{label}:{item}" for item in envelope_errors], trace=trace,
            )
        if prior_decision_day is not None and (
            decision_day <= prior_decision_day or trade_date <= str(prior_trade_date)
            or decision_day != prior_trade_date
        ):
            return _finish(status="fail_closed", source=source, errors=[f"{label}:session_date_chain_invalid"], trace=trace)

        recomputed = run_strict_joint_replay(bundle, project_root=project_root)
        if (
            not isinstance(frozen_result, dict)
            or frozen_result.get("result_id") != _identity(frozen_result, "result_id")
            or _canonical_bytes(frozen_result) != _canonical_bytes(recomputed)
        ):
            return _finish(status="fail_closed", source=source, errors=[f"{label}:strict_result_reproduction_mismatch"], trace=trace)
        previous_state, ref_errors = _read_ref(
            execution.get("previous_state_ref"), project_root=project_root,
            label=f"{label}:previous_state",
        )
        if ref_errors or previous_state is None:
            return _finish(status="fail_closed", source=source, errors=ref_errors, trace=trace)
        if previous_state.get("state_id") != _identity(previous_state, "state_id"):
            return _finish(status="fail_closed", source=source, errors=[f"{label}:previous_state_id_invalid"], trace=trace)
        if previous_state.get("cost_model_id") != (execution.get("cost_model_ref") or {}).get("sha256"):
            return _finish(status="fail_closed", source=source, errors=[f"{label}:state_cost_model_identity_mismatch"], trace=trace)
        if prior_state is not None and _canonical_bytes(previous_state) != _canonical_bytes(prior_state):
            return _finish(status="fail_closed", source=source, errors=[f"{label}:previous_state_chain_broken"], trace=trace)

        if recomputed.get("status") == "replay_complete":
            state_after = ((recomputed.get("execution") or {}).get("state_after") or {})
            action = "executed"
        elif recomputed.get("zero_signal_reasons") == ["stock_candidate:no_executable_plan"]:
            state_after, carry_errors = _valid_no_trade_transition(bundle, previous_state)
            if carry_errors or state_after is None:
                return _finish(status="fail_closed", source=source, errors=[f"{label}:{item}" for item in carry_errors], trace=trace)
            action = "no_trade"
        else:
            reasons = recomputed.get("zero_signal_reasons") or ["strict_replay_incomplete"]
            return _finish(status="fail_closed", source=source, errors=[f"{label}:strict_fail_closed:{item}" for item in reasons], trace=trace)
        if state_after.get("state_id") != _identity(state_after, "state_id"):
            return _finish(status="fail_closed", source=source, errors=[f"{label}:state_after_id_invalid"], trace=trace)

        equity, mark_errors = _mark_errors(
            session.get("after_session_mark"), result_id=recomputed["result_id"],
            state=state_after, trade_date=trade_date, execution_at=execution_at,
        )
        if mark_errors or equity is None:
            return _finish(status="fail_closed", source=source, errors=[f"{label}:{item}" for item in mark_errors], trace=trace)
        if previous_equity is None:
            previous_equity, valuation_errors = _value_state(
                previous_state, _initial_prices(bundle, previous_state),
                label=f"{label}:initial_valuation",
            )
            if valuation_errors or previous_equity is None or previous_equity <= 0:
                return _finish(status="fail_closed", source=source, errors=valuation_errors or [f"{label}:initial_equity_invalid"], trace=trace)
            initial_cost = _state_cost(previous_state)
            initial_notional = _state_notional(previous_state)
            if initial_cost is None or initial_notional is None:
                return _finish(status="fail_closed", source=source, errors=[f"{label}:initial_cost_or_notional_invalid"], trace=trace)
            peak = previous_equity
            curve.append({"kind": "initial", "date": decision_day, "equity_yuan": round(previous_equity, 6), "drawdown_pct": 0.0})
        before_cost = _state_cost(previous_state)
        after_cost = _state_cost(state_after)
        if before_cost is None or after_cost is None or after_cost + 1e-9 < before_cost:
            return _finish(status="fail_closed", source=source, errors=[f"{label}:cumulative_cost_chain_invalid"], trace=trace)
        session_return = equity / previous_equity - 1.0
        peak = max(float(peak), equity)
        drawdown = equity / peak - 1.0
        max_drawdown = min(max_drawdown, drawdown)
        ledger = (recomputed.get("execution") or {}).get("ledger") or []
        market_state = str(((bundle.get("packets") or {}).get("market_state") or {}).get("market_state") or "")
        playbook = str(((bundle.get("packets") or {}).get("fixed_playbook") or {}).get("playbook_id") or "")
        row = {
            "decision_day": decision_day, "execution_date": trade_date,
            "market_state": market_state, "playbook_id": playbook,
            "action": action, "trade_count": len(ledger),
            "cost_delta": after_cost - before_cost,
            "session_return": session_return, "equity_yuan": equity,
        }
        rows.append(row)
        mark = session["after_session_mark"]
        curve.append({
            "kind": "session", "decision_day": decision_day,
            "execution_date": trade_date, "market_state": market_state,
            "playbook_id": playbook, "action": action,
            "trade_count": len(ledger), "cost_yuan": round(row["cost_delta"], 6),
            "equity_yuan": round(equity, 6),
            "session_return_pct": round(session_return * 100.0, 8),
            "drawdown_pct": round(drawdown * 100.0, 8),
            "strict_result_id": recomputed["result_id"],
            "state_id": state_after["state_id"], "mark_packet_id": mark["packet_id"],
            "zero_signal_reasons": recomputed.get("zero_signal_reasons") or [],
        })
        trace.append({
            "session": index, "decision_day": decision_day,
            "execution_date": trade_date, "status": "verified",
            "action": action, "state_id": state_after["state_id"],
        })
        previous_equity = equity
        prior_state = copy.deepcopy(state_after)
        prior_decision_day, prior_trade_date = decision_day, trade_date

    final_cost = _state_cost(prior_state or {})
    final_notional = _state_notional(prior_state or {})
    returns = [row["session_return"] for row in rows]
    average_equity = fmean(point["equity_yuan"] for point in curve)
    metrics = {
        "completed_sessions": len(rows),
        "trade_count": sum(row["trade_count"] for row in rows),
        "no_trade_sessions": sum(row["action"] == "no_trade" for row in rows),
        "no_trade_session_ratio_pct": round(sum(row["action"] == "no_trade" for row in rows) / len(rows) * 100.0, 8),
        "initial_equity_yuan": curve[0]["equity_yuan"],
        "final_equity_yuan": curve[-1]["equity_yuan"],
        "cost_after_total_return_pct": round((curve[-1]["equity_yuan"] / curve[0]["equity_yuan"] - 1.0) * 100.0, 8),
        "maximum_drawdown_pct": round(max_drawdown * 100.0, 8),
        "mean_session_return_pct": round(fmean(returns) * 100.0, 8),
        "session_return_volatility_pct": round(pstdev(returns) * 100.0, 8),
        "total_cost_yuan": round(float(final_cost) - float(initial_cost), 6),
        "turnover_pct_of_average_equity": round((float(final_notional) - float(initial_notional)) / average_equity * 100.0, 8),
        "cash_rate_for_sharpe": None,
        "sharpe_computed": False,
        "performance_claim_authorized": False,
    }
    conditional = {
        "market_state": _conditional(rows, "market_state"),
        "playbook": _conditional(rows, "playbook_id"),
        "interpretation": "descriptive conditional accounting, not causal attribution or production admission",
    }
    return _finish(
        status="replay_complete", source=source, errors=[], trace=trace,
        curve=curve, metrics=metrics, conditional=conditional,
    )


__all__ = ["run_market_cognition_system_replay"]
