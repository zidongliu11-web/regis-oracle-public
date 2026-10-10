"""One bounded primary fee-rule snapshot batch. No market/account access."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlparse
from urllib.request import Request,urlopen

ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/"research/GOAL_FEE_SOURCE_PROTOCOL_20261001.json"
OUTPUT=ROOT/"research/GOAL_FEE_SOURCE_RECEIPT_20261001.json"
def digest(raw):return hashlib.sha256(raw).hexdigest()

class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts=[];self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag in {"script","style","noscript"}:self.skip+=1
    def handle_endtag(self,tag):
        if tag in {"script","style","noscript"}:self.skip=max(0,self.skip-1)
    def handle_data(self,data):
        if not self.skip:self.parts.append(data)
    def result(self):return " ".join(" ".join(self.parts).split())

def main():
    p=argparse.ArgumentParser();p.add_argument("--collect",action="store_true");p.add_argument("--verify",action="store_true")
    args=p.parse_args()
    raw_protocol=PROTOCOL.read_bytes();spec=json.loads(raw_protocol)
    if args.verify:
        report=json.loads(OUTPUT.read_text(encoding="utf-8"))
        if report["protocol_sha256"]!=digest(raw_protocol) or report["implementation_sha256"]!=digest(Path(__file__).read_bytes()):
            raise ValueError("Fee evidence source/protocol changed")
        for row in report["responses"]:
            if row.get("raw_path") and digest((ROOT/row["raw_path"]).read_bytes())!=row["raw_sha256"]:
                raise ValueError("Fee source raw byte drift")
        if report["full_fee_gate"] is not False or report["economic_run"] is not False:
            raise ValueError("Source snapshots cannot admit a full fee/economic gate")
        print(json.dumps({"fee_source_identities_match":True,"responses":len(report["responses"]),"full_fee_gate":False}))
        return
    if not args.collect or OUTPUT.exists():raise ValueError("One batch only; verify existing report, do not recollect")
    directory=ROOT/spec["raw_directory"];directory.mkdir(parents=True,exist_ok=True)
    ignore=subprocess.run(["git","check-ignore",spec["raw_directory"]+"/probe.response"],cwd=ROOT,capture_output=True)
    if ignore.returncode!=0:raise ValueError("Public rule raw snapshots must remain Git ignored")
    started=datetime.now(timezone.utc).isoformat()
    def fetch(source):
        row={"id":source["id"],"url":source["url"],"started_utc":datetime.now(timezone.utc).isoformat(),"rule_content_verified":False}
        try:
            with urlopen(Request(source["url"],headers={"User-Agent":"Regis-Oracle-bounded-rule-research"}),timeout=spec["limits"]["timeout_seconds"]) as response:
                raw=response.read(spec["limits"]["bytes_per_source"]+1)
                row.update(http_status=response.status,content_type=response.headers.get("Content-Type"),final_host=urlparse(response.url).hostname)
                charset=response.headers.get_content_charset()
            if len(raw)>spec["limits"]["bytes_per_source"]:raise ValueError("Frozen fee source byte cap exceeded")
            target=directory/(source["id"]+".response")
            with target.open("xb") as file:file.write(raw)
            row.update(bytes=len(raw),raw_path=target.relative_to(ROOT).as_posix(),raw_sha256=digest(raw))
            if raw.startswith(b"%PDF"):row["inspection"]="PDF bytes only; no rate admitted from unparsed file"
            else:
                match=re.search(br"charset[^a-zA-Z0-9_-]+([a-zA-Z0-9_-]+)",raw[:8192])
                charset=charset or (match.group(1).decode("ascii") if match else "utf-8")
                try:text=raw.decode(charset)
                except (UnicodeDecodeError,LookupError):text=raw.decode("gb18030",errors="replace")
                parser=VisibleText();parser.feed(text);visible=parser.result()
                row["maintenance_or_error_page"]=("maintain" in str(row["final_host"]).lower() or "系统维护" in visible or "404 Not Found" in visible)
                row["marker_presence"]={marker:marker in visible for marker in source["markers"]}
                row["rule_content_verified"]=bool(source["markers"]) and all(row["marker_presence"].values()) and not row["maintenance_or_error_page"]
                row["inspection"]="HTML visible marker presence only; semantic rates/periods require independent review"
        except Exception as exc:row["failure"]={"type":type(exc).__name__,"reason":str(exc)[:180]}
        row["ended_utc"]=datetime.now(timezone.utc).isoformat();return row
    with ThreadPoolExecutor(max_workers=spec["limits"]["parallel_requests"]) as pool:
        rows=list(pool.map(fetch,spec["requests"]))
    report={"schema":"regis.goal.fee_source_receipt.v1","started_utc":started,"ended_utc":datetime.now(timezone.utc).isoformat(),
    "protocol_sha256":digest(raw_protocol),"implementation_sha256":digest(Path(__file__).read_bytes()),
    "responses":rows,"raw_git_ignored":True,"economic_run":False,"full_fee_gate":False,
    "scope":"official fee-rule responses only; content markers are not independent semantic admission"}
    OUTPUT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"responses":len(rows),"marker_matches":[r["id"] for r in rows if r["rule_content_verified"]],
    "missing_or_unverified":[r["id"] for r in rows if not r["rule_content_verified"]],"full_fee_gate":False}))

if __name__=="__main__":main()
