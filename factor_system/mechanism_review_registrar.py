"""CAS-register a closed source-mechanism batch without granting execution."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path

from . import config
from .source_scout import STATE_PATH, _ordered_candidates, _sha256, _stamp, _write


RECORD_TYPE = "public_source_scout_new_mechanisms_batch"


def apply_closed_mechanism_batch(
    *,
    review_path: Path,
    expected_review_sha256: str,
    expected_state_sha256: str,
    state_path: Path = STATE_PATH,
    reviewed_at: dt.datetime | None = None,
) -> dict:
    """Append exactly three terminal source reviews if both inputs still match."""
    if _sha256(review_path) != expected_review_sha256:
        raise ValueError("mechanism review artifact hash mismatch")
    before_sha = _sha256(state_path)
    if before_sha != expected_state_sha256:
        raise ValueError("mechanism review state hash mismatch")

    review = json.loads(review_path.read_text(encoding="utf-8"))
    if review.get("record_type") != RECORD_TYPE:
        raise ValueError("mechanism review record_type is invalid")
    bindings = review.get("deduplication_bindings") or []
    if not any(
        isinstance(item, dict)
        and item.get("path") == "factor_system/state/public_source_scout.json"
        and item.get("sha256") == expected_state_sha256
        for item in bindings
    ):
        raise ValueError("mechanism review does not bind the expected state")

    scope = review.get("scope") or {}
    summary = review.get("summary") or {}
    mechanisms = review.get("mechanisms")
    required_false = {
        "candidate_creation_authorized",
        "factor_values_read_or_computed",
        "prices_read_or_computed",
        "returns_read_or_computed_for_this_batch",
        "ic_read_or_computed_for_this_batch",
        "backtest_started",
        "catalog_gate_state_registry_daemon_modified",
    }
    if any(scope.get(key) is not False for key in required_false):
        raise ValueError("mechanism review safety boundary is invalid")
    if (
        not isinstance(mechanisms, list)
        or len(mechanisms) != 3
        or summary.get("screened_mechanisms") != 3
        or summary.get("source_ready_mechanisms") != 0
        or summary.get("candidates_created") != 0
        or summary.get("computations_started") != 0
    ):
        raise ValueError("mechanism review terminal batch contract is invalid")
    integrity = review.get("integrity_note") or {}
    if integrity.get("incidental_result_snippet_exposure") is not True:
        raise ValueError("mechanism review outcome exposure is not disclosed")
    if "No result, return, IC, backtest, coefficient, or direction was used" not in str(
        integrity.get("what_happened") or ""
    ):
        raise ValueError("mechanism review outcome-use boundary is invalid")

    state = json.loads(state_path.read_text(encoding="utf-8"))
    if int(state.get("unreviewed_count") or 0) != 0:
        raise ValueError("mechanism review requires zero unresolved paper sources")
    rows = [item for item in state.get("candidates") or [] if isinstance(item, dict)]
    existing = {str(item.get("identity") or "") for item in rows}
    timestamp = _stamp(reviewed_at or dt.datetime.now().astimezone())
    try:
        artifact_ref = str(
            review_path.resolve().relative_to(config.PROJECT_ROOT.resolve())
        )
    except ValueError:
        artifact_ref = str(review_path.resolve())

    additions = []
    for mechanism in mechanisms:
        if not isinstance(mechanism, dict):
            raise ValueError("mechanism review item is invalid")
        source = mechanism.get("primary_source") or {}
        doi = str(source.get("doi") or "").strip().lower()
        title = str(source.get("title") or "").strip()
        identity = f"doi:{doi}"
        status = str(mechanism.get("decision") or "")
        gaps = mechanism.get("blocking_gaps")
        if (
            not doi.startswith("10.")
            or not title
            or not status.startswith("closed_")
            or identity in existing
            or not isinstance(gaps, list)
            or not gaps
            or source.get("licence") != "CC BY 4.0"
        ):
            raise ValueError(f"mechanism review item is not terminal: {identity}")
        official = mechanism.get("official_pit_sources") or []
        additions.append({
            "scout_id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20],
            "identity": identity,
            "doi": doi,
            "title": title,
            "authors": [],
            "year": None,
            "container_title": None,
            "market_scope": "China A-share",
            "url": str(source.get("fixed_url") or f"https://doi.org/{doi}"),
            "source_provider": "manual_primary_source_review",
            "source_providers": ["manual_primary_source_review"],
            "scope_evidence": "source_only_batch_review",
            "source_urls": [
                str(item.get("url") or "") for item in official
                if isinstance(item, dict) and item.get("url")
            ],
            "matched_queries": ["manual_primary_source_review_batch2"],
            "first_seen_at": timestamp,
            "reviewed_at": timestamp,
            "status": status,
            "license_audit_status": "cc_by_4_0_source_reuse_verified",
            "pit_feasibility_status": status,
            "review_reason": str(mechanism.get("economic_mechanism") or ""),
            "candidate_family": str(mechanism.get("mechanism_id") or ""),
            "formula_resolution_status": "source_variable_defined_no_predictive_rule",
            "formula": mechanism.get("fixed_source_definition"),
            "input_contract_gaps": gaps,
            "next_action": "reopen only when every recorded source/PIT/timing gap has new evidence",
            "review_artifact": artifact_ref,
            "review_artifact_sha256": expected_review_sha256,
            "research_only": True,
            "runnable": False,
            "factor_values_computed": False,
            "outcomes_read": True,
            "outcomes_used": False,
            "outcomes_used_in_decision": False,
            "recommendation_eligible": False,
            "production_eligible": False,
        })
        existing.add(identity)

    if len(additions) != 3 or _sha256(state_path) != before_sha:
        raise RuntimeError("source scout state changed during mechanism review")
    rows = _ordered_candidates(rows + additions)
    statuses = [str(item.get("status") or "unreviewed_research_source") for item in rows]
    state.update({
        "candidates": rows,
        "unreviewed_count": statuses.count("unreviewed_research_source"),
        "reviewed_count": len(statuses) - statuses.count("unreviewed_research_source"),
        "closed_count": sum(value.startswith("closed_") for value in statuses),
        "formula_pending_count": statuses.count("formula_pending"),
        "source_contract_pending_count": statuses.count("source_contract_pending"),
        "manual_intake_count": int(state.get("manual_intake_count") or 0) + 3,
        "last_manual_intake_at": timestamp,
        "last_manual_intake_artifact": artifact_ref,
        "last_manual_intake_artifact_sha256": expected_review_sha256,
    })
    _write(state_path, state)
    return state


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("review", type=Path)
    parser.add_argument("--review-sha256", required=True)
    parser.add_argument("--state-sha256", required=True)
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    args = parser.parse_args()
    state = apply_closed_mechanism_batch(
        review_path=args.review,
        expected_review_sha256=args.review_sha256,
        expected_state_sha256=args.state_sha256,
        state_path=args.state,
    )
    print(json.dumps({
        "state_sha256": _sha256(args.state),
        "unreviewed_count": state["unreviewed_count"],
        "closed_count": state["closed_count"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
