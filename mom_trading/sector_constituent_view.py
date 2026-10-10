"""Source-time display projection, separate from frozen capture identities.

Changing the collector module would invalidate its whole-file contract on all
existing board/member tapes. Keep ingestion and packet verification unchanged;
correct the website reader without rewriting or re-admitting those originals.
"""
from datetime import datetime
from pathlib import Path

from . import cloud_sector_constituents as members
from . import cloud_sector_observations as boards
from .cloud_research_worker import ROOT


def observation(*, root=ROOT, now=None):
    root = Path(root)
    now = now or datetime.now(boards.SH_TZ)
    empty = {"status": "unavailable", "rows": [], "historical_only": True,
             "summary": "同日成分证据尚未就绪；不能判断板块是普涨还是少数股票拉动。",
             **members.BOUNDARY}
    if now.tzinfo is None:
        return {**empty, "summary": "观察时点缺少时区，不推断当前成分行情。"}
    now = now.astimezone(boards.SH_TZ)
    day = now.date().isoformat()
    current_bucket = boards.bucket(now)
    upper = current_bucket if current_bucket is not None else (
        -1 if now.hour * 60 + now.minute < 570 else 23 if now.hour < 13 else 47)
    try:
        paths = sorted(path for path in (root / "reports/cloud_sector_observations" / day).glob("[0-4][0-9].json")
                       if int(path.stem) <= upper)
        if not paths:
            return empty
        # The latest available board owns the child. Never skip a failed or
        # missing child to show a previous success as current evidence.
        board, sectors = members.parent(root, day, int(paths[-1].stem))
        if datetime.fromisoformat(board["observed_at"]) > now:
            return empty
        if not sectors:
            return {**empty, "status": "no_candidates", "observed_at": board["observed_at"],
                    "board_packet_id": board["packet_id"],
                    "summary": "本次完整板块来源没有行业／题材进入固定规则的成分观察名单；这不是成分核验通过，也不等于市场没有机会。"}
        packet = members.read_packet(root / "reports/cloud_sector_constituents" / day / paths[-1].name,
                                     root=root, day=day)
        if datetime.fromisoformat(packet["observed_at"]) > now:
            return empty
        rows = packet["assessments"]
        # Parent ranks need the complete cross-section, not just a fresh leader.
        source_times = [datetime.fromisoformat(row["data_as_of"])
                        for kind in ("industries", "concepts") for row in board["snapshot"][kind]]
        source_times += [datetime.fromisoformat(row["metrics"][key]) for row in rows if row["metrics"]
                         for key in ("source_as_of_min", "source_as_of_max")]
        oldest, newest = min(source_times), max(source_times)
        age = (now - oldest).total_seconds()
        return {**members.BOUNDARY, "status": "observed" if all(not row["errors"] for row in rows) else "partial",
                "rows": rows, "observed_at": packet["observed_at"], "checked_at": now.isoformat(),
                "board_packet_id": packet["board_packet_id"], "member_packet_id": packet["packet_id"],
                "source_as_of_min": oldest.isoformat(), "source_as_of_max": newest.isoformat(),
                "source_age_seconds": age,
                "historical_only": current_bucket is None or packet["bucket"] != current_bucket or age > 180,
                "summary": "成分逐页核验；以板块及有效成分中最早的源时点核对180秒时效，不用采集完成时间延长行情寿命。历史记录保留，不作为当前确认。"}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {**empty, "summary": "最新板块或同桶成分原件缺失、格式或身份校验失败；不沿用之前的成分结论。"}
