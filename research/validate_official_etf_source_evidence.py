"""Bounded consistency checks for actual official ETF documents; never admission."""
from pathlib import Path
import json,hashlib,re,urllib.parse,base64
from decimal import Decimal
from datetime import date
R=Path.cwd()
pp="research/OFFICIAL_ETF_EVIDENCE_PROTOCOL_20261007.json"
rp="research/OFFICIAL_ETF_EVIDENCE_RESULT_20261007.json"
p=json.loads((R/pp).read_bytes());d=json.loads((R/rp).read_bytes())
sha=lambda x:hashlib.sha256(x).hexdigest()
assert d["stage"]==p["stage"]=="E166" and d["lookup_finished"]
assert d["protocol_sha256"]==sha((R/pp).read_bytes())
assert d["contract_sha256"]==json.loads((R/"WORKFLOW_TASK.json").read_bytes())["contract_sha256"]
N=Path(p["native_evidence_root"]).resolve(strict=True)
def native_bytes(name):
 assert isinstance(name,str) and re.fullmatch(r"[A-Za-z0-9_.-]+",name)
 f=N/name;assert not f.is_symlink() and f.resolve(strict=True).parent==N
 return f.read_bytes()
counts={"search":0,"search_queries":0,"open":0,"screenshot":0,"native_pdf_fetch":0}
past=[];records={};obtained_pdfs={};native_invocations=set();native_documents={}
for i,row in enumerate(d["observations"],1):
 name=row["file"];assert re.fullmatch(r"(?:web_[0-9]{2}_(?:search|open|screenshot)|source_[0-9]{2}_native_pdf_fetch)\.json",name)
 f=N/name;assert not f.is_symlink() and f.resolve(strict=True).parent==N
 raw=f.read_bytes();assert sha(raw)==row["sha256"]
 v=json.loads(raw);op=v["operation"];req=v["request"]
 assert v["schema"]==1 and v["dispatch_index"]==i and name==f"{'source' if op=='native_pdf_fetch' else 'web'}_{i:02d}_{op}.json" and op==row["operation"] and name not in records
 if op=="native_pdf_fetch":
  rule=p["native_PDF_download_refinement"]
  assert sha((N/rule["native_script"]).read_bytes())==rule["native_script_sha256"]
  assert v["transport"]=="stdlib_urllib_single_GET_no_redirect_no_retry" and v["invocation_id"]==rule["invocation_id"]
  expected=rule["documents"][counts["native_pdf_fetch"]]
  assert req=={"url":expected["url"],"method":"GET","headers":{"User-Agent":rule["user_agent"]},"timeout_seconds":15,"redirects_allowed":False,"max_response_bytes":5000000}
  assert req["url"] in obtained_pdfs
  result=v["result"];assert isinstance(result,dict) and type(result["success"]) is bool
  if result["success"]:
   assert result["status"]==200 and result["final_url"]==req["url"] and result["pdf_file"]==expected["pdf_file"]
   pdf=N/result["pdf_file"];assert not pdf.is_symlink() and pdf.resolve(strict=True).parent==N
   rawpdf=pdf.read_bytes();assert rawpdf.startswith(b"%PDF-") and 0<len(rawpdf)<=5000000 and len(rawpdf)==result["bytes_received"] and sha(rawpdf)==result["sha256"]
   native_documents[result["pdf_file"]]=result["sha256"]
  else:assert result["pdf_file"] is None and result["error"]
  counts[op]+=1;native_invocations.add(v["invocation_id"]);records[name]=(row["sha256"],op,result)
  past.append(json.dumps(result,ensure_ascii=False))
  continue
 key={"search":"search_query","open":"open","screenshot":"screenshot"}[op]
 assert set(req)<=set([key,"response_length"]) and isinstance(req[key],list) and req[key]
 assert v["result"] is not None and len(json.dumps(v["result"],ensure_ascii=False))>2
 if op=="search":
  assert len(req[key])<=p["limits"]["search_queries_per_call_max"]
  assert all(x.get("domains")==p["allowed_primary_domains"] and x["q"] for x in req[key])
  counts["search_queries"]+=len(req[key])
 else:
  assert len(req[key])==1
  ref=req[key][0]["ref_id"];assert isinstance(ref,str) and any(ref in body for body in past)
  if op=="screenshot":
   assert set(req[key][0])=={"ref_id","pageno"} and type(req[key][0]["pageno"]) is int
   assert ref in obtained_pdfs, "Screenshot requires a previously obtained successful PDF identity"
   assert 0<=req[key][0]["pageno"]<obtained_pdfs[ref]["pages"]
  else:assert set(req[key][0])<=set(["ref_id","lineno"])
 counts[op]+=1;records[name]=(row["sha256"],op,v["result"])
 if op=="open" and isinstance(v["result"],str):
  result=v["result"];header=result.split("Source: open(",1)[0]
  successful_body=("Source: open(" in result and "Content type:" in header and re.search(r"Total lines: [0-9]+",result) and re.search(r"\bL[0-9]+(?:@P[0-9]+(?:-[0-9]+)?)?:",result))
  pdf_type=re.search(r"Content type:\s*application/pdf\b",header,re.I)
  page_meta=re.search(r"Number of pages:\s*([0-9]+)",header)
  # Search hits, failed-fetch wrappers and absent PDF page counts never seed this registry.
  if successful_body and pdf_type and page_meta and int(page_meta.group(1))>0:
   pdf_urls=set(re.findall(r"https://[^\s<>()\[\]\"']+",header))
   pdf_refs=set(re.findall(r"【(turn[0-9]+(?:view|fetch)[0-9]+)】",header))
   pdf_refs.update(re.findall(r"cite(turn[0-9]+(?:view|fetch)[0-9]+)",header))
   assert pdf_urls
   for target in pdf_urls|pdf_refs:
    identity={"file":name,"pages":int(page_meta.group(1)),"urls":sorted(pdf_urls)}
    assert target not in obtained_pdfs or obtained_pdfs[target]["urls"]==identity["urls"] and obtained_pdfs[target]["pages"]==identity["pages"]
    obtained_pdfs[target]=identity
 past.append(json.dumps(v["result"],ensure_ascii=False))
assert counts==d["actual_public_calls"]
lim=p["limits"]
assert counts["search"]<=lim["search_calls_max"] and counts["search_queries"]<=lim["search_queries_max"]
assert counts["open"]+counts["native_pdf_fetch"]<=lim["open_pages_max"] and counts["screenshot"]<=lim["screenshot_calls_max"]
assert counts["native_pdf_fetch"]<=lim["native_pdf_fetch_max"]
assert len(records)<=lim["public_source_operations_max"]
assert d["actual_public_tool_invocations"]==counts["search"]+counts["open"]+counts["screenshot"]+len(native_invocations)
assert d["actual_public_tool_invocations"]<=lim["public_tool_calls_max"]
sources={}
for s in d["selected_sources"]:
 assert s["source_id"] not in sources
 u=urllib.parse.urlsplit(s["url"])
 assert u.scheme=="https" and any(u.hostname==domain or (u.hostname and u.hostname.endswith("."+domain)) for domain in p["allowed_primary_domains"])
 digest,op,result=records[s["observation"]]
 assert isinstance(result,str) and s["evidence_sha256"]==digest and s["url"] in result and s["read_scope"]
 body=("Source: open(" in result and "Content type:" in result and re.search(r"Total lines: [0-9]+",result) and re.search(r"\bL[0-9]+(?:@P[0-9]+(?:-[0-9]+)?)?:",result) and s["url"] in result.split("Source: open(",1)[0])
 if s["mode"]=="search_excerpt_only":assert op=="search"
 elif s["mode"]=="selected_text_returned":assert op=="open" and body
 else:assert s["mode"]=="unavailable" and op=="open" and not body
 sources[s["source_id"]]=s
ledger_path=N/p["ledger_rules"]["native_file"]
assert not ledger_path.is_symlink() and ledger_path.resolve(strict=True).parent==N
ledger_raw=ledger_path.read_bytes();assert sha(ledger_raw)==d["native_ledger_sha256"]
ledger=json.loads(ledger_raw)
assert ledger["status"]=="partial_unqualified" and ledger["complete_target_interval"] is False
for row in ledger["rows"]:
 assert row["source_id"] in sources and sources[row["source_id"]]["mode"]=="selected_text_returned" and row["source_locator"]
 assert row["instrument"]=="510300.SH"
 assert type(row["visual_table_verified"]) is bool
 if row["visual_table_verified"]:
  proof=row["native_visual_proof"]
  digest,op,fetched=records[proof["source_record"]]
  assert op=="native_pdf_fetch" and fetched["success"] and digest==proof["source_record_sha256"]
  assert fetched["final_url"]==sources[row["source_id"]]["url"]
  assert fetched["pdf_file"]==proof["pdf_file"] and fetched["sha256"]==proof["pdf_sha256"]
  assert proof["pdf_sha256"]==native_documents[proof["pdf_file"]]
  render_raw=native_bytes(proof["render_record"]);assert sha(render_raw)==proof["render_record_sha256"]
  render=json.loads(render_raw);assert render["render_success"] and render["returncode"]==0 and render["pdf_sha256"]==proof["pdf_sha256"]
  assert render["source_record"]==proof["source_record"]
  assert render["pdf_file"]==proof["pdf_file"] and render["pageno"]==proof["pageno"]==1 and render["binary_page_count"]>1
  png=N/render["png_file"];assert not png.is_symlink() and png.resolve(strict=True).parent==N
  png_raw=png.read_bytes();assert png_raw.startswith(b"\x89PNG\r\n\x1a\n") and sha(png_raw)==render["png_sha256"]==proof["png_sha256"]
  view_raw=native_bytes(proof["view_record"]);assert sha(view_raw)==proof["view_record_sha256"]
  view=json.loads(view_raw);assert view["schema"]==1 and view["operation"]=="view_image"
  assert Path(view["request"]["path"]).resolve(strict=True)==png.resolve(strict=True) and view["request"]["detail"]==view["result"]["detail"]=="original"
  data_url=view["result"]["image_url"];assert data_url.split(",",1)[0] in {"data:image/png;base64","data:application/octet-stream;base64"}
  assert base64.b64decode(data_url.split(",",1)[1],validate=True)==png_raw
  notes_raw=native_bytes(proof["inspection_record"]);assert sha(notes_raw)==proof["inspection_record_sha256"]
  notes=json.loads(notes_raw);assert notes["method"]=="root_agent_visual_inspection" and notes["inspected"] is True
  assert notes["source_id"]==row["source_id"] and notes["source_record"]==proof["source_record"] and notes["png_sha256"]==proof["png_sha256"]
  assert notes["view_record"]==proof["view_record"] and notes["view_record_sha256"]==proof["view_record_sha256"]
  for field in ["instrument","record_date","ex_date","pay_date","amount_as_reported","reported_unit_shares","annual_count_as_parsed"]:
   assert notes["checked_fields"][field]==row[field]
 required=p["ledger_rules"]["required_for_complete_row"]
 missing=[k for k in required if row.get(k) is None]
 assert sorted(missing)==sorted(row["unknown_fields"]) and row["qualified"] is False
 for field in ["publication_date","record_date","ex_date","pay_date"]:
  if row.get(field) is not None:assert date.fromisoformat(row[field]).isoformat()==row[field]
 if all(row.get(k) is not None for k in ["amount_as_reported","reported_unit_shares","amount_yuan_per_share"]):
  amount=Decimal(row["amount_as_reported"]);unit=Decimal(str(row["reported_unit_shares"]))
  assert amount>0 and unit>0 and amount/unit==Decimal(row["amount_yuan_per_share"])
assert len(ledger["rows"])==d["partial_notice_rows"]
assert d["complete_target_interval"] is False
assert all(d[k]==0 for k in ["new_economic_runs","new_candidates","new_admissions","raw_quote_or_SDK_API_requests","account_reads","purchases","external_messages"])
assert all(d[k] is False for k in ["financial_admitted","model_admitted","data_admitted","full_goal_complete"])
assert d["next_action"]
print("Actual official ETF source/IO/partial-ledger consistency passed; no complete data or financial admission")
