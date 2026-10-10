"""Causal, research-only adapter for the existing three-playbook router."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .market_sector_route_forward import capture_slot
from .market_state_daily import PROJECT_ROOT


SH_TZ = ZoneInfo("Asia/Shanghai")
PROTOCOL_PATH = PROJECT_ROOT / "research/MARKET_SECTOR_STRATEGY_ROUTER_V2_PROTOCOL.json"
PROTOCOL_SHA256 = "8878f3cb02b83a3eb677f90d19244fe51a7d0204dc78fd38660b23ed0776d9a8"
ROUTER_STATES = (
    "risk_closed",
    "weak_repair",
    "sentiment_rising",
    "crowded_climax",
    "retreat",
)
REQUIRED_PERCENTILES = (
    "up_ratio_pct",
    "above_ma20_ratio_pct",
    "new_high_minus_low_ratio_pct",
    "yesterday_limit_continuation_ratio_pct",
    "highest_board",
    "closed_limit_ratio_pct",
    "eod_failed_limit_ratio_pct",
    "top_5pct_amount_share_pct",
)
STATE_MAP = {
    "RISK_OFF": "risk_closed",
    "STABILIZING": "weak_repair",
    "RISK_ON": "sentiment_rising",
    "OVERHEATED": "crowded_climax",
}
SECTOR_RULE_ID = "same_slot_sector_breadth_top_decile_v1"
UNAVAILABLE_SECTOR_ENRICHMENTS = (
    "turnover_expansion",
    "constituent_leader_breadth",
    "sector_failed_limit_rate",
    "constituent_leadership_concentration",
)


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _source_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.replace(tzinfo=SH_TZ) if parsed.tzinfo is None else parsed.astimezone(SH_TZ)


def protocol_identity_valid(path: Path = PROTOCOL_PATH) -> bool:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == PROTOCOL_SHA256
    except OSError:
        return False


def adapt_market_observation(
    observation: dict[str, Any] | None, *, as_of: object = None,
) -> dict[str, Any]:
    observation = observation if isinstance(observation, dict) else {}
    percentiles = (
        observation.get("percentiles_pct")
        if isinstance(observation.get("percentiles_pct"), dict) else {}
    )
    missing = [
        key for key in REQUIRED_PERCENTILES
        if _finite(percentiles.get(key)) is None
        or not 0 <= float(percentiles[key]) <= 100
    ]
    confirmed = str(observation.get("state") or "UNKNOWN")
    raw = str(observation.get("raw_state") or "UNKNOWN")
    route_state = STATE_MAP.get(confirmed, "unknown")
    if (
        confirmed in {"RISK_ON", "OVERHEATED"}
        and raw in {"STABILIZING", "RISK_OFF"}
    ):
        route_state = "retreat"
    ready = bool(
        protocol_identity_valid()
        and observation.get("status") == "audited_diagnostic_not_validated"
        and observation.get("profitability_tested") is False
        and observation.get("applied_to_live_positions") is False
        and as_of
        and route_state in ROUTER_STATES
        and not missing
    )
    probabilities = {
        state: 1.0 if ready and state == route_state else 0.0
        for state in ROUTER_STATES
    }
    evidence = [
        f"H1 confirmed={confirmed}; raw={raw}",
        *[
            f"{key} lagged-percentile={round(float(percentiles[key]), 3)}%"
            for key in REQUIRED_PERCENTILES if key not in missing
        ],
    ] if ready else []
    contradictions = []
    if ready and float(percentiles["top_5pct_amount_share_pct"]) >= 90:
        contradictions.append("crowding_percentile_at_or_above_90")
    if ready and float(percentiles["eod_failed_limit_ratio_pct"]) >= 80:
        contradictions.append("failed_limit_stress_percentile_at_or_above_80")
    if ready and raw != confirmed:
        contradictions.append("raw_state_not_yet_confirmed")
    return {
        "status": "ready" if ready else "incomplete",
        "state": route_state if ready else "unknown",
        "as_of": as_of,
        "confidence": 1.0 if ready else None,
        "confidence_semantics": (
            "complete deterministic rule evaluation; not win probability, "
            "return forecast or profitability confidence"
        ),
        "state_probabilities": probabilities if ready else None,
        "state_probabilities_semantics": (
            "one-hot deterministic rule membership; not calibrated probability"
        ),
        "evidence": evidence,
        "contradictions": contradictions,
        "completed_data_only": ready,
        "missing_inputs": missing,
        "protocol": {
            "path": str(PROTOCOL_PATH.relative_to(PROJECT_ROOT)),
            "sha256": PROTOCOL_SHA256,
            "identity_valid": protocol_identity_valid(),
        },
    }


def _sector_rows(snapshot: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {}
    reported = int(meta.get("industry_reported_total") or 0) + int(
        meta.get("concept_reported_total") or 0
    )
    for kind, key in (("industry", "industries"), ("concept", "concepts")):
        for raw in snapshot.get(key) or []:
            if not isinstance(raw, dict):
                continue
            change = _finite(raw.get("change_pct"))
            up = _finite(raw.get("up_count"))
            down = _finite(raw.get("down_count"))
            code = str(raw.get("code") or "")
            name = str(raw.get("name") or "")
            if (
                not code or not name or change is None or up is None or down is None
                or up < 0 or down < 0
            ):
                continue
            total = int(up + down)
            rows.append({
                "kind": kind,
                "code": code,
                "name": name,
                "change_pct": change,
                "up_count": int(up),
                "down_count": int(down),
                "constituent_count": total,
                "advance_coverage_pct": (
                    round(up / total * 100, 3) if total > 0 else None
                ),
                "data_as_of": raw.get("data_as_of"),
            })
    return rows, reported


def adapt_sector_snapshot(
    snapshot: dict[str, Any] | None, *, now: datetime | None = None,
) -> dict[str, Any]:
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    observed = (now or datetime.now(SH_TZ)).astimezone(SH_TZ)
    slot = capture_slot(observed)
    meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {}
    completeness_keys = (
        "coverage_complete", "concept_complete", "industry_complete",
        "timestamp_complete", "breadth_count_complete",
    )
    source_as_of = _source_time(snapshot.get("data_as_of") or snapshot.get("as_of"))
    source_age = (
        (observed - source_as_of).total_seconds() if source_as_of is not None else None
    )
    rows, reported = _sector_rows(snapshot)
    coverage_pct = round(len(rows) / reported * 100, 3) if reported > 0 else None
    observation_ready = bool(
        protocol_identity_valid()
        and snapshot.get("status") == "ok"
        and snapshot.get("health_status") == "verified"
        and all(meta.get(key) is True for key in completeness_keys)
        and source_as_of is not None
        and source_as_of.date() == observed.date()
        and source_age is not None and 0 <= source_age <= 180
        and coverage_pct == 100.0
    )
    source_ready = bool(observation_ready and slot is not None)
    eligible = [
        row for row in rows
        if row["constituent_count"] >= 5
        and row["advance_coverage_pct"] is not None
    ]
    by_kind = {
        kind: [row["change_pct"] for row in eligible if row["kind"] == kind]
        for kind in ("concept", "industry")
    }
    for row in eligible:
        values = by_kind[row["kind"]]
        less = sum(value < row["change_pct"] for value in values)
        equal = sum(value == row["change_pct"] for value in values)
        row["change_rank_pct"] = round(
            (less + 0.5 * equal) / len(values) * 100, 3
        ) if values else None
    observed_candidates = [
        row for row in eligible
        if row["change_pct"] > 0
        and row["advance_coverage_pct"] >= 60
        and row["change_rank_pct"] >= 90
    ] if observation_ready else []
    observed_candidates.sort(key=lambda row: (
        -row["change_rank_pct"], -row["advance_coverage_pct"],
        -row["change_pct"], row["kind"], row["code"],
    ))
    observed_leaders = observed_candidates[:3]
    confirmed = observed_leaders if source_ready else []
    leaders = confirmed[:3]
    best = leaders[0] if leaders else {}
    return {
        "status": "confirmed" if leaders else (
            "not_confirmed" if source_ready else "unavailable"
        ),
        "confirmation_rule_id": SECTOR_RULE_ID,
        "observation_slot": slot,
        "as_of": observed.isoformat(timespec="seconds"),
        "source_as_of": source_as_of.isoformat(timespec="seconds") if source_as_of else None,
        "source_age_seconds": round(source_age, 3) if source_age is not None else None,
        "coverage_pct": coverage_pct,
        "observation_ready": observation_ready,
        "observed_sectors": observed_leaders,
        "observed_only_not_route_confirmation": not source_ready,
        "advance_coverage_pct": best.get("advance_coverage_pct"),
        "change_rank_pct": best.get("change_rank_pct"),
        "change_pct": best.get("change_pct"),
        "constituent_count": best.get("constituent_count"),
        "confirmed_sectors": leaders,
        "eligible_sector_count": len(eligible),
        "no_backfill": True,
        "point_in_time_snapshot_only": source_ready,
        "third_party_main_net_flow_used": False,
        "unavailable_enrichment_metrics": list(UNAVAILABLE_SECTOR_ENRICHMENTS),
        "protocol": {
            "path": str(PROTOCOL_PATH.relative_to(PROJECT_ROOT)),
            "sha256": PROTOCOL_SHA256,
            "identity_valid": protocol_identity_valid(),
        },
    }


__all__ = [
    "PROTOCOL_PATH", "PROTOCOL_SHA256", "SECTOR_RULE_ID",
    "adapt_market_observation", "adapt_sector_snapshot", "protocol_identity_valid",
]
