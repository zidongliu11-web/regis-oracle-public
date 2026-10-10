"""Small, side-effect-free API response contracts used by the Flask app."""
from __future__ import annotations

import copy


from .opportunity_contracts import (
    _monitor_opportunities_snapshot,
    _compact_opportunities_snapshot,
    _compact_opportunity_item,
    _finite_number,
    _public_research_composite,
)


def _scanner_model_validation(snapshot: object) -> dict[str, object]:
    """Expose scanner evidence status without promoting a ranking by implication."""
    if not isinstance(snapshot, dict):
        return {
            "status": "unavailable",
            "research_only": True,
            "reason": "量化排名快照不可用",
            "source_data_as_of": None,
        }
    headline = snapshot.get("headline")
    headline = headline if isinstance(headline, dict) else {}
    explicit = str(
        headline.get("validation_status")
        or snapshot.get("validation_status") or ""
    ).strip().lower()
    source_data_as_of = (
        snapshot.get("data_asof") or snapshot.get("snapshot_date")
        or snapshot.get("as_of")
    )
    if headline.get("invalidated") is True or explicit in {
        "invalidated", "rejected", "failed", "no-go", "nogo",
    }:
        status = "failed_validation"
        reason = "历史模型已失效或被拒绝"
    elif explicit in {"passed", "validated", "production"}:
        status = "validated"
        reason = "来源显式记录为已验证"
    else:
        metrics = [
            _finite_number(headline.get(key))
            for key in ("annualized_pct", "alpha_pct", "sharpe")
        ]
        if any(value is not None and value <= 0 for value in metrics):
            status = "failed_validation"
            reason = "历史年化、Alpha 或 Sharpe 未通过正收益验证"
        else:
            status = "unverified"
            reason = "来源未提供可复核的通过状态"
    return {
        "status": status,
        "research_only": status != "validated",
        "reason": reason,
        "source_data_as_of": source_data_as_of,
        "generated_at": snapshot.get("generated_at"),
    }


def _compact_strategy_lab_evidence(payload: dict[str, object]) -> dict[str, object]:
    """Return scalar dashboard facts; period/scenario evidence stays opt-in."""
    headline = payload.get("headline") or {}
    shortest = payload.get("shortest_profit_path") or {}
    active_is_shortest = bool(shortest)
    active_headline = shortest or headline
    latest = payload.get("latest_strategy") or {}
    factor = payload.get("factor") or {}
    legacy_forward = (payload.get("stability_paper_forward") or {}).get(
        "formal_forward"
    ) or (payload.get("paper_forward") or {}).get("formal_forward") or {}
    forward = shortest.get("forward_tracking") or legacy_forward
    current_mark = forward.get("current_mark") or {}
    completed_marks = [
        row for row in forward.get("completed_close_observations") or []
        if isinstance(row, dict)
    ]
    formal_mark = completed_marks[-1] if completed_marks else {}
    execution = active_headline.get("capital_constrained_execution") or {}
    factor_ic = None if active_is_shortest else factor.get("latest_forward_ic")
    if factor_ic is None and not active_is_shortest:
        factor_ic = factor.get("forward_ic")
    return {
        "available": bool(payload.get("available")),
        "generated_at": forward.get("generated_at") or payload.get("generated_at"),
        "headline": {
            key: active_headline.get(key) for key in (
                "label", "status", "reason", "recommendation_eligible",
                "paper_forward_eligible", "signal_date",
            )
        },
        "evidence_view": "/api/strategy-lab",
        "summary": {
            "status": active_headline.get("status"),
            "reason": active_headline.get("reason"),
            "recommendation_eligible": active_headline.get(
                "recommendation_eligible"
            ),
            "method": (
                active_headline.get("label")
                or latest.get("title")
                or latest.get("strategy_id")
            ),
            "latest_return_pct": (
                None
                if active_is_shortest
                else current_mark.get("net_liquidation_return_pct")
                if current_mark.get("net_liquidation_return_pct") is not None
                else latest.get("forward_total_pct")
            ),
            "factor_ic": factor_ic,
            "forward_completed": (
                forward.get("completed_closes")
                if forward.get("completed_closes") is not None
                else forward.get("completed_post_freeze_common_closes")
            ),
            "forward_return_pct": (
                formal_mark.get("net_liquidation_return_pct")
                if formal_mark.get("net_liquidation_return_pct") is not None
                else forward.get("formal_forward_return_pct")
            ),
            "forward_max_drawdown_pct": forward.get("max_drawdown_pct"),
            "forward_latest_date": (
                forward.get("latest_completed_close_date")
                or forward.get("latest_observation_date")
            ),
            "normal_horizon_months": shortest.get("normal_horizon_months"),
            "moderate_stress_horizon_months": shortest.get(
                "moderate_stress_horizon_months"
            ),
            "severe_stress_horizon_months": shortest.get(
                "severe_stress_horizon_months"
            ),
            "historical_cost_scenario_bps_each_side": shortest.get(
                "historical_cost_scenario_bps_each_side"
            ),
            "historical_trading_days": shortest.get("historical_trading_days"),
            "historical_total_return_pct": shortest.get(
                "historical_total_return_pct"
            ),
            "historical_annualized_return_pct": shortest.get(
                "historical_annualized_return_pct"
            ),
            "historical_daily_max_drawdown_pct": shortest.get(
                "historical_daily_max_drawdown_pct"
            ),
            "historical_daily_max_drawdown_date": shortest.get(
                "historical_daily_max_drawdown_date"
            ),
            "safe_core_pct": (
                active_headline.get("cash_weight_pct")
                if active_headline.get("cash_weight_pct") is not None
                else active_headline.get("executable_safe_core_pct")
                if active_headline.get("executable_safe_core_pct") is not None
                else execution.get("post_trade_safe_core_pct")
            ),
            "risk_weight_pct": (
                active_headline.get("risk_exposure_pct")
                if active_headline.get("risk_exposure_pct") is not None
                else active_headline.get("executable_risk_exposure_pct")
                if active_headline.get("executable_risk_exposure_pct") is not None
                else execution.get("post_trade_risk_weight_pct")
            ),
            "residual_cash": execution.get("residual_cash"),
        },
    }


def _active_strategy_public_view(
    strategy_lab: object,
    fallback_headline: object = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return one public strategy identity and its matching paper allocation.

    ``headline`` and ``paper_allocation`` are rendered independently by the
    dashboard and signal center.  Deriving both here prevents a newer research
    path from being shown beside an older allocation.
    """
    lab = strategy_lab if isinstance(strategy_lab, dict) else {}
    shortest = lab.get("shortest_profit_path")
    if not isinstance(shortest, dict) or not shortest:
        fallback = (
            copy.deepcopy(fallback_headline)
            if isinstance(fallback_headline, dict) else {}
        )
        allocation = fallback.get("current_paper_allocation")
        return fallback, (
            copy.deepcopy(allocation) if isinstance(allocation, dict) else {}
        )

    execution = shortest.get("capital_constrained_execution")
    execution = execution if isinstance(execution, dict) else {}
    forward = shortest.get("forward_tracking")
    forward = forward if isinstance(forward, dict) else {}
    current_mark = forward.get("current_mark")
    current_mark = current_mark if isinstance(current_mark, dict) else {}
    completed_marks = [
        row for row in forward.get("completed_close_observations") or []
        if isinstance(row, dict)
    ]
    formal_mark = completed_marks[-1] if completed_marks else {}
    cash_weight = _finite_number(shortest.get("cash_weight_pct"))
    risk_weight = _finite_number(shortest.get("risk_exposure_pct"))
    cash_weight = 100.0 if cash_weight is None else cash_weight
    risk_weight = 0.0 if risk_weight is None else risk_weight

    cash_row: dict[str, object] = {
        "code": "511880",
        "name": "银华日利ETF",
        "role": "货币ETF现金收益底座",
        "weight_pct": cash_weight,
    }
    quantity = (
        (execution.get("quantities") or {}).get("511880")
        if isinstance(execution.get("quantities"), dict)
        else execution.get("quantity")
    )
    if quantity is not None:
        cash_row["quantity"] = quantity
    allocation_execution = copy.deepcopy(execution)
    allocation_execution.pop("forward_tracking", None)
    allocation_execution["capital_role"] = (
        "fixed_research_paper_diagnostic_not_user_account"
    )
    allocation_execution["uses_user_account_cash"] = False

    allocation = {
        "strategy_id": "V233_CASH_FLOOR_HOLDING_HORIZON",
        "status": shortest.get("status"),
        "signal_date": shortest.get("signal_date"),
        "data_as_of": shortest.get("signal_date"),
        "paper_only": True,
        "research_only": True,
        "recommendation_eligible": False,
        "stable_profit_proven": False,
        "cash_weight_pct": cash_weight,
        "risk_exposure_pct": risk_weight,
        "cash_sweep": copy.deepcopy(cash_row),
        "allocations": [cash_row],
        "risk_allocations": [],
        "capital_constrained_execution": allocation_execution,
    }
    headline = {
        **copy.deepcopy(shortest),
        "capital_constrained_execution": copy.deepcopy(allocation_execution),
        "method": shortest.get("label"),
        "metric_scope": "v233_cash_floor_holding_horizon_paper_observation",
        "historical_gate_passed": True,
        "invalidated": False,
        "paper_only": True,
        "research_observation_only": True,
        "stable_profit_proven": False,
        "automatic_trading": False,
        "current_paper_allocation": copy.deepcopy(allocation),
        "research_observation_allocation": copy.deepcopy(allocation),
        "formal_forward_status": forward.get("status"),
        "formal_forward_observations": forward.get("completed_closes"),
        "formal_forward_return_pct": formal_mark.get(
            "net_liquidation_return_pct"
        ),
        "formal_forward_max_drawdown_pct": forward.get("max_drawdown_pct"),
        "formal_forward_latest_date": forward.get(
            "latest_completed_close_date"
        ),
        "generated_at": forward.get("generated_at"),
    }
    return headline, allocation


def _compact_candidate_headline(headline: object) -> dict[str, object]:
    """Keep dashboard status facts without embedding allocation/evidence trees."""
    source = headline if isinstance(headline, dict) else {}
    fields = (
        "label", "status", "reason", "method", "metric_scope", "generated_at",
        "invalidated", "historical_gate_passed", "paper_only",
        "paper_forward_eligible", "recommendation_eligible",
        "formal_forward_status", "formal_forward_latest_date",
        "formal_forward_observations", "formal_forward_return_pct",
        "formal_forward_max_drawdown_pct", "diagnostic_total_return_pct",
        "diagnostic_max_drawdown_pct", "validation_total_return_pct",
        "validation_annualized_return_pct", "validation_max_drawdown_pct",
        "test_total_return_pct", "test_annualized_return_pct",
        "test_max_drawdown_pct",
    )
    return {key: source.get(key) for key in fields}


def _compact_live_signal_snapshot(snapshot: object) -> dict[str, object]:
    """Expose synchronization metadata once; signal rows stay at the top level."""
    source = snapshot if isinstance(snapshot, dict) else {}
    fields = (
        "generated_at", "served_at", "factor_as_of", "required_factor_as_of",
        "decision_eligible", "execution_mode", "automatic_order",
        "broker_connected", "market_status", "snapshot_age_seconds", "stale",
        "stale_reasons", "eligibility_blockers", "zero_signal_reasons",
        "latency_s", "n_factor_codes", "n_quotes",
        "n_watchlist", "watchlist_scope", "snapshot_health",
        "strategy_validation", "honesty",
    )
    return {key: source.get(key) for key in fields}


def _compact_decision_gate(gate: dict | None) -> dict:
    source = gate or {}
    registry = source.get("strategy_registry") or {}
    fields = (
        "status", "recommendation_eligible", "research_only", "reason",
        "active_strategy", "source", "source_status", "generated_at",
        "source_scope", "product_forward_clock_valid",
        "manual_real_money_enabled", "manual_consent", "broker_connected",
        "manual_stock_execution_source",
        "manual_research_alerts_allowed",
        "formal_strategy_recommendations_allowed",
        "automatic_trading", "broker_orders_enabled",
        "paper_forward_eligible", "stable_profit_proven",
    )
    compact = {key: source.get(key) for key in fields}
    compact["reason"] = source.get("compact_reason") or source.get("reason")
    compact["product_forward_clock"] = copy.deepcopy(
        source.get("product_forward_clock") or {}
    )
    compact["research_paper_forward"] = copy.deepcopy(
        source.get("research_paper_forward") or {}
    )
    compact["strategy_registry"] = {
        key: registry.get(key) for key in (
            "active_strategy", "recommendation_gate_passed",
            "historical_gate_passed", "historical_comparator",
            "historical_comparator_gate_passed", "active_research_path",
            "source", "generated_at",
        )
    }
    return compact




def _truthful_strategy_batch_status(raw: dict | None) -> dict:
    """把批次状态归一为 IS/历史闸门/BY/跨批 LOND/注册五段事实。"""
    out = copy.deepcopy(raw or {})
    out["one_way_cost_bps_role"] = "slippage_only"
    out["additional_trading_costs"] = "commission_stamp_tax_transfer_fee"
    results = out.get("results") if isinstance(out.get("results"), list) else []
    running = bool(out.get("running"))
    required = {
        "is_validation_passed", "gate_passed", "batch_fdr_passed",
        "global_lond_passed", "historical_confirmation_passed",
        "account_capital", "one_way_cost_bps",
    }
    strict_rows = all(
        required <= set(row) for row in results
        if isinstance(row, dict)
        and str(row.get("verdict") or "").upper() not in {"ERROR", "INVALIDATED"}
    )
    strict_batch = bool(
        out.get("fdr_method") == "BY" and out.get("fdr_q") is not None
        and out.get("global_fdr_method") == "LOND-dependent"
        and out.get("global_fdr_epoch")
    )
    unsupported_pass = any(
        isinstance(row, dict)
        and str(row.get("verdict") or "").upper() == "PASS"
        for row in results
    )
    legacy_invalidated = bool(results) and not running and (
        not (strict_rows and strict_batch) or unsupported_pass
    )
    invalidated = bool(out.get("invalidated")) or legacy_invalidated
    if invalidated:
        out["invalidated"] = True
        out["invalidated_reason"] = out.get("invalidated_reason") or (
            "旧批次缺少当前严格链路的 IS 验证、历史确认闸门、批内 BY、"
            "跨批 LOND 与注册成功五段证据；"
            "结果保留审计，但全部不计 PASS。"
        )

    normalized = []
    for source in results:
        if not isinstance(source, dict):
            continue
        row = dict(source)
        row["one_way_cost_bps_role"] = "slippage_only"
        row["additional_trading_costs"] = "commission_stamp_tax_transfer_fee"
        original_verdict = str(row.get("verdict") or "PENDING").upper()
        registration_passed = bool(
            original_verdict == "HISTORICAL_PASS"
            and row.get("historical_confirmation_passed") is True
            and row.get("strategy_id")
        )
        row["registration_passed"] = registration_passed
        row["legacy_verdict"] = original_verdict if invalidated else None
        if invalidated:
            row["passed"] = False
            row["historical_confirmation_passed"] = False
            row["verdict"] = "INVALIDATED"
            row["registration_passed"] = False
        elif original_verdict in {"ERROR", "INVALIDATED"}:
            row["passed"] = False
            row["historical_confirmation_passed"] = False
            row["verdict"] = original_verdict
            row["registration_passed"] = False
        else:
            five_stage_pass = bool(
                row.get("is_validation_passed")
                and row.get("gate_passed")
                and row.get("batch_fdr_passed")
                and row.get("global_lond_passed")
                and registration_passed
            )
            historical_pass = bool(
                original_verdict == "HISTORICAL_PASS" and five_stage_pass
            )
            row["passed"] = False
            row["historical_confirmation_passed"] = historical_pass
            row["verdict"] = "HISTORICAL_PASS" if historical_pass else (
                "PENDING" if original_verdict == "PENDING" else "NO-GO"
            )
        normalized.append(row)
    out["results"] = normalized
    out["pass_count"] = sum(row.get("verdict") == "PASS" for row in normalized)
    out["historical_pass_count"] = sum(
        row.get("verdict") == "HISTORICAL_PASS" for row in normalized
    )
    out["no_go_count"] = sum(row.get("verdict") == "NO-GO" for row in normalized)
    out["error_count"] = sum(row.get("verdict") == "ERROR" for row in normalized)
    out["invalidated_count"] = sum(
        row.get("verdict") == "INVALIDATED" for row in normalized)
    return out


def _historical_promotion_verdict(gate: dict | None) -> str:
    """历史确认通过只能写 HISTORICAL_PASS，不能生成可解锁 PASS。"""
    value = gate or {}
    verdict = str(value.get("verdict") or "NO-GO").upper()
    if verdict == "INVALIDATED":
        return "INVALIDATED"
    historical_passed = value.get(
        "historical_confirmation_passed", value.get("passed")
    )
    if verdict in {"PASS", "HISTORICAL_PASS"} and historical_passed is True:
        return "HISTORICAL_PASS"
    return "NO-GO"
