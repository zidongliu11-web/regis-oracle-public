from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from research.download_v4_daily_baostock import (
    BASIC_PATH,
    END_DATE,
    OUTPUT_DIR,
    PROJECT_ROOT,
    REPORT_PATH as DOWNLOAD_MANIFEST_PATH,
    START_DATE,
    eligible_basic_rows,
)
from research.market_rules import daily_price_limits
from research.incremental_v4_daily import MARKET_CALENDAR_PATH


REPORT_PATH = PROJECT_ROOT / "reports" / "v4_daily_quality_audit.json"
SPECIAL_EVENTS_PATH = (
    PROJECT_ROOT / "research" / "data" / "special_price_limit_events.csv"
)
REQUIRED_COLUMNS = {
    "date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "turn",
    "tradestatus",
    "pctChg",
    "isST",
}
NUMERIC_COLUMNS = [
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "turn",
    "pctChg",
]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _atomic_json_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def _basic_map() -> dict[str, dict]:
    frame = pd.read_csv(BASIC_PATH, dtype=str).fillna("")
    return {
        row["code"]: {
            "ipo_date": pd.to_datetime(row["ipo_date"], errors="coerce"),
            "out_date": pd.to_datetime(
                row["out_date"] or pd.NA, errors="coerce"
            ),
        }
        for _, row in frame.iterrows()
    }


def _special_event_map() -> dict[tuple[str, str], str]:
    from mom_trading.cloud_price_limit_events import load_events
    if not SPECIAL_EVENTS_PATH.exists():
        return load_events(PROJECT_ROOT)[0]
    frame = pd.read_csv(SPECIAL_EVENTS_PATH, dtype=str).fillna("")
    events = {
        (row["code"], row["date"]): row["event_type"]
        for _, row in frame.iterrows()
        if row["verification_status"] == "verified"
    }
    return load_events(PROJECT_ROOT, events)[0]


def _query_window(
    manifest_path: Path = DOWNLOAD_MANIFEST_PATH,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Use the published incremental data window, not frozen defaults."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        manifest = {}
    query = manifest.get("query") or {}
    start = pd.to_datetime(
        query.get("start_date") or START_DATE,
        errors="coerce",
    )
    end = pd.to_datetime(
        query.get("end_date") or END_DATE,
        errors="coerce",
    )
    if pd.isna(start) or pd.isna(end):
        raise RuntimeError("下载清单中的查询区间无法解析")
    if end < start:
        raise RuntimeError("下载清单中的结束日期早于开始日期")
    return pd.Timestamp(start), pd.Timestamp(end)


def _official_market_dates(
    path: Path = MARKET_CALENDAR_PATH,
) -> set[pd.Timestamp]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("缺少可解析的BaoStock官方交易日历") from exc
    dates = pd.to_datetime(
        payload.get("trading_dates") or [], errors="coerce"
    )
    valid = {pd.Timestamp(day) for day in dates if pd.notna(day)}
    if not valid:
        raise RuntimeError("BaoStock官方交易日历没有有效交易日")
    return valid


def _audit_file(
    path: Path,
    basic: dict[str, dict],
    special_events: dict[tuple[str, str], str],
    *,
    query_start: pd.Timestamp,
    query_end: pd.Timestamp,
    official_market_dates: set[pd.Timestamp] | None = None,
) -> tuple[dict, dict]:
    expected_code = f"sh.{path.name[:6]}" if path.name.startswith("6") else (
        f"sz.{path.name[:6]}"
    )
    errors: list[str] = []
    frame = pd.read_csv(path)
    missing_columns = sorted(REQUIRED_COLUMNS - set(frame.columns))
    if missing_columns:
        return ({
            "file": _display_path(path),
            "code": expected_code,
            "rows": len(frame),
            "status": "fail",
            "errors": [f"缺少字段:{','.join(missing_columns)}"],
        }, {})

    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in NUMERIC_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["tradestatus"] = pd.to_numeric(
        frame["tradestatus"], errors="coerce"
    )
    frame["isST"] = pd.to_numeric(frame["isST"], errors="coerce")

    invalid_dates = int(frame["date"].isna().sum())
    duplicate_dates = int(frame["date"].duplicated().sum())
    code_mismatches = int((frame["code"] != expected_code).sum())
    invalid_status = int(
        (~frame["tradestatus"].isin([0, 1])).sum()
    )
    invalid_st = int((~frame["isST"].isin([0, 1])).sum())
    active = frame["tradestatus"] == 1
    active_numeric_nulls = int(
        frame.loc[active, NUMERIC_COLUMNS].isna().sum().sum()
    )
    suspended_numeric_nulls = int(
        frame.loc[~active, NUMERIC_COLUMNS].isna().sum().sum()
    )
    negative_volume_amount = int(
        ((frame["volume"] < 0) | (frame["amount"] < 0)).sum()
    )
    invalid_active_prices = int(
        (
            active
            & (
                (frame["open"] <= 0)
                | (frame["high"] <= 0)
                | (frame["low"] <= 0)
                | (frame["close"] <= 0)
                | (frame["preclose"] <= 0)
            )
        ).sum()
    )
    invalid_ohlc = int(
        (
            active
            & (
                (frame["high"] < frame[["open", "close", "low"]].max(axis=1))
                | (frame["low"] > frame[["open", "close", "high"]].min(axis=1))
            )
        ).sum()
    )
    calculated_pct = (frame["close"] / frame["preclose"] - 1) * 100
    pct_diff = (calculated_pct - frame["pctChg"]).abs()
    pct_evaluable = (
        active
        & frame[["close", "preclose", "pctChg"]].notna().all(axis=1)
        & frame["close"].gt(0)
        & frame["preclose"].gt(0)
    )
    pct_mismatches = int((pct_evaluable & (pct_diff > 0.02)).sum())
    pct_passed = int((pct_evaluable & (pct_diff <= 0.02)).sum())
    pct_missing = int((active & ~pct_evaluable).sum())
    unknown_state_rows = int((
        ~frame["tradestatus"].isin([0, 1])
        | ~frame["isST"].isin([0, 1])
    ).sum())

    info = basic.get(expected_code)
    outside_listing = 0
    missing_market_dates: list[str] = []
    if info is None:
        errors.append("证券基础清单不存在")
    else:
        if pd.notna(info["ipo_date"]):
            outside_listing += int((frame["date"] < info["ipo_date"]).sum())
        if pd.notna(info["out_date"]):
            outside_listing += int((frame["date"] > info["out_date"]).sum())
        if official_market_dates is not None:
            listing_start = max(
                query_start,
                info["ipo_date"] if pd.notna(info["ipo_date"]) else query_start,
            )
            listing_end = min(
                query_end,
                info["out_date"] if pd.notna(info["out_date"]) else query_end,
            )
            # BaoStock basic.out_date can be the legal delisting date one or
            # more market sessions after the final tradable row.  For a closed
            # listing, require continuity through the last physical row; do
            # not fabricate a bar on a non-trading legal exit date.
            if pd.notna(info["out_date"]) and frame["date"].notna().any():
                listing_end = min(listing_end, frame["date"].max())
            expected = {
                day for day in official_market_dates
                if listing_start <= day <= listing_end
            }
            actual = {
                pd.Timestamp(day) for day in frame["date"].dropna()
                if query_start <= pd.Timestamp(day) <= query_end
            }
            missing_market_dates = sorted(
                day.date().isoformat() for day in expected - actual
            )

    outside_query = int(
        ((frame["date"] < query_start) | (frame["date"] > query_end)).sum()
    )
    if invalid_dates:
        errors.append(f"非法日期:{invalid_dates}")
    if duplicate_dates:
        errors.append(f"重复日期:{duplicate_dates}")
    if code_mismatches:
        errors.append(f"证券代码不一致:{code_mismatches}")
    if invalid_status:
        errors.append(f"非法交易状态:{invalid_status}")
    if invalid_st:
        errors.append(f"非法ST状态:{invalid_st}")
    if active_numeric_nulls:
        errors.append(f"交易日数值缺失:{active_numeric_nulls}")
    if negative_volume_amount:
        errors.append(f"负成交量/额:{negative_volume_amount}")
    if invalid_active_prices:
        errors.append(f"交易日非正价格:{invalid_active_prices}")
    if invalid_ohlc:
        errors.append(f"OHLC几何错误:{invalid_ohlc}")
    if pct_mismatches:
        errors.append(f"涨跌幅复算偏差:{pct_mismatches}")
    if outside_listing:
        errors.append(f"上市区间外记录:{outside_listing}")
    if outside_query:
        errors.append(f"查询区间外记录:{outside_query}")
    if missing_market_dates:
        errors.append(f"缺失官方交易日:{len(missing_market_dates)}")
    if not frame["date"].is_monotonic_increasing:
        errors.append("日期未升序")

    special_rule_rows = 0
    verified_event_rows = 0
    ordinary_limit_violations = 0
    limit_violation_rows = []
    factor_eligible_rows = 0
    for position, row in enumerate(frame.itertuples(index=False), start=1):
        if (
            pd.isna(row.date)
            or row.tradestatus != 1
            or pd.isna(row.preclose)
            or row.preclose <= 0
        ):
            continue
        event_type = special_events.get(
            (expected_code, row.date.date().isoformat())
        )
        limits = daily_price_limits(
            expected_code,
            row.date.date(),
            float(row.preclose),
            is_st=bool(row.isST),
            listed_trading_days=position,
            special_event=event_type,
        )
        if limits["ratio"] is None:
            special_rule_rows += 1
            if event_type:
                verified_event_rows += 1
            continue
        if (
            row.high > limits["up_limit"] + 0.011
            or row.low < limits["down_limit"] - 0.011
        ):
            ordinary_limit_violations += 1
            limit_violation_rows.append({
                "code": expected_code,
                "date": row.date.date().isoformat(),
                "preclose": round(float(row.preclose), 4),
                "high": round(float(row.high), 4),
                "low": round(float(row.low), 4),
                "calculated_up_limit": limits["up_limit"],
                "calculated_down_limit": limits["down_limit"],
                "is_st": bool(row.isST),
                "listed_trading_days": position,
                "review_status": "unverified_special_event",
            })
        if position >= 120 and not bool(row.isST):
            factor_eligible_rows += 1

    result = {
        "file": _display_path(path),
        "code": expected_code,
        "rows": len(frame),
        "start": (
            frame["date"].min().date().isoformat()
            if not frame.empty and frame["date"].notna().any()
            else None
        ),
        "end": (
            frame["date"].max().date().isoformat()
            if not frame.empty and frame["date"].notna().any()
            else None
        ),
        "active_rows": int(active.sum()),
        "suspended_rows": int((frame["tradestatus"] == 0).sum()),
        "st_rows": int((frame["isST"] == 1).sum()),
        "factor_eligible_rows": factor_eligible_rows,
        "special_rule_rows": special_rule_rows,
        "verified_event_rows": verified_event_rows,
        "ordinary_limit_violations": ordinary_limit_violations,
        "limit_violation_rows": limit_violation_rows,
        "suspended_numeric_nulls": suspended_numeric_nulls,
        "missing_market_date_count": len(missing_market_dates),
        "missing_market_dates_sample": missing_market_dates[:20],
        "pctchg_preclose_identity": {
            "expected_evaluable_rows": int(active.sum()),
            "evaluable_rows": int(pct_evaluable.sum()),
            "passed_rows": pct_passed,
            "missing_required_input_rows": pct_missing,
            "mismatch_rows_above_0_02pp": pct_mismatches,
            "unknown_state_rows": unknown_state_rows,
        },
        "status": "pass" if not errors else "fail",
        "errors": errors,
    }
    totals = {
        "rows": len(frame),
        "active_rows": int(active.sum()),
        "suspended_rows": int((frame["tradestatus"] == 0).sum()),
        "st_rows": int((frame["isST"] == 1).sum()),
        "factor_eligible_rows": factor_eligible_rows,
        "special_rule_rows": special_rule_rows,
        "verified_event_rows": verified_event_rows,
        "ordinary_limit_violations": ordinary_limit_violations,
        "suspended_numeric_nulls": suspended_numeric_nulls,
        "missing_market_dates": len(missing_market_dates),
        "pctchg_identity_expected_rows": int(active.sum()),
        "pctchg_identity_evaluable_rows": int(pct_evaluable.sum()),
        "pctchg_identity_passed_rows": pct_passed,
        "pctchg_identity_missing_required_input_rows": pct_missing,
        "pctchg_identity_mismatch_rows": pct_mismatches,
        "unknown_state_rows": unknown_state_rows,
    }
    return result, totals


def _research_readiness(
    *,
    file_count: int,
    target_symbols: int,
    failed_files: int,
    unresolved_events: int,
    missing_market_dates: int = 0,
) -> tuple[bool, str]:
    if file_count < target_symbols:
        return (
            False,
            f"全量历史尚未完成：{file_count}/{target_symbols}只",
        )
    if failed_files:
        return False, f"仍有{failed_files}个文件未通过结构质量审计"
    if missing_market_dates:
        return False, f"仍有{missing_market_dates}个股票交易日缺失"
    if unresolved_events:
        return (
            False,
            f"仍有{unresolved_events}条特殊价格事件待官方公告核验",
        )
    return (
        True,
        "全量历史、文件结构和特殊价格事件审计均已通过；允许进入正式因子研究",
    )


def resolved_cloud_files(manifest: dict) -> list[dict]:
    """Use the same verified revisions as emotion/features, without rewriting sources."""
    from mom_trading.cloud_daily_revisions import load_files

    entries = manifest["files"]
    indexed = {row["code"]: row for row in entries}
    if len(indexed) != len(entries):
        raise RuntimeError("版本化审计证券清单重复")
    resolved = load_files(PROJECT_ROOT, indexed, manifest["query"]["end_date"],
                          now=datetime.now(timezone.utc))
    if set(resolved) != set(indexed):
        raise RuntimeError("版本化审计修订改变了证券范围")
    return [resolved[row["code"]] for row in entries]


def _versioned_input_bindings(paths: list[Path], *, resolve_revisions=False) -> dict:
    manifest_bytes = DOWNLOAD_MANIFEST_PATH.read_bytes()
    manifest = json.loads(manifest_bytes)
    entries = resolved_cloud_files(manifest) if resolve_revisions else manifest["files"]
    expected = [PROJECT_ROOT / row["path"] for row in entries]
    if (paths != sorted(expected) or len(set(expected)) != len(expected)
            or len({row["code"] for row in entries}) != len(entries)):
        raise RuntimeError("版本化审计路径与完整发布清单不一致")
    master_sha = _sha256(BASIC_PATH)
    if (manifest.get("universe_security_master") or {}).get("sha256") != master_sha:
        raise RuntimeError("版本化审计证券主表与发布清单不一致")
    for row, path in zip(entries, expected):
        if (path.is_symlink() or path.parent.resolve() != OUTPUT_DIR.resolve()
                or path.name[:6] != row["code"] or _sha256(path) != row["sha256"]):
            raise RuntimeError("版本化审计日线原件指纹不一致")
    revision_index = PROJECT_ROOT / "reports/cloud_daily_revisions" / manifest["query"]["end_date"] / "index.json"
    return {"download_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "security_master_sha256": master_sha,
            "market_calendar_sha256": _sha256(MARKET_CALENDAR_PATH),
            "revision_index_sha256": _sha256(revision_index) if resolve_revisions and revision_index.exists() else None,
            "effective_files_sha256": hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "revised_files": sum("revision" in row for row in entries),
            "special_events_sha256": _sha256(SPECIAL_EVENTS_PATH) if SPECIAL_EVENTS_PATH.exists() else None}


def main(*, input_paths: list[Path] | None = None, resolve_revisions=False) -> None:
    from mom_trading.cloud_price_limit_events import load_events
    if resolve_revisions and input_paths is None:
        raise RuntimeError("修订版审计必须提供完整显式来源路径")
    paths = sorted(OUTPUT_DIR.glob("*.csv.gz") if input_paths is None else input_paths)
    if not paths:
        raise RuntimeError("尚无 V4 日线文件可审计")
    versioned_bindings = _versioned_input_bindings(paths, resolve_revisions=resolve_revisions) if input_paths is not None else None
    basic = _basic_map()
    event_identity = load_events(PROJECT_ROOT)[1]
    special_events = _special_event_map()
    query_start, query_end = _query_window()
    target_symbols = len(eligible_basic_rows(
        end_date=query_end.date().isoformat(),
    ))
    official_market_dates = _official_market_dates()
    files = []
    totals = {
        "rows": 0,
        "active_rows": 0,
        "suspended_rows": 0,
        "st_rows": 0,
        "factor_eligible_rows": 0,
        "special_rule_rows": 0,
        "verified_event_rows": 0,
        "ordinary_limit_violations": 0,
        "suspended_numeric_nulls": 0,
        "missing_market_dates": 0,
        "pctchg_identity_expected_rows": 0,
        "pctchg_identity_evaluable_rows": 0,
        "pctchg_identity_passed_rows": 0,
        "pctchg_identity_missing_required_input_rows": 0,
        "pctchg_identity_mismatch_rows": 0,
        "unknown_state_rows": 0,
    }
    for path in paths:
        item, item_totals = _audit_file(
            path,
            basic,
            special_events,
            query_start=query_start,
            query_end=query_end,
            official_market_dates=official_market_dates,
        )
        files.append(item)
        for key in totals:
            totals[key] += item_totals.get(key, 0)

    failures = [item for item in files if item["status"] != "pass"]
    if versioned_bindings is not None and _versioned_input_bindings(paths, resolve_revisions=resolve_revisions) != versioned_bindings:
        raise RuntimeError("版本化审计期间来源发生变化，不发布混合批次")
    if load_events(PROJECT_ROOT)[1] != event_identity:
        raise RuntimeError("审计期间制度公告来源发生变化，不发布混合批次")
    unresolved_limit_violation_rows = [
        row
        for item in files
        for row in item.get("limit_violation_rows", [])
    ]
    ready_for_factor_research, ready_reason = _research_readiness(
        file_count=len(files),
        target_symbols=target_symbols,
        failed_files=len(failures),
        unresolved_events=totals["ordinary_limit_violations"],
        missing_market_dates=totals["missing_market_dates"],
    )
    report = {
        "generated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "scope": "已下载的 V4 BaoStock 不复权日线",
        "query_window": {
            "start_date": query_start.date().isoformat(),
            "end_date": query_end.date().isoformat(),
            "source": str(
                DOWNLOAD_MANIFEST_PATH.relative_to(PROJECT_ROOT)
            ),
        },
        "input_fingerprints": {
            **(versioned_bindings or {}),
            "cloud_price_events_sha256": event_identity,
            "download_manifest_sha256": _sha256(
                DOWNLOAD_MANIFEST_PATH
            ),
            "security_master_sha256": _sha256(BASIC_PATH),
        },
        "file_count": len(files),
        "target_symbols": target_symbols,
        "full_universe_ready": len(files) == target_symbols,
        "passed_files": len(files) - len(failures),
        "failed_files": len(failures),
        **totals,
        "all_downloaded_files_passed": not failures,
        "pctchg_preclose_identity_population": {
            "scope": "all downloaded V4 rows; not a sample",
            "total_rows": totals["rows"],
            "expected_evaluable_rows": totals[
                "pctchg_identity_expected_rows"
            ],
            "evaluable_rows": totals["pctchg_identity_evaluable_rows"],
            "passed_rows": totals["pctchg_identity_passed_rows"],
            "missing_required_input_rows": totals[
                "pctchg_identity_missing_required_input_rows"
            ],
            "mismatch_rows_above_0_02pp": totals[
                "pctchg_identity_mismatch_rows"
            ],
            "unknown_state_rows": totals["unknown_state_rows"],
            "tolerance_percentage_points": 0.02,
            "passed": bool(
                totals["pctchg_identity_expected_rows"]
                == totals["pctchg_identity_evaluable_rows"]
                == totals["pctchg_identity_passed_rows"]
                and totals["pctchg_identity_missing_required_input_rows"] == 0
                and totals["pctchg_identity_mismatch_rows"] == 0
                and totals["unknown_state_rows"] == 0
            ),
        },
        "market_rule_event_review_ready": (
            totals["ordinary_limit_violations"] == 0
        ),
        "unresolved_limit_violation_rows": unresolved_limit_violation_rows,
        "ready_for_factor_research": ready_for_factor_research,
        "ready_reason": ready_reason,
        "checks": [
            "字段、日期、代码、唯一性和升序",
            "OHLC几何、正价格、非负成交量/额",
            "pctChg按close/preclose复算，容差0.02个百分点",
            "tradestatus/isST取值域",
            "逐股票日期集合与BaoStock官方交易日历按上市/退市区间完全对齐",
            "IPO/退市日期与查询区间",
            "按历史制度日期计算普通涨跌停边界",
            "显式事件表覆盖退市整理期首日等无普通涨跌停日期",
            "停牌日成交量/额/换手率/pctChg空值视为合法但单独计数",
        ],
        "limitations": [
            "普通涨跌停边界通过日线检查，不代表涨跌停排队时可成交",
            "超出普通涨跌停的记录须结合退市整理、复牌或公司行动逐条分类",
            "BaoStock历史名称与退市整理标签仍需额外来源核验",
            "只有全量下载并通过后才允许计算正式横截面因子",
            "文件最大日期新鲜不代表中间日期连续；任何内部缺口都会阻断因子门禁",
        ],
        "special_event_source": str(
            SPECIAL_EVENTS_PATH.relative_to(PROJECT_ROOT)
        ),
        "failures": failures,
        "files": files,
    }
    _atomic_json_write(REPORT_PATH, report)
    print(json.dumps({
        "report": str(REPORT_PATH.relative_to(PROJECT_ROOT)),
        "files": report["file_count"],
        "passed_files": report["passed_files"],
        "failed_files": report["failed_files"],
        "rows": report["rows"],
        "ordinary_limit_violations": report[
            "ordinary_limit_violations"
        ],
        "ready_for_factor_research": report[
            "ready_for_factor_research"
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
