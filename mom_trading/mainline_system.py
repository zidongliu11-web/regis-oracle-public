"""Causal mainline candidate trajectories from immutable board/member tapes.

No historical constituent reconstruction, outcomes, account or trading writes.
The old frozen board router is untouched. Thresholds here are research rules.
"""
from datetime import datetime
import hashlib
from pathlib import Path
from statistics import median

from . import cloud_sector_observations as boards
from . import cloud_sector_constituents as members
from .cloud_research_worker import ROOT, read_json
from .market_sector_constituent_forward import _fetch_sector_constituents
from .market_sector_strategy_adapter import _sector_rows
from .next_session_sectors import _preparation_pool, _market_cohort, session_dates

SCHEMA = "mainline_system_candidate.v1"
BOUNDARY = {"research_only": True, "production_eligible": False, "generates_alert": False,
            "changes_trade_authority": False, "forward_sample": False, "account_returns_computed": False}
STAGES = {
    "emerging": ("初现主线候选", "等待至少3个连续达标观察、跨度不少于10分钟，核验同成分扩散和固定核心股联动。"),
    "persistent": ("持续联动候选", "持续跟踪板块广度、固定核心股与其余成分；交给情绪和战法规则进一步研究。"),
    "diverging": ("主线候选分歧", "核查核心股走弱或其余成分扩散不足，暂停把它作为持续联动输入。"),
    "repairing": ("分歧后联动恢复", "保留原核心股，继续观察恢复能否持续；单次恢复不授予买入资格。"),
    "displaced": ("移出前三观察名额", "仍满足广度规则但排名被其他板块超过；不能仅据此认定退潮。"),
    "invalidated": ("板块候选条件失效", "本次完整来源中已不满足原涨幅、广度或排名条件；已有计划按自身退出规则处理。"),
    "input_incomplete": ("核心联动待核验", "等待当时完整成分原件；数据缺失不能解释为板块退潮。"),
}
REASONS = {
    "first_observation": "首份有效观察",
    "observation_gap": "缺少相邻观察或跨越午间休市",
    "board_source_changed": "板块来源或横截面身份发生变化",
    "board_time_not_advanced": "板块来源时间未推进",
    "members_missing": "当时成分原件缺失或未通过校验",
    "membership_changed": "成分代码或名称发生变化",
    "member_time_not_advanced": "部分成分来源时间未推进",
    "cumulative_reset": "部分成分累计成交额或成交量倒退",
    "core_unavailable": "不足3个有成交额的核心观察股",
    "duration_pending": "连续观察次数或跨度尚未达标",
    "breadth_failed": "含平盘的成分上涨比例不足60%",
    "ex_core_failed": "剔除固定核心后的上涨比例不足60%",
    "core_weakened": "原核心股相对起点价格联动不足",
    "no_increment": "相邻观察没有新增成交额",
}


def _key(row):
    return row["kind"], row["code"]


def _frame(root, path, day):
    packet = boards.read_packet(path, day)
    stamp = datetime.fromisoformat(packet["observed_at"])
    if packet["errors"]:
        return {"bucket": packet["bucket"], "stamp": stamp, "error": "board_source_invalid"}
    original = boards.inspect(packet["snapshot"], stamp)[1]["observed_sectors"]
    pool = _preparation_pool(packet["snapshot"], original)
    all_rows = _sector_rows(packet["snapshot"])[0]
    frame = {"bucket": packet["bucket"], "stamp": stamp, "error": None,
             "packet_id": packet["packet_id"], "source": packet["snapshot"].get("source"),
             "universe": (packet["snapshot"].get("source"), sorted((r["kind"], r["code"], r["name"]) for r in all_rows)),
             "values": {_key(r): r for r in all_rows}, "eligible": {_key(r) for r in pool},
             "themes": {_key(r): r for r in pool if not _market_cohort(r)},
             "cohorts": [r for r in pool if _market_cohort(r)][:3], "members": {}}
    frame["selected"] = dict(list(frame["themes"].items())[:3])
    try:
        child = members.read_packet(root / "reports/cloud_sector_constituents" / day / path.name, root=root, day=day)
        frame["member_stamp"] = datetime.fromisoformat(child["observed_at"])
        frame["member_packet_id"] = child["packet_id"]
        for capture, assessment in zip(child["captures"], child["assessments"]):
            if assessment["errors"]:
                continue
            pages = capture["pages"]
            parsed = _fetch_sector_constituents(capture["sector"], lambda fs, page: pages[page-1], capture_date=stamp.date())
            frame["members"][_key(capture["sector"])] = {r["stock_code"]: r for r in parsed["rows"]}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        frame["member_error"] = "members_missing"
    return frame


def _break_reason(previous, current, key):
    if previous is None or previous.get("error"):
        return "first_observation"
    if current["bucket"] != previous["bucket"]+1 or current["bucket"] == 24:
        return "observation_gap"
    if current["universe"] != previous["universe"]:
        return "board_source_changed"
    old = previous["values"].get(key)
    if old is None or current["values"][key]["data_as_of"] <= old["data_as_of"]:
        return "board_time_not_advanced"
    before, after = previous["members"].get(key), current["members"].get(key)
    if not before or not after:
        return "members_missing"
    if {(k, v["stock_name"]) for k, v in before.items()} != {(k, v["stock_name"]) for k, v in after.items()}:
        return "membership_changed"
    if any(after[k]["data_as_of"] <= v["data_as_of"] for k, v in before.items()):
        return "member_time_not_advanced"
    if any(after[k][f] < v[f] for k, v in before.items() for f in ("amount", "volume")):
        return "cumulative_reset"
    return None


def _candidate(frame, key, previous, old_state):
    row = frame["values"][key]
    if key not in frame["selected"]:
        stage = "displaced" if key in frame["eligible"] else "invalidated"
        return {"sector": row, "stage": stage, "label": STAGES[stage][0], "next_condition": STAGES[stage][1],
                "consecutive_observations": 0, "fixed_core": [], "metrics": None, "reasons": []}
    stocks = frame["members"].get(key)
    reason = _break_reason(previous, frame, key)
    linked = bool(old_state and old_state.get("anchor") and reason is None)
    anchor = old_state["anchor"] if linked else {"at": frame.get("member_stamp"), "rows": stocks}
    count = old_state["consecutive_observations"]+1 if linked else 1
    result = {"sector": row, "consecutive_observations": count, "anchor": anchor,
              "comparison_break": reason, "reasons": [], "fixed_core": [], "metrics": None}
    if not stocks:
        stage = "input_incomplete"
        result.update(anchor=None, consecutive_observations=0, reasons=["members_missing"])
    else:
        baseline = anchor["rows"]
        ranked = sorted((r for r in baseline.values() if r["amount"] > 0), key=lambda r: (-r["amount"], r["stock_code"]))[:3]
        core_codes = {r["stock_code"] for r in ranked}
        rest = [r for code, r in stocks.items() if code not in core_codes]
        core = [{"stock_code": r["stock_code"], "stock_name": r["stock_name"], "selected_at": anchor["at"].isoformat(),
                 "anchor_price": r["price"], "current_price": stocks[r["stock_code"]]["price"],
                 "price_change_since_selection_pct": (stocks[r["stock_code"]]["price"] / r["price"]-1)*100,
                 "data_as_of": stocks[r["stock_code"]]["data_as_of"]} for r in ranked]
        breadth = sum(r["change_pct"] > 0 for r in stocks.values()) / len(stocks)*100
        rest_breadth = sum(r["change_pct"] > 0 for r in rest)/len(rest)*100 if rest else None
        amount = sum(r["amount"] for r in stocks.values())
        increment = sum(r["amount"]-previous["members"][key][code]["amount"] for code, r in stocks.items()) if linked else None
        seconds = (frame["member_stamp"]-anchor["at"]).total_seconds()
        failures = []
        if len(core) < 3 or not rest:
            failures.append("core_unavailable")
        if breadth < 60:
            failures.append("breadth_failed")
        if rest_breadth is None or rest_breadth < 60:
            failures.append("ex_core_failed")
        if core and (median(r["price_change_since_selection_pct"] for r in core) < 0
                     or sum(r["price_change_since_selection_pct"] >= 0 for r in core) < 2):
            failures.append("core_weakened")
        if increment is not None and increment <= 0:
            failures.append("no_increment")
        aligned = old_state.get("aligned_observations", 0)+1 if linked and not failures else 0
        aligned_at = (old_state["aligned_since"] if linked and old_state.get("aligned_observations") else frame["member_stamp"].isoformat()) if aligned else None
        aligned_seconds = (frame["member_stamp"]-datetime.fromisoformat(aligned_at)).total_seconds() if aligned_at else 0
        ready = aligned >= 3 and aligned_seconds >= 600
        stage = "input_incomplete" if "core_unavailable" in failures else "diverging" if failures else (
            "persistent" if ready else "repairing" if linked and old_state["stage"] in {"diverging", "repairing"} else "emerging")
        result.update(fixed_core=core, reasons=failures or ([] if ready else ["duration_pending"]),
                      aligned_observations=aligned, aligned_since=aligned_at,
                      metrics={"count": len(stocks), "advance_including_flat_pct": breadth,
                               "ex_fixed_core_advance_pct": rest_breadth,
                               "median_change_pct": median(r["change_pct"] for r in stocks.values()),
                               "fixed_core_amount_share_pct": sum(stocks[k]["amount"] for k in core_codes)/amount*100 if amount > 0 else None,
                               "interval_amount_yuan": increment, "observation_span_seconds": seconds,
                               "aligned_span_seconds": aligned_seconds,
                               "amount_semantics": "同成分、逐股累计额不倒退的相邻快照差额；不是逐笔成交流，也不证明放量优势。"})
    result.update(stage=stage, label=STAGES[stage][0], next_condition=STAGES[stage][1])
    return result


def build(*, root=ROOT, now=None, emotion=None):
    root = Path(root)
    now = now or datetime.now(boards.SH_TZ)
    result = {"schema": SCHEMA, "status": "input_incomplete", "rows": [], "market_cohorts": [],
              "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "constituent_schema": members.SCHEMA,
              "stage_definitions": [{"id": k, "name": v[0], "next_condition": v[1]} for k, v in STAGES.items()],
              "historical_only": True, "persistent_mainline_confirmed": False, **BOUNDARY}
    try:
        reference_day, planning_day = session_dates(root, now)
        now = now.astimezone(boards.SH_TZ)
        day = now.date().isoformat()
        calendar = read_json(root / "reports/current_market_calendar.json")
        result.update(as_of=now.isoformat(), observation_day=day, reference_day=reference_day,
                      calendar_source=calendar.get("source"), calendar_coverage_checked=True)
        emotion = emotion or {}
        emotion_day = emotion.get("data_as_of")
        relation = ("previous_completed_session" if emotion_day == reference_day and emotion_day < day else
                    "same_completed_session" if emotion_day == reference_day == day else "unmatched_or_missing")
        result["emotion_context"] = {"relation": relation, "data_as_of": emotion_day,
                                     "phase": emotion.get("phase") if relation != "unmatched_or_missing" else None,
                                     "label": emotion.get("label") if relation != "unmatched_or_missing" else None,
                                     "intraday_emotion_confirmed": False}
        if day not in calendar["trading_dates"] or now.hour*60+now.minute < 570:
            from .next_session_sectors import build as preparation
            result.update(status="awaiting_session", preparation=preparation(root=root, now=now),
                          summary="尚无本交易时段的主线确认；上一完成日观察仅用于准备，开盘后用新原件重新核验。")
            return result
        paths = sorted((root / "reports/cloud_sector_observations" / day).glob("[0-4][0-9].json"))
        current_bucket = boards.bucket(now)
        # Exclude later buckets before opening them: historical prefix replay
        # must not depend on future captures, their outcomes or corruption.
        upper = current_bucket if current_bucket is not None else 23 if now.hour < 13 else 47
        frames = []
        for path in paths:
            if int(path.stem) > upper:
                continue
            try:
                frame = _frame(root, path, day)
                if frame["stamp"] > now:
                    continue
                if frame.get("member_stamp") and frame["member_stamp"] > now:
                    frame["members"] = {}
                    frame.pop("member_packet_id", None)
                frames.append(frame)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                frames.append({"bucket": int(path.stem), "error": "board_identity_invalid"})
        if not frames or frames[-1].get("error"):
            return {**result, "summary": "最新板块原件缺失或未通过核验；清空持续主线结论，不跳过失败记录。"}
        states, previous = {}, None
        for frame in frames:
            if frame.get("error"):
                states, previous = {}, frame
                continue
            wanted = set(frame["selected"]) | set(states)
            states = {key: _candidate(frame, key, previous, states.get(key)) for key in sorted(wanted)
                      if key in frame["values"] and (key in frame["selected"] or states[key]["stage"] not in {"displaced", "invalidated"})}
            previous = frame
        last = frames[-1]
        for row in states.values():
            row.pop("anchor", None)
            row["reason_labels"] = [REASONS[k] for k in row["reasons"]]
            row["comparison_break_label"] = REASONS.get(row.get("comparison_break"))
        # Quotes inside a capture may be up to 180 seconds old already.
        source_times = [datetime.fromisoformat(row["data_as_of"]) for row in last["values"].values()]
        source_times += [datetime.fromisoformat(row["data_as_of"]) for values in last["members"].values() for row in values.values()]
        historical = current_bucket is None or (now-min(source_times)).total_seconds() > 180 or last["bucket"] != current_bucket
        result.update(status="observed", rows=list(states.values()), historical_only=historical,
                      observed_at=last["stamp"].isoformat(), board_packet_id=last["packet_id"],
                      member_packet_id=last.get("member_packet_id"), market_cohorts=last["cohorts"],
                      summary="以当时成分和固定核心股解释主线生命周期；候选持续性尚不代表有交易优势。",
                      rule_summary="保留原同类排名：涨幅>0、广度≥60%、排名分位≥90、至少5家；行业/题材单列前三。连续达标≥3次且跨度≥10分钟：含平盘广度及剔除固定核心广度均≥60%，3个原核心至少2个价格不低于起点，中位变化非负，相邻有新增成交额。首份记录只建立基准。",
                      evidence_boundary="核心股在连续片段首份完整成分中按成交额选定，随后固定跟踪；新片段重新选定。跨午休、缺桶、成员变化或源时间停滞不接续。历史快照差价不是可成交收益；跨日持续、涨停炸板和龙头资格仍待独立证据。")
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {**result, "summary": "主线交易日历或输入格式未通过核验；不能用自然日补齐主线证据。"}
