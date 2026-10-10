"""Two separate research clocks; neither changes the original opening rule.

The completed-day variant uses the final scheduled 14:55 bucket, not a claim
about the closing auction. Later intraday evidence cannot backfill that cohort.
"""
from datetime import datetime
from pathlib import Path

from . import mainline_system as mainline

DAILY = "closing_bucket_next_open"
LIVE = "live_mainline_next_open"
RISK = "current_risk_observation"
PROTOCOLS = {
    DAILY: {"name": "尾盘主线＋完成日线 → 次日开盘研究", "kind": "entry_research",
            "description": "只用信号日14:55观察桶及此前连续原件，配合同日完成日线。不是收盘竞价确认；缺末桶不沿用更早主线。情绪、价格与可选因子都须在次日09:30之前实际可得。"},
    LIVE: {"name": "盘中主线＋既有日线 → 时点冲突检查", "kind": "entry_research",
           "description": "保留原盘中观察口径；后来确认的主线不能回填此前开盘。尚无独立盘中触发与成交协议，不延长日线信号有效期。"},
    RISK: {"name": "当前观察 → 已有风险复核", "kind": "risk_review",
           "description": "不等待新仓主线或因子，不改变原风险线、实际库存和可卖数量要求。"},
}


def selected(spec):
    # Old saved records remain the original live interpretation, never silently
    # acquire the new completed-day hypothesis when loaded from the archive.
    return spec.get("timing_protocol", LIVE)


def source_for(spec, context):
    return context.get("completed_mainline_lifecycle" if selected(spec) == DAILY else "mainline_lifecycle") or {}


def usable(source, protocol):
    if source.get("status") != "observed":
        return False
    if protocol == DAILY:
        return (source.get("historical_only") is True and source.get("timing_protocol") == DAILY
                and source.get("final_bucket_verified") is True
                and source.get("observation_day") == source.get("signal_day")
                and (source.get("emotion_context") or {}).get("relation") == "same_completed_session")
    return protocol == LIVE and source.get("historical_only") is False


def completed_context(root, now, emotion):
    """Re-read the immutable signal-day prefix, excluding later session files."""
    root = Path(root)
    result = {"status": "input_incomplete", "rows": [], "historical_only": True,
              "timing_protocol": DAILY, "final_bucket_verified": False, **mainline.BOUNDARY}
    try:
        if now.tzinfo is None:
            raise ValueError("timezone")
        now = now.astimezone(mainline.boards.SH_TZ)
        reference, _ = mainline.session_dates(root, now)
        emotion = emotion or {}
        day = emotion.get("data_as_of")
        if emotion.get("status") != "computed" or day != reference:
            return {**result, "summary": "尚无上一完成交易日的已核验情绪；不拼接其他日期的尾盘主线。"}
        cutoff = datetime.fromisoformat(day + "T15:00:00+08:00")
        if now < cutoff:
            raise ValueError("daily_not_closed")
        result.update(signal_day=day, evidence_cutoff_at=cutoff.isoformat(), checked_at=now.isoformat())
        path = root / "reports/cloud_sector_observations" / day / "47.json"
        frame = mainline._frame(root, path, day)
        if (frame.get("error") or frame.get("bucket") != 47 or not frame.get("member_packet_id")
                or not frame.get("members") or frame["stamp"] > cutoff or frame["member_stamp"] > cutoff):
            raise ValueError("final_bucket_incomplete")
        # The existing calendar treats 16:00 as the completed-daily baseline.
        # Board readers already reject captures outside the intraday buckets;
        # the exact final parent/child above must both predate 15:00.
        trajectory = mainline.build(root=root, now=cutoff.replace(hour=16), emotion=emotion)
        if (trajectory.get("status") != "observed" or trajectory.get("board_packet_id") != frame["packet_id"]
                or trajectory.get("member_packet_id") != frame["member_packet_id"]):
            raise ValueError("prefix_identity_changed")
        return {**trajectory, **{k: v for k, v in result.items() if k not in {"status", "rows"}},
                "final_bucket_verified": True, "available_at": max(frame["stamp"], frame["member_stamp"]).isoformat(),
                "summary": "信号日最后计划观察桶及此前主线轨迹；不是实时主线或收盘竞价，次日开盘前仍须核验全部输入的可得时间。"}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {**result, "summary": "信号日14:55末桶、当时成分或连续原件未通过核验；不以更早主线或次日盘中观察替代。"}
