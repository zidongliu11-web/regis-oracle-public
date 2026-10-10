"""SZSE fallback with original query pages and HTTPS disclosure documents.

This repairs historical rule inputs only. It does not confer strategy, forward
or trade authority. SSE document access has not been verified for this adapter.
"""
from datetime import date, datetime, timedelta, timezone
import json
import re
import time

import requests

from . import cloud_price_limit_events as events

ENDPOINT = "https://www.szse.cn/api/disc/announcement/annList"
DOCUMENT_HOST = "https://disc.static.szse.cn"


def query_args(code, target, page):
    if not re.fullmatch(r"sz\.\d{6}", code):
        raise ValueError("disclosure_exchange_security_invalid")
    end = date.fromisoformat(events.day(target))
    return {"seDate": [(end-timedelta(days=45)).isoformat(), (end-timedelta(days=1)).isoformat()],
            "stock": [code[3:]], "channelCode": ["listedNotice_disc"], "pageSize": 30, "pageNum": page}


def page_rows(payload, page):
    total, rows = payload.get("announceCount"), payload.get("data")
    if (type(total) is not int or not 0 <= total <= 150 or not isinstance(rows, list)
            or len(rows) != min(30, max(0, total-30*(page-1)))):
        raise ValueError("disclosure_query_page_incomplete")
    return total, rows


def candidates(payloads, code, target):
    if not 1 <= len(payloads) <= 5:
        raise ValueError("disclosure_query_page_limit")
    query = query_args(code, target, 1)
    total, seen, paths, selected, count = None, set(), set(), [], 0
    for number, payload in enumerate(payloads, 1):
        declared, rows = page_rows(payload, number)
        if total is not None and declared != total:
            raise ValueError("disclosure_query_total_changed")
        total, count = declared, count+len(rows)
        for row in rows:
            key = row.get("id")
            if row.get("secCode") != [code[3:]] or not isinstance(key, str) or not key or key in seen:
                raise ValueError("disclosure_query_identity_or_duplicate")
            seen.add(key)
            published = datetime.strptime(row["publishTime"], "%Y-%m-%d %H:%M:%S").date().isoformat()
            if not query["seDate"][0] <= published < target:
                raise ValueError("disclosure_publication_outside_pre_event_window")
            title = re.sub(r"<[^>]*>", "", row["title"]) if isinstance(row.get("title"), str) else ""
            if not title:
                raise ValueError("disclosure_title_missing")
            if not any(word in title for word in ("退市", "终止上市")):
                continue
            if any(word in title for word in ("更正", "撤销", "补充", "取消")):
                raise ValueError("disclosure_correction_requires_review")
            path = row.get("attachPath")
            prefix = r"/disc/disk\d{2}/finalpage/" + published + "/"
            if (row.get("attachFormat", "").upper() != "PDF" or not isinstance(path, str)
                    or not re.fullmatch(prefix + r"[A-Za-z0-9_-]{1,100}\.[Pp][Dd][Ff]", path) or path in paths):
                raise ValueError("disclosure_url_or_publication_mismatch")
            paths.add(path)
            selected.append({"url": DOCUMENT_HOST+path, "published": published})
    if count != total or len(payloads) != max(1, (total+29)//30):
        raise ValueError("disclosure_query_not_complete")
    if len(selected) > 12:
        raise ValueError("disclosure_document_limit_requires_review")
    return selected


def verify(root, record):
    code, target = record["code"], events.day(record["date"])
    if record.get("disclosure_source") != "SZSE" or record.get("query_endpoint") != ENDPOINT:
        raise ValueError("disclosure_exchange_identity_invalid")
    if record.get("public_query_transport") != "https":
        raise ValueError("disclosure_exchange_requires_https")
    pages = [json.loads(events._bound(root, reference)) for reference in record["queries"]]
    selected = candidates(pages, code, target)
    if set(record["documents"]) != {row["url"] for row in selected}:
        raise ValueError("disclosure_document_coverage_mismatch")
    found = set()
    for row in selected:
        text = events.pdf_text(events._bound(root, record["documents"][row["url"]]))
        event = events.event_from_text(text, code, target)
        if event:
            if not events.printed_date(text) <= row["published"] < target:
                raise ValueError("disclosure_printed_date_not_before_event")
            found.add(event)
    if found != {"delisting_first_day"}:
        raise ValueError("no_supported_pre_event_disclosure")
    return code, target, "delisting_first_day"


def collect(root, code, target):
    if date.fromisoformat(events.day(target)) >= datetime.now(events.TZ).date():
        raise ValueError("only_closed_historical_events")
    deadline = time.monotonic()+120
    with requests.Session() as session:
        session.trust_env = False
        def fetch(url, *, limit=2*1024**2, **kwargs):
            if time.monotonic() >= deadline:
                raise TimeoutError("disclosure_event_deadline")
            with session.request("POST" if "json" in kwargs else "GET", url,
                    headers={**events.HEADERS, "Referer": "https://www.szse.cn/"},
                    timeout=(10, 12), stream=True, allow_redirects=False, **kwargs) as response:
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
        pages, refs = [], []
        for number in range(1, 6):
            raw = fetch(ENDPOINT, json=query_args(code, target, number))
            payload = json.loads(raw)
            pages.append(payload)
            refs.append(events._archive(root, raw, ".json"))
            total, _ = page_rows(payload, number)
            if number*30 >= total:
                break
        selected = candidates(pages, code, target)
        documents = {row["url"]: events._archive(root, fetch(row["url"], limit=8*1024**2), ".pdf") for row in selected}
        record = {"schema": events.SCHEMA, "code": code, "date": target, "disclosure_source": "SZSE",
                  "query_endpoint": ENDPOINT, "queries": refs, "documents": documents,
                  "observed_at": datetime.now(timezone.utc).isoformat(), "public_query_transport": "https",
                  "boundary": "historical_rule_verification;not_contemporaneous_capture;not_strategy_admission"}
        events._verify(root, record)
        return events._archive(root, events.encoded(record), ".json")
