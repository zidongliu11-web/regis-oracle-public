"""Empty IC is missing evidence, never a crash or an inferred positive result."""
import pandas as pd
from factor_system.diagnostics import regime_ic


def test_empty_cross_section_keeps_explicit_empty_evidence():
    dates = pd.bdate_range("2024-01-01", periods=5)
    empty = pd.DataFrame(float("nan"), index=dates, columns=["fictional"])
    result = regime_ic(empty, empty, list(dates))
    assert result["yearly"] == []
    assert result["market_emotion_conditioned"] is False
