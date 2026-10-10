"""Immutable V2 joint envelope and fail-closed decision-audit boundary."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .account_snapshot import build_account_snapshot_identity
from .intraday_market_emotion_forward import verify_intraday_emotion_packet
from .mainline_start_candidates import verify_candidate_packet
from .market_sector_constituent_forward import verify_constituent_packet
from .market_sector_route_forward import (
    capture_slot,
    verify_market_sector_route_packet,
)
from .market_sector_route_shadow import verify_shadow_decision_packet
from .market_state_daily import (
    PROJECT_ROOT,
    SnapshotConflict,
    _atomic_publish,
    _canonical_bytes,
)
from .market_state_forward import verify_forward_packet
from .system_route_replay_driver import (
    PROTOCOL_PATH as SYSTEM_PROTOCOL_PATH,
    PROTOCOL_SHA256 as SYSTEM_PROTOCOL_SHA256,
)
from .unified_regime_route_replay import verify_joint_replay_packet


SH_TZ = ZoneInfo("Asia/Shanghai")
ENVELOPE_PROTOCOL_RELATIVE_PATH = Path(
    "research/SYSTEM_ROUTE_JOINT_ENVELOPE_V2_PROTOCOL_AMENDMENT_1.json"
)
ENVELOPE_PROTOCOL_PATH = PROJECT_ROOT / ENVELOPE_PROTOCOL_RELATIVE_PATH
ENVELOPE_PROTOCOL_SHA256 = (
    "208638f8255af8ae7e32beeab4d21dd6251a20b4491fcaa700d719cf2c445a03"
)
SYSTEM_DRIVER_PATH = PROJECT_ROOT / "mom_trading/system_route_replay_driver.py"
SYSTEM_DRIVER_SHA256 = (
    "7d1a2c7ba49ecf97cac82033385d88342f15337c4829190370b0cfef645777b2"
)
ENVELOPE_DIR = (
    PROJECT_ROOT / "research/data/system_route_joint_replay/v2/envelopes"
)
ACCOUNT_SNAPSHOT_DIR = (
    PROJECT_ROOT / "research/data/system_route_joint_replay/v2/account_snapshots"
)
CALENDAR_RELATIVE_PATH = Path("reports/current_market_calendar.json")

_SOURCE_SPECS: dict[
    str, tuple[str, Callable[..., dict[str, Any]]]
] = {
    "H1_market_state": (
        "market_state_forward_decision_packet.v1", verify_forward_packet,
    ),
    "market_sector": (
        "shadow_market_sector_route_input.v1",
        verify_market_sector_route_packet,
    ),
    "shadow_decision": (
        "market_sector_route_shadow_decision.v1",
        verify_shadow_decision_packet,
    ),
    "sector_constituents": (
        "market_sector_constituent_forward.v1", verify_constituent_packet,
    ),
    "stock_candidates": (
        "mainline_start_candidate_research.v1", verify_candidate_packet,
    ),
    "intraday_emotion": (
        "intraday_market_emotion_forward_input.v1",
        verify_intraday_emotion_packet,
    ),
}
_REF_BASE_KEYS = {"path", "sha256", "captured_at", "first_seen_at"}
_SOURCE_FROZEN_REFS: dict[str, tuple[tuple[str, str], ...]] = {
    "H1_market_state": (
        ("amendment", "H1_amendment"),
        ("selection_amendment", "H1_selection_amendment"),
        ("baseline", "H1_baseline"),
    ),
    "market_sector": (("protocol", "market_sector_protocol"),),
    "shadow_decision": (("protocol", "shadow_decision_protocol"),),
    "sector_constituents": (("protocol", "sector_constituent_protocol"),),
    "stock_candidates": (("formula", "stock_candidate_formula"),),
    "intraday_emotion": (("protocol", "intraday_emotion_protocol"),),
}
_SOURCE_TIME_FIELDS: dict[str, str | None] = {
    "H1_market_state": "decision_at",
    "market_sector": "captured_at",
    "shadow_decision": "decision_at",
    "sector_constituents": "captured_at",
    "stock_candidates": None,
    "intraday_emotion": "captured_at",
}
_LEGACY_FROZEN_REFS = (
    ("protocol", "legacy_joint_protocol"),
    ("protocol_amendment", "legacy_joint_amendment"),
    ("execution_protocol_amendment", "legacy_joint_execution_amendment"),
    ("execution_protocol_correction", "legacy_joint_execution_correction"),
)


def _authority() -> dict[str, bool]:
    return {
        "research_only": True,
        "changes_live_score": False,
        "changes_live_trade_count": False,
        "changes_live_position_or_quantity": False,
        "generates_buy_or_sell_alert": False,
        "connects_broker": False,
        "executes_orders": False,
        "computes_performance": False,
        "historical_return_run": False,
        "production_strategy": False,
        "automatic_trading": False,
        "stable_profit_claim": False,
    }


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _identity(value: dict[str, Any], field: str) -> str:
    body = copy.deepcopy(value)
    body.pop(field, None)
    try:
        return _sha256_bytes(_canonical_bytes(body))
    except (TypeError, ValueError, OverflowError):
        return ""


def _valid_sha(value: object) -> bool:
    text = str(value or "").lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(SH_TZ)


def _frozen_ref(protocol: dict[str, Any], name: str) -> dict[str, str]:
    value = (protocol.get("bindings") or {}).get(name) or {}
    return {"path": value.get("path"), "sha256": value.get("sha256")}


def _embedded_ref_errors(
    packet: dict[str, Any], specifications: tuple[tuple[str, str], ...],
    protocol: dict[str, Any], *, label: str,
) -> list[str]:
    return [
        f"{label}:frozen_ref_mismatch:{field}"
        for field, binding in specifications
        if packet.get(field) != _frozen_ref(protocol, binding)
    ]


def _authority_errors(value: object, *, label: str) -> list[str]:
    errors: list[str] = []

    def visit(item: object, path: str) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                child = f"{path}.{key}"
                if key == "authority":
                    if (
                        not isinstance(nested, dict)
                        or nested.get("research_only") is not True
                        or any(
                            flag is not False
                            for name, flag in nested.items()
                            if name != "research_only"
                        )
                    ):
                        errors.append(f"{label}:unsafe_authority:{child}")
                visit(nested, child)
        elif isinstance(item, list):
            for index, nested in enumerate(item):
                visit(nested, f"{path}[{index}]")

    visit(value, label)
    return errors


def _calendar_check(
    project_root: Path, decision_day: str,
) -> tuple[dict[str, str] | None, list[str]]:
    path = (project_root / CALENDAR_RELATIVE_PATH).resolve()
    try:
        path.relative_to(project_root.resolve())
        raw = path.read_bytes()
        calendar = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError):
        return None, ["trading_calendar_unreadable"]
    if not isinstance(calendar, dict):
        return None, ["trading_calendar_identity_invalid"]
    dates = calendar.get("trading_dates")
    if (
        calendar.get("source") != "BaoStock query_trade_dates"
        or not isinstance(dates, list)
        or not dates
        or dates != sorted(set(dates))
        or (calendar.get("coverage_end") or calendar.get("end_date"))
        != dates[-1]
    ):
        return None, ["trading_calendar_identity_invalid"]
    if decision_day not in dates:
        return None, ["decision_day_not_in_canonical_trading_calendar"]
    return {
        "path": str(CALENDAR_RELATIVE_PATH),
        "sha256": _sha256_bytes(raw),
    }, []


def _relative_path(value: object, *, project_root: Path) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ValueError("project_relative_path_required")
    root = project_root.resolve()
    path = (root / value).resolve()
    path.relative_to(root)
    return path


def _file_ref(
    path: Path, identity: str, *, project_root: Path,
    captured_at: str, first_seen_at: str, identity_key: str = "packet_id",
) -> dict[str, str]:
    resolved = path.resolve()
    resolved.relative_to(project_root.resolve())
    return {
        "path": str(resolved.relative_to(project_root.resolve())),
        "sha256": _sha256_path(resolved),
        identity_key: identity,
        "captured_at": captured_at,
        "first_seen_at": first_seen_at,
    }


def _load_ref(
    value: object, *, project_root: Path, label: str,
    identity_field: str = "packet_id", identity_key: str = "packet_id",
) -> tuple[Path | None, dict[str, Any] | None, dict[str, str] | None, list[str]]:
    if not isinstance(value, dict):
        return None, None, None, [f"{label}:ref_missing"]
    if set(value) != _REF_BASE_KEYS | {identity_key}:
        return None, None, None, [f"{label}:ref_schema_invalid"]
    if not _valid_sha(value.get("sha256")) or not _valid_sha(value.get(identity_key)):
        return None, None, None, [f"{label}:ref_identity_invalid"]
    captured = _time(value.get("captured_at"))
    first_seen = _time(value.get("first_seen_at"))
    if captured is None or first_seen is None or captured > first_seen:
        return None, None, None, [f"{label}:first_seen_or_capture_time_invalid"]
    try:
        path = _relative_path(value.get("path"), project_root=project_root)
        raw = path.read_bytes()
        packet = json.loads(raw)
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None, None, None, [f"{label}:ref_unreadable"]
    if not isinstance(packet, dict):
        return None, None, None, [f"{label}:object_required"]
    if _sha256_bytes(raw) != str(value["sha256"]).lower():
        return None, None, None, [f"{label}:sha256_mismatch"]
    packet_id = str(packet.get(identity_field) or "")
    try:
        expected_id = _identity(packet, identity_field)
    except (TypeError, ValueError):
        return None, None, None, [f"{label}:not_canonical_json"]
    if packet_id != value[identity_key] or packet_id != expected_id:
        return None, None, None, [f"{label}:packet_id_invalid"]
    normalized = {
        "path": str(path.relative_to(project_root.resolve())),
        "sha256": _sha256_bytes(raw),
        identity_key: packet_id,
        "captured_at": captured.isoformat(timespec="seconds"),
        "first_seen_at": first_seen.isoformat(timespec="seconds"),
    }
    return path, packet, normalized, []


def _binding_core(value: object) -> dict[str, Any]:
    row = value if isinstance(value, dict) else {}
    return {
        "path": row.get("path"),
        "sha256": row.get("sha256"),
        "packet_id": row.get("packet_id"),
    }


def _time_and_slot_errors(
    packet: dict[str, Any], ref: dict[str, str], *, decision_day: str,
    slot_id: str, decision_at: datetime, label: str,
    internal_time_field: str | None = None,
) -> list[str]:
    errors: list[str] = []
    if packet.get("decision_day") != decision_day or packet.get("slot_id") != slot_id:
        errors.append(f"{label}:decision_identity_mismatch")
    captured = _time(ref["captured_at"])
    first_seen = _time(ref["first_seen_at"])
    if captured is None or first_seen is None:
        return errors + [f"{label}:binding_time_invalid"]
    if (
        captured.date().isoformat() != decision_day
        or first_seen.date().isoformat() != decision_day
        or capture_slot(captured) != slot_id
        or capture_slot(first_seen) != slot_id
        or first_seen > decision_at
    ):
        errors.append(f"{label}:binding_outside_original_slot")
    internal = _time(packet.get(internal_time_field)) if internal_time_field else None
    if internal is None:
        errors.append(f"{label}:source_capture_timestamp_missing")
    elif captured != internal or first_seen != internal:
        errors.append(f"{label}:binding_timestamp_not_equal_to_source")
    return errors


def _protocol_errors(project_root: Path) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    try:
        protocol_path = _relative_path(
            str(ENVELOPE_PROTOCOL_RELATIVE_PATH), project_root=project_root,
        )
        raw = protocol_path.read_bytes()
        protocol = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}, ["envelope_protocol_unreadable"]
    if _sha256_bytes(raw) != ENVELOPE_PROTOCOL_SHA256:
        errors.append("envelope_protocol_sha256_mismatch")
    if protocol.get("status") != (
        "frozen_causality_remediation_before_first_real_v2_envelope_or_outcome_access"
    ):
        errors.append("envelope_protocol_status_invalid")
    if protocol.get("authority") != _authority():
        errors.append("envelope_protocol_authority_invalid")
    for name, binding in (protocol.get("bindings") or {}).items():
        try:
            path = _relative_path(binding.get("path"), project_root=project_root)
        except (AttributeError, OSError, ValueError):
            errors.append(f"envelope_protocol_binding_invalid:{name}")
            continue
        if not path.is_file() or _sha256_path(path) != binding.get("sha256"):
            errors.append(f"envelope_protocol_binding_invalid:{name}")
    adapter = protocol.get("adapter_contract") or {}
    if (
        adapter.get("current_status") != "adapter_not_implemented"
        or adapter.get("normalized_raw_to_V2_adapter_exists") is not False
        or adapter.get("audit_valid_while_adapter_not_implemented") is not False
        or adapter.get("valid_while_adapter_not_implemented") is not False
    ):
        errors.append("envelope_protocol_adapter_boundary_invalid")
    return protocol, list(dict.fromkeys(errors))


def _account_source_times(portfolio: dict[str, Any]) -> list[object]:
    times: list[object] = [portfolio.get("cash_as_of")]
    for row in portfolio.get("positions") or []:
        if isinstance(row, dict):
            times.extend((row.get("quote_as_of"), row.get("sellable_as_of")))
    return [value for value in times if value]


def _account_content_errors(
    portfolio: dict[str, Any], *, decision_day: str, observed_at: datetime,
) -> list[str]:
    errors: list[str] = []
    ledger = portfolio.get("ledger_identity")
    if not isinstance(ledger, dict) or not _valid_sha(ledger.get("combined_sha256")):
        errors.append("account_ledger_identity_invalid")
    if (
        portfolio.get("cash_user_recorded") is not True
        or portfolio.get("cash_source") != "user_recorded"
        or portfolio.get("valuation_status") != "complete"
        or portfolio.get("fee_source_status") != "complete"
        or _finite(portfolio.get("quote_coverage_pct")) != 100.0
    ):
        errors.append("account_completion_status_invalid")
    if not portfolio.get("cash_as_of") or _finite(
        portfolio.get("available_cash")
    ) is None:
        errors.append("account_cash_fact_invalid")
    positions = portfolio.get("positions")
    if not isinstance(positions, list):
        return errors + ["account_positions_invalid"]
    if portfolio.get("position_count") != len(positions):
        errors.append("account_position_count_invalid")
    ids: list[str] = []
    codes: list[str] = []
    market_values: list[float] = []
    required = {
        "id", "code", "shares", "sellable_shares_snapshot", "sellable_as_of",
        "entry_price", "entry_date", "current_price", "quote_as_of",
        "market_value", "remaining_buy_cost_yuan", "estimated_exit_cost_yuan",
        "pnl_semantics",
    }
    for index, row in enumerate(positions):
        if not isinstance(row, dict) or not required.issubset(row):
            errors.append(f"account_position_required_fields_missing:{index}")
            continue
        identifier, code = str(row.get("id") or ""), str(row.get("code") or "")
        ids.append(identifier)
        codes.append(code)
        shares = _finite(row.get("shares"))
        sellable = _finite(row.get("sellable_shares_snapshot"))
        price = _finite(row.get("current_price"))
        value = _finite(row.get("market_value"))
        entry = _finite(row.get("entry_price"))
        buy_cost = _finite(row.get("remaining_buy_cost_yuan"))
        exit_cost = _finite(row.get("estimated_exit_cost_yuan"))
        if (
            not identifier or not code
            or not row.get("quote_as_of") or not row.get("sellable_as_of")
            or not row.get("entry_date") or not row.get("pnl_semantics")
            or shares is None or not shares.is_integer()
            or shares < 0 or sellable is None or not sellable.is_integer()
            or not 0 <= sellable <= shares or price is None or price < 0
            or value is None or value < 0 or entry is None or entry < 0
            or buy_cost is None or buy_cost < 0 or exit_cost is None
            or exit_cost < 0 or abs(value - shares * price) > 0.01
        ):
            errors.append(f"account_position_economics_invalid:{index}")
        else:
            market_values.append(value)
    if len(ids) != len(set(ids)) or len(codes) != len(set(codes)):
        errors.append("account_position_identity_duplicate")
    total_market = _finite(portfolio.get("market_value"))
    cash = _finite(portfolio.get("available_cash"))
    equity = _finite(portfolio.get("account_equity"))
    if (
        total_market is None or cash is None or equity is None
        or min(total_market, cash, equity) < 0
        or abs(total_market - sum(market_values)) > 0.01
        or abs(equity - cash - total_market) > 0.01
    ):
        errors.append("account_totals_do_not_reconcile")
    for value in _account_source_times(portfolio):
        text = str(value)
        if len(text) == 10:
            if text != decision_day:
                errors.append("account_snapshot_source_day_invalid")
            continue
        source_time = _time(value)
        if (
            source_time is None or source_time > observed_at
            or source_time.date().isoformat() != decision_day
        ):
            errors.append("account_snapshot_source_time_invalid")
    return list(dict.fromkeys(errors))


def _build_account_packet(
    portfolio: dict[str, Any], *, decision_day: str, slot_id: str,
    captured_at: datetime,
) -> dict[str, Any]:
    if not isinstance(portfolio, dict):
        raise ValueError("account_snapshot_object_required")
    canonical_portfolio = copy.deepcopy(portfolio)
    identity = build_account_snapshot_identity(canonical_portfolio)
    if (
        identity.get("account_snapshot_status") != "ready"
        or not _valid_sha(identity.get("immutable_account_snapshot_sha256"))
        or canonical_portfolio.get("immutable_account_snapshot_sha256")
        != identity.get("immutable_account_snapshot_sha256")
        or identity.get("sellable_inventory_status") != "complete"
    ):
        raise ValueError("account_snapshot_identity_or_inventory_invalid")
    content_errors = _account_content_errors(
        canonical_portfolio, decision_day=decision_day,
        observed_at=captured_at,
    )
    if content_errors:
        raise ValueError(",".join(content_errors))
    snapshot_at = _time(identity.get("account_snapshot_as_of"))
    if snapshot_at is None or capture_slot(snapshot_at) != slot_id:
        raise ValueError("account_snapshot_internal_time_or_slot_invalid")
    packet = {
        "schema": "system_route_account_snapshot.v2",
        "decision_day": decision_day,
        "slot_id": slot_id,
        "captured_at": snapshot_at.isoformat(timespec="seconds"),
        "first_seen_at": snapshot_at.isoformat(timespec="seconds"),
        "no_backfill": True,
        "account_snapshot_schema": identity["account_snapshot_schema"],
        "account_snapshot_as_of": identity["account_snapshot_as_of"],
        "immutable_account_snapshot_sha256": identity[
            "immutable_account_snapshot_sha256"
        ],
        "sellable_inventory_status": identity["sellable_inventory_status"],
        "portfolio": canonical_portfolio,
        "full_canonical_portfolio_sha256": _sha256_bytes(
            _canonical_bytes(canonical_portfolio)
        ),
        "returns_or_labels_read": False,
        "authority": _authority(),
    }
    packet["packet_id"] = _identity(packet, "packet_id")
    return packet


def verify_system_route_account_snapshot_v2(
    path: Path, *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        resolved = path.resolve()
        resolved.relative_to(project_root.resolve())
        packet = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"account_packet_unreadable:{exc}"]}
    if not isinstance(packet, dict):
        return {"valid": False, "errors": ["account_packet_object_required"]}
    if packet.get("schema") != "system_route_account_snapshot.v2":
        errors.append("account_schema_invalid")
    if packet.get("packet_id") != _identity(packet, "packet_id"):
        errors.append("account_packet_id_invalid")
    if (
        packet.get("no_backfill") is not True
        or packet.get("returns_or_labels_read") is not False
    ):
        errors.append("account_causality_boundary_invalid")
    if packet.get("authority") != _authority():
        errors.append("account_authority_invalid")
    portfolio = packet.get("portfolio")
    if not isinstance(portfolio, dict):
        errors.append("account_portfolio_missing")
    else:
        try:
            full_sha = _sha256_bytes(_canonical_bytes(portfolio))
            identity = build_account_snapshot_identity(portfolio)
        except (TypeError, ValueError, OverflowError):
            full_sha, identity = None, {}
        if full_sha != packet.get("full_canonical_portfolio_sha256"):
            errors.append("account_full_portfolio_sha256_invalid")
        if (
            identity.get("account_snapshot_status") != "ready"
            or identity.get("immutable_account_snapshot_sha256")
            != packet.get("immutable_account_snapshot_sha256")
            or portfolio.get("immutable_account_snapshot_sha256")
            != packet.get("immutable_account_snapshot_sha256")
            or identity.get("sellable_inventory_status") != "complete"
            or packet.get("sellable_inventory_status") != "complete"
        ):
            errors.append("account_identity_or_inventory_invalid")
    captured = _time(packet.get("captured_at"))
    first_seen = _time(packet.get("first_seen_at"))
    if (
        captured is None or first_seen is None or captured != first_seen
        or captured.date().isoformat() != packet.get("decision_day")
        or capture_slot(captured) != packet.get("slot_id")
    ):
        errors.append("account_capture_identity_invalid")
    elif isinstance(portfolio, dict):
        errors.extend(_account_content_errors(
            portfolio, decision_day=str(packet.get("decision_day") or ""),
            observed_at=captured,
        ))
        if _time(packet.get("account_snapshot_as_of")) != captured:
            errors.append("account_snapshot_as_of_not_capture_time")
    return {
        "valid": not errors,
        "errors": list(dict.fromkeys(errors)),
        "packet": packet,
        "packet_id": packet.get("packet_id"),
    }


def _source_checks(
    refs: dict[str, Any], *, project_root: Path, decision_day: str,
    slot_id: str, envelope_captured_at: datetime, protocol: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, str]], list[str]]:
    packets: dict[str, dict[str, Any]] = {}
    normalized: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    for name, (schema, verifier) in _SOURCE_SPECS.items():
        path, packet, ref, ref_errors = _load_ref(
            refs.get(name), project_root=project_root,
            label=f"raw_input:{name}",
        )
        errors.extend(ref_errors)
        if path is None or packet is None or ref is None:
            continue
        checked = verifier(path, project_root=project_root)
        verified_packet = checked.get("packet") if isinstance(checked, dict) else None
        if (
            not isinstance(checked, dict) or checked.get("valid") is not True
            or not isinstance(verified_packet, dict)
        ):
            errors.append(f"raw_input:{name}:dedicated_verifier_failed")
            continue
        if packet != verified_packet or packet.get("schema") != schema:
            errors.append(f"raw_input:{name}:schema_or_verified_payload_mismatch")
            continue
        errors.extend(_embedded_ref_errors(
            packet, _SOURCE_FROZEN_REFS[name], protocol,
            label=f"raw_input:{name}",
        ))
        errors.extend(_authority_errors(packet, label=f"raw_input:{name}"))
        errors.extend(_time_and_slot_errors(
            packet, ref, decision_day=decision_day, slot_id=slot_id,
            decision_at=envelope_captured_at, label=f"raw_input:{name}",
            internal_time_field=_SOURCE_TIME_FIELDS[name],
        ))
        packets[name], normalized[name] = packet, ref
    if (
        (packets.get("market_sector") or {}).get("source_capture_status")
        != "complete"
    ):
        errors.append("raw_input:market_sector:source_capture_incomplete")
    constituents = packets.get("sector_constituents") or {}
    if constituents.get("capture_status") != "complete" and constituents.get(
        "valid_no_candidate_observation"
    ) is not True:
        errors.append("raw_input:sector_constituents:capture_incomplete")
    if (
        (packets.get("intraday_emotion") or {}).get("source_capture_status")
        != "complete"
    ):
        errors.append("raw_input:intraday_emotion:source_capture_incomplete")
    return packets, normalized, list(dict.fromkeys(errors))


def _cross_binding_errors(
    predecessor: dict[str, Any], packets: dict[str, dict[str, Any]],
    account: dict[str, Any], refs: dict[str, dict[str, str]],
) -> list[str]:
    errors: list[str] = []
    pairs = (
        (
            (packets.get("market_sector") or {}).get("h1_packet"),
            refs.get("H1_market_state"), "market_sector_to_H1",
        ),
        (
            (packets.get("shadow_decision") or {}).get("raw_input_packet"),
            refs.get("market_sector"), "shadow_to_market_sector",
        ),
        (
            (packets.get("sector_constituents") or {}).get("shadow_decision"),
            refs.get("shadow_decision"), "constituents_to_shadow",
        ),
        (
            (packets.get("stock_candidates") or {}).get("constituent_packet"),
            refs.get("sector_constituents"), "candidate_to_constituents",
        ),
        (
            (packets.get("stock_candidates") or {}).get("shadow_decision"),
            refs.get("shadow_decision"), "candidate_to_shadow",
        ),
        (
            predecessor.get("candidate_packet"), refs.get("stock_candidates"),
            "legacy_joint_to_candidate",
        ),
        (
            predecessor.get("shadow_decision"), refs.get("shadow_decision"),
            "legacy_joint_to_shadow",
        ),
        (
            predecessor.get("intraday_emotion_input"),
            refs.get("intraday_emotion"), "legacy_joint_to_emotion",
        ),
    )
    for left, right, label in pairs:
        if _binding_core(left) != _binding_core(right):
            errors.append(f"cross_binding_mismatch:{label}")
    legacy_snapshot = predecessor.get("account_snapshot") or {}
    if legacy_snapshot.get("identity") != account.get(
        "immutable_account_snapshot_sha256"
    ):
        errors.append("cross_binding_mismatch:legacy_joint_to_account_identity")
    portfolio = account.get("portfolio") or {}
    expected_replay = {
        "cash_user_recorded": portfolio.get("cash_user_recorded") is True,
        "valuation_status": portfolio.get("valuation_status"),
        "equity": portfolio.get("account_equity"),
        "available_cash": portfolio.get("available_cash"),
        "positions": [
            {
                "code": row.get("code"),
                "market_value": row.get("market_value"),
                "shares": row.get("shares"),
                "available_to_sell_shares": row.get(
                    "available_to_sell_shares",
                    row.get(
                        "available_quantity", row.get("sellable_shares_snapshot")
                    ),
                ),
            }
            for row in portfolio.get("positions") or [] if isinstance(row, dict)
        ],
    }
    if legacy_snapshot.get("replay_inputs") != expected_replay:
        errors.append("cross_binding_mismatch:legacy_joint_to_account_economics")
    return errors


def verify_system_route_joint_envelope_v2(
    path: Path, *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    protocol, errors = _protocol_errors(project_root)
    try:
        resolved = path.resolve()
        resolved.relative_to(project_root.resolve())
        envelope = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "valid": False, "evidence_valid": False, "audit_eligible": False,
            "errors": [f"envelope_unreadable:{exc}"],
        }
    if not isinstance(envelope, dict):
        return {
            "valid": False, "evidence_valid": False, "audit_eligible": False,
            "errors": ["envelope_object_required"],
        }
    if envelope.get("schema") != "system_route_joint_replay.v2":
        errors.append("envelope_schema_invalid")
    if envelope.get("status") != "adapter_not_implemented":
        errors.append("envelope_status_invalid")
    if envelope.get("envelope_id") != _identity(envelope, "envelope_id"):
        errors.append("envelope_id_invalid")
    if envelope.get("no_backfill") is not True:
        errors.append("envelope_no_backfill_invalid")
    if envelope.get("authority") != _authority():
        errors.append("envelope_authority_invalid")
    if (
        envelope.get("returns_or_labels_read") is not False
        or envelope.get("execution_performed") is not False
        or envelope.get("performance_computed") is not False
    ):
        errors.append("envelope_outcome_boundary_invalid")
    if envelope.get("adapter") != {
        "status": "adapter_not_implemented",
        "implementation_ref": None,
        "normalized_inputs": None,
    }:
        errors.append("envelope_adapter_boundary_invalid")
    if (
        envelope.get("v2_decision") is not None
        or envelope.get("economic_projection") is not None
    ):
        errors.append("unimplemented_adapter_must_not_emit_decision")
    expected_protocol_ref = {
        "path": str(ENVELOPE_PROTOCOL_RELATIVE_PATH),
        "sha256": ENVELOPE_PROTOCOL_SHA256,
    }
    expected_system_ref = {
        "path": str(SYSTEM_PROTOCOL_PATH.relative_to(PROJECT_ROOT)),
        "sha256": SYSTEM_PROTOCOL_SHA256,
    }
    expected_driver_ref = {
        "path": str(SYSTEM_DRIVER_PATH.relative_to(PROJECT_ROOT)),
        "sha256": SYSTEM_DRIVER_SHA256,
    }
    if envelope.get("envelope_protocol") != expected_protocol_ref:
        errors.append("envelope_protocol_identity_invalid")
    if envelope.get("system_route_replay_protocol") != expected_system_ref:
        errors.append("system_route_protocol_identity_invalid")
    if envelope.get("system_route_replay_driver") != expected_driver_ref:
        errors.append("system_route_driver_identity_invalid")

    decision_day = envelope.get("decision_day")
    slot_id = envelope.get("slot_id")
    decision_at = _time(envelope.get("decision_at"))
    captured_at = _time(envelope.get("captured_at"))
    first_seen_at = _time(envelope.get("first_seen_at"))
    if (
        not isinstance(decision_day, str) or not isinstance(slot_id, str)
        or decision_at is None or captured_at is None or first_seen_at is None
        or not (decision_at <= captured_at == first_seen_at)
        or captured_at.date().isoformat() != decision_day
        or capture_slot(captured_at) != slot_id
    ):
        errors.append("envelope_time_or_slot_invalid")
    calendar_ref, calendar_errors = _calendar_check(
        project_root, str(decision_day or ""),
    )
    errors.extend(calendar_errors)
    if envelope.get("trading_calendar") != calendar_ref:
        errors.append("trading_calendar_ref_invalid")

    predecessor_path, predecessor, predecessor_ref, predecessor_errors = _load_ref(
        envelope.get("predecessor_joint"), project_root=project_root,
        label="predecessor_joint",
    )
    errors.extend(predecessor_errors)
    if (
        predecessor_path is not None and predecessor is not None
        and predecessor_ref is not None
    ):
        checked = verify_joint_replay_packet(
            predecessor_path, project_root=project_root,
        )
        if (
            checked.get("valid") is not True
            or checked.get("packet") != predecessor
            or predecessor.get("schema") != "unified_regime_route_replay.v1"
        ):
            errors.append("predecessor_joint_dedicated_verifier_failed")
        errors.extend(_embedded_ref_errors(
            predecessor, _LEGACY_FROZEN_REFS, protocol,
            label="predecessor_joint",
        ))
        errors.extend(_authority_errors(
            predecessor, label="predecessor_joint",
        ))
        if captured_at is not None:
            errors.extend(_time_and_slot_errors(
                predecessor, predecessor_ref, decision_day=str(decision_day),
                slot_id=str(slot_id), decision_at=captured_at,
                label="predecessor_joint",
            ))

    raw_refs = envelope.get("raw_inputs")
    expected_raw_keys = set(_SOURCE_SPECS) | {"account_snapshot"}
    if not isinstance(raw_refs, dict) or set(raw_refs) != expected_raw_keys:
        errors.append("raw_inputs_schema_invalid")
        raw_refs = {}
    packets: dict[str, dict[str, Any]] = {}
    normalized_refs: dict[str, dict[str, str]] = {}
    if captured_at is not None:
        packets, normalized_refs, source_errors = _source_checks(
            raw_refs, project_root=project_root,
            decision_day=str(decision_day), slot_id=str(slot_id),
            envelope_captured_at=captured_at, protocol=protocol,
        )
        errors.extend(source_errors)

    account_path, account, account_ref, account_errors = _load_ref(
        raw_refs.get("account_snapshot"), project_root=project_root,
        label="raw_input:account_snapshot",
    )
    errors.extend(account_errors)
    if account_path is not None and account is not None and account_ref is not None:
        checked = verify_system_route_account_snapshot_v2(
            account_path, project_root=project_root,
        )
        if checked.get("valid") is not True or checked.get("packet") != account:
            errors.append("raw_input:account_snapshot:dedicated_verifier_failed")
        if captured_at is not None:
            errors.extend(_time_and_slot_errors(
                account, account_ref, decision_day=str(decision_day),
                slot_id=str(slot_id), decision_at=captured_at,
                label="raw_input:account_snapshot",
                internal_time_field="captured_at",
            ))
        normalized_refs["account_snapshot"] = account_ref

    if predecessor is not None and account is not None:
        errors.extend(_cross_binding_errors(
            predecessor, packets, account, normalized_refs,
        ))
    shadow_time = _time((packets.get("shadow_decision") or {}).get("decision_at"))
    if decision_at is None or shadow_time is None or decision_at != shadow_time:
        errors.append("envelope_decision_time_not_equal_to_shadow")
    errors = list(dict.fromkeys(errors))
    evidence_valid = not errors
    return {
        "valid": False,
        "evidence_valid": evidence_valid,
        "audit_eligible": False,
        "status": envelope.get("status"),
        "errors": errors,
        "envelope": envelope,
        "envelope_id": envelope.get("envelope_id"),
        "protocol_status": protocol.get("status"),
    }


def capture_system_route_joint_envelope_v2(
    *, predecessor_joint_ref: dict[str, Any], source_refs: dict[str, Any],
    account_snapshot: dict[str, Any], now: datetime | None = None,
    project_root: Path = PROJECT_ROOT, envelope_dir: Path = ENVELOPE_DIR,
    account_snapshot_dir: Path = ACCOUNT_SNAPSHOT_DIR,
) -> dict[str, Any]:
    """Freeze verified evidence only; V2 normalization remains unavailable."""
    root = project_root.resolve()
    live_root = root == PROJECT_ROOT.resolve()
    envelope_base = (
        envelope_dir if envelope_dir.is_absolute() else root / envelope_dir
    ).resolve()
    account_base = (
        account_snapshot_dir
        if account_snapshot_dir.is_absolute() else root / account_snapshot_dir
    ).resolve()
    try:
        envelope_base.relative_to(root)
        account_base.relative_to(root)
    except ValueError:
        return {"status": "output_path_outside_project", "captured": False}
    if live_root and (
        now is not None
        or envelope_base != ENVELOPE_DIR.resolve()
        or account_base != ACCOUNT_SNAPSHOT_DIR.resolve()
    ):
        return {
            "status": "live_clock_or_output_override_forbidden",
            "captured": False,
        }
    observed = (now or datetime.now(SH_TZ)).astimezone(SH_TZ).replace(
        microsecond=0
    )
    slot_id = capture_slot(observed)
    if slot_id is None:
        return {"status": "outside_original_capture_slot", "captured": False}
    protocol, protocol_errors = _protocol_errors(project_root)
    if protocol_errors:
        return {
            "status": "identity_invalid", "captured": False,
            "errors": protocol_errors,
        }
    decision_day = observed.date().isoformat()
    calendar_ref, calendar_errors = _calendar_check(project_root, decision_day)
    if calendar_errors or calendar_ref is None:
        return {
            "status": "trading_calendar_invalid", "captured": False,
            "errors": calendar_errors,
        }
    output = envelope_base / f"{decision_day}-{slot_id}.json"
    if output.exists():
        existing = verify_system_route_joint_envelope_v2(
            output, project_root=project_root,
        )
        return {
            "status": (
                "already_captured"
                if existing.get("evidence_valid") is True
                else "existing_envelope_invalid"
            ),
            "captured": existing.get("evidence_valid") is True,
            "valid": False,
            "evidence_valid": existing.get("evidence_valid") is True,
            "audit_valid": False,
            "path": str(output),
            "envelope_id": existing.get("envelope_id"),
            "errors": existing.get("errors") or [],
        }
    predecessor_path, predecessor, predecessor_ref, predecessor_errors = _load_ref(
        predecessor_joint_ref, project_root=project_root,
        label="predecessor_joint",
    )
    if (
        predecessor_errors or predecessor_path is None or predecessor is None
        or predecessor_ref is None
    ):
        return {
            "status": "predecessor_joint_invalid", "captured": False,
            "errors": predecessor_errors,
        }
    predecessor_errors = _embedded_ref_errors(
        predecessor, _LEGACY_FROZEN_REFS, protocol,
        label="predecessor_joint",
    ) + _authority_errors(predecessor, label="predecessor_joint")
    if predecessor_errors:
        return {
            "status": "predecessor_joint_invalid", "captured": False,
            "errors": list(dict.fromkeys(predecessor_errors)),
        }
    checked_joint = verify_joint_replay_packet(
        predecessor_path, project_root=project_root,
    )
    if (
        checked_joint.get("valid") is not True
        or predecessor.get("schema") != "unified_regime_route_replay.v1"
    ):
        return {
            "status": "predecessor_joint_invalid", "captured": False,
            "errors": checked_joint.get("errors") or ["legacy_joint_schema_invalid"],
        }
    predecessor_time_errors = _time_and_slot_errors(
        predecessor, predecessor_ref, decision_day=decision_day,
        slot_id=slot_id, decision_at=observed, label="predecessor_joint",
    )
    if predecessor_time_errors:
        return {
            "status": "predecessor_joint_invalid", "captured": False,
            "errors": predecessor_time_errors,
        }
    packets, normalized_refs, source_errors = _source_checks(
        source_refs, project_root=project_root, decision_day=decision_day,
        slot_id=slot_id, envelope_captured_at=observed, protocol=protocol,
    )
    if source_errors:
        return {
            "status": "raw_source_invalid", "captured": False,
            "errors": source_errors,
        }
    try:
        account_packet = _build_account_packet(
            account_snapshot, decision_day=decision_day, slot_id=slot_id,
            captured_at=observed,
        )
        account_payload = _canonical_bytes(account_packet)
        account_path = account_base / f"{account_packet['packet_id']}.json"
        account_ref = {
            "path": str(account_path.relative_to(root)),
            "sha256": _sha256_bytes(account_payload),
            "packet_id": account_packet["packet_id"],
            "captured_at": account_packet["captured_at"],
            "first_seen_at": account_packet["first_seen_at"],
        }
    except (OSError, TypeError, ValueError) as exc:
        return {
            "status": "account_snapshot_invalid", "captured": False,
            "errors": [str(exc)],
        }
    normalized_refs["account_snapshot"] = account_ref
    account_time_errors = _time_and_slot_errors(
        account_packet, account_ref, decision_day=decision_day,
        slot_id=slot_id, decision_at=observed,
        label="raw_input:account_snapshot",
        internal_time_field="captured_at",
    )
    cross_errors = _cross_binding_errors(
        predecessor, packets, account_packet, normalized_refs,
    )
    if account_time_errors or cross_errors:
        return {
            "status": "cross_binding_invalid", "captured": False,
            "errors": account_time_errors + cross_errors,
        }
    shadow_time = _time(packets["shadow_decision"].get("decision_at"))
    if shadow_time is None or shadow_time > observed:
        return {
            "status": "decision_time_invalid", "captured": False,
            "errors": ["shadow_decision_time_invalid"],
        }
    try:
        _atomic_publish(account_path, account_payload)
        checked_account = verify_system_route_account_snapshot_v2(
            account_path, project_root=project_root,
        )
        if checked_account.get("valid") is not True:
            raise ValueError("account_snapshot_post_write_invalid")
    except (OSError, SnapshotConflict, TypeError, ValueError) as exc:
        return {
            "status": "account_snapshot_invalid", "captured": False,
            "errors": [str(exc)],
        }
    envelope = {
        "schema": "system_route_joint_replay.v2",
        "status": "adapter_not_implemented",
        "decision_day": decision_day,
        "slot_id": slot_id,
        "decision_at": shadow_time.isoformat(timespec="seconds"),
        "captured_at": observed.isoformat(timespec="seconds"),
        "first_seen_at": observed.isoformat(timespec="seconds"),
        "no_backfill": True,
        "envelope_protocol": {
            "path": str(ENVELOPE_PROTOCOL_RELATIVE_PATH),
            "sha256": ENVELOPE_PROTOCOL_SHA256,
        },
        "system_route_replay_protocol": {
            "path": str(SYSTEM_PROTOCOL_PATH.relative_to(PROJECT_ROOT)),
            "sha256": SYSTEM_PROTOCOL_SHA256,
        },
        "system_route_replay_driver": {
            "path": str(SYSTEM_DRIVER_PATH.relative_to(PROJECT_ROOT)),
            "sha256": SYSTEM_DRIVER_SHA256,
        },
        "trading_calendar": calendar_ref,
        "predecessor_joint": predecessor_ref,
        "raw_inputs": normalized_refs,
        "adapter": {
            "status": "adapter_not_implemented",
            "implementation_ref": None,
            "normalized_inputs": None,
        },
        "v2_decision": None,
        "economic_projection": None,
        "returns_or_labels_read": False,
        "execution_performed": False,
        "performance_computed": False,
        "authority": _authority(),
    }
    envelope["envelope_id"] = _identity(envelope, "envelope_id")
    payload = _canonical_bytes(envelope)
    try:
        _atomic_publish(output, payload)
        verified = verify_system_route_joint_envelope_v2(
            output, project_root=project_root,
        )
        if verified.get("evidence_valid") is not True:
            raise ValueError("V2 evidence envelope failed post-write verification")
    except (OSError, SnapshotConflict, TypeError, ValueError) as exc:
        return {
            "status": "envelope_publish_invalid", "captured": False,
            "valid": False, "evidence_valid": False,
            "errors": [str(exc)],
        }
    return {
        "status": "adapter_not_implemented",
        "captured": True,
        "valid": False,
        "evidence_valid": True,
        "audit_valid": False,
        "path": str(output),
        "envelope_id": envelope["envelope_id"],
        "envelope_ref": _file_ref(
            output, envelope["envelope_id"], project_root=project_root,
            captured_at=envelope["captured_at"],
            first_seen_at=envelope["first_seen_at"],
            identity_key="envelope_id",
        ),
        "adapter_status": "adapter_not_implemented",
    }


def audit_system_route_joint_decision(
    *, envelope_ref: dict[str, Any] | None = None,
    decision_at: str | datetime | None = None,
    input_refs: dict[str, dict[str, Any]] | None = None,
    persisted_joint_ref: dict[str, Any] | None = None,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Reject legacy/unbound inputs; no V2 decision is auditable yet."""
    result: dict[str, Any] = {
        "schema": "system_route_joint_decision_audit.v2",
        "status": "v2_envelope_required",
        "valid": False,
        "audit_eligible": False,
        "envelope_ref": None,
        "all_economic_fields_match": False,
        "returns_or_labels_read": False,
        "execution_performed": False,
        "performance_computed": False,
        "errors": [],
        "authority": _authority(),
    }
    if envelope_ref is None:
        result["errors"] = [
            "legacy_v1_joint_or_unbound_raw_refs_forbidden"
            if (
                persisted_joint_ref is not None or input_refs is not None
                or decision_at is not None
            )
            else "system_route_joint_replay_v2_envelope_missing"
        ]
        result["audit_id"] = _identity(result, "audit_id")
        return result
    path, envelope, normalized, ref_errors = _load_ref(
        envelope_ref, project_root=project_root, label="v2_envelope",
        identity_field="envelope_id", identity_key="envelope_id",
    )
    result["envelope_ref"] = normalized
    if ref_errors or path is None or envelope is None:
        result["status"] = "envelope_invalid"
        result["errors"] = ref_errors
        result["audit_id"] = _identity(result, "audit_id")
        return result
    checked = verify_system_route_joint_envelope_v2(
        path, project_root=project_root,
    )
    ref_time_mismatch = bool(
        normalized is None
        or normalized.get("captured_at") != envelope.get("captured_at")
        or normalized.get("first_seen_at") != envelope.get("first_seen_at")
    )
    if checked.get("evidence_valid") is not True or ref_time_mismatch:
        result["status"] = "envelope_invalid"
        result["errors"] = list(checked.get("errors") or [])
        if ref_time_mismatch:
            result["errors"].append("envelope_ref_timestamp_mismatch")
        if not result["errors"]:
            result["errors"] = ["envelope_verification_failed"]
    else:
        result["status"] = "adapter_not_implemented"
        result["errors"] = ["normalized_raw_to_V2_adapter_not_implemented"]
    result["audit_id"] = _identity(result, "audit_id")
    return result


__all__ = [
    "ACCOUNT_SNAPSHOT_DIR", "ENVELOPE_DIR", "ENVELOPE_PROTOCOL_PATH",
    "ENVELOPE_PROTOCOL_SHA256", "audit_system_route_joint_decision",
    "capture_system_route_joint_envelope_v2",
    "verify_system_route_account_snapshot_v2",
    "verify_system_route_joint_envelope_v2",
]
