"""Reconcile two fixed securities' retained action records; never read price HDFs."""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research"))
import inspect_short_rq_full as base
from audit_stock_action_roles import normalize
from recover_rq_etf_history import safe_dataset


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    protocol = ROOT / "research/SELECTED_WINDOW_ACTION_PROTOCOL_20261008.json"
    p = json.loads(protocol.read_text(encoding="utf-8"))
    n = Path(p["native_evidence_root"])
    with (n / "action_audit.dispatched").open("x", encoding="utf-8") as f:
        f.write(sha(protocol))
    started = time.monotonic()
    result = {"schema": 1, "stage": "E173", "started_utc": datetime.now(timezone.utc).isoformat(),
              "protocol_sha256": sha(protocol), "implementation_sha256": sha(__file__),
              "state": "selected_window_action_reconciliation_started", "failure": None,
              "securities": {}, "source_files": [], "price_datasets_read": 0,
              "economic_runs": 0, "new_candidates": 0, "data_L1_admitted": False,
              "financial_admitted": False, "model_admitted": False, "full_goal_complete": False}
    try:
        h5py, np = base.load_dependencies()
        total = 0
        tables = {}
        for spec in p["objects"]:
            path = Path(spec["path"])
            with path.open("rb") as f:
                raw = f.read(p["limits"]["max_bytes_per_object"] + 1)
            total += len(raw)
            if len(raw) > p["limits"]["max_bytes_per_object"] or total > p["limits"]["max_total_bytes"]:
                raise ValueError("action_source_byte_cap")
            if hashlib.sha256(raw).hexdigest() != spec["sha256"]:
                raise ValueError("action_source_SHA_changed")
            with h5py.File(io.BytesIO(raw), "r", rdcc_nbytes=1048576) as source:
                for key in p["security_keys"]:
                    if key not in source:
                        tables[(spec["role"], key)] = None
                        continue
                    ds = safe_dataset(source, key, h5py, np)
                    if ds.shape[0] > p["limits"]["rows_per_dataset"]:
                        raise ValueError("action_row_cap")
                    tables[(spec["role"], key)] = ds[:]
            with path.open("rb") as f:
                after = f.read(p["limits"]["max_bytes_per_object"] + 1)
            if len(after) != len(raw) or hashlib.sha256(after).hexdigest() != spec["sha256"]:
                raise ValueError("action_source_drift")
            result["source_files"].append({"role": spec["role"], "sha256": spec["sha256"], "bytes": len(raw)})
            if time.monotonic() - started > p["limits"]["max_seconds"]:
                raise ValueError("action_time_cap")
        lower, upper = p["start_date"], p["end_date"]
        for key in p["security_keys"]:
            fact = {"dataset_missing": [], "cash_rows": [], "split_dates": [], "factor_change_dates": []}
            for role in ("cash", "split", "factor"):
                rows = tables[(role, key)]
                if rows is None:
                    fact["dataset_missing"].append(role)
                    continue
                fields = set(rows.dtype.names or ())
                if role == "cash":
                    required = {"announcement_date", "book_closure_date", "ex_dividend_date", "payable_date", "round_lot", "dividend_cash_before_tax"}
                    if not required <= fields:
                        raise ValueError("cash_schema_incompatible")
                    ann = normalize(rows["announcement_date"], np, allow_missing=True)
                    book, ex, pay = [normalize(rows[f], np) for f in ("book_closure_date", "ex_dividend_date", "payable_date")]
                    for i in np.flatnonzero((ex >= lower) & (ex <= upper)):
                        unit, amount = Decimal(str(rows["round_lot"][i])), Decimal(str(rows["dividend_cash_before_tax"][i]))
                        if not unit.is_finite() or not amount.is_finite() or unit <= 0 or amount < 0:
                            raise ValueError("cash_unit_or_amount_invalid")
                        known = p["known_notices"].get(key)
                        dates = {"record_date": int(book[i]), "ex_date": int(ex[i]), "pay_date": int(pay[i])}
                        chronology = bool(ann[i] and book[i] and pay[i] and ann[i] <= book[i] < ex[i] <= pay[i])
                        exact = known is not None and amount / unit == Decimal(known["gross_cash_per_unit"])
                        close = known is not None and abs(amount / unit - Decimal(known["gross_cash_per_unit"])) <= Decimal(p["cash_comparison_tolerance"])
                        match = known is not None and dates == {f: known[f] for f in dates}
                        fact["cash_rows"].append({**dates, "source_announcement_date": int(ann[i]),
                                                  "chronology_valid": chronology, "known_notice_dates_match": match,
                                                  "known_notice_cash_exact": exact, "known_notice_cash_within_tolerance": close})
                elif role == "split":
                    if not {"ex_date", "split_factor", "split_coefficient_to", "split_coefficient_from"} <= fields:
                        raise ValueError("split_schema_incompatible")
                    dates = normalize(rows["ex_date"], np)
                    fact["split_dates"] = list(map(int, dates[(dates >= lower) & (dates <= upper)]))
                else:
                    if not {"start_date", "ex_cum_factor"} <= fields:
                        raise ValueError("factor_schema_incompatible")
                    dates = normalize(rows["start_date"], np)
                    values = rows["ex_cum_factor"]
                    if np.any(np.diff(dates) <= 0) or np.any(~np.isfinite(values)) or np.any(values <= 0):
                        raise ValueError("factor_series_invalid")
                    changed = np.zeros(len(dates), dtype=bool)
                    if len(dates) > 1:
                        changed[1:] = values[1:] != values[:-1]
                    fact["factor_change_dates"] = list(map(int, dates[(dates >= lower) & (dates <= upper) & changed]))
            observed = {x["ex_date"] for x in fact["cash_rows"]} | set(fact["split_dates"])
            fact["unmatched_factor_dates"] = sorted(set(fact["factor_change_dates"]) - observed)
            fact["duplicate_cash_ex_dates"] = len(fact["cash_rows"]) - len({x["ex_date"] for x in fact["cash_rows"]})
            fact["source_date_concordance_only"] = True
            fact["complete_actions_certified"] = False
            result["securities"][key] = fact
        result["state"] = "actual_selected_window_action_roles_reconciled_no_complete_admission"
    except Exception as exc:
        result["state"] = "action_reconciliation_failed_preserved"
        result["failure"] = {"type": type(exc).__name__, "reason": str(exc) if isinstance(exc, ValueError) else "source_or_schema_failure"}
    result["elapsed_seconds"] = round(time.monotonic() - started, 6)
    result["ended_utc"] = datetime.now(timezone.utc).isoformat()
    result["limits"] = "Retrospective source records matched to two public notices; missing datasets remain unknown. No source-price repair, no known-at/PIT or comprehensive noncash-action proof; no fills or actual cash settlement."
    with (n / "terminal.json").open("x", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["failure"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
