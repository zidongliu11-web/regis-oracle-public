"""Read-only projection of the four previously sealed conditional ETF scenarios.

No price reads, reruns, account access, parameter changes or winner selection.
"""
import hashlib
import json
from pathlib import Path

PINNED = {
    "protocol": ("research/ETF_DAILY_REFERENCE_PROTOCOL_20261002.json", "162e07a9eeb988e1da899e5516f87b81f58326cfe96bb8fea05317ef53cebcad"),
    "result": ("research/ETF_DAILY_REFERENCE_RESULT_20261002.json", "e65a9d3f2b0549f7541a469b6db71f45df8271d42c03d1865dc47aa116c05ac5"),
    "acquisition": ("research/RQ_ETF_HISTORY_RECOVERY_RECEIPT_20261002.json", "cd752d56d1b38c200bac26c5dd6ecd01ad7c5c50d650361e6285f9d2062cca87"),
    "implementation": ("research/run_etf_daily_reference.py", "adc05fb6f62045116b3374133476b0cb233f11aeb489c2cccf9911b070a077e1"),
    "issuer_census": ("research/ETF_ACTION_CASH_COMPLETION_RESULT_20261002.json", "b7a33137f7a57c6243cdec50f05447ca522d956f877681d823116d399ef18d21"),
}
SCENARIOS = ("standard_full", "standard_partial_half", "delayed_adverse", "no_fill_cash")
LABELS = ("全额成交假设", "半额成交假设", "延迟且不利成交假设", "未成交，持有现金")
METRICS = ("scenario_id", "entry_date", "terminal_date", "initial_CNY", "terminal_CNY",
           "total_return", "CAGR", "max_drawdown_on_daily_marks", "filled_fund_units",
           "cancelled_fund_units", "model_transaction_fees_CNY", "model_dividend_cash_paid_CNY")


def load_reference(root):
    root, documents = Path(root), {}
    for key, (relative, expected) in PINNED.items():
        with (root / relative).open("rb") as stream:
            raw = stream.read(512001)
        if len(raw) > 512000 or hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("baseline_source_changed")
        if key != "implementation":
            documents[key] = json.loads(raw)
    p, r = documents["protocol"], documents["result"]
    valid = (
        r["state"] == "computed_conditional_reference"
        and r["protocol_sha256"] == PINNED["protocol"][1]
        and r["implementation_sha256"] == PINNED["implementation"][1]
        and r["acquisition_receipt_sha256"] == p["acquisition_receipt_sha256"] == PINNED["acquisition"][1]
        and p["issuer_census_sha256"] == PINNED["issuer_census"][1]
        and p["order_book_id"] == "510300.XSHG" and p["initial_CNY"] == "150000" and p["lot_size"] == 100
        and r["model_admitted"] is False and r["full_L1_L4_passed"] is False and r["raw_prices_exported"] is False
        and r["costs_are_frozen_sensitivity_models"] is True
        and r["cash_control"] == {"initial_CNY": "150000", "terminal_CNY": "150000", "interest_assumed_CNY": "0", "total_return": 0}
        and tuple(s["id"] for s in p["scenarios"]) == SCENARIOS
        and tuple(s["scenario_id"] for s in r["scenarios"]) == SCENARIOS
        and all(s["initial_CNY"] == "150000" and s["executed_actual_orders"] == 0
                and s["not_actual_account_or_minute_execution"] is True and s["book_shared_cash_violation"] is False
                for s in r["scenarios"])
        and p["conditions_and_limits"] == r["conditions_and_limits"]
    )
    if not valid:
        raise ValueError("baseline_scope_changed")
    return {
        "status": "available", "state": r["state"], "order_book_id": p["order_book_id"],
        "initial_CNY": p["initial_CNY"], "lot_size": p["lot_size"],
        "first_date": r["first_date"], "last_date": r["last_date"], "price_sessions": r["price_sessions"],
        "recorded_at": r["ended_utc"], "cash_control": r["cash_control"],
        "scenarios": [{**{k: row[k] for k in METRICS}, "label": LABELS[i], "assumptions": p["scenarios"][i]}
                      for i, row in enumerate(r["scenarios"])],
        "ledger_models": p["ledger_models"], "conditions_and_limits": r["conditions_and_limits"],
        "source_price_units": r["source_price_units"], "dividend_treatment": r["dividend_treatment"],
        "sources": {key: {"path": value[0], "sha256": value[1]} for key, value in PINNED.items()},
        "new_economic_runs": 0, "actual_orders": 0, "winner_selection": False,
        "OOS_certified": False, "full_L1_L4_passed": False,
    }
