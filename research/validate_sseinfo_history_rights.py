"""Single bounded documentary check; no network, market or economic computation."""
from __future__ import annotations
import hashlib, json, re
from pathlib import Path
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/"research/SSEINFO_HISTORY_RIGHTS_PROTOCOL_20261007.json"
RESULT=ROOT/"research/SSEINFO_HISTORY_RIGHTS_RESULT_20261007.json"

def require(ok, why):
    if not ok: raise ValueError(why)

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def unique(pairs):
    out={}
    for k,v in pairs:
        require(k not in out, "Duplicate JSON field")
        out[k]=v
    return out

def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=unique,
                      parse_constant=lambda s: (_ for _ in ()).throw(ValueError("Non-finite JSON")))

def primary(url, domains):
    p=urlparse(url)
    return p.scheme in ("http","https") and not p.username and not p.password and any(
        p.hostname==d or (p.hostname or "").endswith("."+d) for d in domains)

def main():
    p=read(PROTOCOL); r=read(RESULT); t=read(ROOT/"WORKFLOW_TASK.json")
    n=Path(p["native_evidence_root"]).resolve()
    require(n.name=="E168-SSEINFO-history-rights-20261007", "Native scope")
    # Dispatch is exclusive; a failure is retained, not silently rerun.
    with (n/"formal_check.dispatched").open("x",encoding="utf-8") as f:
        f.write(t["contract_sha256"]+"\n")
    require(r["contract_sha256"]==t["contract_sha256"], "Current source contract")
    require(r["protocol_sha256"]==sha(PROTOCOL), "Protocol")
    require(t["status"]=="in_progress" and t["id"]==p["parent"], "Parent retained")
    old=read(n/"task.before.json")
    require(t["checks"][:-1]==old["checks"], "Old checks")
    require(t["planning"]["steps"][:-1]==old["planning"]["steps"], "Old steps")
    require(t["research_gates"]==old["research_gates"], "Research gates")
    require(len(t["requirements"])==len(old["requirements"]), "Requirements count")
    for before,after in zip(old["requirements"],t["requirements"]):
        unchanged={k:v for k,v in after.items() if k!="checks"}
        require(unchanged=={k:v for k,v in before.items() if k!="checks"}, "Old requirement meanings")
        require(after["checks"][:len(before["checks"])]==before["checks"], "Old requirement checks")
    require(r["full_goal_complete"] is False and r["financial_admitted"] is False
            and r["model_admitted"] is False, "No admission")
    for key in ("raw_quote_or_SDK_requests","source_rows_requested","accounts_or_tokens_read",
                "external_messages","purchases","new_economic_runs","new_candidates",
                "new_admissions","synthetic_checker_runs"):
        require(r["actual_counts"][key]==0, "Zero-only scope:"+key)
    require(r["actual_counts"]["formal_document_checks"]==0 and r["planned_document_checks"]==1, "Frozen source snapshot before exactly one actual dispatch")
    basis=p["source_link_basis"]
    require(sha(basis["record"])==r["prior_source_sha256"], "Prior exact source")
    prior=read(basis["record"])["result"]
    require(basis["ref"] in prior and "22" in prior and basis["host"] in prior, "Prior source link")
    bodies={basis["ref"]:prior}; urls=set(); denied_urls=set(); records={}
    counts={"public_tool_calls":0,"search_calls":0,"search_queries":0,"document_opens_or_clicks":0}
    for i,rec in enumerate(r["io_records"],1):
        rel=rec["file"]; path=(n/rel).resolve()
        require(path.is_relative_to(n) and path.name.startswith("web_"), "Native file")
        require(sha(path)==rec["sha256"], "Exact native IO")
        v=read(path); req=v["request"]; body=v["result"]
        require(v["dispatch_index"]==i and isinstance(body,str), "Dispatch/body")
        op=v["operation"]
        if i==1:
            require(op=="click" and req.get("click")==[{"ref_id":basis["ref"],"id":basis["link_id"]}],
                    "First actual known official channel link")
        require(op in ("search_query","open","click") and set(req)=={op,"response_length"},
                "Bounded exact request")
        counts["public_tool_calls"]+=1
        items=req[op]
        if op=="search_query":
            counts["search_calls"]+=1; counts["search_queries"]+=len(items)
            require(0<len(items)<=2 and all(x.get("domains")==p["allowed_domains"] for x in items),
                    "Search primary domains")
        else:
            counts["document_opens_or_clicks"]+=1
            require(len(items)==1, "Single adaptive target")
            item=items[0]
            if op=="click":
                require(item["ref_id"] in bodies, "Previously returned reference")
                parent=bodies[item["ref_id"]]
                target=re.search(r"(?:|\[)"+str(item["id"])+r"†[^\n\]]+†([a-zA-Z0-9.-]+)(?:|\])",parent)
                require(target is not None and any(target.group(1)==d or target.group(1).endswith("."+d)
                        for d in p["allowed_domains"]), "Previously returned explicit primary link host")
                if i==1: require(target.group(1)==basis["host"], "Exact prior SSEINFO channel host")
            else:
                url=item["ref_id"]
                fallback= (url=="https://www.sseinfo.com/" and i==2
                           and r.get("fallback_root_inferred") is True
                           and records[1]["operation"]=="click"
                           and records[1]["request"]["click"]==[{"ref_id":basis["ref"],"id":basis["link_id"]}]
                           and records[1]["reference_expired"])
                require((url in urls or fallback) and url not in denied_urls, "Returned successful URL or counted ref-expiry fallback; no denied URL retry")
                require(primary(url,p["allowed_domains"]), "Primary target")
        # Positive documents require actual content lines and official returned header, not a search excerpt.
        match=re.search(r"\((https?://[^\s)]+)\)\s*\n",body)
        linebody=re.search(r"(?m)^L\d+(?:@P[\d-]+)?:",body)
        failed=any(s in body for s in ("Failed to fetch","Internal Error","URL is not safe to open"))
        successful=op!="search_query" and bool(match and linebody) and not failed
        if successful:
            require(primary(match.group(1),p["allowed_domains"]), "Official response")
        refs=re.findall(r"(?:|【)(turn\d+[A-Za-z]+\d+)",body)
        if successful:
            for ref in refs: bodies[ref]=body
        # Search/header URLs are only pointers. No raw/SDK download is executed.
        for url in re.findall(r"https?://[^\s)\"<>]+",body):
            url=url.rstrip(".,;")
            if primary(url,p["allowed_domains"]):
                if failed and op!="search_query": denied_urls.add(url)
                elif successful or op=="search_query": urls.add(url)
        expired= any(s in body.lower() for s in ("invalid ref_id","invalid ref id","ref_id is invalid",
                    "reference id not found","could not find reference","unknown reference","invalid reference"))
        records[i]={"body":body,"successful":successful,"file":rel,"operation":op,
                    "request":req,"reference_expired":expired}
    lim=p["limits"]
    for key,val in counts.items():
        require(val==r["actual_counts"][key] and val<=lim[key+"_max"], "Actual bound:"+key)
    require(len(records)>0, "No invented research")
    for card in r["document_cards"]:
        i=card["dispatch_index"]; rec=records[i]
        require(rec["successful"] and rec["file"]==card["io_file"], "Actual successful source")
        require(card["io_sha256"]==sha(n/card["io_file"]), "Source card hash")
        require(card["url"] in rec["body"] and primary(card["url"],p["allowed_domains"]), "Exact source URL")
        require(card["supporting_lines"] and all(line in rec["body"] for line in card["supporting_lines"]),
                "Exact observed support")
        observed=set(int(x) for x in re.findall(r"L(\d+)(?:@P[\d-]+)?:",rec["body"]))
        require(card["supporting_line_numbers"] and set(card["supporting_line_numbers"])<=observed,
                "Actual observed line locators")
        require(card["scope"] and card["limits"], "Document scope")
    require(r["findings"] and all(f["status"] in ("documented","unknown","unavailable") for f in r["findings"]),
            "Explicit finding status")
    for f in r["findings"]:
        require(f["claim"] and f["limits"], "No unsupported blanket conclusion")
        require(f["supporting_document_indices"] or f["status"]!="documented", "Document basis")
        require(all(i in [c["dispatch_index"] for c in r["document_cards"]]
                    for i in f["supporting_document_indices"]), "Finding source")
    future=["GOAL_REAL_EXPERIMENT_PROTOCOL_20261001.json","GOAL_M24_REAL_REPLAY_20261001.json",
            "GOAL_ECONOMIC_SCREEN_20261001.json","GOAL_FORWARD_ADMISSION_20261001.json"]
    require(all(not (ROOT/"research"/name).exists() for name in future), "No forged future artifacts")
    print(json.dumps({"documentary_check":"passed","actual_counts":r["actual_counts"],
                      "document_cards":len(r["document_cards"]),"formal_document_checks_executed":1,"market_or_economic_runs":0,
                      "no_source_or_financial_admission":True},ensure_ascii=False))

if __name__=="__main__": main()
