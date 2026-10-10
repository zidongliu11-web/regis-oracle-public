"""Bounded runtime for the isolated verified paper-execution capture."""

from __future__ import annotations

import copy
import hashlib
import threading
import traceback
from datetime import datetime
from typing import Any, Callable

from .market_state_daily import PROJECT_ROOT
from .strict_joint_forward_adapter import capture_strict_joint_forward_audit
from .verified_portfolio_execution_forward import (
    EXECUTION_DIR,
    REFERENCE_CAPITAL_YUAN,
    capture_verified_portfolio_execution_forward,
    execution_capture_schedule_status,
)


def new_capture_state() -> dict[str, Any]:
    return {
        "lock": threading.Lock(),
        "running": False,
        "last_error": None,
        "last_result": None,
        "attempt_trade_date": None,
        "attempt_count": 0,
    }


def public_runtime_state(
    state: dict[str, Any], *, now: datetime | None = None,
) -> dict[str, Any]:
    schedule = execution_capture_schedule_status(now=now)
    with state["lock"]:
        result = copy.deepcopy(state.get("last_result"))
        running = bool(state.get("running"))
        error = state.get("last_error")
    return {
        "schedule": schedule,
        "running": running,
        "last_result": result,
        "last_error": str(error)[:500] if error else None,
        "reference_capital_yuan": REFERENCE_CAPITAL_YUAN,
        "account_scope": "separate_research_paper_state",
        "changes_live_account": False,
        "automatic_trading": False,
        "stable_profit_proven": False,
    }


def _strict_audit(result: dict[str, Any]) -> dict[str, Any] | None:
    """Audit a verified paper packet without becoming its state source."""
    if result.get("captured") is not True:
        return None
    execution_ref = result.get("output_ref")
    if not isinstance(execution_ref, dict):
        trade_date = str(result.get("trade_date") or "")
        if len(trade_date) != 10:
            return {
                "status": "source_execution_ref_missing",
                "captured": False,
                "errors": ["trade_date_invalid"],
            }
        path = PROJECT_ROOT / EXECUTION_DIR / f"{trade_date}.json"
        if not path.is_file():
            return {
                "status": "source_execution_ref_missing",
                "captured": False,
                "errors": ["execution_packet_missing"],
            }
        execution_ref = {
            "path": str(path.resolve().relative_to(PROJECT_ROOT.resolve())),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    return capture_strict_joint_forward_audit(execution_ref)


def maybe_start_capture(
    state: dict[str, Any], *, quote_provider: Any, candle_provider: Any,
    start_worker: Callable[..., bool],
) -> bool:
    """Start at most two attempts in the immutable open window for one date."""
    schedule = execution_capture_schedule_status()
    trade_date = schedule.get("trade_date")
    with state["lock"]:
        if state.get("attempt_trade_date") != trade_date:
            state["attempt_trade_date"] = trade_date
            state["attempt_count"] = 0
        running = bool(state.get("running"))
        attempts = int(state.get("attempt_count") or 0)
    if schedule.get("phase") != "window_open" or running or attempts >= 2:
        return False

    def worker() -> None:
        try:
            result = capture_verified_portfolio_execution_forward(
                quote_provider, candle_provider,
            )
            try:
                strict_audit = _strict_audit(result)
            except Exception:
                strict_audit = {
                    "status": "audit_exception_fail_closed",
                    "captured": False,
                    "errors": [traceback.format_exc()[:1000]],
                }
            if strict_audit is not None:
                result = {**result, "strict_joint_audit": strict_audit}
            with state["lock"]:
                state["last_result"] = result
                state["last_error"] = None
        except Exception:
            with state["lock"]:
                state["last_error"] = traceback.format_exc()
        finally:
            with state["lock"]:
                state["running"] = False

    started = start_worker(
        state, worker, name="verified-paper-execution-capture",
    )
    if started:
        with state["lock"]:
            state["attempt_count"] += 1
    return bool(started)


__all__ = ["maybe_start_capture", "new_capture_state", "public_runtime_state"]
