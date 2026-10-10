"""One original ChinaClear2022 notice capture; reuse existing bounded HTML reader."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from collect_goal_fee_timeline import fetch
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/"research/GOAL_TRANSFER_ORIGINAL_PROTOCOL_20261002.json"
RECEIPT=ROOT/"research/GOAL_TRANSFER_ORIGINAL_RECEIPT_20261002.json"
DEPENDENCIES=["research/collect_goal_fee_timeline.py","research/collect_goal_fee_sources.py"]
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def main():
 p=argparse.ArgumentParser();m=p.add_mutually_exclusive_group(required=True);m.add_argument("--capture",action="store_true");m.add_argument("--verify",action="store_true");args=p.parse_args()
 spec=json.loads(PROTOCOL.read_text(encoding="utf-8"))
 if args.verify:
  report=json.loads(RECEIPT.read_text(encoding="utf-8"));assert report["protocol_sha256"]==sha(PROTOCOL) and report["implementation_sha256"]==sha(Path(__file__))
  for f,h in report["dependency_sha256"].items():assert sha(ROOT/f)==h
  row=report["response"]
  if row.get("raw_path"):assert sha(ROOT/row["raw_path"])==row["raw_sha256"]
  assert report["native_attempts"]==1 and report["full_fee_gate"] is False and report["economic_run"] is False
  print(json.dumps({"identities_match":True,"candidate_content_markers_match":row["content_markers_match"],"full_fee_gate":False}));return
 if RECEIPT.exists():raise ValueError("Original already attempted; verify only, never repeat request")
 assert len(spec["requests"])==spec["limits"]["native_request_invocations"]==1
 assert spec["limits"]["redirects"]==spec["limits"]["retries"]==0
 (ROOT/spec["raw_directory"]).mkdir(parents=True,exist_ok=True)
 assert subprocess.run(["git","check-ignore",spec["raw_directory"]+"/probe.response"],cwd=ROOT,capture_output=True).returncode==0
 started=datetime.now(timezone.utc).isoformat();row=fetch(spec["requests"][0],spec)
 report={"schema":"regis.goal.original_transfer_notice_receipt.v1","started_utc":started,"ended_utc":datetime.now(timezone.utc).isoformat(),"protocol_sha256":sha(PROTOCOL),"implementation_sha256":sha(Path(__file__)),"dependency_sha256":{f:sha(ROOT/f) for f in DEPENDENCIES},"response":row,"native_attempts":1,"full_fee_gate":False,"economic_run":False,"market_account_calls":0,"scope":"one native issuer source candidate only; failed source does not become rate evidence; independent semantic review required"}
 RECEIPT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
 print(json.dumps({"native_attempts":1,"http_status":row.get("http_status"),"candidate_content_markers_match":row["content_markers_match"],"failure":row.get("failure"),"full_fee_gate":False}))
 if not row["content_markers_match"]:sys.exit(1)
if __name__=="__main__":main()
