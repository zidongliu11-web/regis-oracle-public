"""One disclosed ETF action document, not a market replay or a source licence."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import time
import tracemalloc
import urllib.request
from datetime import datetime, timezone

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "research/GOAL_ETF_ACTION_PROTOCOL_20261001.json"
RECEIPT = ROOT / "research/GOAL_ETF_ACTION_RECEIPT_20261001.json"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def inspect(raw, protocol):
    if not raw.startswith(b"%PDF-") or len(raw) > protocol["maximum_pdf_bytes"]:
        raise ValueError("Expected bounded original PDF, not an HTML error response")
    reader = PdfReader(io.BytesIO(raw), strict=True)
    if reader.is_encrypted or not 1 <= len(reader.pages) <= protocol["maximum_pages"]:
        raise ValueError("Encrypted or oversized document")
    pages = [page.extract_text() or "" for page in reader.pages]
    if sum(map(len, pages)) > protocol["maximum_text_characters"]:
        raise ValueError("Text limit exceeded")
    joined = "".join(pages)
    compact = "".join(joined.split())
    if not all(marker in compact for marker in protocol["required_markers"]):
        raise ValueError("Document identity/action fields not confirmed")
    return {"page_count": len(pages), "page_text_characters": list(map(len, pages)),
            "text_sha256": sha(joined.encode("utf-8")), "required_markers_present": True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.collect == args.verify:
        parser.error("Choose exactly one mode")
    protocol_bytes = PROTOCOL.read_bytes()
    protocol = json.loads(protocol_bytes)
    code_sha = sha(Path(__file__).read_bytes())
    if code_sha != protocol["script_sha256"]:
        raise ValueError("Collector changed after protocol freeze")
    raw_path = ROOT / protocol["raw_relative_path"]
    if args.verify:
        receipt = json.loads(RECEIPT.read_bytes())
        raw = raw_path.read_bytes()
        if receipt["protocol_sha256"] != sha(protocol_bytes) or receipt["script_sha256"] != code_sha:
            raise ValueError("Stale protocol/collector receipt")
        if receipt["raw_sha256"] != sha(raw) or receipt["raw_bytes"] != len(raw):
            raise ValueError("Original bytes changed")
        if receipt["document_structure"] != inspect(raw, protocol):
            raise ValueError("Document structure differs")
        if receipt["economic_run"] or receipt["complete_action_coverage"] or receipt["full_N4"]:
            raise ValueError("A single document cannot admit economic/full source use")
        print(json.dumps({"status": "single_action_document_identity_verified", "full_N4": False,
                          "economic_run": False, "raw_sha256": receipt["raw_sha256"]}))
        return
    if raw_path.exists() or RECEIPT.exists():
        raise FileExistsError("One-time collection already exists; do not repeat retrieval")
    started = datetime.now(timezone.utc).isoformat()
    tic = time.perf_counter()
    tracemalloc.start()
    request = urllib.request.Request(protocol["url"], headers={"User-Agent": "Regis-Oracle-single-public-document", "Accept": "application/pdf"})
    with urllib.request.urlopen(request, timeout=protocol["request_timeout_seconds"]) as response:
        if response.geturl() != protocol["url"] or response.status != 200:
            raise ValueError("Unexpected response or redirect; do not bypass")
        raw = response.read(protocol["maximum_pdf_bytes"] + 1)
        headers = {k: response.headers.get(k) for k in ("Date", "Content-Type", "Last-Modified", "ETag")}
    structure = inspect(raw, protocol)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    elapsed = time.perf_counter() - tic
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_path.open("xb") as stream:
        stream.write(raw)
    receipt = {"schema": "regis.goal.etf_action_receipt.v1", "started_utc": started,
               "ended_utc": datetime.now(timezone.utc).isoformat(), "url": protocol["url"],
               "http_status": 200, "headers": headers, "protocol_sha256": sha(protocol_bytes),
               "script_sha256": code_sha, "raw_sha256": sha(raw), "raw_bytes": len(raw),
               "document_structure": structure, "elapsed_seconds": elapsed,
               "python_traced_peak_bytes": peak,
               "resource_scope": "one native request and text parsing; excludes rendering, not OS RSS or full-A forecast",
               "single_notice_only": True, "intraday_available_at_proved": False,
               "actual_cash_credit_proved": False, "complete_action_coverage": False,
               "full_N4": False, "economic_run": False, "model_admitted": False}
    with RECEIPT.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps(receipt, ensure_ascii=True))


if __name__ == "__main__":
    main()
