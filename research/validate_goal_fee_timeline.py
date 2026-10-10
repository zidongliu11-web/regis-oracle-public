"""Check historical rule evidence and unsafe counterexamples, without price inputs."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "research/GOAL_HISTORICAL_FEE_TIMELINE_20261002.json"


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_table(table):
    for flag in ("full_fee_gate", "real_N5_protocol_created", "production_cost_module_changed",
                 "fees_applied_to_market_data", "economic_run", "model_admitted"):
        require(table[flag] is False, "Prerequisite evidence cannot admit economics")
    sources = {source["id"]: source for source in table["sources"]}
    require(len(sources) == len(table["sources"]), "Duplicate source identity")
    for source in sources.values():
        if source.get("raw_path"):
            require(digest(ROOT / source["raw_path"]) == source["raw_sha256"],
                    "Source bytes drifted")
    for reference in table["source_receipts"].values():
        require(digest(ROOT / reference["path"]) == reference["sha256"],
                "Evidence receipt drifted")
    for path, sha in table["legacy_frozen_file_sha256"].items():
        require(digest(ROOT / path) == sha, "Original module or protocol changed")
    rules = {rule["id"]: rule for rule in table["rules"]}
    require(len(rules) == len(table["rules"]) == 15, "Rule identity mismatch")
    for rule in rules.values():
        require(rule["economic_consumer_enabled"] is False, "Premature consumer admission")
        require(rule["actual_broker_terms_verified"] is False, "No broker-account evidence")
        start, end = rule["effective_from_inclusive"], rule["effective_until_exclusive"]
        if start is not None:
            date.fromisoformat(start)
        if end is not None:
            date.fromisoformat(end)
        require(start is None or end is None or start < end, "Invalid interval")
        if rule["rate_fraction"] is not None:
            require(Decimal("0") <= Decimal(rule["rate_fraction"]) < Decimal("1"),
                    "Wrong percent/per-mille fraction")
        if rule["evidence_status"] == "unknown":
            require(rule["rate_fraction"] is None and start is None and end is None,
                    "Unknown cannot become zero or backward-filled interval")
            require(rule["charge_side"] is None, "Unknown charge direction cannot be prefilled")
        for source_id in rule["source_ids"]:
            require(source_id in sources and sources[source_id].get("raw_sha256"),
                    "Missing primary native byte identity")
            require(sources[source_id]["grade"] in {"first_party_body_read", "first_party_body_captured"},
                    "Failure or maintenance page used as fee evidence")
    scope_rule = rules["etf_stamp_scope_2022"]
    require(scope_rule["asset"] == "ETF_510300" and scope_rule["venue"] == "SSE"
            and scope_rule["component"] == "stamp"
            and scope_rule["effective_from_inclusive"] == "2022-07-01"
            and scope_rule["rate_fraction"] == "0" and scope_rule["charge_side"] == "not_applicable",
            "ETF statutory-scope scalar cannot cover wrong asset, period or tax")
    require(scope_rule["source_ids"] == ["STAMP_SCOPE_LAW2022", "ETF_IDENTITY2022"]
            and scope_rule["basis_kind"] == "calculation_scalar_from_securities_transaction_tax_scope_exclusion"
            and scope_rule["evidence_status"] == "scope_exclusion_inference_independently_reviewed"
            and scope_rule["actual_instrument_class_at_trade_required"] is True
            and scope_rule["statutory_zero_rate_claim"] is False
            and scope_rule["explicit_ETF_exemption_claim"] is False,
            "ETF scope inference is conditional, not statutory zero or explicit exemption")
    identity = sources["ETF_IDENTITY2022"]
    require(identity["fund_code"] == "510300" and identity["instrument_class"] == "fund_units"
            and identity["historical_identity_interval_certified"] is False,
            "Single identity notice cannot certify every historical trade")
    assumptions = table["frozen_research_assumptions"]
    require(assumptions["handling_regulatory_included"] is True,
            "Do not double-add commission components")
    require(assumptions["actual_broker_terms_verified"] is False
            and assumptions["accounts_read"] == 0, "Broker assumption presented as fact")
    require(assumptions["no_minimum_on_unfilled"] is True
            and assumptions["no_duplicate_minimum_per_fill"] is True,
            "Commission minimum cannot be charged twice or on unfilled orders")
    lot = table["dividend_lot_clock"]
    require(lot["production_stock_dividend_enabled"] is False
            and lot["M24_current_stock_dividend_rejection_retained"] is True,
            "Unsettled stock lots cannot enter frozen M24")
    require([band["fraction"] for band in lot["rate_bands"]] == ["0.20", "0.10", "0"],
            "2012 withholding/rates must not overwrite 2015 policy")
    require(lot["policy_source_ids"] == ["DIV2015", "DIV2012_OPERATION"],
            "2012 operating rules require the 2015 incorporation and supersession")
    require(lot["period_unit"] == "natural_calendar_anniversary"
            and lot["quantity_scope"] == "securities_account_end_of_day_settlement_net_shares",
            "Trading sessions or individual fills cannot replace statutory lot clocks")
    return rules


def known_boundary_rate(rules, component, venue, asset, day, as_of_date):
    """Public rule scalar only; no complete trading-cost or continuity admission."""
    date.fromisoformat(day)
    require(day <= as_of_date, "No rule extrapolation beyond source snapshot")
    candidates = []
    for rule in rules.values():
        start, end = rule["effective_from_inclusive"], rule["effective_until_exclusive"]
        if rule["component"] != component or rule["venue"] not in {venue, "SSE_SZSE"} or rule["asset"] != asset:
            continue
        # Unknown starts may never silently cover every historical date.
        if start is not None and start <= day and (end is None or day < end):
            candidates.append(rule)
    require(len(candidates) == 1 and candidates[0]["rate_fraction"] is not None,
            "No unique source-bound scalar at this date; full costs remain unadmitted")
    return candidates[0]["rate_fraction"]


def expect_rejection(mutated, name):
    try:
        validate_table(mutated)
    except ValueError:
        return name
    raise ValueError("Unsafe counterexample accepted: " + name)


def main():
    table = json.loads(TABLE.read_text(encoding="utf-8"))
    rules = validate_table(table)
    as_of_date = table["evidence_as_of_utc"][:10]
    cases = [
        ("stamp", "SSE", "stock", "2023-08-25", "0.001"),
        ("stamp", "SSE", "stock", "2023-08-28", "0.0005"),
        ("handling", "SSE", "stock", "2018-01-02", "0.0000487"),
        ("handling", "SSE", "stock", "2023-08-25", "0.0000487"),
        ("handling", "SSE", "stock", "2023-08-28", "0.0000341"),
        ("handling", "SZSE", "stock", "2023-08-28", "0.0000341"),
        ("handling", "SSE", "ETF_510300", "2018-01-02", "0.000045"),
        ("handling", "SSE", "ETF_510300", "2021-07-16", "0.000045"),
        ("handling", "SSE", "ETF_510300", "2021-07-19", "0.00004"),
        ("stamp", "SSE", "ETF_510300", "2022-07-01", "0"),
        ("stamp", "SSE", "ETF_510300", "2026-09-28", "0"),
    ]
    for component, venue, asset, day, expected in cases:
        require(known_boundary_rate(rules, component, venue, asset, day, as_of_date) == expected,
                "Historical boundary/asset route mismatch")
    missing_rejected = 0
    for component, venue, asset, day in [
        ("handling", "SZSE", "stock", "2018-01-02"),
        ("transfer", "SSE", "stock", "2022-04-29"),
        ("stamp", "SSE", "ETF_510300", "2022-06-30"),
        ("transfer", "SSE", "ETF_510300", "2026-09-28"),
        ("stamp", "SSE", "stock", "2027-01-04"),
        ("stamp", "SSE", "ETF_510300", "2027-01-04"),
    ]:
        try:
            known_boundary_rate(rules, component, venue, asset, day, as_of_date)
        except ValueError:
            missing_rejected += 1
        else:
            raise ValueError("Unknown fee path was admitted")
    counterexamples = []
    mutated = deepcopy(table)
    next(row for row in mutated["rules"] if row["id"] == "etf_stamp_unknown")["rate_fraction"] = "0"
    counterexamples.append(expect_rejection(mutated, "unknown_ETF_stamp_zero"))
    mutated = deepcopy(table)
    mutated["frozen_research_assumptions"]["handling_regulatory_included"] = False
    counterexamples.append(expect_rejection(mutated, "duplicate_commission_components"))
    mutated = deepcopy(table)
    mutated["full_fee_gate"] = True
    counterexamples.append(expect_rejection(mutated, "premature_full_fee_gate"))
    mutated = deepcopy(table)
    mutated["dividend_lot_clock"]["rate_bands"][2]["fraction"] = "0.05"
    counterexamples.append(expect_rejection(mutated, "superseded_2012_dividend_band"))
    mutated = deepcopy(table)
    mutated["dividend_lot_clock"]["period_unit"] = "trading_sessions"
    counterexamples.append(expect_rejection(mutated, "twenty_trading_day_month"))
    mutated = deepcopy(table)
    next(row for row in mutated["rules"] if row["id"] == "etf_transfer_unknown")["charge_side"] = "both"
    counterexamples.append(expect_rejection(mutated, "unknown_charge_direction_assumed"))
    for field, value, label in (
            ("effective_from_inclusive", "2018-01-01", "current_statute_backfilled_before_effective_date"),
            ("asset", "stock", "fund_scope_applied_to_underlying_stock"),
            ("explicit_ETF_exemption_claim", True, "scope_inference_misreported_as_literal_exemption"),
            ("actual_instrument_class_at_trade_required", False, "identity_notice_misreported_as_all_trade_identity")):
        mutated = deepcopy(table)
        next(row for row in mutated["rules"] if row["id"] == "etf_stamp_scope_2022")[field] = value
        counterexamples.append(expect_rejection(mutated, label))
    print(json.dumps({"source_and_frozen_identities_match": True, "rule_rows": 15,
                      "boundary_and_asset_cases": len(cases), "unknown_paths_rejected": missing_rejected,
                      "unsafe_counterexamples_rejected": counterexamples,
                      "table_sha256": digest(TABLE), "economic_run": False, "full_fee_gate": False}))


if __name__ == "__main__":
    main()
