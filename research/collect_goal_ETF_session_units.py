"""One anonymous provider day versus independently displayed session totals."""
from pathlib import Path
from datetime import datetime, timezone
from contextlib import redirect_stdout, redirect_stderr
import argparse,hashlib,io,json,socket,sys
from decimal import Decimal

ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/"research/GOAL_ETF_SESSION_PROTOCOL_20261002.json"
RECEIPT=ROOT/"research/GOAL_ETF_SESSION_RECEIPT_20261002.json"

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--verify",action="store_true");args=ap.parse_args()
    p=json.loads(PROTOCOL.read_bytes())
    if sha(Path(__file__))!=p["script_sha256"]:raise ValueError("script changed")
    for s in p["sdk_inputs"]:
        if sha(ROOT/s["path"])!=s["sha256"]:raise ValueError("pinned SDK changed")
    public=ROOT/p["public_path"]
    if sha(public)!=p["public_sha256"]:raise ValueError("public source changed")
    target=ROOT/p["raw_path"]
    if args.verify:
        q=json.loads(RECEIPT.read_bytes())
        if q["protocol_sha256"]!=sha(PROTOCOL) or q["script_sha256"]!=p["script_sha256"]:
            raise ValueError("receipt stale")
        if q.get("raw_sha256") and sha(target)!=q["raw_sha256"]:raise ValueError("new raw changed")
        print(json.dumps({"identity_verified":True,"new_network":False,"full_N4":False}));return
    if RECEIPT.exists() or target.exists():raise ValueError("one-time collection already exists")
    sys.path.insert(0,str(ROOT/".codex-local/deps"))
    import baostock as bs
    if not Path(bs.__file__).resolve().is_relative_to(ROOT/".codex-local/deps"):
        raise ValueError("unexpected SDK package")
    q={"schema":"regis.goal.ETF_session_unit_receipt.v1","started_utc":datetime.now(timezone.utc).isoformat(),
       "protocol_sha256":sha(PROTOCOL),"script_sha256":p["script_sha256"],
       "market_queries_attempted":0,"SDK_calls_attempted":0,"retry_calls":0,"account_or_saved_credentials_read":False,
       "raw_sha256":None,"matched_key":False,"native_fund_units_exact_to_regular":False,
       "amount_exact_to_regular_display_tolerance":False,"native_total_including_afterhours_match":False,
       "raw_values_output":0,"raw_prices_requested":False,"economic_run":False,"full_N4":False,"model_admitted":False}
    socket.setdefaulttimeout(15);connected=False;rows=[];fields=[]
    try:
        q["SDK_calls_attempted"]+=1
        with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):login=bs.login()
        q["login_error_code"]=str(login.error_code)
        if login.error_code!="0":raise ValueError("anonymous_login_rejected")
        connected=True;q["market_queries_attempted"]+=1;q["SDK_calls_attempted"]+=1
        with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            response=bs.query_history_k_data_plus(p["code"],",".join(p["fields"]),start_date=p["day"],end_date=p["day"],frequency="d",adjustflag="3")
        q["query_error_code"]=str(response.error_code);fields=list(response.fields or [])
        if response.error_code!="0" or fields!=p["fields"]:
            raise ValueError("provider_reject_or_field_mismatch")
        rows=response.data
        if not isinstance(rows,list) or len(rows)>p["max_rows"] or len(rows)>=bs.common.contants.BAOSTOCK_PER_PAGE_COUNT:
            raise ValueError("unexpected_rows_or_pagination")
        raw={"schema":"regis.single_BaoStock_ETF_quantity_raw.v1","received_utc":datetime.now(timezone.utc).isoformat(),"fields":fields,"rows":rows}
        data=(json.dumps(raw,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n").encode("utf-8")
        if len(data)>p["max_raw_bytes"]:raise ValueError("raw_byte_limit")
        target.parent.mkdir(parents=True,exist_ok=True)
        with target.open("xb") as f:f.write(data)
        q["raw_sha256"]=sha(target);q["raw_bytes"]=len(data);q["row_count"]=len(rows)
        if len(rows)!=1 or not isinstance(rows[0],list) or len(rows[0])!=len(fields):
            raise ValueError("expected_single_row_missing")
        row=dict(zip(fields,rows[0]))
        if row["date"]!=p["day"] or row["code"]!=p["code"] or row["adjustflag"]!="3":
            raise ValueError("source_identity")
        values={f:Decimal(row[f]) for f in ("volume","amount")}
        if any(not v.is_finite() or v<0 for v in values.values()) or values["volume"]!=values["volume"].to_integral_value():
            raise ValueError("numeric_invalid")
        o=json.loads(public.read_bytes())
        if o["day"]!=p["day"] or o["code"]!=p["code"][3:]:raise ValueError("public_identity")
        regular=Decimal(o["regular_volume_hands"])*100
        after=Decimal(o["afterhours_volume_hands"])*100
        q.update(matched_key=True,
                 native_fund_units_exact_to_regular=values["volume"]==regular,
                 amount_exact_to_regular_display_tolerance=abs(values["amount"]-Decimal(o["regular_amount_10k_CNY"])*10000)<=Decimal(100),
                 native_total_including_afterhours_match=values["volume"]==regular+after,
                 SSE_total_equals_regular_plus_afterhours=regular+after==Decimal(o["total_volume_10k_units"])*10000,
                 wrong_native_volume_x100_rejected=values["volume"]*100!=regular,
                 unit_mapping_supported=values["volume"]==regular,
                 coverage_decision="test exact regular-session match; if regular match and total mismatch, reject use as full-session volume")
        q["status"]="comparison_completed"
    except Exception as exc:
        q["status"]="capture_or_QC_failed";q["failure_type"]=type(exc).__name__
        # No vendor error text or raw message escapes local storage.
    finally:
        if connected:
            q["SDK_calls_attempted"]+=1
            try:
                with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):bs.logout()
            except Exception:q["logout_failed"]=True
    q["ended_utc"]=datetime.now(timezone.utc).isoformat()
    with RECEIPT.open("x",encoding="utf-8",newline="\n") as f:json.dump(q,f,ensure_ascii=False,indent=2);f.write("\n")
    print(json.dumps({k:q[k] for k in ("status","market_queries_attempted","matched_key","native_fund_units_exact_to_regular","amount_exact_to_regular_display_tolerance","native_total_including_afterhours_match","full_N4")}))

if __name__=="__main__":main()
