"""Small runtime identity gate for the frozen H1 forward capture."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADMISSION_PATH = (
    PROJECT_ROOT
    / "research/MARKET_STATE_FORWARD_CAPTURE_RUNTIME_ADMISSION_V6.json"
)
_DENIED_PERMISSIONS = (
    "simulate_orders_or_fills",
    "compute_returns_or_nav",
    "change_unified_stock_score",
    "change_live_position_policy",
    "production",
    "automatic_trading",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_h1_capture_runtime(
    *,
    project_root: Path = PROJECT_ROOT,
    admission_path: Path | None = None,
) -> dict[str, Any]:
    path = admission_path or (
        project_root / "research/MARKET_STATE_FORWARD_CAPTURE_RUNTIME_ADMISSION_V6.json"
    )
    errors: list[str] = []
    try:
        resolved = path.resolve()
        resolved.relative_to(project_root.resolve())
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "valid": False,
            "status": "identity_invalid",
            "errors": [f"admission_unreadable_or_outside_project:{type(exc).__name__}"],
        }
    if document.get("schema_version") != 1:
        errors.append("schema_version_invalid")
    if document.get("status") != "capture_runtime_identity_admitted_research_only":
        errors.append("status_invalid")
    bindings = document.get("bindings")
    if not isinstance(bindings, dict) or not bindings:
        errors.append("bindings_missing")
    else:
        for name, binding in bindings.items():
            raw = Path(str((binding or {}).get("path") or ""))
            target = raw if raw.is_absolute() else project_root / raw
            try:
                target = target.resolve()
                target.relative_to(project_root.resolve())
            except ValueError:
                errors.append(f"binding_outside_project:{name}")
                continue
            if not target.is_file():
                errors.append(f"binding_missing:{name}")
            elif _sha256(target) != (binding or {}).get("sha256"):
                errors.append(f"binding_sha256_mismatch:{name}")
    permissions = document.get("permissions") or {}
    if permissions.get("capture_immutable_forward_decision_packets") is not True:
        errors.append("capture_permission_invalid")
    for key in _DENIED_PERMISSIONS:
        if permissions.get(key) is not False:
            errors.append(f"permission_boundary_invalid:{key}")
    return {
        "valid": not errors,
        "status": "ready" if not errors else "identity_invalid",
        "admission_id": document.get("admission_id"),
        "admission_sha256": _sha256(resolved),
        "forward_start": document.get("forward_start"),
        "errors": errors,
        "permissions": permissions,
    }
