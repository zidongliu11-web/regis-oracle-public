#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
portfolio.py — 组合管理与回测主循环

策略口径（任务书 3.4/3.6 的实现，README 有完整解释）：
  - 等权多因子：单股配置预算 = 初始资金 × 1%（100 只各 1% 的含意 = 入选时每只按 1% 建仓）
  - 买入：T 日综合得分 > +1.5（≈横截面 top 10%档），T+1 日收盘成交
  - 卖出：综合得分 < -1.5，或止损 -5% / 止盈 +15%（按日收盘价判断，T+1 成交）
  - 单股最大仓位 5%：仓位超限时超额部分减回等权
  - 调仓：默认周频（每 5 个交易日评估买入）；止损止盈每日检查
  - 停牌股不可成交；涨停不可买、跌停不可卖；缺失数据跳过（NaN 不补全）

时间线（日线 PIT）：T 日收盘生成信号 → T+1 日收盘以 (收盘价±滑点) 成交 → 同日结算。
"""
from __future__ import annotations
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from . import executor as ex


class Portfolio:
    def _cost_args(self) -> dict:
        """config 键 → executor.compute_costs 参数名映射（JSON: stamp_duty/transfer_fee）。"""
        bt = self.cfg['backtest']
        return {'commission': bt['commission'], 'stamp': bt['stamp_duty'],
                'transfer': bt['transfer_fee'], 'min_comm': bt['min_commission']}

    def __init__(self, capital: float, cfg: dict,
                 prices: pd.DataFrame,        # adj_close (date×code)
                 pct_chg: pd.DataFrame,
                 is_st: pd.DataFrame,
                 trade_status: pd.DataFrame,
                 daily_amount: pd.DataFrame,  # 当日成交额（流动性约束）
                 composite: pd.DataFrame,
                 codes: List[str],
                 dates: List[pd.Timestamp],
                 rebal_period: int = 5,
                 risk_mult: Optional[pd.DataFrame] = None,
                 full_exp: Optional[dict] = None,
                 vol_scale: Optional[pd.Series] = None,
                 market_allow: Optional[pd.Series] = None,
                 atr_pct: Optional[pd.DataFrame] = None,
                 drawdown_stop: Optional[float] = None):
        self.capital = capital
        self.cfg = cfg
        self.prices = prices
        self.pct_chg = pct_chg
        self.is_st = is_st
        self.trade_status = trade_status
        self.amount = daily_amount
        self.composite = composite
        self.codes = codes
        self.dates = dates
        self.rebal_period = rebal_period
        self.risk_mult = risk_mult      # 风险平价预算乘数（date×code），None=固定1%等权
        self.full_exp = full_exp        # 全仓位模式配置（项目 5400），None=关闭
        self.vol_scale = vol_scale      # 市场波动率目标缩放（date→scale），全仓位模式用
        self.market_allow = market_allow
        self.atr_pct = atr_pct
        self.drawdown_stop = drawdown_stop
        self.positions: Dict[str, dict] = {}
        self.cash = float(capital)
        self.trades: List[dict] = []
        self.equity: Dict[pd.Timestamp, float] = {}

    # ---------- 估值 ----------
    def _mark_price(self, code: str, d: pd.Timestamp) -> Optional[float]:
        """持仓估值价：当日有价则用，无（停牌/缺失）用最近价（仅估值，不用于信号）。"""
        if code in self.prices.columns:
            s = self.prices[code]
            if d in s.index and not np.isnan(s.loc[d]):
                return float(s.loc[d])
            hist = s[s.index <= d].dropna()
            return float(hist.iloc[-1]) if len(hist) else None
        return None

    # ---------- 成交 ----------
    def _try_fill(self, d: pd.Timestamp, code: str, side: str,
                  reason: str, budget: Optional[float] = None) -> Optional[dict]:
        ts = code
        def cell(frame: pd.DataFrame):
            return frame.loc[d, code] if d in frame.index and code in frame.columns else np.nan

        pct = cell(self.pct_chg)
        st = cell(self.is_st)
        ts_status = cell(self.trade_status)
        px = cell(self.prices)
        amt = cell(self.amount)
        try:
            price = float(px)
            liquidity = float(amt)
        except (TypeError, ValueError):
            return None
        if not np.isfinite(price) or price <= 0 or not np.isfinite(liquidity) or liquidity <= 0:
            return None                      # 无价（停牌/缺失）不可成交
        ok, why = ex.check_fillable(ts, st, pct, ts_status, side)
        if not ok:
            return None                      # 涨停/跌停/停牌 → 放弃（保持现价不动）

        if side == 'buy':
            fill = ex.fill_price(float(px), 'buy', self.cfg['backtest']['slippage'])
            budget = budget if budget is not None else self.capital * 0.01
            # 流动性约束：单笔 ≤ 当日成交额 10%
            budget = ex.liquidity_cap(liquidity, budget, 0.10)
            if budget <= 0 or self.cash <= 0:
                return None
            shares = int(np.floor(min(budget, self.cash) / fill / 100) * 100)  # 100股整数手
            if shares <= 0:
                return None
            amount = shares * fill
            cost = ex.compute_costs('buy', amount, ts, **self._cost_args())
            total = amount + cost['total']
            if total > self.cash:
                shares = int(np.floor((self.cash - 5) / fill / 100) * 100)     # 留最低佣金
                if shares <= 0:
                    return None
                amount = shares * fill
                cost = ex.compute_costs('buy', amount, ts, **self._cost_args())
                total = amount + cost['total']
            self.cash -= total
            self.positions[code] = {'shares': shares, 'entry_price': fill,
                                    'entry_date': d, 'cost_basis': total,
                                    'high_watermark': fill, 'trailing_active': False}
            trade = {'date': d, 'code': code, 'side': 'buy', 'price': fill, 'shares': shares,
                     'amount': amount, 'cost_total': cost['total'], 'reason': reason,
                     'pnl': None, 'held_days': None}
            self.trades.append(trade)
            return trade

        else:  # sell
            pos = self.positions.get(code)
            if not pos:
                return None
            entry_date = pos.get('entry_date')
            if entry_date is None or pd.isna(entry_date):
                return None
            try:
                if pd.Timestamp(entry_date).normalize() >= pd.Timestamp(d).normalize():
                    return None             # 股票 T+1；买入当日不可卖出或期末强平
            except (TypeError, ValueError):
                return None
            fill = ex.fill_price(float(px), 'sell', self.cfg['backtest']['slippage'])
            shares = pos['shares']
            amount = shares * fill
            if amount > ex.liquidity_cap(liquidity, amount, 0.10):
                return None                 # 日成交额代理不足，整笔卖出不可视为成交
            cost = ex.compute_costs('sell', amount, ts, **self._cost_args())
            self.cash += amount - cost['total']
            pnl = (amount - cost['total']) - pos['cost_basis']
            held = None
            if pos['entry_date'] in self.dates:
                ei = self.dates.index(pos['entry_date'])
                di = self.dates.index(d)
                held = di - ei
            trade = {'date': d, 'code': code, 'side': 'sell', 'price': fill,
                     'shares': shares, 'amount': amount, 'cost_total': cost['total'],
                     'reason': reason, 'pnl': pnl, 'held_days': held}
            self.trades.append(trade)
            del self.positions[code]
            return trade

    # ---------- 主循环 ----------
    def run(self) -> dict:
        dates = self.dates
        running_peak = self.capital
        for i, d in enumerate(dates):
            # 1) 执行昨日生成的订单（在 d 日收盘）
            if i > 0 and getattr(self, '_pending_sells', None):
                for code in list(self._pending_sells):
                    self._try_fill(d, code, 'sell', self._pending_sells[code])
                self._pending_sells = {}
            if i > 0 and getattr(self, '_pending_buys', None):
                for code, budget in list(self._pending_buys.items()):
                    if code in self.positions:
                        continue
                    self._try_fill(d, code, 'buy', self._pending_buy_reason, budget=budget)
                self._pending_buys = {}

            equity_now = self.equity_val(d)
            running_peak = max(running_peak, equity_now)
            current_drawdown = equity_now / running_peak - 1 if running_peak else 0.0
            drawdown_block = self.drawdown_stop is not None and current_drawdown <= -abs(self.drawdown_stop)
            market_scale = float(self.market_allow.get(d, 0.0)) if self.market_allow is not None else 1.0
            market_ok = market_scale > 0

            # 2) 在 d 日收盘生成次日订单（信号只用 ≤d 数据）
            pending_sells, pending_buys = {}, {}
            # 全仓位模式参数覆盖（项目 5400 预注册）：止损 -8%、无止盈、单股上限 8%
            stop_loss = self.cfg['backtest']['stop_loss']
            take_profit = self.cfg['backtest']['take_profit']
            max_position = self.cfg['backtest']['max_position']
            buy_reason = 'score>1.5'
            if self.full_exp:
                stop_loss = self.full_exp.get('stop_loss', stop_loss)
                take_profit = self.full_exp.get('take_profit', take_profit)   # None=不设止盈
                max_position = self.full_exp.get('max_position', max_position)
                buy_reason = 'top-30买入'
            # 退出：止损/止盈/得分<阈值（每日检查）
            for code, pos in list(self.positions.items()):
                px = self._mark_price(code, d)
                if px is None:
                    continue                       # 无价，无法判断，保持
                ret = px / pos['entry_price'] - 1
                pos['high_watermark'] = max(float(pos.get('high_watermark') or px), px)
                taskbook_risk = bool(self.cfg['backtest'].get('taskbook_risk'))
                atr = None
                if taskbook_risk and self.atr_pct is not None and code in self.atr_pct.columns:
                    atr_value = self.atr_pct.loc[d, code]
                    if atr_value is not None and not np.isnan(atr_value):
                        atr = float(atr_value) * pos['entry_price']
                hard_stop = max(pos['entry_price'] * 0.92,
                                pos['entry_price'] - 2 * atr) if taskbook_risk and atr else pos['entry_price'] * (1 + stop_loss)
                if taskbook_risk and ret >= 0.15:
                    pos['trailing_active'] = True
                trailing_stop = (max(pos['entry_price'], pos['high_watermark'] - 2 * atr)
                                 if pos.get('trailing_active') and atr else None)
                effective_stop = max(hard_stop, trailing_stop) if trailing_stop is not None else hard_stop
                if px <= effective_stop:
                    pending_sells[code] = '止损'
                elif take_profit is not None and ret >= take_profit:
                    pending_sells[code] = '止盈'
                else:
                    cv = self.composite.loc[d, code] if code in self.composite.columns else np.nan
                    if not np.isnan(cv) and cv < self.cfg['backtest']['sell_threshold']:
                        pending_sells[code] = '得分<-1.5'
                if market_scale == 0 and self.cfg['backtest'].get('market_liquidate', True):
                    pending_sells[code] = '指数跌破20/60日均线'
                # 单股最大仓位：超限部分减仓
                if code in self.positions:
                    val = px * pos['shares']
                    if self.capital > 0 and val > max_position * self.equity_val(d):
                        pending_sells[code] = f'仓位>{max_position*100:.0f}%减仓'
            # 买入：仅在调仓日评估
            if (not drawdown_block and market_ok and
                    i % self.rebal_period == 0 and d in self.composite.index):
                comp_day = self.composite.loc[d].dropna()
                if self.full_exp:
                    # 全仓位：top-30 且 composite>0，预算按目标总暴露×波动率目标缩放×个股风险权重
                    if len(comp_day) >= 5:
                        ranked = comp_day.sort_values(ascending=False)
                        buys = ranked.head(self.full_exp['n_buy'])
                        buys = buys[buys > 0]
                        # 跌出 top-n_sell_rank 的持仓 → 月度旋转卖出
                        keep = set(ranked.head(self.full_exp['n_sell_rank']).index)
                        for code in list(self.positions):
                            if code in keep or code not in comp_day.index:
                                continue
                            pending_sells[code] = '跌出top-40'
                        scale = 1.0
                        if self.vol_scale is not None:
                            s = self.vol_scale.get(d)
                            if s is not None and not np.isnan(s):
                                scale = float(s)
                        n = max(int(len(buys)), 1)
                        base = self.capital * self.full_exp['target_gross'] * scale / n
                        lo = self.full_exp.get('budget_lo', 0.0) * self.capital
                        hi = min(self.full_exp.get('budget_hi', max_position),
                                 max_position) * self.capital   # 单票买入 ≤ 仓位上限
                        for code in buys.index:
                            if code in self.positions:
                                continue
                            b = base
                            if self.risk_mult is not None and code in self.risk_mult.columns:
                                m = self.risk_mult.loc[d, code]
                                if not np.isnan(m):
                                    b *= float(m)
                            pending_buys[code] = min(max(b * market_scale, lo), hi)
                else:
                    for code in self.codes:
                        if code in self.positions or code not in comp_day.index:
                            continue
                        cv = comp_day[code]
                        if not np.isnan(cv) and cv > self.cfg['backtest']['buy_threshold']:
                            b = self.capital * 0.01 * market_scale
                            if self.risk_mult is not None and code in self.risk_mult.columns:
                                m = self.risk_mult.loc[d, code]
                                if not np.isnan(m):
                                    b *= float(m)
                            pending_buys[code] = b
            self._pending_sells = pending_sells
            self._pending_buys = pending_buys
            self._pending_buy_reason = buy_reason

            # 3) d 日收盘结算（持仓按 d 收盘估值）
            self.equity[d] = self.equity_val(d)

        # 期末：未平仓按最后价强制平仓（记录 reason='期末平仓'）
        if self.positions:
            last_d = dates[-1]
            for code in list(self.positions):
                self._try_fill(last_d, code, 'sell', '期末平仓')
            self.equity[last_d] = self.equity_val(last_d)

        eq = pd.Series(self.equity).sort_index()
        return {'equity': eq, 'trades': self.trades, 'final_cash': self.cash,
                'final_positions': {k: v['shares'] for k, v in self.positions.items()}}

    def equity_val(self, d: pd.Timestamp) -> float:
        v = self.cash
        for code, pos in self.positions.items():
            px = self._mark_price(code, d)
            if px is not None:
                v += px * pos['shares']
        return v
