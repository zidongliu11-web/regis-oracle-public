from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from research.accelerated_historical_simulation import (
    snapshot as accelerated_simulation_snapshot,
)
from research.fifteen_hour_validation_session import (
    snapshot as fifteen_hour_session_snapshot,
)
from research.open_source_factor_registry_v18 import (
    registry_payload as v18_registry_payload,
)
from research.open_source_factor_registry_v27 import (
    registry_payload as v27_registry_payload,
)
from research.open_source_factor_registry_v28 import (
    registry_payload as v28_registry_payload,
)
from research.open_source_factor_registry_v29 import (
    registry_payload as v29_registry_payload,
)
from research.open_source_factor_registry_v30 import (
    registry_payload as v30_registry_payload,
)
from trade_assistant.strategy_gate import (
    GATE_LABELS as STRICT_GATE_LABELS,
    evaluate_historical_strategy_gate,
)


GATE_LABELS = {
    "pretest_selected": "验证集预筛",
    "heldout_confirmed": "独立测试确认",
    "oos_trading_days_at_least_500": "样本外交易日",
    "trades_at_least_50": "至少50笔交易",
    "total_return_positive": "扣费后总收益为正",
    "annualized_return_positive": "年化收益为正",
    "win_rate_at_least_50": "胜率至少50%",
    "profit_factor_at_least_1_15": "盈利因子至少1.15",
    "max_drawdown_at_most_12": "最大回撤不超过12%",
    "positive_fold_ratio_at_least_60": "正收益折数至少60%",
    **STRICT_GATE_LABELS,
}

FAMILY_LABELS = {
    "momentum": "动量",
    "reversal": "反转",
    "volatility": "波动率",
    "trend": "趋势",
    "liquidity": "流动性",
    "price_volume": "量价",
    "momentum_volatility": "动量×波动率",
}

FORMAL_GATE_LABELS = {
    "evaluable_days_at_least_500": "至少500个可评估日",
    "daily_coverage_p10_at_least_80": "每日覆盖率P10至少80%",
    "validation_mean_ic_positive": "验证期平均IC为正",
    "test_block_bootstrap_ci_low_positive": "测试期区块自助CI下界为正",
    "positive_test_folds_at_least_2_of_3": "测试期至少2/3分段为正",
    "decile_monotonicity_at_least_0_6": "十分组单调性至少0.6",
    "top_minus_middle_after_5bp_positive": "扣往返10BP后头部高于中间组",
}

LIVE_CYCLE_STAGES = [
    {
        "id": "v12_fundamentals",
        "label": "V12公告时点季度基本面",
        "worker_step": "构建V12公告时点季度盈利与现金流",
        "default_seconds": 32400,
        "kind": "data",
    },
    {
        "id": "incremental_data",
        "label": "最新日线增量更新",
        "worker_step": "增量更新最新A股日线",
        "default_seconds": 1800,
        "kind": "data",
    },
    {
        "id": "quality",
        "label": "数据质量审计",
        "worker_step": "审计已下载日线质量",
        "default_seconds": 60,
        "kind": "data",
    },
    {
        "id": "factor_pipeline",
        "label": "因子管线计算",
        "worker_step": "计算V4因子管线干跑",
        "default_seconds": 50,
        "kind": "factor",
    },
    {
        "id": "v4_formal",
        "label": "V4正式因子检验",
        "worker_step": "运行全量V4多周期正式因子研究",
        "default_seconds": 100,
        "kind": "backtest",
    },
    {
        "id": "v4_1",
        "label": "V4.1探索复核",
        "worker_step": "运行V4.1风险过滤与滚动方向研究",
        "default_seconds": 18,
        "kind": "backtest",
    },
    {
        "id": "v4_2",
        "label": "V4.2微观结构回测",
        "worker_step": "运行V4.2微观结构因子研究",
        "default_seconds": 92,
        "kind": "backtest",
    },
    {
        "id": "v4_3",
        "label": "V4.3历史失败封存",
        "worker_step": "封存V4.3为历史失败",
        "default_seconds": 2,
        "kind": "archive",
    },
    {
        "id": "v4_4_factor",
        "label": "V4.4三项预注册实验",
        "worker_step": "运行V4.4三项预注册因子实验",
        "default_seconds": 110,
        "kind": "backtest",
    },
    {
        "id": "v4_4_strategy",
        "label": "V4.4低成本策略",
        "worker_step": "运行V4.4低换手含成本策略回测",
        "default_seconds": 30,
        "kind": "backtest",
    },
    {
        "id": "v5_factor",
        "label": "V5首次涨停效应",
        "worker_step": "运行V5首次涨停溢出效应预注册研究",
        "default_seconds": 240,
        "kind": "backtest",
    },
    {
        "id": "v6_factor",
        "label": "V6非涨停MAX效应",
        "worker_step": "运行V6非涨停型MAX效应预注册研究",
        "default_seconds": 90,
        "kind": "backtest",
    },
    {
        "id": "v17_factor",
        "label": "V17跌停量能枯竭恢复",
        "worker_step": "运行V17跌停流动性枯竭恢复预注册研究",
        "default_seconds": 240,
        "kind": "backtest",
    },
    {
        "id": "v21_daily_pilot",
        "label": "V21增持事件日终刷新",
        "worker_step": "刷新V21股东增持人工试运营候选",
        "default_seconds": 60,
        "kind": "backtest",
    },
    {
        "id": "v23_market_regime",
        "label": "V23增持事件MA200复验",
        "worker_step": "复验V21增持事件的上证MA200市场状态过滤",
        "default_seconds": 15,
        "kind": "backtest",
    },
    {
        "id": "current_index",
        "label": "当前上证指数新鲜度",
        "worker_step": "刷新当前上证指数并审计数据新鲜度",
        "default_seconds": 8,
        "kind": "data",
    },
    {
        "id": "current_manual_decision",
        "label": "当日V23候选/空仓裁决",
        "worker_step": "裁决V23当天人工候选或空仓",
        "default_seconds": 3,
        "kind": "decision",
    },
    {
        "id": "paper_shadow",
        "label": "模拟盘影子观察",
        "worker_step": "记录不可执行的模拟盘影子观察",
        "default_seconds": 3,
        "kind": "paper",
    },
    {
        "id": "deepseek",
        "label": "DeepSeek六路并行独立红队",
        "worker_step": "DeepSeek 研究红队",
        "default_seconds": 180,
        "kind": "ai",
    },
    {
        "id": "deepseek_discovery",
        "label": "DeepSeek连续新机制研究",
        "worker_step": "DeepSeek 连续机制研究",
        "default_seconds": 180,
        "kind": "ai",
    },
]
FULL_CYCLE_SLEEP_SECONDS = 15


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _canonical_v23_ten_bp_metrics(report: dict[str, Any]) -> dict[str, Any]:
    """Return the fullest audited V23 cross-period 10BP metric record."""
    metrics = dict(report.get("summary_10bp") or {})
    results = report.get("results") or {}
    combined = (
        ((results.get("combined") or {}).get("round_trip_10bp") or {}).get(
            "metrics"
        )
        or {}
    )
    metrics.update(combined)

    period_metrics = []
    for period_id in ("replication_2024_2025", "current_2026"):
        period = (
            ((results.get(period_id) or {}).get("round_trip_10bp") or {}).get(
                "metrics"
            )
            or {}
        )
        if period.get("total_return_pct") is not None:
            period_metrics.append(period)
    if period_metrics:
        positive_folds = sum(
            float(item.get("total_return_pct") or 0) > 0
            for item in period_metrics
        )
        metrics.update({
            "positive_fold_ratio_pct": round(
                positive_folds / len(period_metrics) * 100,
                3,
            ),
            "positive_folds": positive_folds,
            "total_folds": len(period_metrics),
        })
    return metrics


def _factor_cohort_adjudication_summary(
    strategy: dict[str, Any],
    ensemble: dict[str, Any],
) -> dict[str, Any]:
    """Return the small, decision-relevant cohort payload used by the live UI."""
    strategy_results = strategy.get("results") or {}
    validation_10bp = (
        (strategy_results.get("validation") or {})
        .get("round_trip_10bp", {})
        .get("metrics", {})
    )
    observed_10bp = (
        (strategy_results.get("observed_test") or {})
        .get("round_trip_10bp", {})
        .get("metrics", {})
    )
    strategy_definition = strategy.get("strategy") or {}
    return {
        "strategy": {
            "available": bool(strategy),
            "status": strategy.get("status"),
            "factor_id": strategy_definition.get("factor_id"),
            "holding_sessions": strategy_definition.get("holding_sessions"),
            "maximum_positions": strategy_definition.get("maximum_positions"),
            "historical_strategy_gate_passed": bool(
                strategy.get("historical_strategy_gate_passed")
            ),
            "production_gate_passed": bool(
                strategy.get("production_gate_passed")
            ),
            "real_money_gate_passed": bool(
                strategy.get("real_money_gate_passed")
            ),
            "validation_10bp": validation_10bp,
            "observed_test_10bp": observed_10bp,
            "failed_gates": strategy.get("failed_gates") or [],
            "evidence_class": strategy.get("historical_evidence_class"),
            "warning": strategy.get("warning"),
        },
        "weighted_ensemble": {
            "available": bool(ensemble),
            "status": ensemble.get("status"),
            "required_components": int(
                ensemble.get("required_components") or 2
            ),
            "available_components": int(
                ensemble.get("available_components") or 0
            ),
            "weights": ensemble.get("weights") or [],
            "historical_strategy_gate_passed": bool(
                ensemble.get("historical_strategy_gate_passed")
            ),
            "failed_gates": ensemble.get("failed_gates") or [],
            "warning": ensemble.get("warning"),
        },
    }


# Keep the original private name for callers and tests that predate V29.
_v27_adjudication_summary = _factor_cohort_adjudication_summary
def _data_rebuild_progress_snapshot(report_dir: Path) -> dict[str, Any]:
    """Aggregate mutually exclusive download shards without fake progress."""
    main = _load_json(report_dir / "v4_daily_download_progress.json")
    shard_paths = sorted(
        report_dir.glob("v4_daily_download_progress.shard-*-of-*.json")
    )
    shards = [_load_json(path) for path in shard_paths]
    shards = [
        item
        for item in shards
        if item and (item.get("shard") or {}).get("count")
    ]
    if not shards:
        return main
    shard_counts = {
        int((item.get("shard") or {}).get("count") or 0)
        for item in shards
    }
    query_ends = {
        str((item.get("query") or {}).get("end_date") or "")
        for item in shards
    }
    if len(shard_counts) != 1 or len(query_ends) != 1:
        return main
    expected_count = next(iter(shard_counts))
    by_index = {
        int((item.get("shard") or {}).get("index")): item
        for item in shards
        if (item.get("shard") or {}).get("index") is not None
    }
    if len(by_index) != expected_count:
        # Partial shard startup is still more truthful than an older global
        # progress file; missing shards contribute zero completed units.
        expected_indices = range(expected_count)
    else:
        expected_indices = sorted(by_index)
    selected = [by_index[index] for index in expected_indices if index in by_index]
    def effective_state(item: dict[str, Any]) -> str:
        state = str(item.get("state") or "unknown")
        updated_at = item.get("updated_at")
        if state not in {"starting", "running"} or not updated_at:
            return state
        try:
            observed = datetime.fromisoformat(str(updated_at))
            if observed.tzinfo is None:
                observed = observed.astimezone()
            age = (
                datetime.now().astimezone() - observed
            ).total_seconds()
        except (TypeError, ValueError):
            return state
        return "paused" if age > 90 else state

    completed = sum(int(item.get("completed_symbols") or 0) for item in selected)
    target = sum(int(item.get("target_symbols") or 0) for item in selected)
    if len(by_index) < expected_count:
        declared_targets = [
            int(((item.get("shard") or {}).get("target_symbols")) or 0)
            for item in selected
        ]
        fallback_target = max(declared_targets, default=0)
        target += fallback_target * (expected_count - len(by_index))
    states = [effective_state(item) for item in selected]
    running = any(state in {"starting", "running"} for state in states)
    all_completed = (
        len(by_index) == expected_count
        and states
        and all(state == "completed" for state in states)
    )
    state = (
        "completed" if all_completed
        else "running" if running
        else "partial"
    )
    eta_values = [
        int(item["eta_seconds"])
        for item in selected
        if item.get("eta_seconds") is not None
        and effective_state(item) in {"starting", "running"}
    ]
    failures = [
        failure
        for item in selected
        for failure in (item.get("session_failures") or [])
    ]
    return {
        "schema_version": 1,
        "state": state,
        "started_at": min(
            (
                str(item.get("started_at"))
                for item in selected
                if item.get("started_at")
            ),
            default=None,
        ),
        "updated_at": max(
            (
                str(item.get("updated_at"))
                for item in selected
                if item.get("updated_at")
            ),
            default=None,
        ),
        "query": selected[0].get("query") if selected else {},
        "target_symbols": target,
        "completed_symbols": completed,
        "remaining_symbols": max(0, target - completed),
        "progress_pct": round(
            completed / target * 100,
            3,
        ) if target else 0.0,
        "symbols_per_minute": round(sum(
            float(item.get("symbols_per_minute") or 0)
            for item in selected
            if effective_state(item) in {"starting", "running"}
        ), 3),
        "eta_seconds": max(eta_values) if eta_values else None,
        "session_failures": failures,
        "shards": [
            {
                "index": (item.get("shard") or {}).get("index"),
                "state": effective_state(item),
                "completed_symbols": item.get("completed_symbols"),
                "target_symbols": item.get("target_symbols"),
                "current_code": item.get("current_code"),
                "eta_seconds": item.get("eta_seconds"),
            }
            for item in selected
        ],
        "progress_definition": (
            "完成数为互不重叠下载分片中已写入完整gzip且目标截止日"
            "匹配的证券之和；等待和运行时长不增加进度。"
        ),
    }


def _sha256_file(path: Path | None) -> str | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _registered_v23_gate(
    report: dict[str, Any],
    integrity_audit: dict[str, Any] | None = None,
    *,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """Recalculate V23 and require a matching V24 audit, fail closed."""
    conditions = report.get("gate_conditions") or {}
    report_conditions_passed = bool(
        conditions
        and all(value is True for value in conditions.values())
        and report.get("historical_strategy_gate_passed") is True
        and report.get("research_quality_gate_passed") is True
    )
    audit = integrity_audit or {}
    metric_checks = audit.get("metric_checks") or {}
    trade_implementation = audit.get("trade_implementation") or {}
    claims = audit.get("claims") or {}
    expected_hash = (
        (audit.get("input_fingerprints") or {}).get("v23_report_sha256")
    )
    actual_hash = _sha256_file(report_path)
    audit_matches_report = bool(
        expected_hash and actual_hash and expected_hash == actual_hash
    )
    integrity_passed = bool(
        audit.get("integrity_audit_passed") is True
        and metric_checks
        and all(value is True for value in metric_checks.values())
        and trade_implementation.get("passed") is True
        and claims.get("historical_costed_profit_positive") is True
        and claims.get("buy_sell_implementation_matches_frozen_rule") is True
        and audit_matches_report
    )
    passed = bool(report_conditions_passed and integrity_passed)
    failed = [key for key, value in conditions.items() if value is not True]
    if not report_conditions_passed:
        failed.append("v23_registered_conditions")
    if not integrity_passed:
        failed.append("v24_integrity_audit")
    if expected_hash and actual_hash and not audit_matches_report:
        failed.append("v24_v23_hash_match")
    return {
        "passed": passed,
        "historical_report_passed": report_conditions_passed,
        "integrity_audit_passed": integrity_passed,
        "audit_matches_report": audit_matches_report,
        "expected_report_sha256": expected_hash,
        "actual_report_sha256": actual_hash,
        "conditions": conditions,
        "failed_gates": list(dict.fromkeys(failed)),
        "metrics": report.get("summary_10bp") or {},
        "evidence_layout": "v23_registered_event_regime",
        "reason": (
            "V23预注册条件与匹配的V24独立完整性审计均通过；"
            "这只代表历史证据，不代表前向、模拟盘或真钱获准。"
            if passed
            else "V23预注册条件、V24完整性审计或报告哈希匹配未通过。"
        ),
    }


def _product_historical_gate(paper: dict[str, Any]) -> dict[str, Any]:
    """Return the only historical result that may open product forward work.

    V23/V24 remain valuable *research-integrity* evidence, but their legacy
    gate was deliberately weaker than the product policy.  In particular it
    did not require the same frozen strategy to clear both validation and
    observation samples with 100 trades, 60% cost-inclusive win rate and 15%
    annualised return.  Product-facing callers must therefore use the
    hash-pinned certificate produced by ``paper_forward_prepare`` instead of
    inferring eligibility from a legacy report boolean.
    """
    conditions = paper.get("conditions") or {}
    safety = paper.get("safety") or {}
    legacy = paper.get("legacy_formal_clock") or {}
    legacy_detected = legacy.get("detected") is True
    legacy_excluded = bool(
        not legacy_detected
        or (
            legacy.get("revoked") is True
            and legacy.get("counts_toward_product_forward") is False
            and safety.get(
                "legacy_formal_clock_excluded_from_product_counts"
            ) is True
        )
    )
    certificate_valid = bool(
        paper.get("strict_product_gate_certificate_valid") is True
        and conditions.get("strict_product_gate_certificate_valid") is True
    )
    passed = bool(certificate_valid and legacy_excluded)
    requirements = (
        "同一冻结策略的验证段与观察段各至少100笔，含成本胜率≥60%、"
        "含成本年化≥15%，并完成独立复现"
    )
    if passed:
        reason = (
            "严格产品历史门禁证书已校验；可进入不可回填的正式前向纸面观察。"
        )
    elif legacy_detected and legacy_excluded:
        reason = (
            "V23/V24仅保留为历史完整性证据；旧正式时钟已撤销为影子历史，"
            f"不计入产品前向。产品候选仍须满足：{requirements}。"
        )
    elif legacy_detected:
        reason = (
            "检测到未被排除的旧前向时钟，产品门禁按失败关闭；"
            f"新候选仍须满足：{requirements}。"
        )
    else:
        reason = (
            "尚无已校验的严格产品历史门禁证书；产品候选须满足："
            f"{requirements}。"
        )
    return {
        "passed": passed,
        "strict_product_gate_certificate_valid": certificate_valid,
        "legacy_clock_excluded": legacy_excluded,
        "requirements": requirements,
        "reason": reason,
    }


def _date_age_days(value: Any, now: datetime) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
    return max(0, (now.date() - parsed.date()).days)


def _market_day_age(value: Any, now: datetime) -> int | None:
    """Count elapsed weekday sessions, so weekends do not create false staleness.

    Exchange holidays may still require the official calendar audit, but this
    conservative weekday clock fixes the deterministic Friday-to-weekend bug.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value)[:10]).date()
    except (TypeError, ValueError):
        return None
    if parsed >= now.date():
        return 0
    cursor = parsed
    elapsed = 0
    while cursor < now.date():
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            elapsed += 1
    return elapsed


def _daily_universe_scope(download: dict[str, Any]) -> dict[str, Any]:
    declared = download.get("universe_scope") or {}
    board_scope = str(download.get("board_scope") or "")
    target_symbols = int(download.get("target_symbols") or 0)
    legacy_scoped_manifest = bool(
        "创业板" in board_scope or target_symbols == 4_802
    )
    scope_ready = bool(download.get("ready_for_factor_research"))
    return {
        "id": str(declared.get("id") or (
            "sh_sz_main_chinext" if legacy_scoped_manifest else "unknown"
        )),
        "label": str(declared.get("label") or "沪深主板与创业板"),
        "included_segments": declared.get("included_segments") or [
            "上海主板",
            "深圳主板",
            "创业板",
        ],
        "excluded_segments": declared.get("excluded_segments") or [
            "科创板",
            "北交所",
        ],
        "scope_complete": bool(
            declared.get("scope_complete", scope_ready)
        ),
        "full_a_share_coverage": bool(
            declared.get(
                "full_a_share_coverage",
                download.get("full_a_share_coverage", False),
            )
        ),
        "target_symbols": target_symbols,
        "definition": (
            "完整率只针对已定义的沪深主板+创业板研究范围；"
            "科创板与北交所未计入。"
        ),
    }


def _data_freshness_snapshot(
    report_dir: Path,
    *,
    now: datetime,
) -> dict[str, Any]:
    incremental = _load_json(report_dir / "v4_daily_incremental_update.json")
    download = _load_json(report_dir / "v4_daily_download_manifest.json")
    universe_scope = _daily_universe_scope(download)
    quality = _load_json(report_dir / "v4_daily_quality_audit.json")
    current_index = _load_json(report_dir / "current_market_index_update.json")
    v21_pilot = _load_json(report_dir / "v21_shareholder_increase_pilot.json")
    daily_dates = [
        str(item.get("end") or item.get("last_date") or "")
        for item in (incremental.get("results") or [])
        if item.get("end") or item.get("last_date")
    ]
    daily_as_of = (
        incremental.get("actual_market_data_end")
        or max(daily_dates, default=None)
    )
    daily_data_dir = (
        report_dir.parent / "research" / "data" / "v4_daily"
    )
    physical_daily_files = (
        sum(1 for _ in daily_data_dir.glob("*.csv.gz"))
        if daily_data_dir.is_dir()
        else 0
    )
    expected_download_hash = (
        quality.get("input_fingerprints") or {}
    ).get("download_manifest_sha256")
    quality_matches_download = bool(
        expected_download_hash
        and expected_download_hash == _sha256_file(
            report_dir / "v4_daily_download_manifest.json"
        )
    )
    expected_symbols = int(download.get("target_symbols") or 0)
    daily_integrity_ok = bool(
        expected_symbols > 0
        and incremental.get("complete_universe_check") is True
        and int(incremental.get("checked_symbols") or 0) == expected_symbols
        and not (incremental.get("failures") or [])
        and physical_daily_files == expected_symbols
        and download.get("ready_for_factor_research") is True
        and int(download.get("downloaded_symbols") or 0) == expected_symbols
        and not (download.get("failures") or [])
        and quality.get("ready_for_factor_research") is True
        and quality_matches_download
        and int(quality.get("passed_files") or 0) == expected_symbols
        and int(quality.get("failed_files") or 0) == 0
    )
    index_as_of = current_index.get("latest_available_date")
    event_as_of = (v21_pilot.get("data_window") or {}).get("end")

    def feed(
        *,
        feed_id: str,
        label: str,
        as_of: Any,
        source: Any,
        fresh_after_days: int,
        integrity_ok: bool | None = None,
    ) -> dict[str, Any]:
        age_days = _market_day_age(as_of, now)
        calendar_age_days = _date_age_days(as_of, now)
        status = (
            "invalid"
            if integrity_ok is False
            else "unknown"
            if age_days is None
            else "fresh" if age_days <= fresh_after_days else "stale"
        )
        return {
            "id": feed_id,
            "label": label,
            "as_of": as_of,
            "age_days": age_days,
            "calendar_age_days": calendar_age_days,
            "age_basis": "elapsed_weekday_sessions",
            "fresh_after_days": fresh_after_days,
            "status": status,
            "source": source,
            "integrity_ok": integrity_ok,
            "physical_file_count": (
                physical_daily_files
                if feed_id == "full_universe_daily"
                else None
            ),
            "download_manifest_ready": (
                download.get("ready_for_factor_research") is True
                if feed_id == "full_universe_daily"
                else None
            ),
            "quality_audit_ready": (
                (
                    quality.get("ready_for_factor_research") is True
                    and quality_matches_download
                )
                if feed_id == "full_universe_daily"
                else None
            ),
            "quality_matches_download_manifest": (
                quality_matches_download
                if feed_id == "full_universe_daily"
                else None
            ),
            "quality_passed_files": (
                int(quality.get("passed_files") or 0)
                if feed_id == "full_universe_daily"
                else None
            ),
        }

    feeds = [
        feed(
            feed_id="full_universe_daily",
            label=(
                f"{universe_scope['label']}"
                f"{expected_symbols or 0}只日线"
            ),
            as_of=daily_as_of,
            source=incremental.get("source"),
            fresh_after_days=3,
            integrity_ok=daily_integrity_ok,
        ),
        feed(
            feed_id="current_index",
            label="当前上证指数",
            as_of=index_as_of,
            source=current_index.get("source"),
            fresh_after_days=1,
        ),
        feed(
            feed_id="shareholder_events",
            label="股东增持事件",
            as_of=event_as_of,
            source=(v21_pilot.get("source_audit") or {}).get("source"),
            fresh_after_days=1,
        ),
    ]
    invalid = [item for item in feeds if item["status"] == "invalid"]
    stale = [item for item in feeds if item["status"] == "stale"]
    unknown = [item for item in feeds if item["status"] == "unknown"]
    overall_status = (
        "invalid" if invalid
        else "stale" if stale
        else "unknown" if unknown
        else "fresh"
    )
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "overall_status": overall_status,
        "all_required_feeds_fresh": overall_status == "fresh",
        "feeds": feeds,
        "universe_scope": universe_scope,
        "invalid_feed_ids": [item["id"] for item in invalid],
        "stale_feed_ids": [item["id"] for item in stale],
        "unknown_feed_ids": [item["id"] for item in unknown],
        "definition": (
            "每个数据源单独显示as-of；周末不计为交易日。"
            "全市场日线超过3个工作日、当前指数或事件源超过1个工作日"
            "即视为不新鲜；法定休市仍由交易日历审计补充。"
        ),
    }


def _build_gate_levels(
    *,
    v23_gate: dict[str, Any],
    paper: dict[str, Any],
    data_freshness: dict[str, Any],
    run_manifest: dict[str, Any],
) -> dict[str, Any]:
    paper_conditions = paper.get("conditions") or {}
    ledger = paper.get("ledger") or {}
    safety = paper.get("safety") or {}
    legacy_historical_passed = bool(v23_gate.get("passed"))
    product_historical = _product_historical_gate(paper)
    historical_passed = bool(product_historical["passed"])
    forward_passed = bool(
        historical_passed
        and paper.get("product_forward_clock_valid") is True
        and paper_conditions.get("elapsed_at_least_6_weeks") is True
        and paper_conditions.get("completed_trades_at_least_30") is True
    )
    simulation_passed = bool(
        forward_passed
        and paper.get("paper_gate_passed") is True
        and ledger.get("append_only") is True
        and ledger.get("hash_chained") is True
        and safety.get("gate_bypass") is False
        and safety.get("paper_gate_auto_unlock") is False
    )
    independent_reproduction_passed = bool(
        (run_manifest.get("verification") or {}).get(
            "independent_environment_reproduced"
        ) is True
    )
    real_money_passed = bool(
        simulation_passed
        and data_freshness.get("all_required_feeds_fresh") is True
        and independent_reproduction_passed
    )

    def level(
        *,
        gate_id: str,
        label: str,
        passed: bool,
        conditions: dict[str, bool],
        reason: str,
    ) -> dict[str, Any]:
        return {
            "id": gate_id,
            "label": label,
            "passed": passed,
            "status": "passed" if passed else "locked",
            "conditions": conditions,
            "failed_conditions": [
                key for key, value in conditions.items() if value is not True
            ],
            "reason": reason,
        }

    return {
        "research_access": {
            "passed": True,
            "status": "available",
            "reason": "图表、研究分析和人工记录保持可用。",
        },
        "historical": level(
            gate_id="historical",
            label="产品历史门禁",
            passed=historical_passed,
            conditions={
                "strict_product_gate_certificate_valid": bool(
                    product_historical[
                        "strict_product_gate_certificate_valid"
                    ]
                ),
                "legacy_clock_excluded": bool(
                    product_historical["legacy_clock_excluded"]
                ),
            },
            reason=str(product_historical["reason"]),
        ),
        "legacy_historical_evidence": level(
            gate_id="legacy_historical_evidence",
            label="历史完整性证据（不计产品门禁）",
            passed=legacy_historical_passed,
            conditions={
                "v23_registered_conditions": bool(
                    v23_gate.get("historical_report_passed")
                ),
                "v24_integrity_audit": bool(
                    v23_gate.get("integrity_audit_passed")
                ),
                "v24_v23_hash_match": bool(
                    v23_gate.get("audit_matches_report")
                ),
            },
            reason=(
                "V23/V24历史复现与完整性审计已核对；"
                "该旧证据不等同于严格产品历史门禁，也不能启动正式前向。"
                if legacy_historical_passed
                else "V23/V24历史完整性证据尚未通过。"
            ),
        ),
        "forward": level(
            gate_id="forward",
            label="前向验证",
            passed=forward_passed,
            conditions={
                "product_historical_gate_passed": historical_passed,
                "product_forward_clock_valid": (
                    paper.get("product_forward_clock_valid") is True
                ),
                "elapsed_at_least_6_weeks": (
                    paper_conditions.get("elapsed_at_least_6_weeks") is True
                ),
                "completed_trades_at_least_30": (
                    paper_conditions.get("completed_trades_at_least_30") is True
                ),
            },
            reason=(
                "日历时间与完成交易数均达到冻结目标。"
                if forward_passed
                else (
                    "前向验证尚未启动：严格产品历史门禁证书无效或缺失。"
                    if not historical_passed
                    else "前向验证必须同时满足至少6周和至少30笔完成交易。"
                )
            ),
        ),
        "simulation": level(
            gate_id="simulation",
            label="模拟交易",
            passed=simulation_passed,
            conditions={
                "forward_validation_passed": forward_passed,
                "paper_gate_passed": paper.get("paper_gate_passed") is True,
                "append_only_ledger": ledger.get("append_only") is True,
                "hash_chained_ledger": ledger.get("hash_chained") is True,
                "no_gate_bypass": safety.get("gate_bypass") is False,
            },
            reason=(
                "前向模拟盘及不可变账本门禁均通过。"
                if simulation_passed
                else "模拟盘门禁或不可变账本条件尚未全部通过。"
            ),
        ),
        "real_money": level(
            gate_id="real_money",
            label="真钱允许",
            passed=real_money_passed,
            conditions={
                "simulation_gate_passed": simulation_passed,
                "all_required_feeds_fresh": bool(
                    data_freshness.get("all_required_feeds_fresh")
                ),
                "independent_environment_reproduced": (
                    independent_reproduction_passed
                ),
            },
            reason=(
                "模拟盘、数据新鲜度和独立环境复现均通过。"
                if real_money_passed
                else "真钱保持锁定：模拟盘、数据新鲜度和独立环境复现必须全部通过。"
            ),
        ),
        "automatic_trading": {
            "passed": False,
            "status": "locked",
            "reason": "自动交易未纳入本次P0授权，始终保持关闭。",
        },
    }


def _production_gate_blockers(
    gate_levels: dict[str, Any],
) -> list[dict[str, str]]:
    """Expose every still-locked production gate to API consumers.

    Historical research passing must not erase forward, simulation,
    independent-reproduction, or automatic-trading blockers.
    """

    blockers: list[dict[str, str]] = []
    definitions = (
        ("historical", "严格产品历史门禁未达", "blocking"),
        ("forward", "前向验证未达门禁", "blocking"),
        ("simulation", "模拟交易未达门禁", "blocking"),
        ("real_money", "真钱门禁仍锁定", "blocking"),
        ("automatic_trading", "自动交易未授权", "safety"),
    )
    for gate_id, title, severity in definitions:
        gate = gate_levels.get(gate_id) or {}
        if gate.get("passed") is True:
            continue
        failed = list(gate.get("failed_conditions") or [])
        detail = str(gate.get("reason") or "该门禁尚未通过。")
        if failed:
            detail += " 未通过条件：" + "、".join(failed) + "。"
        blockers.append({
            "severity": severity,
            "gate_id": gate_id,
            "title": title,
            "detail": detail,
        })
    return blockers


def _tail_lines(path: Path | None, limit: int) -> list[str]:
    """Read only the last *limit* lines of an append-only UTF-8 log.

    The worker event ledger can grow for weeks.  Reading the whole file on
    every browser poll made the web process CPU-bound even though the UI only
    consumes a small recent window.
    """
    if path is None or limit <= 0 or not path.exists():
        return []
    block_size = 64 * 1024
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            position = stream.tell()
            chunks: list[bytes] = []
            newline_count = 0
            while position > 0 and newline_count <= limit:
                size = min(block_size, position)
                position -= size
                stream.seek(position)
                chunk = stream.read(size)
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
    except OSError:
        return []
    raw = b"".join(reversed(chunks))
    return raw.decode("utf-8", errors="replace").splitlines()[-limit:]


def _read_events(path: Path | None, limit: int = 18) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    events = []
    for line in _tail_lines(path, limit):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        events.append({
            "time": item.get("time"),
            "kind": item.get("kind"),
            "cycle": item.get("cycle"),
            "message": item.get("message"),
            "duration_seconds": item.get("duration_seconds"),
            "audit_failed": item.get("audit_failed"),
            "unresolved_limit_events": item.get(
                "unresolved_limit_events"
            ),
            "usage": item.get("usage"),
        })
    return events


def _heartbeat_age_seconds(value: Any) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        now = datetime.now().astimezone()
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return max(0, int((now - parsed).total_seconds()))
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone() if parsed.tzinfo else parsed.astimezone()


def _v25_campaign_snapshot(
    *,
    report_dir: Path,
    worker: dict[str, Any],
) -> dict[str, Any]:
    """Return source-backed V25 task progress, separate from gate odds."""
    status = _load_json(report_dir / "v25_structural_factor_status.json")
    report_path = report_dir / "v25_structural_factor_research.json"
    report = _load_json(report_path)
    manifest_path = report_dir / "DEEPSEEK_MULTI_REVIEW_MANIFEST.json"
    manifest = _load_json(manifest_path)
    protocol_path = (
        report_dir.parent
        / "research"
        / "V25_STRUCTURAL_FACTORS_PREREGISTRATION.md"
    )
    local_progress = max(
        0.0, min(100.0, float(status.get("campaign_progress_pct") or 0))
    )
    if report:
        local_progress = 100.0
    report_mtime = report_path.stat().st_mtime if report_path.exists() else None
    manifest_mtime = (
        manifest_path.stat().st_mtime if manifest_path.exists() else None
    )
    review_is_for_v25 = bool(
        report_mtime is not None
        and manifest_mtime is not None
        and manifest_mtime >= report_mtime
    )
    raw_tasks = manifest.get("tasks") or []
    review_tasks = raw_tasks if isinstance(raw_tasks, list) else []
    review_resolved = sum(
        isinstance(item, dict)
        and item.get("status") in {"success", "failed"}
        for item in review_tasks
    )
    review_total = len(review_tasks) or 6
    deepseek = worker.get("deepseek") or {}
    paid_api_disabled = bool(
        worker.get("external_paid_api_disabled")
        or deepseek.get("paid_api_disabled")
    )
    active_tasks = deepseek.get("active_tasks") or []
    if not isinstance(active_tasks, list):
        active_tasks = []
    # Discovery roles and the six-way V25 red team use the same active-task
    # field. Only the explicit review step counts toward V25's final 10%.
    deepseek_active = bool(
        not paid_api_disabled
        and worker.get("current_step") == "DeepSeek 研究红队"
    )
    review_progress = (
        min(100.0, review_resolved / review_total * 100)
        if review_is_for_v25 else 0.0
    )
    overall = (
        round(local_progress, 3)
        if paid_api_disabled
        else round(local_progress * 0.90 + review_progress * 0.10, 3)
    )
    if report and (
        paid_api_disabled
        or (review_is_for_v25 and review_resolved >= review_total)
    ):
        overall = 100.0

    phase = str(status.get("phase") or "queued")
    if overall >= 100:
        current_task = (
            "V25本地确定性检验已完成；外部付费 API 未参与"
            if paid_api_disabled
            else "V25本地检验和DeepSeek独立红队均已完成"
        )
        state = "completed"
    elif report and deepseek_active:
        current_task = "DeepSeek正在对V25代码、数据泄漏和稳健性做六路只读复核"
        state = "ai_review"
    elif report:
        current_task = "V25本地结果已落盘，等待DeepSeek独立红队读取新指纹"
        state = "review_pending"
    elif status.get("status") == "failed":
        current_task = status.get("message") or "V25本地研究失败"
        state = "failed"
    elif status.get("status") == "running":
        current_task = status.get("message") or status.get("phase_label")
        state = "running"
    else:
        current_task = "V25已进入事件驱动队列，等待研究工人领取"
        state = "queued"

    local_eta = status.get("eta_seconds")
    if local_eta is not None:
        local_eta = max(0, int(local_eta))
    review_durations = []
    for item in review_tasks:
        if not isinstance(item, dict):
            continue
        started = _parse_time(item.get("started_at"))
        finished = _parse_time(item.get("finished_at"))
        if started and finished and finished >= started:
            review_durations.append((finished - started).total_seconds())
    typical_review_seconds = (
        max(1, int(round(max(review_durations))))
        if review_durations else None
    )
    if overall >= 100:
        eta_seconds = 0
        eta_label = "已完成"
    elif report and review_is_for_v25 and typical_review_seconds is not None:
        eta_seconds = max(
            0,
            typical_review_seconds
            - int(worker.get("step_elapsed_seconds") or 0),
        )
        eta_label = "按最近一次六路红队实际耗时估算"
    elif report:
        eta_seconds = typical_review_seconds
        eta_label = (
            "等待红队启动；按最近一次实际耗时估算"
            if typical_review_seconds is not None
            else "等待本次红队产生首个实际耗时样本"
        )
    elif local_eta is not None:
        eta_seconds = local_eta + (typical_review_seconds or 0)
        eta_label = "按当前处理速率与最近红队实际耗时估算"
    else:
        eta_seconds = None
        eta_label = "等待前25只股票建立真实处理速率"

    phase_units = int(status.get("phase_completed_units") or 0)
    phase_total = int(status.get("phase_total_units") or 0)
    phase_progress = float(status.get("phase_progress_pct") or 0)
    tasks = [
        {
            "id": "preregistration",
            "label": "冻结三项机制、公式和失败条件",
            "weight_pct": 5,
            "status": "completed" if protocol_path.exists() else "pending",
        },
        {
            "id": "panel_build",
            "label": "4,802只真实A股点时因子面板",
            "weight_pct": 40,
            "status": (
                "completed" if report or phase in {
                    "cross_section_tests", "event_study", "completed"
                }
                else "running" if phase == "panel_build" else "pending"
            ),
        },
        {
            "id": "cross_section_tests",
            "label": "H1/H2训练、验证、测试与Holm裁决",
            "weight_pct": 30,
            "status": (
                "completed" if report or phase in {"event_study", "completed"}
                else "running" if phase == "cross_section_tests" else "pending"
            ),
        },
        {
            "id": "event_study",
            "label": "H3非涨停隔夜跳空事件研究",
            "weight_pct": 15,
            "status": (
                "completed" if report or phase == "completed"
                else "running" if phase == "event_study" else "pending"
            ),
        },
        {
            "id": "deepseek_red_team",
            "label": (
                "外部付费 API 已禁用（不计入研究门禁）"
                if paid_api_disabled
                else "DeepSeek六路代码与稳健性红队"
            ),
            "weight_pct": 0 if paid_api_disabled else 10,
            "status": (
                "disabled" if paid_api_disabled
                else "completed" if review_is_for_v25 and review_resolved >= review_total
                else "running" if report and deepseek_active
                else "pending"
            ),
        },
    ]
    return {
        "version": "V25",
        "state": state,
        "overall_progress_pct": overall,
        "remaining_pct": round(max(0.0, 100.0 - overall), 3),
        "current_task": current_task,
        "current_phase": status.get("phase_label") or "等待队列",
        "phase_progress_pct": phase_progress,
        "phase_completed_units": phase_units,
        "phase_total_units": phase_total,
        "current_unit": status.get("current_unit"),
        "actual_compute_seconds": float(
            status.get("actual_compute_seconds") or 0
        ),
        "eta_seconds": eta_seconds,
        "eta_label": eta_label,
        "updated_at": status.get("updated_at"),
        "report_generated_at": report.get("generated_at"),
        "statistical_gate_pass_count": int(
            report.get("statistical_gate_pass_count") or 0
        ),
        "statistical_gate_passes": report.get("statistical_gate_passes") or [],
        "tasks": tasks,
        "definition": (
            "百分比只表示V25固定任务工作量完成度，不表示策略通过概率；"
            "ETA是按真实处理速率计算的条件估算，不是历史门禁或盈利倒计时。"
        ),
    }


def _visible_manual_candidate(
    report: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any] | None:
    """Expose a V21 event only before its next-open entry window expires."""
    raw = report.get("current_candidate")
    if not isinstance(raw, dict) or not raw.get("code"):
        return None
    candidate = dict(raw)
    today = now.date().isoformat()
    entry_date = str(candidate.get("planned_entry_date") or "")
    if not entry_date:
        return candidate if candidate.get("announcement_date") == today else None
    if today < entry_date:
        candidate["phase"] = "prepare"
        candidate["phase_label"] = f"准备 {entry_date} 开盘前复核"
        return candidate
    if today == entry_date and now.strftime("%H:%M:%S") <= "09:35:00":
        candidate["phase"] = "entry_window"
        candidate["phase_label"] = "开盘执行性复核窗口"
        return candidate
    return None


def _raw_events(path: Path | None, limit: int = 300) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    output = []
    for line in _tail_lines(path, limit):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            output.append(value)
    return output


def _expected_stage_seconds(
    stage: dict[str, Any],
    events: list[dict[str, Any]],
) -> int:
    durations = [
        float(item["duration_seconds"])
        for item in events
        if (
            item.get("kind") == "command_done"
            and item.get("message") == stage["worker_step"]
            and item.get("duration_seconds") is not None
        )
    ]
    if stage["id"] == "deepseek":
        deepseek_durations = []
        for index, item in enumerate(events):
            if item.get("kind") != "deepseek_done":
                continue
            finished = _parse_time(item.get("time"))
            previous = next(
                (
                    _parse_time(candidate.get("time"))
                    for candidate in reversed(events[:index])
                    if candidate.get("kind") == "command_done"
                ),
                None,
            )
            if finished and previous:
                deepseek_durations.append(
                    max(1, (finished - previous).total_seconds())
                )
        durations.extend(deepseek_durations)
    return max(
        1,
        int(round(median(durations)))
        if durations
        else int(stage["default_seconds"]),
    )


def _latest_artifact(path: Path, label: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    generated_at = datetime.fromtimestamp(
        stat.st_mtime
    ).astimezone().isoformat(timespec="seconds")
    return {
        "label": label,
        "file": path.name,
        "generated_at": generated_at,
        "size_kb": round(stat.st_size / 1024, 1),
    }


def _incremental_live_detail(
    *,
    report_dir: Path,
    step_started_at: datetime | None,
) -> dict[str, Any]:
    """Prefer exact updater progress; expose an honest lower bound for old runs."""
    if step_started_at is None:
        return {}
    progress = _load_json(
        report_dir / "v4_daily_incremental_progress.json"
    )
    progress_started = _parse_time(progress.get("started_at"))
    if (
        progress
        and progress_started is not None
        and progress_started >= step_started_at - timedelta(seconds=5)
    ):
        return {
            **progress,
            "detail_mode": "exact",
        }

    data_dir = report_dir.parent / "research" / "data" / "v4_daily"
    try:
        files = list(data_dir.glob("*.csv.gz"))
        started_timestamp = step_started_at.timestamp()
        changed = sum(
            path.stat().st_mtime >= started_timestamp for path in files
        )
    except OSError:
        return {}
    return {
        "status": "running",
        "detail_mode": "legacy_lower_bound",
        "progress_source": "files_modified_since_step_start",
        "total_symbols": len(files),
        "processed_symbols": None,
        "updated_symbols_at_least": changed,
        "progress_pct_lower_bound": round(
            changed / len(files) * 100, 2
        ) if files else 0.0,
        "eta_seconds": None,
        "notice": (
            "当前进程由旧版更新器启动，只能显示已写入文件下限；"
            "下一轮将显示逐股精确进度、速度和ETA。"
        ),
    }


def build_research_live_status(
    *,
    report_dir: Path,
    worker_status_path: Path,
    worker_events_path: Path,
) -> dict[str, Any]:
    """Build a lightweight, polling-friendly view of research activity."""
    now = datetime.now().astimezone()
    worker = _load_json(worker_status_path)
    stable_target_campaign = _load_json(
        report_dir / "stable_target_campaign_status.json"
    )
    external_source = worker.get("external_source") or {}
    events = _raw_events(worker_events_path)
    heartbeat_age = _heartbeat_age_seconds(worker.get("heartbeat_at"))
    worker_status = worker.get("status") or "not_started"
    live = bool(
        worker_status in {
            "starting", "running", "retry_wait", "stopping"
        }
        and heartbeat_age is not None
        and heartbeat_age <= 300
    )
    current_step = worker.get("current_step") or "等待启动"
    candidate_computing = current_step.startswith((
        "验证开源候选：",
        "验证V18开源候选：",
        "验证V27新机制候选：",
        "验证V28价格几何候选：",
        "验证V29路径量能候选：",
        "验证V30趋势宽度候选：",
    ))
    v25_computing = current_step == "运行V25结构性因子三路预注册研究"
    cycle = int(worker.get("cycle") or 0)
    worker_started_at = _parse_time(worker.get("started_at"))
    cycle_events = [
        item for item in events
        if (
            int(item.get("cycle") or 0) == cycle
            and (
                worker_started_at is None
                or (
                    _parse_time(item.get("time")) is not None
                    and _parse_time(item.get("time")) >= worker_started_at
                )
            )
        )
    ]
    completed_steps = {
        item.get("message")
        for item in cycle_events
        if item.get("kind") in {
            "command_done", "experiment_skipped_unchanged"
        }
    }
    if any(
        item.get("kind") == "deepseek_done" for item in cycle_events
    ):
        completed_steps.add("DeepSeek 研究红队")
    if any(
        item.get("kind") == "deepseek_discovery_done"
        for item in cycle_events
    ):
        completed_steps.add("DeepSeek 连续机制研究")

    current_stage_index = next(
        (
            index
            for index, stage in enumerate(LIVE_CYCLE_STAGES)
            if stage["worker_step"] == current_step
        ),
        None,
    )
    waiting = current_step in {"轮次间隔", "等待新输入"}
    stage_rows = []
    stage_expected = []
    for index, stage in enumerate(LIVE_CYCLE_STAGES):
        expected = _expected_stage_seconds(stage, events)
        stage_expected.append(expected)
        if stage["worker_step"] in completed_steps:
            status = "completed"
        elif current_stage_index == index:
            status = "active"
        elif current_stage_index is not None and index < current_stage_index:
            status = "completed"
        else:
            status = "pending"
        stage_rows.append({
            "id": stage["id"],
            "label": stage["label"],
            "kind": stage["kind"],
            "status": status,
            "expected_seconds": expected,
        })

    latest_cycle_event_time = next(
        (
            _parse_time(item.get("time"))
            for item in reversed(cycle_events)
            if item.get("kind") in {
                "command_start", "command_done", "deepseek_done",
                "deepseek_discovery_done",
            }
        ),
        None,
    )
    step_started_at = _parse_time(worker.get("step_started_at"))
    if step_started_at is None and current_stage_index is not None:
        step_started_at = next(
            (
                _parse_time(item.get("time"))
                for item in reversed(cycle_events)
                if (
                    (
                        item.get("kind") == "command_start"
                        and item.get("message") == current_step
                    )
                    or (
                        current_step == "DeepSeek 研究红队"
                        and item.get("kind") == "command_done"
                    )
                )
            ),
            latest_cycle_event_time,
        )
    elapsed_seconds = (
        max(0, int((now - step_started_at).total_seconds()))
        if step_started_at and (
            current_stage_index is not None or candidate_computing
            or v25_computing
        )
        else 0
    )
    expected_seconds = (
        stage_expected[current_stage_index]
        if current_stage_index is not None
        else None
    )
    incremental_detail: dict[str, Any] = {}
    current_stage_fraction = (
        min(1.0, elapsed_seconds / expected_seconds)
        if expected_seconds
        else 0.0
    )
    if v25_computing:
        v25_status = _load_json(
            report_dir / "v25_structural_factor_status.json"
        )
        status_elapsed = int(v25_status.get("actual_compute_seconds") or 0)
        if status_elapsed:
            elapsed_seconds = status_elapsed
        remaining_seconds = v25_status.get("eta_seconds")
        if remaining_seconds is not None:
            remaining_seconds = int(remaining_seconds)
            expected_seconds = elapsed_seconds + remaining_seconds
        else:
            expected_seconds = None
        current_stage_fraction = min(
            1.0,
            max(
                0.0,
                float(v25_status.get("campaign_progress_pct") or 0) / 100,
            ),
        )
        incremental_detail = {
            "detail_mode": "exact",
            "completed_units": int(
                v25_status.get("phase_completed_units") or 0
            ),
            "total_units": int(v25_status.get("phase_total_units") or 0),
            "processed_symbols": int(
                v25_status.get("phase_completed_units") or 0
            ),
            "total_symbols": int(v25_status.get("phase_total_units") or 0),
            "progress_pct": float(
                v25_status.get("campaign_progress_pct") or 0
            ),
            "phase_progress_pct": float(
                v25_status.get("phase_progress_pct") or 0
            ),
            "eta_seconds": remaining_seconds,
            "current_unit": v25_status.get("phase_label"),
            "current_code": v25_status.get("current_unit"),
            "message": v25_status.get("message"),
        }
    elif candidate_computing:
        if current_step.startswith("验证V30"):
            candidate_status_name = "v30_open_source_factor_status.json"
        elif current_step.startswith("验证V29"):
            candidate_status_name = "v29_open_source_factor_status.json"
        elif current_step.startswith("验证V28"):
            candidate_status_name = "v28_open_source_factor_status.json"
        elif current_step.startswith("验证V27"):
            candidate_status_name = "v27_open_source_factor_status.json"
        elif current_step.startswith("验证V18"):
            candidate_status_name = "v18_open_source_factor_status.json"
        else:
            candidate_status_name = "v13_open_source_factor_status.json"
        candidate_status = _load_json(
            report_dir / candidate_status_name
        )
        status_elapsed = int(candidate_status.get("elapsed_seconds") or 0)
        status_remaining = candidate_status.get("eta_seconds")
        if status_elapsed:
            elapsed_seconds = status_elapsed
        if status_remaining is not None:
            remaining_seconds = int(status_remaining)
            expected_seconds = elapsed_seconds + remaining_seconds
        else:
            remaining_seconds = None
            expected_seconds = None
        current_stage_fraction = min(
            1.0,
            max(0.0, float(candidate_status.get("progress_pct") or 0) / 100),
        )
        incremental_detail = {
            "detail_mode": "exact",
            "completed_units": int(candidate_status.get("processed_symbols") or 0),
            "total_units": int(candidate_status.get("total_symbols") or 0),
            "processed_symbols": int(candidate_status.get("processed_symbols") or 0),
            "total_symbols": int(candidate_status.get("total_symbols") or 0),
            "progress_pct": float(candidate_status.get("progress_pct") or 0),
            "eta_seconds": remaining_seconds,
            "current_unit": candidate_status.get("phase"),
            "current_code": candidate_status.get("current_symbol"),
            "rate_symbols_per_second": float(
                candidate_status.get("rate_symbols_per_second") or 0
            ),
            "message": candidate_status.get("message"),
        }
    elif (
        current_stage_index is not None
        and LIVE_CYCLE_STAGES[current_stage_index]["id"]
        == "incremental_data"
    ):
        incremental_detail = _incremental_live_detail(
            report_dir=report_dir,
            step_started_at=step_started_at,
        )
        if incremental_detail.get("detail_mode") == "exact":
            current_stage_fraction = min(
                1.0,
                max(
                    0.0,
                    float(incremental_detail.get("progress_pct") or 0)
                    / 100,
                ),
            )
            elapsed_seconds = int(
                incremental_detail.get("elapsed_seconds")
                or elapsed_seconds
            )
            remaining_seconds = incremental_detail.get("eta_seconds")
            if remaining_seconds is not None:
                expected_seconds = elapsed_seconds + int(
                    remaining_seconds
                )
        else:
            current_stage_fraction = min(
                1.0,
                max(
                    0.0,
                    float(
                        incremental_detail.get(
                            "progress_pct_lower_bound"
                        ) or 0
                    ) / 100,
                ),
            )
            remaining_seconds = None
            expected_seconds = None
    elif (
        current_stage_index is not None
        and LIVE_CYCLE_STAGES[current_stage_index]["id"]
        == "v12_fundamentals"
    ):
        v12_status = _load_json(
            report_dir / "v12_fundamental_builder_status.json"
        )
        counts = v12_status.get("counts") or {}
        total = int(counts.get("total") or 0)
        completed = int(
            (counts.get("completed") or 0)
            + (counts.get("empty") or 0)
            + (counts.get("failed") or 0)
        )
        current_stage_fraction = (
            min(1.0, max(0.0, completed / total))
            if total else 0.0
        )
        rate = completed / elapsed_seconds if elapsed_seconds > 0 else 0.0
        remaining_seconds = (
            int((total - completed) / rate)
            if rate > 0 and completed < total else None
        )
        if remaining_seconds is not None:
            expected_seconds = elapsed_seconds + remaining_seconds
        incremental_detail = {
            "detail_mode": "exact",
            "completed_units": completed,
            "total_units": total,
            "processed_symbols": completed,
            "total_symbols": total,
            "progress_pct": round(current_stage_fraction * 100, 3),
            "eta_seconds": remaining_seconds,
            "records": int(v12_status.get("fundamental_records") or 0),
            "failed": int(counts.get("failed") or 0),
            "current_unit": v12_status.get("current_task"),
            "current_code": v12_status.get("current_task"),
            "rate_symbols_per_second": rate,
        }
    else:
        remaining_seconds = (
            max(0, expected_seconds - elapsed_seconds)
            if expected_seconds is not None
            else None
        )
    if current_stage_index is not None:
        stage_rows[current_stage_index]["progress_pct"] = round(
            current_stage_fraction * 100, 2
        )
    completed_expected = sum(
        stage_expected[index]
        for index, row in enumerate(stage_rows)
        if row["status"] == "completed"
    )
    total_expected = sum(stage_expected)
    cycle_progress_pct = (
        100.0 if waiting
        else round(
            min(
                1,
                (
                    completed_expected
                    + (
                        stage_expected[current_stage_index]
                        * current_stage_fraction
                        if current_stage_index is not None
                        else 0
                    )
                )
                / total_expected,
            )
            * 100,
            1,
        )
        if total_expected
        else 0.0
    )
    if (
        (candidate_computing or v25_computing)
        and incremental_detail.get("detail_mode") == "exact"
    ):
        cycle_progress_pct = round(
            float(incremental_detail.get("progress_pct") or 0), 1
        )

    deepseek = worker.get("deepseek") or {}
    paid_api_disabled = bool(
        worker.get("external_paid_api_disabled")
        or deepseek.get("paid_api_disabled")
    )
    last_deepseek = _parse_time(deepseek.get("last_run_at"))
    next_cycle = _parse_time(worker.get("next_cycle_at"))
    next_check = next_cycle if waiting else None
    if waiting:
        next_cycle = None
    seconds_until_next_cycle = (
        max(0, int((next_cycle - now).total_seconds()))
        if next_cycle else None
    )
    source_circuit_open = external_source.get("source_state") in {
        "circuit_open", "source_blocked", "fallback_blocked"
    }
    if not live:
        worker_state = "offline"
        worker_state_label = "未检测到工作心跳"
    elif v25_computing:
        worker_state = "computing"
        worker_state_label = "V25结构性因子真实检验中"
    elif candidate_computing:
        worker_state = "computing"
        worker_state_label = "本地全市场开源候选验证中"
    elif source_circuit_open:
        worker_state = "blocked"
        worker_state_label = "外部数据源熔断，后台仍在线"
    elif worker_status == "retry_wait":
        worker_state = "error"
        worker_state_label = "遇到错误，自动等待重试"
    elif waiting:
        worker_state = "waiting"
        worker_state_label = "在线守护，等待新的有效输入"
    elif not paid_api_disabled and current_step in {
        "DeepSeek 研究红队",
        "DeepSeek 连续机制研究",
    }:
        worker_state = "ai"
        worker_state_label = "DeepSeek正在独立审查"
    elif current_step in {"质量门禁阻断", "研究门禁阻断"}:
        worker_state = "blocked"
        worker_state_label = (
            "数据质量门禁阻断后续研究"
            if current_step == "质量门禁阻断"
            else "策略研究门禁阻断，独立任务已继续"
        )
    elif current_stage_index is not None:
        worker_state = "computing"
        worker_state_label = "本地程序正在计算"
    else:
        worker_state = "working"
        worker_state_label = "后台正在工作"

    v4_2 = _load_json(report_dir / "v4_2_microstructure_research.json")
    factor_mining = _load_json(report_dir / "factor_mining_report.json")
    v4_3 = _load_json(report_dir / "v4_3_strategy_validation.json")
    v4_4_factor = _load_json(report_dir / "v4_4_factor_research.json")
    v4_4_strategy = _load_json(
        report_dir / "v4_4_low_cost_strategy.json"
    )
    v5_factor = _load_json(
        report_dir / "v5_limit_up_factor_research.json"
    )
    v6_factor = _load_json(
        report_dir / "v6_non_limit_max_effect_research.json"
    )
    v6_strategy = _load_json(
        report_dir / "v6_non_limit_max_strategy_validation.json"
    )
    v13_factor = _load_json(
        report_dir / "v18_open_source_factor_research.json"
    ) or {
        "cohort": "V18",
        "registry": v18_registry_payload(),
        "candidates": {},
        "completed_candidate_count": 0,
        "total_candidate_count": len(
            v18_registry_payload().get("candidates") or []
        ),
    }
    v13_strategy = _load_json(
        report_dir / "v18_open_source_strategy_validation.json"
    )
    v27_factor = _load_json(
        report_dir / "v27_open_source_factor_research.json"
    )
    v27_strategy = _load_json(
        report_dir / "v27_open_source_strategy_validation.json"
    )
    v27_ensemble = _load_json(
        report_dir / "v27_weighted_ensemble_validation.json"
    )
    v27_adjudication = _v27_adjudication_summary(
        v27_strategy,
        v27_ensemble,
    )
    v29_strategy = _load_json(
        report_dir / "v29_open_source_strategy_validation.json"
    )
    v29_ensemble = _load_json(
        report_dir / "v29_weighted_ensemble_validation.json"
    )
    v29_adjudication = _factor_cohort_adjudication_summary(
        v29_strategy,
        v29_ensemble,
    )
    v30_strategy = _load_json(
        report_dir / "v30_open_source_strategy_validation.json"
    )
    v30_ensemble = _load_json(
        report_dir / "v30_weighted_ensemble_validation.json"
    )
    v30_adjudication = _factor_cohort_adjudication_summary(
        v30_strategy,
        v30_ensemble,
    )
    v27_status = _load_json(
        report_dir / "v27_open_source_factor_status.json"
    )
    v9_factor = _load_json(
        report_dir / "v9_margin_factor_research.json"
    )
    v14_factor = _load_json(
        report_dir / "v14_margin_balance_acceleration.json"
    )
    v14_strategy = _load_json(
        report_dir / "v14_margin_balance_strategy_validation.json"
    )
    v14_status = _load_json(
        report_dir / "v14_margin_balance_acceleration_status.json"
    )
    v15_source_audit = _load_json(
        report_dir / "v15_margin_eligibility_source_audit.json"
    )
    v15_source_status = _load_json(
        report_dir / "v15_margin_eligibility_source_audit_status.json"
    )
    v15_events = _load_json(
        report_dir / "v15_margin_eligibility_events.json"
    )
    v15_event_status = _load_json(
        report_dir / "v15_margin_eligibility_event_status.json"
    )
    v15_factor = _load_json(
        report_dir / "v15_margin_eligibility_factor_research.json"
    )
    v15_factor_status = _load_json(
        report_dir / "v15_margin_eligibility_factor_status.json"
    )
    v16_events = _load_json(
        report_dir / "v16_share_repurchase_events.json"
    )
    v16_event_status = _load_json(
        report_dir / "v16_share_repurchase_event_status.json"
    )
    v16_factor = _load_json(
        report_dir / "v16_share_repurchase_factor_research.json"
    )
    v16_factor_status = _load_json(
        report_dir / "v16_share_repurchase_factor_status.json"
    )
    v17_factor = _load_json(
        report_dir / "v17_limit_down_liquidity_recovery.json"
    )
    v17_factor_status = _load_json(
        report_dir / "v17_limit_down_liquidity_recovery_status.json"
    )
    software_tests = _load_json(report_dir / "test_suite_status.json")
    v10_factor = _load_json(
        report_dir / "v10_peer_return_gap_research.json"
    )
    v11_factor = _load_json(
        report_dir / "v11_unlock_announcement_pressure_research.json"
    )
    v12_status = _load_json(
        report_dir / "v12_fundamental_builder_status.json"
    )
    v18_open_source_factors = _load_json(
        report_dir / "v18_open_source_factor_research.json"
    )
    v27_open_source_factors = _load_json(
        report_dir / "v27_open_source_factor_research.json"
    ) or {
        "cohort": "V27",
        "registry": v27_registry_payload(),
        "candidates": {},
        "completed_candidate_count": 0,
        "total_candidate_count": len(
            v27_registry_payload().get("candidates") or []
        ),
        "production_gate_passed": False,
    }
    v28_open_source_factors = _load_json(
        report_dir / "v28_open_source_factor_research.json"
    ) or {
        "cohort": "V28",
        "registry": v28_registry_payload(),
        "candidates": {},
        "completed_candidate_count": 0,
        "total_candidate_count": len(
            v28_registry_payload().get("candidates") or []
        ),
        "production_gate_passed": False,
    }
    v29_open_source_factors = _load_json(
        report_dir / "v29_open_source_factor_research.json"
    ) or {
        "cohort": "V29",
        "registry": v29_registry_payload(),
        "candidates": {},
        "completed_candidate_count": 0,
        "total_candidate_count": len(
            v29_registry_payload().get("candidates") or []
        ),
        "production_gate_passed": False,
    }
    v30_open_source_factors = _load_json(
        report_dir / "v30_open_source_factor_research.json"
    ) or {
        "cohort": "V30",
        "registry": v30_registry_payload(),
        "candidates": {},
        "completed_candidate_count": 0,
        "total_candidate_count": len(
            v30_registry_payload().get("candidates") or []
        ),
        "production_gate_passed": False,
    }
    v19_lhb = _load_json(
        report_dir / "v19_lhb_factor_research.json"
    )
    v20_profit_first = _load_json(
        report_dir / "v20_profit_first_pilot.json"
    )
    v21_manual_pilot = _load_json(
        report_dir / "v21_shareholder_increase_pilot.json"
    )
    v22_risk_managed = _load_json(
        report_dir / "v22_v21_risk_policy_replication.json"
    )
    v23_market_regime = _load_json(
        report_dir / "v23_v21_market_regime_replication.json"
    )
    v23_current_decision = _load_json(
        report_dir / "v23_current_manual_decision.json"
    )
    v24_integrity_audit = _load_json(
        report_dir / "v24_v23_integrity_audit.json"
    )
    current_index_update = _load_json(
        report_dir / "current_market_index_update.json"
    )
    v23_gate = _registered_v23_gate(
        v23_market_regime,
        v24_integrity_audit,
        report_path=(
            report_dir / "v23_v21_market_regime_replication.json"
        ),
    )
    v23_historical_passed = bool(v23_gate["passed"])
    v19_lhb_gates = v19_lhb.get("gate_conditions") or {}
    v19_lhb_passed_gates = sum(bool(value) for value in v19_lhb_gates.values())
    v19_lhb_total_gates = len(v19_lhb_gates)
    v19_lhb_test = (v19_lhb.get("results") or {}).get("test") or {}
    v20_ten_bp = (
        (v20_profit_first.get("results") or {}).get("round_trip_10bp")
        or {}
    )
    v20_ten_bp_metrics = v20_ten_bp.get("metrics") or {}
    v20_passed = bool(
        v20_profit_first.get("profit_first_pilot_gate_passed")
    )
    v21_original_manual_unlocked = bool(
        v21_manual_pilot.get("profit_only_manual_pilot_gate_passed")
    )
    v22_current_manual_unlocked = bool(
        v22_risk_managed.get("current_regime_manual_pilot_gate_passed")
    )
    v21_manual_unlocked = bool(
        v22_current_manual_unlocked or v21_original_manual_unlocked
    )
    v21_research_quality_passed = bool(
        v23_historical_passed
        or v22_risk_managed.get("research_quality_gate_passed")
        if (v23_market_regime or v22_risk_managed)
        else v21_manual_pilot.get("research_quality_gate_passed")
    )
    v21_original_ten_bp_metrics = (
        (((v21_manual_pilot.get("results") or {}).get("round_trip_10bp") or {}).get("metrics"))
        or {}
    )
    v22_current_ten_bp_metrics = (
        v22_risk_managed.get("current_2026_summary_10bp") or {}
    )
    v22_combined_ten_bp_metrics = (
        v22_risk_managed.get("summary_10bp") or {}
    )
    v23_combined_ten_bp_metrics = (
        v23_market_regime.get("summary_10bp") or {}
    )
    v23_current_ten_bp_metrics = (
        v23_market_regime.get("current_2026_summary_10bp") or {}
    )
    v23_canonical_ten_bp_metrics = _canonical_v23_ten_bp_metrics(
        v23_market_regime
    )
    v21_ten_bp_metrics = (
        v23_current_ten_bp_metrics
        if v23_historical_passed
        else v22_current_ten_bp_metrics
        if v22_current_manual_unlocked
        else v21_original_ten_bp_metrics
    )
    v21_visible_candidate = _visible_manual_candidate(
        v21_manual_pilot,
        now=now,
    )
    if (
        v22_current_manual_unlocked
        and v21_visible_candidate
        and not re.match(
            r"^(?:00[0-3]|30[01]|60[0135])\d{3}$",
            str(v21_visible_candidate.get("code") or ""),
        )
    ):
        v21_visible_candidate = None
    if v23_historical_passed:
        if v23_current_decision:
            v21_visible_candidate = (
                dict(v23_current_decision.get("candidate") or {})
                if v23_current_decision.get("actionable_candidate") is True
                else None
            )
        elif v21_visible_candidate:
            latest_v23_signal = v23_market_regime.get(
                "latest_eligible_signal"
            ) or {}
            candidate_matches_v23 = bool(
                str(latest_v23_signal.get("code") or "")
                == str(v21_visible_candidate.get("code") or "")
                and str(latest_v23_signal.get("trade_date") or "")
                == str(v21_visible_candidate.get("announcement_date") or "")
            )
            if not candidate_matches_v23:
                v21_visible_candidate = None
    manual_execution_rule = dict(
        v21_manual_pilot.get("manual_execution_rule") or {}
    )
    if v22_current_manual_unlocked:
        manual_execution_rule["position_limit"] = (
            "单只最多账户权益20%，同时最多1只，100股整数手"
        )
    manual_limitations = (
        list(v23_market_regime.get("limitations") or [])
        if v23_historical_passed
        else list(v22_risk_managed.get("limitations") or [])
        if v22_current_manual_unlocked
        else list(v21_manual_pilot.get("limitations") or [])
    )
    open_source_factors = v18_open_source_factors or {
        "cohort": "V18",
        "registry": v18_registry_payload(),
        "candidates": {},
        "completed_candidate_count": 0,
        "total_candidate_count": len(
            v18_registry_payload().get("candidates") or []
        ),
        "production_gate_passed": False,
    }
    deepseek_assessment = _load_json(
        report_dir / "DEEPSEEK_DISCOVERY_ASSESSMENT_LATEST.json"
    )
    deepseek_discovery_round = _load_json(
        report_dir / "DEEPSEEK_DISCOVERY_ROUND_LATEST.json"
    )
    accelerated_tracker = accelerated_simulation_snapshot(
        status_path=(
            report_dir / "accelerated_historical_simulation_status.json"
        )
    )
    fifteen_hour_session = fifteen_hour_session_snapshot(
        status_path=(
            report_dir / "fifteen_hour_validation_session.json"
        ),
        events_path=worker_events_path,
    )
    v5_strategy = _load_json(
        report_dir / "v5_limit_up_strategy_validation.json"
    )
    paper = _load_json(report_dir / "paper_forward_status.json")
    run_manifest = _load_json(
        report_dir / "research_run_manifest.json"
    )
    data_rebuild_progress = _data_rebuild_progress_snapshot(report_dir)
    data_freshness = _data_freshness_snapshot(report_dir, now=now)
    gate_levels = _build_gate_levels(
        v23_gate=v23_gate,
        paper=paper,
        data_freshness=data_freshness,
        run_manifest=run_manifest,
    )
    real_money_allowed = bool(gate_levels["real_money"]["passed"])
    manual_observation_available = bool(
        v23_historical_passed and v21_manual_unlocked
    )
    experiment_queue = _load_json(
        report_dir / "research_experiment_queue.json"
    )
    queue_tasks = list((experiment_queue.get("tasks") or {}).values())
    session_started_at = (
        _parse_time(worker.get("event_driven_started_at"))
        or worker_started_at
    )
    all_events = [
        item for item in _raw_events(worker_events_path, limit=5000)
        if (
            session_started_at is None
            or (
                _parse_time(item.get("time")) is not None
                and _parse_time(item.get("time")) >= session_started_at
            )
        )
    ]
    actual_commands = [
        item for item in all_events if item.get("kind") == "command_done"
    ]
    deduplicated = [
        item for item in all_events
        if item.get("kind") == "experiment_skipped_unchanged"
    ]
    actual_compute_seconds = round(sum(
        float(item.get("duration_seconds") or 0) for item in actual_commands
    ), 2)
    primary_source = external_source or {}
    fallback_source = v12_status or {}
    completed_open_source = open_source_factors.get("candidates") or {}
    v18_registry_status = _load_json(
        report_dir / "v18_open_source_factor_status.json"
    )
    v27_registry_status = _load_json(
        report_dir / "v27_open_source_factor_status.json"
    )
    v28_registry_status = _load_json(
        report_dir / "v28_open_source_factor_status.json"
    )
    v29_registry_status = _load_json(
        report_dir / "v29_open_source_factor_status.json"
    )
    v30_registry_status = _load_json(
        report_dir / "v30_open_source_factor_status.json"
    )
    active_open_source_id = (
        v18_registry_status.get("candidate_id")
        if v18_registry_status.get("status") == "running"
        else None
    )
    next_open_source = next((
        item for item in (open_source_factors.get("registry") or {}).get("candidates") or []
        if (
            item.get("factor_id") not in completed_open_source
            and item.get("factor_id") != active_open_source_id
        )
    ), None)
    open_source_durations = [
        float(item.get("compute_seconds") or 0)
        for item in sorted(
            completed_open_source.values(),
            key=lambda item: str(item.get("computed_at") or ""),
        )[-3:]
        if float(item.get("compute_seconds") or 0) > 0
    ]
    seconds_per_candidate = (
        int(round(median(open_source_durations)))
        if open_source_durations else None
    )
    open_source_total = int(
        open_source_factors.get("total_candidate_count") or 5
    )
    open_source_completed = int(
        open_source_factors.get("completed_candidate_count") or 0
    )
    v13_progress = v18_registry_status
    v13_running = v13_progress.get("status") == "running"
    remaining_candidates = max(0, open_source_total - open_source_completed)
    screening_eta_seconds = None
    if seconds_per_candidate is not None:
        current_remaining = seconds_per_candidate
        future_candidates = remaining_candidates
        if v13_running and remaining_candidates:
            current_remaining = max(
                0,
                seconds_per_candidate
                - int(v13_progress.get("elapsed_seconds") or 0),
            )
            future_candidates = max(0, remaining_candidates - 1)
        screening_eta_seconds = (
            current_remaining + future_candidates * seconds_per_candidate
            if remaining_candidates else 0
        )
    v13_passed_candidates = [
        factor_id
        for factor_id, item in completed_open_source.items()
        if item.get("status") == "passed_statistical_gate"
    ]
    v13_screen_complete = bool(
        open_source_total and open_source_completed >= open_source_total
    )
    v13_available = bool(open_source_factors)
    legacy_factor_candidate = (
        bool(v6_factor.get("statistical_gate_passed"))
        if v6_factor
        else (
            bool(v5_factor.get("statistical_gate_passed"))
            if v5_factor
            else int(v4_2.get("statistical_gate_pass_count") or 0) > 0
        )
    )
    factor_candidate = bool(
        v23_historical_passed
        or v13_passed_candidates
        or legacy_factor_candidate
    )
    newest_strategy = next((
        report for report in (
            v13_strategy,
            v6_strategy,
            v5_strategy,
            v4_4_strategy,
            v4_3,
        ) if report
    ), {})
    product_historical_gate = _product_historical_gate(paper)
    # A legacy historical report is a research artefact, not a product
    # qualification.  The only path into a formal product forward clock is
    # the hash-pinned strict certificate produced by paper_forward_prepare.
    strategy_passed = bool(product_historical_gate["passed"])
    strategy_finished = bool(
        v13_strategy
        and "historical_strategy_gate_passed" in v13_strategy
    ) or bool(v20_profit_first or v21_manual_pilot or v23_market_regime)
    strategy_running = bool(
        not strategy_finished
        and (
            (v13_screen_complete and v13_passed_candidates)
            or (not v13_available and legacy_factor_candidate)
        )
    )
    paper_passed = bool(
        paper.get("gate_passed")
        or paper.get("paper_gate_passed")
    )
    paper_clock_started = bool(paper.get("product_forward_clock_valid"))
    paper_preparing = bool(
        paper.get("infrastructure_ready")
        or paper.get("shadow_infrastructure_ready")
        or paper.get("status") in {
            "preparing", "ready", "shadow_ready", "shadow_observation"
        }
    )
    roadmap = [
        {
            "id": "data",
            "label": "真实数据与质量审计",
            "status": "completed",
            "detail": "4,802只历史股票数据已齐并通过质量审计",
            "estimate": "已完成",
        },
        {
            "id": "factor",
            "label": "候选因子统计验证",
            "status": (
                "completed" if v21_manual_pilot or v20_profit_first
                else "completed" if v13_passed_candidates
                else "completed" if not v13_available and legacy_factor_candidate
                else "failed" if v13_screen_complete
                else "active"
            ),
            "detail": (
                f"V13失败批次已封存；V18已完成 {open_source_completed}/{open_source_total} 项，"
                f"{len(v13_passed_candidates)} 项通过预注册统计门禁；"
                + (
                    f"V19龙虎榜完成 {v19_lhb_passed_gates}/{v19_lhb_total_gates} 项，"
                    f"测试胜率 {float(v19_lhb_test.get('top_tercile_win_rate_pct') or 0):.3f}% ，已封存；"
                    if v19_lhb_total_gates else "V19龙虎榜尚未运行"
                )
                + (
                    "V21已完成公告日股东增持事件独立样本裁决"
                    if v21_manual_pilot
                    else "V20已按新规则取消胜率60%条件并完成2026独立样本裁决"
                    if v20_profit_first else ""
                )
                if v13_available
                else (
                    "旧候选存在统计通过项，仍须严格成本策略门禁"
                    if legacy_factor_candidate
                    else "V18开源候选尚未开始"
                )
            ),
            "estimate": (
                "V21机制已完成独立裁决"
                if v21_manual_pilot
                else "V20机制已完成独立裁决"
                if v20_profit_first
                else "已有统计候选"
                if v13_passed_candidates or (
                    not v13_available and legacy_factor_candidate
                )
                else "全部失败并封存"
                if v13_available and v13_screen_complete
                else (
                    f"预计还需{screening_eta_seconds // 3600}小时"
                    f"{(screening_eta_seconds % 3600) // 60}分"
                    if screening_eta_seconds is not None
                    else "等待首项实际耗时"
                )
            ),
        },
        {
            "id": "strategy",
            "label": "严格产品历史门禁",
            "status": (
                "completed" if strategy_passed
                else "historical_only" if v23_historical_passed or v21_manual_unlocked
                else "failed" if strategy_finished
                else "active" if strategy_running
                else "locked"
            ),
            "detail": (
                (
                    "严格产品历史门禁证书已通过："
                    "同一冻结策略已满足双区间、成本、独立复现与哈希校验要求。"
                )
                if strategy_passed
                else (
                    "V23/V24历史完整性证据已核对，但不构成产品资格："
                    f"10BP收益 {float(v23_combined_ten_bp_metrics.get('total_return_pct') or 0):.3f}%，"
                    f"{int(v23_combined_ten_bp_metrics.get('trades') or 0)}笔，"
                    f"胜率 {float(v23_combined_ten_bp_metrics.get('win_rate_pct') or 0):.3f}%，"
                    f"年化 {float(v23_combined_ten_bp_metrics.get('annualized_return_pct') or 0):.3f}%，"
                    f"盈利因子 {float(v23_combined_ten_bp_metrics.get('profit_factor') or 0):.3f}，"
                    f"回撤 {float(v23_combined_ten_bp_metrics.get('max_drawdown_pct') or 0):.3f}%；"
                    "尚无严格产品证书，旧前向时钟不计入产品前向。"
                    if v23_historical_passed
                    else (
                    (
                    "V22当前2026风险受限证据已通过："
                    if v22_current_manual_unlocked
                    else "V21人工试运营证据已通过："
                    )
                    +
                    f"10BP总收益 {float(v21_ten_bp_metrics.get('total_return_pct') or 0):.3f}%，"
                    f"{int(v21_ten_bp_metrics.get('trades') or 0)}笔，"
                    f"胜率 {float(v21_ten_bp_metrics.get('win_rate_pct') or 0):.3f}%，"
                    f"盈利因子 {float(v21_ten_bp_metrics.get('profit_factor') or 0):.3f}；"
                    "只保留历史研究观察，不开放真钱执行"
                    if v21_manual_unlocked else (
                    (
                        (
                            "V20盈利优先门禁失败："
                            f"10BP净收益 {float(v20_ten_bp_metrics.get('total_return_pct') or 0):.3f}%，"
                            f"{int(v20_ten_bp_metrics.get('trades') or 0)}笔，"
                            f"胜率 {float(v20_ten_bp_metrics.get('win_rate_pct') or 0):.3f}%；"
                            "本轮未要求60%胜率，失败原因是成本后严重亏损，不进入推荐"
                            if v20_profit_first
                            else (
                                "尚无合格策略：V18完整成本回测失败；"
                                f"V19统计门禁 {v19_lhb_passed_gates}/{v19_lhb_total_gates}，"
                                f"测试胜率 {float(v19_lhb_test.get('top_tercile_win_rate_pct') or 0):.3f}% "
                                "未达到60%；两者均已封存，不进入买入推荐"
                            )
                        )
                    )
                    if strategy_finished
                    else (
                        "V18候选筛选完成且有统计通过项后，冻结并运行完整成本回测"
                        if not strategy_running
                        else "正在检验胜率、净收益、盈利因子、回撤和10/20BP压力"
                    )
                    ))
                )
            ),
            "estimate": (
                "已完成"
                if strategy_passed
                else "旧历史复现已核验；等待严格产品证书" if v23_historical_passed
                else "V22历史观察证据已记录" if v22_current_manual_unlocked
                else "V21历史观察证据已记录" if v21_manual_unlocked
                else "尚无合格策略" if strategy_finished
                else "正在执行" if strategy_running
                else "等待V18统计候选"
            ),
        },
        {
            "id": "paper",
            "label": "6–8周前向模拟盘",
            "status": "completed" if paper_passed else (
                "active" if strategy_passed else (
                    "preparing" if paper_preparing else "locked"
                )
            ),
            "detail": (
                "模拟盘门禁已通过"
                if paper_passed
                else (
                    (
                        f"严格产品前向计时已于 {paper.get('clock_started_at')} 启动；"
                        f"已完成 {int(paper.get('completed_trades') or 0)}/30 笔，"
                        f"最早复核 {((paper.get('targets') or {}).get('earliest_review_at') or '--')}"
                    )
                    if paper_clock_started
                    else
                    "旧前向时钟已撤销为影子历史；当前0/30不计入产品前向，"
                    "等待严格产品历史门禁证书。"
                    if paper.get("status") == "legacy_forward_clock_revoked_shadow_only"
                    else "影子记录与账本并行准备；正式计时尚未开始"
                    if paper_preparing and not strategy_passed
                    else "必须经过真实时间且至少30笔完成交易"
                )
            ),
            "estimate": (
                "已完成"
                if paper_passed
                else "正式计时中" if paper_clock_started
                else "仅影子观察；等待严格产品证书"
                if paper.get("status") == "legacy_forward_clock_revoked_shadow_only"
                else "并行准备中" if paper_preparing and not strategy_passed
                else "必须6–8周"
            ),
        },
        {
            "id": "capital",
            "label": "¥10,000人工真钱试运营",
            "status": "ready" if real_money_allowed else "locked",
            "detail": (
                "四级门禁全部通过后，仍只允许人工确认"
                if real_money_allowed
                else "历史候选仅供观察；前向、模拟盘、数据新鲜度与独立复现未全部通过"
            ),
            "estimate": (
                "可以决定" if real_money_allowed else "未授权"
            ),
        },
    ]
    if real_money_allowed:
        unlock_estimate = {
            "label": "真钱人工确认门禁已通过",
            "earliest_days": 0,
            "latest_days": 0,
            "confidence": "decision_ready",
            "basis": "历史、前向、模拟盘、数据新鲜度及独立环境复现均已通过",
        }
    elif v23_historical_passed:
        unlock_estimate = {
            "label": "V23历史完整性证据已核验 · 产品门禁锁定",
            "earliest_days": None,
            "latest_days": None,
            "confidence": "strict_product_certificate_required",
            "basis": (
                "V23在2024—2025、2026及合并段的0/10/20BP完整成本后均为正，"
                "但该旧研究门槛不足以进入产品；"
                f"合并{int(v23_combined_ten_bp_metrics.get('trades') or 0)}笔，"
                f"胜率{float(v23_combined_ten_bp_metrics.get('win_rate_pct') or 0):.3f}%，"
                f"年化{float(v23_combined_ten_bp_metrics.get('annualized_return_pct') or 0):.3f}%，"
                f"10BP收益{float(v23_combined_ten_bp_metrics.get('total_return_pct') or 0):.3f}%，"
                f"盈利因子{float(v23_combined_ten_bp_metrics.get('profit_factor') or 0):.3f}，"
                f"回撤{float(v23_combined_ten_bp_metrics.get('max_drawdown_pct') or 0):.3f}%；"
                "严格产品标准要求同一冻结策略验证段与观察段各≥100笔、"
                "含成本胜率≥60%、年化≥15%并独立复现；"
                f"当前严格前向完成{int(paper.get('completed_trades') or 0)}/30笔。"
            ),
        }
    elif v21_manual_unlocked:
        unlock_estimate = {
            "label": (
                "V22历史观察证据已记录"
                if v22_current_manual_unlocked
                else "V21历史观察证据已记录"
            ),
            "earliest_days": None,
            "latest_days": None,
            "confidence": "historical_observation_only",
            "basis": (
                (
                    "2026风险受限段在0/10/20BP三档完整成本后均为正，"
                    f"10BP收益{float(v21_ten_bp_metrics.get('total_return_pct') or 0):.3f}%，"
                    f"回撤{float(v21_ten_bp_metrics.get('max_drawdown_pct') or 0):.3f}%；"
                    f"合并57笔10BP收益{float(v22_combined_ten_bp_metrics.get('total_return_pct') or 0):.3f}%，"
                    "但2024—2025最坏20BP仍失败，跨年份研究质量和自动交易仍锁定"
                    if v22_current_manual_unlocked
                    else (
                        "2026独立样本在0/10/20BP三档完整成本后均为正；"
                        f"但仅{int(v21_ten_bp_metrics.get('trades') or 0)}笔且"
                        f"回撤{float(v21_ten_bp_metrics.get('max_drawdown_pct') or 0):.3f}%"
                        "超过15%，研究质量与自动交易仍锁定"
                    )
                )
            ),
        }
    elif strategy_passed:
        unlock_estimate = {
            "label": "条件下限 42–56天",
            "earliest_days": 42,
            "latest_days": 56,
            "confidence": "conditional_low",
            "basis": "假设6–8周前向模拟一次通过；失败会延长",
        }
    elif v20_profit_first:
        unlock_estimate = {
            "label": "下一次独立裁决约2–6小时",
            "earliest_days": None,
            "latest_days": None,
            "confidence": "next_decision_only",
            "basis": (
                "V20已失败；2–6小时只是全新机制的下一次真实裁决时间，"
                "不是解锁或盈利承诺"
            ),
        }
    else:
        unlock_estimate = {
            "label": "条件下限 49–70天",
            "earliest_days": 49,
            "latest_days": 70,
            "confidence": "conditional_low",
            "basis": "假设下一套统计候选和成本策略一次通过，再完成6–8周前向模拟",
        }

    gate_weights = {
        "data": 20,
        "factor": 25,
        "strategy": 20,
        "paper": 25,
        "capital": 10,
    }
    for item in roadmap:
        item["weight_pct"] = gate_weights[item["id"]]
        item["evidence_complete"] = item["status"] in {
            "completed", "ready"
        }
    evidence_completed_pct = sum(
        item["weight_pct"]
        for item in roadmap
        if item["evidence_complete"]
    )
    historical_gate_items = [
        item for item in roadmap if item["id"] in {"data", "factor", "strategy"}
    ]
    historical_gate_weight = sum(
        item["weight_pct"] for item in historical_gate_items
    )
    historical_gate_completed_weight = sum(
        item["weight_pct"]
        for item in historical_gate_items
        if item["evidence_complete"]
    )
    historical_gate_progress_pct = round(
        historical_gate_completed_weight / historical_gate_weight * 100,
        3,
    ) if historical_gate_weight else 0.0
    paper_started_at = (
        _parse_time(paper.get("clock_started_at"))
        if paper_clock_started else None
    )
    paper_elapsed_days = (
        max(0.0, (now - paper_started_at).total_seconds() / 86_400)
        if paper_started_at
        else 0.0
    )
    paper_targets = paper.get("targets") or {}
    paper_min_days = max(
        1, int(paper_targets.get("weeks_min") or 6) * 7
    )
    paper_trade_target = max(
        1, int(paper_targets.get("completed_trades_min") or 30)
    )
    paper_completed_trades = (
        int(paper.get("completed_trades") or 0)
        if paper_clock_started else 0
    )
    paper_calendar_progress_pct = round(min(
        100.0, paper_elapsed_days / paper_min_days * 100
    ), 3)
    paper_trade_progress_pct = round(min(
        100.0, paper_completed_trades / paper_trade_target * 100
    ), 3)
    paper_strict_progress_pct = round(min(
        paper_calendar_progress_pct, paper_trade_progress_pct
    ), 3)
    current_gate = next(
        (item for item in roadmap if not item["evidence_complete"]),
        roadmap[-1],
    )
    unlock_blockers = []
    if not factor_candidate:
        unlock_blockers.append(
            "没有任何候选通过预注册统计门禁"
        )
    if not strategy_passed:
        unlock_blockers.append(
            "严格研究质量门禁尚未通过"
            if v21_manual_unlocked
            else "尚无扣除真实成本后通过的¥10,000策略"
        )
    if (
        not strategy_passed
        and v19_lhb
        and not v19_lhb.get("statistical_gate_passed")
    ):
        unlock_blockers.append(
            f"V19龙虎榜统计门禁 {v19_lhb_passed_gates}/{v19_lhb_total_gates}；"
            f"测试期最高三分位胜率 "
            f"{float(v19_lhb_test.get('top_tercile_win_rate_pct') or 0):.3f}% "
            "未达到60%，已封存"
        )
    if not strategy_passed and v20_profit_first and not v20_passed:
        unlock_blockers.append(
            "V20 2026独立样本盈利优先验证失败："
            f"10BP净收益 {float(v20_ten_bp_metrics.get('total_return_pct') or 0):.3f}%，"
            "已封存，不得调参补救"
        )
    if not paper_passed:
        unlock_blockers.append(
            "6–8周前向模拟尚未完成"
        )
    if (
        not paid_api_disabled
        and not strategy_passed
        and not deepseek_assessment.get("actionable_candidate")
    ):
        assessment_reasons = deepseek_assessment.get("reasons") or []
        unlock_blockers.append(
            "DeepSeek没有可执行候选"
            + (f"：{'；'.join(assessment_reasons)}" if assessment_reasons else "")
        )

    raw_deepseek_active_tasks = deepseek.get("active_tasks") or []
    deepseek_active_tasks = (
        raw_deepseek_active_tasks
        if isinstance(raw_deepseek_active_tasks, list)
        else []
    )
    deepseek_active_task_details = [
        item for item in deepseek_active_tasks if isinstance(item, dict)
    ]
    deepseek_tasks = (
        deepseek.get("tasks")
        or deepseek_discovery_round.get("tasks")
        or []
    )
    deepseek_last_run_at = (
        deepseek.get("last_run_at")
        or deepseek_discovery_round.get("generated_at")
    )
    deepseek_last_usage = (
        deepseek.get("last_usage")
        or deepseek_discovery_round.get("aggregate_usage")
        or {}
    )
    deepseek_active = bool(
        not paid_api_disabled
        and (worker_state == "ai" or deepseek_active_tasks)
    )
    deepseek_configured = bool(
        not paid_api_disabled and deepseek.get("configured_for_worker")
    )
    deepseek_started = next((
        _parse_time(item.get("started_at"))
        for item in deepseek_active_task_details
        if _parse_time(item.get("started_at")) is not None
    ), step_started_at if deepseek_active else None)
    deepseek_elapsed_seconds = (
        max(0, int((now - deepseek_started).total_seconds()))
        if deepseek_started else None
    )
    deepseek_completed_tasks = sum(
        item.get("status") == "success" for item in deepseek_tasks
    )
    deepseek_failed_tasks = sum(
        item.get("status") == "failed" for item in deepseek_tasks
    )
    if paid_api_disabled:
        deepseek_work_label = "外部付费 API 已禁用；本地确定性研究继续"
        deepseek_work_state = "disabled"
    elif deepseek_active:
        deepseek_work_label = (
            f"正在并行执行{len(deepseek_active_tasks)}项只读研究"
            if deepseek_active_tasks else "正在汇总独立审查"
        )
        deepseek_work_state = "running"
    elif not deepseek_configured:
        deepseek_work_label = "未连接，当前没有DeepSeek计算"
        deepseek_work_state = "disconnected"
    elif deepseek.get("last_error"):
        deepseek_work_label = f"最近失败：{deepseek.get('last_error')}"
        deepseek_work_state = "error"
    else:
        deepseek_work_label = "在线待命，等待新的有效研究输入"
        deepseek_work_state = "waiting"
    deepseek_data_ready = bool(
        not paid_api_disabled
        and deepseek_assessment.get("candidate_consensus")
        and deepseek_assessment.get("point_in_time_data_ready")
        and deepseek_assessment.get("actionable_candidate")
    )

    artifacts = [
        item for item in [
            _latest_artifact(
                report_dir / "v27_weighted_ensemble_validation.json",
                "V27训练期IC冻结加权组合",
            ),
            _latest_artifact(
                report_dir / "v27_open_source_factor_research.json",
                "V27 Qlib新机制候选预注册检验",
            ),
            _latest_artifact(
                report_dir / "v25_structural_factor_research.json",
                "V25结构性因子三路预注册研究",
            ),
            _latest_artifact(
                report_dir / "v23_v21_market_regime_replication.json",
                "V23增持事件MA200严格历史门禁裁决",
            ),
            _latest_artifact(
                report_dir / "v22_v21_risk_policy_replication.json",
                "V22股东增持20%仓位跨年份复验",
            ),
            _latest_artifact(
                report_dir / "v21_shareholder_increase_pilot.json",
                "V21股东增持事件人工试运营裁决",
            ),
            _latest_artifact(
                report_dir / "v20_profit_first_pilot.json",
                "V20 2026独立样本盈利优先裁决",
            ),
            _latest_artifact(
                report_dir / "v19_lhb_factor_research.json",
                "V19龙虎榜机构净买入预注册检验",
            ),
            _latest_artifact(
                report_dir / "v19_lhb_source_audit.json",
                "V19龙虎榜点时数据审计",
            ),
            _latest_artifact(
                report_dir / "v18_open_source_factor_research.json",
                "V18开源候选全市场验证",
            ),
            _latest_artifact(
                report_dir / "v13_open_source_factor_research.json",
                "V13 Qlib开源候选失败档案",
            ),
            _latest_artifact(
                report_dir / "v6_non_limit_max_effect_research.json",
                "V6非涨停型MAX效应预注册研究",
            ),
            _latest_artifact(
                report_dir / "v5_limit_up_factor_research.json",
                "V5首次涨停效应预注册研究",
            ),
            _latest_artifact(
                report_dir / "v4_3_historical_failure_archive.json",
                "V4.3历史失败档案",
            ),
            _latest_artifact(
                report_dir
                / "V4_DEEPSEEK_CODE_AUDIT_ADJUDICATION_2026_07_20.md",
                "DeepSeek代码审计独立裁决",
            ),
            _latest_artifact(
                report_dir / "DEEPSEEK_CONTINUOUS_RESEARCH_LATEST.md",
                "DeepSeek最新红队报告",
            ),
            _latest_artifact(
                report_dir / "DEEPSEEK_DISCOVERY_ASSESSMENT_LATEST.json",
                "DeepSeek四路研究确定性裁决",
            ),
            _latest_artifact(
                report_dir / "DEEPSEEK_DISCOVERY_ROUND_LATEST.json",
                "DeepSeek四路研究执行清单",
            ),
            _latest_artifact(
                report_dir / "DEEPSEEK_MULTI_REVIEW_MANIFEST.json",
                "DeepSeek多路审查用量清单",
            ),
            _latest_artifact(
                report_dir / "v4_4_low_cost_strategy.json",
                "V4.4低换手含成本回测",
            ),
            _latest_artifact(
                report_dir / "v4_4_factor_research.json",
                "V4.4三项预注册因子实验",
            ),
            _latest_artifact(
                report_dir / "V4_DEEPSEEK_AUDIT_ADJUDICATION.md",
                "DeepSeek审计复核裁决",
            ),
            _latest_artifact(
                report_dir / "research_experiment_queue.json",
                "研究队列与指纹去重状态",
            ),
            _latest_artifact(
                report_dir / "GITHUB_TARGETED_COMPONENT_REVIEW_V4_4.md",
                "GitHub定向组件复核",
            ),
            _latest_artifact(
                report_dir / "v4_3_strategy_validation.json",
                "V4.3含成本策略回测",
            ),
            _latest_artifact(
                report_dir / "paper_forward_status.json",
                "模拟盘影子观察状态",
            ),
            _latest_artifact(
                report_dir / "v4_2_microstructure_research.json",
                "V4.2微观结构回测",
            ),
            _latest_artifact(
                report_dir / "v4_formal_factor_research.json",
                "V4正式因子报告",
            ),
            _latest_artifact(
                report_dir / "v4_daily_incremental_update.json",
                "A股日线增量更新报告",
            ),
            _latest_artifact(
                report_dir / "v4_daily_quality_audit.json",
                "全量数据质量审计",
            ),
        ]
        if item is not None
    ]
    artifacts.sort(
        key=lambda item: item["generated_at"], reverse=True
    )
    worker_message = worker.get("message")
    if waiting:
        worker_message = (
            worker_message
            or "守护进程在线，持续等待新的本地确定性研究输入。"
        )
    v25_campaign = _v25_campaign_snapshot(
        report_dir=report_dir,
        worker={**worker, "step_elapsed_seconds": elapsed_seconds},
    )

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "stable_target_campaign": stable_target_campaign,
        "gate_levels": gate_levels,
        "data_freshness": data_freshness,
        "data_rebuild_progress": data_rebuild_progress,
        "run_manifest": run_manifest,
        "fifteen_hour_session": fifteen_hour_session,
        "v25_campaign": v25_campaign,
        "worker": {
            "live": live,
            "status": worker_status,
            "state": worker_state,
            "state_label": worker_state_label,
            "cycle": cycle,
            "work_sequence": int(worker.get("work_sequence") or 0),
            "heartbeat_at": worker.get("heartbeat_at"),
            "heartbeat_age_seconds": heartbeat_age,
            "current_step": current_step,
            "message": worker_message,
            "is_computing": worker_state in {"computing", "ai", "working"},
            "is_backtesting": bool(
                current_stage_index is not None
                and LIVE_CYCLE_STAGES[current_stage_index]["kind"]
                == "backtest"
            ),
            "is_deepseek_active": bool(
                not paid_api_disabled and worker_state == "ai"
            ),
            "step_started_at": (
                step_started_at.isoformat(timespec="seconds")
                if step_started_at else None
            ),
            "step_elapsed_seconds": elapsed_seconds,
            "step_expected_seconds": expected_seconds,
            "step_remaining_seconds": remaining_seconds,
            "active_detail": incremental_detail,
            "cycle_progress_pct": cycle_progress_pct,
            "next_cycle_at": (
                next_cycle.isoformat(timespec="seconds")
                if next_cycle else None
            ),
            "next_check_at": (
                next_check.isoformat(timespec="seconds")
                if next_check else None
            ),
            "seconds_until_next_cycle": seconds_until_next_cycle,
            "last_error": worker.get("last_error"),
            "external_source": external_source,
        },
        "deepseek": {
            "external_paid_api_disabled": paid_api_disabled,
            "configured_for_worker": deepseek_configured,
            "active": deepseek_active,
            "state": (
                "disabled" if paid_api_disabled
                else "active" if worker_state == "ai"
                else "error" if deepseek.get("last_error")
                else "ready" if deepseek.get("configured_for_worker")
                else "not_configured"
            ),
            "last_run_at": deepseek_last_run_at,
            "last_report_path": deepseek.get("last_report_path"),
            "last_error": None if paid_api_disabled else deepseek.get("last_error"),
            "last_usage": deepseek_last_usage,
            "active_task": None if paid_api_disabled else deepseek.get("active_task"),
            "active_tasks": [] if paid_api_disabled else deepseek.get("active_tasks") or [],
            "tasks": [] if paid_api_disabled else deepseek_tasks,
            "completed_tasks": deepseek_completed_tasks,
            "failed_tasks": deepseek_failed_tasks,
            "review_mode": deepseek.get("review_mode"),
            "parallelism": int(deepseek.get("parallelism") or 0),
            "assessment": deepseek_assessment,
            "work": {
                "state": deepseek_work_state,
                "label": deepseek_work_label,
                "working_now": deepseek_active,
                "started_at": (
                    deepseek_started.isoformat(timespec="seconds")
                    if deepseek_started else None
                ),
                "elapsed_seconds": deepseek_elapsed_seconds,
                "active_task_count": len(deepseek_active_tasks),
                "completed_task_count": deepseek_completed_tasks,
                "failed_task_count": deepseek_failed_tasks,
                "latest_output": (
                    "外部付费 API 已禁用；当前仅运行本地确定性研究"
                    if paid_api_disabled
                    else "候选可以进入本地预注册复核"
                    if deepseek_assessment.get("actionable_candidate")
                    else "审查完成，但没有可执行候选"
                    if deepseek_assessment.get("review_completed")
                    else "尚无完整审查结论"
                ),
            },
            "data_support": {
                "can_support_next_factor_test": deepseek_data_ready,
                "can_unlock_real_money_gate": False,
                "candidate_consensus": bool(
                    deepseek_assessment.get("candidate_consensus")
                ),
                "point_in_time_data_ready": bool(
                    deepseek_assessment.get("point_in_time_data_ready")
                ),
                "label": (
                    "外部付费模型不参与当前回测或门禁"
                    if paid_api_disabled
                    else "当前数据可以进入本地预注册复核，但不能直接解锁"
                    if deepseek_data_ready
                    else "当前数据不能支持DeepSeek候选进入回测"
                ),
                "reason": (
                    "按系统策略永久禁用外部付费 API"
                    if paid_api_disabled
                    else "；".join(deepseek_assessment.get("reasons") or [])
                    or "等待完整候选裁决"
                ),
            },
            "role": (
                "历史只读档案；当前不连接、不调用外部付费模型"
                if paid_api_disabled
                else "四路连续只读研究：新机制侦察、点时数据可行性、"
                "预注册证伪、开源来源与防泄漏审查；"
                "不修改代码、不判定交易解锁"
                if deepseek.get("review_mode")
                == "continuous_read_only_discovery_not_training"
                else "六路并行只读红队：代码泄漏、策略、V4.3门禁、"
                "因子假设、成本执行、因子稳健性；"
                "不修改代码、不判定交易解锁"
            ),
        },
        "cycle_stages": stage_rows,
        "experiment_queue": {
            "updated_at": experiment_queue.get("updated_at"),
            "tasks": queue_tasks,
        },
        "research_truth": {
            "software_tests": software_tests,
            "current_work": {
                "step": current_step,
                "reason": worker_message or "等待研究状态",
                "completion_condition": (
                    "本候选完成全市场统计门禁，或数据源故障被明确裁决"
                    if current_step not in {"等待新输入", "轮次间隔"}
                    else "新的数据、候选、代码或凭据状态发生变化"
                ),
            },
            "data_health": {
                "primary": {
                    "name": primary_source.get("source") or "BaoStock",
                    "state": primary_source.get("source_state") or "unknown",
                    "last_heartbeat": primary_source.get("heartbeat_at"),
                    "error": primary_source.get("source_error"),
                },
                "fallback": {
                    "name": (
                        fallback_source.get("source")
                        or "Sina Finance public filings / AkShare adapter"
                    ),
                    "state": (
                        "batch_running"
                        if current_step.startswith("通过备用公开源")
                        else "checkpointed_waiting_next_batch"
                        if fallback_source.get("source_state")
                        == "fallback_running"
                        else fallback_source.get("source_state")
                        or "ready_when_primary_blocked"
                    ),
                    "last_heartbeat": fallback_source.get("heartbeat_at"),
                    "detail": (
                        (
                            "本批已落盘并保存断点；当前未占用计算资源，"
                            "仅在新输入触发时继续下一批。"
                            if fallback_source.get("source_state")
                            == "fallback_running"
                            and not current_step.startswith("通过备用公开源")
                            else fallback_source.get("source_detail")
                        )
                        or "保留财报公告日；仅在BaoStock拒绝访问时分批运行"
                    ),
                    "error": fallback_source.get("source_error"),
                },
                "current_market": {
                    "name": current_index_update.get("source")
                    or "AKShare/Sina上证指数日线",
                    "state": current_index_update.get("status") or "not_run",
                    "requested_end_date": current_index_update.get(
                        "requested_end_date"
                    ),
                    "latest_available_date": current_index_update.get(
                        "latest_available_date"
                    ),
                    "rows_added": int(
                        current_index_update.get("rows_added") or 0
                    ),
                    "error": current_index_update.get("error"),
                },
            },
            "ledger": {
                "actual_commands": len(actual_commands),
                "deduplicated_skips": len(deduplicated),
                "actual_compute_seconds": actual_compute_seconds,
                "queue_total": len(queue_tasks),
                "queue_running": sum(item.get("status") == "running" for item in queue_tasks),
            },
            "open_source_registry": {
                "available": bool(open_source_factors),
                "source": (open_source_factors.get("registry") or {}).get("source") or {},
                "total": open_source_total,
                "completed": open_source_completed,
                "candidates": completed_open_source,
                "next_candidate": next_open_source,
                "current_candidate": (
                    v13_progress.get("candidate_id") if v13_running else None
                ),
                "seconds_per_candidate_estimate": seconds_per_candidate,
                "screening_eta_seconds": screening_eta_seconds,
                "estimate_basis": (
                    f"最近{len(open_source_durations)}项实际计算时长中位数"
                    if open_source_durations else "等待首项实际完成"
                ),
                "intraday_volume_ratio": (
                    (open_source_factors.get("registry") or {}).get("intraday_volume_ratio")
                ) or {
                    "label": "盘中实时量比",
                    "formula": "当日累计成交量 / 已开市分钟 / 过去5日平均每分钟成交量",
                    "validation_status": "等待分钟级历史数据",
                },
            },
            "next_generation_registry": {
                "cohort": "V27",
                "available": True,
                "source": (
                    v27_open_source_factors.get("registry") or {}
                ).get("source") or {},
                "total": int(
                    v27_open_source_factors.get("total_candidate_count")
                    or 6
                ),
                "completed": int(
                    v27_open_source_factors.get(
                        "completed_candidate_count"
                    ) or 0
                ),
                "statistical_passes": sum(
                    item.get("status") == "passed_statistical_gate"
                    for item in (
                        v27_open_source_factors.get("candidates") or {}
                    ).values()
                ),
                "current_candidate": (
                    v27_registry_status.get("candidate_id")
                    if v27_registry_status.get("status") == "running"
                    else None
                ),
                "candidates": (
                    v27_open_source_factors.get("candidates") or {}
                ),
                "scope": (
                    v27_open_source_factors.get("registry") or {}
                ).get("scope") or {},
                "rule": (
                    "新机制逐项冻结、失败封存；只有输入或候选变化才运行，"
                    "不会把重复回测计为进展。"
                ),
                "strategy_adjudication": v27_adjudication["strategy"],
                "weighted_ensemble_adjudication": (
                    v27_adjudication["weighted_ensemble"]
                ),
            },
            "emerging_registry": {
                "cohort": "V28",
                "available": True,
                "source": (
                    v28_open_source_factors.get("registry") or {}
                ).get("source") or {},
                "total": int(
                    v28_open_source_factors.get("total_candidate_count")
                    or 4
                ),
                "completed": int(
                    v28_open_source_factors.get(
                        "completed_candidate_count"
                    ) or 0
                ),
                "statistical_passes": sum(
                    item.get("status") == "passed_statistical_gate"
                    for item in (
                        v28_open_source_factors.get("candidates") or {}
                    ).values()
                ),
                "current_candidate": (
                    v28_registry_status.get("candidate_id")
                    if v28_registry_status.get("status") == "running"
                    else None
                ),
                "progress": v28_registry_status,
                "candidates": (
                    v28_open_source_factors.get("candidates") or {}
                ),
                "scope": (
                    v28_open_source_factors.get("registry") or {}
                ).get("scope") or {},
                "multiple_testing": (
                    v28_open_source_factors.get("registry") or {}
                ).get("multiple_testing") or {},
                "rule": (
                    "四项新价格几何机制逐项冻结；4候选×4持有期按"
                    "16项家族做Bonferroni校正，不能按测试段改方向。"
                ),
            },
            "frontier_registry": {
                "cohort": "V29",
                "available": True,
                "source": (
                    v29_open_source_factors.get("registry") or {}
                ).get("source") or {},
                "total": int(
                    v29_open_source_factors.get("total_candidate_count")
                    or 4
                ),
                "completed": int(
                    v29_open_source_factors.get(
                        "completed_candidate_count"
                    ) or 0
                ),
                "statistical_passes": sum(
                    item.get("status") == "passed_statistical_gate"
                    for item in (
                        v29_open_source_factors.get("candidates") or {}
                    ).values()
                ),
                "current_candidate": (
                    v29_registry_status.get("candidate_id")
                    if v29_registry_status.get("status") == "running"
                    else None
                ),
                "progress": v29_registry_status,
                "candidates": (
                    v29_open_source_factors.get("candidates") or {}
                ),
                "scope": (
                    v29_open_source_factors.get("registry") or {}
                ).get("scope") or {},
                "multiple_testing": (
                    v29_open_source_factors.get("registry") or {}
                ).get("multiple_testing") or {},
                "rule": (
                    "四项路径/量能结构机制逐项冻结；4候选×4持有期按"
                    "16项家族做Bonferroni校正，失败封存。"
                ),
                "strategy_adjudication": v29_adjudication["strategy"],
                "weighted_ensemble_adjudication": (
                    v29_adjudication["weighted_ensemble"]
                ),
            },
            "next_frontier_registry": {
                "cohort": "V30",
                "available": True,
                "source": (
                    v30_open_source_factors.get("registry") or {}
                ).get("source") or {},
                "total": int(
                    v30_open_source_factors.get("total_candidate_count")
                    or 4
                ),
                "completed": int(
                    v30_open_source_factors.get(
                        "completed_candidate_count"
                    ) or 0
                ),
                "statistical_passes": sum(
                    item.get("status") == "passed_statistical_gate"
                    for item in (
                        v30_open_source_factors.get("candidates") or {}
                    ).values()
                ),
                "current_candidate": (
                    v30_registry_status.get("candidate_id")
                    if v30_registry_status.get("status") == "running"
                    else None
                ),
                "progress": v30_registry_status,
                "candidates": (
                    v30_open_source_factors.get("candidates") or {}
                ),
                "scope": (
                    v30_open_source_factors.get("registry") or {}
                ).get("scope") or {},
                "multiple_testing": (
                    v30_open_source_factors.get("registry") or {}
                ).get("multiple_testing") or {},
                "rule": (
                    "四项趋势宽度/参与度机制逐项冻结；4候选×4持有期按"
                    "16项家族做Bonferroni校正，失败封存。"
                ),
                "strategy_adjudication": v30_adjudication["strategy"],
                "weighted_ensemble_adjudication": (
                    v30_adjudication["weighted_ensemble"]
                ),
            },
        },
        "roadmap": roadmap,
        "roadmap_completed": sum(
            item["status"] == "completed" for item in roadmap
        ),
        "roadmap_total": len(roadmap),
        "unlock_estimate": unlock_estimate,
        "unlock_progress": {
            "evidence_completed_pct": evidence_completed_pct,
            "evidence_remaining_pct": 100 - evidence_completed_pct,
            "historical_gate": {
                "completed_pct": historical_gate_progress_pct,
                "passed": bool(strategy_passed),
                "completed_steps": sum(
                    item["evidence_complete"] for item in historical_gate_items
                ),
                "total_steps": len(historical_gate_items),
                "label": (
                    "严格产品历史门禁证书已验证"
                    if strategy_passed
                    else "严格产品历史门禁未通过"
                ),
            },
            "forward_validation": {
                "clock_started": paper_clock_started,
                "started_at": paper.get("clock_started_at"),
                "elapsed_days": round(paper_elapsed_days, 3),
                "minimum_days": paper_min_days,
                "calendar_progress_pct": paper_calendar_progress_pct,
                "completed_trades": paper_completed_trades,
                "target_trades": paper_trade_target,
                "trade_progress_pct": paper_trade_progress_pct,
                "strict_gate_progress_pct": paper_strict_progress_pct,
                "open_positions": int(
                    paper.get("open_positions_count") or 0
                ) if paper_clock_started else 0,
                "realized_net_pnl": round(
                    float(paper.get("realized_net_pnl") or 0), 2
                ) if paper_clock_started else 0.0,
                "formal_decisions": int(
                    paper.get("formal_decision_count") or 0
                ) if paper_clock_started else 0,
                "rejected_decisions": int(
                    paper.get("rejected_decision_count") or 0
                ) if paper_clock_started else 0,
                "execution_rejections": int(
                    paper.get("execution_rejection_count") or 0
                ) if paper_clock_started else 0,
                "definition": (
                    "日历时间与完成交易两项都必须达到目标；"
                    "严格进度取两者较小值，不把等待冒充研究成果。"
                ),
            },
            "product_mvp": {
                "status": "in_progress",
                "target_date": "2026-07-30",
                "label": "可部署产品MVP",
                "definition": "产品交付进度与收益证据门禁分开计算。",
            },
            "current_gate_id": current_gate["id"],
            "current_gate_label": current_gate["label"],
            "current_gate_status": current_gate["status"],
            "current_gate_detail": current_gate["detail"],
            "blockers": unlock_blockers,
            "estimate": unlock_estimate,
            "definition": (
                "百分比表示已取得的门禁证据权重，不代表未来盈利概率，"
                "也不会因等待时间自动增加。"
            ),
        },
        "v19_lhb": v19_lhb,
        "v20_profit_first": v20_profit_first,
        "v21_manual_pilot": v21_manual_pilot,
        "v22_risk_managed": v22_risk_managed,
        "v23_market_regime": v23_market_regime,
        "v23_current_decision": v23_current_decision,
        "v24_integrity_audit": v24_integrity_audit,
        "v23_registered_gate": v23_gate,
        "manual_pilot": {
            "unlocked": real_money_allowed,
            "real_money_allowed": real_money_allowed,
            "observation_access": manual_observation_available,
            "access_level": (
                "real_money" if real_money_allowed else "observation_only"
            ),
            "research_quality_passed": v21_research_quality_passed,
            "automatic_trading_unlocked": False,
            "strategy_id": (
                "v23_v21_market_regime"
                if v23_historical_passed
                else "v22_v21_risk_managed"
                if v22_current_manual_unlocked
                else "v21_shareholder_increase"
            ),
            "label": (
                "股东实际增持公告后漂移 × 上证MA200 · 20%仓位上限"
                if v23_historical_passed
                else "股东实际增持公告后漂移 · 20%仓位上限"
                if v22_current_manual_unlocked
                else "股东实际增持公告后漂移"
            ),
            "metrics_10bp": v21_ten_bp_metrics,
            "canonical_metrics_10bp": (
                v23_canonical_ten_bp_metrics
                if v23_historical_passed
                else v21_ten_bp_metrics
            ),
            "canonical_metrics_scope": (
                "v23_cross_year_combined"
                if v23_historical_passed
                else "active_current_period"
            ),
            "canonical_metrics_generated_at": (
                v23_market_regime.get("generated_at")
                if v23_historical_passed
                else (
                    v22_risk_managed.get("generated_at")
                    if v22_current_manual_unlocked
                    else v21_manual_pilot.get("generated_at")
                )
            ),
            "candidate": (
                v21_visible_candidate if real_money_allowed else None
            ),
            "observation_candidate": v21_visible_candidate,
            "current_decision": v23_current_decision,
            "integrity_audit": v24_integrity_audit,
            "latest_observed_signal": v21_manual_pilot.get("latest_observed_signal"),
            "execution_rule": manual_execution_rule,
            "limitations": manual_limitations,
            "combined_metrics_10bp": (
                v23_combined_ten_bp_metrics
                if v23_historical_passed
                else v22_combined_ten_bp_metrics
            ),
            "failed_research_gates": (
                v23_gate.get("failed_gates") or []
                if v23_market_regime
                else v22_risk_managed.get("failed_gates") or []
            ),
            "message": (
                "四级门禁已全部通过；仍需逐笔人工确认，自动交易保持关闭。"
                if real_money_allowed
                else (
                    f"历史候选仅供观察；前向已完成"
                    f"{int(paper.get('completed_trades') or 0)}/30笔，"
                    "模拟盘、数据新鲜度与独立复现未全部通过，真钱锁定。"
                )
            ),
            "legacy_evidence_message": (
                (
                    f"{v23_current_decision.get('reason')} "
                    f"独立复核：历史46笔胜率"
                    f"{((v24_integrity_audit.get('win_rate_target') or {}).get('observed_pct') or 0):.3f}%，"
                    "尚未达到稳定60%，也不代表未来不亏。"
                )
                if v23_historical_passed and v23_current_decision
                else
                (
                    f"历史观察候选只在"
                    f"{v21_visible_candidate.get('planned_entry_date')}开盘窗口复核"
                    if v21_visible_candidate
                    else "历史观察证据已记录；当前没有有效观察窗口"
                )
                if v21_manual_unlocked
                else "V21尚未满足盈利优先人工试运营条件"
            ),
        },
        "estimate_warning": (
            "这是条件性区间，不是保证完成日期；策略回测失败会延长。"
        ),
        "artifacts": artifacts,
    }


def _metric_block(report: dict[str, Any]) -> dict[str, Any]:
    metrics = report.get("metrics") or {}
    folds = report.get("folds") or []
    positive_folds = sum(
        1 for item in folds if float(item.get("return_pct") or 0) > 0
    )
    return {
        "total_return_pct": metrics.get("total_return_pct"),
        "annualized_return_pct": metrics.get("annualized_return_pct"),
        "benchmark_return_pct": metrics.get("benchmark_return_pct"),
        "excess_return_pct": metrics.get("excess_return_pct"),
        "max_drawdown_pct": metrics.get("max_drawdown_pct"),
        "win_rate_pct": metrics.get("win_rate_pct"),
        "profit_factor": metrics.get("profit_factor"),
        "sharpe": metrics.get("sharpe"),
        "trades": metrics.get("trades"),
        "transaction_costs": metrics.get("estimated_transaction_costs"),
        "oos_start": metrics.get("oos_start"),
        "oos_end": metrics.get("oos_end"),
        "oos_trading_days": metrics.get("oos_trading_days"),
        "positive_fold_ratio_pct": (
            round(positive_folds / len(folds) * 100, 3) if folds else None
        ),
    }


def _failed_gate_labels(gates: dict[str, Any]) -> list[str]:
    return [
        GATE_LABELS.get(key, key)
        for key, passed in gates.items()
        if not passed
    ]


def _strategy_card(
    report: dict[str, Any],
    *,
    strategy_id: str,
    label: str,
    kind: str,
) -> dict[str, Any]:
    strict_gate = evaluate_historical_strategy_gate(report)
    return {
        "strategy_id": strategy_id,
        "label": label,
        "kind": kind,
        "gate_passed": strict_gate["passed"],
        "strict_gate": strict_gate,
        "metrics": _metric_block(report),
        "failed_gates": strict_gate["failed_gates"],
        "folds": [
            {
                "fold": item.get("fold"),
                "start": item.get("start"),
                "end": item.get("end"),
                "return_pct": item.get("return_pct"),
                "max_drawdown_pct": item.get("max_drawdown_pct"),
            }
            for item in (report.get("folds") or [])
        ],
        "regimes": report.get("regime_trade_stats") or {},
    }


def _candidate_stage(
    item: dict[str, Any],
    backtest: dict[str, Any] | None,
) -> tuple[str, str]:
    if backtest and backtest.get("gate_passed"):
        return "strategy_passed", "策略门禁通过"
    if backtest:
        return "strategy_failed", "含成本策略未通过"
    if item.get("heldout_confirmed"):
        return "heldout_confirmed", "独立测试确认"
    if item.get("pretest_selected"):
        return "heldout_failed", "独立测试未确认"
    return "screened_out", "验证集筛除"


def _candidate_rows(
    factor_report: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    backtests = {
        item.get("factor_id"): item
        for item in (factor_report.get("backtests") or [])
        if item.get("factor_id")
    }
    rows = []
    for item in factor_report.get("candidate_results") or []:
        factor_id = item.get("factor_id")
        backtest = backtests.get(factor_id)
        stage, stage_label = _candidate_stage(item, backtest)
        backtest_metrics = _metric_block(backtest or {})
        rows.append({
            "factor_id": factor_id,
            "family": item.get("family"),
            "family_label": FAMILY_LABELS.get(
                item.get("family"), item.get("family")
            ),
            "formula": item.get("formula"),
            "description": item.get("description"),
            "stage": stage,
            "stage_label": stage_label,
            "pretest_selected": bool(item.get("pretest_selected")),
            "heldout_confirmed": bool(item.get("heldout_confirmed")),
            "train": item.get("train") or {},
            "validation": item.get("validation") or {},
            "validation_spread": item.get("validation_spread") or {},
            "test": item.get("test") or {},
            "test_spread": item.get("test_spread") or {},
            "backtest": {
                "available": bool(backtest),
                "gate_passed": bool(
                    backtest and backtest.get("gate_passed")
                ),
                "metrics": backtest_metrics,
                "failed_gates": _failed_gate_labels(
                    (backtest or {}).get("gate_conditions") or {}
                ),
            },
        })

    family_counts = Counter(item["family"] for item in rows)
    family_confirmed = Counter(
        item["family"] for item in rows if item["heldout_confirmed"]
    )
    families = [
        {
            "family": family,
            "label": FAMILY_LABELS.get(family, family),
            "candidate_count": count,
            "confirmed_count": family_confirmed.get(family, 0),
        }
        for family, count in sorted(family_counts.items())
    ]
    return rows, families


def _formal_factor_rows(
    report: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = []
    families = []
    for factor in report.get("factors") or []:
        passed_horizons = 0
        for horizon, result in (factor.get("horizons") or {}).items():
            split = result.get("splits") or {}
            test = split.get("test") or {}
            validation = split.get("validation") or {}
            gates = result.get("gate_conditions") or {}
            passed = bool(result.get("statistical_gate_passed"))
            if passed:
                passed_horizons += 1
            rows.append({
                "factor_id": factor.get("factor_id"),
                "family": factor.get("family"),
                "family_label": FAMILY_LABELS.get(
                    factor.get("family"), factor.get("family")
                ),
                "label": factor.get("label"),
                "formula": factor.get("formula"),
                "role": factor.get("role"),
                "holding_days": int(
                    result.get("holding_days") or horizon
                ),
                "validation_mean_ic": validation.get("mean_ic"),
                "test_mean_ic": test.get("mean_ic"),
                "test_ci": test.get("block_bootstrap_95_ci") or [],
                "test_positive_ic_days_pct": test.get(
                    "positive_ic_days_pct"
                ),
                "test_monotonicity": test.get(
                    "decile_monotonicity_spearman"
                ),
                "test_top_minus_middle_after_cost_pct": test.get(
                    "top_minus_middle_after_cost_pct"
                ),
                "positive_test_folds": result.get(
                    "positive_test_folds"
                ),
                "statistical_gate_passed": passed,
                "production_gate_passed": bool(
                    result.get("production_gate_passed")
                ),
                "failed_gates": [
                    FORMAL_GATE_LABELS.get(key, key)
                    for key, gate_passed in gates.items()
                    if not gate_passed
                ],
            })
        families.append({
            "family": factor.get("family"),
            "label": FAMILY_LABELS.get(
                factor.get("family"), factor.get("family")
            ),
            "candidate_count": len(factor.get("horizons") or {}),
            "confirmed_count": passed_horizons,
        })
    return rows, families


def _experiment_titles(path: Path | None) -> list[str]:
    if path is None or not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    deepseek_titles = [
        match.strip()
        for match in re.findall(
            r"^###\s*实验\s*\d+\s*[：:]\s*(.+)$",
            text,
            flags=re.MULTILINE,
        )
    ]
    protocol_titles = [
        match.strip()
        for match in re.findall(
            r"^##\s*V4\.2-H\d+\s*[：:]\s*(.+)$",
            text,
            flags=re.MULTILINE,
        )
    ]
    return (protocol_titles or deepseek_titles)[:3]


def build_research_factory(
    *,
    current_strategy_path: Path,
    baseline_strategy_path: Path,
    factor_report_path: Path,
    report_dir: Path,
    worker_status_path: Path,
    worker_events_path: Path,
) -> dict[str, Any]:
    current = _load_json(current_strategy_path)
    baseline = _load_json(baseline_strategy_path)
    factors = _load_json(factor_report_path)
    worker = _load_json(worker_status_path)
    download = _load_json(report_dir / "v4_daily_download_manifest.json")
    universe_scope = _daily_universe_scope(download)
    quality = _load_json(report_dir / "v4_daily_quality_audit.json")
    pipeline = _load_json(report_dir / "v4_factor_pipeline_dry_run.json")
    formal = _load_json(report_dir / "v4_formal_factor_research.json")
    v4_1 = _load_json(report_dir / "v4_1_exploratory_research.json")
    v4_2 = _load_json(report_dir / "v4_2_microstructure_research.json")
    v4_3 = _load_json(report_dir / "v4_3_strategy_validation.json")
    v4_4_factor = _load_json(report_dir / "v4_4_factor_research.json")
    v4_4_strategy = _load_json(
        report_dir / "v4_4_low_cost_strategy.json"
    )
    v5_factor = _load_json(
        report_dir / "v5_limit_up_factor_research.json"
    )
    v5_strategy = _load_json(
        report_dir / "v5_limit_up_strategy_validation.json"
    )
    v6_factor = _load_json(
        report_dir / "v6_non_limit_max_effect_research.json"
    )
    v6_strategy = _load_json(
        report_dir / "v6_non_limit_max_strategy_validation.json"
    )
    v13_factor = _load_json(
        report_dir / "v18_open_source_factor_research.json"
    ) or {
        "cohort": "V18",
        "registry": v18_registry_payload(),
        "candidates": {},
        "completed_candidate_count": 0,
        "total_candidate_count": len(
            v18_registry_payload().get("candidates") or []
        ),
    }
    v13_strategy = _load_json(
        report_dir / "v18_open_source_strategy_validation.json"
    )
    v27_factor = _load_json(
        report_dir / "v27_open_source_factor_research.json"
    )
    v27_strategy = _load_json(
        report_dir / "v27_open_source_strategy_validation.json"
    )
    v27_ensemble = _load_json(
        report_dir / "v27_weighted_ensemble_validation.json"
    )
    v27_adjudication = _v27_adjudication_summary(
        v27_strategy,
        v27_ensemble,
    )
    v27_status = _load_json(
        report_dir / "v27_open_source_factor_status.json"
    )
    v9_factor = _load_json(
        report_dir / "v9_margin_factor_research.json"
    )
    v14_factor = _load_json(
        report_dir / "v14_margin_balance_acceleration.json"
    )
    v14_strategy = _load_json(
        report_dir / "v14_margin_balance_strategy_validation.json"
    )
    v14_status = _load_json(
        report_dir / "v14_margin_balance_acceleration_status.json"
    )
    v15_source_audit = _load_json(
        report_dir / "v15_margin_eligibility_source_audit.json"
    )
    v15_source_status = _load_json(
        report_dir / "v15_margin_eligibility_source_audit_status.json"
    )
    v15_events = _load_json(
        report_dir / "v15_margin_eligibility_events.json"
    )
    v15_event_status = _load_json(
        report_dir / "v15_margin_eligibility_event_status.json"
    )
    v15_factor = _load_json(
        report_dir / "v15_margin_eligibility_factor_research.json"
    )
    v15_factor_status = _load_json(
        report_dir / "v15_margin_eligibility_factor_status.json"
    )
    v16_events = _load_json(
        report_dir / "v16_share_repurchase_events.json"
    )
    v16_event_status = _load_json(
        report_dir / "v16_share_repurchase_event_status.json"
    )
    v16_factor = _load_json(
        report_dir / "v16_share_repurchase_factor_research.json"
    )
    v16_factor_status = _load_json(
        report_dir / "v16_share_repurchase_factor_status.json"
    )
    v17_factor = _load_json(
        report_dir / "v17_limit_down_liquidity_recovery.json"
    )
    v17_factor_status = _load_json(
        report_dir / "v17_limit_down_liquidity_recovery_status.json"
    )
    v10_factor = _load_json(
        report_dir / "v10_peer_return_gap_research.json"
    )
    v11_factor = _load_json(
        report_dir / "v11_unlock_announcement_pressure_research.json"
    )
    accelerated_tracker = accelerated_simulation_snapshot(
        status_path=(
            report_dir / "accelerated_historical_simulation_status.json"
        )
    )
    fifteen_hour_session = fifteen_hour_session_snapshot(
        status_path=(
            report_dir / "fifteen_hour_validation_session.json"
        ),
        events_path=worker_events_path,
    )
    experiment_queue = _load_json(
        report_dir / "research_experiment_queue.json"
    )
    paper = _load_json(report_dir / "paper_forward_status.json")
    deepseek_path = report_dir / "DEEPSEEK_CONTINUOUS_RESEARCH_LATEST.md"
    v4_2_protocol_path = (
        report_dir.parent
        / "research"
        / "V4_2_RESEARCH_HYPOTHESES.md"
    )
    pipeline_labels = pipeline.get("labels") or {}
    v20_profit_first = _load_json(
        report_dir / "v20_profit_first_pilot.json"
    )
    v21_manual_pilot = _load_json(
        report_dir / "v21_shareholder_increase_pilot.json"
    )
    v22_risk_managed = _load_json(
        report_dir / "v22_v21_risk_policy_replication.json"
    )
    v23_market_regime = _load_json(
        report_dir / "v23_v21_market_regime_replication.json"
    )
    v23_current_decision = _load_json(
        report_dir / "v23_current_manual_decision.json"
    )
    v24_integrity_audit = _load_json(
        report_dir / "v24_v23_integrity_audit.json"
    )
    v25_campaign = _v25_campaign_snapshot(
        report_dir=report_dir,
        worker=worker,
    )
    v23_gate = _registered_v23_gate(
        v23_market_regime,
        v24_integrity_audit,
        report_path=(
            report_dir / "v23_v21_market_regime_replication.json"
        ),
    )
    v23_historical_passed = bool(v23_gate["passed"])
    v21_original_manual_unlocked = bool(
        v21_manual_pilot.get("profit_only_manual_pilot_gate_passed")
    )
    v22_current_manual_unlocked = bool(
        v22_risk_managed.get("current_regime_manual_pilot_gate_passed")
    )
    v21_manual_unlocked = bool(
        v22_current_manual_unlocked or v21_original_manual_unlocked
    )
    run_manifest = _load_json(
        report_dir / "research_run_manifest.json"
    )
    factory_now = datetime.now().astimezone()
    data_freshness = _data_freshness_snapshot(
        report_dir,
        now=factory_now,
    )
    gate_levels = _build_gate_levels(
        v23_gate=v23_gate,
        paper=paper,
        data_freshness=data_freshness,
        run_manifest=run_manifest,
    )
    real_money_allowed = bool(gate_levels["real_money"]["passed"])
    manual_observation_available = bool(
        v23_historical_passed and v21_manual_unlocked
    )
    v21_original_ten_bp_metrics = (
        (((v21_manual_pilot.get("results") or {}).get("round_trip_10bp") or {}).get("metrics"))
        or {}
    )
    v22_current_ten_bp_metrics = (
        v22_risk_managed.get("current_2026_summary_10bp") or {}
    )
    v22_combined_ten_bp_metrics = (
        v22_risk_managed.get("summary_10bp") or {}
    )
    v23_combined_ten_bp_metrics = (
        v23_market_regime.get("summary_10bp") or {}
    )
    v23_current_ten_bp_metrics = (
        v23_market_regime.get("current_2026_summary_10bp") or {}
    )
    v23_canonical_ten_bp_metrics = _canonical_v23_ten_bp_metrics(
        v23_market_regime
    )
    v21_ten_bp_metrics = (
        v23_current_ten_bp_metrics
        if v23_historical_passed
        else v22_current_ten_bp_metrics
        if v22_current_manual_unlocked
        else v21_original_ten_bp_metrics
    )
    manual_execution_rule = dict(
        v21_manual_pilot.get("manual_execution_rule") or {}
    )
    if v22_current_manual_unlocked:
        manual_execution_rule["position_limit"] = (
            "单只最多账户权益20%，同时最多1只，100股整数手"
        )

    factory_manual_candidate = v21_manual_pilot.get("current_candidate")
    if v23_historical_passed:
        if v23_current_decision:
            factory_manual_candidate = (
                dict(v23_current_decision.get("candidate") or {})
                if v23_current_decision.get("actionable_candidate") is True
                else None
            )
        elif factory_manual_candidate:
            latest_v23_signal = v23_market_regime.get(
                "latest_eligible_signal"
            ) or {}
            if not (
                str(latest_v23_signal.get("code") or "")
                == str(factory_manual_candidate.get("code") or "")
                and str(latest_v23_signal.get("trade_date") or "")
                == str(factory_manual_candidate.get("announcement_date") or "")
            ):
                factory_manual_candidate = None

    candidates, families = _candidate_rows(factors)
    formal_rows, formal_families = _formal_factor_rows(formal)
    strategy_cards = []
    if baseline:
        strategy_cards.append(_strategy_card(
            baseline,
            strategy_id="baseline",
            label="原始多因子基线",
            kind="baseline",
        ))
    if current:
        strategy_cards.append(_strategy_card(
            current,
            strategy_id="cost_aware",
            label="成本约束多因子",
            kind="current",
        ))
    for item in factors.get("backtests") or []:
        factor_id = item.get("factor_id") or "unknown_factor"
        strategy_cards.append(_strategy_card(
            item,
            strategy_id=f"factor:{factor_id}",
            label=f"单因子 · {factor_id}",
            kind="factor",
        ))

    progress = worker.get("progress") or {}
    target_symbols = int(
        download.get("target_symbols")
        or progress.get("target_symbols")
        or 0
    )
    downloaded_symbols = int(
        download.get("downloaded_symbols")
        or progress.get("downloaded_symbols")
        or 0
    )
    coverage_pct = float(
        download.get("coverage_pct")
        or progress.get("coverage_pct")
        or 0
    )
    unresolved_events = int(
        quality.get("ordinary_limit_violations")
        or progress.get("unresolved_limit_events")
        or 0
    )
    audit_failed = int(
        quality.get("failed_files")
        or progress.get("audit_failed")
        or 0
    )
    full_universe_ready = bool(
        (pipeline.get("research_gate") or {}).get("full_universe_ready")
    )
    scope_universe_ready = bool(
        full_universe_ready and universe_scope["scope_complete"]
    )
    full_a_share_ready = bool(
        scope_universe_ready and universe_scope["full_a_share_coverage"]
    )
    strategy_evidence = next((
        {"label": label, "report": report}
        for label, report in (
            (
                "V27训练期IC加权组合",
                v27_ensemble if v27_ensemble.get("results") else {},
            ),
            ("V27新机制候选", v27_strategy),
            ("V18开源候选", v13_strategy),
            ("V6", v6_strategy),
            ("V5", v5_strategy),
            ("V4.4", v4_4_strategy),
            ("V4.3", v4_3),
            ("成本约束基线", current),
            ("原始基线", baseline),
        )
        if report
    ), {"label": "尚无冻结策略", "report": {}})
    legacy_historical_gate = (
        v23_gate
        if v23_market_regime
        else evaluate_historical_strategy_gate(strategy_evidence["report"])
    )
    product_historical_gate = gate_levels["historical"]
    strategy_passed = int(product_historical_gate["passed"])
    v13_passed_candidates = [
        factor_id
        for factor_id, item in (v13_factor.get("candidates") or {}).items()
        if item.get("status") == "passed_statistical_gate"
    ]
    v13_completed_count = int(
        v13_factor.get("completed_candidate_count")
        or len(v13_factor.get("candidates") or {})
    )
    v13_total_count = int(v13_factor.get("total_candidate_count") or 5)
    v13_screen_complete = bool(
        v13_total_count and v13_completed_count >= v13_total_count
    )
    v13_strategy_started = bool(
        v13_strategy
        or (v13_screen_complete and v13_passed_candidates)
    )
    v27_completed_count = int(
        v27_factor.get("completed_candidate_count")
        or len(v27_factor.get("candidates") or {})
    )
    v27_total_count = int(v27_factor.get("total_candidate_count") or 6)
    v27_passed_candidates = [
        factor_id
        for factor_id, item in (v27_factor.get("candidates") or {}).items()
        if item.get("status") == "passed_statistical_gate"
    ]
    v27_screen_complete = bool(
        v27_total_count and v27_completed_count >= v27_total_count
    )
    v27_strategy_started = bool(
        v27_strategy
        or (v27_screen_complete and v27_passed_candidates)
    )
    strategy_started = bool(
        v23_market_regime
        or v27_strategy
        or strategy_evidence["report"]
        or v27_passed_candidates
        or v13_passed_candidates
        or v4_2.get("statistical_gate_pass_count")
    )
    paper_passed = bool(
        paper.get("gate_passed")
        or paper.get("paper_gate_passed")
    )
    paper_clock_started = bool(paper.get("product_forward_clock_valid"))
    paper_preparing = bool(
        paper.get("infrastructure_ready")
        or paper.get("shadow_infrastructure_ready")
        or paper.get("status") in {
            "preparing", "ready", "shadow_ready", "shadow_observation"
        }
    )
    heartbeat_age = _heartbeat_age_seconds(worker.get("heartbeat_at"))
    worker_declared_running = worker.get("status") in {
        "starting", "running", "retry_wait", "stopping"
    }
    worker_live = bool(
        worker_declared_running
        and heartbeat_age is not None
        and heartbeat_age <= 300
    )

    data_complete = bool(
        target_symbols
        and downloaded_symbols >= target_symbols
        and audit_failed == 0
        and unresolved_events == 0
    )
    heldout_count = int(factors.get("heldout_confirmed_count") or 0)
    formal_statistical_passes = int(
        formal.get("statistical_gate_pass_count") or 0
    )
    v4_1_tests = int(v4_1.get("exploratory_test_count") or 0)
    v4_1_passes = int(v4_1.get("exploratory_gate_pass_count") or 0)
    v4_2_tests = int(v4_2.get("test_count") or 0)
    v4_2_passes = int(v4_2.get("statistical_gate_pass_count") or 0)
    v4_4_tests = int(v4_4_factor.get("test_count") or 0)
    v4_4_passes = int(
        v4_4_factor.get("statistical_gate_pass_count") or 0
    )
    legacy_factor_candidate = bool(
        v6_factor.get("statistical_gate_passed")
        if v6_factor
        else (
            v5_factor.get("statistical_gate_passed")
            if v5_factor
            else v4_2_passes
        )
    )
    factor_candidate = bool(
        v23_historical_passed
        or v27_passed_candidates
        or v13_passed_candidates
        or legacy_factor_candidate
    )
    latest_strategy_label = (
        "V23增持事件×上证MA200"
        if v23_market_regime
        else strategy_evidence["label"]
    )
    accelerated_status = (
        "passed"
        if v23_historical_passed
        else accelerated_tracker.get("status")
        if accelerated_tracker
        else (
            "passed"
            if strategy_passed
            else "failed"
            if v4_4_strategy
            else "waiting_candidate"
        )
    )
    accelerated_validation = {
        "name": "10–15小时加速历史仿真",
        "status": accelerated_status,
        "latest_strategy": (
            accelerated_tracker.get("candidate_label")
            or latest_strategy_label
        ),
        "can_start_now": bool(factor_candidate and not strategy_passed),
        "compute_window_hours": {"minimum": 10, "maximum": 15},
        "purpose": (
            "用历史时间轴加速回放，淘汰不稳健策略并缩短研究等待；"
            "不等同于未来6–8周真实前向表现。"
        ),
        "decision": (
            (
                f"{accelerated_tracker.get('status_label')}；"
                f"{accelerated_tracker.get('blocker') or '正在执行'}。"
            )
            if accelerated_tracker
            else (
                "历史策略门禁已通过，可进入真实时间前向观察。"
                if strategy_passed
                else (
                    "旧策略已在含成本历史检验中失败；重复增加运行时长不会改变裁决。"
                    if v4_4_strategy
                    else "等待一个预注册且统计通过的新候选策略。"
                )
            )
        ),
        "protocol": accelerated_tracker.get("phases") or [
            {
                "id": "point_in_time",
                "label": "时点化股票池与数据防泄漏",
                "required": True,
                "status": "passed" if data_complete else "blocked",
            },
            {
                "id": "rolling_oos",
                "label": "不少于12个滚动样本外窗口",
                "required": True,
                "status": "waiting" if not strategy_passed else "passed",
            },
            {
                "id": "execution",
                "label": "¥10,000、T+1、整数手、佣金、印花税与涨跌停",
                "required": True,
                "status": "failed" if v4_4_strategy and not strategy_passed else (
                    "passed" if strategy_passed else "waiting"
                ),
            },
            {
                "id": "stress",
                "label": "10/20BP滑点与市场状态压力测试",
                "required": True,
                "status": "failed" if v4_4_strategy and not strategy_passed else (
                    "passed" if strategy_passed else "waiting"
                ),
            },
            {
                "id": "stability",
                "label": "收益、回撤、盈利因子与跨时期稳定性联合裁决",
                "required": True,
                "status": "failed" if v4_4_strategy and not strategy_passed else (
                    "passed" if strategy_passed else "waiting"
                ),
            },
        ],
        "runtime": accelerated_tracker,
        "does_not_replace": "真实时间前向观察与真实成交摩擦验证",
    }
    delivery_plan = [
        {
            "id": "decision_support",
            "label": "今日：个人决策辅助试运行版",
            "target_date": "2026-07-23",
            "status": "completed" if v23_historical_passed else "in_progress",
            "allowed": "实时行情、0–3只候选、K线买卖触发、人工确认、完整日志",
            "boundary": "可运行和记录；不得宣称已证明赚钱，不自动下单",
        },
        {
            "id": "product_mvp",
            "label": "下周四：可部署产品MVP",
            "target_date": "2026-07-30",
            "status": "in_progress",
            "allowed": "完整页面、研究证据、风险提示、部署与演示流程",
            "boundary": "属于研究与决策辅助产品；对外销售前仍需单独完成合规审查",
        },
        {
            "id": "verified_strategy",
            "label": "经验证策略与真钱试运营",
            "target_date": None,
            "status": "conditional",
            "allowed": "仅在历史门禁通过后开始受控前向验证",
            "boundary": "不能用功能交付日期替代收益证据日期",
        },
    ]

    blockers = []
    if (
        v22_current_manual_unlocked
        and not v23_historical_passed
        and not v22_risk_managed.get(
        "research_quality_gate_passed"
        )
    ):
        failed_v22 = list(v22_risk_managed.get("failed_gates") or [])
        blockers.append({
            "severity": "warning",
            "title": "V22仅形成历史观察证据，真钱门禁仍锁定",
            "detail": (
                f"2026段10BP收益 {float(v21_ten_bp_metrics.get('total_return_pct') or 0):.3f}%，"
                f"{int(v21_ten_bp_metrics.get('trades') or 0)}笔，"
                f"最大回撤 {float(v21_ten_bp_metrics.get('max_drawdown_pct') or 0):.3f}%；"
                f"合并57笔10BP收益 {float(v22_combined_ten_bp_metrics.get('total_return_pct') or 0):.3f}%，"
                f"严格门禁仍有 {len(failed_v22)} 项未通过：{'、'.join(failed_v22)}。"
            ),
        })
    elif v21_manual_unlocked and not v21_manual_pilot.get(
        "research_quality_gate_passed"
    ):
        failed_v21 = [
            key for key, passed in (
                v21_manual_pilot.get("gate_conditions") or {}
            ).items() if not passed
        ]
        blockers.append({
            "severity": "warning",
            "title": "V21仅形成历史观察证据，真钱门禁仍锁定",
            "detail": (
                f"10BP总收益 {float(v21_ten_bp_metrics.get('total_return_pct') or 0):.3f}%，"
                f"{int(v21_ten_bp_metrics.get('trades') or 0)}笔，"
                f"胜率 {float(v21_ten_bp_metrics.get('win_rate_pct') or 0):.3f}%，"
                f"最大回撤 {float(v21_ten_bp_metrics.get('max_drawdown_pct') or 0):.3f}%；"
                f"研究质量仍有 {len(failed_v21)} 项未通过，"
                "只允许研究观察，禁止真钱与自动下单。"
            ),
        })
    if v20_profit_first and not v20_profit_first.get(
        "profit_first_pilot_gate_passed"
    ):
        v20_metrics = (
            (((v20_profit_first.get("results") or {}).get("round_trip_10bp") or {}).get("metrics"))
            or {}
        )
        failed_v20 = [
            key for key, passed in (
                v20_profit_first.get("gate_conditions") or {}
            ).items() if not passed
        ]
        blockers.append({
            "severity": "blocking",
            "title": "V20盈利优先独立样本门禁失败",
            "detail": (
                "已按用户新规则取消60%胜率条件；"
                f"2026独立样本10BP收益 "
                f"{float(v20_metrics.get('total_return_pct') or 0):.3f}%，"
                f"{int(v20_metrics.get('trades') or 0)}笔，"
                f"胜率 {float(v20_metrics.get('win_rate_pct') or 0):.3f}%，"
                f"共 {len(failed_v20)} 项条件失败。"
                "这是策略亏损，不是页面Bug；已封存，不进入推荐。"
            ),
        })
    if not v13_screen_complete:
        blockers.append({
            "severity": "blocking",
            "title": "V18开源候选正在真实样本外筛选",
            "detail": (
                f"已完成 {v13_completed_count}/{v13_total_count} 项，"
                f"其中 {len(v13_passed_candidates)} 项通过统计门禁；"
                "未完成候选不会进入买入推荐，旧失败策略也不会被复活。"
            ),
        })
    elif not v13_passed_candidates:
        blockers.append({
            "severity": "blocking",
            "title": "V18开源候选未产生统计合格因子",
            "detail": (
                f"{v13_total_count} 项候选均已完成，但没有候选通过"
                "预注册统计门禁；必须注册机制不同的新候选，不能降低门槛。"
            ),
        })
    elif not v13_strategy:
        blockers.append({
            "severity": "blocking",
            "title": "V18统计候选正在进入完整成本策略回测",
            "detail": (
                f"{len(v13_passed_candidates)} 项候选通过统计门禁；"
                "下一步按冻结规则检验¥10,000、T+1、整数手及10/20BP滑点。"
            ),
        })
    if not v27_screen_complete:
        blockers.append({
            "severity": "research",
            "title": "V27新机制因子批次正在接力",
            "detail": (
                f"已完成 {v27_completed_count}/{v27_total_count} 项，"
                f"其中 {len(v27_passed_candidates)} 项通过统计门禁；"
                "每项先冻结再计算，失败即封存，不重复旧因子。"
            ),
        })
    elif not v27_passed_candidates:
        blockers.append({
            "severity": "research",
            "title": "V27新机制批次未产生统计合格因子",
            "detail": (
                f"{v27_total_count} 项新机制均已裁决；下一批必须登记"
                "新的来源、公式和数据需求，不能在测试集降门槛。"
            ),
        })
    elif not v27_strategy:
        blockers.append({
            "severity": "research",
            "title": "V27通过项等待完整成本策略回测",
            "detail": (
                f"{len(v27_passed_candidates)} 项通过；下一步固定"
                "¥10,000、最低佣金、T+1、整数手及10/20BP滑点。"
            ),
        })
    if coverage_pct < 100:
        blockers.append({
            "severity": "blocking",
            "title": "全量历史日线尚未完成",
            "detail": (
                f"已下载 {downloaded_symbols:,}/{target_symbols:,} 只，"
                f"覆盖率 {coverage_pct:.3f}%。"
            ),
        })
    if unresolved_events:
        blockers.append({
            "severity": "blocking",
            "title": "特殊价格事件仍待核验",
            "detail": (
                f"还有 {unresolved_events} 条涨跌停边界异常需要逐条分类，"
                "正式因子IC和策略回测继续锁定。"
            ),
        })
    if formal and formal_statistical_passes == 0:
        blockers.append({
            "severity": "warning" if v4_2_passes else "blocking",
            "title": (
                "V4首批预注册全部失败，V4.2继续研究"
                if v4_2_passes
                else "预注册因子统计门禁全部失败"
            ),
            "detail": (
                f"全量V4已检验 {len(formal_rows)} 个“因子×周期”组合，"
                "0 个通过完整统计门禁；V4.2候选仍须通过含成本策略验证，"
                "目前禁止据此生成选股信号。"
            ),
        })
    if v4_1 and v4_1_passes:
        blockers.append({
            "severity": "warning",
            "title": "V4.1只有探索候选，尚非交易证据",
            "detail": (
                f"{v4_1_passes}/{v4_1_tests} 项探索门禁通过，"
                "但测试区间已被观察且生产门禁为0；只能进入未来模拟候选。"
            ),
        })
    if v4_2 and v4_2_passes == 0:
        blockers.append({
            "severity": "blocking",
            "title": "V4.2微观结构因子门禁未通过",
            "detail": (
                f"隔夜、日内换手冲击与高离散反转共检验 "
                f"{v4_2_tests} 项，0 项通过完整统计门禁。"
            ),
        })
    elif v4_2 and v4_2_passes:
        blockers.append({
            "severity": "warning",
            "title": "V4.2只有历史统计候选",
            "detail": (
                f"{v4_2_passes}/{v4_2_tests} 项统计门禁通过，"
                "但测试期已被观察，且含成本策略与前向模拟尚未通过。"
            ),
        })
    if v4_4_factor and v4_4_passes == 0:
        blockers.append({
            "severity": "blocking",
            "title": "V4.4三项预注册因子均未通过",
            "detail": (
                f"在4,802只真实A股上检验 {v4_4_tests} 项，"
                "0 项通过完整统计门禁；正IC没有转化为扣成本后的"
                "头部可交易收益。"
            ),
        })
    if v4_4_strategy and not v4_4_strategy.get(
        "historical_strategy_gate_passed"
    ):
        diagnostics = v4_4_strategy.get("cost_diagnostics") or {}
        low_cost = diagnostics.get("v4_4_cost_first") or {}
        blockers.append({
            "severity": "blocking",
            "title": "V4.4低成本策略仍不能交易",
            "detail": (
                f"交易 {int(low_cost.get('trades') or 0)} 笔，扣费后收益"
                f" {float(low_cost.get('total_return_pct') or 0):.3f}%，"
                f"最大回撤 {float(low_cost.get('max_drawdown_pct') or 0):.3f}%；"
                f"仍有 {len(v4_4_strategy.get('failed_gates') or [])} 项门禁失败。"
            ),
        })
    if v5_factor and not v5_factor.get("statistical_gate_passed"):
        test = (v5_factor.get("results") or {}).get("test") or {}
        blockers.append({
            "severity": "blocking",
            "title": "V5首次涨停溢出效应统计门禁失败",
            "detail": (
                f"测试期{int(test.get('executable_events') or 0):,}个可执行事件，"
                f"扣10BP平均收益{float(test.get('mean_event_net_return_pct') or 0):+.3f}%，"
                f"相对对照组{float(test.get('mean_daily_excess_return_pct') or 0):+.3f}%；"
                "因此不冻结策略、不运行¥10,000回测。"
            ),
        })
    if v6_factor and not v6_factor.get("statistical_gate_passed"):
        test = (
            ((v6_factor.get("result") or {}).get("splits") or {})
            .get("test") or {}
        )
        blockers.append({
            "severity": "blocking",
            "title": "V6非涨停型MAX效应统计门禁失败",
            "detail": (
                f"测试期平均IC {float(test.get('mean_ic') or 0):+.4f}，"
                f"95%区间[{float((test.get('block_bootstrap_95_ci') or [0, 0])[0] or 0):+.4f}, "
                f"{float((test.get('block_bootstrap_95_ci') or [0, 0])[1] or 0):+.4f}]；"
                f"最高组相对中间组扣10BP后"
                f"{float(test.get('top_minus_middle_after_cost_pct') or 0):+.3f}%。"
                "已封存，不运行¥10,000策略回测。"
            ),
        })
    if v9_factor and not v9_factor.get("statistical_gate_passed"):
        test = (
            ((v9_factor.get("result") or {}).get("splits") or {})
            .get("test") or {}
        )
        blockers.append({
            "severity": "blocking",
            "title": "V9融资买入强度统计门禁失败",
            "detail": (
                f"测试期平均IC {float(test.get('mean_ic') or 0):+.4f}，"
                f"95%区间[{float((test.get('block_bootstrap_95_ci') or [0, 0])[0] or 0):+.4f}, "
                f"{float((test.get('block_bootstrap_95_ci') or [0, 0])[1] or 0):+.4f}]；"
                f"头部相对中间组扣10BP后"
                f"{float(test.get('top_minus_middle_after_cost_pct') or 0):+.3f}%。"
                "IC显著但没有转化为可交易收益，已封存。"
            ),
        })
    if v10_factor and not v10_factor.get("statistical_gate_passed"):
        test = (
            ((v10_factor.get("result") or {}).get("splits") or {})
            .get("test") or {}
        )
        bootstrap_p = (
            (v10_factor.get("result") or {})
            .get("one_sided_block_bootstrap_p_value")
        )
        blockers.append({
            "severity": "blocking",
            "title": "V10点时同伴收益差统计门禁失败",
            "detail": (
                f"测试期平均IC {float(test.get('mean_ic') or 0):+.4f}，"
                f"95%区间[{float((test.get('block_bootstrap_95_ci') or [0, 0])[0] or 0):+.4f}, "
                f"{float((test.get('block_bootstrap_95_ci') or [0, 0])[1] or 0):+.4f}]，"
                f"单侧p值{float(bootstrap_p or 0):.4f}；"
                f"头部相对中间组扣10BP后"
                f"{float(test.get('top_minus_middle_after_cost_pct') or 0):+.3f}%。"
                "已按预注册规则封存，不运行¥10,000策略回测。"
            ),
        })
    if v11_factor and not v11_factor.get("statistical_gate_passed"):
        universe = v11_factor.get("universe") or {}
        result = v11_factor.get("result") or {}
        blockers.append({
            "severity": "blocking",
            "title": "V11解禁公告供给压力统计门禁失败",
            "detail": (
                f"{int(universe.get('matched_eligible_events') or 0):,}个"
                f"公告匹配事件形成{int(universe.get('executable_events') or 0):,}"
                f"个可执行事件，但只有"
                f"{int(result.get('evaluable_ic_days') or 0)}个可计算日横截面，"
                f"低于冻结切分所需"
                f"{int(result.get('minimum_required_for_split') or 0)}日。"
                "样本结构不足，已封存。"
            ),
        })
    elif heldout_count and strategy_passed == 0:
        blockers.append({
            "severity": "warning",
            "title": "因子有效不等于策略赚钱",
            "detail": (
                f"{heldout_count} 个因子通过独立IC测试，但没有因子通过"
                "含成本、跨时期策略门禁。"
            ),
        })
    if not worker_live and worker_declared_running:
        blockers.append({
            "severity": "warning",
            "title": "后台状态可能已经中断",
            "detail": (
                f"状态文件仍标记运行中，但最近心跳距今"
                f" {heartbeat_age if heartbeat_age is not None else '未知'} 秒。"
            ),
        })
    if v23_historical_passed:
        active_titles = {
            "全量历史日线尚未完成",
            "特殊价格事件仍待核验",
            "后台状态可能已经中断",
        }
        blockers = [
            item for item in blockers
            if item.get("title") in active_titles
        ]
    blockers.extend(_production_gate_blockers(gate_levels))

    stages = [
        {
            "id": "data",
            "label": "真实数据",
            "status": "passed" if data_complete else "running",
            "value": f"{coverage_pct:.3f}%",
            "detail": f"{downloaded_symbols:,}/{target_symbols:,} 只股票",
        },
        {
            "id": "quality",
            "label": "质量审计",
            "status": (
                "passed"
                if data_complete
                else ("blocked" if unresolved_events else "running")
            ),
            "value": f"{int(quality.get('passed_files') or 0):,} 通过",
            "detail": f"{audit_failed} 文件失败 · {unresolved_events} 事件待核验",
        },
        {
            "id": "factors",
            "label": "因子候选验证",
            "status": (
                "research_only" if v13_passed_candidates
                else "failed" if v13_screen_complete
                else "running"
            ),
            "value": (
                f"V18 {len(v13_passed_candidates)}项通过 · "
                f"{v13_completed_count}/{v13_total_count}完成"
            ),
            "detail": "4,802只真实A股 · 1/3/5/10日 · 训练/验证/测试隔离",
        },
        {
            "id": "strategy",
            "label": "严格产品历史门禁",
            "status": (
                "passed"
                if strategy_passed
                else "historical_only" if v23_historical_passed or v21_manual_unlocked
                else "running" if v13_strategy_started
                else "waiting"
            ),
            "value": (
                "严格产品历史门禁证书已验证"
                if strategy_passed
                else "V23历史完整性证据已核验（不计产品门禁）"
                if v23_historical_passed
                else "V22历史观察证据已记录"
                if v22_current_manual_unlocked
                else "V21历史观察证据已记录" if v21_manual_unlocked
                else
                f"严格门禁 {strategy_passed} 个通过"
                if v13_strategy_started or strategy_passed
                else "等待V18统计候选"
            ),
            "detail": (
                f"V23合并10BP收益 {float(v23_combined_ten_bp_metrics.get('total_return_pct') or 0):+.3f}% · "
                f"{int(v23_combined_ten_bp_metrics.get('trades') or 0)}笔 · "
                f"胜率 {float(v23_combined_ten_bp_metrics.get('win_rate_pct') or 0):.3f}% · "
                f"年化 {float(v23_combined_ten_bp_metrics.get('annualized_return_pct') or 0):.3f}% · "
                f"盈利因子 {float(v23_combined_ten_bp_metrics.get('profit_factor') or 0):.3f} · "
                f"回撤 {float(v23_combined_ten_bp_metrics.get('max_drawdown_pct') or 0):.3f}% · "
                "旧历史证据，不计严格产品门禁；禁止自动交易"
                if v23_historical_passed
                else f"V22当前段10BP收益 {float(v21_ten_bp_metrics.get('total_return_pct') or 0):+.3f}% · "
                f"{int(v21_ten_bp_metrics.get('trades') or 0)}笔 · 仅人工确认 · 禁止自动交易"
                if v22_current_manual_unlocked
                else f"V21 10BP收益 {float(v21_ten_bp_metrics.get('total_return_pct') or 0):+.3f}% · "
                f"{int(v21_ten_bp_metrics.get('trades') or 0)}笔 · 仅人工确认 · 禁止自动交易"
                if v21_manual_unlocked
                else "V20当前不要求60%胜率 · 仍必须扣10/20BP成本后盈利 · "
                "T+1 · 整数手 · 涨跌停约束"
                if v20_profit_first
                else "胜率≥60% · ≥100笔 · 扣10/20BP成本 · T+1 · 整数手 · 涨跌停约束"
            ),
        },
        {
            "id": "paper",
            "label": "6–8周模拟盘",
            "status": (
                "passed"
                if paper_passed
                else "active" if paper_clock_started
                else "ready" if strategy_passed
                else "preparing" if paper_preparing
                else "waiting"
            ),
            "value": (
                "门禁已通过"
                if paper_passed
                else (
                    f"正式计时中 · {int(paper.get('completed_trades') or 0)}/30笔"
                    if paper_clock_started
                    else "严格产品门禁证书已验证，等待系统启动正式时钟"
                    if strategy_passed
                    else "旧时钟已撤销为影子历史，等待严格产品证书"
                    if paper.get("status") == "legacy_forward_clock_revoked_shadow_only"
                    else "影子基础设施准备中" if paper_preparing
                    else "尚未启动"
                )
            ),
            "detail": (
                (
                    f"启动 {paper.get('clock_started_at')} · "
                    f"最早复核 {((paper.get('targets') or {}).get('earliest_review_at') or '--')} · "
                    "仍需至少30笔完成交易"
                )
                if paper_clock_started and not paper_passed
                else
                "旧前向记录已撤销为影子历史，0/30不计入产品前向；"
                "严格产品证书缺失。"
                if paper.get("status") == "legacy_forward_clock_revoked_shadow_only"
                else
                "并行准备不计入正式6–8周 · 正式期至少30笔完成交易"
                if paper_preparing and not strategy_passed
                else "至少30笔完成交易"
            ),
        },
        {
            "id": "capital",
            "label": "¥10,000真钱",
            "status": "ready" if real_money_allowed else "locked",
            "value": (
                "四级门禁通过 · 仅人工确认"
                if real_money_allowed
                else "真钱锁定"
            ),
            "detail": (
                "历史候选仅供观察；自动交易始终关闭"
                if manual_observation_available
                else "历史、前向、模拟盘、数据和独立复现全部通过后再决定"
            ),
        },
    ]

    return {
        "generated_at": factory_now.isoformat(timespec="seconds"),
        "gate_levels": gate_levels,
        "data_freshness": data_freshness,
        "run_manifest": run_manifest,
        "objective": {
            "primary": "最大化扣除成本后的账户长期正收益",
            "ranking": "总收益优先，胜率为辅助解释指标",
            "capital": 10000,
            "holding_days": "1–10个交易日",
            "candidate_count": "0–3只",
            "allow_cash": True,
            "drawdown_warning_pct": 10,
            "drawdown_hard_stop_pct": 15,
            "auto_trading": False,
        },
        "summary": {
            "research_gate_passed": bool(strategy_passed and data_complete),
            "product_historical_gate_passed": bool(strategy_passed),
            "legacy_integrity_evidence_passed": bool(
                v23_historical_passed
            ),
            "production_gate_passed": real_money_allowed,
            "gate_levels": gate_levels,
            "data_freshness_status": data_freshness.get(
                "overall_status"
            ),
            "strict_historical_gate": product_historical_gate,
            "legacy_historical_evidence": legacy_historical_gate,
            "data_complete": data_complete,
            "coverage_pct": coverage_pct,
            "downloaded_symbols": downloaded_symbols,
            "target_symbols": target_symbols,
            "quality_passed_files": int(quality.get("passed_files") or 0),
            "quality_rows": int(quality.get("rows") or 0),
            "unresolved_events": unresolved_events,
            "full_universe_ready": full_universe_ready,
            "full_universe_ready_definition": (
                "兼容字段：表示已定义研究范围完整，不表示全A股覆盖"
            ),
            "scope_universe_ready": scope_universe_ready,
            "full_a_share_ready": full_a_share_ready,
            "universe_scope": universe_scope,
            "candidate_count": len(candidates),
            "heldout_confirmed_count": heldout_count,
            "formal_factor_test_count": len(formal_rows),
            "formal_statistical_gate_pass_count": (
                formal_statistical_passes
            ),
            "v4_1_exploratory_test_count": v4_1_tests,
            "v4_1_exploratory_gate_pass_count": v4_1_passes,
            "v4_1_production_gate_pass_count": int(
                v4_1.get("production_gate_pass_count") or 0
            ),
            "v4_2_test_count": v4_2_tests,
            "v4_2_statistical_gate_pass_count": v4_2_passes,
            "v4_2_production_gate_pass_count": int(
                v4_2.get("production_gate_pass_count") or 0
            ),
            "v4_4_test_count": v4_4_tests,
            "v4_4_statistical_gate_pass_count": v4_4_passes,
            "v4_4_strategy_gate_passed": bool(
                v4_4_strategy.get("historical_strategy_gate_passed")
            ),
            "v6_statistical_gate_passed": bool(
                v6_factor.get("statistical_gate_passed")
            ),
            "v20_profit_first_gate_passed": bool(
                v20_profit_first.get("profit_first_pilot_gate_passed")
            ),
            "v21_manual_pilot_unlocked": real_money_allowed,
            "v22_current_manual_pilot_unlocked": real_money_allowed,
            "legacy_manual_evidence_passed": manual_observation_available,
            "v22_research_quality_gate_passed": bool(
                v22_risk_managed.get("research_quality_gate_passed")
            ),
            "v23_historical_strategy_gate_passed": v23_historical_passed,
            "v21_research_quality_gate_passed": bool(
                v23_historical_passed
                or v22_risk_managed.get("research_quality_gate_passed")
                or v21_manual_pilot.get("research_quality_gate_passed")
            ),
            "v13_completed_candidate_count": v13_completed_count,
            "v13_total_candidate_count": v13_total_count,
            "v13_statistical_gate_pass_count": len(v13_passed_candidates),
            "v27_completed_candidate_count": v27_completed_count,
            "v27_total_candidate_count": v27_total_count,
            "v27_statistical_gate_pass_count": len(
                v27_passed_candidates
            ),
            "v27_strategy_started": v27_strategy_started,
            "strategy_gate_passed_count": strategy_passed,
            "worker_live": worker_live,
            "worker_status": worker.get("status") or "not_started",
            "worker_cycle": int(worker.get("cycle") or 0),
            "worker_work_sequence": int(worker.get("work_sequence") or 0),
            "worker_heartbeat_at": worker.get("heartbeat_at"),
            "worker_heartbeat_age_seconds": heartbeat_age,
        },
        "accelerated_validation": accelerated_validation,
        "fifteen_hour_session": fifteen_hour_session,
        "delivery_plan": delivery_plan,
        "stages": stages,
        "label_protocol": {
            "information_time": pipeline_labels.get(
                "information_time", "T日收盘后"
            ),
            "entry_price": pipeline_labels.get(
                "entry_price", "T+1开盘价"
            ),
            "exit_price": pipeline_labels.get(
                "exit_price", "T+h收盘价"
            ),
            "holding_horizons_trading_days": pipeline_labels.get(
                "holding_horizons_trading_days", [1, 3, 5, 10]
            ),
            "entry_and_exit_must_be_tradable": pipeline_labels.get(
                "entry_and_exit_must_be_tradable", True
            ),
            "costs": pipeline_labels.get(
                "costs",
                "标签为毛收益；佣金、印花税、滑点在策略回测层扣除",
            ),
            "excluded_from_factor_columns": pipeline_labels.get(
                "excluded_from_factor_columns", True
            ),
            "availability": pipeline.get("label_availability") or {},
        },
        "blockers": blockers,
        "v21_manual_pilot": {
            "available": bool(
                v21_manual_pilot or v22_risk_managed or v23_market_regime
            ),
            "unlocked": real_money_allowed,
            "real_money_allowed": real_money_allowed,
            "observation_access": manual_observation_available,
            "access_level": (
                "real_money" if real_money_allowed else "observation_only"
            ),
            "research_quality_passed": bool(
                v23_historical_passed
                or v22_risk_managed.get("research_quality_gate_passed")
                if (v23_market_regime or v22_risk_managed)
                else v21_manual_pilot.get("research_quality_gate_passed")
            ),
            "automatic_trading_unlocked": False,
            "metrics_10bp": v21_ten_bp_metrics,
            "canonical_metrics_10bp": (
                v23_canonical_ten_bp_metrics
                if v23_historical_passed
                else v21_ten_bp_metrics
            ),
            "canonical_metrics_scope": (
                "v23_cross_year_combined"
                if v23_historical_passed
                else "active_current_period"
            ),
            "canonical_metrics_generated_at": (
                v23_market_regime.get("generated_at")
                if v23_historical_passed
                else (
                    v22_risk_managed.get("generated_at")
                    if v22_current_manual_unlocked
                    else v21_manual_pilot.get("generated_at")
                )
            ),
            "candidate": (
                factory_manual_candidate if real_money_allowed else None
            ),
            "observation_candidate": factory_manual_candidate,
            "current_decision": v23_current_decision,
            "integrity_audit": v24_integrity_audit,
            "execution_rule": manual_execution_rule,
            "limitations": (
                v23_market_regime.get("limitations") or []
                if v23_historical_passed
                else v22_risk_managed.get("limitations") or []
                if v22_current_manual_unlocked
                else v21_manual_pilot.get("limitations") or []
            ),
            "combined_metrics_10bp": (
                v23_combined_ten_bp_metrics
                if v23_historical_passed
                else v22_combined_ten_bp_metrics
            ),
            "failed_research_gates": (
                v23_gate.get("failed_gates") or []
                if v23_market_regime
                else v22_risk_managed.get("failed_gates") or []
            ),
        },
        "v22_risk_managed": v22_risk_managed,
        "v23_market_regime": v23_market_regime,
        "v23_current_decision": v23_current_decision,
        "v24_integrity_audit": v24_integrity_audit,
        "v25_campaign": v25_campaign,
        "v27_new_mechanism": {
            "available": True,
            "report_generated_at": v27_factor.get("generated_at"),
            "status": v27_factor.get("status") or (
                "running"
                if v27_status.get("status") == "running"
                else "registered_waiting"
            ),
            "protocol": (
                "research/V27_QLIB_MECHANISM_COHORT_PREREGISTRATION.md"
            ),
            "scope": (v27_factor.get("registry") or {}).get("scope")
            or v27_registry_payload().get("scope"),
            "source": (v27_factor.get("registry") or {}).get("source")
            or v27_registry_payload().get("source"),
            "completed": v27_completed_count,
            "total": v27_total_count,
            "statistical_passes": len(v27_passed_candidates),
            "passed_candidates": v27_passed_candidates,
            "current_candidate": v27_status.get("candidate_id"),
            "progress": v27_status,
            "candidates": v27_factor.get("candidates") or {},
            "strategy": v27_strategy,
            "weighted_ensemble": v27_ensemble,
            "strategy_adjudication": v27_adjudication["strategy"],
            "weighted_ensemble_adjudication": (
                v27_adjudication["weighted_ensemble"]
            ),
            "combination_rule": (
                "至少两个统计通过项才允许另行预注册训练期加权组合；"
                "不足时不得用失败因子凑组合。"
            ),
            "evidence_class": (
                "historical_exploratory_existing_test_period_observed"
            ),
        },
        "v23_registered_gate": v23_gate,
        "factors": {
            "generated_at": factors.get("generated_at"),
            "status": factors.get("status"),
            "date_splits": factors.get("date_splits") or {},
            "evaluation": factors.get("evaluation") or {},
            "families": families,
            "candidates": candidates,
            "warning": factors.get("warning"),
            "production_blockers": factors.get(
                "production_blockers"
            ) or [],
        },
        "formal_research": {
            "available": bool(formal),
            "generated_at": formal.get("generated_at"),
            "status": formal.get("status"),
            "universe": formal.get("universe") or {},
            "splits": formal.get("splits") or {},
            "purge_trading_days": formal.get(
                "purge_trading_days"
            ),
            "families": formal_families,
            "rows": formal_rows,
            "statistical_gate_pass_count": formal_statistical_passes,
            "production_factor_gate_pass_count": int(
                formal.get("production_factor_gate_pass_count") or 0
            ),
            "market_regime": formal.get("market_regime") or {},
            "production_blockers": formal.get(
                "production_blockers"
            ) or [],
            "warning": formal.get("warning"),
        },
        "v4_1_research": {
            "available": bool(v4_1),
            "generated_at": v4_1.get("generated_at"),
            "status": v4_1.get("status"),
            "historical_evidence_class": v4_1.get(
                "historical_evidence_class"
            ),
            "exploratory_test_count": v4_1_tests,
            "exploratory_gate_pass_count": v4_1_passes,
            "production_gate_pass_count": int(
                v4_1.get("production_gate_pass_count") or 0
            ),
            "hypotheses": v4_1.get("hypotheses") or {},
            "fixed_parameters": v4_1.get("fixed_parameters") or {},
            "warning": v4_1.get("warning"),
        },
        "v4_2_research": {
            "available": bool(v4_2),
            "generated_at": v4_2.get("generated_at"),
            "status": v4_2.get("status"),
            "historical_evidence_class": v4_2.get(
                "historical_evidence_class"
            ),
            "test_count": v4_2_tests,
            "statistical_gate_pass_count": v4_2_passes,
            "production_gate_pass_count": int(
                v4_2.get("production_gate_pass_count") or 0
            ),
            "splits": v4_2.get("splits") or {},
            "fixed_parameters": v4_2.get("fixed_parameters") or {},
            "dispersion_state": v4_2.get("dispersion_state") or {},
            "tests": v4_2.get("tests") or [],
            "production_blockers": v4_2.get(
                "production_blockers"
            ) or [],
            "warning": v4_2.get("warning"),
        },
        "v4_4_research": {
            "available": bool(v4_4_factor or v4_4_strategy),
            "factor": {
                "generated_at": v4_4_factor.get("generated_at"),
                "status": v4_4_factor.get("status"),
                "universe": v4_4_factor.get("universe") or {},
                "test_count": v4_4_tests,
                "statistical_gate_pass_count": v4_4_passes,
                "tests": v4_4_factor.get("tests") or [],
                "warning": v4_4_factor.get("warning"),
            },
            "strategy": {
                "generated_at": v4_4_strategy.get("generated_at"),
                "status": v4_4_strategy.get("status"),
                "gate_passed": bool(
                    v4_4_strategy.get(
                        "historical_strategy_gate_passed"
                    )
                ),
                "configuration": v4_4_strategy.get("strategy") or {},
                "cost_model": v4_4_strategy.get("cost_model") or {},
                "cost_diagnostics": v4_4_strategy.get(
                    "cost_diagnostics"
                ) or {},
                "observed_test": (
                    (v4_4_strategy.get("results") or {}).get(
                        "observed_test"
                    ) or {}
                ),
                "failed_gates": v4_4_strategy.get("failed_gates") or [],
                "warning": v4_4_strategy.get("warning"),
            },
        },
        "v5_research": {
            "available": bool(v5_factor),
            "generated_at": v5_factor.get("generated_at"),
            "status": v5_factor.get("status"),
            "historical_evidence_class": v5_factor.get(
                "historical_evidence_class"
            ),
            "hypothesis": v5_factor.get("hypothesis") or {},
            "universe": v5_factor.get("universe") or {},
            "splits": v5_factor.get("splits") or {},
            "results": v5_factor.get("results") or {},
            "test_folds": v5_factor.get("test_folds") or [],
            "gate_conditions": v5_factor.get("gate_conditions") or {},
            "failed_gates": v5_factor.get("failed_gates") or [],
            "statistical_gate_passed": bool(
                v5_factor.get("statistical_gate_passed")
            ),
            "strategy_freeze_allowed": bool(
                v5_factor.get("strategy_freeze_allowed")
            ),
            "next_action": v5_factor.get("next_action"),
            "rejected_proposals": v5_factor.get(
                "rejected_proposals"
            ) or [],
            "warning": v5_factor.get("warning"),
        },
        "v6_research": {
            "available": bool(v6_factor),
            "generated_at": v6_factor.get("generated_at"),
            "status": v6_factor.get("status"),
            "historical_evidence_class": v6_factor.get(
                "historical_evidence_class"
            ),
            "universe": v6_factor.get("universe") or {},
            "splits": v6_factor.get("splits") or {},
            "fixed_parameters": v6_factor.get("fixed_parameters") or {},
            "result": v6_factor.get("result") or {},
            "rejected_deepseek_candidates": v6_factor.get(
                "rejected_deepseek_candidates"
            ) or [],
            "statistical_gate_passed": bool(
                v6_factor.get("statistical_gate_passed")
            ),
            "strategy_freeze_allowed": bool(
                v6_factor.get("strategy_freeze_allowed")
            ),
            "next_action": v6_factor.get("next_action"),
            "warning": v6_factor.get("warning"),
        },
        "v9_research": {
            "available": bool(v9_factor),
            "generated_at": v9_factor.get("generated_at"),
            "status": v9_factor.get("status"),
            "protocol": v9_factor.get("protocol"),
            "data_source": v9_factor.get("data_source"),
            "database": v9_factor.get("database") or {},
            "fixed_execution": v9_factor.get("fixed_execution") or {},
            "result": v9_factor.get("result") or {},
            "statistical_gate_passed": bool(
                v9_factor.get("statistical_gate_passed")
            ),
            "strategy_backtest_allowed": bool(
                v9_factor.get("strategy_backtest_allowed")
            ),
            "real_money_allowed": bool(
                v9_factor.get("real_money_allowed")
            ),
            "next_action": v9_factor.get("next_action"),
        },
        "v14_research": {
            "available": bool(v14_factor),
            "generated_at": v14_factor.get("generated_at"),
            "status": v14_factor.get("status"),
            "progress": v14_status,
            "protocol": v14_factor.get("protocol"),
            "data_source": v14_factor.get("data_source"),
            "database": v14_factor.get("database") or {},
            "factor": v14_factor.get("factor") or {},
            "fixed_execution": v14_factor.get("fixed_execution") or {},
            "result": v14_factor.get("result") or {},
            "statistical_gate_passed": bool(
                v14_factor.get("statistical_gate_passed")
            ),
            "strategy_backtest_allowed": bool(
                v14_factor.get("strategy_backtest_allowed")
            ),
            "strategy_available": bool(v14_strategy),
            "strategy_gate_passed": bool(
                v14_strategy.get("historical_strategy_gate_passed")
            ),
            "strategy_results": v14_strategy.get("results") or {},
            "strategy_failed_gates": (
                v14_strategy.get("failed_gates") or []
            ),
            "real_money_allowed": bool(
                v14_strategy.get("historical_strategy_gate_passed")
            ),
            "next_action": v14_factor.get("next_action"),
            "warning": v14_factor.get("warning"),
        },
        "v15_research": {
            "available": bool(v15_source_audit or v15_events or v15_factor),
            "generated_at": (
                v15_factor.get("generated_at")
                or v15_events.get("generated_at")
                or v15_source_audit.get("generated_at")
            ),
            "status": (
                v15_factor.get("status")
                or v15_events.get("status")
                or v15_source_audit.get("status")
            ),
            "progress": (
                v15_factor_status
                or v15_event_status
                or v15_source_status
            ),
            "protocol": v15_source_audit.get("protocol"),
            "sources": v15_source_audit.get("sources") or {},
            "coverage": v15_source_audit.get("coverage") or {},
            "sample_audit": v15_source_audit.get("sample_audit") or {},
            "source_gate_conditions": (
                v15_source_audit.get("gate_conditions") or {}
            ),
            "source_failed_gates": (
                v15_source_audit.get("failed_gates") or []
            ),
            "source_data_gate_passed": bool(
                v15_source_audit.get("data_gate_passed")
            ),
            "event_report": v15_events,
            "event_data_gate_passed": bool(
                v15_events.get("event_data_gate_passed")
            ),
            "factor_report": v15_factor,
            "statistical_gate_passed": bool(
                v15_factor.get("statistical_gate_passed")
            ),
            "real_money_gate_passed": bool(
                v15_factor.get("real_money_gate_passed")
            ),
            "failed_gates": (
                v15_factor.get("failed_gates")
                or v15_events.get("failed_gates")
                or v15_source_audit.get("failed_gates")
                or []
            ),
            "next_action": (
                v15_factor.get("next_action")
                or v15_events.get("next_action")
                or v15_source_audit.get("next_action")
            ),
            "warning": (
                v15_factor.get("warning")
                or v15_events.get("warning")
                or v15_source_audit.get("warning")
            ),
        },
        "v16_research": {
            "available": bool(v16_events or v16_factor),
            "generated_at": (
                v16_factor.get("generated_at")
                or v16_events.get("generated_at")
            ),
            "status": (
                v16_factor.get("status")
                or v16_events.get("status")
            ),
            "progress": v16_factor_status or v16_event_status,
            "event_progress": v16_event_status,
            "factor_progress": v16_factor_status,
            "protocol": v16_factor.get("protocol"),
            "event_report": v16_events,
            "event_data_gate_passed": bool(
                v16_events.get("data_gate_passed")
            ),
            "factor_report": v16_factor,
            "statistical_gate_passed": bool(
                v16_factor.get("statistical_gate_passed")
            ),
            "real_money_gate_passed": bool(
                v16_factor.get("real_money_gate_passed")
            ),
            "failed_gates": v16_factor.get("failed_gates") or [],
            "next_action": (
                v16_factor.get("next_action")
                or (
                    "外部公告源限流；使用已完成缓存，不重复计算收益"
                    if v16_event_status.get("status") == "failed"
                    else None
                )
            ),
            "warning": v16_factor.get("warning"),
        },
        "v17_research": {
            "available": bool(v17_factor),
            "generated_at": v17_factor.get("generated_at"),
            "status": v17_factor.get("status"),
            "progress": v17_factor_status,
            "protocol": v17_factor.get("protocol"),
            "source": v17_factor.get("source") or {},
            "event_counts": v17_factor.get("event_counts") or {},
            "results": v17_factor.get("results") or {},
            "statistical_gate_passed": bool(
                v17_factor.get("statistical_gate_passed")
            ),
            "real_money_gate_passed": bool(
                v17_factor.get("real_money_gate_passed")
            ),
            "failed_gates": v17_factor.get("failed_gates") or [],
            "next_action": v17_factor.get("next_action"),
            "warning": v17_factor.get("warning"),
            "actual_compute_seconds": v17_factor.get("actual_compute_seconds"),
        },
        "v10_research": {
            "available": bool(v10_factor),
            "generated_at": v10_factor.get("generated_at"),
            "status": v10_factor.get("status"),
            "protocol": v10_factor.get("protocol"),
            "data_source": v10_factor.get("data_source"),
            "database": v10_factor.get("database") or {},
            "fixed_parameters": v10_factor.get("fixed_parameters") or {},
            "result": v10_factor.get("result") or {},
            "statistical_gate_passed": bool(
                v10_factor.get("statistical_gate_passed")
            ),
            "strategy_backtest_allowed": bool(
                v10_factor.get("strategy_backtest_allowed")
            ),
            "real_money_allowed": bool(
                v10_factor.get("real_money_allowed")
            ),
            "next_action": v10_factor.get("next_action"),
        },
        "v11_research": {
            "available": bool(v11_factor),
            "generated_at": v11_factor.get("generated_at"),
            "status": v11_factor.get("status"),
            "protocol": v11_factor.get("protocol"),
            "data_source": v11_factor.get("data_source"),
            "data_gate": v11_factor.get("data_gate") or {},
            "universe": v11_factor.get("universe") or {},
            "fixed_execution": v11_factor.get("fixed_execution") or {},
            "result": v11_factor.get("result") or {},
            "statistical_gate_passed": bool(
                v11_factor.get("statistical_gate_passed")
            ),
            "strategy_backtest_allowed": bool(
                v11_factor.get("strategy_backtest_allowed")
            ),
            "real_money_allowed": bool(
                v11_factor.get("real_money_allowed")
            ),
            "next_action": v11_factor.get("next_action"),
        },
        "strategies": strategy_cards,
        "regime_matrix": (
            {
                "strategy_id": "formal_breadth60",
                "metric_type": "forward_return",
                "hypothesis_h1_passed": bool(
                    (
                        formal.get("market_regime") or {}
                    ).get("hypothesis_h1_passed")
                ),
                "items": [
                    {
                        "regime": key,
                        "label": {
                            "strong": "偏强",
                            "sideways": "震荡",
                            "weak": "偏弱",
                        }.get(key, key),
                        "observations": (
                            values.get("5") or {}
                        ).get("observations"),
                        "return_5d_pct": (
                            values.get("5") or {}
                        ).get("mean_equal_weight_return_pct"),
                        "return_10d_pct": (
                            values.get("10") or {}
                        ).get("mean_equal_weight_return_pct"),
                        "positive_5d_pct": (
                            values.get("5") or {}
                        ).get("positive_days_pct"),
                    }
                    for key, values in (
                        (
                            formal.get("market_regime") or {}
                        ).get("states") or {}
                    ).items()
                ],
            }
            if formal
            else {
                "strategy_id": "cost_aware",
                "metric_type": "strategy_pnl",
                "items": [
                    {
                        "regime": key,
                        **value,
                    }
                    for key, value in (
                        current.get("regime_trade_stats") or {}
                    ).items()
                ],
            }
        ),
        "worker": {
            "declared_status": worker.get("status") or "not_started",
            "live": worker_live,
            "cycle": int(worker.get("cycle") or 0),
            "current_step": worker.get("current_step"),
            "message": worker.get("message"),
            "heartbeat_at": worker.get("heartbeat_at"),
            "heartbeat_age_seconds": heartbeat_age,
            "progress": progress,
            "deepseek": worker.get("deepseek") or {},
            "experiment_queue": {
                "updated_at": experiment_queue.get("updated_at"),
                "tasks": list(
                    (experiment_queue.get("tasks") or {}).values()
                ),
            },
            "safety": worker.get("safety") or {},
            "events": _read_events(worker_events_path),
        },
        "next_experiments": (
            _experiment_titles(v4_2_protocol_path)
            or _experiment_titles(deepseek_path)
        ),
        "safety": {
            "signals_unlocked": False,
            "v21_manual_pilot_signals_unlocked": real_money_allowed,
            "v22_risk_managed_manual_signals_unlocked": real_money_allowed,
            "v23_historical_strategy_gate_passed": v23_historical_passed,
            "paper_trading_unlocked": False,
            "real_money_allowed": real_money_allowed,
            "reason": (
                "四级门禁已通过；真钱仍仅允许逐笔人工确认，自动交易关闭。"
                if real_money_allowed
                else "历史候选只用于观察；前向、模拟盘、数据新鲜度和独立复现"
                "未全部通过，真钱与自动交易均锁定。"
                if manual_observation_available
                else "全量数据与特殊事件审计已通过；正式因子门禁、"
                "含成本策略门禁和模拟盘尚未通过。"
                if data_complete
                else (
                    "全量数据、特殊事件审计、策略门禁和模拟盘"
                    "尚未全部通过。"
                )
            ),
        },
    }
