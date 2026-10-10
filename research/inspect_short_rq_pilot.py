"""One capped bundle prefix, safe local catalogue; no unpickling or price output."""
from __future__ import annotations
import argparse
import bz2
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "research/SHORT_RQ_PILOT_PROTOCOL_20261001.json"
RECEIPT = ROOT / "research/SHORT_RQ_PILOT_RECEIPT_20261001.json"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def catalogue(raw, spec):
    cap = spec["limits"]["decompressed_bytes"]
    decoder = bz2.BZ2Decompressor()
    data = decoder.decompress(raw, max_length=cap)
    rows = []
    offset = 0
    while offset + 512 <= len(data) and len(rows) < spec["limits"]["maximum_catalogue_members"]:
        header = data[offset:offset + 512]
        if header == bytes(512):
            break
        checksum = int(header[148:156].strip(b"\0 "), 8)
        if sum(header[:148]) + 8 * 32 + sum(header[156:]) != checksum:
            raise ValueError("Invalid tar header checksum")
        name = header[:100].split(b"\0", 1)[0].decode("utf-8")
        prefix = header[345:500].split(b"\0", 1)[0].decode("utf-8")
        if prefix:
            name = prefix + "/" + name
        size = int(header[124:136].strip(b"\0 ") or b"0", 8)
        kind = chr(header[156])
        complete = offset + 512 + size <= len(data)
        row = {"name": name, "type": kind, "declared_bytes": size, "complete_in_prefix": complete}
        rows.append(row)
        path = PurePosixPath(name)
        regular = kind in ("0", "\0")
        if complete and regular and not path.is_absolute() and ".." not in path.parts and path.name in spec["preselected_sample"]["admissible_complete_members"]:
            target = (ROOT / spec["raw_prefix_path"]).parent / path.name
            if target.exists():
                raise ValueError("Member file already exists; never overwrite original")
            body = data[offset + 512:offset + 512 + size]
            target.write_bytes(body)
            row.update(local_path=target.relative_to(ROOT).as_posix(), sha256=sha(target))
        offset += 512 + ((size + 511) // 512) * 512
        if not complete:
            break
    return {"decompressed_bytes": len(data), "decompression_cap_hit": len(data) == cap and not decoder.eof, "compressed_stream_complete": decoder.eof, "members": rows, "all_market_rows_output": False}


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--capture-catalogue", action="store_true")
    group.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    spec = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    if args.verify:
        report = json.loads(RECEIPT.read_text(encoding="utf-8"))
        assert report["protocol_sha256"] == sha(PROTOCOL)
        assert report["implementation_sha256"] == sha(Path(__file__))
        if report.get("raw_path"):
            assert sha(ROOT / report["raw_path"]) == report["raw_sha256"]
        for row in report.get("catalogue", {}).get("members", []):
            if row.get("local_path"):
                assert sha(ROOT / row["local_path"]) == row["sha256"]
        print(json.dumps({"identities_match": True, "price_rows_acquired": report["price_rows_acquired"], "full_N4_passed": False}))
        return
    if RECEIPT.exists():
        raise ValueError("Pilot already attempted; no implicit retries")
    assert spec["limits"]["GET_invocations"] == 1 and spec["limits"]["retries"] == spec["limits"]["redirects"] == 0
    assert sha(ROOT / "research/SHORT_RQ_HEADER_RECEIPT_20261001.json") == spec["header_receipt_sha256"]
    raw_path = ROOT / spec["raw_prefix_path"]
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    assert subprocess.run(["git", "check-ignore", spec["raw_prefix_path"]], cwd=ROOT, capture_output=True).returncode == 0
    cap = spec["limits"]["compressed_bytes"]
    report = {"schema": "regis.short_rq_catalogue_receipt.v1", "started_utc": datetime.now(timezone.utc).isoformat(), "protocol_sha256": sha(PROTOCOL), "implementation_sha256": sha(Path(__file__)), "GET_invocations": 1, "price_rows_acquired": 0, "full_N4_passed": False, "economic_run": False, "model_admitted": False, "transport_origin_authentication": "HTTP public official locator, not TLS or signed original integrity proof"}
    try:
        req = urllib.request.Request(spec["URL"], headers={"User-Agent": "Regis-bounded-local-personal-data-QC", "Range": f"bytes=0-{cap-1}", "If-Match": spec["expected_ETag"], "Accept-Encoding": "identity"})
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=spec["limits"]["timeout_seconds"]) as response:
            report.update(HTTP_status=response.status, headers={k: response.headers.get(k) for k in ["Content-Range", "Content-Length", "Content-Type", "ETag", "Last-Modified"]})
            assert response.status == 206, "Range ignored; abort before reading archive body"
            assert response.headers.get("Content-Range") == f"bytes 0-{cap-1}/{spec['expected_total_bytes']}", "Range/asset identity mismatch"
            assert response.headers.get("ETag") == spec["expected_ETag"], "Asset changed after frozen header"
            raw = response.read(cap)
            assert len(raw) == cap, "Truncated prefix"
        raw_path.write_bytes(raw)
        report.update(raw_path=spec["raw_prefix_path"], raw_sha256=sha(raw_path), raw_bytes=len(raw))
        report["catalogue"] = catalogue(raw, spec)
    except Exception as error:
        report["failure"] = {"type": type(error).__name__, "reason": str(error)}
    report["ended_utc"] = datetime.now(timezone.utc).isoformat()
    RECEIPT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"HTTP_status": report.get("HTTP_status"), "raw_bytes": report.get("raw_bytes", 0), "catalogue": report.get("catalogue"), "failure": report.get("failure"), "price_rows_acquired": 0}))
    if "failure" in report:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
