"""Research-only, self-financing accounting for reported A-share and stock-ETF fills.

This checks source bytes and arithmetic, not broker authenticity or hypothetical
order executability.  No economic admission can be inferred from its output.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from quantqc.ashare import board_of


CENT = Decimal("0.01")
SHANGHAI = ZoneInfo("Asia/Shanghai")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
CODE = re.compile(r"[0-9]{6}\.(SH|SZ)\Z")
ETF = re.compile(r"(?:5[0-9]{5}\.SH|15[0-9]{4}\.SZ)\Z")
FEE_KEYS = ("commission", "stamp", "transfer", "other")
ORIGINS = {"broker_export", "paper_simulator", "synthetic_fixture"}
DOCUMENTED_BROAD_ETF_CANDIDATE = "510300.SH"
DOCUMENTED_FROM = date(2026, 6, 12)
DOCUMENTED_THROUGH = date(2026, 9, 29)
IDENTITY_SOURCE = (
    "https://www.sse.com.cn/disclosure/announcement/general/jjzssgg/"
    "c/c_20260611_10821513.shtml"
)


class ReplayError(ValueError):
    """An input cannot support even the bounded accounting replay."""


def _number(value: object, label: str, *, positive: bool = False,
            cents: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ReplayError(f"{label}: invalid number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ReplayError(f"{label}: invalid number") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ReplayError(f"{label}: nonpositive or nonfinite")
    if cents and result != result.quantize(CENT):
        raise ReplayError(f"{label}: sub-cent amount")
    return result


def _date(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise ReplayError(f"{label}: invalid date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ReplayError(f"{label}: invalid date") from None
    if parsed.isoformat() != value:
        raise ReplayError(f"{label}: noncanonical date")
    return parsed


def _time(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ReplayError(f"{label}: missing timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ReplayError(f"{label}: invalid timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReplayError(f"{label}: timestamp needs timezone")
    return parsed.astimezone(SHANGHAI)


def _source(payload: dict, ref: object, root: Path, label: str) -> str:
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
        raise ReplayError(f"{label}: source reference missing")
    digest = ref.get("sha256")
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise ReplayError(f"{label}: source hash invalid")
    relative = Path(ref["path"])
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise ReplayError(f"{label}: source path invalid")
    base = root.resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base):
        raise ReplayError(f"{label}: source escapes evidence root")
    def unique_object(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ReplayError(f"{label}: duplicate JSON key {key}")
            result[key] = value
        return result

    def same_json(left: object, right: object) -> bool:
        if type(left) is not type(right):
            return False
        if isinstance(left, dict):
            return left.keys() == right.keys() and all(
                same_json(left[key], right[key]) for key in left
            )
        if isinstance(left, list):
            return len(left) == len(right) and all(
                same_json(a, b) for a, b in zip(left, right)
            )
        return left == right

    try:
        raw = path.read_bytes()
        recorded = json.loads(raw, object_pairs_hook=unique_object)
    except ReplayError:
        raise
    except (OSError, ValueError):
        raise ReplayError(f"{label}: source unreadable") from None
    if hashlib.sha256(raw).hexdigest() != digest or not same_json(recorded, payload):
        raise ReplayError(f"{label}: source bytes or fields mismatch")
    return digest


def _stock(code: str) -> bool:
    if not CODE.fullmatch(code):
        return False
    prefix = code[:3]
    if code.endswith(".SH") and prefix not in {"600", "601", "603", "605"}:
        return False
    if code.endswith(".SZ") and prefix not in {"000", "001", "002", "003", "300", "301"}:
        return False
    return board_of(code) in {"main", "gemb"}


def _validate_fill(row: object, arm: str, benchmark_code: str, etf_account_ref: str,
                   evidence_root: Path, days: set[str]) -> dict:
    if not isinstance(row, dict):
        raise ReplayError("fill: invalid record")
    label = f"fill:{row.get('fill_id', 'unknown')}"
    fill_id = row.get("fill_id")
    code, side = row.get("code"), row.get("side")
    day = row.get("trade_date")
    if not isinstance(fill_id, str) or not fill_id or day not in days:
        raise ReplayError(f"{label}: identity or trade date invalid")
    if side not in {"buy", "sell"} or not isinstance(code, str):
        raise ReplayError(f"{label}: side or code invalid")
    if arm == "strategy" and not _stock(code):
        raise ReplayError(f"{label}: stock outside main/ChiNext scope")
    if arm == "etf" and code != benchmark_code:
        raise ReplayError(f"{label}: benchmark identity mismatch")
    if arm == "etf" and row.get("account_ref") != etf_account_ref:
        raise ReplayError(f"{label}: ETF account reference mismatch")
    shares, order_shares = row.get("shares"), row.get("order_shares")
    order_id = row.get("order_id")
    if (type(shares) is not int or shares <= 0
            or type(order_shares) is not int or order_shares <= 0
            or shares > order_shares
            or not isinstance(order_id, str) or not order_id):
        raise ReplayError(f"{label}: fill/order quantity or identity invalid")
    if side == "buy" and order_shares % 100:
        raise ReplayError(f"{label}: buy order quantity not 100-unit multiple")
    price = _number(row.get("price_yuan"), f"{label}:price", positive=True)
    tick = Decimal("0.001") if arm == "etf" else CENT
    if price % tick:
        raise ReplayError(f"{label}: price tick precision invalid")
    fees = row.get("fees_yuan")
    if not isinstance(fees, dict) or set(fees) != set(FEE_KEYS):
        raise ReplayError(f"{label}: fee breakdown incomplete")
    fee_values = {key: _number(fees[key], f"{label}:{key}", cents=True)
                  for key in FEE_KEYS}
    if (side == "buy" or arm == "etf") and fee_values["stamp"] != 0:
        raise ReplayError(f"{label}: stamp duty incompatible with instrument/side")
    times = [_time(row.get(key), f"{label}:{key}") for key in
             ("alert_at", "decision_at", "submitted_at", "filled_at")]
    if times != sorted(times) or times[-1].date().isoformat() != day:
        raise ReplayError(f"{label}: alert/order/fill time sequence invalid")
    submitted_clock = times[-2].time()
    executed_clock = times[-1].time()
    if times[-2].date().isoformat() != day or not (
        time(9, 15) <= submitted_clock <= time(9, 25)
        or time(9, 30) <= submitted_clock <= time(11, 30)
        or time(13) <= submitted_clock <= time(15)
    ):
        raise ReplayError(f"{label}: submitted outside same-day order sessions")
    if not (executed_clock == time(9, 25)
            or time(9, 30) <= executed_clock <= time(11, 30)
            or time(13) <= executed_clock <= time(15)):
        raise ReplayError(f"{label}: fill outside regular trading sessions")
    origin = row.get("proof_origin")
    if origin not in ORIGINS:
        raise ReplayError(f"{label}: proof origin missing")
    payload = {key: value for key, value in row.items() if key != "proof"}
    _source(payload, row.get("proof"), evidence_root, label)
    return {
        "fill_id": fill_id, "code": code, "side": side, "day": day,
        "shares": shares, "price": price,
        "order_id": order_id, "order_shares": order_shares,
        "gross": (price * shares).quantize(CENT, rounding=ROUND_HALF_UP),
        "fees": fee_values, "times": times, "origin": origin,
    }


def _coverage(rows: object, root: Path, trade_dates: list[str],
              benchmark_code: str) -> tuple[set[tuple[str, str]], dict[tuple[str, str], dict]]:
    if not isinstance(rows, list):
        raise ReplayError("corporate-action coverage missing")
    days = set(trade_dates)
    previous = {trade_dates[i]: trade_dates[i-1] for i in range(1, len(trade_dates))}
    covered: set[tuple[str, str]] = set()
    distributions = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ReplayError("corporate-action coverage invalid")
        day, code = row.get("date"), row.get("code")
        if day not in days or not isinstance(code, str) or not CODE.fullmatch(code):
            raise ReplayError("corporate-action coverage identity invalid")
        key = day, code
        if key in covered:
            raise ReplayError("corporate-action coverage duplicated")
        payload = {key: value for key, value in row.items() if key != "proof"}
        _source(payload, row.get("proof"), root, f"action:{day}:{code}")
        if row.get("event") == "none":
            if set(payload) != {"date", "code", "event"}:
                raise ReplayError(f"action:{day}:{code}: no-event fields invalid")
        elif row.get("event") == "etf_cash_distribution" and code == benchmark_code:
            required = {"date", "code", "event", "announcement_date", "record_date",
                        "payout_date", "cash_per_share_yuan"}
            if set(payload) != required:
                raise ReplayError(f"action:{day}:{code}: distribution fields invalid")
            announcement = _date(row["announcement_date"], "distribution announcement")
            record = _date(row["record_date"], "distribution record")
            payout = _date(row["payout_date"], "distribution payout")
            ex_date = _date(day, "distribution ex date")
            if (announcement > record or previous.get(day) != row["record_date"]
                    or not (record < ex_date <= payout)
                    or row["payout_date"] not in days):
                raise ReplayError(f"action:{day}:{code}: distribution chronology invalid")
            amount = _number(row["cash_per_share_yuan"], "distribution per share", positive=True)
            if amount.as_tuple().exponent < -6:
                raise ReplayError(f"action:{day}:{code}: distribution precision invalid")
            distributions[key] = {**row, "per_share": amount}
        else:
            raise ReplayError(f"action:{day}:{code}: corporate action unsupported")
        covered.add(key)
    return covered, distributions


def _distribution_receipts(rows: object, root: Path, days: set[str],
                           benchmark_code: str, etf_account_ref: str) -> dict[tuple[str, str], dict]:
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise ReplayError("distribution receipts invalid")
    result = {}
    fields = {"ex_date", "record_date", "payout_date", "code", "amount_yuan",
              "proof_origin", "arm", "account_ref"}
    for row in rows:
        if not isinstance(row, dict) or set(row) != fields | {"proof"}:
            raise ReplayError("distribution receipt fields invalid")
        day = row["ex_date"]
        key = day, row["code"]
        if key in result:
            raise ReplayError("distribution receipt duplicated")
        if (row["code"] != benchmark_code or row["arm"] != "etf"
                or row["account_ref"] != etf_account_ref or day not in days
                or row["payout_date"] not in days
                or row["record_date"] not in days
                or row["proof_origin"] not in ORIGINS):
            raise ReplayError("distribution receipt identity invalid")
        amount = _number(row["amount_yuan"], "distribution receipt", positive=True, cents=True)
        payload = {key: value for key, value in row.items() if key != "proof"}
        _source(payload, row["proof"], root, f"distribution-receipt:{day}:{row['code']}")
        result[key] = {**row, "amount": amount}
    return result


def _calendar(calendar: object, root: Path, trade_dates: list[str]) -> None:
    if not isinstance(calendar, dict):
        raise ReplayError("source-bound trading calendar missing")
    payload = {key: value for key, value in calendar.items() if key != "proof"}
    _source(payload, calendar.get("proof"), root, "calendar")
    listed = calendar.get("trading_dates")
    if not isinstance(listed, list) or not listed:
        raise ReplayError("trading calendar dates missing")
    parsed = [_date(day, "calendar") for day in listed]
    if parsed != sorted(set(parsed)) or any(day.weekday() >= 5 for day in parsed):
        raise ReplayError("trading calendar invalid or contains weekend")
    if trade_dates[0] not in listed or trade_dates[-1] not in listed:
        raise ReplayError("replay dates outside calendar")
    begin, end = listed.index(trade_dates[0]), listed.index(trade_dates[-1])
    if listed[begin:end + 1] != trade_dates:
        raise ReplayError("replay omits market sessions")


def _marks(marks: dict, metadata: object, proof: object, root: Path,
           trade_dates: list[str]) -> str:
    """Bind daily closes and their claimed raw-price basis to the same bytes."""
    if not isinstance(metadata, dict) or set(metadata) != {
        "price_basis", "provider_field", "adjustment", "cash_distributions_included",
        "source_provider",
    }:
        raise ReplayError("marks: source basis missing")
    source_sha = _source({"metadata": metadata, "closes": marks}, proof, root, "marks")
    if (metadata["price_basis"] != "unadjusted_exchange_traded_close"
            or metadata["provider_field"] != "close"
            or metadata["adjustment"] != "none"
            or metadata["cash_distributions_included"] is not False
            or not isinstance(metadata["source_provider"], str)
            or not metadata["source_provider"].strip()):
        raise ReplayError("marks: raw unadjusted price basis required")
    if set(marks) != set(trade_dates):
        raise ReplayError("marks: missing or extra trading date")
    for day, closes in marks.items():
        if not isinstance(closes, dict):
            raise ReplayError(f"marks:{day}: daily closes invalid")
        for code, close in closes.items():
            if not isinstance(code, str) or not CODE.fullmatch(code):
                raise ReplayError(f"marks:{day}: instrument code invalid")
            _number(close, f"mark:{day}:{code}", positive=True)
    return source_sha


def _arm_snapshot(state: dict, marks: dict, day: str) -> dict:
    holdings = {}
    receivable = sum(state["receivables"].values(), Decimal("0"))
    equity = state["cash"] + receivable
    for code, lots in sorted(state["lots"].items()):
        shares = sum(qty for _buy_day, qty in lots)
        if shares == 0:
            continue
        if not isinstance(marks.get(day), dict) or code not in marks[day]:
            raise ReplayError(f"mark:{day}:{code}: missing close")
        close = _number(marks[day][code], f"mark:{day}:{code}", positive=True)
        equity += (close * shares).quantize(CENT, rounding=ROUND_HALF_UP)
        holdings[code] = shares
    return {
        "cash_yuan": float(state["cash"]),
        "receivable_yuan": float(receivable),
        "equity_yuan": float(equity),
        "holdings": holdings,
        "cumulative_fees_yuan": float(state["fees"]),
    }


def replay_confirmed_fills(
    *, trade_dates: list[str], marks: dict[str, dict[str, object]],
    marks_proof: dict,
    marks_metadata: dict,
    fills: dict[str, list[dict]], action_coverage: list[dict],
    calendar: dict,
    benchmark_code: str, evidence_root: Path,
    etf_account_ref: str,
    initial_cash_yuan: object = 150000,
    distribution_receipts: list[dict] | None = None,
) -> dict:
    """Replay two 150k accounts from source-bound reported fills; never admit Target A.

    Only source-bound ETF cash distributions are handled. Other actions remain
    unsupported; completeness/authenticity needs independent review.
    """
    if not isinstance(trade_dates, list) or not trade_dates:
        raise ReplayError("trading calendar missing")
    parsed_days = [_date(day, "trade_dates") for day in trade_dates]
    if parsed_days != sorted(set(parsed_days)):
        raise ReplayError("trading calendar duplicated or unordered")
    days = set(trade_dates)
    if not isinstance(benchmark_code, str) or not ETF.fullmatch(benchmark_code):
        raise ReplayError("benchmark ETF code invalid or unfrozen")
    if benchmark_code != DOCUMENTED_BROAD_ETF_CANDIDATE or not (
        DOCUMENTED_FROM <= parsed_days[0] <= parsed_days[-1] <= DOCUMENTED_THROUGH
    ):
        raise ReplayError("benchmark broad-stock ETF identity or documented date invalid")
    if (not isinstance(etf_account_ref, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", etf_account_ref)):
        raise ReplayError("ETF account reference invalid")
    if not isinstance(marks, dict) or not isinstance(fills, dict) or set(fills) != {"strategy", "etf"}:
        raise ReplayError("marks or both account fill streams missing")
    cash = _number(initial_cash_yuan, "initial cash", positive=True, cents=True)
    if cash != Decimal("150000"):
        raise ReplayError("Target A requires matched 150000 yuan starting cash")
    root = Path(evidence_root)
    if not root.is_dir():
        raise ReplayError("evidence root missing")
    _calendar(calendar, root, trade_dates)
    covered, distributions = _coverage(action_coverage, root, trade_dates, benchmark_code)
    receipts = _distribution_receipts(distribution_receipts, root, days,
                                      benchmark_code, etf_account_ref)
    marks_source_sha256 = _marks(marks, marks_metadata, marks_proof, root, trade_dates)
    by_day = {arm: {day: [] for day in trade_dates} for arm in ("strategy", "etf")}
    origins: set[str] = {receipt["proof_origin"] for receipt in receipts.values()}
    fill_ids: set[str] = set()
    for arm in ("strategy", "etf"):
        if not isinstance(fills[arm], list):
            raise ReplayError(f"{arm}: fill stream invalid")
        for row in fills[arm]:
            fill = _validate_fill(row, arm, benchmark_code, etf_account_ref, root, days)
            if fill["fill_id"] in fill_ids:
                raise ReplayError("duplicate fill ID across accounts")
            fill_ids.add(fill["fill_id"])
            origins.add(fill["origin"])
            by_day[arm][fill["day"]].append(fill)
    states = {arm: {"cash": cash, "fees": Decimal("0"), "lots": {},
                    "receivables": {}, "record_positions": {}}
              for arm in ("strategy", "etf")}
    orders_seen: dict[str, dict[str, dict]] = {arm: {} for arm in states}
    daily = []
    delays = {arm: [] for arm in states}
    distributions_report = []
    used_receipts = set()
    for day in trade_dates:
        row = {"trade_date": day}
        for arm, state in states.items():
            if arm == "etf":
                for key, action in distributions.items():
                    if key[0] != day:
                        continue
                    entitled = state["record_positions"].get((action["record_date"], key[1]), 0)
                    expected = (action["per_share"] * entitled).quantize(CENT, rounding=ROUND_HALF_UP)
                    if entitled:
                        state["receivables"][key] = expected
                    distributions_report.append({
                        "code": key[1], "record_date": action["record_date"],
                        "ex_date": day, "payout_date": action["payout_date"],
                        "entitled_shares": entitled, "gross_yuan": float(expected),
                        "action_source_sha256": action["proof"]["sha256"],
                        "receipt_source_sha256": None,
                    })
            touched = {code for code, lots in state["lots"].items() if lots}
            for fill in sorted(by_day[arm][day], key=lambda x: (x["times"][-1], x["fill_id"])):
                code, shares = fill["code"], fill["shares"]
                touched.add(code)
                total_fee = sum(fill["fees"].values())
                lots = state["lots"].setdefault(code, [])
                identity = (day, code, fill["side"], fill["order_shares"],
                            tuple(fill["times"][:3]))
                order = orders_seen[arm].get(fill["order_id"])
                if order is None:
                    if fill["side"] == "sell":
                        sellable_for_order = sum(qty for bought, qty in lots if bought < day)
                        prior_sells = [other for other in orders_seen[arm].values()
                                       if other["day"] == day and other["code"] == code
                                       and other["side"] == "sell"]
                        if any(fill["times"][2] < other["submitted"] for other in prior_sells):
                            raise ReplayError(f"fill:{fill['fill_id']}: ambiguous sell order chronology")
                        reserved = sum(other["order_shares"] - other["filled"]
                                       for other in prior_sells)
                        free = sellable_for_order - reserved
                        odd = fill["order_shares"] % 100
                        if (fill["order_shares"] > free
                                or (odd and (free % 100 == 0 or odd != free % 100))):
                            raise ReplayError(f"fill:{fill['fill_id']}: sell order quantity invalid")
                    order = {
                        "identity": identity, "filled": 0,
                        "day": day, "code": code, "side": fill["side"],
                        "order_shares": fill["order_shares"],
                        "submitted": fill["times"][2],
                    }
                    orders_seen[arm][fill["order_id"]] = order
                elif order["identity"] != identity:
                    raise ReplayError(f"fill:{fill['fill_id']}: order identity changed")
                if order["filled"] + shares > fill["order_shares"]:
                    raise ReplayError(f"fill:{fill['fill_id']}: partial fills exceed order")
                order["filled"] += shares
                if fill["side"] == "buy":
                    debit = fill["gross"] + total_fee
                    if debit > state["cash"]:
                        raise ReplayError(f"fill:{fill['fill_id']}: insufficient shared cash")
                    state["cash"] -= debit
                    lots.append((day, shares))
                else:
                    sellable = sum(qty for bought, qty in lots if bought < day)
                    if shares > sellable:
                        raise ReplayError(f"fill:{fill['fill_id']}: T+1 or inventory violation")
                    remaining = shares
                    new_lots = []
                    for bought, qty in lots:
                        take = min(qty, remaining) if bought < day else 0
                        remaining -= take
                        if qty > take:
                            new_lots.append((bought, qty - take))
                    state["lots"][code] = new_lots
                    credit = fill["gross"] - total_fee
                    if credit < 0:
                        raise ReplayError(f"fill:{fill['fill_id']}: fees exceed proceeds")
                    state["cash"] += credit
                state["fees"] += total_fee
                alert, decision, submitted, executed = fill["times"]
                delays[arm].append({
                    "fill_id": fill["fill_id"],
                    "alert_to_submit_seconds": (submitted - alert).total_seconds(),
                    "decision_to_submit_seconds": (submitted - decision).total_seconds(),
                    "submit_to_fill_seconds": (executed - submitted).total_seconds(),
                })
            for code in touched:
                if (day, code) not in covered:
                    raise ReplayError(f"action:{day}:{code}: no-event coverage missing")
            if arm == "etf":
                state["record_positions"][(day, benchmark_code)] = sum(
                    qty for _bought, qty in state["lots"].get(benchmark_code, [])
                )
                for entry in distributions_report:
                    if entry["payout_date"] != day or not entry["entitled_shares"]:
                        continue
                    key = entry["ex_date"], entry["code"]
                    receipt = receipts.get(key)
                    if (receipt is None or receipt["record_date"] != entry["record_date"]
                            or receipt["payout_date"] != day):
                        raise ReplayError("distribution receipt missing or date mismatch")
                    if receipt["amount"] != state["receivables"].get(key):
                        raise ReplayError("distribution receipt amount mismatch")
                    state["cash"] += receipt["amount"]
                    del state["receivables"][key]
                    entry["receipt_source_sha256"] = receipt["proof"]["sha256"]
                    used_receipts.add(key)
            row[arm] = _arm_snapshot(state, marks, day)
        row["paired_net_excess_pct"] = round(
            100 * (row["strategy"]["equity_yuan"] - row["etf"]["equity_yuan"])
            / float(cash), 6,
        )
        daily.append(row)
    if set(receipts) != used_receipts:
        raise ReplayError("distribution receipt without entitlement")
    return {
        "schema": "target_a_reported_fill_cash_replay.v3",
        "benchmark_code": benchmark_code,
        "etf_account_ref": etf_account_ref,
        "benchmark_selection": "pre-result_documented_candidate_not_user_frozen",
        "benchmark_identity_source": IDENTITY_SOURCE,
        "benchmark_identity_window": [DOCUMENTED_FROM.isoformat(), DOCUMENTED_THROUGH.isoformat()],
        "initial_cash_yuan_per_arm": float(cash),
        "marks_source_sha256": marks_source_sha256,
        "claimed_marks_metadata": marks_metadata,
        "daily": daily,
        "observed_delays": delays,
        "etf_cash_distributions": distributions_report,
        "claimed_proof_origins": sorted(origins),
        "target_a_admitted": False,
        "authority": "research_only_source_bytes_not_broker_authenticity_or_fill_counterfactual",
        "limitations": [
            "source hashes bind supplied JSON but do not authenticate broker or no-event coverage",
            "per-fill fee allocation and final broker settlement are not independently reconciled",
            "marks and calendar JSON bytes are bound, but provider authenticity, rights and market completeness are not independently verified",
            "the raw unadjusted mark basis is a source-bound claim, not independent provider certification",
            "510300.SH is a documented candidate within the coded date window, not user-final-selected",
            "only ETF cash distributions with bound account receipts are supported; other corporate actions remain unsupported",
            "ETF receivables are included in equity but cannot fund orders before recorded payout",
            "receipt source hashes bind supplied JSON, not broker authenticity or actual available-cash time",
            "matching pseudonymous ETF account references do not authenticate the broker account or cash ledger",
            "no point-in-time universe, independent OOS, 1/3/5-day study or forward admission",
        ],
    }
