"""One real delisting exception, paired locally; no economic or order output."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
import argparse
import hashlib
import io
import json
import socket
import sys
import time
import tracemalloc
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research.inspect_goal_action_pairs import inspect_stock
from research.m24_data_adapter import _unique_object

PROTOCOL = ROOT / "research/GOAL_DELIST_PAIR_PROTOCOL_20261002.json"
RECEIPT = ROOT / "research/GOAL_DELIST_PAIR_RECEIPT_20261002.json"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ignored = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.ignored += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.ignored = max(0, self.ignored - 1)

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    protocol = json.loads(PROTOCOL.read_bytes(), object_pairs_hook=_unique_object)
    if sha(Path(__file__)) != protocol["script_sha256"]:
        raise ValueError("script changed")
    for item in protocol["inputs"]:
        if sha(ROOT / item["path"]) != item["sha256"]:
            raise ValueError("pinned input changed")
    raw_path = ROOT / protocol["raw_path"]
    notice_path = ROOT / protocol["notice_path"]
    if args.verify:
        result = json.loads(RECEIPT.read_bytes())
        if result["protocol_sha256"] != sha(PROTOCOL):
            raise ValueError("receipt stale")
        for key, path in (("notice_sha256", notice_path), ("raw_sha256", raw_path)):
            if result.get(key) and sha(path) != result[key]:
                raise ValueError("original changed")
        print(json.dumps({"identity_verified": True, "new_network": False, "full_N4": False}))
        return
    if RECEIPT.exists() or raw_path.exists() or notice_path.exists():
        raise ValueError("one-time capture already exists")
    result = {
        "schema": "regis.goal.delisting_pair_receipt.v1",
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "script_sha256": protocol["script_sha256"],
        "protocol_sha256": sha(PROTOCOL),
        "source_requests": 0, "market_queries": 0, "SDK_calls": 0, "retries": 0,
        "notice_sha256": None, "raw_sha256": None, "rows": 0,
        "price_or_raw_numeric_values_output": 0,
        "source_use_scope": "personal noncommercial local deterministic QC only",
        "account_or_saved_credentials_read": False,
        "historical_available_at_proved": False, "PIT_universe_admitted": False,
        "execution_gate_connected": False, "real_ledger_import": False,
        "economic_run": False, "full_N4": False, "model_admitted": False,
    }
    began = time.perf_counter()
    tracemalloc.start()
    connected = False
    bs = None
    try:
        result["source_requests"] += 1
        request = urllib.request.Request(protocol["notice_url"], headers={
            "User-Agent": "Regis-Oracle-bounded-public-disclosure-QC", "Accept": "text/html"})
        with urllib.request.urlopen(request, timeout=15) as response:
            result["notice_HTTP_status"] = response.status
            result["notice_final_URL"] = response.geturl()
            result["notice_HTTP_headers"] = {key: response.headers.get(key)
                                            for key in ("Date", "Content-Type", "Last-Modified", "ETag")}
            if response.status != 200 or response.geturl() != protocol["notice_url"]:
                raise ValueError("unexpected_status_or_redirect")
            notice = response.read(protocol["max_notice_bytes"] + 1)
        if len(notice) > protocol["max_notice_bytes"]:
            raise ValueError("notice byte limit")
        text = notice.decode("utf-8")
        visible = VisibleText()
        visible.feed(text)
        compact = "".join("".join(visible.parts).split())
        result["notice_marker_matches"] = sum(marker in compact for marker in protocol["notice_markers"])
        result["notice_expected_markers"] = len(protocol["notice_markers"])
        notice_path.parent.mkdir(parents=True, exist_ok=True)
        with notice_path.open("xb") as handle:
            handle.write(notice)
        result["notice_sha256"] = sha(notice_path)
        result["notice_bytes"] = len(notice)
        result["native_notice_fields_verified"] = result["notice_marker_matches"] == len(protocol["notice_markers"])
        if not result["native_notice_fields_verified"]:
            raise ValueError("native official field mismatch")
        basic = json.loads((ROOT / protocol["basic_path"]).read_bytes(), object_pairs_hook=_unique_object)
        matched = [dict(zip(basic["fields"], row)) for row in basic["rows"]
                   if row[basic["fields"].index("code")] == protocol["code"]]
        result["single_current_metadata_key"] = len(matched) == 1
        result["current_inactive_status_and_official_outDate_match"] = (
            len(matched) == 1 and matched[0]["status"] == "0"
            and matched[0]["outDate"] == protocol["official_delisting_day"])
        if not result["current_inactive_status_and_official_outDate_match"]:
            raise ValueError("current identity mismatch")
        etf = json.loads((ROOT / protocol["ETF_path"]).read_bytes(), object_pairs_hook=_unique_object)
        etf_keys = {(r[etf["fields"].index("code")], r[etf["fields"].index("date")]) for r in etf["rows"]}
        calendar = json.loads((ROOT / protocol["calendar_path"]).read_bytes(), object_pairs_hook=_unique_object)
        calendar_rows = dict((r[0], r[1]) for r in calendar["rows"])
        result["six_ETF_and_calendar_keys_match"] = all(
            ("sh.510300", day) in etf_keys and calendar_rows.get(day) == "1"
            for day in protocol["paired_open_days"])
        result["closed_day_control_verified"] = calendar_rows.get("2026-06-19") == "0"
        if not result["six_ETF_and_calendar_keys_match"] or not result["closed_day_control_verified"]:
            raise ValueError("paired public event/calendar geometry")
        sys.path.insert(0, str(ROOT / ".codex-local/deps"))
        import baostock as bs
        if not Path(bs.__file__).resolve().is_relative_to(ROOT / ".codex-local/deps"):
            raise ValueError("unexpected SDK")
        socket.setdefaulttimeout(15)
        result["SDK_calls"] += 1
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            login = bs.login()
        result["login_error_code"] = str(login.error_code)
        if login.error_code != "0":
            raise ValueError("anonymous login failed")
        connected = True
        result["SDK_calls"] += 1
        result["market_queries"] += 1
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            response = bs.query_history_k_data_plus(
                protocol["code"], ",".join(protocol["fields"]),
                start_date=protocol["start_date"], end_date=protocol["end_date"],
                frequency="d", adjustflag="3")
        result["query_error_code"] = str(response.error_code)
        if response.error_code != "0" or list(response.fields or []) != protocol["fields"]:
            raise ValueError("response fields or error")
        rows = response.data
        if not isinstance(rows, list) or len(rows) > protocol["max_rows"]:
            raise ValueError("row limit; pagination prohibited")
        raw = {"schema": "regis.goal.delisting_local_raw.v1", "received_utc": datetime.now(timezone.utc).isoformat(),
               "code": protocol["code"], "start_date": protocol["start_date"], "end_date": protocol["end_date"],
               "fields": protocol["fields"], "rows": rows}
        payload = (json.dumps(raw, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(payload) > protocol["max_raw_bytes"]:
            raise ValueError("raw byte limit")
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        with raw_path.open("xb") as handle:
            handle.write(payload)
        result["raw_sha256"] = sha(raw_path)
        result["raw_bytes"] = len(payload)
        result["rows"] = len(rows)
        shape_valid = all(isinstance(row, list) and len(row) == len(protocol["fields"]) for row in rows)
        if not shape_valid:
            raise ValueError("row shape")
        indices = {field: i for i, field in enumerate(protocol["fields"])}
        daily = {row[indices["date"]]: row for row in rows}
        result["unique_correct_open_keys"] = len(daily) == len(rows) and all(
            row[indices["code"]] == protocol["code"]
            and protocol["start_date"] <= day <= protocol["end_date"]
            and calendar_rows.get(day) == "1" for day, row in daily.items())
        spec = {"id": "delisted_stock", "role": "stock", "path": protocol["raw_path"],
                "sha256": result["raw_sha256"], "code": protocol["code"],
                "expected_schema": raw["schema"], "capture_field": "received_utc"}
        checked = inspect_stock(spec, {"limits": {"max_bytes_per_object": protocol["max_raw_bytes"],
                                                "max_rows_per_object": protocol["max_rows"]}},
                                sorted(daily))
        result["reused_state_aware_numeric_QC"] = checked["selected_checks_all_pass"]
        result["suspended_rows"] = checked["selected_suspended_rows"]
        result["missing_numeric_fields_preserved"] = checked["missing_numeric_fields_preserved"]
        result["missing_imputed"] = 0
        result["two_pre_end_active_keys"] = all(
            day in daily and daily[day][indices["tradestatus"]] == "1" for day in protocol["pre_end_days"])
        result["closed_day_control_has_no_source_row"] = "2026-06-19" not in daily
        result["rows_after_period_end"] = sum(day > protocol["official_last_trading_day"] for day in daily)
        result["active_rows_after_period_end"] = sum(
            day > protocol["official_last_trading_day"] and row[indices["tradestatus"]] == "1"
            for day, row in daily.items())
        result["rows_on_or_after_delisting"] = sum(day >= protocol["official_delisting_day"] for day in daily)
        result["no_post_period_active_rows"] = result["active_rows_after_period_end"] == 0
        result["no_post_delisting_rows"] = result["rows_on_or_after_delisting"] == 0
        result["absence_semantics"] = "missing row alone is not suspension or zero return; official delisting fences identity, no carry/forward fill or code inheritance"
        quality = (result["unique_correct_open_keys"] and result["reused_state_aware_numeric_QC"]
                   and result["two_pre_end_active_keys"] and result["closed_day_control_has_no_source_row"]
                   and result["no_post_period_active_rows"] and result["no_post_delisting_rows"])
        result["limited_delisting_QC_passed"] = bool(quality)
        result["status"] = "local_delisting_QC_passed" if quality else "captured_semantic_mismatch"
    except Exception as exc:
        result["status"] = "capture_or_QC_failed"
        result["failure_type"] = type(exc).__name__
    finally:
        if connected:
            result["SDK_calls"] += 1
            try:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    bs.logout()
            except Exception:
                result["logout_failed"] = True
        result["elapsed_seconds"] = time.perf_counter() - began
        result["python_traced_peak_bytes"] = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    result["resource_scope"] = "single selected stock/notice, imports excluded; traced Python allocation, not process RSS/full-market forecast"
    result["ended_utc"] = datetime.now(timezone.utc).isoformat()
    with RECEIPT.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({key: result[key] for key in ("status", "source_requests", "market_queries", "rows", "full_N4")}))
    return 0 if result.get("limited_delisting_QC_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
