"""A-Share Factor Library v2 — 扩容因子集（第二轮大批量新增）。

针对现有 76 因子缺失的维度补充，全部只用价格/量能数据（open/high/low/
close/volume/amount/turn），全部用 trailing 滚动窗口（无前视）：

  momentum_multi — 多周期动量 + 风险调整动量（长周期在A股偏反转，双信号）
  vol_est        — Parkinson/GK 高低价波动估计、波动率结构、尾部风险
  skew_kurt      — 短窗偏度/峰度
  trend_quality  — Aroon/CCI/Donchian/均线距离/长均线交叉
  vol_price      — OBV/CMF/A-D 量价斜率、量能集中度/突增、价量共振
  serial         — 收益自相关、Kaufman 效率比
  extreme        — 连跌、距52周高天数、回撤修复

因子符号约定：同库内统一"越高越好"方向；负 IC 因子在 walk-forward 组合里
由 select_and_weight 自动反转为正贡献。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from .base import BaseFactor

_ANN = np.sqrt(252)


def _clip(frame: pd.DataFrame, lo: float, hi: float) -> pd.DataFrame:
    """稳健截断：限制尾部爆炸值，保留主要信息。"""
    return frame.clip(lower=lo, upper=hi)


def _days_since_extreme(h: pd.DataFrame, window: int, use_max: bool) -> pd.DataFrame:
    """滚动窗口内"距极值出现天数"（向量化，容忍 NaN，含当日）。

    当日创新高 → 0；窗口内极值很久以前 → 接近 window-1。
    """
    out = pd.DataFrame(np.nan, index=h.index, columns=h.columns)
    if h.shape[0] < window:
        return out
    col_pos = np.arange(window, dtype=float)
    for stock in h.columns:
        col = h[stock].to_numpy(dtype=float)
        if col.size < window:
            continue
        w = sliding_window_view(col, window)  # (n-window+1, window)
        finite = np.isfinite(w)
        if use_max:
            extreme = np.where(finite, w, -np.inf).max(axis=1)
        else:
            extreme = np.where(finite, w, np.inf).min(axis=1)
        last = np.where(np.isclose(w, extreme[:, None]) & finite, col_pos, -1).max(axis=1)
        days = (window - 1) - last  # 0=当日创新高
        out.iloc[window - 1:, out.columns.get_loc(stock)] = days
    return out


# ═══════════ 动量扩展 ═══════════
class Mom20d(BaseFactor):
    name = "mom_20d"
    category = "momentum"
    description = "20日动量: 月频动量（区别于现有5/10日短动量）"

    def compute(self, prices, financials=None):
        c = prices['close']
        return c.pct_change(20).mul(100)


class Mom60d(BaseFactor):
    name = "mom_60d"
    category = "momentum"
    description = "60日动量: 季度动量，A股长周期动量偏反转"

    def compute(self, prices, financials=None):
        c = prices['close']
        return c.pct_change(60).mul(100)


class Mom120d(BaseFactor):
    name = "mom_120d"
    category = "momentum"
    description = "120日动量: 半年动量，A股通常反转"

    def compute(self, prices, financials=None):
        c = prices['close']
        return c.pct_change(120).mul(100)


class Mom20Skip5(BaseFactor):
    name = "mom_20_skip5"
    category = "momentum"
    description = "20日动量剔除最近5日: 避开短期反转污染"

    def compute(self, prices, financials=None):
        c = prices['close']
        return c.shift(5).pct_change(15).mul(100)


class Mom60Skip20(BaseFactor):
    name = "mom_60_skip20"
    category = "momentum"
    description = "60日动量剔除最近20日: 月度动量，经典Jegadeesh-Titman风格"

    def compute(self, prices, financials=None):
        c = prices['close']
        return c.shift(20).pct_change(40).mul(100)


class RiskAdjMom20d(BaseFactor):
    name = "risk_adj_mom_20d"
    category = "momentum"
    description = "20日风险调整动量: 收益/已实现波动，单位风险动量"

    def compute(self, prices, financials=None):
        r = prices['close'].pct_change()
        mu = r.rolling(20, min_periods=10).mean()
        sd = r.rolling(20, min_periods=10).std().replace(0, np.nan)
        return _clip(mu.div(sd) * _ANN, -6, 6)


class TSMom60d(BaseFactor):
    name = "ts_mom_60d"
    category = "momentum"
    description = "60日时间序列动量: 符号×幅度/波动（Momentum越持续越强）"

    def compute(self, prices, financials=None):
        r = prices['close'].pct_change()
        mom = prices['close'].pct_change(60)
        vol = r.rolling(60, min_periods=30).std().replace(0, np.nan)
        return mom.div(vol)


class PPO20_60(BaseFactor):
    name = "ppo_20_60"
    category = "momentum"
    description = "百分比价格振荡器: (EMA20-EMA60)/EMA60，双均线趋势强度"

    def compute(self, prices, financials=None):
        c = prices['close']
        ema20 = c.ewm(span=20, adjust=False).mean()
        ema60 = c.ewm(span=60, adjust=False).mean()
        return ema20.sub(ema60).div(ema60.replace(0, np.nan)).mul(100)


# ═══════════ 波动率扩展 ═══════════
class ParkinsonVol20d(BaseFactor):
    name = "parkinson_vol_20d"
    category = "low_vol"
    description = "Parkinson高低价波动率(反转): 用日内高低价，信息优于收盘价波动"

    def compute(self, prices, financials=None):
        h = prices['high']
        l = prices['low']
        rng = (np.log(h) - np.log(l)).pow(2)
        v = rng.rolling(20, min_periods=10).mean() / (4 * np.log(2))
        return -np.sqrt(v) * _ANN


class GKVol20d(BaseFactor):
    name = "gk_vol_20d"
    category = "low_vol"
    description = "Garman-Klass OHLC波动率(反转): 融合高低开收信息"

    def compute(self, prices, financials=None):
        o, h, l, c = (prices[k] for k in ('open', 'high', 'low', 'close'))
        co = np.log(c) - np.log(o)
        hl = np.log(h) - np.log(l)
        v = 0.5 * hl.pow(2) - (2 * np.log(2) - 1) * co.pow(2)
        v = v.rolling(20, min_periods=10).mean()
        return -np.sqrt(v.clip(lower=0)) * _ANN


class VolRatio5_20(BaseFactor):
    name = "vol_ratio_5_20"
    category = "low_vol"
    description = "5日/20日已实现波动比: 短期波动相对基线，波动率趋势"

    def compute(self, prices, financials=None):
        r = prices['close'].pct_change()
        v5 = r.rolling(5, min_periods=3).std()
        v20 = r.rolling(20, min_periods=10).std().replace(0, np.nan)
        return v5.div(v20)


class VolRatio20_60(BaseFactor):
    name = "vol_ratio_20_60"
    category = "low_vol"
    description = "20日/60日已实现波动比: 中期波动趋势"

    def compute(self, prices, financials=None):
        r = prices['close'].pct_change()
        v20 = r.rolling(20, min_periods=10).std()
        v60 = r.rolling(60, min_periods=30).std().replace(0, np.nan)
        return v20.div(v60)


class EWMAVol20d(BaseFactor):
    name = "ewma_vol_20d"
    category = "low_vol"
    description = "EWMA指数加权波动率(反转): 对近端波动更敏感"

    def compute(self, prices, financials=None):
        r = prices['close'].pct_change()
        ew = r.pow(2).ewm(span=20, adjust=False).mean()
        return -np.sqrt(ew) * _ANN


class VolOfVol60d(BaseFactor):
    name = "vol_of_vol_60d"
    category = "low_vol"
    description = "波动率的波动率: 波动率本身的不稳定性，风险因子"

    def compute(self, prices, financials=None):
        r = prices['close'].pct_change()
        v = r.rolling(20, min_periods=10).std()
        vv = v.rolling(60, min_periods=30).std()
        return -vv


class DownUpVolRatio60d(BaseFactor):
    name = "down_up_vol_ratio_60d"
    category = "low_vol"
    description = "下行/上行波动比: 下跌时波动是否更大，非对称风险"

    def compute(self, prices, financials=None):
        r = prices['close'].pct_change()
        down = r.where(r < 0).rolling(60, min_periods=15).std()
        up = r.where(r > 0).rolling(60, min_periods=15).std().replace(0, np.nan)
        return down.div(up)


class CVaR95_60d(BaseFactor):
    name = "cvar_95_60d"
    category = "low_vol"
    description = "60日95% CVaR(反转): 条件风险价值，左尾平均亏损"

    def compute(self, prices, financials=None):
        r = prices['close'].pct_change()
        out = pd.DataFrame(np.nan, index=r.index, columns=r.columns)
        for i in range(60, len(r)):
            w = r.iloc[i - 60:i]
            q = w.quantile(0.05)
            tail = w.where(w <= q)
            out.iloc[i] = -tail.mean()
        return out


class MaxDD20d(BaseFactor):
    name = "max_dd_20d"
    category = "low_vol"
    description = "20日最大回撤(反转): 短窗回撤风险"

    def compute(self, prices, financials=None):
        c = prices['close']
        roll_max = c.rolling(20, min_periods=10).max()
        return c.div(roll_max) - 1  # 负值；数值越高(=回撤越小)越好


class TailRatio60d(BaseFactor):
    name = "tail_ratio_60d"
    category = "low_vol"
    description = "右尾/左尾收益比: 大涨日相对大跌日的频率，非对称性"

    def compute(self, prices, financials=None):
        r = prices['close'].pct_change()
        up = (r > 0.03).rolling(60, min_periods=30).mean()
        down = (r < -0.03).rolling(60, min_periods=30).mean()
        return up.div(down.replace(0, np.nan))


# ═══════════ 偏度/峰度扩展 ═══════════
class Skew20d(BaseFactor):
    name = "skew_20d"
    category = "quality"
    description = "20日收益偏度: 短窗偏度（现有为60日）"

    def compute(self, prices, financials=None):
        r = prices['close'].pct_change()
        return r.rolling(20, min_periods=10).skew()


class Kurt20d(BaseFactor):
    name = "kurt_20d"
    category = "quality"
    description = "20日收益峰度: 短窗峰度，尾部风险代理"

    def compute(self, prices, financials=None):
        r = prices['close'].pct_change()
        return r.rolling(20, min_periods=10).kurt()


# ═══════════ 趋势质量 ═══════════
class AroonUp25d(BaseFactor):
    name = "aroon_up_25d"
    category = "momentum"
    description = "Aroon上涨: 距25日高点天数，趋势强度"

    def compute(self, prices, financials=None):
        days = _days_since_extreme(prices['high'], 25, use_max=True)
        return _clip(100 * (25 - days) / 25, 0, 100)


class AroonDown25d(BaseFactor):
    name = "aroon_down_25d"
    category = "reversal"
    description = "Aroon下跌(反转): 距25日低点天数，下行趋势强度"

    def compute(self, prices, financials=None):
        days = _days_since_extreme(prices['low'], 25, use_max=False)
        return _clip(100 * (25 - days) / 25, 0, 100)


class CCI20d(BaseFactor):
    name = "cci_20d"
    category = "momentum"
    description = "顺势指标: (TP-MA)/0.015×MAD，超买超卖+趋势"

    def compute(self, prices, financials=None):
        h, l, c = prices['high'], prices['low'], prices['close']
        tp = (h + l + c) / 3
        ma = tp.rolling(20, min_periods=10).mean()
        md = tp.sub(ma).abs().rolling(20, min_periods=10).mean().replace(0, np.nan)
        return _clip(tp.sub(ma).div(md * 0.015), -300, 300)


class DonchianPos20d(BaseFactor):
    name = "donchian_pos_20d"
    category = "momentum"
    description = "Donchian通道位置: (C-LL)/(HH-LL)，趋势位置"

    def compute(self, prices, financials=None):
        h, l, c = prices['high'], prices['low'], prices['close']
        hh = h.rolling(20, min_periods=10).max()
        ll = l.rolling(20, min_periods=10).min()
        return c.sub(ll).div(hh.sub(ll).replace(0, np.nan))


class DistFromMA10d(BaseFactor):
    name = "dist_from_ma10"
    category = "momentum"
    description = "距MA10距离: 短均线乖离"

    def compute(self, prices, financials=None):
        c = prices['close']
        ma = c.rolling(10, min_periods=5).mean()
        return c.div(ma) - 1


class DistFromMA30d(BaseFactor):
    name = "dist_from_ma30"
    category = "momentum"
    description = "距MA30距离: 中均线乖离"

    def compute(self, prices, financials=None):
        c = prices['close']
        ma = c.rolling(30, min_periods=15).mean()
        return c.div(ma) - 1


class MACross20_60(BaseFactor):
    name = "ma_cross_20_60"
    category = "momentum"
    description = "20/60均线交叉: 长周期趋势强度 (MA20/MA60-1)"

    def compute(self, prices, financials=None):
        c = prices['close']
        ma20 = c.rolling(20, min_periods=10).mean()
        ma60 = c.rolling(60, min_periods=30).mean()
        return ma20.div(ma60) - 1


# ═══════════ 量价创新 ═══════════
class OBVSlope20d(BaseFactor):
    name = "obv_slope_20d"
    category = "money_flow"
    description = "OBV 20日斜率: 量能累计方向（标准化）"

    def compute(self, prices, financials=None):
        c = prices['close']
        v = prices['volume']
        signed = v.mul(np.sign(c.pct_change().fillna(0)))
        obv = signed.cumsum()
        slope = obv.diff(20) / obv.rolling(60, min_periods=20).std().replace(0, np.nan)
        return slope


class CMF20d(BaseFactor):
    name = "cmf_20d"
    category = "money_flow"
    description = "Chaikin资金流: 收盘位置×量能，20日资金流向"

    def compute(self, prices, financials=None):
        h, l, c, v = (prices[k] for k in ('high', 'low', 'close', 'volume'))
        mfm = (c - l) - (h - c)
        mfv = mfm.div(h - l).mul(v)
        return mfv.rolling(20, min_periods=10).sum().div(
            v.rolling(20, min_periods=10).sum().replace(0, np.nan))


class ADSlope20d(BaseFactor):
    name = "ad_slope_20d"
    category = "money_flow"
    description = "A/D线20日斜率: 累积量价分配线方向"

    def compute(self, prices, financials=None):
        h, l, c, v = (prices[k] for k in ('high', 'low', 'close', 'volume'))
        rng = (h - l).replace(0, np.nan)
        clv = ((c - l) - (h - c)).div(rng)
        ad = clv.mul(v).fillna(0).cumsum()
        return ad.diff(20) / ad.rolling(60, min_periods=20).std().replace(0, np.nan)


class VolumeConc20d(BaseFactor):
    name = "volume_conc_20d"
    category = "liquidity"
    description = "成交量集中度: 20日Herfindahl，量能是否集中某几日"

    def compute(self, prices, financials=None):
        v = prices['volume']
        w = v.rolling(20, min_periods=10)
        share = v / w.sum().replace(0, np.nan)
        return (share.pow(2)).rolling(20, min_periods=10).mean()


class VolumeSurgeRatio5d(BaseFactor):
    name = "volume_surge_ratio_5d"
    category = "liquidity"
    description = "近5日量能/前20日量能: 量能突增"

    def compute(self, prices, financials=None):
        v = prices['volume']
        v5 = v.rolling(5, min_periods=3).mean()
        v20 = v.shift(5).rolling(20, min_periods=10).mean().replace(0, np.nan)
        return v5.div(v20)


class TurnoverPct20d(BaseFactor):
    name = "turnover_pct_20d"
    category = "liquidity"
    description = "换手率20日分位: 近期换手相对自身历史"

    def compute(self, prices, financials=None):
        t = prices.get('turn')
        if t is None:
            return pd.DataFrame(index=prices['close'].index,
                                columns=prices['close'].columns)
        return t.rolling(252, min_periods=60).rank(pct=True)


class VolMomentum20d(BaseFactor):
    name = "vol_momentum_20d"
    category = "liquidity"
    description = "成交额20日动量: 资金活跃度趋势"

    def compute(self, prices, financials=None):
        a = prices.get('amount')
        if a is None:
            a = prices['close'].mul(prices['volume'])
        return _clip(a.pct_change(20).mul(100), -30, 30)


class PriceVolConfirm20d(BaseFactor):
    name = "price_vol_confirm_20d"
    category = "money_flow"
    description = "价量共振: 20日价格动量与量能动量的交互（趋势确认）"

    def compute(self, prices, financials=None):
        c = prices['close']
        v = prices['volume']
        pm = c.pct_change(20)
        vm = v.pct_change(20)
        return pm.mul(vm)


class VolumePressure5d(BaseFactor):
    name = "volume_pressure_5d"
    category = "money_flow"
    description = "量压: 收盘位置×量能比率，近5日资金压力"

    def compute(self, prices, financials=None):
        h, l, c, v = (prices[k] for k in ('high', 'low', 'close', 'volume'))
        pos = (c - l).div(h - l).replace(0, np.nan)
        vratio = v.div(v.rolling(20, min_periods=10).mean().replace(0, np.nan))
        return pos.mul(vratio)


# ═══════════ 序列结构 ═══════════
class Autocorr1d(BaseFactor):
    name = "autocorr_1d"
    category = "quality"
    description = "1日收益自相关: 短程序列依赖（动量/反转）"

    def compute(self, prices, financials=None):
        r = prices['close'].pct_change()
        r2 = r.shift(1)
        cov = r.mul(r2).rolling(20, min_periods=10).mean()
        var = r.pow(2).rolling(20, min_periods=10).mean().replace(0, np.nan)
        return cov.div(var)


class Autocorr5d(BaseFactor):
    name = "autocorr_5d"
    category = "quality"
    description = "5日滞后收益自相关: 中程序列依赖"

    def compute(self, prices, financials=None):
        r = prices['close'].pct_change()
        r2 = r.shift(5)
        cov = r.mul(r2).rolling(20, min_periods=10).mean()
        var = r.pow(2).rolling(20, min_periods=10).mean().replace(0, np.nan)
        return cov.div(var)


class EfficiencyRatio20d(BaseFactor):
    name = "efficiency_ratio_20d"
    category = "momentum"
    description = "Kaufman效率比: 净变动/路径长度，趋势vs震荡"

    def compute(self, prices, financials=None):
        c = prices['close']
        net = c.diff(20).abs()
        path = c.diff().abs().rolling(20, min_periods=10).sum()
        return net.div(path.replace(0, np.nan))


# ═══════════ 事件/极端 ═══════════
class DownStreak10d(BaseFactor):
    name = "down_streak_10d"
    category = "reversal"
    description = "10日内连跌天数: 极端下跌后的反转机会"

    def compute(self, prices, financials=None):
        r = prices['close'].pct_change()
        neg = (r < 0).astype(float)
        streak = neg * (neg.cumsum() - neg.cumsum().where(neg == 0).ffill().fillna(0))
        return streak


class DaysSince52wHigh(BaseFactor):
    name = "days_since_52w_high"
    category = "technical"
    description = "距52周高点天数: 新高后动量延续 vs 久未创新高的均值回归"

    def compute(self, prices, financials=None):
        return _days_since_extreme(prices['high'], 250, use_max=True)


class RecoveryDays60d(BaseFactor):
    name = "recovery_days_60d"
    category = "quality"
    description = "60日回撤修复能力: 从高点回撤后恢复的天数（越快越强）"

    def compute(self, prices, financials=None):
        c = prices['close']
        dd = c.div(c.rolling(60, min_periods=30).max()) - 1
        # 回撤深度作为恢复能力代理：浅回撤→好
        return dd


# ═══════════ 自研因子 Wave1（2026-08-06，补齐 A 股文献已验证异象） ═══════════
class MaxRet20d(BaseFactor):
    name = "max_ret_20d"
    category = "reversal"
    description = "MAX效应: 过去20日最大单日涨幅（博彩偏好→未来跑输，负IC）"

    def compute(self, prices, financials=None):
        c = prices['close']
        r = c.pct_change()
        return r.rolling(20, min_periods=10).max().mul(100)


class IdioSkew60d(BaseFactor):
    name = "idio_skew_60d"
    category = "skew_kurt"
    description = "特质偏度: 市场回归残差60日偏度（高特质偏度=博彩偏好，负IC）"

    def compute(self, prices, financials=None):
        c = prices['close']
        r = c.pct_change()
        mkt = r.mean(axis=1)
        win = 60
        # 点时安全：beta 用 trailing 120 日滚动估计（不用全样本 polyfit——
        # 全样本会把未来市场关系泄漏进当期残差，属轻微前视）。
        #   beta_t = Cov_120(r, mkt) / Var_120(mkt)
        mm = mkt.rolling(120, min_periods=60).mean()
        mv = mkt.rolling(120, min_periods=60).var().replace(0, np.nan)
        ym = r.rolling(120, min_periods=60).mean()
        yxm = r.mul(mkt, axis=0).rolling(120, min_periods=60).mean()
        beta = yxm.sub(ym.mul(mm, axis=0)).div(mv, axis=0)
        resid = r.sub(beta.mul(mkt, axis=0))
        out = resid.rolling(win, min_periods=win // 2).skew()
        return out


class RVFrac20d(BaseFactor):
    name = "rv_frac_20d"
    category = "reversal"
    description = "波动加速度: 近5日波动占20日波动份额（近期放量波动→均值回归，负IC）"

    def compute(self, prices, financials=None):
        c = prices['close']
        r = c.pct_change()
        rv20 = r.rolling(20, min_periods=10).std()
        rv5 = r.rolling(5, min_periods=3).std()
        return rv5.div(rv20.replace(0, np.nan)).mul(100)


class OvernightVolShare20d(BaseFactor):
    name = "overnight_vol_share_20d"
    category = "serial"
    description = "隔夜波动占比: 20日总方差中隔夜(close→open)方差份额（机构/散户信息含量差异）"

    def compute(self, prices, financials=None):
        c = prices['close']
        o = prices.get('open', c)
        on = o.pct_change()          # open[d]/close[d-1]-1
        intra = c.div(o) - 1.0       # close[d]/open[d]-1
        var_on = on.rolling(20, min_periods=10).var()
        var_in = intra.rolling(20, min_periods=10).var()
        denom = var_on.add(var_in).replace(0, np.nan)
        return var_on.div(denom).mul(100)


class LastDayShare5d(BaseFactor):
    name = "last_day_share_5d"
    category = "reversal"
    description = "末日起额占比: 最近一日收益占5日累计收益份额（涨幅集中在末端→更易反转）"

    def compute(self, prices, financials=None):
        c = prices['close']
        r = c.pct_change()
        cum5 = r.rolling(5, min_periods=3).sum()
        denom = cum5.replace(0, np.nan).abs()
        return r.abs().div(denom).mul(100)


# ═══════════ Wave2：基本面因子（同花顺 THS 季度财报，点时映射 + 日度 ffill） ═══════════
# 依赖 panel 中的点时键：roe/gm/nm/beps/ocfps/np_yoy/rev_yoy/ded_np_yoy/debt 等
# （由 collect_financials.py 注入）。数据天然低频 → 因子缓慢变化。

class BookToPrice(BaseFactor):
    name = "bp_real"
    category = "value"
    description = "真实市净率倒数: 每股净资产/价格（THS 点时每股净资产），A股最稳定的价值异象"

    def compute(self, prices, financials=None):
        beps = prices.get("beps")
        c = prices["close"]
        if beps is None:
            return None
        bp = beps.div(c)
        return bp.where(bp > 0)


class CashYield(BaseFactor):
    name = "cash_yield"
    category = "value"
    description = "每股经营现金流/价格: 现金收益率（价值+现金流质量）"

    def compute(self, prices, financials=None):
        ocf = prices.get("ocfps")
        c = prices["close"]
        if ocf is None:
            return None
        cy = ocf.div(c)
        return cy.where(cy > 0).mul(100)


class RoeLevel(BaseFactor):
    name = "roe_level"
    category = "quality"
    description = "净资产收益率水平: THS 点时 ROE（盈利能力质量）"

    def compute(self, prices, financials=None):
        roe = prices.get("roe")
        return roe if roe is not None else None


class GrossMargin(BaseFactor):
    name = "gm_level"
    category = "quality"
    description = "销售毛利率水平: 商业模式护城河代理"

    def compute(self, prices, financials=None):
        gm = prices.get("gm")
        return gm if gm is not None else None


class NetMargin(BaseFactor):
    name = "nm_level"
    category = "quality"
    description = "销售净利率水平: 净盈利能力"

    def compute(self, prices, financials=None):
        nm = prices.get("nm")
        return nm if nm is not None else None


class ProfitGrowth(BaseFactor):
    name = "np_growth"
    category = "growth"
    description = "净利润同比增长率（THS，累计口径，裁剪极端值）"

    def compute(self, prices, financials=None):
        g = prices.get("np_yoy")
        if g is None:
            return None
        return g.clip(-500, 2000)


class RevGrowth(BaseFactor):
    name = "rev_growth"
    category = "growth"
    description = "营业总收入同比增长率（THS，裁剪极端值）"

    def compute(self, prices, financials=None):
        g = prices.get("rev_yoy")
        if g is None:
            return None
        return g.clip(-500, 2000)


class DedProfitGrowth(BaseFactor):
    name = "ded_np_growth"
    category = "growth"
    description = "扣非净利润同比增长率（THS，裁剪极端值）——更纯的经营增长"

    def compute(self, prices, financials=None):
        g = prices.get("ded_np_yoy")
        if g is None:
            return None
        return g.clip(-500, 2000)


class RoeMomentum(BaseFactor):
    name = "roe_chg_1y"
    category = "quality"
    description = "ROE 一年变化（同比 4 个报告期）：盈利趋势"

    def compute(self, prices, financials=None):
        roe = prices.get("roe")
        if roe is None:
            return None
        return roe - roe.shift(252)


class GmMomentum(BaseFactor):
    name = "gm_chg_1y"
    category = "quality"
    description = "毛利率一年变化：竞争格局趋势"

    def compute(self, prices, financials=None):
        gm = prices.get("gm")
        if gm is None:
            return None
        return gm - gm.shift(252)


class LowDebt(BaseFactor):
    name = "low_debt"
    category = "quality"
    description = "低负债率（-资产负债率）：财务稳健性"

    def compute(self, prices, financials=None):
        d = prices.get("debt")
        if d is None:
            return None
        return d.mul(-1)


class RetainedYield(BaseFactor):
    name = "retained_yield"
    category = "value"
    description = "每股未分配利润/价格: 累计留存盈利收益率（深度价值）"

    def compute(self, prices, financials=None):
        rp = prices.get("retained_ps")
        c = prices["close"]
        if rp is None:
            return None
        ry = rp.div(c)
        return ry.where(ry > 0).mul(100)


# ═══════════ Wave3：THS 点时字段深度开采（2026-08-07） ═══════════
# 针对 panel 中从未被使用的点时字段设计：eps(每股收益, 现有 ep_proxy 只是 -60日动量代理,
# 非真实盈利收益率)、capres_ps(每股资本公积)、roe_diluted(摊薄ROE)、amount(成交额结构)。
# 全部只用现有 panel 字段 → 无新数据下载；全部 trailing 窗口 → 无前视。

class RealEP(BaseFactor):
    name = "ep_real"
    category = "value"
    description = "真实盈利收益率: THS 点时每股收益/价格（现有 ep_proxy 是 -60日动量代理，非真 E/P）"

    def compute(self, prices, financials=None):
        eps = prices.get("eps")
        c = prices["close"]
        if eps is None:
            return None
        ey = eps.div(c)
        return ey.clip(-1, 3) * 100


class CapResYield(BaseFactor):
    name = "capres_yield"
    category = "value"
    description = "资本公积收益率: 每股资本公积/价格（资本实力，转股潜力，A股壳价值代理）"

    def compute(self, prices, financials=None):
        cr = prices.get("capres_ps")
        c = prices["close"]
        if cr is None:
            return None
        return cr.div(c).clip(0, 5) * 100


class RoeDilutedGap(BaseFactor):
    name = "roe_diluted_gap"
    category = "quality"
    description = "摊薄ROE差距: 基本ROE-摊薄ROE（差距大=稀释严重，反映增发/摊薄风险，越低越好）"

    def compute(self, prices, financials=None):
        roe = prices.get("roe")
        roed = prices.get("roe_diluted")
        if roe is None or roed is None:
            return None
        gap = roe - roed
        return -gap


class AccrualYield(BaseFactor):
    name = "accrual_yield"
    category = "quality"
    description = "Sloan应计收益率: (每股收益-每股经营现金流)/价格（高应计=盈利质量差，负向）"

    def compute(self, prices, financials=None):
        eps = prices.get("eps")
        ocf = prices.get("ocfps")
        c = prices["close"]
        if eps is None or ocf is None:
            return None
        accrual = eps - ocf
        return -accrual.div(c).clip(-3, 3) * 100


class OcfToEps(BaseFactor):
    name = "ocf_to_eps"
    category = "quality"
    description = "经营现金流/每股收益: 盈余现金含量（>1 表示现金流优于账面利润，质量好）"

    def compute(self, prices, financials=None):
        eps = prices.get("eps")
        ocf = prices.get("ocfps")
        if eps is None or ocf is None:
            return None
        ratio = ocf.div(eps.replace(0, np.nan))
        return ratio.clip(0, 10)


class OcfYieldChg1y(BaseFactor):
    name = "ocf_yield_chg_1y"
    category = "quality"
    description = "现金流收益率一年变化: Δ(经营现金流/价格)，现金流质量改善趋势"

    def compute(self, prices, financials=None):
        ocf = prices.get("ocfps")
        c = prices["close"]
        if ocf is None:
            return None
        cy = ocf.div(c)
        return cy - cy.shift(252)


class AmountConc20d(BaseFactor):
    name = "amount_conc_20d"
    category = "money_flow"
    description = "成交额集中度(20日): 头部5日成交额/20日总额，资金脉冲vs持续流（结构性资金流）"

    def compute(self, prices, financials=None):
        a = prices["amount"]
        total = a.rolling(20, min_periods=10).sum()
        # 任意连续5日成交额峰值占20日总额比例（资金脉冲集中度，向量化）
        peak5 = a.rolling(5, min_periods=5).sum().rolling(20, min_periods=10).max()
        return peak5.div(total.replace(0, np.nan))


class TurnSkew20d(BaseFactor):
    name = "turn_skew_20d"
    category = "money_flow"
    description = "换手率偏度(20日): 高=交易活跃集中在少数几日（炒作），负向"

    def compute(self, prices, financials=None):
        t = prices["turn"]
        return -t.rolling(20, min_periods=10).skew()


class RealizedSkew10d(BaseFactor):
    name = "realized_skew_10d"
    category = "momentum"
    description = "10日实现偏度: 短窗偏度（区别于20/60日偏度），彩票偏好"

    def compute(self, prices, financials=None):
        r = prices["close"].pct_change()
        return r.rolling(10, min_periods=6).skew()


class EfficiencyRatio60d(BaseFactor):
    name = "efficiency_ratio_60d"
    category = "momentum"
    description = "60日Kaufman效率比: 趋势效率长窗（区别于20日），季度级趋势持续性"

    def compute(self, prices, financials=None):
        c = prices["close"]
        net = c.diff(60).abs()
        path = c.diff().abs().rolling(60, min_periods=30).sum()
        return net.div(path.replace(0, np.nan))


class HighLowDistanceRatio20d(BaseFactor):
    name = "hl_dist_ratio_20d"
    category = "low_vol"
    description = "高低点距离比: (high-low)/close 的20日均值——日内振幅相对价格，结构性波动"

    def compute(self, prices, financials=None):
        h = prices["high"]
        l = prices["low"]
        c = prices["close"]
        rng = (h - l).div(c.replace(0, np.nan))
        return -rng.rolling(20, min_periods=10).mean()


WAVE3_NEW_FACTOR_CLASSES = [
    RealEP, CapResYield, AccrualYield, OcfToEps, AmountConc20d,
]
# 生产入册：只保留与现有因子低相关的正交因子（ocf_to_eps/amount_conc_20d/accrual_yield）；
# ep_real/capres_yield 与 amplitude_vol/ey_proxy 相关性 0.42-0.47，进组合后稀释权重拉低年化（v137 33.06 < v135c 34.01）。
# 保留在 WAVE3_NEW_FACTOR_CLASSES 供研究，不参与生产选择。

# ═══════════ 组合注册 ═══════════
NEW_FACTOR_CLASSES = [
    Mom20d, Mom60d, Mom120d, Mom20Skip5, Mom60Skip20,
    RiskAdjMom20d, TSMom60d, PPO20_60,
    ParkinsonVol20d, GKVol20d, VolRatio5_20, VolRatio20_60,
    EWMAVol20d, VolOfVol60d, DownUpVolRatio60d, CVaR95_60d,
    MaxDD20d, TailRatio60d,
    Skew20d, Kurt20d,
    AroonUp25d, AroonDown25d, CCI20d, DonchianPos20d,
    DistFromMA10d, DistFromMA30d, MACross20_60,
    OBVSlope20d, CMF20d, ADSlope20d, VolumeConc20d,
    VolumeSurgeRatio5d, TurnoverPct20d, VolMomentum20d,
    PriceVolConfirm20d, VolumePressure5d,
    Autocorr1d, Autocorr5d, EfficiencyRatio20d,
    DownStreak10d, DaysSince52wHigh, RecoveryDays60d,
    MaxRet20d, IdioSkew60d, RVFrac20d, OvernightVolShare20d, LastDayShare5d,
    # Wave2 基本面（同花顺 THS）
    BookToPrice, CashYield, RoeLevel, GrossMargin, NetMargin,
    ProfitGrowth, RevGrowth, DedProfitGrowth,
    RoeMomentum, GmMomentum, LowDebt, RetainedYield,
    # Wave3 THS 点时字段深度开采（2026-08-07，实证通过 IC 验证后入册）
    AccrualYield, OcfToEps, AmountConc20d,
]
