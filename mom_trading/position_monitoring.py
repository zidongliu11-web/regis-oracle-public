"""Small identity helpers for per-lot position monitoring."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def positions_by_code(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("code") or ""), []).append(row)
    return grouped


def risk_actions_by_position_id(
    rows: list[dict], quotes: dict[str, Any], *,
    finite_number: Callable[[object], float | None],
    compute_actions: Callable[..., list[dict[str, Any]]],
) -> dict[int, dict[str, Any]]:
    inputs = []
    for row in rows:
        quote = quotes.get(str(row.get("code") or ""))
        current = finite_number(getattr(quote, "price", None))
        inputs.append({
            "id": row.get("id"), "code": row.get("code"),
            "name": row.get("name") or row.get("code"),
            "entry_price": float(row.get("price") or 0),
            "current_price": current if current is not None and current > 0 else None,
            "entry_date": row.get("created_at"),
        })
    return {
        int(action["position_id"]): action
        for action in compute_actions(inputs)
        if action.get("position_id") is not None
    }


__all__ = ["positions_by_code", "risk_actions_by_position_id"]
