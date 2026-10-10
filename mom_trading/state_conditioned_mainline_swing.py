"""Pure candidate selection for the first historically testable routed playbook."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = PROJECT_ROOT / "research/STATE_CONDITIONED_MAINLINE_SWING_V1_PROTOCOL.json"
PROTOCOL_SHA256 = "990e0cdad9196d9be07de8d70c787e9125c794ead0b96df166121d5526ff77b3"
AMENDMENT_1_PATH = PROJECT_ROOT / "research/STATE_CONDITIONED_MAINLINE_SWING_V1_PROTOCOL_AMENDMENT_1.json"
AMENDMENT_1_SHA256 = "2b9a853755043bb61927319868beb5e1d32420e49ed749978cd2082e4f2bd6fc"
AMENDMENT_2_PATH = PROJECT_ROOT / "research/STATE_CONDITIONED_MAINLINE_SWING_V1_PROTOCOL_AMENDMENT_2.json"
AMENDMENT_2_SHA256 = "90ed8e30ae004aa57fa94b0547a136403ad4ea36d304b5db78dd1553093f6900"
AMENDMENT_3_PATH = PROJECT_ROOT / "research/STATE_CONDITIONED_MAINLINE_SWING_V1_PROTOCOL_AMENDMENT_3.json"
AMENDMENT_3_SHA256 = "4ca8a7b13139a769965d5b84c49ceec9c45038d4467d39e14f04748296802bed"
AMENDMENT_4_PATH = PROJECT_ROOT / "research/STATE_CONDITIONED_MAINLINE_SWING_V1_PROTOCOL_AMENDMENT_4.json"
AMENDMENT_4_SHA256 = "13e65de1754d99c12c38fe6c47fc4af2d2f3f0d014e65dc843cdd7c17fbba8cf"
AMENDMENT_5_PATH = PROJECT_ROOT / "research/STATE_CONDITIONED_MAINLINE_SWING_V1_PROTOCOL_AMENDMENT_5.json"
AMENDMENT_5_SHA256 = "adcf7ba169c55fc1c03236c9c99ffaca61fd830b73db5dd18c139445169d4521"

_ROUTER = {
    "UNKNOWN": {"target_exposure_pct": 0, "maximum_new_trades": 0},
    "RISK_OFF": {"target_exposure_pct": 20, "maximum_new_trades": 0},
    "STABILIZING": {"target_exposure_pct": 40, "maximum_new_trades": 1},
    "RISK_ON": {"target_exposure_pct": 80, "maximum_new_trades": 2},
    "OVERHEATED": {"target_exposure_pct": 40, "maximum_new_trades": 0},
}
_REQUIRED_COLUMNS = {
    "stable_instrument_id", "ts_code", "code", "trade_date", "close", "amount",
    "median_amount20", "adv20", "pct_chg", "is_st", "trade_status", "adj_close",
    "sma20", "ret20", "atr14", "float_mv", "board", "adjustment_trusted",
    "history_sessions", "industry_code", "industry_name",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_protocol_chain(*, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    errors: list[str] = []
    paths = (
        (PROTOCOL_PATH, PROTOCOL_SHA256),
        (AMENDMENT_1_PATH, AMENDMENT_1_SHA256),
        (AMENDMENT_2_PATH, AMENDMENT_2_SHA256),
        (AMENDMENT_3_PATH, AMENDMENT_3_SHA256),
        (AMENDMENT_4_PATH, AMENDMENT_4_SHA256),
        (AMENDMENT_5_PATH, AMENDMENT_5_SHA256),
    )
    documents: list[dict[str, Any]] = []
    for path, expected in paths:
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
    if documents[2].get("status") != (
        "sector_constituent_and_stock_entry_scopes_disambiguated_before_candidate_or_outcome_run"
    ):
        errors.append("amendment_2_status_invalid")
    if (documents[3].get("predecessor") or {}).get("sha256") != AMENDMENT_2_SHA256:
        errors.append("amendment_3_predecessor_invalid")
    if documents[3].get("status") != (
        "exposure_ceiling_and_exit_priority_disambiguated_before_real_candidate_or_outcome_run"
    ):
        errors.append("amendment_3_status_invalid")
    if (documents[4].get("predecessor") or {}).get("sha256") != AMENDMENT_3_SHA256:
        errors.append("amendment_4_predecessor_invalid")
    if documents[4].get("status") != (
        "account_slots_order_prices_and_pending_order_semantics_frozen_before_real_candidate_or_outcome_run"
    ):
        errors.append("amendment_4_status_invalid")
    if (documents[5].get("predecessor") or {}).get("sha256") != AMENDMENT_4_SHA256:
        errors.append("amendment_5_predecessor_invalid")
    if documents[5].get("status") != (
        "performance_metric_and_primary_gate_formulas_frozen_before_real_candidate_or_outcome_run"
    ):
        errors.append("amendment_5_status_invalid")
    return {"valid": not errors, "errors": errors}


def _percentile(values: pd.Series, *, smaller_is_better: bool = False) -> pd.Series:
    if len(values) == 1:
        result = pd.Series(1.0, index=values.index, dtype="float64")
    else:
        result = values.rank(method="average", ascending=True).sub(1.0).div(len(values) - 1.0)
    return 1.0 - result if smaller_is_better else result


def _finite_series(frame: pd.DataFrame, names: list[str]) -> pd.Series:
    valid = pd.Series(True, index=frame.index)
    for name in names:
        numeric = pd.to_numeric(frame[name], errors="coerce")
        valid &= numeric.notna() & numeric.map(math.isfinite)
    return valid


def _sector_constituents(frame: pd.DataFrame) -> pd.DataFrame:
    complete = _finite_series(
        frame, ["adj_close", "sma20", "ret20", "median_amount20", "float_mv"],
    )
    mask = (
        frame["board"].isin(["MAIN_SH", "MAIN_SZ", "GEM"])
        & frame["trade_status"].eq(1)
        & frame["is_st"].eq(0)
        & frame["adjustment_trusted"].eq(True)
        & frame["history_sessions"].ge(21)
        & frame["industry_code"].notna()
        & frame["industry_name"].notna()
        & frame["median_amount20"].ge(50_000_000)
        & frame["float_mv"].ge(2_000_000_000)
        & complete
    )
    return frame.loc[mask].copy()


def _confirmed_sectors(constituents: pd.DataFrame) -> pd.DataFrame:
    if constituents.empty:
        return pd.DataFrame(columns=[
            "industry_code", "industry_name", "constituent_count",
            "sector_momentum", "breadth",
        ])
    grouped = constituents.groupby(["industry_code", "industry_name"], observed=True, sort=True)
    sectors = grouped.agg(
        constituent_count=("stable_instrument_id", "size"),
        sector_momentum=("ret20", "median"),
    ).reset_index()
    breadth = grouped.apply(
        lambda group: float(group["adj_close"].gt(group["sma20"]).mean()),
        include_groups=False,
    ).rename("breadth").reset_index()
    sectors = sectors.merge(breadth, on=["industry_code", "industry_name"], validate="one_to_one")
    sectors = sectors.loc[
        sectors["constituent_count"].ge(12)
        & sectors["sector_momentum"].gt(0.0)
        & sectors["breadth"].ge(0.60)
    ].copy()
    sectors.sort_values(
        ["sector_momentum", "breadth", "industry_code"],
        ascending=[False, False, True], inplace=True,
    )
    return sectors.head(2).reset_index(drop=True)


def build_daily_candidates(
    market_state: str,
    daily_rows: pd.DataFrame,
    *,
    route_mode: str = "state_routed",
    excluded_instrument_ids: set[str] | None = None,
    available_new_slots: int | None = None,
) -> dict[str, Any]:
    """Return a frozen candidate set without reading any future price row."""
    if route_mode not in {"state_routed", "fixed_exposure_control"}:
        raise ValueError("route_mode must be state_routed or fixed_exposure_control")
    missing = sorted(_REQUIRED_COLUMNS - set(daily_rows.columns))
    if missing:
        return {
            "status": "input_incomplete", "errors": [f"missing_column:{name}" for name in missing],
            "selected": [], "candidate_count": 0, "confirmed_sectors": [],
            "returns_or_labels_read": False,
        }
    protocol = verify_protocol_chain()
    if not protocol["valid"]:
        return {
            "status": "protocol_invalid", "errors": protocol["errors"],
            "selected": [], "candidate_count": 0, "confirmed_sectors": [],
            "returns_or_labels_read": False,
        }
    state = str(market_state or "UNKNOWN")
    if route_mode == "fixed_exposure_control":
        route = {"target_exposure_pct": 80, "maximum_new_trades": 2}
    else:
        route = _ROUTER.get(state, _ROUTER["UNKNOWN"])
    constituents = _sector_constituents(daily_rows)
    sectors = _confirmed_sectors(constituents)
    entries = constituents.loc[constituents["industry_code"].isin(sectors.get("industry_code", []))].copy()
    if not entries.empty:
        entries = entries.merge(
            sectors[["industry_code", "industry_name", "sector_momentum", "breadth", "constituent_count"]],
            on=["industry_code", "industry_name"], how="inner", validate="many_to_one",
        )
        entries = entries.loc[
            entries["amount"].ge(50_000_000)
            & entries["float_mv"].le(80_000_000_000)
            & entries["pct_chg"].between(0.5, 7.0, inclusive="both")
            & entries["adj_close"].gt(entries["sma20"])
            & entries["ret20"].gt(entries["sector_momentum"])
            & entries["atr14"].notna()
        ].copy()
    if not entries.empty:
        sector_scores = _percentile(sectors.set_index("industry_code")["sector_momentum"])
        entries["component_sector_momentum"] = entries["industry_code"].map(sector_scores)
        entries["component_stock_return"] = _percentile(entries["ret20"])
        entries["component_liquidity"] = _percentile(entries["median_amount20"])
        entries["component_small_cap"] = _percentile(entries["float_mv"], smaller_is_better=True)
        entries["score"] = 100.0 * (
            0.40 * entries["component_sector_momentum"]
            + 0.30 * entries["component_stock_return"]
            + 0.20 * entries["component_liquidity"]
            + 0.10 * entries["component_small_cap"]
        )
        entries.sort_values(
            ["score", "amount", "stable_instrument_id"],
            ascending=[False, False, True], inplace=True,
        )
    excluded = {str(value) for value in (excluded_instrument_ids or set())}
    if excluded and not entries.empty:
        entries = entries.loc[~entries["stable_instrument_id"].astype(str).isin(excluded)].copy()
    maximum = int(route["maximum_new_trades"])
    if available_new_slots is not None:
        maximum = min(maximum, max(0, int(available_new_slots)))
    selected = entries.head(maximum) if maximum > 0 else entries.iloc[:0]
    fields = [
        "stable_instrument_id", "ts_code", "code", "trade_date", "close", "adv20", "atr14",
        "industry_code", "industry_name", "sector_momentum", "breadth", "constituent_count",
        "ret20", "median_amount20", "float_mv", "pct_chg", "score",
        "component_sector_momentum", "component_stock_return", "component_liquidity",
        "component_small_cap",
    ]
    selected_rows = []
    for row in selected[fields].to_dict("records") if not selected.empty else []:
        selected_rows.append({
            key: (value.isoformat() if isinstance(value, pd.Timestamp) else value)
            for key, value in row.items()
        })
    return {
        "schema": "state_conditioned_mainline_candidates.v1",
        "status": "candidate_research_ready" if maximum > 0 else "route_no_new_trade",
        "route_mode": route_mode, "market_state": state,
        "target_exposure_pct": int(route["target_exposure_pct"]),
        "maximum_new_trades": maximum,
        "confirmed_sectors": sectors.to_dict("records"),
        "sector_constituent_count": int(len(constituents)),
        "candidate_count": int(len(entries)), "selected_count": len(selected_rows),
        "selected": selected_rows,
        "playbook_id": "state_conditioned_mainline_swing_v1",
        "named_trader_identity_used": False,
        "unadmitted_factor_used": False,
        "returns_or_labels_read": False,
        "authority": {
            "research_only": True, "changes_live_score": False,
            "changes_live_position_or_quantity": False, "generates_alert": False,
            "production_strategy": False, "automatic_trading": False,
        },
    }


__all__ = ["build_daily_candidates", "verify_protocol_chain"]
