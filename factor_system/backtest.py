"""事件驱动 t+1 回测区（任务书第八部分·诚实适配 + P2-3 接 quantqc + P2-9 严格 A 股规则）。

规则（严格无未来函数）：
  - 信号：收盘 d 的因子横截面 → 选出多头分位（前 1/QUANTILE）
  - 执行：d+1 开盘（t+1 执行，收盘价做不了开盘交易）
      · 可选 previous_close_limit_no_chase_v1：以 d 日未复权收盘为限价，
        d+1 未复权开盘高于限价则取消，不替补、不顺延
      · 可选 previous_close_gap_2pct_no_chase_v1：允许 d+1 开盘最多高开 2%，
        超过则取消，其余执行与成本口径不变
  - 可交易性（P2-9 修复）：
      · 买入侧：执行开盘贴涨停（≥ 涨停价−0.5%）→ 买不进，剔除；
        用 未复权开盘 vs 当执行日源限价参考价；缺参考不成交
      · 卖出侧：到期平仓执行开盘 ≤ 跌停价+0.5% → 当日卖不出，该名顺延持仓一日再平
  - 持仓目标：过去 HOLD_DAYS 个信号日各贡献 1/H 目标权重，cohort 内候选等权，
    同一股票被多个信号 cohort 选中时权重累加；组合按当日净值每日再平衡，
    这是滚动 cohort 目标权重代理，不声称保存逐 cohort 买入 lot 到独立到期日
  - 整手执行（P2-9）：每日按 cohort 累加后的目标权重 floor(目标资金/价/100)*100 股建仓，
    实际市值权重入组合收益（未投入现金 0 收益，如实反映整手舍入拖累）
  - 现实持仓数（P-C）：目标分位内取因子值最高/最低 max_pos 名（默认 10，与成本
    模型 n_positions 对齐）。200k 账户等权切片可买整手；旧"全市场顶分位篮子"
    (~800 名) 整手切资金买不起，退化成空仓只计提成本——上限避免纸面失真
  - 价格：统一复权价（adj_open）算收益，未复权价仅用于可交易性与整手判断
  - 成本/容量：quantqc 同花顺口径——佣金万2.5 双边最低¥5/笔、印花税仅卖出
    0.05%、过户费 0.001% 双边、滑点可配置；成交受信号日前 ADV/参与率限制，
    并按实际参与率计提透明的平方根冲击压力成本
  - 区间：仅 OOS 段（2024-07-01 ~ 2025-12-31），IS 段绝不出现在回测里

诚实边界（8GB 单机）：成交以当日未复权开盘价代理，没有分钟/逐笔撮合。
  跨日持仓用复权开盘收益记账；为使整手的未复权成交名义与复权收益自融资一致，
  内部保存"复权单位" U=raw_shares×raw_open/adj_open。除权除息只改变股数等价换算，
  不制造虚假盈亏；卖出侧跌停按个股顺延建模。

产出：逐日组合收益序列 + 年化收益/Sharpe/最大回撤/胜率 + 等权市场基准对照。
"""

from __future__ import annotations

import hashlib
import json
import numpy as np
import pandas as pd

from . import config
from .data import Panel, raw_close_frame, limit_reference_frame

from quantqc import ashare as qa
from quantqc import costs as qc


def _limit_percentages(
    codes: pd.Index, index: pd.Index, *, trade_date: pd.Timestamp,
    is_st: pd.Series | None = None,
) -> pd.Series:
    """复用日期化共享规则；当前因子截面已排除上市不足400日的股票。"""
    st = (
        is_st.reindex(index).fillna(False).astype(bool)
        if is_st is not None else pd.Series(False, index=index, dtype=bool)
    )
    values: list[float] = []
    for code, row_index in zip(codes.astype(str), index):
        rule = qa.price_limit_rule(
            code, trade_date=trade_date, is_risk_warning=bool(st.loc[row_index]),
            no_price_limit=False,
        )
        values.append(float(rule["rate"]) if rule.get("valid") and rule.get("rate") is not None else np.nan)
    return pd.Series(values, index=index, dtype=float)


def _entry_unbuyable(
    entry_open: pd.Series, prev_close: pd.Series, codes: pd.Index, *,
    trade_date: pd.Timestamp, is_st: pd.Series | None = None,
) -> pd.Series:
    """按精确到0.01元的涨停价判断；缺价格或身份时保守不成交。"""
    st = (
        is_st.reindex(entry_open.index).fillna(False).astype(bool)
        if is_st is not None
        else pd.Series(False, index=entry_open.index, dtype=bool)
    )
    return pd.Series({
        idx: not qa.opening_auction_executability(
            code=str(code), trade_date=trade_date, side="buy",
            open_price=entry_open.get(idx), prev_close=prev_close.get(idx),
            tradable=True, is_risk_warning=bool(st.get(idx, False)),
            no_price_limit=False,
        )["executable"]
        for code, idx in zip(codes, entry_open.index)
    }, dtype=bool).reindex(entry_open.index, fill_value=True)


def _exit_unsellable(
    exit_open: pd.Series, prev_close: pd.Series, codes: pd.Index, *,
    trade_date: pd.Timestamp, is_st: pd.Series | None = None,
) -> pd.Series:
    """按精确跌停价判断；缺价格或身份时保守顺延。"""
    st = (
        is_st.reindex(exit_open.index).fillna(False).astype(bool)
        if is_st is not None
        else pd.Series(False, index=exit_open.index, dtype=bool)
    )
    return pd.Series({
        idx: not qa.opening_auction_executability(
            code=str(code), trade_date=trade_date, side="sell",
            open_price=exit_open.get(idx), prev_close=prev_close.get(idx),
            tradable=True, is_risk_warning=bool(st.get(idx, False)),
            no_price_limit=False,
        )["executable"]
        for code, idx in zip(codes, exit_open.index)
    }, dtype=bool).reindex(exit_open.index, fill_value=True)


def _round_trip_cost(costs: dict) -> float:
    """一次完整建仓+清仓的单向换手成本率。

    P2-3：接 quantqc 同花顺口径（最低 ¥5 佣金、印花税仅卖出、过户费）。
    旧的纯比例模型 commission*2+stamp+slippage*2 已废弃。
    """
    capital = costs.get("capital", 200_000.0)
    n_pos = costs.get("n_positions", 10)
    slippage = costs.get("slippage", qc.COSTS["slippage"])
    base = qc.cost_rate_for_portfolio(capital, n_pos, slippage)
    participation = costs.get("max_adv_participation")
    impact_bps = float(costs.get("impact_coefficient_bps", 0.0) or 0.0)
    impact = (impact_bps / 10_000.0 * np.sqrt(float(participation))
              if participation is not None and float(participation) > 0 else 0.0)
    return float(base + 2.0 * impact)


def _trade_fee_breakdown(notional: float, side: str, slippage: float,
                         impact_rate: float = 0.0) -> dict[str, float]:
    """单笔成交费用（元），与quantqc.costs.one_side_cost_rate 保持同一口径。"""
    if notional <= 0:
        return {"commission": 0.0, "transfer": 0.0, "stamp": 0.0,
                "slippage": 0.0, "market_impact": 0.0, "total": 0.0}
    base = qc.one_side_cost_breakdown(notional, side, slippage)
    commission = float(base["commission"])
    transfer = float(base["transfer"])
    stamp = float(base["stamp"])
    slippage_yuan = float(base["slippage"])
    impact_yuan = notional * max(0.0, float(impact_rate))
    total = float(base["total"]) + impact_yuan
    return {
        "commission": float(commission),
        "transfer": float(transfer),
        "stamp": float(stamp),
        "slippage": float(slippage_yuan),
        "market_impact": float(impact_yuan),
        "total": float(total),
    }


def _audit_sha256(audit: dict) -> str:
    """账本规范 JSON 哈希；调用方可用相同参数独立复核。"""
    payload = json.dumps(audit, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_shares(value: float) -> int | float:
    """整股用 int，仅除权等价换算产生小数时保留精确值。"""
    value = float(value)
    return int(round(value)) if abs(value - round(value)) < 1e-8 else value


def _json_float(value: object) -> float | None:
    """将价格转为严格 JSON 可序列化数字；缺失值保留为 null。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _market_vol_exposure_scale(panel: Panel) -> tuple[pd.Series, pd.Series]:
    """执行日可用的连续仓位系数与其依据的市场波动率。

    规则固定为 20 日、18% 年化目标、40%~100% 仓位且不加杠杆。执行日 t
    只使用截至 t-1 收盘的 PIT 等权市场收益；缺少 20 个观测时维持 100%，
    不把缺失伪装成低波动。
    """
    close = panel.close.astype(np.float64)
    daily = close.pct_change(fill_method=None)
    eligible = getattr(panel, "signal_eligible", None)
    if isinstance(eligible, pd.DataFrame):
        eligible = eligible.reindex(index=close.index, columns=close.columns)
        daily = daily.where(eligible.fillna(False).astype(bool))
    market_daily = daily.where(np.isfinite(daily)).mean(axis=1)
    observed_vol = (
        market_daily.rolling(20, min_periods=20).std(ddof=1)
        * np.sqrt(252.0)
    ).shift(1)
    scale = (0.18 / observed_vol.where(observed_vol > 0)).clip(0.40, 1.00)
    return scale.fillna(1.0).astype(float), observed_vol.astype(float)


def _build_execution_panel_frames(
    panel: Panel,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """构建只依赖 Panel 价格快照的执行矩阵；回测窗口由调用方另行切片。"""
    adj_open = panel.adj_open
    o2o = (adj_open.shift(-1) / adj_open).astype(np.float64) - 1.0
    raw_close = raw_close_frame(panel, required=False)
    if raw_close is None:
        # Old factor-only snapshots retain explicit decode compatibility.
        ratio = (adj_open / panel.open_.replace(0, np.nan)).astype(np.float64)
        raw_close = (panel.close / ratio).replace([np.inf, -np.inf], np.nan)
    elif (not raw_close.index.equals(adj_open.index)
          or not raw_close.columns.equals(adj_open.columns)
          or not panel.open_.index.equals(adj_open.index)
          or not panel.open_.columns.equals(adj_open.columns)):
        raise ValueError("raw_execution_open_axes_invalid")
    return (
        o2o,
        raw_close,
        adj_open.ffill(),
        panel.open_.ffill(),
        raw_close.ffill(),
    )


def _execution_panel_frames_cache_key(panel: Panel) -> tuple:
    """绑定 Panel 实例、输入快照和派生矩阵的全部价格依赖。"""
    return (
        id(panel),
        getattr(panel, "_panel_cache_key", None),
        id(panel.adj_open),
        id(panel.open_),
        id(panel.close),
        id(getattr(panel, "raw_close", None)),
        id(getattr(panel, "limit_reference", None)),
    )


def _execution_panel_frames(
    panel: Panel,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """复用同一只读 Panel 快照的全日期执行矩阵。"""
    raw_close_frame(panel, required=False)  # Validate present raw even on cache hits.
    limit_reference_frame(panel, required=False)
    key = _execution_panel_frames_cache_key(panel)
    cached = getattr(panel, "_execution_panel_frames_cache", None)
    if isinstance(cached, tuple) and len(cached) == 2 and cached[0] == key:
        return cached[1]
    frames = _build_execution_panel_frames(panel)
    # 只保留当前快照，输入矩阵被替换时不滞留上一组大型 DataFrame。
    setattr(panel, "_execution_panel_frames_cache", (key, frames))
    return frames


def run_backtest(panel: Panel, factor: pd.DataFrame,
                 oos_dates: list[pd.Timestamp],
                 hold: int = config.HOLD_DAYS,
                 q: int = config.QUANTILE,
                 costs: dict = None,
                 direction: float = 1.0,
                 capture_audit: bool = False) -> dict:
    """重叠 cohort 目标权重、每日再平衡、t+1 开盘执行的因子多头回测。

    每个信号日固定占 1/H 资金，cohort 内候选等权；同一股票在多个
    未到期 cohort 中重复入选时，其目标权重按各 cohort 贡献相加。

    direction 决定买哪一档（A 股禁做空，负向因子买底分位做多）：
      direction>0 → 买顶分位（因子值高 → 未来收益高）
      direction<0 → 买底分位（因子值高 → 未来收益低，反转做多）
    P2-8 修正：旧代码固定买顶分位，把负向因子反向交易了。"""
    if costs is None:
        costs = config.COSTS
    open_ = panel.adj_open
    participation_value = costs.get("max_adv_participation")
    participation = (float(participation_value)
                     if participation_value is not None else None)
    if participation is not None and not 0 < participation <= 1:
        raise ValueError("max_adv_participation 必须在 (0,1] 内")
    adv_window = int(costs.get("adv_window", 20))
    if participation is not None and adv_window < 2:
        raise ValueError("adv_window 必须至少为 2")
    impact_coefficient_bps = float(
        costs.get("impact_coefficient_bps", 0.0) or 0.0
    )
    if impact_coefficient_bps < 0:
        raise ValueError("impact_coefficient_bps 不得为负")
    prior_adv = (
        panel.amount.where(np.isfinite(panel.amount) & (panel.amount > 0))
        .rolling(adv_window, min_periods=adv_window).mean().shift(1)
        if participation is not None else None
    )

    def _liquidity_fill(requested: float, price: float, date: pd.Timestamp,
                        code: str, *, allow_odd_lot: bool = False) -> tuple[float, float | None]:
        """按信号日前 ADV 限制本次成交；缺失 ADV 时严格不成交。"""
        requested_int = max(0, int(float(requested)))
        if participation is None:
            accepted = qa.constrain_share_quantity(
                requested_int,
                side="sell" if allow_odd_lot else "buy",
                available_to_sell=requested_int if allow_odd_lot else None,
                full_liquidation=allow_odd_lot,
            )
            return float(accepted["accepted_shares"]), None
        adv = float(prior_adv.at[date, code]) if (
            prior_adv is not None and date in prior_adv.index
            and code in prior_adv.columns and pd.notna(prior_adv.at[date, code])
        ) else np.nan
        if not np.isfinite(adv) or adv <= 0 or not np.isfinite(price) or price <= 0:
            return 0.0, None
        max_shares = max(0, int(adv * participation / price))
        if requested_int <= max_shares:
            return float(requested_int), adv
        if allow_odd_lot:
            # 完整清仓中的零股只能一次申报；容量不足时整单等待，不能拆出分数股。
            return 0.0, adv
        accepted = qa.constrain_share_quantity(
            requested_int, side="buy", liquidity_cap_shares=max_shares,
        )
        return float(accepted["accepted_shares"]), adv

    def _impact_rate(notional: float, adv: float | None) -> float:
        if (participation is None or adv is None or not np.isfinite(adv)
                or adv <= 0 or notional <= 0):
            return 0.0
        actual_participation = min(1.0, notional / adv)
        return impact_coefficient_bps / 10_000.0 * np.sqrt(actual_participation)
    entry_rule = str(costs.get("entry_rule", "none") or "none")
    if entry_rule not in {
        "none", "previous_close_limit_no_chase_v1",
        "previous_close_gap_2pct_no_chase_v1",
    }:
        raise ValueError(f"不支持的买点规则: {entry_rule}")
    exposure_rule = str(costs.get("exposure_rule", "none") or "none")
    if exposure_rule == "none":
        exposure_scale = pd.Series(1.0, index=open_.index, dtype=float)
        exposure_observed_vol = pd.Series(np.nan, index=open_.index, dtype=float)
    elif exposure_rule == "market_vol_target_18_v1":
        exposure_scale, exposure_observed_vol = _market_vol_exposure_scale(panel)
        exposure_scale = exposure_scale.reindex(open_.index).fillna(1.0)
        exposure_observed_vol = exposure_observed_vol.reindex(open_.index)
    elif exposure_rule == "external_pit_schedule_v1":
        supplied = costs.get("exposure_schedule")
        schedule_id = str(costs.get("exposure_schedule_id") or "").strip()
        if not isinstance(supplied, pd.Series) or not schedule_id:
            raise ValueError(
                "external_pit_schedule_v1 需要具名的 pandas Series 仓位日程"
            )
        schedule = supplied.copy()
        try:
            schedule.index = pd.DatetimeIndex(schedule.index).normalize()
        except (TypeError, ValueError) as exc:
            raise ValueError("外部仓位日程索引必须是交易日") from exc
        if schedule.index.has_duplicates:
            raise ValueError("外部仓位日程交易日不得重复")
        schedule = pd.to_numeric(schedule, errors="coerce").reindex(open_.index)
        if schedule.isna().any() or not np.isfinite(schedule).all():
            raise ValueError("外部仓位日程必须完整覆盖 Panel 且不得缺失")
        if ((schedule < 0.0) | (schedule > 1.0)).any():
            raise ValueError("外部仓位日程必须位于 [0,1] 且不得加杠杆")
        exposure_scale = schedule.astype(float)
        exposure_observed_vol = pd.Series(np.nan, index=open_.index, dtype=float)
    else:
        raise ValueError(f"不支持的连续仓位规则: {exposure_rule}")

    # 逐日 o2o、未复权昨收和前值估值矩阵只依赖 Panel 价格快照；诊断会在
    # 同一快照上重复回测，复用全日期矩阵，窗口与交易状态仍由本调用独立处理。
    (
        o2o,
        raw_close,
        valuation_adj_open,
        valuation_raw_open,
        valuation_raw_close,
    ) = _execution_panel_frames(panel)
    limit_reference = limit_reference_frame(panel, required=False)
    if limit_reference is None:
        limit_reference = pd.DataFrame(np.nan, index=panel.close.index,
                                       columns=panel.close.columns)

    def _state_at(field: str, date: pd.Timestamp,
                  default: bool | pd.Series) -> pd.Series:
        matrix = getattr(panel, field, None)
        if isinstance(matrix, pd.DataFrame) and date in matrix.index:
            return matrix.loc[date].reindex(open_.columns).fillna(False).astype(bool)
        if isinstance(default, pd.Series):
            return default.reindex(open_.columns).fillna(False).astype(bool)
        return pd.Series(bool(default), index=open_.columns, dtype=bool)

    raw_delisted = getattr(panel, "is_delisted_panel", None)
    delisted_flags = (raw_delisted.reindex(open_.columns).fillna(False).astype(bool)
                      if isinstance(raw_delisted, pd.Series)
                      else pd.Series(False, index=open_.columns, dtype=bool))
    raw_last_date = getattr(panel, "last_date", None)
    last_dates = (pd.to_datetime(raw_last_date.reindex(open_.columns), errors="coerce")
                  if isinstance(raw_last_date, pd.Series)
                  else pd.Series(pd.NaT, index=open_.columns, dtype="datetime64[ns]"))

    # 与 factor / o2o 交集对齐
    common = factor.index.intersection(open_.index)
    oos = [d for d in oos_dates if d in common]
    if len(oos) < hold + 5:
        return {"ok": False, "reason": "OOS 观测日不足", "annual_return": np.nan,
                "sharpe": np.nan, "max_drawdown": np.nan, "win_rate": np.nan}

    C = pd.DataFrame(False, index=open_.index, columns=open_.columns)  # 收盘信号日·横截面
    # 保留发单时的 cohort 人数；开盘未成交的那份预算留现金，
    # 不改配给已成交成员，否则“不替补”仍会偷偷放大其他仓位。
    cohort_order_sizes = pd.Series(0, index=open_.index, dtype=int)
    signal_audit: list[dict] = []
    unfilled_audit: list[dict] = []
    entry_order_count = 0
    entry_order_filled_count = 0
    entry_order_cancelled_count = 0
    previous_close_limit_cancelled_count = 0
    executed_buy_order_count = 0
    executed_sell_order_count = 0
    executed_buy_shares = 0
    executed_sell_shares = 0
    partial_fill_order_count = 0
    idx_pos = {d: i for i, d in enumerate(open_.index)}
    # 信号 T 收盘生成，T+1 开盘入场，完整持有 H 个 o2o 区间后在
    # T+H+1 开盘退出。只接受自然退出日不晚于本次窗口末日的信号：
    # 否则窗口末会把尾部 cohort 提前清仓，还会把末日无法实际买入的
    # 信号错记为 selected。
    terminal_signal_pos = idx_pos[oos[-1]]
    eligible_signal_dates = [
        d for d in oos
        if idx_pos[d] + hold + 1 <= terminal_signal_pos
    ]
    for d in eligible_signal_dates:
        if d in factor.index:
            f = factor.loc[d]
            o = open_.loc[d]
            fallback_signal = o.notna() & np.isfinite(o)
            signal_ok = _state_at("signal_eligible", d, fallback_signal)
            mask = (f.notna() & o.notna() & np.isfinite(f) & np.isfinite(o)
                    & signal_ok)
            n = int(mask.sum())
            if n < max(30, q * 5):
                continue
            try:
                grp = pd.qcut(f[mask], q, labels=False, duplicates="drop")
            except ValueError:
                continue
            # 方向感知：正/负向因子分别买顶/底分位（A 股禁做空，负向反转做多）
            if direction >= 0:
                in_quint = mask[mask].index[grp == grp.max()]
            else:
                in_quint = mask[mask].index[grp == grp.min()]
            # P-C 整手增强·现实持仓上限：200k 账户等权切片才买得起整手。
            # 从目标分位内取因子值最高/最低 max_pos 名（默认 10，与成本模型
            # n_positions 对齐）——旧"全市场顶分位篮子"(~800 名)整手切资金买不起，
            # 会退化成空仓只计提成本；取前 N 名才让整手执行真实可成交。
            max_pos = int(costs.get("n_positions", 10)) or 10
            buffer = int(costs.get("rebal_buffer", 0)) or 0   # 换手缓冲带（名次），0=关闭（原行为）
            order = f[in_quint].sort_values(ascending=(direction < 0))
            if buffer > 0:
                # 2026-08-14 换手缓冲（Web 研究杠杆：净收益 = 毛利 − 换手×成本，缓冲带
                # 是降换手最有效手段）：已在仓（近 H 信号日选出）且名次仍在前 max_pos+buffer
                # → 保留；新进名只在前 max_pos。相邻信号日成员更替变少 → 换手/成本下降，
                # 低换手低IC可跑赢高换手高IC（换手半衰期平滑约把多因子换手降到 ~68%）。
                ranked = list(order.index)
                d_i = idx_pos[d]
                held_recent = (C.iloc[max(0, d_i - hold):d_i].sum(axis=0) > 0)
                # 先保留仍在前 N+B 的在仓成员，再从前 N 新成员中按排名
                # 补足。保留与新进共用同一个 N 名额，避免旧逻辑选出 N+B 只。
                retained = [
                    code for code in ranked[:max_pos + buffer]
                    if bool(held_recent.get(code, False))
                ][:max_pos]
                retained_set = set(retained)
                newcomers = [
                    code for code in ranked[:max_pos]
                    if code not in retained_set
                    and not bool(held_recent.get(code, False))
                ]
                keep = pd.Index(
                    retained + newcomers[:max(0, max_pos - len(retained))]
                )
            else:
                keep = order.index[:max_pos]
            cohort_order_sizes.loc[d] = len(keep)
            C.loc[d, keep] = True   # 收盘 d 选出多头

            # P2-3 入场可交易性：执行开盘 = d 的下一个交易日开盘；开盘贴涨停 → 买不进，剔除。
            # Auction bands use execution-date reference; no-chase still uses
            # the signal-day raw close, and valuation remains a distinct lane.
            pi = idx_pos[d] + 1
            if pi < len(open_.index):
                exec_date = open_.index[pi]
                entry_open = panel.open_.iloc[pi].reindex(keep)
                entry_adj_open = open_.iloc[pi].reindex(keep)
                prev_close = raw_close.loc[d].reindex(entry_open.index)
                exec_tradable = _state_at(
                    "tradable", exec_date,
                    panel.open_.iloc[pi].notna() & (panel.open_.iloc[pi] > 0),
                ).reindex(keep)
                exec_st = _state_at("is_st_panel", exec_date, False).reindex(keep)
                missing_price = ~(
                    entry_open.notna() & (entry_open > 0)
                    & entry_adj_open.notna() & (entry_adj_open > 0)
                )
                execution_reference = limit_reference.loc[exec_date].reindex(entry_open.index)
                missing_reference = ~(np.isfinite(execution_reference) & (execution_reference > 0))
                unbuy = _entry_unbuyable(
                    entry_open, execution_reference, entry_open.index,
                    trade_date=exec_date, is_st=exec_st)
                normal_blocked = (
                    ~exec_tradable.fillna(False) | exec_st.fillna(False)
                    | missing_price | unbuy
                )
                missing_limit_price = pd.Series(
                    False, index=entry_open.index, dtype=bool,
                )
                no_chase_cancelled = pd.Series(
                    False, index=entry_open.index, dtype=bool,
                )
                entry_gap_cap = (
                    0.02
                    if entry_rule == "previous_close_gap_2pct_no_chase_v1"
                    else 0.0
                )
                # A 股买价最小变动单位是 0.01 元；上限向下取整，
                # 避免四舍五入后实际允许高开超过冻结的 2%。
                fill_limit = np.floor(
                    prev_close * (1.0 + entry_gap_cap) * 100.0 + 1e-9
                ) / 100.0
                if entry_rule in {
                    "previous_close_limit_no_chase_v1",
                    "previous_close_gap_2pct_no_chase_v1",
                }:
                    valid_limit = prev_close.notna() & (prev_close > 0)
                    missing_limit_price = ~valid_limit
                    no_chase_cancelled = (
                        (entry_open > fill_limit)
                        .where(valid_limit & entry_open.notna(), False)
                        .fillna(False)
                    )
                blocked_entry = (
                    normal_blocked | missing_limit_price | no_chase_cancelled
                )
                rule_only_cancelled = (
                    no_chase_cancelled & ~normal_blocked & ~missing_limit_price
                )
                entry_order_count += len(keep)
                entry_order_cancelled_count += int(blocked_entry.sum())
                entry_order_filled_count += int((~blocked_entry).sum())
                previous_close_limit_cancelled_count += int(
                    rule_only_cancelled.sum()
                )
                if blocked_entry.any():
                    C.loc[d, blocked_entry[blocked_entry].index] = False
                if capture_audit:
                    rank_by_code = {str(code): rank for rank, code in enumerate(order.index, 1)}
                    for code in keep:
                        code_s = str(code)
                        blocked = bool(blocked_entry.get(code, False))
                        reason = None
                        if bool(exec_st.get(code, False)):
                            reason = "st_restricted"
                        elif not bool(exec_tradable.get(code, False)):
                            reason = "suspended_or_untradable"
                        elif bool(missing_price.get(code, False)):
                            reason = "missing_price"
                        elif bool(missing_reference.get(code, False)):
                            reason = "missing_execution_limit_reference"
                        elif bool(unbuy.get(code, False)):
                            reason = "limit_up"
                        elif bool(missing_limit_price.get(code, False)):
                            reason = "missing_limit_price"
                        elif bool(no_chase_cancelled.get(code, False)):
                            reason = (
                                "previous_close_gap_2pct_not_filled"
                                if entry_rule == "previous_close_gap_2pct_no_chase_v1"
                                else "previous_close_limit_not_filled"
                            )
                        limit_price = (
                            _json_float(fill_limit.get(code))
                            if entry_rule in {
                                "previous_close_limit_no_chase_v1",
                                "previous_close_gap_2pct_no_chase_v1",
                            }
                            else None
                        )
                        execution_open = _json_float(entry_open.get(code))
                        signal_day_raw_close = _json_float(prev_close.get(code))
                        overnight_gap_bps = (
                            (execution_open / signal_day_raw_close - 1.0) * 10_000.0
                            if execution_open is not None
                            and signal_day_raw_close is not None
                            and signal_day_raw_close > 0
                            else None
                        )
                        signal_row = {
                            "signal_date": pd.Timestamp(d).date().isoformat(),
                            "execution_date": pd.Timestamp(exec_date).date().isoformat(),
                            "code": code_s,
                            "score": float(f.loc[code]),
                            "rank": int(rank_by_code[code_s]),
                            "status": "unfilled" if blocked else "selected",
                            "reason": reason,
                            "entry_rule": entry_rule,
                            "limit_price": limit_price,
                            "limit_price_source": (
                                (
                                    "signal_day_raw_close_plus_2pct"
                                    if entry_rule == "previous_close_gap_2pct_no_chase_v1"
                                    else "signal_day_raw_close"
                                )
                                if limit_price is not None else None
                            ),
                            "execution_open": execution_open,
                            "execution_limit_reference": _json_float(execution_reference.get(code)),
                            "fill_price": execution_open if not blocked else None,
                            "overnight_gap_bps": overnight_gap_bps,
                        }
                        if entry_rule == "previous_close_gap_2pct_no_chase_v1":
                            signal_row["signal_day_raw_close"] = signal_day_raw_close
                        signal_audit.append(signal_row)
                        if blocked:
                            unfilled_audit.append({
                                "date": pd.Timestamp(exec_date).date().isoformat(),
                                "code": code_s,
                                "side": "buy",
                                "requested_shares": None,
                                "reason": reason,
                                "source": "signal_entry",
                                "entry_rule": entry_rule,
                                "limit_price": limit_price,
                                "execution_open": execution_open,
                            })

    # ── t+1 执行；日 t 目标 = [t-H, t-1] 各信号 cohort 权重之和 ──
    pos_index = open_.index
    first_exec = pos_index.get_loc(oos[0]) + 1
    last_signal = pos_index.get_loc(oos[-1])

    # ── 逐日自融资账本：目标仓位→实际股数差额成交→逐笔费用→次日净值 ──
    daily_ret: list[float] = []
    dates_out: list[pd.Timestamp] = []
    round_trip = _round_trip_cost(costs)
    cap_total = float(costs.get("capital", 200_000.0)) or 200_000.0
    slippage = float(costs.get("slippage", qc.COSTS["slippage"]))
    cash = cap_total
    shares_state = pd.Series(0.0, index=open_.columns, dtype=float)
    # U=raw_shares×raw_open/adj_open；U×adj_open 即当日持仓价值。
    # 保持 U 跨日不变即严格获得 adj_open 收益，不被除权除息误伤。
    adjusted_units_state = pd.Series(0.0, index=open_.columns, dtype=float)
    stuck_carry = pd.Series(False, index=open_.columns)
    previous_natural = pd.Series(False, index=open_.columns)
    churn_rates: list[float] = []
    target_audit: list[dict] = []
    trade_audit: list[dict] = []
    day_audit: list[dict] = []
    events_audit: list[dict] = []
    total_fees = 0.0
    total_market_impact = 0.0
    total_buy_notional = 0.0
    total_sell_notional = 0.0
    total_delisted_writeoff_loss = 0.0
    nav_denominators = 0.0
    delisted_written_off = pd.Series(False, index=open_.columns, dtype=bool)
    last_return_nav_before: float | None = None
    exposure_scales: list[float] = []
    desired_target_gross_exposures: list[float] = []
    actual_gross_exposures: list[float] = []
    cash_weights: list[float] = []
    max_single_weights: list[float] = []
    desired_position_counts: list[int] = []
    funded_position_counts: list[int] = []
    lot_target_fill_rates: list[float] = []
    adv_limited_order_count = 0
    adv_missing_order_count = 0

    # 收益只用 OOS 区间内相邻开盘；不为末日未到期仓位借用区间外价格。
    for t_idx in range(first_exec, min(last_signal, len(pos_index) - 1)):
        current_date = pd.Timestamp(pos_index[t_idx])
        # 每个有效信号日只分配 1/H 净值，候选内等权。不把过去 H 日做
        # 布尔并集后整体等权，因为那会丢失重复入选的 cohort 权重。
        signal_window = C.iloc[max(0, t_idx - hold):t_idx]
        cohort_sizes = signal_window.sum(axis=1)
        active_cohorts = signal_window.loc[cohort_sizes > 0]
        if active_cohorts.empty:
            desired_weights = pd.Series(0.0, index=open_.columns, dtype=float)
        else:
            order_sizes = cohort_order_sizes.reindex(active_cohorts.index)
            desired_weights = (active_cohorts.div(
                order_sizes, axis=0
            ).sum(axis=0) / float(hold))
        # 连续仓位规则只缩放组合总目标，不改变因子、排序或 cohort 成员。
        # t 日开盘使用的系数只由 t-1 及更早收盘数据生成；未分配权重自然留现金。
        target_exposure_scale = float(exposure_scale.loc[current_date])
        desired_weights = desired_weights * target_exposure_scale
        desired_target_gross_exposure = float(desired_weights.sum())
        natural = desired_weights > 0
        current_prices = panel.open_.iloc[t_idx].astype(float)       # 当日真实 raw 成交价
        current_adj = open_.iloc[t_idx].astype(float)                # 当日真实 adj 成交换算
        valuation_raw = valuation_raw_open.iloc[t_idx].astype(float) # 只用于持仓估值/目标
        valuation_adj = valuation_adj_open.iloc[t_idx].astype(float)
        next_valuation_adj = valuation_adj_open.iloc[t_idx + 1].astype(float)
        valid_valuation = (valuation_raw.notna() & (valuation_raw > 0)
                           & valuation_adj.notna() & (valuation_adj > 0))
        shares_state.loc[valid_valuation] = np.rint(
            adjusted_units_state.loc[valid_valuation]
            * valuation_adj.loc[valid_valuation]
            / valuation_raw.loc[valid_valuation]
        )
        missing_held_valuation = (adjusted_units_state.abs() > 1e-12) & ~valid_valuation
        if missing_held_valuation.any():
            return {"ok": False,
                    "reason": ("已持仓缺少任何可用的历史复权估值: "
                               + ",".join(map(str, missing_held_valuation[
                                   missing_held_valuation].index[:5]))),
                    "annual_return": np.nan, "sharpe": np.nan,
                    "max_drawdown": np.nan, "win_rate": np.nan}
        held_value = float((adjusted_units_state[valid_valuation]
                            * valuation_adj[valid_valuation]).sum())
        nav_before = float(cash + held_value)
        if not np.isfinite(nav_before) or nav_before <= 0:
            continue
        last_return_nav_before = nav_before

        tradable_today = _state_at(
            "tradable", current_date,
            current_prices.notna() & (current_prices > 0),
        )
        st_today = _state_at("is_st_panel", current_date, False)

        # 只有源面板明确标识为退市股，且当日已严格超过 last_date，
        # 才零回收保守核销。普通停牌/缺价不会触发。
        after_last_date = pd.Series(False, index=open_.columns, dtype=bool)
        known_last = last_dates.notna()
        after_last_date.loc[known_last] = current_date > last_dates.loc[known_last]
        delisted_today = (delisted_flags & after_last_date
                          & ~delisted_written_off
                          & (adjusted_units_state.abs() > 1e-12))
        day_writeoff_loss = 0.0
        if delisted_today.any():
            for code in delisted_today[delisted_today].index:
                loss = float(adjusted_units_state.loc[code] * valuation_adj.loc[code])
                loss = max(0.0, loss)
                day_writeoff_loss += loss
                total_delisted_writeoff_loss += loss
                if capture_audit:
                    events_audit.append({
                        "date": current_date.date().isoformat(),
                        "code": str(code),
                        "event": "delisted_writeoff",
                        "last_date": pd.Timestamp(last_dates.loc[code]).date().isoformat(),
                        "recovery_rate": 0.0,
                        "loss_notional": loss,
                        "basis": "is_delisted_panel_and_date_after_last_date",
                    })
                shares_state.loc[code] = 0.0
                adjusted_units_state.loc[code] = 0.0
                delisted_written_off.loc[code] = True
        delisted_now_or_before = delisted_flags & after_last_date
        natural.loc[delisted_now_or_before] = False

        # 同时检查旧 carry 和今日新到期集，避免旧 carry 存在时漏掉新跌停卖出。
        old_expired = stuck_carry & ~natural & (shares_state > 0)
        new_expired = previous_natural & ~natural & (shares_state > 0)
        expiry_candidates = old_expired | new_expired
        blocked_expiry = pd.Series(False, index=open_.columns)
        if expiry_candidates.any():
            expiry_codes = expiry_candidates[expiry_candidates].index
            exit_open = panel.open_.iloc[t_idx].reindex(expiry_codes)
            prev_cl = limit_reference.loc[current_date].reindex(expiry_codes)
            expiry_tradeable = tradable_today.reindex(expiry_codes).fillna(False)
            expiry_st = st_today.reindex(expiry_codes).fillna(False)
            limit_blocked = _exit_unsellable(
                exit_open, prev_cl, expiry_codes,
                trade_date=current_date, is_st=expiry_st)
            blocked = (~expiry_tradeable) | limit_blocked
            blocked_expiry.loc[blocked[blocked].index] = True
            if capture_audit:
                for code in blocked[blocked].index:
                    is_limit = bool(limit_blocked.get(code, False))
                    unfilled_audit.append({
                        "date": current_date.date().isoformat(),
                        "code": str(code),
                        "side": "sell",
                        "requested_shares": _json_shares(shares_state.loc[code]),
                        "reason": "limit_down" if is_limit else "suspended_or_untradable",
                        "source": "old_carry" if bool(old_expired.loc[code]) else "new_expiry",
                        "limit_pct": (float(_limit_percentages(
                            pd.Index([code]), pd.Index([code]),
                            trade_date=current_date,
                            is_st=expiry_st.reindex([code])).iloc[0]) if is_limit else None),
                    })
        stuck_carry = blocked_expiry

        # 跌停到期仓位保持原股数；其余自然持仓按 cohort 累加权重目标化。
        # 若强制 carry 占用了资金，仅在总需求超过可用净值时等比缩小新目标；
        # 否则保留预热期/缺失 cohort 应有的现金，不虚构满仓。
        forced_codes = blocked_expiry[blocked_expiry].index
        forced_value = float((adjusted_units_state.reindex(forced_codes)
                              * valuation_adj.reindex(forced_codes)).fillna(0.0).sum())
        regular_codes = natural[natural].index.difference(forced_codes, sort=False)
        target_shares = pd.Series(0.0, index=open_.columns, dtype=float)
        target_shares.loc[forced_codes] = shares_state.loc[forced_codes]
        desired_notional = desired_weights.reindex(regular_codes).fillna(0.0) * nav_before
        desired_total = float(desired_notional.sum())
        available = max(0.0, nav_before - forced_value)
        allocation_scale = (min(1.0, available / desired_total)
                            if desired_total > 0 else 1.0)
        for code in regular_codes:
            price = float(valuation_raw.loc[code])
            if np.isfinite(price) and price > 0:
                target_shares.loc[code] = float(qc.lots_from_yuan(
                    float(desired_notional.loc[code]) * allocation_scale, price))
            elif shares_state.loc[code] > 0:
                target_shares.loc[code] = shares_state.loc[code]

        desired_count = int(natural.sum())
        target_count = int((target_shares > 0).sum())
        desired_position_counts.append(desired_count)
        lot_target_fill_rates.append(
            min(1.0, target_count / desired_count) if desired_count else 1.0
        )

        if capture_audit:
            day_s = pd.Timestamp(pos_index[t_idx]).date().isoformat()
            for code in target_shares[target_shares > 0].index:
                px = float(valuation_raw.loc[code])
                target_audit.append({
                    "date": day_s,
                    "code": str(code),
                    "target_shares": _json_shares(target_shares.loc[code]),
                    "target_notional": float(target_shares.loc[code] * px),
                    "cohort_target_weight": float(desired_weights.get(code, 0.0)),
                    "allocation_scale": float(allocation_scale),
                    "forced_carry": bool(blocked_expiry.loc[code]),
                    "valuation_source": ("current_raw_open" if pd.notna(
                        current_prices.loc[code]) else "last_available_raw_open"),
                })

        day_fees = {"commission": 0.0, "transfer": 0.0, "stamp": 0.0,
                    "slippage": 0.0, "market_impact": 0.0, "total": 0.0}
        buy_notional = 0.0
        sell_notional = 0.0

        def record_trade(code: str, side: str, shares: float, price: float,
                         fee: dict[str, float]) -> None:
            if not capture_audit:
                return
            trade_audit.append({
                "date": current_date.date().isoformat(),
                "code": str(code), "side": side, "shares": _json_shares(shares),
                "adjusted_units_delta": float(shares * price / current_adj.loc[code]),
                "price": float(price), "notional": float(shares * price),
                **{key: float(value) for key, value in fee.items()},
            })

        # 先卖后买，卖出侧所有减仓均检查跌停；费用直接从现金扣减。
        sell_delta = (shares_state - target_shares).clip(lower=0)
        for code in sorted(sell_delta[sell_delta > 0].index):
            delta = float(sell_delta.loc[code])
            # 清仓允许卖出全部除权等价余额；普通减仓仍以 100 股整手。
            requested = (delta if target_shares.loc[code] <= 1e-9
                         else float(int(delta // qc.LOT_SIZE) * qc.LOT_SIZE))
            price = float(current_prices.loc[code])
            if requested <= 0:
                continue
            adj_price = float(current_adj.loc[code])
            if (not bool(tradable_today.loc[code]) or
                    not np.isfinite(price) or price <= 0 or
                    not np.isfinite(adj_price) or adj_price <= 0):
                if capture_audit:
                    unfilled_audit.append({"date": current_date.date().isoformat(),
                                           "code": str(code), "side": "sell",
                                           "requested_shares": requested,
                                           "reason": "suspended_or_untradable",
                                           "source": "rebalance"})
                continue
            stuck = _exit_unsellable(pd.Series([price], index=[code]),
                                     limit_reference.loc[current_date].reindex([code]),
                                     pd.Index([code]),
                                     trade_date=current_date,
                                     is_st=st_today.reindex([code]))
            if bool(stuck.iloc[0]):
                if not bool(blocked_expiry.loc[code]) and capture_audit:
                    unfilled_audit.append({"date": current_date.date().isoformat(),
                                           "code": str(code), "side": "sell",
                                           "requested_shares": requested,
                                           "reason": "limit_down", "source": "rebalance",
                                           "limit_pct": float(_limit_percentages(
                                               pd.Index([code]), pd.Index([code]),
                                               trade_date=current_date,
                                               is_st=st_today.reindex([code])).iloc[0])})
                continue
            original_requested = requested
            requested, adv = _liquidity_fill(
                requested, price, current_date, code,
                allow_odd_lot=target_shares.loc[code] <= 1e-9,
            )
            if participation is not None and requested <= 0:
                adv_missing_order_count += int(adv is None)
                adv_limited_order_count += int(adv is not None)
                if capture_audit:
                    unfilled_audit.append({
                        "date": current_date.date().isoformat(),
                        "code": str(code), "side": "sell",
                        "requested_shares": original_requested,
                        "reason": "adv_missing" if adv is None else "adv_participation_limit",
                        "source": "rebalance",
                    })
                continue
            if requested < original_requested:
                adv_limited_order_count += 1
                partial_fill_order_count += 1
                if capture_audit:
                    unfilled_audit.append({
                        "date": current_date.date().isoformat(),
                        "code": str(code), "side": "sell",
                        "requested_shares": original_requested - requested,
                        "reason": "adv_participation_limit", "source": "rebalance",
                    })
            notional = float(requested * price)
            fee = _trade_fee_breakdown(
                notional, "sell", slippage, _impact_rate(notional, adv)
            )
            shares_state.loc[code] -= requested
            adjusted_units_state.loc[code] -= requested * price / adj_price
            if abs(shares_state.loc[code]) < 1e-8:
                shares_state.loc[code] = 0.0
                adjusted_units_state.loc[code] = 0.0
            cash += notional - fee["total"]
            sell_notional += notional
            executed_sell_order_count += 1
            executed_sell_shares += int(requested)
            for key in day_fees:
                day_fees[key] += fee[key]
            record_trade(str(code), "sell", requested, price, fee)

        buy_delta = (target_shares - shares_state).clip(lower=0)
        for code in sorted(buy_delta[buy_delta > 0].index):
            requested = int(buy_delta.loc[code] // qc.LOT_SIZE * qc.LOT_SIZE)
            price = float(current_prices.loc[code])
            if requested <= 0:
                continue
            adj_price = float(current_adj.loc[code])
            if bool(st_today.loc[code]):
                if capture_audit:
                    unfilled_audit.append({"date": current_date.date().isoformat(),
                                           "code": str(code), "side": "buy",
                                           "requested_shares": requested,
                                           "reason": "st_restricted", "source": "rebalance"})
                continue
            if (not bool(tradable_today.loc[code]) or
                    not np.isfinite(price) or price <= 0 or
                    not np.isfinite(adj_price) or adj_price <= 0):
                if capture_audit:
                    unfilled_audit.append({"date": current_date.date().isoformat(),
                                           "code": str(code), "side": "buy",
                                           "requested_shares": requested,
                                           "reason": "suspended_or_untradable",
                                           "source": "rebalance"})
                continue
            unbuy = _entry_unbuyable(pd.Series([price], index=[code]),
                                     limit_reference.loc[current_date].reindex([code]),
                                     pd.Index([code]), trade_date=current_date,
                                     is_st=st_today.reindex([code]))
            if bool(unbuy.iloc[0]):
                if capture_audit:
                    unfilled_audit.append({"date": current_date.date().isoformat(),
                                           "code": str(code), "side": "buy",
                                           "requested_shares": requested,
                                           "reason": "limit_up", "source": "rebalance"})
                continue
            original_requested = requested
            requested, adv = _liquidity_fill(
                requested, price, current_date, code,
            )
            if participation is not None and requested <= 0:
                adv_missing_order_count += int(adv is None)
                adv_limited_order_count += int(adv is not None)
                if capture_audit:
                    unfilled_audit.append({
                        "date": current_date.date().isoformat(),
                        "code": str(code), "side": "buy",
                        "requested_shares": original_requested,
                        "reason": "adv_missing" if adv is None else "adv_participation_limit",
                        "source": "rebalance",
                    })
                continue
            if requested < original_requested:
                adv_limited_order_count += 1
                partial_fill_order_count += 1
                if capture_audit:
                    unfilled_audit.append({
                        "date": current_date.date().isoformat(),
                        "code": str(code), "side": "buy",
                        "requested_shares": original_requested - requested,
                        "reason": "adv_participation_limit", "source": "rebalance",
                    })
            affordable = min(requested, qc.lots_from_yuan(cash, price))
            while affordable > 0:
                notional = float(affordable * price)
                fee = _trade_fee_breakdown(
                    notional, "buy", slippage, _impact_rate(notional, adv)
                )
                if notional + fee["total"] <= cash + 1e-9:
                    break
                affordable -= qc.LOT_SIZE
            if affordable <= 0:
                if capture_audit:
                    unfilled_audit.append({"date": pd.Timestamp(pos_index[t_idx]).date().isoformat(),
                                           "code": str(code), "side": "buy",
                                           "requested_shares": requested,
                                           "reason": "insufficient_cash", "source": "rebalance"})
                continue
            notional = float(affordable * price)
            fee = _trade_fee_breakdown(
                notional, "buy", slippage, _impact_rate(notional, adv)
            )
            shares_state.loc[code] += affordable
            adjusted_units_state.loc[code] += affordable * price / adj_price
            cash -= notional + fee["total"]
            buy_notional += notional
            executed_buy_order_count += 1
            executed_buy_shares += int(affordable)
            partial_fill_order_count += int(affordable < requested)
            for key in day_fees:
                day_fees[key] += fee[key]
            record_trade(str(code), "buy", affordable, price, fee)
            if affordable < requested and capture_audit:
                unfilled_audit.append({"date": pd.Timestamp(pos_index[t_idx]).date().isoformat(),
                                       "code": str(code), "side": "buy",
                                       "requested_shares": requested - affordable,
                                       "reason": "insufficient_cash", "source": "rebalance"})

        position_value = float((adjusted_units_state[valid_valuation]
                                * valuation_adj[valid_valuation]).sum())
        nav_after_trade = float(cash + position_value)
        actual_gross = (position_value / nav_after_trade
                        if nav_after_trade > 0 else 0.0)
        cash_weight = (cash / nav_after_trade if nav_after_trade > 0 else 0.0)
        position_values = (adjusted_units_state[valid_valuation]
                           * valuation_adj[valid_valuation])
        max_single_weight = (float(position_values.max()) / nav_after_trade
                             if nav_after_trade > 0 and len(position_values)
                             else 0.0)
        funded_position_counts.append(int((adjusted_units_state.abs() > 1e-12).sum()))
        exposure_scales.append(target_exposure_scale)
        desired_target_gross_exposures.append(desired_target_gross_exposure)
        actual_gross_exposures.append(float(actual_gross))
        cash_weights.append(float(cash_weight))
        max_single_weights.append(float(max(0.0, max_single_weight)))
        next_missing_held = ((adjusted_units_state.abs() > 1e-12)
                             & ~(next_valuation_adj.notna() & (next_valuation_adj > 0)))
        if next_missing_held.any():
            return {"ok": False, "reason": "次日持仓缺少历史复权估值",
                    "annual_return": np.nan, "sharpe": np.nan,
                    "max_drawdown": np.nan, "win_rate": np.nan}
        nav_next = float(cash + (adjusted_units_state * next_valuation_adj).fillna(0.0).sum())
        gross_pnl = float(nav_next - nav_after_trade)
        gross_return = gross_pnl / nav_before
        net_return = nav_next / nav_before - 1.0
        traded = buy_notional + sell_notional
        turnover = traded / (2.0 * nav_before)
        churn_rates.append(float(turnover))
        total_fees += day_fees["total"]
        total_market_impact += day_fees["market_impact"]
        total_buy_notional += buy_notional
        total_sell_notional += sell_notional
        nav_denominators += nav_before
        daily_ret.append(float(net_return))
        dates_out.append(pos_index[t_idx])

        if capture_audit:
            holdings = []
            for code in sorted(shares_state[shares_state > 0].index):
                raw_px = float(valuation_raw.loc[code])
                adj_px = float(valuation_adj.loc[code])
                holdings.append({"code": str(code),
                                 "synthetic_share_equivalent": _json_shares(
                                     shares_state.loc[code]),
                                 "quantity_basis": "total_return_synthetic_equivalent",
                                 "adjusted_units": float(adjusted_units_state.loc[code]),
                                 "valuation_price": raw_px,
                                 "valuation_adj_open": adj_px,
                                 "valuation_source": ("current_adj_open" if (
                                     pd.notna(current_prices.loc[code])
                                     and pd.notna(current_adj.loc[code]))
                                     else "last_available_adj_open"),
                                 "notional": float(adjusted_units_state.loc[code]
                                                   * adj_px),
                                 "forced_carry": bool(blocked_expiry.loc[code])})
            day_audit.append({
                "date": current_date.date().isoformat(),
                "next_date": pd.Timestamp(pos_index[t_idx + 1]).date().isoformat(),
                "nav_before": float(nav_before), "nav_after_trade": float(nav_after_trade),
                "nav_next": float(nav_next), "cash": float(cash),
                "gross_pnl": gross_pnl, "gross_return": float(gross_return),
                "net_return": float(net_return), "buy_notional": float(buy_notional),
                "sell_notional": float(sell_notional), "traded_notional": float(traded),
                "turnover": float(turnover),
                "exposure_scale": float(target_exposure_scale),
                "desired_target_gross_exposure": desired_target_gross_exposure,
                "actual_gross_exposure": float(actual_gross),
                "cash_weight": float(cash_weight),
                "max_single_weight": float(max(0.0, max_single_weight)),
                "market_volatility_20d_annualized": (
                    float(exposure_observed_vol.loc[current_date])
                    if pd.notna(exposure_observed_vol.loc[current_date]) else None
                ),
                "delisted_writeoff_loss": float(day_writeoff_loss),
                "fees": {key: float(value) for key, value in day_fees.items()},
                "holdings": holdings,
            })
        previous_natural = natural.copy()

    # 测试窗末按最后一个可用开盘显式清仓。窗口内最后一段收益原先只把
    # 持仓按末日开盘估值，却没有支付卖出佣金、印花税、过户费和滑点，
    # 会系统性抬高净收益。任何末日不可卖仓位都 fail-closed，不能把未清算
    # 残值当成可投产回测净值。
    terminal_date = pd.Timestamp(pos_index[min(last_signal, len(pos_index) - 1)])
    terminal_fees = {"commission": 0.0, "transfer": 0.0, "stamp": 0.0,
                     "slippage": 0.0, "market_impact": 0.0, "total": 0.0}
    terminal_sell_notional = 0.0
    terminal_writeoff_loss = 0.0
    terminal_held = adjusted_units_state.abs() > 1e-12
    if terminal_held.any():
        terminal_idx = pos_index.get_loc(terminal_date)
        terminal_raw = panel.open_.iloc[terminal_idx].astype(float)
        terminal_adj = open_.iloc[terminal_idx].astype(float)
        terminal_valuation_adj = valuation_adj_open.iloc[terminal_idx].astype(float)
        terminal_tradable = _state_at(
            "tradable", terminal_date,
            terminal_raw.notna() & (terminal_raw > 0),
        )
        terminal_st = _state_at("is_st_panel", terminal_date, False)

        # 若窗口末已严格越过明确退市日，保守按零回收核销；普通停牌、缺价
        # 不能伪装成退市，也不能用前值假装完成卖出。
        terminal_after_last = pd.Series(False, index=open_.columns, dtype=bool)
        known_last = last_dates.notna()
        terminal_after_last.loc[known_last] = (
            terminal_date > last_dates.loc[known_last]
        )
        terminal_delisted = (delisted_flags & terminal_after_last
                             & ~delisted_written_off & terminal_held)
        for code in terminal_delisted[terminal_delisted].index:
            loss = float(adjusted_units_state.loc[code]
                         * terminal_valuation_adj.loc[code])
            loss = max(0.0, loss) if np.isfinite(loss) else 0.0
            terminal_writeoff_loss += loss
            total_delisted_writeoff_loss += loss
            if capture_audit:
                events_audit.append({
                    "date": terminal_date.date().isoformat(),
                    "code": str(code),
                    "event": "terminal_delisted_writeoff",
                    "last_date": pd.Timestamp(last_dates.loc[code]).date().isoformat(),
                    "recovery_rate": 0.0,
                    "loss_notional": loss,
                    "basis": "is_delisted_panel_and_date_after_last_date",
                })
            shares_state.loc[code] = 0.0
            adjusted_units_state.loc[code] = 0.0
            delisted_written_off.loc[code] = True

        terminal_held = adjusted_units_state.abs() > 1e-12
        if terminal_held.any():
            held_codes = terminal_held[terminal_held].index
            missing_price = (~terminal_raw.reindex(held_codes).notna()
                             | (terminal_raw.reindex(held_codes) <= 0)
                             | ~terminal_adj.reindex(held_codes).notna()
                             | (terminal_adj.reindex(held_codes) <= 0))
            untradable = ~terminal_tradable.reindex(held_codes).fillna(False)
            limit_down = _exit_unsellable(
                terminal_raw.reindex(held_codes),
                limit_reference.loc[terminal_date].reindex(held_codes),
                held_codes,
                trade_date=terminal_date,
                is_st=terminal_st.reindex(held_codes),
            )
            blocked = missing_price | untradable | limit_down
            terminal_adv: dict[str, float | None] = {}
            if participation is not None:
                for code in held_codes:
                    raw_price = float(terminal_raw.loc[code])
                    shares = float(round(
                        adjusted_units_state.loc[code]
                        * terminal_adj.loc[code] / raw_price
                    ))
                    fill, adv = _liquidity_fill(
                        shares, raw_price, terminal_date, code,
                        allow_odd_lot=True,
                    )
                    terminal_adv[str(code)] = adv
                    if fill + 1e-8 < shares:
                        blocked.loc[code] = True
            if blocked.any():
                blocked_codes = [str(code) for code in blocked[blocked].index]
                return {
                    "ok": False,
                    "reason": ("测试窗末仍有无法清算的持仓，严格回测拒绝: "
                               + ",".join(blocked_codes[:5])),
                    "annual_return": np.nan,
                    "sharpe": np.nan,
                    "max_drawdown": np.nan,
                    "win_rate": np.nan,
                    "terminal_liquidation": {
                        "ok": False,
                        "date": terminal_date.date().isoformat(),
                        "blocked_codes": blocked_codes,
                    },
                }

            for code in sorted(held_codes):
                raw_price = float(terminal_raw.loc[code])
                adj_price = float(terminal_adj.loc[code])
                shares = float(round(
                    adjusted_units_state.loc[code] * adj_price / raw_price
                ))
                notional = float(shares * raw_price)
                fee = _trade_fee_breakdown(
                    notional, "sell", slippage,
                    _impact_rate(notional, terminal_adv.get(str(code)))
                    if participation is not None else 0.0,
                )
                cash += notional - fee["total"]
                terminal_sell_notional += notional
                for key in terminal_fees:
                    terminal_fees[key] += fee[key]
                if capture_audit:
                    trade_audit.append({
                        "date": terminal_date.date().isoformat(),
                        "code": str(code),
                        "side": "sell",
                        "shares": _json_shares(shares),
                        "adjusted_units_delta": float(
                            shares * raw_price / adj_price
                        ),
                        "price": raw_price,
                        "notional": notional,
                        "source": "terminal_liquidation",
                        **{key: float(value) for key, value in fee.items()},
                    })
                shares_state.loc[code] = 0.0
                adjusted_units_state.loc[code] = 0.0

    if daily_ret and last_return_nav_before is not None:
        daily_ret[-1] = float(cash / last_return_nav_before - 1.0)
        total_fees += terminal_fees["total"]
        total_market_impact += terminal_fees["market_impact"]
        total_sell_notional += terminal_sell_notional
        if churn_rates:
            churn_rates[-1] += float(
                terminal_sell_notional / (2.0 * last_return_nav_before)
            )
        if capture_audit and day_audit:
            last_day = day_audit[-1]
            pre_liquidation_nav = float(last_day["nav_next"])
            last_day["nav_next"] = float(cash)
            last_day["net_return"] = float(daily_ret[-1])
            last_day["sell_notional"] = float(
                last_day["sell_notional"] + terminal_sell_notional
            )
            last_day["traded_notional"] = float(
                last_day["traded_notional"] + terminal_sell_notional
            )
            last_day["turnover"] = float(
                last_day["turnover"]
                + terminal_sell_notional / (2.0 * last_return_nav_before)
            )
            for key in terminal_fees:
                last_day["fees"][key] = float(
                    last_day["fees"][key] + terminal_fees[key]
                )
            last_day["delisted_writeoff_loss"] = float(
                last_day["delisted_writeoff_loss"] + terminal_writeoff_loss
            )
            last_day["terminal_liquidation"] = {
                "ok": True,
                "date": terminal_date.date().isoformat(),
                "pre_liquidation_nav": pre_liquidation_nav,
                "sell_notional": float(terminal_sell_notional),
                "fees": {key: float(value) for key, value in terminal_fees.items()},
                "delisted_writeoff_loss": float(terminal_writeoff_loss),
                "ending_cash": float(cash),
                "ending_holdings": [],
                "ending_actual_gross_exposure": 0.0,
                "ending_cash_weight": 1.0,
                "reconciliation": (
                    "ending_cash = pre_liquidation_nav - sell_fees "
                    "- terminal_delisted_writeoff_loss"
                ),
            }

    if len(daily_ret) < 30:
        return {"ok": False, "reason": "有效回测交易日不足", "annual_return": np.nan,
                "sharpe": np.nan, "max_drawdown": np.nan, "win_rate": np.nan}

    s = pd.Series(daily_ret, index=pd.DatetimeIndex(dates_out), dtype=float)
    equity = (1.0 + s).cumprod()
    n = len(s)
    total = equity.iloc[-1] - 1.0
    annual = (1.0 + total) ** (252.0 / n) - 1.0
    sharpe = float(s.mean() / s.std(ddof=1) * np.sqrt(252.0)) if s.std(ddof=1) > 0 else np.nan
    # 净值序列从首个日收益后开始；初始净值 1.0 也必须是峰值候选，否则
    # “首日先跌、随后修复”会漏报早期回撤。
    peak = equity.cummax().clip(lower=1.0)
    mdd = float(((equity - peak) / peak).min())
    win = float((s > 0).mean())
    realized_volatility = (float(s.std(ddof=1) * np.sqrt(252.0))
                           if s.std(ddof=1) > 0 else 0.0)
    calmar = (float(annual / abs(mdd)) if mdd < 0 else None)

    # 换手 KPI：实际（买+卖）/(2×净值)，而非持仓数变化或固定 1/H 假设。
    avg_churn = float(np.mean(churn_rates)) if churn_rates else None
    churn_flag = bool(avg_churn is not None and avg_churn > 0.15)

    # ── 等权市场基准；策略的实际交易费用不能虚构扣到基准上 ──
    bench = o2o.reindex(s.index).mean(axis=1)
    bench_valid = bench.notna() & np.isfinite(bench)
    benchmark_n_days = int(bench_valid.sum())
    benchmark_coverage = benchmark_n_days / len(s) if len(s) else 0.0
    # 策略每个 open t→t+1 收益日都必须有同期复权总收益基准。
    # 缺任一日不缩窗重算，而是把基准收益置为不可用，供闸门 fail-closed。
    bench_ann = (
        float((1.0 + bench).prod() ** (252.0 / len(bench)) - 1.0)
        if len(bench) and benchmark_n_days == len(bench) else np.nan
    )
    excess = (annual - bench_ann) if pd.notna(bench_ann) else np.nan

    result = {
        "ok": True,
        "annual_return": annual,
        "sharpe": sharpe,
        "max_drawdown": mdd,
        "calmar": calmar,
        "realized_volatility": realized_volatility,
        "win_rate": win,
        "n_days": n,
        "total_return": float(total),
        "benchmark_annual": bench_ann,
        "excess_annual": float(excess) if pd.notna(excess) else np.nan,
        "benchmark_n_days": benchmark_n_days,
        "benchmark_coverage": float(benchmark_coverage),
        "benchmark_basis": "adjusted-open total-return t→t+1",
        "baseline_annual": _baseline_annual(panel, oos_dates, costs),   # 宽基基线（beta 腿）
        "daily_ret": s,
        "equity": equity,
        "hold": hold,
        "round_trip_cost": round_trip,
        "daily_cost": float(total_fees / nav_denominators) if nav_denominators > 0 else 0.0,
        "total_fees": float(total_fees),
        "total_market_impact": float(total_market_impact),
        "buy_notional": float(total_buy_notional),
        "sell_notional": float(total_sell_notional),
        "traded_notional": float(total_buy_notional + total_sell_notional),
        "delisted_writeoff_loss": float(total_delisted_writeoff_loss),
        "signal_churn": avg_churn,
        "churn_flag": churn_flag,
        "churn_n": len(churn_rates),
        "holding_model": "daily_rebalanced_overlapping_cohort_target_weights",
        "cohort_budget_fraction": float(1.0 / hold),
        "entry_rule": entry_rule,
        "entry_order_count": int(entry_order_count),
        "entry_order_filled_count": int(entry_order_filled_count),
        "entry_order_cancelled_count": int(entry_order_cancelled_count),
        "entry_order_fill_rate": (
            float(entry_order_filled_count / entry_order_count)
            if entry_order_count else None
        ),
        "entry_order_fill_rate_semantics": "pre_execution_price_and_status_check_only_deprecated",
        "entry_precheck_passed_count": int(entry_order_filled_count),
        "executed_buy_order_count": int(executed_buy_order_count),
        "executed_sell_order_count": int(executed_sell_order_count),
        "executed_buy_shares": int(executed_buy_shares),
        "executed_sell_shares": int(executed_sell_shares),
        "partial_fill_order_count": int(partial_fill_order_count),
        "previous_close_limit_cancelled_count": int(
            previous_close_limit_cancelled_count
        ),
        "exposure_rule": exposure_rule,
        "exposure_schedule_id": (
            str(costs.get("exposure_schedule_id"))
            if exposure_rule == "external_pit_schedule_v1" else None
        ),
        "average_exposure_scale": (float(np.mean(exposure_scales))
                                   if exposure_scales else 1.0),
        "average_target_gross_exposure": (
            float(np.mean(desired_target_gross_exposures))
            if desired_target_gross_exposures else 0.0
        ),
        "average_actual_gross_exposure": (float(np.mean(actual_gross_exposures))
                                          if actual_gross_exposures else 0.0),
        "average_cash_weight": (float(np.mean(cash_weights))
                                if cash_weights else 1.0),
        "minimum_cash_weight": (float(np.min(cash_weights))
                                if cash_weights else 1.0),
        "maximum_single_weight": (float(np.max(max_single_weights))
                                  if max_single_weights else 0.0),
        "gross_exposure_fill_rate": (
            float(min(1.0, np.sum(actual_gross_exposures)
                      / np.sum(desired_target_gross_exposures)))
            if np.sum(desired_target_gross_exposures) > 0 else 1.0
        ),
        "lot_target_fill_rate": (float(np.mean(lot_target_fill_rates))
                                 if lot_target_fill_rates else 0.0),
        "average_desired_positions": (float(np.mean(desired_position_counts))
                                      if desired_position_counts else 0.0),
        "average_funded_positions": (float(np.mean(funded_position_counts))
                                     if funded_position_counts else 0.0),
        "liquidity_model": (
            "prior_adv_participation_sqrt_impact_v1"
            if participation is not None else "disabled"
        ),
        "adv_window": int(adv_window) if participation is not None else None,
        "max_adv_participation": participation,
        "impact_coefficient_bps": impact_coefficient_bps,
        "adv_limited_order_count": int(adv_limited_order_count),
        "adv_missing_order_count": int(adv_missing_order_count),
        "exposure_observed_days": int(exposure_observed_vol.reindex(
            pd.DatetimeIndex(dates_out)).notna().sum()),
        "terminal_liquidated": True,
        "ending_cash": float(cash),
    }
    if capture_audit:
        exposure_schedule = [[
            row["date"], row["exposure_scale"],
            row["market_volatility_20d_annualized"],
        ] for row in day_audit]
        audit = {
            "schema_version": 3,
            "engine_version": "factor_backtest_v3",
            "initial_capital": float(cap_total),
            "auction_limit_reference": "supplied_execution_date_reference_only",
            "portfolio_construction": {
                "holding_model": "daily_rebalanced_overlapping_cohort_target_weights",
                "cohort_budget_fraction": float(1.0 / hold),
                "cohort_member_weight": "equal",
                "repeat_selection": "weights_accumulate",
                "unfilled_entry_budget": "cash_no_reallocation_or_replacement",
                "warmup_or_missing_cohort_budget": "cash",
                "lot_accounting": "no_independent_cohort_lots_daily_nav_rebalance",
                "corporate_action_share_rounding": "nearest_integer_no_fractional_A_share_orders",
                "entry_rule": entry_rule,
                "entry_rule_parameters": (
                    {
                        "signal_time": "T_close",
                        "submit_time": "T_plus_1_open",
                        "limit_price_source": "T_raw_close",
                        "fill_condition": "T_plus_1_raw_open_lte_limit",
                        "fill_price": "T_plus_1_raw_open",
                        "time_in_force": "opening_execution_only",
                        "cancel_policy": "no_replacement_no_T_plus_2_retry",
                    }
                    if entry_rule == "previous_close_limit_no_chase_v1"
                    else {
                        "signal_time": "T_close",
                        "submit_time": "T_plus_1_open",
                        "reference_price_source": "T_raw_close",
                        "maximum_overnight_gap_bps": 200,
                        "fill_condition": "T_plus_1_raw_open_lte_T_raw_close_times_1_02",
                        "fill_price": "T_plus_1_raw_open",
                        "time_in_force": "opening_execution_only",
                        "cancel_policy": "no_replacement_no_T_plus_2_retry",
                    }
                    if entry_rule == "previous_close_gap_2pct_no_chase_v1"
                    else None
                ),
                "exposure_rule": exposure_rule,
                "exposure_rule_parameters": (
                    {
                        "lookback_days": 20,
                        "target_annualized_volatility": 0.18,
                        "minimum_gross_exposure": 0.40,
                        "maximum_gross_exposure": 1.00,
                        "leverage_allowed": False,
                    }
                    if exposure_rule == "market_vol_target_18_v1"
                    else {
                        "schedule_id": str(costs["exposure_schedule_id"]),
                        "minimum_gross_exposure": 0.0,
                        "maximum_gross_exposure": 1.0,
                        "leverage_allowed": False,
                        "caller_attests_previous_close_or_earlier": True,
                    }
                    if exposure_rule == "external_pit_schedule_v1"
                    else None
                ),
                "exposure_signal_timing": "execution_open_uses_previous_close_or_earlier",
                "exposure_schedule_sha256": hashlib.sha256(json.dumps(
                    exposure_schedule, ensure_ascii=False, separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")).hexdigest(),
            },
            "cost_model": {
                "commission": float(qc.COSTS["commission"]),
                "minimum_commission_yuan": float(qc.MIN_COMMISSION_YUAN),
                "stamp_sell_only": float(qc.COSTS["stamp"]),
                "transfer": float(qc.COSTS["transfer"]),
                "slippage": float(slippage),
                "liquidity_model": result["liquidity_model"],
                "adv_window": result["adv_window"],
                "adv_lag_days": 1 if participation is not None else None,
                "max_adv_participation": participation,
                "impact_coefficient_bps": float(impact_coefficient_bps),
                "lot_size": int(qc.LOT_SIZE),
                "execution_price": "raw_open",
                "buy_order_rule": entry_rule,
                "valuation_price": "adj_open",
                "corporate_action_accounting": "adjusted_units",
                "quantity_basis": "total_return_synthetic_equivalent_not_actual_shares",
                "corporate_action_note": (
                    "adjusted_units 是复权总收益等价单位；不声称还原真实送转股或现金分红"
                ),
            },
            "signals": signal_audit,
            "targets": target_audit,
            "trades": trade_audit,
            "unfilled": unfilled_audit,
            "events": events_audit,
            "days": day_audit,
            "summary": {
                "total_fees": float(total_fees),
                "total_market_impact": float(total_market_impact),
                "buy_notional": float(total_buy_notional),
                "sell_notional": float(total_sell_notional),
                "delisted_writeoff_loss": float(total_delisted_writeoff_loss),
                "ending_nav": float(day_audit[-1]["nav_next"]) if day_audit else float(cap_total),
                "average_target_gross_exposure": result[
                    "average_target_gross_exposure"
                ],
                "average_exposure_scale": result["average_exposure_scale"],
                "average_actual_gross_exposure": result[
                    "average_actual_gross_exposure"
                ],
                "average_cash_weight": result["average_cash_weight"],
                "minimum_cash_weight": result["minimum_cash_weight"],
                "maximum_single_weight": result["maximum_single_weight"],
                "gross_exposure_fill_rate": result["gross_exposure_fill_rate"],
                "lot_target_fill_rate": result["lot_target_fill_rate"],
                "average_desired_positions": result["average_desired_positions"],
                "average_funded_positions": result["average_funded_positions"],
                "adv_limited_order_count": int(adv_limited_order_count),
                "adv_missing_order_count": int(adv_missing_order_count),
                "realized_volatility": result["realized_volatility"],
                "entry_order_count": int(entry_order_count),
                "entry_order_filled_count": int(entry_order_filled_count),
                "entry_order_cancelled_count": int(entry_order_cancelled_count),
                "entry_order_fill_rate": result["entry_order_fill_rate"],
                "previous_close_limit_cancelled_count": int(
                    previous_close_limit_cancelled_count
                ),
            },
        }
        result["audit"] = audit
        result["audit_sha256"] = _audit_sha256(audit)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# 宽基基线（beta 腿）—— 用户目标「年化 ≥20%」的结构性答案（2026-08-14）
#
# OOS 实测（2024-07-01~2025-12-31，THS 成本口径）：
#   市值加权全市场买持（ETF 模拟）净年化 +39.5%，等权月度再平衡 +48.6%。
# 即：宽基 beta 本身已远超 20% 目标线。因子 alpha 全部 NO-GO 时，系统的默认
# 持仓 = 此宽基基线（不空仓），因子只在净成本闸门通过、净超额为正时替换/叠加。
# 诚实边界：不承诺每年达标——宽基有牛市（+39%）也有熊市（2022 宽基约 -20%），
# 结构修复的目标是「不再结构性错过 beta + 诚实报告每年达标/未达标」。
# ──────────────────────────────────────────────────────────────────────────────

_TARGET_ANNUAL = 0.20  # 年化目标线


def _build_float_mv_baseline_daily(panel: Panel, annual_cost: float) -> pd.Series:
    """构建与策略无关的全日期市值加权基线；窗口由调用方二次切片。"""
    open_ = panel.adj_open
    o2o = (open_.shift(-1) / open_).astype(np.float64) - 1.0
    weights = panel.float_mv.astype(np.float64)
    usable_weights = weights.mask(~(weights.notna() & o2o.notna()))
    weight_sum = usable_weights.sum(axis=1).replace(0, np.nan)
    gross = o2o.multiply(usable_weights, axis=0).sum(axis=1) / weight_sum
    return gross - float(annual_cost) / 252.0


def _float_mv_baseline_cache_key(panel: Panel, annual_cost: float) -> tuple:
    """绑定 Panel 实例、实际输入快照与基线依赖矩阵，防止跨版本复用。"""
    return (
        id(panel),
        getattr(panel, "_panel_cache_key", None),
        id(panel.adj_open),
        id(panel.float_mv),
        float(annual_cost),
    )


def _baseline_daily(panel: Panel, oos_dates: list[pd.Timestamp],
                    weight: str = "float_mv", annual_cost: float = 0.008,
                    rebal_days: int = 21) -> pd.Series:
    """宽基基线逐日净收益序列。

    weight="float_mv"：市值加权全市场买持（对应现实中买宽基 ETF）——权重随价格
        自然漂移，无主动换仓；成本 = 年化 ETF 级费率（管理+托管+换仓，0.8%/年）。
    weight="equal"：等权，每 rebal_days 交易日再平衡一档——再平衡日摊一次完整
        换手成本到 rebal_days 天（等权日再平衡的成本会吃掉全部超额，不现实）。
    """
    open_ = panel.adj_open
    days = [d for d in oos_dates if d in open_.index]
    if weight == "equal":
        o2o = (open_.shift(-1) / open_).astype(np.float64) - 1.0
        rt = qc.cost_rate_for_portfolio(200_000.0, 200, qc.COSTS["slippage"])
        rebal_cost = rt / rebal_days
        out = []
        for i, d in enumerate(days):
            cost = rebal_cost if i == 0 or (open_.index.get_loc(d) % rebal_days == 0) else 0.0
            m = float(o2o.loc[d].mean())
            out.append(m - cost)
        return pd.Series(out, index=pd.DatetimeIndex(days), dtype=float).dropna()
    # float_mv：市值加权买持（缺值权重按剩余市值归一）
    # Panel 在单次输入快照生命周期内只读；只缓存全日期 Series，
    # 不缓存窗口年化结果，因此每次仍按 oos_dates 切片并 dropna。
    key = _float_mv_baseline_cache_key(panel, annual_cost)
    cache = getattr(panel, "_float_mv_baseline_daily_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        setattr(panel, "_float_mv_baseline_daily_cache", cache)
    full_daily = cache.get(key)
    if full_daily is None:
        full_daily = _build_float_mv_baseline_daily(panel, annual_cost)
        cache[key] = full_daily
    return full_daily.loc[days].dropna()


def run_market_baseline(panel: Panel, oos_dates: list[pd.Timestamp],
                        weight: str = "float_mv", annual_cost: float = 0.008,
                        rebal_days: int = 21) -> dict:
    """宽基基线组合（beta 腿）科学评估：净年化 / 累计 / 回撤 / 20% 目标线对照。

    定位：因子 alpha 无 PASS 时的默认持仓 = 此宽基基线（不空仓）。调用方把它
    作为「持有市场」的基准口径；因子策略只在与它比较净超额为正时才值得替代。
    """
    s = _baseline_daily(panel, oos_dates, weight=weight, annual_cost=annual_cost,
                        rebal_days=rebal_days)
    if len(s) < 30:
        return {"ok": False, "reason": "基线交易日不足", "annual_return": np.nan}
    equity = (1.0 + s).cumprod()
    total = float(equity.iloc[-1] - 1.0)
    annual = float((1.0 + total) ** (252.0 / len(s)) - 1.0)
    sharpe = float(s.mean() / s.std(ddof=1) * np.sqrt(252.0)) if s.std(ddof=1) > 0 else np.nan
    peak = equity.cummax().clip(lower=1.0)
    mdd = float(((equity - peak) / peak).min())
    return {
        "ok": True,
        "weight": weight,
        "annual_return": annual,
        "total_return": total,
        "max_drawdown": mdd,
        "sharpe": sharpe,
        "n_days": len(s),
        "target_annual": _TARGET_ANNUAL,
        "target_met": bool(annual >= _TARGET_ANNUAL),
        "daily_ret": s,
        "equity": equity,
    }


def _baseline_annual(panel: Panel, oos_dates: list[pd.Timestamp], costs: dict | None) -> float:
    """run_backtest 结果里附带的宽基基线年化（市值加权买持口径），非 NaN 才计入。"""
    try:
        b = run_market_baseline(panel, oos_dates)
        return float(b["annual_return"]) if b.get("ok") else np.nan
    except Exception:
        return np.nan
