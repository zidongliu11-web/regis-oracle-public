"""Immutable, capture-only forward packets for the H1 market-state study."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, time
from pathlib import Path
from .research_artifact_paths import research_artifact_path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .market_state_daily import PROJECT_ROOT, _atomic_publish, _canonical_bytes, _sha256, verify_snapshot


SH_TZ = ZoneInfo("Asia/Shanghai")
AMENDMENT_PATH = PROJECT_ROOT / "research" / "ANONYMOUS_MARKET_STATE_OVERLAY_H1_PREREGISTRATION_AMENDMENT_1.json"
AMENDMENT_SHA256 = "dcc2ca80a0ca2981fdebdd0db36e1e4ebc2c945bd9c8b1b96b1bdf2cca9ee4bd"
SELECTION_AMENDMENT_PATH = PROJECT_ROOT / "research" / "H1_CANDIDATE_SELECTION_AMENDMENT_2.json"
SELECTION_AMENDMENT_SHA256 = "7f40c4fd975e80e3ed8a05f79bb65561bbd2d8a9222cf3e86be309487779b43f"
PACKET_DIR = PROJECT_ROOT / "research" / "data" / "market_state_forward" / "v1" / "decision_packets"
SELECTION_MANIFEST_DIR = PROJECT_ROOT / "research" / "data" / "market_state_forward" / "v2" / "selection_manifests"
CALENDAR_PATH = PROJECT_ROOT / "reports" / "current_market_calendar.json"


def _parse_clock(value: str) -> time:
    return time.fromisoformat(value)


def _slot(amendment: dict[str, Any], clock: time) -> str | None:
    for item in (amendment.get("decision_schedule") or {}).get("slots") or []:
        if _parse_clock(str(item["capture_from"])) <= clock <= _parse_clock(str(item["capture_until"])):
            return str(item["id"])
    return None


def _expected_previous_session(calendar: list[str], decision_day: str) -> str | None:
    earlier = [day for day in calendar if day < decision_day]
    return earlier[-1] if earlier else None


def _load_bound_feature(
    amendment: dict[str, Any], *, project_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    audit_ref = amendment["input_audit"]
    audit_path = research_artifact_path(audit_ref["path"], project_root=project_root)
    if _sha256(audit_path) != audit_ref["sha256"]:
        raise RuntimeError("H1输入审计哈希不匹配")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    feature_ref = audit["bindings"]["feature_epoch"]
    feature_path = research_artifact_path(feature_ref["path"], project_root=project_root)
    if _sha256(feature_path) != feature_ref["sha256"]:
        raise RuntimeError("H1 V2特征层哈希不匹配")
    feature = json.loads(feature_path.read_text(encoding="utf-8"))
    identity = {
        "path": str(feature_path.relative_to(project_root)),
        "sha256": feature_ref["sha256"],
        "audit_path": str(audit_path.relative_to(project_root)),
        "audit_sha256": audit_ref["sha256"],
    }
    daily_dir = project_root / "research/data/market_state_daily/v1/feature_daily_v2"
    daily_paths = sorted(daily_dir.glob("*.json"))
    if daily_paths:
        from research.publish_market_state_feature_daily_v2 import verify_daily_feature

        verified = verify_daily_feature(daily_paths[-1], project_root=project_root)
        if not verified["valid"]:
            raise RuntimeError(f"最新V2日更特征校验失败：{verified['errors']}")
        feature = {"rows": [verified["document"]["row"]]}
        identity.update({
            "daily_feature_path": str(daily_paths[-1].relative_to(project_root)),
            "daily_feature_sha256": verified["feature_sha256"],
        })
    return feature, identity


def _snapshot_time(snapshot: dict[str, Any], fallback: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(snapshot.get("date") or ""))
    except ValueError:
        return fallback
    return parsed.replace(tzinfo=SH_TZ) if parsed.tzinfo is None else parsed.astimezone(SH_TZ)


def _ordered_codes_sha256(codes: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(codes, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _verify_selection_manifest(
    path: Path, *, project_root: Path,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        resolved = research_artifact_path(path, project_root=project_root)
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"selection_manifest_unreadable:{exc}"]}
    if document.get("schema") != "market_candidate_selection_manifest.v1":
        errors.append("selection_manifest_schema_invalid")
    manifest_id = str(document.get("manifest_id") or "")
    identity = dict(document)
    identity.pop("manifest_id", None)
    if hashlib.sha256(_canonical_bytes(identity)).hexdigest() != manifest_id:
        errors.append("selection_manifest_id_invalid")
    top300 = document.get("ordered_top300_codes")
    top24 = document.get("ordered_top24_codes")
    if not isinstance(top300, list) or not top300 or len(top300) > 300:
        errors.append("selection_manifest_top300_invalid")
    elif _ordered_codes_sha256(top300) != document.get("ordered_top300_codes_sha256"):
        errors.append("selection_manifest_top300_hash_invalid")
    if not isinstance(top24, list) or len(top24) > 24:
        errors.append("selection_manifest_top24_invalid")
    elif _ordered_codes_sha256(top24) != document.get("ordered_top24_codes_sha256"):
        errors.append("selection_manifest_top24_hash_invalid")
    provenance = document.get("selection_provenance") or {}
    quotes = provenance.get("quote_cross_section") or {}
    if (
        provenance.get("status") != "verified"
        or quotes.get("coverage_complete") is not True
        or provenance.get("returns_or_labels_read") is not False
    ):
        errors.append("selection_manifest_provenance_incomplete")
    source = document.get("eligible_snapshot") or {}
    raw_path = Path(str(source.get("path") or ""))
    snapshot_path = raw_path if raw_path.is_absolute() else project_root / raw_path
    try:
        snapshot_path = research_artifact_path(snapshot_path, project_root=project_root)
    except ValueError:
        errors.append("selection_manifest_snapshot_outside_project")
    else:
        if not snapshot_path.is_file() or _sha256(snapshot_path) != source.get("sha256"):
            errors.append("selection_manifest_snapshot_identity_invalid")
        elif source.get("storage") == "content_addressed_immutable":
            provenance = document.get("selection_provenance") or {}
            if (
                provenance.get("eligible_snapshot_path") != source.get("path")
                or provenance.get("eligible_snapshot_sha256") != source.get("sha256")
            ):
                errors.append("selection_manifest_snapshot_provenance_mismatch")
            if snapshot_path.name != f"{source.get('sha256')}.json":
                errors.append("selection_manifest_snapshot_archive_name_invalid")
            snapshot: dict[str, Any] = {}
            try:
                loaded = json.loads(snapshot_path.read_text(encoding="utf-8"))
                snapshot = loaded if isinstance(loaded, dict) else {}
                eligible = snapshot.get("eligible")
                codes = [
                    str(row.get("code") or "") for row in eligible or []
                    if isinstance(row, dict)
                ]
            except (OSError, json.JSONDecodeError):
                eligible, codes = None, []
            if (
                not isinstance(eligible, list)
                or len(eligible) != source.get("eligible_count")
                or any(not code for code in codes)
                or len(codes) != len(eligible)
                or _ordered_codes_sha256(codes)
                != source.get("ordered_eligible_codes_sha256")
                or snapshot.get("data_asof") != source.get("as_of")
            ):
                errors.append("selection_manifest_snapshot_content_invalid")
    return {
        "valid": not errors,
        "errors": errors,
        "manifest_id": manifest_id,
        "document": document,
    }


def _freeze_selection_manifest(
    snapshot: dict[str, Any], candidates: list[dict[str, Any]], *,
    decision_day: str, slot_id: str, project_root: Path,
    selection_amendment_path: Path,
    expected_selection_amendment_sha256: str | None,
    manifest_dir: Path,
) -> dict[str, Any]:
    amendment_sha = _sha256(selection_amendment_path)
    if (
        expected_selection_amendment_sha256
        and amendment_sha != expected_selection_amendment_sha256
    ):
        raise RuntimeError("H1选择修订哈希不匹配")
    selection_amendment = json.loads(
        selection_amendment_path.read_text(encoding="utf-8")
    )
    provenance = snapshot.get("selection_provenance") or {}
    quotes = provenance.get("quote_cross_section") or {}
    selected_codes = [str(code) for code in provenance.get("ordered_selected_codes") or []]
    if (
        provenance.get("status") != "verified"
        or provenance.get("returns_or_labels_read") is not False
        or quotes.get("coverage_complete") is not True
        or int(quotes.get("coverage_attempted") or 0) < int(provenance.get("eligible_count") or 0)
        or int(quotes.get("coverage_returned") or 0) < int(provenance.get("eligible_count") or 0)
        or not selected_codes
        or len(selected_codes) > 300
        or _ordered_codes_sha256(selected_codes)
        != provenance.get("ordered_selected_codes_sha256")
    ):
        raise RuntimeError("H1选择证据不完整，禁止前向捕获")
    top24_codes = [str(row.get("code") or "") for row in candidates[:24]]
    if any(not code for code in top24_codes) or len(set(top24_codes)) != len(top24_codes):
        raise RuntimeError("H1 Top24候选代码无效")
    snapshot_raw = Path(str(provenance.get("eligible_snapshot_path") or ""))
    snapshot_path = snapshot_raw if snapshot_raw.is_absolute() else project_root / snapshot_raw
    snapshot_path = research_artifact_path(snapshot_path, project_root=project_root)
    snapshot_bytes = snapshot_path.read_bytes()
    snapshot_sha = hashlib.sha256(snapshot_bytes).hexdigest()
    if snapshot_sha != provenance.get("eligible_snapshot_sha256"):
        raise RuntimeError("H1 eligible快照哈希不匹配")
    archived_path = manifest_dir.parent / "eligible_snapshots" / f"{snapshot_sha}.json"
    _atomic_publish(archived_path, snapshot_bytes)
    archived_relative = str(archived_path.relative_to(project_root))
    frozen_provenance = json.loads(json.dumps(provenance, ensure_ascii=False))
    frozen_provenance["eligible_snapshot_path"] = archived_relative
    frozen_provenance["eligible_snapshot_sha256"] = snapshot_sha
    assessment = snapshot.get("assessment_strategy") or {}
    document = {
        "schema": "market_candidate_selection_manifest.v1",
        "decision_day": decision_day,
        "slot_id": slot_id,
        "selection_amendment": {
            "path": str(selection_amendment_path.relative_to(project_root)),
            "sha256": amendment_sha,
        },
        "eligible_snapshot": {
            "path": archived_relative,
            "sha256": snapshot_sha,
            "storage": "content_addressed_immutable",
            "as_of": provenance.get("data_asof"),
            "eligible_count": provenance.get("eligible_count"),
            "ordered_eligible_codes_sha256": provenance.get(
                "ordered_eligible_codes_sha256"
            ),
        },
        "selection_provenance": frozen_provenance,
        "ordered_top300_codes": selected_codes,
        "ordered_top300_codes_sha256": _ordered_codes_sha256(selected_codes),
        "ordered_top24_codes": top24_codes,
        "ordered_top24_codes_sha256": _ordered_codes_sha256(top24_codes),
        "top24_selection": {
            "professional_preview_available_count": assessment.get(
                "professional_preview_available_count"
            ),
            "professional_preview_scope_count": assessment.get(
                "professional_preview_scope_count"
            ),
            "professional_preview_complete": assessment.get(
                "professional_preview_complete"
            ) is True,
            "selection_rule": assessment.get("selection_rule"),
            "tie_break": "existing_market_order_then_code_ascending",
        },
        "implementation": {
            "market_state_forward_sha256": _sha256(Path(__file__)),
            "signal_system_sha256": _sha256(
                project_root / "mom_trading/signal_system.py"
            ),
            "app_sha256": _sha256(project_root / "mom_trading/app.py"),
        },
        "returns_or_labels_read": False,
    }
    document["manifest_id"] = hashlib.sha256(_canonical_bytes(document)).hexdigest()
    path = manifest_dir / f"{decision_day}-{slot_id}.json"
    if path.exists():
        verified = _verify_selection_manifest(path, project_root=project_root)
        if not verified["valid"]:
            raise RuntimeError(
                "既有H1选择manifest无效:" + ",".join(verified["errors"])
            )
        existing = verified["document"]
        if existing.get("manifest_id") != document.get("manifest_id"):
            raise RuntimeError("既有H1选择manifest与当前候选不一致")
        return {"path": path, "sha256": _sha256(path), "document": existing}
    _atomic_publish(path, _canonical_bytes(document))
    return {"path": path, "sha256": _sha256(path), "document": document}


def verify_forward_packet(path: Path, *, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    errors: list[str] = []
    try:
        resolved = research_artifact_path(path, project_root=project_root)
        packet = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"packet_unreadable_or_outside_project:{exc}"]}
    if packet.get("schema") != "market_state_forward_decision_packet.v1":
        errors.append("schema_invalid")
    packet_id = str(packet.get("packet_id") or "")
    identity_payload = dict(packet)
    identity_payload.pop("packet_id", None)
    if hashlib.sha256(_canonical_bytes(identity_payload)).hexdigest() != packet_id:
        errors.append("packet_id_invalid")
    permissions = packet.get("permissions") or {}
    for key in ("simulate_orders_or_fills", "compute_returns_or_nav", "change_live_position_policy", "production", "automatic_trading"):
        if permissions.get(key) is not False:
            errors.append(f"permission_boundary_invalid:{key}")
    if packet.get("capture_only") is not True or packet.get("no_backfill") is not True:
        errors.append("capture_boundary_invalid")
    candidates = packet.get("assessed_candidates_in_order")
    if not isinstance(candidates, list) or len(candidates) != packet.get("assessed_count"):
        errors.append("assessed_candidate_count_invalid")
    treatment = packet.get("treatment_observation") or {}
    if treatment.get("applied_to_user_execution") is not False or treatment.get("profitability_tested") is not False:
        errors.append("treatment_truth_invalid")
    if str(treatment.get("state_as_of") or "") >= str(packet.get("decision_day") or ""):
        errors.append("market_state_not_strictly_prior")
    for key in ("amendment", "baseline"):
        artifact = packet.get(key) or {}
        try:
            artifact_path = research_artifact_path(artifact.get("path"), project_root=project_root)
        except ValueError:
            errors.append(f"artifact_outside_project:{key}")
            continue
        if not artifact_path.is_file() or _sha256(artifact_path) != artifact.get("sha256"):
            errors.append(f"artifact_identity_invalid:{key}")
    selection_amendment = packet.get("selection_amendment") or {}
    selection_amendment_path = (
        project_root / str(selection_amendment.get("path") or "")
    ).resolve()
    if (
        not selection_amendment_path.is_file()
        or _sha256(selection_amendment_path) != selection_amendment.get("sha256")
    ):
        errors.append("selection_amendment_identity_invalid")
    selection_manifest = packet.get("selection_manifest") or {}
    selection_manifest_path = (
        project_root / str(selection_manifest.get("path") or "")
    ).resolve()
    if (
        not selection_manifest_path.is_file()
        or _sha256(selection_manifest_path) != selection_manifest.get("sha256")
    ):
        errors.append("selection_manifest_identity_invalid")
    else:
        manifest_verification = _verify_selection_manifest(
            selection_manifest_path, project_root=project_root,
        )
        if manifest_verification.get("valid") is not True:
            errors.append("selection_manifest_invalid")
        else:
            manifest = manifest_verification["document"]
            candidate_codes = [str(row.get("code") or "") for row in candidates or []]
            if candidate_codes[:24] != manifest.get("ordered_top24_codes"):
                errors.append("selection_manifest_top24_mismatch")
    daily_path = (project_root / str(treatment.get("daily_snapshot_path") or "")).resolve()
    if not daily_path.is_file() or _sha256(daily_path) != treatment.get("daily_snapshot_sha256"):
        errors.append("daily_snapshot_identity_invalid")
    return {"valid": not errors, "errors": errors, "packet_id": packet_id, "packet": packet}


def capture_forward_decision_packet(
    snapshot: dict[str, Any], pre_account_items: list[dict[str, Any]], *,
    now: datetime | None = None,
    project_root: Path = PROJECT_ROOT,
    amendment_path: Path = AMENDMENT_PATH,
    expected_amendment_sha256: str | None = AMENDMENT_SHA256,
    packet_dir: Path = PACKET_DIR,
    calendar_path: Path = CALENDAR_PATH,
    snapshot_dir: Path | None = None,
    snapshot_verifier: Callable[..., dict[str, Any]] = verify_snapshot,
    selection_amendment_path: Path = SELECTION_AMENDMENT_PATH,
    expected_selection_amendment_sha256: str | None = SELECTION_AMENDMENT_SHA256,
    selection_manifest_dir: Path = SELECTION_MANIFEST_DIR,
) -> dict[str, Any]:
    observed = (now or datetime.now(SH_TZ)).astimezone(SH_TZ)
    amendment_sha = _sha256(amendment_path)
    if expected_amendment_sha256 and amendment_sha != expected_amendment_sha256:
        return {"status": "identity_invalid", "reason": "amendment_sha256_mismatch", "captured": False}
    amendment = json.loads(amendment_path.read_text(encoding="utf-8"))
    decision_day = observed.date().isoformat()
    if decision_day < str(amendment["forward_start"]):
        return {"status": "waiting_forward_start", "forward_start": amendment["forward_start"], "captured": False}
    calendar = [
        str(day) for day in json.loads(calendar_path.read_text(encoding="utf-8")).get("trading_dates") or []
    ]
    if decision_day not in calendar:
        return {"status": "not_trading_session", "decision_day": decision_day, "captured": False}
    slot_id = _slot(amendment, observed.timetz().replace(tzinfo=None))
    if slot_id is None:
        return {"status": "outside_capture_slot", "decision_day": decision_day, "captured": False}
    pipeline = snapshot.get("pipeline_identity") if isinstance(snapshot.get("pipeline_identity"), dict) else {}
    if pipeline.get("id") != "unified_opportunity_pipeline_v3":
        return {"status": "snapshot_incomplete", "reason": "pipeline_identity_invalid", "captured": False}
    assessed_count = snapshot.get("assessed_count")
    if not isinstance(assessed_count, int) or assessed_count != len(pre_account_items):
        return {"status": "snapshot_incomplete", "reason": "pre_account_assessment_count_mismatch", "captured": False}
    baseline_ref = amendment["baseline"]
    baseline_path = research_artifact_path(baseline_ref["path"], project_root=project_root)
    if _sha256(baseline_path) != baseline_ref["sha256"]:
        return {"status": "identity_invalid", "reason": "v8_sha256_mismatch", "captured": False}
    try:
        feature, feature_identity = _load_bound_feature(amendment, project_root=project_root)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, RuntimeError) as exc:
        return {"status": "market_feature_invalid", "reason": str(exc), "captured": False}
    expected_state_day = _expected_previous_session(calendar, decision_day)
    feature_row = (feature.get("rows") or [])[-1] if feature.get("rows") else {}
    if feature_row.get("trade_date") != expected_state_day:
        return {
            "status": "market_feature_not_ready", "captured": False,
            "expected_state_day": expected_state_day,
            "available_state_day": feature_row.get("trade_date"),
        }

    daily_dir = snapshot_dir or project_root / "research/data/market_state_daily/v1/snapshots"
    daily_path = daily_dir / f"{expected_state_day}.json"
    verification = snapshot_verifier(daily_path, project_root=project_root)
    if verification.get("valid") is not True:
        return {"status": "daily_snapshot_invalid", "captured": False, "errors": verification.get("errors") or []}
    daily_sha = verification.get("snapshot_sha256") or _sha256(daily_path)
    decision_at = _snapshot_time(snapshot, observed)
    candidates = json.loads(json.dumps(pre_account_items, ensure_ascii=False, allow_nan=False))
    try:
        selection_manifest = _freeze_selection_manifest(
            snapshot,
            candidates,
            decision_day=decision_day,
            slot_id=slot_id,
            project_root=project_root,
            selection_amendment_path=selection_amendment_path,
            expected_selection_amendment_sha256=(
                expected_selection_amendment_sha256
            ),
            manifest_dir=selection_manifest_dir,
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, RuntimeError) as exc:
        return {
            "status": "selection_provenance_invalid",
            "reason": str(exc),
            "captured": False,
        }
    eligible_codes = [
        str(item.get("code") or "") for item in candidates
        if ((item.get("assessment") or {}).get("verdict") == "APPROVE"
            and (item.get("alert") or {}).get("status") == "eligible_pending_plan")
    ]
    packet = {
        "schema": "market_state_forward_decision_packet.v1",
        "decision_day": decision_day,
        "decision_at": decision_at.isoformat(timespec="seconds"),
        "slot_id": slot_id,
        "capture_only": True,
        "no_backfill": True,
        "amendment": {
            "path": str(amendment_path.relative_to(project_root)),
            "sha256": amendment_sha,
        },
        "selection_amendment": {
            "path": str(selection_amendment_path.relative_to(project_root)),
            "sha256": _sha256(selection_amendment_path),
        },
        "selection_manifest": {
            "path": str(selection_manifest["path"].relative_to(project_root)),
            "sha256": selection_manifest["sha256"],
            "manifest_id": selection_manifest["document"]["manifest_id"],
        },
        "baseline": baseline_ref,
        "pipeline_identity": pipeline,
        "monitoring_scope": snapshot.get("monitoring_scope") or {},
        "market_phase": snapshot.get("market_phase"),
        "buy_window": snapshot.get("buy_window") or {},
        "assessment_counts": ((snapshot.get("signal_status") or {}).get("assessment_counts") or {}),
        "assessed_count": assessed_count,
        "eligible_pre_account_codes": eligible_codes,
        "eligible_pre_account_count": len(eligible_codes),
        "assessed_candidates_in_order": candidates,
        "control_observation": {
            "policy": "manual_opportunity_execution_v8_market_scaled_risk_budget",
            "target_exposure_pct": (snapshot.get("account_execution") or {}).get("market_target_exposure_pct"),
            "applied_to_user_execution": True,
        },
        "treatment_observation": {
            "feature_identity": feature_identity,
            "daily_snapshot_path": str(daily_path.relative_to(project_root)),
            "daily_snapshot_sha256": daily_sha,
            "state_as_of": feature_row.get("trade_date"),
            "raw_state": (feature_row.get("features") or {}).get("raw_state"),
            "confirmed_state": (feature_row.get("features") or {}).get("confirmed_state"),
            "research_exposure_hypothesis_pct": (feature_row.get("features") or {}).get("research_exposure_hypothesis_pct"),
            "applied_to_user_execution": False,
            "profitability_tested": False,
        },
        "rule_bindings": {
            "costs_sha256": _sha256(project_root / "quantqc/costs.py"),
            "allocation_sha256": _sha256(project_root / "mom_trading/profit_pipeline.py"),
            "control_market_budget_sha256": _sha256(project_root / "mom_trading/trading_calendar.py"),
        },
        "permissions": {
            "simulate_orders_or_fills": False, "compute_returns_or_nav": False,
            "change_live_position_policy": False, "production": False,
            "automatic_trading": False,
        },
    }
    packet_id = hashlib.sha256(_canonical_bytes(packet)).hexdigest()
    packet["packet_id"] = packet_id
    path = packet_dir / f"{decision_day}-{slot_id}.json"
    if path.exists():
        verification = verify_forward_packet(path, project_root=project_root)
        if not verification["valid"]:
            return {
                "status": "existing_packet_invalid", "captured": False,
                "path": str(path), "errors": verification["errors"],
            }
        existing = verification["packet"]
        return {
            "status": "already_captured", "captured": True,
            "packet_id": existing.get("packet_id"), "path": str(path),
            "eligible_pre_account_count": existing.get("eligible_pre_account_count"),
        }
    _atomic_publish(path, _canonical_bytes(packet))
    return {
        "status": "captured", "captured": True, "packet_id": packet_id,
        "path": str(path), "eligible_pre_account_count": len(eligible_codes),
        "orders_or_returns_computed": False,
    }


__all__ = ["capture_forward_decision_packet", "verify_forward_packet"]
