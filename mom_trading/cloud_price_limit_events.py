"""Recover historical rule evidence from CNINFO, never from laptop result tables.

Only dated, code-matched pre-event disclosures can explain a no-limit first
day. Receipts are immutable; the index is additive. This is historical rule
verification, not contemporaneous capture, strategy evidence or a buy signal.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re
import time
import unicodedata
from zoneinfo import ZoneInfo

import requests

from .cloud_research_worker import ROOT, publish, read_json

SCHEMA = "cloud_price_limit_events.v1"
DIRECTORY = Path("reports/cloud_price_limit_events")
IMPLEMENTATION_SHA256 = hashlib.sha256(Path(__file__).read_bytes()
    + Path(__file__).with_name("cloud_szse_price_events.py").read_bytes()).hexdigest()
TZ = ZoneInfo("Asia/Shanghai")
HEADERS = {"User-Agent": "A-share-manual-decision-support/1.0", "Referer": "https://www.cninfo.com.cn/"}
DATE = r"(20\d{2})年(\d{1,2})月(\d{1,2})日"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()


def day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("event_date_invalid")
    return value


def event_from_text(text, code, target):
    """Conservative grammar: explicit first date plus explicit no-limit clause."""
    day(target)
    if not re.fullmatch(r"(?:sh|sz)\.\d{6}", code):
        raise ValueError("event_code_invalid")
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    if not re.search(r"证券代码[:：]" + code.split(".")[1] + r"(?!\d)", text[:1500]):
        raise ValueError("disclosure_security_identity_mismatch")
    dates = set()
    for pattern in (
        r"进入退市整理期(?:交易)?的(?:起始日|起始日期)(?:为|是|:|：)?" + DATE,
        r"(?:于|自)" + DATE + r"(?:起)?进入退市整理期",
        r"退市整理期(?:交易)?(?:起始日|起始日期)(?:为|是|:|：)?" + DATE,
    ):
        for y, m, d in re.findall(pattern, text):
            dates.add(date(int(y), int(m), int(d)).isoformat())
    no_limit = re.search(r"(?:首个|第一个|第一|首)(?:交易)?日(?:无|不设|不设置|不实行)(?:价格)?涨跌幅限制", text)
    if dates and dates != {target}:
        raise ValueError("disclosure_first_day_conflicts_with_audit")
    return "delisting_first_day" if dates == {target} and no_limit else None


def pdf_text(raw):
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    if not raw.startswith(b"%PDF") or len(raw) > 8 * 1024 ** 2:
        raise ValueError("disclosure_pdf_invalid")
    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 30:
            raise ValueError("disclosure_pdf_size_or_encryption")
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except PdfReadError as exc:
        raise ValueError("disclosure_pdf_unreadable") from exc


def printed_date(text):
    """Require a dated signature near the end when API metadata lacked TLS."""
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))[-500:]
    numerals = "0-9〇零○一二三四五六七八九"
    matches = re.findall(r"([" + numerals + r"]{4})年([" + numerals + r"十]{1,3})月([" + numerals + r"十]{1,3})日(?:[0-9]{1,2})?$", text)
    if not matches:
        raise ValueError("disclosure_printed_date_requires_review")
    y, m, d = matches[-1]
    digits = str.maketrans("〇零○一二三四五六七八九", "000123456789")
    def number(value):
        value = value.translate(digits)
        if "十" not in value:
            return int(value)
        tens, units = value.split("十")
        return int(tens or "1") * 10 + int(units or "0")
    return date(number(y), number(m), number(d)).isoformat()


def _archive(root, raw, suffix):
    relative = DIRECTORY / "objects" / (sha(raw) + suffix)
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != raw:
            raise ValueError("disclosure_archive_conflict")
    else:
        # A process interruption may leave an unreferenced object, never a pass.
        with path.open("xb") as stream:
            stream.write(raw)
    return {"path": str(relative), "sha256": sha(raw)}


def _bound(root, reference):
    relative = Path(reference["path"])
    path = root / relative
    if (relative.parent != DIRECTORY / "objects" or path.is_symlink()
            or path.resolve().parent != (root / DIRECTORY / "objects").resolve()):
        raise ValueError("disclosure_archive_path_invalid")
    raw = path.read_bytes()
    if sha(raw) != reference["sha256"]:
        raise ValueError("disclosure_archive_hash_mismatch")
    return raw


def _announcements(payloads, code):
    rows, expected, keys = [], None, set()
    if not 1 <= len(payloads) <= 5:
        raise ValueError("disclosure_query_page_limit")
    for page_number, payload in enumerate(payloads, start=1):
        total = payload.get("totalAnnouncement")
        page = payload.get("announcements")
        if total == 0 and page is None:
            page = []
        if type(total) is not int or not 0 <= total <= 150 or not isinstance(page, list):
            raise ValueError("disclosure_query_schema")
        if expected is not None and total != expected:
            raise ValueError("disclosure_query_total_changed")
        expected = total
        if len(page) != min(30, max(0, total - 30 * (page_number - 1))):
            raise ValueError("disclosure_query_page_incomplete")
        if payload.get("hasMore") is not (page_number * 30 < total):
            raise ValueError("disclosure_query_terminal_mismatch")
        for row in page:
            key = (row.get("secCode"), row.get("announcementId"))
            if key[0] != code.split(".")[1] or not key[1] or key in keys:
                raise ValueError("disclosure_query_identity_or_duplicate")
            keys.add(key)
        rows.extend(page)
    if len(rows) != expected:
        raise ValueError("disclosure_query_not_complete")
    return rows


def _candidates(rows, target):
    start = (date.fromisoformat(target) - timedelta(days=45)).isoformat()
    candidates = []
    for row in rows:
        title = re.sub(r"<[^>]*>", "", str(row.get("announcementTitle", "")))
        published = datetime.fromtimestamp(float(row["announcementTime"]) / 1000, TZ).date().isoformat()
        if not start <= published < target:
            raise ValueError("disclosure_publication_outside_pre_event_window")
        if not any(word in title for word in ("退市", "终止上市")):
            continue
        if any(word in title for word in ("更正", "撤销", "补充", "取消")):
            raise ValueError("disclosure_correction_requires_review")
        adjunct = str(row.get("adjunctUrl", ""))
        if not re.fullmatch(r"finalpage/" + published + r"/\d+\.[Pp][Dd][Ff]", adjunct):
            raise ValueError("disclosure_url_or_publication_mismatch")
        candidates.append(row)
    if len(candidates) > 12:
        raise ValueError("disclosure_document_limit_requires_review")
    return candidates


def _verify(root, record):
    code, target = record["code"], day(record["date"])
    if record.get("schema") != SCHEMA:
        raise ValueError("disclosure_receipt_schema")
    if record.get("public_query_transport", "https") not in {"https", "http_after_network_failure"}:
        raise ValueError("disclosure_transport_unknown")
    observed = datetime.fromisoformat(record["observed_at"])
    if observed.tzinfo is None or observed > datetime.now(timezone.utc) + timedelta(minutes=1):
        raise ValueError("disclosure_receipt_time_invalid")
    if record.get("disclosure_source") == "SZSE":
        from .cloud_szse_price_events import verify
        return verify(root, record)
    if record.get("disclosure_source") not in {None, "CNINFO"}:
        raise ValueError("disclosure_source_unknown")
    organizations = json.loads(_bound(root, record["organization"]))
    if len({row.get("orgId") for row in organizations if row.get("code") == code.split(".")[1]
            and row.get("category") == "A股" and row.get("orgId")}) != 1:
        raise ValueError("disclosure_org_identity_missing")
    queries = [json.loads(_bound(root, ref)) for ref in record["queries"]]
    candidates = _candidates(_announcements(queries, code), target)
    docs = record["documents"]
    if {row["adjunctUrl"] for row in candidates} != set(docs):
        raise ValueError("disclosure_document_coverage_mismatch")
    found = set()
    for row in candidates:
        text = pdf_text(_bound(root, docs[row["adjunctUrl"]]))
        event = event_from_text(text, code, target)
        if event:
            if record.get("public_query_transport") == "http_after_network_failure":
                published = datetime.fromtimestamp(float(row["announcementTime"]) / 1000, TZ).date().isoformat()
                if not printed_date(text) <= published < target:
                    raise ValueError("disclosure_printed_date_not_before_event")
            found.add(event)
    if found != {"delisting_first_day"}:
        raise ValueError("no_supported_pre_event_disclosure")
    return code, target, "delisting_first_day"


def load_events(root=ROOT, base=None, *, index_name="index.json"):
    """Revalidate recorded objects; do not trust a saved 'verified' boolean."""
    result = dict(base or {})
    if index_name not in {"index.json", "pending.json"}:
        raise ValueError("disclosure_index_name_invalid")
    path = root / DIRECTORY / index_name
    if not path.exists():
        return result, None
    if path.is_symlink():
        raise ValueError("disclosure_index_path_invalid")
    raw = path.read_bytes()
    index = json.loads(raw)
    if index.get("schema") != SCHEMA or not isinstance(index.get("items"), dict) or len(index["items"]) > 1000:
        raise ValueError("disclosure_index_schema")
    for key, reference in index["items"].items():
        code, target, kind = _verify(root, json.loads(_bound(root, reference)))
        if key != code + "@" + target or result.get((code, target), kind) != kind:
            raise ValueError("disclosure_event_conflict")
        result[(code, target)] = kind
    return result, sha(encoded([sha(raw), IMPLEMENTATION_SHA256]))


def collect(root, code, target, *, session=None):
    """One security, a closed pre-event date interval, fully checked pagination."""
    own_session = session is None
    session = session or requests.Session()
    session.trust_env = False
    deadline = time.monotonic() + 120
    transport = "https"
    def request(url, *, data=None, limit=2 * 1024 ** 2):
        nonlocal transport
        if time.monotonic() >= deadline:
            raise TimeoutError("disclosure_event_deadline")
        try:
            response = session.request("POST" if data is not None else "GET", url, data=data,
                                       headers=HEADERS, timeout=(3.05, 10), stream=True, allow_redirects=False)
        except (requests.ConnectionError, requests.Timeout) as exc:
            # Public, credential-free API only. Never downgrade document
            # downloads or retry an HTTP access-denied response this way.
            if (isinstance(exc, requests.exceptions.SSLError) or data is None
                    or not url.startswith("https://www.cninfo.com.cn/new/")):
                raise
            transport = "http_after_network_failure"
            response = session.request("POST", url.replace("https://", "http://", 1), data=data,
                                       headers=HEADERS, timeout=(3.05, 10), stream=True, allow_redirects=False)
        with response:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("disclosure_http_status")
            parts, size = [], 0
            for part in response.iter_content(65536):
                size += len(part)
                if size > limit or time.monotonic() >= deadline:
                    raise TimeoutError("disclosure_response_limit")
                parts.append(part)
            return b"".join(parts)
    try:
        if not re.fullmatch(r"(?:sh|sz)\.\d{6}", code):
            raise ValueError("event_code_invalid")
        end = date.fromisoformat(day(target))
        if end >= datetime.now(TZ).date():
            raise ValueError("only_closed_historical_events")
        basic = request("https://www.cninfo.com.cn/new/information/topSearch/query",
                        data={"keyWord": code.split(".")[1], "maxNum": "10"})
        matches = {row["orgId"] for row in json.loads(basic)
                   if row.get("code") == code.split(".")[1] and row.get("category") == "A股" and row.get("orgId")}
        if len(matches) != 1:
            raise ValueError("disclosure_org_identity_missing")
        payloads, references = [], []
        for page_number in range(1, 6):
            body = request("https://www.cninfo.com.cn/new/hisAnnouncement/query", data={
                "pageNum": str(page_number), "pageSize": "30", "column": "szse", "tabName": "fulltext",
                "stock": code.split(".")[1] + "," + next(iter(matches)), "searchkey": "", "isHLtitle": "true",
                "seDate": f"{end-timedelta(days=45)}~{end-timedelta(days=1)}"})
            payload = json.loads(body)
            payloads.append(payload)
            references.append(_archive(root, body, ".json"))
            if payload.get("hasMore") is False:
                break
        candidates = _candidates(_announcements(payloads, code), target)
        documents = {row["adjunctUrl"]: _archive(root, request("https://static.cninfo.com.cn/" + row["adjunctUrl"], limit=8 * 1024 ** 2), ".pdf")
                     for row in candidates}
        record = {"schema": SCHEMA, "code": code, "date": target,
                  "observed_at": datetime.now(timezone.utc).isoformat(), "organization": _archive(root, basic, ".json"),
                  "queries": references, "documents": documents,
                  "public_query_transport": transport,
                  "boundary": "historical_rule_verification;not_contemporaneous_capture;not_strategy_admission"}
        _verify(root, record)
        return _archive(root, encoded(record), ".json")
    except (requests.ConnectionError, requests.Timeout) as exc:
        if isinstance(exc, requests.exceptions.SSLError) or not code.startswith("sz."):
            raise
        # A separate official exchange source, not an access-denied bypass.
        # Its original pages and PDFs have their own schema and verification.
        from .cloud_szse_price_events import collect as exchange_collect
        return exchange_collect(root, code, target)
    finally:
        if own_session:
            session.close()


def run(*, root=ROOT, batch_limit=10):
    if os.environ.get("REGIS_EXECUTION_ROLE") != "cloud":
        return {"status": "local_unchanged", "summary": "不改动开发电脑的历史制度事件表。"}
    if type(batch_limit) is not int or not 1 <= batch_limit <= 20:
        raise ValueError("event_batch_limit_invalid")
    audit = read_json(root / "reports/v4_daily_quality_audit.json")
    manifest = root / "reports/v4_daily_download_manifest.json"
    bindings = audit.get("input_fingerprints") or {}
    if not manifest.is_file() or bindings.get("download_manifest_sha256") != sha(manifest.read_bytes()):
        return {"status": "waiting_inputs", "summary": "等待当前日线审计的真实制度缺口，不推测事件。"}
    violations = audit.get("unresolved_limit_violation_rows")
    if not isinstance(violations, list) or len(violations) != audit.get("ordinary_limit_violations") or len(violations) > 1000:
        return {"status": "waiting_inputs", "summary": "制度缺口明细与审计计数未匹配，保留待核验。"}
    active, _ = load_events(root)
    known, _ = load_events(root, active, index_name="pending.json")
    required = {(row["code"], day(row["date"])) for row in violations}
    targets = sorted(required - set(known))
    index_path = root / DIRECTORY / "index.json"
    pending_path = root / DIRECTORY / "pending.json"
    index = read_json(pending_path) or read_json(index_path) or {"schema": SCHEMA, "items": {}}
    if not set((read_json(index_path).get("items") or {})) <= set(index["items"]):
        raise ValueError("disclosure_pending_index_lost_previous_records")
    attempts_path = root / DIRECTORY / "attempts.json"
    attempts = read_json(attempts_path)
    # Unattempted/oldest gaps first: hourly retries plus source backoff must not
    # starve the tail of this queue. This changes order, not the required set.
    targets.sort(key=lambda item: (attempts.get(item[0]+"@"+item[1], {}).get("slot", ""), item))
    backoff_path = root / DIRECTORY / "network_backoff.json"
    backoff = read_json(backoff_path)
    now = datetime.now(timezone.utc)
    try:
        retry_at = datetime.fromisoformat(backoff["retry_at"])
        cooling = (backoff.get("implementation_sha256") == IMPLEMENTATION_SHA256
                   and now < retry_at <= now + timedelta(minutes=5))
    except (KeyError, TypeError, ValueError):
        cooling = False
    if targets and cooling:
        return {"status": "retry_pending", "captured": len(index["items"]),
                "target": len(set(known) | set(targets)), "remaining": len(targets),
                "implementation_sha256": IMPLEMENTATION_SHA256,
                "summary": "官方公告源连续连接失败，短暂退避后自动重试；保留缺口，先让情绪和价格任务继续，不授予交易资格。"}
    slot = datetime.now(TZ).strftime("%Y-%m-%dT%H") + "@" + IMPLEMENTATION_SHA256
    attempted, captured, network_failures = 0, 0, 0
    for code, target in targets:
        key = code + "@" + target
        if attempts.get(key, {}).get("slot") == slot:
            continue
        if attempted >= batch_limit:
            break
        attempted += 1
        try:
            reference = collect(root, code, target)
            index["items"][key] = reference
            publish(pending_path, index)
            captured += 1
            outcome = "verified_disclosure"
            network_failures = 0
        except (OSError, ValueError, KeyError, TypeError, requests.RequestException) as exc:
            outcome = type(exc).__name__ + ": " + str(exc)
            network_failures = (network_failures + 1 if isinstance(exc, (requests.ConnectionError, requests.Timeout, TimeoutError)) else 0)
        attempts[key] = {"slot": slot, "outcome": outcome}
        publish(attempts_path, attempts)
        if network_failures >= 2:
            publish(backoff_path, {"implementation_sha256": IMPLEMENTATION_SHA256,
                                  "retry_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()})
            break
    remaining = len(targets) - captured
    # Publish a batch only after each current gap has a receipt or an explicit
    # attempt. This avoids re-auditing millions of rows for every small batch.
    if index["items"] and all(code + "@" + target in index["items"] or
            str(attempts.get(code + "@" + target, {}).get("slot", "")).endswith("@"+IMPLEMENTATION_SHA256)
            for code, target in required):
        publish(index_path, index)
    return {"status": ("retry_pending" if network_failures >= 2 else "collecting") if remaining else "complete",
            "captured": len(index["items"]), "target": len(set(known) | set(targets)), "remaining": remaining,
            "implementation_sha256": IMPLEMENTATION_SHA256,
            "summary": f"已核验 {len(index['items'])} 条事件日前官方制度公告，当前仍有 {remaining} 条待核验；仅修正历史限价解释，不授予交易资格。"}
