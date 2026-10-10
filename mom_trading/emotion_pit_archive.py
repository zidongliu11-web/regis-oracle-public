"""Capture one same-day market-emotion observation for later research.

The archive binds producer files and preserves the first published version. Its
timestamps are self-reported by the cloud host, not external time attestation.
No retrospective row from a later reconstruction is ever copied into it.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .cloud_pattern_inputs import sha
from .cloud_research_worker import read_json


SCHEMA = "market_emotion_pit_archive.v1"
CLOUD_SCHEMA = "cloud_market_emotion.v1"
SCOPE_ID = "MAIN_GEM_DAILY_RESEARCH"
SHANGHAI = ZoneInfo("Asia/Shanghai")
STATES = {"UNKNOWN", "RISK_OFF", "STABILIZING", "RISK_ON", "OVERHEATED"}
AXES = ("B_strength", "L_strength", "F_stress", "C_stress", "directional_strength")
SOURCE_MODULES = (
    "research/market_rules.py", "research/build_state_conditioned_mainline_inputs.py",
    "research/build_market_state_feature_epoch_v1.py",
    "research/build_market_state_feature_epoch_v2.py",
    "mom_trading/cloud_pattern_inputs.py", "mom_trading/cloud_pattern_features.py",
    "mom_trading/emotion_pattern_research.py", "mom_trading/cloud_daily_revisions.py",
)
BOUNDARY = {"research_only": True, "forecast_validated": False,
            "production_eligible": False, "generates_alert": False,
            "returns_computed": False, "automatic_trading": False}
CLOUD_BOUNDARY = {"production_eligible": False, "generates_alert": False,
                  "account_returns_computed": False, "return_labels_read": False,
                  "historical_input_reconstruction": True, "forward_evidence": False,
                  "state_model_validated": False, "full_a_share_coverage": False}
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_RECEIPT_BYTES = 256 * 1024
MAX_CONTROL_BYTES = 1024 * 1024


def _bounded_json(path: Path, maximum: int) -> dict:
    if path.is_symlink():
        raise ValueError("pit_control_path_unsafe")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("pit_file_too_large")
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("pit_json_invalid") from error
    if not isinstance(payload, dict):
        raise ValueError("pit_json_object_required")
    return payload


def _day(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("pit_day_invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("pit_day_invalid") from error
    if parsed.isoformat() != value:
        raise ValueError("pit_day_invalid")
    return value


def _stamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name}_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name}_invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name}_timezone_required")
    return parsed.astimezone(SHANGHAI)


def _hex(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and not set(value) - set("0123456789abcdef")


def _digest(value: Any) -> str:
    """Frozen v1 JSON identity, independent of later producer implementation."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _bound_snapshot(root: Path, day: str, reference: Mapping[str, Any]) -> dict:
    directory = root / "reports/cloud_market_emotion" / day
    name = reference.get("path")
    if (not isinstance(name, str) or not Path(name).name.startswith("snapshot.")
            or not Path(name).name.endswith(".json") or not _hex(reference.get("sha256"))):
        raise ValueError("pit_snapshot_path_invalid")
    path = root / name
    if (path.is_symlink() or path.parent.resolve() != directory.resolve()
            or not path.is_file()):
        raise ValueError("pit_snapshot_file_missing_or_unsafe")
    with path.open("rb") as stream:
        raw = stream.read(MAX_SNAPSHOT_BYTES + 1)
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise ValueError("pit_snapshot_too_large")
    if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise ValueError("pit_snapshot_sha_mismatch")
    try:
        snapshot = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("pit_snapshot_json_invalid") from error
    if not isinstance(snapshot, dict):
        raise ValueError("pit_snapshot_object_required")
    return snapshot


def _stored_axes(features: Mapping[str, Any], coverage: Mapping[str, Any]) -> dict | None:
    if coverage.get("raw_counts_diagnostic_only") is True:
        return None
    values = [features.get(name) for name in AXES]
    if any(type(value) not in (int, float) or not math.isfinite(value)
           or not 0 <= value <= 1 for value in values):
        return None
    if features.get("raw_state") not in STATES:
        return None
    return {name: round(float(features[name]) * 100, 6) for name in AXES} | {
        "raw_state": features["raw_state"]}


def _reference(root: Path, day: str, reference: Mapping[str, Any], captured: datetime,
               *, check_current_code: bool, transform_sha256: str) -> dict:
    """Verify the bound completed snapshot, not just a reported state string."""
    snapshot = _bound_snapshot(root, day, reference)
    source = snapshot.get("source_contract") or {}
    rows = snapshot.get("rows")
    refs = snapshot.get("source_references") or {}
    if (snapshot.get("schema") != CLOUD_SCHEMA or snapshot.get("data_as_of") != day
            or snapshot.get("input_identity") != reference.get("input_identity")
            or not _hex(source.get("implementation_sha256"))
            or not _hex(source.get("master_sha256"))
            or not isinstance(rows, list) or not rows or not isinstance(refs, dict) or not refs
            or any(not isinstance(item, dict) or item.get("status") != "computed"
                   for item in refs.values())
            or _digest({"histories": [refs[code]["input_identity"] for code in sorted(refs)],
                        "source_contract": source}) != snapshot.get("input_identity")
            or any(snapshot.get(key) != value for key, value in CLOUD_BOUNDARY.items())):
        raise ValueError("pit_snapshot_identity_invalid")
    modules = source.get("source_modules") or {}
    if (set(modules) != set(SOURCE_MODULES)
            or any(not _hex(modules[name]) for name in SOURCE_MODULES)
            or not isinstance(source.get("price_policy"), str)
            or not _hex(transform_sha256)):
        raise ValueError("pit_source_contract_invalid")
    if check_current_code:
        from . import cloud_market_emotion as emotion
        from . import emotion_system
        source_root = Path(__file__).resolve().parents[1]
        if (source["implementation_sha256"] != emotion.IMPLEMENTATION_SHA256
                or any(modules[name] != sha(source_root / name) for name in SOURCE_MODULES)
                or transform_sha256 != sha(Path(emotion_system.__file__))):
            raise ValueError("pit_source_version_changed")
        _axes = emotion_system._axes
    days = [row.get("trade_date") if isinstance(row, dict) else None for row in rows]
    if (days != sorted(set(days)) or days[-1] != day
            or any(_day(item) != item for item in days)
            or _digest(days) != source.get("calendar_sha256")):
        raise ValueError("pit_calendar_identity_invalid")
    observed = _stamp(snapshot.get("observed_at"), "pit_snapshot_observed_at")
    if (observed.date().isoformat() != day or observed.time() < time(15)
            or observed > captured):
        raise ValueError("pit_snapshot_not_available_on_day")
    features = rows[-1].get("features") or {}
    state = features.get("confirmed_state")
    if (state not in STATES or state != snapshot.get("latest_state")):
        raise ValueError("pit_latest_state_invalid")
    axes = _stored_axes(features, rows[-1].get("coverage") or {})
    if axes is None and state != "UNKNOWN":
        raise ValueError("pit_state_without_complete_axes")
    if check_current_code:
        calculated = _axes(rows[-1])
        if ((calculated is None) != (axes is None)
                or calculated is not None and (
                    calculated["confirmed_state"] != state
                    or any(round(calculated[name] * 100, 6) != axes[name] for name in AXES)
                    or calculated["raw_state"] != axes["raw_state"])):
            raise ValueError("pit_axes_formula_mismatch")
    strength = axes["directional_strength"] if axes else None
    version = _digest({"modules": modules, "price_policy": source.get("price_policy"),
                       "archive_transform_sha256": transform_sha256})
    captured_iso, observed_iso = captured.isoformat(), observed.isoformat()
    row = {"trade_date": day, "observed_at": observed_iso,
           "source_snapshot_available_at": observed_iso,
           "universe_available_at": observed_iso,
           "state_available_at": captured_iso, "decision_at": captured_iso,
           "state": state, "temperature": strength,
           "temperature_available_at": captured_iso if strength is not None else None,
           "temperature_definition_id": "directional_strength_v1@" + version if strength is not None else None,
           "scope_id": SCOPE_ID, "taxonomy_version": source["implementation_sha256"],
           "feature_schema_version": version,
           "source_snapshot_sha256": reference["sha256"],
           "universe_snapshot_sha256": source["master_sha256"]}
    return {"snapshot": snapshot, "market_row": row, "emotion_axes": axes}


def _archive_path(root: Path, day: str) -> Path:
    return root / "reports/market_emotion_pit" / (day + ".json")


def read_day(root: Path, day: str, *, now: datetime | None = None) -> dict:
    """Recheck a saved original source; later manifest rebinding cannot rewrite it."""
    root, day = Path(root), _day(day)
    path = _archive_path(root, day)
    if path.is_symlink() or not path.is_file() or path.parent.is_symlink():
        raise ValueError("pit_archive_file_missing_or_unsafe")
    payload = _bounded_json(path, MAX_RECEIPT_BYTES)
    if (payload.get("schema") != SCHEMA or payload.get("target_date") != day
            or any(payload.get(key) != value for key, value in BOUNDARY.items())):
        raise ValueError("pit_archive_identity_invalid")
    captured = _stamp(payload.get("captured_at"), "pit_captured_at")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("pit_read_timezone_required")
    limit = current.astimezone(SHANGHAI)
    if (captured.date().isoformat() != day or captured.time() < time(15)
            or captured > limit):
        raise ValueError("pit_archive_capture_time_invalid")
    reference = payload.get("snapshot_reference")
    target_count = payload.get("target_count")
    if (not isinstance(reference, dict) or not _hex(reference.get("sha256"))
            or type(target_count) is not int or target_count < 1):
        raise ValueError("pit_archive_reference_invalid")
    transform_sha256 = payload.get("archive_transform_sha256")
    checked = _reference(root, day, reference, captured, check_current_code=False,
                         transform_sha256=transform_sha256)
    if target_count != len(checked["snapshot"]["source_references"]):
        raise ValueError("pit_archive_population_changed")
    source = checked["snapshot"]["source_contract"]
    if (payload.get("calendar_sha256") != source["calendar_sha256"]
            or payload.get("universe_master_sha256") != source["master_sha256"]):
        raise ValueError("pit_archive_source_contract_changed")
    expected = checked["market_row"]
    if payload.get("market_row") != expected or payload.get("emotion_axes") != checked["emotion_axes"]:
        raise ValueError("pit_archive_row_changed")
    return payload


def capture(root: Path, result: Mapping[str, Any], *, now: datetime | None = None) -> dict:
    """Cloud-only first observation; a changed same-day source is a conflict."""
    if os.environ.get("REGIS_EXECUTION_ROLE") != "cloud":
        return {"status": "local_unchanged", **BOUNDARY}
    if result.get("status") != "complete":
        return {"status": "producer_incomplete", **BOUNDARY}
    root = Path(root)
    day = _day(result.get("data_as_of"))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("pit_capture_timezone_required")
    captured = current.astimezone(SHANGHAI)
    if captured.date().isoformat() != day or captured.time() < time(15):
        return {"status": "outside_same_day_capture_window", "target_date": day, **BOUNDARY}
    directory = root / "reports/cloud_market_emotion" / day
    manifest_path = directory / "manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("pit_manifest_path_unsafe")
    manifest = read_json(manifest_path)
    reference = manifest.get("snapshot") or {}
    if (manifest.get("schema") != "cloud_market_emotion.v1"
            or manifest.get("target_date") != day or not isinstance(reference, dict)
            or not _hex(reference.get("sha256"))):
        raise ValueError("pit_manifest_identity_invalid")
    from . import emotion_system
    transform_sha256 = sha(Path(emotion_system.__file__))
    checked = _reference(root, day, reference, captured, check_current_code=True,
                         transform_sha256=transform_sha256)
    if (result.get("latest_state") != checked["market_row"]["state"]
            or result.get("histories_computed") != result.get("target")
            or result.get("target") != len(checked["snapshot"]["source_references"])):
        raise ValueError("pit_producer_report_mismatch")
    payload = {"schema": SCHEMA, "target_date": day, "captured_at": captured.isoformat(),
               "snapshot_reference": {key: reference[key] for key in ("path", "sha256", "input_identity")},
               "archive_transform_sha256": transform_sha256,
               "calendar_sha256": checked["snapshot"]["source_contract"]["calendar_sha256"],
               "universe_master_sha256": checked["snapshot"]["source_contract"]["master_sha256"],
               "target_count": result["target"], "market_row": checked["market_row"],
               "emotion_axes": checked["emotion_axes"], **BOUNDARY}
    path = _archive_path(root, day)
    parent = path.parent
    if parent.is_symlink():
        raise ValueError("pit_archive_directory_unsafe")
    parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        try:
            old = read_day(root, day, now=current)
        except (OSError, ValueError, KeyError, TypeError):
            return {"status": "archive_invalid_preserved", "target_date": day, **BOUNDARY}
        return {"status": "unchanged" if old["snapshot_reference"] == payload["snapshot_reference"]
                else "source_revision_conflict", "target_date": day, **BOUNDARY}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    temporary: Path | None = None
    raced = False
    try:
        with tempfile.NamedTemporaryFile(dir=parent, prefix=".pit-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            raced = True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    if raced:
        try:
            old = read_day(root, day, now=current)
        except (OSError, ValueError, KeyError, TypeError):
            return {"status": "archive_invalid_preserved", "target_date": day, **BOUNDARY}
        return {"status": "unchanged" if old["snapshot_reference"] == payload["snapshot_reference"]
                else "source_revision_conflict", "target_date": day, **BOUNDARY}
    read_day(root, day, now=current)
    return {"status": "captured", "target_date": day,
            "archive_sha256": hashlib.sha256(raw).hexdigest(), **BOUNDARY}


def collect(root: Path, calendar: Sequence[str], *, now: datetime | None = None) -> dict:
    """Demand every caller-verified official session; never infer holidays."""
    if not isinstance(calendar, (list, tuple)) or not calendar or len(calendar) > 3000:
        raise ValueError("pit_calendar_invalid")
    sessions = [_day(day) for day in calendar]
    if sessions != sorted(set(sessions)):
        raise ValueError("pit_calendar_not_ordered_unique")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("pit_collect_timezone_required")
    rows, gaps, identities, segments = [], [], set(), []
    previous_identity = None
    root = Path(root)
    for day in sessions:
        try:
            payload = read_day(root, day, now=current)
            row = payload["market_row"]
            rows.append(row)
            identity = tuple(row[key] for key in ("scope_id", "taxonomy_version", "feature_schema_version"))
            identities.add(identity)
            if identity != previous_identity:
                segments.append({"start_date": day, "version_sha256": _digest(identity)})
            previous_identity = identity
        except (OSError, ValueError, KeyError, TypeError) as error:
            gaps.append({"trade_date": day, "reason": str(error)})
    status = "waiting_receipts" if gaps else "version_changed" if len(identities) != 1 else "rows_available"
    if status == "rows_available":
        from .emotion_horizon_research import validate_input
        validate_input(rows, sessions)
    return {"schema": "market_emotion_pit_collect.v1", "status": status,
            "calendar_sessions": len(sessions), "verified_receipts": len(rows),
            "unknown_states": sum(row["state"] == "UNKNOWN" for row in rows),
            "gaps": gaps, "identity_versions": len(identities),
            "version_changed": len(identities) > 1,
            "version_segments": segments,
            "version_change_dates": [segment["start_date"] for segment in segments[1:]],
            "rows": rows if status == "rows_available" else [],
            "chronology": "self_reported_cloud_capture_not_external_timestamp",
            "calendar_authentication": "caller_must_verify_official_calendar", **BOUNDARY}


def current_day_status(root: Path, *, now: datetime | None = None) -> dict:
    """Bounded website status: verify one target-day receipt, not full history."""
    root = Path(root)
    result = {"schema": "market_emotion_pit_status.v1", "status": "waiting_calendar",
              "data_as_of": None, "captured_at": None, "source_snapshot_sha256": None,
              "state": None, "continuous_history_verified": False,
              "participates_in_decision": False, **BOUNDARY}
    try:
        daily = _bounded_json(root / "reports/cloud_market_data.json", MAX_CONTROL_BYTES)
    except (OSError, ValueError):
        return result
    calendar = daily.get("calendar_dates")
    target = daily.get("target_date")
    try:
        _day(target)
        if (daily.get("schema") != "cloud_owned_daily_inputs.v1"
                or not isinstance(calendar, list) or not calendar
                or calendar != sorted(set(calendar)) or calendar[-1] != target
                or any(_day(day) != day for day in calendar)
                or not _hex(daily.get("master_sha256"))):
            return result
    except (TypeError, ValueError):
        return result
    result["data_as_of"] = target
    path = _archive_path(root, target)
    if not path.exists() and not path.is_symlink():
        result["status"] = "waiting_receipt"
        return result
    try:
        receipt = read_day(root, target, now=now)
    except (OSError, ValueError, KeyError, TypeError):
        result["status"] = "archive_invalid"
        return result
    if (receipt["calendar_sha256"] != _digest(calendar)
            or receipt["universe_master_sha256"] != daily["master_sha256"]):
        result["status"] = "source_mismatch"
        return result
    try:
        manifest = _bounded_json(root / "reports/cloud_market_emotion" / target / "manifest.json",
                                 MAX_CONTROL_BYTES)
    except (OSError, ValueError):
        result["status"] = "archive_invalid"
        return result
    current_ref = manifest.get("snapshot")
    if (manifest.get("schema") != CLOUD_SCHEMA or manifest.get("target_date") != target
            or not isinstance(current_ref, dict) or not _hex(current_ref.get("sha256"))
            or not _hex(current_ref.get("input_identity"))):
        result["status"] = "archive_invalid"
        return result
    archived_ref = receipt["snapshot_reference"]
    if (current_ref["sha256"] == archived_ref["sha256"]
            and current_ref["input_identity"] == archived_ref["input_identity"]
            and current_ref.get("path") != archived_ref.get("path")):
        result["status"] = "archive_invalid"
        return result
    if (current_ref["sha256"] != archived_ref["sha256"]
            or current_ref["input_identity"] != archived_ref["input_identity"]):
        try:
            current = (now or datetime.now(timezone.utc)).astimezone(SHANGHAI)
            _reference(root, target, current_ref, current, check_current_code=False,
                       transform_sha256=receipt["archive_transform_sha256"])
        except (OSError, ValueError, KeyError, TypeError):
            result["status"] = "archive_invalid"
            return result
        result["status"] = "source_revision_conflict"
        return result
    result.update(status="captured", captured_at=receipt["captured_at"],
                  source_snapshot_sha256=receipt["market_row"]["source_snapshot_sha256"],
                  state=receipt["market_row"]["state"])
    return result
