"""Bounded quality pilot on one attributed open publication attachment.

Does not estimate returns, issue signals, or admit this sample as stock data.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import subprocess
import re
import time
import tracemalloc
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "research/GOAL_PUBLISHED_SAMPLE_PROTOCOL_20261001.json"
OUTPUT = ROOT / "research/GOAL_SAMPLE_RESOURCE_REPORT_20261001.json"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--resume-local", action="store_true")
    args = parser.parse_args()
    spec_raw = PROTOCOL.read_bytes()
    spec = json.loads(spec_raw)
    raw_path = ROOT / spec["raw_destination"]
    prior = json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.exists() else None
    if args.verify:
        assert prior is not None and prior["protocol_sha256"] == sha(spec_raw)
        assert prior["implementation_sha256"] == sha(Path(__file__).read_bytes())
        assert prior["full_N4_passed"] is False and prior["economic_run"] is False
        if prior.get("download_succeeded"):
            assert raw_path.exists() and sha(raw_path.read_bytes()) == prior["raw_sha256"]
        print(json.dumps({"evidence_matches_current_source": True,
                          "download_succeeded": prior["download_succeeded"], "full_N4_passed": False}))
        return
    if prior is not None and not args.resume_local:
        raise ValueError("A sample receipt exists; verify it instead of repeating this pilot")
    ignored = subprocess.run(["git", "check-ignore", spec["raw_destination"]], cwd=ROOT,
                             capture_output=True, check=True)
    assert ignored.stdout.strip()
    report = {"schema": "regis.goal.sample_resource_report.v1", "protocol_sha256": sha(spec_raw),
              "implementation_sha256": sha(Path(__file__).read_bytes()),
              "started_utc": datetime.now(timezone.utc).isoformat(), "source_url": spec["url"],
              "attribution": spec["license"]["attribution"], "raw_git_ignored": True,
              "download_succeeded": False, "full_N4_passed": False, "economic_run": False,
              "model_admitted": False, "missing_full_N4_scenarios": spec["expected_missing_for_full_N4"]}
    start = time.perf_counter()
    tracemalloc.start()
    try:
        if not args.fetch and not args.resume_local:
            raise ValueError("First pilot must explicitly request the fixed publication attachment")
        if args.resume_local:
            assert prior is not None and prior["download_succeeded"]
            raw = raw_path.read_bytes()
            assert sha(raw) == prior["raw_sha256"]
            report.update(raw_reused_without_network=True, first_attempt_preserved=
                          "workflow_archive/QUANT-GOAL-EXECUTION-20261001-001/sample_first_attempt",
                          http_status=prior["http_status"], content_type=prior["content_type"],
                          final_host=prior["final_host"], bytes=len(raw))
        else:
            request = Request(spec["url"], headers={"User-Agent": "Regis-Oracle-publication-quality-pilot",
                                                   "Cache-Control": "no-cache"})
            with urlopen(request, timeout=30) as response:
                raw = response.read(spec["byte_cap"] + 1)
                report.update(http_status=response.status, content_type=response.headers.get("Content-Type"),
                              final_host=urlparse(response.url).hostname, bytes=len(raw))
        if len(raw) > spec["byte_cap"]:
            raise ValueError("Publication exceeds frozen byte cap")
        if not raw.startswith(b"PK"):
            raise ValueError("Response is not an XLSX ZIP")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if archive.testzip() is not None:
                raise ValueError("Corrupt ZIP")
            if any(name.lower().endswith("vbaproject.bin") for name in archive.namelist()):
                raise ValueError("Unexpected macro content")
            if sum(item.file_size for item in archive.infolist()) > 20 * spec["byte_cap"]:
                raise ValueError("Uncompressed workbook exceeds frozen resource bound")
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(raw)
        report.update(download_succeeded=True, raw_sha256=sha(raw), raw_path=spec["raw_destination"])
        from openpyxl import load_workbook
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=False)
        sheets = []
        for sheet in workbook.worksheets:
            width = sheet.max_column
            nulls, types, dates = [0] * width, [Counter() for _ in range(width)], Counter()
            strings = [Counter() for _ in range(width)]
            count = formula_count = date_parse_failures = 0
            date_order_monotonic = True
            previous_date = None
            first_row_is_data = False
            for row in sheet.iter_rows(values_only=True):
                count += 1
                if count > spec["max_streamed_rows_per_sheet"] or count * width > spec["max_streamed_cells"]:
                    raise ValueError("Frozen streaming row/cell bound exceeded")
                date_text = str(row[0])
                if count == 1:
                    first_row_is_data = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2} 00:00:00", date_text))
                try:
                    parsed = datetime.strptime(date_text, "%Y-%m-%d %H:%M:%S")
                    dates[parsed.strftime("%Y-%m-%d")] += 1
                    date_order_monotonic &= previous_date is None or parsed >= previous_date
                    previous_date = parsed
                except ValueError:
                    date_parse_failures += 1
                for col, value in enumerate(row):
                    nulls[col] += value is None
                    types[col][type(value).__name__] += 1
                    formula_count += isinstance(value, str) and value.startswith("=")
                    if isinstance(value, str) and col > 0:
                        strings[col][value] += 1
            sheets.append({"sheet": sheet.title, "physical_rows": count, "columns": width,
                           "first_row_is_data_not_header": first_row_is_data,
                           "field_names_missing": True, "security_identifiers_missing": True,
                           "null_counts": nulls, "types_by_column": [dict(c) for c in types],
                           "string_sentinels_by_column": [dict(c.most_common(5)) for c in strings],
                           "date_order_monotonic": date_order_monotonic,
                           "formula_cells": formula_count, "date_parse_failures": date_parse_failures,
                           "distinct_date_labels": len(dates),
                           "date_label_min": min(dates) if dates else None,
                           "date_label_max": max(dates) if dates else None,
                           "rows_per_date_distribution": dict(Counter(dates.values())),
                           "date_only_duplicate_rows": count - len(dates),
                           "trade_calendar_independently_verified": False,
                           "grain": "repeated rows per date; stock identity not recoverable from workbook alone",
                           "units": "numeric C2-C5 unlabeled, do not infer price/volume/attention mappings",
                           "historical_available_at": "absent; midnight date labels are not source publication timestamps"})
        workbook.close()
        report["sheets"] = sheets
        report["scope_result"] = "one published attachment parsed; dates/grain/units/PIT/stock and sector coverage require assessment"
        report["outcome_exposure"] = "entire attachment structurally inspected; any dates it represents are seen research material, never blind data"
    except Exception as exc:
        report["failure"] = {"type": type(exc).__name__, "reason": str(exc).split("?X-Goog-")[0][:300]}
        report["scope_result"] = "failure recorded; no retry, synthetic substitution or whole data admission"
    finally:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        report.update(elapsed_seconds=time.perf_counter()-start, python_tracemalloc_peak_bytes=peak,
                      peak_measurement_scope="Python traced allocation incl import/load; not OS RSS nor full market backtest estimate",
                      ended_utc=datetime.now(timezone.utc).isoformat())
        OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("download_succeeded", "full_N4_passed", "elapsed_seconds", "python_tracemalloc_peak_bytes")}, ensure_ascii=True))
    if report.get("failure"):
        print(json.dumps(report["failure"], ensure_ascii=True))
    else:
        print(json.dumps(report["sheets"], ensure_ascii=True))


if __name__ == "__main__":
    main()
