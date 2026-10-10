from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import date, datetime
from pathlib import Path

import baostock as bs
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASIC_PATH = PROJECT_ROOT / "research" / "data" / "stock_basic_baostock.csv"
OUTPUT_DIR = PROJECT_ROOT / "research" / "data" / "v4_daily"
REPORT_PATH = PROJECT_ROOT / "reports" / "v4_daily_download_manifest.json"
PROGRESS_PATH = PROJECT_ROOT / "reports" / "v4_daily_download_progress.json"
START_DATE = "2018-01-01"
END_DATE = "2025-12-31"
FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,adjustflag,"
    "turn,tradestatus,pctChg,isST"
)
ALLOWED_PREFIXES = (
    "sh.600",
    "sh.601",
    "sh.603",
    "sh.605",
    "sz.000",
    "sz.001",
    "sz.002",
    "sz.003",
    "sz.300",
    "sz.301",
    "sz.302",
)
UNIVERSE_SCOPE = {
    "id": "sh_sz_main_chinext",
    "label": "沪深主板与创业板",
    "included_segments": ["上海主板", "深圳主板", "创业板"],
    "excluded_segments": ["科创板", "北交所"],
    "full_a_share_coverage": False,
}


def universe_scope_payload(
    *,
    ready: bool | None = None,
    end_date: str = END_DATE,
) -> dict:
    payload = {
        **UNIVERSE_SCOPE,
        "included_segments": list(UNIVERSE_SCOPE["included_segments"]),
        "excluded_segments": list(UNIVERSE_SCOPE["excluded_segments"]),
        "eligibility_window": {
            "start_date": START_DATE,
            "end_date": end_date,
            "definition": "在研究窗口内曾上市且属于已纳入板块的普通股票",
        },
    }
    if ready is not None:
        payload["scope_complete"] = bool(ready)
    return payload


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def eligible_basic_rows(*, end_date: str = END_DATE) -> pd.DataFrame:
    if not BASIC_PATH.exists():
        raise RuntimeError(
            "缺少stock_basic_baostock.csv，请先运行"
            "python3 -m research.build_point_in_time_universe"
        )
    frame = pd.read_csv(BASIC_PATH, dtype=str).fillna("")
    frame = frame[frame["type"] == "1"].copy()
    frame = frame[
        frame["code"].str.startswith(ALLOWED_PREFIXES)
    ].copy()
    frame["ipo_date_parsed"] = pd.to_datetime(
        frame["ipo_date"], errors="coerce"
    )
    frame["out_date_parsed"] = pd.to_datetime(
        frame["out_date"].replace("", pd.NA), errors="coerce"
    )
    start = pd.Timestamp(START_DATE)
    end = pd.Timestamp(end_date)
    frame = frame[
        (frame["ipo_date_parsed"] <= end)
        & (
            frame["out_date_parsed"].isna()
            | (frame["out_date_parsed"] >= start)
        )
    ]
    return frame.sort_values("code")


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.tmp"
    )
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def query(code: str, *, end_date: str = END_DATE) -> pd.DataFrame:
    result = bs.query_history_k_data_plus(
        code,
        FIELDS,
        start_date=START_DATE,
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


def download_one(
    code: str,
    *,
    end_date: str = END_DATE,
    allow_existing: bool = True,
) -> dict:
    normalized = code.split(".", 1)[1]
    path = OUTPUT_DIR / f"{normalized}.csv.gz"
    partial_path = path.with_name(f"{path.name}.part")
    if allow_existing and path.exists() and path.stat().st_size > 100:
        return {
            "code": code,
            "status": "existing",
            "path": str(path.relative_to(PROJECT_ROOT)),
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
        }
    last_error = None
    for attempt in range(1, 4):
        try:
            partial_path.unlink(missing_ok=True)
            frame = query(code, end_date=end_date)
            if frame.empty:
                return {"code": code, "status": "empty"}
            # Only publish a completed gzip file. The autonomous worker may be
            # stopped while BaoStock is downloading; writing directly to the
            # final path could otherwise leave a truncated file that a later
            # cycle mistakes for valid cached data.
            frame.to_csv(
                partial_path,
                index=False,
                compression="gzip",
            )
            partial_path.replace(path)
            return {
                "code": code,
                "status": "downloaded",
                "rows": len(frame),
                "start": str(frame.iloc[0]["date"]),
                "end": str(frame.iloc[-1]["date"]),
                "suspended_days": int(
                    (frame["tradestatus"] == "0").sum()
                ),
                "st_days": int((frame["isST"] == "1").sum()),
                "path": str(path.relative_to(PROJECT_ROOT)),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
        except Exception as exc:
            last_error = str(exc)
            partial_path.unlink(missing_ok=True)
            time.sleep(attempt)
    return {"code": code, "status": "failed", "error": last_error}


def existing_files() -> list[dict]:
    files = []
    for path in sorted(OUTPUT_DIR.glob("*.csv.gz")):
        files.append({
            "code": path.stem.split(".")[0],
            "path": str(path.relative_to(PROJECT_ROOT)),
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
        })
    return files


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="本次最多处理多少只未达到目标日期的股票，0表示不限制",
    )
    parser.add_argument(
        "--end-date",
        default=END_DATE,
        help="真实行情下载截止日，格式YYYY-MM-DD",
    )
    parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="忽略旧下载清单，重新核验并下载已有文件",
    )
    parser.add_argument(
        "--shard-count",
        type=int,
        default=1,
        help="将固定股票池拆成多少个互不重叠的下载分片",
    )
    parser.add_argument(
        "--shard-index",
        type=int,
        default=0,
        help="本进程处理的分片编号，从0开始",
    )
    parser.add_argument(
        "--consolidate-shards",
        action="store_true",
        help="校验所有分片证据并发布唯一的全市场清单",
    )
    return parser.parse_args()


def should_explicitly_logout(shard_count: int) -> bool:
    """Avoid invalidating sibling BaoStock sessions in sharded mode."""
    return shard_count == 1


def consolidate_shards(
    *,
    shard_count: int,
    requested_end: str,
    target_codes: list[str],
) -> dict:
    if shard_count <= 1:
        raise RuntimeError("分片合并要求--shard-count大于1")
    combined: dict[str, dict] = {}
    failures: list[dict] = []
    shard_states: list[dict] = []
    for shard_index in range(shard_count):
        progress_path = PROGRESS_PATH.with_name(
            "v4_daily_download_progress."
            f"shard-{shard_index:02d}-of-{shard_count:02d}.json"
        )
        try:
            progress = json.loads(
                progress_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"无法读取第{shard_index}分片进度：{exc}"
            ) from exc
        shard = progress.get("shard") or {}
        valid_state = (
            progress.get("state") == "completed"
            and (progress.get("query") or {}).get("end_date")
            == requested_end
            and shard.get("count") == shard_count
            and shard.get("index") == shard_index
        )
        shard_states.append({
            "index": shard_index,
            "state": progress.get("state"),
            "started_at": progress.get("started_at"),
            "completed_symbols": progress.get("completed_symbols"),
            "target_symbols": progress.get("target_symbols"),
            "valid": valid_state,
        })
        if not valid_state:
            raise RuntimeError(
                f"第{shard_index}分片尚未形成可合并的完成证据"
            )
        for item in progress.get("completed") or []:
            code = item.get("code")
            if not code:
                continue
            if item.get("status") not in {"downloaded", "existing"}:
                failures.append(item)
                continue
            if (
                item.get("end") != requested_end
                and item.get("requested_end") != requested_end
            ):
                failures.append({
                    **item,
                    "status": "wrong_requested_end",
                })
                continue
            path = OUTPUT_DIR / f"{code.split('.', 1)[1]}.csv.gz"
            if not path.is_file() or path.stat().st_size <= 100:
                failures.append({
                    **item,
                    "status": "missing_physical_file",
                })
                continue
            combined[code] = item

    missing_codes = [code for code in target_codes if code not in combined]
    failures.extend(
        {"code": code, "status": "missing_from_shard_evidence"}
        for code in missing_codes
    )
    files = existing_files()
    ready = (
        len(combined) == len(target_codes)
        and len(files) == len(target_codes)
        and not failures
    )
    ordered_results = [
        combined[code] for code in target_codes if code in combined
    ]
    report = {
        "generated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "source": "BaoStock 0.9.3 query_history_k_data_plus",
        "query": {
            "start_date": START_DATE,
            "end_date": requested_end,
            "frequency": "d",
            "adjustflag": "3 (不复权)",
            "fields": FIELDS,
        },
        "board_scope": "沪深主板与创业板；排除科创板、北交所",
        "universe_scope": universe_scope_payload(
            ready=ready,
            end_date=requested_end,
        ),
        "full_a_share_coverage": False,
        "target_symbols": len(target_codes),
        "downloaded_symbols": len(files),
        "coverage_pct": round(
            len(combined) / len(target_codes) * 100,
            3,
        ) if target_codes else 0,
        "stored_bytes": sum(item["bytes"] for item in files),
        "batch_results": ordered_results,
        "failures": failures,
        "files": files,
        "ready_for_factor_research": ready,
        "universe_security_master": {
            "path": str(BASIC_PATH.relative_to(PROJECT_ROOT)),
            "sha256": sha256(BASIC_PATH),
            "membership_rule": (
                "type=1; ipo_date<=query.end_date; "
                "out_date empty or out_date>=query.start_date"
            ),
        },
        "shard_consolidation": {
            "count": shard_count,
            "states": shard_states,
            "completed_symbols": len(combined),
        },
        "data_notes": [
            "使用不复权OHLC作为真实成交价和涨跌停研究基础",
            "每个分片处理固定且互不重叠的证券索引",
            "合并时逐项核验目标截止日、物理gzip文件和完整覆盖率",
        ],
    }
    write_json_atomic(REPORT_PATH, report)
    write_json_atomic(PROGRESS_PATH, {
        "schema_version": 1,
        "state": "completed" if ready else "partial",
        "started_at": min(
            (
                str(item.get("started_at") or "")
                for item in shard_states
                if item.get("started_at")
            ),
            default=None,
        ),
        "updated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "query": {
            "start_date": START_DATE,
            "end_date": requested_end,
        },
        "universe_scope": universe_scope_payload(
            ready=ready,
            end_date=requested_end,
        ),
        "target_symbols": len(target_codes),
        "completed_symbols": len(combined),
        "remaining_symbols": len(missing_codes),
        "progress_pct": report["coverage_pct"],
        "session_failures": failures,
        "completed": ordered_results,
        "shard_consolidation": report["shard_consolidation"],
        "progress_definition": (
            "仅合并完成分片中目标截止日匹配且物理gzip仍存在的证券；"
            "等待和运行时长不增加进度。"
        ),
    })
    return report


def main() -> None:
    args = parse_args()
    if args.shard_count < 1:
        raise SystemExit("--shard-count必须至少为1")
    if not 0 <= args.shard_index < args.shard_count:
        raise SystemExit("--shard-index必须在[0, shard-count)范围内")
    try:
        requested_end = date.fromisoformat(args.end_date).isoformat()
    except ValueError as exc:
        raise SystemExit(f"--end-date格式错误：{exc}") from exc
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    universe = eligible_basic_rows(end_date=requested_end)
    all_target_codes = [
        row["code"] for _, row in universe.iterrows()
    ]
    if args.consolidate_shards:
        report = consolidate_shards(
            shard_count=args.shard_count,
            requested_end=requested_end,
            target_codes=all_target_codes,
        )
        print(json.dumps({
            "report": str(REPORT_PATH.relative_to(PROJECT_ROOT)),
            "target_symbols": report["target_symbols"],
            "downloaded_symbols": report["downloaded_symbols"],
            "coverage_pct": report["coverage_pct"],
            "batch_failures": len(report["failures"]),
            "ready_for_factor_research": report[
                "ready_for_factor_research"
            ],
        }, ensure_ascii=False, indent=2))
        return
    target_codes = all_target_codes[
        args.shard_index :: args.shard_count
    ]
    progress_path = (
        PROGRESS_PATH
        if args.shard_count == 1
        else PROGRESS_PATH.with_name(
            "v4_daily_download_progress."
            f"shard-{args.shard_index:02d}-of-"
            f"{args.shard_count:02d}.json"
        )
    )
    report_path = (
        REPORT_PATH
        if args.shard_count == 1
        else REPORT_PATH.with_name(
            "v4_daily_download_manifest."
            f"shard-{args.shard_index:02d}-of-"
            f"{args.shard_count:02d}.json"
        )
    )
    previous_progress = {}
    if progress_path.exists() and not args.refresh_existing:
        try:
            loaded = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if (loaded.get("query") or {}).get("end_date") == requested_end:
            previous_progress = {
                item["code"]: item
                for item in (loaded.get("completed") or [])
                if item.get("status") in {"downloaded", "existing"}
                and item.get("code")
            }
    reusable_codes = {
        code
        for code, item in previous_progress.items()
        if (
            (OUTPUT_DIR / f"{code.split('.', 1)[1]}.csv.gz").is_file()
            and (
                item.get("end") == requested_end
                or item.get("requested_end") == requested_end
            )
        )
    }
    pending = [code for code in target_codes if code not in reusable_codes]
    if args.limit > 0:
        pending = pending[:args.limit]

    results = [
        previous_progress[code]
        for code in target_codes
        if code in reusable_codes
    ]
    session_results: list[dict] = []
    started_at = datetime.now().astimezone()
    started_monotonic = time.monotonic()

    def publish_progress(
        *,
        state: str,
        current_code: str | None = None,
        error: str | None = None,
    ) -> None:
        elapsed = max(0.0, time.monotonic() - started_monotonic)
        completed_codes = {
            item.get("code")
            for item in results
            if item.get("status") in {"downloaded", "existing"}
        }
        completed_count = len(completed_codes)
        processed_this_session = len(session_results)
        rate = (
            processed_this_session / elapsed
            if elapsed > 0 and processed_this_session > 0
            else 0.0
        )
        remaining = max(0, len(target_codes) - completed_count)
        payload = {
            "schema_version": 1,
            "state": state,
            "started_at": started_at.isoformat(timespec="seconds"),
            "updated_at": datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
            "query": {
                "start_date": START_DATE,
                "end_date": requested_end,
            },
            "universe_scope": universe_scope_payload(
                end_date=requested_end,
            ),
            "shard": {
                "count": args.shard_count,
                "index": args.shard_index,
                "target_symbols": len(target_codes),
            },
            "target_symbols": len(target_codes),
            "completed_symbols": completed_count,
            "remaining_symbols": remaining,
            "progress_pct": round(
                completed_count / len(target_codes) * 100,
                3,
            ) if target_codes else 0.0,
            "current_code": current_code,
            "elapsed_seconds": round(elapsed, 3),
            "symbols_per_minute": round(rate * 60, 3),
            "eta_seconds": (
                round(remaining / rate)
                if rate > 0
                else None
            ),
            "session_processed_symbols": processed_this_session,
            "session_failures": [
                item
                for item in session_results
                if item.get("status") not in {"downloaded", "existing"}
            ],
            "last_error": error,
            "completed": results,
            "progress_definition": (
                "仅按已写入完整gzip文件且目标截止日匹配的证券计数；"
                "等待、重试和运行时长不增加进度。"
            ),
        }
        write_json_atomic(progress_path, payload)

    publish_progress(state="starting")
    if pending:
        login = bs.login()
        if login.error_code != "0":
            publish_progress(
                state="failed",
                error=(
                    f"BaoStock登录失败：{login.error_code} "
                    f"{login.error_msg}"
                ),
            )
            raise RuntimeError(
                f"BaoStock登录失败：{login.error_code} {login.error_msg}"
            )
        try:
            for index, code in enumerate(pending, start=1):
                publish_progress(state="running", current_code=code)
                item = download_one(
                    code,
                    end_date=requested_end,
                    allow_existing=False,
                )
                item["requested_end"] = requested_end
                results.append(item)
                session_results.append(item)
                if index % 10 == 0 or item.get("status") == "failed":
                    publish_progress(
                        state="running",
                        current_code=code,
                        error=item.get("error"),
                    )
        finally:
            # BaoStock invalidates other concurrent processes when one process
            # explicitly logs out. A sharded worker exits immediately after
            # publishing its evidence, so closing the process/socket is enough
            # and preserves the remaining workers' authenticated sessions.
            if should_explicitly_logout(args.shard_count):
                bs.logout()

    target_symbols = {
        code.split(".", 1)[1] for code in target_codes
    }
    files = [
        item
        for item in existing_files()
        if item.get("code") in target_symbols
    ]
    failures = [
        item
        for item in session_results
        if item.get("status") not in {"downloaded", "existing"}
    ]
    report = {
        "generated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "source": "BaoStock 0.9.3 query_history_k_data_plus",
        "query": {
            "start_date": START_DATE,
            "end_date": requested_end,
            "frequency": "d",
            "adjustflag": "3 (不复权)",
            "fields": FIELDS,
        },
        "board_scope": "沪深主板与创业板；排除科创板、北交所",
        "shard": {
            "count": args.shard_count,
            "index": args.shard_index,
        },
        "target_symbols": len(target_codes),
        "downloaded_symbols": len(files),
        "coverage_pct": round(
            len({
                item.get("code")
                for item in results
                if item.get("status") in {"downloaded", "existing"}
            }) / len(target_codes) * 100,
            3,
        ) if target_codes else 0,
        "stored_bytes": sum(item["bytes"] for item in files),
        "batch_results": results,
        "failures": failures,
        "files": files,
        "ready_for_factor_research": (
            len({
                item.get("code")
                for item in results
                if item.get("status") in {"downloaded", "existing"}
                and (
                    item.get("end") == requested_end
                    or item.get("requested_end") == requested_end
                )
            }) == len(target_codes)
            and not failures
        ),
        "data_notes": [
            "使用不复权OHLC作为真实成交价和涨跌停研究基础",
            "pctChg用于构建连续收益序列，需另行核对企业行动处理",
            "tradestatus/isST用于逐日可交易约束",
            "断点续跑只复用目标截止日一致且物理文件仍存在的证券",
        ],
        "universe_security_master": {
            "path": str(BASIC_PATH.relative_to(PROJECT_ROOT)),
            "sha256": sha256(BASIC_PATH),
            "membership_rule": (
                "type=1; ipo_date<=query.end_date; "
                "out_date empty or out_date>=query.start_date"
            ),
        },
    }
    report["universe_scope"] = universe_scope_payload(
        ready=report["ready_for_factor_research"],
        end_date=requested_end,
    )
    report["full_a_share_coverage"] = False
    write_json_atomic(report_path, report)
    publish_progress(
        state=(
            "completed"
            if report["ready_for_factor_research"]
            else "partial"
        )
    )
    print(json.dumps({
        "report": str(report_path.relative_to(PROJECT_ROOT)),
        "target_symbols": report["target_symbols"],
        "downloaded_symbols": report["downloaded_symbols"],
        "coverage_pct": report["coverage_pct"],
        "stored_mb": round(report["stored_bytes"] / 1024 / 1024, 2),
        "batch_failures": len(failures),
        "ready_for_factor_research": report["ready_for_factor_research"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
