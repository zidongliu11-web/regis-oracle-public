"""Finite local observation lineage; board qualification is never a return.

Only the explicit CLI writes. Reads do not acquire data, issue an origin, or
backfill labels. Hashes bind editable local evidence, not external time/rights.
"""
from __future__ import annotations

import base64
from datetime import date, datetime, time, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import unicodedata
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SH_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_ORIGIN = "2026-09-29"
SOURCE = "东方财富板块实时（push2delay 延迟源）"
HORIZONS = (1, 3, 5)
WINDOWS = ((time(16), time(16, 5)), (time(16, 15), time(16, 20)))
MAX_PROTOCOL = 512 * 1024
MAX_RECORD = 16 * 1024 * 1024
MAX_SNAPSHOT = 4 * 1024 * 1024
MAX_RIGHTS = 512 * 1024
PREFIX = "reports/local_forward_chain"
MARKET_PREFIX = "research/data/market_state_daily/v1/snapshots"
RIGHTS_PREFIX = "reports/source_rights/"
BOUNDARY = {
    "execution_location": "local", "research_only": True,
    "cloud_backup_verified": False, "external_time_attested": False,
    "immutable_storage_attested": False, "full_market_verified": False,
    "membership_pit_verified": False, "current_mainline_validated": False,
    "leader_validated": False, "forecast_validated": False,
    "returns_computed": False, "economic_target_A_validated": False,
    "generates_alert": False, "changes_trade_authority": False,
    "sequential_rl_trained": False,
}
CODE_PATHS = (
    "mom_trading/local_forward_chain.py", "scripts/local_forward_chain.py",
    "mom_trading/market_intelligence.py", "mom_trading/cloud_sector_close.py",
    "mom_trading/market_sector_strategy_adapter.py",
    "mom_trading/sector_forward_ledger.py", "mom_trading/sector_rotation_review.py",
    "mom_trading/next_session_sectors.py", "mom_trading/market_state_daily.py",
    "research/market_rules.py", "research/MARKET_SECTOR_STRATEGY_ROUTER_V2_PROTOCOL.json",
)


def _code_identity():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in CODE_PATHS}


LOADED_SOURCES = _code_identity()


def _sources():
    if _code_identity() != LOADED_SOURCES:
        raise ValueError("loaded_source_changed")
    return dict(LOADED_SOURCES)


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    def invalid(_):
        raise ValueError("nonfinite_json_value")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp_invalid")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(SH_TZ)


def _now(now=None):
    return _stamp((now or datetime.now(timezone.utc)).isoformat())


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("day_invalid")
    return value


def _redirected(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _path(root, relative):
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or ":" in relative or "\x00" in relative):
        raise ValueError("artifact_path_invalid")
    parts = relative.split("/")
    if (PurePosixPath(relative).is_absolute()
            or any(p in {"", ".", ".."} or p != p.rstrip(" .") for p in parts)):
        raise ValueError("artifact_path_invalid")
    base = Path(root).absolute()
    path = base.joinpath(*parts)
    # Do not resolve away a junction before checking it.
    if any(_redirected(p) for p in (path, *path.parents)):
        raise ValueError("artifact_path_redirected")
    return path


def _read_bytes(path, limit):
    if _redirected(path) or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("artifact_file_invalid")
    before = path.stat()
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    after = path.stat()
    if len(raw) > limit:
        raise ValueError("artifact_too_large")
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("artifact_changed_during_read")
    return raw


def _read(root, relative, limit=MAX_RECORD):
    return _json(_read_bytes(_path(root, relative), limit))


def _local():
    if os.environ.get("REGIS_EXECUTION_ROLE") != "local":
        raise ValueError("local_execution_role_required")


def _publish(root, relative, value, limit=MAX_RECORD):
    _local()
    path = _path(root, relative)
    raw = _bytes(value)
    if len(raw) > limit:
        raise ValueError("artifact_too_large")
    path.parent.mkdir(parents=True, exist_ok=True)
    _path(root, relative)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".local-forward-",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _path(root, relative)
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def _seal(value, field):
    return {**value, field: _digest(value)}


def _verify(value, schema, fields, field):
    if not isinstance(value, dict) or set(value) != fields | set(BOUNDARY) | {field}:
        raise ValueError("record_schema_invalid")
    if (value["schema"] != schema
            or any(type(value[k]) is not type(v) or value[k] != v
                   for k, v in BOUNDARY.items())
            or value[field] != _digest({k: v for k, v in value.items() if k != field})):
        raise ValueError("record_identity_invalid")


def _at(day, value):
    return datetime.combine(date.fromisoformat(day), value, SH_TZ)


def _registered_windows(sessions):
    return [{"day": day, "slots": [
        {"index": i, "start": _at(day, start).isoformat(), "end": _at(day, end).isoformat()}
        for i, (start, end) in enumerate(WINDOWS)]} for day in sessions[:6]]


def _bundle(root, now):
    from .sector_forward_ledger import (
        MAX_CALENDAR_BYTES, MAX_CALENDAR_RECEIPT_BYTES, _calendar)
    raw = _read_bytes(_path(root, "reports/current_market_calendar.json"), MAX_CALENDAR_BYTES)
    calendar = _json(raw)
    receipt = _read_bytes(_path(root, calendar["source_receipt"]["path"]),
                         MAX_CALENDAR_RECEIPT_BYTES)
    _json(receipt)
    sessions, sha = _calendar(Path(root), now, frozen_raw=(raw, receipt))
    return sessions, sha, {"calendar_b64": base64.b64encode(raw).decode("ascii"),
                           "source_receipt_b64": base64.b64encode(receipt).decode("ascii")}


def _calendar_bundle(root, bundle, now):
    from .sector_forward_ledger import (
        MAX_CALENDAR_BYTES, MAX_CALENDAR_RECEIPT_BYTES, _calendar)
    if not isinstance(bundle, dict) or set(bundle) != {"calendar_b64", "source_receipt_b64"}:
        raise ValueError("calendar_bundle_invalid")
    values = []
    for key, limit in (("calendar_b64", MAX_CALENDAR_BYTES),
                       ("source_receipt_b64", MAX_CALENDAR_RECEIPT_BYTES)):
        encoded = bundle[key]
        if not isinstance(encoded, str) or len(encoded) > 4 * ((limit + 2) // 3):
            raise ValueError("calendar_bundle_invalid")
        raw = base64.b64decode(encoded, validate=True)
        _json(raw)
        values.append(raw)
    calendar = _json(values[0])
    _path(root, calendar["source_receipt"]["path"])
    return _calendar(Path(root), now, frozen_raw=tuple(values))


def _evidence_has_content(raw):
    """Conservatively reject bounded blank carriers, without assessing rights.

    Try BOM-aware and common unmarked Unicode encodings. A blank plausible
    decoding rejects ambiguous bytes; normal binary document headers remain
    content. This is an empty-file gate, never a licence authenticity check.
    """
    if not raw:
        return False
    for encoding in ("utf-8-sig", "utf-16", "utf-32", "utf-16-le", "utf-16-be",
                     "utf-32-le", "utf-32-be", "latin-1"):
        try:
            decoded = raw.decode(encoding)
        except UnicodeError:
            continue
        if not any(not char.isspace() and unicodedata.category(char) not in {"Cc", "Cf"}
                   for char in decoded):
            return False
    return True


def _permission(root, receipt_name, registered, last_capture):
    if receipt_name is None:
        return {"status": "unverified", "capture_enabled": False, "receipt": None}
    if not isinstance(receipt_name, str) or not receipt_name.startswith(RIGHTS_PREFIX):
        raise ValueError("rights_receipt_path_invalid")
    raw = _read_bytes(_path(root, receipt_name), MAX_RIGHTS)
    rights = _json(raw)
    fields = {"schema", "source", "usage", "capture_allowed", "publish_allowed",
              "reviewed_at", "valid_until", "evidence"}
    if (not isinstance(rights, dict) or set(rights) != fields
            or rights["schema"] != "local_source_rights.v1"
            or rights["source"] != SOURCE or rights["usage"] != "local_private_research"
            or rights["capture_allowed"] is not True or rights["publish_allowed"] is not False
            or _stamp(rights["reviewed_at"]) > registered
            or _stamp(rights["valid_until"]) < last_capture
            or not isinstance(rights["evidence"], list) or not 1 <= len(rights["evidence"]) <= 8):
        raise ValueError("rights_receipt_invalid")
    seen = set()
    for ref in rights["evidence"]:
        if (not isinstance(ref, dict) or set(ref) != {"path", "sha256"}
                or not isinstance(ref["path"], str) or not ref["path"].startswith(RIGHTS_PREFIX)
                or ref["path"] in seen):
            raise ValueError("rights_evidence_invalid")
        seen.add(ref["path"])
        evidence_raw = _read_bytes(_path(root, ref["path"]), MAX_RIGHTS)
        if not _evidence_has_content(evidence_raw):
            raise ValueError("rights_evidence_empty")
        if _sha(evidence_raw) != ref["sha256"]:
            raise ValueError("rights_evidence_hash_invalid")
    # The explicit operator memo binds evidence. It cannot authenticate a licence.
    return {"status": "operator_attested", "capture_enabled": True,
            "receipt": {"path": receipt_name, "sha256": _sha(raw)}}


PROTOCOL_FIELDS = {"schema", "origin_day", "registered_at", "sessions",
                   "calendar_sha256", "calendar_bundle", "source_contract", "source",
                   "source_permission", "windows", "capture_budget", "target_sessions"}


def register(*, root=ROOT, origin_day=DEFAULT_ORIGIN, now=None, rights_receipt=None):
    _local()
    current = _now(now)
    day = _day(origin_day)
    if current >= _at(day, WINDOWS[0][0]):
        raise ValueError("registration_after_first_window")
    sessions, sha, bundle = _bundle(root, current)
    index = sessions.index(day)
    frozen = sessions[index:index + 7]
    if len(frozen) != 7:
        raise ValueError("calendar_successor_missing")
    permission = _permission(root, rights_receipt, current, _at(frozen[5], WINDOWS[-1][1]))
    protocol = _seal({
        "schema": "local_forward_protocol.v1", "origin_day": day,
        "registered_at": current.isoformat(), "sessions": frozen,
        "calendar_sha256": sha, "calendar_bundle": bundle, "source_contract": _sources(),
        "source": SOURCE, "source_permission": permission,
        "windows": _registered_windows(frozen), "capture_budget": 12,
        "target_sessions": {str(h): frozen[h] for h in HORIZONS}, **BOUNDARY,
    }, "protocol_sha256")
    _publish(root, f"{PREFIX}/{day}/protocol.json", protocol, MAX_PROTOCOL)
    return protocol


def read_protocol(*, root=ROOT, origin_day=DEFAULT_ORIGIN, now=None):
    current = _now(now)
    day = _day(origin_day)
    value = _read(root, f"{PREFIX}/{day}/protocol.json", MAX_PROTOCOL)
    _verify(value, "local_forward_protocol.v1", PROTOCOL_FIELDS, "protocol_sha256")
    registered = _stamp(value["registered_at"])
    if not registered <= current or registered >= _at(day, WINDOWS[0][0]):
        raise ValueError("registration_clock_invalid")
    sessions, sha = _calendar_bundle(root, value["calendar_bundle"], registered)
    index = sessions.index(day)
    frozen = sessions[index:index + 7]
    if (len(frozen) != 7 or value["origin_day"] != day or value["sessions"] != frozen
            or value["calendar_sha256"] != sha or value["source_contract"] != _sources()
            or value["source"] != SOURCE or type(value["capture_budget"]) is not int
            or value["capture_budget"] != 12
            or _bytes(value["windows"]) != _bytes(_registered_windows(frozen))
            or value["target_sessions"] != {str(h): frozen[h] for h in HORIZONS}):
        raise ValueError("protocol_semantics_invalid")
    receipt = value["source_permission"].get("receipt")
    expected = _permission(root, receipt["path"] if receipt is not None else None,
                           registered, _at(frozen[5], WINDOWS[-1][1]))
    if _bytes(value["source_permission"]) != _bytes(expected):
        raise ValueError("rights_binding_changed")
    current_sessions, _, _ = _bundle(root, current)
    if day not in current_sessions or current_sessions[current_sessions.index(day):
                                                          current_sessions.index(day) + 7] != frozen:
        raise ValueError("calendar_window_changed")
    return value


def _slot(protocol, stamp):
    day = stamp.date().isoformat()
    if day not in protocol["sessions"][:6]:
        return None
    return next((i for i, (start, end) in enumerate(WINDOWS)
                 if start <= stamp.time() < end), None)


def _assessment(snapshot, protocol, day, started, completed, slot):
    if (started > completed or _slot(protocol, started) != slot
            or _slot(protocol, completed) != slot or completed.date().isoformat() != day):
        return ["capture_crossed_registered_window"], None
    try:
        from .cloud_sector_close import assess
        if not isinstance(snapshot, dict) or snapshot.get("source") != SOURCE:
            raise ValueError("supplier_identity_invalid")
        if not started <= _stamp(snapshot["fetch_started_at"]) <= _stamp(snapshot["fetched_at"]) <= completed:
            raise ValueError("fetch_time_invalid")
        assessment = assess(snapshot, day, completed)
        return [], assessment
    except (KeyError, ValueError, TypeError, OverflowError, AttributeError):
        return ["supplier_snapshot_rejected"], None


CAPTURE_FIELDS = {"schema", "origin_day", "day", "window_index", "protocol_sha256",
                  "source_contract", "started_at", "observed_at", "snapshot", "errors"}


def _read_capture(root, protocol, day, slot, current):
    value = _read(root, f"{PREFIX}/{protocol['origin_day']}/captures/{day}/{slot:02}.json")
    _verify(value, "local_forward_capture.v1", CAPTURE_FIELDS, "packet_sha256")
    if (value["protocol_sha256"] != protocol["protocol_sha256"]
            or value["source_contract"] != protocol["source_contract"]
            or value["origin_day"] != protocol["origin_day"] or value["day"] != day
            or type(value["window_index"]) is not int or value["window_index"] != slot
            or protocol["source_permission"]["capture_enabled"] is not True):
        raise ValueError("capture_binding_invalid")
    started, completed = _stamp(value["started_at"]), _stamp(value["observed_at"])
    if not _stamp(protocol["registered_at"]) <= started <= completed <= current:
        raise ValueError("capture_clock_invalid")
    if started.date().isoformat() != day or _slot(protocol, started) != slot:
        raise ValueError("capture_clock_invalid")
    if value["snapshot"] is None:
        errors, assessment = ["source_request_failed"], None
    else:
        if len(_bytes(value["snapshot"])) > MAX_SNAPSHOT:
            raise ValueError("snapshot_too_large")
        errors, assessment = _assessment(value["snapshot"], protocol, day, started, completed, slot)
    if value["errors"] != errors:
        raise ValueError("capture_assessment_changed")
    return value, assessment


def _captures(root, protocol, day, current):
    if day not in protocol["sessions"][:6]:
        raise ValueError("capture_day_unregistered")
    results, claims = [], 0
    for slot in range(2):
        prefix = f"{PREFIX}/{protocol['origin_day']}/captures/{day}/{slot:02}"
        claim = _path(root, prefix + ".claim")
        packet = _path(root, prefix + ".json")
        if claim.exists():
            claim_value = _read(root, prefix + ".claim", MAX_PROTOCOL)
            if (not isinstance(claim_value, dict)
                    or set(claim_value) != {"protocol_sha256", "day", "window_index", "started_at"}
                    or claim_value["protocol_sha256"] != protocol["protocol_sha256"]
                    or claim_value["day"] != day or type(claim_value["window_index"]) is not int
                    or claim_value["window_index"] != slot
                    or _slot(protocol, _stamp(claim_value["started_at"])) != slot
                    or _stamp(claim_value["started_at"]).date().isoformat() != day
                    or not _stamp(protocol["registered_at"]) <= _stamp(claim_value["started_at"]) <= current):
                raise ValueError("capture_claim_invalid")
            claims += 1
        if packet.exists():
            results.append(_read_capture(root, protocol, day, slot, current))
    if len(results) > 1 and not results[0][0]["errors"]:
        raise ValueError("capture_after_first_success")
    return results, claims


def _accepted(root, protocol, day, current):
    results, claims = _captures(root, protocol, day, current)
    accepted = [(packet, assessment) for packet, assessment in results if not packet["errors"]]
    if claims:
        raise ValueError("unrecovered_capture_claim")
    if accepted:
        return accepted[0]
    raise ValueError("capture_rejected" if results else "capture_missing")


def _fetch(root):
    from .market_intelligence import MarketIntelligence
    return MarketIntelligence(None, Path(root) / "unused_local_forward_cache",
                              lambda _: ([], {})).market_sectors(force=True)


def capture_once(*, root=ROOT, origin_day=DEFAULT_ORIGIN, now=None, fetch=None, clock=None):
    _local()
    started = _now(now)
    protocol = read_protocol(root=root, origin_day=origin_day, now=started)
    if protocol["source_permission"]["capture_enabled"] is not True:
        raise ValueError("source_rights_unverified")
    slot = _slot(protocol, started)
    if slot is None:
        raise ValueError("outside_registered_capture_window")
    day = started.date().isoformat()
    previous, claims = _captures(root, protocol, day, started)
    if claims:
        raise ValueError("unrecovered_capture_claim")
    if any(not packet["errors"] for packet, _ in previous):
        raise ValueError("capture_after_first_success")
    relative = f"{PREFIX}/{origin_day}/captures/{day}/{slot:02}"
    if _path(root, relative + ".json").exists():
        raise ValueError("capture_slot_already_used")
    claim = _path(root, relative + ".claim")
    claim.parent.mkdir(parents=True, exist_ok=True)
    _path(root, relative + ".claim")
    with claim.open("xb") as stream:
        stream.write(_bytes({"protocol_sha256": protocol["protocol_sha256"],
                             "day": day, "window_index": slot, "started_at": started.isoformat()}))
        stream.flush()
        os.fsync(stream.fileno())
    snapshot = None
    request_failed = False
    try:
        fetched = (fetch or (lambda: _fetch(root)))()
        if not isinstance(fetched, dict):
            raise ValueError("snapshot_not_object")
        raw = _bytes(fetched)
        if len(raw) > MAX_SNAPSHOT:
            raise ValueError("snapshot_too_large")
        snapshot = _json(raw)
    except Exception:
        request_failed = True  # Never publish endpoint URLs, credentials, exception text.
    completed = _now(clock() if clock is not None else None)
    _sources()
    # Re-read the protocol and permission after the request as well.
    if read_protocol(root=root, origin_day=origin_day, now=completed) != protocol:
        raise ValueError("protocol_changed_during_capture")
    errors, _ = (["source_request_failed"], None) if request_failed else _assessment(
        snapshot, protocol, day, started, completed, slot)
    record = _seal({
        "schema": "local_forward_capture.v1", "origin_day": origin_day, "day": day,
        "window_index": slot, "protocol_sha256": protocol["protocol_sha256"],
        "source_contract": protocol["source_contract"], "started_at": started.isoformat(),
        "observed_at": completed.isoformat(), "snapshot": snapshot, "errors": errors, **BOUNDARY,
    }, "packet_sha256")
    _publish(root, relative + ".json", record)
    claim.unlink()  # Only this successful publication's claim, never stale claims.
    return record


def _boards(packet, assessment):
    from .market_sector_strategy_adapter import _sector_rows
    from .next_session_sectors import _market_cohort, _preparation_pool
    rows, _ = _sector_rows(packet["snapshot"])
    selected = _preparation_pool(packet["snapshot"], assessment["rows"])
    qualified = {(r["kind"], r["code"]) for r in selected}
    eligible = [r for r in rows if r["constituent_count"] >= 5
                and r["advance_coverage_pct"] is not None]
    values = {kind: [r["change_pct"] for r in eligible if r["kind"] == kind]
              for kind in ("industry", "concept")}
    output = []
    for row in rows:
        cohort = _market_cohort(row)
        evaluable = row["constituent_count"] >= 5 and row["advance_coverage_pct"] is not None
        group = values[row["kind"]]
        rank = round(100 * (sum(v < row["change_pct"] for v in group)
                           + .5 * sum(v == row["change_pct"] for v in group)) / len(group), 3) if evaluable else None
        state = ("excluded_behavior_cohort" if cohort else "unassessable" if not evaluable
                 else "qualified" if (row["kind"], row["code"]) in qualified else "not_qualified")
        failures = (["behavior_cohort"] if cohort else ["changing_members_below_5"] if not evaluable
                    else [name for name, ok in (
                        ("change_not_positive", row["change_pct"] > 0),
                        ("breadth_below_60", row["advance_coverage_pct"] >= 60),
                        ("rank_below_90", rank >= 90)) if not ok])
        output.append({k: row[k] for k in ("kind", "code", "name", "change_pct",
                                         "advance_coverage_pct", "data_as_of")})
        output[-1].update(changing_member_count=row["constituent_count"], change_rank_pct=rank,
                          screen_status=state, failures=failures)
    return sorted(output, key=lambda row: (row["kind"], row["code"]))


def _unknown_market(status="snapshot_missing", *, observed_at=None,
                    rejection_reason="market_snapshot_missing"):
    return {"state": "UNKNOWN", "status": status, "snapshot": None, "generated_at": None,
            "observed_at": observed_at, "rejection_reason": rejection_reason,
            "as_of": None, "dimension_status": {}, "decision_eligible": False,
            "state_model_validated": False, "full_market_verified": False,
            "risk_gate": "new_risk_closed_market_unknown"}


def _market(root, day, issued):
    relative = f"{MARKET_PREFIX}/{day}.json"
    base = _unknown_market(observed_at=issued.isoformat())
    try:
        path = _path(root, relative)
    except ValueError:
        return {**base, "status": "snapshot_invalid_or_not_visible",
                "rejection_reason": "market_snapshot_path_invalid"}
    try:
        raw = _read_bytes(path, MAX_PROTOCOL)
    except FileNotFoundError:
        return base
    except (OSError, ValueError):
        return {**base, "status": "snapshot_invalid_or_not_visible",
                "rejection_reason": "market_snapshot_unreadable_or_unbounded"}
    identity = {"path": relative, "sha256": _sha(raw)}
    generated = None
    reason = "market_snapshot_json_or_shape_invalid"
    try:
        value = _json(raw)
        if not isinstance(value, dict):
            raise ValueError("market_shape_invalid")
        reason = "market_snapshot_clock_invalid"
        generated = _stamp(value["generated_at"])
        if (value["trade_date"] != day or generated.date().isoformat() != day
                or not _at(day, time(15)) <= generated <= issued):
            raise ValueError("market_clock_invalid")
        reason = "market_evidence_unavailable_or_changed"
        evidence = value.get("evidence")
        if not isinstance(evidence, dict) or not 1 <= len(evidence) <= 32:
            raise ValueError("market_evidence_invalid")
        for ref in evidence.values():
            if _sha(_read_bytes(_path(root, ref["path"]), 32 * 1024 * 1024)) != ref["sha256"]:
                raise ValueError("market_evidence_changed")
        from .market_state_daily import verify_snapshot
        reason = "market_verification_failed"
        verification = verify_snapshot(path, project_root=Path(root))
        if not verification["valid"] or verification["snapshot_sha256"] != _sha(raw):
            raise ValueError("market_verification_failed")
        if _read_bytes(path, MAX_PROTOCOL) != raw:
            raise ValueError("market_changed_during_read")
        return {**_unknown_market("evidence_present_model_unvalidated",
                                  observed_at=issued.isoformat(), rejection_reason=None),
                "snapshot": identity,
                "generated_at": generated.isoformat(), "as_of": _at(day, time(15)).isoformat(),
                "dimension_status": {key: value["dimensions"][key].get("status")
                                     for key in ("B", "L", "F", "C", "P")}}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        # Retain every safely read failed original; never replace its identity.
        return {**base, "status": "snapshot_invalid_or_not_visible", "snapshot": identity,
                "generated_at": generated.isoformat() if generated is not None else None,
                "rejection_reason": reason}


ORIGIN_FIELDS = {"schema", "origin_day", "issued_at", "next_open_at", "protocol_sha256",
                 "packet_sha256", "capture_observed_at", "source_as_of", "market",
                 "boards", "board_count", "target_meaning"}
TARGET_MEANING = "same_frozen_board_screen_passes_at_exact_future_session_not_return_or_mainline"


def issue_origin(*, root=ROOT, origin_day=DEFAULT_ORIGIN, now=None):
    _local()
    current = _now(now)
    protocol = read_protocol(root=root, origin_day=origin_day, now=current)
    next_open = _at(protocol["sessions"][1], time(9, 30))
    if not _at(origin_day, time(16)) <= current < next_open:
        raise ValueError("outside_origin_issuance_window")
    packet, assessment = _accepted(root, protocol, origin_day, current)
    boards = _boards(packet, assessment)
    record = _seal({
        "schema": "local_forward_origin.v1", "origin_day": origin_day,
        "issued_at": current.isoformat(), "next_open_at": next_open.isoformat(),
        "protocol_sha256": protocol["protocol_sha256"], "packet_sha256": packet["packet_sha256"],
        "capture_observed_at": packet["observed_at"], "source_as_of": assessment["source_as_of"],
        "market": _market(root, origin_day, current), "boards": boards, "board_count": len(boards),
        "target_meaning": TARGET_MEANING, **BOUNDARY,
    }, "origin_sha256")
    _publish(root, f"{PREFIX}/{origin_day}/origin.json", record)
    return record


def read_origin(*, root=ROOT, origin_day=DEFAULT_ORIGIN, now=None, protocol=None):
    current = _now(now)
    actual_protocol = read_protocol(root=root, origin_day=origin_day, now=current)
    if protocol is not None and _bytes(protocol) != _bytes(actual_protocol):
        raise ValueError("origin_protocol_invalid")
    protocol = actual_protocol
    value = _read(root, f"{PREFIX}/{origin_day}/origin.json")
    _verify(value, "local_forward_origin.v1", ORIGIN_FIELDS, "origin_sha256")
    issued = _stamp(value["issued_at"])
    next_open = _at(protocol["sessions"][1], time(9, 30))
    if not _at(origin_day, time(16)) <= issued < next_open or issued > current:
        raise ValueError("origin_clock_invalid")
    packet, assessment = _accepted(root, protocol, origin_day, issued)
    boards = _boards(packet, assessment)
    if (value["origin_day"] != origin_day or value["next_open_at"] != next_open.isoformat()
            or value["protocol_sha256"] != protocol["protocol_sha256"]
            or value["packet_sha256"] != packet["packet_sha256"]
            or value["capture_observed_at"] != packet["observed_at"]
            or value["source_as_of"] != assessment["source_as_of"]
            or _bytes(value["boards"]) != _bytes(boards) or type(value["board_count"]) is not int
            or value["board_count"] != len(boards) or value["target_meaning"] != TARGET_MEANING):
        raise ValueError("origin_projection_changed")
    market = value["market"]
    if market.get("snapshot") is not None:
        if _bytes(market) != _bytes(_market(root, origin_day, issued)):
            raise ValueError("origin_market_evidence_changed")
    else:
        allowed = [_unknown_market(observed_at=issued.isoformat())] + [
            _unknown_market("snapshot_invalid_or_not_visible", observed_at=issued.isoformat(),
                            rejection_reason=reason) for reason in (
                "market_snapshot_path_invalid", "market_snapshot_unreadable_or_unbounded")]
        if _bytes(market) not in [_bytes(item) for item in allowed]:
            raise ValueError("origin_market_boundary_invalid")
    return value


def _horizon(value):
    if type(value) is not int or value not in HORIZONS:
        raise ValueError("horizon_invalid")
    return value


def _label_payload(root, protocol, origin, horizon, current):
    target = protocol["sessions"][horizon]
    matured = _at(target, WINDOWS[-1][1])
    if current < matured:
        raise ValueError("label_not_matured")
    path = []
    for day in protocol["sessions"][1:horizon + 1]:
        try:
            packet, assessment = _accepted(root, protocol, day, current)
            path.append({"day": day, "status": "verified", "packet_sha256": packet["packet_sha256"],
                         "observed_at": packet["observed_at"],
                         "boards": {(r["kind"], r["code"]): r for r in _boards(packet, assessment)}})
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            path.append({"day": day, "status": "capture_missing_rejected_or_invalid",
                         "packet_sha256": None, "observed_at": None, "boards": {}})
    output = []
    for row in origin["boards"]:
        key = row["kind"], row["code"]
        reason, missing_day, label = None, None, None
        if row["screen_status"] in {"excluded_behavior_cohort", "unassessable"}:
            reason = "origin_" + row["screen_status"]
        else:
            for slot in path:
                later = slot["boards"].get(key)
                if slot["status"] != "verified":
                    reason = slot["status"]
                elif later is None or later["name"] != row["name"]:
                    reason = "board_identity_changed_or_absent"
                elif later["screen_status"] in {"unassessable", "excluded_behavior_cohort"}:
                    reason = "board_not_evaluable"
                if reason is not None:
                    missing_day = slot["day"]
                    break
            if reason is None:
                label = int(path[-1]["boards"][key]["screen_status"] == "qualified")
        output.append({"kind": row["kind"], "code": row["code"], "name": row["name"],
                       "origin_screen_status": row["screen_status"],
                       "qualification_label": label, "reason": reason, "missing_day": missing_day})
    return _seal({
        "schema": "local_forward_label.v1", "origin_day": protocol["origin_day"],
        "protocol_sha256": protocol["protocol_sha256"], "origin_sha256": origin["origin_sha256"],
        "horizon": horizon, "target_day": target, "matured_at": matured.isoformat(),
        "evaluated_at": current.isoformat(), "target_meaning": TARGET_MEANING,
        "path": [{k: v for k, v in slot.items() if k != "boards"} for slot in path],
        "rows": output, "board_count": len(output),
        "evaluable_count": sum(r["qualification_label"] is not None for r in output),
        "unassessable_count": sum(r["qualification_label"] is None for r in output), **BOUNDARY,
    }, "label_sha256")


LABEL_FIELDS = {"schema", "origin_day", "protocol_sha256", "origin_sha256", "horizon",
                "target_day", "matured_at", "evaluated_at", "target_meaning", "path", "rows",
                "board_count", "evaluable_count", "unassessable_count"}


def publish_label(*, root=ROOT, origin_day=DEFAULT_ORIGIN, horizon, now=None):
    _local()
    current = _now(now)
    horizon = _horizon(horizon)
    protocol = read_protocol(root=root, origin_day=origin_day, now=current)
    # Check maturity before opening an origin or any future capture.
    if current < _at(protocol["sessions"][horizon], WINDOWS[-1][1]):
        raise ValueError("label_not_matured")
    origin = read_origin(root=root, origin_day=origin_day, now=current, protocol=protocol)
    value = _label_payload(root, protocol, origin, horizon, current)
    _publish(root, f"{PREFIX}/{origin_day}/labels/{horizon}.json", value)
    return value


def read_label(*, root=ROOT, origin_day=DEFAULT_ORIGIN, horizon, now=None, protocol=None, origin=None):
    current = _now(now)
    horizon = _horizon(horizon)
    actual_protocol = read_protocol(root=root, origin_day=origin_day, now=current)
    if protocol is not None and _bytes(protocol) != _bytes(actual_protocol):
        raise ValueError("label_protocol_invalid")
    protocol = actual_protocol
    if current < _at(protocol["sessions"][horizon], WINDOWS[-1][1]):
        raise ValueError("label_not_matured")
    value = _read(root, f"{PREFIX}/{origin_day}/labels/{horizon}.json")
    _verify(value, "local_forward_label.v1", LABEL_FIELDS, "label_sha256")
    evaluated = _stamp(value["evaluated_at"])
    if evaluated > current:
        raise ValueError("label_clock_invalid")
    actual_origin = read_origin(root=root, origin_day=origin_day, now=evaluated, protocol=protocol)
    if origin is not None and _bytes(origin) != _bytes(actual_origin):
        raise ValueError("label_origin_invalid")
    origin = actual_origin
    if _bytes(value) != _bytes(_label_payload(root, protocol, origin, horizon, evaluated)):
        raise ValueError("label_projection_changed")
    return value


def current_status(*, root=ROOT, origin_day=DEFAULT_ORIGIN, now=None):
    current = _now(now)
    base = {"schema": "local_forward_status.v1", "origin_day": origin_day,
            "observed_at": current.isoformat(), "writes_performed": False,
            "protocol_sha256": None, "source_permission_status": "unverified",
            "capture_enabled": False, "captures": [], "attempts": 0, "accepted": 0,
            "rejected": 0, "unrecovered_claims": 0, "origin": None, "labels": [], **BOUNDARY}
    try:
        if not _path(root, f"{PREFIX}/{_day(origin_day)}/protocol.json").exists():
            return {**base, "status": "unregistered",
                    "summary": "新本机前向协议尚未登记；真实起点与标签均为 0。"}
        protocol = read_protocol(root=root, origin_day=origin_day, now=current)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return {**base, "status": "evidence_unavailable",
                "summary": "本机前向协议、来源、日历或权限原件复核失败；不回退旧记录。"}
    base.update(protocol_sha256=protocol["protocol_sha256"], sessions=protocol["sessions"],
                registered_at=protocol["registered_at"], target_sessions=protocol["target_sessions"],
                source_permission_status=protocol["source_permission"]["status"],
                capture_enabled=protocol["source_permission"]["capture_enabled"])
    try:
        for day in protocol["sessions"][:6]:
            if current < _at(day, WINDOWS[0][0]):
                base["captures"].append({"day": day, "status": "future_window", "attempts": 0, "accepted": 0})
                continue
            captures, claims = _captures(root, protocol, day, current)
            count = sum(not packet["errors"] for packet, _ in captures)
            base["attempts"] += len(captures)
            base["accepted"] += count
            base["rejected"] += len(captures) - count
            base["unrecovered_claims"] += claims
            base["captures"].append({"day": day, "attempts": len(captures), "accepted": count,
                                     "status": "verified" if count and not claims else
                                     "claim_unrecovered" if claims else
                                     "rejected" if captures else "expired_missing" if current >= _at(day, WINDOWS[-1][1])
                                     else "waiting_window"})
        path = _path(root, f"{PREFIX}/{origin_day}/origin.json")
        origin = read_origin(root=root, origin_day=origin_day, now=current, protocol=protocol) if path.exists() else None
        if origin is not None:
            base["origin"] = {k: origin[k] for k in (
                "issued_at", "origin_sha256", "capture_observed_at", "source_as_of", "board_count", "market")}
        for horizon in HORIZONS:
            target = protocol["sessions"][horizon]
            state = {"horizon": horizon, "target_day": target, "evaluable_count": 0, "unassessable_count": 0}
            if current < _at(target, WINDOWS[-1][1]):
                state["status"] = "not_matured"
            elif origin is None:
                state["status"] = "origin_missing"
            elif not _path(root, f"{PREFIX}/{origin_day}/labels/{horizon}.json").exists():
                state["status"] = "not_published"
            else:
                label = read_label(root=root, origin_day=origin_day, horizon=horizon,
                                   now=current, protocol=protocol, origin=origin)
                state.update(status="published_proxy_only", evaluable_count=label["evaluable_count"],
                             unassessable_count=label["unassessable_count"], label_sha256=label["label_sha256"])
            base["labels"].append(state)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return {**base, "status": "evidence_unavailable", "origin": None, "labels": [],
                "summary": "本机前向原件、发行起点或标签复核失败；不沿用旧成功状态。"}
    status = ("source_rights_unverified" if not base["capture_enabled"] else
              "proxy_origin_observed" if base["origin"] else "waiting_origin")
    return {**base, "status": status,
            "summary": ("前向协议已登记，来源权益未核，采集关闭；" if not base["capture_enabled"]
                        else "本机私有研究权益仅操作者声明，逐份原件另验；")
            + f"实际通过盘后包 {base['accepted']}，研究起点 {int(base['origin'] is not None)}，"
            + f"成熟资格标签 {sum(r['status'] == 'published_proxy_only' for r in base['labels'])}。"
            + "市场未知时关闭新增风险；板块资格不是收益、龙头或已验证预测，完整第一阶段未通过。"}
