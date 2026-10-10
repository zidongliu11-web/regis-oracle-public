"""Append-only first-seen quote journals for the frozen routed candidate."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from datetime import datetime, time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .mainline_start_candidates import verify_candidate_packet
from .market_sector_route_forward import capture_slot
from .market_state_daily import PROJECT_ROOT, _atomic_publish, _canonical_bytes, _sha256


SH_TZ = ZoneInfo("Asia/Shanghai")
PROTOCOL_PATH = (
    PROJECT_ROOT / "research/LEADER_QUOTE_JOURNAL_PROTOCOL_V1_AMENDMENT_2.json"
)
PROTOCOL_SHA256 = "7c537db389ad6b76ccac29eb46407c33f01b3edd9384f550caed71ccfa250a90"
JOURNAL_DIR = PROJECT_ROOT / "research/data/leader_quote_forward/v1/journals"
_LOCK = threading.Lock()
_AUTHORITY_FALSE = (
    "changes_unified_score", "changes_trade_count", "changes_position_or_quantity",
    "generates_buy_or_sell_alert", "selects_production_candidate",
    "production_strategy", "automatic_trading", "stable_profit_claim",
)


def _time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(SH_TZ)


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _project_path(value: object, project_root: Path) -> Path:
    raw = Path(str(value or ""))
    path = (raw if raw.is_absolute() else project_root / raw).resolve()
    path.relative_to(project_root.resolve())
    return path


def _authority() -> dict[str, bool]:
    return {
        "research_only": True,
        **{name: False for name in _AUTHORITY_FALSE},
    }


def verify_leader_quote_protocol(
    *, project_root: Path = PROJECT_ROOT, protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str | None = PROTOCOL_SHA256,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        protocol_sha = _sha256(protocol_path)
        if expected_protocol_sha256 and protocol_sha != expected_protocol_sha256:
            errors.append("protocol_sha256_mismatch")
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        if protocol.get("status") != "frozen_before_first_forward_quote_journal_no_outcome_access":
            errors.append("protocol_status_invalid")
        for name, binding in (protocol.get("bindings") or {}).items():
            path = _project_path(binding.get("path"), project_root)
            if not path.is_file() or _sha256(path) != binding.get("sha256"):
                errors.append(f"binding_invalid:{name}")
        boundary = protocol.get("research_boundary") or {}
        if any(boundary.get(name) is not False for name in (
            "divergence_threshold_defined", "re_strengthening_threshold_defined",
            "entry_or_exit_rule_defined", "returns_or_labels_read",
            "backtest_authorized", "outcomes_used_to_select_inputs",
        )):
            errors.append("research_boundary_invalid")
        authority = protocol.get("authority") or {}
        if authority.get("research_only") is not True or any(
            authority.get(name) is not False for name in _AUTHORITY_FALSE
        ):
            errors.append("authority_invalid")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        protocol, protocol_sha = {}, None
        errors.append(f"protocol_unreadable:{type(exc).__name__}")
    return {
        "valid": not errors, "errors": list(dict.fromkeys(errors)),
        "protocol": protocol, "protocol_sha256": protocol_sha,
    }


def _entry_sha(entry: dict[str, Any]) -> str:
    body = dict(entry)
    body.pop("entry_sha256", None)
    return hashlib.sha256(_canonical_bytes(body)).hexdigest()


def _entry_paths(directory: Path) -> list[Path]:
    return sorted(path for path in directory.glob("*.json") if re.fullmatch(r"\d{6}-[a-z_]+\.json", path.name))


def _append_entry(
    directory: Path, *, entry_type: str, common: dict[str, Any],
    payload: dict[str, Any], observed: datetime,
) -> tuple[dict[str, Any], Path, bool]:
    paths = _entry_paths(directory)
    sequence = len(paths)
    previous_sha = _sha256(paths[-1]) if paths else None
    entry = {
        "schema": "leader_quote_forward_journal_entry.v1",
        **common,
        "sequence": sequence,
        "entry_type": entry_type,
        "first_seen_at": observed.isoformat(timespec="seconds"),
        "previous_entry_file_sha256": previous_sha,
        "payload": payload,
        "no_backfill": True,
        "authority": _authority(),
    }
    entry["entry_sha256"] = _entry_sha(entry)
    path = directory / f"{sequence:06d}-{entry_type}.json"
    created = _atomic_publish(path, _canonical_bytes(entry))
    return entry, path, created


def _slot_end(day: str, slot_id: str, protocol: dict[str, Any]) -> datetime | None:
    key = f"{slot_id}_observe_until"
    raw = (protocol.get("observation") or {}).get(key)
    try:
        return datetime.combine(
            datetime.fromisoformat(day).date(), time.fromisoformat(str(raw)), tzinfo=SH_TZ,
        )
    except (TypeError, ValueError):
        return None


def _quote_payload(quote: object, *, observed: datetime, settings: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    source_at = _time(getattr(quote, "exchange_data_time", None))
    if source_at is None:
        return None, ["exchange_data_time_missing"]
    age = (observed - source_at).total_seconds()
    if source_at.date() != observed.date():
        errors.append("quote_not_same_decision_day")
    if age < 0:
        errors.append("quote_from_future")
    elif age > float(settings["maximum_exchange_age_seconds"]):
        errors.append("quote_stale")
    if str(getattr(quote, "freshness", "")) != settings["source_freshness_required"]:
        errors.append("quote_not_live")
    values = {
        name: _finite(getattr(quote, name, None))
        for name in settings["required_quote_fields"]
    }
    if any(value is None for value in values.values()):
        errors.append("quote_required_field_missing")
    elif (
        values["price"] <= 0 or values["open_price"] <= 0
        or values["previous_close"] <= 0 or values["low"] <= 0
        or values["high"] < max(values["price"], values["open_price"])
        or values["low"] > min(values["price"], values["open_price"])
        or values["volume"] < 0 or values["amount"] < 0
    ):
        errors.append("quote_values_invalid")
    if errors:
        return None, list(dict.fromkeys(errors))
    return {
        "exchange_data_time": source_at.isoformat(timespec="seconds"),
        "source": str(getattr(quote, "source", "") or "unknown"),
        "freshness": str(getattr(quote, "freshness", "")),
        **values,
        "change_pct": _finite(getattr(quote, "change_pct", None)),
    }, []


def _journal_common(
    *, protocol_path: Path, protocol_sha: str, decision_day: str,
    slot_id: str, stock_code: str, project_root: Path,
) -> dict[str, Any]:
    return {
        "protocol": {
            "path": str(protocol_path.resolve().relative_to(project_root.resolve())),
            "sha256": protocol_sha,
        },
        "decision_day": decision_day,
        "slot_id": slot_id,
        "stock_code": stock_code,
    }


def _last_quote(paths: list[Path]) -> dict[str, Any] | None:
    for path in reversed(paths):
        entry = json.loads(path.read_text(encoding="utf-8"))
        if entry.get("entry_type") == "quote_observed":
            return entry
    return None


def _append_quote_if_new(
    directory: Path, *, quote: object, observed: datetime,
    common: dict[str, Any], settings: dict[str, Any], identity_at: datetime,
) -> dict[str, Any]:
    payload, errors = _quote_payload(quote, observed=observed, settings=settings)
    if errors:
        return {"status": "quote_ineligible", "captured": False, "errors": errors}
    paths = _entry_paths(directory)
    last = _last_quote(paths)
    source_at = _time(payload["exchange_data_time"])
    last_source_at = _time((last or {}).get("payload", {}).get("exchange_data_time"))
    if last_source_at is not None:
        step = (source_at - last_source_at).total_seconds()
        if step <= 0:
            if payload == (last or {}).get("payload"):
                return {"status": "quote_unchanged", "captured": False}
            return {"status": "source_revision_rejected", "captured": False}
        if step < float(settings["minimum_exchange_time_step_seconds"]):
            return {"status": "minimum_step_not_reached", "captured": False}
        gap_seconds = step
    else:
        gap_seconds = max(0.0, (source_at - identity_at).total_seconds())
    payload["gap_from_previous_seconds"] = round(gap_seconds, 3)
    payload["path_gap_detected"] = gap_seconds > float(settings["maximum_gap_seconds_for_complete_path"])
    entry, path, _ = _append_entry(
        directory, entry_type="quote_observed", common=common,
        payload=payload, observed=observed,
    )
    return {
        "status": "captured", "captured": True, "path": str(path),
        "entry_sha256": entry["entry_sha256"],
        "path_gap_detected": payload["path_gap_detected"],
    }


def start_leader_quote_journal(
    candidate_capture: dict[str, Any], quote_provider: object, *,
    now: datetime | None = None, project_root: Path = PROJECT_ROOT,
    protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str | None = PROTOCOL_SHA256,
    journal_dir: Path = JOURNAL_DIR,
    candidate_verifier: Callable[..., dict[str, Any]] = verify_candidate_packet,
) -> dict[str, Any]:
    observed = (now or datetime.now(SH_TZ)).astimezone(SH_TZ)
    checked = verify_leader_quote_protocol(
        project_root=project_root, protocol_path=protocol_path,
        expected_protocol_sha256=expected_protocol_sha256,
    )
    if checked.get("valid") is not True:
        return {"status": "identity_invalid", "captured": False, "errors": checked["errors"]}
    protocol = checked["protocol"]
    if observed.date().isoformat() < str(protocol.get("forward_start") or ""):
        return {"status": "waiting_forward_start", "captured": False}
    try:
        candidate_path = _project_path(candidate_capture.get("path"), project_root)
        candidate_check = candidate_verifier(candidate_path, project_root=project_root)
    except (OSError, TypeError, ValueError):
        return {"status": "candidate_input_invalid", "captured": False}
    if candidate_check.get("valid") is not True:
        return {
            "status": "candidate_input_invalid", "captured": False,
            "errors": candidate_check.get("errors") or [],
        }
    candidate = candidate_check.get("packet") or {}
    decision_day, slot_id = str(candidate.get("decision_day") or ""), str(candidate.get("slot_id") or "")
    if observed.date().isoformat() != decision_day or capture_slot(observed) != slot_id:
        return {"status": "outside_original_capture_slot", "captured": False}
    result = candidate.get("research_result") or {}
    selected = result.get("selected") if isinstance(result.get("selected"), list) else []
    if result.get("status") != (protocol.get("identity") or {}).get("required_candidate_status") or not selected:
        return {"status": "no_eligible_candidate", "captured": False}
    row = selected[0] if isinstance(selected[0], dict) else {}
    stock_code = str(row.get("stock_code") or "")
    row_at = _time(row.get("data_as_of"))
    if (
        not re.fullmatch(r"\d{6}", stock_code) or row_at is None
        or row_at.date() != observed.date()
        or not 0 <= (observed - row_at).total_seconds() <= float(
            (protocol.get("identity") or {})["candidate_data_maximum_age_seconds"]
        )
    ):
        return {"status": "candidate_identity_stale_or_invalid", "captured": False}
    end = _slot_end(decision_day, slot_id, protocol)
    if end is None:
        return {"status": "protocol_slot_end_invalid", "captured": False}
    directory = journal_dir / decision_day / slot_id / stock_code
    common = _journal_common(
        protocol_path=protocol_path, protocol_sha=checked["protocol_sha256"],
        decision_day=decision_day, slot_id=slot_id, stock_code=stock_code,
        project_root=project_root,
    )
    with _LOCK:
        paths = _entry_paths(directory)
        if not paths:
            identity_payload = {
                "candidate_packet": {
                    "path": str(candidate_path.relative_to(project_root)),
                    "sha256": _sha256(candidate_path),
                    "packet_id": candidate.get("packet_id"),
                },
                "selected_index": 0,
                "selected_row_sha256": hashlib.sha256(_canonical_bytes(row)).hexdigest(),
                "stock_name": str(row.get("stock_name") or ""),
                "sectors": row.get("sectors") or [],
                "formula_id": "mainline_start_confirmation_stock_formula_v3",
                "leader_label": (protocol.get("identity") or {}).get("leader_label"),
                "observe_until": end.isoformat(timespec="seconds"),
            }
            _append_entry(
                directory, entry_type="identity_frozen", common=common,
                payload=identity_payload, observed=observed,
            )
        elif any(json.loads(path.read_text(encoding="utf-8")).get("entry_type") == "journal_closed" for path in paths):
            return {"status": "already_closed", "captured": False, "path": str(directory)}
        try:
            quotes = quote_provider.get_quotes([stock_code])
            quote = quotes.get(stock_code)
        except Exception as exc:
            return {
                "status": "identity_captured_quote_unavailable", "captured": True,
                "path": str(directory), "error_type": type(exc).__name__,
            }
        if quote is None:
            return {"status": "identity_captured_quote_missing", "captured": True, "path": str(directory)}
        result = _append_quote_if_new(
            directory, quote=quote, observed=observed, common=common,
            settings=protocol["observation"], identity_at=observed,
        )
        return {**result, "journal_started": True, "journal_path": str(directory)}


def refresh_active_leader_quote_journals(
    quote_provider: object, *, now: datetime | None = None,
    project_root: Path = PROJECT_ROOT, protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str | None = PROTOCOL_SHA256,
    journal_dir: Path = JOURNAL_DIR,
) -> dict[str, Any]:
    observed = (now or datetime.now(SH_TZ)).astimezone(SH_TZ)
    checked = verify_leader_quote_protocol(
        project_root=project_root, protocol_path=protocol_path,
        expected_protocol_sha256=expected_protocol_sha256,
    )
    if checked.get("valid") is not True:
        return {"status": "identity_invalid", "active": 0, "errors": checked["errors"]}
    day_dir = journal_dir / observed.date().isoformat()
    directories = sorted(path.parent for path in day_dir.glob("*/*/000000-identity_frozen.json"))
    active: list[tuple[Path, dict[str, Any], datetime]] = []
    for directory in directories:
        paths = _entry_paths(directory)
        if not paths:
            continue
        entries = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
        if any(entry.get("entry_type") == "journal_closed" for entry in entries):
            continue
        identity = entries[0]
        end = _time((identity.get("payload") or {}).get("observe_until"))
        if end is not None:
            active.append((directory, identity, end))
    if not active:
        return {"status": "no_active_journal", "active": 0, "captured": 0, "closed": 0}
    codes = sorted({str(identity.get("stock_code") or "") for _, identity, end in active if observed <= end})
    try:
        quotes = quote_provider.get_quotes(codes) if codes else {}
    except Exception as exc:
        quotes = {}
        quote_error = type(exc).__name__
    else:
        quote_error = None
    captured = closed = 0
    rows: list[dict[str, Any]] = []
    with _LOCK:
        for directory, identity, end in active:
            common = {
                key: identity[key]
                for key in ("protocol", "decision_day", "slot_id", "stock_code")
            }
            paths = _entry_paths(directory)
            identity_at = _time(identity.get("first_seen_at")) or observed
            quote_result: dict[str, Any] | None = None
            quote = quotes.get(str(identity.get("stock_code") or ""))
            if quote is not None and observed <= end:
                quote_result = _append_quote_if_new(
                    directory, quote=quote, observed=observed, common=common,
                    settings=checked["protocol"]["observation"], identity_at=identity_at,
                )
                captured += int(quote_result.get("captured") is True)
            if observed > end:
                quote_entries = [
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in _entry_paths(directory)
                    if "-quote_observed.json" in path.name
                ]
                gap = not quote_entries or any(
                    bool((entry.get("payload") or {}).get("path_gap_detected"))
                    for entry in quote_entries
                )
                last_source = _time(
                    ((quote_entries[-1].get("payload") or {}).get("exchange_data_time"))
                ) if quote_entries else None
                if last_source is None or (end - last_source).total_seconds() > float(
                    checked["protocol"]["observation"]["maximum_gap_seconds_for_complete_path"]
                ):
                    gap = True
                _append_entry(
                    directory, entry_type="journal_closed", common=common,
                    payload={
                        "observe_until": end.isoformat(timespec="seconds"),
                        "quote_observation_count": len(quote_entries),
                        "complete_path": bool(len(quote_entries) >= 2 and not gap),
                        "incomplete_reason": "source_gap_or_insufficient_observations" if gap else None,
                        "returns_or_labels_read": False,
                    },
                    observed=observed,
                )
                closed += 1
            rows.append({
                "journal_path": str(directory),
                "quote": quote_result,
                "closed": observed > end,
            })
    return {
        "status": "updated", "active": len(active), "captured": captured,
        "closed": closed, "quote_error_type": quote_error, "journals": rows,
    }


def verify_leader_quote_journal(
    directory: Path, *, project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        resolved = directory.resolve()
        resolved.relative_to(project_root.resolve())
        paths = _entry_paths(resolved)
        entries = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "complete": False, "errors": [f"journal_unreadable:{type(exc).__name__}"]}
    if not entries or entries[0].get("entry_type") != "identity_frozen":
        errors.append("identity_entry_missing")
    previous_file_sha = None
    quote_times: list[datetime] = []
    common_identity: tuple[object, ...] | None = None
    for index, (path, entry) in enumerate(zip(paths, entries)):
        if entry.get("sequence") != index:
            errors.append("sequence_invalid")
        if entry.get("entry_sha256") != _entry_sha(entry):
            errors.append("entry_sha256_invalid")
        if entry.get("previous_entry_file_sha256") != previous_file_sha:
            errors.append("hash_chain_invalid")
        previous_file_sha = _sha256(path)
        identity = (
            entry.get("decision_day"), entry.get("slot_id"), entry.get("stock_code"),
            json.dumps(entry.get("protocol"), sort_keys=True),
        )
        common_identity = common_identity or identity
        if identity != common_identity or entry.get("no_backfill") is not True:
            errors.append("journal_identity_invalid")
        authority = entry.get("authority") or {}
        if authority.get("research_only") is not True or any(
            authority.get(name) is not False for name in _AUTHORITY_FALSE
        ):
            errors.append("authority_invalid")
        if entry.get("entry_type") == "quote_observed":
            payload = entry.get("payload") or {}
            source_at, seen_at = _time(payload.get("exchange_data_time")), _time(entry.get("first_seen_at"))
            if (
                source_at is None or seen_at is None or source_at > seen_at
                or source_at.date().isoformat() != entry.get("decision_day")
            ):
                errors.append("quote_time_invalid")
            elif quote_times and source_at <= quote_times[-1]:
                errors.append("quote_time_not_strictly_increasing")
            else:
                quote_times.append(source_at)
    close_entries = [entry for entry in entries if entry.get("entry_type") == "journal_closed"]
    if len(close_entries) > 1 or (close_entries and entries[-1] is not close_entries[-1]):
        errors.append("close_entry_invalid")
    complete = bool(
        not errors and close_entries
        and (close_entries[-1].get("payload") or {}).get("complete_path") is True
    )
    return {
        "valid": not errors, "complete": complete,
        "errors": list(dict.fromkeys(errors)), "entry_count": len(entries),
        "quote_observation_count": len(quote_times),
        "closed": bool(close_entries),
    }


__all__ = [
    "refresh_active_leader_quote_journals", "start_leader_quote_journal",
    "verify_leader_quote_journal", "verify_leader_quote_protocol",
]
