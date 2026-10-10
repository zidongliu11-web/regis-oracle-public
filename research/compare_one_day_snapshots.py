"""Compare one SHA-bound vendor snapshot with one existing BaoStock day.

Only counts, hashes and a bounded sample of codes leave this process. This
does not establish a PIT history, source independence, rights or trading alpha.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sys


DAY = "2026-09-28"
ROOT = Path(__file__).resolve().parents[1]
BAO_PATH = "reports/local_closure/m09_bulk_astock_2026-09-28_595a1e198f35e12b5c61530b7c3deee978d1dd90800b818d86a5da52826d2e4e.json"
TRAD_PATH = "reports/local_closure/m17_tradsig_raw/20260928.parquet"
META_PATH = "QUANT_RESEARCH_M17_SOURCE_META_20260929.json"
BAO_MANIFEST = ".codex-local/m09_manifest.json"
MAX_BYTES = 32 * 1024 * 1024
MAX_ROWS = 10_000


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixed(relative: str) -> Path:
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        raise ValueError("input_missing_or_outside_root")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("input_oversized")
    return path


def decimal_price(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("price_invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("price_invalid") from None
    if not result.is_finite() or result <= 0:
        raise ValueError("price_invalid")
    return result


def nonnegative_number(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("amount_or_volume_invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("amount_or_volume_invalid") from None
    if not result.is_finite() or result < 0:
        raise ValueError("amount_or_volume_invalid")
    return result


def bao_code(value: object) -> str:
    match = re.fullmatch(r"(sh|sz|bj)\.(\d{6})", value if isinstance(value, str) else "")
    if not match:
        raise ValueError("bao_code_invalid")
    return match.group(2) + "." + match.group(1).upper()


def trad_code(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value):
        raise ValueError("trad_code_invalid")
    return value


def bool01(value: object) -> bool:
    if value == "0":
        return False
    if value == "1":
        return True
    raise ValueError("bao_flag_invalid")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    if Path(args.root).resolve() != ROOT:
        raise SystemExit("root_mismatch")
    sys.path.insert(0, str(ROOT / ".codex-local/deps"))
    import pyarrow  # type: ignore[import-not-found]  # noqa: E402
    import pyarrow.parquet as pq  # type: ignore[import-not-found]  # noqa: E402

    if pyarrow.__version__ != "25.0.0":
        raise ValueError("pyarrow_version_mismatch")
    meta = json.loads(fixed(META_PATH).read_text(encoding="utf-8"))
    manifest = json.loads(fixed(BAO_MANIFEST).read_text(encoding="utf-8"))
    bao_record = manifest["days"][DAY]["daily"]
    if bao_record["path"] != BAO_PATH or meta["local_raw_path"] != TRAD_PATH:
        raise ValueError("source_path_identity_mismatch")
    bao_path, trad_path = fixed(BAO_PATH), fixed(TRAD_PATH)
    bao_sha, trad_sha = digest(bao_path), digest(trad_path)
    if bao_sha != bao_record["sha256"] or trad_sha != meta["sha256"]:
        raise ValueError("source_sha_mismatch")
    bao = json.loads(bao_path.read_text(encoding="utf-8"))
    if bao.get("day") != DAY or not isinstance(bao.get("fields"), list):
        raise ValueError("bao_date_or_schema_invalid")
    needed = ("date", "code", "close", "isST", "tradestatus", "volume", "amount", "adjustflag")
    fields = bao["fields"]
    if len(fields) != len(set(fields)) or any(field not in fields for field in needed):
        raise ValueError("bao_fields_invalid")
    if not isinstance(bao.get("rows"), list) or len(bao["rows"]) > MAX_ROWS:
        raise ValueError("bao_rows_invalid")
    positions = {field: fields.index(field) for field in needed}
    bao_map = {}
    for row in bao["rows"]:
        if not isinstance(row, list) or len(row) != len(fields) or row[positions["date"]] != DAY:
            raise ValueError("bao_row_shape_or_date_invalid")
        code = bao_code(row[positions["code"]])
        if code in bao_map:
            raise ValueError("bao_duplicate_code")
        if row[positions["adjustflag"]] != "3":
            raise ValueError("bao_adjustflag_not_raw")
        volume = nonnegative_number(row[positions["volume"]])
        amount = nonnegative_number(row[positions["amount"]])
        bao_map[code] = {
            "close": decimal_price(row[positions["close"]]),
            "st": bool01(row[positions["isST"]]),
            "active": (bool01(row[positions["tradestatus"]])
                       and volume is not None and volume > 0
                       and amount is not None and amount > 0),
        }

    metadata = pq.read_metadata(trad_path)
    if metadata.num_rows > MAX_ROWS or metadata.num_columns > 100:
        raise ValueError("trad_metadata_size_invalid")
    columns = ["trade_date", "symbol", "close", "is_st", "data_version"]
    if any(column not in metadata.schema.names for column in columns):
        raise ValueError("trad_fields_missing")
    table = pq.read_table(trad_path, columns=columns)
    trad_map = {}
    versions = Counter()
    for row in table.to_pylist():
        trade_date = row["trade_date"]
        if not isinstance(trade_date, date) or trade_date.isoformat() != DAY:
            raise ValueError("trad_date_invalid")
        code = trad_code(row["symbol"])
        if code in trad_map:
            raise ValueError("trad_duplicate_code")
        if type(row["is_st"]) is not bool:
            raise ValueError("trad_st_invalid")
        version = row["data_version"]
        if not isinstance(version, str):
            raise ValueError("trad_version_invalid")
        versions[version] += 1
        trad_map[code] = {"close": decimal_price(row["close"]), "st": row["is_st"]}
    if set(versions) != {"qlib-cn-20260928"}:
        raise ValueError("trad_version_unexpected")

    common = sorted(bao_map.keys() & trad_map.keys())
    only_bao = sorted(bao_map.keys() - trad_map.keys())
    only_trad = sorted(trad_map.keys() - bao_map.keys())
    only_bao_active = sum(bao_map[code]["active"] for code in only_bao)
    only_trad_by_exchange = dict(Counter(code.rsplit(".", 1)[1] for code in only_trad))
    exact_equal = 0
    over_half_cent = 0
    over_cent = 0
    both_close = 0
    active_common = 0
    active_exact_equal = 0
    price_mismatch_codes = []
    st_mismatch_codes = []
    for code in common:
        b, t = bao_map[code], trad_map[code]
        if b["st"] != t["st"]:
            st_mismatch_codes.append(code)
        if b["active"]:
            active_common += 1
        if b["close"] is None or t["close"] is None:
            continue
        both_close += 1
        difference = abs(b["close"] - t["close"])
        if difference == 0:
            exact_equal += 1
            if b["active"]:
                active_exact_equal += 1
        else:
            price_mismatch_codes.append(code)
        over_half_cent += difference > Decimal("0.005")
        over_cent += difference > Decimal("0.01")
    output = {
        "schema": "regis.m17.one_day_cross_source.v1", "day": DAY,
        "source_sha256": {"baostock": bao_sha, "tradsig": trad_sha},
        "source_rows": {"baostock": len(bao_map), "tradsig": len(trad_map)},
        "symbols": {"intersection": len(common), "bao_only": len(only_bao), "trad_only": len(only_trad),
                    "bao_only_active": only_bao_active,
                    "trad_only_by_exchange": only_trad_by_exchange,
                    "bao_only_examples": only_bao[:10], "trad_only_examples": only_trad[:10]},
        "close": {"both_nonmissing": both_close, "exact_equal": exact_equal,
                  "different": len(price_mismatch_codes), "difference_gt_half_cent": over_half_cent,
                  "difference_gt_cent": over_cent, "different_code_examples": price_mismatch_codes[:10],
                  "bao_missing_total": sum(x["close"] is None for x in bao_map.values()),
                  "trad_missing_total": sum(x["close"] is None for x in trad_map.values()),
                  "bao_active_common": active_common, "bao_active_exact_equal": active_exact_equal},
        "st": {"different": len(st_mismatch_codes), "different_code_examples": st_mismatch_codes[:10]},
        "tradsig_versions": dict(versions),
        "original_market_rows_saved_to_git": False,
        "historical_pit_verified": False,
        "source_independence_verified": False,
        "data_rights_for_cloud_or_website_verified": False,
        "target_a_admitted": False,
    }
    print(json.dumps(output, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
