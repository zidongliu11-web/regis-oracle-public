"""Input-only emotion detail and the original site's shared research workflow.

The existing daily classifier and admitted routes remain separately versioned.
This candidate interpretation uses no future returns, account or trading API.
"""
from datetime import date, datetime
from math import isfinite, prod
from statistics import fmean
from zoneinfo import ZoneInfo


SCHEMA = "emotion_system_context.v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")
PERCENTILES = (
    "up_ratio_pct", "above_ma20_ratio_pct", "new_high_minus_low_ratio_pct",
    "yesterday_limit_continuation_ratio_pct", "highest_board", "closed_limit_ratio_pct",
    "eod_failed_limit_ratio_pct", "top_5pct_amount_share_pct",
)
PHASES = {
    "unknown": ("情绪细分输入不足", "补齐当时可得的市场输入；持仓风险独立复核。"),
    "extreme_stress": ("冰点／极端压力", "观察压力是否缓和；不把冰点本身当作抄底信号。"),
    "retreat": ("强势退潮", "强势基线出现走弱，优先检查已有持仓的退出条件。"),
    "weak_decline": ("弱势低迷", "观察广度和接力恢复；暂无进攻型研究路线。"),
    "early_repair": ("低迷后初次修复", "只建立修复观察，等待持续性和主线证据。"),
    "repair_fading": ("修复减弱", "修复力度下降，检查修复失败和已有风险线。"),
    "weak_repair": ("弱修复延续", "研究收复失地后的承接，不能等同于新一轮上升。"),
    "upturn_pending": ("上升待确认", "当日转强而慢速基线尚未确认，先准备候选。"),
    "rising": ("上升延续", "结合持续主线研究蓄势突破与缩量回踩。"),
    "strong_divergence": ("强势分歧", "广度、接力或失败压力出现分歧，观察修复或转入退潮。"),
    "divergence_repair": ("分歧后转强", "复核原主线是否恢复扩散，以及核心股是否重新转强。"),
    "overheat_pending": ("急修复伴随拥挤，待确认", "当日强度骤升且成交集中，但慢速基线仍弱；观察修复持续性，不据此切换进攻仓位。"),
    "crowded_climax": ("高潮拥挤", "复核持仓冲高回落与退出，不把拥挤当作追高理由。"),
}
BOUNDARY = {"research_only": True, "production_eligible": False,
            "generates_alert": False, "automatic_trading": False,
            "account_returns_computed": False, "suggested_shares": None}


def _number(value):
    return float(value) if type(value) in (int, float) and isfinite(value) else None


def _axes(row):
    features = row.get("features") or {}
    values = [_number(features.get(name + "_percentile")) for name in PERCENTILES]
    if any(value is None or not 0 <= value <= 1 for value in values):
        return None
    coverage = row.get("coverage") or {}
    if coverage.get("raw_counts_diagnostic_only") is True:
        return None
    # Reuse the classifier's actual formula and thresholds, not a score fitted
    # to strategy outcomes. Do not infer calibrated probabilities from ranks.
    from research.build_market_state_feature_epoch_v1 import _raw_state
    calculated = _raw_state(dict(zip((name + "_percentile" for name in PERCENTILES), values)))
    if (features.get("raw_state") != calculated["raw_state"]
            or features.get("confirmed_state") not in {"UNKNOWN", "RISK_OFF", "STABILIZING", "RISK_ON", "OVERHEATED"}):
        return None
    return {**calculated, "confirmed_state": features["confirmed_state"]}


def _phase(current, previous):
    if current is None:
        return "unknown"
    raw, confirmed = current["raw_state"], current["confirmed_state"]
    if current["extreme_risk_off"]:
        return "extreme_stress"
    if raw == "RISK_OFF":
        prior_strong = previous and previous["raw_state"] in {"RISK_ON", "OVERHEATED"}
        return "retreat" if prior_strong or confirmed in {"RISK_ON", "OVERHEATED"} else "weak_decline"
    if raw == "OVERHEATED":
        return "crowded_climax" if confirmed in {"RISK_ON", "OVERHEATED"} else "overheat_pending"
    if raw == "RISK_ON":
        if current["B_strength"] < .5 or current["L_strength"] < .5 or current["F_stress"] >= .8:
            return "strong_divergence"
        if confirmed != "RISK_ON":
            return "upturn_pending"
        if (previous and previous["confirmed_state"] in {"RISK_ON", "OVERHEATED"}
                and previous["raw_state"] == "STABILIZING"):
            return "divergence_repair"
        return "rising"
    if confirmed in {"RISK_ON", "OVERHEATED"}:
        return "strong_divergence"
    if previous and previous["raw_state"] == "RISK_OFF":
        return "early_repair"
    if previous and current["directional_strength"] < previous["directional_strength"]:
        return "repair_fading"
    return "weak_repair"


def detailed_emotion(rows, calendar, *, as_of):
    """Use only completed rows at as_of; gaps break change and duration claims.

    `calendar` must be the caller's verified official sessions. Historical
    calls and daily calls use this exact function. It has no outcome argument.
    """
    stamp = datetime.fromisoformat(as_of)
    if stamp.tzinfo is None:
        raise ValueError("emotion_as_of_timezone_required")
    stamp = stamp.astimezone(SHANGHAI)
    if (not calendar or calendar != sorted(set(calendar))
            or any(date.fromisoformat(day).isoformat() != day for day in calendar)):
        raise ValueError("emotion_official_calendar_invalid")
    completed = [day for day in calendar if datetime.fromisoformat(day + "T15:00:00+08:00") <= stamp]
    if not completed:
        raise ValueError("emotion_no_completed_session")
    target = completed[-1]
    by_day = {}
    calendar_days = set(calendar)
    for row in rows:
        day = row.get("trade_date")
        if day not in calendar_days:
            raise ValueError("emotion_row_outside_calendar")
        if day > target:
            continue
        if day in by_day:
            raise ValueError("emotion_duplicate_session")
        by_day[day] = row
    current = _axes(by_day[target]) if target in by_day else None
    prior_day = completed[-2] if len(completed) > 1 else None
    previous = _axes(by_day[prior_day]) if prior_day in by_day else None
    phase = _phase(current, previous)
    label, action = PHASES[phase]
    axes = []
    for key, name in (("B_strength", "市场广度强度"), ("L_strength", "涨停梯队与接力强度"),
                      ("F_stress", "炸板失败压力"), ("C_stress", "成交集中拥挤"),
                      ("directional_strength", "情绪综合强度")):
        value = current[key] * 100 if current else None
        before = previous[key] * 100 if previous else None
        axes.append({"id": key, "name": name, "value": round(value, 3) if value is not None else None,
                     "previous_value": round(before, 3) if before is not None else None,
                     "change_points": round(value - before, 3) if value is not None and before is not None else None})
    # These are breadth observations, not an index's return or a bull/bear label.
    tail = completed[-5:]
    breadth = [(by_day.get(day, {}).get("B") or {}) for day in tail]
    ma = [_number(row.get("above_ma20_ratio_pct")) for row in breadth]
    highs = [_number(row.get("new_high_minus_low_ratio_pct")) for row in breadth]
    trend = "unknown"
    if (len(tail) == 5 and all(_axes(by_day.get(day, {})) for day in tail)
            and all(value is not None and 0 <= value <= 100 for value in ma)
            and all(value is not None and -100 <= value <= 100 for value in highs)):
        trend = "broadening" if fmean(ma) > 50 and fmean(highs) > 0 else (
            "weakening" if fmean(ma) < 50 and fmean(highs) < 0 else "mixed")
    run_days = 0
    if current:
        for index in range(len(completed) - 1, -1, -1):
            day = completed[index]
            prior = completed[index - 1] if index else None
            value = _axes(by_day.get(day, {}))
            if value is None or _phase(value, _axes(by_day.get(prior, {}))) != phase:
                break
            run_days += 1
    # Keep the official-session slots, including gaps. A missing session must
    # remain visible rather than being joined into an apparent uninterrupted run.
    trajectory = []
    for offset, day in enumerate(completed[-5:], start=max(0, len(completed) - 5)):
        point = _axes(by_day.get(day, {}))
        prior = _axes(by_day.get(completed[offset - 1], {})) if offset else None
        point_phase = _phase(point, prior) if point else None
        trajectory.append({"trade_date": day, "status": "observed" if point else "missing",
                           "axes": {key: round(point[key] * 100, 3) if point else None
                                    for key in ("B_strength", "L_strength", "F_stress", "C_stress")},
                           "raw_state": point["raw_state"] if point else None,
                           "confirmed_state": point["confirmed_state"] if point else None,
                           "phase": point_phase,
                           "phase_label": PHASES[point_phase][0] if point_phase else None,
                           "previous_input_complete": prior is not None})
    price_direction = []
    for window in (1, 5, 20):
        days = completed[-window:]
        moves = [_number((by_day.get(day, {}).get("B") or {}).get("median_return_pct")) for day in days]
        complete = (len(days) == window and all(_axes(by_day.get(day, {})) for day in days)
                    and all(move is not None and move > -100 for move in moves))
        change = (prod(1 + move / 100 for move in moves) - 1) * 100 if complete else None
        price_direction.append({"sessions": window, "median_move_chain_pct": round(change, 4) if change is not None else None,
                                "direction": "unknown" if change is None else "up" if change > 0 else "down" if change < 0 else "flat"})
    names = {"UNKNOWN": "尚未确认", "RISK_OFF": "风险关闭", "STABILIZING": "弱修复", "RISK_ON": "情绪上升", "OVERHEATED": "高潮拥挤"}
    raw_metrics = []
    for group, key, name, unit in (
        ("B", "up_count", "上涨家数", "家"), ("B", "down_count", "下跌家数", "家"),
        ("B", "flat_count", "平盘家数", "家"), ("B", "above_ma20_ratio_pct", "站上20日均线比例", "%"),
        ("L", "first_board_count", "首板", "家"), ("L", "second_board_count", "二板", "家"),
        ("L", "third_board_count", "三板", "家"), ("L", "four_plus_board_count", "四板及以上", "家"),
        ("L", "highest_board", "最高连板", "板"), ("L", "yesterday_limit_continuation_ratio_pct", "昨日涨停延续比例", "%"),
        ("F", "limit_touch_count", "触及涨停", "家"), ("F", "closed_limit_count", "收盘封板", "家"),
        ("F", "eod_failed_limit_count", "触板未封住", "家"), ("F", "eod_failed_limit_ratio_pct", "日线炸板比例", "%"),
        ("C", "total_amount_yi", "观察范围成交额", "亿元"), ("C", "top_5pct_amount_share_pct", "成交额前5%股票占比", "%"),
        ("C", "turnover_median_pct", "换手率中位数", "%"),
        ("C", "turnover_p95_pct", "换手率95分位", "%"),
        ("C", "limit_stock_amount_share_pct", "收盘涨停股成交额占比", "%"),
        ("C", "cross_section_return_dispersion_pct", "当日个股收益横截面离散度", "%"),
    ):
        value = _number((by_day.get(target, {}).get(group) or {}).get(key)) if current else None
        raw_metrics.append({"id": key, "name": name, "value": value, "unit": unit})
    return {"schema": "detailed_emotion_candidate.v1", "status": "computed" if current else "input_incomplete",
            "as_of": as_of, "data_as_of": target, "phase": phase, "label": label,
            "research_action": action, "consecutive_observed_sessions": run_days,
            "previous_official_session": prior_day, "previous_input_complete": previous is not None,
            "confirmed_baseline": current["confirmed_state"] if current else "UNKNOWN",
            "raw_baseline": current["raw_state"] if current else "UNKNOWN", "axes": axes,
            "raw_baseline_label": names[current["raw_state"]] if current else names["UNKNOWN"],
            "confirmed_baseline_label": names[current["confirmed_state"]] if current else names["UNKNOWN"],
            "baseline_agreement": bool(current and current["raw_state"] == current["confirmed_state"]),
            "raw_metrics": raw_metrics,
            "recent_official_sessions": trajectory,
            "recent_official_sessions_definition": "最近至多5个已完成官方交易日逐日四轴、原始态与慢速确认态；阶段按同日与前一官方日可得输入推导。缺原件保留空槽，缺日前后不视为连续迁移；不是未来预测或连续收益曲线。",
            "price_direction": price_direction,
            "price_direction_definition": "按当时有效股票的每日涨跌幅中位数连乘，描述1／5／20日市场涨跌；成分随日变化，不是可投资指数或账户收益。",
            "breadth_trend": {"state": trend, "sessions": 5,
                "label": {"unknown": "五日广度输入不足", "broadening": "五日广度偏强", "weakening": "五日广度偏弱", "mixed": "五日广度分化"}[trend],
                "definition": "最近5个完整交易日：站上20日均线比例均值与新高减新低比例均值；不是指数涨跌收益。"},
            "method": "广度、接力与失败压力沿用既有公式；细分阶段使用当日原始态、慢速确认态和上一完整交易日。候选规则尚未验证交易优势。",
            "confidence_semantics": "输入分位强度及变化，不是胜率；缺行不跨日补连续阶段。",
            **BOUNDARY}


def system_contract():
    """One sequence for the original-site development work and later replay."""
    return [
        {"id": "emotion", "name": "情绪细分与变化", "requirement": "广度、接力、失败压力、拥挤、连续阶段；日线基线与盘中证据分别标时。"},
        {"id": "mainline", "name": "主线与核心股确认", "requirement": "比较相对强度、扩散、量能、跨时段持续性、核心股联动与失效；当时成分留档。单次领涨不能代表持续主线。"},
        {"id": "playbooks", "name": "策略与战法匹配", "requirement": "每项战法明确适用情绪、主线阶段、触发、禁止、退出及证据状态；退潮防守独立于新仓筛选。"},
        {"id": "factors", "name": "因子研发与条件收益", "requirement": "先固定机制、公式、方向和适用阶段，再比较同情绪同主线阶段的成本后增量；保留失败与无样本。"},
        {"id": "timing", "name": "买卖点与失效", "requirement": "区分准备、触发、有效期和实际成交；退出含风险线、情绪改变、主线失效及时间退出，未成交继续记录。"},
        {"id": "risk", "name": "仓位与风险约束", "requirement": "真实现金与可卖库存为基础，同时约束总仓、单股、行业重叠、相关性、风险线、回撤及成交容量；无可靠风险线不生成股数。"},
        {"id": "portfolio_replay", "name": "最后进行总体回测", "requirement": "前六步规则冻结后，用同一情绪与主线路由跑一个共享现金和库存的账户，计入日期化交易规则、真实费用与未成交。"},
    ]


def public_context(*, root=None, now=None, joint=None, include_dependent_context=True):
    """Read the completed emotion producer independently of entry feature gaps."""
    from pathlib import Path
    from . import cloud_market_emotion as emotion
    from . import cloud_pattern_features as feature
    from .cloud_research_worker import ROOT, read_json
    from .cloud_pattern_inputs import sha
    root = Path(root) if root is not None else ROOT
    stamp = now or datetime.now(SHANGHAI)
    result = {"schema": SCHEMA, "status": "input_incomplete", "workflow": system_contract(),
              "phase_definitions": [{"id": key, "name": value[0], "action": value[1]} for key, value in PHASES.items()],
              "implementation_sha256": sha(Path(__file__)), "emotion": None,
              "commission": {"status": "broker_statement_pending", "assumed_rate": .00025,
                  "assumed_minimum_yuan": 5, "label": "当前万2.5、最低5元是研究假设；待核对同花顺所接券商的实际收费、规费是否已含及生效日。"},
              "overall_backtest": {"status": "scheduled_after_system_logic", "started_by_this_context": False}, **BOUNDARY}
    try:
        if stamp.tzinfo is None:
            raise ValueError("emotion_as_of_timezone_required")
        daily = read_json(root / "reports/cloud_market_data.json")
        target, calendar = daily.get("target_date"), daily.get("calendar_dates") or []
        if not target or not calendar or calendar[-1] != target:
            raise ValueError("emotion_official_calendar_invalid")
        directory = root / "reports/cloud_market_emotion" / target
        manifest = read_json(directory / "manifest.json")
        reference = manifest.get("snapshot") or {}
        snapshot = read_json(feature._bound(root, reference, directory))
        source = snapshot.get("source_contract") or {}
        from .cloud_pattern_route import _implementations
        if (snapshot.get("schema") != emotion.SCHEMA or snapshot.get("data_as_of") != target
                or manifest.get("target_date") != target or manifest.get("schema") != emotion.SCHEMA
                or source.get("implementation_sha256") != emotion.IMPLEMENTATION_SHA256
                or source.get("master_sha256") != daily.get("master_sha256")
                or source.get("calendar_sha256") != feature.digest(calendar)
                or any(snapshot.get(key) != value for key, value in emotion.BOUNDARY.items())):
            raise ValueError("emotion_snapshot_identity_invalid")
        source_root = Path(__file__).resolve().parents[1]
        modules = source.get("source_modules") or {}
        required = {"research/market_rules.py", "research/build_state_conditioned_mainline_inputs.py",
                    "research/build_market_state_feature_epoch_v1.py", "research/build_market_state_feature_epoch_v2.py",
                    "mom_trading/cloud_pattern_inputs.py", "mom_trading/cloud_pattern_features.py",
                    "mom_trading/emotion_pattern_research.py", "mom_trading/cloud_daily_revisions.py"}
        if set(modules) != required or any(sha(source_root / name) != modules[name] for name in required):
            raise ValueError("emotion_source_implementation_changed")
        observed = datetime.fromisoformat(snapshot["observed_at"])
        if observed.tzinfo is None or observed > stamp:
            raise ValueError("emotion_source_after_observation")
        references = snapshot.get("source_references") or {}
        if (not references or any(row.get("status") != "computed" for row in references.values())
                or emotion.snapshot_identity(references, sorted(references), source) != snapshot.get("input_identity")):
            raise ValueError("emotion_input_identity_invalid")
        detail = detailed_emotion(snapshot.get("rows") or [], calendar, as_of=stamp.isoformat())
        if detail["data_as_of"] != target:
            raise ValueError("emotion_daily_bar_not_completed")
        if snapshot.get("latest_state") != detail["confirmed_baseline"]:
            raise ValueError("emotion_baseline_identity_mismatch")
        result.update(status=detail["status"], emotion=detail,
                      source={"data_as_of": target, "captured_at": snapshot["observed_at"],
                              "snapshot_sha256": reference["sha256"], "input_identity": snapshot["input_identity"],
                              "scope": "沪深主板与创业板日线研究；盘中情绪和当时主线另行确认。"})
        # Reuse a checked same-day joint observation; no fallback sector list.
        joint = joint or {}
        bound = (joint.get("status") == "complete" and joint.get("data_as_of") == target
                 and joint.get("market_input_identity") == snapshot["input_identity"]
                 and joint.get("source_implementations_sha256") == _implementations())
        result["mainline"] = {"status": "single_day_research_candidates" if bound and joint.get("sector_evaluated") else "not_evaluated",
                              "sectors": joint.get("research_sectors", []) if bound and joint.get("sector_evaluated") else [],
                              "persistent_mainline_confirmed": False,
                              "reason": "日线行业筛选与持续主线分开；仍需跨时段延续、核心股和成交量的同源证据。"}
        result["playbooks"] = joint.get("applicable_playbooks", []) if bound else []
        result["playbooks_status"] = "existing_baseline_research_only" if bound else "joint_inputs_pending"
        result["fine_phase_routing_validated"] = False
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result["reason"] = str(exc) if isinstance(exc, ValueError) else "emotion_input_files_unavailable_or_invalid"
        result["summary"] = "情绪细分输入尚未核验；研发顺序保留，不能把缺失输入解释为冰点或无主线。"
    if not include_dependent_context:
        return result
    from .mainline_system import build as mainline_context
    result["mainline_lifecycle"] = mainline_context(root=root, now=stamp, emotion=result.get("emotion"))
    from .playbook_entry_timing import completed_context
    result["completed_mainline_lifecycle"] = completed_context(root, stamp, result.get("emotion"))
    from .playbook_development import development_context
    result["playbook_development"] = development_context(result, root=root)
    return result
