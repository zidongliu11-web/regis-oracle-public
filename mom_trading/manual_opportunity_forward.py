"""Forward paper evidence for the exact manual opportunity strategy identity."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Callable

from quantqc.costs import one_side_cost_rate
from research.full_market_finder import read_series

from .timing_engine import list_alerts


ROOT = Path(__file__).resolve().parent.parent
PROTOCOL_PATH = ROOT / "research" / "MANUAL_OPPORTUNITY_EXECUTION_V7_FORWARD_PROTOCOL.json"
REPORT_PATH = ROOT / "reports" / "manual_opportunity_execution_v7_forward.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _alert_facts(row: dict, protocol: dict) -> dict | None:
    if (
        row.get("origin") != protocol["eligibility"]["origin"]
        or row.get("action") != protocol["eligibility"]["action"]
        or row.get("strategy_id") != protocol["strategy_id"]
    ):
        return None
    created = _parse_time(row.get("created_at"))
    frozen = _parse_time(protocol.get("frozen_at"))
    if created is None or frozen is None or created <= frozen:
        return None
    try:
        evidence = json.loads(str(row.get("evidence_ref_json") or ""))
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(evidence, dict)
        or evidence.get("strategy_id") != protocol["strategy_id"]
        or evidence.get("strategy_spec_sha256") != protocol["strategy_spec_sha256"]
    ):
        return None
    plan = row.get("action_plan")
    action = ((plan or {}).get("trade_plan") or {}).get("action")
    try:
        price = float(row.get("price"))
        shares = int((action or {}).get("shares"))
    except (TypeError, ValueError):
        return None
    signal_id = str(row.get("signal_id") or "")
    if (
        not signal_id or not math.isfinite(price) or price <= 0
        or shares <= 0 or shares % 100
        or not isinstance((plan or {}).get("allocation"), dict)
        or (action or {}).get("side") != "BUY"
        or (action or {}).get("manual_confirmation") is not True
        or not (action or {}).get("valid_until")
        or not (action or {}).get("cancel_condition")
        or not (action or {}).get("evidence_identity")
    ):
        return None
    try:
        if float((action or {}).get("limit_price")) <= 0 or float((action or {}).get("estimated_cost")) < 0:
            return None
    except (TypeError, ValueError):
        return None
    return {
        "alert_id": row.get("id"), "signal_id": signal_id,
        "code": str(row.get("code") or ""), "name": str(row.get("name") or ""),
        "created_at": created.isoformat(), "signal_date": created.date().isoformat(),
        "reference_price": price, "shares": shares,
        "valid_until": row.get("valid_until"), "alert_status": row.get("status"),
    }


def _scenario(entry_price: float, shares: int, path_return: float, slippage: float) -> dict:
    entry_notional = entry_price * shares
    exit_notional = entry_notional * (1.0 + path_return)
    buy_cost = entry_notional * one_side_cost_rate(entry_notional, "buy", slippage)
    sell_cost = exit_notional * one_side_cost_rate(exit_notional, "sell", slippage)
    basis = entry_notional + buy_cost
    net = exit_notional - sell_cost - basis
    return {
        "net_return_pct": round(net / basis * 100.0, 6),
        "net_pnl_yuan": round(net, 2),
        "buy_cost_yuan": round(buy_cost, 2),
        "sell_cost_yuan": round(sell_cost, 2),
    }


def _summarize(rows: list[dict], scenario: str) -> dict:
    values = [float(row[scenario]["net_return_pct"]) for row in rows]
    return {
        "completed": len(values),
        "mean_net_return_pct": round(mean(values), 6) if values else None,
        "median_net_return_pct": round(median(values), 6) if values else None,
        "win_rate_pct": round(sum(value > 0 for value in values) / len(values) * 100.0, 4) if values else None,
        "total_net_pnl_yuan": round(sum(float(row[scenario]["net_pnl_yuan"]) for row in rows), 2) if values else None,
    }


def refresh_manual_opportunity_forward(
    *,
    protocol_path: Path = PROTOCOL_PATH,
    report_path: Path = REPORT_PATH,
    alert_rows: list[dict] | None = None,
    series_loader: Callable[[str], list[dict]] = read_series,
) -> dict:
    protocol_raw = protocol_path.read_bytes()
    protocol = json.loads(protocol_raw)
    spec_path = ROOT / str(protocol["strategy_spec_path"])
    spec_sha = _sha256(spec_path)
    identity_ok = spec_sha == protocol.get("strategy_spec_sha256")
    source_rows = list_alerts(100_000) if alert_rows is None else alert_rows
    facts_by_signal: dict[str, dict] = {}
    ineligible_alert_count = 0
    for row in source_rows:
        if row.get("strategy_id") != protocol.get("strategy_id"):
            continue
        facts = _alert_facts(row, protocol) if identity_ok else None
        if facts is None:
            ineligible_alert_count += 1
            continue
        facts_by_signal.setdefault(facts["signal_id"], facts)

    completed, pending = [], []
    series_fingerprints = {}
    horizon = int(protocol["outcome"]["horizon_completed_trading_days"])
    for facts in sorted(facts_by_signal.values(), key=lambda row: (row["created_at"], row["signal_id"])):
        bars = series_loader(facts["code"])
        later = [row for row in bars if str(row.get("date") or "") > facts["signal_date"]]
        series_fingerprints[facts["signal_id"]] = {
            "code": facts["code"],
            "last_date": bars[-1]["date"] if bars else None,
            "rows": len(bars),
            "outcome_path_sha256": hashlib.sha256(
                _canonical(later[:horizon]).encode()
            ).hexdigest(),
        }
        if len(later) < horizon:
            pending.append({**facts, "completed_horizon_days": len(later)})
            continue
        path = later[:horizon]
        daily = [float(row["pctChg"]) / 100.0 for row in path]
        if any(not math.isfinite(value) or value <= -1.0 for value in daily):
            pending.append({**facts, "completed_horizon_days": 0, "reason": "invalid_return_path"})
            continue
        path_return = math.prod(1.0 + value for value in daily) - 1.0
        completed.append({
            **facts,
            "exit_date": path[-1]["date"],
            "gross_path_return_pct": round(path_return * 100.0, 6),
            "base": _scenario(facts["reference_price"], facts["shares"], path_return, 0.001),
            "stress": _scenario(facts["reference_price"], facts["shares"], path_return, 0.002),
        })

    signature_payload = {
        "protocol_sha256": hashlib.sha256(protocol_raw).hexdigest(),
        "strategy_spec_sha256": spec_sha,
        "alerts": list(facts_by_signal.values()),
        "series": series_fingerprints,
    }
    input_signature = hashlib.sha256(_canonical(signature_payload).encode()).hexdigest()
    if report_path.exists():
        try:
            existing = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        if existing.get("input_signature") == input_signature:
            return existing

    minimum = int(protocol["interpretation"]["minimum_completed_signals_for_descriptive_review"])
    status = (
        "identity_mismatch" if not identity_ok else
        "collecting_no_signals" if not facts_by_signal else
        "collecting_unmatured" if not completed else
        "insufficient_completed" if len(completed) < minimum else
        "descriptive_review_ready"
    )
    result = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "protocol_id": protocol["protocol_id"],
        "protocol_path": _display_path(protocol_path),
        "protocol_sha256": hashlib.sha256(protocol_raw).hexdigest(),
        "strategy_id": protocol["strategy_id"],
        "strategy_spec_sha256": spec_sha,
        "identity_ok": identity_ok,
        "status": status,
        "eligible_alert_count": len(facts_by_signal),
        "pending_outcome_count": len(pending),
        "completed_outcome_count": len(completed),
        "ineligible_alert_count": ineligible_alert_count,
        "minimum_completed_signals_for_descriptive_review": minimum,
        "base": _summarize(completed, "base"),
        "stress": _summarize(completed, "stress"),
        "pending": pending,
        "completed": completed,
        "input_signature": input_signature,
        "forward_evidence_ready": len(completed) >= minimum,
        "production_unlock": False,
        "stable_profit_proven": False,
        "interpretation": "仅评价冻结后真实触发的V7纸面提醒；不回填、不等同实际成交、不自动投产。",
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(report_path)
    return result


__all__ = ["refresh_manual_opportunity_forward"]
