"""Capture a bounded missing tail, retaining every provider attempt unchanged."""
from datetime import datetime, timezone
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib

from .cloud_daily_gap_probe import FIELDS, SHANGHAI
from .cloud_pattern_inputs import provider_deadline, SDK_PAGE_SIZE, sha
from .cloud_research_worker import read_json
from .market_state_daily import _atomic_publish, _canonical_bytes
from pathlib import Path

SCHEMA = "cloud_daily_tail_probe.v1"
IMPLEMENTATION_SHA256 = sha(Path(__file__))
MAX_SESSIONS = 512  # Below one SDK page; longer gaps require separate source review.
_BATCH_SESSION = ContextVar("daily_tail_batch_session", default=None)


def _close_batch(session):
    client = session["client"]
    session["client"] = None
    if client is None:
        return
    session["logouts"] += 1
    try:
        with provider_deadline() as guard:
            result = client.logout()
        session["logout_status"] = ("timed_out" if guard["expired"] else
            "provider_error" if getattr(result, "error_code", "0") != "0" else "complete")
    except Exception as exc:
        session["logout_status"] = type(exc).__name__


@contextmanager
def reuse_provider_session():
    """One lazy, bounded connection for a worker batch, not a global daemon.

    Keep original per-security receipts and deadlines. Bad responses close the
    connection before the next security; context exit closes it even on errors.
    This does not alter the publisher/proposal or invalidate saved epoch items.
    """
    if _BATCH_SESSION.get() is not None:
        raise ValueError("nested_tail_batch_session")
    session = {"client": None, "logins": 0, "logouts": 0, "queries": 0, "logout_status": "not_opened"}
    token = _BATCH_SESSION.set(session)
    try:
        yield session
    finally:
        _BATCH_SESSION.reset(token)
        _close_batch(session)


def validate(packet, request, days, now):
    body = {k: v for k, v in packet.items() if k != "packet_id"}
    started = datetime.fromisoformat(packet["started_at"])
    completed = datetime.fromisoformat(packet["completed_at"])
    basis_time = datetime.fromisoformat(request["basis_observed_at"])
    page = packet.get("pagination") or {}
    rows = packet.get("rows") or []
    if (packet.get("schema") != SCHEMA or packet.get("request") != request
            or packet.get("packet_id") != hashlib.sha256(_canonical_bytes(body)).hexdigest()
            or started.tzinfo is None or completed.tzinfo is None or now.tzinfo is None
            or basis_time.tzinfo is None or basis_time > started
            or not datetime.fromisoformat(days[-1]+"T18:00:00+08:00") <= started <= completed <= now
            or packet.get("provider") != "BaoStock" or packet.get("endpoint") != "query_history_k_data_plus"
            or packet.get("fields") != FIELDS.split(",") or packet.get("error_code") != "0"
            or packet.get("deadline_expired") is not False
            or packet.get("outcome") != "complete_tail_not_admitted"
            or any(type(page.get(k)) is not int or page[k] != len(days) for k in ("page_rows", "cursor"))
            or str(page.get("page_number")) != "1"
            or len(rows) != len(days) or any(len(row) != len(FIELDS.split(",")) for row in rows)
            or [row[0] for row in rows] != days or any(row[1] != request["code"] for row in rows)):
        raise ValueError("revision_tail_receipt_invalid")
    return [dict(zip(packet["fields"], row)) for row in rows]


def capture(root, *, code, days, basis, basis_observed_at, raw, calendar_sha, clock=None, client=None, reuse_complete=True):
    clock = clock or (lambda: datetime.now(timezone.utc))
    if not 1 <= len(days) <= MAX_SESSIONS or days != sorted(set(days)):
        raise ValueError("revision_tail_range_requires_review")
    request = {"code": code, "start_date": days[0], "end_date": days[-1], "frequency": "d", "adjustflag": "3",
               "requested_fields": FIELDS, "basis_receipt": basis, "basis_observed_at": basis_observed_at,
               "raw": raw, "calendar_sha256": calendar_sha,
               "implementation_sha256": IMPLEMENTATION_SHA256}
    identity = hashlib.sha256(_canonical_bytes(request)).hexdigest()
    directory = root / "reports/cloud_daily_tail_probe" / days[-1]
    directory.mkdir(parents=True, exist_ok=True)
    stamp = clock().astimezone(SHANGHAI)
    path = directory / (identity+"."+stamp.strftime("%Y%m%d")+f".{stamp.hour*2+stamp.minute//30:02}.json")
    for previous in sorted(directory.glob(identity+".*.json")) if reuse_complete else []:
        if previous.is_symlink():
            raise ValueError("revision_tail_receipt_invalid")
        packet = read_json(previous)
        if packet.get("outcome") == "complete_tail_not_admitted":
            validate(packet, request, days, clock())
            path = previous
            break
    if path.is_symlink():
        raise ValueError("revision_tail_receipt_invalid")
    if not path.exists():
        packet = {"schema": SCHEMA, "request": request, "provider": "BaoStock",
                  "endpoint": "query_history_k_data_plus", "started_at": clock().isoformat(),
                  "outcome": "source_unavailable", "rows": [], "fields": []}
        logged_in, guard = False, {"expired": False}
        batch = _BATCH_SESSION.get()
        try:
            if client is None:
                import baostock as client
            from baostock.common.contants import BAOSTOCK_PER_PAGE_COUNT
            if BAOSTOCK_PER_PAGE_COUNT != SDK_PAGE_SIZE or MAX_SESSIONS >= SDK_PAGE_SIZE:
                raise ValueError("provider_page_contract_changed")
            with provider_deadline() as guard:
                if batch is not None and batch["client"] is not None:
                    if batch["client"] is not client:
                        raise ValueError("tail_batch_client_changed")
                    logged_in = True
                    packet["session_reused"] = True
                else:
                    if batch is not None:
                        batch["logins"] += 1
                    logged_in = client.login().error_code == "0" and not guard["expired"]
                    if batch is not None and logged_in:
                        batch["client"] = client
                    packet["session_reused"] = False
                if not logged_in:
                    raise ValueError("provider_login_failed")
                if batch is not None:
                    batch["queries"] += 1
                result = client.query_history_k_data_plus(code, FIELDS, start_date=days[0], end_date=days[-1], frequency="d", adjustflag="3")
                packet["fields"] = list(result.fields or [])
                while not guard["expired"] and result.error_code == "0" and result.next():
                    packet["rows"].append(result.get_row_data())
                    if len(packet["rows"]) > len(days):
                        raise ValueError("provider_unexpected_rows")
                packet.update(error_code=str(result.error_code), pagination={
                    "page_rows": len(result.data), "cursor": result.cur_row_num, "page_number": result.cur_page_num},
                    outcome="complete_tail_not_admitted")
        except Exception as exc:
            packet.update(outcome="source_unavailable", error_code=type(exc).__name__)
        finally:
            packet.update(deadline_expired=guard["expired"], completed_at=clock().isoformat())
            if logged_in and batch is not None:
                packet["logout_status"] = "batch_managed"
            elif logged_in:
                try:
                    with provider_deadline() as logout_guard:
                        client.logout()
                    packet["logout_status"] = "timed_out" if logout_guard["expired"] else "complete"
                except Exception as exc:
                    packet["logout_status"] = type(exc).__name__
        try:
            packet["packet_id"] = hashlib.sha256(_canonical_bytes(packet)).hexdigest()
            validate(packet, request, days, clock())
        except (ValueError, KeyError, TypeError):
            packet["outcome"] = "incomplete_or_invalid_tail"
            if batch is not None:
                _close_batch(batch)
                packet["logout_status"] = "batch_reset_after_invalid_response"
            packet.pop("packet_id", None)
            packet["packet_id"] = hashlib.sha256(_canonical_bytes(packet)).hexdigest()
        _atomic_publish(path, _canonical_bytes(packet))
    packet = read_json(path)
    rows = validate(packet, request, days, clock())
    return rows, {"path": str(path.relative_to(root)), "sha256": sha(path)}, packet["completed_at"]
