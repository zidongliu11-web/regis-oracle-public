"""Immutable same-slot constituent evidence for confirmed research sectors."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .market_sector_route_forward import capture_slot
from .market_sector_route_shadow import verify_shadow_decision_packet
from .market_intelligence import (
    _direct_eastmoney_json, _EASTMONEY_SECTORS_URL, _EASTMONEY_UT,
)
from .market_state_daily import PROJECT_ROOT, _atomic_publish, _canonical_bytes, _sha256
from .research_artifact_paths import research_artifact_path


SH_TZ = ZoneInfo("Asia/Shanghai")
PROTOCOL_PATH = (
    PROJECT_ROOT
    / "research/MARKET_SECTOR_CONSTITUENT_FORWARD_PROTOCOL_V3.json"
)
PROTOCOL_SHA256 = "4509a9da1d16900efe99f5a1824fd84b45810cd6684c14acd85807948fff1eb8"
PACKET_DIR = (
    PROJECT_ROOT
    / "research/data/market_sector_route_forward/v1/constituent_packets"
)
_CONSTITUENT_FIELDS = "f2,f3,f5,f6,f8,f12,f14,f20,f21,f26,f124"


def fetch_constituent_page(fs: str, page: int) -> dict[str, Any] | None:
    """Reuse the existing Eastmoney transport with the frozen stock fields."""
    params = {
        "pn": str(page), "pz": "100", "po": "0", "np": "1", "ut": _EASTMONEY_UT,
        "fltt": "2", "invt": "2", "fid": "f12", "fs": fs,
        "fields": _CONSTITUENT_FIELDS,
    }
    try:
        payload = _direct_eastmoney_json(_EASTMONEY_SECTORS_URL, params)
    except RuntimeError:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    return {"total": data.get("total"), "diff": data.get("diff") or []}


def _project_path(raw: object, *, project_root: Path) -> Path:
    path = Path(str(raw or ""))
    resolved = (path if path.is_absolute() else project_root / path).resolve()
    resolved.relative_to(project_root.resolve())
    return resolved


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _epoch_time(value: object) -> str | None:
    number = _finite(value)
    if number is None or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number, SH_TZ).isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return None


def _listing_date(value: object) -> date | None:
    raw = str(value or "").strip()
    if len(raw) != 8 or not raw.isdigit():
        return None
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        return None


def verify_constituent_protocol(
    *, project_root: Path = PROJECT_ROOT, protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str | None = PROTOCOL_SHA256,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        protocol_path = _project_path(protocol_path, project_root=project_root)
        protocol_sha = _sha256(protocol_path)
        if expected_protocol_sha256 and protocol_sha != expected_protocol_sha256:
            errors.append("protocol_sha256_mismatch")
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        if protocol.get("status") != "frozen_before_first_forward_packet_no_outcome_access":
            errors.append("protocol_status_invalid")
        for name, binding in (protocol.get("bindings") or {}).items():
            path = _project_path(binding.get("path"), project_root=project_root)
            if not path.is_file() or _sha256(path) != binding.get("sha256"):
                errors.append(f"binding_invalid:{name}")
        authority = protocol.get("authority") or {}
        if authority.get("research_only") is not True or any(
            authority.get(key) is not False for key in (
                "changes_unified_score", "changes_trade_count",
                "changes_position_or_quantity", "generates_buy_or_sell_alert",
                "selects_production_candidate", "production_strategy",
                "automatic_trading",
            )
        ):
            errors.append("authority_invalid")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        protocol_sha, protocol = None, {}
        errors.append(f"protocol_unreadable:{exc}")
    return {
        "valid": not errors, "errors": errors, "protocol": protocol,
        "protocol_sha256": protocol_sha,
    }


def _fetch_sector_constituents(
    sector: dict[str, Any], fetch_page: Callable[[str, int], dict[str, Any] | None],
    *, capture_date: date, maximum_pages: int = 6,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    future_listings: list[dict[str, Any]] = []
    total: int | None = None
    errors: list[str] = []
    seen: set[str] = set()
    page_counts: list[int] = []
    reported_totals: list[int | None] = []
    for page in range(1, maximum_pages + 1):
        result = fetch_page(f"b:{sector['code']}", page)
        if not isinstance(result, dict):
            errors.append(f"page_{page}_unavailable")
            break
        raw_total = result.get("total")
        reported_totals.append(raw_total if type(raw_total) is int else None)
        if type(raw_total) is not int or raw_total <= 0:
            errors.append("reported_total_invalid")
        elif total is None:
            total = raw_total
        elif total != raw_total:
            errors.append("reported_total_changed")
        batch = result.get("diff")
        if not isinstance(batch, list):
            errors.append(f"page_{page}_rows_invalid")
            break
        page_counts.append(len(batch))
        if len(batch) > 100 or (total is not None and len(batch) != min(100, max(0, total-(page-1)*100))):
            errors.append("page_length_incomplete")
        for raw in batch:
            raw = raw if isinstance(raw, dict) else {}
            code = str(raw.get("f12") or "")
            name = str(raw.get("f14") or "")
            if not re.fullmatch(r"\d{6}", code) or not name.strip():
                errors.append("constituent_identity_invalid")
            if code in seen:
                errors.append("constituent_identity_duplicate")
            seen.add(code)
            price = _finite(raw.get("f2"))
            change = _finite(raw.get("f3"))
            volume = _finite(raw.get("f5"))
            amount = _finite(raw.get("f6"))
            turnover_rate = _finite(raw.get("f8"))
            total_market_cap = _finite(raw.get("f20"))
            float_market_cap = _finite(raw.get("f21"))
            listing_date = _listing_date(raw.get("f26"))
            data_as_of = _epoch_time(raw.get("f124"))
            if listing_date is not None and listing_date > capture_date:
                if not code or not name:
                    errors.append("future_listing_identity_missing")
                else:
                    future_listings.append({
                        "stock_code": code,
                        "stock_name": name,
                        "listing_date": listing_date.isoformat(),
                        "reason": "source_listing_date_after_capture_date",
                    })
                continue
            required_numbers = (
                price, change, volume, amount, turnover_rate,
                total_market_cap, float_market_cap,
            )
            if (
                not code or not name or not data_as_of
                or any(value is None for value in required_numbers)
            ):
                errors.append("constituent_required_field_missing")
            rows.append({
                "sector_kind": sector.get("kind"),
                "sector_code": sector.get("code"),
                "sector_name": sector.get("name"),
                "stock_code": code,
                "stock_name": name,
                "price": price,
                "change_pct": change,
                "volume": volume,
                "amount": amount,
                "turnover_rate": turnover_rate,
                "total_market_cap": total_market_cap,
                "float_market_cap": float_market_cap,
                "data_as_of": data_as_of,
            })
        if len(batch) < 100 or (total is not None and len(rows)+len(future_listings) >= total):
            break
    rows.sort(key=lambda row: (row["stock_code"], row["stock_name"]))
    future_listings.sort(key=lambda row: (row["stock_code"], row["stock_name"]))
    observed_count = len(rows) + len(future_listings)
    if total is not None and observed_count < total and len(page_counts) == maximum_pages:
        errors.append("page_limit_exceeded")
    complete = bool(total is not None and observed_count == total and not errors)
    return {
        "sector": {
            "kind": sector.get("kind"), "code": sector.get("code"),
            "name": sector.get("name"),
        },
        "reported_total": total,
        "fetched_count": observed_count,
        "eligible_constituent_count": len(rows),
        "excluded_future_listing_count": len(future_listings),
        "excluded_future_listings": future_listings,
        "excluded_future_listings_sha256": hashlib.sha256(
            _canonical_bytes(future_listings)
        ).hexdigest(),
        "complete": complete,
        "errors": list(dict.fromkeys(errors)),
        "pagination": {"order": "security_code_ascending", "page_size": 100,
                       "page_counts": page_counts, "maximum_pages": maximum_pages,
                       "reported_totals": reported_totals,
                       "unique_security_count": len(seen)},
        "rows": rows,
        "rows_sha256": hashlib.sha256(_canonical_bytes(rows)).hexdigest(),
    }


def verify_constituent_packet(
    path: Path, *, project_root: Path = PROJECT_ROOT,
    shadow_verifier: Callable[..., dict[str, Any]] = verify_shadow_decision_packet,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        resolved = research_artifact_path(path, project_root=project_root)
        packet = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"packet_unreadable:{exc}"]}
    if packet.get("schema") != "market_sector_constituent_forward.v1":
        errors.append("schema_invalid")
    identity = dict(packet)
    packet_id = str(identity.pop("packet_id", ""))
    if hashlib.sha256(_canonical_bytes(identity)).hexdigest() != packet_id:
        errors.append("packet_id_invalid")
    if packet.get("no_backfill") is not True or packet.get("returns_or_labels_read") is not False:
        errors.append("causality_boundary_invalid")
    authority = packet.get("authority") or {}
    if authority.get("research_only") is not True or any(
        authority.get(key) is not False for key in (
            "changes_unified_score", "changes_trade_count",
            "changes_position_or_quantity", "generates_buy_or_sell_alert",
            "selects_production_candidate", "production_strategy",
            "automatic_trading",
        )
    ):
        errors.append("authority_invalid")
    shadow = packet.get("shadow_decision") or {}
    try:
        shadow_path = research_artifact_path(shadow.get("path"), project_root=project_root)
        verified = shadow_verifier(shadow_path, project_root=project_root)
        shadow_packet = verified.get("packet") or {}
        if (
            verified.get("valid") is not True
            or _sha256(shadow_path) != shadow.get("sha256")
            or shadow_packet.get("packet_id") != shadow.get("packet_id")
            or shadow_packet.get("decision_day") != packet.get("decision_day")
            or shadow_packet.get("slot_id") != packet.get("slot_id")
        ):
            errors.append("shadow_identity_invalid")
    except (OSError, ValueError):
        errors.append("shadow_identity_invalid")
    captures = packet.get("sector_captures")
    if not isinstance(captures, list) or len(captures) > 3:
        errors.append("sector_captures_invalid")
    else:
        for capture in captures:
            rows = capture.get("rows")
            if not isinstance(rows, list) or hashlib.sha256(
                _canonical_bytes(rows)
            ).hexdigest() != capture.get("rows_sha256"):
                errors.append("constituent_rows_identity_invalid")
                continue
            future_listings = capture.get("excluded_future_listings") or []
            has_exclusion_contract = any(key in capture for key in (
                "eligible_constituent_count", "excluded_future_listing_count",
                "excluded_future_listings", "excluded_future_listings_sha256",
            ))
            if has_exclusion_contract and (
                not isinstance(future_listings, list)
                or hashlib.sha256(_canonical_bytes(future_listings)).hexdigest()
                != capture.get("excluded_future_listings_sha256")
                or capture.get("excluded_future_listing_count") != len(future_listings)
                or capture.get("eligible_constituent_count") != len(rows)
                or capture.get("fetched_count") != len(rows) + len(future_listings)
                or (capture.get("complete") is True
                    and capture.get("reported_total") != capture.get("fetched_count"))
            ):
                errors.append("future_listing_exclusion_identity_invalid")
            for excluded in future_listings:
                try:
                    excluded_date = date.fromisoformat(str(excluded.get("listing_date")))
                    decision_day = date.fromisoformat(str(packet.get("decision_day")))
                except (TypeError, ValueError):
                    errors.append("future_listing_exclusion_invalid")
                    break
                if (
                    not excluded.get("stock_code")
                    or not excluded.get("stock_name")
                    or excluded.get("reason") != "source_listing_date_after_capture_date"
                    or excluded_date <= decision_day
                ):
                    errors.append("future_listing_exclusion_invalid")
                    break
            if capture.get("complete") is True:
                codes = [row.get("stock_code") for row in rows+future_listings if isinstance(row, dict)]
                if (type(capture.get("reported_total")) is not int or capture["reported_total"] <= 0
                        or capture["reported_total"] != len(codes) or len(codes) != len(set(codes))
                        or any(not isinstance(code, str) or not re.fullmatch(r"\d{6}", code) for code in codes)
                        or capture.get("errors")):
                    errors.append("complete_capture_coverage_invalid")
                paging = capture.get("pagination")
                if paging is not None:
                    counts = paging.get("page_counts") or []
                    if (paging.get("order") != "security_code_ascending" or paging.get("page_size") != 100
                            or paging.get("maximum_pages") != 6 or not 1 <= len(counts) <= 6
                            or any(type(n) is not int or not 1 <= n <= 100 for n in counts)
                            or counts[:-1] != [100]*(len(counts)-1) or sum(counts) != len(codes)
                            or paging.get("reported_totals") != [len(codes)]*len(counts)
                            or paging.get("unique_security_count") != len(codes)):
                        errors.append("complete_capture_pagination_invalid")
                for row in rows:
                    if not isinstance(row, dict) or any(
                        row.get(field) is None for field in (
                            "stock_code", "stock_name", "price", "change_pct",
                            "volume", "amount", "turnover_rate",
                            "total_market_cap", "float_market_cap", "data_as_of",
                        )
                    ):
                        errors.append("complete_capture_required_field_missing")
                        break
        if (packet.get("capture_status") == "complete"
                and (packet.get("confirmed_sector_count") != len(captures)
                     or any(capture.get("complete") is not True for capture in captures))):
            errors.append("complete_packet_coverage_invalid")
    return {
        "valid": not errors, "errors": errors, "packet": packet,
        "packet_id": packet_id,
    }


def capture_confirmed_sector_constituents(
    shadow_capture: dict[str, Any],
    fetch_page: Callable[[str, int], dict[str, Any] | None], *,
    now: datetime | None = None, project_root: Path = PROJECT_ROOT,
    protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str | None = PROTOCOL_SHA256,
    packet_dir: Path = PACKET_DIR,
    shadow_verifier: Callable[..., dict[str, Any]] = verify_shadow_decision_packet,
) -> dict[str, Any]:
    observed = (now or datetime.now(SH_TZ)).astimezone(SH_TZ)
    protocol_check = verify_constituent_protocol(
        project_root=project_root, protocol_path=protocol_path,
        expected_protocol_sha256=expected_protocol_sha256,
    )
    if protocol_check.get("valid") is not True:
        return {"status": "identity_invalid", "captured": False, "errors": protocol_check["errors"]}
    try:
        shadow_path = research_artifact_path(shadow_capture.get("path"), project_root=project_root)
    except ValueError:
        return {"status": "shadow_input_invalid", "captured": False}
    shadow_check = shadow_verifier(shadow_path, project_root=project_root)
    if shadow_check.get("valid") is not True:
        return {"status": "shadow_input_invalid", "captured": False, "errors": shadow_check.get("errors") or []}
    shadow = shadow_check["packet"]
    if (
        observed.date().isoformat() != shadow.get("decision_day")
        or capture_slot(observed) != shadow.get("slot_id")
    ):
        return {"status": "outside_original_capture_slot", "captured": False}
    try:
        path = research_artifact_path(
            packet_dir / f"{shadow['decision_day']}-{shadow['slot_id']}.json",
            project_root=project_root,
        )
    except ValueError:
        return {"status": "capture_path_invalid", "captured": False}
    confirmed = (((shadow.get("route") or {}).get("sector_confirmation") or {}).get(
        "confirmed_sectors"
    ) or [])[:3]
    captures = [
        _fetch_sector_constituents(
            sector, fetch_page, capture_date=observed.date(),
        )
        for sector in confirmed if isinstance(sector, dict) and sector.get("code")
    ]
    packet = {
        "schema": "market_sector_constituent_forward.v1",
        "decision_day": shadow.get("decision_day"),
        "slot_id": shadow.get("slot_id"),
        "captured_at": observed.isoformat(timespec="seconds"),
        "no_backfill": True,
        "protocol": {
            "path": str(protocol_path.relative_to(project_root)),
            "sha256": protocol_check["protocol_sha256"],
        },
        "shadow_decision": {
            "path": str(shadow_path.relative_to(project_root)),
            "sha256": _sha256(shadow_path),
            "packet_id": shadow.get("packet_id"),
        },
        "confirmed_sector_count": len(confirmed),
        "sector_captures": captures,
        "capture_status": "complete" if all(row["complete"] for row in captures) else "incomplete",
        "valid_no_candidate_observation": not confirmed,
        "third_party_main_net_flow_captured": False,
        "stock_selected_or_ranked": False,
        "returns_or_labels_read": False,
        "authority": {
            "research_only": True, "changes_unified_score": False,
            "changes_trade_count": False, "changes_position_or_quantity": False,
            "generates_buy_or_sell_alert": False,
            "selects_production_candidate": False,
            "production_strategy": False, "automatic_trading": False,
        },
    }
    packet["packet_id"] = hashlib.sha256(_canonical_bytes(packet)).hexdigest()
    if path.exists():
        verified = verify_constituent_packet(
            path, project_root=project_root, shadow_verifier=shadow_verifier,
        )
        return {
            "status": "already_captured" if verified.get("valid") else "existing_packet_invalid",
            "captured": verified.get("valid") is True,
            "path": str(path), "packet_id": verified.get("packet_id"),
            "errors": verified.get("errors") or [],
        }
    _atomic_publish(path, _canonical_bytes(packet))
    verified = verify_constituent_packet(
        path, project_root=project_root, shadow_verifier=shadow_verifier,
    )
    if verified.get("valid") is not True:
        raise RuntimeError("constituent packet failed post-write verification")
    return {
        "status": "captured", "captured": True, "path": str(path),
        "packet_id": packet["packet_id"], "capture_status": packet["capture_status"],
        "confirmed_sector_count": len(confirmed),
        "constituent_count": sum(row["fetched_count"] for row in captures),
        "changes_trade_count_or_position": False,
    }


__all__ = [
    "capture_confirmed_sector_constituents", "fetch_constituent_page",
    "verify_constituent_packet",
    "verify_constituent_protocol",
]
