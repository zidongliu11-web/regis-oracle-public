"""Calculate frozen-pattern research features, without reading return labels.

Uses the already bound PIT stock/state input pair. Output is an immutable
research snapshot, never a strategy admission or a buy/sell notification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from mom_trading.emotion_pattern_research import (
    REQUIRED, FACTORS, compute_features, build_research_candidates, research_catalog,
)
from mom_trading.sentiment_rising_industry_momentum import (
    STOCK_FEATURES_PATH, MARKET_STATE_PATH,
    EXPECTED_STOCK_FEATURES_SHA256, EXPECTED_MARKET_STATE_SHA256,
)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(*, as_of: str, output: Path) -> dict:
    if output.exists():
        raise FileExistsError("Research snapshot already exists; do not overwrite.")
    timestamp = pd.Timestamp(as_of)
    if timestamp.tzinfo is None:
        raise ValueError("as_of_timezone_required")
    timestamp = timestamp.tz_convert("Asia/Shanghai")
    references = []
    for path, expected in ((STOCK_FEATURES_PATH, EXPECTED_STOCK_FEATURES_SHA256), (MARKET_STATE_PATH, EXPECTED_MARKET_STATE_SHA256)):
        if sha(path) != expected:
            raise ValueError(f"research_input_hash_mismatch:{path.name}")
        references.append({"path": str(path.relative_to(ROOT)), "sha256": expected})
    catalog = research_catalog()  # Freeze rules before any feature computation.
    start = (timestamp - pd.Timedelta(days=180)).tz_localize(None).normalize()
    end = timestamp.tz_localize(None).normalize()
    rows = pd.read_parquet(STOCK_FEATURES_PATH, columns=sorted(REQUIRED), filters=[("trade_date", ">=", start), ("trade_date", "<=", end)])
    features = compute_features(rows, as_of=as_of)
    state_document = json.loads(MARKET_STATE_PATH.read_text(encoding="utf-8"))
    states = pd.DataFrame([
        {"trade_date": r["trade_date"], "confirmed_state": (r.get("features") or {}).get("confirmed_state", "UNKNOWN")}
        for r in state_document.get("rows", []) if r.get("trade_date")
    ])
    latest = features.trade_date.max()
    current = features.loc[features.trade_date.eq(latest)].copy()
    candidates = build_research_candidates(current, states)
    # Recheck source identities after computation, before publishing a snapshot.
    for ref in references:
        if sha(ROOT / ref["path"]) != ref["sha256"]:
            raise ValueError("research_inputs_changed_during_calculation")
    result = {
        "schema": "emotion_pattern_snapshot.v1", "as_of_requested": as_of,
        "data_asof": latest.date().isoformat() if pd.notna(latest) else None,
        "status": "research_features_computed_validation_not_run",
        "input_references": references, "catalog": catalog,
        "implementation_sha256": sha(ROOT / "mom_trading/emotion_pattern_research.py"),
        "historical_input_reused": True, "unseen_out_of_sample": False,
        "input_rows": len(rows), "current_stock_rows": len(current),
        "history_ready_rows": int(current.history_ready.sum()),
        "factor_finite_counts": {f["name"]: int(current[f["id"]].notna().sum()) for f in FACTORS},
        "research_candidates": json.loads(candidates.to_json(orient="records", date_format="iso", force_ascii=False)),
        "candidate_count_by_playbook": candidates.groupby("playbook_name").size().to_dict(),
        "return_labels_read": False, "performance": None,
        "production_eligible": False, "generates_buy_or_sell_alert": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return {k: v for k, v in result.items() if k not in {"research_candidates", "catalog"}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(run(as_of=args.as_of, output=args.output), ensure_ascii=False, indent=2))
