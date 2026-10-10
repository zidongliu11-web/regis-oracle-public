"""Read-only dashboard enrichment for frozen market-state research artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .market_state_daily import (
    DATA_ROOT,
    PROJECT_ROOT,
    _inside_project,
    _sha256,
    latest_research_state as _base_research_state,
)


TRADER_STYLE_AUDIT_NAME = "TRADER_STYLE_CLAIM_AND_HYPOTHESIS_GAP_AUDIT_V1.json"
TRADER_STYLE_SEMANTIC_AUDIT_NAME = "TRADER_STYLE_CLAIM_SOURCE_SEMANTIC_AUDIT_V1.json"
SECTOR_DIFFUSION_AUDIT_NAME = "ANONYMOUS_SECTOR_DIFFUSION_H2_INPUT_EPOCH_AUDIT_V1.json"
SECTOR_DIFFUSION_RESOLUTION_NAME = "ANONYMOUS_SECTOR_DIFFUSION_H2_LIFECYCLE_RESOLUTION_V1.json"
ATTENTION_CROWDING_INPUT_NAME = "ANONYMOUS_ATTENTION_CROWDING_H3_INPUT_FEASIBILITY_V1.json"
ATTENTION_CROWDING_INPUT_AMENDMENT_NAME = (
    "ANONYMOUS_ATTENTION_CROWDING_H3_INPUT_FEASIBILITY_AMENDMENT_2.json"
)
ATTENTION_CROWDING_PROTOCOL_NAME = "ANONYMOUS_ATTENTION_CROWDING_H3_FORWARD_OBSERVATION_PROTOCOL_V2.json"
ATTENTION_CROWDING_PROTOCOL_AMENDMENT_NAME = (
    "ANONYMOUS_ATTENTION_CROWDING_H3_FORWARD_OBSERVATION_PROTOCOL_AMENDMENT_2.json"
)
ATTENTION_CROWDING_DEPLOYMENT_NAME = "ANONYMOUS_ATTENTION_CROWDING_H3_FORWARD_DEPLOYMENT_AUDIT_V18.json"
MARKET_STATE_V2_AUDIT_NAME = "MARKET_STATE_FEATURE_EPOCH_V2_INPUT_AUDIT.json"
MARKET_STATE_V2_AUDIT_SHA256 = (
    "91784732da55ef2012f00b4a6bee1a177ba1cebfd3b8998c6f079aa45ed2f632"
)


def _verified_bindings(document: dict[str, Any], *, project_root: Path) -> bool:
    for binding in (document.get("bindings") or {}).values():
        target = _inside_project(str(binding.get("path") or ""), project_root=project_root)
        if target is None or not target.is_file() or _sha256(target) != binding.get("sha256"):
            return False
    return True


def trader_style_research_program(*, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Return compact research truth; never a score, position or signal input."""
    path = project_root / "research" / TRADER_STYLE_AUDIT_NAME
    try:
        audit = json.loads(path.read_text(encoding="utf-8"))
        hypotheses = audit.get("active_hypotheses") or []
        permissions = audit.get("permissions") or {}
        if not _verified_bindings(audit, project_root=project_root):
            raise ValueError("binding_invalid")
        if len(hypotheses) != 3 or len({item.get("exclusive_role") for item in hypotheses}) != 3:
            raise ValueError("hypothesis_roles_invalid")
        if any(permissions.get(key) is not False for key in (
            "unified_score_change", "position_policy_change", "production",
        )):
            raise ValueError("decision_boundary_invalid")
        semantic = json.loads((
            project_root / "research" / TRADER_STYLE_SEMANTIC_AUDIT_NAME
        ).read_text(encoding="utf-8"))
        if not _verified_bindings(semantic, project_root=project_root):
            raise ValueError("semantic_audit_binding_invalid")
        semantic_summary = semantic.get("summary") or {}
        if (
            semantic_summary.get("source_urls") != 24
            or semantic_summary.get("claim_records") != 28
            or semantic_summary.get("quantitatively_admitted_claims") != 0
        ):
            raise ValueError("semantic_audit_summary_invalid")
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return {
            "status": "audit_unavailable_or_invalid", "hypothesis_count": 0,
            "applied_to_score": False, "applied_to_positions": False,
            "production": False, "reason": f"匿名风格研究冻结合同不可用：{exc}",
        }
    catalog = audit.get("catalog_audit") or {}
    sector_diffusion = sector_diffusion_research_program(project_root=project_root)
    attention_crowding = attention_crowding_research_program(project_root=project_root)
    current_states = {
        "anonymous_sector_diffusion_alpha_h2": sector_diffusion.get("status"),
        "anonymous_attention_crowding_execution_h3": attention_crowding.get("status"),
    }
    return {
        "status": audit.get("status"), "audit_id": audit.get("audit_id"),
        "as_of": audit.get("generated_at"), "hypothesis_count": len(hypotheses),
        "hypotheses": [{
            "id": item.get("hypothesis_id"), "role": item.get("exclusive_role"),
            "state": current_states.get(item.get("hypothesis_id"))
            or item.get("current_state"),
        } for item in hypotheses],
        "claim_coverage": {
            "subjects": catalog.get("subjects"),
            "scoped": catalog.get("subjects_with_claim_scope"),
            "unscoped": catalog.get("subjects_without_claim_scope"),
            "recorded": catalog.get("subjects_with_claim_records"),
            "claim_records": semantic_summary.get("claim_records"),
            "source_urls": semantic_summary.get("source_urls"),
            "content_hash_frozen_sources": semantic_summary.get("transport_frozen"),
            "readable_body_sources": (semantic_summary.get("semantic_body_classes") or {}).get("readable_body"),
            "claim_text_verified": semantic_summary.get("claim_text_verified"),
            "quantitatively_admitted_claims": semantic_summary.get("quantitatively_admitted_claims"),
            "reproducibility_status": catalog.get("reproducibility_status"),
        },
        "sector_diffusion": sector_diffusion,
        "attention_crowding": attention_crowding,
        "applied_to_score": False, "applied_to_positions": False,
        "production": False,
        "reason": "三类匿名、互斥研究角色已冻结；传输哈希、正文可读与声明定位分层披露。H2固定公式已完成盲测并失败；H3仅部署Level-1前向观察，二者均未进入评分、仓位或提醒。",
    }


def sector_diffusion_research_program(*, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Expose the verified terminal H2 result without decision authority."""
    audit_path = project_root / "research" / SECTOR_DIFFUSION_AUDIT_NAME
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        if not _verified_bindings(audit, project_root=project_root):
            raise ValueError("binding_invalid")
        permissions = audit.get("permissions_after_audit") or {}
        for key in (
            "read_future_returns_or_ic", "run_blind_backtest", "change_unified_score",
            "change_position_policy", "production", "automatic_trading",
        ):
            if permissions.get(key) is not False:
                raise ValueError(f"permission_invalid:{key}")
        verified = audit.get("verified") or {}
        if verified.get("factor_rows") != 783866 or verified.get("signal_days") != 1969:
            raise ValueError("verified_counts_invalid")
        manifest_ref = audit["bindings"]["epoch_manifest"]
        manifest = json.loads((project_root / manifest_ref["path"]).read_text(encoding="utf-8"))
        epoch_ref = manifest["epoch"]
        epoch_path = project_root / epoch_ref["path"]
        if not epoch_path.is_file() or _sha256(epoch_path) != epoch_ref["sha256"]:
            raise ValueError("epoch_identity_invalid")
        resolution = json.loads((
            project_root / "research" / SECTOR_DIFFUSION_RESOLUTION_NAME
        ).read_text(encoding="utf-8"))
        if not _verified_bindings(resolution, project_root=project_root):
            raise ValueError("resolution_binding_invalid")
        if resolution.get("status") != "terminally_rejected_no_score_or_position_integration":
            raise ValueError("resolution_status_invalid")
        integration = resolution.get("integration_permissions") or {}
        if integration.get("show_terminal_research_result") is not True:
            raise ValueError("terminal_visibility_invalid")
        for key in (
            "change_professional_composite_score", "change_recommendation_rank",
            "change_account_exposure", "change_position_size",
            "generate_buy_or_sell_alert", "production", "automatic_trading",
        ):
            if integration.get(key) is not False:
                raise ValueError(f"resolution_permission_invalid:{key}")
        facts = resolution.get("blind_test_facts") or {}
        if facts.get("signal_days") != 231 or facts.get("failed_gate_count") != 7:
            raise ValueError("terminal_facts_invalid")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "unavailable_or_invalid", "factor_rows": 0,
            "signal_days": 0, "future_returns_read": False,
            "return_chain_ready": False, "applied_to_score": False,
            "production": False, "reason": f"H2板块扩散输入审计不可用：{exc}",
        }
    return {
        "status": resolution.get("status"),
        "as_of": verified.get("last_signal_day"),
        "factor_rows": verified.get("factor_rows"),
        "signal_days": verified.get("signal_days"),
        "mapped_coverage_pct": verified.get("mapped_coverage_pct"),
        "historical_evaluation_complete": True,
        "historical_gates_passed": False,
        "terminal_rejected": True,
        "blind_test": facts,
        "future_returns_read": True, "return_chain_ready": True,
        "applied_to_score": False, "applied_to_positions": False,
        "production": False,
        "reason": "PIT输入与可信复权开盘链已核验；固定H2公式在2025盲测中成本后失败，已永久终止，不计分、不控仓、不产生提醒。",
    }


def attention_crowding_research_program(*, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Expose H3 forward-capture truth without granting execution authority."""
    try:
        input_path = project_root / "research" / ATTENTION_CROWDING_INPUT_NAME
        input_audit = json.loads(input_path.read_text(encoding="utf-8"))
        amendment_path = (
            project_root / "research" / ATTENTION_CROWDING_INPUT_AMENDMENT_NAME
        )
        amendment = json.loads(amendment_path.read_text(encoding="utf-8"))
        amended = amendment.get("amends") or {}
        invalidated_rows = amendment.get("invalidated_bindings") or []
        invalidated = {
            row.get("name"): row for row in invalidated_rows
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        }
        current = amendment.get("current_evidence") or {}
        decision = amendment.get("decision") or {}
        v183_binding = (input_audit.get("bindings") or {}).get("v183_audit") or {}
        parser_binding = (
            (input_audit.get("bindings") or {}).get("live_level1_parser") or {}
        )
        amendment_1_path = (
            project_root / "research"
            / "ANONYMOUS_ATTENTION_CROWDING_H3_INPUT_FEASIBILITY_AMENDMENT_1.json"
        )
        predecessor = amendment.get("predecessor") or {}
        if (
            amendment.get("schema_version") != 1
            or amendment.get("status")
            != "parser_identity_refresh_confirmed_without_research_boundary_change"
            or predecessor.get("path")
            != "research/ANONYMOUS_ATTENTION_CROWDING_H3_INPUT_FEASIBILITY_AMENDMENT_1.json"
            or predecessor.get("sha256") != _sha256(amendment_1_path)
            or amended.get("path")
            != f"research/{ATTENTION_CROWDING_INPUT_NAME}"
            or amended.get("sha256") != _sha256(input_path)
            or set(invalidated) != {"v183_audit", "live_level1_parser"}
            or invalidated["v183_audit"].get("path") != v183_binding.get("path")
            or invalidated["v183_audit"].get("predecessor_sha256")
            != v183_binding.get("sha256")
            or invalidated["live_level1_parser"].get("path")
            != parser_binding.get("path")
            or invalidated["live_level1_parser"].get("predecessor_sha256")
            != parser_binding.get("sha256")
        ):
            raise ValueError("input_amendment_invalid")
        for name, binding in (input_audit.get("bindings") or {}).items():
            if name in invalidated:
                continue
            target = _inside_project(
                str(binding.get("path") or ""), project_root=project_root,
            )
            if (
                target is None
                or not target.is_file()
                or _sha256(target) != binding.get("sha256")
            ):
                raise ValueError(f"input_binding_invalid:{name}")
        current_audit_path = _inside_project(
            str(current.get("audit_path") or ""), project_root=project_root,
        )
        current_protocol_path = _inside_project(
            str(current.get("protocol_path") or ""), project_root=project_root,
        )
        current_parser_path = _inside_project(
            str(current.get("parser_path") or ""), project_root=project_root,
        )
        parser_admission_path = _inside_project(
            str(current.get("parser_runtime_admission_path") or ""),
            project_root=project_root,
        )
        if (
            current_audit_path is None
            or current_protocol_path is None
            or current_parser_path is None
            or parser_admission_path is None
            or not current_audit_path.is_file()
            or not current_protocol_path.is_file()
            or not current_parser_path.is_file()
            or not parser_admission_path.is_file()
            or current.get("audit_sha256") != _sha256(current_audit_path)
            or invalidated["v183_audit"].get("current_sha256")
            != _sha256(current_audit_path)
            or current.get("protocol_sha256") != _sha256(current_protocol_path)
            or current.get("parser_sha256") != _sha256(current_parser_path)
            or invalidated["live_level1_parser"].get("current_sha256")
            != _sha256(current_parser_path)
            or current.get("parser_runtime_admission_sha256")
            != _sha256(parser_admission_path)
        ):
            raise ValueError("input_amendment_current_identity_invalid")
        current_audit = json.loads(current_audit_path.read_text(encoding="utf-8"))
        for key in (
            "status", "data_gate_passed", "return_data_read",
            "return_data_computed", "single_historical_backtest_allowed",
            "successor_preregistration_allowed", "recommendation_eligible",
        ):
            expected_key = "audit_status" if key == "status" else key
            if current.get(expected_key) != current_audit.get(key):
                raise ValueError(f"input_amendment_fact_invalid:{key}")
        if (
            decision.get("historical_evaluation_ready") is not False
            or decision.get("forward_level1_observation_feasible") is not True
            or any(
                decision.get(key) is not False
                for key in (
                    "change_unified_score", "change_position_policy",
                    "change_buy_or_sell_alerts", "production",
                    "automatic_trading",
                )
            )
        ):
            raise ValueError("input_amendment_decision_boundary_invalid")
        if input_audit.get("status") != "historical_evaluation_not_admitted_forward_level1_observation_feasible":
            raise ValueError("input_status_invalid")
        historical = input_audit.get("historical_evaluation") or {}
        if (
            historical.get("ready") is not False
            or historical.get("returns_or_labels_read") is not False
            or historical.get("backtest_or_ic_run") is not False
        ):
            raise ValueError("historical_boundary_invalid")
        protocol_path = project_root / "research" / ATTENTION_CROWDING_PROTOCOL_NAME
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        for name, binding in (protocol.get("bindings") or {}).items():
            if name == "level1_parser":
                continue
            target = _inside_project(
                str(binding.get("path") or ""), project_root=project_root,
            )
            if (
                target is None or not target.is_file()
                or _sha256(target) != binding.get("sha256")
            ):
                raise ValueError(f"protocol_binding_invalid:{name}")
        protocol_amendment_path = (
            project_root / "research" / ATTENTION_CROWDING_PROTOCOL_AMENDMENT_NAME
        )
        protocol_amendment = json.loads(
            protocol_amendment_path.read_text(encoding="utf-8")
        )
        protocol_predecessor = protocol_amendment.get("predecessor") or {}
        current_parser = protocol_amendment.get("current_level1_parser") or {}
        if (
            protocol_amendment.get("status")
            != "parser_identity_refresh_only_no_formula_schedule_or_authority_change"
            or protocol_predecessor.get("path")
            != "research/ANONYMOUS_ATTENTION_CROWDING_H3_FORWARD_OBSERVATION_PROTOCOL_AMENDMENT_1.json"
            or protocol_predecessor.get("sha256")
            != _sha256(
                project_root / "research"
                / "ANONYMOUS_ATTENTION_CROWDING_H3_FORWARD_OBSERVATION_PROTOCOL_AMENDMENT_1.json"
            )
            or current_parser.get("path") != current.get("parser_path")
            or current_parser.get("sha256") != _sha256(current_parser_path)
            or protocol_amendment.get("input_feasibility_amendment", {}).get(
                "sha256"
            ) != _sha256(amendment_path)
        ):
            raise ValueError("protocol_amendment_invalid")
        permissions = protocol.get("permissions") or {}
        if permissions.get("capture_forward_observations") is not True:
            raise ValueError("capture_permission_invalid")
        for key in (
            "read_returns_or_labels", "compute_backtest_or_ic", "change_unified_score",
            "change_position_or_quantity", "change_buy_or_sell_alerts",
            "production_strategy", "automatic_trading",
        ):
            if permissions.get(key) is not False:
                raise ValueError(f"permission_invalid:{key}")
        deployment_path = project_root / "research" / ATTENTION_CROWDING_DEPLOYMENT_NAME
        deployment = json.loads(deployment_path.read_text(encoding="utf-8"))
        if not _verified_bindings(deployment, project_root=project_root):
            raise ValueError("deployment_binding_invalid")
        if deployment.get("status") != (
            "forward_observation_deployed_first_real_slot_captured"
        ):
            raise ValueError("deployment_status_invalid")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "unavailable_or_invalid", "historical_evaluation_ready": False,
            "protocol_ready": False, "forward_capture_ready": False,
            "packet_count": 0, "invalid_packet_count": 0,
            "applied_to_score": False, "applied_to_positions": False,
            "applied_to_alerts": False, "production": False,
            "reason": f"H3拥挤度前向观察不可用：{exc}",
        }
    packet_dir = project_root / "research/data/h3_execution_forward/v1/observation_packets"
    packets = sorted(packet_dir.glob("*.json")) if packet_dir.is_dir() else []
    verified_packets = 0
    invalid_packets = 0
    if packets:
        from .h3_execution_forward import verify_h3_forward_packet
        for packet in packets:
            try:
                valid = verify_h3_forward_packet(
                    packet, project_root=project_root,
                ).get("valid") is True
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                valid = False
            verified_packets += int(valid)
            invalid_packets += int(not valid)
    return {
        "status": deployment["status"],
        "as_of": deployment.get("generated_at"),
        "forward_start": protocol.get("forward_start"),
        "historical_evaluation_ready": False,
        "historical_failed_gate_count": len(input_audit.get("failed_historical_input_gates") or []),
        "protocol_ready": True,
        # 本函数只能证明协议/部署身份；后台线程是否存活与最近一次捕获结果
        # 由 API 层的 capture_runtime 提供，不能在这里静态宣称运行就绪。
        "forward_capture_ready": False,
        "packet_count": verified_packets,
        "invalid_packet_count": invalid_packets,
        "source_level": "Level-1 five-depth reference only",
        "level2_queue_or_partial_fill": False,
        "applied_to_score": False, "applied_to_positions": False,
        "applied_to_alerts": False, "production": False,
        "reason": "现有历史分钟/盘口不足以回测；已部署绑定全量行情覆盖与不可变Top24选择清单的固定时点Level-1观察包，先积累真实执行证据，不计分、不控仓、不触发提醒。",
    }


def latest_v2_research_observation(*, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Read the independently audited diagnostic row without granting decisions."""
    audit_path = project_root / "research" / MARKET_STATE_V2_AUDIT_NAME
    try:
        if _sha256(audit_path) != MARKET_STATE_V2_AUDIT_SHA256:
            raise ValueError("audit_sha256_mismatch")
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        permissions = audit.get("permissions_after_audit") or {}
        for key in (
            "historical_returns_or_backtest", "change_unified_stock_score",
            "change_live_position_policy", "production", "automatic_trading",
        ):
            if permissions.get(key) is not False:
                raise ValueError(f"permission_invalid:{key}")
        if not _verified_bindings(audit, project_root=project_root):
            raise ValueError("binding_invalid")
        epoch_ref = audit["bindings"]["feature_epoch"]
        epoch = json.loads((project_root / epoch_ref["path"]).read_text(encoding="utf-8"))
        row = epoch["rows"][-1]
        features = row["features"]
        declared = audit["latest_research_observation"]
        if (
            row.get("trade_date") != declared.get("trade_date")
            or features.get("confirmed_state") != declared.get("confirmed_state")
            or features.get("applied_to_live_positions") is not False
        ):
            raise ValueError("latest_observation_mismatch")
    except (OSError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "unavailable_or_invalid", "trade_date": None,
            "state": "UNKNOWN", "research_exposure_hypothesis_pct": None,
            "profitability_tested": False, "applied_to_live_positions": False,
            "reason": f"V2研究观察不可用：{exc}",
        }
    return {
        "status": "audited_diagnostic_not_validated", "trade_date": row["trade_date"],
        "state": features["confirmed_state"], "raw_state": features["raw_state"],
        "research_exposure_hypothesis_pct": features["research_exposure_hypothesis_pct"],
        "percentiles_pct": {
            key.removesuffix("_percentile"): round(float(value) * 100, 3)
            for key, value in features.items()
            if key.endswith("_percentile") and isinstance(value, (int, float))
        },
        "profitability_tested": False, "applied_to_live_positions": False,
        "applied_to_score": False,
        "reason": "历史诊断层可读，但无精确历史V8基线且尚无前向收益证据；只展示、不应用。",
    }


def latest_research_state(
    *, data_root: Path = DATA_ROOT, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Enrich the frozen snapshot response without changing its builder bytes."""
    payload = _base_research_state(data_root=data_root, project_root=project_root)
    payload["trader_style_research"] = trader_style_research_program(
        project_root=project_root
    )
    observation = latest_v2_research_observation(project_root=project_root)
    observation["applied_to_positions"] = bool(
        observation.get("applied_to_live_positions") is True
    )
    payload["model_observation"] = observation
    payload["applied_to_score"] = False
    payload["applied_to_positions"] = False
    payload["applied_to_alerts"] = False
    payload["production"] = False
    # V2 模型诊断分位保留在 model_observation.percentiles_pct；它不是冻结
    # C 维度尚缺的滚动分位，禁止用同名别名回填基础快照。
    return payload
