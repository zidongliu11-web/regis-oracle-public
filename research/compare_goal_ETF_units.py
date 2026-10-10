"""One independent public unit comparison, no raw market output or returns."""
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
import argparse, hashlib, json
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from research.m24_data_adapter import _unique_object, number

PROTOCOL=ROOT/"research/GOAL_ETF_UNIT_PROTOCOL_20261002.json"
RECEIPT=ROOT/"research/GOAL_ETF_UNIT_RECEIPT_20261002.json"

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--verify",action="store_true");args=ap.parse_args()
    p=json.loads(PROTOCOL.read_bytes())
    if sha(Path(__file__)) != p["script_sha256"]:raise ValueError("script changed")
    objects=[]
    for spec in p["objects"]:
        path=(ROOT/spec["path"]).resolve()
        if not path.is_relative_to(ROOT) or path.stat().st_size>p["max_input_bytes"]:
            raise ValueError("path or input byte limit")
        if sha(path)!=spec["sha256"]:raise ValueError("source changed")
        objects.append(json.loads(path.read_bytes(),object_pairs_hook=_unique_object))
    if args.verify:
        q=json.loads(RECEIPT.read_bytes())
        if q["protocol_sha256"]!=sha(PROTOCOL) or q["script_sha256"]!=p["script_sha256"]:
            raise ValueError("receipt stale")
        print(json.dumps({"identity_verified":True,"recompared":False,"full_N4":False}));return
    if RECEIPT.exists():raise ValueError("one-time comparison already exists")
    source,official=objects
    if source.get("schema")!="baostock_symbol_etf_raw.v1" or len(source["rows"])>10000:
        raise ValueError("raw source schema/row limit")
    idx={f:i for i,f in enumerate(source["fields"])}
    if len(idx)!=len(source["fields"]) or not {"code","date","volume","amount","adjustflag"}.issubset(idx):
        raise ValueError("source fields")
    matches=[row for row in source["rows"] if len(row)==len(idx) and row[idx["code"]]==p["code"] and row[idx["date"]]==p["day"]]
    if len(matches)!=1:raise ValueError("expected one raw key")
    if (official["code"]!=p["code"][3:] or official["date"]!=p["day"]
            or official["selected_date_UI"]!=p["day"] or not official["price_date_markers_match"]
            or official["volume_unit"]!="万份" or official["amount_unit"]!="万元"):
        raise ValueError("public date/code/unit mismatch")
    row=matches[0]
    volume=number(row[idx["volume"]],positive=True)
    amount=number(row[idx["amount"]],positive=True)
    total_units=number(official["volume"],positive=True)*10000
    total_CNY=number(official["amount"],positive=True)*10000
    if volume!=volume.to_integral_value() or row[idx["adjustflag"]]!="3":
        raise ValueError("invalid native volume or raw flag")
    session=official["SSE_current_date_session_evidence"]
    session_checksum=(number(session["regular_volume_hands"])+number(session["afterhours_volume_hands"]))*100/10000==number(session["total_volume_10k_units"])
    scale_results=[]
    for scale in p["scale_hypotheses"]:
        error=abs(volume*scale-total_units)
        scale_results.append({"scale":scale,"within_frozen_display_tolerance":error<=number(p["unit_display_tolerance"]),
                              "within_frozen_one_percent_diagnostic":error/total_units<=Decimal("0.01")})
    q={"schema":"regis.goal.ETF_unit_receipt.v1","ran_utc":datetime.now(timezone.utc).isoformat(),
       "protocol_sha256":sha(PROTOCOL),"script_sha256":p["script_sha256"],
       "objects":p["objects"],"single_key_present":True,
       "official_code_day_and_labels_match":True,"SSE_hand_units_afterhours_checksum":session_checksum,
       "scale_results":scale_results,
       "amount_within_frozen_display_tolerance":abs(amount-total_CNY)<=number(p["amount_display_tolerance"]),
       "amount_within_one_percent_diagnostic":abs(amount-total_CNY)/total_CNY<=Decimal("0.01"),
       "native_count_x1_fund_units_supported":bool(session_checksum and scale_results[0]["within_frozen_one_percent_diagnostic"] and not scale_results[1]["within_frozen_one_percent_diagnostic"]),
       "exact_total_volume_coverage_proved":scale_results[0]["within_frozen_display_tolerance"],
       "scope":"one historical public rounded-row comparison, modern SSE rendered factual transcription; no native public response archive, no full vendor/source coverage grant",
       "public_includes_afterhours_inference":"current-date exact regular+afterhours checksum supports total statistic scope; selected-day composition not separately observed",
       "private_numeric_values_emitted":0,"raw_price_output":0,"network_calls":0,
       "returns_calculated":False,"full_N4":False,"real_ledger_import_enabled":False,"model_admitted":False}
    with RECEIPT.open("x",encoding="utf-8",newline="\n") as f:json.dump(q,f,ensure_ascii=False,indent=2);f.write("\n")
    print(json.dumps({k:q[k] for k in ["native_count_x1_fund_units_supported","exact_total_volume_coverage_proved","amount_within_frozen_display_tolerance","full_N4"]}))

if __name__=="__main__":main()
