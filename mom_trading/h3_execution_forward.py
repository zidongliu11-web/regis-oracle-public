"""Immutable H3 Level-1 forward observations, isolated from score and execution."""

from __future__ import annotations

import hashlib
import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .market_intelligence import tencent_stock_depth
from .market_state_daily import PROJECT_ROOT, _atomic_publish, _canonical_bytes, _sha256
from .market_state_forward import _verify_selection_manifest, verify_forward_packet
from .research_artifact_paths import research_artifact_path


SH_TZ = ZoneInfo("Asia/Shanghai")
PROTOCOL_PATH = PROJECT_ROOT / "research/ANONYMOUS_ATTENTION_CROWDING_H3_FORWARD_OBSERVATION_PROTOCOL_V2.json"
PROTOCOL_SHA256 = "6b3b202707f19e6f3d76f2e1ba5272884cb1f2c05edd788919cc92df678d1b42"
PROTOCOL_AMENDMENT_PATH = PROJECT_ROOT / "research/ANONYMOUS_ATTENTION_CROWDING_H3_FORWARD_OBSERVATION_PROTOCOL_AMENDMENT_2.json"
PROTOCOL_AMENDMENT_SHA256 = "886991efb76cc8d37f1a618f6e2a135020f32312cc07e36bdc169efe0bf84f80"
PACKET_DIR = PROJECT_ROOT / "research/data/h3_execution_forward/v1/observation_packets"


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _levels(raw: Any) -> list[dict[str, float | None]]:
    rows: list[dict[str, float | None]] = []
    if not isinstance(raw, list):
        return rows
    for item in raw[:5]:
        if not isinstance(item, dict):
            continue
        price = _number(item.get("price"))
        volume = _number(item.get("volume"))
        if price is None or price <= 0:
            continue
        rows.append({"price": price, "volume": volume if volume is not None and volume >= 0 else None})
    return rows


def _diagnostics(buy: list[dict[str, Any]], sell: list[dict[str, Any]]) -> dict[str, float | None]:
    best_bid = _number((buy[0] if buy else {}).get("price"))
    best_ask = _number((sell[0] if sell else {}).get("price"))
    mid_price = None
    spread_bps = None
    if best_bid is not None and best_ask is not None and 0 < best_bid < best_ask:
        mid_price = (best_bid + best_ask) / 2
        spread_bps = 10000 * (best_ask - best_bid) / mid_price
    bid_volume = sum(
        value for row in buy if (value := _number(row.get("volume"))) is not None and value >= 0
    )
    ask_volume = sum(
        value for row in sell if (value := _number(row.get("volume"))) is not None and value >= 0
    )
    total = bid_volume + ask_volume
    return {
        "mid_price": mid_price,
        "spread_bps": spread_bps,
        "five_level_bid_volume": bid_volume if buy else None,
        "five_level_ask_volume": ask_volume if sell else None,
        "five_level_depth_imbalance": (bid_volume - ask_volume) / total if total > 0 else None,
    }


def _source_time_status(
    raw: Any, *, observed_at: datetime, maximum_age_seconds: int, maximum_future_skew_seconds: int,
) -> tuple[str, str | None, str | None]:
    if not raw:
        return "timestamp_missing", "source_data_time_unavailable", None
    text = str(raw)
    try:
        parsed = datetime.fromisoformat(text)
        parsed = parsed.replace(tzinfo=SH_TZ) if parsed.tzinfo is None else parsed.astimezone(SH_TZ)
    except ValueError:
        return "timestamp_invalid", "source_data_time_invalid", text
    age = (observed_at - parsed).total_seconds()
    if parsed.date() != observed_at.date() or age > maximum_age_seconds:
        return "stale", "source_quote_stale", parsed.isoformat(timespec="seconds")
    if age < -maximum_future_skew_seconds:
        return "future", "source_quote_future_timestamp", parsed.isoformat(timespec="seconds")
    return "observed", None, parsed.isoformat(timespec="seconds")


def _observation(
    rank: int, candidate: dict[str, Any], depth: dict[str, Any] | None, *,
    observed_at: datetime, maximum_age_seconds: int, maximum_future_skew_seconds: int,
) -> dict[str, Any]:
    code = str(candidate.get("code") or "")
    base: dict[str, Any] = {
        "candidate_rank": rank,
        "code": code,
        "name": str(candidate.get("name") or ""),
        "source": "tencent_public_level1_reference_quote",
        "applied_to_score": False,
        "applied_to_position_or_quantity": False,
        "applied_to_alerts": False,
    }
    if not isinstance(depth, dict):
        base.update({
            "status": "unavailable", "reason": "transport_or_source_unavailable",
            "source_data_time": None, "raw": None,
            "diagnostics": {
                "mid_price": None, "spread_bps": None,
                "five_level_bid_volume": None, "five_level_ask_volume": None,
                "five_level_depth_imbalance": None,
            },
        })
        return base
    buy = _levels((depth.get("depth") or {}).get("buy"))
    sell = _levels((depth.get("depth") or {}).get("sell"))
    status, reason, source_time = _source_time_status(
        depth.get("data_time"), observed_at=observed_at,
        maximum_age_seconds=maximum_age_seconds,
        maximum_future_skew_seconds=maximum_future_skew_seconds,
    )
    base.update({
        "status": status,
        "reason": reason,
        "source_data_time": source_time,
        "raw": {
            "price": _number(depth.get("price")),
            "previous_close": _number(depth.get("previous_close")),
            "buy": buy,
            "sell": sell,
            "outer_volume": _number(depth.get("outer_volume")),
            "inner_volume": _number(depth.get("inner_volume")),
            "volume_ratio": _number(depth.get("volume_ratio")),
            "turnover_rate": _number(depth.get("turnover_rate")),
            "average_price": _number(depth.get("avg_price")),
            "bid_ask_delta": _number(depth.get("bid_ask_delta")),
            "latest_tick": str(depth.get("latest_tick")) if depth.get("latest_tick") else None,
        },
        "diagnostics": _diagnostics(buy, sell),
    })
    return base


def _source_path(raw: Any, *, project_root: Path) -> Path:
    # Protocols and executable bindings never inherit runtime-data trust.
    supplied = str(raw or "").strip()
    if not supplied or ".." in Path(supplied).parts:
        raise ValueError("h3_source_path_invalid")
    path = (project_root / supplied).resolve()
    path.relative_to(project_root.resolve())
    return path


def _bound_protocol(
    protocol_path: Path, *, project_root: Path, expected_sha256: str | None,
) -> tuple[dict[str, Any], str]:
    protocol_path = _source_path(protocol_path, project_root=project_root)
    protocol_sha = _sha256(protocol_path)
    if expected_sha256 and protocol_sha != expected_sha256:
        raise RuntimeError("H3前向观察协议哈希不匹配")
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    for key, ref in (protocol.get("bindings") or {}).items():
        path = _source_path(ref.get("path"), project_root=project_root)
        if not path.is_file() or _sha256(path) != ref.get("sha256"):
            raise RuntimeError(f"H3绑定失效:{key}")
    return protocol, protocol_sha


def verify_h3_forward_packet(
    path: Path, *, project_root: Path = PROJECT_ROOT,
    h1_packet_verifier: Callable[..., dict[str, Any]] = verify_forward_packet,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        resolved = research_artifact_path(path, project_root=project_root)
        packet = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError) as exc:
        return {"valid": False, "errors": [f"packet_unreadable_or_outside_trusted_roots:{exc}"]}
    if packet.get("schema") != "h3_execution_level1_forward_observation.v1":
        errors.append("schema_invalid")
    packet_id = str(packet.get("packet_id") or "")
    identity = dict(packet)
    identity.pop("packet_id", None)
    if hashlib.sha256(_canonical_bytes(identity)).hexdigest() != packet_id:
        errors.append("packet_id_invalid")
    if packet.get("capture_only") is not True or packet.get("no_backfill") is not True:
        errors.append("capture_boundary_invalid")
    observations = packet.get("observations")
    protocol = packet.get("protocol") or {}
    h1 = packet.get("h1_forward_packet") or {}
    protocol_amendment = packet.get("protocol_amendment") or {}
    selection_manifest = packet.get("selection_manifest") or {}
    try:
        protocol_path = _source_path(protocol.get("path"), project_root=project_root)
        protocol_amendment_path = _source_path(protocol_amendment.get("path"), project_root=project_root)
        h1_path = research_artifact_path(h1.get("path"), project_root=project_root)
        selection_manifest_path = research_artifact_path(selection_manifest.get("path"), project_root=project_root)
    except (OSError, ValueError, RuntimeError) as exc:
        return {"valid": False, "errors": errors + [f"packet_reference_outside_trusted_roots:{exc}"]}
    if not protocol_path.is_file() or _sha256(protocol_path) != protocol.get("sha256"):
        errors.append("protocol_identity_invalid")
    if not h1_path.is_file() or _sha256(h1_path) != h1.get("sha256"):
        errors.append("h1_packet_identity_invalid")
    else:
        h1_verification = h1_packet_verifier(h1_path, project_root=project_root)
        if h1_verification.get("valid") is not True:
            errors.append("h1_packet_invalid")
        elif h1_verification.get("packet_id") != h1.get("packet_id"):
            errors.append("h1_packet_id_invalid")
    if (
        not protocol_amendment_path.is_file()
        or _sha256(protocol_amendment_path) != protocol_amendment.get("sha256")
    ):
        errors.append("protocol_amendment_identity_invalid")
    if (
        not selection_manifest_path.is_file()
        or _sha256(selection_manifest_path) != selection_manifest.get("sha256")
    ):
        errors.append("selection_manifest_identity_invalid")
    else:
        manifest_verification = _verify_selection_manifest(
            selection_manifest_path, project_root=project_root,
        )
        if manifest_verification.get("valid") is not True:
            errors.append("selection_manifest_invalid")
        else:
            expected_codes = manifest_verification["document"].get(
                "ordered_top24_codes"
            ) or []
            observed_codes = [str(row.get("code") or "") for row in observations or []]
            if observed_codes != expected_codes:
                errors.append("selection_manifest_observation_order_mismatch")
    if not isinstance(observations, list) or len(observations) > 24:
        errors.append("observation_scope_invalid")
    for row in observations or []:
        if any(row.get(key) is not False for key in (
            "applied_to_score", "applied_to_position_or_quantity", "applied_to_alerts",
        )):
            errors.append("observation_permission_invalid")
            break
    permissions = packet.get("permissions") or {}
    for key in (
        "read_returns_or_labels", "compute_backtest_or_ic", "change_unified_score",
        "change_position_or_quantity", "change_buy_or_sell_alerts",
        "production_strategy", "automatic_trading",
    ):
        if permissions.get(key) is not False:
            errors.append(f"permission_boundary_invalid:{key}")
    return {"valid": not errors, "errors": errors, "packet_id": packet_id, "packet": packet}


def capture_h3_forward_observation(
    h1_capture: dict[str, Any], *,
    now: datetime | None = None,
    project_root: Path = PROJECT_ROOT,
    protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str | None = PROTOCOL_SHA256,
    packet_dir: Path = PACKET_DIR,
    depth_fetcher: Callable[[str], dict[str, Any] | None] = tencent_stock_depth,
    h1_packet_verifier: Callable[..., dict[str, Any]] = verify_forward_packet,
    protocol_amendment_path: Path = PROTOCOL_AMENDMENT_PATH,
    expected_protocol_amendment_sha256: str | None = PROTOCOL_AMENDMENT_SHA256,
) -> dict[str, Any]:
    if not isinstance(h1_capture, dict) or h1_capture.get("captured") is not True:
        return {"status": "waiting_for_h1_forward_packet", "captured": False}
    if h1_capture.get("status") not in {"captured", "already_captured"}:
        return {"status": "waiting_for_h1_forward_packet", "captured": False}
    try:
        protocol_path = _source_path(protocol_path, project_root=project_root)
        protocol_amendment_path = _source_path(protocol_amendment_path, project_root=project_root)
        protocol, protocol_sha = _bound_protocol(
            protocol_path, project_root=project_root, expected_sha256=expected_protocol_sha256,
        )
        h1_path = research_artifact_path(h1_capture.get("path"), project_root=project_root)
        h1_verification = h1_packet_verifier(h1_path, project_root=project_root)
        if h1_verification.get("valid") is not True:
            return {"status": "h1_packet_invalid", "captured": False, "errors": h1_verification.get("errors") or []}
        h1_packet = h1_verification["packet"]
        amendment_sha = _sha256(protocol_amendment_path)
        if (
            expected_protocol_amendment_sha256
            and amendment_sha != expected_protocol_amendment_sha256
        ):
            raise RuntimeError("H3选择来源修订哈希不匹配")
        protocol_amendment = json.loads(
            protocol_amendment_path.read_text(encoding="utf-8")
        )
        selection_ref = h1_packet.get("selection_manifest") or {}
        selection_path = research_artifact_path(selection_ref.get("path"), project_root=project_root)
        selection_verification = _verify_selection_manifest(
            selection_path, project_root=project_root,
        )
        if (
            selection_verification.get("valid") is not True
            or _sha256(selection_path) != selection_ref.get("sha256")
        ):
            return {
                "status": "h1_selection_manifest_invalid",
                "captured": False,
                "errors": selection_verification.get("errors") or [],
            }
    except (OSError, ValueError, KeyError, json.JSONDecodeError, RuntimeError) as exc:
        return {"status": "identity_invalid", "captured": False, "reason": str(exc)}
    decision_day = str(h1_packet.get("decision_day") or "")
    slot_id = str(h1_packet.get("slot_id") or "")
    if decision_day < str(protocol.get("forward_start") or ""):
        return {"status": "waiting_forward_start", "captured": False, "forward_start": protocol.get("forward_start")}
    try:
        target = research_artifact_path(packet_dir / f"{decision_day}-{slot_id}.json", project_root=project_root)
    except (OSError, ValueError, RuntimeError) as exc:
        return {"status": "identity_invalid", "captured": False, "reason": str(exc)}
    if target.exists():
        verification = verify_h3_forward_packet(
            target, project_root=project_root, h1_packet_verifier=h1_packet_verifier,
        )
        if verification.get("valid") is not True:
            return {"status": "existing_packet_invalid", "captured": False, "errors": verification.get("errors") or []}
        existing = verification["packet"]
        return {
            "status": "already_captured", "captured": True, "path": str(target),
            "packet_id": existing.get("packet_id"),
            "observed_count": existing.get("observed_count"),
            "unavailable_count": existing.get("unavailable_count"),
        }
    candidates = (h1_packet.get("assessed_candidates_in_order") or [])[: int(protocol["capture_contract"]["candidate_limit"])]
    expected_codes = selection_verification["document"].get("ordered_top24_codes") or []
    if [str(row.get("code") or "") for row in candidates] != expected_codes:
        return {
            "status": "h1_selection_order_mismatch",
            "captured": False,
        }
    codes = [str(row.get("code") or "") for row in candidates]
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(codes)))) as executor:
        depth_rows = list(executor.map(depth_fetcher, codes))
    observed_dt = (now or datetime.now(SH_TZ)).astimezone(SH_TZ)
    freshness = protocol["capture_contract"]["quote_freshness"]
    observations = [
        _observation(
            rank, candidate, depth, observed_at=observed_dt,
            maximum_age_seconds=int(freshness["maximum_age_seconds"]),
            maximum_future_skew_seconds=int(freshness["maximum_future_skew_seconds"]),
        )
        for rank, (candidate, depth) in enumerate(zip(candidates, depth_rows), start=1)
    ]
    observed_at = observed_dt.isoformat(timespec="seconds")
    packet = {
        "schema": "h3_execution_level1_forward_observation.v1",
        "decision_day": decision_day,
        "slot_id": slot_id,
        "observed_at": observed_at,
        "capture_only": True,
        "no_backfill": True,
        "protocol": {
            "path": str(protocol_path.resolve().relative_to(project_root.resolve())),
            "sha256": protocol_sha,
        },
        "protocol_amendment": {
            "path": str(
                protocol_amendment_path.resolve().relative_to(project_root.resolve())
            ),
            "sha256": amendment_sha,
        },
        "h1_forward_packet": {
            "path": str(h1_path.relative_to(project_root.resolve())),
            "sha256": _sha256(h1_path),
            "packet_id": h1_packet.get("packet_id"),
        },
        "selection_manifest": {
            "path": str(selection_path.relative_to(project_root.resolve())),
            "sha256": _sha256(selection_path),
            "manifest_id": selection_verification["manifest_id"],
        },
        "source": {
            "name": "Tencent public Level-1 reference quote",
            "level": "Level-1 five-depth reference only",
            "exchange_official": False,
            "level2_queue_or_partial_fill": False,
            "transport_failure_semantics": "unavailable_not_zero_not_no_crowding",
        },
        "candidate_scope": "first_24_assessed_candidates_in_verified_H1_selection_manifest_order",
        "candidate_count": len(candidates),
        "observed_count": sum(row["status"] == "observed" for row in observations),
        "timestamp_unusable_count": sum(
            row["status"] in {"timestamp_missing", "timestamp_invalid", "stale", "future"}
            for row in observations
        ),
        "unavailable_count": sum(row["status"] == "unavailable" for row in observations),
        "observations": observations,
        "permissions": dict(protocol["permissions"]),
        "truth": {
            "profitability_evidence_created": False,
            "execution_edge_validated": False,
            "applied_to_score_position_or_alerts": False,
        },
    }
    packet_id = hashlib.sha256(_canonical_bytes(packet)).hexdigest()
    packet["packet_id"] = packet_id
    _atomic_publish(target, _canonical_bytes(packet))
    return {
        "status": "captured", "captured": True, "path": str(target),
        "packet_id": packet_id, "observed_count": packet["observed_count"],
        "unavailable_count": packet["unavailable_count"],
        "orders_or_returns_computed": False,
        "applied_to_score_position_or_alerts": False,
    }


__all__ = ["capture_h3_forward_observation", "verify_h3_forward_packet"]
