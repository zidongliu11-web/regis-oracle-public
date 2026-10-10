"""Offline source diagnostics; never downloads, certifies PIT or admits data.

API documentation describes field semantics, not the historical availability of
each returned observation. Missing publication evidence stays missing. Normalized
rows are returned to the caller for local use, never written to tracked summaries.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SH = ZoneInfo("Asia/Shanghai")
CODE = re.compile(r"\d{6}\.(SH|SZ|BJ)\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
BAR_FIELDS = {"ts_code", "trade_date", "open", "high", "low", "close",
              "pre_close", "pct_chg", "vol", "amount"}
DIV_FIELDS = {"ts_code", "ann_date", "imp_anndate", "div_proc", "record_date",
              "ex_date", "pay_date", "div_cash"}
AUTHORITY = {"research_only": True, "data_admitted": False,
             "economic_run_enabled": False, "model_admitted": False}


def number(value, *, positive=False):
    if isinstance(value, bool) or value is None:
        raise ValueError("numeric_missing_or_boolean")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("numeric_invalid") from None
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError("numeric_nonfinite_or_nonpositive")
    return result


def day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
        raise ValueError("source_date_requires_YYYYMMDD")
    return datetime.strptime(value, "%Y%m%d").date().isoformat()


def clock(value):
    if not isinstance(value, str):
        raise ValueError("source_timestamp_missing")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("source_timestamp_requires_timezone")
    return result.astimezone(SH)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_JSON_field")
        result[key] = value
    return result


def load_export(path: Path, *, expected_sha256: str):
    """Read a user-supplied local response, with exact byte and shape checks."""
    if not isinstance(expected_sha256, str) or not SHA.fullmatch(expected_sha256):
        raise ValueError("source_SHA_invalid")
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("source_SHA_mismatch")
    def nonfinite(_):
        raise ValueError("nonfinite_JSON_constant")
    response = json.loads(raw, parse_float=Decimal, object_pairs_hook=_unique_object,
                          parse_constant=nonfinite)
    if not isinstance(response, dict):
        raise ValueError("response_object_required")
    if "code" in response:
        if type(response["code"]) is not int or response["code"] != 0:
            raise ValueError("API_error_response")
        response = response.get("data")
    if not isinstance(response, dict):
        raise ValueError("response_data_missing")
    fields, items = response.get("fields"), response.get("items")
    if (not isinstance(fields, list) or not fields
            or any(not isinstance(x, str) or not x for x in fields)
            or len(fields) != len(set(fields)) or not isinstance(items, list)):
        raise ValueError("response_fields_or_rows_invalid")
    rows = []
    for values in items:
        if not isinstance(values, list) or len(values) != len(fields):
            raise ValueError("response_row_width_invalid")
        rows.append(dict(zip(fields, values)))
    return rows


def _coverage(actual, expected):
    if len(actual) != len(set(actual)):
        raise ValueError("duplicate_instrument_date_key")
    if expected is None:
        return {"state": "unknown_expected_population", "missing_count": None}
    if not isinstance(expected, list) or any(not isinstance(k, tuple) for k in expected):
        raise ValueError("expected_keys_require_tuples")
    if len(expected) != len(set(expected)):
        raise ValueError("expected_keys_invalid_or_duplicate")
    missing, extra = set(expected) - set(actual), set(actual) - set(expected)
    if extra:
        raise ValueError("unexpected_keys_outside_request")
    return {"state": "keys_match_claim_only" if not missing else "incomplete",
            "missing_count": len(missing), "expected_count": len(expected)}


def _available(key, evidence, captured, earliest):
    if evidence is None or key not in evidence:
        return None
    item = evidence[key]
    if (not isinstance(item, dict)
            or not SHA.fullmatch(str(item.get("source_sha256", "")))):
        raise ValueError("publication_proof_invalid")
    available = clock(item.get("available_at"))
    if available < earliest or available > captured:
        raise ValueError("publication_before_observation_or_after_capture")
    return available.isoformat()


def normalize_bars(path: Path, *, expected_sha256: str, api_name: str,
                   captured_at: str, expected_keys: list[tuple] | None = None,
                   publication_by_key: dict | None = None):
    if api_name not in {"daily", "fund_daily"}:
        raise ValueError("raw_daily_endpoint_required")
    captured = clock(captured_at)
    records = load_export(path, expected_sha256=expected_sha256)
    rows, keys = [], []
    for row in records:
        if not BAR_FIELDS <= row.keys():
            raise ValueError("required_bar_field_missing")
        code = row["ts_code"]
        if not isinstance(code, str) or not CODE.fullmatch(code):
            raise ValueError("exchange_suffix_or_code_invalid")
        if api_name == "fund_daily" and code != "510300.SH":
            raise ValueError("M24_benchmark_identity_not_frozen")
        trade_day = day(row["trade_date"])
        close_time = datetime.combine(date.fromisoformat(trade_day), time(15), SH)
        if captured < close_time:
            raise ValueError("completed_bar_captured_before_close")
        key = (code, trade_day)
        keys.append(key)
        values = {field: number(row[field], positive=True)
                  for field in ("open", "high", "low", "close", "pre_close")}
        if not (values["low"] <= min(values["open"], values["close"])
                <= max(values["open"], values["close"]) <= values["high"]):
            raise ValueError("OHLC_inconsistent")
        vol, amount = number(row["vol"]), number(row["amount"])
        if vol < 0 or amount < 0 or (vol == 0) != (amount == 0):
            raise ValueError("negative_or_inconsistent_volume_amount")
        pct = number(row["pct_chg"])
        computed = (values["close"] / values["pre_close"] - 1) * 100
        if abs(pct - computed) > Decimal("0.02"):
            raise ValueError("pct_change_inconsistent_with_source_pre_close")
        available = _available(key, publication_by_key, captured, close_time)
        rows.append({"ts_code": code, "trade_date": trade_day,
                     **{field: str(value) for field, value in values.items()},
                     "pct_chg": str(pct), "volume_units": str(vol * 100),
                     "amount_yuan": str(amount * 1000), "price_basis": "raw",
                     "pre_close_basis": "provider_ex_rights_reference_not_previous_raw_close",
                     "available_at": available, "captured_at": captured.isoformat(),
                     "source_sha256": expected_sha256})
    coverage = _coverage(keys, expected_keys)
    return {"api_name": api_name, "rows": sorted(rows, key=lambda x: (x["trade_date"], x["ts_code"])),
            "quality": {"row_count": len(rows), "coverage": coverage,
                        "publication_missing_count": sum(x["available_at"] is None for x in rows),
                        "PIT_identity_and_tradability": "not_inferred"},
            "authority": AUTHORITY.copy()}


def normalize_fund_distributions(path: Path, *, expected_sha256: str,
                                 captured_at: str, expected_keys=None,
                                 publication_by_key=None):
    captured = clock(captured_at)
    rows, keys = [], []
    for row in load_export(path, expected_sha256=expected_sha256):
        if not DIV_FIELDS <= row.keys():
            raise ValueError("required_distribution_field_missing")
        if row["ts_code"] != "510300.SH" or row["div_proc"] != "实施":
            raise ValueError("unsupported_fund_identity_or_unimplemented_distribution")
        dates = {key: day(row[key]) for key in
                 ("ann_date", "imp_anndate", "record_date", "ex_date", "pay_date")}
        if not (dates["ann_date"] <= dates["record_date"] < dates["ex_date"] <= dates["pay_date"]
                and dates["ann_date"] <= dates["imp_anndate"] <= dates["record_date"]):
            raise ValueError("distribution_dates_inconsistent")
        if dates["imp_anndate"] > captured.date().isoformat():
            raise ValueError("distribution_implementation_after_capture")
        key = (row["ts_code"], dates["record_date"], dates["ex_date"])
        keys.append(key)
        earliest = datetime.combine(date.fromisoformat(dates["imp_anndate"]), time(0), SH)
        available = _available(key, publication_by_key, captured, earliest)
        rows.append({"ts_code": row["ts_code"], **dates,
                     "cash_per_unit_yuan": str(number(row["div_cash"], positive=True)),
                     "status": "implemented_cash_only", "available_at": available,
                     "cash_credit_at": None,
                     "cash_credit_semantics": "requires_separate_actual_cash_event_not_pay_date_or_reinvestment_account_date",
                     "source_sha256": expected_sha256})
    return {"rows": rows, "quality": {"row_count": len(rows),
            "coverage": _coverage(keys, expected_keys),
            "publication_missing_count": sum(x["available_at"] is None for x in rows),
            "cash_credit_clock_missing_count": len(rows)}, "authority": AUTHORITY.copy()}



def normalize_official_cash_notices(path: Path, *, expected_sha256: str,
                                    captured_at: str, expected_keys=None):
    """Normalize selected public cash terms; never infer implementation or clocks."""
    if not isinstance(expected_sha256, str) or not SHA.fullmatch(expected_sha256):
        raise ValueError("source_SHA_invalid")
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("source_SHA_mismatch")
    def nonfinite(_):
        raise ValueError("nonfinite_JSON_constant")
    ledger = json.loads(raw, parse_float=Decimal, object_pairs_hook=_unique_object,
                        parse_constant=nonfinite)
    if (not isinstance(ledger, dict) or type(ledger.get("schema")) is not int
            or ledger["schema"] != 1 or ledger.get("status") != "partial_unqualified"
            or ledger.get("complete_target_interval") is not False
            or not isinstance(ledger.get("rows"), list)):
        raise ValueError("partial_official_ledger_required")
    captured = clock(captured_at)
    rows, keys, source_ids = [], [], set()
    complete_keys = True
    fields = ("publication_date", "record_date", "ex_date", "pay_date",
              "amount_as_reported", "reported_unit_shares", "amount_yuan_per_share")
    for source in ledger["rows"]:
        if (not isinstance(source, dict) or source.get("instrument") != "510300.SH"
                or source.get("qualified") is not False):
            raise ValueError("unqualified_510300_notice_required")
        source_id, locator = source.get("source_id"), source.get("source_locator")
        if not isinstance(source_id, str) or not source_id or not locator:
            raise ValueError("notice_source_identity_or_locator_missing")
        if source_id in source_ids:
            raise ValueError("duplicate_notice_source_id")
        source_ids.add(source_id)
        missing = [key for key in fields if source.get(key) is None]
        if (not isinstance(source.get("unknown_fields"), list)
                or any(not isinstance(x, str) for x in source["unknown_fields"])
                or sorted(source["unknown_fields"]) != sorted(missing)):
            raise ValueError("notice_unknown_fields_inconsistent")
        if source.get("publication_time") is not None:
            raise ValueError("notice_intraday_publication_not_implemented")
        dates = {}
        for field in fields[:4]:
            value = source.get(field)
            if value is None:
                dates[field] = None
            else:
                if not isinstance(value, str):
                    raise ValueError("notice_date_requires_ISO")
                try:
                    parsed = date.fromisoformat(value)
                except ValueError:
                    raise ValueError("notice_date_requires_ISO") from None
                if parsed.isoformat() != value:
                    raise ValueError("notice_date_requires_ISO")
                if field == "publication_date" and parsed > captured.date():
                    raise ValueError("notice_publication_after_capture")
                dates[field] = value
        ordered = list(fields[:4])
        for i, earlier in enumerate(ordered):
            for j in range(i + 1, len(ordered)):
                later = ordered[j]
                strict = i <= 1 and j >= 2  # The interval crosses record < ex.
                if dates[earlier] is not None and dates[later] is not None:
                    if dates[earlier] > dates[later] or (strict and dates[earlier] == dates[later]):
                        raise ValueError("notice_dates_inconsistent")
        amount = source.get("amount_as_reported")
        unit = source.get("reported_unit_shares")
        reported_per = source.get("amount_yuan_per_share")
        amount = number(amount, positive=True) if amount is not None else None
        unit = number(unit, positive=True) if unit is not None else None
        per = None
        if amount is not None and unit is not None:
            from decimal import Inexact, localcontext
            with localcontext() as context:
                context.prec = max(28, len(amount.as_tuple().digits) + len(unit.as_tuple().digits) + 8)
                context.traps[Inexact] = True
                try:
                    per = amount / unit
                except Inexact:
                    raise ValueError("notice_unit_conversion_not_exact") from None
        if reported_per is not None:
            if per is None or number(reported_per, positive=True) != per:
                raise ValueError("notice_unit_conversion_inconsistent")
        key = (source["instrument"], dates["record_date"], dates["ex_date"])
        if None not in key:
            keys.append(key)
        else:
            complete_keys = False
        rows.append({"ts_code": source["instrument"],
                     "ann_date": dates["publication_date"],
                     **{key: dates[key] for key in ("record_date", "ex_date", "pay_date")},
                     "cash_per_unit_yuan": str(per) if per is not None else None,
                     "amount_as_reported": (source["amount_as_reported"] if isinstance(source.get("amount_as_reported"), str)
                                            else str(amount) if amount is not None else None),
                     "reported_unit_shares": str(unit) if unit is not None else None,
                     "status": "announced_cash_terms_only",
                     "available_at": None, "cash_credit_at": None,
                     "captured_at": captured.isoformat(),
                     "source_sha256": expected_sha256, "source_id": source_id,
                     "source_locator": locator, "unknown_fields": missing,
                     "visual_table_verified": source.get("visual_table_verified") is True,
                     "visual_claim_is_not_independent_source_admission": True,
                     "annual_count_as_reported": source.get("annual_count_as_parsed")})
    _coverage(keys, None)  # Validate known event uniqueness even among partial rows.
    key_coverage = _coverage(keys, expected_keys) if complete_keys else {
        "state": "incomplete_notice_terms", "missing_count": None}
    return {"rows": rows, "quality": {"row_count": len(rows),
            "coverage": key_coverage, "complete_target_interval": False,
            "publication_missing_count": len(rows),
            "cash_credit_clock_missing_count": len(rows),
            "source_notice_status": "partial_unqualified",
            "rights_or_original_version_certified": False},
            "authority": AUTHORITY.copy()}


def readiness(root: Path = ROOT):
    contract = json.loads((root / "QUANT_RESEARCH_M24_DATA_CONTRACT_20261001.json").read_text(encoding="utf-8"))
    bundle = root / contract["local_bundle_path"]
    contact_path = root / "QUANT_RESEARCH_M24_PROVIDER_CONTACT_20261001.json"
    contact = json.loads(contact_path.read_text(encoding="utf-8")) if contact_path.is_file() else None
    sent = bool(contact and contact.get("status") == "sent_and_public_readback_verified")
    # Do not open any unapproved data export or credentials while rights are pending.
    return {"schema": "regis.m24.data_readiness.v1", "status": "source_admission_pending",
            "local_manifest_exists": bundle.is_file(),
            "manifest_or_raw_data_read": False,
            "required_dataset_roles": contract["required_dataset_roles"],
            "authorization": contract["authorization"],
            "public_source_check": {"date": "2026-10-01", "pre_consultation_issue_1920_comments": 0,
                                     "response_is_permission": False},
            "provider_contact": {"message_sent_and_readback_verified": sent,
                                 "comment_url": contact.get("comment_url") if contact else None,
                                 "permission_reply_received": False},
            "historical_economic_runs": 0, "provider_messages_sent": int(sent),
            "real_imported_rows": 0, "authority": AUTHORITY.copy(),
            "next_action": "provider consultation sent; explicit scope/actual rights and legal local sample still required before independent admission; no duplicate message or repeated broad supplier search"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["readiness"])
    parser.add_argument("--write-summary", action="store_true")
    args = parser.parse_args()
    result = readiness()
    if args.write_summary:
        (ROOT / "QUANT_RESEARCH_M24_DATA_READINESS_20261001.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(result, ensure_ascii=True))
    return 0  # Readiness diagnosis succeeded; source/economic gates explicitly did not.


if __name__ == "__main__":
    raise SystemExit(main())
