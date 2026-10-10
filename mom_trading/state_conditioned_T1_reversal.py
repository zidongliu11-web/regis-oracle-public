"""Pure same-close selector for the T+1 overreaction reversal hypothesis."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .state_conditioned_mainline_swing import (
    _confirmed_sectors,
    _percentile,
    _sector_constituents,
)


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = ROOT / "research/STATE_CONDITIONED_T1_REVERSAL_V1_PROTOCOL.json"
PROTOCOL_SHA256 = "0b7ec6724ed50aed8294b2010842fffb8e96bae1d36e9e36bc37aae6de534b0e"
AMENDMENT_1_PATH = ROOT / "research/STATE_CONDITIONED_T1_REVERSAL_V1_PROTOCOL_AMENDMENT_1.json"
AMENDMENT_1_SHA256 = "8b0e522f4b4832f8883e49e57ea35f87fec309192376f5e7a793bba05cab931b"
AMENDMENT_2_PATH = ROOT / "research/STATE_CONDITIONED_T1_REVERSAL_V1_PROTOCOL_AMENDMENT_2.json"
AMENDMENT_2_SHA256 = "6853ce9d5250e3bcaea73f65753434e416687ea881e90ccde9134869f5dd8bca"
AMENDMENT_3_PATH = ROOT / "research/STATE_CONDITIONED_T1_REVERSAL_V1_PROTOCOL_AMENDMENT_3.json"
AMENDMENT_3_SHA256 = "53e8e5f451cb7cdc13dec01d9eebf8dbab3443681c6b21e560f256289b0d2ffd"
JOINED_AUDIT_PATH = ROOT / "research/STATE_CONDITIONED_T1_REVERSAL_V1_JOINED_INPUT_AUDIT.json"
JOINED_AUDIT_SHA256 = "39f8663a0aa7e0dd8c16fea0b7c7e68839efe1904beb65ab9354a1f6acf1293b"
_REQUIRED = {
    "stable_instrument_id", "ts_code", "code", "trade_date", "close", "prev_close",
    "amount", "median_amount20", "adv20", "pct_chg", "turnover_rate", "is_st",
    "trade_status", "adj_close", "sma20", "ret20", "float_mv", "board",
    "adjustment_trusted", "factor_changed", "history_sessions", "limit_evaluable",
    "industry_code", "industry_name",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_protocol_chain() -> dict[str, Any]:
    errors: list[str] = []
    documents = []
    for path, expected in (
        (PROTOCOL_PATH, PROTOCOL_SHA256),
        (AMENDMENT_1_PATH, AMENDMENT_1_SHA256),
        (AMENDMENT_2_PATH, AMENDMENT_2_SHA256),
        (AMENDMENT_3_PATH, AMENDMENT_3_SHA256),
    ):
        try:
            if _sha256(path) != expected:
                errors.append(f"sha256_mismatch:{path.name}")
            documents.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"unreadable:{path.name}:{exc}")
            documents.append({})
    if (documents[1].get("predecessor") or {}).get("sha256") != PROTOCOL_SHA256:
        errors.append("amendment_1_predecessor_invalid")
    if (documents[2].get("predecessor") or {}).get("sha256") != AMENDMENT_1_SHA256:
        errors.append("amendment_2_predecessor_invalid")
    if (documents[3].get("predecessor") or {}).get("sha256") != AMENDMENT_2_SHA256:
        errors.append("amendment_3_predecessor_invalid")
    if documents[3].get("status") != (
        "metrics_cost_robustness_and_primary_gates_frozen_before_real_candidate_or_outcome_read"
    ):
        errors.append("amendment_3_status_invalid")
    try:
        if _sha256(JOINED_AUDIT_PATH) != JOINED_AUDIT_SHA256:
            errors.append("joined_input_audit_sha256_mismatch")
        audit = json.loads(JOINED_AUDIT_PATH.read_text(encoding="utf-8"))
        if audit.get("status") != "joined_input_integrity_ready":
            errors.append("joined_input_audit_not_ready")
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"joined_input_audit_unreadable:{exc}")
    return {"valid": not errors, "errors": sorted(set(errors))}


def build_daily_candidates(
    market_state: str, daily_rows: pd.DataFrame, *,
    route_mode: str = "state_routed",
    excluded_instrument_ids: set[str] | None = None,
    available_new_slots: int | None = None,
) -> dict[str, Any]:
    if route_mode not in {"state_routed", "fixed_exposure_control"}:
        raise ValueError("route_mode invalid")
    missing = sorted(_REQUIRED - set(daily_rows.columns))
    if missing:
        return {
            "status": "input_incomplete", "errors": [f"missing_column:{name}" for name in missing],
            "selected": [], "candidate_count": 0, "returns_or_labels_read": False,
        }
    protocol = verify_protocol_chain()
    if not protocol["valid"]:
        return {
            "status": "protocol_invalid", "errors": protocol["errors"],
            "selected": [], "candidate_count": 0, "returns_or_labels_read": False,
        }
    state = str(market_state or "UNKNOWN")
    route_allowed = route_mode == "fixed_exposure_control" or state == "STABILIZING"
    maximum = 2 if route_allowed else 0
    if available_new_slots is not None:
        maximum = min(maximum, max(0, int(available_new_slots)))

    constituents = _sector_constituents(daily_rows)
    sectors = _confirmed_sectors(constituents)
    working = constituents.loc[
        constituents["industry_code"].isin(sectors.get("industry_code", []))
    ].copy()
    if not working.empty:
        grouped = working.groupby("industry_code", observed=True, sort=True)
        working["sector_return_percentile"] = grouped["pct_chg"].rank(
            method="average", ascending=True, pct=True,
        )
        working["sector_turnover_percentile"] = grouped["turnover_rate"].rank(
            method="average", ascending=True, pct=True,
        )
        rate = np.where(working["board"].eq("GEM"), 0.20, 0.10)
        working["lower_limit"] = np.floor(
            working["prev_close"].to_numpy(dtype="float64") * (1.0 - rate) * 100.0
            + 0.500000001
        ) / 100.0
        finite = pd.Series(True, index=working.index)
        for column in (
            "close", "prev_close", "amount", "median_amount20", "float_mv",
            "pct_chg", "turnover_rate", "sector_return_percentile",
            "sector_turnover_percentile", "lower_limit",
        ):
            values = pd.to_numeric(working[column], errors="coerce")
            finite &= values.notna() & values.map(math.isfinite)
        eligible = working.loc[
            finite
            & working["pct_chg"].lt(0.0)
            & working["sector_return_percentile"].le(0.10)
            & working["sector_turnover_percentile"].ge(0.70)
            & working["amount"].ge(50_000_000)
            & working["median_amount20"].ge(50_000_000)
            & working["float_mv"].between(2_000_000_000, 80_000_000_000, inclusive="both")
            & working["limit_evaluable"].eq(True)
            & working["factor_changed"].eq(False)
            & working["close"].sub(working["lower_limit"]).abs().gt(0.0051)
        ].copy()
    else:
        eligible = working
    if not eligible.empty:
        eligible["component_reversal"] = 1.0 - eligible["sector_return_percentile"]
        eligible["component_turnover"] = eligible["sector_turnover_percentile"]
        eligible["component_liquidity"] = _percentile(eligible["median_amount20"])
        eligible["score"] = 100.0 * (
            0.50 * eligible["component_reversal"]
            + 0.30 * eligible["component_turnover"]
            + 0.20 * eligible["component_liquidity"]
        )
        eligible.sort_values(
            ["score", "amount", "stable_instrument_id"],
            ascending=[False, False, True], inplace=True,
        )
    excluded = {str(value) for value in (excluded_instrument_ids or set())}
    if excluded and not eligible.empty:
        eligible = eligible.loc[
            ~eligible["stable_instrument_id"].astype(str).isin(excluded)
        ].copy()
    selected = eligible.head(maximum) if maximum > 0 else eligible.iloc[:0]
    fields = [
        "stable_instrument_id", "ts_code", "code", "trade_date", "close", "adv20",
        "industry_code", "industry_name", "pct_chg", "turnover_rate",
        "sector_return_percentile", "sector_turnover_percentile",
        "median_amount20", "float_mv", "score", "component_reversal",
        "component_turnover", "component_liquidity",
    ]
    selected_rows = []
    for row in selected[fields].to_dict("records") if not selected.empty else []:
        selected_rows.append({
            key: (value.isoformat() if isinstance(value, pd.Timestamp) else value)
            for key, value in row.items()
        })
    return {
        "schema": "state_conditioned_T1_reversal_candidates.v1",
        "status": "candidate_research_ready" if maximum > 0 else "route_no_new_trade",
        "route_mode": route_mode, "market_state": state,
        "target_exposure_pct": 40 if route_allowed else 0,
        "maximum_new_trades": maximum,
        "confirmed_sectors": sectors.to_dict("records"),
        "sector_constituent_count": len(constituents),
        "candidate_count": len(eligible), "selected_count": len(selected_rows),
        "selected": selected_rows,
        "playbook_id": "state_conditioned_T1_overreaction_reversal_v1",
        "named_trader_identity_used": False, "unadmitted_factor_used": False,
        "returns_or_labels_read": False,
        "authority": {
            "research_only": True, "changes_live_score": False,
            "changes_live_position_or_quantity": False,
            "generates_buy_or_sell_alert": False, "production_strategy": False,
            "automatic_trading": False, "stable_profit_claim": False,
        },
    }


__all__ = ["build_daily_candidates", "verify_protocol_chain"]
