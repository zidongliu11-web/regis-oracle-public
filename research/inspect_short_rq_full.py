"""One bounded temporary RQ bundle trial; originals never printed or committed.

Own reader, no rqalpha execution, pickle, account, retry, or economic calculation.
"""
from __future__ import annotations

import argparse
import bz2
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "research/SHORT_RQ_FULL_PROTOCOL_20261001.json"
RECEIPT = ROOT / "research/SHORT_RQ_FULL_RECEIPT_20261001.json"
SAMPLE = Path(os.environ["LOCALAPPDATA"]) / "RegisOracle-local-research/short-rq-20261001/sample.json"
LOCAL_MANIFEST = ROOT / ".codex-local/short_rq_sample_20261001/manifest.json"


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as file:
        for block in iter(lambda: file.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def utc():
    return datetime.now(timezone.utc).isoformat()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ProcessMemory(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
        (key, ctypes.c_size_t) for key in (
            "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
            "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
            "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]


def peak_rss():
    if os.name != "nt":
        raise RuntimeError("This frozen resource meter is Windows-specific")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessMemory), wintypes.DWORD]
    p = ProcessMemory()
    p.cb = ctypes.sizeof(p)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(p), p.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(p.PeakWorkingSetSize)


class Budget:
    def __init__(self, spec, report):
        self.spec, self.report = spec, report
        self.started = time.monotonic()
        self.next_log = self.started + 20

    def check(self, stage):
        elapsed = time.monotonic() - self.started
        peak = peak_rss()
        metrics = self.report.setdefault("resources", {})
        metrics.update(elapsed_seconds=round(elapsed, 3), peak_rss_bytes=peak)
        if elapsed > self.spec["limits"]["batch_seconds"]:
            raise RuntimeError("Batch wall-clock ceiling exceeded")
        if stage == "download" and elapsed > self.spec["limits"]["download_seconds"]:
            raise RuntimeError("Download wall-clock ceiling exceeded")
        if peak > self.spec["limits"]["peak_rss_bytes"]:
            raise RuntimeError("Process peak RSS ceiling exceeded")
        if time.monotonic() >= self.next_log:
            print(json.dumps({"stage": stage, "elapsed_seconds": round(elapsed),
                              "downloaded_bytes": self.report.get("downloaded_bytes", 0),
                              "decompressed_bytes": metrics.get("decompressed_bytes", 0),
                              "peak_rss_bytes": peak}), flush=True)
            self.next_log = time.monotonic() + 20


class ScanReader:
    """Count decompressed bytes, reject large single metadata reads before allocation."""
    def __init__(self, source, budget):
        self.source, self.budget = source, budget
        self.total = 0

    def read(self, size):
        if size < 0 or size > 1048576:
            raise ValueError("Oversize single archive read")
        self.budget.check("extract")
        cap = self.budget.spec["limits"]["decompressed_scan_bytes"]
        if self.total + size > cap:
            raise ValueError("Decompressed scan ceiling exceeded")
        block = self.source.read(size)
        self.total += len(block)
        self.budget.report["resources"]["decompressed_bytes"] = self.total
        return block


def download(spec, report, temp, budget):
    report["GET_invocations"] += 1
    request = urllib.request.Request(spec["url"], headers={
        "User-Agent": "Regis-bounded-local-personal-format-QC",
        "If-Match": spec["expected_ETag"], "Accept-Encoding": "identity"})
    target = temp / "bundle.tar.bz2"
    h = hashlib.sha256()
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
        report.update(http_status=response.status,
                      headers={key: response.headers.get(key) for key in
                               ("ETag", "Content-Length", "Last-Modified", "Content-Type")})
        if response.status != 200 or response.headers.get("ETag") != spec["expected_ETag"]:
            raise ValueError("Frozen object identity changed")
        if response.headers.get("Content-Length") != str(spec["expected_bytes"]):
            raise ValueError("Frozen Content-Length changed")
        with target.open("xb") as output:
            while report["downloaded_bytes"] < spec["expected_bytes"]:
                budget.check("download")
                remaining = spec["expected_bytes"] - report["downloaded_bytes"]
                block = response.read(min(65536, remaining))
                if not block:
                    raise ValueError("Compressed object truncated")
                output.write(block)
                h.update(block)
                report["downloaded_bytes"] += len(block)
            budget.check("download")
            if response.read(1):
                raise ValueError("Compressed object exceeded frozen length")
    report.update(compressed_sha256=h.hexdigest(), compressed_complete=True)
    return target


def extract(spec, report, archive, temp, budget):
    selected = set(spec["selected_members"])
    seen = set()
    extracted = 0
    rows = []
    with bz2.BZ2File(archive, "rb") as source:
        scanner = ScanReader(source, budget)
        with tarfile.open(fileobj=scanner, mode="r|") as tar:
            for info in tar:
                budget.check("extract")
                name = PurePosixPath(info.name)
                if name.is_absolute() or ".." in name.parts or len(name.parts) != 1 or "\\" in info.name or ":" in info.name:
                    raise ValueError("Non-flat or unsafe member path")
                if not info.isfile() or name.name in seen:
                    raise ValueError("Non-regular or duplicate member")
                if len(seen) >= 100 or info.size < 0 or info.size > 4 * 1024**3:
                    raise ValueError("Archive member size/count ceiling")
                seen.add(name.name)
                row = {"name": name.name, "declared_bytes": info.size,
                       "selected": name.name in selected}
                rows.append(row)
                if name.name not in selected:
                    continue
                if extracted + info.size > spec["limits"]["selected_member_bytes"]:
                    raise ValueError("Selected members exceeded disk ceiling")
                stream = tar.extractfile(info)
                if stream is None:
                    raise ValueError("Missing regular member body")
                h, written = hashlib.sha256(), 0
                with (temp / name.name).open("xb") as output:
                    while written < info.size:
                        budget.check("extract")
                        block = stream.read(min(65536, info.size - written))
                        if not block:
                            raise ValueError("Truncated selected member")
                        output.write(block)
                        h.update(block)
                        written += len(block)
                extracted += written
                row["sha256"] = h.hexdigest()
        # Reach BZip2 EOF/CRC, including tar trailing padding. Never admit partial extraction.
        while scanner.read(65536):
            pass
    if not selected <= seen:
        raise ValueError("Required selected member missing")
    report.update(members=rows, archive_complete=True, selected_member_bytes=extracted,
                  peak_temp_data_bytes=report["downloaded_bytes"] + extracted)
    if report["peak_temp_data_bytes"] > spec["limits"]["total_temp_bytes"]:
        raise ValueError("Total temporary disk ceiling exceeded")


def load_dependencies():
    os.environ["HDF5_PLUGIN_PRELOAD"] = "::"
    os.environ["HDF5_PLUGIN_PATH"] = ""
    sys.path.insert(0, str(ROOT / ".codex-local/short_hdf5_reader_dependencies"))
    import h5py
    import numpy as np
    if h5py.__version__ != "3.16.0":
        raise ValueError("Frozen isolated h5py version mismatch")
    # h5py exposes search paths but not H5PLset_loading_state in this wheel.
    # Call the documented HDF5 C API from the very same isolated DLL.
    library = ctypes.CDLL(str(Path(h5py.__file__).parent / "hdf5.dll"))
    library.H5PLset_loading_state.argtypes = [ctypes.c_uint]
    library.H5PLget_loading_state.argtypes = [ctypes.POINTER(ctypes.c_uint)]
    mask = ctypes.c_uint(1)
    if library.H5PLset_loading_state(0) < 0 or library.H5PLget_loading_state(ctypes.byref(mask)) < 0 or mask.value != 0:
        raise RuntimeError("Failed to disable dynamic HDF5 plugins")
    while h5py.h5pl.size():
        h5py.h5pl.remove(0)
    return h5py, np


def qc_one(records, np):
    fields = ("open", "high", "low", "close", "volume", "total_turnover")
    checks = {"nonempty": bool(records), "dates_unique_sorted":
              [r["trading_date"] for r in records] == sorted({r["trading_date"] for r in records})}
    checks["finite"] = all(np.isfinite(r[f]) for r in records for f in fields)
    checks["OHLC_positive_ordered"] = all(0 < r["low"] <= min(r["open"], r["close"]) <= max(r["open"], r["close"]) <= r["high"] for r in records)
    checks["volume_turnover_nonnegative"] = all(r["volume"] >= 0 and r["total_turnover"] >= 0 for r in records)
    return checks


def read_calendar(path, np):
    # Inspect bounded NPY header before numpy allocates the payload.
    with Path(path).open("rb") as source:
        version = np.lib.format.read_magic(source)
        if version == (1, 0):
            shape, _, dtype = np.lib.format.read_array_header_1_0(source, max_header_size=4096)
        elif version == (2, 0):
            shape, _, dtype = np.lib.format.read_array_header_2_0(source, max_header_size=4096)
        else:
            raise ValueError("Unsupported NPY header version")
        if len(shape) != 1 or not 0 < shape[0] <= 30000 or dtype.kind not in "iu" or dtype.itemsize > 8 or dtype.hasobject or dtype.subdtype:
            raise ValueError("Invalid/oversize calendar header")
        if Path(path).stat().st_size != source.tell() + shape[0] * dtype.itemsize:
            raise ValueError("Calendar payload length mismatch")
    return np.load(path, allow_pickle=False)


def validate_bar_dtype(dtype, fields):
    if dtype.hasobject or dtype.itemsize > 256 or not set(fields) <= set(dtype.names or ()):
        raise ValueError("Invalid/oversize bar dtype")
    for field in fields:
        scalar = dtype.fields[field][0]
        if scalar.subdtype or scalar.fields or scalar.itemsize > 8 or scalar.kind not in ("iu" if field == "datetime" else "iuf"):
            raise ValueError("Invalid/oversize scalar bar field")


def sample_rows(spec, report, temp, budget):
    h5py, np = load_dependencies()
    budget.check("sample")
    calendar = read_calendar(temp / "trading_dates.npy", np)
    budget.check("sample")
    dates = [int(d) for d in calendar if spec["start_date"] <= int(d) <= spec["end_date"]]
    if dates != sorted(set(dates)) or not dates:
        raise ValueError("Calendar window missing/invalid")
    all_rows, summaries = [], []
    for selection in spec["selection"]:
        budget.check("sample")
        with h5py.File(temp / selection["member"], "r", rdcc_nbytes=1048576) as file:
            budget.check("sample")
            key = selection["order_book_id"]
            if not isinstance(file.get(key, getlink=True), h5py.HardLink):
                raise ValueError("Missing dataset or external/soft link")
            dataset = file[key]
            if not isinstance(dataset, h5py.Dataset) or dataset.is_virtual or dataset.external:
                raise ValueError("Unsupported HDF5 dataset storage")
            if dataset.ndim != 1 or dataset.shape[0] > 30000 or dataset.dtype.hasobject:
                raise ValueError("Dataset shape/object limit")
            validate_bar_dtype(dataset.dtype, spec["fields"])
            if dataset.chunks and int(np.prod(dataset.chunks)) * dataset.dtype.itemsize > 64 * 1024**2:
                raise ValueError("Oversize HDF5 decompression chunk")
            dcpl = dataset.id.get_create_plist()
            filters = [dcpl.get_filter(i)[0] for i in range(dcpl.get_nfilters())]
            if not set(filters) <= {1, 2, 3, 4, 5, 6}:
                raise ValueError("Unknown/custom HDF5 filter")
            # Date column only: no historical full-price array. Prices read <=50 selected rows.
            times = dataset.fields("datetime")[:]
            budget.check("sample")
            normalized = np.where(times > 100000000, times // 1000000, times)
            if np.any(np.diff(normalized.astype(np.int64)) <= 0):
                raise ValueError("Historical date-column duplicate/order violation")
            offsets = np.flatnonzero((normalized >= spec["start_date"]) & (normalized <= spec["end_date"]))
            if len(all_rows) + len(offsets) > spec["limits"]["sample_rows"]:
                raise ValueError("Preselected sample row ceiling")
            records = []
            for index in offsets:
                budget.check("sample")
                item = dataset.fields(spec["fields"])[int(index)]
                budget.check("sample")
                row = {"order_book_id": key, "trading_date": int(normalized[index])}
                row.update({field: float(item[field]) for field in spec["fields"] if field != "datetime"})
                records.append(row)
            checks = qc_one(records, np)
            checks["matches_bundle_calendar"] = [r["trading_date"] for r in records] == dates
            summaries.append({"order_book_id": key, "rows": len(records),
                              "dates": [r["trading_date"] for r in records],
                              "dataset_shape": list(dataset.shape), "dtype": dataset.dtype.descr,
                              "filters": filters, "checks": checks})
            all_rows.extend(records)
    SAMPLE.parent.parent.mkdir(exist_ok=True)
    SAMPLE.parent.mkdir(exist_ok=False)
    SAMPLE.write_text(json.dumps(all_rows, allow_nan=False) + "\n", encoding="utf-8")
    LOCAL_MANIFEST.parent.mkdir(exist_ok=False)
    LOCAL_MANIFEST.write_text(json.dumps({"sample_path": str(SAMPLE), "sha256": digest(SAMPLE),
                                          "rows": len(all_rows)}, indent=2) + "\n", encoding="utf-8")
    report.update(sample_path=str(SAMPLE), sample_sha256=digest(SAMPLE),
                  sample_bytes=SAMPLE.stat().st_size, sample_rows=len(all_rows),
                  sample_summaries=summaries, actual_sample_acquired=bool(all_rows),
                  structural_qc_passed=all(all(row["checks"].values()) for row in summaries))
    if not report["structural_qc_passed"]:
        raise ValueError("Actual structural/calendar sample QC failed")


def verify(spec):
    report = json.loads(RECEIPT.read_text(encoding="utf-8"))
    lineage = json.loads((ROOT / "research/SHORT_RQ_READER_REPAIR_20261001.json").read_text(encoding="utf-8"))
    if report["protocol_sha256"] != digest(PROTOCOL) or report["implementation_sha256"] != digest(ROOT / lineage["capture_implementation_path"]):
        raise ValueError("Protocol/reader identity changed")
    if digest(__file__) != lineage["repaired_implementation_sha256"]:
        raise ValueError("Repaired offline reader identity changed")
    for path, expected in spec["source_hashes"].items():
        if digest(ROOT / path) != expected:
            raise ValueError("Official source input changed")
    if not report.get("actual_sample_acquired") or "failure" in report:
        raise ValueError("Real bounded sample not acquired")
    if digest(SAMPLE) != report["sample_sha256"]:
        raise ValueError("Local sample identity changed")
    _, np = load_dependencies()
    records = json.loads(SAMPLE.read_text(encoding="utf-8"))
    for summary in report["sample_summaries"]:
        subset = [r for r in records if r["order_book_id"] == summary["order_book_id"]]
        if len(subset) != summary["rows"] or not all(qc_one(subset, np).values()):
            raise ValueError("Replay structural QC failed")
    if report["cleanup"]["remaining_large_files"] != 0 or Path(report["temp_dir"]).exists():
        raise ValueError("Temporary full files not cleaned")
    print(json.dumps({"identities_match": True, "sample_rows": len(records),
                      "structural_qc_replayed": True, "full_N4_passed": False}))


def main():
    args = argparse.ArgumentParser()
    group = args.add_mutually_exclusive_group(required=True)
    group.add_argument("--capture", action="store_true")
    group.add_argument("--verify", action="store_true")
    options = args.parse_args()
    spec = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    if options.verify:
        verify(spec)
        return
    attempt = ROOT / ".codex-local/short_rq_full_attempt_started_20261001.json"
    if RECEIPT.exists() or SAMPLE.parent.exists() or attempt.exists():
        raise ValueError("Trial already attempted; no implicit retries")
    if "onedrive" in str(SAMPLE).lower() or SAMPLE.is_relative_to(ROOT):
        raise ValueError("Sample must also stay outside workspace and OneDrive")
    if subprocess.run(["git", "check-ignore", str(LOCAL_MANIFEST.relative_to(ROOT))], cwd=ROOT, capture_output=True).returncode != 0:
        raise ValueError("Local nonraw sample manifest must be ignored")
    for path, expected in spec["source_hashes"].items():
        if digest(ROOT / path) != expected:
            raise ValueError("Predeclared official source identity mismatch")
    temp_base = Path(tempfile.gettempdir()).resolve()
    if shutil.disk_usage(temp_base).free < spec["limits"]["minimum_free_disk_bytes"]:
        raise ValueError("Insufficient temporary disk headroom")
    temp = temp_base / ("regis-short-rq-20261001-" + uuid.uuid4().hex)
    if "onedrive" in str(temp).lower() or temp.is_relative_to(ROOT):
        raise ValueError("Original temp data must be outside workspace and OneDrive")
    temp.mkdir(exist_ok=False)
    report = {"schema": "regis.short_rq_full_receipt.v1", "started_utc": utc(),
              "protocol_sha256": digest(PROTOCOL), "implementation_sha256": digest(__file__),
              "temp_dir": str(temp), "GET_invocations": 0, "downloaded_bytes": 0,
              "resources": {"elapsed_seconds": 0, "peak_rss_bytes": 0, "decompressed_bytes": 0},
              "sample_rows": 0, "actual_sample_acquired": False,
              "free_disk_preflight_bytes": shutil.disk_usage(temp_base).free,
              "full_N4_passed": False, "economic_run": False, "model_admitted": False}
    task = json.loads((ROOT / "WORKFLOW_TASK.json").read_text(encoding="utf-8"))
    report["capture_contract_sha256"] = task["contract_sha256"]
    attempt.write_text(json.dumps({"temp_dir": str(temp), "started_utc": report["started_utc"],
                                  "protocol_sha256": report["protocol_sha256"],
                                  "capture_contract_sha256": task["contract_sha256"]}, indent=2) + "\n", encoding="utf-8")
    budget = Budget(spec, report)
    try:
        budget.check("preflight")
        archive = download(spec, report, temp, budget)
        extract(spec, report, archive, temp, budget)
        sample_rows(spec, report, temp, budget)
        budget.check("finish")
    except Exception as error:
        # Error messages carry no row values; source bytes stay local.
        report["failure"] = {"type": type(error).__name__, "reason": str(error)}
    finally:
        # Own unique flat directory only, verify every resolved target before unlink.
        deleted, cleanup_failure = [], None
        try:
            for path in temp.iterdir():
                if path.is_symlink() or path.resolve().parent != temp.resolve() or not path.is_file():
                    raise ValueError("Unsafe cleanup target")
                entry = {"name": path.name, "bytes": path.stat().st_size}
                path.unlink()
                deleted.append(entry)
            temp.rmdir()
        except Exception as error:
            cleanup_failure = {"type": type(error).__name__, "reason": str(error)}
            if "failure" not in report:
                report["failure"] = {"type": "CleanupFailure", "reason": "Original temporary files remain; inspect cleanup record"}
        try:
            remaining = len(list(temp.iterdir())) if temp.exists() else 0
        except Exception as error:
            remaining = -1  # unknown; never report missing enumeration as successful cleanup
            cleanup_failure = {"type": type(error).__name__, "reason": str(error)}
        report["cleanup"] = {"deleted": deleted, "remaining_large_files": remaining,
                             "temp_dir_absent": not temp.exists(), "failure": cleanup_failure}
        report["ended_utc"] = utc()
        report["resources"]["elapsed_seconds"] = round(time.monotonic() - budget.started, 3)
        RECEIPT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"sample_rows": report["sample_rows"], "structural_qc_passed": report.get("structural_qc_passed", False),
                      "downloaded_bytes": report["downloaded_bytes"], "resources": report["resources"],
                      "cleaned_large_files": report["cleanup"]["remaining_large_files"] == 0 and report["cleanup"]["temp_dir_absent"], "failure": report.get("failure"),
                      "full_N4_passed": False}), flush=True)
    if "failure" in report:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
