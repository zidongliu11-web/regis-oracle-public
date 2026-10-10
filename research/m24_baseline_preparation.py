"""M24 preparation tools. No download, economic replay, orders or live authority.

The new selector adapts a mature reversal mechanism; it does not reproduce the
old sector-dependent frozen strategy. Supplied PIT facts still need independent
source/coverage admission. Reference-price quantities are conditional drafts.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from factor_system.metrics import ic_stats
from quantqc.ashare import board_of
from quantqc.costs import LOT_SIZE, lots_from_yuan, one_side_cost_breakdown

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = "QUANT_RESEARCH_M24_PROTOCOL_20261001.json"
SUMMARY = "QUANT_RESEARCH_M24_PREPARATION_SUMMARY_20261001.json"
CAPITAL = 150_000.0
HORIZONS = (1, 3, 5)
REQUIRED = {
    "code", "stable_instrument_id", "trade_date", "available_at", "close",
    "pct_chg", "median_amount20", "adv20", "history_sessions",
    "universe_member", "is_st", "trade_status", "adjustment_trusted",
    "factor_changed", "no_price_limit", "lower_limit_yuan",
}
AUTHORITY = dict(research_only=True, model_admitted=False,
                 economic_target_A_validated=False, orders_enabled=False)


def _time(value: Any) -> datetime:
    value = datetime.fromisoformat(str(value))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone_required")
    return value


def _calendar(sessions: list[str]) -> list[str]:
    if not sessions or any(date.fromisoformat(s).isoformat() != s for s in sessions):
        raise ValueError("session_date_invalid")
    if sessions != sorted(set(sessions)):
        raise ValueError("sessions_not_unique_and_sorted")
    return sessions


def signal_clock_context(*, decision_at: str, signal_session: str,
                         sessions: list[str]) -> dict[str, str]:
    """Bind a supplied completed signal session to the actual decision clock.

    Calendar ordering and integrity are checked here; source completeness and
    historical availability still require independent data admission.
    Never shift the actual decision backward to the session date.
    """
    _calendar(sessions)
    if (not isinstance(signal_session, str)
            or date.fromisoformat(signal_session).isoformat() != signal_session
            or signal_session not in sessions):
        raise ValueError('signal_session_not_in_calendar')
    origin = sessions.index(signal_session)
    if origin + 1 >= len(sessions):
        raise ValueError('signal_next_session_missing')
    decision = _time(decision_at).astimezone(ZoneInfo('Asia/Shanghai'))
    close_time = datetime.combine(date.fromisoformat(signal_session), time(15), decision.tzinfo)
    entry_time = datetime.combine(date.fromisoformat(sessions[origin + 1]), time(9, 35), decision.tzinfo)
    if decision < close_time:
        raise ValueError('completed_session_required')
    if decision >= entry_time:
        raise ValueError('signal_entry_window_elapsed')
    calendar_hash = hashlib.sha256(json.dumps(sessions, separators=(',', ':')).encode()).hexdigest()
    return {'signal_session': signal_session, 'session_close_at': close_time.isoformat(),
            'entry_session': sessions[origin + 1], 'entry_deadline_at': entry_time.isoformat(),
            'calendar_sessions_sha256': calendar_hash,
            'calendar_admission': 'supplied_dates_integrity_only_not_source_completeness'}


def select_reversal(rows: pd.DataFrame, *, decision_at: str,
                    market_gate: dict[str, Any], signal_session: str | None = None,
                    sessions: list[str] | None = None) -> dict[str, Any]:
    """Select at most two liquid losing stocks without any sector input."""
    decision = _time(decision_at)
    decision_sh = decision.astimezone(ZoneInfo("Asia/Shanghai"))
    context = None
    if signal_session is not None:
        context = signal_clock_context(decision_at=decision_at,
                                       signal_session=signal_session, sessions=sessions)
        day = context['signal_session']
        close_time = _time(context['session_close_at'])
    else:
        if sessions is not None:
            raise ValueError('explicit_signal_session_required')
        day = decision_sh.date().isoformat()
        close_time = datetime.combine(decision_sh.date(), time(15), decision_sh.tzinfo)
        if decision < close_time:
            raise ValueError("completed_session_required")
    result = {"baseline_id": "m24_market_gated_price_volume_reversal_v1",
              "trade_date": day, "decision_at": decision_sh.isoformat(),
              "selected": [], "candidate_count": 0, "authority": AUTHORITY.copy()}
    if context is not None:
        result['signal_clock_context'] = context
    gate_time = _time(market_gate.get("available_at"))
    if (type(market_gate.get("open")) is not bool or not close_time <= gate_time <= decision
            or market_gate.get("trade_date") != day
            or market_gate.get("full_population_verified") is not True
            or not re.fullmatch(r"[0-9a-f]{64}", str(market_gate.get("source_sha256", "")))):
        raise ValueError("market_gate_unverified_or_future")
    if not market_gate["open"]:
        return {**result, "status": "no_new_risk_cash", "reason": "market_gate_closed"}
    missing = sorted(REQUIRED - set(rows.columns))
    if missing:
        return {**result, "status": "input_incomplete", "missing_columns": missing}
    if rows["stable_instrument_id"].isna().any() or rows["stable_instrument_id"].duplicated().any():
        raise ValueError("instrument_identity_missing_or_duplicate")
    codes = rows["code"].astype(str).str.split(".").str[0].str.zfill(6)
    if codes.duplicated().any() or not codes.str.fullmatch(r"\d{6}").all():
        raise ValueError("code_identity_invalid_or_duplicate")
    eligible, rejected = [], {}
    for raw, code in zip(rows.to_dict("records"), codes):
        reason = None
        try:
            available = _time(raw["available_at"])
            numeric = {k: float(raw[k]) for k in (
                "close", "pct_chg", "median_amount20", "adv20", "history_sessions", "lower_limit_yuan")}
            if not all(math.isfinite(v) for v in numeric.values()):
                reason = "nonfinite_input"
            elif str(raw["trade_date"])[:10] != day or not close_time <= available <= decision:
                reason = "wrong_date_or_future_input"
            elif not str(raw["stable_instrument_id"]).strip():
                reason = "instrument_identity_missing"
            elif board_of(code) not in {"main", "gemb"}:
                reason = "outside_trade_scope"
            elif any(not isinstance(raw[k], (bool, np.bool_)) for k in (
                "universe_member", "is_st", "trade_status", "adjustment_trusted", "factor_changed", "no_price_limit")):
                reason = "PIT_flag_type_invalid"
            elif (not raw["universe_member"] or raw["is_st"] or not raw["trade_status"]
                  or not raw["adjustment_trusted"] or raw["factor_changed"] or raw["no_price_limit"]):
                reason = "PIT_tradability_or_action_gate"
            elif (numeric["history_sessions"] < 20 or numeric["history_sessions"] % 1
                  or numeric["median_amount20"] < 50_000_000 or numeric["adv20"] < 50_000_000):
                reason = "history_or_liquidity_gate"
            elif (numeric["close"] <= 0 or numeric["lower_limit_yuan"] <= 0
                  or numeric["close"] <= numeric["lower_limit_yuan"] or numeric["pct_chg"] >= 0):
                reason = "price_or_reversal_gate"
        except (TypeError, ValueError, OverflowError):
            reason = "input_parse_failed"
        if reason:
            rejected[reason] = rejected.get(reason, 0) + 1
        else:
            eligible.append({"code": code, "stable_instrument_id": str(raw["stable_instrument_id"]),
                             "close": numeric["close"], "pct_chg": numeric["pct_chg"],
                             "median_amount20": numeric["median_amount20"]})
    eligible.sort(key=lambda r: (r["pct_chg"], -r["median_amount20"], r["stable_instrument_id"]))
    return {**result, "status": "conditional_research_candidates",
            "selected": eligible[:2], "candidate_count": len(eligible), "rejected_counts": rejected}


def prepare_horizon_drafts(selection: dict[str, Any], *, sessions: list[str],
                           decision_day: str) -> dict[str, Any]:
    """Reference-price affordability, no assumed fill and no compounding arms."""
    # Invalid selection is not a cash decision, even on a short calendar.
    if (not isinstance(selection, dict)
            or selection.get("status") not in {"conditional_research_candidates", "no_new_risk_cash"}
            or selection.get("baseline_id") != "m24_market_gated_price_volume_reversal_v1"
            or selection.get("authority") != AUTHORITY):
        raise ValueError("selection_not_valid")
    selected, count = selection.get("selected"), selection.get("candidate_count")
    if (not isinstance(selected, list) or type(count) is not int or count < 0
            or len(selected) != min(2, count)
            or (selection["status"] == "no_new_risk_cash" and (selected or count))
            or any(not isinstance(r, dict) or not isinstance(r.get("code"), str)
                   or not re.fullmatch(r"\d{6}", r["code"]) for r in selected)):
        raise ValueError("selection_not_valid")
    if (len({r["code"] for r in selected}) != len(selected)
            or any(board_of(r["code"]) not in {"main", "gemb"} for r in selected)):
        raise ValueError("selection_not_valid")
    try:
        trade_day = selection["trade_date"]
        if (not isinstance(trade_day, str) or date.fromisoformat(trade_day).isoformat() != trade_day
                or trade_day != decision_day):
            raise ValueError("date_mismatch")
        decision = _time(selection["decision_at"]).astimezone(ZoneInfo("Asia/Shanghai"))
        if 'signal_clock_context' in selection:
            expected = signal_clock_context(decision_at=selection['decision_at'],
                                            signal_session=trade_day, sessions=sessions)
            if selection['signal_clock_context'] != expected:
                raise ValueError('signal_clock_context_mismatch')
        elif decision.date().isoformat() != trade_day or decision.time() < time(15):
            raise ValueError("clock_mismatch")
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("selection_decision_context_invalid") from None
    context = {"trade_date": trade_day, "decision_at": decision.isoformat(),
               "selection_status": selection["status"], "candidate_count": count}
    if 'signal_clock_context' in selection:
        context['signal_clock_context'] = selection['signal_clock_context'].copy()
    _calendar(sessions)
    origin = sessions.index(decision_day)
    if origin + 1 + max(HORIZONS) >= len(sessions):
        return {"status": "calendar_incomplete", **context, "authority": AUTHORITY.copy()}
    drafts = []
    for row in selected:
        price = float(row["close"])
        if not math.isfinite(price) or price <= 0:
            raise ValueError("reference_price_invalid")
        allocation = CAPITAL * 0.2
        quantity = lots_from_yuan(allocation, price)
        while quantity > 0:
            notional = quantity * price
            fee = one_side_cost_breakdown(notional, "buy")
            if notional + fee["total"] <= allocation:
                break
            quantity -= LOT_SIZE
        if quantity:
            drafts.append({"code": row["code"], "reference_price_yuan": price,
                           "reference_shares": quantity, "reference_buy_cost_yuan": fee["total"],
                           "reference_cash_required_yuan": notional + fee["total"],
                           "fill_status": "not_observed_not_executed"})
    return {"status": "conditional_drafts_only", **context, "initial_capital_yuan": CAPITAL,
            "capital_relationship": "same_capital_mutually_exclusive",
            "stock_reference_drafts": drafts,
            "reference_cash_remaining_yuan": CAPITAL - sum(x["reference_cash_required_yuan"] for x in drafts),
            "entry_session": sessions[origin + 1], "entry_time": "09:35_Asia/Shanghai_after_manual_delay",
            "exit_sessions": {str(h): sessions[origin + 1 + h] for h in HORIZONS},
            "exit_time": "14:55_Asia/Shanghai_strictly_before_14:57",
            "delayed_exit": "retain_actual_exit_and_open_inventory",
            "ETF_draft": "requires_separate_fee_action_and_executable_quote_path",
            "authority": AUTHORITY.copy()}


def purged_split(sessions: list[str], *, train_end: str, validation_end: str,
                 actual_exit_sessions: dict[str, str] | None = None) -> dict[str, Any]:
    """Purge both planned 1+5 labels and longer realized exit intervals."""
    _calendar(sessions)
    t, v = sessions.index(train_end), sessions.index(validation_end)
    if not 0 < t < v < len(sessions) - 1:
        raise ValueError("split_boundaries_invalid")
    actual = actual_exit_sessions or {}
    if set(actual) - set(sessions):
        raise ValueError("actual_exit_origin_not_in_calendar")
    groups = {"train": [], "validation": [], "test": []}
    excluded = []
    for i, origin in enumerate(sessions):
        group, begin, end = ("train", 0, t) if i <= t else (
            ("validation", t + 1, v) if i <= v else ("test", v + 1, len(sessions) - 1))
        planned_exit = i + 1 + max(HORIZONS)
        realized = planned_exit
        if origin in actual:
            date.fromisoformat(actual[origin])
            if actual[origin] <= origin:
                raise ValueError("actual_exit_precedes_entry")
            if actual[origin] in sessions:
                actual_index = sessions.index(actual[origin])
                if actual_index <= i + 1:
                    raise ValueError("actual_exit_violates_T_plus_1")
                realized = max(planned_exit, actual_index)
            elif actual[origin] > sessions[-1]:
                realized = len(sessions)
            else:
                raise ValueError("actual_exit_not_trading_session")
        if group != "train" and i < begin + 5:
            excluded.append({"origin": origin, "reason": "embargo", "segment": group})
        elif realized > end:
            excluded.append({"origin": origin, "reason": "label_crosses_segment_end", "segment": group})
        else:
            groups[group].append(origin)
    return {"groups": groups, "excluded": excluded, "entry_lag_sessions": 1,
            "max_horizon_sessions": 5, "embargo_sessions": 5,
            "threshold_fit_allowed": "train_only", "calendar_provenance": "caller_must_validate_official_calendar"}


def paired_excess(stock: pd.Series, etf: pd.Series, *, horizon: int,
                  expected_origins: list[str], max_actual_span_sessions: int) -> dict[str, Any]:
    """Describe paired net returns; reuse conservative HAC and time-block CI."""
    if horizon not in HORIZONS:
        raise ValueError("horizon_invalid")
    _calendar(expected_origins)
    if list(stock.index) != expected_origins or list(etf.index) != expected_origins:
        raise ValueError("origin_alignment_or_coverage_invalid")
    x = stock.to_numpy(dtype=float) - etf.to_numpy(dtype=float)
    if (not np.isfinite(x).all() or not np.isfinite(stock.to_numpy(dtype=float)).all()
            or not np.isfinite(etf.to_numpy(dtype=float)).all()
            or (stock.to_numpy(dtype=float) < -1).any() or (etf.to_numpy(dtype=float) < -1).any()):
        raise ValueError("missing_or_nonfinite_returns_no_silent_drop")
    n = len(x)
    result = {"n_origin_dates": n, "horizon_sessions": horizon,
              "mean_net_excess_return": float(x.mean()),
              "median_net_excess_return": float(np.median(x)),
              "std_net_excess_return": float(x.std(ddof=1)) if n > 1 else None,
              "model_admitted": False, "family_size": 3}
    if type(max_actual_span_sessions) is not int or not horizon + 1 <= max_actual_span_sessions <= 10:
        return {**result, "status": "evidence_insufficient", "p_value_bonferroni": 1.0,
                "descriptive_block_ci95": None, "reason": "actual_label_span_unverified_or_exceeds_frozen_block"}
    # Entry is at i+1 09:35 and exit at i+1+h 14:55. Stride h would
    # overlap intraday even without delays; use the verified maximum span.
    stride = max_actual_span_sessions
    if n < 60 or n // stride < 20 or float(x.std()) <= 1e-15:
        return {**result, "status": "evidence_insufficient", "p_value_bonferroni": 1.0,
                "descriptive_block_ci95": None, "reason": "too_few_origins_phases_or_degenerate_variance",
                "phase_stride_sessions": stride}
    stats = ic_stats(pd.Series(x), hold=stride)
    rng = np.random.default_rng(2401)
    block = 10
    offsets = np.arange(block)
    starts = rng.integers(0, n - block + 1, size=(2000, math.ceil(n / block)))
    indices = (starts[..., None] + offsets).reshape(2000, -1)[:, :n]
    means = x[indices].mean(axis=1)
    return {**result, "status": "research_statistics_only",
            "p_value_method": stats["p_value_method"],
            "phase_tests": stats["p_value_phase_sensitivity"],
            "phase_direction_consistent": stats["p_value_direction_consistent"],
            "phase_direction_reason": stats["p_value_direction_reason"],
            "phase_stride_sessions": stride,
            "span_source": "caller_must_verify_maximum_actual_exit_interval_including_delays",
            "n_effective_conservative_phase": stats["n_effective"],
            "p_value_raw": stats["p_value"],
            "p_value_bonferroni": min(1.0, 3 * stats["p_value"]),
            "descriptive_block_ci95": [float(v) for v in np.quantile(means, [0.025, 0.975])],
            "ci_family_adjusted": False, "block_sessions": block, "bootstrap_draws": 2000,
            "seed": 2401, "annualization_performed": False,
            "CI_assumption": "locally_stationary_paired_origin_series_frozen_time_blocks",
            "statistical_test_alone_admits_strategy": False}


def diagnostic(root: Path = ROOT) -> dict[str, Any]:
    """Bounded read of already-known inputs, only counts/hashes leave raw storage."""
    import pyarrow
    import pyarrow.parquet as pq
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    protocol = json.loads((root / PROTOCOL).read_text(encoding="utf-8"))
    if (protocol["initial_capital_yuan"] != CAPITAL or protocol["horizons_sessions_after_entry"] != list(HORIZONS)
            or protocol["historical_data_gate"]["economic_run_enabled"] is not False
            or protocol["authority"]["model_admitted"] is not False):
        raise ValueError("protocol_preparation_boundary_invalid")
    original = json.loads((root / "QUANT_RESEARCH_M21_ORIGIN_SUMMARY_20260930.json").read_text(encoding="utf-8"))
    source = root / "reports/local_closure/m21_tradsig_raw/20260930.parquet"
    if digest(source) != original["source_sha256"]:
        raise ValueError("existing_snapshot_sha_changed")
    file = pq.ParquetFile(source)
    columns = set(file.schema_arrow.names)
    dates = sorted({str(v)[:10] for v in file.read(columns=["trade_date"]).column(0).to_pylist()})
    if file.metadata.num_rows != original["full_population_count"] or dates != [original["trade_date"]]:
        raise ValueError("existing_snapshot_metadata_changed")
    provenance = json.loads((root / "QUANT_RESEARCH_M24_SKILL_PROVENANCE_20261001.json").read_text(encoding="utf-8"))
    skill = Path(provenance["installed_path"])
    if any(digest(skill / p) != expected for p, expected in provenance["files"].items()):
        raise ValueError("installed_skill_changed")
    from mom_trading.state_conditioned_T1_reversal import verify_protocol_chain
    legacy = verify_protocol_chain()
    archive = root / "workflow_archive/QUANT-RESEARCH-20260930-023/m23_preobservation_pending"
    archived = json.loads((archive / "SHA256.json").read_text(encoding="utf-8"))
    if any(digest(archive / p) != expected for p, expected in archived["bindings"].items()):
        raise ValueError("M23_archive_changed")
    return {"schema": "regis.m24.actual_preparation_diagnostic.v1",
            "status": "preparation_checked_historical_data_not_admitted",
            "source_sha256": digest(source), "observed_rows": file.metadata.num_rows,
            "observed_sessions": dates, "observed_session_count": len(dates),
            "new_selector_missing_columns": sorted(REQUIRED - columns),
            "required_OHLCV_missing_columns": sorted({"open", "high", "low", "close", "volume", "amount"} - columns),
            "legacy_protocol_valid": legacy["valid"], "legacy_protocol_errors": legacy["errors"],
            "new_baseline_is_exact_legacy_reproduction": False,
            "skill_files_verified": len(provenance["files"]), "skill_commit": provenance["commit"],
            "M23_archived_state": archived["state"], "M23_archived_files_verified": len(archived["bindings"]),
            "gates": {"multi_year_stock_PIT": False, "ETF_total_return": False,
                      "historical_rights": False, "manual_execution": False,
                      "economic_replay_enabled": False},
            "runtime_versions": {"numpy": np.__version__, "pandas": pd.__version__, "pyarrow": pyarrow.__version__},
            "raw_rows_or_symbols_exported": False, "returns_computed": False,
            "purchase_performed": False, "account_or_token_read": False,
            "authority": AUTHORITY.copy()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-summary", action="store_true")
    args = parser.parse_args()
    result = diagnostic()
    if args.write_summary:
        (ROOT / SUMMARY).write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    # ASCII JSON is also lossless on Windows' default cp1252 command pipes.
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
