"""Actual project metadata inspection. Does not read raw quotes or run experiments."""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research.pb01_pipeline import current_preflight, metadata, sha256, FUTURE

FROZEN = ("quantqc/costs.py", "research/m24_accounting.py", "research/m24_data_adapter.py",
          "QUANT_RESEARCH_M23_PROTOCOL_20260930.json", "QUANT_RESEARCH_M24_PROTOCOL_20261001.json")
INPUT_COMMIT = "214856c8c18139c18d7f407b50b158004b6859d9"


def frozen_bindings():
    request = "".join(INPUT_COMMIT + ":" + path + "\n" for path in FROZEN).encode("utf-8")
    raw = subprocess.check_output(["git", "cat-file", "--batch"], input=request, cwd=ROOT,
                                  creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0))
    pos, results = 0, []
    for name in FROZEN:
        end = raw.index(b"\n", pos); header = raw[pos:end].split()
        if len(header) != 3 or header[1] != b"blob": raise ValueError("frozen_git_blob_missing")
        size = int(header[2]); data = raw[end+1:end+1+size]; pos = end+size+2
        expected = hashlib.sha256(data).hexdigest()
        if sha256(ROOT / name) != expected: raise ValueError("frozen_input_changed")
        results.append({"path": name, "sha256": expected, "unchanged_from": INPUT_COMMIT})
    if pos != len(raw): raise ValueError("frozen_git_batch_invalid")
    return results


def inspect():
    result = current_preflight(ROOT)
    rq = metadata(ROOT, "research/RQ_DAILY_ADMISSION_RESULT_20261002.json")
    identity = metadata(ROOT, "research/IDENTITY_CHECK_RESULT_20261002.json")
    contact = metadata(ROOT, "research/PB01_PROVIDER_OBSERVATION_20261002.json")
    from scripts import workflow_guard
    task = metadata(ROOT, "WORKFLOW_TASK.json")
    workflow_guard.validate_task(ROOT, task, evidence=False)
    stop_probe = workflow_guard.hook({"cwd": str(ROOT), "session_id": task["owner_session"],
                                    "hook_event_name": "Stop"})
    if task["status"] == "in_progress" and stop_probe.get("decision") != "block":
        raise ValueError("parent_stop_probe_failed")
    if rq["sample_sha256"] != identity["sample_sha256"] or rq["snapshot_known_at"] != identity["snapshot_known_at_unchanged"]:
        raise ValueError("reused_sample_version_changed")
    from flask import Flask
    from mom_trading.research_workbench import blueprint
    runtime = ROOT / ".codex-local/pb01-readonly-probe-no-runtime"
    if runtime.exists(): raise ValueError("probe_runtime_must_be_absent")
    app = Flask(__name__)
    app.config.update(PB01_PROJECT_ROOT=ROOT, RESEARCH_WORKBENCH_ROOT=runtime)
    app.register_blueprint(blueprint)
    response = app.test_client().get("/api/research-workbench/pb01")
    if response.status_code != 200 or response.get_json() != result or runtime.exists():
        raise ValueError("actual_metadata_GET_failed_or_created_runtime")
    result.update({
        "scope": "actual project metadata and GET wiring; no new market-data QC or economics",
        "parent_task_id": task["id"], "parent_task_contract_sha256": task["contract_sha256"],
        "parent_task_status_at_inspection": task["status"],
        "manual_stop_probe_decision": stop_probe.get("decision"),
        "automatic_host_hook_trust_verified": False,
        "current_project_GET_status": response.status_code,
        "runtime_or_database_created": False,
        "reused_sample_sha256": rq["sample_sha256"],
        "snapshot_known_at": rq["snapshot_known_at"],
        "historical_visible_rows_in_prior_check": rq["historical_visible_rows"],
        "issuer_observations_are_historical_registry": False,
        "provider_reply_observed": contact["provider_reply_observed"],
        "source_and_unit_or_PIT_admission": False,
        "original_frozen_bindings": frozen_bindings(),
        "new_economic_experiments": 0, "new_quote_requests": 0,
        "cloud_jobs_started": 0, "raw_market_rows_read_by_this_check": 0,
        "future_real_artifacts_absent": [name for name in FUTURE.values() if not (ROOT / name).is_file()],
    })
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--write-summary", action="store_true")
    args = parser.parse_args(); result = inspect()
    if args.write_summary:
        (ROOT / "research/PB01_PIPELINE_RESULT_20261002.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("current_project_GET_status", "current_stage", "parent_delivery_complete",
        "economic_admitted", "new_economic_experiments", "runtime_or_database_created", "future_real_artifacts_absent")}, ensure_ascii=True))
