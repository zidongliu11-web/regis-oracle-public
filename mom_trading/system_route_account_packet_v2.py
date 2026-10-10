"""Pure successor account packet with separate source and receipt clocks."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .account_snapshot import build_account_snapshot_identity
from .research_artifact_paths import research_artifact_path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = (
    PROJECT_ROOT
    / "research/SYSTEM_ROUTE_ACCOUNT_PACKET_V2_PROTOCOL_V1_AMENDMENT_1.json"
)
PROTOCOL_SHA256 = "b2fbffb986e0a10d69c9c177ee714f03908317cc660b84b841eba07e47dd8067"
SH_TZ = ZoneInfo("Asia/Shanghai")


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


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ) + "\n"
    ).encode("utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _identity(value: dict[str, Any]) -> str:
    body = copy.deepcopy(value)
    body.pop("packet_id", None)
    return hashlib.sha256(_canonical_bytes(body)).hexdigest()


def _number(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}_invalid")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}_invalid") from None
    if not math.isfinite(number):
        raise ValueError(f"{label}_invalid")
    return number


def _optional_number(value: object, label: str) -> float | None:
    return None if value is None else _number(value, label)


def _integer(value: object, label: str) -> int:
    number = _number(value, label)
    if not number.is_integer() or number < 0:
        raise ValueError(f"{label}_invalid")
    return int(number)


def _timestamp(value: object, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError(f"{label}_invalid") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{label}_timezone_required")
    return parsed.astimezone(SH_TZ)


def _valid_sha(value: object) -> bool:
    text = str(value or "").lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _protocol() -> dict[str, Any]:
    try:
        raw = PROTOCOL_PATH.read_bytes()
        protocol = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"account_v2_protocol_unreadable:{type(exc).__name__}") from None
    if hashlib.sha256(raw).hexdigest() != PROTOCOL_SHA256:
        raise ValueError("account_v2_protocol_sha256_mismatch")
    if protocol.get("status") != "frozen_before_successor_account_packet_implementation":
        raise ValueError("account_v2_protocol_status_invalid")
    for name, binding in {
        "predecessor": protocol.get("predecessor") or {},
        **(protocol.get("bindings") or {}),
    }.items():
        path = (PROJECT_ROOT / str(binding.get("path") or "")).resolve()
        try:
            path.relative_to(PROJECT_ROOT.resolve())
        except ValueError:
            raise ValueError(f"account_v2_binding_outside_project:{name}") from None
        if not path.is_file() or _sha(path) != binding.get("sha256"):
            raise ValueError(f"account_v2_binding_invalid:{name}")
    return protocol


def _slot(observed: datetime, protocol: dict[str, Any]) -> str | None:
    current = observed.timetz().replace(tzinfo=None)
    for row in protocol.get("capture_slots") or []:
        start = time.fromisoformat(str(row.get("capture_from")))
        end = time.fromisoformat(str(row.get("capture_until")))
        if start <= current <= end:
            return str(row.get("id"))
    return None


def _sellable_time(value: object, observed: datetime, label: str) -> str:
    text = str(value or "")
    if len(text) == 10:
        try:
            day = datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError(f"{label}_invalid") from None
        if day != observed.date():
            raise ValueError(f"{label}_not_current")
        return text
    parsed = _timestamp(value, label)
    if parsed > observed or parsed.date() != observed.date():
        raise ValueError(f"{label}_not_current")
    return parsed.isoformat(timespec="seconds")


def _position(
    raw: object, *, observed: datetime, maximum_quote_age_seconds: int,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("position_object_required")
    identifier = str(raw.get("id") or "")
    code = str(raw.get("code") or "")
    if not identifier or not code:
        raise ValueError("position_identity_invalid")
    shares = _number(raw.get("shares"), f"position_{code}_shares")
    sellable = _number(
        raw.get("sellable_shares_snapshot"), f"position_{code}_sellable_shares",
    )
    if not shares.is_integer() or shares <= 0 or not sellable.is_integer() or not 0 <= sellable <= shares:
        raise ValueError(f"position_{code}_inventory_invalid")
    quote_at = _timestamp(raw.get("quote_as_of"), f"position_{code}_quote_source_as_of")
    quote_age = (observed - quote_at).total_seconds()
    if (
        quote_at.date() != observed.date() or quote_age < 0
        or quote_age > maximum_quote_age_seconds
    ):
        raise ValueError(f"position_{code}_quote_not_current")
    price = _number(raw.get("current_price"), f"position_{code}_current_price")
    market_value = _number(raw.get("market_value"), f"position_{code}_market_value")
    if price < 0 or market_value < 0 or abs(market_value - shares * price) > 0.01:
        raise ValueError(f"position_{code}_valuation_invalid")
    entry_price = _number(raw.get("entry_price"), f"position_{code}_entry_price")
    buy_cost = _number(
        raw.get("remaining_buy_cost_yuan"), f"position_{code}_remaining_buy_cost",
    )
    exit_cost = _number(
        raw.get("estimated_exit_cost_yuan"), f"position_{code}_estimated_exit_cost",
    )
    if entry_price < 0 or buy_cost < 0 or exit_cost < 0 or not raw.get("entry_date"):
        raise ValueError(f"position_{code}_cost_basis_invalid")
    sellable_at = _sellable_time(
        raw.get("sellable_as_of"), observed, f"position_{code}_sellable_source_as_of",
    )
    pnl_semantics = str(raw.get("pnl_semantics") or "")
    if not pnl_semantics:
        raise ValueError(f"position_{code}_pnl_semantics_missing")
    return {
        "id": identifier,
        "code": code,
        "name": str(raw.get("name") or ""),
        "shares": int(shares),
        "sellable_shares_snapshot": int(sellable),
        "sellable_as_of": sellable_at,
        "sellable_source_as_of": sellable_at,
        "entry_price": entry_price,
        "entry_date": str(raw["entry_date"]),
        "current_price": price,
        "quote_as_of": quote_at.isoformat(timespec="seconds"),
        "quote_source_as_of": quote_at.isoformat(timespec="seconds"),
        "market_value": market_value,
        "remaining_buy_cost_yuan": buy_cost,
        "estimated_exit_cost_yuan": exit_cost,
        "unrealized_pnl": _optional_number(
            raw.get("unrealized_pnl"), f"position_{code}_unrealized_pnl",
        ),
        "pnl_semantics": pnl_semantics,
    }


def build_system_route_account_packet_v2(
    portfolio: dict[str, Any], *, observed_at: datetime,
) -> dict[str, Any]:
    """Build one same-slot observation without rewriting source timestamps."""
    if not isinstance(portfolio, dict):
        raise ValueError("account_portfolio_object_required")
    protocol = _protocol()
    if observed_at.tzinfo is None:
        raise ValueError("observed_at_timezone_required")
    observed = observed_at.astimezone(SH_TZ).replace(microsecond=0)
    if observed.date().isoformat() < str(protocol.get("forward_start") or ""):
        raise ValueError("before_forward_start")
    slot_id = _slot(observed, protocol)
    if slot_id is None:
        raise ValueError("observed_at_outside_capture_slot")
    if (
        portfolio.get("cash_user_recorded") is not True
        or portfolio.get("cash_source") != "user_recorded"
        or portfolio.get("valuation_status") != "complete"
        or portfolio.get("fee_source_status") != "complete"
        or _number(portfolio.get("quote_coverage_pct"), "quote_coverage_pct") != 100.0
    ):
        raise ValueError("account_completion_status_invalid")
    ledger = portfolio.get("ledger_identity")
    if not isinstance(ledger, dict) or not _valid_sha(ledger.get("combined_sha256")):
        raise ValueError("account_ledger_identity_invalid")
    cash = _number(portfolio.get("available_cash"), "available_cash")
    if cash < 0:
        raise ValueError("available_cash_negative")
    cash_at = _timestamp(portfolio.get("cash_as_of"), "cash_source_as_of")
    if cash_at > observed:
        raise ValueError("cash_source_as_of_future")
    raw_positions = portfolio.get("positions")
    if not isinstance(raw_positions, list):
        raise ValueError("account_positions_invalid")
    maximum_age = int((protocol.get("time_contract") or {})["maximum_quote_age_seconds"])
    positions = sorted(
        (_position(row, observed=observed, maximum_quote_age_seconds=maximum_age)
         for row in raw_positions),
        key=lambda row: (row["code"], row["id"]),
    )
    ids = [row["id"] for row in positions]
    if len(ids) != len(set(ids)):
        raise ValueError("account_position_id_conflict")
    if _integer(portfolio.get("position_count"), "position_count") != len(positions):
        raise ValueError("account_position_count_mismatch")
    market_value = _number(portfolio.get("market_value"), "market_value")
    equity = _number(portfolio.get("account_equity"), "account_equity")
    if (
        min(market_value, equity) < 0
        or abs(market_value - sum(row["market_value"] for row in positions)) > 0.01
        or abs(equity - cash - market_value) > 0.01
    ):
        raise ValueError("account_totals_do_not_reconcile")

    source_identity = build_account_snapshot_identity(portfolio)
    source_sha = source_identity.get("immutable_account_snapshot_sha256")
    if source_identity.get("account_snapshot_status") != "ready" or not _valid_sha(source_sha):
        raise ValueError("account_source_identity_invalid")
    supplied_sha = portfolio.get("immutable_account_snapshot_sha256")
    if supplied_sha is not None and supplied_sha != source_sha:
        raise ValueError("account_source_identity_conflict")

    canonical_portfolio = {
        "cash_user_recorded": True,
        "available_cash": cash,
        "cash_source": "user_recorded",
        "cash_as_of": cash_at.isoformat(timespec="seconds"),
        "cash_source_as_of": cash_at.isoformat(timespec="seconds"),
        "ledger_identity": copy.deepcopy(ledger),
        "positions": positions,
        "position_count": len(positions),
        "closed_count": _integer(portfolio.get("closed_count", 0), "closed_count"),
        "market_value": market_value,
        "account_equity": equity,
        "cost_value": _optional_number(portfolio.get("cost_value"), "cost_value"),
        "realized_pnl": _optional_number(portfolio.get("realized_pnl"), "realized_pnl"),
        "unrealized_pnl": _optional_number(portfolio.get("unrealized_pnl"), "unrealized_pnl"),
        "remaining_buy_cost_yuan": _optional_number(
            portfolio.get("remaining_buy_cost_yuan"), "remaining_buy_cost_yuan",
        ),
        "estimated_exit_cost_yuan": _optional_number(
            portfolio.get("estimated_exit_cost_yuan"), "estimated_exit_cost_yuan",
        ),
        "valuation_status": "complete",
        "quote_coverage_pct": 100.0,
        "fee_source_status": "complete",
        "pnl_semantics": portfolio.get("pnl_semantics"),
        "source": portfolio.get("source"),
        "immutable_account_snapshot_sha256": source_sha,
    }
    canonical_identity = build_account_snapshot_identity(canonical_portfolio)
    if canonical_identity.get("immutable_account_snapshot_sha256") != source_sha:
        raise ValueError("account_source_identity_not_reproducible")
    packet = {
        "schema": "system_route_account_packet_v2.v1",
        "decision_day": observed.date().isoformat(),
        "slot_id": slot_id,
        "observed_at": observed.isoformat(timespec="seconds"),
        "first_seen_at": observed.isoformat(timespec="seconds"),
        "no_backfill": True,
        "protocol": {
            "path": str(PROTOCOL_PATH.relative_to(PROJECT_ROOT)),
            "sha256": PROTOCOL_SHA256,
        },
        "source_account_identity_sha256": source_sha,
        "portfolio": canonical_portfolio,
        "returns_or_labels_read": False,
        "execution_performed": False,
        "performance_computed": False,
        "authority": _authority(),
    }
    packet["packet_id"] = _identity(packet)
    return packet


def verify_system_route_account_packet_v2(
    value: dict[str, Any] | Path, *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Rebuild the packet from its bound source facts and require exact equality."""
    try:
        if isinstance(value, Path):
            resolved = research_artifact_path(value, project_root=project_root)
            packet = json.loads(resolved.read_text(encoding="utf-8"))
        else:
            packet = copy.deepcopy(value)
        if not isinstance(packet, dict):
            raise ValueError("account_packet_object_required")
        if packet.get("schema") != "system_route_account_packet_v2.v1":
            raise ValueError("account_packet_schema_invalid")
        if packet.get("protocol") != {
            "path": str(PROTOCOL_PATH.relative_to(PROJECT_ROOT)),
            "sha256": PROTOCOL_SHA256,
        }:
            raise ValueError("account_packet_protocol_invalid")
        observed = _timestamp(packet.get("observed_at"), "observed_at")
        if packet.get("first_seen_at") != observed.isoformat(timespec="seconds"):
            raise ValueError("account_packet_first_seen_mismatch")
        expected = build_system_route_account_packet_v2(
            packet.get("portfolio"), observed_at=observed,
        )
        if expected != packet:
            raise ValueError("account_packet_reproduction_mismatch")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [str(exc)]}
    return {
        "valid": True, "errors": [], "packet": packet,
        "packet_id": packet["packet_id"],
    }


__all__ = [
    "PROTOCOL_PATH", "PROTOCOL_SHA256",
    "build_system_route_account_packet_v2",
    "verify_system_route_account_packet_v2",
]
