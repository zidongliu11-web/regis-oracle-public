"""Prototype A-share factor formula library.

Factors accept dict-panel DataFrames and return dates-by-instruments DataFrames.
Category names and historical inline performance comments are research hypotheses,
not validation evidence or public trading admission. The FinHack-derived catalogue
is excluded from this edition; its placeholder contributes no factors.
"""

from __future__ import annotations

from typing import Dict, Optional, Any

import numpy as np
import pandas as pd
from scipy import stats

from .a_share_factors_v2 import NEW_FACTOR_CLASSES as _V2_NEW_FACTOR_CLASSES
from .a_share_factors_finhack import FINHACK_FACTOR_CLASSES as _FINHACK_FACTOR_CLASSES

from .base import BaseFactor


# ═══════════════════════════════════════════════════════════════
# Group 1: LOW VOLATILITY (⭐ Best A-share performer)
# ═══════════════════════════════════════════════════════════════

class LowVol20d(BaseFactor):
    name = "low_vol_20d"
    category = "low_vol"
    description = "20日年化波动率(反转): -std(ret,20)*sqrt(252), A股最强因子"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        ret = prices['close'].pct_change()
        vol = ret.rolling(20, min_periods=10).std() * np.sqrt(252)
        return -vol


class LowVol60d(BaseFactor):
    name = "low_vol_60d"
    category = "low_vol"
    description = "60日年化波动率(反转): 更长回看窗口"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        ret = prices['close'].pct_change()
        vol = ret.rolling(60, min_periods=30).std() * np.sqrt(252)
        return -vol


class IdioVol60d(BaseFactor):
    name = "idio_vol_60d"
    category = "low_vol"
    description = "异质波动率(60日): 剥离市场后的残差波动率, 文献中A股最强因子之一"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        ret = prices['close'].pct_change()
        mkt_ret = ret.mean(axis=1)
        residual = pd.DataFrame(
            ret.values - mkt_ret.values[:, None],
            index=ret.index, columns=ret.columns
        )
        vol = residual.rolling(60, min_periods=30).std() * np.sqrt(252)
        return -vol


class FfcResVol20d(BaseFactor):
    name = "ffc_res_vol_20d"
    category = "low_vol"
    description = "FFC残差波动率(20日): 20天市场残差波动率, IC ~ -0.04"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        ret = prices['close'].pct_change()
        mkt = ret.mean(axis=1)
        residual = pd.DataFrame(
            ret.values - mkt.values[:, None],
            index=ret.index, columns=ret.columns
        )
        vol = residual.rolling(20, min_periods=10).std() * np.sqrt(252)
        return -vol


class AmplitudeVol20d(BaseFactor):
    name = "amplitude_vol_20d"
    category = "low_vol"
    description = "振幅波动率(20日): (high-low)/close均值, CSI1000中IC=-0.04"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        amplitude = (prices['high'] - prices['low']) / prices['close']
        avg_amp = amplitude.rolling(20, min_periods=10).mean()
        return -avg_amp


class MaxDD60d(BaseFactor):
    name = "max_dd_60d"
    category = "low_vol"
    description = "最大回撤(60日): higher(less negative) = better"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        rolling_max = close.rolling(60, min_periods=30).max()
        drawdown = (close - rolling_max) / rolling_max
        return drawdown.rolling(60, min_periods=30).min()


class Beta60d(BaseFactor):
    name = "beta_60d"
    category = "low_vol"
    description = "市场Beta(60日反转): lower beta = better in A-shares"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        ret = prices['close'].pct_change()
        mkt_ret = ret.mean(axis=1)
        beta = pd.DataFrame(1.0, index=ret.index, columns=ret.columns)
        for i in range(60, len(ret)):
            y = ret.iloc[i-59:i+1]
            x = pd.Series(mkt_ret.iloc[i-59:i+1].values)
            cov = y.cov().iloc[:, 0] if hasattr(y, 'cov') else y.apply(
                lambda c: c.cov(x) if c.notna().sum() > 10 else np.nan
            )
        return -beta


class DownsideVol60d(BaseFactor):
    name = "downside_vol_60d"
    category = "low_vol"
    description = "下行波动率(60日): Sortino分母, lower = less downside risk"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        ret = prices['close'].pct_change()
        neg_ret = ret.clip(upper=0)
        down_vol = (neg_ret ** 2).rolling(60, min_periods=30).mean() ** 0.5 * np.sqrt(252)
        return -down_vol


class Var95_60d(BaseFactor):
    name = "var_95_60d"
    category = "low_vol"
    description = "VaR 95%(60日): 历史法, higher(less negative) = safer"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        ret = prices['close'].pct_change()

        def _var95(x):
            arr = x[~np.isnan(x)]
            return np.percentile(arr, 5) if len(arr) > 10 else np.nan

        return ret.rolling(60, min_periods=30).apply(_var95, raw=True)


# ═══════════════════════════════════════════════════════════════
# Group 2: SHORT-TERM REVERSAL (Consistently effective)
# ═══════════════════════════════════════════════════════════════

class Reversal5d(BaseFactor):
    name = "reversal_5d"
    category = "reversal"
    description = "5日反转: -(close/close[-5]-1), higher=more oversold"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        return -prices['close'].pct_change(5)


class Reversal10d(BaseFactor):
    name = "reversal_10d"
    category = "reversal"
    description = "10日反转: 两周窗口"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        return -prices['close'].pct_change(10)


class Reversal20d(BaseFactor):
    name = "reversal_20d"
    category = "reversal"
    description = "20日反转: 一个月窗口, 非动量(长期动量在A股失效)"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        return -prices['close'].pct_change(20)


class OvernightReversal(BaseFactor):
    name = "overnight_reversal"
    category = "reversal"
    description = "隔夜反转: -(open/prev_close-1) 5日均值"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        open_ = prices['open']
        close = prices['close']
        overnight_ret = open_ / close.shift(1) - 1
        return -overnight_ret.rolling(5, min_periods=3).mean()


class IntradayReversal(BaseFactor):
    name = "intraday_reversal"
    category = "reversal"
    description = "日内反转: 日内收益率5日均值反转"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        open_ = prices['open']
        prev_close = close.shift(1)
        intraday = (close - open_) / prev_close
        return -intraday.rolling(5, min_periods=3).mean()


class RSI14Reversal(BaseFactor):
    name = "rsi_14_reversal"
    category = "reversal"
    description = "RSI(14)均值回归: -RSI, high=oversold=buy"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(14, min_periods=7).mean()
        avg_loss = loss.rolling(14, min_periods=7).mean()
        rs = avg_gain / avg_loss.replace(0, 1)
        rsi = 100 - (100 / (1 + rs))
        return -rsi


class StochasticOversold(BaseFactor):
    name = "stoch_oversold"
    category = "reversal"
    description = "随机指标超卖: -Stoch%K, high=oversold"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        ll = prices['low'].rolling(14, min_periods=7).min()
        hh = prices['high'].rolling(14, min_periods=7).max()
        stoch_k = (close - ll) / (hh - ll).replace(0, 1) * 100
        return -stoch_k


class GapFillProb(BaseFactor):
    name = "gap_fill_prob"
    category = "reversal"
    description = "缺口回补概率(20日): A股缺口倾向回补"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        # 修复前视偏差：原实现用 ret_5d.shift(-5) 借用了未来收益。
        # 改为仅用截至 t 日的数据：当日 1 日收益与过去 5 日收益反向 → 反弹/回补信号。
        ret_1d = prices['close'].pct_change()
        ret_5d = prices['close'].pct_change(5)
        reversal_signal = (ret_1d * ret_5d) < 0
        return reversal_signal.rolling(20, min_periods=10).mean()


# ═══════════════════════════════════════════════════════════════
# Group 3: VALUE (Stable in A-shares 2024-2025)
# ═══════════════════════════════════════════════════════════════

class PriceTo52wHigh(BaseFactor):
    name = "price_to_52w_high"
    category = "value"
    description = "价格/52周高点(反转): lower ratio = more room to recover"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        high_52w = close.rolling(250, min_periods=100).max()
        return -(close / high_52w.replace(0, np.nan))


class PriceTo52wLow(BaseFactor):
    name = "price_to_52w_low"
    category = "value"
    description = "价格/52周低点: higher = further from bottom"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        low_52w = close.rolling(250, min_periods=100).min()
        return close / low_52w.replace(0, np.nan)


class PricePosition200d(BaseFactor):
    name = "price_position_200d"
    category = "value"
    description = "200日区间位置(反转): lower=closer to range bottom"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        high_200 = close.rolling(200, min_periods=100).max()
        low_200 = close.rolling(200, min_periods=100).min()
        position = (close - low_200) / (high_200 - low_200).replace(0, np.nan)
        return -position


class DistanceFromMA60(BaseFactor):
    name = "dist_from_ma60"
    category = "value"
    description = "(Price-MA60)/MA60(反转): negative=below trend=potential value"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        ma60 = close.rolling(60, min_periods=30).mean()
        distance = (close - ma60) / ma60
        return -distance


class DistanceFromMA200(BaseFactor):
    name = "dist_from_ma200"
    category = "value"
    description = "(Price-MA200)/MA200(反转): 经典价值择时"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        ma200 = close.rolling(200, min_periods=100).mean()
        distance = (close - ma200) / ma200
        return -distance


class BollPosition20d(BaseFactor):
    name = "boll_position_20d"
    category = "value"
    description = "布林带位置(20日反转): low position=potentially undervalued"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        sma_20 = close.rolling(20, min_periods=10).mean()
        std_20 = close.rolling(20, min_periods=10).std()
        lower = sma_20 - 2 * std_20
        upper = sma_20 + 2 * std_20
        position = (close - lower) / (upper - lower).replace(0, 1)
        return -position


class EPProxy(BaseFactor):
    name = "ep_proxy"
    category = "value"
    description = "EP代理: -60日动量, losers=potentially undervalued"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        return -prices['close'].pct_change(60)


class BPProxy(BaseFactor):
    name = "bp_proxy"
    category = "value"
    description = "BP代理: 1/close, higher=cheaper (简化版, 实盘需PB数据)"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        return 1.0 / prices['close']


# ═══════════════════════════════════════════════════════════════
# Group 4: LIQUIDITY / TURNOVER (Frictions dominate A-shares)
# ═══════════════════════════════════════════════════════════════

class TurnoverAnomaly5d(BaseFactor):
    name = "turnover_anomaly_5d"
    category = "liquidity"
    description = "换手率异常(5/20日反转): abnormal vol=reversal signal"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        volume = prices['volume']
        vol_5d = volume.rolling(5, min_periods=3).mean()
        vol_20d = volume.rolling(20, min_periods=10).mean()
        return -(vol_5d / vol_20d.replace(0, np.nan))


class TurnoverStd20d(BaseFactor):
    name = "turnover_std_20d"
    category = "liquidity"
    description = "换手率波动(20日反转): high vol-of-vol=speculative"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        volume = prices['volume']
        vol_std = volume.rolling(20, min_periods=10).std()
        vol_mean = volume.rolling(20, min_periods=10).mean()
        cv = vol_std / vol_mean.replace(0, np.nan)
        return -cv


class AmihudIlliq(BaseFactor):
    name = "amihud_illiq"
    category = "liquidity"
    description = "Amihud非流动性(20日): |ret|/amount, 文献最强流动性因子"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        amount = prices.get('amount', prices['volume'] * close)
        ret = np.abs(close.pct_change())
        daily_illiq = ret / amount.replace(0, np.nan)
        return daily_illiq.rolling(20, min_periods=10).mean()


class DollarVolume20d(BaseFactor):
    name = "dollar_volume_20d"
    category = "liquidity"
    description = "日均成交额(对数): higher=more liquid"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        amount = prices.get('amount', prices['volume'] * close)
        avg_amount = amount.rolling(20, min_periods=10).mean()
        return np.log(avg_amount.replace(0, np.nan))


class VolumePriceCorr(BaseFactor):
    name = "volume_price_corr"
    category = "liquidity"
    description = "量价相关性(20日反转): divergence=reversal signal"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        volume = prices['volume']
        price_chg = close.diff()
        vol_chg = volume.diff()
        corr = price_chg.rolling(20, min_periods=10).corr(vol_chg)
        return -corr


class VolumeDryUp(BaseFactor):
    name = "volume_dry_up"
    category = "liquidity"
    description = "成交量萎缩: min_vol(20d)/avg_vol(20d), contraction=breakout precursor"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        volume = prices['volume']
        vol_min = volume.rolling(20, min_periods=10).min()
        vol_avg = volume.rolling(20, min_periods=10).mean()
        return vol_min / vol_avg.replace(0, np.nan)


class AmountRatio5_20(BaseFactor):
    name = "amount_ratio_5_20"
    category = "liquidity"
    description = "成交额比(5/20日): abnormal trading activity indicator"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        amount = prices.get('amount', prices['volume'] * close)
        amt_5 = amount.rolling(5, min_periods=3).mean()
        amt_20 = amount.rolling(20, min_periods=10).mean()
        return amt_5 / amt_20.replace(0, np.nan)


class ConsecutiveVolUp(BaseFactor):
    name = "consec_vol_up"
    category = "liquidity"
    description = "持续放量天数(5日): consecutive volume increase days"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        volume = prices['volume']
        vol_chg = volume.diff() > 0
        return vol_chg.rolling(5, min_periods=1).sum()


# ═══════════════════════════════════════════════════════════════
# Group 5: SHORT-TERM MOMENTUM (Only 1-2 weeks works in A-shares)
# ═══════════════════════════════════════════════════════════════

class Momentum5d(BaseFactor):
    name = "mom_5d"
    category = "momentum"
    description = "5日动量: 仅1周窗口(长期动量在A股失效!)"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        return prices['close'].pct_change(5)


class Momentum10d(BaseFactor):
    name = "mom_10d"
    category = "momentum"
    description = "10日动量: 2周窗口(边界有效)"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        return prices['close'].pct_change(10)


class MACrossover5_20(BaseFactor):
    name = "ma_crossover_5_20"
    category = "momentum"
    description = "MA金叉(5/20): short-term trend strength"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        ma5 = close.rolling(5, min_periods=3).mean()
        ma20 = close.rolling(20, min_periods=10).mean()
        return ma5 / ma20 - 1


class MACrossover10_50(BaseFactor):
    name = "ma_crossover_10_50"
    category = "momentum"
    description = "MA金叉(10/50): medium-term trend indicator"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        ma10 = close.rolling(10, min_periods=5).mean()
        ma50 = close.rolling(50, min_periods=25).mean()
        return ma10 / ma50 - 1


class MACDSignal(BaseFactor):
    name = "macd_signal"
    category = "momentum"
    description = "MACD信号线(12,26,9): (DIF-DEA)/close"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        ema12 = close.ewm(span=12, min_periods=6).mean()
        ema26 = close.ewm(span=26, min_periods=13).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, min_periods=5).mean()
        return (dif - dea) / close


class PriceVelocity10d(BaseFactor):
    name = "price_velocity_10d"
    category = "momentum"
    description = "价格速度(10日): linear regression slope / mean price"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']

        def _slope(y):
            y = y[~np.isnan(y)]
            if len(y) < 5:
                return np.nan
            x = np.arange(len(y))
            slope, _, _, _, _ = stats.linregress(x, y)
            return slope / np.mean(y) if np.mean(y) != 0 else 0

        return close.rolling(10, min_periods=5).apply(_slope, raw=True)


class PriceAboveMA20(BaseFactor):
    name = "price_above_ma20"
    category = "momentum"
    description = "价格在MA20上方: (close/MA20)-1"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        ma20 = close.rolling(20, min_periods=10).mean()
        return close / ma20 - 1


class HighLowTrend20d(BaseFactor):
    name = "hl_trend_20d"
    category = "momentum"
    description = "高低点趋势(20日): spread narrowing=trending"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        spread = (prices['high'] - prices['low']) / prices['close']
        spread_5d = spread.rolling(5, min_periods=3).mean()
        spread_20d = spread.rolling(20, min_periods=10).mean()
        return spread_20d / spread_5d.replace(0, np.nan)


# ═══════════════════════════════════════════════════════════════
# Group 6: QUALITY / PROFITABILITY
# ═══════════════════════════════════════════════════════════════

class PriceStability60d(BaseFactor):
    name = "price_stability_60d"
    category = "quality"
    description = "价格稳定性(60日): 1/(60d vol), stable=quality proxy"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        ret = prices['close'].pct_change()
        vol = ret.rolling(60, min_periods=30).std()
        return 1.0 / vol.replace(0, np.nan)


class UpDownRatio20d(BaseFactor):
    name = "up_down_ratio_20d"
    category = "quality"
    description = "涨跌比(20日): up_capture/down_capture"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        ret = prices['close'].pct_change()
        up = ret.clip(lower=0).rolling(20, min_periods=10).sum()
        down = -ret.clip(upper=0).rolling(20, min_periods=10).sum()
        return up / down.replace(0, np.nan)


class PositiveDayRatio20d(BaseFactor):
    name = "pos_day_ratio_20d"
    category = "quality"
    description = "上涨天数比(20日): ratio of positive days"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        ret = prices['close'].pct_change()
        return (ret > 0).rolling(20, min_periods=10).mean()


class ReturnSkew60d(BaseFactor):
    name = "ret_skew_60d"
    category = "quality"
    description = "收益偏度(60日): positive skew=more large up days=quality"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        ret = prices['close'].pct_change()
        return ret.rolling(60, min_periods=30).skew()


class ReturnKurt60d(BaseFactor):
    name = "ret_kurt_60d"
    category = "quality"
    description = "收益峰度(60日反转): lower kurtosis=fewer extreme events"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        ret = prices['close'].pct_change()
        return -ret.rolling(60, min_periods=30).kurt()


class MaxUpStreak20d(BaseFactor):
    name = "max_up_streak_20d"
    category = "quality"
    description = "最长连涨(20日): longest consecutive up days"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        ret = prices['close'].pct_change()
        up = (ret > 0).astype(int)

        def _run_length(col: pd.Series) -> pd.Series:
            group = (col != col.shift()).cumsum()
            return col.groupby(group).cumsum()

        streak = up.apply(_run_length)
        return streak.rolling(20, min_periods=10).max()


class EarningsYieldProxy(BaseFactor):
    name = "ey_proxy"
    category = "quality"
    description = "EP代理(质量版): 1/(close*vol_60d), low vol+low price=quality value"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        ret = close.pct_change()
        vol_60d = ret.rolling(60, min_periods=30).std() * np.sqrt(252)
        return 1.0 / (close * vol_60d.replace(0, 1))


# ═══════════════════════════════════════════════════════════════
# Group 7: SIZE
# ═══════════════════════════════════════════════════════════════

class SizeProxy(BaseFactor):
    name = "size_proxy"
    category = "size"
    description = "规模代理(反转): -log(price*avg_vol), small-cap premium"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        volume = prices['volume']
        avg_vol = volume.rolling(120, min_periods=60).mean()
        cap_proxy = np.log(close * avg_vol)
        return -cap_proxy


class MidCapFilter(BaseFactor):
    name = "midcap_filter"
    category = "size"
    description = "中盘过滤: penalize smallest 30% & largest 20% (shell-value protection)"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        volume = prices['volume']
        cap_proxy = close * volume.rolling(120, min_periods=60).mean()
        cap_rank = cap_proxy.rank(axis=1, pct=True)
        return (1.0 - 2.0 * np.abs(cap_rank - 0.4)).clip(lower=-1, upper=1)


# ═══════════════════════════════════════════════════════════════
# Group 8: TECHNICAL (Conservative, A-share validated only)
# ═══════════════════════════════════════════════════════════════

class NewHighRatio20d(BaseFactor):
    name = "new_high_ratio_20d"
    category = "technical"
    description = "距20日新高天数(反转): closer to new high = better"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        high = prices['high']
        hh_20 = high.rolling(20, min_periods=10).max()
        at_high = (high >= hh_20).astype(float)
        reset = at_high.where(at_high > 0).fillna(0)
        days_since = (1 - at_high).cumsum()
        days_since = days_since - days_since.where(at_high > 0).ffill().fillna(0)
        return -days_since / 20


class HammerPattern(BaseFactor):
    name = "hammer_pattern"
    category = "technical"
    description = "锤子线形态(5日): classic hammer candlestick"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        open_, high, low, close = (prices['open'], prices['high'],
                                    prices['low'], prices['close'])
        body = np.abs(close - open_)
        lower_shadow = pd.DataFrame(
            np.minimum(open_.values, close.values) - low.values,
            index=open_.index, columns=open_.columns
        )
        upper_shadow = pd.DataFrame(
            high.values - np.maximum(open_.values, close.values),
            index=open_.index, columns=open_.columns
        )
        is_hammer = (lower_shadow >= 2 * body) & (upper_shadow <= 0.3 * body) & (body > 0)
        return is_hammer.rolling(5, min_periods=1).sum().astype(float)


class SupportBounce(BaseFactor):
    name = "support_bounce"
    category = "technical"
    description = "支撑反弹(20日低点): near support + reversal day"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        low_20 = close.rolling(20, min_periods=10).min()
        near_support = (close - low_20) / close < 0.02
        reversal = close.pct_change() > 0
        return (near_support & reversal).rolling(5, min_periods=1).sum().astype(float)


class ATRRatio(BaseFactor):
    name = "atr_ratio"
    category = "technical"
    description = "ATR比率(14日反转): ATR(14)/close, lower=vol contraction"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        high, low, close = prices['high'], prices['low'], prices['close']
        prev_close = close.shift(1)
        tr = np.maximum(
            np.maximum(high.values - low.values,
                      np.abs(high.values - prev_close.values)),
            np.abs(low.values - prev_close.values)
        )
        tr = pd.DataFrame(tr, index=high.index, columns=high.columns)
        atr = tr.rolling(14, min_periods=7).mean()
        return -(atr / close)


class TRContraction(BaseFactor):
    name = "tr_contraction"
    category = "technical"
    description = "TR收缩: ATR(5)/ATR(20)-1, negative=vol contracting=breakout potential"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        high, low, close = prices['high'], prices['low'], prices['close']
        prev_close = close.shift(1)
        tr = np.maximum(
            np.maximum(high.values - low.values,
                      np.abs(high.values - prev_close.values)),
            np.abs(low.values - prev_close.values)
        )
        tr = pd.DataFrame(tr, index=high.index, columns=high.columns)
        atr_5 = tr.rolling(5, min_periods=3).mean()
        atr_20 = tr.rolling(20, min_periods=10).mean()
        return -(atr_5 / atr_20.replace(0, np.nan))


# ═══════════════════════════════════════════════════════════════
# Group 9: MONEY FLOW
# ═══════════════════════════════════════════════════════════════

class VWAPDivergence(BaseFactor):
    name = "vwap_divergence"
    category = "money_flow"
    description = "VWAP偏离(反转): -(close-vwap)/vwap, negative=below VWAP"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        vwap_daily = (prices['high'] + prices['low'] + prices['close']) / 3
        return -(prices['close'] - vwap_daily) / vwap_daily


class OBVDivergence(BaseFactor):
    name = "obv_divergence"
    category = "money_flow"
    description = "OBV背离: OBV趋势(20d) vs 价格趋势(20d)"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        volume = prices['volume']
        price_dir = np.sign(close.diff())
        obv = (volume * price_dir).cumsum()
        return obv.pct_change(20) - close.pct_change(20)


class MFI14d(BaseFactor):
    name = "mfi_14d"
    category = "money_flow"
    description = "MFI(14日反转): volume-weighted RSI, lower=oversold"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        high, low, close, volume = (prices['high'], prices['low'],
                                     prices['close'], prices['volume'])
        typical_price = (high + low + close) / 3
        raw_money_flow = typical_price * volume
        price_dir = typical_price.diff()
        pos_flow = raw_money_flow.where(price_dir > 0, 0)
        neg_flow = raw_money_flow.where(price_dir < 0, 0)
        pos_sum = pos_flow.rolling(14, min_periods=7).sum()
        neg_sum = neg_flow.rolling(14, min_periods=7).sum()
        mr = pos_sum / neg_sum.replace(0, 1)
        mfi = 100 - (100 / (1 + mr))
        return -mfi


class AccumDistDivergence(BaseFactor):
    name = "ad_divergence"
    category = "money_flow"
    description = "A/D线背离: Chaikin A/D divergence from price (20d)"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        high, low, close, volume = (prices['high'], prices['low'],
                                     prices['close'], prices['volume'])
        hl_range = (high - low).replace(0, np.nan)
        clv = ((close - low) - (high - close)) / hl_range
        ad = (clv * volume).cumsum()
        return ad.pct_change(20) - close.pct_change(20)


class LargeOrderProxy(BaseFactor):
    name = "large_order_proxy"
    category = "money_flow"
    description = "大单活动代理: high vol + narrow range = accumulation"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        volume = prices['volume']
        price_range = (prices['high'] - prices['low']) / close
        vol_zscore = (volume - volume.rolling(20).mean()) / volume.rolling(20).std().replace(0, 1)
        range_zscore = -(price_range - price_range.rolling(20).mean()) / \
                        price_range.rolling(20).std().replace(0, 1)
        return vol_zscore + range_zscore


# ═══════════════════════════════════════════════════════════════
# Group 10: REGIME / MARKET STATE
# ═══════════════════════════════════════════════════════════════

class RelativeStrength20d(BaseFactor):
    name = "rel_strength_20d"
    category = "regime"
    description = "相对强度(20日): cumulative excess return vs cross-sectional mean"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        ret = prices['close'].pct_change()
        mkt = ret.mean(axis=1)
        rs = pd.DataFrame(
            ret.values - mkt.values[:, None],
            index=ret.index, columns=ret.columns
        )
        return rs.rolling(20, min_periods=10).sum()


class RegimeResilience(BaseFactor):
    name = "regime_resilience"
    category = "regime"
    description = "下跌韧性: avg return on market-down days (60d rolling)"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        ret = prices['close'].pct_change()
        mkt = ret.mean(axis=1)
        is_down = mkt < 0
        down_ret = pd.DataFrame(
            np.where(is_down.values[:, None], ret.values, np.nan),
            index=ret.index, columns=ret.columns
        )
        return pd.DataFrame(down_ret).rolling(60, min_periods=10).mean()


class BreakoutStrength(BaseFactor):
    name = "breakout_strength"
    category = "regime"
    description = "突破强度: (close-20d_high)/20d_high"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        high_20 = close.rolling(20, min_periods=10).max()
        breakout = (close - high_20) / high_20
        return breakout.clip(lower=-0.05, upper=0.15)


class GapUpStrength(BaseFactor):
    name = "gap_up_strength"
    category = "regime"
    description = "跳空强度(5日均): (open-prev_close)/prev_close"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        gap = (prices['open'] - prices['close'].shift(1)) / prices['close'].shift(1)
        return gap.rolling(5, min_periods=3).mean()


# ═══════════════════════════════════════════════════════════════
# Factor Registry & Registration
# ═══════════════════════════════════════════════════════════════
# Group 11+: 增量因子（2026-08 扩展）—— 新维度，降低与既有因子相关性
# ═══════════════════════════════════════════════════════════════


class OvernightReturn5d(BaseFactor):
    """5日累计隔夜收益（开盘相对昨收）：捕捉 A 股隔夜-日内收益分离。"""
    name = "overnight_ret_5d"
    category = "reversal"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        open_ = prices['open']
        overnight = open_ / close.shift(1) - 1
        return overnight.rolling(5, min_periods=3).sum()


class IntradayReturn5d(BaseFactor):
    """5日累计日内收益（收盘相对开盘）：与隔夜收益正交的日内维度。"""
    name = "intraday_ret_5d"
    category = "reversal"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        open_ = prices['open']
        intraday = close / open_ - 1
        return intraday.rolling(5, min_periods=3).sum()


class VolTermSlope5_60(BaseFactor):
    """波动率期限结构：5日波动 / 60日波动。<1 压缩、>1 扩张，均值回归信号。"""
    name = "vol_term_slope_5_60"
    category = "low_vol"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        ret = prices['close'].pct_change()
        vol5 = ret.rolling(5, min_periods=3).std()
        vol60 = ret.rolling(60, min_periods=30).std()
        return vol5 / vol60.replace(0, np.nan)


class VolumeZScore20d(BaseFactor):
    """成交量 20 日 Z 分数：放量/缩量的相对状态。"""
    name = "volume_zscore_20d"
    category = "money_flow"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        vol = prices['volume']
        mean = vol.rolling(20, min_periods=10).mean()
        std = vol.rolling(20, min_periods=10).std()
        return (vol - mean) / std.replace(0, np.nan)


class TurnoverAccel5_20(BaseFactor):
    """换手率加速度：5日均换手 / 20日均换手。资金活跃度边际变化。"""
    name = "turnover_accel_5_20"
    category = "liquidity"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        if 'turn' not in prices:
            return pd.DataFrame(index=prices['close'].index, columns=prices['close'].columns)
        turn = prices['turn']
        return turn.rolling(5, min_periods=3).mean() / turn.rolling(20, min_periods=10).mean()


class RSI2(BaseFactor):
    """2 日 RSI：超短线深度超买/超卖反转。"""
    name = "rsi2"
    category = "reversal"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        delta = prices['close'].diff()
        up = delta.clip(lower=0).rolling(2, min_periods=1).mean()
        down = (-delta).clip(lower=0).rolling(2, min_periods=1).mean()
        rs = up / down.replace(0, np.nan)
        return 100 - 100 / (1 + rs)


class ADX14d(BaseFactor):
    """ADX 趋势强度（简化 Wilder）：+DI 与 -DI 差值的平滑。"""
    name = "adx_14d"
    category = "momentum"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        high = prices['high']
        low = prices['low']
        close = prices['close']
        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = pd.DataFrame(
            np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
            index=high.index, columns=high.columns)
        minus_dm = pd.DataFrame(
            np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
            index=high.index, columns=high.columns)
        tr = pd.concat([high - low, (high - close.shift(1)).abs(),
                        (low - close.shift(1)).abs()], axis=1).max(axis=1)
        tr = tr.to_frame() if tr.ndim == 1 else tr
        # 分股票滚动
        tr = pd.DataFrame(tr, index=high.index, columns=high.columns)
        atr = tr.rolling(14, min_periods=7).mean()
        plus_di = 100 * plus_dm.rolling(14, min_periods=7).mean() / atr.replace(0, np.nan)
        minus_di = 100 * minus_dm.rolling(14, min_periods=7).mean() / atr.replace(0, np.nan)
        dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
        return dx.rolling(14, min_periods=7).mean()


class RangeCompression5_20(BaseFactor):
    """区间压缩：5日平均振幅 / 20日平均振幅。收窄=变盘前兆。"""
    name = "range_compression_5_20"
    category = "technical"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        amp = (prices['high'] - prices['low']) / prices['close'].replace(0, np.nan)
        return amp.rolling(5, min_periods=3).mean() / amp.rolling(20, min_periods=10).mean()


class HighLowPosition20d(BaseFactor):
    """20日区间位置：(close - min20)/(max20 - min20)。"""
    name = "high_low_pos_20d"
    category = "technical"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        close = prices['close']
        min20 = close.rolling(20, min_periods=10).min()
        max20 = close.rolling(20, min_periods=10).max()
        return (close - min20) / (max20 - min20).replace(0, np.nan)


class MaxGain60d(BaseFactor):
    """60日最大单日涨幅：彩票股/暴涨记忆惩罚。"""
    name = "max_gain_60d"
    category = "quality"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        ret = prices['close'].pct_change()
        return ret.rolling(60, min_periods=30).max()


class GapUpFreq20d(BaseFactor):
    """20日跳空高开频率：开盘缺口行为的持续性。"""
    name = "gap_up_freq_20d"
    category = "money_flow"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        gap = prices['open'] / prices['close'].shift(1) - 1
        return gap.gt(0).rolling(20, min_periods=10).mean()


class SkewStability60d(BaseFactor):
    """收益偏度稳定性：|偏度| 惩罚（高偏度彩票股长期跑输）。"""
    name = "skew_stability_60d"
    category = "quality"

    def compute(self, prices, financials=None) -> pd.DataFrame:
        ret = prices['close'].pct_change()
        return ret.rolling(60, min_periods=30).skew().abs()


# ═══════════════════════════════════════════════════════════════

ALL_A_SHARE_FACTORS = [
    # Low Vol (BEST — highest IC in A-shares)
    LowVol20d, LowVol60d, IdioVol60d, FfcResVol20d,
    AmplitudeVol20d, MaxDD60d, Beta60d, DownsideVol60d, Var95_60d,
    # Reversal (consistently effective)
    Reversal5d, Reversal10d, Reversal20d, OvernightReversal,
    IntradayReversal, RSI14Reversal, StochasticOversold, GapFillProb,
    # Value (stable)
    PriceTo52wHigh, PriceTo52wLow, PricePosition200d,
    DistanceFromMA60, DistanceFromMA200, BollPosition20d,
    EPProxy, BPProxy,
    # Liquidity (frictions dominant)
    TurnoverAnomaly5d, TurnoverStd20d, AmihudIlliq, DollarVolume20d,
    VolumePriceCorr, VolumeDryUp, AmountRatio5_20, ConsecutiveVolUp,
    # Short-term momentum only
    Momentum5d, Momentum10d, MACrossover5_20, MACrossover10_50,
    MACDSignal, PriceVelocity10d, PriceAboveMA20, HighLowTrend20d,
    # Quality
    PriceStability60d, UpDownRatio20d, PositiveDayRatio20d,
    ReturnSkew60d, ReturnKurt60d, MaxUpStreak20d, EarningsYieldProxy,
    # Size
    SizeProxy, MidCapFilter,
    # Technical
    NewHighRatio20d, HammerPattern, SupportBounce, ATRRatio, TRContraction,
    # Money Flow
    VWAPDivergence, OBVDivergence, MFI14d, AccumDistDivergence, LargeOrderProxy,
    # Regime
    RelativeStrength20d, RegimeResilience, BreakoutStrength, GapUpStrength,
    # 2026-08 增量因子（新维度）
    OvernightReturn5d, IntradayReturn5d, VolTermSlope5_60, VolumeZScore20d,
    TurnoverAccel5_20, RSI2, ADX14d, RangeCompression5_20,
    HighLowPosition20d, MaxGain60d, GapUpFreq20d, SkewStability60d,
    # 2026-08 第二轮扩容（a_share_factors_v2.py，42 个）
    *_V2_NEW_FACTOR_CLASSES,
    # 2026-08 FinHack α101/191 公式入册（finhack_screen.py 真实面板筛选，35 个）
    *_FINHACK_FACTOR_CLASSES,
]

FACTOR_GROUP_MAP = {
    "low_vol": "Low Volatility (⭐ Best A-share performer)",
    "reversal": "Short-term Reversal (Consistently effective)",
    "value": "Value / Price Ratios (Stable)",
    "liquidity": "Liquidity / Turnover (Frictions dominate)",
    "momentum": "Short-term Momentum (1-2 weeks only!)",
    "quality": "Quality / Profitability Proxies",
    "size": "Size Premium (Small-cap)",
    "technical": "Technical / Price Patterns (Conservative)",
    "money_flow": "Money Flow / Volume-weighted",
    "regime": "Market Regime / Context",
    "growth": "Fundamental Growth (THS quarterly)",
}


def register_all_a_share_factors(registry) -> int:
    """Register all A-share factors into the given FactorRegistry.

    Args:
        registry: FactorRegistry instance (e.g., GLOBAL_REGISTRY).

    Returns:
        Number of factors successfully registered.
    """
    registered = 0
    for factor_cls in ALL_A_SHARE_FACTORS:
        try:
            # Instantiate and ensure correct snake_case name from class attribute
            factor = factor_cls()
            # Use the class-level 'name' attribute if set, otherwise keep default
            if hasattr(factor_cls, 'name') and factor_cls.name:
                factor.name = factor_cls.name
            registry.register(factor)
            registered += 1
        except ValueError:
            pass
    return registered


def get_factor_summary_by_group() -> dict:
    """Return summary of all factors organized by group."""
    groups = {}
    for factor_cls in ALL_A_SHARE_FACTORS:
        category = getattr(factor_cls, 'category', 'unknown')
        name = getattr(factor_cls, 'name', factor_cls.__name__)
        desc = getattr(factor_cls, 'description', '')
        if category not in groups:
            groups[category] = []
        groups[category].append({
            "name": name,
            "description": desc,
        })
    return groups


from .logger import get_logger

_logger = get_logger()


def get_factor_group_info() -> list:
    """Return factor inventory grouped by category for the dashboard."""
    groups = get_factor_summary_by_group()
    return [
        {
            "group": key,
            "label": FACTOR_GROUP_MAP.get(key, key),
            "factors": items,
        }
        for key, items in groups.items()
    ]


# For backward compatibility with existing scripts
def compute_all_factors_from_panel(panel: dict) -> dict:
    """Compute all factors from a raw dict-panel (for standalone scripts).

    Args:
        panel: dict with 'open','high','low','close','volume','amount' DataFrames.

    Returns:
        dict mapping factor_name -> DataFrame.
    """
    results = {}
    failed: list[str] = []
    for factor_cls in ALL_A_SHARE_FACTORS:
        try:
            factor = factor_cls()
            if hasattr(factor_cls, 'name') and factor_cls.name:
                factor.name = factor_cls.name
            frame = factor.compute(panel)
            if frame is None or getattr(frame, "empty", True):
                raise ValueError("空结果")
            results[factor.name] = frame
        except Exception as exc:
            failed.append(f"{factor_cls.__name__}: {exc}")
    if failed:
        _logger.warning(f"因子计算失败 {len(failed)}/{len(ALL_A_SHARE_FACTORS)}: "
                        + "; ".join(failed))
    return results
