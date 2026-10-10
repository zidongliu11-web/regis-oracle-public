"""Readonly identity/source diagnostic; never acquires or rewrites market quotes."""
import hashlib,json,re,sys,unicodedata
from pathlib import Path
from datetime import datetime,timezone
from pypdf import PdfReader
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R))
from research.check_source_coverage import Tables
from research.read_instruments_static import parse,date_int
P=R/"research/STOCK_UNKNOWN_IDENTITY_PROTOCOL_20261003.json"
O=R/"research/STOCK_UNKNOWN_IDENTITY_RESULT_20261003.json"
def read(p):return json.loads(Path(p).read_text(encoding="utf-8-sig"))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def norm(s):return re.sub(r"\s+","",unicodedata.normalize("NFKC",s))
def rebuild(s):
 for p,h in s["input_hashes"].items():assert sha(R/p)==h,p
 for p,h in s["native_input_hashes"].items():assert sha(p)==h,p
 rows,_=parse(Path(s["instruments"]).read_bytes());u=[r for r in rows if r.get("type")=="CS" and date_int(r.get("listed_date")) is None]
 inventory=read(s["inventory"]);assert len(u)==19
 identity={"unknown_records":len(u),"all_19_status_Unknown":all(r.get("status")=="Unknown" for r in u),"all_19_same_future_date_sentinel":all(r.get("listed_date")=="2999-12-31" and r.get("de_listed_date")=="0000-00-00" for r in u),"unknown_keys_in_existing_price_inventory":sum(r["order_book_id"] in inventory for r in u),"stored_quote_rows_in_inventory":sum(inventory.get(r["order_book_id"],{}).get("stored_rows",0) for r in u),"selected_quote_rows_in_inventory":sum(inventory.get(r["order_book_id"],{}).get("selected_rows",0) for r in u),"all_A_absence_proven":False,"source_identifiers_removed":0,"price_arrays_read":False}
 D=Path(s["source_directory"]);m=read(D/"manifest.json");assert m["actual_GET_attempts"]==5 and m["market_quote_calls"]==0 and not m["account_read"] and not m["purchase"] and m["contacts"]==0
 docs=[]
 for z in m["receipts"]:
  assert z["attempts"]==1 and not z["auth_used"] and z["status"]==200 and z["saved"] and z["within_byte_cap"]
  p=Path(z["body_path"]);assert sha(p)==z["sha256"] and p.stat().st_size==z["bytes"]
  v={"id":z["id"],"url":z["url"],"sha256":z["sha256"],"bytes":z["bytes"]}
  if z["format"]=="HTML":
   q=Tables();q.feed(p.read_text(encoding="utf-8"));t=" ".join(q.text);n=norm(t)
   if z["id"]=="rq_generic_docs":
    a=[row for table in q.tables for row in table if row and row[0]=="status"];assert len(a)==1
    v["findings"]={"Unknown_in_status_row":any("Unknown" in " ".join(row) for row in a),"2999_sentinel_documented": "2999-12-31" in t,"PreIPO_FailIPO_words_present_not_current_support_promise":all(k in " ".join(a[0]) for k in ["PreIPO","FailIPO"]),"Unknown_equals_failed_IPO_assigned":False}
   else:
    v["findings"]={"issuer_name": "苏州恒久光电科技股份有限公司" in n,"revocation_of_20100226_permission": "撤销2010年2月26日做出的" in n,"permit250_cancelled": "注销证监许可" in n and "250" in n,"decision_date20100613": "二○一○年六月十三日" in n or "二〇一〇年六月十三日" in n}
    assert all(v["findings"].values())
  else:
   pdf=PdfReader(p,strict=True);v["pages"]=len(pdf.pages)
   if len(pdf.pages)>s["PDF_page_cap"]:
    v.update(state="page_count_ceiling_failed_preserved",failure_stage="page_count_before_text",text_pages_read=0,hits=[]);assert z["id"]=="shengjing_revocation_2011_gazette" and len(pdf.pages)==160;docs.append(v);continue
   hits=[]
   for i,page in enumerate(pdf.pages):
    c=page.get_contents();assert c is None or len(c.get_data())<=s["page_stream_byte_cap"]
    n=norm(page.extract_text() or "")
    if z["id"]=="suzhou_issuance_2010" and "300060" in n and "苏州恒久" in n:
     hits.append({"page":i+1,"issuer_full_name":"苏州恒久光电科技股份有限公司" in n,"application_code_and_planned_listing_not_actual_listing":"申购代码" in n and "本次发行的股票拟在深交所创业板上市" in n,"permission250_present":"2010" in n and "250" in n,"notice_date20100308":"2010年3月8日" in n})
    elif z["id"]=="shengjing_code_suspended_2010Q4" and "002525" in n:
     hits.append({"page":i+1,"code_name_bound":bool(re.search(r"002525.{0,8}胜景山河",n)),"temporarily_unlisted_label":"暂缓上市" in n})
   v.update(text_pages_read=len(pdf.pages),hits=hits,state="bounded_identity_predicates_checked")
  docs.append(v)
 a=next(v for v in docs if v["id"]=="suzhou_issuance_2010");assert len(a["hits"])==1 and all(val for k,val in a["hits"][0].items() if k!="page")
 b=next(v for v in docs if v["id"]=="shengjing_code_suspended_2010Q4");assert any(h["code_name_bound"] and h["temporarily_unlisted_label"] for h in b["hits"])
 names={r["order_book_id"]:r for r in u};assert "恒久" in names["300060.XSHE"]["symbol"] and names["002525.XSHE"]["symbol"]=="胜景山河"
 failure=read(D/"first_predicate_scan_failure.json");assert failure["actual_returncode"]==1 and failure["unchanged_PDF_page_cap"]==150 and sha(D/"initial_predicate_reader.py")==failure["original_reader_sha256"]
 E=Path(s["ETF_preflight_directory"]);home=read(E/"home.receipt.json");indexes=read(E/"dated-indices.receipt.json")
 assert home["actual_GET_attempts"]==1 and home["status"]==200 and not home["saved"] and home["error_type"]=="AssertionError"
 assert indexes["actual_GETs"]==2 and indexes["original_PDFs_acquired"]==0 and all(not r["saved"] and r["error_type"]=="HTTPError" for r in indexes["sources"])
 return {"identity_inventory":identity,"primary_documents":docs,"source_specific_conclusions":{"300060_2010_application_code_and_revocation_chain_supported":True,"2010_issuance_not_secondary_listing_or_buyable_bar":True,"same_issuer_later_other_code_not_automatically_merged":True,"002525_2010Q4_code_and_paused_listing_label_supported":True,"002525_2011_native_revocation_text_not_read":True,"all_19_failed_or_never_listed_certified":False,"other_17_source_histories_adjudicated":False,"Unknown_vendor_status_meaning_unconfirmed":True,"no_raw_listing_date_or_current_metadata_repair":True},"first_scan_failure":{"actual_returncode":1,"page_cap":150,"gazette_pages":160,"cap_not_relaxed":True,"second_scan_originals_reused_no_GETs":True},"ETF2018_independent_preflight":{"actual_new_GET_attempts":3,"saved_source_documents":0,"original2018_notice_recovered":False,"home_HTTP200_assertion_failure_unclassified":True,"dated_index_HTTPError_attempts":2,"HTTP_error_status_not_captured_not_inferred":True,"old_failed_PDF_and_zero_catalogue_results_not_retried":True,"unconfirmed_index_URI_patterns_not_observed_original_links":True},"decision":{"new_price_data_or_identity_lifetime_repaired":False,"source_classes_activated":0,"source_IDs_removed":0,"new_economic_runs":0,"all_A_PIT_certified":False,"model_admitted":False,"full_goal_complete":False}}
def main():
 s=read(P);f=rebuild(s)
 if "--verify" in sys.argv:
  r=read(O);assert r["findings"]==f and r["protocol_sha256"]==sha(P) and r["implementation_sha256"]==sha(__file__);print(json.dumps({"native_rebuild":True,"unknown_records":19,"known_nonlisting_source_counterexample":1,"GETs_in_verify":0,"no_unknown_deleted":True,"model_admitted":False}));return
 assert "--build" in sys.argv and read(O)["state"]=="not_run"
 r={"schema":"regis.stock_unknown_identity_result.v1","state":"actual_source_specific_unknown_identity_diagnostic_not_lifetime_repair","observed_utc":datetime.now(timezone.utc).isoformat(),"protocol_sha256":sha(P),"implementation_sha256":sha(__file__),"findings":f,"new_economic_runs":0,"model_admitted":False,"full_goal_complete":False};O.write_text(json.dumps(r,ensure_ascii=False,indent=2)+"\n",encoding="utf-8",newline="\n");print(json.dumps({"state":r["state"],"unknown_records":19,"price_inventory_records_for_unknowns":f["identity_inventory"]["unknown_keys_in_existing_price_inventory"],"one_specific_failed_IPO_source_chain":True,"gazette_pagecap_failure_preserved":True,"ETF2018_original_still_missing":True,"model_admitted":False}))
if __name__=="__main__":main()
