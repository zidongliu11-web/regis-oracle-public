"""Retain an actual multi-year ETF outcome dataset locally; never print raw quotes.

Reuses the bounded archive transport/reader, without running the RQAlpha SDK.
This acquisition is separate from the original 42-row QC protocol.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import time

import inspect_short_rq_full as base

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "research/RQ_ETF_HISTORY_RECOVERY_PROTOCOL_20261002.json"
RECEIPT = ROOT / "research/RQ_ETF_HISTORY_RECOVERY_RECEIPT_20261002.json"
REPAIR = ROOT / "research/RQ_ETF_HISTORY_READER_REPAIR_20261002.json"


def safe_dataset(file, key, h5py, np):
    if not isinstance(file.get(key, getlink=True), h5py.HardLink):
        raise ValueError("Selected dataset absent or non-hard link")
    ds = file[key]
    if not isinstance(ds, h5py.Dataset) or ds.is_virtual or ds.external:
        raise ValueError("Unsupported dataset storage")
    if ds.ndim != 1 or ds.shape[0] > 30000 or ds.dtype.hasobject or ds.dtype.itemsize > 256:
        raise ValueError("Selected dataset shape/type ceiling")
    if ds.chunks and int(np.prod(ds.chunks)) * ds.dtype.itemsize > 64 * 1024**2:
        raise ValueError("Oversized decompression chunk")
    cp = ds.id.get_create_plist()
    if not {cp.get_filter(i)[0] for i in range(cp.get_nfilters())} <= {1, 2, 3, 4, 5, 6}:
        raise ValueError("Unknown HDF5 filter")
    return ds


def inspect(spec, local, budget):
    h5py, np = base.load_dependencies()
    key = spec["order_book_id"]
    calendar = base.read_calendar(local / "trading_dates.npy", np)
    cal = np.array([int(d) for d in calendar if spec["start_date"] <= int(d) <= spec["end_date"]])
    if len(cal) == 0 or np.any(np.diff(cal) <= 0):
        raise ValueError("Invalid selected calendar")
    output = {"order_book_id": key, "calendar_sessions": len(cal), "members": {}}
    fields = ["datetime", "open", "high", "low", "close", "volume", "total_turnover"]
    with h5py.File(local / "funds.h5", "r", rdcc_nbytes=1048576) as file:
        ds = safe_dataset(file, key, h5py, np)
        base.validate_bar_dtype(ds.dtype, fields)
        times = ds.fields("datetime")[:]
        dates = np.where(times > 100000000, times // 1000000, times)
        if np.any(np.diff(dates.astype(np.int64)) <= 0):
            raise ValueError("Duplicate/unsorted historical dates")
        offsets = np.flatnonzero((dates >= spec["start_date"]) & (dates <= spec["end_date"]))
        selected = ds.fields(fields)[offsets]
        finite = all(bool(np.isfinite(selected[f]).all()) for f in fields)
        positive = bool((selected["low"] > 0).all())
        ordered = bool(((selected["low"] <= np.minimum(selected["open"], selected["close"])) &
                        (np.maximum(selected["open"], selected["close"]) <= selected["high"])).all())
        summary = {"stored_rows": int(ds.shape[0]), "selected_rows": len(offsets),
                   "first_date": int(dates[offsets[0]]) if len(offsets) else None,
                   "last_date": int(dates[offsets[-1]]) if len(offsets) else None,
                   "dtype": ds.dtype.descr, "finite": finite, "positive": positive,
                   "OHLC_ordered": ordered,
                   "missing_calendar_dates": sorted(set(map(int, cal)) - set(map(int, dates[offsets]))),
                   "extra_calendar_dates": sorted(set(map(int, dates[offsets])) - set(map(int, cal))),
                   "annual_rows": {str(y): int(((dates[offsets] // 10000) == y).sum())
                                   for y in range(spec["start_date"] // 10000, spec["end_date"] // 10000 + 1)}}
        output["members"]["funds.h5"] = summary
        if not len(offsets) or not finite or not positive or not ordered:
            raise ValueError("Historical price structural check failed")
    for member in ("dividends.h5", "split_factor.h5", "ex_cum_factor.h5"):
        budget.check("history-inspection")
        with h5py.File(local / member, "r", rdcc_nbytes=1048576) as file:
            if key not in file:
                output["members"][member] = {"selected_key_present": False,
                                            "absence_does_not_prove_no_action": True}
                continue
            ds = safe_dataset(file, key, h5py, np)
            for field in ds.dtype.names or ():
                dt = ds.dtype.fields[field][0]
                if dt.hasobject or dt.subdtype or dt.fields or dt.kind not in "iuf" or dt.itemsize > 8:
                    raise ValueError("Unsupported action scalar field")
            records = ds[:]
            date_fields = [f for f in ds.dtype.names or () if "date" in f]
            output["members"][member] = {"selected_key_present": True,
                                        "stored_rows": len(records), "dtype": ds.dtype.descr,
                                        "dates": {f: [int(x) for x in records[f]] for f in date_fields},
                                        "finite": all(bool(np.isfinite(records[f]).all()) for f in ds.dtype.names or ())}
    output["price_structural_passed"] = not summary["missing_calendar_dates"] and not summary["extra_calendar_dates"]
    output["not_certified"] = ["historical publication vintage", "minute execution", "all-A signal inputs",
                               "total-return accounting", "source units semantic admission", "strategy admission"]
    return output


def finish_receipt(report, local, budget):
    """A cleanup failure must not leave only the initial running receipt."""
    archive = local / "bundle.tar.bz2"
    removed = False
    try:
        if archive.exists() or archive.is_symlink():
            if archive.is_symlink() or archive.resolve().parent != local:
                raise ValueError("Unsafe temporary archive cleanup path")
            archive.unlink()
        removed = not archive.exists() and not archive.is_symlink()
    except Exception as error:
        report["cleanup_failure"] = {"type": type(error).__name__,
                                     "reason": "temporary archive cleanup failed; original retained for inspection"}
        report["state"] = "failed"
        report.setdefault("failure", {"type": "CleanupFailure", "reason": "see cleanup_failure"})
    finally:
        report["temporary_archive_removed"] = removed
        report["ended_utc"] = base.utc()
        report["resources"]["elapsed_seconds"] = round(time.monotonic() - budget.started, 3)
        RECEIPT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def capture(spec):
    if RECEIPT.exists():
        existing = json.loads(RECEIPT.read_text(encoding="utf-8"))
        if existing.get("state") != "not_run":
            raise ValueError("Acquisition already attempted; use verify")
    local = Path(spec["retained_directory"]).resolve()
    allowed = (Path(os.environ["LOCALAPPDATA"]) / "RegisOracle-local-research").resolve()
    if not local.is_relative_to(allowed) or "onedrive" in str(local).lower() or local.exists():
        raise ValueError("Invalid/already existing acquisition directory")
    for path, expected in spec["source_hashes"].items():
        if base.digest(ROOT / path) != expected:
            raise ValueError("Source input version changed")
    if shutil.disk_usage(allowed).free < spec["limits"]["minimum_free_disk_bytes"]:
        raise ValueError("Insufficient disk headroom")
    local.mkdir(parents=True, exist_ok=False)
    report = {"schema": "regis.rq_etf_history_recovery.v1", "state": "running", "started_utc": base.utc(),
              "protocol_sha256": base.digest(PROTOCOL), "implementation_sha256": base.digest(__file__),
              "base_reader_sha256": base.digest(base.__file__), "GET_invocations": 0, "downloaded_bytes": 0,
              "raw_directory": str(local), "economic_run": False, "model_admitted": False,
              "capture_contract_sha256": json.loads((ROOT / "WORKFLOW_TASK.json").read_text(encoding="utf-8"))["contract_sha256"],
              "resources": {"elapsed_seconds": 0, "peak_rss_bytes": 0, "decompressed_bytes": 0}}
    RECEIPT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    budget = base.Budget(spec, report)
    try:
        archive = base.download(spec, report, local, budget)
        if report["compressed_sha256"] != spec["expected_compressed_sha256"]:
            raise ValueError("Observed archive differs from previous object")
        base.extract(spec, report, archive, local, budget)
        for item in report["members"]:
            if item["selected"] and item["sha256"] != spec["expected_member_sha256"].get(item["name"], item["sha256"]):
                raise ValueError("Observed selected member differs from previous object")
        report["history_inspection"] = inspect(spec, local, budget)
        report["retained"] = [{"name": p.name, "bytes": p.stat().st_size, "sha256": base.digest(p)}
                              for p in sorted(local.iterdir()) if p.name in spec["selected_members"]]
        report["state"] = "captured"
    except Exception as error:
        report["state"] = "failed"
        report["failure"] = {"type": type(error).__name__, "reason": str(error)}
    finally:
        # Keep selected history for subsequent local research; remove our compressed temporary object only.
        finish_receipt(report, local, budget)
    print(json.dumps({"state": report["state"], "downloaded_bytes": report["downloaded_bytes"],
                      "history_inspection": report.get("history_inspection"), "failure": report.get("failure"),
                      "raw_quotes_printed": False, "economic_run": False}))
    if report["state"] != "captured":
        raise SystemExit(1)


def verify(spec):
    report = json.loads(RECEIPT.read_text(encoding="utf-8"))
    if report.get("state") != "captured" or report["protocol_sha256"] != base.digest(PROTOCOL):
        raise ValueError("No successful matching acquisition")
    capture_sha = base.digest(__file__)
    if REPAIR.exists():
        repair = json.loads(REPAIR.read_text(encoding="utf-8"))
        original = ROOT / repair["capture_original_path"]
        if not original.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError("Capture lineage path escapes workspace")
        capture_sha = base.digest(original)
        if capture_sha != repair["capture_original_sha256"] or base.digest(__file__) != repair["repaired_reader_sha256"]:
            raise ValueError("Repaired reader lineage changed")
    if report["implementation_sha256"] != capture_sha or report["base_reader_sha256"] != base.digest(base.__file__):
        raise ValueError("Reader version changed")
    for item in report["retained"]:
        path = Path(spec["retained_directory"]) / item["name"]
        if path.stat().st_size != item["bytes"] or base.digest(path) != item["sha256"]:
            raise ValueError("Retained member identity changed")
    if not report["temporary_archive_removed"] or report["economic_run"] or report["model_admitted"]:
        raise ValueError("Invalid acquisition scope")
    print(json.dumps({"verified_retained_members": len(report["retained"]),
                      "history_inspection": report["history_inspection"], "economic_run": False}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--capture", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    spec = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    capture(spec) if args.capture else verify(spec)
