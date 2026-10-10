"""One matched official original; never reprofile market histories."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib, json, urllib.request, argparse

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "research/GOAL_ST_OFFICIAL_MATCH_PROTOCOL_20261002.json"
RECEIPT = ROOT / "research/GOAL_ST_OFFICIAL_MATCH_RECEIPT_20261002.json"

def sha(b):
    return hashlib.sha256(b).hexdigest()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--verify",action="store_true")
    args=parser.parse_args()
    raw_protocol=PROTOCOL.read_bytes()
    p=json.loads(raw_protocol)
    script_sha=sha(Path(__file__).read_bytes())
    if script_sha != p["script_sha256"]:
        raise ValueError("collector changed after freeze")
    target=(ROOT/p["raw_path"]).resolve()
    candidate=(ROOT/p["candidate_path"]).resolve()
    if not target.is_relative_to(ROOT) or not candidate.is_relative_to(ROOT):
        raise ValueError("path escape")
    if sha(candidate.read_bytes()) != p["candidate_sha256"]:
        raise ValueError("candidate changed")
    if args.verify:
        rec=json.loads(RECEIPT.read_bytes())
        if (rec["protocol_sha256"] != sha(raw_protocol) or rec["script_sha256"] != script_sha
                or rec["official_sha256"] != sha(target.read_bytes())):
            raise ValueError("identity changed")
        print(json.dumps({"identity_verified":True,"reprofiled":False,"full_N4":False}))
        return
    if target.exists() or RECEIPT.exists():
        raise ValueError("one-time capture already exists")
    start=datetime.now(timezone.utc).isoformat()
    req=urllib.request.Request(p["official_url"],headers={"User-Agent":"Regis-Oracle-single-matched-disclosure","Accept":"application/pdf"})
    with urllib.request.urlopen(req,timeout=15) as response:
        if response.status != 200 or response.geturl() != p["official_url"]:
            raise ValueError("unexpected response or redirect")
        raw=response.read(p["max_pdf_bytes"]+1)
        headers={k:response.headers.get(k) for k in ("Date","Content-Type","Last-Modified","ETag")}
    if len(raw)>p["max_pdf_bytes"] or not raw.startswith(b"%PDF-"):
        raise ValueError("PDF byte limit or invalid")
    target.parent.mkdir(parents=True,exist_ok=True)
    with target.open("xb") as f: f.write(raw)
    matched=sha(raw)==p["candidate_sha256"]
    rec={"schema":"regis.goal.ST_official_match.v1","started_utc":start,
         "ended_utc":datetime.now(timezone.utc).isoformat(),"protocol_sha256":sha(raw_protocol),
         "script_sha256":script_sha,"official_url":p["official_url"],"http_status":200,"headers":headers,
         "official_sha256":sha(raw),"official_bytes":len(raw),"candidate_sha256":p["candidate_sha256"],
         "exact_candidate_bytes_match_official":matched,
         "existing_field_checks_extend_to_official_same_bytes":matched,
         "historical_version_proved":False,"intraday_available_at":None,
         "content_revision_proved":False,"market_API_calls":0,"raw_quote_output":0,
         "economic_run":False,"full_N4":False,"model_admitted":False,
         "mismatch_policy":"keep both originals and refuse authority promotion; no repeated retrieval"}
    with RECEIPT.open("x",encoding="utf-8",newline="\n") as f:
        json.dump(rec,f,ensure_ascii=False,indent=2);f.write("\n")
    print(json.dumps({"official_same_bytes_as_candidate":matched,"raw_uploaded":False,"full_N4":False}))

if __name__=="__main__":
    main()
