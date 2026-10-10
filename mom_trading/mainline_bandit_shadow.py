"""Research-only first RL layer: logged, one-step emotion-conditioned bandit.

No counterfactual reward is invented. The estimator refuses missing behavior
propensities or unsupported target actions; it never changes trade routing.
Sequential position-control RL is explicitly outside this first layer.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Sequence

from .market_sector_strategy_adapter import SH_TZ
from .market_state_daily import PROJECT_ROOT


EVENT_SCHEMA = "mainline_bandit_logged_event.v1"
RESULT_SCHEMA = "mainline_bandit_shadow_result.v1"
ACTIONS = ("observe", "de_risk", "review_candidate")
MARKET_STATES = ("RISK_OFF", "STABILIZING", "RISK_ON", "OVERHEATED")
BOARD_STATES = ("none", "emerging", "persistent", "diverging", "retreat")
RISK_BANDS = ("flat", "low", "medium", "high")
HORIZONS = (1, 3, 5)
MIN_TRAIN_DAYS = 30
MIN_TEST_DAYS = 20
MIN_ACTION_ROWS = 10
MIN_ACTION_ESS = 10
MIN_OPE_ESS = 20
MIN_PROPENSITY = 1e-6  # Numerical floor only; ESS still rejects weak support.
PURGE_CALENDAR_DAYS = 10  # Conservative buffer for 1-5 A-share sessions.
BOUNDARY = {"research_only": True, "production_eligible": False,
            "generates_alert": False, "automatic_orders": False,
            "sequential_rl_complete": False}


def _bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: object) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _stamp(value: object, field: str) -> datetime:
    try:
        result = datetime.fromisoformat(str(value))
        if result.tzinfo is None:
            raise ValueError(field)
        return result.astimezone(SH_TZ)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}_invalid") from exc


def _hex(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _validate(row: object, frozen_at: datetime) -> dict[str, Any]:
    if not isinstance(row, dict) or row.get("schema") != EVENT_SCHEMA:
        raise ValueError("event_schema_invalid")
    decision = _stamp(row.get("decision_at"), "decision_at")
    settled = _stamp(row.get("settled_at"), "settled_at")
    if not decision < settled <= frozen_at:
        raise ValueError("event_not_settled_by_freeze")
    if row.get("horizon_sessions") not in HORIZONS or type(row["horizon_sessions"]) is not int:
        raise ValueError("horizon_invalid")
    for key in ("source_packet_id", "decision_packet_id", "execution_record_id", "outcome_record_id"):
        if not _hex(row.get(key)):
            raise ValueError(f"{key}_invalid")
    state = row.get("state")
    if (not isinstance(state, dict) or state.get("market") not in MARKET_STATES
            or state.get("board") not in BOARD_STATES or state.get("risk") not in RISK_BANDS):
        raise ValueError("state_invalid")
    available = row.get("available_actions")
    if (not isinstance(available, list) or not available or len(available) != len(set(map(str, available)))
            or any(action not in ACTIONS for action in available)
            or "observe" not in available):
        raise ValueError("actions_invalid")
    if state["market"] in {"RISK_OFF", "OVERHEATED"} and "review_candidate" in available:
        raise ValueError("risk_state_cannot_add_risk")
    behavior = row.get("behavior_probabilities")
    if not isinstance(behavior, dict) or set(behavior) != set(available):
        raise ValueError("behavior_distribution_invalid")
    probabilities = list(behavior.values())
    if (any(type(p) not in (float, int) or not math.isfinite(p)
            or not MIN_PROPENSITY <= p <= 1 for p in probabilities)
            or abs(sum(probabilities) - 1) > 1e-8):
        raise ValueError("behavior_distribution_invalid")
    action = row.get("behavior_action")
    if action not in available:
        raise ValueError("behavior_action_invalid")
    reward = row.get("net_excess_return_pct")
    if type(reward) not in (float, int) or not math.isfinite(reward) or not -100 <= reward <= 100:
        raise ValueError("net_reward_invalid")
    if row.get("reward_basis") != "actual_or_predeclared_execution_replay_after_fees_vs_same_origin_baseline":
        raise ValueError("reward_basis_invalid")
    return {**row, "_decision": decision, "_settled": settled,
            "_state": (state["market"], state["board"], state["risk"])}


def _policy_for(rows: Sequence[dict[str, Any]]) -> dict[tuple[str, str, str], str]:
    cells: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        cells[row["_state"]].append(row)
    policy = {}
    for state, state_rows in cells.items():
        choices = []
        for action in ACTIONS:
            if (state[0] in {"RISK_OFF", "OVERHEATED"} and action == "review_candidate"
                    or any(action not in row["available_actions"] for row in state_rows)):
                continue
            observed = [row for row in state_rows if row["behavior_action"] == action]
            if len(observed) < MIN_ACTION_ROWS:
                continue
            weights = [1.0 / float(row["behavior_probabilities"][action]) for row in observed]
            total = sum(weights)
            ess = total * total / sum(weight * weight for weight in weights)
            if ess < MIN_ACTION_ESS:
                continue
            # Use logged propensities to correct unequal action selection within
            # the coarse state. This penalty is a ranking heuristic, not a CI.
            rewards = [float(row["net_excess_return_pct"]) for row in observed]
            estimate = sum(weight * reward for weight, reward in zip(weights, rewards)) / total
            dispersion = sum(weight * (reward - estimate) ** 2
                             for weight, reward in zip(weights, rewards)) / total
            score = estimate - 1.96 * math.sqrt(dispersion / ess)
            choices.append((score, -ACTIONS.index(action), action))
        if choices:
            policy[state] = max(choices)[2]
    return policy


def _snips(rows: Sequence[dict[str, Any]], policy: dict[tuple[str, str, str], str]) -> tuple[float | None, float]:
    weights = []
    weighted_rewards = []
    for row in rows:
        target = policy.get(row["_state"])
        if target is None:
            return None, 0.0
        weight = (1 / float(row["behavior_probabilities"][target])
                  if row["behavior_action"] == target else 0.0)
        weights.append(weight)
        weighted_rewards.append(weight * float(row["net_excess_return_pct"]))
    total = sum(weights)
    squared = sum(weight * weight for weight in weights)
    return (sum(weighted_rewards) / total if total else None,
            total * total / squared if squared else 0.0)


def train_shadow(events: Sequence[object], *, frozen_at: datetime) -> dict[str, Any]:
    """Freeze a chronological holdout and report only estimable shadow comparisons."""
    frozen = _stamp(frozen_at.isoformat(), "frozen_at")
    base = {"schema": RESULT_SCHEMA, "frozen_at": frozen.isoformat(),
            "economic_target": "A_tradable_leader_basket_net_excess_after_fill_and_costs",
            "horizons": list(HORIZONS), "input_rows": len(events),
            "learning_support_contract": {
                "method": "within_state_inverse_propensity_weighted_action_mean",
                "selection_penalty": "descriptive_1_96_weighted_se_not_confidence_interval",
                "min_action_rows": MIN_ACTION_ROWS, "min_action_ess": MIN_ACTION_ESS,
                "min_behavior_probability": MIN_PROPENSITY,
            },
            "input_evidence_verified": False, "economic_reward_verified": False,
            "off_policy_value_validated": False,
            "receipt_limitation": "self_reported_ids_and_rewards_not_reconciled_to_execution_fee_baseline_and_outcome_artifacts",
            **BOUNDARY}
    if not events:
        return {**base, "status": "no_logged_events", "reasons": ["no_events"],
                "policy": None, "ope": None}
    try:
        rows = [_validate(event, frozen) for event in events]
    except ValueError as exc:
        return {**base, "status": "invalid_log", "reasons": [str(exc)], "policy": None, "ope": None}
    decisions: dict[str, dict[str, Any]] = {}
    for row in rows:
        previous = decisions.get(row["decision_packet_id"])
        if previous is None:
            decisions[row["decision_packet_id"]] = {"row": row, "horizons": {row["horizon_sessions"]}}
            continue
        before = previous["row"]
        fixed = ("decision_at", "source_packet_id", "execution_record_id", "state",
                 "available_actions", "behavior_probabilities", "behavior_action")
        if (row["horizon_sessions"] in previous["horizons"]
                or any(row[key] != before[key] for key in fixed)):
            return {**base, "status": "invalid_log", "reasons": ["decision_packet_conflict"],
                    "policy": None, "ope": None}
        previous["horizons"].add(row["horizon_sessions"])
    days = sorted({row["_decision"].date() for row in rows})
    if len(days) < MIN_TRAIN_DAYS + MIN_TEST_DAYS + PURGE_CALENDAR_DAYS:
        return {**base, "status": "insufficient_sessions", "reasons": ["too_few_distinct_days"],
                "observed_days": len(days), "policy": None, "ope": None}
    first_test_day = days[-MIN_TEST_DAYS]
    train_end = first_test_day - timedelta(days=PURGE_CALENDAR_DAYS)
    train = [row for row in rows if row["_decision"].date() < train_end
             and row["_settled"].date() < first_test_day]
    test = [row for row in rows if row["_decision"].date() >= first_test_day]
    if len({row["_decision"].date() for row in train}) < MIN_TRAIN_DAYS:
        return {**base, "status": "insufficient_sessions", "reasons": ["training_days_after_purge"],
                "policy": None, "ope": None}
    # Avoid repeated use of overlapping one-, three- and five-day labels in a
    # single fit; separate holdout estimates are required for each horizon.
    horizon_results: dict[str, Any] = {}
    for horizon in HORIZONS:
        training = [row for row in train if row["horizon_sessions"] == horizon]
        holdout = [row for row in test if row["horizon_sessions"] == horizon]
        if not training or not holdout:
            horizon_results[str(horizon)] = {"status": "missing_horizon_rows"}
            continue
        policy = _policy_for(training)
        test_states = {row["_state"] for row in holdout}
        if not test_states.issubset(policy):
            horizon_results[str(horizon)] = {"status": "unseen_or_unsupported_state_action",
                                             "train_rows": len(training), "test_rows": len(holdout)}
            continue
        if any(policy[row["_state"]] not in row["available_actions"] for row in holdout):
            horizon_results[str(horizon)] = {"status": "unsupported_target_action",
                                             "train_rows": len(training), "test_rows": len(holdout)}
            continue
        target_value, target_ess = _snips(holdout, policy)
        baseline = {state: "observe" for state in test_states}
        baseline_value, baseline_ess = _snips(holdout, baseline)
        if (target_value is None or baseline_value is None
                or min(target_ess, baseline_ess) < MIN_OPE_ESS):
            horizon_results[str(horizon)] = {"status": "insufficient_ope_support",
                                             "target_ess": round(target_ess, 3),
                                             "baseline_ess": round(baseline_ess, 3),
                                             "train_rows": len(training), "test_rows": len(holdout)}
            continue
        horizon_results[str(horizon)] = {
            "status": "algorithmic_oos_calculation_unverified_inputs", "train_rows": len(training),
            "test_rows": len(holdout), "target_ess": round(target_ess, 3),
            "baseline_ess": round(baseline_ess, 3),
            "target_snips_pct": round(target_value, 6),
            "observe_snips_pct": round(baseline_value, 6),
            "difference_pct": round(target_value - baseline_value, 6),
            "policy_cells": {"|".join(key): action for key, action in sorted(policy.items())},
        }
    passed = all(item["status"] == "algorithmic_oos_calculation_unverified_inputs"
                 for item in horizon_results.values())
    return {**base, "status": "algorithmic_oos_calculation_unverified_inputs" if passed else "not_evaluable",
            "train_end_exclusive": train_end.isoformat(), "test_start": first_test_day.isoformat(),
            "train_days": len({row["_decision"].date() for row in train}),
            "test_days": len({row["_decision"].date() for row in test}),
            "horizon_results": horizon_results,
            "policy": None, "ope": None,
            "reasons": [] if passed else ["one_or_more_horizons_not_evaluable"]}


def current_status(*, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Never train from the web request; display the actual ignored receipt only."""
    path = root / "reports" / "mainline_bandit_shadow" / "receipt.json"
    base = {"schema": "mainline_bandit_shadow_status.v1",
            "algorithm": "offline_contextual_bandit_one_step",
            "economic_target": "A_tradable_leader_basket_net_excess_after_fill_and_costs",
            "horizons": list(HORIZONS), **BOUNDARY}
    try:
        if path.is_symlink() or path.stat().st_size > 200_000:
            raise ValueError("receipt_path_invalid")
        result = json.loads(path.read_text(encoding="utf-8"))
        body = dict(result)
        identity = body.pop("receipt_sha256", None)
        if identity != _sha(body) or result.get("schema") != RESULT_SCHEMA:
            raise ValueError("receipt_identity_invalid")
        calculation = result.get("status")
        return {**base, "status": "unverified_log_research_only" if calculation ==
                "algorithmic_oos_calculation_unverified_inputs" else calculation,
                "calculation_status": calculation,
                "input_evidence_verified": False, "economic_reward_verified": False,
                "off_policy_value_validated": False,
                "input_rows": result.get("input_rows"),
                "train_days": result.get("train_days"), "test_days": result.get("test_days"),
                "horizon_status": {key: item.get("status") for key, item in
                                   (result.get("horizon_results") or {}).items()},
                "receipt_sha256": identity}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {**base, "status": "no_verified_real_log", "input_rows": 0,
                "train_days": 0, "test_days": 0, "horizon_status": {}}


def publish_shadow(events: Sequence[object], *, root: Path = PROJECT_ROOT,
                   frozen_at: datetime | None = None) -> dict[str, Any]:
    """Explicit offline invocation only; raw user/account logs stay ignored."""
    result = train_shadow(events, frozen_at=frozen_at or datetime.now(SH_TZ))
    result["receipt_sha256"] = _sha(result)
    directory = root / "reports" / "mainline_bandit_shadow"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "receipt.json"
    temporary = directory / f"receipt.{hashlib.sha256(_bytes(result)).hexdigest()[:8]}.tmp"
    temporary.write_bytes(_bytes(result))
    temporary.replace(path)
    return result
