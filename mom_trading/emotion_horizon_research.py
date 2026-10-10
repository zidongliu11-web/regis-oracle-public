"""Research-only 1–5 session emotion forecasts with point-in-time training.

The caller supplies a verified official calendar and immutable daily snapshots.
Nothing in this module grants a trading route, produces returns or places orders.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, time
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
STATES = ("RISK_OFF", "STABILIZING", "RISK_ON", "OVERHEATED")
HORIZONS = (1, 2, 3, 4, 5)
TEMPERATURE_BINS = (0.0, 33.3333333333, 66.6666666667, 100.0)
ALPHA = 1.0
MINIMUM_PRIOR_PAIRS = 30
MINIMUM_STATE_CELL = 30
MINIMUM_TEMPERATURE_CELL = 30
PERSISTENCE_MASS = 0.97
BOUNDARY = {
    "research_only": True,
    "production_eligible": False,
    "applied_to_current_route": False,
    "applied_to_live_exposure": False,
    "automatic_trading": False,
    "account_returns_computed": False,
}


def _stamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name}_required")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name}_invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name}_timezone_required")
    return parsed.astimezone(SHANGHAI)


def _day(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name}_required")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name}_invalid") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{name}_invalid")
    return value


def _temperature_bin(value: float) -> int:
    return 0 if value < TEMPERATURE_BINS[1] else 1 if value < TEMPERATURE_BINS[2] else 2


def validate_input(rows: Sequence[Mapping[str, Any]], calendar: Sequence[str]) -> list[dict[str, Any]]:
    """Reject stale, future, cross-scope or incomplete calendar input."""
    if not rows or not calendar:
        raise ValueError("emotion_rows_and_calendar_required")
    sessions = [_day(day, "calendar_day") for day in calendar]
    if sessions != sorted(set(sessions)):
        raise ValueError("emotion_calendar_not_unique_ordered")
    normalized: list[dict[str, Any]] = []
    previous_day: str | None = None
    fixed_identity: tuple[str, str, str] | None = None
    fixed_temperature_definition: str | None = None
    for offset, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise ValueError("emotion_row_invalid")
        day = _day(raw.get("trade_date"), "trade_date")
        if offset >= len(sessions) or day != sessions[offset] or (previous_day and day <= previous_day):
            raise ValueError("emotion_calendar_gap_or_duplicate")
        previous_day = day
        identity = tuple(str(raw.get(key) or "") for key in (
            "scope_id", "taxonomy_version", "feature_schema_version"
        ))
        if any(not part for part in identity):
            raise ValueError("emotion_identity_required")
        if fixed_identity is not None and identity != fixed_identity:
            raise ValueError("emotion_scope_or_version_changed")
        fixed_identity = identity
        snapshot_hash = raw.get("source_snapshot_sha256")
        universe_hash = raw.get("universe_snapshot_sha256")
        for name, value in (("source_snapshot", snapshot_hash), ("universe_snapshot", universe_hash)):
            if (not isinstance(value, str) or len(value) != 64
                    or any(c not in "0123456789abcdef" for c in value)):
                raise ValueError(f"emotion_{name}_sha256_required")
        state = raw.get("state")
        if state not in (*STATES, "UNKNOWN"):
            raise ValueError("emotion_state_invalid")
        observed = _stamp(raw.get("observed_at"), "observed_at")
        source_available = _stamp(raw.get("source_snapshot_available_at"), "source_snapshot_available_at")
        universe_available = _stamp(raw.get("universe_available_at"), "universe_available_at")
        available = _stamp(raw.get("state_available_at"), "state_available_at")
        decision = _stamp(raw.get("decision_at"), "decision_at")
        if (observed.date().isoformat() != day or decision.date().isoformat() != day
                or observed.timetz().replace(tzinfo=None) < time(15, 0)
                or decision.timetz().replace(tzinfo=None) < time(15, 0)
                or not observed <= source_available <= available <= decision
                or universe_available > available):
            raise ValueError("emotion_state_not_available_at_decision")
        temperature = raw.get("temperature")
        temperature_at: datetime | None = None
        temperature_definition = raw.get("temperature_definition_id")
        if temperature is not None:
            if (type(temperature) not in (int, float) or not math.isfinite(temperature)
                    or not 0 <= temperature <= 100):
                raise ValueError("emotion_temperature_invalid")
            temperature_at = _stamp(raw.get("temperature_available_at"), "temperature_available_at")
            if not isinstance(temperature_definition, str) or not temperature_definition.strip():
                raise ValueError("emotion_temperature_definition_required")
            if not source_available <= temperature_at <= decision:
                raise ValueError("emotion_temperature_not_available_at_decision")
        elif raw.get("temperature_available_at") is not None or temperature_definition is not None:
            raise ValueError("emotion_temperature_value_missing")
        if temperature_definition is not None:
            if fixed_temperature_definition is not None and temperature_definition != fixed_temperature_definition:
                raise ValueError("emotion_temperature_definition_changed")
            fixed_temperature_definition = temperature_definition
        normalized.append({
            "trade_date": day, "state": state, "temperature": float(temperature) if temperature is not None else None,
            "observed_at": observed, "source_snapshot_available_at": source_available,
            "universe_available_at": universe_available, "state_available_at": available,
            "temperature_available_at": temperature_at, "decision_at": decision,
            "temperature_definition_id": temperature_definition,
            "source_snapshot_sha256": snapshot_hash,
            "universe_snapshot_sha256": universe_hash, "identity": identity,
        })
    return normalized


def _smoothed(counts: Mapping[str, int]) -> dict[str, float]:
    total = sum(counts.values()) + ALPHA * len(STATES)
    return {state: (counts[state] + ALPHA) / total for state in STATES}


def _persistence(state: str) -> dict[str, float]:
    other = (1.0 - PERSISTENCE_MASS) / (len(STATES) - 1)
    return {candidate: PERSISTENCE_MASS if candidate == state else other for candidate in STATES}


def _origin_forecast(rows: list[dict[str, Any]], origin: int, horizon: int,
                     calendar: Sequence[str]) -> dict[str, Any]:
    current = rows[origin]
    if current["state"] not in STATES:
        return {"status": "origin_state_unknown", "origin_date": current["trade_date"], "horizon": horizon}
    if origin + horizon >= len(calendar):
        return {"status": "future_calendar_missing", "origin_date": current["trade_date"], "horizon": horizon}
    counts = {state: 0 for state in STATES}
    conditional = {state: 0 for state in STATES}
    temp_counts = {state: 0 for state in STATES}
    current_bin = (_temperature_bin(current["temperature"])
                   if current["temperature"] is not None else None)
    # Mature labels only: k+h <= origin, with explicit publication time <= decision.
    for k in range(origin - horizon + 1):
        prior, realized = rows[k], rows[k + horizon]
        if (prior["state"] not in STATES or realized["state"] not in STATES
                or realized["state_available_at"] > current["decision_at"]):
            continue
        outcome = realized["state"]
        counts[outcome] += 1
        if prior["state"] == current["state"]:
            conditional[outcome] += 1
            if (current_bin is not None and prior["temperature"] is not None
                    and _temperature_bin(prior["temperature"]) == current_bin):
                temp_counts[outcome] += 1
    n = sum(counts.values())
    context_n = sum(conditional.values())
    temp_n = sum(temp_counts.values())
    ready = n >= MINIMUM_PRIOR_PAIRS
    state_ready = ready and context_n >= MINIMUM_STATE_CELL
    temperature_ready = state_ready and current_bin is not None and temp_n >= MINIMUM_TEMPERATURE_CELL
    return {
        "status": "forecast" if ready else "insufficient_mature_pairs",
        "origin_date": current["trade_date"], "origin_decision_at": current["decision_at"].isoformat(),
        "target_date": calendar[origin + horizon], "horizon": horizon,
        "context_state": current["state"], "temperature_bin": current_bin,
        "mature_pairs": n, "context_pairs": context_n, "temperature_cell_pairs": temp_n,
        "model_status": {
            "persistence": "fixed_baseline",
            "unconditional": "available" if ready else "insufficient_mature_pairs",
            "state_transition": "available" if state_ready else
            "insufficient_mature_pairs" if not ready else "insufficient_state_pairs",
            "state_temperature": "available" if temperature_ready else
            "insufficient_mature_pairs" if not ready else
            "insufficient_state_pairs" if not state_ready else
            "origin_temperature_missing" if current_bin is None else "insufficient_temperature_cell_pairs",
        },
        "probabilities": {
            "persistence": _persistence(current["state"]),
            "unconditional": _smoothed(counts) if ready else None,
            "state_transition": _smoothed(conditional) if state_ready else None,
            "state_temperature": _smoothed(temp_counts) if temperature_ready else None,
        },
        **BOUNDARY,
    }


def forecast_at_origin(rows: Sequence[Mapping[str, Any]], calendar: Sequence[str],
                       *, origin_date: str) -> dict[str, Any]:
    """Emit five research probabilities from an observed prefix; future rows are not required."""
    history = validate_input(rows, calendar)
    day = _day(origin_date, "origin_date")
    if history[-1]["trade_date"] != day:
        raise ValueError("origin_must_be_latest_observed_session")
    position = len(history) - 1
    results = [_origin_forecast(history, position, h, calendar) for h in HORIZONS]
    return {"schema": "emotion_horizon_research.v2", "origin_date": day,
            "horizons": results, "authority": BOUNDARY.copy(),
            "support_contract": {"mature_pairs": MINIMUM_PRIOR_PAIRS,
                                 "state_cell_pairs": MINIMUM_STATE_CELL,
                                 "temperature_cell_pairs": MINIMUM_TEMPERATURE_CELL},
            "input_sha256": _identity_hash(history, calendar)}


def _identity_hash(rows: list[dict[str, Any]], calendar: Sequence[str]) -> str:
    payload = {"calendar": list(calendar), "rows": [{
        "trade_date": r["trade_date"], "state": r["state"], "temperature": r["temperature"],
        "observed_at": r["observed_at"].isoformat(),
        "source_snapshot_available_at": r["source_snapshot_available_at"].isoformat(),
        "universe_available_at": r["universe_available_at"].isoformat(),
        "state_available_at": r["state_available_at"].isoformat(),
        "temperature_available_at": r["temperature_available_at"].isoformat()
        if r["temperature_available_at"] else None,
        "temperature_definition_id": r["temperature_definition_id"],
        "decision_at": r["decision_at"].isoformat(),
        "source_snapshot_sha256": r["source_snapshot_sha256"],
        "universe_snapshot_sha256": r["universe_snapshot_sha256"], "identity": r["identity"],
    } for r in rows]}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _scores(samples: list[tuple[dict[str, float], str]]) -> dict[str, Any]:
    if not samples:
        return {"observations": 0, "available": False}
    reliability = {}
    for state in STATES:
        bins = []
        for index in range(5):
            selected = [(probabilities[state], realized) for probabilities, realized in samples
                        if min(int(probabilities[state] * 5), 4) == index]
            bins.append({
                "lower_inclusive": index / 5, "upper_exclusive_except_last": (index + 1) / 5,
                "observations": len(selected),
                "mean_probability": sum(value for value, _ in selected) / len(selected) if selected else None,
                "observed_frequency": sum(realized == state for _, realized in selected) / len(selected)
                if selected else None,
            })
        reliability[state] = bins
    return {
        "observations": len(samples), "available": True,
        "multiclass_brier": sum(sum((p[s] - (s == target)) ** 2 for s in STATES)
                                for p, target in samples) / len(samples),
        "multiclass_log_loss": sum(-math.log(max(p[target], 1e-15))
                                   for p, target in samples) / len(samples),
        "reliability_by_state": reliability,
    }


def evaluate_walk_forward(rows: Sequence[Mapping[str, Any]], calendar: Sequence[str],
                          *, evaluation_start: str, protocol_frozen_at: str,
                          evaluation_as_of: str) -> dict[str, Any]:
    """Score equal-origin h=1..5 baselines with an explicit label-availability cut."""
    history = validate_input(rows, calendar)
    start = _day(evaluation_start, "evaluation_start")
    frozen = _stamp(protocol_frozen_at, "protocol_frozen_at")
    as_of = _stamp(evaluation_as_of, "evaluation_as_of")
    if start not in calendar or start > history[-1]["trade_date"]:
        raise ValueError("evaluation_window_empty")
    by_horizon: dict[str, Any] = {}
    for h in HORIZONS:
        baseline_all: dict[str, list[tuple[dict[str, float], str]]] = {
            name: [] for name in ("persistence", "unconditional")
        }
        primary: dict[str, list[tuple[dict[str, float], str]]] = {
            name: [] for name in ("persistence", "unconditional", "state_transition")
        }
        temperature_common: dict[str, list[tuple[dict[str, float], str]]] = {
            name: [] for name in (*primary, "state_temperature")
        }
        unsupported = {"state_transition": 0, "state_temperature": 0}
        forecasts = []
        skipped = []
        skip_counts: dict[str, int] = {}
        attempted = 0

        def skip(day: str, reason: str) -> None:
            skipped.append({"origin_date": day, "reason": reason})
            skip_counts[reason] = skip_counts.get(reason, 0) + 1

        for origin in range(len(history)):
            row = history[origin]
            if row["trade_date"] < start:
                continue
            attempted += 1
            if row["decision_at"] > as_of:
                skip(row["trade_date"], "origin_not_available_at_evaluation")
                continue
            forecast = _origin_forecast(history, origin, h, calendar)
            if forecast["status"] != "forecast":
                skip(row["trade_date"], forecast["status"])
                continue
            if origin + h >= len(history):
                skip(row["trade_date"], "target_row_unavailable")
                continue
            target = history[origin + h]
            if target["state_available_at"] > as_of:
                skip(row["trade_date"], "target_state_not_available_at_evaluation")
                continue
            if target["decision_at"] > as_of:
                skip(row["trade_date"], "target_snapshot_not_final_at_evaluation")
                continue
            if target["state"] not in STATES:
                skip(row["trade_date"], "target_state_unknown")
                continue
            forecast["target_state"] = target["state"]
            forecast["target_state_available_at"] = target["state_available_at"].isoformat()
            forecasts.append(forecast)
            probabilities = forecast["probabilities"]
            for model in baseline_all:
                baseline_all[model].append((probabilities[model], target["state"]))
            if probabilities["state_transition"] is not None:
                for model in primary:
                    primary[model].append((probabilities[model], target["state"]))
            else:
                unsupported["state_transition"] += 1
            if probabilities["state_temperature"] is not None:
                for model in temperature_common:
                    temperature_common[model].append((probabilities[model], target["state"]))
            else:
                unsupported["state_temperature"] += 1
        by_horizon[str(h)] = {
            "attempted_origins": attempted,
            "pre_score_skipped_origins": skipped,
            "pre_score_skip_counts": skip_counts,
            "scored_origins": len(forecasts),
            "baseline_all_origins": {name: _scores(pairs) for name, pairs in baseline_all.items()},
            "state_common_origins": len(primary["state_transition"]),
            "primary_common_sample": {name: _scores(pairs) for name, pairs in primary.items()},
            "temperature_common_origins": len(temperature_common["state_temperature"]),
            "temperature_common_sample": {name: _scores(pairs) for name, pairs in temperature_common.items()},
            "model_unsupported_origins": unsupported,
            "forecasts": forecasts,
        }
    earliest = next((r["decision_at"] for r in history if r["trade_date"] >= start), None)
    available_history = [row for row in history if row["decision_at"] <= as_of]
    return {
        "schema": "emotion_horizon_walk_forward.v2", "evaluation_start": start,
        "protocol_frozen_at": frozen.isoformat(), "evaluation_as_of": as_of.isoformat(),
        "rows_withheld_after_as_of": len(history) - len(available_history),
        "chronology": "historical_diagnostic_only" if earliest and earliest < frozen
        else "prospective_candidate_not_admitted",
        "horizons": by_horizon, "input_sha256": _identity_hash(available_history, calendar),
        "support_contract": {"mature_pairs": MINIMUM_PRIOR_PAIRS,
                             "state_cell_pairs": MINIMUM_STATE_CELL,
                             "temperature_cell_pairs": MINIMUM_TEMPERATURE_CELL},
        "authority": BOUNDARY.copy(),
    }


__all__ = ["STATES", "HORIZONS", "validate_input", "forecast_at_origin", "evaluate_walk_forward"]
