from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP


CHINEXT_20_PCT_START = date(2020, 8, 24)
MAIN_BOARD_REGISTRATION_START = date(2023, 4, 10)
MAIN_BOARD_ST_10_PCT_START = date(2026, 7, 6)
NO_LIMIT_EVENT_TYPES = {
    "delisting_first_day",
    "relisting_first_day",
    "no_limit_resumption_day",
}


def board(code: str) -> str:
    normalized = code.split(".")[-1]
    if normalized.startswith(("300", "301", "302")):
        return "chinext"
    if normalized.startswith((
        "000", "001", "002", "003",
        "600", "601", "603", "605",
    )):
        return "main"
    if normalized.startswith(("688", "689")):
        return "star"
    if normalized.startswith(("4", "8", "920")):
        return "bse"
    raise ValueError(f"V4不支持该证券代码：{code}")


def price_limit_ratio(
    code: str,
    trade_date: date,
    *,
    is_st: bool,
    listed_trading_days: int,
    special_event: str | None = None,
) -> float | None:
    """Return the historical daily price-limit ratio for the V4 scope.

    None means the date is governed by an IPO-specific price-limit regime and
    is excluded from factor trading. Before the registration-based reforms,
    the first listed day used asymmetric IPO limits; after the reforms, the
    first five listed days have no normal daily limit. V4 factor eligibility
    requires at least 120 history rows, so these branches are safety
    boundaries rather than buyable states.
    """
    if special_event in NO_LIMIT_EVENT_TYPES:
        return None
    stock_board = board(code)
    if stock_board == "star":
        if listed_trading_days <= 5:
            return None
        return 0.20
    if stock_board == "bse":
        if listed_trading_days == 1:
            return None
        return 0.30
    if stock_board == "chinext":
        if (
            trade_date >= CHINEXT_20_PCT_START
            and listed_trading_days <= 5
        ):
            return None
        if (
            trade_date < CHINEXT_20_PCT_START
            and listed_trading_days == 1
        ):
            return None
        if trade_date >= CHINEXT_20_PCT_START:
            return 0.20
        return 0.05 if is_st else 0.10
    if (
        trade_date >= MAIN_BOARD_REGISTRATION_START
        and listed_trading_days <= 5
    ):
        return None
    if (
        trade_date < MAIN_BOARD_REGISTRATION_START
        and listed_trading_days == 1
    ):
        return None
    if is_st and trade_date < MAIN_BOARD_ST_10_PCT_START:
        return 0.05
    return 0.10


def rounded_limit_price(previous_close: float, ratio: float, *, up: bool) -> float:
    if previous_close <= 0:
        raise ValueError("前收盘价必须大于0")
    if not 0 < ratio < 1:
        raise ValueError("涨跌幅比例必须在0和1之间")
    multiplier = Decimal("1") + (
        Decimal(str(ratio)) if up else -Decimal(str(ratio))
    )
    value = Decimal(str(previous_close)) * multiplier
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def daily_price_limits(
    code: str,
    trade_date: date,
    previous_close: float,
    *,
    is_st: bool,
    listed_trading_days: int,
    special_event: str | None = None,
) -> dict:
    ratio = price_limit_ratio(
        code,
        trade_date,
        is_st=is_st,
        listed_trading_days=listed_trading_days,
        special_event=special_event,
    )
    if ratio is None:
        return {
            "ratio": None,
            "up_limit": None,
            "down_limit": None,
            "eligible_for_v4": False,
            "reason": (
                f"特殊事件:{special_event}"
                if special_event in NO_LIMIT_EVENT_TYPES
                else "IPO特殊涨跌停阶段不进入V4因子交易"
            ),
        }
    return {
        "ratio": ratio,
        "up_limit": rounded_limit_price(previous_close, ratio, up=True),
        "down_limit": rounded_limit_price(previous_close, ratio, up=False),
        "eligible_for_v4": not is_st,
        "reason": "ST不进入V4" if is_st else "normal",
    }
