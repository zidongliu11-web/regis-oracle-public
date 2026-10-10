"""Bounded, source-bound read of an already built native historical aggregate."""
import hashlib
import json
from pathlib import Path

from mom_trading.local_research_history import _signature
from research.build_local_market_history import RECEIPT, validate_summary

RECEIPT_SHA = "474f45e6eca15a0cf2981787c9eeb0e192b06bb84ff872783fc3b0b94cc4adc6"
CAPTURE_IMPLEMENTATION = "research/LOCAL_MARKET_HISTORY_CAPTURE_V1_20261004.py"


def load_summary(root, source):
    if source is None: raise OSError("stock_snapshot_unavailable")
    root = Path(root).resolve()
    receipt_raw = (root / RECEIPT).read_bytes()
    if hashlib.sha256(receipt_raw).hexdigest() != RECEIPT_SHA: raise ValueError("receipt_changed")
    receipt = json.loads(receipt_raw)
    if receipt["state"] != "captured" or receipt["source_sha256"] != source.sha:
        raise ValueError("source_identity_changed")
    for key, path in (("generator_sha256", CAPTURE_IMPLEMENTATION),
                      ("source_reader_sha256", "mom_trading/local_research_history.py")):
        if hashlib.sha256((root / path).read_bytes()).hexdigest() != receipt[key]: raise ValueError("implementation_changed")
    if _signature(source.path) != source.signature or _signature(source.calendar_path) != source.calendar_signature:
        raise ValueError("source_changed")
    if hashlib.sha256(source.calendar_path.read_bytes()).hexdigest() != receipt["calendar_sha256"]:
        raise ValueError("calendar_identity_changed")
    path = Path(receipt["native_path"])
    if (path.is_symlink() or not path.resolve().is_relative_to(source.path.parent.resolve())
            or path.resolve().is_relative_to(root) or any("onedrive" in p.lower() for p in path.resolve().parts)):
        raise ValueError("summary_location_invalid")
    with path.open("rb") as stream: raw = stream.read(256001)
    if len(raw) > 256000 or len(raw) != receipt["summary_bytes"] or hashlib.sha256(raw).hexdigest() != receipt["summary_sha256"]:
        raise ValueError("summary_changed")
    data = json.loads(raw); validate_summary(data)
    if (data["source_sha256"] != source.sha or len(data["days"]) != receipt["sessions"]
            or data["included_datasets"] != receipt["included_datasets"]
            or data["start_date"] != receipt["start_date"] or data["end_date"] != receipt["end_date"]):
        raise ValueError("summary_identity_invalid")
    if _signature(source.path) != source.signature or _signature(source.calendar_path) != source.calendar_signature:
        raise ValueError("source_changed")
    return {**data, "summary_sha256": receipt["summary_sha256"], "receipt_sha256": RECEIPT_SHA,
            "new_economic_runs": 0, "sector_membership_available": False}
