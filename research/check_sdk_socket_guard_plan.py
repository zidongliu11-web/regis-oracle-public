"""Frozen offline EOF transport repair plan, no SDK/network invocation."""
from pathlib import Path
import sys,json
ROOT=Path(__file__).resolve().parents[1];sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from research.capture_completed_daily_pair import read,sha
from scripts.workflow_guard import contract_hash
P=ROOT/"research/SDK_SOCKET_GUARD_PROTOCOL_20261009.json"
p=read(P);t=read(ROOT/"WORKFLOW_TASK.json");a={x["path"]:x["sha256"] for x in t["artifacts"]}
assert t["id"]==p["parent_task"] and t["status"]=="in_progress" and t["owner_session"]==p["owner_session"]
assert t["contract_sha256"]==contract_hash(t) and a[P.relative_to(ROOT).as_posix()]==sha(P)
assert p["source_queries"]==0 and p["formal_batches"]==4 and p["unique_fictional_integrity_cases"]==3
for f,h in p["input_hashes"].items():assert sha(ROOT/f)==h
for f,h in p["native_sdk_hashes"].items():assert sha(f)==h
print(json.dumps({"frozen_scope_checked":True,"source_queries":0,"market_raw_rows_or_numeric_values_read":False,"transport_runtime_adapter_disclosed":True,"model_admitted":False}))
