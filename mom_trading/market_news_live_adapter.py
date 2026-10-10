"""Honest adapter from the existing display-news snapshot to forward PIT input."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .market_news_forward import (
    PACKET_DIR,
    PROTOCOL_PATH,
    PROTOCOL_SHA256,
    SH_TZ,
    capture_market_news_input,
    capture_slot,
)
from .market_state_daily import PROJECT_ROOT


def adapt_news_live_snapshot(
    snapshot: object, *, now: datetime, capture_slot_id: str,
) -> list[dict[str, Any]]:
    """Map only fields the current ``news_live`` payload actually proves.

    The live payload does not retain first-seen time, original publication
    precision, or revision lineage. Those required fields stay absent so the
    forward collector records an incomplete packet instead of inventing them.
    """
    observed = now.astimezone(SH_TZ).replace(microsecond=0)
    payload = snapshot if isinstance(snapshot, dict) else {}
    fetched_at = payload.get("fetched_at")
    live_items = payload.get("items")
    if not isinstance(live_items, list):
        return []

    adapted: list[dict[str, Any]] = []
    for value in live_items:
        item = value if isinstance(value, dict) else {}
        try:
            content = json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            content = ""
        row: dict[str, Any] = {
            "content": content,
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "content_basis": "canonical_json_of_news_live_item_not_raw_source_bytes",
            "timezone": "Asia/Shanghai",
            "capture_slot": capture_slot_id,
            "decision_at": observed.isoformat(timespec="seconds"),
            "no_backfill": True,
        }
        exact_fields = {
            "source_id": item.get("source"),
            "stable_content_id": item.get("dedupe_key") or item.get("id"),
            "canonical_url": item.get("url"),
            "published_at": item.get("time"),
            "fetched_at": fetched_at,
        }
        row.update({key: value for key, value in exact_fields.items() if value is not None})
        if row.get("stable_content_id"):
            row["stable_content_id_basis"] = "news_live_local_title_dedupe_key"
        adapted.append(row)
    return adapted


def capture_news_live_snapshot(
    snapshot: object, *, now: datetime | None = None,
    project_root: Path = PROJECT_ROOT, protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str | None = PROTOCOL_SHA256,
    packet_dir: Path = PACKET_DIR,
) -> dict[str, Any]:
    """Freeze a caller-supplied live snapshot; this function never fetches it."""
    observed = (now or datetime.now(SH_TZ)).astimezone(SH_TZ).replace(microsecond=0)
    try:
        slot_id = capture_slot(observed, protocol_path=protocol_path)
    except (OSError, ValueError, json.JSONDecodeError, KeyError):
        slot_id = ""
    items = adapt_news_live_snapshot(
        snapshot, now=observed, capture_slot_id=slot_id,
    )
    return capture_market_news_input(
        items, now=observed, project_root=project_root,
        protocol_path=protocol_path,
        expected_protocol_sha256=expected_protocol_sha256,
        packet_dir=packet_dir,
    )


__all__ = ["adapt_news_live_snapshot", "capture_news_live_snapshot"]
