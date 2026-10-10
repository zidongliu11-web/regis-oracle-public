"""Correct V1 percentiles to use the prior 504 market sessions.

V1's same-day B/L/F/C rows are reused byte-for-byte.  This successor does not
rescan market files or read any strategy return; it only replaces the rolling
percentile and derived research-state layer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import deque
from pathlib import Path
from typing import Any

from mom_trading.market_state_daily import DATA_ROOT, PROJECT_ROOT, _atomic_publish, _canonical_bytes, _sha256
from research.build_market_state_feature_epoch_v1 import RAW_FEATURES, _raw_feature_values, _raw_state


SCHEMA = "market_state_feature_epoch.v2"
V1_PATH = DATA_ROOT / "feature_epochs" / "2018-01-09_2026-08-28.json"
V1_SHA256 = "bfebbe290b668f2e0c632db5174af5235c259e76b67a77e2d8d58e2fff96e5a0"
CORRECTION_PATH = PROJECT_ROOT / "research" / "MARKET_STATE_FEATURE_EPOCH_V1_CORRECTION_1.json"
CORRECTION_SHA256 = "d6f576ed55a98d9a8932e74061f7e92b9d62827a8c7fdc0fd252bc7c12660e09"
OUTPUT_DIR = DATA_ROOT / "feature_epochs_v2"


def _midrank(prior: deque[float | None], value: float | None, minimum: int) -> float | None:
    valid = sorted(float(item) for item in prior if item is not None)
    if value is None or len(valid) < minimum:
        return None
    less = sum(item < value for item in valid)
    equal = sum(item == value for item in valid)
    return round((less + 0.5 * equal) / len(valid), 6)


def _apply_calendar_features(
    rows: list[dict[str, Any]], *, window: int = 504, minimum: int = 252,
) -> None:
    history = {name: deque(maxlen=window) for name in RAW_FEATURES}
    previous_raw = "UNKNOWN"
    confirmed = "UNKNOWN"
    exposures = {
        "UNKNOWN": 0, "RISK_OFF": 20, "STABILIZING": 40,
        "RISK_ON": 80, "OVERHEATED": 40,
    }
    for row in rows:
        raw = _raw_feature_values(row)
        features: dict[str, Any] = dict(raw)
        for name in RAW_FEATURES:
            features[f"{name}_percentile"] = _midrank(history[name], raw[name], minimum)
        state = _raw_state(features)
        raw_state = state["raw_state"]
        if raw_state == "UNKNOWN":
            confirmed = "UNKNOWN"
        elif state["extreme_risk_off"]:
            confirmed = "RISK_OFF"
        elif raw_state == previous_raw:
            confirmed = raw_state
        features.update(state)
        features["confirmed_state"] = confirmed
        features["research_exposure_hypothesis_pct"] = exposures[confirmed]
        features["applied_to_live_positions"] = False
        row["features"] = features
        for name in RAW_FEATURES:
            history[name].append(raw[name])
        previous_raw = raw_state


def build_epoch_v2() -> dict[str, Any]:
    if _sha256(V1_PATH) != V1_SHA256 or _sha256(CORRECTION_PATH) != CORRECTION_SHA256:
        raise RuntimeError("V1原始特征或纠正声明哈希漂移")
    v1 = json.loads(V1_PATH.read_text(encoding="utf-8"))
    rows = [
        {
            "trade_date": row["trade_date"],
            "B": row["B"], "L": row["L"], "F": row["F"], "C": row["C"],
        }
        for row in v1["rows"]
    ]
    _apply_calendar_features(rows)
    return {
        "schema": SCHEMA,
        "scope": v1["scope"],
        "date_range": v1["date_range"],
        "input_bindings": {
            "v1_same_day_raw_feature_epoch": {
                "path": str(V1_PATH.relative_to(PROJECT_ROOT)),
                "sha256": V1_SHA256,
                "reused_fields": ["trade_date", "B", "L", "F", "C"],
                "ignored_fields": ["features"],
            },
            "v1_correction": {
                "path": str(CORRECTION_PATH.relative_to(PROJECT_ROOT)),
                "sha256": CORRECTION_SHA256,
            },
        },
        "percentile_policy": {
            "window_market_sessions": 504,
            "minimum_valid_observations_inside_window": 252,
            "time_direction": "strictly_before_current_session",
            "null_sessions_retained_in_window": True,
            "tie_method": "midrank=(count_less+0.5*count_equal)/valid_count_inside_window",
            "missing_policy": "null; no fill, substitution or window extension",
        },
        "confirmation_policy": {
            "ordinary_transition": "new raw state requires two consecutive sessions; during the first session the prior confirmed state remains",
            "unknown": "UNKNOWN is immediate and fail-closed",
            "extreme_risk_off": "immediate when F_stress>=0.95 or directional_strength<=0.10",
        },
        "decision_boundaries": {
            "research_diagnostic_only": True,
            "contains_strategy_returns": False,
            "historical_v8_exact_baseline_available": False,
            "changes_unified_stock_score": False,
            "applied_to_live_positions": False,
            "production": False,
            "automatic_trading": False,
        },
        "rows": rows,
    }


def publish_epoch(epoch: dict[str, Any]) -> tuple[Path, bool]:
    dates = epoch["date_range"]
    path = OUTPUT_DIR / f"{dates['start']}_{dates['end']}.json"
    return path, _atomic_publish(path, _canonical_bytes(epoch))


def main() -> int:
    argparse.ArgumentParser(description="纠正市场状态交易日滚动窗口").parse_args()
    epoch = build_epoch_v2()
    path, created = publish_epoch(epoch)
    print(json.dumps({
        "path": str(path), "created": created,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "date_range": epoch["date_range"],
        "raw_market_files_rescanned": False,
        "contains_strategy_returns": False,
        "applied_to_live_positions": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
