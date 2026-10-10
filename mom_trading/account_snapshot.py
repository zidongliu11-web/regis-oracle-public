"""Deterministic identity for one user-recorded account state.

The hash binds only decision-relevant facts.  It deliberately excludes the
HTTP request time so the same cash, holdings, quotes and fee semantics produce
the same identity across page refreshes.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from typing import Any


SCHEMA = "manual_account_snapshot.v1"


def _number(value: object) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean_is_not_account_number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non_finite_account_number")
    return int(number) if number.is_integer() else number


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _sellable_as_of_is_current(
    value: object, reference: datetime | None,
) -> bool:
    if reference is None or not isinstance(value, str) or not value.strip():
        return False
    raw = value.strip()
    if len(raw) == 10:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date() == reference.date()
        except ValueError:
            return False
    parsed = _timestamp(raw)
    return bool(
        parsed is not None
        and parsed <= reference
        and parsed.date() == reference.date()
    )


def _sellable_inventory_missing_codes(
    positions: list[object], reference_as_of: datetime | None,
) -> list[str]:
    missing: list[str] = []
    for value in positions:
        row = value if isinstance(value, dict) else {}
        code = str(row.get("code") or "")
        try:
            shares = _number(row.get("shares"))
            sellable = _number(row.get("sellable_shares_snapshot"))
        except (TypeError, ValueError, OverflowError):
            shares = sellable = None
        valid = bool(
            reference_as_of is not None
            and isinstance(shares, int) and shares >= 0
            and isinstance(sellable, int) and 0 <= sellable <= shares
            and _sellable_as_of_is_current(
                row.get("sellable_as_of"), reference_as_of,
            )
        )
        if not valid:
            missing.append(code)
    return sorted(set(missing))


def _position_fact(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "code": str(row.get("code") or ""),
        "name": str(row.get("name") or ""),
        "shares": _number(row.get("shares")),
        "sellable_shares_snapshot": _number(
            row.get("sellable_shares_snapshot")
        ),
        "sellable_as_of": row.get("sellable_as_of"),
        "entry_price": _number(row.get("entry_price")),
        "entry_date": row.get("entry_date"),
        "current_price": _number(row.get("current_price")),
        "quote_as_of": row.get("quote_as_of"),
        "market_value": _number(row.get("market_value")),
        "remaining_buy_cost_yuan": _number(row.get("remaining_buy_cost_yuan")),
        "estimated_exit_cost_yuan": _number(row.get("estimated_exit_cost_yuan")),
        "unrealized_pnl": _number(row.get("unrealized_pnl")),
        "pnl_semantics": row.get("pnl_semantics"),
    }


def build_account_snapshot_identity(portfolio: dict[str, Any]) -> dict[str, Any]:
    """Return a stable SHA only when real cash and valuation are complete."""
    missing: list[str] = []
    if portfolio.get("cash_user_recorded") is not True:
        missing.append("user_recorded_cash")
    if portfolio.get("valuation_status") != "complete":
        missing.append("complete_portfolio_valuation")
    ledger_identity = portfolio.get("ledger_identity")
    if not isinstance(ledger_identity, dict) or not ledger_identity.get(
        "combined_sha256"
    ):
        missing.append("account_ledger_identity")
    positions = portfolio.get("positions")
    if not isinstance(positions, list):
        missing.append("portfolio_positions")
    cash_as_of = portfolio.get("cash_as_of")
    reference_times = [
        parsed for value in [
            cash_as_of,
            *(row.get("quote_as_of") for row in positions if isinstance(row, dict)),
        ] if (parsed := _timestamp(value)) is not None
    ] if isinstance(positions, list) else []
    decision_as_of = _timestamp(portfolio.get("decision_as_of"))
    reference_as_of = max(reference_times, default=None)
    if decision_as_of is not None:
        reference_as_of = decision_as_of
    sellable_missing = (
        _sellable_inventory_missing_codes(positions, reference_as_of)
        if isinstance(positions, list) else []
    )
    sellable_status = (
        "complete"
        if isinstance(positions, list) and not sellable_missing
        else "incomplete"
    )
    if sellable_status != "complete":
        missing.append("complete_sellable_inventory")
    if missing:
        return {
            "account_snapshot_schema": SCHEMA,
            "account_snapshot_status": "incomplete",
            "account_snapshot_as_of": None,
            "immutable_account_snapshot_sha256": None,
            "account_snapshot_missing_inputs": missing,
            "sellable_inventory_status": sellable_status,
            "sellable_inventory_missing_codes": sellable_missing,
        }

    try:
        position_facts = sorted(
            (_position_fact(row) for row in positions if isinstance(row, dict)),
            key=lambda row: (row["code"], str(row["id"])),
        )
        if len(position_facts) != len(positions):
            raise ValueError("position_not_object")
        snapshot_as_of = max(
            ([str(cash_as_of)] if cash_as_of else []) + [
                str(row["quote_as_of"])
                for row in position_facts if row.get("quote_as_of")
            ],
            default=None,
        )
        canonical = {
            "schema": SCHEMA,
            "snapshot_as_of": snapshot_as_of,
            "decision_date": reference_as_of.date().isoformat(),
            "cash": {
                "available_cash": _number(portfolio.get("available_cash")),
                "source": portfolio.get("cash_source"),
                "as_of": cash_as_of,
            },
            "positions": position_facts,
            "totals": {
                "position_count": int(portfolio.get("position_count") or 0),
                "closed_count": int(portfolio.get("closed_count") or 0),
                "market_value": _number(portfolio.get("market_value")),
                "cost_value": _number(portfolio.get("cost_value")),
                "account_equity": _number(portfolio.get("account_equity")),
                "realized_pnl": _number(portfolio.get("realized_pnl")),
                "unrealized_pnl": _number(portfolio.get("unrealized_pnl")),
                "remaining_buy_cost_yuan": _number(
                    portfolio.get("remaining_buy_cost_yuan")
                ),
                "estimated_exit_cost_yuan": _number(
                    portfolio.get("estimated_exit_cost_yuan")
                ),
            },
            "semantics": {
                "source": portfolio.get("source"),
                "valuation_status": portfolio.get("valuation_status"),
                "quote_coverage_pct": _number(portfolio.get("quote_coverage_pct")),
                "fee_source_status": portfolio.get("fee_source_status"),
                "pnl_semantics": portfolio.get("pnl_semantics"),
                "sellable_inventory_status": sellable_status,
            },
            "ledger_identity": ledger_identity,
        }
        payload = json.dumps(
            canonical, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        return {
            "account_snapshot_schema": SCHEMA,
            "account_snapshot_status": "invalid",
            "account_snapshot_as_of": None,
            "immutable_account_snapshot_sha256": None,
            "account_snapshot_missing_inputs": [f"canonicalization:{exc}"],
            "sellable_inventory_status": sellable_status,
            "sellable_inventory_missing_codes": sellable_missing,
        }
    return {
        "account_snapshot_schema": SCHEMA,
        "account_snapshot_status": "ready",
        "account_snapshot_as_of": snapshot_as_of,
        "immutable_account_snapshot_sha256": hashlib.sha256(payload).hexdigest(),
        "account_snapshot_missing_inputs": [],
        "sellable_inventory_status": sellable_status,
        "sellable_inventory_missing_codes": sellable_missing,
    }


__all__ = ["SCHEMA", "build_account_snapshot_identity"]
