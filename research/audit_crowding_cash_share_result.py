"""Independent arithmetic/cohort checks; does not call the replay engine."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def close(left, right):
    assert math.isclose(left, right, rel_tol=1e-10, abs_tol=1e-8), (left, right)


def audit(result_path: Path) -> dict:
    result = json.loads(result_path.read_text())
    for ref in result["bindings"] + [result["selection"], result["event_ledger"]]:
        assert digest(ROOT / ref["path"]) == ref["sha256"], ref["path"]
    signals = pd.read_csv(ROOT / result["selection"]["path"], dtype={"code": str})
    assert not signals.duplicated(["signal_day", "code"]).any()
    expected = {}
    for day, group in signals.groupby("signal_day"):
        ordered = sorted(group.to_dict("records"), key=lambda r: (-r["factor"], r["code"]))
        size = math.ceil(len(ordered) / 5)
        top, bottom = {r["code"] for r in ordered[:size]}, {r["code"] for r in ordered[-size:]}
        for row in ordered:
            key = (day, row["code"])
            label = "top" if row["code"] in top else "bottom" if row["code"] in bottom else "middle"
            assert row["quintile"] == label
            expected[key] = row
    seen, weekly, counts = set(), {}, Counter()
    with (ROOT / result["event_ledger"]["path"]).open() as stream:
        for line in stream:
            event = json.loads(line)
            key = event["signal_day"], event["code"]
            scenario = event["slippage_bps"]
            assert scenario in (10, 20, 30) and (*key, scenario) not in seen
            seen.add((*key, scenario))
            signal = expected[key]
            assert event["factor"] == signal["factor"] and event["quintile"] == signal["quintile"]
            assert event["signal_emotion"] == signal["signal_emotion"]
            counts[scenario, event["status"]] += 1
            weekly.setdefault((key[0], scenario), []).append(event)
            inventory, cost, entry_cash, proceeds = 0, 0., 0., 0.
            for order in event["execution_events"]:
                shares = order["shares"]
                if not shares:
                    continue
                assert shares > 0 and shares % 100 == 0
                side, day = order["side"], order["day"]
                notional = shares * order["price"]
                fees = order["costs"]
                close(fees["commission"], max(5., notional * .00025))
                close(fees["transfer"], notional * (.00002 if day < "2022-04-29" else .00001))
                close(fees["stamp"], notional * (.001 if day < "2023-08-28" else .0005) if side == "sell" else 0.)
                close(fees["slippage"], notional * scenario / 10000)
                assert 0 < fees["market_impact"] / notional <= .00025 + 1e-12
                close(fees["total"], sum(v for name, v in fees.items() if name != "total"))
                cost += fees["total"]
                if side == "buy":
                    assert inventory == 0 and entry_cash == 0
                    entry_cash = notional + fees["total"]
                    inventory += shares
                    assert entry_cash <= 10000
                else:
                    assert side == "sell" and day > event["entry_day"] and shares <= inventory
                    proceeds += notional - fees["total"]
                    inventory -= shares
            close(cost, event["cost_yuan"])
            assert inventory == event["remaining_shares"]
            if event["status"] == "closed":
                assert inventory == 0 and entry_cash > 0
                close(proceeds - entry_cash, event["net_pnl_yuan"])
                close(event["fixed_budget_return"], event["net_pnl_yuan"] / 10000)
                close(event["invested_cash_return"], event["net_pnl_yuan"] / entry_cash)
            elif event["status"] == "unresolved":
                assert event["fixed_budget_return"] is None and event["net_pnl_yuan"] is None
            else:
                assert event["status"] == "unfilled" and not event["execution_events"]
                assert event["fixed_budget_return"] == 0
    assert len(seen) == len(expected) * 3 == result["event_path_count"]
    selected_unknown = Counter()
    for metric in result["weekly_metrics"]:
        group = weekly[metric["signal_day"], metric["slippage_bps"]]
        for label in ("universe", "top", "bottom"):
            members = group if label == "universe" else [r for r in group if r["quintile"] == label]
            saved = metric[label]
            unknown = sum(r["status"] == "unresolved" for r in members)
            assert saved["signals"] == len(members) and saved["unresolved"] == unknown
            if label != "universe":
                selected_unknown[metric["slippage_bps"]] += unknown
            if unknown:
                assert saved["full_mean_return"] is None
            else:
                close(saved["full_mean_return"], sum(r["fixed_budget_return"] for r in members) / len(members))
        resolved = pd.DataFrame([r for r in group if r["fixed_budget_return"] is not None])
        ranks = resolved[["factor", "fixed_budget_return"]].rank(method="average")
        independent_ic = ranks.factor.corr(ranks.fixed_budget_return)
        if metric["resolved_only_rank_ic"] is not None:
            close(independent_ic, metric["resolved_only_rank_ic"])
        if metric["universe"]["unresolved"]:
            assert metric["full_rank_ic"] is None
    return {"status": "independent_cash_cohort_and_weekly_checks_passed",
            "result_sha256": digest(result_path), "audit_source_sha256": digest(Path(__file__)),
            "signal_count": len(signals), "event_path_count": len(seen), "weekly_metrics_checked": len(weekly),
            "selected_unknown_by_cost": dict(selected_unknown),
            "status_counts": [{"slippage_bps": bps, "status": state, "count": n} for (bps, state), n in sorted(counts.items())],
            "not_checked": ["Independent upstream raw-data verification", "Corporate entitlements", "Cloud execution"],
            "production_eligible": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.result)
    output = args.result.with_suffix(".independent_audit.json")
    with output.open("x") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps(result, indent=2))
