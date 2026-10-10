"""Pure trade-lifecycle decisions from caller-supplied facts.

This module deliberately does not read quotes, databases, clocks, or strategy
files.  The caller owns evidence collection; this function only turns complete
facts into a manual-confirmation plan.
"""
from __future__ import annotations

import math
from copy import deepcopy
from collections.abc import Mapping
from typing import Any


WAIT_ENTRY = "WAIT_ENTRY"
ENTRY_READY = "ENTRY_READY"
HOLD = "HOLD"
REDUCE_REVIEW = "REDUCE_REVIEW"
EXIT_REVIEW = "EXIT_REVIEW"
CLOSED = "CLOSED"

_EXECUTABLE_ENTRY_STATUSES = {
    "EXECUTABLE",
    "ENTRY_READY",
    "PRICE_CONDITION_MET",
}


def _shares(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0 or not parsed.is_integer():
        return None
    return int(parsed)


def _number(value: Any, *, allow_zero: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0 or (parsed == 0 and not allow_zero):
        return None
    return parsed


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _entry_is_executable(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, Mapping):
        return value.get("executable") is True
    return isinstance(value, str) and value.strip().upper() in _EXECUTABLE_ENTRY_STATUSES


def _has_evidence_identity(value: Any) -> bool:
    return bool(_text(value)) if isinstance(value, str) else isinstance(value, Mapping) and bool(value)


def _observations(facts: Mapping[str, Any]) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    supplied = facts.get("observations", facts.get("heuristics", []))
    if isinstance(supplied, (list, tuple)):
        for item in supplied:
            observation = deepcopy(dict(item)) if isinstance(item, Mapping) else {"detail": str(item)}
            observation["actionable"] = False
            observations.append(observation)

    entry = _number(facts.get("entry_price"))
    current = _number(facts.get("current_price"))
    trailing_active = facts.get("trailing_risk_activated") is True
    if entry is not None and current is not None and current >= entry * 1.15:
        trailing_active = True
    if trailing_active and not any(row.get("kind") == "trailing_risk_activated" for row in observations):
        observations.append({
            "kind": "trailing_risk_activated",
            "threshold_pct": 15.0,
            "actionable": False,
        })
    return observations


def _action(
    facts: Mapping[str, Any], *, side: str, shares: int | None,
    target_shares: int | None, limit_price: Any,
) -> dict[str, Any] | None:
    evidence_identity = facts.get("evidence_identity")
    if shares is None or shares <= 0 or target_shares is None or not _has_evidence_identity(evidence_identity):
        return None
    action = {
        "side": side,
        "shares": shares,
        "target_shares": target_shares,
        "limit_price": _number(limit_price),
        "valid_until": _text(facts.get("valid_until")),
        "cancel_condition": _text(facts.get("cancel_condition")),
        "estimated_cost": _number(facts.get("estimated_cost"), allow_zero=True),
        "evidence_identity": deepcopy(evidence_identity),
        "manual_confirmation": True,
    }
    # A quantity is not a complete manual decision without the exact strategy,
    # playbook and evidence clock that produced it.  Keep these fields optional
    # for non-actionable legacy observations, but preserve them verbatim when
    # the caller has supplied an auditable identity.
    for key in (
        "playbook_id", "strategy_id", "strategy_version",
        "evidence_grade", "as_of",
    ):
        value = _text(facts.get(key))
        if value is not None:
            action[key] = value
    return action


def build_trade_plan(facts: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return one lifecycle state and, only with exact evidence/quantity, an action.

    ``hard_risk_triggered`` and ``strategy_exit_triggered`` are trusted explicit
    facts.  ``observations``/``heuristics`` never create quantity actions.
    """
    facts = facts if isinstance(facts, Mapping) else {}
    observations = _observations(facts)
    current = _shares(facts.get("current_shares"))
    target = _shares(facts.get("target_shares"))

    if facts.get("closed") is True:
        return {"state": CLOSED, "action": None, "observations": observations}

    if facts.get("hard_risk_triggered") is True or facts.get("strategy_exit_triggered") is True:
        return {
            "state": EXIT_REVIEW,
            "action": _action(
                facts, side="SELL", shares=current,
                target_shares=0, limit_price=facts.get("limit_price"),
            ),
            "observations": observations,
        }

    if current is not None and current > 0:
        reduction = current - target if target is not None else None
        if reduction is not None and reduction > 0 and reduction % 100 == 0:
            return {
                "state": REDUCE_REVIEW,
                "action": _action(
                    facts, side="SELL", shares=reduction,
                    target_shares=target, limit_price=facts.get("limit_price"),
                ),
                "observations": observations,
            }
        return {"state": HOLD, "action": None, "observations": observations}

    entry_price = _number(facts.get("entry_price"))
    valid_until = _text(facts.get("valid_until"))
    entry_ready = (
        _entry_is_executable(facts.get("entry_status"))
        and entry_price is not None
        and valid_until is not None
        and target is not None
        and target > 0
        and target % 100 == 0
    )
    if not entry_ready:
        return {"state": WAIT_ENTRY, "action": None, "observations": observations}
    return {
        "state": ENTRY_READY,
        "action": _action(
            facts, side="BUY", shares=target,
            target_shares=target, limit_price=entry_price,
        ),
        "observations": observations,
    }


__all__ = [
    "WAIT_ENTRY", "ENTRY_READY", "HOLD", "REDUCE_REVIEW",
    "EXIT_REVIEW", "CLOSED", "build_trade_plan",
]
