"""Fail-closed identity gate for the shadow market/sector input collector."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .market_state_daily import PROJECT_ROOT


ADMISSION_PATH = (
    PROJECT_ROOT / "research/MARKET_SECTOR_ROUTE_FORWARD_DEPLOYMENT_ADMISSION_V4.json"
)
ADMISSION_SHA256 = "eb006efb56d7b2107184dd43615e81631bd99498a277ff876c41328e5aac0688"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_market_sector_route_runtime(
    *, project_root: Path = PROJECT_ROOT, admission_path: Path = ADMISSION_PATH,
    expected_admission_sha256: str | None = ADMISSION_SHA256,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        admission_sha = _sha(admission_path)
        if expected_admission_sha256 and admission_sha != expected_admission_sha256:
            errors.append("admission_sha256_mismatch")
        admission = json.loads(admission_path.read_text(encoding="utf-8"))
        if admission.get("status") != "capture_runtime_admitted_waiting_first_real_slot":
            errors.append("admission_status_invalid")
        for name, binding in (admission.get("bindings") or {}).items():
            path = (project_root / str(binding.get("path") or "")).resolve()
            try:
                path.relative_to(project_root.resolve())
            except ValueError:
                errors.append(f"binding_outside_project:{name}")
                continue
            if not path.is_file() or _sha(path) != binding.get("sha256"):
                errors.append(f"binding_invalid:{name}")
        permissions = admission.get("permissions") or {}
        if permissions.get("capture_raw_input_packet") is not True:
            errors.append("capture_permission_missing")
        for key in (
            "read_returns_or_labels", "change_unified_score", "change_trade_count",
            "change_position_or_quantity", "generate_buy_or_sell_alert",
            "production", "automatic_trading",
        ):
            if permissions.get(key) is not False:
                errors.append(f"permission_invalid:{key}")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"admission_unreadable:{exc}")
        admission_sha = None
        admission = {}
    return {
        "valid": not errors,
        "errors": errors,
        "admission_id": admission.get("admission_id"),
        "admission_sha256": admission_sha,
        "forward_start": admission.get("forward_start"),
    }


__all__ = ["verify_market_sector_route_runtime"]
