"""Build outcome-blind factor candidate drafts from reviewed public sources.

This module is deliberately pure: it does not read files, returns, registries, or
prices and it never grants runnable/production authority.  A complete draft is
only ready for a later source-catalog review.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_OUTCOME_FLAGS = (
    "outcomes_read",
    "future_outcomes_read",
    "returns_read",
    "prices_read",
    "factor_values_computed",
    "ic_computed",
    "backtest_run",
)


def _text(value: object) -> str:
    return str(value or "").strip()


def _normalized(value: object) -> str:
    return " ".join(_text(value).casefold().split())


def _gap(requirement: str, detail: str, *, pit: bool = False) -> dict[str, Any]:
    return {
        "requirement": requirement,
        "status": "missing_or_unverified",
        "detail": detail,
        "blocks_catalog_review": True,
        "blocks_runnable": True,
        "pit_related": pit,
    }


def _source_binding(record: Mapping[str, Any]) -> dict[str, str]:
    raw = record.get("source_binding")
    raw = raw if isinstance(raw, Mapping) else {}
    identity = _text(raw.get("identity") or record.get("identity"))
    url = _text(
        raw.get("url")
        or record.get("primary_source_url")
        or record.get("source_url")
        or record.get("url")
    )
    immutable_ref = _text(
        raw.get("immutable_ref")
        or record.get("reviewed_source_commit_sha")
        or record.get("current_commit_sha")
        or record.get("doi")
        or record.get("source_sha256")
    )
    return {"identity": identity, "url": url, "immutable_ref": immutable_ref}


def _formula(record: Mapping[str, Any]) -> dict[str, Any]:
    raw = record.get("original_formula")
    raw = raw if isinstance(raw, Mapping) else {}
    expression = _text(raw.get("expression"))
    locator = _text(raw.get("source_locator"))
    exact = raw.get("verified_exact") is True
    digest = (
        hashlib.sha256(_normalized(expression).encode()).hexdigest()
        if expression and exact and locator else None
    )
    return {
        "expression": expression,
        "source_locator": locator,
        "verified_exact": exact,
        "sha256": digest,
    }


def _license(record: Mapping[str, Any]) -> dict[str, Any]:
    raw = record.get("license")
    raw = raw if isinstance(raw, Mapping) else {}
    return {
        "identifier": _text(raw.get("identifier")),
        "status": _text(raw.get("status")),
        "reuse_allowed": raw.get("reuse_allowed") is True,
    }


def _mechanism(record: Mapping[str, Any]) -> dict[str, str]:
    raw = record.get("a_share_mechanism")
    raw = raw if isinstance(raw, Mapping) else {}
    return {
        "statement": _text(raw.get("statement")),
        "market_scope": _text(raw.get("market_scope")),
    }


def _required_fields(record: Mapping[str, Any]) -> list[dict[str, str]]:
    rows = record.get("required_fields")
    if not isinstance(rows, list):
        return []
    return [
        {
            "name": _text(row.get("name")),
            "source": _text(row.get("source")),
            "available_at_rule": _text(row.get("available_at_rule")),
        }
        for row in rows
        if isinstance(row, Mapping)
    ]


def _pit(record: Mapping[str, Any]) -> dict[str, str]:
    raw = record.get("pit")
    raw = raw if isinstance(raw, Mapping) else {}
    return {
        "status": _text(raw.get("status")),
        "input_identity": _text(raw.get("input_identity")),
        "chronology_rule": _text(raw.get("chronology_rule")),
    }


def _preregistration(record: Mapping[str, Any]) -> dict[str, str]:
    raw = record.get("preregistration")
    raw = raw if isinstance(raw, Mapping) else {}
    return {
        "status": _text(raw.get("status")),
        "artifact_path": _text(raw.get("artifact_path")),
        "artifact_sha256": _text(raw.get("artifact_sha256")),
    }


def build_candidate_draft(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return one deterministic, fail-closed draft and its evidence gaps."""
    if not isinstance(record, Mapping):
        raise TypeError("public source record must be a mapping")

    source = _source_binding(record)
    formula = _formula(record)
    license_record = _license(record)
    mechanism = _mechanism(record)
    fields = _required_fields(record)
    pit = _pit(record)
    preregistration = _preregistration(record)
    gaps: list[dict[str, Any]] = []

    review_status = _text(record.get("status"))
    if (
        not _text(record.get("reviewed_at"))
        or record.get("research_only") is not True
        or review_status.startswith("closed_")
        or review_status.startswith("unreviewed")
    ):
        gaps.append(_gap(
            "reviewed_public_source",
            "record must be reviewed, research-only, and not terminally closed",
        ))
    if not all(source.values()):
        gaps.append(_gap(
            "fixed_source",
            "source identity, stable URL, and immutable DOI/commit/hash are required",
        ))
    if not (formula["expression"] and formula["source_locator"] and formula["verified_exact"]):
        gaps.append(_gap(
            "original_formula",
            "exact primary-source expression and page/section/path locator are required",
        ))
    if not (
        license_record["identifier"]
        and license_record["status"] == "verified"
        and license_record["reuse_allowed"]
    ):
        gaps.append(_gap(
            "license",
            "verified licence identifier and explicit reuse permission are required",
        ))
    market_scope = _normalized(mechanism["market_scope"])
    if not mechanism["statement"] or not (
        "a-share" in market_scope or "a share" in market_scope or "a股" in market_scope
    ):
        gaps.append(_gap(
            "a_share_mechanism",
            "an explicit A-share economic mechanism and market scope are required",
        ))
    if not fields or any(not all(row.values()) for row in fields):
        gaps.append(_gap(
            "required_fields",
            "each required input needs name, source, and point-in-time availability rule",
            pit=True,
        ))
    if not (
        pit["status"] == "verified"
        and pit["input_identity"]
        and pit["chronology_rule"]
    ):
        gaps.append(_gap(
            "pit",
            "verified input identity and chronology rule are required",
            pit=True,
        ))
    if not (
        preregistration["status"] == "frozen"
        and preregistration["artifact_path"]
        and _HEX64.fullmatch(preregistration["artifact_sha256"])
    ):
        gaps.append(_gap(
            "preregistration",
            "frozen preregistration path and sha256 are required",
        ))
    if any(record.get(key) is True for key in _FORBIDDEN_OUTCOME_FLAGS):
        gaps.append(_gap(
            "outcome_blind_boundary",
            "records exposed to prices, returns, IC, factor values, or backtests cannot seed a draft",
        ))

    source_key = _normalized(source["identity"]) or None
    identity_payload = "\x1f".join(filter(None, (
        source_key,
        formula["sha256"],
        _normalized(record.get("scout_id")),
        _normalized(record.get("title")),
    ))) or "incomplete"
    ready = not gaps
    return {
        "schema_version": 1,
        "record_type": "outcome_blind_factor_candidate_draft",
        "draft_id": "draft_" + hashlib.sha256(identity_payload.encode()).hexdigest()[:20],
        "source_record_id": _text(record.get("scout_id")) or None,
        "title": _text(record.get("title")) or None,
        "source_binding": source,
        "original_formula": formula,
        "license": license_record,
        "a_share_mechanism": mechanism,
        "required_fields": fields,
        "pit": pit,
        "preregistration": preregistration,
        "status": "draft_ready_for_catalog_review" if ready else "draft_incomplete",
        "catalog_review_ready": ready,
        "runnable": False,
        "production_allowed": False,
        "recommendation_allowed": False,
        "automatic_trading_allowed": False,
        "outcomes_read_by_bridge": False,
        "gap_table": gaps,
        "pit_gap_table": [row for row in gaps if row["pit_related"]],
        "missing_requirements": [row["requirement"] for row in gaps],
        "deduplication": {
            "source_key": source_key,
            "formula_sha256": formula["sha256"],
        },
    }


def build_candidate_drafts(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Build one draft per unique source and formula, retaining duplicate audit rows."""
    drafts: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    seen_sources: dict[str, str] = {}
    seen_formulas: dict[str, str] = {}
    for record in records:
        draft = build_candidate_draft(record)
        keys = draft["deduplication"]
        duplicate_of = (
            seen_sources.get(keys["source_key"])
            if keys["source_key"] else None
        ) or (
            seen_formulas.get(keys["formula_sha256"])
            if keys["formula_sha256"] else None
        )
        if duplicate_of:
            duplicates.append({
                "draft_id": draft["draft_id"],
                "duplicate_of": duplicate_of,
                "same_source": bool(
                    keys["source_key"]
                    and keys["source_key"] in seen_sources
                ),
                "same_formula": bool(
                    keys["formula_sha256"]
                    and keys["formula_sha256"] in seen_formulas
                ),
            })
            continue
        drafts.append(draft)
        if keys["source_key"]:
            seen_sources[keys["source_key"]] = draft["draft_id"]
        if keys["formula_sha256"]:
            seen_formulas[keys["formula_sha256"]] = draft["draft_id"]

    return {
        "schema_version": 1,
        "record_type": "outcome_blind_factor_candidate_draft_batch",
        "drafts": drafts,
        "duplicates": duplicates,
        "summary": {
            "input_records": len(drafts) + len(duplicates),
            "unique_drafts": len(drafts),
            "duplicate_records": len(duplicates),
            "catalog_review_ready": sum(
                draft["catalog_review_ready"] for draft in drafts
            ),
            "runnable": 0,
        },
        "authority": {
            "catalog_write_allowed": False,
            "registry_write_allowed": False,
            "mining_allowed": False,
            "backtest_allowed": False,
            "production_allowed": False,
        },
    }


__all__ = ["build_candidate_draft", "build_candidate_drafts"]
