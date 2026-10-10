"""Normalize persisted manual account facts for the strict V2 packet contract."""

from __future__ import annotations

import copy
import math
from typing import Any

from .account_snapshot import build_account_snapshot_identity


_USER_CASH_SOURCES = {
    "manual_snapshot",
    "manual_position_snapshot",
    "user_recorded",
}
_COMPLETE_FEE_SOURCE_DETAILS = {"actual", "estimated_or_mixed", "complete"}


def _nonnegative_finite(value: object) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number >= 0


def _position_costs_complete(row: object) -> bool:
    return bool(
        isinstance(row, dict)
        and _nonnegative_finite(row.get("remaining_buy_cost_yuan"))
        and _nonnegative_finite(row.get("estimated_exit_cost_yuan"))
        and isinstance(row.get("pnl_semantics"), str)
        and row["pnl_semantics"].strip()
    )


def normalize_system_route_account_input(portfolio: dict[str, Any]) -> dict[str, Any]:
    """Map storage provenance labels to V2 contract labels without filling facts."""
    if not isinstance(portfolio, dict):
        raise ValueError("account_portfolio_object_required")
    normalized = copy.deepcopy(portfolio)

    cash_source = normalized.get("cash_source")
    if (
        normalized.get("cash_user_recorded") is True
        and cash_source in _USER_CASH_SOURCES
    ):
        normalized["cash_input_method"] = cash_source
        normalized["cash_source"] = "user_recorded"

    positions = normalized.get("positions")
    if isinstance(positions, list):
        for row in positions:
            if isinstance(row, dict) and row.get("id") is not None:
                # The frozen packet canonicalizes position identifiers as
                # strings; normalize before hashing so the same live row is
                # reproducible after packet construction.
                row["id"] = str(row["id"])
    fee_detail = normalized.get("fee_source_status")
    fees_complete = bool(
        isinstance(positions, list)
        and (
            (not positions and fee_detail in {
                *_COMPLETE_FEE_SOURCE_DETAILS, "unavailable",
            })
            or (
                bool(positions)
                and fee_detail in _COMPLETE_FEE_SOURCE_DETAILS
                and all(_position_costs_complete(row) for row in positions)
            )
        )
    )
    if fees_complete:
        normalized["fee_source_detail"] = fee_detail
        normalized["fee_source_status"] = "complete"

    identity = build_account_snapshot_identity(normalized)
    normalized["immutable_account_snapshot_sha256"] = identity.get(
        "immutable_account_snapshot_sha256"
    )
    return normalized


__all__ = ["normalize_system_route_account_input"]
