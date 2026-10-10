from __future__ import annotations

import pytest

from quantqc import ashare
from quantqc import costs


def test_risk_warning_limits_are_board_and_date_aware() -> None:
    assert ashare.price_limit_rule(
        "600001", trade_date="2026-07-03", is_risk_warning=True,
        no_price_limit=False,
    )["rate"] == pytest.approx(0.05)
    assert ashare.price_limit_rule(
        "600001", trade_date="2026-07-06", is_risk_warning=True,
        no_price_limit=False,
    )["rate"] == pytest.approx(0.10)
    assert ashare.price_limit_rule(
        "300001", trade_date="2025-01-02", is_risk_warning=True,
        no_price_limit=False,
    )["rate"] == pytest.approx(0.20)


def test_special_listing_identity_is_required_and_no_limit_is_explicit() -> None:
    missing = ashare.price_limit_rule(
        "600001", trade_date="2026-08-31", is_risk_warning=False,
        no_price_limit=None,
    )
    assert missing == {
        "valid": False,
        "status": "listing_or_special_event_identity_missing",
        "rate": None,
    }
    no_limit = ashare.price_limit_rule(
        "600001", trade_date="2026-08-31", is_risk_warning=False,
        no_price_limit=True,
    )
    assert no_limit["valid"] is True
    assert no_limit["rate"] is None


def test_opening_auction_uses_exact_tick_limit_and_fails_closed() -> None:
    below_limit = ashare.opening_auction_executability(
        code="600001", trade_date="2026-08-31", side="buy",
        open_price=10.99, prev_close=10.0, tradable=True,
        is_risk_warning=False, no_price_limit=False,
    )
    at_limit = ashare.opening_auction_executability(
        code="600001", trade_date="2026-08-31", side="buy",
        open_price=11.0, prev_close=10.0, tradable=True,
        is_risk_warning=False, no_price_limit=False,
    )
    missing = ashare.opening_auction_executability(
        code="600001", trade_date="2026-08-31", side="buy",
        open_price=None, prev_close=10.0, tradable=True,
        is_risk_warning=False, no_price_limit=False,
    )
    assert below_limit["executable"] is True
    assert at_limit["executable"] is False
    assert at_limit["limit_price"] == pytest.approx(11.0)
    assert missing["executable"] is False


def test_share_contract_never_creates_fractional_or_split_odd_lots() -> None:
    assert ashare.constrain_share_quantity(255.9, side="buy")["accepted_shares"] == 200
    assert ashare.constrain_share_quantity(
        157, side="sell", available_to_sell=157, full_liquidation=True,
    )["accepted_shares"] == 157
    blocked = ashare.constrain_share_quantity(
        157, side="sell", available_to_sell=157, full_liquidation=True,
        liquidity_cap_shares=120,
    )
    assert blocked["accepted_shares"] == 0
    assert blocked["reason"] == "odd_lot_cannot_be_split_by_liquidity_cap"


def test_continuous_auction_price_cage_and_cost_identity() -> None:
    assert ashare.continuous_auction_price_cage(
        side="buy", reference_price=10.0, order_price=10.20,
    )["accepted"] is True
    assert ashare.continuous_auction_price_cage(
        side="buy", reference_price=10.0, order_price=10.21,
    )["accepted"] is False
    breakdown = costs.one_side_cost_breakdown(20_000, "buy", 0.0)
    assert breakdown["broker_commission_includes_regulatory_fees"] is True
    assert breakdown["commission_calibration"] == (
        "default_assumption_needs_user_broker_statement"
    )
    assert breakdown["total"] == pytest.approx(
        breakdown["commission"] + breakdown["transfer"]
    )
