from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RiskProfile:
    initial_capital: float = 10_000.0
    max_positions: int = 3
    initial_position_limit: float = 0.20
    approved_position_limit: float = 0.30
    stop_loss: float = 0.08
    take_profit: float = 0.15
    daily_loss_limit: float = 0.02
    drawdown_warning: float = 0.10
    drawdown_halt: float = 0.15
    holding_days_min: int = 1
    holding_days_max: int = 10

    def to_dict(self) -> dict:
        return asdict(self)


RISK_PROFILE = RiskProfile()


def stock_is_eligible(code: str, name: str = "", price: float | None = None) -> tuple[bool, str | None]:
    if "ST" in name.upper() or "退" in name:
        return False, "ST、*ST 与退市整理股不纳入股票池"
    if code.startswith(("688", "689", "8", "43", "83", "87", "88", "92")):
        return False, "当前试运营不纳入科创板或北交所"
    if price is not None and price < 5:
        return False, "低于 5 元的股票不纳入股票池"
    return True, None
