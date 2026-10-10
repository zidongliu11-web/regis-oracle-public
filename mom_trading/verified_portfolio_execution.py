"""Strict research-only wrapper for the joint portfolio execution replay.

The existing replay remains the only fill engine.  This module verifies its
causal inputs, applies one account-level budget, and preserves FIFO lot
provenance before returning a re-hashed packet.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from quantqc import ashare
from quantqc.costs import COSTS

from .intraday_market_emotion_forward import (
    build_intraday_emotion_observation,
    verify_intraday_emotion_packet,
)
from .mainline_start_candidates import verify_candidate_packet
from .market_state_daily import PROJECT_ROOT, _atomic_publish, _canonical_bytes
from .market_sector_route_shadow import verify_shadow_decision_packet
from .unified_regime_route_replay import (
    build_joint_replay_decision,
    replay_joint_execution_day,
    verify_joint_execution_day_packet,
    verify_joint_replay_packet,
)


_SHA256 = re.compile(r"[0-9a-f]{64}")
_LOT_SIZE = 100
_MAX_NEW_TRADES = 2
_MAX_STOCKS = 5
_MAX_SINGLE_WEIGHT = 0.20
_FROZEN_SLOTS = ("morning_1030", "afternoon_1400")
_A6_CONTRACT_ID = "verified_portfolio_execution_a6_strict_v1"
_FROZEN_EXECUTION_CONFIG = {
    "max_adv_participation": 0.05,
    "impact_coefficient_bps": 25.0,
    "slippage": float(COSTS["slippage"]),
}
_JOINT_PACKET_DIR = Path(
    "research/data/market_sector_route_forward/v1/joint_replay_packets"
)
_AUDITED_CALENDAR_PATH = Path("reports/current_market_calendar.json")
_IMPLEMENTATION_ARCHIVE_DIR = Path(
    "research/data/verified_portfolio_execution/implementation"
)


def _implementation_binding(project_root: Path) -> dict[str, str]:
    source = Path(__file__).resolve().read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    path = project_root / _IMPLEMENTATION_ARCHIVE_DIR / f"{digest}.py"
    _atomic_publish(path, source)
    return {
        "path": str(path.resolve().relative_to(project_root.resolve())),
        "sha256": digest,
    }


def _implementation_binding_valid(binding: object, project_root: Path) -> bool:
    if not isinstance(binding, dict):
        return False
    digest = str(binding.get("sha256") or "")
    if not _SHA256.fullmatch(digest):
        return False
    try:
        path = (project_root / str(binding.get("path") or "")).resolve()
        expected_parent = (project_root / _IMPLEMENTATION_ARCHIVE_DIR).resolve()
        path.relative_to(expected_parent)
        raw = path.read_bytes()
    except (OSError, ValueError):
        return False
    return (
        path.parent == expected_parent
        and path.name == f"{digest}.py"
        and hashlib.sha256(raw).hexdigest() == digest
    )


def _identity(value: dict[str, Any], field: str) -> str:
    body = copy.deepcopy(value)
    body.pop(field, None)
    return hashlib.sha256(_canonical_bytes(body)).hexdigest()


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _resolve_ref(ref: object, *, project_root: Path) -> tuple[Path | None, str | None]:
    if not isinstance(ref, dict):
        return None, "reference_invalid"
    expected = str(ref.get("sha256") or "")
    if not _SHA256.fullmatch(expected):
        return None, "reference_sha256_invalid"
    try:
        path = Path(str(ref.get("path") or ""))
        resolved = (path if path.is_absolute() else project_root / path).resolve()
        resolved.relative_to(project_root.resolve())
        actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return None, "reference_unreadable"
    return (resolved, None) if actual == expected else (None, "reference_sha256_mismatch")


def _load_json_ref(ref: object, *, project_root: Path) -> tuple[dict[str, Any] | None, str | None]:
    path, error = _resolve_ref(ref, project_root=project_root)
    if error:
        return None, error
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "reference_json_invalid"
    return (value, None) if isinstance(value, dict) else (None, "reference_json_invalid")


def _bound_packet_path(
    packet: dict[str, Any], key: str, *, project_root: Path,
) -> tuple[Path | None, str | None]:
    binding = packet.get(key)
    path, error = _resolve_ref(binding, project_root=project_root)
    if error:
        return None, f"{key}_{error}"
    return path, None


def _strict_decision_check(path: Path, *, project_root: Path) -> dict[str, Any]:
    """Verify the joint packet and each causal source with its dedicated verifier."""
    checked = verify_joint_replay_packet(path, project_root=project_root)
    packet = checked.get("packet") if isinstance(checked, dict) else None
    errors = list(checked.get("errors") or []) if isinstance(checked, dict) else []
    if checked.get("valid") is not True or not isinstance(packet, dict):
        return {"valid": False, "errors": errors or ["joint_packet_invalid"]}
    if packet.get("slot_id") not in _FROZEN_SLOTS:
        errors.append("joint_packet_slot_not_frozen")
    verifier_by_key = {
        "candidate_packet": verify_candidate_packet,
        "shadow_decision": verify_shadow_decision_packet,
        "intraday_emotion_input": verify_intraday_emotion_packet,
    }
    verified_sources: dict[str, dict[str, Any]] = {}
    for key, verifier in verifier_by_key.items():
        source_path, error = _bound_packet_path(packet, key, project_root=project_root)
        if error:
            errors.append(error)
            continue
        source = verifier(source_path, project_root=project_root)
        source_packet = source.get("packet") if isinstance(source, dict) else None
        binding = packet.get(key) or {}
        if (
            source.get("valid") is not True or not isinstance(source_packet, dict)
            or source_packet.get("packet_id") != binding.get("packet_id")
            or source_packet.get("decision_day") != packet.get("decision_day")
            or source_packet.get("slot_id") != packet.get("slot_id")
        ):
            errors.append(f"{key}_dedicated_verification_failed")
            continue
        verified_sources[key] = source_packet
    candidate = verified_sources.get("candidate_packet") or {}
    shadow = verified_sources.get("shadow_decision") or {}
    candidate_shadow = candidate.get("shadow_decision") or {}
    joint_shadow = packet.get("shadow_decision") or {}
    if candidate and (
        candidate_shadow.get("packet_id") != shadow.get("packet_id")
        or candidate_shadow.get("sha256") != joint_shadow.get("sha256")
    ):
        errors.append("candidate_shadow_chain_mismatch")
    protocol, protocol_error = _load_json_ref(
        packet.get("protocol"), project_root=project_root,
    )
    emotion = verified_sources.get("intraday_emotion_input") or {}
    if protocol_error or not candidate or not shadow or not emotion:
        errors.append("decision_recompute_inputs_invalid")
    else:
        try:
            captured_at = datetime.fromisoformat(str(emotion.get("captured_at") or ""))
            emotion_observation = build_intraday_emotion_observation(
                shadow.get("route") or {},
                {"breadth": emotion.get("raw_observation") or {}},
                now=captured_at,
            )
            recomputed = build_joint_replay_decision(
                shadow.get("route") or {}, candidate.get("research_result") or {},
                ((packet.get("account_snapshot") or {}).get("replay_inputs") or {}),
                protocol, intraday_emotion=emotion_observation,
            )
        except (KeyError, TypeError, ValueError):
            errors.append("decision_recompute_failed")
        else:
            if recomputed != packet.get("decision"):
                errors.append("decision_recompute_mismatch")
    return {
        "valid": not errors, "errors": sorted(set(errors)),
        "packet": packet, "packet_id": packet.get("packet_id"),
    }


def _canonical_day_manifest(
    decision_day: str, *, project_root: Path,
) -> tuple[dict[str, Path], list[str]]:
    directory = (project_root / _JOINT_PACKET_DIR).resolve()
    errors: list[str] = []
    found: dict[str, Path] = {}
    try:
        directory.relative_to(project_root.resolve())
        paths = sorted(directory.glob(f"{decision_day}-*.json"))
    except (OSError, ValueError):
        return {}, ["decision_manifest_unreadable"]
    for path in paths:
        prefix = f"{decision_day}-"
        slot = path.stem[len(prefix):] if path.stem.startswith(prefix) else ""
        if slot not in _FROZEN_SLOTS:
            errors.append(f"decision_manifest_unexpected_slot:{slot or 'unknown'}")
        elif slot in found:
            errors.append(f"decision_manifest_duplicate_slot:{slot}")
        else:
            found[slot] = path.resolve()
    missing = [slot for slot in _FROZEN_SLOTS if slot not in found]
    errors.extend(f"decision_manifest_missing_slot:{slot}" for slot in missing)
    return found, errors


def _verified_last_decision(
    refs: object, *, previous_trade_date: str, project_root: Path,
    verifier: Callable[..., dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[str]]:
    if not isinstance(refs, list) or not refs:
        return None, [], ["decision_refs_missing"]
    manifest, errors = _canonical_day_manifest(
        previous_trade_date, project_root=project_root,
    )
    verified: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    ref_paths: set[Path] = set()
    for index, ref in enumerate(refs):
        path, error = _resolve_ref(ref, project_root=project_root)
        if error:
            errors.append(f"decision_ref_{index}:{error}")
            continue
        ref_paths.add(path)
        checked = verifier(path, project_root=project_root)
        packet = checked.get("packet") if isinstance(checked, dict) else None
        if not isinstance(packet, dict) or checked.get("valid") is not True:
            errors.append(f"decision_ref_{index}:verification_failed")
            continue
        if str(packet.get("decision_day") or "") != previous_trade_date:
            errors.append(f"decision_ref_{index}:decision_day_mismatch")
            continue
        slot = str(packet.get("slot_id") or "")
        if slot not in _FROZEN_SLOTS:
            errors.append(f"decision_ref_{index}:slot_identity_invalid")
            continue
        if path != manifest.get(slot):
            errors.append(f"decision_ref_{index}:noncanonical_slot_path")
            continue
        if slot in verified:
            errors.append(f"decision_ref_{index}:duplicate_slot")
            continue
        verified[slot] = (packet, {
            "path": str(path.relative_to(project_root.resolve())),
            "sha256": str(ref["sha256"]), "packet_id": packet.get("packet_id"),
            "slot_id": packet.get("slot_id"),
        })
    if ref_paths != set(manifest.values()):
        errors.append("decision_refs_do_not_match_complete_manifest")
    if errors or set(verified) != set(_FROZEN_SLOTS):
        return None, [], errors or ["decision_refs_missing"]
    last = _FROZEN_SLOTS[-1]
    return verified[last][0], [verified[slot][1] for slot in _FROZEN_SLOTS], []


def _verify_calendar(
    ref: object, market_day: dict[str, Any], *, project_root: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    path, path_error = _resolve_ref(ref, project_root=project_root)
    if path_error:
        return None, f"calendar_{path_error}"
    if path != (project_root / _AUDITED_CALENDAR_PATH).resolve():
        return None, "calendar_asset_path_invalid"
    calendar, error = _load_json_ref(ref, project_root=project_root)
    if error:
        return None, f"calendar_{error}"
    dates = calendar.get("trading_dates")
    trade_date = str(market_day.get("trade_date") or "")
    previous_trade_date = str(market_day.get("previous_trade_date") or "")
    try:
        index = dates.index(trade_date) if isinstance(dates, list) else -1
    except ValueError:
        index = -1
    required = (
        calendar.get("source") == "BaoStock query_trade_dates"
        and isinstance(dates, list) and len(dates) == len(set(dates))
        and dates == sorted(dates)
        and index > 0 and dates[index - 1] == previous_trade_date
        and (calendar.get("coverage_end") or calendar.get("end_date")) == dates[-1]
    )
    normalized = {
        "trade_date": trade_date,
        "previous_trade_date": previous_trade_date,
        "calendar_asset_ref": copy.deepcopy(ref),
    }
    return (normalized, None) if required else (None, "calendar_identity_invalid")


def _verify_previous_state_ref(
    ref: object, state: dict[str, Any], *, previous_trade_date: str,
    project_root: Path, packet_verifier: Callable[..., dict[str, Any]],
) -> str | None:
    value, error = _load_json_ref(ref, project_root=project_root)
    if error or not isinstance(value, dict):
        return f"previous_state_{error or 'reference_invalid'}"
    if value.get("schema") == "verified_portfolio_execution.initial_zero_state.v1":
        frozen = value.get("state")
        costs = (state.get("cumulative_costs") or {}) if isinstance(state, dict) else {}
        cost_values = [_finite(number) for number in costs.values()]
        cumulative_buy = _finite(state.get("cumulative_buy_notional"))
        cumulative_sell = _finite(state.get("cumulative_sell_notional"))
        if (
            frozen != state
            or state.get("last_trade_date") != previous_trade_date
            or bool(state.get("positions")) or bool(state.get("pending_exit_orders"))
            or any(number is None or abs(number) > 1e-9 for number in cost_values)
            or cumulative_buy is None or abs(cumulative_buy) > 1e-9
            or cumulative_sell is None or abs(cumulative_sell) > 1e-9
        ):
            return "previous_state_initial_zero_invalid"
        return None
    wrapper = value.get("verified_wrapper")
    if value.get("schema") != "verified_portfolio_execution.a6" and (
        not isinstance(wrapper, dict)
        or wrapper.get("schema") != "verified_portfolio_execution.a6"
    ):
        return "previous_state_source_schema_invalid"
    checked = packet_verifier(value, project_root=project_root)
    packet = checked.get("packet") if isinstance(checked, dict) else None
    if (
        not isinstance(checked, dict) or checked.get("valid") is not True
        or not isinstance(packet, dict)
        or packet.get("trade_date") != previous_trade_date
        or packet.get("state_after") != state
    ):
        return "previous_state_execution_packet_invalid"
    return None


def _verify_cost_model(
    ref: object, state: dict[str, Any], *, project_root: Path,
) -> tuple[str | None, str | None]:
    path, error = _resolve_ref(ref, project_root=project_root)
    if error:
        return None, f"cost_model_{error}"
    if path != (project_root / "quantqc/costs.py").resolve():
        return None, "cost_model_path_invalid"
    sha256 = str(ref["sha256"])
    if state.get("cost_model_id") != sha256:
        return None, "state_cost_model_identity_mismatch"
    return sha256, None


def _verify_source_ref(
    ref: object, expected_packet_id: object, *, project_root: Path,
    verifier: Callable[..., dict[str, Any]], cache: dict[str, dict[str, Any]],
) -> str | None:
    path, error = _resolve_ref(ref, project_root=project_root)
    if error:
        return f"source_{error}"
    key = str(path)
    checked = cache.get(key)
    if checked is None:
        checked = verifier(path, project_root=project_root)
        cache[key] = checked
    packet = checked.get("packet") if isinstance(checked, dict) else None
    if (
        checked.get("valid") is not True or not isinstance(packet, dict)
        or packet.get("packet_id") != expected_packet_id
    ):
        return "source_packet_verification_failed"
    return None


def _validate_lots(
    state: dict[str, Any], *, project_root: Path,
    verifier: Callable[..., dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    source_cache: dict[str, dict[str, Any]] = {}
    last_day = str(state.get("last_trade_date") or "")
    for code, position in (state.get("positions") or {}).items():
        lots = position.get("lots") if isinstance(position, dict) else None
        if not isinstance(lots, list) or not lots:
            errors.append(f"position_lots_missing:{code}")
            continue
        shares = 0
        cost = 0.0
        sellable = 0
        locked = 0
        for lot in lots:
            lot_id = str((lot or {}).get("lot_id") or "") if isinstance(lot, dict) else ""
            quantity = (lot or {}).get("shares") if isinstance(lot, dict) else None
            lot_cost = _finite((lot or {}).get("book_cost_yuan") if isinstance(lot, dict) else None)
            buy_day = str((lot or {}).get("buy_trade_date") or "") if isinstance(lot, dict) else ""
            source = str((lot or {}).get("source_packet_id") or "") if isinstance(lot, dict) else ""
            playbook = str((lot or {}).get("playbook_id") or "") if isinstance(lot, dict) else ""
            factors = (lot or {}).get("factor_ids") if isinstance(lot, dict) else None
            if (
                not lot_id or lot_id in seen or not isinstance(quantity, int)
                or isinstance(quantity, bool) or quantity <= 0 or lot_cost is None
                or lot_cost < 0 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", buy_day)
                or buy_day > last_day or not source or not playbook
                or not isinstance(factors, list)
                or any(not isinstance(item, str) or not item for item in factors)
                or len(set(factors)) != len(factors)
            ):
                errors.append(f"position_lot_invalid:{code}")
                continue
            source_error = _verify_source_ref(
                lot.get("source_ref"), source, project_root=project_root,
                verifier=verifier, cache=source_cache,
            )
            if source_error:
                errors.append(f"position_lot_{source_error}:{code}")
                continue
            seen.add(lot_id)
            shares += quantity
            cost += lot_cost
            if buy_day < last_day:
                sellable += quantity
            else:
                locked += quantity
        if shares != position.get("shares_total"):
            errors.append(f"position_lot_share_mismatch:{code}")
        if abs(cost - float(position.get("book_cost_yuan") or 0.0)) > 0.01:
            errors.append(f"position_lot_cost_mismatch:{code}")
        if sellable != position.get("sellable_shares"):
            errors.append(f"position_lot_sellable_mismatch:{code}")
        if locked != sum(int(row.get("shares") or 0) for row in position.get("locked_buys") or []):
            errors.append(f"position_lot_locked_mismatch:{code}")
    return errors


def _verify_pending_exit_orders(
    state: dict[str, Any], *, project_root: Path,
    verifier: Callable[..., dict[str, Any]], trade_date: str,
    current_source: dict[str, Any], required_by_code: dict[str, int],
) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]], list[str]]:
    """Verify an old exit's exact authorization, then crop it to today's need."""
    normalized: list[dict[str, Any]] = []
    pending_by_code: dict[str, int] = {}
    superseded: list[dict[str, Any]] = []
    errors: list[str] = []
    source_cache: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for index, order in enumerate(state.get("pending_exit_orders") or []):
        if not isinstance(order, dict):
            errors.append(f"pending_exit_{index}:contract_invalid")
            continue
        order_id = str(order.get("order_id") or "")
        code = str(order.get("stock_code") or "")
        shares = order.get("requested_shares")
        authorization = order.get("authorization")
        authorization_id = str(order.get("authorization_id") or "")
        if (
            not order_id or order_id in seen
            or order.get("side") != "sell" or not code
            or not isinstance(shares, int) or isinstance(shares, bool) or shares <= 0
            or not isinstance(authorization, dict)
            or authorization_id != hashlib.sha256(_canonical_bytes(authorization)).hexdigest()
            or order_id != hashlib.sha256(authorization_id.encode("utf-8")).hexdigest()
        ):
            errors.append(f"pending_exit_{index}:identity_invalid")
            continue
        seen.add(order_id)
        source_error = _verify_source_ref(
            order.get("source_ref"), order.get("source_packet_id"),
            project_root=project_root, verifier=verifier, cache=source_cache,
        )
        if source_error:
            errors.append(f"pending_exit_{index}:{source_error}")
            continue
        source_path, _ = _resolve_ref(order.get("source_ref"), project_root=project_root)
        source_packet = (
            (source_cache.get(str(source_path)) or {}).get("packet")
            if source_path is not None else {}
        ) or {}
        authorized_target = _finite(authorization.get("target_exposure_pct"))
        source_target = _finite(
            (source_packet.get("decision") or {}).get("target_exposure_pct")
        )
        current_target = _finite(
            (current_source.get("decision") or {}).get("target_exposure_pct")
        )
        if (
            authorization.get("source_packet_id") != order.get("source_packet_id")
            or authorization.get("stock_code") != code
            or authorization.get("side") != "sell"
            or authorization.get("reason") != "risk_reduction"
            or authorization.get("authorized_shares") != order.get("authorized_shares")
            or not isinstance(order.get("authorized_shares"), int)
            or order.get("authorized_shares") < shares
            or authorized_target != source_target
            or order.get("reason") != "risk_reduction"
        ):
            errors.append(f"pending_exit_{index}:authorization_invalid")
            continue
        if authorized_target != current_target:
            superseded.append({
                "order_id": order_id,
                "stock_code": code,
                "reason": "target_exposure_changed",
                "authorized_target_exposure_pct": authorized_target,
                "current_target_exposure_pct": current_target,
                "superseded_by_source_packet_id": current_source.get("packet_id"),
            })
            continue
        valid_date = str(order.get("valid_trade_date") or "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", valid_date):
            errors.append(f"pending_exit_{index}:valid_date_invalid")
            continue
        if valid_date <= trade_date:
            remaining_need = max(
                0, int(required_by_code.get(code, 0)) - pending_by_code.get(code, 0),
            )
            cropped = min(shares, remaining_need)
            if cropped <= 0:
                continue
            if cropped != shares:
                order = copy.deepcopy(order)
                order["requested_shares"] = cropped
                order["full_liquidation"] = False
            normalized.append(copy.deepcopy(order))
            pending_by_code[code] = pending_by_code.get(code, 0) + cropped
    for code, shares in pending_by_code.items():
        position = (state.get("positions") or {}).get(code) or {}
        sellable = int(position.get("sellable_shares") or 0) + sum(
            int(row.get("shares") or 0) for row in position.get("locked_buys") or []
            if str(row.get("buy_trade_date") or "") < trade_date
        )
        if shares > sellable:
            errors.append(f"pending_exit_inventory_exceeded:{code}")
    return normalized, pending_by_code, superseded, errors


def _authorized_exit_order(
    *, source_packet_id: str, source_ref: dict[str, Any], stock_code: str,
    shares: int, target_exposure_pct: float, limit_price_yuan: float,
    trade_date: str, priority: str, full_liquidation: bool,
) -> dict[str, Any]:
    authorization = {
        "source_packet_id": source_packet_id,
        "stock_code": stock_code, "side": "sell",
        "reason": "risk_reduction", "authorized_shares": shares,
        "target_exposure_pct": target_exposure_pct,
    }
    authorization_id = hashlib.sha256(_canonical_bytes(authorization)).hexdigest()
    return {
        "source_packet_id": source_packet_id,
        "source_ref": copy.deepcopy(source_ref), "stock_code": stock_code,
        "side": "sell", "requested_shares": shares, "authorized_shares": shares,
        "authorization": authorization, "authorization_id": authorization_id,
        "order_id": hashlib.sha256(authorization_id.encode("utf-8")).hexdigest(),
        "limit_price_yuan": limit_price_yuan,
        "order_session": "next_trading_day_opening_auction",
        "order_type": "limit", "time_in_force": "carry_until_executable",
        "valid_trade_date": trade_date, "full_liquidation": full_liquidation,
        "reason": "risk_reduction", "priority": priority,
    }


def _position_values(
    state: dict[str, Any], quotes: dict[str, Any],
) -> tuple[dict[str, float], list[str]]:
    values: dict[str, float] = {}
    errors: list[str] = []
    for code, position in (state.get("positions") or {}).items():
        previous = _finite((quotes.get(code) or {}).get("prev_close"))
        if previous is None or previous <= 0:
            errors.append(f"position_previous_close_missing:{code}")
            continue
        values[code] = previous * int(position["shares_total"])
    return values, errors


def _execution_position_values(
    state: dict[str, Any], quotes: dict[str, Any],
) -> tuple[dict[str, float], dict[str, dict[str, Any]], list[str]]:
    values: dict[str, float] = {}
    audit: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for code, position in (state.get("positions") or {}).items():
        quote = quotes.get(code) or {}
        opening = _finite(quote.get("open_price"))
        previous = _finite(quote.get("prev_close"))
        if quote.get("tradable") is True and opening is not None and opening > 0:
            price, source = opening, "execution_open"
        elif quote.get("tradable") is not True and previous is not None and previous > 0:
            price, source = previous, "previous_close_for_untradable_held_position"
        else:
            errors.append(f"post_execution_valuation_missing:{code}")
            continue
        shares = int(position.get("shares_total") or 0)
        values[code] = price * shares
        audit[code] = {"price": price, "shares": shares, "source": source}
    return values, audit, errors


def _sell_limit(code: str, trade_date: str, quote: dict[str, Any]) -> float | None:
    previous = _finite(quote.get("prev_close"))
    rule = ashare.price_limit_rule(
        code, trade_date=trade_date,
        is_risk_warning=quote.get("is_risk_warning"),
        no_price_limit=quote.get("no_price_limit"),
    )
    if previous is None or previous <= 0 or rule.get("valid") is not True or rule.get("rate") is None:
        return None
    return ashare._limit_price(
        previous, float(rule["rate"]), "down",
        minimum_tick_rule_verified=ashare._minimum_tick_rule_verified(code, trade_date),
    )


def _risk_reduction_orders(
    state: dict[str, Any], market_day: dict[str, Any], *, target_exposure_pct: float,
    source_packet_id: str, source_ref: dict[str, Any],
    pending_by_code: dict[str, int],
) -> tuple[list[dict[str, Any]], float, float, list[str]]:
    quotes = market_day["quotes"]
    values, errors = _position_values(state, quotes)
    if errors:
        return [], 0.0, 0.0, errors
    equity = float(state["cash_yuan"]) + sum(values.values())
    target = equity * target_exposure_pct / 100.0
    reduction = max(0.0, sum(values.values()) - target)
    pending_value = sum(
        min(int((state["positions"].get(code) or {}).get("shares_total") or 0), shares)
        * values.get(code, 0.0)
        / max(1, int((state["positions"].get(code) or {}).get("shares_total") or 0))
        for code, shares in pending_by_code.items()
    )
    remaining = max(0.0, reduction - pending_value)
    orders: list[dict[str, Any]] = []
    trade_date = str(market_day["trade_date"])
    for code in sorted(values, key=lambda item: (-values[item], item)):
        if remaining <= 0.005:
            break
        position = state["positions"][code]
        quote = quotes.get(code) or {}
        price = _finite(quote.get("prev_close"))
        limit_price = _sell_limit(code, trade_date, quote)
        if price is None or limit_price is None:
            errors.append(f"risk_reduction_execution_identity_missing:{code}")
            continue
        sellable = int(position["sellable_shares"])
        sellable += sum(
            int(row["shares"]) for row in position.get("locked_buys") or []
            if str(row.get("buy_trade_date") or "") < trade_date
        )
        sellable = max(0, sellable - pending_by_code.get(code, 0))
        total = int(position["shares_total"])
        wanted = max(_LOT_SIZE, math.ceil(remaining / price / _LOT_SIZE) * _LOT_SIZE)
        full = sellable == total and wanted >= total
        shares = total if full else min((sellable // _LOT_SIZE) * _LOT_SIZE, wanted)
        if shares <= 0:
            continue
        full = shares == total and sellable == total
        order = _authorized_exit_order(
            source_packet_id=source_packet_id, source_ref=source_ref,
            stock_code=code, shares=shares,
            target_exposure_pct=target_exposure_pct,
            limit_price_yuan=limit_price, trade_date=trade_date,
            priority=f"{len(orders):04d}", full_liquidation=full,
        )
        orders.append(order)
        remaining = max(0.0, remaining - shares * price)
    return orders, equity, reduction, errors


def _budget_entry_packet(
    packet: dict[str, Any], state: dict[str, Any], market_day: dict[str, Any],
    *, equity: float, risk_reduction_required: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result = copy.deepcopy(packet)
    result.pop("packet_id", None)
    decision = result["decision"]
    plans = decision.get("pre_execution_plans") or []
    maximum = min(_MAX_NEW_TRADES, max(0, int(decision.get("maximum_new_trades") or 0)))
    target_pct = _finite(decision.get("target_exposure_pct"))
    target_value = equity * max(0.0, min(100.0, target_pct or 0.0)) / 100.0
    values, _ = _position_values(state, market_day["quotes"])
    remaining_total = max(0.0, target_value - sum(values.values()))
    accepted: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    held = set(values)
    if risk_reduction_required:
        blocked.extend({"plan_id": row.get("plan_id"), "reason": "risk_reduction_precedes_new_entries"}
                       for row in plans[:maximum])
        plans = []
    for plan in plans[:maximum]:
        row = copy.deepcopy(plan)
        code = str(row.get("stock_code") or "")
        price = _finite(row.get("limit_price_yuan"))
        shares = row.get("shares")
        if (
            ashare.board_of(code) not in {"main", "gemb"} or price is None or price <= 0
            or not isinstance(shares, int) or isinstance(shares, bool) or shares <= 0
            or shares % _LOT_SIZE
        ):
            blocked.append({"plan_id": row.get("plan_id"), "reason": "entry_contract_invalid"})
            continue
        if code not in held and len(held) >= _MAX_STOCKS:
            blocked.append({"plan_id": row.get("plan_id"), "reason": "maximum_concurrent_stocks"})
            continue
        single_room = max(0.0, equity * _MAX_SINGLE_WEIGHT - values.get(code, 0.0))
        capped = min(shares, int(single_room // price // _LOT_SIZE) * _LOT_SIZE,
                     int(remaining_total // price // _LOT_SIZE) * _LOT_SIZE)
        if capped <= 0:
            blocked.append({"plan_id": row.get("plan_id"), "reason": "account_exposure_budget"})
            continue
        row["shares"] = capped
        accepted.append(row)
        value = capped * price
        values[code] = values.get(code, 0.0) + value
        remaining_total -= value
        held.add(code)
    decision["pre_execution_plans"] = accepted
    decision["research_orders"] = copy.deepcopy(accepted)
    decision["maximum_new_trades"] = maximum
    result["verified_source_packet_id"] = packet.get("packet_id")
    result["packet_id"] = _identity(result, "packet_id")
    return result, blocked


def _decision_contract_errors(packet: dict[str, Any]) -> list[str]:
    decision = packet.get("decision") or {}
    plans = decision.get("pre_execution_plans")
    if not isinstance(plans, list):
        return ["decision_plans_invalid"]
    plan_ids = [
        str(row.get("plan_id") or "") if isinstance(row, dict) else ""
        for row in plans
    ]
    errors: list[str] = []
    if any(not value for value in plan_ids):
        errors.append("decision_plan_id_missing")
    if len(set(plan_ids)) != len(plan_ids):
        errors.append("decision_plan_id_duplicate")
    return errors


def _apply_fifo_attribution(
    packet: dict[str, Any], previous_state: dict[str, Any], source_packet: dict[str, Any],
    source_ref: dict[str, Any],
) -> list[str]:
    trade_date = str(packet["trade_date"])
    lots_by_code = {
        code: copy.deepcopy(position.get("lots") or [])
        for code, position in previous_state.get("positions", {}).items()
    }
    errors: list[str] = []
    source_decision = source_packet.get("decision") or {}
    factor_ids = list(((source_decision.get("factor_evidence") or {}).get("admitted_factor_ids") or []))
    candidate_step = next(
        (row.get("value") for row in source_decision.get("causal_steps") or []
         if isinstance(row, dict) and row.get("name") == "factor_and_stock_candidates"),
        {},
    )
    playbook_id = (
        candidate_step.get("playbook_id") if isinstance(candidate_step, dict) else None
    )
    for event in packet.get("ledger") or []:
        code = str(event.get("stock_code") or "")
        shares = int(event.get("filled_shares") or 0)
        if event.get("side") == "sell":
            needed = shares
            consumed: list[dict[str, Any]] = []
            for lot in sorted(lots_by_code.get(code, []), key=lambda row: (row["buy_trade_date"], row["lot_id"])):
                if needed <= 0 or lot["buy_trade_date"] >= trade_date:
                    continue
                take = min(needed, int(lot["shares"]))
                released = float(lot["book_cost_yuan"]) * take / int(lot["shares"])
                consumed.append({
                    "lot_id": lot["lot_id"], "shares": take,
                    "released_book_cost_yuan": released,
                    "source_packet_id": lot["source_packet_id"],
                    "source_ref": copy.deepcopy(lot["source_ref"]),
                    "playbook_id": lot.get("playbook_id"),
                    "factor_ids": copy.deepcopy(lot.get("factor_ids") or []),
                })
                lot["shares"] -= take
                lot["book_cost_yuan"] -= released
                needed -= take
            if needed:
                errors.append(f"fifo_sellable_inventory_mismatch:{code}")
                continue
            lots_by_code[code] = [row for row in lots_by_code.get(code, []) if row["shares"] > 0]
            released = sum(row["released_book_cost_yuan"] for row in consumed)
            event["released_book_cost_yuan"] = released
            event["realized_pnl_yuan"] = event["notional_yuan"] - event["costs"]["total"] - released
            event["attributed_lots"] = consumed
        elif event.get("side") == "buy":
            lot = {
                "lot_id": hashlib.sha256(
                    f"{event.get('order_id')}:{trade_date}:{code}".encode("utf-8")
                ).hexdigest(),
                "buy_trade_date": trade_date, "shares": shares,
                "book_cost_yuan": event["notional_yuan"] + event["costs"]["total"],
                "source_packet_id": source_packet.get("packet_id"),
                "source_ref": copy.deepcopy(source_ref),
                "playbook_id": playbook_id, "factor_ids": factor_ids,
            }
            lots_by_code.setdefault(code, []).append(lot)
            event["created_lot_id"] = lot["lot_id"]
    state = packet["state_after"]
    for code, position in state["positions"].items():
        lots = sorted(lots_by_code.get(code, []), key=lambda row: (row["buy_trade_date"], row["lot_id"]))
        if sum(int(row["shares"]) for row in lots) != int(position["shares_total"]):
            errors.append(f"fifo_state_share_mismatch:{code}")
            continue
        position["lots"] = lots
        position["book_cost_yuan"] = sum(float(row["book_cost_yuan"]) for row in lots)
        position["sellable_shares"] = sum(
            int(row["shares"]) for row in lots if row["buy_trade_date"] < trade_date
        )
        position["locked_buys"] = [
            {"buy_trade_date": row["buy_trade_date"], "shares": int(row["shares"])}
            for row in lots if row["buy_trade_date"] >= trade_date
        ]
    return errors


def _execute_verified_portfolio_day(
    previous_state: dict[str, Any], decision_refs: list[dict[str, Any]],
    market_day: dict[str, Any], *, calendar_ref: dict[str, Any],
    previous_state_ref: dict[str, Any], cost_model_ref: dict[str, Any],
    project_root: Path = PROJECT_ROOT,
    _decision_checker: Callable[..., dict[str, Any]] = _strict_decision_check,
    _previous_packet_checker: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Private test seam; the public entry always uses strict verification."""
    errors: list[str] = []
    if not isinstance(previous_state, dict) or not isinstance(market_day, dict):
        errors.append("state_or_market_input_invalid")
    else:
        calendar, calendar_error = _verify_calendar(
            calendar_ref, market_day, project_root=project_root,
        )
        if calendar_error:
            errors.append(calendar_error)
        previous_error = _verify_previous_state_ref(
            previous_state_ref, previous_state,
            previous_trade_date=str(market_day.get("previous_trade_date") or ""),
            project_root=project_root,
            packet_verifier=_previous_packet_checker or verify_verified_portfolio_execution_packet,
        )
        if previous_error:
            errors.append(previous_error)
        cost_sha, cost_error = _verify_cost_model(cost_model_ref, previous_state, project_root=project_root)
        if cost_error:
            errors.append(cost_error)
        errors.extend(_validate_lots(
            previous_state, project_root=project_root,
            verifier=_decision_checker,
        ))
    source, verified_refs, decision_errors = _verified_last_decision(
        decision_refs, previous_trade_date=str(market_day.get("previous_trade_date") or ""),
        project_root=project_root, verifier=_decision_checker,
    )
    errors.extend(decision_errors)
    if errors or source is None:
        return {
            "status": "verified_execution_input_invalid", "executed": False,
            "errors": sorted(set(errors)), "state_unchanged": True,
            "authority": {"research_only": True, "production": False, "automatic_trading": False},
        }
    contract_errors = _decision_contract_errors(source)
    if contract_errors:
        return {
            "status": "verified_execution_input_invalid", "executed": False,
            "errors": contract_errors, "state_unchanged": True,
            "authority": {"research_only": True, "production": False, "automatic_trading": False},
        }
    decision = source.get("decision") or {}
    target = _finite(decision.get("target_exposure_pct"))
    if target is None or not 0 <= target <= 100:
        return {
            "status": "verified_execution_input_invalid", "executed": False,
            "errors": ["target_exposure_invalid"], "state_unchanged": True,
            "authority": {"research_only": True, "production": False, "automatic_trading": False},
        }
    required_exits, equity, reduction, reduction_errors = _risk_reduction_orders(
        previous_state, market_day, target_exposure_pct=target,
        source_packet_id=str(source.get("packet_id") or ""),
        source_ref=verified_refs[-1], pending_by_code={},
    )
    if reduction_errors:
        return {
            "status": "verified_execution_input_invalid", "executed": False,
            "errors": sorted(set(reduction_errors)), "state_unchanged": True,
            "authority": {"research_only": True, "production": False, "automatic_trading": False},
        }
    required_by_code = {
        str(row["stock_code"]): int(row["requested_shares"])
        for row in required_exits
    }
    normalized_pending, pending_by_code, superseded_pending, pending_errors = _verify_pending_exit_orders(
        previous_state, project_root=project_root, verifier=_decision_checker,
        trade_date=str(market_day.get("trade_date") or ""),
        current_source=source, required_by_code=required_by_code,
    )
    if pending_errors:
        return {
            "status": "verified_execution_input_invalid", "executed": False,
            "errors": sorted(set(pending_errors)), "state_unchanged": True,
            "authority": {"research_only": True, "production": False, "automatic_trading": False},
        }
    execution_state = copy.deepcopy(previous_state)
    execution_state["pending_exit_orders"] = normalized_pending
    exits, equity, reduction, reduction_errors = _risk_reduction_orders(
        execution_state, market_day, target_exposure_pct=target,
        source_packet_id=str(source.get("packet_id") or ""),
        source_ref=verified_refs[-1], pending_by_code=pending_by_code,
    )
    if reduction_errors:
        return {
            "status": "verified_execution_input_invalid", "executed": False,
            "errors": sorted(set(reduction_errors)), "state_unchanged": True,
            "authority": {"research_only": True, "production": False, "automatic_trading": False},
        }
    execution_packet, blocked = _budget_entry_packet(
        source, execution_state, market_day, equity=equity,
        risk_reduction_required=reduction > 0.005,
    )
    result = replay_joint_execution_day(
        execution_state, [execution_packet], market_day, exits,
        execution_config=copy.deepcopy(_FROZEN_EXECUTION_CONFIG),
    )
    if result.get("executed") is not True:
        return result
    packet = result["packet"]
    attribution_errors = _apply_fifo_attribution(
        packet, execution_state, source, verified_refs[-1],
    )
    if attribution_errors:
        return {
            "status": "verified_execution_output_invalid", "executed": False,
            "errors": sorted(set(attribution_errors)), "state_unchanged": True,
            "authority": {"research_only": True, "production": False, "automatic_trading": False},
        }
    post_values, post_valuation, post_value_errors = _execution_position_values(
        packet["state_after"], market_day["quotes"],
    )
    if post_value_errors:
        return {
            "status": "verified_execution_output_invalid", "executed": False,
            "errors": sorted(set(post_value_errors)), "state_unchanged": True,
            "authority": {"research_only": True, "production": False, "automatic_trading": False},
        }
    post_equity = float(packet["state_after"]["cash_yuan"]) + sum(post_values.values())
    post_exposure_pct = (
        sum(post_values.values()) / post_equity * 100.0 if post_equity > 0 else 0.0
    )
    target_unreachable = post_exposure_pct > target + 1e-6
    packet["state_after"].pop("state_id", None)
    packet["state_after"]["cost_model_id"] = cost_sha
    packet["state_after"]["state_id"] = _identity(packet["state_after"], "state_id")
    packet["verified_wrapper"] = {
        "schema": "verified_portfolio_execution.a6",
        "a6_contract_id": _A6_CONTRACT_ID,
        "a6_implementation": _implementation_binding(project_root),
        "previous_state_before_normalization": copy.deepcopy(previous_state),
        "execution_input_state": copy.deepcopy(execution_state),
        "market_snapshot": copy.deepcopy(market_day),
        "budgeted_decision_packet": copy.deepcopy(execution_packet),
        "execution_config": copy.deepcopy(_FROZEN_EXECUTION_CONFIG),
        "normalized_previous_pending_exit_orders": copy.deepcopy(normalized_pending),
        "superseded_previous_pending_exit_orders": copy.deepcopy(superseded_pending),
        "decision_refs": verified_refs,
        "selected_source_packet_id": source.get("packet_id"),
        "selected_slot_id": source.get("slot_id"),
        "calendar_ref": copy.deepcopy(calendar_ref),
        "previous_state_ref": copy.deepcopy(previous_state_ref),
        "cost_model_ref": copy.deepcopy(cost_model_ref),
        "target_exposure_pct": target,
        "risk_reduction_review_yuan": reduction,
        "risk_reduction_orders": copy.deepcopy(exits),
        "actual_exposure_after_pct": round(post_exposure_pct, 6),
        "post_execution_valuation": post_valuation,
        "target_exposure_unreachable_due_to_execution": target_unreachable,
        "blocked_entry_plans": blocked,
        "lot_attribution": "FIFO_by_verified_lot_id",
        "research_only": True,
    }
    packet.pop("packet_id", None)
    packet["packet_id"] = _identity(packet, "packet_id")
    checked = _verify_verified_portfolio_execution_packet(
        packet, project_root=project_root,
        _decision_checker=_decision_checker,
        _previous_packet_checker=_previous_packet_checker,
    )
    if checked.get("valid") is not True:
        return {
            "status": "verified_execution_output_invalid", "executed": False,
            "errors": checked.get("errors") or ["execution_packet_verification_failed"],
            "state_unchanged": True,
            "authority": {"research_only": True, "production": False, "automatic_trading": False},
        }
    return {"status": "executed", "executed": True, "packet": packet}


def execute_verified_portfolio_day(
    previous_state: dict[str, Any], decision_refs: list[dict[str, Any]],
    market_day: dict[str, Any], *, calendar_ref: dict[str, Any],
    previous_state_ref: dict[str, Any], cost_model_ref: dict[str, Any],
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Public research entry; causal verification cannot be replaced by callers."""
    return _execute_verified_portfolio_day(
        previous_state, decision_refs, market_day,
        calendar_ref=calendar_ref, previous_state_ref=previous_state_ref,
        cost_model_ref=cost_model_ref,
        project_root=project_root,
        _decision_checker=_strict_decision_check,
        _previous_packet_checker=verify_verified_portfolio_execution_packet,
    )


def _verify_verified_portfolio_execution_packet(
    packet: object, *, project_root: Path,
    _decision_checker: Callable[..., dict[str, Any]],
    _previous_packet_checker: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(packet, dict):
        return {"valid": False, "errors": ["packet_invalid"]}
    base = verify_joint_execution_day_packet(packet)
    errors.extend(base.get("errors") or [])
    wrapper = packet.get("verified_wrapper")
    if not isinstance(wrapper, dict):
        return {"valid": False, "errors": sorted(set(errors + ["a6_wrapper_missing"]))}
    if (
        wrapper.get("schema") != "verified_portfolio_execution.a6"
        or wrapper.get("a6_contract_id") != _A6_CONTRACT_ID
        or wrapper.get("research_only") is not True
    ):
        errors.append("a6_binding_invalid")
    if not _implementation_binding_valid(
        wrapper.get("a6_implementation"), project_root,
    ):
        errors.append("a6_implementation_binding_invalid")
    if wrapper.get("execution_config") != _FROZEN_EXECUTION_CONFIG:
        errors.append("a6_execution_config_binding_invalid")
    market_for_calendar = wrapper.get("market_snapshot")
    calendar, calendar_error = _verify_calendar(
        wrapper.get("calendar_ref"),
        market_for_calendar if isinstance(market_for_calendar, dict) else {},
        project_root=project_root,
    )
    if calendar_error or not isinstance(calendar, dict):
        errors.append("a6_calendar_identity_invalid")
        calendar = {}
    source, normalized_refs, source_errors = _verified_last_decision(
        wrapper.get("decision_refs"),
        previous_trade_date=str(calendar.get("previous_trade_date") or ""),
        project_root=project_root, verifier=_decision_checker,
    )
    errors.extend(f"a6_{error}" for error in source_errors)
    if source is not None:
        source_target = _finite((source.get("decision") or {}).get("target_exposure_pct"))
        if (
            wrapper.get("decision_refs") != normalized_refs
            or wrapper.get("selected_source_packet_id") != source.get("packet_id")
            or wrapper.get("selected_slot_id") != _FROZEN_SLOTS[-1]
        ):
            errors.append("a6_decision_binding_invalid")
        if _finite(wrapper.get("target_exposure_pct")) != source_target:
            errors.append("a6_target_exposure_invalid")
    execution_input = wrapper.get("execution_input_state")
    previous_state_before_normalization = wrapper.get(
        "previous_state_before_normalization"
    )
    market_snapshot = wrapper.get("market_snapshot")
    if not isinstance(execution_input, dict):
        errors.append("a6_execution_input_state_invalid")
        execution_input = {}
    elif packet.get("previous_state_id") != (
        execution_input.get("state_id") or _identity(execution_input, "state_id")
    ):
        errors.append("a6_previous_state_binding_invalid")
    if isinstance(previous_state_before_normalization, dict):
        previous_error = _verify_previous_state_ref(
            wrapper.get("previous_state_ref"), previous_state_before_normalization,
            previous_trade_date=str(calendar.get("previous_trade_date") or ""),
            project_root=project_root,
            packet_verifier=(
                _previous_packet_checker or verify_verified_portfolio_execution_packet
            ),
        )
        if previous_error:
            errors.append(f"a6_{previous_error}")
    else:
        errors.append("a6_previous_state_before_normalization_invalid")
    if (
        not isinstance(market_snapshot, dict)
        or market_snapshot.get("market_snapshot_id")
        != _identity(market_snapshot, "market_snapshot_id")
        or packet.get("market_snapshot_id") != market_snapshot.get("market_snapshot_id")
        or packet.get("trade_date") != market_snapshot.get("trade_date")
    ):
        errors.append("a6_market_snapshot_binding_invalid")
        market_snapshot = {}
    state = packet.get("state_after") if isinstance(packet.get("state_after"), dict) else {}
    _, cost_error = _verify_cost_model(
        wrapper.get("cost_model_ref"), state, project_root=project_root,
    )
    if cost_error:
        errors.append(f"a6_{cost_error}")
    errors.extend(f"a6_{error}" for error in _validate_lots(
        state, project_root=project_root, verifier=_decision_checker,
    ))

    source_cache: dict[str, dict[str, Any]] = {}
    ledger_order_ids: list[str] = []
    created_lot_ids: list[str] = []
    for event in packet.get("ledger") or []:
        order_id = str(event.get("order_id") or "") if isinstance(event, dict) else ""
        if not order_id:
            errors.append("a6_ledger_order_id_missing")
            continue
        ledger_order_ids.append(order_id)
        created = event.get("created_lot_id")
        if created:
            created_lot_ids.append(str(created))
        for lot in event.get("attributed_lots") or []:
            if (
                not isinstance(lot, dict) or not str(lot.get("lot_id") or "")
                or not isinstance(lot.get("shares"), int) or lot.get("shares") <= 0
                or _finite(lot.get("released_book_cost_yuan")) is None
                or not str(lot.get("playbook_id") or "")
                or not isinstance(lot.get("factor_ids"), list)
            ):
                errors.append("a6_attributed_lot_invalid")
                continue
            source_error = _verify_source_ref(
                lot.get("source_ref"), lot.get("source_packet_id"),
                project_root=project_root, verifier=_decision_checker,
                cache=source_cache,
            )
            if source_error:
                errors.append(f"a6_attributed_lot_{source_error}")
    if len(ledger_order_ids) != len(set(ledger_order_ids)):
        errors.append("a6_ledger_order_id_duplicate")
    if len(created_lot_ids) != len(set(created_lot_ids)):
        errors.append("a6_created_lot_id_duplicate")

    valuation = wrapper.get("post_execution_valuation")
    if not isinstance(valuation, dict) or set(valuation) != set(state.get("positions") or {}):
        errors.append("a6_post_execution_valuation_invalid")
    else:
        market_value = 0.0
        for code, position in state.get("positions", {}).items():
            row = valuation.get(code) or {}
            price = _finite(row.get("price"))
            shares = row.get("shares")
            if (
                price is None or price <= 0 or shares != position.get("shares_total")
                or row.get("source") not in {
                    "execution_open", "previous_close_for_untradable_held_position",
                }
            ):
                errors.append(f"a6_post_execution_valuation_invalid:{code}")
                continue
            quote = (market_snapshot.get("quotes") or {}).get(code) or {}
            expected_price = (
                _finite(quote.get("open_price"))
                if row.get("source") == "execution_open"
                else _finite(quote.get("prev_close"))
            )
            if price != expected_price or (
                row.get("source") == "execution_open" and quote.get("tradable") is not True
            ) or (
                row.get("source") == "previous_close_for_untradable_held_position"
                and quote.get("tradable") is True
            ):
                errors.append(f"a6_post_execution_market_binding_invalid:{code}")
            market_value += price * int(shares)
        equity = _finite(state.get("cash_yuan"))
        if equity is None or equity + market_value <= 0:
            errors.append("a6_post_execution_equity_invalid")
        else:
            exposure = round(market_value / (equity + market_value) * 100.0, 6)
            if _finite(wrapper.get("actual_exposure_after_pct")) != exposure:
                errors.append("a6_actual_exposure_invalid")
            target = _finite(wrapper.get("target_exposure_pct"))
            unreachable = target is not None and exposure > target + 1e-6
            if wrapper.get("target_exposure_unreachable_due_to_execution") is not unreachable:
                errors.append("a6_target_unreachable_invalid")

    sell_ids = {
        str(row.get("order_id")) for row in packet.get("ledger") or []
        if isinstance(row, dict) and row.get("side") == "sell"
    } | {
        str(row.get("order_id")) for row in packet.get("unfilled") or []
        if isinstance(row, dict) and row.get("side") == "sell"
    } | {
        str(row.get("order_id")) for row in state.get("pending_exit_orders") or []
        if isinstance(row, dict)
    }
    risk_orders = wrapper.get("risk_reduction_orders")
    risk_ids = [
        str(row.get("order_id") or "") for row in risk_orders
        if isinstance(row, dict)
    ] if isinstance(risk_orders, list) else []
    if (
        not isinstance(risk_orders, list) or len(risk_ids) != len(risk_orders)
        or len(risk_ids) != len(set(risk_ids)) or any(not value for value in risk_ids)
        or any(str(value) not in sell_ids for value in risk_ids)
    ):
        errors.append("a6_risk_reduction_order_binding_invalid")
    if source is not None and execution_input and market_snapshot:
        target = _finite((source.get("decision") or {}).get("target_exposure_pct"))
        if target is not None:
            normalization_input = (
                previous_state_before_normalization
                if isinstance(previous_state_before_normalization, dict) else {}
            )
            full_required, expected_equity, expected_reduction, risk_errors = _risk_reduction_orders(
                normalization_input, market_snapshot, target_exposure_pct=target,
                source_packet_id=str(source.get("packet_id") or ""),
                source_ref=normalized_refs[-1] if normalized_refs else {},
                pending_by_code={},
            )
            required_by_code = {
                str(row["stock_code"]): int(row["requested_shares"])
                for row in full_required
            }
            normalized, pending_by_code, superseded, pending_errors = _verify_pending_exit_orders(
                normalization_input, project_root=project_root, verifier=_decision_checker,
                trade_date=str(market_snapshot.get("trade_date") or ""),
                current_source=source, required_by_code=required_by_code,
            )
            expected_risk, _, _, second_risk_errors = _risk_reduction_orders(
                {**copy.deepcopy(execution_input), "pending_exit_orders": normalized},
                market_snapshot, target_exposure_pct=target,
                source_packet_id=str(source.get("packet_id") or ""),
                source_ref=normalized_refs[-1] if normalized_refs else {},
                pending_by_code=pending_by_code,
            )
            if risk_errors or pending_errors or second_risk_errors:
                errors.append("a6_risk_reduction_recompute_failed")
            if wrapper.get("normalized_previous_pending_exit_orders") != normalized:
                errors.append("a6_pending_normalization_invalid")
            if wrapper.get("superseded_previous_pending_exit_orders") != superseded:
                errors.append("a6_pending_supersession_invalid")
            normalized_input = copy.deepcopy(previous_state_before_normalization)
            normalized_input["pending_exit_orders"] = copy.deepcopy(normalized)
            if normalized_input != execution_input:
                errors.append("a6_execution_input_normalization_invalid")
            if risk_orders != expected_risk:
                errors.append("a6_risk_reduction_recompute_mismatch")
            if abs(float(wrapper.get("risk_reduction_review_yuan") or 0.0) - expected_reduction) > 0.01:
                errors.append("a6_risk_reduction_review_invalid")
            expected_packet, expected_blocked = _budget_entry_packet(
                source, execution_input, market_snapshot, equity=expected_equity,
                risk_reduction_required=expected_reduction > 0.005,
            )
            if wrapper.get("budgeted_decision_packet") != expected_packet:
                errors.append("a6_budgeted_decision_invalid")
            if wrapper.get("blocked_entry_plans") != expected_blocked:
                errors.append("a6_blocked_entry_plans_invalid")
            try:
                replayed = replay_joint_execution_day(
                    copy.deepcopy(execution_input),
                    [copy.deepcopy(wrapper.get("budgeted_decision_packet"))],
                    copy.deepcopy(market_snapshot), copy.deepcopy(risk_orders),
                    execution_config=copy.deepcopy(_FROZEN_EXECUTION_CONFIG),
                )
            except (KeyError, TypeError, ValueError):
                replayed = {"executed": False}
            replay_packet = replayed.get("packet") if isinstance(replayed, dict) else None
            if replayed.get("executed") is not True or not isinstance(replay_packet, dict):
                errors.append("a6_deterministic_replay_failed")
            else:
                attribution_errors = _apply_fifo_attribution(
                    replay_packet, execution_input, source,
                    normalized_refs[-1] if normalized_refs else {},
                )
                if attribution_errors:
                    errors.append("a6_deterministic_fifo_failed")
                replay_packet["state_after"].pop("state_id", None)
                replay_packet["state_after"]["cost_model_id"] = state.get("cost_model_id")
                replay_packet["state_after"]["state_id"] = _identity(
                    replay_packet["state_after"], "state_id",
                )
                for field in ("ledger", "unfilled", "state_after", "daily_costs"):
                    if packet.get(field) != replay_packet.get(field):
                        errors.append(f"a6_deterministic_replay_mismatch:{field}")
    result: dict[str, Any] = {
        "valid": not errors,
        "errors": sorted(set(errors)),
    }
    # The next trading day's state verifier must consume the exact packet it
    # just verified.  Returning only a boolean made every non-initial public
    # execution fail closed because _verify_previous_state_ref could not bind
    # state_after to the recursively verified predecessor.
    if not errors:
        result.update({"packet": packet, "packet_id": packet.get("packet_id")})
    return result


def verify_verified_portfolio_execution_packet(
    packet: object, *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """A6 verifier; callers cannot replace any causal verifier."""
    return _verify_verified_portfolio_execution_packet(
        packet, project_root=project_root,
        _decision_checker=_strict_decision_check,
        _previous_packet_checker=verify_verified_portfolio_execution_packet,
    )


__all__ = [
    "execute_verified_portfolio_day",
    "verify_verified_portfolio_execution_packet",
]
