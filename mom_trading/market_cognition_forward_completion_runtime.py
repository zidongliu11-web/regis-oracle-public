"""Fail-closed close valuation runtime for strict market-cognition sessions.

The existing quote/candle providers expose normalized objects, not the raw
provider response bytes required by the frozen session protocol.  This module
therefore accepts two explicit source-bound collectors.  Without both a raw
completed-close collector and a raw corporate-action collector it reports a
blocker and writes nothing.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import math
import threading
from datetime import datetime, time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .market_cognition_forward_session import (
    INPUT_DIR,
    SESSION_DIR,
    assemble_strict_forward_input,
    assemble_strict_forward_session,
    verify_strict_forward_input,
    verify_strict_forward_session,
)
from .market_cognition_forward_sources import LIVE_PARSERS
from .market_state_daily import (
    PROJECT_ROOT,
    SnapshotConflict,
    _atomic_publish,
    _canonical_bytes,
    _sha256,
)
from .strict_joint_forward_adapter import (
    AUDIT_DIR,
    verify_strict_joint_forward_audit,
)
from .verified_portfolio_execution_forward import verify_forward_execution_packet


SH_TZ = ZoneInfo("Asia/Shanghai")
SESSION_CLOSE = time(15, 0)
CALENDAR_PATH = Path("reports/current_market_calendar.json")
PROTOCOL_PATH = Path("research/MARKET_COGNITION_FORWARD_SESSION_PROTOCOL_V1.json")
PROTOCOL_SHA256 = "8fb761b0cac5a359428b5453aefc38844856e8251bb8725401ef9ee17ef34e46"
SOURCE_ADMISSION_PATH = Path(
    "research/MARKET_COGNITION_FORWARD_SOURCE_ADMISSION_V2.json"
)
SOURCE_ADMISSION_SHA256 = (
    "e78532a70bb4cdcefb28d5a5bb68d7c510a46ab59e16678f4d119b6fd0c3fa47"
)
EVIDENCE_DIR = Path(
    "research/data/market_cognition_system_replay/v1/forward_completion_evidence"
)
_ELIGIBLE_AUDIT_STATUSES = {"audit_complete", "expected_no_trade_zero_signal"}
_SOURCE_ROLES = (
    "completed_session_prices", "corporate_actions", "trading_calendar",
)


def _authority() -> dict[str, bool]:
    return {
        "research_only": True,
        "changes_live_score": False,
        "changes_live_trade_count": False,
        "changes_live_position_or_quantity": False,
        "generates_buy_or_sell_alert": False,
        "updates_verified_paper_state": False,
        "production_strategy": False,
        "automatic_trading": False,
        "stable_profit_claim": False,
    }


def _identity(value: dict[str, Any], field: str) -> str:
    body = copy.deepcopy(value)
    body.pop(field, None)
    return hashlib.sha256(_canonical_bytes(body)).hexdigest()


def _parse_test_completed_close_json(raw: bytes) -> dict[str, Any]:
    """Parse the canonical JSON used only by isolated contract tests."""
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("completed_session_price:raw_object_required")
    return {
        "provider": value.get("provider"),
        "provider_endpoint": value.get("provider_endpoint"),
        "source_as_of": value.get("source_as_of"),
        "prices_yuan": value.get("prices_yuan"),
    }


def _parse_test_corporate_actions_json(raw: bytes) -> dict[str, Any]:
    """Parse the canonical JSON used only by isolated contract tests."""
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("corporate_action:raw_object_required")
    return {
        "provider": value.get("provider"),
        "provider_endpoint": value.get("provider_endpoint"),
        "source_as_of": value.get("source_as_of"),
        "coverage_codes": value.get("coverage_codes"),
        "coverage_complete": value.get("coverage_complete"),
        "actions": value.get("actions"),
    }


def _parse_test_calendar_json(raw: bytes) -> dict[str, Any]:
    """Parse the canonical JSON used only by isolated contract tests."""
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("calendar:raw_object_required")
    return {
        "provider": value.get("source"),
        "provider_endpoint": value.get("provider_endpoint"),
        "coverage_start": value.get("coverage_start") or value.get("start_date"),
        "coverage_end": value.get("coverage_end") or value.get("end_date"),
        "trading_dates": value.get("trading_dates"),
    }


_TEST_ONLY_PARSERS: dict[str, Callable[[bytes], dict[str, Any]]] = {
    "test_completed_close_json_v1": _parse_test_completed_close_json,
    "test_corporate_actions_json_v1": _parse_test_corporate_actions_json,
    "test_calendar_json_v1": _parse_test_calendar_json,
}
def _parser_ref(parser_id: str) -> dict[str, Any]:
    function = _TEST_ONLY_PARSERS.get(parser_id) or LIVE_PARSERS.get(parser_id)
    if function is None:
        raise ValueError("source_parser_not_implemented")
    source = inspect.getsource(function).encode("utf-8")
    return {
        "parser_id": parser_id,
        "module": function.__module__,
        "function": function.__name__,
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "test_only": parser_id in _TEST_ONLY_PARSERS,
    }


def _is_live_root(root: Path) -> bool:
    return root.resolve() == PROJECT_ROOT.resolve()


def _load_source_admission(
    *, root: Path, path: Path, expected_sha256: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    resolved = path if path.is_absolute() else root / path
    admission, admission_ref, _ = _load_ref(
        {"path": str(resolved.resolve().relative_to(root.resolve())),
         "sha256": expected_sha256},
        root=root, label="forward_source_admission",
    )
    status = admission.get("status")
    contract = (
        admission.get("schema_version"), admission.get("protocol_id"), status,
    )
    if (
        contract not in {
            (1, "market_cognition_forward_source_admission_v1", "frozen_no_source_admitted"),
            (1, "market_cognition_forward_source_admission_v1", "frozen_test_source_admission"),
            (2, "market_cognition_forward_source_admission_v2", "frozen_live_source_admission"),
        }
        or admission.get("no_backfill") is not True
        or admission.get("authority") != _authority()
        or not isinstance(admission.get("sources"), dict)
    ):
        raise ValueError("forward_source_admission_contract_invalid")
    if _is_live_root(root):
        official = (root / SOURCE_ADMISSION_PATH).resolve()
        if (
            resolved.resolve() != official
            or expected_sha256 != SOURCE_ADMISSION_SHA256
            or admission_ref != _ref(official, root)
            or status != "frozen_live_source_admission"
        ):
            raise ValueError("live_source_admission_override_forbidden")
    for role in _SOURCE_ROLES:
        source = admission["sources"].get(role)
        if source is None:
            continue
        if not isinstance(source, dict):
            raise ValueError(f"{role}:source_admission_invalid")
        parser = source.get("parser_ref")
        if (
            not str(source.get("provider") or "").strip()
            or not str(source.get("provider_endpoint") or "").strip()
            or not isinstance(parser, dict)
            or parser != _parser_ref(str(parser.get("parser_id") or ""))
            or (
                _is_live_root(root)
                and parser.get("test_only") is not False
            )
        ):
            raise ValueError(f"{role}:source_or_parser_not_admitted")
    if status == "frozen_live_source_admission" and any(
        not isinstance(admission["sources"].get(role), dict)
        for role in _SOURCE_ROLES
    ):
        raise ValueError("live_source_admission_incomplete")
    return admission, admission_ref


def _parse_admitted_source(
    raw: bytes, *, role: str, source: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    parser = source.get("parser_ref")
    expected = _parser_ref(str((parser or {}).get("parser_id") or ""))
    if parser != expected:
        raise ValueError(f"{role}:parser_ref_invalid")
    try:
        function = (
            _TEST_ONLY_PARSERS.get(expected["parser_id"])
            or LIVE_PARSERS[expected["parser_id"]]
        )
        parsed = function(raw)
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{role}:raw_parse_failed") from exc
    if (
        parsed.get("provider") != source.get("provider")
        or parsed.get("provider_endpoint") != source.get("provider_endpoint")
    ):
        raise ValueError(f"{role}:provider_identity_mismatch")
    return parsed, expected


def _local(value: datetime | None) -> datetime:
    current = value or datetime.now(SH_TZ)
    return (
        current.replace(tzinfo=SH_TZ)
        if current.tzinfo is None else current.astimezone(SH_TZ)
    )


def _time(value: object, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label}:time_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label}:timezone_required")
    return parsed.astimezone(SH_TZ)


def _path(value: object, root: Path) -> Path:
    raw = Path(str(value or ""))
    resolved = (raw if raw.is_absolute() else root / raw).resolve()
    resolved.relative_to(root.resolve())
    return resolved


def _ref(path: Path, root: Path) -> dict[str, str]:
    resolved = path.resolve()
    resolved.relative_to(root.resolve())
    return {
        "path": str(resolved.relative_to(root.resolve())),
        "sha256": _sha256(resolved),
    }


def _load_ref(
    value: object, *, root: Path, label: str,
) -> tuple[dict[str, Any], dict[str, str], Path]:
    row = value if isinstance(value, dict) else {}
    try:
        path = _path(row.get("path"), root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label}:path_invalid") from exc
    expected = str(row.get("sha256") or "")
    if not path.is_file() or not expected or _sha256(path) != expected:
        raise ValueError(f"{label}:sha256_invalid")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}:json_invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label}:object_required")
    return payload, _ref(path, root), path


def _load_binary_ref(
    value: object, *, root: Path, label: str,
) -> tuple[bytes, dict[str, str], Path]:
    row = value if isinstance(value, dict) else {}
    try:
        path = _path(row.get("path"), root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label}:path_invalid") from exc
    expected = str(row.get("sha256") or "")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{label}:unreadable") from exc
    if not expected or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError(f"{label}:sha256_invalid")
    normalized = _ref(path, root)
    if row != normalized:
        raise ValueError(f"{label}:ref_not_canonical")
    return raw, normalized, path


def _finite_price(value: object, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}:price_invalid") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{label}:price_invalid")
    return result


def _calendar(
    root: Path, trade_date: str, *, source: dict[str, Any],
) -> tuple[dict[str, Any], bytes, Path, dict[str, Any]]:
    path = root / CALENDAR_PATH
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError("calendar_unreadable") from exc
    calendar, parser_ref = _parse_admitted_source(
        raw, role="trading_calendar", source=source,
    )
    dates = calendar.get("trading_dates")
    if (
        not isinstance(dates, list)
        or not dates
        or dates != sorted(set(dates))
        or calendar.get("coverage_start") != dates[0]
        or calendar.get("coverage_end") != dates[-1]
    ):
        raise ValueError("calendar_identity_invalid")
    if trade_date not in dates:
        raise ValueError("not_a_trading_day")
    return calendar, raw, path, parser_ref


def _verify_execution(
    audit: dict[str, Any], *, root: Path,
    execution_verifier: Callable[..., dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, str]]:
    execution, execution_ref, _path_value = _load_ref(
        audit.get("source_execution"), root=root, label="verified_execution",
    )
    checked = execution_verifier(execution, project_root=root)
    if not isinstance(checked, dict):
        raise ValueError("verified_execution_verifier_result_invalid")
    verified = checked.get("packet")
    if checked.get("valid") is not True or not isinstance(verified, dict):
        raise ValueError(
            "verified_execution_invalid:"
            + ",".join(str(value) for value in checked.get("errors") or [])
        )
    if verified.get("trade_date") != (verified.get("state_after") or {}).get(
        "last_trade_date"
    ):
        raise ValueError("verified_execution_trade_date_invalid")
    return verified, execution_ref


def _audit_candidate(
    value: object, *, root: Path,
    execution_verifier: Callable[..., dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any], dict[str, str]]:
    audit, audit_ref, audit_path = _load_ref(value, root=root, label="strict_audit")
    checked = verify_strict_joint_forward_audit(audit_path, project_root=root)
    if checked.get("valid") is not True:
        raise ValueError(
            "strict_audit_invalid:"
            + ",".join(str(item) for item in checked.get("errors") or [])
        )
    if audit.get("status") not in _ELIGIBLE_AUDIT_STATUSES:
        raise ValueError("strict_audit_not_session_eligible")
    execution, execution_ref = _verify_execution(
        audit, root=root, execution_verifier=execution_verifier,
    )
    if audit.get("source_execution") != execution_ref:
        raise ValueError("strict_audit_execution_ref_not_canonical")
    return audit, audit_ref, execution, execution_ref


def _discover_audit(
    *, root: Path, audit_ref: dict[str, Any] | None, audit_dir: Path,
    current_date: str, execution_verifier: Callable[..., dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any], dict[str, str]]:
    if audit_ref is not None:
        return _audit_candidate(
            audit_ref, root=root, execution_verifier=execution_verifier,
        )
    matches = []
    directory = root / audit_dir
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            source = raw.get("source_execution") if isinstance(raw, dict) else None
            execution, _normalized, _ = _load_ref(
                source, root=root, label="verified_execution",
            )
            if execution.get("trade_date") != current_date:
                continue
            matches.append(_audit_candidate(
                _ref(path, root), root=root,
                execution_verifier=execution_verifier,
            ))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    if not matches:
        raise LookupError("strict_audit_missing")
    if len(matches) != 1:
        raise ValueError("strict_audit_ambiguous")
    return matches[0]


def _call_collector(
    collector: object, method: str, *, codes: list[str], trade_date: str,
    observed_at: datetime,
) -> dict[str, Any]:
    function = getattr(collector, method, None)
    if function is None and callable(collector):
        function = collector
    if not callable(function):
        reason = (
            "completed_session_price_raw_collector_unsupported"
            if method == "capture_completed_session_prices"
            else "corporate_action_raw_collector_unsupported"
        )
        raise ValueError(reason)
    result = function(
        codes=list(codes), trade_date=trade_date, observed_at=observed_at,
    )
    if not isinstance(result, dict):
        raise ValueError(f"{method}:object_required")
    return result


def _raw(value: dict[str, Any], label: str) -> bytes:
    payload = value.get("raw_response_bytes")
    if not isinstance(payload, bytes) or not payload:
        raise ValueError(f"{label}:raw_response_bytes_required")
    return payload


def _validate_price_capture(
    value: dict[str, Any], *, codes: list[str], trade_date: str,
    first_seen: datetime, source: dict[str, Any],
) -> tuple[dict[str, float], bytes, datetime, dict[str, Any], dict[str, Any]]:
    raw = _raw(value, "completed_session_price")
    parsed, parser_ref = _parse_admitted_source(
        raw, role="completed_session_prices", source=source,
    )
    source_as_of = _time(
        parsed.get("source_as_of"), "completed_session_price:source_as_of",
    )
    close_at = datetime.combine(
        first_seen.date(), SESSION_CLOSE, tzinfo=SH_TZ,
    )
    if not (
        source_as_of.date().isoformat() == trade_date
        and close_at <= source_as_of <= first_seen
    ):
        raise ValueError("completed_session_price:chronology_invalid")
    rows = parsed.get("prices_yuan")
    if not isinstance(rows, dict) or set(rows) != set(codes):
        raise ValueError("completed_session_price:position_coverage_invalid")
    prices = {
        code: _finite_price(rows.get(code), f"completed_session_price:{code}")
        for code in codes
    }
    supplied = value.get("prices_yuan")
    if supplied is not None and supplied != prices:
        raise ValueError("completed_session_price:collector_normalized_mismatch")
    return prices, raw, source_as_of, parsed, parser_ref


def _validate_action_capture(
    value: dict[str, Any], *, codes: list[str], trade_date: str,
    first_seen: datetime, source: dict[str, Any],
) -> tuple[list[dict[str, Any]], bytes, datetime, dict[str, Any], dict[str, Any]]:
    raw = _raw(value, "corporate_action")
    parsed, parser_ref = _parse_admitted_source(
        raw, role="corporate_actions", source=source,
    )
    source_as_of = _time(
        parsed.get("source_as_of"), "corporate_action:source_as_of",
    )
    if not (
        source_as_of.date().isoformat() == trade_date
        and source_as_of <= first_seen
    ):
        raise ValueError("corporate_action:chronology_invalid")
    coverage = parsed.get("coverage_codes")
    if (
        parsed.get("coverage_complete") is not True
        or not isinstance(coverage, list)
        or sorted(set(str(code) for code in coverage)) != codes
    ):
        raise ValueError("corporate_action:position_coverage_incomplete")
    actions = parsed.get("actions")
    if not isinstance(actions, list) or any(not isinstance(row, dict) for row in actions):
        raise ValueError("corporate_action:actions_invalid")
    supplied = value.get("actions")
    if supplied is not None and supplied != actions:
        raise ValueError("corporate_action:collector_normalized_mismatch")
    return copy.deepcopy(actions), raw, source_as_of, parsed, parser_ref


def _freeze_raw(
    raw: bytes, *, kind: str, root: Path, evidence_dir: Path,
) -> dict[str, str]:
    digest = hashlib.sha256(raw).hexdigest()
    path = root / evidence_dir / "raw" / kind / f"{digest}.bin"
    _atomic_publish(path, raw)
    return _ref(path, root)


def _publish_packet(
    packet: dict[str, Any], *, kind: str, root: Path, evidence_dir: Path,
) -> dict[str, str]:
    packet["packet_id"] = _identity(packet, "packet_id")
    path = root / evidence_dir / kind / f"{packet['packet_id']}.json"
    _atomic_publish(path, _canonical_bytes(packet))
    return _ref(path, root)


def _manifest_path(root: Path, evidence_dir: Path, trade_date: str) -> Path:
    return root / evidence_dir / "completions" / f"{trade_date}.json"


def _committed_session_refs(
    *, root: Path, evidence_dir: Path, before_trade_date: str,
    execution_verifier: Callable[..., dict[str, Any]],
    source_admission_ref: dict[str, str],
) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    directory = root / evidence_dir / "completions"
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        checked = _verify_manifest(
            path, root=root, execution_verifier=execution_verifier,
            required_source_admission_ref=source_admission_ref,
        )
        manifest = checked.get("manifest")
        if checked.get("valid") is not True or not isinstance(manifest, dict):
            raise ValueError(f"prior_completion_invalid:{path.name}")
        trade_date = str(manifest.get("trade_date") or "")
        if path.stem != trade_date:
            raise ValueError("prior_completion_filename_date_mismatch")
        if trade_date >= before_trade_date:
            raise ValueError("future_or_duplicate_completion_commit_present")
        ref = manifest.get("session_ref")
        if not isinstance(ref, dict):
            raise ValueError("prior_completion_session_ref_missing")
        refs.append(copy.deepcopy(ref))
    identities = [(row.get("path"), row.get("sha256")) for row in refs]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate_committed_session_ref")
    return refs


def _verify_source_evidence(
    value: dict[str, Any], *, root: Path, trade_date: str,
    execution: dict[str, Any],
    required_source_admission_ref: dict[str, str] | None = None,
) -> None:
    admission_value = value.get("source_admission_ref")
    if not isinstance(admission_value, dict):
        raise ValueError("completion_source_admission_ref_missing")
    admission, admission_ref = _load_source_admission(
        root=root, path=Path(str(admission_value.get("path") or "")),
        expected_sha256=str(admission_value.get("sha256") or ""),
    )
    if (
        admission_value != admission_ref
        or (
            required_source_admission_ref is not None
            and admission_ref != required_source_admission_ref
        )
    ):
        raise ValueError("completion_source_admission_ref_invalid")
    sources = admission["sources"]
    if any(not isinstance(sources.get(role), dict) for role in _SOURCE_ROLES):
        raise ValueError("completion_source_not_admitted")
    positions = (execution.get("state_after") or {}).get("positions")
    if not isinstance(positions, dict):
        raise ValueError("completion_execution_positions_invalid")
    codes = sorted(str(code) for code in positions)
    price, price_ref, _ = _load_ref(
        value.get("price_source_ref"), root=root, label="completion_price_source",
    )
    calendar, calendar_ref, _ = _load_ref(
        value.get("calendar_ref"), root=root, label="completion_calendar",
    )
    actions, action_ref, _ = _load_ref(
        value.get("corporate_action_ref"), root=root,
        label="completion_corporate_action",
    )
    if (
        value.get("price_source_ref") != price_ref
        or value.get("calendar_ref") != calendar_ref
        or value.get("corporate_action_ref") != action_ref
    ):
        raise ValueError("completion_source_ref_not_canonical")
    expected = (
        (price, "market_cognition_completed_session_prices.v1", "price_source"),
        (calendar, "market_cognition_trading_calendar_session.v1", "calendar"),
        (actions, "market_cognition_corporate_action_snapshot.v1", "corporate_action"),
    )
    for packet, schema, label in expected:
        if (
            packet.get("schema") != schema
            or packet.get("trade_date") != trade_date
            or packet.get("no_backfill") is not True
            or packet.get("packet_id") != _identity(packet, "packet_id")
            or packet.get("source_admission_ref") != admission_ref
        ):
            raise ValueError(f"completion_{label}_contract_invalid")
        if not str(packet.get("provider") or "").strip():
            raise ValueError(f"completion_{label}_provider_missing")

    price_raw, price_raw_ref, _ = _load_binary_ref(
        price.get("raw_response_ref"), root=root, label="completion_price_raw",
    )
    if (
        price.get("raw_response_ref") != price_raw_ref
        or price.get("raw_response_sha256") != hashlib.sha256(price_raw).hexdigest()
        or price.get("completed_session") is not True
    ):
        raise ValueError("completion_price_raw_binding_invalid")
    parsed_price, price_parser_ref = _parse_admitted_source(
        price_raw, role="completed_session_prices",
        source=sources["completed_session_prices"],
    )
    parsed_rows = parsed_price.get("prices_yuan")
    if not isinstance(parsed_rows, dict) or set(parsed_rows) != set(codes):
        raise ValueError("completion_price_position_coverage_invalid")
    parsed_prices = {
        code: _finite_price(parsed_rows.get(code), f"completion_price:{code}")
        for code in codes
    }
    replayed_price_as_of = _time(
        parsed_price.get("source_as_of"), "completion_price:source_as_of",
    ).isoformat(timespec="seconds")
    if (
        price.get("parser_ref") != price_parser_ref
        or price.get("parser_identity") != price_parser_ref["parser_id"]
        or price.get("provider") != parsed_price.get("provider")
        or price.get("provider_endpoint") != parsed_price.get("provider_endpoint")
        or price.get("source_as_of") != replayed_price_as_of
        or price.get("requested_codes") != codes
        or price.get("prices_yuan") != parsed_prices
    ):
        raise ValueError("completion_price_normalization_replay_mismatch")

    calendar_raw, calendar_raw_ref, _ = _load_binary_ref(
        calendar.get("raw_calendar_ref"), root=root,
        label="completion_calendar_raw",
    )
    if (
        calendar.get("raw_calendar_ref") != calendar_raw_ref
        or calendar.get("raw_calendar_sha256")
        != hashlib.sha256(calendar_raw).hexdigest()
        or calendar.get("is_trading_day") is not True
        or calendar.get("session_completed") is not True
    ):
        raise ValueError("completion_calendar_raw_binding_invalid")
    parsed_calendar, calendar_parser_ref = _parse_admitted_source(
        calendar_raw, role="trading_calendar",
        source=sources["trading_calendar"],
    )
    frozen_dates = parsed_calendar.get("trading_dates")
    if (
        not isinstance(frozen_dates, list)
        or not frozen_dates
        or frozen_dates != sorted(set(frozen_dates))
        or trade_date not in frozen_dates
        or calendar.get("parser_ref") != calendar_parser_ref
        or calendar.get("parser_identity") != calendar_parser_ref["parser_id"]
        or calendar.get("provider") != parsed_calendar.get("provider")
        or calendar.get("provider_endpoint")
        != parsed_calendar.get("provider_endpoint")
        or calendar.get("coverage_start") != parsed_calendar.get("coverage_start")
        or calendar.get("coverage_end") != parsed_calendar.get("coverage_end")
        or parsed_calendar.get("coverage_start") != frozen_dates[0]
        or parsed_calendar.get("coverage_end") != frozen_dates[-1]
    ):
        raise ValueError("completion_calendar_normalization_invalid")

    action_raw, action_raw_ref, _ = _load_binary_ref(
        actions.get("raw_response_ref"), root=root,
        label="completion_corporate_action_raw",
    )
    if (
        actions.get("raw_response_ref") != action_raw_ref
        or actions.get("raw_response_sha256")
        != hashlib.sha256(action_raw).hexdigest()
    ):
        raise ValueError("completion_corporate_action_raw_binding_invalid")
    parsed_actions, action_parser_ref = _parse_admitted_source(
        action_raw, role="corporate_actions",
        source=sources["corporate_actions"],
    )
    parsed_coverage = parsed_actions.get("coverage_codes")
    parsed_events = parsed_actions.get("actions")
    replayed_action_as_of = _time(
        parsed_actions.get("source_as_of"),
        "completion_corporate_action:source_as_of",
    ).isoformat(timespec="seconds")
    if (
        parsed_actions.get("coverage_complete") is not True
        or not isinstance(parsed_coverage, list)
        or sorted(set(str(code) for code in parsed_coverage)) != codes
        or not isinstance(parsed_events, list)
        or parsed_events
        or actions.get("parser_ref") != action_parser_ref
        or actions.get("parser_identity") != action_parser_ref["parser_id"]
        or actions.get("provider") != parsed_actions.get("provider")
        or actions.get("provider_endpoint")
        != parsed_actions.get("provider_endpoint")
        or actions.get("source_as_of") != replayed_action_as_of
        or actions.get("coverage_codes") != codes
        or actions.get("coverage_complete") is not True
        or actions.get("none_observed_source_bound") is not True
        or actions.get("actions") != []
    ):
        raise ValueError("completion_corporate_action_normalization_replay_mismatch")


def _verify_manifest(
    path: Path, *, root: Path,
    execution_verifier: Callable[..., dict[str, Any]] = verify_forward_execution_packet,
    required_source_admission_ref: dict[str, str] | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("completion_manifest_object_required")
        if (
            value.get("schema") != "market_cognition_forward_completion.v1"
            or value.get("no_backfill") is not True
            or value.get("performance_computed") is not False
            or value.get("authority") != _authority()
            or value.get("completion_id") != _identity(value, "completion_id")
        ):
            raise ValueError("completion_manifest_contract_invalid")
        session, session_ref, session_path = _load_ref(
            value.get("session_ref"), root=root, label="completion_session",
        )
        replay_input, input_ref, input_path = _load_ref(
            value.get("input_ref"), root=root, label="completion_input",
        )
        if (
            verify_strict_forward_session(session_path, project_root=root).get("valid")
            is not True
            or verify_strict_forward_input(input_path, project_root=root).get("valid")
            is not True
        ):
            raise ValueError("completion_session_or_input_invalid")
        if _is_live_root(root):
            official_protocol_ref = _ref(root / PROTOCOL_PATH, root)
            if (
                official_protocol_ref.get("sha256") != PROTOCOL_SHA256
                or session.get("assembly_protocol") != official_protocol_ref
                or replay_input.get("assembly_protocol") != official_protocol_ref
            ):
                raise ValueError("completion_live_protocol_binding_invalid")
        if session_ref not in (replay_input.get("session_refs") or []):
            raise ValueError("completion_session_missing_from_input")
        if value.get("session_ref") != session_ref or value.get("input_ref") != input_ref:
            raise ValueError("completion_ref_not_canonical")
        if (
            value.get("strict_audit_ref") != session.get("strict_audit_ref")
            or value.get("verified_execution_ref")
            != session.get("verified_execution_ref")
            or value.get("after_session_mark_ref")
            != session.get("after_session_mark_ref")
        ):
            raise ValueError("completion_upstream_ref_mismatch")
        evidence = session.get("mark_evidence") or {}
        if any(
            value.get(name) != evidence.get(name)
            for name in (
                "price_source_ref", "calendar_ref", "corporate_action_ref",
            )
        ):
            raise ValueError("completion_mark_evidence_ref_mismatch")
        if value.get("trade_date") != session.get("trade_date"):
            raise ValueError("completion_trade_date_mismatch")
        audit, audit_ref, execution, execution_ref = _audit_candidate(
            value.get("strict_audit_ref"), root=root,
            execution_verifier=execution_verifier,
        )
        if (
            value.get("strict_audit_ref") != audit_ref
            or value.get("verified_execution_ref") != execution_ref
            or session.get("strict_audit_ref") != audit_ref
            or session.get("verified_execution_ref") != execution_ref
            or execution.get("trade_date") != value.get("trade_date")
            or audit.get("status") not in _ELIGIBLE_AUDIT_STATUSES
        ):
            raise ValueError("completion_recursive_execution_chain_invalid")
        _verify_source_evidence(
            value, root=root, trade_date=str(value.get("trade_date") or ""),
            execution=execution,
            required_source_admission_ref=required_source_admission_ref,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(str(exc))
        value = None
    return {"valid": not errors, "errors": errors, "manifest": value}


def _resolved(root: Path, value: Path) -> Path:
    return (value if value.is_absolute() else root / value).resolve()


def _live_schedule_override(
    *, root: Path, now: datetime | None, source_admission_path: Path,
    expected_source_admission_sha256: str,
    execution_verifier: Callable[..., dict[str, Any]],
) -> str | None:
    if not _is_live_root(root):
        return None
    if now is not None:
        return "live_test_clock_override_forbidden"
    if execution_verifier is not verify_forward_execution_packet:
        return "live_execution_verifier_override_forbidden"
    if (
        _resolved(root, source_admission_path)
        != (root / SOURCE_ADMISSION_PATH).resolve()
        or expected_source_admission_sha256 != SOURCE_ADMISSION_SHA256
    ):
        return "live_source_admission_override_forbidden"
    return None


def _live_capture_override(
    *, root: Path, now: datetime | None, protocol_path: Path,
    expected_protocol_sha256: str, source_admission_path: Path,
    expected_source_admission_sha256: str,
    execution_verifier: Callable[..., dict[str, Any]],
) -> str | None:
    error = _live_schedule_override(
        root=root, now=now, source_admission_path=source_admission_path,
        expected_source_admission_sha256=expected_source_admission_sha256,
        execution_verifier=execution_verifier,
    )
    if error:
        return error
    if _is_live_root(root) and (
        _resolved(root, protocol_path) != (root / PROTOCOL_PATH).resolve()
        or expected_protocol_sha256 != PROTOCOL_SHA256
    ):
        return "live_session_protocol_override_forbidden"
    return None


def _collector_supported(collector: object, method: str) -> bool:
    return callable(getattr(collector, method, None)) or callable(collector)


def _source_gaps(
    admission: dict[str, Any], *, completed_price_collector: object,
    corporate_action_collector: object,
) -> list[str]:
    sources = admission.get("sources") or {}
    gaps = [
        f"{role}_source_not_admitted"
        for role in _SOURCE_ROLES if not isinstance(sources.get(role), dict)
    ]
    if isinstance(sources.get("completed_session_prices"), dict):
        if completed_price_collector is None:
            gaps.append("completed_session_price_raw_collector_unavailable")
        elif not _collector_supported(
            completed_price_collector, "capture_completed_session_prices",
        ):
            gaps.append("completed_session_price_raw_collector_unsupported")
    if isinstance(sources.get("corporate_actions"), dict):
        if corporate_action_collector is None:
            gaps.append("corporate_action_raw_collector_unavailable")
        elif not _collector_supported(
            corporate_action_collector, "capture_corporate_actions",
        ):
            gaps.append("corporate_action_raw_collector_unsupported")
    return gaps


def completion_schedule_status(
    *, now: datetime | None = None, project_root: Path = PROJECT_ROOT,
    audit_ref: dict[str, Any] | None = None, audit_dir: Path = AUDIT_DIR,
    evidence_dir: Path = EVIDENCE_DIR,
    completed_price_collector: object = None,
    corporate_action_collector: object = None,
    source_admission_path: Path = SOURCE_ADMISSION_PATH,
    expected_source_admission_sha256: str = SOURCE_ADMISSION_SHA256,
    execution_verifier: Callable[..., dict[str, Any]] = verify_forward_execution_packet,
) -> dict[str, Any]:
    """Return pending/blocked/completed without calling either source."""
    override = _live_schedule_override(
        root=project_root, now=now,
        source_admission_path=source_admission_path,
        expected_source_admission_sha256=expected_source_admission_sha256,
        execution_verifier=execution_verifier,
    )
    if override:
        return {
            "status": "blocked", "reason": override, "research_only": True,
            "no_backfill": True, "performance_computed": False,
            "current_provider_gaps": [],
        }
    observed_at = _local(now)
    trade_date = observed_at.date().isoformat()
    manifest_path = _manifest_path(project_root, evidence_dir, trade_date)
    try:
        admission, admission_ref = _load_source_admission(
            root=project_root, path=source_admission_path,
            expected_sha256=expected_source_admission_sha256,
        )
        gaps = _source_gaps(
            admission, completed_price_collector=completed_price_collector,
            corporate_action_collector=corporate_action_collector,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        admission, admission_ref = {}, None
        gaps = [str(exc)]
    base = {
        "trade_date": trade_date,
        "no_backfill": True,
        "research_only": True,
        "performance_computed": False,
        "changes_live_account": False,
        "generates_alerts": False,
        "automatic_trading": False,
        "stable_profit_proven": False,
        "current_provider_gaps": gaps,
    }
    if manifest_path.exists():
        checked = _verify_manifest(
            manifest_path, root=project_root,
            execution_verifier=execution_verifier,
            required_source_admission_ref=admission_ref,
        )
        return {
            **base,
            "status": "completed" if checked["valid"] else "blocked",
            "reason": (
                "strict_forward_session_and_input_completed"
                if checked["valid"] else "existing_completion_manifest_invalid"
            ),
            "completion_ref": _ref(manifest_path, project_root),
            "errors": checked["errors"],
            "current_provider_gaps": [] if checked["valid"] else gaps,
        }
    if gaps:
        return {**base, "status": "blocked", "reason": gaps[0]}
    try:
        _calendar(
            project_root, trade_date,
            source=admission["sources"]["trading_calendar"],
        )
    except ValueError as exc:
        if str(exc) == "not_a_trading_day":
            return {**base, "status": "pending", "reason": str(exc)}
        return {**base, "status": "blocked", "reason": str(exc)}
    try:
        audit, normalized_audit_ref, execution, execution_ref = _discover_audit(
            root=project_root, audit_ref=audit_ref, audit_dir=audit_dir,
            current_date=trade_date, execution_verifier=execution_verifier,
        )
    except LookupError as exc:
        return {**base, "status": "pending", "reason": str(exc)}
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {**base, "status": "blocked", "reason": str(exc)}
    execution_date = str(execution.get("trade_date") or "")
    if execution_date != trade_date:
        return {
            **base, "status": "blocked", "reason": "backfill_forbidden",
            "execution_trade_date": execution_date,
        }
    close_at = datetime.combine(observed_at.date(), SESSION_CLOSE, tzinfo=SH_TZ)
    if observed_at < close_at:
        return {
            **base, "status": "pending", "reason": "waiting_for_session_close",
            "session_close_at": close_at.isoformat(timespec="seconds"),
            "strict_audit_ref": normalized_audit_ref,
            "verified_execution_ref": execution_ref,
        }
    return {
        **base, "status": "pending", "reason": "ready_for_source_bound_completion",
        "strict_audit_ref": normalized_audit_ref,
        "verified_execution_ref": execution_ref,
        "audit_status": audit.get("status"),
    }


def capture_forward_completion(
    *, completed_price_collector: object,
    corporate_action_collector: object, now: datetime | None = None,
    project_root: Path = PROJECT_ROOT, audit_ref: dict[str, Any] | None = None,
    audit_dir: Path = AUDIT_DIR, evidence_dir: Path = EVIDENCE_DIR,
    protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str = PROTOCOL_SHA256,
    source_admission_path: Path = SOURCE_ADMISSION_PATH,
    expected_source_admission_sha256: str = SOURCE_ADMISSION_SHA256,
    session_dir: Path = SESSION_DIR, input_dir: Path = INPUT_DIR,
    execution_verifier: Callable[..., dict[str, Any]] = verify_forward_execution_packet,
) -> dict[str, Any]:
    """Capture one current-day close and assemble immutable replay evidence."""
    override = _live_capture_override(
        root=project_root, now=now, protocol_path=protocol_path,
        expected_protocol_sha256=expected_protocol_sha256,
        source_admission_path=source_admission_path,
        expected_source_admission_sha256=expected_source_admission_sha256,
        execution_verifier=execution_verifier,
    )
    if override:
        return {
            "status": "blocked", "reason": override, "research_only": True,
            "no_backfill": True, "performance_computed": False,
            "current_provider_gaps": [],
        }
    observed_at = _local(now)
    schedule_now = (
        None if _is_live_root(project_root) and now is None else observed_at
    )
    schedule = completion_schedule_status(
        now=schedule_now, project_root=project_root, audit_ref=audit_ref,
        audit_dir=audit_dir, evidence_dir=evidence_dir,
        completed_price_collector=completed_price_collector,
        corporate_action_collector=corporate_action_collector,
        source_admission_path=source_admission_path,
        expected_source_admission_sha256=expected_source_admission_sha256,
        execution_verifier=execution_verifier,
    )
    if schedule["status"] == "completed":
        return {**schedule, "status": "completed", "reason": "already_completed"}
    if schedule.get("reason") != "ready_for_source_bound_completion":
        return schedule
    trade_date = str(schedule["trade_date"])
    try:
        admission, source_admission_ref = _load_source_admission(
            root=project_root, path=source_admission_path,
            expected_sha256=expected_source_admission_sha256,
        )
        sources = admission["sources"]
        calendar, calendar_raw, calendar_source_path, calendar_parser_ref = _calendar(
            project_root, trade_date, source=sources["trading_calendar"],
        )
        audit, normalized_audit_ref, execution, execution_ref = _discover_audit(
            root=project_root, audit_ref=audit_ref, audit_dir=audit_dir,
            current_date=trade_date, execution_verifier=execution_verifier,
        )
        state_after = execution.get("state_after")
        positions = state_after.get("positions") if isinstance(state_after, dict) else None
        if not isinstance(positions, dict):
            raise ValueError("verified_execution_positions_invalid")
        codes = sorted(str(code) for code in positions)
        price_capture = _call_collector(
            completed_price_collector, "capture_completed_session_prices",
            codes=codes, trade_date=trade_date, observed_at=observed_at,
        )
        price_seen = observed_at if now is not None else _local(None)
        action_capture = _call_collector(
            corporate_action_collector, "capture_corporate_actions",
            codes=codes, trade_date=trade_date, observed_at=observed_at,
        )
        action_seen = observed_at if now is not None else _local(None)
        marked_at = observed_at if now is not None else _local(None)
        if marked_at.date().isoformat() != trade_date:
            raise ValueError("completion_crossed_trade_date")
        prices, price_raw, source_as_of, parsed_price, price_parser_ref = (
            _validate_price_capture(
                price_capture, codes=codes, trade_date=trade_date,
                first_seen=price_seen,
                source=sources["completed_session_prices"],
            )
        )
        actions, action_raw, action_as_of, parsed_actions, action_parser_ref = (
            _validate_action_capture(
                action_capture, codes=codes, trade_date=trade_date,
                first_seen=action_seen, source=sources["corporate_actions"],
            )
        )
        if actions:
            return {
                **schedule, "status": "blocked",
                "reason": "corporate_action_settlement_not_implemented",
                "corporate_action_event_count": len(actions),
            }

        price_raw_ref = _freeze_raw(
            price_raw, kind="completed_session_prices", root=project_root,
            evidence_dir=evidence_dir,
        )
        action_raw_ref = _freeze_raw(
            action_raw, kind="corporate_actions", root=project_root,
            evidence_dir=evidence_dir,
        )
        calendar_raw_ref = _freeze_raw(
            calendar_raw, kind="trading_calendars", root=project_root,
            evidence_dir=evidence_dir,
        )
        price_packet = {
            "schema": "market_cognition_completed_session_prices.v1",
            "trade_date": trade_date,
            "completed_session": True,
            "source_as_of": source_as_of.isoformat(timespec="seconds"),
            "first_seen_at": price_seen.isoformat(timespec="seconds"),
            "no_backfill": True,
            "provider": str(parsed_price["provider"]),
            "provider_endpoint": str(parsed_price["provider_endpoint"]),
            "parser_identity": price_parser_ref["parser_id"],
            "parser_ref": price_parser_ref,
            "source_admission_ref": source_admission_ref,
            "raw_response_ref": price_raw_ref,
            "raw_response_sha256": hashlib.sha256(price_raw).hexdigest(),
            "requested_codes": codes,
            "prices_yuan": prices,
        }
        price_ref = _publish_packet(
            price_packet, kind="prices",
            root=project_root, evidence_dir=evidence_dir,
        )
        calendar_packet = {
            "schema": "market_cognition_trading_calendar_session.v1",
            "trade_date": trade_date,
            "is_trading_day": True,
            "session_completed": True,
            "session_close_at": datetime.combine(
                observed_at.date(), SESSION_CLOSE, tzinfo=SH_TZ,
            ).isoformat(timespec="seconds"),
            "first_seen_at": marked_at.isoformat(timespec="seconds"),
            "no_backfill": True,
            "provider": str(calendar["provider"]),
            "provider_endpoint": str(calendar["provider_endpoint"]),
            "parser_identity": calendar_parser_ref["parser_id"],
            "parser_ref": calendar_parser_ref,
            "source_admission_ref": source_admission_ref,
            "coverage_start": calendar["coverage_start"],
            "coverage_end": calendar["coverage_end"],
            "source_path": str(calendar_source_path.relative_to(project_root)),
            "raw_calendar_ref": calendar_raw_ref,
            "raw_calendar_sha256": hashlib.sha256(calendar_raw).hexdigest(),
        }
        calendar_ref = _publish_packet(
            calendar_packet, kind="calendars",
            root=project_root, evidence_dir=evidence_dir,
        )
        action_packet = {
            "schema": "market_cognition_corporate_action_snapshot.v1",
            "trade_date": trade_date,
            "source_as_of": action_as_of.isoformat(timespec="seconds"),
            "first_seen_at": action_seen.isoformat(timespec="seconds"),
            "actions": actions,
            "no_backfill": True,
            "provider": str(parsed_actions["provider"]),
            "provider_endpoint": str(parsed_actions["provider_endpoint"]),
            "parser_identity": action_parser_ref["parser_id"],
            "parser_ref": action_parser_ref,
            "source_admission_ref": source_admission_ref,
            "coverage_codes": codes,
            "coverage_complete": True,
            "none_observed_source_bound": True,
            "raw_response_ref": action_raw_ref,
            "raw_response_sha256": hashlib.sha256(action_raw).hexdigest(),
        }
        action_ref = _publish_packet(
            action_packet, kind="corporate_actions",
            root=project_root, evidence_dir=evidence_dir,
        )
        mark = {
            "schema": "market_cognition_after_session_valuation.v1",
            "trade_date": trade_date,
            "source_as_of": price_packet["source_as_of"],
            "first_seen_at": price_packet["first_seen_at"],
            "marked_at": marked_at.isoformat(timespec="seconds"),
            "completed_session": True,
            "no_backfill": True,
            "outcome_role": "post_decision_valuation_only",
            "price_semantics": (
                "unadjusted_completed_session_close_with_explicit_corporate_actions"
            ),
            "strict_result_id": (audit.get("strict_result") or {}).get("result_id"),
            "verified_execution_ref": execution_ref,
            "state_id": state_after.get("state_id"),
            "prices_yuan": prices,
            "price_source_ref": price_ref,
            "calendar_ref": calendar_ref,
            "corporate_action_ref": action_ref,
        }
        mark_ref = _publish_packet(
            mark, kind="marks",
            root=project_root, evidence_dir=evidence_dir,
        )
        session = assemble_strict_forward_session(
            normalized_audit_ref, mark_ref, project_root=project_root,
            protocol_path=(
                protocol_path if protocol_path.is_absolute()
                else project_root / protocol_path
            ),
            expected_protocol_sha256=expected_protocol_sha256,
            session_dir=session_dir,
        )
        if session.get("status") not in {"session_assembled", "already_assembled"}:
            raise ValueError(
                "session_assembly_failed:"
                + ",".join(str(item) for item in session.get("errors") or [])
            )
        session_refs = _committed_session_refs(
            root=project_root, evidence_dir=evidence_dir,
            before_trade_date=trade_date,
            execution_verifier=execution_verifier,
            source_admission_ref=source_admission_ref,
        )
        session_refs.append(session["session_ref"])
        replay_input = assemble_strict_forward_input(
            session_refs, project_root=project_root,
            protocol_path=(
                protocol_path if protocol_path.is_absolute()
                else project_root / protocol_path
            ),
            expected_protocol_sha256=expected_protocol_sha256,
            input_dir=input_dir,
        )
        if replay_input.get("status") not in {"input_assembled", "already_assembled"}:
            raise ValueError(
                "input_assembly_failed:"
                + ",".join(str(item) for item in replay_input.get("errors") or [])
            )
        manifest = {
            "schema": "market_cognition_forward_completion.v1",
            "trade_date": trade_date,
            "no_backfill": True,
            "source_admission_ref": source_admission_ref,
            "strict_audit_ref": normalized_audit_ref,
            "verified_execution_ref": execution_ref,
            "price_source_ref": price_ref,
            "calendar_ref": calendar_ref,
            "corporate_action_ref": action_ref,
            "after_session_mark_ref": mark_ref,
            "session_ref": session["session_ref"],
            "input_ref": replay_input["input_ref"],
            "session_count": replay_input["session_count"],
            "returns_or_labels_read": False,
            "performance_computed": False,
            "authority": _authority(),
        }
        manifest["completion_id"] = _identity(manifest, "completion_id")
        candidate_path = (
            project_root / evidence_dir / "manifests"
            / f"{manifest['completion_id']}.json"
        )
        _atomic_publish(candidate_path, _canonical_bytes(manifest))
        candidate_checked = _verify_manifest(
            candidate_path, root=project_root,
            execution_verifier=execution_verifier,
            required_source_admission_ref=source_admission_ref,
        )
        if candidate_checked.get("valid") is not True:
            raise RuntimeError("completion_manifest_candidate_invalid")
        manifest_path = _manifest_path(project_root, evidence_dir, trade_date)
        created = _atomic_publish(manifest_path, _canonical_bytes(manifest))
        checked = _verify_manifest(
            manifest_path, root=project_root,
            execution_verifier=execution_verifier,
            required_source_admission_ref=source_admission_ref,
        )
        if checked.get("valid") is not True:
            raise RuntimeError("completion_manifest_post_write_invalid")
        return {
            **schedule,
            "status": "completed",
            "reason": "completion_assembled" if created else "already_completed",
            "completion_ref": _ref(manifest_path, project_root),
            "session_ref": session["session_ref"],
            "input_ref": replay_input["input_ref"],
            "session_count": replay_input["session_count"],
        }
    except SnapshotConflict as exc:
        return {**schedule, "status": "blocked", "reason": "first_write_conflict", "errors": [str(exc)]}
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            **schedule, "status": "blocked",
            "reason": str(exc),
            "errors": [f"{type(exc).__name__}:{exc}"],
        }
    except Exception as exc:
        return {
            **schedule, "status": "blocked",
            "reason": f"completion_dependency_exception:{type(exc).__name__}",
            "errors": [f"{type(exc).__name__}:{exc}"],
        }


def new_runtime_state() -> dict[str, Any]:
    return {
        "lock": threading.Lock(),
        "running": False,
        "last_result": None,
        "last_error": None,
    }


def run_completion_once(state: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Serialize one scheduler attempt; repeated completed dates are idempotent."""
    with state["lock"]:
        if state.get("running"):
            return {"status": "pending", "reason": "completion_already_running"}
        state["running"] = True
    try:
        result = capture_forward_completion(**kwargs)
        with state["lock"]:
            state["last_result"] = copy.deepcopy(result)
            state["last_error"] = None
        return result
    except Exception as exc:  # Boundary: runtime state must never kill its host loop.
        with state["lock"]:
            state["last_error"] = f"{type(exc).__name__}:{exc}"
        return {"status": "blocked", "reason": "completion_runtime_exception"}
    finally:
        with state["lock"]:
            state["running"] = False


def public_runtime_state(state: dict[str, Any], **schedule_kwargs: Any) -> dict[str, Any]:
    schedule = completion_schedule_status(**schedule_kwargs)
    with state["lock"]:
        running = bool(state.get("running"))
        last_result = copy.deepcopy(state.get("last_result"))
        last_error = state.get("last_error")
    if running:
        status, reason = "pending", "completion_in_progress"
    elif last_error:
        status, reason = "blocked", "completion_runtime_exception"
    else:
        status, reason = schedule["status"], schedule["reason"]
    return {
        "status": status,
        "reason": reason,
        "running": running,
        "schedule": schedule,
        "last_result": last_result,
        "last_error": str(last_error)[:500] if last_error else None,
        "authority": _authority(),
    }


__all__ = [
    "capture_forward_completion", "completion_schedule_status",
    "new_runtime_state", "public_runtime_state", "run_completion_once",
]
