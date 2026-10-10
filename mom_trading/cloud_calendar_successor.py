"""Review and atomically publish the receipted 2026-10-31 calendar successor.

The CLI defaults to a read-only check. It never fetches provider data, guesses
weekdays, rewrites old packets, or grants trading authority. The raw provider
receipt is retained in the cloud runtime rather than copied into Git.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .market_state_daily import PROJECT_ROOT, _atomic_publish
from .cloud_decision_inputs import _replace
from .research_artifact_paths import research_artifact_path


PROTOCOL_RELATIVE = Path(
    "research/SYSTEM_ROUTE_JOINT_V2_FORWARD_PERSISTENCE_PROTOCOL_V2_AMENDMENT_15.json"
)
PROTOCOL_SHA256 = "3d8ebc5f6b2b45ce5418ae7c0d0c26d7b21be2ce7f96ee54510d9a120ea87647"
SEED_RELATIVE = Path("mom_trading/static/reference/current_market_calendar.json")
OLD_SEED_RELATIVE = Path("mom_trading/static/reference/current_market_calendar_20260924.json")
CALENDAR_RELATIVE = Path("reports/current_market_calendar.json")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read(path: Path) -> tuple[bytes, dict]:
    if path.is_symlink():
        raise ValueError("calendar_individual_file_symlink")
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("calendar_document_invalid")
    return raw, value


def _days(start: str, end: str) -> list[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first.isoformat() != start or last.isoformat() != end or first > last:
        raise ValueError("calendar_range_invalid")
    return [(first + timedelta(days=i)).isoformat()
            for i in range((last - first).days + 1)]


def verify_calendar_successor(
    *, root: Path = PROJECT_ROOT,
    protocol_path: Path | None = None,
    expected_protocol_sha256: str = PROTOCOL_SHA256,
) -> dict:
    """Check exact source, old runtime and immutable provider response; no writes."""
    try:
        protocol_path = protocol_path or root / PROTOCOL_RELATIVE
        protocol_raw, protocol = _read(protocol_path)
        if _sha(protocol_raw) != expected_protocol_sha256:
            raise ValueError("calendar_protocol_sha_invalid")
        if protocol.get("protocol_id") != "system_route_joint_v2_forward_persistence_protocol_v2_amendment_15":
            raise ValueError("calendar_protocol_id_invalid")
        scope = protocol["validation_scope"]
        if scope.get("source_seed_path") != SEED_RELATIVE.as_posix():
            raise ValueError("calendar_seed_path_invalid")
        bound = protocol["bindings"]["audited_calendar_artifact"]
        if bound["path"] != CALENDAR_RELATIVE.as_posix() or bound["sha256"] != scope["source_seed_runtime_sha256"]:
            raise ValueError("calendar_protocol_binding_invalid")

        seed_raw, seed = _read(root / SEED_RELATIVE)
        proposed = seed_raw.removesuffix(b"\n")
        if _sha(proposed) != bound["sha256"]:
            raise ValueError("calendar_seed_sha_invalid")
        old_seed_raw, old_seed = _read(root / OLD_SEED_RELATIVE)
        if _sha(old_seed_raw.removesuffix(b"\n")) != scope["old_seed_runtime_sha256"]:
            raise ValueError("calendar_old_seed_sha_invalid")
        if (old_seed.get("source") != "BaoStock query_trade_dates"
                or old_seed.get("coverage_end") != "2026-09-24"
                or len(old_seed.get("trading_dates") or []) != 279):
            raise ValueError("calendar_old_seed_content_invalid")

        receipt_ref = scope["raw_provider_receipt"]
        receipt_path = research_artifact_path(receipt_ref["path"], project_root=root)
        receipt_raw, receipt = _read(receipt_path)
        if _sha(receipt_raw) != receipt_ref["sha256"]:
            raise ValueError("calendar_receipt_sha_invalid")
        days = _days("2026-09-26", scope["coverage_end"])
        rows = receipt.get("rows")
        if (receipt.get("schema") != "cloud_daily_epochs.v1"
                or receipt.get("implementation_sha256") != _sha((root / "mom_trading/cloud_daily_epochs.py").read_bytes())
                or receipt.get("provider") != "BaoStock"
                or receipt.get("endpoint") != "query_trade_dates"
                or receipt.get("start_date") != days[0]
                or receipt.get("end_date") != days[-1]
                or receipt.get("fields") != ["calendar_date", "is_trading_day"]
                or receipt.get("error_code") != "0"
                or receipt.get("deadline_expired") is not False
                or receipt.get("page_rows") != len(days)
                or receipt.get("cursor") != len(days)
                or receipt.get("page_number") != "1"
                or not isinstance(rows, list) or len(rows) != len(days)
                or any(row != [day, "0"] and row != [day, "1"]
                       for row, day in zip(rows, days))):
            raise ValueError("calendar_receipt_incomplete")
        started = datetime.fromisoformat(receipt["started_at"])
        completed = datetime.fromisoformat(receipt["completed_at"])
        if (started.tzinfo is None or completed.tzinfo is None
                or started > completed
                or seed.get("generated_at") != completed.astimezone(ZoneInfo("Asia/Shanghai")).isoformat()):
            raise ValueError("calendar_receipt_time_invalid")
        future = [row[0] for row in rows if row[1] == "1"]
        if _sha("\n".join(future).encode()) != scope["future_trading_dates_sha256"]:
            raise ValueError("calendar_future_dates_sha_invalid")
        observed = dict(rows)
        if (observed.get("2026-09-27") != "0"
                or observed.get("2026-09-28") != "1"
                or any(observed.get(f"2026-10-{d:02d}") != "0" for d in range(1, 8))
                or observed.get("2026-10-08") != "1"):
            raise ValueError("calendar_exchange_notice_mismatch")
        if (seed.get("source") != old_seed["source"]
                or seed.get("coverage_start") != old_seed["coverage_start"]
                or seed.get("coverage_end") != scope["coverage_end"]
                or seed.get("price_basis_date") != "2026-09-24"
                or seed.get("source_receipt") != receipt_ref
                or seed.get("trading_dates") != old_seed["trading_dates"] + future):
            raise ValueError("calendar_seed_content_invalid")

        calendar_path = research_artifact_path(CALENDAR_RELATIVE.as_posix(), project_root=root)
        current_raw, current = _read(calendar_path)
        current_sha = _sha(current_raw)
        if current_sha not in (scope["expected_previous_runtime_sha256"], bound["sha256"]):
            raise ValueError("calendar_runtime_unexpected_identity")
        if current_sha == scope["expected_previous_runtime_sha256"]:
            if (current.get("source") != old_seed["source"]
                    or current.get("coverage_start") != old_seed["coverage_start"]
                    or current.get("coverage_end") != "2026-09-26"
                    or current.get("trading_dates") != old_seed["trading_dates"]):
                raise ValueError("calendar_runtime_previous_content_invalid")
        elif current != seed:
            raise ValueError("calendar_runtime_new_content_invalid")
        return {"status": "already_published" if current_sha == bound["sha256"] else "ready_to_publish",
                "current_sha256": current_sha, "new_sha256": bound["sha256"],
                "receipt_sha256": receipt_ref["sha256"], "future_trading_dates": len(future),
                "coverage_end": seed["coverage_end"]}
    except (OSError, ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError) as exc:
        return {"status": "blocked", "reason": str(exc)}


def publish_calendar_successor(
    *, root: Path = PROJECT_ROOT, protocol_path: Path | None = None,
    expected_protocol_sha256: str = PROTOCOL_SHA256,
) -> dict:
    checked = verify_calendar_successor(
        root=root, protocol_path=protocol_path,
        expected_protocol_sha256=expected_protocol_sha256,
    )
    if checked["status"] != "ready_to_publish":
        return checked
    current_path = research_artifact_path(CALENDAR_RELATIVE.as_posix(), project_root=root)
    previous_raw = current_path.read_bytes()
    if _sha(previous_raw) != checked["current_sha256"]:
        raise ValueError("calendar_changed_during_publish")
    archive = root / "reports/cloud_decision_inputs/calendar_versions" / (checked["current_sha256"] + ".json")
    _atomic_publish(archive, previous_raw)
    seed_raw = (root / SEED_RELATIVE).read_bytes().removesuffix(b"\n")
    if _sha(seed_raw) != checked["new_sha256"] or current_path.read_bytes() != previous_raw:
        raise ValueError("calendar_changed_during_publish")
    _replace(current_path, seed_raw)
    if _sha(current_path.read_bytes()) != checked["new_sha256"]:
        raise ValueError("calendar_publish_readback_failed")
    return {**checked, "status": "published", "archive": str(archive.relative_to(root))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true", help="atomically replace only the exact reviewed old calendar")
    args = parser.parse_args()
    result = publish_calendar_successor() if args.publish else verify_calendar_successor()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"ready_to_publish", "already_published", "published"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
