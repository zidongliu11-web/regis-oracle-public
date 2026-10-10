"""A fictional offline panel; never a tradable factor or market-performance result."""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
from factor_system.operators import xs_rank

def main():
    rng = np.random.default_rng(42)
    dates = pd.bdate_range("2024-01-01", periods=60)
    columns = [f"fictional_{i:02d}" for i in range(12)]
    close = pd.DataFrame(
        10 * np.exp(np.cumsum(rng.normal(0, .01, (60, 12)), axis=0)),
        index=dates, columns=columns,
    )
    candidate = close.pct_change(5, fill_method=None)
    ranked = xs_rank(candidate)
    print(json.dumps({
        "data_kind": "fictional_generated_fixture",
        "dates": len(ranked), "instruments": len(ranked.columns),
        "warmup_missing_cells": int(ranked.isna().sum().sum()),
        "candidate_formula": "cross_section_rank(five_session_return)",
        "status": "example_only",
        "real_market_data_used": False, "trading_admitted": False,
    }, ensure_ascii=False))

if __name__ == "__main__":
    main()
