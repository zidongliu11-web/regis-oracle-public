from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from .market import Quote


@dataclass(frozen=True)
class Signal:
    code: str
    name: str
    action: str
    level: str
    reason: str
    data_time: str
    research_status: str
    buy_zone: str | None = None
    stop_loss: float | None = None
    sell_condition: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class SignalEngine:
    """交易信号的唯一入口。

    初始版本默认没有已验证策略；因此不会将行情异动伪装成买卖建议。
    后续只有加载了已审计的策略产物后，才能生成 strong_buy / sell 信号。
    """

    def __init__(self, research_verified: bool = False):
        self.research_verified = research_verified

    def generate(self, quotes: dict[str, Quote]) -> tuple[list[Signal], list[dict]]:
        if not self.research_verified:
            observations = []
            for quote in quotes.values():
                if quote.change_pct is not None and abs(quote.change_pct) >= 5:
                    observations.append({
                        "code": quote.code, "name": quote.name,
                        "level": "weak_watch", "data_time": quote.data_time,
                        "reason": "价格波动较大，仅作行情观察；研究策略尚未验证，不能构成买卖建议。",
                    })
            return [], observations
        # 生产策略接入点：必须由通过门禁的策略产物替换，不能在此处临时猜测信号。
        return [], []

    @staticmethod
    def no_verified_strategy_notice() -> dict:
        return {
            "status": "locked",
            "message": "尚无通过独立样本外验证的策略；系统仅展示真实行情与观察项，不生成买卖建议。",
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
