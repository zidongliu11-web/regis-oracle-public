"""Pure after-cost portfolio replay for the frozen PEAD candidate."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from research.pead_market_eligibility_epoch import board, route_state


INITIAL_CAPITAL = 150_000.0
LOT_SIZE = 100
SINGLE_NAME_CAP = 0.20
SECTOR_CAP = 0.35
CASH_BUFFER = 0.05
LIQUIDITY_CAP = 0.01
CORRELATION_CAP = 0.80
TRACK_CAPS = {
    "unconditioned_same_signal_same_execution": {
        "weak_repair": 0.80, "sentiment_rising": 0.80,
    },
    "fixed_exposure_same_signal_same_execution": {
        "weak_repair": 0.40, "sentiment_rising": 0.40,
    },
    "state_conditioned_same_signal_same_execution": {
        "weak_repair": 0.40, "sentiment_rising": 0.80,
    },
}


class ReplayError(ValueError):
    """A hash-bound replay fact is missing or ambiguous."""


def _finite(value: Any, *, positive: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or (positive and result <= 0):
        return None
    return result


def _same_number(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-10, abs_tol=1e-8)


def trade_costs(
    notional: float, *, side: str, trade_date: pd.Timestamp,
    lagged_median_turnover: float, cost_bps: int,
) -> dict[str, float]:
    if (
        notional <= 0 or lagged_median_turnover <= 0
        or side not in {"buy", "sell"} or cost_bps not in {10, 20}
    ):
        raise ReplayError("trade cost input is outside the frozen contract")
    participation_base = lagged_median_turnover * LIQUIDITY_CAP
    slippage = notional * cost_bps / 10_000.0
    impact_rate = cost_bps / 10_000.0 * math.sqrt(notional / participation_base)
    impact = notional * impact_rate
    commission = max(5.0, notional * 0.0003)
    transfer_rate = 0.00002 if trade_date < pd.Timestamp("2022-04-29") else 0.00001
    transfer = notional * transfer_rate
    stamp_rate = 0.001 if trade_date < pd.Timestamp("2023-08-28") else 0.0005
    stamp = notional * stamp_rate if side == "sell" else 0.0
    result = {
        "commission": commission, "transfer": transfer, "stamp": stamp,
        "slippage": slippage, "market_impact": impact,
    }
    result["total"] = sum(result.values())
    return result


class MarketBook:
    REQUIRED = {
        "stable_id", "alias", "trade_date", "raw_open", "raw_high", "raw_low",
        "raw_close", "adjusted_open", "adjusted_close", "turnover_yuan",
        "trade_status", "is_st",
    }

    def __init__(self, rows: pd.DataFrame, sessions: Iterable[Any]):
        missing = sorted(self.REQUIRED - set(rows.columns))
        if missing:
            raise ReplayError(f"market columns missing: {','.join(missing)}")
        self.sessions = pd.DatetimeIndex(pd.to_datetime(list(sessions))).normalize().sort_values()
        if self.sessions.empty or self.sessions.has_duplicates:
            raise ReplayError("session calendar is invalid")
        self.session_index = {pd.Timestamp(day): index for index, day in enumerate(self.sessions)}
        # The one-shot driver hands ownership of this already-projected frame to
        # the book.  Mutating it avoids a second multi-million-row copy.
        frame = rows
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.normalize()
        frame["session_index"] = frame["trade_date"].map(self.session_index)
        if frame["session_index"].isna().any():
            raise ReplayError("market row is outside the session calendar")
        frame["session_index"] = frame["session_index"].astype("int32")
        if frame.duplicated(["stable_id", "trade_date"]).any():
            raise ReplayError("stable-id/date market identity is duplicated")
        for column in (
            "raw_open", "raw_high", "raw_low", "raw_close", "adjusted_open",
            "adjusted_close", "turnover_yuan",
        ):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame.sort_values(["stable_id", "trade_date"], kind="mergesort", inplace=True)
        grouped = frame.groupby("stable_id", sort=False, observed=True)
        frame["prior_raw_close"] = grouped["raw_close"].shift(1)
        frame["previous_session_index"] = grouped["session_index"].shift(1)
        frame["daily_adjusted_return"] = grouped["adjusted_close"].pct_change(
            fill_method=None
        )
        frame["lagged_median_turnover"] = grouped["turnover_yuan"].transform(
            lambda values: values.shift(1).rolling(20, min_periods=20).median()
        )
        frame["twentieth_prior_index"] = grouped["session_index"].shift(20)
        frame["causal_20_ready"] = frame["twentieth_prior_index"].eq(
            frame["session_index"] - 20
        )
        frame["previous_session_ready"] = frame["previous_session_index"].eq(
            frame["session_index"] - 1
        )
        self.returns: dict[str, np.ndarray] = {}
        for stable_id, group in frame.groupby("stable_id", sort=False, observed=True):
            values = np.full(len(self.sessions), np.nan, dtype="float64")
            values[group["session_index"].to_numpy(dtype="int32")] = group[
                "daily_adjusted_return"
            ].to_numpy(dtype="float64", na_value=np.nan)
            self.returns[str(stable_id)] = values
        frame.drop(columns=[
            "session_index", "daily_adjusted_return", "twentieth_prior_index",
            "previous_session_index", "causal_20_ready", "previous_session_ready",
        ], inplace=True)
        frame.set_index(["stable_id", "trade_date"], inplace=True)
        self.frame = frame

    def row(self, stable_id: str, trade_date: pd.Timestamp) -> pd.Series | None:
        try:
            row = self.frame.loc[(str(stable_id), pd.Timestamp(trade_date))]
        except KeyError:
            return None
        if isinstance(row, pd.DataFrame):
            raise ReplayError(f"ambiguous market row: {stable_id} {trade_date.date()}")
        return row

    def prior_returns(self, stable_id: str, session_index: int, count: int) -> np.ndarray | None:
        values = self.returns.get(str(stable_id))
        if values is None or session_index < count:
            return None
        result = values[session_index - count:session_index]
        return result if len(result) == count and np.isfinite(result).all() else None

    def volatility(self, stable_id: str, session_index: int) -> float | None:
        values = self.prior_returns(stable_id, session_index, 20)
        if values is None:
            return None
        volatility = float(np.std(values, ddof=1))
        return volatility if math.isfinite(volatility) and volatility > 0 else None

    def correlation(
        self, left: str, right: str, session_index: int,
    ) -> float | None:
        left_values = self.prior_returns(left, session_index, 60)
        right_values = self.prior_returns(right, session_index, 60)
        if left_values is None or right_values is None:
            return None
        result = float(np.corrcoef(left_values, right_values)[0, 1])
        return result if math.isfinite(result) else None


@dataclass(frozen=True)
class IndustryInterval:
    start: pd.Timestamp
    end: pd.Timestamp | None
    name: str


class IndustryBook:
    def __init__(self, rows: pd.DataFrame):
        required = {"code", "effective_from", "effective_to", "canonical_industry_name"}
        if not required.issubset(rows.columns):
            raise ReplayError("industry interval schema is invalid")
        self.intervals: dict[str, list[IndustryInterval]] = defaultdict(list)
        for row in rows.to_dict("records"):
            code = str(row["code"]).zfill(6)
            start = pd.Timestamp(row["effective_from"])
            if start.tzinfo is not None:
                start = start.tz_localize(None)
            end_value = row.get("effective_to")
            end = None if pd.isna(end_value) or not str(end_value).strip() else pd.Timestamp(end_value)
            if end is not None and end.tzinfo is not None:
                end = end.tz_localize(None)
            name = str(row.get("canonical_industry_name") or "").strip()
            if not name:
                raise ReplayError("industry name is missing")
            self.intervals[code].append(IndustryInterval(
                start.normalize(), end.normalize() if end is not None else None, name,
            ))
        for code in self.intervals:
            self.intervals[code].sort(key=lambda value: value.start)

    def at(self, code: str, trade_date: pd.Timestamp) -> str | None:
        matches = [
            interval.name for interval in self.intervals.get(str(code).zfill(6), [])
            if interval.start <= trade_date and (
                interval.end is None or trade_date < interval.end
            )
        ]
        return matches[0] if len(matches) == 1 else None


def _one_price_lock(row: pd.Series, *, code: str, day: pd.Timestamp, side: str) -> bool:
    opening = _finite(row.get("raw_open"), positive=True)
    high = _finite(row.get("raw_high"), positive=True)
    low = _finite(row.get("raw_low"), positive=True)
    prior = _finite(row.get("prior_raw_close"), positive=True)
    if None in {opening, high, low, prior}:
        return True
    if abs(opening - high) > 1e-8 or abs(opening - low) > 1e-8:
        return False
    if bool(row.get("is_st")):
        threshold = 0.045
    elif board(code) == "chinext" and day >= pd.Timestamp("2020-08-24"):
        threshold = 0.195
    else:
        threshold = 0.095
    change = opening / prior - 1.0
    return change >= threshold if side == "buy" else change <= -threshold


def _mark(position: Mapping[str, Any], row: pd.Series, column: str) -> float:
    adjusted = _finite(row.get(column), positive=True)
    if adjusted is None:
        raise ReplayError(f"position adjusted price missing: {position['code']} {column}")
    return float(position["entry_notional"]) * adjusted / float(
        position["entry_adjusted_open"]
    )


def _carried_mark(position: Mapping[str, Any]) -> float:
    adjusted = _finite(position.get("last_adjusted_mark"), positive=True)
    if adjusted is None:
        raise ReplayError(f"position has no prior finite mark: {position['code']}")
    return float(position["entry_notional"]) * adjusted / float(
        position["entry_adjusted_open"]
    )


def _state_route_by_day(state_rows: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in state_rows:
        day = str(row.get("trade_date") or "")
        features = row.get("features") or {}
        if not day or day in result:
            raise ReplayError("market-state date is missing or duplicated")
        result[day] = route_state(
            str(features.get("confirmed_state") or "UNKNOWN"),
            str(features.get("raw_state") or "UNKNOWN"),
        )
    return result


def _candidate_events(
    events: Iterable[Mapping[str, Any]], track: str,
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in events:
        threshold = str(raw.get("threshold_eligible") or "").lower() == "true"
        primary = str(raw.get("primary_route_eligible") or "").lower() == "true"
        if not threshold or (track != "unconditioned_same_signal_same_execution" and not primary):
            continue
        row = dict(raw)
        day = str(row.get("entry_session") or "")
        if not day or not str(row.get("stable_id") or ""):
            raise ReplayError("eligible event lacks entry identity")
        result[day].append(row)
    for day in result:
        result[day].sort(key=lambda row: (
            int(row.get("threshold_rank") or 10**9), str(row.get("issuer_code") or ""),
        ))
    return result


def _fit_quantity(
    maximum_notional: float, *, opening: float, cash_available: float,
    day: pd.Timestamp, lagged_median: float, cost_bps: int,
) -> tuple[int, dict[str, float] | None]:
    shares = int(maximum_notional / opening / LOT_SIZE) * LOT_SIZE
    while shares > 0:
        notional = shares * opening
        costs = trade_costs(
            notional, side="buy", trade_date=day,
            lagged_median_turnover=lagged_median, cost_bps=cost_bps,
        )
        if notional + costs["total"] <= cash_available + 1e-9:
            return shares, costs
        shares -= LOT_SIZE
    return 0, None


def replay(
    events: Iterable[Mapping[str, Any]], *, book: MarketBook,
    industries: IndustryBook, state_rows: Iterable[Mapping[str, Any]],
    track: str, cost_bps: int, initial_capital: float = INITIAL_CAPITAL,
) -> dict[str, Any]:
    if track not in TRACK_CAPS or cost_bps not in {10, 20}:
        raise ReplayError("replay track or cost scenario is invalid")
    state_by_day = _state_route_by_day(state_rows)
    event_by_day = _candidate_events(events, track)
    cash = float(initial_capital)
    positions: dict[str, dict[str, Any]] = {}
    pending_exits: dict[str, str] = {}
    ledger: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    rejections: dict[str, int] = defaultdict(int)
    eligible_events = sum(map(len, event_by_day.values()))
    filled_entries = 0
    delayed_exits = 0
    total_costs = 0.0
    total_notional = 0.0
    previous_routed_state = "unknown"

    for session_index, day in enumerate(book.sessions):
        day_text = day.date().isoformat()
        if day < pd.Timestamp("2018-01-01"):
            previous_routed_state = state_by_day.get(day_text, previous_routed_state)
            continue
        if day > pd.Timestamp("2025-12-31"):
            continue

        delayed_exits_today = 0
        for code in sorted(list(pending_exits)):
            position = positions.get(code)
            if position is None:
                pending_exits.pop(code, None)
                continue
            row = book.row(position["stable_id"], day)
            if (
                row is None or int(row.get("trade_status") or 0) != 1
                or _one_price_lock(row, code=code, day=day, side="sell")
            ):
                delayed_exits += 1
                delayed_exits_today += 1
                continue
            lagged_median = _finite(row.get("lagged_median_turnover"), positive=True)
            opening = _finite(row.get("raw_open"), positive=True)
            adjusted_open = _finite(row.get("adjusted_open"), positive=True)
            if opening is None or adjusted_open is None:
                delayed_exits += 1
                delayed_exits_today += 1
                continue
            if lagged_median is None:
                lagged_median = position["entry_lagged_median"]
            gross = _mark(position, row, "adjusted_open")
            costs = trade_costs(
                gross, side="sell", trade_date=day,
                lagged_median_turnover=lagged_median, cost_bps=cost_bps,
            )
            cash += gross - costs["total"]
            total_costs += costs["total"]
            total_notional += gross
            pnl = gross - costs["total"] - position["entry_notional"] - position["buy_cost"]
            ledger.append({
                "trade_date": day_text, "code": code, "side": "sell",
                "notional_yuan": gross, "costs": costs,
                "reason": pending_exits[code],
            })
            trades.append({
                "code": code, "entry_trade_date": position["entry_trade_date"],
                "exit_trade_date": day_text, "exit_reason": pending_exits[code],
                "realized_pnl_yuan": pnl,
            })
            del positions[code]
            del pending_exits[code]

        open_values: dict[str, float] = {}
        for code, position in positions.items():
            row = book.row(position["stable_id"], day)
            adjusted_open = None if row is None else _finite(
                row.get("adjusted_open"), positive=True,
            )
            open_values[code] = (
                _carried_mark(position)
                if adjusted_open is None else _mark(position, row, "adjusted_open")
            )
        equity_open = cash + sum(open_values.values())
        open_routed_state = previous_routed_state
        cap = (
            0.80 if track == "unconditioned_same_signal_same_execution"
            else TRACK_CAPS[track].get(open_routed_state, 0.0)
        )
        raw_candidates = event_by_day.get(day_text, [])
        candidates: list[dict[str, Any]] = []
        accepted_stable_ids = [position["stable_id"] for position in positions.values()]
        for event in raw_candidates:
            code = str(event["issuer_code"])
            stable_id = str(event["stable_id"])
            if code in positions:
                rejections["already_held"] += 1
                continue
            row = book.row(stable_id, day)
            if (
                row is None or int(row.get("trade_status") or 0) != 1
                or bool(row.get("is_st"))
                or _one_price_lock(row, code=code, day=day, side="buy")
            ):
                rejections["entry_not_executable"] += 1
                continue
            opening = _finite(row.get("raw_open"), positive=True)
            adjusted_open = _finite(row.get("adjusted_open"), positive=True)
            market_lagged_median = _finite(
                row.get("lagged_median_turnover"), positive=True,
            )
            event_opening = _finite(event.get("raw_open"), positive=True)
            event_adjusted_open = _finite(event.get("adjusted_open"), positive=True)
            event_lagged_median = _finite(
                event.get("lagged_median_turnover_yuan"), positive=True,
            )
            if None in {
                opening, adjusted_open, market_lagged_median,
                event_opening, event_adjusted_open, event_lagged_median,
            }:
                rejections["entry_fact_missing"] += 1
                continue
            if (
                str(event.get("alias") or "") != str(row.get("alias") or "")
                or str(row.get("alias") or "").split(".", 1)[0] != code
                or not _same_number(opening, event_opening)
                or not _same_number(adjusted_open, event_adjusted_open)
                or not _same_number(market_lagged_median, event_lagged_median)
            ):
                raise ReplayError("entry epoch and market panel identity disagree")
            if (
                track != "unconditioned_same_signal_same_execution"
                and str(event.get("routed_state") or "unknown") != open_routed_state
            ):
                raise ReplayError("entry event market state does not match prior close")
            volatility = book.volatility(stable_id, session_index)
            if volatility is None:
                rejections["trailing_volatility_incomplete"] += 1
                continue
            industry = industries.at(code, day)
            if industry is None:
                rejections["industry_missing"] += 1
                continue
            correlated = False
            for other in accepted_stable_ids:
                correlation = book.correlation(stable_id, other, session_index)
                if correlation is None:
                    rejections["correlation_history_incomplete"] += 1
                    correlated = True
                    break
                if correlation >= CORRELATION_CAP:
                    rejections["correlation_at_least_0_80"] += 1
                    correlated = True
                    break
            if correlated:
                continue
            candidate = dict(event)
            candidate.update({
                "_row": row, "_volatility": volatility, "_industry": industry,
                "_opening": opening, "_adjusted_open": adjusted_open,
                "_lagged_median": event_lagged_median,
            })
            candidates.append(candidate)
            accepted_stable_ids.append(stable_id)

        existing_inverse_vol = 0.0
        existing_volatility_incomplete = False
        if candidates:
            for position in positions.values():
                volatility = book.volatility(position["stable_id"], session_index)
                if volatility is None:
                    existing_volatility_incomplete = True
                    break
                existing_inverse_vol += 1.0 / volatility
        if existing_volatility_incomplete:
            rejections["existing_position_trailing_volatility_incomplete"] += len(candidates)
            candidates = []
        inverse_total = existing_inverse_vol + sum(
            1.0 / candidate["_volatility"] for candidate in candidates
        )
        sector_values: dict[str, float] = defaultdict(float)
        for code, value in open_values.items():
            sector_values[positions[code]["industry"]] += value
        gross_open = sum(open_values.values())
        for candidate in candidates:
            code = str(candidate["issuer_code"])
            opening = candidate["_opening"]
            adjusted_open = candidate["_adjusted_open"]
            lagged_median = candidate["_lagged_median"]
            risk_target = (
                0.0 if inverse_total <= 0 else
                equity_open * cap * (1.0 / candidate["_volatility"]) / inverse_total
            )
            maximum = min(
                risk_target,
                equity_open * SINGLE_NAME_CAP,
                max(0.0, equity_open * cap - gross_open),
                max(0.0, equity_open * SECTOR_CAP - sector_values[candidate["_industry"]]),
                lagged_median * LIQUIDITY_CAP,
            )
            cash_available = max(0.0, cash - equity_open * CASH_BUFFER)
            shares, costs = _fit_quantity(
                maximum, opening=opening, cash_available=cash_available, day=day,
                lagged_median=lagged_median, cost_bps=cost_bps,
            )
            if shares <= 0 or costs is None:
                rejections["zero_lots_or_cash_capacity"] += 1
                continue
            notional = shares * opening
            debit = notional + costs["total"]
            cash -= debit
            total_costs += costs["total"]
            total_notional += notional
            gross_open += notional
            sector_values[candidate["_industry"]] += notional
            filled_entries += 1
            positions[code] = {
                "code": code, "stable_id": str(candidate["stable_id"]),
                "industry": candidate["_industry"], "shares": shares,
                "entry_trade_date": day_text, "entry_notional": notional,
                "entry_adjusted_open": adjusted_open, "buy_cost": costs["total"],
                "entry_lagged_median": lagged_median, "holding_closes": 0,
                "last_adjusted_mark": adjusted_open,
            }
            ledger.append({
                "trade_date": day_text, "code": code, "side": "buy",
                "notional_yuan": notional, "costs": costs,
                "announcement_id": candidate.get("announcement_id"),
            })

        close_values: dict[str, float] = {}
        reserve = 0.0
        for code, position in positions.items():
            row = book.row(position["stable_id"], day)
            adjusted_close = None if row is None else _finite(
                row.get("adjusted_close"), positive=True,
            )
            if adjusted_close is None:
                value = _carried_mark(position)
            else:
                value = _mark(position, row, "adjusted_close")
                position["last_adjusted_mark"] = adjusted_close
            close_values[code] = value
            lagged_median = None if row is None else _finite(
                row.get("lagged_median_turnover"), positive=True,
            )
            if lagged_median is None:
                lagged_median = position["entry_lagged_median"]
            reserve += trade_costs(
                value, side="sell", trade_date=day,
                lagged_median_turnover=lagged_median, cost_bps=cost_bps,
            )["total"]
            position["holding_closes"] += 1

        completed_routed_state = state_by_day.get(day_text, previous_routed_state)
        if track != "unconditioned_same_signal_same_execution" and completed_routed_state in {
            "risk_closed", "retreat",
        }:
            for code in positions:
                pending_exits.setdefault(code, "market_state_risk_exit")
        for code, position in positions.items():
            if position["holding_closes"] >= 20:
                pending_exits.setdefault(code, "twentieth_holding_close")

        gross_close = sum(close_values.values())
        equity_close = cash + gross_close - reserve
        sector_close: dict[str, float] = defaultdict(float)
        for code, value in close_values.items():
            sector_close[positions[code]["industry"]] += value
        records.append({
            "trade_date": day_text, "cash_yuan": cash, "gross_market_value": gross_close,
            "reserved_exit_cost_yuan": reserve, "equity": equity_close,
            "exposure_pct": 0.0 if equity_close <= 0 else gross_close / equity_close * 100,
            "cash_weight_pct": 0.0 if equity_close <= 0 else cash / equity_close * 100,
            "positions": len(positions), "market_state": completed_routed_state,
            "eligible_events": len(raw_candidates),
            "exit_delay_attempts": delayed_exits_today,
            "largest_sector_share_of_gross": (
                0.0 if gross_close <= 0 else max(sector_close.values(), default=0.0) / gross_close
            ),
            "entries_filled": sum(
                1 for entry in ledger
                if entry["side"] == "buy" and entry["trade_date"] == day_text
            ),
        })
        previous_routed_state = completed_routed_state

    return {
        "status": "replay_complete",
        "track": track, "cost_bps": cost_bps, "initial_capital": initial_capital,
        "records": records, "ledger": ledger, "completed_trades": trades,
        "open_positions": positions, "pending_exits": pending_exits,
        "eligible_events": eligible_events, "filled_entries": filled_entries,
        "entry_fill_rate": 0.0 if eligible_events == 0 else filled_entries / eligible_events,
        "delayed_exits": delayed_exits, "rejections": dict(sorted(rejections.items())),
        "cumulative_costs_yuan": total_costs,
        "cumulative_traded_notional_yuan": total_notional,
    }


def evaluate_period(
    replay_result: Mapping[str, Any], start: str, end: str,
) -> dict[str, Any]:
    records = list(replay_result.get("records") or [])
    selected = [row for row in records if start <= row["trade_date"] <= end]
    if not selected:
        return {"status": "period_empty", "start": start, "end": end}
    earlier = [row for row in records if row["trade_date"] < start]
    start_nav = float(earlier[-1]["equity"]) if earlier else float(
        replay_result.get("initial_capital") or INITIAL_CAPITAL
    )
    nav = np.asarray([float(row["equity"]) for row in selected], dtype="float64")
    prior = np.concatenate(([start_nav], nav[:-1]))
    daily = nav / prior - 1.0
    path = np.concatenate(([start_nav], nav))
    drawdown = path / np.maximum.accumulate(path) - 1.0
    total_return = float(nav[-1] / start_nav - 1.0)
    cagr = float((nav[-1] / start_nav) ** (242.0 / len(nav)) - 1.0) if nav[-1] > 0 else -1.0
    maximum_drawdown = float(drawdown.min())
    std = float(np.std(daily, ddof=1)) if len(daily) > 1 else 0.0
    negative = daily[daily < 0]
    downside = float(np.std(negative, ddof=1)) if len(negative) > 1 else 0.0
    trades = [
        row for row in (replay_result.get("completed_trades") or [])
        if start <= row["exit_trade_date"] <= end
    ]
    profits = [float(row["realized_pnl_yuan"]) for row in trades if row["realized_pnl_yuan"] > 0]
    losses = [float(row["realized_pnl_yuan"]) for row in trades if row["realized_pnl_yuan"] < 0]
    ledger = [
        row for row in (replay_result.get("ledger") or [])
        if start <= row["trade_date"] <= end
    ]
    costs = sum(float(row["costs"]["total"]) for row in ledger)
    start_reserve = float(earlier[-1].get("reserved_exit_cost_yuan") or 0.0) if earlier else 0.0
    end_reserve = float(selected[-1].get("reserved_exit_cost_yuan") or 0.0)
    notional = sum(float(row["notional_yuan"]) for row in ledger)
    frame = pd.DataFrame(selected)
    frame["daily_return"] = daily
    eligible_events = int(frame.get("eligible_events", pd.Series(dtype="int64")).sum())
    entries_filled = int(frame.get("entries_filled", pd.Series(dtype="int64")).sum())
    daily_eligible = frame.get("eligible_events", pd.Series(0, index=frame.index)).astype(int)
    calmar_positive_infinity = maximum_drawdown == 0 and cagr > 0
    return {
        "status": "period_metrics_ready", "start": start, "end": end,
        "sessions": len(selected), "period_start_nav": start_nav,
        "period_end_nav": float(nav[-1]), "net_total_return": total_return,
        "net_CAGR": cagr, "maximum_drawdown": maximum_drawdown,
        "Sharpe": 0.0 if std == 0 else float(np.mean(daily) / std * math.sqrt(242)),
        "Sortino": 0.0 if downside == 0 else float(np.mean(daily) / downside * math.sqrt(242)),
        "Calmar": (
            None if calmar_positive_infinity else
            (0.0 if maximum_drawdown == 0 else cagr / abs(maximum_drawdown))
        ),
        "Calmar_positive_infinity": calmar_positive_infinity,
        "round_trips": len(trades),
        "win_rate": 0.0 if not trades else len(profits) / len(trades),
        "profit_factor": None if not losses else sum(profits) / abs(sum(losses)),
        "profit_factor_positive_infinity": bool(profits and not losses),
        "turnover": notional / max(float(np.mean(nav)), 1e-12),
        "cost_drag": (costs + end_reserve - start_reserve) / max(start_nav, 1e-12),
        "average_exposure": float(frame["exposure_pct"].mean()),
        "cash_weight": float(frame["cash_weight_pct"].mean()),
        "entry_fill_rate": 0.0 if eligible_events == 0 else entries_filled / eligible_events,
        "unfilled_entry_count": eligible_events - entries_filled,
        "forced_exit_delay": int(
            frame.get("exit_delay_attempts", pd.Series(dtype="int64")).sum()
        ),
        "event_clustering": {
            "eligible_events": eligible_events,
            "sessions_with_eligible_events": int((daily_eligible > 0).sum()),
            "maximum_eligible_events_in_one_session": int(daily_eligible.max()),
        },
        "largest_sector_share": float(frame["largest_sector_share_of_gross"].max()),
        "state_attribution": {
            str(state): float(np.prod(1.0 + values.to_numpy()) - 1.0)
            for state, values in frame.groupby("market_state")["daily_return"]
        },
    }


def evaluate_primary_gates(
    base_primary: Mapping[str, Any], base_fixed: Mapping[str, Any],
    stress_primary: Mapping[str, Any],
) -> dict[str, Any]:
    folds = {
        "development": ("2018-01-01", "2022-12-31"),
        "validation": ("2023-01-01", "2024-12-31"),
        "sealed_test": ("2025-01-01", "2025-12-31"),
    }
    primary = {name: evaluate_period(base_primary, *dates) for name, dates in folds.items()}
    fixed = {name: evaluate_period(base_fixed, *dates) for name, dates in folds.items()}
    stress = {name: evaluate_period(stress_primary, *dates) for name, dates in folds.items()}
    sealed, sealed_fixed = primary["sealed_test"], fixed["sealed_test"]
    validation = primary["validation"]
    yearly = {
        str(year): evaluate_period(base_primary, f"{year}-01-01", f"{year}-12-31")[
            "net_total_return"
        ]
        for year in range(2018, 2026)
    }
    profit_factor = sealed.get("profit_factor")
    validation_calmar_pass = (
        validation.get("Calmar_positive_infinity") is True
        or (validation.get("Calmar") is not None and validation["Calmar"] >= 0.5)
    )
    sealed_calmar_pass = (
        sealed.get("Calmar_positive_infinity") is True
        or (sealed.get("Calmar") is not None and sealed["Calmar"] >= 0.75)
    )
    gates = {
        "validation_net_total_return_positive": validation["net_total_return"] > 0,
        "validation_Calmar_at_least_0_5": validation_calmar_pass,
        "sealed_test_net_total_return_positive": sealed["net_total_return"] > 0,
        "sealed_test_Calmar_at_least_0_75": sealed_calmar_pass,
        "sealed_test_profit_factor_at_least_1_2": (
            sealed.get("profit_factor_positive_infinity") is True
            or (profit_factor is not None and profit_factor >= 1.2)
        ),
        "sealed_test_maximum_drawdown_abs_pct_at_most_20": abs(
            sealed["maximum_drawdown"]
        ) <= 0.20,
        "stress_20bp_sealed_test_net_total_return_positive": stress[
            "sealed_test"
        ]["net_total_return"] > 0,
        "full_period_round_trips_at_least_50": len(
            base_primary.get("completed_trades") or []
        ) >= 50,
        "positive_calendar_years_at_least_3": sum(value > 0 for value in yearly.values()) >= 3,
        "sealed_test_net_return_above_fixed_exposure_control": sealed[
            "net_total_return"
        ] > sealed_fixed["net_total_return"],
        "sealed_test_maximum_drawdown_not_worse_than_fixed_exposure_control": abs(
            sealed["maximum_drawdown"]
        ) <= abs(sealed_fixed["maximum_drawdown"]),
    }
    return {
        "status": "historical_primary_gates_evaluated",
        "all_passed": all(gates.values()), "gates": gates,
        "positive_calendar_years": sum(value > 0 for value in yearly.values()),
        "calendar_year_returns": yearly,
        "fold_metrics": {"base_primary": primary, "base_fixed": fixed, "stress_primary": stress},
    }
