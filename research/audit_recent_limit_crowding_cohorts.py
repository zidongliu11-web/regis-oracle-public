"""Reconstruct pre-outcome groups of the already-seen crowding factor study.

This is an implementation audit, not a new blind test, formula or sign search.
Original source, admission, returns and state evidence are never overwritten.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from mom_trading.crowding_factor_validation import freeze_quintiles, cohort_difference
from research import recent_limit_hit_crowding_v1 as original
from research.run_emotion_pattern_replay import sha, write_new


def run(output: Path) -> dict:
    output = output.resolve()
    if not output.is_relative_to(ROOT):
        raise ValueError("cohort_audit_output_outside_project")
    started = output.with_suffix(".started.json")
    if output.exists() or started.exists():
        raise FileExistsError("cohort audit attempt exists; do not overwrite")
    protocol = original._load_protocol()
    admission = json.loads(original.ADMISSION_PATH.read_text(encoding="utf-8"))
    historical = json.loads(original.RESULT_PATH.read_text(encoding="utf-8"))
    state_result = json.loads((ROOT / "research/RECENT_LIMIT_HIT_CROWDING_STATE_MATRIX_V1_RESULT.json").read_text())
    refs = list(admission["bindings"].values()) + [historical["selected_evidence"], historical["period_evidence"]]
    refs.append(state_result["evidence"])
    refs += [{"path": str(p.relative_to(ROOT)), "sha256": sha(p)} for p in (
        original.RESULT_PATH, ROOT / "research/RECENT_LIMIT_HIT_CROWDING_STATE_MATRIX_V1_RESULT.json",
        ROOT / "mom_trading/crowding_factor_validation.py", Path(__file__),
    )]
    for ref in refs:
        if sha(ROOT / ref["path"]) != ref["sha256"]:
            raise ValueError(f"cohort_audit_binding_mismatch:{ref['path']}")
    write_new(started, {"status": "implementation_audit_started", "bindings": refs,
                        "formula_or_direction_changed": False, "new_blind_trial": False,
                        "selection": "complete signal universe; never select on outcome availability"})
    try:
        frame = original.add_crowding_factor(original._load_daily(protocol))
        candidates, sessions = original._candidate_rows(frame, protocol)
        signals = candidates.rename(columns={"trade_date": "signal_day", "negative_limit_hit_count_20_v1": "factor"})
        expected = freeze_quintiles(signals)
        saved = pd.read_csv(ROOT / historical["selected_evidence"]["path"], dtype={"code": str, "signal_day": str})
        difference = cohort_difference(expected, saved)
        print(json.dumps({"phase": "cohorts_frozen", "candidates": len(candidates),
                          **{k: v for k, v in difference.items() if not isinstance(v, list)}}), flush=True)
        by_key = frame.set_index(["trade_date", "code"], drop=False)
        calendar = {day: i for i, day in enumerate(sessions)}
        missing_outcomes = [
            {**row, **original.execution_outcome(by_key, sessions, calendar[row["signal_day"]], row["code"])}
            for row in difference["missing_pre_outcome_members"]
        ]
        unresolved = [row for row in missing_outcomes if row["status"].startswith("unresolved")]
        state_rows = pd.read_csv(ROOT / state_result["evidence"]["path"], dtype={"signal_day": str})
        state_map = dict(zip(state_rows.signal_day, state_rows.confirmed_state))
        for row in unresolved:
            row["confirmed_state"] = state_map[row["signal_day"]]
        if len(candidates) != historical["candidate_stock_periods"] or len(saved) != historical["selected_rows"]:
            raise ValueError("cohort_audit_original_population_not_reproduced")
        for ref in refs:
            if sha(ROOT / ref["path"]) != ref["sha256"]:
                raise ValueError("cohort_audit_inputs_or_source_changed")
        result = {
            "schema": "recent_limit_crowding_cohort_audit.v1", "bindings": refs,
            "status": "historical_cohort_integrity_failed" if unresolved or not difference["same_cohorts"] else "cohorts_match",
            "signal_count": len(candidates), "comparison": difference,
            "missing_member_outcomes": missing_outcomes,
            "pre_outcome_selected_unresolved_count": len(unresolved),
            "unresolved_selected_by_state": pd.Series([r["confirmed_state"] for r in unresolved], dtype=str).value_counts().to_dict(),
            "original_reported_unresolved_selected": historical["unresolved_selected_rows"],
            "corrected_complete_return": None, "production_factor": False,
            "new_blind_trial": False, "formula_or_direction_changed": False,
            "consequence": "Cannot rely on the old all-selected-resolved gate or its derived emotion qualification until corrected validation is completed.",
        }
        write_new(output, result)
        return {k: v for k, v in result.items() if k not in {"bindings", "missing_member_outcomes", "comparison"}}
    except Exception as exc:
        write_new(output.with_suffix(".failed.json"), {"status": "cohort_audit_failed", "error": str(exc)})
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(run(parser.parse_args().output), ensure_ascii=False, indent=2))
