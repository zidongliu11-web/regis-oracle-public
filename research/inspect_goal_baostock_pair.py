"""Finite local-only QC of four existing BaoStock originals.

No network, raw rows, prices, returns, normalized bars or ledger input are output.
The Tushare adapter's unit conversion is deliberately never called.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research.m24_data_adapter import _unique_object, clock, number  # noqa: E402

PROTOCOL = ROOT / "research/GOAL_BAOSTOCK_PAIR_QC_PROTOCOL_20261001.json"
RECEIPT = ROOT / "research/GOAL_BAOSTOCK_PAIR_QC_RECEIPT_20261001.json"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def iso_day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("noncanonical_source_date")
    return value


def nonfinite(_):
    raise ValueError("nonfinite_JSON_constant")


def inspect_object(spec, protocol):
    limits = protocol["limits"]
    path = (ROOT / spec["path"]).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        raise ValueError("source_missing_or_outside_project")
    with path.open("rb") as stream:
        raw = stream.read(limits["max_bytes_per_object"] + 1)
    if len(raw) > limits["max_bytes_per_object"]:
        raise ValueError("source_byte_limit")
    sha = hashlib.sha256(raw).hexdigest()
    if sha != spec["sha256"]:
        raise ValueError("source_SHA_changed")
    obj = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=nonfinite)
    if not isinstance(obj, dict) or obj.get("schema") != spec["expected_schema"]:
        raise ValueError("source_schema_changed")
    fields, rows = obj.get("fields"), obj.get("rows")
    if (not isinstance(fields, list) or not fields
            or any(not isinstance(f, str) or not f for f in fields)
            or len(fields) != len(set(fields)) or not isinstance(rows, list)
            or len(rows) > limits["max_rows_per_object"]):
        raise ValueError("source_shape_or_row_limit")
    for row in rows:
        if not isinstance(row, list) or len(row) != len(fields):
            raise ValueError("source_row_width")
    role = spec["role"]
    required = {"calendar": {"calendar_date", "is_trading_day"},
                "identity": {"code", "code_name", "ipoDate", "outDate", "type", "status"}}
    bar_fields = {"date", "code", "open", "high", "low", "close", "volume", "amount", "adjustflag"}
    if not required.get(role, bar_fields).issubset(fields):
        raise ValueError("required_source_field_missing")
    collected = clock(obj.get(spec["capture_field"]))
    idx = {f: i for i, f in enumerate(fields)}
    key_fields = {"calendar": ("calendar_date",), "identity": ("code",)}.get(role, ("code", "date"))
    keyed = {}
    for row in rows:
        key = tuple(row[idx[f]] for f in key_fields)
        if key in keyed:
            raise ValueError("duplicate_source_key")
        keyed[key] = row
    expected = protocol["expected_keys"][role]
    expected = [tuple(k) if isinstance(k, list) else (k,) for k in expected]
    selected = [keyed[k] for k in expected if k in keyed]
    facts = {"role": role, "path": spec["path"], "sha256": sha,
             "bytes": len(raw), "source_rows": len(rows), "source_fields": fields,
             "source_capture_at": collected.isoformat(),
             "historical_row_available_at": None,
             "expected_key_count": len(expected), "matching_keys": len(selected),
             "missing_key_count": len(expected) - len(selected),
             "duplicate_keys": 0, "shape_valid": True}
    if role in ("stock", "etf"):
        checks = {"raw_adjustflag3": True, "positive_consistent_OHLC": True,
                  "nonnegative_integral_native_volume": True, "nonnegative_CNY_amount": True}
        for row in selected:
            iso_day(row[idx["date"]])
            op, hi, lo, cl = [number(row[idx[f]], positive=True) for f in ("open", "high", "low", "close")]
            vol, amount = [number(row[idx[f]]) for f in ("volume", "amount")]
            checks["raw_adjustflag3"] &= row[idx["adjustflag"]] == "3"
            checks["positive_consistent_OHLC"] &= lo <= min(op, cl) <= max(op, cl) <= hi
            checks["nonnegative_integral_native_volume"] &= vol >= 0 and vol == vol.to_integral_value()
            checks["nonnegative_CNY_amount"] &= amount >= 0
        facts["selected_checks"] = checks
        facts["selected_checks_all_pass"] = bool(selected) and all(checks.values())
        facts["volume_scale_applied"] = 1
        facts["amount_scale_applied"] = 1
        facts["volume_unit_admitted"] = role == "stock"
        facts["ETF_volume_denominator_independently_proved"] = False if role == "etf" else None
        facts["normalized_or_ledger_rows_emitted"] = 0
    elif role == "calendar":
        for row in selected:
            iso_day(row[idx["calendar_date"]])
            if row[idx["is_trading_day"]] not in ("0", "1"):
                raise ValueError("calendar_flag_invalid")
        facts["selected_calendar_says_open"] = bool(selected) and all(row[idx["is_trading_day"]] == "1" for row in selected)
        facts["calendar_generated_from_prices_or_weekdays"] = False
        facts["historical_calendar_version_proved"] = False
    else:
        facts["selected_current_names_present"] = bool(selected) and all(bool(row[idx["code_name"]]) for row in selected)
        facts["current_record_not_historical_stable_ID_or_PIT"] = True
    return facts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true", help="only byte identity, never reprofile")
    args = parser.parse_args()
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    if args.verify:
        receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
        if digest(PROTOCOL) != receipt["protocol_sha256"] or digest(Path(__file__)) != receipt["script_sha256"]:
            raise SystemExit("QC protocol/script changed")
        for spec in protocol["objects"]:
            if digest(ROOT / spec["path"]) != spec["sha256"]:
                raise SystemExit("original source changed")
        print(json.dumps({"identity_verified": True, "raw_originals": 4,
                          "reprofiled": False, "N4_admitted": False}, sort_keys=True))
        return 0
    if RECEIPT.exists():
        raise SystemExit("existing result: use --verify; do not repeat QC")
    started = datetime.now(timezone.utc).isoformat()
    tracemalloc.start()
    begin = time.perf_counter()
    results, failure = [], None
    try:
        for spec in protocol["objects"]:
            results.append(inspect_object(spec, protocol))
            if sum(x["bytes"] for x in results) > protocol["limits"]["max_total_bytes"]:
                raise ValueError("total_byte_limit")
            if time.perf_counter() - begin > protocol["limits"]["max_seconds"]:
                raise ValueError("elapsed_QC_limit")
    except (ValueError, KeyError, TypeError, OSError) as exc:
        failure = type(exc).__name__ + ":" + str(exc)
    elapsed = time.perf_counter() - begin
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    by_role = {x["role"]: x for x in results}
    complete = len(results) == 4 and failure is None
    paired = complete and all(x["missing_key_count"] == 0 for x in results)
    structural = bool(paired and all(by_role[k]["selected_checks_all_pass"] for k in ("stock", "etf"))
                      and by_role["calendar"]["selected_calendar_says_open"]
                      and by_role["identity"]["selected_current_names_present"])
    receipt = {"schema": "regis.goal.baostock_pair_qc_receipt.v1", "started_utc": started,
               "ended_utc": datetime.now(timezone.utc).isoformat(), "protocol_sha256": digest(PROTOCOL),
               "script_sha256": digest(Path(__file__)), "objects": results, "failure": failure,
               "paired_expected_keys_present": bool(paired), "normal_structural_pair_QC_passed": structural,
               "network_calls": 0, "raw_rows_or_prices_output": 0, "returns_calculated": False,
               "elapsed_seconds": elapsed, "python_traced_peak_bytes": peak,
               "resource_scope": "local four-object read/parse/QC only, excludes imports; traced allocations are not RSS/full-A replay",
               "mandatory_scenarios": protocol["mandatory_scenarios"],
               "rights_scope": "personal noncommercial deterministic local QC only; no remote raw or broader model licence",
               "N4_complete": False, "real_ledger_import_enabled": False, "model_admitted": False}
    RECEIPT.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result_path": str(RECEIPT), "failure": failure,
                      "normal_structural_pair_QC_passed": structural,
                      "objects": len(results), "N4_complete": False}, sort_keys=True))
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
