"""Read-only schedule health for the immutable regime-route input captures."""

from __future__ import annotations

import json
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .forward_capture_integrity import verify_capture_artifact
from .intraday_market_emotion_forward import build_intraday_emotion_observation
from .leader_path_forward import verify_leader_path_packet
from .market_news_forward import verify_market_news_packet
from .sector_quote_panel_forward import (
    JOURNAL_DIR as SECTOR_QUOTE_PANEL_JOURNAL_DIR,
    verify_sector_quote_panel_journal,
)
from .strict_joint_forward_adapter import verify_strict_joint_forward_audit
from .system_route_joint_v2_forward import (
    verify_system_route_joint_v2_forward_packet,
)
from .verified_portfolio_execution_forward import (
    execution_capture_schedule_status,
    verify_forward_execution_packet,
)


SH_TZ = ZoneInfo("Asia/Shanghai")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = PROJECT_ROOT / "research/INTRADAY_MARKET_EMOTION_FORWARD_INPUT_PROTOCOL_V1.json"
_PACKET_DIRS = {
    "market_state": PROJECT_ROOT / "research/data/market_state_forward/v1/decision_packets",
    "attention_crowding": PROJECT_ROOT / "research/data/h3_execution_forward/v1/observation_packets",
    "intraday_emotion": PROJECT_ROOT / "research/data/intraday_market_emotion_forward/v1/input_packets",
    "market_sector": PROJECT_ROOT / "research/data/market_sector_route_forward/v1/input_packets",
    "route_decision": PROJECT_ROOT / "research/data/market_sector_route_forward/v1/route_decisions",
    "sector_constituents": PROJECT_ROOT / "research/data/market_sector_route_forward/v1/constituent_packets",
}
_CORE_ROUTE_ARTIFACTS = (
    "market_state", "attention_crowding", "intraday_emotion",
    "market_sector", "route_decision",
)
_NEWS_PACKET_DIR = PROJECT_ROOT / "research/data/market_news_forward/v1/input_packets"
_LEADER_PACKET_DIR = PROJECT_ROOT / "research/data/leader_path_forward/v1/input_packets"
_EXECUTION_DIR = PROJECT_ROOT / "research/data/verified_portfolio_execution/v1"
_AUDIT_DIR = PROJECT_ROOT / "research/data/strict_joint_strategy_replay/v1/audit_results"
_STRICT_FORWARD_V2_DIR = (
    PROJECT_ROOT
    / "research/data/system_route_joint_replay/v2/strict_forward_decisions"
)
_CALENDAR_PATH = PROJECT_ROOT / "reports/current_market_calendar.json"

# No runtime caller exists yet. Keep this separate from evidence presence so a
# manually-created packet cannot make the automatic system look complete.
LEADER_PATH_RUNTIME_SCHEDULED = False


def _under_root(project_root: Path, path: Path) -> Path:
    return project_root / path.relative_to(PROJECT_ROOT)


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _news_evidence(path: Path, *, project_root: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "present": False, "valid": False, "complete": False}
    try:
        checked = verify_market_news_packet(path, project_root=project_root)
    except Exception as exc:
        return {
            "status": "invalid", "present": True, "valid": False, "complete": False,
            "errors": [f"{type(exc).__name__}:{exc}"],
        }
    valid = checked.get("valid") is True
    source_complete = checked.get("source_complete") is True
    return {
        "status": "complete" if valid and source_complete else "incomplete" if valid else "invalid",
        "present": True, "valid": valid, "complete": bool(valid and source_complete),
        "source_complete": source_complete,
        "errors": checked.get("errors") or checked.get("source_errors") or [],
        "path": str(path),
    }


def _leader_evidence(paths: list[Path], *, project_root: Path) -> dict[str, Any]:
    if not paths:
        return {
            "status": "not_scheduled" if not LEADER_PATH_RUNTIME_SCHEDULED else "missing",
            "runtime_schedule_status": (
                "scheduled" if LEADER_PATH_RUNTIME_SCHEDULED else "not_scheduled"
            ),
            "present": False, "valid": False, "complete": False, "paths": [],
        }
    checks = []
    for path in paths:
        try:
            checked = verify_leader_path_packet(path, project_root=project_root)
        except Exception as exc:
            checked = {"valid": False, "errors": [f"{type(exc).__name__}:{exc}"]}
        checks.append({
            "path": str(path), "valid": checked.get("valid") is True,
            "errors": checked.get("errors") or [],
        })
    evidence_valid = bool(checks) and all(row["valid"] for row in checks)
    complete = bool(evidence_valid and LEADER_PATH_RUNTIME_SCHEDULED)
    return {
        "status": "complete" if complete else "valid_unscheduled" if evidence_valid else "invalid",
        "runtime_schedule_status": (
            "scheduled" if LEADER_PATH_RUNTIME_SCHEDULED else "not_scheduled"
        ),
        "present": True, "valid": evidence_valid, "complete": complete,
        "paths": checks,
    }


def _supplemental_evidence(
    *, decision_day: str, slots: list[dict[str, Any]], project_root: Path,
) -> dict[str, Any]:
    news_dir = _under_root(project_root, _NEWS_PACKET_DIR)
    leader_dir = _under_root(project_root, _LEADER_PACKET_DIR)
    rows = []
    for slot in slots:
        slot_id = str(slot["id"])
        news = _news_evidence(
            news_dir / f"{decision_day}-{slot_id}.json", project_root=project_root,
        )
        leader = _leader_evidence(
            sorted(leader_dir.glob(f"{decision_day}-{slot_id}-*.json")),
            project_root=project_root,
        )
        rows.append({"slot_id": slot_id, "news": news, "leader": leader})
    complete = bool(rows) and all(
        row["news"]["complete"] and row["leader"]["complete"] for row in rows
    )
    return {
        "status": "complete" if complete else "incomplete",
        "decision_day": decision_day,
        "complete": complete,
        "required_for_core_slot_status": False,
        "slots": rows,
    }


def _sector_quote_panel_evidence(
    *, decision_day: str, slot_id: str, project_root: Path,
) -> dict[str, Any]:
    """Verify the optional intraday quote journal without granting authority."""
    directory = (
        _under_root(project_root, SECTOR_QUOTE_PANEL_JOURNAL_DIR)
        / decision_day / slot_id
    )
    if not directory.is_dir():
        return {
            "status": "missing", "present": False, "valid": False,
            "complete": False, "phase": "not_started",
            "path_complete": False, "errors": ["journal_missing"],
            "required_for_current_trade_route": False,
            "trade_action_authority": False,
        }
    try:
        checked = verify_sector_quote_panel_journal(
            directory, project_root=project_root,
        )
    except Exception as exc:
        checked = {
            "valid": False, "complete": False,
            "errors": [f"journal_verifier:{type(exc).__name__}"],
        }
    valid = checked.get("valid") is True
    complete = bool(valid and checked.get("complete") is True)
    return {
        "status": "complete" if complete else "incomplete" if valid else "invalid",
        "present": True, "valid": valid, "complete": complete,
        "phase": checked.get("phase") or (
            "closed_complete" if complete else "invalid"
        ),
        "path_complete": complete,
        "panel_snapshot_count": int(checked.get("panel_snapshot_count") or 0),
        "latest_coverage_pct": checked.get("latest_coverage_pct"),
        "incomplete_reason": checked.get("incomplete_reason"),
        "errors": list(checked.get("errors") or []),
        "journal_path": str(directory),
        "required_for_current_trade_route": False,
        "trade_action_authority": False,
    }


def _strict_forward_v2_evidence(
    path: Path, *, project_root: Path,
) -> dict[str, Any]:
    if not path.is_file():
        return {
            "status": "missing", "present": False, "valid": False,
            "complete": False, "path": str(path),
        }
    try:
        checked = verify_system_route_joint_v2_forward_packet(
            path, project_root=project_root,
        )
    except Exception as exc:
        checked = {"valid": False, "errors": [f"{type(exc).__name__}:{exc}"]}
    valid = checked.get("valid") is True
    return {
        "status": "complete" if valid else "invalid",
        "present": True, "valid": valid, "complete": valid,
        "path": str(path), "packet_id": checked.get("packet_id"),
        "errors": checked.get("errors") or [],
    }


def _core_day_complete(
    decision_day: str, slots: list[dict[str, Any]], *, project_root: Path,
    artifact_verifier,
) -> bool:
    for slot in slots:
        slot_id = str(slot["id"])
        for name, directory in _PACKET_DIRS.items():
            path = _under_root(project_root, directory) / f"{decision_day}-{slot_id}.json"
            if not path.is_file():
                return False
            try:
                checked = artifact_verifier(
                    name, path, decision_day=decision_day, slot_id=slot_id,
                    project_root=project_root,
                )
            except Exception:
                return False
            if checked.get("valid") is not True:
                return False
        strict_path = (
            _under_root(project_root, _STRICT_FORWARD_V2_DIR)
            / f"{decision_day}-{slot_id}.json"
        )
        if _strict_forward_v2_evidence(
            strict_path, project_root=project_root,
        ).get("valid") is not True:
            return False
    return bool(slots)


def _next_trading_day(decision_day: str, *, project_root: Path) -> str | None:
    payload = _read_object(_under_root(project_root, _CALENDAR_PATH)) or {}
    dates = payload.get("trading_dates")
    if not isinstance(dates, list):
        return None
    try:
        index = dates.index(decision_day)
    except ValueError:
        return None
    return str(dates[index + 1]) if index + 1 < len(dates) else None


def _execution_records(*, project_root: Path) -> tuple[list[dict[str, Any]], int]:
    records, invalid = [], 0
    directory = _under_root(project_root, _EXECUTION_DIR)
    for path in sorted(directory.glob("????-??-??.json")):
        packet = _read_object(path)
        if packet is None:
            invalid += 1
            continue
        try:
            checked = verify_forward_execution_packet(packet, project_root=project_root)
        except Exception:
            checked = {"valid": False}
        if checked.get("valid") is True:
            records.append({"path": path, "packet": checked.get("packet") or packet})
        else:
            invalid += 1
    return records, invalid


def _audit_records(*, project_root: Path) -> tuple[list[dict[str, Any]], int]:
    records, invalid = [], 0
    directory = _under_root(project_root, _AUDIT_DIR)
    for path in sorted(directory.glob("*.json")):
        try:
            checked = verify_strict_joint_forward_audit(path, project_root=project_root)
        except Exception:
            checked = {"valid": False}
        if checked.get("valid") is True:
            records.append({"path": path, "audit": checked.get("audit") or {}})
        else:
            invalid += 1
    return records, invalid


def _downstream_evidence(
    *, observed: datetime, source_decision_day: str | None, project_root: Path,
) -> dict[str, Any]:
    # The only implemented next-session executor consumes the permanently
    # retired legacy joint-replay packets.  Keep its old evidence inspectable,
    # but never advertise or schedule it as the downstream of strict V2.
    schedule = {
        "phase": "disabled_retired_strategy_source",
        "enabled": False,
        "reason": "等待独立入场策略准入并绑定 strict V2 后再启用纸面执行",
        "research_only": True,
    }
    executions, invalid_executions = _execution_records(project_root=project_root)
    audits, invalid_audits = _audit_records(project_root=project_root)
    matching = [
        row for row in executions
        if str(row["packet"].get("previous_trade_date") or "") == source_decision_day
    ] if source_decision_day else []
    execution_paths = {
        str(row["path"].resolve().relative_to(project_root.resolve())) for row in matching
    }
    matching_audits = [
        row for row in audits
        if str((row["audit"].get("source_execution") or {}).get("path") or "")
        in execution_paths
    ]
    if source_decision_day is None:
        status = "no_completed_decision_day"
    else:
        status = "paused_until_independent_strategy_admission"
    return {
        "status": status,
        "source_decision_day": source_decision_day,
        "expected_execution_trade_date": None,
        "execution_schedule": schedule,
        "verified_execution_count": len(executions),
        "matching_execution_count": len(matching),
        "invalid_execution_count": invalid_executions,
        "strict_audit_count": len(audits),
        "matching_strict_audit_count": len(matching_audits),
        "invalid_strict_audit_count": invalid_audits,
        "legacy_execution_archives_are_current_strategy_evidence": False,
        "next_session_absence_is_current_slot_miss": False,
    }


def _add_system_layers(
    result: dict[str, Any], *, observed: datetime, slots: list[dict[str, Any]],
    project_root: Path, artifact_verifier,
) -> dict[str, Any]:
    try:
        execution_schedule = execution_capture_schedule_status(
            now=observed, project_root=project_root,
        )
    except Exception:
        execution_schedule = {"phase": "identity_invalid"}
    source_day = None
    decision_day = str(result.get("decision_day") or "")
    if (
        result.get("status") == "schedule_complete"
        and decision_day
        and _core_day_complete(
            decision_day, slots, project_root=project_root,
            artifact_verifier=artifact_verifier,
        )
    ):
        source_day = decision_day
    else:
        previous = str(execution_schedule.get("previous_trade_date") or "")
        if previous and _core_day_complete(
            previous, slots, project_root=project_root,
            artifact_verifier=artifact_verifier,
        ):
            source_day = previous
    evidence_day = source_day or str(result.get("decision_day") or observed.date().isoformat())
    supplemental = _supplemental_evidence(
        decision_day=evidence_day, slots=slots, project_root=project_root,
    )
    downstream = _downstream_evidence(
        observed=observed, source_decision_day=source_day, project_root=project_root,
    )
    if source_day is None:
        overall = "core_pending"
    elif not supplemental["complete"]:
        overall = "core_complete_supplemental_incomplete"
    else:
        overall = downstream["status"]
    return {
        **result,
        "overall_system_status": overall,
        "supplemental_evidence": supplemental,
        "downstream": downstream,
    }


def latest_verified_core_route_review(
    schedule_status: dict[str, Any], *, observed_at: datetime | None = None,
    project_root: Path = PROJECT_ROOT,
    artifact_verifier=verify_capture_artifact,
) -> dict[str, Any]:
    """Return the newest verified same-slot market/sector route for display.

    The six core capture artifacts are separate from the strict account and
    candidate packet.  Missing account facts must block quantities and alerts,
    but must not hide an already verified market/sector observation.
    """
    observed = (observed_at or datetime.now(SH_TZ)).astimezone(SH_TZ)
    decision_day = str(schedule_status.get("decision_day") or "")
    complete_slots = [
        row for row in (schedule_status.get("slots") or [])
        if row.get("phase") in {"window_open", "captured", "missed"}
        and isinstance(row.get("artifact_integrity"), dict)
        and row["artifact_integrity"]
        and all(
            (row["artifact_integrity"].get(name) or {}).get("valid") is True
            for name in _CORE_ROUTE_ARTIFACTS
        )
    ]
    if not decision_day or not complete_slots:
        return {
            "status": "no_verified_core_route", "valid": False,
            "trade_action_authority": False,
        }
    slot = complete_slots[-1]
    slot_id = str(slot.get("slot_id") or "")
    path = (
        _under_root(project_root, _PACKET_DIRS["route_decision"])
        / f"{decision_day}-{slot_id}.json"
    )
    try:
        checked = artifact_verifier(
            "route_decision", path, decision_day=decision_day,
            slot_id=slot_id, project_root=project_root,
        )
        packet = _read_object(path) or {}
        decision_at = datetime.fromisoformat(str(packet.get("decision_at") or ""))
        if decision_at.tzinfo is None:
            decision_at = decision_at.replace(tzinfo=SH_TZ)
        decision_at = decision_at.astimezone(SH_TZ)
    except (OSError, TypeError, ValueError):
        return {
            "status": "core_route_invalid", "valid": False,
            "trade_action_authority": False,
        }
    if checked.get("valid") is not True or decision_at > observed:
        return {
            "status": "core_route_invalid", "valid": False,
            "errors": checked.get("errors") or ["decision_time_in_future"],
            "trade_action_authority": False,
        }
    route = packet.get("route")
    route = route if isinstance(route, dict) else {}
    emotion_packet = _read_object(
        _under_root(project_root, _PACKET_DIRS["intraday_emotion"])
        / f"{decision_day}-{slot_id}.json"
    ) or {}
    try:
        emotion_captured_at = datetime.fromisoformat(
            str(emotion_packet.get("captured_at") or "")
        )
    except ValueError:
        emotion_captured_at = None
    intraday_emotion = (
        build_intraday_emotion_observation(
            route,
            {"breadth": emotion_packet.get("raw_observation") or {}},
            now=emotion_captured_at,
        )
        if emotion_captured_at is not None
        else None
    )
    sector = route.get("sector_confirmation")
    sector = sector if isinstance(sector, dict) else {}
    budget = route.get("research_budget_hypothesis")
    budget = budget if isinstance(budget, dict) else {}
    strict_ready = (slot.get("strict_forward_v2") or {}).get("valid") is True
    constituent_integrity = (
        slot.get("artifact_integrity") or {}
    ).get("sector_constituents") or {}
    candidate_input_ready = constituent_integrity.get("valid") is True
    candidate_input_errors = list(constituent_integrity.get("errors") or [])
    return {
        "status": "verified_core_route_research_only",
        "valid": True,
        "decision_day": decision_day,
        "slot_id": slot_id,
        "decision_at": packet.get("decision_at"),
        "packet_id": packet.get("packet_id"),
        "market_state": route.get("market_state"),
        "market_state_as_of": route.get("market_state_as_of"),
        "market_confidence": route.get("market_confidence"),
        "market_confidence_semantics": route.get(
            "market_confidence_semantics"
        ),
        "evidence": list(route.get("evidence") or []),
        "contradictions": list(route.get("contradictions") or []),
        "intraday_emotion_observation": intraday_emotion,
        "confirmed_sectors": list(sector.get("confirmed_sectors") or []),
        "eligible_playbooks": list(budget.get("allowed_playbooks") or []),
        "research_target_exposure_range_pct": list(
            budget.get("target_exposure_range_pct") or []
        ),
        "research_maximum_new_trades": budget.get("maximum_new_trades"),
        "candidate_constituent_input_ready": candidate_input_ready,
        "candidate_constituent_input_errors": candidate_input_errors,
        "strict_route_ready": strict_ready,
        "account_or_candidate_route_ready": strict_ready,
        "trade_action_authority": False,
        "changes_position_or_quantity": False,
        "generates_buy_or_sell_alert": False,
        "reason": (
            "同槽市场与板块核心证据已核验；严格账户、候选和策略包"
            "已完成，但仍只作研究展示。"
            if strict_ready else
            "同槽市场与板块核心证据已核验；成分股候选输入"
            + ("已核验，" if candidate_input_ready else "未完整核验，")
            + "严格账户和策略包尚未完成，因此不生成股数、仓位调整或买卖提醒。"
        ),
    }


def forward_capture_schedule_status(
    *, now: datetime | None = None, is_trading_day: bool,
    project_root: Path = PROJECT_ROOT, protocol_path: Path = PROTOCOL_PATH,
    artifact_verifier=verify_capture_artifact,
) -> dict[str, Any]:
    """Distinguish upcoming windows from a capture window that was missed."""
    observed = (now or datetime.now(SH_TZ)).astimezone(SH_TZ)
    try:
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        forward_start = str(protocol["forward_start"])
        slots = list(protocol["schedule"]["slots"])
        if not slots:
            raise ValueError("schedule_slots_empty")
        previous_end = None
        seen_ids: set[str] = set()
        for slot in slots:
            slot_id = str(slot["id"])
            start = time.fromisoformat(str(slot["capture_from"]))
            end = time.fromisoformat(str(slot["capture_until"]))
            if not slot_id or slot_id in seen_ids:
                raise ValueError("schedule_slot_id_invalid_or_duplicate")
            if start >= end:
                raise ValueError("schedule_slot_window_not_increasing")
            if previous_end is not None and start < previous_end:
                raise ValueError("schedule_slots_overlap_or_out_of_order")
            seen_ids.add(slot_id)
            previous_end = end
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return _add_system_layers({
            "status": "schedule_invalid", "decision_day": observed.date().isoformat(),
            "missed_slots": [], "next_slot": None, "reason": str(exc),
            "strict_forward_v2_status": "schedule_invalid",
            "strict_forward_v2_phase": "blocked",
            "strict_forward_v2_missed_slots": [],
            "slots": [],
        }, observed=observed, slots=[], project_root=project_root,
            artifact_verifier=artifact_verifier)
    decision_day = observed.date().isoformat()
    if decision_day < forward_start:
        return _add_system_layers({
            "status": "waiting_forward_start", "decision_day": decision_day,
            "forward_start": forward_start, "missed_slots": [],
            "strict_forward_v2_status": "waiting_forward_start",
            "strict_forward_v2_phase": "upcoming",
            "strict_forward_v2_missed_slots": [],
            "next_slot": f"{forward_start}T{slots[0]['capture_from']}+08:00",
            "slots": [],
        }, observed=observed, slots=slots, project_root=project_root,
            artifact_verifier=artifact_verifier)
    if not is_trading_day:
        return _add_system_layers({
            "status": "not_trading_session", "decision_day": decision_day,
            "forward_start": forward_start, "missed_slots": [],
            "strict_forward_v2_status": "not_trading_session",
            "strict_forward_v2_phase": "not_applicable",
            "strict_forward_v2_missed_slots": [],
            "next_slot": None, "slots": [],
        }, observed=observed, slots=slots, project_root=project_root,
            artifact_verifier=artifact_verifier)

    current = observed.timetz().replace(tzinfo=None)
    slot_rows = []
    for slot in slots:
        slot_id = str(slot["id"])
        start = time.fromisoformat(str(slot["capture_from"]))
        end = time.fromisoformat(str(slot["capture_until"]))
        artifact_paths = {
            name: project_root / directory.relative_to(PROJECT_ROOT)
            / f"{decision_day}-{slot_id}.json"
            for name, directory in _PACKET_DIRS.items()
        }
        artifact_integrity = {
            name: (
                artifact_verifier(
                    name, path, decision_day=decision_day, slot_id=slot_id,
                    project_root=project_root,
                )
                if path.is_file()
                else {"valid": False, "errors": ["artifact_missing"]}
            )
            for name, path in artifact_paths.items()
        }
        present = {
            name: row.get("valid") is True
            for name, row in artifact_integrity.items()
        }
        strict_v2_path = (
            _under_root(project_root, _STRICT_FORWARD_V2_DIR)
            / f"{decision_day}-{slot_id}.json"
        )
        strict_v2 = _strict_forward_v2_evidence(
            strict_v2_path, project_root=project_root,
        )
        sector_quote_panel = _sector_quote_panel_evidence(
            decision_day=decision_day, slot_id=slot_id,
            project_root=project_root,
        )
        if current < start:
            phase = "upcoming"
        elif current <= end:
            phase = "window_open"
        else:
            phase = "captured" if all(present.values()) else "missed"
        slot_rows.append({
            "slot_id": slot_id, "capture_from": str(slot["capture_from"]),
            "capture_until": str(slot["capture_until"]), "phase": phase,
            "artifacts_present": present,
            "missing_artifacts": [name for name, exists in present.items() if not exists],
            "artifact_integrity": artifact_integrity,
            "strict_forward_v2": strict_v2,
            "strict_forward_v2_missing": strict_v2.get("valid") is not True,
            # Supplemental evidence for future playbook research.  It is not
            # part of core route completeness or today's trade authority.
            "sector_quote_panel": sector_quote_panel,
        })

    missed = [row["slot_id"] for row in slot_rows if row["phase"] == "missed"]
    upcoming = next((row for row in slot_rows if row["phase"] == "upcoming"), None)
    if missed:
        status = "missed_capture"
    elif any(row["phase"] == "window_open" for row in slot_rows):
        status = "capture_window_open"
    elif upcoming:
        status = "waiting_next_slot"
    else:
        status = "schedule_complete"
    strict_v2_missed = [
        row["slot_id"] for row in slot_rows
        if row["phase"] in {"missed", "captured"}
        and row["strict_forward_v2_missing"]
    ]
    strict_v2_invalid = [
        row["slot_id"] for row in slot_rows
        if row["strict_forward_v2"].get("present") is True
        and row["strict_forward_v2"].get("valid") is not True
    ]
    strict_v2_open_missing = any(
        row["phase"] == "window_open" and row["strict_forward_v2_missing"]
        for row in slot_rows
    )
    if strict_v2_invalid:
        strict_v2_status, strict_v2_phase = "identity_invalid", "blocked"
    elif strict_v2_missed:
        strict_v2_status, strict_v2_phase = "missed_capture", "missed"
    elif strict_v2_open_missing:
        strict_v2_status, strict_v2_phase = "capture_window_open", "window_open"
    elif status == "schedule_complete" and all(
        row["strict_forward_v2"].get("valid") is True for row in slot_rows
    ):
        strict_v2_status, strict_v2_phase = "schedule_complete", "complete"
    elif status == "capture_window_open":
        strict_v2_status, strict_v2_phase = "captured_current_slot", "captured"
    else:
        strict_v2_status, strict_v2_phase = "waiting_next_slot", "pending"
    return _add_system_layers({
        "status": status, "decision_day": decision_day,
        "forward_start": forward_start, "missed_slots": missed,
        "strict_forward_v2_status": strict_v2_status,
        "strict_forward_v2_phase": strict_v2_phase,
        "strict_forward_v2_missed_slots": strict_v2_missed,
        "strict_forward_v2_invalid_slots": strict_v2_invalid,
        "next_slot": (
            f"{decision_day}T{upcoming['capture_from']}+08:00" if upcoming else None
        ),
        "slots": slot_rows,
    }, observed=observed, slots=slots, project_root=project_root,
        artifact_verifier=artifact_verifier)


__all__ = [
    "forward_capture_schedule_status", "latest_verified_core_route_review",
]
