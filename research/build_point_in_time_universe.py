from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

import baostock as bs
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = (
    PROJECT_ROOT / "research" / "data" / "stock_basic_baostock.csv"
)
SNAPSHOT_PATH = (
    PROJECT_ROOT / "research" / "data" / "universe_year_end_counts.csv"
)
REPORT_PATH = (
    PROJECT_ROOT / "reports" / "point_in_time_universe_manifest.json"
)
START_YEAR = 2018
END_YEAR = 2025


def get_frame(result, label: str) -> pd.DataFrame:
    if result.error_code != "0":
        raise RuntimeError(
            f"BaoStock {label}失败：{result.error_code} {result.error_msg}"
        )
    rows = []
    while result.next():
        rows.append(result.get_row_data())
    return pd.DataFrame(rows, columns=result.fields)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    frame.to_csv(temporary, index=False)
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def main() -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(
            f"BaoStock登录失败：{login.error_code} {login.error_msg}"
        )
    try:
        stocks = get_frame(bs.query_stock_basic(), "证券基本资料")
    finally:
        bs.logout()
    if stocks.empty:
        raise RuntimeError("BaoStock未返回证券基本资料")

    stocks = stocks.rename(columns={
        "ipoDate": "ipo_date",
        "outDate": "out_date",
        "code_name": "name",
    })
    _atomic_csv(DATA_PATH, stocks)

    stock_rows = stocks[stocks["type"].astype(str) == "1"].copy()
    stock_rows["ipo_date_parsed"] = pd.to_datetime(
        stock_rows["ipo_date"], errors="coerce"
    )
    stock_rows["out_date_parsed"] = pd.to_datetime(
        stock_rows["out_date"].replace("", pd.NA), errors="coerce"
    )
    snapshots = []
    for year in range(START_YEAR, END_YEAR + 1):
        day = pd.Timestamp(f"{year}-12-31")
        active = stock_rows[
            (stock_rows["ipo_date_parsed"] <= day)
            & (
                stock_rows["out_date_parsed"].isna()
                | (stock_rows["out_date_parsed"] > day)
            )
        ]
        snapshots.append({
            "date": day.date().isoformat(),
            "listed_stock_count": len(active),
        })
    _atomic_csv(SNAPSHOT_PATH, pd.DataFrame(snapshots))

    delisted = stock_rows[stock_rows["out_date_parsed"].notna()]
    report = {
        "generated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "source": "BaoStock 0.9.3 query_stock_basic",
        "security_rows": len(stocks),
        "stock_rows": len(stock_rows),
        "delisted_stock_rows": len(delisted),
        "stock_basic_path": str(DATA_PATH.relative_to(PROJECT_ROOT)),
        "stock_basic_sha256": sha256(DATA_PATH),
        "year_end_counts_path": str(
            SNAPSHOT_PATH.relative_to(PROJECT_ROOT)
        ),
        "year_end_counts_sha256": sha256(SNAPSHOT_PATH),
        "year_end_counts": snapshots,
        "membership_rule": (
            "ipo_date <= T 且 out_date 为空或 out_date > T；type=1"
        ),
        "status": "listing_membership_ready",
        "remaining_work": [
            "为全量股票下载逐日OHLCV、tradestatus和isST",
            "按板块、ST、新股和制度日期重建逐日涨跌停价",
            "对历史名称和退市整理状态做额外核验",
        ],
    }
    _atomic_json(REPORT_PATH, report)
    print(json.dumps({
        "report": str(REPORT_PATH.relative_to(PROJECT_ROOT)),
        "stock_rows": report["stock_rows"],
        "delisted_stock_rows": report["delisted_stock_rows"],
        "year_end_counts": snapshots,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
