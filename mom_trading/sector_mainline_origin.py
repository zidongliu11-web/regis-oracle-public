"""First-write, research-only after-close origin for future board studies.

This is an observation record, not a validated forecast or trade signal. The
verified calendar, board close packet and market PIT receipt must all exist
before the following open. No future quote or label is read.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any

from .cloud_research_worker import ROOT
from .emotion_pit_archive import SCOPE_ID, read_day as read_market_pit
from .market_sector_strategy_adapter import SH_TZ, _sector_rows
from .sector_forward_ledger import HORIZONS, _calendar, _read_close, _slot


SCHEMA = "sector_mainline_origin.v1"
MAX_BOARD_ROWS = 20_000
MAX_RECORD_BYTES = 16 * 1024 * 1024
BOUNDARY = {"research_only": True, "forecast_validated": False,
            "production_eligible": False, "generates_alert": False,
            "returns_computed": False, "leader_validated": False,
            "changes_trade_authority": False}
BOARD_FIELDS = {"kind", "code", "name", "screen_status", "change_pct",
                "advance_coverage_pct", "constituent_count", "data_as_of"}
RECORD_FIELDS = {"schema", "status", "origin_day", "issued_at", "next_open_at",
                 "calendar_window", "calendar_sha256", "target_sessions", "market",
                 "sector", "boards", "board_count", "issuer_sources",
                 "calendar_alignment", "chronology", "target_meaning", "summary",
                 "input_sha256", "record_sha256", *BOUNDARY}
STATES = {"RISK_OFF", "STABILIZING", "RISK_ON", "OVERHEATED"}
AXES = {"B_strength", "L_strength", "F_stress", "C_stress", "directional_strength"}
AXIS_FIELDS = AXES | {"raw_state"}


def _day(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("origin_day_invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("origin_day_invalid") from error
    if parsed.isoformat() != value:
        raise ValueError("origin_day_invalid")
    return value


def _stamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("origin_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("origin_timestamp_invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("origin_timezone_required")
    return parsed.astimezone(SH_TZ)


def _hex(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and not set(value) - set("0123456789abcdef")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False)
                          .encode("utf-8")).hexdigest()


def _source_code_identity() -> dict[str, str]:
    directory = Path(__file__).resolve().parent
    names = ("sector_mainline_origin.py", "sector_forward_ledger.py",
             "sector_rotation_review.py", "next_session_sectors.py")
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in names}


def _window(sessions: list[str], current: datetime) -> int | None:
    for index in range(len(sessions) - 1):
        close = datetime.combine(date.fromisoformat(sessions[index]), time(16), SH_TZ)
        next_open = datetime.combine(date.fromisoformat(sessions[index + 1]), time(9, 30), SH_TZ)
        if close <= current < next_open:
            return index
    return None


def _identity(payload: dict) -> str:
    return _digest({key: payload[key] for key in (
        "origin_day", "target_sessions", "calendar_window",
        "next_open_at", "market", "sector", "boards", "issuer_sources")})


def _record_identity(payload: dict) -> str:
    """Cover issuance metadata too; the digest remains self-reported, not signed."""
    return _digest({key: value for key, value in payload.items() if key != "record_sha256"})


def prepare(*, root=ROOT, now: datetime | None = None) -> dict[str, Any]:
    """Read only records visible in this after-close window; do not backfill."""
    root = Path(root)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("origin_now_timezone_required")
    current = current.astimezone(SH_TZ)
    base = {"schema": SCHEMA, "status": "waiting_calendar", "origin_day": None,
            "issued_at": None, "board_count": 0, **BOUNDARY}
    try:
        sessions, calendar_sha = _calendar(root, current)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return {**base, "summary": "官方交易日历及来源收据未核；不猜源日。"}
    index = _window(sessions, current)
    if index is None:
        return {**base, "status": "outside_after_close_window",
                "summary": "当前不在已完成交易日 16:00 至下一交易日开盘前；不补做旧起点。"}
    day = sessions[index]
    base["origin_day"] = day
    if index + max(HORIZONS) >= len(sessions):
        return {**base, "status": "future_calendar_missing",
                "summary": "官方日历不足以确定未来恰好 1/3/5 个交易日。"}
    slot = _slot(root, sessions, index, current, _read_close)
    if slot["status"] != "verified":
        return {**base, "status": "sector_" + slot["status"],
                "summary": "盘后板块完整原件尚未通过当时可得时点与来源核验。"}
    try:
        pit = read_market_pit(root, day, now=current)
    except (OSError, ValueError, KeyError, TypeError):
        return {**base, "status": "market_pit_missing_or_invalid",
                "summary": "同日首次市场情绪档未核；不使用事后重算状态。"}
    market = pit["market_row"]
    axes = pit["emotion_axes"]
    next_open = _stamp(slot["next_open_at"])
    if (market["state"] not in STATES or not isinstance(axes, dict)
            or set(axes) != AXIS_FIELDS or axes["raw_state"] not in STATES
            or any(type(axes[key]) not in (int, float) or not math.isfinite(axes[key])
                   or not 0 <= axes[key] <= 100 for key in AXES)
            or market["trade_date"] != day or market["scope_id"] != SCOPE_ID
            or _stamp(market["state_available_at"]) > current
            or _stamp(market["state_available_at"]) >= next_open):
        return {**base, "status": "market_state_unavailable",
                "summary": "市场状态未知、超时或四轴不完整；不发行条件起点。"}
    try:
        packet, _ = _read_close(root, day, current)
        if (packet["packet_id"] != slot["packet_id"]
                or packet["observed_at"] != slot["observed_at"]
                or packet["snapshot"]["source"] != slot["source"]
                or _digest(packet["source_contract"]) != slot["source_contract_id"]):
            raise ValueError("sector_source_changed_between_reads")
        raw, _ = _sector_rows(packet["snapshot"])
        mapped = {(row["kind"], row["code"]): row for row in raw}
        if len(mapped) != len(raw):
            raise ValueError("sector_duplicate_identity")
        boards = []
        for key, name in sorted(slot["identities"].items()):
            item = mapped.get(key)
            if item is None or item["name"] != name:
                raise ValueError("sector_identity_changed_between_reads")
            row_time = _stamp(item["data_as_of"])
            if (row_time.date().isoformat() != day or row_time.time() < time(15)
                    or row_time > _stamp(slot["observed_at"])):
                raise ValueError("sector_row_time_invalid")
            status = ("unassessable" if key not in slot["evaluable"] else
                      "qualified" if key in slot["qualified"] else "not_qualified")
            boards.append({"kind": key[0], "code": key[1], "name": name,
                           "screen_status": status, "change_pct": item["change_pct"],
                           "advance_coverage_pct": item["advance_coverage_pct"],
                           "constituent_count": item["constituent_count"],
                           "data_as_of": item["data_as_of"]})
        if len(boards) > MAX_BOARD_ROWS:
            raise ValueError("sector_population_limit_exceeded")
    except (OSError, ValueError, KeyError, TypeError):
        return {**base, "status": "sector_identity_or_size_invalid",
                "summary": "二次读取的板块身份、完整母体或容量不一致；不发行起点。"}
    target_sessions = {str(h): sessions[index + h] for h in HORIZONS}
    payload = {"schema": SCHEMA, "status": "origin_recorded",
               "origin_day": day, "issued_at": current.isoformat(),
               "next_open_at": slot["next_open_at"],
               "calendar_window": sessions[index:index + max(HORIZONS) + 1],
               "calendar_sha256": calendar_sha, "target_sessions": target_sessions,
               "market": {"state": market["state"], "axes": axes,
                          "scope_id": market["scope_id"],
                          "captured_at": market["state_available_at"],
                          "source_snapshot_sha256": market["source_snapshot_sha256"],
                          "universe_snapshot_sha256": market["universe_snapshot_sha256"],
                          "taxonomy_version": market["taxonomy_version"],
                          "feature_schema_version": market["feature_schema_version"]},
               "sector": {"packet_id": slot["packet_id"], "source": slot["source"],
                          "source_contract_id": slot["source_contract_id"],
                          "source_as_of": slot["source_as_of"],
                          "observed_at": slot["observed_at"],
                          "population": slot["population"]},
               "boards": boards, "board_count": len(boards),
               "issuer_sources": _source_code_identity(),
               "calendar_alignment": "same_date_separate_market_and_board_universes",
               "chronology": "self_reported_cloud_issue_time_not_external_attestation",
               "target_meaning": "future_mainline_target_not_yet_frozen",
               "summary": "盘后全板块研究起点已形成，尚无未来主流标签、预测概率或交易资格。",
               **BOUNDARY}
    payload["input_sha256"] = _identity(payload)
    payload["record_sha256"] = _record_identity(payload)
    raw_bytes = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                           allow_nan=False).encode("utf-8")
    if len(raw_bytes) > MAX_RECORD_BYTES:
        return {**base, "status": "record_too_large",
                "summary": "完整分母起点超过文件上限；不截断失败板块。"}
    return payload


def _path(root: Path, day: str) -> Path:
    return root / "reports/sector_mainline_origin" / (day + ".json")


def read_day(root: Path, day: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Check a saved origin's internal identity; this is not external attestation."""
    root, day = Path(root), _day(day)
    path = _path(root, day)
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise ValueError("origin_record_missing_or_unsafe")
    with path.open("rb") as stream:
        raw = stream.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError("origin_record_too_large")
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("origin_record_json_invalid") from error
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("origin_read_timezone_required")
    current = current.astimezone(SH_TZ)
    if (not isinstance(payload, dict) or set(payload) != RECORD_FIELDS
            or payload.get("schema") != SCHEMA
            or payload.get("status") != "origin_recorded"
            or payload.get("origin_day") != day
            or any(payload.get(key) != value for key, value in BOUNDARY.items())
            or payload.get("calendar_alignment") != "same_date_separate_market_and_board_universes"
            or payload.get("target_meaning") != "future_mainline_target_not_yet_frozen"
            or not _hex(payload.get("calendar_sha256"))
            or not _hex(payload.get("input_sha256"))
            or not _hex(payload.get("record_sha256"))
            or _record_identity(payload) != payload["record_sha256"]):
        raise ValueError("origin_record_contract_invalid")
    sessions = payload.get("calendar_window")
    target = payload.get("target_sessions")
    if (not isinstance(sessions, list) or len(sessions) != 6
            or sessions != sorted(set(sessions)) or sessions[0] != day
            or not isinstance(target, dict) or set(target) != {str(h) for h in HORIZONS}
            or any(_day(session) != session for session in sessions)
            or any(target[str(h)] != sessions[h] for h in HORIZONS)):
        raise ValueError("origin_calendar_window_invalid")
    issued = _stamp(payload.get("issued_at"))
    next_open = _stamp(payload.get("next_open_at"))
    first_close = datetime.combine(date.fromisoformat(day), time(16), SH_TZ)
    if (issued < first_close
            or next_open.date().isoformat() != sessions[1]
            or next_open.time() != time(9, 30)
            or not issued < next_open or issued > current):
        raise ValueError("origin_issue_clock_invalid")
    market, sector = payload.get("market"), payload.get("sector")
    if (not isinstance(market, dict) or set(market) != {
                "state", "axes", "scope_id", "captured_at", "source_snapshot_sha256",
                "universe_snapshot_sha256", "taxonomy_version", "feature_schema_version"}
            or market.get("state") not in STATES
            or market.get("scope_id") != SCOPE_ID
            or not isinstance(market.get("axes"), dict)
            or set(market["axes"]) != AXIS_FIELDS
            or market["axes"]["raw_state"] not in STATES
            or any(type(market["axes"][key]) not in (int, float)
                   or not math.isfinite(market["axes"][key])
                   or not 0 <= market["axes"][key] <= 100 for key in AXES)
            or any(not _hex(market.get(key)) for key in (
                "source_snapshot_sha256", "universe_snapshot_sha256",
                "taxonomy_version", "feature_schema_version"))
            or not isinstance(sector, dict) or set(sector) != {
                "packet_id", "source", "source_contract_id", "source_as_of",
                "observed_at", "population"} or not sector.get("packet_id")
            or not sector.get("source") or not _hex(sector.get("source_contract_id"))
            or _stamp(market.get("captured_at")).date().isoformat() != day
            or _stamp(market["captured_at"]).time() < time(15)
            or not _stamp(market["captured_at"]) <= issued
            or _stamp(sector.get("source_as_of")).date().isoformat() != day
            or _stamp(sector["source_as_of"]).time() < time(15)
            or not _stamp(sector["source_as_of"]) <= _stamp(sector.get("observed_at")) <= issued):
        raise ValueError("origin_source_identity_invalid")
    population = sector.get("population")
    boards = payload.get("boards")
    if (not isinstance(population, dict)
            or population.get("scope") != "this_verified_supplier_packet_only"
            or set(population) != {"scope", "by_kind", "excluded_behavior_cohorts"}
            or type(population.get("excluded_behavior_cohorts")) is not int
            or population["excluded_behavior_cohorts"] < 0
            or not isinstance(population.get("by_kind"), dict)
            or set(population["by_kind"]) != {"industry", "concept"}
            or not isinstance(boards, list) or len(boards) > MAX_BOARD_ROWS
            or type(payload.get("board_count")) is not int
            or payload["board_count"] != len(boards)):
        raise ValueError("origin_population_invalid")
    counts = {kind: {"reported": 0, "evaluable": 0, "qualified": 0}
              for kind in ("industry", "concept")}
    seen = set()
    for row in boards:
        if not isinstance(row, dict) or set(row) != BOARD_FIELDS:
            raise ValueError("origin_board_fields_invalid")
        key = (row.get("kind"), row.get("code"))
        if (key in seen or key[0] not in counts or not isinstance(key[1], str)
                or not key[1] or not isinstance(row.get("name"), str) or not row["name"]
                or row.get("screen_status") not in {"qualified", "not_qualified", "unassessable"}
                or type(row.get("change_pct")) not in (int, float)
                or not math.isfinite(row["change_pct"])
                or type(row.get("constituent_count")) is not int
                or row["constituent_count"] < 0
                or (row.get("advance_coverage_pct") is not None
                    and (type(row["advance_coverage_pct"]) not in (int, float)
                         or not math.isfinite(row["advance_coverage_pct"])
                         or not 0 <= row["advance_coverage_pct"] <= 100))
                or _stamp(row.get("data_as_of")).date().isoformat() != day
                or _stamp(row["data_as_of"]).time() < time(15)
                or _stamp(row["data_as_of"]) > _stamp(sector["observed_at"])):
            raise ValueError("origin_board_identity_invalid")
        seen.add(key)
        bucket = counts[key[0]]
        bucket["reported"] += 1
        bucket["evaluable"] += row["screen_status"] != "unassessable"
        bucket["qualified"] += row["screen_status"] == "qualified"
    for kind, expected in counts.items():
        supplied = population["by_kind"].get(kind)
        if (not isinstance(supplied, dict)
                or any(type(supplied.get(field)) is not int for field in (
                    "reported", "evaluable", "qualified", "displayed",
                    "unassessable", "not_qualified"))
                or any(expected[field] != supplied[field] for field in expected)
                or supplied["unassessable"] != supplied["reported"] - supplied["evaluable"]
                or supplied["not_qualified"] != supplied["evaluable"] - supplied["qualified"]
                or not 0 <= supplied["displayed"] <= supplied["qualified"]):
            raise ValueError("origin_population_counts_invalid")
    sources = payload.get("issuer_sources")
    if (not isinstance(sources, dict) or set(sources) != set(_source_code_identity())
            or any(not _hex(value) for value in sources.values())
            or _identity(payload) != payload["input_sha256"]):
        raise ValueError("origin_input_identity_invalid")
    return payload


def capture(*, root=ROOT, now: datetime | None = None) -> dict[str, Any]:
    """Cloud timer only. Preserve the first issue even if sources revise later."""
    if os.environ.get("REGIS_EXECUTION_ROLE") != "cloud":
        return {"status": "local_unchanged", **BOUNDARY}
    root = Path(root)
    prepared = prepare(root=root, now=now)
    if prepared["status"] != "origin_recorded":
        return {key: prepared[key] for key in ("status", "origin_day", "summary")}
    current = now or datetime.now(timezone.utc)
    day = prepared["origin_day"]
    path = _path(root, day)
    if path.parent.is_symlink():
        raise ValueError("origin_directory_unsafe")
    path.parent.mkdir(parents=True, exist_ok=True)
    def existing():
        try:
            old = read_day(root, day, now=current)
        except (OSError, ValueError, KeyError, TypeError):
            return {"status": "record_invalid_preserved", "origin_day": day, **BOUNDARY}
        return {"status": "unchanged" if old["input_sha256"] == prepared["input_sha256"]
                else "source_revision_conflict", "origin_day": day, **BOUNDARY}
    if path.exists() or path.is_symlink():
        return existing()
    raw = json.dumps(prepared, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".origin-", suffix=".tmp",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return existing()
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    read_day(root, day, now=current)
    return {"status": "captured", "origin_day": day,
            "input_sha256": prepared["input_sha256"],
            "file_sha256": hashlib.sha256(raw).hexdigest(), **BOUNDARY}


def current_status(*, root=ROOT, now: datetime | None = None) -> dict[str, Any]:
    """Small read-only website status, never the full board population."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("origin_status_timezone_required")
    current = current.astimezone(SH_TZ)
    base = {"schema": "sector_mainline_origin_status.v1",
            "status": "waiting_calendar", "origin_day": None,
            "issued_at": None, "board_count": 0,
            "summary": "官方交易日历尚未核验。", **BOUNDARY}
    try:
        sessions, calendar_sha = _calendar(Path(root), current)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return base
    index = _window(sessions, current)
    if index is None:
        return {**base, "status": "outside_after_close_window",
                "summary": "当前不在盘后研究起点发行窗口。"}
    day = sessions[index]
    base["origin_day"] = day
    path = _path(Path(root), day)
    if not path.exists() and not path.is_symlink():
        return {**base, "status": "origin_not_saved",
                "summary": "当日尚无首写档；云端定时器负责检查来源，网页不重算发行输入。"}
    try:
        saved = read_day(root, day, now=current)
    except (OSError, ValueError, KeyError, TypeError):
        return {**base, "status": "origin_not_saved_or_invalid",
                "summary": "当日研究起点尚无可核首写档。"}
    # The calendar file's raw SHA also changes on a harmless receipt refresh.
    # Compare the audited trading-day window; keep the original SHA in the record.
    if saved["calendar_window"] != sessions[index:index + max(HORIZONS) + 1]:
        return {**base, "status": "calendar_revision_conflict",
                "summary": "官方交易日历与首写档的未来交易日不一致；保留原档，暂停使用该起点。"}
    receipt_refreshed = saved["calendar_sha256"] != calendar_sha
    return {**base, "status": "captured", "issued_at": saved["issued_at"],
            "board_count": saved["board_count"],
            "input_sha256": saved["input_sha256"],
            "calendar_receipt_refreshed": receipt_refreshed,
            "market_state": saved["market"]["state"],
            "market_source_sha256": saved["market"]["source_snapshot_sha256"],
            "source_board_packet_id": saved["sector"]["packet_id"],
            "summary": ("当前日历收据已更新，交易日窗口未变；保留原始首写档。"
                        if receipt_refreshed else "已保存当时完整板块研究输入。")
            + "未发行未来主流概率或交易提醒。"}


__all__ = ["prepare", "capture", "read_day", "current_status", "SCHEMA"]
