"""Fixed daily emotion-pattern research; never a production signal provider.

Rules are local hypotheses, not renamed promotions of rejected strategies.
Only completed daily bars are accepted. The three entry patterns share a
momentum/reversal testing family; their five features are not five proven alphas.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


ENGINE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


FACTORS = [
    {"id": "range_contraction_5_20", "name": "区间收缩", "formula": "负的近5日平均振幅 / 近20日平均振幅", "characteristic": "值越高表示近期振幅相对收缩；不单独构成突破买点。"},
    {"id": "breakout_distance_20", "name": "突破距离", "formula": "复权收盘 / 前20日复权最高价 - 1", "characteristic": "描述突破位置，不把涨得越远解释为越值得追买。"},
    {"id": "close_acceptance", "name": "收盘承接", "formula": "(收盘-最低) / (最高-最低)", "characteristic": "收盘越接近日内上沿值越高；零振幅时不可计算，不假定承接。"},
    {"id": "pullback_volume_3_20", "name": "回踩量能", "formula": "负的前3日均成交额 / 此前20日均成交额", "characteristic": "研究回踩时是否缩量；不是资金净流入或买盘强度。"},
    {"id": "industry_excess_20", "name": "行业相对强度", "formula": "个股20日收益 - 同日PIT行业成分20日收益中位数", "characteristic": "比较股票与其当时行业，不用今天的行业成分回填历史。"},
]
PLAYBOOKS = [
    {
        "id": "nr7_mainline_breakout", "name": "主线窄幅蓄势突破", "kind": "entry_research",
        "states": ["RISK_ON"], "state_names": ["情绪上升"], "family": "趋势突破",
        "characteristic": "前一日为近7日最窄振幅，随后放量突破此前20日高点，研究收缩后的扩张。",
        "entry": "主线确认；完成日线收盘突破，成交额至少为前20日均值的1.2倍；只研究次日开盘，不用当日收盘假设成交。",
        "stop": "信号日最低价", "exit": "收盘跌破风险线、达到2R或满5个交易日，下一可成交开盘退出；情绪离开上升时退出复核。",
        "related_rejected_family": "与既有动量及突破家族有关，独立性未通过，不获得新的独立样本外资格。",
    },
    {
        "id": "mainline_dry_pullback", "name": "主线缩量回踩再启动", "kind": "entry_research",
        "states": ["RISK_ON"], "state_names": ["情绪上升"], "family": "趋势回踩",
        "characteristic": "60日趋势向上，前三日回落且缩量，低点守住20日均线后重新突破前一日高点。",
        "entry": "主线确认；前三日均额不超过此前20日均额的0.8倍；当日完成收盘转强后研究次日开盘。",
        "stop": "信号日及前三日最低价", "exit": "收盘跌破风险线、达到2R或满5个交易日，下一可成交开盘退出；情绪离开上升时退出复核。",
        "related_rejected_family": "与既有龙头分歧转强、趋势回踩有关；不是把失败的5分钟战法换名投产。",
    },
    {
        "id": "repair_failed_breakdown", "name": "箱体假跌破收复", "kind": "entry_research",
        "states": ["STABILIZING"], "state_names": ["弱修复"], "family": "失败突破反转",
        "characteristic": "盘中跌破前20日箱体下沿但收盘收复，且收在当日振幅上四分之一，研究下破失败后的修复。",
        "entry": "只在弱修复且主线确认时研究；完成日线低点下破、收盘收复后，次日开盘再判断成交条件。",
        "stop": "信号日最低价", "exit": "收盘跌破风险线、达到2R或满5个交易日，下一可成交开盘退出；情绪离开弱修复时退出复核。",
        "related_rejected_family": "与既有短期反转家族有关；需要比较无形态过滤的反转对照，不能事后选择赚钱情绪。",
    },
    {
        "id": "climax_failed_extension", "name": "高潮冲高回落退出复核", "kind": "exit_review",
        "states": ["OVERHEATED"], "state_names": ["高潮拥挤"], "family": "持仓退出",
        "characteristic": "创新高后长上影、收盘转弱且放量，提示已有持仓复核；不是做空或新入场策略。",
        "entry": "不新开仓；创新高，上影超过日内振幅一半，收盘低于前收，成交额至少为前20日均值的1.5倍。",
        "stop": "沿用已有持仓风险线，不用新观察覆盖原计划", "exit": "下一可交易时段结合真实可卖股数复核；T+1、跌停与未成交仓位仍保留。",
        "related_rejected_family": "风险观察规则，不能计入可买入策略数量。",
    },
]
REQUIRED = {
    "code", "trade_date", "session_index", "open", "high", "low", "close",
    "amount", "adj_factor", "industry_code", "trade_status", "is_st", "adjustment_trusted",
}


def research_catalog() -> dict:
    rules = {
        "schema": "emotion_pattern_research.v1", "version": 1,
        "playbooks": deepcopy(PLAYBOOKS), "factors": deepcopy(FACTORS),
        "entry_pattern_count": 3, "exit_review_count": 1, "factor_definition_count": 5,
        "status": "implemented_research_hypotheses_not_validated",
        "production_strategy_count": 0, "production_factor_count": 0,
        "generates_buy_or_sell_alert": False, "automatic_trading": False,
        "validation": {
            "status": "not_run", "performance": None,
            "design": "按事先定义情绪分别评价；时间用于顺序训练、验证及未见数据验证，不用年份替代情绪。",
            "controls": "同规则不加情绪路由、同情绪不加形态过滤；归入关联家族记录多重试验。",
            "costs": "独立验证时复用A股成交引擎：手续费、最低佣金、税费、滑点10/20/30bp及流动性约束；不能用因子IC冒充账户净收益。",
            "required": "规则与输入冻结、家族重复检查、成本后验证、未见数据或明确标注历史复用、前向验证；未完成前不接入正式提醒。",
        },
        "execution": {
            "entry_time": "signal_close_to_next_session_open", "maximum_open_gap": 0.02,
            "maximum_initial_stop_distance": 0.08, "holding_sessions": 5,
            "profit_target_R": 2.0, "signal_expires": "next_session_open",
            "position": "研究快照不生成股数；正式计划另需真实账户、有效风险线、可成交行情与准入证据。",
        },
        "sector_rule": "PIT行业至少12个有效成分；20日收益中位数为正、站上20日均线比例不少于60%；按行业收益排序取前两名。不是盘中同槽板块确认。",
        "history_requirement": "至少61个连续交易日的可信价格；停牌、缺行或未完成日线不补值。",
    }
    rules["rule_sha256"] = hashlib.sha256(json.dumps(
        rules, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return rules


def compute_features(rows: pd.DataFrame, *, as_of: str) -> pd.DataFrame:
    """Pure, past-only feature calculation on canonical PIT research rows."""
    missing = REQUIRED - set(rows.columns)
    if missing:
        raise ValueError(f"research_columns_missing:{','.join(sorted(missing))}")
    cutoff = pd.Timestamp(as_of)
    if cutoff.tzinfo is None:
        raise ValueError("as_of_timezone_required")
    cutoff = cutoff.tz_convert("Asia/Shanghai")
    frame = rows.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="raise").dt.normalize()
    if frame["trade_date"].dt.tz is not None:
        raise ValueError("trade_date_must_be_local_calendar_date")
    completed_at = frame["trade_date"].dt.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=15)
    frame = frame.loc[completed_at.le(cutoff)].copy()
    frame["code"] = frame["code"].astype(str)
    if not frame["code"].str.fullmatch(r"\d{6}").all():
        raise ValueError("research_stock_code_invalid")
    if frame.duplicated(["trade_date", "code"]).any():
        raise ValueError("research_stock_date_duplicate")
    frame = frame.sort_values(["code", "trade_date"], kind="stable").reset_index(drop=True)
    numeric = ["open", "high", "low", "close", "amount", "adj_factor", "session_index"]
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if not np.isfinite(frame["session_index"]).all() or frame["session_index"].mod(1).ne(0).any():
        raise ValueError("research_session_index_invalid")
    calendar = frame[["trade_date", "session_index"]].drop_duplicates().sort_values("trade_date")
    if calendar["trade_date"].duplicated().any() or calendar["session_index"].diff().dropna().le(0).any():
        raise ValueError("research_session_identity_conflict")
    prices = frame[["open", "high", "low", "close"]]
    valid = (
        np.isfinite(prices).all(axis=1) & prices.gt(0).all(axis=1)
        & frame.high.ge(prices.max(axis=1)) & frame.low.le(prices.min(axis=1))
        & np.isfinite(frame.adj_factor) & frame.adj_factor.gt(0)
        & np.isfinite(frame.amount) & frame.amount.gt(0)
        & frame.trade_status.eq(1) & frame.adjustment_trusted.eq(True)
    )
    for name in ("open", "high", "low", "close"):
        frame[f"a_{name}"] = (frame[name] * frame.adj_factor).where(valid)
    frame["valid_amount"] = frame.amount.where(valid)
    by_code = frame.groupby("code", sort=False)

    def roll(column, window, method="mean", shift=0):
        values = by_code[column].shift(shift) if shift else frame[column]
        windows = values.groupby(frame.code, sort=False).rolling(window, min_periods=window)
        return getattr(windows, method)().reset_index(level=0, drop=True).reindex(frame.index)

    frame["day_range"] = frame.a_high - frame.a_low
    frame["ma20"] = roll("a_close", 20)
    frame["high20_before"] = roll("a_high", 20, "max", 1)
    frame["low20_before"] = roll("a_low", 20, "min", 1)
    frame["amount20_before"] = roll("valid_amount", 20, shift=1)
    frame["ret20"] = frame.a_close / by_code.a_close.shift(20) - 1
    frame["ret60"] = frame.a_close / by_code.a_close.shift(60) - 1
    frame["range_contraction_5_20"] = -roll("day_range", 5) / roll("day_range", 20).replace(0, np.nan)
    frame["breakout_distance_20"] = frame.a_close / frame.high20_before - 1
    frame["close_acceptance"] = (frame.a_close - frame.a_low) / frame.day_range.replace(0, np.nan)
    frame["pullback_volume_3_20"] = -roll("valid_amount", 3, shift=1) / roll("valid_amount", 20, shift=4)
    frame["history_ready"] = roll("a_close", 61, "count").eq(61) & (
        frame.session_index - by_code.session_index.shift(60)
    ).eq(60)
    frame["eligible"] = frame.history_ready & valid & frame.is_st.eq(False) & frame.industry_code.notna()
    frame["above_ma20"] = frame.a_close.gt(frame.ma20).where(frame.eligible)
    frame["industry_return_input"] = frame.ret20.where(frame.eligible)
    industry = frame.groupby(["trade_date", "industry_code"], observed=True)
    frame["industry_median20"] = industry.industry_return_input.transform("median")
    frame["industry_excess_20"] = frame.ret20 - frame.industry_median20
    frame["industry_count"] = industry.industry_return_input.transform("count")
    frame["industry_breadth"] = industry.above_ma20.transform("mean")
    sectors = frame[["trade_date", "industry_code", "industry_median20", "industry_count", "industry_breadth"]].drop_duplicates()
    sectors = sectors.loc[sectors.industry_count.ge(12) & sectors.industry_median20.gt(0) & sectors.industry_breadth.ge(0.6)]
    sectors = sectors.sort_values(["trade_date", "industry_median20", "industry_code"], ascending=[True, False, True]).groupby("trade_date", sort=False).head(2)
    confirmed = pd.MultiIndex.from_frame(sectors[["trade_date", "industry_code"]])
    frame["sector_confirmed"] = pd.MultiIndex.from_frame(frame[["trade_date", "industry_code"]]).isin(confirmed)
    # Only fixed pattern predicates use features; no formal-factor registry gate.
    frame["nr7_mainline_breakout"] = (
        by_code.day_range.shift(1).le(roll("day_range", 7, "min", 1))
        & frame.a_close.gt(frame.high20_before)
        & frame.amount.ge(1.2 * frame.amount20_before)
    )
    frame["mainline_dry_pullback"] = (
        frame.ret60.gt(0) & by_code.a_close.shift(1).lt(by_code.a_close.shift(4))
        & frame.pullback_volume_3_20.ge(-0.8)
        & roll("a_low", 3, "min", 1).ge(by_code.ma20.shift(1))
        & frame.a_close.gt(by_code.a_high.shift(1))
    )
    frame["repair_failed_breakdown"] = frame.a_low.lt(frame.low20_before) & frame.a_close.gt(frame.low20_before) & frame.close_acceptance.ge(0.75)
    frame["climax_failed_extension"] = (
        frame.a_high.gt(frame.high20_before) & frame.a_close.lt(by_code.a_close.shift(1))
        & (frame.a_high - frame[["a_open", "a_close"]].max(axis=1)).gt(frame.day_range * 0.5)
        & frame.amount.ge(1.5 * frame.amount20_before)
    )
    frame["pullback_stop"] = roll("a_low", 4, "min") / frame.adj_factor
    for factor in FACTORS:
        frame[factor["id"]] = frame[factor["id"]].where(frame.eligible)
    return frame


def build_research_candidates(features: pd.DataFrame, states: pd.DataFrame) -> pd.DataFrame:
    """Return research conditions, not orders, holdings changes or alert events."""
    needed = {"trade_date", "confirmed_state"}
    if not needed.issubset(states.columns):
        raise ValueError("research_market_states_missing")
    states = states[list(needed)].copy()
    states["trade_date"] = pd.to_datetime(states.trade_date, errors="raise").dt.normalize()
    if states.trade_date.duplicated().any():
        raise ValueError("research_market_state_duplicate")
    frame = features.merge(states, on="trade_date", how="left", validate="many_to_one")
    candidates = []
    for playbook in PLAYBOOKS:
        selected = frame.loc[frame.eligible & frame[playbook["id"]] & frame.confirmed_state.isin(playbook["states"])].copy()
        if playbook["kind"] == "entry_research":
            selected = selected.loc[selected.sector_confirmed].copy()
        selected["risk_line"] = selected.pullback_stop if playbook["id"] == "mainline_dry_pullback" else selected.low
        if playbook["kind"] == "entry_research":
            risk = selected.close - selected.risk_line
            selected = selected.loc[risk.gt(0) & risk.div(selected.close).le(0.08)].copy()
        else:
            selected["risk_line"] = np.nan  # Never overwrite the existing holding's stop.
        selected["playbook_id"] = playbook["id"]
        selected["playbook_name"] = playbook["name"]
        selected["kind"] = playbook["kind"]
        selected["reference_close"] = selected.close
        selected["maximum_entry_open"] = selected.close * 1.02 if playbook["kind"] == "entry_research" else np.nan
        selected["profit_target_R"] = 2.0 if playbook["kind"] == "entry_research" else np.nan
        selected["holding_sessions"] = 5 if playbook["kind"] == "entry_research" else np.nan
        selected["suggested_shares"] = None
        selected["production_eligible"] = False
        selected["generates_alert"] = False
        candidates.append(selected)
    columns = ["trade_date", "code", "industry_code", "confirmed_state", "playbook_id", "playbook_name", "kind", "reference_close", "maximum_entry_open", "risk_line", "profit_target_R", "holding_sessions", "suggested_shares", "production_eligible", "generates_alert"] + [f["id"] for f in FACTORS]
    return pd.concat(candidates, ignore_index=True)[columns].sort_values(["trade_date", "playbook_id", "industry_excess_20", "code"], ascending=[True, True, False, True], na_position="last").reset_index(drop=True)


def research_entry_check(candidate: dict, opening_bar: dict, *, expected_session: str) -> dict:
    """Check next-session paper-entry conditions; never manufacture a fill."""
    from quantqc.ashare import opening_auction_executability

    result = {"eligible_for_execution_replay": False, "actual_fill": False, "generates_alert": False}
    playbook = next((p for p in PLAYBOOKS if p["id"] == candidate.get("playbook_id")), None)
    if not playbook or playbook["kind"] != "entry_research":
        return {**result, "reason": "not_an_entry_playbook"}
    if (str(opening_bar.get("trade_date")) != expected_session
        or str(candidate.get("code")) != str(opening_bar.get("code"))
        or pd.Timestamp(expected_session) <= pd.Timestamp(candidate["trade_date"])):
        return {**result, "reason": "next_session_identity_mismatch"}
    try:
        price, risk, cap = (float(opening_bar["open"]), float(candidate["risk_line"]), float(candidate["maximum_entry_open"]))
    except (ValueError, TypeError, KeyError):
        return {**result, "reason": "entry_inputs_missing"}
    if not all(np.isfinite(v) and v > 0 for v in (price, risk, cap)) or not risk < price <= cap or (price - risk) / price > 0.08:
        return {**result, "reason": "price_or_risk_boundary_failed"}
    if opening_bar.get("no_price_limit") is not False:
        return {**result, "reason": "ordinary_price_limit_identity_required"}
    if opening_bar.get("is_st") is not False:
        return {**result, "reason": "risk_warning_stock_excluded"}
    gate = opening_auction_executability(
        code=str(candidate["code"]), trade_date=expected_session, side="buy",
        open_price=price, prev_close=opening_bar.get("prev_close"),
        tradable=opening_bar.get("trade_status") == 1,
        is_risk_warning=opening_bar.get("is_st") is not False, no_price_limit=False,
    )
    if gate.get("executable") is not True:
        return {**result, "reason": gate.get("reason") or "auction_not_executable"}
    return {**result, "eligible_for_execution_replay": True,
            "risk_line": risk, "profit_target": price + 2 * (price - risk),
            "reason": "paper_conditions_met_not_a_fill"}


def research_exit_check(*, playbook_id: str, entry_price: float, risk_line: float,
                        completed_close: float, held_sessions: int, state: str) -> dict:
    """Close-confirmed exit intent; execution, inventory and costs remain separate."""
    playbook = next((p for p in PLAYBOOKS if p["id"] == playbook_id), None)
    if not playbook or playbook["kind"] != "entry_research":
        raise ValueError("entry_playbook_required")
    if not all(np.isfinite(v) and v > 0 for v in (entry_price, risk_line, completed_close)) or risk_line >= entry_price:
        raise ValueError("position_risk_identity_invalid")
    if type(held_sessions) is not int or held_sessions < 0:
        raise ValueError("held_sessions_invalid")
    reasons = []
    if completed_close <= risk_line:
        reasons.append("risk_line_breached")
    if completed_close >= entry_price + 2 * (entry_price - risk_line):
        reasons.append("two_R_target")
    if held_sessions >= 5:
        reasons.append("five_session_time_exit")
    if state not in playbook["states"]:
        reasons.append("emotion_route_invalidated")
    return {"exit_intent": bool(reasons), "reasons": reasons,
            "execution_time": "next_executable_session_open" if reasons else None,
            "actual_fill": False, "suggested_shares": None, "generates_alert": False}
