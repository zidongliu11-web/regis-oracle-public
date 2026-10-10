#!/usr/bin/env python3
"""
Regis Oracle — A股量化研究工作台
端口 5200 · 独立站点 · 复用 5100 全部量化模块
"""
from __future__ import annotations

import copy
import csv
import gzip
import hashlib
import hmac
import json
import logging
import math
import os
import re
import sqlite3
import sys
import threading
import time as _time
import traceback as _traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from functools import lru_cache, wraps
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.exceptions import HTTPException

# 确保项目根在 import path 中
_PROJECT = Path(__file__).resolve().parent.parent
_PUBLIC_RELEASE_ID = "current_market_route_research_release_v1_amendment_83"
_LIGHTWEIGHT_CHARTS_ASSET = (
    _PROJECT / "static" / "vendor" / "lightweight-charts"
    / "lightweight-charts.standalone.production.js"
)
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))

from quantqc.costs import one_side_cost_rate, round_trip_cost_rate
from service_readiness import capability_readiness

from trade_assistant.candles import (
    CachedCandleProvider,
    Candle,
    CandleDataUnavailable,
    FallbackCandleProvider,
    TencentCandleProvider,
    completed_signal_candles,
    candle_price_origin,
    research_signals,
    technical_snapshot,
    technical_values,
)
from trade_assistant.deepseek import (
    PAID_API_DISABLED_MESSAGE,
)
from trade_assistant.market import (
    CachedQuoteProvider,
    FallbackQuoteProvider,
    MarketDataUnavailable,
    SinaBatchQuoteProvider,
    TencentBatchQuoteProvider,
)
from trade_assistant.risk import RISK_PROFILE, stock_is_eligible
from trade_assistant.stock_analysis import (
    build_stock_analysis,
    quant_stock_view,
)

from .signal_engine import (
    ACCOUNT_CAPITAL,
    generate_today_signals,
    get_portfolio_status,
    get_signal_history,
    is_recommendable_stock,
    _is_blacklisted,
)
from .http_runtime import begin_request, finish_response, internal_error_response, request_nonce
from .remote_web_login import (
    AUTH_COOKIE,
    LOGIN_CSRF_COOKIE,
    SESSION_MAX_AGE_SECONDS,
    issue_session,
    new_login_csrf,
    safe_next,
    valid_login_csrf,
    valid_session,
)
from .bounded_runtime import bounded_call as _run_bounded_call, health_snapshot as _bounded_health, native_timeout_health
from .full_market_runtime import audited_realtime_universe, audited_universe
from .data_semantics import (
    FLOW_MAGNITUDE_SCALE,
    MAX_INDUSTRY_EXPOSURE_PCT,
    MAX_OPEN_POSITIONS,
    MAX_PLANNED_LOSS_PER_NEW_POSITION_PCT,
    MAX_PORTFOLIO_OPEN_RISK_PCT,
    MAX_POSITIONS,
    MAX_SINGLE_POSITION_PCT,
    MAX_TOTAL_EXPOSURE_PCT,
    MIN_CASH_RESERVE_PCT,
    normalize_fund_flow_yi,
)
from .api_contracts import (
    _active_strategy_public_view,
    _compact_candidate_headline,
    _compact_decision_gate,
    _compact_live_signal_snapshot,
    _monitor_opportunities_snapshot,
    _compact_opportunities_snapshot,
    _compact_strategy_lab_evidence,
    _finite_number,
    _historical_promotion_verdict,
    _public_research_composite,
    _scanner_model_validation,
    _truthful_strategy_batch_status,
)
from .buy_tickets import (
    BUY_TICKETS,
    TicketConflict,
    TicketExpired,
    TicketInvalid,
)
from .factor_system_api import (
    _backtest_factor_payload,
    _factor_registry_error_response,
    _factor_forward_readiness,
    _factor_production_catalog_summary,
    _factor_registry_summary,
    _factor_research_workflow,
    _public_factor_row,
    _public_factor_status_counts,
    register_factor_system_routes,
)
from .research_evidence.strategy_lab_base import build_strategy_lab_base
from .research_evidence.system_base import build_system_evidence
from .research_evidence.base_decision import (
    build_base_decision_gate_status,
    build_base_historical_candidate_headline,
    build_base_profitability_research_evidence,
)
from .research_evidence import cash_floor_decision as _cash_floor_decision
from .position_sizing import sizing_summary, get_all_plans
from .risk_control import (
    build_manual_nav_curve,
    calculate_drawdown,
    calculate_volatility_managed_exposure,
    daily_history_evidence,
    get_risk_report,
    position_risk_actions,
)
from .remote_runtime import (
    is_loopback_host as _is_loopback_host,
    validated_bind_host as _validated_bind_host,
)
from .trading_calendar import (
    audited_trading_day_age,
    index_data_evidence,
    last_completed_trading_day,
    market_regime,
    next_trading_day_after,
    trading_day_status,
)
from .user_strategy_lab import (
    RUN_LOCK as _STRATEGY_RUN_LOCK,
    _acquire_forward_lock as _acquire_user_strategy_forward_lock,
    _release_forward_lock as _release_user_strategy_forward_lock,
    acquire_research_mutation_lock as _acquire_research_mutation_lock,
    assistant_for_run as user_strategy_assistant_for_run,
    BacktestBusyError,
    ForwardEvidenceError,
    batch_status as user_strategy_batch_status,
    create_strategy_experiment as user_strategy_create_experiment,
    register_strategy_experiment as user_strategy_register_experiment,
    load_backtest_curve_state as user_strategy_backtest_curve_state,
    load_paper_forward_state as user_strategy_paper_forward_state,
    load_backtest_report as load_user_strategy_backtest_report,
    publish_unified_diagnosis_report as user_strategy_publish_diagnosis_report,
    record_strategy_historical_test as user_strategy_record_historical_test,
    release_research_mutation_lock as _release_research_mutation_lock,
    research_mutation_lock_state as user_strategy_research_lock_state,
    refresh_paper_forward as user_strategy_refresh_paper_forward,
    save_strategy_diagnosis as user_strategy_save_diagnosis,
    save_strategy as save_user_strategy,
    set_promotion_action as user_strategy_set_promotion_action,
    start_batch_generation as user_strategy_start_batch,
    StrategyValidationError,
    run_backtest as run_user_strategy_backtest,
    validate_unified_diagnosis_input as user_strategy_validate_diagnosis_input,
    strategy_evaluator_fingerprint as user_strategy_evaluator_fingerprint,
    validate_spec as validate_user_strategy_spec,
    validate_draft_spec as validate_user_strategy_draft_spec,
    workbench_state as user_strategy_workbench_state,
)

def _research_mutation_route(view):
    """让所有 Web 研究写入与 epoch 迁移/批量生成共享同一跨进程锁。"""
    @wraps(view)
    def guarded(*args, **kwargs):
        handle = _acquire_research_mutation_lock(blocking=False)
        if handle is None:
            return jsonify({
                "ok": False,
                "error": "研究证据正在批量生成或迁移，请稍后重试",
                "research_lock": user_strategy_research_lock_state(),
            }), 409
        try:
            return view(*args, **kwargs)
        finally:
            _release_research_mutation_lock(handle)
    return guarded
from .signal_system import (  # 实时信号系统（Step 3）
    _live_strategy_validation_status,
    load_live_signals,
    run_once as _signal_run_once,
    tail_audit as _signal_tail_audit,
    load_watchlist as _signal_watchlist,
    load_realtime_universe as _signal_realtime_universe,
    watchlist_scope as _signal_watchlist_scope,
)
from .market_intelligence import MarketIntelligence
from .market_sector_strategy_adapter import adapt_market_observation
from .market_state_research_runtime import latest_research_state
from .private_fund_research import load_private_fund_scope
from .strategy_route_api import (
    build_research_route_payload,
    dashboard_research_route_payload,
    latest_route_market_observation,
)
from .system_route_joint_v2_forward import (
    DECISION_DIR as _STRICT_FORWARD_DECISION_DIR,
    PROTOCOL_PATH as _STRICT_FORWARD_PROTOCOL_PATH,
    system_route_joint_v2_forward_status,
)
from .system_route_joint_v2_gate import (
    ADMISSION_PATH as _STRICT_FORWARD_ADMISSION_PATH,
)
from .strict_alert_binding import (
    frozen_buy_action_plan as _frozen_buy_action_plan,
    strict_route_buy_provenance as _strict_route_buy_provenance,
    strict_route_opportunity_candidates as _strict_route_opportunity_candidates,
)
from .intraday_emotion_scope_v2 import (
    scoped_strict_route_alert_context as _strict_route_alert_context,
    enforce_current_full_a_entry_scope as _enforce_current_full_a_entry_scope,
)
from .trader_style_distillation import load_trader_style_distillation
from .runtime_lifecycle import BackgroundThreadGroup, stop_app_background
from quant_data.api.server import create_data_blueprint
from quant_data.scheduler.jobs import DataScheduler
from quant_data.service import QuantDataService
from .timing_engine import (
    account_cashflow_reconciliation_status,
    compute_levels,
    compute_timing_signals,
    confirm_buy,
    count_risk_increasing_buys,
    delete_position,
    dismiss_alert_exact,
    execution_performance,
    get_account_cash,
    get_alert,
    get_position_risk_state,
    get_buy_window,
    is_actionable_buy_window,
    IdempotencyConflictError,
    is_position_snapshot,
    kronos_status,
    list_account_cashflows,
    list_account_nav_snapshots,
    list_alerts,
    list_history,
    list_positions,
    list_sell_events,
    manual_fill_review,
    get_position_by_idempotency_key,
    load_fund_flow_snapshot,
    load_kronos_top,
    model_research_score,
    observe_position_risk_state,
    account_ledger_rows_sha256,
    read_account_ledger_facts,
    record_account_nav_snapshot,
    score_timing,
    sell_position,
    set_account_cash,
    try_record_alert,
    trade_capacity,
)
from .account_snapshot import build_account_snapshot_identity
from .account_fact_routes import register_account_fact_routes, manual_ledger_implementation_sha
from .company_profile_routes import register_company_profile_routes
from .intraday_market_emotion_forward import load_intraday_emotion_snapshot
from .forward_capture_observability import (
    forward_capture_schedule_status,
    latest_verified_core_route_review,
)
from .forward_capture_integrity import (
    forward_capture_wake_policy,
    verify_forward_selection_snapshot,
)
from .forward_capture_orchestrator import capture_forward_chain
from .sector_quote_panel_forward import (
    refresh_active_sector_quote_panels,
    start_sector_quote_panel,
)
from .system_route_account_input import normalize_system_route_account_input
from .verified_portfolio_execution_runtime import (
    new_capture_state as _new_verified_execution_capture_state,
    public_runtime_state as _verified_execution_runtime_state,
)
from .position_monitoring import positions_by_code, risk_actions_by_position_id
from .trade_plan import build_trade_plan
from .profit_pipeline import (
    allocate_capital_with_risk as _allocate_capital_with_risk,
    build_position_sizing_risk_context as _build_position_sizing_risk_context,
    entry_timing_status as _entry_timing_status,
    ledger_position_cost as _ledger_position_cost,
    manual_portfolio_status as _manual_portfolio_status_impl,
    opportunity_market_evidence as _opportunity_market_evidence_impl,
    portfolio_exposure_pct as _portfolio_exposure_pct,
    position_trade_plan as _position_trade_plan,
    refine_opportunity_quality as _refine_opportunity_quality,
    remaining_buy_cost as _remaining_buy_cost,
)
from . import manual_fill_fees
_LOG = logging.getLogger(__name__)
_MANUAL_STRATEGY_SPEC_PATH = _PROJECT / "research" / "MANUAL_OPPORTUNITY_EXECUTION_V14.json"

def _manual_strategy_ref() -> dict[str, object]:
    """Bind research alerts to the exact frozen manual strategy specification."""
    try:
        raw = _MANUAL_STRATEGY_SPEC_PATH.read_bytes()
        spec = json.loads(raw)
        if not isinstance(spec, dict) or not spec.get("strategy_id"):
            return {}
        return {
            "origin": "manual_decision_support",
            "strategy_id": str(spec["strategy_id"]),
            "strategy_spec_sha256": hashlib.sha256(raw).hexdigest(),
            "catalog_version": str(spec.get("version") or "1.0.0"),
            "production_eligible": False,
            "evidence_ref": str(_MANUAL_STRATEGY_SPEC_PATH.relative_to(_PROJECT)),
        }
    except (OSError, json.JSONDecodeError, ValueError):
        return {}

_MANUAL_STRATEGY_REF = _manual_strategy_ref()

_SCORING_STANDARD = {
    "id": "professional_composite_v4_profit_sign_aware",
    "scale": "0_to_100",
    "weights_pct": {
        "growth": 22, "quality": 22, "valuation": 16, "risk": 20,
        "technical": 20,
    },
    "missing_policy": "exclude_stale_or_missing_dimension_then_normalize_visible_weight",
    "veto_policy": "confirmed_risk_only; unavailable_evidence_reduces_confidence",
    "market_policy": "market_state_controls_account_exposure_not_stock_score",
    "fund_flow_policy": "third_party_flow_is_observation_only_not_stock_score",
    "timing_policy": "live_condition_match_controls_entry_review_only_not_stock_score",
    "timing_score_semantics": "condition_match_not_probability_or_expected_return",
}

_QUALITY_TIMING_BASELINE = 50.0

# ── 常亮 ──────────────────────────────────────
MARKET_INDEX_CODES = ("sh000001", "sz399001", "sz399006")
INDEX_LABELS = {"sh000001": "上证", "sz399001": "深证", "sz399006": "创业板"}
CODE_PATTERN = re.compile(r"^(?:0|3|6)\d{5}$")
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
def _strict_json_payload(
    *, allowed: set[str], required: set[str] = frozenset(),
) -> dict[str, object]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    unknown = sorted(set(payload) - allowed)
    missing = sorted(required - set(payload))
    if unknown:
        raise ValueError(f"请求体包含未知字段：{', '.join(unknown)}")
    if missing:
        raise ValueError(f"请求体缺少字段：{', '.join(missing)}")
    return payload

def _bounded_float(
    value: object, name: str, *, minimum: float, maximum: float,
) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是数字") from None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{name} 必须在 {minimum:g}–{maximum:g} 之间")
    return number

def _bounded_int(
    value: object, name: str, *, minimum: int, maximum: int,
) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是整数")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是整数") from None
    if str(value).strip() not in {str(number), f"{number}.0"}:
        raise ValueError(f"{name} 必须是整数")
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} 必须在 {minimum}–{maximum} 之间")
    return number

def _validated_display_name(value: object, *, fallback: str = "") -> str:
    """Bound names crossing into HTML-facing storage, including upstream data."""
    name = str(value or fallback).strip()
    if len(name) > 40 or any(not char.isprintable() for char in name):
        raise ValueError("name 不得超过 40 个可见字符")
    return name

def _display_name_or_unavailable(value: object) -> str:
    """Keep bad upstream labels from breaking a whole read-only scan."""
    try:
        return _validated_display_name(value) or "名称不可用"
    except ValueError:
        return "名称不可用"

def _request_idempotency_key(payload: dict[str, object]) -> str:
    raw = request.headers.get("Idempotency-Key") or payload.get("idempotency_key")
    if raw in (None, ""):
        raise ValueError("写入请求必须提供 Idempotency-Key，重试时复用同一键")
    key = str(raw).strip()
    if not _IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
        raise ValueError("幂等键必须为 8–128 位字母、数字或 ._:-")
    return key

def _idempotent_position_replay(
    payload: dict[str, object], idempotency_key: str | None, *, action: str,
    entry_context: dict[str, object] | None = None,
) -> dict | None:
    existing = get_position_by_idempotency_key(idempotency_key)
    if existing is None:
        return None
    requested_price = payload.get("price")
    requested_buy_fee = payload.get("buy_fee_yuan")
    expected_entry = (
        entry_context.get("entry_origin"),
        entry_context.get("entry_strategy_id"),
        entry_context.get("entry_evidence_ref_json"),
        entry_context.get("entry_signal_id"),
        entry_context.get("entry_alert_id"),
    ) if isinstance(entry_context, dict) else (None, None, None, None, None)
    actual_entry = tuple(existing.get(key) for key in (
        "entry_origin", "entry_strategy_id", "entry_evidence_ref_json",
        "entry_signal_id", "entry_alert_id",
    ))
    expected_reference_price = (
        entry_context.get("entry_reference_price")
        if isinstance(entry_context, dict) else None
    )
    if (
        existing.get("code") != payload.get("code")
        or int(existing.get("initial_shares") if existing.get("initial_shares") is not None else existing.get("shares") or 0) != int(payload.get("shares") or 0)
        or existing.get("action") != action
        or (
            requested_price is not None
            and abs(float(existing.get("price") or 0) - float(requested_price)) > 1e-9
        )
        or actual_entry != expected_entry
        or existing.get("entry_reference_price") != expected_reference_price
        or (
            requested_buy_fee is not None
            and (
                existing.get("buy_cost_source") != "actual_user_recorded"
                or abs(
                    float(existing.get("buy_cost") or 0)
                    - float(requested_buy_fee)
                ) > 1e-9
            )
        )
        or (
            requested_buy_fee is None
            and existing.get("buy_cost_source") == "actual_user_recorded"
        )
    ):
        raise IdempotencyConflictError("幂等键已用于另一笔不同的持仓记录")
    return {**existing, "idempotent_replay": True}

def _data_staleness_days(as_of: object, now: datetime | None = None) -> int | None:
    raw = str(as_of or "").strip()
    if not raw:
        return None
    try:
        observed = datetime.fromisoformat(raw[:10]).date()
    except ValueError:
        return None
    current = (now or datetime.now(_SH_TZ)).astimezone(_SH_TZ).date()
    age = (current - observed).days
    return age if age >= 0 else None

# 本机审计日线快照只给“休市/上游异常时可打开的研究界面”使用。它不是实时行情
# 的替代品：每个响应都保留输入文件的 as-of，前端也会明确显示为非实时快照。
_AUDITED_DAILY_DIR = _PROJECT / "research" / "data" / "v4_daily"
_AUDITED_STAR_DAILY_DIR = _PROJECT / "research" / "data" / "monitoring_star_daily"
_STOCK_BASIC_PATH = _PROJECT / "research" / "data" / "stock_basic_baostock.csv"
_AUDITED_DAILY_LOCK = threading.Lock()
_AUDITED_DAILY_CACHE: dict[str, tuple[int, list[Candle], dict[str, object]]] = {}
_STOCK_NAME_CACHE: dict[str, str] | None = None
_V2_BACKTEST_DIR = _PROJECT / "research" / "backtest_project" / "data" / "results_v2_repro_20260809_c"

def _v2_backtest_status() -> dict[str, object]:
    """Only report artifacts actually written by the v2 rerun, never an elapsed-time estimate."""
    root = _V2_BACKTEST_DIR
    completed = 0
    metrics = root / "metrics.csv"
    if metrics.exists():
        try:
            with metrics.open(encoding="utf-8", newline="") as handle:
                seen = set()
                for row in csv.DictReader(handle):
                    if None in row:
                        continue
                    env = (row.get("env") or "").strip()
                    try:
                        run = int(row.get("run") or "")
                        total_return = float(row.get("total_return") or "")
                    except (TypeError, ValueError):
                        continue
                    if env and run >= 0 and math.isfinite(total_return):
                        seen.add((env, run))
                completed = len(seen)
        except (OSError, UnicodeError, csv.Error):
            completed = 0
    running = False
    pid_file = root / "runner.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text(encoding="utf-8").strip()), 0)
            running = True
        except (OSError, ValueError):
            pass
    candidate_status = (
        f"v2 已见{completed}条历史回测记录；旧执行口径未按新门禁重算，不将股票池成员作为买入推荐"
        if completed else "有效回测原件未见；旧执行口径未按新门禁重算，不将股票池成员作为买入推荐"
    )
    return {
        "pool": "v2 中小盘活跃股池",
        "completed_runs": completed,
        "expected_runs": 50,
        "running": running,
        "data_provenance_ready": (root / "data_provenance.json").exists(),
        "manifest_ready": (root / "run_manifest.json").exists(),
        "candidate_status": candidate_status,
        "research_only": True,
        "legacy_execution_validated": False,
        "target_a_admitted": False,
        "historical_result_scope": "修复前旧日线执行口径；未按新执行门禁重算；不得用作交易或目标A证据",
    }

def _audited_stock_names() -> dict[str, str]:
    """Load local stock names once; no network call and no guessed company name."""

    global _STOCK_NAME_CACHE
    if _STOCK_NAME_CACHE is not None:
        return _STOCK_NAME_CACHE
    names: dict[str, str] = {}
    try:
        with _STOCK_BASIC_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                raw = str(row.get("code") or "")
                code = raw.split(".")[-1]
                name = str(row.get("name") or "").strip()
                if CODE_PATTERN.fullmatch(code) and name:
                    names[code] = name
    except OSError:
        pass
    _STOCK_NAME_CACHE = names
    return names

def _audited_daily_snapshot(code: str, limit: int = 200) -> tuple[list[Candle], dict[str, object]]:
    """Return a local, versioned daily input snapshot without touching an upstream API.

    The mtime-keyed cache keeps the terminal responsive while still invalidating
    automatically whenever the data worker replaces a daily input file.

    Data availability and analytical sufficiency are deliberately separate.
    A newly listed stock with two or more audited bars is valid chart/quote data;
    consumers that need 20, 35, or 60 observations must report their own sample
    gate instead of turning the entire stock terminal into a 503 response.
    """

    def _view(
        source_candles: list[Candle], source_metadata: dict[str, object],
    ) -> tuple[list[Candle], dict[str, object]]:
        selected = source_candles[-limit:]
        selected_start = max(0, len(source_candles) - limit)
        values = list(source_metadata.get("_return_index_values") or [])[selected_start:]
        segments = list(source_metadata.get("_return_index_segments") or [])[selected_start:]
        metadata = {
            key: value for key, value in source_metadata.items()
            if not key.startswith("_return_index_")
        }
        last_segment = segments[-1] if segments else None
        continuous = [
            float(value) for value, segment in zip(values, segments)
            if last_segment is not None and segment == last_segment
            and value is not None and math.isfinite(float(value))
        ]
        metadata.update({
            "return_index_closes": continuous,
            "return_index_observations": len(continuous),
            "return_index_basis": "audited_pctChg_compounded_v1",
            "corporate_action_safe_returns": bool(continuous),
        })
        return selected, metadata

    path = _AUDITED_DAILY_DIR / f"{code}.csv.gz"
    source_label = "本机已审计日线快照（非实时）"
    if not path.exists() and code.startswith(("688", "689")):
        universe = audited_universe(_PROJECT)
        star = (universe.get("sources") or {}).get("star_monitoring_supplement") or {}
        if universe.get("status") != "verified" or star.get("status") != "verified":
            raise CandleDataUnavailable("科创板监控日线审计未闭合")
        path = _AUDITED_STAR_DAILY_DIR / f"{code}.csv.gz"
        source_label = "本机已审计科创板监控补充日线（非实时、非因子面板）"
    if not path.exists():
        raise CandleDataUnavailable("本机审计日线快照缺失")
    mtime_ns = path.stat().st_mtime_ns
    with _AUDITED_DAILY_LOCK:
        cached = _AUDITED_DAILY_CACHE.get(code)
        if cached and cached[0] == mtime_ns:
            return _view(cached[1], cached[2])

        candles: list[Candle] = []
        return_index_values: list[float | None] = []
        return_index_segments: list[int | None] = []
        return_index_value: float | None = None
        return_index_segment = 0
        last_row: dict[str, str] | None = None
        try:
            with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    try:
                        candle = Candle(
                            time=str(row["date"]),
                            open=float(row["open"]),
                            high=float(row["high"]),
                            low=float(row["low"]),
                            close=float(row["close"]),
                            volume=float(row["volume"]),
                            amount=float(row["amount"]) if row.get("amount") else None,
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
                    candles.append(candle)
                    try:
                        pct_chg = float(row["pctChg"]) / 100.0
                    except (KeyError, TypeError, ValueError):
                        pct_chg = math.nan
                    if math.isfinite(pct_chg) and pct_chg > -1.0:
                        if return_index_value is None:
                            return_index_segment += 1
                            return_index_value = 100.0
                        else:
                            return_index_value *= 1.0 + pct_chg
                        return_index_values.append(return_index_value)
                        return_index_segments.append(return_index_segment)
                    else:
                        return_index_value = None
                        return_index_values.append(None)
                        return_index_segments.append(None)
                    last_row = row
        except (OSError, EOFError) as exc:
            raise CandleDataUnavailable(f"本机审计日线读取失败: {type(exc).__name__}") from exc

        if len(candles) < 2 or not last_row:
            raise CandleDataUnavailable("本机审计日线不足2根，不能计算涨跌")
        metadata: dict[str, object] = {
            "source": source_label,
            "as_of": candles[-1].time,
            "file": path.name,
            "history_bars": len(candles),
            "technical_history_required": 35,
            "technical_history_ready": len(candles) >= 35,
            "decision_price_eligible": True,
            "price_basis": "audited_raw_unadjusted_close",
            "factor_panel_input": False if code.startswith(("688", "689")) else True,
            "is_st": str(last_row.get("isST") or "0") in {"1", "true", "True"},
            "pct_chg": last_row.get("pctChg"),
            "turnover": last_row.get("turn"),
            "_return_index_values": return_index_values,
            "_return_index_segments": return_index_segments,
        }
        _AUDITED_DAILY_CACHE[code] = (mtime_ns, candles, metadata)
        return _view(candles, metadata)


def _runtime_public_daily_snapshot(
    code: str, provider: object, limit: int = 200,
    *, observed_at: datetime | None = None, raw_candles: list | None = None,
) -> tuple[list[Candle], dict[str, object]]:
    """Load a cloud-safe public daily fallback and validate its visible facts.

    Production hosts intentionally do not receive the workstation's large local
    research-data directory.  This fallback keeps the chart usable there while
    preserving the evidence boundary: it is public data with runtime shape
    checks, never relabelled as the workstation's audited raw epoch and never
    eligible to create an execution price.
    """

    try:
        candles = (raw_candles if raw_candles is not None else
                   provider.get_candles(code, max(35, limit), interval="1d"))
    except Exception as exc:
        if isinstance(exc, CandleDataUnavailable):
            raise
        raise CandleDataUnavailable(
            f"云端公开日线读取失败: {type(exc).__name__}"
        ) from exc
    candles = list(candles or [])[-limit:]
    if len(candles) < 35:
        raise CandleDataUnavailable("云端公开日线不足35根")
    previous_time = ""
    for candle in candles:
        current_time = str(candle.time or "")[:10]
        values = (
            candle.open, candle.high, candle.low, candle.close, candle.volume,
        )
        if (
            not re.fullmatch(r"\d{4}-\d{2}-\d{2}", current_time)
            or current_time <= previous_time
            or not all(math.isfinite(float(value)) for value in values)
            or min(candle.open, candle.high, candle.low, candle.close) <= 0
            or candle.volume < 0
            or candle.high < max(candle.open, candle.close)
            or candle.low > min(candle.open, candle.close)
        ):
            raise CandleDataUnavailable("云端公开日线结构校验失败")
        previous_time = current_time
    # Public providers can include today's developing candle. Keep the
    # technical snapshot and its provenance bound to completed observations.
    candles = completed_signal_candles(
        candles, "1d", observed_at=observed_at or datetime.now(_SH_TZ),
    )
    if len(candles) < 35:
        raise CandleDataUnavailable("公开日线已完成K线不足35根")
    origin = candle_price_origin(candles) or {}
    basis = origin.get("adjust_type", "unknown")
    is_tencent = origin.get("source") == "tencent_fqkline"
    basis_label = {"raw": "不复权", "qfq": "前复权", "hfq": "后复权"}.get(basis, "口径待核验")
    return candles, {
        "source": ("腾讯公开" if is_tencent else "公开") + basis_label + "日线（运行时校验，非本机审计）",
        "as_of": candles[-1].time,
        "file": None,
        "history_bars": len(candles),
        "technical_history_required": 35,
        "technical_history_ready": True,
        "decision_price_eligible": False,
        "price_basis": f"tencent_{basis}_close_runtime_validated" if is_tencent else "public_price_basis_unverified",
        "adjust_type": basis,
        "data_origin": origin or None,
        "factor_panel_input": False,
        "is_st": None,
        "runtime_validation": "date_order_ohlc_geometry_positive_price_nonnegative_volume",
        "terminal_mode": "public_daily_runtime_fallback",
    }

def _stock_display_observations(
    code: str, candles: list | None, *, observed_at: datetime | None = None,
) -> dict:
    """Independent visible observations; never supply audited quality inputs."""
    from .stock_observations import load_public_valuation
    from .workstation import _price_risk_metrics, serialize_candles
    observed = observed_at or datetime.now(_SH_TZ)
    result = {"valuation": load_public_valuation(code, observed_at=observed),
              "technical": {"status":"unavailable", "lenses":[],
                            "note":"公开已完成日线暂不可用。"}}
    try:
        completed, meta = _runtime_public_daily_snapshot(
            code, None, raw_candles=list(candles or []), observed_at=observed,
        )
        display = technical_snapshot(completed)
        result["technical"] = {
            **display, "source":meta["source"], "as_of":meta["as_of"],
            "observed_at":observed.isoformat(timespec="seconds"),
            "adjust_type":meta["adjust_type"], "history_bars":meta["history_bars"],
            "market_risk":_price_risk_metrics(serialize_candles(completed)),
            "role":"public_observation_only", "decision_eligible":False,
            "score_eligible":False,
            "note":"技术指标由公开已完成日线本地计算；来源与复权口径见本页，不纳入统一评分或执行价格。",
        }
    except (CandleDataUnavailable, IndexError, TypeError, ValueError):
        pass
    return result


def _opportunity_market_evidence(
    candidate: dict, price: float | None = None,
    account_capital: float = ACCOUNT_CAPITAL,
) -> dict[str, object]:
    return _opportunity_market_evidence_impl(
        candidate, price, account_capital,
        snapshot_loader=_audited_daily_snapshot,
        unavailable_exception=CandleDataUnavailable,
    )

# 公司档案自动预热：固定 3 个 worker，并限制运行+排队总量，避免候选轮询无限创建线程。
_CP_PREHEAT_LOCK = threading.Lock()
_CP_PREHEAT_TRIED: dict = {}
_CP_PREHEAT_INTERVAL_S = 3600
_CP_PREHEAT_CAPACITY = 24
_CP_PREHEAT_INFLIGHT: set[str] = set()
_CP_PREHEAT_ERRORS: dict[str, dict] = {}
_CP_PREHEAT_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="cp-preheat")

# 实时高分扫描状态：后台线程跑，结果缓存 TTL 自适应（见下）。
_LIVE_SCAN_STATE: dict = {
    "lock": threading.Lock(),
    "result": None,          # 最近一轮扫描结果
    "running": False,        # 是否有扫描线程在跑
    "last_ts": 0.0,          # 最近一次完成的时刻
    "last_error": None,      # 最近一轮扫描的异常（worker 崩溃时可见，避免"starting"死循环不可诊断）
    "last_duration_s": None, # 最近一轮实际耗时（用于自适应 TTL）
    "progress": {"phase": "idle", "done": 0, "total": 0, "started": None},
}
# 新鲜期取 max(180s, 最近耗时×2.5)，避免长扫描完成即过期。
_LIVE_SCAN_TTL_S = 180

# 今日机会由后台在连续竞价时段每 60s 维护真实快照；轮询 GET 不触发重算。
_OPP_STATE: dict = {
    "lock": threading.Lock(),
    "result": None,
    "forward_context": None,
    "panel_refresh": None,
    "ts": 0.0,
    "running": False,
    "last_error": None,
}
_STRICT_FORWARD_STATUS_CACHE: dict = {
    "lock": threading.Lock(),
    "identity": None,
    "payload": None,
    "stored_at": 0.0,
}
_STRICT_FORWARD_STATUS_CACHE_MAX_AGE_S = 10.0
_MARKET_RESEARCH_BASE_CACHE: dict = {
    "lock": threading.Lock(), "identity": None,
    "payload": None, "stored_at": 0.0,
}
_FORWARD_SCHEDULE_STATUS_CACHE: dict = {
    "lock": threading.Lock(), "identity": None,
    "payload": None, "stored_at": 0.0,
}
_CORE_ROUTE_REVIEW_CACHE: dict = {
    "lock": threading.Lock(), "identity": None, "payload": None,
}


def _cached_latest_research_state() -> dict:
    """Reuse the immutable daily research program across dashboard polling."""
    identity = latest_research_state
    now = _time.monotonic()
    with _MARKET_RESEARCH_BASE_CACHE["lock"]:
        payload = _MARKET_RESEARCH_BASE_CACHE["payload"]
        if (
            isinstance(payload, dict)
            and _MARKET_RESEARCH_BASE_CACHE["identity"] is identity
            and now - _MARKET_RESEARCH_BASE_CACHE["stored_at"] <= 75.0
        ):
            return copy.deepcopy(payload)
    payload = latest_research_state()
    with _MARKET_RESEARCH_BASE_CACHE["lock"]:
        _MARKET_RESEARCH_BASE_CACHE.update({
            "identity": identity, "payload": copy.deepcopy(payload),
            "stored_at": _time.monotonic(),
        })
    return payload


def _forward_schedule_cache_bucket(observed: datetime) -> tuple[str, int]:
    """Use five-second buckets near capture windows, minute buckets otherwise."""
    current = observed.timetz().replace(tzinfo=None)
    near_capture = (
        time(10, 39) <= current <= time(10, 45, 59)
        or time(14, 9) <= current <= time(14, 15, 59)
    )
    seconds = observed.hour * 3600 + observed.minute * 60 + observed.second
    bucket_seconds = 5 if near_capture else 60
    return observed.date().isoformat(), seconds // bucket_seconds


def _cached_forward_capture_schedule_status(
    *, now: datetime, is_trading_day: bool,
) -> dict:
    """Share one full immutable-artifact verification inside a clock bucket."""
    observed = now.astimezone(_SH_TZ)
    identity = (
        forward_capture_schedule_status,
        bool(is_trading_day),
        _forward_schedule_cache_bucket(observed),
    )
    monotonic_now = _time.monotonic()
    with _FORWARD_SCHEDULE_STATUS_CACHE["lock"]:
        payload = _FORWARD_SCHEDULE_STATUS_CACHE["payload"]
        if (
            isinstance(payload, dict)
            and _FORWARD_SCHEDULE_STATUS_CACHE["identity"] == identity
            and monotonic_now - _FORWARD_SCHEDULE_STATUS_CACHE["stored_at"] <= 65.0
        ):
            return copy.deepcopy(payload)
    payload = forward_capture_schedule_status(
        now=observed, is_trading_day=is_trading_day,
    )
    with _FORWARD_SCHEDULE_STATUS_CACHE["lock"]:
        _FORWARD_SCHEDULE_STATUS_CACHE.update({
            "identity": identity, "payload": copy.deepcopy(payload),
            "stored_at": _time.monotonic(),
        })
    return payload


def _cached_latest_core_route_review(
    schedule_status: dict, *, observed_at: datetime,
) -> dict:
    """Verify an immutable core route once per artifact identity."""
    identities = tuple(
        (
            row.get("slot_id"),
            ((row.get("artifact_integrity") or {}).get("route_decision") or {}).get(
                "sha256"
            ),
            (row.get("strict_forward_v2") or {}).get("valid") is True,
            (row.get("strict_forward_v2") or {}).get("packet_id"),
        )
        for row in (schedule_status.get("slots") or [])
        if row.get("phase") in {"window_open", "captured"}
    )
    identity = (
        latest_verified_core_route_review,
        schedule_status.get("decision_day"), identities,
    )
    with _CORE_ROUTE_REVIEW_CACHE["lock"]:
        payload = _CORE_ROUTE_REVIEW_CACHE.get("payload")
        if (
            isinstance(payload, dict)
            and _CORE_ROUTE_REVIEW_CACHE.get("identity") == identity
        ):
            return copy.deepcopy(payload)
    payload = latest_verified_core_route_review(
        schedule_status, observed_at=observed_at, project_root=_PROJECT,
    )
    with _CORE_ROUTE_REVIEW_CACHE["lock"]:
        _CORE_ROUTE_REVIEW_CACHE.update({
            "identity": identity, "payload": copy.deepcopy(payload),
        })
    return payload


def _strict_forward_status_cache_identity() -> tuple | None:
    """Track every mutable file used by the cached strict-route verification.

    JSON parsing here is intentionally shallow and cheap.  Verification still
    happens in ``system_route_joint_v2_forward_status``; the cache only avoids
    repeating that full reproduction while dashboard endpoints fan out.
    """
    paths: set[Path] = {
        _STRICT_FORWARD_PROTOCOL_PATH.resolve(),
        _STRICT_FORWARD_ADMISSION_PATH.resolve(),
    }
    try:
        documents: list[dict] = []
        for path in (
            _STRICT_FORWARD_PROTOCOL_PATH,
            _STRICT_FORWARD_ADMISSION_PATH,
        ):
            documents.append(json.loads(path.read_text(encoding="utf-8")))
        decision_paths = sorted(_STRICT_FORWARD_DECISION_DIR.glob("*.json"))
        for path in decision_paths:
            paths.add(path.resolve())
            packet = json.loads(path.read_text(encoding="utf-8"))
            refs = ((packet.get("adapter_result") or {}).get("input_refs") or {})
            for ref in refs.values():
                if not isinstance(ref, dict) or not ref.get("path"):
                    continue
                target = (_PROJECT / str(ref["path"])).resolve()
                target.relative_to(_PROJECT.resolve())
                paths.add(target)
        for document in documents:
            bindings = {
                **(document.get("bindings") or {}),
                "predecessor": document.get("predecessor") or {},
            }
            for binding in bindings.values():
                if not isinstance(binding, dict) or not binding.get("path"):
                    continue
                target = (_PROJECT / str(binding["path"])).resolve()
                target.relative_to(_PROJECT.resolve())
                paths.add(target)
        identities = []
        for path in sorted(paths, key=str):
            stat = path.stat()
            identities.append((
                str(path), stat.st_ino, stat.st_size,
                stat.st_mtime_ns, stat.st_ctime_ns,
            ))
        return tuple(identities)
    except (AttributeError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


def _cached_strict_forward_status() -> dict:
    """Return a fully verified strict-route status without endpoint fan-out work."""
    identity = _strict_forward_status_cache_identity()
    if identity is not None:
        identity = (*identity, ("verifier", system_route_joint_v2_forward_status))
    now = _time.monotonic()
    if identity is not None:
        with _STRICT_FORWARD_STATUS_CACHE["lock"]:
            payload = _STRICT_FORWARD_STATUS_CACHE["payload"]
            if (
                isinstance(payload, dict)
                and _STRICT_FORWARD_STATUS_CACHE["identity"] == identity
                and now - _STRICT_FORWARD_STATUS_CACHE["stored_at"]
                <= _STRICT_FORWARD_STATUS_CACHE_MAX_AGE_S
            ):
                return copy.deepcopy(payload)
    payload = system_route_joint_v2_forward_status()
    final_identity = _strict_forward_status_cache_identity()
    if final_identity is not None:
        final_identity = (
            *final_identity,
            ("verifier", system_route_joint_v2_forward_status),
        )
    if identity is not None and final_identity == identity:
        with _STRICT_FORWARD_STATUS_CACHE["lock"]:
            _STRICT_FORWARD_STATUS_CACHE["identity"] = identity
            _STRICT_FORWARD_STATUS_CACHE["payload"] = copy.deepcopy(payload)
            _STRICT_FORWARD_STATUS_CACHE["stored_at"] = _time.monotonic()
    return payload
_FORWARD_CAPTURE_STATE = {
    "lock": threading.Lock(), "running": False, "last_error": None,
    "last_attempt_key": None,
}
_FORWARD_CAPTURE_RUNTIME_STATUS = (
    _PROJECT / "reports" / "forward_capture_runtime_status.json"
)
_VERIFIED_EXECUTION_CAPTURE_STATE = _new_verified_execution_capture_state()
_FORWARD_CAPTURE_RESULT_FIELDS = (
    "market_state_forward_capture", "h3_execution_forward_capture",
    "intraday_emotion_forward_capture", "market_news_forward_capture",
    "market_sector_route_forward_capture",
    "market_sector_route_shadow_decision",
    "market_sector_constituent_forward_capture",
    "sector_constituent_quote_panel_capture",
    "system_route_joint_v2_forward_capture",
    "manual_forward_evidence",
)
_OPP_REFRESH_S = 60
_OPP_CURRENT_MAX_AGE_S = _OPP_REFRESH_S * 2
_OPP_APPROVED_TARGET = 5
_OPP_ASSESSMENT_BUDGET = 24

_OPPORTUNITY_RANKING_TIE_BREAK = {
    "primary": "opportunity_quality.rank_score_desc",
    "secondary": "assessment.can_alert_desc",
    "tertiary": "assessment.gates.composite.composite_desc",
    "quaternary": "price_fit.fit_desc",
    "final_tie_policy": "stable_preselection_order_no_random_jitter",
}

def _opportunity_snapshot_freshness(
    result: object, completed_at: object, *, now: float | None = None,
    session_open: bool | None = None,
) -> dict[str, object]:
    """One freshness contract for monitor status and opportunity responses."""
    current_time = _time.time() if now is None else float(now)
    has_snapshot = isinstance(result, dict)
    failed = bool(has_snapshot and result.get("error"))
    completed = _finite_number(completed_at)
    age = (
        max(0.0, current_time - completed)
        if has_snapshot and completed is not None and completed > 0 else None
    )
    open_now = _is_market_open() if session_open is None else bool(session_open)
    outside_session_current = bool(
        has_snapshot and not failed and not open_now
        and _snapshot_current_outside_open_session(
            result, session_open=open_now,
        )
    )
    within_live_age = bool(
        has_snapshot and not failed and age is not None
        and age <= _OPP_CURRENT_MAX_AGE_S
    )
    return {
        "has_snapshot": has_snapshot,
        "failed": failed,
        "age_s": age,
        "max_age_s": _OPP_CURRENT_MAX_AGE_S,
        "outside_session_current": outside_session_current,
        "current": within_live_age if open_now else outside_session_current,
    }

def _opportunity_score_contract(
    assessment: object, condition_match_score: object,
) -> dict[str, object]:
    """Expose one stock score and keep timing as a separately named gate."""
    assessed = assessment if isinstance(assessment, dict) else {}
    gates = assessed.get("gates") if isinstance(assessed.get("gates"), dict) else {}
    composite = (
        gates.get("composite")
        if isinstance(gates.get("composite"), dict) else {}
    )
    timing = gates.get("timing") if isinstance(gates.get("timing"), dict) else {}
    unified_score = _finite_number(composite.get("composite"))
    timing_score = _finite_number(
        timing.get("score", condition_match_score)
    )
    detail = []
    for raw in composite.get("detail") or []:
        if not isinstance(raw, dict):
            continue
        row = copy.deepcopy(raw)
        effective_weight = _finite_number(row.get("effective_weight"))
        component_score = _finite_number(row.get("score"))
        row["contribution"] = (
            round(effective_weight * component_score, 2)
            if effective_weight is not None and component_score is not None
            else None
        )
        detail.append(row)
    return {
        "unified_composite_score": unified_score,
        "condition_match_score": timing_score,
        # Compatibility only: opportunity ``score`` historically represented
        # the stock score in compact clients. New clients must use the explicit
        # field above.
        "score": unified_score,
        "score_role": "deprecated_compatibility_alias_of_unified_composite_score",
        "score_deprecated": True,
        "score_deprecated_alias_of": "unified_composite_score",
        "composite_evidence": {
            "weight_used": composite.get("weight_used"),
            "coverage_pct": composite.get("coverage_pct"),
            "coverage_status": composite.get("coverage_status"),
            "missing_dimensions": copy.deepcopy(composite.get("missing") or []),
            "data_gap": composite.get("data_gap"),
            "data_gap_missing": copy.deepcopy(
                composite.get("data_gap_missing") or []
            ),
            "excluded_for_freshness": copy.deepcopy(
                composite.get("excluded_for_freshness") or []
            ),
            "detail": detail,
        },
        "ranking_tie_break": copy.deepcopy(_OPPORTUNITY_RANKING_TIE_BREAK),
    }

def _public_score_roles(value: object) -> object:
    """Remove the ambiguous legacy company score from public JSON surfaces."""
    if isinstance(value, list):
        return [_public_score_roles(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {
        key: _public_score_roles(item)
        for key, item in value.items()
        if key != "company_score"
    }
    if "company_score" in value:
        result["risk_verification_raw_points_0_30"] = value.get("company_score")
        result["risk_verification_score_contract"] = {
            "role": "risk_gate_diagnostic_not_overall_score",
            "scale": {"min": 0, "max": 30},
            "ranking_eligible": False,
            "overall_score_field": "unified_composite_score",
        }
    return result


def _best_opportunity_assessment_batch(
    candidates: list[dict], *,
    assessment_budget: int = _OPP_ASSESSMENT_BUDGET,
) -> list[dict]:
    """Select deep-assessment rows by the single professional score.

    A cache-only preview uses the same professional composite as the final
    ranking, but only when every row in the retained pool has a comparable
    preview.  Partial cache coverage must never decide admission: until the
    whole pool is covered, every row follows the existing market-evidence
    order.  No neutral score or second blended total is invented.
    """
    budget = max(0, int(assessment_budget))
    if budget == 0 or not candidates:
        return []
    if len(candidates) <= budget:
        return list(candidates)

    preview_ranked = []
    for index, row in enumerate(candidates):
        preview = row.get("preselection_professional_preview") or {}
        score = _finite_number(preview.get("professional_score"))
        if score is None:
            return list(candidates[:budget])
        preview_ranked.append((score, -index, row))
    preview_ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [row for _, _, row in preview_ranked[:budget]]


def _frozen_full_market_preselection_preview(candidate: dict) -> dict | None:
    """Reuse the same frozen score that admitted the full-market Top300."""
    preview = candidate.get("preselection_professional_preview") or {}
    if not (
        candidate.get("source") == "full_market"
        and preview.get("status") == "available"
        and preview.get("method") == _SCORING_STANDARD["id"]
        and _finite_number(preview.get("professional_score")) is not None
    ):
        return None
    return copy.deepcopy(preview)


def _assess_ranked_opportunities(
    candidates: list[dict], assessor, *,
    approved_target: int = _OPP_APPROVED_TARGET,
    assessment_budget: int = _OPP_ASSESSMENT_BUDGET,
) -> list[dict]:
    """Assess in rank order until the main list is filled or budget is spent."""
    assessed = []
    approved = 0
    for candidate in candidates[:assessment_budget]:
        item = assessor(candidate)
        assessed.append(item)
        assessment = item.get("assessment") or {}
        if (
            assessment.get("verdict") == "APPROVE"
            and assessment.get("manual_actionable") is True
        ):
            approved += 1
            if approved >= approved_target:
                break
    return assessed


# 雷达约 4s 的网络重建同样归后台调度器。
_RADAR_STATE: dict = {
    "lock": threading.Lock(),
    "data": None,
    "ts": 0.0,
    "running": False,
    "last_error": None,
}
_RADAR_REFRESH_S = 180

def _try_start_snapshot_worker(state: dict, target, *, name: str, args: tuple = ()) -> bool:
    with state["lock"]:
        if state["running"]:
            return False
        state["running"] = True
    try:
        threading.Thread(target=target, args=args, daemon=True, name=name).start()
    except Exception:
        with state["lock"]:
            state["running"] = False
        raise
    return True


def _forward_capture_attempt_key(schedule_status: dict) -> tuple[str, str] | None:
    """Identify the one immutable capture attempt allowed for an open slot."""
    open_slot = next(
        (
            row for row in schedule_status.get("slots") or []
            if row.get("phase") == "window_open" and row.get("slot_id")
        ),
        None,
    )
    decision_day = str(schedule_status.get("decision_day") or "")
    if not decision_day or not open_slot:
        return None
    return decision_day, str(open_slot["slot_id"])


def _try_start_forward_capture_worker(
    state: dict, target, *, name: str, attempt_key: tuple[str, str] | None,
) -> bool:
    """Start at most once per real slot so retries cannot overwrite evidence."""
    if attempt_key is None:
        return False
    with state["lock"]:
        if state["running"] or state.get("last_attempt_key") == attempt_key:
            return False
        state["running"] = True
        state["last_attempt_key"] = attempt_key
    try:
        threading.Thread(target=target, daemon=True, name=name).start()
    except Exception:
        with state["lock"]:
            state["running"] = False
            if state.get("last_attempt_key") == attempt_key:
                state["last_attempt_key"] = None
        raise
    return True

def _snapshot_refresh_response(state: dict, target, *, name: str,
                               status_endpoint: str, args: tuple = ()):
    try:
        if request.content_length: _strict_json_payload(allowed=set())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    started = _try_start_snapshot_worker(state, target, name=name, args=args)
    return jsonify({"ok": True, "running": True, "started": started,
                    "idempotent_replay": not started,
                    "status_endpoint": status_endpoint}), 202

# 时机信号共享缓存：扫描池 / 今日机会 / 档案页三处共用同一套 compute_timing_signals+score_timing。
# 5 分钟 K 线 + 资金流属分钟级稳定数据：相同 (code, prev_close) 在 _SCORING_CACHE_TTL 内复用，
# 避免后台扫描(每 180s)与机会轮询(每 40s)反复串行打 K 线源——那曾是 waitress 队列饱和的根因之一。
# 口径：5 分钟 K 线本就 5 分钟粒度，缓存 300s 是诚实的新鲜度（前端已展示 stale 标记）。
_SCORING_CACHE: dict = {}
_SCORING_CACHE_TTL = 300
_SCORING_CACHE_LOCK = threading.Lock()   # 后台重算周期；重算期间接口秒回上次快照（stale 标记）

# M3.2 统一引擎专业诊断缓存：按 spec 哈希 + TTL，避免重复重算（每份 ~50s 重负载）
_LAB_DIAG_CACHE: dict[str, tuple[float, dict]] = {}
_LAB_DIAG_TTL = 600
_LAB_DIAG_MAX_ENTRIES = 20

def _begin_strategy_historical_observation(spec: dict) -> dict:
    """在统一策略引擎读取历史确认数据前冻结 LOND 到达顺序。"""
    from factor_system import config as fconfig
    from factor_system import registry as freg
    from factor_system.gate import research_input_key
    from . import user_strategy_lab as strategy_lab

    hypothesis_id = strategy_lab._batch_hypothesis_id(spec)
    context = strategy_lab._manual_strategy_latch_context(spec)
    data_key = research_input_key()
    c = freg._conn()
    try:
        reservation = freg.begin_factor_observation(
            c, hypothesis_id,
            epoch=f"{fconfig.RESEARCH_EPOCH}:historical-confirmation",
            p_value_method="non_overlapping_phase_max_hac",
            data_key=data_key,
            producer_type="strategy_manual", context=context,
        )
        c.commit()
        reservation.setdefault("data_key", data_key)
        return reservation
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
_STRATEGY_COST_SEMANTICS = {
    "one_way_cost_bps_role": "slippage_only",
    "additional_trading_costs": "commission_stamp_tax_transfer_fee",
    "label": "单边滑点；佣金、印花税、过户费另加",
}

# ── 个人排除名单（2026-08-09，用户指令）────────────────────────
# 与板块黑名单正交：老邓股/已持仓等「家里已知的股票」由用户维护，可随时增删。
# 榜单（今日机会 / 实时扫描）都会过滤；排除只影响推荐，不影响行情/档案等独立页面。
_EXCLUSIONS_FILE = Path(__file__).resolve().parent.parent / "data" / "personal_exclusions.json"
_EXCLUSIONS_LOCK = threading.Lock()

def _load_exclusions() -> dict:
    try:
        return json.loads(_EXCLUSIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

def _save_exclusions(data: dict) -> None:
    _EXCLUSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _EXCLUSIONS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_EXCLUSIONS_FILE)

def _is_personal_excluded(code: str) -> bool:
    return code in _load_exclusions()

def _add_exclusion(code: str, name: str) -> dict:
    with _EXCLUSIONS_LOCK:
        d = _load_exclusions()
        d[code] = {"name": name or code,
                   "added_at": datetime.now(_SH_TZ).isoformat(timespec="seconds"),
                   "reason": "个人排除"}
        _save_exclusions(d)
        return d[code]

def _remove_exclusion(code: str) -> bool:
    with _EXCLUSIONS_LOCK:
        d = _load_exclusions()
        if code in d:
            del d[code]
            _save_exclusions(d)
            return True
        return False

# ── 连续上榜天数（2026-08-09，诚实口径）────────────────────────
# 仅统计「该股进入推荐名单」的连续自然日天数，同一自然日内多次重算不重复计数。
# 前端在连续 ≥2 天时显示"连续N天"徽章，让用户一眼看出是重复推荐还是新发现。
_RECURRENCE_FILE = Path(__file__).resolve().parent.parent / "data" / "recommend_recurrence.json"
_RECURRENCE_LOCK = threading.Lock()

def _load_recurrence() -> dict:
    try:
        return json.loads(_RECURRENCE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

def _save_recurrence(data: dict) -> None:
    _RECURRENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _RECURRENCE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_RECURRENCE_FILE)

def _update_recurrence(codes: list[str], today: str) -> dict:
    """更新连续上榜状态，返回 {code: {"streak": int}}（所有条目都返回当前值）。

    同一自然日内重复计算不重复计数（last_date == today 直接返回现值）；
    隔日未上榜（出现空档）→ streak 归 1。
    """
    if not codes:
        return {}
    with _RECURRENCE_LOCK:
        state = _load_recurrence()
        yesterday = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        result = {}
        for code in codes:
            rec = state.get(code) or {"streak": 0, "last_date": None}
            if rec.get("last_date") != today:
                if rec.get("last_date") == yesterday:
                    rec["streak"] = int(rec.get("streak", 0)) + 1
                else:
                    rec["streak"] = 1
                rec["last_date"] = today
                state[code] = rec
            result[code] = {"streak": rec["streak"]}
        _save_recurrence(state)
        return result

# 综合评分权重（合计 1.0）：公司侧(避雷) 与 量化侧(alpha) 对总分的占比。
# 权重是易变决策参数，集中在此一处调整。
_COMPANY_WEIGHT = 0.15   # 公司侧（避雷）：score_gates 原始 0-30 分 → 归一化后贡献 15 分
_QUANT_WEIGHT = 0.85     # 量化侧（alpha）：score_timing 原始 0-100 分 → 贡献 85 分
# 模型研究复核使用的权重：原始模型输出不改动；同批横截面排序分仅用于与公司侧同量纲汇总。
# 这与上方「时机评分 85%」的旧综合分并存：时机只作为交易时点复核，不替代模型优先级。
_MODEL_RESEARCH_WEIGHT = 0.85
# 价格合适性：账户资金与单仓目标（与 signal_engine/timing_engine 同口径，默认 20 万/5 仓）。
# 用于给"默认账户能不能按现价整手买入、佣金占比是否偏高"一个客观标签，不参与 100 分制总分，
# 只作为并列同分时的排序增强 + 前端明示（避免"价格合适"成为隐藏黑箱）。
_LOT_UNIT = 100                       # A 股一手 100 股
_COMMISSION_SENSITIVE_YUAN = 10000.0  # 单笔 < ¥1 万 → 最低佣金 5 元占比 >0.05%，标注佣金敏感

def _price_fit(price: float | None, account_capital: float | None = None,
               max_positions: int = MAX_POSITIONS) -> dict:
    """现价 × 账户结构 → 价格合适性标签（客观，不编造）。

    - lot_cost: 一手成本（price×100），None 表示无价
    - afford_lots: 按单仓目标（账户/最大持仓数）能买几手，0 = 连一手都买不起
    - commission_sensitive: 单笔 < ¥1 万 → 最低佣金 5 元占比偏高
    - fit: 'good'（可整手且不敏感）/ 'ok'（可整手但佣金敏感）/ 'poor'（一手都买不起）
    """
    if not price or price <= 0:
        return {"lot_cost": None, "afford_lots": 0, "commission_sensitive": True,
                "fit": "no_price", "label": "无价"}
    lot_cost = round(price * _LOT_UNIT, 2)
    if account_capital is None or account_capital <= 0:
        return {
            "lot_cost": lot_cost, "afford_lots": None,
            "commission_sensitive": None, "fit": "unknown_account_cash",
            "label": "一手金额已计算；未录入真实现金，购买能力不评估",
        }
    per_slot = account_capital / max_positions   # 单仓目标金额
    lots = int(per_slot // lot_cost) if lot_cost > 0 else 0
    if lots < 1:
        fit, label = "poor", f"¥{price:.2f}/股·一手¥{lot_cost:,.0f} 超单项观察预算"
    elif lot_cost < _COMMISSION_SENSITIVE_YUAN:
        fit, label = "ok", f"一手金额¥{lot_cost:,.0f}<1万 · 佣金敏感"
    else:
        fit, label = "good", "一手金额可覆盖"
    return {"lot_cost": lot_cost, "afford_lots": lots,
            "commission_sensitive": lot_cost < _COMMISSION_SENSITIVE_YUAN and lots >= 1,
            "fit": fit, "label": label}

def _company_profile_preheat_status() -> dict:
    """Return bounded worker state for diagnostics without exposing mutable globals."""
    with _CP_PREHEAT_LOCK:
        return {
            "workers": 3,
            "capacity": _CP_PREHEAT_CAPACITY,
            "inflight": sorted(_CP_PREHEAT_INFLIGHT),
            "last_errors": dict(_CP_PREHEAT_ERRORS),
        }


def _company_profile_preheat_available_slots() -> int:
    """Return real queue headroom before a persistent coverage claim."""
    with _CP_PREHEAT_LOCK:
        return max(0, _CP_PREHEAT_CAPACITY - len(_CP_PREHEAT_INFLIGHT))

def _ensure_company_profile(
    code: str, name: str, force: bool = False,
    *, include_supplementary: bool = True,
) -> bool:
    """Schedule one bounded background build; duplicate codes never queue twice."""
    code = str(code or "").strip()
    if not code:
        return False
    now = datetime.now().timestamp()
    with _CP_PREHEAT_LOCK:
        if code in _CP_PREHEAT_INFLIGHT:
            return False
        if not force and now - _CP_PREHEAT_TRIED.get(code, 0) < _CP_PREHEAT_INTERVAL_S:
            return False
        if len(_CP_PREHEAT_INFLIGHT) >= _CP_PREHEAT_CAPACITY:
            _CP_PREHEAT_ERRORS.pop(code, None)
            _CP_PREHEAT_ERRORS[code] = {
                "at": datetime.now(_SH_TZ).isoformat(timespec="seconds"),
                "error": "preheat_queue_full",
            }
            while len(_CP_PREHEAT_ERRORS) > 50:
                _CP_PREHEAT_ERRORS.pop(next(iter(_CP_PREHEAT_ERRORS)))
            return False
        _CP_PREHEAT_TRIED[code] = now
        _CP_PREHEAT_INFLIGHT.add(code)

    def _run() -> None:
        try:
            from .company_profile import build_company_profile
            build_company_profile(
                code, name, force_refresh=force,
                include_audit_opinion=include_supplementary,
            )
        except Exception as exc:
            with _CP_PREHEAT_LOCK:
                _CP_PREHEAT_ERRORS.pop(code, None)
                _CP_PREHEAT_ERRORS[code] = {
                    "at": datetime.now(_SH_TZ).isoformat(timespec="seconds"),
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
                while len(_CP_PREHEAT_ERRORS) > 50:
                    _CP_PREHEAT_ERRORS.pop(next(iter(_CP_PREHEAT_ERRORS)))
            print(f"[company_profile] {code} 后台预热失败: {type(exc).__name__}: {str(exc)[:200]}")
        else:
            with _CP_PREHEAT_LOCK:
                _CP_PREHEAT_ERRORS.pop(code, None)
        finally:
            with _CP_PREHEAT_LOCK:
                _CP_PREHEAT_INFLIGHT.discard(code)

    try:
        _CP_PREHEAT_EXECUTOR.submit(_run)
    except Exception as exc:
        with _CP_PREHEAT_LOCK:
            _CP_PREHEAT_INFLIGHT.discard(code)
            _CP_PREHEAT_ERRORS[code] = {
                "at": datetime.now(_SH_TZ).isoformat(timespec="seconds"),
                "error": f"submit_failed: {type(exc).__name__}: {str(exc)[:160]}",
            }
            while len(_CP_PREHEAT_ERRORS) > 50:
                _CP_PREHEAT_ERRORS.pop(next(iter(_CP_PREHEAT_ERRORS)))
        return False
    return True

_COMPANY_PROFILE_REFRESH_SOURCES = (
    "financials", "balance_sheet", "business", "pledge", "announcements",
)
_COMPANY_PROFILE_INDIVIDUAL_REFRESH_SOURCES = (
    *_COMPANY_PROFILE_REFRESH_SOURCES, "audit_opinion",
)


def _company_profile_needs_refresh(
    summary: object, *, include_supplementary: bool = False,
) -> bool:
    """Refresh only source staleness/missingness, not structural gate unknowns."""
    if not isinstance(summary, dict):
        return True
    if summary.get("status") == "stale":
        return True
    if include_supplementary:
        goodwill_status = str(
            (((summary.get("gates") or {}).get("fundamentals") or {}).get("metrics") or {}).get(
                "goodwill_reporting_status"
            ) or ""
        )
        if goodwill_status == "inferred_blank_zero_unverified":
            return True
    sources = summary.get("data_sources")
    if not isinstance(sources, dict):
        return summary.get("status") in {"missing", "unavailable"}
    required_sources = (
        _COMPANY_PROFILE_INDIVIDUAL_REFRESH_SOURCES
        if include_supplementary else _COMPANY_PROFILE_REFRESH_SOURCES
    )
    return any(
        (sources.get(source) or {}).get("status") != "available"
        for source in required_sources
    )


def _warm_pool_profiles(cands: list) -> dict:
    """对候选池中缺失或过期的档案做有界后台刷新，提高 ready 覆盖。

    固定 worker 与有界排队，主流程零阻塞。绝不阻塞轮询、绝不伪造评分——档案没生成就绪前
    只按当前可得证据评分。刷新受一小时节流约束，不让扫描循环反复拉同一股票。
    """
    from .company_profile import get_cached_summary
    stats = {
        "requested": len(cands), "inspected": 0, "scheduled": 0,
        "already_current": 0, "deferred": 0, "queue_full": False,
    }
    for c in cands:
        with _CP_PREHEAT_LOCK:
            if len(_CP_PREHEAT_INFLIGHT) >= _CP_PREHEAT_CAPACITY:
                stats["queue_full"] = True
                break
        code = c.get("code") or c
        name = c.get("name") or code
        stats["inspected"] += 1
        try:
            summary = get_cached_summary(code, name)
            if _company_profile_needs_refresh(summary):
                if _ensure_company_profile(
                    code, name, include_supplementary=False,
                ):
                    stats["scheduled"] += 1
                else:
                    stats["deferred"] += 1
            else:
                stats["already_current"] += 1
        except Exception:
            stats["deferred"] += 1
    return stats

# ── 全市场找股候选池：审计日线预筛后与 Kronos 候选合并 ──
_FM_SNAPSHOT = Path(__file__).resolve().parent.parent / "research" / "full_market_top300.json"
_FM_MERGE_LIMIT = 300      # 全市场短名单全部参加本地轻评分；联网档案只预热最终24只深评池。
_FM_WARM_BATCH = 60        # 刷新时单批预热档案上限（300 只一次性预热曾与扫描抢网络，封顶后余量由扫描懒预热）
_FM_REFRESH_S = 4 * 3600   # 全市场候选池刷新周期
_FM_SNAPSHOT_LOCK = threading.Lock()
_FM_LAST_GOOD: tuple[Path, dict[str, object]] | None = None
_FM_REFRESH_STATE = {
    "lock": threading.Lock(), "last_ts": 0.0, "running": False,
    "last_error": None, "last_count": 0, "factor_forward_error": None,
}

def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    """Durably replace one JSON snapshot without exposing a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _persist_forward_capture_runtime(
    path: Path | None, *, status: str, started_at: datetime,
    error: str | None = None, snapshot: dict[str, object] | None = None,
) -> None:
    """Leave a durable trace when a capture is killed before it can finish."""
    if path is None:
        return
    results = {}
    for key in _FORWARD_CAPTURE_RESULT_FIELDS:
        item = (snapshot or {}).get(key)
        if isinstance(item, dict):
            results[key] = {
                field: item.get(field)
                for field in ("status", "captured", "error", "packet_id")
                if field in item
            }
    now = datetime.now(_SH_TZ)
    _atomic_write_json(path, {
        "schema": "forward_capture_runtime_status.v1",
        "status": status,
        "pid": os.getpid(),
        "decision_day": started_at.astimezone(_SH_TZ).date().isoformat(),
        "started_at": started_at.astimezone(_SH_TZ).isoformat(timespec="seconds"),
        "updated_at": now.isoformat(timespec="seconds"),
        "error": error,
        "results": results,
    })


def _full_market_snapshot_identity() -> dict[str, str]:
    """Return the immutable identity of the exact full-market snapshot bytes."""
    try:
        raw = _FM_SNAPSHOT.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("top"), list):
        return {}
    generated_at = str(payload.get("generated_at") or "").strip()
    data_asof = str(payload.get("data_asof") or "").strip()
    if not generated_at or not data_asof:
        return {}
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "generated_at": generated_at,
        "data_asof": data_asof,
    }


def _opportunity_full_market_source_changed(
    snapshot: object, current_identity: dict[str, str] | None = None,
) -> bool:
    """Treat a bound opportunity snapshot as stale when its source changed."""
    source = snapshot if isinstance(snapshot, dict) else {}
    pipeline = (
        source.get("pipeline_identity")
        if isinstance(source.get("pipeline_identity"), dict) else {}
    )
    bound = pipeline.get("full_market_snapshot")
    if not isinstance(bound, dict):
        return False
    current = current_identity or _full_market_snapshot_identity()
    return not current or any(
        bound.get(key) != current.get(key)
        for key in ("sha256", "generated_at", "data_asof")
    )


def _full_market_refresh_needed(
    project: Path = _PROJECT, snapshot_path: Path = _FM_SNAPSHOT,
) -> bool:
    """Refresh once when verified daily audits move past the frozen snapshot."""
    realtime = audited_realtime_universe(project, snapshot_path)
    if realtime.get("eligible_snapshot_complete") is True:
        return _professional_coverage_refresh_ready(snapshot_path)
    return audited_universe(project).get("status") == "verified"


def _professional_coverage_refresh_ready(
    snapshot_path: Path = _FM_SNAPSHOT,
) -> bool:
    """Rebuild one partial ranking only after its financial repair jobs finish."""
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        preview = snapshot.get("professional_preview") or {}
        scan_audit = snapshot.get("scan_audit") or {}
        complete = preview.get("complete")
        if complete is None:
            complete = scan_audit.get("professional_preview_complete")
        if complete is not False:
            return False
        from .professional_coverage import coverage_job_status, coverage_state

        state = coverage_state(snapshot)
        epoch_id = str(state.get("epoch_id") or "")
        if state.get("status") != "ready" or not epoch_id:
            return False
        jobs = coverage_job_status(
            _PROJECT / "research" / "professional_coverage.sqlite3",
            epoch_id,
        )
        total = int(jobs.get("total") or 0)
        states = jobs.get("states") or {}
        return total > 0 and int(states.get("success") or 0) == total
    except (OSError, json.JSONDecodeError, sqlite3.Error, TypeError, ValueError):
        return False


def _forward_prepare_stage(
    *, observed_at: datetime, realtime_universe: dict | None = None,
    live_signal_snapshot: dict | None = None,
) -> str:
    """Choose the cheapest missing upstream stage before opportunity ranking."""
    realtime = realtime_universe or audited_realtime_universe(
        _PROJECT, _FM_SNAPSHOT,
    )
    if realtime.get("eligible_snapshot_complete") is not True:
        return "full_market"
    live = live_signal_snapshot if live_signal_snapshot is not None else (
        load_live_signals() or {}
    )
    selection = verify_forward_selection_snapshot(
        {"selection_provenance": live.get("selection_audit") or {}},
        observed_at=observed_at,
    )
    return "opportunities" if selection.get("valid") is True else "live_signal"


def _read_full_market_snapshot() -> dict[str, object]:
    global _FM_LAST_GOOD
    with _FM_SNAPSHOT_LOCK:
        try:
            payload = json.loads(_FM_SNAPSHOT.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("top"), list):
                raise ValueError("full_market_snapshot_schema_invalid")
            _FM_LAST_GOOD = (_FM_SNAPSHOT, payload)
            return payload
        except (OSError, json.JSONDecodeError, ValueError):
            if _FM_LAST_GOOD is not None and _FM_LAST_GOOD[0] == _FM_SNAPSHOT:
                return _FM_LAST_GOOD[1]
            return {}

def _load_full_market_candidates(limit: int = _FM_MERGE_LIMIT) -> list[dict]:
    """读全市场找股快照，返回与 load_kronos_top 同形状的候选（source='full_market'）。

    历史涨跌与模型预测分字段保存；全市场初筛绝不伪造 pred_ret。
    """
    data = _read_full_market_snapshot()
    if not data:
        return []
    source_as_of = data.get("data_asof") or data.get("snapshot_date") or data.get("generated_at")
    eligible = data.get("eligible") if isinstance(data.get("eligible"), list) else []
    rows = eligible or (data.get("top") or [])
    row_by_code = {
        str(row.get("code")): row for row in rows
        if isinstance(row, dict) and row.get("code")
    }
    live_snapshot = load_live_signals() or {}
    live_scope = live_snapshot.get("watchlist_scope") or {}
    selected_codes = live_snapshot.get("monitor_selected_codes") or []
    if (
        isinstance(live_scope, dict)
        and live_scope.get("data_asof") == source_as_of
        and isinstance(selected_codes, list)
    ):
        selected_rows = [row_by_code.get(str(code)) for code in selected_codes]
        selected_rows = [row for row in selected_rows if isinstance(row, dict)]
    else:
        selected_rows = []
    if not selected_rows:
        selected_rows = list(data.get("top") or rows)
    items = []
    for c in selected_rows[:limit]:
        try:
            code = str(c["code"])
        except (KeyError, TypeError):
            continue
        items.append({
            "code": code, "source": "full_market",
            "name": c.get("name"),
            "close": _finite_number(c.get("close")),
            "pred_ret_5d": None, "pred_ret_10d": None,
            "pred_ret_20d": None, "pred_ret_30d": None,
            "trailing_5d_pct": _finite_number(c.get("ret_5d")),
            "trailing_20d_pct": _finite_number(c.get("ret_20d")),
            "trailing_60d_pct": _finite_number(c.get("ret_60d")),
            "avg_amount_20d": _finite_number(c.get("avg_amount_20d")),
            "screen_score": _finite_number(c.get("screen_score")),
            "screen_method": c.get("screen_method") or data.get("ranking_method"),
            "screen_components": copy.deepcopy(c.get("screen_components") or {}),
            "preselection_professional_preview": copy.deepcopy(
                c.get("preselection_professional_preview") or {}
            ),
            "float_cap_proxy_20d_median": _finite_number(
                c.get("float_cap_proxy_20d_median")
            ),
            "classification_label": c.get("classification_label"),
            "classification_status": c.get("classification_status"),
            "market_segment": c.get("market_segment"),
            "source_data_as_of": source_as_of,
            "source_generated_at": data.get("generated_at"),
            "_industry": str(c.get("industry") or ""),
        })
    return items

def _merge_candidates(*groups: list[dict]) -> list[dict]:
    """多候选源去重合并（保持组内顺序，先 kronos 后全市场）。"""
    seen: set[str] = set()
    out: list[dict] = []
    for g in groups:
        for c in g or []:
            code = c.get("code")
            if code and code not in seen:
                seen.add(code)
                out.append(c)
    return out

def _interleave_candidates(*groups: list[dict]) -> list[dict]:
    """多候选源**轮流取**去重合并（机会池用）：保证每个来源在前排都有代表，
    否则 kronos 在前会把全市场候选全部压到 monitor_n 之后、永远监控不到。"""
    gs = [list(g or []) for g in groups]
    seen: set[str] = set()
    out: list[dict] = []
    while any(gs):
        for g in gs:
            if g:
                c = g.pop(0)
                if c.get("code") not in seen:
                    seen.add(c.get("code"))
                    out.append(c)
    return out


def _admitted_model_candidates(rows: list[dict]) -> list[dict]:
    """Only independently validated model outputs may decide candidate admission."""
    return [
        row for row in rows
        if row.get("validation_status") == "validated"
        and row.get("freshness_status") == "fresh"
        and row.get("decision_eligible") is True
        and row.get("research_only") is False
    ]

def _run_full_market_screen(top_n: int = 300) -> dict[str, object]:
    """进程内跑一次全市场找股，并返回候选与同轮扫描审计。"""
    import importlib.util
    fpath = Path(__file__).resolve().parent.parent / "research" / "full_market_finder.py"
    if not fpath.exists():
        return {}
    try:
        spec = importlib.util.spec_from_file_location("full_market_finder", fpath)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        result = mod.screen_full_market_snapshot(top_n, quiet=True)
        return result if isinstance(result, dict) else {}
    except Exception as exc:
        with _FM_REFRESH_STATE["lock"]:
            _FM_REFRESH_STATE["last_error"] = type(exc).__name__
        _LOG.exception("full market screen failed")
        return {}

def _refresh_full_market_pool() -> None:
    """后台刷新全市场候选池：重跑离线筛选 → 写快照 → 预热池内未就绪档案。"""
    try:
        universe = audited_universe(_PROJECT)
        requested = int(universe.get("count") or 0)
        if universe.get("status") != "verified" or requested <= 0:
            with _FM_REFRESH_STATE["lock"]:
                _FM_REFRESH_STATE["last_error"] = "universe_audit_not_verified"
            return
        result = _run_full_market_screen(requested)
        eligible = result.get("top") if isinstance(result, dict) else None
        if not eligible:
            with _FM_REFRESH_STATE["lock"]:
                _FM_REFRESH_STATE["last_error"] = "empty_result"
            return
        scan_audit = result.get("scan_audit") or {}
        eligible_complete = bool(
            scan_audit.get("status") == "verified"
            and scan_audit.get("complete_path_scan") is True
            and int(scan_audit.get("ranked_count") or 0) == len(eligible)
            and int(scan_audit.get("current_asof_count") or 0) == len(eligible)
        )
        if not eligible_complete:
            with _FM_REFRESH_STATE["lock"]:
                _FM_REFRESH_STATE["last_error"] = "eligible_snapshot_incomplete"
            return
        top = eligible[:_FM_MERGE_LIMIT]
        payload = {
            "generated_at": datetime.now(_SH_TZ).isoformat(timespec="seconds"),
            "data_asof": eligible[0].get("data_asof") if eligible else None,
            "ranking_method": (
                scan_audit.get("selection_method")
                or (eligible[0].get("screen_method") if eligible else None)
            ),
            "market_screen_method": (
                eligible[0].get("screen_method") if eligible else None
            ),
            "professional_preview": {
                "available_count": scan_audit.get(
                    "professional_preview_available_count"
                ),
                "scope_count": scan_audit.get(
                    "professional_preview_scope_count"
                ),
                "coverage_pct": scan_audit.get(
                    "professional_preview_coverage_pct"
                ),
                "complete": scan_audit.get("professional_preview_complete") is True,
                "partial_ranking_forbidden": (
                    scan_audit.get("professional_preview_partial_ranking_forbidden")
                    is True
                ),
            },
            "universe_count": universe["count"],
            "universe_audit": universe,
            "scan_audit": scan_audit,
            "eligible_count": len(eligible),
            "eligible_complete": True,
            "eligible": eligible,
            "top_count": len(top),
            "top": top,
        }
        with _FM_SNAPSHOT_LOCK:
            _atomic_write_json(_FM_SNAPSHOT, payload)
            global _FM_LAST_GOOD
            _FM_LAST_GOOD = (_FM_SNAPSHOT, payload)
        factor_forward_error = None
        try:
            from .leader_crowding_factor_forward_v3 import capture
            capture(_PROJECT)
        except Exception as exc:
            factor_forward_error = type(exc).__name__
            _LOG.exception("emotion-routed leader factor forward capture failed")
        with _FM_REFRESH_STATE["lock"]:
            _FM_REFRESH_STATE.update({
                "last_ts": _time.time(), "last_count": len(top), "last_error": None,
                "factor_forward_error": factor_forward_error,
            })
        # 预热全市场新候选的公司档案，避免下轮扫描把高分股挡在 preheating。
        # 只预热 top 批次（_FM_WARM_BATCH）——300 只一次性预热会把 8GB 机打到 waitress 队列饱和，
        # 余量由最终深评池按需懒预热；轻评分阶段不联网预热档案。
        _warm_pool_profiles([
            {"code": c["code"], "name": c["code"]} for c in top[:_FM_WARM_BATCH]
        ])
    except Exception as exc:
        with _FM_REFRESH_STATE["lock"]:
            _FM_REFRESH_STATE["last_error"] = type(exc).__name__
        _LOG.exception("full market refresh failed")
    finally:
        with _FM_REFRESH_STATE["lock"]:
            _FM_REFRESH_STATE["running"] = False

def _full_market_initial_wait(now: float | None = None) -> float:
    """Avoid recomputing 4,802 local files just because the web server restarted."""

    try:
        age = max(0.0, (now if now is not None else _time.time()) - _FM_SNAPSHOT.stat().st_mtime)
    except OSError:
        return 0.0
    return max(0.0, _FM_REFRESH_S - age)

def _full_market_refresher_loop(stop_event: threading.Event) -> None:
    """Follow verified audit identity changes without periodic rescans."""
    while not stop_event.is_set():
        if _full_market_refresh_needed():
            _try_start_snapshot_worker(
                _FM_REFRESH_STATE, _refresh_full_market_pool,
                name="finder-refresh",
            )
        if stop_event.wait(60):
            break

# 资金流快照是研究数据，不是报价接口。所有下游页面统一经过这里，避免把
# 读取文件的当前时间当成行情时间，也避免单条坏数据污染涨跌/排序结论。
_FUNDFLOW_PCT_ABS_LIMIT = 35.0
_FUNDFLOW_BALANCE_TOLERANCE_YI = 0.03
_SH_TZ = ZoneInfo("Asia/Shanghai")
_PAPER_FORWARD_STATUS = _PROJECT / "reports" / "paper_forward_status.json"
_STRATEGY_REGISTRY = _PROJECT / "reports" / "mom_strategy_registry.json"
_TASKBOOK_V3_GATE = _PROJECT / "reports" / "taskbook_v3_gate_result.json"
_V86_FORWARD_LATEST = _PROJECT / "reports" / "v86_expectancy_forward_latest.json"
_V86_FORWARD_EVALUATION = _PROJECT / "reports" / "v86_expectancy_forward_evaluation.json"
_V86_FORWARD_SCHEDULE = _PROJECT / "reports" / "v86_forward_observation_schedule_status.json"
_V86_FORWARD_PROMOTION = _PROJECT / "reports" / "v86_forward_promotion_audit.json"
_V86_EXTERNAL_REPRODUCTION = _PROJECT / "reports" / "v86_external_reproduction_status.json"
_V86_FINAL_CANDIDATE_GATE = _PROJECT / "reports" / "v86_final_candidate_gate.json"
_V3_PROFIT_SOURCE_AUDIT = _PROJECT / "reports" / "v3_profit_source_failure_audit.json"
_V86_REGIME_ROBUSTNESS_GATE = _PROJECT / "reports" / "v86_regime_robustness_gate.json"
_V86_ROLLING_OOS_GATE = _PROJECT / "reports" / "v86_purged_rolling_oos_gate.json"
_V86_COST_CAPACITY_GATE = _PROJECT / "reports" / "v86_cost_capacity_gate.json"
_V86_CORPORATE_ACTION_GATE = _PROJECT / "reports" / "v86_corporate_action_gate.json"
_V86_TEMPORAL_DEPENDENCE_GATE = _PROJECT / "reports" / "v86_temporal_dependence_gate.json"
_V46_TEMPORAL_DEPENDENCE_AUDIT = _PROJECT / "reports" / "v46_temporal_dependence_audit.json"
_V86_SELECTION_BENCHMARK_GATE = _PROJECT / "reports" / "v86_selection_benchmark_gate.json"
_V46_SELECTION_BENCHMARK_AUDIT = _PROJECT / "reports" / "v46_selection_benchmark_audit.json"
_V86_MULTI_BENCHMARK_GATE = _PROJECT / "reports" / "v86_multi_benchmark_gate.json"
_V46_MULTI_BENCHMARK_AUDIT = _PROJECT / "reports" / "v46_multi_benchmark_audit.json"
_V86_CONTRIBUTOR_CONCENTRATION_GATE = _PROJECT / "reports" / "v86_contributor_concentration_gate.json"
_V46_CONTRIBUTOR_CONCENTRATION_AUDIT = _PROJECT / "reports" / "v46_contributor_concentration_audit.json"
_V86_MODEL_TRAINING_INTEGRITY_GATE = _PROJECT / "reports" / "v86_model_training_integrity_gate.json"
_V46_TRAINING_BOUNDARY_PURGE_AUDIT = _PROJECT / "reports" / "v46_training_boundary_purge_audit.json"
_V87_SUCCESSOR_ADMISSION_GATE = _PROJECT / "reports" / "v87_successor_research_admission_gate.json"
_V87_CSRC_MAPPING_FAILURE = _PROJECT / "reports" / "v87_csrc_enforcement_mapping_failure_diagnostic.json"
_V88_CSRC_MAPPING_AUDIT = _PROJECT / "reports" / "v88_csrc_scoped_entity_mapping_audit.json"
_V89_SSE_DATA_EPOCH_AUDIT = _PROJECT / "reports" / "v89_sse_regulatory_measures_data_epoch_audit.json"
_V90_SUCCESSOR_STATUS = _PROJECT / "reports" / "v90_successor_research_status.json"
_V120_SUCCESSOR_STATUS = _PROJECT / "reports" / "v120_successor_research_status.json"
_V143_SUCCESSOR_STATUS = _PROJECT / "reports" / "v143_successor_research_status.json"
_V145_SUCCESSOR_STATUS = _PROJECT / "reports" / "v145_successor_research_status.json"
_DEEPSEEK_DISCOVERY_ROUND = _PROJECT / "reports" / "DEEPSEEK_DISCOVERY_ROUND_LATEST.json"
_DEEPSEEK_DISCOVERY_ASSESSMENT = _PROJECT / "reports" / "DEEPSEEK_DISCOVERY_ASSESSMENT_LATEST.json"
_V88_CSRC_MAPPING_REVIEW_QUEUE = (
    _PROJECT / "research" / "data" / "v88_csrc_scoped_entity_mapping"
    / "blind_review_queue.csv"
)
_V88_CSRC_MAPPING_REVIEW_LABELS = (
    _PROJECT / "research" / "data" / "v88_csrc_scoped_mapping_review_labels.csv"
)
_V88_CSRC_OFFICIAL_ALIASES = (
    _PROJECT / "research" / "data" / "v87_csrc_enforcement_entity_mapping"
    / "official_security_aliases.csv"
)
_V88_MAPPING_REVIEW_LOCK = threading.Lock()
_V88_MAPPING_REVIEW_FIELDS = (
    "manuscript_id",
    "correct_stock_codes",
    "correct_entity_type",
    "reviewer_id",
    "reviewed_at",
    "notes",
)
_V88_MAPPING_ENTITY_TYPES = {
    "issuer", "both", "individual", "other_institution", "ambiguous", "unmapped",
}
_V88_MAPPING_STOCK_CODE = re.compile(
    r"^(?:sh\.(?:600|601|603|605|688|689)|"
    r"sz\.(?:000|001|002|003|300|301))\d{3}$"
)

def _read_v88_mapping_review_queue() -> list[dict[str, str]]:
    with _V88_CSRC_MAPPING_REVIEW_QUEUE.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))

def _read_v88_mapping_review_labels() -> dict[str, dict[str, str]]:
    if not _V88_CSRC_MAPPING_REVIEW_LABELS.exists():
        return {}
    with _V88_CSRC_MAPPING_REVIEW_LABELS.open(
        encoding="utf-8", newline=""
    ) as stream:
        return {
            row["manuscript_id"]: row
            for row in csv.DictReader(stream)
            if row.get("manuscript_id")
        }

def _v88_mapping_review_snapshot(index: int | None = None) -> dict[str, object]:
    queue = _read_v88_mapping_review_queue()
    labels = _read_v88_mapping_review_labels()
    queue_ids = {row["manuscript_id"] for row in queue}
    completed = sum(manuscript_id in queue_ids for manuscript_id in labels)
    if not queue:
        raise RuntimeError("V88 人工盲审队列为空")
    if index is None:
        index = next(
            (position for position, row in enumerate(queue)
             if row["manuscript_id"] not in labels),
            len(queue) - 1,
        )
    index = max(0, min(index, len(queue) - 1))
    item = dict(queue[index])
    manuscript_id = item["manuscript_id"]
    existing = labels.get(manuscript_id)
    if existing:
        existing = {
            "manuscript_id": manuscript_id,
            "correct_stock_codes": [
                value for value in existing.get("correct_stock_codes", "").split(";")
                if value and value.upper() != "NONE"
            ],
            "correct_entity_type": existing.get("correct_entity_type", ""),
            "reviewer_id": existing.get("reviewer_id", ""),
            "reviewed_at": existing.get("reviewed_at", ""),
            "notes": existing.get("notes", ""),
        }
    return {
        "total": len(queue),
        "completed": completed,
        "remaining": len(queue) - completed,
        "index": index,
        "item": item,
        "existing_label": existing,
        "predictions_exposed": False,
        "blindness": "仅显示冻结原文，不显示 V88 预测代码、别名或实体分类。",
    }

def _parse_v88_review_codes(value: object) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[;,，\s]+", value.strip()) if value.strip() else []
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        values = value
    else:
        raise ValueError("correct_stock_codes 必须是字符串或字符串数组")
    codes = sorted({item.strip().lower() for item in values if item.strip()})
    if len(codes) == 1 and codes[0] == "none":
        return []
    if any(not _V88_MAPPING_STOCK_CODE.fullmatch(code) for code in codes):
        raise ValueError("股票代码必须使用 sh.600000 / sz.000001 格式，或留空表示 NONE")
    return codes

def _search_v88_official_aliases(query: str, limit: int = 20) -> list[dict[str, str]]:
    needle = query.strip().casefold()
    if len(needle) < 2:
        return []
    results: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    with _V88_CSRC_OFFICIAL_ALIASES.open(
        encoding="utf-8", newline=""
    ) as stream:
        for row in csv.DictReader(stream):
            alias = str(row.get("alias") or "").strip()
            code = str(row.get("code") or "").strip().lower()
            key = (code, alias)
            if (
                needle not in alias.casefold()
                or key in seen
                or not _V88_MAPPING_STOCK_CODE.fullmatch(code)
            ):
                continue
            seen.add(key)
            results.append({
                "code": code,
                "alias": alias,
                "alias_type": str(row.get("alias_type") or ""),
                "effective_date": str(row.get("effective_date") or ""),
                "source": str(row.get("source") or ""),
            })
            if len(results) >= max(1, min(limit, 50)):
                break
    return results

def _save_v88_mapping_review_label(
    manuscript_id: str, payload: dict[str, object]
) -> dict[str, object]:
    queue = _read_v88_mapping_review_queue()
    queue_ids = {row["manuscript_id"] for row in queue}
    if manuscript_id not in queue_ids:
        raise LookupError("稿件不在冻结的 V88 盲审队列中")
    codes = _parse_v88_review_codes(payload.get("correct_stock_codes"))
    entity_type = str(payload.get("correct_entity_type") or "").strip().lower()
    if entity_type not in _V88_MAPPING_ENTITY_TYPES:
        raise ValueError("correct_entity_type 不在允许集合中")
    reviewer_id = str(os.environ.get("OLYMPUS_MAPPING_REVIEWER_ID") or "").strip()
    if not reviewer_id or len(reviewer_id) > 80:
        raise PermissionError("盲审写入未配置服务端审核员身份")
    claimed_reviewer = str(payload.get("reviewer_id") or "").strip()
    if claimed_reviewer and not hmac.compare_digest(claimed_reviewer, reviewer_id):
        raise PermissionError("客户端 reviewer_id 与服务端审核员身份不一致")
    notes = str(payload.get("notes") or "").strip()
    if len(notes) > 1000:
        raise ValueError("notes 不得超过 1000 个字符")
    row = {
        "manuscript_id": manuscript_id,
        "correct_stock_codes": ";".join(codes) if codes else "NONE",
        "correct_entity_type": entity_type,
        "reviewer_id": reviewer_id,
        "reviewed_at": datetime.now(_SH_TZ).isoformat(timespec="seconds"),
        "notes": notes,
    }
    with _V88_MAPPING_REVIEW_LOCK:
        labels = _read_v88_mapping_review_labels()
        existing = labels.get(manuscript_id)
        if existing:
            unchanged = all(
                str(existing.get(key) or "") == str(row.get(key) or "")
                for key in (
                    "manuscript_id", "correct_stock_codes",
                    "correct_entity_type", "reviewer_id", "notes",
                )
            )
            if unchanged:
                return _v88_mapping_review_snapshot()
            raise FileExistsError("该稿件已有盲审标签；禁止静默覆盖，请保留审计历史")
        labels[manuscript_id] = row
        _V88_CSRC_MAPPING_REVIEW_LABELS.parent.mkdir(parents=True, exist_ok=True)
        temporary = _V88_CSRC_MAPPING_REVIEW_LABELS.with_name(
            f".{_V88_CSRC_MAPPING_REVIEW_LABELS.name}.{os.getpid()}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=_V88_MAPPING_REVIEW_FIELDS)
                writer.writeheader()
                writer.writerows(
                    labels[key] for key in sorted(labels)
                    if key in queue_ids
                )
            temporary.replace(_V88_CSRC_MAPPING_REVIEW_LABELS)
        finally:
            if temporary.exists():
                temporary.unlink()
    return _v88_mapping_review_snapshot()

_BOUNDED_CALL_SLOTS = threading.BoundedSemaphore(8)
_BOUNDED_CALL_CIRCUIT_LOCK = threading.Lock()
_BOUNDED_CALL_TIMED_OUT: set[int] = set()
_BOUNDED_CALL_TELEMETRY: dict[str, object] = {}

def _bounded_call(callback, timeout_seconds: float):
    return _run_bounded_call(
        callback, timeout_seconds, slots=_BOUNDED_CALL_SLOTS,
        lock=_BOUNDED_CALL_CIRCUIT_LOCK, timed_out=_BOUNDED_CALL_TIMED_OUT,
        telemetry=_BOUNDED_CALL_TELEMETRY,
    )

def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}

def _latest_paper_forward_report() -> dict[str, object]:
    """Prefer the append-only manual refresh state over the frozen zero-day baseline."""
    return (
        _read_json(
            _PROJECT / "reports" / "v215_global_defensive_manual_forward_refresh.json"
        )
        or _read_json(
            _PROJECT / "reports" / "v213_global_defensive_paper_forward_baseline.json"
        )
    )

_PAPER_FORWARD_REFRESH_LOCK = threading.Lock()

def _run_manual_paper_forward_refresh() -> dict[str, object]:
    from research.v225_stability_80_20_manual_forward import refresh

    return refresh()

@lru_cache(maxsize=4096)
def _cached_sha256_matches(
    path: str, size: int, mtime_ns: int, declared_sha: str,
) -> bool:
    del size, mtime_ns
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == declared_sha

def _manifest_physical_evidence(
    manifest_path: Path, *, project_root: Path,
) -> dict[str, object]:
    """Check project-local files exist and match declared SHA-256 values."""
    manifest = _read_json(manifest_path)
    declared = int(manifest.get("trade_dates") or 0)
    items = manifest.get("files")
    listed = items if isinstance(items, list) else []
    root = project_root.resolve()
    physical = 0
    hash_verified = 0
    missing_hashes = 0
    invalid_paths = 0
    for item in listed:
        raw = item.get("path") if isinstance(item, dict) else None
        if not raw:
            invalid_paths += 1
            continue
        candidate = Path(str(raw))
        candidate = candidate if candidate.is_absolute() else root / candidate
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            invalid_paths += 1
            continue
        try:
            if resolved.is_file() and resolved.stat().st_size > 40:
                physical += 1
                declared_sha = str(item.get("sha256") or "").lower()
                if len(declared_sha) != 64:
                    missing_hashes += 1
                else:
                    stat = resolved.stat()
                    if _cached_sha256_matches(
                        str(resolved), stat.st_size, stat.st_mtime_ns,
                        declared_sha,
                    ):
                        hash_verified += 1
        except OSError:
            continue
    declared_gate = (manifest.get("data_gate") or {}).get("passed") is True
    passed = bool(
        declared_gate
        and declared > 0
        and len(listed) == declared
        and physical == declared
        and hash_verified == declared
        and missing_hashes == 0
        and invalid_paths == 0
    )
    return {
        "passed": passed,
        "date_range": manifest.get("date_range"),
        "declared_gate_passed": declared_gate,
        "declared_trade_dates": declared,
        "listed_files": len(listed),
        "physical_files": physical,
        "hash_verified_files": hash_verified,
        "missing_hashes": missing_hashes,
        "invalid_paths": invalid_paths,
    }

def _v90_pmi_physical_evidence(
    manifest_path: Path, audit_path: Path, *, project_root: Path,
) -> dict[str, object]:
    """Verify every archived official V90 page plus the derived CSV."""
    manifest = _read_json(manifest_path)
    audit = _read_json(audit_path)
    terms = manifest.get("terms")
    listings = manifest.get("listings")
    releases = manifest.get("releases")
    csv_item = manifest.get("csv")
    source_items: list[dict[str, object]] = []
    if isinstance(terms, dict):
        source_items.append(terms)
    if isinstance(listings, list):
        source_items.extend(item for item in listings if isinstance(item, dict))
    if isinstance(releases, list):
        source_items.extend(
            {"path": item.get("archive_path"), "sha256": item.get("sha256")}
            for item in releases if isinstance(item, dict)
        )

    root = project_root.resolve()

    def verified(item: dict[str, object]) -> tuple[bool, bool]:
        raw = item.get("path")
        declared_sha = str(item.get("sha256") or "").lower()
        if not raw or len(declared_sha) != 64:
            return False, False
        candidate = Path(str(raw))
        candidate = candidate if candidate.is_absolute() else root / candidate
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
            stat = resolved.stat()
        except (OSError, ValueError):
            return False, False
        if not resolved.is_file() or stat.st_size <= 40:
            return False, False
        return True, _cached_sha256_matches(
            str(resolved), stat.st_size, stat.st_mtime_ns, declared_sha,
        )

    checks = [verified(item) for item in source_items]
    physical = sum(int(exists) for exists, _ in checks)
    hashes = sum(int(matches) for _, matches in checks)
    csv_exists, csv_hash = verified(csv_item if isinstance(csv_item, dict) else {})
    expected_archives = int(audit.get("source_archives") or 0)
    actual_months = int(audit.get("actual_months") or 0)
    release_count = len(releases) if isinstance(releases, list) else 0
    passed = bool(
        audit.get("data_epoch_ready") is True
        and expected_archives > 0
        and len(source_items) == expected_archives
        and physical == expected_archives
        and hashes == expected_archives
        and release_count == actual_months == int(audit.get("expected_months") or 48)
        and csv_exists and csv_hash
    )
    return {
        "passed": passed,
        "data_epoch_id": audit.get("data_epoch_id"),
        "month_range": audit.get("month_range"),
        "expected_months": int(audit.get("expected_months") or 48),
        "actual_months": actual_months,
        "source_archives": expected_archives,
        "physical_archives": physical,
        "hash_verified_archives": hashes,
        "csv_hash_verified": csv_hash,
        "first_publication_at": audit.get("first_publication_at"),
        "last_publication_at": audit.get("last_publication_at"),
        "source_attribution": audit.get("source_attribution"),
    }

def _v120_pbc_physical_evidence(
    manifest_path: Path, audit_path: Path, *, project_root: Path,
) -> dict[str, object]:
    """Verify the official PBoC listings, monthly reports, content, and CSV."""
    manifest = _read_json(manifest_path)
    audit = _read_json(audit_path)
    listings = manifest.get("listings") or []
    reports = manifest.get("reports") or []
    root = project_root.resolve()

    def verified(path: object, sha256: object) -> tuple[bool, bool]:
        declared_sha = str(sha256 or "").lower()
        if not path or len(declared_sha) != 64:
            return False, False
        candidate = Path(str(path))
        candidate = candidate if candidate.is_absolute() else root / candidate
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
            stat = resolved.stat()
        except (OSError, ValueError):
            return False, False
        if not resolved.is_file() or stat.st_size <= 40:
            return False, False
        return True, _cached_sha256_matches(
            str(resolved), stat.st_size, stat.st_mtime_ns, declared_sha,
        )

    listing_checks = [
        verified(item.get("path"), item.get("sha256"))
        for item in listings if isinstance(item, dict)
    ]
    report_checks = [
        verified(item.get("archive_path"), item.get("sha256"))
        for item in reports if isinstance(item, dict)
    ]
    content_checks = [
        verified(item.get("content_archive_path"), item.get("content_sha256"))
        for item in reports if isinstance(item, dict)
    ]
    csv_item = manifest.get("csv") or {}
    csv_exists, csv_hash = verified(
        csv_item.get("path") if isinstance(csv_item, dict) else None,
        csv_item.get("sha256") if isinstance(csv_item, dict) else None,
    )

    def count(checks: list[tuple[bool, bool]], index: int) -> int:
        return sum(int(row[index]) for row in checks)

    expected_months = int(audit.get("expected_months") or 96)
    actual_months = int(audit.get("actual_months") or 0)
    expected_listings = int(audit.get("listing_archives") or 18)
    expected_reports = int(audit.get("report_archives") or expected_months)
    gates = audit.get("gates") or {}
    passed = bool(
        audit.get("new_data_epoch_ready") is True
        and gates and all(value is True for value in gates.values())
        and actual_months == expected_months
        and len(listing_checks) == expected_listings
        and count(listing_checks, 0) == count(listing_checks, 1) == expected_listings
        and len(report_checks) == len(content_checks) == expected_reports
        and count(report_checks, 0) == count(report_checks, 1) == expected_reports
        and count(content_checks, 0) == count(content_checks, 1) == expected_reports
        and csv_exists and csv_hash
    )
    return {
        "passed": passed,
        "status": audit.get("status") or "missing",
        "data_epoch_id": audit.get("data_epoch_id"),
        "month_range": audit.get("month_range"),
        "expected_months": expected_months,
        "actual_months": actual_months,
        "listing_archives": expected_listings,
        "physical_listings": count(listing_checks, 0),
        "hash_verified_listings": count(listing_checks, 1),
        "report_archives": expected_reports,
        "physical_reports": count(report_checks, 0),
        "hash_verified_reports": count(report_checks, 1),
        "physical_content_archives": count(content_checks, 0),
        "hash_verified_content_archives": count(content_checks, 1),
        "csv_hash_verified": csv_hash,
        "successor_discovery_allowed": (
            audit.get("successor_discovery_allowed") is True
        ),
        "successor_backtest_allowed": (
            audit.get("successor_backtest_allowed") is True
        ),
        "source_attribution": manifest.get("source_attribution"),
    }

def _v143_nbs_cpi_ppi_physical_evidence(
    manifest_path: Path, audit_path: Path, *, project_root: Path,
) -> dict[str, object]:
    """Verify V143 searches, release responses, terms, CSV and all hashes."""
    manifest = _read_json(manifest_path)
    audit = _read_json(audit_path)
    root = project_root.resolve()

    def verified(path: object, sha256: object) -> tuple[bool, bool]:
        declared = str(sha256 or "").lower()
        if not path or len(declared) != 64:
            return False, False
        candidate = Path(str(path))
        candidate = candidate if candidate.is_absolute() else root / candidate
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
            stat = resolved.stat()
        except (OSError, ValueError):
            return False, False
        if not resolved.is_file() or stat.st_size <= 40:
            return False, False
        return True, _cached_sha256_matches(
            str(resolved), stat.st_size, stat.st_mtime_ns, declared,
        )

    searches = manifest.get("searches") or []
    releases = manifest.get("releases") or []
    search_checks = [
        verified(row.get("path"), row.get("sha256"))
        for row in searches if isinstance(row, dict)
    ]
    release_checks = [
        verified(row.get("archive_path"), row.get("sha256"))
        for row in releases if isinstance(row, dict)
    ]
    terms = manifest.get("terms") or {}
    csv_item = manifest.get("csv") or {}
    terms_exists, terms_hash = verified(terms.get("path"), terms.get("sha256"))
    csv_exists, csv_hash = verified(csv_item.get("path"), csv_item.get("sha256"))

    def count(checks: list[tuple[bool, bool]], index: int) -> int:
        return sum(int(row[index]) for row in checks)

    expected_months = int(audit.get("expected_months") or 120)
    expected_releases = int(audit.get("expected_releases") or 240)
    expected_searches = int(audit.get("search_archives") or 40)
    gates = audit.get("gates") or {}
    passed = bool(
        audit.get("new_data_epoch_ready") is True
        and gates and all(value is True for value in gates.values())
        and int(audit.get("actual_months") or 0) == expected_months
        and len(search_checks) == expected_searches
        and count(search_checks, 0) == count(search_checks, 1) == expected_searches
        and len(release_checks) == expected_releases
        and count(release_checks, 0) == count(release_checks, 1) == expected_releases
        and terms_exists and terms_hash and csv_exists and csv_hash
    )
    return {
        "passed": passed,
        "status": audit.get("status") or "missing",
        "data_epoch_id": audit.get("data_epoch_id"),
        "expected_months": expected_months,
        "actual_months": int(audit.get("actual_months") or 0),
        "search_archives": expected_searches,
        "hash_verified_searches": count(search_checks, 1),
        "release_archives": expected_releases,
        "hash_verified_releases": count(release_checks, 1),
        "terms_hash_verified": terms_hash,
        "csv_hash_verified": csv_hash,
        "search_fallback_releases": audit.get(
            "official_search_index_fallback_releases"
        ),
        "conservative_next_day_boundaries": audit.get(
            "conservative_next_day_boundaries"
        ),
        "successor_discovery_allowed": (
            audit.get("successor_discovery_allowed") is True
        ),
        "successor_backtest_allowed": (
            audit.get("successor_backtest_allowed") is True
        ),
        "source_attribution": manifest.get("source_attribution"),
    }

def _v145_nbs_industrial_profit_physical_evidence(
    manifest_path: Path, audit_path: Path, *, project_root: Path,
) -> dict[str, object]:
    """Verify V145 searches, official pages, terms, CSV and all hashes."""
    manifest = _read_json(manifest_path)
    audit = _read_json(audit_path)
    root = project_root.resolve()

    def verified(path: object, sha256: object) -> tuple[bool, bool]:
        declared = str(sha256 or "").lower()
        if not path or len(declared) != 64:
            return False, False
        candidate = Path(str(path))
        candidate = candidate if candidate.is_absolute() else root / candidate
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
            stat = resolved.stat()
        except (OSError, ValueError):
            return False, False
        if not resolved.is_file() or stat.st_size <= 40:
            return False, False
        return True, _cached_sha256_matches(
            str(resolved), stat.st_size, stat.st_mtime_ns, declared,
        )

    searches = manifest.get("searches") or []
    releases = manifest.get("releases") or []
    search_checks = [
        verified(row.get("path"), row.get("sha256"))
        for row in searches if isinstance(row, dict)
    ]
    release_checks = [
        verified(row.get("archive_path"), row.get("sha256"))
        for row in releases if isinstance(row, dict)
    ]
    terms = manifest.get("terms") or {}
    csv_item = manifest.get("csv") or {}
    terms_exists, terms_hash = verified(terms.get("path"), terms.get("sha256"))
    csv_exists, csv_hash = verified(csv_item.get("path"), csv_item.get("sha256"))

    def count(checks: list[tuple[bool, bool]], index: int) -> int:
        return sum(int(row[index]) for row in checks)

    expected = int(audit.get("expected_observations") or 110)
    expected_searches = int(audit.get("search_archives") or 50)
    expected_releases = int(audit.get("release_archives") or 110)
    gates = audit.get("gates") or {}
    passed = bool(
        audit.get("new_data_epoch_ready") is True
        and gates and all(value is True for value in gates.values())
        and int(audit.get("actual_observations") or 0) == expected
        and len(search_checks) == expected_searches
        and count(search_checks, 0) == count(search_checks, 1) == expected_searches
        and len(release_checks) == expected_releases
        and count(release_checks, 0) == count(release_checks, 1) == expected_releases
        and terms_exists and terms_hash and csv_exists and csv_hash
    )
    return {
        "passed": passed,
        "status": audit.get("status") or "missing",
        "data_epoch_id": audit.get("data_epoch_id"),
        "expected_observations": expected,
        "actual_observations": int(audit.get("actual_observations") or 0),
        "search_archives": expected_searches,
        "hash_verified_searches": count(search_checks, 1),
        "release_archives": expected_releases,
        "hash_verified_releases": count(release_checks, 1),
        "terms_hash_verified": terms_hash,
        "csv_hash_verified": csv_hash,
        "official_interpretation_fallbacks": audit.get(
            "official_interpretation_fallbacks"
        ),
        "conservative_next_day_boundaries": audit.get(
            "conservative_next_day_boundaries"
        ),
        "successor_discovery_allowed": (
            audit.get("successor_discovery_allowed") is True
        ),
        "successor_backtest_allowed": (
            audit.get("successor_backtest_allowed") is True
        ),
        "source_attribution": manifest.get("source_attribution"),
    }

def _manual_portfolio_status(
    quote_provider, *, open_rows: list[dict] | None = None,
    closed_rows: list[dict] | None = None,
) -> dict[str, object]:
    ledger_before = read_account_ledger_facts()
    selected_open = (
        open_rows if open_rows is not None else ledger_before["open_rows"]
    )
    selected_closed = (
        closed_rows if closed_rows is not None else ledger_before["closed_rows"]
    )
    valued_open = manual_fill_fees.from_snapshot(selected_open, ledger_before)
    valued_closed = manual_fill_fees.from_snapshot(selected_closed, ledger_before)
    portfolio = _manual_portfolio_status_impl(
        quote_provider, bounded_call=_bounded_call,
        open_rows=valued_open, closed_rows=valued_closed,
    )
    ledger_after = read_account_ledger_facts()
    expected_cash = ledger_before.get("account_cash")
    cash_matches = (
        portfolio.get("cash_user_recorded") is (expected_cash is not None)
        and portfolio.get("available_cash") == (
            round(float(expected_cash["cash_balance"]), 2)
            if expected_cash is not None else None
        )
        and portfolio.get("cash_as_of") == (
            expected_cash.get("updated_at") if expected_cash else None
        )
        and portfolio.get("cash_source") == (
            expected_cash.get("source") if expected_cash else "not_recorded"
        )
    )
    selected_open_ledger_rows = [
        {key: value for key, value in row.items() if key != "sell_events"}
        for row in selected_open
    ]
    selected_closed_ledger_rows = [
        {key: value for key, value in row.items() if key != "sell_events"}
        for row in selected_closed
    ]
    selected_rows_match = (
        account_ledger_rows_sha256(selected_open_ledger_rows)
        == account_ledger_rows_sha256(ledger_before["open_rows"])
        and account_ledger_rows_sha256(selected_closed_ledger_rows)
        == account_ledger_rows_sha256(ledger_before["closed_rows"])
    )
    ledger_stable = (
        ledger_before["ledger_identity"]["combined_sha256"]
        == ledger_after["ledger_identity"]["combined_sha256"]
        and cash_matches and selected_rows_match
    )
    portfolio["ledger_as_of"] = ledger_before.get("ledger_as_of")
    portfolio["ledger_identity"] = ledger_before["ledger_identity"]
    if not ledger_stable:
        portfolio["valuation_status"] = "concurrent_change"
        portfolio["account_snapshot_concurrent_change"] = True
        portfolio["quote_health"] = {
            "status": "degraded",
            "reason": "账户账本在估值期间发生变化；本次组合快照不可用于行动",
            "fallback": "不循环重算；下一次读取重新生成完整快照",
        }
    else:
        portfolio["account_snapshot_concurrent_change"] = False
    return {**portfolio, **build_account_snapshot_identity(portfolio)}

def _public_account_facts(portfolio: dict[str, object]) -> dict[str, object]:
    recorded = portfolio.get("cash_user_recorded") is True
    valuation_complete = portfolio.get("valuation_status") == "complete"
    return {
        "capital": portfolio.get("account_capital") if recorded else None,
        "available_cash": portfolio.get("available_cash") if recorded else None,
        "equity": (
            portfolio.get("account_equity")
            if recorded and valuation_complete else None
        ),
        "cash_user_recorded": recorded,
        "source": portfolio.get("cash_source"),
        "fact_status": "recorded" if recorded else "missing_cash_input",
    }

def _current_market_regime(portfolio: dict[str, object]) -> dict[str, object]:
    """Map audited market evidence to one conservative account risk budget.

    The index overlay is a ceiling, never independent permission to buy.  A
    risk-increasing entry additionally requires an admitted strategy, which the
    current manual research specification intentionally does not provide.
    """
    strategy_id = str(
        _MANUAL_STRATEGY_REF.get("strategy_id")
        or "manual_opportunity_execution_current"
    )
    current = _portfolio_exposure_pct(portfolio)
    try:
        observed = market_regime(current_exposure_pct=current)
    except Exception as exc:
        observed = {
            "date": None, "source": "unavailable_index_close_ma20_ma60",
            "details": [], "target_exposure_pct": None, "risk_off": None,
            "reason": f"市场状态观察不可用：{type(exc).__name__}",
        }
    observed_target = _finite_number(observed.get("target_exposure_pct"))
    risk_budget_ceiling = min(
        float(MAX_TOTAL_EXPOSURE_PCT), max(0.0, observed_target or 0.0),
    )
    strategy_entry_authorized = (
        _MANUAL_STRATEGY_REF.get("production_eligible") is True
    )
    if current is None:
        target = 0.0
        remaining = None
        allowed = None
    elif strategy_entry_authorized:
        target = risk_budget_ceiling
        remaining = max(0.0, target - current)
        allowed = remaining > 0
    else:
        # Existing holdings can still be managed or reduced.  Without an
        # admitted entry strategy the effective target may not rise above the
        # current exposure, even when the old index overlay has headroom.
        target = min(risk_budget_ceiling, current)
        remaining = 0.0
        allowed = False
    budget_headroom = (
        max(0.0, risk_budget_ceiling - current)
        if current is not None else None
    )
    account_context = (
        f"当前账户暴露{current:.1f}%，风险上限剩余{budget_headroom:.1f}%；"
        f"当前可新增暴露{remaining:.1f}%。"
        if current is not None and budget_headroom is not None
        else "当前账户暴露尚不可核算。"
    )
    observed_reason = str(observed.get("reason") or "市场状态观察暂无说明")
    return {
        **observed,
        "observed_target_exposure_pct": observed.get("target_exposure_pct"),
        "observed_risk_off": observed.get("risk_off"),
        "observed_reason": observed_reason,
        "observed_source": observed.get("source"),
        "applied_to_execution": True,
        "execution_policy_applied": True,
        "execution_policy": f"{strategy_id}_market_scaled_risk_budget",
        "execution_policy_evidence_ref": _MANUAL_STRATEGY_REF.get("evidence_ref"),
        "execution_policy_sha256": _MANUAL_STRATEGY_REF.get(
            "strategy_spec_sha256"
        ),
        "target_exposure_pct": target,
        "risk_budget_ceiling_pct": risk_budget_ceiling,
        "current_exposure_pct": current,
        "remaining_exposure_pct": remaining,
        "risk_budget_headroom_pct": budget_headroom,
        "capacity_status": "complete" if current is not None else "unknown",
        "allow_new_positions": allowed,
        "strategy_entry_authorized": strategy_entry_authorized,
        "position_management_mode": (
            "entry_and_risk_management"
            if strategy_entry_authorized else "hold_or_reduce_only"
        ),
        "risk_off": risk_budget_ceiling < float(MAX_TOTAL_EXPOSURE_PCT),
        "source": f"account_risk_policy_{strategy_id}_with_market_budget",
        "reason": (
            f"{strategy_id}把已审计指数状态映射为{risk_budget_ceiling:g}%账户风险上限；"
            "该上限不是买入许可。当前没有通过扣成本、样本外及前向准入的入场策略，"
            "因此只管理或降低已有风险，不新增仓位；"
            "它只缩放仓位、不提高个股评分，也不是收益预测。"
            f"{account_context}"
            f"市场证据：{observed_reason}"
        ),
    }


def _market_adjusted_trade_capacity(
    regime: dict[str, object] | None,
    *,
    base_capacity: dict[str, object] | None = None,
) -> dict[str, object]:
    """Scale only risk-increasing buys by the existing market exposure budget."""
    capacity = dict(base_capacity or trade_capacity())
    raw_limit = capacity.get("limit")
    base_limit = (
        max(0, int(raw_limit)) if raw_limit is not None else None
    )
    used = max(0, int(capacity.get("used") or 0))
    target = _finite_number((regime or {}).get("target_exposure_pct"))
    if (regime or {}).get("allow_new_positions") is False:
        target = 0.0
    exposure_cap = float(MAX_TOTAL_EXPOSURE_PCT)
    if target is None or exposure_cap <= 0:
        effective_limit = 0
        remaining = 0
        limit_reached = True
        status = "market_target_unavailable_fail_closed"
    else:
        bounded_target = max(0.0, min(exposure_cap, target))
        if bounded_target <= 0:
            effective_limit = 0
            remaining = 0
            limit_reached = True
            status = "market_closed_to_new_risk"
        elif base_limit is None:
            effective_limit = None
            remaining = None
            limit_reached = False
            status = "market_exposure_budget_no_personal_count_quota"
        else:
            effective_limit = math.floor(
                base_limit * bounded_target / exposure_cap + 1e-12
            )
            remaining = max(0, effective_limit - used)
            limit_reached = used >= effective_limit
            status = "market_scaled"
    return {
        **capacity,
        "base_limit": base_limit,
        "limit": effective_limit,
        "remaining": remaining,
        "daily_trade_remaining": remaining,
        "limit_reached": limit_reached,
        "market_adjusted": True,
        "market_target_exposure_pct": target,
        "market_exposure_cap_pct": exposure_cap,
        "market_adjustment_status": status,
        "capacity_policy": (
            "risk_increasing_buys_market_scaled_exits_never_blocked"
        ),
        "profitability_claim": False,
    }

def _build_risk_report(quote_provider):
    """Keep the full request path bounded; concurrent data readers can also stall."""
    portfolio = _manual_portfolio_status(quote_provider)
    facts = read_account_ledger_facts()
    ledger = manual_fill_fees.from_snapshot(facts["open_rows"] + facts["closed_rows"], facts)
    explicit_initial_equity = _finite_number(
        portfolio.get("initial_equity_yuan")
    )
    has_closed_ledger = any(
        isinstance(row, dict) and row.get("status") == "closed"
        for row in ledger
    )
    if explicit_initial_equity is not None and explicit_initial_equity > 0:
        initial_capital = explicit_initial_equity
        initial_equity_status = "complete_explicit"
    elif not has_closed_ledger and portfolio.get("cash_user_recorded") is True:
        initial_capital = _finite_number(portfolio.get("account_capital")) or 0.0
        initial_equity_status = (
            "complete_pre_first_close_baseline"
            if initial_capital > 0 else "missing_account_cash"
        )
    else:
        initial_capital = 0.0
        initial_equity_status = (
            "missing_after_realized_history"
            if has_closed_ledger else "missing_account_cash"
        )
    performance_as_of = (
        portfolio.get("valuation_as_of") or portfolio.get("cash_as_of")
    )
    report = get_risk_report(
        positions=portfolio.get("positions", []),
        realized_pnl=portfolio.get("realized_pnl", 0),
        ledger=ledger,
        initial_capital=initial_capital,
        initial_equity_as_of=portfolio.get("initial_equity_as_of"),
        current_account_equity=_finite_number(portfolio.get("account_equity")),
        current_account_equity_as_of=performance_as_of,
        account_nav_snapshots=list_account_nav_snapshots(),
        account_cashflows=list_account_cashflows(),
        cashflow_reconciliation=account_cashflow_reconciliation_status(
            str(performance_as_of or "")[:10] or None,
        ),
    )
    report["initial_equity_status"] = initial_equity_status
    report["initial_equity_yuan"] = (
        initial_capital if initial_capital > 0 else None
    )
    if initial_equity_status == "missing_after_realized_history":
        report["drawdown"]["available"] = False
        report["drawdown"]["status"] = (
            "已有真实平仓记录但未录入稳定初始权益，回撤暂不可计算"
        )
        if portfolio.get("positions") and "drawdown" not in report["missing_metrics"]:
            report["missing_metrics"].append("drawdown")
            report["risk_score"] = None
            report["risk_alert_index"] = None
            report["risk_level"] = "⚪ 风险未知"
            report["risk_assessment_status"] = "insufficient"
    _augment_risk_report(report, portfolio)
    return report, portfolio

def _augment_risk_report(report: dict, portfolio: dict) -> None:
    """风控驾驶舱 5 数据块：决策门禁 / 市场 regime / 交易容量 / 单一集中度 / 止损止盈距离。

    全部复用既有调用（decision_gate 由路由追加），与站点其余页面同一真相源。
    """
    # 1) V5账户暴露策略 + MA20/MA60观察（观察值不作硬门禁）
    report["market_regime"] = _current_market_regime(portfolio)
    report["volatility_managed_exposure"] = calculate_volatility_managed_exposure(
        portfolio.get("positions", []),
        current_exposure_pct=_finite_number(
            report["market_regime"].get("current_exposure_pct")
        ),
        market_state_cap_pct=_finite_number(
            report["market_regime"].get("risk_budget_ceiling_pct")
            if report["market_regime"].get("risk_budget_ceiling_pct") is not None
            else report["market_regime"].get("target_exposure_pct")
        ),
    )
    # 2) 交易容量（手工账本当日买卖次数 / 持仓数 vs 上限）
    report["trade_capacity"] = _market_adjusted_trade_capacity(
        report["market_regime"]
    )
    # 3) 单一集中度：每名市值占比 + 最大单名占比
    positions_out = portfolio.get("positions", [])
    report["position_actions"] = position_risk_actions(
        positions_out, market_state=report["market_regime"],
    )
    total_mv = _finite_number(portfolio.get("market_value"))
    concentration = []
    for pos in positions_out:
        mv = _finite_number(pos.get("market_value"))
        concentration.append({
            "code": pos.get("code"),
            "name": pos.get("name"),
            "shares": pos.get("shares"),
            "market_value": round(mv, 2) if mv is not None else None,
            "value_pct": (
                round(mv / total_mv * 100, 2)
                if mv is not None and total_mv is not None and total_mv > 0 else None
            ),
        })
    concentration.sort(
        key=lambda item: item["value_pct"] if item["value_pct"] is not None else -1,
        reverse=True,
    )
    known_pcts = [
        item["value_pct"] for item in concentration if item["value_pct"] is not None
    ]
    max_single_pct = max(known_pcts) if known_pcts else None
    report["concentration"] = {
        "positions": concentration,
        "max_single_pct": round(max_single_pct, 2) if max_single_pct is not None else None,
        "valuation_status": portfolio.get("valuation_status", "insufficient"),
        "max_single_pct_threshold": MAX_SINGLE_POSITION_PCT,
    }
    # 4) 止损止盈距离：每持仓 compute_levels（基于现价），现价距各线%
    stop_levels = []
    for pos in positions_out:
        cp = _finite_number(pos.get("current_price"))
        if cp is None or cp <= 0:
            continue
        lv = compute_levels(cp, code=pos.get("code") or "", prev_close=None)
        if not lv:
            continue
        stop_levels.append({
            "code": pos.get("code"),
            "name": pos.get("name"),
            "current_price": round(cp, 2),
            "stop_loss": lv["stop_loss"],
            "stop_loss_pct": round((lv["stop_loss"] / cp - 1) * 100, 2),
            "take_profit_1": lv["take_profit_1"],
            "take_profit_1_pct": round((lv["take_profit_1"] / cp - 1) * 100, 2),
            "take_profit_2": lv["take_profit_2"],
            "take_profit_2_pct": round((lv["take_profit_2"] / cp - 1) * 100, 2),
            "trailing_activation": lv["trailing_activation"],
            "note": lv.get("note", ""),
        })
    report["stop_levels"] = stop_levels
    report["daily_history_evidence"] = daily_history_evidence([
        str(position.get("code") or "") for position in positions_out
    ])

def _quote_chain_valid_fields(
    price: object, previous_close: object, change_pct: object,
) -> bool:
    """Validate the three coupled quote facts before any score or price level."""
    current = _finite_number(price)
    previous = _finite_number(previous_close)
    change = _finite_number(change_pct)
    if current is None or current <= 0 or previous is None or previous <= 0 or change is None:
        return False
    implied_change = (current / previous - 1.0) * 100
    return abs(implied_change - change) <= 0.5

def _parse_iso_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=_SH_TZ) if parsed.tzinfo is None else parsed.astimezone(_SH_TZ)

def _last_trading_close(current: datetime) -> datetime:
    """最近一个交易日的 15:00 收盘时点（上海时区）。

    无法定节假日日历，工作日/周末按"最接近的周一至周五"推算：
    - 工作日 15:00 及之后 → 今天 15:00
    - 工作日 15:00 前（盘前）→ 前一工作日 15:00
    - 周末 → 最近一个工作日（周五）15:00
    """
    if trading_day_status(current)["is_trading_day"] and current.time() >= time(15, 0):
        base = current.date()
    else:
        base = current.date() - timedelta(days=1)
        while not trading_day_status(base)["is_trading_day"]:
            base = base - timedelta(days=1)
    return datetime.combine(base, time(15, 0), tzinfo=_SH_TZ)

def _is_trading_day(current: datetime | None = None) -> bool:
    return bool(trading_day_status(current or datetime.now(_SH_TZ))["is_trading_day"])

def _is_market_open(now: datetime | None = None) -> bool:
    """连续竞价时段：9:30-11:30 / 13:00-15:00（北京时间）。"""
    t = now or datetime.now(_SH_TZ)
    if not _is_trading_day(t):
        return False
    minute = t.hour * 60 + t.minute
    return 570 <= minute < 690 or 780 <= minute < 900


def _opportunity_refresh_wait_seconds(
    *, observed_at: datetime, now_epoch: float, last_completed_at: object,
    worker_running: bool, policy_wait_seconds: object, trading_day: bool,
) -> float:
    """Wake on session/close boundaries and after the last completed snapshot."""
    wait_seconds = max(0.05, float(policy_wait_seconds))
    current = observed_at.astimezone(_SH_TZ)
    if trading_day:
        for boundary_time in (time(9, 30), time(13, 0), time(15, 0)):
            boundary = datetime.combine(
                current.date(), boundary_time, tzinfo=_SH_TZ,
            )
            seconds_to_boundary = (boundary - current).total_seconds()
            if seconds_to_boundary > 0:
                wait_seconds = min(wait_seconds, seconds_to_boundary)
                break
    completed = _finite_number(last_completed_at)
    if not worker_running and completed is not None and completed > 0:
        seconds_to_refresh = completed + _OPP_REFRESH_S - float(now_epoch)
        if seconds_to_refresh > 0:
            wait_seconds = min(wait_seconds, seconds_to_refresh)
    return max(0.05, wait_seconds)


def _snapshot_current_outside_open_session(
    snapshot: object, *, session_open: bool | None = None,
) -> bool:
    """Keep a newly computed closed-session snapshot current past its TTL.

    This is service-snapshot freshness only.  Input/model freshness remains in
    each payload's own ``as_of`` fields and is never upgraded here.
    """
    open_now = _is_market_open() if session_open is None else bool(session_open)
    if not isinstance(snapshot, dict) or open_now:
        return False
    pipeline = snapshot.get("pipeline_identity") or {}
    as_of = (
        pipeline.get("snapshot_as_of")
        if isinstance(pipeline, dict) else None
    ) or snapshot.get("date")
    if not as_of:
        return False
    # Weekend/holiday recomputation is still a current closed-market view.
    # ``audited_trading_day_age`` correctly treats that timestamp as a
    # non-trading date, so first recognise an actual same-calendar-day build.
    if str(as_of)[:10] == datetime.now(_SH_TZ).date().isoformat():
        return True
    return audited_trading_day_age(
        as_of, allow_current_session=True,
    ).get("status") == "fresh"

def _audited_market_session(now: datetime | None = None) -> dict[str, object]:
    """Return an A-share session only when the audited calendar covers today."""
    current = now or datetime.now(_SH_TZ)
    current = (
        current.replace(tzinfo=_SH_TZ)
        if current.tzinfo is None else current.astimezone(_SH_TZ)
    )
    calendar = trading_day_status(current)
    day = current.date().isoformat()
    covered = bool(
        calendar.get("source") == "audited_baostock_calendar"
        and str(calendar.get("coverage_start") or "") <= day
        <= str(calendar.get("coverage_end") or "")
    )
    if not covered:
        return {
            "status": "unknown",
            "label": "交易状态未知",
            "is_trading_day": None,
            "is_open": None,
            "reason": calendar.get("reason") or "审计交易日历未覆盖当前日期",
            "server_time": current.isoformat(),
        }
    if calendar.get("is_trading_day") is not True:
        return {
            "status": "closed",
            "label": "休市",
            "is_trading_day": False,
            "is_open": False,
            "reason": calendar.get("reason") or "非交易日",
            "server_time": current.isoformat(),
        }
    minute = current.hour * 60 + current.minute
    if 570 <= minute < 690 or 780 <= minute < 897:
        status, label, is_open = "open", "交易中", True
    elif 897 <= minute < 900:
        status, label, is_open = "auction", "收盘集合竞价", False
    elif 690 <= minute < 780:
        status, label, is_open = "break", "午间休市", False
    elif minute < 555:
        status, label, is_open = "preopen", "未开盘", False
    elif minute < 570:
        status, label, is_open = "auction", "集合竞价", False
    else:
        status, label, is_open = "closed", "已收盘", False
    return {
        "status": status,
        "label": label,
        "is_trading_day": True,
        "is_open": is_open,
        "reason": "审计交易日历与上海竞价交易时段",
        "server_time": current.isoformat(),
    }


def _timing_review_day(now: datetime, scope: str = "current"):
    """Choose the ledger day without erasing the last review at midnight."""
    current = (
        now.replace(tzinfo=_SH_TZ)
        if now.tzinfo is None else now.astimezone(_SH_TZ)
    )
    if scope != "last_review":
        return current.date()
    calendar = trading_day_status(current)
    if (
        calendar.get("is_trading_day") is True
        and current.timetz().replace(tzinfo=None) >= time(15, 0)
    ):
        return current.date()
    return last_completed_trading_day(current) or current.date()


def _audited_market_state(now: datetime | None = None) -> dict[str, object]:
    """Legacy-shaped market metadata derived only from the audited session."""
    session = _audited_market_session(now)
    phase = {
        "break": "lunch",
        "preopen": "preopen",
    }.get(str(session.get("status")), str(session.get("status") or "unknown"))
    return {
        "phase": phase,
        "label": session.get("label") or "交易状态未知",
        "is_trading_day": session.get("is_trading_day"),
        "is_open": session.get("is_open"),
        "reason": session.get("reason"),
        "server_time": session.get("server_time"),
    }

def _fund_flow_freshness(source_time: object, now: datetime | None = None) -> dict:
    """Describe snapshot age without claiming a static file is live market data."""
    if not isinstance(source_time, str):
        return {"state": "unknown", "label": "来源时间缺失", "age_seconds": None}
    try:
        source_dt = datetime.fromisoformat(source_time.replace("Z", "+00:00"))
    except ValueError:
        return {"state": "unknown", "label": "来源时间无法解析", "age_seconds": None}

    if source_dt.tzinfo is None:
        source_dt = source_dt.replace(tzinfo=_SH_TZ)
    source_dt = source_dt.astimezone(_SH_TZ)
    current = now.astimezone(_SH_TZ) if now and now.tzinfo else (now or datetime.now(_SH_TZ))
    trading_age = audited_trading_day_age(
        source_dt, current, allow_current_session=True,
    )
    age_seconds = int((current - source_dt).total_seconds())
    if age_seconds < -60:
        return {"state": "invalid", "label": "来源时间晚于当前时间", "age_seconds": age_seconds, "trading_day_age": trading_age}
    age_seconds = max(0, age_seconds)

    # A close-to-noon snapshot remains a truthful closing snapshot during the lunch break.
    if (
        current.date() == source_dt.date()
        and time(11, 30) <= current.time() < time(13, 0)
        and time(11, 15) <= source_dt.time() <= time(11, 30)
    ):
        return {"state": "market_break", "label": "午间停盘快照", "age_seconds": age_seconds, "trading_day_age": trading_age}

    # 休市感知（修复"周六早上把周五收盘快照标成已过时"的误报）：
    # 非交易时段（周末/盘前/盘后，午休由上面分支单独处理）内，最近一个交易日
    # 收盘时点的快照就是"应得的最新快照"——墙钟走了多久不算过时；只有比最近
    # 收盘更旧（刷新器漏跑了一个交易日）才是真的 stale。
    session = _audited_market_session(current)
    closed_window = session.get("is_open") is False
    if closed_window and source_dt >= _last_trading_close(current):
        return {"state": "market_closed", "label": "休市·收盘快照", "age_seconds": age_seconds, "trading_day_age": trading_age}
    if age_seconds <= 180:
        return {"state": "fresh", "label": "最近更新", "age_seconds": age_seconds, "trading_day_age": trading_age}
    if age_seconds <= 900:
        return {"state": "delayed", "label": "更新延迟", "age_seconds": age_seconds, "trading_day_age": trading_age}
    return {"state": "stale", "label": "快照已过时", "age_seconds": age_seconds, "trading_day_age": trading_age}

def _live_signal_refresh_decision(
    *,
    trading_day: bool,
    market_open: bool,
    seconds_since_run: float,
    closing_refresh_attempted: bool,
    persisted_freshness_state: str,
) -> tuple[bool, bool]:
    """Return ``(due, closing_refresh)`` without closed-hours polling."""
    if trading_day and market_open:
        return seconds_since_run >= 60, False
    if closing_refresh_attempted:
        return False, False
    if persisted_freshness_state in {"market_closed", "market_break"}:
        return False, False
    return True, True

def _load_fund_flow_snapshot(path: Path, now: datetime | None = None) -> tuple[list[dict], dict]:
    """Load one fund-flow snapshot and isolate invalid records before presentation."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_stocks = payload.get("stocks")
    if not isinstance(raw_stocks, dict):
        raise ValueError("资金流快照缺少 stocks 字典")

    quality = {
        "raw_rows": len(raw_stocks),
        "declared_rows": payload.get("n") if isinstance(payload.get("n"), int) else None,
        "served_rows": 0,
        "rejected_rows": 0,
        "invalid_code_rows": 0,
        "invalid_numeric_rows": 0,
        "invalid_turnover_rows": 0,
        "flow_identity_mismatch_rows": 0,
        "invalid_pct_chg_rows": 0,
    }
    records: list[dict] = []
    for code, raw in raw_stocks.items():
        if not isinstance(code, str) or not CODE_PATTERN.fullmatch(code):
            quality["invalid_code_rows"] += 1
            quality["rejected_rows"] += 1
            continue
        if not isinstance(raw, dict):
            quality["invalid_numeric_rows"] += 1
            quality["rejected_rows"] += 1
            continue

        values = {field: _finite_number(raw.get(field)) for field in (
            "net_yi", "inflow_yi", "outflow_yi", "turnover", "pct_chg",
        )}
        if any(value is None for value in values.values()):
            quality["invalid_numeric_rows"] += 1
            quality["rejected_rows"] += 1
            continue
        raw_net = values["net_yi"]
        raw_inflow = values["inflow_yi"]
        raw_outflow = values["outflow_yi"]
        turnover, pct_chg = values["turnover"], values["pct_chg"]
        if raw_inflow < 0 or raw_outflow < 0 or not 0 <= turnover <= 100:
            quality["invalid_turnover_rows"] += 1
            quality["rejected_rows"] += 1
            continue
        if abs(raw_net - (raw_inflow - raw_outflow)) > _FUNDFLOW_BALANCE_TOLERANCE_YI:
            quality["flow_identity_mismatch_rows"] += 1
            quality["rejected_rows"] += 1
            continue
        net = normalize_fund_flow_yi(raw_net)
        inflow = normalize_fund_flow_yi(raw_inflow)
        outflow = normalize_fund_flow_yi(raw_outflow)
        if net is None or inflow is None or outflow is None:
            quality["invalid_numeric_rows"] += 1
            quality["rejected_rows"] += 1
            continue

        # Flow numbers can remain useful when only the percent-change cell is corrupted;
        # the malformed percent is withheld from ranking and breadth rather than replaced by 0.
        safe_pct_chg = pct_chg
        if abs(pct_chg) > _FUNDFLOW_PCT_ABS_LIMIT:
            safe_pct_chg = None
            quality["invalid_pct_chg_rows"] += 1
        records.append({
            "code": code,
            "raw_net_yi": round(raw_net, 2),
            "raw_inflow_yi": round(raw_inflow, 1),
            "raw_outflow_yi": round(raw_outflow, 1),
            "net_yi": round(net, 2),
            "inflow_yi": round(inflow, 1),
            "outflow_yi": round(outflow, 1),
            "magnitude_scale": FLOW_MAGNITUDE_SCALE,
            "flow_unit": "亿元（统一折算）",
            "turnover": round(turnover, 1),
            "pct_chg": round(safe_pct_chg, 1) if safe_pct_chg is not None else None,
        })

    quality["served_rows"] = len(records)
    source_time = payload.get("ts") if isinstance(payload.get("ts"), str) else None
    quality.update({
        "source_name": payload.get("source_name") or "本机资金流快照",
        "source_disclosure": payload.get("source_url") or "原始文件未记录数据供应商",
        "source_time": source_time,
        "source_date": payload.get("date") if isinstance(payload.get("date"), str) else None,
        "source_file_mtime": datetime.fromtimestamp(path.stat().st_mtime, _SH_TZ).isoformat(),
        "freshness": _fund_flow_freshness(source_time, now),
        "magnitude_scale": FLOW_MAGNITUDE_SCALE,
        "flow_unit": "亿元（统一折算）",
    })
    return records, quality

def _fund_flow_breadth(records: list[dict]) -> dict:
    """全市场涨跌家数——全站唯一口径：±0.01% 分上/平/下，平盘计入分母。

    所有展示"涨跌家数/广度"的接口都必须走这里，避免各处重复实现漂移
    （曾出现 /api/market/breadth 与 /api/market/status 同日差 ~40 只）。
    """
    up = down = flat = 0
    for data in records:
        chg = data.get("pct_chg")
        if chg is None:
            continue
        if chg > 0.01:
            up += 1
        elif chg < -0.01:
            down += 1
        else:
            flat += 1
    total = up + down + flat
    return {
        "up": up, "down": down, "flat": flat, "total": total,
        "up_ratio": round(up / max(total, 1) * 100, 1),
        "flat_in_denominator": True,
    }

def _fund_flow_market_summary(records: list[dict]) -> dict:
    """Market-wide cash-flow summary, deliberately independent of the page limit/filter."""
    positive = [record for record in records if record["net_yi"] > 0.1]
    negative = [record for record in records if record["net_yi"] < -0.1]
    return {
        "positive_net_count": len(positive),
        "negative_net_count": len(negative),
        "net_total_yi": round(sum(record["net_yi"] for record in records), 2),
        "top_in": max(records, key=lambda record: record["net_yi"], default=None),
        "top_out": min(records, key=lambda record: record["net_yi"], default=None),
    }

def _base_strategy_lab_evidence() -> dict[str, object]:
    """Thin adapter preserving app-level project/helper monkeypatch behavior."""
    return build_strategy_lab_base(
        _PROJECT,
        read_json=_read_json,
        latest_paper_forward_report=_latest_paper_forward_report,
        sha256_matches=_cached_sha256_matches,
    )

def _base_system_evidence(
    decision_gate: dict[str, object] | None = None,
) -> dict[str, object]:
    """Keep app-level monkeypatches effective across the module boundary."""
    return build_system_evidence(
        _PROJECT,
        decision_gate,
        sh_tz=_SH_TZ,
        decision_gate_status=_decision_gate_status,
        latest_paper_forward_report=_latest_paper_forward_report,
        manifest_physical_evidence=_manifest_physical_evidence,
        read_json=_read_json,
        v90_pmi_physical_evidence=_v90_pmi_physical_evidence,
        v120_pbc_physical_evidence=_v120_pbc_physical_evidence,
        v143_nbs_cpi_ppi_physical_evidence=_v143_nbs_cpi_ppi_physical_evidence,
        v145_nbs_industrial_profit_physical_evidence=(
            _v145_nbs_industrial_profit_physical_evidence
        ),
        index_data_evidence=index_data_evidence,
        trading_day_status=trading_day_status,
    )

def _base_decision_gate_status() -> dict[str, object]:
    return build_base_decision_gate_status({
        "_PAPER_FORWARD_STATUS": _PAPER_FORWARD_STATUS,
        "_STRATEGY_REGISTRY": _STRATEGY_REGISTRY,
        "_V86_SELECTION_BENCHMARK_GATE": _V86_SELECTION_BENCHMARK_GATE,
        "_V86_MULTI_BENCHMARK_GATE": _V86_MULTI_BENCHMARK_GATE,
        "_V86_CONTRIBUTOR_CONCENTRATION_GATE": _V86_CONTRIBUTOR_CONCENTRATION_GATE,
        "_V86_MODEL_TRAINING_INTEGRITY_GATE": _V86_MODEL_TRAINING_INTEGRITY_GATE,
        "_V87_SUCCESSOR_ADMISSION_GATE": _V87_SUCCESSOR_ADMISSION_GATE,
        "_V87_CSRC_MAPPING_FAILURE": _V87_CSRC_MAPPING_FAILURE,
        "_V88_CSRC_MAPPING_AUDIT": _V88_CSRC_MAPPING_AUDIT,
        "_V89_SSE_DATA_EPOCH_AUDIT": _V89_SSE_DATA_EPOCH_AUDIT,
        "_read_json": _read_json,
        "_V90_SUCCESSOR_STATUS": _V90_SUCCESSOR_STATUS,
        "_V120_SUCCESSOR_STATUS": _V120_SUCCESSOR_STATUS,
        "_V143_SUCCESSOR_STATUS": _V143_SUCCESSOR_STATUS,
        "_V145_SUCCESSOR_STATUS": _V145_SUCCESSOR_STATUS,
        "_PROJECT": _PROJECT,
        "_latest_paper_forward_report": _latest_paper_forward_report,
    })

def _base_historical_candidate_headline(
    gate: dict[str, object],
) -> dict[str, object] | None:
    return build_base_historical_candidate_headline({
        "_read_json": _read_json,
        "_PROJECT": _PROJECT,
        "_latest_paper_forward_report": _latest_paper_forward_report,
    }, gate)

def _base_profitability_research_evidence() -> dict[str, object]:
    return build_base_profitability_research_evidence({
        "_read_json": _read_json,
        "_PROJECT": _PROJECT,
        "_latest_paper_forward_report": _latest_paper_forward_report,
        "_TASKBOOK_V3_GATE": _TASKBOOK_V3_GATE,
        "_V86_FORWARD_LATEST": _V86_FORWARD_LATEST,
        "_V86_FORWARD_EVALUATION": _V86_FORWARD_EVALUATION,
        "_V86_FORWARD_SCHEDULE": _V86_FORWARD_SCHEDULE,
        "_V86_FORWARD_PROMOTION": _V86_FORWARD_PROMOTION,
        "_V86_EXTERNAL_REPRODUCTION": _V86_EXTERNAL_REPRODUCTION,
        "_V86_FINAL_CANDIDATE_GATE": _V86_FINAL_CANDIDATE_GATE,
        "_V3_PROFIT_SOURCE_AUDIT": _V3_PROFIT_SOURCE_AUDIT,
        "_V86_REGIME_ROBUSTNESS_GATE": _V86_REGIME_ROBUSTNESS_GATE,
        "_V86_ROLLING_OOS_GATE": _V86_ROLLING_OOS_GATE,
        "_V86_COST_CAPACITY_GATE": _V86_COST_CAPACITY_GATE,
        "_V86_CORPORATE_ACTION_GATE": _V86_CORPORATE_ACTION_GATE,
        "_V86_TEMPORAL_DEPENDENCE_GATE": _V86_TEMPORAL_DEPENDENCE_GATE,
        "_V46_TEMPORAL_DEPENDENCE_AUDIT": _V46_TEMPORAL_DEPENDENCE_AUDIT,
        "_V86_SELECTION_BENCHMARK_GATE": _V86_SELECTION_BENCHMARK_GATE,
        "_V46_SELECTION_BENCHMARK_AUDIT": _V46_SELECTION_BENCHMARK_AUDIT,
        "_V86_MULTI_BENCHMARK_GATE": _V86_MULTI_BENCHMARK_GATE,
        "_V46_MULTI_BENCHMARK_AUDIT": _V46_MULTI_BENCHMARK_AUDIT,
        "_V86_CONTRIBUTOR_CONCENTRATION_GATE": _V86_CONTRIBUTOR_CONCENTRATION_GATE,
        "_V46_CONTRIBUTOR_CONCENTRATION_AUDIT": _V46_CONTRIBUTOR_CONCENTRATION_AUDIT,
        "_V86_MODEL_TRAINING_INTEGRITY_GATE": _V86_MODEL_TRAINING_INTEGRITY_GATE,
        "_V46_TRAINING_BOUNDARY_PURGE_AUDIT": _V46_TRAINING_BOUNDARY_PURGE_AUDIT,
        "_V87_SUCCESSOR_ADMISSION_GATE": _V87_SUCCESSOR_ADMISSION_GATE,
        "_V87_CSRC_MAPPING_FAILURE": _V87_CSRC_MAPPING_FAILURE,
        "_V88_CSRC_MAPPING_AUDIT": _V88_CSRC_MAPPING_AUDIT,
        "_V89_SSE_DATA_EPOCH_AUDIT": _V89_SSE_DATA_EPOCH_AUDIT,
        "_DEEPSEEK_DISCOVERY_ROUND": _DEEPSEEK_DISCOVERY_ROUND,
        "_DEEPSEEK_DISCOVERY_ASSESSMENT": _DEEPSEEK_DISCOVERY_ASSESSMENT,
        "_V90_SUCCESSOR_STATUS": _V90_SUCCESSOR_STATUS,
        "_V120_SUCCESSOR_STATUS": _V120_SUCCESSOR_STATUS,
        "_V143_SUCCESSOR_STATUS": _V143_SUCCESSOR_STATUS,
        "_V145_SUCCESSOR_STATUS": _V145_SUCCESSOR_STATUS,
    })

_BASE_STRATEGY_LAB_EVIDENCE = _base_strategy_lab_evidence
_BASE_SYSTEM_EVIDENCE = _base_system_evidence
_BASE_DECISION_GATE_STATUS = _base_decision_gate_status
_BASE_HISTORICAL_CANDIDATE_HEADLINE = _base_historical_candidate_headline
_BASE_PROFITABILITY_RESEARCH_EVIDENCE = _base_profitability_research_evidence
def _stability_80_20_reports() -> tuple[dict[str, object], dict[str, object]]:
    return _cash_floor_decision._stability_80_20_reports(_cash_floor_context())
def _latest_stability_paper_forward_report() -> dict[str, object]:
    return _cash_floor_decision._latest_stability_paper_forward_report(
        _cash_floor_context()
    )
def _stability_execution_reality_report() -> dict[str, object]:
    return _cash_floor_decision._stability_execution_reality_report(
        _cash_floor_context()
    )
def _risk_sleeve_incremental_attribution_report() -> dict[str, object]:
    return _cash_floor_decision._risk_sleeve_incremental_attribution_report(
        _cash_floor_context()
    )
def _rolling_risk_cap_walk_forward_report() -> dict[str, object]:
    return _cash_floor_decision._rolling_risk_cap_walk_forward_report(
        _cash_floor_context()
    )

_V233_FORWARD_PATH = _PROJECT / "reports" / "v233_cash_floor_forward.json"
_V233_FORWARD_PROTOCOL_PATH = (
    _PROJECT / "research"
    / "V233_CASH_FLOOR_FORWARD_EVALUATION_PROTOCOL_V1.json"
)
_V233_FORWARD_LOCK = threading.Lock()
_V233_FORWARD_CACHE: dict[str, object] = {"loaded_at": 0.0, "payload": {}}
_CASH_COMPARISON_LOCK = threading.Lock()
_CASH_COMPARISON_CACHE: dict[str, object] = {"loaded_at": 0.0, "payload": {}}
_DUAL_CASH_FORWARD_PATH = _PROJECT / "reports" / "v241_dual_money_fund_forward.json"
_V239_511990_INCOME_PATH = (
    _PROJECT / "research" / "data" / "v239_cash_floor_cross_product"
    / "511990_income.csv"
)
_DUAL_CASH_FORWARD_LOCK = threading.Lock()
_DUAL_CASH_FORWARD_CACHE: dict[str, object] = {"loaded_at": 0.0, "payload": {}}
def _cash_floor_context() -> dict[str, object]:
    """Resolve app dependencies at call time so monkeypatches remain effective."""
    return {
        "_PROJECT": _PROJECT,
        "_read_json": _read_json,
        "_V233_FORWARD_PROTOCOL_PATH": _V233_FORWARD_PROTOCOL_PATH,
        "_finite_number": _finite_number,
        "_SH_TZ": _SH_TZ,
        "_V233_FORWARD_CACHE": _V233_FORWARD_CACHE,
        "_V233_FORWARD_LOCK": _V233_FORWARD_LOCK,
        "_V233_FORWARD_PATH": _V233_FORWARD_PATH,
        "_bounded_call": _bounded_call,
        "trading_day_status": trading_day_status,
        "_CASH_COMPARISON_CACHE": _CASH_COMPARISON_CACHE,
        "_CASH_COMPARISON_LOCK": _CASH_COMPARISON_LOCK,
        "_audited_market_state": _audited_market_state,
        "_V239_511990_INCOME_PATH": _V239_511990_INCOME_PATH,
        "_DUAL_CASH_FORWARD_CACHE": _DUAL_CASH_FORWARD_CACHE,
        "_DUAL_CASH_FORWARD_LOCK": _DUAL_CASH_FORWARD_LOCK,
        "_DUAL_CASH_FORWARD_PATH": _DUAL_CASH_FORWARD_PATH,
        "_cached_sha256_matches": _cached_sha256_matches,
        "_BASE_STRATEGY_LAB_EVIDENCE": _BASE_STRATEGY_LAB_EVIDENCE,
        "_active_strategy_public_view": _active_strategy_public_view,
        "_safe_live_signal_snapshot": _safe_live_signal_snapshot,
        "_BASE_DECISION_GATE_STATUS": _BASE_DECISION_GATE_STATUS,
        "_stability_80_20_reports": _stability_80_20_reports,
        "_latest_stability_paper_forward_report": _latest_stability_paper_forward_report,
        "_stability_execution_reality_report": _stability_execution_reality_report,
        "_risk_sleeve_incremental_attribution_report": _risk_sleeve_incremental_attribution_report,
        "_rolling_risk_cap_walk_forward_report": _rolling_risk_cap_walk_forward_report,
        "_v233_forward_evaluation": _v233_forward_evaluation,
        "_v233_cash_floor_forward_status": _v233_cash_floor_forward_status,
        "_cash_floor_daily_recovery_report": _cash_floor_daily_recovery_report,
        "_cash_floor_cross_product_report": _cash_floor_cross_product_report,
        "_cash_floor_live_comparison": _cash_floor_live_comparison,
        "_511990_declared_income_ledger": _511990_declared_income_ledger,
        "_dual_cash_floor_forward_status": _dual_cash_floor_forward_status,
        "_dual_cash_floor_report": _dual_cash_floor_report,
        "_cash_floor_daily_path_metrics_report": _cash_floor_daily_path_metrics_report,
        "_cash_floor_holding_horizon_report": _cash_floor_holding_horizon_report,
        "_stability_80_20_passed": _stability_80_20_passed,
        "_stability_80_20_allocation": _stability_80_20_allocation,
        "_report_integrity_rows": _report_integrity_rows,
    }
def _v233_forward_evaluation(
    observations: list[dict[str, object]],
) -> dict[str, object]:
    return _cash_floor_decision._v233_forward_evaluation(
        _cash_floor_context(), observations
    )
def _v233_cash_floor_forward_status(
    cash_floor: dict[str, object],
) -> dict[str, object]:
    return _cash_floor_decision._v233_cash_floor_forward_status(
        _cash_floor_context(), cash_floor
    )
def _cash_floor_daily_recovery_report() -> dict[str, object]:
    return _cash_floor_decision._cash_floor_daily_recovery_report(
        _cash_floor_context()
    )
def _cash_floor_cross_product_report(
    *, include_live: bool = False,
) -> dict[str, object]:
    return _cash_floor_decision._cash_floor_cross_product_report(
        _cash_floor_context(), include_live=include_live
    )
def _cash_floor_live_comparison(
    cross_product: dict[str, object],
) -> dict[str, object]:
    return _cash_floor_decision._cash_floor_live_comparison(
        _cash_floor_context(), cross_product
    )
def _511990_declared_income_ledger(
    paper_entry: dict[str, object],
    exit_date: str,
    income_path: Path | None = None,
) -> dict[str, object]:
    return _cash_floor_decision._511990_declared_income_ledger(
        _cash_floor_context(), paper_entry, exit_date, income_path
    )
def _dual_cash_floor_forward_status(
    dual_report: dict[str, object],
) -> dict[str, object]:
    return _cash_floor_decision._dual_cash_floor_forward_status(
        _cash_floor_context(), dual_report
    )
def _dual_cash_floor_report() -> dict[str, object]:
    return _cash_floor_decision._dual_cash_floor_report(_cash_floor_context())

def _cash_floor_daily_path_metrics_report() -> dict[str, object]:
    return _cash_floor_decision._cash_floor_daily_path_metrics_report(
        _cash_floor_context()
    )


def _cash_floor_holding_horizon_report() -> dict[str, object]:
    return _cash_floor_decision._cash_floor_holding_horizon_report(
        _cash_floor_context()
    )

def _stability_80_20_passed(
    candidate: dict[str, object], reproduction: dict[str, object],
) -> bool:
    return _cash_floor_decision._stability_80_20_passed(
        _cash_floor_context(), candidate, reproduction
    )


def _stability_80_20_allocation(
    candidate: dict[str, object], reproduction: dict[str, object],
) -> dict[str, object]:
    return _cash_floor_decision._stability_80_20_allocation(
        _cash_floor_context(), candidate, reproduction
    )


def _stability_metric(
    report: dict[str, object], metric: str,
) -> dict[str, object]:
    return _cash_floor_decision._stability_metric(
        _cash_floor_context(), report, metric
    )


def _report_integrity_rows(
    report: dict[str, object], label: str,
) -> list[dict[str, object]]:
    return _cash_floor_decision._report_integrity_rows(
        _cash_floor_context(), report, label
    )


def _strategy_lab_evidence_inner() -> dict[str, object]:
    return _cash_floor_decision._strategy_lab_evidence_inner(
        _cash_floor_context()
    )

# 2026-08-13 延迟整改：/api/dashboard 每 60s 轮询都重建策略实验证据（读 ~8 个稳定性报告
# JSON + 输入/输出哈希校验），是 /api/dashboard 冷负载的固定部分。稳定性报告是分钟~小时级
# 产物、自带 generated_at 时间戳展示，120s 缓存是诚实新鲜度。
_LAB_EVIDENCE_CACHE: dict[str, object] = {"ts": 0.0, "data": None, "project": None}
_LAB_EVIDENCE_CACHE_LOCK = threading.Lock()
_LAB_EVIDENCE_CACHE_TTL = 120

def _strategy_lab_evidence() -> dict[str, object]:
    now = _time.time()
    project = str(_PROJECT)
    with _LAB_EVIDENCE_CACHE_LOCK:
        if (
            _LAB_EVIDENCE_CACHE["data"] is not None
            and _LAB_EVIDENCE_CACHE["project"] == project
            and (now - _LAB_EVIDENCE_CACHE["ts"]) < _LAB_EVIDENCE_CACHE_TTL
        ):
            # 深拷贝：调用方可能改动返回结构，缓存必须不可变
            return json.loads(json.dumps(_LAB_EVIDENCE_CACHE["data"]))
    payload = _strategy_lab_evidence_inner()
    with _LAB_EVIDENCE_CACHE_LOCK:
        _LAB_EVIDENCE_CACHE["data"] = json.loads(json.dumps(payload))
        _LAB_EVIDENCE_CACHE["ts"] = now
        _LAB_EVIDENCE_CACHE["project"] = project
    return payload

def _decision_gate_status_full() -> dict[str, object]:
    return _cash_floor_decision._decision_gate_status_full(
        _cash_floor_context()
    )

# ── 决策门禁：有界 + TTL 缓存，绝不阻塞请求线程 ────────────────────────────────
# _decision_gate_status_full（V233/V241 纸面前向）会拉腾讯实时行情（511880/511990）。
# 数据源健康时单次 ~1s；源不可达时单次 15s 超时、两处调用合计 30s。2026-08-18 实测
# 腾讯 qt.gtimg.cn 超时 -> /api/kronos/top 阻塞 25-37s（看涨池卡片空白）、/api/dashboard 10s。
# 修复：TTL 缓存 = 每窗口至多一次全量评估，其余调用命中缓存即回；冷缺失用 _bounded_call
# 限时评估、超时/异常回退到纯 JSON 文件基础闸门（fail-closed）；已在刷新时并发调用者
# 直接拿基础闸门（不重复触发网络）。
_DECISION_GATE_CACHE: dict[str, object] = {"loaded_at": 0.0, "payload": None}
_DECISION_GATE_REFRESHING = False
# 进行中刷新的 manual 键（None=无）：等待者只等与本次 OLYMPUS_MANUAL_REAL_MONEY_MODE
# 一致的刷新；键不一致的结果必然不同，等待方应自行重算而非空等拿错键旧值。
_DECISION_GATE_REFRESH_MANUAL: bool | None = None
_DECISION_GATE_LOCK = threading.Lock()
_DECISION_GATE_TTL = 30.0
# 全量评估内两处行情已各自限时 3s，最坏 ~6s；预算 8s 保证降级后仍能拿全语义门禁。
_DECISION_GATE_BUDGET = 8.0

def _decision_gate_status() -> dict[str, object]:
    """Fail-closed 决策门禁（同步）：有界求值 + TTL 缓存。

    供测试与内部逻辑直接调用，返回全语义门禁（含 V233/V241 稳定证据）；行情源不可达时
    以 quote_error 降级、不阻塞 15s。冷缺失有界求值（≤8s），TTL 窗口内其余调用命中缓存。
    缓存按 OLYMPUS_MANUAL_REAL_MONEY_MODE 分键：该开关翻转立即重算（测试翻转环境变量）。
    """
    global _DECISION_GATE_REFRESHING, _DECISION_GATE_REFRESH_MANUAL
    manual = os.environ.get("OLYMPUS_MANUAL_REAL_MONEY_MODE") == "1"
    now = _time.monotonic()
    with _DECISION_GATE_LOCK:
        cached = _DECISION_GATE_CACHE["payload"]
        if (cached is not None
                and _DECISION_GATE_CACHE.get("manual") == manual
                and now - float(_DECISION_GATE_CACHE["loaded_at"]) < _DECISION_GATE_TTL):
            return copy.deepcopy(cached)
        # 只等待与本次 manual 键一致的进行中刷新；键不同（如先前环境触发的后台刷新）
        # 结果必然不同——本线程应自行成为刷新者，而不是空等 8s 后拿到错键旧值。
        refreshing = (
            _DECISION_GATE_REFRESHING and _DECISION_GATE_REFRESH_MANUAL == manual)
        if not refreshing:
            _DECISION_GATE_REFRESHING = True
            _DECISION_GATE_REFRESH_MANUAL = manual
    if refreshing:
        # 后台刷新进行中（fast 访问器触发，且 manual 键一致）：同步调用是权威路径——
        # 轮询等待其完成（≤预算），拿全量门禁；等不到再回退同键旧值/基础闸门。
        # 轮询在锁外进行：_DECISION_GATE_LOCK 不可重入，绝不能在持有它时再 acquire
        # （否则同一线程自死锁、锁被永久占住——2026-08-18 实测 3 文件定向测试挂死）。
        _deadline = now + _DECISION_GATE_BUDGET
        while _time.monotonic() < _deadline:
            _time.sleep(0.2)
            with _DECISION_GATE_LOCK:
                _rf = _DECISION_GATE_CACHE["payload"]
                if (_rf is not None
                        and _DECISION_GATE_CACHE.get("manual") == manual
                        and _time.monotonic()
                        - float(_DECISION_GATE_CACHE["loaded_at"]) < _DECISION_GATE_TTL):
                    return copy.deepcopy(_rf)
        # An expired allow verdict is never authoritative. If evidence cannot
        # be refreshed within budget, lock recommendations instead of reusing
        # or silently renewing stale permission.
        return _manual_gate_fallback(manual)
    # 本线程成为刷新者：有界求值全量门禁并写缓存（最坏 ~6s < 预算 8s）。
    try:
        _ok, value, _err = _bounded_call(
            _decision_gate_status_full, _DECISION_GATE_BUDGET)
        if _ok and isinstance(value, dict):
            result = value
        else:
            result = _manual_gate_fallback(manual)
        with _DECISION_GATE_LOCK:
            _DECISION_GATE_CACHE["payload"] = copy.deepcopy(result)
            _DECISION_GATE_CACHE["loaded_at"] = now
            _DECISION_GATE_CACHE["manual"] = manual
        return result
    finally:
        with _DECISION_GATE_LOCK:
            _DECISION_GATE_REFRESHING = False
            _DECISION_GATE_REFRESH_MANUAL = None

def _gate_refresh_worker(manual: bool) -> None:
    """后台刷新全量门禁：冷缺失时请求线程绝不等待；刷新完成后缓存即热。

    直接求值全量门禁（绕过 _decision_gate_status，否则会看到 REFRESHING=True 直接短路）。
    """
    global _DECISION_GATE_REFRESHING, _DECISION_GATE_REFRESH_MANUAL
    try:
        _ok, value, _err = _bounded_call(
            _decision_gate_status_full, _DECISION_GATE_BUDGET)
        result = (
            value if _ok and isinstance(value, dict)
            else _manual_gate_fallback(manual)
        )
        with _DECISION_GATE_LOCK:
            _DECISION_GATE_CACHE["payload"] = copy.deepcopy(result)
            _DECISION_GATE_CACHE["loaded_at"] = _time.monotonic()
            # Bind the result to the mode that launched this refresh. An env
            # change while the worker is running must not relabel old facts.
            _DECISION_GATE_CACHE["manual"] = manual
    except Exception:
        with _DECISION_GATE_LOCK:
            _DECISION_GATE_CACHE["payload"] = _manual_gate_fallback(manual)
            _DECISION_GATE_CACHE["loaded_at"] = _time.monotonic()
            _DECISION_GATE_CACHE["manual"] = manual
    finally:
        with _DECISION_GATE_LOCK:
            if _DECISION_GATE_REFRESH_MANUAL == manual:
                _DECISION_GATE_REFRESHING = False
                _DECISION_GATE_REFRESH_MANUAL = None

def _manual_gate_fallback(manual: bool) -> dict[str, object]:
    """Preserve explicit consent/broker truth while full evidence refreshes."""
    gate = dict(_BASE_DECISION_GATE_STATUS())
    gate.update({
        # This helper is used only while full, current evidence is unavailable.
        # Never inherit an allow verdict from a static/older base snapshot.
        "recommendation_eligible": False,
        "research_only": True,
        "status": "locked_evidence_refresh",
        "reason": "当前权威决策证据尚未刷新；按未通过处理，不生成买入建议。",
        "manual_real_money_enabled": manual,
        "manual_consent": manual,
        "manual_broker_ticket_enabled": False,
        "broker_connected": False,
        "broker_orders_enabled": False,
        "automatic_trading": False,
        "stable_profit_proven": False,
        "manual_research_alerts_allowed": manual,
        "formal_strategy_recommendations_allowed": False,
        "manual_stock_execution_source": (
            "/api/opportunities" if manual else None
        ),
    })
    if manual:
        gate.update({
            "recommendation_eligible": False,
            "research_only": True,
            "status": "locked_manual_consent",
            "reason": (
                "已记录用户愿意手工操作，但人工同意不改变研究推荐资格。"
                "当前独立、成本后、前向证据仍未通过，因此不生成正式策略推荐。"
                "研究型人工复核提醒仍可在证据刷新后按统一质量、买点与风控条件触发；"
                "系统不连接券商、不自动交易。"
            ),
        })
    return gate

def _decision_gate_status_fast(*, start_refresh: bool = True) -> dict[str, object]:
    """Never block polling; an expired verdict becomes locked while refreshing."""
    global _DECISION_GATE_REFRESHING, _DECISION_GATE_REFRESH_MANUAL
    manual = os.environ.get("OLYMPUS_MANUAL_REAL_MONEY_MODE") == "1"
    now = _time.monotonic()
    with _DECISION_GATE_LOCK:
        cached = _DECISION_GATE_CACHE["payload"]
        if (cached is not None
                and _DECISION_GATE_CACHE.get("manual") == manual
                and now - float(_DECISION_GATE_CACHE["loaded_at"]) < _DECISION_GATE_TTL):
            return copy.deepcopy(cached)
        # 只有同一 manual 键的刷新在跑才复用；键不同（env 翻转）则另行起刷新。
        if start_refresh and not (_DECISION_GATE_REFRESHING
                and _DECISION_GATE_REFRESH_MANUAL == manual):
            _DECISION_GATE_REFRESHING = True
            _DECISION_GATE_REFRESH_MANUAL = manual
            threading.Thread(
                target=_gate_refresh_worker, args=(manual,), daemon=True,
                name="decision-gate-refresh").start()
        # An expired allow verdict must never survive.  An explicit locked
        # verdict, however, cannot grant execution authority and is safer and
        # clearer than flashing back to an unrelated legacy base report while
        # the same evidence refreshes in the background.
        if (
            cached is not None
            and _DECISION_GATE_CACHE.get("manual") == manual
            and cached.get("recommendation_eligible") is False
            and cached.get("research_only") is True
        ):
            locked = copy.deepcopy(cached)
            locked["evidence_refreshing"] = True
            return locked
    return _manual_gate_fallback(manual)

def _historical_candidate_headline(gate: dict[str, object]) -> dict[str, object] | None:
    candidate, reproduction = _stability_80_20_reports()
    if not _stability_80_20_passed(candidate, reproduction):
        return _BASE_HISTORICAL_CANDIDATE_HEADLINE(gate)
    cost40 = (candidate.get("cost_ladder_metrics") or {}).get("40.0") or {}
    cost120 = (candidate.get("cost_ladder_metrics") or {}).get("120.0") or {}
    validation = cost40.get("validation_2022_2023") or {}
    test = cost40.get("test_2024_2025") or {}
    diagnostic = cost40.get("diagnostic_2026_through_july") or {}
    stress_validation = cost120.get("validation_2022_2023") or {}
    paper_forward = _latest_stability_paper_forward_report()
    execution_reality = _stability_execution_reality_report()
    risk_sleeve_attribution = _risk_sleeve_incremental_attribution_report()
    selection_bias_audit = _rolling_risk_cap_walk_forward_report()
    risk_periods = risk_sleeve_attribution.get("period_attribution") or {}
    formal_forward = paper_forward.get("formal_forward") or {}
    return {
        "method": "V221/V223 稳定优先80/20核心—卫星（独立复算通过）",
        "metric_scope": "v221_stability_80_20_paper_candidate",
        "validation_total_return_pct": validation.get("total_return_pct"),
        "validation_annualized_return_pct": validation.get("annualized_return_pct"),
        "validation_max_drawdown_pct": validation.get("max_drawdown_pct"),
        "validation_yearly_total_return_pct": validation.get("yearly_total_return_pct") or {},
        "test_total_return_pct": test.get("total_return_pct"),
        "test_annualized_return_pct": test.get("annualized_return_pct"),
        "test_max_drawdown_pct": test.get("max_drawdown_pct"),
        "diagnostic_total_return_pct": diagnostic.get("total_return_pct"),
        "diagnostic_max_drawdown_pct": diagnostic.get("max_drawdown_pct"),
        "stress_120bp_validation_total_return_pct": stress_validation.get("total_return_pct"),
        "stress_120bp_2022_return_pct": (
            (stress_validation.get("yearly_total_return_pct") or {}).get("2022")
        ),
        "independent_months": sum(
            int((row or {}).get("v223_rows") or 0)
            for row in (reproduction.get("monthly_comparison") or {}).values()
        ),
        "current_paper_allocation": _stability_80_20_allocation(candidate, reproduction),
        "research_observation_allocation": _stability_80_20_allocation(candidate, reproduction),
        "capital_constrained_execution": (
            execution_reality.get("current_lot_plan") or {}
        ),
        "execution_reality": {
            "audit_id": execution_reality.get("audit_id"),
            "passed": execution_reality.get("execution_reality_passed") is True,
            "stress_50bp": (
                (execution_reality.get("scenario_metrics") or {}).get("50.0") or {}
            ),
            "stable_profit_proven": execution_reality.get("stable_profit_proven") is True,
        },
        "risk_sleeve_attribution": {
            "audit_id": risk_sleeve_attribution.get("audit_id"),
            "retained": risk_sleeve_attribution.get("risk_sleeve_retained") is True,
            "years_beating_cash": risk_sleeve_attribution.get("years_beating_cash") or [],
            "years_lagging_cash": risk_sleeve_attribution.get("years_lagging_cash") or [],
            "test_incremental_return_pct_points": (
                (risk_periods.get("test_2024_2025") or {}).get(
                    "incremental_total_return_pct_points"
                )
            ),
            "diagnostic_incremental_return_pct_points": (
                (risk_periods.get("diagnostic_2026_through_july") or {}).get(
                    "incremental_total_return_pct_points"
                )
            ),
            "stable_profit_proven": False,
        },
        "selection_bias_audit": {
            "audit_id": selection_bias_audit.get("audit_id"),
            "status": selection_bias_audit.get("status"),
            "walk_forward_historical_passed": (
                selection_bias_audit.get("walk_forward_historical_passed") is True
            ),
            "supersedes_fixed_20pct": (
                selection_bias_audit.get(
                    "supersedes_fixed_20pct_for_paper_observation"
                ) is True
            ),
            "comparison": selection_bias_audit.get("comparison") or {},
            "rejection_reason": selection_bias_audit.get("rejection_reason"),
        },
        "invalidated": False,
        "historical_gate_passed": True,
        "paper_forward_eligible": True,
        "paper_only": True,
        "research_observation_only": False,
        "recommendation_eligible": False,
        "automatic_trading": False,
        "formal_forward_observations": formal_forward.get(
            "completed_post_freeze_common_closes", 0
        ),
        "formal_forward_return_pct": formal_forward.get("formal_forward_return_pct", 0.0),
        "formal_forward_max_drawdown_pct": formal_forward.get("max_drawdown_pct", 0.0),
        "formal_forward_latest_date": formal_forward.get("latest_observation_date"),
        "formal_forward_status": formal_forward.get("status"),
        "generated_at": paper_forward.get("generated_at") or reproduction.get("generated_at"),
        "audit_message": gate.get("reason"),
    }

def _profitability_research_evidence() -> dict[str, object]:
    evidence = _BASE_PROFITABILITY_RESEARCH_EVIDENCE()
    candidate, reproduction = _stability_80_20_reports()
    if not _stability_80_20_passed(candidate, reproduction):
        return evidence
    cost40 = (candidate.get("cost_ladder_metrics") or {}).get("40.0") or {}
    current = _stability_80_20_allocation(candidate, reproduction)
    evidence["latest_independent_mechanism"] = {
        "available": True,
        "test_id": "V223",
        "candidate_id": candidate.get("strategy_id"),
        "status": reproduction.get("status"),
        "results": {
            "training_audit": _stability_metric(cost40, "development_2015_2021"),
            "validation": _stability_metric(cost40, "validation_2022_2023"),
            "test": _stability_metric(cost40, "test_2024_2025"),
            "diagnostic_2026": _stability_metric(cost40, "diagnostic_2026_through_july"),
        },
        "test_folds": [],
        "failed_gates": [],
        "historical_factor_gate_passed": True,
        "historical_candidate_passed": True,
        "paper_forward_eligible": True,
        "current_paper_allocation": current,
        "research_observation_allocation": current,
        "recommendation_eligible": False,
        "external_model_used": False,
        "paid_api_used": False,
        "source": "reports/v223_stability_80_20_independent_reproduction.json",
    }
    evidence["stability_candidate"] = candidate
    evidence["stability_reproduction"] = reproduction
    evidence["stability_paper_forward"] = _latest_stability_paper_forward_report()
    evidence["stability_execution_reality"] = _stability_execution_reality_report()
    evidence["risk_sleeve_incremental_attribution"] = (
        _risk_sleeve_incremental_attribution_report()
    )
    evidence["rolling_risk_cap_walk_forward"] = (
        _rolling_risk_cap_walk_forward_report()
    )
    evidence["cash_floor_holding_horizon"] = (
        _cash_floor_holding_horizon_report()
    )
    return evidence

def _system_evidence(
    decision_gate: dict[str, object] | None = None,
) -> dict[str, object]:
    if decision_gate is None:
        decision_gate = _decision_gate_status()
    evidence = _BASE_SYSTEM_EVIDENCE(decision_gate)
    candidate, reproduction = _stability_80_20_reports()
    if not _stability_80_20_passed(candidate, reproduction):
        return evidence
    cash_floor = _cash_floor_holding_horizon_report()
    evidence["stability_first_80_20"] = {
        "available": True,
        "strategy_id": candidate.get("strategy_id"),
        "audit_id": reproduction.get("audit_id"),
        "historical_candidate_passed": True,
        "independent_reproduction_passed": True,
        "paper_forward_eligible": True,
        "recommendation_eligible": False,
        "real_money_eligible": False,
        "automatic_trading": False,
        "current_paper_allocation": _stability_80_20_allocation(candidate, reproduction),
        "paper_forward": _latest_stability_paper_forward_report(),
        "execution_reality": _stability_execution_reality_report(),
        "risk_sleeve_incremental_attribution": (
            _risk_sleeve_incremental_attribution_report()
        ),
        "selection_bias_audit": _rolling_risk_cap_walk_forward_report(),
        "cash_floor_holding_horizon": cash_floor,
    }
    evidence["cash_floor_holding_horizon"] = cash_floor
    profitability = evidence.get("profitability") or {}
    successor = dict(profitability.get("successor_discovery") or {})
    successor.update({
        "latest_completed_candidate": candidate.get("strategy_id"),
        "latest_completed_status": reproduction.get("status"),
        "latest_completed_test": "V223",
        "latest_return_computed": True,
        "next_candidate_preregistered": False,
    })
    profitability["successor_discovery"] = successor
    evidence["profitability"] = profitability
    archived_blockers = []
    for raw_blocker in evidence.get("blockers") or []:
        blocker = {
            **raw_blocker,
            "archived": True,
            "current_scope": False,
        }
        if "V219" in str(blocker.get("label") or ""):
            blocker.update({
                "gap": (
                    "V209/V211/V213/V215不可继续使用；该结论已由V221/V223的"
                    "80/20纸面候选取代，不代表当前仍是100%现金"
                ),
                "superseded_by": [
                    "V221_STABILITY_FIRST_80_20_CORE_SATELLITE",
                    "V223_STABILITY_80_20_INDEPENDENT_REPRODUCTION",
                ],
            })
        archived_blockers.append(blocker)
    formal_forward = (
        _latest_stability_paper_forward_report().get("formal_forward") or {}
    )
    forward_observations = int(
        cash_floor.get("post_freeze_forward_observations") or 0
    )
    cash_forward = cash_floor.get("forward_tracking") or {}
    daily_recovery = cash_floor.get("daily_recovery") or {}
    daily_scenarios = daily_recovery.get("scenarios") or {}
    daily_base = daily_scenarios.get(
        "three_bp_commission_observed_spread_diagnostic"
    ) or {}
    daily_stress = daily_scenarios.get(
        "three_bp_commission_5bp_slippage_stress"
    ) or {}
    cross_product = cash_floor.get("cross_product") or {}
    cross_summary = cross_product.get("cross_product_summary") or {}
    dual_product = cash_floor.get("dual_product_floor") or {}
    dual_comparison = dual_product.get("comparison") or {}
    dual_forward = dual_product.get("forward_tracking") or {}
    cash_mark = cash_forward.get("current_mark") or {}
    cash_entry = cash_forward.get("paper_entry") or {}
    active_forward_blocker = {
        "category": "profitability",
        "severity": "warning",
        "archived": False,
        "current_scope": True,
        "label": "V233纸面前向验证进度",
        "current": (
            f"{forward_observations}个正式收盘 · 盘中基线已建立"
            if cash_mark else f"{forward_observations}个正式收盘 · 等待行情基线"
        ),
        "target": str(cash_forward.get("next_action") or "累计真实纸面收盘记录"),
        "gap": "系统已开始自动记账；盘中估值不冒充正式收盘",
        "detail": (
            "V233已证明纯511880在5/20/50BP双边成本下，历史全部完成批次对应"
            "3/6/12个月持有期为正；当前20万元5BP纸面计划为511880 1900股。"
            f"当前盘中价{cash_mark.get('price', '—')}、成本后估算清算收益"
            f"{cash_mark.get('net_liquidation_return_pct', '—')}%；"
            f"真实前向已按形成后首次卖一价{cash_entry.get('ask_price', '—')}冻结入场，"
            f"V237逐日历史诊断覆盖{daily_base.get('completed_entry_cohorts', 0)}个完整入场日，"
            f"3BP佣金+观察价差持续回正中位{daily_base.get('median_durable_positive_trading_days', '—')}日、"
            f"最慢{daily_base.get('maximum_durable_positive_trading_days', '—')}日；"
            f"额外5BP滑点压力最慢{daily_stress.get('maximum_durable_positive_trading_days', '—')}日。"
            f"V239已横向复现{cross_summary.get('product_count', 0)}只真实货币ETF，"
            f"基准成本下跨产品最慢持续回正为"
            f"{cross_summary.get('base_maximum_durable_days_across_products', '—')}日；"
            f"V241双产品底座把历史最慢持续回正降到"
            f"{((dual_comparison.get('base_max_durable_days') or {}).get('dual', '—'))}日，"
            f"V243已累计{dual_forward.get('completed_closes', 0)}个完整正式收盘；"
            f"已累计{forward_observations}个形成后正式收盘。系统只在交易所正式收盘后加1，"
            "不会把盘中价格伪造成前向证据。"
        ),
    }
    live_snapshot = _safe_live_signal_snapshot()
    live_signal_blocker = {
        "category": "profitability",
        "severity": "warning",
        "archived": False,
        "current_scope": True,
        "label": "实时因子60/40组合已停用",
        "current": "2026独立复权样本失败 · 5BP平均每笔约-0.91%",
        "target": "开发规则完整且未见样本成本后为正的新股票策略",
        "gap": "旧组合追涨、换手高，独立样本在5/20/50BP下均为负",
        "detail": (
            "使用上交所主板复权开收盘数据复核：2026-01至2026-06共816笔可执行交易，"
            "下一交易日开盘入场、持有10个交易日、100股整数手、最低5元佣金、"
            "卖出印花税及双边滑点均计入；5/20/50BP平均每笔约-0.91%/-1.20%/-1.80%。"
            "该规则已明确否决，信号中心不再把它输出为真钱买入。"
        ),
        "strategy_validation": live_snapshot.get("strategy_validation") or {},
    }
    evidence["cash_floor_forward_tracking"] = cash_forward
    evidence["cash_floor_cross_product"] = cross_product
    evidence["dual_cash_floor"] = dual_product
    evidence["live_factor_signal_validation"] = live_signal_blocker
    evidence["active_blockers"] = [active_forward_blocker, live_signal_blocker]
    evidence["archived_blockers"] = archived_blockers
    evidence["blockers"] = archived_blockers + [active_forward_blocker, live_signal_blocker]
    return evidence

def _safe_live_signal_snapshot() -> dict[str, object]:
    """Fail closed at the API boundary if a stale snapshot still contains BUY."""
    snapshot = dict(load_live_signals() or {})
    served_at = datetime.now(_SH_TZ).isoformat(timespec="seconds")
    # A closed-market signal snapshot can legitimately remain unchanged while
    # the deterministic monitoring universe is refreshed. Keep its original
    # scope for audit, but serve the current pool identity so dashboard and
    # signal pages cannot disagree about what the next session will monitor.
    current_watchlist = _signal_watchlist()
    current_scope = _signal_watchlist_scope(len(current_watchlist))
    current_identity = _signal_realtime_universe()
    current_scope.update({
        "realtime_universe_status": current_identity.get("status"),
        "realtime_universe_reason": current_identity.get("reason"),
        "eligible_snapshot_complete": (
            current_identity.get("eligible_snapshot_complete") is True
        ),
        "universe_total": current_identity.get("universe_total"),
        "full_a_share_coverage": current_identity.get("full_a_share_coverage") is True,
        "included_segments": current_identity.get("included_segments") or [],
        "excluded_segments": current_identity.get("excluded_segments") or [],
    })
    generated_scope = snapshot.get("watchlist_scope")
    if (
        isinstance(generated_scope, dict)
        and generated_scope.get("data_asof") == current_scope.get("data_asof")
    ):
        for key in (
            "quote_attempted", "quote_covered", "quote_missing",
            "fresh_quote_covered", "monitor_selected", "deep_selected",
            "realtime_eligible_coverage_complete",
        ):
            if key in generated_scope:
                current_scope[key] = generated_scope[key]
    if isinstance(generated_scope, dict) and generated_scope != current_scope:
        snapshot["generated_watchlist_scope"] = generated_scope
        current_scope["changed_since_signal_snapshot"] = True
    snapshot["watchlist_scope"] = current_scope
    snapshot["n_watchlist"] = len(current_watchlist)
    quote_meta = dict(snapshot.get("quote_meta") or {})
    quote_meta["served_at"] = served_at
    snapshot["quote_meta"] = quote_meta
    snapshot["served_at"] = served_at
    # The background writer can still be fetching factors after a service restart.
    # Read the tiny validation report here so every API reflects the current verdict.
    factor_registry = snapshot.get("factor_registry") or {}
    catalog_version = (
        factor_registry.get("catalog_version")
        if isinstance(factor_registry, dict) else None
    )
    stale_reasons = []
    eligibility_blockers = []
    current_version = None
    generated_at = snapshot.get("generated_at")
    market_data_time = quote_meta.get("as_of") or generated_at
    snapshot_age_seconds = None
    freshness = _fund_flow_freshness(market_data_time)
    snapshot_age_seconds = freshness.get("age_seconds")
    if freshness.get("state") not in {"fresh", "market_break", "market_closed"}:
        stale_reasons.append(
            f"实时行情{freshness.get('label') or '新鲜度无法核验'}"
        )
    market_status_for_coverage = (
        snapshot.get("market_status")
        if isinstance(snapshot.get("market_status"), dict) else {}
    )
    market_open_for_coverage = market_status_for_coverage.get("open") is True
    closed_market_snapshot = bool(
        market_status_for_coverage.get("open") is False
        or freshness.get("state") in {"market_break", "market_closed"}
    )
    coverage_expected = int(
        quote_meta.get("coverage_expected") or len(current_watchlist)
    )
    display_coverage = int(
        quote_meta.get("coverage_returned") or snapshot.get("n_quotes") or 0
    )
    raw_quote_health = str(quote_meta.get("health_status") or "unknown")
    live_coverage = (
        int(quote_meta.get("decision_coverage_returned") or display_coverage)
        if market_open_for_coverage
        and quote_meta.get("degraded") is not True
        and raw_quote_health in {"live", "validated"}
        else 0
    )
    quote_meta.update({
        "display_coverage_returned": display_coverage,
        "live_decision_coverage_returned": live_coverage,
        "coverage_expected": coverage_expected,
        "coverage_complete": bool(
            market_open_for_coverage
            and coverage_expected > 0
            and live_coverage >= math.ceil(coverage_expected * 0.8)
        ),
        "coverage_mode": (
            "market_closed_reference_only"
            if closed_market_snapshot else "live_decision_universe"
        ),
        "operational_health_status": (
            "market_closed" if closed_market_snapshot else raw_quote_health
        ),
        "operational_degraded": (
            False if closed_market_snapshot else quote_meta.get("degraded") is True
        ),
        "coverage_reason": (
            f"休市仅保留{display_coverage}只参考快照；下次连续交易时段尝试完整合格候选"
            f"{int(current_scope.get('universe_total') or coverage_expected)}只，"
            f"统一排序后持续监控Top{len(current_watchlist)}，不足80%不产生新信号"
            if closed_market_snapshot
            else f"交易时段实时覆盖{live_coverage}/{coverage_expected}；统一排序后持续监控"
                 f"Top{len(current_watchlist)}，不足80%不产生新信号"
        ),
    })
    current_total = int(current_scope.get("universe_total") or coverage_expected)
    attempted = int(quote_meta.get("coverage_attempted") or coverage_expected)
    covered = int(quote_meta.get("coverage_returned") or display_coverage)
    monitor_selected = int(snapshot.get("n_watchlist") or len(current_watchlist))
    realtime_complete = bool(
        not closed_market_snapshot
        and current_scope.get("eligible_snapshot_complete") is True
        and attempted >= current_total
        and live_coverage >= current_total
    )
    current_scope.update({
        "universe_total": current_total,
        "quote_attempted": attempted,
        "quote_covered": covered,
        "quote_missing": max(0, current_total - covered),
        "monitor_selected": monitor_selected,
        "deep_selected": min(24, monitor_selected),
        "coverage_mode": quote_meta["coverage_mode"],
        "closed_reference": closed_market_snapshot,
        "realtime": realtime_complete,
        "realtime_eligible_coverage_complete": realtime_complete,
    })
    snapshot["quote_meta"] = quote_meta
    snapshot["n_live_quotes"] = live_coverage
    try:
        from factor_system import production as factor_production

        current_catalog = factor_production.verified_catalog()
        current_version = current_catalog.get("catalog_version")
        if not current_catalog.get("complete"):
            eligibility_blockers.append("当前没有可复核的 production 消费目录")
        if current_version != catalog_version:
            eligibility_blockers.append("production catalog 版本已变化")
        snapshot_complete = bool(
            isinstance(factor_registry, dict) and factor_registry.get("complete")
        )
        if snapshot_complete != bool(current_catalog.get("complete")):
            eligibility_blockers.append("production catalog 可用状态已变化")
        if snapshot_complete:
            snapshot_input = factor_registry.get("runtime_data_key")
            current_input = factor_production.current_input_identity()
            if not snapshot_input or snapshot_input != current_input:
                eligibility_blockers.append("production Panel/基准输入身份已变化")
    except Exception as exc:
        eligibility_blockers.append(f"当前 production catalog 无法复核: {exc}")
    validation = _live_strategy_validation_status(current_version)
    snapshot["strategy_validation"] = validation
    exact_strategy_passed = bool(
        not stale_reasons
        and not eligibility_blockers
        and isinstance(validation, dict) and validation.get("passed") is True
        and validation.get("provenance_complete") is True
        and isinstance(factor_registry, dict)
        and factor_registry.get("complete") is True
    )
    validation_reason = (
        str(validation.get("reason") or "")
        if isinstance(validation, dict) else ""
    )

    def _current_strategy_ref(row: dict) -> bool:
        """A cached BUY is actionable only with its complete current identity."""
        ref = row.get("strategy_ref")
        evidence = ref.get("validation_evidence") if isinstance(ref, dict) else None
        expected_evidence = (
            validation.get("validation_evidence")
            if isinstance(validation, dict) else None
        )
        return bool(
            isinstance(ref, dict)
            and ref.get("origin") == "production_strategy"
            and ref.get("production_eligible") is True
            and ref.get("strategy_id") == validation.get("strategy_id")
            and ref.get("strategy_spec_sha256") == validation.get("strategy_spec_sha256")
            and ref.get("catalog_version") == catalog_version
            and ref.get("runtime_data_key") == factor_registry.get("runtime_data_key")
            and ref.get("factor_as_of") == row.get("factor_as_of")
            and isinstance(evidence, dict)
            and isinstance(expected_evidence, dict)
            and evidence.get("path") == expected_evidence.get("path")
            and evidence.get("sha256") == expected_evidence.get("sha256")
            and isinstance(ref.get("signal_id"), str) and bool(ref["signal_id"])
        )

    rows = []
    for raw in snapshot.get("signals") or []:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        if stale_reasons and row.get("action") in {"BUY", "SELL", "WATCH"}:
            blocker = "；".join(stale_reasons)
            row.update({
                "action": "NO_SIGNAL",
                "execution_eligible": False,
                "execution_blocker": blocker,
                "reason": f"{row.get('reason') or ''} + {blocker}".strip(" +"),
            })
        elif row.get("action") == "BUY" and not exact_strategy_passed:
            blocker = validation_reason or "该实时60/40组合未通过成本后验证，只允许观察"
            row.update({
                "action": "WATCH",
                "execution_eligible": False,
                "execution_blocker": blocker,
                "reason": f"{row.get('reason') or ''} + {blocker}".strip(" +"),
            })
        elif row.get("action") == "BUY" and not _current_strategy_ref(row):
            blocker = "实时买入信号缺少与当前生产策略一致的不可变身份或验证证据"
            row.update({
                "action": "WATCH",
                "execution_eligible": False,
                "execution_blocker": blocker,
                "reason": f"{row.get('reason') or ''} + {blocker}".strip(" +"),
            })
        rows.append(row)
    snapshot["signals"] = rows
    snapshot["stale"] = bool(stale_reasons)
    snapshot["stale_reasons"] = stale_reasons
    snapshot["eligibility_blockers"] = eligibility_blockers
    snapshot["snapshot_age_seconds"] = (
        round(max(0.0, snapshot_age_seconds), 1)
        if isinstance(snapshot_age_seconds, (int, float)) else None
    )
    snapshot["snapshot_health"] = {
        "current": not stale_reasons,
        "age_seconds": snapshot["snapshot_age_seconds"],
        "catalog_version": catalog_version,
        "current_catalog_version": current_version,
        "runtime_data_key": (
            factor_registry.get("runtime_data_key")
            if isinstance(factor_registry, dict) else None
        ),
        "blockers": list(stale_reasons),
        "freshness": freshness,
    }
    snapshot["strategy_validation"] = (
        validation if isinstance(validation, dict) else {}
    )
    snapshot["decision_eligible"] = exact_strategy_passed
    market_status = (
        snapshot.get("market_status")
        if isinstance(snapshot.get("market_status"), dict) else {}
    )
    zero_signal_reasons = []
    if market_status.get("open") is False:
        zero_signal_reasons.append(
            f"当前{market_status.get('label') or '不在连续交易时段'}"
        )
    zero_signal_reasons.extend(stale_reasons)
    zero_signal_reasons.extend(eligibility_blockers)
    if (
        not exact_strategy_passed
        and not eligibility_blockers
        and validation_reason
    ):
        zero_signal_reasons.append(validation_reason)
    snapshot["zero_signal_reasons"] = list(dict.fromkeys(
        str(reason).strip()
        for reason in zero_signal_reasons
        if str(reason).strip()
    ))
    if not exact_strategy_passed:
        snapshot["execution_mode"] = "research_observation"
        stale_text = (
            "；".join(stale_reasons) + "。"
            if stale_reasons else ""
        )
        eligibility_text = (
            "；".join(eligibility_blockers) + "。"
            if eligibility_blockers else ""
        )
        monitoring_text = (
            "实时监控在已审计主板、创业板、科创板完整合格候选行情截面"
            "统一排序后保留Top300，北交所边界单独披露。"
            if current_identity.get("eligible_snapshot_complete") is True
            else "完整合格候选快照尚未冻结；当前仅用旧Top候选缓存，"
                 "不得称为全市场实时覆盖，北交所仍未覆盖。"
        )
        snapshot["honesty"] = (
            "行情为逐票校验后的交易所时间快照；因子按registry.db中的production状态、"
            "冻结证据与内容寻址制品复核后，在沪深主板与创业板研究截面"
            "（不含科创板、北交所）重算；"
            f"{monitoring_text}"
            f"{stale_text}{eligibility_text}"
            f"{validation_reason or '当前组合未通过成本后验证'}，"
            "因此只观察、不生成真钱买入票据。"
        )
    return snapshot


def _apply_current_market_runtime_contract(payload: dict) -> dict:
    """Overlay current universe identity without rewriting the old signal audit."""
    live = _safe_live_signal_snapshot()
    current = live.get("watchlist_scope") or {}
    quote_meta = live.get("quote_meta") or {}
    monitoring = dict(payload.get("monitoring_scope") or {})
    for key in (
        "universe_total", "quote_attempted", "quote_covered", "quote_missing",
        "monitor_selected", "deep_selected", "realtime",
        "realtime_eligible_coverage_complete", "changed_since_signal_snapshot",
        "coverage_mode", "closed_reference", "full_a_share_coverage",
        "included_segments", "excluded_segments",
    ):
        if key in current:
            monitoring[key] = current[key]
    monitoring["coverage_mode"] = quote_meta.get("coverage_mode")
    monitoring["closed_reference"] = (
        quote_meta.get("coverage_mode") == "market_closed_reference_only"
    )
    monitoring["realtime"] = (
        current.get("realtime_eligible_coverage_complete") is True
    )
    generated = live.get("generated_watchlist_scope")
    if isinstance(generated, dict):
        monitoring["generated_watchlist_scope"] = copy.deepcopy(generated)
    payload["monitoring_scope"] = monitoring

    full_market = _read_full_market_snapshot()
    eligible_count = int(full_market.get("eligible_count") or 0)
    strategy = dict(payload.get("assessment_strategy") or {})
    if full_market.get("eligible_complete") is True and eligible_count > 0:
        strategy["universe_light_scored"] = eligible_count
        strategy["frozen_eligible_compared_count"] = eligible_count
    strategy["runtime_retained_scored_count"] = int(
        payload.get("candidate_count")
        or monitoring.get("merged_candidate_count")
        or monitoring.get("full_market_shortlist_count")
        or 0
    )
    strategy["universe_light_scored_role"] = (
        "compatibility_alias_of_frozen_eligible_compared_count"
    )
    strategy["retained_pool_professional_preview_available_count"] = strategy.get(
        "professional_preview_available_count"
    )
    strategy["retained_pool_professional_preview_scope_count"] = int(
        monitoring.get("full_market_shortlist_count")
        or monitoring.get("monitor_selected") or 0
    )
    scan_audit = (
        full_market.get("scan_audit")
        if isinstance(full_market.get("scan_audit"), dict) else {}
    )
    strategy["professional_preview_available_count"] = int(
        scan_audit.get("professional_preview_available_count") or 0
    )
    strategy["professional_preview_scope_count"] = int(
        scan_audit.get("professional_preview_scope_count") or eligible_count
    )
    strategy["professional_preview_coverage_pct"] = scan_audit.get(
        "professional_preview_coverage_pct"
    )
    strategy["professional_preview_complete"] = (
        scan_audit.get("professional_preview_complete") is True
    )
    strategy["professional_preview_claim_scope"] = (
        "full_comparable"
        if strategy["professional_preview_complete"] else "scored_subset"
    )
    payload["assessment_strategy"] = strategy
    return payload

def _load_production_factor_candidates(limit: int = 20) -> list[dict]:
    """Return only current BUY rows from an exact, validated production snapshot."""
    snapshot = _safe_live_signal_snapshot()
    registry = snapshot.get("factor_registry") or {}
    validation = snapshot.get("strategy_validation") or {}
    if not (
        isinstance(registry, dict) and registry.get("complete") is True
        and isinstance(validation, dict)
        and validation.get("provenance_complete") is True
        and snapshot.get("stale") is False
        and snapshot.get("decision_eligible") is True
    ):
        return []
    rows = []
    for raw in snapshot.get("signals") or []:
        if not isinstance(raw, dict) or raw.get("action") != "BUY":
            continue
        factor_z = _finite_number(raw.get("factor_z"))
        strategy_ref = raw.get("strategy_ref")
        if (
            factor_z is None
            or not isinstance(strategy_ref, dict)
            or strategy_ref.get("production_eligible") is not True
        ):
            continue
        rows.append({
            "code": str(raw.get("code") or ""),
            "name": str(raw.get("name") or ""),
            "source": "production_factor",
            "pred_ret_5d": None, "pred_ret_10d": None,
            "pred_ret_20d": None, "pred_ret_30d": None,
            "validation_status": "validated_production_factor",
            "freshness_status": "fresh",
            "decision_eligible": True,
            "research_only": False,
            "source_data_as_of": raw.get("factor_as_of"),
            "strategy_ref": copy.deepcopy(strategy_ref),
            "factor_signal": {
                key: raw.get(key) for key in (
                    "action", "score", "factor_z", "momentum_z", "factor_as_of",
                    "execution_eligible", "reason",
                )
            },
        })
    return [row for row in rows if CODE_PATTERN.fullmatch(row["code"])][:limit]

def _synced_live_signal_data() -> dict[str, object]:
    """Expose factor observations without claiming account trade capacity."""
    snapshot = _safe_live_signal_snapshot()
    rows = [
        row for row in snapshot.get("signals") or []
        if isinstance(row, dict) and row.get("action") in {"BUY", "SELL", "WATCH"}
    ]
    generated_at = str(snapshot.get("generated_at") or "")
    factor_asof = str(snapshot.get("required_factor_as_of") or "")
    quote_asof = str((snapshot.get("quote_meta") or {}).get("as_of") or "")
    data_asof = factor_asof or quote_asof[:10]
    staleness_days = _data_staleness_days(data_asof)
    observed_at = datetime.now(_SH_TZ).isoformat(timespec="seconds")
    return {
        "date": observed_at,
        "decision_day": observed_at[:10],
        "snapshot_generated_at": generated_at or None,
        "snapshot_date": data_asof,
        "data_asof": data_asof,
        "data_staleness_days": staleness_days,
        "data_stale": (
            bool(snapshot.get("stale")) or not bool(generated_at)
            or bool(factor_asof) and (
                staleness_days is None or staleness_days > 0
            )
        ),
        "research_only": snapshot.get("decision_eligible") is not True,
        "research_nominal_account": {
            "capital": ACCOUNT_CAPITAL,
            "max_positions": MAX_POSITIONS,
            "lot_size": 100,
            "position_pct": round(100 / MAX_POSITIONS),
            "role": "fixed_research_cost_diagnostic_nominal",
            "not_user_account": True,
        },
        "entry_timing": {
            "status": (
                "active" if snapshot.get("decision_eligible") is True else "observe"
            ),
            "reason": (
                "；".join(snapshot.get("zero_signal_reasons") or [])
                or "当前没有满足完整证据、时段与执行条件的信号"
            ),
            "zero_signal_reasons": list(
                snapshot.get("zero_signal_reasons") or []
            ),
        },
        "signals": rows,
        "industries_covered": [],
        "summary": {
            "total": len(rows),
            "buy": sum(row.get("action") == "BUY" for row in rows),
            "sell": sum(row.get("action") == "SELL" for row in rows),
            "watch": sum(row.get("action") == "WATCH" for row in rows),
            "max_per_day": None,
            "legacy_research_max_positions": MAX_POSITIONS,
            "sets_trade_capacity": False,
            "trade_capacity_source": "/api/strategy-route/research",
            "industries": 0,
        },
        "live_signal_snapshot": snapshot,
        "live_signal_source": "/api/signals/live",
        "decision_context_source": "/api/strategy-route/research",
        "current_action_source": "/api/opportunities",
    }

def _fast_compact_decision_gate() -> dict[str, object]:
    """Return the polling-safe decision facts without waiting for full evidence."""
    return _compact_decision_gate(_decision_gate_status_fast())


def _position_risk_alert_interpretation(
    alert: dict, positions_by_id: dict[int, dict],
) -> dict | None:
    """Explain a position-risk alert without rewriting its recorded history."""
    if (
        alert.get("action") != "sell"
        or alert.get("origin") != "position_risk_review"
    ):
        return None
    try:
        evidence = json.loads(str(alert.get("evidence_ref_json") or "{}"))
    except json.JSONDecodeError:
        evidence = {}
    if (
        not isinstance(evidence, dict)
        or evidence.get("method") not in {
            "position_risk_action_v1", "position_risk_action_v2",
        }
    ):
        return None
    try:
        position_id = int(evidence.get("position_id"))
    except (TypeError, ValueError):
        position_id = 0
    position = positions_by_id.get(position_id) or {}
    action_plan = alert.get("action_plan") if isinstance(alert.get("action_plan"), dict) else {}
    trade_plan = action_plan.get("trade_plan") if isinstance(action_plan.get("trade_plan"), dict) else {}
    action = trade_plan.get("action") if isinstance(trade_plan.get("action"), dict) else {}
    action_evidence = (
        action.get("evidence_identity")
        if isinstance(action.get("evidence_identity"), dict) else {}
    )
    risk_line = _finite_number(
        evidence.get("episode_risk_line")
        if evidence.get("episode_risk_line") is not None
        else action_evidence.get("effective_stop")
    )
    alert_price = _finite_number(alert.get("price"))
    episode_kind = str(evidence.get("episode_kind") or "")
    imported_snapshot = (
        position.get("action") == "position_snapshot"
        or position.get("entry_origin") == "manual_position_snapshot"
    )
    if episode_kind == "fresh_intraday_cross":
        classification = "fresh_intraday_risk_cross"
        summary = (
            "上一有效观测仍在风险线上方，本次首次跌破；这是盘中新风险事件，"
            "但仍不是经过收益验证的最佳卖点。"
        )
    elif episode_kind == "existing_breach_discovered_after_import" or imported_snapshot:
        classification = "existing_breach_discovered_after_import"
        summary = (
            "这是导入已有持仓后发现的风险线下状态；系统没有导入前的逐时点记录，"
            "不能证明首次跌破时刻，也不能称为最佳卖点。"
        )
    else:
        classification = "risk_review_timing_unproven"
        summary = (
            "历史记录只能证明提醒时价格低于风险线，不能证明当时是首次跌破或最佳卖点。"
        )
    facts = []
    if alert_price is not None:
        facts.append(f"提醒价 ¥{alert_price:.2f}")
    if risk_line is not None:
        facts.append(f"风险复核线 ¥{risk_line:.2f}")
    if position.get("created_at"):
        facts.append(f"持仓于 {str(position['created_at']).replace('T', ' ')[:19]} 录入")
    return {
        "classification": classification,
        "summary": summary,
        "facts": facts,
        "rule": "max(持仓成本×0.92, 持仓成本−2×ATR14)",
        "calibration_status": "rule_based_unvalidated",
        "best_timing_validated": False,
        "optimality_claim": False,
        "timed_exit_actionable": classification == "fresh_intraday_risk_cross",
        "decision_role": "loss_containment_review_only",
        "recorded_reason": alert.get("reason"),
        "database_history_rewritten": False,
    }


def create_app(
    *,
    start_background: bool = True,
    start_paper_forward: bool | None = None,
) -> Flask:
    if start_paper_forward is None:
        start_paper_forward = start_background
    app = Flask(
        __name__,
        template_folder=str(_PROJECT / "mom_trading" / "templates"),
        static_folder=str(_PROJECT / "mom_trading" / "static"),
        static_url_path="/static",
    )

    app.config["MAX_CONTENT_LENGTH"] = 128 * 1024
    app.config["WEBSITE_BACKGROUND_ENABLED"] = bool(start_background)
    app.config["FORWARD_CAPTURE_RUNTIME_STATUS_PATH"] = (
        _FORWARD_CAPTURE_RUNTIME_STATUS if start_background else None
    )
    app.config["REMOTE_MODE"] = os.environ.get("OLYMPUS_REMOTE_MODE") == "1"
    app.config["TRUSTED_ORIGINS"] = frozenset(
        value.strip().rstrip("/")
        for value in os.environ.get("OLYMPUS_TRUSTED_ORIGINS", "").split(",")
        if value.strip()
    )
    from .research_workbench import blueprint as research_blueprint, WRITABLE_ENDPOINTS
    app.register_blueprint(research_blueprint)
    from .playbook_development import blueprint as playbook_blueprint, WRITABLE_ENDPOINTS as PLAYBOOK_WRITES
    app.register_blueprint(playbook_blueprint)
    from .playbook_stock_checks import blueprint as playbook_stock_blueprint
    app.register_blueprint(playbook_stock_blueprint)
    research_writes = WRITABLE_ENDPOINTS | PLAYBOOK_WRITES
    from .website_foundation import register_routes as register_website_routes
    register_website_routes(app, root=_PROJECT, readiness=lambda: _readiness_health())

    def paid_api_disabled_response():
        return jsonify({
            "ok": False,
            "code": "external_paid_api_disabled",
            "error": PAID_API_DISABLED_MESSAGE,
            "external_paid_api_disabled": True,
            "network_requests_allowed": False,
        }), 410

    @app.before_request
    def _protect_request_boundary():
        begin_request()
        if app.config["REMOTE_MODE"]:
            if request.headers.get("X-Forwarded-Proto", "").casefold() != "https":
                return jsonify({"ok": False, "error": "远程访问必须经 HTTPS 反向代理"}), 403
            if request.endpoint == "remote_login_page" and request.method in {"GET", "POST"}:
                # This page uses a CSRF-bound form instead of the Basic challenge.
                return None
            configured = os.environ.get("OLYMPUS_REMOTE_AUTH_TOKEN", "")
            supplied = request.headers.get("Authorization", "")
            basic = request.authorization
            bearer_ok = bool(configured) and hmac.compare_digest(
                supplied, f"Bearer {configured}",
            )
            basic_ok = bool(
                configured
                and basic
                and basic.type.casefold() == "basic"
                and hmac.compare_digest(basic.username or "", "regis")
                and hmac.compare_digest(basic.password or "", configured)
            )
            cookie_ok = valid_session(configured, request.cookies.get(AUTH_COOKIE))
            if not bearer_ok and not basic_ok and not cookie_ok:
                if (
                    request.method == "GET"
                    and not request.path.startswith("/api/")
                    and "text/html" in request.headers.get("Accept", "").casefold()
                ):
                    response = redirect(url_for(
                        "remote_login_page", next=safe_next(request.full_path.rstrip("?"))
                    ))
                    response.headers["Cache-Control"] = "no-store"
                    return response
                response = jsonify({"ok": False, "error": "需要远程访问认证"})
                response.status_code = 401
                response.headers["WWW-Authenticate"] = 'Basic realm="Regis Oracle"'
                response.headers["X-Regis-Release"] = _PUBLIC_RELEASE_ID
                if request.path in {"/api/factor-system/factors", "/api/factor-system/status", "/api/backtest/factors"}:
                    response.headers["X-Regis-Factor-Registry"] = "read-health-v1"
                if request.path == "/api/research-worker/status":
                    from .mainline_diagnostics import public_implementation_sha
                    response.headers["X-Regis-Mainline-UI"] = public_implementation_sha()
                    response.headers["X-Regis-Manual-Ledger"] = manual_ledger_implementation_sha()
                    from .cloud_research_worker import public_status, market_data_health_header, pattern_input_health_header, pattern_feature_health_header, market_emotion_health_header, industry_daily_health_header, pattern_route_health_header
                    worker = public_status()
                    response.headers["X-Regis-Research-Worker"] = (
                        "cloud-online" if worker["online"]
                        and worker["execution_role"] == "cloud" else "unverified"
                    )
                    response.headers["X-Regis-Market-Data"] = market_data_health_header(worker)
                    response.headers["X-Regis-Pattern-Inputs"] = pattern_input_health_header(worker)
                    response.headers["X-Regis-Pattern-Features"] = pattern_feature_health_header(worker)
                    response.headers["X-Regis-Market-Emotion"] = market_emotion_health_header(worker)
                    response.headers["X-Regis-Industry-Daily"] = industry_daily_health_header(worker)
                    response.headers["X-Regis-Pattern-Route"] = pattern_route_health_header(worker)
                    from .cloud_sector_observations import health_header as sector_observation_header
                    response.headers["X-Regis-Sector-Observations"] = sector_observation_header(worker)
                    from .cloud_sector_close import health_header as sector_close_header
                    response.headers["X-Regis-Sector-Close"] = sector_close_header(worker)
                return response
            if request.method in _UNSAFE_METHODS and request.endpoint not in research_writes:
                return jsonify({
                    "ok": False,
                    "error": "远程账本与策略晋升固定只读；研究入口仅允许战法规格保存与适用性检查、受控历史实验登记及重试",
                }), 403

        if request.method == "POST" and request.path == "/api/ai/analyze":
            return paid_api_disabled_response()

        if request.method not in _UNSAFE_METHODS or not request.path.startswith("/api/"):
            return None
        if request.headers.get("Sec-Fetch-Site", "").casefold() in {
            "cross-site", "none",
        }:
            return jsonify({"ok": False, "error": "拒绝跨站写请求"}), 403

        origin = (request.headers.get("Origin") or "").rstrip("/")
        referer = request.headers.get("Referer") or ""
        if not origin and referer:
            parsed_referer = urlsplit(referer)
            origin = f"{parsed_referer.scheme}://{parsed_referer.netloc}".rstrip("/")
        if app.config["REMOTE_MODE"]:
            if not origin or origin not in app.config["TRUSTED_ORIGINS"]:
                return jsonify({"ok": False, "error": "写请求 Origin 不在允许列表"}), 403
        elif origin:
            parsed = urlsplit(origin)
            request_host = urlsplit(request.host_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not _is_loopback_host(parsed.hostname or "")
                or parsed.scheme != request_host.scheme
                or (parsed.hostname or "").casefold()
                != (request_host.hostname or "").casefold()
                or parsed.port != request_host.port
            ):
                return jsonify({"ok": False, "error": "写请求必须来自本机同源页面"}), 403
        elif not _is_loopback_host(request.remote_addr or ""):
            return jsonify({"ok": False, "error": "缺少可验证的同源请求头"}), 403

        # Only these local recovery routes accept bounded multipart snapshots.
        # Their own boundary requires loopback + explicit same Origin + header.
        if request.endpoint in {"registry_snapshot_inspect", "registry_snapshot_restore"}:
            if request.mimetype != "multipart/form-data":
                return jsonify({"ok": False, "error": "快照必须使用 multipart/form-data"}), 415
            return None

        if request.content_length:
            if request.mimetype != "application/json":
                return jsonify({"ok": False, "error": "写请求必须使用 application/json"}), 415
            if not isinstance(request.get_json(silent=True), dict):
                return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        return None

    @app.after_request
    def _secure_and_compress_response(response):
        if request.path in {"/api/factor-system/status", "/api/strategy-lab/development-catalog"}:
            response.headers["X-Regis-Cohort-Review"] = "signal-first-v1"
            crowding_source = _PROJECT / "mom_trading" / "crowding_factor_replay.py"
            response.headers["X-Regis-Crowding-Replay"] = (
                hashlib.sha256(crowding_source.read_bytes()).hexdigest()
                if crowding_source.is_file() else "unavailable"
            )
            entitlement_source = _PROJECT / "mom_trading" / "crowding_cash_settlement.py"
            response.headers["X-Regis-Entitlement-Replay"] = (
                hashlib.sha256(entitlement_source.read_bytes()).hexdigest()
                if entitlement_source.is_file() else "unavailable"
            )
            execution_input_sources = [
                _PROJECT / "mom_trading" / name
                for name in ("suspension_liquidity.py", "crowding_alias_identity.py")
            ]
            response.headers["X-Regis-Execution-Inputs"] = (
                hashlib.sha256(b"\0".join(p.read_bytes() for p in execution_input_sources)).hexdigest()
                if all(p.is_file() for p in execution_input_sources) else "unavailable"
            )
            attribution_sources = [
                _PROJECT / name for name in (
                    "mom_trading/crowding_exit_audit.py", "research/audit_crowding_deadline_inventory.py",
                    "mom_trading/emotion_factor_attribution.py", "research/run_emotion_factor_attribution.py",
                )
            ]
            response.headers["X-Regis-Research-Attribution"] = (
                hashlib.sha256(b"\0".join(p.read_bytes() for p in attribution_sources)).hexdigest()
                if all(p.is_file() for p in attribution_sources) else "unavailable"
            )
            pattern_cash_sources = [_PROJECT / name for name in (
                "mom_trading/emotion_pattern_cash_settlement.py",
                "research/run_emotion_pattern_cash_settlement.py",
                "research/run_emotion_control_cash_settlement.py",
            )]
            response.headers["X-Regis-Pattern-Cash-Research"] = (
                hashlib.sha256(b"\0".join(p.read_bytes() for p in pattern_cash_sources)).hexdigest()
                if all(p.is_file() for p in pattern_cash_sources) else "unavailable"
            )
        if request.path == "/api/strategy-lab/development-catalog":
            from .emotion_pattern_research import ENGINE_SHA256
            from .emotion_pattern_replay import contract_identity
            response.headers["X-Regis-Research-Engine"] = ENGINE_SHA256
            response.headers["X-Regis-Replay-Contract"] = contract_identity()
            controls_source = _PROJECT / "research" / "run_emotion_pattern_controls.py"
            response.headers["X-Regis-Research-Controls"] = (
                hashlib.sha256(controls_source.read_bytes()).hexdigest()
                if controls_source.is_file() else "unavailable"
            )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        accept_encoding = request.headers.get("Accept-Encoding", "")
        gzip_allowed = bool(re.search(
            r"(?:^|,)\s*gzip(?:\s*;\s*q=(?!0(?:\.0*)?\b)[0-9.]+)?",
            accept_encoding,
        ))
        compressible = (
            response.mimetype == "application/json"
            or response.mimetype.startswith("text/")
            or response.mimetype in {"application/javascript", "image/svg+xml"}
        )
        if compressible:
            vary = {
                token.strip() for token in response.headers.get("Vary", "").split(",")
                if token.strip()
            }
            vary.add("Accept-Encoding")
            response.headers["Vary"] = ", ".join(sorted(vary))
        if (
            gzip_allowed and compressible
            and response.status_code not in {204, 304}
            and not response.direct_passthrough
            and "Content-Encoding" not in response.headers
        ):
            raw = response.get_data()
            if len(raw) >= 1024:
                compressed = gzip.compress(raw, compresslevel=5)
                response.set_data(compressed)
                response.headers["Content-Encoding"] = "gzip"
                response.headers["Content-Length"] = str(len(compressed))
        return finish_response(response, app)

    @app.context_processor
    def _http_template_context():
        return {"csp_nonce": request_nonce()}

    @app.errorhandler(HTTPException)
    def _api_http_error(exc: HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": exc.description}), exc.code
        return exc

    @app.errorhandler(Exception)
    def _api_unhandled_error(exc: Exception):
        if request.path.startswith("/api/"):
            return internal_error_response(app, "服务器内部错误", exc)
        raise exc

    # ── 行情 & K线 Provider ───────────────────
    app.config["QUOTE_PROVIDER"] = CachedQuoteProvider(
        FallbackQuoteProvider([
            TencentBatchQuoteProvider(),
            SinaBatchQuoteProvider(),
        ]),
        ttl_seconds=30,
    )
    app.config["CANDLE_PROVIDER"] = CachedCandleProvider(
        FallbackCandleProvider(
            # 只保留具备原生 connect/read timeout 的生产源；AKShare 包装层
            # 无法保证硬超时，不能作为常驻 web 线程中的后备源。
            [TencentCandleProvider()]
        ),
        ttl_seconds=60,
    )
    app.config["MARKET_INTELLIGENCE"] = MarketIntelligence(
        quote_provider=app.config["QUOTE_PROVIDER"],
        fund_flow_path=_PROJECT / "research" / "fund_flow_live.json",
        fund_flow_loader=_load_fund_flow_snapshot,
    )
    # Phase 0's single, source-labelled data entrypoint.  Existing legacy
    # endpoints remain intact while every new consumer uses /api/data/*.
    app.config["QUANT_DATA_SERVICE"] = QuantDataService(
        quote_provider=app.config["QUOTE_PROVIDER"],
        candle_provider=app.config["CANDLE_PROVIDER"],
        fund_flow_path=_PROJECT / "research" / "fund_flow_live.json",
        fund_flow_loader=_load_fund_flow_snapshot,
        fundamental_path=(
            _PROJECT / "research" / "data" / "v150_sse_xbrl_roe"
            / "sse_annual_roe_2018_2025.csv"
        ),
        fundamental_audit_path=(
            _PROJECT / "reports" / "v150_sse_xbrl_roe_data_epoch_audit.json"
        ),
    )
    app.register_blueprint(create_data_blueprint())

    @app.get(
        "/static/vendor/lightweight-charts/"
        "lightweight-charts.standalone.production.js"
    )
    def lightweight_charts_asset():
        """Serve the vendored chart runtime; the research pages need no CDN."""
        return send_file(
            _LIGHTWEIGHT_CHARTS_ASSET,
            mimetype="application/javascript",
            conditional=True,
        )

    # ── 页面路由 ──────────────────────────────
    @app.route("/login", methods=["GET", "POST"])
    def remote_login_page():
        if not app.config["REMOTE_MODE"]:
            return redirect("/") if request.method == "GET" else ("", 404)

        configured = os.environ.get("OLYMPUS_REMOTE_AUTH_TOKEN", "")
        destination = safe_next(
            request.form.get("next") if request.method == "POST"
            else request.args.get("next")
        )
        if request.method == "GET" and valid_session(
            configured, request.cookies.get(AUTH_COOKIE)
        ):
            return redirect(destination)

        def render_login(error: str = ""):
            # Keep one pre-login token per browser so opening another tab does
            # not invalidate a still-visible form. The cookie remains host-only,
            # HttpOnly, Secure, SameSite=Strict and expires after ten minutes.
            csrf = request.cookies.get(LOGIN_CSRF_COOKIE, "")
            if len(csrf) != 43 or not csrf.replace("-", "").replace("_", "").isalnum():
                csrf = new_login_csrf()
            response = app.make_response(render_template(
                "remote_login.html", csrf_token=csrf,
                next_path=destination, error=error,
            ))
            response.set_cookie(
                LOGIN_CSRF_COOKIE, csrf, max_age=600, path="/",
                secure=True, httponly=True, samesite="Strict",
            )
            response.headers["Cache-Control"] = "no-store"
            return response

        if request.method == "GET":
            return render_login()

        if request.headers.get("Sec-Fetch-Site", "").casefold() in {"cross-site", "none"}:
            return render_login("请刷新页面后重试")
        origin = (request.headers.get("Origin") or "").rstrip("/")
        if origin and origin != f"https://{request.host}":
            return render_login("请刷新页面后重试")
        if not valid_login_csrf(
            request.form.get("csrf_token"), request.cookies.get(LOGIN_CSRF_COOKIE)
        ):
            return render_login("请刷新页面后重试")
        if not (
            configured
            and hmac.compare_digest(request.form.get("username", ""), "regis")
            and hmac.compare_digest(request.form.get("password", ""), configured)
        ):
            return render_login("账号或密码不正确")

        response = redirect(destination)
        response.set_cookie(
            AUTH_COOKIE, issue_session(configured),
            max_age=SESSION_MAX_AGE_SECONDS, path="/",
            secure=True, httponly=True, samesite="Strict",
        )
        response.delete_cookie(LOGIN_CSRF_COOKIE, path="/", secure=True, httponly=True)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/next-session-plan")
    @app.get("/review")
    @app.get("/")
    def dashboard_page():
        return render_template("spa.html")

    @app.get("/api/research/sector-rotation")
    def sector_rotation_review_api():
        """Read only the same bounded sector evidence used by the planning UI."""
        from .next_session_sectors import build as next_session_sectors
        from .sector_rotation_review import build as sector_rotation_review
        preparation = next_session_sectors()
        response = jsonify(sector_rotation_review(preparation=preparation))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/research/emotion-pit-status")
    def emotion_pit_status_api():
        """Show only the current source-bound archive slot; no forecast/trade."""
        from .emotion_pit_archive import current_day_status

        root = app.config.get("RESEARCH_WORKBENCH_ROOT") or _PROJECT
        response = jsonify(current_day_status(root))
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/research/sector-mainline-origin")
    def sector_mainline_origin_status_api():
        """Read only the current first-write board research origin status."""
        from .sector_mainline_origin import current_status

        root = app.config.get("RESEARCH_WORKBENCH_ROOT") or _PROJECT
        response = jsonify(current_status(root=root))
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/research/sector-proxy-forecast")
    def sector_proxy_forecast_status_api():
        """Read the after-close proxy forecast status; never issue in HTTP."""
        from .sector_proxy_forecast import current_status

        root = app.config.get("RESEARCH_WORKBENCH_ROOT") or _PROJECT
        response = jsonify(current_status(root=root))
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/research/sector-proxy-forward-score")
    def sector_proxy_forward_score_status_api():
        """Read saved paired forecast scores; never rebuild labels in HTTP."""
        from .sector_proxy_forward_score import current_status

        root = app.config.get("RESEARCH_WORKBENCH_ROOT") or _PROJECT
        response = jsonify(current_status(root=root))
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/research/sector-first-write-eval")
    def sector_first_write_eval_status_api():
        """Read a bounded offline research receipt; never start a backtest here."""
        from .sector_first_write_eval import current_status

        root = app.config.get("RESEARCH_WORKBENCH_ROOT") or _PROJECT
        response = jsonify(current_status(root=root))
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/research/sector-peer-continuation")
    def sector_peer_continuation_status_api():
        """Only read the offline supplier-peer candidate status, not its rows."""
        from .sector_peer_continuation import current_status

        root = app.config.get("RESEARCH_WORKBENCH_ROOT") or _PROJECT
        response = jsonify(current_status(root=root))
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/research/sector-direction-audit")
    def sector_direction_audit_status_api():
        """Read a saved paired-direction diagnostic; never start evaluation."""
        from .sector_direction_audit import current_status

        root = app.config.get("RESEARCH_WORKBENCH_ROOT") or _PROJECT
        response = jsonify(current_status(root=root))
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/research/mainline-archive-integrity")
    def mainline_archive_integrity_api():
        """Bounded byte-integrity inventory of a retired strategy, never P&L."""
        from .mainline_archive_audit import audit

        root = app.config.get("RESEARCH_WORKBENCH_ROOT") or _PROJECT
        response = jsonify(audit(root))
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/research/local-sector-probe")
    def local_sector_probe_status_api():
        """Read ignored local forward receipts; do not trigger a supplier call."""
        from .local_sector_probe import current_status

        root = app.config.get("RESEARCH_WORKBENCH_ROOT") or _PROJECT
        response = jsonify(current_status(root=root))
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/research/local-sector-close")
    def local_sector_close_status_api():
        """Read local after-close evidence; HTTP never fetches or issues forecasts."""
        from .local_sector_close import current_status

        root = app.config.get("RESEARCH_WORKBENCH_ROOT") or _PROJECT
        response = jsonify(current_status(root=root))
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/research/local-forward-chain")
    def local_forward_chain_status_api():
        """Read local lineage only; HTTP cannot register, acquire or issue it."""
        from .local_forward_chain import current_status

        root = app.config.get("RESEARCH_WORKBENCH_ROOT") or _PROJECT
        response = jsonify(current_status(root=root))
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/research/project-inputs")
    def project_input_status_api():
        """Read local recovery gaps; never create a DB, acquire data or train."""
        from .project_input_recovery import audit

        root = app.config.get("RESEARCH_WORKBENCH_ROOT") or _PROJECT
        response = jsonify(audit(root))
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/research/mainline-bandit-shadow")
    def mainline_bandit_shadow_status_api():
        """Expose readiness only; never train or publish a policy via HTTP."""
        from .mainline_bandit_shadow import current_status

        root = app.config.get("RESEARCH_WORKBENCH_ROOT") or _PROJECT
        response = jsonify(current_status(root=root))
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/research/mainline-quote-pilot")
    def mainline_quote_pilot_status_api():
        """Read an ignored, rechecked pilot receipt; never fetch quotes via HTTP."""
        from .mainline_quote_pilot import current_status

        root = app.config.get("RESEARCH_WORKBENCH_ROOT") or _PROJECT
        response = jsonify(current_status(root=root))
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/research/official-listing-inventory")
    def official_listing_inventory_status_api():
        """Read saved official-site observations; never fetch a listing via HTTP."""
        from .official_listing_inventory import current_status

        root = app.config.get("RESEARCH_WORKBENCH_ROOT") or _PROJECT
        response = jsonify(current_status(root=root))
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/decision-v2")
    def decision_v2_page():
        """Parallel, read-only decision workspace; the existing home remains default."""
        return render_template("decision_v2.html")

    @app.get("/signals")
    def signals_page():
        return render_template("spa.html")

    @app.get("/positions")
    def positions_page():
        return render_template("positions.html")

    @app.get("/strategy-lab")
    def strategy_lab_page():
        # P5：专门策略研发工作区（独立页），与 SPA 内嵌 lab 同一后端/注册表/账本。
        return render_template("strategy_lab.html")

    @app.get("/stock")
    def stock_page():
        code = request.args.get("code", "")
        return render_template("stock.html", code=code)

    @app.get("/company/<code>")
    def company_page(code):
        code = (code or "").strip()
        if not re.fullmatch(r"\d{6}", code or ""):
            return render_template("spa.html")
        return render_template("company.html", code=code)

    @app.get("/fund-flow")
    def fund_flow_page():
        return render_template("spa.html")

    @app.get("/scanner")
    def scanner_page():
        return render_template("spa.html")

    @app.get("/backtest")
    def backtest_page():
        # P5 整改（2026-08-14）：旧回测中心界面移除，统一指向 /strategy-lab（策略开发+
        # 科学回测中心）。旧 /api/backtest/* 接口保持可用（因子表现/历史实验留痕）。
        return redirect(url_for("strategy_lab_page"))

    @app.get("/risk")
    def risk_page():
        return render_template("spa.html")

    @app.get("/risk/mapping-review")
    def mapping_review_page():
        return render_template("mapping_review.html")

    @app.get("/timing")
    def timing_page():
        # 2026-08-14：独立「时机研究」页并入系统（时机数据在仪表盘 AI看涨/持仓动作 与
        # 个股工作台「实时机会观察」中承载），/timing 旧深链重定向到仪表盘。
        return redirect(url_for("dashboard_page"))

    @app.get("/global-radar")
    def global_radar_page():
        return render_template("spa.html")

    @app.get("/market-panorama")
    def market_panorama_page():
        return render_template("spa.html")

    @app.get("/factor-system")
    def factor_system_page():
        return render_template("factor_system.html")

    # ── API: 个股投研聚合（原 /workstation，已并入 /stock 页）──
    @app.get("/api/stock/<code>/intelligence")
    def stock_intelligence_api(code: str):
        """个股投研聚合：行情/七维画像/综合评分/一票否决/9段式总结.

        数据全部真实带来源；失败维度标 degraded/数据不足。仅展示不自动下单。
        """
        if not CODE_PATTERN.match(code or ""):
            return jsonify({"error": "无效代码", "code": code}), 400
        from .workstation import build_workstation, serialize_candles
        try:
            # 行情（实时 Provider，缓存 30s）
            quote = None
            qs = app.config["QUOTE_PROVIDER"].get_quotes([code])
            if qs:
                quote = qs.get(code) or next(iter(qs.values()), None)
            # 日 K（主图）
            candles = None
            try:
                candles = app.config["CANDLE_PROVIDER"].get_candles(code, 220, interval="1d")
            except Exception:
                candles = None
            # 公司档案只读缓存；刷新必须显式 POST，GET 不启动后台任务。
            from .company_profile import get_cached_summary
            quote_summary = None
            if quote is not None:
                quote_summary = {
                    "price": getattr(quote, "price", None),
                    "prev_close": getattr(quote, "previous_close", None),
                    "change_pct": getattr(quote, "change_pct", None),
                    "amount": getattr(quote, "amount", None),
                    "name": getattr(quote, "name", None),
                    "data_time": getattr(quote, "data_time", None),
                    "exchange_data_time": getattr(
                        quote, "exchange_data_time", None,
                    ),
                    "freshness": getattr(quote, "freshness", None),
                    "source": getattr(quote, "source", None),
                }
            profile = get_cached_summary(code, quote.name if quote else "", quote_summary)
            if profile is None:
                profile = {
                    "ready": False,
                    "status": "missing",
                    "generated_at": None,
                    "data_sources": {},
                    "note": "公司档案缓存尚未生成；本轮只展示可核验的归档与行情维度",
                    "refresh_endpoint": f"/api/company-profile/{code}/refresh",
                }
            elif _company_profile_needs_refresh(
                profile, include_supplementary=True,
            ):
                profile = {**profile, "refresh_endpoint": f"/api/company-profile/{code}/refresh"}
            # 质量评分只使用本机审计且已完成的日 K；形成中 K 仍可由图表接口展示，
            # 但不能进入策略、风险或统一评分。
            quality_context = _quality_context(code)
            quality_candles = quality_context.get("candles") or []
            snap = quality_context.get("technical_snap")
            display_observations = _stock_display_observations(code, candles)
            # 资金流（含快照元数据/失败原因：缺失≠归零，页面显示健康度而非静默横杠）
            from .timing_engine import load_fund_flow_snapshot
            ff_snapshot = load_fund_flow_snapshot(code)
            net = ff_snapshot.get("net_yi")
            turnover = ff_snapshot.get("turnover")
            fund = {
                "net_yi": net,
                "normalized_net_yi": ff_snapshot.get("normalized_net_yi"),
                "raw_net_yi": ff_snapshot.get("raw_net_yi"),
                "turnover": turnover,
                "decision_eligible": ff_snapshot.get("decision_eligible") is True,
                "as_of": ff_snapshot.get("ts") or ff_snapshot.get("date"),
            }
            fund["ff_snapshot"] = {
                "ts": ff_snapshot.get("ts"),
                "date": ff_snapshot.get("date"),
                "snapshot_only": True,
                "scale": FLOW_MAGNITUDE_SCALE,
                "note": ff_snapshot.get("note"),
                "reason": ff_snapshot.get("reason"),
            }
            # 量化时机：与实时机会观察/今日机会同一引擎（_timing_signals_scoring 共享缓存）
            # 同一门禁（_gated_recommendation），保证个股研究与全站推荐同步。
            # 股票质量分固定使用中性时机基线；实时盘中条件只控制入场复核，
            # 避免同一股票因页面打开时点不同而改写质量评级。
            account_access = request.args.get("account_access", "true").lower() != "false"
            timing_block = _stock_timing_block(
                code, quote, net, start_gate_refresh=False, account_access=account_access,
            )
            payload = build_workstation(
                code,
                quote_obj=quote,
                candles_daily=(
                    serialize_candles(quality_candles) if quality_candles else None
                ),
                profile=profile,
                fund=fund,
                snap=snap,
                timing_score=_QUALITY_TIMING_BASELINE,
                display_observations=display_observations,
            )
            if isinstance(payload.get("composite"), dict):
                payload["composite"]["timing_basis"] = (
                    "neutral_50_live_condition_match_used_only_for_entry"
                )
                if isinstance(timing_block, dict):
                    timing_block["unified_composite_score"] = (
                        payload["composite"].get("composite")
                    )
                payload["composite"].update(
                    _public_research_composite(payload["composite"])
                )
            payload["timing"] = timing_block
            payload["account_access"] = account_access
            payload["scoring_standard"] = copy.deepcopy(_SCORING_STANDARD)
            return jsonify({"ok": True, "data": payload})
        except Exception as exc:
            return internal_error_response(
                app, "个股投研聚合暂不可用", exc, extra={"code": code},
            )

    # ── API: 因子挖掘系统（双区状态 + 注册中心）──
    register_factor_system_routes(
        app,
        research_lock_state=lambda: user_strategy_research_lock_state(),
    )

    # ── API: 健康检查 ─────────────────────────
    def _readiness_health() -> dict[str, object]:
        checks: dict[str, object] = {}
        try:
            trade_capacity()
            checks["timing_database"] = {"ready": True}
        except Exception as exc:
            checks["timing_database"] = {
                "ready": False, "error_type": type(exc).__name__,
            }
        quote_provider = app.config.get("QUOTE_PROVIDER")
        quote_health = (
            quote_provider.health_snapshot()
            if quote_provider is not None and hasattr(quote_provider, "health_snapshot")
            else {}
        )
        quote_age = audited_trading_day_age(
            quote_health.get("last_exchange_data_at")
            or quote_health.get("last_success_at") or "",
            allow_current_session=True,
        )
        checks["quote_provider"] = {
            "ready": bool(
                quote_health.get("last_success_at")
                and quote_age.get("status") == "fresh"
            ),
            "trading_day_age": quote_age,
            **quote_health,
        }
        checks["provider_call_guard"] = _bounded_health(
            lock=_BOUNDED_CALL_CIRCUIT_LOCK,
            timed_out=_BOUNDED_CALL_TIMED_OUT,
            telemetry=_BOUNDED_CALL_TELEMETRY,
        )
        checks["provider_native_timeouts"] = native_timeout_health(
            quote_provider, app.config.get("CANDLE_PROVIDER"),
        )
        ready = all(bool((value or {}).get("ready")) for value in checks.values())
        payload = {
            "status": "ready" if ready else "not_ready", "ready": ready,
            "checks": checks, **capability_readiness(
                operational_ready=ready,
                research_product_ready=ready,
                # This endpoint checks infrastructure only.  Formal strategy
                # signal readiness depends on the data-health snapshot and is
                # intentionally evaluated by the aggregate health endpoint.
                trade_signal_product_ready=None,
            ),
        }
        payload.update({
            "manual_decision_alerts_ready": ready,
            "manual_decision_alerts_ready_scope": "operational_infrastructure_only",
            "manual_decision_alerts_enabled": (
                os.environ.get("OLYMPUS_MANUAL_REAL_MONEY_MODE") == "1"
            ),
            "manual_decision_account_plan_ready": None,
            "manual_decision_account_plan_status_endpoint": "/api/opportunities",
            "production_strategy_signals_ready": None,
            "trade_signal_scope": "not_evaluated_by_readiness_endpoint",
        })
        return payload

    def _data_health() -> dict[str, object]:
        signal_error = None
        try:
            signal_snapshot = _safe_live_signal_snapshot()
        except Exception as exc:
            signal_snapshot = {}
            signal_error = type(exc).__name__
        signal_current = bool(
            signal_snapshot and not signal_snapshot.get("stale")
            and signal_snapshot.get("generated_at")
        )
        fund_path = _PROJECT / "research" / "fund_flow_live.json"
        fund_ready = False
        fund_as_of = None
        try:
            fund_doc = json.loads(fund_path.read_text(encoding="utf-8"))
            fund_as_of = fund_doc.get("ts") or fund_doc.get("date")
            fund_freshness = _fund_flow_freshness(fund_as_of)
            fund_ready = bool(
                fund_as_of
                and isinstance(fund_doc.get("stocks"), dict)
                and (fund_freshness.get("trading_day_age") or {}).get("status")
                == "fresh"
                and fund_freshness.get("state") in {
                    "fresh", "market_break", "market_closed",
                }
            )
        except (OSError, json.JSONDecodeError, TypeError):
            fund_freshness = {"state": "invalid", "label": "快照读取失败"}
        market_intelligence = app.config.get("MARKET_INTELLIGENCE")
        intelligence_health = (
            market_intelligence.health_snapshot()
            if market_intelligence is not None and hasattr(market_intelligence, "health_snapshot")
            else {"healthy": False, "worker_alive": False, "global_status": "missing"}
        )
        # A missing production strategy is a capability state, not a service
        # outage. Manual opportunity monitoring remains healthy when its data
        # dependencies are current, even while production signals are absent.
        production_signal_ready = bool(
            signal_current and signal_snapshot.get("decision_eligible") is True
        )
        # Fund flow is one optional scoring dimension. A stale or incomplete
        # third-party scrape remains excluded from scores, but must not turn
        # otherwise current quotes, market state and monitoring into a 503.
        healthy = bool(intelligence_health.get("healthy"))
        optional_degradations = []
        if not fund_ready:
            optional_degradations.append("fund_flow")
        if not production_signal_ready:
            optional_degradations.append("production_strategy_signals")
        return {
            "status": "healthy" if healthy else "degraded",
            "healthy": healthy,
            "production_strategy_signals_ready": production_signal_ready,
            "optional_degradations": optional_degradations,
            "checks": {
                "live_signals": {
                    "healthy": signal_current,
                    "decision_eligible": signal_snapshot.get("decision_eligible") is True,
                    "as_of": signal_snapshot.get("generated_at") if signal_snapshot else None,
                    "reasons": (
                        signal_snapshot.get("zero_signal_reasons", [])
                        if signal_snapshot else ["无快照"]
                    ),
                    "error_type": signal_error,
                },
                "fund_flow": {
                    "healthy": fund_ready, "as_of": fund_as_of,
                    "freshness": fund_freshness,
                },
                "market_intelligence": intelligence_health,
            },
        }

    def _monitoring_health() -> dict[str, object]:
        """Report server-side alert monitoring separately from browser delivery."""
        group = app.extensions.get("background_threads")
        thread_health = (
            group.health_snapshot()
            if group is not None and hasattr(group, "health_snapshot")
            else {"stopping": False, "workers": {}, "alive_count": 0,
                  "worker_count": 0}
        )
        workers = thread_health.get("workers") or {}
        configured = bool(app.extensions.get("background_monitoring_enabled"))
        supervisor_alive = bool(workers.get("opportunities-refresher"))
        now = _time.time()
        session = _audited_market_session()
        session_open = session.get("is_open") is True
        current_source_identity = _full_market_snapshot_identity()
        with _OPP_STATE["lock"]:
            source_changed = _opportunity_full_market_source_changed(
                _OPP_STATE.get("result"), current_source_identity,
            )
            freshness = _opportunity_snapshot_freshness(
                _OPP_STATE.get("result"), _OPP_STATE.get("ts"),
                now=now, session_open=session_open,
            )
            worker_running = bool(_OPP_STATE.get("running"))
            last_error = bool(_OPP_STATE.get("last_error"))
        has_snapshot = freshness["has_snapshot"]
        snapshot_age = freshness["age_s"]
        snapshot_current = bool(freshness["current"] and not source_changed)
        ready_for_session = bool(
            configured and supervisor_alive and not thread_health.get("stopping")
        )
        if last_error and not worker_running:
            snapshot_status = "refresh_failed"
        elif worker_running:
            snapshot_status = "refreshing"
        elif snapshot_current:
            snapshot_status = "current"
        elif has_snapshot:
            snapshot_status = "stale"
        else:
            snapshot_status = "missing"
        return {
            "configured": configured,
            "ready_for_session": ready_for_session,
            "active_now": bool(ready_for_session and session_open),
            "supervisor_alive": supervisor_alive,
            "snapshot_worker_running": worker_running,
            "snapshot_status": snapshot_status,
            "snapshot_age_s": (
                round(snapshot_age, 1) if snapshot_age is not None else None
            ),
            "snapshot_current_max_age_s": freshness["max_age_s"],
            "refresh_interval_s": _OPP_REFRESH_S,
            "last_refresh_failed": last_error,
            "market_session": session,
            "delivery_scope": "persistent_in_app_alert_ledger_manual_confirmation",
            "system_notification_permission": "browser_owned_not_server_observable",
            "thread_group": thread_health,
            "account_nav_freeze": _account_nav_freeze_health(),
        }

    @app.get("/api/health/live")
    def health_live():
        return jsonify({
            "status": "alive", "service": "Regis Oracle",
            "time": datetime.now(_SH_TZ).isoformat(),
        })

    @app.get("/api/health/ready")
    def health_ready():
        payload = _readiness_health()
        return jsonify(payload), 200 if payload["ready"] else 503

    @app.get("/api/health/data")
    def health_data():
        payload = _data_health()
        return jsonify(payload), 200 if payload["healthy"] else 503

    @app.get("/api/monitor/status")
    def monitor_status():
        payload = _monitoring_health()
        return jsonify(payload), 200 if payload["ready_for_session"] else 503

    @app.get("/api/health")
    def health():
        readiness = _readiness_health()
        data = _data_health()
        monitoring = _monitoring_health()
        research_ready = bool(readiness["ready"] and data["healthy"])
        monitor_ready = bool(
            not monitoring["configured"] or monitoring["ready_for_session"]
        )
        manual_alerts_ready = bool(
            readiness["ready"] and data["healthy"] and monitor_ready
        )
        production_signal_ready = bool(
            data.get("production_strategy_signals_ready") is True
        )
        readiness.update(capability_readiness(
            operational_ready=readiness["ready"],
            research_product_ready=research_ready,
            trade_signal_product_ready=production_signal_ready,
        ))
        readiness.update({
            "manual_decision_alerts_ready": manual_alerts_ready,
            "manual_decision_alerts_ready_scope": "operational_infrastructure_only",
            "manual_decision_alerts_enabled": (
                os.environ.get("OLYMPUS_MANUAL_REAL_MONEY_MODE") == "1"
            ),
            "manual_decision_account_plan_ready": None,
            "manual_decision_account_plan_status_endpoint": "/api/opportunities",
            "production_strategy_signals_ready": production_signal_ready,
            "trade_signal_scope": (
                "production_strategy_signals_for_manual_decision_"
                "not_broker_execution"
            ),
        })
        payload = {
            "status": "healthy" if readiness["ready"] and data["healthy"] else "degraded",
            "service": "Regis Oracle",
            "liveness": {"status": "alive"},
            "readiness": readiness,
            "data_health": data,
            "monitoring": monitoring,
            "time": datetime.now(_SH_TZ).isoformat(),
        }
        healthy = bool(readiness["ready"] and data["healthy"] and monitor_ready)
        payload["status"] = "healthy" if healthy else "degraded"
        return jsonify(payload), 200 if healthy else 503

    def _market_intelligence_response(payload: dict, status: int = 200):
        response = jsonify(payload)
        # The browser must ask the server for current health metadata rather
        # than presenting a browser cache as a live external-data snapshot.
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response, status

    # ── API: 全球市场冲击雷达 ─────────────────
    @app.get("/api/global/radar")
    def global_market_radar():
        return _market_intelligence_response(
            app.config["MARKET_INTELLIGENCE"].global_radar()
        )

    @app.get("/api/global/radar/events")
    def global_market_radar_events():
        payload = app.config["MARKET_INTELLIGENCE"].global_radar()
        raw_since = request.args.get("since")
        since = _parse_iso_timestamp(raw_since)
        if raw_since is not None and since is None:
            return _market_intelligence_response({
                "status": "invalid_request",
                "error": "since 必须是带时区的 ISO-8601 时间",
            }, 400)
        if since is not None:
            filtered = []
            for event in payload["events"]:
                timestamp = _parse_iso_timestamp(event.get("timestamp"))
                if timestamp is not None and timestamp >= since:
                    filtered.append(event)
            payload["events"] = filtered
        return _market_intelligence_response({
            "status": payload["status"],
            "server_time": payload["server_time"],
            "events": payload["events"],
            "health": payload["health"],
            "decision_policy": payload["decision_policy"],
        })

    @app.get("/api/global/radar/heatmap")
    def global_market_radar_heatmap():
        payload = app.config["MARKET_INTELLIGENCE"].global_radar()
        return _market_intelligence_response({
            "status": payload["status"],
            "server_time": payload["server_time"],
            "industry_heatmap": payload["industry_heatmap"],
            "health": payload["health"],
            "participates_in_decision": False,
        })

    # ── API: A 股市场全景 ─────────────────────
    @app.get("/api/market/panorama")
    def market_panorama():
        payload = app.config["MARKET_INTELLIGENCE"].panorama(
            request.args.get("dimension", "today")
        )
        return _market_intelligence_response(
            payload, 400 if payload.get("status") == "invalid_request" else 200
        )

    @app.get("/api/market/status")
    def market_panorama_status():
        # Session badges and the T+1 execution plan must not wait for the
        # descriptive panorama's network/data aggregation.  Full dimensions
        # remain available from /api/market/panorama.
        payload = {
            "server_time": datetime.now(_SH_TZ).isoformat(),
            "participates_in_decision": False,
        }
        payload["decision_calendar"] = trading_day_status(datetime.now(_SH_TZ))
        payload["session"] = _audited_market_session()
        return _market_intelligence_response(payload)

    @app.get("/api/market/emotion-profile")
    def market_emotion_profile():
        """Read the existing verified daily emotion context; never authorize a route."""
        from .emotion_system import public_context

        context = public_context(root=app.config.get("RESEARCH_WORKBENCH_ROOT"),
                                 include_dependent_context=False)
        response = jsonify({
            "schema": "market_emotion_profile.v1",
            "status": context["status"],
            "reason": context.get("reason"),
            "source": context.get("source"),
            "emotion": context.get("emotion"),
            "research_only": True,
            "production_eligible": False,
            "participates_in_decision": False,
        })
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/market/research-state")
    def market_research_state():
        """Expose only a verified immutable M_t input snapshot."""
        if request.headers.get("X-Regis-Account-Access") == "false":
            from .account_independent_market import historical_context
            try:
                data = historical_context(
                    load_base=_cached_latest_research_state,
                    latest_observation=latest_route_market_observation,
                    adapt_observation=adapt_market_observation,
                )
                response = jsonify(data)
            except (OSError, ValueError, TypeError, KeyError):
                response = jsonify({"status": "unavailable", "account_access": False,
                                    "research_only": True, "production_eligible": False,
                                    "generates_alert": False, "automatic_orders": False,
                                    "error": "历史研究状态读取或校验失败，可稍后重试。"})
                response.status_code = 503
            response.headers["Cache-Control"] = "no-store"
            return response
        payload = _cached_latest_research_state()
        # Keep the market center and the strategy route on the same verified
        # append-only daily observation.  The base research epoch remains
        # inspectable, but it must not make one page look several sessions
        # older than the decision route.
        payload["model_observation"] = latest_route_market_observation(
            payload.get("model_observation"),
        )
        route_observation = adapt_market_observation(
            payload["model_observation"],
            as_of=payload["model_observation"].get("trade_date"),
        )
        payload["decision_state"] = {
            "state": route_observation.get("state") or "unknown",
            "as_of": route_observation.get("as_of"),
            "status": route_observation.get("status") or "incomplete",
            "confidence": route_observation.get("confidence"),
            "confidence_semantics": route_observation.get(
                "confidence_semantics"
            ),
            "evidence": list(route_observation.get("evidence") or []),
            "contradictions": list(
                route_observation.get("contradictions") or []
            ),
            "model_confirmed_state": payload["model_observation"].get("state"),
            "model_raw_state": payload["model_observation"].get("raw_state"),
            "observation_source": payload["model_observation"].get("observation_source"),
            "applied_to_execution": False,
            "production_strategy": False,
            "semantics": (
                "与今日决策共用的保守研究路由状态；不是收益概率、"
                "不单独授权交易或放大真实仓位。"
            ),
        }
        trader_style = payload.get("trader_style_research")
        if isinstance(trader_style, dict):
            trader_style["distillation"] = load_trader_style_distillation()
        monitoring = _monitoring_health()
        with _OPP_STATE["lock"]:
            opportunity = copy.deepcopy(_OPP_STATE.get("result") or {})
            completed_at = float(_OPP_STATE.get("ts") or 0)
            running = bool(_OPP_STATE.get("running"))
            state_error = _OPP_STATE.get("last_error")
        with _FORWARD_CAPTURE_STATE["lock"]:
            capture_running = bool(_FORWARD_CAPTURE_STATE.get("running"))
            capture_error = _FORWARD_CAPTURE_STATE.get("last_error")
        runtime_path = app.config.get("FORWARD_CAPTURE_RUNTIME_STATUS_PATH")
        persisted_capture = (
            _read_json(Path(runtime_path)) if runtime_path else {}
        )
        h1 = opportunity.get("market_state_forward_capture") or {}
        h3 = opportunity.get("h3_execution_forward_capture") or {}
        route_input = opportunity.get("market_sector_route_forward_capture") or {}
        emotion_input = opportunity.get("intraday_emotion_forward_capture") or {}
        news_input = opportunity.get("market_news_forward_capture") or {}
        sector_quote_panel = (
            opportunity.get("sector_constituent_quote_panel_capture") or {}
        )
        cycle_completed_at = (
            datetime.fromtimestamp(completed_at, tz=_SH_TZ).isoformat(timespec="seconds")
            if completed_at > 0 else None
        )
        observed_now = datetime.now(_SH_TZ)
        schedule_status = _cached_forward_capture_schedule_status(
            now=observed_now, is_trading_day=_is_trading_day(observed_now),
        )
        relevant_slots = [
            row for row in schedule_status.get("slots") or []
            if row.get("phase") in {"window_open", "missed", "captured"}
        ]
        current_slot = relevant_slots[-1] if relevant_slots else {}
        complete_slots = [
            row for row in relevant_slots
            if row.get("artifact_integrity")
            and all(
                item.get("valid") is True
                for item in row["artifact_integrity"].values()
            )
        ]
        complete_capture_times = [
            str(item.get("captured_at"))
            for row in complete_slots
            for item in row["artifact_integrity"].values()
            if item.get("captured_at")
        ]
        missed_artifacts = {
            artifact
            for row in schedule_status.get("slots") or []
            if row.get("phase") == "missed"
            for artifact in row.get("missing_artifacts") or []
        }
        persisted_capture_relevant = bool(
            persisted_capture.get("decision_day") == observed_now.date().isoformat()
        )
        persisted_status = (
            str(persisted_capture.get("status") or "")
            if persisted_capture_relevant else ""
        )
        persisted_error = None
        if persisted_status == "running" and (
            persisted_capture.get("pid") != os.getpid() or not capture_running
        ):
            persisted_error = "forward_capture_interrupted_before_completion"
            persisted_status = "interrupted"
        elif persisted_status in {"blocked", "failed"}:
            persisted_error = str(
                persisted_capture.get("error") or "forward_capture_failed"
            )[:500]
        runtime_error = (
            str(state_error)[:500] if state_error else
            str(capture_error)[:500] if capture_error else
            persisted_error if persisted_error else
            str(h1.get("error") or h3.get("error") or route_input.get("error")
                or "")[:500] or None
        )
        supervisor_alive = monitoring.get("supervisor_alive") is True
        runtime_ready = bool(
            supervisor_alive and opportunity and not runtime_error
            and not missed_artifacts
        )
        panel_phase = str(sector_quote_panel.get("phase") or "")
        verified_panel = current_slot.get("sector_quote_panel") or {}
        if verified_panel.get("present") is True:
            panel_phase = str(verified_panel.get("phase") or "")
        if not panel_phase:
            panel_phase = (
                "error" if sector_quote_panel.get("status") in {
                    "error", "identity_invalid", "constituent_input_invalid",
                    "constituent_capture_incomplete", "panel_identity_invalid",
                    "protocol_slot_end_invalid", "outside_original_capture_slot",
                }
                else "collecting" if sector_quote_panel.get("active")
                else "awaiting_window"
            )
        panel_research_ready = bool(
            verified_panel.get("valid") is True
            and verified_panel.get("complete") is True
            and verified_panel.get("path_complete") is True
        )
        verified_panel_problems = list(verified_panel.get("errors") or [])
        if verified_panel.get("incomplete_reason"):
            verified_panel_problems.append(verified_panel["incomplete_reason"])
        current_core_review = _cached_latest_core_route_review(
            schedule_status, observed_at=observed_now,
        )
        current_slot_core_ready = current_core_review.get("valid") is True
        current_slot_strict_route_ready = any(
            row.get("phase") in {"window_open", "captured"}
            and row.get("artifact_integrity")
            and all(
                item.get("valid") is True
                for item in row["artifact_integrity"].values()
            )
            and (row.get("strict_forward_v2") or {}).get("valid") is True
            for row in schedule_status.get("slots") or []
        )
        persisted_results = persisted_capture.get("results") or {}
        current_release_results = {
            key: persisted_results[key]
            for key in _FORWARD_CAPTURE_RESULT_FIELDS
            if key in persisted_results
        }
        payload["capture_runtime"] = {
            "background_worker_alive": supervisor_alive,
            "snapshot_worker_running": running,
            "capture_worker_running": capture_running,
            "ready": runtime_ready,
            "ready_semantics": "worker_available_not_slot_or_strategy_ready",
            "current_slot_core_ready": current_slot_core_ready,
            "current_slot_strict_route_ready": current_slot_strict_route_ready,
            "current_slot_research_review": current_core_review,
            "sector_quote_panel": {
                "phase": panel_phase,
                "status": sector_quote_panel.get("status") or "no_active_panel",
                "next_slot": schedule_status.get("next_slot"),
                "active": int(sector_quote_panel.get("active") or 0),
                "captured": int(sector_quote_panel.get("captured") or 0),
                "closed": int(sector_quote_panel.get("closed") or 0),
                "path_complete": panel_research_ready,
                "panel_research_ready": panel_research_ready,
                "journal_verified": verified_panel.get("valid") is True,
                "journal_present": verified_panel.get("present") is True,
                "panel_snapshot_count": verified_panel.get("panel_snapshot_count"),
                "latest_coverage_pct": verified_panel.get("latest_coverage_pct"),
                "incomplete_reasons": (
                    sector_quote_panel.get("incomplete_reasons") or []
                ) + verified_panel_problems,
                "terminal_error": sector_quote_panel.get("terminal_error")
                or sector_quote_panel.get("error"),
                "required_for_current_trade_route": False,
                "purpose": "future_independent_playbook_input_only",
                "trade_action_authority": False,
            },
            "h1_status": h1.get("status") or "no_completed_cycle",
            "h3_status": h3.get("status") or "no_completed_cycle",
            "market_sector_route_input_status": (
                "missed_capture" if "market_sector" in missed_artifacts else
                route_input.get("status") or "no_completed_cycle"
            ),
            "market_sector_route_source_status": route_input.get(
                "source_capture_status"
            ),
            "intraday_emotion_input_status": (
                "missed_capture" if "intraday_emotion" in missed_artifacts else
                emotion_input.get("status") or "no_completed_cycle"
            ),
            "market_news_input_status": news_input.get("status") or "no_completed_cycle",
            "market_news_source_status": news_input.get("source_capture_status"),
            "market_news_eligible_for_emotion": (
                news_input.get("eligible_for_market_emotion") is True
            ),
            "last_cycle_completed_at": cycle_completed_at,
            "last_capture_success_at": (
                max(complete_capture_times) if complete_capture_times else None
            ),
            "last_error": runtime_error,
            "last_attempt_status": persisted_status or None,
            "last_attempt_started_at": persisted_capture.get("started_at"),
            "last_attempt_updated_at": persisted_capture.get("updated_at"),
            "last_attempt_pid": persisted_capture.get("pid"),
            # Historical journals remain immutable, but retired candidate and
            # execution stages must not reappear as current runtime capability.
            "last_attempt_results": current_release_results,
            "next_slot": (
                schedule_status.get("next_slot")
                or h1.get("forward_start") or h3.get("forward_start")
            ),
            "schedule": schedule_status,
            "missed_slots": schedule_status.get("missed_slots") or [],
            "missing_capture_artifacts": sorted(missed_artifacts),
        }
        payload["verified_paper_execution"] = _verified_execution_runtime_state(
            _VERIFIED_EXECUTION_CAPTURE_STATE, now=observed_now,
        )
        payload["verified_paper_execution"].update({
            "runtime_enabled": False,
            "status": "paused_until_independent_strategy_admission",
            "reason": (
                "旧执行器只读取已永久退役的战法包；等待独立入场策略绑定 strict V2 后再启用"
            ),
        })
        attention = (
            ((payload.get("trader_style_research") or {}).get("attention_crowding"))
        )
        if isinstance(attention, dict):
            attention["collector_ready"] = bool(
                attention.get("protocol_ready") is True and runtime_ready
            )
            attention["forward_capture_ready"] = bool(
                attention.get("protocol_ready") is True
                and current_slot
                and ((current_slot.get("artifact_integrity") or {}).get(
                    "attention_crowding"
                ) or {}).get("valid") is True
            )
            attention["forward_capture_slot_id"] = (
                current_slot.get("slot_id")
                if attention["forward_capture_ready"] else None
            )
            attention["runtime_status"] = h3.get("status") or "no_completed_cycle"
        return _market_intelligence_response(payload)
    @app.get("/api/research/private-fund-method-scope")
    def private_fund_method_scope():
        """Expose source-only research with every decision authority disabled."""
        return jsonify(load_private_fund_scope())

    def _strategy_route_input_identity() -> tuple:
        """Cheap cache key; evidence verification remains inside the builder."""
        try:
            ledger = read_account_ledger_facts()["ledger_identity"][
                "combined_sha256"
            ]
        except Exception:
            ledger = None
        with _OPP_STATE["lock"]:
            opportunity_revision = _OPP_STATE.get("ts")
        strict_dir = (
            _PROJECT
            / "research/data/system_route_joint_replay/v2/strict_forward_decisions"
        )
        strict_revision = []
        for path in sorted(strict_dir.glob("*.json")):
            try:
                stat = path.stat()
            except OSError:
                continue
            strict_revision.append((path.name, stat.st_mtime_ns, stat.st_size))
        core_route_dir = (
            _PROJECT
            / "research/data/market_sector_route_forward/v1/route_decisions"
        )
        core_route_revision = []
        for path in sorted(core_route_dir.glob("*.json"))[-4:]:
            try:
                stat = path.stat()
            except OSError:
                continue
            core_route_revision.append((path.name, stat.st_mtime_ns, stat.st_size))
        intelligence = app.config["MARKET_INTELLIGENCE"]
        with intelligence._lock:
            market_revision = str(intelligence._market_fetched_at or "")
            sector_revision = str(intelligence._sectors_fetched_at or "")
        session = _audited_market_session()
        session_revision = (
            session.get("status"), str(session.get("server_time") or "")[:10],
        )
        # Observation files are immutable. Notice new/changed published input
        # without parsing its research payload in the request thread.
        evidence_revision = []
        reports = _PROJECT / "reports"
        paths = [reports / name for name in (
            "current_market_calendar.json", "cloud_market_data.json",
            "cloud_market_emotion_progress.json",
        )]
        paths.extend(sorted((_PROJECT / "research/data/market_state_daily/v1/feature_daily_v2").glob("*.json"))[-2:])
        for name in ("cloud_sector_close", "cloud_sector_close_members", "cloud_sector_observations", "cloud_sector_constituents"):
            for directory in sorted((reports / name).glob("[0-9]*"))[-2:]:
                paths.extend(directory.glob("*.json"))
                paths.extend(directory.glob("*/*.json"))
        for path in sorted(paths):
            try:
                stat = path.stat()
                evidence_revision.append((str(path), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino))
            except FileNotFoundError:
                evidence_revision.append((str(path), None))
        return (
            ledger, opportunity_revision, tuple(strict_revision),
            tuple(core_route_revision),
            market_revision, sector_revision, session_revision, tuple(evidence_revision),
        )

    def _build_research_strategy_route():
        research = _cached_latest_research_state()
        research["model_observation"] = latest_route_market_observation(
            research.get("model_observation"),
        )
        try:
            intelligence = app.config["MARKET_INTELLIGENCE"]
            # A dashboard GET never starts external source I/O.  The market
            # worker owns refreshes; a missing cache is an explicit no-action
            # input, not a reason to block the request thread.
            with intelligence._lock:
                sectors = copy.deepcopy(intelligence._sectors_cache)
            if not isinstance(sectors, dict):
                sectors = {
                    "status": "unavailable",
                    "reason": "板块后台快照尚未完成；本次请求不等待外部数据源。",
                    "participates_in_decision": False,
                }
        except Exception as exc:
            sectors = {
                "status": "unavailable",
                "reason": f"板块观察不可用：{type(exc).__name__}",
                "participates_in_decision": False,
            }
        intelligence = app.config["MARKET_INTELLIGENCE"]
        session = _audited_market_session()
        with intelligence._lock:
            market_cache_ready = bool(
                intelligence._market_cache is not None
                and intelligence._market_fetched_at is not None
                and "market" not in intelligence._refresh_flights
            )
        today_market = (
            load_intraday_emotion_snapshot(intelligence)
            if session.get("is_open") is True and market_cache_ready
            else {
                "status": "unavailable",
                "reason": (
                    "当前非连续竞价时段，不读取盘中情绪。"
                    if session.get("is_open") is not True else
                    "盘中行情后台快照尚未完成；本次请求不等待外部数据源。"
                ),
            }
        )
        try:
            portfolio = _manual_portfolio_status(app.config["QUOTE_PROVIDER"])
            portfolio["market_session"] = _audited_market_session()
            portfolio["buy_window"] = get_buy_window()
        except Exception as exc:
            portfolio = {
                "cash_user_recorded": False,
                "valuation_status": "unavailable",
                "market_session": _audited_market_session(),
                "buy_window": get_buy_window(),
                "reason": f"账户事实不可用：{type(exc).__name__}",
            }
        with _OPP_STATE["lock"]:
            opportunity_context = copy.deepcopy(_OPP_STATE.get("result") or {})
        route_observed_at = datetime.now(_SH_TZ)
        route_schedule = _cached_forward_capture_schedule_status(
            now=route_observed_at,
            is_trading_day=_is_trading_day(route_observed_at),
        )
        current_core_review = _cached_latest_core_route_review(
            route_schedule, observed_at=route_observed_at,
        )
        candidate_rows: list[dict] = []
        seen_codes: set[str] = set()
        for bucket_name in ("items", "watch_items"):
            for raw in opportunity_context.get(bucket_name) or []:
                if not isinstance(raw, dict):
                    continue
                code = str(raw.get("code") or "").strip()
                if len(code) != 6 or not code.isdigit() or code in seen_codes:
                    continue
                seen_codes.add(code)
                row = copy.deepcopy(raw)
                row["_workspace_bucket"] = bucket_name
                candidate_rows.append(row)
                if len(candidate_rows) >= 12:
                    break
            if len(candidate_rows) >= 12:
                break
        payload = build_research_route_payload(
            research, sectors, portfolio, today_market,
            candidates=candidate_rows,
            opportunity_context=opportunity_context,
            current_core_review=current_core_review,
        )
        return payload

    from .research_route_snapshot import ResearchRouteSnapshot

    def _produce_research_route():
        from .research_release_guard import source_lease
        with source_lease(required=app.config["REMOTE_MODE"]) as lease, app.app_context():
            if lease is None:
                raise RuntimeError("publication_wait")
            return _build_research_strategy_route()

    route_snapshot = ResearchRouteSnapshot(_produce_research_route, _strategy_route_input_identity)
    app.extensions["research_route_snapshot"] = route_snapshot
    from .playbook_candidate_scan import attach as attach_playbook_scan
    playbook_scan = attach_playbook_scan(app)

    @app.get("/api/strategy-route/research")
    def research_strategy_route():
        """Read the background projection; cold GETs never start or await work."""
        view = request.args.get("view", "full")
        if view not in {"full", "dashboard"}:
            return jsonify({"error": "view 仅支持 full/dashboard"}), 400
        if request.args.get("account_access", "true").lower() == "false":
            # The cached projection includes ledger identity and account plans.
            # Do not read it and redact afterwards: even its identity reads cash.
            return jsonify({
                "account_access": False, "research_only": True,
                "execution_allowed": False,
                "account_plan": {"status": "disabled", "reason": "账户读取关闭"},
                "research_snapshot": {"current": False, "status": "account_access_disabled"},
            })
        payload = route_snapshot.read()
        metadata = payload["research_snapshot"]
        if view == "dashboard" and metadata["current"]:
            payload = dashboard_research_route_payload(payload)
            payload["research_snapshot"] = metadata
        return jsonify(payload), 200 if metadata["current"] else 202

    @app.get("/api/market/alerts")
    def market_panorama_alerts():
        return _market_intelligence_response(
            app.config["MARKET_INTELLIGENCE"].alerts()
        )

    @app.get("/api/market/sectors")
    def market_panorama_sectors():
        if request.args.get("force") is not None:
            return _market_intelligence_response({
                "status": "invalid_request", "error": "GET 不支持强制抓源；请读取当前快照",
            }, 400)
        return _market_intelligence_response(
            app.config["MARKET_INTELLIGENCE"].market_sectors()
        )

    @app.get("/api/news/live")
    def market_panorama_news():
        if request.args.get("force") is not None:
            return _market_intelligence_response({
                "status": "invalid_request", "error": "GET 不支持强制抓源；请读取当前快照",
            }, 400)
        return _market_intelligence_response(
            app.config["MARKET_INTELLIGENCE"].news_live()
        )

    @app.get("/api/market/index-spark")
    def market_panorama_index_sparks():
        if request.args.get("force") is not None:
            return _market_intelligence_response({
                "status": "invalid_request", "error": "GET 不支持强制抓源；请读取当前快照",
            }, 400)
        return _market_intelligence_response(
            app.config["MARKET_INTELLIGENCE"].index_sparks()
        )

    # ── API: 市场概况 ─────────────────────────
    @app.get("/api/market")
    def market_overview():
        try:
            quotes = app.config["QUOTE_PROVIDER"].get_quotes(list(MARKET_INDEX_CODES))
        except MarketDataUnavailable as exc:
            return internal_error_response(
                app, "市场指数行情不可用", exc, status=503,
                extra={"data_status": "unavailable"},
            )
        except Exception:
            app.logger.warning("市场指数行情提供方异常", exc_info=True)
            return jsonify({
                "error": "市场指数行情不可用",
                "data_status": "unavailable",
                "regime": "unavailable",
                "regime_label": "市场状态未知",
            }), 503

        indexes = []
        for code in MARKET_INDEX_CODES:
            q = (quotes or {}).get(code)
            if q is None:
                continue
            chg = _finite_number(getattr(q, "change_pct", None))
            # -100 是闭市哨兵值
            if chg is not None and chg <= -99:
                chg = None
            price = _finite_number(getattr(q, "price", None))
            indexes.append({
                "code": code,
                "name": INDEX_LABELS.get(code, getattr(q, "name", code)),
                "price": price if price is not None and price > 0 else None,
                "change_pct": round(chg, 2) if chg is not None else None,
                "freshness": getattr(q, "freshness", "unknown"),
            })

        # ── v3.1: 三维市场情绪（指数方向 + 市场宽度 + 涨跌比） ──
        # 文艺复兴标准：单一维度不足以判定市场状态
        changes = [ix["change_pct"] for ix in indexes if ix["change_pct"] is not None]
        avg_chg = sum(changes) / len(changes) if changes else None
        all_up = bool(changes) and all(c > 0 for c in changes)
        all_down = bool(changes) and all(c < 0 for c in changes)

        # 读取市场宽度（涨跌比）——与 /api/market/breadth 共用同一函数，防止口径漂移
        breadth_up_ratio = None
        breadth_flat = None
        breadth_status = "unavailable"
        try:
            ff_path = _PROJECT / "research" / "fund_flow_live.json"
            if ff_path.exists():
                ff_records, _ = _load_fund_flow_snapshot(ff_path)
                _breadth = _fund_flow_breadth(ff_records)
                if _breadth.get("total", 0) > 0:
                    breadth_up_ratio = _breadth["up_ratio"]
                    breadth_flat = _breadth["flat"]
                    breadth_status = "available"
        except Exception:
            app.logger.warning("市场宽度快照读取异常", exc_info=True)
            breadth_status = "error"

        # 三维投票判定（优先级：宽度 > 指数方向 > 分化）
        if not changes:
            regime = "unavailable"
            regime_label = "市场涨跌数据不可用"
        elif breadth_up_ratio is not None and breadth_up_ratio >= 60 and avg_chg >= 0.5:
            regime = "strong"
            regime_label = "市场普涨"
        elif breadth_up_ratio is not None and breadth_up_ratio <= 25 and avg_chg <= -0.5:
            regime = "weak"
            regime_label = "市场普跌"
        elif breadth_up_ratio is not None and breadth_up_ratio < 40 and avg_chg > 0:
            regime = "divergent"
            regime_label = "指数涨但个股弱"  # 权重股拉指数，大部分个股在跌——风险信号
        elif breadth_up_ratio is not None and breadth_up_ratio > 55 and avg_chg < 0:
            regime = "divergent"
            regime_label = "指数跌但个股稳"
        elif all_up and avg_chg >= 1.0:
            regime = "strong"
            regime_label = "指数普涨"
        elif all_down and avg_chg <= -1.0:
            regime = "weak"
            regime_label = "指数普跌"
        elif any(c > 0 for c in changes) and any(c < 0 for c in changes):
            regime = "divergent"
            regime_label = "指数分化"
        else:
            regime = "range"
            regime_label = "窄幅震荡"

        payload = {
            "indexes": indexes,
            "regime": regime,
            "regime_label": regime_label,
            "breadth_up_ratio": breadth_up_ratio,
            "breadth_flat": breadth_flat,
            "breadth_status": breadth_status,
            "average_index_change": round(avg_chg, 2) if avg_chg is not None else None,
            "data_status": "available" if changes else "unavailable",
            "time": datetime.now().strftime("%H:%M:%S"),
        }
        return jsonify(payload), 200 if changes else 503

    # ── API: 独立策略开发区 ──────────────────
    @app.get("/api/strategy-lab")
    def strategy_lab_api():
        view = request.args.get("view", "full")
        if view not in {"full", "compact", "summary"}:
            return jsonify({
                "ok": False,
                "error": "view 仅支持 full、compact、summary",
                "code": "invalid_view",
            }), 400
        account_access = request.args.get("account_access", "true").lower()
        if account_access not in {"true", "false"}:
            return jsonify({"ok": False, "error": "account_access 仅支持 true/false", "code": "invalid_account_access"}), 400
        if account_access == "false":
            # Research metadata is independent of personal account identity,
            # the account-derived gate cache and its background refresher.
            state = dict(user_strategy_workbench_state(view=view))
            state.update({
                "account_access": False, "research_only": True,
                "decision_gate": {
                    "account_access": False, "research_only": True,
                    "recommendation_eligible": False, "execution_allowed": False,
                    "generates_alert": False, "automatic_orders": False,
                    "requested_initial_capital_yuan": 150000,
                    "capital_role": "requested_research_budget_not_account_balance",
                    "historical_results_rebased": False,
                    "reason": "账户读取关闭；研究预算15万元。历史报告保留原冻结口径，当前不授予交易资格。",
                },
            })
            response = jsonify(state)
            response.headers["Cache-Control"] = "no-store, max-age=0"
            # Missing execution inputs do not make available metadata unreadable.
            return response, 200
        if view != "full":
            state = user_strategy_workbench_state(view="summary")
            payload = {
                "available": bool(state.get("available")),
                "view": view,
                "source_error": state.get("source_error"),
                "workflow": state.get("workflow"),
                "decision_gate": _compact_decision_gate(
                    _decision_gate_status_fast(),
                ),
            }
            response = jsonify(payload)
            response.headers["Cache-Control"] = "no-store, max-age=0"
            return response, (200 if payload["available"] else 503)
        evidence = _strategy_lab_evidence()
        evidence["decision_gate"] = _decision_gate_status()
        return jsonify(evidence), (200 if evidence["available"] else 503)

    @app.get("/api/strategy-lab/baseline")
    def strategy_lab_baseline_api():
        """年化目标 20% + 宽基基线（beta 腿）——用户目标的结构性答案（2026-08-14）。

        历史确认（2024-07-01~2025-12-31，THS 成本口径）：市值加权全市场买持
        （ETF 模拟）净年化 +39.5%、等权月度再平衡 +48.6%——宽基 beta 本身已超
        20% 目标线。无 PASS 因子时的默认持仓 = 此宽基基线（不空仓），因子只在
        净成本闸门通过且净超额为正时替换/叠加。诚实边界：不承诺每年达标。
        """
        try:
            import factor_system.backtest as fsbt
            from factor_system.compose import load_thin_panel as _fs_thin
            from factor_system.data import split_windows as _fs_split
            panel = _fs_thin()
            _, historical_dates = _fs_split(panel.dates)
            all_dates = list(panel.dates)

            def _summ(dates_):
                out = {}
                for w in ("float_mv", "equal"):
                    b = fsbt.run_market_baseline(panel, dates_, weight=w)
                    if b.get("ok"):
                        out[w] = {
                            "annual_return_pct": round(b["annual_return"] * 100, 2),
                            "total_return_pct": round(b["total_return"] * 100, 2),
                            "max_drawdown_pct": round(b["max_drawdown"] * 100, 2),
                            "n_days": b["n_days"],
                            "target_met": b["target_met"],
                        }
                return out

            # 历史确认窗口与全窗口（2021-01~2025-12，含熊市）双口径
            baselines = _summ(historical_dates)
            full = _summ(all_dates)
            # 逐自然年（市值加权买持口径）——诚实展示 beta 的年际波动
            fm_full = fsbt._baseline_daily(panel, all_dates, weight="float_mv")
            yearly = {}
            for year in range(all_dates[0].year, all_dates[-1].year + 1):
                sub = fm_full.loc[[d for d in fm_full.index if d.year == year]]
                if len(sub) >= 30:
                    yearly[str(year)] = round(float((1.0 + sub).prod() ** (252.0 / len(sub)) - 1.0) * 100, 2)
            # 跨周期缺口：20% 目标 − 全周期 beta = 需要真 alpha 每年叠加多少
            beta_full = (full.get("float_mv") or {}).get("annual_return_pct")
            alpha_gap = round(20.0 - beta_full, 1) if beta_full is not None else None
            return jsonify({
                "ok": True,
                "target_annual_pct": 20.0,
                "as_of": str(historical_dates[-1].date()) if historical_dates else None,
                "window_label": {"start": str(historical_dates[0].date()), "end": str(historical_dates[-1].date())},
                "window_role": "historical_confirmation",
                "baselines": baselines,                 # 历史确认窗口
                "full_window": full,                    # 全周期 2021-01~2025-12（含熊市）
                "yearly": yearly,                       # 逐自然年净年化
                "alpha_gap_pct": alpha_gap,             # 20% − 全周期 beta
            })
        except Exception as exc:
            return internal_error_response(app, "宽基基线计算失败", exc)

    @app.get("/api/cash-floor/comparison")
    def cash_floor_comparison_api():
        report = _cash_floor_cross_product_report(include_live=True)
        comparison = report.get("live_comparison") or {}
        return jsonify(comparison), (200 if comparison.get("rows") else 503)

    @app.post("/api/strategy-lab/paper-forward/refresh")
    def refresh_paper_forward_api():
        v221, v223 = _stability_80_20_reports()
        if not _stability_80_20_passed(v221, v223):
            return jsonify({
                "ok": False,
                "error": (
                    "V221/V223尚未通过纸面前向准入；未写入任何刷新回执。"
                ),
            }), 409
        if not _PAPER_FORWARD_REFRESH_LOCK.acquire(blocking=False):
            return jsonify({
                "ok": False,
                "error": "正式前向刷新正在运行，请等待本次手动刷新完成。",
            }), 409
        try:
            report = _run_manual_paper_forward_refresh()
        except Exception as exc:
            return internal_error_response(
                app, "免费公开行情刷新失败，未解锁任何交易权限",
                exc, status=503,
            )
        finally:
            _PAPER_FORWARD_REFRESH_LOCK.release()
        return jsonify({
            "ok": True,
            "report": report,
            "strategy_lab": _strategy_lab_evidence(),
            "decision_gate": _decision_gate_status(),
        })

    @app.get("/api/strategy-lab/development-catalog")
    def strategy_development_catalog_api():
        from .emotion_pattern_research import research_catalog
        from .emotion_pattern_replay import contract_identity
        from .cloud_pattern_route import public_observation
        from .emotion_system import public_context

        payload = research_catalog()
        payload["cloud_joint_observation"] = public_observation()
        payload["emotion_system"] = public_context(root=app.config.get("RESEARCH_WORKBENCH_ROOT"), joint=payload["cloud_joint_observation"])
        payload["event_replay"] = {
            "implemented": True, "contract_sha256": contract_identity(),
            "description": "已实现逐事件扣成本回放，并支持日线战法的现金分红与红利税结算：按登记日实际模拟持股计权益，未付现金保留应收。原入场上限、风险线、2R与情绪退出规则保持不变，包括除息日；这不是账户组合回测，不代表策略准入。云端代码更新不等于本地验证结果已同步。",
            "cash_entitlement_settlement_implemented": (_PROJECT / "research" / "run_emotion_pattern_cash_settlement.py").is_file(),
            "cloud_results_available": False,
            "portfolio_validation_complete": False, "production_eligible": False,
        }
        payload["diagnostic_controls"] = {
            "implemented": (_PROJECT / "research" / "run_emotion_pattern_controls.py").is_file(),
            "description": "验证方法：同信号比较情绪退出、取消情绪过滤、同日期取消形态筛选；五个因子按实际情绪分别评价排序。属于历史诊断，不是新增合格策略；本地结果未自动同步云端。",
            "new_blind_trial": False, "production_eligible": False,
            "cloud_results_available": False,
        }
        payload["corrected_factor_replay"] = {
            "implemented": (_PROJECT / "research" / "run_crowding_cash_share_diagnostic.py").is_file(),
            "description": "近期涨停低拥挤度：固定完整分组，按实际情绪核验股数与历史费用；纯现金分红按登记日持股记权益、按研究卖出日计税。已核实的证券换码按官方生效日还原交易身份，不重置持仓。明确停牌的成交额均值缺口使用执行前20个完整市场交易日修复；正常交易日的缺失值不填零。到期未卖出属于持仓与退出风险，和权益证据缺失分开记录；不延长原期限使旧验证通过，也不记成亏损归零。未知送转等仍保留，未通过验证不计为合格因子。云端代码部署不等于回放已运行，本地账本不经 GitHub 同步。",
            "cash_entitlement_settlement_implemented": (_PROJECT / "research" / "run_crowding_cash_entitlement_replay.py").is_file(),
            "suspension_execution_input_repair_implemented": (_PROJECT / "research" / "run_crowding_suspension_liquidity_replay.py").is_file(),
            "dated_security_identity_replay_implemented": (_PROJECT / "research" / "run_crowding_alias_replay.py").is_file(),
            "inventory_exit_audit_implemented": (_PROJECT / "research" / "audit_crowding_deadline_inventory.py").is_file(),
            "new_blind_trial": False, "production_eligible": False,
            "cloud_results_available": False,
        }
        payload["signal_first_factor_attribution"] = {
            "implemented": (_PROJECT / "research" / "run_emotion_factor_attribution.py").is_file(),
            "description": "五因子按情绪评价：区间收缩、突破距离、收盘承接、回踩量能、行业相对强度。每个战法、信号日与实际情绪先固定高低分组，再分别计入10／20／30bp滑点及原模型费用；支持全部四组对照的现金分红与红利税结算，沿用原来的完整成员，不按结算结果换组。保留未成交、未解决权益和样本不足。展示的是排序验证方法，不是五条独立策略；不根据已见收益翻转方向，不把完整子样本当成全部样本。云端已部署评估代码不代表已有云端验证结果。",
            "cash_entitlement_attribution_implemented": (_PROJECT / "research" / "run_emotion_control_cash_settlement.py").is_file(),
            "new_blind_trial": False, "direction_selected": False,
            "production_eligible": False, "cloud_results_available": False,
        }
        response = jsonify(payload)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/strategy-lab/user-workbench")
    def user_strategy_workbench_api():
        view = request.args.get("view", "full")
        try:
            state = {**(
                user_strategy_workbench_state()
                if view == "full"
                else user_strategy_workbench_state(view=view)
            )}
        except StrategyValidationError as exc:
            return jsonify({
                "ok": False, "error": str(exc), "code": "invalid_view",
            }), 400
        state.setdefault("cost_semantics", _STRATEGY_COST_SEMANTICS)
        response = jsonify(state)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        # The workbench/archive request itself succeeded even when the current
        # research Panel is not ready.  ``available`` remains the explicit
        # execution-source flag; callers can still render workflow and history.
        return response, 200

    @app.post("/api/strategy-lab/user-strategies")
    @_research_mutation_route
    def save_user_strategy_api():
        payload = request.get_json(silent=True)
        forward_handle = None
        if isinstance(payload, dict) and payload.get("strategy_id"):
            forward_handle = _acquire_user_strategy_forward_lock()
            if forward_handle is None:
                return jsonify({
                    "ok": False,
                    "error": "纸面前向runner正在推进，策略修订未写入；请稍后重试",
                }), 409
        try:
            try:
                strategy = save_user_strategy(payload or {})
            except StrategyValidationError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
        finally:
            _release_user_strategy_forward_lock(forward_handle)
        return jsonify({
            "ok": True, "strategy": strategy,
            "cost_semantics": _STRATEGY_COST_SEMANTICS,
        }), 201

    @app.post("/api/strategy-lab/user-strategies/validate")
    def validate_user_strategy_api():
        payload = request.get_json(silent=True)
        try:
            spec = validate_user_strategy_draft_spec(payload or {})
        except StrategyValidationError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({
            "ok": True, "spec": spec,
            "validation_scope": "draft_syntax_only",
            "execution_eligible": False,
            "cost_semantics": _STRATEGY_COST_SEMANTICS,
        })

    @app.post("/api/strategy-lab/user-backtests")
    def run_user_strategy_backtest_api():
        return jsonify({
            "ok": False,
            "code": "local_fractional_label_backtest_deprecated",
            "error": (
                "旧本地V4路径未执行100股整手、现金与全成本约束，"
                "不得再作为回测或门禁证据。"
            ),
            "next_action": {
                "code": "use_unified_diagnose",
                "endpoint": "/api/strategy-lab/diagnose",
                "text": "使用统一引擎科学诊断",
            },
        }), 410

    @app.get("/api/strategy-lab/user-backtests/<run_id>")
    def user_strategy_backtest_report_api(run_id: str):
        try:
            report = load_user_strategy_backtest_report(run_id)
        except StrategyValidationError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        return jsonify({
            "ok": True, "report": report,
            "cost_semantics": _STRATEGY_COST_SEMANTICS,
        })

    @app.get("/api/strategy-lab/user-backtests/<run_id>/curve")
    def user_strategy_backtest_curve_api(run_id: str):
        try:
            curve = user_strategy_backtest_curve_state(run_id)
        except StrategyValidationError as exc:
            response = jsonify({"ok": False, "error": str(exc)})
            response.headers["Cache-Control"] = "no-store, max-age=0"
            return response, 404
        response = jsonify({"ok": True, "curve": curve})
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/strategy-lab/user-backtests/<run_id>/forward")
    def user_strategy_backtest_forward_api(run_id: str):
        try:
            forward = user_strategy_paper_forward_state(run_id)
        except ForwardEvidenceError as exc:
            response = jsonify({
                "ok": False,
                "code": "forward_evidence_corrupt",
                "error": str(exc),
            })
            response.headers["Cache-Control"] = "no-store, max-age=0"
            return response, 503
        except StrategyValidationError as exc:
            response = jsonify({"ok": False, "error": str(exc)})
            response.headers["Cache-Control"] = "no-store, max-age=0"
            return response, 404
        response = jsonify({"ok": True, "forward": forward})
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.post("/api/strategy-lab/diagnose")
    @_research_mutation_route
    def strategy_lab_unified_diagnose_api():
        """M3.2 统一引擎专业诊断：factor_system evaluate + diagnose（只加注不替换门槛）。

        与 V4 回测共用 RUN_LOCK 串行化（防同进程双载面板 ~800MB）；结果按 spec 哈希缓存。
        首次冷面板加载 + 发现/验证/历史确认需数分钟，同规格缓存即时返回；前端已提示等待。
        """
        payload = request.get_json(silent=True) or {}
        spec = payload.get("spec") or {}
        strategy_id = payload.get("strategy_id")
        publish_history_report = payload.get("publish_history_report") is True
        try:
            evaluator_fingerprint = user_strategy_evaluator_fingerprint()
        except StrategyValidationError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 503
        if not spec.get("factors") or not isinstance(spec.get("factors"), dict):
            return jsonify({"ok": False, "error": "缺少策略因子权重（factors）"}), 400
        try:
            spec = validate_user_strategy_spec(spec)
        except StrategyValidationError as exc:
            return jsonify({"ok": False, "error": f"策略规格校验失败: {exc}"}), 400
        key = hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        now = _time.time()
        hit = _LAB_DIAG_CACHE.get(key)
        if hit:
            try:
                user_strategy_validate_diagnosis_input(hit[1])
            except StrategyValidationError:
                _LAB_DIAG_CACHE.pop(key, None)
                hit = None
        if hit and now - hit[0] < _LAB_DIAG_TTL:
            try:
                online_test = user_strategy_record_historical_test(
                    spec, hit[1].get("evaluate") or {},
                )
            except Exception as exc:
                return internal_error_response(
                    app, "共享在线检验记录失败，诊断未晋升",
                    exc, status=503,
                )
            hit[1]["historical_online_test"] = online_test
            # 缓存命中仍为当前策略版本落一份不可变证据；不得用 None 清空旧指针。
            diagnosis_evidence = None
            if strategy_id:
                try:
                    diagnosis_evidence = user_strategy_save_diagnosis(
                        strategy_id, hit[1], evaluated_spec=spec,
                    )
                except StrategyValidationError:
                    pass
            report_summary = None
            if publish_history_report:
                try:
                    report_summary = user_strategy_publish_diagnosis_report(
                        hit[1], spec,
                        strategy_id=strategy_id,
                        diagnosis_evidence=diagnosis_evidence,
                    )
                except (StrategyValidationError, RuntimeError) as exc:
                    return internal_error_response(
                        app, "统一诊断完成，但实时图证据发布失败",
                        exc, status=503,
                    )
            return jsonify({
                "ok": True, "report": hit[1], "cached": True,
                "promotion": None, "diagnosis_evidence": diagnosis_evidence,
                "run_id": (
                    report_summary.get("run_id") if report_summary else None
                ),
                "report_summary": report_summary,
                "cost_semantics": _STRATEGY_COST_SEMANTICS,
            })
        if not _STRATEGY_RUN_LOCK.acquire(blocking=False):
            return jsonify({
                "ok": False,
                "error": "回测引擎忙（有回测/诊断正在运行），请稍后重试",
                "research_lock": user_strategy_research_lock_state(),
            }), 409
        try:
            import factor_system.compose as compose
            try:
                observation = _begin_strategy_historical_observation(spec)
            except Exception as exc:
                return internal_error_response(
                    app, "历史观察顺序预约失败，未启动评估",
                    exc, status=503,
                )
            try:
                evaluate = compose.evaluate_strategy(
                    spec, expected_input_key=observation["data_key"],
                )
            except Exception as exc:
                try:
                    online_test = user_strategy_record_historical_test(
                        spec, {"evaluation_error": str(exc)},
                    )
                except Exception as finalize_exc:
                    return internal_error_response(
                        app,
                        "统一引擎评估失败，且保守 p=1 检验未能落账；观察预约保留",
                        finalize_exc, status=503,
                    )
                return internal_error_response(
                    app, "统一引擎评估失败；已保守消费 p=1 检验序号",
                    exc, extra={"historical_online_test": online_test},
                )
            # 历史 p 一旦被计算，必须先占用共享 LOND 序号并提交；后续诊断、
            # 缓存或证据失败不能把已观察假设从全局序列中抹掉。
            try:
                online_test = user_strategy_record_historical_test(spec, evaluate)
            except Exception as exc:
                return internal_error_response(
                    app, "共享在线检验记录失败，诊断已停止",
                    exc, status=503,
                )
            try:
                diagnose = compose.diagnose_strategy(
                    spec, expected_input_key=observation["data_key"],
                    include_extended=False,
                )
            except Exception as exc:
                return internal_error_response(
                    app, "在线检验已冻结，但扩展诊断失败", exc,
                    extra={"historical_online_test": online_test},
                )
        finally:
            _STRATEGY_RUN_LOCK.release()
        try:
            if user_strategy_evaluator_fingerprint() != evaluator_fingerprint:
                raise StrategyValidationError("策略执行指纹在诊断期间变化")
        except StrategyValidationError as exc:
            return jsonify({
                "ok": False,
                "error": f"统一诊断完成但执行源码已变化，拒绝缓存或发布: {exc}",
            }), 503
        report = {
            "evaluate": evaluate,
            "diagnose": diagnose,
            "historical_online_test": online_test,
            "strategy_evaluator_fingerprint": evaluator_fingerprint,
        }
        # TTL starts when the expensive diagnosis completes.  Using request
        # start time makes every calculation longer than the TTL expire before
        # it is even inserted, forcing an immediate duplicate full run.
        _LAB_DIAG_CACHE[key] = (_time.time(), report)
        if len(_LAB_DIAG_CACHE) > _LAB_DIAG_MAX_ENTRIES:
            for k in list(_LAB_DIAG_CACHE)[: len(_LAB_DIAG_CACHE) - _LAB_DIAG_MAX_ENTRIES]:
                _LAB_DIAG_CACHE.pop(k, None)
        # 手工诊断只落不可变探索证据，不写 promotion；HISTORICAL_PASS 只认固定批次。
        diagnosis_evidence = None
        if strategy_id:
            try:
                diagnosis_evidence = user_strategy_save_diagnosis(
                    strategy_id, report, evaluated_spec=spec,
                )
            except StrategyValidationError:
                diagnosis_evidence = None
        report_summary = None
        if publish_history_report:
            try:
                report_summary = user_strategy_publish_diagnosis_report(
                    report, spec,
                    strategy_id=strategy_id,
                    diagnosis_evidence=diagnosis_evidence,
                )
            except (StrategyValidationError, RuntimeError) as exc:
                return jsonify({
                    "ok": False,
                    "error": f"统一诊断完成，但实时图证据发布失败: {exc}",
                }), 503
        return jsonify({
            "ok": True, "report": report, "cached": False,
            "promotion": None, "diagnosis_evidence": diagnosis_evidence,
            "run_id": report_summary.get("run_id") if report_summary else None,
            "report_summary": report_summary,
            "cost_semantics": _STRATEGY_COST_SEMANTICS,
        })

    @app.post("/api/strategy-lab/experiments/register")
    @_research_mutation_route
    def strategy_lab_experiment_register_api():
        """在候选运行前冻结基线、单变量规格与研究假设。"""
        try:
            payload = _strict_json_payload(
                allowed={"baseline_evidence", "candidate_spec", "hypothesis"},
                required={"baseline_evidence", "candidate_spec", "hypothesis"},
            )
            registration = user_strategy_register_experiment(
                payload["baseline_evidence"], payload["candidate_spec"], payload["hypothesis"],
            )
        except (ValueError, StrategyValidationError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc),
                            "research_lock": user_strategy_research_lock_state()}), 409
        return jsonify({"ok": True, "registration": registration}), 201

    @app.post("/api/strategy-lab/experiments")
    @_research_mutation_route
    def strategy_lab_experiment_api():
        """按先验登记比较候选历史证据；结果永远不直接确证或晋升。"""
        try:
            payload = _strict_json_payload(
                allowed={"registration_id", "candidate_evidence"},
                required={"registration_id", "candidate_evidence"},
            )
            experiment = user_strategy_create_experiment(
                payload["registration_id"], payload["candidate_evidence"],
            )
        except (ValueError, StrategyValidationError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({
                "ok": False,
                "error": str(exc),
                "research_lock": user_strategy_research_lock_state(),
            }), 409
        return jsonify({"ok": True, "experiment": experiment}), 201

    @app.post("/api/strategy-lab/promotion")
    @_research_mutation_route
    def strategy_lab_promotion_api():
        """晋升/否决动作（纸面前向观察级，不触实盘）。

        body: {"strategy_id": "...", "run_id": "...", "action": "paper_observe_on|paper_observe_off|unlock|lock"}
        - 当前没有策略前向终测 runner，unlock 一律返回 409。
        - 任何动作或人工同意都不授予推荐资格；recommendation_eligible 只由研究证据决定。
        """
        payload = request.get_json(silent=True) or {}
        strategy_id = payload.get("strategy_id")
        run_id = payload.get("run_id")
        action = payload.get("action")
        if not strategy_id or action not in (
            "paper_observe_on", "paper_observe_off", "unlock", "lock",
        ):
            return jsonify({"ok": False, "error": "参数缺失：需要 strategy_id 与合法 action"}), 400
        if action == "paper_observe_on" and not run_id:
            return jsonify({
                "ok": False,
                "error": "开启纸面前向观察必须绑定 run_id",
            }), 400
        try:
            saved = user_strategy_set_promotion_action(
                strategy_id, action, run_id=run_id,
            )
        except StrategyValidationError as exc:
            return jsonify({
                "ok": False,
                "error": str(exc),
                "research_lock": user_strategy_research_lock_state(),
            }), 409
        return jsonify({
            "ok": True, "strategy_id": strategy_id, "action": action,
            "promotion": saved.get("promotion"),
            "recommendation_eligible": saved.get("recommendation_eligible"),
        })

    @app.post("/api/strategy-lab/batch-generate")
    def strategy_lab_batch_generate_api():
        """从当前严格 epoch 因子池生成选股策略并逐条历史确认。

        后台单 worker 与回测/诊断共用 RUN_LOCK；全批完成后执行 BY-FDR。
        one_way_cost_bps 仅表示单边滑点；佣金、印花税、过户费由成本模型另加。
        五段均成功时只记 HISTORICAL_PASS（历史确认通过，仍不可投产）。
        """
        try:
            result = user_strategy_start_batch()
        except Exception as exc:
            return internal_error_response(app, "批量生成启动失败", exc)
        if not result.get("ok"):
            status_code = (409 if "运行中" in str(result.get("error", ""))
                           else 409 if "忙" in str(result.get("error", "")) else 400)
            if status_code == 409:
                result = {
                    **result,
                    "research_lock": user_strategy_research_lock_state(),
                }
            return jsonify(result), status_code
        return jsonify(result)

    @app.get("/api/strategy-lab/batch-status")
    def strategy_lab_batch_status_api():
        """批量生成进度与候选池（每 3s 轮询；候选全部展示，不裁剪不过滤）。"""
        return jsonify(_truthful_strategy_batch_status(user_strategy_batch_status()))

    @app.post("/api/strategy-lab/user-assistant")
    def user_strategy_assistant_api():
        payload = request.get_json(silent=True) or {}
        try:
            assistant = user_strategy_assistant_for_run(payload.get("run_id"))
        except StrategyValidationError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        return jsonify({"ok": True, "assistant": assistant})

    # ── API: 仪表盘聚合数据 ──────────────────
    @app.get("/api/dashboard")
    def dashboard_data():
        view = request.args.get("view", "compact")
        if view not in {"compact", "evidence"}:
            return jsonify({"error": "view 仅支持 compact/evidence"}), 400
        decision_gate_full = (
            _decision_gate_status() if view == "evidence"
            else _decision_gate_status_fast()
        )
        decision_gate = (
            decision_gate_full if view == "evidence"
            else _compact_decision_gate(decision_gate_full)
        )
        names: dict[str, str] = {}
        prices: dict[str, float] = {}
        prevs: dict[str, float] = {}

        qp = _PROJECT / "research" / "quant_stock_snapshot.json"
        quant_codes = []
        if qp.exists():
            try:
                quant = json.loads(qp.read_text(encoding="utf-8"))
                # 候选池 rank≤100；配额覆盖全池，
                # 否则信号里非持仓股名称为空、显示成代码
                quant_codes = [
                    s["code"] for s in quant.get("stocks", [])[:100]
                    if is_recommendable_stock(s.get("code", ""), s.get("name", ""))
                ]
            except (OSError, json.JSONDecodeError):
                quant_codes = []

        if quant_codes:
            quote_ok, quotes, _quote_error = _bounded_call(
                lambda: app.config["QUOTE_PROVIDER"].get_quotes(quant_codes),
                1.5,
            )
            if quote_ok and isinstance(quotes, dict):
                for c, q in quotes.items():
                    names[c] = getattr(q, "name", "") or c
                    p = getattr(q, "price", 0) or 0
                    pc = getattr(q, "previous_close", 0) or 0
                    if p > 0:
                        prices[c] = p
                    if pc > 0:
                        prevs[c] = pc

        signals_data = _synced_live_signal_data()
        portfolio_data = _manual_portfolio_status(app.config["QUOTE_PROVIDER"])
        signal_summary = dict(signals_data.get("summary", {}))
        signal_summary["held_positions"] = portfolio_data["position_count"]

        # Dashboard 的股票排名与机会监控共用同一快照和同一评分；旧量化快照
        # composite 不再作为第二套“综合分”对外输出。
        with _OPP_STATE["lock"]:
            opportunity_snapshot = copy.deepcopy(_OPP_STATE.get("result") or {})
        # Dashboard “Top” rows are the same quality-passed research candidates
        # as /api/opportunities.items.  Watch/rejected rows stay in their own
        # buckets and must never lose their verdict on the way to the UI.
        ranked_rows = list(opportunity_snapshot.get("items") or [])
        def dashboard_score_key(row: dict) -> tuple[float, float]:
            professional = _finite_number(
                (((row.get("assessment") or {}).get("gates") or {})
                 .get("composite") or {}).get("composite")
            )
            market_diagnostic = _finite_number(
                (row.get("opportunity_quality") or {}).get("pre_rank_score")
            )
            return (
                professional if professional is not None else -1.0,
                market_diagnostic if market_diagnostic is not None else -1.0,
            )

        ranked_rows.sort(key=dashboard_score_key, reverse=True)
        top_stocks = []
        for rank, row in enumerate(ranked_rows[:15], start=1):
            professional = (
                (((row.get("assessment") or {}).get("gates") or {})
                 .get("composite") or {}).get("composite")
            )
            top_stocks.append({
                "code": row.get("code"),
                "name": row.get("name") or row.get("code"),
                "rank": rank,
                "composite": professional,
                "score": professional,
                "unified_composite_score": professional,
                "condition_match_score": _finite_number(
                    row.get("condition_match_score")
                ),
                "score_role": (
                    "deprecated_compatibility_alias_of_unified_composite_score"
                ),
                "score_standard_id": _SCORING_STANDARD["id"],
                "candidate_role": "quality_passed_research_candidate",
                "verdict": (row.get("assessment") or {}).get("verdict"),
                "research_only": True,
                "price": row.get("price"),
            })

        legacy_headline = (
            _historical_candidate_headline(decision_gate)
            or signals_data.get("headline")
        )
        strategy_lab_full = _strategy_lab_evidence()
        headline_full, paper_allocation = _active_strategy_public_view(
            strategy_lab_full, legacy_headline
        )
        headline = (
            headline_full if view == "evidence"
            else _compact_candidate_headline(headline_full)
        )
        live_signal_snapshot_full = signals_data["live_signal_snapshot"]
        live_signal_snapshot = (
            live_signal_snapshot_full if view == "evidence"
            else _compact_live_signal_snapshot(live_signal_snapshot_full)
        )
        strategy_lab = strategy_lab_full
        if view != "evidence":
            strategy_lab = _compact_strategy_lab_evidence(strategy_lab)
        return jsonify({
            "signals": signals_data.get("signals", []),
            "signal_summary": signal_summary,
            "snapshot_date": signals_data.get("snapshot_date", ""),
            "data_asof": signals_data.get("data_asof") or signals_data.get("snapshot_date", ""),
            "data_staleness_days": signals_data.get("data_staleness_days", 0),
            "data_stale": signals_data.get("data_stale", False),
            "headline": headline,
            "paper_allocation": paper_allocation,
            "entry_timing": signals_data.get("entry_timing", {}),
            "account": _public_account_facts(portfolio_data),
            "research_nominal_account": signals_data.get(
                "research_nominal_account", {}
            ),
            "portfolio": portfolio_data,
            "decision_gate": decision_gate,
            "strategy_lab": strategy_lab,
            "scoring_standard": copy.deepcopy(_SCORING_STANDARD),
            "top_stocks": top_stocks,
            "live_signal_snapshot": live_signal_snapshot,
            "live_signal_source": "/api/signals/live",
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })

    # ── API: 今日信号 ─────────────────────────
    @app.get("/api/signals/today")
    def today_signals():
        view = request.args.get("view", "compact")
        if view not in {"compact", "evidence"}:
            return jsonify({"error": "view 仅支持 compact/evidence"}), 400
        gate_full = (
            _decision_gate_status() if view == "evidence"
            else _decision_gate_status_fast()
        )
        gate = gate_full if view == "evidence" else _compact_decision_gate(gate_full)
        signal_data = _synced_live_signal_data()
        portfolio = _manual_portfolio_status(app.config["QUOTE_PROVIDER"])
        signal_data["account"] = _public_account_facts(portfolio)
        signal_data["summary"]["held_positions"] = portfolio["position_count"]
        legacy_headline = (
            _historical_candidate_headline(gate) or signal_data.get("headline")
        )
        strategy_lab_full = _strategy_lab_evidence()
        headline_full, paper_allocation = _active_strategy_public_view(
            strategy_lab_full, legacy_headline
        )
        signal_data["headline"] = (
            headline_full if view == "evidence"
            else _compact_candidate_headline(headline_full)
        )
        signal_data["paper_allocation"] = paper_allocation
        strategy_lab = strategy_lab_full
        if view != "evidence":
            strategy_lab = _compact_strategy_lab_evidence(strategy_lab)
        else:
            strategy_lab["decision_gate"] = gate
        live_signal_snapshot_full = signal_data["live_signal_snapshot"]
        live_signal_snapshot = (
            live_signal_snapshot_full if view == "evidence"
            else _compact_live_signal_snapshot(live_signal_snapshot_full)
        )
        return jsonify({
            **signal_data,
            "decision_gate": gate,
            "strategy_lab": strategy_lab,
            "live_signal_snapshot": live_signal_snapshot,
            "live_signal_source": "/api/signals/live",
        })

    # ── API: 历史信号 ─────────────────────────
    @app.get("/api/signals/history")
    def signal_history():
        limit = request.args.get("limit", 100, type=int)
        signals = get_signal_history(limit=min(limit, 500))
        decision_gate = _decision_gate_status_fast()
        audit_rows = []
        for source in signals:
            row = copy.deepcopy(source) if isinstance(source, dict) else {}
            original_action = str(row.get("action") or "").upper()
            strategy_ref = row.get("strategy_ref")
            evidence_complete = bool(
                isinstance(strategy_ref, dict)
                and (strategy_ref.get("strategy_id") or strategy_ref.get("candidate_id"))
                and row.get("data_asof")
                and row.get("valid_until")
            )
            historical_plan = {
                key: copy.deepcopy(row.get(key))
                for key in (
                    "entry_zone", "stop_target", "estimated_cost",
                    "position_pct", "affordable",
                )
                if key in row
            }
            row.update({
                "original_action": original_action or None,
                "action": "WATCH",
                "action_display": "历史研究观察",
                "status": (
                    "expired_historical_evidence"
                    if evidence_complete else "invalidated_legacy"
                ),
                "research_only": True,
                "execution_eligible": False,
                "expired": True,
                "invalidated_legacy": not evidence_complete,
                "expiry_reason": (
                    "历史信号接口仅供审计，不产生当前买卖动作；"
                    "缺少完整证据身份的旧记录永久失效。"
                ),
                "historical_plan": historical_plan,
                "entry_zone": None,
                "stop_target": None,
                "estimated_cost": None,
                "position_pct": None,
                "affordable": False,
            })
            if not isinstance(strategy_ref, dict):
                row["strategy_ref"] = {
                    "status": "missing_legacy_evidence_identity",
                }
            audit_rows.append(row)
        original_buys = [
            row for row in audit_rows if row.get("original_action") == "BUY"
        ]
        original_sells = [
            row for row in audit_rows if row.get("original_action") == "SELL"
        ]
        return jsonify({
            "signals": audit_rows,
            "scope": "historical_audit_only",
            "execution_eligible": False,
            "current_action_source": "/api/opportunities",
            "decision_gate": _compact_decision_gate(decision_gate),
            "stats": {
                "total": len(audit_rows),
                "buy_count": 0,
                "sell_count": 0,
                "execution_eligible_count": 0,
                "historical_original_buy_count": len(original_buys),
                "historical_original_sell_count": len(original_sells),
                "invalidated_legacy_count": sum(
                    row.get("invalidated_legacy") is True for row in audit_rows
                ),
            },
        })

    # ── API: 实时因子信号（任务书 Step 3）──────────────
    @app.get("/api/signals/live")
    def live_signals_api():
        """实时因子信号最新快照（仅审计，不自动下单）。"""
        d = _safe_live_signal_snapshot()
        if not d:
            return jsonify({"ok": True, "data": {
                "generated_at": None, "signals": [], "quote_meta": {},
                "market_status": {}, "honesty": "信号系统尚未生成快照（后台线程每60s刷新）"}})
        payload = copy.deepcopy(d)
        rows = payload.get("signals") if isinstance(payload.get("signals"), list) else []
        actionable = [
            row for row in rows
            if isinstance(row, dict)
            and str(row.get("action") or "").upper() in {"BUY", "SELL"}
            and row.get("execution_eligible") is True
        ]
        observations = [row for row in rows if row not in actionable]
        payload["signals"] = actionable
        payload["observations"] = observations
        payload["n_actionable_signals"] = len(actionable)
        payload["n_observations"] = len(observations)
        payload["signals_contract"] = {
            "signals": "current_execution_eligible_buy_or_sell_only",
            "observations": "non_actionable_market_watch_rows",
            "legacy_no_signal_rows_in_signals": False,
        }
        return jsonify({"ok": True, "data": payload})

    @app.get("/api/signals/audit")
    def live_signals_audit_api():
        """实时信号审计日志（append-only JSONL，最近 N 条）。"""
        limit = min(request.args.get("limit", 50, type=int), 200)
        return jsonify({"ok": True, "audit": _signal_tail_audit(limit)})

    @app.get("/api/signals/health")
    def live_signals_health_api():
        """信号系统健康/降级状态。"""
        d = _safe_live_signal_snapshot()
        qm = d.get("quote_meta", {}) if d else {}
        scope = d.get("watchlist_scope", {}) if d else {}
        return jsonify({
            "ok": True,
            "generated_at": d.get("generated_at") if d else None,
            "stale": bool(d.get("stale")) if d else True,
            "stale_reasons": d.get("stale_reasons", []) if d else ["尚无信号快照"],
            "decision_eligible": d.get("decision_eligible") is True if d else False,
            "zero_signal_reasons": (
                d.get("zero_signal_reasons", [])
                if d else ["尚无信号快照"]
            ),
            "snapshot_age_seconds": d.get("snapshot_age_seconds") if d else None,
            "quote_health": qm.get("operational_health_status", qm.get("health_status")),
            "quote_snapshot_health": qm.get("health_status"),
            "degraded": qm.get("operational_degraded", qm.get("degraded")),
            "quote_source": qm.get("source"),
            "quote_coverage": {
                "mode": qm.get("coverage_mode"),
                "expected": qm.get("coverage_expected"),
                "display_returned": qm.get("display_coverage_returned"),
                "live_decision_returned": qm.get("live_decision_coverage_returned"),
                "complete": qm.get("coverage_complete"),
                "reason": qm.get("coverage_reason"),
                "universe_total": scope.get("universe_total"),
                "attempted": scope.get("quote_attempted"),
                "covered": scope.get("quote_covered"),
                "missing": scope.get("quote_missing"),
            },
            "monitoring_scope": scope,
            "n_signals": sum(
                1 for row in (d.get("signals", []) if d else [])
                if isinstance(row, dict)
                and row.get("action") in {"BUY", "SELL", "WATCH"}
            ),
            "factor_as_of": d.get("factor_as_of", []) if d else [],
            "n_watchlist": d.get("n_watchlist", 0) if d else 0,
            "snapshot_health": d.get("snapshot_health", {}) if d else {},
            "refresh_seconds": 60,
            "audit_path": "research/mom_live_signals_audit.jsonl",
            "auto_order": False,
            "honesty": "仅展示审计，不自动下单",
        })

    @app.get("/api/research/v2-backtest-status")
    def v2_backtest_status_api():
        """Only exposes file/worker facts; this is not a strategy permission endpoint."""
        return jsonify({"ok": True, "data": _v2_backtest_status()})

    @app.get("/api/research/csrc-mapping-review")
    def csrc_mapping_review_api():
        """Return frozen source text only; per-record model predictions stay hidden."""
        try:
            return jsonify({
                "ok": True,
                "data": _v88_mapping_review_snapshot(request.args.get("index", type=int)),
            })
        except (OSError, RuntimeError, csv.Error) as exc:
            return jsonify({
                "ok": False,
                "error": f"V88 盲审队列不可用：{exc}",
                "predictions_exposed": False,
            }), 503

    @app.get("/api/research/csrc-security-aliases")
    def csrc_security_aliases_api():
        """User-initiated official alias lookup; never match against the review item."""
        query = str(request.args.get("q") or "").strip()
        if len(query) < 2 or len(query) > 60:
            return jsonify({
                "ok": False,
                "error": "查询词长度必须为 2–60 个字符",
                "predictions_exposed": False,
            }), 400
        try:
            items = _search_v88_official_aliases(query)
        except (OSError, csv.Error) as exc:
            return jsonify({"ok": False, "error": f"官方别名表不可用：{exc}"}), 503
        return jsonify({
            "ok": True,
            "data": {
                "query": query,
                "items": items,
                "source": "沪深交易所官方证券清单及更名表的冻结别名库",
                "review_item_auto_matched": False,
                "predictions_exposed": False,
            },
        })

    @app.post("/api/research/csrc-mapping-review/<manuscript_id>")
    def save_csrc_mapping_review_api(manuscript_id: str):
        """Upsert one genuine human label without revealing the frozen prediction."""
        try:
            payload = _strict_json_payload(
                allowed={
                    "correct_stock_codes", "correct_entity_type", "notes",
                    "reviewer_id",
                },
                required={"correct_stock_codes", "correct_entity_type"},
            )
            snapshot = _save_v88_mapping_review_label(manuscript_id, payload)
        except LookupError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except FileExistsError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
        except PermissionError as exc:
            status = 503 if "未配置" in str(exc) else 403
            return jsonify({"ok": False, "error": str(exc)}), status
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except (OSError, csv.Error) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 503
        audit: dict[str, object] | None = None
        audit_error = None
        if snapshot["completed"] == snapshot["total"]:
            try:
                from research.v88_csrc_scoped_entity_mapping import run as audit_v88_mapping
                audit = audit_v88_mapping()
            except Exception as exc:  # Label is saved; audit remains fail-closed.
                audit_error = f"标签已保存，但冻结审计失败：{exc}"
        return jsonify({
            "ok": True,
            "data": snapshot,
            "audit": ({
                "status": audit.get("status"),
                "mapping_data_ready": audit.get("mapping_data_ready") is True,
                "failed_gates": audit.get("failed_gates") or [],
                "manual_review": audit.get("manual_review") or {},
                "factor_backtest_allowed": audit.get("factor_backtest_allowed") is True,
            } if audit else None),
            "audit_error": audit_error,
            "safety": {
                "predictions_exposed": False,
                "recommendation_allowed": False,
                "order_generation_allowed": False,
                "real_money_trading": False,
                "automatic_trading": False,
            },
        })

    # ── API: 持仓状态 ─────────────────────────
    @app.get("/api/portfolio")
    def portfolio_api():
        return jsonify({
            **_manual_portfolio_status(app.config["QUOTE_PROVIDER"]),
            "decision_gate": _fast_compact_decision_gate(),
        })

    # ── API: 买入时机 ─────────────────────────
    def _server_timing_snapshot(
        code: str, *, authoritative_gate: bool = False, account_access: bool = True,
    ) -> tuple[dict[str, object], object, dict[str, object]]:
        ok, provider_data, provider_error = _bounded_call(
            lambda: (
                app.config["QUOTE_PROVIDER"].get_quotes([code]),
                app.config["CANDLE_PROVIDER"].get_candles(code, 60, interval="5m"),
                app.config["CANDLE_PROVIDER"].get_intraday(code),
            ),
            8.0,
        )
        if not ok or not provider_data:
            raise RuntimeError(f"行情或分钟线不可用：{provider_error}")
        quotes, candles, intraday = provider_data
        q = (quotes or {}).get(code)
        quote_price = getattr(q, "price", None) if q else None
        if (
            not q or not isinstance(quote_price, (int, float))
            or not math.isfinite(quote_price) or quote_price <= 0
        ):
            raise RuntimeError("实时行情缺失或价格无效")
        previous_close = _finite_number(getattr(q, "previous_close", None))
        change_pct = _finite_number(getattr(q, "change_pct", None))
        if not _quote_chain_valid_fields(quote_price, previous_close, change_pct):
            raise RuntimeError("实时价、昨收或涨跌幅缺失/不一致")
        signal_candles = completed_signal_candles(candles, "5m")
        if len(signal_candles) < 30:
            raise RuntimeError("已完成分钟线不足 30 根")
        quote = {
            "price": quote_price,
            "change_pct": change_pct,
            "previous_close": previous_close,
            "data_time": getattr(q, "data_time", None),
            "exchange_data_time": getattr(q, "exchange_data_time", None),
            "freshness": getattr(q, "freshness", "unknown"),
        }
        ff_snapshot = load_fund_flow_snapshot(code)
        ff_net_raw = ff_snapshot.get("raw_net_yi")
        ff_net_normalized = (
            ff_snapshot.get("normalized_net_yi")
            if ff_snapshot.get("decision_eligible") is True else None
        )
        signals = compute_timing_signals(
            signal_candles, quote, intraday, ff_net_normalized,
        )
        scoring = score_timing(signals)
        # Reuse this exact server-side calculation in the unified assessment.
        # This avoids fetching a second candle set for the same confirmation.
        with _SCORING_CACHE_LOCK:
            _SCORING_CACHE[(code, previous_close)] = (
                _time.time(), {"signals": signals, "scoring": scoring},
            )
        window = get_buy_window()
        if not account_access:
            response = _research_timing_payload(code, {"signals": signals, "scoring": scoring}, window, ff_net_normalized)
            response.update({
                "quote": quote, "price": quote_price, "change_pct": change_pct,
                "name": getattr(q, "name", code),
                "market": {"phase": window["phase"], "label": window["label"]},
                "analysis_time": datetime.now(_SH_TZ).isoformat(),
            })
            return response, q, {}
        gate_source = (
            _decision_gate_status() if authoritative_gate
            else _decision_gate_status_fast()
        )
        decision_gate = _compact_decision_gate(gate_source)
        portfolio = _manual_portfolio_status(app.config["QUOTE_PROVIDER"])
        current_exposure_pct = _portfolio_exposure_pct(portfolio)
        regime = _current_market_regime(portfolio)
        capacity = _market_adjusted_trade_capacity(regime)
        recommended_action = _gated_recommendation(
            scoring, window, decision_gate, regime, capacity,
        )
        try:
            name = _validated_display_name(getattr(q, "name", ""), fallback=code)
        except ValueError as exc:
            raise RuntimeError(f"行情名称无效：{exc}") from exc
        company_grade = None
        company_verification_reason = None
        try:
            from .company_profile import get_cached_summary
            company = get_cached_summary(code, name)
            if not company or company.get("ready") is not True:
                company_verification_reason = "公司档案未就绪"
            else:
                company_grade = _quality_composite_grade(code, company, q)
                if company_grade.get("pass_") is not True:
                    company_verification_reason = (
                        company_grade.get("note") or "关键否决证据未完整核验"
                    )
        except Exception as exc:
            company_verification_reason = f"公司与否决证据无法复核：{type(exc).__name__}"
        if company_verification_reason:
            recommended_action = "wait"
        assessment = _assess_candidate(
            code, name, q, model_research_score(code), q=quote,
            decision_gate=gate_source,
            available_cash=portfolio.get("available_cash"),
        )
        # Candidate quality may only reduce the already account/regime/capacity
        # gated action.  It must never overwrite a blocked action with buy_now.
        if assessment.get("manual_actionable") is not True:
            recommended_action = "wait"
        unified_score = _finite_number(
            ((assessment.get("gates") or {}).get("composite") or {}).get(
                "composite"
            )
        )
        return ({
            "code": code,
            "name": name,
            "quote": quote,
            "price": quote_price,
            "change_pct": quote.get("change_pct"),
            "market": {"phase": window["phase"], "label": window["label"]},
            "buy_window": window,
            "signals": signals,
            "score": scoring["score"],
            "unified_composite_score": unified_score,
            "condition_match_score": scoring["score"],
            "score_role": "condition_match_score_only_not_unified_stock_score",
            "score_deprecated": True,
            "score_deprecated_alias_of": "condition_match_score",
            "timing_evidence": {
                key: scoring.get(key) for key in (
                    "metric_name", "metric_label", "calibrated_probability",
                    "expected_return", "evidence_grade", "threshold_basis",
                    "coverage_pct", "available_weight_pct",
                    "available_signal_count", "total_signal_count",
                    "available_signals", "unavailable_signals", "hit_signals",
                    "partial_signals", "interpretation",
                )
            },
            "level": scoring["level"],
            "server_level": scoring["level"],
            "recommended_action": recommended_action,
            "company_grade": company_grade,
            "company_verification_reason": company_verification_reason,
            "assessment": assessment,
            "decision_gate": decision_gate,
            "market_regime": regime,
            "portfolio_valuation_status": portfolio.get("valuation_status"),
            "portfolio_missing_quote_codes": portfolio.get("missing_quote_codes") or [],
            "current_exposure_pct": current_exposure_pct,
            "trade_capacity": capacity,
            "ff_net": ff_net_normalized,
            "ff_net_raw": ff_net_raw,
            "ff_snapshot": {
                key: ff_snapshot.get(key)
                for key in (
                    "ts", "date", "note", "reason", "available",
                    "decision_eligible", "freshness_status", "data_staleness_days",
                )
            } | {"snapshot_only": True},
            "analysis_time": datetime.now(_SH_TZ).isoformat(),
        }, q, portfolio)

    app.extensions["olympus_timing_snapshot"] = _server_timing_snapshot

    def _validated_buy_facts(
        payload: dict[str, object], code: str, quote_obj,
        *, portfolio: dict[str, object] | None = None,
        max_quote_deviation: float = 0.05,
    ) -> tuple[float, int, float]:
        quote_price = _bounded_float(
            getattr(quote_obj, "price", None), "实时行情",
            minimum=0.01, maximum=100_000,
        )
        price = _bounded_float(
            payload.get("price"), "实际成交价格 price",
            minimum=0.01, maximum=100_000,
        )
        shares = _bounded_int(
            payload.get("shares"), "shares", minimum=100, maximum=10_000_000,
        )
        if shares % 100:
            raise ValueError("shares 必须是 100 股整数倍")
        if abs(price / quote_price - 1) > max_quote_deviation:
            raise ValueError("成交价与服务端实时价偏离超过 5%")
        previous_close = _bounded_float(
            getattr(quote_obj, "previous_close", None), "昨收",
            minimum=0.01, maximum=100_000,
        )
        change_pct = _bounded_float(
            getattr(quote_obj, "change_pct", None), "涨跌幅",
            minimum=-100, maximum=100,
        )
        implied_change = (quote_price / previous_close - 1.0) * 100
        if abs(implied_change - change_pct) > 0.5:
            raise ValueError("实时价、昨收与涨跌幅不一致")
        board_limit = 0.20 if code.startswith(("300", "301", "688", "689")) else 0.10
        if not (
            previous_close * (1 - board_limit - 0.005)
            <= price <= previous_close * (1 + board_limit + 0.005)
        ):
            raise ValueError("成交价超出当日板块涨跌停范围")
        amount = round(price * shares, 2)
        buy_cost = (
            _bounded_float(payload["buy_fee_yuan"], "buy_fee_yuan", minimum=0.0, maximum=100_000)
            if payload.get("buy_fee_yuan") is not None
            else amount * one_side_cost_rate(amount, "buy", slippage=0.0)
        )
        required_cash = amount + buy_cost
        account_cash = get_account_cash() if portfolio is None else None
        authoritative_cash = (
            _finite_number(portfolio.get("available_cash"))
            if portfolio is not None
            else _finite_number((account_cash or {}).get("cash_balance"))
        )
        if authoritative_cash is None:
            raise ValueError("尚未录入真实剩余资金，不能确认买入成交")
        available_cash = max(0.0, authoritative_cash)
        if required_cash > available_cash + 0.005:
            raise ValueError(
                f"成交金额及买入费用 ¥{required_cash:.2f} 超过账本可用现金 ¥{available_cash:.2f}"
            )
        return price, shares, available_cash

    @app.get("/api/timing/analyze")
    def timing_analyze():
        code = (request.args.get("code") or "").strip()
        if not CODE_PATTERN.fullmatch(code):
            return jsonify({"error": "仅支持沪深6位股票代码"}), 400

        account_access = request.args.get("account_access", "true").lower() != "false"
        try:
            if not account_access:
                snapshot, _quote, _portfolio = _server_timing_snapshot(code, account_access=False)
            else:
                snapshot, _quote, _portfolio = app.extensions["olympus_timing_snapshot"](code)
        except RuntimeError as exc:
            return jsonify({
                "error": str(exc), "recommended_action": "wait",
                "research_only": True,
                "account_access": account_access,
                "decision_gate": (_fast_compact_decision_gate() if account_access
                                  else {"recommendation_eligible": False}),
                "data_health": "unavailable",
            }), 503
        if not account_access:
            return jsonify(snapshot)
        snapshot["confirmation"] = {
            "ticket_required": True, "ticket_method": "POST",
            "ticket_endpoint": "/api/timing/ticket",
            "note": "人工确认后才签短票；最终确认仍重算全部门禁"}
        return jsonify(snapshot)
    @app.post("/api/timing/ticket")
    def timing_ticket():
        try:
            data = _strict_json_payload(
                allowed={"code", "alert_id"}, required={"code"},
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        code = str(data.get("code") or "").strip()
        if not CODE_PATTERN.fullmatch(code):
            return jsonify({"error": "仅支持沪深6位股票代码"}), 400
        alert_id = None
        if data.get("alert_id") is not None:
            try:
                alert_id = _bounded_int(
                    data.get("alert_id"), "alert_id", minimum=1,
                    maximum=2_147_483_647,
                )
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
        if alert_id is None:
            entry_context = {
                "entry_origin": "manual_ticket",
                "entry_strategy_id": None,
                "entry_evidence_ref_json": None,
                "entry_signal_id": None,
                "entry_alert_id": None,
                "entry_reference_price": None,
                "entry_action_plan_json": None,
            }
        else:
            alert = get_alert(alert_id)
            if (
                not alert or str(alert.get("code") or "") != code
                or alert.get("action") != "buy" or alert.get("status") != "triggered"
            ):
                return jsonify({"error": "买入提醒已失效或不属于该股票，请重新打开当前提醒"}), 409
            try:
                alert_deadline = datetime.fromisoformat(str(alert.get("valid_until") or ""))
                if alert_deadline.tzinfo is None:
                    alert_deadline = alert_deadline.replace(tzinfo=_SH_TZ)
            except ValueError:
                alert_deadline = None
            if alert_deadline is None or alert_deadline <= datetime.now(_SH_TZ):
                return jsonify({"error": "买入提醒缺少有效期或窗口已过期，不允许顺延或补买"}), 409
            frozen_action_plan = alert.get("action_plan")
            frozen_action = (
                (frozen_action_plan.get("trade_plan") or {}).get("action")
                if isinstance(frozen_action_plan, dict) else None
            )
            if (
                not isinstance(frozen_action, dict)
                or frozen_action.get("side") != "BUY"
                or not isinstance(frozen_action.get("shares"), int)
                or isinstance(frozen_action.get("shares"), bool)
                or int(frozen_action["shares"]) <= 0
                or int(frozen_action["shares"]) % 100
            ):
                return jsonify({
                    "error": "买入提醒尚未绑定完整的冻结数量与限价计划，请刷新后再复核"
                }), 409
            origin = alert.get("origin")
            evidence_ref_json = alert.get("evidence_ref_json")
            signal_id = alert.get("signal_id")
            strategy_id = alert.get("strategy_id")
            if (
                not isinstance(origin, str) or not origin
                or not isinstance(evidence_ref_json, str) or not evidence_ref_json
                or not isinstance(signal_id, str) or not signal_id
            ):
                return jsonify({"error": "提醒缺少不可变来源证据；可改用手工记账，不会写成策略成交"}), 409
            if origin == "production_strategy" and (
                not isinstance(strategy_id, str) or not strategy_id
            ):
                return jsonify({"error": "生产策略提醒缺少策略身份，拒绝确认"}), 409
            alert_reference_price = _finite_number(alert.get("price"))
            if alert_reference_price is None or alert_reference_price <= 0:
                return jsonify({"error": "买入提醒缺少有效参考价，拒绝确认"}), 409
            entry_context = {
                "entry_origin": origin,
                "entry_strategy_id": strategy_id,
                "entry_evidence_ref_json": evidence_ref_json,
                "entry_signal_id": signal_id,
                "entry_alert_id": int(alert["id"]),
                "entry_reference_price": alert_reference_price,
                "entry_action_plan_json": (
                    json.dumps(
                        frozen_action_plan, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"),
                    )
                ),
            }
        return jsonify({"confirmation_ticket": BUY_TICKETS.issue(
                            code, entry_context=entry_context),
                        "confirmation_required": True,
                        "next_endpoint": "/api/timing/confirm"})
    @app.post("/api/timing/confirm")
    def timing_confirm():
        """Use a short server reference plus execution facts, then recompute all gates."""
        try:
            data = _strict_json_payload(
                allowed={
                    "ticket_id", "price", "shares", "buy_fee_yuan",
                    "idempotency_key",
                },
                required={"ticket_id", "price", "shares"},
            )
            data["shares"] = _bounded_int(
                data.get("shares"), "shares", minimum=100, maximum=10_000_000,
            )
            if int(data["shares"]) % 100:
                raise ValueError("shares 必须是 100 股整数倍")
            data["price"] = _bounded_float(
                data.get("price"), "price", minimum=0.01, maximum=100_000,
            )
            if data.get("buy_fee_yuan") not in (None, ""):
                data["buy_fee_yuan"] = _bounded_float(
                    data.get("buy_fee_yuan"), "buy_fee_yuan",
                    minimum=0.0, maximum=100_000,
                )
            else:
                data.pop("buy_fee_yuan", None)
            idempotency_key = _request_idempotency_key(data)
            ticket = BUY_TICKETS.reserve(data.get("ticket_id"), idempotency_key)
            code = str(ticket["code"])
            if not CODE_PATTERN.fullmatch(code):
                raise TicketInvalid("确认票据绑定的股票代码无效")
            entry_context = ticket.get("entry_context")
            if not isinstance(entry_context, dict):
                raise TicketInvalid("确认票据缺少服务端来源上下文")
            # Internal facts derived from the opaque server reference; the
            # client can no longer submit or alter the code/score/level/action.
            data["code"] = code
            replay_position = _idempotent_position_replay(
                data, idempotency_key, action="buy_now", entry_context=entry_context,
            )
        except TicketExpired as exc:
            return jsonify({
                "error": str(exc), "ticket_endpoint": "/api/timing/ticket",
            }), 410
        except TicketConflict as exc:
            return jsonify({"error": str(exc)}), 409
        except TicketInvalid as exc:
            return jsonify({
                "error": f"{exc}；请先 POST /api/timing/ticket 获取新票据",
                "ticket_endpoint": "/api/timing/ticket",
            }), 400
        except IdempotencyConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValueError as exc:
            submitted = request.get_json(silent=True)
            if not isinstance(submitted, dict) or not submitted.get("ticket_id"):
                return jsonify({
                    "error": f"{exc}；请先 POST /api/timing/ticket 获取确认票据",
                    "ticket_endpoint": "/api/timing/ticket",
                }), 400
            return jsonify({"error": str(exc)}), 400

        if replay_position is not None:
            return jsonify({
                "position": replay_position,
                "idempotent_replay": True,
                "message": "重复提交已返回原买入记录，未再次写入",
            }), 200

        entry_alert_id = entry_context.get("entry_alert_id")
        if entry_alert_id is not None:
            live_alert = get_alert(int(entry_alert_id))
            live_action_plan_json = (
                json.dumps(
                    live_alert["action_plan"], ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                )
                if isinstance((live_alert or {}).get("action_plan"), dict)
                else None
            )
            if (
                not live_alert or live_alert.get("status") != "triggered"
                or str(live_alert.get("code") or "") != code
                or live_alert.get("action") != "buy"
                or tuple(live_alert.get(key) for key in (
                    "origin", "strategy_id", "evidence_ref_json", "signal_id",
                )) != (
                    entry_context.get("entry_origin"),
                    entry_context.get("entry_strategy_id"),
                    entry_context.get("entry_evidence_ref_json"),
                    entry_context.get("entry_signal_id"),
                )
                or live_action_plan_json
                != entry_context.get("entry_action_plan_json")
            ):
                return jsonify({"error": "提醒来源已变化或已处理，请重新确认当前买点"}), 409
        production_route_account_snapshot = None
        if entry_context.get("entry_origin") == "production_strategy":
            try:
                production_evidence = json.loads(str(
                    entry_context.get("entry_evidence_ref_json") or ""
                ))
            except (TypeError, ValueError, json.JSONDecodeError):
                production_evidence = {}
            strict_route_strategy = bool(
                isinstance(production_evidence, dict)
                and production_evidence.get("schema")
                == "strict_route_production_strategy_v1"
            )
            if strict_route_strategy:
                now_shanghai = datetime.now(_SH_TZ)
                current_context = _strict_route_alert_context(
                    _cached_strict_forward_status(),
                    decision_day=now_shanghai.date().isoformat(),
                    reference_time=now_shanghai,
                )
                current_candidates = _strict_route_opportunity_candidates(
                    current_context
                )
                production_route_account_snapshot = (
                    (production_evidence.get("strict_forward_route") or {}).get(
                        "account_snapshot_sha256"
                    )
                )
            else:
                current_candidates = _load_production_factor_candidates(limit=200)
            current_provenance = [
                _alert_provenance(code, "buy", candidate=candidate)
                for candidate in current_candidates
                if str(candidate.get("code") or "") == code
            ]
            expected_provenance = (
                entry_context.get("entry_origin"),
                entry_context.get("entry_strategy_id"),
                entry_context.get("entry_evidence_ref_json"),
                entry_context.get("entry_signal_id"),
            )
            if not any(
                tuple(current.get(key) for key in (
                    "origin", "strategy_id", "evidence_ref_json", "signal_id",
                )) == expected_provenance
                for current in current_provenance
            ):
                return jsonify({"error": "生产策略证据已变化或不再有效，请重新获取当前提醒"}), 409

        try:
            analysis, q, portfolio = app.extensions["olympus_timing_snapshot"](
                code, authoritative_gate=True,
            )
        except RuntimeError as exc:
            return jsonify({"error": str(exc), "recommended_action": "wait"}), 503
        decision_gate = analysis["decision_gate"]
        if (
            production_route_account_snapshot is not None
            and production_route_account_snapshot
            != portfolio.get("immutable_account_snapshot_sha256")
        ):
            return jsonify({
                "error": "账户持仓或现金已变化，严格路由的冻结买入数量已失效"
            }), 409
        if decision_gate.get("manual_consent") is not True:
            return jsonify({
                "error": "尚未获得手工成交记录的明确同意，禁止确认买入",
                "decision_gate": decision_gate,
            }), 423
        if analysis.get("portfolio_valuation_status") != "complete":
            return jsonify({
                "error": "持仓行情不完整，无法复核当前暴露，禁止生成买入票据",
                "missing_quote_codes": analysis.get("portfolio_missing_quote_codes") or [],
                "recommended_action": "wait",
                "manual_review_state": "waiting_for_valid_quote",
                "timing_state": "WAIT_DATA",
            }), 503
        if (analysis.get("assessment") or {}).get("manual_actionable") is not True:
            return jsonify({
                "error": "服务端重算未形成可手工复核的买点",
                "analysis": analysis,
            }), 409
        if (analysis.get("quote") or {}).get("freshness") != "live":
            return jsonify({
                "error": "行情新鲜度无法复核，禁止生成票据",
                "recommended_action": "wait",
            }), 503
        capacity = analysis["trade_capacity"]
        if capacity.get("limit_reached"):
            return jsonify({"error": "已达当日交易上限", "trade_capacity": capacity}), 409
        if capacity.get("open_positions", 0) >= capacity.get("max_open_positions", 0):
            return jsonify({"error": "已达持仓上限", "trade_capacity": capacity}), 409
        regime = analysis["market_regime"]
        if not regime.get("allow_new_positions"):
            return jsonify({"error": regime.get("reason") or "市场风控禁止新仓"}), 409
        try:
            name = _validated_display_name(analysis.get("name"), fallback=code)
        except ValueError as exc:
            return jsonify({"error": f"行情名称无效：{exc}"}), 503
        if not is_recommendable_stock(code, name):
            return jsonify({"error": "指数基金、ETF、LOF和基金不允许进入个股买入票据"}), 409
        frozen_action_plan_json = entry_context.get("entry_action_plan_json")
        if frozen_action_plan_json:
            try:
                frozen = json.loads(str(frozen_action_plan_json))
                frozen_action = frozen["trade_plan"]["action"]
                planned_shares = int(frozen_action["shares"])
                planned_limit = float(frozen_action["limit_price"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return jsonify({"error": "提醒绑定的行动计划无法复核，请重新打开当前提醒"}), 409
            if int(data["shares"]) != planned_shares:
                return jsonify({
                    "error": f"成交数量必须与提醒冻结计划一致：{planned_shares}股",
                    "planned_shares": planned_shares,
                }), 409
            if float(data["price"]) > planned_limit + 1e-9:
                return jsonify({
                    "error": f"成交价超过提醒限价 ¥{planned_limit:.2f}",
                    "limit_price": planned_limit,
                }), 409
        try:
            price, shares, available_cash = _validated_buy_facts(
                data, code, q, portfolio=portfolio,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        amount = round(price * shares, 2)
        current_market_value = _finite_number(portfolio.get("market_value"))
        current_cash = _finite_number(portfolio.get("available_cash"))
        if (
            current_market_value is None
            or current_cash is None
        ):
            return jsonify({
                "error": "当前持仓市值或账户权益无法复核，禁止生成买入票据",
                "recommended_action": "wait",
            }), 503
        current_equity = current_cash + current_market_value
        if not math.isfinite(current_equity) or current_equity <= 0:
            return jsonify({
                "error": "当前账户权益无效，禁止生成买入票据",
                "recommended_action": "wait",
            }), 503
        same_code_market_value = sum(
            float(position["market_value"])
            for position in portfolio.get("positions", [])
            if position.get("code") == code and position.get("market_value") is not None
        )
        single_limit = current_equity * MAX_SINGLE_POSITION_PCT / 100
        if same_code_market_value + amount > single_limit + 0.005:
            return jsonify({
                "error": f"交易后单股暴露超过 {MAX_SINGLE_POSITION_PCT}% 账户上限",
                "amount": amount,
                "position_after": round(same_code_market_value + amount, 2),
                "single_position_limit": round(single_limit, 2),
                "available_cash": available_cash,
            }), 409
        regime_target = _finite_number(regime.get("target_exposure_pct"))
        if regime_target is None or regime_target < 0:
            return jsonify({
                "error": "市场目标暴露无法复核，禁止生成买入票据",
                "market_regime": regime,
            }), 409
        policy_exposure_limit = min(
            float(MAX_TOTAL_EXPOSURE_PCT),
            100.0 - float(MIN_CASH_RESERVE_PCT),
        )
        effective_exposure_limit = min(policy_exposure_limit, regime_target)
        post_trade_exposure_pct = (
            (current_market_value + amount) / current_equity * 100
        )
        if post_trade_exposure_pct > effective_exposure_limit + 1e-9:
            return jsonify({
                "error": "交易后总暴露超过策略或市场 regime 上限",
                "post_trade_exposure_pct": round(post_trade_exposure_pct, 4),
                "effective_exposure_limit_pct": effective_exposure_limit,
                "policy_exposure_limit_pct": policy_exposure_limit,
                "market_target_exposure_pct": regime_target,
                "minimum_cash_reserve_pct": MIN_CASH_RESERVE_PCT,
            }), 409

        price_by_code = {
            str(row.get("code")): _finite_number(row.get("current_price"))
            for row in portfolio.get("positions", []) if row.get("code")
        }
        price_by_code[code] = _finite_number(getattr(q, "price", None))
        capacity_limit = (
            int(capacity["limit"])
            if capacity.get("limit") is not None else None
        )
        max_open_positions = int(
            capacity.get("max_open_positions") or MAX_OPEN_POSITIONS
        )
        today_key = datetime.now(_SH_TZ).date().isoformat()

        def _revalidate_ledger_before_insert(conn) -> None:
            """Final cash/capacity/exposure check inside BEGIN IMMEDIATE."""
            buys = count_risk_increasing_buys(conn, today_key)
            if capacity_limit is not None and (
                capacity_limit <= 0 or buys >= capacity_limit
            ):
                raise ValueError("并发复核：已达当日交易上限")

            rows = [
                dict(row) for row in conn.execute(
                    "SELECT * FROM timing_positions ORDER BY id"
                ).fetchall()
            ]
            open_rows = [row for row in rows if row.get("status") == "open"]
            if len(open_rows) >= max_open_positions:
                raise ValueError("并发复核：已达持仓上限")

            open_costs = [_ledger_position_cost(row) for row in open_rows]
            if any(value is None for value in open_costs):
                raise ValueError("并发复核：手工账本成本无法复核")
            cost_value = sum(float(value) for value in open_costs)
            realized = sum(
                float(row.get("pnl") or 0)
                for row in rows
            )
            cash_row = conn.execute(
                "SELECT cash_balance FROM timing_account WHERE id=1"
            ).fetchone()
            available = (
                float(cash_row["cash_balance"])
                if cash_row is not None
                else float(ACCOUNT_CAPITAL) + realized - cost_value
            )
            buy_cost = data.get("buy_fee_yuan")
            if buy_cost is None:
                buy_cost = amount * one_side_cost_rate(
                    amount, "buy", slippage=0.0,
                )
            if amount + buy_cost > available + 0.005:
                raise ValueError("并发复核：交易金额超过账本可用现金")

            current_market_value = 0.0
            same_code_market_value = 0.0
            for row in open_rows:
                row_code = str(row.get("code") or "")
                current = _finite_number(price_by_code.get(row_code))
                row_shares = _finite_number(row.get("shares"))
                if current is None or current <= 0 or row_shares is None or row_shares <= 0:
                    raise ValueError("并发复核：新增持仓导致实时估值不完整")
                value = current * row_shares
                current_market_value += value
                if row_code == code:
                    same_code_market_value += value

            current_equity = available + current_market_value
            if not math.isfinite(current_equity) or current_equity <= 0:
                raise ValueError("并发复核：当前账户权益无效")
            if (
                same_code_market_value + amount
                > current_equity * MAX_SINGLE_POSITION_PCT / 100 + 0.005
            ):
                raise ValueError("并发复核：交易后单股暴露超过账户上限")
            post_exposure = (
                current_market_value + amount
            ) / current_equity * 100
            if post_exposure > effective_exposure_limit + 1e-9:
                raise ValueError("并发复核：交易后总暴露超过策略或市场上限")

        try:
            position = confirm_buy(
                code=code, name=name, price=price, shares=shares,
                score=float(analysis["score"]),
                level=str(analysis["server_level"]),
                window_label=str((analysis["buy_window"] or {}).get("label") or ""),
                action="buy_now", idempotency_key=idempotency_key,
                pre_insert_validator=_revalidate_ledger_before_insert,
                entry_context=entry_context,
                buy_fee_yuan=data.get("buy_fee_yuan"),
                entry_reference_price=entry_context.get("entry_reference_price"),
                resolve_alert_in_transaction=entry_alert_id is not None,
                sellable_shares_snapshot=0,
                sellable_as_of=datetime.now(_SH_TZ).isoformat(timespec="seconds"),
            )
        except IdempotencyConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        replay = bool(position.get("idempotent_replay"))
        performance_scope = (
            "production_strategy"
            if entry_context.get("entry_origin") == "production_strategy"
            else "manual_research"
        )
        return jsonify({
            "position": position,
            "idempotent_replay": replay,
            "performance_scope": performance_scope,
            "message": (
                f"已确认买入 {name} {shares} 股 @ ¥{price:.2f}"
                + (
                    ""
                    if performance_scope == "production_strategy"
                    else "；已计入人工研究账本，不计入正式策略绩效"
                )
            ),
        }), 200 if replay else 201

    @app.post("/api/timing/record")
    def timing_record():
        """Record an already executed fill, independently of present entry permission."""
        try:
            data = _strict_json_payload(
                allowed={
                    "code", "name", "price", "shares", "buy_fee_yuan",
                    "idempotency_key",
                },
                required={"shares", "price"},
            )
            code = str(data.get("code") or "").strip()
            claimed_name = _validated_display_name(data.get("name"))
            if not code or not CODE_PATTERN.fullmatch(code):
                raise ValueError("请填写成交明细中的沪深6位股票代码；不从当前候选池猜测证券身份")
            data["code"] = code
            data["shares"] = _bounded_int(
                data.get("shares"), "shares", minimum=1, maximum=10_000_000,
            )
            data["price"] = _bounded_float(
                data.get("price"), "price", minimum=0.01, maximum=100_000,
            )
            if data.get("buy_fee_yuan") not in (None, ""):
                data["buy_fee_yuan"] = _bounded_float(
                    data.get("buy_fee_yuan"), "buy_fee_yuan",
                    minimum=0.0, maximum=100_000,
                )
            else:
                data.pop("buy_fee_yuan", None)
            idempotency_key = _request_idempotency_key(data)
            replay_position = _idempotent_position_replay(
                data, idempotency_key, action="manual", entry_context={
                    "entry_origin": "manual_record",
                    "entry_strategy_id": None,
                    "entry_evidence_ref_json": None,
                    "entry_signal_id": None,
                    "entry_alert_id": None,
                    "entry_reference_price": None,
                },
            )
        except IdempotencyConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        if replay_position is not None:
            return jsonify({
                "position": replay_position,
                "levels": compute_levels(
                    replay_position.get("price"), code=code, prev_close=None,
                ),
                "assessment": None,
                "warning": {"note": "仅记录用户已发生的成交，不构成建议"},
                "idempotent_replay": True,
                "message": "重复提交已返回原手工成交记录，未再次写入",
            }), 200

        # Opening policy is checked on /confirm, not on a user-reported fact.
        # Current quotes cannot validate an earlier fill or its actual quantity.
        name = claimed_name or code
        if not is_recommendable_stock(code, name):
            return jsonify({"error": "指数基金、ETF、LOF和基金不允许记入个股真钱持仓"}), 409
        price, shares = float(data["price"]), int(data["shares"])
        amount = round(price * shares, 2)
        available_cash = _finite_number((get_account_cash() or {}).get("cash_balance"))
        if available_cash is None or available_cash < 0:
            return jsonify({"error": "尚未录入可核对的真实剩余资金；不能凭默认本金记账"}), 400
        buy_cost = data.get("buy_fee_yuan")
        if buy_cost is None:
            buy_cost = amount * one_side_cost_rate(amount, "buy", slippage=0.0)
        if amount + buy_cost > available_cash + 0.005:
            return jsonify({"error": "成交金额及买入费用超过账本可用现金，请先核对资金事实"}), 400

        def _revalidate_manual_record(conn) -> None:
            # confirm_buy debits and checks sufficient cash in this same
            # transaction. Do not fall back to a guessed account capital.
            cash_row = conn.execute(
                "SELECT cash_balance FROM timing_account WHERE id=1"
            ).fetchone()
            current_cash = _finite_number(cash_row["cash_balance"]) if cash_row else None
            if current_cash is None or current_cash < 0:
                raise ValueError("并发复核：真实现金事实已缺失或无效，请重新核对")
        try:
            position = confirm_buy(
                code=code, name=name, price=price, shares=shares,
                score=0.0, level="手动记录", window_label="手动记录",
                action="manual", idempotency_key=idempotency_key,
                pre_insert_validator=_revalidate_manual_record,
                entry_context={
                    "entry_origin": "manual_record",
                    "entry_strategy_id": None,
                    "entry_evidence_ref_json": None,
                    "entry_signal_id": None,
                    "entry_alert_id": None,
                    "entry_reference_price": None,
                },
                buy_fee_yuan=data.get("buy_fee_yuan"),
                sellable_shares_snapshot=0,
                sellable_as_of=datetime.now(_SH_TZ).isoformat(timespec="seconds"),
            )
        except IdempotencyConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        levels = compute_levels(price, code=code)
        replay = bool(position.get("idempotent_replay"))
        return jsonify({
            "position": position, "levels": levels,
            "assessment": None,
            "warning": {"note": "仅记录用户已发生的成交，不构成建议"},
            "available_cash_before": available_cash,
            "idempotent_replay": replay,
            "message": f"已记录实际买入 {name} {shares} 股 @ ¥{price:.2f}；金额及费用已从账本现金扣除。行情恢复后复核持仓风险，不代表已核验买点或计划执行。",
        }), 200 if replay else 201

    register_account_fact_routes(
        app,
        strict_json_payload=_strict_json_payload,
        bounded_int=_bounded_int,
        bounded_float=_bounded_float,
        validated_display_name=_validated_display_name,
        resolve_name_to_code=lambda name: _resolve_name_to_code(name),
        request_idempotency_key=_request_idempotency_key,
        code_pattern=CODE_PATTERN,
        sh_tz=_SH_TZ,
        current_market_regime=_current_market_regime,
        market_adjusted_trade_capacity=_market_adjusted_trade_capacity,
        compact_decision_gate=_fast_compact_decision_gate,
    )

    account_nav_freeze_state = {
        "lock": threading.Lock(), "running": False,
        "trading_date": None, "attempt_count": 0,
        "last_attempt_at": 0.0, "last_result": None, "last_error": None,
    }
    app.extensions["account_nav_freeze_state"] = account_nav_freeze_state

    def _account_nav_freeze_health() -> dict:
        """Expose the one server-owned close freeze without starting work."""
        observed_at = datetime.now(_SH_TZ)
        trading_date = observed_at.date().isoformat()
        snapshots = list_account_nav_snapshots()
        latest = snapshots[-1] if snapshots else None
        with account_nav_freeze_state["lock"]:
            state = {
                "running": bool(account_nav_freeze_state["running"]),
                "trading_date": account_nav_freeze_state["trading_date"],
                "attempt_count": int(account_nav_freeze_state["attempt_count"]),
                "last_error": account_nav_freeze_state["last_error"],
            }
        calendar = trading_day_status(observed_at)
        complete_today = bool(
            latest and str(latest.get("trading_date") or "") == trading_date
        )
        if complete_today:
            status = "complete"
        elif calendar.get("is_trading_day") is not True:
            status = "not_applicable_non_trading_day"
        elif observed_at.timetz().replace(tzinfo=None) < time(15, 0):
            status = "awaiting_close"
        elif state["running"]:
            status = "freezing"
        elif state["attempt_count"] >= 15:
            status = "failed_after_max_attempts"
        elif state["last_error"]:
            status = "retrying"
        else:
            status = "awaiting_worker_tick"
        return {
            **state,
            "status": status,
            "required_trading_date": trading_date if calendar.get("is_trading_day") is True else None,
            "latest_completed_trading_date": (
                str(latest.get("trading_date") or "") if latest else None
            ),
            "automatic": True,
            "append_only": True,
        }

    app.extensions["account_nav_freeze_health"] = _account_nav_freeze_health

    def _freeze_current_day_account_nav(now: datetime) -> dict:
        """Freeze only today's completed-session account NAV, idempotently."""
        calendar = trading_day_status(now)
        if (
            calendar.get("is_trading_day") is not True
            or now.timetz().replace(tzinfo=None) < time(15, 0)
        ):
            raise ValueError("只有交易日收盘后才能冻结当日账户净值")
        trading_date = now.date().isoformat()
        existing = next(
            (
                row for row in list_account_nav_snapshots()
                if str(row.get("trading_date") or "") == trading_date
            ),
            None,
        )
        if existing is not None:
            return {**existing, "idempotent_replay": True}
        portfolio = _manual_portfolio_status(app.config["QUOTE_PROVIDER"])
        equity = _finite_number(portfolio.get("account_equity"))
        cash = _finite_number(portfolio.get("available_cash"))
        market_value = _finite_number(portfolio.get("market_value"))
        if (
            portfolio.get("valuation_status") != "complete"
            or portfolio.get("account_snapshot_concurrent_change") is True
            or portfolio.get("cash_user_recorded") is not True
            or equity is None or cash is None or market_value is None
        ):
            raise ValueError("账户现金、持仓收盘估值或账本身份不完整，未冻结净值")
        has_positions = bool(portfolio.get("positions"))
        valuation_basis = (
            str(portfolio.get("valuation_basis") or "")
            if has_positions else "cash_only"
        )
        valuation_as_of = (
            portfolio.get("valuation_as_of")
            if has_positions else now.isoformat(timespec="seconds")
        )
        if (
            has_positions
            and (
                valuation_basis != "same_day_closing_snapshot"
                or not str(valuation_as_of or "").startswith(trading_date)
            )
        ):
            raise ValueError("持仓缺少同日已完成收盘估值，未使用旧价冻结净值")
        ledger_identity = (
            (portfolio.get("ledger_identity") or {}).get("combined_sha256")
        )
        return record_account_nav_snapshot(
            trading_date=trading_date,
            account_equity_yuan=equity,
            cash_balance_yuan=cash,
            market_value_yuan=market_value,
            valuation_as_of=str(valuation_as_of or ""),
            valuation_basis=valuation_basis,
            ledger_identity_sha256=str(ledger_identity or ""),
        )

    def _account_nav_freeze_worker(observed_at: datetime) -> None:
        try:
            snapshot = _freeze_current_day_account_nav(observed_at)
            with account_nav_freeze_state["lock"]:
                account_nav_freeze_state["last_result"] = snapshot
                account_nav_freeze_state["last_error"] = None
        except Exception as exc:
            with account_nav_freeze_state["lock"]:
                account_nav_freeze_state["last_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )
        finally:
            with account_nav_freeze_state["lock"]:
                account_nav_freeze_state["running"] = False

    def _maybe_start_account_nav_freeze(
        observed_at: datetime,
        *, start_worker=_try_start_snapshot_worker,
    ) -> bool:
        """Try at most 15 same-day close freezes, one per minute."""
        if (
            trading_day_status(observed_at).get("is_trading_day") is not True
            or observed_at.timetz().replace(tzinfo=None) < time(15, 0)
        ):
            return False
        trading_date = observed_at.date().isoformat()
        if any(
            str(row.get("trading_date") or "") == trading_date
            for row in list_account_nav_snapshots()
        ):
            return False
        now_epoch = observed_at.timestamp()
        with account_nav_freeze_state["lock"]:
            if account_nav_freeze_state["trading_date"] != trading_date:
                account_nav_freeze_state.update({
                    "trading_date": trading_date, "attempt_count": 0,
                    "last_attempt_at": 0.0, "last_result": None,
                    "last_error": None,
                })
            if (
                account_nav_freeze_state["running"]
                or account_nav_freeze_state["attempt_count"] >= 15
                or now_epoch - float(
                    account_nav_freeze_state["last_attempt_at"] or 0.0
                ) < 60
            ):
                return False
            account_nav_freeze_state["attempt_count"] += 1
            account_nav_freeze_state["last_attempt_at"] = now_epoch
        try:
            started = start_worker(
                account_nav_freeze_state,
                _account_nav_freeze_worker,
                name="account-nav-close-freeze",
                args=(observed_at,),
            )
        except Exception:
            with account_nav_freeze_state["lock"]:
                account_nav_freeze_state["attempt_count"] -= 1
                account_nav_freeze_state["last_attempt_at"] = 0.0
            raise
        if not started:
            with account_nav_freeze_state["lock"]:
                account_nav_freeze_state["attempt_count"] -= 1
                account_nav_freeze_state["last_attempt_at"] = 0.0
        return bool(started)

    app.extensions["account_nav_freeze_tick"] = _maybe_start_account_nav_freeze

    @app.route("/api/timing/account-nav-snapshots", methods=["GET", "POST"])
    def timing_account_nav_snapshots():
        """Read or freeze one completed-session account equity observation."""
        if request.method == "GET":
            snapshots = list_account_nav_snapshots()
            return jsonify({
                "snapshots": snapshots, "count": len(snapshots),
                "append_only": True, "automatic_orders": False,
                "automatic_close_freeze": _account_nav_freeze_health(),
            })
        try:
            snapshot = _freeze_current_day_account_nav(datetime.now(_SH_TZ))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        replay = snapshot.get("idempotent_replay") is True
        return jsonify({
            "snapshot": snapshot,
            "message": (
                f"已冻结 {snapshot['trading_date']} 收盘账户净值 "
                f"¥{float(snapshot['account_equity_yuan']):,.2f}"
            ),
            "append_only": True,
            "automatic_orders": False,
        }), 200 if replay else 201

    @app.get("/api/timing/portfolio")
    def timing_portfolio():
        """持仓监控：时机持仓 + 实时现价 + 浮盈亏 + 距止盈/止损位置."""
        open_pos = list_positions("open")
        market_session = _audited_market_state()
        buy_window = get_buy_window()
        alerts_active = market_session.get("is_open") is True
        portfolio = _manual_portfolio_status(
            app.config["QUOTE_PROVIDER"], open_rows=open_pos,
        )
        decision_gate = _fast_compact_decision_gate()
        items = []
        exposure_pct = _portfolio_exposure_pct(portfolio)
        regime = _current_market_regime(portfolio)
        capacity = _market_adjusted_trade_capacity(regime)
        action_map = {
            int(row["position_id"]): row
            for row in position_risk_actions(
                portfolio["positions"], market_state=regime,
            )
            if row.get("position_id") is not None
        }
        sizing_risk_context = _build_position_sizing_risk_context(
            portfolio, {"position_actions": list(action_map.values())},
        )
        valued_by_id = {
            row.get("id"): row for row in portfolio["positions"]
            if row.get("id") is not None
        }
        valued_by_code = {
            row.get("code"): row for row in portfolio["positions"]
            if row.get("code")
        }
        for p in open_pos:
            valued = valued_by_id.get(p.get("id")) or valued_by_code.get(p["code"]) or {}
            cur = _finite_number(valued.get("current_price"))
            if cur is not None and cur <= 0:
                cur = None
            cost = p.get("price") or 0
            pnl = valued.get("unrealized_pnl")
            pnl_pct = valued.get("unrealized_pnl_pct")
            # 传 prev_close 让止盈2 涨停封顶口径与记录/监控一致
            lv = compute_levels(
                cost, code=p["code"],
                prev_close=valued.get("previous_close"),
            )
            risk_action = action_map.get(int(p.get("id") or 0))
            authoritative_risk = (
                risk_action.get("effective_risk_level")
                if isinstance(risk_action, dict) else None
            )
            if isinstance(authoritative_risk, dict):
                authoritative_risk = copy.deepcopy(authoritative_risk)
                lv["effective_risk_level"] = authoritative_risk
                lv["stop_loss"] = authoritative_risk.get("value")
                lv["risk_levels"] = [authoritative_risk] + [
                    row for row in (lv.get("risk_levels") or [])
                    if isinstance(row, dict) and row.get("kind") != "hard_stop"
                ]
                lv["risk_line_contract"] = (
                    "single_authoritative_frozen_position_risk_line_v2"
                )
            risk_observation = get_position_risk_state(
                int(p.get("id") or 0)
            )
            trade_plan = _position_trade_plan(
                p, risk_action, cur, quote_as_of=valued.get("quote_as_of"),
                valid_until=(
                    datetime.now(_SH_TZ) + timedelta(seconds=120)
                ).isoformat() if alerts_active else None,
                inventory_reference_at=market_session.get("server_time"),
                risk_observation=risk_observation,
            )
            items.append({
                **p,
                "current": round(cur, 3) if cur else None,
                "pnl": round(pnl, 2) if pnl is not None else None,
                "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
                "gross_unrealized_pnl": valued.get("gross_unrealized_pnl"),
                "remaining_buy_cost_yuan": valued.get("remaining_buy_cost_yuan"),
                "estimated_exit_cost_yuan": valued.get("estimated_exit_cost_yuan"),
                "pnl_semantics": valued.get("pnl_semantics"),
                "levels": lv,
                "prog": _position_progress(cur, cost, lv),
                "risk_action": risk_action,
                "risk_observation": risk_observation,
                "trade_plan": trade_plan,
                "alert_provenance": _alert_provenance(
                    str(p.get("code") or ""), "sell", position=p,
                    sell_method=str(
                        ((((trade_plan.get("action") or {}).get(
                            "evidence_identity"
                        ) or {}).get("method")) or "")
                    ),
                ),
            })
        target_pct = _finite_number(
            regime.get("risk_budget_ceiling_pct")
            if regime.get("risk_budget_ceiling_pct") is not None
            else regime.get("target_exposure_pct")
        )
        equity = _finite_number(portfolio.get("account_equity"))
        cash = _finite_number(portfolio.get("available_cash"))
        cash_pct = (
            cash / equity * 100
            if cash is not None and equity is not None and equity > 0 else None
        )
        concentrations = []
        for row in portfolio.get("positions") or []:
            value = _finite_number(row.get("market_value"))
            if value is not None and equity is not None and equity > 0:
                concentrations.append(value / equity * 100)
        max_single_pct = max(concentrations) if concentrations else 0.0
        industry_values: dict[str, float] = {}
        industry_unknown_codes: list[str] = []
        industry_unknown_value = 0.0
        for row in portfolio.get("positions") or []:
            industry = str(row.get("industry") or "").strip()
            value = _finite_number(row.get("market_value"))
            if not industry:
                industry_unknown_codes.append(str(row.get("code") or ""))
                if value is not None and value >= 0:
                    industry_unknown_value += value
            elif value is not None and value >= 0:
                industry_values[industry] = industry_values.get(industry, 0.0) + value
        max_industry_value = max(industry_values.values(), default=0.0)
        worst_case_industry_value = (
            max_industry_value + industry_unknown_value
        )
        max_industry_pct = (
            worst_case_industry_value / equity * 100.0
            if equity is not None and equity > 0 else None
        )
        known_max_industry_pct = (
            max_industry_value / equity * 100.0
            if equity is not None and equity > 0 else None
        )
        unknown_industry_pct = (
            industry_unknown_value / equity * 100.0
            if equity is not None and equity > 0 else None
        )
        existing_open_risk = _finite_number(
            sizing_risk_context.get("existing_open_risk_yuan")
        )
        open_risk_unknown_codes = list(
            sizing_risk_context.get("open_risk_unknown_codes") or []
        )
        open_risk_limit = (
            equity * MAX_PORTFOLIO_OPEN_RISK_PCT / 100.0
            if equity is not None and equity > 0 else None
        )
        exit_reviews = [
            row for row in action_map.values()
            if row.get("action") in {"离场复核", "减仓复核"}
        ]
        if portfolio.get("valuation_status") != "complete":
            portfolio_action = {
                "code": "DATA_REVIEW", "label": "先修复持仓行情",
                "reason": "持仓实时估值不完整，暂停组合级买卖与仓位结论。",
            }
        elif (
            exit_reviews
            and portfolio.get("sellable_inventory_status") != "complete"
        ):
            portfolio_action = {
                "code": "NEED_SELLABLE_INPUT",
                "label": "先核验今日可卖数量",
                "reason": "风险线已触发，但券商当日可卖库存缺失或过期；保留离场复核，不生成精确卖出数量。",
            }
        elif exit_reviews:
            portfolio_action = {
                "code": "EXIT_REVIEW", "label": "优先复核风险持仓",
                "reason": f"{len(exit_reviews)} 只持仓触发明确风险线或减仓条件；逐只人工确认数量。",
            }
        elif not portfolio.get("cash_user_recorded"):
            portfolio_action = {
                "code": "NEED_CASH_INPUT", "label": "录入真实剩余资金",
                "reason": "旧20万元仅保留为历史账本审计值，不参与仓位或买入建议。",
            }
        elif not open_pos and regime.get("allow_new_positions") is False:
            portfolio_action = {
                "code": "CASH_NO_NEW", "label": "保持现金，暂停新开仓",
                "reason": str(regime.get("reason") or "当前市场风险预算不允许新增暴露。"),
            }
        elif not open_pos:
            portfolio_action = {
                "code": "CASH_AND_SCAN", "label": "保持现金，等待合格机会",
                "reason": "当前没有已录入持仓；只有市场、板块、战法、个股、风险线、流动性和账户约束全部通过，才生成精确买入计划。",
            }
        elif (
            exposure_pct is not None and target_pct is not None
            and exposure_pct > target_pct + 1e-9
        ):
            portfolio_action = {
                "code": "REDUCE_REVIEW", "label": "复核降低总仓位",
                "reason": f"当前暴露 {exposure_pct:.1f}% 高于账户总暴露上限 {target_pct:.1f}%；仅在逐股计划给出精确数量时执行。",
            }
        elif (
            int(capacity.get("open_positions") or 0)
            >= int(capacity.get("max_open_positions") or 0)
        ):
            portfolio_action = {
                "code": "HOLD_POSITION_LIMIT", "label": "持仓已达上限，暂停新增",
                "reason": "真实持仓已完整记录；继续监控并允许减仓或退出，但不再增加新持仓。",
            }
        elif regime.get("allow_new_positions") is False:
            portfolio_action = {
                "code": "HOLD_NO_NEW", "label": "持有复核，暂停新开仓",
                "reason": str(regime.get("reason") or "当前市场状态不允许新增暴露。"),
            }
        else:
            portfolio_action = {
                "code": "HOLD_AND_SCAN", "label": "持有并等待合格机会",
                "reason": "组合未触发硬风险，新增仓位仍须通过个股、现金、集中度和市场暴露校验。",
            }
        cash_user_recorded = portfolio.get("cash_user_recorded") is True
        valuation_complete = portfolio.get("valuation_status") == "complete"
        sellable_complete = (
            portfolio.get("sellable_inventory_status") == "complete"
        )
        sellable_missing_codes = (
            portfolio.get("sellable_inventory_missing_codes") or []
        )
        account_fact_gaps = []
        if not cash_user_recorded:
            account_fact_gaps.append("真实剩余可用资金")
        if not valuation_complete:
            account_fact_gaps.append("持仓实时估值")
        if (
            int(portfolio.get("closed_count") or 0) > 0
            and _finite_number(portfolio.get("initial_equity_yuan")) is None
        ):
            account_fact_gaps.append("初始权益基准")
        if open_pos and not sellable_complete:
            account_fact_gaps.append(
                "当日可卖数量（" + "、".join(sellable_missing_codes) + "）"
                if sellable_missing_codes else "当日可卖数量"
            )
        derived_cash = portfolio.get("ledger_derived_cash_yuan")
        derived_equity = portfolio.get("ledger_derived_account_equity_yuan")
        remaining_exposure_pct = _finite_number(
            regime.get("remaining_exposure_pct")
        )
        remaining_exposure_yuan = (
            min(cash, equity * remaining_exposure_pct / 100.0)
            if cash is not None and equity is not None and equity > 0
            and remaining_exposure_pct is not None else None
        )
        structural_exposure_ceiling = min(
            float(MAX_TOTAL_EXPOSURE_PCT),
            100.0 - float(MIN_CASH_RESERVE_PCT),
        )
        total_exposure_caps = [
            {
                "id": "account_hard_cap",
                "label": "账户总仓硬上限",
                "value_pct": structural_exposure_ceiling,
                "basis": (
                    f"总仓不超过{MAX_TOTAL_EXPOSURE_PCT:g}%，"
                    f"并至少保留{MIN_CASH_RESERVE_PCT:g}%现金"
                ),
            },
        ]
        if target_pct is not None:
            total_exposure_caps.append({
                "id": "market_regime_cap",
                "label": "当前市场风险预算",
                "value_pct": max(0.0, target_pct),
                "basis": str(
                    regime.get("reason")
                    or "市场风险预算没有提供可解释依据"
                ),
            })
        binding_total_cap = min(
            total_exposure_caps, key=lambda row: float(row["value_pct"]),
        )
        for row in total_exposure_caps:
            row["binding"] = row["id"] == binding_total_cap["id"]
        position_policy = {
            "status": (
                "complete_risk_ceiling"
                if cash_user_recorded and valuation_complete
                and target_pct is not None else "incomplete"
            ),
            "model_status": "conservative_policy_not_return_optimized",
            "model_meaning": (
                "这是用于控制生存风险的保守上限，不是收益最优仓位；"
                "完整情绪—板块—战法联合回放通过前，不用研究状态放大真钱仓位。"
            ),
            "current_exposure_pct": (
                round(exposure_pct, 4) if exposure_pct is not None else None
            ),
            "live_total_exposure_ceiling_pct": round(
                float(binding_total_cap["value_pct"]), 4,
            ),
            "binding_total_exposure_constraint": binding_total_cap["id"],
            "binding_total_exposure_label": binding_total_cap["label"],
            "total_exposure_caps": total_exposure_caps,
            "executable_target_exposure_pct": None,
            "executable_target_status": "requires_bound_trade_plan",
            "executable_target_reason": (
                "总仓上限只表示最多可承受多少暴露；没有同一观察时点且绑定股数、"
                "限价、风险线、有效期和证据身份的计划时，可执行目标保持为空。"
            ),
            "candidate_level_constraints": [
                {
                    "id": "single_stock_exposure",
                    "label": "单股暴露",
                    "limit": f"≤ 权益 {MAX_SINGLE_POSITION_PCT:g}%",
                },
                {
                    "id": "single_trade_planned_loss",
                    "label": "单笔计划亏损",
                    "limit": (
                        f"≤ 权益 {MAX_PLANNED_LOSS_PER_NEW_POSITION_PCT:g}%"
                    ),
                },
                {
                    "id": "portfolio_open_risk",
                    "label": "组合计划风险",
                    "limit": f"≤ 权益 {MAX_PORTFOLIO_OPEN_RISK_PCT:g}%",
                },
                {
                    "id": "industry_exposure",
                    "label": "单行业暴露",
                    "limit": f"≤ 权益 {MAX_INDUSTRY_EXPOSURE_PCT:g}%",
                },
                {
                    "id": "trade_capacity",
                    "label": "今日新增交易",
                    "limit": f"≤ {int(capacity.get('limit') or 0)} 笔",
                },
            ],
            "candidate_specific_checks": [
                "有效风险线", "账户回撤", "组合VaR与Beta", "候选相关性",
                "行业容量", "ADV20流动性", "整手与交易成本",
            ],
            "missing_candidate_means_zero_executable_quantity": True,
            "automatic_orders": False,
        }
        summary = {
            "available_cash": (
                portfolio.get("available_cash") if cash_user_recorded else None
            ),
            "cash_user_recorded": cash_user_recorded,
            "cash_source": portfolio.get("cash_source"),
            "cash_as_of": portfolio.get("cash_as_of"),
            "initial_equity_yuan": portfolio.get("initial_equity_yuan"),
            "initial_equity_as_of": portfolio.get("initial_equity_as_of"),
            "initial_equity_source": portfolio.get("initial_equity_source"),
            "account_facts_complete": not account_fact_gaps,
            "account_fact_gaps": account_fact_gaps,
            "account_equity_authoritative": cash_user_recorded and valuation_complete,
            "sellable_inventory_status": (
                "complete" if not open_pos else portfolio.get("sellable_inventory_status")
            ),
            "sellable_inventory_missing_codes": sellable_missing_codes,
            "ledger_derived_cash_yuan": (
                derived_cash if not cash_user_recorded else None
            ),
            "ledger_derived_account_equity_yuan": (
                derived_equity if not cash_user_recorded else None
            ),
            "market_value": portfolio.get("market_value"),
            "account_equity": (
                portfolio.get("account_equity")
                if cash_user_recorded and valuation_complete else None
            ),
            "exposure_pct": (
                round(exposure_pct, 2)
                if cash_user_recorded and exposure_pct is not None else None
            ),
            "cash_pct": (
                round(cash_pct, 2)
                if cash_user_recorded and cash_pct is not None else None
            ),
            "unrealized_pnl": portfolio.get("unrealized_pnl"),
            "unrealized_pnl_pct": portfolio.get("unrealized_pnl_pct"),
            "realized_pnl": portfolio.get("realized_pnl"),
            "valuation_status": portfolio.get("valuation_status"),
            "quote_coverage_pct": portfolio.get("quote_coverage_pct"),
            "missing_quote_codes": portfolio.get("missing_quote_codes") or [],
            "market_target_exposure_pct": target_pct,
            "remaining_exposure_pct": (
                round(remaining_exposure_pct, 2)
                if remaining_exposure_pct is not None else None
            ),
            "remaining_exposure_yuan": (
                round(max(0.0, remaining_exposure_yuan), 2)
                if remaining_exposure_yuan is not None else None
            ),
            "remaining_exposure_semantics": (
                "risk_budget_ceiling_not_a_buy_recommendation"
            ),
            "max_single_exposure_pct": (
                round(max_single_pct, 2) if cash_user_recorded else None
            ),
            "single_position_limit_pct": MAX_SINGLE_POSITION_PCT,
            "position_risk_budget": {
                "status": sizing_risk_context.get("status"),
                "existing_open_risk_yuan": existing_open_risk,
                "existing_open_risk_pct": (
                    round(existing_open_risk / equity * 100.0, 4)
                    if existing_open_risk is not None
                    and equity is not None and equity > 0 else None
                ),
                "maximum_open_risk_yuan": (
                    round(open_risk_limit, 2)
                    if open_risk_limit is not None else None
                ),
                "maximum_open_risk_pct": MAX_PORTFOLIO_OPEN_RISK_PCT,
                "remaining_open_risk_yuan": (
                    round(max(0.0, open_risk_limit - existing_open_risk), 2)
                    if open_risk_limit is not None
                    and existing_open_risk is not None
                    and not open_risk_unknown_codes else None
                ),
                "unknown_codes": open_risk_unknown_codes,
                "exact_new_buy_quantity_ready": not open_risk_unknown_codes,
                "method": "current_quote_to_active_risk_line_plus_exit_cost",
                "actual_loss_can_exceed_plan": True,
            },
            "industry_risk_budget": {
                "status": (
                    "bounded_with_unknown_reserve"
                    if industry_unknown_codes else "complete"
                ),
                "method": "known_industry_plus_unknown_worst_case_reserve_v1",
                "maximum_industry_exposure_pct": MAX_INDUSTRY_EXPOSURE_PCT,
                "largest_industry_exposure_pct": (
                    round(max_industry_pct, 2)
                    if max_industry_pct is not None else None
                ),
                "known_largest_industry_exposure_pct": (
                    round(known_max_industry_pct, 2)
                    if known_max_industry_pct is not None else None
                ),
                "unknown_industry_exposure_pct": (
                    round(unknown_industry_pct, 2)
                    if unknown_industry_pct is not None else None
                ),
                "unknown_industry_market_value_yuan": round(
                    industry_unknown_value, 2,
                ),
                "unknown_exposure_reserved_against_every_candidate": True,
                "unknown_codes": industry_unknown_codes,
            },
            "new_position_risk_budget_pct": (
                MAX_PLANNED_LOSS_PER_NEW_POSITION_PCT
            ),
            "position_policy": position_policy,
            "portfolio_action": portfolio_action,
            "risk_review_count": len(exit_reviews),
            "pnl_semantics": portfolio.get("pnl_semantics"),
            "automatic_orders": False,
            "human_confirmation_required": True,
        }
        result = {"open": items, "count": len(items),
                        "summary": summary,
                        "market_regime": regime,
                        "trade_capacity": capacity,
                        "decision_gate": decision_gate,
                        "market_session": market_session,
                        "buy_window": buy_window,
                        "alerts_active": alerts_active}
        if request.args.get("view") == "planning":
            from .playbook_candidate_scan import current_snapshot
            from .planning_account_review import build as planning_account_review
            scan = current_snapshot()
            result["planning"] = {
                "candidate_scan": scan,
                "account_review": planning_account_review(
                    scan, portfolio, regime,
                    {**sizing_risk_context, "priority_exit_review": bool(exit_reviews)},
                    now=datetime.now(_SH_TZ),
                ),
            }
            result["account_snapshot_sha256"] = portfolio.get("immutable_account_snapshot_sha256")
        response = jsonify(result)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.delete("/api/timing/positions/<int:position_id>")
    def timing_position_delete(position_id: int):
        """删除一条持仓记录（记错/补录错误时用）。"""
        try:
            idempotency_key = _request_idempotency_key({})
            removed = delete_position(position_id, idempotency_key)
        except IdempotencyConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if not removed:
            return jsonify({"error": "记录不存在或已删除"}), 404
        return jsonify({
            "removed": removed,
            "idempotent_replay": bool(removed.get("idempotent_replay")),
            "message": f"已删除 {removed.get('name', '')} {removed.get('shares', 0)} 股 @ ¥{removed.get('price') or 0:.2f} 的记录",
        })

    @app.get("/api/timing/review")
    def timing_review():
        """Return the after-close review from recorded facts only."""
        from . import manual_fill_time, manual_fill_fees, manual_fill_plan
        now = datetime.now(_SH_TZ)
        review_scope = str(request.args.get("scope") or "current").strip()
        if review_scope not in {"current", "last_review"}:
            return jsonify({"error": "复盘范围无效"}), 400
        today = _timing_review_day(now, review_scope).isoformat()
        basis = request.args.get("basis", "recorded")
        if basis not in {"recorded", "execution"}:
            return jsonify({"error": "复盘时间口径无效"}), 400
        requested_day = request.args.get("date")
        if requested_day is not None:
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", requested_day):
                    raise ValueError
                selected_day = datetime.strptime(requested_day, "%Y-%m-%d").date()
                if selected_day > now.date():
                    raise ValueError
            except ValueError:
                return jsonify({"error": "复盘日期必须为已发生的 YYYY-MM-DD 日期"}), 400
            today, review_scope = selected_day.isoformat(), "date"

        # ── 今日提醒 ──────────────────────────────
        alerts = list_alerts(limit=-1, recorded_day=today) if requested_day else list_alerts(limit=120)
        raw_today_alerts = [a for a in alerts
                            if str(a.get("created_at", "")).startswith(today)]
        decision_gate = _decision_gate_status_fast()
        review_snapshot = read_account_ledger_facts(include_review=True)
        all_pos = manual_fill_fees.from_snapshot(review_snapshot["open_rows"] + review_snapshot["closed_rows"], review_snapshot)
        all_pos.sort(key=lambda p: p["id"], reverse=True)
        fee_correction_error = any(p.get("fee_correction_error") for p in all_pos)
        positions_by_id = {
            int(row["id"]): row for row in all_pos
            if isinstance(row.get("id"), int)
        }
        # 手工复核提醒是事实历史，不因策略/券商执行资格关闭而从复盘中消失。
        today_alerts = []
        for raw_alert in raw_today_alerts:
            alert = copy.deepcopy(raw_alert)
            interpretation = _position_risk_alert_interpretation(
                alert, positions_by_id,
            )
            if interpretation is not None:
                alert["timing_interpretation"] = interpretation
            today_alerts.append(alert)
        alert_counts = {"total": len(today_alerts), "triggered": 0,
                        "dismissed": 0, "confirmed": 0, "superseded": 0,
                        "expired": 0}
        unresolved = []
        for a in today_alerts:
            st = a.get("status") or "triggered"
            if st in alert_counts:
                alert_counts[st] += 1
            if st == "triggered":
                unresolved.append(a)

        # ── 今日买入 / 今日卖出 / 已实现盈亏 ────────
        open_pos = [p for p in all_pos if p["status"] == "open"]
        closed_pos = [p for p in all_pos if p["status"] == "closed"]
        created_today = [p for p in all_pos
                         if str(p.get("created_at", "") or "").startswith(today)]
        today_snapshots = [p for p in created_today if is_position_snapshot(p)]
        today_bought = [p for p in all_pos if not is_position_snapshot(p)
                        and manual_fill_time.review_time(p, "created_at", basis)[:10] == today]
        unassigned_buys = [p for p in all_pos if basis == "execution" and not is_position_snapshot(p)
                           and manual_fill_time.unassigned_may_belong(p, "created_at", today)]
        today_closed = [p for p in closed_pos
                        if str(p.get("sold_at", "") or "").startswith(today)]
        if basis == "execution":
            today_closed = [p for p in closed_pos if p.get("sell_events")
                            and all(e.get("execution_time_fact") for e in p["sell_events"])
                            and max(manual_fill_time.review_time(e, "sold_at", basis) for e in p["sell_events"])[:10] == today]
        sell_events = []
        for p in all_pos:
            for event in p.get("sell_events") or []:
                sell_events.append({
                    **event,
                    "code": p.get("code"), "name": p.get("name"),
                    "entry_price": p.get("price"),
                    "review_at": manual_fill_time.review_time(event, "sold_at", basis),
                })
        today_sell_events = [
            event for event in sell_events
            if str(event.get("review_at") or "").startswith(today)
        ]
        fill_review = manual_fill_review(all_pos, today, basis=basis)
        plan_review = [row for row in manual_fill_plan.records(all_pos, review_snapshot["plan_rows"])
                       if str(row.get("executed_at" if basis == "execution" else "recorded_at") or "")[:10] == today]
        realized_today = fill_review["net_pnl_yuan"]
        realized_all = sum(float(p.get("pnl") or 0) for p in all_pos)

        # 平仓补充：持有天数 + 盈亏率（真实日期差）
        def _parse_ts(s):
            if not s:
                return None
            try:
                d = datetime.fromisoformat(str(s))
            except ValueError:
                return None
            return d.replace(tzinfo=_SH_TZ) if d.tzinfo is None else d
        for p in closed_pos:
            d0, d1 = _parse_ts(p.get("created_at")), _parse_ts(p.get("sold_at"))
            if basis == "execution":
                d0 = _parse_ts(manual_fill_time.review_time(p, "created_at", basis))
                exit_times = [manual_fill_time.review_time(e, "sold_at", basis) for e in p.get("sell_events") or []]
                d1 = _parse_ts(max(exit_times)) if exit_times and all(exit_times) else None
            p["hold_days"] = (
                (d1 - d0).days
                if d0 and d1 and not is_position_snapshot(p) else None
            )
            initial_shares = int(p.get("initial_shares") or 0)
            events = p.get("sell_events") or []
            sold_shares_total = sum(int(event.get("shares") or 0) for event in events)
            sold_amount_total = sum(float(event.get("amount") or 0) for event in events)
            p["sold_shares_total"] = sold_shares_total or initial_shares
            p["average_sell_price"] = (
                round(sold_amount_total / sold_shares_total, 4)
                if sold_shares_total > 0 else p.get("sold_price")
            )
            original_cost = float(p.get("price") or 0) * initial_shares
            original_cost += float(p.get("buy_cost") or 0)
            p["pnl_pct"] = (
                round(float(p.get("pnl") or 0) / original_cost * 100, 2)
                if original_cost > 0 else None
            )
            cost_breakdown_complete = (
                p.get("buy_cost") is not None
                and p.get("sell_cost") is not None
                and p.get("realized_pnl_gross") is not None
            )
            p["pnl_semantics"] = (
                "net_after_recorded_fills_and_actual_fees"
                if (
                    cost_breakdown_complete
                    and p.get("buy_cost_source") == "actual_user_recorded"
                    and p.get("sell_cost_source") == "actual_user_recorded"
                )
                else "net_after_recorded_fills_and_estimated_fees"
                if cost_breakdown_complete
                else "recorded_pnl_missing_fee_breakdown"
            )

        # ── 当前持仓实时浮盈（真实行情）───────────
        portfolio_snapshot = _manual_portfolio_status(
            app.config["QUOTE_PROVIDER"],
        )
        same_account_ledger = (
            (portfolio_snapshot.get("ledger_identity") or {}).get("combined_sha256")
            == review_snapshot["ledger_identity"]["combined_sha256"]
            and portfolio_snapshot.get("account_snapshot_concurrent_change") is False
        )
        valued_by_id = {
            row.get("id"): row for row in portfolio_snapshot.get("positions", [])
        } if same_account_ledger else {}
        open_now = []
        for p in open_pos:
            valued = valued_by_id.get(p.get("id")) or {}
            open_now.append({
                **p,
                "current": valued.get("current_price"),
                "pnl": valued.get("unrealized_pnl"),
                "pnl_pct": valued.get("unrealized_pnl_pct"),
                "gross_unrealized_pnl": valued.get("gross_unrealized_pnl"),
                "remaining_buy_cost_yuan": valued.get("remaining_buy_cost_yuan"),
                "estimated_exit_cost_yuan": valued.get("estimated_exit_cost_yuan"),
                "pnl_semantics": valued.get("pnl_semantics"),
            })
        open_pnl_total = portfolio_snapshot.get("unrealized_pnl") if same_account_ledger else None
        explicit_initial_equity = _finite_number(
            portfolio_snapshot.get("initial_equity_yuan")
        )
        performance_as_of = (
            portfolio_snapshot.get("valuation_as_of")
            or portfolio_snapshot.get("cash_as_of")
        )
        account_performance = calculate_drawdown(
            portfolio_snapshot.get("positions") or [],
            realized_all,
            build_manual_nav_curve(
                all_pos, explicit_initial_equity or 0.0,
                initial_equity_as_of=portfolio_snapshot.get(
                    "initial_equity_as_of"
                ),
                current_account_equity=_finite_number(
                    portfolio_snapshot.get("account_equity")
                ),
                current_account_equity_as_of=(
                    portfolio_snapshot.get("valuation_as_of")
                    or portfolio_snapshot.get("cash_as_of")
                ),
                account_nav_snapshots=list_account_nav_snapshots(),
                account_cashflows=list_account_cashflows(),
            ),
            current_account_equity=_finite_number(
                portfolio_snapshot.get("account_equity")
            ),
            cashflow_reconciliation=account_cashflow_reconciliation_status(
                str(performance_as_of or "")[:10] or None,
            ),
        )
        # 今日买入项补上真实现价/浮盈（与 open_now 同一份行情）
        _open_by_id = {
            o.get("id"): o for o in open_now if o.get("id") is not None
        }
        # Presentation copies must not overwrite realised P&L used by closed-lot statistics.
        today_bought = [dict(p) for p in today_bought]
        today_snapshots = [dict(p) for p in today_snapshots]
        for p in today_bought:
            o = _open_by_id.get(p.get("id"))
            if o:
                p["current"] = o.get("current")
                p["pnl"] = o.get("pnl")
                p["pnl_pct"] = o.get("pnl_pct")
        for p in today_snapshots:
            o = _open_by_id.get(p.get("id"))
            if o:
                p["current"] = o.get("current")
                p["pnl"] = o.get("pnl")
                p["pnl_pct"] = o.get("pnl_pct")

        # ── 今日时间线（提醒+买卖，按时间排）──────
        timeline = []
        for a in today_alerts:
            interpretation = a.get("timing_interpretation") or {}
            alert_label = (
                f"{a.get('name')} 风险复核"
                if interpretation else
                f"{a.get('name')} {a.get('window_label') or '时机提醒'}"
            )
            timeline.append({"t": a.get("created_at"), "type": "alert",
                             "kind": a.get("action"), "status": a.get("status"),
                             "label": alert_label,
                             "timing_interpretation": interpretation or None})
        for p in today_bought:
            timeline.append({"t": manual_fill_time.review_time(p, "created_at", basis), "type": "buy", "kind": "buy",
                             "position_id": p.get("id"), "entry_alert_id": p.get("entry_alert_id"),
                             "label": f"买入 {p.get('name')} {p.get('initial_shares') or '未记录'}股 @ ¥{p.get('price') or 0:.2f}"})
        for p in today_snapshots:
            timeline.append({
                "t": p.get("created_at"), "type": "position_snapshot", "kind": "import",
                "label": (
                    f"导入已有持仓 {p.get('name')} {p.get('shares')}股 "
                    f"@ 平均成本 ¥{p.get('price') or 0:.2f}"
                ),
            })
        for event in fill_review["items"]:
            net_label = (
                f"¥{event['net_pnl_yuan']:.2f}"
                if event["net_pnl_yuan"] is not None else "待核验"
            )
            fee_label = "实录费用" if event["cost_status"] == "actual" else "含估算费用" if event["cost_status"] == "estimated" else "费用或记录不完整"
            timeline.append({
                "t": event.get("review_at") or event.get("recorded_at"), "type": "sell", "kind": "sell",
                "fill_id": event["fill_id"], "position_id": event["position_id"],
                "label": (
                    f"卖出 {event.get('name')} {event.get('shares') or '未记录'}股 "
                    f"· {fee_label} · 扣费后 {net_label}"
                ),
            })
        timeline.sort(key=lambda e: e.get("t") or "")
        for e in timeline:
            e["time"] = (e.get("t") or "")[11:16]

        # ── 市场状态（审计交易日历 + 服务器上海时间）─────────
        market_session = _audited_market_state(now)

        return jsonify({
            "date": today,
            "review_scope": review_scope,
            "time_basis": basis,
            "unassigned_buy_time_count": len(unassigned_buys),
            "market_state": "所选历史日期的情绪快照未关联，不以当前状态代替" if today != now.date().isoformat() else market_session["label"],
            "market_session": market_session,
            "alerts_count": len(today_alerts),
            "suppressed_buy_alert_count": 0,
            "alert_counts": alert_counts,
            "alerts": today_alerts if requested_day else today_alerts[:20],
            "unresolved": unresolved,
            "bought_today": today_bought,
            "bought_count": len(today_bought),
            "position_snapshots_today": today_snapshots,
            "position_snapshot_count": len(today_snapshots),
            "closed_count": len(today_closed),
            "closed": today_closed,
            "closed_all_count": len(closed_pos),
            "closed_all": closed_pos[:50],
            "sell_events": today_sell_events,
            "sell_event_count": len(today_sell_events),
            "realized_pnl": realized_today,
            "fill_review": fill_review,
            "plan_review": plan_review,
            "manual_ledger_snapshot": {**review_snapshot["review_identity"],
                "portfolio_same_ledger": same_account_ledger,
                "scope": "成交、补记时间、费用与冻结计划关联来自同一账本读取；不证明券商实收、行情或策略资格",
                "valuation_note": "当前估值与复盘账本版本一致" if same_account_ledger else "当前估值与复盘账本版本未能对齐：不拼接持仓浮盈或账户收益，请刷新重新核对"},
            "all_time_realized": None if fee_correction_error else round(realized_all, 2),
            "execution_performance": execution_performance(closed_pos),
            "open_now": open_now,
            "open_pnl_total": open_pnl_total,
            "account_performance": {"available": False, "reason": "费用更正关联的原始成交不完整或不一致，账户收益待核验"} if fee_correction_error else account_performance if same_account_ledger else {"available": False, "reason": "当前估值与复盘账本版本未对齐，账户收益待重新核验"},
            "timeline": timeline,
            "decision_gate": _compact_decision_gate(decision_gate),
        })

    @app.get("/api/timing/exit-attribution")
    def timing_exit_attribution():
        """Compare today's actual exits with fixed after-close benchmarks.

        The session high is explicitly hindsight-only.  Neither benchmark
        rewrites the alert or implies that the alternative price was tradable.
        """
        now = datetime.now(_SH_TZ)
        day = now.date().isoformat()
        events = []
        for position in list_positions():
            for event in position.get("sell_events") or []:
                if str(event.get("sold_at") or "").startswith(day):
                    events.append({
                        **event,
                        "position_id": position.get("id"),
                        "code": position.get("code"),
                        "name": position.get("name"),
                    })
        if not events:
            return jsonify({
                "date": day, "status": "no_exit", "items": [],
                "history_rewritten": False,
            })
        if now.timetz().replace(tzinfo=None) < time(15, 0):
            return jsonify({
                "date": day, "status": "pending_close", "items": [],
                "reason": "收盘前不使用形成中的价格评价卖出质量",
                "history_rewritten": False,
            })
        codes = sorted({str(event["code"]) for event in events})
        fresh_getter = getattr(
            app.config["QUOTE_PROVIDER"], "get_quotes_fresh", None,
        )
        fetch = (
            (lambda: fresh_getter(codes, maximum_age_seconds=5.0))
            if callable(fresh_getter) else
            (lambda: app.config["QUOTE_PROVIDER"].get_quotes(codes))
        )
        ok, quotes, error = _bounded_call(fetch, 0.8)
        quotes = quotes if ok and isinstance(quotes, dict) else {}
        items = []
        for event in events:
            q = quotes.get(str(event["code"]))
            close_price = _finite_number(getattr(q, "price", None))
            high_price = _finite_number(getattr(q, "high", None))
            quote_as_of = (
                getattr(q, "exchange_data_time", None)
                or getattr(q, "data_time", None)
            )
            same_day = str(quote_as_of or "").startswith(day)
            sold_price = _finite_number(event.get("price"))
            shares = int(event.get("shares") or 0)

            def comparison(price):
                if (
                    not same_day or price is None or price <= 0
                    or sold_price is None or shares <= 0
                ):
                    return None
                actual_cost = _finite_number(event.get("sell_cost"))
                alternative_notional = price * shares
                alternative_cost = alternative_notional * one_side_cost_rate(
                    alternative_notional, "sell",
                )
                return {
                    "gross_price_difference_yuan": round(
                        (price - sold_price) * shares, 2,
                    ),
                    "estimated_net_difference_yuan": (
                        round(
                            alternative_notional - alternative_cost
                            - (sold_price * shares - actual_cost), 2,
                        ) if actual_cost is not None else None
                    ),
                }

            items.append({
                "position_id": event.get("position_id"),
                "sell_event_id": event.get("id"),
                "code": event.get("code"), "name": event.get("name"),
                "sold_at": event.get("sold_at"),
                "sold_price": sold_price, "shares": shares,
                "quote_as_of": quote_as_of,
                "quote_source": getattr(q, "source", None),
                "close_price": close_price if same_day else None,
                "session_high": high_price if same_day else None,
                "versus_close": comparison(close_price),
                "versus_session_high": comparison(high_price),
                "session_high_role": (
                    "hindsight_ceiling_not_achievable_benchmark"
                ),
                "history_rewritten": False,
            })
        complete = bool(items) and all(
            row.get("versus_close") is not None for row in items
        )
        return jsonify({
            "date": day,
            "status": "complete" if complete else "quote_unavailable",
            "items": items,
            "reason": None if complete else str(error or "同日收盘行情不可用"),
            "semantics": (
                "收盘价用于固定事后归因；当日最高价仅为后视上限，"
                "不代表当时可成交或系统应当预测到"
            ),
            "history_rewritten": False,
        })

    @app.get("/api/company-profile/<code>")
    def company_profile_api(code: str):
        """公司全景档案：五道闸门 + 证据分级 + 风险否决（全部真实数据，不可得即降级）.
        首次生成约需 20-40 秒（拉取财务/主营/质押/公告四源），之后走缓存秒回."""
        if not CODE_PATTERN.match(code):
            return jsonify({"error": "无效代码", "code": code}), 400
        try:
            from .company_profile import build_company_profile
            prof = build_company_profile(code)
            # 量化侧评分：真实时机信号（与今日机会同一套引擎），非占位空参
            scoring = _profile_quant_scoring(code)
            if scoring:
                prof["conclusion"]["technical_candidate"] = scoring.get("level")
                # 质量评分用全站同一中性时机基线；实时条件分只控制入场复核。
                prof["composite"] = _quality_composite_grade(code, prof, None)
                prof["composite"].update(
                    _public_research_composite(prof["composite"])
                )
                prof["conclusion"]["verdict"] = (
                    f"{prof['composite']['tier']}（分数评级 {prof['composite']['score_rating']} · "
                    f"研究状态 {prof['composite']['action_rating']} · 综合 {prof['composite']['composite']}/100"
                    + (f"，合格线 {prof['composite']['bar_label']}" if not prof["veto"] else "，否决生效") + "）"
                )
            return jsonify(_public_score_roles(prof))
        except Exception as exc:
            return internal_error_response(
                app, "公司档案生成暂不可用", exc, extra={"code": code},
            )

    def _timing_signals_scoring(code: str, qdict: dict | None) -> dict | None:
        """计算并缓存单只六维时机信号+评分（真实行情/资金流）。
        返回 {'signals', 'scoring'}；取数/计算失败返回 None（不缓存，瞬态错误下次重试）。
        缓存键 = (code, prev_close)：prev_close 日内不变，5 分钟 K 线粒度下 300s 复用是诚实新鲜度。"""
        prev_close = None
        if qdict:
            prev_close = qdict.get("prev_close") or qdict.get("previous_close")
        key = (code, prev_close)
        with _SCORING_CACHE_LOCK:
            hit = _SCORING_CACHE.get(key)
            if hit and _time.time() - hit[0] < _SCORING_CACHE_TTL:
                return hit[1]
        try:
            candles = app.config["CANDLE_PROVIDER"].get_candles(code, 60, interval="5m")
            intraday = app.config["CANDLE_PROVIDER"].get_intraday(code)
            ff_snapshot = load_fund_flow_snapshot(code)
            # ``net_yi`` is already the single canonical, normalised amount.
            # Stale/unknown snapshots remain traceable in the snapshot payload,
            # but must not enter the timing score.
            ff_net = (
                ff_snapshot.get("net_yi")
                if ff_snapshot.get("decision_eligible") is True else None
            )
            signal_candles = completed_signal_candles(candles, "5m")
            signals = compute_timing_signals(
                signal_candles, qdict, intraday, ff_net,
            )
            result = {"signals": signals, "scoring": score_timing(signals)}
        except Exception:
            return None
        with _SCORING_CACHE_LOCK:
            _SCORING_CACHE[key] = (_time.time(), result)
        return result

    def _profile_quant_scoring(code: str, qdict: dict | None = None) -> dict | None:
        """档案页的量化侧评分：与今日机会同一套六维时机引擎（真实行情/资金流），失败返回 None.
        qdict：外部已取好的实时行情 dict（扫描池批量复用同一报价，避免每只再取一次）；
        缺省则自行单只取行情. 计算走共享 _timing_signals_scoring 缓存."""
        try:
            if qdict is None:
                quotes = app.config["QUOTE_PROVIDER"].get_quotes([code])
                q = (quotes or {}).get(code)
                qdict = {
                    "price": getattr(q, "price", None),
                    "change_pct": getattr(q, "change_pct", None),
                    "previous_close": getattr(q, "previous_close", None),
                } if q else None
            cached = _timing_signals_scoring(code, qdict)
            return cached["scoring"] if cached else None
        except Exception:
            return None

    def _profile_fundamental_scoring(code: str, co: dict | None = None,
                                     quote=None, technical_snap: dict | None = None,
                                     technical_as_of: str | None = None,
                                     market_risk: dict | None = None) -> dict | None:
        """P1b：六维基本面评分（成长/质量/估值/风险/资金）带缓存，与时机评分同层。
        数据源：归档财务(V12) + 实时估值跨截面/PIT + 档案灯号 + 当日资金流快照；
        扫描池已有审计日K时复用技术快照，不新增行情请求。co 提供档案灯号/否决；
        quote 提供当日成交额（资金相对占比口径，dict 或行情对象均可）。
        返回 workstation 六维 dims 汇总 dict；失败返回 None（调用方 fail-closed）。"""
        co = co or {}
        profile_source_identity = tuple(
            (
                source,
                str(((co.get("data_sources") or {}).get(source) or {}).get("status") or ""),
                str(((co.get("data_sources") or {}).get(source) or {}).get("fetched_at") or ""),
            )
            for source in _COMPANY_PROFILE_REFRESH_SOURCES
        )
        key = (
            "fund_scoring", code, technical_as_of or "no_technical",
            profile_source_identity,
        )
        with _SCORING_CACHE_LOCK:
            hit = _SCORING_CACHE.get(key)
            if hit and _time.time() - hit[0] < _SCORING_CACHE_TTL:
                return hit[1]
        amount = None
        amount_as_of = None
        if quote is not None:
            amount = (quote.get("amount") if isinstance(quote, dict)
                      else getattr(quote, "amount", None))
            amount_as_of = (
                quote.get("exchange_data_time") or quote.get("data_time")
                if isinstance(quote, dict) else
                getattr(quote, "exchange_data_time", None)
                or getattr(quote, "data_time", None)
            )
        lights = co.get("lights")
        reasons = co.get("reasons")
        fund_metrics = co.get("fundamental_metrics")
        gates = co.get("gates") or {}
        if not lights and gates:
            lights = {k: g.get("light") for k, g in gates.items() if isinstance(g, dict)}
        if not reasons and gates:
            reasons = {k: g.get("reasons", []) for k, g in gates.items() if isinstance(g, dict)}
        fund_metrics = fund_metrics or (gates.get("fundamentals") or {}).get("metrics")
        co_slim = {
            "lights": lights or {},
            "reasons": reasons or {},
            "gates": co.get("gates") or {},
            "veto": bool(co.get("veto")),
            "vetoed_gates": co.get("vetoed_gates") or [],
            "company_score": co.get("company_score"),
            "fundamental_metrics": fund_metrics or {},
        }
        try:
            from .workstation import profile_fundamental_scoring as _pfs
            dims = _pfs(
                code, co_slim, amount, amount_as_of=amount_as_of,
                technical_snap=technical_snap,
                technical_as_of=technical_as_of,
                market_risk=market_risk,
            )
        except Exception:
            return None
        if not dims:
            return None
        with _SCORING_CACHE_LOCK:
            _SCORING_CACHE[key] = (_time.time(), dims)
        return dims

    def _composite_grade(code: str, timing_score, co: dict | None = None,
                         quote=None, technical_snap: dict | None = None,
                         technical_as_of: str | None = None,
                         market_risk: dict | None = None) -> dict:
        """P1b 统一专业评分入口（全站唯一合成点，替代旧 0.15公司+0.85量化）。
        六维基本面(带缓存) + 明确的盘中条件输入 → workstation.compute_composite →
        apply_rating_overrides → 研究动作评级（强烈买入/买入/中性/回避/排除）。
        旧字段兼容：composite/tier/tier_key/pass_/bar；新增 rating/data_gap/confidence。
        合格线 = 中性及以上（≥50）且无否决且 data_gap 非 critical；
        基本面评分不可用 → fail-closed（pass_=False），不因数据缺失放行买入。"""
        co = co or {}
        vetoed = bool(co.get("veto"))
        from .workstation import compute_composite, apply_rating_overrides
        dims = _profile_fundamental_scoring(
            code, co, quote,
            technical_snap=technical_snap,
            technical_as_of=technical_as_of,
            market_risk=market_risk,
        )
        data_ok = dims is not None
        veto_audit = (
            dims.get("veto_audit") if data_ok else None
        ) or {
            "vetoed": vetoed,
            "reasons": co.get("vetoed_gates") or [],
            "verification_complete": False,
            "unverified_rules": ["关键否决证据未生成"],
        }
        if vetoed and not veto_audit.get("vetoed"):
            veto_audit = {
                **veto_audit,
                "vetoed": True,
                "reasons": list(veto_audit.get("reasons") or [])
                + list(co.get("vetoed_gates") or []),
            }
        if isinstance(quote, dict):
            timing_as_of = (
                quote.get("exchange_data_time") or quote.get("data_time")
                or quote.get("time")
            )
        else:
            timing_as_of = (
                getattr(quote, "exchange_data_time", None)
                or getattr(quote, "data_time", None)
            ) if quote is not None else None
        as_of_by_dimension = dict(
            (dims or {}).get("as_of_by_dimension") or {}
        )
        as_of_by_dimension["timing"] = timing_as_of
        result = apply_rating_overrides(
            compute_composite(dims["dims"] if data_ok else {},
                              timing_score=timing_score, veto=vetoed,
                              as_of_by_dimension=as_of_by_dimension),
            veto_audit,
        )
        if data_ok:
            result["data_gap"] = dims.get("data_gap")
            result["data_gap_missing"] = dims.get("data_gap_missing")
        rating = result.get("rating")
        score_rating = result.get("score_rating") or rating
        gap = result.get("data_gap")
        if vetoed:
            tier, tier_key, pass_ = "否决·排除", "red", False
        elif not data_ok:
            tier, tier_key, pass_ = "不纳入", "red", False
        elif rating in ("排除", "卖出", "回避") or gap == "critical":
            tier, tier_key, pass_ = "不纳入", "red", False
        elif rating == "中性":
            tier, tier_key, pass_ = "继续观察", "yellow", True
        else:  # 强烈买入 / 买入
            tier, tier_key, pass_ = "研究筛选通过", "green", True
        note = result.get("note") or ""
        if not data_ok:
            note = (note + "；基本面评分不可用（fail-closed），不通过买入闸门").strip("；")
        detail = copy.deepcopy(result.get("detail") or [])
        for row in detail:
            if not isinstance(row, dict):
                continue
            effective_weight = _finite_number(row.get("effective_weight"))
            component_score = _finite_number(row.get("score"))
            row["contribution"] = (
                round(effective_weight * component_score, 2)
                if effective_weight is not None and component_score is not None
                else None
            )
        return {
            "composite": result.get("composite"),
            "score_rating": score_rating,
            "action_rating": rating,
            "rating": rating,
            "tier": tier,
            "tier_key": tier_key,
            "bar": 50,
            "bar_label": "中性及以上",
            "pass_": pass_,
            "data_gap": gap,
            "data_gap_missing": result.get("data_gap_missing"),
            "confidence": result.get("confidence"),
            "company_score": co.get("company_score"),
            "veto": vetoed,
            "veto_verification_complete": veto_audit.get("verification_complete") is True,
            "unverified_veto_rules": veto_audit.get("unverified_rules") or [],
            "weight_used": result.get("weight_used"),
            "coverage_pct": result.get("coverage_pct"),
            "coverage_status": result.get("coverage_status"),
            "missing": result.get("missing") or [],
            "detail": detail,
            "as_of_audit": result.get("as_of_audit"),
            "excluded_for_freshness": result.get("excluded_for_freshness") or [],
            "note": note,
        }

    def _quality_composite_grade(
        code: str,
        co: dict | None = None,
        quote=None,
        technical_snap: dict | None = None,
        technical_as_of: str | None = None,
        market_risk: dict | None = None,
    ) -> dict:
        """全站统一股票质量分；盘中条件只决定时机，不改写质量。"""
        if quote is None:
            try:
                quote = (app.config["QUOTE_PROVIDER"].get_quotes([code]) or {}).get(code)
            except Exception:
                quote = None
        if technical_snap is None or technical_as_of is None or market_risk is None:
            context = _quality_context(code)
            technical_snap = technical_snap or context.get("technical_snap")
            technical_as_of = technical_as_of or context.get("technical_as_of")
            market_risk = market_risk or context.get("market_risk")
        result = _composite_grade(
            code,
            _QUALITY_TIMING_BASELINE,
            co,
            quote,
            technical_snap=technical_snap,
            technical_as_of=technical_as_of,
            market_risk=market_risk,
        )
        result["timing_basis"] = (
            "neutral_50_live_condition_match_used_only_for_entry"
        )
        return result

    def _quality_context(code: str) -> dict:
        """共享的已完成日 K 质量上下文；同一 5 分钟内不重复读取/计算。"""
        key = ("quality_context", code)
        with _SCORING_CACHE_LOCK:
            hit = _SCORING_CACHE.get(key)
            if hit and _time.time() - hit[0] < _SCORING_CACHE_TTL:
                return hit[1]
        context = {
            "candles": [], "technical_snap": None,
            "technical_as_of": None, "market_risk": None,
        }
        try:
            daily, audit = _audited_daily_snapshot(code, 200)
            from trade_assistant.candles import technical_snapshot
            from .workstation import _price_risk_metrics
            context = {
                "candles": daily,
                "technical_snap": technical_snapshot(daily),
                "technical_as_of": str(audit.get("as_of") or daily[-1].time),
                "market_risk": _price_risk_metrics(daily),
            }
        except (CandleDataUnavailable, IndexError, TypeError, ValueError):
            pass
        with _SCORING_CACHE_LOCK:
            _SCORING_CACHE[key] = (_time.time(), context)
        return context

    def _gated_recommendation(scoring, window, decision_gate, regime, capacity) -> str:
        """推荐动作最终门禁 —— 全站唯一真相源（/api/timing/analyze、/api/live-scan、
        /api/stock/<code>/intelligence 三处共用）。

        六维时机评分给出的 recommended_action（buy_now/wait/avoid）在对外呈现前
        再叠"决策门禁"：研究闸门、人工复核窗口、账户暴露容量、交易容量任一未过，
        一律降级为 wait。目的：同一只股票在站点各处看到的建议完全一致，
        杜绝"研究页宜买入、实时机会观察却在等待"的口径矛盾。
        本来就不是 buy_now 的动作（wait/avoid）不加门禁、原样返回。
        """
        if (scoring or {}).get("recommended_action") != "buy_now":
            return (scoring or {}).get("recommended_action", "wait")
        if (
            not (decision_gate or {}).get("recommendation_eligible")
            or not is_actionable_buy_window(window)
            or not (regime or {}).get("allow_new_positions")
            or (capacity or {}).get("limit_reached")
            or (capacity or {}).get("open_positions", 0)
            >= (capacity or {}).get("max_open_positions", 0)
        ):
            return "wait"
        return "buy_now"

    def _research_timing_payload(code, cached, window, net) -> dict:
        """Public conditions only; no ledger, account projection or execution gate refresh."""
        scoring = cached["scoring"]
        return {
            "code": code, "account_access": False, "research_only": True,
            "execution_allowed": False, "recommended_action": "wait",
            "signals": cached["signals"], "score": scoring["score"],
            "condition_match_score": scoring["score"], "unified_composite_score": None,
            "score_role": "condition_match_score_only_not_unified_stock_score",
            "score_deprecated": True, "score_deprecated_alias_of": "condition_match_score",
            "level": scoring["level"], "buy_window": window,
            "timing_evidence": {key: scoring.get(key) for key in (
                "metric_name", "metric_label", "calibrated_probability", "expected_return",
                "evidence_grade", "threshold_basis", "coverage_pct", "available_weight_pct",
                "available_signal_count", "total_signal_count", "available_signals",
                "unavailable_signals", "hit_signals", "partial_signals", "interpretation",
            )},
            "decision_gate": {"recommendation_eligible": False, "status": "research_only"},
            "market_regime": {"status": "not_evaluated", "account_access": False},
            "trade_capacity": {"status": "not_evaluated", "account_access": False},
            "ff_net": round(net, 2) if net is not None else None,
            "honesty": "账户读取关闭；仅展示行情条件，未计算账户容量、买卖股数或交易准入。条件匹配分不是胜率。",
        }

    def _stock_timing_block(code: str, quote_obj, net, *, start_gate_refresh: bool = True,
                            account_access: bool = True) -> dict | None:
        """个股研究页的实时机会观察块：与 /api/live-scan、/api/timing/analyze 同源。

        复用 _timing_signals_scoring 的 300s 共享缓存（同一 5m K 线 + 资金流），
        再用 _gated_recommendation 统一门禁。取数/计算失败返回 None（前端如实降级）。
        """
        try:
            cached = _timing_signals_scoring(code, _quote_to_dict(quote_obj, code))
            if not cached:
                return None
            scoring = cached["scoring"]
            window = get_buy_window()
            if not account_access:
                return _research_timing_payload(code, cached, window, net)
            dgate = _decision_gate_status_fast(start_refresh=start_gate_refresh)
            regime = _current_market_regime(
                _manual_portfolio_status(app.config["QUOTE_PROVIDER"]),
            )
            capacity = _market_adjusted_trade_capacity(regime)
            return {
                "signals": cached["signals"],
                "score": scoring["score"],
                "unified_composite_score": None,
                "condition_match_score": scoring["score"],
                "score_role": "condition_match_score_only_not_unified_stock_score",
                "score_deprecated": True,
                "score_deprecated_alias_of": "condition_match_score",
                "timing_evidence": {
                    key: scoring.get(key) for key in (
                        "metric_name", "metric_label", "calibrated_probability",
                        "expected_return", "evidence_grade", "threshold_basis",
                        "coverage_pct", "available_weight_pct",
                        "available_signal_count", "total_signal_count",
                        "available_signals", "unavailable_signals", "hit_signals",
                        "partial_signals", "interpretation",
                    )
                },
                "level": scoring["level"],
                "recommended_action": _gated_recommendation(
                    scoring, window, dgate, regime, capacity
                ),
                "buy_window": {
                    key: window[key] for key in (
                        "phase", "label", "note", "countdown_seconds",
                        "countdown_target", "window_action", "next_session",
                    ) if key in window
                },
                "decision_gate": _compact_decision_gate(dgate),
                "market_regime": regime,
                "trade_capacity": capacity,
                "ff_net": round(net, 2) if net is not None else None,
                "honesty": (
                    "与实时机会观察/今日机会同一时机引擎与同一门禁；"
                    "盘中条件匹配分不是胜率、收益概率或最佳买点证明；"
                    "研究闸门/人工复核窗口/账户暴露与交易容量任一未过则统一降级为 wait；"
                    "MA20/MA60市场状态仅缩放账户暴露，不改变个股评分"
                ),
            }
        except Exception:
            return None

    def _model_company_review(kronos_info: dict | None, company: dict | None,
                              timing_score: float | None = None) -> dict:
        """Second-pass research review for a model candidate.

        The model's published return fields are never normalised or capped here.
        ``model_score`` is its rank within the same snapshot (0–100), which makes
        the requested 85% model / 15% company mix meaningful without pretending
        that a raw percentage return and a company gate score share a unit.
        """
        info = kronos_info or {}
        raw_value = info.get("pred_ret_20d")
        model_score = info.get("model_score")
        if info.get("source", "kronos") != "kronos" or model_score is None:
            return {
                "ready": False, "eligible": False, "status": "non_model_source",
                "reason": "非模型候选，不参与模型-公司复核",
                "raw_model_value": raw_value, "model_score": None,
                "composite": None,
            }
        co = company or {}
        if not co.get("ready"):
            return {
                "ready": False, "eligible": False, "status": "company_pending",
                "reason": "公司侧档案生成中，暂不发出买入研究提醒",
                "raw_model_value": raw_value, "model_score": model_score,
                "composite": None,
            }
        company_raw = co.get("company_score")
        try:
            company_component = round(max(0.0, min(15.0, float(company_raw or 0) / 30 * 15)), 1)
        except (TypeError, ValueError):
            company_component = 0.0
        model_component = round(max(0.0, min(85.0, float(model_score) * _MODEL_RESEARCH_WEIGHT)), 1)
        composite = round(model_component + company_component, 1)
        veto = bool(co.get("veto"))
        timing_ok = timing_score is not None and float(timing_score) >= 40
        if veto:
            status, reason = "vetoed", "公司风险红灯，复核不通过"
        elif composite < 60:
            status, reason = "below_bar", "模型-公司复核低于研究线 60 分"
        elif not timing_ok:
            status, reason = "timing_pending", "时机评分未到 40 分，继续监控"
        else:
            status, reason = "passed", "模型、公司与时机复核通过，可发研究提醒"
        return {
            "ready": True,
            "eligible": status == "passed",
            "status": status,
            "reason": reason,
            "raw_model_value": raw_value,
            "model_score": round(float(model_score), 1),
            "model_component": model_component,
            "company_component": company_component,
            "company_score_raw": company_raw,
            "composite": composite,
            "bar": 60,
            "weights": {"model": 85, "company": 15},
            "timing_score": timing_score,
            "timing_ok": timing_ok,
        }

    # ── 实时高分扫描系统 ────────────────────────────
    # 全池(Kronos AI 看涨度 top pool) 粗筛 → 逐只 综合评分(公司侧+量化时机) →
    # 按 (综合分, AI 看涨度) 排序 → 取 Top limit，附价格评估/买卖时机/复核窗口倒计时。
    # 后台线程跑，结果缓存 TTL 90s；档案未就绪的股票不参与排序但触发后台预热，
    # 下一轮扫描自动纳入（绝不伪造评分）。

    def _quote_to_dict(qobj, code: str) -> dict:
        """行情对象 → 六字段 dict（与今日机会轮询同口径：board_limit_pct 按板块）. """
        if not qobj:
            return {}
        return {
            "price": getattr(qobj, "price", None),
            "change_pct": getattr(qobj, "change_pct", None),
            "previous_close": getattr(qobj, "previous_close", None),
            "prev_close": getattr(qobj, "previous_close", None),
            "amount": getattr(qobj, "amount", None),
            "data_time": getattr(qobj, "data_time", None),
            "exchange_data_time": getattr(qobj, "exchange_data_time", None),
            "freshness": getattr(qobj, "freshness", None),
            "board_limit_pct": 20 if str(code).startswith(("300", "301", "688", "689")) else 10,
        }

    def _live_scan_run(limit: int = 5, pool: int = 30) -> dict:
        """执行一轮实时高分扫描。返回 status=done 的结果 dict（含 items/preheating）。"""
        _t0 = _time.time()
        decision_gate = _decision_gate_status_fast(start_refresh=False)
        cands = _merge_candidates(
            load_kronos_top(limit=pool),
            _load_full_market_candidates(_FM_MERGE_LIMIT),
        )
        if not cands:
            return {"status": "error", "error": "模型研究评分数据不可用", "items": [],
                    "preheating": [], "scanned": 0, "ready_count": 0,
                    "kronos_status": kronos_status()}
        codes = [c["code"] for c in cands]
        try:
            quotes = app.config["QUOTE_PROVIDER"].get_quotes(codes) or {}
        except Exception:
            quotes = {}
        names = {
            code: _display_name_or_unavailable(getattr(q, "name", ""))
            for code, q in quotes.items()
        }
        # 个人排除名单仍生效；行业只用于分散和风险预算。
        filtered = [
            c for c in cands
            if not _is_blacklisted(c["code"], names.get(c["code"], c["code"]) or c["code"])
            and not _is_personal_excluded(c["code"])   # 个人排除名单（老邓股/已持仓）
        ]
        window = get_buy_window()
        portfolio = _manual_portfolio_status(app.config["QUOTE_PROVIDER"])
        regime = _current_market_regime(portfolio)
        available_cash = _finite_number(portfolio.get("available_cash"))
        capacity = _market_adjusted_trade_capacity(regime)
        # 主动预热：未就绪档案的股票尽快转 ready（force 跳过 1h 节流），减少漏分
        _warm_pool_profiles(
            [{"code": c["code"], "name": names.get(c["code"], c["code"])} for c in filtered])
        _t1 = _time.time()
        rows: list = []
        preheating: list = []
        unavailable: list = []
        _scan_total = len(filtered)
        with _LIVE_SCAN_STATE["lock"]:
            _LIVE_SCAN_STATE["progress"] = {
                "phase": "scanning", "done": 0, "total": _scan_total,
                "started": datetime.now(_SH_TZ).strftime("%H:%M:%S"),
            }
        for _scan_idx, cand in enumerate(filtered):
            # 逐只推进进度（页每 8s 轮询；写太频繁没有意义，5 只一跳足够顺滑）。
            if _scan_idx % 5 == 0 or _scan_idx == _scan_total - 1:
                with _LIVE_SCAN_STATE["lock"]:
                    _LIVE_SCAN_STATE["progress"] = {
                        "phase": "scanning", "done": _scan_idx + 1,
                        "total": _scan_total,
                        "started": _LIVE_SCAN_STATE["progress"].get("started"),
                    }
            code = cand["code"]
            name = names.get(code, "名称不可用")
            name_status = "unavailable" if name == "名称不可用" else "available"
            q = _quote_to_dict(quotes.get(code), code)
            freshness = str(q.get("freshness") or "unknown")
            freshness_ok = freshness == "live" or (
                freshness == "closed_snapshot"
                and not (_is_trading_day() and _is_market_open())
            )
            if not freshness_ok or not _quote_chain_valid_fields(
                q.get("price"), q.get("previous_close"), q.get("change_pct"),
            ):
                unavailable.append({
                    "code": code, "name": name, "name_status": name_status, "price": None,
                    "change_pct": None, "quant_score": None,
                    "quant_level": "行情不可用", "levels": None,
                    "price_fit": {"fit": "unknown", "label": "行情不可用"},
                    "recommended_action": "wait", "data_health": "invalid_quote",
                    "assessment": {
                        "verdict": "REJECT", "can_alert": False,
                        "failed_gates": ["quote"],
                        "reason": "实时价、昨收、涨跌幅或新鲜度无法核验",
                    },
                })
                continue
            # 公司侧评分：只读缓存命中才参与排序；未就绪 → 后台预热，本次不排（跳过时机计算提速）
            co = _company_profile_summary(code, name, q)
            if not (co and co.get("ready")):
                preheating.append({
                    "code": code, "name": name, "name_status": name_status,
                    "ai_ret_20d": cand["pred_ret_20d"],
                    "source": cand.get("source", "kronos"),
                })
                continue
            # 量化侧评分：真实六维时机（5m K线 + 资金流 + 复用批量行情）
            scoring = _profile_quant_scoring(code, q) or {
                "score": 0, "level": "数据不足", "recommended_action": "wait"
            }
            composite = _quality_composite_grade(code, co, q)
            model_review = _model_company_review(cand, co, scoring.get("score"))
            price = q.get("price")
            levels = compute_levels(price, code=code, prev_close=q.get("previous_close"))
            price_fit = _price_fit(price, account_capital=available_cash)
            # M5 统一评估：评分闸门（排除/五闸门/综合≥65/时机≥70/最佳窗口/价格/模型证据）
            # 全过且买点就绪 → manual_actionable/can_alert；券商执行资格单列。
            assessment = _assess_candidate(code, name, quotes.get(code), cand, q=q,
                                           decision_gate=decision_gate,
                                           available_cash=available_cash)
            rows.append({
                "code": code, "name": name, "name_status": name_status,
                "price": q.get("price"), "change_pct": q.get("change_pct"),
                "prev_close": q.get("previous_close"),
                "ai_ret_5d": cand["pred_ret_5d"], "ai_ret_10d": cand["pred_ret_10d"],
                "ai_ret_20d": cand["pred_ret_20d"], "ai_ret_30d": cand["pred_ret_30d"],
                "source": cand.get("source", "kronos"),
                "model_validation_status": cand.get("validation_status", "unvalidated_model_output"),
                "model_research_only": cand.get("research_only", True) is not False,
                "composite": composite,
                "model_review": model_review,
                "assessment": assessment,
                "unified_composite_score": composite.get("composite"),
                "condition_match_score": scoring.get("score"),
                "score_role": "unified_composite_primary_condition_match_entry_only",
                "quant_score": scoring.get("score"),
                "quant_score_role": "timing_gate_only_not_unified_stock_score",
                "quant_level": scoring.get("level"),
                "recommended_action": (
                    "buy_now" if assessment.get("manual_actionable") else "wait"
                ),
                "levels": levels,
                "price_fit": price_fit,
                "veto": co.get("veto"), "vetoed_gates": co.get("vetoed_gates", []),
            })
        # 模型候选按「模型横截面 85% + 公司审核 15%」优先；时机只作为通过后的执行复核。
        # 非模型动量候选没有该分，保留在其后作为独立研究来源，不冒充模型推荐。
        # 只推荐合格：红灯否决的股票不占推荐名额，单独列入"红灯拦截"区展示原因；
        # 未达合格线(<60)列入"未达合格线"区。三个区各自按综合分排序。
        # fit 排序键：good=3 / ok=2 / poor=1 / no_price=0，让"价格合适的高分股"优先浮现（不改变总分）。
        _FIT_ORDER = {"good": 3, "ok": 2, "poor": 1, "no_price": 0}
        # M5 统一排序：可预警(can_alert) > 评分闸门全过(APPROVE) > 综合分 > 价格适配。
        # 未验证模型原始收益只保留作审计字段，绝不作为同分排序器。
        # 旧 85/15 model_review.composite 排序键已退役，推荐唯一依据 = 统一评估。
        rows.sort(key=lambda r: (
            1 if r.get("assessment", {}).get("can_alert") else 0,
            1 if r.get("assessment", {}).get("verdict") == "APPROVE" else 0,
            r["composite"]["composite"], _FIT_ORDER.get(r.get("price_fit", {}).get("fit"), 0),
        ), reverse=True)
        # 三区全部由统一评估派生：推荐 Top = 评分闸门全过（APPROVE，研究通过）；
        # can_alert 表示手工复核提醒；策略/券商执行资格由 execution_eligible 单独表达。
        # 红灯拦截 = 公司五闸门否决；其余 = 未达买入门槛（某一硬闸门未过，reason 注明）。
        approved = [r for r in rows if r.get("assessment", {}).get("verdict") == "APPROVE"]
        recommended = approved[:limit]
        blocked = [r for r in rows if r["veto"]][:limit]
        below = [r for r in rows
                 if r.get("assessment", {}).get("verdict") != "APPROVE" and not r["veto"]][:limit]
        _can_alert_count = sum(1 for r in rows if r.get("assessment", {}).get("can_alert"))
        print(
            f"[live-scan] 候选 {len(cands)} 过滤后 {len(filtered)} 就绪 {len(rows)} "
            f"预热 {len(preheating)} 耗时 {_time.time()-_t0:.1f}s "
            f"(行情+预热 {_t1-_t0:.1f}s 评分 {_time.time()-_t1:.1f}s)",
            flush=True)
        return {
            "status": "done",
            "date": datetime.now(_SH_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "market_phase": _audited_market_state().get("phase"),
            "decision_gate": _compact_decision_gate(decision_gate),
            "buy_window": {
                k: window[k] for k in ("phase", "label", "note",
                                       "countdown_seconds", "countdown_target",
                                       "window_action", "next_session")
                if k in window
            },
            "kronos_status": kronos_status(),
            "scanned": len(filtered), "ready_count": len(rows),
            "preheating_count": len(preheating),
            "unavailable_count": len(unavailable),
            "can_alert_count": _can_alert_count,
            "assessment_note": (
                "APPROVE 且买点就绪时产生手工复核提醒；"
                "策略与券商执行资格不改变提醒事实，本站不会自动下单"
            ),
            "items": recommended,
            "blocked": blocked,
            "below_bar": below,
            "preheating": preheating,
            "unavailable": unavailable,
        }

    def _live_scan_worker(limit: int, pool: int) -> None:
        """后台线程：跑一轮扫描并把结果写回 _LIVE_SCAN_STATE（同一时间只有一轮在跑）."""
        _t0 = _time.time()
        try:
            result = _live_scan_run(limit, pool)
            if not isinstance(result, dict) or result.get("status") == "error":
                reason = (
                    str(result.get("error") or "实时扫描未生成可用结果")
                    if isinstance(result, dict) else "实时扫描返回结构无效"
                )
                with _LIVE_SCAN_STATE["lock"]:
                    _LIVE_SCAN_STATE["last_error"] = reason
                    _LIVE_SCAN_STATE["progress"] = {
                        **(_LIVE_SCAN_STATE.get("progress") or {}), "phase": "error",
                    }
                return
            with _LIVE_SCAN_STATE["lock"]:
                _LIVE_SCAN_STATE["result"] = result
                _LIVE_SCAN_STATE["last_ts"] = _time.time()
                _LIVE_SCAN_STATE["last_duration_s"] = _time.time() - _t0
                _LIVE_SCAN_STATE["last_error"] = None
                _scanned = result.get("scanned", 0) or 0
                _LIVE_SCAN_STATE["progress"] = {
                    "phase": "done", "done": _scanned, "total": _scanned,
                    "started": _LIVE_SCAN_STATE["progress"].get("started"),
                }
        except Exception:
            # 关键：worker 崩溃必须在状态里留痕，否则下一轮 poll 又会回到 "starting"、
            # 反复重启注定失败的扫描（曾表现为 10 分钟卡 starting 且 log 无痕）。
            error = _traceback.format_exc()
            with _LIVE_SCAN_STATE["lock"]:
                _LIVE_SCAN_STATE["last_error"] = error
                _LIVE_SCAN_STATE["progress"] = {
                    **(_LIVE_SCAN_STATE.get("progress") or {}), "phase": "error",
                }
            print(f"[live-scan] worker 异常:\n{error}", flush=True)
        finally:
            with _LIVE_SCAN_STATE["lock"]:
                _LIVE_SCAN_STATE["running"] = False

    def _live_scan_refresher_loop(stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            result = None
            if _is_trading_day():
                with _LIVE_SCAN_STATE["lock"]:
                    result = _LIVE_SCAN_STATE["result"]
                    duration = _LIVE_SCAN_STATE.get("last_duration_s") or 0
                    ttl = max(_LIVE_SCAN_TTL_S, duration * 2.5)
                    due = (result is None or bool(_LIVE_SCAN_STATE.get("last_error"))
                           or _time.time() - _LIVE_SCAN_STATE["last_ts"] >= ttl)
                    running = _LIVE_SCAN_STATE["running"]
                with _OPP_STATE["lock"]:
                    opportunities_running = _OPP_STATE["running"]
                if due and not running and not opportunities_running:
                    _try_start_snapshot_worker(
                        _LIVE_SCAN_STATE, _live_scan_worker,
                        name="live-scan-worker", args=(5, 30),
                    )
            # On a cold start the opportunity snapshot may temporarily own the
            # shared provider budget.  Recheck soon only until the first live
            # scan exists; normal steady-state polling keeps the original TTL.
            if stop_event.wait(5 if result is None else _LIVE_SCAN_TTL_S):
                break
    @app.get("/api/live-scan")
    def live_scan_api():
        """Compatibility alias for the single unified opportunity snapshot.

        The former live-scan worker duplicated quotes, company profiles and
        timing assessments already owned by ``_OPP_STATE``. Keep the public
        URL stable, but never start or serve a second ranking pipeline.
        """
        try:
            limit = min(10, max(1, int(request.args.get("limit", 5))))
        except ValueError:
            limit = 5
        try:
            pool = min(200, max(5, int(request.args.get("pool", 30))))
        except ValueError:
            pool = 30
        if limit != 5 or pool != 30:
            return jsonify({
                "error": "实时扫描使用固定服务端预算：limit=5、pool=30",
            }), 400
        return opportunities_api()

    @app.post("/api/live-scan/refresh")
    def live_scan_refresh_api():
        return _snapshot_refresh_response(
            _OPP_STATE, _opportunities_worker,
            name="opp-manual", status_endpoint="/api/live-scan",
        )
    @app.get("/live-scan")
    def live_scan_page():
        """Keep old bookmarks working; buy/sell review now lives in Today Decision."""
        return redirect("/?workspace=intraday", code=302)

    register_company_profile_routes(
        app, code_pattern=CODE_PATTERN, public_score_roles=_public_score_roles,
        strict_json_payload=_strict_json_payload,
        ensure_company_profile=lambda *args, **kwargs: _ensure_company_profile(*args, **kwargs),
        audited_stock_names=lambda: _audited_stock_names(),
        company_profile_preheat_status=lambda: _company_profile_preheat_status(),
    )
    def _allocate_capital(
        items: list, capital: float,
        market_target_exposure_pct: float | None = None,
        portfolio: dict | None = None,
        entry_context: dict | None = None,
    ) -> dict:
        return _allocate_capital_with_risk(
            items, capital, market_target_exposure_pct, portfolio, entry_context,
            risk_report_loader=get_risk_report,
            position_ledger_loader=list_positions,
            default_account_capital=ACCOUNT_CAPITAL,
        )

    app.extensions["olympus_allocate_capital"] = _allocate_capital

    @app.get("/api/timing/alloc")
    def timing_alloc():
        """Return a read-only manual sizing plan; never place an order."""
        capital_arg = request.args.get("capital")
        scenario_capital = (
            _finite_number(request.args.get("capital", type=float))
            if capital_arg is not None else None
        )
        if capital_arg is not None and scenario_capital is None:
            return jsonify({
                "error": "capital 必须是有限数字",
                "capital": None,
                "actionable_now": False,
                "automatic_orders": False,
            }), 400
        if scenario_capital is not None:
            scenario_capital = max(scenario_capital, 1000.0)
        decision_gate = _decision_gate_status_fast()
        window = get_buy_window()
        with _OPP_STATE["lock"]:
            snapshot = _OPP_STATE.get("result") or {}
        items = [
            item for item in snapshot.get("items", [])
            if (item.get("assessment") or {}).get("verdict") == "APPROVE"
        ]
        strict_forward = _cached_strict_forward_status()
        strict_latest = strict_forward.get("latest")
        strict_latest = strict_latest if isinstance(strict_latest, dict) else {}
        now_shanghai = datetime.now(_SH_TZ)
        strict_alert_context = _strict_route_alert_context(
            strict_forward,
            decision_day=now_shanghai.date().isoformat(),
            reference_time=now_shanghai,
        )
        strict_route_valid = strict_alert_context.get("route_valid") is True
        strict_route_current = strict_alert_context.get("route_current") is True
        strict_alert_ready = strict_alert_context.get("alert_ready") is True
        strict_candidate_codes = {
            str(row.get("stock_code") or row.get("code") or "").strip()
            for row in strict_latest.get("selected_candidates") or []
            if isinstance(row, dict)
        }
        strict_candidate_codes.discard("")
        portfolio = _manual_portfolio_status(app.config["QUOTE_PROVIDER"])
        route_account_snapshot_sha256 = strict_alert_context.get(
            "account_snapshot_sha256"
        )
        current_account_snapshot_sha256 = portfolio.get(
            "immutable_account_snapshot_sha256"
        )
        account_snapshot_matches = bool(
            route_account_snapshot_sha256
            and route_account_snapshot_sha256 == current_account_snapshot_sha256
        )
        strict_entry_playbooks = [
            str(value)
            for value in strict_alert_context.get("eligible_playbooks") or []
            if str(value) != "retreat_defense"
        ]
        strict_maximum_new_trades = int(
            strict_alert_context.get("maximum_new_trades") or 0
        )
        strict_budget_ready = bool(
            strict_route_valid
            and strict_route_current
            and account_snapshot_matches
            and portfolio.get("valuation_executable") is True
        )
        strict_research_plan_ready = bool(
            strict_budget_ready
            and strict_maximum_new_trades > 0
            and len(strict_entry_playbooks) == 1
            and strict_candidate_codes
        )
        allocation_items = (
            [
                item for item in items
                if str(item.get("code") or "") in strict_candidate_codes
            ][:strict_maximum_new_trades]
            if strict_research_plan_ready else [] if strict_route_valid else items
        )
        strict_account_ready = bool(
            strict_alert_ready
            and account_snapshot_matches
            and portfolio.get("valuation_executable") is True
        )
        cash_marker = portfolio.get("cash_user_recorded")
        recorded_cash = _finite_number(portfolio.get("available_cash"))
        cash_recorded = bool(
            cash_marker is True
            or (cash_marker is None and recorded_cash is not None)
        )
        account_capital = (
            _finite_number(portfolio.get("account_equity"))
            if cash_recorded and portfolio.get("valuation_status") == "complete"
            else None
        )
        if account_capital is None and cash_recorded:
            market_value = _finite_number(portfolio.get("market_value"))
            if recorded_cash is not None and market_value is not None:
                account_capital = recorded_cash + market_value
        scenario = scenario_capital is not None
        capital = scenario_capital if scenario else account_capital
        capital_semantics = {
            "mode": (
                "scenario" if scenario else
                "account_fact" if account_capital is not None else
                "missing_account_fact"
            ),
            "source": (
                "query_parameter" if scenario else
                str(portfolio.get("cash_source") or "not_recorded")
            ),
            "scenario": scenario,
            "real_account_fact": not scenario and account_capital is not None,
            "scenario_authorizes_action": False,
            "actionable": bool(account_capital is not None and strict_account_ready),
            "note": (
                (
                    "显式情景本金不是账户事实；任何可行动性仅由已录入的真实账户资金决定。"
                    if account_capital is not None else
                    "显式情景本金仅用于只读测算，不代表真实账户资金，不能产生可行动计划。"
                )
                if scenario else
                "使用已录入现金与当前持仓估值得到的账户权益。"
                if account_capital is not None else
                "尚未录入真实剩余资金，未假设默认本金。"
            ),
        }
        requested_target = request.args.get("target_exposure_pct", type=float)
        regime = _current_market_regime(portfolio)
        regime_target = _finite_number(regime.get("target_exposure_pct"))
        strict_target = (
            _finite_number(strict_alert_context.get("target_exposure_pct"))
            if strict_budget_ready else None
        )
        market_target = (
            min(regime_target, strict_target)
            if regime_target is not None and strict_target is not None
            else strict_target if strict_target is not None else regime_target
        )
        if requested_target is not None and market_target is not None:
            market_target = max(0.0, min(requested_target, market_target))
        market_session = _audited_market_state()
        decision_time = market_session.get("server_time")
        market_valid_until = None
        if market_session.get("is_open") is True and decision_time:
            try:
                market_valid_until = (
                    datetime.fromisoformat(str(decision_time))
                    + timedelta(seconds=120)
                ).isoformat()
            except ValueError:
                market_valid_until = None
        entry_valid_until = None
        if is_actionable_buy_window(window) and window.get("countdown_seconds") is not None:
            try:
                entry_valid_until = (
                    datetime.fromisoformat(window["server_time"])
                    + timedelta(seconds=int(window["countdown_seconds"]))
                ).isoformat()
            except (KeyError, TypeError, ValueError):
                entry_valid_until = None
        if capital is None:
            positions = portfolio.get("positions")
            plan = {
                "error": "尚未录入真实剩余资金，资金计划暂不可用；未生成任何交易动作",
                "plan_status": "missing_account_cash",
                "items": None,
                "capital": None,
                "total_used": None,
                "used_pct": None,
                "cash_buffer": None,
                "cash_buffer_pct": None,
                "cash_user_recorded": False,
                "account_fact_gaps": ["真实剩余可用资金"],
                "position_count": len(positions) if isinstance(positions, list) else 0,
                "max_positions": MAX_OPEN_POSITIONS,
                "portfolio_valuation_status": portfolio.get("valuation_status"),
                "missing_quote_codes": portfolio.get("missing_quote_codes") or [],
            }
        else:
            plan = _allocate_capital(
                allocation_items, capital, market_target, portfolio,
                {
                    "executable": bool(
                        account_capital is not None
                        and strict_account_ready
                        and is_actionable_buy_window(window)
                        and entry_valid_until
                    ),
                    "valid_until": entry_valid_until,
                    "market_session_executable": (
                        market_session.get("is_open") is True
                    ),
                    "decision_time": decision_time,
                    "market_valid_until": market_valid_until,
                    "cash_as_of": portfolio.get("cash_as_of"),
                },
            )
        plan["capital_semantics"] = capital_semantics
        plan["date"] = datetime.now(_SH_TZ).strftime("%Y-%m-%d %H:%M:%S")
        plan["buy_window"] = {k: window[k] for k in ("phase", "label", "countdown_seconds")}
        plan["decision_gate"] = _compact_decision_gate(decision_gate)
        plan["market_regime_applied"] = True
        plan["execution_policy_applied"] = True
        plan["market_regime"] = {
            "applied_to_execution": True,
            "target_exposure_pct": regime.get("target_exposure_pct"),
            "observed_target_exposure_pct": regime.get("observed_target_exposure_pct"),
            "risk_off": regime.get("observed_risk_off"),
            "date": regime.get("date"),
            "source": regime.get("observed_source"),
            "reason": regime.get("reason"),
            "observed_reason": regime.get("observed_reason"),
        }
        plan["account_exposure_policy"] = {
            key: regime.get(key) for key in (
                "execution_policy", "target_exposure_pct",
                "current_exposure_pct", "remaining_exposure_pct",
                "capacity_status", "allow_new_positions", "reason",
            )
        }
        plan["manual_decision_support"] = True
        plan["readonly_snapshot"] = True
        plan["side_effect"] = False
        plan["strict_forward_route"] = {
            "valid": strict_route_valid,
            "current": strict_route_current,
            "research_budget_applied": strict_budget_ready,
            "research_plan_ready": strict_research_plan_ready,
            "research_playbook_id": (
                strict_entry_playbooks[0]
                if len(strict_entry_playbooks) == 1 else None
            ),
            "eligible_playbooks": list(
                strict_alert_context.get("eligible_playbooks") or []
            ),
            "production_strategy_ready": strict_account_ready,
            "route_account_snapshot_sha256": route_account_snapshot_sha256,
            "current_account_snapshot_sha256": current_account_snapshot_sha256,
            "account_snapshot_matches": account_snapshot_matches,
            "status": strict_forward.get("status") or "unavailable",
            "packet_id": strict_latest.get("packet_id") if strict_route_valid else None,
            "slot_id": strict_latest.get("slot_id") if strict_route_valid else None,
            "selected_candidate_count": len(strict_candidate_codes),
            "maximum_new_trades": strict_maximum_new_trades,
            "required_for_manual_action": True,
            "reason": (
                strict_alert_context.get("reason")
                if not strict_alert_ready or account_snapshot_matches else
                "账户持仓或现金已变化，旧路由数量失效，等待新同槽账户快照"
            ),
        }
        has_exact_action = any(
            isinstance((row.get("trade_plan") or {}).get("action"), dict)
            for row in (plan.get("items") or []) if isinstance(row, dict)
        )
        plan["automatic_orders"] = False
        for allocation in plan.get("items") or []:
            if isinstance(allocation, dict):
                allocation["action_plan_frozen"] = False
        frozen_action_ready = False
        if (
            capital_semantics["actionable"]
            and is_actionable_buy_window(window)
            and has_exact_action
        ):
            snapshot_by_code = {
                str(row.get("code")): row
                for row in snapshot.get("items", []) if isinstance(row, dict)
            }
            for allocation in plan.get("items", []):
                source = snapshot_by_code.get(str(allocation.get("code"))) or {}
                alert = source.get("alert") or {}
                trade_plan = allocation.get("trade_plan")
                action = (
                    trade_plan.get("action")
                    if isinstance(trade_plan, dict) else None
                )
                if not (
                    alert.get("status") == "triggered" and alert.get("id")
                    and isinstance(action, dict) and action.get("side") == "BUY"
                ):
                    continue
                # GET must remain side-effect free.  The opportunity worker
                # freezes the first complete plan transactionally when it
                # records the alert; this endpoint only reads that immutable
                # ledger copy and never manufactures or updates it by polling.
                stored = get_alert(int(alert["id"])) or {}
                stored_plan = stored.get("action_plan")
                if isinstance(stored_plan, dict):
                    allocation["trade_plan"] = copy.deepcopy(
                        stored_plan.get("trade_plan")
                    )
                    for key, value in (
                        stored_plan.get("allocation") or {}
                    ).items():
                        allocation[key] = copy.deepcopy(value)
                    allocation["alert_id"] = int(alert["id"])
                    allocation["action_plan_frozen"] = True
                    frozen_action_ready = True
        plan["actionable_now"] = bool(frozen_action_ready)
        return jsonify(plan)

    def _resolve_name_to_code(name: str) -> tuple:
        """在 AI 看涨候选池内按名称/代码匹配股票（手动记录买入用）。

        候选池 = load_kronos_top(30) 的代码 + QUOTE_PROVIDER 批量取名称。
        不在池内的股票返回 (None, None)，提示用户填代码。
        """
        name = (name or "").strip()
        if CODE_PATTERN.fullmatch(name) and is_recommendable_stock(name):
            return name, None
        cands = load_kronos_top(limit=30)
        codes = [c["code"] for c in cands if is_recommendable_stock(c.get("code", ""), c.get("name", ""))]
        try:
            quotes = app.config["QUOTE_PROVIDER"].get_quotes(codes)
        except Exception:
            quotes = {}
        pool = {c: (getattr(q, "name", "") or c) for c, q in (quotes or {}).items()}
        for c, n in pool.items():
            if n == name or name in n or n in name:
                return c, n
        return None, None

    @app.post("/api/timing/sell")
    def timing_sell():
        try:
            data = _strict_json_payload(
                allowed={
                    "id", "price", "shares", "sell_fee_yuan", "alert_id",
                    "idempotency_key",
                },
                required={"id", "price"},
            )
            position_id = _bounded_int(data.get("id"), "id", minimum=1, maximum=2_147_483_647)
            sell_price = _bounded_float(
                data.get("price"), "price", minimum=0.01, maximum=100_000,
            )
            sell_shares = None
            if data.get("shares") is not None:
                sell_shares = _bounded_int(
                    data.get("shares"), "shares", minimum=1, maximum=10_000_000,
                )
            sell_fee_yuan = None
            if data.get("sell_fee_yuan") not in (None, ""):
                sell_fee_yuan = _bounded_float(
                    data.get("sell_fee_yuan"), "sell_fee_yuan",
                    minimum=0.0, maximum=100_000,
                )
            idempotency_key = _request_idempotency_key(data)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        alert = None
        sell_reference_price = None
        frozen_expected_shares = None
        current_held_shares = None
        if data.get("alert_id") is not None:
            try:
                alert_id = _bounded_int(
                    data.get("alert_id"), "alert_id", minimum=1,
                    maximum=2_147_483_647,
                )
                alert = get_alert(alert_id)
                position = next(
                    (row for row in list_positions()
                     if int(row.get("id") or 0) == position_id),
                    None,
                )
                if position is None:
                    raise ValueError("卖出提醒对应的持仓已不存在")
                current_held_shares = int(position.get("shares") or 0)
                if (
                    not alert or alert.get("action") != "sell"
                    or str(alert.get("code") or "")
                    != str(position.get("code") or "")
                ):
                    raise ValueError("卖出提醒已失效或不属于该持仓")
                alert_action = (
                    ((alert.get("action_plan") or {}).get("trade_plan") or {})
                    .get("action")
                    if isinstance(alert.get("action_plan"), dict) else None
                )
                sell_method = str(
                    ((((alert_action or {}).get("evidence_identity") or {})
                      .get("method")) or "")
                )
                expected_provenance = _alert_provenance(
                    str(position.get("code") or ""), "sell", position=position,
                    sell_method=sell_method,
                )
                provenance_keys = (
                    "origin", "strategy_id", "evidence_ref_json", "signal_id",
                )
                if tuple(alert.get(key) for key in provenance_keys) != tuple(
                    expected_provenance.get(key) for key in provenance_keys
                ):
                    raise ValueError("卖出提醒来源已变化，请重新复核当前持仓")
                if alert.get("status") == "confirmed":
                    replay = next(
                        (event for event in list_sell_events(position_id)
                         if event.get("idempotency_key") == idempotency_key),
                        None,
                    )
                    if replay is None:
                        raise ValueError("卖出提醒已确认处理")
                elif alert.get("status") != "triggered":
                    raise ValueError("卖出提醒已失效，请重新复核当前风险")
                else:
                    try:
                        deadline = datetime.fromisoformat(
                            str(alert.get("valid_until") or "")
                        )
                        if deadline.tzinfo is None:
                            deadline = deadline.replace(tzinfo=_SH_TZ)
                    except ValueError:
                        deadline = None
                    if deadline is None or deadline <= datetime.now(_SH_TZ):
                        raise ValueError("卖出提醒窗口已过期，请重新复核当前风险")
                frozen = alert.get("action_plan")
                frozen_action = (
                    (frozen.get("trade_plan") or {}).get("action")
                    if isinstance(frozen, dict) else None
                )
                if (
                    not isinstance(frozen_action, dict)
                    or frozen_action.get("side") != "SELL"
                ):
                    raise ValueError("卖出提醒缺少冻结行动计划")
                sell_reference_price = _finite_number(alert.get("price"))
                if sell_reference_price is None or sell_reference_price <= 0:
                    raise ValueError("卖出提醒缺少有效参考价")
                expected_shares = int(frozen_action.get("shares") or 0)
                frozen_expected_shares = expected_shares
                if sell_shares is None:
                    sell_shares = expected_shares
                if sell_shares > expected_shares:
                    raise ValueError(
                        f"实际成交数量不能超过冻结计划：{expected_shares} 股"
                    )
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 409
        try:
            result = sell_position(
                position_id, sell_price, idempotency_key=idempotency_key,
                shares=sell_shares, sell_fee_yuan=sell_fee_yuan,
                alert_id=(int(alert["id"]) if alert is not None else None),
                reference_price=sell_reference_price,
                resolve_alert_in_transaction=bool(
                    alert is not None
                    and sell_shares in {
                        frozen_expected_shares, current_held_shares,
                    }
                ),
            )
        except (IdempotencyConflictError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        if not result:
            return jsonify({"error": "持仓不存在或已卖出"}), 404
        event = result.get("sell_event") or {}
        sold_shares = event.get("shares") or sell_shares
        return jsonify({
            "position": result,
            "idempotent_replay": bool(result.get("idempotent_replay")),
            "message": (
                "重复提交已返回原卖出记录，未再次平仓"
                if result.get("idempotent_replay")
                else "已记录卖出"
                if result.get("status") == "closed"
                else (
                    f"已记录卖出 {sold_shares} 股，剩余 {result.get('shares', 0)} 股；"
                    + (
                        "原提醒继续跟踪剩余计划"
                        if alert is not None and sell_shares != frozen_expected_shares
                        else "本次计划已完成"
                    )
                )
            ),
        })

    @app.post("/api/timing/dismiss")
    def timing_dismiss():
        """用户忽略一条精确提醒，以追加事件留存理由。"""
        try:
            data = _strict_json_payload(
                allowed={"alert_id", "reason", "idempotency_key"},
                required={"alert_id", "reason"},
            )
            alert_id = _bounded_int(
                data.get("alert_id"), "alert_id", minimum=1,
                maximum=2_147_483_647,
            )
            reason = str(data.get("reason") or "").strip()
            if not reason or len(reason) > 256:
                raise ValueError("忽略理由必须为1至256字符")
            idempotency_key = _request_idempotency_key(data)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        try:
            event = dismiss_alert_exact(alert_id, reason, idempotency_key)
        except IdempotencyConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({
            "ok": True, "dismissed": True, "decision_event": event,
            "idempotent_replay": bool(event.get("idempotent_replay")),
            "message": "已精确忽略本次提醒，决策理由已留存",
        })

    @app.get("/api/timing/history")
    def timing_history():
        """提醒历史 + 时机持仓（开/平）。"""
        limit = request.args.get("limit", 100, type=int)
        return jsonify(list_history(limit=min(limit, 500)))

    # ── API: 今日机会（全市场多维预筛 + 固定最佳池实时监控）────
    def _position_progress(cur, cost, lv):
        """当前价在 止损→止盈2 区间的进度（0~100），用于持仓进度条."""
        if not cur or not lv:
            return None
        sl = lv.get("stop_loss") or 0
        tp2 = lv.get("take_profit_2") or 0
        if tp2 <= sl:
            return None
        return round(max(0.0, min(100.0, (cur - sl) / (tp2 - sl) * 100)), 1)

    app.extensions["olympus_position_progress"] = _position_progress

    # ── M5 统一评估流水线（2026-08-15，用户指令：所有信号/评分一体）──────────────────
    # 所有决策面（今日机会 / 实时扫描 / 模型评分卡 / 个股机会观察 / 手动记录）
    # 共用同一份 evidence，但明确区分机会质量、买点状态与自动执行资格。
    def _assess_candidate(code, name, quote=None, kronos_info=None, q=None,
                          decision_gate=None, available_cash=None) -> dict:
        """统一评估入口，把「机会质量、买点、自动执行」拆成三层。

        REJECT 只表示真实硬否决；资料缺失是 PENDING，中性机会是 WATCH。
        时机、价格和未验证模型只影响买点/观察，不再把研究质量合格的股票改写成
        硬否决。can_alert 表示手工复核提醒，券商执行资格是独立字段。
        """
        code = str(code)
        name = name or code
        quote = quote or {}
        if q is None:
            if isinstance(quote, dict):
                q = dict(quote)
            else:
                q = {
                    "price": getattr(quote, "price", None),
                    "change_pct": getattr(quote, "change_pct", None),
                    "previous_close": getattr(quote, "previous_close", None),
                    "prev_close": getattr(quote, "previous_close", None),
                    "amount": getattr(quote, "amount", None),
                    "data_time": getattr(quote, "data_time", None),
                    "exchange_data_time": getattr(
                        quote, "exchange_data_time", None,
                    ),
                }
        price = _finite_number(q.get("price"))
        if price is None or price <= 0:
            # 无实时价无法评估：快速 fail-closed PENDING，跳过公司档案/时机/资金流等慢闸门。
            # 行情源不可达时（如 2026-08-18 腾讯超时）后台预填立即给出行情不可用结论，
            # 而非每个候选空转至 25s 超时。缺价是数据状态，不是股票硬否决。
            return {
                "code": code, "name": name,
                "price": None, "change_pct": q.get("change_pct"),
                "prev_close": q.get("previous_close"),
                "gates": {"quote": {"ok": False, "detail": "实时行情不可用"}},
                "verdict": "PENDING",
                "can_alert": False,
                "manual_actionable": False,
                "execution_eligible": False,
                "action_state": "WAIT_DATA",
                "research_only": bool((decision_gate or {}).get("research_only", True)),
                "passed_gates": [],
                "failed_gates": ["quote"],
                "blocking_gates": [],
                "pending_gates": ["quote"],
                "watch_gates": [],
                "reason": "待补证：实时行情不可用，无法形成可复核机会",
                "as_of": datetime.now(_SH_TZ).isoformat(timespec="seconds"),
                "source": "统一评估流水线·professional_composite_v4_profit_sign_aware",
            }
        decision_gate = decision_gate or _decision_gate_status()
        gates: dict = {}
        blocking: list = []
        pending: list = []
        watching: list = []
        block_reasons: list = []
        pending_reasons: list = []
        watch_reasons: list = []

        def _mark(target: list, reasons: list, key: str, reason: str) -> None:
            if key not in target:
                target.append(key)
            if reason and reason not in reasons:
                reasons.append(reason)

        def _pending_error(key: str, what: str, detail: str) -> None:
            gates.setdefault(key, {})["ok"] = False
            gates[key]["detail"] = detail
            _mark(pending, pending_reasons, key, f"{what}{detail}")

        # G1 仅保留用户个人排除；行业硬黑名单兼容值恒为 false。
        try:
            _ex = _is_personal_excluded(code)
            _bl = _is_blacklisted(code, name)
            gates["exclusion"] = {"ok": not (_ex or _bl), "excluded": _ex,
                                  "blacklisted": _bl}
            if _ex or _bl:
                _mark(blocking, block_reasons, "exclusion", "个人排除名单命中")
        except Exception:
            _pending_error("exclusion", "排除名单", "检查暂不可用")

        # G2 公司证据：只有已确认红灯才否决；档案陈旧/未就绪只降低置信度。
        co = {}
        try:
            co = _company_profile_summary(code, name, q) or {}
            _veto = bool(co.get("veto"))
            _vg = co.get("vetoed_gates") or []
            gates["five_gate"] = {
                                  "ok": False if _veto else True if co.get("ready") else None,
                                  "ready": bool(co.get("ready")), "veto": _veto,
                                  "vetoed_gates": _vg,
                                  "company_score": co.get("company_score")}
            if _veto:
                _mark(blocking, block_reasons, "five_gate",
                      f"公司红灯({'、'.join(_vg) or '否决'})")
            elif not co.get("ready"):
                profile_note = (
                    "公司档案来源待刷新，未发现已确认红灯"
                    if _company_profile_needs_refresh(co)
                    else "公司档案部分核验，未发现已确认红灯"
                )
                _mark(watching, watch_reasons, "five_gate", profile_note)
        except Exception:
            _pending_error("five_gate", "公司档案", "暂不可用")

        # G3 买点时机：只决定现在行动还是等待，不改变机会质量 verdict。
        scoring = {"score": 0, "level": "数据不足", "recommended_action": "wait"}
        signals: dict = {}
        _cached = None
        try:
            _cached = _timing_signals_scoring(code, q)
            if _cached:
                signals = _cached.get("signals") or {}
                scoring = _cached.get("scoring") or scoring
        except Exception:
            _cached = None
        window = get_buy_window()
        timing_score = float(scoring.get("score") or 0)
        phase = window.get("phase")
        gates["timing"] = {
            "ok": bool(_cached) and timing_score >= 70 and is_actionable_buy_window(window),
            "score": timing_score,
            "metric_name": scoring.get("metric_name", "condition_match_score"),
            "metric_label": scoring.get("metric_label", "盘中条件匹配分"),
            "calibrated_probability": False,
            "evidence_grade": scoring.get("evidence_grade", "rule_based_unvalidated"),
            "coverage_pct": scoring.get("coverage_pct"),
            "available_signal_count": scoring.get("available_signal_count"),
            "total_signal_count": scoring.get("total_signal_count"),
            "unavailable_signals": scoring.get("unavailable_signals") or [],
            "interpretation": scoring.get("interpretation") or (
                "仅表示当前规则条件匹配度，不是胜率、收益概率或最佳买点证明"
            ),
            "level": scoring.get("level"),
            "phase": phase,
            "window_label": window.get("label", ""),
        }
        if not _cached:
            _mark(watching, watch_reasons, "timing", "买点数据不足")
        elif timing_score < 70:
            hit_count = len(scoring.get("hit_signals") or [])
            available_count = int(scoring.get("available_signal_count") or 0)
            condition_state = (
                f"盘中买点条件命中{hit_count}/{available_count}，未达复核门槛"
                if available_count > 0 else "盘中买点条件证据不足"
            )
            _mark(watching, watch_reasons, "timing", condition_state)
        elif not is_actionable_buy_window(window):
            _mark(watching, watch_reasons, "timing", f"等待更佳交易窗口({phase})")

        # G4 机会质量：买入及以上进入机会层，中性进入观察层，回避/排除不进入候选。
        comp = {}
        _rating = None
        _cscore = None
        try:
            # 当前时机只控制 WAIT_ENTRY/ENTRY_READY；机会质量用中性时机基线，
            # 避免同一盘中信号在评级、排序、买点三处重复计权。
            quality_context = _quality_context(code)
            local_technical = quality_context.get("technical_snap")
            local_technical_as_of = quality_context.get("technical_as_of")
            local_market_risk = quality_context.get("market_risk")
            quality_quote = dict(q or {})
            if quote is not None:
                for key, attr in (
                    ("price", "price"), ("change_pct", "change_pct"),
                    ("previous_close", "previous_close"), ("amount", "amount"),
                    ("data_time", "data_time"),
                    ("exchange_data_time", "exchange_data_time"),
                ):
                    value = getattr(quote, attr, None)
                    if value is not None and quality_quote.get(key) is None:
                        quality_quote[key] = value
            comp = _quality_composite_grade(
                code, co, quality_quote,
                technical_snap=local_technical,
                technical_as_of=local_technical_as_of,
                market_risk=local_market_risk,
            ) or {}
            _rating = comp.get("rating")
            _score_rating = comp.get("score_rating") or _rating
            _action_rating = comp.get("action_rating") or _rating
            _cscore = comp.get("composite")
            _comp_ok = bool(
                comp.get("pass_") is True
                and _rating in ("买入", "强烈买入")
            )
            gates["composite"] = {"ok": _comp_ok,
                                  "composite": _cscore,
                                  "score_rating": _score_rating,
                                  "action_rating": _action_rating,
                                  "rating": _rating,
                                  "tier": comp.get("tier"),
                                  "tier_key": comp.get("tier_key"), "bar": 65,
                                  "veto_verification_complete": comp.get("veto_verification_complete"),
                                  "unverified_veto_rules": comp.get("unverified_veto_rules") or [],
                                  "weight_used": comp.get("weight_used"),
                                  "coverage_pct": comp.get("coverage_pct"),
                                  "coverage_status": comp.get("coverage_status"),
                                  "missing": copy.deepcopy(comp.get("missing") or []),
                                  "data_gap": comp.get("data_gap"),
                                  "data_gap_missing": copy.deepcopy(comp.get("data_gap_missing") or []),
                                  "excluded_for_freshness": copy.deepcopy(comp.get("excluded_for_freshness") or []),
                                  "detail": copy.deepcopy(comp.get("detail") or []),
                                  "timing_basis": "neutral_50_current_timing_used_only_for_entry",
                                  "as_of_audit": comp.get("as_of_audit"),
                                  "bar_label": "买入及以上(≥65)"}
            if not _comp_ok:
                if _rating in ("回避", "排除", "卖出"):
                    _mark(blocking, block_reasons, "composite",
                          f"综合评级{_rating}({_cscore if _cscore is not None else '—'}分)")
                else:
                    _mark(watching, watch_reasons, "composite",
                          f"综合评级{_rating or '数据不足'}未到买入层")
            elif comp.get("veto_verification_complete") is not True:
                rules = "、".join(comp.get("unverified_veto_rules") or [])
                _mark(watching, watch_reasons, "composite",
                      f"风险证据待更新({rules or '来源缺失'})，未确认触发不否决")
        except Exception:
            _pending_error("composite", "综合评分", "暂不可用")

        # G5 价格适配：影响仓位/等待状态，不否决研究机会。
        pf = {}
        try:
            pf = _price_fit(price, account_capital=available_cash)
            gates["price_fit"] = {"ok": pf.get("fit") in ("good", "ok"), "fit": pf.get("fit"),
                                  "label": pf.get("label")}
            if pf.get("fit") == "poor":
                _mark(watching, watch_reasons, "price_fit",
                      f"当前预算不足一手({pf.get('label', '')})")
        except Exception:
            _pending_error("price_fit", "价格适配", "暂不可用")

        # G6 AI 模型证据：原始输出不改不归一化。模型只有在已验证、可参与
        # 决策且输入新鲜时才成为硬闸门；当前未验证研究快照只展示证据，既不
        # 冒充通过，也不否决没有模型覆盖的全市场候选。
        info = kronos_info or {}
        raw20 = info.get("pred_ret_20d")
        model_score = info.get("model_score")
        is_model = info.get("source", "kronos") == "kronos" and model_score is not None
        validation_status = str(info.get("validation_status") or "unknown")
        freshness_status = str(info.get("freshness_status") or "unknown")
        model_applied = bool(
            is_model
            and validation_status == "validated"
            and info.get("decision_eligible") is True
            and info.get("research_only") is False
            and freshness_status == "fresh"
        )
        model_pos = False
        try:
            model_pos = is_model and raw20 is not None and float(raw20) > 0
        except (TypeError, ValueError):
            model_pos = False
        gates["model"] = {"ok": model_pos if model_applied else None,
                          "source": info.get("source", "kronos"),
                          "coverage": is_model, "positive": model_pos,
                          "applied": model_applied,
                          "validation_status": validation_status,
                          "decision_eligible": info.get("decision_eligible") is True,
                          "research_only": info.get("research_only", True) is not False,
                          "freshness_status": freshness_status,
                          "status": (
                              "applied" if model_applied else
                              "not_applicable_unvalidated" if is_model else
                              "not_applicable_no_coverage"
                          ),
                          "model_score": model_score, "pred_ret_20d": raw20}
        if not is_model:
            _mark(watching, watch_reasons, "model", "无已验证模型覆盖")
        elif model_applied and not model_pos:
            _mark(watching, watch_reasons, "model",
                  f"模型旁证非正(pred_ret_20d={raw20})")

        # G7 风控：研究闸门（research_only 时不产生实际买入提示；评估仍展示）
        risk = {"research_only": bool(decision_gate.get("research_only", True)),
                "recommendation_eligible": bool(decision_gate.get("recommendation_eligible", False)),
                "manual_consent": bool(decision_gate.get("manual_consent", False)),
                "broker_orders_enabled": bool(decision_gate.get("broker_orders_enabled", False))}
        gates["risk"] = risk

        if blocking:
            verdict = "REJECT"
        elif pending:
            verdict = "PENDING"
        elif _rating in ("买入", "强烈买入"):
            verdict = "APPROVE"
        else:
            verdict = "WATCH"

        entry_ready = bool(
            verdict == "APPROVE" and _cached and timing_score >= 70
            and is_actionable_buy_window(window) and pf.get("fit") != "poor"
            and (not model_applied or model_pos)
        )
        action_state = {
            "REJECT": "BLOCKED",
            "PENDING": "WAIT_DATA",
            "WATCH": "WATCH",
        }.get(verdict, "ENTRY_READY" if entry_ready else "WAIT_ENTRY")

        # 屏幕/声音提醒只表示“手工复核买点已满足”；策略与券商资格只控制
        # execution_eligible。当前站点始终不会据此自动下单。
        can_alert = entry_ready
        execution_eligible = bool(
            entry_ready
            and risk["recommendation_eligible"]
            and risk["broker_orders_enabled"]
        )
        if verdict == "REJECT":
            reason = "已排除：" + "；".join(block_reasons)
        elif verdict == "PENDING":
            reason = "待补证：" + "；".join(pending_reasons)
        elif verdict == "WATCH":
            reason = "继续观察：" + "；".join(watch_reasons or ["综合评分未到买入层"])
        elif entry_ready:
            reason = "机会质量通过，买点条件已满足；请手工复核价格与仓位"
            if watch_reasons:
                reason += "；低置信度观察：" + "；".join(watch_reasons)
        else:
            reason = "机会质量通过，等待买点：" + "；".join(
                watch_reasons or ["尚未进入最佳交易窗口"]
            )
        # A low-confidence observation may coexist with a passed gate (for
        # example, a buy-grade composite whose veto evidence is incomplete).
        # Keep that in watch_gates, but never report the same gate as both
        # passed and failed.  failed_gates is reserved for an explicit false
        # gate plus blocking/pending failures.
        failed = list(dict.fromkeys(
            blocking + pending + [
                key for key in watching
                if (gates.get(key) or {}).get("ok") is False
            ]
        ))
        return {
            "code": code, "name": name,
            "price": price, "change_pct": q.get("change_pct"),
            "prev_close": q.get("previous_close"),
            "gates": gates,
            "verdict": verdict,
            "can_alert": can_alert,
            "manual_actionable": entry_ready,
            "execution_eligible": execution_eligible,
            "action_state": action_state,
            "research_only": risk["research_only"],
            "passed_gates": [k for k in gates if gates[k].get("ok")],
            "failed_gates": failed,
            "blocking_gates": blocking,
            "pending_gates": pending,
            "watch_gates": watching,
            "reason": reason,
            "execution_note": (
                "当前仅提供手工决策支持，不会自动下单"
                if not execution_eligible else "自动执行资格已满足"
            ),
            "as_of": datetime.now(_SH_TZ).isoformat(timespec="seconds"),
            "source": "统一评估流水线·机会质量与当前买点分离",
        }

    def _decide_alert(
        code: str, name: str, price: float | None, window: dict,
        scoring: dict, levels: dict | None, position: dict | None,
        decision_gate: dict, model_review: dict | None = None,
        risk_action: dict | None = None, price_fit: dict | None = None,
        assessment: dict | None = None,
        risk_observation: dict | None = None,
    ) -> dict:
        """判定是否触发全屏倒计时提醒（卖出优先于买入）。"""
        # 0) 非交易日门控：周六/周日休市，价格是上周五收盘价，弹"最佳买卖时间"
        #    倒计时无法执行也无意义 → 不触发任何全屏提醒（含持仓止盈/止损）。
        if not _is_trading_day():
            return {"trigger": False, "action": None,
                    "reason": "今日休市（周末），不触发买卖提醒",
                    "window_label": "", "countdown_seconds": None}
        # 1) 持仓卖出风险：两个连续竞价时段都实时监控，不依赖该股仍在模型候选池。
        #    这只是红屏+声音的人工复核提醒，绝不代替用户确认卖出。
        if position and price and levels:
            if not _is_market_open():
                return {"trigger": False, "action": None,
                        "reason": "当前非连续竞价时段；下一交易时段继续监控持仓风险线",
                        "window_label": window.get("label", ""), "countdown_seconds": None}
            # A single authoritative risk line drives alerts.  Prefer the
            # audited ATR/high-water line from risk_control; the transparent
            # entry -8% hard line is only a fallback when that report is absent.
            effective_level = (
                (risk_action or {}).get("effective_risk_level")
                or levels.get("effective_risk_level")
            )
            if effective_level is None and _finite_number(levels.get("stop_loss")) is not None:
                effective_level = {
                    "kind": "hard_stop",
                    "value": levels.get("stop_loss"),
                    "source": "legacy_entry_hard_stop",
                    "priority": 1,
                    "active": True,
                    "executable": False,
                }
            effective_value = _finite_number(
                (effective_level or {}).get("value")
            )
            if (
                effective_value is not None
                and (effective_level or {}).get("active") is True
                and price <= effective_value
            ):
                risk_label = "成本-波动风险复核线"
                risk_formula = str(
                    (effective_level or {}).get("formula") or "公式未标注"
                )
                atr_as_of = (risk_action or {}).get("atr14_as_of")
                transition = str((risk_observation or {}).get("transition") or "")
                episode_kind = str((risk_observation or {}).get("episode_kind") or "")
                if episode_kind == "existing_breach_discovered_after_import":
                    return {
                        "trigger": False, "action": None,
                        "reason": (
                            f"已导入持仓当前低于{risk_label} ¥{effective_value:.2f}；"
                            "系统不知道导入前的首次跌破时刻，仅保留普通风险复核，"
                            "不生成紧急卖出提醒或数量"
                        ),
                        "window_label": "已有风险复核",
                        "countdown_seconds": None,
                        "risk_level": effective_level,
                        "risk_observation": copy.deepcopy(risk_observation),
                    }
                elif transition == "fresh_intraday_cross":
                    timing_explanation = "盘中上一观测仍在线上，本次首次跌破，属于新风险事件"
                elif transition == "persistent_breach":
                    timing_explanation = "该价格仍处于同一次跌破过程，继续复核但不重复生成提醒"
                else:
                    timing_explanation = "首次可用观测已在线下，无法证明这是最优卖点"
                return {"trigger": True, "action": "sell",
                        "reason": (
                            f"已触及{risk_label} ¥{effective_value:.2f}，请人工复核；"
                            f"规则 {risk_formula}"
                            f"{f'，ATR数据截至 {atr_as_of}' if atr_as_of else ''}；"
                            f"{timing_explanation}；"
                            "该规则尚未完成个股退出收益校准，不代表最佳卖点，也不会自动执行"
                        ),
                        "window_label": "成本-波动风险复核", "countdown_seconds": 120,
                        "risk_level": effective_level,
                        "risk_observation": copy.deepcopy(risk_observation)}
            # 趋势/动量/支撑/RSI 等尚未绑定可复现的退出策略证据，
            # 只保留在 trade_plan.observations，不生成带倒计时的卖出动作。
            if risk_action and risk_action.get("quant_signals"):
                _q = risk_action.get("quant_signals") or []
                _labels = "、".join(s.get("label", "") for s in _q) or risk_action.get("action", "")
                return {
                    "trigger": False, "action": None,
                    "reason": (
                        f"技术观察：{_labels}。尚无精确目标股数与退出证据，"
                        "不生成卖出动作"
                    ),
                    "window_label": "技术观察", "countdown_seconds": None,
                }
        # 2) 买入：只给「最佳」且必须过统一评估。单一真相源 = assessment.manual_actionable：
        #    排除名单 + 公司五闸门无红灯 + 综合评分评级≥买入(65) + 六维时机≥70 + 人工复核窗口
        #    + 价格可整手买 + 已适用模型证据为正。策略/券商资格不隐藏手工复核提醒。
        if assessment and assessment.get("manual_actionable"):
            return {"trigger": True, "action": "buy",
                    "reason": f"统一评估通过：{assessment.get('reason', '人工复核窗口+综合评分达标')}",
                    "window_label": window.get("label", ""),
                    "countdown_seconds": window.get("countdown_seconds")}
        # 3) 观望：有评估则如实说明卡在哪道硬闸门（fail-closed 透明）；无评估（纯持仓路径）沿用时机备注
        if assessment:
            note = f"统一评估未达买入门槛：{assessment.get('reason', '继续观察')}"
        else:
            note = f"窗口：{window.get('label', '')}；信号：{scoring.get('level', '')}（{scoring.get('score', 0)}分）"
        return {"trigger": False, "action": None, "reason": note,
                "window_label": "", "countdown_seconds": window.get("countdown_seconds")}

    def _alert_valid_until(window: dict, decision: dict) -> str | None:
        if not decision.get("trigger") or decision.get("countdown_seconds") is None:
            return None
        try:
            return (
                datetime.fromisoformat(str(window["server_time"]))
                + timedelta(seconds=int(decision["countdown_seconds"]))
            ).isoformat()
        except (KeyError, TypeError, ValueError):
            return None

    def _alert_provenance(
        code: str, action: str, *, candidate: dict | None = None,
        position: dict | None = None,
        risk_observation: dict | None = None,
        sell_method: str | None = None,
    ) -> dict[str, str | None]:
        """Scope an alert to its immutable strategy or manual-risk evidence."""
        if action == "buy" and isinstance(candidate, dict):
            strict_route_provenance = _strict_route_buy_provenance(candidate)
            if strict_route_provenance is not None:
                return strict_route_provenance
            ref = candidate.get("strategy_ref")
            if (
                isinstance(ref, dict)
                and ref.get("origin") == "production_strategy"
                and ref.get("production_eligible") is True
                and all(ref.get(key) for key in (
                    "strategy_id", "strategy_spec_sha256", "catalog_version",
                    "runtime_data_key", "factor_as_of", "signal_id",
                ))
                and isinstance(ref.get("validation_evidence"), dict)
                and ref["validation_evidence"].get("path")
                and ref["validation_evidence"].get("sha256")
            ):
                evidence = {
                    key: ref.get(key) for key in (
                        "catalog_version", "factor_as_of", "runtime_data_key",
                        "strategy_id", "strategy_spec_sha256",
                    )
                }
                evidence["validation_evidence"] = {
                    key: ref["validation_evidence"].get(key)
                    for key in ("path", "sha256")
                }
                return {
                    "origin": "production_strategy",
                    "strategy_id": str(ref["strategy_id"]),
                    "evidence_ref_json": json.dumps(
                        evidence, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "signal_id": str(ref["signal_id"]),
                }
            source = str(candidate.get("source") or "research")
            source_as_of = candidate.get("source_data_as_of")
            research_ref = (
                candidate.get("strategy_ref")
                if isinstance(candidate.get("strategy_ref"), dict) else {}
            )
            evidence = {
                "code": str(code), "source": source,
                "source_data_as_of": source_as_of,
                "strategy_id": research_ref.get("strategy_id"),
                "strategy_spec_sha256": research_ref.get("strategy_spec_sha256"),
                "production_eligible": False,
            }
            return {
                "origin": (
                    "manual_strategy_observation"
                    if research_ref.get("strategy_id") else "research_observation"
                ),
                "strategy_id": research_ref.get("strategy_id"),
                "evidence_ref_json": json.dumps(
                    evidence, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ),
                "signal_id": hashlib.sha256(
                    json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
            }
        position_id = (position or {}).get("id")
        if action == "sell" and sell_method == "manual_exit_intent_v1":
            evidence = {
                "method": "manual_exit_intent_v1",
                "position_id": position_id,
                "target_shares": (position or {}).get(
                    "manual_exit_target_shares"
                ),
                "effective_date": (position or {}).get(
                    "manual_exit_effective_date"
                ),
                "profit_optimality_claim": False,
            }
            return {
                "origin": "manual_exit_intent",
                "strategy_id": "manual_exit_intent_v1",
                "evidence_ref_json": json.dumps(
                    evidence, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ),
                "signal_id": (
                    f"position:{position_id}:sell:manual_exit:"
                    f"{evidence['effective_date']}:{evidence['target_shares']}"
                ),
            }
        if action == "sell" and risk_observation is None and isinstance(position_id, int):
            risk_observation = get_position_risk_state(position_id)
        evidence = {
            "method": "position_risk_action_v2",
            "position_id": position_id,
        }
        episode = None
        if isinstance(risk_observation, dict) and risk_observation.get("episode"):
            episode = int(risk_observation["episode"])
            evidence["risk_episode"] = episode
            evidence["episode_started_at"] = risk_observation.get("episode_started_at")
            evidence["episode_kind"] = risk_observation.get("episode_kind")
            evidence["episode_risk_line"] = risk_observation.get("episode_risk_line")
        return {
            "origin": "position_risk_review",
            "strategy_id": "position_risk_action_v2",
            "evidence_ref_json": json.dumps(
                evidence, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ),
            "signal_id": (
                f"position:{position_id}:{action}:risk_episode:{episode}"
                if episode is not None else f"position:{position_id}:{action}"
            ),
        }

    def _monitor_opportunity(
        code: str, name: str, quote_obj, kronos_info: dict,
        window: dict, position: dict | None, decision_gate: dict,
        available_cash: float | None = None,
    ) -> dict:
        """单只今日机会的实时监控快照：价格/时机信号/止盈止损/提醒状态."""
        q = {
            "price": getattr(quote_obj, "price", None),
            "open_price": getattr(quote_obj, "open_price", None),
            "change_pct": getattr(quote_obj, "change_pct", None),
            "previous_close": getattr(quote_obj, "previous_close", None),
            "prev_close": getattr(quote_obj, "previous_close", None),
            "amount": getattr(quote_obj, "amount", None),
            "data_time": getattr(quote_obj, "data_time", None),
            "exchange_data_time": getattr(quote_obj, "exchange_data_time", None),
            "freshness": getattr(quote_obj, "freshness", "unknown"),
            "board_limit_pct": 20 if str(code).startswith(("300", "301", "688", "689")) else 10,
        } if quote_obj else {}
        price = _finite_number(q.get("price"))
        prev_close = _finite_number(q.get("previous_close"))
        change_pct = _finite_number(q.get("change_pct"))
        freshness = str(q.get("freshness") or "unknown")
        freshness_ok = freshness == "live" or (
            freshness == "closed_snapshot"
            and not (_is_trading_day() and _is_market_open())
        )
        quote_valid = bool(
            _quote_chain_valid_fields(price, prev_close, change_pct)
            and freshness_ok
        )
        if not quote_valid:
            assessment = {
                "code": code,
                "name": name,
                "price": None,
                "open_price": None,
                "change_pct": None,
                "prev_close": prev_close if prev_close and prev_close > 0 else None,
                "gates": {
                    "quote": {
                        "ok": False,
                        "freshness": freshness,
                        "detail": "实时行情价格、昨收或新鲜度无效",
                    },
                },
                "verdict": "PENDING",
                "can_alert": False,
                "manual_actionable": False,
                "execution_eligible": False,
                "action_state": "WAIT_DATA",
                "research_only": True,
                "passed_gates": [],
                "failed_gates": ["quote"],
                "blocking_gates": [],
                "pending_gates": ["quote"],
                "watch_gates": [],
                "reason": "待补证：实时行情无效，未进入评分、公司复核或提醒流程(fail-closed)",
                "as_of": datetime.now(_SH_TZ).isoformat(timespec="seconds"),
                "source": "quote_validity_gate",
            }
            return {
                "code": code,
                "name": name,
                "industry": str(
                    kronos_info.get("industry")
                    or kronos_info.get("_industry") or ""
                ).strip() or None,
                "price": None,
                "open_price": None,
                "change_pct": None,
                "prev_close": assessment["prev_close"],
                "kronos": kronos_info,
                "model_review": {
                    "ready": False, "eligible": False,
                    "status": "invalid_quote", "reason": assessment["reason"],
                },
                "levels": None,
                "score": None,
                "level": "行情未知",
                "recommended_action": "wait",
                "recommended_action_role": (
                    "formal_recommendation_only_not_manual_timing_state"
                ),
                "manual_review_state": "waiting_for_valid_quote",
                "timing_state": "WAIT_DATA",
                "signals": {},
                "price_fit": {"fit": "unknown", "label": "行情不可用"},
                "buy_window": {
                    "phase": window.get("phase"),
                    "label": window.get("label"),
                    "countdown_seconds": window.get("countdown_seconds"),
                },
                "position": position,
                "alert": {
                    "status": "watching", "action": None,
                    "reason": assessment["reason"], "window_label": "",
                    "countdown_seconds": None,
                },
                "company": {
                    "ready": False, "status": "invalid_quote",
                    "message": "行情无效，未调用公司复核",
                },
                "assessment": assessment,
                "data_health": "invalid_quote",
            }
        q["price"] = price
        q["previous_close"] = prev_close
        q["prev_close"] = prev_close
        q["change_pct"] = change_pct
        # 持仓股按入场价计算止盈/止损线（对应你的实际成本），未持仓按现价给参考线
        if position and position.get("price"):
            levels = compute_levels(
                position["price"], code=code, prev_close=prev_close
            )
        else:
            levels = compute_levels(price or prev_close, code=code, prev_close=prev_close)

        # 六维时机信号（复用时机引擎，走共享缓存：相同 prev_close 300s 内不重复打K源）
        scoring = {"score": 0, "level": "数据不足", "recommended_action": "wait"}
        signals: dict = {}
        cached = _timing_signals_scoring(code, q)
        if cached:
            signals = cached["signals"]
            scoring = cached["scoring"]

        company = _opportunity_company_summary(code, name, scoring, q)
        model_review = _model_company_review(kronos_info, company, scoring.get("score"))
        price_fit = _price_fit(price, available_cash)
        # 统一评估（M5）：所有推荐面共用同一份闸门证据，买入预警单一真相源
        assessment = _assess_candidate(code, name, quote_obj, kronos_info, q=q,
                                       decision_gate=decision_gate,
                                       available_cash=available_cash)
        entry_timing = _entry_timing_status(
            current_price=price,
            open_price=q.get("open_price"),
            previous_close=prev_close,
            session_executable=is_actionable_buy_window(window),
        )
        assessment["entry_timing"] = entry_timing
        if (
            position is None
            and assessment.get("manual_actionable") is True
            and entry_timing["actionable"] is not True
        ):
            assessment["manual_actionable"] = False
            assessment["can_alert"] = False
            assessment["action_state"] = "WATCH"
            assessment["reason"] = (
                f"{assessment.get('reason', '机会质量已通过')}；"
                f"买点待复核：{entry_timing['reason']}"
            )
        decision = _decide_alert(
            code, name, price, window, scoring, levels, position, decision_gate,
            model_review, price_fit=price_fit, assessment=assessment,
        )
        alert_valid_until = _alert_valid_until(window, decision)
        alert_provenance = _alert_provenance(
            code, str(decision.get("action") or "buy"), candidate=kronos_info,
            position=position,
        )
        alert = {
            "status": "watching", "action": decision["action"],
            "reason": decision["reason"], "window_label": decision["window_label"],
            "countdown_seconds": decision["countdown_seconds"],
            "valid_until": alert_valid_until,
            "provenance": alert_provenance,
        }
        if decision["trigger"]:
            # 这里只确认个股条件。提醒必须等整个候选池完成账户级分配后，
            # 与现金、整手、费用和行动计划在同一事务中写入。
            alert["status"] = "eligible_pending_plan"

        risk_gate = ((assessment.get("gates") or {}).get("risk") or {})
        formal_recommendation_ready = bool(
            assessment.get("manual_actionable") is True
            and risk_gate.get("recommendation_eligible") is True
        )

        return {
            "code": code, "name": name,
            "industry": str(
                kronos_info.get("industry")
                or kronos_info.get("_industry")
                or company.get("industry") or ""
            ).strip() or None,
            "price": price, "change_pct": change_pct,
            "open_price": _finite_number(q.get("open_price")),
            "prev_close": prev_close,
            "quote_as_of": q.get("exchange_data_time") or q.get("data_time"),
            "kronos": kronos_info,
            "model_review": model_review,
            "levels": levels,
            "score": scoring["score"],
            "condition_match_score": scoring["score"],
            "score_role": "timing_gate_only_not_unified_stock_score",
            "level": scoring["level"],
            "recommended_action": (
                "buy_now" if formal_recommendation_ready else "wait"
            ),
            "recommended_action_role": (
                "formal_recommendation_only_not_manual_timing_state"
            ),
            "manual_review_state": (
                "ready_for_manual_review"
                if assessment.get("manual_actionable") is True else "monitoring"
            ),
            "timing_state": assessment.get("action_state"),
            "signals": signals,
            "price_fit": price_fit,
            "buy_window": {
                "phase": window["phase"], "label": window["label"],
                "countdown_seconds": window["countdown_seconds"],
            },
            "position": position,
            "alert": alert,
            # 公司全景档案只读摘要：缓存命中才返回，不阻塞轮询。
            # 红灯闸门 → veto=True（禁止生成交易草稿）；无缓存 → ready=False（前端提示先生成）
            "company": company,
            # 统一评估（M5）：verdict/can_alert/各闸门/失败原因——前端徽章与排序唯一依据
            "assessment": assessment,
            "strategy_ref": (
                copy.deepcopy(kronos_info.get("strategy_ref"))
                if isinstance(kronos_info.get("strategy_ref"), dict) else None
            ),
            "data_health": "ok",
        }

    app.extensions["olympus_monitor_opportunity"] = _monitor_opportunity

    # 测试钩子：嵌套函数不可直接 import，经 Flask extensions 暴露供单测隔离验证（M5）
    app.extensions["olympus_assess_candidate"] = _assess_candidate
    app.extensions["olympus_decide_alert"] = _decide_alert
    app.extensions["olympus_alert_provenance"] = _alert_provenance

    def _monitor_position_exit(
        position: dict, quote_obj, window: dict, decision_gate: dict,
        risk_action: dict | None = None,
    ) -> dict:
        """Monitor every recorded open position, including stocks no longer in a candidate pool.

        Only an authoritative risk line creates a quantity action. Technical
        indicators remain non-actionable observations.
        """
        if (
            isinstance(risk_action, dict)
            and risk_action.get("position_id") is not None
            and risk_action.get("position_id") != position.get("id")
        ):
            risk_action = None
        code = str(position.get("code") or "")
        name = str(position.get("name") or code)
        q = _quote_to_dict(quote_obj, code)
        price = _finite_number(q.get("price"))
        freshness = str(q.get("freshness") or "unknown")
        freshness_ok = freshness == "live" or (
            freshness == "closed_snapshot"
            and not (_is_trading_day() and _is_market_open())
        )
        if price is None or price <= 0 or not freshness_ok:
            reason = "持仓实时行情价格或新鲜度无法复核，不生成卖出提醒"
            trade_plan = _position_trade_plan(
                position, risk_action, None,
                quote_as_of=q.get("exchange_data_time") or q.get("data_time"),
                risk_observation=get_position_risk_state(
                    int(position.get("id") or 0)
                ),
            )
            return {
                "code": code, "name": name, "price": None,
                "change_pct": None,
                "prev_close": _finite_number(q.get("previous_close")),
                "levels": None, "score": None, "level": "行情未知",
                "position": position, "signals": {},
                "alert": {
                    "status": "watching", "action": None, "reason": reason,
                    "window_label": "", "countdown_seconds": None,
                },
                "buy_window": {
                    "phase": window.get("phase"), "label": window.get("label"),
                    "countdown_seconds": window.get("countdown_seconds"),
                },
                "data_health": "invalid_quote",
                "trade_plan": trade_plan,
            }
        levels = compute_levels(
            position.get("price"), code=code, prev_close=q.get("previous_close")
        )
        scoring = {"score": 0, "level": "持仓风控", "recommended_action": "wait"}
        effective_level = (
            (risk_action or {}).get("effective_risk_level")
            or levels.get("effective_risk_level")
        )
        if effective_level is None and _finite_number(levels.get("stop_loss")) is not None:
            effective_level = {
                "kind": "hard_stop", "value": levels.get("stop_loss"),
                "source": "legacy_entry_hard_stop", "active": True,
            }
        effective_value = _finite_number((effective_level or {}).get("value"))
        risk_observation = None
        if effective_value is not None and (effective_level or {}).get("active") is True:
            risk_observation = observe_position_risk_state(
                position, price, effective_value,
            )
        decision = _decide_alert(
            code, name, price, window, scoring, levels, position, decision_gate,
            risk_action=risk_action, risk_observation=risk_observation,
        )
        manual_target = position.get("manual_exit_target_shares")
        manual_effective = position.get("manual_exit_effective_date")
        manual_exit_due = False
        try:
            manual_exit_due = bool(
                not isinstance(manual_target, bool)
                and int(manual_target) >= 0
                and int(manual_target) < int(position.get("shares") or 0)
                and datetime.strptime(
                    str(manual_effective), "%Y-%m-%d",
                ).date() <= datetime.now(_SH_TZ).date()
            )
        except (TypeError, ValueError):
            manual_exit_due = False
        sellable_as_of = str(position.get("sellable_as_of") or "")
        sellable_today = sellable_as_of[:10] == datetime.now(_SH_TZ).date().isoformat()
        sellable_shares = position.get("sellable_shares_snapshot")
        exact_manual_exit_ready = bool(
            manual_exit_due
            and _is_market_open()
            and sellable_today
            and not isinstance(sellable_shares, bool)
            and isinstance(sellable_shares, int)
            and sellable_shares > 0
        )
        if not decision.get("trigger") and exact_manual_exit_ready:
            decision = {
                "trigger": True,
                "action": "sell",
                "reason": (
                    f"用户退出计划已生效：目标持仓{int(manual_target)}股；"
                    f"当前核验可卖{sellable_shares}股，请结合实时价格人工复核。"
                    "该提醒保证计划及时进入决策链，不宣称当前价是全天最高价"
                ),
                "window_label": "用户退出计划·实时复核",
                "countdown_seconds": 120,
            }
        alert_valid_until = _alert_valid_until(window, decision)
        trade_plan = _position_trade_plan(
            position, risk_action, price,
            quote_as_of=q.get("exchange_data_time") or q.get("data_time"),
            valid_until=alert_valid_until,
            risk_observation=risk_observation,
        )
        sell_method = str(
            ((((trade_plan.get("action") or {}).get("evidence_identity") or {})
              .get("method")) or "")
        )
        alert = {
            "status": "watching", "action": decision["action"],
            "reason": decision["reason"], "window_label": decision["window_label"],
            "countdown_seconds": decision["countdown_seconds"],
            "valid_until": alert_valid_until,
            "provenance": _alert_provenance(
                code, str(decision.get("action") or "sell"), position=position,
                risk_observation=risk_observation, sell_method=sell_method,
            ),
            "risk_observation": copy.deepcopy(risk_observation),
        }
        if decision["trigger"]:
            action_plan = None
            action = trade_plan.get("action") if isinstance(trade_plan, dict) else None
            if isinstance(action, dict) and action.get("side") == "SELL":
                sold_shares = int(action.get("shares") or 0)
                amount = price * sold_shares
                action_plan = {
                    "trade_plan": copy.deepcopy(trade_plan),
                    "allocation": {
                        "position_id": position.get("id"),
                        "code": code, "name": name, "price": price,
                        "held_shares": int(position.get("shares") or 0),
                        "sell_shares": sold_shares,
                        "target_shares": int(action.get("target_shares") or 0),
                        "estimated_sell_cost_yuan": action.get("estimated_cost"),
                        "estimated_sell_proceeds_yuan": round(
                            amount - float(action.get("estimated_cost") or 0), 2,
                        ),
                    },
                }
            outcome = try_record_alert(
                code, name, decision["action"], decision["window_label"],
                price, scoring["score"], scoring["level"], decision["reason"],
                valid_until=alert_valid_until, action_plan=action_plan,
                **alert["provenance"],
            )
            if outcome["status"] in {"recorded", "existing_triggered"}:
                alert["status"] = "triggered"
                stored_alert = outcome.get("alert") if isinstance(outcome.get("alert"), dict) else {}
                if stored_alert.get("id"):
                    alert["id"] = int(stored_alert["id"])
                stored_plan = stored_alert.get("action_plan")
                if isinstance(stored_plan, dict) and isinstance(stored_plan.get("trade_plan"), dict):
                    trade_plan = copy.deepcopy(stored_plan["trade_plan"])
            else:
                alert.update({"status": "idle", "reason": {
                    "confirmed": "该提醒已确认处理",
                    "dismissed": "本次风险事件已忽略；价格回到风险线上方后再次跌破才会重新提醒",
                    "cooldown": "冷却期内不重复提醒",
                    "expired": "该提醒窗口已过期，不顺延执行",
                }.get(outcome["status"], "提醒未记录"),
                              "countdown_seconds": None})
        return {
            "code": code, "name": name, "price": price,
            "change_pct": q.get("change_pct"), "prev_close": q.get("previous_close"),
            "levels": levels, "score": scoring["score"], "level": scoring["level"],
            "position": position, "signals": {}, "alert": alert,
            "buy_window": {"phase": window["phase"], "label": window["label"],
                           "countdown_seconds": window["countdown_seconds"]},
            "data_health": "ok",
            "trade_plan": trade_plan,
        }

    app.extensions["olympus_monitor_position_exit"] = _monitor_position_exit

    def _opportunity_company_summary(code: str, name: str, scoring: dict, quote: dict | None = None) -> dict:
        """机会卡片的档案摘要 + 综合评分（100 分制，公司侧 0-15 + 量化侧时机分×0.85 0-85）.
        quote 提供实时行情 → 交易闸门用真实分，与档案页一致."""
        co = _company_profile_summary(code, name, quote)
        if co and co.get("ready"):
            co["composite"] = _quality_composite_grade(code, co, quote)
        return co

    def _professional_preselection_preview(
        code: str, name: str, quote_obj, market_score: object,
    ) -> dict:
        """Return a cache-only professional score hint for batch selection.

        The helper never starts a profile refresh.  Missing or unusable cache
        evidence remains explicitly unavailable, so selection cannot invent a
        neutral/positive company score merely to fill the assessment budget.
        """
        market = _finite_number(market_score)
        if market is None:
            return {"status": "unavailable_market_score", "cache_only": True}
        try:
            from .company_profile import get_cached_summary
            quote = _quote_to_dict(quote_obj, code)
            summary = get_cached_summary(code, name, quote)
            if not isinstance(summary, dict):
                return {"status": "unavailable_no_profile_cache", "cache_only": True}
            grade = _quality_composite_grade(code, summary, quote)
            professional = _finite_number(grade.get("composite"))
            if professional is None:
                return {
                    "status": "unavailable_incomplete_professional_score",
                    "cache_only": True,
                    "profile_status": summary.get("status"),
                }
            return {
                "status": "available",
                "cache_only": True,
                "method": "professional_composite_v4_profit_sign_aware",
                "market_diagnostic_score": round(market, 2),
                "professional_score": round(professional, 2),
                "profile_status": summary.get("status"),
                "data_gap": grade.get("data_gap"),
                "confidence": grade.get("confidence"),
            }
        except Exception as exc:
            return {
                "status": "unavailable_error",
                "cache_only": True,
                "error_type": type(exc).__name__,
            }

    def _company_profile_summary(code: str, name: str, quote: dict | None = None) -> dict:
        """档案摘要（只读缓存 + 无缓存时后台预热一次，主流程零阻塞）.
        首次请求返回"生成中"，约 1-2s 后缓存就绪，下一轮轮询自动带出灯号.
        quote：实时行情 dict，提供则交易闸门用真实分（与档案页一致），否则行情分给中性."""
        if name == "名称不可用":
            return {"ready": False, "message": "名称缺失，不生成公司档案"}
        try:
            from .company_profile import get_cached_summary
            s = get_cached_summary(code, name, quote)
            if not s:
                _ensure_company_profile(
                    code, name, include_supplementary=False,
                )
                return {"ready": False, "message": "档案生成中"}
            if _company_profile_needs_refresh(s):
                # A stale or source-incomplete cache entry is still useful for
                # transparent low-confidence display, but it must not suppress
                # the bounded refresh that can make the next assessment fresher.
                _ensure_company_profile(
                    code, name, include_supplementary=False,
                )
            return s
        except Exception:
            return {"ready": False, "message": "档案暂不可用"}

    def _partition_opportunity_items(rows: list[dict]) -> tuple[list, list, list]:
        """Keep only currently actionable opportunities in the main list."""
        approved, watch, rejected = [], [], []
        for row in rows:
            assessment = row.get("assessment") or {}
            verdict = assessment.get("verdict")
            actionable = (
                verdict == "APPROVE"
                and assessment.get("manual_actionable") is True
            )
            row["review_visible"] = bool(assessment.get("failed_gates"))
            row["review_status"] = {
                "APPROVE": "approved" if actionable else "watch",
                "WATCH": "watch",
                "PENDING": "pending", "REJECT": "rejected",
            }.get(verdict, "pending")
            if actionable:
                approved.append(row)
            elif verdict in ("APPROVE", "WATCH", "PENDING"):
                watch.append(row)
            else:
                rejected.append(row)
        return approved, watch, rejected

    def _execution_wait(item: dict, state: str, reason: str) -> None:
        assessment = item.get("assessment") or {}
        assessment["manual_actionable"] = False
        assessment["can_alert"] = False
        assessment["action_state"] = state
        watch_gates = list(assessment.get("watch_gates") or [])
        if "account_execution" not in watch_gates:
            watch_gates.append("account_execution")
        assessment["watch_gates"] = watch_gates
        assessment["reason"] = f"{assessment.get('reason') or '机会质量通过'}；{reason}"
        item["assessment"] = assessment
        item["recommended_action"] = "wait"
        item["manual_review_state"] = "monitoring"
        prior = item.get("alert") or {}
        item["alert"] = {
            "status": "watching", "action": None,
            "reason": reason, "window_label": "",
            "countdown_seconds": None, "valid_until": None,
            "provenance": prior.get("provenance") or {},
        }

    def _market_target_for_account(portfolio: dict) -> tuple[float | None, dict | None]:
        try:
            regime = _current_market_regime(portfolio)
        except Exception:
            return None, None
        target = regime.get("risk_budget_ceiling_pct")
        if target is None:
            target = regime.get("target_exposure_pct")
        return _finite_number(target), regime

    def _complete_buy_alerts(
        rows: list[dict], window: dict, portfolio: dict | None,
        decision_gate: dict | None = None,
        strict_alert_context: dict | None = None,
    ) -> dict:
        raw_eligible = [
            row for row in rows
            if (row.get("assessment") or {}).get("manual_actionable") is True
            and (row.get("alert") or {}).get("status") == "eligible_pending_plan"
        ]
        portfolio = portfolio if isinstance(portfolio, dict) else {}
        decision_gate = decision_gate if isinstance(decision_gate, dict) else {}
        strict_alert_context = (
            _enforce_current_full_a_entry_scope(strict_alert_context)
            if isinstance(strict_alert_context, dict)
            else _strict_route_alert_context(
                _cached_strict_forward_status(),
                decision_day=datetime.now(_SH_TZ).date().isoformat(),
                reference_time=datetime.now(_SH_TZ),
            )
        )
        selected_codes = set(strict_alert_context.get("selected_codes") or set())
        eligible = [
            row for row in raw_eligible
            if str(row.get("code") or "") in selected_codes
        ]
        maximum_new_trades = int(
            strict_alert_context.get("maximum_new_trades") or 0
        )
        if maximum_new_trades >= 0:
            overflow = eligible[maximum_new_trades:]
            eligible = eligible[:maximum_new_trades]
        else:
            overflow = eligible
            eligible = []
        strict_reason = str(
            strict_alert_context.get("reason")
            or "当日严格市场路由尚未验证"
        )
        for item in raw_eligible:
            if item not in eligible:
                reason = (
                    strict_reason
                    if strict_alert_context.get("alert_ready") is not True else
                    f"今日严格路由最多允许{maximum_new_trades}笔新交易"
                    if item in overflow else
                    "该股票未进入当日同一观察时点的严格候选，不生成买入提醒"
                )
                _execution_wait(
                    item, "WAIT_STRICT_ROUTE", reason,
                )
        manual_alerts_enabled = bool(
            decision_gate.get("manual_consent") is True
            and decision_gate.get("manual_research_alerts_allowed") is True
        )
        target, regime = _market_target_for_account(portfolio)
        production_strategy = strict_alert_context.get("strategy_ref")
        production_strategy_ready = bool(
            isinstance(production_strategy, dict)
            and production_strategy.get("production_eligible") is True
        )
        strict_route_candidates = {
            str(row.get("code")): row
            for row in _strict_route_opportunity_candidates(strict_alert_context)
            if isinstance(row, dict) and row.get("code")
        }
        route_account_snapshot_sha256 = strict_alert_context.get(
            "account_snapshot_sha256"
        )
        current_account_snapshot_sha256 = portfolio.get(
            "immutable_account_snapshot_sha256"
        )
        account_snapshot_matches = bool(
            route_account_snapshot_sha256
            and route_account_snapshot_sha256 == current_account_snapshot_sha256
        )
        strict_target = (
            _finite_number(strict_alert_context.get("target_exposure_pct"))
            if strict_alert_context.get("alert_ready") is True
            and production_strategy_ready else None
        )
        if target is not None and strict_target is not None:
            target = min(target, strict_target)
        readiness = {
            "manual_alerts_enabled": manual_alerts_enabled,
            "manual_consent": decision_gate.get("manual_consent") is True,
            "manual_research_alerts_allowed": (
                decision_gate.get("manual_research_alerts_allowed") is True
            ),
            "cash_user_recorded": portfolio.get("cash_user_recorded") is True,
            "portfolio_valuation_status": portfolio.get("valuation_status"),
            "portfolio_valuation_executable": (
                portfolio.get("valuation_executable") is True
            ),
            "market_target_exposure_pct": target,
            "strict_route_valid": strict_alert_context.get("route_valid") is True,
            "strict_alert_ready": strict_alert_context.get("alert_ready") is True,
            "production_strategy_ready": production_strategy_ready,
            "strict_forward_packet_id": strict_alert_context.get("packet_id"),
            "strict_forward_slot_id": strict_alert_context.get("slot_id"),
            "strict_playbook_id": strict_alert_context.get("playbook_id"),
            "strict_candidate_count": len(selected_codes),
            "strict_matched_candidate_count": len(eligible),
            "maximum_new_trades": maximum_new_trades,
            "new_buy_plan_eligible": False,
            "account_ledger_identity_ready": False,
            "route_account_snapshot_sha256": route_account_snapshot_sha256,
            "current_account_snapshot_sha256": current_account_snapshot_sha256,
            "account_snapshot_matches": account_snapshot_matches,
            "available_cash_yuan": None,
            "minimum_candidate_lot_cost_yuan": None,
            "maximum_affordable_lots_at_min_candidate_price": None,
            "buy_capacity_shares": None,
            "blocking_reasons": [],
            "reason": None,
        }
        available_cash = (
            _finite_number(portfolio.get("available_cash"))
            if portfolio.get("cash_user_recorded") is True else None
        )
        account_equity = (
            _finite_number(portfolio.get("account_equity"))
            if portfolio.get("valuation_status") == "complete" else None
        )
        candidate_lot_costs = [
            price * _LOT_UNIT
            for row in rows
            if (price := _finite_number(row.get("price"))) is not None
            and price > 0
        ]
        minimum_lot_cost = min(candidate_lot_costs) if candidate_lot_costs else None
        affordable_lots = (
            max(0, int(available_cash // minimum_lot_cost))
            if available_cash is not None and minimum_lot_cost is not None
            else None
        )
        readiness.update({
            "available_cash_yuan": available_cash,
            "minimum_candidate_lot_cost_yuan": (
                round(minimum_lot_cost, 2) if minimum_lot_cost is not None else None
            ),
            "maximum_affordable_lots_at_min_candidate_price": affordable_lots,
            "buy_capacity_shares": (
                affordable_lots * _LOT_UNIT if affordable_lots is not None else None
            ),
        })
        expected_ledger_identity = (
            portfolio.get("ledger_identity") or {}
        ).get("combined_sha256")
        ledger_identity_ready = bool(
            isinstance(expected_ledger_identity, str)
            and len(expected_ledger_identity) == 64
        )
        readiness["account_ledger_identity_ready"] = ledger_identity_ready
        blockers: list[str] = []
        if not manual_alerts_enabled:
            blockers.append("研究级人工提醒未启用，不写入买入提醒账本")
        if strict_alert_context.get("alert_ready") is not True:
            blockers.append(strict_reason)
        elif not production_strategy_ready:
            blockers.append("未绑定成本后与样本外验证的生产策略，不生成买入提醒")
        elif not account_snapshot_matches:
            blockers.append("账户持仓或现金已变化，旧路由数量失效，等待新同槽账户快照")
        if portfolio.get("valuation_status") != "complete":
            blockers.append("持仓估值不完整，不能生成账户级买入计划")
        elif portfolio.get("valuation_executable") is not True:
            blockers.append(
                "持仓仅使用最近完成收盘价估值；等待盘中实时价后重新核验账户估值和买入能力"
            )
        if portfolio.get("cash_user_recorded") is not True:
            blockers.append("尚未录入真实剩余资金，研究候选保留但不生成买入提醒")
        elif affordable_lots == 0 and minimum_lot_cost is not None:
            blockers.append(
                f"可用现金 ¥{available_cash:,.2f} 低于当前候选最低一手成本 "
                f"¥{minimum_lot_cost:,.2f}，新增买入数量为0股"
            )
        if not ledger_identity_ready:
            blockers.append("账户现金与持仓快照缺少统一账本身份，不能生成买入提醒")
        if account_equity is None or account_equity <= 0:
            blockers.append("当前账户权益不可核验，不能计算真实仓位和买入股数")
        if target is None:
            blockers.append("账户总暴露容量不可核验，不能生成买入计划")
        elif target <= 0:
            blockers.append(str((regime or {}).get("reason") or "账户总暴露上限为0%，暂停新开仓"))
        readiness["blocking_reasons"] = list(dict.fromkeys(blockers))
        readiness["reason"] = readiness["blocking_reasons"][0] if blockers else None
        readiness["new_buy_plan_eligible"] = not blockers
        if not eligible:
            return readiness
        if not readiness["new_buy_plan_eligible"]:
            state = (
                "WAIT_CONSENT" if not readiness["manual_alerts_enabled"]
                else "WAIT_CASH" if not readiness["cash_user_recorded"]
                else "WAIT_ACCOUNT_PLAN"
            )
            for item in eligible:
                _execution_wait(item, state, str(readiness["reason"]))
            return readiness

        current_ledger_identity = (
            read_account_ledger_facts().get("ledger_identity") or {}
        ).get("combined_sha256")
        if current_ledger_identity != expected_ledger_identity:
            readiness["new_buy_plan_eligible"] = False
            readiness["reason"] = "账户现金或持仓在规划期间发生变化，旧数量已作废"
            for item in eligible:
                _execution_wait(
                    item, "WAIT_ACCOUNT_PLAN", str(readiness["reason"]),
                )
            return readiness

        valid_until = (eligible[0].get("alert") or {}).get("valid_until")
        plan = _allocate_capital(
            eligible,
            float(account_equity),
            target,
            portfolio,
            {
                "executable": True,
                "valid_until": valid_until,
                "market_session_executable": True,
                "decision_time": datetime.now(_SH_TZ).isoformat(
                    timespec="seconds"
                ),
                "cash_as_of": portfolio.get("cash_as_of"),
            },
        )
        allocations = {
            str(row.get("code")): row
            for row in (plan.get("items") or []) if isinstance(row, dict)
        }
        skipped = {
            str(row.get("code")): str(row.get("reason") or "账户分配后无法生成整手买入计划")
            for row in (plan.get("skipped") or []) if isinstance(row, dict)
        }
        for item in eligible:
            alert = item.get("alert") or {}
            allocation = allocations.get(str(item.get("code")))
            action = (
                ((allocation or {}).get("trade_plan") or {}).get("action")
                if isinstance(allocation, dict) else None
            )
            if not isinstance(action, dict) or action.get("side") != "BUY":
                reason = (
                    skipped.get(str(item.get("code")))
                    or str(plan.get("error") or plan.get("note") or "账户分配后无可执行买入数量")
                )
                _execution_wait(item, "WAIT_ACCOUNT_PLAN", reason)
                continue
            strict_ref = {
                key: copy.deepcopy(strict_alert_context.get(key))
                for key in (
                    "packet_id", "slot_id", "decision_day", "decision_at",
                    "market_state", "playbook_id", "account_snapshot_sha256",
                )
            }
            production_strategy = copy.deepcopy(production_strategy or {})
            evidence_identity = action.get("evidence_identity")
            evidence_identity = (
                copy.deepcopy(evidence_identity)
                if isinstance(evidence_identity, dict) else {}
            )
            evidence_identity["strict_forward_route"] = strict_ref
            evidence_identity["account_ledger_identity"] = (
                expected_ledger_identity
            )
            action.update({
                "playbook_id": strict_alert_context.get("playbook_id"),
                "strategy_id": production_strategy.get("strategy_id"),
                "strategy_version": production_strategy.get("strategy_version"),
                "strategy_role": "validated_state_routed_manual_alert",
                "evidence_grade": "production_cost_after_out_of_sample",
                "as_of": strict_alert_context.get("decision_at"),
                "evidence_identity": evidence_identity,
            })
            allocation["strict_forward_route"] = strict_ref
            frozen = _frozen_buy_action_plan(allocation, plan)
            provenance = _alert_provenance(
                str(item.get("code")), "buy",
                candidate=strict_route_candidates.get(str(item.get("code"))),
            )
            if provenance.get("origin") != "production_strategy":
                _execution_wait(
                    item, "WAIT_STRATEGY",
                    "严格路由的生产策略证据无法绑定到本次提醒",
                )
                continue
            outcome = try_record_alert(
                str(item.get("code")), str(item.get("name") or item.get("code")),
                "buy", str(alert.get("window_label") or ""),
                _finite_number(item.get("price")),
                _finite_number(item.get("score")) or 0.0,
                str(item.get("level") or ""), str(alert.get("reason") or ""),
                valid_until=alert.get("valid_until"), action_plan=frozen,
                **provenance,
            )
            if outcome.get("status") in {"recorded", "existing_triggered"}:
                stored = outcome.get("alert") or {}
                item["alert"] = {
                    **alert, "status": "triggered", "id": stored.get("id"),
                    "valid_until": stored.get("valid_until") or alert.get("valid_until"),
                    "action_plan": stored.get("action_plan") or frozen,
                    "provenance": provenance,
                }
                action_plan = item["alert"]["action_plan"] or {}
                item["trade_plan"] = copy.deepcopy(
                    action_plan.get("trade_plan")
                )
                item["allocation"] = copy.deepcopy(
                    action_plan.get("allocation") or {}
                )
                continue
            blocked_reason = {
                "daily_limit": "本次策略或账户允许的买入提醒容量已用完",
                "confirmed": "该提醒已确认处理",
                "cooldown": "冷却期内不重复提醒",
                "expired": "该提醒窗口已过期，不顺延或补买",
            }.get(str(outcome.get("status")), "提醒未记录")
            _execution_wait(item, "WAIT_ALERT_LEDGER", blocked_reason)
        return readiness

    app.extensions["olympus_partition_opportunities"] = _partition_opportunity_items
    app.extensions["olympus_complete_buy_alerts"] = _complete_buy_alerts

    def _compute_opportunities() -> dict:
        """计算今日机会完整快照（纯计算，含提醒副作用）。后台 worker 与首次同步调用共用."""
        # This background worker can create alert records. Keep the complete
        # authoritative gate here; only the polling response is compact/fast.
        decision_gate = _decision_gate_status()
        strict_forward_status = _cached_strict_forward_status()
        strict_alert_context = _strict_route_alert_context(
            strict_forward_status,
            decision_day=datetime.now(_SH_TZ).date().isoformat(),
            reference_time=datetime.now(_SH_TZ),
        )
        try:
            position_rows = list_positions("open")
        except Exception:
            position_rows = []
        grouped_positions = positions_by_code(position_rows)
        try:
            factor_candidates = _load_production_factor_candidates(20)
        except Exception:
            factor_candidates = []
        model_candidates = _admitted_model_candidates(load_kronos_top(limit=65))
        cands = _merge_candidates(
            _strict_route_opportunity_candidates(strict_alert_context),
            _interleave_candidates(
                factor_candidates,
                model_candidates,
                _load_full_market_candidates(_FM_MERGE_LIMIT),
            ),
        )
        if not cands and not position_rows:
            return {"error": "模型研究数据不可用", "items": [], "position_alerts": [], "trigger": None}

        # 已记录持仓单独加进实时报价批次；不要求它仍在模型候选池内。
        codes = list(dict.fromkeys(
            [c["code"] for c in cands]
            + [str(position.get("code") or "") for position in position_rows]
        ))
        try:
            quotes = app.config["QUOTE_PROVIDER"].get_quotes(codes)
        except Exception:
            quotes = {}
        quotes = quotes or {}

        # 一次计算全部持仓 risk_action（复用上面的 quotes，不重复打行情源）。
        # 只有止损/移动风险线能生成精确卖出提醒；趋势、动量、支撑等
        # 技术指标只进入 observations，不生成卖出数量。
        risk_map = {}
        try:
            from .risk_control import position_risk_actions as _risk_actions
            risk_map = risk_actions_by_position_id(
                position_rows, quotes, finite_number=_finite_number,
                compute_actions=_risk_actions,
            )
        except Exception:
            risk_map = {}

        # 个人排除名单仍生效；行业不剥夺候选资格。
        candidate_names = {
            str(row.get("code")): str(row.get("name") or "").strip()
            for row in cands
        }
        names = {
            code: _display_name_or_unavailable(
                getattr(quotes.get(code), "name", "") or candidate_names.get(code)
            )
            for code in codes
        }
        filtered = [
            c for c in cands
            if is_recommendable_stock(c["code"], names.get(c["code"], "名称不可用"))
            and not _is_blacklisted(c["code"], names.get(c["code"], "名称不可用"))
            and not _is_personal_excluded(c["code"])   # 个人排除名单（老邓股/已持仓）
        ]
        # 先对过滤后的整个候选池做同口径市场证据排序，再深评固定最佳池。
        # 软数据缺失给中性分，不新增否决门槛；未验证模型值不会进入 expected_return。
        ranked_candidates = []
        for candidate in filtered:
            quote = quotes.get(candidate["code"])
            quote_price = _finite_number(getattr(quote, "price", None))
            row = dict(candidate)
            row["opportunity_quality"] = _opportunity_market_evidence(
                row, quote_price,
            )
            frozen_preview = _frozen_full_market_preselection_preview(row)
            if frozen_preview is not None:
                row["preselection_professional_preview"] = frozen_preview
            else:
                row["preselection_professional_preview"] = (
                    _professional_preselection_preview(
                        row["code"], names.get(row["code"], "名称不可用"), quote,
                        (row.get("opportunity_quality") or {}).get("rank_score"),
                    )
                )
            ranked_candidates.append(row)
        filtered = sorted(
            ranked_candidates,
            key=lambda row: _finite_number(
                (row.get("opportunity_quality") or {}).get("rank_score")
            ) or -1.0,
            reverse=True,
        )
        # 全市场先完成低成本、多维预筛；有专业综合分缓存时按唯一专业分
        # 选择深评池，缺失缓存才按市场预筛顺序补位。无随机、无第二总分。
        assessment_batch = _best_opportunity_assessment_batch(filtered)
        assessment_profile_warmup = _warm_pool_profiles([
            {"code": c["code"], "name": names[c["code"]]}
            for c in assessment_batch
            if names.get(c["code"]) not in (None, "名称不可用")
        ])
        # Do not preheat the retained Top300 as a second queue.  That path both
        # reproduced selection bias and could consume all worker capacity
        # before the full-market stratified repair had a chance to run.
        coverage_profile_warmup = {
            "status": "superseded_by_full_market_stratified_scheduler",
            "requested": 0,
            "scheduled": 0,
        }
        try:
            from .professional_coverage import coverage_job_status, coverage_state
            full_market_coverage_snapshot = _read_full_market_snapshot()
            professional_coverage_state = coverage_state(
                full_market_coverage_snapshot
            )
            epoch_id = str(professional_coverage_state.get("epoch_id") or "")
            professional_coverage_warmup = {
                key: value for key, value in professional_coverage_state.items()
                if key not in {"rows", "cells"}
            }
            professional_coverage_warmup.update({
                "claimed_codes": [],
                "reason": "dedicated_financial_only_launchd_worker",
                "writer": "com.zidongliu.ashare-professional-coverage",
                "jobs": coverage_job_status(
                    _PROJECT / "research" / "professional_coverage.sqlite3",
                    epoch_id,
                ) if epoch_id else {},
                "queue": {
                    "requested": 0, "scheduled": 0,
                    "note": "机会接口只读展示；补数由独立轻量任务唯一写入。",
                },
                "available_slots_before_claim": None,
            })
        except Exception as exc:
            professional_coverage_warmup = {
                "status": "deferred_error",
                "error_type": type(exc).__name__,
                "claimed_codes": [],
            }

        try:
            portfolio_snapshot = _manual_portfolio_status(
                app.config["QUOTE_PROVIDER"],
            )
        except Exception:
            portfolio_snapshot = {
                "valuation_status": "unavailable",
                "cash_user_recorded": False,
            }
        available_cash = (
            _finite_number(portfolio_snapshot.get("available_cash"))
            if portfolio_snapshot.get("cash_user_recorded") is True
            else None
        )

        window = get_buy_window()

        def _assess_one(s):
            code = s["code"]
            candidate = dict(s)
            if not isinstance(candidate.get("strategy_ref"), dict):
                candidate["strategy_ref"] = copy.deepcopy(_MANUAL_STRATEGY_REF)
            item = _monitor_opportunity(
                code, names.get(code, "名称不可用"), quotes.get(code),
                candidate, window,
                (grouped_positions.get(code) or [None])[0], decision_gate,
                available_cash,
            )
            item["name_status"] = (
                "unavailable" if item.get("name") == "名称不可用" else "available"
            )
            item["source"] = s.get("source", "kronos")
            if isinstance(candidate.get("strategy_ref"), dict):
                item["strategy_ref"] = copy.deepcopy(candidate["strategy_ref"])
            item["opportunity_quality"] = _refine_opportunity_quality(
                s.get("opportunity_quality") or {}, item.get("assessment"),
            )
            item["selection_audit"] = {
                "selected_via": (
                    "professional_composite_preview"
                    if _finite_number(
                        (s.get("preselection_professional_preview") or {}).get(
                            "professional_score"
                        )
                    ) is not None
                    else "market_evidence_fallback"
                ),
                "professional_preview": copy.deepcopy(
                    s.get("preselection_professional_preview") or {}
                ),
            }
            return item

        items = _assess_ranked_opportunities(
            assessment_batch, _assess_one,
            assessment_budget=len(assessment_batch),
        )
        # Freeze the pre-account research decision identity before real cash
        # availability can turn an otherwise eligible item into WAIT_CASH.
        forward_research_candidates = copy.deepcopy(items)

        account_execution = _complete_buy_alerts(
            items, window, portfolio_snapshot, decision_gate,
            strict_alert_context,
        )

        position_alerts = [
            _monitor_position_exit(
                pos, quotes.get(str(pos.get("code") or "")), window,
                decision_gate, risk_map.get(int(pos.get("id") or 0)),
            )
            for pos in position_rows
        ]

        # 主榜、观察层、否决层必须语义隔离。过去把所有候选无条件塞进 items，
        # 导致 REJECT 股票看起来像推荐；现在 items 只代表机会质量通过。
        good, watch_items, rejected = _partition_opportunity_items(items)

        # 分层后按收益/回撤/波动/成本/可交易性机会质量优先；买点可预警只作次级键。
        # 未验证模型值只保留作审计证据，不参与 expected_return 或推荐优先级。
        _FIT_ORDER = {"good": 3, "ok": 2, "poor": 1, "no_price": 0}

        def _opp_sort_key(it):
            a = it.get("assessment") or {}
            comp = ((a.get("gates") or {}).get("composite") or {}).get("composite")
            comp = comp if comp is not None else -1
            rank_score = _finite_number(
                (it.get("opportunity_quality") or {}).get("rank_score")
            )
            return (rank_score if rank_score is not None else -1,
                    1 if a.get("can_alert") else 0,
                    comp,
                    _FIT_ORDER.get((it.get("price_fit") or {}).get("fit"), 0))

        good = sorted(good, key=_opp_sort_key, reverse=True)
        watch_items = sorted(watch_items, key=_opp_sort_key, reverse=True)
        rejected = sorted(rejected, key=_opp_sort_key, reverse=True)
        # The model evidence card is a source slice of this exact assessed
        # snapshot.  It must not fetch and re-assess a separate Top-N batch,
        # otherwise the two dashboard cards can disagree on codes and status.
        model_evidence_items = sorted(
            [row for row in items if row.get("source") == "kronos"],
            key=_opp_sort_key,
            reverse=True,
        )[:12]
        # 连续上榜天数（诚实标注：同股跨日进入推荐榜，前端显示"连续N天"）
        _recur = _update_recurrence(
            [it["code"] for it in good],
            datetime.now(_SH_TZ).strftime("%Y-%m-%d"),
        )
        for it in good:
            rec = _recur.get(it["code"])
            if rec:
                it["streak_days"] = rec["streak"]
        # 持仓卖出优先：它不依赖研究候选是否仍在榜上，且研究模式也允许红屏风险提示。
        trigger = next((it for it in position_alerts if it["alert"]["status"] == "triggered"), None)
        if trigger is None:
            trigger = next((it for it in good if it["alert"]["status"] == "triggered"), None)

        # 模型日频快照新鲜度只由输入交易日决定；制品重生成不能洗新。
        _kstat = kronos_status()
        _kgen = _kstat.get("generated_at") or ""
        _kdata_as_of = _kstat.get("source_data_as_of")
        _kstale_days = _kstat.get("data_staleness_days")
        _kage_h = (
            round(float(_kstale_days) * 24, 1)
            if _finite_number(_kstale_days) is not None else None
        )
        snapshot_at = datetime.now(_SH_TZ).strftime("%Y-%m-%d %H:%M:%S")
        source_counts = {
            "model": sum(
                1 for row in filtered if row.get("source", "kronos") == "kronos"
            ),
            "full_market": sum(
                1 for row in filtered if row.get("source") == "full_market"
            ),
            "production_factor": sum(
                1 for row in filtered if row.get("source") == "production_factor"
            ),
            "strict_route": sum(
                1 for row in filtered if row.get("source") == "strict_route"
            ),
        }
        full_market_snapshot = _read_full_market_snapshot()
        full_market_audit = audited_universe(_PROJECT)
        live_signal_snapshot = load_live_signals() or {}
        live_monitor_scope = live_signal_snapshot.get("watchlist_scope") or {}
        if not isinstance(live_monitor_scope, dict):
            live_monitor_scope = {}
        full_market_scan_audit = (
            full_market_snapshot.get("scan_audit")
            if isinstance(full_market_snapshot.get("scan_audit"), dict)
            else {}
        )
        assessment_counts = {
            "actionable": len(good),
            "approved_waiting_entry": sum(
                1 for row in watch_items
                if (row.get("assessment") or {}).get("verdict") == "APPROVE"
            ),
            "watch": sum(
                1 for row in watch_items
                if (row.get("assessment") or {}).get("verdict") != "APPROVE"
            ),
            "rejected": len(rejected),
        }
        failed_condition_counts: dict[str, int] = {}
        for row in items:
            for condition in (row.get("assessment") or {}).get("failed_gates") or []:
                failed_condition_counts[condition] = (
                    failed_condition_counts.get(condition, 0) + 1
                )
        triggered_buy_count = sum(
            1 for row in good
            if (row.get("alert") or {}).get("status") == "triggered"
        )
        triggered_sell_count = sum(
            1 for row in position_alerts
            if (row.get("alert") or {}).get("status") == "triggered"
        )
        factor_policy = {
            "role": "optional_stock_ranking_and_cross_confirmation",
            "production_factor_count": source_counts["production_factor"],
            "strategy_route_requires_production_factor": False,
            "zero_production_factors_blocks_non_factor_playbook": False,
            "factor_dependent_strategy_requires_admitted_factor": True,
            "status": (
                "supplemental_evidence_available"
                if source_counts["production_factor"] > 0
                else "supplemental_evidence_unavailable"
            ),
            "reason": (
                "正式因子仅用于补充排序和交叉确认；当前为0不阻断独立战法，"
                "但依赖因子的策略仍须等待对应因子正式准入。"
            ),
        }
        zero_reasons = []
        if not is_actionable_buy_window(window):
            zero_reasons.append(
                f"当前{window.get('label') or '非买入窗口'}："
                f"{window.get('note') or '不生成常规买入提醒'}"
            )
        if not good:
            zero_reasons.append(
                "人工买点为0："
                f"{assessment_counts['approved_waiting_entry']}只处于买入评分层但仍待时机/补证，"
                f"{assessment_counts['watch']}只观察，{assessment_counts['rejected']}只排除"
            )
        if not position_rows:
            zero_reasons.append("卖出提醒为0：尚未录入持仓")
        if not account_execution.get("new_buy_plan_eligible"):
            zero_reasons.extend(
                f"账户买入计划不可用：{reason}"
                for reason in (
                    account_execution.get("blocking_reasons")
                    or [account_execution.get("reason") or "账户执行条件未满足"]
                )
            )

        return {
            "date": snapshot_at,
            "pipeline_identity": {
                "id": "unified_opportunity_pipeline_v3",
                "snapshot_as_of": snapshot_at,
                "candidate_sources": [
                    "strict_route", "production_factor", "validated_model",
                    "full_market",
                ],
                "unvalidated_model_candidate_admission": False,
                "ranking_method": "professional_composite_v4_profit_sign_aware",
                "preselection_method": "market_evidence_then_cached_professional_composite",
                "assessment_method": "unified_assessment",
                "strategy_ref": copy.deepcopy(_MANUAL_STRATEGY_REF),
                "scoring_standard": copy.deepcopy(_SCORING_STANDARD),
                "factor_policy": factor_policy,
            },
            "selection_provenance": copy.deepcopy(
                live_signal_snapshot.get("selection_audit") or {
                    "status": "closed_reference_without_forward_manifest",
                    "selection_manifest_available": False,
                    "selection_manifest_required_for_h1_h3": True,
                    "data_asof": full_market_snapshot.get("data_asof"),
                    "eligible_count": full_market_snapshot.get("eligible_count"),
                    "selection_mode": full_market_scan_audit.get("selection_method"),
                    "returns_or_labels_read": False,
                    "reason": (
                        "当前为闭市参考快照；不可变选择清单只在真实交易时段、"
                        "同一时点全截面行情完整时生成，闭市不伪造。"
                    ),
                }
            ),
            "monitoring_scope": {
                "mode": "full_market_multidimensional_then_best_pool",
                "random_rotation": False,
                "full_market_universe_count": (
                    full_market_audit.get("count")
                ),
                "full_market_inspected_count": (
                    full_market_scan_audit.get("inspected_file_count")
                ),
                "full_market_scan_status": full_market_scan_audit.get("status"),
                "full_market_complete_path_scan": (
                    full_market_scan_audit.get("complete_path_scan") is True
                ),
                "full_market_current_asof_count": (
                    full_market_scan_audit.get("current_asof_count")
                ),
                "full_market_scan_exclusion_counts": (
                    full_market_scan_audit.get("exclusion_counts") or {}
                ),
                "full_market_shortlist_count": (
                    full_market_snapshot.get("top_count")
                    or len(full_market_snapshot.get("top") or [])
                ),
                # Compatibility only.  This value has always represented the
                # retained Top-N, not the number of universe files inspected.
                "full_market_screened_count": (
                    full_market_snapshot.get("top_count")
                    or len(full_market_snapshot.get("top") or [])
                ),
                "universe_coverage_label": full_market_audit.get("coverage_label"),
                "full_a_share_coverage": full_market_audit.get("full_a_share_coverage"),
                "included_segments": full_market_audit.get("included_segments") or [],
                "excluded_segments": full_market_audit.get("excluded_segments") or [],
                "excluded_special_securities": (
                    full_market_audit.get("excluded_special_securities") or {}
                ),
                "merged_candidate_count": len(filtered),
                "universe_total": live_monitor_scope.get("universe_total"),
                "quote_attempted": live_monitor_scope.get("quote_attempted"),
                "quote_covered": live_monitor_scope.get("quote_covered"),
                "quote_missing": live_monitor_scope.get("quote_missing"),
                "monitor_selected": live_monitor_scope.get("monitor_selected"),
                "deep_selected": len(assessment_batch),
                "realtime_eligible_coverage_complete": (
                    live_monitor_scope.get("realtime_eligible_coverage_complete") is True
                ),
                "deep_assessed_count": len(items),
            },
            "market_phase": _audited_market_state().get("phase"),
            "decision_gate": decision_gate,
            "buy_window": {
                k: window[k] for k in ("phase", "label", "note",
                                       "countdown_seconds", "countdown_target",
                                       "window_action", "next_session")
                if k in window
            },
            "kronos_model": {
                "generated_at": _kgen or None,
                "source_data_as_of": _kdata_as_of,
                "data_staleness_days": _kstale_days,
                "freshness_status": _kstat.get("freshness_status") or "unknown",
                "validation_status": _kstat.get("validation_status") or "unknown",
                "horizon_semantics": _kstat.get("horizon_semantics"),
                "forecast_calendar": _kstat.get("forecast_calendar"),
                "decision_eligible": False,
                "research_only": True,
                "age_h": _kage_h,
                "count": _kstat.get("count"),
                "reason": _kstat.get("reason"),
            },
            "items": good,
            "model_evidence_items": model_evidence_items,
            "watch_items": watch_items,
            "watch_count": len(watch_items),
            "research_approved_count": sum(
                1 for item in items
                if (item.get("assessment") or {}).get("verdict") == "APPROVE"
            ),
            "waiting_entry_count": sum(
                1 for item in watch_items
                if (item.get("assessment") or {}).get("verdict") == "APPROVE"
            ),
            "candidate_count": len(filtered),
            "assessed_count": len(items),
            "assessment_budget": min(_OPP_ASSESSMENT_BUDGET, len(filtered)),
            "assessment_strategy": {
                "mode": "deterministic_full_market_two_stage_best_pool_v7_all_or_none_preview",
                "batch_size": len(assessment_batch),
                "universe_light_scored": len(filtered),
                "professional_preview_selected_count": sum(
                    1 for row in assessment_batch
                    if _finite_number(
                        (row.get("preselection_professional_preview") or {}).get(
                            "professional_score"
                        )
                    ) is not None
                ),
                "market_evidence_fallback_count": sum(
                    1 for row in assessment_batch
                    if _finite_number(
                        (row.get("preselection_professional_preview") or {}).get(
                            "professional_score"
                        )
                    ) is None
                ),
                "professional_preview_available_count": sum(
                    1 for row in filtered
                    if (row.get("preselection_professional_preview") or {}).get("status")
                    == "available"
                ),
                "professional_preview_scope_count": len(filtered),
                "professional_preview_complete": bool(
                    filtered
                    and all(
                        _finite_number(
                            (row.get("preselection_professional_preview") or {}).get(
                                "professional_score"
                            )
                        ) is not None
                        for row in filtered
                    )
                ),
                "profile_coverage_warmup": {
                    "assessment_batch": assessment_profile_warmup,
                    "top300_backlog": coverage_profile_warmup,
                    "full_market_stratified_backlog": professional_coverage_warmup,
                    "workers": 3,
                    "queue_capacity": _CP_PREHEAT_CAPACITY,
                    "deep_assessment_budget_unchanged": True,
                },
                "selection_rule": (
                    "仅当整个保留池专业综合分预览100%可比时按全站唯一综合分选择深评池；"
                    "覆盖不足时全体统一按市场证据顺序，不得按缓存可用性混排；"
                    "缺失预览不补分；无随机、无轮换"
                ),
            },
            "candidate_source_counts": source_counts,
            "signal_status": {
                "as_of": snapshot_at,
                "triggered_count": triggered_buy_count + triggered_sell_count,
                "triggered_buy_count": triggered_buy_count,
                "triggered_sell_count": triggered_sell_count,
                "assessment_counts": assessment_counts,
                "failed_condition_counts": failed_condition_counts,
                "zero_reasons": zero_reasons if not trigger else [],
                "factor_policy": factor_policy,
                "basis": [
                    "市场、板块与固定战法可独立形成候选；正式因子只补充排序和交叉确认",
                    "统一机会分使用可复核的走势质量、回撤、波动、成本、流动性和专业综合评分",
                    "提醒还需当前行情、买点时机、整手资金与有效交易窗口同时成立",
                    "买入提醒必须同时属于当日严格市场情绪、主线板块与固定战法候选",
                    "持仓卖出仅由明确退出规则或硬风险触发",
                ],
            },
            "account_execution": account_execution,
            "_research_account_snapshot": portfolio_snapshot,
            "position_alerts": position_alerts,
            "_forward_research_candidates": forward_research_candidates,
            "rejected": rejected,
            "rejected_count": len(rejected),
            "trigger": ({"code": trigger["code"], "name": trigger["name"],
                         "action": trigger["alert"]["action"]} if trigger else None),
            "monitor_interval_s": _OPP_REFRESH_S,
            "browser_poll_interval_s": 20,
        }

    def _opportunities_worker() -> None:
        """后台线程：重算机会快照写回 _OPP_STATE（同一时间只有一个 worker）."""
        try:
            source_identity = _full_market_snapshot_identity()
            if not source_identity:
                with _OPP_STATE["lock"]:
                    _OPP_STATE["last_error"] = "full_market_snapshot_identity_unavailable"
                return
            snap = _compute_opportunities()
            if not isinstance(snap, dict) or snap.get("error"):
                reason = (
                    str(snap.get("error") or "机会快照未生成可用结果")
                    if isinstance(snap, dict) else "机会快照返回结构无效"
                )
                with _OPP_STATE["lock"]:
                    _OPP_STATE["last_error"] = reason
                return
            if _full_market_snapshot_identity() != source_identity:
                with _OPP_STATE["lock"]:
                    _OPP_STATE["last_error"] = "full_market_snapshot_changed_during_compute"
                return
            pipeline_identity = dict(snap.get("pipeline_identity") or {})
            pipeline_identity["full_market_snapshot"] = source_identity
            snap["pipeline_identity"] = pipeline_identity
            forward_candidates = snap.pop("_forward_research_candidates", [])
            research_account_snapshot = snap.pop("_research_account_snapshot", {})
            forward_context = {
                "snapshot": copy.deepcopy(snap),
                "candidates": copy.deepcopy(forward_candidates),
                "account": copy.deepcopy(research_account_snapshot),
                "source_identity": source_identity,
            }
            with _OPP_STATE["lock"]:
                if _full_market_snapshot_identity() != source_identity:
                    _OPP_STATE["last_error"] = "full_market_snapshot_changed_before_publish"
                    return
                current = _OPP_STATE.get("result") or {}
                snap.update({
                    key: current[key] for key in _FORWARD_CAPTURE_RESULT_FIELDS
                    if key in current
                })
                if isinstance(_OPP_STATE.get("panel_refresh"), dict):
                    snap["sector_constituent_quote_panel_capture"] = copy.deepcopy(
                        _OPP_STATE["panel_refresh"]
                    )
                _OPP_STATE["result"] = snap
                _OPP_STATE["forward_context"] = forward_context
                _OPP_STATE["ts"] = _time.time()
                _OPP_STATE["last_error"] = None
        except Exception:
            # worker 崩溃必须留痕，避免接口层重复"无快照"死循环
            _OPP_STATE["last_error"] = _traceback.format_exc()
            print(f"[opportunities] worker 异常:\n{_OPP_STATE['last_error']}", flush=True)
        finally:
            with _OPP_STATE["lock"]:
                _OPP_STATE["running"] = False

    def _forward_capture_worker() -> None:
        """Use the prepared snapshot first so a three-minute slot never waits on ranking."""
        started_at = datetime.now(_SH_TZ)
        runtime_path = app.config.get("FORWARD_CAPTURE_RUNTIME_STATUS_PATH")
        _persist_forward_capture_runtime(
            runtime_path, status="running", started_at=started_at,
        )
        try:
            with _OPP_STATE["lock"]:
                context = copy.deepcopy(_OPP_STATE.get("forward_context"))
            if not context:
                error = "forward_capture_context_unavailable"
                with _FORWARD_CAPTURE_STATE["lock"]:
                    _FORWARD_CAPTURE_STATE["last_error"] = error
                _persist_forward_capture_runtime(
                    runtime_path, status="blocked", started_at=started_at,
                    error=error,
                )
                return
            context_check = verify_forward_selection_snapshot(
                context.get("snapshot"), observed_at=datetime.now(_SH_TZ),
            )
            if context_check.get("valid") is not True:
                error = (
                    "forward_capture_context_invalid:"
                    + ",".join(context_check.get("errors") or [])
                )
                with _FORWARD_CAPTURE_STATE["lock"]:
                    _FORWARD_CAPTURE_STATE["last_error"] = error
                _persist_forward_capture_runtime(
                    runtime_path, status="blocked", started_at=started_at,
                    error=error,
                )
                return
            # The ranking is intentionally prepared before the three-minute
            # slot, but the account observation is not.  Re-read the ledger
            # and value current holdings inside the slot so an old prepared
            # snapshot cannot masquerade as a same-slot account observation.
            try:
                current_account = _manual_portfolio_status(
                    app.config["QUOTE_PROVIDER"],
                )
                strict_account = normalize_system_route_account_input(
                    current_account,
                )
            except Exception as exc:
                error = (
                    "forward_account_refresh_or_normalization_failed:"
                    f"{type(exc).__name__}:{exc}"
                )
                with _FORWARD_CAPTURE_STATE["lock"]:
                    _FORWARD_CAPTURE_STATE["last_error"] = error
                _persist_forward_capture_runtime(
                    runtime_path, status="blocked", started_at=started_at,
                    error=error,
                )
                return
            snap = capture_forward_chain(
                context["snapshot"], context["candidates"], strict_account,
                market_intelligence=app.config["MARKET_INTELLIGENCE"],
                candle_provider=app.config["CANDLE_PROVIDER"],
            )
            constituent_capture = (
                snap.get("market_sector_constituent_forward_capture") or {}
            )
            if (
                constituent_capture.get("status")
                in {"captured", "already_captured"}
                and constituent_capture.get("captured") is True
            ):
                try:
                    snap["sector_constituent_quote_panel_capture"] = (
                        start_sector_quote_panel(
                            constituent_capture,
                            app.config["QUOTE_PROVIDER"],
                        )
                    )
                    snap["sector_constituent_quote_panel_capture"].setdefault(
                        "decision_day", started_at.date().isoformat(),
                    )
                except Exception as exc:
                    snap["sector_constituent_quote_panel_capture"] = {
                        "status": "error", "phase": "error", "captured": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "terminal_error": f"{type(exc).__name__}: {exc}",
                        "decision_day": started_at.date().isoformat(),
                        "research_only": True, "trade_action_authority": False,
                    }
            with _OPP_STATE["lock"]:
                current = copy.deepcopy(_OPP_STATE.get("result") or {})
                current.update({
                    key: snap[key] for key in _FORWARD_CAPTURE_RESULT_FIELDS
                    if key in snap
                })
                _OPP_STATE["result"] = current
            with _FORWARD_CAPTURE_STATE["lock"]:
                _FORWARD_CAPTURE_STATE["last_error"] = None
            _persist_forward_capture_runtime(
                runtime_path, status="finished", started_at=started_at,
                snapshot=snap,
            )
        except Exception:
            error = _traceback.format_exc()
            with _FORWARD_CAPTURE_STATE["lock"]:
                _FORWARD_CAPTURE_STATE["last_error"] = error
            _persist_forward_capture_runtime(
                runtime_path, status="failed", started_at=started_at,
                error=error,
            )
        finally:
            with _FORWARD_CAPTURE_STATE["lock"]:
                _FORWARD_CAPTURE_STATE["running"] = False

    def _opportunities_refresher_loop(stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            # Refresh the immutable daily payload on the background cadence so
            # an open dashboard never pays the periodic verification cost.
            try:
                _cached_latest_research_state()
            except Exception:
                pass
            observed_at = datetime.now(_SH_TZ)
            try:
                panel_refresh = refresh_active_sector_quote_panels(
                    app.config["QUOTE_PROVIDER"],
                )
            except Exception as exc:
                panel_refresh = {
                    "status": "error", "active": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                    "research_only": True,
                }
            with _OPP_STATE["lock"]:
                previous_panel = (
                    (_OPP_STATE.get("result") or {}).get(
                        "sector_constituent_quote_panel_capture"
                    ) or {}
                )
                if (
                    panel_refresh.get("phase") == "awaiting_window"
                    and previous_panel.get("phase") == "error"
                    and previous_panel.get("decision_day")
                    == observed_at.date().isoformat()
                ):
                    panel_refresh = previous_panel
                _OPP_STATE["panel_refresh"] = panel_refresh
                current = _OPP_STATE.get("result")
                if isinstance(current, dict):
                    current["sector_constituent_quote_panel_capture"] = panel_refresh
            _maybe_start_account_nav_freeze(observed_at)
            trading_day = _is_trading_day(observed_at)
            schedule_status = _cached_forward_capture_schedule_status(
                now=observed_at,
                is_trading_day=trading_day,
            )
            with _OPP_STATE["lock"]:
                context = _OPP_STATE.get("forward_context") or {}
            wake_policy = forward_capture_wake_policy(
                schedule_status, ordinary_refresh_seconds=_OPP_REFRESH_S,
                prepared_snapshot=context.get("snapshot"),
                observed_at=observed_at,
            )
            capture_needed = bool(wake_policy["capture_needed"])
            pre_slot_guard = bool(wake_policy["pre_slot_guard"])
            context_valid = bool(wake_policy["capture_context_valid"])
            prepare_needed = bool(wake_policy["prepare_needed"])
            current_source_identity = _full_market_snapshot_identity()
            prepare_needed = bool(
                prepare_needed
                or ((pre_slot_guard or capture_needed) and not context_valid)
            )
            with _OPP_STATE["lock"]:
                result = _OPP_STATE["result"]
                has_forward_context = bool(_OPP_STATE.get("forward_context"))
                due = result is None or _time.time() - _OPP_STATE["ts"] >= _OPP_REFRESH_S
                already_running = _OPP_STATE["running"]
                source_changed = _opportunity_full_market_source_changed(
                    result, current_source_identity,
                )
            with _FORWARD_CAPTURE_STATE["lock"]:
                capture_running = _FORWARD_CAPTURE_STATE["running"]
            capture_attempt_key = _forward_capture_attempt_key(schedule_status)
            # 休市也要生成启动后的首份收盘快照，供研究页面读取；已有快照后
            # 源文件换代时必须重建；除此之外只有开市才周期刷新，避免周末空转。
            regular_refresh = bool(
                trading_day and _is_market_open(observed_at) and due
            )
            should_refresh = bool(
                result is None
                or ((source_changed or regular_refresh) and not pre_slot_guard)
            )
            prepare_at = observed_at
            if pre_slot_guard and schedule_status.get("next_slot"):
                try:
                    prepare_at = datetime.fromisoformat(
                        str(schedule_status["next_slot"])
                    ).astimezone(_SH_TZ)
                except ValueError:
                    pass
            prepare_stage = (
                _forward_prepare_stage(observed_at=prepare_at)
                if prepare_needed else None
            )
            if capture_needed and has_forward_context and context_valid:
                if not capture_running:
                    _try_start_forward_capture_worker(
                        _FORWARD_CAPTURE_STATE, _forward_capture_worker,
                        name="forward-capture-worker",
                        attempt_key=capture_attempt_key,
                    )
            elif prepare_needed and prepare_stage == "full_market":
                with _OPP_STATE["lock"]:
                    _OPP_STATE["last_error"] = "blocked_universe_identity"
                if audited_universe(_PROJECT).get("status") == "verified":
                    _try_start_snapshot_worker(
                        _FM_REFRESH_STATE, _refresh_full_market_pool,
                        name="finder-slot-prepare",
                    )
            elif prepare_needed and prepare_stage == "live_signal":
                with _OPP_STATE["lock"]:
                    _OPP_STATE["last_error"] = "preparing_live_selection"
                _try_start_snapshot_worker(
                    _LIVE_SIGNAL_STATE, _live_signal_worker,
                    name="live-signal-slot-prepare",
                )
            elif (
                prepare_needed and prepare_stage == "opportunities"
                and not already_running
            ):
                _try_start_snapshot_worker(
                    _OPP_STATE, _opportunities_worker, name="opp-slot-prepare",
                )
            elif should_refresh and not already_running:
                _try_start_snapshot_worker(
                    _OPP_STATE, _opportunities_worker, name="opp-worker",
                )
            wait_observed_at = datetime.now(_SH_TZ)
            with _OPP_STATE["lock"]:
                worker_running = bool(_OPP_STATE["running"])
                last_completed_at = _OPP_STATE["ts"]
            wait_seconds = _opportunity_refresh_wait_seconds(
                observed_at=wait_observed_at,
                now_epoch=_time.time(),
                last_completed_at=last_completed_at,
                worker_running=worker_running,
                policy_wait_seconds=wake_policy["wait_seconds"],
                trading_day=_is_trading_day(wait_observed_at),
            )
            if stop_event.wait(wait_seconds):
                break

    _LIVE_SIGNAL_STATE: dict = {
        "last_run": 0.0,
        "running": False,
        "closed_refresh_date": None,
        "lock": threading.Lock(),
    }

    def _live_signal_refresh_needed() -> tuple[bool, bool]:
        """Return (due, closing_refresh) without polling during closed hours."""
        current_day = datetime.now(_SH_TZ).date().isoformat()
        with _LIVE_SIGNAL_STATE["lock"]:
            seconds_since_run = _time.time() - _LIVE_SIGNAL_STATE["last_run"]
            closing_refresh_attempted = (
                _LIVE_SIGNAL_STATE["closed_refresh_date"] == current_day
            )
        existing = load_live_signals() or {}
        source_time = (
            (existing.get("quote_meta") or {}).get("as_of")
            or existing.get("generated_at")
        )
        freshness = _fund_flow_freshness(source_time)
        return _live_signal_refresh_decision(
            trading_day=_is_trading_day(),
            market_open=_is_market_open(),
            seconds_since_run=seconds_since_run,
            closing_refresh_attempted=closing_refresh_attempted,
            persisted_freshness_state=str(freshness.get("state") or "unknown"),
        )

    def _live_signal_refresher_loop(stop_event: threading.Event) -> None:
        """Refresh in-session, plus one evidence-changing close snapshot."""
        due, closing_refresh = _live_signal_refresh_needed()
        if due:
            if closing_refresh:
                with _LIVE_SIGNAL_STATE["lock"]:
                    _LIVE_SIGNAL_STATE["closed_refresh_date"] = (
                        datetime.now(_SH_TZ).date().isoformat()
                    )
            _try_start_snapshot_worker(
                _LIVE_SIGNAL_STATE, _live_signal_worker, name="live-signal-worker",
            )
        while not stop_event.wait(60):
            due, closing_refresh = _live_signal_refresh_needed()
            with _LIVE_SIGNAL_STATE["lock"]:
                already_running = _LIVE_SIGNAL_STATE["running"]
                if due and closing_refresh:
                    _LIVE_SIGNAL_STATE["closed_refresh_date"] = (
                        datetime.now(_SH_TZ).date().isoformat()
                    )
            if due and not already_running:
                _try_start_snapshot_worker(
                    _LIVE_SIGNAL_STATE, _live_signal_worker, name="live-signal-worker",
                )

    def _live_signal_worker() -> None:
        """在独立线程里跑一次实时信号刷新（akshare 主源 → /api/data/quote 降级）。"""
        errors = []
        try:
            _signal_run_once()
            _cash_floor_holding_horizon_report()
        except Exception as exc:  # 兜底：不抛给线程，写进 state 便于诊断
            errors.append(f"live_signal: {type(exc).__name__}: {exc}")
        finally:
            with _LIVE_SIGNAL_STATE["lock"]:
                _LIVE_SIGNAL_STATE["last_error"] = "; ".join(errors) or None
                _LIVE_SIGNAL_STATE["running"] = False
                _LIVE_SIGNAL_STATE["last_run"] = _time.time()

    def _paper_forward_refresher_loop(stop_event: threading.Event) -> None:
        """全时启动的日线前向证据检查；不依赖当日是否开市。"""
        while not stop_event.is_set():
            try:
                user_strategy_refresh_paper_forward()
            except Exception as exc:
                print(
                    f"[paper-forward] {type(exc).__name__}: {exc}",
                    flush=True,
                )
            if stop_event.wait(60):
                break

    @app.get("/api/opportunities")
    def opportunities_api():
        view = request.args.get("view", "compact")
        if view not in {"compact", "monitor", "evidence"}:
            return jsonify({"error": "view 仅支持 compact/monitor/evidence"}), 400
        now = _time.time()
        current_source_identity = _full_market_snapshot_identity()
        decision_gate = (
            _decision_gate_status() if view == "evidence"
            else _fast_compact_decision_gate()
        )

        def _present(snapshot: dict) -> dict:
            if view == "evidence":
                payload = dict(snapshot)
            elif view == "monitor":
                payload = _monitor_opportunities_snapshot(snapshot)
            else:
                payload = _compact_opportunities_snapshot(snapshot)
            if view == "monitor":
                payload["decision_gate"] = decision_gate
                payload["evidence_view"] = "/api/opportunities?view=evidence"
                return payload
            # The scored full-market snapshot is intentionally frozen while the
            # financial-only worker repairs coverage.  Overlay only the live
            # read-only job ledger so the UI does not report an hours-old queue.
            assessment_strategy = copy.deepcopy(
                payload.get("assessment_strategy") or {}
            )
            warmup = assessment_strategy.get("profile_coverage_warmup") or {}
            backlog = warmup.get("full_market_stratified_backlog") or {}
            epoch_id = str(backlog.get("epoch_id") or "")
            if epoch_id:
                try:
                    from .professional_coverage import coverage_job_status
                    backlog["jobs"] = coverage_job_status(
                        _PROJECT / "research" / "professional_coverage.sqlite3",
                        epoch_id,
                    )
                    backlog["jobs_live_read"] = True
                except Exception as exc:
                    backlog["jobs_live_read"] = False
                    backlog["jobs_live_error_type"] = type(exc).__name__
                warmup["full_market_stratified_backlog"] = backlog
                assessment_strategy["profile_coverage_warmup"] = warmup
                payload["assessment_strategy"] = assessment_strategy
            for field in (
                "items", "model_evidence_items", "watch_items", "rejected",
            ):
                raw_rows = snapshot.get(field) or []
                shown_rows = payload.get(field) or []
                decorated = []
                for index, shown in enumerate(shown_rows):
                    raw = raw_rows[index] if index < len(raw_rows) else shown
                    row = (
                        copy.deepcopy(shown) if view == "evidence"
                        else dict(shown)
                    ) if isinstance(shown, dict) else {}
                    row.update(_opportunity_score_contract(
                        (raw or {}).get("assessment") if isinstance(raw, dict) else {},
                        (
                            (raw or {}).get("condition_match_score", (raw or {}).get("score"))
                            if isinstance(raw, dict) else None
                        ),
                    ))
                    if view == "evidence":
                        company = row.get("company")
                        company_composite = (
                            company.get("composite")
                            if isinstance(company, dict)
                            and isinstance(company.get("composite"), dict)
                            else None
                        )
                        assessment = row.get("assessment")
                        assessment_gates = (
                            assessment.get("gates")
                            if isinstance(assessment, dict)
                            and isinstance(assessment.get("gates"), dict)
                            else {}
                        )
                        assessment_composite = assessment_gates.get("composite")
                        for composite in (
                            company_composite,
                            assessment_composite
                            if isinstance(assessment_composite, dict) else None,
                        ):
                            if isinstance(composite, dict):
                                composite.update(
                                    _public_research_composite(composite)
                                )
                    decorated.append(_public_score_roles(row))
                payload[field] = decorated
            payload["decision_gate"] = decision_gate
            payload["evidence_view"] = "/api/opportunities?view=evidence"
            if _opportunity_full_market_source_changed(
                snapshot, current_source_identity,
            ):
                return payload
            return _apply_current_market_runtime_contract(payload)

        with _OPP_STATE["lock"]:
            result = _OPP_STATE["result"]
            freshness = _opportunity_snapshot_freshness(
                result, _OPP_STATE.get("ts"), now=now,
                session_open=_is_market_open(),
            )
            result_failed = freshness["failed"]
            source_changed = _opportunity_full_market_source_changed(
                result, current_source_identity,
            )
            fresh = bool(freshness["current"] and not source_changed)
            running = _OPP_STATE["running"]
            last_error = _OPP_STATE.get("last_error") or (
                str(result.get("error")) if result_failed else None
            )
        if fresh:
            return jsonify({
                **_present(result), "cached": True,
                "cached_age_s": (
                    round(freshness["age_s"], 1)
                    if freshness["age_s"] is not None else None
                ),
                "stale": False,
                "snapshot_current_max_age_s": freshness["max_age_s"],
                "snapshot_status": (
                    "live" if _is_market_open() else "closed_snapshot"
                ),
            })
        if result is None:
            if last_error and not running:
                return jsonify({"error": "机会快照生成失败，暂无可用快照",
                                "items": [], "watch_items": [], "rejected": [],
                                "generating": False, "stale": True,
                                "snapshot_current_max_age_s": freshness["max_age_s"],
                                "last_error": "snapshot_generation_failed",
                                "decision_gate": decision_gate,
                                "evidence_view": "/api/opportunities?view=evidence",
                                "refresh_endpoint": "/api/opportunities/refresh"}), 503
            payload = {"items": [], "watch_items": [], "rejected": [], "trigger": None,
                       "decision_gate": decision_gate,
                       "evidence_view": "/api/opportunities?view=evidence",
                       "buy_window": get_buy_window(),
                       "generating": bool(running), "cached": False, "stale": True,
                       "snapshot_current_max_age_s": freshness["max_age_s"],
                       "cached_age_s": None,
                       "refresh_endpoint": "/api/opportunities/refresh",
                       "last_error": "snapshot_generation_failed" if last_error else None}
            return jsonify(payload), 202
        if last_error:
            return jsonify({"error": "机会快照刷新失败；现有快照已过期",
                            "items": [], "watch_items": [], "rejected": [],
                            "generating": bool(running), "stale": True,
                            "snapshot_current_max_age_s": freshness["max_age_s"],
                            "last_error": "snapshot_generation_failed",
                            "stale_snapshot_available": bool(result),
                            "decision_gate": decision_gate,
                            "evidence_view": "/api/opportunities?view=evidence",
                            "refresh_endpoint": "/api/opportunities/refresh"}), 503
        return jsonify({**_present(result), "cached": True, "stale": True,
                        "snapshot_status": "stale",
                        "snapshot_current_max_age_s": freshness["max_age_s"],
                        "cached_age_s": (
                            round(freshness["age_s"], 1)
                            if freshness["age_s"] is not None else None
                        ),
                        "refreshing": bool(running),
                        "refresh_endpoint": "/api/opportunities/refresh"})

    @app.post("/api/opportunities/refresh")
    def opportunities_refresh_api():
        return _snapshot_refresh_response(_OPP_STATE, _opportunities_worker,
            name="opp-manual", status_endpoint="/api/opportunities")
    # ── API: 个人排除名单（老邓股/已持仓）────────────────
    @app.get("/api/exclusions")
    def exclusions_api():
        return jsonify({"items": [
            {"code": code, **info} for code, info in _load_exclusions().items()
        ]})

    @app.post("/api/exclusions")
    def exclusions_add():
        try:
            body = _strict_json_payload(allowed={"code", "name"}, required={"code"})
            code = str(body.get("code") or "").strip()
            name = str(body.get("name") or "").strip()
            if not CODE_PATTERN.fullmatch(code):
                raise ValueError("仅支持沪深6位股票代码")
            name = _validated_display_name(name)
            name = name or code
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        existing = _load_exclusions().get(code)
        if existing:
            if str(existing.get("name") or "") == name:
                return jsonify({
                    "ok": True, "item": {"code": code, **existing},
                    "idempotent_replay": True,
                })
            return jsonify({"error": "该股票已在排除名单，拒绝静默覆盖"}), 409
        entry = _add_exclusion(code, name)
        return jsonify({"ok": True, "item": {"code": code, **entry}}), 201

    @app.delete("/api/exclusions/<code>")
    def exclusions_del(code):
        if not CODE_PATTERN.fullmatch(str(code or "")):
            return jsonify({"error": "仅支持沪深6位股票代码"}), 400
        removed = _remove_exclusion(code)
        if not removed:
            return jsonify({"ok": False, "error": "排除项不存在"}), 404
        return jsonify({"ok": True, "removed": True})

    # ── API: 个股工作台本机审计快照 ───────────
    @app.get("/api/stock/<code>/terminal-snapshot")
    def terminal_snapshot_api(code: str):
        """Local-first bootstrap for the institutional stock terminal.

        It never depends on the slower minute feed.  When the workstation's
        audited files are absent on a cloud host, a shape-validated public daily
        fallback keeps the chart visible but is explicitly ineligible for an
        execution price, factor admission, or a trading signal.
        """

        normalized = code.strip().split(".")[0]
        if not CODE_PATTERN.fullmatch(normalized):
            return jsonify({"error": "仅支持沪深6位股票代码"}), 400
        try:
            candles, audit = _audited_daily_snapshot(normalized, 200)
        except CandleDataUnavailable as local_exc:
            try:
                candles, audit = _runtime_public_daily_snapshot(
                    normalized, app.config["CANDLE_PROVIDER"], 200,
                )
            except CandleDataUnavailable as public_exc:
                combined = CandleDataUnavailable(
                    "本机审计日线与云端公开日线均不可用"
                )
                combined.add_note(f"local={type(local_exc).__name__}")
                combined.add_note(f"public={type(public_exc).__name__}")
                return internal_error_response(
                    app, "日线数据暂不可用", combined, status=503,
                    extra={"code": normalized},
                )

        latest, previous = candles[-1], candles[-2]
        decision_price_eligible = audit.get("decision_price_eligible") is True
        valid_execution_day = (
            next_trading_day_after(datetime.fromisoformat(latest.time))
            if decision_price_eligible else None
        )
        names = _audited_stock_names()
        name = names.get(normalized) or "名称不可用"
        name_status = "available" if name != "名称不可用" else "unavailable"
        exchange_time = f"{latest.time}T15:00:00+08:00"
        served_at = datetime.now(_SH_TZ).isoformat(timespec="seconds")
        pct_chg = ((latest.close / previous.close - 1) * 100) if previous.close else None
        quote = {
            "code": normalized,
            "name": name,
            "name_status": name_status,
            "price": latest.close,
            "change_pct": round(pct_chg, 4) if pct_chg is not None else None,
            "amount": latest.amount,
            "open_price": latest.open,
            "high": latest.high,
            "low": latest.low,
            "previous_close": previous.close,
            "volume": latest.volume,
            "data_time": exchange_time,
            "exchange_data_time": exchange_time,
            "fetched_at": served_at,
            "served_at": served_at,
            "source": str(audit["source"]),
            "freshness": "closed_snapshot",
        }
        technical = technical_snapshot(candles)
        technical_history_ready = audit.get("technical_history_ready", True) is not False
        if technical_history_ready:
            result = build_stock_analysis(
                code=normalized,
                name=name,
                candles=candles,
                quote=quote,
                interval="1d",
                market_regime=_simple_regime({}),
                research_gate_passed=False,
            )
            technical["values"] = technical_values(candles)
        else:
            bars = len(candles)
            result = {
                "state": {
                    "code": "insufficient_history",
                    "label": "审计行情可用，技术样本不足",
                    "score": None,
                    "score_range": [-4, 4],
                    "reasons": [f"已取得 {bars} 根本机审计日线，可展示真实K线与收盘行情"],
                    "cautions": [f"技术状态至少需要35根，当前缺少 {35 - bars} 根"],
                },
                "signals": [],
            }
            technical.update({
                "values": {},
                "history_bars": bars,
                "required_history_bars": 35,
                "missing_history_bars": 35 - bars,
                "message": (
                    f"本机审计日线可用（{bars}根）；技术状态至少需要35根，"
                    "样本积累前不生成技术买卖点。"
                ),
            })
        serialized = [
            {
                "time": candle.time,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
                "amount": candle.amount,
            }
            for candle in candles
        ]
        analysis_state = (
            result.get("state")
            if isinstance(result.get("state"), dict)
            else {}
        )
        public_state = copy.deepcopy(analysis_state)
        technical_condition_balance = public_state.pop("score", None)
        public_state["technical_condition_balance"] = technical_condition_balance
        public_state["technical_condition_scale"] = {"min": -4, "max": 4}
        public_state["score_role"] = "technical_diagnostic_not_overall_score"
        return jsonify({
            "code": normalized,
            "name": name,
            "name_status": name_status,
            "quote": quote,
            "state": public_state,
            "technical_condition_balance": technical_condition_balance,
            "technical_condition_scale": {"min": -4, "max": 4},
            "score_role": "technical_diagnostic_not_overall_score",
            "signals": result.get("signals"),
            # build_stock_analysis exposes the audited markers as `signals`.
            # Keep the API alias for the terminal instead of silently sending [] .
            "research_signals": result.get("signals", []),
            "quant": quant_stock_view(normalized),
            "technical": technical,
            "candles": serialized,
            "execution_plan": {
                "signal_date": latest.time,
                "valid_execution_date": (
                    valid_execution_day.isoformat()
                    if valid_execution_day is not None else None
                ),
                "price_basis": audit.get("price_basis"),
                "calendar_source": "audited_baostock_calendar",
                "decision_price_eligible": decision_price_eligible,
            },
            "meta": {
                "data_time": latest.time,
                "market": _audited_market_state(),
                "snapshot_source": audit["source"],
                "snapshot_file": audit["file"],
                "as_of": audit["as_of"],
                "is_st": audit["is_st"],
                "history_bars": audit.get("history_bars", len(candles)),
                "technical_history_required": audit.get("technical_history_required", 35),
                "technical_history_ready": audit.get("technical_history_ready", len(candles) >= 35),
                "terminal_mode": audit.get("terminal_mode", "audited_daily_snapshot"),
                "adjust_type": "raw" if decision_price_eligible else audit.get("adjust_type", "unknown"),
                "price_basis": audit.get("price_basis"),
                "decision_price_eligible": decision_price_eligible,
                "runtime_validation": audit.get("runtime_validation"),
            },
            "v2_backtest": _v2_backtest_status(),
        })

    # ── API: 个股分析 ─────────────────────────
    @app.get("/api/stock/<code>")
    def stock_analysis_api(code: str):
        normalized = code.strip().split(".")[0]
        if not CODE_PATTERN.fullmatch(normalized):
            return jsonify({"error": "仅支持沪深6位股票代码"}), 400

        # 获取行情
        try:
            quotes = app.config["QUOTE_PROVIDER"].get_quotes(
                list(MARKET_INDEX_CODES) + [normalized]
            )
        except MarketDataUnavailable as exc:
            return internal_error_response(
                app, "公开行情暂不可用", exc, status=503,
                extra={"code": normalized},
            )

        quote_obj = (quotes or {}).get(normalized)
        quote = quote_obj.to_dict() if quote_obj else None
        name = (quote or {}).get("name", normalized)

        # 盘口与流动性（Level-1 五档 + 量比 + 换手率 + 委差 + 均价 + 最近一笔）：
        # 腾讯公开行情补充（副本合并，不改共享 Quote dataclass）。失败保持诚实
        # "未接入/不可计算"，绝不伪造盘口，也不阻塞整个分析请求。
        if quote is not None:
            quote["level2_available"] = False
            quote["level2_status"] = "licensed_source_not_configured"
            quote["market_depth_level"] = "unavailable"
            try:
                from .market_intelligence import tencent_stock_depth
                depth = tencent_stock_depth(normalized)
                if depth:
                    for _field in ("depth", "outer_volume", "inner_volume", "volume_ratio",
                                   "turnover_rate", "avg_price", "bid_ask_delta",
                                   "latest_tick", "data_time"):
                        quote[_field] = depth.get(_field)
                    quote["depth_source"] = "腾讯财经公开行情（qt.gtimg.cn）· Level-1"
                    quote["market_depth_level"] = "level1"
            except Exception:
                pass

        # 资格检查
        eligible, reason = stock_is_eligible(
            normalized,
            name,
            (quote or {}).get("price") if quote else None,
        )
        if not eligible:
            return jsonify({"error": reason, "code": normalized, "name": name}), 400

        # A missing minute feed must not discard a quote already obtained.
        minute_status = {"status": "available", "completed_bars": None, "message": None}
        try:
            candles = app.config["CANDLE_PROVIDER"].get_candles(normalized, 400, interval="5m")
        except CandleDataUnavailable:
            candles = []
            minute_status.update(status="unavailable", message="分钟K线暂不可用；已取得的报价继续显示，技术分析暂停。")

        # 构建分析
        market_regime = _simple_regime((quotes or {}))
        signal_candles = completed_signal_candles(candles, "5m")
        if minute_status["status"] == "available":
            minute_status["completed_bars"] = len(signal_candles)
            if len(signal_candles) < 35:
                minute_status.update(status="insufficient_history",
                                     message=f"完整分钟K线仅 {len(signal_candles)} 根，技术分析至少需要35根；报价继续显示。")
        if minute_status["status"] == "available":
            result = build_stock_analysis(
                code=normalized, name=name, candles=signal_candles, quote=quote,
                interval="5m", market_regime=market_regime, research_gate_passed=False,
            )
        else:
            result = {
                "state": {"code": minute_status["status"], "label": minute_status["message"],
                          "score": None, "reasons": [], "cautions": [minute_status["message"]]},
                "technical": None, "signals": [],
                "decision": {"action": "等待完整分钟数据", "execution_allowed": False,
                             "research_gate_passed": False, "gate_reason": minute_status["message"],
                             "buy_scenario": None, "sell_scenario": None},
            }

        # 量化视图
        quant_view = quant_stock_view(normalized)

        # 止盈/止损线（供分时图与K线图叠加显示区域）
        _q = quote or {}
        _levels = compute_levels(
            _q.get("price") or _q.get("previous_close"),
            code=normalized, prev_close=_q.get("previous_close"),
        )

        state = copy.deepcopy(result.get("state") or {})
        technical_condition_balance = state.pop("score", None)
        state["technical_condition_balance"] = technical_condition_balance
        state["technical_condition_scale"] = {"min": -4, "max": 4}
        state["score_role"] = "technical_diagnostic_not_overall_score"
        decision = result.get("decision") or {}
        return jsonify({
            "code": normalized,
            "name": name,
            "quote": quote,
            "state": state,
            "technical_condition_balance": technical_condition_balance,
            "technical_condition_scale": {"min": -4, "max": 4},
            "score_role": "technical_diagnostic_not_overall_score",
            "data_status": {"minute_analysis": minute_status},
            "decision": decision,
            "buy_scenario": decision.get("buy_scenario"),
            "sell_scenario": decision.get("sell_scenario"),
            "technical": result.get("technical"),
            "signals": result.get("signals"),
            "research_signals": result.get("signals", []),
            "quant": quant_view,
            "levels": _levels,
        })

    # ── API: K线数据（多周期） ────────────────
    @app.get("/api/candles/<code>")
    def candles_api(code: str):
        normalized = code.strip().split(".")[0]
        if not CODE_PATTERN.fullmatch(normalized):
            return jsonify({"error": "仅支持沪深6位股票代码"}), 400
        interval = request.args.get("interval", "5m")
        if interval not in ("1m", "5m", "15m", "30m", "1d"):
            interval = "5m"
        count = request.args.get("count", 200, type=int)

        try:
            candles = app.config["CANDLE_PROVIDER"].get_candles(
                normalized,
                max(count, 100),
                interval=interval,
                cache_ttl_seconds=(5 if request.args.get("live") == "1" else None),
            )
        except CandleDataUnavailable as exc:
            return internal_error_response(
                app, "K线数据暂不可用", exc, status=503,
                extra={"code": normalized},
            )

        serialized = []
        for c in candles[-count:]:
            t = c.time
            if hasattr(t, 'isoformat'):
                t = t.isoformat()
            else:
                t = str(t)
            serialized.append({
                "time": t,
                "open": c.open, "high": c.high,
                "low": c.low, "close": c.close,
                "volume": c.volume,
            })
        state = _audited_market_state()
        now = datetime.now(_SH_TZ)
        latest_date = serialized[-1]["time"][:10] if serialized else None
        forming = bool(
            latest_date == now.date().isoformat()
            and state["phase"] in {"auction", "open", "lunch"}
        )
        origin = candle_price_origin(candles[-count:])
        return jsonify({
            "code": normalized,
            "interval": interval,
            "candles": serialized,
            "meta": {
                "data_time": serialized[-1]["time"] if serialized else None,
                "market": state,
                "source": {
                    "tencent_fqkline": "腾讯公开日线",
                    "tencent_mkline": "腾讯公开分钟K线",
                    "akshare_daily": "AkShare公开日线",
                    "akshare_fallback": "AkShare公开日线",
                    "akshare_minute": "AkShare公开分钟K线",
                }.get((origin or {}).get("source"), "公开K线（本次来源待核验）"),
                "data_origin": origin,
                "decision_price_eligible": False,
                "adjust_type": (origin or {}).get("adjust_type", "unknown"),
                "is_forming": forming,
                "bar_status": "盘中形成中，不作为收盘信号" if forming else "已完成K线",
                "refresh_seconds": 10 if state["phase"] == "open" else None,
            },
        })

    # ── API: 今日真实分时（分时图专用） ──────────
    @app.get("/api/candles/<code>/intraday")
    def intraday_api(code: str):
        """当前交易日真实分时：1分钟收盘价 + VWAP 均价 + 昨收 + 数据时间戳."""
        normalized = code.strip().split(".")[0]
        if not CODE_PATTERN.fullmatch(normalized):
            return jsonify({"error": "仅支持沪深6位股票代码"}), 400
        try:
            data = app.config["CANDLE_PROVIDER"].get_intraday(normalized)
        except CandleDataUnavailable as exc:
            return internal_error_response(
                app, "分时数据暂不可用", exc, status=503,
                extra={"code": normalized},
            )
        # 分时图专业增强（与全站 board_limit 同口径）：板块涨跌幅、
        # 涨跌停参考价 / 今日开高低 / 数据时点。全部只做计算，不伪造来源。
        board_limit_pct = 20 if normalized.startswith(("300", "301", "688", "689")) else 10
        prev_close = data.get("previous_close")
        limit_up = limit_down = None
        if prev_close not in (None, 0):
            limit_up = round(float(prev_close) * (1 + board_limit_pct / 100.0), 2)
            limit_down = round(float(prev_close) * (1 - board_limit_pct / 100.0), 2)
        today = {"open": None, "high": None, "low": None}
        try:
            quote = (app.config["QUOTE_PROVIDER"].get_quotes([normalized]) or {}).get(normalized)
            if quote:
                today = {
                    "open": getattr(quote, "open_price", None),
                    "high": getattr(quote, "high", None),
                    "low": getattr(quote, "low", None),
                }
        except Exception:
            today = {"open": None, "high": None, "low": None}
        data["board_limit_pct"] = board_limit_pct
        data["limit_up_price"] = limit_up
        data["limit_down_price"] = limit_down
        data["today"] = today
        data["as_of"] = data.get("data_time")
        # Provider metadata historically guessed from weekday/time. The API
        # boundary replaces it with the audited calendar-derived session.
        data["market"] = _audited_market_state()
        return jsonify(data)

    # ── API: 全周期K线（分时+15K+60K+日K） ─────
    @app.get("/api/candles/<code>/all")
    def candles_all_timeframes(code: str):
        """一次性返回四个周期的K线数据."""
        normalized = code.strip().split(".")[0]
        if not CODE_PATTERN.fullmatch(normalized):
            return jsonify({"error": "仅支持沪深6位股票代码"}), 400

        result = {"code": normalized, "timeframes": {}}
        configs = [
            ("1m", 240, "分时"),     # 分时线（最近240分钟）
            ("15m", 200, "15分钟"),   # 15分钟K
            ("1d", 120, "日K"),       # 日K
        ]
        for interval, count, label in configs:
            try:
                candles = app.config["CANDLE_PROVIDER"].get_candles(
                    normalized, count, interval=interval,
                )
                serialized = []
                for c in candles:
                    t = c.time
                    if hasattr(t, 'isoformat'):
                        t = t.isoformat()
                    else:
                        t = str(t)
                    serialized.append({
                        "time": t,
                        "open": c.open, "high": c.high,
                        "low": c.low, "close": c.close,
                        "volume": c.volume,
                    })
                result["timeframes"][label] = {
                    "candles": serialized,
                    "data_time": serialized[-1]["time"] if serialized else None,
                }
            except CandleDataUnavailable:
                result["timeframes"][label] = {"candles": [], "data_time": None}
        result["meta"] = {"market": _audited_market_state()}

        # 添加技术分析摘要
        try:
            quotes = app.config["QUOTE_PROVIDER"].get_quotes([normalized])
            q = (quotes or {}).get(normalized)
            if q:
                result["quote"] = {
                    "price": getattr(q, "price", None),
                    "change_pct": getattr(q, "change_pct", None),
                    "previous_close": getattr(q, "previous_close", None),
                    "name": getattr(q, "name", ""),
                    "freshness": getattr(q, "freshness", "unknown"),
                }
        except Exception:
            result["quote"] = None

        # 量化视图
        result["quant"] = quant_stock_view(normalized)

        return jsonify(result)

    # ── API: AI 分析 ──────────────────────────
    @app.post("/api/ai/analyze")
    def ai_analyze():
        return paid_api_disabled_response()

    # ── API: 市场宽度 ──────────────────────────
    @app.get("/api/market/breadth")
    def market_breadth():
        """涨跌家数统计：只使用通过字段校验的资金流快照。"""
        ff_path = _PROJECT / "research" / "fund_flow_live.json"
        if not ff_path.exists():
            return jsonify({"error": "资金流数据不可用"}), 503

        try:
            stocks, quality = _load_fund_flow_snapshot(ff_path)
        except (OSError, ValueError, json.JSONDecodeError):
            return jsonify({"error": "数据读取失败"}), 503

        excluded = sum(1 for data in stocks if data.get("pct_chg") is None)
        return jsonify({
            **_fund_flow_breadth(stocks),
            "excluded_invalid_pct_rows": excluded,
            "source_time": quality["source_time"],
            "data_quality": quality,
            "server_time": datetime.now(_SH_TZ).isoformat(),
        })

    # ── API: 资金流排名 ────────────────────────
    @app.get("/api/fund-flow/ranking")
    def fund_flow_ranking():
        """全市场资金流排名，支持排序和筛选."""
        sort_by = request.args.get("sort", "net")
        direction = request.args.get("dir", "desc")
        keyword = request.args.get("q", "").strip()
        unknown = sorted(set(request.args) - {"sort", "dir", "limit", "q"})
        try:
            limit = _bounded_int(
                request.args.get("limit", "50"), "limit", minimum=1, maximum=200,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if unknown:
            return jsonify({"error": f"未知查询参数：{', '.join(unknown)}"}), 400
        if sort_by not in {"net", "inflow", "turnover", "pct_chg"}:
            return jsonify({"error": "sort 仅支持 net/inflow/turnover/pct_chg"}), 400
        if direction not in {"asc", "desc"}:
            return jsonify({"error": "dir 仅支持 asc/desc"}), 400
        if keyword and not CODE_PATTERN.fullmatch(keyword):
            return jsonify({"error": "资金流搜索必须是沪深6位股票代码"}), 400

        ff_path = _PROJECT / "research" / "fund_flow_live.json"
        if not ff_path.exists():
            return jsonify({"error": "资金流数据不可用"}), 503

        try:
            all_records, quality = _load_fund_flow_snapshot(ff_path)
        except (OSError, ValueError, json.JSONDecodeError):
            return jsonify({"error": "数据读取失败"}), 503

        market_summary = _fund_flow_market_summary(all_records)
        items = [record.copy() for record in all_records if not keyword or keyword in record["code"]]
        # 涨停失真标注：涨停封板量大单对冲 → 主力净流出不代表真实离场
        for r in items:
            pct = r["pct_chg"]
            limit_pct = 19.5 if r["code"].startswith(("300", "688")) else 9.5
            r["limit_up"] = bool(pct is not None and pct >= limit_pct)
            r["limit_up_net_out"] = bool(r["limit_up"] and r["net_yi"] < 0)

        # 排序
        reverse = direction == "desc"
        if sort_by == "inflow":
            items.sort(key=lambda x: x["inflow_yi"], reverse=reverse)
        elif sort_by == "turnover":
            items.sort(key=lambda x: x["turnover"], reverse=reverse)
        elif sort_by == "pct_chg":
            valid_pct = [item for item in items if item["pct_chg"] is not None]
            missing_pct = [item for item in items if item["pct_chg"] is None]
            valid_pct.sort(key=lambda x: x["pct_chg"], reverse=reverse)
            items = valid_pct + missing_pct
        else:
            items.sort(key=lambda x: x["net_yi"], reverse=reverse)

        total = len(items)
        items = items[:limit]

        # 补充名称；摘要的极值也需要名称，但绝不改变资金流快照本身。
        summary_codes = [
            item["code"] for item in (market_summary.get("top_in"), market_summary.get("top_out"))
            if item
        ]
        names = _batch_names(list(dict.fromkeys([i["code"] for i in items] + summary_codes)))

        for i in items:
            i["name"] = names.get(i["code"], "名称不可用")
            i["name_status"] = "available" if i["name"] != "名称不可用" else "unavailable"
        for key in ("top_in", "top_out"):
            if market_summary[key]:
                market_summary[key] = market_summary[key].copy()
                market_summary[key]["name"] = names.get(
                    market_summary[key]["code"], "名称不可用"
                )
                market_summary[key]["name_status"] = (
                    "available" if market_summary[key]["name"] != "名称不可用"
                    else "unavailable"
                )

        return jsonify({
            "items": items,
            "total": total,
            "limit": limit,
            "market_summary": market_summary,
            "data_quality": quality,
            "source_time": quality["source_time"],
            "server_time": datetime.now(_SH_TZ).isoformat(),
            "magnitude": {
                "scale": FLOW_MAGNITUDE_SCALE,
                "unit": "亿元（统一折算）",
                "note": "净额、流入与流出均已按审计比例统一折算；raw_* 字段仅用于溯源。",
            },
        })

    # ── API: 资金流板块汇总 ────────────────────
    @app.get("/api/fund-flow/sectors")
    def fund_flow_sectors():
        """按新浪行业聚合资金流：净流入/净流出/家数/异动家数."""
        ff_path = _PROJECT / "research" / "fund_flow_live.json"
        if not ff_path.exists():
            return jsonify({"error": "资金流数据不可用"}), 503
        try:
            all_records, quality = _load_fund_flow_snapshot(ff_path)
        except (OSError, ValueError, json.JSONDecodeError):
            return jsonify({"error": "数据读取失败"}), 503

        industry_map_path = _PROJECT / "research" / "industry_map.json"
        industry_map: dict[str, str] = {}
        if industry_map_path.exists():
            try:
                industry_map = json.loads(industry_map_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                industry_map = {}

        from collections import defaultdict
        agg: dict[str, dict] = defaultdict(lambda: {
            "count": 0, "net_yi": 0.0, "inflow_yi": 0.0, "outflow_yi": 0.0,
            "positive": 0, "negative": 0, "limit_up_net_out": 0,
        })
        for r in all_records:
            ind = industry_map.get(r["code"], "")
            if not ind:
                continue
            a = agg[ind]
            a["count"] += 1
            a["net_yi"] += r["net_yi"]
            a["inflow_yi"] += r["inflow_yi"]
            a["outflow_yi"] += r["outflow_yi"]
            if r["net_yi"] > 0.1:
                a["positive"] += 1
            elif r["net_yi"] < -0.1:
                a["negative"] += 1
            # 涨停但主力净流出 → 失真标注
            pct = r["pct_chg"]
            limit_pct = 19.5 if r["code"].startswith(("300", "688")) else 9.5
            if pct is not None and pct >= limit_pct and r["net_yi"] < 0:
                a["limit_up_net_out"] += 1

        sectors = []
        for name, a in agg.items():
            sectors.append({
                "industry": name,
                "count": a["count"],
                "net_yi": round(a["net_yi"], 2),
                "inflow_yi": round(a["inflow_yi"], 1),
                "outflow_yi": round(a["outflow_yi"], 1),
                "positive": a["positive"],
                "negative": a["negative"],
                "limit_up_net_out": a["limit_up_net_out"],
            })
        sectors.sort(key=lambda s: s["net_yi"], reverse=True)
        return jsonify({
            "sectors": sectors,
            "total_sectors": len(sectors),
            "mapped_codes": sum(s["count"] for s in sectors),
            "total_rows": len(all_records),
            "data_quality": quality,
            "source_time": quality["source_time"],
            "server_time": datetime.now(_SH_TZ).isoformat(),
        })

    # ── API: 资金流总览 ────────────────────────
    @app.get("/api/fund-flow/summary")
    def fund_flow_summary():
        """全市场资金流总览：净流入/净流出家数、合计、极端值、涨停失真统计."""
        ff_path = _PROJECT / "research" / "fund_flow_live.json"
        if not ff_path.exists():
            return jsonify({"error": "资金流数据不可用"}), 503
        try:
            all_records, quality = _load_fund_flow_snapshot(ff_path)
        except (OSError, ValueError, json.JSONDecodeError):
            return jsonify({"error": "数据读取失败"}), 503

        summary = _fund_flow_market_summary(all_records)
        limit_up = 0
        limit_up_net_out = 0
        for r in all_records:
            pct = r["pct_chg"]
            limit_pct = 19.5 if r["code"].startswith(("300", "688")) else 9.5
            if pct is not None and pct >= limit_pct:
                limit_up += 1
                if r["net_yi"] < 0:
                    limit_up_net_out += 1

        names = _batch_names(list(dict.fromkeys(
            [summary["top_in"]["code"] if summary["top_in"] else None,
             summary["top_out"]["code"] if summary["top_out"] else None]
        )))
        for key in ("top_in", "top_out"):
            if summary[key]:
                summary[key] = summary[key].copy()
                summary[key]["name"] = names.get(summary[key]["code"], "名称不可用")
                summary[key]["name_status"] = (
                    "available" if summary[key]["name"] != "名称不可用"
                    else "unavailable"
                )

        return jsonify({
            "market_summary": summary,
            "limit_up_count": limit_up,
            "limit_up_net_out_count": limit_up_net_out,
            "limit_up_note": (
                f"{limit_up} 只涨停股中 {limit_up_net_out} 只显示主力净流出"
                "（同花顺口径失真：涨停封板量大单对冲，净额不代表真实离场）。"
            ),
            "data_quality": quality,
            "source_time": quality["source_time"],
            "server_time": datetime.now(_SH_TZ).isoformat(),
            "magnitude": {
                "scale": FLOW_MAGNITUDE_SCALE,
                "unit": "亿元（统一折算）",
                "note": "展示与筛选均使用统一折算值；raw_* 字段仅用于溯源。",
            },
        })

    # ── API: 多因子扫描器 ──────────────────────
    @app.get("/api/scanner/scan")
    def scanner_scan():
        """多因子股票筛选."""
        flow_dir = request.args.get("flow", "")  # "in", "out", ""
        kronos_dir = request.args.get("kronos", "")  # "up", "down", ""
        include_research_raw = request.args.get("include_research", "0").lower()
        unknown = sorted(set(request.args) - {
            "max_rank", "max_price", "flow", "kronos", "min_turnover", "limit",
            "include_research",
        })
        try:
            max_rank = _bounded_int(
                request.args.get("max_rank", "100"), "max_rank",
                minimum=1, maximum=1000,
            )
            max_price = _bounded_float(
                request.args.get("max_price", "50"), "max_price",
                minimum=0.01, maximum=10_000,
            )
            min_turnover = _bounded_float(
                request.args.get("min_turnover", "0"), "min_turnover",
                minimum=0, maximum=100,
            )
            limit = _bounded_int(
                request.args.get("limit", "50"), "limit", minimum=1, maximum=200,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if unknown:
            return jsonify({"error": f"未知查询参数：{', '.join(unknown)}"}), 400
        if flow_dir not in {"", "in", "out"}:
            return jsonify({"error": "flow 仅支持 in/out 或留空"}), 400
        if kronos_dir not in {"", "up", "down"}:
            return jsonify({"error": "kronos 仅支持 up/down 或留空"}), 400
        if include_research_raw not in {"0", "1", "false", "true"}:
            return jsonify({"error": "include_research 仅支持 0/1/false/true"}), 400
        include_research = include_research_raw in {"1", "true"}
        if kronos_dir and not include_research:
            return jsonify({
                "error": "Kronos 尚未通过验证；使用该筛选必须显式 include_research=1",
            }), 400

        # 加载数据
        quant = {}
        source_errors: dict[str, str] = {}
        qp = _PROJECT / "research" / "quant_stock_snapshot.json"
        if qp.exists():
            try:
                quant = json.loads(qp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError) as exc:
                source_errors["quant"] = type(exc).__name__

        ff_data = {}
        ffp = _PROJECT / "research" / "fund_flow_live.json"
        if ffp.exists():
            try:
                ff_data = json.loads(ffp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError) as exc:
                source_errors["fund_flow"] = type(exc).__name__

        k_data = {}
        kp = _PROJECT / "research" / "kronos_stock_snapshot.json"
        if kp.exists():
            try:
                k_data = json.loads(kp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError) as exc:
                source_errors["kronos"] = type(exc).__name__

        validation = _scanner_model_validation(quant)
        research_rows = quant.get("stocks", []) if isinstance(quant.get("stocks"), list) else []
        if validation["research_only"] and not include_research:
            return jsonify({
                "results": [],
                "count": 0,
                "research_records_available": len(research_rows),
                "excluded_research_only_count": len(research_rows),
                "validation": validation,
                "research_only": True,
                "decision_eligible": False,
                "source_errors": source_errors,
                "filters": {
                    "max_rank": max_rank, "max_price": max_price,
                    "flow": flow_dir, "kronos": kronos_dir,
                    "include_research": False,
                },
            })

        ff_stocks = ff_data.get("stocks", {}) if isinstance(ff_data.get("stocks"), dict) else {}
        k_stocks = k_data.get("stocks", {}) if isinstance(k_data.get("stocks"), dict) else {}

        # 2026-08-13 延迟整改：一次性批量取价（分块防单请求过大），替代原来逐只 get_quotes
        # 的 ≤100 次串行网络往返（实测首扫 45.5s）。provider 自带 30s TTL，取价零额外负担。
        rank_codes = [
            str(s.get("code") or "") for s in research_rows
            if isinstance(s, dict)
            and CODE_PATTERN.fullmatch(str(s.get("code") or ""))
            and _finite_number(s.get("rank")) is not None
            and float(s.get("rank")) <= max_rank
        ]
        quotes_map: dict = {}
        quote_error = None
        if rank_codes:
            try:
                _qprovider = app.config["QUOTE_PROVIDER"]
                for _i in range(0, len(rank_codes), 300):
                    quotes_map.update(_qprovider.get_quotes(rank_codes[_i:_i + 300]) or {})
            except Exception as exc:
                quote_error = type(exc).__name__
                quotes_map = {}

        results = []
        for s in research_rows:
            if not isinstance(s, dict):
                continue
            code = str(s.get("code") or "")
            if not CODE_PATTERN.fullmatch(code):
                continue
            rank = s.get("rank", 999)
            if _finite_number(rank) is None or float(rank) > max_rank:
                continue

            ff = ff_stocks.get(code, {})
            raw_net_yi = ff.get("net_yi") if isinstance(ff, dict) else None
            net_yi = normalize_fund_flow_yi(raw_net_yi)
            turnover = (
                _finite_number(ff.get("turnover")) if isinstance(ff, dict) else None
            )

            # 价格过滤（用上面一次性批量取到的价格）
            q = quotes_map.get(code)
            if not q or not getattr(q, "price", None):
                continue
            price = float(q.price)
            if not math.isfinite(price) or price <= 0 or price > max_price:
                continue

            # 资金流方向
            if flow_dir == "in" and (net_yi is None or net_yi <= 0.1):
                continue
            if flow_dir == "out" and (net_yi is None or net_yi >= -0.1):
                continue

            # Kronos 方向
            k_info = k_stocks.get(code, {})
            k_ret = None
            if isinstance(k_info, dict) and k_info.get("pred_ret_20d") is not None:
                try:
                    k_ret = round(float(k_info["pred_ret_20d"]) * 100, 1)
                except (TypeError, ValueError):
                    pass
            if kronos_dir == "up" and (k_ret is None or k_ret <= 0):
                continue
            if kronos_dir == "down" and (k_ret is None or k_ret >= 0):
                continue

            # 换手率
            if min_turnover > 0 and (turnover is None or turnover < min_turnover):
                continue

            raw_name = str(s.get("name") or "").strip()
            name_available = bool(raw_name and raw_name != code)

            results.append({
                "code": code,
                "name": raw_name if name_available else "名称不可用",
                "name_status": "available" if name_available else "unavailable",
                "rank": int(float(rank)),
                "composite": (
                    round(float(s["composite"]), 2)
                    if _finite_number(s.get("composite")) is not None else None
                ),
                "price": price,
                "net_yi": round(net_yi, 2) if net_yi is not None else None,
                "raw_net_yi": (
                    round(float(raw_net_yi), 2)
                    if _finite_number(raw_net_yi) is not None else None
                ),
                "flow_magnitude_scale": FLOW_MAGNITUDE_SCALE,
                "turnover": round(turnover, 1) if turnover else None,
                "kronos_20d": k_ret,
                "validation_status": validation["status"],
                "research_only": validation["research_only"],
                "decision_eligible": not validation["research_only"],
                "source_data_as_of": validation["source_data_as_of"],
                "kronos_validation_status": "unverified" if k_ret is not None else "not_used",
            })

        results.sort(key=lambda x: x["rank"])
        results = results[:limit]

        return jsonify({
            "results": results, "count": len(results),
            "validation": validation,
            "research_only": validation["research_only"],
            "decision_eligible": not validation["research_only"],
            "source_errors": source_errors,
            "quote_error": quote_error,
            "filters": {"max_rank": max_rank, "max_price": max_price,
                        "flow": flow_dir, "kronos": kronos_dir,
                        "include_research": include_research},
        })

    # ── API: 回测汇总 ──────────────────────────
    @app.get("/api/strategy/registry")
    def strategy_registry_api():
        gate = _decision_gate_status_fast()
        registry = gate.get("strategy_registry") or {}
        return jsonify({
            **registry,
            "decision_gate": _compact_decision_gate(gate),
        })

    @app.get("/api/backtest/summary")
    def backtest_summary():
        """Expose the frozen historical candidate and keep live permission separate."""
        path = _PROJECT / "research" / "reports" / "strict_pit_baseline.json"
        try:
            strict = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return jsonify({"error": "严格PIT审计数据不可用"}), 503
        summary = [{
            "method": row.get("name") or row.get("method"),
            "topk": row.get("topk"),
            "annualized_pct": row.get("annualized_pct"),
            "sharpe": row.get("sharpe"),
            "calmar": row.get("calmar"),
            "max_dd_pct": row.get("max_dd_pct"),
            "win_rate": None,
            "alpha_pct": None,
            "portfolio_ic": row.get("portfolio_ic"),
            "total_return_pct": None,
            "n_trades": None,
            "years_positive": row.get("years_positive"),
            "years_total": row.get("years_total"),
            "dsr": row.get("dsr"),
            "is_champion": False,
            "recommendation_eligible": False,
        } for row in strict.get("results", [])]
        gate = _decision_gate_status()
        registry = gate["strategy_registry"]
        headline = _historical_candidate_headline(gate)
        historical_passed = bool(
            headline
            and headline.get("historical_gate_passed")
            and not headline.get("invalidated")
        )
        v205 = (
            _read_json(_PROJECT / "reports" / "v209_global_defensive_rotation.json")
            or _read_json(_PROJECT / "reports" / "v205_multi_asset_defensive_rotation.json")
        )
        v219 = _read_json(
            _PROJECT / "reports" / "v219_qdii_split_nav_alignment_correction.json"
        )
        v221, v223 = _stability_80_20_reports()
        v221_active = bool(
            historical_passed
            and _stability_80_20_passed(v221, v223)
            and headline
            and headline.get("metric_scope") == "v221_stability_80_20_paper_candidate"
        )
        v205_active = bool(
            historical_passed
            and headline
            and headline.get("metric_scope") in {
                "v205_multi_asset_paper_candidate",
                "v209_global_defensive_paper_candidate",
            }
        )
        if v221_active:
            methods = [{
                "method": headline.get("method"),
                "validation_total_return_pct": headline.get("validation_total_return_pct"),
                "test_total_return_pct": headline.get("test_total_return_pct"),
                "diagnostic_total_return_pct": headline.get("diagnostic_total_return_pct"),
                "metric_scope": headline.get("metric_scope"),
                "paper_only": True,
                "recommendation_eligible": False,
            }]
            cost_scenarios = []
            for cost, metrics in (v221.get("cost_ladder_metrics") or {}).items():
                validation = (metrics or {}).get("validation_2022_2023") or {}
                test = (metrics or {}).get("test_2024_2025") or {}
                cost_scenarios.append({
                    "round_trip_cost_bps": float(cost),
                    "validation_total_return_pct": validation.get("total_return_pct"),
                    "test_total_return_pct": test.get("total_return_pct"),
                    "validation_max_drawdown_pct": validation.get("max_drawdown_pct"),
                    "test_max_drawdown_pct": test.get("max_drawdown_pct"),
                })
            cost_sensitivity = {
                "status": "completed",
                "scenarios": sorted(
                    cost_scenarios, key=lambda row: row["round_trip_cost_bps"]
                ),
                "verdict": (
                    "V221固定80/20结构已完成40/80/120BP重跑，并由V223另一套实现"
                    "逐月复现；只开放纸面观察。"
                ),
            }
        elif v219:
            methods = []
            cost_scenarios = []
            for cost, metrics in (v219.get("cost_ladder_metrics") or {}).items():
                validation = (metrics or {}).get("validation_2022_2023") or {}
                test = (metrics or {}).get("test_2024_2025") or {}
                cost_scenarios.append({
                    "round_trip_cost_bps": float(cost),
                    "validation_total_return_pct": validation.get("total_return_pct"),
                    "test_total_return_pct": test.get("total_return_pct"),
                    "validation_max_drawdown_pct": validation.get("max_drawdown_pct"),
                    "test_max_drawdown_pct": test.get("max_drawdown_pct"),
                })
            cost_sensitivity = {
                "status": "completed",
                "scenarios": sorted(
                    cost_scenarios, key=lambda row: row["round_trip_cost_bps"]
                ),
                "verdict": "V219纠错口径已完成40/80/120BP逐场景重跑；旧候选撤销。",
            }
        elif v205_active:
            methods = [{
                "method": headline.get("method"),
                "annualized_pct": headline.get("annualized_pct"),
                "sharpe": headline.get("sharpe"),
                "max_dd_pct": headline.get("max_dd_pct"),
                "win_rate": headline.get("win_rate"),
                "validation_total_return_pct": headline.get("validation_total_return_pct"),
                "test_total_return_pct": headline.get("test_total_return_pct"),
                "diagnostic_total_return_pct": headline.get("diagnostic_total_return_pct"),
                "metric_scope": headline.get("metric_scope"),
                "pre_freeze_catchup_net_return_pct": headline.get(
                    "pre_freeze_catchup_net_return_pct"
                ),
                "formal_forward_observations": headline.get(
                    "formal_forward_observations"
                ),
                "formal_forward_return_pct": headline.get(
                    "formal_forward_return_pct"
                ),
                "formal_forward_max_drawdown_pct": headline.get(
                    "formal_forward_max_drawdown_pct"
                ),
                "paper_only": True,
                "recommendation_eligible": False,
            }]
            cost_scenarios = []
            for cost, metrics in (v205.get("cost_ladder_metrics") or {}).items():
                validation = (metrics or {}).get("validation_2022_2023") or {}
                test = (metrics or {}).get("test_2024_2025") or {}
                cost_scenarios.append({
                    "round_trip_cost_bps": float(cost),
                    "validation_total_return_pct": validation.get("total_return_pct"),
                    "test_total_return_pct": test.get("total_return_pct"),
                    "validation_max_drawdown_pct": validation.get("max_drawdown_pct"),
                    "test_max_drawdown_pct": test.get("max_drawdown_pct"),
                })
            cost_sensitivity = {
                "status": "completed",
                "scenarios": sorted(
                    cost_scenarios, key=lambda row: row["round_trip_cost_bps"]
                ),
                "verdict": "40/80/120BP逐场景重跑均完成；仅允许纸面前向观察。",
            }
        else:
            methods = summary if historical_passed else []
            cost_sensitivity = {
                "status": "unavailable",
                "scenarios": [],
                "verdict": "未执行逐成本重跑，不提供公式外推。",
            }
        return jsonify({
            "invalidated": not historical_passed,
            "historical_gate_passed": historical_passed,
            "recommendation_eligible": False,
            "audit_message": (
                registry.get("recommendation_lock_reason")
                or "旧历史回测因前视泄漏已永久否决；当前无可展示候选。"
            ),
            # Revoked research rows must not remain visible as selectable methods.
            "methods": methods,
            "best_method": None,
            "best_topk": None,
            "n_trials": (
                None if (v221_active or v205_active)
                else strict.get("n_trials_dsr") if historical_passed else None
            ),
            "n_dates": None,
            "costs": (
                v221.get("rules") if v221_active
                else v219.get("rules") if v219
                else v205.get("rules") if v205_active
                else strict.get("costs") if historical_passed else None
            ),
            "generated_at": (
                v223.get("generated_at") if v221_active
                else v219.get("generated_at") if v219
                else v205.get("generated_at") if v205_active
                else strict.get("generated_at") if historical_passed
                else registry.get("generated_at")
            ),
            "headline": headline,
            "period_returns": {},
            "cost_sensitivity": cost_sensitivity,
            "decision_gate": gate,
            "strategy_registry": registry,
            "research_evidence": _profitability_research_evidence(),
        })

    # ── API: 因子表现 ──────────────────────────
    @app.get("/api/backtest/factors")
    def backtest_factors():
        """公开因子历史确认记录；旧 epoch 证据只可追溯，不可获得正面评级。

        2024-07~2025-12 已参与旧系统观察，统一降级为历史确认。production 仅表示
        当前严格 epoch 在该因子自身 seal 后新增数据上完成固定前向终测。
        """
        sort = request.args.get("sort", "abs_ic")
        try:
            limit = max(1, min(int(request.args.get("limit", 30)), 200))
        except (TypeError, ValueError):
            limit = 30
        try:
            return jsonify(_backtest_factor_payload(sort=sort, limit=limit))
        except Exception as exc:
            return _factor_registry_error_response(app, exc, "因子历史记录读取失败")

    @app.get("/api/backtest/history")
    def backtest_history():
        """P3-4 历史实验留痕区：冠军前视伪影 / R1-R5 / 250次回测 / 300次四路径，只读。"""
        path = _PROJECT / "research" / "historical_experiment_ledger.json"
        if not path.is_file():
            return jsonify({"ok": False, "error": "实验留痕文件不存在"}), 503
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        for entry in data.get("entries", []):
            ep = entry.get("evidence_path")
            if ep:
                full = _PROJECT / ep
                entry["evidence_exists"] = full.is_file()
        return jsonify({"ok": True, **data})

    # ── API: K线 + 技术指标叠加 ────────────────
    @app.get("/api/candles/<code>/with-indicators")
    def candles_with_indicators(code: str):
        """返回K线数据 + EMA + 布林带叠加."""
        normalized = code.strip().split(".")[0]
        if not CODE_PATTERN.fullmatch(normalized):
            return jsonify({"error": "仅支持沪深6位股票代码"}), 400

        try:
            candles = app.config["CANDLE_PROVIDER"].get_candles(
                normalized, 200, interval="1d",
            )
        except CandleDataUnavailable as exc:
            return internal_error_response(
                app, "日线数据源暂不可用", exc, status=503,
                extra={"code": normalized},
            )

        # 计算 EMA5, EMA20, 布林带(20,2)
        closes = [c.close for c in candles]
        ema5 = _calc_ema(closes, 5)
        ema20 = _calc_ema(closes, 20)
        boll = _calc_bollinger(closes, 20, 2)

        serialized = []
        for i, c in enumerate(candles):
            t = c.time
            if hasattr(t, 'isoformat'):
                t = t.isoformat()
            else:
                t = str(t)
            serialized.append({
                "time": t,
                "open": c.open, "high": c.high,
                "low": c.low, "close": c.close,
                "volume": c.volume,
                "ema5": ema5[i] if i < len(ema5) else None,
                "ema20": ema20[i] if i < len(ema20) else None,
                "boll_mid": boll["mid"][i] if i < len(boll["mid"]) else None,
                "boll_upper": boll["upper"][i] if i < len(boll["upper"]) else None,
                "boll_lower": boll["lower"][i] if i < len(boll["lower"]) else None,
            })

        # 技术快照
        try:
            snap = technical_snapshot(candles)
        except Exception:
            snap = {"status": "insufficient_history"}

        return jsonify({
            "code": normalized,
            "candles": serialized,
            "technical": snap,
            "meta": {
                "data_time": serialized[-1]["time"] if serialized else None,
                "market": _audited_market_state(),
            },
        })

    # ── API: Kronos 预测 Top ──────────────────
    def _kronos_prefill_worker(rows: list[dict], decision_gate: dict) -> None:
        """后台逐只算 Kronos Top 的统一评估，绝不阻塞 /api/kronos/top 请求线程。

        行情由本线程独立按批取（_bounded_call 8s），不依赖请求线程传入的 quotes——
        那可能已被 CachedQuoteProvider 锁拖住超时为空，导致评估全部 fail-closed。
        """
        model_by_code = {
            str(row.get("code")): dict(row)
            for row in rows if isinstance(row, dict) and row.get("code")
        }
        codes = list(model_by_code)
        with _KRONOS_PREFILL_LOCK:
            todo = [c for c in codes if c not in _KRONOS_PREFILL_INFLIGHT]
            _KRONOS_PREFILL_INFLIGHT.update(todo)
        if not todo:
            return
        try:
            cash_snapshot = get_account_cash()
            available_cash = _finite_number(
                (cash_snapshot or {}).get("cash_balance")
            )
            names = {
                code: str(model_by_code[code].get("name") or code)
                for code in todo
            }
            _qok, _qdata, _qerr = _bounded_call(
                lambda: app.config["QUOTE_PROVIDER"].get_quotes(todo), 8.0)
            quotes = _qdata if _qok and isinstance(_qdata, dict) else {}
            for code in todo:
                try:
                    ok, assessment, err = _bounded_call(
                        lambda c=code: _assess_candidate(
                            c, names.get(c, c), quotes.get(c), model_by_code.get(c),
                            q=_quote_to_dict(quotes.get(c), c),
                            decision_gate=decision_gate,
                            available_cash=available_cash),
                        25,
                    )
                    if not ok:
                        assessment = {
                            "code": code, "name": names.get(code, code),
                            "verdict": "REJECT", "can_alert": False, "gates": {},
                            "passed_gates": [], "failed_gates": ["evaluation"],
                            "reason": "统一评估超时（fail-closed，可显式重试）",
                            "as_of": datetime.now(_SH_TZ).isoformat(timespec="seconds"),
                            "source": "统一评估流水线·后台预热",
                        }
                except Exception:
                    assessment = {
                        "code": code, "name": names.get(code, code),
                        "verdict": "REJECT", "can_alert": False, "gates": {},
                        "passed_gates": [], "failed_gates": ["evaluation"],
                        "reason": "统一评估异常(fail-closed，下轮重试)",
                        "as_of": datetime.now(_SH_TZ).isoformat(timespec="seconds"),
                        "source": "统一评估流水线·后台预热",
                    }
                with _KRONOS_ASSESS_LOCK:
                    _KRONOS_ASSESS[code] = assessment
                    _KRONOS_ASSESS_TS[code] = _time.time()
        finally:
            with _KRONOS_PREFILL_LOCK:
                _KRONOS_PREFILL_INFLIGHT.difference_update(todo)

    def _kronos_prefill_job(rows: list[dict], decision_gate: dict) -> None:
        error_type = None
        try:
            _kronos_prefill_worker(rows, decision_gate)
        except Exception as exc:
            error_type = type(exc).__name__
            app.logger.exception("kronos prefill failed")
        finally:
            with _KRONOS_PREFILL_STATE["lock"]:
                _KRONOS_PREFILL_STATE.update({
                    "running": False, "last_error": error_type,
                })

    app.extensions["olympus_kronos_prefill_worker"] = _kronos_prefill_worker

    @app.get("/api/kronos/top")
    def kronos_top():
        """Kronos 模型研究 Top-N：保留原始输出，并提供同批横截面评分。"""
        kp = _PROJECT / "research" / "kronos_stock_snapshot.json"
        if not kp.exists():
            return jsonify({"error": "Kronos 数据不可用"}), 503
        try:
            k_data = json.loads(kp.read_text(encoding="utf-8"))
        except Exception:
            return jsonify({"error": "数据读取失败"}), 503

        top = load_kronos_top(limit=12)
        for item in top:
            score = item.get("model_score")
            if score is None:
                item["strength"] = "neutral"
                item["strength_label"] = "排序待生成"
            elif score >= 90:
                item["strength"] = "strong_bull"
                item["strength_label"] = "模型前10%"
            elif score >= 70:
                item["strength"] = "moderate_bull"
                item["strength_label"] = "模型前30%"
            else:
                item["strength"] = "neutral"
                item["strength_label"] = f"横截面 {score:.1f}/100"

        # Keep the dashboard path local-only.
        local_names = _audited_stock_names()
        for i in top:
            i["name"] = local_names.get(i["code"], "名称不可用")
            i["name_status"] = "available" if i["name"] != "名称不可用" else "unavailable"

        # 生成时间只表示制品生成；数据新鲜度必须来自模型输入交易日。
        gen = k_data.get("generated_at")
        artifact_age_h = None
        if gen:
            try:
                ts = datetime.fromisoformat(str(gen))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_SH_TZ)
                artifact_age_h = round((datetime.now(_SH_TZ) - ts).total_seconds() / 3600, 1)
            except (TypeError, ValueError):
                artifact_age_h = None
        model_status = kronos_status()
        data_age_days = model_status.get("data_staleness_days")
        legacy_age_h = (
            round(float(data_age_days) * 24, 1)
            if _finite_number(data_age_days) is not None else None
        )

        decision_gate = _decision_gate_status_fast(start_refresh=False)
        codes = [i["code"] for i in top]
        ls_assess: dict = {}
        try:
            with _OPP_STATE["lock"]:
                _ls = copy.deepcopy(_OPP_STATE.get("result") or {})
            for _r in (
                list(_ls.get("items") or [])
                + list(_ls.get("watch_items") or [])
                + list(_ls.get("rejected") or [])
            ):
                if _r.get("assessment"):
                    ls_assess[_r.get("code")] = _r["assessment"]
        except Exception:
            ls_assess = {}
        _assess_errors = 0
        _pending = 0
        _now = _time.time()

        def _needs_kronos_prefill(assessment: object) -> bool:
            if not isinstance(assessment, dict):
                return True
            # PENDING is also a legitimate completed verdict when company or
            # risk evidence is awaiting verification.  Only an empty-gates
            # PENDING object is the route's unfinished placeholder.
            return (
                assessment.get("verdict") == "PENDING"
                and not assessment.get("gates")
            )

        for i in top:
            code = i["code"]
            assessment = ls_assess.get(code)
            if _needs_kronos_prefill(assessment):
                with _KRONOS_ASSESS_LOCK:
                    if _KRONOS_ASSESS_TS.get(code, 0) > _now - _KRONOS_ASSESS_TTL:
                        cached_assessment = _KRONOS_ASSESS.get(code)
                        # An empty live-scan placeholder must not hide a
                        # completed prefill result, including a fully-evaluated
                        # PENDING verdict with populated gate evidence.
                        if not _needs_kronos_prefill(cached_assessment):
                            assessment = cached_assessment
            if _needs_kronos_prefill(assessment):
                assessment = {
                    "code": code, "name": i["name"],
                    "verdict": "PENDING", "can_alert": False,
                    "gates": {}, "passed_gates": [], "failed_gates": [],
                    "reason": "统一评估后台计算中（下一轮自动填充）",
                    "as_of": datetime.now(_SH_TZ).isoformat(timespec="seconds"),
                    "source": "统一评估流水线·后台预热",
                }
            if _needs_kronos_prefill(assessment):
                _pending += 1
            i["assessment"] = assessment
        # 统一排序：可预警 > 评分闸门全过(APPROVE) > 综合分 > 模型横截面分。
        # 旧 85/15 研究优先排序键退役——推荐唯一依据 = 统一评估。
        top.sort(key=lambda x: (
            1 if (x.get("assessment") or {}).get("can_alert") else 0,
            1 if (x.get("assessment") or {}).get("verdict") == "APPROVE" else 0,
            ((x.get("assessment") or {}).get("gates") or {}).get("composite", {}).get("composite") or -1,
            x.get("model_score") or 0,
        ), reverse=True)

        return jsonify({
            "items": top, "count": len(top),
            # ``updated``/``age_h`` are retained for older clients but now
            # describe source-data age, never artifact regeneration time.
            "updated": model_status.get("source_data_as_of") or "",
            "age_h": legacy_age_h,
            "source_data_as_of": model_status.get("source_data_as_of"),
            "data_staleness_days": data_age_days,
            "freshness_status": model_status.get("freshness_status") or "unknown",
            "validation_status": model_status.get("validation_status") or "unknown",
            "horizon_semantics": model_status.get("horizon_semantics"),
            "forecast_calendar": model_status.get("forecast_calendar"),
            "artifact_generated_at": gen,
            "artifact_age_h": artifact_age_h,
            "decision_eligible": False,
            "research_only": True,
            "refresh_endpoint": "/api/kronos/refresh",
            "assessment_as_of": (
                datetime.now(_SH_TZ).isoformat(timespec="seconds")),
            "model_note": ("模型原始输出不做截断；当前未验证或陈旧模型只作研究证据，不参与买入通过/否决。"
                           "统一评估综合评分 = professional_composite_v4_profit_sign_aware；买入预警 = "
                           "排除/五闸门/评级≥买入65/时机≥70/最佳窗口/价格可整手等硬闸门全过 + 决策门禁放行。"
                           f"{'部分行评估异常(fail-closed, %d 行)' % _assess_errors if _assess_errors else ''}"
                           f"{'%d 行评估等待显式刷新' % _pending if _pending else ''}"
                           "模型输出不等同已实现收益。"),
        })

    @app.post("/api/kronos/refresh")
    def kronos_refresh_api():
        try:
            if request.content_length:
                _strict_json_payload(allowed=set())
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        rows = load_kronos_top(limit=12)
        names = _audited_stock_names()
        for row in rows:
            row["name"] = names.get(str(row.get("code")), "名称不可用")
        started = _try_start_snapshot_worker(
            _KRONOS_PREFILL_STATE, _kronos_prefill_job,
            name="kronos-assess-prefill", args=(rows, _decision_gate_status_fast()),
        )
        return jsonify({
            "ok": True, "started": started, "running": True,
            "idempotent_replay": not started,
            "status_endpoint": "/api/kronos/top",
        }), 202

    def _radar_worker() -> None:
        """重建市场雷达快照（东财网络抓取 + 持仓行情 + 公告，~4s），写进 _RADAR_STATE."""
        try:
            from trade_assistant.mom_radar import build_major_event_notification, build_mom_radar_snapshot
            radar = build_mom_radar_snapshot(quote_provider=app.config["QUOTE_PROVIDER"])
            portfolio = _manual_portfolio_status(app.config["QUOTE_PROVIDER"])
            positions = portfolio.get("positions", [])
            raw_quotes = app.config["QUOTE_PROVIDER"].get_quotes([p["code"] for p in positions]) if positions else {}
            quotes = {code: {
                "change_pct": getattr(quote, "change_pct", None),
                "source": getattr(quote, "source", None) or "站内行情源",
            } for code, quote in (raw_quotes or {}).items()}
            from .company_profile import get_cached_source_data
            announcements = {
                p["code"]: (get_cached_source_data(p["code"], "announcements") or [])
                for p in positions
            }
            major = build_major_event_notification(
                positions, quotes, radar.get("events", []), announcements,
                now=datetime.now(_SH_TZ),
            )
            payload = {
                "events": radar.get("events", [])[:8],
                "indices": radar.get("indices", [])[:6],
                "sectors": radar.get("sectors", [])[:10],
                "generated_at": radar.get("generated_at"),
                "market": radar.get("market", {}),
                "major_event": major,
                "notification_count": major["notification_count"],
            }
            with _RADAR_STATE["lock"]:
                _RADAR_STATE["data"] = payload
                _RADAR_STATE["ts"] = _time.time()
                _RADAR_STATE["last_error"] = None
        except Exception:
            with _RADAR_STATE["lock"]:
                _RADAR_STATE["last_error"] = _traceback.format_exc()
        finally:
            with _RADAR_STATE["lock"]:
                _RADAR_STATE["running"] = False

    def _radar_refresher_loop(stop_event: threading.Event) -> None:
        """常驻后台：启动立即重建一次（首屏即有快照），此后每 _RADAR_REFRESH_S 刷新."""
        _try_start_snapshot_worker(
            _RADAR_STATE, _radar_worker, name="radar-worker",
        )
        while not stop_event.wait(_RADAR_REFRESH_S):
            with _RADAR_STATE["lock"]:
                stale = (_RADAR_STATE["data"] is None) or (
                    _time.time() - _RADAR_STATE["ts"]) >= _RADAR_REFRESH_S
                already_running = _RADAR_STATE["running"]
            if stale and not already_running:
                _try_start_snapshot_worker(
                    _RADAR_STATE, _radar_worker, name="radar-worker",
                )

    # ── API: 市场雷达事件 ──────────────────────
    @app.get("/api/radar")
    def market_radar():
        now = _time.time()
        with _RADAR_STATE["lock"]:
            data = _RADAR_STATE["data"]
            running = _RADAR_STATE["running"]
            last_error = _RADAR_STATE.get("last_error")
            stale = bool(data) and (now - _RADAR_STATE["ts"]) >= _RADAR_REFRESH_S
        if data is None:
            if last_error and not running:
                return jsonify({"error": "雷达快照生成失败，暂无可用快照", "events": [],
                                "generating": False,
                                "refresh_endpoint": "/api/radar/refresh",
                                "last_error": "snapshot_generation_failed"}), 503
            return jsonify({"status": "generating", "events": [],
                            "generating": bool(running), "refresh_endpoint": "/api/radar/refresh",
                            "last_error": "snapshot_generation_failed" if last_error else None}), 202
        if stale and last_error:
            return jsonify({"error": "雷达快照刷新失败；现有快照已过期",
                            "events": [], "generating": bool(running), "stale": True,
                            "last_error": "snapshot_generation_failed",
                            "refresh_endpoint": "/api/radar/refresh",
                            "stale_snapshot_available": True}), 503
        return jsonify({**data, "cached": True, "stale": stale,
                        "refreshing": bool(running),
                        "refresh_endpoint": "/api/radar/refresh",
                        "last_error": "snapshot_generation_failed" if last_error else None})

    @app.post("/api/radar/refresh")
    def radar_refresh_api():
        return _snapshot_refresh_response(_RADAR_STATE, _radar_worker,
            name="radar-manual", status_endpoint="/api/radar")

    # ── API: 风控报告 ──────────────────────────
    @app.get("/api/risk/report")
    def risk_report_api():
        """完整风控报告：VaR + Beta + 行业暴露 + 回撤."""
        view = request.args.get("view", "compact")
        if view not in {"compact", "evidence"}:
            return jsonify({"error": "view 仅支持 compact/evidence"}), 400
        decision_gate_full = (
            _decision_gate_status() if view == "evidence"
            else _decision_gate_status_fast(start_refresh=False)
        )
        decision_gate = (
            decision_gate_full if view == "evidence"
            else _compact_decision_gate(decision_gate_full)
        )
        system_evidence = (
            _system_evidence(decision_gate) if view == "evidence" else None
        )
        # Account/risk polling must not rebuild the research archive before
        # its bounded account calculation even starts. Load it only on demand.
        strategy_lab = _strategy_lab_evidence() if view == "evidence" else None
        report_ok, result, report_error = _bounded_call(
            lambda: _build_risk_report(app.config["QUOTE_PROVIDER"]), 8.0,
        )
        if not report_ok:
            return internal_error_response(
                app, "风控计算暂不可用", report_error, status=503, extra={
                "risk_level": "不可用",
                "recommended_action": "wait",
                "research_only": True,
                "decision_gate": decision_gate,
                "system_evidence": system_evidence,
                "strategy_lab": strategy_lab,
                "evidence_view": "/api/risk/report?view=evidence",
                "data_health": "unavailable",
            })
        report, portfolio = result
        report["portfolio_source"] = portfolio["source"]
        report["decision_gate"] = decision_gate
        report["system_evidence"] = system_evidence
        report["strategy_lab"] = strategy_lab
        report["evidence_view"] = "/api/risk/report?view=evidence"
        return jsonify(report)

    # ── API: 引擎侧风控与仓位（任务书 Step 2） ──────────
    @app.get("/api/engine-risk/profiles")
    def engine_risk_profiles_api():
        """三档风险偏好边界参数（先验分层，非回测调参）。"""
        _bt = _PROJECT / "research" / "backtest_project"
        if str(_bt) not in sys.path:
            sys.path.insert(0, str(_bt))
        try:
            from backtest_engine.risk_control import RISK_PROFILES
        except Exception as exc:
            return internal_error_response(
                app, "引擎风控模块暂不可用", exc, status=503,
            )
        labels = {k: v["label"] for k, v in RISK_PROFILES.items()}
        params = {k: {kk: vv for kk, vv in v.items() if kk not in ("label", "source")}
                  for k, v in RISK_PROFILES.items()}
        return jsonify({"ok": True, "labels": labels, "profiles": params})

    @app.get("/api/engine-risk/report")
    def engine_risk_report_api():
        """引擎侧风控：策略回测口径（Kelly/回撤/波动率缩放）+ 实盘持仓硬边界检查。"""
        profile = request.args.get("profile", "moderate")
        _bt = _PROJECT / "research" / "backtest_project"
        if str(_bt) not in sys.path:
            sys.path.insert(0, str(_bt))
        try:
            import pandas as _pd
            from backtest_engine.risk_control import get_risk_profile, RiskManager
        except Exception as exc:
            return internal_error_response(
                app, "引擎风控模块不可用", exc, status=503,
            )
        prof = get_risk_profile(profile)
        # 1) 策略回测口径（由 risk_check_strategies.py 生成，缺省为空并如实标注）
        bt_data = {}
        report_path = _PROJECT / "research" / "backtest_project" / "data" / "engine_risk_report.json"
        if report_path.exists():
            try:
                bt_data = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception:
                bt_data = {}
        decision_gate = _decision_gate_status_fast()
        if not decision_gate["recommendation_eligible"]:
            bt_data = {"strategies": [], "locked": True, "reason": decision_gate["reason"]}
        # 2) 实盘持仓硬边界（站点 positions 口径 100 股/手，与 risk_control.py 一致）
        portfolio = _manual_portfolio_status(app.config["QUOTE_PROVIDER"])
        positions = portfolio.get("positions", [])
        if portfolio.get("cash_user_recorded") is not True:
            return jsonify({
                "ok": False,
                "error": "尚未录入真实剩余资金；不使用20万元试算值冒充账户事实",
                "profile": prof,
                "engine": bt_data,
                "decision_gate": _compact_decision_gate(decision_gate),
                "live": {
                    "positions": len(positions),
                    "account_capital": None,
                    "market_value": portfolio.get("market_value"),
                    "equity_value": None,
                    "cash": None,
                    "valuation_status": "missing_account_cash",
                    "account_fact_gaps": ["真实剩余可用资金"],
                },
            })
        account_capital = float(portfolio["account_capital"])
        pos_dict, prices, ind_map = {}, {}, {}
        invested = 0.0
        missing_prices = []
        for p in positions:
            code = p.get("code")
            if not code:
                continue
            entry = p.get("entry_price") or 0.0
            cur = _finite_number(p.get("current_price"))
            shares = int(p.get("shares") or 0)
            pos_dict[code] = {"shares": shares, "cost_basis": entry * shares}
            if cur is None or cur <= 0:
                missing_prices.append(code)
            else:
                prices[code] = cur
            ind_map[code] = p.get("industry") or "未分类"
            invested += entry * shares
        cash = max(account_capital - invested, 0.0)
        if missing_prices:
            return jsonify({
                "ok": False,
                "error": "持仓行情不完整，未用成交价冒充现价；引擎侧实时风控不可计算",
                "profile": prof,
                "engine": bt_data,
                "decision_gate": _compact_decision_gate(decision_gate),
                "live": {
                    "positions": len(positions),
                    "account_capital": round(account_capital, 2),
                    "market_value": None,
                    "equity_value": None,
                    "cash": round(cash, 2),
                    "valuation_status": "insufficient",
                    "missing_quote_codes": missing_prices,
                },
            }), 503
        equity_value = cash + sum(pd_["shares"] * prices[c] for c, pd_ in pos_dict.items())
        mgr = RiskManager(profile_name=profile)
        # 单点净值 → 回撤恒为 0（站点无历史净值曲线，回撤口径在引擎侧回测部分展示）
        live = mgr.apply(_pd.Series([equity_value]), pos_dict, cash, prices, ind_map,
                         [p["code"] for p in positions])
        return jsonify({
            "ok": True,
            "profile": prof,
            "engine": bt_data,
            "decision_gate": _compact_decision_gate(decision_gate),
            "live": {
                "positions": len(positions),
                "account_capital": round(account_capital, 2),
                "market_value": round(float(portfolio["market_value"]), 2),
                "equity_value": round(equity_value, 2),
                "cash": round(cash, 2),
                "violations": live["violations"],
                "warnings": live["warnings"],
                "budgets": live["budgets"],
            },
            "honesty": (
                "策略回测证据已锁定，不计算Kelly或建议总暴露；持仓边界只读取手工持仓账本，仅审计不自动下单。"
                if not decision_gate["recommendation_eligible"] else
                "Kelly与波动缩放仅在严格产品门禁通过后展示；持仓边界读取手工持仓账本，仅审计不自动下单。"
            ),
        })

    # ── API: 资金-仓位建议 ──────────────────────
    @app.get("/api/sizing")
    def sizing_api():
        """根据本金生成仓位建议."""
        capital = request.args.get("capital", 10000, type=float)
        capital = max(1000, min(10_000_000, capital))  # 限制范围
        gate = _decision_gate_status_fast()
        if not gate["recommendation_eligible"]:
            return jsonify({
                "locked": True,
                "decision_gate": _compact_decision_gate(gate),
                "capital": capital,
                "note": gate["reason"],
            })
        return jsonify(sizing_summary(capital))

    # ── API: 所有资金级别的仓位方案对比 ─────────
    @app.get("/api/sizing/tiers")
    def sizing_tiers_api():
        """返回所有资金级别的仓位方案（供前端表格展示）."""
        gate = _decision_gate_status_fast()
        return jsonify({
            "tiers": get_all_plans() if gate["recommendation_eligible"] else [],
            "locked": not gate["recommendation_eligible"],
            "decision_gate": _compact_decision_gate(gate),
        })

    # ── 全市场找股候选池：状态查询 + 手动刷新 ─────────
    @app.get("/api/finder/status")
    def finder_status_api():
        """全市场候选池刷新状态（透明度，不编造）。"""
        snapshot = _read_full_market_snapshot()
        universe = audited_universe(_PROJECT)
        with _FM_REFRESH_STATE["lock"]:
            refresh = {
                key: _FM_REFRESH_STATE[key]
                for key in ("last_ts", "last_count", "running", "last_error")
            }
        return jsonify({
            "snapshot": _FM_SNAPSHOT.name,
            "generated_at": snapshot.get("generated_at"),
            "universe_count": universe.get("count"),
            "universe_audit": universe,
            "last_refresh_ts": refresh["last_ts"],
            "last_count": refresh["last_count"],
            "running": refresh["running"],
            "last_error": refresh["last_error"],
            "eligible_count": snapshot.get("eligible_count"),
            "eligible_complete": snapshot.get("eligible_complete") is True,
        })

    @app.post("/api/finder/refresh")
    def finder_refresh_api():
        """手动触发一次全市场候选池刷新（后台，立即返回）。"""
        return _snapshot_refresh_response(
            _FM_REFRESH_STATE, _refresh_full_market_pool,
            name="finder-refresh", status_endpoint="/api/finder/status",
        )

    background_threads = BackgroundThreadGroup()
    app.extensions["background_threads"] = background_threads
    app.extensions["background_monitoring_enabled"] = bool(start_background)
    try:
        if start_background:
            for target, name in (
                (route_snapshot.run, "research-route-refresher"),
                (playbook_scan.run, "playbook-candidate-refresher"),
                (_full_market_refresher_loop, "full-market-refresher"),
                (_opportunities_refresher_loop, "opportunities-refresher"),
                (_live_signal_refresher_loop, "live-signal-refresher"),
                (_radar_refresher_loop, "radar-refresher"),
            ):
                background_threads.start(target, name=name)
        if start_paper_forward:
            background_threads.start(
                _paper_forward_refresher_loop, name="paper-forward-refresher",
            )
    except Exception:
        background_threads.stop()
        raise

    return app

def _simple_regime(quotes: dict) -> dict:
    """简化版市场情绪（不依赖 _market_status）。"""
    indexes = [quotes.get(c) for c in MARKET_INDEX_CODES]
    available = [q for q in indexes if q is not None]
    changes = [q.change_pct for q in available if q.change_pct is not None]
    if len(changes) < 2:
        return {"status": "unavailable", "label": "数据不足"}
    avg = sum(changes) / len(changes)
    if all(c > 0 for c in changes) and avg >= 1:
        return {"status": "strong", "label": "当日偏强"}
    if all(c < 0 for c in changes) and avg <= -1:
        return {"status": "weak", "label": "当日偏弱"}
    if any(c > 0 for c in changes) and any(c < 0 for c in changes):
        return {"status": "divergent", "label": "指数分化"}
    return {"status": "range", "label": "当日震荡"}

def _calc_ema(values: list[float], period: int) -> list[float | None]:
    """计算 EMA，前 period-1 个值为 None."""
    if len(values) < period:
        return [None] * len(values)
    ema = [None] * (period - 1)
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    ema.append(seed)
    for v in values[period:]:
        ema.append(ema[-1] + k * (v - ema[-1]))
    return ema

def _calc_bollinger(values: list[float], period: int = 20, nbdev: int = 2
                    ) -> dict[str, list[float | None]]:
    """计算布林带."""
    n = len(values)
    mid = [None] * (period - 1)
    upper = [None] * (period - 1)
    lower = [None] * (period - 1)
    for i in range(period - 1, n):
        window = values[i - period + 1:i + 1]
        m = sum(window) / period
        variance = sum((x - m) ** 2 for x in window) / period
        std = variance ** 0.5
        mid.append(m)
        upper.append(m + nbdev * std)
        lower.append(m - nbdev * std)
    return {"mid": mid, "upper": upper, "lower": lower}

# 股票名称 TTL 缓存：名称日频稳定，/api/kronos/top 与 /api/fund-flow/ranking 每 60s 轮询，
# 旧逻辑每次都 new SinaBatchQuoteProvider().get_quotes() 打一次网络（实测污染 /api/kronos/top
# 至 25s 尖峰）。600s 缓存：名称不是行情，无需 60s 级新鲜，锁争用直接归零。
_NAMES_CACHE: dict[str, str] = {}
_NAMES_CACHE_TS: float = 0.0
_NAMES_CACHE_LOCK = threading.Lock()
_NAMES_CACHE_TTL = 600

# Kronos 统一评估后台预热：/api/kronos/top 绝不阻塞请求线程。
# 背景：M5 综合评估曾对 top 12 只逐只同步 _assess_candidate（每只 10~75s），
# 实测整路由 >90s 卡死 → 前端「AI 看涨池 · 综合评估」卡片空白。修复：live-scan
# 快照优先（TTL 跟随扫描耗时），缺档由后台线程按只预填；首轮返回 PENDING 占位
# （前端渲染「综合 —/档案待」），页面每 60s 轮询自动升级为完整评估。
_KRONOS_ASSESS: dict[str, dict] = {}
_KRONOS_ASSESS_TS: dict[str, float] = {}
_KRONOS_ASSESS_LOCK = threading.Lock()
_KRONOS_ASSESS_TTL = 600  # 评估结果 10 分钟有效，过期由下一轮预填刷新
_KRONOS_PREFILL_LOCK = threading.Lock()
_KRONOS_PREFILL_INFLIGHT: set[str] = set()
_KRONOS_PREFILL_STATE = {
    "lock": threading.Lock(), "running": False, "last_error": None,
}

def _batch_names(codes: list[str]) -> dict[str, str]:
    """批量获取股票名称（本机离线名表优先 + 新浪增量补全，带 TTL 缓存）。

    名称日频稳定，/api/kronos/top 与 /api/fund-flow/ranking 每 60s 轮询。旧逻辑每次
    new SinaBatchQuoteProvider().get_quotes() 打一次网络（实测污染 /api/kronos/top
    至 25s 尖峰；2026-08-18 起新浪源本机不可达 ReadTimeout，名称全部退化成股票代码
    → 全市场热力图满屏数字）。修复：先查 research/data/stock_basic_baostock.csv
    本机离线名表（0 网络、必命中中文名），仅表外代码（新股等）再走新浪增量补全；
    新浪失败不影响离线结果。锁争用仍由 600s 缓存吸收。
    """
    global _NAMES_CACHE_TS
    if not codes:
        return {}
    def _usable_name(code: str, value: object) -> bool:
        text = str(value or "").strip()
        return bool(text and text != code and text != "名称不可用")

    now = _time.time()
    local = _audited_stock_names()  # 本机离线名表（模块级缓存，无网络）
    with _NAMES_CACHE_LOCK:
        if _NAMES_CACHE and (now - _NAMES_CACHE_TS) < _NAMES_CACHE_TTL:
            missing = [c for c in codes if not _usable_name(c, _NAMES_CACHE.get(c))]
            if not missing:
                return {c: _NAMES_CACHE[c] for c in codes}
            # 部分命中：只补拉缺失代码，而不是把缺失项退化回股票代码——
            # 否则 fund-flow 与 kronos 不同批次交叉填充缓存时，未命中的代码
            # 会以数字形态一直显示到 TTL 过期（CDP 实测「一会名字一会代码」）。
            need = missing
        else:
            need = codes
    result: dict[str, str] = {}
    still_missing: list[str] = []
    for c in need:
        n = local.get(c)
        if n:
            result[c] = n
        else:
            still_missing.append(c)
    # 表外代码走新浪增量补全（新股/更名新鲜度）；失败保留离线结果不丢弃。
    # 2026-08-18 新浪源本机 ReadTimeout 需 ~10s：用 _bounded_call 限到 5s，
    # 避免每 10 分钟冷缓存轮询时 fund-flow/kronos 被表外代码拖慢一次。
    if still_missing:
        _sok = False
        _sdata: dict = {}
        try:
            from trade_assistant.market import SinaBatchQuoteProvider

            def _sina_fetch():
                return SinaBatchQuoteProvider().get_quotes(still_missing)

            _sok, _sdata, _serr = _bounded_call(_sina_fetch, 5.0)
        except Exception:
            _sok = False
        if _sok and isinstance(_sdata, dict) and _sdata:
            # 新浪名称用空格做 CJK 宽度对齐（如「五 粮 液」），A 股名称不含空格，统一去空格
            for c, q in _sdata.items():
                candidate = str(getattr(q, "name", "") or "").replace(" ", "").strip()
                if _usable_name(c, candidate):
                    result[c] = candidate
    if result:
        with _NAMES_CACHE_LOCK:
            _NAMES_CACHE.update(result)
            _NAMES_CACHE_TS = now
    resolved: dict[str, str] = {}
    for c in codes:
        candidate = _NAMES_CACHE.get(c) or local.get(c)
        resolved[c] = str(candidate).strip() if _usable_name(c, candidate) else "名称不可用"
    return resolved

# ── 入口 ──────────────────────────────────────
_RUNTIME_APP = None
_RUNTIME_APP_LOCK = threading.Lock()

def _create_runtime_app():
    """Start schedulers unconditionally; each loop audits the current session."""
    global _RUNTIME_APP
    with _RUNTIME_APP_LOCK:
        if _RUNTIME_APP is None:
            app = create_app(start_background=True, start_paper_forward=True)
            try:
                app.config["MARKET_INTELLIGENCE"].start()
                scheduler = DataScheduler(app.config["QUANT_DATA_SERVICE"])
                scheduler.start()
                app.extensions["quant_data_scheduler"] = scheduler
            except Exception:
                stop_app_background(app)
                raise
            _RUNTIME_APP = app
        return _RUNTIME_APP

def main():
    import os as _os
    from scripts.verify_runtime_dependencies import verify_or_raise
    from .runtime_lifecycle import start_log_rotator, stop_log_rotators

    verify_or_raise()
    start_log_rotator(Path(_os.environ.get(
        "OLYMPUS_LOG_PATH", "/tmp/olympus-trading.log",
    )))
    app = None
    try:
        _waitress_runtime = _PROJECT / "third_party" / "runtime" / "waitress"
        if _waitress_runtime.exists() and str(_waitress_runtime) not in sys.path:
            sys.path.insert(0, str(_waitress_runtime))
        from waitress import serve
        port = _bounded_int(
            _os.environ.get("OLYMPUS_PORT") or _os.environ.get("PORT", "5200"),
            "OLYMPUS_PORT",
            minimum=1, maximum=65535,
        )
        host = _validated_bind_host(_os.environ)
        app = _create_runtime_app()
        print(f"⚡ Regis Oracle starting on http://{host}:{port}")
        proxy_settings = {}
        if (
            _os.environ.get("OLYMPUS_REMOTE_MODE") == "1"
            and _os.environ.get("OLYMPUS_BEHIND_TLS_PROXY") == "1"
        ):
            proxy_settings = {
                "trusted_proxy": _os.environ.get(
                    "OLYMPUS_TRUSTED_PROXY", "*",
                ),
                "trusted_proxy_headers": {"x-forwarded-proto"},
            }
        serve(
            app, host=host, port=port, threads=16, channel_timeout=120,
            **proxy_settings,
        )
    finally:
        if app is not None:
            stop_app_background(app, timeout_seconds=5.0)
        stop_log_rotators(timeout_seconds=2.0)

if __name__ == "__main__":
    main()
