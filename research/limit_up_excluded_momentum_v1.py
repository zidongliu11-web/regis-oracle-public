"""Single frozen test of momentum with upper-limit returns removed."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantqc.ashare import opening_auction_executability
from quantqc.costs import one_side_cost_breakdown


PROTOCOL_PATH = ROOT / "research/LIMIT_UP_EXCLUDED_MOMENTUM_V1_PROTOCOL.json"
IMPLEMENTATION_PATH = ROOT / "research/limit_up_excluded_momentum_v1.py"
ADMISSION_PATH = ROOT / "research/LIMIT_UP_EXCLUDED_MOMENTUM_V1_RUN_ADMISSION.json"
RESULT_PATH = ROOT / "research/LIMIT_UP_EXCLUDED_MOMENTUM_V1_BLIND_RESULT.json"
ARCHIVE_PATH = ROOT / "research/LIMIT_UP_EXCLUDED_MOMENTUM_V1_TERMINAL_ARCHIVE.json"
EVIDENCE_DIR = ROOT / "research/data/limit_up_excluded_momentum_v1"
MONTHLY_PATH = EVIDENCE_DIR / "monthly_metrics.csv"
SELECTED_PATH = EVIDENCE_DIR / "selected_quintiles.csv"
SH_TZ = ZoneInfo("Asia/Shanghai")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_bytes(_canonical(value))
    os.replace(temp, path)


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
        "code", "trade_date", "open", "close", "prev_close", "amount",
        "median_amount20", "float_mv", "board", "is_st", "trade_status",
        "adj_factor", "adj_close", "adjustment_trusted", "factor_changed",
        "history_sessions", "limit_evaluable", "limit_closed",
    ]
    frame = pd.read_parquet(path, columns=columns)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.strftime("%Y-%m-%d")
    frame["code"] = frame["code"].astype(str).str.zfill(6)
    return frame.sort_values(["code", "trade_date"], kind="mergesort").reset_index(drop=True)


def add_frozen_factor_values(frame: pd.DataFrame) -> pd.DataFrame:
    """Use only current/past rows; this function never reads a future return."""
    ordered = frame.sort_values(["code", "trade_date"], kind="mergesort").copy()
    groups = ordered.groupby("code", sort=False)
    previous_adjusted_close = groups["adj_close"].shift(1)
    daily_log_return = np.log(ordered["adj_close"] / previous_adjusted_close)
    previous_limit = groups["limit_closed"].shift(1).fillna(False).astype(bool)
    excluded = ordered["limit_closed"].fillna(False).astype(bool) | previous_limit
    valid_return = daily_log_return.replace([np.inf, -np.inf], np.nan).notna()
    clean_return = daily_log_return.where(~excluded, 0.0).where(valid_return)
    raw_return = daily_log_return.where(valid_return)
    ordered["excluded_limit_return"] = excluded
    ordered["valid_factor_return"] = valid_return
    ordered["limit_up_excluded_momentum_252_20_v1"] = clean_return.groupby(
        ordered["code"], sort=False,
    ).transform(lambda values: values.shift(20).rolling(252, min_periods=230).sum())
    ordered["raw_momentum_252_20"] = raw_return.groupby(
        ordered["code"], sort=False,
    ).transform(lambda values: values.shift(20).rolling(252, min_periods=230).sum())
    return ordered


def _net_trade_return(entry: float, exit_: float, shares: int, bps: int) -> float:
    slip = bps / 10000
    buy_price, sell_price = entry * (1 + slip), exit_ * (1 - slip)
    buy_notional, sell_notional = buy_price * shares, sell_price * shares
    buy_cost = float(one_side_cost_breakdown(buy_notional, "buy", slippage=0)["total"])
    sell_cost = float(one_side_cost_breakdown(sell_notional, "sell", slippage=0)["total"])
    invested = buy_notional + buy_cost
    return (sell_notional - sell_cost - invested) / invested


def execution_outcome(
    frame_by_key: pd.DataFrame, sessions: list[str], signal_index: int, code: str,
) -> dict[str, Any]:
    entry_index = signal_index + 1
    target_exit_index = entry_index + 20
    if target_exit_index >= len(sessions):
        return {"status": "unresolved_out_of_range"}
    entry_day = sessions[entry_index]
    try:
        entry = frame_by_key.loc[(entry_day, code)]
        if isinstance(entry, pd.DataFrame):
            entry = entry.iloc[0]
    except KeyError:
        return {"status": "cash_missing_entry", "entry_day": entry_day}
    entry_gate = opening_auction_executability(
        code=code, trade_date=entry_day, side="buy", open_price=entry.open,
        prev_close=entry.prev_close, tradable=int(entry.trade_status) == 1,
        is_risk_warning=bool(entry.is_st), no_price_limit=False,
    )
    if entry_gate.get("executable") is not True or not bool(entry.adjustment_trusted):
        return {
            "status": "cash_entry_not_executable", "entry_day": entry_day,
            "reason": entry_gate.get("reason") or "adjustment_untrusted",
        }
    entry_adjusted = float(entry.open) * float(entry.adj_factor)
    target_day = sessions[target_exit_index]
    for day in sessions[target_exit_index:target_exit_index + 6]:
        try:
            exit_row = frame_by_key.loc[(day, code)]
            if isinstance(exit_row, pd.DataFrame):
                exit_row = exit_row.iloc[0]
        except KeyError:
            continue
        exit_gate = opening_auction_executability(
            code=code, trade_date=day, side="sell", open_price=exit_row.open,
            prev_close=exit_row.prev_close, tradable=int(exit_row.trade_status) == 1,
            is_risk_warning=bool(exit_row.is_st), no_price_limit=False,
        )
        if exit_gate.get("executable") is not True or not bool(exit_row.adjustment_trusted):
            continue
        exit_adjusted = float(exit_row.open) * float(exit_row.adj_factor)
        shares = int(10000 / float(entry.open) / 100) * 100
        if shares <= 0:
            return {"status": "cash_below_one_lot", "entry_day": entry_day}
        return {
            "status": "filled" if day == target_day else "filled_delayed_exit",
            "entry_day": entry_day, "target_exit_day": target_day, "exit_day": day,
            "shares": shares,
            **{f"net_return_{bps}bp": _net_trade_return(entry_adjusted, exit_adjusted, shares, bps) for bps in (10, 20, 30)},
        }
    return {"status": "unresolved_exit", "entry_day": entry_day, "target_exit_day": target_day}


def _mean(series: pd.Series) -> float | None:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    return None if clean.empty else float(clean.mean())


def _fold_metrics(monthly: pd.DataFrame) -> dict[str, Any]:
    return {
        "months": int(len(monthly)),
        "mean_rank_ic": _mean(monthly["rank_ic"]),
        "rank_ic_positive_month_rate": _mean(monthly["rank_ic"] > 0),
        "mean_raw_momentum_rank_ic": _mean(monthly["raw_rank_ic"]),
        **{
            f"mean_{name}_{bps}bp": _mean(monthly[f"{name}_{bps}bp"])
            for name in ("top", "bottom", "top_minus_bottom", "universe")
            for bps in (10, 20, 30)
        },
        "mean_top_minus_universe_20bp": _mean(monthly["top_20bp"] - monthly["universe_20bp"]),
    }


def _month_rows(frame: pd.DataFrame, protocol: dict[str, Any]) -> tuple[pd.DataFrame, list[str]]:
    sessions = sorted(frame.trade_date.unique())
    session_index = {day: index for index, day in enumerate(sessions)}
    date_range = protocol["evaluation"]["date_range"]
    scoped_sessions = [day for day in sessions if date_range[0] <= day <= date_range[1]]
    month_ends = pd.Series(scoped_sessions).groupby(pd.Series(scoped_sessions).str[:7]).max().tolist()
    spec = protocol["universe"]
    value_column = "limit_up_excluded_momentum_252_20_v1"
    candidates = frame[
        frame.trade_date.isin(month_ends)
        & frame.board.isin(spec["eligible_boards"])
        & (frame.is_st == spec["is_st"])
        & (frame.trade_status == spec["trade_status"])
        & (frame.adjustment_trusted == spec["adjustment_trusted"])
        & (frame.factor_changed == spec["factor_changed_on_signal_day"])
        & (frame.history_sessions >= spec["minimum_history_sessions"])
        & (frame.close >= spec["signal_close_yuan_min"])
        & (frame.median_amount20 >= spec["median_amount_20_yuan_min"])
        & frame.float_mv.between(*spec["float_market_cap_yuan_range_inclusive"])
        & frame[value_column].notna()
        & frame.raw_momentum_252_20.notna()
    ].copy()
    candidates["signal_index"] = candidates.trade_date.map(session_index)
    return candidates, sessions


def admit() -> dict[str, Any]:
    if RESULT_PATH.exists():
        raise RuntimeError("factor test already consumed")
    protocol = _load_protocol()
    bindings = {
        "protocol": {"path": str(PROTOCOL_PATH.relative_to(ROOT)), "sha256": _sha(PROTOCOL_PATH)},
        "implementation": {"path": str(IMPLEMENTATION_PATH.relative_to(ROOT)), "sha256": _sha(IMPLEMENTATION_PATH)},
        **protocol["bindings"],
    }
    admission = {
        "schema": "limit_up_excluded_momentum_run_admission.v1",
        "created_at": datetime.now(SH_TZ).isoformat(timespec="seconds"),
        "status": "admitted_for_single_blind_factor_test",
        "bindings": bindings,
        "future_return_data_read": False,
        "single_test_authorized": True,
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
    frame = add_frozen_factor_values(_load_daily(protocol))
    candidates, sessions = _month_rows(frame, protocol)
    by_key = frame.set_index(["trade_date", "code"], drop=False)
    outcome_rows: list[dict[str, Any]] = []
    for row in candidates.itertuples(index=False):
        outcome_rows.append({
            "month": row.trade_date[:7], "signal_day": row.trade_date, "code": row.code,
            "factor": float(row.limit_up_excluded_momentum_252_20_v1),
            "raw_momentum": float(row.raw_momentum_252_20),
            **execution_outcome(by_key, sessions, int(row.signal_index), row.code),
        })
    outcomes = pd.DataFrame(outcome_rows)
    for bps in (10, 20, 30):
        column = f"net_return_{bps}bp"
        outcomes.loc[outcomes.status.str.startswith("cash_"), column] = 0.0

    monthly_rows: list[dict[str, Any]] = []
    selected_rows: list[pd.DataFrame] = []
    for month, group in outcomes.groupby("month", sort=True):
        complete = group[group.net_return_10bp.notna()].copy()
        if len(complete) < 20:
            continue
        count = max(1, math.ceil(len(complete) * 0.20))
        ordered = complete.sort_values(["factor", "code"], ascending=[False, True], kind="mergesort")
        top, bottom = ordered.head(count).copy(), ordered.tail(count).copy()
        top["quintile"] = "top"
        bottom["quintile"] = "bottom"
        selected_rows.extend([top, bottom])
        record: dict[str, Any] = {
            "month": month, "candidates": int(len(group)), "complete": int(len(complete)),
            "unresolved_all": int(group.status.str.startswith("unresolved").sum()),
            "rank_ic": float(complete.factor.corr(complete.net_return_10bp, method="spearman")),
            "raw_rank_ic": float(complete.raw_momentum.corr(complete.net_return_10bp, method="spearman")),
        }
        for bps in (10, 20, 30):
            col = f"net_return_{bps}bp"
            record[f"top_{bps}bp"] = float(top[col].mean())
            record[f"bottom_{bps}bp"] = float(bottom[col].mean())
            record[f"top_minus_bottom_{bps}bp"] = record[f"top_{bps}bp"] - record[f"bottom_{bps}bp"]
            record[f"universe_{bps}bp"] = float(complete[col].mean())
        monthly_rows.append(record)
    monthly = pd.DataFrame(monthly_rows)
    selected = pd.concat(selected_rows, ignore_index=True) if selected_rows else pd.DataFrame()
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    monthly.to_csv(MONTHLY_PATH, index=False)
    selected.to_csv(SELECTED_PATH, index=False)

    ranges = {
        "development": protocol["evaluation"]["development_diagnostic_no_tuning"],
        "validation": protocol["evaluation"]["validation"],
        "sealed_test": protocol["evaluation"]["sealed_test"],
    }
    folds = {
        name: _fold_metrics(monthly[monthly.month.between(start[:7], end[:7])])
        for name, (start, end) in ranges.items()
    }
    validation, test = folds["validation"], folds["sealed_test"]
    unresolved_selected = int(selected.status.str.startswith("unresolved").sum()) if not selected.empty else 0
    gates = {
        "validation_months_at_least_20": validation["months"] >= 20,
        "sealed_test_months_at_least_9": test["months"] >= 9,
        "validation_mean_rank_ic_positive": (validation["mean_rank_ic"] or -1) > 0,
        "sealed_test_mean_rank_ic_positive": (test["mean_rank_ic"] or -1) > 0,
        "sealed_test_rank_ic_positive_month_rate_at_least_0_5": (test["rank_ic_positive_month_rate"] or 0) >= 0.5,
        "validation_top_minus_bottom_10bp_positive": (validation["mean_top_minus_bottom_10bp"] or -1) > 0,
        "sealed_test_top_minus_bottom_10bp_positive": (test["mean_top_minus_bottom_10bp"] or -1) > 0,
        "sealed_test_top_minus_bottom_20bp_positive": (test["mean_top_minus_bottom_20bp"] or -1) > 0,
        "sealed_test_top_quintile_20bp_beats_universe": (test["mean_top_minus_universe_20bp"] or -1) > 0,
        "all_selected_entries_and_exits_resolved": unresolved_selected == 0,
    }
    passed = all(gates.values())
    result = {
        "schema": "limit_up_excluded_momentum_blind_result.v1",
        "created_at": datetime.now(SH_TZ).isoformat(timespec="seconds"),
        "status": "historical_gate_passed_forward_required" if passed else "terminal_historical_gate_failure_no_retune",
        "candidate_id": protocol["candidate_id"], "factor_id": protocol["factor_id"],
        "admission": {"path": str(ADMISSION_PATH.relative_to(ROOT)), "sha256": _sha(ADMISSION_PATH)},
        "candidate_stock_months": int(len(outcomes)), "months": int(len(monthly)),
        "execution_status_counts": {str(k): int(v) for k, v in outcomes.status.value_counts().items()},
        "selected_rows": int(len(selected)), "unresolved_selected_rows": unresolved_selected,
        "fold_metrics": folds, "gates": gates,
        "failed_gates": [name for name, value in gates.items() if not value],
        "historical_factor_gate_passed": passed,
        "strict_forward_required": passed,
        "production_factor": False,
        "factor_blocks_non_factor_routes": False,
        "generates_buy_or_sell_alert": False,
        "monthly_evidence": {"path": str(MONTHLY_PATH.relative_to(ROOT)), "sha256": _sha(MONTHLY_PATH)},
        "selected_evidence": {"path": str(SELECTED_PATH.relative_to(ROOT)), "sha256": _sha(SELECTED_PATH)},
    }
    _write_json(RESULT_PATH, result)
    if not passed:
        _write_json(ARCHIVE_PATH, {
            "schema": "limit_up_excluded_momentum_terminal_archive.v1",
            "status": "permanently_archived_no_retune",
            "candidate_id": protocol["candidate_id"],
            "result": {"path": str(RESULT_PATH.relative_to(ROOT)), "sha256": _sha(RESULT_PATH)},
            "failed_gates": result["failed_gates"],
            "forbidden": ["sign_flip", "window_tuning", "fold_removal", "threshold_tuning"],
        })
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("admit", "run"))
    command = parser.parse_args().command
    output = admit() if command == "admit" else run()
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
