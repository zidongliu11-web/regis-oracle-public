from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import baostock as bs
import pandas as pd

from research.download_v4_daily_baostock import (
    BASIC_PATH,
    FIELDS,
    OUTPUT_DIR,
    PROJECT_ROOT,
    REPORT_PATH,
    eligible_basic_rows,
    existing_files,
    sha256,
)


UPDATE_REPORT_PATH = (
    PROJECT_ROOT / "reports" / "v4_daily_incremental_update.json"
)
PROGRESS_REPORT_PATH = (
    PROJECT_ROOT / "reports" / "v4_daily_incremental_progress.json"
)
LOCK_PATH = PROJECT_ROOT / "instance" / "v4_daily_incremental.lock"
MARKET_CALENDAR_PATH = PROJECT_ROOT / "reports" / "v4_market_calendar.json"
UPSTREAM_PROBE_CODES = ("sh.600000", "sz.000001", "sh.600519")
SAME_DAY_DATA_SAFE_HOUR = 18
AUTH_EXPIRED_CODE = "10001001"
FAILURE_STATES = {
    "failed", "missing_base_file", "invalid_base_file",
    "source_missing_expected_dates", "source_missing_active_snapshot",
}


class BaoStockSessionRecoveryError(RuntimeError):
    pass


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def build_progress_snapshot(
    *,
    started_at: str,
    started_monotonic: float,
    end_date: str,
    total_symbols: int,
    results: list[dict[str, Any]],
    current_code: str | None,
    status: str = "running",
) -> dict[str, Any]:
    elapsed = max(0.0, time.monotonic() - started_monotonic)
    processed = len(results)
    rate = processed / elapsed if elapsed > 0 else 0.0
    remaining_symbols = max(0, total_symbols - processed)
    eta = (
        int(round(remaining_symbols / rate))
        if rate > 0 and status == "running"
        else 0 if status == "completed" else None
    )
    counts: dict[str, int] = {}
    for item in results:
        item_status = str(item.get("status") or "unknown")
        counts[item_status] = counts.get(item_status, 0) + 1
    return {
        "status": status,
        "started_at": started_at,
        "heartbeat_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "requested_end_date": end_date,
        "total_symbols": total_symbols,
        "processed_symbols": processed,
        "remaining_symbols": remaining_symbols,
        "progress_pct": round(
            processed / total_symbols * 100, 2
        ) if total_symbols else 100.0,
        "current_code": current_code,
        "last_completed_code": (
            results[-1].get("code") if results else None
        ),
        "status_counts": counts,
        "updated_symbols": sum(
            count for key, count in counts.items()
            if key.startswith("updated") or key == "bootstrapped_new_listing"
        ),
        "rows_added": sum(
            int(item.get("rows_added") or 0) for item in results
        ),
        "failed_symbols": sum(
            counts.get(key, 0)
            for key in {
                "failed", "missing_base_file", "invalid_base_file",
                "source_missing_expected_dates", "source_missing_active_snapshot",
            }
        ),
        "elapsed_seconds": int(round(elapsed)),
        "rate_symbols_per_second": round(rate, 3),
        "eta_seconds": eta,
        "progress_source": "processed_symbol_count",
    }


def _load_manifest() -> dict[str, Any]:
    try:
        value = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _query_range(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    result = bs.query_history_k_data_plus(
        code,
        FIELDS,
        start_date=start_date,
        end_date=end_date,
        frequency="d",
        adjustflag="3",
    )
    if result.error_code != "0":
        raise RuntimeError(f"{result.error_code} {result.error_msg}")
    rows = []
    while result.next():
        rows.append(result.get_row_data())
    return pd.DataFrame(rows, columns=result.fields)


def run_with_session_recovery(operation: Any) -> Any:
    """Retry one request after an expired BaoStock session, then fail fast."""
    try:
        return operation()
    except RuntimeError as exc:
        if not str(exc).startswith(AUTH_EXPIRED_CODE):
            raise
    bs.logout()
    login = bs.login()
    if getattr(login, "error_code", "") != "0":
        raise BaoStockSessionRecoveryError(
            f"BaoStock重新登录失败：{getattr(login, 'error_code', '')} "
            f"{getattr(login, 'error_msg', '')}"
        )
    try:
        return operation()
    except RuntimeError as exc:
        if str(exc).startswith(AUTH_EXPIRED_CODE):
            raise BaoStockSessionRecoveryError(
                "BaoStock重新登录后会话仍失效；立即停止本轮"
            ) from exc
        raise


def resume_failed_results(
    report: dict[str, Any], *, full_codes: list[str], end_date: str,
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Validate one complete prior report and return successes plus retry codes."""
    rows = report.get("results")
    if report.get("requested_end_date") != end_date or not isinstance(rows, list):
        raise RuntimeError("失败续传报告与本次结束日期不一致或缺少结果")
    by_code = {
        str(row.get("code")): row for row in rows
        if isinstance(row, dict) and row.get("code")
    }
    if len(by_code) != len(rows) or set(by_code) != set(full_codes):
        raise RuntimeError("失败续传报告不是当前完整证券全集或包含重复代码")
    retry_codes = {
        code for code, row in by_code.items()
        if row.get("status") in FAILURE_STATES
    }
    if not retry_codes:
        raise RuntimeError("失败续传报告没有可重试标的")
    return {
        code: row for code, row in by_code.items() if code not in retry_codes
    }, retry_codes


def query_official_market_dates(start_date: str, end_date: str) -> list[str]:
    result = bs.query_trade_dates(
        start_date=start_date,
        end_date=end_date,
    )
    if result.error_code != "0":
        raise RuntimeError(f"{result.error_code} {result.error_msg}")
    rows = []
    while result.next():
        rows.append(result.get_row_data())
    frame = pd.DataFrame(rows, columns=result.fields)
    if frame.empty or not {"calendar_date", "is_trading_day"}.issubset(frame.columns):
        raise RuntimeError("BaoStock官方交易日历为空或缺少字段")
    return frame.loc[
        frame["is_trading_day"].astype(str).eq("1"),
        "calendar_date",
    ].astype(str).drop_duplicates().sort_values().tolist()


def require_upstream_market_date(
    official_market_dates: list[str],
    *,
    published_end: str,
    query_range: Any = _query_range,
    probe_codes: tuple[str, ...] = UPSTREAM_PROBE_CODES,
) -> str:
    """Fail fast when the calendar leads the upstream daily bars."""
    if not official_market_dates:
        raise RuntimeError("BaoStock官方交易日历为空")
    target = official_market_dates[-1]
    if target <= published_end:
        return target
    errors: list[str] = []
    for code in probe_codes:
        try:
            frame = query_range(code, target, target)
        except Exception as exc:
            errors.append(f"{code}:{exc}")
            continue
        if not frame.empty and "date" in frame:
            dates = set(frame["date"].astype(str))
            if target in dates:
                return target
    detail = f"；探针错误 {len(errors)}/{len(probe_codes)}"
    raise RuntimeError(
        f"BaoStock交易日历已到{target}，但个股日线尚未发布；"
        f"保留已发布截止日{published_end}{detail}"
    )


def publish_market_calendar(
    official_market_dates: list[str],
    *,
    published_end: str,
    generated_at: str | None = None,
    path: Path = MARKET_CALENDAR_PATH,
) -> dict[str, Any]:
    """Publish only dates backed by the completed full-universe update."""
    dates = sorted({
        str(day) for day in official_market_dates
        if str(day) <= published_end
    })
    if not dates or dates[-1] != published_end:
        raise RuntimeError("实际行情截止日不在BaoStock官方交易日历中")
    payload = {
        "generated_at": generated_at or datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "source": "BaoStock query_trade_dates",
        "start_date": dates[0],
        "end_date": dates[-1],
        "trading_dates": dates,
    }
    _atomic_json_write(path, payload)
    return payload


def merge_incremental_rows(
    existing: pd.DataFrame,
    incremental: pd.DataFrame,
) -> pd.DataFrame:
    if incremental.empty:
        return existing
    combined = pd.concat([existing, incremental], ignore_index=True)
    combined = combined.drop_duplicates(subset=["date"], keep="last")
    return combined.sort_values("date").reset_index(drop=True)


def update_one(
    code: str,
    *,
    end_date: str,
    expected_market_dates: list[str],
    listing_start: str,
    listing_end: str,
    allow_trailing_missing: bool = False,
) -> dict[str, Any]:
    normalized = code.split(".", 1)[1]
    path = OUTPUT_DIR / f"{normalized}.csv.gz"
    if not path.exists():
        initial = _query_range(code, listing_start, end_date)
        if initial.empty or "date" not in initial:
            return {"code": code, "status": "source_missing_expected_dates"}
        initial_dates = set(initial["date"].astype(str))
        expected = [
            day for day in expected_market_dates
            if listing_start <= day <= listing_end
        ]
        missing = sorted(set(expected) - initial_dates)
        if missing:
            return {
                "code": code,
                "status": "source_missing_expected_dates",
                "missing_dates": missing[:20],
                "missing_date_count": len(missing),
            }
        temporary = path.with_name(f"{path.name}.incremental.part")
        temporary.unlink(missing_ok=True)
        initial.sort_values("date").to_csv(
            temporary, index=False, compression="gzip",
        )
        temporary.replace(path)
        return {
            "code": code,
            "status": "bootstrapped_new_listing",
            "rows_added": len(initial),
            "previous_end": None,
            "end": str(initial["date"].astype(str).max()),
        }
    existing = pd.read_csv(path, dtype=str).fillna("")
    if existing.empty or "date" not in existing:
        return {"code": code, "status": "invalid_base_file"}
    existing_dates = set(existing["date"].astype(str))
    expected = [
        day for day in expected_market_dates
        if listing_start <= day <= listing_end
    ]
    missing_before = sorted(set(expected) - existing_dates)
    last_date = str(existing["date"].astype(str).max())
    if not missing_before:
        return {
            "code": code,
            "status": "already_current",
            "last_date": last_date,
            "expected_trading_dates": len(expected),
        }
    start = missing_before[0]
    incremental = _query_range(code, start, end_date)
    if incremental.empty:
        if allow_trailing_missing and all(day > last_date for day in missing_before):
            return {
                "code": code,
                "status": "already_current_retired",
                "last_date": last_date,
                "metadata_dates_after_last_trade": missing_before[:20],
            }
        return {
            "code": code,
            "status": "source_missing_expected_dates",
            "last_date": last_date,
            "missing_dates": missing_before[:20],
        }
    merged = merge_incremental_rows(existing, incremental)
    remaining = sorted(set(expected) - set(merged["date"].astype(str)))
    if allow_trailing_missing and remaining:
        merged_last = str(merged["date"].astype(str).max())
        remaining = [day for day in remaining if day <= merged_last]
    if remaining:
        return {
            "code": code,
            "status": "source_missing_expected_dates",
            "last_date": last_date,
            "missing_dates": remaining[:20],
            "missing_date_count": len(remaining),
        }
    temporary = path.with_name(f"{path.name}.incremental.part")
    temporary.unlink(missing_ok=True)
    merged.to_csv(temporary, index=False, compression="gzip")
    temporary.replace(path)
    return {
        "code": code,
        "status": "updated",
        "rows_added": len(merged) - len(existing),
        "previous_end": last_date,
        "end": str(merged.iloc[-1]["date"]),
        "missing_dates_filled": len(missing_before),
    }


def _default_end_date() -> str:
    # BaoStock can publish the calendar before same-day stock bars.  Delay the
    # same-day request and still probe availability before a full-market run.
    local_now = datetime.now().astimezone()
    if local_now.hour >= SAME_DAY_DATA_SAFE_HOUR:
        return local_now.date().isoformat()
    return (local_now.date() - timedelta(days=1)).isoformat()


def reference_market_end() -> str:
    """Return the actual latest market session, not the requested date."""
    reference = OUTPUT_DIR / "000001.csv.gz"
    if not reference.exists():
        raise RuntimeError("缺少000001参考日线，不能发布增量截止日期")
    frame = pd.read_csv(reference, usecols=["date"], dtype=str)
    if frame.empty:
        raise RuntimeError("000001参考日线为空，不能发布增量截止日期")
    return str(frame["date"].max())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="在已审计V4日线之上增量追加新交易日"
    )
    parser.add_argument("--end-date", default=_default_end_date())
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="本轮最多检查多少只；0表示完整检查4,802只固定历史股票",
    )
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument(
        "--resume-failures",
        action="store_true",
        help="仅续传同结束日期完整报告中的失败标的，并复用已成功结果",
    )
    return parser.parse_args()


def shard_artifact_paths(
    *, shard_count: int, shard_index: int,
) -> tuple[Path, Path, Path]:
    if shard_count == 1:
        return UPDATE_REPORT_PATH, PROGRESS_REPORT_PATH, LOCK_PATH
    suffix = f"shard_{shard_index:03d}_of_{shard_count:03d}"
    return (
        UPDATE_REPORT_PATH.with_name(
            f"{UPDATE_REPORT_PATH.stem}_{suffix}{UPDATE_REPORT_PATH.suffix}"
        ),
        PROGRESS_REPORT_PATH.with_name(
            f"{PROGRESS_REPORT_PATH.stem}_{suffix}{PROGRESS_REPORT_PATH.suffix}"
        ),
        LOCK_PATH.with_name(f"{LOCK_PATH.stem}_{suffix}{LOCK_PATH.suffix}"),
    )


SINA_PROBE_CODES = ("sh.600000", "sz.000001", "sh.600519")


def confirm_sina_snapshot(
    snapshot: pd.DataFrame,
    *,
    target_date: str,
    daily_fetcher: Any,
    probe_codes: tuple[str, ...] = SINA_PROBE_CODES,
) -> pd.DataFrame:
    """Confirm a date-less Sina market snapshot against dated daily probes."""
    required = {
        "代码", "名称", "最新价", "昨收", "今开", "最高", "最低",
        "成交量", "成交额", "时间戳",
    }
    missing = required - set(snapshot.columns)
    if missing:
        raise RuntimeError(f"新浪全市场快照缺列：{sorted(missing)}")
    frame = snapshot.copy()
    frame["normalized_code"] = frame["代码"].astype(str).str.extract(
        r"(\d{6})$", expand=False,
    )
    frame = frame.dropna(subset=["normalized_code"]).drop_duplicates(
        "normalized_code", keep="last",
    ).set_index("normalized_code", drop=False)
    for code in probe_codes:
        normalized = code.split(".", 1)[1]
        prefix = code.replace(".", "")[:2] + normalized
        daily = daily_fetcher(prefix, target_date)
        if daily is None or daily.empty:
            raise RuntimeError(f"新浪日期探针{code}没有{target_date}日线")
        dates = pd.to_datetime(daily["date"], errors="raise").dt.strftime(
            "%Y-%m-%d"
        )
        rows = daily.loc[dates.eq(target_date)]
        if len(rows) != 1 or normalized not in frame.index:
            raise RuntimeError(f"新浪日期探针{code}无法确认{target_date}")
        spot = frame.loc[normalized]
        row = rows.iloc[0]
        comparisons = {
            "open": "今开", "high": "最高", "low": "最低",
            "close": "最新价", "volume": "成交量", "amount": "成交额",
        }
        for daily_name, spot_name in comparisons.items():
            left = float(row[daily_name])
            right = float(spot[spot_name])
            if not math.isclose(left, right, rel_tol=1e-8, abs_tol=1e-6):
                raise RuntimeError(
                    f"新浪日期探针{code}的{daily_name}与全市场快照不一致"
                )
        if str(spot["时间戳"])[:5] < "15:00":
            raise RuntimeError(f"新浪全市场快照{code}不是收盘后快照")
    return frame


def _sina_incremental_row(
    existing: pd.DataFrame,
    *,
    code: str,
    target_date: str,
    spot: pd.Series | None,
    confirmed_no_trade: bool = False,
) -> dict[str, Any]:
    existing = existing.sort_values("date").reset_index(drop=True)
    last = existing.iloc[-1]
    if spot is None:
        if not confirmed_no_trade and str(last.get("tradestatus") or "") != "0":
            raise RuntimeError(f"新浪快照缺少仍在交易的股票{code}")
        close = float(last["close"])
        return {
            "date": target_date, "code": code,
            "open": close, "high": close, "low": close, "close": close,
            "preclose": close, "volume": "", "amount": "",
            "adjustflag": "3", "turn": "", "tradestatus": "0",
            "pctChg": "", "isST": str(last.get("isST") or "0"),
        }

    def number(name: str) -> float:
        value = float(pd.to_numeric(spot[name], errors="raise"))
        if not math.isfinite(value):
            raise RuntimeError(f"新浪快照{code}的{name}不是有限数")
        return value

    close = number("最新价")
    previous_close = number("昨收")
    open_ = number("今开")
    high = number("最高")
    low = number("最低")
    volume = number("成交量")
    amount = number("成交额")
    if min(close, previous_close, open_, high, low) <= 0 or min(volume, amount) < 0:
        raise RuntimeError(f"新浪快照{code}包含非正价格或负成交")

    historical_volume = pd.to_numeric(existing["volume"], errors="coerce")
    historical_turn = pd.to_numeric(existing["turn"], errors="coerce")
    traded = existing.loc[historical_volume.gt(0) & historical_turn.gt(0)]
    if traded.empty:
        raise RuntimeError(f"{code}缺少可推导流通股本的历史换手")
    prior = traded.iloc[-1]
    outstanding_shares = float(prior["volume"]) / (float(prior["turn"]) / 100)
    turn_pct = volume / outstanding_shares * 100
    return {
        "date": target_date, "code": code,
        "open": open_, "high": high, "low": low, "close": close,
        "preclose": previous_close, "volume": volume, "amount": amount,
        "adjustflag": "3", "turn": round(turn_pct, 6), "tradestatus": "1",
        "pctChg": round((close / previous_close - 1) * 100, 6),
        "isST": "1" if "ST" in str(spot.get("名称") or "").upper() else "0",
    }


def update_one_from_sina_snapshot(
    code: str,
    *,
    target_date: str,
    snapshot: pd.DataFrame,
    listing_end: str,
    output_dir: Path = OUTPUT_DIR,
    confirmed_no_trade: bool = False,
) -> dict[str, Any]:
    normalized = code.split(".", 1)[1]
    path = output_dir / f"{normalized}.csv.gz"
    if not path.exists():
        return {"code": code, "status": "missing_base_file"}
    existing = pd.read_csv(path, dtype=str).fillna("")
    if existing.empty or "date" not in existing:
        return {"code": code, "status": "invalid_base_file"}
    last_date = str(existing["date"].max())
    if target_date <= last_date:
        return {"code": code, "status": "already_current", "last_date": last_date}
    if listing_end and listing_end < target_date:
        return {"code": code, "status": "already_current_retired", "last_date": last_date}
    spot = (
        None if confirmed_no_trade
        else snapshot.loc[normalized] if normalized in snapshot.index else None
    )
    try:
        row = _sina_incremental_row(
            existing, code=code, target_date=target_date, spot=spot,
            confirmed_no_trade=confirmed_no_trade,
        )
    except RuntimeError as exc:
        return {"code": code, "status": "source_missing_active_snapshot", "error": str(exc)}
    merged = merge_incremental_rows(existing, pd.DataFrame([row]))
    temporary = path.with_name(f"{path.name}.incremental.part")
    temporary.unlink(missing_ok=True)
    merged.to_csv(temporary, index=False, compression="gzip")
    temporary.replace(path)
    return {
        "code": code,
        "status": "updated_sina" if spot is not None else "updated_sina_suspended",
        "no_trade_confirmation": (
            "Sina dated daily returned no target-date row after market-close probes"
            if confirmed_no_trade else None
        ),
        "rows_added": len(merged) - len(existing),
        "previous_end": last_date,
        "end": target_date,
    }


def run_sina_snapshot_fallback(
    *,
    args: argparse.Namespace,
    manifest: dict[str, Any],
    previous_end: str,
    full_universe: pd.DataFrame,
    universe: pd.DataFrame,
    update_report_path: Path,
    progress_report_path: Path,
    started_at: str,
    started_monotonic: float,
    baostock_error: str,
) -> None:
    """One-snapshot fallback when BaoStock rejects the configured client."""
    import akshare as ak

    target_date = pd.Timestamp(args.end_date).date().isoformat()

    def fetch_daily(symbol: str, day: str) -> pd.DataFrame:
        compact = day.replace("-", "")
        return ak.stock_zh_a_daily(
            symbol=symbol, start_date=compact, end_date=compact, adjust="",
        )

    snapshot = confirm_sina_snapshot(
        ak.stock_zh_a_spot(), target_date=target_date, daily_fetcher=fetch_daily,
    )
    results: list[dict[str, Any]] = []
    last_progress_write = time.monotonic()
    for index, row in enumerate(universe.itertuples(index=False), start=1):
        result = update_one_from_sina_snapshot(
            str(row.code),
            target_date=target_date,
            snapshot=snapshot,
            listing_end=str(row.out_date or "").strip(),
        )
        if result["status"] == "source_missing_active_snapshot":
            dated = fetch_daily(str(row.code).replace(".", ""), target_date)
            dated_dates = (
                pd.to_datetime(dated["date"], errors="coerce").dt.strftime("%Y-%m-%d")
                if not dated.empty and "date" in dated else pd.Series(dtype=str)
            )
            if not dated_dates.eq(target_date).any():
                result = update_one_from_sina_snapshot(
                    str(row.code),
                    target_date=target_date,
                    snapshot=snapshot,
                    listing_end=str(row.out_date or "").strip(),
                    confirmed_no_trade=True,
                )
        results.append(result)
        now = time.monotonic()
        if index == len(universe) or index % 100 == 0 or now - last_progress_write >= 5:
            _atomic_json_write(progress_report_path, build_progress_snapshot(
                started_at=started_at,
                started_monotonic=started_monotonic,
                end_date=target_date,
                total_symbols=len(universe),
                results=results,
                current_code=(
                    str(universe.iloc[index].code) if index < len(universe) else None
                ),
            ))
            last_progress_write = now
    failure_states = {
        "missing_base_file", "invalid_base_file", "source_missing_active_snapshot",
    }
    failures = [row for row in results if row["status"] in failure_states]
    complete_universe_check = (
        args.shard_count == 1 and args.limit == 0 and len(universe) == len(full_universe)
    )
    updated = [row for row in results if row["status"].startswith("updated_sina")]
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    observed_reference_end = reference_market_end()
    publishable = not failures and complete_universe_check
    report = {
        "generated_at": generated_at,
        "source": "AKShare stock_zh_a_spot Sina snapshot + stock_zh_a_daily probes",
        "fallback_reason": baostock_error,
        "previous_published_end_date": previous_end,
        "requested_end_date": target_date,
        "actual_market_data_end": (
            observed_reference_end if publishable else previous_end
        ),
        "observed_reference_end": observed_reference_end,
        "complete_universe_check": complete_universe_check,
        "checked_symbols": len(results),
        "snapshot_rows": len(snapshot),
        "updated_symbols": len(updated),
        "suspended_symbols": sum(
            row["status"] == "updated_sina_suspended" for row in results
        ),
        "rows_added": sum(int(row.get("rows_added") or 0) for row in updated),
        "turnover_method": "current volume / shares inferred from latest audited nonzero volume and turnover",
        "failures": failures,
        "results": results,
    }
    _atomic_json_write(update_report_path, report)
    _atomic_json_write(progress_report_path, build_progress_snapshot(
        started_at=started_at,
        started_monotonic=started_monotonic,
        end_date=target_date,
        total_symbols=len(universe),
        results=results,
        current_code=None,
        status="failed" if failures else "completed",
    ))
    if failures or not complete_universe_check:
        raise RuntimeError(
            f"新浪降级更新失败{len(failures)}只或不是完整全市场检查；保留旧发布清单"
        )

    published_end = report["actual_market_data_end"]
    if published_end != target_date:
        raise RuntimeError(f"新浪降级写入后参考日线只到{published_end}")
    calendar_payload = _load_json(MARKET_CALENDAR_PATH)
    dates = sorted(set(calendar_payload.get("trading_dates") or []) | {published_end})
    _atomic_json_write(MARKET_CALENDAR_PATH, {
        **calendar_payload,
        "generated_at": generated_at,
        "source": "BaoStock historical calendar + Sina dated close probes",
        "end_date": published_end,
        "trading_dates": dates,
    })
    files = existing_files()
    manifest["generated_at"] = generated_at
    manifest.setdefault("query", {})["end_date"] = published_end
    manifest["files"] = files
    manifest["stored_bytes"] = sum(item["bytes"] for item in files)
    manifest["incremental_update"] = {
        "report": str(update_report_path.relative_to(PROJECT_ROOT)),
        "previous_end_date": previous_end,
        "requested_end_date": target_date,
        "end_date": published_end,
        "updated_symbols": len(updated),
        "rows_added": report["rows_added"],
        "source": report["source"],
    }
    _atomic_json_write(REPORT_PATH, manifest)
    print(json.dumps({
        "report": str(update_report_path.relative_to(PROJECT_ROOT)),
        "checked_symbols": len(results),
        "updated_symbols": len(updated),
        "rows_added": report["rows_added"],
        "published_end_date": published_end,
        "source": report["source"],
    }, ensure_ascii=False, indent=2))


def main() -> None:
    args = parse_args()
    if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
        raise SystemExit("V4日线分片参数无效")
    update_report_path, progress_report_path, lock_path = shard_artifact_paths(
        shard_count=args.shard_count, shard_index=args.shard_index
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_stream = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(
            lock_stream.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
    except BlockingIOError as exc:
        lock_stream.close()
        raise RuntimeError("已有V4日线增量任务持有单写者锁") from exc
    lock_stream.seek(0)
    lock_stream.truncate()
    lock_stream.write(str(os.getpid()))
    lock_stream.flush()

    end_date = pd.Timestamp(args.end_date).date().isoformat()
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    started_monotonic = time.monotonic()
    manifest = _load_manifest()
    previous_end = str(
        (manifest.get("query") or {}).get("end_date") or ""
    )
    if not previous_end:
        raise RuntimeError("缺少已发布V4下载清单的结束日期")
    full_universe = eligible_basic_rows(end_date=end_date).reset_index(drop=True)
    universe = full_universe.iloc[args.shard_index :: args.shard_count].copy()
    if args.limit > 0:
        universe = universe.iloc[: args.limit].copy()
    resumed_successes: dict[str, dict[str, Any]] = {}
    if args.resume_failures:
        if args.shard_count != 1 or args.limit:
            raise RuntimeError("失败续传只允许完整单分片任务")
        full_codes = full_universe["code"].astype(str).tolist()
        resumed_successes, retry_codes = resume_failed_results(
            _load_json(update_report_path),
            full_codes=full_codes,
            end_date=end_date,
        )
        universe = full_universe.loc[
            full_universe["code"].astype(str).isin(retry_codes)
        ].copy()
    codes = universe["code"].astype(str).tolist()

    results: list[dict[str, Any]] = []
    _atomic_json_write(
        progress_report_path,
        build_progress_snapshot(
            started_at=started_at,
            started_monotonic=started_monotonic,
            end_date=end_date,
            total_symbols=len(codes),
            results=results,
            current_code=None,
        ),
    )
    login = bs.login()
    if login.error_code != "0":
        if login.error_code == "10001011":
            if args.resume_failures:
                raise RuntimeError(
                    "失败续传期间BaoStock拒绝登录；不混用新浪源，保留断点等待恢复"
                )
            run_sina_snapshot_fallback(
                args=args,
                manifest=manifest,
                previous_end=previous_end,
                full_universe=full_universe,
                universe=universe,
                update_report_path=update_report_path,
                progress_report_path=progress_report_path,
                started_at=started_at,
                started_monotonic=started_monotonic,
                baostock_error=(
                    f"{login.error_code} {login.error_msg}"
                ),
            )
            return
        raise RuntimeError(
            f"BaoStock登录失败：{login.error_code} {login.error_msg}"
        )
    try:
        official_market_dates = query_official_market_dates(
            str((manifest.get("query") or {}).get("start_date") or "2018-01-01"),
            end_date,
        )
        try:
            require_upstream_market_date(
                official_market_dates,
                published_end=previous_end,
            )
        except RuntimeError as exc:
            waiting = build_progress_snapshot(
                started_at=started_at,
                started_monotonic=started_monotonic,
                end_date=end_date,
                total_symbols=len(codes),
                results=results,
                current_code=None,
                status="waiting_for_upstream",
            )
            waiting["upstream_data_ready"] = False
            waiting["error"] = str(exc)
            _atomic_json_write(progress_report_path, waiting)
            raise
        last_progress_write = time.monotonic()
        for index, row in enumerate(universe.itertuples(index=False), start=1):
            code = str(row.code)
            listing_start = max(
                official_market_dates[0], str(row.ipo_date)
            )
            out_date = str(row.out_date or "").strip()
            listing_end = min(end_date, out_date) if out_date else end_date
            try:
                results.append(run_with_session_recovery(lambda: update_one(
                    code,
                    end_date=end_date,
                    expected_market_dates=official_market_dates,
                    listing_start=listing_start,
                    listing_end=listing_end,
                    allow_trailing_missing=bool(out_date),
                )))
            except BaoStockSessionRecoveryError:
                raise
            except Exception as exc:
                results.append({
                    "code": code,
                    "status": "failed",
                    "error": str(exc),
                })
            now = time.monotonic()
            if (
                index == len(codes)
                or index % 10 == 0
                or now - last_progress_write >= 5
            ):
                _atomic_json_write(
                    progress_report_path,
                    build_progress_snapshot(
                        started_at=started_at,
                        started_monotonic=started_monotonic,
                        end_date=end_date,
                        total_symbols=len(codes),
                        results=results,
                        current_code=(
                            codes[index]
                            if index < len(codes) else None
                        ),
                    ),
                )
                last_progress_write = now
    finally:
        bs.logout()

    if resumed_successes:
        retried = {str(item.get("code")): item for item in results}
        combined = {**resumed_successes, **retried}
        results = [
            combined[code]
            for code in full_universe["code"].astype(str).tolist()
        ]

    failures = [
        item for item in results if item["status"] in FAILURE_STATES
    ]
    complete_universe_check = (
        args.shard_count == 1
        and args.limit == 0
        and len(results) == len(full_universe)
    )
    updated = [
        item for item in results
        if item["status"] in {"updated", "bootstrapped_new_listing"}
    ]
    report = {
        "generated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "source": "BaoStock query_history_k_data_plus",
        "previous_published_end_date": previous_end,
        "requested_end_date": end_date,
        "shard_count": args.shard_count,
        "shard_index": args.shard_index,
        "official_market_date_count": len(official_market_dates),
        "resumed_failed_only": bool(resumed_successes),
        "retried_symbols": len(codes) if resumed_successes else 0,
        "actual_market_data_end": reference_market_end(),
        "complete_universe_check": complete_universe_check,
        "checked_symbols": len(results),
        "updated_symbols": len(updated),
        "rows_added": sum(item.get("rows_added", 0) for item in updated),
        "failures": failures,
        "results": results,
    }
    _atomic_json_write(
        update_report_path,
        report,
    )
    _atomic_json_write(
        progress_report_path,
        build_progress_snapshot(
            started_at=started_at,
            started_monotonic=started_monotonic,
            end_date=end_date,
            total_symbols=(len(full_universe) if resumed_successes else len(codes)),
            results=results,
            current_code=None,
            status="failed" if failures else "completed",
        ),
    )
    if failures:
        raise RuntimeError(
            f"增量更新有 {len(failures)} 只失败；保留旧发布清单，"
            "下一轮按失败状态重试"
        )

    if complete_universe_check:
        published_end = report["actual_market_data_end"]
        files = existing_files()
        publish_market_calendar(
            official_market_dates,
            published_end=published_end,
            generated_at=report["generated_at"],
        )
        manifest["generated_at"] = report["generated_at"]
        # A requested weekend/holiday or an empty upstream response must not
        # masquerade as published market data.  The reference stock supplies
        # the actual latest completed market session.
        manifest.setdefault("query", {})["end_date"] = published_end
        manifest["files"] = files
        manifest["target_symbols"] = len(full_universe)
        manifest["downloaded_symbols"] = len(files)
        manifest["coverage_pct"] = round(
            len(files) / len(full_universe) * 100, 3,
        ) if len(full_universe) else 0.0
        manifest["ready_for_factor_research"] = (
            len(files) == len(full_universe)
        )
        manifest["universe_security_master"] = {
            "path": str(BASIC_PATH.relative_to(PROJECT_ROOT)),
            "sha256": sha256(BASIC_PATH),
            "membership_rule": (
                "type=1; ipo_date<=query.end_date; "
                "out_date empty or out_date>=query.start_date"
            ),
        }
        manifest["stored_bytes"] = sum(item["bytes"] for item in files)
        manifest["incremental_update"] = {
            "report": str(update_report_path.relative_to(PROJECT_ROOT)),
            "previous_end_date": previous_end,
            "requested_end_date": end_date,
            "end_date": published_end,
            "updated_symbols": len(updated),
            "rows_added": report["rows_added"],
        }
        _atomic_json_write(REPORT_PATH, manifest)

    print(json.dumps({
        "report": str(update_report_path.relative_to(PROJECT_ROOT)),
        "checked_symbols": len(results),
        "updated_symbols": len(updated),
        "rows_added": report["rows_added"],
        "published_end_date": (
            report["actual_market_data_end"]
            if complete_universe_check else previous_end
        ),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
