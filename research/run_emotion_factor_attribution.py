"""Evaluate all five predefined auxiliary factors, all tracks and all costs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mom_trading.emotion_factor_attribution import SPEC, split_inputs_and_outcomes, freeze_factor_groups, evaluate_factor_groups, summarize_by_emotion
from research.run_emotion_pattern_replay import sha, write_new

PRIMARY = ROOT / "output/research/emotion_pattern_controls_v1.json"
PRIMARY_SHA = "dc74285d44b18255b4e47e258c13ae7968994a6c55274e2eea6eeb90580d44c0"


def run(output: Path, progress=print):
    output = output.resolve()
    started, selection = output.with_suffix(".started.json"), output.with_suffix(".groups.json")
    if not output.is_relative_to(ROOT):
        raise ValueError("attribution_output_outside_project")
    if any(p.exists() for p in (output, started, selection, output.with_suffix(".failed.json"))):
        raise FileExistsError("factor attribution attempt exists; do not overwrite")
    if sha(PRIMARY) != PRIMARY_SHA:
        raise ValueError("factor_attribution_primary_identity_mismatch")
    prior = json.loads(PRIMARY.read_text())
    refs = prior["bindings"] + [prior["event_ledger"], {"path": str(PRIMARY.relative_to(ROOT)), "sha256": PRIMARY_SHA}]
    refs += [{"path": p, "sha256": sha(ROOT / p)} for p in (
        "mom_trading/emotion_factor_attribution.py", "research/run_emotion_factor_attribution.py")]
    refs = list({r["path"]: r for r in refs}.values())
    for ref in refs:
        if sha(ROOT / ref["path"]) != ref["sha256"]:
            raise ValueError(f"factor_attribution_binding_mismatch:{ref['path']}")
    write_new(started, {"status": "factor_attribution_started", "spec": SPEC, "bindings": refs})
    try:
        events = [json.loads(line) for line in (ROOT / prior["event_ledger"]["path"]).open()]
        if len(events) != prior["path_count"]:
            raise ValueError("factor_attribution_path_count_changed")
        inputs, outcomes = split_inputs_and_outcomes(events)
        groups = freeze_factor_groups(inputs)
        write_new(selection, {"schema": SPEC["schema"], "signal_count": len(inputs), "groups": groups})
        progress(json.dumps({"phase": "all_factor_groups_frozen", "signals": len(inputs), "groups": len(groups)}), flush=True)
        rows = evaluate_factor_groups(groups, outcomes)
        summary = summarize_by_emotion(rows)
        for ref in refs:
            if sha(ROOT / ref["path"]) != ref["sha256"]:
                raise ValueError("factor_attribution_source_changed_during_run")
        result = {"status": "five_factor_attribution_completed_not_admitted", "spec": SPEC, "bindings": refs,
                  "source_event_ledger": prior["event_ledger"], "source_ledger_unchanged": True,
                  "signal_count": len(inputs), "cost_path_count": len(events),
                  "selection": {"path": str(selection.relative_to(ROOT)), "sha256": sha(selection)},
                  "factor_count": len(SPEC["factors"]), "factor_day_cost_count": len(rows),
                  "summaries": summary, "daily_metrics": rows,
                  "production_eligible": False, "generates_alert": False, "portfolio_return": None,
                  "limitations": ["All historical results were already seen; not a new blind trial.",
                                  "Missing factor inputs are counted; factor-eligible results do not represent those missing inputs.",
                                  "Complete-days-only results can be outcome-selected and cannot qualify a factor.",
                                  "No long-short portfolio, significance claim, factor-direction choice or real order is produced.",
                                  "Original execution and corporate-entitlement gaps remain; this evaluates rankings, not a repaired price feed."]}
        write_new(output, result)
        return {k: v for k, v in result.items() if k not in {"bindings", "summaries", "daily_metrics"}}
    except Exception as exc:
        write_new(output.with_suffix(".failed.json"), {"status": "factor_attribution_failed", "error": str(exc)})
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(run(parser.parse_args().output), ensure_ascii=False, indent=2))
