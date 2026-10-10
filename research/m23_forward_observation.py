"""Frozen M23 manual forward-observation recorder.

The recorder never fetches a quote.  It validates a manually preserved visible
source payload, creates one immutable event origin, or records an expired
missing event after the frozen window.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
import sys
import tempfile
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_IMPORT_ROOT))

from quantqc.costs import (
    BROKER_COMMISSION_ALL_IN_RATE,
    COSTS,
    LOT_SIZE,
    MIN_COMMISSION_YUAN,
)


PROTOCOL_PATH = "QUANT_RESEARCH_M23_PROTOCOL_20260930.json"
SEAL_PATH = "QUANT_RESEARCH_M23_PREOBSERVATION_SEAL_20260930.json"
SYNC_PATH = "QUANT_RESEARCH_M23_PREOBSERVATION_SYNC_20260930.json"
PREFLIGHT_PENDING_SUMMARY_PATH = "QUANT_RESEARCH_M23_PREFLIGHT_SUMMARY_20260930.json"
PREFLIGHT_FROZEN_SUMMARY_PATH = (
    "QUANT_RESEARCH_M23_FROZEN_PREFLIGHT_SUMMARY_20261001.json"
)
FORWARD_SUMMARY_PATH = "QUANT_RESEARCH_M23_FORWARD_PATH_SUMMARY_20261015.json"
EVENT_RECEIPT_PATHS = {
    "entry": "QUANT_RESEARCH_M23_EVENT_RECEIPT_ENTRY_20261008.json",
    "exit_1": "QUANT_RESEARCH_M23_EVENT_RECEIPT_EXIT1_20261009.json",
    "exit_3": "QUANT_RESEARCH_M23_EVENT_RECEIPT_EXIT3_20261013.json",
    "exit_5": "QUANT_RESEARCH_M23_EVENT_RECEIPT_EXIT5_20261015.json",
}
CONTRACT_SHA256 = "15cc7006c722b6dfc386b79c008610480b630486ec34fc415b253bf829e81fb1"
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
CENT = Decimal("0.01")
RETURN_PRECISION = Decimal("0.000001")


class ObservationError(ValueError):
    """A frozen observation invariant failed."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ObservationError(message)


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _safe_path(root: Path, relative: str) -> Path:
    _require(isinstance(relative, str) and bool(relative.strip()), "path_invalid")
    parts = relative.replace("\\", "/").split("/")
    _require(
        not PureWindowsPath(relative).drive
        and not relative.startswith(("/", "\\"))
        and all(
            part not in {"", ".", ".."}
            and part == part.rstrip(" .")
            and ":" not in part
            and "\x00" not in part
            for part in parts
        ),
        f"path_unsafe:{relative}",
    )
    base = root.resolve()
    result = base.joinpath(*parts).resolve()
    _require(result.is_relative_to(base), f"path_escape:{relative}")
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        _require(path.stat().st_size <= MAX_JSON_BYTES, f"json_too_large:{path.name}")
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ObservationError(f"json_unreadable:{path.name}:{exc}") from None
    _require(isinstance(value, dict), f"json_object_required:{path.name}")
    return value


def _digest(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise ObservationError(f"file_unreadable:{path.name}:{exc}") from None


def _canonical(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def write_once_json(path: Path, value: dict[str, Any]) -> str:
    raw = _canonical(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _require(path.read_bytes() == raw, "immutable_output_conflict")
        return "existing_identical"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".m23-", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            _require(path.read_bytes() == raw, "immutable_output_conflict")
            return "existing_identical"
        return "created"
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _aware(value: Any, label: str) -> datetime:
    _require(isinstance(value, str) and bool(value), f"{label}_invalid")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ObservationError(f"{label}_invalid") from None
    _require(result.utcoffset() is not None, f"{label}_timezone_missing")
    return result


def _positive(value: Any, label: str) -> float:
    _require(
        type(value) in {int, float}
        and math.isfinite(float(value))
        and float(value) > 0,
        f"{label}_invalid",
    )
    return float(value)


def _nonnegative_integer(value: Any, label: str) -> int:
    _require(type(value) is int and value >= 0, f"{label}_invalid")
    return value


def _event_map(protocol: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = protocol.get("events")
    _require(isinstance(rows, list) and len(rows) == 4, "events_invalid")
    _require(
        all(isinstance(row, dict) and isinstance(row.get("id"), str) for row in rows),
        "event_invalid",
    )
    result = {row["id"]: row for row in rows}
    _require(
        set(result) == {"entry", "exit_1", "exit_3", "exit_5"}
        and len(result) == len(rows),
        "event_identity_invalid",
    )
    return result


def validate_protocol(protocol: dict[str, Any]) -> dict[str, dict[str, Any]]:
    _require(
        protocol.get("schema") == "regis.m23.forward_observation_protocol.v1",
        "protocol_schema_invalid",
    )
    _require(protocol.get("task_id") == "QUANT-RESEARCH-20260930-023", "task_id_invalid")
    _require(
        protocol.get("research_only") is True
        and protocol.get("reads_live_account") is False
        and protocol.get("automatic_order_submission") is False
        and protocol.get("automatic_quote_scraping") is False,
        "authority_boundary_invalid",
    )
    capital = protocol.get("capital", {})
    _require(
        capital.get("yuan_per_arm") == 150000
        and capital.get("stock_arm") == "cash_due_to_m21_zero_candidate"
        and capital.get("etf_arm")
        == "single_entry_three_mutually_exclusive_exit_horizons"
        and capital.get("combine_arms_or_horizons") is False,
        "capital_boundary_invalid",
    )
    benchmark = protocol.get("benchmark", {})
    _require(
        benchmark.get("code") == "510300.SH"
        and benchmark.get("order_unit") == 100
        and benchmark.get("t_plus_one") is True
        and benchmark.get("entry_side") == "executable_ask"
        and benchmark.get("exit_side") == "executable_bid",
        "benchmark_boundary_invalid",
    )
    source = protocol.get("source_policy", {})
    _require(
        source.get("source_id") == "sse_public_web_manual"
        and source.get("status_before_live_attestation")
        == "unverified_until_market_session"
        and source.get("capture_mode") == "manual_browser_saved_observation_only"
        and source.get("documented_programmatic_api_authorized") is False
        and source.get("undocumented_endpoint_forbidden") is True
        and source.get("live_attestation_required_per_event") is True
        and source.get("credentials_forbidden") is True,
        "source_policy_invalid",
    )
    session_rule = protocol.get("market_session_rule", {})
    _require(
        session_rule.get("source")
        == "https://www.sse.com.cn/lawandrules/sselawsrules2025/fund/trading/c/c_20260424_10817739.shtml"
        and session_rule.get("effective_date") == "2026-07-06"
        and session_rule.get("fund_closing_call_auction_start") == "14:57:00"
        and session_rule.get("fund_closing_call_auction_end") == "15:00:00"
        and session_rule.get("exit_observation_phase")
        == "continuous_auction_before_14_57",
        "market_session_rule_invalid",
    )
    timing = protocol.get("timing_policy", {})
    _require(
        timing.get("maximum_exchange_to_observation_lag_seconds") == 30
        and timing.get("maximum_observation_to_local_receive_lag_seconds") == 30
        and timing.get("actual_local_receive_time_generated_by_program") is True
        and timing.get("no_backfill") is True
        and timing.get("late_event_status") == "expired_missing",
        "timing_policy_invalid",
    )
    receipt_policy = protocol.get("event_receipt_policy", {})
    _require(
        receipt_policy.get("tracked_sanitized_receipt_required") is True
        and receipt_policy.get("export_deadline_seconds_after_window") == 600
        and receipt_policy.get("private_remote_push_deadline_seconds_after_window")
        == 1200
        and receipt_policy.get("receipt_contains_price_or_account_data") is False
        and receipt_policy.get("git_add_requires_force_for_root_artifact") is True
        and receipt_policy.get("remote_commit_time_is_external_evidence_not_exchange_timestamp")
        is True,
        "event_receipt_policy_invalid",
    )
    binding = protocol.get("root_and_contract_binding", {})
    _require(
        binding.get("root_source") == "implementation_file_parent_repository"
        and binding.get("remote_url")
        == "https://github.com/zidongliu11-web/regis-oracle.git"
        and binding.get("branch") == "codex/emotion-depth-20260927"
        and re.fullmatch(r"[0-9a-f]{40}", str(binding.get("base_commit", "")))
        and binding.get("workflow_contract_required") is True
        and binding.get("pre_observation_seal_required") is True
        and binding.get("private_remote_freeze_required") is True,
        "root_contract_binding_invalid",
    )
    accounting = protocol.get("execution_and_accounting", {})
    fees = accounting.get("fee_assumptions", {})
    corporate_actions = accounting.get("corporate_action_source_policy", {})
    _require(
        accounting.get("forward_accounting_engine")
        == "research/m23_forward_observation.py"
        and accounting.get("reused_accounting_reference")
        == "research/target_a_confirmed_fill_replay.py"
        and accounting.get("cost_model") == "quantqc/costs.py"
        and fees.get("commission_rate") == BROKER_COMMISSION_ALL_IN_RATE
        and fees.get("minimum_commission_yuan") == MIN_COMMISSION_YUAN
        and fees.get("exchange_handling_rate_included_in_commission") == 0.00004
        and fees.get("commission_assumption_includes_exchange_handling") is True
        and fees.get("transfer_rate_for_stock_etf") == 0
        and fees.get("stamp_rate_for_stock_etf") == 0
        and fees.get("additional_slippage_rate_beyond_visible_quote")
        == COSTS["slippage"]
        and fees.get("market_impact_rate") == 0
        and fees.get("account_specific_fee_status")
        == "unverified_no_account_read"
        and fees.get("zero_transfer_and_stamp_status")
        == "research_assumption_current_chinaclear_body_not_independently_retrieved"
        and fees.get("model_admission_use")
        == "prohibited_until_actual_costs_verified"
        and accounting.get("top_of_book_size_unit") == "shares"
        and accounting.get("partial_fill_supported") is True
        and accounting.get("corporate_action_total_return_requires_complete_coverage")
        is True
        and accounting.get("unknown_corporate_action_rejects_total_return") is True
        and corporate_actions.get("source_id") == "sse_fund_announcements_manual"
        and corporate_actions.get("capture_mode")
        == "manual_browser_saved_observation_only"
        and corporate_actions.get("allowed_url_prefix") == "https://www.sse.com.cn/"
        and corporate_actions.get("announcement_page")
        == "https://www.sse.com.cn/disclosure/fund/announcement/"
        and corporate_actions.get("required_security_code") == "510300"
        and corporate_actions.get("required_announcement_type") == "全部"
        and corporate_actions.get("required_page_scope") == "all_result_pages"
        and corporate_actions.get("clear_status") == "no_relevant_event_found",
        "accounting_policy_invalid",
    )

    events = _event_map(protocol)
    expected = {
        "entry": ("2026-10-08", 0, "hypothetical_buy", "executable_ask", 180),
        "exit_1": ("2026-10-09", 1, "hypothetical_sell", "executable_bid", 120),
        "exit_3": ("2026-10-13", 3, "hypothetical_sell", "executable_bid", 120),
        "exit_5": ("2026-10-15", 5, "hypothetical_sell", "executable_bid", 120),
    }
    try:
        shanghai = ZoneInfo("Asia/Shanghai")
        new_york = ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError as exc:
        raise ObservationError(f"timezone_database_missing:{exc}") from None
    for event_id, event in events.items():
        date, horizon, action, quote, duration_seconds = expected[event_id]
        _require(
            (
                event.get("trade_date"),
                event.get("horizon_sessions"),
                event.get("action"),
                event.get("required_quote"),
            )
            == (date, horizon, action, quote),
            f"event_rule_invalid:{event_id}",
        )
        _require(
            event.get("session_phase") == "continuous_auction",
            f"event_session_phase_invalid:{event_id}",
        )
        start = _aware(event.get("start_asia_shanghai"), f"{event_id}_start")
        end = _aware(event.get("end_asia_shanghai"), f"{event_id}_end")
        utc = _aware(event.get("start_utc"), f"{event_id}_utc")
        ny = _aware(event.get("start_america_new_york"), f"{event_id}_new_york")
        _require(start.astimezone(shanghai).isoformat() == event["start_asia_shanghai"], f"event_shanghai_invalid:{event_id}")
        _require(start.astimezone(timezone.utc) == utc, f"event_utc_invalid:{event_id}")
        _require(start.astimezone(new_york) == ny, f"event_new_york_invalid:{event_id}")
        _require(
            end > start
            and end.astimezone(shanghai).date().isoformat() == date
            and (end - start).total_seconds() == duration_seconds
            and (event_id == "entry" or end.astimezone(shanghai).time().isoformat() == "14:57:00"),
            f"event_window_invalid:{event_id}",
        )
    return events


def validate_seal(root: Path, seal: dict[str, Any]) -> None:
    _require(seal.get("schema") == "regis.m23.preobservation_seal.v1", "seal_schema_invalid")
    _require(seal.get("task_id") == "QUANT-RESEARCH-20260930-023", "seal_task_invalid")
    _require(seal.get("contract_sha256") == CONTRACT_SHA256, "seal_contract_invalid")
    required = {
        PROTOCOL_PATH,
        "research/m23_forward_observation.py",
        "tests/test_m23_forward_observation.py",
        "quantqc/costs.py",
        "research/target_a_confirmed_fill_replay.py",
    }
    bindings = seal.get("bindings")
    _require(isinstance(bindings, list), "seal_bindings_invalid")
    found: dict[str, str] = {}
    for row in bindings:
        _require(
            isinstance(row, dict)
            and isinstance(row.get("path"), str)
            and re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256", ""))),
            "seal_binding_invalid",
        )
        found[row["path"]] = row["sha256"]
    _require(set(found) == required, "seal_binding_set_invalid")
    for relative, expected in found.items():
        _require(_digest(_safe_path(root, relative)) == expected, f"seal_hash_mismatch:{relative}")


def _run_git(root: Path, *args: str) -> str:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            shell=False,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ObservationError(f"git_check_failed:{args[0]}:{exc}") from None
    return result.stdout.strip()


def _git_blob_digest(root: Path, revision: str, relative: str) -> str:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        result = subprocess.run(
            ["git", "show", f"{revision}:{relative}"],
            cwd=root,
            shell=False,
            check=True,
            capture_output=True,
            timeout=20,
            creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ObservationError(f"git_blob_check_failed:{relative}:{exc}") from None
    return hashlib.sha256(result.stdout).hexdigest()


def validate_sync(root: Path, protocol: dict[str, Any], seal: dict[str, Any], sync: dict[str, Any]) -> bool:
    if sync.get("status") == "pending":
        return False
    _require(sync.get("schema") == "regis.m23.preobservation_sync.v1", "sync_schema_invalid")
    _require(sync.get("status") == "verified", "sync_status_invalid")
    freeze_commit = sync.get("freeze_commit")
    _require(
        isinstance(freeze_commit, str)
        and re.fullmatch(r"[0-9a-f]{40}", freeze_commit)
        and sync.get("local_head") == freeze_commit
        and sync.get("remote_head") == freeze_commit
        and sync.get("exact_match") is True,
        "sync_commit_invalid",
    )
    branch = _run_git(root, "branch", "--show-current")
    _require(branch == protocol["root_and_contract_binding"]["branch"], "git_branch_invalid")
    remote = _run_git(root, "remote", "get-url", "origin").rstrip("/")
    expected_remote = protocol["root_and_contract_binding"]["remote_url"].rstrip("/")
    _require(remote == expected_remote, "git_remote_invalid")
    _run_git(root, "merge-base", "--is-ancestor", freeze_commit, "HEAD")
    seal_map = {row["path"]: row["sha256"] for row in seal["bindings"]}
    for relative, expected in seal_map.items():
        _require(
            _git_blob_digest(root, freeze_commit, relative) == expected,
            f"freeze_blob_mismatch:{relative}",
        )
    return True


def build_preflight() -> dict[str, Any]:
    root = repository_root()
    protocol = _read_json(_safe_path(root, PROTOCOL_PATH))
    events = validate_protocol(protocol)
    seal = _read_json(_safe_path(root, SEAL_PATH))
    validate_seal(root, seal)
    sync = _read_json(_safe_path(root, SYNC_PATH))
    frozen = validate_sync(root, protocol, seal, sync)
    summary = {
        "schema": "regis.m23.preflight_summary.v1",
        "task_id": protocol["task_id"],
        "status": (
            "preobservation_frozen_waiting_for_live_window"
            if frozen
            else "preobservation_engineering_ready_remote_freeze_pending"
        ),
        "protocol_valid": True,
        "seal_valid": True,
        "private_remote_freeze_verified": frozen,
        "network_call_performed": False,
        "quote_capture_performed": False,
        "reads_live_account": False,
        "purchase_performed": False,
        "automatic_quote_scraping": False,
        "events": [
            {
                "id": event_id,
                "trade_date": event["trade_date"],
                "state": "future_unobserved",
            }
            for event_id, event in events.items()
        ],
        "claims": {
            "returns_computed": False,
            "model_admitted": False,
            "economic_target_A_validated": False,
        },
    }
    if frozen:
        summary["freeze_commit"] = sync["freeze_commit"]
        summary["contract_sha256"] = CONTRACT_SHA256
    return summary


ATTESTATION_FIELDS = {
    "schema",
    "event_id",
    "source_id",
    "rights_status",
    "source_url",
    "capture_mode",
    "verified_at",
    "exchange_timestamp_field",
    "best_bid_field",
    "best_ask_field",
    "best_bid_size_field",
    "best_ask_size_field",
    "source_payload_sha256",
    "source_payload_bytes",
    "credentials_present",
}
OBSERVATION_FIELDS = {
    "schema",
    "event_id",
    "code",
    "source_id",
    "source_url",
    "currency",
    "observed_at",
    "exchange_timestamp",
    "bid_price",
    "ask_price",
    "bid_size_shares",
    "ask_size_shares",
    "last_price",
    "source_payload_sha256",
    "source_payload_bytes",
}


def validate_live_inputs(
    protocol: dict[str, Any],
    event: dict[str, Any],
    attestation: dict[str, Any],
    observation: dict[str, Any],
    *,
    payload_sha256: str,
    payload_bytes: int,
    received_at: datetime,
) -> None:
    _require(set(attestation) == ATTESTATION_FIELDS, "attestation_fields_invalid")
    _require(set(observation) == OBSERVATION_FIELDS, "observation_fields_invalid")
    source_policy = protocol["source_policy"]
    benchmark = protocol["benchmark"]
    event_id = event["id"]
    _require(
        attestation.get("schema") == "regis.m23.live_source_attestation.v1"
        and attestation.get("event_id") == event_id
        and attestation.get("source_id") == source_policy["source_id"]
        and attestation.get("rights_status") == source_policy["rights_status_required"]
        and attestation.get("source_url") == benchmark["market_page"]
        and attestation.get("capture_mode") == source_policy["capture_mode"]
        and attestation.get("credentials_present") is False,
        "attestation_identity_invalid",
    )
    for field in (
        "exchange_timestamp_field",
        "best_bid_field",
        "best_ask_field",
        "best_bid_size_field",
        "best_ask_size_field",
    ):
        _require(isinstance(attestation.get(field), str) and bool(attestation[field].strip()), f"attestation_semantics_missing:{field}")
    _require(
        observation.get("schema") == "regis.m23.quote_observation.v1"
        and observation.get("event_id") == event_id
        and observation.get("code") == benchmark["code"]
        and observation.get("source_id") == source_policy["source_id"]
        and observation.get("source_url") == benchmark["market_page"]
        and observation.get("currency") == "CNY",
        "observation_identity_invalid",
    )
    _require(
        0 < payload_bytes <= MAX_PAYLOAD_BYTES
        and attestation.get("source_payload_bytes") == payload_bytes
        and observation.get("source_payload_bytes") == payload_bytes
        and attestation.get("source_payload_sha256") == payload_sha256
        and observation.get("source_payload_sha256") == payload_sha256,
        "source_payload_binding_invalid",
    )
    bid = _positive(observation.get("bid_price"), "bid_price")
    ask = _positive(observation.get("ask_price"), "ask_price")
    _require(
        Decimal(str(bid)) % Decimal("0.001") == 0
        and Decimal(str(ask)) % Decimal("0.001") == 0,
        "quote_tick_invalid",
    )
    _nonnegative_integer(observation.get("bid_size_shares"), "bid_size_shares")
    _nonnegative_integer(observation.get("ask_size_shares"), "ask_size_shares")
    _require(ask >= bid, "crossed_quote_invalid")
    if observation.get("last_price") is not None:
        _positive(observation["last_price"], "last_price")

    start = _aware(event["start_asia_shanghai"], "event_start")
    end = _aware(event["end_asia_shanghai"], "event_end")
    observed = _aware(observation.get("observed_at"), "observed_at")
    exchange = _aware(observation.get("exchange_timestamp"), "exchange_timestamp")
    verified = _aware(attestation.get("verified_at"), "verified_at")
    received = received_at.astimezone(timezone.utc)
    start_utc, end_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    before_end = (lambda value: value <= end_utc) if event_id == "entry" else (
        lambda value: value < end_utc
    )
    _require(start_utc <= received and before_end(received), "actual_receive_outside_window")
    _require(
        start_utc <= verified.astimezone(timezone.utc)
        and before_end(verified.astimezone(timezone.utc)),
        "attestation_outside_window",
    )
    _require(
        start_utc <= observed.astimezone(timezone.utc)
        and before_end(observed.astimezone(timezone.utc)),
        "observation_outside_window",
    )
    _require(
        verified.astimezone(timezone.utc) <= observed.astimezone(timezone.utc),
        "attestation_after_observation",
    )
    source_lag = (observed.astimezone(timezone.utc) - exchange.astimezone(timezone.utc)).total_seconds()
    receive_lag = (received - observed.astimezone(timezone.utc)).total_seconds()
    _require(source_lag >= 0, "exchange_timestamp_future")
    _require(
        source_lag
        <= protocol["timing_policy"]["maximum_exchange_to_observation_lag_seconds"],
        "source_quote_stale",
    )
    _require(receive_lag >= 0, "observation_after_receive")
    _require(
        receive_lag
        <= protocol["timing_policy"]["maximum_observation_to_local_receive_lag_seconds"],
        "actual_receive_late_no_backfill",
    )


def _event_paths(root: Path, event_id: str) -> dict[str, Path]:
    base = _safe_path(
        root, f"reports/local_closure/m23_forward_observation/{event_id}"
    )
    return {
        "attestation": base / "live_source_attestation.json",
        "observation": base / "quote_observation.json",
        "payload": base / "source_payload.bin",
        "origin": base / "origin.json",
        "coverage": base / "corporate_action_coverage.json",
        "coverage_payload": base / "corporate_action_payload.bin",
        "coverage_origin": base / "corporate_action_origin.json",
    }


def capture_event(event_id: str) -> str:
    root = repository_root()
    protocol = _read_json(_safe_path(root, PROTOCOL_PATH))
    events = validate_protocol(protocol)
    _require(event_id in events, "unknown_event")
    seal = _read_json(_safe_path(root, SEAL_PATH))
    validate_seal(root, seal)
    sync = _read_json(_safe_path(root, SYNC_PATH))
    _require(validate_sync(root, protocol, seal, sync), "remote_freeze_not_verified")
    paths = _event_paths(root, event_id)
    attestation = _read_json(paths["attestation"])
    observation = _read_json(paths["observation"])
    try:
        payload_bytes = paths["payload"].stat().st_size
    except OSError as exc:
        raise ObservationError(f"source_payload_unreadable:{exc}") from None
    payload_sha256 = _digest(paths["payload"])
    received_at = datetime.now(timezone.utc)
    validate_live_inputs(
        protocol,
        events[event_id],
        attestation,
        observation,
        payload_sha256=payload_sha256,
        payload_bytes=payload_bytes,
        received_at=received_at,
    )
    origin = {
        "schema": "regis.m23.event_origin.v1",
        "event_id": event_id,
        "status": "observed",
        "received_at_utc": received_at.isoformat(),
        "attestation": attestation,
        "observation": observation,
        "source_payload": {
            "path": str(paths["payload"].relative_to(root)).replace("\\", "/"),
            "bytes": payload_bytes,
            "sha256": payload_sha256,
        },
        "research_only": True,
        "trade_or_order_created": False,
    }
    return write_once_json(paths["origin"], origin)


def expire_event(event_id: str) -> str:
    root = repository_root()
    protocol = _read_json(_safe_path(root, PROTOCOL_PATH))
    events = validate_protocol(protocol)
    _require(event_id in events, "unknown_event")
    seal = _read_json(_safe_path(root, SEAL_PATH))
    validate_seal(root, seal)
    sync = _read_json(_safe_path(root, SYNC_PATH))
    _require(validate_sync(root, protocol, seal, sync), "remote_freeze_not_verified")
    now = datetime.now(timezone.utc)
    end = _aware(events[event_id]["end_asia_shanghai"], "event_end")
    _require(now >= end.astimezone(timezone.utc), "event_not_yet_expired")
    origin = {
        "schema": "regis.m23.event_origin.v1",
        "event_id": event_id,
        "status": "expired_missing",
        "recorded_at_utc": now.isoformat(),
        "reason": "no_valid_observation_was_written_inside_the_frozen_window",
        "research_only": True,
        "trade_or_order_created": False,
    }
    return write_once_json(_event_paths(root, event_id)["origin"], origin)


EVENT_RECEIPT_FIELDS = {
    "schema",
    "event_id",
    "status",
    "event_origin_sha256",
    "local_event_time_utc",
    "source_payload_sha256",
    "source_payload_bytes",
    "exported_at_utc",
    "contract_sha256",
    "freeze_commit",
    "contains_price_data",
    "contains_account_data",
    "research_only",
}


def build_event_receipt(
    protocol: dict[str, Any],
    event: dict[str, Any],
    origin: dict[str, Any],
    *,
    origin_sha256: str,
    freeze_commit: str,
    exported_at: datetime,
) -> dict[str, Any]:
    status = origin.get("status")
    _require(
        status in {"observed", "expired_missing"}
        and origin.get("event_id") == event["id"]
        and isinstance(origin_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", origin_sha256)
        and isinstance(freeze_commit, str)
        and re.fullmatch(r"[0-9a-f]{40}", freeze_commit),
        "receipt_origin_identity_invalid",
    )
    event_time = (
        origin.get("received_at_utc")
        if status == "observed"
        else origin.get("recorded_at_utc")
    )
    event_clock = _aware(event_time, "receipt_event_time")
    export_clock = exported_at.astimezone(timezone.utc)
    end = _aware(event["end_asia_shanghai"], "event_end").astimezone(timezone.utc)
    _require(export_clock >= event_clock.astimezone(timezone.utc), "receipt_export_before_event")
    _require(
        export_clock
        <= end
        + timedelta(
            seconds=protocol["event_receipt_policy"][
                "export_deadline_seconds_after_window"
            ]
        ),
        "receipt_export_deadline_missed",
    )
    payload = origin.get("source_payload") if status == "observed" else None
    return {
        "schema": "regis.m23.sanitized_event_receipt.v1",
        "event_id": event["id"],
        "status": status,
        "event_origin_sha256": origin_sha256,
        "local_event_time_utc": event_clock.astimezone(timezone.utc).isoformat(),
        "source_payload_sha256": payload["sha256"] if payload else None,
        "source_payload_bytes": payload["bytes"] if payload else None,
        "exported_at_utc": export_clock.isoformat(),
        "contract_sha256": CONTRACT_SHA256,
        "freeze_commit": freeze_commit,
        "contains_price_data": False,
        "contains_account_data": False,
        "research_only": True,
    }


def export_event_receipt(event_id: str) -> str:
    root = repository_root()
    protocol = _read_json(_safe_path(root, PROTOCOL_PATH))
    events = validate_protocol(protocol)
    _require(event_id in events, "unknown_event")
    seal = _read_json(_safe_path(root, SEAL_PATH))
    validate_seal(root, seal)
    sync = _read_json(_safe_path(root, SYNC_PATH))
    _require(validate_sync(root, protocol, seal, sync), "remote_freeze_not_verified")
    origin_path = _event_paths(root, event_id)["origin"]
    _require(origin_path.is_file(), f"event_origin_missing:{event_id}")
    origin = _validated_event_origin(
        root, protocol, events[event_id], _read_json(origin_path)
    )
    receipt = build_event_receipt(
        protocol,
        events[event_id],
        origin,
        origin_sha256=_digest(origin_path),
        freeze_commit=sync["freeze_commit"],
        exported_at=datetime.now(timezone.utc),
    )
    return write_once_json(_safe_path(root, EVENT_RECEIPT_PATHS[event_id]), receipt)


COVERAGE_FIELDS = {
    "schema",
    "event_id",
    "code",
    "period_start",
    "period_end",
    "source_id",
    "source_url",
    "rights_status",
    "capture_mode",
    "checked_at",
    "query_security_code",
    "query_announcement_type",
    "query_start_date",
    "query_end_date",
    "page_scope",
    "captured_result_pages",
    "reported_total_pages",
    "reported_result_count",
    "matched_relevant_announcements",
    "payload_represents_all_result_pages",
    "coverage_status",
    "source_payload_sha256",
    "source_payload_bytes",
    "credentials_present",
}


def validate_corporate_action_coverage(
    protocol: dict[str, Any],
    event: dict[str, Any],
    coverage: dict[str, Any],
    *,
    payload_sha256: str,
    payload_bytes: int,
    received_at: datetime,
) -> None:
    _require(event["id"] != "entry", "coverage_not_applicable_to_entry")
    _require(set(coverage) == COVERAGE_FIELDS, "coverage_fields_invalid")
    policy = protocol["execution_and_accounting"]["corporate_action_source_policy"]
    source_url = coverage.get("source_url")
    parsed = urlsplit(source_url) if isinstance(source_url, str) else None
    _require(
        coverage.get("schema") == "regis.m23.corporate_action_coverage.v1"
        and coverage.get("event_id") == event["id"]
        and coverage.get("code") == protocol["benchmark"]["code"]
        and coverage.get("period_start") == "2026-10-08"
        and coverage.get("period_end") == event["trade_date"]
        and coverage.get("source_id") == policy["source_id"]
        and coverage.get("rights_status")
        == protocol["source_policy"]["rights_status_required"]
        and coverage.get("capture_mode") == policy["capture_mode"]
        and coverage.get("source_url") == policy["announcement_page"]
        and coverage.get("query_security_code") == policy["required_security_code"]
        and coverage.get("query_announcement_type")
        == policy["required_announcement_type"]
        and coverage.get("query_start_date") == "2026-10-08"
        and coverage.get("query_end_date") == event["trade_date"]
        and coverage.get("page_scope") == policy["required_page_scope"]
        and type(coverage.get("captured_result_pages")) is int
        and coverage["captured_result_pages"] >= 1
        and coverage.get("captured_result_pages")
        == coverage.get("reported_total_pages")
        and type(coverage.get("reported_result_count")) is int
        and coverage["reported_result_count"] >= 0
        and isinstance(coverage.get("matched_relevant_announcements"), list)
        and all(
            isinstance(title, str) and bool(title.strip())
            for title in coverage["matched_relevant_announcements"]
        )
        and coverage.get("payload_represents_all_result_pages") is True
        and coverage.get("coverage_status")
        in {policy["clear_status"], *policy["other_statuses"]}
        and coverage.get("credentials_present") is False
        and parsed is not None
        and parsed.scheme == "https"
        and parsed.hostname == "www.sse.com.cn"
        and parsed.username is None
        and parsed.password is None,
        "coverage_identity_invalid",
    )
    if coverage["coverage_status"] == policy["clear_status"]:
        _require(
            coverage["matched_relevant_announcements"] == [],
            "coverage_clear_status_conflicts_with_matches",
        )
    if coverage["coverage_status"] == "relevant_event_found_unsupported":
        _require(
            bool(coverage["matched_relevant_announcements"]),
            "coverage_event_status_requires_match",
        )
    _require(
        0 < payload_bytes <= MAX_PAYLOAD_BYTES
        and coverage.get("source_payload_bytes") == payload_bytes
        and coverage.get("source_payload_sha256") == payload_sha256,
        "coverage_payload_binding_invalid",
    )
    checked = _aware(coverage.get("checked_at"), "coverage_checked_at")
    received = received_at.astimezone(timezone.utc)
    end = _aware(event["end_asia_shanghai"], "event_end").astimezone(timezone.utc)
    _require(checked.astimezone(timezone.utc) >= end, "coverage_checked_before_horizon_end")
    _require(checked.astimezone(timezone.utc) <= received, "coverage_checked_after_receive")


def capture_corporate_action_coverage(event_id: str) -> str:
    root = repository_root()
    protocol = _read_json(_safe_path(root, PROTOCOL_PATH))
    events = validate_protocol(protocol)
    _require(event_id in events and event_id != "entry", "unknown_coverage_event")
    seal = _read_json(_safe_path(root, SEAL_PATH))
    validate_seal(root, seal)
    sync = _read_json(_safe_path(root, SYNC_PATH))
    _require(validate_sync(root, protocol, seal, sync), "remote_freeze_not_verified")
    paths = _event_paths(root, event_id)
    coverage = _read_json(paths["coverage"])
    try:
        payload_bytes = paths["coverage_payload"].stat().st_size
    except OSError as exc:
        raise ObservationError(f"coverage_payload_unreadable:{exc}") from None
    payload_sha256 = _digest(paths["coverage_payload"])
    received_at = datetime.now(timezone.utc)
    validate_corporate_action_coverage(
        protocol,
        events[event_id],
        coverage,
        payload_sha256=payload_sha256,
        payload_bytes=payload_bytes,
        received_at=received_at,
    )
    origin = {
        "schema": "regis.m23.corporate_action_origin.v1",
        "event_id": event_id,
        "received_at_utc": received_at.isoformat(),
        "coverage": coverage,
        "source_payload": {
            "path": str(paths["coverage_payload"].relative_to(root)).replace("\\", "/"),
            "bytes": payload_bytes,
            "sha256": payload_sha256,
        },
        "research_only": True,
    }
    return write_once_json(paths["coverage_origin"], origin)


def _validated_event_origin(
    root: Path,
    protocol: dict[str, Any],
    event: dict[str, Any],
    origin: dict[str, Any],
) -> dict[str, Any]:
    event_id = event["id"]
    _require(
        origin.get("schema") == "regis.m23.event_origin.v1"
        and origin.get("event_id") == event_id
        and origin.get("status") in {"observed", "expired_missing"},
        f"event_origin_invalid:{event_id}",
    )
    if origin["status"] == "expired_missing":
        _require(
            set(origin)
            == {
                "schema",
                "event_id",
                "status",
                "recorded_at_utc",
                "reason",
                "research_only",
                "trade_or_order_created",
            }
            and origin.get("reason")
            == "no_valid_observation_was_written_inside_the_frozen_window"
            and origin.get("research_only") is True
            and origin.get("trade_or_order_created") is False
            and _aware(origin.get("recorded_at_utc"), "expired_recorded_at")
            > _aware(event["end_asia_shanghai"], "event_end").astimezone(timezone.utc),
            f"expired_origin_invalid:{event_id}",
        )
        return origin
    _require(
        set(origin)
        == {
            "schema",
            "event_id",
            "status",
            "received_at_utc",
            "attestation",
            "observation",
            "source_payload",
            "research_only",
            "trade_or_order_created",
        }
        and origin.get("research_only") is True
        and origin.get("trade_or_order_created") is False,
        f"observed_origin_invalid:{event_id}",
    )
    paths = _event_paths(root, event_id)
    payload = origin.get("source_payload")
    _require(
        isinstance(payload, dict)
        and payload.get("path")
        == str(paths["payload"].relative_to(root)).replace("\\", "/")
        and payload.get("bytes") == paths["payload"].stat().st_size
        and payload.get("sha256") == _digest(paths["payload"]),
        f"event_origin_payload_invalid:{event_id}",
    )
    validate_live_inputs(
        protocol,
        event,
        origin["attestation"],
        origin["observation"],
        payload_sha256=payload["sha256"],
        payload_bytes=payload["bytes"],
        received_at=_aware(origin["received_at_utc"], "origin_received_at"),
    )
    return origin


def _validated_event_receipt(
    root: Path,
    protocol: dict[str, Any],
    event: dict[str, Any],
    origin: dict[str, Any],
    origin_sha256: str,
    freeze_commit: str,
) -> dict[str, Any]:
    receipt_path = _safe_path(root, EVENT_RECEIPT_PATHS[event["id"]])
    _require(receipt_path.is_file(), f"event_receipt_missing:{event['id']}")
    receipt = _read_json(receipt_path)
    _require(set(receipt) == EVENT_RECEIPT_FIELDS, f"event_receipt_fields_invalid:{event['id']}")
    expected = build_event_receipt(
        protocol,
        event,
        origin,
        origin_sha256=origin_sha256,
        freeze_commit=freeze_commit,
        exported_at=_aware(receipt.get("exported_at_utc"), "receipt_exported_at"),
    )
    _require(receipt == expected, f"event_receipt_invalid:{event['id']}")
    return receipt


def _validated_coverage_origin(
    root: Path,
    protocol: dict[str, Any],
    event: dict[str, Any],
    origin: dict[str, Any],
) -> dict[str, Any]:
    event_id = event["id"]
    _require(
        set(origin)
        == {
            "schema",
            "event_id",
            "received_at_utc",
            "coverage",
            "source_payload",
            "research_only",
        }
        and origin.get("schema") == "regis.m23.corporate_action_origin.v1"
        and origin.get("event_id") == event_id
        and origin.get("research_only") is True,
        f"coverage_origin_invalid:{event_id}",
    )
    paths = _event_paths(root, event_id)
    payload = origin.get("source_payload")
    _require(
        isinstance(payload, dict)
        and payload.get("path")
        == str(paths["coverage_payload"].relative_to(root)).replace("\\", "/")
        and payload.get("bytes") == paths["coverage_payload"].stat().st_size
        and payload.get("sha256") == _digest(paths["coverage_payload"]),
        f"coverage_origin_payload_invalid:{event_id}",
    )
    validate_corporate_action_coverage(
        protocol,
        event,
        origin["coverage"],
        payload_sha256=payload["sha256"],
        payload_bytes=payload["bytes"],
        received_at=_aware(origin["received_at_utc"], "coverage_received_at"),
    )
    return origin


def _yuan(value: Decimal) -> str:
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), ".2f")


def _etf_costs(protocol: dict[str, Any], notional: Decimal) -> dict[str, Decimal]:
    assumptions = protocol["execution_and_accounting"]["fee_assumptions"]
    commission = max(
        Decimal(str(assumptions["minimum_commission_yuan"])),
        notional * Decimal(str(assumptions["commission_rate"])),
    ).quantize(CENT, rounding=ROUND_HALF_UP)
    transfer = (
        notional * Decimal(str(assumptions["transfer_rate_for_stock_etf"]))
    ).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    slippage = (
        notional
        * Decimal(str(assumptions["additional_slippage_rate_beyond_visible_quote"]))
    ).quantize(CENT, rounding=ROUND_HALF_UP)
    stamp = Decimal("0.00")
    market_impact = Decimal("0.00")
    return {
        "commission": commission,
        "transfer": transfer,
        "stamp": stamp,
        "slippage": slippage,
        "market_impact": market_impact,
        "total": commission + transfer + stamp + slippage + market_impact,
    }


def _maximum_affordable_shares(
    protocol: dict[str, Any], cash: Decimal, price: Decimal
) -> int:
    shares = int((cash / (price * LOT_SIZE)).to_integral_value(rounding=ROUND_DOWN)) * LOT_SIZE
    while shares > 0:
        notional = (price * shares).quantize(CENT, rounding=ROUND_HALF_UP)
        if notional + _etf_costs(protocol, notional)["total"] <= cash:
            return shares
        shares -= LOT_SIZE
    return 0


def account_horizon(
    protocol: dict[str, Any],
    entry_origin: dict[str, Any],
    exit_origin: dict[str, Any],
    coverage_origin: dict[str, Any] | None,
) -> dict[str, Any]:
    event_id = exit_origin["event_id"]
    horizon = int(event_id.removeprefix("exit_"))
    base = {
        "exit_event_id": event_id,
        "horizon_sessions": horizon,
        "capital_yuan": "150000.00",
        "return_computed": False,
        "net_return_pct": None,
        "cash_comparison_pct": None,
    }
    if entry_origin.get("status") != "observed" or exit_origin.get("status") != "observed":
        return {**base, "status": "not_computable_missing_observation"}
    if coverage_origin is None:
        return {**base, "status": "not_computable_corporate_action_coverage_missing"}
    clear_status = protocol["execution_and_accounting"]["corporate_action_source_policy"][
        "clear_status"
    ]
    if coverage_origin["coverage"]["coverage_status"] != clear_status:
        return {
            **base,
            "status": "not_computable_corporate_action_coverage_not_clear",
            "coverage_status": coverage_origin["coverage"]["coverage_status"],
        }

    capital = Decimal("150000.00")
    entry_quote = entry_origin["observation"]
    exit_quote = exit_origin["observation"]
    ask = Decimal(str(entry_quote["ask_price"]))
    bid = Decimal(str(exit_quote["bid_price"]))
    desired = _maximum_affordable_shares(protocol, capital, ask)
    visible_ask = entry_quote["ask_size_shares"] // LOT_SIZE * LOT_SIZE
    bought = min(desired, visible_ask)
    if bought == 0:
        return {
            **base,
            "status": "not_computable_no_entry_fill",
            "entry_order_shares": desired,
            "entry_filled_shares": 0,
            "entry_visible_ask_shares": entry_quote["ask_size_shares"],
        }
    buy_notional = (ask * bought).quantize(CENT, rounding=ROUND_HALF_UP)
    buy_costs = _etf_costs(protocol, buy_notional)
    cash_after_buy = capital - buy_notional - buy_costs["total"]
    visible_bid = exit_quote["bid_size_shares"] // LOT_SIZE * LOT_SIZE
    sold = min(bought, visible_bid)
    sell_notional = (bid * sold).quantize(CENT, rounding=ROUND_HALF_UP)
    sell_costs = _etf_costs(protocol, sell_notional) if sold else {
        key: Decimal("0.00")
        for key in ("commission", "transfer", "stamp", "slippage", "market_impact", "total")
    }
    cash_after_sell = cash_after_buy + sell_notional - sell_costs["total"]
    remaining = bought - sold
    details = {
        **base,
        "entry_ask_yuan": format(ask, "f"),
        "entry_order_shares": desired,
        "entry_visible_ask_shares": entry_quote["ask_size_shares"],
        "entry_filled_shares": bought,
        "entry_fill_fraction": round(bought / desired, 6) if desired else 0,
        "entry_notional_yuan": _yuan(buy_notional),
        "entry_costs_yuan": {key: _yuan(value) for key, value in buy_costs.items()},
        "cash_after_entry_yuan": _yuan(cash_after_buy),
        "exit_bid_yuan": format(bid, "f"),
        "exit_order_shares": bought,
        "exit_visible_bid_shares": exit_quote["bid_size_shares"],
        "exit_filled_shares": sold,
        "remaining_shares": remaining,
        "exit_notional_yuan": _yuan(sell_notional),
        "exit_costs_yuan": {key: _yuan(value) for key, value in sell_costs.items()},
        "cash_after_exit_yuan": _yuan(cash_after_sell),
        "cost_assumption_status": "unverified_no_account_read",
        "zero_transfer_and_stamp_status": (
            "research_assumption_current_chinaclear_body_not_independently_retrieved"
        ),
        "model_admission_use": "prohibited_until_actual_costs_verified",
        "corporate_action_coverage_status": clear_status,
        "corporate_action_coverage_authority": (
            "source_bound_manual_all_pages_claim_not_independent_provider_completeness"
        ),
    }
    if remaining:
        return {**details, "status": "not_computable_partial_exit_fill"}
    net_return = ((cash_after_sell - capital) / capital * 100).quantize(
        RETURN_PRECISION, rounding=ROUND_HALF_UP
    )
    return {
        **details,
        "status": "computed_full_top_of_book_path",
        "return_computed": True,
        "net_return_pct": format(net_return, ".6f"),
        "cash_comparison_pct": format(net_return, ".6f"),
    }


def build_forward_summary() -> dict[str, Any]:
    root = repository_root()
    protocol = _read_json(_safe_path(root, PROTOCOL_PATH))
    events = validate_protocol(protocol)
    seal = _read_json(_safe_path(root, SEAL_PATH))
    validate_seal(root, seal)
    sync = _read_json(_safe_path(root, SYNC_PATH))
    _require(validate_sync(root, protocol, seal, sync), "remote_freeze_not_verified")
    rows = []
    origins: dict[str, dict[str, Any]] = {}
    for event_id in events:
        path = _event_paths(root, event_id)["origin"]
        _require(path.is_file(), f"event_origin_missing:{event_id}")
        origin = _validated_event_origin(root, protocol, events[event_id], _read_json(path))
        origins[event_id] = origin
        origin_sha256 = _digest(path)
        receipt = _validated_event_receipt(
            root,
            protocol,
            events[event_id],
            origin,
            origin_sha256,
            sync["freeze_commit"],
        )
        receipt_path = _safe_path(root, EVENT_RECEIPT_PATHS[event_id])
        rows.append(
            {
                "event_id": event_id,
                "status": origin["status"],
                "origin_sha256": origin_sha256,
                "sanitized_receipt_sha256": _digest(receipt_path),
                "receipt_exported_at_utc": receipt["exported_at_utc"],
            }
        )
    horizons = []
    for event_id in ("exit_1", "exit_3", "exit_5"):
        coverage_path = _event_paths(root, event_id)["coverage_origin"]
        coverage_origin = None
        coverage_sha256 = None
        if coverage_path.is_file():
            coverage_origin = _validated_coverage_origin(
                root, protocol, events[event_id], _read_json(coverage_path)
            )
            coverage_sha256 = _digest(coverage_path)
        result = account_horizon(
            protocol, origins["entry"], origins[event_id], coverage_origin
        )
        result["entry_origin_sha256"] = rows[0]["origin_sha256"]
        result["exit_origin_sha256"] = next(
            row["origin_sha256"] for row in rows if row["event_id"] == event_id
        )
        result["corporate_action_origin_sha256"] = coverage_sha256
        horizons.append(result)
    all_observed = all(row["status"] == "observed" for row in rows)
    all_returns = all(row["return_computed"] for row in horizons)
    return {
        "schema": "regis.m23.forward_path_summary.v1",
        "task_id": protocol["task_id"],
        "events": rows,
        "all_events_observed": all_observed,
        "horizons_are_mutually_exclusive": True,
        "horizons": horizons,
        "returns_computed": all_returns,
        "return_status": (
            "all_three_horizons_computed"
            if all_returns
            else "one_or_more_horizons_not_computable"
        ),
        "stock_arm": {
            "state": "cash_due_to_m21_zero_candidate",
            "capital_yuan": "150000.00",
            "return_pct": "0.000000",
        },
        "stock_selection_excess_computable": False,
        "model_admitted": False,
        "economic_target_A_validated": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--write-summary", action="store_true")
    capture = sub.add_parser("capture")
    capture.add_argument("event_id")
    receipt = sub.add_parser("export-receipt")
    receipt.add_argument("event_id")
    coverage = sub.add_parser("capture-coverage")
    coverage.add_argument("event_id")
    expire = sub.add_parser("expire")
    expire.add_argument("event_id")
    finalize = sub.add_parser("finalize")
    finalize.add_argument("--write-summary", action="store_true")
    args = parser.parse_args(argv)
    try:
        root = repository_root()
        if args.action == "preflight":
            summary = build_preflight()
            state = "not_requested"
            if args.write_summary:
                summary_path = (
                    PREFLIGHT_FROZEN_SUMMARY_PATH
                    if summary["private_remote_freeze_verified"]
                    else PREFLIGHT_PENDING_SUMMARY_PATH
                )
                state = write_once_json(
                    _safe_path(root, summary_path), summary
                )
            result = {"status": summary["status"], "summary_write": state}
        elif args.action == "capture":
            result = {"event_id": args.event_id, "write": capture_event(args.event_id)}
        elif args.action == "export-receipt":
            result = {"event_id": args.event_id, "write": export_event_receipt(args.event_id)}
        elif args.action == "capture-coverage":
            result = {
                "event_id": args.event_id,
                "write": capture_corporate_action_coverage(args.event_id),
            }
        elif args.action == "expire":
            result = {"event_id": args.event_id, "write": expire_event(args.event_id)}
        else:
            summary = build_forward_summary()
            state = "not_requested"
            if args.write_summary:
                state = write_once_json(_safe_path(root, FORWARD_SUMMARY_PATH), summary)
            result = {"status": summary["return_status"], "summary_write": state}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ObservationError, OSError, TypeError) as exc:
        print(f"m23_observation: {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
