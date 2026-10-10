"""No-backfill raw inputs for future market/sector strategy routing research."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, time
from pathlib import Path
from .research_artifact_paths import research_artifact_path
from typing import Any
from zoneinfo import ZoneInfo

from .market_state_daily import PROJECT_ROOT, _atomic_publish, _canonical_bytes, _sha256
from .market_state_forward import verify_forward_packet


SH_TZ = ZoneInfo("Asia/Shanghai")
PROTOCOL_PATH = (
    PROJECT_ROOT / "research/MARKET_SECTOR_ROUTE_FORWARD_INPUT_PROTOCOL_V1.json"
)
PROTOCOL_SHA256 = "150634c0ada3005b2f808c958d8af6d64290d18637beec16db80caa45fa954f7"
PACKET_DIR = (
    PROJECT_ROOT / "research/data/market_sector_route_forward/v1/input_packets"
)


def _clock(value: str) -> time:
    return time.fromisoformat(value)


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


def _source_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.replace(tzinfo=SH_TZ) if parsed.tzinfo is None else parsed.astimezone(SH_TZ)


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _h1_reference(
    capture: dict[str, Any], *, project_root: Path,
) -> tuple[dict[str, Any] | None, list[str]]:
    raw_path = Path(str(capture.get("path") or ""))
    path = raw_path if raw_path.is_absolute() else project_root / raw_path
    try:
        path = research_artifact_path(path, project_root=project_root)
    except ValueError:
        return None, ["h1_packet_outside_project"]
    verification = verify_forward_packet(path, project_root=project_root)
    if verification.get("valid") is not True:
        return None, ["h1_packet_invalid"]
    packet = verification["packet"]
    return {
        "path": str(path.relative_to(project_root)),
        "sha256": _sha256(path),
        "packet_id": packet.get("packet_id"),
        "decision_day": packet.get("decision_day"),
        "slot_id": packet.get("slot_id"),
        "market_state": packet.get("treatment_observation") or {},
    }, []


def _sector_rows(
    snapshot: dict[str, Any], observed: datetime, *, maximum_age_seconds: int,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    errors: list[str] = []
    meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {}
    for key in (
        "coverage_complete", "concept_complete", "industry_complete",
        "timestamp_complete", "breadth_count_complete",
    ):
        if meta.get(key) is not True:
            errors.append(f"sector_{key}_missing")
    rows: list[dict[str, Any]] = []
    for kind, key, count_key in (
        ("concept", "concepts", "concept_reported_total"),
        ("industry", "industries", "industry_reported_total"),
    ):
        raw_rows = snapshot.get(key)
        if not isinstance(raw_rows, list):
            errors.append(f"sector_{key}_missing")
            continue
        if int(meta.get(count_key) or -1) != len(raw_rows):
            errors.append(f"sector_{key}_count_mismatch")
        for raw in raw_rows:
            if not isinstance(raw, dict):
                errors.append("sector_row_not_object")
                continue
            timestamp = _source_time(raw.get("data_as_of"))
            if timestamp is None:
                errors.append("sector_row_time_missing")
            else:
                age = (observed - timestamp).total_seconds()
                if timestamp.date() != observed.date():
                    errors.append("sector_row_not_same_decision_day")
                if age < 0:
                    errors.append("sector_row_time_after_capture")
                elif age > maximum_age_seconds:
                    errors.append("sector_row_stale")
            change = _finite(raw.get("change_pct"))
            up = _finite(raw.get("up_count"))
            down = _finite(raw.get("down_count"))
            if not str(raw.get("code") or "") or not str(raw.get("name") or ""):
                errors.append("sector_row_identity_missing")
            if change is None or up is None or down is None or up < 0 or down < 0:
                errors.append("sector_row_value_invalid")
            rows.append({
                "kind": kind,
                "code": str(raw.get("code") or ""),
                "name": str(raw.get("name") or ""),
                "change_pct": change,
                "up_count": int(up) if up is not None and up >= 0 else None,
                "down_count": int(down) if down is not None and down >= 0 else None,
                "data_as_of": raw.get("data_as_of"),
            })
    rows.sort(key=lambda row: (row["kind"], row["code"], row["name"]))
    top_time_raw = (
        snapshot.get("latest_data_as_of") or snapshot.get("data_as_of")
        or snapshot.get("as_of")
    )
    top_time = _source_time(top_time_raw)
    if top_time is None:
        errors.append("sector_source_time_missing")
    else:
        top_age = (observed - top_time).total_seconds()
        if top_time.date() != observed.date():
            errors.append("sector_source_not_same_decision_day")
        if top_age < 0:
            errors.append("sector_source_time_after_capture")
        elif top_age > maximum_age_seconds:
            errors.append("sector_source_stale")
    return rows, list(dict.fromkeys(errors)), {
        "source": snapshot.get("source"),
        "source_status": snapshot.get("status"),
        "source_health_status": snapshot.get("health_status"),
        "source_data_as_of": top_time_raw,
        "source_fetched_at": snapshot.get("fetched_at"),
        "meta": meta,
        "third_party_main_net_flow_captured": False,
        "participates_in_route": False,
    }


def verify_market_sector_route_packet(
    path: Path, *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        resolved = research_artifact_path(path, project_root=project_root)
        packet = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"packet_unreadable:{exc}"]}
    if packet.get("schema") != "shadow_market_sector_route_input.v1":
        errors.append("schema_invalid")
    identity = dict(packet)
    packet_id = str(identity.pop("packet_id", ""))
    if hashlib.sha256(_canonical_bytes(identity)).hexdigest() != packet_id:
        errors.append("packet_id_invalid")
    if packet.get("no_backfill") is not True:
        errors.append("no_backfill_invalid")
    authority = packet.get("authority") or {}
    if authority.get("research_only") is not True or any(
        authority.get(key) is not False for key in (
            "changes_score", "changes_trade_count", "changes_position_or_quantity",
            "generates_alerts", "production", "automatic_trading",
        )
    ):
        errors.append("authority_invalid")
    h1 = packet.get("h1_packet") or {}
    try:
        h1_path = research_artifact_path(h1.get("path"), project_root=project_root)
    except ValueError:
        h1_path = None
    if h1_path is None or not h1_path.is_file() or _sha256(h1_path) != h1.get("sha256"):
        errors.append("h1_identity_invalid")
    else:
        verified_h1 = verify_forward_packet(h1_path, project_root=project_root)
        if verified_h1.get("valid") is not True:
            errors.append("h1_packet_invalid")
        elif (
            h1.get("packet_id") != verified_h1["packet"].get("packet_id")
            or packet.get("decision_day") != verified_h1["packet"].get("decision_day")
            or packet.get("slot_id") != verified_h1["packet"].get("slot_id")
        ):
            errors.append("h1_decision_identity_mismatch")
    rows = packet.get("sector_rows")
    if not isinstance(rows, list):
        errors.append("sector_rows_invalid")
    elif hashlib.sha256(_canonical_bytes(rows)).hexdigest() != packet.get(
        "sector_rows_sha256"
    ):
        errors.append("sector_rows_sha256_invalid")
    return {"valid": not errors, "errors": errors, "packet": packet}


def capture_market_sector_route_input(
    h1_capture: dict[str, Any], sector_snapshot: dict[str, Any] | None, *,
    h3_capture: dict[str, Any] | None = None, now: datetime | None = None,
    project_root: Path = PROJECT_ROOT, protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str | None = PROTOCOL_SHA256,
    packet_dir: Path = PACKET_DIR,
) -> dict[str, Any]:
    observed = (now or datetime.now(SH_TZ)).astimezone(SH_TZ)
    protocol_sha = _sha256(protocol_path)
    if expected_protocol_sha256 and protocol_sha != expected_protocol_sha256:
        return {"status": "identity_invalid", "captured": False}
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    if observed.date().isoformat() < str(protocol.get("forward_start")):
        return {
            "status": "waiting_forward_start", "captured": False,
            "forward_start": protocol.get("forward_start"),
        }
    slot_id = capture_slot(observed, protocol_path=protocol_path)
    if slot_id is None:
        return {"status": "outside_capture_slot", "captured": False}
    h1, h1_errors = _h1_reference(h1_capture, project_root=project_root)
    if h1 is None:
        return {
            "status": "waiting_for_verified_h1_packet", "captured": False,
            "errors": h1_errors,
        }
    decision_day = observed.date().isoformat()
    if h1.get("decision_day") != decision_day or h1.get("slot_id") != slot_id:
        return {
            "status": "h1_slot_mismatch", "captured": False,
            "expected": {"decision_day": decision_day, "slot_id": slot_id},
        }
    maximum_age = int((protocol.get("causality") or {})[
        "maximum_sector_age_seconds"
    ])
    rows, sector_errors, source = _sector_rows(
        sector_snapshot if isinstance(sector_snapshot, dict) else {},
        observed, maximum_age_seconds=maximum_age,
    )
    packet = {
        "schema": "shadow_market_sector_route_input.v1",
        "decision_day": decision_day,
        "slot_id": slot_id,
        "captured_at": observed.isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "no_backfill": True,
        "protocol": {
            "path": str(protocol_path.relative_to(project_root)),
            "sha256": protocol_sha,
        },
        "h1_packet": h1,
        "sector_source": source,
        "sector_rows": rows,
        "sector_rows_sha256": hashlib.sha256(_canonical_bytes(rows)).hexdigest(),
        "source_capture_status": "complete" if not sector_errors else "incomplete",
        "source_capture_errors": sector_errors,
        "route_formula_status": "not_defined_no_route_output",
        "missing_route_fields": protocol.get("not_yet_defined_and_never_imputed") or [],
        "h3_packet": {
            "packet_id": (h3_capture or {}).get("packet_id"),
            "path": (h3_capture or {}).get("path"),
            "influences_route": False,
        },
        "returns_or_labels_read": False,
        "authority": {
            "research_only": True,
            "changes_score": False,
            "changes_trade_count": False,
            "changes_position_or_quantity": False,
            "generates_alerts": False,
            "production": False,
            "automatic_trading": False,
        },
    }
    packet["packet_id"] = hashlib.sha256(_canonical_bytes(packet)).hexdigest()
    path = packet_dir / f"{decision_day}-{slot_id}.json"
    if path.exists():
        verified = verify_market_sector_route_packet(path, project_root=project_root)
        if verified.get("valid") is not True:
            return {
                "status": "existing_packet_invalid", "captured": False,
                "errors": verified.get("errors") or [], "path": str(path),
            }
        existing = verified["packet"]
        return {
            "status": "already_captured", "captured": True,
            "packet_id": existing.get("packet_id"), "path": str(path),
            "source_capture_status": existing.get("source_capture_status"),
        }
    _atomic_publish(path, _canonical_bytes(packet))
    verified = verify_market_sector_route_packet(path, project_root=project_root)
    if verified.get("valid") is not True:
        raise RuntimeError("route input packet failed post-write verification")
    return {
        "status": "captured", "captured": True,
        "packet_id": packet["packet_id"], "path": str(path),
        "source_capture_status": packet["source_capture_status"],
        "route_output_computed": False,
    }


__all__ = [
    "capture_slot", "capture_market_sector_route_input",
    "verify_market_sector_route_packet",
]
