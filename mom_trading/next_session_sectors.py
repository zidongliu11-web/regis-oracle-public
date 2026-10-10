"""Read a completed session's sector tape for preparation, never live admission."""
from datetime import date, datetime, time, timedelta
from . import cloud_sector_observations as boards
from . import cloud_sector_constituents as members
from .cloud_research_worker import ROOT, read_json

BOUNDARY = {"research_only": True, "historical_only": True,
            "generates_alert": False, "changes_trade_authority": False}


def _market_cohort(row):
    # The supplier also lists first-board, touch-board and activity cohorts as
    # concepts. They describe trading behavior, not an industry/theme mainline.
    return row.get("kind") == "concept" and str(row.get("name") or "").startswith((
        "昨日涨停", "昨日连板", "昨日炸板", "昨日首板", "昨日触板",
        "昨日高振幅", "昨日高换手", "昨日打二板",
    ))


def _preparation_pool(snapshot, original):
    """Project a verified full tape, retaining the frozen ranks and original top 3.

    Do not edit the frozen adapters/capture contracts to change an old receipt.
    This separate preparation view cannot feed live confirmation or admission.
    """
    from .market_sector_strategy_adapter import _sector_rows
    rows, _ = _sector_rows(snapshot)
    eligible = [row for row in rows if row["constituent_count"] >= 5 and row["advance_coverage_pct"] is not None]
    by_kind = {kind: [row["change_pct"] for row in eligible if row["kind"] == kind] for kind in ("industry", "concept")}
    for row in eligible:
        values = by_kind[row["kind"]]
        row["change_rank_pct"] = round(100 * (sum(v < row["change_pct"] for v in values)
                                                   + .5 * sum(v == row["change_pct"] for v in values)) / len(values), 3)
    selected = [row for row in eligible if row["change_pct"] > 0 and row["advance_coverage_pct"] >= 60 and row["change_rank_pct"] >= 90]
    selected.sort(key=lambda row: (-row["change_rank_pct"], -row["advance_coverage_pct"], -row["change_pct"], row["kind"], row["code"]))
    keys = ("kind", "code", "name", "change_pct", "advance_coverage_pct", "change_rank_pct", "data_as_of")
    project = lambda row: tuple(row.get(key) for key in keys)
    if [project(row) for row in selected[:3]] != [project(row) for row in original]:
        raise ValueError("preparation_projection_disagrees_with_frozen_observation")
    return selected


def session_dates(root, now):
    if now.tzinfo is None:
        raise ValueError("timezone_required")
    now = now.astimezone(boards.SH_TZ)
    calendar = read_json(root / "reports/current_market_calendar.json")
    start, end = (date.fromisoformat(calendar[key]) for key in ("coverage_start", "coverage_end"))
    sessions = [date.fromisoformat(value) for value in calendar["trading_dates"]]
    if (not start <= now.date() <= end or not sessions or sessions != sorted(set(sessions))
            or sessions[0] < start or sessions[-1] > end):
        raise ValueError("calendar_coverage_invalid")
    # Same finalized-daily cutoff as before; never relabel a 15:xx tick as a bar.
    cutoff = now.date() if now.time() >= time(16) else now.date()-timedelta(days=1)
    previous = [day for day in sessions if day <= cutoff]
    future = [day for day in sessions if day > now.date()
              or (day == now.date() and now.time() < time(9, 30))]
    if not previous or not future:
        raise ValueError("reference_or_planning_session_missing")
    return previous[-1].isoformat(), future[0].isoformat()


def build(*, root=ROOT, now=None):
    now = now or datetime.now(boards.SH_TZ)
    result = {"schema": "next_session_sector_preparation.v1", "status": "waiting_calendar",
              "reference_day": None, "planning_day": None, "rows": [],
              "sector_watch_rows": [], "market_cohort_rows": [], **BOUNDARY}
    try:
        day, planning_day = session_dates(root, now)
        now = now.astimezone(boards.SH_TZ)
        result.update(reference_day=day, planning_day=planning_day, status="waiting_sources")
    except (OSError, ValueError, KeyError, TypeError):
        return {**result, "summary": "预案交易日历未完整覆盖当前日、上一完成日和准备日；不猜自然日或沿用未知日期。"}

    directory = root / "reports/cloud_sector_observations" / day
    paths = sorted(directory.glob("[0-4][0-9].json"))
    close_path = root / "reports/cloud_sector_close" / day / "latest.json"
    if not paths and not close_path.exists():
        return {**result, "summary": f"{day}没有板块留档原件；待补的是实际板块观察，不是市场没有主线。"}
    try:
        if close_path.exists():
            from .cloud_sector_close import read_packet
            packet, assessment = read_packet(root, day, now)
            rows = assessment["rows"]
            result.update(observation_mode="after_close", source_as_of=assessment["source_as_of"],
                          observed_at=packet["observed_at"], board_packet_id=packet["packet_id"],
                          source=packet["snapshot"]["source"])
        else:
            packet = boards.read_packet(paths[-1], day)
            stamp = datetime.fromisoformat(packet["observed_at"])
            if packet["errors"] or stamp > now:
                raise ValueError("last_observation_not_verified")
            rows = boards.inspect(packet["snapshot"], stamp)[1]["observed_sectors"]
            result.update(observed_at=packet["observed_at"], board_packet_id=packet["packet_id"],
                          source=packet["snapshot"]["source"])
        # Use only this exact parent bucket. A later failure or missing child
        # never revives an older constituent list or its alleged core stocks.
        details, child = {}, {}
        try:
            if close_path.exists():
                from .cloud_sector_close_members import read_packet as read_close_members
                child = read_close_members(root, day, now)
            else:
                child = members.read_packet(root / "reports/cloud_sector_constituents" / day / paths[-1].name,
                                            root=root, day=day)
            if child["board_packet_id"] != packet["packet_id"]:
                raise ValueError("parent_changed")
            details = {(row["sector"]["kind"], row["sector"]["code"]): row
                       for row in child["assessments"]}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass  # Explicit per-board missing-member status below, not success.
        pool = _preparation_pool(packet["snapshot"], rows)
        output = []
        for row in pool:
            detail = details.get((row["kind"], row["code"])) or {}
            metrics = detail.get("metrics") if not detail.get("errors") else None
            cohort = _market_cohort(row)
            output.append({key: row.get(key) for key in (
                "code", "kind", "name", "change_pct", "advance_coverage_pct", "change_rank_pct",
                "up_count", "down_count", "data_as_of")})
            output[-1].update(
                market_cohort=cohort,
                scope_note="按昨日涨停、触板、打板或振幅、换手等行为形成的行情分组，不是产业或题材主线；须另行确认真实所属板块。" if cohort else None,
                constituent_status=("verified_preparation" if close_path.exists() else "verified_observation") if metrics else "missing_or_invalid",
                constituent_metrics=metrics,
                constituent_observed_at=child.get("observed_at") if detail else None,
                constituent_packet_id=child.get("packet_id") if detail else None,
                constituent_scope_note=("成员清单属于实际获取时点，不代表上一收盘时的成员；成分与板块的源时点分别记录，不是盘中同时证据。" if close_path.exists() else None),
                confirmation_condition="新交易日10:30–10:45或14:00–14:15，以同一观察时点完整来源重新核验：涨幅>0、上涨/(上涨+下跌)≥60%、同类排名分位≥90、至少5家有涨跌计数的成分。",
                abandon_condition="上述条件任一不满足，移出达标观察名单；来源失败则标记待核，不能称为板块退潮。已有买卖计划按各自失效规则处理。")
        themes = [row for row in output if not row["market_cohort"]][:3]
        cohorts = [row for row in output if row["market_cohort"]][:3]
        return {**result, "status": "observed" if output else "no_matching_observation", "rows": output[:3],
                "sector_watch_rows": themes, "market_cohort_rows": cohorts,
                "sector_watch_status": "observed" if themes else "cohorts_only" if cohorts else "no_matching_observation",
                "selection_scope": "preparation_only_preserving_original_ranks",
                "selection_note": ("原始前三榜单保留不变；从同一完整原件中单列达标行业/题材，行情分组不占用其观察名额。"
                                   if themes else "来源完整，但达标对象只有行情分组，尚无达标行业/题材观察。" if cohorts
                                   else "完整来源中没有满足既有涨幅、广度与排名条件的行业/题材或行情分组。")
                                  + " 不重算剔除分组后的排名，不放宽阈值；仅用于预案，不改变盘中冻结确认规则。",
                "summary": f"为{result['planning_day']}准备，引用{day}" + ("盘后获取的历史板块原件；" if close_path.exists() else "最后一份板块桶原件；") + (
                    "这是观察名单，不是已确认的下一日主线。" if output else "来源完整，但没有同时满足既有涨幅、广度与排名条件的观察板块。"),
                "observation_boundary": ("盘后获取的供应商历史快照，不是盘中当时已知证据，也未独立认定为交易所最终收盘值。" if close_path.exists()
                                         else "留档时刻不一定是收盘。") + "上涨比例分母不含平盘。成分成交额前三只是核心观察，不认定龙头。"}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {**result, "summary": f"{day}最后板块桶或盘后原件未通过校验；不跳过失败桶选择更早的强势名单。"}
