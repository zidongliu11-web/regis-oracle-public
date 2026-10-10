"""Specific post-review failure probes. Synthetic faults, zero native requests."""
from __future__ import annotations
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]


def main():
    module_spec = importlib.util.spec_from_file_location("reader", ROOT / "research/inspect_short_rq_full.py")
    reader = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(reader)
    _, np = reader.load_dependencies()
    original = json.loads(reader.PROTOCOL.read_text(encoding="utf-8"))
    checks = {}
    temp_base = Path(tempfile.gettempdir()).resolve()
    with tempfile.TemporaryDirectory(prefix="regis-reader-faults-", dir=temp_base) as location:
        base = Path(location).resolve()
        assert base.parent == temp_base and base.name.startswith("regis-reader-faults-")
        runtime = base / "runtime"
        runtime.mkdir()
        p = base / "oversize.npy"
        with p.open("wb") as file:
            np.lib.format.write_array_header_1_0(file, {"descr": "<i8", "fortran_order": False, "shape": (30001,)})
        with patch.object(np, "load", side_effect=AssertionError("must not allocate")) as load:
            try:
                reader.read_calendar(p, np)
                checks["NPY_oversize_rejected_before_allocation"] = False
            except ValueError:
                checks["NPY_oversize_rejected_before_allocation"] = load.call_count == 0
        with p.open("wb") as file:
            np.lib.format.write_array_header_1_0(file, {"descr": "|O", "fortran_order": False, "shape": (1,)})
        with patch.object(np, "load", side_effect=AssertionError("must not load pickle")) as load:
            try:
                reader.read_calendar(p, np)
                checks["NPY_object_rejected_before_load"] = False
            except ValueError:
                checks["NPY_object_rejected_before_load"] = load.call_count == 0
        good = np.dtype([(key, "<i8" if key == "datetime" else "<f8") for key in original["fields"]])
        reader.validate_bar_dtype(good, original["fields"])
        bad = np.dtype([(key, "S1048576" if key == "datetime" else "<f8") for key in original["fields"]])
        try:
            reader.validate_bar_dtype(bad, original["fields"])
            checks["HDF_date_nonscalar_large_dtype_rejected"] = False
        except ValueError:
            checks["HDF_date_nonscalar_large_dtype_rejected"] = True
        for fault in ("HTTP412", "cleanup_permission"):
            fixture = base / fault
            fixture.mkdir()
            (fixture / ".codex-local").mkdir()
            (fixture / "WORKFLOW_TASK.json").write_text(json.dumps({"contract_sha256": "synthetic-fault-test-only"}), encoding="utf-8")
            protocol = fixture / "protocol.json"
            spec = dict(original, source_hashes={})
            protocol.write_text(json.dumps(spec), encoding="utf-8")
            receipt, sample = fixture / "receipt.json", base / ("sample-" + fault) / "sample.json"
            def failed_download(spec, report, temp, budget):
                if fault == "cleanup_permission":
                    (temp / "synthetic_partial").write_bytes(b"synthetic fault fixture")
                raise urllib.error.HTTPError(spec["url"], 412, "synthetic identity fault; no request", {}, None)
            original_unlink = Path.unlink
            def failing_unlink(path, *args, **kwargs):
                if path.name == "synthetic_partial":
                    raise PermissionError("synthetic cleanup permission fault")
                return original_unlink(path, *args, **kwargs)
            with patch.object(reader, "ROOT", fixture), patch.object(reader, "PROTOCOL", protocol), \
                 patch.object(reader, "RECEIPT", receipt), patch.object(reader, "SAMPLE", sample), \
                 patch.object(reader, "LOCAL_MANIFEST", fixture / ".codex-local/sample/manifest.json"), \
                 patch.object(reader.tempfile, "gettempdir", return_value=str(runtime)), \
                 patch.object(reader.subprocess, "run") as run, patch.object(reader, "download", side_effect=failed_download), \
                 patch.object(reader.sys, "argv", ["reader", "--capture"]), \
                 patch.object(Path, "unlink", failing_unlink), redirect_stdout(io.StringIO()):
                run.return_value.returncode = 0
                try:
                    reader.main()
                except SystemExit as error:
                    assert error.code == 1
            report = json.loads(receipt.read_text(encoding="utf-8"))
            checks[f"{fault}_failure_receipt_preserved"] = report["failure"]["type"] == "HTTPError" and "resources" in report
            if fault == "cleanup_permission":
                checks["cleanup_failure_separately_recorded"] = bool(report["cleanup"].get("failure")) and report["cleanup"]["remaining_large_files"] == 1
        # Verify the resolved test root before TemporaryDirectory's recursive cleanup.
        assert base.parent == temp_base and base.name.startswith("regis-reader-faults-")
    if not all(checks.values()):
        raise AssertionError("Reader failure repair probe failed")
    path = ROOT / ".codex-local/short_rq_reader_fault_result_20261001.json"
    path.write_text(json.dumps({"kind": "synthetic_adverse_path_only", "native_requests": 0,
                                "quote_rows_read": 0, "checks": checks}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"native_requests": 0, "quote_rows_read": 0, "fault_checks": checks}))


if __name__ == "__main__":
    main()
