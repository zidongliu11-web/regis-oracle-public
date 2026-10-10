"""One declared guarded-transport original API same-case bulk daily capture."""
from pathlib import Path
import sys,argparse,json,socket,subprocess
ROOT=Path(__file__).resolve().parents[1];sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from research.capture_completed_daily_pair import read,write,sha,clock,completed_cutoff,first_response,claim_child_dispatch
from research.sdk_socket_guard import GuardedSocket
from research.baostock_bulk_daily_gate import DAILY_FIELDS,_daily_rows
P=ROOT/"research/GUARDED_BULK_DAILY_PROTOCOL_20261009.json";O=ROOT/"research/GUARDED_BULK_DAILY_RESULT_20261009.json";DAY="2026-10-08"
def gate(p):
    from scripts.workflow_guard import contract_hash
    t=read(ROOT/"WORKFLOW_TASK.json");a={x["path"]:x["sha256"] for x in t["artifacts"]}
    assert t["id"]==p["parent_task"] and t["status"]=="in_progress" and t["owner_session"]==p["owner_session"]
    assert t["contract_sha256"]==contract_hash(t) and a[P.relative_to(ROOT).as_posix()]==sha(P)
    assert p["day"]==DAY and p["same_daily_case_attempt_number"]==2 and p["socket_seconds"]==20 and p["child_seconds"]==100
    for f,h in p["input_hashes"].items():assert sha(ROOT/f)==h
    for f,h in p["native_sdk_hashes"].items():assert sha(f)==h
    assert "onedrive" not in str(Path(p["native_directory"]).resolve()).lower()
    completed_cutoff(clock(),DAY);return t
def child(p):
    gate(p);claim_child_dispatch(p);socket.setdefaulttimeout(20)
    def audit(event,args):
        if event=="socket.connect" and args[1]!=("public-api.baostock.com",10030):raise PermissionError("unexpected_target")
        if event in ("subprocess.Popen","os.system"):raise PermissionError("child_spawn")
    sys.addaudithook(audit);sys.path.insert(0,p["sdk_directory"])
    import baostock as bs
    import baostock.common.context as context
    import baostock.common.contants as constants
    out=Path(p["native_directory"])/"capture";jp=out/"journal.json"
    j={"started_utc":clock(),"stage":"before_guest","guest_attempts":0,"daily_query_attempts":0,"logout_attempts":0,"page_next_attempts":0,"automatic_retries":0,"failure_type":None,"transport_stats":None,"raw_ref":None,"shape":None,"runtime_guard_activated":False}
    def emit(stats):j["transport_stats"]=stats;j["transport_observed_at_utc"]=clock();write(jp,j)
    try:
        j["guest_attempts"]=1;write(jp,j);lg=bs.login();j["guest_error_code"]=str(lg.error_code);write(jp,j)
        if str(lg.error_code)!="0":raise ValueError("guest_handshake_failed")
        context.default_socket=GuardedSocket(context.default_socket,emit)
        j.update(runtime_guard_activated=True,stage="single_bulk_daily_query",daily_query_attempts=1,sent_at_utc=clock());write(jp,j)
        rs=bs.query_daily_history_k_AStock(date=DAY)
        j.update(returned_at_utc=clock(),daily_error_code=str(rs.error_code),daily_fields=list(rs.fields),cached_rows=len(rs.data) if isinstance(rs.data,list) else None);write(jp,j)
        # Preserve a bounded decoded response before exactfield/OHLC QA.
        if isinstance(rs.fields,list) and len(rs.fields)<=64 and all(isinstance(v,str) for v in rs.fields) and isinstance(rs.data,list) and len(rs.data)<=10000 and all(isinstance(r,list) and len(r)<=64 and all(isinstance(v,str) and len(v)<=512 for v in r) for r in rs.data):
            raw=out/"provider_decoded_response.json"
            write(raw,{"schema":"regis.guarded_bulk_provider_response_raw.v1","requested_day":DAY,"error_code":str(rs.error_code),"fields":list(rs.fields),"rows":rs.data,"received_at_utc":j["returned_at_utc"]},exclusive=True)
            j["provider_decoded_response_ref"]={"path":str(raw),"sha256":sha(raw)};write(jp,j)
        rows=first_response(rs,list(DAILY_FIELDS),10000)
        payload={"schema":"bulk_astock_exploration.v1","day":DAY,"fields":list(DAILY_FIELDS),"rows":[[r[f] for f in DAILY_FIELDS] for r in rows],"received_at_utc":j["returned_at_utc"],"client_version":str(constants.BAOSTOCK_CLIENT_VERSION),"runtime_guard_activated":True}
        f=out/"source.json";write(f,payload,exclusive=True);j["raw_ref"]={"path":str(f),"sha256":sha(f)};write(jp,j)
        status,shape=_daily_rows(payload,DAY);j["shape"]={"daily_rows":len(status),"shape_counts":shape,"requested_day_and_columns_exact":True,"code_date_unique":True,"fullA_typed_or_PIT_verified":False}
        j["stage"]="current_bulk_source_shape_observed";write(jp,j)
    except Exception as exc:j["failure_type"]=type(exc).__name__;j["failure_phase"]=j["stage"]
    finally:
        if j.get("runtime_guard_activated"):
            j["daily_transport_stats_before_logout"]=dict(context.default_socket.stats)
        j["stage_before_logout"]=j["stage"]
        if j.get("guest_error_code")=="0":
            j["logout_attempts"]=1;write(jp,j)
            try:j["logout_error_code"]=str(bs.logout().error_code)
            except Exception as exc:j["logout_failure_type"]=type(exc).__name__
        if hasattr(context,"default_socket"):
            try:context.default_socket.close()
            except Exception:pass
        j["ended_utc"]=clock();write(jp,j)
    return 0 if j["failure_type"] is None else 1
def run(p):
    t=gate(p);ph,ih=sha(P),sha(__file__);out=Path(p["native_directory"])/"capture";out.mkdir(exist_ok=False)
    write(out/"protocol.json",p,exclusive=True);write(out/"frozen_task.json",t,exclusive=True)
    r={"schema":"regis.guarded_bulk_daily_result.v1","started_utc":clock(),"child_returncode":None,"child_timed_out":False,"failure_type":None,"fresh_child_dispatches":1,"same_daily_case_attempt_number":2,"economic_runs":0,"new_formulas":0,"new_factor_values_computed":0,"automatic_retries":0,"raw_values_exported":False,"full_goal_complete":False,"model_admitted":False}
    phase="guarded_bulk_daily_source_capture"
    try:
        q=subprocess.run([sys.executable,"-I","-X","utf8",str(Path(__file__).resolve()),"--child"],cwd=out,capture_output=True,timeout=100,creationflags=0x08000000 if sys.platform=="win32" else 0)
        r["child_returncode"]=q.returncode;(out/"stdout.log").write_bytes(q.stdout);(out/"stderr.log").write_bytes(q.stderr)
        j=read(out/"journal.json");r["diagnostics"]=j;r["failure_type"]=j.get("failure_type")
        if q.returncode or r["failure_type"]:raise ValueError("guarded_bulk_source_not_passed")
        phase="post_source_binding";gate(p);r["state"]="current_bulk_source_shape_facts_observed_not_fullA_PIT"
    except subprocess.TimeoutExpired as exc:
        r.update(child_timed_out=True,failure_type="TimeoutExpired",failure_phase=phase)
        (out/"stdout.log").write_bytes(exc.stdout or b"");(out/"stderr.log").write_bytes(exc.stderr or b"")
    except Exception as exc:
        r["failure_type"]=r["failure_type"] or type(exc).__name__;r["failure_phase"]=phase
        if phase=="post_source_binding":r["native_source_invalidated_by_binding_failure"]=True
    if "diagnostics" not in r:
        try:r["diagnostics"]=read(out/"journal.json")
        except(OSError,ValueError):r["diagnostics"]={"partial_journal_unavailable":True}
    if r["failure_type"]:r["state"]="guarded_bulk_failure_preserved_no_retry"
    r.update(ended_utc=clock(),protocol_sha256=ph,implementation_sha256=ih,contract_sha256=t["contract_sha256"],runtime_transport_changed_and_disclosed=True,original_package_bytes_pinned=True,SDK_internal_wire_bytes_or_other_retries="unknown",socket_seconds=20,child_seconds=100,historical_PIT_verified=False,full_A_verified=False,market_gate_ready=False,orders_enabled=False,source_receipt_not_historical_available_at=True)
    write(O,r);write(out/"terminal.json",r,exclusive=True);print(json.dumps(r));return 0 if r["failure_type"] is None else 1
if __name__=="__main__":
    ap=argparse.ArgumentParser(description=__doc__);g=ap.add_mutually_exclusive_group();g.add_argument("--validate-plan",action="store_true");g.add_argument("--child",action="store_true");a=ap.parse_args();p=read(P)
    if a.validate_plan:gate(p);print(json.dumps({"frozen_scope_checked":True,"source_queries":0,"runtime_adapter_declared":True}))
    else:raise SystemExit(child(p) if a.child else run(p))
