"""Bounded anonymous header inspection; never downloads market-data bodies."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "research/SHORT_RQ_HEADER_PROTOCOL_20261001.json"
RECEIPT = ROOT / "research/SHORT_RQ_HEADER_RECEIPT_20261001.json"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe(row, limits):
    result = {"id": row["id"], "requested_url": row["url"], "responses": [], "body_bytes_read": 0}
    url = row["url"]
    opener = urllib.request.build_opener(NoRedirect())
    for index in range(limits["redirects_per_url"] + 1):
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Regis-bounded-public-data-inspection", "Accept-Encoding": "identity"})
            try:
                response = opener.open(req, timeout=limits["timeout_seconds_per_request"])
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status = response.code
                selected = {k: response.headers.get(k) for k in ["Content-Length", "Content-Type", "Accept-Ranges", "Last-Modified", "ETag", "Location"]}
                result["responses"].append({"url": url, "status": status, "headers": selected})
                if status == 200:
                    result.update(successful_HEAD=True, actual_url=url)
                    break
                if status in (301, 302, 303, 307, 308) and index < limits["redirects_per_url"]:
                    next_url = urllib.parse.urljoin(url, selected["Location"] or "")
                    first = urllib.parse.urlsplit(row["url"])
                    parsed = urllib.parse.urlsplit(next_url)
                    if parsed.scheme != "https" or parsed.hostname != first.hostname or parsed.username or parsed.password:
                        raise ValueError("Reject unexpected redirect; only same-host HTTPS Location allowed")
                    url = next_url
                    continue
                break
        except Exception as error:
            result["failure"] = {"type": type(error).__name__, "reason": str(error)}
            break
    result.setdefault("successful_HEAD", False)
    return result


def main():
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--probe", action="store_true")
    modes.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    if args.verify:
        report = json.loads(RECEIPT.read_text(encoding="utf-8"))
        assert report["protocol_sha256"] == sha(PROTOCOL)
        assert report["implementation_sha256"] == sha(Path(__file__))
        for path, expected in report["discovery_source_sha256"].items():
            assert sha(ROOT / path) == expected
        assert report["market_body_bytes_read"] == 0
        assert report["economic_run"] is False
        print(json.dumps({"identity_matches": True, "market_body_bytes_read": 0, "actual_sample_acquired": False}))
        return
    if RECEIPT.exists():
        raise ValueError("Header batch already attempted; use verify, never silently repeat")
    assert sha(ROOT / protocol["source_code_path"]) == protocol["source_code_sha256"]
    discovery = json.loads((ROOT / ".codex-local/short_source_discovery_20261001/receipt.json").read_text(encoding="utf-8"))
    source_hashes = {row["path"]: row["sha256"] for row in discovery["sources"]}
    for path, expected in source_hashes.items():
        assert sha(ROOT / path) == expected
    started = datetime.now(timezone.utc).isoformat()
    results = [probe(row, protocol["limits"]) for row in protocol["URLs"]]
    report = {"schema": "regis.short_rq_header_receipt.v1", "started_utc": started, "ended_utc": datetime.now(timezone.utc).isoformat(), "protocol_sha256": sha(PROTOCOL), "implementation_sha256": sha(Path(__file__)), "discovery_source_sha256": source_hashes, "results": results, "market_body_bytes_read": 0, "actual_sample_acquired": False, "economic_run": False, "scope": "HEAD metadata only, response Last-Modified is not historical available_at or integrity authentication"}
    RECEIPT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"attempted": len(results), "successful_HEADs": sum(row["successful_HEAD"] for row in results), "market_body_bytes_read": 0, "results": results}))


if __name__ == "__main__":
    main()
