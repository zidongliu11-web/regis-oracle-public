"""Atomically register a source-only GitHub review with disclosed outcome exposure."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path

from . import config
from .source_scout import STATE_PATH, _sha256, _stamp, _write


RECORD_TYPE = (
    "public_source_github_repository_review_with_incidental_readme_"
    "outcome_exposure_not_used"
)
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def apply_repository_review(
    *,
    review_path: Path,
    state_path: Path = STATE_PATH,
    reviewed_at: dt.datetime | None = None,
) -> dict:
    """CAS-register terminal repository reviews without granting execution."""
    before_sha = _sha256(state_path)
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if review.get("record_type") != RECORD_TYPE:
        raise ValueError("repository review record_type is invalid")
    if review.get("source_state_sha256_observed") != before_sha:
        raise ValueError("repository review state hash mismatch")

    scope = review.get("scope")
    if not isinstance(scope, dict):
        raise ValueError("repository review scope is missing")
    required_false = {
        "factor_values_computed",
        "price_values_read",
        "outcomes_used_in_decision",
        "ic_computed",
        "backtest_run",
        "result_artifacts_read",
        "source_scout_state_modified",
        "source_catalog_modified",
        "production_state_modified",
    }
    if any(scope.get(key) is not False for key in required_false):
        raise ValueError("repository review safety boundary is invalid")
    if scope.get("outcomes_read") is not True or not str(
        scope.get("outcome_exposure") or ""
    ).strip():
        raise ValueError("repository review outcome exposure is not disclosed")

    reviews = review.get("reviews")
    if not isinstance(reviews, list) or not reviews:
        raise ValueError("repository reviews are empty")
    scout_ids = [str(item.get("scout_id") or "") for item in reviews]
    if "" in scout_ids or len(set(scout_ids)) != len(scout_ids):
        raise ValueError("repository review scout ids are invalid or duplicated")

    state = json.loads(state_path.read_text(encoding="utf-8"))
    rows = [
        item for item in state.get("repository_discoveries") or []
        if isinstance(item, dict)
    ]
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
            raise ValueError("repository review is not an object")
        scout_id = str(item.get("scout_id") or "")
        row = by_scout_id.get(scout_id)
        if row is None:
            raise ValueError(f"repository review source missing: {scout_id}")
        if row.get("status") != "unreviewed_repository_source":
            raise ValueError(f"repository review source already reviewed: {scout_id}")
        if str(row.get("repository") or "") != str(item.get("repository") or ""):
            raise ValueError(f"repository review identity mismatch: {scout_id}")
        commit = str(item.get("fixed_commit") or "")
        if not _COMMIT_RE.fullmatch(commit):
            raise ValueError(f"repository review commit is invalid: {scout_id}")

        decision = item.get("decision")
        formula = item.get("formula_review")
        data = item.get("data_and_pit_review")
        licence = item.get("licence_review")
        if not all(isinstance(value, dict) for value in (decision, formula, data, licence)):
            raise ValueError(f"repository review evidence is incomplete: {scout_id}")
        status = str(decision.get("status") or "")
        if not status.startswith("closed_"):
            raise ValueError(f"repository review status is not closed: {scout_id}")
        if any(decision.get(key) is not False for key in (
            "register_factor_candidate", "runnable", "recommendation_eligible",
            "production_eligible",
        )):
            raise ValueError(f"repository review decision is executable: {scout_id}")

        row.update({
            "status": status,
            "reviewed_at": timestamp,
            "reviewed_source_commit_sha": commit,
            "review_artifact": artifact_ref,
            "review_artifact_sha256": artifact_sha,
            "license_audit_status": (
                "code_license_verified_upstream_data_terms_unresolved"
                if licence.get("upstream_data_reuse_status") == "unresolved_fail_closed"
                else "license_unresolved_fail_closed"
            ),
            "formula_audit_status": formula.get("status"),
            "data_provenance_status": data.get("status"),
            "pit_feasibility_status": data.get("status"),
            "review_reason": item.get("repository_classification"),
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
        raise RuntimeError("source scout state changed during repository review")
    statuses = [str(item.get("status") or "") for item in rows]
    state.update({
        "repository_discoveries": rows,
        "repository_discovery_unreviewed_count": statuses.count(
            "unreviewed_repository_source"
        ),
        "repository_discovery_reviewed_count": sum(
            value.startswith(("reviewed_", "closed_")) for value in statuses
        ),
        "repository_discovery_closed_count": sum(
            value.startswith("closed_") for value in statuses
        ),
        "repository_last_triage_at": timestamp,
        "repository_last_triage_artifact": artifact_ref,
        "repository_last_triage_artifact_sha256": artifact_sha,
    })
    _write(state_path, state)
    return state


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("review", type=Path)
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    args = parser.parse_args()
    state = apply_repository_review(review_path=args.review, state_path=args.state)
    print(json.dumps({
        "state_sha256": _sha256(args.state),
        "repository_discovery_unreviewed_count": state[
            "repository_discovery_unreviewed_count"
        ],
        "repository_discovery_closed_count": state[
            "repository_discovery_closed_count"
        ],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
