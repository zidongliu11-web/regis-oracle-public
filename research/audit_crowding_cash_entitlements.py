"""Independent cash/quantity/tax reconciliation of the amended event ledger."""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def same(a, b):
    assert math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-8), (a, b)


def money(value):
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def audit(path: Path):
    result = json.loads(path.read_text())
    for ref in result["bindings"] + [result["event_ledger"], result["selection"]]:
        assert sha(ROOT / ref["path"]) == ref["sha256"], ref["path"]
    primary = json.loads((ROOT / "output/research/recent_limit_crowding_cash_share_v1a.json").read_text())
    counts, transitions, credited = Counter(), Counter(), Counter()
    affected = 0
    with (ROOT / result["event_ledger"]["path"]).open() as new_stream, (ROOT / primary["event_ledger"]["path"]).open() as old_stream:
        for new_line, old_line in zip(new_stream, old_stream, strict=True):
            new, old = json.loads(new_line), json.loads(old_line)
            for key in ("signal_day", "code", "factor", "quintile", "slippage_bps", "signal_emotion"):
                assert new[key] == old[key], key
            counts[new["slippage_bps"], new["status"]] += 1
            if new["cash_entitlement_review"] == "no_action_detected_in_original_window":
                assert {k: new[k] for k in old} == old
                continue
            affected += 1
            transitions[old["status"], new["status"]] += 1
            orders = [r for r in new["execution_events"] if r["shares"]]
            inventory = sum(r["shares"] * (1 if r["side"] == "buy" else -1) for r in orders)
            assert inventory == new["remaining_shares"]
            if new["status"] != "closed":
                if new["status"] == "unresolved":
                    assert new["net_pnl_yuan"] is None and new["fixed_budget_return"] is None
                continue
            buys = sum(r["price"] * r["shares"] + r["costs"]["total"] for r in orders if r["side"] == "buy")
            sales = sum(r["price"] * r["shares"] - r["costs"]["total"] for r in orders if r["side"] == "sell")
            gross, tax = 0., 0.
            for action in new["cash_entitlements"]:
                quantity = sum(r["shares"] * (1 if r["side"] == "buy" else -1) for r in orders if r["day"] <= action["record_date"])
                assert quantity == action["entitled_shares"] > 0
                assert new["entry_day"] <= action["record_date"] < action["ex_date"] <= action["payout_date"]
                credit = money(Decimal(str(action["cash_per_share"])) * quantity)
                entry = pd.Timestamp(new["entry_day"])
                sold, due = 0, 0.
                for order in orders:
                    if order["side"] == "sell" and order["day"] > action["record_date"]:
                        sale = pd.Timestamp(order["day"])
                        rate = .2 if sale <= entry + pd.DateOffset(months=1) else .1 if sale <= entry + pd.DateOffset(years=1) else 0.
                        due += money(Decimal(str(action["cash_per_share"])) * order["shares"] * Decimal(str(rate)))
                        sold += order["shares"]
                assert sold == quantity
                same(credit, action["gross_yuan"])
                same(due, action["tax_yuan"])
                gross += credit
                tax += due
            same(gross, new["dividend_gross_yuan"])
            same(tax, new["dividend_tax_yuan"])
            same(sales - buys + gross - tax, new["net_pnl_yuan"])
            same(new["net_pnl_yuan"] / 10000, new["fixed_budget_return"])
            same(new["net_pnl_yuan"] / buys, new["invested_cash_return"])
            assert new["remaining_shares"] == 0
            if gross:
                credited[new["slippage_bps"]] += 1
    assert sum(counts.values()) == result["event_path_count"]
    assert affected == result["affected_signal_count"] * 3
    assert {int(k): v for k, v in result["cash_credited_events_by_cost"].items()} == dict(credited)
    return {"status": "independent_cash_entitlement_audit_passed", "result_sha256": sha(path),
            "auditor_source_sha256": sha(Path(__file__)), "all_paths_identity_checked": sum(counts.values()),
            "replayed_paths_arithmetic_checked": affected, "cash_credited_events_by_cost": dict(credited),
            "production_eligible": False, "not_a_broker_cash_confirmation": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.result)
    with args.result.with_suffix(".independent_audit.json").open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps(result, indent=2))
