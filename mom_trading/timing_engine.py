"""智能买入时机引擎（Smart Buy-Timing Engine）.

为 /timing 页面提供后端：
- get_buy_window(): A股交易时段分级（可复核/谨慎/休市）
- compute_timing_signals(): 六维信号（放量突破/MACD金叉/RSI超卖回升/分时均线支撑/北向/大单转正）
- score_timing(): 加权评分 + 等级 + recommended_action
- 持仓记录（确认买入/卖出）：SQLite data/timing_positions.db

数据真实性约束（2026-08 审计）：
- 行情/分时来自实时报价与分钟K线
- 资金流来自 research/fund_flow_live.json 实时快照（绝对值放大 ~5 倍，判断用方向）
- 北向资金：THS 实时快照无北向字段，eastmoney L2/北向不可达 → 信号标记 unavailable，
  计分时按可用权重归一化，绝不用假数据凑数。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from quantqc.costs import one_side_cost_rate

from .data_semantics import (
    FLOW_MAGNITUDE_SCALE,
    MAX_OPEN_POSITIONS,
    normalize_fund_flow_yi,
)
from .trading_calendar import (
    audited_trading_day_age,
    next_trading_day_after,
    trading_day_status,
)

_SH_TZ = ZoneInfo("Asia/Shanghai")
_KRONOS_SNAPSHOT_PATH = (
    Path(__file__).resolve().parent.parent / "research" / "kronos_stock_snapshot.json"
)

# 加权评分权重（%）
WEIGHTS = {
    "volume_breakout": 0.30,   # 放量突破
    "macd_golden_cross": 0.20, # MACD金叉
    "rsi_rebound": 0.15,       # RSI超卖回升
    "vwap_support": 0.15,      # 分时均线支撑
    "northbound": 0.10,        # 北向流入
    "big_order_positive": 0.10,# 大单净买入转正
}

# 评分等级
LEVELS = [
    (70, "条件匹配高", "buy_now"),
    (40, "条件匹配中", "wait"),
    (30, "观望", "wait"),
    (0, "不宜", "avoid"),
]

# ── 交易时段窗口（分钟数，0=00:00）────────────────
# 09:30=570, 09:45=585, 10:30=630, 11:00=660, 11:30=690,
# 13:00=780, 13:30=810, 14:30=870, 14:50=890, 15:00=900
_WINDOWS = [
    # (start_min, end_min, phase, label, note)
    (570, 660, "actionable", "上午复核窗口", "09:30–11:00 可完成人工复核"),
    (660, 690, "caution", "谨慎窗口", "11:00–11:30 临近午休，谨慎"),
    (690, 780, "closed", "午间休市", "11:30–13:00 休市"),
    (780, 870, "actionable", "午后复核窗口", "13:00–14:30 可完成人工复核"),
    (870, 900, "caution", "尾盘谨慎窗口", "14:30 后不新开仓，继续持仓风险观察"),
]

# 人工复核窗口：用于倒计时目标与 recommended_action
_ACTIONABLE_STARTS = (570, 780)
_ACTIONABLE_ENDS = (660, 870)
_LUNCH = (690, 780)         # 午休区间
_WEEKEND = (5, 6)


def _now_sh() -> datetime:
    return datetime.now(_SH_TZ)


def get_buy_window(now: datetime | None = None) -> dict:
    """当前处于哪个交易时段，以及距下一个人工复核窗口的倒计时。

    返回字段：
    - phase: actionable / caution / closed
    - label / note: 中文说明
    - window_action: 仅由时段决定的建议（buy_now / wait / avoid）
    - countdown_seconds: 距下一复核窗口开启（若在窗口外）或本窗口结束（若在窗口内）
    - countdown_target: 倒计时目标描述
    """
    now = now or _now_sh()
    minutes = now.hour * 60 + now.minute
    today = now.date()
    calendar = trading_day_status(now)

    current = None
    for start, end, phase, label, note in _WINDOWS:
        if start <= minutes < end:
            current = {"phase": phase, "label": label, "note": note,
                       "start": start, "end": end}
            break

    if current is None:
        if minutes < 570:
            current = {"phase": "closed", "label": "未开盘", "note": "早于 09:30",
                       "start": 570, "end": 570}
        else:
            current = {"phase": "closed", "label": "已收盘", "note": "晚于 15:00",
                       "start": 900, "end": 900}

    # 窗口本身给出的建议
    window_action = {
        "actionable": "buy_now",
        "best": "buy_now",
        "observe": "wait",
        "caution": "avoid",
        "avoid": "avoid",
        "forbidden": "avoid",
        "closed": "wait",
    }.get(current["phase"], "wait")

    # ── 倒计时 ──
    countdown_seconds = None
    countdown_target = ""
    # V7 在上午和午后各保留一个人工复核窗口；午休应指向当日13:00，
    # 14:30后才指向下一已审计交易日。
    next_session = (
        today
        if calendar["is_trading_day"] and minutes < _ACTIONABLE_ENDS[-1]
        else next_trading_day_after(today)
    )

    def _target_minutes(target_min: int) -> timedelta:
        """把 分钟数 转成今天/明天的绝对时间并算剩余秒数。"""
        hour, minute = divmod(target_min, 60)
        base = datetime.combine(today, time(hour=hour, minute=minute), tzinfo=_SH_TZ)
        if base <= now:
            base += timedelta(days=1)
        return base - now

    if not calendar["is_trading_day"]:
        current = {"phase": "closed", "label": "非交易日", "note": calendar["reason"],
                   "start": 570, "end": 570}
        window_action = "wait"
        if next_session is None:
            countdown_seconds = None
            countdown_target = "等待已审计交易日历更新"
        else:
            next_open = datetime.combine(
                next_session, time(hour=9, minute=30), tzinfo=_SH_TZ,
            )
            countdown_seconds = max(0, int((next_open - now).total_seconds()))
            countdown_target = f"{next_session.isoformat()} 09:30 上午复核窗口开启"
    elif current["phase"] == "actionable":
        # 人工复核窗口内 → 倒计时到本窗口结束
        end_min = current["end"]
        rem = _target_minutes(end_min)
        countdown_seconds = max(0, int(rem.total_seconds()))
        hour, minute = divmod(end_min, 60)
        countdown_target = f"{hour:02d}:{minute:02d} 本窗口结束"
    elif minutes < _ACTIONABLE_STARTS[0]:
        # 当天开盘前可安全倒计时；收盘后不能把自然日次日猜成交易日。
        opening = _target_minutes(_ACTIONABLE_STARTS[0])
        countdown_seconds = max(0, int(opening.total_seconds()))
        countdown_target = "09:30 上午复核窗口开启"
    elif minutes < _ACTIONABLE_STARTS[1]:
        afternoon = _target_minutes(_ACTIONABLE_STARTS[1])
        countdown_seconds = max(0, int(afternoon.total_seconds()))
        countdown_target = "13:00 午后复核窗口开启"
    else:
        if next_session is not None:
            next_open = datetime.combine(
                next_session, time(hour=9, minute=30), tzinfo=_SH_TZ,
            )
            countdown_seconds = max(0, int((next_open - now).total_seconds()))
            countdown_target = (
                f"{next_session.isoformat()} 09:30 上午复核窗口开启"
            )
        elif minutes >= _ACTIONABLE_ENDS[-1]:
            countdown_seconds = None
            countdown_target = "等待已审计交易日历更新"
        else:
            countdown_seconds = None
            countdown_target = "等待下一已审计交易日 09:30"

    return {
        "phase": current["phase"],
        "label": current["label"],
        "note": current["note"],
        "window_action": window_action,
        "countdown_seconds": countdown_seconds,
        "countdown_target": countdown_target,
        "next_session": next_session.isoformat() if next_session else None,
        "server_time": now.isoformat(),
        "calendar": calendar,
    }


def is_actionable_buy_window(window: dict | None) -> bool:
    """One shared session decision for V7, while accepting archived V6 fixtures."""
    return bool(window) and window.get("phase") in {"actionable", "best"}


# ── 六维信号计算 ────────────────────────────────

def _ema(series: list[float], span: int) -> list[float]:
    """指数移动平均（与 candles._ema 同口径）。"""
    out: list[float] = []
    k = 2 / (span + 1)
    prev = None
    for v in series:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def _rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    if len(closes) < period + 1:
        return [None] * len(closes)
    out: list[float | None] = [None] * len(closes)
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for idx in range(period, len(closes)):
        g = gains[idx - 1]
        l = losses[idx - 1]
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else 999.0
        out[idx] = 100 - 100 / (1 + rs)
    return out


def compute_timing_signals(
    candles: list,
    quote: dict | None,
    intraday: dict | None,
    ff_net_yi: float | None,
) -> dict:
    """六维买入时机信号。每个信号返回 {weight, score(0~1), state, detail}。

    state: hit / partial / miss / unavailable
    """
    signals: dict = {}

    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]
    last_close = closes[-1] if closes else 0.0
    n = len(closes)

    # ── 1. 放量突破（30%）────────────────────
    sig = {"weight": WEIGHTS["volume_breakout"], "score": 0.0, "state": "miss",
           "detail": "数据不足"}
    if n >= 21 and last_close > 0:
        avg_vol = sum(volumes[-21:-1]) / 20
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0.0
        prev_high = max(c.high for c in candles[-21:-1])
        broke = last_close > prev_high
        if vol_ratio >= 2.0 and broke:
            sig = {"weight": WEIGHTS["volume_breakout"], "score": 1.0, "state": "hit",
                   "detail": f"放量突破！量比 {vol_ratio:.1f}，突破前20根高点"}
        elif vol_ratio >= 1.5 and broke:
            sig = {"weight": WEIGHTS["volume_breakout"], "score": 0.75, "state": "hit",
                   "detail": f"放量突破，量比 {vol_ratio:.1f}"}
        elif vol_ratio >= 1.5:
            sig = {"weight": WEIGHTS["volume_breakout"], "score": 0.4, "state": "partial",
                   "detail": f"放量 {vol_ratio:.1f} 但未突破前高"}
        elif broke:
            sig = {"weight": WEIGHTS["volume_breakout"], "score": 0.2, "state": "partial",
                   "detail": "突破前高但量能未放大"}
        else:
            sig = {"weight": WEIGHTS["volume_breakout"], "score": 0.0, "state": "miss",
                   "detail": f"量比 {vol_ratio:.1f}，未突破"}
    signals["volume_breakout"] = sig

    # ── 2. MACD 金叉（20%）────────────────────
    sig = {"weight": WEIGHTS["macd_golden_cross"], "score": 0.0, "state": "miss",
           "detail": "数据不足"}
    if n >= 30:
        ema12 = _ema(closes, 12)
        ema26 = _ema(closes, 26)
        dif = [a - b for a, b in zip(ema12, ema26)]
        dea = _ema(dif, 9)
        # 最近 3 根内发生 DIF 上穿 DEA
        crossed = False
        for i in range(max(1, n - 3), n):
            if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
                crossed = True
                break
        above = dif[-1] > dea[-1]
        hist = dif[-1] - dea[-1]
        if crossed and above:
            sig = {"weight": WEIGHTS["macd_golden_cross"], "score": 1.0, "state": "hit",
                   "detail": "MACD 金叉（DIF 上穿 DEA）"}
        elif above:
            sig = {"weight": WEIGHTS["macd_golden_cross"], "score": 0.5, "state": "partial",
                   "detail": f"DIF 在 DEA 上方，柱值 {hist:+.4f}"}
        else:
            sig = {"weight": WEIGHTS["macd_golden_cross"], "score": 0.0, "state": "miss",
                   "detail": f"DIF 未上穿 DEA（柱值 {hist:+.4f}）"}
    signals["macd_golden_cross"] = sig

    # ── 3. RSI 超卖回升（15%）─────────────────
    sig = {"weight": WEIGHTS["rsi_rebound"], "score": 0.0, "state": "miss",
           "detail": "数据不足"}
    rsi_series = _rsi_series(closes) if n >= 16 else []
    if len(rsi_series) >= 5:
        rsi_now = rsi_series[-1]
        rsi_prev5 = rsi_series[-5]
        rsi_prev1 = rsi_series[-2]
        if rsi_now is not None:
            if (rsi_prev5 is not None and rsi_prev5 <= 35) and rsi_now > rsi_prev1:
                sig = {"weight": WEIGHTS["rsi_rebound"], "score": 1.0, "state": "hit",
                       "detail": f"RSI 从超卖回升 {rsi_prev5:.0f}→{rsi_now:.0f}"}
            elif 35 < rsi_now < 50:
                sig = {"weight": WEIGHTS["rsi_rebound"], "score": 0.5, "state": "partial",
                       "detail": f"RSI {rsi_now:.0f}，中性偏低区间"}
            elif rsi_now >= 70:
                sig = {"weight": WEIGHTS["rsi_rebound"], "score": 0.0, "state": "miss",
                       "detail": f"RSI {rsi_now:.0f} 已超买，不追"}
            else:
                sig = {"weight": WEIGHTS["rsi_rebound"], "score": 0.2, "state": "partial",
                       "detail": f"RSI {rsi_now:.0f}"}
    signals["rsi_rebound"] = sig

    # ── 4. 分时均线支撑（15%）─────────────────
    sig = {"weight": WEIGHTS["vwap_support"], "score": 0.0, "state": "unavailable",
           "detail": "无分时数据"}
    vwap = None
    if intraday:
        # get_intraday 返回 {candles:[{time,close,volume,amount,vwap}], previous_close,...}
        if isinstance(intraday.get("candles"), list) and intraday["candles"]:
            vwap = intraday["candles"][-1].get("vwap")
        else:
            vwap = intraday.get("vwap")
    if vwap and last_close > 0:
        if last_close >= vwap:
            sig = {"weight": WEIGHTS["vwap_support"], "score": 1.0, "state": "hit",
                   "detail": f"现价 {last_close:.2f} ≥ 分时均价 {vwap:.2f}"}
        else:
            sig = {"weight": WEIGHTS["vwap_support"], "score": 0.0, "state": "miss",
                   "detail": f"现价 {last_close:.2f} < 分时均价 {vwap:.2f}"}
    elif quote and quote.get("price"):
        # 无分时数据时退化为现价 vs 昨收
        pc = quote.get("previous_close")
        price = quote.get("price")
        if pc and pc > 0:
            if price >= pc:
                sig = {"weight": WEIGHTS["vwap_support"], "score": 0.5, "state": "partial",
                       "detail": f"现价 {price:.2f} ≥ 昨收 {pc:.2f}（无分时，降权）"}
            else:
                sig = {"weight": WEIGHTS["vwap_support"], "score": 0.0, "state": "miss",
                       "detail": f"现价 {price:.2f} < 昨收 {pc:.2f}"}
    signals["vwap_support"] = sig

    # ── 5. 北向流入（10%）：THS 快照无北向 → 如实标记 ──
    signals["northbound"] = {
        "weight": WEIGHTS["northbound"], "score": 0.0, "state": "unavailable",
        "detail": "北向资金数据暂缺（接口不可达），未计入评分",
    }

    # Third-party trade-size buckets are observable, but "主力" is not an
    # identified investor and this proxy has no admitted predictive test.
    signals["big_order_positive"] = {
        "weight": WEIGHTS["big_order_positive"], "score": 0.0,
        "state": "unavailable", "role": "research_observation_only",
        "observed_net_yi": ff_net_yi,
        "detail": "第三方大单净额仅展示，未通过预测验证，不计入盘中条件分",
    }

    return signals


def score_timing(signals: dict) -> dict:
    """加权条件匹配分，而不是胜率或预期收益。

    北向等 unavailable 信号不计入分母，避免缺失数据拖低分数。
    同时返回覆盖率和证据身份，防止前端把一个由少数可用条件归一化后的
    数字误读为校准概率。该元数据不改变已冻结策略的阈值或触发结果。
    """
    total_weight = 0.0
    weighted = 0.0
    available_names: list[str] = []
    unavailable_names: list[str] = []
    hit_names: list[str] = []
    partial_names: list[str] = []
    for name, sig in signals.items():
        if sig.get("state") == "unavailable":
            unavailable_names.append(name)
            continue
        w = sig.get("weight", 0)
        total_weight += w
        weighted += w * sig.get("score", 0)
        available_names.append(name)
        if sig.get("state") == "hit":
            hit_names.append(name)
        elif sig.get("state") == "partial":
            partial_names.append(name)
    score = round(weighted / total_weight * 100) if total_weight > 0 else 0

    level = "不宜"
    action = "avoid"
    for threshold, lvl, act in LEVELS:
        if score >= threshold:
            level, action = lvl, act
            break
    configured_weight = sum(
        float((sig or {}).get("weight") or 0) for sig in signals.values()
    )
    coverage_pct = round(
        total_weight / configured_weight * 100, 1,
    ) if configured_weight > 0 else 0.0
    return {
        "score": score,
        "level": level,
        "recommended_action": action,
        "metric_name": "condition_match_score",
        "metric_label": "盘中条件匹配分",
        "calibrated_probability": False,
        "expected_return": None,
        "evidence_grade": "rule_based_unvalidated",
        "threshold_basis": "frozen_research_rule_not_calibrated_probability",
        "coverage_pct": coverage_pct,
        "available_weight_pct": round(total_weight * 100, 1),
        "available_signal_count": len(available_names),
        "total_signal_count": len(signals),
        "available_signals": available_names,
        "unavailable_signals": unavailable_names,
        "hit_signals": hit_names,
        "partial_signals": partial_names,
        "interpretation": (
            "仅表示当前规则条件匹配度，不是胜率、收益概率或最佳买点证明"
        ),
    }


# ── 持仓记录（SQLite）───────────────────────────

_DB_DIR = Path(__file__).resolve().parent.parent / "data"
_DB_PATH = _DB_DIR / "timing_positions.db"
# Do not impose a personal-habit count quota.  New risk is still bounded by
# cash, exposure, open-position slots, liquidity and the active strategy route.
DAILY_TRADE_LIMIT = None
MAX_DAILY_BUY_ALERTS = None


class IdempotencyConflictError(ValueError):
    """The same idempotency key was reused for a different ledger write."""


def _db() -> sqlite3.Connection:
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    try:
        return _initialize_db(conn)
    except BaseException:
        conn.close()
        raise


def _initialize_db(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # Serialize schema inspection and migration across requests and processes.
    # Otherwise two first reads can both ALTER the same apparently absent column.
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS timing_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            price REAL,
            shares INTEGER,
            amount REAL,
            score REAL,
            level TEXT,
            window_label TEXT,
            action TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            sold_at TEXT,
            sold_price REAL,
            pnl REAL,
            initial_shares INTEGER,
            buy_cost REAL,
            realized_pnl_gross REAL,
            sell_cost REAL,
            buy_cost_source TEXT,
            sell_cost_source TEXT,
            entry_origin TEXT,
            entry_strategy_id TEXT,
            entry_evidence_ref_json TEXT,
            entry_signal_id TEXT,
            entry_alert_id INTEGER,
            entry_reference_price REAL,
            entry_slippage_yuan REAL,
            entry_slippage_bps REAL,
            sell_slippage_yuan REAL,
            sell_slippage_attributed_events INTEGER NOT NULL DEFAULT 0,
            sell_fill_events INTEGER NOT NULL DEFAULT 0
        )
    """)
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(timing_positions)")
    }
    if "idempotency_key" not in columns:
        conn.execute(
            "ALTER TABLE timing_positions ADD COLUMN idempotency_key TEXT"
        )
    if "sell_idempotency_key" not in columns:
        conn.execute(
            "ALTER TABLE timing_positions ADD COLUMN sell_idempotency_key TEXT"
        )
    for name, sql_type in (
        ("initial_shares", "INTEGER"),
        ("buy_cost", "REAL"),
        ("realized_pnl_gross", "REAL"),
        ("sell_cost", "REAL"),
        ("buy_cost_source", "TEXT"),
        ("sell_cost_source", "TEXT"),
        ("entry_origin", "TEXT"),
        ("entry_strategy_id", "TEXT"),
        ("entry_evidence_ref_json", "TEXT"),
        ("entry_signal_id", "TEXT"),
        ("entry_alert_id", "INTEGER"),
        ("entry_reference_price", "REAL"),
        ("entry_slippage_yuan", "REAL"),
        ("entry_slippage_bps", "REAL"),
        ("sell_slippage_yuan", "REAL"),
        ("sell_slippage_attributed_events", "INTEGER NOT NULL DEFAULT 0"),
        ("sell_fill_events", "INTEGER NOT NULL DEFAULT 0"),
        ("cash_adjusted", "INTEGER NOT NULL DEFAULT 0"),
        ("cash_snapshot_yuan", "REAL"),
        ("sellable_shares_snapshot", "INTEGER"),
        ("sellable_as_of", "TEXT"),
        ("manual_exit_target_shares", "INTEGER"),
        ("manual_exit_effective_date", "TEXT"),
        ("manual_exit_note", "TEXT"),
        ("manual_exit_updated_at", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE timing_positions ADD COLUMN {name} {sql_type}")
    conn.execute(
        """UPDATE timing_positions
           SET initial_shares=shares
           WHERE initial_shares IS NULL AND shares IS NOT NULL"""
    )
    # Before this migration the application never accepted broker-reported
    # fees, so every populated cost was generated by the local fee model.
    conn.execute(
        """UPDATE timing_positions
           SET buy_cost_source='estimated_model_legacy'
           WHERE buy_cost IS NOT NULL AND buy_cost_source IS NULL"""
    )
    conn.execute(
        """UPDATE timing_positions
           SET sell_cost_source='estimated_model_legacy'
           WHERE sell_cost > 0 AND sell_cost_source IS NULL"""
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS timing_sell_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL,
            shares INTEGER NOT NULL,
            price REAL NOT NULL,
            amount REAL NOT NULL,
            gross_pnl REAL NOT NULL,
            allocated_buy_cost REAL NOT NULL,
            sell_cost REAL NOT NULL,
            sell_cost_source TEXT,
            alert_id INTEGER,
            reference_price REAL,
            slippage_yuan REAL,
            slippage_bps REAL,
            net_pnl REAL NOT NULL,
            sold_at TEXT NOT NULL,
            idempotency_key TEXT UNIQUE
        )
    """)
    event_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(timing_sell_events)")
    }
    for name, sql_type in (
        ("sell_cost_source", "TEXT"),
        ("alert_id", "INTEGER"),
        ("reference_price", "REAL"),
        ("slippage_yuan", "REAL"),
        ("slippage_bps", "REAL"),
    ):
        if name not in event_columns:
            conn.execute(
                f"ALTER TABLE timing_sell_events ADD COLUMN {name} {sql_type}"
            )
    conn.execute(
        """UPDATE timing_sell_events
           SET sell_cost_source='estimated_model_legacy'
           WHERE sell_cost_source IS NULL"""
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS timing_sellable_inventory_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL,
            previous_sellable_shares_snapshot INTEGER,
            previous_sellable_as_of TEXT,
            new_sellable_shares_snapshot INTEGER NOT NULL CHECK (
                new_sellable_shares_snapshot >= 0
            ),
            new_sellable_as_of TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS timing_fill_time_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fill_id TEXT NOT NULL,
            executed_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            supersedes_id INTEGER,
            note TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS timing_fill_plan_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fill_id TEXT NOT NULL,
            alert_id INTEGER,
            plan_snapshot_json TEXT,
            plan_sha256 TEXT,
            source TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            supersedes_id INTEGER,
            note TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS timing_fill_fee_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fill_id TEXT NOT NULL,
            fee_yuan REAL NOT NULL CHECK (fee_yuan >= 0),
            recorded_at TEXT NOT NULL,
            supersedes_id INTEGER,
            note TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            cash_identity TEXT NOT NULL,
            previous_cash_yuan REAL,
            confirmed_cash_yuan REAL NOT NULL CHECK (confirmed_cash_yuan >= 0)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS timing_exit_intent_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL,
            previous_target_shares INTEGER,
            previous_effective_date TEXT,
            target_shares INTEGER NOT NULL CHECK (target_shares >= 0),
            effective_date TEXT NOT NULL,
            note TEXT,
            recorded_at TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE
        )
    """)
    conn.execute(
        """UPDATE timing_positions
           SET sell_fill_events=(
               SELECT COUNT(*) FROM timing_sell_events event
               WHERE event.position_id=timing_positions.id
           )
           WHERE sell_fill_events=0"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS ux_timing_positions_idempotency
           ON timing_positions(idempotency_key)
           WHERE idempotency_key IS NOT NULL"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS ux_timing_positions_sell_idempotency
           ON timing_positions(sell_idempotency_key)
           WHERE sell_idempotency_key IS NOT NULL"""
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS timing_delete_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL,
            position_json TEXT NOT NULL,
            deleted_at TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS timing_account (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            cash_balance REAL NOT NULL CHECK (cash_balance >= 0),
            updated_at TEXT NOT NULL,
            source TEXT NOT NULL
        )
    """)
    account_columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(timing_account)").fetchall()
    }
    for name, definition in (
        ("initial_equity_yuan", "REAL"),
        ("initial_equity_as_of", "TEXT"),
        ("initial_equity_source", "TEXT"),
        ("initial_equity_recorded_at", "TEXT"),
    ):
        if name not in account_columns:
            conn.execute(
                f"ALTER TABLE timing_account ADD COLUMN {name} {definition}"
            )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS timing_account_cashflow_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            flow_type TEXT NOT NULL CHECK (
                flow_type IN ('deposit', 'withdrawal')
            ),
            amount_yuan REAL NOT NULL CHECK (amount_yuan > 0),
            cash_before_yuan REAL NOT NULL CHECK (cash_before_yuan >= 0),
            cash_after_yuan REAL NOT NULL CHECK (cash_after_yuan >= 0),
            effective_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            source TEXT NOT NULL,
            note TEXT,
            idempotency_key TEXT NOT NULL UNIQUE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS timing_account_nav_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trading_date TEXT NOT NULL UNIQUE,
            account_equity_yuan REAL NOT NULL CHECK (account_equity_yuan > 0),
            cash_balance_yuan REAL NOT NULL CHECK (cash_balance_yuan >= 0),
            market_value_yuan REAL NOT NULL CHECK (market_value_yuan >= 0),
            valuation_as_of TEXT NOT NULL,
            valuation_basis TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            source TEXT NOT NULL,
            ledger_identity_sha256 TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS timing_account_cashflow_reconciliations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            through_date TEXT NOT NULL,
            cashflow_event_rows_sha256 TEXT NOT NULL,
            cashflow_event_count INTEGER NOT NULL CHECK (
                cashflow_event_count >= 0
            ),
            confirmed_at TEXT NOT NULL,
            source TEXT NOT NULL,
            note TEXT,
            idempotency_key TEXT NOT NULL UNIQUE
        )
    """)
    conn.commit()
    return conn


def get_account_cash() -> dict | None:
    """Return the user-recorded remaining cash snapshot, if one exists."""
    conn = _db()
    try:
        row = conn.execute(
            """SELECT cash_balance, updated_at, source,
                      initial_equity_yuan, initial_equity_as_of,
                      initial_equity_source, initial_equity_recorded_at
               FROM timing_account WHERE id=1"""
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def account_ledger_rows_sha256(rows: list[dict]) -> str:
    """Hash logical rows, never SQLite/WAL physical bytes."""
    ordered = sorted(
        (dict(row) for row in rows),
        key=lambda row: (int(row.get("id") or 0), str(row.get("code") or "")),
    )
    payload = json.dumps(
        ordered, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def read_account_ledger_facts(*, include_review: bool = False) -> dict:
    """Read account facts and optional retrospective evidence in one transaction."""
    conn = _db()
    try:
        conn.execute("BEGIN")
        position_rows = [
            dict(row) for row in conn.execute(
                "SELECT * FROM timing_positions ORDER BY id"
            ).fetchall()
        ]
        sell_rows = [
            dict(row) for row in conn.execute(
                "SELECT * FROM timing_sell_events ORDER BY id"
            ).fetchall()
        ]
        cashflow_rows = [
            dict(row) for row in conn.execute(
                "SELECT * FROM timing_account_cashflow_events ORDER BY id"
            ).fetchall()
        ]
        fee_rows = [dict(row) for row in conn.execute("SELECT * FROM timing_fill_fee_events ORDER BY id")]
        review_rows = {}
        if include_review:
            review_rows["time_rows"] = [dict(row) for row in conn.execute("SELECT * FROM timing_fill_time_events ORDER BY id")]
            review_rows["plan_rows"] = [dict(row) for row in conn.execute("SELECT * FROM timing_fill_plan_events ORDER BY id")]
        cash_row = conn.execute(
            """SELECT cash_balance, updated_at, source,
                      initial_equity_yuan, initial_equity_as_of,
                      initial_equity_source, initial_equity_recorded_at
               FROM timing_account WHERE id=1"""
        ).fetchone()
        cash = dict(cash_row) if cash_row else None
        conn.rollback()
    finally:
        conn.close()
    open_rows = [row for row in position_rows if row.get("status") == "open"]
    closed_rows = [row for row in position_rows if row.get("status") == "closed"]
    cash_sha = account_ledger_rows_sha256([cash] if cash else [])
    positions_sha = account_ledger_rows_sha256(position_rows)
    sell_events_sha = account_ledger_rows_sha256(sell_rows)
    cashflow_events_sha = account_ledger_rows_sha256(cashflow_rows)
    identity_body = {
        "cash_rows_sha256": cash_sha,
        "position_rows_sha256": positions_sha,
        "sell_event_rows_sha256": sell_events_sha,
        "cashflow_event_rows_sha256": cashflow_events_sha,
    }
    if fee_rows:
        identity_body["fee_event_rows_sha256"] = account_ledger_rows_sha256(fee_rows)
    identity_sha = hashlib.sha256(
        json.dumps(
            identity_body, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    as_of_candidates = [
        str(value) for value in (
            cash.get("updated_at") if cash else None,
            *(row.get("created_at") for row in position_rows),
            *(row.get("sold_at") for row in position_rows),
            *(row.get("sold_at") for row in sell_rows),
        ) if value
    ]
    result = {
        "open_rows": open_rows,
        "closed_rows": closed_rows,
        "cashflow_rows": cashflow_rows,
        "sell_rows": sell_rows,
        "fee_rows": fee_rows,
        "account_cash": cash,
        "ledger_as_of": max(as_of_candidates, default=None),
        "ledger_identity": {
            **identity_body,
            "combined_sha256": identity_sha,
        },
    }
    if include_review:
        # Retrospective links/clocks change review identity, not trading-account authority.
        review_body = {
            "schema": "manual_fill_review_snapshot.v1",
            "account_ledger_sha256": identity_sha,
            **{key + "_sha256": account_ledger_rows_sha256(rows) for key, rows in review_rows.items()},
        }
        result.update(review_rows)
        result["review_identity"] = {
            **review_body,
            "combined_sha256": hashlib.sha256(json.dumps(review_body, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "recorded_as_of": max(as_of_candidates + [str(row["recorded_at"]) for rows in (*review_rows.values(), fee_rows) for row in rows], default=None),
        }
    return result


def set_account_cash(cash_balance: float, *, source: str = "manual_snapshot") -> dict:
    """Persist one authoritative remaining-cash fact for portfolio analysis."""
    amount = float(cash_balance)
    if not math.isfinite(amount) or amount < 0 or amount > 10_000_000_000:
        raise ValueError("剩余现金必须是 0 至 100 亿元之间的有限金额")
    if source not in {"manual_snapshot", "manual_position_snapshot"}:
        raise ValueError("现金来源无效")
    conn = _db()
    try:
        updated_at = _now_sh().isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO timing_account (id, cash_balance, updated_at, source)
               VALUES (1, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   cash_balance=excluded.cash_balance,
                   updated_at=excluded.updated_at,
                   source=excluded.source""",
            (round(amount, 2), updated_at, source),
        )
        conn.commit()
        return {
            "cash_balance": round(amount, 2), "updated_at": updated_at,
            "source": source,
        }
    finally:
        conn.close()


def set_account_initial_equity(
    initial_equity_yuan: float, initial_equity_as_of: str, *,
    source: str = "user_supplied_broker_snapshot",
) -> dict:
    """Persist one immutable user-supplied account-equity baseline."""
    amount = float(initial_equity_yuan)
    if not math.isfinite(amount) or amount <= 0 or amount > 10_000_000_000:
        raise ValueError("初始权益必须是大于 0 且不超过 100 亿元的有限金额")
    if source != "user_supplied_broker_snapshot":
        raise ValueError("初始权益来源无效")
    raw_as_of = str(initial_equity_as_of or "").strip()
    try:
        parsed_as_of = datetime.fromisoformat(raw_as_of.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("初始权益时点必须是带时区的 ISO 时间") from None
    if parsed_as_of.tzinfo is None:
        raise ValueError("初始权益时点必须包含时区")
    normalized_as_of = parsed_as_of.astimezone(_SH_TZ)
    now = _now_sh()
    if normalized_as_of > now + timedelta(minutes=5):
        raise ValueError("初始权益时点不能晚于当前时间")
    recorded_at = now.isoformat(timespec="seconds")
    normalized_amount = round(amount, 2)
    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT initial_equity_yuan, initial_equity_as_of,
                      initial_equity_source, initial_equity_recorded_at
               FROM timing_account WHERE id=1"""
        ).fetchone()
        if row is None:
            raise ValueError("请先录入真实剩余现金，再录入初始权益")
        existing = dict(row)
        if existing.get("initial_equity_yuan") is not None:
            same_fact = bool(
                round(float(existing["initial_equity_yuan"]), 2)
                == normalized_amount
                and str(existing.get("initial_equity_as_of") or "")
                == normalized_as_of.isoformat(timespec="seconds")
                and existing.get("initial_equity_source") == source
            )
            if not same_fact:
                raise ValueError("已存在不同的初始权益基准；不得静默覆盖历史基准")
            conn.rollback()
            return {
                "initial_equity_yuan": normalized_amount,
                "initial_equity_as_of": existing["initial_equity_as_of"],
                "initial_equity_source": source,
                "initial_equity_recorded_at": existing[
                    "initial_equity_recorded_at"
                ],
                "idempotent_replay": True,
            }
        updated = conn.execute(
            """UPDATE timing_account
               SET initial_equity_yuan=?, initial_equity_as_of=?,
                   initial_equity_source=?, initial_equity_recorded_at=?
               WHERE id=1 AND initial_equity_yuan IS NULL""",
            (
                normalized_amount,
                normalized_as_of.isoformat(timespec="seconds"),
                source,
                recorded_at,
            ),
        )
        if updated.rowcount != 1:
            raise ValueError("初始权益基准发生并发变化，请重新读取后再试")
        conn.commit()
        return {
            "initial_equity_yuan": normalized_amount,
            "initial_equity_as_of": normalized_as_of.isoformat(
                timespec="seconds"
            ),
            "initial_equity_source": source,
            "initial_equity_recorded_at": recorded_at,
            "idempotent_replay": False,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_account_cashflows() -> list[dict]:
    """Return immutable user-recorded external account cash flows."""
    conn = _db()
    try:
        return [
            dict(row) for row in conn.execute(
                """SELECT * FROM timing_account_cashflow_events
                   ORDER BY effective_at, id"""
            ).fetchall()
        ]
    finally:
        conn.close()


def list_account_cashflow_reconciliations() -> list[dict]:
    """Return immutable user confirmations that no external flow was omitted."""
    conn = _db()
    try:
        return [
            dict(row) for row in conn.execute(
                """SELECT * FROM timing_account_cashflow_reconciliations
                   ORDER BY confirmed_at DESC, id DESC"""
            ).fetchall()
        ]
    finally:
        conn.close()


def _cashflow_rows_through(conn: sqlite3.Connection, through_date: str) -> list[dict]:
    return [
        dict(row) for row in conn.execute(
            """SELECT * FROM timing_account_cashflow_events
               WHERE substr(effective_at, 1, 10) <= ?
               ORDER BY effective_at, id""",
            (through_date,),
        ).fetchall()
    ]


def account_cashflow_reconciliation_status(
    through_date: str | None = None,
) -> dict:
    """Verify that a cashflow completeness confirmation still matches its rows."""
    target = str(through_date or "").strip() or None
    if target is not None:
        try:
            datetime.strptime(target, "%Y-%m-%d")
        except ValueError:
            raise ValueError("资金完整性核对日期必须为 YYYY-MM-DD") from None
    conn = _db()
    try:
        if target is None:
            confirmation = conn.execute(
                """SELECT * FROM timing_account_cashflow_reconciliations
                   ORDER BY through_date DESC, confirmed_at DESC, id DESC
                   LIMIT 1"""
            ).fetchone()
        else:
            confirmation = conn.execute(
                """SELECT * FROM timing_account_cashflow_reconciliations
                   WHERE through_date >= ?
                   ORDER BY through_date DESC, confirmed_at DESC, id DESC
                   LIMIT 1""",
                (target,),
            ).fetchone()
        if confirmation is None:
            return {
                "status": "unconfirmed",
                "verified_complete": False,
                "requested_through_date": target,
                "confirmed_through_date": None,
                "reason": "尚未人工确认该日期前的入金与出金已全部记录",
            }
        row = dict(confirmation)
        rows = _cashflow_rows_through(conn, row["through_date"])
        current_sha = account_ledger_rows_sha256(rows)
        current_count = len(rows)
        valid = (
            current_sha == row["cashflow_event_rows_sha256"]
            and current_count == int(row["cashflow_event_count"])
        )
        return {
            "status": "verified" if valid else "invalidated_by_later_entry",
            "verified_complete": valid,
            "requested_through_date": target,
            "confirmed_through_date": row["through_date"],
            "confirmed_at": row["confirmed_at"],
            "cashflow_event_count": current_count,
            "confirmation_id": row["id"],
            "reason": (
                f"已人工确认截至 {row['through_date']} 的外部资金变动无遗漏"
                if valid else
                "确认后补录了更早的资金变动，请重新核对"
            ),
        }
    finally:
        conn.close()


def record_account_cashflow_reconciliation(
    through_date: str, idempotency_key: str, *, note: str | None = None,
) -> dict:
    """Append a dated completeness attestation bound to exact cashflow rows."""
    target = str(through_date or "").strip()
    try:
        target_day = datetime.strptime(target, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("资金完整性核对日期必须为 YYYY-MM-DD") from None
    now = _now_sh()
    if target_day > now.date():
        raise ValueError("不能确认未来日期的资金变动完整性")
    if target_day == now.date() and now.time() < time(15, 0):
        raise ValueError("当日资金变动只能在 15:00 收盘后确认")
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 200:
        raise ValueError("资金完整性核对幂等键无效")
    clean_note = str(note or "").strip() or None
    if clean_note is not None and len(clean_note) > 500:
        raise ValueError("资金完整性核对备注过长")
    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = _cashflow_rows_through(conn, target)
        rows_sha = account_ledger_rows_sha256(rows)
        existing = conn.execute(
            """SELECT * FROM timing_account_cashflow_reconciliations
               WHERE idempotency_key=?""",
            (key,),
        ).fetchone()
        if existing is not None:
            replay = dict(existing)
            if not (
                replay["through_date"] == target
                and replay["cashflow_event_rows_sha256"] == rows_sha
                and int(replay["cashflow_event_count"]) == len(rows)
                and replay.get("note") == clean_note
            ):
                raise IdempotencyConflictError(
                    "同一幂等键已用于另一份资金完整性确认"
                )
            conn.rollback()
            return {**replay, "idempotent_replay": True}
        confirmed_at = now.isoformat(timespec="seconds")
        cur = conn.execute(
            """INSERT INTO timing_account_cashflow_reconciliations
               (through_date, cashflow_event_rows_sha256,
                cashflow_event_count, confirmed_at, source, note,
                idempotency_key)
               VALUES (?, ?, ?, ?, 'manual_user_confirmed', ?, ?)""",
            (target, rows_sha, len(rows), confirmed_at, clean_note, key),
        )
        conn.commit()
        row = conn.execute(
            """SELECT * FROM timing_account_cashflow_reconciliations
               WHERE id=?""",
            (cur.lastrowid,),
        ).fetchone()
        return {**dict(row), "idempotent_replay": False}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_account_cashflow(
    flow_type: str, amount_yuan: float, effective_at: str,
    cash_balance_after_yuan: float, idempotency_key: str, *,
    note: str | None = None,
) -> dict:
    """Record one external flow and reconcile it to the cash balance atomically."""
    kind = str(flow_type or "").strip().lower()
    if kind not in {"deposit", "withdrawal"}:
        raise ValueError("资金变动类型必须是入金或出金")
    amount = float(amount_yuan)
    cash_after = float(cash_balance_after_yuan)
    if not math.isfinite(amount) or amount <= 0 or amount > 10_000_000_000:
        raise ValueError("资金变动金额必须是大于 0 且不超过 100 亿元的有限金额")
    if not math.isfinite(cash_after) or cash_after < 0 or cash_after > 10_000_000_000:
        raise ValueError("变动后可用资金必须是 0 至 100 亿元之间的有限金额")
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 200:
        raise ValueError("资金变动幂等键无效")
    clean_note = str(note or "").strip() or None
    if clean_note is not None and len(clean_note) > 500:
        raise ValueError("资金变动备注过长")
    try:
        parsed_at = datetime.fromisoformat(
            str(effective_at or "").strip().replace("Z", "+00:00")
        )
    except ValueError:
        raise ValueError("资金变动时点必须是带时区的 ISO 时间") from None
    if parsed_at.tzinfo is None:
        raise ValueError("资金变动时点必须包含时区")
    normalized_at = parsed_at.astimezone(_SH_TZ)
    now = _now_sh()
    if normalized_at > now + timedelta(minutes=5):
        raise ValueError("资金变动时点不能晚于当前时间")
    normalized_amount = round(amount, 2)
    normalized_after = round(cash_after, 2)
    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """SELECT * FROM timing_account_cashflow_events
               WHERE idempotency_key=?""",
            (key,),
        ).fetchone()
        if existing is not None:
            replay = dict(existing)
            if not (
                replay["flow_type"] == kind
                and round(float(replay["amount_yuan"]), 2) == normalized_amount
                and replay["effective_at"]
                == normalized_at.isoformat(timespec="seconds")
                and round(float(replay["cash_after_yuan"]), 2)
                == normalized_after
                and replay.get("note") == clean_note
            ):
                raise IdempotencyConflictError(
                    "同一幂等键已用于另一笔不同的资金变动"
                )
            conn.rollback()
            return {**replay, "idempotent_replay": True}
        account = conn.execute(
            "SELECT * FROM timing_account WHERE id=1"
        ).fetchone()
        if account is None:
            raise ValueError("请先录入真实剩余现金")
        baseline_as_of = account["initial_equity_as_of"]
        if baseline_as_of:
            baseline = datetime.fromisoformat(str(baseline_as_of))
            if normalized_at < baseline.astimezone(_SH_TZ):
                raise ValueError("资金变动早于初始权益基准，不能用于当前绩效区间")
        cash_before = round(float(account["cash_balance"]), 2)
        expected_after = round(
            cash_before + normalized_amount
            if kind == "deposit" else cash_before - normalized_amount,
            2,
        )
        if expected_after < 0:
            raise ValueError("出金金额超过当前记录的可用资金")
        if abs(expected_after - normalized_after) > 0.01:
            raise ValueError(
                f"变动后可用资金应为 ¥{expected_after:.2f}；"
                "请先同步券商当前现金，避免把交易成交误记为入金或出金"
            )
        recorded_at = now.isoformat(timespec="seconds")
        cur = conn.execute(
            """INSERT INTO timing_account_cashflow_events
               (flow_type, amount_yuan, cash_before_yuan, cash_after_yuan,
                effective_at, recorded_at, source, note, idempotency_key)
               VALUES (?, ?, ?, ?, ?, ?, 'manual_user_recorded', ?, ?)""",
            (
                kind, normalized_amount, cash_before, normalized_after,
                normalized_at.isoformat(timespec="seconds"), recorded_at,
                clean_note, key,
            ),
        )
        conn.execute(
            """UPDATE timing_account
               SET cash_balance=?, updated_at=?, source='manual_cashflow_event'
               WHERE id=1""",
            (normalized_after, recorded_at),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM timing_account_cashflow_events WHERE id=?",
            (cur.lastrowid,),
        ).fetchone()
        return {**dict(row), "idempotent_replay": False}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_account_nav_snapshots() -> list[dict]:
    """Return immutable completed-session account equity observations."""
    conn = _db()
    try:
        return [
            dict(row) for row in conn.execute(
                """SELECT * FROM timing_account_nav_snapshots
                   ORDER BY valuation_as_of, id"""
            ).fetchall()
        ]
    finally:
        conn.close()


def record_account_nav_snapshot(
    *, trading_date: str, account_equity_yuan: float,
    cash_balance_yuan: float, market_value_yuan: float,
    valuation_as_of: str, valuation_basis: str,
    ledger_identity_sha256: str,
) -> dict:
    """Freeze one server-observed, completed-session account NAV per day."""
    try:
        parsed_day = datetime.strptime(str(trading_date), "%Y-%m-%d").date()
        parsed_at = datetime.fromisoformat(
            str(valuation_as_of or "").strip().replace("Z", "+00:00")
        )
    except ValueError:
        raise ValueError("净值交易日或估值时点格式无效") from None
    if parsed_at.tzinfo is None:
        raise ValueError("净值估值时点必须包含时区")
    normalized_at = parsed_at.astimezone(_SH_TZ)
    if normalized_at.date() != parsed_day:
        raise ValueError("净值交易日必须与估值时点属于同一天")
    now = _now_sh()
    if normalized_at > now + timedelta(minutes=5):
        raise ValueError("净值估值时点不能晚于当前时间")
    equity = round(float(account_equity_yuan), 2)
    cash = round(float(cash_balance_yuan), 2)
    market_value = round(float(market_value_yuan), 2)
    if not all(math.isfinite(value) for value in (equity, cash, market_value)):
        raise ValueError("净值、现金和市值必须是有限金额")
    if equity <= 0 or cash < 0 or market_value < 0:
        raise ValueError("净值必须大于 0，现金和市值不得为负")
    if abs(equity - cash - market_value) > 0.02:
        raise ValueError("账户净值必须等于剩余现金加持仓市值")
    basis = str(valuation_basis or "").strip()
    if basis not in {"same_day_closing_snapshot", "cash_only"}:
        raise ValueError("只有同日收盘估值或全现金账户可以冻结每日净值")
    identity = str(ledger_identity_sha256 or "").strip().lower()
    if len(identity) != 64 or any(ch not in "0123456789abcdef" for ch in identity):
        raise ValueError("账户账本身份无效")
    payload = {
        "trading_date": parsed_day.isoformat(),
        "account_equity_yuan": equity,
        "cash_balance_yuan": cash,
        "market_value_yuan": market_value,
        "valuation_as_of": normalized_at.isoformat(timespec="seconds"),
        "valuation_basis": basis,
        "source": "server_observed_completed_account_valuation",
        "ledger_identity_sha256": identity,
    }
    conn = _db()
    try:
        existing = conn.execute(
            """SELECT * FROM timing_account_nav_snapshots
               WHERE trading_date=?""",
            (payload["trading_date"],),
        ).fetchone()
        if existing is not None:
            replay = dict(existing)
            same = all(replay.get(key) == value for key, value in payload.items())
            if not same:
                raise ValueError(
                    "该交易日已有不同的不可变账户净值；不得静默覆盖"
                )
            return {**replay, "idempotent_replay": True}
        recorded_at = now.isoformat(timespec="seconds")
        cur = conn.execute(
            """INSERT INTO timing_account_nav_snapshots
               (trading_date, account_equity_yuan, cash_balance_yuan,
                market_value_yuan, valuation_as_of, valuation_basis,
                recorded_at, source, ledger_identity_sha256)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                payload["trading_date"], equity, cash, market_value,
                payload["valuation_as_of"], basis, recorded_at,
                payload["source"], identity,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM timing_account_nav_snapshots WHERE id=?",
            (cur.lastrowid,),
        ).fetchone()
        return {**dict(row), "idempotent_replay": False}
    finally:
        conn.close()


def _entry_context_values(entry_context: dict | None) -> tuple[
    str | None, str | None, str | None, str | None, int | None,
]:
    """Normalize the server-owned source copied into a position ledger row."""
    if entry_context is None:
        return None, None, None, None, None
    if not isinstance(entry_context, dict):
        raise ValueError("成交来源上下文无效")
    origin = entry_context.get("entry_origin")
    strategy_id = entry_context.get("entry_strategy_id")
    evidence_ref_json = entry_context.get("entry_evidence_ref_json")
    signal_id = entry_context.get("entry_signal_id")
    alert_id = entry_context.get("entry_alert_id")
    if not isinstance(origin, str) or not origin or len(origin) > 128:
        raise ValueError("成交来源标识无效")
    if strategy_id is not None and (
        not isinstance(strategy_id, str) or not strategy_id or len(strategy_id) > 256
    ):
        raise ValueError("成交策略标识无效")
    if evidence_ref_json is not None:
        if not isinstance(evidence_ref_json, str) or not evidence_ref_json or len(evidence_ref_json) > 4096:
            raise ValueError("成交证据引用无效")
        try:
            evidence = json.loads(evidence_ref_json)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("成交证据引用不是有效 JSON") from exc
        if not isinstance(evidence, dict):
            raise ValueError("成交证据引用必须是 JSON 对象")
        evidence_ref_json = json.dumps(
            evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
    if signal_id is not None and (
        not isinstance(signal_id, str) or not signal_id or len(signal_id) > 256
    ):
        raise ValueError("成交信号标识无效")
    if alert_id is not None:
        if isinstance(alert_id, bool) or not isinstance(alert_id, int) or alert_id <= 0:
            raise ValueError("成交提醒标识无效")
    if origin == "production_strategy" and not all(
        (strategy_id, evidence_ref_json, signal_id, alert_id)
    ):
        raise ValueError("生产策略成交必须绑定完整策略、证据、信号和提醒")
    return origin, strategy_id, evidence_ref_json, signal_id, alert_id


def _sellable_inventory_values(
    total_shares: int,
    sellable_shares_snapshot: int | None,
    sellable_as_of: str | None,
) -> tuple[int | None, str | None]:
    """Validate one explicit broker/user sellable-inventory fact.

    Position creation time is not a purchase date for imported holdings, so a
    missing snapshot deliberately remains unknown instead of being inferred as
    either zero or the full holding.
    """
    if (sellable_shares_snapshot is None) != (sellable_as_of is None):
        raise ValueError("可卖股数与其数据时间必须同时提供")
    if sellable_shares_snapshot is None:
        return None, None
    if isinstance(sellable_shares_snapshot, bool) or not isinstance(
        sellable_shares_snapshot, int
    ):
        raise ValueError("可卖股数必须是整数")
    if sellable_shares_snapshot < 0 or sellable_shares_snapshot > int(total_shares):
        raise ValueError("可卖股数必须在0至当前持仓股数之间")
    return sellable_shares_snapshot, _normalize_sellable_as_of(sellable_as_of)


def _normalize_sellable_as_of(sellable_as_of: str | None) -> str:
    if not isinstance(sellable_as_of, str) or not sellable_as_of.strip():
        raise ValueError("可卖库存数据时间无效")
    value = sellable_as_of.strip()
    try:
        if len(value) == 10:
            datetime.strptime(value, "%Y-%m-%d")
            normalized_as_of = value
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError
            normalized_as_of = parsed.astimezone(_SH_TZ).isoformat(timespec="seconds")
    except ValueError as exc:
        raise ValueError("可卖库存数据时间必须是日期或带时区的ISO时间") from exc
    return normalized_as_of


def _same_day_sellable_inventory(
    row: sqlite3.Row, day: str,
) -> int | None:
    """Return an explicit same-day sellable cap, or unknown for absent/stale data."""
    snapshot = row["sellable_shares_snapshot"]
    as_of = row["sellable_as_of"]
    if snapshot is None and as_of is None:
        return None
    if snapshot is None or as_of is None:
        raise ValueError("持仓可卖库存快照不完整，请先重新核验")
    try:
        snapshot_day = _normalize_sellable_as_of(str(as_of))[:10]
    except ValueError as exc:
        raise ValueError("持仓可卖库存数据时间无效，请先重新核验") from exc
    if snapshot_day != day:
        return None
    sellable = int(snapshot)
    if sellable < 0 or sellable > int(row["shares"] or 0):
        raise ValueError("持仓可卖库存快照超出当前持仓，请先重新核验")
    return sellable


def confirm_buy(code: str, name: str, price: float, shares: int,
                score: float, level: str, window_label: str,
                action: str, idempotency_key: str | None = None,
                pre_insert_validator=None, entry_context: dict | None = None,
                buy_fee_yuan: float | None = None,
                entry_reference_price: float | None = None,
                adjust_cash: bool = True,
                cash_snapshot_yuan: float | None = None,
                resolve_alert_in_transaction: bool = False,
                sellable_shares_snapshot: int | None = None,
                sellable_as_of: str | None = None) -> dict:
    sellable_shares_snapshot, sellable_as_of = _sellable_inventory_values(
        shares, sellable_shares_snapshot, sellable_as_of,
    )
    conn = _db()
    amount = round(price * shares, 2)
    # The user records an actual fill price, so do not charge modelled slippage
    # again.  Broker commission, transfer fee and later sell-side stamp tax are
    # still required to turn gross price change into net ledger PnL.
    if buy_fee_yuan is not None and (
        not math.isfinite(float(buy_fee_yuan))
        or float(buy_fee_yuan) < 0
        or float(buy_fee_yuan) > max(100.0, amount * 0.1)
    ):
        raise ValueError("买入实际费用超出合理范围")
    buy_cost_source = (
        "actual_user_recorded" if buy_fee_yuan is not None else "estimated_model"
    )
    buy_cost = round(
        float(buy_fee_yuan)
        if buy_fee_yuan is not None
        else amount * one_side_cost_rate(amount, "buy", slippage=0.0),
        2,
    )
    if entry_reference_price is not None and (
        not math.isfinite(float(entry_reference_price))
        or float(entry_reference_price) <= 0
    ):
        raise ValueError("买入提醒参考价无效")
    entry_reference_price = (
        float(entry_reference_price)
        if entry_reference_price is not None else None
    )
    entry_slippage_yuan = (
        round((float(price) - entry_reference_price) * shares, 2)
        if entry_reference_price is not None else None
    )
    entry_slippage_bps = (
        round((float(price) / entry_reference_price - 1.0) * 10_000, 4)
        if entry_reference_price is not None else None
    )
    now = _now_sh()
    created_at = now.isoformat(timespec="seconds")
    duplicate_after = (now - timedelta(seconds=15)).isoformat(timespec="seconds")
    entry_values = _entry_context_values(entry_context)
    resolution_alert_id = entry_values[-1] if resolve_alert_in_transaction else None
    if resolve_alert_in_transaction:
        if resolution_alert_id is None:
            raise ValueError("原子确认买入缺少精确提醒标识")
        if not idempotency_key:
            raise ValueError("原子确认买入缺少幂等键")
        _db_init_alerts(conn)
        conn.commit()
    if cash_snapshot_yuan is not None:
        cash_snapshot_yuan = float(cash_snapshot_yuan)
        if (
            not math.isfinite(cash_snapshot_yuan)
            or cash_snapshot_yuan < 0
            or cash_snapshot_yuan > 10_000_000_000
        ):
            raise ValueError("剩余现金必须是 0 至 100 亿元之间的有限金额")
        if adjust_cash or action != "position_snapshot":
            raise ValueError("现金快照仅允许与既有持仓导入原子写入")
        cash_snapshot_yuan = round(cash_snapshot_yuan, 2)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if idempotency_key:
            row = conn.execute(
                "SELECT * FROM timing_positions WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row:
                if (
                    row["code"] != code
                    or float(row["price"]) != float(price)
                    or int(row["initial_shares"] if row["initial_shares"] is not None else row["shares"]) != int(shares)
                    or row["action"] != action
                    or abs(float(row["buy_cost"] or 0) - buy_cost) > 1e-9
                    or row["buy_cost_source"] != buy_cost_source
                    or row["entry_reference_price"] != entry_reference_price
                    or row["cash_snapshot_yuan"] != cash_snapshot_yuan
                    or row["sellable_shares_snapshot"] != sellable_shares_snapshot
                    or row["sellable_as_of"] != sellable_as_of
                    or tuple(row[key] for key in (
                        "entry_origin", "entry_strategy_id", "entry_evidence_ref_json",
                        "entry_signal_id", "entry_alert_id",
                    )) != entry_values
                ):
                    raise IdempotencyConflictError(
                        "幂等键已用于另一笔不同的持仓记录"
                    )
                decision = _record_alert_decision(
                    conn, int(resolution_alert_id), "confirmed",
                    "buy_fill_recorded", str(idempotency_key),
                    position_id=int(row["id"]),
                ) if resolve_alert_in_transaction else None
                conn.commit() if resolve_alert_in_transaction else conn.rollback()
                result = {**dict(row), "idempotent_replay": True}
                if decision is not None:
                    result["alert_decision"] = decision
                return result
        # Older clients do not yet send Idempotency-Key.  The short exact-match
        # window closes double-click/retry races without preventing a later,
        # intentional add-on purchase.
        row = conn.execute(
            """SELECT * FROM timing_positions
               WHERE code=? AND price=? AND shares=? AND action=?
                 AND entry_origin IS ? AND entry_strategy_id IS ?
                 AND entry_evidence_ref_json IS ? AND entry_signal_id IS ?
                 AND entry_alert_id IS ?
                 AND entry_reference_price IS ?
                 AND cash_snapshot_yuan IS ?
                 AND sellable_shares_snapshot IS ?
                 AND sellable_as_of IS ?
                 AND created_at>=?
               ORDER BY id DESC LIMIT 1""",
            (code, price, shares, action, *entry_values,
             entry_reference_price, cash_snapshot_yuan,
             sellable_shares_snapshot, sellable_as_of, duplicate_after),
        ).fetchone()
        if row:
            decision = _record_alert_decision(
                conn, int(resolution_alert_id), "confirmed",
                "buy_fill_recorded", str(idempotency_key),
                position_id=int(row["id"]),
            ) if resolve_alert_in_transaction else None
            conn.commit() if resolve_alert_in_transaction else conn.rollback()
            result = {**dict(row), "idempotent_replay": True}
            if decision is not None:
                result["alert_decision"] = decision
            return result
        if pre_insert_validator is not None:
            # The callback runs after BEGIN IMMEDIATE and after idempotency
            # replay detection. It can re-read the ledger atomically before
            # this new row is inserted.
            pre_insert_validator(conn)
        account = conn.execute(
            "SELECT cash_balance FROM timing_account WHERE id=1"
        ).fetchone()
        cash_adjusted = int(bool(adjust_cash and account is not None))
        if cash_adjusted:
            required_cash = amount + buy_cost
            current_cash = float(account["cash_balance"])
            if required_cash > current_cash + 0.005:
                raise ValueError(
                    f"成交金额及买入费用 ¥{required_cash:.2f} 超过用户录入剩余现金 ¥{current_cash:.2f}"
                )
            conn.execute(
                """UPDATE timing_account
                   SET cash_balance=?, updated_at=?, source='manual_snapshot'
                   WHERE id=1""",
                (
                    round(current_cash - required_cash, 2),
                    _now_sh().isoformat(timespec="seconds"),
                ),
            )
        elif cash_snapshot_yuan is not None:
            conn.execute(
                """INSERT INTO timing_account (id, cash_balance, updated_at, source)
                   VALUES (1, ?, ?, 'manual_position_snapshot')
                   ON CONFLICT(id) DO UPDATE SET
                       cash_balance=excluded.cash_balance,
                       updated_at=excluded.updated_at,
                       source=excluded.source""",
                (cash_snapshot_yuan, created_at),
            )
        cur = conn.execute(
            """INSERT INTO timing_positions
               (code, name, price, shares, amount, score, level, window_label,
                action, status, created_at, idempotency_key, initial_shares,
                buy_cost, realized_pnl_gross, sell_cost, pnl,
                buy_cost_source, sell_cost_source, entry_origin,
                entry_strategy_id, entry_evidence_ref_json, entry_signal_id,
                entry_alert_id, entry_reference_price, entry_slippage_yuan,
                entry_slippage_bps, cash_adjusted, cash_snapshot_yuan,
                sellable_shares_snapshot, sellable_as_of)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (code, name, price, shares, amount, score, level, window_label,
             action, "open", created_at, idempotency_key, shares,
             buy_cost, 0.0, 0.0, 0.0, buy_cost_source, None, *entry_values,
             entry_reference_price, entry_slippage_yuan, entry_slippage_bps,
             cash_adjusted, cash_snapshot_yuan, sellable_shares_snapshot,
             sellable_as_of),
        )
        decision = _record_alert_decision(
            conn, int(resolution_alert_id), "confirmed",
            "buy_fill_recorded", str(idempotency_key),
            position_id=int(cur.lastrowid),
        ) if resolve_alert_in_transaction else None
        conn.commit()
        row = conn.execute(
            "SELECT * FROM timing_positions WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        result = dict(row)
        if decision is not None:
            result["alert_decision"] = decision
        return result
    except sqlite3.IntegrityError:
        conn.rollback()
        if not idempotency_key:
            raise
        row = conn.execute(
            "SELECT * FROM timing_positions WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if not row:
            raise
        if (
            row["code"] != code
            or float(row["price"]) != float(price)
            or int(row["initial_shares"] if row["initial_shares"] is not None else row["shares"]) != int(shares)
            or row["action"] != action
            or abs(float(row["buy_cost"] or 0) - buy_cost) > 1e-9
            or row["buy_cost_source"] != buy_cost_source
            or row["entry_reference_price"] != entry_reference_price
            or row["cash_snapshot_yuan"] != cash_snapshot_yuan
            or row["sellable_shares_snapshot"] != sellable_shares_snapshot
            or row["sellable_as_of"] != sellable_as_of
            or tuple(row[key] for key in (
                "entry_origin", "entry_strategy_id", "entry_evidence_ref_json",
                "entry_signal_id", "entry_alert_id",
            )) != entry_values
        ):
            raise IdempotencyConflictError(
                "幂等键已用于另一笔不同的持仓记录"
            )
        decision = None
        if resolve_alert_in_transaction:
            conn.execute("BEGIN IMMEDIATE")
            decision = _record_alert_decision(
                conn, int(resolution_alert_id), "confirmed",
                "buy_fill_recorded", str(idempotency_key),
                position_id=int(row["id"]),
            )
            conn.commit()
        result = {**dict(row), "idempotent_replay": True}
        if decision is not None:
            result["alert_decision"] = decision
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_position_by_idempotency_key(idempotency_key: str | None) -> dict | None:
    if not idempotency_key:
        return None
    conn = _db()
    try:
        row = conn.execute(
            "SELECT * FROM timing_positions WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_sellable_inventory_snapshot(
    position_id: int,
    sellable_shares_snapshot: int,
    sellable_as_of: str,
    idempotency_key: str,
) -> dict:
    """Atomically record one explicit sellable-inventory fact for an open position."""
    if isinstance(position_id, bool) or not isinstance(position_id, int) or position_id <= 0:
        raise ValueError("持仓标识无效")
    if (
        not isinstance(idempotency_key, str)
        or not idempotency_key.strip()
        or len(idempotency_key.strip()) > 256
    ):
        raise ValueError("更新可卖库存必须提供有效幂等键")
    if (
        isinstance(sellable_shares_snapshot, bool)
        or not isinstance(sellable_shares_snapshot, int)
        or sellable_shares_snapshot < 0
    ):
        raise ValueError("可卖股数必须是非负整数")
    idempotency_key = idempotency_key.strip()
    sellable_as_of = _normalize_sellable_as_of(sellable_as_of)
    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        event = conn.execute(
            """SELECT * FROM timing_sellable_inventory_events
               WHERE idempotency_key=?""",
            (idempotency_key,),
        ).fetchone()
        if event:
            if (
                int(event["position_id"]) != position_id
                or int(event["new_sellable_shares_snapshot"])
                != sellable_shares_snapshot
                or event["new_sellable_as_of"] != sellable_as_of
            ):
                raise IdempotencyConflictError(
                    "可卖库存幂等键已用于另一笔不同记录"
                )
            conn.rollback()
            return {**dict(event), "idempotent_replay": True}
        row = conn.execute(
            "SELECT * FROM timing_positions WHERE id=? AND status='open'",
            (position_id,),
        ).fetchone()
        if not row:
            raise ValueError("仅可更新当前开放持仓的可卖库存")
        sellable_shares_snapshot, sellable_as_of = _sellable_inventory_values(
            int(row["shares"] or 0), sellable_shares_snapshot, sellable_as_of,
        )
        recorded_at = _now_sh().isoformat(timespec="seconds")
        cur = conn.execute(
            """INSERT INTO timing_sellable_inventory_events
               (position_id, previous_sellable_shares_snapshot,
                previous_sellable_as_of, new_sellable_shares_snapshot,
                new_sellable_as_of, recorded_at, idempotency_key)
               VALUES (?,?,?,?,?,?,?)""",
            (
                position_id, row["sellable_shares_snapshot"],
                row["sellable_as_of"], sellable_shares_snapshot,
                sellable_as_of, recorded_at, idempotency_key,
            ),
        )
        updated = conn.execute(
            """UPDATE timing_positions
               SET sellable_shares_snapshot=?, sellable_as_of=?
               WHERE id=? AND status='open'""",
            (sellable_shares_snapshot, sellable_as_of, position_id),
        )
        if updated.rowcount != 1:
            raise ValueError("持仓状态已变化，可卖库存未更新")
        event = conn.execute(
            "SELECT * FROM timing_sellable_inventory_events WHERE id=?",
            (cur.lastrowid,),
        ).fetchone()
        conn.commit()
        return dict(event)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_position_exit_intent(
    position_id: int,
    target_shares: int,
    effective_date: str,
    note: str | None,
    idempotency_key: str,
) -> dict:
    """Record a user's future manual exit target without recording a trade.

    The intent may open an intraday manual-review plan once its effective date,
    a fresh quote and same-day sellable inventory are all available.  It never
    mutates shares, cash, fills or broker state.
    """
    if isinstance(position_id, bool) or not isinstance(position_id, int) or position_id <= 0:
        raise ValueError("持仓标识无效")
    if isinstance(target_shares, bool) or not isinstance(target_shares, int) or target_shares < 0:
        raise ValueError("目标持仓股数必须是非负整数")
    if (
        not isinstance(idempotency_key, str)
        or not idempotency_key.strip()
        or len(idempotency_key.strip()) > 256
    ):
        raise ValueError("退出计划必须提供有效幂等键")
    if not isinstance(effective_date, str):
        raise ValueError("退出计划生效日期无效")
    try:
        normalized_date = datetime.strptime(
            effective_date.strip(), "%Y-%m-%d",
        ).date().isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError("退出计划生效日期必须为YYYY-MM-DD") from exc
    normalized_note = note.strip() if isinstance(note, str) and note.strip() else None
    if normalized_note is not None and len(normalized_note) > 500:
        raise ValueError("退出计划备注不能超过500字")
    idempotency_key = idempotency_key.strip()

    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM timing_exit_intent_events WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            if (
                int(existing["position_id"]) != position_id
                or int(existing["target_shares"]) != target_shares
                or existing["effective_date"] != normalized_date
                or existing["note"] != normalized_note
            ):
                raise IdempotencyConflictError("退出计划幂等键已用于另一笔不同记录")
            conn.rollback()
            return {**dict(existing), "idempotent_replay": True}

        position = conn.execute(
            "SELECT * FROM timing_positions WHERE id=? AND status='open'",
            (position_id,),
        ).fetchone()
        if not position:
            raise ValueError("仅可为当前开放持仓设置退出计划")
        held_shares = int(position["shares"] or 0)
        if target_shares >= held_shares:
            raise ValueError("退出计划目标股数必须低于当前持仓")
        if target_shares and target_shares % 100:
            raise ValueError("退出计划目标股数必须为100股整数倍或0股")

        recorded_at = _now_sh().isoformat(timespec="seconds")
        cur = conn.execute(
            """INSERT INTO timing_exit_intent_events
               (position_id, previous_target_shares, previous_effective_date,
                target_shares, effective_date, note, recorded_at,
                idempotency_key)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                position_id, position["manual_exit_target_shares"],
                position["manual_exit_effective_date"], target_shares,
                normalized_date, normalized_note, recorded_at,
                idempotency_key,
            ),
        )
        updated = conn.execute(
            """UPDATE timing_positions
               SET manual_exit_target_shares=?, manual_exit_effective_date=?,
                   manual_exit_note=?, manual_exit_updated_at=?
               WHERE id=? AND status='open'""",
            (
                target_shares, normalized_date, normalized_note,
                recorded_at, position_id,
            ),
        )
        if updated.rowcount != 1:
            raise ValueError("持仓状态已变化，退出计划未写入")
        event = conn.execute(
            "SELECT * FROM timing_exit_intent_events WHERE id=?",
            (cur.lastrowid,),
        ).fetchone()
        conn.commit()
        return dict(event)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def count_risk_increasing_buys(
    conn: sqlite3.Connection, day: str,
) -> int:
    """Count actual buy fills; importing an existing holding is not a trade."""
    return int(conn.execute(
        """SELECT COUNT(*) FROM timing_positions
           WHERE substr(created_at,1,10)=?
             AND COALESCE(action,'')!='position_snapshot'""",
        (day,),
    ).fetchone()[0])


def is_position_snapshot(row: dict) -> bool:
    """Return whether a ledger row imports a pre-existing holding."""
    return (
        row.get("action") == "position_snapshot"
        or row.get("entry_origin") == "manual_position_snapshot"
    )


def trade_capacity(day: str | None = None) -> dict:
    """Report fills without imposing a personal-habit daily count quota."""
    target = day or _now_sh().date().isoformat()
    conn = _db()
    buys = count_risk_increasing_buys(conn, target)
    event_sells = conn.execute(
        "SELECT COUNT(*) FROM timing_sell_events WHERE substr(sold_at,1,10)=?",
        (target,),
    ).fetchone()[0]
    legacy_sells = conn.execute(
        """SELECT COUNT(*) FROM timing_positions AS p
           WHERE substr(p.sold_at,1,10)=?
             AND NOT EXISTS (
               SELECT 1 FROM timing_sell_events AS e WHERE e.position_id=p.id
             )""",
        (target,),
    ).fetchone()[0]
    sells = int(event_sells) + int(legacy_sells)
    open_positions = conn.execute(
        "SELECT COUNT(*) FROM timing_positions WHERE status='open'"
    ).fetchone()[0]
    conn.close()
    used = int(buys)
    return {
        "date": target,
        "limit": DAILY_TRADE_LIMIT,
        "used": used,
        "remaining": None,
        "daily_trade_remaining": None,
        "buys": int(buys),
        "sells": sells,
        "exits": sells,
        "total_manual_events": int(buys) + sells,
        "capacity_policy": (
            "no_personal_count_quota_cash_exposure_strategy_and_liquidity_bound"
            "_exits_never_blocked"
        ),
        "open_positions": int(open_positions),
        "max_open_positions": MAX_OPEN_POSITIONS,
        "position_slots_remaining": max(0, MAX_OPEN_POSITIONS - int(open_positions)),
        "limit_reached": False,
    }


def sell_position(
    position_id: int, sell_price: float, idempotency_key: str | None = None,
    shares: int | None = None, sell_fee_yuan: float | None = None,
    alert_id: int | None = None, reference_price: float | None = None,
    resolve_alert_in_transaction: bool = False,
) -> dict | None:
    """Record a full or partial manual fill and return the remaining position.

    ``shares=None`` preserves the old full-close API.  A partial fill must be a
    positive 100-share lot, or include the holding's complete odd-lot balance.
    Each fill is written as its own immutable event, so later reductions do not
    erase earlier prices, costs or realised PnL.
    """
    if (alert_id is None) != (reference_price is None):
        raise ValueError("卖出提醒标识与参考价必须同时提供")
    if alert_id is not None and (
        isinstance(alert_id, bool) or int(alert_id) <= 0
    ):
        raise ValueError("卖出提醒标识无效")
    if reference_price is not None and (
        not math.isfinite(float(reference_price)) or float(reference_price) <= 0
    ):
        raise ValueError("卖出提醒参考价无效")
    alert_id = int(alert_id) if alert_id is not None else None
    reference_price = (
        float(reference_price) if reference_price is not None else None
    )
    if resolve_alert_in_transaction and (alert_id is None or not idempotency_key):
        raise ValueError("原子确认卖出缺少精确提醒或幂等键")
    conn = _db()
    try:
        if resolve_alert_in_transaction:
            _db_init_alerts(conn)
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        if idempotency_key:
            event = conn.execute(
                "SELECT * FROM timing_sell_events WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if event:
                if (
                    int(event["position_id"]) != int(position_id)
                    or abs(float(event["price"]) - float(sell_price)) > 1e-9
                    or (shares is not None and int(event["shares"]) != int(shares))
                    or event["alert_id"] != alert_id
                    or event["reference_price"] != reference_price
                    or (
                        sell_fee_yuan is not None
                        and (
                            event["sell_cost_source"] != "actual_user_recorded"
                            or abs(float(event["sell_cost"]) - float(sell_fee_yuan)) > 1e-9
                        )
                    )
                    or (
                        sell_fee_yuan is None
                        and event["sell_cost_source"] == "actual_user_recorded"
                    )
                ):
                    raise IdempotencyConflictError(
                        "卖出幂等键已用于另一笔不同记录"
                    )
                replay = conn.execute(
                    "SELECT * FROM timing_positions WHERE id=?", (position_id,),
                ).fetchone()
                decision = _record_alert_decision(
                    conn, int(alert_id), "confirmed", "sell_fill_recorded",
                    str(idempotency_key), position_id=int(position_id),
                    sell_event_id=int(event["id"]),
                ) if resolve_alert_in_transaction else None
                conn.commit() if resolve_alert_in_transaction else conn.rollback()
                result = {
                    **dict(replay), "sell_event": dict(event),
                    "idempotent_replay": True,
                }
                if decision is not None:
                    result["alert_decision"] = decision
                return result
            # Compatibility for closed rows written before sell events existed.
            replay = conn.execute(
                "SELECT * FROM timing_positions WHERE sell_idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if replay:
                if resolve_alert_in_transaction:
                    raise ValueError("旧版卖出记录缺少不可变成交事件，不能确认提醒")
                requested = int(shares) if shares is not None else int(replay["initial_shares"] or replay["shares"] or 0)
                if (
                    int(replay["id"]) != int(position_id)
                    or abs(float(replay["sold_price"] or 0) - float(sell_price)) > 1e-9
                    or requested != int(replay["initial_shares"] or requested)
                ):
                    raise IdempotencyConflictError(
                        "卖出幂等键已用于另一笔不同记录"
                    )
                conn.rollback()
                return {**dict(replay), "idempotent_replay": True}
        row = conn.execute(
            "SELECT * FROM timing_positions WHERE id=? AND status='open'",
            (position_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            return None
        available = int(row["shares"] or 0)
        sold_shares = available if shares is None else int(shares)
        sell_now = _now_sh()
        same_day_sellable = _same_day_sellable_inventory(
            row, sell_now.date().isoformat(),
        )
        odd_lot = available % 100
        if sold_shares <= 0 or (
            sold_shares % 100 and sold_shares % 100 != odd_lot
        ):
            raise ValueError("卖出数量须为100股整数倍，或一次性包含全部零股")
        if sold_shares > available:
            raise ValueError("卖出数量超过当前账本持仓")
        if same_day_sellable is not None and sold_shares > same_day_sellable:
            raise ValueError("卖出数量超过同日明确可卖股数")
        initial_shares = int(row["initial_shares"] or available)
        initial_amount = float(row["price"]) * initial_shares
        buy_cost_total = row["buy_cost"]
        if buy_cost_total is None:
            buy_cost_total = initial_amount * one_side_cost_rate(
                initial_amount, "buy", slippage=0.0,
            )
        allocated_buy_cost = float(buy_cost_total) * sold_shares / initial_shares
        sell_amount = float(sell_price) * sold_shares
        if sell_fee_yuan is not None and (
            not math.isfinite(float(sell_fee_yuan))
            or float(sell_fee_yuan) < 0
            or float(sell_fee_yuan) > max(100.0, sell_amount * 0.1)
        ):
            raise ValueError("卖出实际费用超出合理范围")
        sell_cost_source = (
            "actual_user_recorded"
            if sell_fee_yuan is not None else "estimated_model"
        )
        sell_cost = (
            float(sell_fee_yuan)
            if sell_fee_yuan is not None
            else sell_amount * one_side_cost_rate(
                sell_amount, "sell", slippage=0.0,
            )
        )
        gross_pnl = (float(sell_price) - float(row["price"])) * sold_shares
        net_pnl = gross_pnl - allocated_buy_cost - sell_cost
        sell_slippage_yuan = (
            round((reference_price - float(sell_price)) * sold_shares, 2)
            if reference_price is not None else None
        )
        sell_slippage_bps = (
            round((reference_price - float(sell_price)) / reference_price * 10_000, 4)
            if reference_price is not None else None
        )
        previous_net = float(row["pnl"] or 0.0)
        previous_gross = float(row["realized_pnl_gross"] or 0.0)
        previous_sell_cost = float(row["sell_cost"] or 0.0)
        remaining = available - sold_shares
        remaining_sellable = (
            same_day_sellable - sold_shares
            if same_day_sellable is not None
            else row["sellable_shares_snapshot"]
        )
        sold_at = sell_now.isoformat(timespec="seconds")
        cur = conn.execute(
            """INSERT INTO timing_sell_events
               (position_id, shares, price, amount, gross_pnl,
                allocated_buy_cost, sell_cost, sell_cost_source, alert_id,
                reference_price, slippage_yuan, slippage_bps, net_pnl,
                sold_at, idempotency_key)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                position_id, sold_shares, sell_price, round(sell_amount, 2),
                round(gross_pnl, 2), round(allocated_buy_cost, 2),
                round(sell_cost, 2), sell_cost_source, alert_id,
                reference_price, sell_slippage_yuan, sell_slippage_bps,
                round(net_pnl, 2), sold_at, idempotency_key,
            ),
        )
        previous_sell_cost_source = row["sell_cost_source"]
        aggregate_sell_cost_source = (
            sell_cost_source
            if not previous_sell_cost_source
            else previous_sell_cost_source
            if previous_sell_cost_source == sell_cost_source
            else "mixed_actual_and_estimated"
        )
        previous_slippage = row["sell_slippage_yuan"]
        aggregate_sell_slippage = (
            (float(previous_slippage) if previous_slippage is not None else 0.0)
            + (float(sell_slippage_yuan) if sell_slippage_yuan is not None else 0.0)
            if previous_slippage is not None or sell_slippage_yuan is not None
            else None
        )
        attributed_events = int(row["sell_slippage_attributed_events"] or 0)
        if sell_slippage_yuan is not None:
            attributed_events += 1
        fill_events = int(row["sell_fill_events"] or 0) + 1
        status = "closed" if remaining == 0 else "open"
        updated = conn.execute(
            """UPDATE timing_positions
               SET status=?, shares=?, amount=?, sold_at=?, sold_price=?,
                   pnl=?, realized_pnl_gross=?, sell_cost=?,
                   buy_cost=?, initial_shares=?, sell_idempotency_key=?,
                   sell_cost_source=?, sell_slippage_yuan=?,
                   sell_slippage_attributed_events=?, sell_fill_events=?,
                   sellable_shares_snapshot=?
               WHERE id=? AND status='open'""",
            (
                status, remaining, round(float(row["price"]) * remaining, 2),
                sold_at, sell_price, round(previous_net + net_pnl, 2),
                round(previous_gross + gross_pnl, 2),
                round(previous_sell_cost + sell_cost, 2),
                round(float(buy_cost_total), 2), initial_shares,
                idempotency_key, aggregate_sell_cost_source,
                aggregate_sell_slippage, attributed_events, fill_events,
                remaining_sellable, position_id,
            ),
        )
        if updated.rowcount != 1:
            conn.rollback()
            return None
        account = conn.execute(
            "SELECT cash_balance FROM timing_account WHERE id=1"
        ).fetchone()
        if account is not None:
            conn.execute(
                """UPDATE timing_account
                   SET cash_balance=?, updated_at=?, source='manual_snapshot'
                   WHERE id=1""",
                (
                    round(float(account["cash_balance"]) + sell_amount - sell_cost, 2),
                    sold_at,
                ),
            )
        decision = _record_alert_decision(
            conn, int(alert_id), "confirmed", "sell_fill_recorded",
            str(idempotency_key), position_id=int(position_id),
            sell_event_id=int(cur.lastrowid),
        ) if resolve_alert_in_transaction else None
        conn.commit()
        row = conn.execute(
            "SELECT * FROM timing_positions WHERE id=?", (position_id,)
        ).fetchone()
        event = conn.execute(
            "SELECT * FROM timing_sell_events WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        result = {**dict(row), "sell_event": dict(event)}
        if decision is not None:
            result["alert_decision"] = decision
        return result
    except sqlite3.IntegrityError:
        conn.rollback()
        if not idempotency_key:
            raise
        event = conn.execute(
            "SELECT * FROM timing_sell_events WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if not event:
            raise
        if (
            int(event["position_id"]) != int(position_id)
            or abs(float(event["price"]) - float(sell_price)) > 1e-9
            or (shares is not None and int(event["shares"]) != int(shares))
            or event["alert_id"] != alert_id
            or event["reference_price"] != reference_price
            or (
                sell_fee_yuan is not None
                and (
                    event["sell_cost_source"] != "actual_user_recorded"
                    or abs(float(event["sell_cost"]) - float(sell_fee_yuan)) > 1e-9
                )
            )
            or (
                sell_fee_yuan is None
                and event["sell_cost_source"] == "actual_user_recorded"
            )
        ):
            raise IdempotencyConflictError("卖出幂等键已用于另一笔不同记录")
        row = conn.execute(
            "SELECT * FROM timing_positions WHERE id=?", (position_id,),
        ).fetchone()
        decision = None
        if resolve_alert_in_transaction:
            conn.execute("BEGIN IMMEDIATE")
            decision = _record_alert_decision(
                conn, int(alert_id), "confirmed", "sell_fill_recorded",
                str(idempotency_key), position_id=int(position_id),
                sell_event_id=int(event["id"]),
            )
            conn.commit()
        result = {**dict(row), "sell_event": dict(event), "idempotent_replay": True}
        if decision is not None:
            result["alert_decision"] = decision
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_position(
    position_id: int, idempotency_key: str | None = None,
) -> dict | None:
    """Atomically delete one mistaken ledger row; replay the same keyed result."""
    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if idempotency_key:
            deletion = conn.execute(
                "SELECT * FROM timing_delete_events WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if deletion:
                if int(deletion["position_id"]) != int(position_id):
                    raise IdempotencyConflictError(
                        "删除幂等键已用于另一条持仓记录"
                    )
                conn.rollback()
                return {
                    **json.loads(deletion["position_json"]),
                    "idempotent_replay": True,
                }
        row = conn.execute(
            "SELECT * FROM timing_positions WHERE id=?", (position_id,)
        ).fetchone()
        if not row:
            conn.rollback()
            return None
        if int(row["sell_fill_events"] or 0) > 0:
            raise ValueError("已有卖出成交的持仓不能删除；请保留完整成交审计链")
        if conn.execute("SELECT 1 FROM timing_fill_fee_events WHERE fill_id=? LIMIT 1", (f"buy:{position_id}",)).fetchone():
            raise ValueError("已补记实际费用的成交不能删除；请保留费用与现金核对审计链")
        removed = dict(row)
        if idempotency_key:
            conn.execute(
                """INSERT INTO timing_delete_events
                   (position_id, position_json, deleted_at, idempotency_key)
                   VALUES (?,?,?,?)""",
                (
                    position_id,
                    json.dumps(removed, ensure_ascii=False, sort_keys=True),
                    _now_sh().isoformat(timespec="seconds"),
                    idempotency_key,
                ),
            )
        conn.execute(
            "DELETE FROM timing_sell_events WHERE position_id=?", (position_id,)
        )
        if row["status"] == "open" and int(row["cash_adjusted"] or 0) == 1:
            account = conn.execute(
                "SELECT cash_balance FROM timing_account WHERE id=1"
            ).fetchone()
            if account is not None:
                remaining_amount = float(row["price"] or 0) * int(row["shares"] or 0)
                initial_shares = int(row["initial_shares"] or row["shares"] or 0)
                remaining_buy_cost = (
                    float(row["buy_cost"] or 0) * int(row["shares"] or 0) / initial_shares
                    if initial_shares > 0 else 0.0
                )
                conn.execute(
                    """UPDATE timing_account
                       SET cash_balance=?, updated_at=?, source='manual_snapshot'
                       WHERE id=1""",
                    (
                        round(float(account["cash_balance"]) + remaining_amount + remaining_buy_cost, 2),
                        _now_sh().isoformat(timespec="seconds"),
                    ),
                )
        deleted = conn.execute(
            "DELETE FROM timing_positions WHERE id=?", (position_id,)
        )
        if deleted.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        return removed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_positions(status: str | None = None) -> list[dict]:
    conn = _db()
    if status:
        rows = conn.execute(
            "SELECT * FROM timing_positions WHERE status=? ORDER BY id DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM timing_positions ORDER BY id DESC"
        ).fetchall()
    positions = [dict(row) for row in rows]
    if positions:
        ids = [int(row["id"]) for row in positions]
        marks = ",".join("?" for _ in ids)
        events = conn.execute(
            f"""SELECT * FROM timing_sell_events
                WHERE position_id IN ({marks}) ORDER BY id ASC""",
            ids,
        ).fetchall()
        by_position: dict[int, list[dict]] = {row_id: [] for row_id in ids}
        for event in events:
            by_position[int(event["position_id"])].append(dict(event))
        for position in positions:
            position["sell_events"] = by_position[int(position["id"])]
    conn.close()
    return positions


def list_sell_events(position_id: int | None = None) -> list[dict]:
    """Return immutable manual sell fills, newest first."""
    conn = _db()
    try:
        if position_id is None:
            rows = conn.execute(
                "SELECT * FROM timing_sell_events ORDER BY id DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM timing_sell_events
                   WHERE position_id=? ORDER BY id DESC""",
                (position_id,),
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def manual_fill_review(positions: list[dict], day: str, *, basis: str = "recorded") -> dict:
    """Join immutable exit fills to entry facts without inventing fee or strategy evidence."""
    from .manual_fill_time import review_time, unassigned_may_belong
    if basis not in {"recorded", "execution"}:
        raise ValueError("复盘时间口径无效")
    def number(value):
        if value is None or isinstance(value, bool) or value == "":
            return None
        try:
            result = float(value)
            return result if math.isfinite(result) else None
        except (TypeError, ValueError):
            return None

    items, unassigned = [], []
    for position in positions:
        events = position.get("sell_events") or []
        for event in events:
            selected_time = review_time(event, "sold_at", basis)
            if basis == "execution" and unassigned_may_belong(event, "sold_at", day):
                unassigned.append({"fill_id": f"sell:{event.get('id')}", "name": position.get("name"),
                                   "recorded_at": event.get("sold_at"), "reason": "实际成交日期未补记，不能确定是否属于所选日期"})
            if selected_time[:10] != day:
                continue
            gross = number(event.get("gross_pnl"))
            buy_fee = number(event.get("allocated_buy_cost"))
            sell_fee = number(event.get("sell_cost"))
            net = number(event.get("net_pnl"))
            initial_shares, total_buy_fee = number(position.get("initial_shares")), number(position.get("buy_cost"))
            price, entry_price, shares = (number(value) for value in (event.get("price"), position.get("price"), event.get("shares")))
            facts_complete = all(value is not None and value > 0 for value in (price, entry_price, shares))
            facts_complete = facts_complete and shares.is_integer() and event.get("position_id") == position.get("id") and event.get("id") is not None
            sources = (position.get("buy_cost_source"), event.get("sell_cost_source"))
            fee_complete = all(source in {"actual_user_recorded", "estimated_model"} for source in sources)
            fee_complete = fee_complete and all(value is not None and value >= 0 for value in (buy_fee, sell_fee))
            fee_complete = fee_complete and total_buy_fee is not None and total_buy_fee >= 0
            facts_complete = facts_complete and initial_shares is not None and initial_shares.is_integer() and initial_shares >= shares
            consistent = gross is not None and net is not None and fee_complete and facts_complete
            consistent = consistent and abs(total_buy_fee * shares / initial_shares - buy_fee) <= 0.015
            consistent = consistent and abs((price - entry_price) * shares - gross) <= 0.015
            consistent = consistent and abs(gross - buy_fee - sell_fee - net) <= 0.015
            consistent = consistent and not is_position_snapshot(position)
            consistent = consistent and not event.get("fee_correction_error")
            status = (
                "actual" if consistent and all(source == "actual_user_recorded" for source in sources)
                else "estimated" if consistent
                else "inconsistent" if fee_complete and facts_complete and not is_position_snapshot(position) and gross is not None and net is not None
                else "missing"
            )
            items.append({
                "fill_id": f"sell:{event.get('id')}", "sell_event_id": event.get("id"),
                "position_id": position.get("id"), "code": position.get("code"),
                "name": position.get("name"), "recorded_at": event.get("sold_at"),
                "review_at": selected_time,
                "execution_time_fact": event.get("execution_time_fact"),
                "shares": event.get("shares"), "price": event.get("price"),
                "entry_price": position.get("price"),
                "entry_origin": position.get("entry_origin"),
                "entry_alert_id": position.get("entry_alert_id"),
                "exit_alert_id": event.get("alert_id"),
                "entry_signal_id": position.get("entry_signal_id"),
                "entry_strategy_id": position.get("entry_strategy_id"),
                "entry_is_snapshot": is_position_snapshot(position),
                "reason": event.get("fee_correction_error") or ("导入持仓缺少原始成交与成本口径，不计为完整成交收益" if is_position_snapshot(position) else None),
                "recorded_costs": event.get("recorded_costs"),
                "buy_fee_fact": event.get("buy_fee_fact"), "sell_fee_fact": event.get("sell_fee_fact"),
                "gross_pnl_yuan": gross, "allocated_buy_fee_yuan": buy_fee,
                "sell_fee_yuan": sell_fee, "cost_status": status,
                "net_pnl_yuan": round(net, 2) if consistent else None,
            })
        if not events and position.get("status") == "closed" and basis == "execution" and unassigned_may_belong({"sold_at": position.get("sold_at")}, "sold_at", day):
            unassigned.append({"fill_id": f"legacy:{position.get('id')}", "name": position.get("name"),
                               "recorded_at": position.get("sold_at"), "reason": "旧平仓缺少逐笔事件与实际成交日期"})
        if basis == "recorded" and not events and position.get("status") == "closed" and str(position.get("sold_at") or "")[:10] == day:
            items.append({
                "fill_id": f"legacy:{position.get('id')}", "position_id": position.get("id"),
                "code": position.get("code"), "name": position.get("name"),
                "recorded_at": position.get("sold_at"), "cost_status": "missing",
                "net_pnl_yuan": None, "gross_pnl_yuan": None,
                "reason": "旧记录缺少逐笔成交事件，不能核验成本后收益",
            })
    items.sort(key=lambda row: (row.get("review_at") or row.get("recorded_at") or "", row["fill_id"]))
    complete = not unassigned and all(item["net_pnl_yuan"] is not None for item in items)
    status = (
        "missing" if not complete else "no_fills" if not items
        else "actual" if all(item["cost_status"] == "actual" for item in items)
        else "estimated"
    )
    return {
        "date": day, "items": items, "fill_count": len(items), "cost_status": status,
        "time_basis": basis, "unassigned_fills": unassigned,
        "net_pnl_yuan": round(sum(item["net_pnl_yuan"] for item in items), 2) if complete else None,
        "actual_fee_fill_count": sum(item["cost_status"] == "actual" for item in items),
        "estimated_fee_fill_count": sum(item["cost_status"] == "estimated" for item in items),
        "unverified_fill_count": sum(item["net_pnl_yuan"] is None for item in items),
        "strategy_qualification_granted": False,
        "note": ("按用户补记的实际成交日期复盘；未补日期另列，不冒充当日完整结果。" if basis == "execution" else "按录入日期复盘，不等于实际成交日期。")
                + "时间补记不改现金、库存或提醒状态；编号只供追溯，未关联不推断策略。模型估费不是实录费用，也不是验证收益。",
        "fee_revision_note": "后补实费按原始买量累计分摊到各笔卖出；原记录不覆盖，当前现金另由用户明确核对。重述收益不是交易当时已知的研究证据。",
    }


def list_sellable_inventory_events(position_id: int | None = None) -> list[dict]:
    """Return immutable sellable-inventory updates, newest first."""
    conn = _db()
    try:
        if position_id is None:
            rows = conn.execute(
                "SELECT * FROM timing_sellable_inventory_events ORDER BY id DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM timing_sellable_inventory_events
                   WHERE position_id=? ORDER BY id DESC""",
                (position_id,),
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _fund_flow_snapshot_doc() -> dict | None:
    """读取 fund_flow_live.json 顶层元数据（带 reason 的失败返回）。"""
    ff_path = Path(__file__).resolve().parent.parent / "research" / "fund_flow_live.json"
    if not ff_path.exists():
        return {
            "ok": False,
            "reason": "资金流快照文件缺失（research/fund_flow_live.json 不存在）；不会把缺值解释为资金归零。",
        }
    try:
        data = json.loads(ff_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        return {
            "ok": False,
            "reason": f"资金流快照文件解析失败（{type(exc).__name__}）；不会把缺值解释为资金归零。",
        }
    return {
        "ok": True,
        "snapshot": {
            "ts": data.get("ts"),
            "date": data.get("date"),
            "source_name": data.get("source_name"),
            "n": data.get("n"),
            "snapshot_only": True,
            "note": "当日资金流快照（as_of 见 ts），不含 20 日历史，不构成资金持续性证据。",
        },
        "stocks": data.get("stocks") or {},
    }


def load_fund_flow_snapshot(code: str) -> dict:
    """取该股统一折算后的主力净流入 + 原始审计值与快照元数据。

    返回结构：
      {"net_yi": float|None, "raw_net_yi": float|None, "turnover": float|None,
       "ts": ..., "date": ...,
       "source_name": ..., "reason": str|None, "available": bool}
    ``net_yi`` 是全站展示/计算口径；原值只允许通过 ``raw_net_yi`` 溯源。
    """
    doc = _fund_flow_snapshot_doc()
    if not doc["ok"]:
        return {"net_yi": None, "turnover": None, "available": False, "reason": doc["reason"]}
    snap = doc["snapshot"]
    info = doc["stocks"].get(code)
    net_yi = None
    turnover = None
    if isinstance(info, dict):
        if info.get("net_yi") is not None:
            try:
                net_yi = float(info["net_yi"])
            except (TypeError, ValueError):
                net_yi = None
        if info.get("turnover") is not None:
            try:
                turnover = float(info["turnover"])
            except (TypeError, ValueError):
                turnover = None
    reason = None if net_yi is not None else (
        "资金流快照无该股数据（net_yi 缺失）；不会把缺值解释为资金归零。"
    )
    source_as_of = snap["ts"] or snap["date"]
    freshness = audited_trading_day_age(
        source_as_of, allow_current_session=True,
    ) if source_as_of else {
        "status": "unknown", "days": None, "reason": "资金流快照时点缺失",
    }
    normalized_net_yi = normalize_fund_flow_yi(net_yi)
    decision_eligible = normalized_net_yi is not None and freshness.get("status") == "fresh"
    if normalized_net_yi is not None and not decision_eligible:
        reason = (
            f"资金流快照时效为 {freshness.get('status')}；"
            "仅供溯源，不参与评分或决策。"
        )
    elif normalized_net_yi is None:
        reason = "资金流数值缺失或非有限数；不参与评分或决策。"
    return {
        "net_yi": normalized_net_yi,
        "raw_net_yi": net_yi,
        "normalized_net_yi": normalized_net_yi,
        "magnitude_scale": FLOW_MAGNITUDE_SCALE,
        "unit": "亿元（normalized）",
        "turnover": turnover,
        "ts": snap["ts"],
        "date": snap["date"],
        "source_name": snap["source_name"],
        "n": snap["n"],
        "snapshot_only": True,
        "note": snap["note"],
        "available": normalized_net_yi is not None,
        "decision_eligible": decision_eligible,
        "freshness_status": freshness.get("status"),
        "data_staleness_days": freshness.get("days"),
        "freshness": freshness,
        "reason": reason,
    }


def load_fund_flow_net(code: str) -> float | None:
    """旧签名兼容：返回主力净流入统一折算值；缺失/失败返回 None。

    新代码请用 load_fund_flow_snapshot()，它区分“无数据”与“数据为零”并给出原因。
    """
    return load_fund_flow_snapshot(code)["net_yi"]


# ── 止盈/止损线（今日机会实时监控用）──────────

def _board_limit_pct(code: str) -> float:
    """按板块返回涨跌停幅度：创业板/科创板 ±20%，其余 ±10%."""
    if code[:3] in ("300", "301", "688", "689"):
        return 20.0
    return 10.0


def compute_levels(
    price: float | None,
    code: str = "",
    prev_close: float | None = None,
) -> dict | None:
    """按现价/参考价计算止盈止损线（简易透明口径，可在页面展示依据）。

    - 硬止损 entry×(1−8%)；实际监控再与 2×ATR 取更严格者
    - 盈利 +15% 后启用 2×ATR 移动止损
    """
    if not price or price <= 0:
        return None
    lim = _board_limit_pct(code or "")
    entry = round(price, 2)
    stop_loss = round(price * 0.92, 2)
    take_profit_1 = round(price * 1.15, 2)
    take_profit_2 = round(price * 1.25, 2)
    if prev_close and prev_close > 0:
        limit_up = round(prev_close * (1 + lim / 100.0), 2)
        # 涨停封顶，但保证止盈2 ≥ 止盈1（接近涨停时两档可能都在涨停内）
        take_profit_2 = max(take_profit_1, min(take_profit_2, limit_up))
    risk_levels = [
        {
            "kind": "hard_stop",
            "value": stop_loss,
            "source": "entry_price_minus_8pct",
            "priority": 1,
            "active": True,
            "executable": False,
            "semantics": "基础硬风险复核线；只提示人工复核，不自动下单",
        },
        {
            "kind": "trailing_activation",
            "value": take_profit_1,
            "source": "entry_price_plus_15pct",
            "priority": 2,
            "active": False,
            "executable": False,
            "semantics": "达到后才启用ATR移动风险线；不是止盈订单",
        },
        {
            "kind": "research_target",
            "value": take_profit_2,
            "source": "entry_price_plus_25pct_capped_by_daily_limit",
            "priority": 3,
            "active": False,
            "executable": False,
            "semantics": "研究观察价；不是预测、推荐或订单价格",
        },
    ]
    return {
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "limit_pct": lim,
        "trailing_activation": take_profit_1,
        "risk_levels": risk_levels,
        "effective_risk_level": risk_levels[0],
        "note": "硬止损 -8%；+15% 后启用 2×ATR 移动止损（风控报告给出实际ATR线）",
    }


# ── 提醒生命周期（全屏倒计时触发/忽略/确认）──

def _db_init_alerts(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS timing_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            action TEXT NOT NULL,          -- buy / sell
            window_label TEXT,
            price REAL,
            score REAL,
            level TEXT,
            reason TEXT,
            origin TEXT,
            strategy_id TEXT,
            evidence_ref_json TEXT,
            signal_id TEXT,
            valid_until TEXT,
            action_plan_json TEXT,
            status TEXT NOT NULL DEFAULT 'triggered',  -- triggered/dismissed/confirmed/superseded/expired
            created_at TEXT NOT NULL,
            updated_at TEXT,
            resolved_at TEXT
        )
    """)
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(timing_alerts)")
    }
    for column in (
        "origin", "strategy_id", "evidence_ref_json", "signal_id",
        "valid_until", "action_plan_json", "updated_at",
    ):
        if column not in columns:
            conn.execute(f"ALTER TABLE timing_alerts ADD COLUMN {column} TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_alerts_code_action ON timing_alerts(code, action)"
    )
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_alerts_provenance
        ON timing_alerts(code, action, origin, strategy_id, evidence_ref_json, signal_id, id DESC)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS timing_alert_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id INTEGER NOT NULL UNIQUE,
            decision TEXT NOT NULL CHECK (decision IN ('dismissed', 'confirmed')),
            reason TEXT NOT NULL,
            position_id INTEGER,
            sell_event_id INTEGER,
            decided_at TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS position_risk_states (
            position_id INTEGER PRIMARY KEY,
            code TEXT NOT NULL,
            breached INTEGER NOT NULL CHECK (breached IN (0, 1)),
            episode INTEGER NOT NULL DEFAULT 0,
            episode_started_at TEXT,
            episode_kind TEXT,
            episode_risk_line REAL,
            current_risk_line REAL,
            last_price REAL,
            updated_at TEXT NOT NULL
        )
    """)


def observe_position_risk_state(
    position: dict, current_price: float, risk_line: float,
) -> dict:
    """Persist one position's risk-line state and identify actual crossings.

    A manual position snapshot first observed below its risk line is an existing
    breach discovered after import, not a newly timed sell signal.  A fresh
    crossing is emitted only after this process has previously observed the
    same position above the line.
    """
    position_id = position.get("id")
    if isinstance(position_id, bool) or not isinstance(position_id, int) or position_id <= 0:
        raise ValueError("持仓风险状态缺少有效持仓标识")
    try:
        price = float(current_price)
        line = float(risk_line)
    except (TypeError, ValueError):
        raise ValueError("持仓风险状态缺少有效价格或风险线") from None
    if not math.isfinite(price) or price <= 0 or not math.isfinite(line) or line <= 0:
        raise ValueError("持仓风险状态的价格或风险线无效")

    code = str(position.get("code") or "").strip()
    if not code:
        raise ValueError("持仓风险状态缺少证券代码")
    now = _now_sh().isoformat(timespec="seconds")
    breached = price <= line
    conn = _db()
    _db_init_alerts(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            "SELECT * FROM position_risk_states WHERE position_id=?",
            (position_id,),
        ).fetchone()
        previous_breached = bool(previous["breached"]) if previous is not None else False
        episode = int(previous["episode"] or 0) if previous is not None else 0
        episode_started_at = previous["episode_started_at"] if previous is not None else None
        episode_kind = previous["episode_kind"] if previous is not None else None
        episode_risk_line = previous["episode_risk_line"] if previous is not None else None

        if previous is None and breached:
            episode = 1
            episode_started_at = now
            episode_kind = (
                "existing_breach_discovered_after_import"
                if is_position_snapshot(position)
                else "below_line_at_first_observation"
            )
            episode_risk_line = line
            transition = episode_kind
        elif previous_breached and breached:
            transition = "persistent_breach"
        elif previous_breached and not breached:
            transition = "recovered_above_risk_line"
        elif breached:
            episode += 1
            episode_started_at = now
            episode_kind = "fresh_intraday_cross"
            episode_risk_line = line
            transition = episode_kind
        else:
            transition = "clear"

        conn.execute(
            """INSERT INTO position_risk_states
               (position_id, code, breached, episode, episode_started_at,
                episode_kind, episode_risk_line, current_risk_line, last_price,
                updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(position_id) DO UPDATE SET
                 code=excluded.code, breached=excluded.breached,
                 episode=excluded.episode,
                 episode_started_at=excluded.episode_started_at,
                 episode_kind=excluded.episode_kind,
                 episode_risk_line=excluded.episode_risk_line,
                 current_risk_line=excluded.current_risk_line,
                 last_price=excluded.last_price, updated_at=excluded.updated_at""",
            (
                position_id, code, int(breached), episode, episode_started_at,
                episode_kind, episode_risk_line, line, price, now,
            ),
        )
        conn.commit()
        return {
            "position_id": position_id,
            "code": code,
            "breached": breached,
            "transition": transition,
            "episode": episode,
            "episode_started_at": episode_started_at,
            "episode_kind": episode_kind,
            "episode_risk_line": episode_risk_line,
            "current_risk_line": line,
            "last_price": price,
            "observed_at": now,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_position_risk_state(position_id: int) -> dict | None:
    """Return the persisted crossing episode used to bind a risk alert."""
    if isinstance(position_id, bool) or not isinstance(position_id, int) or position_id <= 0:
        return None
    conn = _db()
    _db_init_alerts(conn)
    try:
        row = conn.execute(
            "SELECT * FROM position_risk_states WHERE position_id=?",
            (position_id,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _alert_provenance_scope(
    origin: str | None,
    strategy_id: str | None,
    evidence_ref_json: str | None,
    signal_id: str | None,
) -> tuple[str, tuple[str | None, ...]]:
    """Build a null-safe provenance filter; absent provenance keeps legacy scope."""
    values = (origin, strategy_id, evidence_ref_json, signal_id)
    if not any(value is not None for value in values):
        return "", ()
    return (
        " AND origin IS ? AND strategy_id IS ?"
        " AND evidence_ref_json IS ? AND signal_id IS ?",
        values,
    )


def _canonical_action_plan(action: str, value: object) -> str | None:
    """Validate and canonicalize the exact manual action shown by an alert."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("提醒行动计划必须为对象")
    trade_plan = value.get("trade_plan")
    allocation = value.get("allocation")
    if not isinstance(trade_plan, dict) or not isinstance(allocation, dict):
        raise ValueError("提醒行动计划缺少交易计划或仓位分配")
    plan_action = trade_plan.get("action")
    expected_side = "BUY" if action == "buy" else "SELL"
    if (
        not isinstance(plan_action, dict)
        or plan_action.get("side") != expected_side
        or plan_action.get("manual_confirmation") is not True
    ):
        raise ValueError("提醒行动计划与买卖方向不一致")
    shares = plan_action.get("shares")
    if isinstance(shares, bool) or not isinstance(shares, int) or shares <= 0:
        raise ValueError("提醒行动计划必须包含正整数数量")
    if expected_side == "BUY" and shares % 100:
        raise ValueError("买入提醒行动计划必须包含100股整数倍数量")
    if expected_side == "SELL":
        held = allocation.get("held_shares")
        target = plan_action.get("target_shares")
        if (
            isinstance(held, bool) or not isinstance(held, int) or held <= 0
            or isinstance(target, bool) or not isinstance(target, int)
            or target < 0 or target > held or shares != held - target
            or allocation.get("sell_shares") != shares
            or allocation.get("target_shares") != target
        ):
            raise ValueError("卖出提醒数量与冻结持仓或目标持仓不一致")
        odd_lot = held % 100
        if shares % 100 and (odd_lot == 0 or shares % 100 != odd_lot):
            raise ValueError("卖出提醒数量须为100股整数倍，或一次性包含全部零股")
    try:
        limit_price = float(plan_action.get("limit_price"))
        estimated_cost = float(plan_action.get("estimated_cost"))
    except (TypeError, ValueError):
        raise ValueError("提醒行动计划缺少限价或预计成本") from None
    if limit_price <= 0 or estimated_cost < 0:
        raise ValueError("提醒行动计划的限价或预计成本无效")
    if not plan_action.get("valid_until") or not plan_action.get("cancel_condition"):
        raise ValueError("提醒行动计划缺少有效期或失效条件")
    if not plan_action.get("evidence_identity"):
        raise ValueError("提醒行动计划缺少证据身份")
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _present_alert_row(row: sqlite3.Row | dict) -> dict:
    item = dict(row)
    raw = item.pop("action_plan_json", None)
    if raw:
        try:
            parsed = json.loads(str(raw))
        except json.JSONDecodeError:
            parsed = None
        item["action_plan"] = parsed if isinstance(parsed, dict) else None
    else:
        item["action_plan"] = None
    return item


def _record_alert_decision(
    conn: sqlite3.Connection, alert_id: int, decision: str, reason: str,
    idempotency_key: str, *, position_id: int | None = None,
    sell_event_id: int | None = None,
) -> dict:
    """Transition one exact alert and append its immutable decision event."""
    if isinstance(alert_id, bool) or not isinstance(alert_id, int) or alert_id <= 0:
        raise ValueError("提醒标识无效")
    if decision not in {"dismissed", "confirmed"}:
        raise ValueError("提醒决策无效")
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 256:
        raise ValueError("提醒决策理由无效")
    if (
        not isinstance(idempotency_key, str) or not idempotency_key.strip()
        or len(idempotency_key.strip()) > 256
    ):
        raise ValueError("提醒决策缺少有效幂等键")
    for value, label in (
        (position_id, "持仓标识"), (sell_event_id, "卖出事件标识"),
    ):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise ValueError(f"{label}无效")
    if decision == "dismissed" and (position_id is not None or sell_event_id is not None):
        raise ValueError("忽略提醒不得绑定成交")
    if decision == "confirmed" and position_id is None:
        raise ValueError("确认提醒必须绑定持仓记录")

    reason = reason.strip()
    idempotency_key = idempotency_key.strip()
    expected = (alert_id, decision, reason, position_id, sell_event_id)
    by_key = conn.execute(
        "SELECT * FROM timing_alert_decisions WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if by_key is not None:
        actual = tuple(by_key[key] for key in (
            "alert_id", "decision", "reason", "position_id", "sell_event_id",
        ))
        if actual != expected:
            raise IdempotencyConflictError("提醒决策幂等键已用于另一操作")
        return {**dict(by_key), "idempotent_replay": True}

    existing = conn.execute(
        "SELECT * FROM timing_alert_decisions WHERE alert_id=?", (alert_id,),
    ).fetchone()
    if existing is not None:
        actual = tuple(existing[key] for key in (
            "alert_id", "decision", "reason", "position_id", "sell_event_id",
        ))
        if actual != expected:
            raise IdempotencyConflictError("该精确提醒已记录另一决策")
        return {**dict(existing), "idempotent_replay": True}

    alert = conn.execute(
        "SELECT * FROM timing_alerts WHERE id=?", (alert_id,),
    ).fetchone()
    if alert is None:
        raise ValueError("提醒不存在")
    presented = _expire_alert_row(conn, alert, _now_sh())
    current_status = str(presented.get("status") or "")
    if current_status != "triggered":
        raise ValueError("提醒已失效或已处理")
    if decision == "confirmed":
        from .manual_fill_plan import capture_confirmation
        capture_confirmation(conn, dict(alert), position_id, sell_event_id)
    updated = conn.execute(
        "UPDATE timing_alerts SET status=?, resolved_at=? "
        "WHERE id=? AND status='triggered'",
        (decision, _now_sh().isoformat(), alert_id),
    )
    if updated.rowcount != 1:
        raise ValueError("提醒状态已并发变化")
    cur = conn.execute(
        """INSERT INTO timing_alert_decisions
           (alert_id, decision, reason, position_id, sell_event_id,
            decided_at, idempotency_key)
           VALUES (?,?,?,?,?,?,?)""",
        (
            alert_id, decision, reason, position_id, sell_event_id,
            _now_sh().isoformat(), idempotency_key,
        ),
    )
    row = conn.execute(
        "SELECT * FROM timing_alert_decisions WHERE id=?", (cur.lastrowid,),
    ).fetchone()
    return dict(row)


def dismiss_alert_exact(
    alert_id: int, reason: str, idempotency_key: str,
) -> dict:
    """Dismiss exactly one alert; replaying the same decision is harmless."""
    conn = _db()
    try:
        _db_init_alerts(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        event = _record_alert_decision(
            conn, alert_id, "dismissed", reason, idempotency_key,
        )
        conn.commit()
        return event
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_alert_decisions(alert_id: int | None = None, limit: int = 100) -> list[dict]:
    conn = _db()
    try:
        _db_init_alerts(conn)
        if alert_id is None:
            rows = conn.execute(
                "SELECT * FROM timing_alert_decisions ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM timing_alert_decisions WHERE alert_id=? ORDER BY id",
                (int(alert_id),),
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def record_alert(
    code: str, name: str, action: str, window_label: str,
    price: float | None, score: float, level: str, reason: str,
    *, origin: str | None = None, strategy_id: str | None = None,
    evidence_ref_json: str | None = None, signal_id: str | None = None,
    valid_until: str | None = None,
    action_plan: dict | None = None,
) -> dict:
    """登记一次触发；同一 provenance 先置为 superseded，未传 provenance 保持旧范围。"""
    conn = _db()
    _db_init_alerts(conn)
    now = _now_sh().isoformat()
    scope, provenance_values = _alert_provenance_scope(
        origin, strategy_id, evidence_ref_json, signal_id,
    )
    action_plan_json = _canonical_action_plan(action, action_plan)
    conn.execute(
        f"""UPDATE timing_alerts SET status='superseded', resolved_at=?
           WHERE code=? AND action=?{scope} AND status='triggered'""",
        (now, code, action, *provenance_values),
    )
    cur = conn.execute(
        """INSERT INTO timing_alerts
           (code, name, action, window_label, price, score, level, reason,
            origin, strategy_id, evidence_ref_json, signal_id, valid_until,
            action_plan_json, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (code, name, action, window_label, price, score, level, reason,
         origin, strategy_id, evidence_ref_json, signal_id, valid_until,
         action_plan_json, "triggered", now, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM timing_alerts WHERE id=?", (cur.lastrowid,)
    ).fetchone()
    conn.close()
    return _present_alert_row(row)


def _alert_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.replace(tzinfo=_SH_TZ) if parsed.tzinfo is None else parsed


def _expire_alert_row(
    conn: sqlite3.Connection, row: sqlite3.Row | dict, now: datetime,
) -> dict:
    item = _present_alert_row(row)
    deadline = _alert_timestamp(item.get("valid_until"))
    if item.get("status") == "triggered" and deadline is not None and deadline <= now:
        resolved_at = deadline.isoformat()
        conn.execute(
            "UPDATE timing_alerts SET status='expired', resolved_at=? WHERE id=? AND status='triggered'",
            (resolved_at, item["id"]),
        )
        item.update({"status": "expired", "resolved_at": resolved_at})
    elif item.get("status") == "triggered" and deadline is None:
        created = _alert_timestamp(item.get("created_at"))
        if created is not None and created.astimezone(_SH_TZ).date() < now.date():
            # Legacy rows predate the valid_until schema.  Preserve their stored
            # history, but never present a prior-day, un-ticketable reminder as
            # currently actionable or unresolved.
            item.update({
                "status": "expired",
                "status_reason": "legacy_missing_valid_until",
            })
    return item


def try_record_alert(
    code: str, name: str, action: str, window_label: str,
    price: float | None, score: float, level: str, reason: str,
    *, cooldown_minutes: int = 15,
    daily_buy_limit: int | None = MAX_DAILY_BUY_ALERTS,
    daily_sell_limit: int | None = None,
    origin: str | None = None,
    strategy_id: str | None = None,
    evidence_ref_json: str | None = None,
    signal_id: str | None = None,
    valid_until: str | None = None,
    action_plan: dict | None = None,
) -> dict:
    """Atomically check cooldown/quota and persist one provenance alert."""
    conn = _db()
    _db_init_alerts(conn)
    conn.commit()
    now_dt = _now_sh()
    now = now_dt.isoformat()
    deadline = _alert_timestamp(valid_until)
    if valid_until is not None and deadline is None:
        conn.close()
        raise ValueError("提醒有效期格式无效")
    valid_until = deadline.isoformat() if deadline is not None else None
    action_plan_json = _canonical_action_plan(action, action_plan)
    if deadline is not None and deadline <= now_dt:
        conn.close()
        return {"status": "expired", "recorded": False, "alert": None}
    scope, provenance_values = _alert_provenance_scope(
        origin, strategy_id, evidence_ref_json, signal_id,
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        latest = conn.execute(
            f"""SELECT * FROM timing_alerts WHERE code=? AND action=?{scope}
               ORDER BY id DESC LIMIT 1""",
            (code, action, *provenance_values),
        ).fetchone()
        if latest:
            latest = _expire_alert_row(conn, latest, now_dt)
            status = str(latest["status"])
            reference = _alert_timestamp(
                latest["created_at"] if status == "triggered" else latest["resolved_at"]
            )
            same_day = bool(
                reference
                and reference.astimezone(_SH_TZ).date() == now_dt.date()
            )
            if status == "confirmed" and same_day:
                conn.rollback()
                return {"status": "confirmed", "recorded": False, "alert": dict(latest)}
            if status == "triggered" and same_day:
                if action == "sell" and origin == "position_risk_review":
                    conn.execute(
                        """UPDATE timing_alerts
                           SET window_label=?, price=?, score=?, level=?, reason=?,
                               valid_until=?, action_plan_json=?, updated_at=?
                           WHERE id=? AND status='triggered'""",
                        (
                            window_label, price, score, level, reason,
                            valid_until, action_plan_json, now, latest["id"],
                        ),
                    )
                    conn.commit()
                    refreshed = conn.execute(
                        "SELECT * FROM timing_alerts WHERE id=?", (latest["id"],),
                    ).fetchone()
                    return {
                        "status": "existing_triggered", "recorded": False,
                        "alert": _present_alert_row(refreshed),
                    }
                conn.rollback()
                return {"status": "existing_triggered", "recorded": False, "alert": latest}
            if (
                status == "expired" and same_day
                and action == "sell" and origin == "position_risk_review"
            ):
                # Expiry is a stale-price boundary, not a human resolution.
                # A new live observation may refresh the same risk episode in place.
                conn.execute(
                    """UPDATE timing_alerts
                       SET status='triggered', resolved_at=NULL, window_label=?,
                           price=?, score=?, level=?, reason=?, valid_until=?,
                           action_plan_json=?, updated_at=?
                       WHERE id=? AND status='expired'""",
                    (
                        window_label, price, score, level, reason,
                        valid_until, action_plan_json, now, latest["id"],
                    ),
                )
                conn.commit()
                refreshed = conn.execute(
                    "SELECT * FROM timing_alerts WHERE id=?", (latest["id"],),
                ).fetchone()
                return {
                    "status": "existing_triggered", "recorded": False,
                    "alert": _present_alert_row(refreshed),
                }
            if status == "expired" and same_day:
                conn.commit()
                return {"status": "expired", "recorded": False, "alert": latest}
            if (
                status == "dismissed" and same_day
                and action == "sell" and origin == "position_risk_review"
            ):
                conn.rollback()
                return {"status": "dismissed", "recorded": False, "alert": dict(latest)}
            in_cooldown = bool(
                reference
                and (now_dt - reference).total_seconds() < cooldown_minutes * 60
            )
            if status in {"dismissed", "superseded", "expired"} and in_cooldown:
                conn.rollback()
                return {"status": "cooldown", "recorded": False, "alert": dict(latest)}

        if action == "buy":
            today = now_dt.strftime("%Y-%m-%d")
            used = int(conn.execute(
                """SELECT COUNT(*) FROM timing_alerts
                   WHERE action='buy' AND substr(created_at,1,10)=?""",
                (today,),
            ).fetchone()[0])
            if daily_buy_limit is not None and used >= daily_buy_limit:
                conn.rollback()
                return {"status": "daily_limit", "recorded": False, "used": used}
        elif action == "sell":
            today = now_dt.strftime("%Y-%m-%d")
            used = int(conn.execute(
                f"""SELECT COUNT(*) FROM timing_alerts
                    WHERE code=? AND action='sell'{scope}
                      AND substr(created_at,1,10)=?""",
                (code, *provenance_values, today),
            ).fetchone()[0])
            if daily_sell_limit is not None and used >= daily_sell_limit:
                conn.rollback()
                return {"status": "daily_limit", "recorded": False, "used": used}

        conn.execute(
            f"""UPDATE timing_alerts SET status='superseded', resolved_at=?
               WHERE code=? AND action=?{scope} AND status='triggered'""",
            (now, code, action, *provenance_values),
        )
        cur = conn.execute(
            """INSERT INTO timing_alerts
               (code, name, action, window_label, price, score, level, reason,
                origin, strategy_id, evidence_ref_json, signal_id, valid_until,
                action_plan_json, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                code, name, action, window_label, price, score, level, reason,
                origin, strategy_id, evidence_ref_json, signal_id, valid_until,
                action_plan_json, "triggered", now, now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM timing_alerts WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return {
            "status": "recorded", "recorded": True,
            "alert": _present_alert_row(row),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def has_unresolved_alert(
    code: str, action: str, *, origin: str | None = None,
    strategy_id: str | None = None, evidence_ref_json: str | None = None,
    signal_id: str | None = None,
) -> bool:
    """是否存在未处理的 triggered 提醒（sticky：触发后持续返回 triggered，
    直到用户确认执行 / 忽略，避免前端错过单次轮询就永久丢失红屏）。"""
    conn = _db()
    _db_init_alerts(conn)
    scope, provenance_values = _alert_provenance_scope(
        origin, strategy_id, evidence_ref_json, signal_id,
    )
    row = conn.execute(
        f"""SELECT * FROM timing_alerts WHERE code=? AND action=?{scope} AND status='triggered'
           ORDER BY id DESC LIMIT 1""",
        (code, action, *provenance_values),
    ).fetchone()
    if row is not None:
        row = _expire_alert_row(conn, row, _now_sh())
        conn.commit()
    conn.close()
    return row is not None and row["status"] == "triggered"


def should_fire_alert(
    code: str, action: str, cooldown_minutes: int = 15,
    *, origin: str | None = None, strategy_id: str | None = None,
    evidence_ref_json: str | None = None, signal_id: str | None = None,
) -> bool:
    """决定是否应该触发一次新提醒。

    - 同日已有 triggered：保持原提醒，不重复落库；跨日重置为新提醒
    - 同日已 confirmed → False；下一交易日出现新条件时可再次提醒
    - 已 dismissed → 冷却期内不重复打扰，冷却后允许再次提醒
    """
    conn = _db()
    _db_init_alerts(conn)
    scope, provenance_values = _alert_provenance_scope(
        origin, strategy_id, evidence_ref_json, signal_id,
    )
    row = conn.execute(
        f"""SELECT * FROM timing_alerts WHERE code=? AND action=?{scope}
           ORDER BY id DESC LIMIT 1""",
        (code, action, *provenance_values),
    ).fetchone()
    if not row:
        conn.close()
        return True
    row = _expire_alert_row(conn, row, _now_sh())
    conn.commit()
    now = _now_sh()
    if row["status"] in {"triggered", "confirmed", "expired"}:
        reference = _alert_timestamp(
            row["created_at"] if row["status"] == "triggered" else row["resolved_at"]
        )
        same_day = bool(
            reference and reference.astimezone(_SH_TZ).date() == now.date()
        )
        conn.close()
        return not same_day
    # A position-risk signal_id identifies one continuous breach episode.
    # Dismissing that exact episode suppresses it until the price recovers and
    # a later crossing receives a new episode identity.
    if row["status"] == "dismissed" and origin == "position_risk_review":
        reference = _alert_timestamp(row["resolved_at"])
        same_day = bool(
            reference and reference.astimezone(_SH_TZ).date() == now.date()
        )
        if same_day:
            conn.close()
            return False
    # dismissed / superseded → 冷却期
    resolved = None
    if row["resolved_at"]:
        try:
            resolved = datetime.fromisoformat(row["resolved_at"])
            if resolved.tzinfo is None:
                resolved = resolved.replace(tzinfo=_SH_TZ)
        except ValueError:
            resolved = None
    if resolved and (now - resolved).total_seconds() < cooldown_minutes * 60:
        conn.close()
        return False
    conn.close()
    return True


def count_today_buy_alerts(limit: int | None = MAX_DAILY_BUY_ALERTS) -> int:
    """Return today's persisted buy-alert count for monitoring only."""
    conn = _db()
    _db_init_alerts(conn)
    today = _now_sh().strftime("%Y-%m-%d")
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM timing_alerts
           WHERE action='buy' AND substr(created_at,1,10)=?""",
        (today,),
    ).fetchone()
    conn.close()
    return int(row["n"] if row else 0)


def dismiss_alert(
    code: str, action: str, *, origin: str | None = None,
    strategy_id: str | None = None, evidence_ref_json: str | None = None,
    signal_id: str | None = None,
) -> bool:
    """用户点击"忽略"：把进行中的提醒置为 dismissed（进入冷却期）。"""
    conn = _db()
    _db_init_alerts(conn)
    now = _now_sh().isoformat()
    scope, provenance_values = _alert_provenance_scope(
        origin, strategy_id, evidence_ref_json, signal_id,
    )
    cur = conn.execute(
        f"""UPDATE timing_alerts SET status='dismissed', resolved_at=?
           WHERE code=? AND action=?{scope} AND status='triggered'""",
        (now, code, action, *provenance_values),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def resolve_alert(
    code: str, action: str, *, origin: str | None = None,
    strategy_id: str | None = None, evidence_ref_json: str | None = None,
    signal_id: str | None = None, legacy_only: bool = False,
) -> bool:
    """确认买卖执行后把提醒置为 confirmed（不再提醒该动作）。"""
    conn = _db()
    _db_init_alerts(conn)
    now = _now_sh().isoformat()
    if legacy_only:
        scope, provenance_values = (
            " AND origin IS NULL AND strategy_id IS NULL"
            " AND evidence_ref_json IS NULL AND signal_id IS NULL", ()
        )
    else:
        scope, provenance_values = _alert_provenance_scope(
            origin, strategy_id, evidence_ref_json, signal_id,
        )
    cur = conn.execute(
        f"""UPDATE timing_alerts SET status='confirmed', resolved_at=?
           WHERE code=? AND action=?{scope} AND status='triggered'""",
        (now, code, action, *provenance_values),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def freeze_alert_action_plan(alert_id: int, action_plan: dict) -> dict:
    """Bind the first complete sizing plan to a live alert without overwriting it."""
    conn = _db()
    _db_init_alerts(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM timing_alerts WHERE id=?", (int(alert_id),),
        ).fetchone()
        if row is None:
            raise ValueError("提醒不存在")
        item = _expire_alert_row(conn, row, _now_sh())
        if item.get("status") != "triggered":
            raise ValueError("提醒已失效，不能绑定行动计划")
        canonical = _canonical_action_plan(str(item.get("action")), action_plan)
        stored = row["action_plan_json"]
        if stored is not None and stored != canonical:
            raise ValueError("提醒已绑定另一份行动计划")
        if stored is None:
            conn.execute(
                "UPDATE timing_alerts SET action_plan_json=? "
                "WHERE id=? AND status='triggered' AND action_plan_json IS NULL",
                (canonical, int(alert_id)),
            )
        conn.commit()
        current = conn.execute(
            "SELECT * FROM timing_alerts WHERE id=?", (int(alert_id),),
        ).fetchone()
        return _present_alert_row(current)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_alert(alert_id: int) -> dict | None:
    """Read one alert for server-side ticket binding; callers validate its scope."""
    conn = _db()
    _db_init_alerts(conn)
    try:
        row = conn.execute("SELECT * FROM timing_alerts WHERE id=?", (alert_id,)).fetchone()
        if row is None:
            return None
        item = _expire_alert_row(conn, row, _now_sh())
        conn.commit()
        return item
    finally:
        conn.close()


def list_alerts(limit: int = 100, *, recorded_day: str | None = None) -> list[dict]:
    conn = _db()
    _db_init_alerts(conn)
    rows = conn.execute(
        "SELECT * FROM timing_alerts"
        + (" WHERE substr(created_at,1,10)=?" if recorded_day else "")
        + " ORDER BY id DESC LIMIT ?", ((recorded_day, limit) if recorded_day else (limit,))
    ).fetchall()
    rows = [_expire_alert_row(conn, row, _now_sh()) for row in rows]
    conn.commit()
    conn.close()
    return rows


def list_history(limit: int = 100) -> dict:
    """今日机会提醒历史 + 时机持仓（开/平）。"""
    from .manual_fill_fees import from_snapshot
    snapshot = read_account_ledger_facts()
    rows = from_snapshot(snapshot["open_rows"] + snapshot["closed_rows"], snapshot)
    closed = sorted((p for p in rows if p.get("status") == "closed"), key=lambda p: p["id"], reverse=True)[:limit]
    opened = sorted((p for p in rows if p.get("status") == "open"), key=lambda p: p["id"], reverse=True)
    alerts = list_alerts(limit)
    decisions = list_alert_decisions(limit=limit)
    return {
        "alerts": alerts,
        "alert_decisions": decisions,
        "positions_closed": closed,
        "positions_open": opened,
        "execution_performance": execution_performance(closed),
        "fee_basis": "latest_user_fee_corrections_original_fills_preserved",
        "count": {"alerts": len(alerts), "alert_decisions": len(decisions),
                  "open": len(opened), "closed": len(closed)},
    }


def execution_performance(positions: list[dict] | None = None) -> dict:
    """Describe completed manual trades using net, cost-inclusive outcomes."""
    rows = list_positions("closed") if positions is None else [
        row for row in positions if row.get("status") == "closed"
    ]

    def summarize(group: list[dict]) -> dict:
        recorded = [float(row.get("pnl") or 0.0) for row in group]
        complete_rows = [
            row for row in group
            if row.get("buy_cost") is not None
            and not row.get("fee_correction_error")
            and row.get("sell_cost") is not None
            and row.get("realized_pnl_gross") is not None
        ]
        cost_complete = len(complete_rows) == len(group)
        actual_fee_rows = [
            row for row in complete_rows
            if row.get("buy_cost_source") == "actual_user_recorded"
            and row.get("sell_cost_source") == "actual_user_recorded"
        ]
        complete_slippage_rows = []
        partial_slippage_rows = []
        attributed_slippage = 0.0
        attributed_components = 0
        for row in group:
            entry_available = row.get("entry_slippage_yuan") is not None
            sell_events = row.get("sell_events") or []
            fill_events = int(
                row.get("sell_fill_events")
                if row.get("sell_fill_events") is not None else len(sell_events)
            )
            attributed_events = int(
                row.get("sell_slippage_attributed_events")
                if row.get("sell_slippage_attributed_events") is not None
                else sum(event.get("slippage_yuan") is not None for event in sell_events)
            )
            sell_complete = bool(
                fill_events > 0 and attributed_events == fill_events
                and row.get("sell_slippage_yuan") is not None
            )
            if entry_available:
                attributed_slippage += float(row["entry_slippage_yuan"])
                attributed_components += 1
            if row.get("sell_slippage_yuan") is not None:
                attributed_slippage += float(row["sell_slippage_yuan"])
                attributed_components += 1
            if entry_available and sell_complete:
                complete_slippage_rows.append(row)
            elif entry_available or attributed_events > 0:
                partial_slippage_rows.append(row)
        positive = sum(value for value in recorded if value > 0)
        negative = -sum(value for value in recorded if value < 0)
        returns, hold_days = [], []
        for row in group:
            initial_shares = int(row.get("initial_shares") or 0)
            basis = float(row.get("price") or 0.0) * initial_shares
            basis += float(row.get("buy_cost") or 0.0)
            if basis > 0 and row in complete_rows:
                returns.append(float(row.get("pnl") or 0.0) / basis * 100.0)
            if not is_position_snapshot(row):
                try:
                    opened = datetime.fromisoformat(str(row.get("created_at")))
                    closed = datetime.fromisoformat(str(row.get("sold_at")))
                    hold_days.append(max(0, (closed - opened).days))
                except (TypeError, ValueError):
                    pass
        count = len(group)
        return {
            "completed_positions": count,
            "wins": sum(value > 0 for value in recorded),
            "losses": sum(value < 0 for value in recorded),
            "flat": sum(value == 0 for value in recorded),
            "recorded_pnl_yuan": round(sum(recorded), 2),
            "net_pnl_yuan": round(sum(recorded), 2) if cost_complete else None,
            "gross_pnl_yuan": round(sum(float(row["realized_pnl_gross"]) for row in group), 2) if cost_complete else None,
            "total_cost_yuan": round(sum(
                float(row.get("buy_cost") or 0.0) + float(row.get("sell_cost") or 0.0)
                for row in group
            ), 2) if cost_complete else None,
            "cost_breakdown_status": (
                "complete" if cost_complete else
                "legacy_missing" if not complete_rows else "partial"
            ),
            "cost_breakdown_complete_positions": len(complete_rows),
            "cost_breakdown_missing_positions": count - len(complete_rows),
            "actual_fee_positions": len(actual_fee_rows),
            "fee_source_status": (
                "unavailable" if not complete_rows else
                "actual" if len(actual_fee_rows) == count else
                "estimated" if not actual_fee_rows and cost_complete else
                "mixed"
            ),
            "slippage_attribution_status": (
                "complete" if count and len(complete_slippage_rows) == count else
                "partial" if complete_slippage_rows or partial_slippage_rows else
                "unavailable"
            ),
            "slippage_attribution_complete_positions": len(complete_slippage_rows),
            "slippage_attribution_partial_positions": len(partial_slippage_rows),
            "slippage_attribution_missing_positions": (
                count - len(complete_slippage_rows) - len(partial_slippage_rows)
            ),
            "attributed_adverse_slippage_yuan": (
                round(attributed_slippage, 2) if attributed_components else None
            ),
            "total_adverse_slippage_yuan": (
                round(sum(
                    float(row["entry_slippage_yuan"])
                    + float(row["sell_slippage_yuan"])
                    for row in group
                ), 2)
                if count and len(complete_slippage_rows) == count else None
            ),
            "win_rate_pct": round(sum(value > 0 for value in recorded) / count * 100, 2) if count and cost_complete else None,
            "recorded_win_rate_pct": round(sum(value > 0 for value in recorded) / count * 100, 2) if count else None,
            "average_net_pnl_yuan": round(sum(recorded) / count, 2) if count and cost_complete else None,
            "average_recorded_pnl_yuan": round(sum(recorded) / count, 2) if count else None,
            "average_net_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
            "profit_factor": round(positive / negative, 3) if cost_complete and negative > 0 else None,
            "average_holding_calendar_days": round(sum(hold_days) / len(hold_days), 2) if hold_days else None,
            "holding_period_known_positions": len(hold_days),
            "holding_period_missing_positions": count - len(hold_days),
        }

    summary = summarize(rows)
    windows = {}
    for row in rows:
        windows.setdefault(str(row.get("window_label") or "未标注"), []).append(row)
    summary["by_entry_window"] = [
        {"window_label": label, **summarize(group)}
        for label, group in sorted(windows.items())
    ]
    origins = {}
    strategies = {}
    for row in rows:
        origin = str(row.get("entry_origin") or "legacy_unattributed")
        origins.setdefault(origin, []).append(row)
        if origin != "production_strategy" or not row.get("entry_strategy_id"):
            continue
        try:
            evidence = json.loads(str(row.get("entry_evidence_ref_json") or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        strategy_id = str(row["entry_strategy_id"])
        if not isinstance(evidence, dict) or evidence.get("strategy_id") != strategy_id:
            continue
        validation = evidence.get("validation_evidence")
        if not isinstance(validation, dict):
            continue
        if evidence.get("schema") == "strict_route_production_strategy_v1":
            refs = {
                key: evidence.get(key) for key in (
                    "cost_after_validation_ref", "out_of_sample_validation_ref",
                    "production_admission_ref",
                )
            }
            if not (
                evidence.get("strategy_kind") == "state_routed_playbook"
                and evidence.get("strategy_version")
                and all(
                    isinstance(ref, dict)
                    and ref.get("path")
                    and len(str(ref.get("sha256") or "")) == 64
                    and all(
                        char in "0123456789abcdef"
                        for char in str(ref.get("sha256") or "").lower()
                    )
                    for ref in refs.values()
                )
                and validation == refs["production_admission_ref"]
            ):
                continue
            identity = {
                "strategy_id": strategy_id,
                "strategy_kind": "state_routed_playbook",
                "strategy_version": str(evidence["strategy_version"]),
                **refs,
            }
            display_identity = {
                "strategy_id": strategy_id,
                "strategy_kind": "state_routed_playbook",
                "strategy_version": str(evidence["strategy_version"]),
                "cost_after_validation_sha256": str(
                    refs["cost_after_validation_ref"]["sha256"]
                ),
                "out_of_sample_validation_sha256": str(
                    refs["out_of_sample_validation_ref"]["sha256"]
                ),
                "production_admission_sha256": str(
                    refs["production_admission_ref"]["sha256"]
                ),
            }
            factor_as_of = None
        else:
            required = (
                "strategy_spec_sha256", "catalog_version", "runtime_data_key",
                "factor_as_of",
            )
            if not all(evidence.get(key) for key in required):
                continue
            if not validation.get("path") or not validation.get("sha256"):
                continue
            identity = {
                "strategy_id": strategy_id,
                "strategy_spec_sha256": str(evidence["strategy_spec_sha256"]),
                "catalog_version": str(evidence["catalog_version"]),
                "runtime_data_key": str(evidence["runtime_data_key"]),
                "validation_evidence": {
                    "path": str(validation["path"]),
                    "sha256": str(validation["sha256"]),
                },
            }
            display_identity = {
                key: value for key, value in identity.items()
                if key != "validation_evidence"
            }
            factor_as_of = str(evidence["factor_as_of"])
        strategy_key = json.dumps(
            identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        bucket = strategies.setdefault(strategy_key, {
            "rows": [], "factor_as_of": set(),
            "display_identity": display_identity,
            "validation_evidence_sha256": str(validation["sha256"]),
        })
        bucket["rows"].append(row)
        if factor_as_of is not None:
            bucket["factor_as_of"].add(factor_as_of)
    summary["by_entry_origin"] = [
        {"entry_origin": origin, **summarize(group)}
        for origin, group in sorted(origins.items())
    ]
    strategy_summaries = []
    for version_json, bucket in sorted(strategies.items()):
        factor_as_of_values = sorted(bucket["factor_as_of"])
        strategy_summaries.append({
            **bucket["display_identity"],
            "factor_as_of_values": factor_as_of_values,
            "validation_evidence_sha256": bucket[
                "validation_evidence_sha256"
            ],
            "evidence_ref_sha256": hashlib.sha256(version_json.encode("utf-8")).hexdigest(),
            **summarize(bucket["rows"]),
        })
    summary["by_strategy"] = strategy_summaries
    summary["sample_status"] = (
        "no_completed_trades" if not rows
        else "insufficient_sample" if len(rows) < 10
        else "descriptive_sample"
    )
    summary["interpretation"] = (
        "仅有完整 production_strategy 身份的分组可归因到策略；"
        "手工或研究观察账本不构成策略验证，也不自动改变信号或仓位；"
        "旧成交缺少费用拆分时仅展示记录盈亏，不冒充成本后净绩效"
        "；执行滑点是实际成交相对提醒参考价的归因，不会从净收益中重复扣除"
    )
    return summary


# ── AI 看涨度 Top-N（今日机会推荐源）──────────

def _kronos_model_items() -> tuple[str, list[dict], str | None]:
    """Read raw model outputs and add a same-batch percentile for aggregation.

    `pred_ret_*` remains the model's unmodified original output.  The separate
    percentile is only a bounded cross-sectional score for mixing with timing
    and company review; it never replaces or clips the displayed raw value.

    P4-2d：缺失/解析失败不再静默返回空——第三元素携带失败原因，供页面显示健康度。
    """
    kp = _KRONOS_SNAPSHOT_PATH
    if not kp.exists():
        return "", [], (
            "Kronos 模型快照文件缺失（research/kronos_stock_snapshot.json 不存在）；"
            "AI 看涨度候选不可用，不会用旧数据或空值充数。"
        )
    try:
        data = json.loads(kp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return "", [], (
            f"Kronos 模型快照解析失败（{type(exc).__name__}）；"
            "AI 看涨度候选不可用。"
        )
    snapshot_date = str(data.get("snapshot_date") or data.get("data_asof") or "")
    horizon_semantics = str(data.get("horizon_semantics") or "legacy_calendar_days")
    calendar_evidence = (
        data.get("forecast_calendar")
        if isinstance(data.get("forecast_calendar"), dict) else None
    )
    freshness = audited_trading_day_age(snapshot_date) if snapshot_date else {
        "status": "unknown", "days": None, "reason": "模型输入日期缺失",
    }
    rows = []
    for code, info in (data.get("stocks") or {}).items():
        if not isinstance(info, dict):
            continue
        try:
            raw20 = float(info.get("pred_ret_20d")) * 100
        except (TypeError, ValueError):
            continue
        def raw_return(key):
            try:
                value = info.get(key)
                return round(float(value) * 100, 2) if value is not None else None
            except (TypeError, ValueError):
                return None
        rows.append({
            "code": str(code),
            "pred_ret_5d": raw_return("pred_ret_5d"),
            "pred_ret_10d": raw_return("pred_ret_10d"),
            "pred_ret_20d": round(raw20, 2),
            "pred_ret_30d": raw_return("pred_ret_30d"),
            "_raw20": raw20,
            "source_data_as_of": info.get("input_as_of") or snapshot_date or None,
            "horizon_semantics": horizon_semantics,
            "forecast_calendar": calendar_evidence,
            **{
                f"pred_date_{horizon}d": info.get(f"pred_date_{horizon}d")
                for horizon in (5, 10, 20, 30)
                if info.get(f"pred_date_{horizon}d")
            },
            "data_staleness_days": freshness.get("days"),
            "freshness_status": freshness.get("status"),
            "decision_eligible": False,
            "research_only": True,
            "validation_status": "unvalidated_model_output",
        })
    rows.sort(key=lambda item: item["_raw20"])
    denominator = max(1, len(rows) - 1)
    for index, item in enumerate(rows):
        item["model_score"] = round(index / denominator * 100, 1)
    rows.sort(key=lambda item: item["_raw20"], reverse=True)
    if horizon_semantics != "audited_a_share_trading_sessions":
        reason = (
            "当前 Kronos 快照仍是旧自然日预测期限，周末/节假日会扭曲5/10/20/30日语义；"
            "仅保留原始研究证据，不参与收益排序或买入判断。"
        )
    else:
        reason = (
            "Kronos 预测期限已绑定审计A股交易日历；输出仍须积累成本后前向验证，"
            "当前不参与买入通过/否决。"
        )
    return str(data.get("generated_at") or ""), rows, reason


def kronos_status() -> dict:
    """Kronos 模型快照健康度（含失败原因），供扫描/看板接口带出而非静默消失。"""
    generated_at, rows, reason = _kronos_model_items()
    sample = rows[0] if rows else {}
    return {
        "available": bool(rows),
        "generated_at": generated_at or None,
        "source_data_as_of": sample.get("source_data_as_of"),
        "data_staleness_days": sample.get("data_staleness_days"),
        "freshness_status": sample.get("freshness_status") or "unknown",
        "decision_eligible": False,
        "research_only": True,
        "validation_status": "unvalidated_model_output",
        "horizon_semantics": sample.get("horizon_semantics"),
        "forecast_calendar": sample.get("forecast_calendar"),
        "count": len(rows),
        "reason": reason,
    }


def model_research_score(code: str) -> dict | None:
    """Return raw model output plus its same-batch 0–100 ordering score."""
    generated_at, rows, _reason = _kronos_model_items()
    for item in rows:
        if item["code"] == code:
            return {**item, "generated_at": generated_at}
    return None


def load_kronos_top(limit: int = 12) -> list[dict]:
    """Return raw-output model candidates; no prediction value is clipped."""
    from .signal_engine import _is_blacklisted

    items = []
    _generated_at, raw_items, _reason = _kronos_model_items()
    for item in raw_items:
        code = item["code"]
        if _is_blacklisted(code, code):
            continue
        items.append(dict(item))
    for it in items:
        it.pop("_raw20", None)
    return items[:limit]
