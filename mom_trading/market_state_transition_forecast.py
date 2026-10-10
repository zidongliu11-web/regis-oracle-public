"""Expanding, no-lookahead next-session market-state probabilities."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from .market_state_daily import PROJECT_ROOT, _atomic_publish, _canonical_bytes
from .research_artifact_paths import research_artifact_path


PROTOCOL_PATH = PROJECT_ROOT / "research/MARKET_STATE_NEXT_TRANSITION_PROTOCOL_V1.json"
PROTOCOL_SHA256 = "da3a65ce51e72c8c87cda61feae8e2f88b9b6df483a73cd67bee5d78b6a98ce3"
REPORT_PATH = PROJECT_ROOT / "reports/market_state_next_transition_v1.json"
CORRECTION_AMENDMENT_1_PATH = PROJECT_ROOT / (
    "research/MARKET_STATE_BENCHMARK_EXPOSURE_OOS_PROTOCOL_V1_AMENDMENT_1.json"
)
CORRECTION_AMENDMENT_1_SHA256 = (
    "3eb7c6cbcf15290ffbd9431529b9773e78ab1683d1c545e94239421d2cc47ca7"
)
CORRECTION_AMENDMENT_2_PATH = PROJECT_ROOT / (
    "research/MARKET_STATE_BENCHMARK_EXPOSURE_OOS_PROTOCOL_V1_AMENDMENT_2.json"
)
CORRECTION_AMENDMENT_2_SHA256 = (
    "522a01578008bd8909835f0e086b8b0206b87a9c1f80a29e0854664549234842"
)
STATES = ("RISK_OFF", "STABILIZING", "RISK_ON", "OVERHEATED")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _project_path(raw: object, project_root: Path) -> Path:
    return research_artifact_path(raw, project_root=project_root)


def verify_transition_protocol(
    *, project_root: Path = PROJECT_ROOT, protocol_path: Path = PROTOCOL_PATH,
    expected_sha256: str | None = PROTOCOL_SHA256,
) -> dict[str, Any]:
    errors: list[str] = []
    protocol: dict[str, Any] = {}
    protocol_sha: str | None = None
    try:
        protocol_path = _project_path(protocol_path, project_root)
        protocol_sha = _sha256(protocol_path)
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        if expected_sha256 and protocol_sha != expected_sha256:
            errors.append("protocol_sha256_mismatch")
        if protocol.get("status") != "frozen_before_first_transition_evaluation":
            errors.append("protocol_status_invalid")
        for name, binding in (protocol.get("bindings") or {}).items():
            target = _project_path(binding.get("path"), project_root)
            if not target.is_file() or _sha256(target) != binding.get("sha256"):
                errors.append(f"binding_invalid:{name}")
        contract = protocol.get("forecast_contract") or {}
        if tuple(contract.get("states") or ()) != STATES:
            errors.append("state_taxonomy_invalid")
        if contract.get("parameter_search") is not False:
            errors.append("parameter_search_boundary_invalid")
        if contract.get("return_or_profit_label_used") is not False:
            errors.append("label_boundary_invalid")
        authority = protocol.get("authority") or {}
        if authority.get("research_only") is not True or any(
            value is not False for key, value in authority.items()
            if key != "research_only"
        ):
            errors.append("authority_invalid")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        errors.append(f"protocol_unreadable:{exc}")
    return {
        "valid": not errors, "errors": list(dict.fromkeys(errors)),
        "protocol": protocol, "protocol_sha256": protocol_sha,
    }


def verify_transition_chronology_correction(
    *, project_root: Path = PROJECT_ROOT,
    amendment_1_path: Path = CORRECTION_AMENDMENT_1_PATH,
    amendment_2_path: Path = CORRECTION_AMENDMENT_2_PATH,
) -> dict[str, Any]:
    """Verify the append-only correction that downgrades the old report."""
    errors: list[str] = []
    documents: dict[str, dict[str, Any]] = {}
    expected = (
        ("amendment_1", amendment_1_path, CORRECTION_AMENDMENT_1_SHA256),
        ("amendment_2", amendment_2_path, CORRECTION_AMENDMENT_2_SHA256),
    )
    for name, path, expected_sha in expected:
        try:
            if _sha256(path) != expected_sha:
                errors.append(f"{name}_sha256_mismatch")
            documents[name] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            errors.append(f"{name}_unreadable:{exc}")
            documents[name] = {}

    first = documents["amendment_1"]
    second = documents["amendment_2"]
    try:
        predecessor = first.get("predecessor") or {}
        predecessor_path = _project_path(predecessor.get("path"), project_root)
        if (
            not predecessor_path.is_file()
            or _sha256(predecessor_path) != predecessor.get("sha256")
        ):
            errors.append("amendment_1_predecessor_invalid")
        policy = first.get("effective_classification_policy") or {}
        if (
            policy.get("strict_oos_classification_evidence") is not False
            or policy.get("research_or_production_admission") is not False
            or policy.get("used_by_exposure_mapping") is not False
            or policy.get("used_by_return_gate") is not False
        ):
            errors.append("chronology_downgrade_policy_invalid")
        predecessor = second.get("predecessor") or {}
        predecessor_path = _project_path(predecessor.get("path"), project_root)
        if (
            not predecessor_path.is_file()
            or _sha256(predecessor_path) != predecessor.get("sha256")
            or predecessor.get("sha256") != CORRECTION_AMENDMENT_1_SHA256
        ):
            errors.append("amendment_2_predecessor_invalid")
        forward = second.get("effective_evaluation_policy") or {}
        if (
            forward.get("strict_forward_start") != "2026-08-31"
            or forward.get("no_backfill") is not True
            or forward.get("historical_csi_return_columns_may_be_read") is not False
            or forward.get("historical_return_metrics_may_be_computed") is not False
        ):
            errors.append("strict_forward_policy_invalid")
        for document in (first, second):
            authority = document.get("authority") or {}
            if authority.get("research_only") is not True or any(
                value is not False for key, value in authority.items()
                if key != "research_only"
            ):
                errors.append("correction_authority_invalid")
    except (OSError, ValueError, TypeError) as exc:
        errors.append(f"correction_binding_invalid:{exc}")
    return {
        "valid": not errors,
        "errors": list(dict.fromkeys(errors)),
        "amendment_1": first,
        "amendment_2": second,
    }


def _partition(target_date: str, protocol: dict[str, Any]) -> str | None:
    for name in ("development", "validation", "test", "contamination_diagnostic_only"):
        start, end = (protocol.get("time_partition") or {}).get(name) or (None, None)
        if start and end and start <= target_date <= end:
            return name
    return None


def _probabilities(counts: dict[str, int], alpha: float) -> dict[str, float]:
    denominator = sum(counts.values()) + alpha * len(STATES)
    return {
        state: (counts[state] + alpha) / denominator for state in STATES
    }


def _score_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"observations": 0, "available": False}

    def metrics(key: str) -> dict[str, float]:
        brier = 0.0
        log_loss = 0.0
        correct = 0
        realized_probability = 0.0
        for row in rows:
            probabilities = row[key]
            target = row["target_state"]
            brier += sum(
                (probabilities[state] - (1.0 if state == target else 0.0)) ** 2
                for state in STATES
            )
            realized = max(float(probabilities[target]), 1e-15)
            log_loss -= math.log(realized)
            realized_probability += probabilities[target]
            top_state = max(STATES, key=lambda state: (probabilities[state], -STATES.index(state)))
            correct += int(top_state == target)
        count = len(rows)
        return {
            "multiclass_brier": round(brier / count, 9),
            "multiclass_log_loss": round(log_loss / count, 9),
            "top_state_accuracy": round(correct / count, 9),
            "mean_realized_state_probability": round(realized_probability / count, 9),
        }

    return {
        "observations": len(rows), "available": True,
        "transition_model": metrics("probabilities"),
        "unconditional_baseline": metrics("unconditional_probabilities"),
        "persistence_baseline": metrics("persistence_probabilities"),
    }


def build_transition_forecast(
    *, project_root: Path = PROJECT_ROOT, protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str | None = PROTOCOL_SHA256,
) -> dict[str, Any]:
    checked = verify_transition_protocol(
        project_root=project_root, protocol_path=protocol_path,
        expected_sha256=expected_protocol_sha256,
    )
    if checked.get("valid") is not True:
        return {
            "status": "identity_invalid", "errors": checked.get("errors") or [],
            "research_display_admitted": False, "authority": _authority(),
        }
    protocol = checked["protocol"]
    epoch_binding = protocol["bindings"]["market_state_feature_epoch_v2"]
    epoch_path = _project_path(epoch_binding["path"], project_root)
    epoch = json.loads(epoch_path.read_text(encoding="utf-8"))
    rows = epoch.get("rows") if isinstance(epoch.get("rows"), list) else []
    alpha = float(protocol["forecast_contract"]["dirichlet_alpha_per_next_state"])
    minimum = int(protocol["forecast_contract"]["minimum_prior_transitions_for_context"])
    transition_counts = {
        state: {target: 0 for target in STATES} for state in STATES
    }
    global_counts = {state: 0 for state in STATES}
    scored: dict[str, list[dict[str, Any]]] = {
        name: [] for name in (
            "development", "validation", "test", "contamination_diagnostic_only"
        )
    }
    excluded_unknown = 0
    for current, following in zip(rows, rows[1:]):
        current_state = str((current.get("features") or {}).get("confirmed_state") or "UNKNOWN")
        target_state = str((following.get("features") or {}).get("confirmed_state") or "UNKNOWN")
        target_date = str(following.get("trade_date") or "")
        if current_state not in STATES or target_state not in STATES:
            excluded_unknown += 1
            continue
        current_counts = transition_counts[current_state]
        prior_count = sum(current_counts.values())
        if prior_count >= minimum:
            partition = _partition(target_date, protocol)
            if partition in scored:
                scored[partition].append({
                    "decision_date": current.get("trade_date"),
                    "target_date": target_date,
                    "context_state": current_state,
                    "target_state": target_state,
                    "prior_context_transition_count": prior_count,
                    "probabilities": _probabilities(current_counts, alpha),
                    "unconditional_probabilities": _probabilities(global_counts, alpha),
                    "persistence_probabilities": {
                        state: 1.0 if state == current_state else 0.0 for state in STATES
                    },
                })
        current_counts[target_state] += 1
        global_counts[target_state] += 1

    evaluation = {name: _score_rows(values) for name, values in scored.items()}
    test = evaluation["test"]
    transition_test = test.get("transition_model") or {}
    unconditional_test = test.get("unconditional_baseline") or {}
    persistence_test = test.get("persistence_baseline") or {}
    research_admitted = bool(
        test.get("available") is True
        and transition_test.get("multiclass_brier", math.inf)
        < unconditional_test.get("multiclass_brier", -math.inf)
        and transition_test.get("multiclass_brier", math.inf)
        < persistence_test.get("multiclass_brier", -math.inf)
        and transition_test.get("multiclass_log_loss", math.inf)
        <= unconditional_test.get("multiclass_log_loss", -math.inf)
    )

    latest = rows[-1] if rows else {}
    latest_state = str((latest.get("features") or {}).get("confirmed_state") or "UNKNOWN")
    context_counts = transition_counts.get(latest_state)
    context_total = sum(context_counts.values()) if context_counts else 0
    ready = bool(context_counts and context_total >= minimum)
    probabilities = _probabilities(context_counts, alpha) if ready else None
    entropy = (
        -sum(value * math.log(value) for value in probabilities.values())
        if probabilities else None
    )
    contradictions: list[str] = []
    if not ready:
        contradictions.append("insufficient_prior_transitions_for_current_state")
    if not research_admitted:
        contradictions.append("historical_test_did_not_beat_all_frozen_baselines")
    chronology_invalid = bool(
        checked.get("protocol_sha256") == PROTOCOL_SHA256
        and project_root.resolve() == PROJECT_ROOT.resolve()
    )
    if chronology_invalid:
        correction = verify_transition_chronology_correction(project_root=project_root)
        if correction.get("valid") is not True:
            return {
                "status": "identity_invalid",
                "errors": correction.get("errors") or [],
                "research_display_admitted": False,
                "authority": _authority(),
            }
        research_admitted = False
        contradictions.append(
            "historical_evaluation_predated_declared_protocol_freeze"
        )

    report = {
        "schema": "market_state_next_transition_forecast.v1",
        "status": (
            "research_state_forecast_validated" if ready and research_admitted
            else "historical_state_transition_diagnostic_chronology_invalid"
            if ready and chronology_invalid
            else "research_state_forecast_diagnostic_only" if ready
            else "research_state_forecast_incomplete"
        ),
        "protocol": {
            "path": str(_project_path(protocol_path, project_root).relative_to(project_root.resolve())),
            "sha256": checked["protocol_sha256"],
        },
        "source_epoch": {
            "path": epoch_binding["path"], "sha256": epoch_binding["sha256"],
            "row_count": len(rows), "scope": epoch.get("scope"),
        },
        "as_of": latest.get("trade_date"),
        "current_state": latest_state,
        "probabilities": probabilities,
        "probability_semantics": (
            "empirical next-completed-session state probability; not expected "
            "return, win rate or profit probability"
        ),
        "training_transition_count": sum(global_counts.values()),
        "context_transition_count": context_total,
        "context_transition_counts": context_counts,
        "excluded_unknown_pairs": excluded_unknown,
        "entropy": round(entropy, 9) if entropy is not None else None,
        "evidence": [
            f"current completed-session state={latest_state}",
            f"prior transitions from current state={context_total}",
            "expanding counts update only after each target session is scored",
        ],
        "contradictions": contradictions,
        "evaluation_against_baselines": evaluation,
        "research_display_admitted": research_admitted,
        "historical_diagnostic_only": chronology_invalid,
        "strict_oos_classification_evidence": False if chronology_invalid else None,
        "strict_forward_start": "2026-08-31" if chronology_invalid else None,
        "applied_to_live_positions": False,
        "returns_or_profit_labels_read": False,
        "authority": _authority(),
    }
    report["report_id"] = hashlib.sha256(_canonical_bytes(report)).hexdigest()
    return report


def verify_transition_report(
    path: Path = REPORT_PATH, *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        resolved = _project_path(path, project_root)
        report = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"report_unreadable:{exc}"]}
    identity = dict(report)
    report_id = str(identity.pop("report_id", ""))
    if hashlib.sha256(_canonical_bytes(identity)).hexdigest() != report_id:
        errors.append("report_id_invalid")
    protocol_ref = report.get("protocol") or {}
    try:
        bound_protocol = _project_path(protocol_ref.get("path"), project_root)
    except (OSError, ValueError):
        bound_protocol = project_root / "__invalid_protocol__"
        errors.append("protocol_path_invalid")
    checked = verify_transition_protocol(
        project_root=project_root, protocol_path=bound_protocol,
        expected_sha256=str(protocol_ref.get("sha256") or ""),
    )
    if checked.get("valid") is not True:
        errors.append("protocol_identity_invalid")
    source = report.get("source_epoch") or {}
    try:
        source_path = _project_path(source.get("path"), project_root)
        if not source_path.is_file() or _sha256(source_path) != source.get("sha256"):
            errors.append("source_epoch_identity_invalid")
    except (OSError, ValueError):
        errors.append("source_epoch_identity_invalid")
    probabilities = report.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != set(STATES) or any(
        not isinstance(probabilities.get(state), (int, float))
        or not math.isfinite(float(probabilities[state]))
        or not 0 <= float(probabilities[state]) <= 1
        for state in STATES
    ) or abs(sum(float(probabilities[state]) for state in STATES) - 1.0) > 1e-9:
        errors.append("probabilities_invalid")
    if report.get("returns_or_profit_labels_read") is not False:
        errors.append("label_boundary_invalid")
    if report.get("applied_to_live_positions") is not False:
        errors.append("live_position_authority_invalid")
    if report.get("authority") != _authority():
        errors.append("authority_invalid")
    return {
        "valid": not errors, "errors": list(dict.fromkeys(errors)),
        "report": report, "report_id": report_id,
        "sha256": _sha256(resolved),
    }


def load_transition_forecast(
    path: Path = REPORT_PATH, *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    checked = verify_transition_report(path, project_root=project_root)
    if checked.get("valid") is not True:
        return {
            "status": "unavailable_or_invalid", "probabilities": None,
            "research_display_admitted": False,
            "reason": ",".join(checked.get("errors") or []),
            "authority": _authority(),
        }
    correction = verify_transition_chronology_correction(project_root=project_root)
    if correction.get("valid") is not True:
        return {
            "status": "unavailable_or_invalid", "probabilities": None,
            "research_display_admitted": False,
            "reason": ",".join(correction.get("errors") or []),
            "authority": _authority(),
        }
    report = checked["report"]
    return {
        "status": "historical_state_transition_diagnostic_chronology_invalid",
        "as_of": report["as_of"],
        "current_state": report["current_state"],
        "probabilities": report["probabilities"],
        "probability_semantics": report["probability_semantics"],
        "training_transition_count": report["training_transition_count"],
        "context_transition_count": report["context_transition_count"],
        "context_transition_counts": report["context_transition_counts"],
        "entropy": report["entropy"], "evidence": report["evidence"],
        "contradictions": report["contradictions"],
        "evaluation_against_baselines": report["evaluation_against_baselines"],
        "research_display_admitted": False,
        "historical_diagnostic_only": True,
        "strict_oos_classification_evidence": False,
        "strict_forward_start": (
            correction["amendment_2"]["effective_evaluation_policy"]
            ["strict_forward_start"]
        ),
        "strict_forward_minimum_completed_sessions": (
            correction["amendment_2"]["effective_evaluation_policy"]
            ["minimum_completed_forward_sessions"]
        ),
        "strict_forward_minimum_calendar_months": (
            correction["amendment_2"]["effective_evaluation_policy"]
            ["minimum_calendar_months"]
        ),
        "exposure_return_validation_status": "blocked_no_strict_oos_input",
        "exposure_return_metrics_available": False,
        "chronology_limitation": (
            "historical transition report predates its declared protocol freeze; "
            "probabilities and scores are diagnostic only"
        ),
        "report_id": checked["report_id"], "report_sha256": checked["sha256"],
        "applied_to_live_positions": False, "authority": _authority(),
    }


def publish_transition_forecast(
    *, report_path: Path = REPORT_PATH,
) -> dict[str, Any]:
    report_path = _project_path(report_path, PROJECT_ROOT)
    if report_path.exists():
        loaded = load_transition_forecast(report_path)
        return {
            "path": str(report_path), "created": False,
            "sha256": _sha256(report_path), "status": loaded.get("status"),
            "research_display_admitted": False,
        }
    report = build_transition_forecast()
    created = _atomic_publish(report_path, _canonical_bytes(report))
    return {
        "path": str(report_path), "created": created,
        "sha256": _sha256(report_path), "status": report.get("status"),
        "research_display_admitted": report.get("research_display_admitted"),
    }


def _authority() -> dict[str, bool]:
    return {
        "research_only": True, "changes_unified_score": False,
        "changes_live_trade_count": False,
        "changes_live_position_or_quantity": False,
        "generates_buy_or_sell_alert": False, "production_strategy": False,
        "automatic_trading": False, "stable_profit_claim": False,
    }


def main() -> int:
    result = publish_transition_forecast()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PROTOCOL_PATH", "PROTOCOL_SHA256", "REPORT_PATH", "STATES",
    "build_transition_forecast", "load_transition_forecast",
    "publish_transition_forecast", "verify_transition_protocol",
    "verify_transition_report", "verify_transition_chronology_correction",
]
