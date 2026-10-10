"""Bounded, read-only projection of the existing feasibility registrations."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from research.pb01_pipeline import BOUNDARY, _object, project_path, reference

FIRST = "research/GOAL_FIRST_FACTOR_REGISTRATION_20261002.json"
BOOKS = "research/GOAL_PARALLEL_PLAYBOOK_REGISTRATION_20261002.json"
DECISIONS = {"excluded_pending_lineage", "control_only", "evidence_insufficient"}
HISTORICAL_SOURCE = {
    "path": "research/m24_baseline_preparation.py",
    "commit": "ff23f8e44d214160a7f1f29fc07875eca590754a",
    "sha256": "ce2a5a0805533b91a6919cf36c78060b524cb1b1b1e979693adb2d682e71c6d1",
    "snapshot": "workflow_archive/m24_handoff_pre_fix_20261004/m24_baseline_preparation.py",
}


def _require(condition):
    if not condition:
        raise ValueError("catalogue_contract_invalid")


def _read(root, name):
    with project_path(root, name).open("rb") as stream:
        raw = stream.read(256_001)
    _require(len(raw) <= 256_000)
    doc = json.loads(raw, object_pairs_hook=_object,
                     parse_constant=lambda _: _require(False))
    _require(isinstance(doc, dict))
    return doc, {"path": name, "sha256": hashlib.sha256(raw).hexdigest()}


def _closed(doc, keys):
    _require(all(doc.get(key) is False for key in keys))


def _zero(doc, keys):
    _require(all(type(doc.get(key)) is int and doc[key] == 0 for key in keys))


def _rows(doc, key, maximum):
    rows = doc.get(key)
    _require(isinstance(rows, list) and 0 < len(rows) <= maximum)
    _require(all(isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"] for row in rows))
    _require(len({row["id"] for row in rows}) == len(rows))
    return rows


def read_catalogue(root: Path) -> dict:
    factors, factor_ref = _read(root, FIRST)
    books, book_ref = _read(root, BOOKS)
    _require(factors.get("schema") == "regis.goal.first_factor_registration.v1")
    _require(books.get("schema") == "regis.goal.parallel_playbook_registration.v1")
    _require(factors.get("status") == "bounded_catalogue_feasibility_screen_not_economic_preregistration")
    _require(books.get("status") == "complete_catalogue_feasibility_dispositions_not_economic_passes")
    _require(books.get("factor_registration") == factor_ref)
    verified = set()

    historical_used = False

    def bind(ref, source_commit):
        nonlocal historical_used
        if isinstance(ref, dict) and (ref.get("path") == HISTORICAL_SOURCE["path"]
                                      or ref.get("sha256") == HISTORICAL_SOURCE["sha256"]):
            _require(ref == {"path": HISTORICAL_SOURCE["path"], "sha256": HISTORICAL_SOURCE["sha256"]}
                     and source_commit == HISTORICAL_SOURCE["commit"])
            # Original source is data provenance only, never imported/executed.
            reference(root, {"path": HISTORICAL_SOURCE["snapshot"], "sha256": HISTORICAL_SOURCE["sha256"]})
            historical_used = True
        else:
            ref = reference(root, ref)
        verified.add((ref["path"], ref["sha256"]))
        return ref

    for doc in (factors, books):
        _closed(doc, ("production_eligible", "generates_alert"))
        _require(doc.get("maximum_deep_strategy_count") == 3
                 and doc.get("max_complete_configs_per_mechanism") == 4
                 and doc.get("max_winner_selection_configs") == 12)
        _require(isinstance(doc.get("bindings"), list) and doc["bindings"])
        for ref in doc["bindings"]:
            bind(ref, doc.get("input_commit"))
    _zero(factors, ("all_economic_runs",))
    _zero(books, ("new_deep_admissions", "new_economic_configs_frozen", "new_economic_runs"))
    _closed(books, ("automatic_orders",))
    _require(str(books.get("prior_search_count", "")).startswith("unknown_not_reset"))
    rows = _rows(factors, "factors", 16)
    _require(factors.get("factor_count") == len(rows) and factors.get("factor_cap") == 16
             and factors.get("max_formula_variants_per_family") == 4)
    budgets = Counter()
    projected_factors = []
    for row in rows:
        _require(row.get("decision") in DECISIONS)
        _zero(row, ("economic_runs", "economic_configurations_frozen"))
        _closed(row, ("new_admission", "production_eligible", "generates_alert"))
        _require(row.get("prior_search_count") == "unknown_not_reset" and row.get("parents"))
        _require(all(row.get(key) for key in ("formula", "unit", "input_clock", "failure_condition",
                                              "choice_freedom", "required_inputs", "family")))
        _require(isinstance(row["required_inputs"], list) and all(isinstance(x, str) for x in row["required_inputs"]))
        _require(type(row.get("formula_variants")) is int and 0 < row["formula_variants"] <= 4)
        budgets[row["family"]] += row["formula_variants"]
        bind(row["source_binding"], factors.get("input_commit"))
        projected_factors.append({key: row[key] for key in (
            "id", "family", "formula", "unit", "decision", "required_inputs", "input_clock",
            "failure_condition", "source_binding", "prior_search_count")})
    _require(all(value <= 4 for value in budgets.values()))
    cards = _rows(books, "complete_disposition_cards", 7)
    _require(books.get("cards_count") == len(cards) == 7)
    ids = {card["id"] for card in cards}
    maps = {}
    for key in ("original_M25_six_family_coverage", "N3_six_mechanism_coverage"):
        cover = books.get(key)
        _require(isinstance(cover, list) and len(cover) == 6)
        _require(all(isinstance(x, dict) and isinstance(x.get("family"), str)
                     and x.get("card_id") in ids for x in cover))
        _require(len({x["family"] for x in cover}) == 6)
        maps[key] = cover
    representatives = books.get("representative_economic_cards")
    _require(isinstance(representatives, list) and len(representatives) <= 3
             and all(isinstance(x, str) and x in ids for x in representatives)
             and len(set(representatives)) == len(representatives))
    fields = ("mechanism", "market_condition", "sector_condition", "stock_selection", "entry_new_cards",
              "position", "exit_new_cards", "conflicts", "costs", "no_trade", "failure_condition",
              "validation", "required_inputs")
    projected_cards = []
    for card in cards:
        _require(all(card.get(key) for key in fields))
        _require(card.get("capital_yuan") == 150000 and card.get("decision") in DECISIONS)
        _zero(card, ("economic_runs",))
        _closed(card, ("deep_trial_admitted", "trial_admitted", "production_eligible", "generates_alert"))
        _require(card.get("prior_search_count") == "unknown_not_reset")
        _require(isinstance(card["required_inputs"], dict) and all(
            isinstance(value, list) and all(isinstance(x, str) for x in value)
            for value in card["required_inputs"].values()))
        for key in ("binding", "original_card_binding"):
            if key in card:
                bind(card[key], books.get("input_commit"))
        projected_cards.append({key: card[key] for key in (
            "id", "decision", "capital_yuan", "prior_search_count", *fields)})
    _require(next((c for c in cards if c["id"] == "PB04_TREND_BREAKOUT_EXCLUDED"), {}).get("entry_rule_is_closed") is True)
    return {"schema": "regis.website.research_catalogue.v1", "status": "available",
            "catalogue_only": True, "factor_count": len(rows), "cards_count": len(cards),
            "factors": projected_factors, "playbooks": projected_cards, "family_coverage": maps,
            "source_bindings": [factor_ref, book_ref], "verified_dependency_count": len(verified),
            "historical_code_bindings": [dict(HISTORICAL_SOURCE)] if historical_used else [],
            "current_implementation_verified": False,
            "registered_utc": factors.get("registered_utc"), "capital_yuan": 150000,
            "current_catalogue_economic_runs": 0, "prior_search_count": "unknown_not_reset",
            "note": "可行性登记；所需输入尚待核验，未授予经济试验或交易资格。历史尝试总数未知，未重置。",
            **BOUNDARY}
