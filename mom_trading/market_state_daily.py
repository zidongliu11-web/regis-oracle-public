"""Immutable, research-only end-of-day market evidence.

The builder is deliberately offline: it reads the already-audited BaoStock
daily epoch and the persisted THS observation, then publishes one immutable
snapshot.  Nothing in this module creates a stock score or position target.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
from bisect import bisect_left, bisect_right
from collections import deque
from datetime import date, datetime, time
from pathlib import Path
from statistics import median, pstdev
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from research.market_rules import daily_price_limits


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "market_state_daily.v1"
DATA_ROOT = PROJECT_ROOT / "research" / "data" / "market_state_daily" / "v1"
SNAPSHOT_DIR = DATA_ROOT / "snapshots"
EVIDENCE_DIR = DATA_ROOT / "evidence"
SH_TZ = ZoneInfo("Asia/Shanghai")


class SnapshotConflict(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ) + "\n"
    ).encode("utf-8")


def _atomic_publish(path: Path, payload: bytes) -> bool:
    """Publish without overwrite; identical retries are idempotent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
            return True
        except FileExistsError:
            if path.read_bytes() == payload:
                return False
            raise SnapshotConflict(f"同一交易日已有不同内容：{path}")
    finally:
        temporary.unlink(missing_ok=True)


def _freeze_bytes(payload: bytes, suffix: str, *, data_root: Path) -> dict[str, Any]:
    digest = hashlib.sha256(payload).hexdigest()
    path = data_root / "evidence" / f"{digest}{suffix}"
    _atomic_publish(path, payload)
    return {
        "path": str(path.relative_to(PROJECT_ROOT)),
        "sha256": digest,
        "bytes": len(payload),
    }


def _freeze_file(path: Path, *, data_root: Path) -> dict[str, Any]:
    return _freeze_bytes(path.read_bytes(), path.suffix, data_root=data_root)


def _number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _pct(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator * 100, 3)


def _listed_days(
    trade_day: str, ipo_day: str, calendar: list[str], calendar_start: str,
) -> int:
    # The price-limit contract only distinguishes the first five sessions.
    # Pre-window listings are safely represented as at least six sessions old.
    if not ipo_day or ipo_day < calendar_start:
        return 6 + bisect_right(calendar, trade_day)
    return max(
        1,
        bisect_right(calendar, trade_day) - bisect_left(calendar, ipo_day),
    )


def _limit_flags(
    code: str,
    row: dict[str, str] | None,
    *,
    ipo_day: str,
    calendar: list[str],
    special_events: dict[tuple[str, str], str],
) -> dict[str, Any]:
    if not row or str(row.get("tradestatus")) != "1" or str(row.get("isST")) != "0":
        return {"evaluable": False, "closed": False, "touched": False}
    day = str(row.get("date") or "")
    previous_close = _number(row.get("preclose"))
    high = _number(row.get("high"))
    close = _number(row.get("close"))
    if not day or previous_close is None or high is None or close is None:
        return {"evaluable": False, "closed": False, "touched": False}
    limits = daily_price_limits(
        code,
        date.fromisoformat(day),
        previous_close,
        is_st=False,
        listed_trading_days=_listed_days(day, ipo_day, calendar, calendar[0]),
        special_event=special_events.get((code, day)),
    )
    up_limit = limits.get("up_limit")
    if up_limit is None:
        return {"evaluable": False, "closed": False, "touched": False}
    tolerance = 0.0051
    return {
        "evaluable": True,
        "closed": abs(close - float(up_limit)) <= tolerance,
        "touched": high >= float(up_limit) - tolerance,
        "up_limit": float(up_limit),
    }


def _stock_observation(
    code: str,
    rows: Iterable[dict[str, str]],
    *,
    trade_date: str,
    previous_date: str,
    ipo_day: str,
    calendar: list[str],
    special_events: dict[tuple[str, str], str],
) -> dict[str, Any]:
    by_date = {str(row.get("date") or ""): row for row in rows}
    current = by_date.get(trade_date)
    previous = by_date.get(previous_date)
    current_flags = _limit_flags(
        code, current, ipo_day=ipo_day, calendar=calendar,
        special_events=special_events,
    )
    previous_flags = _limit_flags(
        code, previous, ipo_day=ipo_day, calendar=calendar,
        special_events=special_events,
    )
    streak = 0
    target_index = bisect_right(calendar, trade_date) - 1
    if current_flags["closed"]:
        for day in reversed(calendar[: target_index + 1]):
            flags = _limit_flags(
                code, by_date.get(day), ipo_day=ipo_day, calendar=calendar,
                special_events=special_events,
            )
            if not flags["closed"]:
                break
            streak += 1
    streak_capped = bool(streak == len(by_date) == 64)

    active_rows = [
        row for row in rows
        if str(row.get("tradestatus")) == "1" and _number(row.get("close")) is not None
    ]
    closes = [_number(row.get("close")) for row in active_rows[-20:]]
    closes = [value for value in closes if value is not None]
    current_close = _number((current or {}).get("close"))
    return {
        "current": current,
        "current_flags": current_flags,
        "previous_flags": previous_flags,
        "streak": streak,
        "streak_capped": streak_capped,
        "ma20_available": len(closes) == 20 and current_close is not None,
        "above_ma20": (
            current_close > sum(closes) / len(closes)
            if len(closes) == 20 and current_close is not None else None
        ),
        "new_high_20d": (
            current_close >= max(closes)
            if len(closes) == 20 and current_close is not None else None
        ),
        "new_low_20d": (
            current_close <= min(closes)
            if len(closes) == 20 and current_close is not None else None
        ),
    }


def _load_reference_maps(
    master_path: Path, event_path: Path,
) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    with master_path.open(encoding="utf-8-sig", newline="") as stream:
        ipo_by_code = {
            str(row["code"]): str(row.get("ipo_date") or "")
            for row in csv.DictReader(stream)
            if str(row.get("type")) == "1"
        }
    with event_path.open(encoding="utf-8-sig", newline="") as stream:
        events = {
            (str(row["code"]), str(row["date"])): str(row["event_type"])
            for row in csv.DictReader(stream)
            if str(row.get("verification_status")) == "verified"
        }
    return ipo_by_code, events


def _scan_v4_scope(
    *,
    quality: dict[str, Any],
    calendar: list[str],
    ipo_by_code: dict[str, str],
    special_events: dict[tuple[str, str], str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    trade_date = calendar[-1]
    previous_date = calendar[-2]
    up = down = flat = st_count = 0
    ma20_total = above_ma20 = new_high = new_low = 0
    touch_count = closed_count = failed_count = 0
    previous_closed_evaluable = continued_count = 0
    first = second = third = four_plus = highest = 0
    returns: list[float] = []
    amounts: list[float] = []
    turnovers: list[float] = []
    closed_amount = 0.0
    input_files: list[dict[str, Any]] = []

    files = quality.get("files") if isinstance(quality.get("files"), list) else []
    if not (
        len(files) == int(quality.get("file_count") or -1)
        == int(quality.get("passed_files") or -2)
    ):
        raise RuntimeError("V4质量审计文件清单不是完整通过全集")
    for item in files:
        relative = str((item or {}).get("file") or "")
        path = PROJECT_ROOT / relative
        if not relative or not path.is_file() or path.is_symlink():
            raise RuntimeError(f"V4输入文件缺失或非法：{relative}")
        digest = _sha256(path)
        input_files.append({
            "path": relative, "sha256": digest, "bytes": path.stat().st_size,
        })
        tail: deque[dict[str, str]] = deque(maxlen=64)
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                tail.append(row)
        if not tail:
            continue
        code = str(tail[-1].get("code") or "")
        observation = _stock_observation(
            code,
            tail,
            trade_date=trade_date,
            previous_date=previous_date,
            ipo_day=ipo_by_code.get(code, ""),
            calendar=calendar,
            special_events=special_events,
        )
        if observation["streak_capped"]:
            raise RuntimeError(f"连板尾窗不足，停止发布：{code}")
        row = observation["current"]
        if not row or str(row.get("tradestatus")) != "1":
            continue
        change = _number(row.get("pctChg"))
        if change is not None:
            returns.append(change)
            if change > 0:
                up += 1
            elif change < 0:
                down += 1
            else:
                flat += 1
        if str(row.get("isST")) == "1":
            st_count += 1
        if observation["ma20_available"]:
            ma20_total += 1
            above_ma20 += int(bool(observation["above_ma20"]))
            new_high += int(bool(observation["new_high_20d"]))
            new_low += int(bool(observation["new_low_20d"]))

        amount = _number(row.get("amount"))
        turnover = _number(row.get("turn"))
        if amount is not None and amount >= 0:
            amounts.append(amount)
        if turnover is not None and turnover >= 0:
            turnovers.append(turnover)

        flags = observation["current_flags"]
        if flags["evaluable"]:
            touch_count += int(bool(flags["touched"]))
            closed_count += int(bool(flags["closed"]))
            failed_count += int(bool(flags["touched"] and not flags["closed"]))
            if flags["closed"] and amount is not None:
                closed_amount += amount
            streak = int(observation["streak"])
            highest = max(highest, streak)
            first += int(streak == 1)
            second += int(streak == 2)
            third += int(streak == 3)
            four_plus += int(streak >= 4)
            if observation["previous_flags"]["closed"]:
                previous_closed_evaluable += 1
                continued_count += int(bool(flags["closed"]))

    usable = up + down + flat
    sorted_amounts = sorted(amounts, reverse=True)
    top_count = max(1, math.ceil(len(sorted_amounts) * 0.05)) if sorted_amounts else 0
    total_amount = sum(sorted_amounts)
    sorted_turnover = sorted(turnovers)
    p95_index = max(0, math.ceil(len(sorted_turnover) * 0.95) - 1) if sorted_turnover else 0
    dimensions = {
        "B": {
            "status": "available", "as_of": f"{trade_date}T15:00:00+08:00",
            "source_ids": ["v4_baostock_eod"],
            "coverage": {
                "scope": "sh_sz_main_chinext", "scope_complete": True,
                "full_a_share": False, "files": len(input_files), "usable": usable,
            },
            "values": {
                "up_count": up, "down_count": down, "flat_count": flat,
                "up_ratio_pct": _pct(up, usable),
                "median_return_pct": round(median(returns), 4) if returns else None,
                "above_ma20_ratio_pct": _pct(above_ma20, ma20_total),
                "new_high_20d_count": new_high, "new_low_20d_count": new_low,
                "st_count": st_count,
            },
            "missing_fields": [],
            "reason": "沪深主板与创业板范围完整；科创板和北交所尚未接入。",
        },
        "L": {
            "status": "available", "as_of": f"{trade_date}T15:00:00+08:00",
            "source_ids": ["v4_baostock_eod"],
            "coverage": {
                "scope": "sh_sz_main_chinext_non_st_ordinary_limit",
                "scope_complete": True, "full_a_share": False,
            },
            "values": {
                "first_board_count": first, "second_board_count": second,
                "third_board_count": third, "four_plus_board_count": four_plus,
                "highest_board": highest,
                "yesterday_limit_continuation_denominator": previous_closed_evaluable,
                "yesterday_limit_continuation_ratio_pct": _pct(
                    continued_count, previous_closed_evaluable,
                ),
            },
            "missing_fields": [
                "first_touch_time", "reopen_count", "seal_queue_amount",
                "intraday_tradability",
            ],
            "reason": "严格收盘连板；不把日线推断成盘中封板过程或可成交性。",
        },
        "F": {
            "status": "available", "as_of": f"{trade_date}T15:00:00+08:00",
            "source_ids": ["v4_baostock_eod"],
            "coverage": {
                "scope": "sh_sz_main_chinext_non_st_ordinary_limit",
                "scope_complete": True, "full_a_share": False,
            },
            "values": {
                "limit_touch_count": touch_count,
                "closed_limit_count": closed_count,
                "eod_failed_limit_count": failed_count,
                "eod_failed_limit_ratio_pct": _pct(failed_count, touch_count),
                "late_failed_count": None,
            },
            "missing_fields": [
                "late_failed_count", "first_touch_time", "reopen_count",
                "last_seal_time",
            ],
            "reason": "仅表示最高价触板但收盘未封；没有分钟过程，不称尾盘炸板。",
        },
        "C": {
            "status": "partial", "as_of": f"{trade_date}T15:00:00+08:00",
            "source_ids": ["v4_baostock_eod"],
            "coverage": {
                "scope": "sh_sz_main_chinext", "scope_complete": True,
                "full_a_share": False,
            },
            "values": {
                "total_amount_yi": round(total_amount / 100_000_000, 3),
                "top_5pct_amount_share_pct": _pct(sum(sorted_amounts[:top_count]), total_amount),
                "turnover_median_pct": round(median(turnovers), 4) if turnovers else None,
                "turnover_p95_pct": round(sorted_turnover[p95_index], 4) if turnovers else None,
                "limit_stock_amount_share_pct": _pct(closed_amount, total_amount),
                "cross_section_return_dispersion_pct": round(pstdev(returns), 4) if len(returns) > 1 else None,
                "rolling_percentile": None,
            },
            "missing_fields": ["rolling_percentile", "sector_amount_share_delta"],
            "reason": "当日横截面可用；训练期滚动拥挤分位尚未冻结。",
        },
    }
    input_manifest = {
        "schema": "market_state_v4_input_manifest.v1",
        "trade_date": trade_date,
        "scope": "sh_sz_main_chinext",
        "raw_bytes_frozen": False,
        "reproducibility": "all_source_paths_and_sha256_bound",
        "files": input_files,
    }
    return dimensions, input_manifest


def build_snapshot(
    *,
    project_root: Path = PROJECT_ROOT,
    data_root: Path = DATA_ROOT,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    if project_root.resolve() != PROJECT_ROOT.resolve():
        raise ValueError("当前构建器只接受实际项目根；测试请验证纯函数和发布合同")
    generated = (generated_at or datetime.now(SH_TZ)).astimezone(SH_TZ)
    if generated.time() < time(16, 0):
        raise RuntimeError("当日日终快照不得在16:00前生成")

    paths = {
        "quality": project_root / "reports" / "v4_daily_quality_audit.json",
        "calendar": project_root / "reports" / "v4_market_calendar.json",
        "master": project_root / "research" / "data" / "stock_basic_baostock.csv",
        "events": project_root / "research" / "data" / "special_price_limit_events.csv",
        "rules": project_root / "research" / "market_rules.py",
        "builder": Path(__file__).resolve(),
        "funds": project_root / "research" / "fund_flow_live.json",
    }
    quality = json.loads(paths["quality"].read_text(encoding="utf-8"))
    calendar_doc = json.loads(paths["calendar"].read_text(encoding="utf-8"))
    calendar = [str(day) for day in calendar_doc.get("trading_dates") or []]
    if not (
        quality.get("all_downloaded_files_passed") is True
        and quality.get("unknown_state_rows") == 0
        and calendar
        and calendar[-1] == str((quality.get("query_window") or {}).get("end_date"))
    ):
        raise RuntimeError("V4日线质量审计、交易日历或结束日期未闭合")
    trade_date = calendar[-1]
    if generated.date().isoformat() < trade_date:
        raise RuntimeError("生成时间早于输入交易日")
    ipo_by_code, special_events = _load_reference_maps(paths["master"], paths["events"])
    dimensions, input_manifest = _scan_v4_scope(
        quality=quality, calendar=calendar, ipo_by_code=ipo_by_code,
        special_events=special_events,
    )
    manifest_evidence = _freeze_bytes(
        _canonical_bytes(input_manifest), ".json", data_root=data_root,
    )
    evidence = {
        key: _freeze_file(path, data_root=data_root)
        for key, path in paths.items() if key != "funds"
    }
    evidence["v4_input_manifest"] = manifest_evidence

    p_dimension = {
        "status": "observation_only", "as_of": None, "source_ids": [],
        "coverage": {},
        "values": {"third_party_main_net_yi": None},
        "missing_fields": ["official_public_capital_pit", "margin_balance_change_pct"],
        "reason": "第三方主力资金只展示，不进入个股评分、买点或仓位。",
    }
    if paths["funds"].exists():
        funds = json.loads(paths["funds"].read_text(encoding="utf-8"))
        if funds.get("date") == trade_date and isinstance(funds.get("stocks"), dict):
            evidence["third_party_fund_observation"] = _freeze_file(
                paths["funds"], data_root=data_root,
            )
            net_values = [
                _number(row.get("net_yi"))
                for row in funds["stocks"].values() if isinstance(row, dict)
            ]
            p_dimension.update({
                "as_of": funds.get("ts"),
                "source_ids": ["ths_third_party_observation"],
                "coverage": {
                    "scope": "baostock_active_reference_only",
                    "expected": funds.get("reference_universe_size"),
                    "returned": funds.get("n"),
                    "reference_coverage_pct": funds.get("reference_coverage_pct"),
                    "exchange_coverage_status": "unknown",
                },
                "values": {
                    "third_party_main_net_yi": round(
                        sum(value for value in net_values if value is not None), 4,
                    ),
                },
            })
    dimensions["P"] = p_dimension
    return {
        "schema": SCHEMA,
        "trade_date": trade_date,
        "generated_at": generated.isoformat(timespec="seconds"),
        "usable_from_trade_date": None,
        "research_only": True,
        "decision_eligible": False,
        "scope": {
            "id": "sh_sz_main_chinext",
            "label": "沪深主板+创业板",
            "scope_complete": True,
            "full_a_share": False,
            "missing_segments": ["star", "bse"],
        },
        "evidence": evidence,
        "dimensions": dimensions,
        "core_evidence_ready_for_scope": all(
            dimensions[key]["status"] == "available" for key in ("B", "L", "F")
        ),
        "state_model_validated": False,
        "stock_score_role": "none",
        "position_control_role": "not_applied_until_oos_validation",
        "limitations": [
            "not_full_a_share",
            "no_intraday_limit_process",
            "no_tradability_or_queue_evidence",
            "third_party_fund_flow_observation_only",
        ],
    }


def publish_snapshot(
    snapshot: dict[str, Any], *, data_root: Path = DATA_ROOT,
) -> tuple[Path, bool]:
    path = data_root / "snapshots" / f"{snapshot['trade_date']}.json"
    created = _atomic_publish(path, _canonical_bytes(snapshot))
    return path, created


def _inside_project(path_text: str, *, project_root: Path) -> Path | None:
    path = (project_root / path_text).resolve()
    try:
        path.relative_to(project_root.resolve())
    except ValueError:
        return None
    return path


def verify_snapshot(
    path: Path, *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        raw = path.read_bytes()
        snapshot = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"snapshot_unreadable:{exc}"], "warnings": []}
    if snapshot.get("schema") != SCHEMA:
        errors.append("schema_invalid")
    if snapshot.get("research_only") is not True or snapshot.get("decision_eligible") is not False:
        errors.append("decision_boundary_invalid")
    if snapshot.get("state_model_validated") is not False:
        errors.append("unvalidated_state_model_must_remain_false")
    if (snapshot.get("scope") or {}).get("full_a_share") is not False:
        errors.append("scope_truth_invalid")
    dimensions = snapshot.get("dimensions") or {}
    for key in ("B", "L", "F", "C", "P"):
        if key not in dimensions:
            errors.append(f"dimension_missing:{key}")
    core_ready = all(
        (dimensions.get(key) or {}).get("status") == "available"
        for key in ("B", "L", "F")
    )
    if snapshot.get("core_evidence_ready_for_scope") is not core_ready:
        errors.append("core_evidence_ready_mismatch")
    b_values = (dimensions.get("B") or {}).get("values") or {}
    counts = [b_values.get(name) for name in ("up_count", "down_count", "flat_count")]
    usable = ((dimensions.get("B") or {}).get("coverage") or {}).get("usable")
    if all(isinstance(value, int) for value in counts) and isinstance(usable, int):
        if sum(counts) != usable:
            errors.append("breadth_count_mismatch")
    f_values = (dimensions.get("F") or {}).get("values") or {}
    touch, closed, failed = (
        f_values.get("limit_touch_count"), f_values.get("closed_limit_count"),
        f_values.get("eod_failed_limit_count"),
    )
    if all(isinstance(value, int) for value in (touch, closed, failed)):
        if failed > touch or closed + failed != touch:
            errors.append("failed_limit_arithmetic_invalid")
    for name, artifact in (snapshot.get("evidence") or {}).items():
        relative = str((artifact or {}).get("path") or "")
        evidence_path = _inside_project(relative, project_root=project_root)
        if evidence_path is None or not evidence_path.is_file() or evidence_path.is_symlink():
            errors.append(f"evidence_missing_or_unsafe:{name}")
            continue
        if _sha256(evidence_path) != artifact.get("sha256"):
            errors.append(f"evidence_sha256_mismatch:{name}")
    if not ((snapshot.get("scope") or {}).get("scope_complete") is True):
        warnings.append("declared_scope_incomplete")
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "snapshot_sha256": hashlib.sha256(raw).hexdigest(),
        "snapshot": snapshot,
    }


def latest_research_state(
    *, data_root: Path = DATA_ROOT, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    paths = sorted((data_root / "snapshots").glob("*.json"), reverse=True)
    if not paths:
        return {
            "schema_version": 1, "state": "UNKNOWN",
            "status": "research_snapshot_missing", "as_of": None,
            "dimensions": {}, "missing_core_dimensions": ["B", "L", "F"],
            "target_exposure_pct": None, "stock_score_role": "none",
            "position_control_role": "not_applied_until_oos_validation",
            "decision_eligible": False,
            "reason": "尚无通过验证的不可变日终市场证据快照。",
        }
    verification = verify_snapshot(paths[0], project_root=project_root)
    if not verification["valid"]:
        return {
            "schema_version": 1, "state": "UNKNOWN",
            "status": "research_snapshot_invalid", "as_of": None,
            "dimensions": {}, "missing_core_dimensions": ["B", "L", "F"],
            "target_exposure_pct": None, "stock_score_role": "none",
            "position_control_role": "not_applied_until_oos_validation",
            "decision_eligible": False,
            "reason": "最新日终快照校验失败，未回退到实时拼接数据。",
            "verification_errors": verification["errors"],
        }
    snapshot = verification["snapshot"]
    return {
        "schema_version": 1,
        "state": "UNKNOWN",
        "status": "research_evidence_ready_model_unvalidated",
        "as_of": f"{snapshot['trade_date']}T15:00:00+08:00",
        "scope": snapshot["scope"],
        "dimensions": snapshot["dimensions"],
        "missing_core_dimensions": [],
        "missing_full_market_segments": snapshot["scope"]["missing_segments"],
        "core_evidence_ready_for_scope": snapshot["core_evidence_ready_for_scope"],
        "state_model_validated": False,
        "target_exposure_pct": None,
        "stock_score_role": "none",
        "position_control_role": "not_applied_until_oos_validation",
        "decision_eligible": False,
        "snapshot_sha256": verification["snapshot_sha256"],
        "reason": (
            "沪深主板与创业板B/L/F日终证据已闭合；科创板、北交所和盘中过程仍缺失，"
            "且状态映射尚未样本外验证，因此保持UNKNOWN，不改变个股评分或账户仓位。"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="日终市场状态证据快照")
    parser.add_argument("command", choices=("publish", "verify"))
    parser.add_argument("--path")
    args = parser.parse_args()
    if args.command == "publish":
        calendar_doc = json.loads(
            (PROJECT_ROOT / "reports" / "v4_market_calendar.json").read_text(
                encoding="utf-8"
            )
        )
        trade_dates = calendar_doc.get("trading_dates") or []
        existing = SNAPSHOT_DIR / f"{trade_dates[-1]}.json" if trade_dates else None
        if existing and existing.exists():
            result = verify_snapshot(existing)
            if not result["valid"]:
                raise RuntimeError(f"已有快照校验失败：{result['errors']}")
            print(json.dumps({
                "path": str(existing), "created": False,
                "trade_date": trade_dates[-1],
                "snapshot_sha256": result["snapshot_sha256"],
                "core_evidence_ready_for_scope": result["snapshot"].get(
                    "core_evidence_ready_for_scope"
                ),
                "decision_eligible": False,
            }, ensure_ascii=False, indent=2))
            return 0
        snapshot = build_snapshot()
        path, created = publish_snapshot(snapshot)
        result = verify_snapshot(path)
        if not result["valid"]:
            raise RuntimeError(f"发布后校验失败：{result['errors']}")
        print(json.dumps({
            "path": str(path), "created": created,
            "trade_date": snapshot["trade_date"],
            "snapshot_sha256": result["snapshot_sha256"],
            "core_evidence_ready_for_scope": snapshot["core_evidence_ready_for_scope"],
            "decision_eligible": False,
        }, ensure_ascii=False, indent=2))
        return 0
    path = Path(args.path) if args.path else sorted(SNAPSHOT_DIR.glob("*.json"))[-1]
    result = verify_snapshot(path)
    print(json.dumps({key: value for key, value in result.items() if key != "snapshot"}, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
