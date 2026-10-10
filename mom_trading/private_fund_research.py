"""Fail-closed reader for the source-only private-fund research scope."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCOPE_NAME = "PRIVATE_FUND_PUBLIC_METHOD_SCOPE_V1.json"
AMENDMENT_NAME = "PRIVATE_FUND_PUBLIC_METHOD_SCOPE_V1_AMENDMENT_1.json"


def load_private_fund_scope(*, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    path = project_root / "research" / SCOPE_NAME
    try:
        scope = json.loads(path.read_text(encoding="utf-8"))
        amendment_path = project_root / "research" / AMENDMENT_NAME
        amendment = json.loads(amendment_path.read_text(encoding="utf-8"))
        institutions = scope["institutions"]
        summary = {**scope["summary"], **amendment["correction"]}
        allowed = set(scope["source_policy"]["allowed_domains"])
        amended = amendment["amends"]
        if (
            amended.get("path") != f"research/{SCOPE_NAME}"
            or amended.get("sha256") != hashlib.sha256(path.read_bytes()).hexdigest()
        ):
            raise ValueError("amendment_binding_invalid")
        if len(institutions) != summary.get("institution_count"):
            raise ValueError("institution_count_mismatch")
        if len({item.get("institution_id") for item in institutions}) != len(institutions):
            raise ValueError("duplicate_institution_id")
        if any(urlparse(item["source"]["url"]).netloc not in allowed for item in institutions):
            raise ValueError("source_domain_not_allowed")
        manager_subjects = sorted({
            subject
            for item in institutions
            for subject in (item.get("manager_subjects") or [])
        })
        if (
            set(manager_subjects) != set(amendment["finding"]["unique_manager_subjects"])
            or len(manager_subjects) != summary.get("manager_subject_count")
        ):
            raise ValueError("manager_subject_count_mismatch")
        if any(item.get("quantitatively_admitted") is not False for item in institutions):
            raise ValueError("quantitative_admission_invalid")
        if (
            summary.get("quantitatively_admitted") != 0
            or summary.get("audited_performance_records") != 0
            or summary.get("production_factors") != 0
            or any(value is not False for value in scope["permissions"].values())
        ):
            raise ValueError("decision_boundary_invalid")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "scope_unavailable_or_invalid", "institution_count": 0,
            "manager_subject_count": 0, "mechanism_candidate_count": 0,
            "quantitatively_admitted": 0, "audited_performance_records": 0,
            "applied_to_score": False, "applied_to_positions": False,
            "applied_to_alerts": False, "production": False,
            "reason": f"私募公开方法研究范围不可用：{exc}",
        }
    return {
        "status": scope["status"], "scope_id": scope["scope_id"],
        "as_of": scope["as_of"], "label": scope["label"],
        "catalog_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "amendment_id": amendment["amendment_id"],
        "amendment_sha256": hashlib.sha256(amendment_path.read_bytes()).hexdigest(),
        **summary,
        "transport_frozen_sources": scope["source_policy"]["transport_frozen_sources"],
        "applied_to_score": False, "applied_to_positions": False,
        "applied_to_alerts": False, "production": False,
        "reason": "仅完成公开方法来源范围；来源正文尚未冻结，全部机制候选均未读取收益、未准入。",
        "next_action": scope["next_action"],
    }
