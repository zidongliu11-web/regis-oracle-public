#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
signal_system.py — 实时信号系统（任务书 Step 3）

分层实时数据 + 因子系统 production 因子实时计算 + 阈值信号 + 审计日志 + 降级规则。
**仅展示审计，不自动下单。**

数据分层（诚实口径）：
  - 实时行情层（每分钟）：腾讯批量公开行情（含交易所时点）→站点统一数据层。
    AkShare 新浪全市场快照无逐票交易所时点，仅作降级展示，不混入新信号截面。
  - 因子层（目录/数据变化即失效）：只消费当前 research epoch 的 production catalog；
    每个配方和冻结前向证据先复核 hash，再用 factor_system 原 evaluator 在完整 Panel
    全市场复算，最后一步才切全市场多维预筛 Top300；无随机抽样或轮换。

信号合成（固定阈值，非 ML）：
  total = 0.6 × factor_z（当前 production catalog 全市场方向化 z）+ 0.4 × 实时动量 z（今日涨跌幅）
  WATCH: total > 0.5（或 factor_z > 0.8 的"结构性关注"）且市场开放、行情健康
  其余 NO_SIGNAL

降级规则：
  腾讯批量公开行情 → /api/data/quote 逐票（统一数据层）→ AkShare 无时点降级展示。
  只有腾讯同日且不超过15分钟的完整截面可产生新信号。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import threading
import time as _time
from datetime import datetime, time as _dtime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from factor_system import production as factor_production

from .full_market_runtime import audited_realtime_universe
from .trading_calendar import trading_day_status

_PROJECT = Path(__file__).resolve().parent.parent
_SIGNALS_PATH = _PROJECT / "research" / "mom_live_signals.json"
_AUDIT_PATH = _PROJECT / "research" / "mom_live_signals_audit.jsonl"
_WATCHLIST_SRC = _PROJECT / "research" / "full_market_top300.json"
_LIVE_STRATEGY_VALIDATION_PATH = (
    _PROJECT / "reports" / "live_factor_signal_validation.json"
)
LIVE_STRATEGY_ID = "live_factor_z_60_momentum_z_40_v1"

_SH_TZ = ZoneInfo("Asia/Shanghai")

WATCHLIST_SIZE = int(os.environ.get("SIGNAL_WATCHLIST", 300))
FALLBACK_QUOTE_LIMIT = 30
REFRESH_SECONDS = int(os.environ.get("SIGNAL_INTERVAL", 60))
FACTOR_CACHE_HOURS = 6.0          # 因子层缓存（新收盘日/半日内复用，减少逐票拉取）
W_FACTOR, W_MOMENTUM = 0.6, 0.4   # 固定权重（先验，非回测调参）
WATCH_SCORE = 0.5
LIMIT_PROXIMITY_PCT = 0.25
QUOTE_CACHE_SECONDS = 180.0
_QUOTE_CACHE: dict[str, tuple[float, dict]] = {}
_QUOTE_CACHE_LOCK = threading.Lock()

# ── 工具 ──────────────────────────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now(_SH_TZ)


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _ordered_codes_sha256(codes: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(codes, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _market_status(now: Optional[datetime] = None) -> dict:
    """Audited calendar + both official continuous-auction observation sessions."""
    now = now or _now()
    calendar = trading_day_status(now)
    if not calendar["is_trading_day"]:
        return {"open": False, "label": calendar["reason"], "calendar": calendar}
    t = now.time()
    if _dtime(9, 30) <= t <= _dtime(11, 30):
        return {"open": True, "label": "上午研究观察窗口", "calendar": calendar}
    if _dtime(13, 0) <= t <= _dtime(15, 0):
        return {"open": True, "label": "下午研究观察窗口", "calendar": calendar}
    return {"open": False, "label": "非连续交易时段；观察静默", "calendar": calendar}


def _akshare_to_code(ak_code: str) -> str:
    """'sh600519' → '600519.SH'。"""
    ak_code = (ak_code or "").strip()
    prefix, num = ak_code[:2], ak_code[2:]
    if prefix == "bj":
        return f"{num}.BJ"
    return f"{num}.{'SH' if prefix == 'sh' else 'SZ'}"


# ── 1. Watchlist ──────────────────────────────────────────────────────────
def load_watchlist(n: int = WATCHLIST_SIZE) -> List[str]:
    """全市场确定性多维预筛 Top-N 作为实时监控池。"""
    if not _WATCHLIST_SRC.exists():
        return []
    try:
        d = json.loads(_WATCHLIST_SRC.read_text(encoding="utf-8"))
        top = d.get("top", [])
        return [t["code"] for t in top[:n] if t.get("code")]
    except (OSError, json.JSONDecodeError):
        return []


def load_realtime_universe() -> dict[str, object]:
    """Load the full eligible snapshot; never relabel a legacy Top-N as full."""
    return audited_realtime_universe(_PROJECT, _WATCHLIST_SRC)


def _snapshot_selection_rows(universe_identity: dict) -> tuple[list[dict], dict]:
    """Return a verified single-as-of eligible ranking and its audit metadata."""
    try:
        payload = json.loads(_WATCHLIST_SRC.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], {"status": "unavailable_snapshot"}
    rows = payload.get("eligible") if isinstance(payload.get("eligible"), list) else []
    data_asof = str(payload.get("data_asof") or "")
    codes = [str(row.get("code") or "") for row in rows if isinstance(row, dict)]
    complete = bool(
        universe_identity.get("eligible_snapshot_complete") is True
        and data_asof
        and data_asof == str(universe_identity.get("data_asof") or "")
        and rows
        and len(codes) == len(set(codes))
        and all(str(row.get("data_asof") or "") == data_asof for row in rows)
    )
    if not complete:
        return [], {"status": "unavailable_unverified_snapshot"}
    scan = payload.get("scan_audit") if isinstance(payload.get("scan_audit"), dict) else {}
    try:
        snapshot_path = str(_WATCHLIST_SRC.resolve().relative_to(_PROJECT.resolve()))
    except ValueError:
        snapshot_path = str(_WATCHLIST_SRC.resolve())
    return rows, {
        "status": "verified",
        "eligible_snapshot_path": snapshot_path,
        "eligible_snapshot_sha256": hashlib.sha256(
            _WATCHLIST_SRC.read_bytes()
        ).hexdigest(),
        "data_asof": data_asof,
        "eligible_count": len(rows),
        "ordered_eligible_codes_sha256": _ordered_codes_sha256(codes),
        "professional_preview_complete": scan.get("professional_preview_complete") is True,
        "professional_preview_available_count": int(
            scan.get("professional_preview_available_count") or 0
        ),
        "professional_preview_scope_count": int(
            scan.get("professional_preview_scope_count") or len(rows)
        ),
        "snapshot_selection_method": scan.get("selection_method"),
    }


def _select_monitor_signals(
    signals: list[dict], universe_identity: dict, limit: int,
) -> tuple[list[dict], dict]:
    """Select the retained monitor pool without letting intraday momentum admit it.

    Verified production factor signals keep first priority.  The remaining
    capacity follows the complete eligible snapshot order: professional score
    only when coverage is complete, otherwise the uniform market-evidence
    fallback already frozen in that snapshot.  Realtime momentum remains an
    execution observation and never decides who receives a professional score.
    """
    limit = max(0, int(limit))
    if not signals or limit == 0:
        return [], {"mode": "empty", "selected_count": 0}
    by_code = {str(row.get("code") or ""): row for row in signals}
    production = sorted(
        (
            row for row in signals
            if row.get("action") in {"BUY", "WATCH"}
            and _finite_number(row.get("factor_z")) is not None
        ),
        key=lambda row: (
            -float(_finite_number(row.get("score")) or 0.0),
            str(row.get("code") or ""),
        ),
    )
    rows, audit = _snapshot_selection_rows(universe_identity)
    if rows:
        ordered = [
            by_code[code] for code in (str(row.get("code") or "") for row in rows)
            if code in by_code
        ]
        fallback_mode = (
            "full_professional_preview_order"
            if audit.get("professional_preview_complete") is True
            else "uniform_market_evidence_order_incomplete_professional_coverage"
        )
    else:
        ordered = sorted(signals, key=lambda row: str(row.get("code") or ""))
        fallback_mode = "code_order_unverified_snapshot"
    selected, seen = [], set()
    for row in [*production, *ordered]:
        code = str(row.get("code") or "")
        if not code or code in seen:
            continue
        selected.append(row)
        seen.add(code)
        if len(selected) >= limit:
            break
    return selected, {
        **audit,
        "mode": "production_signal_priority_then_" + fallback_mode,
        "production_signal_priority_count": min(len(production), len(selected)),
        "selected_count": len(selected),
        "random_sampling": False,
        "random_rotation": False,
        "intraday_momentum_decides_admission": False,
        "ordered_selected_codes": [str(row.get("code") or "") for row in selected],
        "ordered_selected_codes_sha256": _ordered_codes_sha256([
            str(row.get("code") or "") for row in selected
        ]),
        "returns_or_labels_read": False,
    }


def watchlist_scope(
    selected_count: int, *, universe_identity: dict | None = None,
    runtime_counts: dict | None = None,
) -> dict:
    """披露实时池如何从审计市场得到，避免 Top-N 被误解为随机抽样。"""
    try:
        source = json.loads(_WATCHLIST_SRC.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        source = {}
    try:
        source_path = str(_WATCHLIST_SRC.relative_to(_PROJECT))
    except ValueError:
        source_path = str(_WATCHLIST_SRC)
    universe = source.get("universe_audit") or {}
    identity = universe_identity or {}
    scope = {
        "source": source_path,
        "data_asof": source.get("data_asof"),
        "ranking_method": source.get("ranking_method"),
        "full_market_universe_count": source.get("universe_count"),
        "full_market_shortlist_count": (
            source.get("top_count") or len(source.get("top") or [])
        ),
        "universe_coverage_label": universe.get("coverage_label"),
        "full_a_share_coverage": universe.get("full_a_share_coverage"),
        "included_segments": universe.get("included_segments") or [],
        "excluded_segments": universe.get("excluded_segments") or [],
        "excluded_special_securities": (
            universe.get("excluded_special_securities") or {}
        ),
        "realtime_monitored_count": selected_count,
        "selection": "full_market_deterministic_multidimensional_top_prefix",
        "random_sampling": False,
        "random_rotation": False,
    }
    if identity:
        scope.update({
            "realtime_universe_status": identity.get("status"),
            "realtime_universe_reason": identity.get("reason"),
            "eligible_snapshot_complete": (
                identity.get("eligible_snapshot_complete") is True
            ),
            "universe_total": identity.get("universe_total"),
            "full_a_share_coverage": identity.get("full_a_share_coverage") is True,
            "included_segments": identity.get("included_segments") or [],
            "excluded_segments": identity.get("excluded_segments") or [],
            "excluded_special_securities": (
                identity.get("excluded_special_securities") or {}
            ),
            "selection": "full_eligible_quote_cross_section_then_deterministic_top300",
        })
    if runtime_counts:
        scope.update(runtime_counts)
    return scope


# ── 2. 实时行情层（腾讯时点主源 / 统一数据层与 AkShare 降级） ─────────────
def _akshare_spot_snapshot() -> tuple[Optional[pd.DataFrame], str, datetime]:
    """akshare 新浪全市场快照 → (df, source, fetched_at)。失败返回 (None, '', ts)。"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot()
        if df is None or len(df) == 0:
            return None, "", _now()
        return df, "akshare:stock_zh_a_spot(新浪全市场)", _now()
    except Exception:
        return None, "", _now()


def _quotes_from_snapshot(df: pd.DataFrame) -> dict:
    """从 akshare 快照取 watchlist 代码的实时报价。"""
    out = {}
    df = df.rename(columns={"代码": "code", "名称": "name", "最新价": "price",
                            "涨跌幅": "pct_chg", "成交额": "amount", "换手率": "turnover",
                            "今开": "open", "昨收": "prev_close"})
    for _, r in df.iterrows():
        code = _akshare_to_code(str(r.get("code", "")))
        price = _finite_number(r.get("price"))
        if code and price is not None and price > 0:
            out[code] = {
                "price": price,
                "pct_chg": _finite_number(r.get("pct_chg")),
                "amount": _finite_number(r.get("amount")),
                "turnover": _finite_number(r.get("turnover")),
                "name": str(r.get("name", "") or ""),
            }
    return out


def _quotes_from_api(watchlist: List[str]) -> tuple[dict, dict]:
    """降级到统一行情层，并保留上游交易所时点而非本次抓取时点。"""
    import urllib.request
    out, sources, as_of_values = {}, set(), []
    base = os.environ.get("BT_DATA_API", "http://127.0.0.1:5200")
    # 批量腾讯源失败后，逐票 API 只保留前 30 只降级展示。
    # 降级行情不参与新信号，无需串行请求 300 只阻塞刷新器。
    attempted = watchlist[:FALLBACK_QUOTE_LIMIT]
    for code in attempted:
        url = f"{base}/api/data/quote?code={code[:6]}"
        try:
            with urllib.request.urlopen(url, timeout=8) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            q = (d.get("data") or {}).get("quote") or {}
            upstream_meta = d.get("meta") or {}
            raw_as_of = q.get("time") or upstream_meta.get("as_of")
            as_of = datetime.fromisoformat(str(raw_as_of)) if raw_as_of else None
            if as_of is not None and as_of.tzinfo is None:
                as_of = as_of.replace(tzinfo=_SH_TZ)
            elif as_of is not None:
                as_of = as_of.astimezone(_SH_TZ)
            price = _finite_number(q.get("price"))
            if price is not None and price > 0 and as_of is not None:
                out[code] = {
                    "price": price,
                    "pct_chg": _finite_number(q.get("change_pct")),
                    "amount": _finite_number(q.get("amount")),
                    "turnover": None,
                    "name": q.get("name", ""),
                    "exchange_as_of": as_of.isoformat(timespec="seconds"),
                }
                as_of_values.append(as_of)
                sources.add(str(upstream_meta.get("source") or q.get("provider_source") or "统一数据层"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    latest = max(as_of_values).isoformat(timespec="seconds") if as_of_values else None
    return out, {
        "source": " / ".join(sorted(sources)) if sources else "api/data/quote(全部失败)",
        "as_of": latest,
        "fetched_at": _now().isoformat(timespec="seconds"),
        "health_status": "delayed" if out else "unavailable",
        "degraded": True,
        "coverage_returned": len(out),
        "coverage_expected": len(watchlist),
        "coverage_attempted": len(attempted),
        "coverage_ratio": round(len(out) / len(watchlist) * 100, 2) if watchlist else 0.0,
        "decision_coverage_returned": 0,
        "fallback_scope": "top_prefix_display_only_not_signal_eligible",
        "freshness_rule": "保留上游交易所时间；回退行情不标记为实时",
    }


def _quotes_from_tencent(watchlist: List[str]) -> tuple[dict, dict]:
    """复用站内腾讯批量行情，只接收15分钟内的当日交易所时间。"""
    from .market_intelligence import _tencent_market_quotes

    rows, source = _tencent_market_quotes([code[:6] for code in watchlist])
    now = _now()
    out = {}
    for row in rows:
        as_of = row.get("as_of")
        if as_of is None:
            continue
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=_SH_TZ)
        else:
            as_of = as_of.astimezone(_SH_TZ)
        raw_age = (now - as_of).total_seconds()
        if raw_age < -60.0:
            continue
        age = max(0.0, raw_age)
        price = _finite_number(row.get("price"))
        if as_of.date() != now.date() or age > 15 * 60 or price is None or price <= 0:
            continue
        code = str(row.get("code") or "")[:6]
        out[code] = {
            "price": price,
            "pct_chg": _finite_number(row.get("change_pct")),
            "amount": (
                amount_yi * 100_000_000
                if (amount_yi := _finite_number(row.get("amount_yi"))) is not None
                else None
            ),
            "turnover": _finite_number(row.get("turnover_pct")),
            "name": str(row.get("name") or code),
            "exchange_as_of": as_of.isoformat(timespec="seconds"),
        }
    required = max(1, min(len(watchlist), max(5, math.ceil(len(watchlist) * 0.8))))
    source_as_of = source.get("as_of") if isinstance(source, dict) else None
    latest = max(
        (row["exchange_as_of"] for row in out.values()),
        default=source_as_of,
    )
    return out, {
        "source": "腾讯财经公开行情（自选池批量）",
        "as_of": latest,
        "fetched_at": now.isoformat(timespec="seconds"),
        "health_status": "live" if len(out) >= required else "unavailable",
        "degraded": False,
        "coverage_returned": len(out),
        "coverage_expected": len(watchlist),
        "coverage_attempted": len(watchlist),
        "coverage_ratio": round(len(out) / len(watchlist) * 100, 2) if watchlist else 0.0,
        "coverage_status": (
            "complete" if watchlist and len(out) == len(watchlist)
            else "partial" if out else "unavailable"
        ),
        "full_quote_coverage": bool(watchlist and len(out) == len(watchlist)),
        "fresh_coverage_returned": len(out),
        "decision_coverage_returned": len(out) if len(out) >= required else 0,
        "freshness_rule": "交易所时间同日且不超过15分钟",
    }


def _cache_quotes(quotes: dict) -> None:
    captured = _time.monotonic()
    with _QUOTE_CACHE_LOCK:
        for code, quote in quotes.items():
            if code != "_meta" and isinstance(quote, dict):
                _QUOTE_CACHE[code] = (captured, dict(quote))


def _cached_quotes(watchlist: List[str]) -> dict:
    now = _time.monotonic()
    with _QUOTE_CACHE_LOCK:
        expired = [
            code for code, (captured, _) in _QUOTE_CACHE.items()
            if now - captured > QUOTE_CACHE_SECONDS
        ]
        for code in expired:
            _QUOTE_CACHE.pop(code, None)
        return {
            code: dict(_QUOTE_CACHE[code][1])
            for code in watchlist if code in _QUOTE_CACHE
        }


def fetch_realtime(watchlist: List[str]) -> dict:
    """取实时行情：优先使用带交易所时点的腾讯批量源。"""
    quotes, tencent_meta = _quotes_from_tencent(watchlist)
    _cache_quotes(quotes)
    if tencent_meta["health_status"] == "live":
        quotes["_meta"] = tencent_meta
        return quotes
    cached = _cached_quotes(watchlist)
    cached.update(quotes)
    if cached:
        tencent_meta.update({
            "health_status": "partial" if quotes else "cached",
            "degraded": True,
            "coverage_returned": len(cached),
            "cache_returned": len(cached) - len(quotes),
            "coverage_ratio": round(len(cached) / len(watchlist) * 100, 2) if watchlist else 0.0,
            "coverage_status": "partial",
            "full_quote_coverage": False,
            "decision_coverage_returned": 0,
            "cache_scope": "display_only_not_signal_eligible",
        })
        cached["_meta"] = tencent_meta
        return cached
    # 统一数据层只作降级；保留上游时间，不参与新信号。
    quotes, fallback_meta = _quotes_from_api(watchlist)
    if quotes:
        quotes["_meta"] = fallback_meta
        return quotes

    # AkShare 全市场快照不提供可核验的逐票交易所时点，仅作展示降级。
    df, src, fetched_at = _akshare_spot_snapshot()
    if df is not None:
        quotes = _quotes_from_snapshot(df)
        quotes = {c: q for c, q in quotes.items() if c in set(watchlist)}
        if quotes:
            quotes["_meta"] = {
                "source": src,
                "as_of": None,
                "fetched_at": fetched_at.isoformat(timespec="seconds"),
                "health_status": "unverified_time",
                "degraded": True,
                "coverage_returned": len(quotes),
                "coverage_expected": len(watchlist),
                "coverage_attempted": len(watchlist),
                "coverage_ratio": round(len(quotes) / len(watchlist) * 100, 2) if watchlist else 0.0,
                "decision_coverage_returned": 0,
                "freshness_rule": "上游未提供交易所时点，不得参与新信号",
            }
            return quotes
    quotes["_meta"] = fallback_meta
    return quotes


# ── 3. 因子层（动态 production catalog，全市场先算、自选池后切） ───────
def _production_factor_metadata() -> dict:
    """只暴露完整复核通过的当前 epoch production catalog。"""
    try:
        catalog = factor_production.verified_catalog()
        catalog.pop("recipes", None)
        return catalog
    except Exception as exc:
        return {
            "complete": False,
            "research_epoch": None,
            "catalog_version": None,
            "factors": {},
            "error": f"{type(exc).__name__}: {exc}",
        }


def _live_strategy_validation_status(catalog_version: str | None = None) -> dict:
    """Read the exact rule's costed holdout result; failed rules stay disabled."""
    try:
        raw = _LIVE_STRATEGY_VALIDATION_PATH.read_bytes()
        report = json.loads(raw.decode("utf-8"))
    except (OSError, json.JSONDecodeError):
        raw, report = b"", {}
    report_catalog = report.get("catalog_version")
    catalog_matches = bool(catalog_version and report_catalog == catalog_version)
    passed = bool(
        report.get("strategy_id") == LIVE_STRATEGY_ID
        and catalog_matches
        and report.get("cost_inclusive_oos_passed") is True
        and report.get("independent_reproduction_passed") is True
    )
    verdict = str(report.get("verdict") or "")
    failed = bool(report and verdict in {"REJECTED", "FAILED", "NO_GO"})
    if passed:
        reason = "成本后样本外与独立复算均已通过"
    elif failed and catalog_matches:
        metrics = ((report.get("cost_ladder") or {}).get("5bp") or {})
        mean_return = metrics.get("mean_trade_return_pct")
        metric_text = (
            f"，5BP平均每笔{float(mean_return):+.2f}%"
            if isinstance(mean_return, (int, float)) else ""
        )
        reason = f"该60/40组合已在独立2026复权样本失败{metric_text}，已停用"
    elif report and not catalog_matches:
        reason = "production catalog 已变化；旧60/40组合验证自动失效，需按新目录独立复算"
    else:
        reason = "该实时60/40组合尚无成本后样本外与独立复算报告，只允许观察"
    strategy_spec_sha256 = str(report.get("strategy_spec_sha256") or "") or None
    if raw:
        try:
            evidence_path = str(_LIVE_STRATEGY_VALIDATION_PATH.relative_to(_PROJECT))
        except ValueError:
            evidence_path = str(_LIVE_STRATEGY_VALIDATION_PATH)
        evidence = {
            "path": evidence_path,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    else:
        evidence = None
    return {
        "strategy_id": LIVE_STRATEGY_ID,
        "catalog_version": catalog_version,
        "validated_catalog_version": report_catalog,
        "passed": passed,
        "verdict": verdict or "UNVALIDATED",
        "report": (
            "reports/live_factor_signal_validation.json"
            if report else None
        ),
        "strategy_spec_sha256": strategy_spec_sha256,
        "validation_evidence": evidence,
        "provenance_complete": bool(
            passed and strategy_spec_sha256 and evidence
        ),
        "reason": reason,
    }


def _production_strategy_ref(
    registry: dict, validation: dict | None, *, factor_as_of: str | None,
    code: str,
) -> dict | None:
    """Build the immutable identity required for one production BUY signal."""
    validation = validation if isinstance(validation, dict) else {}
    evidence = validation.get("validation_evidence")
    strategy_id = str(validation.get("strategy_id") or "")
    strategy_spec = str(validation.get("strategy_spec_sha256") or "")
    catalog_version = str(registry.get("catalog_version") or "")
    runtime_data_key = str(registry.get("runtime_data_key") or "")
    evidence_path = str((evidence or {}).get("path") or "")
    evidence_sha256 = str((evidence or {}).get("sha256") or "")
    if not (
        validation.get("provenance_complete") is True
        and registry.get("complete") is True
        and catalog_version
        and runtime_data_key
        and factor_as_of
        and strategy_id
        and strategy_spec
        and evidence_path
        and evidence_sha256
    ):
        return None
    identity = {
        "origin": "production_strategy",
        "strategy_id": strategy_id,
        "strategy_spec_sha256": strategy_spec,
        "catalog_version": catalog_version,
        "runtime_data_key": runtime_data_key,
        "factor_as_of": str(factor_as_of),
        "validation_evidence": {
            "path": evidence_path,
            "sha256": evidence_sha256,
        },
    }
    signal_body = json.dumps(
        {**identity, "code": str(code), "action": "BUY"},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return {
        **identity,
        "signal_id": hashlib.sha256(signal_body).hexdigest(),
        "production_eligible": True,
    }


class FactorCache:
    """完整 Panel 分数缓存；catalog 版本变化立即失效。"""

    def __init__(self):
        self.values: Dict[str, dict] = {}
        self.last_ts: Optional[datetime] = None
        self.catalog: dict = {}
        self.catalog_version: str | None = None

    def stale(self) -> bool:
        if self.last_ts is None:
            return True
        try:
            current = factor_production.catalog_identity().get("catalog_version")
        except Exception:
            return True
        if current != self.catalog_version:
            return True
        cached_evaluator = self.catalog.get("evaluator_fingerprint")
        if not cached_evaluator:
            return True
        try:
            if factor_production.evaluator_fingerprint() != cached_evaluator:
                return True
        except Exception:
            return True
        cached_input = self.catalog.get("runtime_data_key")
        if cached_input:
            try:
                if factor_production.current_input_identity() != cached_input:
                    return True
            except Exception:
                return True
        return (_now() - self.last_ts).total_seconds() > FACTOR_CACHE_HOURS * 3600

    def refresh(self, watchlist: List[str]) -> Dict[str, dict]:
        try:
            self.values, self.catalog = factor_production.load_verified_scores(watchlist)
        except Exception as exc:
            self.values = {}
            self.catalog = {
                "complete": False, "catalog_version": None, "factors": {},
                "error": f"{type(exc).__name__}: {exc}",
            }
        self.catalog_version = self.catalog.get("catalog_version")
        self.last_ts = _now()
        return self.values

    def get(self, watchlist: List[str]) -> Dict[str, dict]:
        if self.stale() or any(c not in self.values for c in watchlist):
            return self.refresh(watchlist)
        return self.values


_FACTOR_CACHE = FactorCache()


# ── 4. 信号合成 ───────────────────────────────────────────────────────────
def _zscore_cs(vals: List[float]) -> List[float]:
    arr = np.array(vals, dtype=float)
    if len(arr) == 0 or not np.isfinite(arr).any():
        return [np.nan] * len(arr)
    m, s = np.nanmean(arr), np.nanstd(arr)
    if not np.isfinite(s) or s == 0:
        return [np.nan] * len(arr)
    return ((arr - m) / s).tolist()


def _upper_limit_pct(code: str, name: str) -> float:
    """Conservative A-share daily upper-limit classification for ticket checks."""
    normalized_code = str(code).split(".")[0]
    normalized_name = str(name or "").upper().replace(" ", "")
    if "ST" in normalized_name:
        return 5.0
    if normalized_code.startswith(("300", "301", "688")):
        return 20.0
    return 10.0


def generate_signals(
    watchlist: List[str], quotes: dict, factor_values: Dict[str, dict],
    *, observation_allowed: bool = True, required_factor_as_of: str | None = None,
    execution_allowed: bool = False, production_catalog: dict | None = None,
    strategy_validation: dict | None = None,
) -> dict:
    """因子 z + 截面涨跌 z；仅显式手工真钱模式可把健康观察升级为 BUY。"""
    qs = {c: q for c, q in quotes.items() if c != "_meta"}
    if not qs:
        return {"signals": [], "health": "no_quotes", "note": "无实时行情，未生成信号"}
    codes = [
        c for c in watchlist if c in qs
        and re.fullmatch(r"(?:0|3|6)\d{5}", str(c))
        and (price := _finite_number(qs[c].get("price"))) is not None and price > 0
        and _finite_number(qs[c].get("pct_chg")) is not None
        and not any(marker in str(qs[c].get("name", "")).upper()
                    for marker in ("ETF", "LOF", "基金", "指数"))
    ]
    if not codes:
        return {
            "signals": [], "health": "no_valid_quote_fields",
            "note": "行情价格或涨跌幅缺失，未用0值代替，不生成信号",
        }

    # 因子层已经在完整市场方向化/标准化；这里只切自选池，绝不让 rejected
    # 或旧 epoch 数据通过动量分支单独生成 WATCH。
    registry = production_catalog or _production_factor_metadata()
    catalog_version = registry.get("catalog_version")
    catalog_ready = bool(registry.get("complete") and catalog_version)
    as_ofs = {}
    fz_z = {}
    for c in codes:
        fv = factor_values.get(c, {})
        as_ofs[c] = fv.get("_as_of")
        score = fv.get("_production_score")
        if (catalog_ready and fv.get("_catalog_version") == catalog_version
                and isinstance(score, (int, float)) and np.isfinite(score)):
            fz_z[c] = float(score)

    # 实时动量：今日涨跌幅 横截面 z
    pcts = [qs[c]["pct_chg"] for c in codes]
    pct_z = dict(zip(codes, _zscore_cs(pcts)))

    signals = []
    for c in codes:
        f = fz_z.get(c, np.nan)
        m = pct_z.get(c, np.nan)
        total = W_FACTOR * (f if np.isfinite(f) else 0.0) + W_MOMENTUM * (m if np.isfinite(m) else 0.0)
        pct = qs[c]["pct_chg"]
        reason_parts = []
        if np.isfinite(f):
            reason_parts.append(f"因子z={f:+.2f}")
        if np.isfinite(m):
            reason_parts.append(f"截面涨跌z={m:+.2f}")
        factor_current = required_factor_as_of is None or as_ofs.get(c) == required_factor_as_of
        buy_condition = total > WATCH_SCORE
        structural_watch = bool(np.isfinite(f) and f > 0.8)
        signal_condition = bool(catalog_ready and np.isfinite(f)
            and observation_allowed and factor_current and (
            buy_condition or structural_watch
        ))
        limit_pct = _upper_limit_pct(c, qs[c].get("name", c))
        near_upper_limit = pct >= limit_pct - LIMIT_PROXIMITY_PCT
        strategy_ref = (
            _production_strategy_ref(
                registry, strategy_validation, factor_as_of=as_ofs.get(c), code=c,
            )
            if signal_condition and buy_condition and execution_allowed
            else None
        )
        if signal_condition:
            action = (
                "BUY" if (
                    execution_allowed and strategy_ref is not None
                    and buy_condition and not near_upper_limit
                )
                else "WATCH"
            )
        else:
            action = "NO_SIGNAL"
        if not factor_current:
            reason_parts.append(f"因子日期落后于{required_factor_as_of}")
        execution_blocker = None
        if signal_condition and near_upper_limit:
            execution_blocker = f"涨幅{pct:.2f}%接近{limit_pct:.0f}%涨停，禁止生成追涨买入票据"
            reason_parts.append(execution_blocker)
        elif signal_condition and not buy_condition:
            execution_blocker = f"综合分{total:.3f}未超过{WATCH_SCORE:.1f}，仅结构性观察"
            reason_parts.append(execution_blocker)
        elif signal_condition and buy_condition and execution_allowed and strategy_ref is None:
            execution_blocker = "生产策略身份或验证证据不完整，未生成买入票据"
            reason_parts.append(execution_blocker)
        signals.append({
            "code": c, "name": qs[c].get("name", c),
            "action": action, "score": round(total, 3),
            "factor_z": round(f, 3) if np.isfinite(f) else None,
            "momentum_z": round(m, 3) if np.isfinite(m) else None,
            "pct_chg": round(pct, 2), "price": round(qs[c]["price"], 2),
            "factor_as_of": as_ofs.get(c),
            "execution_eligible": action == "BUY",
            "execution_blocker": execution_blocker,
            "strategy_ref": strategy_ref if action == "BUY" else None,
            "upper_limit_pct": limit_pct,
            "reason": " + ".join(reason_parts) or "无可用因子/动量",
        })
    signals.sort(key=lambda s: (-s["score"], s["code"]))
    return {
        "signals": signals, "health": "ok", "note": "",
        "factor_registry": registry,
    }


# ── 5. 持久化 + 审计 ─────────────────────────────────────────────────────
def _append_audit(entry: dict) -> None:
    _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def run_once(watchlist: Optional[List[str]] = None, factor_cache: Optional[FactorCache] = None) -> dict:
    """单次全周期：行情 → 因子 → 信号 → 落盘 + 审计。返回结果 dict。"""
    if watchlist is None:
        universe_identity = load_realtime_universe()
        watchlist = list(universe_identity.get("candidate_codes") or [])
    else:
        watchlist = list(watchlist)
        universe_identity = {
            "status": "caller_supplied",
            "reason": None,
            "universe_total": len(watchlist),
            "eligible_snapshot_complete": False,
            "full_a_share_coverage": False,
            "included_segments": [],
            "excluded_segments": [],
            "excluded_special_securities": {},
        }
    t0 = _time.time()
    mk = _market_status()
    quotes = fetch_realtime(watchlist)
    meta = quotes.pop("_meta", {})
    fcache = factor_cache if factor_cache is not None else _FACTOR_CACHE
    fvalues = fcache.get(watchlist)
    cached_catalog = getattr(fcache, "catalog", {}) or {}
    verified_registry = _production_factor_metadata()
    if (cached_catalog.get("catalog_version")
            == verified_registry.get("catalog_version")):
        verified_registry = {**verified_registry, **{
            key: value for key, value in cached_catalog.items()
            if key.startswith("runtime_")
        }}
    factor_as_of = sorted({v["_as_of"] for v in fvalues.values() if v.get("_as_of")})
    required_factor_as_of = factor_as_of[-1] if factor_as_of else None
    observation_allowed = bool(
        mk.get("open") is True
        and meta.get("degraded") is not True
        and meta.get("health_status") in {"live", "validated"}
    )
    strategy_validation = _live_strategy_validation_status(
        verified_registry.get("catalog_version"),
    )
    result_registry_complete = bool(verified_registry.get("complete"))
    execution_allowed = bool(
        observation_allowed
        and result_registry_complete
        and os.environ.get("OLYMPUS_MANUAL_REAL_MONEY_MODE") == "1"
        and strategy_validation.get("provenance_complete") is True
    )
    result = generate_signals(
        watchlist, quotes, fvalues,
        observation_allowed=observation_allowed,
        required_factor_as_of=required_factor_as_of,
        execution_allowed=execution_allowed,
        production_catalog=verified_registry,
        strategy_validation=strategy_validation,
    )
    all_signals = result.get("signals") or []
    selected_signals, selection_audit = _select_monitor_signals(
        all_signals, universe_identity, WATCHLIST_SIZE,
    )
    quote_attempted = int(meta.get("coverage_attempted") or len(watchlist))
    quote_covered = int(meta.get("coverage_returned") or len(quotes))
    universe_total = int(universe_identity.get("universe_total") or len(watchlist))
    runtime_counts = {
        "universe_total": universe_total,
        "quote_attempted": quote_attempted,
        "quote_covered": quote_covered,
        "quote_missing": max(0, universe_total - quote_covered),
        "fresh_quote_covered": int(meta.get("fresh_coverage_returned") or 0),
        "monitor_selected": len(selected_signals),
        "deep_selected": min(24, len(selected_signals)),
        "retained_selection_mode": selection_audit.get("mode"),
        "professional_preview_complete": (
            selection_audit.get("professional_preview_complete") is True
        ),
        "professional_preview_available_count": selection_audit.get(
            "professional_preview_available_count"
        ),
        "professional_preview_scope_count": selection_audit.get(
            "professional_preview_scope_count"
        ),
        "realtime_eligible_coverage_complete": bool(
            universe_identity.get("eligible_snapshot_complete") is True
            and quote_attempted >= universe_total
            and quote_covered >= universe_total
        ),
    }
    selection_audit = {
        **selection_audit,
        "quote_cross_section": {
            "source": meta.get("source"),
            "as_of": meta.get("as_of"),
            "health_status": meta.get("health_status"),
            "coverage_expected": int(meta.get("coverage_expected") or len(watchlist)),
            "coverage_attempted": quote_attempted,
            "coverage_returned": quote_covered,
            "decision_coverage_returned": int(
                meta.get("decision_coverage_returned") or 0
            ),
            "coverage_complete": runtime_counts[
                "realtime_eligible_coverage_complete"
            ],
        },
        "implementation": {
            "signal_system_sha256": hashlib.sha256(
                Path(__file__).read_bytes()
            ).hexdigest(),
        },
    }

    payload = {
        "generated_at": _now().isoformat(),
        "market_status": mk,
        "quote_meta": meta,
        "n_watchlist": len(selected_signals),
        "watchlist_scope": watchlist_scope(
            len(selected_signals), universe_identity=universe_identity,
            runtime_counts=runtime_counts,
        ),
        "monitor_selected_codes": [row["code"] for row in selected_signals],
        "selection_audit": selection_audit,
        "n_quotes": sum(1 for c in quotes if c != "_meta"),
        "n_factor_codes": len(fvalues),
        "factor_as_of": factor_as_of,
        "required_factor_as_of": required_factor_as_of,
        "factor_registry": result.get("factor_registry") or {},
        "strategy_validation": strategy_validation,
        "execution_mode": (
            "manual_broker_ticket" if execution_allowed else "research_observation"
        ),
        "broker_connected": False,
        "automatic_order": False,
        "signals": selected_signals,
        "latency_s": round(_time.time() - t0, 2),
        "honesty": (
            "行情=腾讯批量/统一数据层；腾讯逐票校验交易所时间，"
            "AkShare 无可核验时点时仅降级展示；"
            "因子=当前epoch的production catalog，recipe/前向证据/制品hash复核后"
            "在完整Panel全市场重算；实时行情先覆盖完整合格候选全集，"
            "再按正式生产信号优先、完整专业预览或统一市场证据回退的确定性顺序保留Top300；"
            "盘中涨跌只作执行观察，不决定监控池准入；"
            "监控池不随机、不轮换；"
            f"{strategy_validation['reason']}。"
            "系统不连接券商、不确认成交；休市或真实行情不可用时不产生信号。"
        ),
    }
    _SIGNALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SIGNALS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    _append_audit({
        "ts": _now().isoformat(),
        "source": meta.get("source"),
        "as_of": meta.get("as_of"),
        "health_status": meta.get("health_status"),
        "participates_in_decision": False,
        "degraded": meta.get("degraded", False),
        "market_open": mk["open"],
        "n_watchlist": payload["n_watchlist"],
        "n_quotes": payload["n_quotes"],
        "n_signals": len(selected_signals),
        "latency_s": payload["latency_s"],
    })
    return payload


def load_live_signals() -> dict:
    if not _SIGNALS_PATH.exists():
        return {}
    try:
        return json.loads(_SIGNALS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def tail_audit(limit: int = 50) -> List[dict]:
    if not _AUDIT_PATH.exists():
        return []
    lines = _AUDIT_PATH.read_text(encoding="utf-8").strip().splitlines()
    out = []
    for ln in lines[-limit:]:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


if __name__ == "__main__":
    print("=== 实时信号系统 单次自检 ===")
    wl = load_watchlist()
    print(f"watchlist: {len(wl)} 只 ({', '.join(wl[:5])}...)")
    r = run_once(wl)
    print("行情源:", r["quote_meta"].get("source"), "| 健康:", r["quote_meta"].get("health_status"))
    print("市场状态:", r["market_status"]["label"])
    print("因子 as_of:", r["factor_as_of"])
    for s in r["signals"][:8]:
        print(f"  {s['action']:<9} {s['code']} {s['name']} score={s['score']} 涨跌={s['pct_chg']}% {s['reason']}")
    print("落盘:", _SIGNALS_PATH)
