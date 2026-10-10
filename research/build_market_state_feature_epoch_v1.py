"""Build the frozen, input-only B/L/F/C market-state feature epoch.

The builder is offline and deterministic.  It never reads strategy returns,
orders, fills, holdings, or account cash.  Its output is research evidence and
cannot change the live stock score or position budget.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import tempfile
from bisect import bisect_left, bisect_right
from collections import deque
from pathlib import Path
from statistics import fmean
from typing import Any

import numpy as np

from mom_trading.market_state_daily import (
    DATA_ROOT,
    PROJECT_ROOT,
    _atomic_publish,
    _canonical_bytes,
    _limit_flags,
    _load_reference_maps,
    _number,
    _pct,
    _sha256,
)


SCHEMA = "market_state_feature_epoch.v1"
OUTPUT_DIR = DATA_ROOT / "feature_epochs"
MIN_HISTORY = 252
ROLLING_WINDOW = 504
RAW_FEATURES = (
    "up_ratio_pct",
    "above_ma20_ratio_pct",
    "new_high_minus_low_ratio_pct",
    "yesterday_limit_continuation_ratio_pct",
    "highest_board",
    "closed_limit_ratio_pct",
    "eod_failed_limit_ratio_pct",
    "top_5pct_amount_share_pct",
)


class _LaggedWindow:
    def __init__(self, *, size: int = ROLLING_WINDOW) -> None:
        self._values: deque[float] = deque()
        self._sorted: list[float] = []
        self._size = size

    def percentile_before_append(self, value: float | None) -> float | None:
        result = None
        if value is not None and len(self._values) >= MIN_HISTORY:
            left = bisect_left(self._sorted, value)
            right = bisect_right(self._sorted, value)
            result = round((left + 0.5 * (right - left)) / len(self._values), 6)
        if value is not None:
            if len(self._values) == self._size:
                expired = self._values.popleft()
                self._sorted.pop(bisect_left(self._sorted, expired))
            self._values.append(value)
            self._sorted.insert(bisect_right(self._sorted, value), value)
        return result


def _finite(values: np.ndarray) -> np.ndarray:
    return values[np.isfinite(values)]


def _rounded(value: float | None, digits: int = 4) -> float | None:
    return round(float(value), digits) if value is not None and math.isfinite(value) else None


def _raw_feature_values(row: dict[str, Any]) -> dict[str, float | None]:
    breadth = row["B"]
    ladder = row["L"]
    failed = row["F"]
    crowding = row["C"]
    return {
        "up_ratio_pct": breadth["up_ratio_pct"],
        "above_ma20_ratio_pct": breadth["above_ma20_ratio_pct"],
        "new_high_minus_low_ratio_pct": breadth["new_high_minus_low_ratio_pct"],
        "yesterday_limit_continuation_ratio_pct": ladder[
            "yesterday_limit_continuation_ratio_pct"
        ],
        "highest_board": float(ladder["highest_board"]),
        "closed_limit_ratio_pct": ladder["closed_limit_ratio_pct"],
        "eod_failed_limit_ratio_pct": failed["eod_failed_limit_ratio_pct"],
        "top_5pct_amount_share_pct": crowding["top_5pct_amount_share_pct"],
    }


def _raw_state(features: dict[str, float | None]) -> dict[str, Any]:
    p = features
    required = [p.get(f"{name}_percentile") for name in RAW_FEATURES]
    if any(value is None for value in required):
        return {
            "B_strength": None, "L_strength": None, "F_stress": None,
            "C_stress": None, "directional_strength": None,
            "raw_state": "UNKNOWN", "extreme_risk_off": False,
        }
    b_strength = fmean([
        p["up_ratio_pct_percentile"],
        p["above_ma20_ratio_pct_percentile"],
        p["new_high_minus_low_ratio_pct_percentile"],
    ])
    l_strength = fmean([
        p["yesterday_limit_continuation_ratio_pct_percentile"],
        p["highest_board_percentile"],
        p["closed_limit_ratio_pct_percentile"],
    ])
    f_stress = p["eod_failed_limit_ratio_pct_percentile"]
    c_stress = p["top_5pct_amount_share_pct_percentile"]
    directional = fmean([b_strength, l_strength, 1.0 - f_stress])
    extreme = f_stress >= 0.95 or directional <= 0.10
    if f_stress >= 0.90 or directional <= 0.33:
        state = "RISK_OFF"
    elif directional >= 0.67 and c_stress >= 0.90:
        state = "OVERHEATED"
    elif directional >= 0.67:
        state = "RISK_ON"
    else:
        state = "STABILIZING"
    return {
        "B_strength": round(b_strength, 6),
        "L_strength": round(l_strength, 6),
        "F_stress": round(f_stress, 6),
        "C_stress": round(c_stress, 6),
        "directional_strength": round(directional, 6),
        "raw_state": state,
        "extreme_risk_off": extreme,
    }


def _apply_lagged_features(rows: list[dict[str, Any]]) -> None:
    windows = {name: _LaggedWindow() for name in RAW_FEATURES}
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
            features[f"{name}_percentile"] = windows[name].percentile_before_append(raw[name])
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
        previous_raw = raw_state


def _load_bound_inputs(project_root: Path) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    snapshot_path = DATA_ROOT / "snapshots" / "2026-08-28.json"
    snapshot_raw = snapshot_path.read_bytes()
    snapshot = json.loads(snapshot_raw)
    manifest_ref = snapshot["evidence"]["v4_input_manifest"]
    manifest_path = project_root / manifest_ref["path"]
    if _sha256(manifest_path) != manifest_ref["sha256"]:
        raise RuntimeError("冻结的V4输入清单哈希不匹配")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    calendar_path = project_root / "reports" / "v4_market_calendar.json"
    calendar = json.loads(calendar_path.read_text(encoding="utf-8"))["trading_dates"]
    if manifest["trade_date"] != calendar[-1] or snapshot["trade_date"] != calendar[-1]:
        raise RuntimeError("输入清单、快照与交易日历结束日期不一致")
    bindings = {
        "daily_snapshot": {
            "path": str(snapshot_path.relative_to(project_root)),
            "sha256": _sha256(snapshot_path),
        },
        "v4_input_manifest": {
            "path": str(manifest_path.relative_to(project_root)),
            "sha256": manifest_ref["sha256"],
        },
        "calendar": {
            "path": "reports/v4_market_calendar.json",
            "sha256": _sha256(calendar_path),
        },
        "quality_audit": {
            "path": "reports/v4_daily_quality_audit.json",
            "sha256": _sha256(project_root / "reports" / "v4_daily_quality_audit.json"),
        },
        "stock_master": {
            "path": "research/data/stock_basic_baostock.csv",
            "sha256": _sha256(project_root / "research/data/stock_basic_baostock.csv"),
        },
        "special_events": {
            "path": "research/data/special_price_limit_events.csv",
            "sha256": _sha256(project_root / "research/data/special_price_limit_events.csv"),
        },
        "market_rules": {
            "path": "research/market_rules.py",
            "sha256": _sha256(project_root / "research/market_rules.py"),
        },
    }
    return manifest, [str(day) for day in calendar], bindings


def build_epoch(*, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    if project_root.resolve() != PROJECT_ROOT.resolve():
        raise ValueError("当前构建器只接受实际项目根")
    manifest, calendar, bindings = _load_bound_inputs(project_root)
    if len(manifest.get("files") or []) != 4834:
        raise RuntimeError("V4输入清单不是冻结的4834文件全集")
    date_index = {day: index for index, day in enumerate(calendar)}
    start_index = 5
    size = len(calendar)
    scalar_names = (
        "up", "down", "flat", "st", "ma20_total", "above_ma20",
        "new_high", "new_low", "first", "second", "third", "four_plus",
        "highest", "continuation_denominator", "continuation_numerator",
        "touch", "closed", "failed", "closed_amount",
    )
    scalars = {name: np.zeros(size, dtype=np.float64) for name in scalar_names}
    master = project_root / "research/data/stock_basic_baostock.csv"
    events = project_root / "research/data/special_price_limit_events.csv"
    ipo_by_code, special_events = _load_reference_maps(master, events)

    with tempfile.TemporaryDirectory(prefix="market-state-epoch-") as temporary:
        temp = Path(temporary)
        shape = (size, len(manifest["files"]))
        returns = np.memmap(temp / "returns.dat", dtype="float64", mode="w+", shape=shape)
        amounts = np.memmap(temp / "amounts.dat", dtype="float64", mode="w+", shape=shape)
        turnovers = np.memmap(temp / "turnovers.dat", dtype="float64", mode="w+", shape=shape)
        returns[:] = np.nan
        amounts[:] = np.nan
        turnovers[:] = np.nan

        for stock_index, item in enumerate(manifest["files"]):
            path = project_root / item["path"]
            if not path.is_file() or path.is_symlink() or _sha256(path) != item["sha256"]:
                raise RuntimeError(f"V4输入文件缺失或哈希漂移：{item['path']}")
            close_window: deque[float] = deque(maxlen=20)
            previous_index: int | None = None
            previous_closed = False
            previous_streak = 0
            with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as stream:
                for row in csv.DictReader(stream):
                    day = str(row.get("date") or "")
                    index = date_index.get(day)
                    if index is None:
                        raise RuntimeError(f"交易日不在冻结日历：{item['path']}:{day}")
                    code = str(row.get("code") or "")
                    active = str(row.get("tradestatus")) == "1"
                    close = _number(row.get("close"))
                    change = _number(row.get("pctChg"))
                    amount = _number(row.get("amount"))
                    turnover = _number(row.get("turn"))
                    if active and change is not None:
                        returns[index, stock_index] = change
                        scalars["up"][index] += change > 0
                        scalars["down"][index] += change < 0
                        scalars["flat"][index] += change == 0
                    if active and str(row.get("isST")) == "1":
                        scalars["st"][index] += 1
                    if active and close is not None:
                        close_window.append(close)
                        if len(close_window) == 20:
                            scalars["ma20_total"][index] += 1
                            mean_close = sum(close_window) / 20
                            scalars["above_ma20"][index] += close > mean_close
                            scalars["new_high"][index] += close >= max(close_window)
                            scalars["new_low"][index] += close <= min(close_window)
                    if active and amount is not None and amount >= 0:
                        amounts[index, stock_index] = amount
                    if active and turnover is not None and turnover >= 0:
                        turnovers[index, stock_index] = turnover

                    flags = _limit_flags(
                        code, row, ipo_day=ipo_by_code.get(code, ""),
                        calendar=calendar, special_events=special_events,
                    )
                    consecutive = previous_index is not None and index == previous_index + 1
                    if flags["evaluable"]:
                        scalars["touch"][index] += flags["touched"]
                        scalars["closed"][index] += flags["closed"]
                        scalars["failed"][index] += flags["touched"] and not flags["closed"]
                        if flags["closed"] and amount is not None:
                            scalars["closed_amount"][index] += amount
                        streak = previous_streak + 1 if flags["closed"] and consecutive and previous_closed else int(flags["closed"])
                        if streak == 1:
                            scalars["first"][index] += 1
                        elif streak == 2:
                            scalars["second"][index] += 1
                        elif streak == 3:
                            scalars["third"][index] += 1
                        elif streak >= 4:
                            scalars["four_plus"][index] += 1
                        scalars["highest"][index] = max(scalars["highest"][index], streak)
                        if consecutive and previous_closed:
                            scalars["continuation_denominator"][index] += 1
                            scalars["continuation_numerator"][index] += flags["closed"]
                    else:
                        streak = 0
                    previous_index = index
                    previous_closed = bool(flags["closed"])
                    previous_streak = streak

        rows: list[dict[str, Any]] = []
        for index in range(start_index, size):
            day_returns = _finite(returns[index])
            day_amounts = _finite(amounts[index])
            day_turnovers = _finite(turnovers[index])
            usable = int(scalars["up"][index] + scalars["down"][index] + scalars["flat"][index])
            total_amount = float(np.sum(day_amounts)) if day_amounts.size else 0.0
            sorted_amounts = np.sort(day_amounts)[::-1]
            top_count = max(1, math.ceil(day_amounts.size * 0.05)) if day_amounts.size else 0
            sorted_turnovers = np.sort(day_turnovers)
            p95_index = max(0, math.ceil(day_turnovers.size * 0.95) - 1) if day_turnovers.size else 0
            ma20_total = int(scalars["ma20_total"][index])
            continuation_denominator = int(scalars["continuation_denominator"][index])
            touch = int(scalars["touch"][index])
            closed = int(scalars["closed"][index])
            row = {
                "trade_date": calendar[index],
                "B": {
                    "usable": usable,
                    "up_count": int(scalars["up"][index]),
                    "down_count": int(scalars["down"][index]),
                    "flat_count": int(scalars["flat"][index]),
                    "up_ratio_pct": _pct(scalars["up"][index], usable),
                    "median_return_pct": _rounded(float(np.median(day_returns)) if day_returns.size else None),
                    "ma20_usable": ma20_total,
                    "above_ma20_count": int(scalars["above_ma20"][index]),
                    "above_ma20_ratio_pct": _pct(scalars["above_ma20"][index], ma20_total),
                    "new_high_20d_count": int(scalars["new_high"][index]),
                    "new_low_20d_count": int(scalars["new_low"][index]),
                    "new_high_minus_low_ratio_pct": _pct(
                        scalars["new_high"][index] - scalars["new_low"][index], usable,
                    ),
                    "st_count": int(scalars["st"][index]),
                },
                "L": {
                    "first_board_count": int(scalars["first"][index]),
                    "second_board_count": int(scalars["second"][index]),
                    "third_board_count": int(scalars["third"][index]),
                    "four_plus_board_count": int(scalars["four_plus"][index]),
                    "highest_board": int(scalars["highest"][index]),
                    "yesterday_limit_continuation_denominator": continuation_denominator,
                    "yesterday_limit_continuation_numerator": int(scalars["continuation_numerator"][index]),
                    "yesterday_limit_continuation_ratio_pct": _pct(
                        scalars["continuation_numerator"][index], continuation_denominator,
                    ),
                    "closed_limit_ratio_pct": _pct(closed, usable),
                },
                "F": {
                    "limit_touch_count": touch,
                    "closed_limit_count": closed,
                    "eod_failed_limit_count": int(scalars["failed"][index]),
                    "eod_failed_limit_ratio_pct": _pct(scalars["failed"][index], touch),
                },
                "C": {
                    "total_amount_yi": round(total_amount / 100_000_000, 3),
                    "top_5pct_amount_share_pct": _pct(float(np.sum(sorted_amounts[:top_count])), total_amount),
                    "turnover_median_pct": _rounded(float(np.median(day_turnovers)) if day_turnovers.size else None),
                    "turnover_p95_pct": _rounded(float(sorted_turnovers[p95_index]) if day_turnovers.size else None),
                    "limit_stock_amount_share_pct": _pct(scalars["closed_amount"][index], total_amount),
                    "cross_section_return_dispersion_pct": _rounded(float(np.std(day_returns)) if day_returns.size > 1 else None),
                },
            }
            rows.append(row)
        del returns, amounts, turnovers

    _apply_lagged_features(rows)
    return {
        "schema": SCHEMA,
        "scope": {
            "id": "sh_sz_main_chinext", "scope_complete": True,
            "full_a_share": False, "missing_segments": ["star", "bse"],
            "frequency": "completed_daily_bar_only",
        },
        "date_range": {"start": rows[0]["trade_date"], "end": rows[-1]["trade_date"], "sessions": len(rows)},
        "input_bindings": bindings,
        "percentile_policy": {
            "window_sessions": ROLLING_WINDOW,
            "minimum_prior_valid_sessions": MIN_HISTORY,
            "time_direction": "strictly_before_current_session",
            "tie_method": "midrank=(count_less+0.5*count_equal)/prior_valid_count",
            "missing_policy": "null; no fill or substitution",
        },
        "decision_boundaries": {
            "research_only": True,
            "contains_strategy_returns": False,
            "changes_unified_stock_score": False,
            "applied_to_live_positions": False,
            "production": False,
            "automatic_trading": False,
        },
        "rows": rows,
    }


def publish_epoch(epoch: dict[str, Any]) -> tuple[Path, bool]:
    date_range = epoch["date_range"]
    path = OUTPUT_DIR / f"{date_range['start']}_{date_range['end']}.json"
    return path, _atomic_publish(path, _canonical_bytes(epoch))


def main() -> int:
    parser = argparse.ArgumentParser(description="构建匿名市场状态历史特征层")
    parser.add_argument("command", choices=("publish",))
    args = parser.parse_args()
    if args.command == "publish":
        epoch = build_epoch()
        path, created = publish_epoch(epoch)
        print(json.dumps({
            "path": str(path), "created": created,
            "sha256": _sha256(path), "date_range": epoch["date_range"],
            "contains_strategy_returns": False,
            "applied_to_live_positions": False,
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
