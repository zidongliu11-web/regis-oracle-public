"""Capture one bounded public historical-fee source batch; never access prices."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from collect_goal_fee_sources import VisibleText

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "research/GOAL_FEE_TIMELINE_PROTOCOL_20261002.json"
RECEIPT = ROOT / "research/GOAL_FEE_TIMELINE_RECEIPT_20261002.json"
PARSER = ROOT / "research/collect_goal_fee_sources.py"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def utc():
    return datetime.now(timezone.utc).isoformat()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(source, spec):
    row = {"id": source["id"], "url": source["url"], "started_utc": utc(),
           "request_invocations": 1, "content_markers_match": False}
    try:
        opener = build_opener(NoRedirect())
        request = Request(source["url"], headers={
            "User-Agent": "Regis-Oracle-bounded-public-rule-research"})
        try:
            response = opener.open(request, timeout=spec["limits"]["timeout_seconds"])
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(spec["limits"]["bytes_per_source"] + 1)
            row.update(http_status=response.code, final_url=response.url,
                       http_date=response.headers.get("Date"),
                       last_modified=response.headers.get("Last-Modified"),
                       etag=response.headers.get("ETag"),
                       content_type=response.headers.get("Content-Type"))
            charset = response.headers.get_content_charset()
        if len(raw) > spec["limits"]["bytes_per_source"]:
            raise ValueError("Frozen source-byte cap exceeded; response not admitted")
        target = ROOT / spec["raw_directory"] / (source["id"] + ".response")
        with target.open("xb") as file:
            file.write(raw)
        row.update(raw_path=target.relative_to(ROOT).as_posix(), bytes=len(raw),
                   raw_sha256=digest(raw))
        match = re.search(br"charset[^a-zA-Z0-9_-]+([a-zA-Z0-9_-]+)", raw[:8192])
        charset = charset or (match.group(1).decode("ascii") if match else "utf-8")
        visible = VisibleText()
        # Strict decoding preserves failures rather than silently repairing text.
        visible.feed(raw.decode(charset))
        text = visible.result()
        row["decode_charset"] = charset
        row["marker_presence"] = {marker: marker in text for marker in source["markers"]}
        row["content_markers_match"] = (row["http_status"] == 200
                                        and all(row["marker_presence"].values()))
        row["scope"] = "marker and byte evidence only; not semantic fee admission or historical availability"
    except Exception as error:
        row["failure"] = {"type": type(error).__name__, "reason": str(error)[:240]}
    row["ended_utc"] = utc()
    return row


def verify(spec, report):
    assert report["protocol_sha256"] == digest(PROTOCOL.read_bytes())
    assert report["implementation_sha256"] == digest(Path(__file__).read_bytes())
    assert report["reused_parser_sha256"] == digest(PARSER.read_bytes())
    assert [r["id"] for r in report["responses"]] == [r["id"] for r in spec["requests"]]
    assert len(report["responses"]) == spec["limits"]["native_request_invocations"] == 6
    for row in report["responses"]:
        assert row["request_invocations"] == 1
        if row.get("raw_path"):
            assert digest((ROOT / row["raw_path"]).read_bytes()) == row["raw_sha256"]
    assert report["full_fee_gate"] is False and report["economic_run"] is False
    assert report["market_API_calls"] == report["account_calls"] == report["retries"] == 0


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--collect", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    spec = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    if args.verify:
        report = json.loads(RECEIPT.read_text(encoding="utf-8"))
        verify(spec, report)
        print(json.dumps({"identities_match": True, "responses": 6, "full_fee_gate": False}))
        return
    if RECEIPT.exists():
        raise ValueError("Receipt exists; identity-only verify, never repeat requests")
    assert len(spec["requests"]) == spec["limits"]["native_request_invocations"] == 6
    directory = ROOT / spec["raw_directory"]
    directory.mkdir(parents=True, exist_ok=True)
    assert subprocess.run(["git", "check-ignore", spec["raw_directory"] + "/probe.response"],
                          cwd=ROOT, capture_output=True).returncode == 0
    started = utc()
    with ThreadPoolExecutor(max_workers=spec["limits"]["parallel_requests"]) as pool:
        rows = list(pool.map(lambda source: fetch(source, spec), spec["requests"]))
    report = {"schema": "regis.goal.fee_timeline_receipt.v1", "started_utc": started,
              "ended_utc": utc(), "protocol_sha256": digest(PROTOCOL.read_bytes()),
              "implementation_sha256": digest(Path(__file__).read_bytes()),
              "reused_parser_sha256": digest(PARSER.read_bytes()), "responses": rows,
              "full_fee_gate": False, "economic_run": False, "market_API_calls": 0,
              "account_calls": 0, "retries": 0, "raw_git_ignored": True,
              "scope": "six public native source attempts, including recorded failures; no original price rows"}
    verify(spec, report)
    RECEIPT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"native_attempts": 6,
                      "marker_matches": [r["id"] for r in rows if r["content_markers_match"]],
                      "failed_or_unmatched": [r["id"] for r in rows if not r["content_markers_match"]],
                      "full_fee_gate": False}))


if __name__ == "__main__":
    main()
