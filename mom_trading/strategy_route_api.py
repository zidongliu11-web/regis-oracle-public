"""Compose the research-only market/sector route API payload."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .market_sector_strategy_adapter import (
    PROTOCOL_PATH,
    PROTOCOL_SHA256,
    REQUIRED_PERCENTILES,
    adapt_market_observation,
    adapt_sector_snapshot,
)
from .market_state_daily import PROJECT_ROOT
from .market_state_transition_forecast import load_transition_forecast
from .intraday_emotion_scope_v2 import (
    build_scoped_intraday_emotion_observation as build_intraday_emotion_observation,
    scope_v1_emotion_observation,
    scoped_strict_route_alert_context as strict_route_alert_context,
)
from .research_exposure_review import build_research_exposure_review
from .strategy_router import build_research_strategy_route
from .mainline_diagnostics import build_mainline_diagnostics
from .system_route_joint_v2_forward import system_route_joint_v2_forward_status
from .trader_style_candidate_interpretation import (
    interpret_candidate_with_trader_style_lenses,
)
from .trader_style_distillation import (
    build_trader_style_lenses,
    load_small_capital_playbook_library,
    load_trader_style_distillation,
)


_STRATEGY_COHORT_PATH = PROJECT_ROOT / "research/STATE_ROUTED_STRATEGY_COHORT_V1.json"
_STRATEGY_COHORT_SHA256 = "19623fc154e9eca1333e3cca21e1c8623077f211b484dafc4058321d196dc8dd"
_MAINLINE_TERMINAL_PATH = (
    PROJECT_ROOT
    / "research/STATE_CONDITIONED_MAINLINE_SWING_V1_TERMINAL_ARCHIVE.json"
)
_MAINLINE_TERMINAL_SHA256 = (
    "43fa4503926fde0f3920eb7e69c72146f422001f773635018d6712f4a0efab47"
)
_T1_REVERSAL_TERMINAL_PATH = (
    PROJECT_ROOT
    / "research/STATE_CONDITIONED_T1_REVERSAL_V1_TERMINAL_ARCHIVE.json"
)
_T1_REVERSAL_TERMINAL_SHA256 = (
    "c5fcb35ae5bc1bad4d6fd852a8fe94e82e9637bf13193bb323e024db964541f2"
)
_LOW_VOLATILITY_TERMINAL_PATH = (
    PROJECT_ROOT
    / "research/STATE_CONDITIONED_SECTOR_LOW_VOLATILITY_252D_V1_TERMINAL_ARCHIVE.json"
)
_LOW_VOLATILITY_TERMINAL_SHA256 = (
    "eec2a65f18f3b0d3b119a38e1c5c624b5850734853bf0aa27dbcaee761487fd4"
)
_OPERATING_SYSTEM_PATH = (
    PROJECT_ROOT
    / "research/REGIS_MARKET_COGNITION_OPERATING_SYSTEM_V1_AMENDMENT_7.json"
)
_OPERATING_SYSTEM_SHA256 = (
    "2f622d976349071f11e724f0b967325acf98f5e781aec2e4933f28cf75becdb9"
)
_COMPLETED_REVIEW_RELEASE_PATH = (
    PROJECT_ROOT
    / "research/CURRENT_MARKET_ROUTE_RESEARCH_RELEASE_V1_AMENDMENT_49.json"
)
_COMPLETED_REVIEW_RELEASE_SHA256 = (
    "633a10b6064b3a106010aeb53bff76d840e9603a4689a018086dffe002cf887a"
)
_RELEASE_BOUND_MARKET_OBSERVATION_SHA256 = (
    "ab0c0843b64b146f1683a27011d10098dae6884fdbd1701410786c9762c1d24f"
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _shanghai_now() -> datetime:
    return datetime.now(_SHANGHAI)


def _active_deadline(value: Any, now: datetime | None = None) -> bool:
    text = _text(value)
    if text is None:
        return False
    try:
        deadline = datetime.fromisoformat(text)
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=_SHANGHAI)
    current = now or _shanghai_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=_SHANGHAI)
    return deadline > current
_SYSTEM_REPLAY_COVERAGE_PATH = (
    PROJECT_ROOT / "research/SYSTEM_ROUTE_HISTORICAL_PIT_COVERAGE_AUDIT_V1.json"
)
_SYSTEM_REPLAY_COVERAGE_SHA256 = "a4fe92af13ad207f4f2c6a2bb002ed7051eb8b1ce9cc2b61430a2e3838b6b79d"
_SYSTEM_REPLAY_PROTOCOL_V2_PATH = (
    PROJECT_ROOT / "research/NONCONVENTIONAL_MARKET_COGNITION_REPLAY_PROTOCOL_V2.json"
)
_SYSTEM_REPLAY_PROTOCOL_V2_SHA256 = "90141b329c730f9e35e2809a7a84f383a3dbf94f6ba8cd283b3a39221eb28724"
_SYSTEM_REPLAY_ADAPTER_PROTOCOL_V6_PATH = (
    PROJECT_ROOT / "research/SYSTEM_ROUTE_JOINT_V2_ADAPTER_PROTOCOL_V10.json"
)
_SYSTEM_REPLAY_ADAPTER_PROTOCOL_V6_SHA256 = (
    "0bf4bf7905ece192460097424e308adbe54821eea6fbe587bf60fa5f970428a3"
)
_SYSTEM_REPLAY_PERSISTENCE_V2_AMENDMENT_5_PATH = (
    PROJECT_ROOT
    / "research/SYSTEM_ROUTE_JOINT_V2_FORWARD_PERSISTENCE_PROTOCOL_V2_AMENDMENT_10.json"
)
_SYSTEM_REPLAY_PERSISTENCE_V2_AMENDMENT_5_SHA256 = (
    "220fc81b6256609803abf37b9086fbb5d0ba857f5b792660ff2a79c3cf43532a"
)
_SYSTEM_REPLAY_FORWARD_ADMISSION_V29_PATH = (
    PROJECT_ROOT
    / "research/SYSTEM_ROUTE_JOINT_V2_FORWARD_DEPLOYMENT_ADMISSION_V39.json"
)
_SYSTEM_REPLAY_FORWARD_ADMISSION_V29_SHA256 = (
    "6daadbe0a93e44a2bc17882cb6dcf630a5c266a589b6181f9275c1c4092f1caf"
)
_SYSTEM_REPLAY_ENVELOPE_AMENDMENT_31_PATH = (
    PROJECT_ROOT
    / "research/SYSTEM_ROUTE_JOINT_ENVELOPE_V2_PROTOCOL_AMENDMENT_45.json"
)
_SYSTEM_REPLAY_ENVELOPE_AMENDMENT_31_SHA256 = (
    "cb0ad4b63b2d8e461c72c4ceecca054387cd1a479f72ce4e75728a7e33f7aef4"
)
_SYSTEM_REPLAY_GATE_PATH = (
    PROJECT_ROOT / "mom_trading/system_route_joint_v2_gate.py"
)
_SYSTEM_REPLAY_GATE_SHA256 = (
    "d48096c0f71587bc215b3bb8e94c9ce044140919927e56f52e82f28b979534bc"
)
_SYSTEM_REPLAY_ADAPTER_PATH = (
    PROJECT_ROOT / "mom_trading/system_route_joint_v2_adapter.py"
)
_SYSTEM_REPLAY_DRIVER_PATH = (
    PROJECT_ROOT / "mom_trading/system_route_replay_driver.py"
)
_SYSTEM_REPLAY_IMPLEMENTATIONS = {
    "multi_session_engine": (
        PROJECT_ROOT / "mom_trading/market_cognition_system_replay.py",
        "be62a2dd95e0af1c6d7f1ace04936a5a7e9bc978f1ed9852e3288391bfcdc6c6",
    ),
}

_CANDIDATE_INTERPRETATION_LIMIT = 12


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _positive_integer(value: Any) -> int | None:
    number = _number(value)
    if number is None or number <= 0 or not number.is_integer():
        return None
    return int(number)


def _nonnegative_integer(value: Any) -> int | None:
    number = _number(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    return int(number)


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _release_bound_market_observation(
    *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any] | None:
    """Load the display-only cloud seed when the full daily chain is absent."""
    path = (
        project_root
        / "mom_trading/static/reference/latest_verified_market_route_observation.json"
    )
    try:
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != _RELEASE_BOUND_MARKET_OBSERVATION_SHA256:
            raise ValueError("release_bound_observation_sha_invalid")
        document = json.loads(raw)
        percentiles = document.get("percentiles_pct") or {}
        authority = document.get("authority") or {}
        required_false_authority = (
            "changes_live_score",
            "changes_live_trade_count",
            "changes_live_position_or_quantity",
            "generates_buy_or_sell_alert",
            "production_strategy",
            "automatic_trading",
            "stable_profit_claim",
        )
        if (
            document.get("schema") != "release_bound_market_route_observation.v1"
            or document.get("status") != "audited_diagnostic_not_validated"
            or document.get("state") not in {"RISK_OFF", "STABILIZING", "RISK_ON", "OVERHEATED"}
            or document.get("raw_state") not in {"RISK_OFF", "STABILIZING", "RISK_ON", "OVERHEATED"}
            or not document.get("trade_date")
            or document.get("profitability_tested") is not False
            or document.get("applied_to_live_positions") is not False
            or document.get("applied_to_score") is not False
            or any(authority.get(key) is not False for key in required_false_authority)
            or any(
                _number(percentiles.get(key)) is None
                or not 0 <= float(percentiles[key]) <= 100
                for key in REQUIRED_PERCENTILES
            )
        ):
            raise ValueError("release_bound_observation_boundary_invalid")
        observation = copy.deepcopy(document)
        observation["observation_source"] = {
            "path": str(path.relative_to(project_root)),
            "sha256": _RELEASE_BOUND_MARKET_OBSERVATION_SHA256,
            "kind": "release_bound_verified_daily_feature_projection",
            "full_runtime_chain_present": False,
        }
        return observation
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _first_number(*values: Any) -> float | None:
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return None


def _plan_evidence_metadata(
    subject: dict[str, Any], trade_plan: dict[str, Any],
    action: dict[str, Any], assessment: dict[str, Any],
) -> dict[str, Any]:
    evidence = action.get("evidence_identity")
    evidence = evidence if isinstance(evidence, dict) else {}
    strategy_ref = subject.get("strategy_ref")
    strategy_ref = strategy_ref if isinstance(strategy_ref, dict) else {}
    playbook_id = _text(
        action.get("playbook_id") or trade_plan.get("playbook_id")
        or subject.get("playbook_id") or strategy_ref.get("playbook_id")
    )
    evidence_grade = _text(
        action.get("evidence_grade") or evidence.get("evidence_grade")
        or assessment.get("evidence_grade") or subject.get("evidence_grade")
    )
    raw_grades = action.get("evidence_grades") or evidence.get("evidence_grades")
    evidence_grades = [
        value for value in (
            _text(item) for item in raw_grades
        ) if value
    ] if isinstance(raw_grades, list) else []
    if evidence_grade and evidence_grade not in evidence_grades:
        evidence_grades.insert(0, evidence_grade)
    as_of = _text(
        action.get("as_of") or assessment.get("as_of") or subject.get("as_of")
    )
    public_identity = {
        key: value for key in (
            "id", "packet_id", "method", "protocol_id", "sha256",
            "strategy_id", "strategy_version", "slot_id", "decision_day",
            "evidence_grade", "evidence_grades",
        ) if (value := evidence.get(key)) not in (None, "", [])
    }
    evidence_id = next((
        _text(public_identity.get(key))
        for key in ("id", "packet_id", "method", "protocol_id", "sha256")
        if _text(public_identity.get(key))
    ), None)
    strategy_id = _text(
        action.get("strategy_id") or trade_plan.get("strategy_id")
        or strategy_ref.get("strategy_id") or evidence.get("strategy_id")
    )
    strategy_version = _text(
        action.get("strategy_version") or trade_plan.get("strategy_version")
        or strategy_ref.get("catalog_version")
        or evidence.get("strategy_version")
    )
    strategy_role = _text(
        action.get("strategy_role") or trade_plan.get("strategy_role")
        or strategy_ref.get("origin")
    )
    return {
        "playbook_id": playbook_id,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "strategy_role": strategy_role,
        "evidence_identity": public_identity,
        "evidence_id": evidence_id,
        "evidence_grade": evidence_grade,
        "evidence_grades": evidence_grades,
        "as_of": as_of,
        "ready": bool(
            playbook_id and strategy_id and strategy_version
            and evidence_id and evidence_grade and as_of
        ),
    }


def _strict_candidate_route_metadata(
    candidate: dict[str, Any], route: dict[str, Any],
) -> dict[str, str] | None:
    """Return the one causal route identity allowed to label a buy plan.

    A manual execution policy is not itself a playbook.  The playbook is
    attached only when a verified strict same-slot packet selected this exact
    code and exactly one eligible entry playbook can be identified from the
    frozen library.  Ambiguity remains missing instead of being guessed.
    """
    capture = ((route.get("system_replay_readiness") or {}).get(
        "strict_forward_capture"
    ) or {})
    latest = capture.get("latest") if isinstance(capture, dict) else None
    runtime = capture.get("runtime_admission") if isinstance(capture, dict) else None
    if not (
        isinstance(latest, dict) and latest.get("valid") is True
        and isinstance(runtime, dict) and runtime.get("valid") is True
    ):
        return None
    code = str(candidate.get("code") or "").strip()
    selected_codes = {
        str(row.get("stock_code") or row.get("code") or "").strip()
        for row in (latest.get("selected_candidates") or [])
        if isinstance(row, dict)
    }
    if code not in selected_codes:
        return None
    eligible = {
        str(row.get("id") or "").strip() if isinstance(row, dict)
        else str(row or "").strip()
        for row in (latest.get("eligible_playbooks") or [])
    }
    library = route.get("small_capital_playbook_library") or {}
    entry_playbooks = [
        str(row.get("id") or "").strip()
        for row in (library.get("playbooks") or [])
        if isinstance(row, dict)
        and row.get("kind") == "research_entry_playbook"
        and str(row.get("id") or "").strip() in eligible
    ]
    if len(entry_playbooks) != 1:
        return None
    strategy_ref = candidate.get("strategy_ref")
    strategy_ref = strategy_ref if isinstance(strategy_ref, dict) else {}
    strategy_id = _text(strategy_ref.get("strategy_id"))
    strategy_version = _text(strategy_ref.get("catalog_version"))
    as_of = _text(
        ((candidate.get("assessment") or {}).get("as_of"))
        if isinstance(candidate.get("assessment"), dict) else None
    ) or _text(latest.get("decision_at"))
    packet_id = _text(latest.get("packet_id"))
    slot_id = _text(latest.get("slot_id"))
    decision_day = _text(latest.get("decision_day"))
    if not all((strategy_id, strategy_version, as_of, packet_id, slot_id, decision_day)):
        return None
    return {
        "playbook_id": entry_playbooks[0],
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "strategy_role": str(
            strategy_ref.get("origin") or "manual_execution_policy"
        ),
        "evidence_grade": (
            "strict_same_slot_research_packet_plus_manual_execution_policy"
        ),
        "as_of": as_of,
        "packet_id": packet_id,
        "slot_id": slot_id,
        "decision_day": decision_day,
    }


def _bind_strict_candidate_route(
    candidate: dict[str, Any], route: dict[str, Any],
) -> dict[str, Any]:
    bound = copy.deepcopy(candidate)
    metadata = _strict_candidate_route_metadata(bound, route)
    trade_plan = bound.get("trade_plan")
    trade_plan = trade_plan if isinstance(trade_plan, dict) else {}
    action = trade_plan.get("action")
    if metadata is None or not isinstance(action, dict) or action.get("side") != "BUY":
        return bound
    existing = _text(action.get("playbook_id") or bound.get("playbook_id"))
    if existing is not None and existing != metadata["playbook_id"]:
        action.pop("playbook_id", None)
        action.pop("evidence_grade", None)
        bound.pop("playbook_id", None)
        return bound
    evidence = action.get("evidence_identity")
    evidence = copy.deepcopy(evidence) if isinstance(evidence, dict) else {}
    evidence.update({
        key: metadata[key]
        for key in (
            "packet_id", "slot_id", "decision_day", "strategy_id",
            "strategy_version", "evidence_grade",
        )
    })
    action.update({
        key: metadata[key]
        for key in (
            "playbook_id", "strategy_id", "strategy_version",
            "strategy_role", "evidence_grade", "as_of",
        )
    })
    action["evidence_identity"] = evidence
    trade_plan["playbook_id"] = metadata["playbook_id"]
    bound["playbook_id"] = metadata["playbook_id"]
    bound["trade_plan"] = trade_plan
    return bound


def _manual_plan_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    trade_plan = candidate.get("trade_plan")
    trade_plan = trade_plan if isinstance(trade_plan, dict) else {}
    action = trade_plan.get("action")
    action = action if isinstance(action, dict) else {}
    assessment = candidate.get("assessment")
    assessment = assessment if isinstance(assessment, dict) else {}
    allocation = candidate.get("allocation")
    allocation = allocation if isinstance(allocation, dict) else {}
    alert = candidate.get("alert")
    alert = alert if isinstance(alert, dict) else {}
    side = str(action.get("side") or "").upper()
    shares = _positive_integer(action.get("shares"))
    target_shares = _nonnegative_integer(action.get("target_shares"))
    limit_price = _number(action.get("limit_price"))
    valid_until = _text(action.get("valid_until"))
    invalidation = _text(
        action.get("cancel_condition") or action.get("invalidation_condition")
    )
    estimated_cash = _first_number(
        allocation.get("estimated_buy_cash_yuan"),
        action.get("estimated_cash_required"),
        action.get("cash_required"),
    )
    estimated_cost = _first_number(
        action.get("estimated_cost"), allocation.get("estimated_buy_cost_yuan"),
        action.get("estimated_fee"),
    )
    target_weight = _first_number(
        allocation.get("target_weight_pct"), action.get("target_weight_pct"),
        action.get("target_position_pct"),
    )
    cash_after = _number(allocation.get("cash_after_yuan"))
    liquidity_capacity = _number(allocation.get("liquidity_capacity_yuan"))
    raw_risk_line = allocation.get("risk_line")
    risk_line = raw_risk_line if isinstance(raw_risk_line, dict) else {}
    risk_value = _number(risk_line.get("value"))
    evidence = _plan_evidence_metadata(candidate, trade_plan, action, assessment)
    quantity_valid = bool(shares is not None and shares % 100 == 0)
    account_and_risk_ready = bool(
        estimated_cash is not None and estimated_cash > 0
        and estimated_cost is not None and estimated_cost >= 0
        and target_weight is not None and 0 < target_weight <= 100
        and cash_after is not None and cash_after >= 0
        and liquidity_capacity is not None and liquidity_capacity > 0
        and risk_line.get("kind") == "hard_stop"
        and risk_value is not None and risk_value > 0
    )
    review_ready = bool(
        side == "BUY"
        and trade_plan.get("state") == "ENTRY_READY"
        and quantity_valid
        and limit_price is not None and limit_price > 0
        and target_shares == shares
        and _active_deadline(valid_until)
        and invalidation is not None
        and evidence["ready"]
        and action.get("manual_confirmation") is True
        and alert.get("status") == "triggered"
        and _positive_integer(alert.get("id")) is not None
        and alert.get("valid_until") == valid_until
        and assessment.get("execution_eligible") is True
        and assessment.get("can_alert") is True
        and account_and_risk_ready
    )
    reason = (
        trade_plan.get("reason")
        or assessment.get("reason")
        or (candidate.get("alert") or {}).get("reason")
        or "缺少经过账户、仓位、风险、情绪与时机约束的完整数量计划"
    )
    return {
        "status": (
            "ready_for_manual_review" if review_ready
            else "quantity_or_execution_inputs_incomplete"
        ),
        "side": side if side == "BUY" else None,
        "shares": shares if review_ready else None,
        "target_shares": target_shares if review_ready else None,
        "limit_price": limit_price if limit_price and limit_price > 0 else None,
        "estimated_cash_required": estimated_cash if review_ready else None,
        "estimated_cost": estimated_cost if review_ready else None,
        "target_weight_pct": target_weight if review_ready else None,
        "cash_after_yuan": cash_after if review_ready else None,
        "liquidity_capacity_yuan": (
            liquidity_capacity if review_ready else None
        ),
        "risk_line": risk_value if review_ready else None,
        "risk_line_kind": risk_line.get("kind") if review_ready else None,
        "valid_until": valid_until if review_ready else None,
        "invalidation_condition": invalidation if review_ready else None,
        "alert_id": _positive_integer(alert.get("id")) if review_ready else None,
        "playbook_id": evidence["playbook_id"],
        "strategy_id": evidence["strategy_id"],
        "strategy_version": evidence["strategy_version"],
        "strategy_role": evidence["strategy_role"],
        "evidence_identity": evidence["evidence_identity"],
        "evidence_id": evidence["evidence_id"],
        "evidence_grade": evidence["evidence_grade"],
        "evidence_grades": evidence["evidence_grades"],
        "as_of": evidence["as_of"],
        "reason": str(reason),
        "human_confirmation_required": True,
        "broker_connected": False,
        "automatic_trading": False,
    }


def _position_review_summaries(context: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in context.get("position_alerts") or []:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "").strip()
        if len(code) != 6 or not code.isdigit():
            continue
        alert = raw.get("alert")
        alert = alert if isinstance(alert, dict) else {}
        trade_plan = raw.get("trade_plan")
        trade_plan = trade_plan if isinstance(trade_plan, dict) else {}
        action = trade_plan.get("action")
        action = action if isinstance(action, dict) else {}
        position = raw.get("position")
        position = position if isinstance(position, dict) else {}
        assessment = raw.get("assessment")
        assessment = assessment if isinstance(assessment, dict) else {}
        shares = _positive_integer(action.get("shares"))
        target = _nonnegative_integer(action.get("target_shares"))
        held = _positive_integer(position.get("shares"))
        limit_price = _number(action.get("limit_price"))
        estimated_cost = _number(action.get("estimated_cost"))
        valid_until = _text(action.get("valid_until"))
        invalidation = _text(action.get("cancel_condition"))
        evidence = _plan_evidence_metadata(raw, trade_plan, action, assessment)
        manual_exit = trade_plan.get("manual_exit_intent")
        manual_exit = manual_exit if isinstance(manual_exit, dict) else {}
        sellable_inventory = trade_plan.get("sellable_inventory")
        sellable_inventory = (
            sellable_inventory if isinstance(sellable_inventory, dict) else {}
        )
        ready = bool(
            alert.get("status") == "triggered"
            and _positive_integer(alert.get("id")) is not None
            and action.get("side") == "SELL"
            and trade_plan.get("state") in {"EXIT_REVIEW", "REDUCE_REVIEW"}
            and shares is not None and target is not None and held is not None
            and shares <= held and target == held - shares
            and limit_price is not None and limit_price > 0
            and estimated_cost is not None and estimated_cost >= 0
            and _active_deadline(valid_until) and invalidation is not None
            and alert.get("valid_until") == valid_until
            and action.get("manual_confirmation") is True
            and evidence["ready"]
            and raw.get("data_health") == "ok"
        )
        reason = str(
            trade_plan.get("reason") or alert.get("reason")
            or "持仓继续监控，尚未形成精确卖出复核计划"
        )
        rows.append({
            "status": "ready_for_manual_review" if ready else "monitoring",
            "priority": "urgent_risk" if ready else "normal_observation",
            "position_id": _positive_integer(position.get("id")),
            "code": code,
            "name": str(raw.get("name") or code),
            "stock_research_url": f"/stock?code={code}",
            "side": "SELL" if ready else None,
            "shares": shares if ready else None,
            "held_shares": held,
            "target_shares": target if ready else None,
            "limit_price": limit_price if ready else None,
            "estimated_cost": estimated_cost if ready else None,
            "valid_until": valid_until if ready else None,
            "invalidation_condition": invalidation if ready else None,
            "playbook_id": evidence["playbook_id"],
            "strategy_id": evidence["strategy_id"],
            "strategy_version": evidence["strategy_version"],
            "strategy_role": evidence["strategy_role"],
            "evidence_identity": evidence["evidence_identity"],
            "evidence_id": evidence["evidence_id"],
            "evidence_grade": evidence["evidence_grade"],
            "evidence_grades": evidence["evidence_grades"],
            "as_of": evidence["as_of"],
            "manual_exit_intent": copy.deepcopy(manual_exit),
            "sellable_inventory": copy.deepcopy(sellable_inventory),
            "reason": reason,
            "human_confirmation_required": True,
            "broker_connected": False,
            "automatic_trading": False,
        })
        if len(rows) >= 12:
            break
    return rows


def _candidate_interpretation_summary(
    candidates: list[dict[str, Any]] | None,
    route: dict[str, Any],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        candidate = _bind_strict_candidate_route(candidate, route)
        code = str(candidate.get("code") or "").strip()
        if len(code) != 6 or not code.isdigit() or code in seen:
            continue
        seen.add(code)
        interpretation = interpret_candidate_with_trader_style_lenses(
            candidate, route,
        )
        assessment = candidate.get("assessment")
        assessment = assessment if isinstance(assessment, dict) else {}
        risk_gate = ((assessment.get("gates") or {}).get("risk") or {})
        formal_recommendation_eligible = bool(
            risk_gate.get("recommendation_eligible") is True
        )
        manual_review_state = str(
            candidate.get("manual_review_state")
            or (
                "ready_for_manual_review"
                if assessment.get("manual_actionable") is True
                else "monitoring"
            )
        )
        rows.append({
            "code": code,
            "name": str(candidate.get("name") or code),
            "bucket": str(candidate.get("_workspace_bucket") or "candidate"),
            "stock_research_url": f"/stock?code={code}",
            "price": _number(candidate.get("price")),
            "change_pct": _number(candidate.get("change_pct")),
            "unified_composite_score": _number(
                candidate.get("unified_composite_score")
            ),
            "condition_match_score": _number(
                candidate.get("condition_match_score")
            ),
            "action_state": assessment.get("action_state"),
            "timing_state": candidate.get("timing_state") or assessment.get("action_state"),
            "manual_review_state": manual_review_state,
            "recommended_action": (
                candidate.get("recommended_action")
                if formal_recommendation_eligible else "wait"
            ),
            "recommended_action_role": (
                "formal_recommendation_only_not_manual_timing_state"
            ),
            "recommendation_eligible": formal_recommendation_eligible,
            "manual_plan": _manual_plan_summary(candidate),
            "interpretation": interpretation,
        })
        if len(rows) >= _CANDIDATE_INTERPRETATION_LIMIT:
            break
    return {
        "status": (
            "candidate_evidence_interpretations_ready"
            if rows else "no_unified_candidates_available"
        ),
        "count": len(rows),
        "limit": _CANDIDATE_INTERPRETATION_LIMIT,
        "selection_rule": (
            "preserve_unified_opportunity_order_items_then_watch_deduplicated"
        ),
        "items": rows,
        "authority": {
            "changes_unified_score": False,
            "changes_ranking": False,
            "changes_position_or_quantity": False,
            "changes_trade_count": False,
            "generates_buy_or_sell_alert": False,
            "generates_recommendation": False,
            "production": False,
            "automatic_trading": False,
        },
    }


def _strict_selected_candidate_codes(strict_latest: dict[str, Any]) -> set[str]:
    codes: set[str] = set()
    for row in strict_latest.get("selected_candidates") or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("stock_code") or row.get("code") or "").strip()
        if len(code) == 6 and code.isdigit():
            codes.add(code)
    return codes


def _today_decision_workspace(
    route: dict[str, Any],
    portfolio: dict[str, Any],
    opportunity_context: dict[str, Any] | None,
    interpretations: dict[str, Any],
) -> dict[str, Any]:
    context = opportunity_context if isinstance(opportunity_context, dict) else {}
    account_execution = context.get("account_execution")
    account_execution = (
        account_execution if isinstance(account_execution, dict) else {}
    )
    replay = route.get("system_replay_readiness")
    replay = replay if isinstance(replay, dict) else {}
    strict_capture = replay.get("strict_forward_capture")
    strict_capture = strict_capture if isinstance(strict_capture, dict) else {}
    strict_latest = strict_capture.get("latest")
    strict_latest = strict_latest if isinstance(strict_latest, dict) else {}
    core_review = route.get("current_slot_research_review")
    core_review = core_review if isinstance(core_review, dict) else {}
    core_review_valid = core_review.get("valid") is True
    completed_review = route.get("completed_market_review")
    completed_review = (
        completed_review if isinstance(completed_review, dict) else {}
    )
    context_day = str(context.get("date") or "")[:10]
    strict_alert = strict_route_alert_context(
        strict_capture,
        decision_day=context_day,
        reference_time=(portfolio.get("market_session") or {}).get(
            "server_time"
        ),
    )
    strict_valid = strict_alert.get("route_valid") is True
    execution_route_authorized = strict_alert.get("alert_ready") is True
    market_session = portfolio.get("market_session")
    market_session = market_session if isinstance(market_session, dict) else {}
    session_open = market_session.get("is_open") is True
    session_status = str(
        market_session.get("status") or market_session.get("phase") or "unknown"
    )
    server_time = _text(market_session.get("server_time"))
    try:
        decision_date = (
            datetime.fromisoformat(server_time).date()
            if server_time else _shanghai_now().date()
        )
    except ValueError:
        decision_date = _shanghai_now().date()
    equity = _number(portfolio.get("account_equity"))
    market_value = _number(portfolio.get("market_value"))
    current_exposure = (
        round(market_value / equity * 100, 2)
        if equity is not None and equity > 0 and market_value is not None
        else None
    )
    strict_target = _number(strict_latest.get("target_exposure_pct"))
    strict_trade_count = _positive_integer(
        strict_latest.get("maximum_new_trades")
    )
    strict_market_budget_valid = bool(
        strict_valid and strict_target is not None
        and strict_alert.get("full_a_emotion_scope_valid") is True
    )
    strict_research_target = (
        max(0.0, min(100.0, strict_target))
        if strict_market_budget_valid else None
    )
    market_budget_ceiling = _number(
        account_execution.get("market_target_exposure_pct")
    )
    market_budget_ceiling = (
        max(0.0, min(100.0, market_budget_ceiling))
        if market_budget_ceiling is not None else None
    )
    research_budget_comparison_available = bool(
        strict_market_budget_valid and market_budget_ceiling is not None
    )
    effective_market_budget_valid = bool(
        execution_route_authorized and market_budget_ceiling is not None
    )
    strict_confirmed_sectors = list(
        strict_latest.get("confirmed_sectors") or []
    )
    core_confirmed_sectors = (
        list(core_review.get("confirmed_sectors") or [])
        if core_review_valid else []
    )
    sector_confirmation_ready = bool(strict_valid and strict_confirmed_sectors)
    sector_entry_gate_ready = bool(
        execution_route_authorized and strict_confirmed_sectors
    )
    if effective_market_budget_valid:
        target_exposure = min(strict_research_target, market_budget_ceiling)
        target_basis = (
            "minimum_of_verified_same_slot_research_budget_and_"
            "audited_account_risk_ceiling"
        )
        exposure_capacity_ready = bool(
            current_exposure is not None
            and target_exposure > current_exposure + 1e-9
        )
        maximum_new_trades = (
            (strict_trade_count or 0)
            if sector_entry_gate_ready and exposure_capacity_ready else 0
        )
        decision_source = (
            "strict_forward_v2_entry_route_capped_by_account_risk_budget"
            if sector_entry_gate_ready and exposure_capacity_ready
            else "strict_forward_v2_account_budget_fully_used"
            if sector_entry_gate_ready
            else "strict_forward_v2_market_budget_waiting_sector"
        )
    elif market_budget_ceiling is not None:
        target_exposure = current_exposure
        target_basis = "hold_current_exposure_no_authorized_entry_route"
        maximum_new_trades = 0
        decision_source = "research_route_not_authorized_for_position_or_alerts"
    else:
        target_exposure = current_exposure
        target_basis = (
            "hold_current_exposure_until_same_slot_emotion_sector_and_account_inputs_complete"
        )
        maximum_new_trades = 0
        decision_source = "fail_closed_current_account_hold"
    candidate_rows = interpretations.get("items") or []
    raw_actionable = [
        row for row in candidate_rows
        if isinstance(row, dict)
        and (row.get("manual_plan") or {}).get("status")
        == "ready_for_manual_review"
        and _active_deadline((row.get("manual_plan") or {}).get("valid_until"))
    ]
    strict_candidate_codes = _strict_selected_candidate_codes(strict_latest)
    actionable = [
        row for row in raw_actionable
        if str(row.get("code") or "") in strict_candidate_codes
    ]
    confirmed = route.get("sector_confirmation")
    confirmed = confirmed if isinstance(confirmed, dict) else {}
    research_budget = route.get("research_budget_hypothesis")
    research_budget = research_budget if isinstance(research_budget, dict) else {}
    strict_playbooks = strict_latest.get("eligible_playbooks") or []
    core_playbooks = (
        list(core_review.get("eligible_playbooks") or [])
        if core_review_valid else []
    )
    research_playbooks = [
        row for row in (research_budget.get("allowed_playbooks") or [])
        if isinstance(row, dict)
    ]
    manual_review_ready = bool(
        session_open
        and sector_entry_gate_ready
        and maximum_new_trades > 0
        and actionable
    )
    manual_review_items = (
        actionable[:min(2, maximum_new_trades)] if manual_review_ready else []
    )
    authorized_buy_codes = {
        str(row.get("code") or "") for row in manual_review_items
    }
    for row in candidate_rows:
        plan = row.get("manual_plan") if isinstance(row, dict) else None
        if not isinstance(plan, dict) or plan.get("status") != "ready_for_manual_review":
            continue
        if str(row.get("code") or "") in authorized_buy_codes:
            plan["strict_forward_packet_id"] = strict_latest.get("packet_id")
            plan["strict_forward_slot_id"] = strict_latest.get("slot_id")
            continue
        plan.update({
            "status": "withheld_by_today_decision",
            "shares": None, "target_shares": None, "limit_price": None,
            "estimated_cash_required": None, "estimated_cost": None,
            "target_weight_pct": None, "cash_after_yuan": None,
            "liquidity_capacity_yuan": None, "risk_line": None,
            "risk_line_kind": None, "valid_until": None,
            "invalidation_condition": None, "alert_id": None,
            "reason": (
                "该股票未同时进入今日同一观察时点的严格候选与最多两项人工复核清单；"
                "不展示旧数量或买入按钮"
            ),
        })
    position_reviews = _position_review_summaries(context)
    # The opportunity snapshot can legitimately predate a later, audited
    # account-inventory update.  Keep the risk observation tied to its original
    # trigger, but show the newest account fact so the decision workspace does
    # not claim that today's sellable quantity is missing when the account
    # ledger has already recorded it.
    portfolio_positions = {
        str(row.get("code") or ""): row
        for row in (portfolio.get("positions") or [])
        if isinstance(row, dict) and row.get("code")
    }
    for review in position_reviews:
        live_position = portfolio_positions.get(str(review.get("code") or ""))
        if not isinstance(live_position, dict):
            continue
        held_shares = _positive_integer(live_position.get("shares"))
        sellable_shares = _nonnegative_integer(
            live_position.get("sellable_shares_snapshot")
        )
        sellable_as_of = _text(live_position.get("sellable_as_of"))
        if (
            held_shares is None
            or sellable_shares is None
            or sellable_shares > held_shares
            or sellable_as_of != decision_date.isoformat()
        ):
            continue
        review["held_shares"] = held_shares
        review["sellable_inventory"] = {
            "status": "current",
            "total_shares": held_shares,
            "sellable_shares": sellable_shares,
            "as_of": sellable_as_of,
        }
    sell_review_items = [
        row for row in position_reviews
        if row.get("status") == "ready_for_manual_review"
        and session_open
        and _active_deadline(row.get("valid_until"))
    ][:2]
    inventory_confirmations = []
    for row in portfolio_positions.values():
        held_shares = _positive_integer(row.get("shares"))
        sellable_shares = _nonnegative_integer(
            row.get("sellable_shares_snapshot")
        )
        sellable_as_of = _text(row.get("sellable_as_of"))
        if held_shares is None or (
            sellable_shares is not None
            and sellable_shares <= held_shares
            and sellable_as_of == decision_date.isoformat()
        ):
            continue
        previous_sellable_shares = (
            sellable_shares
            if sellable_shares is not None
            and sellable_shares <= held_shares
            and sellable_as_of
            else None
        )
        inventory_confirmations.append({
            "position_id": row.get("id"),
            "code": str(row.get("code") or ""),
            "name": str(row.get("name") or row.get("code") or "持仓"),
            "held_shares": held_shares,
            # A prior-day value is useful for comparison but must never be
            # prefilled as today's T+1 inventory confirmation.
            "sellable_shares": None,
            "sellable_as_of": None,
            "previous_sellable_shares": previous_sellable_shares,
            "previous_sellable_as_of": (
                sellable_as_of if previous_sellable_shares is not None else None
            ),
            "stock_research_url": f"/stock?code={row.get('code')}",
        })
    buy_count = len(manual_review_items)
    sell_count = len(sell_review_items)
    pending_exit_confirmations = []
    for row in position_reviews:
        manual = row.get("manual_exit_intent")
        manual = manual if isinstance(manual, dict) else {}
        effective_date = _text(manual.get("effective_date"))
        try:
            due = bool(
                manual.get("configured") is True
                and effective_date
                and datetime.strptime(effective_date, "%Y-%m-%d").date()
                <= decision_date
            )
        except ValueError:
            due = False
        if due and row.get("status") != "ready_for_manual_review":
            pending_exit_confirmations.append(row)
    if sell_count and buy_count:
        workspace_status = "buy_and_position_risk_review_ready"
        headline = f"{sell_count} 项持仓风险 + {buy_count} 项买入计划待人工复核"
    elif sell_count:
        workspace_status = "position_risk_review_ready"
        headline = f"{sell_count} 项持仓风险待人工复核 · 今日不新开仓"
    elif buy_count:
        workspace_status = "manual_review_candidates_ready"
        headline = f"{buy_count} 个买入计划待人工复核"
    elif pending_exit_confirmations or inventory_confirmations:
        workspace_status = "preopen_account_confirmation_required"
        headline = (
            f"{len(inventory_confirmations)} 项当日可卖数量待确认 · 今日不新开仓"
        )
    else:
        workspace_status = "today_no_new_trade"
        headline = "今日不新开仓"
    due_exit_codes = {str(row.get("code") or "") for row in pending_exit_confirmations}
    must_handle_items = [
        {
            "kind": "position_inventory_confirmation",
            "priority": "urgent_risk",
            "position_id": row.get("position_id"),
            "code": row["code"],
            "label": f"确认今日可卖股数 {row['name']}",
            "detail": (
                f"已记录将持仓降至{(row.get('manual_exit_intent') or {}).get('target_shares')}股；"
                f"当前持仓{row.get('held_shares')}股。确认当日可卖库存后，"
                "盘中实时价才能生成精确卖出复核计划"
            ),
            "held_shares": row.get("held_shares"),
            "sellable_shares": (
                (row.get("sellable_inventory") or {}).get("sellable_shares")
            ),
            "sellable_as_of": (
                (row.get("sellable_inventory") or {}).get("as_of")
            ),
            "stock_research_url": row["stock_research_url"],
        }
        for row in pending_exit_confirmations
    ] + [
        {
            "kind": "position_inventory_confirmation",
            "priority": "account_input",
            "position_id": row.get("position_id"),
            "code": row["code"],
            "label": f"确认今日可卖股数 {row['name']}",
            "detail": (
                f"当前持仓{row['held_shares']}股；当日可卖数量尚未核验。"
                + (
                    f"上次记录 {row['previous_sellable_as_of']} 为"
                    f"{row['previous_sellable_shares']}股，仅供核对、不自动沿用。"
                    if row.get("previous_sellable_shares") is not None
                    else ""
                )
                +
                "完成核验后，盘中风险触发才可生成精确卖出复核计划"
            ),
            "held_shares": row["held_shares"],
            "sellable_shares": row["sellable_shares"],
            "sellable_as_of": row["sellable_as_of"],
            "stock_research_url": row["stock_research_url"],
        }
        for row in inventory_confirmations
        if row["code"] not in due_exit_codes
    ] + [
        {
            "kind": "position_risk", "priority": "urgent_risk",
            "code": row["code"], "label": f"复核卖出 {row['name']}",
            "detail": f"{row['shares']} 股 · {row['reason']}",
            "stock_research_url": row["stock_research_url"],
        }
        for row in sell_review_items
    ] + [
        {
            "kind": "buy_review", "priority": "actionable_opportunity",
            "code": str(row.get("code") or ""),
            "label": f"复核买入 {row.get('name') or row.get('code')}",
            "detail": (
                f"{(row.get('manual_plan') or {}).get('shares')} 股 · "
                f"{(row.get('manual_plan') or {}).get('reason')}"
            ),
            "stock_research_url": row.get("stock_research_url"),
        }
        for row in manual_review_items
    ]
    return {
        "status": workspace_status,
        "headline": headline,
        "decision_source": decision_source,
        "market_state": (
            strict_latest.get("market_state")
            if strict_market_budget_valid else
            core_review.get("market_state")
            if core_review_valid else route.get("market_state")
        ),
        "research_hypothesis_state": route.get("requested_market_state"),
        "market_confidence": (
            strict_latest.get("market_confidence")
            if strict_market_budget_valid else
            core_review.get("market_confidence")
            if core_review_valid else route.get("market_confidence")
        ),
        "target_exposure_pct": target_exposure,
        "target_exposure_basis": target_basis,
        "target_exposure_semantics": (
            "人工复核规划上限；不是自动指令、收益预测或已验证盈利概率"
        ),
        "strict_research_target_exposure_pct": strict_research_target,
        "current_exposure_pct": current_exposure,
        "existing_market_budget_ceiling_pct": market_budget_ceiling,
        "market_budget_cap_applied": bool(
            effective_market_budget_valid
            and strict_research_target > market_budget_ceiling
        ),
        "research_budget_comparison_available": (
            research_budget_comparison_available
        ),
        "maximum_new_trades": maximum_new_trades,
        "intraday_confirmation": (
            strict_latest.get("intraday_confirmation")
            if strict_market_budget_valid else None
        ),
        "confirmed_sectors": (
            strict_confirmed_sectors
            if strict_valid else core_confirmed_sectors
            if core_review_valid else confirmed.get("confirmed_sectors") or []
        ),
        "eligible_playbooks": (
            strict_playbooks if strict_valid else core_playbooks
            if core_review_valid else research_playbooks
        ),
        "evidence": (
            strict_latest.get("evidence")
            if strict_market_budget_valid else core_review.get("evidence")
            if core_review_valid else route.get("evidence") or []
        ),
        "contradictions": (
            strict_latest.get("contradictions")
            if strict_market_budget_valid else core_review.get("contradictions")
            if core_review_valid else route.get("contradictions") or []
        ),
        "account": {
            "cash_user_recorded": portfolio.get("cash_user_recorded") is True,
            "available_cash": _number(portfolio.get("available_cash")),
            "account_equity": equity,
            "market_value": market_value,
            "valuation_status": portfolio.get("valuation_status"),
            "position_count": portfolio.get("position_count"),
        },
        "candidate_count": len(candidate_rows),
        "candidate_plan_ready_before_route_count": len(raw_actionable),
        "manual_review_ready_count": buy_count,
        "manual_review_items": manual_review_items,
        "position_risk_count": len(position_reviews),
        "sell_review_ready_count": sell_count,
        "sell_review_items": sell_review_items,
        "pending_exit_confirmation_count": len(pending_exit_confirmations),
        "inventory_confirmation_count": len(inventory_confirmations),
        "position_risk_observations": position_reviews,
        "must_handle_items": must_handle_items,
        "strict_forward_packet_valid": strict_valid,
        "strict_market_budget_valid": strict_market_budget_valid,
        "effective_market_budget_valid": effective_market_budget_valid,
        "strict_packet_applied_to_execution": False,
        "execution_route_authorized": execution_route_authorized,
        "execution_route_reason": strict_alert.get("reason"),
        "sector_confirmation_ready": sector_confirmation_ready,
        "sector_entry_gate_ready": sector_entry_gate_ready,
        "research_sector_confirmation_ready": bool(
            core_review_valid and core_confirmed_sectors
        ),
        "research_candidate_input_ready": bool(
            core_review_valid
            and core_review.get("candidate_constituent_input_ready") is True
        ),
        "research_candidate_input_errors": list(
            core_review.get("candidate_constituent_input_errors") or []
        ) if core_review_valid else [],
        "current_slot_research_review": copy.deepcopy(core_review),
        "session_status": session_status,
        "actionable_now": bool(session_open and (buy_count or sell_count)),
        "strict_forward_packet_id": (
            strict_latest.get("packet_id") if strict_valid else None
        ),
        "strict_forward_slot_id": (
            strict_latest.get("slot_id") if strict_valid else None
        ),
        "strict_forward_decision_at": (
            strict_latest.get("decision_at") if strict_valid else None
        ),
        "completed_market_review": {
            key: copy.deepcopy(completed_review.get(key))
            for key in (
                "valid", "review_source", "current_reproduction_valid",
                "display_only", "grants_live_authority", "packet_id",
                "decision_day", "slot_id", "decision_at", "market_state",
                "market_state_as_of", "market_confidence",
                "market_confidence_semantics", "evidence", "contradictions",
                "target_exposure_pct", "maximum_new_trades",
                "intraday_confirmation", "decision_status",
                "account_plan_status", "candidate_count",
                "eligible_playbooks", "confirmed_sectors",
                "next_session_preparation", "terminal_playbook_retirement",
                "current_verifier_status", "current_verifier_errors",
            )
            if completed_review.get(key) is not None
        },
        "phase_clock": {
            "timezone": "Asia/Shanghai",
            "source": "server_audited_market_session_plus_beijing_clock",
            "snapshot_server_time": server_time or context.get("date"),
            "buy_window": (
                portfolio.get("buy_window")
                if isinstance(portfolio.get("buy_window"), dict)
                else context.get("buy_window") or {}
            ),
            "auto_focus": True,
        },
        "human_confirmation_required": True,
        "broker_connected": False,
        "automatic_trading": False,
        "authority": {
            "research_only": True,
            "changes_unified_score": False,
            "changes_position_or_quantity": False,
            "generates_buy_or_sell_alert": False,
            "executes_orders": False,
        },
        "reason": (
            "持仓硬风险计划优先显示；卖出仍需人工核验当日可卖数量与成交。"
            if sell_count else
            "昨日偏强基线被今天盘中上涨覆盖率反证；即使研究主线已经确认，"
            "今天新交易仍为0，保持当前持仓并继续风险复核。"
            if strict_market_budget_valid
            and strict_latest.get("intraday_confirmation") == "contradicted" else
            "当日情绪、板块和候选研究包可读，但未绑定成本后与样本外验证的生产策略；"
            "不改变账户仓位、不生成买入数量或买入提醒。"
            if strict_valid and not execution_route_authorized else
            "同一观察时点的市场情绪、板块、账户和候选包已验证；只开放人工买入复核。"
            if buy_count else
            "同一观察时点的市场仓位预算已形成，但板块尚未确认；保留仓位预算展示，新交易为0。"
            if effective_market_budget_valid and not sector_entry_gate_ready else
            "同一观察时点的严格研究包已形成，但现行账户风险预算不可用；保持当前仓位且不新增交易。"
            if strict_market_budget_valid and not effective_market_budget_valid else
            "同一观察时点的市场情绪、板块与账户证据已验证，但当前没有同一候选的有效完整买入计划；今日不新开仓。"
            if effective_market_budget_valid and sector_entry_gate_ready
            and not raw_actionable else
            "同一观察时点的市场情绪、板块与账户证据已验证，但现有完整计划未进入严格候选；今日不新开仓。"
            if effective_market_budget_valid and sector_entry_gate_ready else
            "当前同槽市场情绪与板块核心证据已核验；严格账户、候选和策略包尚未完成，"
            "因此只展示研究状态，保持当前仓位且不生成股数或买卖提醒。"
            if core_review_valid else
            "同一观察时点的严格市场情绪、板块与账户决策包尚未形成；保持当前仓位且不新增交易，避免用盘后或过期数据补出建议。"
        ),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path, expected_sha256: str) -> dict[str, Any]:
    if _sha256(path) != expected_sha256:
        raise ValueError(f"identity_invalid:{path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"document_invalid:{path.name}")
    return value


def _completed_market_review_snapshot(
    strict_capture: dict[str, Any],
) -> dict[str, Any]:
    """Return a stable completed-session observation without granting authority.

    The live verifier intentionally follows every upstream identity.  A mutable
    full-market candidate snapshot can therefore make an otherwise immutable
    intraday packet fail later in the day.  That must close live authority, but
    it must not erase a packet that an exact release already bound for review.
    """
    live = strict_capture.get("latest") if isinstance(strict_capture, dict) else None
    runtime = (
        strict_capture.get("runtime_admission")
        if isinstance(strict_capture, dict) else None
    )
    if (
        isinstance(live, dict) and live.get("valid") is True
        and isinstance(runtime, dict) and runtime.get("valid") is True
    ):
        return {
            **copy.deepcopy(live),
            "review_source": "current_verified_strict_forward_packet",
            "current_reproduction_valid": True,
            "display_only": True,
            "grants_live_authority": False,
        }
    try:
        release = _load_json(
            _COMPLETED_REVIEW_RELEASE_PATH,
            _COMPLETED_REVIEW_RELEASE_SHA256,
        )
        binding = (release.get("current_bindings") or {}).get(
            "latest_completed_strict_packet"
        ) or {}
        raw_path = Path(str(binding.get("path") or ""))
        packet_path = (PROJECT_ROOT / raw_path).resolve()
        packet_path.relative_to(PROJECT_ROOT.resolve())
        if (
            not packet_path.is_file()
            or _sha256(packet_path) != binding.get("sha256")
        ):
            raise ValueError("release_bound_packet_identity_invalid")
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        adapter = packet.get("adapter_result")
        adapter = adapter if isinstance(adapter, dict) else {}
        decision = adapter.get("decision")
        decision = decision if isinstance(decision, dict) else {}
        route = decision.get("route")
        route = route if isinstance(route, dict) else {}
        market = decision.get("market")
        market = market if isinstance(market, dict) else {}
        sector = decision.get("sector")
        sector = sector if isinstance(sector, dict) else {}
        budget = decision.get("market_budget")
        budget = budget if isinstance(budget, dict) else {}
        candidates = decision.get("candidates")
        candidates = candidates if isinstance(candidates, dict) else {}
        current_adapter_protocol = _load_json(
            _SYSTEM_REPLAY_ADAPTER_PROTOCOL_V6_PATH,
            _SYSTEM_REPLAY_ADAPTER_PROTOCOL_V6_SHA256,
        )
        retirement = current_adapter_protocol.get(
            "terminal_playbook_retirement"
        ) or {}
        retired_entry_playbooks = {
            str(value)
            for value in retirement.get("retired_entry_playbooks") or []
        }
        candidate_playbook_id = str(candidates.get("playbook_id") or "")
        candidate_playbook_retired = (
            candidate_playbook_id in retired_entry_playbooks
        )
        account_plan = decision.get("account_plan")
        account_plan = account_plan if isinstance(account_plan, dict) else {}
        refs = adapter.get("input_refs")
        refs = refs if isinstance(refs, dict) else {}
        if not (
            packet.get("schema") == "system_route_joint_v2_forward_packet.v2"
            and packet.get("no_backfill") is True
            and packet.get("returns_or_labels_read") is False
            and packet.get("execution_performed") is False
            and packet.get("performance_computed") is False
            and adapter.get("valid") is True
            and decision.get("status") in {
                "research_decision_ready", "research_no_trade",
            }
            and refs
        ):
            raise ValueError("release_bound_packet_boundary_invalid")
        for ref in refs.values():
            if not isinstance(ref, dict):
                raise ValueError("release_bound_input_ref_invalid")
            source = (PROJECT_ROOT / str(ref.get("path") or "")).resolve()
            source.relative_to(PROJECT_ROOT.resolve())
            if not source.is_file() or _sha256(source) != ref.get("sha256"):
                raise ValueError("release_bound_direct_input_identity_invalid")
        raw_plans = account_plan.get("pre_execution_plans")
        raw_plans = raw_plans if isinstance(raw_plans, list) else []
        preparation_plans: list[dict[str, Any]] = []
        for raw_plan in raw_plans:
            if not isinstance(raw_plan, dict):
                raise ValueError("release_bound_preparation_plan_invalid")
            shares = _positive_integer(raw_plan.get("shares"))
            reference_price = _number(raw_plan.get("reference_price"))
            notional = _number(raw_plan.get("notional_yuan"))
            estimated_cost = _number(raw_plan.get("estimated_buy_cost_yuan"))
            if not (
                shares is not None and shares % 100 == 0
                and reference_price is not None and reference_price > 0
                and notional is not None and notional > 0
                and estimated_cost is not None and estimated_cost >= 0
                and raw_plan.get("side") == "buy"
                and raw_plan.get("status") == "planned_not_executed"
                and raw_plan.get("live_instruction") is False
                and raw_plan.get("order_session")
                == "next_trading_day_opening_auction"
            ):
                raise ValueError("release_bound_preparation_plan_boundary_invalid")
            preparation_plans.append(copy.deepcopy(raw_plan))
        account_authority = account_plan.get("authority")
        account_authority = (
            account_authority if isinstance(account_authority, dict) else {}
        )
        preparation_ready = bool(
            account_plan.get("status") == "research_replay_decision_ready"
            and account_plan.get("execution_replay_complete") is False
            and account_plan.get("executed_orders") == []
            and account_plan.get("returns_or_labels_read") is False
            and account_authority.get("research_only") is True
            and all(
                value is False for key, value in account_authority.items()
                if key != "research_only"
            )
            and preparation_plans
            and not candidate_playbook_retired
        )
        return {
            "valid": True,
            "errors": [],
            "path": str(packet_path),
            "packet_id": packet.get("packet_id"),
            "decision_day": packet.get("decision_day"),
            "slot_id": packet.get("slot_id"),
            "decision_at": packet.get("decision_at"),
            "decision_status": decision.get("status"),
            "market_state": route.get("market_state") or market.get("state"),
            "market_state_as_of": (
                route.get("market_state_as_of") or market.get("as_of")
            ),
            "market_confidence": (
                route.get("market_confidence") or market.get("confidence")
            ),
            "market_confidence_semantics": (
                route.get("market_confidence_semantics")
                or market.get("confidence_semantics")
            ),
            "evidence": list(route.get("evidence") or market.get("evidence") or []),
            "contradictions": list(
                route.get("contradictions") or market.get("contradictions") or []
            ),
            "target_exposure_pct": budget.get("target_exposure_pct"),
            "maximum_new_trades": budget.get("maximum_new_trades"),
            "eligible_playbooks": [
                playbook_id
                for playbook_id in budget.get("eligible_playbooks") or []
                if str(playbook_id) not in retired_entry_playbooks
            ],
            "intraday_confirmation": budget.get("intraday_confirmation"),
            "confirmed_sectors": list(sector.get("confirmed_sectors") or []),
            "candidate_count": candidates.get("selected_count"),
            "selected_candidates": list(candidates.get("selected") or []),
            "next_session_preparation": {
                "status": (
                    "review_ready_requires_next_session_revalidation"
                    if preparation_ready else "unavailable"
                ),
                "playbook_id": (
                    None if candidate_playbook_retired
                    else candidates.get("playbook_id")
                ),
                "stock_formula_id": (
                    None if candidate_playbook_retired
                    else candidates.get("stock_formula_id")
                ),
                "captured_target_exposure_pct": (
                    None if candidate_playbook_retired
                    else account_plan.get("target_exposure_pct")
                ),
                "captured_existing_exposure_pct": (
                    None if candidate_playbook_retired
                    else account_plan.get("existing_exposure_pct")
                ),
                "captured_new_buy_capacity_yuan": (
                    None if candidate_playbook_retired
                    else account_plan.get("new_buy_capacity_yuan")
                ),
                "maximum_new_trades": (
                    None if candidate_playbook_retired
                    else account_plan.get("maximum_new_trades")
                ),
                "plans": preparation_plans if preparation_ready else [],
                "historical_reference": ({
                    "playbook_id": candidates.get("playbook_id"),
                    "stock_formula_id": candidates.get("stock_formula_id"),
                    "captured_target_exposure_pct": account_plan.get(
                        "target_exposure_pct"
                    ),
                    "captured_existing_exposure_pct": account_plan.get(
                        "existing_exposure_pct"
                    ),
                    "captured_new_buy_capacity_yuan": account_plan.get(
                        "new_buy_capacity_yuan"
                    ),
                    "maximum_new_trades": account_plan.get(
                        "maximum_new_trades"
                    ),
                    "semantics": "retired_playbook_historical_review_only",
                    "actionable_now": False,
                } if candidate_playbook_retired else None),
                "reference_only": True,
                "actionable_now": False,
                "next_session_revalidation_required": True,
                "revalidation_fields": [
                    "market_emotion", "same_slot_sector_confirmation",
                    "opening_price_and_limit_state", "tradability",
                    "account_cash_and_positions", "risk_line", "liquidity",
                ],
                "human_confirmation_required": True,
                "broker_connected": False,
                "automatic_trading": False,
            },
            "terminal_playbook_retirement": {
                "applied": candidate_playbook_retired,
                "retired_entry_playbooks": sorted(retired_entry_playbooks),
                "historical_review_preserved": True,
                "future_candidate_or_plan_authority": False,
                "reason": (
                    "该历史复盘所用入场战法已在扣成本历史验证中终止；"
                    "保留市场与账户复盘，但不再形成明日候选或买入计划"
                    if candidate_playbook_retired else None
                ),
            },
            "review_source": "release_bound_completed_intraday_packet",
            "current_reproduction_valid": False,
            "display_only": True,
            "grants_live_authority": False,
            "current_verifier_status": strict_capture.get("status"),
            "current_verifier_errors": [
                "current_route_version_superseded_release_bound_review_only"
            ],
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {
            "valid": False,
            "review_source": "unavailable",
            "display_only": True,
            "grants_live_authority": False,
        }


def _operating_system_summary() -> dict[str, Any]:
    try:
        document = _load_json(_OPERATING_SYSTEM_PATH, _OPERATING_SYSTEM_SHA256)
        bindings = document["bindings"]
        if (
            document.get("system_id")
            != "regis_market_cognition_operating_system_v1"
            or document.get("not_a_conventional_factor_first_system") is not True
            or len(document.get("single_causal_chain") or []) != 10
            or not isinstance(bindings, dict) or not bindings
            or any(
                _sha256(PROJECT_ROOT / binding["path"])
                != binding["sha256"]
                for binding in bindings.values()
            )
            or document["current_truth_at_freeze"].get(
                "stable_profit_proven"
            ) is not False
            or document["authority"].get("research_only") is not True
            or any(
                value is not False
                for key, value in document["authority"].items()
                if key != "research_only"
            )
        ):
            raise ValueError("operating_system_boundary_invalid")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "operating_system_identity_unavailable_or_invalid",
            "system_id": None,
            "display_name": "Regis 市场认知交易体系不可用",
            "chain": [],
            "manual_confirmation_required": True,
            "broker_connected": False,
            "automatic_trading": False,
            "stable_profit_proven": False,
            "reason": str(exc),
        }
    truth = document["current_truth_at_freeze"]
    capital_objective = document["long_term_capital_objective"]
    return {
        "status": document["status"],
        "system_id": document["system_id"],
        "display_name": document["display_name"],
        "document_sha256": _OPERATING_SYSTEM_SHA256,
        "chain": document["single_causal_chain"],
        "current_runtime_states": document["market_state_model"][
            "current_runtime_taxonomy"
        ],
        "active_public_method_lens_count": len(
            document["public_method_research"]["active_evidence_bound_lenses"]
        ),
        "first_playbook_cohort": [
            {
                "id": row["id"],
                "role": row["role"],
                "quantitatively_admitted": row["quantitatively_admitted"],
            }
            for row in document["first_playbook_cohort"]
        ],
        "factor_service_model": document["factor_research_role"][
            "service_model"
        ],
        "factor_candidate_drafts": truth["factor_candidate_drafts"],
        "factor_candidate_drafts_catalog_review_ready": truth[
            "factor_candidate_drafts_catalog_review_ready"
        ],
        "manual_confirmation_required": True,
        "broker_connected": truth["broker_connected"],
        "automatic_trading": truth["automatic_trading"],
        "production_factor_count": truth["production_factors"],
        "production_strategy_count": truth["production_strategies"],
        "stable_profit_proven": truth["stable_profit_proven"],
        "long_term_capital_objective": {
            "initial_capital_cny": capital_objective["initial_capital_cny"],
            "year_1_target_capital_cny": capital_objective[
                "year_1_target_capital_cny"
            ],
            "year_2_target_capital_cny": capital_objective[
                "year_2_target_capital_cny"
            ],
            "classification": capital_objective["classification"],
            "affects_live_position_or_trade_count_now": capital_objective[
                "affects_live_position_or_trade_count_now"
            ],
            "stable_profit_or_target_attainment_proven": capital_objective[
                "stable_profit_or_target_attainment_proven"
            ],
        },
        "reason": "唯一主链已冻结；真实同一观察时点的数据包持续不可覆盖积累，前向绩效与生产准入仍需市场时间形成。",
    }


def _state_routed_strategy_summary() -> dict[str, Any]:
    try:
        document = _load_json(_STRATEGY_COHORT_PATH, _STRATEGY_COHORT_SHA256)
        mainline_failure = _load_json(
            _MAINLINE_TERMINAL_PATH, _MAINLINE_TERMINAL_SHA256,
        )
        reversal_failure = _load_json(
            _T1_REVERSAL_TERMINAL_PATH, _T1_REVERSAL_TERMINAL_SHA256,
        )
        low_volatility_failure = _load_json(
            _LOW_VOLATILITY_TERMINAL_PATH,
            _LOW_VOLATILITY_TERMINAL_SHA256,
        )
        cohort = document["cohort"]
        if (
            document.get("status")
            != "selected_before_joint_return_evaluation_research_only"
            or len(cohort) != 3
            or any(row.get("quantitative_admission") is not False for row in cohort)
            or any(row.get("production") is not False for row in cohort)
            or mainline_failure.get("status")
            != "terminal_historical_failure_archived_no_retune_no_production"
            or reversal_failure.get("status")
            != (
                "terminal_candidate_specific_historical_failure_archived_"
                "no_retune_no_production"
            )
            or low_volatility_failure.get("status")
            != "terminal_historical_failure_archived_no_retune_no_production"
        ):
            raise ValueError("strategy_cohort_boundary_invalid")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "strategy_cohort_unavailable_or_invalid",
            "selected_count": 0,
            "quantitatively_admitted": 0,
            "production_count": 0,
            "strategies": [],
            "reason": str(exc),
        }
    strategies = []
    for row in cohort:
        status = row["status"]
        if row["id"] == "market_state_exposure_with_retreat_defense":
            status = (
                "forward_risk_control_observation_only_"
                "loss_reduction_diagnostic_not_positive_expectancy"
            )
        elif row["id"] == "mainline_start_confirmation_v3":
            status = "terminal_historical_failure_no_retune_no_production"
        elif row["id"] == "state_conditioned_liquid_low_volatility_252d":
            status = "terminal_historical_failure_no_retune_no_production"
        strategies.append({
            "rank": row["rank"],
            "id": row["id"],
            "kind": row["kind"],
            "status": status,
            "quantitative_admission": False,
            "production": False,
        })
    return {
        "status": "historical_outcomes_integrated_zero_quantitative_admission",
        "cohort_id": document["cohort_id"],
        "frozen_at": document["frozen_at"],
        "selected_count": len(cohort),
        "active_research_count": 1,
        "terminally_rejected_count": 3,
        "quantitatively_admitted": 0,
        "production_count": 0,
        "strategies": strategies,
        "terminally_rejected": [
            {
                "id": mainline_failure["candidate_id"],
                "status": mainline_failure["status"],
                "failed_primary_gates": mainline_failure[
                    "failed_primary_gates"
                ],
                "sealed_net_total_return": mainline_failure[
                    "sealed_2025_result"
                ]["net_total_return"],
                "archive_ref": {
                    "path": str(_MAINLINE_TERMINAL_PATH.relative_to(PROJECT_ROOT)),
                    "sha256": _MAINLINE_TERMINAL_SHA256,
                },
            },
            {
                "id": reversal_failure["candidate_id"],
                "status": reversal_failure["status"],
                "failed_primary_gates": reversal_failure[
                    "failed_primary_gates"
                ],
                "sealed_net_total_return": reversal_failure[
                    "candidate_specific_sealed_2025"
                ]["net_total_return"],
                "archive_ref": {
                    "path": str(_T1_REVERSAL_TERMINAL_PATH.relative_to(PROJECT_ROOT)),
                    "sha256": _T1_REVERSAL_TERMINAL_SHA256,
                },
            },
            {
                "id": low_volatility_failure["candidate_id"],
                "status": low_volatility_failure["status"],
                "failed_primary_gates": low_volatility_failure[
                    "failed_primary_gates"
                ],
                "sealed_net_total_return": low_volatility_failure[
                    "sealed_out_of_sample_2024_2025"
                ]["net_total_return"],
                "archive_ref": {
                    "path": str(
                        _LOW_VOLATILITY_TERMINAL_PATH.relative_to(PROJECT_ROOT)
                    ),
                    "sha256": _LOW_VOLATILITY_TERMINAL_SHA256,
                },
            },
        ],
        "deadline_boundary": document["deadline_acceptance_2026_09_01_00_00"],
        "reason": (
            "主线波段、T+1反转与状态板块低波均已在扣成本盲测中失败并归档，"
            "不得调参换名后重新上线；市场状态缩放仅显示过降低损失的诊断作用，"
            "尚未创造正期望。当前生产策略仍为0。"
        ),
    }


def _system_replay_readiness_summary() -> dict[str, Any]:
    forward_status = system_route_joint_v2_forward_status()
    try:
        audit = _load_json(
            _SYSTEM_REPLAY_COVERAGE_PATH, _SYSTEM_REPLAY_COVERAGE_SHA256,
        )
        replay_protocol = _load_json(
            _SYSTEM_REPLAY_PROTOCOL_V2_PATH,
            _SYSTEM_REPLAY_PROTOCOL_V2_SHA256,
        )
        adapter_protocol = _load_json(
            _SYSTEM_REPLAY_ADAPTER_PROTOCOL_V6_PATH,
            _SYSTEM_REPLAY_ADAPTER_PROTOCOL_V6_SHA256,
        )
        persistence = _load_json(
            _SYSTEM_REPLAY_PERSISTENCE_V2_AMENDMENT_5_PATH,
            _SYSTEM_REPLAY_PERSISTENCE_V2_AMENDMENT_5_SHA256,
        )
        admission = _load_json(
            _SYSTEM_REPLAY_FORWARD_ADMISSION_V29_PATH,
            _SYSTEM_REPLAY_FORWARD_ADMISSION_V29_SHA256,
        )
        envelope = _load_json(
            _SYSTEM_REPLAY_ENVELOPE_AMENDMENT_31_PATH,
            _SYSTEM_REPLAY_ENVELOPE_AMENDMENT_31_SHA256,
        )
        account_protocol_binding = envelope["bindings"]["account_packet_protocol"]
        account_protocol = _load_json(
            PROJECT_ROOT / account_protocol_binding["path"],
            account_protocol_binding["sha256"],
        )

        def bindings_current(document: dict[str, Any]) -> bool:
            bindings = document.get("bindings")
            return bool(
                isinstance(bindings, dict)
                and bindings
                and all(
                    isinstance(binding, dict)
                    and isinstance(binding.get("path"), str)
                    and _sha256(PROJECT_ROOT / binding["path"])
                    == binding.get("sha256")
                    for binding in bindings.values()
                )
            )

        implementation = {
            "decision_driver": _sha256(_SYSTEM_REPLAY_DRIVER_PATH)
            == envelope["bindings"]["decision_driver"]["sha256"],
            "multi_session_engine": (
                _sha256(_SYSTEM_REPLAY_IMPLEMENTATIONS["multi_session_engine"][0])
                == envelope["bindings"]["multi_session_engine"]["sha256"]
            ),
            "raw_forward_adapter": (
                envelope["bindings"]["raw_packet_adapter"]["sha256"]
                == _sha256(_SYSTEM_REPLAY_ADAPTER_PATH)
            ),
            "runtime_gate": _sha256(_SYSTEM_REPLAY_GATE_PATH)
            == _SYSTEM_REPLAY_GATE_SHA256,
        }
        if (
            not all(implementation.values())
            or replay_protocol.get("protocol_id")
            != "nonconventional_market_cognition_replay_v2"
            or replay_protocol.get("status")
            != "successor_frozen_before_first_real_system_replay_or_performance_evaluation"
            or adapter_protocol.get("protocol_id")
            != "system_route_joint_v2_adapter_protocol_v10"
            or persistence.get("status")
            != "adapter_v10_future_listing_recovery_frozen_for_next_real_session"
            or admission.get("status")
            != "research_authority_and_position_evidence_hardening"
            or envelope.get("status")
            != "retired_legacy_capture_and_execution_fail_closed_release"
            or envelope.get("protocol_id")
            != "system_route_joint_envelope_v2_protocol_amendment_45"
            or envelope.get("predecessor") != {
                "path": "research/SYSTEM_ROUTE_JOINT_ENVELOPE_V2_PROTOCOL_AMENDMENT_44.json",
                "sha256": "095149a43f38824cad8da0b8de7b94ca6c2f72fb5db8b315efcf41313e3437c9",
            }
            or any(
                envelope["strict_forward_contract"].get(key) is not False
                for key in (
                    "legacy_candidate_capture_enabled",
                    "legacy_joint_replay_capture_enabled",
                    "legacy_30m_candidate_capture_enabled",
                    "legacy_next_session_paper_execution_enabled",
                )
            )
            # Each current protocol layer must validate its own direct bytes;
            # otherwise an exact outer document can hide stale nested bindings.
            or not all(bindings_current(document) for document in (
                adapter_protocol, persistence, admission, envelope,
            ))
            or account_protocol.get("protocol_id")
            != "system_route_account_packet_v2_protocol_v1_amendment_1"
            or not bindings_current(account_protocol)
            or admission["verified_capabilities"].get(
                "content_addressed_H1_selection_snapshot"
            ) is not True
            or admission["verified_capabilities"].get(
                "current_snapshot_refresh_cannot_invalidate_new_packet"
            ) is not True
            or admission["verified_capabilities"].get(
                "same_slot_market_sector_emotion_account_required"
            ) is not True
            or admission["verified_capabilities"].get(
                "historical_backfill"
            ) is not False
            or admission["verified_capabilities"].get(
                "performance_evaluation"
            ) is not False
            or admission["verified_capabilities"].get(
                "terminally_failed_mainline_entry_removed_from_future_routes"
            ) is not True
            or admission["verified_capabilities"].get(
                "future_candidate_and_pre_execution_plan_count_is_zero_until_distinct_strategy_admission"
            ) is not True
            or forward_status.get("runtime_admission", {}).get("valid") is not True
        ):
            raise ValueError("successor_system_replay_contract_identity_invalid")
        authority = audit["authority"]
        if (
            audit.get("status")
            != "strict_full_system_historical_replay_not_ready_forward_capture_ready"
            or authority.get("research_only") is not True
            or any(value is not False for key, value in authority.items() if key != "research_only")
        ):
            raise ValueError("system_replay_boundary_invalid")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": (
                "successor_runtime_admission_invalid"
                if forward_status.get("status") == "runtime_admission_invalid"
                else "successor_contract_identity_invalid"
            ),
            "decision_driver_component_ready": False,
            "multi_session_accounting_component_ready": False,
            "end_to_end_contract_ready": False,
            "strict_forward_mode_identity_ready": False,
            "verified_after_session_mark_ready": False,
            "evaluation_ready": False,
            "strict_historical_ready": False,
            "strict_forward_capture": forward_status,
            "stable_profit_proven": False,
            "production_strategy_count": 0,
            "reason": str(exc),
        }
    market_history = audit["coverage"][0]
    forward_phase = forward_status.get("status")
    status = (
        "capturing_strict_forward"
        if forward_phase == "capturing_strict_forward"
        else "successor_runtime_admitted_awaiting_next_real_packet"
    )
    first_real_packet_ready = bool(
        forward_status.get("packet_count", 0) >= 1
        and (forward_status.get("latest") or {}).get("valid") is True
    )
    next_boundary = forward_status.get("next_real_slot_boundary") or {}
    next_window = (
        f"{next_boundary.get('not_before')} 至 {next_boundary.get('capture_until')}"
        if next_boundary.get("not_before") and next_boundary.get("capture_until")
        else "下一真实自动核验时段"
    )
    return {
        "status": status,
        "audit_id": audit["audit_id"],
        "audited_at": audit["audited_at"],
        "decision_driver_component_ready": implementation["decision_driver"],
        "multi_session_accounting_component_ready": implementation["multi_session_engine"],
        "system_replay_protocol_id": replay_protocol["protocol_id"],
        "joint_envelope_protocol_id": envelope["protocol_id"],
        "adapter_protocol_id": adapter_protocol["protocol_id"],
        "persistence_protocol_id": persistence["protocol_id"],
        "deployment_admission_id": admission["admission_id"],
        "adapter_status": "terminally_failed_entry_retired_future_strict_packets_research_only",
        "normalized_raw_to_v2_adapter_exists": True,
        "append_only_persistence_wired": True,
        "capture_orchestrator_wired": True,
        "end_to_end_contract_ready": True,
        "strict_forward_mode_identity_ready": (
            implementation["raw_forward_adapter"] and implementation["runtime_gate"]
        ),
        "verified_after_session_mark_ready": False,
        "evaluation_ready": False,
        "strict_historical_ready": False,
        "diagnostic_market_sessions": market_history["rows"],
        "diagnostic_market_span": market_history["historical_span"],
        "historical_same_slot_packets": 0,
        "audited_forward_counts": audit["current_forward_counts"],
        "strict_forward_capture": forward_status,
        "activation_boundary": {
            **admission["activation_boundary"],
            "first_new_capture_date": admission["forward_start"],
        },
        "minimum_forward_sessions": 60,
        "minimum_calendar_months": 3,
        "closed_sessions_count_toward_forward": False,
        "manual_observation_required": False,
        "next_slot_runs_automatically": True,
        "stable_profit_proven": False,
        "production_strategy_count": 0,
        "reason": (
            "当前版本的首个真实同一观察时点严格包已形成并通过运行身份校验；失败入场战法已从未来候选和账户计划中移除。该单包只是前向采集证据，不是盈利证据。继续不可覆盖积累，绩效评价、生产策略和自动交易保持关闭。"
            if first_real_packet_ready else
            f"运行身份已准入，尚待 {next_window} 的新原生同槽严格包；失败入场战法已从未来候选和账户计划中移除，仅保留市场、板块、账户与退潮风控诊断。旧包不回填、不重写，新策略须另行验证准入，绩效评价、生产策略和自动交易继续关闭。"
        ),
    }


def _style_route_from_verified_capture(
    route: dict[str, Any], strict_capture: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the research-lens context without rewriting the live route truth.

    The current sector adapter may correctly be unavailable after the market
    closes while the latest immutable intraday packet remains valid research
    evidence.  Public-method lenses may explain that verified packet, but they
    must not mutate the current adapter status or gain execution authority.
    """
    style_route = copy.deepcopy(route)
    latest = strict_capture.get("latest")
    runtime = strict_capture.get("runtime_admission")
    context = {
        "source": "current_route_inputs",
        "source_label": "当前时点研究输入",
        "packet_id": None,
        "slot_id": None,
        "as_of": None,
        "market_state": style_route.get("market_state"),
        "confirmed_sectors": copy.deepcopy(
            (style_route.get("sector_confirmation") or {}).get(
                "confirmed_sectors"
            ) or []
        ),
        "research_only": True,
        "applied_to_execution": False,
    }
    if not (
        isinstance(latest, dict) and latest.get("valid") is True
        and isinstance(runtime, dict) and runtime.get("valid") is True
    ):
        style_route["public_trader_style_context"] = context
        return style_route, context

    market_state = _text(latest.get("market_state"))
    confirmed_sectors = [
        copy.deepcopy(row) for row in (latest.get("confirmed_sectors") or [])
        if isinstance(row, dict)
    ]
    allowed_playbooks = [
        {"id": playbook_id}
        for raw in (latest.get("eligible_playbooks") or [])
        if (playbook_id := _text(raw))
    ]
    if market_state:
        style_route["market_state"] = market_state
    if latest.get("decision_status") == "research_decision_ready":
        style_route["status"] = "research_route_ready"
    review_source = str(latest.get("review_source") or "")
    archived_review = review_source == "release_bound_completed_intraday_packet"
    style_route["sector_confirmation"] = {
        "ready": bool(confirmed_sectors),
        "confirmed_sectors": confirmed_sectors,
        "observation_slot": latest.get("slot_id"),
        "as_of": latest.get("decision_at"),
        "source": review_source or "latest_verified_strict_forward_packet",
        "research_only": True,
        "applied_to_execution": False,
    }
    budget = copy.deepcopy(style_route.get("research_budget_hypothesis") or {})
    budget["allowed_playbooks"] = allowed_playbooks
    budget["applied_to_execution"] = False
    style_route["research_budget_hypothesis"] = budget
    context = {
        "source": (
            "release_bound_completed_intraday_packet"
            if archived_review else "latest_verified_strict_forward_packet"
        ),
        "source_label": (
            "今日收盘前已核验的盘中快照（仅复盘）"
            if archived_review else "最近一次已核验的盘中快照"
        ),
        "packet_id": latest.get("packet_id"),
        "slot_id": latest.get("slot_id"),
        "as_of": latest.get("decision_at"),
        "market_state": market_state,
        "confirmed_sectors": copy.deepcopy(confirmed_sectors),
        "research_only": True,
        "applied_to_execution": False,
    }
    style_route["public_trader_style_context"] = context
    return style_route, context


def build_research_route_payload(
    research: dict[str, Any], sectors: dict[str, Any], portfolio: dict[str, Any],
    today_market: dict[str, Any] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    opportunity_context: dict[str, Any] | None = None,
    current_core_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Join one route, account review and evidence lenses without live authority."""
    model = research.get("model_observation") or {}
    market_input = adapt_market_observation(model, as_of=model.get("trade_date"))
    market_input["observation_source"] = copy.deepcopy(
        model.get("observation_source")
    )
    sector_input = adapt_sector_snapshot(sectors)
    route = build_research_strategy_route(
        market_input,
        sector_input,
        {
            "cash_user_recorded": portfolio.get("cash_user_recorded") is True,
            "valuation_status": portfolio.get("valuation_status"),
            "account_snapshot_sha256": portfolio.get(
                "immutable_account_snapshot_sha256"
            ),
        },
    )
    if route.get("market_state") == "unknown":
        route["research_budget_hypothesis"]["allowed_playbooks"] = []
    route["small_capital_playbook_library"] = (
        load_small_capital_playbook_library()
    )
    route["system_identity"] = _operating_system_summary()
    route["state_routed_strategy_cohort"] = _state_routed_strategy_summary()
    route["system_replay_readiness"] = _system_replay_readiness_summary()
    strict_capture = route["system_replay_readiness"].get(
        "strict_forward_capture"
    ) or {}
    boundary = strict_capture.get("next_real_slot_boundary") or {}
    if (
        strict_capture.get("status") == "no_verified_packet"
        and boundary.get("not_before")
        and boundary.get("capture_until")
    ):
        route["next_action"] = (
            f"系统将在 {boundary['not_before']} 至 {boundary['capture_until']} 自动采集首个真实同槽包；"
            "休市日不计样本、无需人工观察，也不回填旧数据。采集后联合核验市场状态、"
            "板块、战法、账户、成本与执行；完整优势未形成前不新开仓。"
        )
    completed_review = _completed_market_review_snapshot(strict_capture)
    route["completed_market_review"] = completed_review
    route["strict_forward_decision"] = {
        "status": strict_capture.get("status") or "unavailable",
        "latest": strict_capture.get("latest"),
        "packet_count": strict_capture.get("packet_count", 0),
        "runtime_admission": strict_capture.get("runtime_admission") or {},
        "research_only": True,
        "applied_to_execution": False,
        "automatic_trading": False,
    }
    route["current_slot_research_review"] = copy.deepcopy(
        current_core_review
        if isinstance(current_core_review, dict)
        else {
            "status": "no_verified_core_route", "valid": False,
            "trade_action_authority": False,
        }
    )
    core_intraday = route["current_slot_research_review"].get(
        "intraday_emotion_observation"
    )
    if isinstance(core_intraday, dict):
        route["current_slot_research_review"]["intraday_emotion_observation"] = (
            scope_v1_emotion_observation(core_intraday)
        )
    style_capture = {
        "latest": completed_review,
        "runtime_admission": {"valid": completed_review.get("valid") is True},
    }
    style_route, style_context = _style_route_from_verified_capture(
        route, style_capture,
    )
    distillation = load_trader_style_distillation()
    style_route["public_trader_style_role"]["distillation"] = distillation
    style_lenses = build_trader_style_lenses(style_route)
    style_lenses["context"] = style_context
    style_route["public_trader_style_role"]["style_lenses"] = style_lenses
    style = route["public_trader_style_role"]
    style["distillation"] = distillation
    style["style_lenses"] = style_lenses
    route["account_exposure_review"] = build_research_exposure_review(
        route, portfolio,
    )
    route["intraday_emotion_observation"] = build_intraday_emotion_observation(
        route, today_market,
    )
    next_state = load_transition_forecast()
    next_state_as_of_matches = bool(
        next_state.get("as_of")
        and next_state.get("as_of") == model.get("trade_date")
    )
    next_state["source_as_of_matches_current_market_state"] = next_state_as_of_matches
    next_state["applied_to_current_route"] = False
    next_state["applied_to_live_exposure"] = False
    if not next_state_as_of_matches:
        next_state["research_display_admitted"] = False
        next_state["stale_reason"] = "forecast_as_of_does_not_match_current_market_state"
    route["next_market_state_forecast"] = next_state
    route["source_truth"] = {
        "market_state": research.get("state") or "UNKNOWN",
        "market_research_input_status": market_input.get("status"),
        "market_decision_eligible": research.get("decision_eligible") is True,
        "sector_status": sectors.get("status") or "unavailable",
        "sector_research_input_status": sector_input.get("status"),
        "sector_participates_in_decision": (
            sectors.get("participates_in_decision") is True
        ),
        "cash_user_recorded": portfolio.get("cash_user_recorded") is True,
        "portfolio_valuation_status": portfolio.get("valuation_status"),
        "next_state_forecast_as_of_matches": next_state_as_of_matches,
    }
    route["input_adapter"] = {
        "protocol_path": str(PROTOCOL_PATH.relative_to(PROJECT_ROOT)),
        "protocol_sha256": PROTOCOL_SHA256,
        "market": market_input,
        "sector": sector_input,
        "applied_to_execution": False,
    }
    interpretations = _candidate_interpretation_summary(candidates, style_route)
    route["unified_candidate_interpretations"] = interpretations
    route["today_decision_workspace"] = _today_decision_workspace(
        route, portfolio, opportunity_context, interpretations,
    )
    route["mainline_diagnostics"] = build_mainline_diagnostics(sectors, sector_input)
    return route


def _cloud_market_observation(runtime_root: Path) -> dict[str, Any] | None:
    """Project a complete, source-bound cloud EOD snapshot for research display."""
    try:
        from .cloud_market_emotion import IMPLEMENTATION_SHA256, SCHEMA

        report = json.loads((runtime_root / "reports/cloud_market_emotion_progress.json").read_text())
        day = report.get("data_as_of")
        if (not isinstance(day, str) or date.fromisoformat(day).isoformat() != day
                or report.get("status") != "complete"
                or report.get("implementation_sha256") != IMPLEMENTATION_SHA256):
            return None
        directory = runtime_root / "reports/cloud_market_emotion" / day
        manifest = json.loads((directory / "manifest.json").read_text())
        reference = manifest.get("snapshot") or {}
        path = runtime_root / str(reference.get("path") or "")
        if (manifest.get("target_date") != day or path.is_symlink()
                or path.parent.resolve() != directory.resolve()
                or not path.is_file() or path.stat().st_size > 20_000_000):
            return None
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != reference.get("sha256"):
            return None
        snapshot = json.loads(raw)
        rows = snapshot.get("rows") or []
        last = rows[-1] if rows else {}
        features = last.get("features") or {}
        coverage = last.get("coverage") or {}
        state = features.get("confirmed_state")
        raw_state = features.get("raw_state")
        states = {"RISK_OFF", "STABILIZING", "RISK_ON", "OVERHEATED"}
        if (snapshot.get("schema") != SCHEMA or snapshot.get("data_as_of") != day
                or str(last.get("trade_date") or "")[:10] != day
                or snapshot.get("input_identity") != reference.get("input_identity")
                or (snapshot.get("source_contract") or {}).get("implementation_sha256") != IMPLEMENTATION_SHA256
                or snapshot.get("latest_state") != state or report.get("latest_state") != state
                or state not in states or raw_state not in states
                or type(coverage.get("missing_listed_sessions")) is not int
                or coverage["missing_listed_sessions"] != 0
                or type(coverage.get("unresolved_limit_sessions")) is not int
                or coverage["unresolved_limit_sessions"] != 0
                or coverage.get("raw_counts_diagnostic_only") is not False
                or type(report.get("target")) is not int
                or report.get("target") != report.get("histories_computed")
                or len(snapshot.get("source_references") or {}) != report["target"]
                or snapshot.get("production_eligible") is not False
                or snapshot.get("generates_alert") is not False
                or snapshot.get("state_model_validated") is not False):
            return None
        percentiles = {}
        for key in REQUIRED_PERCENTILES:
            value = _number(features.get(key + "_percentile"))
            if value is None or not 0 <= value <= 1:
                return None
            percentiles[key] = round(value * 100, 3)
        exposure = _number(features.get("research_exposure_hypothesis_pct"))
        if exposure is None or not 0 <= exposure <= 100:
            return None
        return {
            "status": "audited_diagnostic_not_validated", "trade_date": day,
            "state": state, "raw_state": raw_state,
            "research_exposure_hypothesis_pct": exposure,
            "percentiles_pct": percentiles,
            "profitability_tested": False, "applied_to_live_positions": False,
            "applied_to_score": False,
            "observation_source": {
                "path": str(path.relative_to(runtime_root)),
                "sha256": reference["sha256"],
                "kind": "verified_cloud_market_emotion_snapshot",
                "coverage": "sh_sz_research_universe_only",
                "full_runtime_chain_present": False,
            },
            "reason": "云端日终情绪快照仅供研究展示；未验证预测收益，不生成提醒或仓位。",
        }
    except (OSError, ValueError, KeyError, TypeError, IndexError, json.JSONDecodeError):
        return None


def latest_route_market_observation(
    fallback: dict[str, Any] | None, *, project_root: Path = PROJECT_ROOT,
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    """Use the latest verified append-only EOD feature for the next route.

    The audited base epoch remains immutable.  This read model only advances
    the research display and route input; it does not grant position, alert or
    trading authority.
    """
    observation = copy.deepcopy(fallback) if isinstance(fallback, dict) else {}
    cloud_root = runtime_root or (Path(os.environ["REGIS_RUNTIME_ROOT"])
                                  if project_root == PROJECT_ROOT and os.environ.get("REGIS_RUNTIME_ROOT")
                                  else project_root)
    cloud = _cloud_market_observation(cloud_root)
    if cloud and str(cloud["trade_date"]) > str(observation.get("trade_date") or ""):
        observation = cloud
    daily_dir = project_root / "research/data/market_state_daily/v1/feature_daily_v2"
    daily_paths = sorted(daily_dir.glob("*.json"))
    if not daily_paths:
        release_bound = _release_bound_market_observation(
            project_root=project_root,
        )
        release_date = str((release_bound or {}).get("trade_date") or "")
        fallback_date = str(observation.get("trade_date") or "")
        if release_bound and (
            release_date > fallback_date
            or (
                release_date == fallback_date
                and observation.get("state") not in {
                    "RISK_OFF", "STABILIZING", "RISK_ON", "OVERHEATED",
                }
            )
        ):
            return release_bound
        return observation
    try:
        from research.publish_market_state_feature_daily_v2 import (
            verify_daily_feature,
        )

        verified = verify_daily_feature(daily_paths[-1], project_root=project_root)
        if verified.get("valid") is not True:
            raise ValueError(str(verified.get("errors") or []))
        row = (verified.get("document") or {}).get("row") or {}
        features = row.get("features") or {}
        if str(row.get("trade_date") or "") <= str(
            observation.get("trade_date") or ""
        ):
            return observation
        if features.get("applied_to_live_positions") is not False:
            raise ValueError("daily_feature_live_position_boundary_invalid")
        return {
            "status": "audited_diagnostic_not_validated",
            "trade_date": row["trade_date"],
            "state": features["confirmed_state"],
            "raw_state": features["raw_state"],
            "research_exposure_hypothesis_pct": features[
                "research_exposure_hypothesis_pct"
            ],
            "percentiles_pct": {
                key.removesuffix("_percentile"): round(float(value) * 100, 3)
                for key, value in features.items()
                if key.endswith("_percentile")
                and isinstance(value, (int, float))
            },
            "profitability_tested": False,
            "applied_to_live_positions": False,
            "applied_to_score": False,
            "observation_source": {
                "path": str(daily_paths[-1].relative_to(project_root)),
                "sha256": verified["feature_sha256"],
                "kind": "verified_append_only_daily_feature",
            },
            "reason": (
                "最新收盘特征已核验；状态模型尚无样本外收益证据，"
                "只用于研究路由，不直接控制真钱仓位。"
            ),
        }
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        release_bound = _release_bound_market_observation(
            project_root=project_root,
        )
        if release_bound and str(release_bound.get("trade_date") or "") >= str(
            observation.get("trade_date") or ""
        ):
            release_bound["route_input_error"] = (
                f"latest_daily_feature_invalid_using_release_bound_fallback:{exc}"
            )
            return release_bound
        return {
            **observation,
            "status": "unavailable_or_invalid",
            "route_input_error": f"latest_daily_feature_invalid:{exc}",
            "profitability_tested": False,
            "applied_to_live_positions": False,
            "applied_to_score": False,
        }


_DASHBOARD_ROUTE_FIELDS = (
    "route_version", "status", "market_state", "requested_market_state",
    "market_state_as_of", "sector_confirmation", "research_budget_hypothesis",
    "evidence", "contradictions", "missing_inputs", "authority", "next_action",
    "small_capital_playbook_library", "system_replay_readiness",
    "completed_market_review", "intraday_emotion_observation",
    "next_market_state_forecast", "public_trader_style_role",
    "today_decision_workspace",
    "current_slot_research_review",
    "mainline_diagnostics",
)


def dashboard_research_route_payload(payload: object) -> dict[str, Any]:
    """Project the full audit packet to the fields rendered on the dashboard.

    Per-stock trader-lens interpretations remain available on the dedicated
    live-scan page through the default full response.  The dashboard refreshes
    every minute and must not repeatedly transfer that large, unused packet.
    """
    source = payload if isinstance(payload, dict) else {}
    projected = {
        key: copy.deepcopy(source[key])
        for key in _DASHBOARD_ROUTE_FIELDS if key in source
    }
    projected["response_view"] = "dashboard"
    projected["omitted_full_evidence"] = {
        "unified_candidate_interpretations": (
            "/api/strategy-route/research"
        ),
        "semantics": "dashboard_projection_only_no_authority_change",
    }
    return projected


__all__ = ["build_research_route_payload", "dashboard_research_route_payload"]
