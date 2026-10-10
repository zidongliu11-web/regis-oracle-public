"""Bounded personal local action/state QC; never a return or ledger replay.
Only source facts, counts and predicates leave local storage.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import sys
import time
import tracemalloc
import urllib.error
import urllib.request

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research.inspect_goal_baostock_pair import inspect_object, nonfinite, iso_day
from research.m24_data_adapter import _unique_object, number, clock

PROTOCOL = ROOT / "research/GOAL_ACTION_PAIR_PROTOCOL_20261002.json"
RECEIPT = ROOT / "research/GOAL_ACTION_PAIR_RECEIPT_20261002.json"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def load_rows(spec, limits):
    path = (ROOT / spec["path"]).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError("path_escape")
    with path.open("rb") as stream:
        raw = stream.read(limits["max_bytes_per_object"] + 1)
    if len(raw) > limits["max_bytes_per_object"] or sha(raw) != spec["sha256"]:
        raise ValueError("source_byte_limit_or_SHA_changed")
    obj = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=nonfinite)
    idx = {f: i for i, f in enumerate(obj["fields"])}
    return idx, obj["rows"]


def capture_document(spec, limits):
    path = (ROOT / spec["path"]).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError("document_path_escape")
    fact = {"id": spec["id"], "url": spec["url"], "path": spec["path"],
            "source_kind": spec["source_kind"], "http_status": None,
            "raw_sha256": None, "raw_bytes": 0, "verified_declared_fields": False,
            "historical_byte_version_proved": False, "intraday_available_at": None}
    req = urllib.request.Request(spec["url"], headers={
        "User-Agent": "Regis-Oracle-bounded-public-disclosure-QC", "Accept": "application/pdf"})
    try:
        if spec.get("reuse_captured_sha256"):
            raw = path.read_bytes()
            if sha(raw) != spec["reuse_captured_sha256"]:
                raise ValueError("first_attempt_document_changed")
            fact["capture_recovered_from_failed_attempt"] = True
            fact["headers"] = None
            fact["HTTP_header_evidence"] = "not persisted before failed QC; unknown"
        else:
            if path.exists():
                raise ValueError("one_time_capture_already_exists")
            with urllib.request.urlopen(req, timeout=limits["request_timeout_seconds"]) as response:
                if response.status != 200 or response.geturl() != spec["url"]:
                    raise ValueError("unexpected_status_or_redirect")
                raw = response.read(limits["max_pdf_bytes"] + 1)
                fact["http_status"] = response.status
                fact["headers"] = {k: response.headers.get(k) for k in
                                   ("Date", "Content-Type", "Last-Modified", "ETag")}
        if len(raw) > limits["max_pdf_bytes"] or not raw.startswith(b"%PDF-"):
            raise ValueError("oversize_or_not_PDF")
        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= limits["max_pdf_pages"]:
            raise ValueError("encrypted_or_page_limit")
        text = "".join(page.extract_text() or "" for page in reader.pages)
        if len(text) > limits["max_pdf_text_characters"]:
            raise ValueError("text_limit")
        compact = "".join(text.split())
        if not spec.get("reuse_captured_sha256"):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(raw)
        fact.update(raw_sha256=sha(raw), raw_bytes=len(raw),
                    page_count=len(reader.pages), text_sha256=sha(text.encode("utf-8")),
                    verified_declared_fields=all(m in compact for m in spec["required_markers"]))
        fact["field_match_count"] = sum(m in compact for m in spec["required_markers"])
        fact["expected_field_count"] = len(spec["required_markers"])
        fact["content_revision_proved"] = False
        # HTTP metadata and signature date alone never establish historical availability.
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        fact["failure_type"] = type(exc).__name__
        fact["failure"] = str(exc)[:400]
    return fact


def quality_predicates(row, idx, multiplier):
    if "tradestatus" in idx and row[idx["tradestatus"]] == "0":
        return {"testable": False, "suspended_not_fillable": True}
    if row[idx["volume"]] == "" or row[idx["amount"]] == "":
        return {"testable": False, "missing_active_volume_or_amount": True}
    high, low = [number(row[idx[f]], positive=True) for f in ("high", "low")]
    volume, amount = [number(row[idx[f]]) for f in ("volume", "amount")]
    if volume == 0:
        return {"testable": False, "zero_volume_zero_amount": amount == 0}
    if volume < 0 or amount <= 0:
        return {"testable": False, "zero_volume_zero_amount": False}
    vwap = amount / (volume * multiplier)
    # Source amount and daily extrema may round; frozen one-price-tick tolerance.
    tolerance = Decimal("0.001")
    return {"testable": True, "vwap_within_daily_extrema": low - tolerance <= vwap <= high + tolerance}


def inspect_stock(spec, protocol, days):
    idx, rows = load_rows(spec, protocol["limits"])
    obj = json.loads((ROOT / spec["path"]).read_bytes(), object_pairs_hook=_unique_object,
                     parse_constant=nonfinite)
    required = {"date", "code", "open", "high", "low", "close", "volume", "amount",
                "adjustflag", "tradestatus", "isST"}
    if (obj.get("schema") != spec["expected_schema"] or not required.issubset(idx)
            or len(idx) != len(obj["fields"]) or len(rows) > protocol["limits"]["max_rows_per_object"]
            or any(not isinstance(row, list) or len(row) != len(idx) for row in rows)):
        raise ValueError("stock_shape_or_schema")
    keyed = {(row[idx["code"]], row[idx["date"]]): row for row in rows}
    if len(keyed) != len(rows):
        raise ValueError("duplicate_stock_key")
    selected = [keyed[(spec["code"], d)] for d in days if (spec["code"], d) in keyed]
    checks = {"raw_adjustflag3": True, "positive_consistent_OHLC": True,
              "valid_status_flags": True, "active_numeric_volume_amount": True,
              "suspended_no_positive_volume_or_amount": True}
    missing = {"volume": 0, "amount": 0}
    suspended = 0
    for row in selected:
        iso_day(row[idx["date"]])
        op, hi, lo, cl = [number(row[idx[f]], positive=True) for f in ("open", "high", "low", "close")]
        checks["raw_adjustflag3"] &= row[idx["adjustflag"]] == "3"
        checks["positive_consistent_OHLC"] &= lo <= min(op, cl) <= max(op, cl) <= hi
        checks["valid_status_flags"] &= row[idx["tradestatus"]] in ("0", "1") and row[idx["isST"]] in ("0", "1")
        paused = row[idx["tradestatus"]] == "0"
        suspended += int(paused)
        for field in ("volume", "amount"):
            value = row[idx[field]]
            missing[field] += int(value == "")
            if value == "":
                checks["active_numeric_volume_amount"] &= paused
                continue
            n = number(value)
            checks["active_numeric_volume_amount"] &= n >= 0 and (field != "volume" or n == n.to_integral_value())
            if paused:
                checks["suspended_no_positive_volume_or_amount"] &= n == 0
    return {"object_id": spec["id"], "role": "stock", "path": spec["path"],
            "sha256": spec["sha256"], "source_rows": len(rows), "source_fields": obj["fields"],
            "source_capture_at": clock(obj[spec["capture_field"]]).isoformat(),
            "expected_key_count": len(days), "matching_keys": len(selected),
            "missing_key_count": len(days) - len(selected), "selected_checks": checks,
            "selected_checks_all_pass": bool(selected) and all(checks.values()),
            "selected_suspended_rows": suspended, "missing_numeric_fields_preserved": missing,
            "missing_values_imputed": 0, "historical_row_available_at": None,
            "suspension_execution_policy": "no fill regardless of carried price or empty numeric fields"}


def local_qc(protocol, documents):
    objects = {o["id"]: o for o in protocol["objects"]}
    dates = sorted({d for c in protocol["cases"] for d in c["dates"]})
    facts, tables = [], {}
    for spec in protocol["objects"]:
        role = spec["role"]
        selected_dates = dates if role in ("calendar", "etf", "stock") else []
        keys = [selected_dates if role == "calendar" else
                [[spec["code"], d] for d in selected_dates]][0]
        if role == "identity":
            keys = protocol["identity_codes"]
        shape_protocol = {"limits": protocol["limits"], "expected_keys": {role: keys}}
        facts.append(inspect_stock(spec, protocol, selected_dates) if role == "stock" else
                     {"object_id": spec["id"], **inspect_object(spec, shape_protocol)})
        idx, rows = load_rows(spec, protocol["limits"])
        fields = {"calendar": ("calendar_date",), "identity": ("code",)}.get(role, ("code", "date"))
        tables[spec["id"]] = (idx, {tuple(r[idx[f]] for f in fields): r for r in rows})
    doc_map = {d["id"]: d for d in documents}
    object_facts = {f["object_id"]: f for f in facts}
    results = []
    for case in protocol["cases"]:
        verdict = {"id": case["id"], "declared_event": case["declared_event"],
                   "expected_dates": case["dates"], "expected_keys": 3 * len(case["dates"]),
                   "matching_keys": 0, "calendar_open_matches": 0,
                   "native_vwap_testable_rows": 0, "native_vwap_pass_rows": 0,
                   "wrong_lot_x100_rejected_rows": 0, "status_checks": [],
                   "actual_cash_credit_proved": False, "economic_use_admitted": False}
        document = doc_map[case["document_id"]]
        verdict["document_fields_verified"] = document["verified_declared_fields"]
        verdict["document_source_kind"] = document["source_kind"]
        verdict["stock_bar_structure_matches"] = object_facts[case["stock_object"]]["selected_checks_all_pass"]
        verdict["etf_bar_structure_matches"] = object_facts["etf"]["selected_checks_all_pass"]
        for day in case["dates"]:
            iso_day(day)
            for oid in (case["stock_object"], "etf"):
                idx, rows = tables[oid]
                row = rows.get((objects[oid]["code"], day))
                if row is None:
                    continue
                verdict["matching_keys"] += 1
                native = quality_predicates(row, idx, Decimal(1))
                scaled = quality_predicates(row, idx, Decimal(100))
                if native["testable"]:
                    verdict["native_vwap_testable_rows"] += 1
                    verdict["native_vwap_pass_rows"] += int(native["vwap_within_daily_extrema"])
                    verdict["wrong_lot_x100_rejected_rows"] += int(
                        scaled["testable"] and not scaled["vwap_within_daily_extrema"])
                if oid == case["stock_object"] and day in case.get("expected_status", {}):
                    expected = case["expected_status"][day]
                    checks = {f: row[idx[f]] == v for f, v in expected.items()}
                    if expected.get("tradestatus") == "0":
                        checks["no_positive_volume_or_amount"] = all(
                            row[idx[f]] == "" or number(row[idx[f]]) == 0 for f in ("volume", "amount"))
                        checks["suspended_not_fillable"] = True
                    verdict["status_checks"].append({"date": day, "expected": expected,
                                                     "predicates": checks, "passed": all(checks.values())})
            idx, rows = tables["calendar"]
            row = rows.get((day,))
            if row is not None:
                verdict["matching_keys"] += 1
                verdict["calendar_open_matches"] += int(row[idx["is_trading_day"]] == "1")
        verdict["paired_keys_present"] = verdict["matching_keys"] == verdict["expected_keys"]
        verdict["calendar_open_all"] = verdict["calendar_open_matches"] == len(case["dates"])
        verdict["native_unit_consistency"] = (
            verdict["native_vwap_testable_rows"] > 0 and
            verdict["native_vwap_pass_rows"] == verdict["native_vwap_testable_rows"])
        verdict["status_matches_declared"] = (
            all(c["passed"] for c in verdict["status_checks"]) if case.get("expected_status") else None)
        verdict["limited_structural_event_pair_passed"] = bool(
            verdict["document_fields_verified"] and verdict["paired_keys_present"] and
            verdict["calendar_open_all"] and verdict["native_unit_consistency"] and
            verdict["stock_bar_structure_matches"] and verdict["etf_bar_structure_matches"] and
            verdict["status_matches_declared"] is not False)
        results.append(verdict)
    return facts, results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    protocol = json.loads(PROTOCOL.read_bytes())
    if sha(Path(__file__).read_bytes()) != protocol["script_sha256"]:
        raise ValueError("script_changed_after_protocol_freeze")
    if args.verify:
        result = json.loads(RECEIPT.read_bytes())
        if result["protocol_sha256"] != sha(PROTOCOL.read_bytes()):
            raise ValueError("protocol_changed_after_QC")
        for obj in protocol["objects"]:
            if sha((ROOT / obj["path"]).read_bytes()) != obj["sha256"]:
                raise ValueError("existing_original_changed")
        for doc in result["documents"]:
            if doc["raw_sha256"] and sha((ROOT / doc["path"]).read_bytes()) != doc["raw_sha256"]:
                raise ValueError("captured_document_changed")
        print(json.dumps({"identity_verified": True, "reprofiled": False, "N4_complete": False}))
        return 0
    if RECEIPT.exists():
        raise ValueError("one_time_QC_already_exists")
    start = datetime.now(timezone.utc).isoformat()
    tic = time.perf_counter()
    tracemalloc.start()
    documents = [capture_document(s, protocol["limits"]) for s in protocol["documents"]]
    facts, results = local_qc(protocol, documents)
    elapsed = time.perf_counter() - tic
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    receipt = {"schema": "regis.goal.action_pair_receipt.v1",
               "started_utc": start, "ended_utc": datetime.now(timezone.utc).isoformat(),
               "script_sha256": protocol["script_sha256"], "protocol_sha256": sha(PROTOCOL.read_bytes()),
               "documents": documents, "objects": facts, "cases": results,
               "native_requests_attempted": sum(not d.get("reuse_captured_sha256") for d in protocol["documents"]),
               "prior_failed_attempt_native_requests": len(protocol["documents"]), "market_API_calls": 0,
               "raw_price_or_row_output": 0, "returns_calculated": False,
               "raw_documents_uploaded": False, "elapsed_seconds": elapsed,
               "python_traced_peak_bytes": peak,
               "resource_scope": "bounded capture/parse and local QC, excludes imports/rendering; not RSS/full-A replay",
               "ETF_native_units_independently_quantitatively_proved": False,
               "N4_complete": False, "real_ledger_import_enabled": False, "model_admitted": False,
               "mandatory_gaps": protocol["mandatory_gaps"]}
    with RECEIPT.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"result": str(RECEIPT), "cases": [
        {"id": c["id"], "paired": c["paired_keys_present"],
         "fields_verified": c["document_fields_verified"],
         "limited_structural_passed": c["limited_structural_event_pair_passed"]}
        for c in results], "full_N4": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
