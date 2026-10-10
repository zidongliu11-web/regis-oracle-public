"""Append-only full constituent quote panels for confirmed research sectors."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from datetime import datetime, time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .market_sector_constituent_forward import verify_constituent_packet
from .market_sector_route_forward import capture_slot
from .market_state_daily import PROJECT_ROOT, _atomic_publish, _canonical_bytes, _sha256


SH_TZ = ZoneInfo("Asia/Shanghai")
PROTOCOL_PATH = (
    PROJECT_ROOT
    / "research/SECTOR_CONSTITUENT_QUOTE_PANEL_FORWARD_PROTOCOL_V6.json"
)
PROTOCOL_SHA256 = "33d32f671c7f7e7e88d2bc74e22d8c92ca0aba1ca87d245759ca2ac1cd4f5837"
JOURNAL_DIR = (
    PROJECT_ROOT / "research/data/sector_constituent_quote_panel/v1/journals"
)
_LOCK = threading.Lock()
_AUTHORITY_FALSE = (
    "changes_unified_score", "changes_trade_count",
    "changes_position_or_quantity", "generates_buy_or_sell_alert",
    "selects_production_candidate", "production_strategy",
    "automatic_trading", "stable_profit_claim",
)
_DUPLICATE_IDENTITY_FIELDS = ("stock_name",)
_CONSTITUENT_REQUIRED_FIELDS = (
    "stock_code", "stock_name", "price", "change_pct", "volume", "amount",
    "turnover_rate", "total_market_cap", "float_market_cap", "data_as_of",
)


def _authority() -> dict[str, bool]:
    return {"research_only": True, **{name: False for name in _AUTHORITY_FALSE}}


def _runtime_result(status: str, phase: str, **values: Any) -> dict[str, Any]:
    return {
        "status": status,
        "phase": phase,
        **values,
        "research_only": True,
        "trade_action_authority": False,
    }


def _project_path(raw: object, *, project_root: Path) -> Path:
    path = Path(str(raw or ""))
    resolved = (path if path.is_absolute() else project_root / path).resolve()
    resolved.relative_to(project_root.resolve())
    return resolved


def _time(raw: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(SH_TZ) if parsed.tzinfo is not None else None


def _finite(raw: object) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def verify_sector_quote_panel_protocol(
    *, project_root: Path = PROJECT_ROOT, protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str | None = PROTOCOL_SHA256,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        protocol_sha = _sha256(protocol_path)
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        if expected_protocol_sha256 and protocol_sha != expected_protocol_sha256:
            errors.append("protocol_sha256_mismatch")
        if protocol.get("status") != (
            "frozen_before_first_forward_panel_snapshot_no_outcome_access"
        ):
            errors.append("protocol_status_invalid")
        for name, binding in (protocol.get("bindings") or {}).items():
            path = _project_path(binding.get("path"), project_root=project_root)
            if not path.is_file() or _sha256(path) != binding.get("sha256"):
                errors.append(f"binding_invalid:{name}")
        boundary = protocol.get("research_boundary") or {}
        if any(boundary.get(name) is not False for name in (
            "leader_identity_defined", "stock_rank_or_score_defined",
            "divergence_or_restrengthening_defined", "entry_or_exit_rule_defined",
            "returns_or_labels_read", "backtest_authorized",
            "production_admission",
        )):
            errors.append("research_boundary_invalid")
        if protocol.get("authority") != _authority():
            errors.append("authority_invalid")
        identity = protocol.get("identity") or {}
        if identity.get("duplicate_stock_identity_fields_must_match") != list(
            _DUPLICATE_IDENTITY_FIELDS
        ):
            errors.append("duplicate_identity_contract_invalid")
        if identity.get("duplicate_stock_market_fields_must_match") is not False:
            errors.append("duplicate_market_field_contract_invalid")
        if identity.get(
            "duplicate_stock_market_fields_may_differ_between_sequential_sector_snapshots"
        ) is not True:
            errors.append("sequential_market_field_contract_invalid")
        if identity.get(
            "duplicate_stock_constituent_data_as_of_uses_latest_same_day_value"
        ) is not True:
            errors.append("duplicate_timestamp_contract_invalid")
        effective_at = _time(protocol.get("effective_not_before"))
        if effective_at is None:
            errors.append("effective_not_before_invalid")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        protocol, protocol_sha = {}, None
        errors.append(f"protocol_unreadable:{type(exc).__name__}")
    return {
        "valid": not errors, "errors": list(dict.fromkeys(errors)),
        "protocol": protocol, "protocol_sha256": protocol_sha,
    }


def _entry_paths(directory: Path) -> list[Path]:
    return sorted(
        path for path in directory.glob("*.json")
        if re.fullmatch(r"\d{6}-[a-z_]+\.json", path.name)
    )


def _entry_sha(entry: dict[str, Any]) -> str:
    body = dict(entry)
    body.pop("entry_sha256", None)
    return hashlib.sha256(_canonical_bytes(body)).hexdigest()


def _append_entry(
    directory: Path, *, entry_type: str, common: dict[str, Any],
    payload: dict[str, Any], observed: datetime,
) -> tuple[dict[str, Any], Path, bool]:
    paths = _entry_paths(directory)
    entry = {
        "schema": "sector_constituent_quote_panel_journal_entry.v1",
        **common,
        "sequence": len(paths), "entry_type": entry_type,
        "first_seen_at": observed.isoformat(timespec="seconds"),
        "previous_entry_file_sha256": _sha256(paths[-1]) if paths else None,
        "payload": payload, "no_backfill": True,
        "authority": _authority(),
    }
    entry["entry_sha256"] = _entry_sha(entry)
    path = directory / f"{entry['sequence']:06d}-{entry_type}.json"
    created = _atomic_publish(path, _canonical_bytes(entry))
    return entry, path, created


def _slot_end(day: str, slot_id: str, protocol: dict[str, Any]) -> datetime | None:
    raw = (protocol.get("observation") or {}).get(f"{slot_id}_observe_until")
    try:
        return datetime.combine(
            datetime.fromisoformat(day).date(), time.fromisoformat(str(raw)),
            tzinfo=SH_TZ,
        )
    except (TypeError, ValueError):
        return None


def _identity_rows(
    packet: dict[str, Any], protocol: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    errors: list[str] = []
    unsupported: list[str] = []
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for capture in packet.get("sector_captures") or []:
        if not isinstance(capture, dict) or capture.get("complete") is not True:
            errors.append("constituent_sector_capture_incomplete")
            continue
        sector = capture.get("sector") or {}
        for raw in capture.get("rows") or []:
            if not isinstance(raw, dict):
                errors.append("constituent_row_invalid")
                continue
            grouped.setdefault(str(raw.get("stock_code") or ""), []).append(
                (raw, sector)
            )
    prefixes = tuple((protocol.get("identity") or {}).get(
        "source_supported_code_prefixes"
    ) or [])
    rows: list[dict[str, Any]] = []
    decision_day = str(packet.get("decision_day") or "")
    for code, values in sorted(grouped.items()):
        if not re.fullmatch(r"\d{6}", code) or not code.startswith(prefixes):
            unsupported.append(code)
            continue
        first = values[0][0]
        if any(
            any(
                other.get(field) != first.get(field)
                for field in _DUPLICATE_IDENTITY_FIELDS
            )
            for other, _sector in values[1:]
        ):
            errors.append(f"duplicate_identity_fields_inconsistent:{code}")
            continue
        observations = [
            (raw, _time(raw.get("data_as_of"))) for raw, _sector in values
        ]
        if any(
            as_of is None or as_of.date().isoformat() != decision_day
            for _raw, as_of in observations
        ):
            errors.append(f"constituent_data_as_of_invalid:{code}")
            continue
        latest, as_of = max(observations, key=lambda item: item[1])
        rows.append({
            "stock_code": code,
            "stock_name": str(latest.get("stock_name") or ""),
            "constituent_data_as_of": as_of.isoformat(timespec="seconds"),
            "sector_codes": sorted({
                str(sector.get("code") or "") for _row, sector in values
                if sector.get("code")
            }),
        })
    if not rows:
        errors.append("no_source_supported_constituents")
    return rows, sorted(set(unsupported)), list(dict.fromkeys(errors))


def _quote_row(
    code: str, quote: object, *, observed: datetime, settings: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    exchange_at = _time(getattr(quote, "exchange_data_time", None))
    if exchange_at is None:
        return None, ["exchange_data_time_missing"]
    age = (observed - exchange_at).total_seconds()
    if exchange_at.date() != observed.date():
        errors.append("quote_not_same_decision_day")
    if age < 0:
        errors.append("quote_from_future")
    elif age > float(settings["maximum_exchange_age_seconds"]):
        errors.append("quote_stale")
    if str(getattr(quote, "freshness", "")) != settings["required_freshness"]:
        errors.append("quote_not_live")
    values = {
        field: _finite(getattr(quote, field, None))
        for field in settings["required_quote_fields"]
    }
    if any(value is None for value in values.values()):
        errors.append("quote_required_field_missing")
    elif (
        values["price"] <= 0 or values["open_price"] <= 0
        or values["previous_close"] <= 0 or values["low"] <= 0
        or values["high"] < values["low"]
        or values["volume"] < 0 or values["amount"] < 0
    ):
        errors.append("quote_values_invalid")
    if errors:
        return None, list(dict.fromkeys(errors))
    return {
        "stock_code": code,
        "stock_name": str(getattr(quote, "name", "") or ""),
        "exchange_data_time": exchange_at.isoformat(timespec="seconds"),
        "source": str(getattr(quote, "source", "") or "unknown"),
        "freshness": str(getattr(quote, "freshness", "")),
        **values,
        "change_pct": _finite(getattr(quote, "change_pct", None)),
    }, []


def _fetch_quotes(
    quote_provider: object, codes: list[str], *, batch_size: int,
) -> tuple[dict[str, object], list[str]]:
    quotes: dict[str, object] = {}
    errors: list[str] = []
    for start in range(0, len(codes), batch_size):
        chunk = codes[start:start + batch_size]
        try:
            result = quote_provider.get_quotes(chunk)
        except Exception as exc:
            errors.append(f"batch_{start // batch_size}:{type(exc).__name__}")
            continue
        if not isinstance(result, dict):
            errors.append(f"batch_{start // batch_size}:invalid_result")
            continue
        quotes.update({str(code): quote for code, quote in result.items()})
    return quotes, errors


def _last_snapshot(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next(
        (entry for entry in reversed(entries) if entry.get("entry_type") == "panel_snapshot"),
        None,
    )


def _constituent_capture_incomplete_reasons(
    packet: dict[str, Any],
) -> list[dict[str, Any]]:
    """Preserve source failure detail for operators without relaxing capture."""
    reasons: list[dict[str, Any]] = []
    for capture in packet.get("sector_captures") or []:
        if not isinstance(capture, dict) or (
            capture.get("complete") is True and not capture.get("errors")
        ):
            continue
        sector = capture.get("sector") or {}
        affected_codes: list[str] = []
        missing_fields: set[str] = set()
        for raw in capture.get("rows") or []:
            if not isinstance(raw, dict):
                continue
            missing = [
                field for field in _CONSTITUENT_REQUIRED_FIELDS
                if raw.get(field) is None or raw.get(field) == ""
            ]
            if missing:
                code = str(raw.get("stock_code") or "")
                if code:
                    affected_codes.append(code)
                missing_fields.update(missing)
        reasons.append({
            "sector_code": str(sector.get("code") or ""),
            "sector_name": str(sector.get("name") or ""),
            "error_codes": sorted({
                str(error) for error in (capture.get("errors") or []) if error
            }),
            "affected_codes": sorted(set(affected_codes)),
            "missing_fields": sorted(missing_fields),
        })
    return reasons


def _capture_panel_snapshot(
    directory: Path, quote_provider: object, *, observed: datetime,
    common: dict[str, Any], identity: dict[str, Any], protocol: dict[str, Any],
) -> dict[str, Any]:
    paths = _entry_paths(directory)
    entries = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    last = _last_snapshot(entries)
    last_seen = _time((last or {}).get("first_seen_at"))
    settings = protocol["observation"]
    if last_seen is not None and (
        observed - last_seen
    ).total_seconds() < float(settings["minimum_snapshot_step_seconds"]):
        return _runtime_result(
            "minimum_step_not_reached", "collecting", captured=False,
            entry_persisted=False, snapshot_complete=None, path_complete=False,
        )
    codes = list((identity.get("payload") or {}).get("ordered_codes") or [])
    quotes, provider_errors = _fetch_quotes(
        quote_provider, codes, batch_size=int(settings["provider_batch_size"]),
    )
    normalized: list[dict[str, Any]] = []
    invalid: dict[str, list[str]] = {}
    for code in codes:
        quote = quotes.get(code)
        if quote is None:
            invalid[code] = ["quote_missing"]
            continue
        row, errors = _quote_row(code, quote, observed=observed, settings=settings)
        if errors:
            invalid[code] = errors
        elif row is not None:
            normalized.append(row)
    normalized.sort(key=lambda row: row["stock_code"])
    gap_seconds = (
        (observed - last_seen).total_seconds() if last_seen is not None else 0.0
    )
    gap = bool(
        last_seen is not None
        and gap_seconds > float(settings["maximum_snapshot_gap_seconds_for_complete_panel"])
    )
    coverage_complete = len(normalized) == len(codes) and not provider_errors
    payload = {
        "expected_code_count": len(codes),
        "observed_code_count": len(normalized),
        "coverage_pct": round(len(normalized) / len(codes) * 100, 6) if codes else 0.0,
        "coverage_complete": coverage_complete,
        "ordered_codes_sha256": (identity.get("payload") or {}).get(
            "ordered_codes_sha256"
        ),
        "rows": normalized,
        "rows_sha256": hashlib.sha256(_canonical_bytes(normalized)).hexdigest(),
        "invalid_or_missing": invalid,
        "provider_error_types": provider_errors,
        "gap_from_previous_snapshot_seconds": round(gap_seconds, 3),
        "path_gap_detected": gap,
        "stock_ranked_or_selected": False,
        "returns_or_labels_read": False,
    }
    entry, path, _created = _append_entry(
        directory, entry_type="panel_snapshot", common=common,
        payload=payload, observed=observed,
    )
    return _runtime_result(
        "captured" if coverage_complete else "captured_incomplete",
        "collecting",
        captured=True, entry_persisted=True,
        snapshot_complete=coverage_complete, path_complete=False,
        expected_code_count=len(codes), observed_code_count=len(normalized),
        path=str(path),
        entry_sha256=entry["entry_sha256"],
        coverage_pct=payload["coverage_pct"],
        path_gap_detected=gap,
    )


def start_sector_quote_panel(
    constituent_capture: dict[str, Any], quote_provider: object, *,
    now: datetime | None = None, project_root: Path = PROJECT_ROOT,
    protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str | None = PROTOCOL_SHA256,
    journal_dir: Path = JOURNAL_DIR,
    constituent_verifier: Callable[..., dict[str, Any]] = verify_constituent_packet,
) -> dict[str, Any]:
    observed = (now or datetime.now(SH_TZ)).astimezone(SH_TZ)
    checked = verify_sector_quote_panel_protocol(
        project_root=project_root, protocol_path=protocol_path,
        expected_protocol_sha256=expected_protocol_sha256,
    )
    if checked.get("valid") is not True:
        return _runtime_result(
            "identity_invalid", "error", captured=False,
            terminal_error="identity_invalid", errors=checked["errors"],
        )
    if observed.date().isoformat() < str(checked["protocol"].get("forward_start") or ""):
        return _runtime_result(
            "waiting_forward_start", "awaiting_window", captured=False,
        )
    effective_at = _time(checked["protocol"].get("effective_not_before"))
    if effective_at is None or observed < effective_at:
        return _runtime_result(
            "waiting_protocol_effective_at", "awaiting_window", captured=False,
            effective_not_before=(effective_at.isoformat(timespec="seconds")
                                  if effective_at is not None else None),
        )
    try:
        packet_path = _project_path(
            constituent_capture.get("path"), project_root=project_root,
        )
        constituent_check = constituent_verifier(
            packet_path, project_root=project_root,
        )
    except (OSError, ValueError):
        return _runtime_result(
            "constituent_input_invalid", "error", captured=False,
            terminal_error="constituent_input_invalid",
        )
    if constituent_check.get("valid") is not True:
        return _runtime_result(
            "constituent_input_invalid", "error", captured=False,
            terminal_error="constituent_input_invalid",
            errors=constituent_check.get("errors") or [],
        )
    packet = constituent_check.get("packet") or {}
    if packet.get("capture_status") != "complete":
        return _runtime_result(
            "constituent_capture_incomplete", "error", captured=False,
            terminal_error="constituent_capture_incomplete",
            incomplete_reasons=_constituent_capture_incomplete_reasons(packet),
        )
    if (
        observed.date().isoformat() != packet.get("decision_day")
        or capture_slot(observed) != packet.get("slot_id")
    ):
        return _runtime_result(
            "outside_original_capture_slot", "error", captured=False,
            terminal_error="outside_original_capture_slot",
        )
    rows, unsupported, errors = _identity_rows(packet, checked["protocol"])
    if errors:
        return _runtime_result(
            "panel_identity_invalid", "error", captured=False,
            terminal_error="panel_identity_invalid", errors=errors,
        )
    day, slot_id = str(packet["decision_day"]), str(packet["slot_id"])
    end = _slot_end(day, slot_id, checked["protocol"])
    if end is None:
        return _runtime_result(
            "protocol_slot_end_invalid", "error", captured=False,
            terminal_error="protocol_slot_end_invalid",
        )
    directory = journal_dir / day / slot_id
    common = {
        "protocol": {
            "path": str(protocol_path.resolve().relative_to(project_root.resolve())),
            "sha256": checked["protocol_sha256"],
        },
        "decision_day": day, "slot_id": slot_id,
    }
    with _LOCK:
        paths = _entry_paths(directory)
        if paths:
            entries = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
            if any(entry.get("entry_type") == "journal_closed" for entry in entries):
                close = next(
                    entry for entry in reversed(entries)
                    if entry.get("entry_type") == "journal_closed"
                )
                complete = (close.get("payload") or {}).get("complete_panel_path") is True
                return _runtime_result(
                    "already_closed",
                    "closed_complete" if complete else "closed_incomplete",
                    captured=False, path_complete=complete,
                    incomplete_reason=(close.get("payload") or {}).get("incomplete_reason"),
                    path=str(directory),
                )
            return _runtime_result(
                "already_started", "collecting", captured=False,
                path_complete=False, path=str(directory),
            )
        codes = [row["stock_code"] for row in rows]
        identity_payload = {
            "constituent_packet": {
                "path": str(packet_path.relative_to(project_root.resolve())),
                "sha256": _sha256(packet_path), "packet_id": packet.get("packet_id"),
            },
            "ordered_codes": codes,
            "ordered_codes_sha256": hashlib.sha256(_canonical_bytes(codes)).hexdigest(),
            "code_count": len(codes), "constituent_identity_rows": rows,
            "unsupported_source_codes": unsupported,
            "stock_ranked_or_selected": False,
            "old_mainline_formula_used": False,
            "observe_until": end.isoformat(timespec="seconds"),
            "collector_pid": os.getpid(),
        }
        identity, identity_path, _created = _append_entry(
            directory, entry_type="identity_frozen", common=common,
            payload=identity_payload, observed=observed,
        )
        result = _capture_panel_snapshot(
            directory, quote_provider, observed=observed, common=common,
            identity=identity, protocol=checked["protocol"],
        )
        return {
            **result, "journal_started": True,
            "identity_path": str(identity_path), "journal_path": str(directory),
            "code_count": len(codes), "unsupported_source_count": len(unsupported),
        }


def refresh_active_sector_quote_panels(
    quote_provider: object, *, now: datetime | None = None,
    project_root: Path = PROJECT_ROOT, protocol_path: Path = PROTOCOL_PATH,
    expected_protocol_sha256: str | None = PROTOCOL_SHA256,
    journal_dir: Path = JOURNAL_DIR,
) -> dict[str, Any]:
    observed = (now or datetime.now(SH_TZ)).astimezone(SH_TZ)
    checked = verify_sector_quote_panel_protocol(
        project_root=project_root, protocol_path=protocol_path,
        expected_protocol_sha256=expected_protocol_sha256,
    )
    if checked.get("valid") is not True:
        return _runtime_result(
            "identity_invalid", "error", active=0,
            terminal_error="identity_invalid", errors=checked["errors"],
        )
    day_dir = journal_dir / observed.date().isoformat()
    directories = sorted(path.parent for path in day_dir.glob("*/000000-identity_frozen.json"))
    if not directories:
        return _runtime_result(
            "no_active_panel", "awaiting_window", active=0, captured=0,
            closed=0, decision_day=observed.date().isoformat(),
            path_complete=False,
        )
    captured = closed = 0
    results: list[dict[str, Any]] = []
    with _LOCK:
        for directory in directories:
            paths = _entry_paths(directory)
            entries = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
            if not entries:
                continue
            close_entry = next(
                (entry for entry in reversed(entries)
                 if entry.get("entry_type") == "journal_closed"),
                None,
            )
            if close_entry is not None:
                close_payload = close_entry.get("payload") or {}
                complete = close_payload.get("complete_panel_path") is True
                closed += 1
                results.append({
                    "journal_path": str(directory),
                    "phase": "closed_complete" if complete else "closed_incomplete",
                    "closed": True,
                    "path_complete": complete,
                    "incomplete_reason": close_payload.get("incomplete_reason"),
                    "panel_snapshot_count": close_payload.get("panel_snapshot_count"),
                })
                continue
            identity = entries[0]
            end = _time((identity.get("payload") or {}).get("observe_until"))
            if end is None:
                continue
            common = {
                key: identity[key] for key in ("protocol", "decision_day", "slot_id")
            }
            if (
                (identity.get("payload") or {}).get("collector_pid") != os.getpid()
                and not any(entry.get("entry_type") == "restart_detected" for entry in entries)
            ):
                _append_entry(
                    directory, entry_type="restart_detected", common=common,
                    payload={"original_pid": (identity.get("payload") or {}).get("collector_pid"), "current_pid": os.getpid()},
                    observed=observed,
                )
            panel_result = None
            if observed <= end:
                panel_result = _capture_panel_snapshot(
                    directory, quote_provider, observed=observed, common=common,
                    identity=identity, protocol=checked["protocol"],
                )
                captured += int(panel_result.get("captured") is True)
            else:
                entries = [
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in _entry_paths(directory)
                ]
                panels = [entry for entry in entries if entry.get("entry_type") == "panel_snapshot"]
                last_seen = _time((panels[-1] if panels else {}).get("first_seen_at"))
                invalid = bool(
                    not panels
                    or any(
                        (entry.get("payload") or {}).get("coverage_complete") is not True
                        or (entry.get("payload") or {}).get("path_gap_detected") is True
                        for entry in panels
                    )
                    or any(entry.get("entry_type") in {"source_error", "restart_detected"} for entry in entries)
                    or last_seen is None
                    or (end - last_seen).total_seconds() > float(
                        checked["protocol"]["observation"]["maximum_snapshot_gap_seconds_for_complete_panel"]
                    )
                )
                _append_entry(
                    directory, entry_type="journal_closed", common=common,
                    payload={
                        "observe_until": end.isoformat(timespec="seconds"),
                        "panel_snapshot_count": len(panels),
                        "complete_panel_path": bool(len(panels) >= 2 and not invalid),
                        "incomplete_reason": "coverage_gap_source_error_or_restart" if invalid else None,
                        "returns_or_labels_read": False,
                    },
                    observed=observed,
                )
                closed += 1
                panel_result = _runtime_result(
                    "journal_closed",
                    "closed_incomplete" if invalid else "closed_complete",
                    captured=False,
                    path_complete=bool(len(panels) >= 2 and not invalid),
                    incomplete_reason=(
                        "coverage_gap_source_error_or_restart" if invalid else None
                    ),
                    panel_snapshot_count=len(panels),
                )
            results.append({
                "journal_path": str(directory), "snapshot": panel_result,
                "phase": (panel_result or {}).get("phase") or "collecting",
                "closed": observed > end,
                "path_complete": (panel_result or {}).get("path_complete") is True,
                "incomplete_reason": (panel_result or {}).get("incomplete_reason"),
            })
    active = sum(row.get("closed") is not True for row in results)
    if active:
        phase = "collecting"
    elif any(row.get("phase") == "closed_incomplete" for row in results):
        phase = "closed_incomplete"
    elif results and all(row.get("phase") == "closed_complete" for row in results):
        phase = "closed_complete"
    else:
        phase = "awaiting_window"
    incomplete_reasons = sorted({
        str(row.get("incomplete_reason")) for row in results
        if row.get("incomplete_reason")
    })
    return _runtime_result(
        "updated", phase, active=active, captured=captured,
        closed=closed, panels=results,
        path_complete=bool(results) and all(
            row.get("path_complete") is True for row in results
        ),
        incomplete_reasons=incomplete_reasons,
    )


def verify_sector_quote_panel_journal(
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
    previous_sha = None
    identity = entries[0] if entries else {}
    codes = list((identity.get("payload") or {}).get("ordered_codes") or [])
    if codes != sorted(set(codes)) or hashlib.sha256(
        _canonical_bytes(codes)
    ).hexdigest() != (identity.get("payload") or {}).get("ordered_codes_sha256"):
        errors.append("ordered_codes_identity_invalid")
    common_identity = None
    for index, (path, entry) in enumerate(zip(paths, entries)):
        if entry.get("sequence") != index:
            errors.append("sequence_invalid")
        if entry.get("entry_sha256") != _entry_sha(entry):
            errors.append("entry_sha256_invalid")
        if entry.get("previous_entry_file_sha256") != previous_sha:
            errors.append("hash_chain_invalid")
        previous_sha = _sha256(path)
        current = (
            entry.get("decision_day"), entry.get("slot_id"),
            json.dumps(entry.get("protocol"), sort_keys=True),
        )
        common_identity = common_identity or current
        if current != common_identity or entry.get("no_backfill") is not True:
            errors.append("journal_identity_invalid")
        if entry.get("authority") != _authority():
            errors.append("authority_invalid")
        if entry.get("entry_type") == "panel_snapshot":
            payload = entry.get("payload") or {}
            rows = payload.get("rows") or []
            row_codes = [row.get("stock_code") for row in rows if isinstance(row, dict)]
            if row_codes != sorted(set(row_codes)) or any(code not in codes for code in row_codes):
                errors.append("panel_row_identity_invalid")
            if hashlib.sha256(_canonical_bytes(rows)).hexdigest() != payload.get("rows_sha256"):
                errors.append("panel_rows_sha256_invalid")
            if payload.get("stock_ranked_or_selected") is not False or payload.get("returns_or_labels_read") is not False:
                errors.append("panel_research_boundary_invalid")
    close_entries = [entry for entry in entries if entry.get("entry_type") == "journal_closed"]
    if len(close_entries) > 1 or (close_entries and entries[-1] is not close_entries[-1]):
        errors.append("close_entry_invalid")
    complete = bool(
        not errors and close_entries
        and (close_entries[-1].get("payload") or {}).get("complete_panel_path") is True
    )
    latest_panel = next(
        (entry for entry in reversed(entries)
         if entry.get("entry_type") == "panel_snapshot"),
        None,
    )
    close_payload = (close_entries[-1].get("payload") or {}) if close_entries else {}
    latest_payload = (latest_panel.get("payload") or {}) if latest_panel else {}
    return {
        "valid": not errors, "complete": complete,
        "errors": list(dict.fromkeys(errors)), "entry_count": len(entries),
        "panel_snapshot_count": sum(entry.get("entry_type") == "panel_snapshot" for entry in entries),
        "closed": bool(close_entries), "code_count": len(codes),
        "phase": (
            "closed_complete" if complete else
            "closed_incomplete" if close_entries else "collecting"
        ),
        "path_complete": complete,
        "incomplete_reason": close_payload.get("incomplete_reason"),
        "latest_snapshot_complete": (
            latest_payload.get("coverage_complete") is True
            if latest_panel is not None else None
        ),
        "latest_coverage_pct": latest_payload.get("coverage_pct"),
    }


__all__ = [
    "refresh_active_sector_quote_panels", "start_sector_quote_panel",
    "verify_sector_quote_panel_journal", "verify_sector_quote_panel_protocol",
]
