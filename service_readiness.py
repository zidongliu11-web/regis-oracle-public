"""Canonical readiness names shared by the 5100 and 5200 services."""

from __future__ import annotations


def capability_readiness(
    *,
    operational_ready: bool,
    research_product_ready: bool | None,
    trade_signal_product_ready: bool | None,
) -> dict[str, object]:
    """Return shared capability names without conflating infrastructure.

    ``trade_signal_product_ready`` means a validated strategy-signal product
    is available for manual decision support.  Alert workers, quotes and
    notification infrastructure have their own readiness fields.
    """
    return {
        "operational_ready": bool(operational_ready),
        "research_product_ready": research_product_ready,
        "trade_signal_product_ready": trade_signal_product_ready,
        # Neither service connects to a broker or submits orders.
        "executable_ready": False,
        "execution_mode": "manual_decision_support_only",
        "readiness_semantics": (
            "trade_signal_product_ready means validated strategy signals for "
            "manual decision support, not alert infrastructure or broker "
            "execution; executable_ready is a separate capability."
        ),
    }
