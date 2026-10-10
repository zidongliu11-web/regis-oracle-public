"""Independent verifier for the calendar-session V2 feature layer."""

from __future__ import annotations

import argparse
import hashlib
import json
from bisect import bisect_left, bisect_right
from collections import deque
from pathlib import Path
from statistics import fmean
from typing import Any

from mom_trading.market_state_daily import PROJECT_ROOT, _sha256


SCHEMA = "market_state_feature_epoch.v2"
FEATURE_NAMES = (
    "up_ratio_pct", "above_ma20_ratio_pct", "new_high_minus_low_ratio_pct",
    "yesterday_limit_continuation_ratio_pct", "highest_board",
    "closed_limit_ratio_pct", "eod_failed_limit_ratio_pct",
    "top_5pct_amount_share_pct",
)


def _equal(left: Any, right: Any, tolerance: float = 1e-6) -> bool:
    if left is None or right is None:
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) <= tolerance
    return left == right


def _raw(row: dict[str, Any]) -> dict[str, float | None]:
    return {
        "up_ratio_pct": row["B"]["up_ratio_pct"],
        "above_ma20_ratio_pct": row["B"]["above_ma20_ratio_pct"],
        "new_high_minus_low_ratio_pct": row["B"]["new_high_minus_low_ratio_pct"],
        "yesterday_limit_continuation_ratio_pct": row["L"]["yesterday_limit_continuation_ratio_pct"],
        "highest_board": float(row["L"]["highest_board"]),
        "closed_limit_ratio_pct": row["L"]["closed_limit_ratio_pct"],
        "eod_failed_limit_ratio_pct": row["F"]["eod_failed_limit_ratio_pct"],
        "top_5pct_amount_share_pct": row["C"]["top_5pct_amount_share_pct"],
    }


def _percentile(prior: deque[float | None], value: float | None) -> float | None:
    valid = sorted(float(item) for item in prior if item is not None)
    if value is None or len(valid) < 252:
        return None
    left = bisect_left(valid, value)
    right = bisect_right(valid, value)
    return round((left + 0.5 * (right - left)) / len(valid), 6)


def verify_epoch_v2(path: Path, *, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    errors: list[str] = []
    try:
        resolved = path.resolve()
        resolved.relative_to(project_root.resolve())
        payload = resolved.read_bytes()
        epoch = json.loads(payload)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"epoch_unreadable_or_outside_project:{exc}"]}
    if epoch.get("schema") != SCHEMA:
        errors.append("schema_invalid")
    boundaries = epoch.get("decision_boundaries") or {}
    expected = {
        "research_diagnostic_only": True, "contains_strategy_returns": False,
        "historical_v8_exact_baseline_available": False,
        "changes_unified_stock_score": False, "applied_to_live_positions": False,
        "production": False, "automatic_trading": False,
    }
    for key, value in expected.items():
        if boundaries.get(key) is not value:
            errors.append(f"decision_boundary_invalid:{key}")
    bindings = epoch.get("input_bindings") or {}
    for name, binding in bindings.items():
        artifact = (project_root / str(binding.get("path") or "")).resolve()
        try:
            artifact.relative_to(project_root.resolve())
        except ValueError:
            errors.append(f"input_outside_project:{name}")
            continue
        if not artifact.is_file() or artifact.is_symlink() or _sha256(artifact) != binding.get("sha256"):
            errors.append(f"input_binding_invalid:{name}")
    v1_ref = bindings.get("v1_same_day_raw_feature_epoch") or {}
    v1_path = project_root / str(v1_ref.get("path") or "")
    v1 = json.loads(v1_path.read_text(encoding="utf-8")) if v1_path.is_file() else {"rows": []}
    rows = epoch.get("rows") if isinstance(epoch.get("rows"), list) else []
    calendar = json.loads((project_root / "reports/v4_market_calendar.json").read_text(encoding="utf-8"))["trading_dates"]
    if [row.get("trade_date") for row in rows] != calendar[5:]:
        errors.append("date_sequence_invalid")
    if len(rows) != len(v1.get("rows") or []):
        errors.append("v1_raw_row_count_invalid")
    else:
        for index, (row, source) in enumerate(zip(rows, v1["rows"])):
            if {key: row.get(key) for key in ("trade_date", "B", "L", "F", "C")} != {
                key: source.get(key) for key in ("trade_date", "B", "L", "F", "C")
            }:
                errors.append(f"v1_raw_fields_changed:{index}")
                break

    history = {name: deque(maxlen=504) for name in FEATURE_NAMES}
    previous_raw = "UNKNOWN"
    confirmed = "UNKNOWN"
    state_counts: dict[str, int] = {}
    exposures = {"UNKNOWN": 0, "RISK_OFF": 20, "STABILIZING": 40, "RISK_ON": 80, "OVERHEATED": 40}
    for index, row in enumerate(rows):
        raw = _raw(row)
        features = row.get("features") or {}
        percentiles = {name: _percentile(history[name], value) for name, value in raw.items()}
        for name, value in raw.items():
            if not _equal(features.get(f"{name}_percentile"), percentiles[name]):
                errors.append(f"calendar_lagged_percentile_invalid:{index}:{name}")
                break
        if errors:
            break
        if any(value is None for value in percentiles.values()):
            raw_state, extreme = "UNKNOWN", False
        else:
            b_strength = fmean(percentiles[name] for name in FEATURE_NAMES[:3])
            l_strength = fmean(percentiles[name] for name in FEATURE_NAMES[3:6])
            f_stress = percentiles["eod_failed_limit_ratio_pct"]
            c_stress = percentiles["top_5pct_amount_share_pct"]
            directional = fmean((b_strength, l_strength, 1 - f_stress))
            extreme = f_stress >= 0.95 or directional <= 0.10
            if f_stress >= 0.90 or directional <= 0.33:
                raw_state = "RISK_OFF"
            elif directional >= 0.67 and c_stress >= 0.90:
                raw_state = "OVERHEATED"
            elif directional >= 0.67:
                raw_state = "RISK_ON"
            else:
                raw_state = "STABILIZING"
        if raw_state == "UNKNOWN":
            confirmed = "UNKNOWN"
        elif extreme:
            confirmed = "RISK_OFF"
        elif raw_state == previous_raw:
            confirmed = raw_state
        if features.get("raw_state") != raw_state or features.get("confirmed_state") != confirmed:
            errors.append(f"state_or_confirmation_invalid:{index}")
            break
        if features.get("research_exposure_hypothesis_pct") != exposures[confirmed]:
            errors.append(f"hypothesis_exposure_invalid:{index}")
            break
        if features.get("applied_to_live_positions") is not False:
            errors.append(f"live_position_boundary_invalid:{index}")
            break
        state_counts[confirmed] = state_counts.get(confirmed, 0) + 1
        for name in FEATURE_NAMES:
            history[name].append(raw[name])
        previous_raw = raw_state
    return {
        "valid": not errors, "errors": errors,
        "epoch_sha256": hashlib.sha256(payload).hexdigest(),
        "sessions": len(rows), "state_counts": state_counts,
        "v1_raw_fields_reused_exactly": not any(error.startswith("v1_raw_fields_changed") for error in errors),
        "contains_strategy_returns": False,
        "applied_to_live_positions": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="独立校验V2交易日窗口")
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    result = verify_epoch_v2(args.path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
