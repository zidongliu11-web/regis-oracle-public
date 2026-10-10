"""Offline bounded delivery checks; no raw rows or economic output."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
INPUT_COMMIT = "7ada8fc63804afc08a9e4abb4e527a3e43083c98"
FROZEN = ["quantqc/costs.py", "research/m24_accounting.py", "research/m24_data_adapter.py",
          "QUANT_RESEARCH_M23_PROTOCOL_20260930.json", "QUANT_RESEARCH_M24_PROTOCOL_20261001.json"]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    manifests = sorted((ROOT / "workflow_archive/QUANT-SHORT-DATA-20261001-001").glob("*/MANIFEST.json"))
    checked = 0
    for path in manifests:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        entries = data if isinstance(data, list) else data["files"]
        for row in entries:
            relative = row.get("archive_path", row.get("path"))
            target = (ROOT / relative).resolve()
            if not target.is_relative_to(ROOT) or sha(target) != row["sha256"]:
                raise ValueError("Preserved original identity mismatch")
            checked += 1
    for relative in FROZEN:
        source = subprocess.run(["git", "show", INPUT_COMMIT + ":" + relative],
                                cwd=ROOT, capture_output=True, check=True).stdout
        if hashlib.sha256(source).hexdigest() != sha(ROOT / relative):
            raise ValueError("Prior research/accounting freeze changed")
    report = json.loads((ROOT / "research/SHORT_RQ_FULL_RECEIPT_20261001.json").read_text(encoding="utf-8"))
    spec = json.loads((ROOT / "research/SHORT_RQ_FULL_PROTOCOL_20261001.json").read_text(encoding="utf-8"))
    if not report.get("actual_sample_acquired") or not report.get("structural_qc_passed") or report.get("failure"):
        raise ValueError("No successful actual limited sample")
    if report["GET_invocations"] != 1 or report["downloaded_bytes"] != spec["expected_bytes"]:
        raise ValueError("Original native invocation/byte mismatch")
    metrics = report["resources"]
    if (report["sample_rows"] > 50 or metrics["peak_rss_bytes"] > spec["limits"]["peak_rss_bytes"] or
        metrics["elapsed_seconds"] > spec["limits"]["batch_seconds"] or
        metrics["decompressed_bytes"] > spec["limits"]["decompressed_scan_bytes"] or
        report["peak_temp_data_bytes"] > spec["limits"]["total_temp_bytes"]):
        raise ValueError("Observed resource bound not met")
    if report["full_N4_passed"] or report["economic_run"] or report["model_admitted"]:
        raise ValueError("Format trial incorrectly admits whole research")
    if report["cleanup"]["remaining_large_files"] or Path(report["temp_dir"]).exists():
        raise ValueError("Original large temp files remain")
    lineage = json.loads((ROOT / "research/SHORT_RQ_READER_REPAIR_20261001.json").read_text(encoding="utf-8"))
    if report["implementation_sha256"] != sha(ROOT / lineage["capture_implementation_path"]):
        raise ValueError("Capture implementation provenance lost")
    if report["capture_contract_sha256"] != json.loads((ROOT / lineage["capture_task_path"]).read_text(encoding="utf-8"))["contract_sha256"]:
        raise ValueError("Capture contract provenance lost")
    if not all(lineage["offline_fault_checks"].values()):
        raise ValueError("Reader fault repairs not verified")
    fault_path = ROOT / lineage["fault_result_path"]
    fault = json.loads(fault_path.read_text(encoding="utf-8"))
    if (sha(fault_path) != lineage["fault_result_sha256"] or fault["checks"] != lineage["offline_fault_checks"] or
        fault["native_requests"] != 0 or fault["quote_rows_read"] != 0 or
        sha(ROOT / "research/test_short_rq_reader_faults.py") != lineage["fault_test_script_sha256"] or
        sha(ROOT / "research/inspect_short_rq_full.py") != lineage["repaired_implementation_sha256"]):
        raise ValueError("Actual offline fault evidence identity mismatch")
    local = json.loads((ROOT / ".codex-local/short_rq_sample_20261001/manifest.json").read_text(encoding="utf-8"))
    sample = Path(local["sample_path"])
    if "onedrive" in str(sample).lower() or sample.is_relative_to(ROOT) or sha(sample) != local["sha256"]:
        raise ValueError("Sample custody/identity mismatch")
    if subprocess.run(["git", "ls-files", "--error-unmatch", ".codex-local/short_rq_sample_20261001/manifest.json"], cwd=ROOT, capture_output=True).returncode == 0:
        raise ValueError("Ignored local manifest unexpectedly tracked")
    print(json.dumps({"archive_originals_checked": checked, "archive_manifests": len(manifests),
                      "frozen_original_files": len(FROZEN), "actual_sample_rows": report["sample_rows"],
                      "observed_resource_bounds_met": True, "large_files_cleaned": True,
                      "raw_sample_outside_OneDrive_and_Git": True, "reader_fault_checks_passed": True,
                      "full_N4_passed": False, "economic_or_model_admission": False}))


if __name__ == "__main__":
    main()
