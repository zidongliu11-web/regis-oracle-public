"""Public factor-system status API and its presentation helpers.

Routes are registered directly on the owning Flask app; no blueprint or hidden dependency container.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from threading import Condition

from flask import jsonify, request

from .http_runtime import internal_error_response, log_internal_error
from trade_assistant.deepseek import EXTERNAL_PAID_API_DISABLED


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_EXTERNAL_RESEARCH_STATUS_PATH = (
    _PROJECT_ROOT / "reports" / "research_supervisor_status.json"
)
_SOURCE_FRONTIER_PATH = (
    _PROJECT_ROOT / "research" / "REMAINING_HIGH_QUALITY_SOURCE_FRONTIER_V1.json"
)
_SOURCE_FRONTIER_V2_PATH = (
    _PROJECT_ROOT / "research" / "NEXT_INDEPENDENT_FACTOR_MECHANISM_SCREEN_V2.json"
)
_OVERNIGHT_TREND_INPUT_ROOT = (
    _PROJECT_ROOT / "research" / "data" / "overnight_trend_monthly_12_1_v1"
)
_OVERNIGHT_TREND_PROGRESS_PATH = _OVERNIGHT_TREND_INPUT_ROOT / "download_progress.json"
_OVERNIGHT_TREND_MANIFEST_PATH = _OVERNIGHT_TREND_INPUT_ROOT / "download_manifest.json"
_OVERNIGHT_TREND_PIPELINE_STATUS_PATH = (
    _PROJECT_ROOT / "reports" / "ovnt_input_pipeline_status.json"
)
_OVERNIGHT_TREND_FACTOR_VALUE_AUDIT_PATH = (
    _PROJECT_ROOT / "research" / "OVNT_FACTOR_VALUE_REAL_EXECUTION_AUDIT_1.json"
)
_OVERNIGHT_TREND_HISTORICAL_CLOSURE_PATH = (
    _PROJECT_ROOT / "research" / "OVNT_HISTORICAL_VALIDATION_TERMINAL_CLOSURE_1.json"
)
_GITHUB_FACTOR_BATCH_PATH = (
    _PROJECT_ROOT / "reports" / "v276_github_factor_batch_intake.json"
)
_SHARE_ISS_RESOLUTION_PATH = (
    _PROJECT_ROOT / "research" / "SHARE_ISS_1Y_V1_INPUT_EPOCH_RESOLUTION.json"
)
_CASH_INPUT_RESOLUTION_PATH = (
    _PROJECT_ROOT / "research" / "CASH_TO_ASSETS_V1_INPUT_EPOCH_RESOLUTION.json"
)
_INVENTORY_FACTOR_RESULT_PATH = (
    _PROJECT_ROOT / "reports" / "v280_inventory_growth_blind_factor_test.json"
)
_DIVSEASON_FACTOR_RESULT_PATH = (
    _PROJECT_ROOT / "reports" / "v284_divseason_blind_factor_test.json"
)
_DIVSEASON_LIFECYCLE_PATH = (
    _PROJECT_ROOT / "research" / "DIVIDEND_SEASONALITY_V1_LIFECYCLE_RESOLUTION.json"
)
_DIVINIT_FACTOR_RESULT_PATH = (
    _PROJECT_ROOT / "reports" / "dividend_initiation_v1_blind_factor_test.json"
)
_DIVINIT_CLOSURE_PATH = (
    _PROJECT_ROOT / "research" / "DIVIDEND_INITIATION_V1_HISTORICAL_TEST_CLOSURE.json"
)
_BLOCK_TRADE_V2_RESULT_PATH = (
    _PROJECT_ROOT / "reports" / "v303_block_trade_premium_v2_blind_factor_test.json"
)
_BLOCK_TRADE_V2_TERMINAL_PATH = (
    _PROJECT_ROOT / "research" / "BLOCK_TRADE_PREMIUM_V1_V2_TERMINAL_RESOLUTION.json"
)
_CHTAX_INPUT_EPOCH_PATH = (
    _PROJECT_ROOT / "reports" / "v281_chtax_full_market_input_epoch_audit.json"
)
_V292_PROTOCOL_PATH = (
    _PROJECT_ROOT / "research" / "V292_CNINFO_ANNUAL_SHAREHOLDER_COUNT_INPUT_EPOCH_PROTOCOL.json"
)
_V292_CHECKPOINT_PATH = (
    _PROJECT_ROOT / "research" / "data" / "v292_cninfo_annual_shareholder_count_inputs"
    / "checkpoint_v2.sqlite3"
)
_V292_REPORT_PATH = (
    _PROJECT_ROOT / "reports" / "v292_cninfo_annual_shareholder_count_input_epoch_audit.json"
)
_V293_REPORT_PATH = (
    _PROJECT_ROOT / "reports" / "v293_cninfo_shareholder_count_summary_repair_audit.json"
)
_FIRST_FILING_V21_ADMISSION_PATH = (
    _PROJECT_ROOT / "research"
    / "FIRST_FILING_ANNUAL_PDF_EPOCH_V1_V21_FORMAL_COLLECTION_ADMISSION.json"
)
_FIRST_FILING_V21_TICKET_PATH = (
    _PROJECT_ROOT / "research"
    / "FIRST_FILING_ANNUAL_PDF_EPOCH_V1_V21_FORMAL_EXECUTION_TICKET.json"
)
_FIRST_FILING_V21_RECEIPT_PATH = (
    _PROJECT_ROOT / "reports"
    / "first_filing_annual_pdf_epoch_v1_v21_execution_used.json"
)
_FIRST_FILING_V21_PAUSE_PATH = (
    _PROJECT_ROOT / "reports" / "first_filing_annual_pdf_epoch_v1_v21_pause.json"
)
_FIRST_FILING_V21_RUN_PATH = (
    _PROJECT_ROOT / "reports" / "first_filing_annual_pdf_epoch_v1_v21_run.json"
)
_FIRST_FILING_V21_RECOVERY1_ADJUDICATION_PATH = (
    _PROJECT_ROOT / "research"
    / "FIRST_FILING_ANNUAL_PDF_EPOCH_V1_V21_PAUSE_ADJUDICATION_V1.json"
)
_FIRST_FILING_V21_RECOVERY1_ADMISSION_PATH = (
    _PROJECT_ROOT / "research"
    / "FIRST_FILING_ANNUAL_PDF_EPOCH_V1_V21_RECOVERY1_FORMAL_COLLECTION_ADMISSION.json"
)
_FIRST_FILING_V21_RECOVERY1_TICKET_PATH = (
    _PROJECT_ROOT / "research"
    / "FIRST_FILING_ANNUAL_PDF_EPOCH_V1_V21_RECOVERY1_FORMAL_EXECUTION_TICKET.json"
)
_FIRST_FILING_V21_RECOVERY1_RECEIPT_PATH = (
    _PROJECT_ROOT / "reports"
    / "first_filing_annual_pdf_epoch_v1_v21_recovery1_execution_used.json"
)
_FIRST_FILING_V21_RECOVERY1_PAUSE_PATH = (
    _PROJECT_ROOT / "reports"
    / "first_filing_annual_pdf_epoch_v1_v21_recovery1_pause.json"
)
_FIRST_FILING_V21_RECOVERY1_RUN_PATH = (
    _PROJECT_ROOT / "reports"
    / "first_filing_annual_pdf_epoch_v1_v21_recovery1_run.json"
)
_FIRST_FILING_V21_RECOVERY2_AMENDMENT_PATH = (
    _PROJECT_ROOT / "research"
    / "FIRST_FILING_ANNUAL_PDF_EPOCH_V1_V21_RECOVERY1_PARSER_REMEDIATION_AMENDMENT_V1.json"
)
_FIRST_FILING_V21_RECOVERY2_ADMISSION_PATH = (
    _PROJECT_ROOT / "research"
    / "FIRST_FILING_ANNUAL_PDF_EPOCH_V1_V21_RECOVERY2_FORMAL_COLLECTION_ADMISSION.json"
)
_FIRST_FILING_V21_RECOVERY2_TICKET_PATH = (
    _PROJECT_ROOT / "research"
    / "FIRST_FILING_ANNUAL_PDF_EPOCH_V1_V21_RECOVERY2_FORMAL_EXECUTION_TICKET.json"
)
_FIRST_FILING_V21_RECOVERY2_RECEIPT_PATH = (
    _PROJECT_ROOT / "reports"
    / "first_filing_annual_pdf_epoch_v1_v21_recovery2_execution_used.json"
)
_FIRST_FILING_V21_RECOVERY2_PAUSE_PATH = (
    _PROJECT_ROOT / "reports"
    / "first_filing_annual_pdf_epoch_v1_v21_recovery2_pause.json"
)
_FIRST_FILING_V21_RECOVERY2_RUN_PATH = (
    _PROJECT_ROOT / "reports"
    / "first_filing_annual_pdf_epoch_v1_v21_recovery2_run.json"
)
_FIRST_FILING_V21_RECOVERY2_TERMINAL_AUDIT_PATH = (
    _PROJECT_ROOT / "reports"
    / "first_filing_annual_pdf_epoch_v1_v21_recovery2_terminal_audit.json"
)
_FIRST_FILING_V21_RECOVERY3_AMENDMENT_PATH = (
    _PROJECT_ROOT / "research"
    / "FIRST_FILING_ANNUAL_PDF_EPOCH_V1_V21_RECOVERY2_600884_PAUSE_ADJUDICATION_AND_PARSER_AMENDMENT_V1.json"
)
_FIRST_FILING_V21_RECOVERY3_ADMISSION_PATH = (
    _PROJECT_ROOT / "research"
    / "FIRST_FILING_ANNUAL_PDF_EPOCH_V1_V21_RECOVERY3_FORMAL_COLLECTION_ADMISSION.json"
)
_FIRST_FILING_V21_RECOVERY3_TICKET_PATH = (
    _PROJECT_ROOT / "research"
    / "FIRST_FILING_ANNUAL_PDF_EPOCH_V1_V21_RECOVERY3_FORMAL_EXECUTION_TICKET.json"
)
_FIRST_FILING_V21_RECOVERY3_RECEIPT_PATH = (
    _PROJECT_ROOT / "reports"
    / "first_filing_annual_pdf_epoch_v1_v21_recovery3_execution_used.json"
)
_FIRST_FILING_V21_RECOVERY3_PAUSE_PATH = (
    _PROJECT_ROOT / "reports"
    / "first_filing_annual_pdf_epoch_v1_v21_recovery3_pause.json"
)
_FIRST_FILING_V21_RECOVERY3_RUN_PATH = (
    _PROJECT_ROOT / "reports"
    / "first_filing_annual_pdf_epoch_v1_v21_recovery3_run.json"
)
_FIRST_FILING_V21_RECOVERY3_TERMINAL_AUDIT_PATH = (
    _PROJECT_ROOT / "reports"
    / "first_filing_annual_pdf_epoch_v1_v21_recovery3_terminal_audit.json"
)
_FIRST_FILING_STATEMENT_PARSER_PATH = (
    _PROJECT_ROOT / "research" / "first_filing_statement_values_v2_parser.py"
)
_FIRST_FILING_2024_CHECKPOINT_PATH = (
    _PROJECT_ROOT / "research" / "data" / "first_filing_annual_pdf_epoch_v1"
    / "shards" / "2024-12-31" / "checkpoint_v3.sqlite3"
)
_FIRST_FILING_TERMINAL_REPARSE_FAILURE_PATH = (
    _PROJECT_ROOT / "reports"
    / "first_filing_statement_values_v2_v21_terminal_reparse_failure.json"
)
_FIRST_FILING_MATERIAL_OVERLAY_PATH = (
    _PROJECT_ROOT / "reports"
    / "first_filing_statement_values_v2_v21_material_overlay.json"
)
_FIRST_FILING_MATERIAL_OVERLAY_COMPLETION_PATH = (
    _PROJECT_ROOT / "reports"
    / "first_filing_statement_values_v2_v21_material_overlay_completion.json"
)
_FIRST_FILING_FIXED2_MERGE_V2_PATH = (
    _PROJECT_ROOT / "reports"
    / "first_filing_statement_values_v2_v21_fixed2_ocr_merge_v2.json"
)
_CASH_FLOW_FINAL_STATEMENT_MANIFEST_V2_PATH = (
    _PROJECT_ROOT / "reports"
    / "cash_flow_profitability_final_statement_inputs_v2_manifest.json"
)
_CASH_FLOW_INPUT_READINESS_V2_PATH = (
    _PROJECT_ROOT / "reports"
    / "cash_flow_profitability_current_assets_input_readiness_v2.json"
)
_CASH_FLOW_V2_FREEZE_ADMISSION_PATH = (
    _PROJECT_ROOT / "research"
    / "CASH_FLOW_PROFITABILITY_CURRENT_ASSETS_V2_FREEZE_ADMISSION_V1.json"
)
_CASH_FLOW_V2_ADAPTER_PATH = (
    _PROJECT_ROOT / "research"
    / "cash_flow_profitability_current_assets_input_readiness_v2_adapter.py"
)
_CASH_FLOW_FACTOR_NOVELTY_REPORT_PATH = (
    _PROJECT_ROOT / "reports"
    / "cash_flow_profitability_current_assets_v1_factor_novelty_v2.json"
)
_CASH_FLOW_FACTOR_NOVELTY_ADMISSION_PATH = (
    _PROJECT_ROOT / "research"
    / "CASH_FLOW_PROFITABILITY_CURRENT_ASSETS_V1_FACTOR_NOVELTY_ADMISSION_V2.json"
)
_CASH_FLOW_FACTOR_NOVELTY_IMPLEMENTATION_PATH = (
    _PROJECT_ROOT / "research"
    / "cash_flow_profitability_current_assets_v1_factor_novelty_v2.py"
)
_CASH_FLOW_FACTOR_NOVELTY_TEST_PATH = (
    _PROJECT_ROOT / "tests"
    / "test_cash_flow_profitability_current_assets_v1_factor_novelty_v2.py"
)
_CASH_FLOW_FACTOR_NOVELTY_FAILED_V1_REPORT_PATH = (
    _PROJECT_ROOT / "reports"
    / "cash_flow_profitability_current_assets_v1_factor_novelty.json"
)
_CASH_FLOW_FACTOR_NOVELTY_TERMINAL_AMENDMENT_PATH = (
    _PROJECT_ROOT / "research"
    / "CASH_FLOW_PROFITABILITY_CURRENT_ASSETS_V1_TERMINAL_LIFECYCLE_AMENDMENT_1.json"
)
_FIRST_FILING_2025_TERMINAL_PATH = (
    _PROJECT_ROOT / "research"
    / "FIRST_FILING_ANNUAL_PDF_EPOCH_V1_V20_TERMINAL_AUDIT_V1.json"
)
_EMPLOYEE_INPUT_EPOCH_PATH = (
    _PROJECT_ROOT / "research" / "EMPLOYEE_HISTORY_PIT_INPUT_EPOCH_V1.json"
)
_EMPLOYMENT_FACTOR_RESULT_PATH = (
    _PROJECT_ROOT / "reports" / "employment_growth_blind_factor_test_v1.json"
)
_LEADER_CROWDING_AGGREGATE_RESULT_PATH = (
    _PROJECT_ROOT / "research" / "RECENT_LIMIT_HIT_CROWDING_V1_BLIND_RESULT.json"
)
_LEADER_CROWDING_STATE_RESULT_PATH = (
    _PROJECT_ROOT / "research"
    / "RECENT_LIMIT_HIT_CROWDING_STATE_MATRIX_V1_RESULT.json"
)
_LEADER_CROWDING_FORWARD_RESULT_PATH = (
    _PROJECT_ROOT / "reports" / "recent_limit_hit_crowding_forward_v3_latest.json"
)
_LEADER_SINGLE_HIT_RESULT_PATH = (
    _PROJECT_ROOT / "research" / "STATE_MAINLINE_SINGLE_HIT_LEADER_V1_BLIND_RESULT.json"
)
_LEADER_RESTRENGTHENING_RESULT_PATH = (
    _PROJECT_ROOT / "research"
    / "STATE_CONDITIONED_LEADER_RESTRENGTHENING_5M_V1_BLIND_RESULT.json"
)
_STABILIZING_SINGLE_HIT_RESULT_PATH = (
    _PROJECT_ROOT / "research"
    / "STABILIZING_MAINLINE_SINGLE_HIT_LEADER_V1_BLIND_RESULT.json"
)
_LEADER_TACTIC_PLAIN_LANGUAGE = {
    "STATE_MAINLINE_SINGLE_HIT_LEADER_V1": {
        "characteristic": "在情绪上升期只选两条最强主线内、首次封板且成交活跃的核心股，避免重复涨停拥挤。",
        "consider_when": "已完成市场状态为情绪上升，板块动量为正、站上20日均线的成分股不少于六成。",
        "entry_method": "收盘后冻结信号，下一交易日开盘仅在可成交时研究入场，单只参考仓位不超过账户一成。",
        "invalidation": "次日封死涨停、身份缺失或风控输入不完整时不成交；目标持有五个交易日后按可成交开盘复核退出。",
    },
    "STATE_CONDITIONED_LEADER_RESTRENGTHENING_5M_V1": {
        "characteristic": "在情绪上升期观察预先冻结的主线龙头，先出现分歧，再确认重新站回开盘价、均价和前一根高点。",
        "consider_when": "情绪上升且主线扩散仍成立，核心股在路径出现前已经确认，不临时追认后排。",
        "entry_method": "09:35—10:30 内只使用已完成5分钟K线，首个有效转强信号后的下一根K线开盘研究入场。",
        "invalidation": "未完成K线、封板排队不可核验、板块走弱或账户成本不完整均不入场；次一可交易日复核退出。",
    },
    "STABILIZING_MAINLINE_SINGLE_HIT_LEADER_V1": {
        "characteristic": "把同一套首次封板核心股规则单独放在弱修复情绪检验，研究早期扩散而非成熟龙头追高。",
        "consider_when": "已完成市场状态为弱修复，主线动量与六成以上板块广度同时确认。",
        "entry_method": "收盘后冻结信号，下一交易日开盘可成交时才研究入场，最多两只且总新仓参考不超过两成。",
        "invalidation": "封板不可成交、板块确认撤销或账户风险线缺失时不入场；五个交易日后按可成交开盘复核退出。",
    },
}

_STATE_AUXILIARY_PRESENTATION = {
    "risk_closed": {
        "label": "252日低波动（防守排序研究）",
        "characteristic": "只在已确认板块内研究波动更低的候选，目标是减少回撤，不用于寻找涨停股。",
        "status_label": "封闭样本外失败 · 当前不使用",
    },
    "weak_repair": {
        "label": "近20日涨停拥挤度（次数越少越优）",
        "characteristic": "弱修复时回避反复涨停的拥挤候选，优先研究尚未被过度交易的主线成分股。",
        "status_label": "历史选组存在偏差 · 修正验证中",
    },
    "sentiment_rising": {
        "label": "行业动量与低换手早期强势",
        "characteristic": "情绪上升时研究主线行业强度与早期扩散，任何一项都不能单独当作买点。",
        "status_label": "两条历史检验均失败 · 当前不使用",
    },
    "crowded_climax": {
        "label": "涨停拥挤度（追高风险观察）",
        "characteristic": "只用于识别重复涨停与交易拥挤，触发的是追高否决，不是反向抄底信号。",
        "status_label": "样本不足且终段为负 · 仅观察",
    },
    "retreat": {
        "label": "账户波动与回撤约束",
        "characteristic": "只根据真实账户与持仓风险收缩暴露，不参加新股排序，也不产生买点。",
        "status_label": "风控覆盖层研究 · 不产生入场提醒",
    },
}
_SEALED_RESEARCH_REPORTS = (
    ("Gross Profitability", "v250_gross_profitability_blind_factor_test.json"),
    ("Coskewness", "v256_coskewness_blind_factor_test.json"),
    ("Mom12m", "v258_mom12m_blind_factor_test.json"),
    ("High52", "v260_high52_blind_factor_test.json"),
    ("Firm Age", "v262_firm_age_blind_factor_test.json"),
    ("Herf Sales", "herf_sales_blind_factor_test_v1.json"),
    ("Employment Growth", "employment_growth_blind_factor_test_v1.json"),
    ("Nominal Price", "v264_nominal_price_blind_factor_test.json"),
    ("Operating Leverage", "v267_operating_leverage_blind_factor_test.json"),
    ("Percent Operating Accruals", "v270_percent_operating_accruals_blind_factor_test.json"),
    ("Book Equity Growth", "v275_book_equity_growth_blind_factor_test.json"),
    ("Inventory Growth", "v280_inventory_growth_blind_factor_test.json"),
    ("Dividend Seasonality", "v284_divseason_blind_factor_test.json"),
    ("Dividend Initiation", "dividend_initiation_v1_blind_factor_test.json"),
    ("Block Trade Premium", "v287_block_trade_premium_blind_factor_test.json"),
    ("Operating Cash Flow Growth", "v290_operating_cash_flow_growth_blind_factor_test.json"),
    ("Shareholder Count Change", "v296_cninfo_shareholder_count_change_blind_factor_test.json"),
    ("Block Trade Premium V2", "v303_block_trade_premium_v2_blind_factor_test.json"),
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_STATUS_IDENTITY_CACHE: dict[str, tuple[tuple, dict]] = {}
_STATUS_IDENTITY_INFLIGHT: set[str] = set()
_STATUS_IDENTITY_CONDITION = Condition()


def _path_stat_identity(path: Path) -> tuple:
    """Return a cheap identity that changes when a status input is replaced."""
    try:
        stat = path.stat()
    except OSError:
        return (str(path), None)
    return (
        str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
    )


def _paths_stat_identity(paths) -> tuple:
    return tuple(_path_stat_identity(Path(path)) for path in paths)


def _identity_cached_status(
    name: str,
    identity,
    loader,
    *,
    cacheable,
    unstable,
) -> dict:
    """Coalesce same-input status reads; never reuse a result across identities."""
    while True:
        key = identity()
        with _STATUS_IDENTITY_CONDITION:
            cached = _STATUS_IDENTITY_CACHE.get(name)
            if cached is not None and cached[0] == key:
                return cached[1]
            if name in _STATUS_IDENTITY_INFLIGHT:
                _STATUS_IDENTITY_CONDITION.wait()
                continue
            _STATUS_IDENTITY_INFLIGHT.add(name)
            break

    try:
        value = loader()
        stable = identity() == key
        if not stable:
            value = unstable()
        with _STATUS_IDENTITY_CONDITION:
            if stable and cacheable(value):
                _STATUS_IDENTITY_CACHE[name] = (key, value)
        return value
    finally:
        with _STATUS_IDENTITY_CONDITION:
            _STATUS_IDENTITY_INFLIGHT.discard(name)
            _STATUS_IDENTITY_CONDITION.notify_all()


def _active_candidate_collection_status(
    progress_path: Path = _OVERNIGHT_TREND_PROGRESS_PATH,
    manifest_path: Path = _OVERNIGHT_TREND_MANIFEST_PATH,
    *,
    now: datetime | None = None,
) -> dict:
    """Expose source collection progress without implying factor computation."""
    paths = (progress_path, manifest_path)

    def load() -> dict:
        try:
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {"available": False, "reason": "progress_unavailable"}
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            manifest = {}
        symbols = progress.get("symbols")
        symbol_rows = list(symbols.values()) if isinstance(symbols, dict) else (
            symbols if isinstance(symbols, list) else []
        )
        status_counts: dict[str, int] = {}
        for row in symbol_rows:
            status = str((row or {}).get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
        return {
            "available": True,
            "candidate_id": progress.get("candidate_id"),
            "state": progress.get("state"),
            "updated_at": progress.get("updated_at"),
            "target_symbols": int(progress.get("target_symbols") or 0),
            "completed_symbols": int(progress.get("completed_symbols") or 0),
            "remaining_symbols": int(progress.get("remaining_symbols") or 0),
            "current_code": progress.get("current_code"),
            "status_counts": status_counts,
            "manifest_status": manifest.get("status"),
            "manifest_generated_at": manifest.get("generated_at"),
            "ready_for_input_audit": manifest.get("ready_for_input_audit") is True,
            "ready_for_factor_values": manifest.get("ready_for_factor_values") is True,
            "result_data_read": progress.get("result_data_read") is True,
            "candidate_outcome_computed": progress.get("candidate_outcome_computed") is True,
        }

    raw = dict(_identity_cached_status(
        "active_candidate_collection",
        lambda: _paths_stat_identity(paths),
        load,
        cacheable=lambda value: True,
        unstable=lambda: {"available": False, "reason": "progress_changed_during_read"},
    ))
    updated_at = raw.get("updated_at")
    try:
        updated = datetime.fromisoformat(str(updated_at))
        observed = now or datetime.now(updated.tzinfo)
        age_seconds = max(0, int((observed - updated).total_seconds()))
    except (TypeError, ValueError):
        age_seconds = None
    target = int(raw.get("target_symbols") or 0)
    completed = int(raw.get("completed_symbols") or 0)
    state = str(raw.get("state") or "unknown")
    fresh = age_seconds is not None and age_seconds <= 120
    raw.update({
        "fresh": fresh,
        "age_seconds": age_seconds,
        "progress_pct": round(completed / target * 100, 2) if target > 0 else None,
        "operational_status": (
            "collecting" if state == "running" and fresh
            else "stalled" if state == "running" else state
        ),
        "factor_values_computed": False,
        "ic_or_backtest_run": False,
        "production_allowed": False,
    })
    return raw


def _active_candidate_input_status(
    status_path: Path = _OVERNIGHT_TREND_PIPELINE_STATUS_PATH,
) -> dict:
    """Expose audited-input completion without granting factor authority."""
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
        manifest_path = Path(str(status.get("input_manifest_path") or ""))
        expected_sha = str(status.get("input_manifest_sha256") or "")
        actual_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"available": False, "reason": "input_pipeline_status_unavailable"}
    valid = (
        status.get("candidate_id") == "fresh_overnight_trend_monthly_12_1_v1"
        and status.get("status")
        == "input_integrity_passed_waiting_factor_admission"
        and status.get("audit_passed") is True
        and len(expected_sha) == 64
        and actual_sha == expected_sha
    )
    authority_false = all(status.get(key) is False for key in (
        "factor_values_computed", "candidate_outcome_read", "ic_or_backtest_run",
        "runnable", "production_allowed", "automatic_trading_allowed",
    ))
    if not valid or not authority_false:
        return {"available": False, "reason": "input_pipeline_identity_invalid"}
    return {
        "available": True,
        "candidate_id": status["candidate_id"],
        "status": status["status"],
        "completed_symbols": int(status.get("completed_symbols") or 0),
        "target_symbols": int(status.get("target_symbols") or 0),
        "audit_passed": True,
        "input_manifest_sha256": expected_sha,
        "factor_values_computed": False,
        "candidate_outcome_read": False,
        "ic_or_backtest_run": False,
        "runnable": False,
        "production_allowed": False,
        "automatic_trading_allowed": False,
    }


def _active_candidate_factor_value_status(
    audit_path: Path = _OVERNIGHT_TREND_FACTOR_VALUE_AUDIT_PATH,
    *,
    project_root: Path = _PROJECT_ROOT,
) -> dict:
    """Expose reproduced factor values without implying predictive validity."""
    audit_path = Path(audit_path).resolve()
    project_root = Path(project_root).resolve()
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        bindings = audit.get("bindings") or {}
        paths = [audit_path]
        for binding in bindings.values():
            path = (project_root / str(binding.get("path") or "")).resolve()
            path.relative_to(project_root)
            paths.append(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"available": False, "reason": "factor_value_audit_unavailable"}

    def load() -> dict:
        try:
            current = json.loads(audit_path.read_text(encoding="utf-8"))
            current_bindings = current.get("bindings") or {}
            for binding in current_bindings.values():
                path = (project_root / str(binding.get("path") or "")).resolve()
                path.relative_to(project_root)
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                if actual != binding.get("sha256"):
                    raise ValueError("factor_value_binding_mismatch")
            result = current.get("result") or {}
            truth = current.get("truth_flags") or {}
            authority = current.get("authority") or {}
            if (
                current.get("candidate_id")
                != "fresh_overnight_trend_monthly_12_1_v1"
                or current.get("status")
                != "factor_values_byte_reproduced_waiting_validation_admission"
                or not current_bindings.get("formal_output")
                or not current_bindings.get("reproduction_output")
                or result.get("output_sha256") != result.get("reproduction_sha256")
                or truth.get("factor_values_computed") is not True
                or truth.get("factor_value_bytes_independently_reproduced") is not True
                or any(truth.get(key) is not False for key in (
                    "candidate_outcomes_read", "future_returns_read",
                    "ic_or_backtest_run", "performance_compared", "runnable",
                    "production_allowed",
                ))
                or any(value is not False for value in authority.values())
            ):
                raise ValueError("factor_value_audit_identity_invalid")
            return {
                "available": True,
                "candidate_id": current["candidate_id"],
                "status": current["status"],
                "rows": int(result.get("rows") or 0),
                "formation_dates": int(result.get("formation_dates") or 0),
                "first_signal_date": result.get("first_signal_date"),
                "last_signal_date": result.get("last_signal_date"),
                "output_sha256": result.get("output_sha256"),
                "factor_values_computed": True,
                "factor_value_bytes_independently_reproduced": True,
                "candidate_outcomes_read": False,
                "future_returns_read": False,
                "ic_or_backtest_run": False,
                "performance_compared": False,
                "runnable": False,
                "production_allowed": False,
                "automatic_trading_allowed": False,
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {"available": False, "reason": "factor_value_audit_identity_invalid"}

    return dict(_identity_cached_status(
        f"active_candidate_factor_values:{audit_path}",
        lambda: _paths_stat_identity(tuple(paths)),
        load,
        cacheable=lambda value: True,
        unstable=lambda: {
            "available": False,
            "reason": "factor_value_audit_changed_during_read",
        },
    ))


def _active_candidate_historical_validation_status(
    closure_path: Path = _OVERNIGHT_TREND_HISTORICAL_CLOSURE_PATH,
    *,
    project_root: Path = _PROJECT_ROOT,
) -> dict:
    """Expose the terminal validation truth without inventing performance."""
    closure_path = Path(closure_path).resolve()
    project_root = Path(project_root).resolve()
    try:
        closure = json.loads(closure_path.read_text(encoding="utf-8"))
        bindings = closure.get("bindings") or {}
        for binding in bindings.values():
            path = (project_root / str(binding.get("path") or "")).resolve()
            path.relative_to(project_root)
            if hashlib.sha256(path.read_bytes()).hexdigest() != binding.get("sha256"):
                raise ValueError("historical_closure_binding_mismatch")
        result = closure.get("result_boundary") or {}
        disposition = closure.get("disposition") or {}
        failure = closure.get("terminal_failure") or {}
        if (
            closure.get("candidate_id")
            != "fresh_overnight_trend_monthly_12_1_v1"
            or closure.get("status")
            != "permanently_closed_non_runnable_execution_identity_gap"
            or result.get("monthly_result_artifact_written") is not False
            or result.get("statistics_runner_called") is not False
            or result.get("aggregate_performance_seen") is not False
            or result.get("historical_factor_gate_evaluated") is not False
            or disposition.get("exact_candidate_closed") is not True
            or disposition.get("third_run_allowed") is not False
            or any(disposition.get(key) is not False for key in (
                "runnable", "production_allowed", "recommendation_allowed",
                "position_sizing_allowed", "automatic_trading_allowed",
            ))
        ):
            raise ValueError("historical_closure_identity_invalid")
        return {
            "available": True,
            "candidate_id": closure["candidate_id"],
            "status": closure["status"],
            "terminal": True,
            "factor_values_computed": result.get("factor_values_computed_and_byte_reproduced") is True,
            "real_outcome_inputs_opened": result.get("real_outcome_inputs_opened") is True,
            "historical_factor_gate_evaluated": False,
            "historical_factor_gate_passed": False,
            "performance_metrics_available": False,
            "failure_code": "missing_delisting_or_exchange_economics",
            "failure_security": failure.get("missing_identity"),
            "message": closure.get("user_facing_summary"),
            "next_action": disposition.get("next_research_action"),
            "runnable": False,
            "production_allowed": False,
            "recommendation_allowed": False,
            "position_sizing_allowed": False,
            "automatic_trading_allowed": False,
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"available": False, "reason": "historical_closure_unavailable"}


def _apply_active_candidate_terminal_closure(
    source_gate: dict, closure: dict,
) -> dict:
    """Overlay immutable terminal evidence without rewriting source history."""
    if not (
        closure.get("available") is True
        and closure.get("terminal") is True
        and closure.get("candidate_id")
        == "fresh_overnight_trend_monthly_12_1_v1"
    ):
        return source_gate
    result = dict(source_gate)
    candidates = [dict(item) for item in result.get("candidates") or []]
    found = False
    for candidate in candidates:
        if candidate.get("candidate_id") != closure["candidate_id"]:
            continue
        found = True
        candidate.update({
            "status": "terminal_validation_closed",
            "runnable": False,
            "actionable": False,
            "terminal": True,
            "terminal_reason": closure.get("failure_code"),
        })
        warnings = list(candidate.get("warnings") or [])
        warnings.append("historical_validation_closed_no_performance_metrics")
        candidate["warnings"] = list(dict.fromkeys(warnings))
    if not found:
        return source_gate
    frontier = [item for item in candidates if item.get("actionable") is True]
    pending = sum(
        item.get("status") == "implementation_pending" for item in frontier
    )
    data_pending = sum(item.get("status") == "data_pending" for item in frontier)
    result.update({
        "candidates": candidates,
        "frontier_candidate_count": len(frontier),
        "frontier_candidate_ids": [item["candidate_id"] for item in frontier],
        "superseded_candidate_count": len(candidates) - len(frontier),
        "actionable_implementation_pending_count": pending,
        "actionable_data_pending_count": data_pending,
        "actionable_runnable_count": sum(
            item.get("runnable") is True for item in frontier
        ),
        "terminal_validation_closed_count": sum(
            item.get("status") == "terminal_validation_closed"
            for item in candidates
        ),
    })
    blockers = [
        dict(item) for item in result.get("blockers") or []
        if item.get("code") not in {
            "source_candidate_data_pending", "source_candidates_not_runnable",
            "source_hypotheses_exhausted",
        }
    ]
    findings = [dict(item) for item in result.get("findings") or []]
    findings.append({
        "code": "active_candidate_terminally_closed",
        "message": (
            "隔夜趋势候选已按不可变失败证据关闭：没有可报告绩效，"
            "不再重跑、不投产；研究流程转向新的独立假设。"
        ),
        "system_fault": False,
    })
    if pending or data_pending:
        blockers.append({
            "code": "source_candidate_data_pending",
            "message": (
                "来源目录有效，但当前 runnable=0；"
                f"{pending} 个候选待实现，{data_pending} 个候选待补冻结输入。"
            ),
            "system_fault": False,
        })
    result["blockers"] = blockers
    result["findings"] = findings
    return result


def _apply_ovnt_terminal_closure_to_source_scout(
    source_scout: dict, closure: dict,
) -> dict:
    """Stop presenting the closed OVNT trial as unfinished source work."""
    if not (
        closure.get("available") is True
        and closure.get("terminal") is True
        and closure.get("candidate_id")
        == "fresh_overnight_trend_monthly_12_1_v1"
    ):
        return source_scout
    result = dict(source_scout)
    preview = [dict(item) for item in result.get("preview") or []]
    closed_doi = "10.1016/j.jedc.2024.104997"
    pending_identities = list(
        result.get("source_contract_pending_identities") or []
    )
    matched = closed_doi in pending_identities
    for item in preview:
        if (
            item.get("doi") == closed_doi
            and item.get("status") == "source_contract_pending"
        ):
            matched = True
            item.update({
                "status": "closed_historical_validation_execution_identity_gap",
                "pit_feasibility_status": closure.get("failure_code"),
                "input_audit_status": closure.get("status"),
                "input_contract_gaps": [],
                "next_action": closure.get("next_action"),
            })
    if not matched:
        return source_scout
    result["preview"] = preview
    result["source_contract_pending_identities"] = [
        identity for identity in pending_identities if identity != closed_doi
    ]
    result["source_contract_pending_count"] = max(
        0, int(result.get("source_contract_pending_count") or 0) - 1,
    )
    result["closed_count"] = int(result.get("closed_count") or 0) + 1
    unresolved = sum(int(result.get(key) or 0) for key in (
        "unreviewed_count", "formula_pending_count",
        "source_contract_pending_count",
        "repository_discovery_unreviewed_count",
        "repository_review_required_count",
    ))
    result["unresolved_total"] = unresolved
    result["work_blocked"] = unresolved > 0
    if unresolved == 0:
        result["operational_status"] = "review_complete"
    if (
        int(result.get("formula_pending_count") or 0) == 0
        and int(result.get("source_contract_pending_count") or 0) == 0
    ):
        result["candidate_draft_pipeline"] = {
            **(result.get("candidate_draft_pipeline") or {}),
            "status": "no_active_reviewed_source",
            "input_records": 0,
            "unique_drafts": 0,
            "catalog_review_ready": 0,
            "runnable": 0,
            "production": 0,
            "top_missing_requirements": [],
            "research_only": True,
        }
    return result


def _public_source_scout_status(path: Path) -> dict:
    """Return a bounded, fail-closed view of the research-only source inbox."""
    empty = {
        "enabled": True,
        "cadence_hours": 24,
        "scheduler_mode": "continuous_service_daily_bounded_scan",
        "continuous_compute": False,
        "status": "not_started",
        "operational_status": "not_started",
        "work_blocked": False,
        "last_attempt_at": None,
        "last_full_attempt_at": None,
        "last_success_at": None,
        "next_due_at": None,
        "run_mode": None,
        "retry_due_at": None,
        "retry_pending_count": 0,
        "retry_exhausted_count": 0,
        "retry_policy": {},
        "new_count": 0,
        "paper_unreviewed_count": 0,
        "unreviewed_count": 0,
        "unresolved_total": 0,
        "reviewed_count": 0,
        "closed_count": 0,
        "formula_pending_count": 0,
        "source_contract_pending_count": 0,
        "source_contract_pending_identities": [],
        "research_only": True,
        "automatic_factor_registration": False,
        "automatic_factor_computation": False,
        "automatic_backtest": False,
        "automatic_production": False,
        "automatic_repository_candidate_registration": False,
        "automatic_repository_execution": False,
        "sources": [],
        "input_snapshot_results": [],
        "repository_snapshot_results": [],
        "repository_status": "not_checked",
        "repository_successful_checks": 0,
        "repository_expected_checks": 0,
        "repository_review_required_count": 0,
        "repository_discovery_status": "not_checked",
        "repository_discovery_successful_queries": 0,
        "repository_discovery_expected_queries": 0,
        "repository_discovery_new_count": 0,
        "repository_discovery_unreviewed_count": 0,
        "repository_discovery_reviewed_count": 0,
        "repository_discovery_closed_count": 0,
        "repository_discovery_screened_out_count": 0,
        "repository_discoveries": [],
        "source_review_queue": {
            "status": "unavailable",
            "updated_at": None,
            "pending_count": None,
            "overdue_count": None,
            "resolved_count": None,
            "paper_pending_count": None,
            "repository_pending_count": None,
            "tasks": [],
            "research_only": True,
        },
        "candidate_draft_pipeline": {
            "status": "awaiting_reviewed_source",
            "input_records": 0,
            "unique_drafts": 0,
            "catalog_review_ready": 0,
            "runnable": 0,
            "production": 0,
            "top_missing_requirements": [],
            "research_only": True,
        },
        "preview": [],
        "errors": [],
    }
    try:
        source_body = path.read_bytes()
        source_state_sha256 = hashlib.sha256(source_body).hexdigest()
        raw = json.loads(source_body.decode("utf-8"))
        policy = raw.get("policy") or {}
        candidates = raw.get("candidates") or []
        if (
            not isinstance(raw, dict) or not isinstance(policy, dict)
            or policy.get("research_only") is not True
            or policy.get("automatic_candidate_registration") is not False
            or policy.get("automatic_factor_computation") is not False
            or policy.get("automatic_backtest") is not False
            or policy.get("automatic_production") is not False
            or policy.get("automatic_repository_candidate_registration") is not False
            or policy.get("automatic_repository_execution") is not False
            or not isinstance(candidates, list)
        ):
            raise ValueError("research-only source scout boundary is invalid")
    except FileNotFoundError:
        return empty
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            **empty,
            "status": "unavailable",
            "errors": [f"{type(exc).__name__}: source scout state invalid"],
        }
    def _source_recency(item: dict) -> str:
        return max(
            (
                str(item.get(key) or "")
                for key in ("updated_at", "reviewed_at", "first_seen_at")
            ),
            default="",
        )

    preview = []
    latest_candidates = sorted(
        (item for item in candidates if isinstance(item, dict)),
        key=_source_recency,
        reverse=True,
    )[:5]
    for item in latest_candidates:
        preview.append({
            "scout_id": item.get("scout_id"),
            "title": item.get("title"),
            "doi": item.get("doi"),
            "year": item.get("year"),
            "url": item.get("url"),
            "source_provider": item.get("source_provider"),
            "source_providers": list(item.get("source_providers") or []),
            "scope_evidence": item.get("scope_evidence"),
            "open_access_url": item.get("open_access_url"),
            "status": str(item.get("status") or "unreviewed_research_source"),
            "input_audit_status": item.get("input_audit_status"),
            "pit_feasibility_status": item.get("pit_feasibility_status"),
            "input_contract_gaps": list(item.get("input_contract_gaps") or [])[:8],
            "input_audit_artifact": item.get("input_audit_artifact"),
            "next_action": item.get("next_action"),
            "research_only": True,
            "runnable": False,
        })
    statuses = [
        str(item.get("status") or "unreviewed_research_source")
        for item in candidates if isinstance(item, dict)
    ]
    unreviewed_count = statuses.count("unreviewed_research_source")
    repository_snapshots = [
        item for item in raw.get("repository_snapshot_results") or []
        if isinstance(item, dict)
    ]
    repository_discoveries = [
        item for item in raw.get("repository_discoveries") or []
        if isinstance(item, dict)
    ]
    repository_review_required_count = sum(
        item.get("review_required") is True for item in repository_snapshots
    )
    repository_discovery_unreviewed_count = sum(
        str(item.get("status") or "") == "unreviewed_repository_source"
        for item in repository_discoveries
    )
    formula_pending_count = statuses.count("formula_pending")
    source_contract_pending_count = statuses.count("source_contract_pending")
    source_contract_pending_identities = sorted({
        str(item.get("doi") or item.get("identity") or item.get("scout_id") or "")
        for item in candidates
        if isinstance(item, dict)
        and str(item.get("status") or "") == "source_contract_pending"
        and (item.get("doi") or item.get("identity") or item.get("scout_id"))
    })
    unresolved_total = (
        unreviewed_count
        + formula_pending_count
        + source_contract_pending_count
        + repository_discovery_unreviewed_count
        + repository_review_required_count
    )
    try:
        from factor_system.candidate_draft_bridge import build_candidate_drafts

        active_reviewed_candidates = [
            item for item in candidates
            if isinstance(item, dict)
            and not str(item.get("status") or "").startswith((
                "closed_", "unreviewed",
            ))
        ]
        draft_batch = build_candidate_drafts(active_reviewed_candidates)
        draft_summary = draft_batch["summary"]
        missing_counts: dict[str, int] = {}
        for draft in draft_batch["drafts"]:
            for requirement in draft["missing_requirements"]:
                missing_counts[requirement] = missing_counts.get(requirement, 0) + 1
        draft_pipeline = {
            "status": (
                "catalog_review_ready"
                if draft_summary["catalog_review_ready"]
                else "no_active_reviewed_source"
                if not active_reviewed_candidates
                else "evidence_gaps_or_terminal_sources"
            ),
            **draft_summary,
            "production": 0,
            "top_missing_requirements": [
                {"requirement": requirement, "count": count}
                for requirement, count in sorted(
                    missing_counts.items(), key=lambda row: (-row[1], row[0])
                )[:5]
            ],
            "research_only": True,
        }
    except (KeyError, TypeError, ValueError) as exc:
        draft_pipeline = {
            **empty["candidate_draft_pipeline"],
            "status": "candidate_draft_bridge_invalid",
            "error": type(exc).__name__,
        }
    try:
        from factor_system.source_review_queue import public_status

        queue_status = public_status(
            path.with_name("public_source_review_queue.json")
        )
        queue_status_name = str(queue_status.get("status") or "unavailable")
        queue_source_sha = queue_status.get("source_state_sha256")
        if queue_status_name == "unavailable":
            source_review_queue = {
                **empty["source_review_queue"],
                "status": "unavailable",
                "error": queue_status.get("error") or "source review queue unavailable",
            }
        elif queue_source_sha != source_state_sha256:
            source_review_queue = {
                **empty["source_review_queue"],
                "status": "stale",
                "queue_source_state_sha256": queue_source_sha,
                "expected_source_state_sha256": source_state_sha256,
                "error": "source review queue does not match current source scout state",
            }
        else:
            queue_summary = queue_status.get("summary") or {}
            source_review_queue = {
                "status": queue_status_name,
                "updated_at": queue_status.get("updated_at"),
                **{
                    key: int(queue_summary[key]) for key in (
                        "pending_count", "overdue_count", "resolved_count",
                        "paper_pending_count", "repository_pending_count",
                    )
                },
                "tasks": list(queue_status.get("tasks") or [])[:10],
                "source_state_sha256": queue_source_sha,
                "research_only": True,
            }
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        source_review_queue = {
            **empty["source_review_queue"],
            "status": "unavailable",
            "error": f"{type(exc).__name__}: source review queue invalid",
        }
    repository_expected_checks = max(
        int(raw.get("repository_expected_checks") or 0),
        len(repository_snapshots),
    )
    repository_successful_checks = int(
        raw.get("repository_successful_checks") or 0
    )
    raw_status = str(raw.get("status") or "unknown")
    operational_status = (
        "degraded"
        if unresolved_total > 0
        else "review_complete_with_source_warnings"
        if raw.get("last_attempt_at") and unresolved_total == 0 and raw.get("errors")
        else "review_complete"
        if raw.get("last_attempt_at") and unresolved_total == 0
        else raw_status
    )
    return {
        **empty,
        "status": raw_status,
        "operational_status": operational_status,
        "work_blocked": unresolved_total > 0,
        "last_attempt_at": raw.get("last_attempt_at"),
        "last_full_attempt_at": raw.get("last_full_attempt_at"),
        "last_success_at": raw.get("last_success_at"),
        "next_due_at": raw.get("next_due_at"),
        "run_mode": raw.get("run_mode"),
        "retry_due_at": raw.get("retry_due_at"),
        "retry_pending_count": int(raw.get("retry_pending_count") or 0),
        "retry_exhausted_count": int(raw.get("retry_exhausted_count") or 0),
        "retry_policy": {
            key: (raw.get("retry_policy") or {}).get(key)
            for key in (
                "base_seconds", "max_seconds", "daily_attempt_limit",
                "successful_queries_repeated", "retry_after_respected",
            )
        },
        "items_scanned": int(raw.get("items_scanned") or 0),
        "new_count": int(raw.get("new_count") or 0),
        "paper_unreviewed_count": unreviewed_count,
        "unreviewed_count": unreviewed_count,
        "unresolved_total": unresolved_total,
        "reviewed_count": len(statuses) - unreviewed_count,
        "closed_count": sum(status.startswith("closed_") for status in statuses),
        "formula_pending_count": formula_pending_count,
        "source_contract_pending_count": source_contract_pending_count,
        "source_contract_pending_identities": source_contract_pending_identities,
        "source": raw.get("source") or {},
        "sources": list(raw.get("sources") or []),
        "input_snapshot_results": [
            {
                key: item.get(key) for key in (
                    "status", "fetched_at", "attempted_at", "source_sha256",
                    "last_month", "monthly_rows", "observation_path",
                    "research_only", "runnable", "production_eligible",
                )
            }
            for item in (raw.get("input_snapshot_results") or [])[:3]
            if isinstance(item, dict)
        ],
        "repository_snapshot_results": [
            {
                key: item.get(key) for key in (
                    "repository_id", "repository", "path", "source_url",
                    "commit_url", "current_commit_sha", "prior_commit_sha",
                    "committed_at", "checked_at", "change_status",
                    "review_required", "research_only", "runnable",
                    "candidate_registration_allowed", "factor_values_computed",
                    "outcomes_read", "production_eligible",
                )
            }
            for item in (raw.get("repository_snapshot_results") or [])[:5]
            if isinstance(item, dict)
        ],
        "repository_status": str(raw.get("repository_status") or (
            "ok" if (
                repository_expected_checks > 0
                and repository_successful_checks == repository_expected_checks
            ) else "degraded" if int(
                raw.get("repository_successful_checks") or 0
            ) > 0 else "not_checked"
        )),
        "repository_successful_checks": repository_successful_checks,
        "repository_expected_checks": repository_expected_checks,
        "repository_review_required_count": repository_review_required_count,
        "repository_discovery_status": str(
            raw.get("repository_discovery_status") or "not_checked"
        ),
        "repository_discovery_successful_queries": int(
            raw.get("repository_discovery_successful_queries") or 0
        ),
        "repository_discovery_expected_queries": int(
            raw.get("repository_discovery_expected_queries") or 0
        ),
        "repository_discovery_new_count": int(
            raw.get("repository_discovery_new_count") or 0
        ),
        "repository_discovery_unreviewed_count": (
            repository_discovery_unreviewed_count
        ),
        "repository_discovery_reviewed_count": sum(
            str(item.get("status") or "").startswith(("reviewed_", "closed_"))
            for item in raw.get("repository_discoveries") or []
            if isinstance(item, dict)
        ),
        "repository_discovery_closed_count": sum(
            str(item.get("status") or "").startswith("closed_")
            for item in raw.get("repository_discoveries") or []
            if isinstance(item, dict)
        ),
        "repository_discovery_screened_out_count": int(
            raw.get("repository_discovery_screened_out_count") or 0
        ),
        "repository_discoveries": [
            {
                key: item.get(key) for key in (
                    "scout_id", "identity", "repository", "name", "url",
                    "license_spdx", "stars", "forks", "updated_at", "pushed_at",
                    "status", "license_audit_status", "formula_audit_status",
                    "data_provenance_status", "pit_feasibility_status",
                    "research_only", "runnable", "factor_values_computed",
                    "outcomes_read", "production_eligible",
                )
            }
            for item in (raw.get("repository_discoveries") or [])[:5]
            if isinstance(item, dict)
        ],
        "source_review_queue": source_review_queue,
        "candidate_draft_pipeline": draft_pipeline,
        "preview": preview,
        "errors": list(raw.get("errors") or [])[:3],
    }


def _read_sealed_report(path: Path):
    from research.pb01_pipeline import _object
    with path.open("rb") as stream:
        raw = stream.read(4_000_001)
    if len(raw) > 4_000_000:
        raise ValueError("report_too_large")
    def nonfinite(_):
        raise ValueError("report_nonfinite")
    report = json.loads(raw, object_pairs_hook=_object, parse_constant=nonfinite)
    if not isinstance(report, dict):
        raise ValueError("report_root_invalid")
    for key in ("splits", "pooled_metrics", "pooled", "pooled_conditions"):
        if report.get(key) is not None and not isinstance(report[key], dict):
            raise ValueError("report_mapping_invalid")
    mappings = [report.get("pooled_metrics") or {}, report.get("pooled") or {}]
    for value in (report.get("splits") or {}).values():
        if not isinstance(value, dict):
            raise ValueError("report_split_invalid")
        for key in ("metrics", "conditions"):
            if value.get(key) is not None and not isinstance(value[key], dict):
                raise ValueError("report_split_mapping_invalid")
        if value.get("failed_gates") is not None and not isinstance(value["failed_gates"], list):
            raise ValueError("report_failed_gates_invalid")
        mappings.append(value.get("metrics") or {})
    if report.get("pooled_failed_gates") is not None and not isinstance(report["pooled_failed_gates"], list):
        raise ValueError("report_pooled_gates_invalid")
    for value in mappings:
        if value.get("bootstrap") is not None and not isinstance(value["bootstrap"], dict):
            raise ValueError("report_bootstrap_invalid")
    return report, hashlib.sha256(raw).hexdigest()


def _recent_sealed_research(reports_dir: Path | None = None) -> dict:
    """Expose current sealed manual research without mutating the factor registry."""
    directory = reports_dir or (_PROJECT_ROOT / "reports")
    rows = []
    missing = unreadable = without_outcomes = 0
    for display_name, filename in _SEALED_RESEARCH_REPORTS:
        path = directory / filename
        try:
            report, report_sha = _read_sealed_report(path)
        except FileNotFoundError:
            missing += 1
            continue
        except (OSError, ValueError):
            unreadable += 1
            continue
        outcome_read = (
            report.get("future_outcome_read") is True
            or report.get("return_data_read") is True
        )
        if not outcome_read:
            without_outcomes += 1
            continue
        splits = report.get("splits") or {}
        split_name = next(
            (name for name in (
                "untouched_confirmation", "confirmation", "observed_test",
                "validation",
            ) if isinstance(splits.get(name), dict)),
            None,
        )
        split = splits.get(split_name) if split_name else {}
        metrics = (split or {}).get("metrics") or {}
        pooled_metrics = report.get("pooled_metrics") or report.get("pooled") or {}
        net_spread = next((
            metrics.get(key) for key in (
                "mean_top30_minus_bottom30_after_full_base_cost",
                "mean_d10_minus_d1_after_full_base_cost",
                "mean_young_minus_old_after_turnover_base_cost",
                "mean_high_minus_low_after_full_base_cost",
                "mean_low_minus_high_after_full_base_cost",
                "mean_high_minus_low_after_turnover_base_cost",
                "mean_high_minus_low_after_base_cost",
                "mean_decile_spread_after_base_cost",
                "mean_net_base_spread",
            ) if metrics.get(key) is not None
        ), None)
        failed_gates = []
        for split_value in splits.values():
            if not isinstance(split_value, dict):
                continue
            explicit = split_value.get("failed_gates") or []
            failed_gates.extend(explicit)
            if not explicit:
                failed_gates.extend(
                    key for key, value in (split_value.get("conditions") or {}).items()
                    if value is False
                )
        if not failed_gates:
            failed_gates = list(report.get("pooled_failed_gates") or []) or [
                key for key, value in (report.get("pooled_conditions") or {}).items()
                if value is False
            ]
        failed_split_count = sum(
            isinstance(split_value, dict) and split_value.get("passed") is False
            for split_value in splits.values()
        )
        rows.append({
            "display_name": display_name,
            "candidate_id": report.get("candidate_id"),
            "status": report.get("status"),
            "generated_at": report.get("generated_at"),
            "future_outcome_read": outcome_read,
            "historical_factor_gate_passed": (
                report.get("historical_factor_gate_passed") is True
            ),
            "historical_factor_gate_evaluated": (
                report.get("historical_factor_gate_evaluated") is True
                or ("historical_factor_gate_evaluated" not in report
                    and type(report.get("historical_factor_gate_passed")) is bool)
            ),
            "candidate_performance_metrics_computed": (
                report.get("candidate_performance_metrics_computed") is True
                or ("candidate_performance_metrics_computed" not in report and bool(metrics))
            ),
            "reported_production_allowed": report.get("production_allowed") is True,
            "production_allowed": False,
            "production_eligible": False,
            "generates_alert": False,
            "report_path": f"reports/{filename}",
            "report_sha256": report_sha,
            "terminal_split": split_name,
            "metrics": {
                "sample_count": (
                    metrics.get("dates")
                    if metrics.get("dates") is not None else metrics.get("months")
                ),
                "sample_unit": (
                    "交易日" if metrics.get("dates") is not None else "月"
                ),
                "mean_rank_ic": metrics.get("mean_rank_ic"),
                "net_spread_after_full_base_cost": net_spread,
                "bootstrap_95pct_lower_net_base_spread": metrics.get(
                    "bootstrap_95pct_lower_net_base_spread"
                ) if metrics.get("bootstrap_95pct_lower_net_base_spread") is not None
                else (
                    pooled_metrics.get("bootstrap_95pct_lower_net_base_spread")
                    if pooled_metrics.get("bootstrap_95pct_lower_net_base_spread") is not None
                    else (
                        metrics.get("bootstrap_95pct_lower_long_excess")
                        if metrics.get("bootstrap_95pct_lower_long_excess") is not None
                        else (pooled_metrics.get("bootstrap") or {}).get("lower_95")
                    )
                ),
                "failed_gate_count": len(failed_gates),
                "failed_split_count": failed_split_count,
            },
        })
    rows.sort(key=lambda row: str(row.get("generated_at") or ""), reverse=True)
    return {
        "status": ("partial" if missing or unreadable else "current_sealed_research_available") if rows
                  else "unreadable" if unreadable else "files_missing" if missing else "no_outcomes",
        "listed_count": len(_SEALED_RESEARCH_REPORTS),
        "missing_count": missing, "unreadable_count": unreadable,
        "without_outcomes_count": without_outcomes,
        "count": len(rows),
        "latest_at": rows[0].get("generated_at") if rows else None,
        "items": rows,
        "automatic_mining_running": False,
        "production_eligible": False, "generates_alert": False, "automatic_orders": False,
        "note": "仅展示可读取的历史报告；文件缺失或损坏不代表没有研究，历史结论不授予当前交易资格。",
    }


def _external_research_status(path: Path | None = None) -> dict:
    """Return the public, secret-free health of optional remote research lanes."""
    source = path or _EXTERNAL_RESEARCH_STATUS_PATH
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("status root must be an object")
    except (OSError, ValueError, json.JSONDecodeError):
        return {
            "available": False,
            "status": "unknown",
            "operational_health": "degraded",
            "degraded": True,
            "external_paid_api_disabled": EXTERNAL_PAID_API_DISABLED,
            "heartbeat_at": None,
            "message": "外部研究监督器状态不可用；核心因子与本地确定性研究继续。",
            "blockers": [{
                "code": "external_research_status_unavailable",
                "component": "external_research_supervisor",
                "message": "外部研究监督器状态不可用；核心因子与本地确定性研究继续。",
                "local_deterministic_work_continues": True,
            }],
            "retry_policy": {},
        }

    lifecycle = str(raw.get("status") or "unknown")
    health = str(raw.get("operational_health") or "unknown")
    paid_api_disabled = EXTERNAL_PAID_API_DISABLED
    degraded = bool(raw.get("degraded")) or health == "degraded"
    blockers = []
    for item in raw.get("blockers") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "")
        if code == "deepseek_authentication_failed":
            blockers.append({
                "code": code,
                "component": "deepseek_remote_inference",
                "message": (
                    "DeepSeek 远程研究鉴权失败，自动重试已熔断；"
                    "核心因子与本地确定性研究继续。"
                ),
                "local_deterministic_work_continues": True,
            })
        elif code:
            blockers.append({
                "code": "external_research_degraded",
                "component": "external_research_supervisor",
                "message": "外部研究能力已降级；核心因子与本地确定性研究继续。",
                "local_deterministic_work_continues": True,
            })
    if degraded and not blockers:
        blockers.append({
            "code": "external_research_degraded",
            "component": "external_research_supervisor",
            "message": "外部研究能力已降级；核心因子与本地确定性研究继续。",
            "local_deterministic_work_continues": True,
        })

    raw_deepseek_policy = (raw.get("retry_policy") or {}).get("deepseek") or {}
    deepseek_policy = {}
    if isinstance(raw_deepseek_policy, dict):
        for key in (
            "mode", "remote_requests_allowed", "automatic_retry",
            "next_retry_at", "retry_condition", "local_deterministic_work",
        ):
            if key in raw_deepseek_policy:
                deepseek_policy[key] = raw_deepseek_policy[key]
    return {
        "available": True,
        "status": lifecycle,
        "operational_health": "degraded" if degraded else health,
        "degraded": degraded,
        "external_paid_api_disabled": paid_api_disabled,
        "heartbeat_at": raw.get("heartbeat_at"),
        "message": (
            "外部研究能力已降级；核心因子与本地确定性研究继续。"
            if degraded else (
                "外部付费 API 已禁用；本地确定性研究继续。"
                if paid_api_disabled else "外部研究监督器在线。"
            )
        ),
        "blockers": blockers,
        "retry_policy": {"deepseek": deepseek_policy} if deepseek_policy else {},
    }


def _active_shareholder_input_research(
    *,
    protocol_path: Path | None = None,
    checkpoint_path: Path | None = None,
    v292_report_path: Path | None = None,
    v293_report_path: Path | None = None,
    final_result_path: Path | None = None,
) -> dict:
    """Expose only genuinely in-flight PIT input work, never a sealed candidate."""
    protocol_path = protocol_path or _V292_PROTOCOL_PATH
    checkpoint_path = checkpoint_path or _V292_CHECKPOINT_PATH
    v292_report_path = v292_report_path or _V292_REPORT_PATH
    v293_report_path = v293_report_path or _V293_REPORT_PATH
    final_result_path = final_result_path or (
        _PROJECT_ROOT / "reports"
        / "v296_cninfo_shareholder_count_change_blind_factor_test.json"
    )
    try:
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        expected = int(protocol["gates"]["exact_target_code_years"])
        candidate_id = str(protocol["candidate_id"])
        builder_path = _PROJECT_ROOT / protocol["input_binding"]["builder"]
        expected_identity = {
            "protocol_sha256": _file_sha256(protocol_path),
            "builder_sha256": str(protocol["input_binding"]["builder_sha256"]),
            "source_manifest_sha256": str(
                protocol["input_binding"]["source_manifest_sha256"]
            ),
            "source_records_sha256": str(
                protocol["input_binding"]["source_records_sha256"]
            ),
            "v291_report_sha256": str(
                protocol["input_binding"]["v291_report_sha256"]
            ),
        }
        builder_current = _file_sha256(builder_path)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return {
            "available": False,
            "status": "input_research_unavailable",
            "candidate_registered": False,
            "runnable": False,
            "factor_values_computed": False,
            "future_outcomes_read": False,
        }

    if final_result_path.is_file():
        try:
            final_result = json.loads(final_result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            final_result = {}
        final_status = str(final_result.get("status") or "final_result_invalid")
        terminal = bool(
            final_status.startswith("permanently_rejected_")
            and final_result.get("candidate_formula_permanently_sealed") is True
            and final_result.get("production_allowed") is False
        )
        return {
            "available": True,
            "active": False,
            "terminal": terminal,
            "status": final_status,
            "candidate_id": final_result.get("candidate_id") or candidate_id,
            "input_candidate_id": candidate_id,
            "input_gate_passed": True,
            "candidate_registered": False,
            "runnable": False,
            "factor_values_computed": bool(final_result),
            "future_outcomes_read": final_result.get("future_outcome_read") is True,
            "historical_factor_gate_passed": final_result.get(
                "historical_factor_gate_passed"
            ) is True,
            "production_allowed": final_result.get("production_allowed") is True,
            "report_path": str(final_result_path.relative_to(_PROJECT_ROOT)),
            "report_sha256": _file_sha256(final_result_path),
            "next_action": final_result.get("next_action") or (
                "Keep the input and blind result in the archive; no active task."
            ),
        }

    if v293_report_path.is_file():
        try:
            report = json.loads(v293_report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            report = {}
        status = str(report.get("status") or "summary_repair_report_invalid")
        return {
            "available": True,
            "active": False,
            "status": status,
            "candidate_id": candidate_id,
            "completed": int(report.get("final_records") or 0),
            "targets": expected,
            "progress_pct": round(
                int(report.get("final_records") or 0) / expected * 100, 2
            ),
            "input_gate_passed": status
            == "full_market_input_epoch_accepted_not_factor_or_strategy",
            "candidate_registered": False,
            "runnable": False,
            "factor_values_computed": False,
            "future_outcomes_read": False,
            "report_path": str(v293_report_path.relative_to(_PROJECT_ROOT)),
            "report_sha256": _file_sha256(v293_report_path),
        }

    if v292_report_path.is_file():
        try:
            report = json.loads(v292_report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            report = {}
        return {
            "available": True,
            "active": True,
            "status": "primary_input_epoch_complete_summary_repair_pending",
            "candidate_id": candidate_id,
            "completed": int(report.get("targets") or expected),
            "targets": expected,
            "progress_pct": 100.0,
            "primary_accepted": int(report.get("accepted_records") or 0),
            "primary_excluded": int(report.get("excluded_records") or 0),
            "input_gate_passed": False,
            "candidate_registered": False,
            "runnable": False,
            "factor_values_computed": False,
            "future_outcomes_read": False,
            "report_path": str(v292_report_path.relative_to(_PROJECT_ROOT)),
            "report_sha256": _file_sha256(v292_report_path),
        }

    if not checkpoint_path.is_file():
        return {
            "available": False,
            "active": False,
            "status": "input_research_not_started",
            "candidate_id": candidate_id,
            "candidate_registered": False,
            "runnable": False,
            "factor_values_computed": False,
            "future_outcomes_read": False,
        }
    try:
        connection = sqlite3.connect(
            f"file:{checkpoint_path}?mode=ro", uri=True, timeout=0.5
        )
        try:
            identity_row = connection.execute(
                "SELECT value FROM meta WHERE key='identity'"
            ).fetchone()
            completed = int(connection.execute(
                "SELECT COUNT(*) FROM results"
            ).fetchone()[0])
            accepted = int(connection.execute(
                "SELECT COUNT(*) FROM results "
                "WHERE json_extract(payload,'$.status')='accepted'"
            ).fetchone()[0])
        finally:
            connection.close()
        checkpoint_identity = json.loads(identity_row[0]) if identity_row else {}
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        return {
            "available": False,
            "active": False,
            "status": "input_checkpoint_unreadable",
            "candidate_id": candidate_id,
            "candidate_registered": False,
            "runnable": False,
            "factor_values_computed": False,
            "future_outcomes_read": False,
        }
    identity_valid = bool(
        builder_current == expected_identity["builder_sha256"]
        and checkpoint_identity == expected_identity
    )
    timestamp_paths = [checkpoint_path, Path(str(checkpoint_path) + "-wal")]
    updated_timestamp = max(
        path.stat().st_mtime for path in timestamp_paths if path.is_file()
    )
    updated_at = datetime.fromtimestamp(updated_timestamp).isoformat(
        timespec="seconds"
    )
    return {
        "available": True,
        "active": True,
        "status": (
            "input_epoch_building" if completed < expected
            else "input_epoch_finalizing"
        ) if identity_valid else "input_epoch_identity_mismatch",
        "candidate_id": candidate_id,
        "completed": completed,
        "targets": expected,
        "progress_pct": round(completed / expected * 100, 2),
        "primary_accepted": accepted,
        "primary_excluded": completed - accepted,
        "checkpoint_updated_at": updated_at,
        "identity_valid": identity_valid,
        "input_gate_passed": False,
        "candidate_registered": False,
        "runnable": False,
        "factor_values_computed": False,
        "future_outcomes_read": False,
        "forbidden_until_input_acceptance": list(
            protocol.get("forbidden_until_epoch_acceptance") or []
        ),
    }


def _active_multiyear_first_filing_input_research(
    *,
    admission_path: Path | None = None,
    ticket_path: Path | None = None,
    receipt_path: Path | None = None,
    checkpoint_path: Path | None = None,
    pause_path: Path | None = None,
    run_path: Path | None = None,
    prior_terminal_path: Path | None = None,
    recovery_adjudication_path: Path | None = None,
    recovery_admission_path: Path | None = None,
    recovery_ticket_path: Path | None = None,
    recovery_receipt_path: Path | None = None,
    recovery_pause_path: Path | None = None,
    recovery_run_path: Path | None = None,
    recovery2_amendment_path: Path | None = None,
    recovery2_admission_path: Path | None = None,
    recovery2_ticket_path: Path | None = None,
    recovery2_receipt_path: Path | None = None,
    recovery2_pause_path: Path | None = None,
    recovery2_run_path: Path | None = None,
    recovery2_terminal_audit_path: Path | None = None,
    recovery2_parser_path: Path | None = None,
    recovery3_amendment_path: Path | None = None,
    recovery3_admission_path: Path | None = None,
    recovery3_ticket_path: Path | None = None,
    recovery3_receipt_path: Path | None = None,
    recovery3_pause_path: Path | None = None,
    recovery3_run_path: Path | None = None,
    recovery3_terminal_audit_path: Path | None = None,
    recovery3_parser_path: Path | None = None,
) -> dict:
    """Read the live official-PDF input epoch without mutating its WAL or files."""
    default_scope = all(path is None for path in (
        admission_path, ticket_path, receipt_path, checkpoint_path, pause_path,
        run_path, prior_terminal_path, recovery_adjudication_path,
        recovery_admission_path, recovery_ticket_path, recovery_receipt_path,
        recovery_pause_path, recovery_run_path, recovery2_amendment_path,
        recovery2_admission_path, recovery2_ticket_path,
        recovery2_receipt_path, recovery2_pause_path, recovery2_run_path,
        recovery2_terminal_audit_path, recovery2_parser_path,
        recovery3_amendment_path, recovery3_admission_path,
        recovery3_ticket_path, recovery3_receipt_path, recovery3_pause_path,
        recovery3_run_path, recovery3_terminal_audit_path,
        recovery3_parser_path,
    ))
    admission_path = admission_path or _FIRST_FILING_V21_ADMISSION_PATH
    ticket_path = ticket_path or _FIRST_FILING_V21_TICKET_PATH
    receipt_path = receipt_path or _FIRST_FILING_V21_RECEIPT_PATH
    checkpoint_path = checkpoint_path or _FIRST_FILING_2024_CHECKPOINT_PATH
    pause_path = pause_path or _FIRST_FILING_V21_PAUSE_PATH
    run_path = run_path or _FIRST_FILING_V21_RUN_PATH
    prior_terminal_path = prior_terminal_path or _FIRST_FILING_2025_TERMINAL_PATH
    if default_scope:
        recovery_adjudication_path = _FIRST_FILING_V21_RECOVERY1_ADJUDICATION_PATH
        recovery_admission_path = _FIRST_FILING_V21_RECOVERY1_ADMISSION_PATH
        recovery_ticket_path = _FIRST_FILING_V21_RECOVERY1_TICKET_PATH
        recovery_receipt_path = _FIRST_FILING_V21_RECOVERY1_RECEIPT_PATH
        recovery_pause_path = _FIRST_FILING_V21_RECOVERY1_PAUSE_PATH
        recovery_run_path = _FIRST_FILING_V21_RECOVERY1_RUN_PATH
        recovery2_amendment_path = _FIRST_FILING_V21_RECOVERY2_AMENDMENT_PATH
        recovery2_admission_path = _FIRST_FILING_V21_RECOVERY2_ADMISSION_PATH
        recovery2_ticket_path = _FIRST_FILING_V21_RECOVERY2_TICKET_PATH
        recovery2_receipt_path = _FIRST_FILING_V21_RECOVERY2_RECEIPT_PATH
        recovery2_pause_path = _FIRST_FILING_V21_RECOVERY2_PAUSE_PATH
        recovery2_run_path = _FIRST_FILING_V21_RECOVERY2_RUN_PATH
        recovery2_terminal_audit_path = (
            _FIRST_FILING_V21_RECOVERY2_TERMINAL_AUDIT_PATH
        )
        recovery2_parser_path = _FIRST_FILING_STATEMENT_PARSER_PATH
        recovery3_amendment_path = _FIRST_FILING_V21_RECOVERY3_AMENDMENT_PATH
        recovery3_admission_path = _FIRST_FILING_V21_RECOVERY3_ADMISSION_PATH
        recovery3_ticket_path = _FIRST_FILING_V21_RECOVERY3_TICKET_PATH
        recovery3_receipt_path = _FIRST_FILING_V21_RECOVERY3_RECEIPT_PATH
        recovery3_pause_path = _FIRST_FILING_V21_RECOVERY3_PAUSE_PATH
        recovery3_run_path = _FIRST_FILING_V21_RECOVERY3_RUN_PATH
        recovery3_terminal_audit_path = (
            _FIRST_FILING_V21_RECOVERY3_TERMINAL_AUDIT_PATH
        )
        recovery3_parser_path = _FIRST_FILING_STATEMENT_PARSER_PATH
    try:
        admission = json.loads(admission_path.read_text(encoding="utf-8"))
        ticket = json.loads(ticket_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        prior = json.loads(prior_terminal_path.read_text(encoding="utf-8"))
        targets = int(admission["scope"]["planned_rows"])
        admission_sha256 = _file_sha256(admission_path)
        ticket_sha256 = _file_sha256(ticket_path)
        identity_errors = []
        if admission.get("status") != "v21_2024_formal_collection_admitted":
            identity_errors.append("admission_status")
        if ticket.get("status") != "frozen_local_execution_ticket":
            identity_errors.append("ticket_status")
        if ticket.get("bindings", {}).get("admission_sha256") != admission_sha256:
            identity_errors.append("ticket_admission_sha256")
        if receipt.get("status") != "v21_2024_formal_execution_ticket_used":
            identity_errors.append("receipt_status")
        if receipt.get("ticket_sha256") != ticket_sha256:
            identity_errors.append("receipt_ticket_sha256")
        if int(ticket.get("scope", {}).get("planned_rows") or -1) != targets:
            identity_errors.append("ticket_scope")
        prior_counts = (prior.get("terminal_checkpoint") or {}).get("counts") or {}
        prior_year_ready = bool(
            prior.get("status") == "v20_formal_collection_terminal_complete"
            and int(prior_counts.get("success") or 0) == 5478
            and int(prior_counts.get("planned") or 0) == 0
            and int(prior_counts.get("failed") or 0) == 0
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return {
            "available": False,
            "active": False,
            "status": "multiyear_official_pit_identity_unavailable",
            "identity_valid": False,
            "factor_values_computed": False,
            "prices_or_returns_read": False,
            "ic_or_backtest_run": False,
            "production_allowed": False,
        }
    recovery = None
    recovery_paths = (
        recovery_adjudication_path, recovery_admission_path,
        recovery_ticket_path, recovery_receipt_path,
    )
    if all(path is not None and path.is_file() for path in recovery_paths):
        try:
            adjudication = json.loads(
                recovery_adjudication_path.read_text(encoding="utf-8")
            )
            recovery_admission = json.loads(
                recovery_admission_path.read_text(encoding="utf-8")
            )
            recovery_ticket = json.loads(
                recovery_ticket_path.read_text(encoding="utf-8")
            )
            recovery_receipt = json.loads(
                recovery_receipt_path.read_text(encoding="utf-8")
            )
            recovery_admission_sha256 = _file_sha256(recovery_admission_path)
            recovery_ticket_sha256 = _file_sha256(recovery_ticket_path)
            recovery_errors = []
            if adjudication.get("status") != (
                "v21_pause_transport_truncation_adjudicated_for_recovery1_only"
            ):
                recovery_errors.append("recovery_adjudication_status")
            if adjudication.get("bindings", {}).get("v21_pause_sha256") != (
                _file_sha256(pause_path)
            ):
                recovery_errors.append("recovery_adjudication_pause_sha256")
            if recovery_admission.get("status") != (
                "v21_recovery1_formal_collection_admitted"
            ):
                recovery_errors.append("recovery_admission_status")
            if recovery_ticket.get("status") != "frozen_local_execution_ticket":
                recovery_errors.append("recovery_ticket_status")
            if recovery_ticket.get("bindings", {}).get(
                "admission_sha256"
            ) != recovery_admission_sha256:
                recovery_errors.append("recovery_ticket_admission_sha256")
            if recovery_receipt.get("status") != (
                "v21_recovery1_formal_execution_ticket_used"
            ):
                recovery_errors.append("recovery_receipt_status")
            if recovery_receipt.get("ticket_sha256") != recovery_ticket_sha256:
                recovery_errors.append("recovery_receipt_ticket_sha256")
            if int(recovery_ticket.get("scope", {}).get("execution_rows") or -1) != (
                int(recovery_admission.get("permissions", {}).get("execution_rows") or -2)
            ):
                recovery_errors.append("recovery_ticket_scope")
            identity_errors.extend(recovery_errors)
            recovery = {
                "admission_sha256": recovery_admission_sha256,
                "ticket_sha256": recovery_ticket_sha256,
                "pause_path": recovery_pause_path,
                "run_path": recovery_run_path,
                "valid": not recovery_errors,
            }
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            identity_errors.append("recovery_identity_unavailable")
    recovery3_amendment = None
    if recovery3_amendment_path is not None and recovery3_amendment_path.is_file():
        try:
            amendment3 = json.loads(
                recovery3_amendment_path.read_text(encoding="utf-8")
            )
            amendment3_errors = []
            amendment3_bindings = amendment3.get("bindings", {})
            if amendment3.get("status") != (
                "recovery2_pause_adjudicated_parser_remediation_"
                "authorized_before_recovery3_admission"
            ):
                amendment3_errors.append("recovery3_amendment_status")
            for key, path in (
                ("recovery2_pause_sha256", recovery2_pause_path),
                ("recovery2_run_sha256", recovery2_run_path),
                ("recovery2_receipt_sha256", recovery2_receipt_path),
                ("recovery2_admission_sha256", recovery2_admission_path),
                ("recovery2_ticket_sha256", recovery2_ticket_path),
            ):
                if (
                    path is None or not path.is_file()
                    or amendment3_bindings.get(key) != _file_sha256(path)
                ):
                    amendment3_errors.append(f"recovery3_amendment_{key}")
            recovery2_admitted_parser_sha256 = None
            if recovery2_admission_path is not None \
                    and recovery2_admission_path.is_file():
                recovery2_admitted_parser_sha256 = json.loads(
                    recovery2_admission_path.read_text(encoding="utf-8")
                ).get("bindings", {}).get("parser_sha256")
            if amendment3_bindings.get(
                "prechange_parser_sha256"
            ) != recovery2_admitted_parser_sha256:
                amendment3_errors.append("recovery3_amendment_prechange_parser_sha256")
            identity_errors.extend(amendment3_errors)
            recovery3_amendment = {
                "sha256": _file_sha256(recovery3_amendment_path),
                "valid": not amendment3_errors,
            }
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            identity_errors.append("recovery3_amendment_unavailable")
            recovery3_amendment = {"valid": False}
    recovery2 = None
    recovery2_started = any(
        path is not None and path.is_file()
        for path in (
            recovery2_receipt_path, recovery2_pause_path, recovery2_run_path,
            recovery2_terminal_audit_path,
        )
    )
    recovery2_paths = (
        recovery2_amendment_path, recovery2_admission_path,
        recovery2_ticket_path, recovery2_receipt_path, recovery2_parser_path,
    )
    if recovery2_started:
        if not all(path is not None and path.is_file() for path in recovery2_paths):
            identity_errors.append("recovery2_identity_incomplete")
            recovery2 = {"valid": False}
        else:
            try:
                amendment = json.loads(
                    recovery2_amendment_path.read_text(encoding="utf-8")
                )
                recovery2_admission = json.loads(
                    recovery2_admission_path.read_text(encoding="utf-8")
                )
                recovery2_ticket = json.loads(
                    recovery2_ticket_path.read_text(encoding="utf-8")
                )
                recovery2_receipt = json.loads(
                    recovery2_receipt_path.read_text(encoding="utf-8")
                )
                recovery2_admission_sha256 = _file_sha256(recovery2_admission_path)
                recovery2_ticket_sha256 = _file_sha256(recovery2_ticket_path)
                recovery2_live_parser_sha256 = _file_sha256(recovery2_parser_path)
                recovery2_parser_sha256 = recovery2_admission.get(
                    "bindings", {}
                ).get("parser_sha256")
                recovery2_errors = []
                amendment_bindings = amendment.get("bindings", {})
                if not recovery or not recovery["valid"]:
                    recovery2_errors.append("recovery2_requires_valid_recovery1")
                if amendment.get("status") != (
                    "parser_remediation_authorized_after_recovery1_pause_"
                    "before_recovery2_admission"
                ):
                    recovery2_errors.append("recovery2_amendment_status")
                for key, path in (
                    ("recovery1_pause_sha256", recovery_pause_path),
                    ("recovery1_run_sha256", recovery_run_path),
                    ("recovery1_receipt_sha256", recovery_receipt_path),
                    ("recovery1_admission_sha256", recovery_admission_path),
                    ("recovery1_ticket_sha256", recovery_ticket_path),
                ):
                    if (
                        path is None or not path.is_file()
                        or amendment_bindings.get(key) != _file_sha256(path)
                    ):
                        recovery2_errors.append(f"recovery2_amendment_{key}")
                if recovery2_admission.get("status") != (
                    "v21_recovery2_formal_collection_admitted"
                ):
                    recovery2_errors.append("recovery2_admission_status")
                if recovery2_admission.get("bindings", {}).get(
                    "amendment_sha256"
                ) != _file_sha256(recovery2_amendment_path):
                    recovery2_errors.append("recovery2_admission_amendment_sha256")
                if not recovery2_parser_sha256:
                    recovery2_errors.append("recovery2_admission_parser_sha256")
                elif (
                    recovery2_live_parser_sha256 != recovery2_parser_sha256
                    and not (
                        recovery3_amendment
                        and recovery3_amendment["valid"]
                    )
                ):
                    recovery2_errors.append("recovery2_admission_parser_sha256")
                if recovery2_ticket.get("status") != "frozen_local_execution_ticket":
                    recovery2_errors.append("recovery2_ticket_status")
                if recovery2_ticket.get("bindings", {}).get(
                    "admission_sha256"
                ) != recovery2_admission_sha256:
                    recovery2_errors.append("recovery2_ticket_admission_sha256")
                if recovery2_receipt.get("status") != (
                    "v21_recovery2_formal_execution_ticket_used"
                ):
                    recovery2_errors.append("recovery2_receipt_status")
                if recovery2_receipt.get("ticket_sha256") != recovery2_ticket_sha256:
                    recovery2_errors.append("recovery2_receipt_ticket_sha256")
                execution_nonce = recovery2_ticket.get("bindings", {}).get(
                    "execution_nonce"
                )
                if execution_nonce and recovery2_receipt.get(
                    "execution_nonce"
                ) != execution_nonce:
                    recovery2_errors.append("recovery2_receipt_execution_nonce")
                if int(recovery2_ticket.get("scope", {}).get(
                    "execution_rows"
                ) or -1) != int(recovery2_admission.get("permissions", {}).get(
                    "execution_rows"
                ) or -2):
                    recovery2_errors.append("recovery2_ticket_scope")
                retry = recovery2_admission.get("permissions", {}).get(
                    "retry_exact_failed_row_once", {}
                )
                if (
                    retry.get("issuer_code") != "600196"
                    or int(retry.get("prior_attempts") or -1) != 1
                    or int(retry.get("maximum_new_outer_attempts") or -1) != 1
                ):
                    recovery2_errors.append("recovery2_retry_scope")
                identity_errors.extend(recovery2_errors)
                recovery2 = {
                    "admission_sha256": recovery2_admission_sha256,
                    "ticket_sha256": recovery2_ticket_sha256,
                    "parser_sha256": recovery2_parser_sha256,
                    "pause_path": recovery2_pause_path,
                    "run_path": recovery2_run_path,
                    "terminal_audit_path": recovery2_terminal_audit_path,
                    "valid": not recovery2_errors,
                }
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                identity_errors.append("recovery2_identity_unavailable")
                recovery2 = {"valid": False}
    recovery3 = None
    recovery3_started = any(
        path is not None and path.is_file()
        for path in (
            recovery3_receipt_path, recovery3_pause_path, recovery3_run_path,
            recovery3_terminal_audit_path,
        )
    )
    recovery3_paths = (
        recovery3_amendment_path, recovery3_admission_path,
        recovery3_ticket_path, recovery3_receipt_path, recovery3_parser_path,
    )
    if recovery3_started:
        if not all(path is not None and path.is_file() for path in recovery3_paths):
            identity_errors.append("recovery3_identity_incomplete")
            recovery3 = {"valid": False}
        else:
            try:
                recovery3_admission = json.loads(
                    recovery3_admission_path.read_text(encoding="utf-8")
                )
                recovery3_ticket = json.loads(
                    recovery3_ticket_path.read_text(encoding="utf-8")
                )
                recovery3_receipt = json.loads(
                    recovery3_receipt_path.read_text(encoding="utf-8")
                )
                recovery3_admission_sha256 = _file_sha256(
                    recovery3_admission_path
                )
                recovery3_ticket_sha256 = _file_sha256(recovery3_ticket_path)
                recovery3_parser_sha256 = _file_sha256(recovery3_parser_path)
                recovery3_errors = []
                if not recovery2 or not recovery2["valid"]:
                    recovery3_errors.append("recovery3_requires_valid_recovery2")
                if not recovery3_amendment or not recovery3_amendment["valid"]:
                    recovery3_errors.append("recovery3_requires_valid_amendment")
                if recovery3_admission.get("status") != (
                    "v21_recovery3_formal_collection_admitted"
                ):
                    recovery3_errors.append("recovery3_admission_status")
                admission3_bindings = recovery3_admission.get("bindings", {})
                if admission3_bindings.get("amendment_sha256") != (
                    recovery3_amendment["sha256"]
                    if recovery3_amendment and recovery3_amendment["valid"]
                    else None
                ):
                    recovery3_errors.append("recovery3_admission_amendment_sha256")
                if admission3_bindings.get(
                    "parser_sha256"
                ) != recovery3_parser_sha256:
                    recovery3_errors.append("recovery3_admission_parser_sha256")
                if recovery3_ticket.get("status") != "frozen_local_execution_ticket":
                    recovery3_errors.append("recovery3_ticket_status")
                if recovery3_ticket.get("bindings", {}).get(
                    "admission_sha256"
                ) != recovery3_admission_sha256:
                    recovery3_errors.append("recovery3_ticket_admission_sha256")
                if recovery3_receipt.get("status") != (
                    "v21_recovery3_formal_execution_ticket_used"
                ):
                    recovery3_errors.append("recovery3_receipt_status")
                if recovery3_receipt.get(
                    "ticket_sha256"
                ) != recovery3_ticket_sha256:
                    recovery3_errors.append("recovery3_receipt_ticket_sha256")
                execution_nonce = recovery3_ticket.get("bindings", {}).get(
                    "execution_nonce"
                )
                if not execution_nonce or recovery3_receipt.get(
                    "execution_nonce"
                ) != execution_nonce:
                    recovery3_errors.append("recovery3_receipt_execution_nonce")
                if int(recovery3_ticket.get("scope", {}).get(
                    "execution_rows"
                ) or -1) != int(recovery3_admission.get("permissions", {}).get(
                    "execution_rows"
                ) or -2):
                    recovery3_errors.append("recovery3_ticket_scope")
                repair = recovery3_admission.get("repair", {})
                if (
                    repair.get("issuer_code") != "600884"
                    or repair.get("pdf_sha256") != (
                        "d97ea19a85fe04468519271ff24abe311cd595fa1bb6cfc7f1b4ddff852f9988"
                    )
                    or repair.get("marker_path") != (
                        "post_collection_remediations.side_by_side_shifted_total_v1"
                    )
                    or repair.get("attempts_increment") != 0
                    or repair.get("preserve_collection_provenance") is not True
                ):
                    recovery3_errors.append("recovery3_repair_scope")
                identity_errors.extend(recovery3_errors)
                recovery3 = {
                    "admission_sha256": recovery3_admission_sha256,
                    "ticket_sha256": recovery3_ticket_sha256,
                    "parser_sha256": recovery3_parser_sha256,
                    "amendment_sha256": recovery3_amendment.get("sha256"),
                    "repair_pdf_sha256": repair.get("pdf_sha256"),
                    "pause_path": recovery3_pause_path,
                    "run_path": recovery3_run_path,
                    "terminal_audit_path": recovery3_terminal_audit_path,
                    "valid": not recovery3_errors,
                }
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                identity_errors.append("recovery3_identity_unavailable")
                recovery3 = {"valid": False}
    if not checkpoint_path.is_file():
        return {
            "available": True,
            "active": False,
            "status": "2024_official_pit_collection_not_started",
            "targets": targets,
            "completed": 0,
            "progress_pct": 0.0,
            "identity_valid": not identity_errors,
            "identity_errors": identity_errors,
            "prior_2025_success": 5478 if prior_year_ready else 0,
            "factor_values_computed": False,
            "prices_or_returns_read": False,
            "ic_or_backtest_run": False,
            "production_allowed": False,
        }
    try:
        with closing(sqlite3.connect(
            f"file:{checkpoint_path.resolve()}?mode=ro", uri=True, timeout=0.5,
        )) as connection:
            rows = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
            counts = {
                str(status): int(count)
                for status, count in connection.execute(
                    "SELECT status,COUNT(*) FROM jobs GROUP BY status"
                )
            }
            remediation_marker_mismatch = 0
            if recovery3 and recovery3["valid"]:
                repeated_attempts = int(connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE attempts>1 AND NOT ("
                    "(issuer_code='301041' AND attempts=2) OR "
                    "(issuer_code='600196' AND attempts=2))"
                ).fetchone()[0])
            elif recovery2 and recovery2["valid"]:
                repeated_attempts = int(connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE attempts>1 AND NOT ("
                    "(issuer_code='301041' AND attempts=2) OR "
                    "(issuer_code='600196' AND attempts=2))"
                ).fetchone()[0])
            elif recovery and recovery["valid"]:
                repeated_attempts = int(connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE attempts>1 AND NOT ("
                    "issuer_code='301041' AND attempts=2)"
                ).fetchone()[0])
            else:
                repeated_attempts = int(connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE attempts>1"
                ).fetchone()[0])
            latest_updated_at = connection.execute(
                "SELECT MAX(updated_at) FROM jobs"
            ).fetchone()[0]
            if recovery3 and recovery3["valid"]:
                mismatched_results = int(connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status='success' AND NOT ("
                    "(attempts=1 AND (("
                    "COALESCE(json_extract(result_json,'$.collection_execution_ticket_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.collection_admission_sha256'),'')=?) OR ("
                    "COALESCE(json_extract(result_json,'$.collection_execution_ticket_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.collection_admission_sha256'),'')=?) OR ("
                    "COALESCE(json_extract(result_json,'$.collection_execution_ticket_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.collection_admission_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.collection_parser_sha256'),'')=?) OR ("
                    "COALESCE(json_extract(result_json,'$.collection_execution_ticket_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.collection_admission_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.collection_parser_sha256'),'')=?))) OR "
                    "(issuer_code='301041' AND attempts=2 AND "
                    "COALESCE(json_extract(result_json,'$.collection_execution_ticket_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.collection_admission_sha256'),'')=?) OR "
                    "(issuer_code='600196' AND attempts=2 AND "
                    "COALESCE(json_extract(result_json,'$.collection_execution_ticket_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.collection_admission_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.collection_parser_sha256'),'')=?))",
                    (
                        ticket_sha256, admission_sha256,
                        recovery["ticket_sha256"], recovery["admission_sha256"],
                        recovery2["ticket_sha256"], recovery2["admission_sha256"],
                        recovery2["parser_sha256"],
                        recovery3["ticket_sha256"], recovery3["admission_sha256"],
                        recovery3["parser_sha256"],
                        recovery["ticket_sha256"], recovery["admission_sha256"],
                        recovery2["ticket_sha256"], recovery2["admission_sha256"],
                        recovery2["parser_sha256"],
                    ),
                ).fetchone()[0])
                remediation_marker_mismatch = int(connection.execute(
                    "SELECT CASE WHEN COUNT(*)=1 AND COALESCE(SUM(CASE WHEN "
                    "status='success' AND attempts=1 AND "
                    "COALESCE(json_extract(result_json,'$.collection_execution_ticket_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.collection_admission_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.collection_parser_sha256'),'')=? AND "
                    "json_type(result_json,'$.post_collection_remediations.side_by_side_shifted_total_v1')='object' AND "
                    "COALESCE(json_extract(result_json,'$.post_collection_remediations.side_by_side_shifted_total_v1.amendment_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.post_collection_remediations.side_by_side_shifted_total_v1.admission_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.post_collection_remediations.side_by_side_shifted_total_v1.ticket_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.post_collection_remediations.side_by_side_shifted_total_v1.parser_after_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.post_collection_remediations.side_by_side_shifted_total_v1.source_pdf_sha256'),'')=? "
                    "THEN 1 ELSE 0 END),0)=1 THEN 0 ELSE 1 END "
                    "FROM jobs WHERE issuer_code='600884'",
                    (
                        recovery2["ticket_sha256"], recovery2["admission_sha256"],
                        recovery2["parser_sha256"], recovery3["amendment_sha256"],
                        recovery3["admission_sha256"], recovery3["ticket_sha256"],
                        recovery3["parser_sha256"], recovery3["repair_pdf_sha256"],
                    ),
                ).fetchone()[0])
            elif recovery2 and recovery2["valid"]:
                mismatched_results = int(connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status='success' AND NOT ("
                    "(attempts=1 AND (("
                    "COALESCE(json_extract(result_json,'$.collection_execution_ticket_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.collection_admission_sha256'),'')=?) OR ("
                    "COALESCE(json_extract(result_json,'$.collection_execution_ticket_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.collection_admission_sha256'),'')=?) OR ("
                    "COALESCE(json_extract(result_json,'$.collection_execution_ticket_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.collection_admission_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.collection_parser_sha256'),'')=?))) OR "
                    "(issuer_code='301041' AND attempts=2 AND "
                    "COALESCE(json_extract(result_json,'$.collection_execution_ticket_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.collection_admission_sha256'),'')=?) OR "
                    "(issuer_code='600196' AND attempts=2 AND "
                    "COALESCE(json_extract(result_json,'$.collection_execution_ticket_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.collection_admission_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.collection_parser_sha256'),'')=?))",
                    (
                        ticket_sha256, admission_sha256,
                        recovery["ticket_sha256"], recovery["admission_sha256"],
                        recovery2["ticket_sha256"], recovery2["admission_sha256"],
                        recovery2["parser_sha256"],
                        recovery["ticket_sha256"], recovery["admission_sha256"],
                        recovery2["ticket_sha256"], recovery2["admission_sha256"],
                        recovery2["parser_sha256"],
                    ),
                ).fetchone()[0])
            elif recovery and recovery["valid"]:
                mismatched_results = int(connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status='success' AND NOT ("
                    "(attempts=1 AND (("
                    "COALESCE(json_extract(result_json,'$.collection_execution_ticket_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.collection_admission_sha256'),'')=?) OR ("
                    "COALESCE(json_extract(result_json,'$.collection_execution_ticket_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.collection_admission_sha256'),'')=?))) OR "
                    "(issuer_code='301041' AND attempts=2 AND "
                    "COALESCE(json_extract(result_json,'$.collection_execution_ticket_sha256'),'')=? AND "
                    "COALESCE(json_extract(result_json,'$.collection_admission_sha256'),'')=?))",
                    (
                        ticket_sha256, admission_sha256,
                        recovery["ticket_sha256"], recovery["admission_sha256"],
                        recovery["ticket_sha256"], recovery["admission_sha256"],
                    ),
                ).fetchone()[0])
            else:
                mismatched_results = int(connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status='success' AND ("
                    "COALESCE(json_extract(result_json,'$.collection_execution_ticket_sha256'),'')!=? OR "
                    "COALESCE(json_extract(result_json,'$.collection_admission_sha256'),'')!=?)",
                    (ticket_sha256, admission_sha256),
                ).fetchone()[0])
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return {
            "available": False,
            "active": False,
            "status": "2024_official_pit_checkpoint_unreadable",
            "identity_valid": False,
            "factor_values_computed": False,
            "prices_or_returns_read": False,
            "ic_or_backtest_run": False,
            "production_allowed": False,
        }
    if rows != targets:
        identity_errors.append("checkpoint_scope")
    if repeated_attempts:
        identity_errors.append("repeated_outer_attempt")
    if mismatched_results:
        identity_errors.append("stored_result_binding")
    if remediation_marker_mismatch:
        identity_errors.append("recovery3_600884_remediation_binding")
    completed = int(counts.get("success", 0)) + int(counts.get("failed", 0))
    active_recovery = (
        recovery3 if recovery3 and recovery3["valid"]
        else recovery2 if recovery2 and recovery2["valid"]
        else recovery if recovery and recovery["valid"]
        else None
    )
    recovery3_active = recovery3 is not None and active_recovery is recovery3
    recovery2_active = recovery2 is not None and active_recovery is recovery2
    collection_generation = (
        "v21_recovery3" if recovery3_active
        else "v21_recovery2" if recovery2_active
        else "v21_recovery1" if recovery is not None and active_recovery is recovery
        else "v21"
    )
    remediation_pending = bool(
        recovery3_amendment and recovery3_amendment["valid"]
        and not recovery3_started
    )
    historical_pause_exists = bool(
        pause_path.is_file()
        or (
            (recovery2_active or recovery3_active)
            and recovery_pause_path is not None
            and recovery_pause_path.is_file()
        )
        or (
            recovery3_active
            and recovery2_pause_path is not None
            and recovery2_pause_path.is_file()
        )
    )
    current_pause_path = (
        active_recovery["pause_path"] if active_recovery else pause_path
    )
    current_run_path = (
        active_recovery["run_path"] if active_recovery else run_path
    )
    pause_exists = bool(current_pause_path and current_pause_path.is_file())
    terminal = int(counts.get("planned", 0)) == 0
    run_report = None
    run_bindings = {}
    if recovery3_active and current_run_path and current_run_path.is_file():
        try:
            run_report = json.loads(current_run_path.read_text(encoding="utf-8"))
            run_bindings = run_report.get("bindings", {})
            run_status = run_report.get("status")
            if run_status not in {"v21_recovery3_paused", "v21_recovery3_finished"}:
                identity_errors.append("recovery3_run_status")
            if run_bindings.get("ticket_sha256") != recovery3["ticket_sha256"]:
                identity_errors.append("recovery3_run_ticket_sha256")
            if run_bindings.get("admission_sha256") != recovery3["admission_sha256"]:
                identity_errors.append("recovery3_run_admission_sha256")
            if run_bindings.get("parser_sha256") != recovery3["parser_sha256"]:
                identity_errors.append("recovery3_run_parser_sha256")
            report_counts = run_report.get("counts") or {}
            if any(
                int(report_counts.get(key) or 0) != int(counts.get(key, 0))
                for key in ("success", "planned", "failed")
            ):
                identity_errors.append("recovery3_run_counts")
            if run_status == "v21_recovery3_paused":
                if not pause_exists:
                    identity_errors.append("recovery3_pause_missing")
                elif run_bindings.get("pause_sha256") != _file_sha256(
                    current_pause_path
                ):
                    identity_errors.append("recovery3_run_pause_sha256")
            elif run_status == "v21_recovery3_finished" and pause_exists:
                identity_errors.append("recovery3_finished_with_pause")
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            identity_errors.append("recovery3_run_unavailable")
    elif recovery2_active and current_run_path and current_run_path.is_file():
        try:
            run_report = json.loads(current_run_path.read_text(encoding="utf-8"))
            run_bindings = run_report.get("bindings", {})
            run_status = run_report.get("status")
            if run_status not in {"v21_recovery2_paused", "v21_recovery2_finished"}:
                identity_errors.append("recovery2_run_status")
            if run_bindings.get("ticket_sha256") != recovery2["ticket_sha256"]:
                identity_errors.append("recovery2_run_ticket_sha256")
            if run_bindings.get("admission_sha256") != recovery2["admission_sha256"]:
                identity_errors.append("recovery2_run_admission_sha256")
            if run_bindings.get("parser_sha256") != recovery2["parser_sha256"]:
                identity_errors.append("recovery2_run_parser_sha256")
            report_counts = run_report.get("counts") or {}
            if any(
                int(report_counts.get(key) or 0) != int(counts.get(key, 0))
                for key in ("success", "planned", "failed")
            ):
                identity_errors.append("recovery2_run_counts")
            if run_status == "v21_recovery2_paused":
                if not pause_exists:
                    identity_errors.append("recovery2_pause_missing")
                elif run_bindings.get("pause_sha256") != _file_sha256(
                    current_pause_path
                ):
                    identity_errors.append("recovery2_run_pause_sha256")
            elif run_status == "v21_recovery2_finished" and pause_exists:
                identity_errors.append("recovery2_finished_with_pause")
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            identity_errors.append("recovery2_run_unavailable")
    if recovery3_active:
        if (
            counts.get("failed", 0) and not pause_exists
            and (
                run_report is None
                or run_report.get("status") != "v21_recovery3_paused"
            )
        ):
            identity_errors.append("recovery3_pause_missing")
        if terminal and not counts.get("failed", 0) and (
            run_report is None
            or run_report.get("status") != "v21_recovery3_finished"
        ):
            identity_errors.append("recovery3_terminal_run_missing")
        if terminal and not counts.get("failed", 0):
            terminal_audit_path = recovery3["terminal_audit_path"]
            if terminal_audit_path is None or not terminal_audit_path.is_file():
                identity_errors.append("recovery3_terminal_audit_missing")
            else:
                try:
                    terminal_audit = json.loads(
                        terminal_audit_path.read_text(encoding="utf-8")
                    )
                    if terminal_audit.get("status") != (
                        "v21_recovery3_collection_complete_parser_epoch_mixed"
                    ):
                        identity_errors.append("recovery3_terminal_audit_status")
                    audit_counts = terminal_audit.get("counts") or {}
                    if any(
                        int(audit_counts.get(key) or 0) != int(counts.get(key, 0))
                        for key in ("success", "planned", "failed")
                    ):
                        identity_errors.append("recovery3_terminal_audit_counts")
                    if run_bindings.get("terminal_audit_sha256") != _file_sha256(
                        terminal_audit_path
                    ):
                        identity_errors.append(
                            "recovery3_run_terminal_audit_sha256"
                        )
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    identity_errors.append("recovery3_terminal_audit_unavailable")
        if pause_exists and (
            run_report is None
            or run_report.get("status") != "v21_recovery3_paused"
        ):
            identity_errors.append("recovery3_pause_run_missing")
    if recovery2_active:
        if terminal and not counts.get("failed", 0) and (
            run_report is None
            or run_report.get("status") != "v21_recovery2_finished"
        ):
            identity_errors.append("recovery2_terminal_run_missing")
        if terminal and not counts.get("failed", 0):
            terminal_audit_path = recovery2["terminal_audit_path"]
            if terminal_audit_path is None or not terminal_audit_path.is_file():
                identity_errors.append("recovery2_terminal_audit_missing")
            else:
                try:
                    terminal_audit = json.loads(
                        terminal_audit_path.read_text(encoding="utf-8")
                    )
                    if terminal_audit.get("status") != (
                        "v21_recovery2_collection_complete_parser_epoch_mixed"
                    ):
                        identity_errors.append("recovery2_terminal_audit_status")
                    audit_counts = terminal_audit.get("counts") or {}
                    if any(
                        int(audit_counts.get(key) or 0) != int(counts.get(key, 0))
                        for key in ("success", "planned", "failed")
                    ):
                        identity_errors.append("recovery2_terminal_audit_counts")
                    if run_bindings.get("terminal_audit_sha256") != _file_sha256(
                        terminal_audit_path
                    ):
                        identity_errors.append(
                            "recovery2_run_terminal_audit_sha256"
                        )
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    identity_errors.append("recovery2_terminal_audit_unavailable")
        if pause_exists and (
            run_report is None
            or run_report.get("status") != "v21_recovery2_paused"
        ):
            identity_errors.append("recovery2_pause_run_missing")
    terminal_audit_exists = bool(
        (recovery2_active or recovery3_active)
        and active_recovery["terminal_audit_path"] is not None
        and active_recovery["terminal_audit_path"].is_file()
    )
    if identity_errors:
        status = "2024_official_pit_identity_mismatch"
    elif pause_exists or counts.get("failed", 0):
        status = "2024_official_pit_collection_paused"
    elif terminal and terminal_audit_exists:
        status = "2024_official_pit_collection_terminal_audit_complete"
    elif terminal:
        status = "2024_official_pit_collection_complete_terminal_audit_pending"
    else:
        status = "2024_official_pit_collection_running"
    timestamp_paths = [checkpoint_path, Path(str(checkpoint_path) + "-wal")]
    checkpoint_updated_at = datetime.fromtimestamp(max(
        path.stat().st_mtime for path in timestamp_paths if path.is_file()
    )).astimezone().isoformat(timespec="seconds")
    return {
        "available": True,
        "active": status == "2024_official_pit_collection_running",
        "terminal": terminal,
        "status": status,
        "years": {
            "2025": {"success": 5478 if prior_year_ready else 0, "terminal": prior_year_ready},
            "2024": {**counts, "targets": targets},
        },
        "completed": completed,
        "success": int(counts.get("success", 0)),
        "failed": int(counts.get("failed", 0)),
        "planned": int(counts.get("planned", 0)),
        "targets": targets,
        "progress_pct": round(completed / targets * 100, 2) if targets else 0.0,
        "checkpoint_updated_at": checkpoint_updated_at,
        "latest_job_updated_at": latest_updated_at,
        "pause_exists": pause_exists,
        "historical_pause_exists": historical_pause_exists,
        "collection_generation": collection_generation,
        "remediation_status": (
            "paused_remediation_pending" if remediation_pending else None
        ),
        "recovery_identity_valid": (
            active_recovery["valid"] if active_recovery else None
        ),
        "recovery2_identity_valid": (
            recovery2["valid"] if recovery2 is not None else None
        ),
        "recovery3_identity_valid": (
            recovery3["valid"] if recovery3 is not None else None
        ),
        "run_report_exists": bool(current_run_path and current_run_path.is_file()),
        "terminal_audit_exists": terminal_audit_exists,
        "identity_valid": not identity_errors,
        "identity_errors": identity_errors,
        "admission_sha256": (
            active_recovery["admission_sha256"] if active_recovery
            else admission_sha256
        ),
        "ticket_sha256": (
            active_recovery["ticket_sha256"] if active_recovery
            else ticket_sha256
        ),
        "factor_values_computed": False,
        "prices_or_returns_read": False,
        "ic_or_backtest_run": False,
        "production_allowed": False,
        "next_action": (
            "2024官方PDF采集及采集终局审计均已完成；由冻结的下游重解析与新颖性证据"
            "决定候选状态。禁止机械扩年、重复采集或读取未准入候选的收益。"
            if status == "2024_official_pit_collection_terminal_audit_complete"
            else
            "等待唯一2024官方PDF采集自然完成；终局后按版式家族一次审计与修复，"
            "随后先冻结新的独立一手公式与PIT合同，再只采集该公式明确需要的最小输入。"
            "禁止机械扩年、并跑第二采集器或读取收益。"
        ),
    }


def _cash_flow_profitability_terminal_chain_status(
    paths: dict[str, Path] | None = None,
) -> dict:
    """Read the small frozen terminal artifacts; never scan the DB or CSV."""
    paths = paths or {
        "recovery3": _FIRST_FILING_V21_RECOVERY3_TERMINAL_AUDIT_PATH,
        "terminal_failure": _FIRST_FILING_TERMINAL_REPARSE_FAILURE_PATH,
        "material_prepared": _FIRST_FILING_MATERIAL_OVERLAY_PATH,
        "material_completion": _FIRST_FILING_MATERIAL_OVERLAY_COMPLETION_PATH,
        "fixed2_merge_v2": _FIRST_FILING_FIXED2_MERGE_V2_PATH,
        "manifest_v2": _CASH_FLOW_FINAL_STATEMENT_MANIFEST_V2_PATH,
        "input_audit_v2": _CASH_FLOW_INPUT_READINESS_V2_PATH,
        "freeze_admission_v2": _CASH_FLOW_V2_FREEZE_ADMISSION_PATH,
        "adapter_v2": _CASH_FLOW_V2_ADAPTER_PATH,
        "factor_novelty": _CASH_FLOW_FACTOR_NOVELTY_REPORT_PATH,
        "factor_novelty_admission": _CASH_FLOW_FACTOR_NOVELTY_ADMISSION_PATH,
        "factor_novelty_implementation": _CASH_FLOW_FACTOR_NOVELTY_IMPLEMENTATION_PATH,
        "factor_novelty_test": _CASH_FLOW_FACTOR_NOVELTY_TEST_PATH,
        "factor_novelty_failed_v1_report": (
            _CASH_FLOW_FACTOR_NOVELTY_FAILED_V1_REPORT_PATH
        ),
        "factor_novelty_terminal_amendment": (
            _CASH_FLOW_FACTOR_NOVELTY_TERMINAL_AMENDMENT_PATH
        ),
    }
    core_names = (
        "recovery3", "terminal_failure", "material_prepared",
        "material_completion", "fixed2_merge_v2",
    )
    present = [name for name in core_names if paths[name].is_file()]
    empty = {
        "available": False,
        "status": "not_started",
        "identity_valid": None,
        "terminal_reparse_status": None,
        "input_manifest_status": "pending",
        "input_audit_status": "pending",
        "factor_values_computed": False,
        "prices_or_returns_read": False,
        "ic_or_backtest_run": False,
        "production_allowed": False,
        "artifact_sha256": {},
        "counts": {},
        "errors": [],
    }
    if not present:
        return empty
    if len(present) != len(core_names):
        return {
            **empty,
            "available": True,
            "status": "multiyear_pit_terminal_evidence_validation",
            "identity_valid": False,
            "errors": ["terminal_chain_artifact_missing"],
        }

    try:
        artifacts = {
            name: json.loads(paths[name].read_text(encoding="utf-8"))
            for name in core_names
        }
        shas = {name: _file_sha256(paths[name]) for name in core_names}
        recovery = artifacts["recovery3"]
        failure = artifacts["terminal_failure"]
        prepared = artifacts["material_prepared"]
        completion = artifacts["material_completion"]
        fixed2 = artifacts["fixed2_merge_v2"]
        recovery_truth = recovery.get("truth_flags") or {}
        failure_truth = failure.get("truth_flags") or {}
        prepared_truth = prepared.get("truth_flags") or {}
        completion_truth = completion.get("truth_flags") or {}
        fixed2_truth = fixed2.get("truth_flags") or {}
        prepared_bindings = prepared.get("bindings") or {}
        completion_bindings = completion.get("bindings") or {}
        mutation = fixed2.get("mutation") or {}
        fixed2_before = mutation.get("checkpoint_before") or {}
        fixed2_after = mutation.get("checkpoint_after") or {}
        recovery_logical = recovery.get("logical_rows_sha256")
        errors = []

        def require(condition: bool, code: str) -> None:
            if not condition:
                errors.append(code)

        require(
            recovery.get("status")
            == "v21_recovery3_collection_complete_parser_epoch_mixed"
            and recovery.get("counts")
            == {"success": 5380, "planned": 0, "failed": 0},
            "recovery3_terminal_invalid",
        )
        require(
            recovery_truth.get("collection_complete") is True
            and all(
                recovery_truth.get(key) is False
                for key in (
                    "factor_input_ready", "factor_values_computed",
                    "prices_or_returns_read", "ic_or_backtest_run",
                    "production_ready",
                )
            ),
            "recovery3_truth_boundary_invalid",
        )
        require(
            failure.get("status")
            == "v21_terminal_reparse_failed_ticket_consumed_no_retry"
            and failure_truth.get("execution_receipt_consumed") is True
            and failure_truth.get("retry_allowed") is False
            and failure_truth.get("database_changed") is False
            and all(
                failure_truth.get(key) in (False, 0)
                for key in (
                    "database_writes", "network_reads", "prices_or_returns_read",
                    "factor_values_computed", "ic_or_backtest_run",
                    "factor_input_ready", "production_ready",
                )
            ),
            "terminal_reparse_failure_not_closed",
        )
        require(
            prepared.get("status") == "v21_material_field_overlay_prepared"
            and prepared_bindings.get("recovery3_terminal_sha256")
            == shas["recovery3"]
            and prepared_bindings.get("failure_artifact_sha256")
            == shas["terminal_failure"]
            and prepared_bindings.get("checkpoint_logical_rows_sha256")
            == recovery_logical
            and prepared_truth.get("factor_values_computed") is False
            and prepared_truth.get("prices_or_returns_read") is False
            and prepared_truth.get("ic_or_backtest_run") is False
            and prepared_truth.get("production_ready") is False,
            "material_overlay_prepared_invalid",
        )
        require(
            completion.get("status") == "v21_material_field_overlay_passed"
            and completion.get("prepared_report_sha256")
            == shas["material_prepared"]
            and completion_bindings.get("recovery3_terminal_sha256")
            == shas["recovery3"]
            and completion_bindings.get("failure_artifact_sha256")
            == shas["terminal_failure"]
            and completion_truth == {
                "database_identity_unchanged": True,
                "prepared_report_sha256_verified": True,
                "third_freeze_passed": True,
            },
            "material_overlay_completion_invalid",
        )
        require(
            fixed2.get("status") == "passed"
            and fixed2_before.get("logical_rows_sha256") == recovery_logical
            and fixed2_after.get("logical_rows_sha256") != recovery_logical
            and fixed2_truth.get("outcomes_read") is False
            and fixed2_truth.get("production_modified") is False,
            "fixed2_merge_v2_invalid",
        )

        manifest_status = "pending"
        audit_status = "pending"
        counts = {}
        manifest_path = paths["manifest_v2"]
        audit_path = paths["input_audit_v2"]
        if manifest_path.is_file() or audit_path.is_file():
            require(
                manifest_path.is_file() and audit_path.is_file()
                and paths["freeze_admission_v2"].is_file()
                and paths["adapter_v2"].is_file(),
                "input_readiness_v2_pair_incomplete",
            )
            if all(
                path.is_file()
                for path in (
                    manifest_path, audit_path, paths["freeze_admission_v2"],
                    paths["adapter_v2"],
                )
            ):
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                admission = json.loads(
                    paths["freeze_admission_v2"].read_text(encoding="utf-8")
                )
                shas["manifest_v2"] = _file_sha256(manifest_path)
                shas["input_audit_v2"] = _file_sha256(audit_path)
                shas["freeze_admission_v2"] = _file_sha256(
                    paths["freeze_admission_v2"]
                )
                shas["adapter_v2"] = _file_sha256(paths["adapter_v2"])
                admission_bindings = admission.get("bindings") or {}
                manifest_bindings = manifest.get("bindings") or {}
                bindings = audit.get("artifact_bindings") or {}
                audit_truth = audit.get("truth_flags") or {}
                manifest_status = str(manifest.get("status") or "invalid")
                audit_status = str(audit.get("status") or "invalid")
                counts = audit.get("counts") or {}
                base_binding_keys = {
                    "adapter_sha256", "recovery3_terminal_sha256",
                    "material_overlay_prepared_sha256",
                    "material_overlay_completion_sha256",
                    "fixed2_merge_v2_report_sha256",
                    "database_logical_rows_sha256",
                    "pit_industry_manifest_sha256",
                    "pit_industry_records_csv_sha256",
                    "pit_industry_crosswalk_report_sha256",
                }
                expected_manifest_bindings = {
                    **{
                        key: admission_bindings.get(key)
                        for key in base_binding_keys
                    },
                    "freeze_manifest_admission_sha256": shas[
                        "freeze_admission_v2"
                    ],
                }
                expected_audit_bindings = {
                    **expected_manifest_bindings,
                    "final_statement_manifest_sha256": shas["manifest_v2"],
                }
                expected_permissions = {
                    "write_manifest_once": True,
                    "database_writes": False,
                    "source_artifact_writes": False,
                    "factor_values": False,
                    "ratios": False,
                    "prices_or_returns": False,
                    "ic_or_backtest": False,
                    "production": False,
                }
                expected_audit_truth_keys = {
                    "input_ready", "coverage_threshold_frozen",
                    "formation_timing_frozen", "required_years_frozen",
                    "common_rows_frozen", "factor_values_computed",
                    "ratios_computed", "candidate_ranks_computed",
                    "correlations_computed", "prices_or_returns_read",
                    "ic_computed", "backtest_computed",
                    "strategy_signal_computed", "production_allowed",
                    "recommendation_eligible",
                }
                scope = admission.get("scope") or {}
                expected_manifest_row_fields = [
                    "issuer_code", "board", "stat_date", "pub_datetime",
                    "announcement_id", "pdf_url", "pdf_sha256",
                    "statement_scope", "currency", "unit_basis",
                    "overlay_source", "base_result_sha256",
                    "selected_result_sha256", "overlay_evidence_sha256",
                ]
                require(
                    set(admission) == {
                        "schema_version", "status", "admitted_at",
                        "execution_nonce", "bindings", "scope", "permissions",
                    }
                    and admission.get("schema_version") == 2
                    and admission.get("status")
                    == "cash_flow_profitability_final_statement_inputs_v2_freeze_admitted"
                    and set(admission_bindings)
                    == base_binding_keys | {"output_path"}
                    and admission_bindings.get("adapter_sha256")
                    == shas["adapter_v2"]
                    and admission_bindings.get("recovery3_terminal_sha256")
                    == shas["recovery3"]
                    and admission_bindings.get("material_overlay_prepared_sha256")
                    == shas["material_prepared"]
                    and admission_bindings.get("material_overlay_completion_sha256")
                    == shas["material_completion"]
                    and admission_bindings.get("fixed2_merge_v2_report_sha256")
                    == shas["fixed2_merge_v2"]
                    and admission_bindings.get("database_logical_rows_sha256")
                    == fixed2_after.get("logical_rows_sha256")
                    and admission_bindings.get("output_path")
                    == str(manifest_path.resolve())
                    and set(scope) == {
                        "expected_rows", "report_period", "overlay_priority",
                        "material_overlay_issuer_codes", "fixed2_issuer_codes",
                        "statement_scope", "currency", "unit_basis",
                        "manifest_row_fields", "result_sha_identity",
                    }
                    and scope.get("expected_rows") == 5380
                    and scope.get("report_period") == "2024-12-31"
                    and scope.get("overlay_priority") == [
                        "fixed2_ocr_v2", "material_field_overlay", "checkpoint",
                    ]
                    and scope.get("material_overlay_issuer_codes") == [
                        "000060", "000090", "000513", "000635", "000686",
                        "001356", "002352",
                    ]
                    and scope.get("fixed2_issuer_codes") == ["000619", "002028"]
                    and scope.get("statement_scope")
                    == "consolidated_current_year_only"
                    and scope.get("currency") == "CNY"
                    and scope.get("unit_basis") == "yuan"
                    and scope.get("manifest_row_fields")
                    == expected_manifest_row_fields
                    and scope.get("result_sha_identity") == {
                        "checkpoint": "utf8_result_json_bytes",
                        "material_field_overlay_base": (
                            "legacy_v14_insertion_order_object_digest_"
                            "bound_by_prepared_report"
                        ),
                        "material_field_overlay_selected": "json_sort_keys_utf8",
                        "fixed2_ocr_v2": "utf8_result_json_bytes",
                    }
                    and admission.get("permissions") == expected_permissions,
                    "freeze_admission_v2_invalid",
                )
                require(
                    set(manifest) == {
                        "schema_version", "status", "bindings", "scope", "rows",
                    }
                    and manifest.get("schema_version") == 2
                    and manifest_status
                    == "cash_flow_profitability_final_statement_inputs_v2_frozen"
                    and manifest_bindings == expected_manifest_bindings
                    and manifest.get("scope") == admission.get("scope")
                    and isinstance(manifest.get("rows"), list)
                    and len(manifest["rows"])
                    == int((manifest.get("scope") or {}).get("expected_rows", -1)),
                    "final_statement_manifest_v2_invalid",
                )
                require(
                    set(audit) == {
                        "schema_version", "audit_id", "status",
                        "artifact_bindings", "counts", "coverage_by_stratum",
                        "missing_reason_counts", "exclusion_reason_counts",
                        "overlay_source_counts", "unresolved_contracts",
                        "truth_flags",
                    }
                    and audit.get("schema_version") == 2
                    and audit.get("audit_id")
                    == "cash_flow_profitability_current_assets_input_readiness_v2"
                    and audit_status == "input_not_ready_contracts_pending"
                    and bindings == expected_audit_bindings
                    and set(counts) == {
                        "total_rows", "pit_time_valid_rows",
                        "same_report_same_pdf_rows", "consolidated_scope_rows",
                        "direct_operating_cashflow_rows",
                        "positive_total_assets_rows", "pit_industry_matched_rows",
                        "financial_industry_excluded_rows",
                        "eligible_nonfinancial_input_rows",
                    }
                    and counts.get("total_rows") == scope.get("expected_rows")
                    and audit.get("overlay_source_counts") == {
                        "checkpoint": 5371,
                        "fixed2_ocr_v2": 2,
                        "material_field_overlay": 7,
                    }
                    and set(audit_truth) == expected_audit_truth_keys
                    and all(value is False for value in audit_truth.values()),
                    "input_readiness_v2_audit_invalid",
                )
        factor_path = paths.get("factor_novelty")
        factor_status = None
        factor_rows = []
        failed_comparisons = []
        if factor_path is not None and factor_path.is_file():
            factor_artifact_paths = {
                name: paths.get(name)
                for name in (
                    "factor_novelty_admission", "factor_novelty_implementation",
                    "factor_novelty_test", "factor_novelty_failed_v1_report",
                )
            }
            require(
                all(path is not None and path.is_file() for path in factor_artifact_paths.values()),
                "factor_novelty_chain_incomplete",
            )
            if all(path is not None and path.is_file() for path in factor_artifact_paths.values()):
                factor_report = json.loads(factor_path.read_text(encoding="utf-8"))
                factor_admission = json.loads(
                    factor_artifact_paths["factor_novelty_admission"].read_text(
                        encoding="utf-8"
                    )
                )
                shas["factor_novelty"] = _file_sha256(factor_path)
                shas["factor_novelty_admission"] = _file_sha256(
                    factor_artifact_paths["factor_novelty_admission"]
                )
                shas["factor_novelty_implementation"] = _file_sha256(
                    factor_artifact_paths["factor_novelty_implementation"]
                )
                shas["factor_novelty_test"] = _file_sha256(
                    factor_artifact_paths["factor_novelty_test"]
                )
                shas["factor_novelty_failed_v1_report"] = _file_sha256(
                    factor_artifact_paths["factor_novelty_failed_v1_report"]
                )
                admission_bindings = factor_admission.get("bindings") or {}
                report_bindings = factor_report.get("bindings") or {}
                factor_rows = (factor_report.get("factor_values") or {}).get("rows") or []
                novelty = factor_report.get("adjacent_novelty") or {}
                comparisons = novelty.get("comparisons") or {}
                truth = factor_report.get("truth_flags") or {}
                terminal_amendment_valid = False
                terminal_amendment_path = paths.get(
                    "factor_novelty_terminal_amendment"
                )
                if (
                    factor_report.get("status")
                    == "novelty_failed_permanently_archived_before_outcomes"
                    and terminal_amendment_path is not None
                    and terminal_amendment_path.is_file()
                ):
                    amendment = json.loads(
                        terminal_amendment_path.read_text(encoding="utf-8")
                    )
                    amendment_bindings = amendment.get("bindings") or {}
                    expected_paths = {
                        "terminal_report": factor_path,
                        "historical_execution_admission": factor_artifact_paths[
                            "factor_novelty_admission"
                        ],
                        "current_terminal_test": factor_artifact_paths[
                            "factor_novelty_test"
                        ],
                        "current_joint_execution_amendment": (
                            _PROJECT_ROOT / "research"
                            / "UNIFIED_REGIME_ROUTE_PORTFOLIO_BACKTEST_PROTOCOL_V1_AMENDMENT_5.json"
                        ),
                    }
                    terminal_amendment_valid = (
                        amendment.get("status")
                        == "terminal_research_identity_rebound_without_outcome_or_reentry"
                        and amendment.get("candidate_id")
                        == factor_report.get("candidate_id")
                        and set(amendment_bindings) == set(expected_paths)
                        and all(
                            amendment_bindings[name].get("path")
                            == str(path.relative_to(_PROJECT_ROOT))
                            and amendment_bindings[name].get("sha256")
                            == _file_sha256(path)
                            for name, path in expected_paths.items()
                        )
                        and (amendment.get("superseded_historical_binding") or {}).get(
                            "test_sha256"
                        ) is None
                        and (amendment.get("superseded_historical_binding") or {}).get(
                            "sha256"
                        ) == admission_bindings.get("test_sha256")
                        and all(
                            value is False
                            for value in (amendment.get("authority") or {}).values()
                        )
                        and all(
                            value is False
                            for value in (amendment.get("truth_boundary") or {}).values()
                        )
                    )
                    shas["factor_novelty_terminal_amendment"] = _file_sha256(
                        terminal_amendment_path
                    )
                expected_report_bindings = {
                    **{
                        key: value for key, value in admission_bindings.items()
                        if key != "output_path"
                    },
                    "execution_admission_sha256": shas["factor_novelty_admission"],
                }
                factor_status = str(factor_report.get("status") or "invalid")
                failed_comparisons = [
                    identity for identity, item in comparisons.items()
                    if isinstance(item, dict) and item.get("passed") is False
                ]
                require(
                    set(factor_admission) == {
                        "schema_version", "status", "admitted_at",
                        "execution_nonce", "bindings", "scope", "permissions",
                    }
                    and factor_admission.get("schema_version") == 2
                    and factor_admission.get("status")
                    == "cash_flow_profitability_factor_novelty_clean_replication_admitted"
                    and admission_bindings.get("implementation_sha256")
                    == shas["factor_novelty_implementation"]
                    and (
                        admission_bindings.get("test_sha256")
                        == shas["factor_novelty_test"]
                        or terminal_amendment_valid
                    )
                    and admission_bindings.get("input_2024_manifest_sha256")
                    == shas.get("manifest_v2")
                    and admission_bindings.get("input_2024_audit_sha256")
                    == shas.get("input_audit_v2")
                    and admission_bindings.get("failed_v1_report_sha256")
                    == shas["factor_novelty_failed_v1_report"]
                    and admission_bindings.get("output_path")
                    == str(factor_path.resolve())
                    and factor_admission.get("permissions", {}).get(
                        "calculate_exactly_five_adjacent_correlations"
                    ) is True
                    and all(
                        factor_admission.get("permissions", {}).get(key) is False
                        for key in (
                            "database_writes", "source_artifact_writes",
                            "prices_or_returns", "ic_or_backtest",
                            "forward_observation", "candidate_registration",
                            "production", "recommendation", "automatic_trading",
                        )
                    ),
                    "factor_novelty_admission_invalid",
                )
                require(
                    set(factor_report) == {
                        "schema_version", "execution_id", "generated_at", "status",
                        "candidate_id", "bindings", "scope", "input_preflight",
                        "factor_values", "adjacent_novelty", "gates", "truth_flags",
                        "next_action", "supersedes",
                    }
                    and factor_report.get("schema_version") == 2
                    and factor_report.get("candidate_id")
                    == "cash_flow_profitability_level_current_period_total_assets_v1"
                    and factor_status in {
                        "input_collection_incomplete_not_pass_or_fail",
                        "adjacent_inputs_incomplete_not_pass_or_fail",
                        "novelty_failed_permanently_archived_before_outcomes",
                        "novelty_passed_forward_observation_not_started",
                    }
                    and report_bindings == expected_report_bindings
                    and (factor_report.get("factor_values") or {}).get("row_count")
                    == len(factor_rows)
                    and len({
                        (row.get("report_year"), row.get("issuer_code"))
                        for row in factor_rows if isinstance(row, dict)
                    }) == len(factor_rows)
                    and novelty.get("comparison_count") == 5
                    and set(comparisons) == {
                        "percent_operating_accruals", "v47_cash_to_profit",
                        "v47_cash_to_revenue", "operating_cash_flow_growth",
                        "piotroski_cash_flow_to_beginning_assets_subsignal",
                    }
                    and truth.get("prices_or_returns_read") is False
                    and truth.get("prior_v47_report_opened") is False
                    and truth.get("ic_or_backtest_computed") is False
                    and truth.get("production_allowed") is False
                    and truth.get("recommendation_eligible") is False
                    and truth.get("automatic_trading_allowed") is False
                    and (factor_report.get("gates") or {}).get(
                        "v47_report_bytes_opened"
                    ) is False
                    and factor_report.get("supersedes") == {
                        "report_path": (
                            "reports/cash_flow_profitability_current_assets_"
                            "v1_factor_novelty.json"
                        ),
                        "report_sha256": shas[
                            "factor_novelty_failed_v1_report"
                        ],
                        "reason": (
                            "V1 opened V47 bytes for hash verification; "
                            "mathematical result retained only as failed audit history"
                        ),
                    }
                    and (
                        factor_status != "novelty_failed_permanently_archived_before_outcomes"
                        or (
                            novelty.get("all_five_inputs_complete") is True
                            and novelty.get("all_five_passed") is False
                            and bool(failed_comparisons)
                            and all(
                                comparisons[identity].get("decision_statistic", 0) >= 0.80
                                for identity in failed_comparisons
                            )
                        )
                    ),
                    "factor_novelty_report_invalid",
                )
                if not errors:
                    counts = {
                        **counts,
                        "factor_rows": len(factor_rows),
                        "cross_year_common_rows": int(
                            (factor_report.get("input_preflight") or {}).get(
                                "common_issuer_rows", 0
                            )
                        ),
                        "failed_novelty_comparisons": len(failed_comparisons),
                    }
                    manifest_status = str(manifest_status)
                    audit_status = str(audit_status)
        return {
            **empty,
            "available": True,
            "status": (
                "multiyear_pit_terminal_evidence_validation" if errors
                else factor_status if factor_path is not None and factor_path.is_file()
                else "input_readiness_contract_freeze"
            ),
            "identity_valid": not errors,
            "terminal_reparse_status": "failed_closed_no_retry",
            "input_manifest_status": manifest_status,
            "input_audit_status": audit_status,
            "artifact_sha256": shas,
            "counts": counts,
            "errors": errors,
            "factor_values_computed": (
                bool(factor_rows)
                if factor_path is not None and factor_path.is_file() and not errors
                else False
            ),
            "factor_novelty_failed_comparisons": (
                failed_comparisons
                if factor_path is not None and factor_path.is_file() and not errors
                else []
            ),
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            **empty,
            "available": True,
            "status": "multiyear_pit_terminal_evidence_validation",
            "identity_valid": False,
            "errors": [f"terminal_chain_artifact_invalid:{type(exc).__name__}"],
        }


def _remaining_source_frontier(path: Path | None = None) -> dict:
    """Expose the next distinct data unlock without starting another experiment."""
    if path is None and _GITHUB_FACTOR_BATCH_PATH.is_file():
        try:
            batch = json.loads(
                _GITHUB_FACTOR_BATCH_PATH.read_text(encoding="utf-8")
            )
            resolution = json.loads(
                _SHARE_ISS_RESOLUTION_PATH.read_text(encoding="utf-8")
            )
            cash_resolution = json.loads(
                _CASH_INPUT_RESOLUTION_PATH.read_text(encoding="utf-8")
            )
            inventory_result = json.loads(
                _INVENTORY_FACTOR_RESULT_PATH.read_text(encoding="utf-8")
            )
            divseason_result = json.loads(
                _DIVSEASON_FACTOR_RESULT_PATH.read_text(encoding="utf-8")
            )
            divseason_lifecycle = json.loads(
                _DIVSEASON_LIFECYCLE_PATH.read_text(encoding="utf-8")
            )
            divinit_result = json.loads(
                _DIVINIT_FACTOR_RESULT_PATH.read_text(encoding="utf-8")
            )
            divinit_closure = json.loads(
                _DIVINIT_CLOSURE_PATH.read_text(encoding="utf-8")
            )
            block_trade_result = json.loads(
                _BLOCK_TRADE_V2_RESULT_PATH.read_text(encoding="utf-8")
            )
            block_trade_terminal = json.loads(
                _BLOCK_TRADE_V2_TERMINAL_PATH.read_text(encoding="utf-8")
            )
            chtax_input = json.loads(
                _CHTAX_INPUT_EPOCH_PATH.read_text(encoding="utf-8")
            )
            source = batch["source"]
            completed = batch["completed_candidate"]
            mechanisms = batch["next_source_rows"]
            truth = batch["truth"]
            frozen = resolution["frozen_result"]
            root_cause = resolution["root_cause"]
            cash_sample = cash_resolution["acceptance_sample"]
            cash_decision = cash_resolution["decision"]
            inventory_splits = inventory_result["splits"]
            if not isinstance(mechanisms, list):
                raise ValueError("next_source_rows must be a list")
            if inventory_result.get("future_outcome_read") is not True:
                raise ValueError("latest inventory result is not terminal")
            if divseason_result.get("future_outcome_read") is not True:
                raise ValueError("latest dividend-seasonality result is not terminal")
            if divseason_lifecycle.get("status") != (
                "permanently_archived_after_single_cost_inclusive_test"
            ):
                raise ValueError("latest dividend-seasonality lifecycle is not sealed")
            if (
                divinit_result.get("status") != "permanently_rejected_historical_factor"
                or divinit_result.get("future_outcome_read") is not True
                or divinit_result.get("historical_factor_gate_passed") is not False
                or (divinit_closure.get("bound_historical_result") or {}).get("sha256")
                != _file_sha256(_DIVINIT_FACTOR_RESULT_PATH)
            ):
                raise ValueError("latest dividend-initiation result is not sealed")
            if (
                block_trade_result.get("status")
                != "permanently_rejected_historical_factor"
                or block_trade_result.get("future_outcome_read") is not True
                or block_trade_result.get("historical_factor_gate_passed") is not False
                or (block_trade_terminal.get("chain_bindings") or {}).get(
                    "reports/v303_block_trade_premium_v2_blind_factor_test.json"
                ) != _file_sha256(_BLOCK_TRADE_V2_RESULT_PATH)
                or (block_trade_terminal.get("terminal_policy") or {}).get(
                    "production_allowed"
                ) is not False
            ):
                raise ValueError("latest block-trade result is not sealed")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
        else:
            return {
                "available": True,
                "as_of": str(block_trade_terminal.get("resolved_at") or "")[:10]
                or batch.get("generated_on"),
                "source_project": source.get("repository"),
                "source_commit": source.get("commit"),
                "reviewed_count": source.get("predictor_rows") or (1 + len(mechanisms)),
                "operational_status": "source_hypotheses_exhausted",
                "eligible_new_candidate": None,
                "reason": (
                    "最新独立来源候选大宗交易溢价已完成唯一成本后盲测并永久否决；"
                    "其正Rank IC未转化为可执行净收益。当前没有候选获准投产。"
                ),
                "next_input_priority": [],
                "next_compute_action": (
                    "Wait for a genuinely new PIT data identity or an independent economic mechanism; "
                    "do not rerun sealed families."
                ),
                "latest_source_batch": {
                    "status": batch.get("status"),
                    "repository": source.get("repository"),
                    "commit": source.get("commit"),
                    "license_spdx": source.get("license_spdx"),
                    "signal_document_rows": source.get("signal_document_rows"),
                    "predictor_rows": source.get("predictor_rows"),
                    "clear_good_replication_rows": source.get(
                        "clear_good_replication_rows"
                    ),
                    "historically_passed": truth.get(
                        "historically_passed_in_this_batch"
                    ),
                    "production_factors_added": truth.get(
                        "production_factors_added"
                    ),
                    "production_factors_total": truth.get(
                        "production_factors_total"
                    ),
                    "report_path": str(
                        _GITHUB_FACTOR_BATCH_PATH.relative_to(_PROJECT_ROOT)
                    ),
                    "report_sha256": _file_sha256(_GITHUB_FACTOR_BATCH_PATH),
                },
                "completed_candidate_result": {
                    "candidate_id": block_trade_result.get("candidate_id"),
                    "source_acronym": "BlockTradePremium",
                    "formation_dates": (
                        block_trade_result.get("pooled") or {}
                    ).get("dates"),
                    "decision": block_trade_result.get("status"),
                    "reason": block_trade_terminal.get("interpretation"),
                    "result_path": str(
                        _BLOCK_TRADE_V2_RESULT_PATH.relative_to(_PROJECT_ROOT)
                    ),
                    "result_sha256": _file_sha256(_BLOCK_TRADE_V2_RESULT_PATH),
                    "production_allowed": False,
                    "pooled_mean_net_base_spread": (
                        block_trade_result.get("pooled") or {}
                    ).get("mean_net_base_spread"),
                    "bootstrap_probability_mean_le_zero": (
                        (block_trade_result.get("pooled") or {}).get("bootstrap") or {}
                    ).get("probability_mean_le_zero"),
                },
                "chtax_input_result": {
                    "status": chtax_input.get("status"),
                    "rows": (chtax_input.get("counts") or {}).get("rows"),
                    "periods": (chtax_input.get("counts") or {}).get("periods"),
                    "income_tax_ytd_coverage_pct": (
                        ((chtax_input.get("coverage_pct") or {}).get("income_tax_ytd") or {}).get("overall")
                    ),
                    "complete_factor_input_coverage_pct": (
                        ((chtax_input.get("coverage_pct") or {}).get(
                            "complete_discrete_tax_and_lagged_inputs"
                        ) or {}).get("overall")
                    ),
                    "income_notice_within_3_days_pct": (
                        ((chtax_input.get("vendor_notice_date_audit_against_v148") or {}).get("income") or {}).get("within_3_days_pct")
                    ),
                    "input_gate_passed": all(
                        (chtax_input.get("gates") or {}).get(key) is True
                        for key in (
                            "G5_tax_and_asset_coverage",
                            "G6_discrete_tax_and_lagged_asset_coverage",
                            "G7_original_initial_filing_value_validation",
                        )
                    ),
                    "factor_values_computed": chtax_input.get("factor_values_computed") is True,
                    "return_data_read": chtax_input.get("return_data_read") is True,
                    "report_path": str(_CHTAX_INPUT_EPOCH_PATH.relative_to(_PROJECT_ROOT)),
                    "report_sha256": _file_sha256(_CHTAX_INPUT_EPOCH_PATH),
                },
                "cash_to_assets_result": {
                    "status": cash_resolution.get("status"),
                    "candidate_family": cash_resolution.get("candidate_family"),
                    "sample_issuers": cash_sample.get("issuers"),
                    "pit_eligible_rate": cash_sample.get("pit_eligible_rate"),
                    "coverage_gate": cash_sample.get(
                        "frozen_minimum_complete_field_coverage"
                    ),
                    "input_gate_passed": cash_decision.get("input_gate_passed") is True,
                    "full_epoch_started": cash_decision.get("full_epoch_started") is True,
                    "future_outcomes_read": cash_decision.get("future_outcomes_read") is True,
                    "report_path": str(
                        _CASH_INPUT_RESOLUTION_PATH.relative_to(_PROJECT_ROOT)
                    ),
                    "report_sha256": _file_sha256(_CASH_INPUT_RESOLUTION_PATH),
                },
                "share_issuance_result": {
                    "status": resolution.get("status"),
                    "candidate_id": resolution.get("proposed_candidate_id"),
                    "successful_request_codes_pct": frozen.get(
                        "successful_request_codes_pct"
                    ),
                    "nonempty_official_history_codes_pct": frozen.get(
                        "nonempty_official_history_codes_pct"
                    ),
                    "failed_gate": frozen.get("failed_gate"),
                    "empty_delisted_codes": root_cause.get(
                        "successful_but_empty_codes"
                    ),
                    "factor_values_computed": (
                        (resolution.get("disposition") or {}).get(
                            "factor_values_computed"
                        ) is True
                    ),
                    "report_path": str(
                        _SHARE_ISS_RESOLUTION_PATH.relative_to(_PROJECT_ROOT)
                    ),
                    "report_sha256": _file_sha256(
                        _SHARE_ISS_RESOLUTION_PATH
                    ),
                },
                "screened_mechanisms": [
                    {
                        key: item.get(key)
                        for key in (
                            "acronym", "family", "decision", "reason",
                        )
                    }
                    for item in mechanisms if isinstance(item, dict)
                ],
                "report_path": str(
                    _BLOCK_TRADE_V2_RESULT_PATH.relative_to(_PROJECT_ROOT)
                ),
                "report_sha256": _file_sha256(_BLOCK_TRADE_V2_RESULT_PATH),
            }
    if path is None and _SOURCE_FRONTIER_V2_PATH.is_file():
        try:
            audit = json.loads(
                _SOURCE_FRONTIER_V2_PATH.read_text(encoding="utf-8")
            )
            resolution = json.loads(
                _SHARE_ISS_RESOLUTION_PATH.read_text(encoding="utf-8")
            )
            decision = audit["decision"]
            completed = audit["completed_route"]
            mechanisms = audit.get("next_mechanisms") or []
            frozen = resolution["frozen_result"]
            root_cause = resolution["root_cause"]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
        else:
            return {
                "available": True,
                "as_of": audit.get("created_on"),
                "reviewed_count": 1 + len(mechanisms),
                "operational_status": "source_hypotheses_exhausted",
                "eligible_new_candidate": decision.get("eligible_candidate_id"),
                "reason": completed.get("reason"),
                "next_input_priority": [],
                "next_compute_action": decision.get("next_action"),
                "share_issuance_result": {
                    "status": resolution.get("status"),
                    "candidate_id": resolution.get("proposed_candidate_id"),
                    "successful_request_codes_pct": frozen.get(
                        "successful_request_codes_pct"
                    ),
                    "nonempty_official_history_codes_pct": frozen.get(
                        "nonempty_official_history_codes_pct"
                    ),
                    "failed_gate": frozen.get("failed_gate"),
                    "empty_delisted_codes": root_cause.get(
                        "successful_but_empty_codes"
                    ),
                    "factor_values_computed": (
                        (resolution.get("disposition") or {}).get(
                            "factor_values_computed"
                        ) is True
                    ),
                    "report_path": str(
                        _SHARE_ISS_RESOLUTION_PATH.relative_to(_PROJECT_ROOT)
                    ),
                    "report_sha256": _file_sha256(
                        _SHARE_ISS_RESOLUTION_PATH
                    ),
                },
                "screened_mechanisms": [
                    {
                        key: item.get(key)
                        for key in ("family", "decision", "reason")
                    }
                    for item in mechanisms if isinstance(item, dict)
                ],
                "report_path": str(
                    _SOURCE_FRONTIER_V2_PATH.relative_to(_PROJECT_ROOT)
                ),
                "report_sha256": _file_sha256(_SOURCE_FRONTIER_V2_PATH),
            }
    source = path or _SOURCE_FRONTIER_PATH
    try:
        audit = json.loads(source.read_text(encoding="utf-8"))
        conclusion = audit["conclusion"]
        priorities = conclusion["next_input_priority"]
        if not isinstance(priorities, list):
            raise ValueError("next_input_priority must be a list")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return {
            "available": False,
            "operational_status": "source_frontier_unavailable",
            "eligible_new_candidate": None,
            "next_input_priority": [],
            "message": "剩余来源审计不可用；不自动启动替代回测。",
        }
    industry_sample = (
        (audit.get("local_data_evidence") or {})
        .get("official_historical_industry_acceptance_sample") or {}
    )
    industry_intervals = (
        (audit.get("local_data_evidence") or {})
        .get("pit_industry_intervals") or {}
    )
    candidate_input = (
        (audit.get("local_data_evidence") or {})
        .get("selected_candidate_input_feasibility") or {}
    )
    sealed_candidate = (
        (audit.get("local_data_evidence") or {})
        .get("sealed_candidate_result") or {}
    )
    employee_sample = (
        (audit.get("local_data_evidence") or {})
        .get("employee_history_acceptance_sample") or {}
    )
    employee_epoch = {}
    employee_result = {}
    if source == _SOURCE_FRONTIER_PATH:
        try:
            employee_epoch = json.loads(
                _EMPLOYEE_INPUT_EPOCH_PATH.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, json.JSONDecodeError):
            employee_epoch = {}
        try:
            employee_result = json.loads(
                _EMPLOYMENT_FACTOR_RESULT_PATH.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, json.JSONDecodeError):
            employee_result = {}
    employment_terminal = bool(
        employee_result.get("future_outcome_read") is True
        and employee_result.get("status")
        == "permanently_rejected_historical_factor"
    )
    if employment_terminal:
        operational_status = "source_hypotheses_exhausted"
        eligible_new_candidate = None
        reason = (
            "Employment Growth 已完成冻结输入、固定因子与唯一成本后历史验证，"
            "并在 validation 与 untouched confirmation 均失败；该公式永久封存。"
        )
        priorities = []
        next_compute_action = "登记新的独立经济假设；不得重跑或调参复活已否决公式。"
    else:
        operational_status = conclusion.get("operational_status")
        eligible_new_candidate = conclusion.get("eligible_new_candidate")
        reason = conclusion.get("reason")
        next_compute_action = conclusion.get("next_compute_action")
    employee_splits = employee_result.get("splits") or {}

    def employee_metric(split: str, key: str):
        return ((employee_splits.get(split) or {}).get("metrics") or {}).get(key)

    return {
        "available": True,
        "as_of": audit.get("generated_at"),
        "source_project": (audit.get("source") or {}).get("project"),
        "source_commit": (audit.get("source") or {}).get("commit"),
        "reviewed_count": len(audit.get("reviewed_frontier") or []),
        "operational_status": operational_status,
        "eligible_new_candidate": eligible_new_candidate,
        "reason": reason,
        "next_input_priority": priorities,
        "next_compute_action": next_compute_action,
        "accepted_input_sample": {
            "status": industry_sample.get("status"),
            "coverage": industry_sample.get("coverage"),
            "snapshots": industry_sample.get("snapshots"),
            "coverage_pct": industry_sample.get("coverage_pct"),
            "unique_reclassified_stocks": industry_sample.get(
                "unique_reclassified_stocks"
            ),
            "remaining_gap": industry_sample.get("remaining_gap"),
            "report_path": industry_sample.get("path"),
        } if industry_sample else None,
        "pit_industry_intervals": {
            "status": industry_intervals.get("status"),
            "rows": industry_intervals.get("rows"),
            "stocks": industry_intervals.get("stocks"),
            "snapshots": industry_intervals.get("snapshots"),
            "coverage": industry_intervals.get("coverage"),
            "report_path": industry_intervals.get("path"),
        } if industry_intervals else None,
        "selected_candidate_input": {
            "status": candidate_input.get("status"),
            "candidate_id": candidate_input.get("candidate_id"),
            "annual_rows": candidate_input.get("annual_rows"),
            "complete_joined_rows": candidate_input.get("complete_joined_rows"),
            "codes_with_three_contiguous_complete_years": candidate_input.get(
                "codes_with_three_contiguous_complete_years"
            ),
            "report_years": candidate_input.get("report_years"),
            "report_path": candidate_input.get("path"),
        } if candidate_input else None,
        "sealed_candidate_result": {
            "status": sealed_candidate.get("status"),
            "candidate_id": sealed_candidate.get("candidate_id"),
            "historical_factor_gate_passed": sealed_candidate.get(
                "historical_factor_gate_passed"
            ) is True,
            "validation_net_spread_after_full_base_cost": sealed_candidate.get(
                "validation_net_spread_after_full_base_cost"
            ),
            "untouched_confirmation_net_spread_after_full_base_cost": sealed_candidate.get(
                "untouched_confirmation_net_spread_after_full_base_cost"
            ),
            "report_path": sealed_candidate.get("path"),
        } if sealed_candidate else None,
        "employee_history_acceptance_sample": {
            "status": employee_sample.get("status"),
            "issuers": employee_sample.get("issuers"),
            "report_years": employee_sample.get("report_years"),
            "records": employee_sample.get("records"),
            "first_publication_lineage_matches": employee_sample.get(
                "first_publication_lineage_matches"
            ),
            "records_with_later_full_report_revision": employee_sample.get(
                "records_with_later_full_report_revision"
            ),
            "failures": employee_sample.get("failures"),
            "report_path": employee_sample.get("path"),
            "protocol_path": employee_sample.get("protocol"),
        } if employee_sample else None,
        "employee_history_input_epoch": {
            "status": employee_epoch.get("status"),
            "eligible_issuers": employee_epoch.get("eligible_issuers"),
            "report_years": employee_epoch.get("report_years"),
            "target_code_years": employee_epoch.get("target_code_years"),
            "completed_batches": employee_epoch.get("completed_batches"),
            "records": employee_epoch.get("records"),
            "latest_batch_status": employee_epoch.get("latest_batch_status"),
            "report_path": str(
                _EMPLOYEE_INPUT_EPOCH_PATH.relative_to(_PROJECT_ROOT)
            ),
            "report_sha256": _file_sha256(_EMPLOYEE_INPUT_EPOCH_PATH),
        } if employee_epoch else None,
        "employment_growth_result": {
            "status": employee_result.get("status"),
            "candidate_id": employee_result.get("candidate_id"),
            "generated_at": employee_result.get("generated_at"),
            "historical_factor_gate_passed": (
                employee_result.get("historical_factor_gate_passed") is True
            ),
            "validation_net_spread_after_full_base_cost": employee_metric(
                "validation", "mean_high_minus_low_after_full_base_cost"
            ),
            "untouched_confirmation_net_spread_after_full_base_cost": employee_metric(
                "untouched_confirmation", "mean_high_minus_low_after_full_base_cost"
            ),
            "production_allowed": employee_result.get("production_allowed") is True,
            "report_path": str(
                _EMPLOYMENT_FACTOR_RESULT_PATH.relative_to(_PROJECT_ROOT)
            ),
            "report_sha256": _file_sha256(_EMPLOYMENT_FACTOR_RESULT_PATH),
        } if employee_result else None,
        "report_path": "research/REMAINING_HIGH_QUALITY_SOURCE_FRONTIER_V1.json",
        "report_sha256": _file_sha256(source),
    }


def _source_url(repository: object, commit: object, path: object) -> str | None:
    if not all(isinstance(value, str) and value for value in (
        repository, commit, path,
    )):
        return None
    return f"{repository}/blob/{commit}/{path}"


def _source_brief(source: dict) -> dict:
    license_value = source.get("license") or {}
    paper = source.get("paper") or {}
    repository = source.get("repository")
    commit = source.get("commit")
    return {
        "source_id": source.get("source_id"),
        "upstream_factor": source.get("upstream_factor"),
        "repository": repository,
        "commit": commit,
        "source_path": source.get("source_path"),
        "source_url": _source_url(
            repository, commit, source.get("source_path"),
        ),
        "license": license_value.get("spdx"),
        "license_url": _source_url(
            repository, commit, license_value.get("path"),
        ) or license_value.get("license_url"),
        "paper_title": paper.get("title"),
        "paper_url": paper.get("official_url") or paper.get("primary_url"),
    }


def _factor_source_gate_summary(rows=(), *, legacy_unsourced: int | None = None,
                                registry_total: int | None = None) -> dict:
    """Return the small public source-gate contract, never source documents."""
    from factor_system import source_gate

    materialized = [dict(row) for row in rows]
    if legacy_unsourced is None:
        legacy_unsourced = sum(
            not row.get("research_record_json") for row in materialized
        )
    if registry_total is None:
        registry_total = len(materialized)
    try:
        catalog = source_gate.load_catalog()
        summary = source_gate.catalog_summary(catalog)
        superseded_by = {
            str(item.get("candidate_id")): str(item.get("superseded_by"))
            for item in catalog.get("superseded_non_outcome_trials") or []
            if isinstance(item, dict) and item.get("candidate_id")
            and item.get("superseded_by")
        }
        sources = {
            source.get("source_id"): source
            for source in catalog.get("sources") or []
            if isinstance(source, dict) and source.get("source_id")
        }
        candidates = []
        for candidate in catalog.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            verification = source_gate.verify_candidate(candidate, catalog)
            candidate_id = str(candidate.get("candidate_id") or "")
            terminal = verification.get("status") in {"blind_rejected"}
            candidates.append({
                "candidate_id": candidate_id,
                "name": candidate.get("name"),
                "evidence_basis": candidate.get("evidence_basis"),
                "status": verification.get("status"),
                "valid": verification.get("valid") is True,
                "runnable": verification.get("runnable") is True,
                "errors": list(verification.get("errors") or []),
                "warnings": list(verification.get("warnings") or []),
                "actionable": candidate_id not in superseded_by and not terminal,
                "terminal": terminal,
                "superseded_by": superseded_by.get(candidate_id),
                "sources": [
                    _source_brief(sources[source_id])
                    for source_id in verification.get("source_ids") or []
                    if source_id in sources
                ],
            })
        frontier = [item for item in candidates if item["actionable"]]
        summary.update({
            "frontier_candidate_count": len(frontier),
            "frontier_candidate_ids": [
                item["candidate_id"] for item in frontier
            ],
            "superseded_candidate_count": len(candidates) - len(frontier),
            "actionable_implementation_pending_count": sum(
                item.get("status") == "implementation_pending"
                for item in frontier
            ),
            "actionable_data_pending_count": sum(
                item.get("status") == "data_pending" for item in frontier
            ),
            "actionable_runnable_count": sum(
                item.get("runnable") is True for item in frontier
            ),
            "blind_rejected_count": sum(
                item.get("status") == "blind_rejected" for item in candidates
            ),
        })
    except Exception as exc:
        summary = {
            "valid": False,
            "errors": [f"source_catalog_unavailable:{type(exc).__name__}"],
            "warnings": [],
            "catalog_sha256": None,
            "source_count": 0,
            "candidate_count": 0,
            "evidence_based_count": 0,
            "implementation_pending_count": 0,
            "data_pending_count": 0,
            "runnable_count": 0,
            "contaminated_trial_count": 0,
            "archived_prior_test_count": 0,
            "frontier_candidate_count": 0,
            "frontier_candidate_ids": [],
            "superseded_candidate_count": 0,
            "actionable_implementation_pending_count": 0,
            "actionable_data_pending_count": 0,
            "actionable_runnable_count": 0,
            "blind_rejected_count": 0,
        }
        candidates = []

    blockers = []
    findings = []
    if not summary.get("valid"):
        blockers.append({
            "code": "source_catalog_invalid",
            "message": "来源目录校验失败；所有候选保持不可运行。",
            "system_fault": True,
        })
    elif int(summary.get("runnable_count") or 0) == 0:
        pending = int(
            summary.get("actionable_implementation_pending_count") or 0
        )
        data_pending = int(summary.get("actionable_data_pending_count") or 0)
        blind_rejected = int(summary.get("blind_rejected_count") or 0)
        reasons = []
        if data_pending:
            reasons.append(
                f"{data_pending} 个候选已完成实现审计、仍缺冻结输入证据"
            )
        if pending:
            reasons.append(f"{pending} 个候选尚未完成本地实现与 PIT 审计")
        if blind_rejected:
            reasons.append(
                f"{blind_rejected} 个候选已完成一次盲测并被永久拒绝"
            )
        if not reasons:
            reasons.append("没有候选满足可运行条件")
        finding = {
            "code": (
                "source_candidate_data_pending"
                if data_pending and not pending else "source_candidates_not_runnable"
                if pending or data_pending else "source_hypotheses_exhausted"
            ),
            "message": "来源目录有效，但当前 runnable=0；" + "；".join(reasons) + "。",
            "system_fault": False,
        }
        (blockers if pending or data_pending else findings).append(finding)
    if legacy_unsourced:
        findings.append({
            "code": "legacy_unsourced_registry",
            "message": (
                f"旧注册表 {legacy_unsourced} 条记录均为 legacy_unsourced，"
                "不计入可运行或投产。"
            ),
            "system_fault": False,
        })
    return {
        **summary,
        "candidates": candidates,
        "legacy_unsourced_count": legacy_unsourced,
        "registry_sourced_count": max(0, registry_total - legacy_unsourced),
        "blind_gp_enabled": False,
        "paid_api_allowed": False,
        "github_role": "candidate_source_only",
        "blockers": blockers,
        "findings": findings,
    }


def _factor_row_source_gate(row: dict) -> dict:
    """Verify a registry row against its frozen recipe; old rows fail closed."""
    from factor_system import config as fconfig
    from factor_system import source_gate

    raw_record = row.get("research_record_json")
    if not raw_record:
        return {
            "candidate_id": None,
            "status": "legacy_unsourced",
            "record_status": "legacy_unsourced",
            "valid": False,
            "runnable": False,
            "errors": ["research_record_missing"],
            "warnings": [],
            "research_record_sha256": None,
            "sources": [],
        }
    try:
        record = (
            json.loads(raw_record) if isinstance(raw_record, str)
            else dict(raw_record)
        )
        candidate = record.get("candidate") or {}
        factor_id = str(row.get("factor_id") or "")
        catalog = source_gate.load_catalog()
        contaminated = next((
            trial for trial in catalog.get("contaminated_trials") or []
            if isinstance(trial, dict)
            and trial.get("candidate_id") == candidate.get("candidate_id")
            and trial.get("research_record_sha256")
                == row.get("research_record_sha256")
            and trial.get("status") == "rejected_after_outcome_observed"
            and trial.get("result_data_read") is True
            and trial.get("runnable") is False
        ), None)
        sources = [
            _source_brief(source) for source in record.get("sources") or []
            if isinstance(source, dict)
        ]
        if contaminated is not None:
            return {
                "candidate_id": candidate.get("candidate_id"),
                "status": "contaminated_trial",
                "record_status": contaminated.get("status"),
                "valid": True,
                "runnable": False,
                "errors": [],
                "warnings": [str(contaminated.get("reason") or "")],
                "research_record_sha256": row.get("research_record_sha256"),
                "sources": sources,
            }
        catalog_check = source_gate.verify_catalog(catalog)
        if not catalog_check["valid"]:
            raise ValueError("source catalog invalid")
        canonical = next((
            item for item in catalog.get("candidates") or []
            if isinstance(item, dict)
            and item.get("candidate_id") == candidate.get("candidate_id")
        ), None)
        canonical_check = source_gate.verify_candidate(canonical, catalog)
        if (canonical_check.get("valid") is True
                and canonical_check.get("status") == "blind_rejected"
                and row.get("status") == "rejected"):
            blind = canonical.get("blind_evaluation") or {}
            authorization_binding = blind.get("authorization") or {}
            evidence_binding = blind.get("evidence") or {}
            authorization = json.loads((
                fconfig.PROJECT_ROOT / authorization_binding["path"]
            ).read_text("utf-8"))
            evidence = json.loads((
                fconfig.PROJECT_ROOT / evidence_binding["path"]
            ).read_text("utf-8"))
            if (((authorization.get("candidate") or {}).get(
                    "research_record_sha256"
                ) == row.get("research_record_sha256"))
                    and evidence.get("candidate_id")
                    == candidate.get("candidate_id")
                    and evidence.get("authorization_sha256")
                    == authorization_binding.get("sha256")
                    and evidence.get("final_status") == "rejected"
                    and (evidence.get("l3_fdr") or {}).get("reject") is False):
                return {
                    "candidate_id": candidate.get("candidate_id"),
                    "status": "blind_rejected",
                    "record_status": "blind_rejected",
                    "valid": True,
                    "runnable": False,
                    "errors": [],
                    "warnings": list(canonical_check.get("warnings") or []),
                    "research_record_sha256": row.get(
                        "research_record_sha256"
                    ),
                    "sources": sources,
                }
        recipe = json.loads(
            (fconfig.REGISTRY_DIR / "recipes" / f"{factor_id}.json")
            .read_text(encoding="utf-8")
        )
        holder = {
            "source_candidate_id": candidate.get("candidate_id"),
            "status": candidate.get("status"),
            "runnable": candidate.get("runnable"),
            "research_record": record,
            "research_record_sha256": row.get("research_record_sha256"),
        }
        result = source_gate.verify_frozen_record(holder, recipe)
        return {
            key: result.get(key) for key in (
                "candidate_id", "record_status", "valid", "status",
                "runnable", "errors", "warnings", "research_record_sha256",
            )
        } | {"sources": sources}
    except Exception as exc:
        return {
            "candidate_id": None,
            "status": "source_audit_failed",
            "record_status": "source_audit_failed",
            "valid": False,
            "runnable": False,
            "errors": [f"frozen_source_record_unverifiable:{type(exc).__name__}"],
            "warnings": [],
            "research_record_sha256": row.get("research_record_sha256"),
            "sources": [],
        }


def _factor_production_catalog_summary() -> dict:
    """复核信号消费者实际可读取的当前 production 目录。"""
    try:
        from factor_system import production as factor_production

        catalog = factor_production.verified_catalog()
        factors = catalog.get("factors") or {}
        as_of_values = [
            str((row.get("score_artifact") or {}).get("as_of") or "")
            for row in factors.values() if isinstance(row, dict)
        ]
        return {
            "healthy": True,
            "consumer_ready": bool(catalog.get("complete") and factors),
            "deployable_count": len(factors),
            "factor_ids": sorted(factors),
            "catalog_version": catalog.get("catalog_version"),
            "updated_at": catalog.get("updated_at"),
            "score_as_of": max(as_of_values) if as_of_values else None,
            "source": catalog.get("source") or (
                "factor_system/registry/registry.db:production_catalog"
            ),
            "error": None,
        }
    except Exception as exc:
        return {
            "healthy": False,
            "consumer_ready": False,
            "deployable_count": 0,
            "factor_ids": [],
            "catalog_version": None,
            "updated_at": None,
            "score_as_of": None,
            "source": "factor_system/registry/registry.db:production_catalog",
            "error": f"production_catalog_unavailable:{type(exc).__name__}",
        }


_PRODUCTION_EVALUATOR_PATHS = tuple(
    _PROJECT_ROOT / "factor_system" / relative
    for relative in (
        "config.py", "search.py", "templates.py", "operators.py",
        "preprocess.py", "data.py", "v4_panel.py", "source_gate.py",
        "validate.py", "metrics.py", "gate.py", "backtest.py", "pipeline.py",
        "registry.py", "production.py", "../quantqc/costs.py",
        "../quantqc/stats.py",
    )
)


def _production_catalog_status_identity() -> tuple:
    registry_path = _PROJECT_ROOT / "factor_system" / "registry" / "registry.db"
    return _paths_stat_identity((
        registry_path,
        Path(f"{registry_path}-wal"),
        *_PRODUCTION_EVALUATOR_PATHS,
    ))


def _multiyear_status_identity() -> tuple:
    checkpoint_path = _FIRST_FILING_2024_CHECKPOINT_PATH
    return _paths_stat_identity((
        _FIRST_FILING_V21_ADMISSION_PATH,
        _FIRST_FILING_V21_TICKET_PATH,
        _FIRST_FILING_V21_RECEIPT_PATH,
        checkpoint_path,
        Path(f"{checkpoint_path}-wal"),
        _FIRST_FILING_V21_PAUSE_PATH,
        _FIRST_FILING_V21_RUN_PATH,
        _FIRST_FILING_2025_TERMINAL_PATH,
        _FIRST_FILING_V21_RECOVERY1_ADJUDICATION_PATH,
        _FIRST_FILING_V21_RECOVERY1_ADMISSION_PATH,
        _FIRST_FILING_V21_RECOVERY1_TICKET_PATH,
        _FIRST_FILING_V21_RECOVERY1_RECEIPT_PATH,
        _FIRST_FILING_V21_RECOVERY1_PAUSE_PATH,
        _FIRST_FILING_V21_RECOVERY1_RUN_PATH,
        _FIRST_FILING_V21_RECOVERY2_AMENDMENT_PATH,
        _FIRST_FILING_V21_RECOVERY2_ADMISSION_PATH,
        _FIRST_FILING_V21_RECOVERY2_TICKET_PATH,
        _FIRST_FILING_V21_RECOVERY2_RECEIPT_PATH,
        _FIRST_FILING_V21_RECOVERY2_PAUSE_PATH,
        _FIRST_FILING_V21_RECOVERY2_RUN_PATH,
        _FIRST_FILING_V21_RECOVERY2_TERMINAL_AUDIT_PATH,
        _FIRST_FILING_V21_RECOVERY3_AMENDMENT_PATH,
        _FIRST_FILING_V21_RECOVERY3_ADMISSION_PATH,
        _FIRST_FILING_V21_RECOVERY3_TICKET_PATH,
        _FIRST_FILING_V21_RECOVERY3_RECEIPT_PATH,
        _FIRST_FILING_V21_RECOVERY3_PAUSE_PATH,
        _FIRST_FILING_V21_RECOVERY3_RUN_PATH,
        _FIRST_FILING_V21_RECOVERY3_TERMINAL_AUDIT_PATH,
        _FIRST_FILING_STATEMENT_PARSER_PATH,
    ))


def _cached_factor_production_catalog_summary() -> dict:
    return _identity_cached_status(
        "factor_production_catalog",
        lambda: (
            _production_catalog_status_identity(),
            id(_factor_production_catalog_summary),
        ),
        _factor_production_catalog_summary,
        cacheable=lambda value: (
            value.get("healthy") is True
            and int(value.get("deployable_count") or 0) == 0
        ),
        unstable=lambda: {
            "healthy": False,
            "consumer_ready": False,
            "deployable_count": 0,
            "factor_ids": [],
            "catalog_version": None,
            "updated_at": None,
            "score_as_of": None,
            "source": "factor_system/registry/registry.db:production_catalog",
            "error": "production_catalog_identity_changed_during_status_read",
        },
    )


def _cached_multiyear_first_filing_input_research() -> dict:
    return _identity_cached_status(
        "multiyear_first_filing_input",
        lambda: (
            _multiyear_status_identity(),
            id(_active_multiyear_first_filing_input_research),
        ),
        _active_multiyear_first_filing_input_research,
        cacheable=lambda value: value.get("available") is True,
        unstable=lambda: {
            "available": False,
            "active": False,
            "status": "multiyear_official_pit_identity_changed_during_status_read",
            "identity_valid": False,
            "factor_values_computed": False,
            "prices_or_returns_read": False,
            "ic_or_backtest_run": False,
            "production_allowed": False,
        },
    )


def _factor_forward_readiness(
    fconfig,
    backtest_status: dict | None = None,
    production_catalog: dict | None = None,
) -> dict:
    """公开全局新数据概况；明确它不能替代每个因子的 seal 后进度。"""
    metrics = ((backtest_status or {}).get("last_metrics") or {})
    try:
        observed = max(0, int(metrics.get("forward_shadow_days") or 0))
    except (TypeError, ValueError):
        observed = 0
    required = int(fconfig.FORWARD_SHADOW_MIN_DAYS)
    catalog = production_catalog or {}
    return {
        "start": fconfig.FORWARD_SHADOW_START,
        "observed_days": observed,
        "required_days": required,
        # 不再用全局日期数推导单个因子 ready。每个因子生成时间不同，只能以
        # sealed_data_end 之后 120 个完整标签日逐项判断。
        "ready": (
            bool(catalog.get("consumer_ready"))
            if production_catalog is not None
            else bool(metrics.get("production"))
        ),
        "scope": "global_calendar_only_not_factor_readiness",
        "auto_deploy_on_all_gates_pass": True,
        "manual_promotion_required": False,
        "production_registry": "factor_system/registry/registry.db",
        "verified_production_count": int(catalog.get("deployable_count") or 0),
        "production_consumer_ready": bool(catalog.get("consumer_ready")),
        "production_catalog_version": catalog.get("catalog_version"),
        "production_catalog_error": catalog.get("error"),
        "verified_factor_ids": list(catalog.get("factor_ids") or []),
        "production_rule": (
            f"仅因子 seal 后新增数据形成至少 {required} 个完整前向标签，"
            "并通过固定终测与全局 LOND；最后一道门禁通过后同一事务自动写入 production"
        ),
    }


def _public_factor_status_counts(rows, *, research_epoch: str) -> dict[str, int]:
    """按公开生命周期口径计数；旧 epoch 一律只作为失效证据。"""
    counts: dict[str, int] = {}
    for raw in rows:
        status = str(raw["status"] if not isinstance(raw, dict) else raw.get("status")
                     or "candidate")
        epoch = (raw["research_epoch"] if not isinstance(raw, dict)
                 else raw.get("research_epoch"))
        if epoch != research_epoch:
            status = "invalidated"
        counts[status] = counts.get(status, 0) + 1
    return counts


def _public_factor_row(raw: dict, *, research_epoch: str,
                       forward_readiness: dict) -> dict:
    """把注册表行转换为不夸大历史证据的公开口径。"""
    row = dict(raw)
    source_gate = _factor_row_source_gate(row)
    row["source_gate"] = source_gate
    row["source_eligible"] = source_gate.get("runnable") is True
    row.pop("research_record_json", None)
    source_status = str(row.get("status") or "candidate")
    epoch = row.get("research_epoch")
    stale_lifecycle = epoch != research_epoch
    invalidated = source_status == "invalidated" or stale_lifecycle
    row["source_status"] = source_status
    row["research_epoch"] = epoch
    row["current_research_epoch"] = research_epoch
    row_forward = dict(forward_readiness)
    row_forward.update({
        "sealed_at": row.get("sealed_at"),
        "sealed_data_end": row.get("sealed_data_end"),
        "factor_specific": True,
    })
    row["forward_shadow_readiness"] = row_forward

    if invalidated:
        row["status"] = "invalidated"
        row["verdict"] = "INVALIDATED"
        row["tier"] = "t4_invalidated"
        # 旧 backtest_evidence 仍留在 SQLite/证据文件中供追溯，但不再经公开
        # API 当作正面表现展示；路径和哈希继续公开，方便审计原始记录。
        row["backtest_evidence"] = None
        for key in (
            "oos_ic_mean", "oos_ic_ir", "backtest_annual_return",
            "backtest_sharpe", "backtest_max_drawdown", "backtest_win_rate",
            "backtest_excess_annual", "backtest_benchmark_annual",
            "backtest_fold_pass_rate", "backtest_t_stat", "oos_test_p_value",
        ):
            row[key] = None
        reason = row.get("status_reason") or "旧研究 epoch/旧验证口径已失效"
        source_note = (
            "；source_gate=legacy_unsourced，不可用于新研究或策略"
            if source_gate.get("status") == "legacy_unsourced" else ""
        )
        row["audit_message"] = (
            f"已失效：{reason}；旧历史结果仅可按证据路径追溯，不计 PASS"
            f"{source_note}。"
        )
    elif source_status == "historical_pass":
        row["verdict"] = "HISTORICAL-PASS"
        row["tier"] = "t2_historical_confirmation"
        row["audit_message"] = (
            "历史确认通过，但这不是未触碰样本外或投产结论；"
            f"仍需晚于自身 seal 数据尾 {row.get('sealed_data_end') or '未冻结'}，且不早于 "
            f"{forward_readiness['start']} 的新数据形成 {forward_readiness['required_days']} 个"
            "完整标签日，再做固定前向终测。"
        )
    elif source_status == "production":
        consumer_ready = bool(
            source_gate.get("runnable") is True
            and row.get("factor_id") in set(
                forward_readiness.get("verified_factor_ids") or []
            )
        )
        row["consumer_ready"] = consumer_ready
        row["verdict"] = "PRODUCTION" if consumer_ready else "DEPLOYMENT-BLOCKED"
        row["tier"] = "t1_forward_production"
        row["audit_message"] = (
            "当前 production 配方、前向证据与沪深主板+创业板分数制品已复核，"
            "信号消费者可读取。"
            if consumer_ready else
            "注册表写有 production，但可消费目录/分数制品未完整复核；当前禁止进入信号消费者。"
        )
    elif source_status == "rejected":
        row["verdict"] = "NO-GO"
        row["tier"] = "t3_negative"
        row["audit_message"] = row.get("status_reason") or "研究门槛未通过。"
    else:
        row["verdict"] = None
        row["tier"] = "t2_research"
        row["audit_message"] = row.get("status_reason") or "研究流程尚未形成终局结论。"
    if not invalidated and source_gate.get("runnable") is not True:
        source_status_text = str(source_gate.get("status") or "source_audit_failed")
        row["audit_message"] = (
            f"来源门禁未通过（{source_status_text}），当前不可运行；"
            f"{row.get('audit_message') or ''}"
        )
    return row


def _factor_registry_health(exc: Exception | None = None) -> dict:
    if exc is None:
        return {"available": True}
    code = "factor_registry_read_failed"
    message = "因子注册表读取失败"
    reason = "unknown"
    if isinstance(exc, FileNotFoundError):
        code, reason = "factor_registry_unavailable", "missing"
        message = "旧因子研究库文件未恢复；历史因子数量未知，已保存的研究候选可独立查看"
    elif isinstance(exc, PermissionError):
        code, reason = "factor_registry_unavailable", "permission"
        message = "因子研究库读取权限不足；需修复文件访问权限后重试"
    elif isinstance(exc, sqlite3.Error):
        sqlite_code = (getattr(exc, "sqlite_errorcode", 0) or 0) & 255
        if str(exc) == "no such table: factors":
            code = "factor_registry_not_initialized"
            reason = "schema_missing"
            message = "因子研究库缺少原始表结构；需恢复可核验的研究库"
        elif sqlite_code in {
            sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_CORRUPT,
            sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_IOERR,
            sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
            sqlite3.SQLITE_PERM, sqlite3.SQLITE_AUTH,
        }:
            code = "factor_registry_unavailable"
            reason, message = ({
                sqlite3.SQLITE_BUSY: ("busy", "因子研究库正被占用；请稍后重新读取"),
                sqlite3.SQLITE_LOCKED: ("busy", "因子研究库正被占用；请稍后重新读取"),
                sqlite3.SQLITE_CORRUPT: ("corrupt", "因子研究库文件损坏；需从可核验备份恢复"),
                sqlite3.SQLITE_NOTADB: ("corrupt", "因子研究库文件格式无效；需恢复正确的数据库文件"),
                sqlite3.SQLITE_PERM: ("permission", "因子研究库读取权限不足；需修复访问权限"),
                sqlite3.SQLITE_AUTH: ("permission", "因子研究库读取权限不足；需修复访问权限"),
                sqlite3.SQLITE_IOERR: ("storage", "因子研究库发生存储读取错误；需检查存储状态"),
            }).get(sqlite_code, ("cannot_open", "因子研究库无法打开；需检查路径、权限与存储状态"))
    return {"available": False, "code": code, "reason": reason, "message": message}


def _factor_registry_error_response(app, exc: Exception, public_message: str):
    health = _factor_registry_health(exc)
    unavailable = health["code"] != "factor_registry_read_failed"
    response, status = internal_error_response(
        app, health["message"] if unavailable else public_message, exc,
        status=503 if unavailable else 500,
        extra={"code": health["code"], "registry_health": health},
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response, status


def _backtest_factor_payload(*, sort: str, limit: int) -> dict:
    """Build the historical-factor view from one bounded registry page."""
    import factor_system.config as fconfig
    import factor_system.registry as freg
    import factor_system.status as fstatus

    c = freg._conn(readonly=True)
    try:
        summary = freg.public_registry_summary(
            c, research_epoch=fconfig.RESEARCH_EPOCH,
        )
        rows, _ = freg.public_factor_page(
            c, research_epoch=fconfig.RESEARCH_EPOCH,
            sort=sort if sort in {"abs_ic", "ic", "icir"} else "abs_ic",
            limit=limit,
        )
    finally:
        c.close()
    readiness = _factor_forward_readiness(
        fconfig, fstatus.read_zone("backtest"),
    )
    public_rows = [
        _public_factor_row(
            row, research_epoch=fconfig.RESEARCH_EPOCH,
            forward_readiness=readiness,
        )
        for row in rows
    ]
    factors = []
    for row in public_rows:
        raw_evidence = row.get("backtest_evidence")
        try:
            evidence = (
                json.loads(raw_evidence)
                if isinstance(raw_evidence, str) else raw_evidence
            ) or {}
        except (TypeError, json.JSONDecodeError):
            evidence = {}
        historical = evidence.get("oos_ic") or {}
        fold_pass = evidence.get("fold_pass") or {}
        ic = (
            historical.get("ic_mean")
            if historical.get("ic_mean") is not None
            else row.get("oos_ic_mean")
        )
        ir = (
            historical.get("ic_ir")
            if historical.get("ic_ir") is not None
            else row.get("oos_ic_ir")
        )
        factors.append({
            "factor": row.get("name") or row.get("factor_id"),
            "factor_id": row.get("factor_id"),
            "group": row.get("family") or "",
            "status": row.get("status", "candidate"),
            "verdict": row.get("verdict") or evidence.get("verdict"),
            "tier": row.get("tier"),
            "mean_ic": ic,
            "historical_ic_mean": ic,
            "icir": ir,
            "ic_t": historical.get("t_stat"),
            "ic_pos_ratio": None,
            "fold_pass_rate": fold_pass.get("pass_rate") if fold_pass else None,
            "reason": row.get("audit_message") or evidence.get("reason") or "",
            "audit_message": row.get("audit_message"),
            "research_epoch": row.get("research_epoch"),
            "current_research_epoch": fconfig.RESEARCH_EPOCH,
            "evidence_path": row.get("evidence_path"),
            "evidence_sha256": row.get("evidence_sha256"),
        })
    counts = summary["current"]["by_status"]
    n_invalidated = (
        summary["archive"]["total"] + int(counts.get("invalidated") or 0)
    )
    audit_message = (
        f"当前研究 epoch={fconfig.RESEARCH_EPOCH}。2024-07~2025-12 仅作历史确认，"
        "不能作为全新前向结论；"
        f"历史确认通过 {int(counts.get('historical_pass') or 0)}、"
        f"已失效 {n_invalidated}、拒绝 {int(counts.get('rejected') or 0)}。"
        f"production {int(counts.get('production') or 0)} 个，仅允许来自逐因子 seal 后前向终测。"
        f"历史科学闸门 NO-GO {summary['nogo']}、FDR 拒绝 {summary['fdr']}、"
        f"无可展示证据 {summary['no_evidence']}。"
        "已失效行不读取旧 backtest_evidence 生成评级；证据路径/哈希仅供追溯。"
    )
    return {
        "factors": factors,
        "count": len(factors),
        "total_factors": summary["total"],
        "invalidated": False,
        "invalidated_records": n_invalidated,
        "source": "factor_system.scientific_registry",
        "research_epoch": fconfig.RESEARCH_EPOCH,
        "forward_shadow_readiness": readiness,
        "audit_message": audit_message,
    }


_FACTOR_RESEARCH_PIPELINE_STATUSES = {
    "candidate", "validated", "registered", "backtesting",
    "historical_pass", "production",
}
_FACTOR_RESEARCH_STRATEGY_STATUSES = {"historical_pass", "production"}
_FACTOR_PUBLIC_STATUSES = _FACTOR_RESEARCH_PIPELINE_STATUSES | {
    "rejected", "frozen", "invalidated",
}


def _factor_registry_summary(rows, *, research_epoch: str) -> dict:
    materialized = [dict(row) for row in rows]
    current = [
        row for row in materialized
        if row.get("research_epoch") == research_epoch
    ]
    archive = [
        row for row in materialized
        if row.get("research_epoch") != research_epoch
    ]

    def scope(rows_in_scope):
        by_status = _public_factor_status_counts(
            rows_in_scope, research_epoch=research_epoch,
        )
        pipeline_active = sum(
            by_status.get(status, 0)
            for status in _FACTOR_RESEARCH_PIPELINE_STATUSES
        )
        return {
            "total": len(rows_in_scope),
            "runnable_total": pipeline_active,
            "terminal_total": len(rows_in_scope) - pipeline_active,
            "by_status": by_status,
        }

    as_of_values = [
        str(row["updated_at"]) for row in materialized if row.get("updated_at")
    ]
    as_of = max(as_of_values) if as_of_values else None
    summary = {"current": scope(current), "archive": scope(archive)}
    eligibility = {
        name: {
            "runnable_scope": "research_pipeline_only",
            "research_pipeline_active_total": row["runnable_total"],
            "research_strategy_eligible_total": sum(
                row["by_status"].get(status, 0)
                for status in _FACTOR_RESEARCH_STRATEGY_STATUSES
            ),
            "production_consumer_ready_total": int(
                row["by_status"].get("production", 0)
            ),
        }
        for name, row in summary.items()
    }
    version = hashlib.sha256(json.dumps({
        "research_epoch": research_epoch,
        "as_of": as_of,
        "counts": summary, "eligibility": eligibility,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )).hexdigest()[:16]
    return {
        **summary, "eligibility": eligibility,
        "version": version, "as_of": as_of,
    }


def _factor_information_gain(mining: dict, daemon: dict) -> dict:
    metrics = mining.get("last_metrics") or {}
    counts = metrics.get("counts") or {}
    search = metrics.get("search") or {}
    output = int(search.get("output_candidates", metrics.get("candidates", 0)) or 0)
    validated = int(counts.get("validated") or 0)
    return {
        "source": "factor_discovery",
        "observed_at": mining.get("last_completed") or mining.get("updated_at"),
        "status": (
            "positive" if output > 0 or validated > 0
            else "zero" if (
                mining.get("last_completed")
                or int(daemon.get("zero_info_rounds") or 0) > 0
            ) else "unknown"
        ),
        "output_candidates": output,
        "validated_candidates": validated,
        "fitness_evaluated": int(search.get("fitness_evaluated") or 0),
        "zero_info_rounds": int(daemon.get("zero_info_rounds") or 0),
    }


def _factor_research_workflow(
    *, mining: dict, backtest: dict, daemon: dict, research_lock: dict,
    registry_summary: dict, source_gate: dict | None = None,
    source_scout: dict | None = None,
) -> dict:
    source_gate = source_gate or {}
    source_scout = source_scout or {}
    paper_unreviewed = int(source_scout.get("unreviewed_count") or 0)
    repository_unreviewed = int(
        source_scout.get("repository_discovery_unreviewed_count") or 0
    )
    repository_review_required = int(
        source_scout.get("repository_review_required_count") or 0
    )
    formula_pending = int(source_scout.get("formula_pending_count") or 0)
    source_contract_pending = int(
        source_scout.get("source_contract_pending_count") or 0
    )
    active_source_hypotheses = formula_pending + source_contract_pending
    source_unresolved = {
        "paper_unreviewed": paper_unreviewed,
        "repository_unreviewed": repository_unreviewed,
        "repository_review_required": repository_review_required,
        "unresolved_total": (
            paper_unreviewed
            + repository_unreviewed
            + repository_review_required
        ),
    }
    current_counts = registry_summary["current"]
    by_status = current_counts["by_status"]
    daemon_status = str(daemon.get("daemon") or "stopped")
    supervisor_online = bool(
        daemon_status == "running"
        and daemon.get("process_alive") is True
        and daemon.get("heartbeat_fresh") is True
    )
    progress_fresh = daemon.get("progress_fresh") is not False
    current_zone = str(daemon.get("current_zone") or "")
    daemon_phase = str(daemon.get("phase") or "")
    waiting_phase = (
        daemon_phase if daemon_phase in {
            "await_new_input", "needs_new_hypothesis",
        } else current_zone if current_zone in {
            "await_new_input", "needs_new_hypothesis",
        } else None
    )
    terminal_source_exhausted = bool(
        source_gate.get("valid") is True
        and int(source_gate.get("runnable_count") or 0) == 0
        and int(source_gate.get("blind_rejected_count") or 0) > 0
        and int(source_gate.get(
            "actionable_implementation_pending_count", 0,
        ) or 0) == 0
        and int(source_gate.get(
            "actionable_data_pending_count", 0,
        ) or 0) == 0
        and active_source_hypotheses == 0
    )
    daemon_active = bool(
        supervisor_online
        and progress_fresh
        and daemon_phase == "work"
        and current_zone in {
            "source_research", "source_audit", "gp", "full_mining",
            "backtest", "revalidate",
        }
    )
    supervisor_recovery_needed = bool(
        daemon_status == "running" and not supervisor_online
    )
    progress_stalled = bool(supervisor_online and not progress_fresh)
    inactive_phases = {
        "", "pacing", "done", "idle", "completed",
        "await_new_input", "needs_new_hypothesis",
    }
    mining_running = bool(
        not supervisor_recovery_needed
        and not progress_stalled
        and
        str(mining.get("status") or "") == "running"
        and str(mining.get("phase") or "") not in inactive_phases
    )
    backtest_running = bool(
        not supervisor_recovery_needed
        and not progress_stalled
        and
        str(backtest.get("status") or "") == "running"
        and str(backtest.get("phase") or "") not in inactive_phases
    )
    lock_busy = research_lock.get("busy") is True
    lock_metadata = research_lock.get("metadata") or {}
    lock_pid = research_lock.get("pid") or lock_metadata.get("pid")
    daemon_pid = daemon.get("process_pid")
    lock_matches_daemon = bool(
        lock_busy and daemon_active and lock_pid and daemon_pid
        and str(lock_pid) == str(daemon_pid)
    )
    runtime_active = bool(
        not supervisor_recovery_needed and not progress_stalled
        and (daemon_active or mining_running or backtest_running or lock_busy)
    )
    blockers = []
    research_findings = []

    if daemon_status == "error" and not terminal_source_exhausted:
        blockers.append({
            "code": "factor_daemon_error",
            "message": str(daemon.get("last_error") or "因子 supervisor 报告错误"),
        })
    if (
        daemon_status == "running"
        and daemon.get("process_alive") is not True
        and not terminal_source_exhausted
    ):
        blockers.append({
            "code": "daemon_process_unavailable",
            "message": "Supervisor 状态为 running，但 daemon PID 当前不可用",
        })
    if (
        daemon_status == "running"
        and daemon.get("heartbeat_fresh") is not True
        and not terminal_source_exhausted
    ):
        heartbeat_age = daemon.get("heartbeat_age_seconds")
        age_text = (
            f"已间隔 {heartbeat_age} 秒"
            if isinstance(heartbeat_age, (int, float)) else "时间不可判定"
        )
        blockers.append({
            "code": "daemon_heartbeat_stale",
            "message": f"Supervisor 心跳过期（{age_text}，健康阈值 120 秒）",
        })
    if progress_stalled and not terminal_source_exhausted:
        blockers.append({
            "code": "daemon_progress_stalled",
            "message": (
                "Supervisor 任务进度超过看门狗上限："
                f"{daemon.get('progress_age_seconds')} / "
                f"{daemon.get('progress_timeout_seconds')} 秒"
            ),
        })
    ingestion_blocker = daemon.get("ingestion_blocker")
    if isinstance(ingestion_blocker, dict) and ingestion_blocker:
        blockers.append({
            "code": str(
                ingestion_blocker.get("code") or "factor_data_ingestion_blocked"
            ),
            "message": str(
                ingestion_blocker.get("message")
                or ingestion_blocker.get("error")
                or "新输入摄取失败，Supervisor 正使用最近一份已验证数据"
            ),
            "at": ingestion_blocker.get("at"),
        })
    # 没有存量候选是发现区的工作原因，不是阻止一个在线 supervisor 继续搜索的
    # blocker。只有 supervisor 不在线时，才把它作为需要恢复服务的阻塞展示。
    if (
        current_counts["runnable_total"] == 0
        and not supervisor_online
        and not terminal_source_exhausted
    ):
        blockers.append({
            "code": "no_current_runnable_factors",
            "message": (
                f"当前 strict epoch 注册表共 {current_counts['total']} 条，"
                f"可继续研究流程 0 条，终局淘汰/失效 {current_counts['terminal_total']} 条"
            ),
        })
    if lock_busy and not lock_matches_daemon:
        blockers.append({
            "code": "research_lock_busy",
            "message": (
                f"真实跨进程研究写锁由 PID {lock_pid or '未知'} 持有，"
                "无法归属于当前因子活动运行区"
            ),
        })
    for finding in source_gate.get("findings") or []:
        if isinstance(finding, dict):
            research_findings.append({**finding, "scope": "source_gate"})
    for blocker in source_gate.get("blockers") or []:
        if isinstance(blocker, dict):
            target = (
                research_findings
                if terminal_source_exhausted
                and blocker.get("system_fault") is False
                else blockers
            )
            target.append({**blocker, "scope": "source_gate"})
    if source_scout.get("last_attempt_at"):
        research_findings.append({
            "code": "public_source_scout_research_only",
            "message": (
                "公开论文元数据每日侦察已运行："
                f"已审 {int(source_scout.get('reviewed_count') or 0)} 条，"
                f"未审总计 {source_unresolved['unresolved_total']} 条"
                f"（论文 {paper_unreviewed}、GitHub新源 {repository_unreviewed}、"
                f"公式源变更 {repository_review_required}），"
                f"待补公式 {int(source_scout.get('formula_pending_count') or 0)} 条，"
                "待规则合同 "
                f"{int(source_scout.get('source_contract_pending_count') or 0)} 条；"
                "全部仍不可运行。"
            ),
            "scope": "source_scout",
            "system_fault": False,
        })
    if (
        waiting_phase and not terminal_source_exhausted
        and active_source_hypotheses == 0
    ):
        blockers.append({
            "code": waiting_phase,
            "message": str(
                daemon.get("wait_reason")
                or "连续零信息轮次已停损，等待真实研究输入变化"
            ),
            "scope": "factor_discovery",
        })

    if daemon_status == "error":
        current_stage, stage_status = "supervisor_recovery", "blocked"
        next_action = "核对 supervisor 当前错误与输入身份后再恢复研究流程"
    elif progress_stalled:
        current_stage, stage_status = "supervisor_recovery", "blocked"
        next_action = "由看门狗重启停滞轮次后自动接续研究流程"
    elif lock_busy and not lock_matches_daemon:
        current_stage, stage_status = "research_mutation", "running"
        next_action = "等待当前未归属的持锁研究任务完成并刷新版本号"
    elif active_source_hypotheses:
        current_stage, stage_status = "source_audit", "waiting"
        next_action = (
            "新独立假设已经找到；"
            f"待补公式 {formula_pending} 条、输入/评估合同 "
            f"{source_contract_pending} 条。"
            "完成可信PIT输入、月频成本评估与去重合同前不读取收益；"
            "不重跑旧候选。"
        )
    elif terminal_source_exhausted:
        current_stage, stage_status = "needs_new_hypothesis", "waiting"
        if source_unresolved["unresolved_total"]:
            next_action = (
                f"每日公开来源侦察继续运行；人工审查当前 "
                f"{source_unresolved['unresolved_total']} 条未决来源"
                f"（论文 {paper_unreviewed}、GitHub新源 {repository_unreviewed}、"
                f"公式源变更 {repository_review_required}），"
                "只有通过许可、PIT 与预注册后才可登记新假设；禁止重跑已拒绝候选"
            )
        else:
            next_action = (
                "每日公开来源侦察继续运行；只推进已审候选的明确缺口："
                f"待补公式 {int(source_scout.get('formula_pending_count') or 0)} 条，"
                "待规则合同 "
                f"{int(source_scout.get('source_contract_pending_count') or 0)} 条；"
                "未冻结前禁止计算因子或收益；禁止重跑已拒绝候选"
            )
    elif supervisor_recovery_needed:
        current_stage, stage_status = "supervisor_recovery", "blocked"
        next_action = "恢复 Supervisor 进程与独立心跳后自动接续研究流程"
    elif waiting_phase == "await_new_input":
        current_stage, stage_status = waiting_phase, "waiting"
        next_action = "等待 panel、原子或其他冻结研究输入身份发生变化"
    elif waiting_phase == "needs_new_hypothesis":
        current_stage, stage_status = waiting_phase, "waiting"
        next_action = "登记新的独立经济假设；禁止只换随机种子重跑"
    elif daemon_active or mining_running or backtest_running:
        if current_zone == "source_research":
            current_stage = "source_research"
        elif current_zone == "source_audit":
            current_stage = "source_audit"
        elif current_zone == "backtest" or backtest_running:
            current_stage = "historical_confirmation"
        elif current_zone == "revalidate":
            current_stage = "forward_revalidation"
        else:
            current_stage = "factor_discovery"
        stage_status = "running"
        next_action = str(
            daemon.get("next_plan") or "等待当前真实输入驱动的研究轮次完成"
        )
    elif source_gate and (
        source_gate.get("valid") is not True
        or int(source_gate.get("runnable_count") or 0) == 0
    ):
        current_stage, stage_status = (
            ("needs_new_hypothesis", "waiting")
            if terminal_source_exhausted else ("source_audit", "blocked")
        )
        next_action = (
            "登记新的独立经济假设；当前盲测试验已永久拒绝，禁止重跑或调参复活"
            if terminal_source_exhausted else
            "先冻结当前前沿候选的 implementation amendment/admission，再实施；通过前不读取候选收益"
            if (source_gate.get("valid") is True
                and int(source_gate.get(
                    "actionable_implementation_pending_count", 0,
                ) or 0) > 0)
            else
            "补齐 PriceDelay 冻结 discovery 所需的更早免费 PIT 历史；通过前不读取候选收益"
            if (source_gate.get("valid") is True
                and int(source_gate.get(
                    "actionable_data_pending_count", 0,
                ) or 0) > 0)
            else "完成可行动来源候选的本地实现与 PIT 审计；通过前不进入多重检验"
            if source_gate.get("valid") is True
            else "修复并重新核验来源目录"
        )
    elif supervisor_online:
        current_stage = (
            "supervisor_recovery"
            if current_zone == "memory_recovery" else "factor_discovery"
        )
        stage_status = "pacing"
        next_action = str(
            daemon.get("next_plan") or "短间隔后自动接续下一轮真实研究任务"
        )
    elif current_counts["runnable_total"] == 0:
        current_stage, stage_status = "factor_discovery", "blocked"
        next_action = "使用当前 strict 数据身份挖掘并注册新的非终局候选"
    elif by_status.get("production", 0):
        current_stage, stage_status = "forward_monitoring", "ready"
        next_action = "仅用各因子 seal 后新数据继续固定前向监测"
    elif by_status.get("historical_pass", 0):
        current_stage, stage_status = "forward_shadow", "ready"
        next_action = "等待各因子 seal 后形成足量完整前向标签"
    elif by_status.get("registered", 0) or by_status.get("backtesting", 0):
        current_stage, stage_status = "historical_confirmation", "ready"
        next_action = "运行固定历史确认并写入不可变检验与全局 FDR 证据"
    else:
        current_stage, stage_status = "factor_validation", "ready"
        next_action = "完成发现段与验证段门禁后再注册历史确认候选"

    last_errors = []
    for name, state in (("mining", mining), ("backtest", backtest), ("daemon", daemon)):
        if state.get("last_error"):
            last_errors.append({
                "code": f"archived_{name}_error",
                "message": str(state["last_error"]),
                "at": state.get("last_error_at"),
                "current": False,
            })
    recent_information_gain = _factor_information_gain(mining, daemon)
    source_blocked = bool(source_gate) and (
        source_gate.get("valid") is not True
        or int(source_gate.get("runnable_count") or 0) == 0
    )
    upstream_active = bool(
        supervisor_online and not waiting_phase and not source_blocked
        and (daemon_active or daemon_phase == "pacing")
    )
    blocked_by = list(dict.fromkeys(
        str(row.get("code")) for row in blockers if row.get("code")
    ))
    transition_condition = (
        "new_independent_hypothesis_registered"
        if current_stage == "needs_new_hypothesis"
        else "research_input_identity_changed"
        if waiting_phase == "await_new_input"
        else "new_independent_hypothesis_registered"
        if waiting_phase == "needs_new_hypothesis"
        else "factor_supervisor_process_alive_and_heartbeat_fresh"
        if current_stage == "supervisor_recovery"
        else "source_candidate_passes_local_implementation_and_pit_audit"
        if current_stage == "source_audit"
        else "current_epoch_factor_passes_source_and_validation_gates"
        if current_stage in {"factor_discovery", "factor_validation"}
        else "current_stage_writes_immutable_gate_evidence"
    )
    workflow_version = hashlib.sha256(json.dumps({
        "registry_version": registry_summary["version"],
        "stage": current_stage,
        "status": stage_status,
        "upstream_active": upstream_active,
        "blocked_by": blocked_by,
        "recent_information_gain": recent_information_gain,
        "source_unresolved": source_unresolved,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )).hexdigest()[:16]
    return {
        "current_stage": current_stage,
        "stage_status": stage_status,
        "blockers": blockers,
        "blocked_by": blocked_by,
        "research_findings": research_findings,
        "source_unresolved": source_unresolved,
        "upstream_active": upstream_active,
        "recent_information_gain": recent_information_gain,
        "transition_condition": transition_condition,
        "next_action": next_action,
        "counts": {
            "current": registry_summary["current"],
            "archive": registry_summary["archive"],
        },
        "version": workflow_version,
        "as_of": registry_summary["as_of"],
        "runtime": {
            "active": runtime_active,
            "source": (
                "research_mutation_lock" if lock_busy and not lock_matches_daemon
                else "source_research_zone"
                if daemon_active and current_zone in {"source_research", "source_audit"}
                else "daemon" if daemon_active
                else "backtest_zone" if backtest_running
                else "mining_zone" if mining_running
                else "source_contract_work"
                if active_source_hypotheses
                else "needs_new_hypothesis" if terminal_source_exhausted
                else waiting_phase if waiting_phase
                else "supervisor_online" if supervisor_online
                else "stopped"
            ),
            "lock_busy": lock_busy,
            "lock_pid": lock_pid,
            "lock_matches_daemon": lock_matches_daemon,
            "daemon_status": daemon_status,
            "supervisor_process_online": supervisor_online,
            "daemon_active": daemon_active,
            "heartbeat_fresh": daemon.get("heartbeat_fresh") is True,
            "progress_fresh": progress_fresh,
            "daemon_process_pid": daemon_pid,
            "current_zone": current_zone,
            "supervisor_required": not (
                terminal_source_exhausted or active_source_hypotheses
            ),
        },
        "last_errors": last_errors,
    }


def _factor_zone_operational_view(
    state: dict, *, zone: str, workflow: dict,
) -> dict:
    """Separate persisted batch history from the work running right now."""
    out = dict(state or {})
    persisted_status = str(out.get("status") or "stopped")
    phase = str(out.get("phase") or "")
    runtime = workflow.get("runtime") or {}
    current_stage = str(workflow.get("current_stage") or "")
    zone_active = bool(
        runtime.get("active") is True
        and str(runtime.get("source") or "") == f"{zone}_zone"
    )
    waiting = bool(
        (
            persisted_status == "running"
            and current_stage in {"needs_new_hypothesis", "await_new_input"}
        )
        or phase in {"needs_new_hypothesis", "await_new_input"}
    )
    out["persisted_status"] = persisted_status
    if zone_active:
        out.update({
            "status": "running",
            "reported_status": "running",
            "operational_status": "running",
        })
    elif waiting:
        previous_batch = out.pop("current_batch", None)
        out.update({
            "status": "waiting",
            "reported_status": "waiting",
            "operational_status": "waiting",
            "phase": current_stage or phase,
            "operational_reason": (
                "new_independent_hypothesis_required"
                if current_stage == "needs_new_hypothesis"
                else "research_input_identity_unchanged"
            ),
            "detail": (
                "按需研究已收口；等待新的独立经济假设，不自动重跑旧批次"
                if current_stage == "needs_new_hypothesis"
                else "当前输入身份未变化；等待新输入，不自动重跑旧批次"
            ),
        })
        if previous_batch:
            out["last_reported_batch"] = previous_batch
    else:
        public_status = "error" if persisted_status == "error" else "stopped"
        out["status"] = public_status
        out["reported_status"] = public_status
        out["operational_status"] = (
            "error" if persisted_status == "error" else "stopped"
        )
    return out


def _compact_public_factor_row(row: dict) -> dict:
    evidence = row.get("backtest_evidence")
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except json.JSONDecodeError:
            evidence = None
    evidence = evidence if isinstance(evidence, dict) else {}
    oos_ic = evidence.get("oos_ic") or {}
    fold = evidence.get("fold_pass") or {}
    full = evidence.get("full") or {}
    checks = evidence.get("checks") or {}
    fields = (
        "factor_id", "name", "family", "formula", "description", "direction",
        "status", "source_status", "status_reason", "verdict", "tier",
        "research_epoch", "current_research_epoch", "created_at", "updated_at",
        "sealed_at", "sealed_data_end", "sealed_recipe_sha256",
        "is_ic_mean", "is_ic_ir", "raw_ic_mean", "raw_ic_ir",
        "neutralize_mode", "monotonic_aligned", "ic_half_life",
        "turnover_ratio", "capacity_estimate", "incremental_ic",
        "oos_ic_mean", "oos_ic_ir", "ic_retention", "long_short_annualized",
        "backtest_annual_return", "backtest_excess_annual", "backtest_sharpe",
        "backtest_max_drawdown", "backtest_fold_pass_rate", "backtest_t_stat",
        "evidence_path", "evidence_sha256", "audit_message",
        "forward_shadow_readiness", "source_gate", "source_eligible",
    )
    compact = {key: row.get(key) for key in fields}
    source_gate = row.get("source_gate") or {}
    current = row.get("research_epoch") == row.get("current_research_epoch")
    source_runnable = source_gate.get("runnable") is True
    research_pipeline_active = bool(
        current and row.get("status") in _FACTOR_RESEARCH_PIPELINE_STATUSES
        and source_runnable
    )
    research_strategy_eligible = bool(
        current and row.get("status") in _FACTOR_RESEARCH_STRATEGY_STATUSES
        and source_runnable
    )
    production_consumer_ready = bool(
        row.get("status") == "production" and row.get("consumer_ready") is True
    )
    source_blocked_reason = (
        "legacy_unsourced · 不可用"
        if source_gate.get("status") == "legacy_unsourced"
        else f"来源门禁未通过：{source_gate.get('status') or 'source_audit_failed'}"
    )
    compact.update({
        "research_pipeline_active": research_pipeline_active,
        "research_strategy_eligible": research_strategy_eligible,
        "production_consumer_ready": production_consumer_ready,
        # Compatibility alias, now narrowed to its literal meaning.
        "available_for_strategy_run": research_strategy_eligible,
        "blocked_reason": (
            None
            if research_strategy_eligible
            else source_blocked_reason
            if not source_runnable
            else "仅处于因子研究管线；尚未通过历史确认，不进入策略研究"
            if research_pipeline_active
            else row.get("audit_message") or row.get("status_reason")
        ),
        "oos_ic_t_stat": oos_ic.get("t_stat"),
        "fold_passed": fold.get("passed"),
        "fold_count": fold.get("n_folds"),
        "signal_churn": full.get("signal_churn"),
        "churn_flag": full.get("churn_flag"),
        "failed_checks": [
            str(name) for name, passed in checks.items() if not passed
        ],
    })
    return compact


def _published_leader_limit_program(base: Path) -> dict | None:
    """Read the release-bound fallback used when raw research files are absent."""
    path = base / "mom_trading" / "static" / "strategy_evidence_status.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        program = payload["current_research_program"][
            "emotion_conditioned_leader_limit_program"
        ]
        states = program["states"]
        tactics = program["tactics"]
        factor = program["factor"]
        headline = program["headline"]
        authority = program["authority"]
        expected_tactics = set(_LEADER_TACTIC_PLAIN_LANGUAGE)
        expected_routes = {
            "risk_closed", "weak_repair", "sentiment_rising",
            "crowded_climax", "retreat",
        }
        if (
            program.get("schema")
            != "emotion_conditioned_leader_limit_program_status.v1"
            or program.get("primary_routing_axis") != "market_emotion_state"
            or not isinstance(states, list)
            or {row.get("route_state") for row in states} != expected_routes
            or not isinstance(tactics, list)
            or {row.get("id") for row in tactics} != expected_tactics
            or any(
                not all(
                    isinstance(row.get(key), str) and row[key].strip()
                    for key in (
                        "label", "characteristic", "consider_when",
                        "entry_method", "invalidation",
                    )
                )
                for row in tactics
            )
            or any(row.get("production") is not False for row in tactics)
            or any(row.get("generates_alert") is not False for row in tactics)
            or factor.get("role")
            != "auxiliary_ranking_only_never_blocks_main_route"
            or factor.get("production") is not False
            or factor.get("generates_alert") is not False
            or headline.get("production_auxiliary_factor_count") != 0
            or headline.get("production_entry_tactic_count") != 0
            or headline.get("buy_sell_alert_count") != 0
            or any(value is not False for value in authority.values())
        ):
            raise ValueError("published_leader_program_boundary_invalid")
        return copy.deepcopy(program)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _published_playbook_library(base: Path) -> dict | None:
    """Read the release-bound Chinese playbook descriptions for presentation."""
    path = base / "mom_trading" / "static" / "strategy_evidence_status.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        library = payload["current_research_program"]["published_playbook_library"]
        playbooks = library["playbooks"]
        expected = {
            "mainline_start_confirmation",
            "leader_divergence_restrengthening",
            "retreat_defense",
        }
        required_text = (
            "label", "characteristic", "consider_when", "entry_method",
            "avoid_when", "invalidation",
        )
        if (
            library.get("library_id") != "small_capital_playbook_library_v1"
            or not isinstance(playbooks, list)
            or {row.get("id") for row in playbooks} != expected
            or any(
                not all(
                    isinstance(row.get(key), str) and row[key].strip()
                    for key in required_text
                )
                or not isinstance(row.get("applicable_market_states"), list)
                for row in playbooks
            )
            or library.get("production") is not False
            or int(library.get("quantitatively_admitted") or 0) != 0
            or any(value is not False for value in (library.get("authority") or {}).values())
        ):
            raise ValueError("published_playbook_library_boundary_invalid")
        return copy.deepcopy(library)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _decorate_leader_limit_program(
    program: dict, playbook_library: dict | None,
) -> dict:
    """Attach human-readable, state-specific tactics without changing authority."""
    decorated = copy.deepcopy(program)
    factor = decorated.get("factor") or {}
    if (decorated.get("schema") == "emotion_conditioned_leader_limit_program_status.v1"
            and factor.get("id") == "negative_limit_hit_count_20_v1"):
        # The v1 result selected quintiles AFTER dropping unresolved outcomes.
        # Preserve its claims as history, but do not inherit that qualification.
        reason = "旧验证先剔除未结算事件再选组，历史通过结论需重验；不代表因子公式无效。"
        factor.setdefault("archived_historical_gate_claim", factor.get("aggregate_historical_gate_passed"))
        factor.update(aggregate_historical_gate_passed=False, historically_applicable_states=[],
                      historical_claim_status="outcome_selection_invalidated", historical_claim_reason=reason)
        decorated["headline"]["historically_applicable_factor_state_count"] = 0
        for row in decorated.get("states") or []:
            row.setdefault("archived_factor_state_claim", row.get("factor_historical_state_applicable"))
            row.update(factor_historical_state_applicable=False,
                       factor_status="cohort_integrity_review_required", factor_reason=reason)
    playbooks = list((playbook_library or {}).get("playbooks") or [])
    for row in decorated.get("states") or []:
        route_state = str(row.get("route_state") or "")
        auxiliary = _STATE_AUXILIARY_PRESENTATION.get(route_state) or {}
        row["auxiliary_signal"] = copy.deepcopy(auxiliary)
        row["playbooks"] = [
            copy.deepcopy(playbook) for playbook in playbooks
            if route_state in (playbook.get("applicable_market_states") or [])
        ]
    decorated["playbook_library"] = copy.deepcopy(playbook_library) if playbook_library else {
        "status": "release_bound_playbook_library_unavailable",
        "playbooks": [],
        "quantitatively_admitted": 0,
        "production": False,
        "authority": {
            "changes_live_router": False,
            "changes_live_position_or_quantity": False,
            "generates_buy_or_sell_alert": False,
            "creates_manual_order": False,
            "production_strategy": False,
            "automatic_trading": False,
            "stable_profit_claim": False,
        },
    }
    return decorated


def _leader_limit_program_status(root: Path | None = None) -> dict:
    """Build the fail-closed emotion-conditioned leader research contract."""
    base = Path(root) if root is not None else _PROJECT_ROOT
    playbook_library = _published_playbook_library(base)
    paths = ({
        "aggregate_factor": _LEADER_CROWDING_AGGREGATE_RESULT_PATH,
        "state_factor": _LEADER_CROWDING_STATE_RESULT_PATH,
        "forward_factor": _LEADER_CROWDING_FORWARD_RESULT_PATH,
        "single_hit_tactic": _LEADER_SINGLE_HIT_RESULT_PATH,
        "restrengthening_tactic": _LEADER_RESTRENGTHENING_RESULT_PATH,
        "stabilizing_single_hit_tactic": _STABILIZING_SINGLE_HIT_RESULT_PATH,
    } if root is None else {
        "aggregate_factor": base / "research"
        / "RECENT_LIMIT_HIT_CROWDING_V1_BLIND_RESULT.json",
        "state_factor": base / "research"
        / "RECENT_LIMIT_HIT_CROWDING_STATE_MATRIX_V1_RESULT.json",
        "forward_factor": base / "reports"
        / "recent_limit_hit_crowding_forward_v3_latest.json",
        "single_hit_tactic": base / "research"
        / "STATE_MAINLINE_SINGLE_HIT_LEADER_V1_BLIND_RESULT.json",
        "restrengthening_tactic": base / "research"
        / "STATE_CONDITIONED_LEADER_RESTRENGTHENING_5M_V1_BLIND_RESULT.json",
        "stabilizing_single_hit_tactic": base / "research"
        / "STABILIZING_MAINLINE_SINGLE_HIT_LEADER_V1_BLIND_RESULT.json",
    })
    payloads: dict[str, dict] = {}
    integrity_errors: list[str] = []
    live_input_errors: list[str] = []
    evidence: dict[str, dict] = {}
    for name, path in paths.items():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("top-level JSON is not an object")
            payloads[name] = raw
            evidence[name] = {
                "path": str(path.relative_to(base)),
                "sha256": _file_sha256(path),
            }
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            payloads[name] = {}
            error = f"{name}: {type(exc).__name__}"
            if name == "forward_factor":
                live_input_errors.append(error)
            else:
                integrity_errors.append(error)

    historical_keys = (
        "aggregate_factor", "state_factor", "single_hit_tactic",
        "restrengthening_tactic", "stabilizing_single_hit_tactic",
    )
    if integrity_errors and all(
        not paths[name].is_file() for name in historical_keys
    ):
        published = _published_leader_limit_program(base)
        if published is not None:
            forward = payloads["forward_factor"]
            factor = published["factor"]
            if forward:
                factor.update({
                    "forward_data_asof": forward.get("data_asof"),
                    "forward_universe_rows": forward.get(
                        "factor_universe_rows"
                    ),
                    "forward_recent_limit_hitter_rows": forward.get(
                        "recent_limit_hitter_rows"
                    ),
                    "forward_completed_weekly_labels": forward.get(
                        "forward_completed_weekly_labels"
                    ),
                    "forward_required_weekly_labels_in_same_state": (
                        forward.get(
                            "forward_required_weekly_labels_in_same_state"
                        )
                    ),
                    "forward_status": "current_snapshot_loaded",
                })
            else:
                factor["forward_status"] = (
                    "published_last_verified_snapshot_runtime_pending"
                )
            published.update({
                "status": (
                    "published_historical_evidence_live_forward_ready"
                    if forward
                    else "published_historical_evidence_live_forward_pending"
                ),
                "source_mode": (
                    "release_bound_historical_evidence_plus_live_snapshot"
                    if forward
                    else "release_bound_historical_evidence_runtime_snapshot_pending"
                ),
                "integrity_errors": [],
                "live_input_errors": live_input_errors,
            })
            return _decorate_leader_limit_program(
                published, playbook_library,
            )

    aggregate = payloads["aggregate_factor"]
    state_result = payloads["state_factor"]
    forward = payloads["forward_factor"]
    matrix = state_result.get("matrix") or {}
    historical_factor_pass = bool(
        aggregate.get("historical_factor_gate_passed") is True
        and state_result.get("status")
        == "historical_state_cells_passed_forward_required"
    )

    state_definitions = (
        ("RISK_OFF", "risk_closed", "风险关闭", "防守与持仓处置", 0),
        ("STABILIZING", "weak_repair", "弱修复", "主线确认后才研究轻仓", 0),
        ("RISK_ON", "sentiment_rising", "情绪上升", "龙头进攻研究", 0),
        ("OVERHEATED", "crowded_climax", "高潮拥挤", "停止追高，防冲高回落", 0),
        ("TRANSITION_TO_RISK", "retreat", "退潮", "只减风险，不新增买入", 0),
    )
    states = []
    for emotion_state, route_state, label, posture, max_new_trades in state_definitions:
        cell = matrix.get(emotion_state) if emotion_state in matrix else {}
        validation = ((cell.get("fold_metrics") or {}).get("validation") or {})
        sealed = ((cell.get("fold_metrics") or {}).get("sealed_test") or {})
        historically_applicable = bool(
            historical_factor_pass
            and cell.get("historical_state_applicable") is True
        )
        if emotion_state == "STABILIZING" and historically_applicable:
            factor_status = "historical_pass_forward_required"
            factor_reason = "历史状态格通过；同状态前向标签未满，不授予生产或提醒权"
        elif emotion_state == "TRANSITION_TO_RISK":
            factor_status = "not_routed"
            factor_reason = "退潮为状态迁移，不沿用单一情绪格的因子结论"
        else:
            factor_status = "insufficient_or_inapplicable"
            factor_reason = "该情绪格样本、方向或置信下界未全部通过"

        if emotion_state == "RISK_ON":
            tactic_status = "two_exact_tactics_failed"
            tactic_reason = "首板次日接力与核心分歧转强均在该情绪终段失败，已封存"
            tactic_ids = [
                "STATE_MAINLINE_SINGLE_HIT_LEADER_V1",
                "STATE_CONDITIONED_LEADER_RESTRENGTHENING_5M_V1",
            ]
        elif emotion_state == "STABILIZING":
            tactic_status = "exact_tactic_failed"
            tactic_reason = "同一主线首板次日接力规则在弱修复情绪也完成盲测并失败，已封存"
            tactic_ids = ["STABILIZING_MAINLINE_SINGLE_HIT_LEADER_V1"]
        else:
            tactic_status = "defense_only_no_entry"
            tactic_reason = "该状态不开放新的进攻买入；持仓处置须由账户与风控链决定"
            tactic_ids = ["retreat_defense"]
        states.append({
            "emotion_state": emotion_state,
            "route_state": route_state,
            "label": label,
            "posture": posture,
            "maximum_new_trades_from_this_research": max_new_trades,
            "tactic_status": tactic_status,
            "tactic_reason": tactic_reason,
            "tactic_ids": tactic_ids,
            "factor_status": factor_status,
            "factor_reason": factor_reason,
            "factor_historical_state_applicable": historically_applicable,
            "factor_forward_completed": int(
                cell.get("forward_state_labels_completed") or 0
            ),
            "factor_forward_required": int(
                cell.get("forward_state_labels_required") or 0
            ),
            "factor_metrics": {
                "validation_weeks": validation.get("weeks"),
                "validation_mean_rank_ic": validation.get("mean_rank_ic"),
                "validation_top_minus_bottom_after_20bp": (
                    validation.get("mean_top_minus_bottom_20bp")
                ),
                "sealed_weeks": sealed.get("weeks"),
                "sealed_mean_rank_ic": sealed.get("mean_rank_ic"),
                "sealed_top_minus_bottom_after_20bp": (
                    sealed.get("mean_top_minus_bottom_20bp")
                ),
                "sealed_spread_95pct_ci_low": (
                    (sealed.get("top_minus_bottom_20bp_moving_block_95pct_ci")
                     or {}).get("low")
                ),
            },
        })

    tactic_specs = (
        (
            "STATE_MAINLINE_SINGLE_HIT_LEADER_V1",
            "情绪上升期主线首板次日接力",
            "RISK_ON",
            payloads["single_hit_tactic"],
            "single_hit_tactic",
        ),
        (
            "STATE_CONDITIONED_LEADER_RESTRENGTHENING_5M_V1",
            "情绪上升期核心分歧转强",
            "RISK_ON",
            payloads["restrengthening_tactic"],
            "restrengthening_tactic",
        ),
        (
            "STABILIZING_MAINLINE_SINGLE_HIT_LEADER_V1",
            "弱修复期主线首板次日接力",
            "STABILIZING",
            payloads["stabilizing_single_hit_tactic"],
            "stabilizing_single_hit_tactic",
        ),
    )
    tactics = []
    for tactic_id, label, emotion_state, result, evidence_key in tactic_specs:
        folds = result.get("fold_metrics") or {}
        validation = (folds.get("validation") or {}).get("10bp") or {}
        sealed = (folds.get("sealed_test") or {}).get("10bp") or {}
        plain = _LEADER_TACTIC_PLAIN_LANGUAGE[tactic_id]
        tactics.append({
            "id": tactic_id,
            "label": label,
            "emotion_state": emotion_state,
            **plain,
            "verdict": "terminal_historical_failure_no_retune",
            "production": False,
            "generates_alert": False,
            "validation_10bp": {
                "trades": validation.get("trades"),
                "mean_net_return": validation.get("mean_net_return"),
                "median_net_return": validation.get("median_net_return"),
                "profit_factor": validation.get("profit_factor"),
            },
            "sealed_10bp": {
                "trades": sealed.get("trades"),
                "mean_net_return": sealed.get("mean_net_return"),
                "median_net_return": sealed.get("median_net_return"),
                "profit_factor": sealed.get("profit_factor"),
            },
            "failed_gates": list(result.get("failed_gates") or []),
            "evidence": evidence.get(evidence_key),
        })

    program = {
        "schema": "emotion_conditioned_leader_limit_program_status.v1",
        "status": (
            "evidence_incomplete_fail_closed" if integrity_errors
            else "historical_evidence_ready_live_forward_input_pending"
            if live_input_errors
            else "historical_factor_cell_forward_pending_tactics_failed"
        ),
        "source_mode": (
            "versioned_historical_evidence_plus_live_forward_snapshot"
            if not live_input_errors
            else "versioned_historical_evidence_live_forward_snapshot_pending"
        ),
        "primary_routing_axis": "market_emotion_state",
        "calendar_year_role": "out_of_sample_split_only_not_routing_axis",
        "headline": {
            "historically_applicable_factor_state_count": sum(
                1 for row in states
                if row["factor_historical_state_applicable"]
            ),
            "production_auxiliary_factor_count": 0,
            "production_entry_tactic_count": 0,
            "buy_sell_alert_count": 0,
        },
        "factor": {
            "id": "negative_limit_hit_count_20_v1",
            "label": "近20日涨停拥挤度（次数越少排序越高）",
            "role": "auxiliary_ranking_only_never_blocks_main_route",
            "aggregate_historical_gate_passed": historical_factor_pass,
            "historically_applicable_states": list(
                state_result.get("historically_applicable_states") or []
            ) if historical_factor_pass else [],
            "forward_data_asof": forward.get("data_asof"),
            "forward_universe_rows": forward.get("factor_universe_rows"),
            "forward_recent_limit_hitter_rows": (
                forward.get("recent_limit_hitter_rows")
            ),
            "forward_completed_weekly_labels": (
                forward.get("forward_completed_weekly_labels")
            ),
            "forward_required_weekly_labels_in_same_state": (
                forward.get("forward_required_weekly_labels_in_same_state")
            ),
            "forward_status": (
                "current_snapshot_loaded" if forward
                else "runtime_snapshot_unavailable_or_not_yet_collected"
            ),
            "production": False,
            "generates_alert": False,
        },
        "states": states,
        "tactics": tactics,
        "evidence": evidence,
        "integrity_errors": integrity_errors,
        "live_input_errors": live_input_errors,
        "authority": {
            "production_factor": False,
            "production_strategy": False,
            "generates_buy_or_sell_alert": False,
            "changes_position_or_quantity": False,
            "automatic_trading": False,
        },
    }
    return _decorate_leader_limit_program(program, playbook_library)


def _factor_status_summary_projection(payload: dict) -> dict:
    """Return the small authoritative status contract used outside this page."""
    out = {
        "view": "summary",
        "research_epoch": payload.get("research_epoch"),
        "registry": payload.get("registry") or {},
        "registry_health": payload.get("registry_health") or {},
        "workflow": payload.get("workflow") or {},
        "leader_limit_program": payload.get("leader_limit_program") or {},
    }
    if "recent_sealed_research" in payload:
        recent = payload.get("recent_sealed_research") or {}
        out["recent_sealed_research"] = {key: recent.get(key) for key in (
            "status", "count", "listed_count", "missing_count", "unreadable_count",
            "without_outcomes_count", "latest_at", "note")}
        items = [row for row in recent.get("items", []) if isinstance(row, dict)][:12]
        out["recent_sealed_research"].update({
            "items": [{key: row.get(key) for key in (
                "display_name", "candidate_id", "status", "generated_at",
                "historical_factor_gate_passed", "historical_factor_gate_evaluated",
                "report_path", "report_sha256", "terminal_split", "metrics")}
                      for row in items],
            "display_limit": 12, "production_eligible": False,
            "generates_alert": False, "automatic_orders": False})
    return out


def register_factor_system_routes(app, *, research_lock_state) -> None:
    @app.get("/api/research-worker/status")
    def research_worker_status():
        from .cloud_research_worker import public_status
        status = public_status()
        status["website_background_enabled"] = bool(app.config.get("WEBSITE_BACKGROUND_ENABLED"))
        status["local_website_mode"] = "interactive_only" if not app.config.get("WEBSITE_BACKGROUND_ENABLED") else "background_enabled"
        response = jsonify(status)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/factor-system/status")
    def factor_system_status():
        import factor_system.config as fconfig
        import factor_system.registry as freg
        import factor_system.status as fstatus
        view = request.args.get("view", "full")
        if view not in {"full", "summary"}:
            return jsonify({
                "ok": False,
                "error": "view 仅支持 full、summary",
                "code": "invalid_view",
            }), 400
        out = {}
        for zone in ("mining", "backtest"):
            st = fstatus.read_zone(zone)
            out[zone] = st if st else {
                "zone": zone,
                "status": "stopped",
                "phase": "awaiting_supervisor",
                "detail": "等待常驻 Supervisor 调度首个真实批次",
            }
        out["daemon"] = fstatus.read_daemon() or {
            "daemon": "stopped", "phase": "未运行", "detail": "24h supervisor 未启动",
            "current_zone": "", "rounds": 0, "errors": 0,
            "novelty_today": 0, "low_power": False, "next_plan": "等待启动"}
        try:
            daemon_lock_text = (
                fconfig.STATE_DIR / "daemon.lock"
            ).read_text(encoding="utf-8")
            daemon_pid_match = re.search(r"(?:^|\s)pid=(\d+)(?:\s|$)", daemon_lock_text)
            if daemon_pid_match:
                daemon_pid = int(daemon_pid_match.group(1))
                out["daemon"]["process_pid"] = daemon_pid
                try:
                    os.kill(daemon_pid, 0)
                    out["daemon"]["process_alive"] = True
                except OSError:
                    out["daemon"]["process_alive"] = False
        except OSError:
            out["daemon"]["process_alive"] = False
        out["daemon"].setdefault("process_alive", False)
        heartbeat_source = (
            "heartbeat_at" if out["daemon"].get("heartbeat_at") else "updated_at"
        )
        try:
            heartbeat = datetime.fromisoformat(
                str(out["daemon"].get(heartbeat_source) or "")
            )
            if heartbeat.tzinfo is not None:
                now_for_heartbeat = datetime.now(heartbeat.tzinfo)
            else:
                now_for_heartbeat = datetime.now()
            heartbeat_age = max(
                0, int((now_for_heartbeat - heartbeat).total_seconds())
            )
        except (TypeError, ValueError):
            heartbeat_age = None
        out["daemon"]["heartbeat_source"] = heartbeat_source
        out["daemon"]["heartbeat_age_seconds"] = heartbeat_age
        # watchdog 每 30 秒独立落盘；容许 4 个周期，避免短暂调度抖动误报。
        out["daemon"]["heartbeat_fresh"] = bool(
            heartbeat_age is not None and heartbeat_age <= 120
        )
        reported_state = str(out["daemon"].get("daemon") or "stopped")
        process_alive = out["daemon"]["process_alive"] is True
        heartbeat_fresh = out["daemon"]["heartbeat_fresh"] is True
        physically_online = process_alive and heartbeat_fresh
        operational_online = reported_state == "running" and physically_online
        if reported_state == "error":
            operational_state = "error"
            status_reason = "reported_error"
        elif operational_online:
            operational_state = "online"
            status_reason = "process_alive_and_heartbeat_fresh"
        elif not process_alive:
            operational_state = "offline"
            status_reason = "process_unavailable"
        elif not heartbeat_fresh:
            operational_state = "offline"
            status_reason = "heartbeat_stale"
        else:
            operational_state = "offline"
            status_reason = "reported_not_running"
        # Preserve the persisted state for diagnostics, while exposing the
        # current operational truth as a separate, unambiguous contract.
        out["daemon"].update({
            "reported_state": reported_state,
            "operational_state": operational_state,
            "status_reason": status_reason,
            "state_consistent": (
                (reported_state == "running") == physically_online
            ),
        })
        try:
            progress_timeout = max(
                900,
                int(os.environ.get("FACTOR_DAEMON_MAX_ROUND_SECONDS", "21600")),
            )
        except ValueError:
            progress_timeout = 21600
        try:
            progress_age = max(
                0, int(out["daemon"].get("progress_age_seconds"))
            )
        except (TypeError, ValueError):
            progress_age = None
        out["daemon"]["progress_age_seconds"] = progress_age
        out["daemon"]["progress_timeout_seconds"] = progress_timeout
        # 旧状态没有 progress_age 时仅靠独立心跳过渡；新 watchdog 会同时写入两者。
        out["daemon"]["progress_fresh"] = bool(
            progress_age is None or progress_age <= progress_timeout
        )
        # C4 看板：合并衰减再验证信息（daemon_runtime.json：时间 + 最近一轮结果）
        try:
            rt_path = fconfig.STATE_DIR / "daemon_runtime.json"
            if rt_path.is_file():
                rt = json.loads(rt_path.read_text(encoding="utf-8"))
                out["daemon"]["last_revalidate"] = rt.get("last_revalidate", "")
                out["daemon"]["last_revalidate_result"] = rt.get("last_revalidate_result")
                if isinstance(rt.get("ingestion_blocker"), dict):
                    out["daemon"]["ingestion_blocker"] = rt["ingestion_blocker"]
        except Exception:
            pass
        registry_error = None
        rows = []
        try:
            with closing(freg._conn(readonly=True)) as c:
                available_columns = {
                    str(column[1]) for column in c.execute(
                        "PRAGMA table_info(factors)"
                    ).fetchall()
                }
                public_status_columns = ["status", *[
                    name for name in (
                        "research_epoch", "updated_at",
                        "research_record_json", "research_record_sha256",
                    )
                    if name in available_columns
                ]]
                rows = c.execute(
                    f"SELECT {','.join(public_status_columns)} FROM factors"
                ).fetchall()
            source_counts: dict[str, int] = {}
            for row in rows:
                source_counts[row["status"]] = source_counts.get(row["status"], 0) + 1
            out["registry_source_counts"] = source_counts
            out["registry"] = _public_factor_status_counts(
                rows, research_epoch=fconfig.RESEARCH_EPOCH,
            )
            out["registry_total"] = len(rows)
            out["registry_health"] = _factor_registry_health()
        except Exception as exc:
            log_internal_error(app, exc, event="factor_registry_failure")
            out["registry_health"] = _factor_registry_health(exc)
            registry_error = out["registry_health"]["message"]
            out["registry"] = {}
            out["registry_total"] = None
            rows = []
        out["research_epoch"] = fconfig.RESEARCH_EPOCH
        out["research_windows"] = {
            "discovery": [fconfig.DISCOVERY_START, fconfig.DISCOVERY_END],
            "validation": [fconfig.VALIDATION_START, fconfig.VALIDATION_END],
            "historical_confirmation": [
                fconfig.HISTORICAL_TEST_START, fconfig.HISTORICAL_TEST_END,
            ],
            "forward_shadow": [fconfig.FORWARD_SHADOW_START, None],
        }
        production_catalog = _cached_factor_production_catalog_summary()
        out["production_catalog"] = production_catalog
        out["forward_shadow_readiness"] = _factor_forward_readiness(
            fconfig, out.get("backtest"), production_catalog,
        )
        research_lock = research_lock_state()
        registry_summary = _factor_registry_summary(
            rows, research_epoch=fconfig.RESEARCH_EPOCH,
        )
        historical_validation = _active_candidate_historical_validation_status()
        source_gate = _apply_active_candidate_terminal_closure(
            _factor_source_gate_summary(rows), historical_validation,
        )
        out["source_gate"] = source_gate
        out["active_candidate_collection"] = _active_candidate_collection_status()
        out["active_candidate_input"] = _active_candidate_input_status()
        out["active_candidate_factor_values"] = (
            _active_candidate_factor_value_status()
        )
        out["active_candidate_historical_validation"] = historical_validation
        source_scout = _apply_ovnt_terminal_closure_to_source_scout(
            _public_source_scout_status(
                fconfig.STATE_DIR / "public_source_scout.json"
            ),
            historical_validation,
        )
        out["public_source_scout"] = source_scout
        workflow = _factor_research_workflow(
            mining=out["mining"], backtest=out["backtest"],
            daemon=out["daemon"], research_lock=research_lock,
            registry_summary=registry_summary, source_gate=source_gate,
            source_scout=source_scout,
        )
        out["mining"] = _factor_zone_operational_view(
            out["mining"], zone="mining", workflow=workflow,
        )
        out["backtest"] = _factor_zone_operational_view(
            out["backtest"], zone="backtest", workflow=workflow,
        )
        external_research = _external_research_status()
        if external_research["degraded"]:
            for blocker in external_research["blockers"]:
                workflow["blockers"].append({
                    **blocker,
                    "scope": "external_research",
                })
        workflow["external_research_degraded"] = external_research["degraded"]
        out["external_research"] = external_research
        out["recent_sealed_research"] = _recent_sealed_research()
        source_frontier = _remaining_source_frontier()
        active_input = _active_shareholder_input_research()
        multiyear_pit_input = _cached_multiyear_first_filing_input_research()
        source_frontier["active_input_research"] = active_input
        source_frontier["active_multiyear_pit_input"] = multiyear_pit_input
        terminal_chain = _cash_flow_profitability_terminal_chain_status()
        source_frontier["cash_flow_profitability_input_readiness"] = terminal_chain
        multiyear_status = str(multiyear_pit_input.get("status") or "")
        active_status = str(active_input.get("status") or "")
        reviewed_source_work_pending = bool(
            int(source_scout.get("formula_pending_count") or 0)
            + int(source_scout.get("source_contract_pending_count") or 0)
        )
        if terminal_chain.get("available") is True \
                and multiyear_status in {
                    "2024_official_pit_collection_complete_terminal_audit_pending",
                    "2024_official_pit_collection_terminal_audit_complete",
                } \
                and not reviewed_source_work_pending \
                and workflow.get("current_stage") != "research_mutation":
            workflow_runtime = workflow.setdefault("runtime", {})
            workflow_runtime.update({
                "active": False,
                "source": terminal_chain["status"],
            })
            if (
                terminal_chain.get("identity_valid") is True
                and terminal_chain.get("status")
                == "novelty_failed_permanently_archived_before_outcomes"
            ):
                failed_names = terminal_chain.get(
                    "factor_novelty_failed_comparisons"
                ) or []
                closed_next_action = (
                    "本候选已因邻族相关性达到0.80而永久归档；"
                    "常驻服务每日执行一次有界来源侦察，继续等待新的独立经济假设，"
                    "不调参、不换名、不读取该候选收益。"
                )
                source_frontier.update({
                    "operational_status": "needs_new_hypothesis",
                    "eligible_new_candidate": None,
                    "reason": (
                        "两年度官方PIT输入与五项新颖性审计均已完成；"
                        f"{len(failed_names)}项邻族比较达到归档阈值，"
                        "该公式已科学淘汰，不是系统故障。"
                    ),
                    "next_compute_action": closed_next_action,
                })
                workflow.update({
                    "current_stage": "needs_new_hypothesis",
                    "stage_status": "waiting",
                    "next_action": closed_next_action,
                    "transition_condition": "new_independent_hypothesis_registered",
                    "upstream_active": False,
                    "as_of": workflow.get("as_of"),
                })
                workflow_runtime = workflow.setdefault("runtime", {})
                workflow_runtime.update({
                    "active": False,
                    "source": "needs_new_hypothesis",
                })
                workflow["blockers"] = [
                    item for item in workflow.get("blockers") or []
                    if item.get("system_fault") is not False
                ]
                prior_findings = [
                    item for item in workflow.get("research_findings") or []
                    if item.get("code") != "source_hypotheses_exhausted"
                ]
                workflow["research_findings"] = [{
                    "code": "cash_flow_profitability_novelty_archived",
                    "message": (
                        "CFO/总资产候选完成9839行因子值与五项无收益新颖性审计；"
                        "因现金流/收入与期初资产子信号重复度过高，已永久归档。"
                    ),
                    "scope": "factor_novelty",
                    "system_fault": False,
                }, *prior_findings]
            elif terminal_chain.get("identity_valid") is True:
                eligible_rows = int(
                    (terminal_chain.get("counts") or {}).get(
                        "eligible_nonfinancial_input_rows", 0,
                    )
                )
                contract_next_action = (
                    "冻结覆盖阈值、形成期、年份和共同样本合同；"
                    "不计算因子或收益。"
                )
                source_frontier.update({
                    "operational_status": "input_readiness_contract_freeze",
                    "eligible_new_candidate": None,
                    "reason": (
                        "官方PIT终局材料和输入清单身份已核对；"
                        f"当前可用非金融输入{eligible_rows}行，"
                        "但覆盖、形成期、年份和共同样本合同尚未冻结。"
                    ),
                    "next_compute_action": contract_next_action,
                })
                workflow.update({
                    "current_stage": "input_readiness_contract_freeze",
                    "stage_status": "pending",
                    "next_action": contract_next_action,
                    "transition_condition": "input_readiness_contract_admitted",
                    "upstream_active": False,
                    "as_of": multiyear_pit_input.get("checkpoint_updated_at")
                    or workflow.get("as_of"),
                })
                prior_findings = [
                    item for item in workflow.get("research_findings") or []
                    if item.get("code") != "source_hypotheses_exhausted"
                ]
                workflow["research_findings"] = [{
                    "code": "multiyear_pit_input_readiness_contracts_pending",
                    "message": (
                        "2024官方PIT终局证据已收口；当前只冻结输入合同，"
                        "尚未计算因子、收益、IC或策略信号。"
                    ),
                    "scope": "input_readiness_contract_freeze",
                    "system_fault": False,
                }, *prior_findings]
            else:
                evidence_next_action = (
                    "核对终局材料、FIXED2合并与V2输入审计的哈希绑定；"
                    "身份恢复前不计算因子或收益。"
                )
                source_frontier.update({
                    "operational_status": "multiyear_pit_terminal_evidence_validation",
                    "eligible_new_candidate": None,
                    "reason": "官方PIT终局证据链身份不一致，已失败关闭。",
                    "next_compute_action": evidence_next_action,
                })
                workflow.update({
                    "current_stage": "multiyear_pit_terminal_evidence_validation",
                    "stage_status": "blocked",
                    "next_action": evidence_next_action,
                    "transition_condition": "terminal_evidence_identity_restored",
                    "upstream_active": False,
                })
                workflow["blockers"].insert(0, {
                    "code": "multiyear_pit_terminal_chain_identity_mismatch",
                    "message": "；".join(terminal_chain.get("errors") or [])
                    or "终局证据链身份不一致",
                    "scope": "multiyear_pit_terminal_evidence_validation",
                    "system_fault": True,
                })
        elif multiyear_status == "2024_official_pit_collection_running" \
                and workflow.get("current_stage") != "research_mutation":
            source_frontier.update({
                "operational_status": multiyear_status,
                "eligible_new_candidate": None,
                "reason": (
                    "2025官方首发年报分片已终局，2024分片正在按冻结身份采集；"
                    "这是多年度PIT输入工程，不是因子收益或推荐。"
                ),
                "next_compute_action": multiyear_pit_input["next_action"],
            })
            workflow.update({
                "current_stage": "multiyear_pit_input_epoch",
                "stage_status": "running",
                "next_action": source_frontier["next_compute_action"],
                "transition_condition": "2024_official_pit_terminal_audit_complete",
                "upstream_active": True,
                "as_of": multiyear_pit_input.get("checkpoint_updated_at")
                or workflow.get("as_of"),
            })
            workflow_runtime = workflow.setdefault("runtime", {})
            workflow_runtime.update({
                "active": True,
                "source": "multiyear_pit_input_epoch",
            })
            prior_findings = [
                item for item in workflow.get("research_findings") or []
                if item.get("code") != "source_hypotheses_exhausted"
            ]
            workflow["research_findings"] = [{
                "code": "multiyear_official_pit_input_active_not_candidate",
                "message": (
                    f"2025已终局5478家；2024已成功"
                    f"{multiyear_pit_input.get('success', 0)}/"
                    f"{multiyear_pit_input.get('targets', 0)}，错误"
                    f"{multiyear_pit_input.get('failed', 0)}，身份"
                    f"{'有效' if multiyear_pit_input.get('identity_valid') else '不一致'}。"
                ),
                "scope": "multiyear_pit_input_epoch",
                "system_fault": False,
            }, *prior_findings]
        elif multiyear_status == (
            "2024_official_pit_collection_complete_terminal_audit_pending"
        ) and workflow.get("current_stage") != "research_mutation":
            terminal_next_action = (
                "2024官方PIT采集已完成；等待一次冻结身份的终局重解析审计。"
                "审计通过前不计算因子、不读取收益。"
            )
            source_frontier.update({
                "operational_status": multiyear_status,
                "eligible_new_candidate": None,
                "reason": (
                    "2024官方PIT采集已完成且采集终局身份有效；"
                    "当前等待终局重解析审计，不是等待新假设。"
                ),
                "next_compute_action": terminal_next_action,
            })
            workflow.update({
                "current_stage": "multiyear_pit_terminal_reparse_audit",
                "stage_status": "pending",
                "next_action": terminal_next_action,
                "transition_condition": "terminal_reparse_audit_complete",
                "upstream_active": False,
                "as_of": multiyear_pit_input.get("checkpoint_updated_at")
                or workflow.get("as_of"),
            })
            workflow_runtime = workflow.setdefault("runtime", {})
            workflow_runtime.update({
                "active": False,
                "source": "multiyear_pit_terminal_reparse_audit",
            })
            prior_findings = [
                item for item in workflow.get("research_findings") or []
                if item.get("code") != "source_hypotheses_exhausted"
            ]
            workflow["research_findings"] = [{
                "code": "multiyear_official_pit_collection_complete_not_factor",
                "message": (
                    f"2025已终局5478家；2024已成功"
                    f"{multiyear_pit_input.get('success', 0)}/"
                    f"{multiyear_pit_input.get('targets', 0)}，等待终局重解析审计。"
                ),
                "scope": "multiyear_pit_input_epoch",
                "system_fault": False,
            }, *prior_findings]
        elif multiyear_pit_input.get("remediation_status") == (
            "paused_remediation_pending"
        ):
            source_frontier.update({
                "operational_status": "paused_remediation_pending",
                "eligible_new_candidate": None,
                "reason": (
                    "2024官方PIT采集已按冻结暂停工件停下；解析器修复已获准，"
                    "正在等待新的Recovery执行回执，不是旧执行身份损坏。"
                ),
                "next_compute_action": multiyear_pit_input["next_action"],
            })
            workflow["blockers"].insert(0, {
                "code": "multiyear_official_pit_remediation_pending",
                "message": (
                    "2024官方PIT输入处于已裁决的解析器修复等待状态；"
                    "保持暂停，等待新Recovery回执后再继续。"
                ),
                "scope": "multiyear_pit_input_epoch",
                "system_fault": False,
            })
        elif multiyear_status in {
            "2024_official_pit_identity_mismatch",
            "2024_official_pit_collection_paused",
            "2024_official_pit_checkpoint_unreadable",
        }:
            workflow["blockers"].insert(0, {
                "code": "multiyear_official_pit_input_failed_closed",
                "message": (
                    "2024官方PIT输入身份、检查点或采集终止条件异常；"
                    "保持失败关闭，不重跑、不读取收益。"
                ),
                "scope": "multiyear_pit_input_epoch",
                "system_fault": True,
            })
        elif active_status in {
            "input_epoch_building", "input_epoch_finalizing",
            "primary_input_epoch_complete_summary_repair_pending",
        } and workflow.get("current_stage") != "research_mutation":
            source_frontier.update({
                "operational_status": active_status,
                "eligible_new_candidate": None,
                "reason": (
                    "新的独立股东户数机制正在进行公开来源 PIT 输入审计；"
                    "输入通过前不登记候选、不读取收益。"
                ),
                "next_compute_action": (
                    "完成唯一 V292 输入任务；随后仅执行同日官方摘要 V293 补缺。"
                ),
            })
            workflow.update({
                "current_stage": "source_input_epoch",
                "stage_status": "running",
                "next_action": source_frontier["next_compute_action"],
                "transition_condition": "v293_full_market_input_epoch_accepted",
                "upstream_active": True,
                "as_of": active_input.get("checkpoint_updated_at")
                or workflow.get("as_of"),
            })
            workflow_runtime = workflow.setdefault("runtime", {})
            workflow_runtime.update({
                "active": True,
                "source": "source_input_epoch",
            })
            prior_findings = [
                item for item in workflow.get("research_findings") or []
                if item.get("code") != "source_hypotheses_exhausted"
            ]
            workflow["research_findings"] = [{
                "code": "new_pit_input_epoch_active_not_candidate",
                "message": (
                    f"V292 已完成 {active_input.get('completed', 0)}/"
                    f"{active_input.get('targets', 0)}；身份"
                    f"{'有效' if active_input.get('identity_valid') is not False else '不一致'}。"
                ),
                "scope": "source_input_epoch",
                "system_fault": False,
            }, *prior_findings]
        elif active_status == "input_epoch_identity_mismatch":
            workflow["blockers"].insert(0, {
                "code": "active_input_epoch_identity_mismatch",
                "message": "V292 检查点身份与冻结协议不一致；禁止继续或准入。",
                "scope": "source_input_epoch",
                "system_fault": True,
            })
        out["source_frontier"] = source_frontier
        out["research_lock"] = research_lock
        if registry_error:
            # Independent collectors may run, but their progress cannot certify this DB.
            workflow.update({
                "current_stage": "registry_read",
                "stage_status": "blocked",
                "counts": {},
                "next_action": "恢复服务端研究库后重新读取；数量未知，不代表合格因子为 0",
            })
            workflow["blockers"].insert(0, {
                "code": "factor_registry_unavailable",
                "message": registry_error,
                "scope": "factor_registry",
                "system_fault": True,
            })
        out["workflow"] = workflow
        out["leader_limit_program"] = _leader_limit_program_status()
        if view == "summary":
            out = _factor_status_summary_projection(out)
        response = jsonify(out)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/api/factor-system/factors")
    def factor_system_factors():
        import factor_system.config as fconfig
        import factor_system.registry as freg
        import factor_system.status as fstatus
        legacy_full = not any(
            key in request.args for key in (
                "view", "scope", "status", "query", "limit", "cursor",
            )
        )
        view = request.args.get("view", "full")
        scope = request.args.get("scope", "all")
        if view not in {"full", "compact", "summary"}:
            return jsonify({
                "ok": False, "error": "view 仅支持 full、compact、summary",
                "code": "invalid_view",
            }), 400
        if scope not in {"all", "current", "archive"}:
            return jsonify({
                "ok": False, "error": "scope 仅支持 all、current、archive",
                "code": "invalid_scope",
            }), 400
        raw_status = ",".join(request.args.getlist("status"))
        statuses = {
            value.strip() for value in raw_status.split(",") if value.strip()
        }
        if "pending" in statuses:
            statuses.remove("pending")
            statuses.update({"validated", "registered", "backtesting"})
        unknown_statuses = sorted(statuses - _FACTOR_PUBLIC_STATUSES)
        if unknown_statuses:
            return jsonify({
                "ok": False,
                "error": f"不支持的公开因子状态: {', '.join(unknown_statuses)}",
                "code": "invalid_status",
            }), 400
        query = request.args.get("query", "").strip().casefold()
        if len(query) > 200:
            return jsonify({
                "ok": False, "error": "query 最长 200 个字符",
                "code": "query_too_long",
            }), 400
        try:
            cursor = int(request.args.get("cursor", "0") or 0)
            limit = int(request.args.get("limit", "50") or 50)
        except ValueError:
            return jsonify({
                "ok": False, "error": "limit 与 cursor 必须是整数",
                "code": "invalid_page",
            }), 400
        if cursor < 0 or (not legacy_full and not 1 <= limit <= 200):
            return jsonify({
                "ok": False,
                "error": "cursor 不得为负；limit 必须在 1 至 200 之间",
                "code": "invalid_page",
            }), 400

        try:
            with closing(freg._conn(readonly=True)) as c:
                registry_summary = freg.public_registry_summary(
                    c, research_epoch=fconfig.RESEARCH_EPOCH,
                )
                effective_limit = (
                    max(1, registry_summary["total"]) if legacy_full else limit
                )
                if view == "summary":
                    rows = []
                    filtered_total = freg.public_factor_count(
                        c, research_epoch=fconfig.RESEARCH_EPOCH,
                        scope=scope, statuses=tuple(sorted(statuses)), query=query,
                    )
                else:
                    rows, filtered_total = freg.public_factor_page(
                        c, research_epoch=fconfig.RESEARCH_EPOCH,
                        scope=scope, statuses=tuple(sorted(statuses)), query=query,
                        limit=effective_limit, offset=cursor,
                    )
        except Exception as exc:
            return _factor_registry_error_response(
                app, exc, "因子注册表读取失败",
            )
        production_catalog = _cached_factor_production_catalog_summary()
        readiness = _factor_forward_readiness(
            fconfig, fstatus.read_zone("backtest"), production_catalog,
        )
        source_gate = _apply_active_candidate_terminal_closure(
            _factor_source_gate_summary(
                legacy_unsourced=registry_summary["legacy_unsourced"],
                registry_total=registry_summary["total"],
            ),
            _active_candidate_historical_validation_status(),
        )
        public_rows = [
            _public_factor_row(
                row, research_epoch=fconfig.RESEARCH_EPOCH,
                forward_readiness=readiness,
            )
            for row in rows
        ]
        factors = (
            [_compact_public_factor_row(row) for row in public_rows]
            if view == "compact" else public_rows
        )
        page_limit = 0 if view == "summary" else effective_limit
        next_cursor = (
            str(cursor + page_limit)
            if page_limit and cursor + page_limit < filtered_total else None
        )
        payload = {
            "ok": True,
            "factors": factors,
            "research_epoch": fconfig.RESEARCH_EPOCH,
            "version": registry_summary["version"],
            "as_of": registry_summary["as_of"],
            "summary": {
                "current": registry_summary["current"],
                "archive": registry_summary["archive"],
                "eligibility": registry_summary["eligibility"],
                "filtered_total": filtered_total,
            },
            "page": {
                "limit": page_limit,
                "cursor": str(cursor),
                "next_cursor": next_cursor,
                "has_more": next_cursor is not None,
            },
            "forward_shadow_readiness": readiness,
            "production_catalog": production_catalog,
            "source_gate": source_gate,
            "audit_message": (
                "2024-07~2025-12 统一称历史确认；production 仅由每个因子自身 "
                "seal 后新增数据的固定前向终测产生。"
            ),
        }
        response = jsonify(payload)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response
