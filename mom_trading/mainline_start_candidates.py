"""Frozen research candidates for the mainline-start playbook."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable

from .market_sector_constituent_forward import verify_constituent_packet
from .market_sector_route_shadow import verify_shadow_decision_packet
from .market_state_daily import PROJECT_ROOT, _atomic_publish, _canonical_bytes, _sha256


FORMULA_PATH = PROJECT_ROOT / "research/MAINLINE_START_CONFIRMATION_STOCK_FORMULA_V3.json"
FORMULA_SHA256 = "5d8bf5e0c9e890451e3e46481066d49e5cb7f5a21e99f88d76f5e90153626aca"
_CONSTITUENT_ADMISSION_PATH = (
    PROJECT_ROOT
    / "research/MARKET_SECTOR_CONSTITUENT_FORWARD_DEPLOYMENT_ADMISSION_V3.json"
)
_CONSTITUENT_ADMISSION_SHA256 = (
    "6c1681e40c7b7133e89b0601bbb4cd90685bda55394b7510e18b0517c3556d43"
)
PACKET_DIR = (
    PROJECT_ROOT / "research/data/market_sector_route_forward/v1/candidate_packets"
)


def _project_path(raw: object, *, project_root: Path) -> Path:
    path = Path(str(raw or ""))
    resolved = (path if path.is_absolute() else project_root / path).resolve()
    resolved.relative_to(project_root.resolve())
    return resolved


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _constituent_successor_admitted(*, project_root: Path) -> bool:
    try:
        path = (
            project_root / _CONSTITUENT_ADMISSION_PATH.relative_to(PROJECT_ROOT)
        ).resolve()
        path.relative_to(project_root.resolve())
        if _sha256(path) != _CONSTITUENT_ADMISSION_SHA256:
            return False
        admission = json.loads(path.read_text(encoding="utf-8"))
        if admission.get("status") != (
            "successor_transport_identity_runtime_admitted_waiting_first_real_packet"
        ):
            return False
        for binding in (admission.get("bindings") or {}).values():
            target = _project_path(binding.get("path"), project_root=project_root)
            if not target.is_file() or _sha256(target) != binding.get("sha256"):
                return False
        permissions = admission.get("permissions") or {}
        return (
            permissions.get("capture_research_constituent_packets") is True
            and all(
                permissions.get(key) is False
                for key in (
                    "read_returns_or_labels", "change_unified_score",
                    "change_trade_count", "change_position_or_quantity",
                    "generate_buy_or_sell_alert", "connect_broker",
                    "execute_orders", "production", "automatic_trading",
                    "stable_profit_claim",
                )
            )
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def verify_formula(
    *, project_root: Path = PROJECT_ROOT, formula_path: Path = FORMULA_PATH,
    expected_formula_sha256: str | None = FORMULA_SHA256,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        formula_sha = _sha256(formula_path)
        if expected_formula_sha256 and formula_sha != expected_formula_sha256:
            errors.append("formula_sha256_mismatch")
        formula = json.loads(formula_path.read_text(encoding="utf-8"))
        if formula.get("status") != "frozen_before_first_candidate_packet_no_outcome_access":
            errors.append("formula_status_invalid")
        for name, binding in (formula.get("bindings") or {}).items():
            path = _project_path(binding.get("path"), project_root=project_root)
            if not path.is_file() or _sha256(path) != binding.get("sha256"):
                if (
                    name == "constituent_collector"
                    and _constituent_successor_admitted(project_root=project_root)
                ):
                    continue
                errors.append(f"binding_invalid:{name}")
        authority = formula.get("authority") or {}
        if authority.get("research_only") is not True or any(
            authority.get(key) is not False for key in (
                "changes_unified_score", "changes_trade_count",
                "changes_position_or_quantity", "generates_buy_or_sell_alert",
                "selects_production_candidate", "production_strategy",
                "automatic_trading", "stable_profit_claim",
            )
        ):
            errors.append("authority_invalid")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        formula_sha, formula = None, {}
        errors.append(f"formula_unreadable:{exc}")
    return {
        "valid": not errors, "errors": errors, "formula": formula,
        "formula_sha256": formula_sha,
    }


def _percentiles(values: dict[str, float], *, smaller_is_better: bool = False) -> dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    if len(ordered) == 1:
        return {ordered[0][0]: 1.0}
    result = {
        code: index / (len(ordered) - 1)
        for index, (code, _value) in enumerate(ordered)
    }
    return {
        code: (1.0 - value if smaller_is_better else value)
        for code, value in result.items()
    }


def build_mainline_candidates(
    route: dict[str, Any], constituent_packet: dict[str, Any], formula: dict[str, Any],
) -> dict[str, Any]:
    gate = formula["route_gate"]
    budget = route.get("research_budget_hypothesis") or {}
    playbook_ids = {
        str(item.get("id")) for item in (budget.get("allowed_playbooks") or [])
        if isinstance(item, dict)
    }
    state = str(route.get("market_state") or "unknown")
    route_ready = (
        route.get("status") == gate["route_status"]
        and state in gate["allowed_market_states"]
        and gate["required_playbook"] in playbook_ids
        and (route.get("sector_confirmation") or {}).get("ready") is True
    )
    hard_cap = int(gate["candidate_limit_hard_cap"])
    route_limit = int(budget.get("maximum_new_trades") or 0)
    limit = max(0, min(hard_cap, route_limit)) if route_ready else 0
    if constituent_packet.get("capture_status") != "complete":
        route_ready = False
        limit = 0

    rules = formula["eligibility"]
    prefixes = tuple(rules["A_share_code_prefixes"])
    excluded_tokens = tuple(token.upper() for token in rules["exclude_name_tokens"])
    grouped: dict[str, list[dict[str, Any]]] = {}
    for capture in constituent_packet.get("sector_captures") or []:
        sector = capture.get("sector") or {}
        for row in capture.get("rows") or []:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            item["sector"] = {
                "kind": sector.get("kind"), "code": sector.get("code"),
                "name": sector.get("name"),
            }
            grouped.setdefault(str(item.get("stock_code") or ""), []).append(item)

    eligible: dict[str, dict[str, Any]] = {}
    excluded: dict[str, list[str]] = {}
    critical = (
        "stock_name", "price", "change_pct", "volume", "amount",
        "turnover_rate", "total_market_cap", "float_market_cap", "data_as_of",
    )
    for code, rows in grouped.items():
        reasons: list[str] = []
        row = rows[0]
        if len(code) != 6 or not code.isdigit() or not code.startswith(prefixes):
            reasons.append("unsupported_share_code")
        name = str(row.get("stock_name") or "")
        if any(token in name.upper() for token in excluded_tokens):
            reasons.append("name_risk_excluded")
        if any(row.get(field) is None for field in critical):
            reasons.append("required_field_missing")
        if any(
            any(other.get(field) != row.get(field) for field in critical)
            for other in rows[1:]
        ):
            reasons.append("duplicate_sector_quote_inconsistent")
        change = _finite(row.get("change_pct"))
        amount = _finite(row.get("amount"))
        turnover = _finite(row.get("turnover_rate"))
        float_cap = _finite(row.get("float_market_cap"))
        checks = (
            (change, "change_pct", rules["change_pct_min_inclusive"], rules["change_pct_max_inclusive"]),
            (amount, "amount", rules["amount_cny_min_inclusive"], None),
            (turnover, "turnover_rate", rules["turnover_rate_pct_min_inclusive"], rules["turnover_rate_pct_max_inclusive"]),
            (float_cap, "float_market_cap", rules["float_market_cap_cny_min_inclusive"], rules["float_market_cap_cny_max_inclusive"]),
        )
        for value, label, lower, upper in checks:
            if value is None or value < lower or (upper is not None and value > upper):
                reasons.append(f"{label}_outside_fixed_band")
        if reasons:
            excluded[code] = list(dict.fromkeys(reasons))
            continue
        item = dict(row)
        item["sectors"] = sorted(
            [other["sector"] for other in rows], key=lambda sector: (str(sector["code"]), str(sector["name"])),
        )
        eligible[code] = item

    components: dict[str, dict[str, float]] = {}
    if eligible:
        strength = _percentiles({code: float(row["change_pct"]) for code, row in eligible.items()})
        amounts = _percentiles({code: float(row["amount"]) for code, row in eligible.items()})
        turnovers = _percentiles({code: min(float(row["turnover_rate"]), 10.0) for code, row in eligible.items()})
        float_caps = _percentiles(
            {code: float(row["float_market_cap"]) for code, row in eligible.items()},
            smaller_is_better=True,
        )
        for code in eligible:
            components[code] = {
                "same_slot_relative_strength": strength[code],
                "same_slot_amount": amounts[code],
                "turnover_rate_capped_at_10pct": turnovers[code],
                "smaller_float_market_cap_within_the_safe_band": float_caps[code],
            }
    weights = formula["ranking"]["higher_is_better_percentile_components"]
    ranked: list[dict[str, Any]] = []
    for code, row in eligible.items():
        score = sum(components[code][key] * float(weight) for key, weight in weights.items())
        ranked.append({
            "stock_code": code, "stock_name": row["stock_name"],
            "score": round(score * 100, 6), "components": components[code],
            "price": row["price"], "change_pct": row["change_pct"],
            "amount": row["amount"], "turnover_rate": row["turnover_rate"],
            "float_market_cap": row["float_market_cap"],
            "data_as_of": row["data_as_of"], "sectors": row["sectors"],
        })
    ranked.sort(key=lambda row: (-row["score"], -float(row["amount"]), row["stock_code"]))
    selected = ranked[:limit]
    return {
        "status": "candidate_research_ready" if route_ready else "route_or_input_not_ready",
        "playbook_id": gate["required_playbook"],
        "stock_formula_id": formula["formula_id"],
        "factor_evidence": {
            "status": "strategy_formula_only_no_admitted_factor",
            "admitted_factor_ids": [],
            "factor_score_applied": False,
        },
        "market_state": state, "route_candidate_limit": limit,
        "eligible_count": len(ranked), "selected_count": len(selected),
        "selected": selected, "ranked_eligible": ranked,
        "excluded": excluded,
        "named_person_signal_used": False,
        "returns_or_labels_read": False,
    }


def verify_candidate_packet(path: Path, *, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    errors: list[str] = []
    try:
        resolved = path.resolve()
        resolved.relative_to(project_root.resolve())
        packet = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"packet_unreadable:{exc}"]}
    identity = dict(packet)
    packet_id = str(identity.pop("packet_id", ""))
    if packet.get("schema") != "mainline_start_candidate_research.v1":
        errors.append("schema_invalid")
    if hashlib.sha256(_canonical_bytes(identity)).hexdigest() != packet_id:
        errors.append("packet_id_invalid")
    if packet.get("no_backfill") is not True or packet.get("returns_or_labels_read") is not False:
        errors.append("causality_boundary_invalid")
    try:
        formula_binding = packet.get("formula") or {}
        formula_path = _project_path(formula_binding.get("path"), project_root=project_root)
        if _sha256(formula_path) != formula_binding.get("sha256"):
            raise ValueError("formula_sha256_invalid")
        formula_check = verify_formula(
            project_root=project_root, formula_path=formula_path,
            expected_formula_sha256=str(formula_binding.get("sha256") or ""),
        )
        constituent_binding = packet.get("constituent_packet") or {}
        constituent_path = _project_path(
            constituent_binding.get("path"), project_root=project_root,
        )
        if _sha256(constituent_path) != constituent_binding.get("sha256"):
            raise ValueError("constituent_sha256_invalid")
        constituent_check = verify_constituent_packet(
            constituent_path, project_root=project_root,
        )
        shadow_binding = packet.get("shadow_decision") or {}
        shadow_path = _project_path(
            shadow_binding.get("path"), project_root=project_root,
        )
        if _sha256(shadow_path) != shadow_binding.get("sha256"):
            raise ValueError("shadow_sha256_invalid")
        shadow_check = verify_shadow_decision_packet(
            shadow_path, project_root=project_root,
        )
        constituent = constituent_check.get("packet") or {}
        shadow = shadow_check.get("packet") or {}
        if formula_check.get("valid") is not True:
            errors.append("formula_verification_failed")
        if (
            constituent_check.get("valid") is not True
            or constituent_binding.get("packet_id") != constituent.get("packet_id")
        ):
            errors.append("constituent_verification_failed")
        if (
            shadow_check.get("valid") is not True
            or shadow_binding.get("packet_id") != shadow.get("packet_id")
        ):
            errors.append("shadow_verification_failed")
        identity_pair = (packet.get("decision_day"), packet.get("slot_id"))
        if identity_pair != (
            constituent.get("decision_day"), constituent.get("slot_id")
        ) or identity_pair != (shadow.get("decision_day"), shadow.get("slot_id")):
            errors.append("upstream_decision_identity_mismatch")
        expected_result = build_mainline_candidates(
            shadow.get("route") or {}, constituent,
            formula_check.get("formula") or {},
        )
        if packet.get("research_result") != expected_result:
            errors.append("research_result_recompute_mismatch")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"upstream_verification_failed:{exc}")
    authority = packet.get("authority") or {}
    if authority.get("research_only") is not True or any(
        authority.get(key) is not False for key in (
            "changes_unified_score", "changes_trade_count",
            "changes_position_or_quantity", "generates_buy_or_sell_alert",
            "selects_production_candidate", "production_strategy",
            "automatic_trading", "stable_profit_claim",
        )
    ):
        errors.append("authority_invalid")
    return {"valid": not errors, "errors": errors, "packet": packet, "packet_id": packet_id}


def capture_mainline_candidates(
    constituent_capture: dict[str, Any], *, project_root: Path = PROJECT_ROOT,
    formula_path: Path = FORMULA_PATH, expected_formula_sha256: str | None = FORMULA_SHA256,
    packet_dir: Path = PACKET_DIR,
    constituent_verifier: Callable[..., dict[str, Any]] = verify_constituent_packet,
    shadow_verifier: Callable[..., dict[str, Any]] = verify_shadow_decision_packet,
) -> dict[str, Any]:
    formula_check = verify_formula(
        project_root=project_root, formula_path=formula_path,
        expected_formula_sha256=expected_formula_sha256,
    )
    if formula_check.get("valid") is not True:
        return {"status": "identity_invalid", "captured": False, "errors": formula_check["errors"]}
    try:
        constituent_path = _project_path(constituent_capture.get("path"), project_root=project_root)
        constituent_check = constituent_verifier(constituent_path, project_root=project_root)
        constituent = constituent_check.get("packet") or {}
        shadow_path = _project_path(
            (constituent.get("shadow_decision") or {}).get("path"), project_root=project_root,
        )
        shadow_check = shadow_verifier(shadow_path, project_root=project_root)
    except (OSError, ValueError):
        return {"status": "input_invalid", "captured": False}
    if constituent_check.get("valid") is not True or shadow_check.get("valid") is not True:
        return {"status": "input_invalid", "captured": False}
    shadow = shadow_check["packet"]
    result = build_mainline_candidates(
        shadow.get("route") or {}, constituent, formula_check["formula"],
    )
    packet = {
        "schema": "mainline_start_candidate_research.v1",
        "decision_day": constituent.get("decision_day"),
        "slot_id": constituent.get("slot_id"),
        "no_backfill": True,
        "formula": {"path": str(formula_path.relative_to(project_root)), "sha256": formula_check["formula_sha256"]},
        "constituent_packet": {"path": str(constituent_path.relative_to(project_root)), "sha256": _sha256(constituent_path), "packet_id": constituent.get("packet_id")},
        "shadow_decision": {"path": str(shadow_path.relative_to(project_root)), "sha256": _sha256(shadow_path), "packet_id": shadow.get("packet_id")},
        "research_result": result,
        "returns_or_labels_read": False,
        "authority": {
            "research_only": True, "changes_unified_score": False,
            "changes_trade_count": False, "changes_position_or_quantity": False,
            "generates_buy_or_sell_alert": False,
            "selects_production_candidate": False, "production_strategy": False,
            "automatic_trading": False, "stable_profit_claim": False,
        },
    }
    packet["packet_id"] = hashlib.sha256(_canonical_bytes(packet)).hexdigest()
    path = packet_dir / f"{packet['decision_day']}-{packet['slot_id']}.json"
    if path.exists():
        verified = verify_candidate_packet(path, project_root=project_root)
        return {
            "status": "already_captured" if verified.get("valid") else "existing_packet_invalid",
            "captured": verified.get("valid") is True, "path": str(path),
            "packet_id": verified.get("packet_id"), "errors": verified.get("errors") or [],
        }
    _atomic_publish(path, _canonical_bytes(packet))
    verified = verify_candidate_packet(path, project_root=project_root)
    if verified.get("valid") is not True:
        raise RuntimeError("candidate packet failed post-write verification")
    return {
        "status": "captured", "captured": True, "path": str(path),
        "packet_id": packet["packet_id"],
        "selected_count": result["selected_count"],
        "changes_trade_count_or_position": False,
    }


__all__ = [
    "build_mainline_candidates", "capture_mainline_candidates",
    "verify_candidate_packet", "verify_formula",
]
