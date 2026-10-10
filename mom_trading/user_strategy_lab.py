"""Safe, local strategy DSL and reproducible research-only backtests."""

from __future__ import annotations

import fcntl
import hashlib
import io
import inspect
import json
import math
import os
import secrets
import threading
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from factor_system import config as factor_config

# P3-1 统一 QC：同花顺费用口径 + 统计检验与 factor_system / 回测中心共用。
try:
    import quantqc.costs as qc
    import quantqc.stats as qs
except Exception:  # pragma: no cover - 仅当 quantqc 不可导入时降级
    qc = qs = None


ROOT = Path(__file__).resolve().parents[1]
CODE_ROOT = ROOT
INDEX_PATH = ROOT / "research" / "data" / "current" / "sh000001.csv"
QUALITY_PATH = ROOT / "reports" / "v4_daily_quality_audit.json"
PIPELINE_PATH = ROOT / "reports" / "v4_factor_pipeline_dry_run.json"
V4_MANIFEST_PATH = ROOT / "reports" / "v4_daily_download_manifest.json"
V4_CALENDAR_PATH = ROOT / "reports" / "v4_market_calendar.json"
FACTOR_DATA_PATH = ROOT / "factor_system" / "data.py"
FACTOR_V4_BRIDGE_PATH = ROOT / "factor_system" / "v4_panel.py"
FACTOR_COMPOSE_PATH = ROOT / "factor_system" / "compose.py"
APP_SOURCE_PATH = ROOT / "mom_trading" / "app.py"
STRATEGY_EVALUATOR_SOURCE_PATHS = (
    CODE_ROOT / "mom_trading" / "user_strategy_lab.py",
    CODE_ROOT / "factor_system" / "config.py",
    CODE_ROOT / "factor_system" / "data.py",
    CODE_ROOT / "factor_system" / "v4_panel.py",
    CODE_ROOT / "factor_system" / "compose.py",
    CODE_ROOT / "factor_system" / "backtest.py",
    CODE_ROOT / "factor_system" / "diagnostics.py",
    CODE_ROOT / "factor_system" / "metrics.py",
    CODE_ROOT / "factor_system" / "validate.py",
    CODE_ROOT / "factor_system" / "registry.py",
    CODE_ROOT / "factor_system" / "gate.py",
    CODE_ROOT / "factor_system" / "search.py",
    CODE_ROOT / "factor_system" / "templates.py",
    CODE_ROOT / "factor_system" / "operators.py",
    CODE_ROOT / "factor_system" / "preprocess.py",
    CODE_ROOT / "quantqc" / "ashare.py",
    CODE_ROOT / "quantqc" / "costs.py",
    CODE_ROOT / "quantqc" / "stats.py",
)
STRATEGIES_DIR = ROOT / "research" / "user_strategies"
REPORTS_DIR = ROOT / "reports" / "user_strategy_backtests"
REPORT_INDEX_SCHEMA = "user_strategy_report_index_v1"
REPORT_HEADER_READ_LIMIT = 1024 * 1024
EVIDENCE_DIR = ROOT / "research" / "data" / "user_strategy_backtests"
SHANGHAI = ZoneInfo("Asia/Shanghai")
RUN_LOCK = threading.Lock()
RESEARCH_MUTATION_LOCK_PATH = ROOT / "factor_system" / "state" / "research_mutation.lock"
STRATEGY_ID_PREFIX = "user-"
RUN_ID_PREFIX = "run-"
COST_LADDER_BPS = (0, 5, 10, 20, 30, 50)
BACKTEST_REPORT_VERSION = 3
BACKTEST_REPORT_SCHEMA = "user_strategy_backtest_strict_v1"
UNIFIED_DIAGNOSIS_REPORT_SCHEMA = "user_strategy_unified_diagnosis_v1"
CURVE_REFRESH_AFTER_SECONDS = 15
V4_DAILY_DIR = ROOT / "research" / "data" / "v4_daily"
V4_INCREMENTAL_LOCK_PATH = ROOT / "instance" / "v4_daily_incremental.lock"
V4_FACTOR_PIPELINE_PATH = ROOT / "research" / "v4_factor_pipeline.py"
V4_SPECIAL_EVENTS_PATH = ROOT / "research" / "data" / "special_price_limit_events.csv"
FORWARD_DIR = ROOT / "research" / "user_strategy_forward"
FORWARD_LOCK_PATH = ROOT / "instance" / "user_strategy_forward.lock"
FORWARD_REFRESH_AFTER_SECONDS = 15
FORWARD_SCHEMA = "user_strategy_forward_shadow_v1"
FORWARD_LOGIC_VERSION = "forward_shadow_label_continuation_v1"
FORWARD_RUN_LOCK = threading.Lock()


def _configured_rebal_buffer() -> int:
    """统一引擎当前的换仓缓冲默认值；旧规格缺字段时保持原有语义。"""
    from factor_system import config as fconfig

    return int(fconfig.COSTS.get("rebal_buffer", 3))


def acquire_research_mutation_lock(*, blocking: bool = False):
    """取得跨进程研究写锁；忙时返回 ``None``，成功返回须保活的文件句柄。"""
    RESEARCH_MUTATION_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = RESEARCH_MUTATION_LOCK_PATH.open("a+", encoding="utf-8")
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    json.dump({
        "schema": "research_mutation_lock_v1",
        "pid": os.getpid(),
        "acquired_at": _now(),
    }, handle, ensure_ascii=False, separators=(",", ":"))
    handle.flush()
    return handle


def release_research_mutation_lock(handle) -> None:
    """释放 ``acquire_research_mutation_lock`` 返回的句柄（幂等容错）。"""
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def research_mutation_lock_state() -> dict[str, Any]:
    """Probe the flock without changing it; file metadata is advisory only."""
    path = RESEARCH_MUTATION_LOCK_PATH
    if not path.is_file():
        return {
            "busy": False,
            "metadata": None,
            "metadata_format": "missing",
            "hint": "锁文件不存在，当前空闲；busy 以非阻塞 flock 探测为准",
        }
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = handle.read().strip()
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                busy = True
            else:
                busy = False
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        return {
            "busy": None,
            "metadata": None,
            "metadata_format": "unreadable",
            "hint": f"锁探测失败: {exc}",
        }

    metadata = None
    metadata_format = "empty"
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            metadata = {
                key: parsed.get(key) for key in ("schema", "pid", "acquired_at")
                if parsed.get(key) is not None
            }
            metadata_format = "json_v1"
        else:
            # Compatibility with the previous ``pid=... acquired_at=...`` text.
            legacy = {}
            for token in raw.replace("\n", " ").split():
                key, separator, value = token.partition("=")
                if separator and key in {"pid", "acquired_at"}:
                    legacy[key] = value
            if legacy:
                try:
                    legacy["pid"] = int(legacy["pid"])
                except (KeyError, TypeError, ValueError):
                    pass
                metadata = legacy
                metadata_format = "legacy_text"
            else:
                metadata_format = "unparseable"

    if metadata_format == "legacy_text":
        hint = (
            "检测到旧文本锁元数据，可能过期；busy 仅以非阻塞 flock 探测为准"
        )
    elif busy:
        hint = "锁正忙；metadata 由最近一次成功获取者写入"
    else:
        hint = "锁当前空闲；metadata 仅表示最近一次成功获取，不代表仍持锁"
    return {
        "busy": busy,
        "pid": metadata.get("pid") if metadata else None,
        "acquired_at": metadata.get("acquired_at") if metadata else None,
        "metadata": metadata,
        "metadata_format": metadata_format,
        "hint": hint,
    }

FACTOR_CATALOG = {
    "trend60": {
        "label": "60日趋势",
        "description": "复权收益指数相对60日均线；正权重偏好强趋势。",
    },
    "residual_momentum_20_5": {
        "label": "20减5日动量",
        "description": "跳过最近5日的中短期动量；正权重偏好强动量。",
    },
    "low_volatility_20": {
        "label": "20日低波动",
        "description": "20日波动率的相反数；正权重偏好低波动。",
    },
    "price_volume_confirmation": {
        "label": "价量确认",
        "description": "5日收益与短中期成交额变化的交互。",
    },
    "liquidity_20": {
        "label": "20日流动性",
        "description": "20日平均成交额对数；正权重偏好高流动性。",
    },
    # ── M4.1 A股实证因子族（2026-08-15 Web 研究新增）────────────
    # 方向编码约定：因子值高 = 该因子经济假设下的「好票」，正权重即买它（高=买）。
    # 依据：research/2026-08-15_quant_research_strategy.md 六源汇总——A股中期动量
    # 基本不存在/反向、短期反转最强、小市值溢价强但极不稳定、非流动性溢价存在
    # （与小市值共线）、低换手溢价、彩票(MAX)负预期、价值 2019 后失效。
    "short_term_reversal_5": {
        "label": "5日短期反转",
        "description": "正权重=买近期输家（A股周频反转最强）。⚠ 高换手成本敏感。",
    },
    "medium_term_reversal_25_5": {
        "label": "25日中期反转",
        "description": "正权重=买中期输家（月度 J=6/K=1 反转最强）。",
    },
    "size_small": {
        "label": "小市值",
        "description": "正权重=买小盘。⚠ 溢价强但 2024-01 微盘 -40% 危机，容量极小，仅小仓卫星。",
    },
    "illiquidity_amihud_20": {
        "label": "20日Amihud非流动性",
        "description": "正权重=买低流动性（非流动性溢价）。⚠ 与小市值共线，勿叠加过重。",
    },
    "low_turnover_20": {
        "label": "20日低换手",
        "description": "正权重=买低换手（低换手溢价）。",
    },
    "lottery_max_return_20": {
        "label": "20日最大日收益(彩票)",
        "description": "**负权重**=买非彩票股（彩票/MAX 效应负预期）。",
    },
    "value_earnings_yield": {
        "label": "盈利收益率 1/PE",
        "description": "正权重=买便宜。⚠ 价值 2019 后失效 + PIT 数据仅 2021-09~2024-12，仅研究假设。",
    },
    "value_book_yield": {
        "label": "账面收益率 1/PB",
        "description": "正权重=买便宜。⚠ 同 1/PE：2019 后失效，仅研究假设。",
    },
}

# M7 统一因子宇宙：13 内置实证因子 + 156 用户挖掘配方。
# 配方因子证据（oos_ic/ic_retention/monotonic）随 catalog 下发前端证据芯片。
_FACTOR_PANEL_COVERAGE = {
    "start": "2021-01-04",
    "end": "2025-12-31",
    "note": "配方因子由因子系统面板重建（2021-01~2025-12）",
}


def _full_factor_catalog() -> dict[str, Any]:
    """内置因子沿用 FACTOR_CATALOG 中文文案；配方带 evidence/group/direction_hint。

    M7：策略编辑器因子宇宙 = 内置 + 我的因子（全部研究态，证据如实展示）。
    """
    from factor_system.catalog import FACTOR_META
    merged = {
        k: {**v, **({"label": FACTOR_CATALOG[k]["label"],
                      "description": FACTOR_CATALOG[k]["description"]}
                     if k in FACTOR_CATALOG else {})}
        for k, v in FACTOR_META().items()
    }
    return merged


_RUNNABLE_GENERATED_FACTOR_STATUSES = {
    "candidate", "validated", "registered", "backtesting",
    "historical_pass", "production",
}


def _registered_factor_run_states(
    factor_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Read the registry truth needed to admit generated factors to new runs."""
    if not factor_ids:
        return {}
    from factor_system import registry as freg

    placeholders = ",".join("?" for _ in factor_ids)
    try:
        connection = freg._conn(readonly=True)
        rows = connection.execute(
            f"SELECT factor_id,status,research_epoch,status_reason,"
            f"sealed_at,sealed_data_end,sealed_recipe_sha256,"
            f"research_record_json,research_record_sha256 FROM factors "
            f"WHERE factor_id IN ({placeholders})",
            factor_ids,
        ).fetchall()
    except Exception as exc:
        raise StrategyValidationError(
            f"无法核验挖掘因子当前状态: {exc}"
        ) from exc
    finally:
        if "connection" in locals():
            connection.close()
    return {str(row["factor_id"]): dict(row) for row in rows}


def _factor_run_blockers(factor_ids: list[str]) -> list[dict[str, str]]:
    """Fail closed unless every factor has a runnable canonical source record."""
    from factor_system import config as fconfig
    from factor_system import registry as freg
    from factor_system import source_gate

    blockers = []
    try:
        source_catalog = source_gate.load_catalog(fconfig.SOURCE_CATALOG)
        source_check = source_gate.verify_catalog(source_catalog)
    except Exception as exc:
        return [{
            "factor_id": "*",
            "code": "source_catalog_unavailable",
            "message": f"来源目录不可用：{type(exc).__name__}",
        }]
    if source_check.get("valid") is not True:
        return [{
            "factor_id": "*",
            "code": "source_catalog_invalid",
            "message": "来源目录校验失败，所有策略因子保持禁用",
        }]

    for factor_id in sorted(set(factor_ids)):
        source = source_gate.verify_candidate(
            factor_id, source_catalog,
        )
        if source.get("runnable") is not True:
            blockers.append({
                "factor_id": factor_id,
                "code": "factor_source_not_runnable",
                "message": (
                    f"来源/预注册/PIT 门禁未通过："
                    f"{source.get('status') or 'not_in_catalog'}"
                ),
            })

    generated = sorted(set(factor_ids) - set(FACTOR_CATALOG))
    states = _registered_factor_run_states(generated)
    for factor_id in generated:
        row = states.get(factor_id)
        if row is None:
            blockers.append({
                "factor_id": factor_id,
                "code": "factor_registry_missing",
                "message": "注册表没有该因子的当前状态",
            })
            continue
        status = str(row.get("status") or "")
        if row.get("research_epoch") != factor_config.RESEARCH_EPOCH:
            blockers.append({
                "factor_id": factor_id,
                "code": "factor_archived_epoch",
                "message": "因子属于旧 research epoch，仅可审计",
            })
        elif status not in _RUNNABLE_GENERATED_FACTOR_STATUSES:
            reason = str(row.get("status_reason") or "无补充原因")
            blockers.append({
                "factor_id": factor_id,
                "code": f"factor_status_{status or 'unknown'}",
                "message": f"当前状态 {status or 'unknown'} 禁止新运行：{reason}",
            })
        else:
            recipe_path = fconfig.REGISTRY_DIR / "recipes" / f"{factor_id}.json"
            try:
                recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
                freg.verify_frozen_source_record(row, recipe)
            except Exception as exc:
                blockers.append({
                    "factor_id": factor_id,
                    "code": "factor_source_record_invalid",
                    "message": f"注册表与配方来源冻结记录不一致：{exc}",
                })
    return blockers


DEFAULT_SPEC = {
    "name": "我的多因子策略",
    # New editable drafts use the user's research budget. Legacy normalisation,
    # archived templates and fixed diagnostic protocols retain their own capital.
    "account_capital": 150_000.0,
    "holding_days": 5,
    "top_n": 30,
    "one_way_cost_bps": 10,
    "rebal_buffer": _configured_rebal_buffer(),
    "risk_rule": "none",
    "entry_rule": "none",
    "exposure_rule": "none",
    "factors": {
        "trend60": 1.0,
        "residual_momentum_20_5": 1.0,
        "low_volatility_20": 1.0,
        "price_volume_confirmation": 0.0,
        "liquidity_20": 0.25,
    },
}

# ── M3.4 策略模板库（方向显式）────────────────────────────────
# 组合语义：横截面 pct-rank × 权重 求和，高=买，权重符号即方向。
# 因子值定义（compose.py REBUILDERS）：
#   trend60/residual_momentum_20_5/price_volume_confirmation = 原始量（高=强趋势/强动量/价量确认）
#   low_volatility_20 = -20日波动率（已取相反数 → 高=低波动，正权重即买低波动）
#   liquidity_20 = log(20日均额)（高=流动性好，正权重是可交易性倾斜，非 alpha）
# 方向依据（M3.2 真实诊断 evidence：user-35f04c14634d.json sub_factor_decomposition）：
#   OOS 2024-07~2025-12 单因子 IC：low_vol +0.070（唯一正）/ trend60 -0.091 /
#   residual_momentum_20_5 -0.055 / liquidity_20 -0.085 → 动量系在该窗口反转，负权重=买反转。
#   ⚠ 长多低波 OOS 组合仍亏（IC 正但成本+行情轮动吃掉）——模板是研究起点，不是可投结论。
STRATEGY_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "low_vol",
        "name": "低波优先",
        "direction": "低波正权重=买低波动（因子已取相反数）",
        "note": "OOS 单因子 IC +0.070/IR+0.34 为唯一显著正方向。liquidity +0.25 是可交易性倾斜（非 alpha）。⚠ 长多低波 OOS 组合 -10.94%（IC 正但成本+924 行情轮动跑输），非方向错误。",
        "spec": {
            "name": "模板·低波优先",
            "holding_days": 5, "top_n": 30, "one_way_cost_bps": 10, "risk_rule": "none",
            "factors": {
                "trend60": 0.0, "residual_momentum_20_5": 0.0,
                "low_volatility_20": 1.0, "price_volume_confirmation": 0.0, "liquidity_20": 0.25,
            },
        },
    },
    {
        "id": "momentum",
        "name": "动量趋势",
        "direction": "正权重=买强趋势/强动量",
        "note": "⚠ OOS 动量系 IC 全负（trend -0.091 / rm -0.055）——该方向在 2024-07~2025-12 是逆势假设，正是 M3.2 默认组合反转的元凶。保留为对照模板。",
        "spec": {
            "name": "模板·动量趋势",
            "holding_days": 5, "top_n": 30, "one_way_cost_bps": 10, "risk_rule": "none",
            "factors": {
                "trend60": 1.0, "residual_momentum_20_5": 1.0,
                "low_volatility_20": 0.0, "price_volume_confirmation": 0.0, "liquidity_20": 0.25,
            },
        },
    },
    {
        "id": "reversal",
        "name": "反转",
        "direction": "负权重=买短期输家（fade 动量）",
        "note": "OOS 动量 IC 负 → 反向权重转正预期。⚠ 仅中短期反转假设；M3.1 对账里 momentum h10 OOS +19.99% 正是反转方向的赢家。",
        "spec": {
            "name": "模板·反转",
            "holding_days": 5, "top_n": 30, "one_way_cost_bps": 10, "risk_rule": "none",
            "factors": {
                "trend60": -0.5, "residual_momentum_20_5": -1.0,
                "low_volatility_20": 0.0, "price_volume_confirmation": 0.0, "liquidity_20": 0.25,
            },
        },
    },
    {
        "id": "low_vol_reversal",
        "name": "低波+反转",
        "direction": "低波正权重 + 反转负权重",
        "note": "组合两条正预期方向（M3.2 证据）：低波 IC +0.070 正 + fade 反转（OOS 动量 IC 负）。研究假设模板。",
        "spec": {
            "name": "模板·低波+反转",
            "holding_days": 5, "top_n": 30, "one_way_cost_bps": 10, "risk_rule": "none",
            "factors": {
                "trend60": 0.0, "residual_momentum_20_5": -0.5,
                "low_volatility_20": 1.0, "price_volume_confirmation": 0.0, "liquidity_20": 0.25,
            },
        },
    },
    {
        "id": "price_volume",
        "name": "量价确认",
        "direction": "正权重=趋势+量价确认同向",
        "note": "价量因子在默认组合中权重为 0，无独立 OOS 证据，纯研究模板。",
        "spec": {
            "name": "模板·量价确认",
            "holding_days": 5, "top_n": 30, "one_way_cost_bps": 10, "risk_rule": "none",
            "factors": {
                "trend60": 0.7, "residual_momentum_20_5": 0.0,
                "low_volatility_20": 0.0, "price_volume_confirmation": 0.7, "liquidity_20": 0.5,
            },
        },
    },
    {
        "id": "baseline",
        "name": "均衡对照（已知反转）",
        "direction": "默认等权基线，方向 +1",
        "note": "⚠ 等权对照：M3.2 已证明该组合 OOS 系统性反向（IS 单调 -1.0 / OOS -16.21% vs 上证超额 -37.5pp / 滚动WF 0/4 折正）。保留用于横向对比，勿当可投。",
        "spec": {
            "name": "模板·均衡对照",
            "holding_days": 5, "top_n": 30, "one_way_cost_bps": 10, "risk_rule": "none",
            "factors": {
                "trend60": 1.0, "residual_momentum_20_5": 1.0,
                "low_volatility_20": 1.0, "price_volume_confirmation": 0.0, "liquidity_20": 0.25,
            },
        },
    },
    # ── M4.1 A股实证因子族模板（2026-08-15 Web 研究新增）──────────
    # 方向编码：因子值高=好票（见 FACTOR_CATALOG / compose.py REBUILDERS），
    # 正权重=买该方向。实证依据：research/2026-08-15_quant_research_strategy.md。
    # 全模板诚实标注：⚠ 均研究假设；OOS 窗 12-24 个月噪声级；PIT 数据窗限制。
    {
        "id": "reversal_short",
        "name": "短期反转",
        "direction": "短期反转正权重=买近期输家（A股周频反转最强）",
        "note": "A股短期反转实证最强（国信 UMR IC 0.114/ICIR 5.04；太平洋 1 月反转多空年化 18.7%）。⚠ 高换手成本敏感——持有5日反转换手高，1%单边成本下净利差收窄；下跌市反转更强。研究假设。",
        "spec": {
            "name": "模板·短期反转",
            "holding_days": 5, "top_n": 30, "one_way_cost_bps": 10, "risk_rule": "none",
            "factors": {
                "short_term_reversal_5": 1.0, "medium_term_reversal_25_5": 0.5,
                "liquidity_20": 0.25,
            },
        },
    },
    {
        "id": "low_vol_reversal_short",
        "name": "低波+短期反转",
        "direction": "低波正权重 + 短期反转正权重（防守+攻）",
        "note": "组合两条 A股正预期方向：低波 OOS IC +0.070 唯一显著正（M3.2 证据）+ 短期反转实证最强。⚠ 两者风格低相关、攻守搭配；仍受换手成本与 OOS 噪声级约束。研究假设。",
        "spec": {
            "name": "模板·低波+短期反转",
            "holding_days": 5, "top_n": 30, "one_way_cost_bps": 10, "risk_rule": "none",
            "factors": {
                "low_volatility_20": 1.0, "short_term_reversal_5": 0.7,
                "liquidity_20": 0.25,
            },
        },
    },
    {
        "id": "small_cap_reversal",
        "name": "小市值+反转（卫星）",
        "direction": "小市值正权重 + 短期反转正权重",
        "note": "小市值溢价实证极强（万得微盘 2010-2024 年化 28.78%）但**极不稳定**：2017/2020 失效、2024-01 微盘 -40% 流动性危机（雪球敲入/DMA 强平）。⚠ 容量极小、尾部大，只能小仓卫星+严格风控，本质是风格 beta 非 alpha。研究假设，勿当主仓。",
        "spec": {
            "name": "模板·小市值+反转",
            "holding_days": 5, "top_n": 30, "one_way_cost_bps": 10, "risk_rule": "none",
            "factors": {
                "size_small": 1.0, "short_term_reversal_5": 0.7,
                "illiquidity_amihud_20": 0.3, "liquidity_20": 0.25,
            },
        },
    },
    {
        "id": "defensive_low_turnover",
        "name": "低波+低换手防御",
        "direction": "低波正权重 + 低换手正权重（防御）",
        "note": "低波（OOS 唯一显著正 IC）+ 低换手溢价（换手与预期收益显著负相关）均为防守方向，天然低换手、成本友好。⚠ 换手率 2 月/9-10 月曾短暂反转。研究假设。",
        "spec": {
            "name": "模板·低波+低换手",
            "holding_days": 10, "top_n": 30, "one_way_cost_bps": 10, "risk_rule": "none",
            "factors": {
                "low_volatility_20": 1.0, "low_turnover_20": 0.5,
                "liquidity_20": 0.25,
            },
        },
    },
    {
        "id": "value_defensive",
        "name": "价值防御（研究假设）",
        "direction": "低波正权重 + 盈利收益率正权重",
        "note": "⚠ 价值 2019 后显著失效（A股国际化使 PB-ROE 相关上升），且 PIT 基本面仅 2021-09~2024-12（2025 全年 NaN）→ OOS 后段无值。纯研究假设，不可投证据为零，保留用于对照价值改造方向。",
        "spec": {
            "name": "模板·价值防御",
            "holding_days": 10, "top_n": 30, "one_way_cost_bps": 10, "risk_rule": "none",
            "factors": {
                "low_volatility_20": 1.0, "value_earnings_yield": 0.5,
                "low_turnover_20": 0.3, "liquidity_20": 0.25,
            },
        },
    },
    {
        "id": "smile_rev_low_turnover",
        "name": "小市值+反转+低换手",
        "direction": "小市值正权重 + 短期反转正权重 + 低换手正权重",
        "note": "M8/≥20% 达标组合（research/2026-08-16_fullcycle_beat_shanghai.md）：全周期 2021-2025 年化 hold10/top30/500万 = +14.48%，hold20/top10/500万 上探 +21.42%（超额上证 +18.64pp，Sharpe 0.90）——唯一过 20% 网格组合。⚠ 诚实边界：需 ≥200 万资金（20万 100股整手退化空仓）；OOS 闸门仍 NO-GO（t=0.46<2）；MDD -36.2%。研究候选，非部署证据。",
        "spec": {
            "name": "模板·小市值+反转+低换手",
            "holding_days": 20, "top_n": 10, "one_way_cost_bps": 10, "risk_rule": "none",
            "account_capital": 5_000_000,
            "factors": {
                "size_small": 1.0, "short_term_reversal_5": 1.0,
                "low_turnover_20": 1.0,
            },
        },
    },
    {
        "id": "low_turnover_200k",
        "name": "低换手·20万研究",
        "direction": "低换手正权重（换手越低越好）",
        "note": "M9续（research/2026-08-17_losers_capital_sweep.md）：20万 网格 108 格实测**唯一稳健赚钱路线 = 低换手 + hold20 + 只买 10 只以内**（20万 下交易成本占资金比重大，高换手 h5 全灭 -16%~-18%，hold10/top30 也亏 -7%）。本模板全周期 2021-2025 **+9.46%**（上证+2.78%，超额 +6.69pp，Sharpe 0.72，MDD -13.4%），4/5 年跑赢上证（2025 跑输 +18.3% 暴涨年）；邻格 n5/n20/n30 全正（+9.4/+7.1/+5.9%）。⚠ 诚实边界：20万 天花板实测 ~9.5%，**不是 20%**（20%+ 需 500万 堆资金）；OOS 科学闸门未过（研究候选，非部署证据）；单因子依赖低换手因子。",
        "spec": {
            "name": "模板·低换手20万",
            "holding_days": 20, "top_n": 10, "one_way_cost_bps": 10, "risk_rule": "none",
            "account_capital": 200_000,
            "factors": {
                "low_turnover_20": 1.0,
            },
        },
    },
]
SPLITS = {
    "development": (
        factor_config.DISCOVERY_START, factor_config.DISCOVERY_END,
    ),
    "validation": (
        factor_config.VALIDATION_START, factor_config.VALIDATION_END,
    ),
    "test": (
        factor_config.HISTORICAL_TEST_START, factor_config.HISTORICAL_TEST_END,
    ),
    "diagnostic_2026": ("2026-01-01", "2026-12-31"),
}


# ═══ 因子驱动候选策略：注册表因子 → 选股组合 → 统一科学回测 ═══
_FACTOR_BATCH_PROFILES = ((10, 20), (20, 10))  # (holding_days, top_n)
BATCH_RESEARCH_CAPITAL = 200_000.0
BATCH_ONE_WAY_COST_BPS = 10
ONE_WAY_COST_BPS_ROLE = "slippage_only"
ADDITIONAL_TRADING_COSTS = "commission_stamp_tax_transfer_fee"
_BATCH_ACTIVE = threading.Event()
BATCH_STATE_PATH = ROOT / "research" / "strategy_batch_state.json"
BATCH_RESULTS_PATH = ROOT / "research" / "strategy_batch_results.json"
BATCH_FDR_METHOD = "BY"
BATCH_FDR_METHOD_VERSION = "quantqc.stats.by_fdr:v1"
BATCH_P_VALUE_METHOD = "non_overlapping_phase_max_hac"
BATCH_FAMILY_SCHEMA_VERSION = 2
BATCH_DECISION_SCHEMA_VERSION = 1
BATCH_STATE_SCHEMA_VERSION = 3


def _strategy_factor_pool(limit: int | None = 6) -> list[dict[str, Any]]:
    """从当前严格研究 epoch 选取候选，供策略组合再验证。

    终局 NO-GO/失效/冻结因子不得进入新组合；其旧结果仅供审计。其余候选的选择、
    排序和方向只用发现/验证期证据，不读取或要求历史确认期回测证据，避免同一历史
    区间既选模又验模。注册表与配方文件的 epoch 必须一致，任一缺失都 fail-closed。
    """
    from factor_system import config as fconfig
    from factor_system import registry as freg
    from factor_system.search import _canonical_tree, _tree_key

    try:
        c = freg._conn(readonly=True)
        rows = [dict(r) for r in c.execute(
            """SELECT factor_id,name,family,formula,status,is_ic_mean,is_ic_ir,
                      research_epoch,sealed_at,sealed_data_end,sealed_recipe_sha256,
                      research_record_json,research_record_sha256
                 FROM factors
                WHERE is_ic_mean IS NOT NULL AND is_ic_ir IS NOT NULL
                  AND research_epoch = ?
                  AND status IN ('candidate','validated','registered','backtesting',
                                 'historical_pass','production')""",
            (fconfig.RESEARCH_EPOCH,),
        )]
        c.close()
    except Exception:
        return []
    if not rows:
        return []
    rows = [r for r in rows
            if fconfig.REGISTRY_DIR.joinpath("recipes", f"{r['factor_id']}.json").is_file()]
    rows.sort(key=lambda r: (
        -abs(float(r.get("is_ic_ir") or 0.0)),
        -abs(float(r.get("is_ic_mean") or 0.0)),
        str(r.get("factor_id") or ""),
    ))
    meta = _full_factor_catalog()
    pool: list[dict[str, Any]] = []
    seen_recipes: set[str] = set()
    for row in rows:
        fid = row["factor_id"]
        path = fconfig.REGISTRY_DIR.joinpath("recipes", f"{fid}.json")
        try:
            raw = path.read_bytes()
            recipe = json.loads(raw)
        except Exception:
            continue
        if (not isinstance(recipe, dict)
                or any(not row.get(key) for key in (
                    "sealed_at", "sealed_data_end", "sealed_recipe_sha256",
                    "research_record_json", "research_record_sha256"))
                or hashlib.sha256(raw).hexdigest() != row["sealed_recipe_sha256"]
                or recipe.get("research_epoch") != fconfig.RESEARCH_EPOCH
                or recipe.get("sealed_at") != row["sealed_at"]
                or recipe.get("sealed_data_end") != row["sealed_data_end"]
                or recipe.get("research_record_sha256")
                != row["research_record_sha256"]):
            continue
        try:
            freg.verify_frozen_source_record(row, recipe)
        except Exception:
            continue
        nodes = recipe.get("nodes") if isinstance(recipe, dict) else None
        canonical = _canonical_tree(nodes) if nodes else None
        recipe_key = (_tree_key(canonical) if canonical else json.dumps({
            "kind": recipe.get("kind"), "formula": recipe.get("formula"),
            "template_id": recipe.get("template_id"), "params": recipe.get("params"),
            "components": recipe.get("components"),
        }, sort_keys=True, ensure_ascii=False, default=str))
        if not recipe_key or recipe_key in seen_recipes:
            continue
        seen_recipes.add(recipe_key)
        pool.append({
            "factor_id": fid,
            "label": (meta.get(fid) or {}).get("label") or row.get("name") or fid,
            "status": row.get("status"),
            "weight": 1.0 if float(row["is_ic_mean"]) >= 0 else -1.0,
            "is_ic_mean": row.get("is_ic_mean"),
            "is_ic_ir": row.get("is_ic_ir"),
            "sealed_at": row["sealed_at"],
            "sealed_data_end": row["sealed_data_end"],
            "sealed_recipe_sha256": row["sealed_recipe_sha256"],
        })
        if limit is not None and len(pool) >= limit:
            break
    return pool


def _batch_grid() -> list[dict[str, Any]]:
    """真实因子驱动网格：单因子选股 + 相关感知多因子组合，再跑统一回测。"""
    pool = _strategy_factor_pool()
    if not pool:
        return []
    grid: list[dict[str, Any]] = []
    for factor in pool:
        for hold, top_n in _FACTOR_BATCH_PROFILES:
            grid.append({
                "name": _batch_candidate_name([factor["factor_id"]], hold, top_n),
                "holding_days": hold, "top_n": top_n,
                "one_way_cost_bps": BATCH_ONE_WAY_COST_BPS, "risk_rule": "none",
                "rebal_buffer": _configured_rebal_buffer(),
                "entry_rule": "none",
                "exposure_rule": "none",
                "account_capital": BATCH_RESEARCH_CAPITAL,
                "factors": {factor["factor_id"]: factor["weight"]},
                "compose_method": "rank_weighted_v1",
            })
    # 前缀组合 2..N：仅用发现期估计相关感知 IC 权重，后续窗口只负责检验。
    for size in range(2, len(pool) + 1):
        chosen = pool[:size]
        for hold, top_n in _FACTOR_BATCH_PROFILES:
            grid.append({
                "name": _batch_candidate_name(
                    [x["factor_id"] for x in chosen], hold, top_n),
                "holding_days": hold, "top_n": top_n,
                "one_way_cost_bps": BATCH_ONE_WAY_COST_BPS, "risk_rule": "none",
                "rebal_buffer": _configured_rebal_buffer(),
                "entry_rule": "none",
                "exposure_rule": "none",
                "account_capital": BATCH_RESEARCH_CAPITAL,
                "factors": {x["factor_id"]: 1.0 for x in chosen},
                "compose_method": "corr_ic_weighted",
            })
    return grid


def _batch_candidate_name(factor_ids: list[str], hold: int, top_n: int) -> str:
    """按候选定义生成稳定短名称，避免展示标签变化或过长导致注册失败。"""
    ids = sorted(str(fid) for fid in factor_ids)
    digest = hashlib.sha256(
        json.dumps(ids, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:10]
    subject = ids[0][:24] if len(ids) == 1 else f"{len(ids)}因子"
    return f"因子{'驱动' if len(ids) == 1 else '组合'}·{subject}·h{hold}·n{top_n}·{digest}"


def _batch_hypothesis_id(spec: dict[str, Any]) -> str:
    """不含展示名的稳定策略假设 ID；同规格重跑不会重复消费 alpha。"""
    frozen = {
        key: spec.get(key) for key in (
            "holding_days", "top_n", "one_way_cost_bps", "account_capital",
            "risk_rule", "factors", "compose_method",
        )
    }
    frozen["rebal_buffer"] = int(spec.get(
        "rebal_buffer", _configured_rebal_buffer(),
    ))
    frozen["entry_rule"] = str(spec.get("entry_rule") or "none")
    frozen["exposure_rule"] = str(spec.get("exposure_rule") or "none")
    digest = hashlib.sha256(
        json.dumps(frozen, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:24]
    return f"strategy::{digest}"


def _batch_strategy_id(spec: dict[str, Any]) -> str:
    """Stable saved-strategy identity makes promotion recovery idempotent."""
    suffix = _batch_hypothesis_id(spec).rsplit("::", 1)[-1]
    return f"{STRATEGY_ID_PREFIX}{suffix[:12]}"


def _save_batch_strategy(spec: dict[str, Any]) -> dict[str, Any]:
    from factor_system import config as fconfig

    normalized = validate_spec(spec)
    strategy_id = _batch_strategy_id(normalized)
    expected_spec_sha256 = result_spec_sha256(normalized)
    path = STRATEGIES_DIR / f"{strategy_id}.json"
    if path.is_file():
        saved = _read_json(path)
        if (saved.get("research_epoch") != fconfig.RESEARCH_EPOCH
                or (saved.get("result_spec_sha256") or result_spec_sha256(
                    saved.get("spec") or {},
                )) != expected_spec_sha256):
            raise RuntimeError("批次稳定 strategy_id 已被不同 epoch/规格占用")
        return saved
    return save_strategy({**normalized, "strategy_id": strategy_id})


def _batch_test_context() -> dict[str, Any]:
    """Return the frozen statistical/data context shared by the batch family."""
    from factor_system import config as fconfig
    from factor_system.gate import research_input_key

    return {
        "research_epoch": str(fconfig.RESEARCH_EPOCH),
        "historical_confirmation_epoch": (
            f"{fconfig.RESEARCH_EPOCH}:historical-confirmation"
        ),
        "panel_data_key": research_input_key(),
        "fdr_q": float(fconfig.FDR_Q),
        "fdr_method": BATCH_FDR_METHOD,
        "fdr_method_version": BATCH_FDR_METHOD_VERSION,
        "p_value_method": BATCH_P_VALUE_METHOD,
    }


def _batch_latch_context(
    family: dict[str, Any], index: int, spec: dict[str, Any],
) -> dict[str, Any]:
    return {
        "family_sha256": family["sha256"],
        "family_index": int(index),
        "research_epoch": family["payload"]["research_epoch"],
        "research_input_key": family["payload"]["panel_data_key"],
        "hypothesis_id": _batch_hypothesis_id(spec),
    }


def _manual_strategy_latch_context(spec: dict[str, Any]) -> dict[str, Any]:
    hypothesis_id = _batch_hypothesis_id(spec)
    spec_sha256 = hashlib.sha256(json.dumps(
        spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()
    return {
        "producer": "strategy_manual",
        "hypothesis_id": hypothesis_id,
        "spec_sha256": spec_sha256,
    }


def _begin_batch_online_observation(
    spec: dict[str, Any], *, family: dict[str, Any] | None = None,
    family_index: int | None = None,
) -> dict[str, Any]:
    """Durably reserve this hypothesis' global arrival order before evaluation."""
    from factor_system import registry as freg

    test_context = family["payload"] if family is not None else _batch_test_context()
    producer_type = "strategy_batch" if family is not None else "strategy_manual"
    latch_context = (
        _batch_latch_context(family, int(family_index), spec)
        if family is not None and family_index is not None
        else _manual_strategy_latch_context(spec)
    )
    freg.migrate_schema()
    c = freg._conn()
    try:
        observation = freg.begin_factor_observation(
            c,
            _batch_hypothesis_id(spec),
            epoch=test_context["historical_confirmation_epoch"],
            q=test_context["fdr_q"],
            p_value_method=test_context["p_value_method"],
            data_key=test_context["panel_data_key"],
            producer_type=producer_type,
            context=latch_context,
        )
        c.commit()
        return observation
    finally:
        c.close()


def _record_batch_online_tests(results: list[dict[str, Any]],
                               grid: list[dict[str, Any]], *,
                               family: dict[str, Any] | None = None,
                               family_start_index: int = 0) -> None:
    """把策略批次接入与因子共享的跨批历史确认 LOND 账本。"""
    from factor_system import registry as freg

    freg.migrate_schema()
    c = freg._conn()
    context = family["payload"] if family is not None else _batch_test_context()
    epoch = context["historical_confirmation_epoch"]
    data_key = context["panel_data_key"]
    try:
        for offset, (row, spec) in enumerate(zip(results, grid)):
            hypothesis_id = _batch_hypothesis_id(spec)
            p_value_method = row.get("p_value_method")
            if row.get("verdict") == "ERROR" and not p_value_method:
                # evaluate 可能在内部算出 p 后才抛错；无法证明未观察时，保守地
                # 以同一冻结统计口径消费 p=1，绝不让后续候选抢占更早 alpha。
                row["fdr_p_value"] = 1.0
                row["is_validation_passed"] = False
                row["gate_passed"] = False
                row["p_value_method"] = BATCH_P_VALUE_METHOD
                row["p_value_source"] = "conservative_evaluation_error_default"
                p_value_method = row["p_value_method"]
            if not p_value_method:
                raise RuntimeError("全局在线检验缺少 gate p-value 口径")
            if str(p_value_method) != context["p_value_method"]:
                raise RuntimeError("全局在线检验 p-value 口径与冻结批次不一致")
            try:
                test = freg.record_factor_test(
                    c, hypothesis_id, epoch=epoch,
                    q=float(context["fdr_q"]),
                    p_value=row.get("fdr_p_value"),
                    gate_passed=bool(row.get("is_validation_passed")
                                     and row.get("gate_passed")),
                    p_value_method=str(p_value_method),
                    data_key=data_key,
                    require_observation=True,
                    producer_type=(
                        "strategy_batch" if family is not None else "strategy_manual"
                    ),
                    context=(
                        _batch_latch_context(
                            family, family_start_index + offset, spec,
                        ) if family is not None
                        else _manual_strategy_latch_context(spec)
                    ),
                )
                # 每条策略的 p 值一旦计算完成，就先独立持久化其全局序号；
                # 后续进度文件、BY 汇总或证据 I/O 崩溃都不得回滚已观察假设。
                c.commit()
            except Exception as exc:
                raise RuntimeError(f"全局在线检验记录失败: {exc}") from exc
            row.update({
                "global_fdr_method": "LOND-dependent",
                "global_fdr_epoch": epoch,
                "global_test_id": int(test["test_id"]),
                "global_hypothesis_id": hypothesis_id,
                "global_hypothesis_order": int(test["hypothesis_order"]),
                "global_alpha_i": float(test["alpha_i"]),
                "global_lond_passed": bool(test["online_reject"]),
                "global_tested_at": test["tested_at"],
                "global_p_value_method": test["p_value_method"],
                "global_data_key": test["data_key"],
                "global_evidence_path": test.get("evidence_path"),
                "global_evidence_sha256": test.get("evidence_sha256"),
            })
        c.commit()
    finally:
        c.close()


def record_strategy_historical_test(
    spec: dict[str, Any], evaluation: dict[str, Any],
) -> dict[str, Any]:
    """把手工统一诊断接入与批次/因子共享的历史确认 LOND 账本。"""
    evaluation_error = evaluation.get("evaluation_error") if isinstance(
        evaluation, dict) else None
    is_validation = evaluation.get("is_validation") or {}
    gate = evaluation.get("gate") or {}
    raw_p = (gate.get("oos_ic") or {}).get("p_value")
    try:
        p_value = float(raw_p)
    except (TypeError, ValueError):
        p_value = 1.0
    if not math.isfinite(p_value) or not 0 <= p_value <= 1:
        p_value = 1.0
    row: dict[str, Any] = {
        "verdict": "PENDING",
        "passed": False,
        "historical_confirmation_passed": False,
        "is_validation_passed": bool(is_validation.get("passed")),
        "gate_passed": bool(gate.get(
            "historical_confirmation_passed", gate.get("passed"))),
        "gate_verdict": gate.get("verdict") or "NO-GO",
        "gate_reason": gate.get("reason") or "",
        "oos_ic_p_value": p_value,
        "fdr_p_value": p_value,
        "p_value_method": (gate.get("oos_ic") or {}).get("p_value_method"),
        "batch_fdr_passed": None,
        "fdr_method": "manual_single_hypothesis_not_a_batch_family",
        "fdr_family_size": 1,
    }
    if evaluation_error:
        row.update({
            "verdict": "ERROR",
            "reason": str(evaluation_error),
            "error": str(evaluation_error),
            "fdr_p_value": 1.0,
            "p_value_method": "non_overlapping_phase_max_hac",
            "p_value_source": "conservative_evaluation_error_default",
            "is_validation_passed": False,
            "gate_passed": False,
        })
    _record_batch_online_tests([row], [spec])
    _persist_batch_test_evidence(row, spec, evaluation)
    if evaluation_error:
        return row
    row["manual_gate_and_lond_passed"] = bool(
        row["is_validation_passed"] and row["gate_passed"]
        and row.get("global_lond_passed")
    )
    # 手工规格不是预先冻结的 batch family，不能补做/绕过 batch BY-FDR。
    # 即使 gate+LOND 通过，也只允许探索性结论；HISTORICAL_PASS 只由固定批次产生。
    row["historical_confirmation_passed"] = False
    row["verdict"] = (
        "EXPLORATORY" if row["manual_gate_and_lond_passed"] else "NO-GO"
    )
    if row["manual_gate_and_lond_passed"]:
        row["reason"] = "手工诊断仅供探索；需进入固定批次并通过 BY-FDR 才可历史确认"
    else:
        row["reason"] = (
            row.get("gate_reason") if not row["gate_passed"]
            else "共享跨批 LOND 未通过"
        )
    return row


def _persist_batch_test_evidence(row: dict[str, Any], spec: dict[str, Any],
                                 evaluation: dict[str, Any] | None) -> str:
    """冻结每个已消费全局 alpha 的假设证据，包括 NO-GO 候选。"""
    from factor_system import registry as freg

    test_id = int(row["global_test_id"])
    c = freg._conn()
    try:
        test = c.execute(
            "SELECT * FROM factor_tests WHERE test_id=?", (test_id,),
        ).fetchone()
        if test is None:
            raise RuntimeError(f"全局检验不存在: {test_id}")
        test = dict(test)
        if test.get("evidence_path") and test.get("evidence_sha256"):
            path = ROOT / test["evidence_path"]
            if not path.is_file() or _sha256(path) != test["evidence_sha256"]:
                raise RuntimeError("已冻结策略证据缺失或哈希不符")
            row["global_evidence_path"] = test["evidence_path"]
            row["global_evidence_sha256"] = test["evidence_sha256"]
            row["global_evidence_ready"] = True
            return test["evidence_path"]

        epoch_dir = str(row["global_fdr_epoch"]).replace(":", "_")
        hypothesis_id = str(row["global_hypothesis_id"])
        hypothesis_suffix = hypothesis_id.rsplit("::", 1)[-1][:24]
        path = (ROOT / "research" / "strategy_diagnostics" / "batch" / epoch_dir
                / f"{test_id:06d}_{hypothesis_suffix}.json")
        test_snapshot = {
            key: row.get(key) for key in (
                "fdr_p_value", "p_value_method", "is_validation_passed",
                "gate_passed", "gate_verdict", "gate_reason",
                "p_value_source", "error",
                "batch_fdr_passed", "fdr_q", "fdr_method",
                "fdr_effective_q", "fdr_family_size", "global_fdr_method",
                "global_fdr_epoch", "global_test_id", "global_hypothesis_id",
                "global_hypothesis_order", "global_alpha_i",
                "global_lond_passed", "global_tested_at",
                "global_p_value_method", "global_data_key",
            )
        }
        evidence_payload = {
            "schema_version": 2,
            "evidence_stage": "historical_confirmation_global_test",
            "hypothesis_id": hypothesis_id,
            "test_id": test_id,
            "research_epoch": row["global_fdr_epoch"],
            "tested_at": row.get("global_tested_at"),
            "result_spec_sha256": result_spec_sha256(spec),
            "spec": spec,
            "test": test_snapshot,
            "evaluation": evaluation,
        }
        if path.is_file():
            # O_EXCL 文件写入与 SQLite attach 不能跨文件系统做同一事务。若进程恰在
            # 二者之间退出，重试时只核对冻结身份/统计字段后接回原孤儿文件；评估报告
            # 中的 evaluated_at 等易变字段不参与恢复判断，也绝不覆盖原文件。
            orphan = _read_json(path)
            orphan_test = orphan.get("test") if isinstance(orphan, dict) else None
            expected = {
                "fdr_p_value": float(test["p_value"]),
                "global_fdr_epoch": str(test["epoch"]),
                "global_test_id": int(test["test_id"]),
                "global_hypothesis_id": str(test["factor_id"]),
                "global_hypothesis_order": int(test["hypothesis_order"]),
                "global_alpha_i": float(test["alpha_i"]),
                "global_lond_passed": bool(test["online_reject"]),
                "global_tested_at": str(test["tested_at"]),
                "global_p_value_method": str(test["p_value_method"]),
                "global_data_key": str(test["data_key"]),
            }
            valid_orphan = (
                orphan.get("schema_version") == 2
                and orphan.get("evidence_stage") == "historical_confirmation_global_test"
                and orphan.get("hypothesis_id") == hypothesis_id
                and orphan.get("test_id") == test_id
                and orphan.get("research_epoch") == row["global_fdr_epoch"]
                and orphan.get("result_spec_sha256") == result_spec_sha256(spec)
                and isinstance(orphan_test, dict)
                and bool(orphan_test.get("is_validation_passed")
                         and orphan_test.get("gate_passed"))
                    == bool(test["gate_passed"])
                and all(orphan_test.get(key) == value
                        for key, value in expected.items())
            )
            if not valid_orphan:
                raise RuntimeError("孤儿全局检验证据与冻结账本不一致")
        else:
            _immutable_json(path, evidence_payload)
        rel = str(path.relative_to(ROOT))
        sha = _sha256(path)
        freg.attach_factor_test_evidence(
            c, test_id, evidence_path=rel, evidence_sha256=sha,
        )
        c.commit()
        row["global_evidence_path"] = rel
        row["global_evidence_sha256"] = sha
        row["global_evidence_ready"] = True
        return rel
    finally:
        c.close()


def _persist_all_batch_test_evidence(
    results: list[dict[str, Any]], grid: list[dict[str, Any]],
    evaluations: list[dict[str, Any] | None],
) -> None:
    """逐条留存全局检验证据；单条失败只污染自身并允许下次重试。"""
    for row, spec, evaluation in zip(results, grid, evaluations):
        if not row.get("global_test_id"):
            continue
        try:
            _persist_batch_test_evidence(row, spec, evaluation)
        except Exception as exc:
            row.update({
                "passed": False,
                "historical_confirmation_passed": False,
                "verdict": "ERROR",
                "global_evidence_ready": False,
                "reason": f"全局在线检验证据冻结失败: {exc}",
                "error": f"全局在线检验证据冻结失败: {exc}",
            })


def _canonical_json_value(value: Any) -> Any:
    """Return a JSON-only value with deterministic object-key ordering."""
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )
    return json.loads(encoded)


def _canonical_payload_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _batch_artifact_ref(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path.resolve())


def _resolve_batch_artifact(ref: str) -> Path:
    path = Path(str(ref))
    return path if path.is_absolute() else ROOT / path


def _write_content_addressed_json(directory: Path, payload: dict[str, Any]) -> tuple[Path, str]:
    """Write canonical immutable JSON whose filename is its exact byte SHA-256."""
    canonical = _canonical_json_value(payload)
    encoded = (
        json.dumps(canonical, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    sha256 = hashlib.sha256(encoded).hexdigest()
    path = directory / f"{sha256}.json"
    directory.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        if path.read_bytes() != encoded:
            raise RuntimeError(f"内容寻址证据文件已存在但字节不匹配: {path}")
    else:
        temporary = directory / f".{sha256}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != encoded:
                    raise RuntimeError(f"内容寻址证据并发写入不一致: {path}")
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    if _sha256(path) != sha256:
        raise RuntimeError(f"不可变证据哈希校验失败: {path}")
    return path, sha256


def _canonical_batch_grid(
    grid: list[dict[str, Any]], *, allow_empty: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(grid, list) or (not grid and not allow_empty):
        raise RuntimeError("策略批次 family 不能为空")
    canonical = _canonical_json_value(grid)
    if not all(isinstance(spec, dict) for spec in canonical):
        raise RuntimeError("策略批次 family 含非对象候选")
    hypothesis_ids = [_batch_hypothesis_id(spec) for spec in canonical]
    if len(hypothesis_ids) != len(set(hypothesis_ids)):
        raise RuntimeError("策略批次 family 含重复假设")
    return canonical


def _existing_final_batch_decisions(
    context: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Read immutable final batch decisions that lock a hypothesis' first BY vote."""
    directory = BATCH_STATE_PATH.parent / "strategy_batch_decisions"
    if not directory.is_dir():
        return {}
    locked: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(directory.glob("*.json")):
        evidence_sha256 = path.stem
        if (len(evidence_sha256) != 64
                or _sha256(path) != evidence_sha256):
            raise RuntimeError("既有 batch 决策证据文件名或哈希不符")
        payload = _read_json(path)
        if (payload.get("schema_version") != BATCH_DECISION_SCHEMA_VERSION
                or payload.get("evidence_stage")
                != "strategy_batch_by_lond_decisions"):
            raise RuntimeError("既有 batch 决策证据格式错误")
        if payload.get("historical_confirmation_epoch") != context[
            "historical_confirmation_epoch"
        ]:
            continue
        family_sha256 = str(payload.get("family_sha256") or "")
        family_path = (
            BATCH_STATE_PATH.parent / "strategy_batch_families"
            / f"{family_sha256}.json"
        )
        if (len(family_sha256) != 64 or not family_path.is_file()
                or _sha256(family_path) != family_sha256):
            raise RuntimeError("既有 batch 决策缺少匹配的不可变 family")
        family_payload = _read_json(family_path)
        decisions = payload.get("decisions")
        family_grid = family_payload.get("grid")
        if (not isinstance(decisions, list) or not isinstance(family_grid, list)
                or payload.get("family_size") != len(decisions)
                or len(decisions) != len(family_grid)
                or family_payload.get("hypothesis_ids") != [
                    _batch_hypothesis_id(spec) for spec in family_grid
                ]
                or family_payload.get("historical_confirmation_epoch")
                != payload.get("historical_confirmation_epoch")
                or family_payload.get("panel_data_key")
                != payload.get("panel_data_key")):
            raise RuntimeError("既有 batch 决策与不可变 family 不一致")
        for index, (decision, spec) in enumerate(zip(decisions, family_grid)):
            if not isinstance(decision, dict):
                raise RuntimeError("既有 batch 决策行格式错误")
            row_sha256 = str(decision.get("decision_row_sha256") or "")
            core = dict(decision)
            core.pop("decision_row_sha256", None)
            hypothesis_id = _batch_hypothesis_id(spec)
            if (decision.get("index") != index
                    or decision.get("family_sha256") != family_sha256
                    or decision.get("hypothesis_id") != hypothesis_id
                    or decision.get("result_spec_sha256")
                    != result_spec_sha256(spec)
                    or not isinstance(decision.get("by_reject"), bool)
                    or row_sha256 != _canonical_payload_sha256(core)):
                raise RuntimeError("既有 batch 决策行身份或哈希不一致")
            locked.setdefault(hypothesis_id, []).append({
                "family_sha256": family_sha256,
                "decision_evidence_path": _batch_artifact_ref(path),
                "decision_evidence_sha256": evidence_sha256,
                "decision_index": index,
                "decision_row_sha256": row_sha256,
                "by_reject": bool(decision["by_reject"]),
                "global_lond_reject": bool(decision["global_lond_reject"]),
                "global_test_id": int(decision["global_test_id"]),
                "global_hypothesis_order": int(
                    decision["global_hypothesis_order"]
                ),
            })
    for decisions in locked.values():
        decisions.sort(key=lambda row: (
            row["global_hypothesis_order"], row["global_test_id"],
            row["family_sha256"], row["decision_evidence_sha256"],
        ))
    return locked


def _exclude_incompatible_existing_strategy_tests(
    grid: list[dict[str, Any]], context: dict[str, Any],
) -> tuple[
    list[dict[str, Any]], list[int], list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Exclude immutable same-epoch tests whose input identity cannot be reused."""
    from factor_system import registry as freg

    freg.migrate_schema()
    c = freg._conn(readonly=True)
    eligible: list[dict[str, Any]] = []
    eligible_indices: list[int] = []
    excluded: list[dict[str, Any]] = []
    decision_excluded: list[dict[str, Any]] = []
    try:
        pending = freg.pending_factor_observation(
            c, context["historical_confirmation_epoch"],
        )
        if pending is not None:
            raise RuntimeError(
                "共享历史确认流仍有未完成观察，须先由原生产者或 daemon 保守终结: "
                f"{pending['factor_id']}"
            )
        final_batch_decisions = _existing_final_batch_decisions(context)
        for index, spec in enumerate(grid):
            hypothesis_id = _batch_hypothesis_id(spec)
            prior_decisions = final_batch_decisions.get(hypothesis_id) or []
            if prior_decisions:
                decision_excluded.append({
                    "planned_index": index,
                    "hypothesis_id": hypothesis_id,
                    "result_spec_sha256": result_spec_sha256(spec),
                    "reason": "existing_final_batch_decision",
                    "prior_final_batch_decisions": prior_decisions,
                })
                continue
            test = c.execute(
                "SELECT * FROM factor_tests WHERE epoch=? AND factor_id=?",
                (context["historical_confirmation_epoch"], hypothesis_id),
            ).fetchone()
            mismatch = bool(test is not None and (
                test["data_key"] != context["panel_data_key"]
                or test["p_value_method"] != context["p_value_method"]
            ))
            if not mismatch:
                eligible.append(spec)
                eligible_indices.append(index)
                continue
            excluded.append({
                "planned_index": index,
                "hypothesis_id": hypothesis_id,
                "result_spec_sha256": result_spec_sha256(spec),
                "reason": "existing_test_input_key_or_method_mismatch",
                "existing_test_id": int(test["test_id"]),
                "existing_data_key": test["data_key"],
                "existing_p_value_method": test["p_value_method"],
                "requested_data_key": context["panel_data_key"],
                "requested_p_value_method": context["p_value_method"],
            })
    finally:
        c.close()
    return eligible, eligible_indices, excluded, decision_excluded


def _freeze_batch_family(grid: list[dict[str, Any]]) -> dict[str, Any]:
    """Freeze the complete planned family before any candidate is evaluated."""
    planned_grid = _canonical_batch_grid(grid)
    context = _batch_test_context()
    canonical_grid, planned_indices, excluded, decision_excluded = (
        _exclude_incompatible_existing_strategy_tests(planned_grid, context)
    )
    hypothesis_ids = [_batch_hypothesis_id(spec) for spec in canonical_grid]
    payload = {
        "schema_version": BATCH_FAMILY_SCHEMA_VERSION,
        "evidence_stage": "strategy_batch_family",
        "producer_type": "strategy_batch",
        "latch_context_version": "family_sha256+family_index+hypothesis_id:v1",
        **context,
        "planned_family_size": len(planned_grid),
        "planned_grid": planned_grid,
        "family_size": len(canonical_grid),
        "grid_planned_indices": planned_indices,
        "hypothesis_ids": hypothesis_ids,
        "grid": canonical_grid,
        "excluded_existing_test_key_mismatch": excluded,
        "excluded_existing_final_batch_decision": decision_excluded,
    }
    path, sha256 = _write_content_addressed_json(
        BATCH_STATE_PATH.parent / "strategy_batch_families", payload,
    )
    return {
        "payload": _read_json(path),
        "path": path,
        "path_ref": _batch_artifact_ref(path),
        "sha256": sha256,
        "grid": canonical_grid,
        "context_drift": [],
        "conservative_close": False,
    }


def _family_state_fields(family: dict[str, Any]) -> dict[str, Any]:
    payload = family["payload"]
    return {
        "batch_family_evidence_path": family["path_ref"],
        "batch_family_sha256": family["sha256"],
        "batch_family_size": int(payload["family_size"]),
        "batch_family_planned_size": int(payload["planned_family_size"]),
        "excluded_existing_test_key_mismatch_count": len(
            payload.get("excluded_existing_test_key_mismatch") or []
        ),
        "excluded_existing_final_batch_decision_count": len(
            payload.get("excluded_existing_final_batch_decision") or []
        ),
        "batch_family_research_epoch": payload["research_epoch"],
        "batch_family_data_key": payload["panel_data_key"],
        "batch_family_fdr_method": payload["fdr_method"],
        "batch_family_fdr_method_version": payload["fdr_method_version"],
        "batch_family_context_drift": list(family.get("context_drift") or []),
    }


def _load_frozen_batch_family(state: dict[str, Any]) -> dict[str, Any]:
    """Load a pending family and fail closed on any context or hash drift."""
    sha256 = str(state.get("batch_family_sha256") or "")
    ref = str(state.get("batch_family_evidence_path") or "")
    if len(sha256) != 64 or not ref:
        raise RuntimeError("未完成批次缺少不可变 family 引用，拒绝重新生成候选族")
    expected = (
        BATCH_STATE_PATH.parent / "strategy_batch_families" / f"{sha256}.json"
    ).resolve()
    path = _resolve_batch_artifact(ref).resolve()
    if path != expected or not path.is_file() or _sha256(path) != sha256:
        raise RuntimeError("策略批次 family 文件缺失、路径越界或哈希不符")
    payload = _read_json(path)
    if not payload:
        raise RuntimeError("策略批次 family 文件不可解析")
    from factor_system import config as fconfig

    try:
        current_context = _batch_test_context()
        context_error = None
    except Exception as exc:
        current_context = {
            "research_epoch": str(fconfig.RESEARCH_EPOCH),
        }
        context_error = type(exc).__name__
    expected_identity = {
        "schema_version": BATCH_FAMILY_SCHEMA_VERSION,
        "evidence_stage": "strategy_batch_family",
        "producer_type": "strategy_batch",
        "latch_context_version": "family_sha256+family_index+hypothesis_id:v1",
        "research_epoch": current_context["research_epoch"],
    }
    for key, value in expected_identity.items():
        if payload.get(key) != value:
            raise RuntimeError(f"策略批次 family 上下文不匹配: {key}")
    drift_keys = [
        key for key in (
            "panel_data_key", "fdr_q", "fdr_method",
            "fdr_method_version", "p_value_method",
        ) if payload.get(key) != current_context.get(key)
    ]
    if context_error:
        drift_keys.append(f"current_context_unavailable:{context_error}")
    planned_grid = _canonical_batch_grid(payload.get("planned_grid"))
    grid = _canonical_batch_grid(payload.get("grid"), allow_empty=True)
    if payload.get("grid") != grid:
        raise RuntimeError("策略批次 family grid 不是 canonical JSON")
    hypothesis_ids = [_batch_hypothesis_id(spec) for spec in grid]
    if payload.get("family_size") != len(grid):
        raise RuntimeError("策略批次 family_size 不一致")
    if payload.get("hypothesis_ids") != hypothesis_ids:
        raise RuntimeError("策略批次 family 假设身份不一致")
    planned_indices = payload.get("grid_planned_indices")
    excluded = payload.get("excluded_existing_test_key_mismatch")
    decision_excluded = payload.get(
        "excluded_existing_final_batch_decision", [],
    )
    if (payload.get("planned_family_size") != len(planned_grid)
            or not isinstance(planned_indices, list)
            or len(planned_indices) != len(grid)
            or not isinstance(excluded, list)
            or not isinstance(decision_excluded, list)):
        raise RuntimeError("策略批次 planned family 元数据不一致")
    frozen_prior_decisions = (
        _existing_final_batch_decisions(payload) if decision_excluded else {}
    )
    covered: set[int] = set()
    for spec, planned_index in zip(grid, planned_indices):
        if (not isinstance(planned_index, int)
                or not 0 <= planned_index < len(planned_grid)
                or planned_grid[planned_index] != spec
                or planned_index in covered):
            raise RuntimeError("策略批次 eligible/planned 映射不一致")
        covered.add(planned_index)
    for item in decision_excluded:
        if not isinstance(item, dict):
            raise RuntimeError("策略批次既有决策 exclusion 证据格式错误")
        planned_index = item.get("planned_index")
        if (not isinstance(planned_index, int)
                or not 0 <= planned_index < len(planned_grid)
                or planned_index in covered):
            raise RuntimeError("策略批次既有决策 exclusion/planned 映射不一致")
        planned_spec = planned_grid[planned_index]
        prior_decisions = item.get("prior_final_batch_decisions")
        hypothesis_id = _batch_hypothesis_id(planned_spec)
        if (item.get("reason") != "existing_final_batch_decision"
                or item.get("hypothesis_id") != hypothesis_id
                or item.get("result_spec_sha256")
                != result_spec_sha256(planned_spec)
                or not isinstance(prior_decisions, list)
                or not prior_decisions
                or any(
                    not isinstance(decision, dict)
                    or len(str(decision.get("family_sha256") or "")) != 64
                    or len(str(decision.get(
                        "decision_evidence_sha256",
                    ) or "")) != 64
                    or not isinstance(decision.get("by_reject"), bool)
                    for decision in prior_decisions
                )
                or any(
                    decision not in frozen_prior_decisions.get(hypothesis_id, [])
                    for decision in prior_decisions
                )):
            raise RuntimeError("策略批次既有决策 exclusion 身份不一致")
        covered.add(planned_index)
    for item in excluded:
        if not isinstance(item, dict):
            raise RuntimeError("策略批次 exclusion 证据格式错误")
        planned_index = item.get("planned_index")
        if (not isinstance(planned_index, int)
                or not 0 <= planned_index < len(planned_grid)
                or planned_index in covered):
            raise RuntimeError("策略批次 exclusion/planned 映射不一致")
        planned_spec = planned_grid[planned_index]
        if (item.get("reason") != "existing_test_input_key_or_method_mismatch"
                or item.get("hypothesis_id") != _batch_hypothesis_id(planned_spec)
                or item.get("result_spec_sha256") != result_spec_sha256(planned_spec)):
            raise RuntimeError("策略批次 exclusion 身份不一致")
        covered.add(planned_index)
    if covered != set(range(len(planned_grid))):
        raise RuntimeError("策略批次 planned family 未完整分区")
    if state.get("total") not in (None, len(grid)):
        raise RuntimeError("策略批次状态 total 与冻结 family 不一致")
    return {
        "payload": payload,
        "path": path,
        "path_ref": ref,
        "sha256": sha256,
        "grid": grid,
        "context_drift": drift_keys,
        "conservative_close": bool(drift_keys),
    }


def _batch_attempt_exists(state: dict[str, Any]) -> bool:
    # Epoch migration deliberately leaves an auditable INVALIDATED snapshot.
    # It is terminal legacy evidence, not a resumable current-epoch family.
    if state.get("invalidated") is True:
        return False
    return bool(
        state.get("started_at") or state.get("total") or state.get("results")
        or state.get("batch_family_sha256")
    )


def _batch_is_complete(state: dict[str, Any]) -> bool:
    return bool(
        state.get("batch_complete") is True
        and state.get("by_complete") is True
        and state.get("decision_evidence_path")
        and state.get("decision_evidence_sha256")
        and not state.get("running")
    )


def _prepared_batch_state(
    family: dict[str, Any], *, previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    previous = previous or {}
    resumed = bool(previous and not _batch_is_complete(previous))
    started_at = previous.get("started_at") if resumed else None
    state: dict[str, Any] = {
        "schema_version": BATCH_STATE_SCHEMA_VERSION,
        "status": "running",
        "running": True,
        "phase": "resuming" if resumed else "prepared",
        "started_at": started_at or _now(),
        "attempt_started_at": _now(),
        "worker_pid": os.getpid(),
        "account_capital": BATCH_RESEARCH_CAPITAL,
        "one_way_cost_bps": BATCH_ONE_WAY_COST_BPS,
        "one_way_cost_bps_role": ONE_WAY_COST_BPS_ROLE,
        "additional_trading_costs": ADDITIONAL_TRADING_COSTS,
        "fdr_q": float(family["payload"]["fdr_q"]),
        "fdr_method": family["payload"]["fdr_method"],
        "fdr_method_version": family["payload"]["fdr_method_version"],
        "total": len(family["grid"]),
        "done": 0,
        "current": "",
        "current_factor_ids": [],
        "current_compose_method": None,
        "current_holding_days": None,
        "current_top_n": None,
        "results": [],
        "by_complete": False,
        "batch_complete": False,
        "resumed_from_frozen_family": resumed,
        "conservative_close": bool(family.get("conservative_close")),
        **_family_state_fields(family),
    }
    if resumed:
        state["previous_done"] = int(previous.get("done") or 0)
    return state


def _prepare_or_resume_batch_family() -> tuple[dict[str, Any], dict[str, Any]]:
    """Prepare a new family or resume only the exact previously frozen family."""
    prior = _batch_state()
    if _batch_attempt_exists(prior) and not _batch_is_complete(prior):
        family = _load_frozen_batch_family(prior)
    else:
        grid = _batch_grid()
        if not grid:
            raise StrategyValidationError(
                "当前严格研究 epoch 暂无合格挖掘因子；已禁止内置模板或纯策略回退"
            )
        family = _freeze_batch_family(grid)
        prior = {}
    state = _prepared_batch_state(family, previous=prior)
    _atomic_json(BATCH_STATE_PATH, state)
    return family, state


def _batch_decision_row(
    index: int, row: dict[str, Any], spec: dict[str, Any], family_sha256: str,
) -> dict[str, Any]:
    p_value = float(row["fdr_p_value"])
    if not math.isfinite(p_value) or not 0 <= p_value <= 1:
        raise RuntimeError("批次决策证据含非法 p-value")
    if row.get("batch_fdr_passed") not in (True, False):
        raise RuntimeError("批次决策证据缺少 BY 判定")
    if not row.get("global_test_id") or not row.get("global_hypothesis_id"):
        raise RuntimeError("批次决策证据缺少 LOND 检验身份")
    core = {
        "index": index,
        "family_sha256": family_sha256,
        "hypothesis_id": _batch_hypothesis_id(spec),
        "result_spec_sha256": result_spec_sha256(spec),
        "p_value": p_value,
        "p_value_method": row.get("p_value_method"),
        "is_validation_passed": bool(row.get("is_validation_passed")),
        "gate_passed": bool(row.get("gate_passed")),
        "by_reject": bool(row["batch_fdr_passed"]),
        "global_test_id": int(row["global_test_id"]),
        "global_hypothesis_order": int(row["global_hypothesis_order"]),
        "global_alpha_i": float(row["global_alpha_i"]),
        "global_lond_reject": bool(row.get("global_lond_passed")),
        "global_evidence_path": row.get("global_evidence_path"),
        "global_evidence_sha256": row.get("global_evidence_sha256"),
    }
    if core["hypothesis_id"] != row["global_hypothesis_id"]:
        raise RuntimeError("批次决策证据的规格与 LOND 假设不一致")
    return {**core, "decision_row_sha256": _canonical_payload_sha256(core)}


def _freeze_batch_decision_evidence(
    family: dict[str, Any], results: list[dict[str, Any]],
    grid: list[dict[str, Any]], fdr: dict[str, Any],
) -> dict[str, Any]:
    """Freeze the full BY+LOND decision table before any promotion write."""
    if len(results) != len(grid) or len(grid) != len(family["grid"]):
        raise RuntimeError("批次决策行数与冻结 family 不一致")
    decisions = [
        _batch_decision_row(index, row, spec, family["sha256"])
        for index, (row, spec) in enumerate(zip(results, grid))
    ]
    context = family["payload"]
    payload = {
        "schema_version": BATCH_DECISION_SCHEMA_VERSION,
        "evidence_stage": "strategy_batch_by_lond_decisions",
        "family_sha256": family["sha256"],
        "research_epoch": context["research_epoch"],
        "historical_confirmation_epoch": context["historical_confirmation_epoch"],
        "panel_data_key": context["panel_data_key"],
        "fdr_q": float(context["fdr_q"]),
        "fdr_method": context["fdr_method"],
        "fdr_method_version": context["fdr_method_version"],
        "fdr_effective_q": float(fdr["effective_q"]),
        "family_size": len(decisions),
        "decisions": decisions,
    }
    path, sha256 = _write_content_addressed_json(
        BATCH_STATE_PATH.parent / "strategy_batch_decisions", payload,
    )
    for row, decision in zip(results, decisions):
        row.update({
            "batch_family_sha256": family["sha256"],
            "batch_decision_evidence_path": _batch_artifact_ref(path),
            "batch_decision_evidence_sha256": sha256,
            "batch_decision_index": decision["index"],
            "batch_decision_row_sha256": decision["decision_row_sha256"],
        })
    return {
        "payload": _read_json(path),
        "path": path,
        "path_ref": _batch_artifact_ref(path),
        "sha256": sha256,
        "decisions": decisions,
    }


def _new_batch_result(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": spec.get("name"), "holding_days": spec.get("holding_days"),
        "top_n": spec.get("top_n"), "passed": False, "verdict": "PENDING",
        "reason": "", "net_annual_pct": None, "shanghai_annual_pct": None,
        "shanghai_excess_pct": None, "excess_vs_ew_pct": None,
        "t_stat": None, "mdd_pct": None, "strategy_id": None,
        "fullcycle_annual_pct": None, "fullcycle_excess_pct": None,
        "account_capital": float(spec.get(
            "account_capital", BATCH_RESEARCH_CAPITAL)),
        "one_way_cost_bps": int(spec.get(
            "one_way_cost_bps", BATCH_ONE_WAY_COST_BPS)),
        "one_way_cost_bps_role": ONE_WAY_COST_BPS_ROLE,
        "additional_trading_costs": ADDITIONAL_TRADING_COSTS,
        "factor_ids": list(spec.get("factors") or {}),
        "compose_method": spec.get("compose_method", "rank_weighted_v1"),
        "is_validation_passed": False, "gate_passed": False,
        "historical_confirmation_passed": False,
        "batch_fdr_passed": False, "oos_ic_p_value": None,
        "fdr_p_value": 1.0,
    }


def _batch_result_from_evaluation(
    spec: dict[str, Any], evaluation: dict[str, Any],
) -> dict[str, Any]:
    row = _new_batch_result(spec)
    evaluation_error = evaluation.get("evaluation_error")
    if evaluation_error:
        row.update({
            "verdict": "ERROR", "reason": str(evaluation_error),
            "error": str(evaluation_error), "fdr_p_value": 1.0,
            "p_value_method": BATCH_P_VALUE_METHOD,
            "p_value_source": "conservative_evaluation_error_default",
        })
        return row
    is_validation = evaluation.get("is_validation") or {}
    gate = evaluation.get("gate") or {}
    full = gate.get("full") or {}
    full_cycle = gate.get("full_cycle") or {}
    raw_p = (gate.get("oos_ic") or {}).get("p_value")
    try:
        p_value = float(raw_p)
    except (TypeError, ValueError):
        p_value = 1.0
        p_value_valid = False
    else:
        p_value_valid = math.isfinite(p_value) and 0 <= p_value <= 1
    if not p_value_valid:
        p_value = 1.0
    row.update({
        "is_validation_passed": bool(is_validation.get("passed")),
        "gate_passed": bool(gate.get(
            "historical_confirmation_passed", gate.get("passed"))),
        "gate_verdict": gate.get("verdict") or "NO-GO",
        "gate_reason": gate.get("reason") or "",
        "oos_ic_p_value": p_value if p_value_valid else None,
        "p_value_method": (gate.get("oos_ic") or {}).get("p_value_method"),
        "fdr_p_value": p_value,
        "net_annual_pct": full.get("annual_return"),
        "shanghai_annual_pct": full.get("shanghai_annual"),
        "shanghai_excess_pct": full.get("shanghai_excess_annual"),
        "excess_vs_ew_pct": full.get("excess_annual"),
        "t_stat": full.get("t_stat"),
        "mdd_pct": full.get("max_drawdown"),
        "fullcycle_annual_pct": full_cycle.get("annual_return"),
        "fullcycle_excess_pct": full_cycle.get("shanghai_excess_annual"),
    })
    return row


def _batch_result_from_frozen_test(
    spec: dict[str, Any], test: dict[str, Any], reason: str,
) -> dict[str, Any]:
    """Reconstruct a fail-closed row when only the immutable LOND row survives."""
    row = _new_batch_result(spec)
    gate_passed = bool(test.get("gate_passed"))
    row.update({
        "verdict": "ERROR",
        "reason": reason,
        "error": reason,
        "is_validation_passed": gate_passed,
        "gate_passed": gate_passed,
        "gate_verdict": "RECOVERED_NO_GO",
        "gate_reason": reason,
        "oos_ic_p_value": float(test["p_value"]),
        "fdr_p_value": float(test["p_value"]),
        "p_value_method": test["p_value_method"],
        "p_value_source": "immutable_global_test_recovery",
        "forced_no_go": True,
    })
    return row


def _batch_observation_directory(
    family: dict[str, Any], index: int, spec: dict[str, Any],
) -> Path:
    suffix = _batch_hypothesis_id(spec).rsplit("::", 1)[-1]
    return (BATCH_STATE_PATH.parent / "strategy_batch_observations"
            / family["sha256"] / f"{index:04d}_{suffix}")


def _load_batch_observation(
    family: dict[str, Any], index: int, spec: dict[str, Any],
) -> dict[str, Any] | None:
    directory = _batch_observation_directory(family, index, spec)
    if not directory.exists():
        return None
    files = sorted(directory.glob("*.json"))
    if len(files) != 1:
        raise RuntimeError("候选观察证据数量异常，拒绝恢复")
    path = files[0]
    if path.stem != _sha256(path):
        raise RuntimeError("候选观察证据哈希不符")
    payload = _read_json(path)
    expected = {
        "schema_version": 1,
        "evidence_stage": "strategy_batch_candidate_observation",
        "family_sha256": family["sha256"],
        "index": index,
        "hypothesis_id": _batch_hypothesis_id(spec),
        "result_spec_sha256": result_spec_sha256(spec),
        "research_epoch": family["payload"]["research_epoch"],
        "panel_data_key": family["payload"]["panel_data_key"],
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise RuntimeError("候选观察证据与冻结 family 不一致")
    if not isinstance(payload.get("row"), dict) or not isinstance(
        payload.get("evaluation"), dict
    ):
        raise RuntimeError("候选观察证据内容不完整")
    return {"payload": payload, "path": path, "sha256": path.stem}


def _freeze_batch_observation(
    family: dict[str, Any], index: int, spec: dict[str, Any],
    row: dict[str, Any], evaluation: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "evidence_stage": "strategy_batch_candidate_observation",
        "family_sha256": family["sha256"],
        "index": index,
        "hypothesis_id": _batch_hypothesis_id(spec),
        "result_spec_sha256": result_spec_sha256(spec),
        "research_epoch": family["payload"]["research_epoch"],
        "panel_data_key": family["payload"]["panel_data_key"],
        "row": row,
        "evaluation": evaluation,
    }
    path, sha256 = _write_content_addressed_json(
        _batch_observation_directory(family, index, spec), payload,
    )
    return {"payload": _read_json(path), "path": path, "sha256": sha256}


def _frozen_global_test(
    spec: dict[str, Any], test_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    from factor_system import registry as freg

    context = test_context or _batch_test_context()
    freg.migrate_schema()
    c = freg._conn(readonly=True)
    try:
        row = c.execute(
            "SELECT * FROM factor_tests WHERE epoch=? AND factor_id=?",
            (context["historical_confirmation_epoch"],
             _batch_hypothesis_id(spec)),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        c.close()


def _evaluation_from_global_evidence(
    test: dict[str, Any], spec: dict[str, Any],
) -> dict[str, Any] | None:
    ref, sha256 = test.get("evidence_path"), test.get("evidence_sha256")
    if not ref or not sha256:
        return None
    path = ROOT / ref
    if not path.is_file() or _sha256(path) != sha256:
        raise RuntimeError("已冻结全局检验证据缺失或哈希不符")
    payload = _read_json(path)
    if (payload.get("hypothesis_id") != _batch_hypothesis_id(spec)
            or payload.get("result_spec_sha256") != result_spec_sha256(spec)):
        raise RuntimeError("已冻结全局检验证据与策略规格不一致")
    evaluation = payload.get("evaluation")
    return evaluation if isinstance(evaluation, dict) else None


def _strategy_batch_orphan_recovery(
    test: dict[str, Any], family: dict[str, Any], index: int,
    spec: dict[str, Any],
) -> dict[str, Any] | None:
    """Validate a generic daemon p=1 finalization of this exact batch latch."""
    ref, sha256 = test.get("evidence_path"), test.get("evidence_sha256")
    if not ref or not sha256:
        return None
    path = _resolve_batch_artifact(str(ref))
    if not path.is_file() or _sha256(path) != sha256:
        raise RuntimeError("孤儿观察恢复证据缺失或哈希不符")
    payload = _read_json(path)
    if payload.get("evidence_stage") != "orphan_observation_conservative_finalize":
        return None
    expected_context = _batch_latch_context(family, index, spec)
    expected_context_json = json.dumps(
        expected_context, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    )
    expected_context_sha = hashlib.sha256(
        expected_context_json.encode("utf-8")
    ).hexdigest()
    frozen_test = payload.get("test") or {}
    valid = (
        payload.get("schema_version") == 1
        and payload.get("producer_type") == "strategy_batch"
        and payload.get("epoch") == family["payload"]["historical_confirmation_epoch"]
        and payload.get("factor_id") == _batch_hypothesis_id(spec)
        and payload.get("data_key") == family["payload"]["panel_data_key"]
        and payload.get("p_value_method") == family["payload"]["p_value_method"]
        and payload.get("context_json") == expected_context_json
        and payload.get("context_sha256") == expected_context_sha
        and payload.get("new_data_used_for_decision") is False
        and payload.get("verdict") == "ERROR"
        and float(test.get("p_value")) == 1.0
        and not bool(test.get("gate_passed"))
        and int(frozen_test.get("test_id")) == int(test["test_id"])
        and float(frozen_test.get("p_value")) == 1.0
        and not bool(frozen_test.get("gate_passed"))
    )
    if not valid:
        raise RuntimeError("strategy_batch 孤儿恢复证据与冻结 family/latch 不一致")
    return {
        "path": path,
        "path_ref": str(ref),
        "sha256": sha256,
        "reason": str(payload.get("reason") or "孤儿观察已由 daemon 按 p=1 保守终结"),
    }


def _apply_frozen_global_test(
    row: dict[str, Any], spec: dict[str, Any], test: dict[str, Any],
    test_context: dict[str, Any] | None = None,
) -> None:
    context = test_context or _batch_test_context()
    expected_gate = bool(row.get("is_validation_passed") and row.get("gate_passed"))
    if (test.get("factor_id") != _batch_hypothesis_id(spec)
            or test.get("epoch") != context["historical_confirmation_epoch"]
            or test.get("data_key") != context["panel_data_key"]
            or test.get("p_value_method") != context["p_value_method"]
            or abs(float(test.get("p_value")) - float(row["fdr_p_value"])) > 1e-12
            or bool(test.get("gate_passed")) != expected_gate):
        raise RuntimeError("已冻结 LOND 检验与候选观察证据不一致")
    row.update({
        "global_fdr_method": "LOND-dependent",
        "global_fdr_epoch": test["epoch"],
        "global_test_id": int(test["test_id"]),
        "global_hypothesis_id": test["factor_id"],
        "global_hypothesis_order": int(test["hypothesis_order"]),
        "global_alpha_i": float(test["alpha_i"]),
        "global_lond_passed": bool(test["online_reject"]),
        "global_tested_at": test["tested_at"],
        "global_p_value_method": test["p_value_method"],
        "global_data_key": test["data_key"],
        "global_evidence_path": test.get("evidence_path"),
        "global_evidence_sha256": test.get("evidence_sha256"),
    })


def _batch_state() -> dict[str, Any]:
    """读批量状态；done==total 不等于 BY/证据/晋升阶段已完成。"""
    state = _read_json(BATCH_STATE_PATH) or {}
    if _batch_is_complete(state):
        changed = bool(state.pop("crashed_stale", None) or state.pop("interrupted_reason", None))
        if changed:
            _atomic_json(BATCH_STATE_PATH, state)
        return state

    owner_pid = int(state.get("worker_pid") or 0)
    owner_alive = False
    if owner_pid and owner_pid != os.getpid():
        try:
            os.kill(owner_pid, 0)
            owner_alive = True
        except ProcessLookupError:
            owner_alive = False
        except PermissionError:
            owner_alive = True

    # worker 是进程内线程；Event 复位且记录中的所属进程也不存在，才说明线程已消失。
    if state.get("running") and not _BATCH_ACTIVE.is_set() and not owner_alive:
        state["running"] = False
        state["status"] = "interrupted"
        state["crashed_stale"] = True
        state["interrupted_reason"] = (
            "网站进程重启，旧批次线程已不存在；只能从不可变 family 恢复"
        )
        _atomic_json(BATCH_STATE_PATH, state)
        return state
    if (_batch_attempt_exists(state) and not state.get("running")
            and not _batch_is_complete(state)):
        state["crashed_stale"] = True
        state["status"] = "interrupted"
        state.setdefault(
            "interrupted_reason",
            "批次尚未完成 BY/决策证据/晋升阶段；只能从不可变 family 恢复",
        )
        _atomic_json(BATCH_STATE_PATH, state)
    return state


def start_batch_generation() -> dict[str, Any]:
    """启动批量生成；完整生命周期同时持有线程锁与跨进程研究写锁。"""
    if _BATCH_ACTIVE.is_set():
        return {"ok": False, "error": "批量生成已在运行中，请等待完成"}
    if not RUN_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "回测引擎忙（有回测/诊断正在运行），请稍后重试"}
    mutation_lock = acquire_research_mutation_lock(blocking=False)
    if mutation_lock is None:
        RUN_LOCK.release()
        return {"ok": False, "error": "研究数据正在迁移或已有跨进程研究写任务，请稍后重试"}
    try:
        existing = _batch_state()
        # Pending families may need p=1 conservative closure precisely because
        # their old input snapshot is no longer loadable. Fresh work still must
        # pass the ordinary source validation before a family is frozen.
        if not (_batch_attempt_exists(existing) and not _batch_is_complete(existing)):
            _source_state()
        family, prepared_state = _prepare_or_resume_batch_family()
        grid = family["grid"]
    except Exception as exc:
        release_research_mutation_lock(mutation_lock)
        RUN_LOCK.release()
        return {"ok": False, "error": f"批次准备失败: {exc}"}
    _BATCH_ACTIVE.set()
    try:
        threading.Thread(
            target=_batch_worker, args=(grid, mutation_lock, family), daemon=True,
        ).start()
    except Exception as exc:
        _BATCH_ACTIVE.clear()
        prepared_state.update({
            "running": False,
            "status": "interrupted",
            "crashed_stale": True,
            "interrupted_reason": f"批次线程启动失败: {exc}",
        })
        _atomic_json(BATCH_STATE_PATH, prepared_state)
        release_research_mutation_lock(mutation_lock)
        RUN_LOCK.release()
        raise
    factor_ids = sorted({fid for spec in grid for fid in spec.get("factors", {})
                         if fid not in FACTOR_CATALOG})
    return {"ok": True, "total": len(grid), "factor_ids": factor_ids,
            "resumed_from_frozen_family": bool(
                prepared_state.get("resumed_from_frozen_family")
            ),
            "batch_family_sha256": family["sha256"],
            "batch_family_evidence_path": family["path_ref"],
            "excluded_existing_test_key_mismatch_count": len(
                family["payload"].get("excluded_existing_test_key_mismatch") or []
            ),
            "excluded_existing_final_batch_decision_count": len(
                family["payload"].get(
                    "excluded_existing_final_batch_decision",
                ) or []
            ),
            "account_capital": BATCH_RESEARCH_CAPITAL,
            "one_way_cost_bps": BATCH_ONE_WAY_COST_BPS,
            "one_way_cost_bps_role": ONE_WAY_COST_BPS_ROLE,
            "additional_trading_costs": ADDITIONAL_TRADING_COSTS,
            "note": f"{len(grid)} 条因子驱动候选，使用 {len(factor_ids)} 个挖掘因子；"
                    "先按因子选股，再由统一引擎严格回测。候选全部展示；"
                    "五段通过只记 HISTORICAL_PASS 并锁定。"}


def _batch_worker(
    grid: list[dict[str, Any]], mutation_lock=None,
    family: dict[str, Any] | None = None,
) -> None:
    """逐条冻结 LOND 序号，完成全批 BY-FDR 后保存历史确认候选。"""
    import traceback
    held_mutation_lock = mutation_lock or acquire_research_mutation_lock(blocking=False)
    try:
        if held_mutation_lock is None:
            raise RuntimeError("跨进程研究写锁忙，批次未执行")
        import factor_system.compose as compose
        from quantqc.stats import by_fdr

        prior = _read_json(BATCH_STATE_PATH) or {}
        if family is not None:
            verified = _load_frozen_batch_family(prior)
            if verified["sha256"] != family.get("sha256"):
                raise RuntimeError("worker 收到的 family 与启动状态不一致")
            family = verified
            grid = family["grid"]
            state = dict(prior)
            state.update({"running": True, "status": "running", "phase": "evaluating",
                          "worker_pid": os.getpid(), "done": 0, "results": []})
        elif _batch_attempt_exists(prior) and not _batch_is_complete(prior):
            family = _load_frozen_batch_family(prior)
            grid = family["grid"]
            state = _prepared_batch_state(family, previous=prior)
            state["phase"] = "evaluating"
        else:
            family = _freeze_batch_family(grid)
            grid = family["grid"]
            state = _prepared_batch_state(family)
            state["phase"] = "evaluating"
        _atomic_json(BATCH_STATE_PATH, state)
        results: list[dict[str, Any]] = []
        evaluations: list[dict[str, Any] | None] = []
        for index, spec in enumerate(grid):
            state["current"] = spec.get("name") or ""
            state["current_factor_ids"] = list(spec.get("factors") or {})
            state["current_compose_method"] = spec.get("compose_method", "rank_weighted_v1")
            state["current_holding_days"] = spec.get("holding_days")
            state["current_top_n"] = spec.get("top_n")
            state["done"] = index
            _atomic_json(BATCH_STATE_PATH, state)
            frozen_test = _frozen_global_test(spec, family["payload"])
            observation = _load_batch_observation(family, index, spec)
            orphan_recovery = (
                _strategy_batch_orphan_recovery(
                    frozen_test, family, index, spec,
                ) if frozen_test is not None else None
            )
            row: dict[str, Any] | None = None
            # An unfinished candidate must own the global latch before any gate
            # input is read. Finalized prefix rows are reconstructed from immutable
            # evidence and never re-evaluated, so a later pending latch is not hit.
            if frozen_test is None:
                _begin_batch_online_observation(
                    spec, family=family, family_index=index,
                )
            if orphan_recovery is not None:
                # The daemon's immutable p=1 test is authoritative even when an
                # actual-p observation file survived the original crash. Keep the
                # latter only as superseded audit evidence; never reuse its p.
                evaluation = {"evaluation_error": orphan_recovery["reason"]}
                row = _batch_result_from_frozen_test(
                    spec, frozen_test, orphan_recovery["reason"],
                )
                row.update({
                    "orphan_recovery_evidence_path": orphan_recovery["path_ref"],
                    "orphan_recovery_evidence_sha256": orphan_recovery["sha256"],
                })
                if observation is not None:
                    row.update({
                        "superseded_batch_observation_evidence_path": (
                            _batch_artifact_ref(observation["path"])
                        ),
                        "superseded_batch_observation_evidence_sha256": (
                            observation["sha256"]
                        ),
                    })
            elif observation is None:
                if frozen_test is not None:
                    evaluation = _evaluation_from_global_evidence(frozen_test, spec)
                    if evaluation is None:
                        if not family.get("conservative_close"):
                            raise RuntimeError(
                                "已完成的 LOND 候选缺少可恢复评估证据，拒绝重新观察"
                            )
                        reason = "冻结 family 输入已变化；仅按不可变 LOND 行保守收口"
                        row = _batch_result_from_frozen_test(
                            spec, frozen_test, reason,
                        )
                        evaluation = {"evaluation_error": reason}
                else:
                    if family.get("conservative_close"):
                        evaluation = {
                            "evaluation_error": (
                                "冻结 family 的 research_input_key 已变化；"
                                "未观察候选以 p=1 保守收口"
                            )
                        }
                    else:
                        try:
                            evaluation = compose.evaluate_strategy(
                                spec,
                                expected_input_key=family["payload"][
                                    "panel_data_key"
                                ],
                            )
                        except Exception as exc:
                            traceback.print_exc()
                            evaluation = {"evaluation_error": str(exc)}
                if row is None:
                    row = _batch_result_from_evaluation(spec, evaluation)
                observation = _freeze_batch_observation(
                    family, index, spec, row, evaluation,
                )
            else:
                evaluation = observation["payload"]["evaluation"]
                row = dict(observation["payload"]["row"])
            assert row is not None
            if orphan_recovery is None:
                row.update({
                    "batch_observation_evidence_path": _batch_artifact_ref(
                        observation["path"]
                    ),
                    "batch_observation_evidence_sha256": observation["sha256"],
                })
            if frozen_test is None:
                # 正常和 ERROR 都在同一预留 latch 上 finalize；ERROR 的冻结
                # observation 已把 p=1 写死，恢复时不会重新采样。
                _record_batch_online_tests(
                    [row], [spec], family=family, family_start_index=index,
                )
            else:
                _apply_frozen_global_test(
                    row, spec, frozen_test, family["payload"],
                )
            # Candidate evidence is attached before public progress. A crash in
            # the tiny record→attach gap is recovered from the observation file.
            _persist_batch_test_evidence(row, spec, evaluation)
            if family.get("conservative_close"):
                reason = (
                    "冻结 family 上下文已变化，批次仅保守收口；"
                    "不得历史确认，需用当前输入启动新 family"
                )
                row.update({
                    "passed": False,
                    "historical_confirmation_passed": False,
                    "verdict": "ERROR",
                    "forced_no_go": True,
                    "reason": reason,
                    "error": reason,
                })
            results.append(row)
            evaluations.append(evaluation)
            state["done"] = index + 1
            state["results"] = results
            _atomic_json(BATCH_STATE_PATH, state)

        state["phase"] = "by_pending"
        state["done"] = len(results)
        _atomic_json(BATCH_STATE_PATH, state)
        family_q = float(family["payload"]["fdr_q"])
        fdr = by_fdr([row["fdr_p_value"] for row in results], q=family_q)
        state["fdr_method"] = fdr["method"]
        state["fdr_effective_q"] = fdr["effective_q"]
        for row, rejected in zip(results, fdr["rejected"]):
            row["batch_fdr_passed"] = bool(rejected)
            row["fdr_q"] = family_q
            row["fdr_method"] = fdr["method"]
            row["fdr_method_version"] = family["payload"]["fdr_method_version"]
            row["fdr_effective_q"] = fdr["effective_q"]
            row["fdr_family_size"] = len(results)
        _persist_all_batch_test_evidence(results, grid, evaluations)
        decision = _freeze_batch_decision_evidence(family, results, grid, fdr)
        state["global_fdr_method"] = "LOND-dependent"
        state["global_fdr_epoch"] = family["payload"]["historical_confirmation_epoch"]
        state["decision_evidence_path"] = decision["path_ref"]
        state["decision_evidence_sha256"] = decision["sha256"]
        state["by_complete"] = True
        state["phase"] = "promotion"
        state["results"] = results
        _atomic_json(BATCH_STATE_PATH, state)

        for row, spec, ev in zip(results, grid, evaluations):
            if row.get("verdict") == "ERROR":
                continue
            failed = []
            if not row["is_validation_passed"]:
                failed.append("IS验证未通过")
            if not row["gate_passed"]:
                failed.append(row.get("gate_reason") or "统一科学闸门未通过")
            if not row["batch_fdr_passed"]:
                failed.append("批次BY-FDR未通过")
            if not row.get("global_lond_passed"):
                failed.append("跨批全局LOND未通过")
            if failed:
                row.update({
                    "passed": False, "historical_confirmation_passed": False,
                    "verdict": "NO-GO", "reason": "、".join(failed),
                })
                continue
            try:
                saved = _save_batch_strategy(spec)
                sid = saved["strategy_id"]
                diag = compose.diagnose_strategy(
                    spec,
                    expected_input_key=family["payload"]["panel_data_key"],
                )
                decision_binding = {
                    "family_sha256": family["sha256"],
                    "decision_evidence_path": decision["path_ref"],
                    "decision_evidence_sha256": decision["sha256"],
                    "decision_index": row["batch_decision_index"],
                    "decision_row_sha256": row["batch_decision_row_sha256"],
                    "hypothesis_id": row["global_hypothesis_id"],
                    "global_test_id": row["global_test_id"],
                    "by_reject": row["batch_fdr_passed"],
                    "global_lond_reject": row["global_lond_passed"],
                }
                evidence = save_strategy_diagnosis(
                    sid, {"evaluate": ev, "diagnose": diag,
                          "batch_decision": decision_binding},
                    evaluated_spec=spec,
                )
                record_promotion_verdict(
                    sid, "HISTORICAL_PASS", row.get("gate_reason"), evidence,
                    diag.get("diagnosed_at"), evaluated_spec=spec,
                )
                row.update({
                    "passed": False,
                    "historical_confirmation_passed": True,
                    "verdict": "HISTORICAL_PASS",
                    "reason": (row.get("gate_reason")
                               or "五段历史确认通过；等待独立前向 runner"),
                    "strategy_id": sid,
                    "promotion_evidence_path": evidence,
                    "promotion_evidence_sha256": _sha256(ROOT / evidence),
                })
            except Exception as exc:
                traceback.print_exc()
                row.update({
                    "passed": False, "verdict": "ERROR", "strategy_id": None,
                    "reason": str(exc), "error": str(exc),
                })
            state["results"] = results
            _atomic_json(BATCH_STATE_PATH, state)
        state["running"] = False
        state["status"] = "complete"
        state["phase"] = "complete"
        state["batch_complete"] = True
        state["completed_at"] = _now()
        state.pop("crashed_stale", None)
        state.pop("interrupted_reason", None)
        state.pop("current", None)
        state.pop("current_factor_ids", None)
        state.pop("current_compose_method", None)
        state.pop("current_holding_days", None)
        state.pop("current_top_n", None)
        _atomic_json(BATCH_STATE_PATH, state)
        _atomic_json(BATCH_RESULTS_PATH, state)  # 终态归档，供非易失参考
    except Exception as exc:
        traceback.print_exc()
        try:
            failed_state = dict(locals().get("state") or _read_json(BATCH_STATE_PATH) or {})
            if failed_state:
                failed_state.update({
                    "running": False,
                    "status": "interrupted",
                    "crashed_stale": True,
                    "interrupted_reason": f"批次中断: {exc}",
                })
                failed_state.setdefault("batch_complete", False)
                _atomic_json(BATCH_STATE_PATH, failed_state)
        except Exception:
            traceback.print_exc()
    finally:
        _BATCH_ACTIVE.clear()
        release_research_mutation_lock(held_mutation_lock)
        if RUN_LOCK.locked():
            RUN_LOCK.release()


def batch_status() -> dict[str, Any]:
    """批量生成进度与候选池（全部候选诚实展示，不裁剪）。"""
    state = _batch_state()
    return {
        "ok": True,
        "schema_version": state.get("schema_version"),
        "status": state.get("status"),
        "running": bool(state.get("running")),
        "active": _BATCH_ACTIVE.is_set(),
        "crashed_stale": bool(state.get("crashed_stale")),
        "phase": state.get("phase"),
        "by_complete": bool(state.get("by_complete")),
        "batch_complete": bool(state.get("batch_complete")),
        "resumed_from_frozen_family": bool(
            state.get("resumed_from_frozen_family")
        ),
        "total": int(state.get("total") or 0),
        "done": int(state.get("done") or 0),
        "current": state.get("current"),
        "current_factor_ids": state.get("current_factor_ids") or [],
        "current_compose_method": state.get("current_compose_method"),
        "current_holding_days": state.get("current_holding_days"),
        "current_top_n": state.get("current_top_n"),
        "started_at": state.get("started_at"),
        "completed_at": state.get("completed_at"),
        "account_capital": state.get("account_capital", BATCH_RESEARCH_CAPITAL),
        "one_way_cost_bps": state.get("one_way_cost_bps", BATCH_ONE_WAY_COST_BPS),
        "one_way_cost_bps_role": ONE_WAY_COST_BPS_ROLE,
        "additional_trading_costs": ADDITIONAL_TRADING_COSTS,
        "fdr_q": state.get("fdr_q"),
        "fdr_method": state.get("fdr_method"),
        "fdr_effective_q": state.get("fdr_effective_q"),
        "fdr_method_version": state.get("fdr_method_version"),
        "global_fdr_method": state.get("global_fdr_method"),
        "global_fdr_epoch": state.get("global_fdr_epoch"),
        "batch_family_evidence_path": state.get("batch_family_evidence_path"),
        "batch_family_sha256": state.get("batch_family_sha256"),
        "batch_family_planned_size": state.get("batch_family_planned_size"),
        "excluded_existing_test_key_mismatch_count": int(
            state.get("excluded_existing_test_key_mismatch_count") or 0
        ),
        "decision_evidence_path": state.get("decision_evidence_path"),
        "decision_evidence_sha256": state.get("decision_evidence_sha256"),
        "excluded_existing_final_batch_decision_count": int(
            state.get("excluded_existing_final_batch_decision_count") or 0
        ),
        "results": state.get("results") or [],
        "results_file": str(BATCH_RESULTS_PATH.relative_to(ROOT)),
        "gate_benchmark": "sh000001 上证指数（close-to-close）强制跑赢",
    }


class StrategyValidationError(ValueError):
    pass


class BacktestBusyError(RuntimeError):
    pass


class ForwardEvidenceError(RuntimeError):
    """An existing paper-forward artifact is corrupt or internally inconsistent."""

    pass


def _now() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_slice_sha256(path: Path, start_marker: str, end_marker: str) -> str:
    """Hash one executable source section without coupling to unrelated routes."""
    source = path.read_text(encoding="utf-8")
    start = source.find(start_marker)
    end = source.find(end_marker, start + len(start_marker))
    if start < 0 or end < 0 or end <= start:
        raise RuntimeError(
            f"无法定位策略执行源码区间: {path.name} / {start_marker!r}"
        )
    return hashlib.sha256(source[start:end].encode("utf-8")).hexdigest()


def _strategy_app_contract_sha256() -> dict[str, str]:
    """Bind only the app sections that can alter unified strategy execution."""
    return {
        "research_mutation_route": _source_slice_sha256(
            APP_SOURCE_PATH,
            "def _research_mutation_route(view):",
            "from .signal_system import",
        ),
        "historical_observation": _source_slice_sha256(
            APP_SOURCE_PATH,
            "def _begin_strategy_historical_observation(spec: dict) -> dict:",
            "_STRATEGY_COST_SEMANTICS =",
        ),
        "diagnose_route": _source_slice_sha256(
            APP_SOURCE_PATH,
            '    @app.post("/api/strategy-lab/diagnose")',
            '    @app.post("/api/strategy-lab/experiments")',
        ),
    }


def _current_strategy_evaluator_fingerprint() -> str:
    """Hash every source file that can change unified strategy results."""
    manifest = {
        "schema": "strategy_evaluator_fingerprint_v1",
        "research_epoch": factor_config.RESEARCH_EPOCH,
        "source_sha256": {
            str(path.relative_to(CODE_ROOT)): _sha256(path)
            for path in STRATEGY_EVALUATOR_SOURCE_PATHS
        },
        "app_contract_sha256": _strategy_app_contract_sha256(),
    }
    return hashlib.sha256(json.dumps(
        manifest, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


_PROCESS_STRATEGY_EVALUATOR_FINGERPRINT = (
    _current_strategy_evaluator_fingerprint()
)


def strategy_evaluator_fingerprint() -> str:
    """Return the frozen process identity, rejecting source hot changes."""
    current = _current_strategy_evaluator_fingerprint()
    if current != _PROCESS_STRATEGY_EVALUATOR_FINGERPRINT:
        raise StrategyValidationError(
            "策略执行源码已热更新；必须重启进程后重新运行统一诊断"
        )
    return _PROCESS_STRATEGY_EVALUATOR_FINGERPRINT


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _immutable_json(path: Path, value: dict[str, Any]) -> None:
    """首次写入后不可覆盖；同内容重试幂等，不同内容直接失败。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != encoded:
            raise RuntimeError(f"不可变证据已存在且内容不同: {path}")
        return
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _atomic_immutable_json(path: Path, value: dict[str, Any]) -> None:
    """Publish immutable JSON atomically without exposing a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    temporary = path.parent / (
        f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    )
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != encoded:
                raise RuntimeError(f"不可变证据已存在且内容不同: {path}")
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False)
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_id(value: Any) -> str | None:
    candidate = str(value or "")
    if (
        candidate.startswith(STRATEGY_ID_PREFIX)
        and len(candidate) == len(STRATEGY_ID_PREFIX) + 12
        and all(char in "0123456789abcdef" for char in candidate[len(STRATEGY_ID_PREFIX):])
    ):
        return candidate
    return None


def _safe_run_id(value: Any) -> str | None:
    candidate = str(value or "")
    parts = candidate.split("-")
    if (
        len(parts) == 3
        and parts[0] == "run"
        and len(parts[1]) == 14
        and parts[1].isdigit()
        and len(parts[2]) == 6
        and all(char in "0123456789abcdef" for char in parts[2])
    ):
        return candidate
    return None


# ── M3.3 晋升通路（fail-closed）────────────────────────────────
# 每版策略带 promotion 块：unified_verdict 由 factor_system 统一引擎科学闸门写入，
# paper_observe 仅记录纸面观察。当前没有策略前向终测 runner，PASS、unlock 与
# recommendation_eligible 全部 fail-closed；历史确认只能写 HISTORICAL_PASS。
_LEGACY_MISSING_ENTRY_RULE_HASH_SCHEMA = (
    "legacy_result_spec_v1_missing_entry_rule_defaults_none"
)


def _result_spec_payload(spec: dict[str, Any] | None) -> dict[str, Any]:
    source = spec or {}
    payload = {
        "holding_days": int(source.get("holding_days") or 5),
        "top_n": int(source.get("top_n") or 30),
        "one_way_cost_bps": int(source.get("one_way_cost_bps") or 0),
        "rebal_buffer": int(source.get(
            "rebal_buffer", _configured_rebal_buffer(),
        )),
        "risk_rule": str(source.get("risk_rule") or "none"),
        "entry_rule": str(source.get("entry_rule") or "none"),
        "exposure_rule": str(source.get("exposure_rule") or "none"),
        "account_capital": float(source.get("account_capital", 200_000.0)),
        "compose_method": str(source.get("compose_method") or "rank_weighted_v1"),
    }
    factors = source.get("factors") or {}
    payload["factors"] = {
        str(key): float(value) for key, value in sorted(factors.items())
    }
    return payload


def _payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def result_spec_sha256(spec: dict[str, Any] | None) -> str:
    """只哈希会影响回测结果的规格字段；名称与 strategy_id 不参与。"""
    return _payload_sha256(_result_spec_payload(spec))


def _legacy_result_spec_sha256_without_entry_rule(
    spec: dict[str, Any] | None,
) -> str:
    """只用于核对 entry_rule 入库前的冻结策略身份。"""
    payload = _result_spec_payload(spec)
    payload.pop("entry_rule")
    return _payload_sha256(payload)


def _diagnosis_spec_hash_identity(
    saved: dict[str, Any],
) -> tuple[str, str, str | None]:
    """解析诊断规格身份；仅兼容缺 entry_rule 的旧冻结哈希。"""
    spec = saved.get("spec") if isinstance(saved, dict) else None
    if not isinstance(spec, dict):
        raise StrategyValidationError("策略版本缺少规格")
    behavioral_hash = result_spec_sha256(spec)
    stored_hash = str(saved.get("result_spec_sha256") or behavioral_hash)
    if stored_hash == behavioral_hash:
        return stored_hash, behavioral_hash, None
    if "entry_rule" in spec:
        raise StrategyValidationError("策略已冻结规格哈希与当前行为不一致")
    legacy_hash = _legacy_result_spec_sha256_without_entry_rule(spec)
    if stored_hash != legacy_hash:
        raise StrategyValidationError("策略已冻结规格哈希与当前行为不一致")
    return (
        stored_hash,
        behavioral_hash,
        _LEGACY_MISSING_ENTRY_RULE_HASH_SCHEMA,
    )


def _promotion_block(spec_sha256: str | None = None) -> dict[str, Any]:
    return {
        "unified_verdict": None,       # 当前无策略前向 runner，只允许 HISTORICAL_PASS
        "verdict_stage": None,
        "gate_reason": None,           # 判定理由（闸门 failed/通过原因）
        "evidence_report": None,       # 诊断证据文件相对路径
        "evidence_sha256": None,
        "result_spec_sha256": spec_sha256,
        "diagnosed_at": None,          # 最近一次统一引擎诊断时间
        "paper_observe": False,        # 纸面前向观察标记（不授予任何资格）
        "paper_observation_id": None,
        "paper_observation_run_id": None,
        "paper_observation_started_at": None,
        "paper_observation_stopped_at": None,
        "unlocked": False,             # 当前无前向 runner，不可设为 True
        "unlocked_at": None,
        "invalidated_at": None,
    }


def _promotion_eligible(promotion: dict[str, Any] | None) -> bool:
    """当前没有策略前向终测 runner，任何策略都保持 fail-closed。"""
    return False


def _audit_safe_promotion(promotion: dict[str, Any] | None) -> dict[str, Any]:
    """旧 PASS 没有受支持的前向 runner 证据，API/UI 一律按失效展示。"""
    promo = {**(promotion or _promotion_block())}
    if promo.get("unified_verdict") == "PASS":
        promo.update({
            "unified_verdict": "INVALIDATED",
            "verdict_stage": "legacy_unverified_forward",
            "gate_reason": "当前没有策略前向终测 runner；旧 PASS 证据失效",
            "unlocked": False,
            "unlocked_at": None,
        })
    return promo


def _promotion_of(strategy_id: str | None) -> dict[str, Any]:
    """按 strategy_id 读已保存策略的 promotion 块（无则给默认 fail-closed 块）。"""
    if not strategy_id:
        return _promotion_block()
    path = STRATEGIES_DIR / f"{strategy_id}.json"
    if not path.is_file():
        return _promotion_block()
    saved = _read_json(path)
    return _audit_safe_promotion(saved.get("promotion"))


def record_promotion_verdict(
    strategy_id: str, verdict: str, gate_reason: str | None,
    evidence_report: str | None, diagnosed_at: str | None,
    *, evaluated_spec: dict[str, Any] | None = None,
    evidence_stage: str = "historical_confirmation",
) -> dict[str, Any]:
    """把与当前规格哈希绑定的诊断结论写回 promotion。

    当前没有策略前向终测 runner，因此 PASS/forward_final 一律拒绝；只有固定
    批次的 HISTORICAL_PASS 可以写入且永不解锁。手工 EXPLORATORY 诊断不进入
    promotion。``evidence_report=None`` 不会清空 NO-GO/INVALIDATED 的已有审计指针。
    """
    path = STRATEGIES_DIR / f"{strategy_id}.json"
    if not path.is_file():
        raise StrategyValidationError(f"策略不存在: {strategy_id}")
    saved = _read_json(path)
    current_spec_hash = saved.get("result_spec_sha256") or result_spec_sha256(
        saved.get("spec") or {},
    )
    if evaluated_spec is not None:
        evaluated_hash = result_spec_sha256(evaluated_spec)
        if evaluated_hash != current_spec_hash:
            raise StrategyValidationError(
                "诊断规格与已保存策略当前版本不一致；请先保存该规格再重新诊断"
            )
    verdict = str(verdict or "NO-GO").upper()
    if verdict == "PASS" or evidence_stage == "forward_final":
        raise StrategyValidationError(
            "当前没有策略前向终测 runner；不得写入 PASS/forward_final"
        )
    if verdict not in {"HISTORICAL_PASS", "NO-GO", "INVALIDATED"}:
        raise StrategyValidationError(f"未知统一判定: {verdict}")

    promo = saved.get("promotion") or _promotion_block(current_spec_hash)
    promo["unified_verdict"] = verdict
    promo["verdict_stage"] = evidence_stage
    promo["gate_reason"] = gate_reason
    promo["result_spec_sha256"] = current_spec_hash
    if evidence_report is not None:
        rel = Path(evidence_report)
        if rel.is_absolute() or ".." in rel.parts:
            raise StrategyValidationError("诊断证据路径必须是项目内相对路径")
        evidence_path = ROOT / rel
        if not evidence_path.is_file():
            raise StrategyValidationError(f"诊断证据不存在: {evidence_report}")
        promo["evidence_report"] = evidence_report
        promo["evidence_sha256"] = _sha256(evidence_path)
    if diagnosed_at is not None:
        promo["diagnosed_at"] = diagnosed_at
    if verdict == "HISTORICAL_PASS" and evidence_report is None:
        raise StrategyValidationError("历史确认通过必须在本次调用绑定不可变诊断证据")
    promo["unlocked"] = False
    promo["unlocked_at"] = None
    saved["promotion"] = promo
    saved["recommendation_eligible"] = _promotion_eligible(promo)
    _atomic_json(path, saved)
    return saved


def save_strategy_diagnosis(
    strategy_id: str, report: dict[str, Any],
    *, evaluated_spec: dict[str, Any] | None = None,
) -> str:
    """按策略版本、规格哈希和报告哈希写不可覆盖诊断证据。"""
    strategy_path = STRATEGIES_DIR / f"{strategy_id}.json"
    if not strategy_path.is_file():
        raise StrategyValidationError(f"策略不存在: {strategy_id}")
    saved = _read_json(strategy_path)
    current_spec_hash, behavioral_spec_hash, hash_schema = (
        _diagnosis_spec_hash_identity(saved)
    )
    if (evaluated_spec is not None
            and result_spec_sha256(evaluated_spec) != behavioral_spec_hash):
        raise StrategyValidationError(
            "诊断规格与已保存策略当前版本不一致；证据未写入"
        )
    report_json = json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False, default=str,
    )
    report_sha = hashlib.sha256(report_json.encode("utf-8")).hexdigest()
    version = int(saved.get("version") or 1)
    d = ROOT / "research" / "strategy_diagnostics" / strategy_id
    path = d / f"v{version}_{current_spec_hash[:12]}_{report_sha[:16]}.json"
    diagnosed_at = ((report.get("diagnose") or {}).get("diagnosed_at")
                    if isinstance(report, dict) else None)
    evidence = {
        "schema_version": 2,
        "strategy_id": strategy_id,
        "strategy_version": version,
        "result_spec_sha256": current_spec_hash,
        "report_sha256": report_sha,
        "diagnosed_at": diagnosed_at,
        "report": report,
    }
    if hash_schema is not None:
        online = report.get("historical_online_test") if isinstance(
            report, dict,
        ) else None
        legacy_hypothesis_id = (
            online.get("global_hypothesis_id")
            if isinstance(online, dict) else None
        )
        try:
            legacy_test_id = int((online or {}).get("global_test_id"))
        except (TypeError, ValueError):
            legacy_test_id = 0
        if (legacy_hypothesis_id != f"strategy::{current_spec_hash[:24]}"
                or legacy_test_id < 1):
            raise StrategyValidationError(
                "legacy 诊断缺少与旧冻结哈希一致的全局检验身份"
            )
        evidence.update({
            "spec_hash_schema": hash_schema,
            "behavioral_spec_sha256": behavioral_spec_hash,
            "legacy_hypothesis_id": legacy_hypothesis_id,
            "legacy_test_id": legacy_test_id,
        })
    _immutable_json(path, evidence)
    return path.relative_to(ROOT).as_posix()


def _experiment_spec_payload(spec: dict[str, Any]) -> dict[str, Any]:
    """把展示字段排除，只保留可做单变量对照的结果规格。"""
    source = spec or {}
    payload = {
        "holding_days": int(source.get("holding_days") or 5),
        "top_n": int(source.get("top_n") or 30),
        "one_way_cost_bps": int(source.get("one_way_cost_bps") or 0),
        "rebal_buffer": int(source.get(
            "rebal_buffer", _configured_rebal_buffer(),
        )),
        "risk_rule": str(source.get("risk_rule") or "none"),
        "entry_rule": str(source.get("entry_rule") or "none"),
        "exposure_rule": str(source.get("exposure_rule") or "none"),
        "account_capital": float(source.get("account_capital", 200_000.0)),
        "compose_method": str(source.get("compose_method") or "rank_weighted_v1"),
        "factors": {
            str(key): float(value)
            for key, value in sorted((source.get("factors") or {}).items())
        },
    }
    return payload


def _experiment_leaf_changes(
    baseline: dict[str, Any], candidate: dict[str, Any], prefix: str = "",
) -> list[dict[str, Any]]:
    """稳定比较嵌套规格；一个因子增删/调权是一条叶子变化。"""
    changes: list[dict[str, Any]] = []
    for key in sorted(set(baseline) | set(candidate)):
        path = f"{prefix}.{key}" if prefix else key
        before = baseline.get(key)
        after = candidate.get(key)
        if isinstance(before, dict) and isinstance(after, dict):
            changes.extend(_experiment_leaf_changes(before, after, path))
        elif before != after:
            changes.append({"path": path, "before": before, "after": after})
    return changes


def _diagnosis_evidence_path(value: Any) -> tuple[Path, str]:
    relative = Path(str(value or ""))
    if (not str(value or "").strip() or relative.is_absolute()
            or ".." in relative.parts or relative.suffix != ".json"):
        raise StrategyValidationError("诊断证据路径必须是项目内相对 JSON 路径")
    expected_root = (ROOT / "research" / "strategy_diagnostics").resolve()
    path = (ROOT / relative).resolve()
    try:
        path.relative_to(expected_root)
    except ValueError as exc:
        raise StrategyValidationError("只能比较 strategy_diagnostics 内的诊断证据") from exc
    if not path.is_file():
        raise StrategyValidationError(f"诊断证据不存在: {relative}")
    return path, str(path.relative_to(ROOT.resolve()))


def _strategy_version_spec(
    strategy_id: str, version: int, expected_spec_sha256: str,
    *, expected_behavioral_spec_sha256: str,
    expected_hash_schema: str | None,
) -> dict[str, Any]:
    current_path = STRATEGIES_DIR / f"{strategy_id}.json"
    current = _read_json(current_path)
    if int(current.get("version") or 0) == version:
        saved = current
    else:
        saved = _read_json(
            STRATEGIES_DIR / "history" / f"{strategy_id}_v{version}.json"
        )
    spec = saved.get("spec") if isinstance(saved, dict) else None
    if not isinstance(spec, dict):
        raise StrategyValidationError("诊断证据对应的策略版本快照不存在")
    declared, behavioral, hash_schema = _diagnosis_spec_hash_identity(saved)
    if (declared != expected_spec_sha256
            or behavioral != expected_behavioral_spec_sha256
            or hash_schema != expected_hash_schema):
        raise StrategyValidationError("策略版本快照与诊断证据规格哈希不一致")
    return _experiment_spec_payload(spec)


def _load_diagnosis_evidence(value: Any) -> dict[str, Any]:
    path, relative = _diagnosis_evidence_path(value)
    evidence = _read_json(path)
    report = evidence.get("report") if isinstance(evidence, dict) else None
    strategy_id = _safe_id(evidence.get("strategy_id")) if evidence else None
    try:
        version = int(evidence.get("strategy_version"))
    except (TypeError, ValueError):
        version = 0
    result_hash = str(evidence.get("result_spec_sha256") or "")
    report_hash = str(evidence.get("report_sha256") or "")
    if (evidence.get("schema_version") != 2 or not strategy_id or version < 1
            or len(result_hash) != 64 or len(report_hash) != 64
            or not isinstance(report, dict)):
        raise StrategyValidationError("诊断证据结构无效")
    encoded_report = json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False, default=str,
    ).encode("utf-8")
    if hashlib.sha256(encoded_report).hexdigest() != report_hash:
        raise StrategyValidationError("诊断证据报告哈希不一致")
    expected_name = f"v{version}_{result_hash[:12]}_{report_hash[:16]}.json"
    if path.name != expected_name:
        raise StrategyValidationError("诊断证据文件名与冻结身份不一致")

    hash_schema = evidence.get("spec_hash_schema")
    behavioral_hash = str(evidence.get("behavioral_spec_sha256") or "")
    legacy_hypothesis_id = evidence.get("legacy_hypothesis_id")
    legacy_test_id = evidence.get("legacy_test_id")
    if hash_schema is None:
        if behavioral_hash or legacy_hypothesis_id is not None or legacy_test_id is not None:
            raise StrategyValidationError("legacy 诊断证据哈希身份不完整")
        behavioral_hash = result_hash
    elif hash_schema == _LEGACY_MISSING_ENTRY_RULE_HASH_SCHEMA:
        if (len(behavioral_hash) != 64
                or legacy_hypothesis_id != f"strategy::{result_hash[:24]}"):
            raise StrategyValidationError("legacy 诊断证据哈希身份无效")
        try:
            legacy_test_id = int(legacy_test_id)
        except (TypeError, ValueError) as exc:
            raise StrategyValidationError("legacy 诊断证据检验身份无效") from exc
        if legacy_test_id < 1:
            raise StrategyValidationError("legacy 诊断证据检验身份无效")
    else:
        raise StrategyValidationError("诊断证据规格哈希 schema 无效")
    spec = _strategy_version_spec(
        strategy_id, version, result_hash,
        expected_behavioral_spec_sha256=behavioral_hash,
        expected_hash_schema=hash_schema,
    )

    online = report.get("historical_online_test") or {}
    evaluate = report.get("evaluate") or {}
    diagnose = report.get("diagnose") or {}
    input_values = (
        online.get("global_data_key"),
        (evaluate.get("spec") or {}).get("research_input_key"),
        (diagnose.get("spec") or {}).get("research_input_key"),
    )
    input_keys = {str(value) for value in input_values if value}
    epoch = str(online.get("global_fdr_epoch") or "")
    if hash_schema is not None and (
        online.get("global_hypothesis_id") != legacy_hypothesis_id
        or online.get("global_test_id") != legacy_test_id
    ):
        raise StrategyValidationError("legacy 诊断证据与冻结全局检验不一致")
    report_diagnosed_at = diagnose.get("diagnosed_at")
    if any(not value for value in input_values) or len(input_keys) != 1 or not epoch:
        raise StrategyValidationError("诊断证据缺少唯一 research input 或 epoch")
    if not report_diagnosed_at or evidence.get("diagnosed_at") != report_diagnosed_at:
        raise StrategyValidationError("诊断证据时间身份不一致")
    audit = diagnose.get("execution_audit")
    audit_schema = audit.get("schema_version") if isinstance(audit, dict) else None
    engine_version = audit.get("engine_version") if isinstance(audit, dict) else None
    audit_days = audit.get("days") if isinstance(audit, dict) else None
    if audit_schema != 3 or engine_version != "factor_backtest_v3":
        raise StrategyValidationError("诊断证据执行审计 schema 或引擎版本无效")
    if not isinstance(audit_days, list) or not audit_days:
        raise StrategyValidationError("诊断证据缺少成功执行的逐日账本")
    execution_dates = [
        [str(day.get("date") or ""), str(day.get("next_date") or "")]
        for day in audit_days if isinstance(day, dict)
    ]
    if len(execution_dates) != len(audit_days) or any(
        not date or not next_date for date, next_date in execution_dates
    ):
        raise StrategyValidationError("诊断证据执行日期序列无效")
    execution_dates_sha256 = hashlib.sha256(json.dumps(
        execution_dates, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()
    return {
        "path": path,
        "relative_path": relative,
        "sha256": _sha256(path),
        "schema_version": int(evidence["schema_version"]),
        "strategy_id": strategy_id,
        "strategy_version": version,
        "result_spec_sha256": result_hash,
        "behavioral_spec_sha256": behavioral_hash,
        "spec_hash_schema": hash_schema,
        "diagnosed_at": evidence.get("diagnosed_at"),
        "report": report,
        "spec": spec,
        "research_input_key": next(iter(input_keys)),
        "research_epoch": epoch,
        "execution_audit_schema": audit_schema,
        "execution_engine_version": engine_version,
        "execution_dates_sha256": execution_dates_sha256,
    }


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _experiment_metrics(evidence: dict[str, Any]) -> dict[str, float | None]:
    report = evidence["report"]
    diagnose = report.get("diagnose") or {}
    oos = diagnose.get("oos_metrics") or {}
    shanghai = (diagnose.get("multi_benchmark") or {}).get("sh000001") or {}
    audit = diagnose.get("execution_audit") or {}
    summary = audit.get("summary") or {}
    signals = audit.get("signals") or []
    days = audit.get("days") or []
    risk = diagnose.get("risk_control") or (report.get("evaluate") or {}).get(
        "risk_control", {},
    )
    historical_risk = (risk.get("windows") or {}).get("historical_test") or {}

    traded = sum(
        _finite_number(day.get("traded_notional")) or 0.0
        for day in days if isinstance(day, dict)
    ) if days else None
    turnovers = [
        number for number in (
            _finite_number(day.get("turnover"))
            for day in days if isinstance(day, dict)
        ) if number is not None
    ]
    positions = [
        float(len(day.get("holdings") or []))
        for day in days if isinstance(day, dict)
    ]
    cash_ratios = []
    for day in days:
        if not isinstance(day, dict):
            continue
        cash = _finite_number(day.get("cash"))
        nav = _finite_number(day.get("nav_after_trade"))
        if cash is not None and nav and nav > 0:
            cash_ratios.append(cash / nav * 100.0)

    return {
        "annual_return_pct": _finite_number(oos.get("annual_return_pct")),
        "shanghai_excess_pct": _finite_number(shanghai.get("excess_annual_pct")),
        "sharpe": _finite_number(oos.get("sharpe")),
        "calmar": _finite_number(oos.get("calmar")),
        "max_drawdown_pct": _finite_number(oos.get("max_drawdown_pct")),
        "realized_volatility_pct": _finite_number(
            oos.get("realized_volatility_pct")
        ),
        "win_rate_pct": _finite_number(oos.get("win_rate_pct")),
        "total_fees_yuan": _finite_number(summary.get("total_fees")),
        "traded_notional_yuan": traded,
        "average_turnover": (sum(turnovers) / len(turnovers) if turnovers else None),
        "average_positions": (sum(positions) / len(positions) if positions else None),
        "average_gross_exposure_pct": _finite_number(
            oos.get("average_gross_exposure_pct")
        ),
        "average_cash_pct": (
            _finite_number(oos.get("average_cash_weight_pct"))
            if oos.get("average_cash_weight_pct") is not None
            else (sum(cash_ratios) / len(cash_ratios) if cash_ratios else None)
        ),
        "minimum_cash_weight_pct": _finite_number(
            oos.get("minimum_cash_weight_pct")
        ),
        "maximum_single_weight_pct": _finite_number(
            oos.get("maximum_single_weight_pct")
        ),
        "risk_on_dates": _finite_number(historical_risk.get("risk_on_dates")),
        "risk_off_dates": _finite_number(historical_risk.get("risk_off_dates")),
        "entry_order_fill_rate_pct": (
            (_finite_number(summary.get("entry_order_fill_rate")) * 100.0)
            if _finite_number(summary.get("entry_order_fill_rate")) is not None
            else None
        ),
        "entry_order_count": (
            _finite_number(summary.get("entry_order_count"))
            if summary.get("entry_order_count") is not None else float(len(signals))
        ),
        "entry_order_cancelled_count": (
            _finite_number(summary.get("entry_order_cancelled_count"))
            if summary.get("entry_order_cancelled_count") is not None
            else float(sum(
                1 for row in signals
                if isinstance(row, dict) and row.get("status") == "unfilled"
            ))
        ),
        "previous_close_limit_cancelled_count": (
            _finite_number(summary.get("previous_close_limit_cancelled_count"))
            if summary.get("previous_close_limit_cancelled_count") is not None
            else float(sum(
                1 for row in signals if isinstance(row, dict)
                and row.get("reason") == "previous_close_limit_not_filled"
            ))
        ),
    }


def _normalized_experiment_hypothesis(
    value: Any, *, metrics: dict[str, Any],
) -> dict[str, Any]:
    """Validate client provenance; only the server registration grants timing."""
    if value is None:
        raise StrategyValidationError("候选运行前必须冻结实验 hypothesis")
    if not isinstance(value, dict):
        raise StrategyValidationError("实验 hypothesis 必须是对象")
    allowed = {
        "hypothesis_id", "source_gate", "expected_metric", "direction",
        "success_threshold", "cost_seconds", "cost_scope",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise StrategyValidationError(
            f"实验 hypothesis 不支持字段: {', '.join(unknown)}"
        )
    hypothesis_id = str(value.get("hypothesis_id") or "")
    valid_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
    if not 6 <= len(hypothesis_id) <= 128 or any(
        character not in valid_chars for character in hypothesis_id
    ):
        raise StrategyValidationError("hypothesis_id 必须为6至128位稳定标识")
    source_gate = value.get("source_gate")
    if not isinstance(source_gate, dict) or not source_gate.get("status"):
        raise StrategyValidationError("实验必须声明 source_gate.status")
    if source_gate.get("eligible_for_confirmation") is True:
        raise StrategyValidationError(
            "不能接受客户端自证 confirmation；当前实验只能是探索性证据"
        )
    try:
        encoded_source_gate = json.dumps(
            source_gate, ensure_ascii=False, separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise StrategyValidationError("source_gate 必须是有限 JSON 对象") from exc
    if len(encoded_source_gate.encode("utf-8")) > 4096:
        raise StrategyValidationError("source_gate 最大4096字节")
    expected_metric = str(value.get("expected_metric") or "")
    if expected_metric not in metrics:
        raise StrategyValidationError(f"不支持的实验目标指标: {expected_metric}")
    direction = str(value.get("direction") or "")
    if direction not in {"increase", "decrease"}:
        raise StrategyValidationError("实验 direction 仅支持 increase 或 decrease")
    threshold = _finite_number(value.get("success_threshold"))
    if threshold is None or threshold < 0:
        raise StrategyValidationError("success_threshold 必须是非负有限数")
    cost_seconds = _finite_number(value.get("cost_seconds", 0.0))
    if cost_seconds is None or not 0 <= cost_seconds <= 31_536_000:
        raise StrategyValidationError("cost_seconds 必须在0至31536000之间")
    cost_scope = str(value.get("cost_scope") or "not_recorded")
    if cost_scope not in {
        "not_recorded", "candidate_wall_clock", "server_wall_clock",
    }:
        raise StrategyValidationError("cost_scope 无效")
    return {
        "hypothesis_id": hypothesis_id,
        "source_gate": json.loads(encoded_source_gate),
        "expected_metric": expected_metric,
        "direction": direction,
        "success_threshold": float(threshold),
        "cost_seconds": round(float(cost_seconds), 6),
        "cost_scope": cost_scope,
    }


def _experiment_decision(
    hypothesis: dict[str, Any], metrics: dict[str, Any],
) -> tuple[str, str]:
    """Classify the same-sample comparison; it can never confirm a hypothesis."""
    row = metrics[hypothesis["expected_metric"]]
    delta = _finite_number(row.get("delta"))
    if delta is None:
        return (
            "inconclusive_missing_metric",
            "目标指标不可比；停止解释本次差值，先修复同口径证据。",
        )
    threshold = float(hypothesis["success_threshold"])
    supported = (
        delta >= threshold
        if hypothesis["direction"] == "increase"
        else delta <= -threshold
    )
    if supported:
        return (
            "exploratory_support",
            "同历史样本仅探索性支持；保持规格冻结，等待配对基线的独立前向证据。",
        )
    return (
        "exploratory_reject",
        "同历史样本仅探索性否决；不得事后改阈值，继续研究须重新先验登记。",
    )


def _experiment_registration_identity(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: payload.get(key) for key in (
            "baseline", "candidate_spec", "candidate_spec_sha256", "change",
            "context", "hypothesis", "confirmation_policy",
        )
    }


def _experiment_registration_path(registration_id: str) -> Path:
    return (
        ROOT / "research" / "strategy_experiments" / "registrations"
        / f"{registration_id}.json"
    )


def _load_experiment_registration(value: Any) -> dict[str, Any]:
    registration_id = str(value or "")
    if (
        not registration_id.startswith("reg-")
        or len(registration_id) != 28
        or any(character not in "0123456789abcdef" for character in registration_id[4:])
    ):
        raise StrategyValidationError("实验 registration_id 无效")
    path = _experiment_registration_path(registration_id)
    payload = _read_json(path)
    declared = str(payload.get("registration_body_sha256") or "")
    body = {
        key: item for key, item in payload.items()
        if key != "registration_body_sha256"
    }
    expected_id = "reg-" + _payload_sha256(
        _experiment_registration_identity(payload)
    )[:24]
    if (
        payload.get("schema_version") != 1
        or payload.get("evidence_stage")
        != "single_variable_strategy_experiment_registration"
        or payload.get("registration_id") != registration_id
        or expected_id != registration_id
        or len(declared) != 64
        or _payload_sha256(body) != declared
    ):
        raise StrategyValidationError("实验 registration 证据缺失、损坏或身份不一致")
    return {
        **payload,
        "evidence_path": str(path.relative_to(ROOT)),
        "evidence_sha256": _sha256(path),
    }


def register_strategy_experiment(
    baseline_evidence: Any, candidate_spec: Any, hypothesis: Any,
) -> dict[str, Any]:
    """Freeze one single-variable experiment before any candidate diagnosis."""
    baseline = _load_diagnosis_evidence(baseline_evidence)
    normalized_candidate = _experiment_spec_payload(validate_spec(candidate_spec))
    changes = _experiment_leaf_changes(baseline["spec"], normalized_candidate)
    if len(changes) != 1:
        raise StrategyValidationError(
            f"单变量实验必须恰好改变一个叶子字段；当前改变 {len(changes)} 个"
        )
    candidate_spec_sha256 = result_spec_sha256(normalized_candidate)
    diagnosis_root = ROOT / "research" / "strategy_diagnostics"
    for path in diagnosis_root.glob(
        f"*/v*_{candidate_spec_sha256[:12]}_*.json"
    ):
        prior = _read_json(path)
        if candidate_spec_sha256 in {
            prior.get("result_spec_sha256"),
            prior.get("behavioral_spec_sha256"),
        }:
            raise StrategyValidationError(
                "候选规格已有诊断证据；不得事后登记为先验实验"
            )

    registered_at = _now()
    try:
        if datetime.fromisoformat(str(baseline["diagnosed_at"])) > datetime.fromisoformat(
            registered_at
        ):
            raise StrategyValidationError("基线证据形成时间晚于实验登记时间")
    except (TypeError, ValueError) as exc:
        raise StrategyValidationError("实验登记或基线证据时间无效") from exc
    hypothesis_row = _normalized_experiment_hypothesis(
        hypothesis, metrics=_experiment_metrics(baseline),
    )
    context = {
        "research_input_key": baseline["research_input_key"],
        "research_epoch": baseline["research_epoch"],
        "diagnosis_schema_version": baseline["schema_version"],
        "execution_audit_schema": baseline["execution_audit_schema"],
        "execution_engine_version": baseline["execution_engine_version"],
        "execution_dates_sha256": baseline["execution_dates_sha256"],
        "strategy_evaluator_fingerprint": strategy_evaluator_fingerprint(),
    }
    core = {
        "baseline": {
            "evidence_path": baseline["relative_path"],
            "evidence_sha256": baseline["sha256"],
            "strategy_id": baseline["strategy_id"],
            "strategy_version": baseline["strategy_version"],
            "result_spec_sha256": baseline["result_spec_sha256"],
        },
        "candidate_spec": normalized_candidate,
        "candidate_spec_sha256": candidate_spec_sha256,
        "change": changes[0],
        "context": context,
        "hypothesis": hypothesis_row,
        "confirmation_policy": {
            "historical_comparison": "exploratory_only",
            "independent_forward_required": True,
            "matched_baseline_required": True,
            "global_multiple_testing_required": True,
            "global_multiple_testing_method": "LOND-dependent",
            "compatible_forward_runner": None,
        },
    }
    registration_id = "reg-" + _payload_sha256(core)[:24]
    path = _experiment_registration_path(registration_id)
    if path.is_file():
        return _load_experiment_registration(registration_id)
    payload = {
        "schema_version": 1,
        "evidence_stage": "single_variable_strategy_experiment_registration",
        "registration_id": registration_id,
        "registered_at": registered_at,
        **core,
    }
    payload["registration_body_sha256"] = _payload_sha256(payload)
    _immutable_json(path, payload)
    return _load_experiment_registration(registration_id)


def create_strategy_experiment(
    registration_id: Any, candidate_evidence: Any, hypothesis: Any = None,
) -> dict[str, Any]:
    """Compare history after preregistration; confirmation stays unreachable."""
    if hypothesis is not None:
        raise StrategyValidationError("比较阶段不得修改已冻结 hypothesis")
    registration = _load_experiment_registration(registration_id)
    baseline = _load_diagnosis_evidence(
        (registration.get("baseline") or {}).get("evidence_path")
    )
    candidate = _load_diagnosis_evidence(candidate_evidence)
    baseline_ref = registration["baseline"]
    if (
        baseline["sha256"] != baseline_ref.get("evidence_sha256")
        or baseline["result_spec_sha256"] != baseline_ref.get("result_spec_sha256")
    ):
        raise StrategyValidationError("基线证据与不可变 registration 不一致")
    context = registration["context"]
    actual_context = {
        "research_input_key": candidate["research_input_key"],
        "research_epoch": candidate["research_epoch"],
        "diagnosis_schema_version": candidate["schema_version"],
        "execution_audit_schema": candidate["execution_audit_schema"],
        "execution_engine_version": candidate["execution_engine_version"],
        "execution_dates_sha256": candidate["execution_dates_sha256"],
    }
    for key, value in actual_context.items():
        if context.get(key) != value:
            raise StrategyValidationError(f"候选证据 {key} 与 registration 不一致")
    if context.get("strategy_evaluator_fingerprint") != strategy_evaluator_fingerprint():
        raise StrategyValidationError("策略执行身份已变化；旧 registration 不可继续")
    if (
        _experiment_spec_payload(candidate["spec"])
        != registration["candidate_spec"]
        or result_spec_sha256(candidate["spec"])
        != registration["candidate_spec_sha256"]
    ):
        raise StrategyValidationError("候选诊断规格与先验 registration 不一致")
    try:
        if datetime.fromisoformat(str(candidate["diagnosed_at"])) < datetime.fromisoformat(
            str(registration["registered_at"])
        ):
            raise StrategyValidationError("候选证据并非在 registration 之后形成")
    except (TypeError, ValueError) as exc:
        raise StrategyValidationError("registration 或候选证据时间无效") from exc

    change = registration["change"]
    baseline_metrics = _experiment_metrics(baseline)
    candidate_metrics = _experiment_metrics(candidate)
    metrics = {}
    for name in baseline_metrics:
        before = baseline_metrics[name]
        after = candidate_metrics[name]
        delta = None if before is None or after is None else round(after - before, 6)
        metrics[name] = {
            "baseline": before,
            "candidate": after,
            "delta": delta,
        }

    comparison_identity = {
        "registration_id": registration["registration_id"],
        "candidate_sha256": candidate["sha256"],
    }
    hypothesis_row = registration["hypothesis"]
    decision_category, next_action = _experiment_decision(
        hypothesis_row, metrics,
    )
    identity = {
        **comparison_identity,
        "hypothesis_id": hypothesis_row["hypothesis_id"],
    }
    experiment_id = "exp-" + hashlib.sha256(json.dumps(
        identity, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()[:24]
    payload = {
        "schema_version": 3,
        "evidence_stage": "single_variable_strategy_experiment",
        "experiment_id": experiment_id,
        "compared_at": candidate.get("diagnosed_at"),
        "registration": {
            "registration_id": registration["registration_id"],
            "evidence_path": registration["evidence_path"],
            "evidence_sha256": registration["evidence_sha256"],
            "registered_at": registration["registered_at"],
        },
        "baseline": baseline_ref,
        "candidate": {
            "evidence_path": candidate["relative_path"],
            "evidence_sha256": candidate["sha256"],
            "strategy_id": candidate["strategy_id"],
            "strategy_version": candidate["strategy_version"],
            "result_spec_sha256": candidate["result_spec_sha256"],
        },
        "context": context,
        "change": change,
        "metrics": metrics,
        **hypothesis_row,
        "decision_category": decision_category,
        "confirmation_status": "awaiting_independent_forward",
        "confirmation_eligible": False,
        "historical_evidence_role": "exploratory_same_sample_comparison",
        "global_multiple_testing": {
            "required": True,
            "method": "LOND-dependent",
            "status": "not_observed",
            "p_value": None,
            "test_id": None,
        },
        "independent_forward": {
            "required": True,
            "matched_baseline_required": True,
            "compatible_runner": None,
            "paper_forward_reused": False,
        },
        "next_action": next_action,
        "interpretation": "historical_exploratory_only_no_confirmation_no_promotion",
    }
    path = ROOT / "research" / "strategy_experiments" / f"{experiment_id}.json"
    if path.is_file():
        existing = _read_json(path)
        stable_keys = (
            "schema_version", "evidence_stage", "experiment_id", "baseline",
            "registration", "candidate", "context", "change", "metrics", "hypothesis_id",
            "source_gate", "expected_metric", "direction",
            "success_threshold", "decision_category", "next_action",
            "confirmation_status", "confirmation_eligible",
            "historical_evidence_role", "global_multiple_testing",
            "independent_forward", "interpretation",
        )
        if all(existing.get(key) == payload.get(key) for key in stable_keys):
            return {
                **existing,
                "evidence_path": str(path.relative_to(ROOT)),
                "evidence_sha256": _sha256(path),
            }
    _immutable_json(path, payload)
    return {
        **payload,
        "evidence_path": str(path.relative_to(ROOT)),
        "evidence_sha256": _sha256(path),
    }


def _recent_strategy_experiments(limit: int = 20) -> list[dict[str, Any]]:
    directory = ROOT / "research" / "strategy_experiments"
    rows: list[dict[str, Any]] = []
    if directory.is_dir():
        paths = sorted(
            directory.glob("exp-*.json"),
            key=lambda candidate: candidate.stat().st_mtime,
            reverse=True,
        )[:max(limit * 2, limit)]
        for path in paths:
            item = _read_json(path)
            if (item.get("schema_version") not in {1, 2, 3}
                    or item.get("evidence_stage")
                    != "single_variable_strategy_experiment"
                    or item.get("experiment_id") != path.stem):
                continue
            rows.append({
                **item,
                "evidence_path": str(path.relative_to(ROOT)),
                "evidence_sha256": _sha256(path),
            })
    rows.sort(key=lambda item: str(item.get("compared_at") or ""), reverse=True)
    return rows[:limit]


def set_promotion_action(
    strategy_id: str,
    action: str,
    *,
    run_id: Any = None,
) -> dict[str, Any]:
    """晋升/否决动作（纸面前向观察级，不触实盘）。

    - paper_observe_on/off：设置/取消纸面前向观察标记（任何策略可设，不授予资格）
    - unlock/lock：当前无策略前向终测 runner，unlock 一律拒绝；lock 可锁回。
    """
    path = STRATEGIES_DIR / f"{strategy_id}.json"
    if not path.is_file():
        raise StrategyValidationError(f"策略不存在: {strategy_id}")
    forward_handle = None
    if action in {"paper_observe_on", "paper_observe_off"}:
        forward_handle = _acquire_forward_lock()
        if forward_handle is None:
            raise StrategyValidationError("纸面前向runner正在推进，请稍后重试")
    try:
        saved = _read_json(path)
        promo = saved.get("promotion") or _promotion_block()
        if action == "paper_observe_on":
            observation = _freeze_paper_observation(
                saved,
                run_id=run_id,
                existing_promotion=promo,
            )
            promo.update({
                "paper_observe": True,
                "paper_observation_id": observation["observation_id"],
                "paper_observation_run_id": observation["origin_run_id"],
                "paper_observation_started_at": observation["observed_at"],
                "paper_observation_stopped_at": None,
            })
        elif action == "paper_observe_off":
            if run_id is not None:
                safe_run = _safe_run_id(run_id)
                if safe_run is None:
                    raise StrategyValidationError("回测运行ID无效")
                current_run = promo.get("paper_observation_run_id")
                if current_run and current_run != safe_run:
                    raise StrategyValidationError("该回测不是当前纸面观察对象")
            promo["paper_observe"] = False
            promo["paper_observation_stopped_at"] = _now()
        elif action == "unlock":
            raise StrategyValidationError(
                "当前没有策略前向终测 runner；历史确认通过也不能解锁。"
            )
        elif action == "lock":
            promo["unlocked"] = False
            promo["unlocked_at"] = None
        else:
            raise StrategyValidationError(f"未知晋升动作: {action}")
        saved["promotion"] = promo
        saved["recommendation_eligible"] = _promotion_eligible(promo)
        _atomic_json(path, saved)
        return saved
    finally:
        _release_forward_lock(forward_handle)


def validate_spec(payload: dict[str, Any], *, require_runnable: bool = True) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise StrategyValidationError("策略规格必须是对象")
    allowed = {
        "strategy_id", "name", "holding_days", "top_n",
        "one_way_cost_bps", "rebal_buffer", "risk_rule", "entry_rule",
        "exposure_rule", "factors",
        "account_capital", "compose_method",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise StrategyValidationError(f"不支持的策略字段: {', '.join(unknown)}")
    name = str(payload.get("name") or "").strip()
    if not 1 <= len(name) <= 60:
        raise StrategyValidationError("策略名称需为1至60个字符")
    try:
        holding_days = int(payload.get("holding_days"))
        top_n = int(payload.get("top_n"))
        cost = int(payload.get("one_way_cost_bps"))
        rebal_buffer = int(payload.get(
            "rebal_buffer", _configured_rebal_buffer(),
        ))
    except (TypeError, ValueError) as exc:
        raise StrategyValidationError(
            "持有期、持仓数、成本和换仓缓冲必须是整数"
        ) from exc
    # M9（2026-08-16）：扩到 20 日——统一引擎/本地路径本就支持任意持有期，且 20 日是
    # 「小市值+反转+低换手」达标 ≥20% 年化所需持有期（见 research/2026-08-16_annualized20_grid.md）。
    if holding_days not in (5, 10, 20):
        raise StrategyValidationError("持有期仅支持5、10、20个交易日")
    if not 10 <= top_n <= 100:
        raise StrategyValidationError("持仓数必须在10至100只之间")
    if not 0 <= cost <= 50:
        raise StrategyValidationError(
            "单边滑点必须在0至50BP之间；佣金、印花税、过户费另加"
        )
    if not 0 <= rebal_buffer <= 20:
        raise StrategyValidationError("换仓缓冲必须在0至20个名次之间")
    # M4.1 资金量（影响 A股 100 股整手落地：20万配 top_N≥30 会退化空仓，回测失真）
    try:
        capital = float(payload.get("account_capital", 200_000.0))
    except (TypeError, ValueError) as exc:
        raise StrategyValidationError("资金量必须是数字") from exc
    if not math.isfinite(capital) or not 100_000 <= capital <= 50_000_000:
        raise StrategyValidationError("资金量必须在10万至5000万之间")
    risk_rule = str(payload.get("risk_rule") or "none")
    if risk_rule not in {"none", "breadth_majority"}:
        raise StrategyValidationError("不支持的风险开关")
    entry_rule = str(payload.get("entry_rule") or "none")
    if entry_rule not in {
        "none", "previous_close_limit_no_chase_v1",
        "previous_close_gap_2pct_no_chase_v1",
    }:
        raise StrategyValidationError("不支持的买点规则")
    exposure_rule = str(payload.get("exposure_rule") or "none")
    if exposure_rule not in {"none", "market_vol_target_18_v1"}:
        raise StrategyValidationError("不支持的连续市场波动目标仓位规则")
    compose_method = str(payload.get("compose_method") or "rank_weighted_v1")
    if compose_method not in {"rank_weighted_v1", "ic_weighted", "corr_ic_weighted"}:
        raise StrategyValidationError("不支持的因子合成方式")
    raw_factors = payload.get("factors")
    if not isinstance(raw_factors, dict):
        raise StrategyValidationError("因子权重必须是对象")
    # M7：统一因子宇宙（内置 13 + 我的配方 156）
    catalog = _full_factor_catalog()
    unknown_factors = sorted(set(raw_factors) - set(catalog))
    if unknown_factors:
        raise StrategyValidationError(f"不支持的因子: {', '.join(unknown_factors)}")
    factors: dict[str, float] = {}
    for factor in sorted(set(raw_factors) & set(catalog)):
        try:
            weight = float(raw_factors.get(factor, 0.0))
        except (TypeError, ValueError) as exc:
            raise StrategyValidationError(f"{factor} 权重必须是数字") from exc
        if not math.isfinite(weight) or not -3 <= weight <= 3:
            raise StrategyValidationError(f"{factor} 权重必须在-3至3之间")
        # M7：只保留非零权重（DSL/存档精简；下游按权重过滤，语义不变）
        if abs(weight) >= 1e-12:
            factors[factor] = round(weight, 6)
    if not factors:
        raise StrategyValidationError("至少需要一个非零因子权重")
    blockers = _factor_run_blockers(list(factors)) if require_runnable else []
    if blockers:
        detail = "；".join(
            f"{row['factor_id']}[{row['code']}] {row['message']}"
            for row in blockers
        )
        raise StrategyValidationError(f"因子不可用于新的策略运行：{detail}")
    normalized = {
        "name": name,
        "holding_days": holding_days,
        "top_n": top_n,
        "one_way_cost_bps": cost,
        "rebal_buffer": rebal_buffer,
        "risk_rule": risk_rule,
        "entry_rule": entry_rule,
        "exposure_rule": exposure_rule,
        "account_capital": capital,
        "compose_method": compose_method,
        "factors": factors,
    }
    strategy_id = _safe_id(payload.get("strategy_id"))
    if payload.get("strategy_id") and strategy_id is None:
        raise StrategyValidationError("策略ID无效")
    if strategy_id:
        normalized["strategy_id"] = strategy_id
    return normalized


def validate_draft_spec(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate editable syntax; execution callers still use validate_spec.

    A saved draft is not source/PIT approval, research admission or a trade.
    Unknown factors, zero weights and invalid fields/ranges stay rejected.
    """
    return validate_spec(payload, require_runnable=False)


def save_strategy(payload: dict[str, Any]) -> dict[str, Any]:
    from factor_system import config as fconfig

    spec = validate_draft_spec(payload)
    strategy_id = spec.pop("strategy_id", None) or (
        STRATEGY_ID_PREFIX + secrets.token_hex(6)
    )
    path = STRATEGIES_DIR / f"{strategy_id}.json"
    previous = _read_json(path) if path.is_file() else {}
    previous_version = int(previous.get("version") or 0)
    current_spec_hash = result_spec_sha256(spec)
    previous_spec_hash = (
        previous.get("result_spec_sha256")
        or (result_spec_sha256(previous.get("spec") or {}) if previous else None)
    )
    result_spec_changed = bool(
        previous and previous_spec_hash != current_spec_hash
    )
    if previous:
        _atomic_json(
            STRATEGIES_DIR / "history" / f"{strategy_id}_v{previous_version}.json",
            previous,
        )
    legacy_promotions = list(previous.get("legacy_promotions") or [])
    if result_spec_changed:
        legacy_promotions.append({
            "strategy_version": previous_version,
            "result_spec_sha256": previous_spec_hash,
            "invalidated_at": _now(),
            "invalidated_reason": "影响回测结果的策略规格已变化",
            "promotion": {**(previous.get("promotion") or _promotion_block(
                previous_spec_hash,
            ))},
        })
    elif (previous.get("promotion") or {}).get("unified_verdict") == "PASS":
        legacy_promotions.append({
            "strategy_version": previous_version,
            "result_spec_sha256": previous_spec_hash,
            "invalidated_at": _now(),
            "invalidated_reason": "当前没有策略前向终测 runner；旧 PASS 证据失效",
            "promotion": {**previous["promotion"]},
        })

    saved = {
        "version": previous_version + 1,
        "strategy_id": strategy_id,
        "saved_at": _now(),
        "research_epoch": fconfig.RESEARCH_EPOCH,
        "spec": spec,
        "result_spec_sha256": current_spec_hash,
        "previous_result_spec_sha256": previous_spec_hash,
        "result_spec_changed": result_spec_changed,
        "legacy_promotions": legacy_promotions,
        "execution_boundary": "safe_dsl_only_no_eval_no_exec",
        "promotion": _promotion_block(current_spec_hash),
        "recommendation_eligible": False,
    }
    # 只有结果规格哈希完全相同时才沿用 promotion；改名不影响结果，允许保留。
    if previous.get("promotion") and not result_spec_changed:
        saved["promotion"] = _audit_safe_promotion(previous["promotion"])
        saved["promotion"]["result_spec_sha256"] = current_spec_hash
    elif result_spec_changed:
        saved["promotion"].update({
            "unified_verdict": "INVALIDATED",
            "verdict_stage": "spec_change",
            "gate_reason": "影响回测结果的策略规格已变化；旧证据仅保留在 legacy_promotions",
            "invalidated_at": _now(),
        })
    saved["recommendation_eligible"] = _promotion_eligible(saved["promotion"])
    _atomic_json(path, saved)
    return saved


@lru_cache(maxsize=32)
def _sha256_for_stat(
    path_text: str,
    size: int,
    mtime_ns: int,
    inode: int,
) -> str:
    """Hash one immutable-looking file once per physical stat identity."""
    del size, mtime_ns, inode
    return _sha256(Path(path_text))


def _stable_file_sha256(path: Path) -> str:
    """Hash a file only if its identity is stable for the complete read."""
    for _attempt in range(2):
        try:
            before = path.stat()
        except OSError as exc:
            raise StrategyValidationError(f"策略研究输入不可读: {path}: {exc}") from exc
        identity = (
            int(before.st_size), int(before.st_mtime_ns), int(before.st_ino),
        )
        digest = _sha256_for_stat(str(path), *identity)
        try:
            after = path.stat()
        except OSError as exc:
            raise StrategyValidationError(f"策略研究输入在校验时消失: {path}") from exc
        if identity == (
            int(after.st_size), int(after.st_mtime_ns), int(after.st_ino),
        ):
            return digest
    raise StrategyValidationError(f"策略研究输入在哈希期间持续变化: {path}")


def _strategy_panel_evidence_paths(panel_key: str) -> tuple[Path, ...]:
    cache_path = factor_config.CACHE_DIR / panel_key
    if cache_path.parent.resolve() != factor_config.CACHE_DIR.resolve():
        raise StrategyValidationError("因子面板内容地址越出授权缓存目录")
    return (
        cache_path,
        INDEX_PATH,
        QUALITY_PATH,
        V4_MANIFEST_PATH,
        V4_CALENDAR_PATH,
        PIPELINE_PATH,
        FACTOR_DATA_PATH,
        FACTOR_V4_BRIDGE_PATH,
        FACTOR_COMPOSE_PATH,
    )


def _panel_source_state(panel: Any) -> dict[str, Any]:
    """Bind strategy research to the current audited shared Panel in memory."""
    from factor_system.data import PANEL_UNIVERSE_SCOPE, _panel_cache_path
    from factor_system.gate import research_input_key

    panel_key = str(getattr(panel, "_panel_cache_key", "") or "")
    current_panel_key = _panel_cache_path().name
    if panel_key != current_panel_key:
        raise StrategyValidationError(
            f"策略研发面板不是当前审计输入: {panel_key} -> {current_panel_key}"
        )
    if (
        not panel_key.startswith("panel_")
        or not panel_key.endswith((".panel", ".pkl"))
    ):
        raise StrategyValidationError("策略研发仅接受当前内容寻址审计面板")
    if not isinstance(getattr(panel, "signal_eligible", None), pd.DataFrame):
        raise StrategyValidationError("共享面板缺少 PIT 信号资格矩阵")
    if not isinstance(getattr(panel, "tradable", None), pd.DataFrame):
        raise StrategyValidationError("共享面板缺少可交易状态矩阵")
    if not isinstance(getattr(panel, "adj_open", None), pd.DataFrame):
        raise StrategyValidationError("共享面板缺少复权开盘价矩阵")

    dates = pd.DatetimeIndex(panel.close.index)
    if (
        dates.empty
        or dates.has_duplicates
        or not dates.is_monotonic_increasing
        or panel.close.shape != panel.signal_eligible.shape
    ):
        raise StrategyValidationError("共享面板日期或矩阵结构无效")

    quality = _read_json(QUALITY_PATH)
    manifest = _read_json(V4_MANIFEST_PATH)
    calendar = _read_json(V4_CALENDAR_PATH)
    pipeline = _read_json(PIPELINE_PATH)
    published_end = str((quality.get("query_window") or {}).get("end_date") or "")
    manifest_end = str((manifest.get("query") or {}).get("end_date") or "")
    calendar_end = str(calendar.get("end_date") or "")
    panel_end = dates[-1].date().isoformat()
    quality_ready = bool(
        quality.get("ready_for_factor_research") is True
        and quality.get("full_universe_ready") is True
        and quality.get("all_downloaded_files_passed") is True
        and int(quality.get("failed_files") or 0) == 0
    )
    if not quality_ready:
        raise StrategyValidationError("V4 日线全量质量门禁未通过")
    if not published_end or not (
        panel_end == published_end == manifest_end == calendar_end
    ):
        raise StrategyValidationError(
            "共享面板、V4清单、质量报告与交易日历截止日不一致"
        )

    evidence_paths = _strategy_panel_evidence_paths(panel_key)
    evidence_hashes = {
        str(path.relative_to(ROOT)): _stable_file_sha256(path)
        for path in evidence_paths
    }
    manifest_relative = str(V4_MANIFEST_PATH.relative_to(ROOT))
    audited_manifest_sha = str(
        (quality.get("input_fingerprints") or {}).get(
            "download_manifest_sha256"
        ) or ""
    )
    if audited_manifest_sha != evidence_hashes[manifest_relative]:
        raise StrategyValidationError("V4 下载清单未被当前质量报告审计")
    eligible = panel.signal_eligible.fillna(False).astype(bool)
    active_dates = eligible.any(axis=1)
    active_symbols = eligible.any(axis=0)
    if not bool(active_dates.any()) or not bool(active_symbols.any()):
        raise StrategyValidationError("共享面板没有可研究的 PIT 合格样本")
    cache_path = factor_config.CACHE_DIR / panel_key
    input_key = research_input_key(panel_key=panel_key)
    benchmark_sha = evidence_hashes[str(INDEX_PATH.relative_to(ROOT))]
    if input_key != f"{panel_key}:shanghai:{benchmark_sha}":
        raise StrategyValidationError("上证基准在策略输入冻结期间发生变化")
    return {
        "schema": "strategy_lab_shared_panel_snapshot_v2",
        "source_model": "factor_system_audited_content_addressed_panel",
        "panel_schema": panel_key.split("_", 2)[1],
        "universe_scope": json.loads(json.dumps(PANEL_UNIVERSE_SCOPE)),
        "research_epoch": factor_config.RESEARCH_EPOCH,
        "panel_rows": int(eligible.to_numpy(dtype=bool, copy=False).sum()),
        "symbols": int(active_symbols.sum()),
        "date_min": pd.Timestamp(active_dates.index[active_dates][0]).date().isoformat(),
        "date_max": panel_end,
        "eligible_date_max": pd.Timestamp(
            active_dates.index[active_dates][-1]
        ).date().isoformat(),
        "metadata_fingerprint": panel_key,
        "metadata_built_at": datetime.fromtimestamp(
            cache_path.stat().st_mtime, SHANGHAI,
        ).isoformat(timespec="seconds"),
        "panel_cache_key": panel_key,
        "panel_path": str(cache_path.relative_to(ROOT)),
        "panel_sha256": evidence_hashes[str(cache_path.relative_to(ROOT))],
        "research_input_key": input_key,
        "strategy_evaluator_fingerprint": strategy_evaluator_fingerprint(),
        "label_semantics": {
            "signal_time": "T日收盘后",
            "entry": "T+1首个计划开盘；不可交易则保持现金",
            "exit": "T+h收盘；停牌则顺延至其后首个可交易收盘",
            "explicit_delisting": "已入场且计划退出前显式退市按零回收",
        },
        "quality_generated_at": quality.get("generated_at"),
        "pipeline_generated_at": pipeline.get("generated_at"),
        "evidence_hashes": evidence_hashes,
    }


def _strategy_panel_snapshot() -> tuple[Any, dict[str, Any]]:
    from factor_system.data import _panel_cache_path, load_panel

    current_cache = _panel_cache_path()
    if not current_cache.is_file():
        raise StrategyValidationError(
            "当前审计共享面板尚未完成原子发布；原子发布前禁止策略回测"
        )
    panel = load_panel()
    return panel, _panel_source_state(panel)


def _source_state() -> dict[str, Any]:
    return _strategy_panel_snapshot()[1]


def _verify_source_snapshot(source: dict[str, Any]) -> None:
    """Fail closed if any source changed after the run began."""
    from factor_system.data import _panel_cache_path
    from factor_system.gate import research_input_key

    evidence = source.get("evidence_hashes") or {}
    if not evidence:
        raise StrategyValidationError("策略研究输入快照缺少证据哈希")
    for relative, expected in evidence.items():
        path = ROOT / str(relative)
        if _stable_file_sha256(path) != expected:
            raise StrategyValidationError(f"策略研究输入在运行期间变化: {relative}")
    current_panel_key = _panel_cache_path().name
    if current_panel_key != source.get("panel_cache_key"):
        raise StrategyValidationError(
            "策略研究面板在运行期间切换: "
            f"{source.get('panel_cache_key')} -> {current_panel_key}"
        )
    current_input_key = research_input_key(panel_key=current_panel_key)
    if current_input_key != source.get("research_input_key"):
        raise StrategyValidationError("策略研究完整输入身份在运行期间变化")


def _unified_diagnosis_payload(report: dict[str, Any]) -> dict[str, Any]:
    nested = report.get("unified_diagnosis") if isinstance(report, dict) else None
    return nested if isinstance(nested, dict) else report


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _declared_payload_hash_valid(payload: dict[str, Any], field: str) -> bool:
    declared = str(payload.get(field) or "")
    body = {key: value for key, value in payload.items() if key != field}
    return len(declared) == 64 and _canonical_sha256(body) == declared


def validate_unified_diagnosis_input(report: dict[str, Any]) -> str:
    """Require a successful diagnosis to still match the current full input."""
    from factor_system.data import _panel_cache_path
    from factor_system.gate import research_input_key

    payload = _unified_diagnosis_payload(report)
    process_evaluator = strategy_evaluator_fingerprint()
    report_evaluator = str(
        payload.get("strategy_evaluator_fingerprint")
        or report.get("strategy_evaluator_fingerprint")
        or ""
    )
    if report_evaluator != process_evaluator:
        raise StrategyValidationError(
            "统一诊断未绑定当前进程的策略执行指纹"
        )
    evaluate = payload.get("evaluate") or {}
    diagnose = payload.get("diagnose") or {}
    online = payload.get("historical_online_test") or {}
    values = (
        (evaluate.get("spec") or {}).get("research_input_key"),
        (diagnose.get("spec") or {}).get("research_input_key"),
        online.get("global_data_key"),
    )
    current_panel_key = _panel_cache_path().name
    current_input_key = research_input_key(panel_key=current_panel_key)
    if any(not value for value in values) or {
        str(value) for value in values
    } != {current_input_key}:
        raise StrategyValidationError(
            "统一诊断缓存与当前完整 research_input_key 不一致，拒绝发布"
        )
    execution_audit = diagnose.get("execution_audit")
    execution_hash = str(diagnose.get("execution_audit_sha256") or "")
    if not isinstance(execution_audit, dict) or (
        len(execution_hash) != 64
        or _canonical_sha256(execution_audit) != execution_hash
    ):
        raise StrategyValidationError("统一诊断执行审计哈希无效")
    curve = diagnose.get("historical_curve")
    if not isinstance(curve, dict) or curve.get("available") is not True:
        raise StrategyValidationError(
            "统一诊断未生成可发布的历史确认净值曲线"
        )
    if (
        curve.get("research_input_key") != current_input_key
        or curve.get("execution_audit_sha256") != execution_hash
        or not _declared_payload_hash_valid(curve, "curve_sha256")
    ):
        raise StrategyValidationError("统一诊断曲线与输入或执行审计绑定无效")
    points = curve.get("points")
    if not isinstance(points, list) or len(points) < 2:
        raise StrategyValidationError("统一诊断历史确认曲线点数不足")
    return current_input_key


def _unified_report_body_sha256(report: dict[str, Any]) -> str:
    return _canonical_sha256({
        key: value for key, value in report.items()
        if key not in {"integrity", "report_body_sha256"}
    })


def _unified_report_integrity(report: dict[str, Any]) -> dict[str, Any]:
    payload = _unified_diagnosis_payload(report)
    evaluate = payload.get("evaluate") or {}
    diagnose = payload.get("diagnose") or {}
    online = payload.get("historical_online_test") or {}
    source = report.get("source") or {}
    curve = diagnose.get("historical_curve") or {}
    input_key = str(report.get("research_input_key") or "")
    audit = diagnose.get("execution_audit")
    audit_hash = str(diagnose.get("execution_audit_sha256") or "")
    points = curve.get("points")
    checks = [
        {
            "label": "统一诊断报告 schema / version / 状态",
            "passed": (
                report.get("report_schema") == UNIFIED_DIAGNOSIS_REPORT_SCHEMA
                and report.get("version") == 1
                and report.get("status") == "unified_diagnosis_evidence_only"
            ),
        },
        {
            "label": "不可变 run_id",
            "passed": _safe_run_id(report.get("run_id")) == report.get("run_id"),
        },
        {
            "label": "research epoch",
            "passed": bool(
                report.get("research_epoch")
                and report.get("strategy_evaluator_fingerprint")
                == source.get("strategy_evaluator_fingerprint")
                == payload.get("strategy_evaluator_fingerprint")
            ),
        },
        {
            "label": "共享 Panel 输入身份",
            "passed": bool(
                input_key
                and source.get("research_input_key") == input_key
                and input_key.startswith(
                    f"{source.get('panel_cache_key')}:shanghai:"
                )
            ),
        },
        {
            "label": "evaluate 输入身份",
            "passed": (
                (evaluate.get("spec") or {}).get("research_input_key")
                == input_key
            ),
        },
        {
            "label": "diagnose 输入身份",
            "passed": (
                (diagnose.get("spec") or {}).get("research_input_key")
                == input_key
            ),
        },
        {
            "label": "LOND 观察输入身份",
            "passed": online.get("global_data_key") == input_key,
        },
        {
            "label": "统一执行审计哈希",
            "passed": bool(
                isinstance(audit, dict)
                and len(audit_hash) == 64
                and _canonical_sha256(audit) == audit_hash
            ),
        },
        {
            "label": "历史确认曲线可用",
            "passed": bool(
                curve.get("available") is True
                and isinstance(points, list) and len(points) >= 2
            ),
        },
        {
            "label": "曲线 research input 绑定",
            "passed": curve.get("research_input_key") == input_key,
        },
        {
            "label": "曲线执行审计绑定",
            "passed": curve.get("execution_audit_sha256") == audit_hash,
        },
        {
            "label": "曲线内容哈希",
            "passed": bool(
                isinstance(curve, dict)
                and _declared_payload_hash_valid(curve, "curve_sha256")
            ),
        },
        {
            "label": "报告主体哈希",
            "passed": (
                report.get("report_body_sha256")
                == _unified_report_body_sha256(report)
            ),
        },
    ]
    passed = sum(bool(row["passed"]) for row in checks)
    return {
        "passed": passed,
        "total": len(checks),
        "all_passed": passed == len(checks),
        "checks": checks,
    }


def _report_integrity(report: dict[str, Any]) -> dict[str, Any]:
    if report.get("report_schema") == UNIFIED_DIAGNOSIS_REPORT_SCHEMA:
        return _unified_report_integrity(report)
    checks: list[dict[str, Any]] = []
    for path_key, hash_key, label in (
        ("spec_path", "spec_sha256", "策略规格"),
        ("periods_path", "periods_sha256", "逐期收益"),
        ("selected_path", "selected_sha256", "逐期目标"),
        ("factor_ic_path", "factor_ic_sha256", "逐期因子IC"),
    ):
        relative = report.get(path_key)
        declared = report.get(hash_key)
        if not relative and not declared:
            continue
        path = ROOT / str(relative) if relative else None
        passed = bool(path and path.is_file() and declared and _sha256(path) == declared)
        checks.append({"label": label, "passed": passed})
    source_hashes = report.get("source_evidence_hashes") or {}
    for relative, declared in source_hashes.items():
        path = ROOT / str(relative)
        checks.append({
            "label": f"输入证据 {Path(str(relative)).name}",
            "passed": bool(path.is_file() and _sha256(path) == declared),
        })
    passed = sum(bool(row["passed"]) for row in checks)
    return {
        "passed": passed,
        "total": len(checks),
        "all_passed": bool(checks) and passed == len(checks),
        "checks": checks,
    }


def _split_pair(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    return str(value[0]), str(value[1])


def _current_epoch_eligibility(report: dict[str, Any]) -> tuple[bool, str | None]:
    """Only the current schema, epoch and strict three-way split can count as PASS."""
    if report.get("report_schema") == UNIFIED_DIAGNOSIS_REPORT_SCHEMA:
        if report.get("research_epoch") != factor_config.RESEARCH_EPOCH:
            return False, (
                "统一诊断报告 research epoch 已变化；仅保留不可变审计"
            )
        actual = _split_pair((report.get("splits") or {}).get("test"))
        expected = (
            str(factor_config.HISTORICAL_TEST_START),
            str(factor_config.HISTORICAL_TEST_END),
        )
        if actual != expected:
            return False, "统一诊断报告未绑定当前历史确认窗口"
        try:
            current_input_key = validate_unified_diagnosis_input(report)
        except StrategyValidationError as exc:
            return False, str(exc)
        if report.get("research_input_key") != current_input_key:
            return False, "统一诊断报告顶层输入身份无效"
        return True, None
    if report.get("report_schema") != BACKTEST_REPORT_SCHEMA:
        return False, (
            "旧报告未绑定当前 strict 回测 schema；仅保留审计，不计入当前历史确认 PASS"
        )
    if report.get("research_epoch") != factor_config.RESEARCH_EPOCH:
        return False, (
            "报告 research epoch 与当前 strict epoch 不一致；仅保留审计，不计入当前历史确认 PASS"
        )
    actual = report.get("splits") or {}
    for name in ("development", "validation", "test"):
        if _split_pair(actual.get(name)) != _split_pair(SPLITS[name]):
            return False, (
                f"报告 {name} 分段与当前 strict 三段不一致；仅保留审计，不计入当前历史确认 PASS"
            )
    return True, None


def _with_current_epoch_status(report: dict[str, Any]) -> dict[str, Any]:
    """Derive current-epoch truth without rewriting the immutable report file."""
    eligible, reason = _current_epoch_eligibility(report)
    recorded_pass = bool(report.get("historical_validation_gate_passed"))
    return {
        **report,
        "recorded_historical_validation_gate_passed": recorded_pass,
        "historical_validation_gate_passed": bool(recorded_pass and eligible),
        "current_epoch_eligible": eligible,
        "legacy_reason": reason,
    }


def _report_index_path(path: Path) -> Path:
    return path.parent / "_index" / path.name


def _report_evidence_ref(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return path.name


def _report_listing_payload(
    report: dict[str, Any], path: Path, *, identity_verified: bool,
) -> dict[str, Any]:
    """Keep status/list views independent from multi-megabyte evidence bodies."""
    stat = path.stat()
    metrics = report.get("metrics") or {}
    return {
        "index_schema": REPORT_INDEX_SCHEMA,
        "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "identity_verified": bool(identity_verified),
        "version": report.get("version"),
        "report_schema": report.get("report_schema"),
        "research_epoch": report.get("research_epoch"),
        "generated_at": report.get("generated_at"),
        "status": report.get("status"),
        "run_id": report.get("run_id") or path.stem,
        "strategy_id": report.get("strategy_id"),
        "spec": {"name": (report.get("spec") or {}).get("name")},
        "splits": report.get("splits") or {},
        "historical_validation_gate_passed": bool(
            report.get("historical_validation_gate_passed")
        ),
        "failed_gates": list(report.get("failed_gates") or []),
        "metrics": {
            "validation": metrics.get("validation") or {},
            "test": metrics.get("test") or {},
        },
        "evidence_path": _report_evidence_ref(path),
        "evidence_size_bytes": stat.st_size,
        "evidence_lazy": True,
    }


def _write_report_index(path: Path, report: dict[str, Any]) -> dict[str, Any]:
    eligible, _reason = _current_epoch_eligibility(report)
    payload = _report_listing_payload(
        report, path, identity_verified=eligible,
    )
    _atomic_json(_report_index_path(path), payload)
    return payload


def _read_report_header(path: Path) -> dict[str, Any]:
    """Read only the top-level fields before the intentionally heavy diagnostics."""
    chunks = bytearray()
    marker = b'\n  "diagnostics":'
    with path.open("rb") as stream:
        while len(chunks) < REPORT_HEADER_READ_LIMIT:
            chunk = stream.read(min(64 * 1024, REPORT_HEADER_READ_LIMIT - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            position = chunks.find(marker)
            if position >= 0:
                prefix = bytes(chunks[:position]).rstrip()
                if prefix.endswith(b","):
                    prefix = prefix[:-1]
                return json.loads(prefix + b"\n}")
    # Small legacy reports are cheap and still need exact listing semantics.
    if path.stat().st_size <= REPORT_HEADER_READ_LIMIT:
        value = json.loads(bytes(chunks))
        return value if isinstance(value, dict) else {}
    return {}


def _report_listing(path: Path) -> dict[str, Any]:
    """Return a bounded listing row; full evidence is an explicit run-id read."""
    try:
        stat = path.stat()
        index_path = _report_index_path(path)
        index = _read_json(index_path) if index_path.is_file() else {}
        if (
            index.get("index_schema") == REPORT_INDEX_SCHEMA
            and int(index.get("source_size") or -1) == stat.st_size
            and int(index.get("source_mtime_ns") or -1) == stat.st_mtime_ns
            and index.get("run_id") == path.stem
        ):
            return index
        header = _read_report_header(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not header:
        return {
            "run_id": path.stem,
            "evidence_path": _report_evidence_ref(path),
            "evidence_size_bytes": stat.st_size,
            "evidence_lazy": True,
            "identity_verified": False,
        }
    return _report_listing_payload(
        header, path, identity_verified=False,
    )


def _with_listing_epoch_status(report: dict[str, Any]) -> dict[str, Any]:
    """Conservative status derivation for an index/header, never a PASS oracle."""
    recorded_pass = bool(report.get("historical_validation_gate_passed"))
    if report.get("identity_verified") is True:
        if report.get("report_schema") == UNIFIED_DIAGNOSIS_REPORT_SCHEMA:
            expected = (
                str(factor_config.HISTORICAL_TEST_START),
                str(factor_config.HISTORICAL_TEST_END),
            )
            eligible = bool(
                report.get("research_epoch") == factor_config.RESEARCH_EPOCH
                and _split_pair((report.get("splits") or {}).get("test")) == expected
            )
        else:
            eligible, _unused = _current_epoch_eligibility(report)
    else:
        eligible = False
    return {
        **report,
        "recorded_historical_validation_gate_passed": recorded_pass,
        "historical_validation_gate_passed": bool(recorded_pass and eligible),
        "current_epoch_eligible": eligible,
        "legacy_reason": (
            None if eligible else
            "轻量索引未验证当前证据身份；完整报告按 run_id 惰性核验"
        ),
    }


def _report_source_updated_at(report: dict[str, Any]) -> str | None:
    source = report.get("source") or {}
    candidates = [
        source.get("metadata_built_at"),
        source.get("quality_generated_at"),
        source.get("pipeline_generated_at"),
    ]
    timestamps = [str(value) for value in candidates if value]
    return max(timestamps) if timestamps else report.get("generated_at")


def load_backtest_curve_state(run_id: Any) -> dict[str, Any]:
    """Read only the immutable report JSON and return its lightweight curve state."""
    safe = _safe_run_id(run_id)
    if safe is None:
        raise StrategyValidationError("回测运行ID无效")
    report = _read_json(REPORTS_DIR / f"{safe}.json")
    if not report:
        raise StrategyValidationError("未找到该回测报告")
    if (
        report.get("report_schema") == UNIFIED_DIAGNOSIS_REPORT_SCHEMA
        and _report_integrity(report).get("all_passed") is not True
    ):
        raise StrategyValidationError("统一诊断曲线报告完整性校验未通过")
    public = _with_current_epoch_status(report)
    actual_split = _split_pair((report.get("splits") or {}).get("test"))
    points = (
        ((report.get("diagnostics") or {}).get("equity_curve") or {}).get("test")
        or []
    )
    return {
        "run_id": safe,
        "generated_at": report.get("generated_at"),
        "source_updated_at": _report_source_updated_at(report),
        "split": list(actual_split) if actual_split else [],
        "canonical_split": list(SPLITS["test"]),
        "current_epoch_eligible": public["current_epoch_eligible"],
        "points": [dict(point) for point in points if isinstance(point, dict)],
        "refresh_after_seconds": CURVE_REFRESH_AFTER_SECONDS,
        "immutable_historical": True,
        "research_input_key": report.get("research_input_key"),
        "execution_audit_sha256": report.get("execution_audit_sha256"),
        "curve_sha256": (
            ((report.get("unified_diagnosis") or {}).get("diagnose") or {})
            .get("historical_curve", {}).get("curve_sha256")
        ),
    }


def publish_unified_diagnosis_report(
    report: dict[str, Any],
    spec: dict[str, Any],
    *,
    strategy_id: Any = None,
    diagnosis_evidence: str | None = None,
) -> dict[str, Any]:
    """Publish one current-input immutable chart report from unified diagnose."""
    lock = research_mutation_lock_state()
    lock_metadata = lock.get("metadata") or {}
    if lock.get("busy") is not True or lock_metadata.get("pid") != os.getpid():
        raise RuntimeError("发布统一诊断报告必须持有当前进程 research mutation lock")
    current_input_key = validate_unified_diagnosis_input(report)
    source = _source_state()
    if source.get("research_input_key") != current_input_key:
        raise StrategyValidationError("统一诊断与共享 Panel 审计快照不一致")
    _verify_source_snapshot(source)

    safe_strategy_id = _safe_id(strategy_id) if strategy_id else None
    if strategy_id and safe_strategy_id is None:
        raise StrategyValidationError("统一诊断报告 strategy_id 无效")
    if safe_strategy_id:
        saved = _read_json(STRATEGIES_DIR / f"{safe_strategy_id}.json")
        if not saved or result_spec_sha256(saved.get("spec") or {}) != result_spec_sha256(spec):
            raise StrategyValidationError("统一诊断报告规格与当前策略版本不一致")

    payload = _unified_diagnosis_payload(report)
    diagnose = payload.get("diagnose") or {}
    evaluate = payload.get("evaluate") or {}
    online = payload.get("historical_online_test") or {}
    curve = diagnose["historical_curve"]
    points = [dict(point) for point in curve["points"]]
    oos = diagnose.get("oos_metrics") or {}
    gate = evaluate.get("gate") or {}
    generated_at = _now()
    run_id = (
        "run-" + datetime.now(SHANGHAI).strftime("%Y%m%d%H%M%S")
        + "-" + secrets.token_hex(3)
    )
    total_return_pct = round((float(points[-1]["nav"]) - 1.0) * 100.0, 6)
    benchmark_total_return_pct = round(
        (float(points[-1]["benchmark_nav"]) - 1.0) * 100.0, 6,
    )
    published = {
        "version": 1,
        "report_schema": UNIFIED_DIAGNOSIS_REPORT_SCHEMA,
        "research_epoch": factor_config.RESEARCH_EPOCH,
        "generated_at": generated_at,
        "status": "unified_diagnosis_evidence_only",
        "run_id": run_id,
        "strategy_id": safe_strategy_id,
        "spec": json.loads(json.dumps(spec, ensure_ascii=False, allow_nan=False)),
        "research_input_key": current_input_key,
        "strategy_evaluator_fingerprint": strategy_evaluator_fingerprint(),
        "execution_audit_sha256": diagnose.get("execution_audit_sha256"),
        "source": source,
        "source_evidence_hashes": dict(source.get("evidence_hashes") or {}),
        "splits": {
            "test": [
                str(factor_config.HISTORICAL_TEST_START),
                str(factor_config.HISTORICAL_TEST_END),
            ],
        },
        "metrics": {
            "test": {
                "periods": int(curve.get("n_return_days") or 0),
                "total_return_pct": total_return_pct,
                "benchmark_total_return_pct": benchmark_total_return_pct,
                "annualized_return_pct": oos.get("annual_return_pct"),
                "annualized_excess_return_pct": (
                    (diagnose.get("multi_benchmark") or {})
                    .get("sh000001", {}).get("excess_annual_pct")
                ),
                "max_drawdown_pct": oos.get("max_drawdown_pct"),
                "sharpe_ratio": oos.get("sharpe"),
                "period_win_rate_pct": oos.get("win_rate_pct"),
            },
        },
        "diagnostics": {
            "equity_curve": {"test": points},
            "historical_curve": curve,
        },
        "unified_diagnosis": payload,
        "diagnosis_evidence": diagnosis_evidence,
        "evaluate_gate_passed": bool(gate.get("passed")),
        "historical_validation_gate_passed": False,
        "failed_gates": [
            "manual_diagnosis.exploratory_only"
        ] + ([] if gate.get("passed") else [
            str(gate.get("reason") or "unified_gate_failed")
        ]),
        "promotion": _promotion_of(safe_strategy_id),
        "recommendation_eligible": False,
        "capital_allocation": False,
        "orders": False,
        "automatic_trading": False,
        "safety": {
            "same_unified_backtest_curve": True,
            "second_backtest_executed": False,
            "historical_online_verdict": online.get("verdict"),
            "external_model_used": False,
            "arbitrary_python_used": False,
        },
    }
    published["report_body_sha256"] = _unified_report_body_sha256(published)
    published["integrity"] = _report_integrity(published)
    if published["integrity"].get("all_passed") is not True:
        raise RuntimeError("统一诊断不可变报告发布前完整性校验未通过")
    _verify_source_snapshot(source)
    path = REPORTS_DIR / f"{run_id}.json"
    _atomic_immutable_json(path, published)
    stored = _read_json(path)
    if stored.get("report_body_sha256") != published["report_body_sha256"]:
        raise RuntimeError("统一诊断不可变报告发布后复核失败")
    try:
        _write_report_index(path, published)
    except OSError:
        pass
    return {
        "run_id": run_id,
        "report_schema": UNIFIED_DIAGNOSIS_REPORT_SCHEMA,
        "generated_at": generated_at,
        "research_epoch": factor_config.RESEARCH_EPOCH,
        "research_input_key": current_input_key,
        "source_date_max": source.get("date_max"),
        "curve_points": len(points),
        "integrity": dict(published["integrity"]),
        "report_path": str(path.relative_to(ROOT)),
    }


def _compact_factor_catalog() -> dict[str, dict[str, Any]]:
    from factor_system import source_gate

    catalog = _full_factor_catalog()
    generated = sorted(set(catalog) - set(FACTOR_CATALOG))
    try:
        states = _registered_factor_run_states(generated)
        registry_error = None
    except StrategyValidationError as exc:
        states, registry_error = {}, str(exc)
    try:
        source_catalog = source_gate.load_catalog(factor_config.SOURCE_CATALOG)
        source_catalog_valid = source_gate.verify_catalog(
            source_catalog,
        ).get("valid") is True
    except Exception:
        source_catalog, source_catalog_valid = {}, False
    compact = {}
    for factor_id, raw_meta in catalog.items():
        meta = dict(raw_meta)
        builtin = factor_id in FACTOR_CATALOG
        row = states.get(factor_id)
        source_status = "builtin" if builtin else str(
            (row or {}).get("status") or meta.get("status") or "missing"
        )
        current_epoch = bool(
            builtin or (
                row is not None
                and row.get("research_epoch") == factor_config.RESEARCH_EPOCH
            )
        )
        status = source_status
        source = source_gate.verify_candidate(
            factor_id, source_catalog,
        ) if source_catalog_valid else {"runnable": False, "status": "invalid"}
        if (
            not current_epoch
            and source_status in {"historical_pass", "production", "frozen"}
        ):
            status = "invalidated"
        available = bool(
            source.get("runnable") is True and (
                builtin or (
                current_epoch
                and source_status in _RUNNABLE_GENERATED_FACTOR_STATUSES
                )
            )
        )
        if available:
            blocked_reason = None
        elif source.get("runnable") is not True:
            blocked_reason = (
                "来源、预注册、本地实现或 PIT 门禁未通过："
                f"{source.get('status') or 'not_in_catalog'}"
            )
        elif registry_error:
            blocked_reason = registry_error
        elif row is None:
            blocked_reason = "注册表缺少当前状态，禁止新运行"
        elif not current_epoch:
            blocked_reason = "旧 research epoch，仅可审计"
        else:
            blocked_reason = (
                f"状态 {source_status} 禁止新运行："
                f"{row.get('status_reason') or '无补充原因'}"
            )
        evidence = meta.get("evidence") if available else None
        if not available:
            continue
        compact[factor_id] = {
            "id": meta.get("id"),
            "kind": meta.get("kind"),
            "formula": meta.get("formula") if available else None,
            "group": meta.get("group"),
            "label": meta.get("label"),
            "description": meta.get("description") if available else blocked_reason,
            "direction_hint": meta.get("direction_hint") if available else None,
        }
        compact[factor_id].update({
            "status": status,
            "source_status": source_status,
            "research_epoch": (row or {}).get("research_epoch"),
            "current_research_epoch": factor_config.RESEARCH_EPOCH,
            "available_for_new_run": available,
            "blocked_reason": blocked_reason,
            "evidence": ({
                key: evidence.get(key) for key in (
                    "oos_ic_mean", "oos_ic_ir", "ic_retention",
                    "monotonic_aligned",
                )
            } if isinstance(evidence, dict) else None),
        })
    return compact


def _pid_alive(pid: Any) -> bool:
    try:
        value = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _status_time_is_fresh(value: Any, *, seconds: int = 120) -> bool:
    try:
        observed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return False
    now = datetime.now(observed.tzinfo) if observed.tzinfo else datetime.now()
    age = (now - observed).total_seconds()
    return 0 <= age <= seconds


def _factor_upstream_workflow() -> dict[str, Any]:
    """Small shared handoff: factor discovery either advances or names its wait."""
    from factor_system import status as factor_status
    from .factor_system_api import _factor_source_gate_summary

    daemon = factor_status.read_daemon() or {}
    mining = factor_status.read_zone("mining") or {}
    phase = str(daemon.get("phase") or "")
    zone = str(daemon.get("current_zone") or "")
    waiting = phase if phase in {
        "await_new_input", "needs_new_hypothesis",
    } else zone if zone in {
        "await_new_input", "needs_new_hypothesis",
    } else None
    pid = daemon.get("process_pid")
    if not pid:
        try:
            lock_text = (factor_config.STATE_DIR / "daemon.lock").read_text("utf-8")
            pid = next(
                (part[4:] for part in lock_text.split() if part.startswith("pid=")),
                None,
            )
        except OSError:
            pid = None
    process_alive = (
        daemon.get("process_alive") is True or _pid_alive(pid)
    )
    heartbeat_fresh = (
        daemon.get("heartbeat_fresh") is True
        or _status_time_is_fresh(daemon.get("heartbeat_at") or daemon.get("updated_at"))
    )
    supervisor_online = bool(
        daemon.get("daemon") == "running" and process_alive and heartbeat_fresh
    )
    source_gate = _factor_source_gate_summary(())
    source_blocked = bool(
        source_gate.get("valid") is not True
        or int(source_gate.get("runnable_count") or 0) == 0
    )
    terminal_blind_rejection = bool(
        source_gate.get("valid") is True
        and int(source_gate.get("blind_rejected_count") or 0) > 0
        and int(source_gate.get(
            "actionable_implementation_pending_count", 0,
        ) or 0) == 0
        and int(source_gate.get("actionable_data_pending_count", 0) or 0) == 0
    )
    active = bool(
        supervisor_online and not waiting and not source_blocked
        and phase in {"work", "pacing"}
    )
    last_metrics = mining.get("last_metrics") or {}
    search = last_metrics.get("search") or {}
    output = int(
        search.get("output_candidates", last_metrics.get("candidates", 0)) or 0
    )
    validated = int((last_metrics.get("counts") or {}).get("validated", 0) or 0)
    information_gain = {
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
    if terminal_blind_rejection:
        next_action = (
            "登记新的独立经济假设；当前盲测试验已永久拒绝，"
            "禁止重跑或调参复活"
        )
        transition = "new_independent_hypothesis_registered"
    elif waiting == "await_new_input" and supervisor_online:
        next_action = "等待 panel、原子或其他冻结研究输入身份发生变化"
        transition = "research_input_identity_changed"
    elif waiting == "needs_new_hypothesis" and supervisor_online:
        next_action = "登记新的独立经济假设；禁止只换随机种子重跑"
        transition = "new_independent_hypothesis_registered"
    elif source_blocked and supervisor_online:
        next_action = (
            "完成来源候选本地实现与 PIT 审计；通过前不进入策略回测"
            if source_gate.get("valid") is True
            else "修复并重新核验来源目录；通过前不进入策略回测"
        )
        transition = "source_candidate_passes_local_implementation_and_pit_audit"
    elif active:
        next_action = str(
            daemon.get("next_plan") or "等待当前因子发现轮次完成"
        )
        transition = "current_epoch_factor_passes_source_and_validation_gates"
    else:
        next_action = "恢复因子 Supervisor 的真实进程与新鲜心跳"
        transition = "factor_supervisor_online"
    blocked_by = []
    research_findings = list(source_gate.get("findings") or [])
    if not supervisor_online and not terminal_blind_rejection:
        blocked_by.append("factor_supervisor_unavailable")
    if terminal_blind_rejection:
        if not any(
            row.get("code") == "source_hypotheses_exhausted"
            for row in research_findings if isinstance(row, dict)
        ):
            research_findings.append({
                "code": "source_hypotheses_exhausted",
                "message": "当前来源候选已完成唯一盲测并永久封存。",
                "system_fault": False,
            })
    elif waiting:
        blocked_by.append(waiting)
    elif source_blocked:
        blocked_by.extend(
            str(row.get("code"))
            for row in source_gate.get("blockers") or []
            if row.get("code")
        )
    return {
        "active": active,
        "supervisor_online": supervisor_online,
        "stage": (
            "needs_new_hypothesis" if terminal_blind_rejection
            else waiting if waiting and supervisor_online
            else "source_audit" if source_blocked and supervisor_online
            else "factor_discovery" if active
            else "supervisor_recovery"
        ),
        "stage_status": (
            "waiting" if terminal_blind_rejection
            else "waiting" if waiting and supervisor_online
            else "running" if active else "blocked"
        ),
        "blocked_by": blocked_by,
        "research_findings": research_findings,
        "next_action": next_action,
        "transition_condition": transition,
        "recent_information_gain": information_gain,
    }


def _strategy_workflow_summary(
    *, source_error: str | None, counts: dict[str, Any], as_of: str | None,
) -> dict[str, Any]:
    batch_state = _read_json(BATCH_STATE_PATH)
    reported_running = bool(batch_state.get("running"))
    owner_alive = _pid_alive(batch_state.get("worker_pid"))
    batch_running = bool(
        reported_running and (_BATCH_ACTIVE.is_set() or owner_alive)
    )
    lock = research_mutation_lock_state()
    lock_busy = lock.get("busy") is True
    eligible_factor_count = len(_strategy_factor_pool(limit=None))
    upstream = _factor_upstream_workflow()
    batch_blockers = []
    if source_error:
        batch_blockers.append({
            "code": "strategy_source_unavailable", "message": source_error,
        })
    if eligible_factor_count == 0:
        batch_blockers.append({
            "code": "no_strict_batch_factors",
            "message": (
                "当前 strict epoch 没有通过来源、预注册、PIT、状态与 seal "
                "校验的策略因子；旧内置因子仅供归档展示，不可绕过来源门禁"
            ),
        })
    if batch_running:
        batch_blockers.append({
            "code": "strategy_batch_running", "message": "策略严格批次正在运行",
        })
    elif lock_busy:
        batch_blockers.append({
            "code": "research_lock_busy",
            "message": "另一研究写任务正持有跨进程锁",
        })
    elif RUN_LOCK.locked():
        batch_blockers.append({
            "code": "strategy_engine_busy", "message": "策略回测引擎正在运行",
        })

    if source_error and (
        upstream["active"] or upstream["supervisor_online"]
        or upstream["stage_status"] == "waiting"
    ):
        current_stage = upstream["stage"]
        stage_status = upstream["stage_status"]
        next_action = upstream["next_action"]
    elif source_error:
        current_stage, stage_status = "source_validation", "blocked"
        next_action = "修复本地因子面板或质量门禁后重新检查"
    elif batch_running:
        current_stage, stage_status = "strategy_batch", "running"
        next_action = "等待当前严格策略批次完成并写入不可变判定证据"
    elif lock_busy:
        current_stage, stage_status = "research_mutation", "running"
        next_action = "等待当前研究写任务释放跨进程锁后重新检查"
    elif eligible_factor_count == 0 and upstream["active"]:
        current_stage = upstream["stage"]
        stage_status = upstream["stage_status"]
        next_action = upstream["next_action"]
    elif eligible_factor_count == 0 and upstream["stage_status"] == "waiting":
        current_stage = upstream["stage"]
        stage_status = "waiting"
        next_action = upstream["next_action"]
    elif eligible_factor_count == 0:
        current_stage, stage_status = "strict_batch_factor_admission", "blocked"
        next_action = "先恢复因子发现链，再产出通过来源、PIT 与验证门禁的当前因子"
    elif counts["current"]["backtests"]:
        current_stage, stage_status = "historical_confirmation_review", "ready"
        next_action = "按 run_id 打开最新完整报告并审阅当前严格口径门禁"
    else:
        current_stage, stage_status = "strategy_design", "ready"
        next_action = "从当前可用因子池启动固定候选族的严格策略批次"

    last_errors = []
    if batch_state.get("status") in {"error", "interrupted"} or batch_state.get(
        "crashed_stale"
    ):
        last_errors.append({
            "code": "archived_strategy_batch_error",
            "message": str(
                batch_state.get("error")
                or batch_state.get("interrupted_reason")
                or "旧策略批次未正常完成"
            ),
            "at": batch_state.get("completed_at") or batch_state.get("started_at"),
            "current": False,
        })
    recent_experiments = _recent_strategy_experiments(1)
    recent_experiment = recent_experiments[0] if recent_experiments else {}
    recent_information_gain = {
        "factor_discovery": upstream["recent_information_gain"],
        "strategy_experiment": ({
            "source": "strategy_experiment",
            "experiment_id": recent_experiment.get("experiment_id"),
            "observed_at": recent_experiment.get("compared_at"),
            "decision_category": (
                recent_experiment.get("decision_category")
                or "legacy_descriptive_only"
            ),
            "expected_metric": recent_experiment.get("expected_metric"),
            "cost_seconds": recent_experiment.get("cost_seconds"),
        } if recent_experiment else None),
    }
    transition_condition = (
        upstream["transition_condition"]
        if (
            current_stage == upstream["stage"]
            and upstream["stage_status"] in {"blocked", "waiting"}
        )
        else "shared_panel_atomic_publish_completed"
        if source_error
        else upstream["transition_condition"]
        if eligible_factor_count == 0
        else "strategy_batch_writes_immutable_decision_evidence"
        if batch_running
        else "user_starts_strict_batch_with_current_eligible_factors"
    )
    blocked_by = list(dict.fromkeys([
        *(str(row.get("code")) for row in batch_blockers if row.get("code")),
        *(str(code) for code in upstream.get("blocked_by") or [] if code),
    ]))
    version_payload = {
        "epoch": factor_config.RESEARCH_EPOCH,
        "counts": counts,
        "as_of": as_of,
        "batch_status": batch_state.get("status"),
        "batch_done": batch_state.get("done"),
        "upstream_active": upstream["active"],
        "blocked_by": blocked_by,
        "recent_information_gain": recent_information_gain,
    }
    version = hashlib.sha256(json.dumps(
        version_payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()[:16]
    return {
        "current_stage": current_stage,
        "stage_status": stage_status,
        "blockers": batch_blockers,
        "blocked_by": blocked_by,
        "upstream_active": upstream["active"],
        "recent_information_gain": recent_information_gain,
        "transition_condition": transition_condition,
        "next_action": next_action,
        "counts": counts,
        "version": version,
        "as_of": as_of,
        "runtime": {
            "active": bool(
                batch_running or lock_busy or RUN_LOCK.locked()
                or upstream["active"]
            ),
            "source": (
                "strategy_batch" if batch_running
                else "research_mutation_lock" if lock_busy
                else "strategy_engine" if RUN_LOCK.locked()
                else "factor_discovery" if upstream["active"]
                else upstream["stage"] if upstream["stage_status"] == "waiting"
                else "idle"
            ),
            "upstream": upstream,
            "lock_busy": lock_busy,
            "lock": lock,
            "eligible_factor_count": eligible_factor_count,
            "strict_registry_eligible_factor_count": eligible_factor_count,
            "builtin_runnable_factor_count": sum(
                not _factor_run_blockers([factor_id])
                for factor_id in FACTOR_CATALOG
            ),
            "manual_strategy_runnable_factor_count": eligible_factor_count,
            "batch": {
                "status": batch_state.get("status"),
                "running": batch_running,
                "reported_running": reported_running,
                "stale_reported_running": bool(reported_running and not batch_running),
                "phase": batch_state.get("phase"),
                "total": int(batch_state.get("total") or 0),
                "done": int(batch_state.get("done") or 0),
                "start_allowed": not batch_blockers,
                "blockers": batch_blockers,
            },
        },
        "last_errors": last_errors,
    }


def workbench_state(view: str = "full") -> dict[str, Any]:
    if view not in {"full", "compact", "summary"}:
        raise StrategyValidationError("workbench view 仅支持 full、compact、summary")
    source_error = None
    try:
        source = _source_state()
    except StrategyValidationError as exc:
        source, source_error = {}, str(exc)
    strategies = []
    strategy_counts = {"current": 0, "archive": 0}
    as_of_values = [str(source[key]) for key in (
        "date_max", "metadata_built_at", "quality_generated_at",
        "pipeline_generated_at",
    ) if source.get(key)]
    if STRATEGIES_DIR.is_dir():
        for path in STRATEGIES_DIR.glob("user-*.json"):
            item = _read_json(path)
            if item:
                scope = (
                    "current" if item.get("research_epoch") == factor_config.RESEARCH_EPOCH
                    else "archive"
                )
                strategy_counts[scope] += 1
                if item.get("saved_at"):
                    as_of_values.append(str(item["saved_at"]))
                if view == "summary":
                    continue
                item["promotion"] = _audit_safe_promotion(item.get("promotion"))
                item["recommendation_eligible"] = _promotion_eligible(
                    item["promotion"],
                )
                item["invalidated"] = (
                    item["promotion"].get("unified_verdict") == "INVALIDATED"
                )
                item["revision_count"] = len(list(
                    (STRATEGIES_DIR / "history").glob(
                        f"{item.get('strategy_id')}_v*.json"
                    )
                ))
                strategies.append(item)
        strategies.sort(key=lambda item: str(item.get("saved_at") or ""), reverse=True)
        strategies = strategies[:20]
    recent = []
    latest_backtest: dict[str, Any] | None = None
    latest_run_id = None
    report_counts = {
        "current": 0, "archive": 0,
        "current_historical_pass": 0, "archive_recorded_historical_pass": 0,
    }
    if REPORTS_DIR.is_dir():
        for path in sorted(REPORTS_DIR.glob("run-*.json"), reverse=True):
            stored_report = _report_listing(path)
            if stored_report:
                report = _with_listing_epoch_status(stored_report)
                if latest_run_id is None:
                    latest_run_id = report.get("run_id") or path.stem
                if report.get("generated_at"):
                    as_of_values.append(str(report["generated_at"]))
                if report["current_epoch_eligible"]:
                    report_counts["current"] += 1
                    report_counts["current_historical_pass"] += int(bool(
                        report.get("historical_validation_gate_passed")
                    ))
                else:
                    report_counts["archive"] += 1
                    report_counts["archive_recorded_historical_pass"] += int(bool(
                        report.get("recorded_historical_validation_gate_passed")
                    ))
                if view == "summary" or len(recent) >= 10:
                    continue
                integrity = None
                if latest_backtest is None and view == "full":
                    latest_backtest = dict(report)
                promotion = _promotion_of(report.get("strategy_id"))
                recent.append({
                    "run_id": report.get("run_id"),
                    "strategy_id": report.get("strategy_id"),
                    "name": (report.get("spec") or {}).get("name"),
                    "generated_at": report.get("generated_at"),
                    "status": report.get("status"),
                    "historical_validation_gate_passed": bool(
                        report.get("historical_validation_gate_passed")
                    ),
                    "recorded_historical_validation_gate_passed": bool(
                        report.get("recorded_historical_validation_gate_passed")
                    ),
                    "current_epoch_eligible": bool(
                        report.get("current_epoch_eligible")
                    ),
                    "legacy_reason": report.get("legacy_reason"),
                    "evidence_lazy": True,
                    "evidence_path": report.get("evidence_path"),
                    "evidence_size_bytes": report.get("evidence_size_bytes"),
                    "failed_gates": report.get("failed_gates") or [],
                    "metrics": (
                        report.get("metrics") or {} if view == "full" else {
                            "validation": (
                                (report.get("metrics") or {}).get("validation") or {}
                            ),
                            "test": ((report.get("metrics") or {}).get("test") or {})
                        }
                    ),
                    **({
                        "spec": report.get("spec") or {},
                        "integrity": integrity,
                    } if view == "full" else {}),
                    "promotion": promotion,
                    "recommendation_eligible": _promotion_eligible(promotion),
                })
    counts = {
        "current": {
            "strategies": strategy_counts["current"],
            "backtests": report_counts["current"],
            "historical_pass": report_counts["current_historical_pass"],
        },
        "archive": {
            "strategies": strategy_counts["archive"],
            "backtests": report_counts["archive"],
            "recorded_historical_pass": report_counts[
                "archive_recorded_historical_pass"
            ],
        },
    }
    as_of = max(as_of_values) if as_of_values else None
    safety = {
        "external_model_used": False,
        "network_used": False,
        "paid_api_used": False,
        "assistant_mode": "local_statistical_rule_engine",
        "orders": False,
        "automatic_trading": False,
    }
    cost_semantics = {
        "one_way_cost_bps_role": ONE_WAY_COST_BPS_ROLE,
        "additional_trading_costs": ADDITIONAL_TRADING_COSTS,
        "label": "单边滑点；佣金、印花税、过户费另加",
    }
    result = {
        "available": not source_error,
        "view": view,
        "source_error": source_error,
        "safe_dsl": True,
        "arbitrary_python_allowed": False,
        "factor_panel_coverage": _FACTOR_PANEL_COVERAGE,
        "defaults": DEFAULT_SPEC,
        "draft_factor_catalog": {
            factor_id: {
                "label": meta.get("label") or factor_id,
                "description": meta.get("description") or "草稿定义，运行资格另核",
                "group": "草稿构建（运行资格另核）", "status": "draft_only",
                "available_for_new_runs": False,
            } for factor_id, meta in _full_factor_catalog().items()
        } if view != "summary" else {},
        "fixed_splits": SPLITS,
        "cost_ladder_bps": list(COST_LADDER_BPS),
        "source": source,
        "latest_run_id": latest_run_id,
        "workflow": _strategy_workflow_summary(
            source_error=source_error, counts=counts, as_of=as_of,
        ),
        "safety": safety,
        "cost_semantics": cost_semantics,
    }
    if view == "summary":
        return result
    experiments = _recent_strategy_experiments()
    if view == "compact":
        compact_catalog = _compact_factor_catalog()
        experiments = [{
            key: item.get(key) for key in (
                "experiment_id", "change", "compared_at",
                "evidence_path", "evidence_sha256",
                "hypothesis_id", "source_gate", "expected_metric", "direction",
                "success_threshold", "decision_category", "next_action",
                "cost_seconds", "cost_scope", "confirmation_status",
                "confirmation_eligible", "registration",
            )
        } for item in experiments]
        result.update({
            "factor_catalog": compact_catalog,
            "factor_catalog_scope": "available_for_new_run_only",
            "factor_catalog_available_count": len(compact_catalog),
            "templates": STRATEGY_TEMPLATES,
            "saved_strategies": strategies,
            "recent_backtests": recent,
            "recent_experiments": experiments,
            "latest_backtest_summary": recent[0] if recent else None,
        })
        return result
    result.update({
        "factor_catalog": _full_factor_catalog(),
        "templates": STRATEGY_TEMPLATES,
        "saved_strategies": strategies,
        "recent_backtests": recent,
        "recent_experiments": experiments,
        "latest_backtest": latest_backtest,
    })
    return result


def _signal_dates(panel: Any, holding_days: int) -> list[pd.Timestamp]:
    eligible = panel.signal_eligible.fillna(False).astype(bool)
    dates = eligible.index[eligible.sum(axis=1).ge(500)].tolist()
    horizon = max(1, int(holding_days))
    return [pd.Timestamp(day) for day in dates[:-horizon:horizon]]


def _load_panel(spec: dict[str, Any], panel: Any | None = None) -> pd.DataFrame:
    """Build the quick-diagnostic frame from the exact audited shared snapshot.

    This deliberately does not publish or mutate the retired V4 SQLite.  Factor
    mining and strategy research now share one in-memory Panel identity.
    """
    from factor_system.compose import REBUILDERS
    from factor_system.data import research_close_forward_return

    if panel is None:
        panel, _source = _strategy_panel_snapshot()
    active = [name for name, weight in spec["factors"].items() if weight]
    # Liquidity is loaded even when it is not a scoring factor so every run can
    # publish the same capacity diagnostics.
    columns = sorted(set(active + ["trend60", "liquidity_20"]))
    unknown = sorted(set(columns).difference(REBUILDERS))
    if unknown:
        raise StrategyValidationError(
            "共享面板因子重建器缺失: " + ", ".join(unknown)
        )
    signal_dates = _signal_dates(panel, spec["holding_days"])
    if not signal_dates:
        raise StrategyValidationError("没有可用信号日期")

    eligible = panel.signal_eligible.loc[signal_dates].fillna(False).astype(bool)
    series: list[pd.Series] = []
    with np.errstate(divide="ignore", invalid="ignore"):
        for name in columns:
            values = REBUILDERS[name](panel).loc[signal_dates]
            series.append(values.stack(future_stack=True).rename(name))

    horizon = int(spec["holding_days"])
    entry = panel.adj_open.shift(-1)
    entry_filled = (
        panel.tradable.shift(-1).fillna(False).astype(bool)
        & entry.notna()
        & entry.gt(0)
    )
    scheduled_exit_tradable = (
        panel.tradable.shift(-horizon).fillna(False).astype(bool)
    )
    # 已入场后若计划退出日停牌，明确顺延到其后第一个可交易收盘；
    # 绝不能把该仓位倒推成“未成交现金”。数据尾部仍无退出价时保留 NaN，
    # 若它进入目标组合，_run_periods 会 fail-closed。
    tradable_close = panel.close.where(panel.tradable.fillna(False).astype(bool))
    exit_price = tradable_close.bfill().shift(-horizon)
    date_positions = pd.DataFrame(
        np.broadcast_to(
            np.arange(len(panel.close.index), dtype=np.int32)[:, None],
            panel.close.shape,
        ),
        index=panel.close.index,
        columns=panel.close.columns,
    )
    exit_position = date_positions.where(
        panel.tradable.fillna(False).astype(bool)
    ).bfill().shift(-horizon)
    forward = exit_price.div(entry).sub(1.0).where(entry_filled)
    delayed_exit = (
        entry_filled
        & ~scheduled_exit_tradable
        & exit_price.notna()
    )
    # 与正式因子面板共用显式退市定义。close-to-close 结果这里只用作
    # “计划窗口内发生零回收”的判别；收益分母仍是可执行的 T+1 复权开盘。
    delisting_reference = research_close_forward_return(
        panel.close,
        horizon,
        is_delisted_panel=getattr(panel, "is_delisted_panel", None),
        last_date=getattr(panel, "last_date", None),
    )
    zero_recovery = entry_filled & delisting_reference.eq(-1.0)
    forward = forward.mask(zero_recovery, -1.0)
    delayed_exit = delayed_exit & ~zero_recovery
    series.append(
        forward.loc[signal_dates].stack(future_stack=True).rename("gross_return")
    )
    series.append(
        entry_filled.loc[signal_dates].stack(future_stack=True).rename(
            "entry_filled"
        )
    )
    series.append(
        delayed_exit.loc[signal_dates].stack(future_stack=True).rename(
            "label_exit_delayed"
        )
    )
    series.append(
        exit_position.loc[signal_dates].stack(future_stack=True).rename(
            "_label_exit_position"
        )
    )
    series.append(
        zero_recovery.loc[signal_dates].stack(future_stack=True).rename(
            "_label_zero_recovery"
        )
    )
    result = pd.concat(series, axis=1).reset_index(
        names=["trade_date", "code"],
    )
    eligible_rows = eligible.stack(future_stack=True).to_numpy(dtype=bool)
    result = result.loc[eligible_rows].reset_index(drop=True)
    result["trade_date"] = pd.to_datetime(
        result["trade_date"], errors="raise",
    ).dt.date.astype(str)
    date_index = pd.DatetimeIndex(panel.close.index)
    signal_positions = date_index.get_indexer(pd.DatetimeIndex(signal_dates))
    if (signal_positions < 0).any() or (
        signal_positions + horizon >= len(date_index)
    ).any():
        raise StrategyValidationError("共享面板缺少可证明的计划退出交易日")
    scheduled_exit_by_signal = {
        pd.Timestamp(signal_day).date().isoformat():
        date_index[position + horizon].date().isoformat()
        for signal_day, position in zip(signal_dates, signal_positions)
    }
    result["label_scheduled_exit_date"] = result["trade_date"].map(
        scheduled_exit_by_signal,
    )
    if result["label_scheduled_exit_date"].isna().any():
        raise StrategyValidationError("共享面板计划退出交易日映射不完整")
    positions = pd.to_numeric(
        result.pop("_label_exit_position"), errors="coerce"
    )
    resolved = (
        result["entry_filled"].fillna(False).astype(bool)
        & positions.notna()
    )
    exit_dates = pd.Series("", index=result.index, dtype="str")
    exit_dates.loc[resolved] = [
        date_index[int(position)].date().isoformat()
        for position in positions.loc[resolved]
    ]
    zero_rows = result.pop("_label_zero_recovery").fillna(False).astype(bool)
    if bool(zero_rows.any()):
        last_dates = pd.to_datetime(
            pd.Series(
                getattr(panel, "last_date", None), dtype=object,
            ).reindex(panel.close.columns),
            errors="coerce",
        )
        mapped = pd.to_datetime(
            result.loc[zero_rows, "code"].map(last_dates), errors="coerce",
        )
        if mapped.isna().any():
            raise StrategyValidationError("显式退市标签缺少可证明的退市日期")
        exit_dates.loc[zero_rows] = mapped.dt.date.astype(str).to_numpy()
    result["label_exit_date"] = exit_dates
    result = result.replace([np.inf, -np.inf], np.nan)
    if result.empty or result["gross_return"].notna().sum() == 0:
        raise StrategyValidationError("共享面板没有可执行的前向标签")
    return result


def _benchmark_returns(signal_dates: list[str], holding_days: int) -> dict[str, float]:
    frame = pd.read_csv(INDEX_PATH, usecols=["date", "open", "close"], dtype={"date": str})
    frame["open"] = pd.to_numeric(frame["open"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna().drop_duplicates("date", keep="last").sort_values("date")
    indexed = frame.set_index("date")
    dates = frame["date"].tolist()
    positions = {day: index for index, day in enumerate(dates)}
    result: dict[str, float] = {}
    for day in signal_dates:
        index = positions.get(day)
        if index is None or index + holding_days >= len(dates):
            continue
        entry = float(indexed.loc[dates[index + 1], "open"])
        exit_price = float(indexed.loc[dates[index + holding_days], "close"])
        if entry > 0 and exit_price > 0:
            result[day] = exit_price / entry - 1.0
    return result


def _split_safe_signal_dates(
    frame: pd.DataFrame,
    start: str,
    end: str,
    *,
    candidate_dates: list[str] | None = None,
    schedule_frame: pd.DataFrame | None = None,
) -> tuple[set[str], dict[str, Any]]:
    """Purge planned or delayed exits that settle outside the split."""
    signal = frame["trade_date"].astype(str)
    candidate = frame.loc[signal.between(str(start), str(end))].copy()
    all_dates = sorted(set(
        str(day) for day in (
            candidate_dates
            if candidate_dates is not None
            else candidate["trade_date"].astype(str).unique()
        )
        if str(start) <= str(day) <= str(end)
    ))
    schedule = schedule_frame if schedule_frame is not None else frame
    required = {"trade_date", "label_scheduled_exit_date"}
    if not required.issubset(schedule.columns):
        raise StrategyValidationError("分段净化缺少计划退出交易日")
    schedule = schedule.loc[
        schedule["trade_date"].astype(str).isin(all_dates),
        ["trade_date", "label_scheduled_exit_date"],
    ].copy()
    schedule["trade_date"] = schedule["trade_date"].astype(str)
    inconsistent = schedule.groupby("trade_date")[
        "label_scheduled_exit_date"
    ].nunique(dropna=False)
    if bool(inconsistent.gt(1).any()):
        raise StrategyValidationError("同一信号日存在冲突的计划退出交易日")
    planned = schedule.drop_duplicates("trade_date").set_index("trade_date")[
        "label_scheduled_exit_date"
    ]
    planned_exits = pd.to_datetime(planned, errors="coerce")
    planned_unsafe = {
        day for day in all_dates
        if day not in planned_exits.index
        or pd.isna(planned_exits.loc[day])
        or planned_exits.loc[day] > pd.Timestamp(end)
    }

    delayed_unsafe: set[str] = set()
    if not candidate.empty:
        entered = candidate["entry_filled"].fillna(False).astype(bool)
        exits = pd.to_datetime(candidate["label_exit_date"], errors="coerce")
        row_safe = (
            ~entered
            | (
                candidate["gross_return"].notna()
                & exits.notna()
                & exits.le(pd.Timestamp(end))
            )
        )
        safe_by_date = row_safe.groupby(candidate["trade_date"].astype(str)).all()
        delayed_unsafe = set(safe_by_date.index[~safe_by_date].astype(str))
    unsafe = planned_unsafe | delayed_unsafe
    safe = set(all_dates).difference(unsafe)
    purged = [day for day in all_dates if day in unsafe]
    return safe, {
        "candidate_signal_dates": len(all_dates),
        "retained_signal_dates": len(safe),
        "purged_signal_dates": len(purged),
        "purged_dates": purged,
        "planned_exit_boundary_purged_dates": sorted(planned_unsafe),
        "selected_delayed_exit_boundary_purged_dates": sorted(delayed_unsafe),
        "rule": (
            "every_candidate_planned_exit_and_every_selected_filled_actual_exit_"
            "must_resolve_on_or_before_split_end"
        ),
    }


def _split_ic_overrides(
    frame: pd.DataFrame,
    safe_dates: set[str],
    split_end: str,
    active: list[str],
) -> tuple[dict[str, dict[str, float | None]], int]:
    """Recompute IC using only labels whose actual exit is inside the split."""
    subset = frame[frame["trade_date"].astype(str).isin(safe_dates)].copy()
    exits = pd.to_datetime(subset["label_exit_date"], errors="coerce")
    entered = subset["entry_filled"].fillna(False).astype(bool)
    label_safe = (
        entered
        & subset["gross_return"].notna()
        & exits.notna()
        & exits.le(pd.Timestamp(split_end))
    )
    excluded = int((entered & ~label_safe).sum())
    overrides: dict[str, dict[str, float | None]] = {}
    for signal_date, group in subset.groupby("trade_date", sort=True):
        usable = group.loc[label_safe.loc[group.index]].copy()
        values: dict[str, float | None] = {}
        if len(usable) >= 20:
            future_rank = usable["gross_return"].rank(
                method="average", pct=True,
            )
            for factor in active:
                factor_rank = group[factor].rank(method="average", pct=True)
                correlation = factor_rank.loc[usable.index].corr(future_rank)
                values[factor] = (
                    round(float(correlation), 8)
                    if pd.notna(correlation) else None
                )
        else:
            values = {factor: None for factor in active}
        overrides[str(signal_date)] = values
    return overrides, excluded


def _net_after_cost(returns: pd.Series, filled: pd.Series, bps: int | float) -> pd.Series:
    cost = float(bps) / 10_000.0
    result = pd.Series(0.0, index=returns.index)
    result.loc[filled] = (
        (1.0 + returns.loc[filled].astype(float)) * (1.0 - cost)
        / (1.0 + cost) - 1.0
    )
    return result


def _run_periods(spec: dict[str, Any], panel: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    panel = panel.copy()
    active = [name for name, weight in spec["factors"].items() if weight]
    if "liquidity_20" not in panel:
        panel["liquidity_20"] = np.nan
    for factor in active:
        panel[f"rank_{factor}"] = panel.groupby("trade_date")[factor].rank(
            method="average", pct=True,
        )
    panel["score"] = sum(
        float(spec["factors"][factor]) * panel[f"rank_{factor}"]
        for factor in active
    )
    signal_dates = sorted(panel["trade_date"].unique().tolist())
    benchmark = _benchmark_returns(signal_dates, spec["holding_days"])
    periods: list[dict[str, Any]] = []
    selected_rows: list[pd.DataFrame] = []
    previous_codes: set[str] = set()
    for signal_date, group in panel.groupby("trade_date", sort=True):
        eligible = group.dropna(subset=active + ["score"]).copy()
        breadth = float(eligible["trend60"].gt(0).mean()) if len(eligible) else 0.0
        risk_on = spec["risk_rule"] == "none" or breadth >= 0.5
        selected = eligible.sort_values(
            ["score", "code"], ascending=[False, True],
        ).head(spec["top_n"]).copy()
        current_codes = set(selected["code"].astype(str)) if risk_on else set()
        if current_codes or previous_codes:
            retained = len(current_codes & previous_codes)
            turnover = 1.0 - retained / max(spec["top_n"], 1)
        else:
            turnover = 0.0
        previous_codes = current_codes

        ic_values: dict[str, float | None] = {}
        ic_sample = eligible.dropna(subset=["gross_return"]).copy()
        if len(ic_sample) >= 20:
            future_rank = ic_sample["gross_return"].rank(method="average", pct=True)
            for factor in active:
                correlation = ic_sample[f"rank_{factor}"].corr(future_rank)
                ic_values[factor] = (
                    round(float(correlation), 8) if pd.notna(correlation) else None
                )
        else:
            ic_values = {factor: None for factor in active}

        ladder_returns = {bps: 0.0 for bps in COST_LADDER_BPS}
        capacity_cny = None
        if risk_on and len(selected):
            entry_filled = selected.get(
                "entry_filled", selected["gross_return"].notna(),
            ).fillna(False).astype(bool)
            unresolved = entry_filled & selected["gross_return"].isna()
            if bool(unresolved.any()):
                examples = ", ".join(
                    selected.loc[unresolved, "code"].astype(str).head(5)
                )
                raise StrategyValidationError(
                    f"{signal_date} 已入场目标缺少可证明的退出标签: {examples}"
                )
            filled = entry_filled & selected["gross_return"].notna()
            gross_slots = selected["gross_return"].where(filled, 0.0)
            net_slots = _net_after_cost(
                selected["gross_return"], filled, spec["one_way_cost_bps"],
            )
            stress_slots = _net_after_cost(
                selected["gross_return"], filled,
                min(float(spec["one_way_cost_bps"]) * 2, 100),
            )
            for bps in COST_LADDER_BPS:
                ladder_returns[bps] = float(
                    _net_after_cost(selected["gross_return"], filled, bps).sum()
                    / spec["top_n"]
                )
            selected["filled_execution"] = filled
            selected["net_return"] = net_slots
            selected["stress_net_return"] = stress_slots
            selected["risk_on"] = True
            selected["market_breadth"] = breadth
            selected["estimated_daily_amount_cny"] = np.exp(
                selected["liquidity_20"].clip(upper=30)
            )
            selected_rows.append(selected)
            gross_return = float(gross_slots.sum() / spec["top_n"])
            net_return = float(net_slots.sum() / spec["top_n"])
            stress_return = float(stress_slots.sum() / spec["top_n"])
            filled_count = int(filled.sum())
            delayed_exit_count = int(
                selected.loc[filled, "label_exit_delayed"].fillna(False).sum()
            ) if "label_exit_delayed" in selected else 0
            selected_count = len(selected)
            valid_amount = selected["estimated_daily_amount_cny"].dropna()
            if len(valid_amount):
                # Equal-weight portfolio, each name capped at 1% of its recent
                # average daily traded amount.
                capacity_cny = float(valid_amount.min() * 0.01 * spec["top_n"])
        else:
            gross_return = net_return = stress_return = 0.0
            filled_count = selected_count = 0
            delayed_exit_count = 0
        period = {
            "signal_date": signal_date,
            "year": str(signal_date)[:4],
            "eligible_stocks": int(len(eligible)),
            "selected_stocks": int(selected_count),
            "filled_executions": filled_count,
            "delayed_exits": delayed_exit_count,
            "risk_on": bool(risk_on),
            "market_breadth": breadth,
            "one_way_turnover_pct": turnover * 100,
            "estimated_capacity_cny": capacity_cny,
            "maximum_position_weight_pct": 100.0 / spec["top_n"],
            "gross_return": gross_return,
            "net_return": net_return,
            "stress_net_return": stress_return,
            "benchmark_return": benchmark.get(str(signal_date)),
        }
        period.update({f"net_return_cost_{bps}bp": value for bps, value in ladder_returns.items()})
        period.update({f"ic_{factor}": value for factor, value in ic_values.items()})
        periods.append(period)
    selected_frame = (
        pd.concat(selected_rows, ignore_index=True) if selected_rows
        else pd.DataFrame()
    )
    return pd.DataFrame(periods), selected_frame


def _compound(values: pd.Series) -> float:
    return float((1.0 + values.astype(float)).prod() - 1.0)


def _max_drawdown(values: pd.Series) -> float:
    nav = np.r_[1.0, np.cumprod(1.0 + values.to_numpy(dtype=float))]
    peaks = np.maximum.accumulate(nav)
    return float(np.min(nav / peaks - 1.0))


def _annualize(total_return: float, periods: int, periods_per_year: float) -> float:
    if periods <= 0 or total_return <= -1.0:
        return -1.0 if total_return <= -1.0 else 0.0
    return float((1.0 + total_return) ** (periods_per_year / periods) - 1.0)


def _bootstrap_lower(values: np.ndarray, block: int, seed: int) -> float:
    if len(values) < 2:
        return 0.0
    rng = np.random.default_rng(seed)
    samples = np.empty(2_000)
    for index in range(len(samples)):
        chosen: list[float] = []
        while len(chosen) < len(values):
            start = int(rng.integers(0, len(values)))
            chosen.extend(values[(start + offset) % len(values)] for offset in range(block))
        samples[index] = float(np.mean(chosen[:len(values)]))
    return float(np.quantile(samples, 0.025))


def _summarize(frame: pd.DataFrame, holding_days: int, seed: int) -> dict[str, Any]:
    if frame.empty:
        return {"periods": 0}
    returns = frame["net_return"].fillna(0.0).astype(float)
    benchmark = frame["benchmark_return"].fillna(0.0).astype(float)
    stress = frame["stress_net_return"].fillna(0.0).astype(float)
    periods_per_year = 252.0 / holding_days
    total = _compound(returns)
    benchmark_total = _compound(benchmark)
    annualized = _annualize(total, len(frame), periods_per_year)
    benchmark_annualized = _annualize(benchmark_total, len(frame), periods_per_year)
    volatility = float(returns.std(ddof=1) * math.sqrt(periods_per_year)) if len(frame) > 1 else 0.0
    downside = returns[returns < 0]
    downside_deviation = (
        float(math.sqrt(float((downside ** 2).mean())) * math.sqrt(periods_per_year))
        if len(downside) else 0.0
    )
    drawdown = _max_drawdown(returns)
    var_95 = float(returns.quantile(0.05))
    tail = returns[returns <= var_95]
    cvar_95 = float(tail.mean()) if len(tail) else var_95
    yearly = {
        str(year): round(_compound(group["net_return"]) * 100, 6)
        for year, group in frame.groupby("year", sort=True)
    }
    return {
        "periods": int(len(frame)),
        "invested_periods": int(frame["risk_on"].sum()),
        "risk_on_rate_pct": round(float(frame["risk_on"].mean()) * 100, 6),
        "median_eligible_stocks": round(float(frame["eligible_stocks"].median()), 2),
        "median_selected_stocks": round(float(frame["selected_stocks"].median()), 2),
        "filled_execution_rate_pct": round(
            100 * float(frame["filled_executions"].sum())
            / max(float(frame["selected_stocks"].sum()), 1.0), 6,
        ),
        "mean_period_net_return_pct": round(float(returns.mean()) * 100, 6),
        "total_return_pct": round(total * 100, 6),
        "annualized_return_pct": round(annualized * 100, 6),
        "benchmark_total_return_pct": round(benchmark_total * 100, 6),
        "benchmark_annualized_return_pct": round(benchmark_annualized * 100, 6),
        "annualized_excess_return_pct": round(
            (annualized - benchmark_annualized) * 100, 6,
        ),
        "annualized_volatility_pct": round(volatility * 100, 6),
        "sharpe_ratio": round(annualized / volatility, 6) if volatility else None,
        "sortino_ratio": round(annualized / downside_deviation, 6) if downside_deviation else None,
        "calmar_ratio": round(annualized / abs(drawdown), 6) if drawdown < 0 else None,
        "period_var_95_pct": round(var_95 * 100, 6),
        "period_cvar_95_pct": round(cvar_95 * 100, 6),
        "cost_stress_total_return_pct": round(_compound(stress) * 100, 6),
        "max_drawdown_pct": round(drawdown * 100, 6),
        "period_win_rate_pct": round(float(returns.gt(0).mean()) * 100, 6),
        "positive_or_cash_rate_pct": round(float(returns.ge(0).mean()) * 100, 6),
        "best_period_return_pct": round(float(returns.max()) * 100, 6),
        "worst_period_return_pct": round(float(returns.min()) * 100, 6),
        "average_one_way_turnover_pct": round(
            float(frame["one_way_turnover_pct"].fillna(0).mean()), 6,
        ),
        "median_estimated_capacity_cny": (
            round(float(frame["estimated_capacity_cny"].dropna().median()), 2)
            if frame["estimated_capacity_cny"].notna().any() else None
        ),
        "minimum_estimated_capacity_cny": (
            round(float(frame["estimated_capacity_cny"].dropna().min()), 2)
            if frame["estimated_capacity_cny"].notna().any() else None
        ),
        "maximum_position_weight_pct": round(
            float(frame["maximum_position_weight_pct"].max()), 6,
        ),
        "block_bootstrap_95pct_lower_mean_pct": round(
            _bootstrap_lower(
                returns.to_numpy(), max(1, 20 // holding_days), seed,
            ) * 100,
            6,
        ),
        "yearly_total_return_pct": yearly,
    }


def _equity_curve(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    nav = (1.0 + frame["net_return"].fillna(0.0).astype(float)).cumprod()
    benchmark = (
        1.0 + frame["benchmark_return"].fillna(0.0).astype(float)
    ).cumprod()
    # Initial capital at NAV=1 is a real peak candidate.  Without this floor a
    # first-period loss was incorrectly displayed as 0% drawdown.
    peaks = nav.cummax().clip(lower=1.0)
    return [
        {
            "date": str(day),
            "nav": round(float(strategy_nav), 8),
            "benchmark_nav": round(float(benchmark_nav), 8),
            "drawdown_pct": round(float(strategy_nav / peak - 1.0) * 100, 6),
        }
        for day, strategy_nav, benchmark_nav, peak in zip(
            frame["signal_date"], nav, benchmark, peaks,
        )
    ]


def _cost_ladder(frame: pd.DataFrame, holding_days: int) -> list[dict[str, Any]]:
    rows = []
    periods_per_year = 252.0 / holding_days
    for bps in COST_LADDER_BPS:
        values = frame.get(f"net_return_cost_{bps}bp", pd.Series(dtype=float)).fillna(0.0)
        total = _compound(values) if len(values) else 0.0
        rows.append({
            "one_way_cost_bps": bps,
            "total_return_pct": round(total * 100, 6),
            "annualized_return_pct": round(
                _annualize(total, len(values), periods_per_year) * 100, 6,
            ),
            "max_drawdown_pct": round(_max_drawdown(values) * 100, 6) if len(values) else 0.0,
        })
    return rows


def _factor_diagnostics(frame: pd.DataFrame, active: list[str]) -> list[dict[str, Any]]:
    rows = []
    for factor in active:
        values = pd.to_numeric(frame.get(f"ic_{factor}"), errors="coerce").dropna()
        mean = float(values.mean()) if len(values) else 0.0
        standard_deviation = float(values.std(ddof=1)) if len(values) > 1 else 0.0
        rows.append({
            "factor": factor,
            "label": (_full_factor_catalog().get(factor) or {}).get("label") or factor,
            "observations": int(len(values)),
            "mean_rank_ic": round(mean, 8),
            "rank_ic_positive_rate_pct": round(float(values.gt(0).mean()) * 100, 6) if len(values) else 0.0,
            "rank_ic_ir": round(mean / standard_deviation, 6) if standard_deviation else None,
        })
    return rows


def _split_gates(metrics: dict[str, Any]) -> dict[str, bool]:
    yearly = metrics.get("yearly_total_return_pct") or {}
    return {
        "minimum_45_nonoverlapping_periods": metrics.get("periods", 0) >= 45,
        "median_at_least_500_eligible_stocks": metrics.get("median_eligible_stocks", 0) >= 500,
        "total_return_positive": metrics.get("total_return_pct", 0) > 0,
        "annualized_return_at_least_10pct": metrics.get("annualized_return_pct", 0) >= 10,
        "annualized_excess_positive": metrics.get("annualized_excess_return_pct", 0) > 0,
        "double_cost_total_positive": metrics.get("cost_stress_total_return_pct", 0) > 0,
        "max_drawdown_not_worse_than_20pct": metrics.get("max_drawdown_pct", -100) >= -20,
        "period_win_rate_at_least_55pct": metrics.get("period_win_rate_pct", 0) >= 55,
        "bootstrap_95pct_lower_mean_positive": metrics.get(
            "block_bootstrap_95pct_lower_mean_pct", 0,
        ) > 0,
        "every_calendar_year_positive": bool(yearly) and all(value > 0 for value in yearly.values()),
    }


def _test_folds(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """测试期 5 折等分（P3-1 对齐 quantqc 部署闸门：全折通过率≥60%，5 折≥3 折）。

    每折：净收益为正 且 跑赢上证基准（benchmark_return 即 sh000001 上证指数，
    次日开盘进/收盘出；2026-08-14 用户强制基准口径）。返回 list 保持前端
    `folds.map(row=>…)` 兼容，另附 passed 标记供闸门使用。"""
    folds = []
    for index, positions in enumerate(np.array_split(np.arange(len(frame)), 5), start=1):
        portion = frame.iloc[positions]
        strategy = _compound(portion["net_return"]) if len(portion) else 0.0
        benchmark = _compound(portion["benchmark_return"].fillna(0.0)) if len(portion) else 0.0
        excess = strategy - benchmark
        folds.append({
            "fold": index,
            "periods": int(len(portion)),
            "total_return_pct": round(strategy * 100, 6),
            "benchmark_return_pct": round(benchmark * 100, 6),
            "excess_return_pct": round(excess * 100, 6),
            "passed": bool(strategy > 0 and excess > 0),
        })
    return folds


def _assistant_experiment_hypothesis(
    report: dict[str, Any], *, change_path: str, expected_metric: str,
    direction: str, success_threshold: float,
) -> dict[str, Any]:
    source_run_id = str(report.get("run_id") or "unpublished-report")
    identity = {
        "source_run_id": source_run_id,
        "source_spec_sha256": result_spec_sha256(report.get("spec") or {}),
        "change_path": change_path,
        "expected_metric": expected_metric,
        "direction": direction,
        "success_threshold": success_threshold,
    }
    return {
        "hypothesis_id": "hyp-assistant-" + hashlib.sha256(json.dumps(
            identity, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()[:20],
        "source_gate": {
            "status": "assistant_exploratory",
            "eligible_for_confirmation": False,
            "source_run_id": source_run_id,
        },
        "expected_metric": expected_metric,
        "direction": direction,
        "success_threshold": success_threshold,
        "cost_seconds": 0.0,
        "cost_scope": "not_recorded",
    }


def _local_assistant(report: dict[str, Any]) -> dict[str, Any]:
    """Explain a completed run with deterministic local rules only."""
    metrics = report.get("metrics") or {}
    development = metrics.get("development") or {}
    validation = metrics.get("validation") or {}
    test = metrics.get("test") or {}
    spec = dict(report.get("spec") or {})
    proposed = json.loads(json.dumps(spec, ensure_ascii=False)) if spec else {}
    risks: list[dict[str, str]] = []
    strengths: list[str] = []
    suggestions: list[dict[str, Any]] = []

    development_annual = float(development.get("annualized_return_pct") or 0)
    validation_annual = float(validation.get("annualized_return_pct") or 0)
    test_annual = float(test.get("annualized_return_pct") or 0)
    gap = development_annual - validation_annual
    if gap >= 15 or (development_annual > 0 >= validation_annual):
        risks.append({
            "severity": "high", "code": "development_validation_gap",
            "message": f"开发与验证年化相差 {gap:.2f} 个百分点，存在过拟合或状态失配。",
        })
        next_top_n = min(int(spec.get("top_n", 30)) + 20, 100)
        if (proposed and not suggestions
                and next_top_n != int(spec.get("top_n", 30))):
            proposed["top_n"] = next_top_n
            suggestions.append({
                "priority": "P0", "action": "只改变分散度做下一次实验",
                "reason": "先扩大持仓数，检验收益是否依赖少数极端样本；这是待验证假设，不是优化结论。",
                "experiment": _assistant_experiment_hypothesis(
                    report, change_path="top_n",
                    expected_metric="shanghai_excess_pct",
                    direction="increase", success_threshold=0.01,
                ),
            })
    if float(test.get("max_drawdown_pct") or -100) < -20:
        risks.append({
            "severity": "high", "code": "drawdown",
            "message": f"测试最大回撤 {float(test.get('max_drawdown_pct') or 0):.2f}%，超过20%研究上限。",
        })
        if proposed and not suggestions and spec.get("risk_rule") == "none":
            proposed["risk_rule"] = "breadth_majority"
            suggestions.append({
                "priority": "P0", "action": "单独检验市场宽度新开仓门控（旧仓按持有期到期）",
                "reason": "只增加已定义的多数趋势风控，观察尾部损失是否下降。",
                "experiment": _assistant_experiment_hypothesis(
                    report, change_path="risk_rule",
                    expected_metric="max_drawdown_pct",
                    direction="increase", success_threshold=2.0,
                ),
            })
    if float(test.get("annualized_excess_return_pct") or 0) <= 0:
        risks.append({
            "severity": "high", "code": "negative_excess",
            "message": "测试期年化超额不为正，当前机制没有证明优于宽基持有。",
        })
    if float(test.get("period_win_rate_pct") or 0) < 55:
        risks.append({
            "severity": "medium", "code": "low_win_rate",
            "message": f"测试期胜率仅 {float(test.get('period_win_rate_pct') or 0):.2f}%。",
        })
    if float(test.get("block_bootstrap_95pct_lower_mean_pct") or 0) <= 0:
        risks.append({
            "severity": "high", "code": "bootstrap_uncertain",
            "message": "区块自助法95%下界不为正，正均值尚不能排除抽样偶然。",
        })
    if float(test.get("filled_execution_rate_pct") or 0) < 98:
        risks.append({
            "severity": "medium", "code": "fill_rate",
            "message": "目标执行标签覆盖不足98%，缺失目标已按现金槽处理。",
        })
    if float(test.get("maximum_position_weight_pct") or 100) > 5:
        risks.append({
            "severity": "medium", "code": "concentration",
            "message": "单票等权上限高于5%，组合集中度偏高。",
        })

    cost_rows = (report.get("diagnostics") or {}).get("cost_ladder", {}).get("test", [])
    cost_10 = next((row for row in cost_rows if row.get("one_way_cost_bps") == 10), {})
    cost_30 = next((row for row in cost_rows if row.get("one_way_cost_bps") == 30), {})
    if float(cost_10.get("total_return_pct") or 0) > 0 >= float(cost_30.get("total_return_pct") or 0):
        risks.append({
            "severity": "medium", "code": "cost_fragile",
            "message": "成本从10BP升至30BP后累计收益转负，机制对摩擦敏感。",
        })
        if (proposed and not suggestions
                and int(spec.get("holding_days", 5)) == 5):
            proposed["holding_days"] = 10
            suggestions.append({
                "priority": "P1", "action": "单独测试10日调仓",
                "reason": "降低调仓频率可能减小成本拖累；必须重新跑完整固定分段。",
                "experiment": _assistant_experiment_hypothesis(
                    report, change_path="holding_days",
                    expected_metric="total_fees_yuan",
                    direction="decrease", success_threshold=0.01,
                ),
            })

    for label, condition in (
        ("测试期累计收益为正", float(test.get("total_return_pct") or 0) > 0),
        ("测试期最大回撤在20%以内", float(test.get("max_drawdown_pct") or -100) >= -20),
        ("双倍成本后累计收益为正", float(test.get("cost_stress_total_return_pct") or 0) > 0),
        ("执行标签覆盖至少98%", float(test.get("filled_execution_rate_pct") or 0) >= 98),
    ):
        if condition:
            strengths.append(label)
    if not suggestions:
        has_high_risk = any(row.get("severity") == "high" for row in risks)
        if report.get("historical_validation_gate_passed") is True and not has_high_risk:
            suggestions.append({
                "priority": "P1", "action": "冻结当前规格并做前向纸面观察",
                "reason": "不要继续查看测试段后反复调参；纸面观察只记录全新前向样本。",
            })
        else:
            suggestions.append({
                "priority": "P0", "action": "停止当前规格，不进入纸面观察",
                "reason": "当前机制未通过历史门禁；下一次实验必须来自独立经济假设或全新样本，不能延用同一失败规格。",
            })
            proposed = {}
    if proposed:
        proposed.pop("strategy_id", None)
        proposed["name"] = f"{str(spec.get('name') or '策略')[:46]} · 助手假设"
    return {
        "mode": "local_statistical_rule_engine",
        "label": "本地智能研究助手（统计+规则）",
        "generated_at": _now(),
        "external_model_used": False,
        "llm_used": False,
        "api_used": False,
        "network_used": False,
        "cost_rmb": 0,
        "verdict": (
            "历史门禁通过，但仍只是一条待独立复现的研究候选。"
            if report.get("historical_validation_gate_passed") else
            "当前策略没有通过固定样本外门禁，不能用于买入提醒。"
        ),
        "risk_flags": risks,
        "strengths": strengths,
        "suggestions": suggestions,
        "proposed_spec": proposed,
        "warning": "建议是基于已见回测的研究假设；应用后会产生新规格，不能把同一测试段继续称为未触碰样本。",
    }


def load_backtest_report(run_id: Any) -> dict[str, Any]:
    safe = _safe_run_id(run_id)
    if safe is None:
        raise StrategyValidationError("回测运行ID无效")
    path = REPORTS_DIR / f"{safe}.json"
    stored_report = _read_json(path)
    if not stored_report:
        raise StrategyValidationError("未找到该回测报告")
    report = _with_current_epoch_status(stored_report)
    report["integrity"] = _report_integrity(stored_report)
    try:
        _write_report_index(path, stored_report)
    except OSError:
        pass
    return report


def assistant_for_run(run_id: Any) -> dict[str, Any]:
    report = load_backtest_report(run_id)
    return _local_assistant(report)


_V4_FACTOR_COLUMNS = {
    "trend60", "residual_momentum_20_5", "low_volatility_20",
    "price_volume_confirmation", "liquidity_20",
}


def _forward_logic_sha256() -> str:
    """Version the exact, deliberately narrow paper-forward semantics."""
    implementation = "\n".join(inspect.getsource(function) for function in (
        _forward_signal_payload,
        _advance_forward_observation,
        _settle_forward_signal,
        _net_after_cost,
    ))
    return _payload_sha256({
        "version": FORWARD_LOGIC_VERSION,
        "model": "forward_shadow_label_continuation",
        "source": "published_raw_v4_daily_manifest_and_quality",
        "signal": "same_day_cross_section_rank_after_completed_close",
        "entry": "next_session_open_if_tradable",
        "exit": "signal_plus_h_sessions_close_if_tradable",
        "portfolio": "equal_weight_fixed_top_n_denominator",
        "no_backfill": True,
        "recommendation_eligible": False,
        "v4_factor_pipeline_sha256": _sha256(V4_FACTOR_PIPELINE_PATH),
        "implementation_sha256": hashlib.sha256(
            implementation.encode("utf-8")
        ).hexdigest(),
        "user_strategy_lab_module_sha256": _sha256(Path(__file__).resolve()),
    })


def _json_document(path: Path, label: str) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise StrategyValidationError(f"{label}不可读: {exc}") from exc
    if not isinstance(value, dict):
        raise StrategyValidationError(f"{label}格式无效")
    return value, hashlib.sha256(raw).hexdigest()


def _forward_read_json(
    path: Path,
    *,
    observation_id: str | None = None,
    expected_events: set[str] | None = None,
) -> dict[str, Any]:
    """Strictly validate forward state/evidence; corruption is never absence."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ForwardEvidenceError(f"前向证据不可读: {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != FORWARD_SCHEMA:
        raise ForwardEvidenceError(f"前向证据schema无效: {path}")
    if observation_id and value.get("observation_id") != observation_id:
        raise ForwardEvidenceError(f"前向证据observation_id不匹配: {path}")
    if path.name == "observation.json":
        required = {
            "observation_id", "origin_run_id", "origin_report_sha256",
            "strategy_id", "research_epoch", "result_spec_sha256",
            "forward_logic_sha256", "schedule_anchor", "first_eligible_date", "spec",
        }
        if any(value.get(key) in (None, "") for key in required):
            raise ForwardEvidenceError(f"前向观察冻结字段不完整: {path}")
    elif path.name == "state.json":
        required = {
            "observation_id", "updated_at", "last_source_version",
            "last_source_data_as_of", "schedule_cursor_date",
            "calendar_prefix_sha256",
        }
        if any(value.get(key) in (None, "") for key in required):
            raise ForwardEvidenceError(f"前向状态字段不完整: {path}")
    if expected_events is not None:
        event = value.get("event")
        if event not in expected_events:
            raise ForwardEvidenceError(f"前向事件类型无效: {path}")
        date_value = value.get("signal_date") or value.get("expected_date")
        try:
            datetime.fromisoformat(str(date_value))
        except (TypeError, ValueError) as exc:
            raise ForwardEvidenceError(f"前向事件日期无效: {path}") from exc
        if event == "signal_frozen":
            required = {
                "signal_date", "result_spec_sha256", "forward_logic_sha256",
                "target_set_sha256", "holding_days", "top_n_denominator",
                "one_way_cost_bps", "targets",
            }
            if any(key not in value for key in required):
                raise ForwardEvidenceError(f"前向信号字段不完整: {path}")
            if (
                value.get("holding_days") not in {5, 10}
                or not isinstance(value.get("targets"), list)
                or value.get("target_set_sha256")
                != _payload_sha256({"targets": value.get("targets")})
                or path.stem != value.get("signal_date")
            ):
                raise ForwardEvidenceError(f"前向信号内容或文件名无效: {path}")
        elif event == "label_settled":
            if (
                not value.get("signal_date")
                or not value.get("signal_sha256")
                or not isinstance(value.get("targets"), list)
                or not isinstance(value.get("net_return"), (int, float))
                or path.stem != value.get("signal_date")
            ):
                raise ForwardEvidenceError(f"前向结算字段或文件名无效: {path}")
        elif event == "signal_missed":
            if path.stem != f"signal_missed_{value.get('expected_date')}":
                raise ForwardEvidenceError(f"错过信号事件文件名无效: {path}")
        elif event == "settlement_missed":
            if (
                not value.get("signal_date")
                or path.stem != f"settlement_missed_{value.get('signal_date')}"
            ):
                raise ForwardEvidenceError(f"错过结算事件文件名无效: {path}")
    return value


def _validate_forward_settlement_bindings(
    signals: list[dict[str, Any]], settlements: list[dict[str, Any]],
) -> None:
    signals_by_date = {str(item["signal_date"]): item for item in signals}
    for settlement in settlements:
        signal = signals_by_date.get(str(settlement["signal_date"]))
        if signal is None or _payload_sha256(signal) != settlement.get("signal_sha256"):
            raise ForwardEvidenceError("前向结算未绑定对应不可变信号")
        results = settlement.get("targets") or []
        signal_codes = [str(item.get("code")) for item in signal.get("targets") or []]
        result_codes = [str(item.get("code")) for item in results]
        filled = [item for item in results if item.get("filled") is True]
        gross_values = []
        for item in filled:
            value = item.get("gross_return")
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ForwardEvidenceError("前向结算目标收益不是有限数")
            gross_values.append(float(value))
        top_n = int(signal["top_n_denominator"])
        cost = float(signal["one_way_cost_bps"]) / 10_000.0
        expected_gross = sum(gross_values) / top_n
        expected_net = sum(
            (1.0 + value) * (1.0 - cost) / (1.0 + cost) - 1.0
            for value in gross_values
        ) / top_n
        declared_gross = settlement.get("gross_return")
        declared_net = settlement.get("net_return")
        semantics_match = bool(
            result_codes == signal_codes
            and settlement.get("holding_days") == signal.get("holding_days")
            and int(settlement.get("selected_stocks") or 0)
            == int(signal.get("selected_stocks") or 0)
            and int(settlement.get("filled_executions") or 0) == len(filled)
            and isinstance(declared_gross, (int, float))
            and isinstance(declared_net, (int, float))
            and math.isfinite(float(declared_gross))
            and math.isfinite(float(declared_net))
            and abs(float(declared_gross) - expected_gross) <= 1e-10
            and abs(float(declared_net) - expected_net) <= 1e-10
        )
        if not semantics_match:
            raise ForwardEvidenceError("前向结算目标或收益聚合与冻结信号不一致")


def _validate_forward_signal_bindings(
    observation: dict[str, Any], signals: list[dict[str, Any]],
) -> None:
    spec = observation.get("spec") or {}
    special_sha = (observation.get("special_price_events") or {}).get("sha256")
    for signal in signals:
        source = signal.get("source") or {}
        if not (
            signal.get("origin_run_id") == observation.get("origin_run_id")
            and signal.get("strategy_id") == observation.get("strategy_id")
            and signal.get("result_spec_sha256") == observation.get("result_spec_sha256")
            and signal.get("forward_logic_sha256") == observation.get("forward_logic_sha256")
            and signal.get("special_price_events_sha256") == special_sha
            and signal.get("model") == "forward_shadow_label_continuation"
            and signal.get("paper_only") is True
            and signal.get("recommendation_eligible") is False
            and int(signal.get("holding_days") or 0) == int(spec.get("holding_days") or 0)
            and int(signal.get("top_n_denominator") or 0) == int(spec.get("top_n") or 0)
            and signal.get("one_way_cost_bps") is not None
            and int(signal["one_way_cost_bps"])
            == int(spec.get("one_way_cost_bps") or 0)
            and source.get("data_as_of") == signal.get("signal_date")
        ):
            raise ForwardEvidenceError("前向信号与观察冻结身份或执行参数不一致")


def _forward_immutable_json(path: Path, value: dict[str, Any]) -> None:
    """Publish one complete immutable event with atomic no-overwrite linking."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    temporary = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != encoded:
                raise RuntimeError(f"不可变前向证据已存在且内容不同: {path}")
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _forward_source_state() -> dict[str, Any]:
    """Return only a fully published, fully audited raw-V4 source version."""
    manifest, manifest_sha = _json_document(V4_MANIFEST_PATH, "V4发布清单")
    quality, quality_sha = _json_document(QUALITY_PATH, "V4质量审计")
    calendar, calendar_sha = _json_document(V4_CALENDAR_PATH, "V4交易日历")
    query = manifest.get("query") or {}
    quality_window = quality.get("query_window") or {}
    quality_inputs = quality.get("input_fingerprints") or {}
    files = manifest.get("files") or []
    dates = calendar.get("trading_dates") or []
    try:
        parsed_dates = [datetime.fromisoformat(str(day)).date() for day in dates]
    except (TypeError, ValueError):
        parsed_dates = []
    calendar_is_strict = bool(
        parsed_dates
        and len(parsed_dates) == len(dates)
        and all(
            parsed_dates[index] < parsed_dates[index + 1]
            for index in range(len(parsed_dates) - 1)
        )
    )
    data_as_of = str(query.get("end_date") or "")
    target = int(manifest.get("target_symbols") or 0)
    checks = {
        "manifest_ready": manifest.get("ready_for_factor_research") is True,
        "quality_ready": quality.get("ready_for_factor_research") is True,
        "full_universe_ready": quality.get("full_universe_ready") is True,
        "manifest_hash_audited": (
            quality_inputs.get("download_manifest_sha256") == manifest_sha
        ),
        "same_published_date": bool(
            data_as_of
            and quality_window.get("end_date") == data_as_of
            and calendar.get("end_date") == data_as_of
        ),
        "exact_universe": bool(
            target == 4_802
            and int(manifest.get("downloaded_symbols") or 0) == 4_802
            and len(files) == 4_802
            and int(quality.get("target_symbols") or 0) == 4_802
            and int(quality.get("file_count") or 0) == 4_802
            and int(quality.get("passed_files") or 0) == 4_802
        ),
        "zero_failures": bool(
            not (manifest.get("failures") or [])
            and int(quality.get("failed_files") or 0) == 0
            and not (quality.get("failures") or [])
        ),
        "calendar_contains_end": bool(dates and dates[-1] == data_as_of),
        "calendar_strictly_increasing": calendar_is_strict,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise StrategyValidationError(
            "V4完整发布门禁未通过: " + ", ".join(failed)
        )
    return {
        "data_as_of": data_as_of,
        "generated_at": max(
            str(manifest.get("generated_at") or ""),
            str(quality.get("generated_at") or ""),
        ),
        "source_version": _payload_sha256({
            "data_as_of": data_as_of,
            "manifest_sha256": manifest_sha,
            "quality_sha256": quality_sha,
            "calendar_sha256": calendar_sha,
        }),
        "manifest_sha256": manifest_sha,
        "quality_sha256": quality_sha,
        "calendar_sha256": calendar_sha,
        "files": files,
        "trading_dates": [str(day) for day in dates],
        "checks": checks,
    }


def _forward_public_source(source: dict[str, Any]) -> dict[str, Any]:
    return {
        key: source.get(key) for key in (
            "data_as_of", "generated_at", "source_version",
            "manifest_sha256", "quality_sha256", "calendar_sha256",
        )
    }


def _forward_calendar_prefix_sha256(
    source: dict[str, Any], through: str | None = None,
) -> str:
    end = through or str(source.get("data_as_of") or "")
    dates = [str(day) for day in source.get("trading_dates") or [] if str(day) <= end]
    if not dates or dates[-1] != end:
        raise StrategyValidationError(f"V4交易日历缺少已发布终点: {end}")
    return _payload_sha256({"trading_dates": dates})


def _forward_observation_dir(observation_id: str) -> Path:
    return FORWARD_DIR / observation_id


def _forward_observation(observation_id: Any) -> dict[str, Any]:
    candidate = str(observation_id or "")
    if not candidate.startswith("obs-") or "/" in candidate or ".." in candidate:
        return {}
    path = _forward_observation_dir(candidate) / "observation.json"
    if not path.is_file():
        return {}
    value = _forward_read_json(path, observation_id=candidate)
    if (
        _safe_run_id(value.get("origin_run_id")) is None
        or not value.get("result_spec_sha256")
        or not value.get("schedule_anchor")
    ):
        raise ForwardEvidenceError(f"前向观察冻结字段不完整: {path}")
    return value


def _forward_support_blockers(spec: dict[str, Any]) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    active = sorted(
        name for name, weight in (spec.get("factors") or {}).items() if weight
    )
    unsupported = sorted(set(active) - _V4_FACTOR_COLUMNS)
    if unsupported:
        blockers.append({
            "code": "unsupported_factor_columns",
            "text": "前向V1仅支持已发布raw V4五列: " + ", ".join(unsupported),
        })
    if int(spec.get("holding_days") or 0) not in {5, 10}:
        blockers.append({
            "code": "unsupported_holding_days",
            "text": "前向V1仅支持5或10个交易日标签续接",
        })
    requirements = (
        ("compose_method", "rank_weighted_v1", "unsupported_compose_method"),
        ("entry_rule", "none", "unsupported_entry_rule"),
        ("exposure_rule", "none", "unsupported_exposure_rule"),
    )
    for key, expected, code in requirements:
        if str(spec.get(key) or "none") != expected:
            blockers.append({
                "code": code,
                "text": f"前向V1尚未实现 {key}={spec.get(key)}",
            })
    if int(spec.get("rebal_buffer") or 0) != 0:
        blockers.append({
            "code": "unsupported_rebal_buffer",
            "text": "前向V1仅支持 rebal_buffer=0",
        })
    return blockers


def _forward_schedule_anchor(
    report: dict[str, Any], source: dict[str, Any], holding_days: int,
) -> tuple[str, str]:
    candidates = [
        str((report.get("current_research_state") or {}).get("signal_date") or "")
    ]
    curves = ((report.get("diagnostics") or {}).get("equity_curve") or {})
    for points in curves.values():
        if isinstance(points, list):
            candidates.extend(
                str(point.get("date") or "")
                for point in points if isinstance(point, dict)
            )
    dates = source["trading_dates"]
    valid = sorted({day for day in candidates if day in dates})
    if not valid:
        raise StrategyValidationError(
            "起始回测没有可审计且能映射到V4交易日历的信号锚点"
        )
    anchor = valid[-1]
    position = dates.index(anchor)
    last_position = dates.index(source["data_as_of"])
    while position + holding_days <= last_position:
        position += holding_days
    return anchor, dates[position]


def _strategy_definition_sha256(saved: dict[str, Any]) -> str:
    return _payload_sha256({
        "strategy_id": saved.get("strategy_id"),
        "version": int(saved.get("version") or 0),
        "research_epoch": saved.get("research_epoch"),
        "result_spec_sha256": saved.get("result_spec_sha256"),
        "spec": saved.get("spec") or {},
    })


def _freeze_paper_observation(
    saved: dict[str, Any],
    *,
    run_id: Any,
    existing_promotion: dict[str, Any],
) -> dict[str, Any]:
    safe_run = _safe_run_id(run_id)
    if safe_run is None:
        raise StrategyValidationError("开启纸面前向观察必须绑定合法 run_id")
    report_path = REPORTS_DIR / f"{safe_run}.json"
    report = _read_json(report_path)
    if not report:
        raise StrategyValidationError("未找到要绑定的回测报告")
    if report.get("run_id") != safe_run:
        raise StrategyValidationError("回测报告内部run_id与文件名不一致")
    if report.get("strategy_id") != saved.get("strategy_id"):
        raise StrategyValidationError("回测报告不属于该策略")
    required_evidence = (
        ("spec_path", "spec_sha256", "策略规格"),
        ("periods_path", "periods_sha256", "逐期收益"),
        ("selected_path", "selected_sha256", "逐期目标"),
        ("factor_ic_path", "factor_ic_sha256", "逐期因子IC"),
    )
    missing_evidence = [
        label for path_key, sha_key, label in required_evidence
        if not report.get(path_key) or not report.get(sha_key)
    ]
    if missing_evidence:
        raise StrategyValidationError(
            "起始回测报告缺少必需完整性证据: " + "、".join(missing_evidence)
        )
    integrity = _report_integrity(report)
    if integrity.get("all_passed") is not True:
        raise StrategyValidationError("起始回测报告完整性校验未全部通过")
    current_epoch, reason = _current_epoch_eligibility(report)
    if not current_epoch:
        raise StrategyValidationError(reason or "回测报告不属于当前研究epoch")
    spec = saved.get("spec") or {}
    spec_hash = str(saved.get("result_spec_sha256") or result_spec_sha256(spec))
    if result_spec_sha256(report.get("spec") or {}) != spec_hash:
        raise StrategyValidationError("回测报告规格与策略当前规格不一致")
    blockers = _forward_support_blockers(spec)
    if blockers:
        raise StrategyValidationError("；".join(row["text"] for row in blockers))

    current_id = existing_promotion.get("paper_observation_id")
    current = _forward_observation(current_id)
    if (
        existing_promotion.get("paper_observe") is True
        and current.get("origin_run_id") == safe_run
        and current.get("result_spec_sha256") == spec_hash
        and current.get("forward_logic_sha256") == _forward_logic_sha256()
    ):
        return current

    source_handle = _acquire_v4_read_lock()
    if source_handle is None:
        raise StrategyValidationError("V4增量发布进行中，请发布完成后重试开启观察")
    try:
        source = _forward_source_state()
    finally:
        _release_v4_read_lock(source_handle)
    observed_at = _now()
    next_day = (
        datetime.fromisoformat(observed_at).date() + timedelta(days=1)
    ).isoformat()
    first_eligible = max(next_day, str(factor_config.FORWARD_SHADOW_START))
    schedule_anchor, schedule_cursor = _forward_schedule_anchor(
        report, source, int(spec["holding_days"]),
    )
    observation_id = (
        "obs-" + datetime.now(SHANGHAI).strftime("%Y%m%d%H%M%S")
        + "-" + secrets.token_hex(4)
    )
    observation = {
        "schema": FORWARD_SCHEMA,
        "observation_id": observation_id,
        "model": "forward_shadow_label_continuation",
        "observed_at": observed_at,
        "first_eligible_date": first_eligible,
        "schedule_anchor": schedule_anchor,
        "schedule_rule": "anchor_plus_integer_h_trading_sessions",
        "origin_run_id": safe_run,
        "origin_report_path": str(report_path.relative_to(ROOT)),
        "origin_report_sha256": _sha256(report_path),
        "origin_report_integrity": integrity,
        "strategy_path": str(
            (STRATEGIES_DIR / f"{saved['strategy_id']}.json").relative_to(ROOT)
        ),
        "strategy_sha256_at_observation": _sha256(
            STRATEGIES_DIR / f"{saved['strategy_id']}.json"
        ),
        "strategy_definition_sha256": _strategy_definition_sha256(saved),
        "strategy_id": saved["strategy_id"],
        "strategy_version": int(saved.get("version") or 0),
        "research_epoch": saved.get("research_epoch"),
        "result_spec_sha256": spec_hash,
        "forward_logic_version": FORWARD_LOGIC_VERSION,
        "forward_logic_sha256": _forward_logic_sha256(),
        "v4_factor_pipeline_sha256": _sha256(V4_FACTOR_PIPELINE_PATH),
        "special_price_events": {
            "path": str(V4_SPECIAL_EVENTS_PATH.relative_to(ROOT)),
            "sha256": _sha256(V4_SPECIAL_EVENTS_PATH),
            "used_by_signal_calculation": True,
            "used_by_signal_eligibility": True,
            "used_by_label_calculation": False,
            "reason": "信号沿用正式V4 verified-event排除；标签本身直接复用compute_security_factors",
        },
        "spec": spec,
        "source_at_observation": _forward_public_source(source),
        "paper_only": True,
        "recommendation_eligible": False,
        "counts_toward_promotion": False,
        "no_backfill": True,
    }
    directory = _forward_observation_dir(observation_id)
    _forward_immutable_json(directory / "observation.json", observation)
    _atomic_json(directory / "state.json", {
        "schema": FORWARD_SCHEMA,
        "observation_id": observation_id,
        "updated_at": observed_at,
        "last_source_version": source["source_version"],
        "last_source_data_as_of": source["data_as_of"],
        "calendar_prefix_sha256": _forward_calendar_prefix_sha256(source),
        "last_signal_date": None,
        "schedule_cursor_date": schedule_cursor,
        "unobserved_source_dates": [],
        "last_error": None,
    })
    return observation


def _acquire_forward_lock():
    if not FORWARD_RUN_LOCK.acquire(blocking=False):
        return None
    FORWARD_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = FORWARD_LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        FORWARD_RUN_LOCK.release()
        return None
    handle.seek(0)
    handle.truncate()
    json.dump({
        "schema": "user_strategy_forward_lock_v1",
        "pid": os.getpid(),
        "acquired_at": _now(),
    }, handle, ensure_ascii=False, separators=(",", ":"))
    handle.flush()
    return handle


def _release_forward_lock(handle) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
    finally:
        FORWARD_RUN_LOCK.release()


def _acquire_v4_read_lock():
    """Keep the incremental writer out while one published version is read."""
    V4_INCREMENTAL_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = V4_INCREMENTAL_LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _release_v4_read_lock(handle) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


_FORWARD_RAW_COLUMNS = [
    "date", "code", "open", "close", "amount", "tradestatus", "pctChg", "isST",
]


def _read_published_raw(item: dict[str, Any]) -> pd.DataFrame:
    relative = Path(str(item.get("path") or ""))
    path = ROOT / relative
    try:
        path.resolve().relative_to(V4_DAILY_DIR.resolve())
    except (OSError, ValueError) as exc:
        raise StrategyValidationError("V4清单包含越界数据路径") from exc
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise StrategyValidationError(f"V4数据文件不可读: {relative}") from exc
    if hashlib.sha256(raw).hexdigest() != item.get("sha256"):
        raise StrategyValidationError(
            f"V4数据文件正在更新或与发布清单不一致: {relative.name}"
        )
    return pd.read_csv(
        io.BytesIO(raw), compression="gzip", usecols=_FORWARD_RAW_COLUMNS,
        dtype={"date": str, "code": str},
    )


def _build_forward_signal_panel(source: dict[str, Any]) -> pd.DataFrame:
    from research.v4_factor_pipeline import (
        _verified_special_events,
        compute_security_factors,
    )

    rows: list[dict[str, Any]] = []
    data_as_of = source["data_as_of"]
    special_events = _verified_special_events()
    for item in source["files"]:
        raw = _read_published_raw(item)
        raw = raw[raw["date"].le(data_as_of)].tail(140)
        if raw.empty or str(raw.iloc[-1]["date"]) != data_as_of:
            continue
        calculated = compute_security_factors(raw)
        row = calculated.iloc[-1]
        if (str(row["code"]), data_as_of) in special_events:
            continue
        if not bool(row.get("base_eligible")):
            continue
        rows.append({
            "trade_date": data_as_of,
            "code": str(row["code"]),
            "source_path": str(item["path"]),
            **{
                factor: (
                    float(row[factor]) if pd.notna(row[factor]) else np.nan
                ) for factor in _V4_FACTOR_COLUMNS
            },
        })
    return pd.DataFrame(rows)


def _forward_signal_payload(
    observation: dict[str, Any],
    source: dict[str, Any],
    panel: pd.DataFrame,
) -> dict[str, Any]:
    spec = observation["spec"]
    active = [name for name, weight in spec["factors"].items() if weight]
    ranked = panel.copy()
    for factor in active:
        ranked[f"rank_{factor}"] = ranked[factor].rank(
            method="average", pct=True,
        )
    ranked["score"] = sum(
        float(spec["factors"][factor]) * ranked[f"rank_{factor}"]
        for factor in active
    )
    eligible = ranked.dropna(subset=active + ["score"]).copy()
    breadth = float(eligible["trend60"].gt(0).mean()) if len(eligible) else 0.0
    risk_on = spec["risk_rule"] == "none" or breadth >= 0.5
    selected = eligible.sort_values(
        ["score", "code"], ascending=[False, True],
    ).head(int(spec["top_n"])) if risk_on else eligible.iloc[:0]
    targets = []
    for row in selected.itertuples(index=False):
        targets.append({
            "code": str(row.code),
            "source_path": str(row.source_path),
            "score": round(float(row.score), 12),
            "factors": {
                factor: round(float(getattr(row, factor)), 12)
                for factor in active
            },
        })
    payload = {
        "schema": FORWARD_SCHEMA,
        "event": "signal_frozen",
        "model": "forward_shadow_label_continuation",
        "observation_id": observation["observation_id"],
        "origin_run_id": observation["origin_run_id"],
        "strategy_id": observation["strategy_id"],
        "result_spec_sha256": observation["result_spec_sha256"],
        "forward_logic_sha256": observation["forward_logic_sha256"],
        "special_price_events_sha256": (
            observation.get("special_price_events") or {}
        ).get("sha256"),
        "signal_date": source["data_as_of"],
        "frozen_at": _now(),
        "source": _forward_public_source(source),
        "risk_on": bool(risk_on),
        "market_breadth": round(breadth, 12),
        "eligible_stocks": int(len(eligible)),
        "selected_stocks": int(len(targets)),
        "top_n_denominator": int(spec["top_n"]),
        "holding_days": int(spec["holding_days"]),
        "one_way_cost_bps": int(spec["one_way_cost_bps"]),
        "targets": targets,
        "paper_only": True,
        "recommendation_eligible": False,
        "label_known_when_frozen": False,
        "no_backfill": True,
    }
    payload["target_set_sha256"] = _payload_sha256({"targets": targets})
    return payload


def _forward_event_files(observation_id: str, kind: str) -> list[Path]:
    return sorted((_forward_observation_dir(observation_id) / kind).glob("*.json"))


def _forward_expected_date(
    signal_date: str, holding_days: int, source: dict[str, Any],
) -> str | None:
    dates = source["trading_dates"]
    try:
        return dates[dates.index(signal_date) + holding_days]
    except (IndexError, ValueError):
        return None


def _forward_missed_event(
    observation: dict[str, Any],
    *,
    event: str,
    expected_date: str,
    source: dict[str, Any],
    signal_date: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": FORWARD_SCHEMA,
        "event": event,
        "model": "forward_shadow_label_continuation",
        "observation_id": observation["observation_id"],
        "origin_run_id": observation["origin_run_id"],
        "strategy_id": observation["strategy_id"],
        "signal_date": signal_date,
        "expected_date": expected_date,
        "first_observed_later_source": _forward_public_source(source),
        "recorded_at": _now(),
        "reason": "未在该完整发布版本当日观察到，按no_backfill规则永久跳过",
        "paper_only": True,
        "recommendation_eligible": False,
    }


def _settle_forward_signal(
    observation: dict[str, Any],
    signal: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    from research.v4_factor_pipeline import compute_security_factors

    files_by_path = {
        str(item.get("path")): item for item in source.get("files") or []
    }
    horizon = int(signal["holding_days"])
    target_results = []
    for target in signal.get("targets") or []:
        item = files_by_path.get(str(target.get("source_path")))
        if item is None:
            raise StrategyValidationError(
                f"当前发布清单缺少目标股文件: {target.get('code')}"
            )
        raw = _read_published_raw(item).sort_values("date").reset_index(drop=True)
        calculated = compute_security_factors(raw)
        positions = calculated.index[
            calculated["date"].dt.date.astype(str).eq(signal["signal_date"])
        ].tolist()
        index = positions[0] if positions else None
        has_horizon = bool(index is not None and index + horizon < len(raw))
        if has_horizon:
            entry = raw.iloc[index + 1]
            exit_row = raw.iloc[index + horizon]
            label = calculated.iloc[index][f"label_return_{horizon}d"]
            filled = bool(pd.notna(label))
            gross = float(label) if filled else None
            entry_date = str(entry["date"])
            exit_date = str(exit_row["date"])
            unfilled_reason = None if filled else "entry_or_exit_not_tradable"
        else:
            # The market-wide label is mature, but this name has no T+h row
            # (normally delisted). Historical semantics keep that slot in cash.
            filled = False
            gross = None
            entry_date = (
                str(raw.iloc[index + 1]["date"])
                if index is not None and index + 1 < len(raw) else None
            )
            exit_date = None
            unfilled_reason = (
                "signal_date_missing" if index is None else "security_ended_before_horizon"
            )
        target_results.append({
            "code": target["code"],
            "filled": filled,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "unfilled_reason": unfilled_reason,
            "return_basis": "pctChg_return_index_T_plus_1_open_to_T_plus_h_close",
            "gross_return": round(gross, 12) if gross is not None else None,
        })
    top_n = max(int(signal["top_n_denominator"]), 1)
    cost = float(signal["one_way_cost_bps"]) / 10_000.0
    gross_values = [
        float(row["gross_return"]) for row in target_results if row["filled"]
    ]
    net_values = [
        (1.0 + value) * (1.0 - cost) / (1.0 + cost) - 1.0
        for value in gross_values
    ]
    return {
        "schema": FORWARD_SCHEMA,
        "event": "label_settled",
        "model": "forward_shadow_label_continuation",
        "observation_id": observation["observation_id"],
        "origin_run_id": observation["origin_run_id"],
        "strategy_id": observation["strategy_id"],
        "signal_date": signal["signal_date"],
        "signal_sha256": _payload_sha256(signal),
        "settled_at": _now(),
        "settlement_source": _forward_public_source(source),
        "holding_days": horizon,
        "selected_stocks": int(signal.get("selected_stocks") or 0),
        "filled_executions": len(gross_values),
        "gross_return": round(sum(gross_values) / top_n, 12),
        "net_return": round(sum(net_values) / top_n, 12),
        "targets": target_results,
        "paper_only": True,
        "recommendation_eligible": False,
        "counts_toward_promotion": False,
    }


def _observation_invalid_reason(observation: dict[str, Any]) -> str | None:
    if observation.get("research_epoch") != factor_config.RESEARCH_EPOCH:
        return "观察research epoch已不是当前严格epoch"
    strategy = _read_json(
        STRATEGIES_DIR / f"{observation.get('strategy_id')}.json"
    )
    if not strategy:
        return "当前策略文件不存在"
    if strategy.get("research_epoch") != observation.get("research_epoch"):
        return "策略research epoch已变化"
    current_hash = strategy.get("result_spec_sha256") or result_spec_sha256(
        strategy.get("spec") or {},
    )
    if current_hash != observation.get("result_spec_sha256"):
        return "策略结果规格已变化"
    if _strategy_definition_sha256(strategy) != observation.get(
        "strategy_definition_sha256"
    ):
        return "策略定义版本已变化"
    if observation.get("forward_logic_sha256") != _forward_logic_sha256():
        return "前向计算逻辑版本已变化"
    special = observation.get("special_price_events") or {}
    if (
        not V4_SPECIAL_EVENTS_PATH.is_file()
        or _sha256(V4_SPECIAL_EVENTS_PATH) != special.get("sha256")
    ):
        return "verified special-event排除表已变化"
    report_path = ROOT / str(observation.get("origin_report_path") or "")
    if not report_path.is_file() or _sha256(report_path) != observation.get(
        "origin_report_sha256"
    ):
        return "起始回测报告缺失或字节已变化"
    return None


def _active_forward_observations() -> list[dict[str, Any]]:
    rows = []
    if not STRATEGIES_DIR.is_dir():
        return rows
    for path in STRATEGIES_DIR.glob("user-*.json"):
        strategy = _read_json(path)
        promotion = strategy.get("promotion") or {}
        if promotion.get("paper_observe") is not True:
            continue
        observation = _forward_observation(promotion.get("paper_observation_id"))
        if observation and observation.get("strategy_id") == strategy.get("strategy_id"):
            rows.append(observation)
    return rows


def _forward_observation_is_active(observation: dict[str, Any]) -> bool:
    strategy = _read_json(
        STRATEGIES_DIR / f"{observation.get('strategy_id')}.json"
    )
    promotion = strategy.get("promotion") or {}
    return bool(
        promotion.get("paper_observe") is True
        and promotion.get("paper_observation_id") == observation.get("observation_id")
    )


def _advance_forward_observation(
    observation: dict[str, Any],
    source: dict[str, Any],
    panel_cache: dict[str, pd.DataFrame],
) -> bool:
    directory = _forward_observation_dir(observation["observation_id"])
    state_path = directory / "state.json"
    state = _forward_read_json(
        state_path, observation_id=observation["observation_id"],
    )
    if not _forward_observation_is_active(observation):
        return False
    invalid_reason = _observation_invalid_reason(observation)
    if invalid_reason:
        _atomic_json(state_path, {
            **state,
            "updated_at": _now(),
            "invalidated": True,
            "invalidated_reason": invalid_reason,
        })
        return False
    if state.get("last_source_version") == source["source_version"]:
        return False

    previous_source_date = str(state.get("last_source_data_as_of") or "")
    data_as_of = source["data_as_of"]
    calendar_restatement = False
    if previous_source_date:
        try:
            calendar_restatement = (
                _forward_calendar_prefix_sha256(source, previous_source_date)
                != state.get("calendar_prefix_sha256")
            )
        except StrategyValidationError:
            calendar_restatement = True
    if previous_source_date and (data_as_of <= previous_source_date or calendar_restatement):
        _atomic_json(state_path, {
            **state,
            "updated_at": _now(),
            "source_restatement": {
                "observed_at": _now(),
                "previous_data_as_of": previous_source_date,
                "candidate": _forward_public_source(source),
                "reason": (
                    "calendar_history_restatement" if calendar_restatement
                    else "source_date_regressed" if data_as_of < previous_source_date
                    else "same_date_source_version_changed"
                ),
            },
            "last_error": "源版本非单调，已fail-closed；未推进任何事件",
        })
        return False

    changed = False
    settlement_events = [
        _forward_read_json(
            path,
            observation_id=observation["observation_id"],
            expected_events={"label_settled"},
        )
        for path in _forward_event_files(
            observation["observation_id"], "settlements"
        )
    ]
    settlements = {str(item["signal_date"]) for item in settlement_events}
    missed_events = [
        _forward_read_json(
            path,
            observation_id=observation["observation_id"],
            expected_events={"signal_missed", "settlement_missed"},
        ) for path in _forward_event_files(observation["observation_id"], "missed")
    ]
    signal_events = [
        _forward_read_json(
            path,
            observation_id=observation["observation_id"],
            expected_events={"signal_frozen"},
        ) for path in _forward_event_files(observation["observation_id"], "signals")
    ]
    _validate_forward_signal_bindings(observation, signal_events)
    _validate_forward_settlement_bindings(signal_events, settlement_events)
    missed_settlements = {
        str(item.get("signal_date")) for item in missed_events
        if item.get("event") == "settlement_missed"
    }
    for signal in signal_events:
        signal_date = str(signal["signal_date"])
        if signal_date in settlements or signal_date in missed_settlements:
            continue
        expected = _forward_expected_date(
            signal_date, int(signal.get("holding_days") or 0), source,
        )
        if expected == data_as_of:
            settlement = _settle_forward_signal(observation, signal, source)
            _forward_immutable_json(
                directory / "settlements" / f"{signal_date}.json", settlement,
            )
            changed = True
        elif expected and data_as_of > expected:
            missed = _forward_missed_event(
                observation,
                event="settlement_missed",
                signal_date=signal_date,
                expected_date=expected,
                source=source,
            )
            _forward_immutable_json(
                directory / "missed" / f"settlement_missed_{signal_date}.json",
                missed,
            )
            changed = True

    signal_dates = [str(item["signal_date"]) for item in signal_events]
    last_signal = max(signal_dates) if signal_dates else None
    dates = source["trading_dates"]
    horizon = int(observation["spec"]["holding_days"])
    first_eligible = observation["first_eligible_date"]
    schedule_cursor = str(
        state.get("schedule_cursor_date") or observation["schedule_anchor"]
    )
    recorded_signal_misses = {
        str(item.get("expected_date")) for item in missed_events
        if item.get("event") == "signal_missed"
    }
    due = False
    while schedule_cursor in dates:
        try:
            next_scheduled = dates[dates.index(schedule_cursor) + horizon]
        except (IndexError, ValueError):
            break
        if next_scheduled > data_as_of:
            break
        schedule_cursor = next_scheduled
        if next_scheduled < first_eligible:
            continue
        if next_scheduled in signal_dates:
            continue
        if next_scheduled < data_as_of:
            if next_scheduled not in recorded_signal_misses:
                missed_event = _forward_missed_event(
                    observation,
                    event="signal_missed",
                    expected_date=next_scheduled,
                    source=source,
                )
                _forward_immutable_json(
                    directory / "missed" / f"signal_missed_{next_scheduled}.json",
                    missed_event,
                )
                changed = True
            continue
        due = next_scheduled == data_as_of
        break
    if due:
        signal_path = directory / "signals" / f"{data_as_of}.json"
        if not signal_path.is_file():
            panel = panel_cache.get(source["source_version"])
            if panel is None:
                panel = _build_forward_signal_panel(source)
                panel_cache[source["source_version"]] = panel
            signal = _forward_signal_payload(observation, source, panel)
            _forward_immutable_json(signal_path, signal)
            changed = True
        last_signal = data_as_of

    missed = list(state.get("unobserved_source_dates") or [])
    if previous_source_date in dates and data_as_of in dates:
        skipped = dates[
            dates.index(previous_source_date) + 1:dates.index(data_as_of)
        ]
        missed.extend(day for day in skipped if day >= observation["first_eligible_date"])
    _atomic_json(state_path, {
        **state,
        "updated_at": _now(),
        "last_source_version": source["source_version"],
        "last_source_data_as_of": data_as_of,
        "calendar_prefix_sha256": _forward_calendar_prefix_sha256(source),
        "last_signal_date": last_signal,
        "schedule_cursor_date": schedule_cursor,
        "unobserved_source_dates": sorted(set(missed)),
        "source_restatement": None,
        "last_error": None,
    })
    return changed


def refresh_paper_forward() -> dict[str, Any]:
    """Advance immutable signal/settlement evidence once per source version."""
    runner_path = FORWARD_DIR / "runner_state.json"
    try:
        observations = _active_forward_observations()
    except Exception as exc:
        result = {
            "status": "degraded",
            "checked_at": _now(),
            "active_observations": None,
            "advanced": 0,
            "errors": [{"error": f"{type(exc).__name__}: {exc}"}],
        }
        _atomic_json(runner_path, result)
        return result
    if not observations:
        return {"status": "idle", "active_observations": 0, "advanced": 0}
    handle = _acquire_forward_lock()
    if handle is None:
        return {"status": "busy", "active_observations": len(observations), "advanced": 0}
    # Start/stop uses the same lock. Re-read after acquiring it so a queued OFF
    # cannot leave this refresh writing one more event for a stale observation.
    try:
        observations = _active_forward_observations()
    except Exception as exc:
        _release_forward_lock(handle)
        result = {
            "status": "degraded",
            "checked_at": _now(),
            "active_observations": None,
            "advanced": 0,
            "errors": [{"error": f"{type(exc).__name__}: {exc}"}],
        }
        _atomic_json(runner_path, result)
        return result
    if not observations:
        _release_forward_lock(handle)
        return {"status": "idle", "active_observations": 0, "advanced": 0}
    source_handle = None
    try:
        source_handle = _acquire_v4_read_lock()
        if source_handle is None:
            result = {
                "status": "waiting_for_incremental_publish",
                "checked_at": _now(),
                "active_observations": len(observations),
                "advanced": 0,
                "errors": [],
            }
            _atomic_json(runner_path, result)
            return result
        source = _forward_source_state()
        panel_cache: dict[str, pd.DataFrame] = {}
        advanced = 0
        errors = []
        for observation in observations:
            try:
                advanced += int(_advance_forward_observation(
                    observation, source, panel_cache,
                ))
            except Exception as exc:
                errors.append({
                    "observation_id": observation.get("observation_id"),
                    "error": f"{type(exc).__name__}: {exc}",
                })
        result = {
            "status": "degraded" if errors else "ok",
            "checked_at": _now(),
            "source": _forward_public_source(source),
            "active_observations": len(observations),
            "advanced": advanced,
            "errors": errors,
        }
        _atomic_json(runner_path, result)
        return result
    except Exception as exc:
        result = {
            "status": "waiting_for_complete_source",
            "checked_at": _now(),
            "active_observations": len(observations),
            "advanced": 0,
            "errors": [{"error": f"{type(exc).__name__}: {exc}"}],
        }
        _atomic_json(runner_path, result)
        return result
    finally:
        _release_v4_read_lock(source_handle)
        _release_forward_lock(handle)


def _latest_forward_observation(run_id: str) -> dict[str, Any]:
    matches = []
    if FORWARD_DIR.is_dir():
        for path in FORWARD_DIR.glob("obs-*/observation.json"):
            item = _forward_observation(path.parent.name)
            if item.get("origin_run_id") == run_id:
                matches.append(item)
    return max(matches, key=lambda item: str(item.get("observed_at") or "")) if matches else {}


def load_paper_forward_state(run_id: Any) -> dict[str, Any]:
    """Read-only aggregate for one immutable historical run's paper shadow."""
    safe_run = _safe_run_id(run_id)
    if safe_run is None:
        raise StrategyValidationError("回测运行ID无效")
    report = _read_json(REPORTS_DIR / f"{safe_run}.json")
    if not report:
        raise StrategyValidationError("未找到该回测报告")
    public_report = _with_current_epoch_status(report)
    observation = _latest_forward_observation(safe_run)
    if not observation:
        return {
            "run_id": safe_run,
            "strategy_id": report.get("strategy_id"),
            "status": "off",
            "status_text": "尚未开启纸面前向观察",
            "stage": {"code": "observation_off", "text": "观察未开启"},
            "updated_at": report.get("generated_at"),
            "data_as_of": None,
            "source_version": None,
            "version": None,
            "current_epoch_eligible": public_report["current_epoch_eligible"],
            "paper_only": True,
            "recommendation_eligible": False,
            "mode": "forward_shadow_label_continuation",
            "model": "forward_shadow_label_continuation",
            "promotion": False,
            "real_time": False,
            "orders": False,
            "automatic_trading": False,
            "broker_connected": False,
            "unlocked": False,
            "counts_toward_promotion": False,
            "no_backfill": True,
            "immutable_events": True,
            "counts": {
                "signals": 0, "pending": 0, "settled": 0,
                "signal_missed": 0, "settlement_missed": 0,
            },
            "points": [],
            "blockers": [{
                "code": "paper_observation_off",
                "text": "该回测尚未冻结纸面前向观察起点",
            }],
            "next_action": {
                "code": "enable_paper_observation",
                "text": "确认规格后开启纸面观察并等待下一次完整V4发布",
            },
            "refresh_after_seconds": FORWARD_REFRESH_AFTER_SECONDS,
        }

    directory = _forward_observation_dir(observation["observation_id"])
    state = _forward_read_json(
        directory / "state.json", observation_id=observation["observation_id"],
    )
    signals = [_forward_read_json(
        path,
        observation_id=observation["observation_id"],
        expected_events={"signal_frozen"},
    ) for path in _forward_event_files(observation["observation_id"], "signals")]
    settlements = [_forward_read_json(
        path,
        observation_id=observation["observation_id"],
        expected_events={"label_settled"},
    ) for path in _forward_event_files(observation["observation_id"], "settlements")]
    missed_events = [_forward_read_json(
        path,
        observation_id=observation["observation_id"],
        expected_events={"signal_missed", "settlement_missed"},
    ) for path in _forward_event_files(observation["observation_id"], "missed")]
    _validate_forward_signal_bindings(observation, signals)
    _validate_forward_settlement_bindings(signals, settlements)
    settled_dates = {str(item.get("signal_date")) for item in settlements}
    missed_settlement_dates = {
        str(item.get("signal_date")) for item in missed_events
        if item.get("event") == "settlement_missed"
    }
    pending = sum(
        str(item.get("signal_date")) not in settled_dates
        and str(item.get("signal_date")) not in missed_settlement_dates
        for item in signals
    )
    nav = 1.0
    peak = 1.0
    points = []
    for item in sorted(settlements, key=lambda row: str(row.get("signal_date") or "")):
        nav *= 1.0 + float(item.get("net_return") or 0.0)
        peak = max(peak, nav)
        drawdown = nav / peak - 1.0
        points.append({
            "date": item.get("signal_date"),
            "nav": round(nav, 8),
            "drawdown": round(drawdown, 8),
            "drawdown_pct": round(drawdown * 100.0, 6),
            "source_data_as_of": (
                item.get("settlement_source") or {}
            ).get("data_as_of"),
        })

    invalid_reason = _observation_invalid_reason(observation)
    strategy = _read_json(STRATEGIES_DIR / f"{observation['strategy_id']}.json")
    promotion = strategy.get("promotion") or {}
    runner = _read_json(FORWARD_DIR / "runner_state.json")
    runner_errors = [
        item for item in (runner.get("errors") or [])
        if not item.get("observation_id")
        or item.get("observation_id") == observation["observation_id"]
    ]
    active = bool(
        promotion.get("paper_observe") is True
        and promotion.get("paper_observation_id") == observation["observation_id"]
    )
    if invalid_reason:
        status, status_text = "invalidated", invalid_reason
        stage = {"code": "observation_invalidated", "text": "观察已失效"}
        blockers = [{"code": "identity_changed", "text": invalid_reason}]
        next_action = {
            "code": "run_new_backtest",
            "text": "按当前规格生成新回测，再重新开启纸面观察",
        }
    elif not active:
        status, status_text = "off", "纸面观察已停止；历史证据保留"
        stage = {"code": "observation_stopped", "text": "观察已停止"}
        blockers = [{"code": "paper_observation_off", "text": status_text}]
        next_action = {
            "code": "resume_with_new_observation",
            "text": "需要继续时重新冻结观察起点；不会补记停止期间信号",
        }
    elif state.get("source_restatement"):
        status, status_text = "waiting_for_new_panel", "检测到数据源回退或同日重述，已fail-closed"
        stage = {"code": "source_restatement", "text": "数据源重述阻断"}
        blockers = [{"code": "non_monotonic_source", "text": status_text}]
        next_action = {
            "code": "wait_for_later_complete_publication",
            "text": "等待日期严格更新的全量V4发布；本次不推进事件",
        }
    elif runner_errors:
        status, status_text = "waiting_for_new_panel", "前向runner最近一次推进失败，未写入不完整事件"
        stage = {"code": "runner_error", "text": "runner待重试"}
        blockers = [{
            "code": "forward_runner_error",
            "text": str(runner_errors[0].get("error") or status_text),
        }]
        next_action = {
            "code": "wait_for_automatic_retry",
            "text": "保持观察开启；后台60秒轮询会在同一源版本重试",
        }
    elif not signals:
        status, status_text = "waiting_for_new_panel", "等待观察起点后的下一次完整V4发布"
        stage = {"code": "waiting_for_signal", "text": "等待新信号"}
        blockers = [{"code": "no_post_observation_source", "text": status_text}]
        next_action = {
            "code": "wait_for_complete_v4_publication",
            "text": "保持观察开启；完整发布后后台会冻结当期信号",
        }
    elif pending:
        status, status_text = "waiting_for_label", "已有冻结信号，等待持有期标签成熟"
        stage = {"code": "signal_frozen", "text": "信号已冻结"}
        blockers = [{"code": "forward_label_pending", "text": status_text}]
        next_action = {
            "code": "wait_for_label_maturity",
            "text": "等待未来完整V4发布，仅结算已冻结信号",
        }
    else:
        status, status_text = "active", "纸面标签续接正在按不可变证据推进"
        stage = {"code": "settlement_active", "text": "前向结算中"}
        blockers = []
        next_action = {
            "code": "continue_observation",
            "text": "继续等待新完整发布；结果仍不计入晋升",
        }
    event_hashes = [
        _sha256(path) for kind in ("signals", "settlements", "missed")
        for path in _forward_event_files(observation["observation_id"], kind)
    ]
    return {
        "run_id": safe_run,
        "strategy_id": observation["strategy_id"],
        "observation_id": observation["observation_id"],
        "status": status,
        "status_text": status_text,
        "stage": stage,
        "updated_at": state.get("updated_at") or observation.get("observed_at"),
        "data_as_of": state.get("last_source_data_as_of"),
        "source_version": state.get("last_source_version"),
        "version": _payload_sha256({
            "observation": observation["observation_id"],
            "events": event_hashes,
        }),
        "first_eligible_date": observation.get("first_eligible_date"),
        "result_spec_sha256": observation.get("result_spec_sha256"),
        "forward_logic_sha256": observation.get("forward_logic_sha256"),
        "current_epoch_eligible": public_report["current_epoch_eligible"],
        "paper_only": True,
        "recommendation_eligible": False,
        "mode": "forward_shadow_label_continuation",
        "promotion": False,
        "real_time": False,
        "orders": False,
        "automatic_trading": False,
        "broker_connected": False,
        "unlocked": False,
        "counts_toward_promotion": False,
        "no_backfill": True,
        "immutable_events": True,
        "model": "forward_shadow_label_continuation",
        "counts": {
            "signals": len(signals), "pending": pending, "settled": len(settlements),
            "signal_missed": sum(
                item.get("event") == "signal_missed" for item in missed_events
            ),
            "settlement_missed": sum(
                item.get("event") == "settlement_missed" for item in missed_events
            ),
        },
        "points": points,
        "blockers": blockers,
        "next_action": next_action,
        "runner": {
            "status": runner.get("status"),
            "checked_at": runner.get("checked_at"),
            "errors": runner_errors,
        },
        "refresh_after_seconds": FORWARD_REFRESH_AFTER_SECONDS,
    }


def run_backtest(payload: dict[str, Any]) -> dict[str, Any]:
    if not RUN_LOCK.acquire(blocking=False):
        raise BacktestBusyError("已有用户策略回测正在运行")
    try:
        validated = validate_spec(payload)
        local_requirements = (
            ("compose_method", "rank_weighted_v1", "因子合成方法"),
            ("entry_rule", "none", "买点规则"),
            ("exposure_rule", "none", "暴露规则"),
        )
        for key, expected, label in local_requirements:
            if validated[key] != expected:
                raise StrategyValidationError(
                    f"共享Panel快速诊断未实现{label} {validated[key]}；"
                    "请使用统一引擎，不得静默按默认语义运行。"
                )
        if int(validated["rebal_buffer"]) != 0:
            raise StrategyValidationError(
                "共享Panel快速诊断未实现换仓缓冲；请设 rebal_buffer=0"
                "，或使用统一引擎。"
            )
        if float(validated["account_capital"]) != 200_000.0:
            raise StrategyValidationError(
                "共享Panel快速诊断未实现100股整手/现金约束；"
                "account_capital仅支持固定200000元费用诊断名义值，"
                "其他资金量请使用统一引擎。"
            )
        saved = save_strategy(validated)
        spec = saved["spec"]
        # M4.1 新 A股因子族仅在 factor_system 统一引擎可重建（V4 面板无这些列）
        new_factors = sorted(
            name for name, weight in spec["factors"].items()
            if weight and name not in _V4_FACTOR_COLUMNS
        )
        if new_factors:
            raise StrategyValidationError(
                "新因子族/我的因子（" + ", ".join(new_factors) + "）由因子系统按配方重建"
                "（当前共享审计面板），仅支持统一引擎评估——请点「运行科学回测」"
                "或「统一引擎专业诊断」，自动切换后即用您的因子真实回测。"
            )
        strategy_panel, source = _strategy_panel_snapshot()
        panel = _load_panel(spec, strategy_panel)
        all_periods, selected = _run_periods(spec, panel)
        run_id = "run-" + datetime.now(SHANGHAI).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
        metrics: dict[str, Any] = {}
        split_frames: dict[str, pd.DataFrame] = {}
        split_boundary_purge: dict[str, dict[str, Any]] = {}
        retained_signal_dates: set[str] = set()
        active = [name for name, weight in spec["factors"].items() if weight]
        portfolio_rows = selected if not selected.empty else pd.DataFrame({
            "trade_date": pd.Series(dtype="str"),
            "entry_filled": pd.Series(dtype=bool),
            "gross_return": pd.Series(dtype=float),
            "label_exit_date": pd.Series(dtype="str"),
            "label_scheduled_exit_date": pd.Series(dtype="str"),
        })
        seed_base = int(hashlib.sha256(
            json.dumps(spec, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:8], 16)
        for offset, (name, (start, end)) in enumerate(SPLITS.items()):
            candidate_dates = all_periods.loc[
                all_periods["signal_date"].between(
                    start, end, inclusive="both",
                ),
                "signal_date",
            ].astype(str).tolist()
            safe_dates, purge = _split_safe_signal_dates(
                portfolio_rows, start, end,
                candidate_dates=candidate_dates,
                schedule_frame=panel,
            )
            ic_overrides, excluded_ic_rows = _split_ic_overrides(
                panel, safe_dates, end, active,
            )
            purge["excluded_cross_boundary_ic_rows"] = excluded_ic_rows
            retained_signal_dates.update(safe_dates)
            split_boundary_purge[name] = purge
            portion = all_periods[
                all_periods["signal_date"].astype(str).isin(safe_dates)
            ].copy()
            for row_index, signal_date in portion["signal_date"].items():
                values = ic_overrides.get(str(signal_date)) or {}
                for factor in active:
                    portion.at[row_index, f"ic_{factor}"] = values.get(factor)
            split_frames[name] = portion.reset_index(drop=True)
            metrics[name] = _summarize(
                split_frames[name], spec["holding_days"], seed_base + offset,
            )
        periods = pd.concat(
            [split_frames[name] for name in SPLITS], ignore_index=True,
        )
        if not selected.empty:
            selected = selected[
                selected["trade_date"].astype(str).isin(retained_signal_dates)
            ].reset_index(drop=True)
        gate_conditions = {
            name: _split_gates(metrics[name]) for name in ("validation", "test")
        }
        test_frame = split_frames["test"]
        test_folds = _test_folds(test_frame)
        fold_pass = [row["passed"] for row in test_folds]
        fold_gate = bool(
            len(fold_pass) == 5
            and qs is not None
            and qs.fold_pass_rate(fold_pass)["gate_ok"]
        )
        historical_passed = bool(
            all(all(values.values()) for values in gate_conditions.values())
            and fold_gate
            and spec["one_way_cost_bps"] >= 10
        )
        failed_gates = [
            f"{split}.{gate}"
            for split, values in gate_conditions.items()
            for gate, passed in values.items() if not passed
        ]
        if not fold_gate:
            n_fold_pass = sum(1 for row in test_folds if row["passed"])
            failed_gates.append(
                f"test.five_time_folds_pass_rate_ge_60pct({n_fold_pass}/5通过)"
            )
        if spec["one_way_cost_bps"] < 10:
            failed_gates.append("cost.one_way_at_least_10bp_for_validation")

        _verify_source_snapshot(source)
        periods_path = EVIDENCE_DIR / f"{run_id}_periods.csv"
        selected_path = EVIDENCE_DIR / f"{run_id}_selected_targets.csv"
        ic_columns = ["signal_date", "year"] + [f"ic_{name}" for name in active]
        factor_ic = periods.reindex(columns=ic_columns)
        factor_ic_path = EVIDENCE_DIR / f"{run_id}_factor_ic.csv"
        _atomic_csv(periods_path, periods)
        _atomic_csv(selected_path, selected)
        _atomic_csv(factor_ic_path, factor_ic)
        spec_path = STRATEGIES_DIR / f"{saved['strategy_id']}.json"
        diagnostics = {
            "equity_curve": {
                name: _equity_curve(frame) for name, frame in split_frames.items()
            },
            "cost_ladder": {
                name: _cost_ladder(frame, spec["holding_days"])
                for name, frame in split_frames.items()
            },
            "factor_ic": {
                name: _factor_diagnostics(frame, active)
                for name, frame in split_frames.items()
            },
            "generalization": {
                "development_minus_validation_annualized_pp": round(
                    metrics["development"]["annualized_return_pct"]
                    - metrics["validation"]["annualized_return_pct"], 6,
                ),
                "validation_minus_test_annualized_pp": round(
                    metrics["validation"]["annualized_return_pct"]
                    - metrics["test"]["annualized_return_pct"], 6,
                ),
            },
            "capacity_method": (
                "等权组合中每只股票的目标金额不超过其20日平均成交额的1%；"
                "组合容量取所有目标股约束的最小值。仅为日线研究估算。"
            ),
        }
        # P3-1 统一 QC：同花顺口径成本基线 + T+1 开盘成交口径（诚实披露）
        capital = float(spec.get("account_capital", 200_000.0))
        top_n = max(int(spec.get("top_n", 30)), 1)
        ths_round_trip = (
            qc.cost_rate_for_portfolio(capital, top_n)
            if qc is not None else None
        )
        quantqc_cost_baseline = {
            "capital": capital,
            "positions": top_n,
            "round_trip_rate": ths_round_trip,
            "round_trip_bp": round(ths_round_trip * 10_000, 1) if ths_round_trip else None,
            "one_way_bp_equivalent": round(ths_round_trip * 10_000 / 2, 1) if ths_round_trip else None,
            "note": (
                "quantqc 同花顺口径：佣金万2.5双边最低¥5/笔 + 印花税仅卖出0.05% + "
                "过户费0.001%双边 + 滑点0.1%×2，按组合等权往返估算。"
            ),
            "execution": (
                "信号 T 收盘定 → T+1 开盘成交（label_return 为可执行 next-open 标签，"
                "entry=T+1 open，计划 exit=T+h close；停牌顺延至首个可交易收盘，"
                "计划窗口内显式退市按零回收）。"
            ),
        }
        report = {
            "version": BACKTEST_REPORT_VERSION,
            "report_schema": BACKTEST_REPORT_SCHEMA,
            "research_epoch": factor_config.RESEARCH_EPOCH,
            "generated_at": _now(),
            "status": "fractional_label_diagnostic_only",
            "run_id": run_id,
            "strategy_id": saved["strategy_id"],
            "spec": spec,
            "safe_dsl": True,
            "arbitrary_code_executed": False,
            "source": source,
            "splits": SPLITS,
            "split_boundary_purge": split_boundary_purge,
            "quantqc_cost_baseline": quantqc_cost_baseline,
            "local_execution_scope": {
                "account_capital_role": "fixed_200000_cny_cost_diagnostic_nominal",
                "board_lot_and_cash_constraints_executed": False,
                "one_way_cost_bps_role": "configured_slippage_only",
            },
            "metrics": metrics,
            "diagnostics": diagnostics,
            "gate_conditions": gate_conditions,
            "test_folds": test_folds,
            "test_five_fold_pass_rate": (
                round(sum(1 for r in test_folds if r["passed"]) / max(len(test_folds), 1), 3)
                if test_folds else None
            ),
            "test_all_three_folds_total_and_excess_positive": fold_gate,
            "failed_gates": failed_gates,
            "fractional_label_diagnostic_gate_passed": historical_passed,
            "historical_validation_gate_passed": False,
            "gate_disqualified_reason": (
                "共享Panel快速路径未执行100股整手/现金约束，仅为fractional label诊断"
            ),
            "execution_model": "fractional_label_continuation_diagnostic",
            "account_capital_applied": False,
            "round_lot_simulated": False,
            "current_research_state": {
                "signal_date": str(periods["signal_date"].max()),
                "risk_on": bool(periods.iloc[-1]["risk_on"]),
                "eligible_stocks": int(periods.iloc[-1]["eligible_stocks"]),
                "selected_stocks": int(periods.iloc[-1]["selected_stocks"]),
                "buy_alert": False,
                "reason": "用户自定义回测只用于研究验证，不自动进入买入提醒。",
            },
            "spec_path": str(spec_path.relative_to(ROOT)),
            "spec_sha256": _sha256(spec_path),
            "periods_path": str(periods_path.relative_to(ROOT)),
            "periods_sha256": _sha256(periods_path),
            "selected_path": str(selected_path.relative_to(ROOT)),
            "selected_sha256": _sha256(selected_path),
            "factor_ic_path": str(factor_ic_path.relative_to(ROOT)),
            "factor_ic_sha256": _sha256(factor_ic_path),
            "source_evidence_hashes": dict(source["evidence_hashes"]),
            "promotion": saved.get("promotion") or _promotion_block(),
            "recommendation_eligible": _promotion_eligible(
                saved.get("promotion") or _promotion_block()
            ),
            "capital_allocation": False,
            "orders": False,
            "automatic_trading": False,
            "safety": {
                "external_model_used": False,
                "network_used": False,
                "arbitrary_python_used": False,
                "paid_api_used": False,
            },
            "next_action": "该结果仅作数值诊断；门禁评估请使用统一引擎。",
        }
        _verify_source_snapshot(source)
        report["assistant"] = _local_assistant(report)
        report["integrity"] = _report_integrity(report)
        report_path = REPORTS_DIR / f"{run_id}.json"
        _atomic_json(report_path, report)
        try:
            _write_report_index(report_path, report)
        except OSError:
            pass
        return _with_current_epoch_status(report)
    finally:
        RUN_LOCK.release()
