"""Read source identities without importing runtime/account modules or data.

Build a preparation inventory, never a factor/strategy admission registry.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "research/GOAL_REUSE_INVENTORY_20261001.json"
INPUT_COMMIT = "0c7cc4296762dce9d62d8f22d660d88044c3bf4d"
INPUTS = (
    "research/STRATEGY_SOURCE_AND_FEASIBILITY_CATALOGUE_V1.json",
    "research/SENTIMENT_DIFFUSION_CANDIDATES_20260929.json",
    "research/SYSTEM_ROUTE_JOINT_V2_ADAPTER_PROTOCOL_V14.json",
    "research/STATE_CONDITIONED_MAINLINE_SWING_V1_TERMINAL_ARCHIVE.json",
    "mom_trading/emotion_pattern_research.py",
    "mom_trading/playbook_development.py",
    "mom_trading/playbook_entry_timing.py",
    "QUANT_RESEARCH_M24_PROTOCOL_20261001.json",
    "research/m24_baseline_preparation.py",
    "research/m24_data_adapter.py",
    "research/m24_accounting.py",
    "QUANT_RESEARCH_M24_DATA_READINESS_20261001.json",
    "mom_trading/project_input_recovery.py",
)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def literal(name):
    tree = ast.parse((ROOT / "mom_trading/emotion_pattern_research.py").read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError(f"Missing literal {name}")


def build():
    identities = []
    for path in INPUTS:
        raw = (ROOT / path).read_bytes()
        original = subprocess.run(["git", "show", f"{INPUT_COMMIT}:{path}"], cwd=ROOT,
                                  capture_output=True, check=True).stdout
        if raw != original:
            raise ValueError(f"Source drift: {path}")
        identities.append({"path": path, "sha256": digest(raw), "input_blob_verified": True})
    catalogue = read(INPUTS[0])
    candidates = read(INPUTS[1])
    adapter = read(INPUTS[2])
    archive = read(INPUTS[3])
    binding = adapter["bindings"]["terminal_mainline_archive"]
    if digest((ROOT / binding["path"]).read_bytes()) != binding["sha256"]:
        raise ValueError("Terminal archive binding mismatch")
    retired = adapter["terminal_playbook_retirement"]["retired_entry_playbooks"]
    families = []
    for row in catalogue["strategy_families"]:
        status = "retired_no_rerun" if row["id"] in retired else row["decision"]
        families.append({"original_id": row["id"], "original_catalogue_decision": row["decision"],
                         "current_disposition": status, "original": row,
                         "source_path": INPUTS[0], "source_sha256": identities[0]["sha256"],
                         "current_data_feasibility": "unverified_do_not_inherit_old_available_label",
                         "historical_search_count": "unknown_not_reset",
                         "next_action": "retain_retirement" if row["id"] in retired else "mechanism_card_and_role_specific_data_check"})
    return {
        "schema": "regis.goal.reuse_inventory.v1", "input_commit": INPUT_COMMIT,
        "scope": "N1_source_inventory_not_admission", "inputs": identities,
        "families": families, "diffusion_candidates": candidates["candidates"],
        "diffusion_source_path": INPUTS[1], "diffusion_source_sha256": identities[1]["sha256"],
        "existing_playbook_templates": literal("PLAYBOOKS"), "existing_feature_templates": literal("FACTORS"),
        "template_source_path": INPUTS[4], "template_source_sha256": identities[4]["sha256"],
        "template_execution_limit": "AST literals only; playbook_development imports fcntl; Windows route not verified",
        "current_retirement": {"source": INPUTS[2], "source_sha256": identities[2]["sha256"],
                               "ids": retired, "archive_id": archive["candidate_id"],
                               "archive_status": archive["status"], "reuse_policy": archive["reuse_policy"]},
        "obsolete_priority_conflict": {"old": catalogue["priority"]["next_candidate"],
                                       "resolution": "later frozen retirement prevails; no renamed rerun"},
        "baseline": read(INPUTS[7]),
        "missing_named_originals": [{"path": "research/SMALL_CAPITAL_PLAYBOOK_LIBRARY_V1.json",
                                    "exists": (ROOT / "research/SMALL_CAPITAL_PLAYBOOK_LIBRARY_V1.json").exists(),
                                    "recovery_status": "missing_no_empty_replacement"}],
        "legacy_registry_and_three_PIT_panels": {
            "prior_audit": "M19 Git object/ref/release/Actions audit inherited, not rerun",
            "path_source": INPUTS[12], "path_source_sha256": identities[12]["sha256"],
            "current_exact_path_existence_only": [
                {"path": path, "exists": (ROOT/path).exists(), "bytes_not_read": True}
                for path in ("factor_system/registry/registry.db",
                             "research/backtest_project/data/universe_daily.parquet",
                             "research/backtest_project/data/pit_fundamentals.parquet",
                             "research/backtest_project/data/stock_meta.parquet")],
            "interpretation": "specific expected originals absent; no empty replacement or whole repository rerun",
        },
        "seen_data": {"historical_family_outcomes": "known_prior_exposure",
                      "this_inventory_reads_archive": "terminal status/reuse only in output; archive contains old result values",
                      "complete_exposure_dates": "unknown_until_original_registry_recovered",
                      "new_unseen_OOS_status": "not_granted"},
        "new_economic_runs": 0, "model_admitted": False, "raw_or_account_read": False,
        "check_scope": "source byte identities/AST/inherited status only; not economic validity or rights",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    result = build()
    if args.verify:
        if read(OUTPUT.relative_to(ROOT)) != result:
            raise ValueError("Inventory stale or altered")
    else:
        OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"verified": args.verify, "families": len(result["families"]),
                      "diffusion_candidates": len(result["diffusion_candidates"]),
                      "playbook_templates": len(result["existing_playbook_templates"]),
                      "feature_templates": len(result["existing_feature_templates"]),
                      "retired": result["current_retirement"]["ids"], "economic_runs": 0}))


if __name__ == "__main__":
    main()
