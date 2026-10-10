"""A 股共享执行规则。

本模块只回答一笔委托在已知交易身份下是否合规、价格边界和股数约束；
因子回测与市场状态联合回放都应复用这里，避免各写一套规则。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


BOARD_LIMITS = {"main": 0.10, "gemb": 0.20, "star": 0.20}
RISK_WARNING_MAIN_LIMIT_BEFORE_2026_07_06 = 0.05
RISK_WARNING_MAIN_LIMIT_FROM_2026_07_06 = 0.10
PRICE_TICK_YUAN = Decimal("0.01")
RISK_WARNING_MAIN_REFORM_DATE = date(2026, 7, 6)
ST_PREFIXES = ("ST", "*ST", "S", "S*ST", "SST")


def board_of(code: str) -> str | None:
    """按证券代码返回当前研究范围内的板块。"""
    code = str(code).strip().split(".")[0]
    if code.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        return "main"
    if code.startswith(("300", "301")):
        return "gemb"
    if code.startswith(("688", "689")):
        return "star"
    return None


def _date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    date_method = getattr(value, "date", None)
    if callable(date_method):
        try:
            converted = date_method()
            if isinstance(converted, date):
                return converted
        except (TypeError, ValueError):
            pass
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _money(value: object) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def price_limit_rule(
    code: str, *, trade_date: object, is_risk_warning: bool | None,
    no_price_limit: bool | None,
) -> dict:
    """返回日期化涨跌幅身份；字段不全时显式 fail-closed。"""
    board = board_of(code)
    day = _date(trade_date)
    if board is None:
        return {"valid": False, "status": "board_identity_missing", "rate": None}
    if day is None:
        return {"valid": False, "status": "trade_date_missing", "rate": None}
    if no_price_limit is None:
        return {
            "valid": False,
            "status": "listing_or_special_event_identity_missing",
            "rate": None,
        }
    if is_risk_warning is None:
        return {
            "valid": False,
            "status": "risk_warning_identity_missing",
            "rate": None,
        }
    if no_price_limit:
        return {
            "valid": True, "status": "no_price_limit_day", "rate": None,
            "board": board, "trade_date": day.isoformat(),
        }
    if board in {"gemb", "star"}:
        rate = BOARD_LIMITS[board]
    elif is_risk_warning and day < RISK_WARNING_MAIN_REFORM_DATE:
        rate = RISK_WARNING_MAIN_LIMIT_BEFORE_2026_07_06
    elif is_risk_warning:
        rate = RISK_WARNING_MAIN_LIMIT_FROM_2026_07_06
    else:
        rate = BOARD_LIMITS["main"]
    return {
        "valid": True, "status": "price_limit_applies", "rate": rate,
        "board": board, "trade_date": day.isoformat(),
    }


def _minimum_tick_rule_verified(code: str, trade_date: object) -> bool:
    """Selected current-version evidence, not the rule's historical start date.

    SSE2026 3.3.17 and SZSE2026 3.3.19 have separately verified publication
    intervals. Earlier amendment chains remain unverified for this edge case.
    Literals stay in this source-bound callable; never infer law from board rate.
    """
    code = str(code).strip().split(".")[0]
    day = _date(trade_date)
    if board_of(code) is None or day is None:
        return False
    exchange = "SSE" if code.startswith("6") else "SZSE"
    known_from = {"SSE": date(2026, 7, 6), "SZSE": date(2026, 7, 6)}
    return day >= known_from[exchange]


def _limit_price(prev_close: object, rate: float, side: str, *,
                 minimum_tick_rule_verified: bool = False) -> float | None:
    price = _money(prev_close)
    if price is None:
        return None
    delta = Decimal(str(rate)) if side == "up" else -Decimal(str(rate))
    rounded = (price * (Decimal("1") + delta)).quantize(
        PRICE_TICK_YUAN, rounding=ROUND_HALF_UP,
    )
    result = rounded
    if abs(result - price) < PRICE_TICK_YUAN:
        result = price + (PRICE_TICK_YUAN if side == "up" else -PRICE_TICK_YUAN)
    result = max(result, PRICE_TICK_YUAN)
    # Compare AFTER the floor: e.g. .01 down returns the original .01 bound.
    if result != rounded and minimum_tick_rule_verified is not True:
        return None
    return float(result)


def opening_auction_executability(
    *, code: str, trade_date: object, side: str, open_price: object,
    prev_close: object, tradable: bool | None, is_risk_warning: bool | None,
    no_price_limit: bool | None,
) -> dict:
    """保守判断开盘集合竞价成交；缺身份或价格时不成交。"""
    if side not in {"buy", "sell"}:
        return {"executable": False, "reason": "side_invalid"}
    if tradable is not True:
        return {"executable": False, "reason": "suspended_or_untradable"}
    price = _money(open_price)
    previous = _money(prev_close)
    if price is None or previous is None:
        return {"executable": False, "reason": "execution_price_missing"}
    rule = price_limit_rule(
        code, trade_date=trade_date, is_risk_warning=is_risk_warning,
        no_price_limit=no_price_limit,
    )
    if rule.get("valid") is not True:
        return {
            "executable": False, "reason": rule["status"],
            "price_limit_rule": rule,
        }
    if rule["rate"] is None:
        return {
            "executable": True, "reason": "no_price_limit_day",
            "price_limit_rule": rule, "limit_price": None,
        }
    limit_side = "up" if side == "buy" else "down"
    limit_price = _limit_price(
        previous, float(rule["rate"]), limit_side,
        minimum_tick_rule_verified=_minimum_tick_rule_verified(code, trade_date),
    )
    if limit_price is None:
        return {"executable": False, "reason": "historical_minimum_tick_rule_unverified",
                "price_limit_rule": rule, "limit_price": None}
    blocked = (
        price >= Decimal(str(limit_price))
        if side == "buy" else price <= Decimal(str(limit_price))
    )
    return {
        "executable": not blocked,
        "reason": (
            "limit_up_no_fill" if side == "buy" else "limit_down_no_fill"
        ) if blocked else "price_executable",
        "price_limit_rule": rule,
        "limit_price": limit_price,
    }


def opening_auction_mask(
    *, codes, trade_dates, side: str, open_prices, prev_closes,
    tradable, is_risk_warning,
):
    """Batch equivalent of the scalar opening rule for regular limited sessions.

    Input matrices are date by security. This does not certify special-event
    identity, queue fills or source prices. Rates come from price_limit_rule;
    half-tick floating ambiguities use the same scalar Decimal calculation.
    """
    import numpy as np

    opening = np.asarray(open_prices, dtype=np.float64)
    previous = np.asarray(prev_closes, dtype=np.float64)
    trading = np.asarray(tradable)
    states = np.asarray(is_risk_warning)
    shape = (len(trade_dates), len(codes))
    if (side not in {"buy", "sell"} or opening.shape != shape
            or previous.shape != shape or trading.shape != shape or states.shape != shape):
        raise ValueError("execution_grid_contract_invalid")

    def known_boolean(values):
        if values.dtype.kind == "b":
            return np.ones(shape, dtype=bool)
        return np.fromiter((isinstance(x, (bool, np.bool_)) for x in values.flat),
                           dtype=bool, count=values.size).reshape(shape)

    state_known = known_boolean(states)
    trading_known = known_boolean(trading)
    risk = np.equal(states, True)
    rates = np.full(shape, np.nan)
    boards = np.array([board_of(str(code)) for code in codes], dtype=object)
    for board in {b for b in boards if b is not None}:
        columns = np.flatnonzero(boards == board)
        representative = str(codes[int(columns[0])])
        for warning in (False, True):
            dated_rates = []
            for day in trade_dates:
                rule = price_limit_rule(representative, trade_date=day,
                                       is_risk_warning=warning, no_price_limit=False)
                dated_rates.append(rule["rate"] if rule.get("valid") is True
                                   and rule.get("rate") is not None else np.nan)
            selected = risk[:, columns] == warning
            rates[:, columns] = np.where(selected, np.asarray(dated_rates)[:, None],
                                         rates[:, columns])

    valid = (state_known & trading_known & np.equal(trading, True)
             & np.isfinite(opening) & (opening > 0)
             & np.isfinite(previous) & (previous > 0) & np.isfinite(rates))
    tick = float(PRICE_TICK_YUAN)
    delta = rates if side == "buy" else -rates
    # Code-specific legal evidence must not inherit a board representative's
    # state, even where today's two exchange effective dates happen to match.
    tick_rule_known = np.asarray([
        [_minimum_tick_rule_verified(str(code), day) for code in codes]
        for day in trade_dates
    ], dtype=bool).reshape(shape)
    with np.errstate(over="ignore", invalid="ignore"):
        scaled = np.where(valid, previous * (1.0 + delta) / tick, np.nan)
        floored = np.floor(scaled)
        limits = np.floor(scaled + .5) / (1.0 / tick)
        # Binary multiply/divide may straddle an exact Decimal half-tick.
        ambiguous = (valid & np.isfinite(scaled)
                     & (np.abs(scaled - floored - .5)
                        <= 8 * np.finfo(float).eps * np.maximum(1.0, np.abs(scaled))))
        distance = np.abs(limits - previous)
        # The strict Decimal '<tick' comparison also has a floating boundary.
        ambiguous |= (valid & (np.abs(distance - tick)
                      <= 8 * np.finfo(float).eps * np.maximum(1.0, np.abs(previous))))
        adjusted = np.where(distance < tick,
                            previous + (tick if side == "buy" else -tick), limits)
        adjusted = np.maximum(adjusted, tick)
        limits = np.where(tick_rule_known | (adjusted == limits), adjusted, np.nan)
    for row, column in zip(*np.nonzero(ambiguous)):
        limits[row, column] = _limit_price(
            float(previous[row, column]), float(rates[row, column]),
            "up" if side == "buy" else "down",
            minimum_tick_rule_verified=bool(tick_rule_known[row, column]))
    valid &= np.isfinite(limits) & (limits > 0)
    return valid & (opening < limits if side == "buy" else opening > limits)


def opening_limit_order_executability(
    *, code: str, trade_date: object, side: str, order_price: object,
    open_price: object, prev_close: object, tradable: bool | None,
    is_risk_warning: bool | None, no_price_limit: bool | None,
) -> dict:
    """保守验证开盘集合竞价限价单；无涨跌幅日暂不假定成交。"""
    market = opening_auction_executability(
        code=code, trade_date=trade_date, side=side, open_price=open_price,
        prev_close=prev_close, tradable=tradable,
        is_risk_warning=is_risk_warning, no_price_limit=no_price_limit,
    )
    if market.get("executable") is not True:
        return market
    rule = market["price_limit_rule"]
    if rule.get("rate") is None:
        return {
            "executable": False,
            "reason": "unsupported_no_price_limit_day_no_fill",
            "price_limit_rule": rule,
        }
    order = _money(order_price)
    opening = _money(open_price)
    previous = _money(prev_close)
    if order is None or opening is None or previous is None:
        return {"executable": False, "reason": "order_or_execution_price_missing"}
    if order != order.quantize(PRICE_TICK_YUAN):
        return {"executable": False, "reason": "order_price_not_tick_aligned"}
    rate = float(rule["rate"])
    verified = _minimum_tick_rule_verified(code, trade_date)
    lower = _limit_price(previous, rate, "down", minimum_tick_rule_verified=verified)
    upper = _limit_price(previous, rate, "up", minimum_tick_rule_verified=verified)
    if lower is None or upper is None:
        return {"executable": False, "reason": "historical_minimum_tick_rule_unverified"}
    if order < Decimal(str(lower)) or order > Decimal(str(upper)):
        return {
            "executable": False, "reason": "order_outside_daily_price_bounds",
            "lower_price": lower, "upper_price": upper,
        }
    crosses = order >= opening if side == "buy" else order <= opening
    return {
        "executable": bool(crosses),
        "reason": "price_executable" if crosses else "opening_price_not_reached",
        "price_limit_rule": rule,
        "lower_price": lower, "upper_price": upper,
        "limit_price": market.get("limit_price"),
    }


def constrain_share_quantity(
    requested_shares: object, *, side: str,
    available_to_sell: object | None = None, full_liquidation: bool = False,
    liquidity_cap_shares: object | None = None, lot_size: int = 100,
) -> dict:
    """输出整数股；买入/普通卖出按整手，零股只允许随完整清仓一次卖出。"""
    try:
        requested = max(0, int(float(requested_shares)))
        cap = (
            None if liquidity_cap_shares is None
            else max(0, int(float(liquidity_cap_shares)))
        )
    except (TypeError, ValueError, OverflowError):
        return {"accepted_shares": 0, "reason": "share_input_invalid"}
    if side == "buy":
        accepted = requested // lot_size * lot_size
        if cap is not None:
            accepted = min(accepted, cap // lot_size * lot_size)
    elif side == "sell":
        try:
            available = max(0, int(float(available_to_sell)))
        except (TypeError, ValueError, OverflowError):
            return {"accepted_shares": 0, "reason": "sellable_inventory_missing"}
        requested = min(requested, available)
        if full_liquidation:
            if requested != available:
                return {
                    "accepted_shares": 0,
                    "reason": "full_liquidation_must_use_entire_inventory",
                }
            if cap is not None and cap < available:
                return {
                    "accepted_shares": 0,
                    "reason": "odd_lot_cannot_be_split_by_liquidity_cap",
                }
            accepted = available
        else:
            accepted = requested // lot_size * lot_size
            if cap is not None:
                accepted = min(accepted, cap // lot_size * lot_size)
    else:
        return {"accepted_shares": 0, "reason": "side_invalid"}
    return {
        "accepted_shares": int(accepted),
        "reason": "accepted" if accepted > 0 else "below_lot_or_zero",
        "partial": 0 < accepted < requested,
    }


def continuous_auction_price_cage(
    *, side: str, reference_price: object, order_price: object,
) -> dict:
    """验证连续竞价 102%/98% 与十个最小价位的价格笼子。"""
    reference = _money(reference_price)
    order = _money(order_price)
    if reference is None or order is None:
        return {"valid": False, "accepted": False, "reason": "price_missing"}
    if order != order.quantize(PRICE_TICK_YUAN):
        return {
            "valid": True, "accepted": False,
            "reason": "order_price_not_tick_aligned",
        }
    ten_ticks = PRICE_TICK_YUAN * 10
    if side == "buy":
        boundary = max(reference * Decimal("1.02"), reference + ten_ticks)
        accepted = order <= boundary
    elif side == "sell":
        boundary = min(reference * Decimal("0.98"), reference - ten_ticks)
        accepted = order >= boundary
    else:
        return {"valid": False, "accepted": False, "reason": "side_invalid"}
    return {
        "valid": True, "accepted": bool(accepted),
        "reason": "accepted" if accepted else "outside_price_cage",
        "boundary_price": float(
            boundary.quantize(PRICE_TICK_YUAN, rounding=ROUND_HALF_UP)
        ),
    }


# 兼容旧调用；新执行代码必须使用上面的日期化接口。
def price_limit(code: str, name: str | None = None) -> float | None:
    board = board_of(code)
    if board is None:
        return None
    if board == "main" and name and any(
        str(name).strip().upper().startswith(p) for p in ST_PREFIXES
    ):
        return RISK_WARNING_MAIN_LIMIT_BEFORE_2026_07_06
    return BOARD_LIMITS[board]


def limit_up_price(prev_close: float, code: str, name: str | None = None) -> float | None:
    rate = price_limit(code, name)
    return None if rate is None else _limit_price(prev_close, rate, "up")


def limit_down_price(prev_close: float, code: str, name: str | None = None) -> float | None:
    rate = price_limit(code, name)
    return None if rate is None else _limit_price(prev_close, rate, "down")


def tradeable_at_open(
    price_open: float, prev_close: float, code: str,
    name: str | None = None, side: str = "buy",
) -> bool:
    result = opening_auction_executability(
        code=code, trade_date="2025-01-01", side=side,
        open_price=price_open, prev_close=prev_close, tradable=True,
        is_risk_warning=bool(name and any(
            str(name).strip().upper().startswith(p) for p in ST_PREFIXES
        )),
        no_price_limit=False,
    )
    return result["executable"] is True


def t_plus_one_sellable(buy_date: str, sell_date: str) -> bool:
    return str(sell_date) > str(buy_date)


def round_lots(shares: float) -> int:
    return constrain_share_quantity(shares, side="buy")["accepted_shares"]


def position_value_after_limits(cash: float, price: float) -> int:
    from quantqc.costs import lots_from_yuan
    return lots_from_yuan(cash, price)


__all__ = [
    "BOARD_LIMITS", "PRICE_TICK_YUAN", "RISK_WARNING_MAIN_REFORM_DATE",
    "board_of", "price_limit_rule", "opening_auction_executability",
    "opening_limit_order_executability", "constrain_share_quantity",
    "continuous_auction_price_cage",
    "price_limit", "limit_up_price", "limit_down_price", "tradeable_at_open",
    "t_plus_one_sellable", "round_lots", "position_value_after_limits",
]
