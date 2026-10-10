"""Create one protocol-bound, research-only shadow origin from a SHA-bound snapshot.

The raw snapshot and symbol-level origin remain in ignored local storage.  The
versioned public summary contains counts, hashes, dates and boundary states,
without candidate identities or price/return values.  This module neither
downloads data nor reads an account, and it cannot admit a trading model.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = "QUANT_RESEARCH_M20_PREREGISTRATION_20260930.json"
SOURCE_PATH = "reports/local_closure/m20_tradsig_raw/20260929.parquet"
SOURCE_RECEIPT_PATH = "reports/local_closure/m20_tradsig_raw/download_receipt.json"
ORIGIN_PATH = "reports/local_closure/m20_shadow_baseline/2026-09-29/origin.json"
SUMMARY_PATH = "QUANT_RESEARCH_M20_ORIGIN_SUMMARY_20260930.json"
MAX_BYTES = 32 * 1024 * 1024
MAX_ROWS = 10_000
REQUIRED = {
    "trade_date", "symbol", "name", "close", "pct_chg", "circ_mv", "is_st",
    "turnr_pct", "illiq_pct", "data_version", "generated_at",
}
BOUNDARY = {
    "research_only": True,
    "model_admitted": False,
    "economic_target_A_validated": False,
    "generates_alert": False,
    "changes_trade_authority": False,
    "account_read": False,
    "returns_computed": False,
}
SH_TZ = ZoneInfo("Asia/Shanghai")


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def digest_file(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def bounded_path(root: Path, relative: str, *, must_exist: bool = True) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise ValueError("path_invalid")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError("path_invalid")
    resolved_root = root.resolve()
    path = resolved_root.joinpath(*pure.parts).resolve()
    if not path.is_relative_to(resolved_root):
        raise ValueError("path_outside_root")
    if must_exist and not path.is_file():
        raise ValueError("input_missing")
    return path


def read_json(path: Path, *, max_bytes: int = 1024 * 1024) -> dict[str, Any]:
    if path.stat().st_size > max_bytes:
        raise ValueError("json_oversized")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("json_object_required")
    return value


def parse_stamp(value: Any) -> datetime:
    if not isinstance(value, str):
        value = value.isoformat() if hasattr(value, "isoformat") else ""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp_timezone_required")
    return parsed.astimezone(timezone.utc)


def finite(value: Any, *, positive: bool = False) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError("number_invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("number_invalid") from None
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError("number_invalid")
    return result


def optional_finite(value: Any) -> Decimal | None:
    return None if value is None else finite(value)


def optional_percentile(value: Any) -> Decimal | None:
    result = optional_finite(value)
    if result is not None and not Decimal("0") <= result <= Decimal("100"):
        raise ValueError("percentile_out_of_range")
    return result


def symbol_allowed(symbol: str, prefixes: list[str]) -> bool:
    if not isinstance(symbol, str) or not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
        raise ValueError("symbol_invalid")
    code, exchange = symbol.split(".")
    if exchange == "SH":
        return any(code.startswith(prefix) for prefix in prefixes[:4])
    if exchange == "SZ":
        return any(code.startswith(prefix) for prefix in prefixes[4:])
    return False


def _write_once(path: Path, value: dict[str, Any]) -> None:
    raw = canonical(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("immutable_output_conflict")
        return
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".m20-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != raw:
                raise ValueError("immutable_output_conflict") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate_protocol(protocol: dict[str, Any]) -> None:
    if protocol.get("schema") != "regis.m20.shadow_baseline_preregistration.v1":
        raise ValueError("protocol_schema_invalid")
    if protocol.get("registered_before_source_download") is not True:
        raise ValueError("protocol_chronology_flag_invalid")
    if protocol.get("source", {}).get("trade_date") != "2026-09-29":
        raise ValueError("protocol_day_invalid")
    if protocol.get("admission") != {
        "research_only": True,
        "model_admitted": False,
        "economic_target_A_validated": False,
        "generates_alert": False,
        "changes_trade_authority": False,
        "website_or_cloud_use": False,
    }:
        raise ValueError("protocol_admission_invalid")
    if protocol.get("capital_and_execution", {}).get("hypothetical_initial_cash_cny_each_arm") != 150000:
        raise ValueError("capital_invalid")
    if protocol.get("selection", {}).get("maximum_names") != 5:
        raise ValueError("selection_budget_invalid")


def load_rows(root: Path, protocol: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source_path = bounded_path(root, SOURCE_PATH)
    receipt = read_json(bounded_path(root, SOURCE_RECEIPT_PATH))
    if source_path.stat().st_size > protocol["source"]["max_bytes"] or source_path.stat().st_size > MAX_BYTES:
        raise ValueError("source_oversized")
    source_sha = digest_file(source_path)
    expected_receipt = {
        "schema", "url", "fetched_at_utc", "http_status", "content_type", "bytes",
        "sha256", "local_path", "download_count",
    }
    if set(receipt) != expected_receipt:
        raise ValueError("source_receipt_schema_invalid")
    if (
        receipt["url"] != protocol["source"]["download_url"]
        or receipt["http_status"] != 200
        or receipt["bytes"] != source_path.stat().st_size
        or receipt["sha256"] != source_sha
        or receipt["local_path"] != SOURCE_PATH
        or receipt["download_count"] != 1
        or str(receipt["content_type"]).lower().split(";", 1)[0].strip()
        not in {"application/octet-stream", "application/x-parquet"}
        or parse_stamp(receipt["fetched_at_utc"]) <= parse_stamp(protocol["registered_at_utc"])
    ):
        raise ValueError("source_receipt_identity_invalid")
    sys.path.insert(0, str(root / ".codex-local/deps"))
    import pyarrow  # type: ignore[import-not-found]  # noqa: PLC0415
    import pyarrow.parquet as pq  # type: ignore[import-not-found]  # noqa: PLC0415
    if pyarrow.__version__ != "25.0.0":
        raise ValueError("pyarrow_version_mismatch")
    table = pq.read_table(source_path)
    if table.num_rows <= 0 or table.num_rows > MAX_ROWS or not REQUIRED.issubset(table.column_names):
        raise ValueError("source_schema_or_rows_invalid")
    rows = table.select(sorted(REQUIRED)).to_pylist()
    return rows, {**receipt, "protocol_sha256": digest_file(bounded_path(root, PROTOCOL_PATH))}


def build_origin(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    protocol_path = bounded_path(root, PROTOCOL_PATH)
    protocol = read_json(protocol_path)
    validate_protocol(protocol)
    rows, receipt = load_rows(root, protocol)
    expected_day = date.fromisoformat(protocol["source"]["trade_date"])
    expected_version = protocol["source"]["expected_data_version"]
    generated_after = datetime.combine(expected_day, time(15, 0), SH_TZ).astimezone(timezone.utc)
    generated_before = parse_stamp(protocol["chronology"]["decision_information_cutoff"])
    prefixes = protocol["universe"]["allowed_symbol_prefixes"]
    seen: set[str] = set()
    eligible: list[dict[str, Any]] = []
    full_population = 0
    valid_trade_change = 0
    for raw in rows:
        if set(raw) != REQUIRED:
            raise ValueError("row_schema_invalid")
        raw_day = raw["trade_date"]
        if isinstance(raw_day, datetime):
            raw_day = raw_day.date()
        elif isinstance(raw_day, str):
            raw_day = date.fromisoformat(raw_day)
        if raw_day != expected_day or raw["data_version"] != expected_version:
            raise ValueError("row_day_or_version_invalid")
        generated_at = parse_stamp(raw["generated_at"])
        if not generated_after <= generated_at <= generated_before:
            raise ValueError("row_generated_outside_frozen_window")
        if generated_at > parse_stamp(receipt["fetched_at_utc"]):
            raise ValueError("row_generated_after_download")
        symbol = raw["symbol"]
        allowed = symbol_allowed(symbol, prefixes)
        if symbol in seen:
            raise ValueError("duplicate_symbol")
        seen.add(symbol)
        full_population += 1
        is_st = raw["is_st"]
        if type(is_st) is not bool:
            raise ValueError("is_st_invalid")
        close = finite(raw["close"], positive=True)
        pct = optional_finite(raw["pct_chg"])
        optional_finite(raw["circ_mv"])
        turnr = optional_percentile(raw["turnr_pct"])
        illiq = optional_percentile(raw["illiq_pct"])
        if not allowed or is_st or pct is None:
            continue
        valid_trade_change += 1
        eligible.append({
            "symbol": symbol,
            "name": str(raw["name"]),
            "close": str(close),
            "pct_chg": str(pct),
            "turnr_pct": None if turnr is None else str(turnr),
            "illiq_pct": None if illiq is None else str(illiq),
        })
    if full_population != len(rows) or valid_trade_change == 0:
        raise ValueError("population_invalid")
    returns = sorted(Decimal(row["pct_chg"]) for row in eligible)
    advancers = sum(value > 0 for value in returns)
    median = returns[len(returns) // 2] if len(returns) % 2 else (returns[len(returns)//2 - 1] + returns[len(returns)//2]) / 2
    breadth = Decimal(advancers) / Decimal(len(returns))
    gate_open = breadth >= Decimal("0.50") and median > 0
    negative = sorted(value for value in returns if value < 0)
    cutoff = None if not negative else negative[max(0, math.ceil(len(negative) * 0.20) - 1)]
    candidates = []
    if gate_open and cutoff is not None:
        filtered = [row for row in eligible if (
            Decimal(row["pct_chg"]) <= cutoff
            and row["turnr_pct"] is not None and Decimal(row["turnr_pct"]) >= 50
            and row["illiq_pct"] is not None and Decimal(row["illiq_pct"]) <= 50
        )]
        candidates = sorted(
            filtered,
            key=lambda row: (Decimal(row["pct_chg"]), -Decimal(row["turnr_pct"]), row["symbol"]),
        )[: protocol["selection"]["maximum_names"]]
    candidate_identity = [
        {"symbol": row["symbol"], "prior_close": row["close"], "pct_chg": row["pct_chg"],
         "turnr_pct": row["turnr_pct"], "illiq_pct": row["illiq_pct"]}
        for row in candidates
    ]
    origin_base = {
        "schema": "regis.m20.shadow_origin.v1",
        "created_from_source_fetched_at_utc": receipt["fetched_at_utc"],
        "protocol": {"path": PROTOCOL_PATH, "sha256": receipt["protocol_sha256"]},
        "source": {"path": SOURCE_PATH, "sha256": receipt["sha256"], "bytes": receipt["bytes"],
                   "fetched_at_utc": receipt["fetched_at_utc"]},
        "trade_date": protocol["source"]["trade_date"],
        "first_shadow_entry_session": protocol["chronology"]["first_shadow_entry_session"],
        "full_population_count": full_population,
        "tradable_non_st_return_count": len(returns),
        "market_gate": {
            "advancer_count": advancers,
            "advancer_fraction": str(breadth),
            "median_pct_chg": str(median),
            "open": gate_open,
        },
        "negative_return_p20_cutoff": None if cutoff is None else str(cutoff),
        "candidate_count": len(candidate_identity),
        "candidates": candidate_identity,
        "benchmark": protocol["capital_and_execution"]["benchmark_arm"],
        "cash_cny_each_arm": 150000,
        **BOUNDARY,
    }
    origin = {**origin_base, "origin_sha256": digest_bytes(canonical(origin_base))}
    candidate_sha = digest_bytes(canonical(candidate_identity))
    summary = {
        "schema": "regis.m20.shadow_origin_summary.v1",
        "protocol_sha256": receipt["protocol_sha256"],
        "source_sha256": receipt["sha256"],
        "source_bytes": receipt["bytes"],
        "source_fetched_at_utc": receipt["fetched_at_utc"],
        "trade_date": origin["trade_date"],
        "first_shadow_entry_session": origin["first_shadow_entry_session"],
        "post_entry_exit_sessions": protocol["chronology"]["post_entry_exit_sessions"],
        "full_population_count": full_population,
        "tradable_non_st_return_count": len(returns),
        "market_gate_open": gate_open,
        "candidate_count": len(candidate_identity),
        "candidate_identity_sha256": candidate_sha,
        "local_origin_sha256": digest_bytes(canonical(origin)),
        "symbols_or_prices_published": False,
        "next_required_event": "Record reachable 2026-10-08 09:35 quotes/fills, then fixed 1/3/5-session exits; do not revise this origin.",
        **BOUNDARY,
    }
    return origin, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(ROOT))
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if root != ROOT:
        raise SystemExit("root_mismatch")
    try:
        origin, summary = build_origin(root)
        _write_once(bounded_path(root, ORIGIN_PATH, must_exist=False), origin)
        _write_once(bounded_path(root, SUMMARY_PATH, must_exist=False), summary)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "rejected", "reason": str(exc), **BOUNDARY}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
