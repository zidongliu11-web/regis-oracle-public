"""Side-effect-free opportunity response contracts."""
from __future__ import annotations

import copy
import math

def _finite_number(value: object) -> float | None:
    """Return a finite numeric value, excluding booleans and malformed cells."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


_PUBLIC_RESEARCH_SCORE_BANDS = (
    (80.0, "高分研究候选"),
    (65.0, "较高分研究候选"),
    (50.0, "中性研究候选"),
    (35.0, "低分研究候选"),
    (0.0, "排除级研究候选"),
)


def _public_research_composite(composite: object) -> dict[str, object]:
    """Remove trade-action wording from a stock score at the public API edge.

    The internal screening pipeline still uses its frozen numeric thresholds.
    Public clients receive a research score band plus a separate, non-trading
    review state; a cross-sectional score must never look like an order.
    """
    source = composite if isinstance(composite, dict) else {}
    score = _finite_number(source.get("composite"))
    research_band = str(source.get("research_band") or "").strip()
    if not research_band and score is not None:
        research_band = next((
            label for lower, label in _PUBLIC_RESEARCH_SCORE_BANDS
            if score >= lower
        ), "排除级研究候选")
    if not research_band:
        research_band = "评分数据不足"

    raw_action = str(
        source.get("action_rating") or source.get("rating") or ""
    ).strip()
    if raw_action in {"排除", "卖出"}:
        review_state = "排除新开仓"
    elif raw_action == "回避":
        review_state = "风险复核"
    elif raw_action == "中性":
        review_state = "继续观察"
    elif score is None:
        review_state = "等待数据"
    else:
        review_state = "研究筛选通过"
    return {
        "score_band_rating": research_band,
        "score_rating": research_band,
        "rating": research_band,
        "action_rating": review_state,
        "rating_scope": "research_only_not_trade_recommendation",
        "trade_recommendation": False,
    }


def _compact_opportunity_item(item: object) -> dict[str, object]:
    """Keep the fields rendered by the desktop monitor; full evidence stays opt-in."""
    source = item if isinstance(item, dict) else {}
    fields = (
        "code", "name", "price", "open_price", "prev_close", "change_pct", "score", "level",
        "recommended_action", "streak_days", "alert", "price_fit",
        "buy_window", "source",
    )
    compact = {key: source.get(key) for key in fields if key in source}
    position = source.get("position") if isinstance(source.get("position"), dict) else None
    if position is not None:
        compact["position"] = {
            key: position.get(key)
            for key in ("id", "shares", "price", "code", "name")
            if key in position
        }
    trade_plan = (
        source.get("trade_plan")
        if isinstance(source.get("trade_plan"), dict) else None
    )
    if trade_plan is not None:
        action = (
            trade_plan.get("action")
            if isinstance(trade_plan.get("action"), dict) else None
        )
        compact["trade_plan"] = {
            "state": trade_plan.get("state"),
            "action": (
                {
                    key: copy.deepcopy(action.get(key))
                    for key in (
                        "side", "shares", "target_shares", "limit_price",
                        "valid_until", "cancel_condition", "estimated_cost",
                        "playbook_id", "strategy_id", "strategy_version",
                        "evidence_grade", "as_of", "evidence_identity",
                        "manual_confirmation",
                    ) if key in action
                }
                if action is not None else None
            ),
        }
    quality = (
        source.get("opportunity_quality")
        if isinstance(source.get("opportunity_quality"), dict) else {}
    )
    compact["opportunity_quality"] = {
        key: quality.get(key) for key in (
            "method", "stage", "rank_score", "pre_rank_score",
            "market_diagnostic_score",
            "expected_return_20d_pct", "expected_return_status",
            "ranking_return_basis", "trailing_20d_pct",
            "max_drawdown_60d_pct", "annualized_volatility_pct",
            "avg_amount_20d_yuan", "comparison_role",
            "comparison_capital_yuan", "comparison_shares",
            "comparison_notional_yuan", "round_trip_cost_pct",
            "participation_pct", "data_as_of", "data_source",
            "price_return_basis", "corporate_action_safe_returns",
            "soft_evidence_missing",
        ) if key in quality
    }
    components = quality.get("components") if isinstance(quality.get("components"), dict) else {}
    compact["opportunity_quality"]["components"] = {
        name: {
            key: row.get(key) for key in (
                "label", "score", "weight_pct", "contribution",
            ) if key in row
        }
        for name, row in components.items() if isinstance(row, dict)
    }
    deep = quality.get("deep_components") if isinstance(quality.get("deep_components"), dict) else {}
    compact["opportunity_quality"]["deep_components"] = {
        name: {
            key: row.get(key)
            for key in ("method", "score", "weight_pct", "role") if key in row
        }
        for name, row in deep.items() if isinstance(row, dict)
    }
    factor_signal = (
        quality.get("production_factor_signal")
        if isinstance(quality.get("production_factor_signal"), dict) else None
    )
    compact["opportunity_quality"]["production_factor_signal"] = (
        {
            key: factor_signal.get(key) for key in (
                "action", "score", "factor_z", "momentum_z", "factor_as_of",
                "execution_eligible", "reason",
            ) if key in factor_signal
        }
        if factor_signal else None
    )
    kronos = source.get("kronos") if isinstance(source.get("kronos"), dict) else {}
    compact["kronos"] = {"pred_ret_20d": kronos.get("pred_ret_20d")}

    alert = source.get("alert") if isinstance(source.get("alert"), dict) else {}
    signals = source.get("signals") if isinstance(source.get("signals"), dict) else {}
    compact["signals"] = {
        key: (
            {
                field: row.get(field) for field in ("state", "score", "detail")
                if field in row
            }
            if alert.get("status") == "triggered" else {"state": row.get("state")}
        )
        for key, row in signals.items() if isinstance(row, dict)
    }

    levels = source.get("levels") if isinstance(source.get("levels"), dict) else {}
    compact["levels"] = {
        "risk_levels": [
            {
                key: row.get(key) for key in (
                    "active", "executable", "kind", "priority", "source", "value",
                )
            }
            for row in (levels.get("risk_levels") or []) if isinstance(row, dict)
        ],
        "effective_risk_level": None,
    }

    company = source.get("company") if isinstance(source.get("company"), dict) else {}
    composite = (
        company.get("composite")
        if isinstance(company.get("composite"), dict) else {}
    )
    compact["company"] = {
        key: company.get(key) for key in (
            "ready", "message", "veto", "vetoed_gates", "warning_gates",
            "company_score",
        )
    }
    compact["company"]["composite"] = {
        key: composite.get(key) for key in (
            "composite", "score_rating", "action_rating", "rating", "tier",
            "tier_key", "pass_", "as_of_audit",
        )
    }
    company_composite = compact["company"]["composite"]
    company_composite.update(_public_research_composite(composite))

    assessment = (
        source.get("assessment")
        if isinstance(source.get("assessment"), dict) else {}
    )
    gates = assessment.get("gates") if isinstance(assessment.get("gates"), dict) else {}
    compact["assessment"] = {
        key: assessment.get(key) for key in (
            "action_state", "as_of", "blocking_gates", "can_alert",
            "execution_eligible", "failed_gates", "manual_actionable",
            "passed_gates", "pending_gates", "reason", "source", "verdict",
            "watch_gates", "research_only",
        )
    }
    gate_fields = {
        "quote": ("ok", "freshness", "detail"),
        "exclusion": ("ok", "excluded", "blacklisted", "detail"),
        "five_gate": (
            "ok", "ready", "veto", "vetoed_gates", "company_score", "detail",
        ),
        "timing": (
            "ok", "score", "level", "phase", "window_label", "metric_name",
            "metric_label", "interpretation", "calibrated_probability",
            "evidence_grade", "available_signal_count", "total_signal_count",
            "coverage_pct", "unavailable_signals", "detail",
        ),
        "composite": (
            "ok", "composite", "score_rating", "action_rating", "rating",
            "tier", "tier_key", "bar",
            "bar_label", "veto_verification_complete", "unverified_veto_rules",
            "as_of_audit", "detail",
        ),
        "price_fit": ("ok", "fit", "label", "detail"),
        "model": (
            "ok", "applied", "status", "coverage", "positive", "model_score",
            "pred_ret_20d", "validation_status", "freshness_status",
            "decision_eligible", "research_only", "detail",
        ),
        "risk": (
            "research_only", "recommendation_eligible", "manual_consent",
            "broker_orders_enabled",
        ),
    }
    compact["assessment"]["gates"] = {
        gate: {key: row.get(key) for key in gate_fields[gate] if key in row}
        for gate, row in gates.items()
        if gate in gate_fields and isinstance(row, dict)
    }

    # Old clients still read ``score`` as the stock score.  Make that alias the
    # one professional composite; entry timing remains available only under
    # its explicit ``condition_match_score`` name.
    timing_gate = gates.get("timing") if isinstance(gates.get("timing"), dict) else {}
    composite_gate = (
        gates.get("composite")
        if isinstance(gates.get("composite"), dict) else {}
    )
    public_rating = _public_research_composite(composite_gate)
    score_band_rating = public_rating["score_band_rating"]
    action_rating = public_rating["action_rating"]
    compact_composite_gate = compact["assessment"]["gates"].get("composite")
    if isinstance(compact_composite_gate, dict):
        compact_composite_gate.update(public_rating)
    compact.update({
        "composite_score": composite_gate.get("composite"),
        "score": composite_gate.get("composite"),
        "score_band_rating": score_band_rating,
        "score_rating": public_rating["score_rating"],
        "action_rating": action_rating,
        "condition_match_score": timing_gate.get("score", source.get("score")),
        "score_semantics": {
            "primary_score_field": "composite_score",
            "primary_score_label": "统一专业综合评分",
            "primary_rating_field": "action_rating",
            "primary_rating_label": "研究复核状态",
            "score_band_rating_field": "score_band_rating",
            "score_band_rating_label": "研究分档",
            "score_band_rating_is_recommendation": False,
            "entry_timing_field": "condition_match_score",
            "entry_timing_label": "盘中条件匹配分",
            "entry_timing_is_overall_score": False,
            "ranking_score_field": "composite_score",
            "ranking_score_label": "统一专业综合评分",
            "ranking_score_is_overall_score": True,
            "score_alias_of": "composite_score",
        },
    })
    if compact["composite_score"] is not None:
        compact["opportunity_quality"]["rank_score"] = compact["composite_score"]

    strategy_ref = (
        source.get("strategy_ref")
        if isinstance(source.get("strategy_ref"), dict) else {}
    )
    compact["strategy_ref"] = {
        key: strategy_ref.get(key) for key in (
            "origin", "strategy_kind", "strategy_id", "strategy_version",
            "strategy_spec_sha256", "catalog_version",
            "runtime_data_key", "factor_as_of", "signal_id", "production_eligible",
            "evidence_ref",
        ) if key in strategy_ref
    }
    for ref_key in (
        "cost_after_validation_ref", "out_of_sample_validation_ref",
        "production_admission_ref",
    ):
        value = strategy_ref.get(ref_key)
        if isinstance(value, dict):
            compact["strategy_ref"][ref_key] = {
                key: value.get(key) for key in ("path", "sha256") if key in value
            }
    strict_route_ref = strategy_ref.get("strict_route_ref")
    if isinstance(strict_route_ref, dict):
        compact["strategy_ref"]["strict_route_ref"] = {
            key: copy.deepcopy(strict_route_ref.get(key))
            for key in (
                "packet_id", "slot_id", "decision_day", "decision_at",
                "market_state", "playbook_id", "account_snapshot_sha256",
            ) if key in strict_route_ref
        }
    validation_evidence = strategy_ref.get("validation_evidence")
    if isinstance(validation_evidence, dict):
        compact["strategy_ref"]["validation_evidence"] = {
            key: validation_evidence.get(key)
            for key in ("path", "sha256") if key in validation_evidence
        }
    return compact


def _compact_observation_item(item: object) -> dict[str, object]:
    """Return the small card contract used for watch/rejected rows.

    Observation rows are never executable plans.  Keeping the full signal,
    company, component and strategy evidence for every row made the dashboard
    transfer the same large research packet every polling cycle.  The full
    evidence remains available from ``view=evidence``; this projection keeps
    only what the dashboard and live-scan fallback actually render.
    """
    item_contract = _compact_opportunity_item(item)
    assessment = item_contract.get("assessment")
    assessment = assessment if isinstance(assessment, dict) else {}
    gates = assessment.get("gates")
    gates = gates if isinstance(gates, dict) else {}
    composite = gates.get("composite")
    composite = composite if isinstance(composite, dict) else {}
    quality = item_contract.get("opportunity_quality")
    quality = quality if isinstance(quality, dict) else {}
    result = {
        key: copy.deepcopy(item_contract.get(key))
        for key in (
            "code", "name", "price", "change_pct", "level",
            "recommended_action", "streak_days", "alert", "buy_window",
            "composite_score", "score", "score_band_rating",
            "score_rating", "action_rating", "condition_match_score",
        )
        if key in item_contract
    }
    result["opportunity_quality"] = {
        key: copy.deepcopy(quality.get(key))
        for key in (
            "expected_return_20d_pct", "expected_return_status",
            "ranking_return_basis", "trailing_20d_pct",
            "max_drawdown_60d_pct", "annualized_volatility_pct",
            "round_trip_cost_pct", "avg_amount_20d_yuan", "data_as_of",
        )
        if key in quality
    }
    result["assessment"] = {
        key: copy.deepcopy(assessment.get(key))
        for key in (
            "action_state", "as_of", "failed_gates", "pending_gates",
            "reason", "verdict", "research_only",
        )
        if key in assessment
    }
    result["assessment"]["gates"] = {
        "composite": {
            key: copy.deepcopy(composite.get(key))
            for key in (
                "composite", "score_band_rating", "score_rating", "rating",
                "action_rating", "as_of_audit",
            )
            if key in composite
        }
    }
    return result


def _compact_position_alert_item(item: object) -> dict[str, object]:
    """Keep only the server alert identity joined back onto a live position."""
    raw = item if isinstance(item, dict) else {}
    alert = raw.get("alert") if isinstance(raw.get("alert"), dict) else {}
    position = (
        raw.get("position") if isinstance(raw.get("position"), dict) else {}
    )
    compact = {
        "code": raw.get("code"),
        "name": raw.get("name"),
        "alert": {
            key: copy.deepcopy(alert.get(key))
            for key in (
                "id", "status", "action", "reason", "created_at",
                "triggered_at", "updated_at", "valid_until", "window_label",
                "countdown_seconds", "provenance", "risk_observation",
            ) if key in alert
        },
        "position": {
            key: position.get(key)
            for key in ("id", "code", "name", "shares", "price")
            if key in position
        },
    }
    trade_plan = _compact_opportunity_item(raw).get("trade_plan")
    if trade_plan is not None:
        compact["trade_plan"] = trade_plan
    return compact


def _compact_opportunities_snapshot(snapshot: object) -> dict[str, object]:
    source = snapshot if isinstance(snapshot, dict) else {}
    fields = (
        "date", "market_phase", "buy_window", "kronos_model", "rejected_count",
        "watch_count", "research_approved_count", "waiting_entry_count",
        "trigger", "monitor_interval_s", "browser_poll_interval_s", "error", "candidate_count",
        "assessed_count", "assessment_budget", "assessment_strategy",
        "candidate_source_counts", "pipeline_identity", "monitoring_scope",
        "signal_status", "manual_forward_evidence", "account_execution",
        "market_state_forward_capture", "h3_execution_forward_capture",
        "market_news_forward_capture",
        "sector_constituent_quote_panel_capture",
    )
    compact = {key: source.get(key) for key in fields if key in source}
    buy_window = compact.get("buy_window")
    if isinstance(buy_window, dict):
        compact["buy_window"] = {
            **copy.deepcopy(buy_window),
            "role": "time_window_only_not_trade_permission",
            "trade_action_authority": False,
        }
    account_execution = compact.get("account_execution")
    if isinstance(account_execution, dict):
        compact["account_execution"] = {
            **copy.deepcopy(account_execution),
            "role": "account_prerequisite_status_only",
            "new_buy_plan_eligible_semantics": (
                "account_inputs_ready_not_strategy_route_authorized"
            ),
            "trade_action_authority": False,
            "authorization_endpoint": "/api/strategy-route/research",
        }
    compact["items"] = [
        _compact_opportunity_item(item) for item in (source.get("items") or [])
    ]
    compact["model_evidence_items"] = [
        _compact_opportunity_item(item)
        for item in (source.get("model_evidence_items") or [])
    ]
    compact["watch_items"] = [
        _compact_observation_item(item)
        for item in (source.get("watch_items") or [])
    ]
    compact["position_alerts"] = [
        _compact_position_alert_item(item)
        for item in (source.get("position_alerts") or [])
    ]
    compact["rejected"] = [
        _compact_observation_item(item)
        for item in (source.get("rejected") or [])
    ]
    # The opportunity engine may have completed an account-sized manual draft
    # before the strict same-slot route is available.  Compact/live clients must
    # not turn that draft into a buy alert.  The evidence view retains the raw
    # record for audit; only the today-decision route can re-authorize it.
    withheld_buy_count = 0
    for item in compact["items"]:
        alert = item.get("alert") if isinstance(item, dict) else None
        if not isinstance(alert, dict):
            continue
        if (
            alert.get("status") == "triggered"
            and str(alert.get("action") or "").lower() == "buy"
        ):
            withheld_buy_count += 1
            item["alert"] = {
                **alert,
                "status": "awaiting_strict_today_decision",
                "action": None,
                "reason": (
                    "账户计划草稿已形成；等待严格同槽市场、板块、战法与候选路由授权"
                ),
            }
            item["trade_plan"] = None
    trigger = compact.get("trigger")
    if (
        isinstance(trigger, dict)
        and str(trigger.get("action") or "").lower() == "buy"
    ):
        compact["trigger"] = None
    signal_status = compact.get("signal_status")
    if isinstance(signal_status, dict):
        raw_buy_count = int(
            _finite_number(signal_status.get("triggered_buy_count")) or 0
        )
        sell_count = int(
            _finite_number(signal_status.get("triggered_sell_count")) or 0
        )
        compact["signal_status"] = {
            **copy.deepcopy(signal_status),
            "triggered_buy_count": 0,
            "triggered_count": sell_count,
            "withheld_buy_plan_count": max(withheld_buy_count, raw_buy_count),
            "buy_action_authority": "/api/strategy-route/research",
        }
    compact["evidence_view"] = "/api/opportunities?view=evidence"
    return compact


def _monitor_opportunities_snapshot(snapshot: object) -> dict[str, object]:
    """Return only the facts needed by the always-on account monitor.

    The dashboard polls while it is open.  Re-projecting and transferring every
    research/watch/rejected row on each poll adds latency without changing an
    alert.  Buy authority remains exclusively in the strict strategy-route
    response; this contract carries only account sell-alert identities and
    small market-window counters.
    """
    source = snapshot if isinstance(snapshot, dict) else {}
    fields = (
        "date", "market_phase", "buy_window", "candidate_count",
        "research_approved_count", "waiting_entry_count", "watch_count",
        "rejected_count", "monitor_interval_s", "browser_poll_interval_s",
        "account_execution", "signal_status", "error",
        "sector_constituent_quote_panel_capture",
    )
    compact = {
        key: copy.deepcopy(source.get(key))
        for key in fields if key in source
    }
    buy_window = compact.get("buy_window")
    if isinstance(buy_window, dict):
        compact["buy_window"].update({
            "role": "time_window_only_not_trade_permission",
            "trade_action_authority": False,
        })
    account_execution = compact.get("account_execution")
    if isinstance(account_execution, dict):
        compact["account_execution"].update({
            "role": "account_prerequisite_status_only",
            "new_buy_plan_eligible_semantics": (
                "account_inputs_ready_not_strategy_route_authorized"
            ),
            "trade_action_authority": False,
            "authorization_endpoint": "/api/strategy-route/research",
        })
    compact["items"] = []
    compact["position_alerts"] = [
        _compact_position_alert_item(item)
        for item in (source.get("position_alerts") or [])
    ]
    signal_status = compact.get("signal_status")
    if isinstance(signal_status, dict):
        raw_buy_count = int(
            _finite_number(signal_status.get("triggered_buy_count")) or 0
        )
        sell_count = int(
            _finite_number(signal_status.get("triggered_sell_count")) or 0
        )
        compact["signal_status"].update({
            "triggered_buy_count": 0,
            "triggered_count": sell_count,
            "withheld_buy_plan_count": raw_buy_count,
            "buy_action_authority": "/api/strategy-route/research",
        })
    compact["trigger"] = None
    compact["evidence_view"] = "/api/opportunities?view=evidence"
    compact["contract_view"] = "monitor"
    compact["trade_action_authority"] = False
    return compact
