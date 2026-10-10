"""Immutable caller-supplied news evidence for forward-only research.

This module deliberately has no network client and computes no sentiment,
return, score, route, position, alert, or order.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .market_state_daily import PROJECT_ROOT, _atomic_publish, _canonical_bytes, _sha256


SH_TZ = ZoneInfo("Asia/Shanghai")
PROTOCOL_PATH = PROJECT_ROOT / "research/MARKET_NEWS_FORWARD_INPUT_PROTOCOL_V1.json"
PROTOCOL_SHA256 = "221a9d2cd8300155906187a5ea7429046ee8a05212bf3d06cdb5378c5d65f0a4"
PACKET_DIR = PROJECT_ROOT / "research/data/market_news_forward/v1/input_packets"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _clock(value: str) -> time:
    return time.fromisoformat(value)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(SH_TZ)


def _authority() -> dict[str, bool]:
    return {
        "research_only": True,
        "changes_live_score": False,
        "changes_live_trade_count": False,
        "changes_live_position_or_quantity": False,
        "generates_buy_or_sell_alert": False,
        "production_strategy": False,
        "automatic_trading": False,
    }


def capture_slot(
    now: datetime | None = None, *, protocol_path: Path = PROTOCOL_PATH,
) -> str | None:
    observed = (now or datetime.now(SH_TZ)).astimezone(SH_TZ)
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    current = observed.timetz().replace(tzinfo=None)
    for slot in (protocol.get("schedule") or {}).get("slots") or []:
        if _clock(slot["capture_from"]) <= current <= _clock(slot["capture_until"]):
            return str(slot["id"])
    return None


def _copy_json_item(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    try:
        return json.loads(
            json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        )
    except (TypeError, ValueError):
        return None


def _item_error(errors: list[str], index: int, reason: str) -> None:
    errors.append(f"item[{index}].{reason}")


def normalize_news_items(
    raw_items: object, *, decision_at: datetime, capture_slot_id: str,
    protocol: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate and freeze input facts without deriving any news feature."""
    if not isinstance(raw_items, list):
        return [], ["items_not_list"]

    items: list[dict[str, Any]] = []
    errors: list[str] = []
    for value in raw_items:
        copied = _copy_json_item(value)
        if copied is None:
            copied = {"_capture_input_error": "not_json_object"}
        items.append(copied)
    items.sort(key=lambda item: (
        str(item.get("source_id") or ""),
        str(item.get("stable_content_id") or ""),
        str(item.get("revision_id") or ""),
        str(item.get("canonical_url") or ""),
    ))
    if not items:
        errors.append("items_empty")

    source_contract = protocol.get("source_contract") or {}
    required = tuple(source_contract.get("required_item_fields") or ())
    allowed_precision = set(source_contract.get("allowed_publication_precision") or ())
    maximum_fetch_age = int(source_contract.get("maximum_fetch_age_seconds") or 0)
    expected_timezone = str(source_contract.get("timezone") or "")
    allowed_schemes = set(
        (protocol.get("identity_and_hash_contract") or {}).get(
            "canonical_url_schemes"
        ) or ()
    )
    stable_identities: set[tuple[str, str, str]] = set()
    url_identities: set[tuple[str, str, str]] = set()

    for index, item in enumerate(items):
        if item.get("_capture_input_error") == "not_json_object":
            _item_error(errors, index, "not_json_object")
        for key in required:
            if key not in item:
                _item_error(errors, index, f"{key}_missing")
        for key in (
            "source_id", "stable_content_id", "canonical_url", "published_at",
            "publication_precision", "first_seen_at", "fetched_at", "content",
            "content_sha256", "timezone", "revision_id", "capture_slot",
            "decision_at",
        ):
            if not isinstance(item.get(key), str) or not str(item.get(key)).strip():
                _item_error(errors, index, f"{key}_invalid")

        source_id = str(item.get("source_id") or "")
        stable_content_id = str(item.get("stable_content_id") or "")
        revision_id = str(item.get("revision_id") or "")
        canonical_url = str(item.get("canonical_url") or "")
        stable_identity = (source_id, stable_content_id, revision_id)
        url_identity = (source_id, canonical_url, revision_id)
        if all(stable_identity):
            if stable_identity in stable_identities:
                _item_error(errors, index, "duplicate_stable_identity")
            stable_identities.add(stable_identity)
        if all(url_identity):
            if url_identity in url_identities:
                _item_error(errors, index, "duplicate_canonical_url_revision")
            url_identities.add(url_identity)

        parsed_url = urlsplit(canonical_url)
        if (
            parsed_url.scheme not in allowed_schemes or not parsed_url.hostname
            or parsed_url.username is not None or parsed_url.password is not None
            or parsed_url.fragment
        ):
            _item_error(errors, index, "canonical_url_invalid")

        timezone_name = str(item.get("timezone") or "")
        try:
            valid_timezone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            valid_timezone = None
        if valid_timezone is None or timezone_name != expected_timezone:
            _item_error(errors, index, "timezone_invalid")

        precision = str(item.get("publication_precision") or "")
        published = _timestamp(item.get("published_at"))
        first_seen = _timestamp(item.get("first_seen_at"))
        fetched = _timestamp(item.get("fetched_at"))
        item_decision = _timestamp(item.get("decision_at"))
        for key, parsed in (
            ("published_at", published), ("first_seen_at", first_seen),
            ("fetched_at", fetched), ("decision_at", item_decision),
        ):
            if parsed is None:
                _item_error(errors, index, f"{key}_timestamp_invalid")
        if precision not in allowed_precision:
            _item_error(errors, index, "publication_precision_invalid")
        elif published is not None:
            if precision == "minute" and (published.second or published.microsecond):
                _item_error(errors, index, "publication_precision_mismatch")
            elif precision == "date" and (
                published.hour or published.minute or published.second
                or published.microsecond
            ):
                _item_error(errors, index, "publication_precision_mismatch")
        if published and first_seen and published > first_seen:
            _item_error(errors, index, "published_after_first_seen")
        if first_seen and fetched and first_seen > fetched:
            _item_error(errors, index, "first_seen_after_fetched")
        if (
            first_seen
            and first_seen.date().isoformat() < str(protocol.get("forward_start") or "")
        ):
            _item_error(errors, index, "first_seen_before_forward_start")
        for key, parsed in (
            ("published_at", published), ("first_seen_at", first_seen),
            ("fetched_at", fetched),
        ):
            if parsed and parsed > decision_at:
                _item_error(errors, index, f"{key}_after_decision")
        if fetched and maximum_fetch_age > 0:
            age = (decision_at - fetched).total_seconds()
            if age > maximum_fetch_age:
                _item_error(errors, index, "fetched_stale")
        if item_decision != decision_at:
            _item_error(errors, index, "decision_at_mismatch")
        if item.get("capture_slot") != capture_slot_id:
            _item_error(errors, index, "capture_slot_mismatch")
        if item.get("no_backfill") is not True:
            _item_error(errors, index, "no_backfill_invalid")

        supersedes = item.get("supersedes_id")
        if supersedes is not None and not isinstance(supersedes, str):
            _item_error(errors, index, "supersedes_id_invalid")
        elif supersedes and supersedes == revision_id:
            _item_error(errors, index, "revision_self_supersedes")

        content = item.get("content")
        content_sha = item.get("content_sha256")
        if not isinstance(content_sha, str) or not _SHA256_RE.fullmatch(content_sha):
            _item_error(errors, index, "content_sha256_invalid")
        elif isinstance(content, str) and hashlib.sha256(
            content.encode("utf-8")
        ).hexdigest() != content_sha:
            _item_error(errors, index, "content_sha256_mismatch")

        has_raw_content = "raw_content" in item
        has_raw_sha = "raw_sha256" in item
        if has_raw_content != has_raw_sha:
            _item_error(errors, index, "raw_hash_pair_incomplete")
        elif has_raw_content:
            raw_content, raw_sha = item.get("raw_content"), item.get("raw_sha256")
            if not isinstance(raw_content, str):
                _item_error(errors, index, "raw_content_invalid")
            if not isinstance(raw_sha, str) or not _SHA256_RE.fullmatch(raw_sha):
                _item_error(errors, index, "raw_sha256_invalid")
            elif isinstance(raw_content, str) and hashlib.sha256(
                raw_content.encode("utf-8")
            ).hexdigest() != raw_sha:
                _item_error(errors, index, "raw_sha256_mismatch")

    return items, list(dict.fromkeys(errors))


def verify_market_news_packet(
    path: Path, *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        resolved = path.resolve()
        resolved.relative_to(project_root.resolve())
        packet = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"packet_unreadable:{exc}"]}
    if not isinstance(packet, dict):
        return {"valid": False, "errors": ["packet_not_object"]}
    if packet.get("schema") != "market_news_forward_input.v1":
        errors.append("schema_invalid")
    identity = dict(packet)
    packet_id = str(identity.pop("packet_id", ""))
    if hashlib.sha256(_canonical_bytes(identity)).hexdigest() != packet_id:
        errors.append("packet_id_invalid")
    if packet.get("no_backfill") is not True:
        errors.append("no_backfill_invalid")

    protocol_ref = packet.get("protocol") or {}
    raw_protocol_path = Path(str(protocol_ref.get("path") or ""))
    if raw_protocol_path.is_absolute():
        protocol_path = raw_protocol_path
        errors.append("protocol_path_invalid")
    else:
        protocol_path = (project_root / raw_protocol_path).resolve()
        try:
            protocol_path.relative_to(project_root.resolve())
        except ValueError:
            errors.append("protocol_path_invalid")
    try:
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        protocol = {}
        errors.append("protocol_unreadable")
    if not protocol_path.is_file() or _sha256(protocol_path) != protocol_ref.get("sha256"):
        errors.append("protocol_identity_invalid")

    decision_at = _timestamp(packet.get("decision_at"))
    slot_id = str(packet.get("capture_slot") or "")
    if decision_at is None:
        errors.append("decision_at_invalid")
    else:
        if packet.get("decision_day") != decision_at.date().isoformat():
            errors.append("decision_day_mismatch")
        if packet.get("captured_at") != packet.get("decision_at"):
            errors.append("captured_at_mismatch")
        if protocol and capture_slot(decision_at, protocol_path=protocol_path) != slot_id:
            errors.append("capture_slot_invalid")
        if decision_at.date().isoformat() < str(protocol.get("forward_start") or ""):
            errors.append("before_forward_start")

    items = packet.get("items")
    if not isinstance(items, list):
        items = []
        errors.append("items_invalid")
    if hashlib.sha256(_canonical_bytes(items)).hexdigest() != packet.get("items_sha256"):
        errors.append("items_sha256_invalid")
    if packet.get("item_count") != len(items):
        errors.append("item_count_invalid")
    if decision_at is not None and protocol:
        normalized, source_errors = normalize_news_items(
            items, decision_at=decision_at, capture_slot_id=slot_id,
            protocol=protocol,
        )
        if normalized != items:
            errors.append("items_not_canonical")
    else:
        source_errors = ["packet_context_invalid"]
    if packet.get("source_capture_errors") != source_errors:
        errors.append("source_capture_errors_invalid")
    expected_status = "complete" if not source_errors else "incomplete"
    if packet.get("source_capture_status") != expected_status:
        errors.append("source_capture_status_invalid")
    if packet.get("returns_or_labels_read") is not False:
        errors.append("returns_or_labels_read_invalid")
    if packet.get("emotion_score_computed") is not False:
        errors.append("emotion_score_computed_invalid")
    if packet.get("eligible_for_market_emotion") is not False:
        errors.append("market_emotion_authority_invalid")
    if packet.get("authority") != _authority():
        errors.append("authority_invalid")
    return {
        "valid": not errors,
        "errors": list(dict.fromkeys(errors)),
        "packet": packet,
        "packet_id": packet_id,
        "source_complete": not source_errors,
        "source_errors": source_errors,
        "eligible_for_market_emotion": False,
    }


def capture_market_news_input(
    raw_items: object, *, now: datetime | None = None,
    project_root: Path = PROJECT_ROOT, protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str | None = PROTOCOL_SHA256,
    packet_dir: Path = PACKET_DIR,
) -> dict[str, Any]:
    """Freeze one caller-provided slot; never fetch, refresh, or score news."""
    observed = (now or datetime.now(SH_TZ)).astimezone(SH_TZ).replace(microsecond=0)
    try:
        protocol_sha = _sha256(protocol_path)
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        protocol_relative = str(protocol_path.resolve().relative_to(project_root.resolve()))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"status": "protocol_unreadable", "captured": False}
    if expected_protocol_sha256 and protocol_sha != expected_protocol_sha256:
        return {"status": "identity_invalid", "captured": False}
    if observed.date().isoformat() < str(protocol.get("forward_start") or ""):
        return {
            "status": "waiting_forward_start", "captured": False,
            "forward_start": protocol.get("forward_start"),
        }
    slot_id = capture_slot(observed, protocol_path=protocol_path)
    if slot_id is None:
        return {"status": "outside_capture_slot", "captured": False}
    items, source_errors = normalize_news_items(
        raw_items, decision_at=observed, capture_slot_id=slot_id,
        protocol=protocol,
    )
    packet = {
        "schema": "market_news_forward_input.v1",
        "decision_day": observed.date().isoformat(),
        "capture_slot": slot_id,
        "decision_at": observed.isoformat(timespec="seconds"),
        "captured_at": observed.isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "no_backfill": True,
        "protocol": {"path": protocol_relative, "sha256": protocol_sha},
        "items": items,
        "items_sha256": hashlib.sha256(_canonical_bytes(items)).hexdigest(),
        "item_count": len(items),
        "source_capture_status": "complete" if not source_errors else "incomplete",
        "source_capture_errors": source_errors,
        "returns_or_labels_read": False,
        "emotion_score_computed": False,
        "eligible_for_market_emotion": False,
        "authority": _authority(),
    }
    packet["packet_id"] = hashlib.sha256(_canonical_bytes(packet)).hexdigest()
    path = packet_dir / f"{packet['decision_day']}-{slot_id}.json"
    if path.exists():
        verified = verify_market_news_packet(path, project_root=project_root)
        if verified.get("valid") is not True:
            return {
                "status": "existing_packet_invalid", "captured": False,
                "path": str(path), "errors": verified.get("errors") or [],
            }
        existing = verified["packet"]
        return {
            "status": "already_captured", "captured": True,
            "path": str(path), "packet_id": existing.get("packet_id"),
            "source_capture_status": existing.get("source_capture_status"),
            "source_capture_errors": existing.get("source_capture_errors") or [],
            "eligible_for_market_emotion": False,
        }
    _atomic_publish(path, _canonical_bytes(packet))
    verified = verify_market_news_packet(path, project_root=project_root)
    if verified.get("valid") is not True:
        raise RuntimeError("market news packet failed post-write verification")
    return {
        "status": "captured", "captured": True, "path": str(path),
        "packet_id": packet["packet_id"],
        "source_capture_status": packet["source_capture_status"],
        "source_capture_errors": packet["source_capture_errors"],
        "eligible_for_market_emotion": False,
    }


__all__ = [
    "PACKET_DIR", "PROTOCOL_PATH", "PROTOCOL_SHA256", "capture_market_news_input",
    "capture_slot", "normalize_news_items", "verify_market_news_packet",
]
