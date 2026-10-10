"""Truthful universe metadata for the audited SH/SZ candidate pipeline."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def audited_universe(project: Path) -> dict[str, object]:
    """Bind the base panel and STAR supplement to one monitoring identity.

    STAR stays outside the frozen factor panel. The combined count is exposed
    only when both audits are complete, current to the same date, and the STAR
    audit still binds the exact manifest on disk.
    """
    reports = project / "reports"
    base = _read_json(reports / "v4_daily_quality_audit.json")
    star_path = reports / "monitoring_star_daily_manifest.json"
    star_manifest = _read_json(star_path)
    star = _read_json(reports / "monitoring_star_daily_quality_audit.json")

    def number(source: dict[str, object], key: str) -> int:
        try:
            return int(source.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    base_files = number(base, "file_count")
    base_passed = number(base, "passed_files")
    base_failed = number(base, "failed_files")
    base_end = str((base.get("query_window") or {}).get("end_date") or "")
    base_verified = bool(
        base.get("ready_for_factor_research") is True
        and base_files > 0
        and base_passed == base_files
        and base_failed == 0
    )

    star_files = number(star, "file_count")
    star_target = number(star, "target_symbols")
    star_passed = number(star, "passed_files")
    star_failed = number(star, "failed_files")
    star_end = str((star.get("query_window") or {}).get("end_date") or "")
    manifest_target = number(star_manifest, "target_symbols")
    manifest_downloaded = number(star_manifest, "downloaded_symbols")
    manifest_end = str((star_manifest.get("query") or {}).get("end_date") or "")
    expected_manifest_hash = str(
        (star.get("input_fingerprints") or {}).get("manifest_sha256") or ""
    )
    star_verified = bool(
        star.get("ready_for_monitoring") is True
        and star_files > 0
        and star_files == star_target == star_passed
        and star_failed == 0
        and star_manifest.get("ready_for_monitoring_audit") is True
        and manifest_target == manifest_downloaded == star_files
        and star_end == manifest_end == base_end
        and expected_manifest_hash
        and expected_manifest_hash == _sha256(star_path)
    )
    verified = base_verified and star_verified
    excluded_special = (
        (star_manifest.get("universe_scope") or {}).get(
            "excluded_special_securities"
        ) or {}
    )
    bse_admission_path = project / "research" / "BSE_MONITORING_DATA_SOURCE_ADMISSION_V1.json"
    bse_admission = _read_json(bse_admission_path)
    bse_reason = str(
        bse_admission.get("decision")
        or "完整证券清单与历史日线尚未接入审计"
    )
    return {
        "status": "verified" if verified else "degraded",
        "count": base_files + star_files if verified else None,
        "data_asof": base_end if verified else None,
        "coverage_label": "沪深主板、创业板、科创板普通股票",
        "full_a_share_coverage": False,
        "included_segments": ["沪市主板", "深市主板", "创业板", "科创板"],
        "excluded_segments": [
            {
                "segment": "北交所",
                "status": bse_admission.get("status") or "not_covered",
                "reason": bse_reason,
                "admission_path": (
                    "research/BSE_MONITORING_DATA_SOURCE_ADMISSION_V1.json"
                    if bse_admission else None
                ),
            },
        ],
        "excluded_special_securities": excluded_special,
        "factor_panel_scope": "沪深主板与创业板；科创板仅用于监控候选",
        "sources": {
            "base_main_and_chinext": {
                "status": "verified" if base_verified else "degraded",
                "count": base_files if base_verified else None,
                "audit_generated_at": base.get("generated_at"),
                "data_asof": base_end or None,
            },
            "star_monitoring_supplement": {
                "status": "verified" if star_verified else "degraded",
                "count": star_files if star_verified else None,
                "audit_generated_at": star.get("generated_at"),
                "data_asof": star_end or None,
            },
        },
        "audit_generated_at": star.get("generated_at") if verified else None,
        "passed_files": base_passed + star_passed,
        "failed_files": base_failed + star_failed,
    }


def audited_realtime_universe(
    project: Path, snapshot_path: Path | None = None,
) -> dict[str, object]:
    """Return the complete eligible snapshot, or an explicitly partial fallback.

    ``top`` remains a legacy Top-300 contract.  Realtime cross-sectional ranking
    may call itself universe-first only when the snapshot also contains every
    eligible row bound to the same verified scan identity.
    """
    snapshot_path = snapshot_path or project / "research" / "full_market_top300.json"
    snapshot = _read_json(snapshot_path)
    universe = audited_universe(project)
    scan = snapshot.get("scan_audit") if isinstance(snapshot.get("scan_audit"), dict) else {}
    eligible = snapshot.get("eligible") if isinstance(snapshot.get("eligible"), list) else []
    legacy_top = snapshot.get("top") if isinstance(snapshot.get("top"), list) else []
    data_asof = str(snapshot.get("data_asof") or "")

    def valid_codes(rows: list[object]) -> list[str]:
        codes: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                return []
            code = str(row.get("code") or "")
            if not re.fullmatch(r"\d{6}", code):
                return []
            codes.append(code)
        return codes

    eligible_codes = valid_codes(eligible)
    eligible_count = int(snapshot.get("eligible_count") or 0)
    current_count = int(scan.get("current_asof_count") or 0)
    ranked_count = int(scan.get("ranked_count") or 0)
    eligible_complete = bool(
        universe.get("status") == "verified"
        and scan.get("status") == "verified"
        and scan.get("complete_path_scan") is True
        and scan.get("data_asof_matches_universe_audit") is True
        and data_asof
        and data_asof == universe.get("data_asof") == scan.get("data_asof")
        and eligible_codes
        and len(set(eligible_codes)) == len(eligible_codes)
        and eligible_count == current_count == ranked_count == len(eligible_codes)
        and all(str(row.get("data_asof") or "") == data_asof for row in eligible)
    )
    if eligible_complete:
        codes = eligible_codes
        status = "verified_full_eligible_snapshot"
        reason = None
    else:
        codes = valid_codes(legacy_top)
        status = "partial_cached_shortlist" if codes else "unavailable"
        reason = (
            "完整合格候选快照未冻结；仅使用旧Top候选缓存，不能称为全市场实时覆盖"
            if codes else "完整合格候选快照与旧Top缓存均不可用"
        )

    try:
        source = str(snapshot_path.relative_to(project))
    except ValueError:
        source = str(snapshot_path)
    universe_total = ranked_count or current_count or eligible_count or len(codes)
    return {
        "status": status,
        "reason": reason,
        "source": source,
        "data_asof": data_asof or None,
        "candidate_codes": codes,
        "input_count": len(codes),
        "universe_total": universe_total,
        "eligible_snapshot_complete": eligible_complete,
        "full_a_share_coverage": universe.get("full_a_share_coverage") is True,
        "coverage_label": universe.get("coverage_label"),
        "included_segments": universe.get("included_segments") or [],
        "excluded_segments": universe.get("excluded_segments") or [],
        "excluded_special_securities": universe.get("excluded_special_securities") or {},
        "audited_security_count": universe.get("count"),
    }
