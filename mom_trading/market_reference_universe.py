"""Bounded, non-authoritative A-share code reference for market observations.

This is a *current code list*, not a point-in-time security master, exchange
completeness proof, quote timestamp, or research input. Runtime data stays out
of Git. A failed capture leaves the previous file untouched.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4
from zoneinfo import ZoneInfo


SH_TZ = ZoneInfo("Asia/Shanghai")
SCHEMA = "market_reference_universe.v1"
PROVIDER = "AKShare stock_zh_a_spot (Sina A-share snapshot)"
MAX_BYTES = 256_000
MAX_AGE = timedelta(days=7)
_CODE = re.compile(r"(?:sh6\d{5}|sz[03]\d{5}|bj(?:[48]\d{5}|920\d{3}))")


def is_a_share_numeric_code(code: str) -> bool:
    """Only a syntactic guard; an exchange membership check is still absent."""
    if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
        return False
    return bool(re.fullmatch(r"(?:[03468]\d{5}|920\d{3})", code))


def _canonical_symbols(rows, *, minimum: int) -> tuple[list[str], dict[str, int]]:
    symbols: set[str] = set()
    segments = {"sh": 0, "sz": 0, "bj": 0}
    seen_rows = 0
    for row in rows:
        seen_rows += 1
        symbol = str(row.get("代码") or "").strip().lower() if isinstance(row, dict) else ""
        if not _CODE.fullmatch(symbol) or symbol in symbols:
            raise ValueError("reference_universe_invalid_or_duplicate_code")
        symbols.add(symbol)
        segments[symbol[:2]] += 1
    if seen_rows < minimum or not all(segments.values()):
        raise ValueError("reference_universe_partial_segments_or_count")
    return sorted(symbols), segments


def _digest(symbols: list[str]) -> str:
    return hashlib.sha256("\n".join(symbols).encode("ascii")).hexdigest()


def build_snapshot(rows, *, captured_at: datetime, version: str,
                   minimum: int = 5000) -> dict:
    if captured_at.tzinfo is None:
        raise ValueError("reference_capture_timezone_missing")
    symbols, segments = _canonical_symbols(rows, minimum=minimum)
    return {
        "schema": SCHEMA, "provider": PROVIDER, "provider_version": version,
        "captured_at": captured_at.astimezone(SH_TZ).isoformat(timespec="seconds"),
        "capture_time_is_quote_time": False,
        "exchange_coverage_status": "unknown",
        "symbols": symbols, "symbol_count": len(symbols),
        "segments": segments, "symbols_sha256": _digest(symbols),
    }


def load_snapshot(path: Path, *, now: datetime | None = None,
                  minimum: int = 5000) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("reference_universe_file_missing_or_oversize")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != SCHEMA or value.get("provider") != PROVIDER:
        raise ValueError("reference_universe_schema_or_provider_invalid")
    captured = datetime.fromisoformat(value["captured_at"])
    if captured.tzinfo is None:
        raise ValueError("reference_capture_timezone_missing")
    now = (now or datetime.now(SH_TZ)).astimezone(SH_TZ)
    age = now - captured.astimezone(SH_TZ)
    if age < -timedelta(minutes=5) or age > MAX_AGE:
        raise ValueError("reference_universe_expired_or_future")
    if value.get("capture_time_is_quote_time") is not False or value.get("exchange_coverage_status") != "unknown":
        raise ValueError("reference_universe_claim_invalid")
    symbols, segments = _canonical_symbols(
        ({"代码": item} for item in value.get("symbols", [])), minimum=minimum,
    )
    if (value["symbols"] != symbols or value.get("symbol_count") != len(symbols)
            or value.get("segments") != segments or value.get("symbols_sha256") != _digest(symbols)):
        raise ValueError("reference_universe_integrity_mismatch")
    return value


def capture(path: Path) -> dict:
    """Explicit, infrequent capture. No background network call on page load."""
    import akshare as ak

    frame = ak.stock_zh_a_spot()
    snapshot = build_snapshot(
        frame.to_dict("records"), captured_at=datetime.now(SH_TZ),
        version=str(getattr(ak, "__version__", "unknown")),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        load_snapshot(temporary)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return {key: snapshot[key] for key in (
        "schema", "provider", "provider_version", "captured_at", "symbol_count", "segments", "symbols_sha256",
        "exchange_coverage_status", "capture_time_is_quote_time",
    )}


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    summary = capture(root / "reports" / "market_reference_universe.json")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
