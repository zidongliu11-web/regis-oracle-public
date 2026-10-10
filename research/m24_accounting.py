"""Three alternative 150k research accounts, currently restricted to fixtures.

This is an accounting core, not a fill simulator. Supplied execution prices must
already include their assumed slippage. There is no daily-open/09:35 equivalence,
real input admission or order transmission. The M23-bound replay is unchanged.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from quantqc.costs import COSTS, MIN_COMMISSION_YUAN
from research.target_a_confirmed_fill_replay import ReplayError, _date, _time, _number, _stock

CENT = Decimal("0.01")
CAPITAL = Decimal("150000")
ETF = "510300.SH"


def money(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _qty(value, label):
    if type(value) is not int or value <= 0:
        raise ReplayError(label + ": positive integer quantity required")
    return value


def _fees(arm, side, gross, order):
    # One minimum per order, charged on the first actual fill. Subsequent fills
    # charge only the incremental aggregate commission; failed orders cost zero.
    cumulative = order["gross"] + gross
    due = money(max(Decimal(str(MIN_COMMISSION_YUAN)),
                    cumulative * Decimal(str(COSTS["commission"]))))
    commission = due - order["commission"]
    stamp = money(gross * Decimal(str(COSTS["stamp"]))) if arm == "strategy" and side == "sell" else Decimal(0)
    transfer = money(gross * Decimal(str(COSTS["transfer"]))) if arm == "strategy" else Decimal(0)
    order["gross"], order["commission"] = cumulative, due
    return {"commission": commission, "stamp": stamp, "transfer": transfer}


def start_fixture_accounts(*, sessions, marks, action_coverage, horizon,
                           input_origin, declared_distributions=()):
    """One incremental book for three alternative synthetic accounts."""
    if input_origin != "synthetic_fixture":
        raise ReplayError("real economic input admission not implemented; synthetic_fixture required")
    if type(horizon) is not int or horizon not in {1, 3, 5}:
        raise ReplayError("one independent 1/3/5 horizon per run required")
    if not isinstance(sessions, list) or not sessions or sessions != sorted(set(sessions)):
        raise ReplayError("unique ordered sessions required")
    for value in sessions:
        _date(value, "session")
    if not isinstance(marks, dict) or not isinstance(action_coverage, dict):
        raise ReplayError("events, marks and coverage required")
    return {"sessions": sessions, "marks": marks, "coverage": action_coverage,
            "horizon": horizon, "input_origin": input_origin,
            "states": {arm: {"cash": CAPITAL, "lots": {}, "fees": Decimal(0),
                "receivable": {}, "record_positions": {}, "orders": {},
                "peak": CAPITAL, "max_drawdown": Decimal(0)}
                for arm in ("cash", "strategy", "etf")},
            "distributions": {}, "failed": [], "partial": [], "daily": [],
            "seen": set(), "last_at": None, "last_arm_at": {}, "closed_day": None,
            "event_day": None, "touched": {a: set() for a in ("cash", "strategy", "etf")},
            "declared_distributions": set(declared_distributions)}


def _apply_ledger_event(state, distributions, failed, partial, sessions, day, at, event):
    arm, kind = event["arm"], event.get("kind")
    if kind in {"fill", "unfilled_order", "order_intent", "cancel_request"}:
        known = state["orders"].get(event.get("order_id"))
        late_confirm = (kind == "unfilled_order" and event.get("confirmed") is True
                        and known is not None and known.get("registered_intent"))
        cancel_request = kind == "cancel_request" and known is not None and known.get("registered_intent")
        if not (late_confirm or cancel_request) and (day not in sessions or not ((9, 30) <= (at.hour, at.minute) < (11, 30)
                                       or (13, 0) <= (at.hour, at.minute) < (15, 0))):
            raise ReplayError("trade outside supplied trading session")
        code, side = event.get("code"), event.get("side")
        if not isinstance(code, str) or side not in {"buy", "sell"} or (arm == "etf" and code != ETF) or (
                arm == "strategy" and not _stock(code)):
            raise ReplayError("instrument or side outside arm trade scope")
        requested = _qty(event.get("requested_shares"), "requested shares")
        order_id = event.get("order_id")
        if not isinstance(order_id, str) or not order_id:
            raise ReplayError("order identity required")
        submitted = _time(event.get("submitted_at"), "submitted time")
        if submitted > at or (not (late_confirm or cancel_request) and submitted.date().isoformat() != day):
            raise ReplayError("order submitted after fill or wrong session")
        if side == "buy" and requested % 100:
            raise ReplayError("buy order requires 100-unit lot")
        order = state["orders"].get(order_id)
        identity = (code, side, requested, submitted)
        if order is None:
            if side == "sell":
                at_creation_sellable = sum(q for bought, q in state["lots"].get(code, [])
                                           if bought < day)
                if (requested > at_creation_sellable
                        or (requested % 100 and requested != at_creation_sellable)):
                    raise ReplayError("sell requested exceeds T+1 inventory or odd-lot liquidation")
            order = {"identity": identity, "requested": requested, "filled": 0,
                     "gross": Decimal(0), "commission": Decimal(0), "closed": False}
            state["orders"][order_id] = order
        elif order["identity"] != identity or order["closed"]:
            raise ReplayError("order identity changed or order already cancelled")
        if kind == "order_intent":
            if known is not None:
                raise ReplayError("duplicate order intent")
            order["registered_intent"] = True
            order["cancel_requested"] = False
            return
        if kind == "cancel_request":
            if not order.get("registered_intent") or order["filled"] == requested:
                raise ReplayError("cancel request without pending registered intent")
            order["cancel_requested"] = True
            return
        if kind == "unfilled_order" and order.get("registered_intent") and event.get("confirmed") is not True:
            raise ReplayError("registered intent requires confirmed unfilled closure")
        if kind == "unfilled_order":
            if not isinstance(event.get("reason"), str) or not event["reason"]:
                raise ReplayError("failed order reason required")
            if order["filled"] == requested:
                raise ReplayError("completed order cannot be unfilled")
            order["closed"] = True
            failed.append({"arm": arm, "order_id": order_id, "side": side,
                           "unfilled_shares": requested - order["filled"],
                           "reason": event["reason"], "at": at.isoformat()})
            return
        if event.get("price_basis") != "synthetic_execution_price_includes_slippage":
            raise ReplayError("execution price/slippage basis required")
        qty = _qty(event.get("shares"), "filled shares")
        if order["filled"] + qty > requested:
            raise ReplayError("partial fills exceed requested quantity")
        gross = money(_number(event.get("price"), "fill price", positive=True) * qty)
        fees = sum(_fees(arm, side, gross, order).values())
        lots = state["lots"].setdefault(code, [])
        if side == "buy":
            if gross + fees > state["cash"]:
                raise ReplayError("insufficient shared cash; receivable cannot fund orders")
            state["cash"] -= gross + fees
            lots.append((day, qty))
        else:
            sellable = sum(q for bought, q in lots if bought < day)
            if qty > sellable:
                raise ReplayError("T+1 or inventory violation")
            if fees > gross:
                raise ReplayError("fees exceed sale proceeds")
            remaining, new = qty, []
            for bought, q in lots:
                used = min(q, remaining) if bought < day else 0
                remaining -= used
                if q > used:
                    new.append((bought, q-used))
            state["lots"][code] = new
            state["cash"] += gross-fees
        state["fees"] += fees
        order["filled"] += qty
        if order["filled"] < requested:
            partial.append({"arm": arm, "order_id": order_id,
                            "filled_so_far": order["filled"], "requested_shares": requested})
    elif kind == "cash_dividend_entitlement":
        if arm != "etf" or event.get("code") != ETF:
            raise ReplayError("only ETF cash distributions supported")
        ident = event.get("dividend_id")
        record = event.get("record_date")
        _date(record, "dividend record date")
        if (not isinstance(ident, str) or not ident or ident in distributions
                or record >= day or record not in sessions or day not in sessions):
            raise ReplayError("distribution duplicate or record/ex date invalid")
        if _time(event.get("available_at"), "distribution availability") > at:
            raise ReplayError("future distribution terms")
        qty = state["record_positions"].get((record, ETF), 0)
        amount = money(_number(event.get("cash_per_unit_yuan"), "cash per unit", positive=True)*qty)
        state["receivable"][ident] = amount
        distributions[ident] = {"arm": arm, "at": at, "amount": amount,
                               "entitled_units": qty, "credited_at": None}
    elif kind == "cash_dividend_credit":
        ident = event.get("dividend_id")
        action = distributions.get(ident)
        if (arm != "etf" or action is None or action["credited_at"] is not None
                or action["at"] > at or action["entitled_units"] == 0):
            raise ReplayError("cash receipt duplicate, premature or without entitlement")
        amount = _number(event.get("amount_yuan"), "cash receipt", positive=True, cents=True)
        if amount != state["receivable"].get(ident):
            raise ReplayError("cash receipt amount mismatch")
        state["cash"] += amount
        del state["receivable"][ident]
        action["credited_at"] = at
    else:
        raise ReplayError("unsupported or revoked corporate action/event")

def apply_fixture_event(book, event):
    """Apply one chronological event atomically; no historical replay."""
    if not isinstance(event, dict) or event.get("arm") not in {"strategy", "etf"}:
        raise ReplayError("cash cannot trade; event arm invalid")
    ident = event.get("event_id")
    if not isinstance(ident, str) or not ident or ident in book["seen"]:
        raise ReplayError("event identity missing or duplicated")
    at = _time(event.get("at"), "event time")
    day = at.date().isoformat()
    if not book["sessions"][0] <= day <= book["sessions"][-1]:
        raise ReplayError("event outside replay calendar; leave future receivable pending")
    arm = event["arm"]
    if book["last_at"] is not None and at < book["last_at"]:
        raise ReplayError("incremental events must be chronological")
    if book["last_arm_at"].get(arm) == at:
        raise ReplayError("same-arm same-time events need unambiguous sequence")
    if book["closed_day"] is not None and day <= book["closed_day"]:
        raise ReplayError("event after its daily book was closed")
    if book["event_day"] not in {None, day}:
        raise ReplayError("previous event day must close before next day")
    original = book["states"][arm]
    state = dict(original)
    state["lots"] = dict(original["lots"])
    if event.get("code") in state["lots"]:
        state["lots"][event["code"]] = list(state["lots"][event["code"]])
    state["orders"] = dict(original["orders"])
    if event.get("order_id") in state["orders"]:
        state["orders"][event["order_id"]] = dict(state["orders"][event["order_id"]])
    state["receivable"] = dict(original["receivable"])
    distributions = {key: dict(value) for key, value in book["distributions"].items()}
    failed, partial = [], []
    _apply_ledger_event(state, distributions, failed, partial, book["sessions"], day, at, event)
    book["states"][arm] = state
    book["distributions"] = distributions
    book["failed"].extend(failed)
    book["partial"].extend(partial)
    book["seen"].add(ident)
    book["last_at"] = at
    book["last_arm_at"][arm] = at
    book["event_day"] = day
    if event.get("code"):
        book["touched"][arm].add(event["code"])


def close_fixture_day(book, day):
    """Mark one completed day after all supplied events, with explicit actions."""
    _date(day, "book day")
    sessions = book["sessions"]
    if (not sessions[0] <= day <= sessions[-1]
            or book["closed_day"] is not None and day <= book["closed_day"]
            or book["event_day"] not in {None, day}):
        raise ReplayError("daily book close ordering invalid")
    pending = [s for s in sessions if book["closed_day"] is None or s > book["closed_day"]]
    if pending and day > pending[0]:
        raise ReplayError("daily book cannot skip supplied session")
    snapshot = {"date": day, "is_trading_session": day in sessions, "accounts": {}}
    new_states = {}
    for arm, old in book["states"].items():
        state = dict(old)
        state["record_positions"] = dict(old["record_positions"])
        holdings = {code: sum(q for _, q in lots) for code, lots in state["lots"].items() if lots}
        if day in sessions:
            for code in set(holdings) | book["touched"][arm]:
                kinds = book["coverage"].get((day, code))
                if not isinstance(kinds, set) or not kinds <= {"cash_distribution"} or (arm == "strategy" and kinds):
                    raise ReplayError("corporate action coverage missing or unsupported")
                if ("cash_distribution" in kinds) != ((day, code) in book["declared_distributions"]):
                    raise ReplayError("cash distribution coverage and events disagree")
            state["record_positions"][(day, ETF)] = holdings.get(ETF, 0)
        mark_day = max(s for s in sessions if s <= day)
        equity = state["cash"] + sum(state["receivable"].values())
        for code, qty in holdings.items():
            mark = book["marks"].get((mark_day, code))
            if not isinstance(mark, dict) or mark.get("price_basis") != "raw":
                raise ReplayError("missing or adjusted close mark")
            equity += money(_number(mark.get("close"), "close mark", positive=True) * qty)
        state["peak"] = max(state["peak"], equity)
        state["max_drawdown"] = min(state["max_drawdown"], equity / state["peak"] - 1)
        snapshot["accounts"][arm] = {"cash_yuan": float(state["cash"]),
            "receivable_yuan": float(sum(state["receivable"].values())),
            "equity_yuan": float(equity), "holdings": holdings,
            "cumulative_fees_yuan": float(state["fees"]),
            "max_drawdown_from_initial_capital": float(state["max_drawdown"])}
        new_states[arm] = state
    book["states"] = new_states
    book["daily"].append(snapshot)
    book["closed_day"] = day
    book["event_day"] = None
    book["touched"] = {a: set() for a in ("cash", "strategy", "etf")}


def finish_fixture_accounts(book):
    if book["closed_day"] != book["sessions"][-1] or book["event_day"] is not None:
        raise ReplayError("fixture book not fully closed")
    return {"schema": "regis.m24.fixture_accounting.v1", "input_origin": book["input_origin"],
            "horizon": book["horizon"], "initial_yuan_per_arm": 150000,
            "capital_relationship": "alternative_accounts_never_sum",
            "daily": book["daily"], "unfilled_orders": book["failed"], "partial_fill_events": book["partial"],
            "open_order_remainders": [{"arm": arm, "order_id": ident,
                "remaining_shares": o["requested"] - o["filled"],
                "status": "unknown_future_fill_not_imputed"}
                for arm, state in book["states"].items() for ident, o in state["orders"].items()
                if not o["closed"] and o["filled"] < o["requested"]],
            "distributions": [{"dividend_id": ident, "entitled_units": a["entitled_units"],
                               "amount_yuan": float(a["amount"]),
                               "cash_credit_at": a["credited_at"].isoformat() if a["credited_at"] else None}
                              for ident, a in book["distributions"].items()],
            "fee_assumptions": "existing stock rates; ETF zero stamp/transfer assumption; per-order minimum on first fill; prices include slippage; not broker costs",
            "authority": {"economic_run_enabled": False, "model_admitted": False,
                          "orders_enabled": False, "actual_fill_evidence": False}}


def replay_fixture_accounts(*, sessions, marks, events, action_coverage, horizon, input_origin):
    """Legacy batch wrapper uses the same incremental ledger and output schema."""
    if not isinstance(events, list):
        raise ReplayError("events, marks and coverage required")
    parsed, seen = [], set()
    for event in events:
        if not isinstance(event, dict) or event.get("arm") not in {"strategy", "etf"}:
            raise ReplayError("cash cannot trade; event arm invalid")
        ident = event.get("event_id")
        if not isinstance(ident, str) or not ident or ident in seen:
            raise ReplayError("event identity missing or duplicated")
        seen.add(ident)
        parsed.append((_time(event.get("at"), "event time"), event))
    parsed.sort(key=lambda x: x[0])
    if len({(at, e["arm"]) for at, e in parsed}) != len(parsed):
        raise ReplayError("same-arm same-time events need unambiguous sequence")
    declared = {(at.date().isoformat(), e.get("code")) for at, e in parsed
                if e.get("kind") == "cash_dividend_entitlement"}
    book = start_fixture_accounts(sessions=sessions, marks=marks, action_coverage=action_coverage,
        horizon=horizon, input_origin=input_origin, declared_distributions=declared)
    grouped = {}
    for at, event in parsed:
        day = at.date().isoformat()
        if not sessions[0] <= day <= sessions[-1]:
            raise ReplayError("event outside replay calendar; leave future receivable pending")
        grouped.setdefault(day, []).append(event)
    for day in sorted(set(sessions) | set(grouped)):
        for event in grouped.get(day, []):
            apply_fixture_event(book, event)
        close_fixture_day(book, day)
    return finish_fixture_accounts(book)
