"""Audit the actual bounded catalogue, source identities and retained checkpoints.

No data prices, account, provider network, database, trial queue or economic runs.
This is a delivery audit, not an L1/strategy admission engine.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = "workflow_archive/QUANT-PB01-CONTINUOUS-20261002-001/checkpoint_ff23_full_goal"
FIRST = "research/GOAL_FIRST_FACTOR_REGISTRATION_20261002.json"
PLAYBOOKS = "research/GOAL_PARALLEL_PLAYBOOK_REGISTRATION_20261002.json"
SCOPE = "research/GOAL_USE_AND_CLOCK_SCOPE_20261002.json"
# The October 2 registrations describe this original implementation. The
# October 4 causal handoff repair must not rewrite their historical evidence.
# This is one explicit provenance binding, not a general Git fallback.
HISTORICAL_SOURCE_PATH = "research/m24_baseline_preparation.py"
HISTORICAL_SOURCE_COMMIT = "ff23f8e44d214160a7f1f29fc07875eca590754a"
HISTORICAL_SOURCE_SHA256 = "ce2a5a0805533b91a6919cf36c78060b524cb1b1b1e979693adb2d682e71c6d1"
HISTORICAL_WORKBENCH_PATH = "mom_trading/research_workbench.py"
HISTORICAL_WORKBENCH_SHA256 = "a3dd32e40a43e7692daf8550fc49f2614546c21c2524c5935b7a3708fae8702e"


def digest(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def read(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def git_blobs(commit, paths):
    data = subprocess.check_output(["git", "cat-file", "--batch"], cwd=ROOT,
        input="".join(commit+":"+p+"\n" for p in paths).encode(),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    pos, out = 0, {}
    for path in paths:
        end = data.index(b"\n", pos); header = data[pos:end].split()
        assert len(header) == 3 and header[1] == b"blob", path
        size = int(header[2]); payload = data[end+1:end+1+size]; pos = end+size+2
        out[path] = hashlib.sha256(payload).hexdigest()
    assert pos == len(data)
    return out


def verify_registered_source(ref, *, historical_cache=None):
    """Verify original provenance; this never certifies the amended code."""
    path = ref["path"]
    if path == HISTORICAL_SOURCE_PATH:
        assert ref["sha256"] == HISTORICAL_SOURCE_SHA256, "historical_binding_sha_changed"
        cache = {} if historical_cache is None else historical_cache
        if path not in cache:
            cache.update(git_blobs(HISTORICAL_SOURCE_COMMIT, [path]))
        assert cache[path] == HISTORICAL_SOURCE_SHA256, "historical_git_blob_changed"
        return "historical_git"
    assert digest(ROOT/path) == ref["sha256"], path
    return "current_file"


def verify_archived_regression_inputs(inputs, source_commit):
    """Preserve the pre-E117 workbench receipt, without approving current code."""
    historical = 0
    for path, expected in inputs.items():
        if path == HISTORICAL_WORKBENCH_PATH:
            assert source_commit == HISTORICAL_SOURCE_COMMIT, "archived_regression_commit_changed"
            assert expected == HISTORICAL_WORKBENCH_SHA256, "archived_workbench_digest_changed"
            assert git_blobs(source_commit, [path])[path] == expected, "archived_workbench_blob_changed"
            historical += 1
        else:
            assert digest(ROOT/path) == expected, path
    return historical


def audit():
    factors, books, scopes = read(FIRST), read(PLAYBOOKS), read(SCOPE)
    historical_cache = {}
    historical_bindings = 0
    for collection in (factors, books):
        for ref in collection["bindings"]:
            historical_bindings += verify_registered_source(ref, historical_cache=historical_cache) == "historical_git"
        assert collection["production_eligible"] is False and collection["generates_alert"] is False
    assert books["factor_registration"]["sha256"] == digest(ROOT/FIRST)
    rows = factors["factors"]
    assert 0 < len(rows) == factors["factor_count"] <= 16
    assert len({r["id"] for r in rows}) == len(rows)
    assert all(sum(r["formula_variants"] for r in rows if r["family"] == family) <= 4
               for family in {r["family"] for r in rows})
    decisions = {"excluded_pending_lineage", "control_only", "evidence_insufficient"}
    for row in rows:
        assert row["decision"] in decisions and row["economic_runs"] == 0
        assert row["economic_configurations_frozen"] == 0 and not row["new_admission"]
        assert row["prior_search_count"] == "unknown_not_reset" and row["parents"]
        historical_bindings += verify_registered_source(row["source_binding"], historical_cache=historical_cache) == "historical_git"
        assert all(row[k] for k in ("formula", "unit", "input_clock", "failure_condition", "choice_freedom", "required_inputs"))
    tree = ast.parse((ROOT/"mom_trading/emotion_pattern_research.py").read_text(encoding="utf-8"))
    templates = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "FACTORS" for t in n.targets))
    by_factor = {r["id"]: r for r in rows}
    assert all(by_factor[t["id"]]["formula"] == t["formula"] for t in templates)
    diff = read("research/SENTIMENT_DIFFUSION_CANDIDATES_20260929.json")
    assert all(by_factor[t["id"]]["formula"] == t["formula"] for t in diff["candidates"])
    cards = {c["id"]: c for c in books["complete_disposition_cards"]}
    assert len(cards) == books["cards_count"] == 7
    for name in ("original_M25_six_family_coverage", "N3_six_mechanism_coverage"):
        cover = books[name]
        assert len(cover) == len({r["family"] for r in cover}) == 6
        assert all(r["card_id"] in cards for r in cover)
    fields = ("market_condition", "sector_condition", "stock_selection", "entry_new_cards", "position",
              "exit_new_cards", "conflicts", "costs", "no_trade", "failure_condition", "validation", "required_inputs")
    for card in cards.values():
        assert all(card[k] for k in fields), card["id"]
        assert card["capital_yuan"] == 150000 and card["decision"] in decisions
        assert card["economic_runs"] == 0 and not card["deep_trial_admitted"]
        assert not card["production_eligible"] and not card["generates_alert"]
    assert len(books["representative_economic_cards"]) <= 3
    for collection in (factors, books):
        assert collection["maximum_deep_strategy_count"] == 3
        assert collection["max_complete_configs_per_mechanism"] == 4
        assert collection["max_winner_selection_configs"] == 12
    assert read("research/STATE_CONDITIONED_MAINLINE_SWING_V1_TERMINAL_ARCHIVE.json")["reuse_policy"]["rename_and_reenter_queue"] is False
    assert cards["PB04_TREND_BREAKOUT_EXCLUDED"]["entry_rule_is_closed"] is True
    for row in scopes["primary_sources_reused"]:
        assert digest(ROOT/row["path"]) == row["sha256"]
    assert scopes["current_full_data_L1"] is False and scopes["model_admitted"] is False
    assert scopes["stage_dependencies"]["pb01_stock_signal"]["minute_quotes_required"] is False
    assert scopes["stage_dependencies"]["original_M24_replay"]["real_fill_requires_independent_evidence"] is True
    archive = read(ARCHIVE+"/manifest.json")
    originals = {r["path"]:r["sha256"] for r in archive["files"]}
    assert git_blobs(archive["source_commit"], list(originals)) == originals
    assert all(digest(ROOT/ARCHIVE/p) == h for p,h in originals.items())
    old = read(ARCHIVE+"/WORKFLOW_TASK.json")
    historical_regression_bindings = 0
    for check in old["checks"]:
        receipt = read(ARCHIVE+"/"+check["receipt"])
        assert receipt["returncode"] == 0 and not receipt["timed_out"]
        assert receipt["contract_sha256"] == old["contract_sha256"]
        assert receipt["inputs_before"] == receipt["inputs_after"]
        if check["id"] == "pb01-workbench-regression":
            historical_regression_bindings += verify_archived_regression_inputs(receipt["inputs_after"], archive["source_commit"])
    frozen = read(ARCHIVE+"/research/PB01_PIPELINE_RESULT_20261002.json")["original_frozen_bindings"]
    assert all(digest(ROOT/r["path"]) == r["sha256"] for r in frozen)
    assert git_blobs("214856c8c18139c18d7f407b50b158004b6859d9",[r["path"] for r in frozen]) == {r["path"]:r["sha256"] for r in frozen}
    assert all(not (ROOT/p).exists() for p in old["future_evidence_required"])
    refined = "workflow_archive/QUANT-PB01-CONTINUOUS-20261002-001/checkpoint_fb6_before_role_clock_refinement"
    intermediate = read(refined+"/manifest.json")
    assert all(digest(ROOT/refined/r["path"]) == r["sha256"] for r in intermediate["files"])
    before = read(refined+"/WORKFLOW_TASK.json")
    for c in before["checks"]:
        receipt = read(refined+"/"+c["receipt"])
        assert receipt["contract_sha256"] == before["contract_sha256"]
        assert receipt["returncode"] == 0 and receipt["inputs_before"] == receipt["inputs_after"]
    return dict(scope="actual catalogue/source/archive consistency only; no L1-L4 or return certification",
        historical_source_bindings=historical_bindings,
        historical_source_commit=HISTORICAL_SOURCE_COMMIT,
        amended_implementation_validated_by_this_audit=False,
        factor_definitions=len(rows),complete_disposition_cards=len(cards),original_six_family_coverage=6,
        N3_six_mechanism_coverage=6,archived_git_originals=len(originals),retained_guards=4,
        exact_local_pre_refinement_files=len(intermediate["files"]),
        retained_legacy_regression_inputs_unchanged=False,
        retained_legacy_regression_evidence_preserved=True,
        historical_legacy_regression_bindings=historical_regression_bindings,
        current_workbench_verified_by_this_audit=False,old_frozen_files_unchanged=5,
        raw_market_rows_read=0,new_economic_runs=0,model_admitted=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--write-summary", action="store_true")
    args = parser.parse_args(); result = audit()
    if args.write_summary:
        (ROOT/"research/GOAL_REGISTRATION_AUDIT_20261002.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))
