"""Short-lived, process-local references for manual buy confirmation.

A ticket binds a server-selected stock code plus its immutable entry source.
It is never an authorisation decision: the confirmation route must recompute
market, cash and risk facts before writing the manual ledger.
"""
from __future__ import annotations

import copy
import secrets
import threading
import time
from datetime import datetime, timedelta


class TicketInvalid(ValueError):
    """The supplied ticket does not exist or has an invalid shape."""


class TicketExpired(ValueError):
    """The ticket existed but is past its short validity window."""


class TicketConflict(ValueError):
    """The ticket was already bound to a different idempotency key."""


class BuyTicketStore:
    def __init__(self, ttl_seconds: int = 120, max_entries: int = 2048):
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._rows: dict[str, dict] = {}

    def _prune(self, now: float) -> None:
        expired = [key for key, row in self._rows.items() if row["expires_mono"] < now]
        for key in expired:
            self._rows.pop(key, None)
        overflow = len(self._rows) - self.max_entries
        if overflow > 0:
            oldest = sorted(self._rows, key=lambda key: self._rows[key]["issued_mono"])
            for key in oldest[:overflow]:
                self._rows.pop(key, None)

    def issue(self, code: str, entry_context: dict | None = None) -> dict:
        if entry_context is not None and not isinstance(entry_context, dict):
            raise TicketInvalid("确认票据来源上下文无效")
        if entry_context is None:
            # Compatibility for internal callers that create a manual ticket
            # directly.  It remains explicitly non-strategy provenance.
            entry_context = {
                "entry_origin": "manual_ticket",
                "entry_strategy_id": None,
                "entry_evidence_ref_json": None,
                "entry_signal_id": None,
                "entry_alert_id": None,
            }
        now = time.monotonic()
        ticket_id = secrets.token_urlsafe(24)
        expires_at = (
            datetime.now().astimezone() + timedelta(seconds=self.ttl_seconds)
        ).isoformat(timespec="seconds")
        with self._lock:
            self._prune(now)
            self._rows[ticket_id] = {
                "code": str(code),
                "issued_mono": now,
                "expires_mono": now + self.ttl_seconds,
                "expires_at": expires_at,
                "idempotency_key": None,
                # The browser never receives or supplies this context.  It is
                # copied at both boundaries so later mutable objects cannot
                # rewrite what a confirmation ticket refers to.
                "entry_context": copy.deepcopy(entry_context),
            }
            self._prune(now)
        return {"id": ticket_id, "expires_at": expires_at, "ttl_seconds": self.ttl_seconds}

    def reserve(self, ticket_id: object, idempotency_key: str) -> dict:
        token = str(ticket_id or "").strip()
        if not token or len(token) > 128:
            raise TicketInvalid("确认票据无效")
        now = time.monotonic()
        with self._lock:
            row = self._rows.get(token)
            if row is None:
                self._prune(now)
                raise TicketInvalid("确认票据不存在或服务已重启")
            if row["expires_mono"] < now:
                self._rows.pop(token, None)
                raise TicketExpired("确认票据已过期，请重新分析")
            bound = row.get("idempotency_key")
            if bound and bound != idempotency_key:
                raise TicketConflict("确认票据已绑定另一笔提交")
            row["idempotency_key"] = idempotency_key
            return {
                "code": row["code"],
                "expires_at": row["expires_at"],
                "entry_context": copy.deepcopy(row.get("entry_context")),
            }


BUY_TICKETS = BuyTicketStore()
