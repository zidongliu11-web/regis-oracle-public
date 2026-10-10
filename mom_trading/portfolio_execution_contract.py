"""Frozen source admission for the research-only daily portfolio transition.

This replaces the missing legacy daily gate, without reconstructing its files.
It does not grant data, strategy, live-trading or exchange-fill qualification.
The manifest must be deliberately frozen after source changes; runtime never
writes or repairs it. Source hashes are reproducibility checks, not signatures.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

CONTRACT_RELATIVE_PATH = "research/DAILY_PORTFOLIO_EXECUTION_CONTRACT_20261006.json"
SOURCE_PATHS = {
    "daily_execution": "mom_trading/unified_regime_route_replay.py",
    "verified_wrapper": "mom_trading/verified_portfolio_execution.py",
    "contract_verifier": "mom_trading/portfolio_execution_contract.py",
    "strict_strategy": "mom_trading/strict_joint_strategy_replay.py",
    "shared_rules": "quantqc/ashare.py",
    "shared_costs": "quantqc/costs.py",
    "causal_serialization": "mom_trading/market_state_daily.py",
    "causal_observation": "mom_trading/intraday_market_emotion_forward.py",
    "causal_candidate": "mom_trading/mainline_start_candidates.py",
    "causal_shadow": "mom_trading/market_sector_route_shadow.py",
}
EXECUTION_FLAGS = (
    "sell_before_buy", "T_plus_1_share_inventory", "integer_share_state",
    "odd_lot_only_on_full_liquidation", "cash_includes_costs",
    "lagged_ADV_capacity", "market_impact_cost", "no_backfill",
    "deterministic_state_hash",
)
AUTHORITY_FLAGS = (
    "modify_live_score", "modify_live_exposure", "modify_live_trade_count",
    "emit_buy_or_sell_signal", "run_strategy_returns", "admit_strategy",
    "production", "automatic_trading", "stable_profit_claim",
)
MODEL_BOUNDARY = {
    "session": "opening_auction",
    "fill": "daily_open_proxy_lagged_ADV_cap_limit_event_no_fill",
    "full_exchange_matching_claimed": False,
    "opening_auction_queue_or_volume_proven": False,
    "continuous_auction_price_cage_applied": False,
    "exit_source_packet_identity_independently_verified": False,
}
OUTCOME_BOUNDARY = {
    "returns_or_labels_read": False,
    "data_source_qualified": False,
    "financial_admitted": False,
    "full_goal_complete": False,
}
LEGACY_ORIGINALS = [
    "research/UNIFIED_REGIME_ROUTE_PORTFOLIO_BACKTEST_PROTOCOL_V1_AMENDMENT_5.json",
    "research/UNIFIED_REGIME_ROUTE_PORTFOLIO_BACKTEST_PROTOCOL_V1_AMENDMENT_14.json",
]


def _exact_flags(value: object, names: tuple[str, ...], expected: bool) -> bool:
    return isinstance(value, dict) and set(value) == set(names) and all(
        value[name] is expected for name in names
    )


def _exact_boundary(value: object, expected: dict[str, Any]) -> bool:
    # bool is an int subclass: equality alone would also accept 0/1.
    return isinstance(value, dict) and set(value) == set(expected) and all(
        type(value[name]) is type(wanted) and value[name] == wanted
        for name, wanted in expected.items()
    )


def verify_daily_execution_contract(
    *, project_root: Path, path: Path,
) -> dict[str, Any]:
    """Read the fixed contract and its exact source set, rejecting any gap."""
    errors: list[str] = []
    digest: str | None = None
    contract: dict[str, Any] = {}
    try:
        root = project_root.resolve()
        target = path.resolve()
        if target != (root / CONTRACT_RELATIVE_PATH).resolve():
            raise ValueError("daily_execution_contract_path_invalid")
        target.relative_to(root)
        raw = target.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("daily_execution_contract_object_invalid")
        contract = payload
        if (contract.get("schema") != "daily_portfolio_execution_contract.v1"
                or contract.get("contract_id") != "daily_portfolio_execution_20261006_e163"
                or contract.get("status") != "frozen_research_only_no_outcome_run"):
            errors.append("daily_execution_contract_identity_invalid")
        if contract.get("research_only") is not True:
            errors.append("daily_execution_research_only_invalid")
        if not _exact_flags(contract.get("execution_contract"), EXECUTION_FLAGS, True):
            errors.append("daily_execution_invariants_invalid")
        if not _exact_flags(contract.get("authority"), AUTHORITY_FLAGS, False):
            errors.append("daily_execution_authority_invalid")
        if not _exact_boundary(contract.get("execution_model"), MODEL_BOUNDARY):
            errors.append("daily_execution_model_boundary_invalid")
        if not _exact_boundary(contract.get("outcome_boundary"), OUTCOME_BOUNDARY):
            errors.append("daily_execution_outcome_boundary_invalid")
        if (contract.get("legacy_originals_missing_at_freeze") != LEGACY_ORIGINALS
                or contract.get("legacy_originals_restored") is not False):
            errors.append("daily_execution_legacy_boundary_invalid")
        bindings = contract.get("bindings")
        if not isinstance(bindings, dict) or set(bindings) != set(SOURCE_PATHS):
            errors.append("daily_execution_source_set_invalid")
            bindings = {}
        for name, relative in SOURCE_PATHS.items():
            binding = bindings.get(name)
            if (not isinstance(binding, dict) or set(binding) != {"path", "sha256"}
                    or binding.get("path") != relative):
                errors.append(f"daily_execution_source_identity_invalid:{name}")
                continue
            source = (root / relative).resolve()
            source.relative_to(root)
            actual = hashlib.sha256(source.read_bytes()).hexdigest()
            if actual != binding.get("sha256"):
                errors.append(f"daily_execution_source_hash_invalid:{name}")
    except (OSError, ValueError, TypeError) as exc:
        errors.append(f"daily_execution_contract_unreadable:{exc}")
    return {
        "valid": not errors, "errors": errors, "sha256": digest,
        "path": str(path), "contract": contract,
    }
