"""Capture the next-session open for the strict research portfolio replay.

This is deliberately separate from the live/manual account.  It consumes the
previous trading day's two immutable joint decision packets, takes one narrow
opening snapshot, and advances only the 200,000 yuan research paper state.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trade_assistant.candles import completed_signal_candles

from .market_state_daily import PROJECT_ROOT, _atomic_publish, _canonical_bytes
from .verified_portfolio_execution import (
    execute_verified_portfolio_day,
    verify_verified_portfolio_execution_packet,
)


SH_TZ = ZoneInfo("Asia/Shanghai")
WINDOW_START = time(9, 35)
WINDOW_END = time(9, 38)
REFERENCE_CAPITAL_YUAN = 200_000.0
EXECUTION_DIR = Path("research/data/verified_portfolio_execution/v1")
DECISION_DIR = Path(
    "research/data/market_sector_route_forward/v1/joint_replay_packets"
)
CALENDAR_PATH = Path("reports/current_market_calendar.json")
FULL_MARKET_PATH = Path("research/full_market_top300.json")
COST_MODEL_PATH = Path("quantqc/costs.py")
MARKET_INPUT_DIR = EXECUTION_DIR / "market_inputs"
SLOTS = ("morning_1030", "afternoon_1400")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ref(path: Path, root: Path) -> dict[str, str]:
    return {
        "path": str(path.absolute().relative_to(root.absolute())),
        "sha256": _sha(path),
    }


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _archive_full_market_input(
    path: Path, root: Path, *, expected_data_asof: str,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None, None
    if (
        not isinstance(value, dict)
        or str(value.get("data_asof") or "") != expected_data_asof
        or not isinstance(value.get("eligible"), list)
    ):
        return None, None
    digest = hashlib.sha256(raw).hexdigest()
    archived = root / MARKET_INPUT_DIR / f"{digest}.json"
    _atomic_publish(archived, raw)
    return value, _ref(archived, root)


def _resolve_archived_full_market_ref(
    ref: object, root: Path,
) -> tuple[Path | None, str | None]:
    if not isinstance(ref, dict):
        return None, "full_market_ref_invalid"
    digest = str(ref.get("sha256") or "")
    if not _SHA256.fullmatch(digest):
        return None, "full_market_sha_invalid"
    try:
        path = (root / str(ref.get("path") or "")).resolve()
        parent = (root / MARKET_INPUT_DIR).resolve()
        path.relative_to(parent)
        raw = path.read_bytes()
    except (OSError, ValueError):
        return None, "full_market_archive_unreadable"
    if (
        path.parent != parent or path.name != f"{digest}.json"
        or hashlib.sha256(raw).hexdigest() != digest
    ):
        return None, "full_market_archive_identity_invalid"
    return path, None


def verify_forward_execution_packet(
    packet: object, *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Verify strict execution plus the immutable lagged-ADV source bytes."""
    checked = verify_verified_portfolio_execution_packet(
        packet, project_root=project_root,
    )
    if checked.get("valid") is not True or not isinstance(packet, dict):
        return checked
    wrapper = packet.get("verified_wrapper")
    market = wrapper.get("market_snapshot") if isinstance(wrapper, dict) else None
    if not isinstance(market, dict):
        return {"valid": False, "errors": ["forward_market_snapshot_missing"]}
    ref = market.get("full_market_snapshot_ref")
    path, error = _resolve_archived_full_market_ref(ref, project_root)
    if error or path is None:
        return {"valid": False, "errors": [error or "full_market_ref_invalid"]}
    full_market = _read_json(path)
    previous = str(market.get("previous_trade_date") or "")
    errors: list[str] = []
    if (
        full_market is None
        or str(full_market.get("data_asof") or "") != previous
        or not isinstance(full_market.get("eligible"), list)
    ):
        errors.append("full_market_archive_content_invalid")
        rows: dict[str, dict[str, Any]] = {}
    else:
        rows = {
            str(row.get("code")): row
            for row in full_market["eligible"]
            if isinstance(row, dict) and row.get("code")
        }
    for code, quote in (market.get("quotes") or {}).items():
        if not isinstance(quote, dict):
            errors.append(f"forward_quote_invalid:{code}")
            continue
        row = rows.get(str(code)) or {}
        if quote.get("lagged_adv_source_ref") != ref:
            errors.append(f"lagged_adv_ref_mismatch:{code}")
        if _finite(quote.get("lagged_adv_yuan")) != _finite(
            row.get("avg_amount_20d")
        ):
            errors.append(f"lagged_adv_value_mismatch:{code}")
    return {
        **checked,
        "valid": not errors,
        "errors": sorted(set(errors)),
        "packet": packet if not errors else None,
        "packet_id": packet.get("packet_id") if not errors else None,
    }


def _calendar_day(
    observed_at: datetime, root: Path,
) -> tuple[str | None, str | None, dict[str, str] | None, str | None]:
    path = root / CALENDAR_PATH
    calendar = _read_json(path)
    if calendar is None:
        return None, None, None, "calendar_unreadable"
    dates = calendar.get("trading_dates")
    if (
        calendar.get("source") != "BaoStock query_trade_dates"
        or not isinstance(dates, list) or dates != sorted(set(dates))
        or not dates
        or (calendar.get("coverage_end") or calendar.get("end_date")) != dates[-1]
    ):
        return None, None, None, "calendar_identity_invalid"
    trade_date = observed_at.date().isoformat()
    try:
        index = dates.index(trade_date)
    except ValueError:
        return trade_date, None, _ref(path, root), "not_a_trading_day"
    if index <= 0:
        return trade_date, None, _ref(path, root), "previous_trade_date_missing"
    return trade_date, str(dates[index - 1]), _ref(path, root), None


def execution_capture_schedule_status(
    *, now: datetime | None = None, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    observed_at = (now or datetime.now(SH_TZ)).astimezone(SH_TZ)
    trade_date, previous, _calendar, error = _calendar_day(
        observed_at, project_root,
    )
    output = project_root / EXECUTION_DIR / f"{trade_date}.json"
    if output.is_file():
        phase = "captured"
    elif error == "not_a_trading_day":
        phase = "closed"
    elif error:
        phase = "identity_invalid"
    elif observed_at.time() < WINDOW_START:
        phase = "before_window"
    elif observed_at.time() <= WINDOW_END:
        phase = "window_open"
    else:
        phase = "missed"
    return {
        "phase": phase,
        "trade_date": trade_date,
        "previous_trade_date": previous,
        "window": "09:35-09:38 Asia/Shanghai",
        "error": None if error == "not_a_trading_day" else error,
        "research_only": True,
        "changes_live_account": False,
        "automatic_trading": False,
    }


def _initial_state(previous_trade_date: str, cost_sha: str) -> dict[str, Any]:
    return {
        "schema": "joint_execution_portfolio_state.v1",
        "last_trade_date": previous_trade_date,
        "cash_yuan": REFERENCE_CAPITAL_YUAN,
        "cost_model_id": cost_sha,
        "positions": {},
        "pending_exit_orders": [],
        "cumulative_costs": {
            "commission": 0.0, "transfer": 0.0, "stamp": 0.0,
            "slippage": 0.0, "market_impact": 0.0, "total": 0.0,
        },
        "cumulative_buy_notional": 0.0,
        "cumulative_sell_notional": 0.0,
    }


def _previous_state(
    previous_trade_date: str, cost_sha: str, root: Path,
) -> tuple[dict[str, Any] | None, dict[str, str] | None, str | None]:
    directory = root / EXECUTION_DIR
    previous_path = directory / f"{previous_trade_date}.json"
    if previous_path.is_file():
        packet = _read_json(previous_path)
        checked = verify_forward_execution_packet(
            packet, project_root=root,
        )
        verified = checked.get("packet") if isinstance(checked, dict) else None
        state = verified.get("state_after") if isinstance(verified, dict) else None
        if checked.get("valid") is not True or not isinstance(state, dict):
            return None, None, "previous_execution_packet_invalid"
        return state, _ref(previous_path, root), None
    existing = sorted(directory.glob("????-??-??.json")) if directory.is_dir() else []
    if existing:
        return None, None, "previous_execution_packet_missing_no_reseed"
    state = _initial_state(previous_trade_date, cost_sha)
    initial_path = directory / f"initial-{previous_trade_date}.json"
    _atomic_publish(initial_path, _canonical_bytes({
        "schema": "verified_portfolio_execution.initial_zero_state.v1",
        "state": state,
    }))
    return state, _ref(initial_path, root), None


def _decision_refs(
    previous_trade_date: str, root: Path,
) -> tuple[list[dict[str, str]], dict[str, Any] | None, str | None]:
    paths = [
        root / DECISION_DIR / f"{previous_trade_date}-{slot}.json"
        for slot in SLOTS
    ]
    if any(not path.is_file() for path in paths):
        return [], None, "waiting_for_complete_previous_decision_day"
    latest = _read_json(paths[-1])
    if latest is None:
        return [], None, "latest_decision_unreadable"
    return [_ref(path, root) for path in paths], latest, None


def _codes(latest: dict[str, Any], state: dict[str, Any]) -> list[str]:
    plans = (latest.get("decision") or {}).get("pre_execution_plans") or []
    selected = {
        str(row.get("stock_code") or "")
        for row in plans if isinstance(row, dict)
    }
    selected.update(str(code) for code in (state.get("positions") or {}))
    return sorted(code for code in selected if code)


def _history_proves_price_limit_identity(
    candle_provider: Any, code: str, *, observed_at: datetime,
    previous_trade_date: str,
) -> bool:
    try:
        candles = candle_provider.get_candles(code, 35, interval="1d")
        completed = completed_signal_candles(
            candles, "1d", observed_at=observed_at,
        )
        dates = [str(row.time)[:10] for row in completed]
    except Exception:
        return False
    return len(dates) >= 20 and max(dates) <= previous_trade_date


def _market_day(
    codes: list[str], *, quote_provider: Any, candle_provider: Any,
    observed_at: datetime, trade_date: str, previous_trade_date: str,
    full_market: dict[str, Any], full_market_ref: dict[str, str],
) -> dict[str, Any]:
    rows = {
        str(row.get("code")): row
        for row in (full_market.get("eligible") or [])
        if isinstance(row, dict) and row.get("code")
    }
    try:
        live = quote_provider.get_quotes(codes) if codes else {}
        quote_error = None
    except Exception as exc:
        live = {}
        quote_error = type(exc).__name__
    quotes: dict[str, dict[str, Any]] = {}
    for code in codes:
        source_row = rows.get(code) or {}
        quote = live.get(code) if isinstance(live, dict) else None
        exchange_time = str(getattr(quote, "exchange_data_time", "") or "")
        fresh = (
            exchange_time[:10] == trade_date
            and exchange_time <= (observed_at + timedelta(seconds=30)).isoformat()
        )
        open_price = _finite(getattr(quote, "open_price", None)) if fresh else None
        current_price = _finite(getattr(quote, "price", None)) if fresh else None
        source_close = _finite(source_row.get("close"))
        previous_close = (
            _finite(getattr(quote, "previous_close", None)) if fresh else None
        ) or source_close
        name = str(getattr(quote, "name", "") or source_row.get("name") or "")
        quotes[code] = {
            "open_price": open_price,
            "prev_close": previous_close,
            "lagged_adv_yuan": _finite(source_row.get("avg_amount_20d")),
            "tradable": bool(
                fresh and open_price is not None and open_price > 0
                and current_price is not None and current_price > 0
            ),
            "is_risk_warning": ("ST" in name.upper()) if name else None,
            "no_price_limit": (
                False if _history_proves_price_limit_identity(
                    candle_provider, code, observed_at=observed_at,
                    previous_trade_date=previous_trade_date,
                ) else None
            ),
            "quote_source": str(getattr(quote, "source", "") or "unavailable"),
            "exchange_data_time": exchange_time or None,
            "lagged_adv_source_ref": full_market_ref,
        }
    market = {
        "schema": "joint_execution_market_day.v1",
        "trade_date": trade_date,
        "previous_trade_date": previous_trade_date,
        "captured_at": observed_at.isoformat(timespec="seconds"),
        "capture_window": "09:35-09:38 Asia/Shanghai",
        "no_backfill": True,
        "quotes": quotes,
        "full_market_snapshot_ref": full_market_ref,
        "quote_error": quote_error,
        "research_only": True,
    }
    market["market_snapshot_id"] = hashlib.sha256(
        _canonical_bytes(market),
    ).hexdigest()
    return market


def capture_verified_portfolio_execution_forward(
    quote_provider: Any, candle_provider: Any, *, now: datetime | None = None,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Capture once inside the frozen opening window; never backfill."""
    observed_at = (now or datetime.now(SH_TZ)).astimezone(SH_TZ)
    schedule = execution_capture_schedule_status(
        now=observed_at, project_root=project_root,
    )
    trade_date = str(schedule.get("trade_date") or "")
    output = project_root / EXECUTION_DIR / f"{trade_date}.json"
    if schedule["phase"] == "captured":
        packet = _read_json(output)
        checked = verify_forward_execution_packet(
            packet, project_root=project_root,
        )
        return {
            **schedule, "captured": checked.get("valid") is True,
            "status": "already_captured" if checked.get("valid") is True else "identity_invalid",
            "errors": checked.get("errors") or [],
        }
    if schedule["phase"] != "window_open":
        return {**schedule, "captured": False, "status": schedule["phase"]}
    previous = str(schedule.get("previous_trade_date") or "")
    decisions, latest, decision_error = _decision_refs(previous, project_root)
    if decision_error:
        return {**schedule, "captured": False, "status": decision_error}
    cost_path = project_root / COST_MODEL_PATH
    if not cost_path.is_file():
        return {**schedule, "captured": False, "status": "cost_model_unreadable"}
    cost_ref = _ref(cost_path, project_root)
    state, state_ref, state_error = _previous_state(
        previous, cost_ref["sha256"], project_root,
    )
    if state_error or state is None or state_ref is None:
        return {**schedule, "captured": False, "status": state_error}
    full_market_path = project_root / FULL_MARKET_PATH
    full_market, full_market_ref = _archive_full_market_input(
        full_market_path, project_root, expected_data_asof=previous,
    )
    if (
        full_market is None
        or str(full_market.get("data_asof") or "") != previous
        or not isinstance(full_market.get("eligible"), list)
        or full_market_ref is None
    ):
        return {
            **schedule, "captured": False,
            "status": "lagged_full_market_snapshot_unavailable",
        }
    market = _market_day(
        _codes(latest or {}, state), quote_provider=quote_provider,
        candle_provider=candle_provider, observed_at=observed_at,
        trade_date=trade_date, previous_trade_date=previous,
        full_market=full_market, full_market_ref=full_market_ref,
    )
    result = execute_verified_portfolio_day(
        state, decisions, market, calendar_ref=_ref(
            project_root / CALENDAR_PATH, project_root,
        ), previous_state_ref=state_ref, cost_model_ref=cost_ref,
        project_root=project_root,
    )
    if result.get("executed") is not True or not isinstance(result.get("packet"), dict):
        return {
            **schedule, "captured": False,
            "status": str(result.get("status") or "execution_failed"),
            "errors": result.get("errors") or [],
        }
    packet = result["packet"]
    checked = verify_forward_execution_packet(
        packet, project_root=project_root,
    )
    if checked.get("valid") is not True:
        return {
            **schedule, "captured": False, "status": "output_verification_failed",
            "errors": checked.get("errors") or [],
        }
    _atomic_publish(output, _canonical_bytes(packet))
    return {
        **schedule, "captured": True, "status": "captured",
        "packet_id": packet.get("packet_id"),
        "output_ref": _ref(output, project_root),
        "ledger_events": len(packet.get("ledger") or []),
        "daily_costs": packet.get("daily_costs"),
        "stable_profit_proven": False,
    }


__all__ = [
    "capture_verified_portfolio_execution_forward",
    "execution_capture_schedule_status",
    "verify_forward_execution_packet",
]
