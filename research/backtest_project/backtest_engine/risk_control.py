#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
risk_control.py — 引擎侧风控与仓位（任务书 Step 2）

职责边界（诚实声明）：
  - 本模块是**独立交付物**，不为了美化回测结果而改动历史执行。
  - 风险偏好三档参数为**先验设定**（用户风险偏好分层），不对回测时段搜索/过拟合。
  - 提供两类用法：
      1) 事后风控体检：对任意回测/组合的净值曲线、持仓、成交做边界检查 + 回撤预警 + 集中度审计，
         产出 actions/warnings/violations（可在 5200 /risk 页展示）；
      2) 可被策略/组合调用：Kelly 仓位、波动率目标缩放、单票/行业/现金硬边界，作为仓位预算裁剪函数。

只读依赖 numpy/pandas，可被回测引擎与 5200 站点共同 import，无站点依赖。
"""

from __future__ import annotations
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

# ── 用户风险偏好三档（先验分层，非回测调参） ──────────────────────────────
RISK_PROFILES: Dict[str, dict] = {
    'conservative': {
        'label': '保守',
        'max_total_position': 0.50,   # 组合总仓位上限（现金 ≥ 50%）
        'max_single': 0.05,           # 单股净值占比上限
        'max_industry': 0.20,         # 单行业净值占比上限
        'min_cash': 0.50,             # 现金下限
        'drawdown_warn': 0.08,        # 回撤预警阈值（限制新开仓）
        'drawdown_stop': 0.15,        # 回撤硬止损（强制减仓）
        'kelly_cap': 0.25,            # 凯利使用比例（fraction of full Kelly）
        'vol_floor': 0.30,            # 波动率缩放下限
        'vol_cap': 1.00,              # 波动率缩放上限
        'stop_loss': -0.04,           # 单股止损
    },
    'moderate': {
        'label': '稳健',
        'max_total_position': 0.80,
        'max_single': 0.10,
        'max_industry': 0.30,
        'min_cash': 0.20,
        'drawdown_warn': 0.12,
        'drawdown_stop': 0.20,
        'kelly_cap': 0.50,
        'vol_floor': 0.40,
        'vol_cap': 1.20,
        'stop_loss': -0.05,
    },
    'aggressive': {
        'label': '进取',
        'max_total_position': 1.00,
        'max_single': 0.15,
        'max_industry': 0.40,
        'min_cash': 0.05,
        'drawdown_warn': 0.15,
        'drawdown_stop': 0.25,
        'kelly_cap': 1.00,
        'vol_floor': 0.50,
        'vol_cap': 1.50,
        'stop_loss': -0.06,
    },
}
DEFAULT_PROFILE = 'moderate'


def get_risk_profile(name: str = DEFAULT_PROFILE) -> dict:
    """按名称取风险偏好参数；未知名称取稳健档并如实告警。"""
    if name not in RISK_PROFILES:
        return dict(RISK_PROFILES[DEFAULT_PROFILE], source='default(moderate)')
    return dict(RISK_PROFILES[name], source=name)


# ── 凯利仓位 ─────────────────────────────────────────────────────────────
def kelly_fraction(win_rate: float, profit_loss_ratio: float,
                   cap: Optional[float] = None) -> float:
    """全凯利 f* = (b·p − q) / b；返回裁剪后的仓位分数（0~cap）。

    诚实约束：无正期望（f*≤0）返回 0；cap 默认全凯利，风险偏好档可收紧到 1/4 或 1/2 凯利。
    profit_loss_ratio = 平均盈利/平均亏损（b）。
    """
    if not (0 < win_rate < 1) or profit_loss_ratio <= 0:
        return 0.0
    b = float(profit_loss_ratio)
    f = (b * win_rate - (1 - win_rate)) / b
    if f <= 0:
        return 0.0
    if cap is not None:
        f = min(f, float(cap))
    return float(f)


def kelly_position_size(capital: float, win_rate: float, profit_loss_ratio: float,
                        cap: Optional[float] = None) -> float:
    """凯利建议单笔预算（金额）。无正期望返回 0。"""
    return capital * kelly_fraction(win_rate, profit_loss_ratio, cap)


# ── 动态仓位：波动率目标缩放 ────────────────────────────────────────────
def volatility_scaler(current_vol_annual: float, target_vol_annual: float,
                      floor: Optional[float] = None, cap: Optional[float] = None) -> float:
    """组合波动率目标缩放 scale = target/current，裁剪到 [floor, cap]。

    动态调整仓位（任务书 2.3 动态调整）：当前波动越高 → 缩放越小 → 目标总暴露越低。
    诚实约束：current_vol≤0 或 NaN 时返回 1.0（不做缩放，如实标注"无波动数据"）。
    """
    if current_vol_annual is None or not np.isfinite(current_vol_annual) or current_vol_annual <= 0:
        return 1.0
    if target_vol_annual is None or not np.isfinite(target_vol_annual) or target_vol_annual <= 0:
        return 1.0
    scale = float(target_vol_annual) / float(current_vol_annual)
    if floor is not None:
        scale = max(scale, float(floor))
    if cap is not None:
        scale = min(scale, float(cap))
    return float(scale)


# ── 风险边界（单股/行业/现金硬约束） ────────────────────────────────────
def check_boundaries(positions: Dict[str, dict], equity_value: float, cash: float,
                     profile: dict, industry_map: Optional[Dict[str, str]] = None,
                     prices: Optional[Dict[str, float]] = None) -> List[dict]:
    """硬边界检查 → 违规列表（每项 {rule, code, value, limit, severity}）。

    positions: {code: {'shares': int}}，估值价取 prices[code]（缺失时按成本估，如实标注）。
    规则：单股净值占比 ≤ max_single；单行业净值占比 ≤ max_industry；总仓位 ≤ max_total_position（现金 ≥ min_cash）。
    severity: 'violation'（硬约束，必须处理）/ 'warning'（接近阈值）。
    """
    if equity_value is None or equity_value <= 0:
        return [{'rule': 'equity', 'code': '*', 'value': equity_value, 'limit': 0,
                 'severity': 'violation', 'note': '净值无效，无法做边界检查'}]
    out: List[dict] = []
    # 总仓位 / 现金
    total_mv = equity_value - cash
    gross = total_mv / equity_value
    if gross > profile['max_total_position'] + 1e-9:
        out.append({'rule': 'total_position', 'code': '*', 'value': round(gross, 4),
                    'limit': profile['max_total_position'], 'severity': 'violation',
                    'note': f"总仓位超限（现金 {cash/equity_value:.1%} < 下限 {profile['min_cash']:.1%}）"})
    elif gross > profile['max_total_position'] * 0.9:
        out.append({'rule': 'total_position', 'code': '*', 'value': round(gross, 4),
                    'limit': profile['max_total_position'], 'severity': 'warning',
                    'note': '总仓位接近上限'})
    # 单股 / 行业
    ind_vals: Dict[str, float] = {}
    for code, pos in positions.items():
        mv = 0.0
        if prices and code in prices and prices[code]:
            mv = pos['shares'] * prices[code]
        else:
            mv = pos.get('cost_basis', pos.get('amount', 0.0))  # 无价按成本估
        ratio = mv / equity_value
        if ratio > profile['max_single'] + 1e-9:
            out.append({'rule': 'single', 'code': code, 'value': round(ratio, 4),
                        'limit': profile['max_single'], 'severity': 'violation',
                        'note': '单股净值占比超限'})
        elif ratio > profile['max_single'] * 0.9:
            out.append({'rule': 'single', 'code': code, 'value': round(ratio, 4),
                        'limit': profile['max_single'], 'severity': 'warning',
                        'note': '单股接近上限'})
        if industry_map:
            ind = industry_map.get(code, '未知行业')
            ind_vals[ind] = ind_vals.get(ind, 0.0) + mv
    for ind, mv in ind_vals.items():
        ratio = mv / equity_value
        if ratio > profile['max_industry'] + 1e-9:
            out.append({'rule': 'industry', 'code': ind, 'value': round(ratio, 4),
                        'limit': profile['max_industry'], 'severity': 'violation',
                        'note': '单行业净值占比超限'})
        elif ratio > profile['max_industry'] * 0.9:
            out.append({'rule': 'industry', 'code': ind, 'value': round(ratio, 4),
                        'limit': profile['max_industry'], 'severity': 'warning',
                        'note': '单行业接近上限'})
    return out


# ── 回撤预警 ─────────────────────────────────────────────────────────────
def drawdown_metrics(equity_series: pd.Series) -> dict:
    """净值曲线 → {current_drawdown, max_drawdown, peak_date, trough_date}（纯函数，无状态）。"""
    if equity_series is None or len(equity_series) == 0:
        return {'current_drawdown': 0.0, 'max_drawdown': 0.0, 'peak_date': None, 'trough_date': None}
    eq = pd.Series(equity_series).astype(float)
    peak = eq.cummax()
    dd = eq / peak - 1.0
    max_dd = float(dd.min()) if len(dd) else 0.0
    trough_date = dd.idxmin() if len(dd) else None
    peak_date = peak[peak == peak.loc[trough_date]].index[0] if trough_date is not None else None
    return {'current_drawdown': float(dd.iloc[-1]) if len(dd) else 0.0,
            'max_drawdown': max_dd, 'peak_date': peak_date, 'trough_date': trough_date}


def drawdown_alert(equity_series: pd.Series, profile: dict) -> dict:
    """回撤预警：超过 drawdown_warn → 限制新开仓；超过 drawdown_stop → 强制减仓。"""
    m = drawdown_metrics(equity_series)
    cur = m['current_drawdown']
    return {
        'metrics': m,
        'warn_breached': cur <= -abs(profile['drawdown_warn']),
        'stop_breached': cur <= -abs(profile['drawdown_stop']),
        'restrict_new_positions': cur <= -abs(profile['drawdown_warn']),
        'force_reduce': cur <= -abs(profile['drawdown_stop']),
        'warn_threshold': profile['drawdown_warn'],
        'stop_threshold': profile['drawdown_stop'],
        'message': (f"回撤 {cur:.1%}（预警线 {-profile['drawdown_warn']:.0%} / 硬止损线 "
                    f"{-profile['drawdown_stop']:.0%}）"),
    }


# ── 风险管理器（组合层） ────────────────────────────────────────────────
class RiskManager:
    """把风险偏好档 + 凯利 + 波动率缩放 + 边界约束收敛成一个统一的仓位预算裁剪层。

    可被策略/组合调用：给定候选买入列表，返回每只建议预算（经 max_single、行业剩余容量、
    总仓位上限、凯利、波动率缩放共同裁剪），并给出 actions/warnings。
    """

    def __init__(self, profile_name: str = DEFAULT_PROFILE,
                 win_rate: Optional[float] = None,
                 profit_loss_ratio: Optional[float] = None,
                 target_vol_annual: float = 0.20):
        self.profile = get_risk_profile(profile_name)
        self.win_rate = win_rate
        self.profit_loss_ratio = profit_loss_ratio
        self.target_vol_annual = target_vol_annual

    def kelly_budget(self, capital: float) -> float:
        """凯利建议预算（单笔）。无 win_rate/plr 数据 → 返回 None 并由调用方按等权兜底（诚实标注）。"""
        if self.win_rate is None or self.profit_loss_ratio is None:
            return 0.0
        return kelly_position_size(capital, self.win_rate, self.profit_loss_ratio,
                                   cap=self.profile['kelly_cap'] * self.profile['max_single'] * 4)

    def position_cap(self, capital: float, equity_value: float, cash: float,
                     current_gross: float, industry_value: Dict[str, float],
                     code: str, industry: Optional[str] = None) -> float:
        """单票建议预算上限 = min(单股上限×净值, 行业剩余容量, 现金/总仓位剩余)。"""
        p = self.profile
        caps = []
        caps.append(p['max_single'] * equity_value)                 # 单股净值上限
        if industry is not None:
            room = p['max_industry'] * equity_value - industry_value.get(industry, 0.0)
            caps.append(max(room, 0.0))                             # 行业剩余容量
        caps.append(p['max_total_position'] * equity_value - current_gross * equity_value)
        caps.append(max(cash, 0.0))                                 # 现金
        return max(min(caps), 0.0)

    def apply(self, equity_series: pd.Series, positions: Dict[str, dict], cash: float,
              prices: Optional[Dict[str, float]] = None,
              industry_map: Optional[Dict[str, str]] = None,
              candidates: Optional[List[str]] = None) -> dict:
        """对当前状态做完整风控体检 → {alerts, violations, warnings, budgets}。"""
        equity_value = float(pd.Series(equity_series).iloc[-1]) if len(equity_series) else float('nan')
        alerts = drawdown_alert(equity_series, self.profile)
        violations = check_boundaries(positions, equity_value, cash, self.profile,
                                      industry_map, prices)
        budgets = {}
        if candidates:
            ind_vals = {}
            for c, pos in positions.items():
                mv = pos['shares'] * prices[c] if (prices and c in prices and prices[c]) else pos.get('cost_basis', 0)
                ind_vals[industry_map.get(c, '未知行业')] = ind_vals.get(industry_map.get(c, '未知行业'), 0) + mv
            current_gross = (equity_value - cash) / equity_value if equity_value else 0.0
            kelly = self.kelly_budget(equity_value)
            for code in candidates:
                budget = self.position_cap(equity_value, equity_value, cash, current_gross,
                                           ind_vals, code, industry_map.get(code) if industry_map else None)
                if kelly > 0:
                    budget = min(budget, kelly)                    # 凯利再裁剪
                budgets[code] = max(round(budget, 2), 0.0)
        return {
            'profile': self.profile['label'],
            'alerts': alerts,
            'violations': [v for v in violations if v['severity'] == 'violation'],
            'warnings': [v for v in violations if v['severity'] == 'warning'],
            'budgets': budgets,
        }


def risk_report(equity_series: pd.Series, positions: Dict[str, dict], cash: float,
                profile_name: str = DEFAULT_PROFILE,
                prices: Optional[Dict[str, float]] = None,
                industry_map: Optional[Dict[str, str]] = None) -> dict:
    """一站式风控报告（供 5200 /risk 页 JSON 化）。"""
    mgr = RiskManager(profile_name)
    return mgr.apply(equity_series, positions, cash, prices, industry_map)


if __name__ == '__main__':
    # 自检：合成净值验证回撤预警/凯利/边界（净值 100k 尺度，持仓占净值 8%~10%，制造 -25% 回撤）
    idx = pd.date_range('2026-01-01', periods=60, freq='D')
    rise = np.linspace(100_000, 120_000, 40)
    drop = np.linspace(120_000, 90_000, 20)
    eq = pd.Series(np.concatenate([rise, drop]), index=idx)
    pos = {'000001.SZ': {'shares': 1000, 'cost_basis': 8500},    # 净值占比 ~9.4%
           '600519.SH': {'shares': 100, 'cost_basis': 16500}}    # 净值占比 ~18%（单行业超限演示）
    prices = {'000001.SZ': 8.5, '600519.SH': 165.0}
    rep = risk_report(eq, pos, cash=50000, profile_name='moderate',
                      prices=prices, industry_map={'000001.SZ': '银行', '600519.SH': '食品饮料'})
    print('=== RiskManager 自检 ===')
    print('回撤预警:', rep['alerts']['message'], '| 限制新开仓:', rep['alerts']['restrict_new_positions'])
    print('违规:', rep['violations'])
    print('警告:', rep['warnings'])
    k = kelly_fraction(0.55, 1.5, cap=0.25)
    print(f'凯利(55%胜率, 盈亏比1.5, cap=0.25): {k:.3f} → 建议 {k*100:.1f}% 仓位')
    vs = volatility_scaler(0.30, 0.20, floor=0.4, cap=1.2)
    print(f'波动率缩放(当前30%→目标20%): {vs:.2f} → 总暴露降至 {vs*100:.0f}%')
