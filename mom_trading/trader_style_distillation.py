"""Fail-closed reader for the evidence-graded public-method distillation."""

from __future__ import annotations

import hashlib
import json
import copy
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCUMENT_NAME = "PUBLIC_TRADER_METHOD_DISTILLATION_V3_AMENDMENT_1.json"
BASE_DOCUMENT_NAME = "PUBLIC_TRADER_METHOD_DISTILLATION_V3.json"
DISTILLATION_CONTRACT_SHA256 = (
    "737477fe89812995b0c621e399a5b5a932793d97caafd8f5288959ce056bce31"
)
CORRECTION_DOCUMENT_NAME = "PUBLIC_TRADER_METHOD_DISTILLATION_V3_AMENDMENT_2.json"
CORRECTION_CONTRACT_SHA256 = (
    "6da847b574743c03031ce7f4f1f3474e27bf6c0b48aee784b4142f1eeed1bf39"
)
PLAYBOOK_LIBRARY_NAME = "SMALL_CAPITAL_PLAYBOOK_LIBRARY_V1.json"
_PLAYBOOK_PLAIN_LANGUAGE = {
    "mainline_start_confirmation": {
        "short_label": "主线启动确认",
        "characteristic": "先等板块广度与核心股同步确认，再研究低冲击的小仓位机会；不靠消息单独追涨。",
        "consider_when": "弱修复或情绪上升，且主线板块从启动候选进入确认扩散。",
        "entry_method": "完整同槽证据形成后，仅生成有有效期、整手数量和风险线的人工计划。",
        "avoid_when": "市场风险关闭、板块未确认或已经拥挤、涨停排队不可验证、账户与成本输入不完整。",
        "invalidation": "板块扩散失败、候选不再满足冻结规则、市场转弱、价格不可成交或计划过期。",
    },
    "leader_divergence_restrengthening": {
        "short_label": "核心分歧转强",
        "characteristic": "只研究事先确认的主线核心股，等待分歧后重新站回关键盘中价格，不用后排股替代龙头。",
        "consider_when": "情绪上升、主线仍在扩散，且核心股身份在盘中路径出现前已经冻结。",
        "entry_method": "完成分歧与转强两段确认后，下一根已完成K线开盘仅生成可成交的人工计划。",
        "avoid_when": "高潮拥挤、板块走弱、临时挑龙头、K线尚未完成、封板排队或账户成本不完整。",
        "invalidation": "核心身份、板块广度或转强规则任一失效，市场离开允许状态，或计划超过有效期。",
    },
    "retreat_defense": {
        "short_label": "退潮防守",
        "characteristic": "目标是少亏与保留现金，只处理已有风险，不寻找新的进攻买点。",
        "consider_when": "风险关闭、高潮拥挤、退潮或市场状态无法可靠确认。",
        "entry_method": "不新开仓；只依据真实持仓、当日可卖数量和有效风险线制定人工处置。",
        "avoid_when": "不得把防守规则反向解释为抄底信号，也不得用旧持仓快照计算精确卖出数量。",
        "invalidation": "只有市场、主线板块、账户与正式入场策略重新同槽通过后，才退出防守状态。",
    },
}
_LENS_CLAIMS = {
    "emotion_cycle": ("CGYJ-01",),
    "sector_front_row": ("BJCJ-01",),
    "ladder_extremes": ("KB92-02",),
    "weak_market_and_hot_sector": ("PRO-01", "PRO-02"),
    "predict_confirm_follow": ("NIR-01", "NIR-02"),
    "high_energy_and_tradability": ("LFH-01", "LFH-02"),
    "mode_discipline": ("ZQ-01",),
    "consensus_and_error_exit": ("ASK-01", "ASK-02"),
}


def _correction_overlay(
    project_root: Path,
) -> tuple[Path, dict[str, Any], dict[str, str]]:
    path = project_root / "research" / CORRECTION_DOCUMENT_NAME
    if _sha(path) != CORRECTION_CONTRACT_SHA256:
        raise ValueError("distillation_correction_sha_invalid")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("distillation_correction_invalid")
    for binding in (document.get("bindings") or {}).values():
        _bound_json(
            project_root, binding, "distillation_correction_binding_invalid",
        )
    coverage = document.get("coverage") or {}
    overlay = document.get("claim_disposition_overlay") or {}
    buckets = (
        "direct_anonymous_research_or_QA_seed",
        "narrow_non_alpha_provenance_or_QA_only",
        "downgraded_or_suspended",
        "rejected",
    )
    dispositions: dict[str, str] = {}
    for bucket in buckets:
        values = overlay.get(bucket)
        if not isinstance(values, list):
            raise ValueError("distillation_correction_overlay_invalid")
        for claim_id in values:
            if not isinstance(claim_id, str) or claim_id in dispositions:
                raise ValueError("distillation_correction_overlay_invalid")
            dispositions[claim_id] = bucket
    if (
        len(dispositions) != 28
        or coverage.get("known_claim_count") != 28
        or coverage.get("direct_source_research_or_QA_seed_count") != 3
        or coverage.get("narrow_non_alpha_provenance_or_QA_count") != 4
        or coverage.get("downgraded_or_suspended_count") != 14
        or coverage.get("rejected_count") != 7
        or coverage.get("person_feature_admitted_count") != 0
        or any(value is not False for value in (document.get("authority") or {}).values())
    ):
        raise ValueError("distillation_correction_boundary_invalid")
    return path, document, dispositions


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bound_json(
    project_root: Path, binding: dict[str, Any], error: str,
) -> dict[str, Any]:
    path = project_root / binding["path"]
    if not path.is_file() or _sha(path) != binding["sha256"]:
        raise ValueError(error)
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(error)
    return document


def _distillation_chain(
    project_root: Path,
) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
    path = project_root / "research" / DOCUMENT_NAME
    if _sha(path) != DISTILLATION_CONTRACT_SHA256:
        raise ValueError("distillation_amendment_sha_invalid")
    amendment = json.loads(path.read_text(encoding="utf-8"))
    for binding in amendment["bindings"].values():
        _bound_json(project_root, binding, "amendment_binding_invalid")
    base = _bound_json(
        project_root, amendment["bindings"]["prior_v3"],
        "prior_v3_binding_invalid",
    )
    if base.get("distillation_id") != "public_trader_method_distillation_v3":
        raise ValueError("prior_v3_identity_invalid")
    for binding in base["bindings"].values():
        _bound_json(project_root, binding, "base_binding_invalid")
    prior = _bound_json(
        project_root, base["bindings"]["prior_distillation"],
        "prior_distillation_binding_invalid",
    )
    for binding in prior["bindings"].values():
        _bound_json(project_root, binding, "prior_binding_invalid")
    return path, amendment, base, prior


def _validate_evidence_records(
    base: dict[str, Any], amendment: dict[str, Any],
) -> None:
    records = base["evidence_records"]
    overlay = amendment["evidence_record_use_overlay"]
    if not isinstance(records, list) or not isinstance(overlay, list):
        raise ValueError("evidence_record_contract_invalid")
    record_ids = [row.get("source_id") for row in records if isinstance(row, dict)]
    use_ids = [row.get("source_id") for row in overlay if isinstance(row, dict)]
    if (
        len(record_ids) != len(records)
        or len(set(record_ids)) != len(records)
        or len(use_ids) != len(overlay)
        or len(set(use_ids)) != len(overlay)
        or set(record_ids) != set(use_ids)
    ):
        raise ValueError("evidence_record_identity_invalid")
    uses = {row["source_id"]: row.get("current_use") for row in overlay}
    allowed_grades = set(base["evidence_grade_policy_v3"])
    use_by_prefix = {
        "R1_": "execution_constraint_only",
        "P1_": "mechanism_prior_only",
        "P2_": "mechanism_prior_only",
        "S1_": "anonymous_hypothesis_context_only",
        "S2_": "anonymous_hypothesis_context_only",
        "S3_": "secondary_catalogue_only",
    }
    for row in records:
        source_id = row.get("source_id")
        source_url = row.get("source_url")
        grade = row.get("evidence_grade")
        parsed = urlparse(source_url) if isinstance(source_url, str) else None
        expected_use = next(
            (use for prefix, use in use_by_prefix.items()
             if isinstance(grade, str) and grade.startswith(prefix)),
            None,
        )
        if (
            not isinstance(source_id, str) or not source_id.strip()
            or parsed is None or parsed.scheme != "https" or not parsed.netloc
            or grade not in allowed_grades
            or expected_use is None
            or uses.get(source_id) != expected_use
        ):
            raise ValueError("evidence_record_required_field_invalid")


def _validated_claims(
    *, project_root: Path, amendment: dict[str, Any], prior: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    claim_binding = _bound_json(
        project_root, amendment["bindings"]["claim_evidence_binding"],
        "claim_evidence_binding_invalid",
    )
    audit = _bound_json(
        project_root, amendment["bindings"]["claim_semantic_audit"],
        "claim_semantic_audit_binding_invalid",
    )
    if not (
        prior["bindings"]["claim_semantic_audit"]
        == amendment["bindings"]["claim_semantic_audit"]
        == claim_binding["bindings"]["semantic_audit"]
        and claim_binding["bindings"]["claim_ledger"]
        == audit["bindings"]["claim_ledger"]
    ):
        raise ValueError("claim_chain_identity_invalid")
    ledger = _bound_json(
        project_root, audit["bindings"]["claim_ledger"],
        "claim_ledger_binding_invalid",
    )
    records = claim_binding["records"]
    claims = ledger["claims"]
    assessments = audit["claim_assessments"]
    record_map = {row.get("claim_id"): row for row in records}
    claim_map = {row.get("claim_id"): row for row in claims}
    audit_map = {row.get("claim_id"): row for row in assessments}
    if (
        len(records) != len(record_map) != 0
        or len(claims) != len(claim_map) != 0
        or len(assessments) != len(audit_map) != 0
        or set(record_map) != set(claim_map) or set(record_map) != set(audit_map)
        or len(record_map) != prior["evidence_boundary"]["claim_records"]
    ):
        raise ValueError("claim_record_identity_invalid")
    contract = amendment["claim_evidence_contract"]
    required = contract["required_fields"]
    identity_fields = contract["identity_must_match_claim_ledger"]
    enriched: dict[str, dict[str, Any]] = {}
    for claim_id, row in record_map.items():
        claim = claim_map[claim_id]
        assessment = audit_map[claim_id]
        if (
            not all(field in row for field in required)
            or not isinstance(row.get("claim_id"), str)
            or not isinstance(row.get("public_subject"), str)
            or not isinstance(row.get("source_url"), str)
            or urlparse(row["source_url"]).scheme != "https"
            or not urlparse(row["source_url"]).netloc
            or row.get("evidence_grade") not in {"A1", "A2", "B", "C"}
            or not isinstance(row.get("transport_status"), str)
            or not isinstance(row.get("disposition"), str)
            or row.get("quantitatively_admitted") is not False
            or any(row.get(field) != claim.get(field) for field in identity_fields)
            or row.get("claim_text_verified")
            is not assessment.get("claim_text_verified")
        ):
            raise ValueError("claim_record_required_field_invalid")
        if row["transport_status"] == "frozen":
            frozen_path = row.get("frozen_path")
            content_sha = row.get("content_sha256")
            if (
                row.get("content_hash_bound") is not True
                or not isinstance(frozen_path, str)
                or not isinstance(content_sha, str)
                or len(content_sha) != 64
                or _sha(project_root / frozen_path) != content_sha
            ):
                raise ValueError("claim_content_binding_invalid")
        elif not (
            row["transport_status"] == "failed"
            and row.get("content_hash_bound") is False
            and row.get("frozen_path") is None
            and row.get("content_sha256") is None
            and row.get("disposition") == "not_admissible"
        ):
            raise ValueError("claim_transport_status_invalid")
        if row["disposition"] == "provenance_repair_required":
            use_status = "evidence_insufficient"
        elif row["evidence_grade"] == "C":
            use_status = "secondary_catalogue"
        else:
            use_status = "research_context_only"
        enriched[claim_id] = {
            **claim,
            "claim_text_verified": row["claim_text_verified"],
            "source_disposition": row["disposition"],
            "source_semantic_class": row.get("source_semantic_class"),
            "transport_status": row["transport_status"],
            "content_hash_bound": row.get("content_hash_bound") is True,
            "content_sha256": row.get("content_sha256"),
            "evidence_use_status": use_status,
        }
    return enriched


def _validate_inactive_lenses(
    *, project_root: Path, amendment: dict[str, Any], claims: dict[str, dict[str, Any]],
) -> None:
    catalog = _bound_json(
        project_root, amendment["bindings"]["source_catalog"],
        "source_catalog_binding_invalid",
    )
    catalog_rows = {
        row.get("public_identity"): row for row in catalog["subjects"]
        if isinstance(row, dict)
    }
    disabled = amendment["inactive_public_style_lenses"]
    if {row.get("public_subject") for row in disabled} != {
        "陈小群", "退学炒股", "瑞鹤仙", "章建平",
    }:
        raise ValueError("inactive_lens_subjects_invalid")
    for row in disabled:
        catalog_row = catalog_rows.get(row.get("public_subject"), {})
        if (
            row.get("status") != "disabled"
            or row.get("source_catalog_disposition")
            != catalog_row.get("disposition")
            or not isinstance(row.get("reason"), str) or not row["reason"].strip()
            or not isinstance(row.get("claim_ids"), list)
            or any(claim_id not in claims for claim_id in row["claim_ids"])
        ):
            raise ValueError("inactive_lens_reason_invalid")


def _validate_h2_negative_control(
    *, project_root: Path, amendment: dict[str, Any],
) -> None:
    resolution = _bound_json(
        project_root, amendment["bindings"]["h2_lifecycle_resolution"],
        "h2_resolution_binding_invalid",
    )
    audit = _bound_json(
        project_root, amendment["bindings"]["h2_blind_result_audit"],
        "h2_result_audit_binding_invalid",
    )
    controls = amendment["counterevidence"]
    if len(controls) != 1:
        raise ValueError("h2_negative_control_count_invalid")
    control = controls[0]
    if (
        control.get("status") != resolution.get("status")
        or control.get("facts") != resolution.get("blind_test_facts")
        or control.get("lifecycle_resolution_binding") != "h2_lifecycle_resolution"
        or control.get("independent_audit_binding") != "h2_blind_result_audit"
        or control.get("current_use") != "negative_control_only_no_direct_transfer"
        or audit.get("valid") is not True
        or audit.get("errors") != []
        or not all(audit.get("checks", {}).values())
        or audit.get("score_change_allowed") is not False
        or audit.get("position_policy_change_allowed") is not False
        or audit.get("production") is not False
        or "does not directly reject" not in control.get("non_transfer_boundary", "")
    ):
        raise ValueError("h2_negative_control_invalid")


def load_trader_style_distillation(
    *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Return compact research truth without score, position or alert authority."""
    try:
        path, amendment, document, prior = _distillation_chain(project_root)
        _validate_evidence_records(document, amendment)
        claims = _validated_claims(
            project_root=project_root, amendment=amendment, prior=prior,
        )
        _validate_inactive_lenses(
            project_root=project_root, amendment=amendment, claims=claims,
        )
        _validate_h2_negative_control(
            project_root=project_root, amendment=amendment,
        )
        correction_path, correction, correction_dispositions = (
            _correction_overlay(project_root)
        )
        families = prior["anonymous_feature_families"]
        family_names = {item.get("feature_family") for item in families}
        evidence = prior["evidence_boundary"]
        private = prior["private_fund_distillation"]
        mechanisms = document["anonymous_mechanisms"]
        quantitative = document["quantitative_admission"]
        if len(families) != 6 or len(family_names) != 6:
            raise ValueError("feature_family_contract_invalid")
        if (
            evidence.get("public_short_term_subjects") != 12
            or evidence.get("claim_records") != 28
            or evidence.get("quantitatively_admitted_claims") != 0
            or private.get("institutions") != 9
            or private.get("unique_manager_subjects") != 6
            or private.get("quantitatively_admitted") != 0
            or len(mechanisms) != 7
            or len(document.get("evidence_records") or []) < 18
            or len(document.get("counterevidence") or []) < 6
            or quantitative.get("returns_or_labels_read") is not False
            or any(
                value != 0 for key, value in quantitative.items()
                if key != "returns_or_labels_read"
            )
            or any(value is not False for value in document["authority"].values())
            or any(value is not False for value in amendment["authority"].values())
            or amendment["coverage_boundary"].get("internet_search_complete")
            is not False
            or amendment["coverage_boundary"].get(
                "all_public_material_recovered"
            ) is not False
        ):
            raise ValueError("research_boundary_invalid")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "distillation_unavailable_or_invalid",
            "feature_family_count": 0,
            "quantitatively_admitted": 0,
            "applied_to_score": False,
            "applied_to_positions": False,
            "applied_to_alerts": False,
            "production": False,
            "reason": f"公开方法匿名蒸馏合同不可用：{exc}",
        }
    return {
        "status": prior["status"],
        "distillation_id": document["distillation_id"],
        "as_of": str(amendment["generated_at"])[:10],
        "document_sha256": _sha(path),
        "base_document_sha256": _sha(
            project_root / "research" / BASE_DOCUMENT_NAME
        ),
        "document_version": 3,
        "amendment_version": 2,
        "correction_document_sha256": _sha(correction_path),
        "direct_anonymous_research_or_QA_seed_count": correction[
            "coverage"
        ]["direct_source_research_or_QA_seed_count"],
        "narrow_non_alpha_provenance_or_QA_count": correction[
            "coverage"
        ]["narrow_non_alpha_provenance_or_QA_count"],
        "downgraded_or_suspended_count": correction[
            "coverage"
        ]["downgraded_or_suspended_count"],
        "rejected_count": correction["coverage"]["rejected_count"],
        "person_feature_admitted_count": correction[
            "coverage"
        ]["person_feature_admitted_count"],
        "feature_family_count": len(families),
        "feature_families": [{
            "id": item["feature_family"],
            "role": item["role"],
            "state": item["current_state"],
        } for item in families],
        "public_subject_count": evidence["public_short_term_subjects"],
        "claim_records": evidence["claim_records"],
        "claim_text_verified": evidence["claim_text_verified"],
        "private_institution_count": private["institutions"],
        "private_manager_subject_count": private["unique_manager_subjects"],
        "anonymous_mechanism_count": len(mechanisms),
        "evidence_record_count": len(document["evidence_records"]),
        "counterevidence_count": (
            len(document["counterevidence"]) + len(amendment["counterevidence"])
        ),
        "claim_evidence_record_count": len(claims),
        "claim_text_verified": sum(
            row["claim_text_verified"] is True for row in claims.values()
        ),
        "inactive_style_lens_count": len(
            amendment["inactive_public_style_lenses"]
        ),
        "internet_search_complete": False,
        "local_negative_control_count": len(amendment["counterevidence"]),
        "quantitatively_admitted": 0,
        "applied_to_score": False,
        "applied_to_positions": False,
        "applied_to_alerts": False,
        "production": False,
        "reason": "V3 A2已将28项公开叙事逐条分为3项匿名研究或QA种子、4项窄QA、14项降级或暂停、7项拒绝；人物身份不进入评分、仓位、提醒或生产准入。",
    }


def _published_small_capital_playbook_library(
    project_root: Path,
) -> dict[str, Any] | None:
    path = (
        project_root / "mom_trading" / "static"
        / "strategy_evidence_status.json"
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        library = payload["current_research_program"][
            "published_playbook_library"
        ]
        playbooks = library["playbooks"]
        authority = library["authority"]
        if (
            library.get("status")
            != "frozen_research_only_zero_quantitative_admission"
            or library.get("mechanism_count") != 7
            or library.get("playbook_count") != 3
            or {row.get("id") for row in playbooks}
            != set(_PLAYBOOK_PLAIN_LANGUAGE)
            or any(
                not all(
                    isinstance(row.get(key), str) and row[key].strip()
                    for key in (
                        "label", "short_label", "characteristic",
                        "consider_when", "entry_method", "avoid_when",
                        "invalidation",
                    )
                )
                for row in playbooks
            )
            or library.get("quantitatively_admitted") != 0
            or any(
                library.get(key) is not False for key in (
                    "applied_to_route", "applied_to_score",
                    "applied_to_positions", "applied_to_alerts", "production",
                )
            )
            or any(value is not False for value in authority.values())
        ):
            raise ValueError("published_playbook_boundary_invalid")
        return copy.deepcopy(library)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def load_small_capital_playbook_library(
    *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Return the frozen research library without granting runtime authority."""
    path = project_root / "research" / PLAYBOOK_LIBRARY_NAME
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        for binding in document["bindings"].values():
            target = project_root / binding["path"]
            if not target.is_file() or _sha(target) != binding["sha256"]:
                raise ValueError("playbook_binding_invalid")
        mechanisms = document["mechanism_contracts"]
        playbooks = document["playbooks"]
        admission = document["quantitative_admission"]
        if (
            document.get("status")
            != "frozen_research_only_zero_quantitative_admission"
            or len(mechanisms) != 7
            or len(playbooks) != 3
            or {row.get("id") for row in playbooks}
            != set(_PLAYBOOK_PLAIN_LANGUAGE)
            or admission.get("returns_or_labels_read") is not False
            or any(
                value != 0 for key, value in admission.items()
                if key != "returns_or_labels_read"
            )
            or any(value is not False for value in document["authority"].values())
        ):
            raise ValueError("playbook_research_boundary_invalid")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if not path.is_file():
            published = _published_small_capital_playbook_library(project_root)
            if published is not None:
                return published
        return {
            "status": "small_capital_playbook_library_unavailable_or_invalid",
            "mechanism_count": 0, "playbook_count": 0,
            "quantitatively_admitted": 0,
            "applied_to_route": False, "applied_to_score": False,
            "applied_to_positions": False, "applied_to_alerts": False,
            "production": False, "reason": str(exc),
        }
    return {
        "status": document["status"],
        "library_id": document["library_id"],
        "as_of": str(document["created_at"])[:10],
        "document_sha256": _sha(path),
        "mechanism_count": len(mechanisms),
        "playbook_count": len(playbooks),
        "playbooks": [
            {
                "id": row["id"],
                "label": _PLAYBOOK_PLAIN_LANGUAGE[row["id"]]["short_label"],
                "kind": row["kind"],
                "applicable_market_states": list(
                    row.get("applicable_market_states") or []
                ),
                "applicable_sector_stages": list(
                    row.get("applicable_sector_stages") or []
                ),
                **_PLAYBOOK_PLAIN_LANGUAGE[row["id"]],
            }
            for row in playbooks
        ],
        "quantitatively_admitted": 0,
        "applied_to_route": False,
        "applied_to_score": False,
        "applied_to_positions": False,
        "applied_to_alerts": False,
        "production": False,
        "reason": "七个匿名机制已形成三套小资金固定战法合同；尚待整条路由的成本后、样本外与前向验证。",
    }


def _verified_claims(*, project_root: Path) -> dict[str, dict[str, Any]]:
    _, amendment, _, prior = _distillation_chain(project_root)
    claims = _validated_claims(
        project_root=project_root, amendment=amendment, prior=prior,
    )
    return {
        claim_id: row for claim_id, row in claims.items()
        if row["claim_text_verified"] is True
    }


_STATE_PLAIN = {
    "risk_closed": "风险关闭",
    "retreat": "退潮",
    "unknown": "状态未知",
    "weak_repair": "弱修复",
    "sentiment_rising": "情绪上升",
    "crowded_climax": "高潮拥挤",
}
_EVIDENCE_GRADE_PLAIN = {
    "A1": "原始或官方材料",
    "A2": "已核验原始平台内容",
    "B": "可信媒体或访谈转述",
    "C": "二手资料，仅作研究线索",
}


def _sector_reason(sectors: list[dict[str, Any]]) -> str:
    summaries = []
    for row in sectors[:3]:
        name = str(row.get("name") or "未命名板块")
        coverage = row.get("advance_coverage_pct")
        change = row.get("change_pct")
        metrics = []
        if isinstance(coverage, (int, float)):
            metrics.append(f"上涨覆盖{coverage:.1f}%")
        if isinstance(change, (int, float)):
            metrics.append(f"涨幅{change:.2f}%")
        summaries.append(f"{name}（{'、'.join(metrics)}）" if metrics else name)
    return "、".join(summaries)


def _plain_lens_payload(
    lens_id: str, stance: str, route: dict[str, Any],
) -> dict[str, str]:
    state = str(route.get("market_state") or "unknown")
    state_label = _STATE_PLAIN.get(state, state)
    sector = route.get("sector_confirmation") or {}
    sectors = [
        row for row in (sector.get("confirmed_sectors") or [])
        if isinstance(row, dict)
    ]
    sector_ready = sector.get("ready") is True and bool(sectors)
    sector_summary = _sector_reason(sectors)
    playbook_ids = [
        str(row.get("id")) for row in
        ((route.get("research_budget_hypothesis") or {}).get(
            "allowed_playbooks"
        ) or [])
        if isinstance(row, dict) and row.get("id")
    ]
    payloads = {
        "emotion_cycle": {
            "status": "市场状态已识别" if state != "unknown" else "暂不判断",
            "conclusion": (
                "当前先控制风险，不增加新仓。"
                if state in {"risk_closed", "retreat", "unknown"} else
                "当前一致性过强，不追涨后排股票。"
                if state == "crowded_climax" else
                "当前可选择性研究已进入固定战法的机会。"
                if state == "sentiment_rising" else
                "当前只观察修复能否得到板块和成交确认。"
            ),
            "reason": f"系统识别的市场阶段为“{state_label}”；该视角只解释风险节奏，不单独选股。",
            "counterpoint": "市场阶段判断可能变化，不能把情绪好直接等同于个股会涨。",
            "applies_when": "决定今日总风险预算和交易节奏时",
            "watch_next": "上涨覆盖、涨跌停平衡、连板延续和盘中状态是否反转",
        },
        "sector_front_row": {
            "status": "板块数据已确认" if sector_ready else "暂不判断",
            "conclusion": (
                "只研究已确认主线中的前排，不追脱离板块的孤立上涨。"
                if sector_ready else
                "当前缺少同一决策时点的完整板块快照，暂不判断谁是前排。"
            ),
            "reason": (
                f"最近一次已核验快照确认：{sector_summary}。"
                if sector_ready else
                "板块强弱、上涨覆盖和成分股同步性尚未在同一时点完整取得。"
            ),
            "counterpoint": "板块强不代表个股可买；还要核对个股归属、前排地位、可成交性和完整交易计划。",
            "applies_when": "市场出现主线启动、扩散或强势延续时",
            "watch_next": "板块上涨覆盖、成交额放大、龙头与后排同步、炸板率和集中度",
        },
        "ladder_extremes": {
            "status": "关键数据未齐",
            "conclusion": "暂不根据高位股或低位股叙事调整候选。",
            "reason": "尚无已冻结的高低位梯队、排队强度和真实可成交数据。",
            "counterpoint": "仅看涨停高度容易忽略一字板无法成交和中位股掉队风险。",
            "applies_when": "比较高位龙头、低位补涨和中位跟风时",
            "watch_next": "梯队完整度、晋级率、开板回封、成交可得性和中位股亏钱效应",
        },
        "weak_market_and_hot_sector": {
            "status": "防守" if stance == "defensive" else "板块已确认" if sector_ready else "等待确认",
            "conclusion": "弱市减少交易；非弱市也只研究获得盘面确认的持续热点。",
            "reason": f"当前市场阶段为“{state_label}”{f'，已确认板块为{sector_summary}' if sector_ready else '，板块持续性尚待确认'}。",
            "counterpoint": "单日强势可能只是脉冲，热点必须经过后续覆盖率和成交持续性复核。",
            "applies_when": "市场偏弱但局部板块仍有持续赚钱效应时",
            "watch_next": "指数承压程度、热点持续天数、板块内部赚钱覆盖和资金回流",
        },
        "predict_confirm_follow": {
            "status": "研究路由已确认" if route.get("status") == "research_route_ready" else "等待确认",
            "conclusion": "盘前预判不产生动作，只有盘面达到预先写好的确认条件才进入研究。",
            "reason": "系统把预判、盘中确认和后续跟随拆开，避免事后挑选看起来正确的战法。",
            "counterpoint": "确认后仍可能失败，必须同时保留失效条件和不交易选项。",
            "applies_when": "盘前计划转为盘中候选时",
            "watch_next": "预判证据是否被盘面支持、确认发生时间和确认后的成交质量",
        },
        "high_energy_and_tradability": {
            "status": "可选择性研究" if state == "sentiment_rising" and sector_ready else "保持防守" if state in {"risk_closed", "retreat", "unknown"} else "等待确认",
            "conclusion": "只有市场活跃、板块同步且股票真实可成交时，才研究少量机会。",
            "reason": f"当前市场为“{state_label}”，板块确认={'已完成' if sector_ready else '未完成'}。",
            "counterpoint": "一字涨停、封单拥挤或流动性不足的股票即使强势，也不等于可执行。",
            "applies_when": "高活跃阶段筛选真实可成交机会时",
            "watch_next": "成交额、换手、封单结构、开板次数和预计滑点",
        },
        "mode_discipline": {
            "status": "固定战法内" if playbook_ids else "当前无适用战法",
            "conclusion": (
                f"当前只允许研究固定战法：{'、'.join(playbook_ids)}。"
                if playbook_ids else
                "当前没有符合条件的固定战法，不为交易临时编理由。"
            ),
            "reason": "战法在看到结果前固定，系统不允许盘后从大量模式中挑一个解释。",
            "counterpoint": "进入模式只代表符合研究条件，不代表策略已经通过收益验证。",
            "applies_when": "决定一个候选是否属于既定交易模式时",
            "watch_next": "入场条件、失效条件、持有周期和退出规则是否完整一致",
        },
        "consensus_and_error_exit": {
            "status": "必须先写退出条件",
            "conclusion": "市场合力只作背景证据；入场前必须写清判断错误时如何退出。",
            "reason": "共识可能继续强化，也可能快速瓦解，因此不能只记录支持理由。",
            "counterpoint": "止损线本身不能保证成交，A股还受T+1、跌停和流动性约束。",
            "applies_when": "形成任何人工买入计划之前",
            "watch_next": "反向证据、关键失效价、可卖数量、跌停风险和实际成交偏差",
        },
    }
    return payloads[lens_id]


def build_trader_style_lenses(
    route: dict[str, Any], *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Explain one frozen route through evidence-graded public-method lenses."""
    try:
        distillation = load_trader_style_distillation(project_root=project_root)
        if distillation.get("status") != "scientific_distillation_contract_ready_zero_quantitative_admission":
            raise ValueError("distillation_invalid")
        claims = _verified_claims(project_root=project_root)
        correction_path, _, correction_dispositions = _correction_overlay(
            project_root
        )
        if any(claim_id not in claims for ids in _LENS_CLAIMS.values() for claim_id in ids):
            raise ValueError("lens_claim_not_semantically_verified")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "research_style_lenses_unavailable", "lenses": [],
            "reason": str(exc), "authority": _lens_authority(),
        }

    state = str(route.get("market_state") or "unknown")
    sector_ready = ((route.get("sector_confirmation") or {}).get("ready") is True)
    playbooks = [
        row.get("id") for row in
        ((route.get("research_budget_hypothesis") or {}).get("allowed_playbooks") or [])
        if isinstance(row, dict)
    ]
    defensive = state in {"risk_closed", "retreat", "unknown"}
    crowded = state == "crowded_climax"
    rising = state == "sentiment_rising"
    views = {
        "emotion_cycle": (
            "defensive" if defensive else "avoid_chasing" if crowded else "selective_participation" if rising else "wait_for_confirmation",
            "情绪决定风险预算；当前不新开仓。" if defensive else "高潮拥挤，不追后排。" if crowded else "情绪上升，仅研究已准入战法。" if rising else "弱修复，只等待板块确认。",
        ),
        "sector_front_row": (
            "sector_confirmed_research" if sector_ready else "data_incomplete",
            "仅观察同槽已确认板块的前排候选。" if sector_ready else "缺同槽板块确认，不输出前排偏好。",
        ),
        "ladder_extremes": (
            "data_incomplete",
            "尚无冻结的高低位梯队与排队可成交数据，不把中位股叙事用于评分。",
        ),
        "weak_market_and_hot_sector": (
            "defensive" if defensive else "sector_confirmed_research" if sector_ready else "wait_for_confirmation",
            "弱市减少活动。" if defensive else "只在持续热点获同槽确认后研究。" if sector_ready else "等待持续热点确认。",
        ),
        "predict_confirm_follow": (
            "confirmed_route" if route.get("status") == "research_route_ready" else "wait_for_confirmation",
            "预判不能直接交易；只有固定条件确认后的路由可进入研究。",
        ),
        "high_energy_and_tradability": (
            "selective_participation" if rising and sector_ready else "defensive" if defensive else "wait_for_confirmation",
            "高能期与板块同时确认，可研究少量机会；一字板仍按不可成交。" if rising and sector_ready else "非高能确认期，保持防守或等待。",
        ),
        "mode_discipline": (
            "inside_mode" if playbooks else "outside_mode_veto",
            f"只允许当前路由中的战法：{', '.join(playbooks)}。" if playbooks else "当前没有准入战法，模式外交易否决。",
        ),
        "consensus_and_error_exit": (
            "requires_predeclared_invalidation",
            "市场合力只作环境证据；任何候选必须在入场前写明失效条件与可执行退出。",
        ),
    }
    lenses = []
    for lens_id, claim_ids in _LENS_CLAIMS.items():
        rows = [claims[claim_id] for claim_id in claim_ids]
        stance, view = views[lens_id]
        claim_evidence = [{
            "claim_id": row["claim_id"],
            "source_url": row["source_url"],
            "evidence_grade": row["evidence_grade"],
            "claim_text_verified": row["claim_text_verified"],
            "source_disposition": row["source_disposition"],
            "transport_status": row["transport_status"],
            "content_hash_bound": row["content_hash_bound"],
            "content_sha256": row["content_sha256"],
            "evidence_use_status": row["evidence_use_status"],
            "correction_disposition": correction_dispositions[row["claim_id"]],
        } for row in rows]
        correction_values = [
            correction_dispositions[claim_id] for claim_id in claim_ids
        ]
        use_status = (
            "evidence_insufficient"
            if any(value in {"downgraded_or_suspended", "rejected"}
                   for value in correction_values)
            else "evidence_insufficient"
            if any(row["evidence_use_status"] == "evidence_insufficient"
                   for row in rows)
            else "secondary_catalogue"
            if any(row["evidence_use_status"] == "secondary_catalogue"
                   for row in rows)
            else "research_context_only"
        )
        lenses.append({
            "lens_id": lens_id,
            "provenance_subject": " / ".join(dict.fromkeys(row["public_subject"] for row in rows)),
            "claim_ids": list(claim_ids),
            "evidence_grades": list(dict.fromkeys(row["evidence_grade"] for row in rows)),
            "source_dispositions": list(dict.fromkeys(
                row["source_disposition"] for row in rows
            )),
            "evidence_use_status": use_status,
            "claim_evidence": claim_evidence,
            "claim_correction_dispositions": correction_values,
            "stance": stance,
            "view": view,
            "plain_language": _plain_lens_payload(
                lens_id, stance, route,
            ),
            "evidence_explanations": [
                _EVIDENCE_GRADE_PLAIN.get(
                    grade, f"内部来源等级 {grade}"
                ) for grade in dict.fromkeys(
                    row["evidence_grade"] for row in rows
                )
            ],
            "evidence_scope_note": "来源可信度不代表策略胜率，也不产生交易权限。",
            "anonymous_rules": [row["anonymous_translation"] for row in rows],
            "named_identity_signal_used": False,
            "recommendation": False,
        })
    return {
        "status": "research_style_lenses_ready",
        "market_state": state,
        "distillation_document_sha256": _sha(
            project_root / "research" / DOCUMENT_NAME
        ),
        "correction_document_sha256": _sha(correction_path),
        "implementation_sha256": _sha(Path(__file__)),
        "lens_count": len(lenses),
        "lenses": lenses,
        "context": copy.deepcopy(
            route.get("public_trader_style_context") or {}
        ),
        "reason": "同一冻结路由的证据纠错后研究解读；资料覆盖尚未完成，降级、暂停或拒绝项不产生支持结论，人物身份不产生推荐。",
        "authority": _lens_authority(),
    }


def _lens_authority() -> dict[str, bool]:
    return {
        "changes_score": False, "changes_trade_count": False,
        "changes_position_or_quantity": False, "generates_alert": False,
        "production": False, "automatic_trading": False,
    }
