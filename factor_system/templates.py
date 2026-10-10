"""模板因子库（任务书第四部分）。

按家族 A-I 提供「带经济逻辑的种子」。每个模板：
  - family / tpl_id / name / formula（人类可读）
  - description（经济逻辑：做什么、为何可能有效）
  - direction（higher_is_better / lower_is_better / bidirectional）
  - data_requires（依赖的字段）
  - param_grid（参数网格，搜索引擎据此展开）
  - compute(panel, params) -> 日期×代码 因子面板

数据不可得的家族按诚实降级原则注册为 BLOCKED（见 BLOCKED_TEMPLATES），
不计算、不出业绩，与现有 open_source_factor_registry 的 blocked 口径一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from . import operators as op
from .data import Panel
from .preprocess import industry_exposures


@dataclass(frozen=True)
class Template:
    family: str
    tpl_id: str
    name: str
    formula: str
    description: str
    direction: str
    data_requires: tuple[str, ...]
    compute: Callable[[Panel, dict], pd.DataFrame] | None
    param_grid: tuple[dict, ...] = field(default_factory=tuple)

    def params(self):
        return self.param_grid or ({},)


# ── 家族 A：动量 Momentum ────────────────────────────────────────

def _mom(close, n): return close / close.shift(n) - 1.0

_tpl_a1 = Template(
    "A_momentum", "A1", "普通动量",
    "close[t]/close[t-n] - 1",
    "趋势延续：过去 n 日涨幅越高的股票短期继续跑赢的概率更大（A股动量效应在中周期最稳）。",
    "higher_is_better", ("close",),
    lambda p, q: _mom(p.close, q["n"]),
    tuple({"n": n} for n in (5, 10, 20, 40, 60, 120, 180, 250)),
)
_tpl_a2 = Template(
    "A_momentum", "A2", "风险调整动量",
    "ts_mean(ret,n) / ts_std(ret,n)",
    "单位风险的动量：动量除以已实现波动，偏好平稳上涨而非暴涨暴跌，降低回撤暴露。",
    "higher_is_better", ("close",),
    lambda p, q: op.ts_ir(p.ret, q["n"]),
    tuple({"n": n} for n in (10, 20, 60, 120)),
)
_tpl_a3 = Template(
    "A_momentum", "A3", "剔除近月动量",
    "close[t-5]/close[t-60] - 1",
    "中期趋势剔除最近 5 日，规避短期反转噪音（A股短期反转强，动量需跳近期）。",
    "higher_is_better", ("close",),
    lambda p, q: p.close.shift(q["skip"]) / p.close.shift(q["n"]) - 1.0,
    tuple({"skip": 5, "n": n} for n in (20, 40, 60, 120)),
)
_tpl_a4 = Template(
    "A_momentum", "A4", "平滑动量",
    "ts_decay_linear(ret, w)",
    "动量做线性衰减平滑：近期收益权重更高，捕捉动量同时抑制单日噪音。",
    "higher_is_better", ("close",),
    lambda p, q: op.ts_decay_linear(p.ret, q["w"]),
    tuple({"w": w} for w in (10, 20, 40)),
)
_tpl_a5 = Template(
    "A_momentum", "A5", "多周期动量平均",
    "mean(mom_5, mom_20, mom_60)",
    "短中长期动量等权平均：跨周期一致性信号，降低单一周期失效风险。",
    "higher_is_better", ("close",),
    lambda p, q: (_mom(p.close, 5) + _mom(p.close, 20) + _mom(p.close, 60)) / 3.0,
)

# ── 家族 B：反转 Reversal ────────────────────────────────────────

_tpl_b1 = Template(
    "B_reversal", "B1", "短期反转",
    "-mom_5",
    "A股最稳健的短期反转：过去 5 日涨幅过高者短期回调概率大，做多反转。",
    "lower_is_better", ("close",),
    lambda p, q: -_mom(p.close, q["n"]),
    tuple({"n": n} for n in (2, 5, 10)),
)
_tpl_b2 = Template(
    "B_reversal", "B2", "日内反转",
    "-(close/open - 1)",
    "日内高开低走者次日常反转；以开盘为锚的日线反转代理。",
    "lower_is_better", ("close", "open"),
    lambda p, q: -(p.close / p.open_ - 1.0),
)
_tpl_b3 = Template(
    "B_reversal", "B3", "中期反转",
    "-(mom_20 + mom_60)",
    "中期涨幅过大后回落，捕捉 20-60 日反转带。",
    "lower_is_better", ("close",),
    lambda p, q: -(_mom(p.close, 20) + _mom(p.close, 60)),
)

# ── 家族 C：波动率 Volatility ────────────────────────────────────

_tpl_c1 = Template(
    "C_volatility", "C1", "已实现波动",
    "ts_std(ret,n) * sqrt(250)",
    "低波动异象：波动率低者预期收益高（A股亦有此效应）。",
    "lower_is_better", ("close",),
    lambda p, q: op.ts_std(p.ret, q["n"]) * np.sqrt(250),
    tuple({"n": n} for n in (10, 20, 60)),
)
_tpl_c2 = Template(
    "C_volatility", "C2", "相对市场特质波动",
    "ts_std(ret - xs_mean(ret), n)",
    "剔除横截面市场均值后的特质波动：纯公司层面波动性。",
    "lower_is_better", ("close",),
    lambda p, q: op.ts_std(p.ret.sub(p.ret.mean(axis=1), axis=0), q["n"]),
    tuple({"n": n} for n in (20, 60)),
)
_tpl_c3 = Template(
    "C_volatility", "C3", "波动比率",
    "ts_std(ret,5) / ts_std(ret,60)",
    "短期/长期波动比：波动突增预警或低波延续信号，双向测试。",
    "bidirectional", ("close",),
    lambda p, q: op.safe_div(op.ts_std(p.ret, 5), op.ts_std(p.ret, 60)),
)
_tpl_c4 = Template(
    "C_volatility", "C4", "下侧波动",
    "sqrt(mean(min(ret,0)^2, n))",
    "仅计入下跌的半方差：衡量下行风险，低者更受避险资金青睐。",
    "lower_is_better", ("close",),
    lambda p, q: (p.ret.clip(upper=0) ** 2).rolling(q["n"], min_periods=max(2, q["n"] // 2)).mean().pow(0.5),
    tuple({"n": n} for n in (20, 60)),
)
_tpl_c5 = Template(
    "C_volatility", "C5", "波动稳定性",
    "-ts_std(vol_level, n)",
    "波动本身随时间稳定的股票更可预测，反向为信号。",
    "higher_is_better", ("close",),
    lambda p, q: -op.ts_std(op.ts_std(p.ret, 20), q["n"]),
    tuple({"n": n} for n in (20, 60)),
)

# ── 家族 D：价值 Valuation（PIT）────────────────────────────────

_tpl_d1 = Template(
    "D_value", "D1", "盈利收益率 EP",
    "1/pe_ttm",
    "PIT 口径的盈利收益率：低估值（高 EP）长期倾向更高收益（价值效应）。",
    "higher_is_better", ("pe_ttm",),
    lambda p, q: op.safe_div(pd.DataFrame(1.0, index=p.pe_ttm.index, columns=p.pe_ttm.columns), p.pe_ttm),
)
_tpl_d2 = Template(
    "D_value", "D2", "账面收益率 BP",
    "1/pb_mrq",
    "PIT 口径账面市值比：价值因子经典代理。",
    "higher_is_better", ("pb_mrq",),
    lambda p, q: op.safe_div(pd.DataFrame(1.0, index=p.pb_mrq.index, columns=p.pb_mrq.columns), p.pb_mrq),
)
_tpl_d3 = Template(
    "D_value", "D3", "综合价值 EP+BP",
    "xs_rank(1/pe) + xs_rank(1/pb)",
    "盈利与账面双价值合分，降低单一价值代理失效风险。",
    "higher_is_better", ("pe_ttm", "pb_mrq"),
    lambda p, q: op.xs_rank(op.safe_div(pd.DataFrame(1.0, index=p.pe_ttm.index, columns=p.pe_ttm.columns), p.pe_ttm))
    + op.xs_rank(op.safe_div(pd.DataFrame(1.0, index=p.pb_mrq.index, columns=p.pb_mrq.columns), p.pb_mrq)),
)

# ── 家族 F：流动性 Liquidity ─────────────────────────────────────

_tpl_f1 = Template(
    "F_liquidity", "F1", "换手率均值",
    "ts_mean(turnover, n)",
    "低换手股票信息含量低但流动性溢价高；换手率同时反映关注度。",
    "bidirectional", ("turnover",),
    lambda p, q: op.ts_mean(p.turnover, q["n"]),
    tuple({"n": n} for n in (5, 10, 20, 60, 120)),
)
_tpl_f2 = Template(
    "F_liquidity", "F2", "Amihud 非流动性",
    "ts_mean(|ret|/amount, n)",
    "Amihud 非流动性：单位成交额价格冲击越大，非流动性溢价越高。",
    "lower_is_better", ("close", "amount"),
    lambda p, q: op.ts_mean(p.ret.abs() / p.amount.replace(0, np.nan), q["n"]),
    tuple({"n": n} for n in (20, 60)),
)
_tpl_f3 = Template(
    "F_liquidity", "F3", "量比",
    "vol / ts_mean(vol, 5)",
    "当前相对近 5 日平均量能：放量异动信号，双向。",
    "bidirectional", ("vol",),
    lambda p, q: op.safe_div(p.vol, op.ts_mean(p.vol, 5)),
)
_tpl_f4 = Template(
    "F_liquidity", "F4", "成交额增长率",
    "ts_delta(amount, n)",
    "资金活跃度变化：成交额扩张往往伴随方向性行情，双向。",
    "bidirectional", ("amount",),
    lambda p, q: op.ts_delta(p.amount, q["n"]),
    tuple({"n": n} for n in (5, 20)),
)
_tpl_f5 = Template(
    "F_liquidity", "F5", "换手率变化",
    "ts_delta(turnover, n)",
    "关注度边际变化，双向。",
    "bidirectional", ("turnover",),
    lambda p, q: op.ts_delta(p.turnover, q["n"]),
    tuple({"n": n} for n in (5, 20)),
)

# ── 家族 G：量价资金 Price-Volume-Fund ───────────────────────────

_tpl_g4 = Template(
    "G_pvf", "G4", "量价背离",
    "xs_rank(ts_delta(close,n)) - xs_rank(ts_delta(vol,n))",
    "价涨量缩 / 价跌量升的背离：量价关系不健康者倾向反转。",
    "bidirectional", ("close", "vol"),
    lambda p, q: op.xs_rank(op.ts_delta(p.close, q["n"])) - op.xs_rank(op.ts_delta(p.vol, q["n"])),
    tuple({"n": n} for n in (3, 5, 10, 20)),
)
_tpl_g5 = Template(
    "G_pvf", "G5", "资金强度代理",
    "amount / float_mv（换手强度）",
    "成交额相对流通市值的强度：直接捕捉资金活跃度（近似换手）。",
    "bidirectional", ("amount", "float_mv"),
    lambda p, q: op.safe_div(p.amount, p.float_mv),
)

# ── 家族 H：技术指标 Technical ───────────────────────────────────

_tpl_h1 = Template(
    "H_technical", "H1", "RSI",
    "100 - 100/(1+avg_gain/avg_loss)",
    "相对强弱指标：超买超卖反转与趋势确认，周期参数化。",
    "bidirectional", ("close",),
    lambda p, q: _rsi(p.close, q["n"]),
    tuple({"n": n} for n in (6, 14, 21, 28)),
)
_tpl_h2 = Template(
    "H_technical", "H2", "MACD 柱",
    "ema(fast) - ema(slow) - ema(signal)",
    "均线差动能：金叉死叉前的柱体放大，捕捉趋势启动。",
    "bidirectional", ("close",),
    lambda p, q: _macd(p.close, q["fast"], q["slow"], q["signal"]),
    tuple({"fast": f, "slow": s, "signal": 9} for f, s in ((5, 13), (12, 26), (24, 52))),
)
_tpl_h3 = Template(
    "H_technical", "H3", "均线多头排列强度",
    "ma5/ma20 - ma20/ma60",
    "短中长期均线斜率差：多头排列的强度度量。",
    "higher_is_better", ("close",),
    lambda p, q: p.close.rolling(5).mean().div(p.close.rolling(20).mean())
    - p.close.rolling(20).mean().div(p.close.rolling(60).mean()),
)
_tpl_h4 = Template(
    "H_technical", "H4", "布林位置",
    "(close - ma20) / (2*std20)",
    "价格在布林带内位置：超买超卖与趋势强度。",
    "bidirectional", ("close",),
    lambda p, q: (p.close - p.close.rolling(20).mean()).div(
        2 * p.close.rolling(20).std().replace(0, np.nan)),
)
_tpl_h5 = Template(
    "H_technical", "H5", "KDJ-J",
    "3K - 2D",
    "随机指标 J 值：超买超卖（>100/<0）与趋势动能。",
    "bidirectional", ("close", "high", "low"),
    lambda p, q: _kdj_j(p.close, p.high, p.low, q["n"]),
    tuple({"n": n} for n in (9, 14)),
)
_tpl_h6 = Template(
    "H_technical", "H6", "动量加速度",
    "ts_delta(mom_5, n)",
    "动量的二阶导数：加速上涨/减速，捕捉趋势拐点。",
    "bidirectional", ("close",),
    lambda p, q: op.ts_delta(_mom(p.close, 5), q["n"]),
    tuple({"n": n} for n in (3, 5)),
)


# ── 家族 K：KBar（日本蜡烛形态，Qlib Alpha158 / WorldQuant 常用）──

def _ohlc_adj(p: Panel):
    """统一复权 OHLC：adj_open/close 已是复权，high/low 用同源复权因子修正，
    避免除权除息日在 KBar 形态上产生虚假跳变（与 P2-2 口径一致）。"""
    ratio = p.adj_open.div(p.open_.replace(0, np.nan))
    return p.adj_open, p.close, p.high.mul(ratio), p.low.mul(ratio)


_tpl_k1 = Template(
    "K_kbar", "K1", "KBar 实体 KMID",
    "(close - open)/open",
    "日内多空力量实体：阳线实体强弱，短期动量/反转双方向。",
    "bidirectional", ("open", "high", "low", "close"),
    lambda p, q: (lambda o, c, h, l: (c - o).div(o.replace(0, np.nan)))(*_ohlc_adj(p)),
)
_tpl_k2 = Template(
    "K_kbar", "K2", "KBar 振幅 KLEN",
    "(high - low)/open",
    "日内振幅：波动性代理，低振幅更稳。",
    "bidirectional", ("open", "high", "low", "close"),
    lambda p, q: (lambda o, c, h, l: (h - l).div(o.replace(0, np.nan)))(*_ohlc_adj(p)),
)
_tpl_k3 = Template(
    "K_kbar", "K3", "上影线 KUP",
    "(high - max(open,close))/open",
    "上影线：冲高回落压力，趋势见顶信号之一。",
    "bidirectional", ("open", "high", "low", "close"),
    lambda p, q: (lambda o, c, h, l: (h - np.maximum(o, c)).div(o.replace(0, np.nan)))(*_ohlc_adj(p)),
)
_tpl_k4 = Template(
    "K_kbar", "K4", "下影线 KLOW",
    "(min(open,close) - low)/open",
    "下影线：杀跌承接，底部抵抗信号之一。",
    "bidirectional", ("open", "high", "low", "close"),
    lambda p, q: (lambda o, c, h, l: (np.minimum(o, c) - l).div(o.replace(0, np.nan)))(*_ohlc_adj(p)),
)
_tpl_k5 = Template(
    "K_kbar", "K5", "RSV 随机值",
    "(close - min(low,n))/(max(high,n) - min(low,n))",
    "随机指标 RSV：价格在 n 日高低区间的位置，超买超卖。",
    "bidirectional", ("open", "high", "low", "close"),
    lambda p, q: (lambda o, c, h, l:
                  (c - l.rolling(q["n"], min_periods=max(2, q["n"] // 2)).min()).div(
                      (h.rolling(q["n"], min_periods=max(2, q["n"] // 2)).max()
                       - l.rolling(q["n"], min_periods=max(2, q["n"] // 2)).min()).replace(0, np.nan)))(
        *_ohlc_adj(p)),
    tuple({"n": n} for n in (5, 10, 20)),
)
_tpl_k6 = Template(
    "K_kbar", "K6", "量价相关",
    "ts_corr(close_ret, vol/ts_mean(vol,5), n)",
    "价格变动与量能变化的相关：量价共振方向，双向。",
    "bidirectional", ("close", "vol"),
    lambda p, q: op.ts_corr(p.close.pct_change(fill_method=None),
                            op.safe_div(p.vol, op.ts_mean(p.vol, 5)), q["n"]),
    tuple({"n": n} for n in (5, 10)),
)
_tpl_k7 = Template(
    "K_kbar", "K7", "影线差 KUP-KLOW",
    "(high - max(o,c) - min(o,c) + low)/open",
    "上影减下影：上方压力 vs 下方承接的相对强弱。",
    "bidirectional", ("open", "high", "low", "close"),
    lambda p, q: (lambda o, c, h, l:
                  (h - np.maximum(o, c) - np.minimum(o, c) + l).div(o.replace(0, np.nan)))(*_ohlc_adj(p)),
)

# ── 家族 J：行业相对（行业内 rank/中性化）──

def _industry_of(p: Panel):
    exp = industry_exposures(p)
    if exp["mode"] != "industry+size":
        return None
    return pd.Series(exp["ind_labs"], index=exp["codes"])


_tpl_j1 = Template(
    "J_industry", "J1", "行业相对动量",
    "行业内 xs_rank 去均值(mom_20)",
    "剔除行业共同动量后的个股相对强弱：行业中性动量（行业色被移除）。",
    "higher_is_better", ("close", "industry"),
    lambda p, q: (lambda ind: None if ind is None else
                  op.neutralize_industry(op.xs_rank(_mom(p.close, 20)), ind))(_industry_of(p)),
)
_tpl_j2 = Template(
    "J_industry", "J2", "行业相对低波",
    "行业内 xs_rank 去均值(-vol_20)",
    "行业中性低波动：同行业内波动最低的个股更受青睐。",
    "higher_is_better", ("close", "industry"),
    lambda p, q: (lambda ind: None if ind is None else
                  op.neutralize_industry(op.xs_rank(-op.ts_std(p.ret, 20)), ind))(_industry_of(p)),
)


# ── 辅助计算 ─────────────────────────────────────────────────────

def _rsi(close: pd.DataFrame, n: int) -> pd.DataFrame:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    al = loss.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    rs = ag / al.replace(0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def _macd(close: pd.DataFrame, fast: int, slow: int, signal: int) -> pd.DataFrame:
    line = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    return line - line.ewm(span=signal, adjust=False).mean()


def _kdj_j(close: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame, n: int) -> pd.DataFrame:
    llv = low.rolling(n, min_periods=max(2, n // 2)).min()
    hhv = high.rolling(n, min_periods=max(2, n // 2)).max()
    rsv = (close - llv) / (hhv - llv).replace(0, np.nan) * 100.0
    k = rsv.ewm(alpha=1.0 / 3, adjust=False).mean()
    d = k.ewm(alpha=1.0 / 3, adjust=False).mean()
    return 3.0 * k - 2.0 * d


# ── 全部可计算模板 ───────────────────────────────────────────────

TEMPLATES: tuple[Template, ...] = (
    _tpl_a1, _tpl_a2, _tpl_a3, _tpl_a4, _tpl_a5,
    _tpl_b1, _tpl_b2, _tpl_b3,
    _tpl_c1, _tpl_c2, _tpl_c3, _tpl_c4, _tpl_c5,
    _tpl_d1, _tpl_d2, _tpl_d3,
    _tpl_f1, _tpl_f2, _tpl_f3, _tpl_f4, _tpl_f5,
    _tpl_g4, _tpl_g5,
    _tpl_h1, _tpl_h2, _tpl_h3, _tpl_h4, _tpl_h5, _tpl_h6,
    _tpl_k1, _tpl_k2, _tpl_k3, _tpl_k4, _tpl_k5, _tpl_k6, _tpl_k7,
    _tpl_j1, _tpl_j2,
)

# ── 诚实降级：数据不可得、仅登记不计算 ────────────────────────────

@dataclass(frozen=True)
class BlockedTemplate:
    family: str
    tpl_id: str
    name: str
    description: str
    data_requires: tuple[str, ...]
    blocked_reason: str


BLOCKED_TEMPLATES: tuple[BlockedTemplate, ...] = (
    BlockedTemplate("E_quality", "E1", "ROE", "净利润/净资产", ("roe_quarterly",),
                    "blocked_pending_expanded_point_in_time_fundamentals"),
    BlockedTemplate("E_quality", "E2", "ROE 稳定性", "-ts_std(ROE, 8季)", ("roe_quarterly",),
                    "blocked_pending_expanded_point_in_time_fundamentals"),
    BlockedTemplate("E_quality", "E3", "ROA", "净利润/总资产", ("roa_quarterly",),
                    "blocked_pending_expanded_point_in_time_fundamentals"),
    BlockedTemplate("E_quality", "E4", "ROIC", "税后经营利润/投入资本", ("roic_quarterly",),
                    "blocked_pending_expanded_point_in_time_fundamentals"),
    BlockedTemplate("E_quality", "E7", "盈利质量", "经营现金流/净利润", ("ocf_cashflow",),
                    "blocked_pending_expanded_point_in_time_fundamentals"),
    BlockedTemplate("G_pvf", "G1", "大单净流入", "ts_mean(big_order_net,n)", ("big_order_net",),
                    "blocked_pending_licensed_intraday_flow_data"),
    BlockedTemplate("G_pvf", "G2", "北向占比变化", "delta(north_hold_pct,5)", ("north_hold_pct",),
                    "blocked_pending_northbound_flow_data"),
    BlockedTemplate("G_pvf", "G3", "融资余额变化率", "delta(margin_balance,n)/|..|", ("margin_balance",),
                    "blocked_pending_margin_balance_data"),
    BlockedTemplate("I_expectation", "I1", "一致预期上修", "consensus_eps 变化率", ("consensus_eps",),
                    "blocked_pending_licensed_analyst_consensus_data"),
    BlockedTemplate("I_expectation", "I2", "评级上调", "分析师评级调整", ("rating_revision",),
                    "blocked_pending_licensed_analyst_rating_data"),
    BlockedTemplate("I_expectation", "I3", "预期差", "实际EPS-预期EPS", ("eps_surprise",),
                    "blocked_pending_licensed_analyst_data"),
)


def iter_param_candidates() -> list[dict]:
    """展开全部模板 × 参数组合，返回候选规格（供搜索引擎用）。"""
    out = []
    for t in TEMPLATES:
        for params in t.params():
            out.append({"template": t, "params": params,
                        "factor_id": f"{t.tpl_id}__" + "_".join(f"{k}{v}" for k, v in sorted(params.items()))})
    return out
