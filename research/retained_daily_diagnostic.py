"""One frozen, local descriptive diagnostic; never an executable backtest.

Raw arrays and per-day factor values stay in the Native evidence directory.
The public result contains counts and source metadata only.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import time


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_JSON_key")
        result[key] = value
    return result


def parse(raw):
    def reject(_):
        raise ValueError("nonfinite_JSON_constant")
    return json.loads(raw.decode("utf-8-sig"),
                      object_pairs_hook=unique, parse_constant=reject)


def read(path):
    return parse(Path(path).read_bytes())


def numeric(value, *, positive=False):
    number = Decimal(str(value))
    if not number.is_finite() or (positive and number <= 0):
        raise ValueError("invalid_source_number")
    return number


def day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("invalid_source_date")
    return value


def run(root, protocol_path):
    root = Path(root).resolve()
    p = read(protocol_path)
    n = Path(p["native_evidence_root"]).resolve()
    n.mkdir(exist_ok=True)
    with (n / "diagnostic.dispatched").open("x", encoding="utf-8") as stream:
        stream.write(digest(protocol_path))
    started = time.monotonic()
    out = {"schema": 1, "stage": "E172", "started_utc": datetime.now(timezone.utc).isoformat(),
           "protocol_sha256": digest(protocol_path), "implementation_sha256": digest(__file__),
           "state": "descriptive_diagnostic_started", "objects": [], "failure": None,
           "economic_runs": 0, "alpha_candidates_added": 0, "orders": 0,
           "financial_admitted": False, "model_admitted": False, "full_goal_complete": False,
           "factor_id": "m24_negative_daily_return_rank",
           "formula_fragment": "-pctChg/100; no cross-sectional rank or eligibility claimed",
           "window": p["window"], "raw_values_exported": 0}
    tables = {}
    try:
        total_bytes = 0
        for spec in p["objects"]:
            source = (root / spec["path"]).resolve()
            if not source.is_relative_to(root):
                raise ValueError("source_path_outside_project")
            with source.open("rb") as stream:
                raw = stream.read(p["limits"]["max_bytes_per_object"] + 1)
            size = len(raw)
            total_bytes += size
            if size > p["limits"]["max_bytes_per_object"] or total_bytes > p["limits"]["max_total_bytes"]:
                raise ValueError("source_byte_cap")
            if hashlib.sha256(raw).hexdigest() != spec["sha256"]:
                raise ValueError("source_SHA_changed")
            obj = parse(raw)
            if obj.get("schema") != spec["expected_schema"]:
                raise ValueError("source_schema_changed")
            fields, rows = obj["fields"], obj["rows"]
            if len(set(fields)) != len(fields) or len(rows) > p["limits"]["max_rows_per_object"]:
                raise ValueError("source_shape_or_row_cap")
            if any(not isinstance(row, list) or len(row) != len(fields) for row in rows):
                raise ValueError("source_row_width")
            capture = datetime.fromisoformat(obj[spec["capture_field"]])
            if capture.tzinfo is None:
                raise ValueError("source_capture_timezone_missing")
            role = spec["role"]
            key_fields = ("calendar_date",) if role == "calendar" else ("code",) if role == "identity" else ("date",)
            table = {}
            for values in rows:
                row = dict(zip(fields, values))
                if role in ("stock", "etf") and row["code"] != spec["code"]:
                    raise ValueError("unexpected_source_instrument")
                key = tuple(row[key] for key in key_fields)
                if key in table:
                    raise ValueError("duplicate_source_key")
                table[key] = row
            tables[role] = table
            out["objects"].append({"role": role, "sha256": spec["sha256"], "schema": obj["schema"],
                                   "source_rows": len(rows), "fields": fields, "bytes": size,
                                   "capture_at": capture.isoformat(), "client_version": obj.get("client_version"),
                                   "query_start": obj.get("start_date"), "query_end": obj.get("end_date")})
            with source.open("rb") as stream:
                after = stream.read(p["limits"]["max_bytes_per_object"] + 1)
            if len(after) != size or hashlib.sha256(after).hexdigest() != spec["sha256"]:
                raise ValueError("source_changed_during_read")
            if time.monotonic() - started > p["limits"]["max_seconds"]:
                raise ValueError("diagnostic_time_cap")
        start, end = p["window"]["start"], p["window"]["end"]
        calendar = tables["calendar"]
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        civil_days = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
        calendar_missing = [d for d in civil_days if (d,) not in calendar]
        out["calendar_missing_civil_dates"] = calendar_missing
        if calendar_missing:
            raise ValueError("calendar_internal_coverage_missing")
        if any(row["is_trading_day"] not in ("0", "1") for row in calendar.values()):
            raise ValueError("calendar_flag_invalid")
        sessions = sorted(day(key[0]) for key, row in calendar.items() if row["is_trading_day"] == "1")
        expected = [d for d in sessions if start <= d <= end]
        if not expected or start not in expected or end not in expected:
            raise ValueError("frozen_window_endpoint_missing")
        missing = {role: [d for d in expected if (d,) not in tables[role]] for role in ("stock", "etf")}
        unexpected = {role: sorted(day(key[0]) for key in tables[role]
                                   if start <= key[0] <= end and key[0] not in expected)
                      for role in ("stock", "etf")}
        out["coverage"] = {"expected_sessions": len(expected),
                           "missing_counts": {role: len(days) for role, days in missing.items()},
                           "missing_dates": missing,
                           "unexpected_closed_session_dates": unexpected,
                           "prior_stock_sessions": sum((d,) in tables["stock"] for d in sessions if d < start),
                           "full_window_present": not any(missing.values()) and not any(unexpected.values())}
        if any(unexpected.values()):
            raise ValueError("prices_on_calendar_closed_day")
        diagnostics, signs, flags, mismatch = [], Counter(), Counter(), 0
        for d in expected:
            if any((d,) not in tables[role] for role in ("stock", "etf")):
                continue
            for role in ("stock", "etf"):
                row = tables[role][(d,)]
                if row["adjustflag"] != "3":
                    raise ValueError("nonraw_source_adjustflag")
                for name in ("open", "high", "low", "close"):
                    numeric(row[name], positive=True)
            stock = tables["stock"][(d,)]
            pct = numeric(stock["pctChg"])
            factor = -pct / Decimal(100)
            signs["positive" if factor > 0 else "negative" if factor < 0 else "zero"] += 1
            preclose = numeric(stock["preclose"], positive=True)
            implied_pct = (numeric(stock["close"], positive=True) / preclose - 1) * 100
            disagreement = abs(pct - implied_pct)
            mismatch += disagreement > Decimal(p["arithmetic_comparison_tolerance_percentage_points"])
            for field in ("tradestatus", "isST"):
                if stock[field] not in ("0", "1"):
                    raise ValueError("source_state_flag_invalid")
                flags[field + "=" + stock[field]] += 1
            pos = sessions.index(d)
            covered = {str(h): pos + h < len(sessions) and sessions[pos + h] <= end
                       and (sessions[pos + h],) in tables["stock"] and (sessions[pos + h],) in tables["etf"]
                       for h in p["horizons"]}
            diagnostics.append({"date": d, "factor_fragment": str(factor),
                                "pct_arithmetic_disagreement": str(disagreement), "exit_price_coverage": covered})
        if time.monotonic() - started > p["limits"]["max_seconds"]:
            raise ValueError("diagnostic_time_cap")
        with (n / "native_factor_fragments.json").open("x", encoding="utf-8") as stream:
            json.dump(diagnostics, stream, ensure_ascii=False, separators=(",", ":"))
        out["descriptive"] = {"computed_sessions": len(diagnostics), "factor_sign_counts": dict(signs),
                              "source_flag_counts": dict(flags), "pct_arithmetic_tolerance_exceeded": mismatch,
                              "exit_coverage_counts": {str(h): sum(x["exit_price_coverage"][str(h)] for x in diagnostics)
                                                       for h in p["horizons"]}}
        out["current_identity_keys_present"] = {code: (code,) in tables["identity"] for code in p["codes"]}
        out["state"] = "descriptive_factor_fragment_computed_no_trading_or_economic_admission"
        out["native_fragments_sha256"] = digest(n / "native_factor_fragments.json")
    except Exception as exc:
        out["failure"] = {"type": type(exc).__name__, "reason": str(exc) if isinstance(exc, ValueError) else "source_or_field_unavailable"}
        out["state"] = "descriptive_diagnostic_failed_preserved"
    out["ended_utc"] = datetime.now(timezone.utc).isoformat()
    out["elapsed_seconds"] = round(time.monotonic() - started, 6)
    out["unresolved"] = p["unresolved"]
    with (n / "terminal.json").open("x", encoding="utf-8") as stream:
        json.dump(out, stream, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False))
    return 1 if out["failure"] else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", required=True)
    args = parser.parse_args()
    raise SystemExit(run(Path(__file__).resolve().parents[1], Path(args.protocol)))
