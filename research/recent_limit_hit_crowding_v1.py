"""Single frozen test of recent upper-limit-hit crowding."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantqc.ashare import opening_auction_executability
from research.limit_up_excluded_momentum_v1 import _mean, _net_trade_return, _sha, _write_json


PROTOCOL_PATH = ROOT / "research/RECENT_LIMIT_HIT_CROWDING_V1_PROTOCOL.json"
IMPLEMENTATION_PATH = ROOT / "research/recent_limit_hit_crowding_v1.py"
ADMISSION_PATH = ROOT / "research/RECENT_LIMIT_HIT_CROWDING_V1_RUN_ADMISSION.json"
RESULT_PATH = ROOT / "research/RECENT_LIMIT_HIT_CROWDING_V1_BLIND_RESULT.json"
ARCHIVE_PATH = ROOT / "research/RECENT_LIMIT_HIT_CROWDING_V1_TERMINAL_ARCHIVE.json"
EVIDENCE_DIR = ROOT / "research/data/recent_limit_hit_crowding_v1"
PERIOD_PATH = EVIDENCE_DIR / "weekly_metrics.csv"
SELECTED_PATH = EVIDENCE_DIR / "selected_quintiles.csv"
SH_TZ = ZoneInfo("Asia/Shanghai")


def _load_protocol() -> dict[str, Any]:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    if protocol.get("status") != "formula_frozen_before_future_return_read":
        raise RuntimeError("factor protocol is not frozen")
    for name, ref in protocol["bindings"].items():
        path = ROOT / ref["path"]
        if not path.is_file() or _sha(path) != ref["sha256"]:
            raise RuntimeError(f"protocol binding invalid: {name}")
    return protocol


def _load_daily(protocol: dict[str, Any]) -> pd.DataFrame:
    path = ROOT / protocol["bindings"]["daily_stock_features"]["path"]
    columns = [
        "code", "trade_date", "open", "close", "prev_close", "median_amount20",
        "float_mv", "board", "is_st", "trade_status", "adj_factor",
        "adjustment_trusted", "factor_changed", "history_sessions", "limit_touched",
    ]
    frame = pd.read_parquet(path, columns=columns)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.strftime("%Y-%m-%d")
    frame["code"] = frame["code"].astype(str).str.zfill(6)
    return frame.sort_values(["code", "trade_date"], kind="mergesort").reset_index(drop=True)


def add_crowding_factor(frame: pd.DataFrame) -> pd.DataFrame:
    ordered = frame.sort_values(["code", "trade_date"], kind="mergesort").copy()
    hits = ordered["limit_touched"].fillna(False).astype(int)
    ordered["limit_hit_count_20"] = hits.groupby(ordered.code, sort=False).transform(
        lambda values: values.rolling(20, min_periods=20).sum()
    )
    ordered["negative_limit_hit_count_20_v1"] = -ordered["limit_hit_count_20"]
    return ordered


def execution_outcome(
    frame_by_key: pd.DataFrame, sessions: list[str], signal_index: int, code: str,
) -> dict[str, Any]:
    entry_index = signal_index + 1
    target_exit_index = entry_index + 5
    if target_exit_index >= len(sessions):
        return {"status": "unresolved_out_of_range"}
    entry_day = sessions[entry_index]
    try:
        entry = frame_by_key.loc[(entry_day, code)]
        if isinstance(entry, pd.DataFrame):
            entry = entry.iloc[0]
    except KeyError:
        return {"status": "cash_missing_entry", "entry_day": entry_day}
    gate = opening_auction_executability(
        code=code, trade_date=entry_day, side="buy", open_price=entry.open,
        prev_close=entry.prev_close, tradable=int(entry.trade_status) == 1,
        is_risk_warning=bool(entry.is_st), no_price_limit=False,
    )
    if gate.get("executable") is not True or not bool(entry.adjustment_trusted):
        return {"status": "cash_entry_not_executable", "entry_day": entry_day, "reason": gate.get("reason") or "adjustment_untrusted"}
    entry_adjusted = float(entry.open) * float(entry.adj_factor)
    target_day = sessions[target_exit_index]
    for day in sessions[target_exit_index:target_exit_index + 6]:
        try:
            exit_row = frame_by_key.loc[(day, code)]
            if isinstance(exit_row, pd.DataFrame):
                exit_row = exit_row.iloc[0]
        except KeyError:
            continue
        gate = opening_auction_executability(
            code=code, trade_date=day, side="sell", open_price=exit_row.open,
            prev_close=exit_row.prev_close, tradable=int(exit_row.trade_status) == 1,
            is_risk_warning=bool(exit_row.is_st), no_price_limit=False,
        )
        if gate.get("executable") is not True or not bool(exit_row.adjustment_trusted):
            continue
        shares = int(10000 / float(entry.open) / 100) * 100
        if shares <= 0:
            return {"status": "cash_below_one_lot", "entry_day": entry_day}
        exit_adjusted = float(exit_row.open) * float(exit_row.adj_factor)
        return {
            "status": "filled" if day == target_day else "filled_delayed_exit",
            "entry_day": entry_day, "target_exit_day": target_day, "exit_day": day,
            "shares": shares,
            **{f"net_return_{bps}bp": _net_trade_return(entry_adjusted, exit_adjusted, shares, bps) for bps in (10, 20, 30)},
        }
    return {"status": "unresolved_exit", "entry_day": entry_day, "target_exit_day": target_day}


def _fold_metrics(periods: pd.DataFrame) -> dict[str, Any]:
    return {
        "periods": int(len(periods)),
        "mean_rank_ic": _mean(periods["rank_ic"]),
        "rank_ic_positive_period_rate": _mean(periods["rank_ic"] > 0),
        **{
            f"mean_{name}_{bps}bp": _mean(periods[f"{name}_{bps}bp"])
            for name in ("top", "bottom", "top_minus_bottom", "universe")
            for bps in (10, 20, 30)
        },
        "mean_top_minus_universe_20bp": _mean(periods["top_20bp"] - periods["universe_20bp"]),
    }


def _candidate_rows(frame: pd.DataFrame, protocol: dict[str, Any]) -> tuple[pd.DataFrame, list[str]]:
    sessions = sorted(frame.trade_date.unique())
    session_index = {day: index for index, day in enumerate(sessions)}
    date_range = protocol["evaluation"]["date_range"]
    eligible_sessions = [day for day in sessions if date_range[0] <= day <= date_range[1]]
    calendar = pd.to_datetime(pd.Series(eligible_sessions))
    week_keys = calendar.dt.strftime("%G-W%V")
    week_ends = pd.Series(eligible_sessions).groupby(week_keys).max().tolist()
    spec = protocol["universe"]
    candidates = frame[
        frame.trade_date.isin(week_ends)
        & frame.board.isin(spec["eligible_boards"])
        & (frame.is_st == spec["is_st"])
        & (frame.trade_status == spec["trade_status"])
        & (frame.adjustment_trusted == spec["adjustment_trusted"])
        & (frame.factor_changed == spec["factor_changed_on_signal_day"])
        & (frame.history_sessions >= spec["minimum_history_sessions"])
        & (frame.close >= spec["signal_close_yuan_min"])
        & (frame.median_amount20 >= spec["median_amount_20_yuan_min"])
        & frame.float_mv.between(*spec["float_market_cap_yuan_range_inclusive"])
        & (frame.limit_hit_count_20 >= 1)
    ].copy()
    candidates["signal_index"] = candidates.trade_date.map(session_index)
    return candidates, sessions


def admit() -> dict[str, Any]:
    if RESULT_PATH.exists():
        raise RuntimeError("factor test already consumed")
    protocol = _load_protocol()
    admission = {
        "schema": "recent_limit_hit_crowding_run_admission.v1",
        "created_at": datetime.now(SH_TZ).isoformat(timespec="seconds"),
        "status": "admitted_for_single_blind_factor_test",
        "bindings": {
            "protocol": {"path": str(PROTOCOL_PATH.relative_to(ROOT)), "sha256": _sha(PROTOCOL_PATH)},
            "implementation": {"path": str(IMPLEMENTATION_PATH.relative_to(ROOT)), "sha256": _sha(IMPLEMENTATION_PATH)},
            **protocol["bindings"],
        },
        "future_return_data_read": False, "single_test_authorized": True,
        "generates_buy_or_sell_alert": False,
    }
    _write_json(ADMISSION_PATH, admission)
    return admission


def run() -> dict[str, Any]:
    if RESULT_PATH.exists():
        raise RuntimeError("blind factor result already exists; rerun forbidden")
    admission = json.loads(ADMISSION_PATH.read_text(encoding="utf-8"))
    for name, ref in admission["bindings"].items():
        path = ROOT / ref["path"]
        if not path.is_file() or _sha(path) != ref["sha256"]:
            raise RuntimeError(f"run admission binding invalid: {name}")
    protocol = _load_protocol()
    frame = add_crowding_factor(_load_daily(protocol))
    candidates, sessions = _candidate_rows(frame, protocol)
    by_key = frame.set_index(["trade_date", "code"], drop=False)
    rows: list[dict[str, Any]] = []
    for row in candidates.itertuples(index=False):
        rows.append({
            "period": pd.Timestamp(row.trade_date).strftime("%G-W%V"),
            "signal_day": row.trade_date, "code": row.code,
            "factor": float(row.negative_limit_hit_count_20_v1),
            "limit_hit_count_20": int(row.limit_hit_count_20),
            **execution_outcome(by_key, sessions, int(row.signal_index), row.code),
        })
    outcomes = pd.DataFrame(rows)
    for bps in (10, 20, 30):
        outcomes.loc[outcomes.status.str.startswith("cash_"), f"net_return_{bps}bp"] = 0.0

    period_rows: list[dict[str, Any]] = []
    selected_rows: list[pd.DataFrame] = []
    for period, group in outcomes.groupby("period", sort=True):
        complete = group[group.net_return_10bp.notna()].copy()
        if len(complete) < 20 or complete.factor.nunique() < 2:
            continue
        count = max(1, math.ceil(len(complete) * 0.20))
        ordered = complete.sort_values(["factor", "code"], ascending=[False, True], kind="mergesort")
        top, bottom = ordered.head(count).copy(), ordered.tail(count).copy()
        top["quintile"], bottom["quintile"] = "top", "bottom"
        selected_rows.extend([top, bottom])
        record: dict[str, Any] = {
            "period": period, "signal_day": str(group.signal_day.iloc[0]),
            "candidates": int(len(group)), "complete": int(len(complete)),
            "unresolved_all": int(group.status.str.startswith("unresolved").sum()),
            "rank_ic": float(complete.factor.corr(complete.net_return_10bp, method="spearman")),
        }
        for bps in (10, 20, 30):
            column = f"net_return_{bps}bp"
            record[f"top_{bps}bp"] = float(top[column].mean())
            record[f"bottom_{bps}bp"] = float(bottom[column].mean())
            record[f"top_minus_bottom_{bps}bp"] = record[f"top_{bps}bp"] - record[f"bottom_{bps}bp"]
            record[f"universe_{bps}bp"] = float(complete[column].mean())
        period_rows.append(record)
    periods = pd.DataFrame(period_rows)
    selected = pd.concat(selected_rows, ignore_index=True) if selected_rows else pd.DataFrame()
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    periods.to_csv(PERIOD_PATH, index=False)
    selected.to_csv(SELECTED_PATH, index=False)

    ranges = {
        "development": protocol["evaluation"]["development_diagnostic_no_tuning"],
        "validation": protocol["evaluation"]["validation"],
        "sealed_test": protocol["evaluation"]["sealed_test"],
    }
    folds = {
        name: _fold_metrics(periods[periods.signal_day.between(start, end)])
        for name, (start, end) in ranges.items()
    }
    validation, test = folds["validation"], folds["sealed_test"]
    unresolved_selected = int(selected.status.str.startswith("unresolved").sum()) if not selected.empty else 0
    gates = {
        "validation_periods_at_least_80": validation["periods"] >= 80,
        "sealed_test_periods_at_least_40": test["periods"] >= 40,
        "validation_mean_rank_ic_positive": (validation["mean_rank_ic"] or -1) > 0,
        "sealed_test_mean_rank_ic_positive": (test["mean_rank_ic"] or -1) > 0,
        "sealed_test_rank_ic_positive_period_rate_at_least_0_5": (test["rank_ic_positive_period_rate"] or 0) >= 0.5,
        "validation_top_minus_bottom_10bp_positive": (validation["mean_top_minus_bottom_10bp"] or -1) > 0,
        "sealed_test_top_minus_bottom_10bp_positive": (test["mean_top_minus_bottom_10bp"] or -1) > 0,
        "sealed_test_top_minus_bottom_20bp_positive": (test["mean_top_minus_bottom_20bp"] or -1) > 0,
        "sealed_test_top_20bp_beats_universe": (test["mean_top_minus_universe_20bp"] or -1) > 0,
        "all_selected_entries_and_exits_resolved": unresolved_selected == 0,
    }
    passed = all(gates.values())
    result = {
        "schema": "recent_limit_hit_crowding_blind_result.v1",
        "created_at": datetime.now(SH_TZ).isoformat(timespec="seconds"),
        "status": "historical_gate_passed_forward_required" if passed else "terminal_historical_gate_failure_no_retune",
        "candidate_id": protocol["candidate_id"], "factor_id": protocol["factor_id"],
        "admission": {"path": str(ADMISSION_PATH.relative_to(ROOT)), "sha256": _sha(ADMISSION_PATH)},
        "candidate_stock_periods": int(len(outcomes)), "periods": int(len(periods)),
        "execution_status_counts": {str(k): int(v) for k, v in outcomes.status.value_counts().items()},
        "selected_rows": int(len(selected)), "unresolved_selected_rows": unresolved_selected,
        "fold_metrics": folds, "gates": gates,
        "failed_gates": [name for name, value in gates.items() if not value],
        "historical_factor_gate_passed": passed, "strict_forward_required": passed,
        "production_factor": False, "factor_blocks_non_factor_routes": False,
        "generates_buy_or_sell_alert": False,
        "period_evidence": {"path": str(PERIOD_PATH.relative_to(ROOT)), "sha256": _sha(PERIOD_PATH)},
        "selected_evidence": {"path": str(SELECTED_PATH.relative_to(ROOT)), "sha256": _sha(SELECTED_PATH)},
    }
    _write_json(RESULT_PATH, result)
    if not passed:
        _write_json(ARCHIVE_PATH, {
            "schema": "recent_limit_hit_crowding_terminal_archive.v1",
            "status": "permanently_archived_no_retune", "candidate_id": protocol["candidate_id"],
            "result": {"path": str(RESULT_PATH.relative_to(ROOT)), "sha256": _sha(RESULT_PATH)},
            "failed_gates": result["failed_gates"],
            "forbidden": ["sign_flip", "lookback_tuning", "horizon_tuning", "fold_removal"],
        })
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("admit", "run"))
    output = admit() if parser.parse_args().command == "admit" else run()
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
