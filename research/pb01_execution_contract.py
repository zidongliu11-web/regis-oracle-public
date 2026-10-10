"""Validate proposed execution evidence; this neither fills orders nor replays money."""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from datetime import timedelta, timezone, time
from research.pb01_pipeline import aware, reference, BOUNDARY


def positive(value, *, zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (not zero and value == 0):
        raise ValueError("number_invalid")
    return value


def validate_execution_event(root, event, *, sessions):
    fields = {"event_id", "instrument_id", "book", "side", "decision_at",
              "human_received_at", "human_submitted_at", "execution_at", "quote",
              "requested_qty", "executed_qty", "execution_price", "outcome",
              "position_qty_before", "last_buy_session", "evidence"}
    if not isinstance(event, dict) or set(event) != fields:
        raise ValueError("event_shape_invalid")
    if any(not isinstance(event[k], str) or not event[k] for k in ("event_id", "instrument_id")):
        raise ValueError("event_identity_missing")
    if event["book"] not in {"etf", "strategy"} or event["side"] not in {"buy", "sell"}:
        raise ValueError("event_book_or_side_invalid")
    if not isinstance(sessions, list) or not sessions or sessions != sorted(set(sessions)):
        raise ValueError("session_calendar_invalid")
    refs = [reference(root, r) for r in event["evidence"]] if isinstance(event["evidence"], list) else []
    if not refs:
        raise ValueError("execution_evidence_missing")
    decision, received, submitted = [aware(event[k]) for k in ("decision_at", "human_received_at", "human_submitted_at")]
    execution = aware(event["execution_at"])
    if not decision <= received <= submitted <= execution:
        raise ValueError("human_clock_order_invalid")
    # All clocks may use any explicit offset, but execution windows use Shanghai.
    shanghai = timezone(timedelta(hours=8))
    local = execution.astimezone(shanghai)
    day = local.date().isoformat()
    if day not in sessions:
        raise ValueError("execution_outside_calendar")
    low, high = (time(9, 35), time(9, 38)) if event["side"] == "buy" else (time(14, 55), time(14, 57))
    if not low <= local.time() < high:
        raise ValueError("execution_outside_frozen_window")
    if event["side"] == "buy":
        decision_local = decision.astimezone(shanghai)
        index = sessions.index(day)
        if index == 0 or decision_local.date().isoformat() != sessions[index-1] or decision_local.time() < time(15):
            raise ValueError("entry_not_after_previous_completed_session")
    requested, filled, before = [positive(event[k], zero=(k != "requested_qty")) for k in ("requested_qty", "executed_qty", "position_qty_before")]
    if any(type(event[k]) is not int for k in ("requested_qty", "executed_qty", "position_qty_before")):
        raise ValueError("quantity_not_integer")
    if filled > requested:
        raise ValueError("fill_exceeds_request")
    if event["side"] == "buy" and (requested % 100 or filled % 100):
        raise ValueError("buy_lot_invalid")
    if event["side"] == "sell":
        last = event["last_buy_session"]
        if requested > before or filled > before or (requested % 100 and requested != before) or last not in sessions or sessions.index(last) >= sessions.index(day):
            raise ValueError("sell_inventory_or_T1_invalid")
    elif event["last_buy_session"] is not None:
        raise ValueError("buy_prior_session_field_invalid")
    if event["outcome"] not in {"filled", "partial", "rejected", "no_fill"}:
        raise ValueError("execution_outcome_invalid")
    if ((event["outcome"] == "filled" and filled != requested)
            or (event["outcome"] == "partial" and not 0 < filled < requested)
            or (event["outcome"] in {"rejected", "no_fill"} and filled != 0)):
        raise ValueError("outcome_quantity_conflict")
    quote = event["quote"]
    qfields = {"observed_at", "bid", "bid_qty", "ask", "ask_qty", "source_ref"}
    if not isinstance(quote, dict) or set(quote) != qfields:
        raise ValueError("quote_shape_invalid")
    quote_ref = reference(root, quote["source_ref"])
    observed = aware(quote["observed_at"])
    if not submitted <= observed <= execution or execution - observed > timedelta(seconds=60):
        raise ValueError("quote_clock_stale_or_future")
    bid, ask = positive(quote["bid"]), positive(quote["ask"])
    bid_qty, ask_qty = positive(quote["bid_qty"], zero=True), positive(quote["ask_qty"], zero=True)
    if bid > ask or type(quote["bid_qty"]) is not int or type(quote["ask_qty"]) is not int:
        raise ValueError("quote_crossed_or_quantity_invalid")
    if filled:
        price = positive(event["execution_price"])
        displayed = ask_qty if event["side"] == "buy" else bid_qty
        if displayed < filled:
            raise ValueError("fill_exceeds_observed_side_size")
        if (event["side"] == "buy" and price < ask) or (event["side"] == "sell" and price > bid):
            raise ValueError("price_improves_quote_without_separate_evidence")
    elif event["execution_price"] is not None:
        raise ValueError("unfilled_execution_price_must_be_null")
    residual = before + filled if event["side"] == "buy" else before - filled
    return {"event_id": event["event_id"], "candidate_structure_valid": True,
            "residual_position_qty": residual, "evidence_references": refs,
            "quote_reference": quote_ref, "unfilled_qty": requested-filled,
            "independent_execution_review_required": True,
            "touch_price_is_fill": False, "ledger_replayed": False, **BOUNDARY}


def validate_cash_action(root, action, *, held_quantity, known_at, actual_credit=None):
    """Keep announced entitlement, receivable, and actually credited cash separate."""
    fields = {"action_id", "instrument_id", "record_at", "ex_at", "payable_on",
              "announced_at", "cash_per_unit_CNY", "quantity_unit", "evidence"}
    if not isinstance(action, dict) or set(action) != fields:
        raise ValueError("action_shape_invalid")
    if any(not isinstance(action[k], str) or not action[k] for k in ("action_id", "instrument_id")):
        raise ValueError("action_identity_missing")
    if action["quantity_unit"] not in {"stock_share", "fund_unit"}:
        raise ValueError("action_unit_unknown")
    if type(held_quantity) is not int or held_quantity < 0:
        raise ValueError("record_quantity_invalid")
    announced, record, ex = [aware(action[k]) for k in ("announced_at", "record_at", "ex_at")]
    if announced > aware(known_at) or announced > record or record >= ex:
        raise ValueError("action_publication_or_record_clock_invalid")
    from datetime import datetime
    payable = action["payable_on"]
    shanghai = timezone(timedelta(hours=8))
    if not isinstance(payable, str) or datetime.strptime(payable, "%Y-%m-%d").strftime("%Y-%m-%d") != payable or payable < ex.astimezone(shanghai).date().isoformat():
        raise ValueError("action_payable_date_invalid")
    # Exact decimal text avoids silently rounding binary floats or guessing scale.
    if not isinstance(action["cash_per_unit_CNY"], str):
        raise ValueError("action_amount_must_be_decimal_text")
    try:
        amount = Decimal(action["cash_per_unit_CNY"])
    except InvalidOperation:
        raise ValueError("action_amount_invalid") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("action_amount_invalid")
    refs = [reference(root, ref) for ref in action["evidence"]] if isinstance(action["evidence"], list) else []
    if not refs:
        raise ValueError("action_evidence_missing")
    credited = None
    if actual_credit is not None:
        if not isinstance(actual_credit, dict) or set(actual_credit) != {"action_id", "instrument_id", "credited_at", "amount_CNY", "evidence"}:
            raise ValueError("credit_shape_invalid")
        at = aware(actual_credit["credited_at"])
        if (actual_credit["action_id"] != action["action_id"] or actual_credit["instrument_id"] != action["instrument_id"]
                or at < ex or at > aware(known_at) or at.astimezone(shanghai).date().isoformat() < payable):
            raise ValueError("credit_identity_or_clock_invalid")
        if not isinstance(actual_credit["amount_CNY"], str):
            raise ValueError("credit_amount_must_be_decimal_text")
        try:
            credited = Decimal(actual_credit["amount_CNY"])
        except InvalidOperation:
            raise ValueError("credit_amount_invalid") from None
        if not credited.is_finite() or credited < 0:
            raise ValueError("credit_amount_invalid")
        credit_refs = [reference(root, ref) for ref in actual_credit["evidence"]] if isinstance(actual_credit["evidence"], list) else []
        if not credit_refs:
            raise ValueError("credit_evidence_missing")
        refs += credit_refs
    # Gross declared terms need not equal net credited cash (tax and corrections).
    # Do not invent net cash or imply possession of a brokerage cash statement.
    return {"action_id": action["action_id"], "candidate_structure_valid": True,
            "declared_gross_entitlement_CNY": str(amount * held_quantity),
            "actual_credit_supplied": credited is not None,
            "credit_is_announcement_or_payable_date": False,
            "cash_available_for_orders_verified": False,
            "evidence_references": refs, "ledger_replayed": False, **BOUNDARY}
