"""Bounded local diagnostic of public display granularity; no supplier calls."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "research/GOAL_ETF_QUANTITY_DIAGNOSTIC_PROTOCOL_20261002.json"
RECEIPT = ROOT / "research/GOAL_ETF_QUANTITY_DIAGNOSTIC_RECEIPT_20261002.json"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    protocol = json.loads(PROTOCOL.read_bytes())
    if sha(Path(__file__)) != protocol["script_sha256"]:
        raise ValueError("script identity changed")
    for item in protocol["inputs"]:
        if sha(ROOT / item["path"]) != item["sha256"]:
            raise ValueError("input identity changed")
    if args.verify:
        receipt = json.loads(RECEIPT.read_bytes())
        if receipt["protocol_sha256"] != sha(PROTOCOL):
            raise ValueError("diagnostic receipt stale")
        print(json.dumps({"identity_verified": True, "network_calls": 0, "model_admitted": False}))
        return
    if RECEIPT.exists():
        raise ValueError("diagnostic already recorded")
    raw = json.loads((ROOT / protocol["raw_path"]).read_bytes())
    public = json.loads((ROOT / protocol["public_path"]).read_bytes())
    if raw["fields"] != ["date", "code", "volume", "amount", "adjustflag"] or len(raw["rows"]) != 1:
        raise ValueError("unexpected raw shape")
    row = dict(zip(raw["fields"], raw["rows"][0]))
    if row["code"] != "sh.510300" or row["date"] != "2026-09-30" or row["adjustflag"] != "3":
        raise ValueError("unexpected source key")
    if public["code"] != "510300" or public["day"] != row["date"]:
        raise ValueError("unexpected public key")
    volume = Decimal(row["volume"])
    amount = Decimal(row["amount"])
    if any(not value.is_finite() or value < 0 for value in (volume, amount)):
        raise ValueError("invalid numeric value")
    regular = Decimal(public["regular_volume_hands"]) * Decimal(100)
    afterhours = Decimal(public["afterhours_volume_hands"]) * Decimal(100)
    display_step = Decimal(protocol["public_display_step_fund_units"])
    receipt = {
        "schema": "regis.goal.ETF_quantity_display_diagnostic.v1",
        "ran_utc": datetime.now(timezone.utc).isoformat(),
        "script_sha256": protocol["script_sha256"],
        "protocol_sha256": sha(PROTOCOL),
        "selection_status": "exploratory_after_exact_match_failure; protocol frozen before residual computation",
        "original_exact_volume_match": volume == regular,
        "within_one_public_display_step": abs(volume - regular) <= display_step,
        "native_volume_is_exact_multiple_of_display_step": volume % display_step == 0,
        "scaled_x100_within_one_public_display_step": abs(volume * 100 - regular) <= display_step,
        "full_session_within_one_public_display_step": abs(volume - regular - afterhours) <= display_step,
        "regular_amount_within_preexisting_100_CNY_tolerance":
            abs(amount - Decimal(public["regular_amount_10k_CNY"]) * 10000) <= 100,
        "original_strict_check_overwritten": False,
        "display_rounding_method_proved": False,
        "coverage_admitted": False,
        "unit_scale_consistent_with_x1_at_display_granularity": abs(volume - regular) <= display_step,
        "private_numeric_values_output": 0,
        "raw_prices_requested": False,
        "network_calls": 0,
        "returns_calculated": False,
        "full_N4": False,
        "model_admitted": False,
    }
    with RECEIPT.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(receipt, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({key: receipt[key] for key in (
        "original_exact_volume_match", "within_one_public_display_step",
        "scaled_x100_within_one_public_display_step", "full_session_within_one_public_display_step",
        "unit_scale_consistent_with_x1_at_display_granularity", "full_N4")}))


if __name__ == "__main__":
    main()

