"""M22 execution preflight.

This module validates the frozen research protocol and local runtime only.  It
does not call a quote endpoint, read a brokerage account, arm a scheduler, or
infer that a future trading-session quote will be reachable.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import subprocess
import tempfile
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PROTOCOL_PATH = "QUANT_RESEARCH_M22_PROTOCOL_20260930.json"
MATRIX_PATH = "QUANT_RESEARCH_M22_DATA_RIGHTS_MATRIX_20260930.json"
SUMMARY_PATH = "QUANT_RESEARCH_M22_PREFLIGHT_SUMMARY_20260930.json"
OBSERVATION_INBOX_PATH = (
    "reports/local_closure/m22_forward_observer/2026-10-08/"
    "quote_observation_inbox.json"
)
SOURCE_PAYLOAD_PATH = (
    "reports/local_closure/m22_forward_observer/2026-10-08/source_payload.bin"
)
RAW_OBSERVATION_PATH = (
    "reports/local_closure/m22_forward_observer/2026-10-08/quote_origin.json"
)
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_SOURCE_PAYLOAD_BYTES = 4 * 1024 * 1024


class PreflightError(ValueError):
    """A frozen preflight or observation invariant failed."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PreflightError(message)


def _safe_path(root: Path, value: str) -> Path:
    _require(isinstance(value, str) and bool(value.strip()), "path_invalid")
    parts = value.replace("\\", "/").split("/")
    _require(
        not PureWindowsPath(value).drive
        and not value.startswith(("/", "\\"))
        and all(
            part not in {"", ".", ".."}
            and part == part.rstrip(" .")
            and ":" not in part
            and "\x00" not in part
            for part in parts
        ),
        f"path_unsafe:{value}",
    )
    base = root.resolve()
    result = base.joinpath(*parts).resolve()
    _require(result.is_relative_to(base), f"path_escape:{value}")
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        _require(path.stat().st_size <= MAX_JSON_BYTES, f"json_too_large:{path.name}")
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreflightError(f"json_unreadable:{path.name}:{exc}") from None
    _require(isinstance(value, dict), f"json_object_required:{path.name}")
    return value


def _digest(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise PreflightError(f"binding_unreadable:{path.name}:{exc}") from None


def _aware_datetime(value: Any, label: str) -> datetime:
    _require(isinstance(value, str) and bool(value), f"{label}_invalid")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PreflightError(f"{label}_invalid") from None
    _require(result.utcoffset() is not None, f"{label}_timezone_missing")
    return result


def _positive_number(value: Any, label: str) -> float:
    _require(
        type(value) in {int, float}
        and math.isfinite(float(value))
        and float(value) > 0,
        f"{label}_invalid",
    )
    return float(value)


def _canonical(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def write_once_json(path: Path, value: dict[str, Any]) -> str:
    """Create *path* once; an identical retry is allowed, a rewrite is rejected."""
    raw = _canonical(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _require(path.read_bytes() == raw, "immutable_output_conflict")
        return "existing_identical"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".m22-", suffix=".tmp", delete=False
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


def validate_protocol(protocol: dict[str, Any]) -> dict[str, datetime]:
    _require(
        protocol.get("schema") == "regis.m22.execution_preflight_protocol.v1",
        "protocol_schema_invalid",
    )
    _require(protocol.get("task_id") == "QUANT-RESEARCH-20260930-022", "task_id_invalid")
    for field in (
        "research_only",
        "changes_trade_authority",
        "reads_live_account",
        "automatic_order_submission",
    ):
        expected = field == "research_only"
        _require(protocol.get(field) is expected, f"protocol_authority_invalid:{field}")

    capital = protocol.get("capital")
    _require(isinstance(capital, dict), "capital_missing")
    _require(
        capital.get("stock_arm_yuan") == 150000
        and capital.get("etf_arm_yuan") == 150000,
        "capital_not_matched_150000",
    )
    _require(
        capital.get("relationship") == "mutually_exclusive_same_capital_counterfactuals"
        and capital.get("combine_capital_or_returns") is False,
        "capital_arms_not_mutually_exclusive",
    )
    _require(
        capital.get("stock_arm_m21_action") == "hold_cash_no_candidate",
        "m21_stock_action_changed",
    )

    binding = protocol.get("m21_binding")
    _require(isinstance(binding, dict), "m21_binding_missing")
    _require(
        binding.get("expected_candidate_count") == 0
        and binding.get("expected_market_gate_open") is False,
        "m21_expected_state_changed",
    )

    benchmark = protocol.get("benchmark")
    _require(isinstance(benchmark, dict), "benchmark_missing")
    _require(
        benchmark.get("code") == "510300.SH"
        and benchmark.get("order_unit") == 100
        and benchmark.get("t_plus_one") is True
        and benchmark.get("price_basis") == "unadjusted_exchange_traded_quote",
        "benchmark_execution_boundary_invalid",
    )

    window = protocol.get("observation_window")
    _require(isinstance(window, dict), "observation_window_missing")
    start = _aware_datetime(window.get("start_asia_shanghai"), "window_start")
    end = _aware_datetime(window.get("end_asia_shanghai"), "window_end")
    start_utc = _aware_datetime(window.get("start_utc"), "window_start_utc")
    start_new_york = _aware_datetime(
        window.get("start_america_new_york"), "window_start_new_york"
    )
    try:
        shanghai = ZoneInfo("Asia/Shanghai")
        new_york = ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError as exc:
        raise PreflightError(f"timezone_database_missing:{exc}") from None
    _require(start.astimezone(shanghai).isoformat() == window["start_asia_shanghai"], "shanghai_time_mismatch")
    _require(end.astimezone(shanghai).isoformat() == window["end_asia_shanghai"], "shanghai_end_mismatch")
    _require(start.astimezone(timezone.utc) == start_utc, "utc_time_mismatch")
    _require(start.astimezone(new_york) == start_new_york, "new_york_time_mismatch")
    _require(end > start, "observation_window_order_invalid")
    _require(
        window.get("trade_date") == start.astimezone(shanghai).date().isoformat()
        and window.get("maximum_source_lag_seconds") == 30
        and window.get("no_backfill") is True,
        "observation_window_policy_invalid",
    )

    source = protocol.get("quote_source_policy")
    _require(isinstance(source, dict), "quote_source_policy_missing")
    _require(
        source.get("selected_source_id") == "sse_public_web_manual"
        and source.get("selected_source_status") == "unverified_until_market_session"
        and source.get("documented_programmatic_api_authorized") is False
        and source.get("capture_armed") is False
        and source.get("requires_exchange_timestamp") is True
        and source.get("requires_executable_ask") is True
        and source.get("forbid_undocumented_scraping") is True
        and source.get("raw_observation_git_policy") == "local_ignored_only",
        "quote_source_policy_invalid",
    )
    _require(
        set(source.get("allowed_capture_methods_after_live_preflight", []))
        == {"manual_browser_saved_observation", "licensed_provider_api"},
        "capture_methods_invalid",
    )

    actions = protocol.get("corporate_actions")
    _require(
        isinstance(actions, dict)
        and actions.get("total_return_requires_complete_event_coverage") is True
        and actions.get("cash_distribution_requires_record_ex_payout_and_receipt") is True
        and actions.get("unknown_event_status") == "reject_total_return",
        "corporate_action_gate_invalid",
    )
    background = protocol.get("background_process")
    _require(
        isinstance(background, dict)
        and background.get("required_on_windows") is True
        and background.get("create_no_window") is True
        and background.get("use_shell_execute") is False
        and background.get("redirect_output") is True
        and background.get("temporary_helper_cleanup_required") is True
        and background.get("armed") is False,
        "background_policy_invalid",
    )
    boundary = protocol.get("completion_boundary")
    _require(
        isinstance(boundary, dict)
        and boundary.get("engineering_preflight_may_pass") is True
        and boundary.get("live_quote_source_may_remain_pending") is True
        and boundary.get("returns_computed") is False
        and boundary.get("model_admitted") is False
        and boundary.get("economic_target_A_validated") is False
        and boundary.get("m23_requires_new_frozen_task") is True,
        "completion_boundary_invalid",
    )
    return {
        "start": start,
        "end": end,
        "start_utc": start_utc,
        "start_new_york": start_new_york,
    }


def validate_matrix(matrix: dict[str, Any]) -> dict[str, dict[str, Any]]:
    _require(matrix.get("schema") == "regis.m22.data_rights_matrix.v1", "matrix_schema_invalid")
    _require(matrix.get("budget_limit_cny_per_month") == 200, "budget_limit_changed")
    _require(matrix.get("purchase_performed") is False, "purchase_not_allowed")
    _require(matrix.get("account_or_token_read") is False, "account_or_token_read_not_allowed")
    rows = matrix.get("sources")
    _require(isinstance(rows, list) and rows, "source_rows_missing")
    _require(
        all(isinstance(row, dict) and isinstance(row.get("id"), str) for row in rows),
        "source_row_invalid",
    )
    sources = {row["id"]: row for row in rows}
    _require(len(sources) == len(rows), "source_id_duplicate")
    required = {
        "github_private_archive",
        "tradsig_personal_research_snapshot",
        "baostock_public_sdk",
        "sse_public_web_manual",
        "sse_licensed_level1_level2",
        "tushare_candidate",
        "broker_terminal_quote",
        "undocumented_third_party_quote_endpoints",
    }
    _require(required <= sources.keys(), "source_inventory_incomplete")
    public = sources["sse_public_web_manual"]
    _require(
        public.get("decision") == "unverified_until_market_session"
        and public.get("actual_access")
        == "identity_and_pages_verified_trading_session_quote_unverified",
        "public_source_state_overstated",
    )
    undocumented = sources["undocumented_third_party_quote_endpoints"]
    _require(
        undocumented.get("decision") == "rejected"
        and undocumented.get("actual_access") == "not_called",
        "undocumented_source_not_rejected",
    )
    gates = matrix.get("gates")
    _require(isinstance(gates, dict), "matrix_gates_missing")
    _require(
        gates.get("m22_engineering_preflight") == "eligible_for_implementation"
        and gates.get("m23_live_quote_capture")
        == "blocked_until_market_session_reachability_and_executable_ask_verified"
        and gates.get("m24_historical_baseline")
        == "blocked_no_admitted_multi_year_pit_stock_and_etf_total_return_source"
        and gates.get("etf_total_return")
        == "blocked_until_complete_corporate_action_and_cash_distribution_coverage"
        and gates.get("automatic_capture") == "not_armed"
        and gates.get("model_admission") is False
        and gates.get("economic_target_A_validated") is False,
        "matrix_gate_overstated",
    )
    return sources


def validate_m21(summary: dict[str, Any]) -> None:
    _require(summary.get("schema") == "regis.m21.shadow_origin_summary.v1", "m21_schema_invalid")
    _require(summary.get("account_read") is False, "m21_account_read_invalid")
    _require(summary.get("candidate_count") == 0, "m21_candidate_state_changed")
    _require(summary.get("market_gate_open") is False, "m21_market_gate_changed")
    _require(summary.get("returns_computed") is False, "m21_returns_overstated")
    _require(summary.get("model_admitted") is False, "m21_model_overstated")
    _require(summary.get("economic_target_A_validated") is False, "m21_target_overstated")
    _require(summary.get("symbols_or_prices_published") is False, "m21_identity_publication_changed")


def _verify_binding(root: Path, relative: str, expected: Any, label: str) -> str:
    _require(
        isinstance(expected, str)
        and len(expected) == 64
        and all(char in "0123456789abcdef" for char in expected),
        f"{label}_hash_invalid",
    )
    actual = _digest(_safe_path(root, relative))
    _require(actual == expected, f"{label}_hash_mismatch")
    return actual


def build_preflight(root: Path) -> dict[str, Any]:
    root = root.resolve()
    protocol = _read_json(_safe_path(root, PROTOCOL_PATH))
    matrix = _read_json(_safe_path(root, MATRIX_PATH))
    times = validate_protocol(protocol)
    validate_matrix(matrix)

    m21_binding = protocol["m21_binding"]
    m21_summary_path = m21_binding["origin_summary_path"]
    bindings = {
        "m21_origin_summary": _verify_binding(
            root, m21_summary_path, m21_binding["origin_summary_sha256"], "m21_origin"
        ),
        "m21_pre_download_seal": _verify_binding(
            root,
            m21_binding["pre_download_seal_path"],
            m21_binding["pre_download_seal_sha256"],
            "m21_seal",
        ),
        "accounting_engine": _verify_binding(
            root,
            protocol["corporate_actions"]["accounting_engine_path"],
            protocol["corporate_actions"]["accounting_engine_sha256"],
            "accounting_engine",
        ),
        "cost_model": _verify_binding(
            root,
            protocol["costs_and_execution"]["cost_model_path"],
            protocol["costs_and_execution"]["cost_model_sha256"],
            "cost_model",
        ),
        "approved_plan": _verify_binding(
            root,
            protocol["implementation_binding"]["plan_path"],
            protocol["implementation_binding"]["plan_sha256"],
            "approved_plan",
        ),
        "plan_review": _verify_binding(
            root,
            protocol["implementation_binding"]["plan_review_path"],
            protocol["implementation_binding"]["plan_review_sha256"],
            "plan_review",
        ),
    }
    validate_m21(_read_json(_safe_path(root, m21_summary_path)))

    host_is_windows = os.name == "nt"
    create_no_window_available = hasattr(subprocess, "CREATE_NO_WINDOW")
    hidden_runtime_ready = (not host_is_windows) or create_no_window_available
    _require(hidden_runtime_ready, "windows_create_no_window_unavailable")

    return {
        "schema": "regis.m22.execution_preflight_summary.v1",
        "task_id": protocol["task_id"],
        "status": "engineering_ready_source_pending",
        "engineering_ready": True,
        "live_source_ready": False,
        "capture_armed": False,
        "m23_live_observation_ready": False,
        "m24_historical_baseline_ready": False,
        "etf_total_return_ready": False,
        "research_only": True,
        "reads_live_account": False,
        "purchase_performed": False,
        "network_call_performed": False,
        "long_running_process_started": False,
        "capital": {
            "yuan_per_counterfactual_arm": 150000,
            "arms_mutually_exclusive": True,
            "capital_or_returns_combined": False,
            "stock_arm_state": "cash_due_to_m21_zero_candidate",
            "etf_arm_state": "identity_and_rules_frozen_no_quote_or_fill",
        },
        "benchmark": {
            "code": protocol["benchmark"]["code"],
            "execution_quote_status": "unverified_until_market_session",
            "total_return_status": "blocked_missing_complete_event_coverage",
        },
        "observation_window": {
            "trade_date": protocol["observation_window"]["trade_date"],
            "asia_shanghai": times["start"].isoformat(),
            "utc": times["start_utc"].isoformat(),
            "america_new_york": times["start_new_york"].isoformat(),
            "maximum_source_lag_seconds": 30,
            "no_backfill": True,
        },
        "source_gate": {
            "selected_source_id": protocol["quote_source_policy"]["selected_source_id"],
            "status": "unverified_until_market_session",
            "documented_programmatic_api_authorized": False,
            "requires_exchange_timestamp": True,
            "requires_executable_ask": True,
        },
        "windows_background": {
            "host_is_windows": host_is_windows,
            "create_no_window_available": create_no_window_available,
            "configuration_valid": hidden_runtime_ready,
            "observer_started": False,
        },
        "bindings": bindings,
        "claims": {
            "returns_computed": False,
            "model_admitted": False,
            "economic_target_A_validated": False,
            "trade_signal_generated": False,
        },
        "next_required_event": (
            "Freeze M23, then verify rights, exchange timestamp and executable ask "
            "during the 2026-10-08 observation window; preserve zero/unreachable."
        ),
    }


def validate_observation(
    protocol: dict[str, Any],
    matrix: dict[str, Any],
    observation: dict[str, Any],
) -> None:
    """Validate a future armed observation; the frozen M22 inputs intentionally fail."""
    allowed_fields = {
        "schema",
        "source_id",
        "rights_status",
        "capture_method",
        "no_backfill",
        "code",
        "currency",
        "quote_type",
        "source_url",
        "observed_at",
        "exchange_timestamp",
        "ask_price",
        "bid_price",
        "last_price",
        "source_payload_sha256",
        "source_payload_bytes",
    }
    _require(set(observation) == allowed_fields, "observation_fields_invalid")
    _require(
        observation.get("schema") == "regis.m22.raw_quote_observation.v1",
        "observation_schema_invalid",
    )
    policy = protocol.get("quote_source_policy", {})
    background = protocol.get("background_process", {})
    _require(
        policy.get("capture_armed") is True
        and policy.get("selected_source_status") == "verified_live"
        and background.get("armed") is True,
        "source_not_verified_live",
    )
    rows = matrix.get("sources")
    _require(isinstance(rows, list), "source_rows_missing")
    sources = {
        row.get("id"): row for row in rows if isinstance(row, dict) and row.get("id")
    }
    source_id = observation.get("source_id")
    _require(source_id == policy.get("selected_source_id"), "observation_source_mismatch")
    source = sources.get(source_id, {})
    _require(
        source.get("decision") == "usable_for_live_observation"
        and source.get("actual_access") == "verified_live_quote",
        "source_not_verified_live",
    )
    _require(
        observation.get("rights_status") == policy.get("required_rights_status"),
        "observation_rights_invalid",
    )
    _require(
        observation.get("capture_method")
        in policy.get("allowed_capture_methods_after_live_preflight", []),
        "observation_capture_method_invalid",
    )
    _require(observation.get("no_backfill") is True, "observation_backfill_forbidden")
    _require(observation.get("code") == protocol.get("benchmark", {}).get("code"), "observation_code_invalid")
    _require(observation.get("currency") == "CNY", "observation_currency_invalid")
    _require(observation.get("quote_type") == "best_ask", "observation_quote_type_invalid")
    _require(
        observation.get("source_url") == protocol.get("benchmark", {}).get("market_page"),
        "observation_source_url_invalid",
    )
    payload_sha = observation.get("source_payload_sha256")
    _require(
        isinstance(payload_sha, str)
        and len(payload_sha) == 64
        and all(char in "0123456789abcdef" for char in payload_sha),
        "source_payload_hash_invalid",
    )
    _require(
        type(observation.get("source_payload_bytes")) is int
        and 0 < observation["source_payload_bytes"] <= MAX_SOURCE_PAYLOAD_BYTES,
        "source_payload_size_invalid",
    )

    observed = _aware_datetime(observation.get("observed_at"), "observed_at")
    exchange = _aware_datetime(observation.get("exchange_timestamp"), "exchange_timestamp")
    window = protocol.get("observation_window", {})
    start = _aware_datetime(window.get("start_asia_shanghai"), "window_start")
    end = _aware_datetime(window.get("end_asia_shanghai"), "window_end")
    _require(start <= observed <= end, "observation_outside_window")
    lag = (observed.astimezone(timezone.utc) - exchange.astimezone(timezone.utc)).total_seconds()
    _require(lag >= 0, "exchange_timestamp_in_future")
    _require(lag <= window.get("maximum_source_lag_seconds"), "quote_stale")

    ask = _positive_number(observation.get("ask_price"), "ask_price")
    bid_value = observation.get("bid_price")
    if bid_value is not None:
        bid = _positive_number(bid_value, "bid_price")
        _require(ask >= bid, "crossed_quote_invalid")
    if observation.get("last_price") is not None:
        _positive_number(observation["last_price"], "last_price")


def validate_receive_time(
    protocol: dict[str, Any], observation: dict[str, Any], received_at: datetime
) -> None:
    """Pure helper; production supplies received_at from the local clock."""
    _require(received_at.utcoffset() is not None, "received_at_timezone_missing")
    window = protocol.get("observation_window", {})
    start = _aware_datetime(window.get("start_asia_shanghai"), "window_start")
    end = _aware_datetime(window.get("end_asia_shanghai"), "window_end")
    observed = _aware_datetime(observation.get("observed_at"), "observed_at")
    received = received_at.astimezone(timezone.utc)
    _require(
        start.astimezone(timezone.utc) <= received <= end.astimezone(timezone.utc),
        "actual_receive_time_outside_window",
    )
    receive_lag = (received - observed.astimezone(timezone.utc)).total_seconds()
    _require(receive_lag >= 0, "observed_at_after_actual_receive")
    _require(
        receive_lag <= window.get("maximum_source_lag_seconds"),
        "actual_receive_too_late_no_backfill",
    )


def _require_source_armed(
    protocol: dict[str, Any], matrix: dict[str, Any]
) -> None:
    policy = protocol.get("quote_source_policy", {})
    background = protocol.get("background_process", {})
    rows = matrix.get("sources")
    sources = {
        row.get("id"): row
        for row in rows
        if isinstance(rows, list) and isinstance(row, dict) and row.get("id")
    } if isinstance(rows, list) else {}
    source = sources.get(policy.get("selected_source_id"), {})
    _require(
        policy.get("capture_armed") is True
        and policy.get("selected_source_status") == "verified_live"
        and background.get("armed") is True
        and source.get("decision") == "usable_for_live_observation"
        and source.get("actual_access") == "verified_live_quote",
        "source_not_verified_live",
    )


def capture_observation(root: Path) -> str:
    """Production write path: frozen files, fixed ignored paths, actual local time."""
    root = root.resolve()
    protocol = _read_json(_safe_path(root, PROTOCOL_PATH))
    matrix = _read_json(_safe_path(root, MATRIX_PATH))
    _require_source_armed(protocol, matrix)
    observation = _read_json(_safe_path(root, OBSERVATION_INBOX_PATH))
    validate_observation(protocol, matrix, observation)
    received_at = datetime.now(timezone.utc)
    validate_receive_time(protocol, observation, received_at)

    source_payload = _safe_path(root, SOURCE_PAYLOAD_PATH)
    try:
        payload_bytes = source_payload.stat().st_size
    except OSError as exc:
        raise PreflightError(f"source_payload_unreadable:{exc}") from None
    _require(
        0 < payload_bytes <= MAX_SOURCE_PAYLOAD_BYTES,
        "source_payload_size_invalid",
    )
    payload_sha256 = _digest(source_payload)
    _require(
        observation["source_payload_bytes"] == payload_bytes
        and observation["source_payload_sha256"] == payload_sha256,
        "source_payload_binding_mismatch",
    )
    origin = {
        "schema": "regis.m22.raw_quote_origin.v1",
        "received_at_utc": received_at.isoformat(),
        "observation": observation,
        "source_payload": {
            "path": SOURCE_PAYLOAD_PATH,
            "bytes": payload_bytes,
            "sha256": payload_sha256,
        },
    }
    return write_once_json(_safe_path(root, RAW_OBSERVATION_PATH), origin)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--write-summary", action="store_true")
    parser.add_argument("--capture-observation", action="store_true")
    args = parser.parse_args(argv)
    try:
        root = Path(args.root).resolve()
        summary = build_preflight(root)
        write_state = "not_requested"
        if args.write_summary:
            write_state = write_once_json(_safe_path(root, SUMMARY_PATH), summary)
        if args.capture_observation:
            capture_observation(root)
        print(
            json.dumps(
                {
                    "status": summary["status"],
                    "engineering_ready": summary["engineering_ready"],
                    "live_source_ready": summary["live_source_ready"],
                    "capture_armed": summary["capture_armed"],
                    "summary_write": write_state,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (PreflightError, OSError, TypeError) as exc:
        print(f"m22_preflight: {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
