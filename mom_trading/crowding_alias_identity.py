"""Use a documented continuous issuer's old symbol as a frozen-engine rule key.

Vendor canonical code, actual exchange code and the legacy rule lookup key are
three distinct fields. Only the latter is normalized in a private execution
view. No price, share, order date, factor value or cohort member is substituted.
This narrow adapter does not classify arbitrary 302-prefixed securities.
"""
from __future__ import annotations

from copy import copy
import pandas as pd

CANONICAL = "302132"
RULE_KEY = "300114"
STABLE_ID = "SZSE_300114_302132_CONTINUOUS_ISSUER"
ALIAS_SHA = "dc7c9d5cb4aa766e4574ccf1e6d0806fb64a4fe1411acfbce20215cb4f5b8388"
SPEC = {
    "schema": "crowding_continuous_issuer_alias.v1",
    "canonical_vendor_code": CANONICAL, "legacy_engine_rule_key": RULE_KEY,
    "effective_date": "2025-02-17", "board_before_and_after": "GEM",
    "share_quantity_and_tax_holding_clock_reset": False,
    "scope": "2021_2025_documented_continuous_issuer_only",
    "signal_formula_membership_or_entry_exit_rule_changed": False,
    "historical_identity_reconstruction": True, "new_blind_trial": False,
    "production_eligible": False, "generates_alert": False,
    "implementation_source": "https://disc.static.szse.cn/download/disc/disk03/finalpage/2025-02-15/cedb693a-f5ee-4463-9682-ea33d406b569.PDF",
}


def alias_execution_view(market, canonical: pd.DataFrame, aliases: pd.DataFrame):
    """Require the immutable panel's date-qualified alias, not a current prefix."""
    records = aliases.fillna("").to_dict("records")
    spans = sorted((r["exchange_code"], r["valid_from"], r["valid_through"]) for r in records)
    if (len(records) != 2 or spans != [("300114.SZ", "2010-08-27", "2025-02-14"), ("302132.SZ", "2025-02-17", "")]
            or any(r["stable_instrument_id"] != STABLE_ID or r["canonical_ts_code"] != "302132.SZ"
                   or r["known_from"] != "2025-02-07" for r in records)):
        raise ValueError("continuous_issuer_alias_contract_mismatch")
    frame = canonical.copy()
    frame["day"] = pd.to_datetime(frame.trade_date).dt.strftime("%Y-%m-%d")
    if frame.day.duplicated().any() or set(frame.ts_code) != {"302132.SZ"}:
        raise ValueError("continuous_issuer_panel_identity_invalid")
    rows = {r["day"]: r for r in frame.to_dict("records")}
    exchange_codes = {}
    for day, source in market.rows.items():
        row = rows.get(day)
        expected = "300114.SZ" if day < "2025-02-17" else "302132.SZ"
        matches = [r for r in records if r["valid_from"] <= day and (not r["valid_through"] or day <= r["valid_through"])]
        if (not "2021-01-01" <= day <= "2025-12-31" or row is None or len(matches) != 1
                or source["code"] != CANONICAL or source["stable_instrument_id"] != STABLE_ID
                or row["stable_instrument_id"] != STABLE_ID or row["exchange_code"] != expected
                or matches[0]["exchange_code"] != expected or row["board"] != "GEM"
                or row["identity_provenance"] != "official_effective_dated_alias_table"
                or row["identity_source_sha256"] != ALIAS_SHA):
            raise ValueError("continuous_issuer_dated_identity_mismatch")
        for field in ("open", "high", "low", "close", "trade_status", "is_st"):
            if not (pd.isna(source[field]) and pd.isna(row[field])) and source[field] != row[field]:
                raise ValueError("continuous_issuer_raw_market_mismatch")
        if day == "2025-02-17" and source["no_price_limit"] is not False:
            raise ValueError("code_transition_is_not_a_new_listing")
        exchange_codes[day] = expected
    view = copy(market)
    view.rows = {day: {**row, "code": RULE_KEY} for day, row in market.rows.items()}
    return view, exchange_codes


def restore_exchange_identity(event: dict, signal: dict, exchange_codes: dict) -> dict:
    """The rule key must never be presented as a post-transition order symbol."""
    if event["code"] != RULE_KEY or signal["code"] != CANONICAL:
        raise ValueError("continuous_issuer_replay_identity_mismatch")
    result = {**event, "code": CANONICAL, "quintile": signal["quintile"],
              "execution_identity_schema": SPEC["schema"], "legacy_engine_rule_key": RULE_KEY,
              "signal_exchange_code": exchange_codes[signal["signal_day"]],
              "stable_instrument_id": STABLE_ID,
              "execution_events": [{**r, "exchange_code": exchange_codes[r["day"]]} for r in event["execution_events"]]}
    return result
