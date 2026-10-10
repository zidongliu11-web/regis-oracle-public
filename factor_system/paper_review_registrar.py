"""CAS-register source-only paper reviews with disclosed outcome exposure."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from . import config
from .source_scout import STATE_PATH, _ordered_candidates, _sha256, _stamp, _write


RECORD_TYPE = (
    "public_source_paper_review_with_incidental_abstract_"
    "outcome_exposure_not_used"
)
_ALLOWED_STATUSES = {"formula_pending", "source_contract_pending"}


def apply_paper_review(
    *,
    review_path: Path,
    state_path: Path = STATE_PATH,
    reviewed_at: dt.datetime | None = None,
) -> dict:
    """Register fail-closed paper reviews without granting computation authority."""
    before_sha = _sha256(state_path)
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if review.get("record_type") != RECORD_TYPE:
        raise ValueError("paper review record_type is invalid")
    if review.get("source_state_sha256_observed") != before_sha:
        raise ValueError("paper review state hash mismatch")

    scope = review.get("scope")
    if not isinstance(scope, dict):
        raise ValueError("paper review scope is missing")
    required_false = {
        "factor_values_computed",
        "price_values_read",
        "outcomes_used_in_decision",
        "candidate_ranks_computed",
        "ic_computed",
        "backtest_run",
        "source_catalog_modified",
        "production_state_modified",
    }
    if any(scope.get(key) is not False for key in required_false):
        raise ValueError("paper review safety boundary is invalid")
    if scope.get("outcomes_read") is not True or not str(
        scope.get("outcome_exposure") or ""
    ).strip():
        raise ValueError("paper review outcome exposure is not disclosed")

    reviews = review.get("reviews")
    if not isinstance(reviews, list) or not reviews:
        raise ValueError("paper reviews are empty")
    scout_ids = [str(item.get("scout_id") or "") for item in reviews]
    if "" in scout_ids or len(set(scout_ids)) != len(scout_ids):
        raise ValueError("paper review scout ids are invalid or duplicated")

    state = json.loads(state_path.read_text(encoding="utf-8"))
    rows = [item for item in state.get("candidates") or [] if isinstance(item, dict)]
    by_scout_id = {str(item.get("scout_id") or ""): item for item in rows}
    timestamp = _stamp(reviewed_at or dt.datetime.now().astimezone())
    try:
        artifact_ref = str(
            review_path.resolve().relative_to(config.PROJECT_ROOT.resolve())
        )
    except ValueError:
        artifact_ref = str(review_path.resolve())
    artifact_sha = _sha256(review_path)

    for item in reviews:
        if not isinstance(item, dict):
            raise ValueError("paper review is not an object")
        scout_id = str(item.get("scout_id") or "")
        row = by_scout_id.get(scout_id)
        if row is None:
            raise ValueError(f"paper review source missing: {scout_id}")
        if row.get("status") != "unreviewed_research_source":
            raise ValueError(f"paper review source already reviewed: {scout_id}")
        if str(row.get("doi") or "") != str(item.get("doi") or ""):
            raise ValueError(f"paper review DOI mismatch: {scout_id}")

        decision = item.get("decision")
        formula = item.get("formula_review")
        data = item.get("data_and_pit_review")
        licence = item.get("licence_review")
        if not all(isinstance(value, dict) for value in (decision, formula, data, licence)):
            raise ValueError(f"paper review evidence is incomplete: {scout_id}")
        status = str(decision.get("status") or "")
        if not (status.startswith("closed_") or status in _ALLOWED_STATUSES):
            raise ValueError(f"paper review status is not fail-closed: {scout_id}")
        if any(decision.get(key) is not False for key in (
            "register_factor_candidate",
            "runnable",
            "recommendation_eligible",
            "production_eligible",
        )):
            raise ValueError(f"paper review decision is executable: {scout_id}")

        row.update({
            "status": status,
            "reviewed_at": timestamp,
            "primary_source_url": item.get("primary_source_url"),
            "review_artifact": artifact_ref,
            "review_artifact_sha256": artifact_sha,
            "license_audit_status": licence.get("status"),
            "formula_resolution_status": formula.get("status"),
            "pit_feasibility_status": data.get("status"),
            "review_reason": item.get("mechanism_classification"),
            "candidate_family": item.get("candidate_family"),
            "input_contract_gaps": data.get("blocking_gaps"),
            "next_action": decision.get("reopen_only_if"),
            "research_only": True,
            "runnable": False,
            "factor_values_computed": False,
            "outcomes_read": True,
            "outcomes_used_in_decision": False,
            "outcome_exposure": scope["outcome_exposure"],
            "recommendation_eligible": False,
            "production_eligible": False,
        })

    if _sha256(state_path) != before_sha:
        raise RuntimeError("source scout state changed during paper review")
    rows = _ordered_candidates(rows)
    statuses = [str(item.get("status") or "") for item in rows]
    state.update({
        "candidates": rows,
        "unreviewed_count": statuses.count("unreviewed_research_source"),
        "reviewed_count": len(statuses) - statuses.count("unreviewed_research_source"),
        "closed_count": sum(value.startswith("closed_") for value in statuses),
        "formula_pending_count": statuses.count("formula_pending"),
        "source_contract_pending_count": statuses.count("source_contract_pending"),
        "last_triage_at": timestamp,
        "last_triage_artifact": artifact_ref,
        "last_triage_artifact_sha256": artifact_sha,
    })
    _write(state_path, state)
    return state


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("review", type=Path)
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    args = parser.parse_args()
    state = apply_paper_review(review_path=args.review, state_path=args.state)
    print(json.dumps({
        "state_sha256": _sha256(args.state),
        "unreviewed_count": state["unreviewed_count"],
        "closed_count": state["closed_count"],
        "formula_pending_count": state["formula_pending_count"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
