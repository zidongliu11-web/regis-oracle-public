#!/usr/bin/env python3
"""沪深审计市场多维找股（本地日线，只读计算）。

背景：实盘"今日机会"只吃 kronos AI 看涨度 top 池（200 只，pred_ret>0 仅 65）。
实证：当前 200 只模型池不能代表全市场。研究层先对完整可交易宇宙排序，账户资金、
整手与费用约束留到交易计划层按用户真实现金判断，避免把研究覆盖和账户执行混为一谈。

本脚本：离线读已审计的主板/创业板 V4 与独立科创板监控补充 → 市场预筛 →
趋势、回撤、波动、流动性与上涨一致性横截面评分 → top N。只写报告 JSON，
不动 live 状态；科创板补充不进入冻结因子面板，北交所未覆盖时显式披露。

口径透明：
- 研究宇宙不按股价或假定账户资金裁剪；一手是否买得起由实时交易计划使用真实现金判断。
- 流动性：近 20 日日均成交额 ≥ ¥0.5 亿（能容纳 1 万/笔而不显著冲击）。
- 收益：使用审计 pctChg 复利，避免除权造成虚假跳变。
- 排序：市场初筛不是全站综合评级；公司、估值、资金和时机继续由
  professional_composite_v2_continuous_anchor 在深评阶段统一计算。
- 排名前 N 只与 kronos 200 只对比，量化"拓宽宇宙"真实新增了多少候选。
"""
from __future__ import annotations

import gzip
import csv
import hashlib
import json
import math
import statistics
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
from mom_trading.full_market_runtime import audited_universe

V4 = BASE / "research" / "data" / "v4_daily"
STAR = BASE / "research" / "data" / "monitoring_star_daily"
INDUSTRY_MAP = BASE / "research" / "industry_map.json"
INDUSTRY_SNAPSHOT = BASE / "reports" / "v86_baostock_industry_snapshot.json"
INDUSTRY_SNAPSHOT_SHA256 = "d24fb10211a6cbefe696ec4d43214d9d5660e8e1c4faab771c900610b02af2bc"
STOCK_BASIC = BASE / "research" / "data" / "stock_basic_baostock.csv"
OUT = BASE / "research" / "full_market_top300.json"

MIN_AMOUNT_20D = 0.5e8    # 近20日日均成交额 ≥ 0.5 亿
MIN_ROWS = 61             # 至少 60 个已完成交易日，才能同口径比较趋势与风险
TOP_N = 300
SCREEN_METHOD = "market_screen_v3_universe_first_trend_risk_liquidity"
RISK_WARNING_PREFIXES = ("*ST", "S*ST", "SST", "ST")


def valid_research_price(value: float) -> bool:
    """Research coverage only needs a usable positive price; cash belongs downstream."""
    return math.isfinite(value) and value > 0


def is_risk_warning_security(name: object, is_st: object) -> bool:
    """Fail closed when the daily flag and the official name disagree."""
    try:
        flagged = int(is_st) != 0
    except (TypeError, ValueError):
        flagged = True
    normalized = str(name or "").upper().replace(" ", "")
    return flagged or normalized.startswith(RISK_WARNING_PREFIXES)


def parse_bar(line: str) -> dict | None:
    """csv 行 → dict；解析失败返回 None。"""
    p = line.strip().split(",")
    if len(p) < 14:
        return None
    try:
        return {
            "date": p[0],
            "open": float(p[2]),
            "high": float(p[3]),
            "low": float(p[4]),
            "close": float(p[5]),
            "volume": float(p[7]),
            "amount": float(p[8]),
            "turn": float(p[10]),
            "pctChg": float(p[12]),
            "tradestatus": int(p[11]),
            "isST": int(p[13]),
        }
    except (ValueError, IndexError):
        return None


def audited_daily_paths(project: Path = BASE) -> list[Path]:
    """Return exact audited monitoring files, or fail closed on any drift."""
    universe = audited_universe(project)
    if universe.get("status") != "verified":
        return []
    directories = (
        project / "research" / "data" / "v4_daily",
        project / "research" / "data" / "monitoring_star_daily",
    )
    paths = sorted(path for directory in directories for path in directory.glob("*.csv.gz"))
    return paths if len(paths) == universe.get("count") else []


def read_series(code: str, directory: Path | None = None) -> list[dict]:
    """读单只日线，返回 tradestatus=1 的行（日期升序）。内存按文件释放。"""
    if directory is None:
        path = V4 / f"{code}.csv.gz"
        if not path.exists():
            path = STAR / f"{code}.csv.gz"
    else:
        path = directory / f"{code}.csv.gz"
    rows: list[dict] = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            next(f, None)  # header
            for line in f:
                b = parse_bar(line)
                if b and b["tradestatus"] == 1:
                    rows.append(b)
    except (OSError, gzip.BadGzipFile):
        return []
    return rows


def _compounded_pct(rows: list[dict], days: int) -> float:
    values = [float(row["pctChg"]) / 100.0 for row in rows[-days:]]
    return (math.prod(1.0 + value for value in values) - 1.0) * 100.0


def _risk_metrics(rows: list[dict]) -> tuple[float, float, float]:
    daily = [float(row["pctChg"]) / 100.0 for row in rows[-60:]]
    index, peak, max_drawdown = 1.0, 1.0, 0.0
    for value in daily:
        index *= 1.0 + value
        peak = max(peak, index)
        max_drawdown = max(max_drawdown, (peak - index) / peak)
    volatility = statistics.stdev(daily[-20:]) * math.sqrt(252.0) * 100.0
    positive_ratio = sum(value > 0 for value in daily[-20:]) / 20.0 * 100.0
    return max_drawdown * 100.0, volatility, positive_ratio


def _float_cap_proxy(rows: list[dict]) -> float | None:
    """20-day median free-float market-cap proxy for coverage stratification.

    BaoStock turnover is percent of free-float shares, so
    ``close * volume / (turn / 100)`` estimates same-day free-float value.
    This proxy never enters the stock score or return test.
    """
    values = []
    for row in rows[-20:]:
        try:
            close = float(row["close"])
            volume = float(row["volume"])
            turnover = float(row["turn"])
        except (KeyError, TypeError, ValueError):
            continue
        if all(math.isfinite(value) for value in (close, volume, turnover)) and close > 0 and volume > 0 and turnover > 0:
            values.append(close * volume / (turnover / 100.0))
    return statistics.median(values) if len(values) >= 10 else None


def _load_industry_classification() -> tuple[dict[str, str], dict[str, object]]:
    """Load the frozen CSRC classification snapshot, failing closed on drift."""
    try:
        if hashlib.sha256(INDUSTRY_SNAPSHOT.read_bytes()).hexdigest() != INDUSTRY_SNAPSHOT_SHA256:
            raise ValueError("industry_snapshot_sha256_mismatch")
        payload = json.loads(INDUSTRY_SNAPSHOT.read_text(encoding="utf-8"))
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("industry_snapshot_rows_missing")
        mapping = {
            str(row.get("code") or "").split(".")[-1]: str(row.get("industry") or "").strip()
            for row in rows if isinstance(row, dict) and str(row.get("industry") or "").strip()
        }
        if len(mapping) != int(payload.get("nonempty_industry_count") or 0):
            raise ValueError("industry_snapshot_count_mismatch")
        return mapping, {
            "status": "verified",
            "path": "reports/v86_baostock_industry_snapshot.json",
            "sha256": INDUSTRY_SNAPSHOT_SHA256,
            "snapshot_fingerprint": payload.get("snapshot_fingerprint"),
            "as_of": (payload.get("update_dates") or [None])[-1],
            "classification": payload.get("classification"),
            "mapped_count": len(mapping),
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return {}, {
            "status": "unavailable",
            "reason": str(exc),
            "path": "reports/v86_baostock_industry_snapshot.json",
        }


def _compact_professional_preview(code: str, rows: list[dict], amount: float) -> dict:
    """Use only local PIT/archive/completed bars; never fetch network data."""
    try:
        from trade_assistant.candles import Candle, technical_snapshot
        from mom_trading.workstation import (
            _price_risk_metrics,
            compact_professional_preview,
        )
        candles = [
            Candle(
                time=str(row["date"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                amount=float(row["amount"]),
            )
            for row in rows[-200:]
        ]
        return compact_professional_preview(
            code,
            amount=amount,
            amount_as_of=str(rows[-1]["date"]),
            technical_snap=technical_snapshot(candles),
            technical_as_of=str(rows[-1]["date"]),
            market_risk=_price_risk_metrics(candles),
        )
    except (KeyError, TypeError, ValueError):
        return {
            "status": "unavailable_local_inputs",
            "professional_score": None,
            "decision_eligible": False,
            "network_reads": False,
        }


def _finite_preview_score(row: dict) -> float | None:
    try:
        value = float(
            (row.get("preselection_professional_preview") or {}).get(
                "professional_score"
            )
        )
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _percentiles(rows: list[dict], field: str, *, reverse: bool = False) -> dict[str, float]:
    values = sorted({float(row[field]) for row in rows})
    if len(values) == 1:
        return {str(row["code"]): 50.0 for row in rows}
    ranks = {value: index / (len(values) - 1) * 100.0 for index, value in enumerate(values)}
    return {
        str(row["code"]): 100.0 - ranks[float(row[field])]
        if reverse else ranks[float(row[field])]
        for row in rows
    }


def score_candidates(rows: list[dict]) -> list[dict]:
    """Deterministic cross-sectional pre-rank; no future return is read."""
    if not rows:
        return []
    ranks = {
        "ret_5d": _percentiles(rows, "ret_5d"),
        "ret_20d": _percentiles(rows, "ret_20d"),
        "ret_60d": _percentiles(rows, "ret_60d"),
        "max_drawdown_60d": _percentiles(rows, "max_drawdown_60d", reverse=True),
        "annualized_volatility_20d": _percentiles(
            rows, "annualized_volatility_20d", reverse=True,
        ),
        "avg_amount_20d": _percentiles(rows, "avg_amount_20d"),
        "positive_day_ratio_20d": _percentiles(rows, "positive_day_ratio_20d"),
    }
    output = []
    for row in rows:
        code = str(row["code"])
        components = {
            "trend": {
                "score": round(max(0.0,
                    ranks["ret_5d"][code] * 0.20
                    + ranks["ret_20d"][code] * 0.45
                    + ranks["ret_60d"][code] * 0.35
                    - max(0.0, float(row["ret_5d"]) - 12.0) * 2.0
                    - max(0.0, float(row["ret_20d"]) - 30.0) * 1.5
                    - max(0.0, float(row["ret_60d"]) - 60.0) * 0.5
                ), 2),
                "weight_pct": 35,
                "overheat_penalty": round(
                    max(0.0, float(row["ret_5d"]) - 12.0) * 2.0
                    + max(0.0, float(row["ret_20d"]) - 30.0) * 1.5
                    + max(0.0, float(row["ret_60d"]) - 60.0) * 0.5,
                    2,
                ),
            },
            "drawdown": {"score": round(ranks["max_drawdown_60d"][code], 2), "weight_pct": 20},
            "volatility": {"score": round(ranks["annualized_volatility_20d"][code], 2), "weight_pct": 15},
            "liquidity": {"score": round(ranks["avg_amount_20d"][code], 2), "weight_pct": 20},
            "consistency": {"score": round(ranks["positive_day_ratio_20d"][code], 2), "weight_pct": 10},
        }
        score = sum(part["score"] * part["weight_pct"] / 100.0 for part in components.values())
        output.append({
            **row,
            "screen_method": SCREEN_METHOD,
            "screen_score": round(score, 2),
            "screen_components": components,
        })
    return sorted(output, key=lambda row: (-row["screen_score"], str(row["code"])))


def screen_full_market_snapshot(
    top_n: int = TOP_N, quiet: bool = True,
) -> dict[str, object]:
    """离线全市场预筛 + 多维横截面排名，并返回同轮扫描审计。

    每项含趋势、回撤、波动、流动性、一致性及可审计分项。
    - 股价：只要求为有限正数；账户可买手数由交易计划层按真实现金判断
    - 流动性：近20日日均成交额 ≥ 0.5 亿
    - 排除 ST / 停牌 / 无历史 / 流动性不足；行业不决定候选资格
    """
    t0 = time.time()
    industry: dict[str, str] = {}
    name_map: dict[str, str] = {}
    industry, industry_audit = _load_industry_classification()
    if INDUSTRY_MAP.exists():
        try:
            raw = json.loads(INDUSTRY_MAP.read_text(encoding="utf-8"))
            for code, ind in (raw or {}).items():
                industry.setdefault(code, str(ind))
                name_map[code] = str(ind)
        except (OSError, json.JSONDecodeError):
            pass
    if STOCK_BASIC.exists():
        try:
            with STOCK_BASIC.open(encoding="utf-8", newline="") as stream:
                for row in csv.DictReader(stream):
                    raw_code = str(row.get("code") or "")
                    code = raw_code.split(".")[-1]
                    name = str(row.get("name") or "").strip()
                    if len(code) == 6 and name:
                        name_map[code] = name
        except OSError:
            pass

    universe = audited_universe(BASE)
    audit_data_asof = str(universe.get("data_asof") or "")
    paths = audited_daily_paths()
    codes = [path.name.removesuffix(".csv.gz") for path in paths]
    if not quiet:
        print(f"[finder] 审计日线 {len(codes)} 只；行业映射 {len(industry)}", flush=True)

    exclusion_counts = {
        "unreadable_or_insufficient_history": 0,
        "st": 0,
        "invalid_price": 0,
        "insufficient_liquidity": 0,
        "blacklisted": 0,
        "blacklist_check_error": 0,
        "stale_asof": 0,
    }
    inspected = 0
    post_audit_rows_ignored = 0
    files_with_post_audit_rows = 0
    passed: list[dict] = []
    for i, (code, path) in enumerate(zip(codes, paths)):
        if not quiet and i % 800 == 0 and i:
            print(f"[finder] 处理 {i}/{len(codes)} …通过 {len(passed)}", flush=True)
        inspected += 1
        rows = read_series(code, path.parent)
        if audit_data_asof:
            audited_rows = [
                row for row in rows
                if str(row.get("date") or "") <= audit_data_asof
            ]
            ignored = len(rows) - len(audited_rows)
            if ignored:
                post_audit_rows_ignored += ignored
                files_with_post_audit_rows += 1
            rows = audited_rows
        if len(rows) < MIN_ROWS:
            exclusion_counts["unreadable_or_insufficient_history"] += 1
            continue
        last = rows[-1]
        security_name = name_map.get(code, "")
        if is_risk_warning_security(security_name, last["isST"]):
            exclusion_counts["st"] += 1
            continue
        close = last["close"]
        if not valid_research_price(close):
            exclusion_counts["invalid_price"] += 1
            continue
        ret_5d = _compounded_pct(rows, 5)
        ret_20d = _compounded_pct(rows, 20)
        ret_60d = _compounded_pct(rows, 60)
        max_drawdown, volatility, positive_ratio = _risk_metrics(rows)
        avg_amt = sum(r["amount"] for r in rows[-20:]) / min(20, len(rows))
        if avg_amt < MIN_AMOUNT_20D:
            exclusion_counts["insufficient_liquidity"] += 1
            continue
        float_cap_proxy = _float_cap_proxy(rows)
        professional_preview = _compact_professional_preview(code, rows, avg_amt)
        classification = industry.get(code, "")
        passed.append({
            "code": code,
            "close": round(close, 2),
            "data_asof": last["date"],
            "ret_5d": round(ret_5d, 2),
            "ret_20d": round(ret_20d, 2),
            "ret_60d": round(ret_60d, 2),
            "max_drawdown_60d": round(max_drawdown, 2),
            "annualized_volatility_20d": round(volatility, 2),
            "positive_day_ratio_20d": round(positive_ratio, 2),
            "avg_amount_20d": round(avg_amt, 0),
            "float_cap_proxy_20d_median": (
                round(float_cap_proxy, 0) if float_cap_proxy is not None else None
            ),
            "classification_label": (
                classification or (
                    "__UNMAPPED__:科创板"
                    if path.parent == STAR else "__UNMAPPED__:沪深主板或创业板"
                )
            ),
            "classification_status": "mapped" if classification else "unmapped_explicit",
            "industry": classification,
            "name": security_name,
            "market_segment": "科创板" if path.parent == STAR else "沪深主板或创业板",
            "preselection_professional_preview": professional_preview,
        })
    eligible_before_asof = len(passed)
    latest_asof = max((row["data_asof"] for row in passed), default=None)
    current = [row for row in passed if row["data_asof"] == latest_asof]
    exclusion_counts["stale_asof"] = eligible_before_asof - len(current)
    market_ranked = score_candidates(current)
    preview_available_count = sum(
        1 for row in market_ranked
        if (row.get("preselection_professional_preview") or {}).get("status") == "available"
        and _finite_preview_score(row) is not None
    )
    professional_preview_complete = bool(
        market_ranked and preview_available_count == len(market_ranked)
    )
    if professional_preview_complete:
        ranked = sorted(
            market_ranked,
            key=lambda row: (
                -float(_finite_preview_score(row)),
                -float(row.get("screen_score") or 0.0),
                str(row.get("code") or ""),
            ),
        )
        selection_method = "professional_composite_v3_full_eligible_preview_then_market_tiebreak"
    else:
        ranked = market_ranked
        selection_method = "market_screen_v3_uniform_fallback_incomplete_professional_coverage"
    limit = max(0, int(top_n))
    top = ranked[:limit]
    universe_count = universe.get("count")
    complete_path_scan = bool(
        universe.get("status") == "verified"
        and isinstance(universe_count, int)
        and universe_count == len(paths) == inspected
    )
    data_asof_matches_audit = bool(
        latest_asof
        and latest_asof == universe.get("data_asof")
    )
    scan_audit = {
        "status": (
            "verified"
            if complete_path_scan
            and data_asof_matches_audit
            and exclusion_counts["blacklist_check_error"] == 0
            else "degraded"
        ),
        "universe_count": universe_count,
        "audited_path_count": len(paths),
        "inspected_file_count": inspected,
        "complete_path_scan": complete_path_scan,
        "eligible_before_asof_count": eligible_before_asof,
        "current_asof_count": len(current),
        "ranked_count": len(ranked),
        "top_count": len(top),
        "selection_method": selection_method,
        "professional_preview_available_count": preview_available_count,
        "professional_preview_scope_count": len(market_ranked),
        "professional_preview_coverage_pct": round(
            preview_available_count / len(market_ranked) * 100.0, 2,
        ) if market_ranked else 0.0,
        "professional_preview_complete": professional_preview_complete,
        "professional_preview_partial_ranking_forbidden": True,
        "industry_classification": industry_audit,
        "data_asof": latest_asof,
        "data_asof_matches_universe_audit": data_asof_matches_audit,
        "post_audit_rows_ignored_count": post_audit_rows_ignored,
        "files_with_post_audit_rows_count": files_with_post_audit_rows,
        "exclusion_counts": exclusion_counts,
    }
    if not quiet:
        print(
            f"[finder] 实扫 {inspected}/{len(paths)}，当期有效 {len(ranked)}，"
            f"top{limit} 用时 {time.time()-t0:.1f}s",
            flush=True,
        )
    return {"top": top, "scan_audit": scan_audit}


def screen_full_market(top_n: int = TOP_N, quiet: bool = True) -> list[dict]:
    """Compatibility wrapper returning only the ranked candidates."""
    return list(screen_full_market_snapshot(top_n, quiet)["top"])


def main() -> int:
    t0 = time.time()
    universe = audited_universe(BASE)
    paths = audited_daily_paths()
    if not paths:
        print("[finder] 审计市场范围不可用；拒绝覆盖旧快照", file=sys.stderr)
        return 2
    result = screen_full_market_snapshot(int(universe["count"]), quiet=False)
    eligible = result["top"]
    top = eligible[:TOP_N]
    kronos = set()
    kp = BASE / "research" / "kronos_stock_snapshot.json"
    if kp.exists():
        try:
            kronos = set(json.loads(kp.read_text(encoding="utf-8")).get("stocks", {}).keys())
        except (OSError, json.JSONDecodeError):
            pass
    top_codes = {t["code"] for t in top}
    new_codes = top_codes - kronos

    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "universe_count": universe["count"],
        "universe_audit": universe,
        "scan_audit": result["scan_audit"],
        "data_asof": top[0].get("data_asof") if top else None,
        "ranking_method": result["scan_audit"].get("selection_method") or SCREEN_METHOD,
        "market_screen_method": SCREEN_METHOD,
        "passed_count": len(eligible),
        "eligible_count": len(eligible),
        "eligible_complete": True,
        "eligible": eligible,
        "top_count": len(top),
        "top_n": TOP_N,
        "new_vs_kronos": len(new_codes),
        "kronos_count": len(kronos),
        "cron": {"schedule": "Phase2 接入后由 app 后台线程刷新"},
        "top": top,
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[finder] top{len(top)} 中不在 kronos({len(kronos)}) 的 {len(new_codes)} 只", flush=True)
    print(f"[finder] 报告已写 {OUT}", flush=True)
    print(f"[finder] 用时 {time.time()-t0:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
