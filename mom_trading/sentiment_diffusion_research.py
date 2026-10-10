"""Prospective diffusion input geometry, never an admission or trading engine."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

SH = ZoneInfo("Asia/Shanghai")
BOUNDARY = dict(research_only=True, full_A_verified=False,
                historical_PIT_verified=False, source_rights_verified=False,
                production_eligible=False, generates_alert=False, automatic_trading=False)


def _clock(value):
    try:
        clock = datetime.fromisoformat(value)
        if clock.tzinfo is None or clock.utcoffset() is None:
            raise ValueError
        return clock
    except (TypeError, ValueError):
        raise ValueError("diffusion_aware_clock_required") from None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _unique(target, key, value):
    if key in target and target[key] != value:
        raise ValueError("diffusion_conflicting_observations")
    target[key] = value


def measure(snapshot: dict, *, minimum_active_members: int = 12) -> dict:
    """Compute only information available at a frozen decision clock.

    A supplied census is a measurement input, not proof of whole-market or PIT
    coverage. Known full-session suspensions stay in the census and are excluded
    explicitly from the trading denominator. Unknown/missing status is not flat.
    """
    if snapshot.get("schema") != "sentiment_diffusion_snapshot.v1":
        raise ValueError("diffusion_snapshot_schema_invalid")
    if (isinstance(minimum_active_members, bool) or not isinstance(minimum_active_members, int)
            or minimum_active_members < 1):
        raise ValueError("diffusion_minimum_members_invalid")
    decision = _clock(snapshot.get("decision_at"))
    known = _clock(snapshot.get("membership_known_at"))
    day = snapshot.get("session_day")
    if known > decision or day != decision.astimezone(SH).date().isoformat():
        raise ValueError("diffusion_membership_or_session_clock_invalid")
    members = snapshot.get("membership")
    if (not isinstance(members, dict) or not members
            or any(not isinstance(k, str) or not k.strip()
                   or not isinstance(v, str) or not v.strip() for k, v in members.items())):
        raise ValueError("diffusion_census_invalid")
    quote_rows, attention_rows = snapshot.get("quotes", []), snapshot.get("attention", [])
    if (not isinstance(quote_rows, list) or not isinstance(attention_rows, list)
            or any(not isinstance(row, dict) for row in quote_rows + attention_rows)):
        raise ValueError("diffusion_row_shape_invalid")
    quotes, attention = {}, {}
    for row in quote_rows:
        key = row.get("stable_instrument_id")
        if key not in members:
            continue
        event, available = _clock(row.get("event_at")), _clock(row.get("available_at"))
        if event > decision or available > decision or row.get("session_day") != day:
            continue
        if available < event:
            raise ValueError("diffusion_information_precedes_event")
        local_event = event.astimezone(SH)
        if (row.get("completed") is not True or local_event.date().isoformat() != day
                or local_event.time() < time(15)):
            continue
        status = row.get("trade_status")
        if status not in {"trading", "suspended"}:
            raise ValueError("diffusion_trade_status_unknown")
        ret, amount = _number(row.get("return_fraction")), _number(row.get("amount_yuan"))
        if ret is not None and ret <= -1:
            ret = None
        if amount is not None and amount < 0:
            amount = None
        if status == "suspended" and ((ret is not None and ret != 0)
                                      or (amount is not None and amount != 0)):
            raise ValueError("diffusion_suspension_evidence_conflict")
        _unique(quotes, key, (status, ret, amount, event, available))
    windows = set()
    for row in attention_rows:
        key = row.get("stable_instrument_id")
        if key not in members:
            continue
        start, end = _clock(row.get("window_start")), _clock(row.get("window_end"))
        available = _clock(row.get("available_at"))
        if available > decision or end > decision:
            continue
        if end.astimezone(SH).date().isoformat() != day:
            continue
        if available < end or end - start != timedelta(hours=24):
            raise ValueError("diffusion_attention_window_invalid")
        count = _number(row.get("new_watchlist_users_24h"))
        if count is not None and (count < 0 or not count.is_integer()):
            count = None
        windows.add((start, end))
        _unique(attention, key, (count, start, end, available))
    if len(windows) > 1:
        raise ValueError("diffusion_attention_windows_not_comparable")
    census_complete = len(quotes) == len(members)
    active = [key for key, row in quotes.items() if row[0] == "trading"]
    price_complete = census_complete and bool(active) and all(quotes[k][1] is not None for k in active)
    attention_complete = (census_complete and bool(active)
                          and all(k in attention and attention[k][0] is not None for k in active))
    market_breadth = sum(quotes[k][1] > 0 for k in active) / len(active) if price_complete else None
    market_attention = (sum(attention[k][0] > 0 for k in active) / len(active)
                        if attention_complete else None)
    groups = defaultdict(list)
    for key, sector in members.items():
        groups[sector].append(key)
    sectors = []
    for sector, expected in sorted(groups.items()):
        observed = [k for k in expected if k in quotes]
        eligible = [k for k in observed if quotes[k][0] == "trading"]
        n = len(eligible)
        sufficient = len(observed) == len(expected) and n >= minimum_active_members
        breadth = (sum(quotes[k][1] > 0 for k in eligible) / n
                   if sufficient and all(quotes[k][1] is not None for k in eligible) else None)
        excess = breadth - market_breadth if breadth is not None and market_breadth is not None else None
        effective = None
        if sufficient and census_complete and all(quotes[k][2] is not None for k in eligible):
            # Ratios avoid overflow from squared currency amounts.
            amounts = [quotes[k][2] for k in eligible]
            largest = max(amounts)
            if largest > 0:
                scaled = [a / largest for a in amounts]
                total = math.fsum(scaled)
                effective = 1 / (n * math.fsum((a / total) ** 2 for a in scaled))
        attention_breadth = (
            sum(attention[k][0] > 0 for k in eligible) / n
            if sufficient and all(k in attention and attention[k][0] is not None for k in eligible)
            else None)
        gap = (attention_breadth - market_attention - excess
               if attention_breadth is not None and market_attention is not None and excess is not None
               else None)
        sectors.append({
            "sector": sector, "census_members": len(expected), "active_members": n,
            "suspended_members": len(observed) - n, "missing_status_members": len(expected) - len(observed),
            "sector_breadth_excess_v1": excess,
            "sector_effective_amount_participation_v1": effective,
            "attention_price_diffusion_gap_v1": gap,
            "feature_status": "geometry_observed" if all(v is not None for v in (excess, effective, gap))
                              else "incomplete_geometry",
        })
    try:
        digest = sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
                                  allow_nan=False, separators=(",", ":")).encode()).hexdigest()
    except (TypeError, ValueError):
        raise ValueError("diffusion_snapshot_not_finite_json") from None
    return {"schema": "sentiment_diffusion_geometry.v1", "decision_at": decision.isoformat(),
            "session_day": day, "input_sha256": digest,
            "minimum_active_members": minimum_active_members,
            "measurement_census_complete": census_complete,
            "market_breadth": market_breadth, "market_attention_breadth": market_attention,
            "sectors": sectors, "economic_experiments_run": 0, "performance": None, **BOUNDARY}


def preflight(root: Path) -> dict:
    """Reuse the existing read-only input audit; never create a registry."""
    from .project_input_recovery import audit
    root = Path(root).resolve()
    path = root / "research/SENTIMENT_DIFFUSION_CANDIDATES_20260929.json"
    batch = json.loads(path.read_text(encoding="utf-8"))
    current = audit(root)
    bindings = {
        "existing_catalog_sha256": "factor_system/source_catalog.json",
        "retired_archive_sha256": "research/STATE_CONDITIONED_MAINLINE_SWING_V1_TERMINAL_ARCHIVE.json",
    }
    parent_current = all((root / name).is_file()
                         and sha256((root / name).read_bytes()).hexdigest() == batch[field]
                         for field, name in bindings.items())
    blockers = list(current["blockers"])
    if not parent_current:
        blockers.append("prior_family_binding_changed")
    blockers += ["complete decision-time census/PIT identity not supplied",
                 "independent same-window attention coverage not supplied",
                 "source rights and complete economic protocol not verified"]
    return {"schema": "sentiment_diffusion_batch_preflight.v1",
            "observed_at_utc": datetime.now(timezone.utc).isoformat(),
            "batch_id": batch["batch_id"], "batch_sha256": sha256(path.read_bytes()).hexdigest(),
            "parent_bindings_current": parent_current, "inputs": current,
            "candidate_results": [{"id": row["id"], "outcome": "evidence_insufficient",
                                   "economic_experiments_run": 0, "reasons": blockers}
                                  for row in batch["candidates"]],
            "complete_economic_configurations_frozen": 0, "cumulative_prior_family_trials": "unknown",
            "performance": None, "ready_for_economic_run": False, "writes_performed": False, **BOUNDARY}
