"""Research-only end-of-day market-state contract.

This module deliberately does not fetch data and does not produce a position
target.  It exposes which transparent inputs exist and keeps the state UNKNOWN
until the limit-up ladder (L) and failed-limit process (F) are available.
"""

from __future__ import annotations

from typing import Any


def _dimension(status: str, **values: Any) -> dict[str, Any]:
    return {"status": status, **values}


def build_eod_research_state(today: dict[str, Any]) -> dict[str, Any]:
    breadth = today.get("breadth") if isinstance(today, dict) else None
    breadth = breadth if isinstance(breadth, dict) else {}
    sentiment = today.get("sentiment") if isinstance(today, dict) else None
    sentiment = sentiment if isinstance(sentiment, dict) else {}
    funds = today.get("fund_flow") if isinstance(today, dict) else None
    funds = funds if isinstance(funds, dict) else {}

    breadth_ok = (
        breadth.get("status") == "ok"
        and breadth.get("up_ratio") is not None
        and bool(breadth.get("as_of"))
    )
    concentration_observed = (
        breadth_ok and breadth.get("weighted_turnover_pct") is not None
        and breadth.get("total_amount_yi") is not None
    )
    dimensions = {
        "B": _dimension(
            "available" if breadth_ok else "incomplete",
            as_of=breadth.get("as_of"), source=breadth.get("source"),
            up_ratio=breadth.get("up_ratio"), up=breadth.get("up"),
            down=breadth.get("down"), flat=breadth.get("flat"),
            sample_size=breadth.get("sample_size"),
            reason=(None if breadth_ok else "全市场广度覆盖、时点或完整性不足"),
        ),
        "L": _dimension(
            "unavailable", reason="缺少首板至高板梯队、最高板和昨日涨停延续率",
        ),
        "F": _dimension(
            "unavailable", reason="缺少触板未封、尾盘炸板及开板过程数据",
        ),
        "C": _dimension(
            "partial" if concentration_observed else "incomplete",
            turnover_pct=breadth.get("weighted_turnover_pct"),
            total_amount_yi=breadth.get("total_amount_yi"),
            reason=("已有透明成交与换手截面，但滚动拥挤分位尚未冻结"
                    if concentration_observed else "参考池成交/换手完整性不足，不给出拥挤度截面"),
        ),
        "P": _dimension(
            "observation_only", as_of=funds.get("as_of"),
            source=funds.get("source"),
            reason="第三方主力资金只展示；融资融券和公开资金PIT合同未完成",
        ),
    }
    missing_core = [key for key in ("B", "L", "F") if dimensions[key]["status"] != "available"]
    return {
        "schema_version": 1,
        "state": "UNKNOWN",
        "status": "research_only_incomplete",
        "as_of": breadth.get("as_of") or sentiment.get("as_of"),
        "dimensions": dimensions,
        "missing_core_dimensions": missing_core,
        "target_exposure_pct": None,
        "stock_score_role": "none",
        "position_control_role": "not_applied_until_oos_validation",
        "decision_eligible": False,
        "reason": "B/L/F核心证据未全部可用，市场情绪状态保持UNKNOWN；不改变个股评分或账户仓位。",
    }
