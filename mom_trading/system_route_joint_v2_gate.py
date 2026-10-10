"""Runtime trust anchor for strict-forward V2 research persistence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .market_state_daily import PROJECT_ROOT
from .research_artifact_paths import research_artifact_path


ADMISSION_PATH = (
    PROJECT_ROOT
    / "research/SYSTEM_ROUTE_JOINT_V2_FORWARD_DEPLOYMENT_ADMISSION_V45.json"
)
# The admission binds final runtime-critical bytes but intentionally does not
# bind this gate, avoiding a circular identity dependency.  Successor release
# documents bind the gate after this trust anchor is embedded.
ADMISSION_SHA256 = "dfb00e2504c9a975cdc4f0685293db500b94125fecb3b1a6d716916bfb8abcd6"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_system_route_joint_v2_runtime(
    *, project_root: Path = PROJECT_ROOT,
    admission_path: Path | None = None,
    expected_admission_sha256: str | None = ADMISSION_SHA256,
) -> dict[str, Any]:
    path = admission_path or (
        project_root
        / "research/SYSTEM_ROUTE_JOINT_V2_FORWARD_DEPLOYMENT_ADMISSION_V45.json"
    )
    errors: list[str] = []
    document: dict[str, Any] = {}
    admission_sha: str | None = None
    try:
        resolved = path.resolve()
        resolved.relative_to(project_root.resolve())
        admission_sha = _sha(resolved)
        if not expected_admission_sha256 or admission_sha != expected_admission_sha256:
            errors.append("admission_sha256_mismatch")
        document = json.loads(resolved.read_text(encoding="utf-8"))
        if document.get("status") != (
            "research_authority_and_position_evidence_hardening"
        ):
            errors.append("admission_status_invalid")
        for name, binding in (document.get("bindings") or {}).items():
            target = (project_root / str(binding.get("path") or "")).resolve()
            try:
                target.relative_to(project_root.resolve())
            except ValueError:
                errors.append(f"binding_outside_project:{name}")
                continue
            if not target.is_file() or _sha(target) != binding.get("sha256"):
                errors.append(f"binding_invalid:{name}")
        for parent_name in ("adapter_protocol", "persistence_protocol"):
            parent = (document.get("bindings") or {}).get(parent_name) or {}
            parent_path = (
                project_root / str(parent.get("path") or "")
            ).resolve()
            try:
                parent_path.relative_to(project_root.resolve())
                nested = json.loads(parent_path.read_text(encoding="utf-8"))
                for child_name, child in (nested.get("bindings") or {}).items():
                    try:
                        child_path = research_artifact_path(child.get("path"), project_root=project_root)
                    except ValueError:
                        errors.append(
                            f"nested_binding_outside_project:{parent_name}:{child_name}"
                        )
                        continue
                    if (
                        not child_path.is_file()
                        or _sha(child_path) != child.get("sha256")
                    ):
                        errors.append(
                            f"nested_binding_invalid:{parent_name}:{child_name}"
                        )
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                errors.append(
                    f"nested_protocol_unreadable:{parent_name}:{type(exc).__name__}"
                )
        permissions = document.get("permissions") or {}
        if permissions.get("persist_strict_forward_research_packets") is not True:
            errors.append("capture_permission_missing")
        for key in (
            "read_returns_or_labels", "compute_performance",
            "change_unified_score", "change_trade_count",
            "change_position_or_quantity", "generate_buy_or_sell_alert",
            "connect_broker", "execute_orders", "production",
            "automatic_trading", "stable_profit_claim",
        ):
            if permissions.get(key) is not False:
                errors.append(f"permission_invalid:{key}")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"admission_unreadable_or_outside_project:{type(exc).__name__}")
    return {
        "valid": not errors,
        "status": "ready" if not errors else "identity_invalid",
        "errors": list(dict.fromkeys(errors)),
        "admission_id": document.get("admission_id"),
        "admission_sha256": admission_sha,
        "forward_start": document.get("forward_start"),
        "permissions": document.get("permissions") or {},
    }


__all__ = [
    "ADMISSION_PATH", "ADMISSION_SHA256",
    "verify_system_route_joint_v2_runtime",
]
