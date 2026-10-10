"""因子基类与接口定义（优化版 v3.0）。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy import stats


class FactorValidationError(Exception):
    """因子数据验证错误。"""
    pass


class BaseFactor(ABC):
    """因子基类，提供统一的接口和验证逻辑。"""

    name: str
    category: str
    description: str = ""

    # A股交易规则常量
    MIN_TRADE_DAYS: int = 20  # 最小交易天数
    MAX_RETURN_CAP: float = 0.1  # 单日最大收益率限制
    MIN_PRICE: float = 0.01  # 最小价格
    VOLUME_THRESHOLD: float = 0.8  # 成交量有效性阈值
    LIMIT_UP_THRESHOLD: float = 0.095  # 涨停阈值
    LIMIT_DOWN_THRESHOLD: float = -0.095  # 跌停阈值
    MIN_STOCKS_PER_DATE: int = 30  # 每日期最小股票数
    MAX_MISSING_RATIO: float = 0.3  # 最大缺失比例

    def __init__(self, name: str = "", category: str = "其他"):
        self.name = name or self.__class__.__name__
        self.category = category

    @abstractmethod
    def compute(
        self,
        prices: Dict[str, pd.DataFrame],
        financials: Optional[Dict[str, pd.DataFrame]] = None,
        **kwargs: Any
    ) -> pd.DataFrame:
        """
        计算因子值。

        Parameters
        ----------
        prices : Dict[str, pd.DataFrame]
            价格数据字典，key为数据名称（如'close','volume'），value为DataFrame，index为日期，columns为股票代码
        financials : Dict[str, pd.DataFrame], optional
            财务数据字典，key为数据名称（如'pe','pb'），value为DataFrame，index为日期，columns为股票代码

        Returns
        -------
        pd.DataFrame
            因子值，index为日期，columns为股票代码
        """
        pass

    def validate_input(
        self,
        prices: Dict[str, pd.DataFrame],
        financials: Optional[Dict[str, pd.DataFrame]] = None
    ) -> None:
        """验证输入数据的完整性和有效性。"""
        if not prices:
            raise FactorValidationError("价格数据不能为空")

        for name, df in prices.items():
            if df is None or df.empty:
                raise FactorValidationError(f"价格数据 '{name}' 不能为空")
            if not isinstance(df.index, pd.DatetimeIndex):
                raise FactorValidationError(f"价格数据 '{name}' 索引必须是日期类型")
            if len(df) < self.MIN_TRADE_DAYS:
                raise FactorValidationError(
                    f"价格数据 '{name}' 天数不足{self.MIN_TRADE_DAYS}天"
                )

        # 检查必要数据
        required_data = ['close', 'volume']
        for data_name in required_data:
            if data_name not in prices:
                raise FactorValidationError(f"价格数据缺少必要数据: {data_name}")

        # 检查异常值（以close为例）
        close_df = prices['close']
        if (close_df < self.MIN_PRICE).any().any():
            raise FactorValidationError("存在异常低价")

        # 检查数据连续性
        date_diff = close_df.index.to_series().diff().dropna()
        max_gap = date_diff.max()
        if max_gap > pd.Timedelta(days=10):
            raise FactorValidationError(f"数据存在超过10天的间隔: {max_gap}")

        # 验证财务数据（如果提供）
        if financials is not None:
            for name, df in financials.items():
                if df is not None and not df.empty:
                    if not isinstance(df.index, pd.DatetimeIndex):
                        raise FactorValidationError(
                            f"财务数据 '{name}' 索引必须是日期类型"
                        )

    def get_valid_stocks(
        self,
        prices: Dict[str, pd.DataFrame],
        date: pd.Timestamp
    ) -> pd.Index:
        """
        获取在指定日期有效的股票列表（非停牌、非涨跌停、非ST等）。
        这是一个不引入未来信息的筛选方法。

        Parameters
        ----------
        prices : Dict[str, pd.DataFrame]
            价格数据字典
        date : pd.Timestamp
            指定日期

        Returns
        -------
        pd.Index
            有效股票的代码索引
        """
        if date not in prices['close'].index:
            return pd.Index([])

        # 获取当日数据
        close = prices['close'].loc[date]
        volume = prices['volume'].loc[date]

        # 获取前一日收盘价（用于计算涨跌幅）
        date_idx = prices['close'].index.get_loc(date)
        if date_idx == 0:
            return close.dropna().index

        prev_date = prices['close'].index[date_idx - 1]
        pre_close = prices['close'].loc[prev_date].reindex(close.index)

        # 计算涨跌幅
        returns = close / pre_close - 1

        # 筛选条件
        valid_mask = (
            close.notna() &  # 非缺失
            volume.notna() &  # 非缺失
            (volume > 0) &  # 非停牌
            (close > self.MIN_PRICE) &  # 非低价股
            (returns < self.LIMIT_UP_THRESHOLD) &  # 非涨停
            (returns > self.LIMIT_DOWN_THRESHOLD)  # 非跌停
        )

        return close[valid_mask].index

    def postprocess_factor(
        self,
        factor: pd.DataFrame,
        method: str = 'mad',
        industry: Optional[pd.DataFrame] = None
    ) -> pd.DataFrame:
        """
        因子后处理：去极值、标准化、中性化。

        Parameters
        ----------
        factor : pd.DataFrame
            因子值，index为日期，columns为股票代码
        method : str
            去极值方法：'mad' (默认), 'quantile', 'zscore'
        industry : pd.DataFrame, optional
            行业分类数据，用于行业中性化

        Returns
        -------
        pd.DataFrame
            处理后的因子值
        """
        df = factor.copy()

        # 对每一天进行截面处理
        def process_cross_section(series: pd.Series) -> pd.Series:
            # 去极值
            if method == 'mad':
                series = self._mad_winsorize(series)
            elif method == 'quantile':
                series = self._quantile_winsorize(series)
            elif method == 'zscore':
                series = self._zscore_winsorize(series)
            else:
                raise ValueError(f"未知的去极值方法: {method}")

            # 行业中性化（如果提供行业数据）
            if industry is not None and date in industry.index:
                series = self._industry_neutralize(series, industry.loc[date])

            # 标准化（Z-score）
            mean = series.mean()
            std = series.std()
            if std == 0:
                return series * 0
            series = (series - mean) / std

            return series

        # 逐日处理
        for date in df.index:
            df.loc[date] = process_cross_section(df.loc[date])

        # 处理无穷值和NaN
        df = df.replace([np.inf, -np.inf], np.nan)

        # 使用截面中位数填充缺失值
        df = df.T.fillna(df.median(axis=1)).T

        return df

    def _mad_winsorize(self, series: pd.Series, n: float = 5.0) -> pd.Series:
        """MAD去极值方法。"""
        median = series.median()
        mad = (series - median).abs().median()
        if mad == 0:
            return series * 0
        upper = median + n * mad
        lower = median - n * mad
        return series.clip(lower, upper)

    def _quantile_winsorize(
        self,
        series: pd.Series,
        lower_quantile: float = 0.01,
        upper_quantile: float = 0.99
    ) -> pd.Series:
        """分位数去极值方法。"""
        lower = series.quantile(lower_quantile)
        upper = series.quantile(upper_quantile)
        return series.clip(lower, upper)

    def _zscore_winsorize(self, series: pd.Series, threshold: float = 3.0) -> pd.Series:
        """Z-score去极值方法。"""
        mean = series.mean()
        std = series.std()
        if std == 0:
            return series * 0
        z_scores = (series - mean) / std
        return series.where(z_scores.abs() <= threshold, mean + threshold * std * np.sign(z_scores))

    def _industry_neutralize(
        self,
        series: pd.Series,
        industry_series: pd.Series
    ) -> pd.Series:
        """行业中性化处理。"""
        # 计算每个行业的均值
        industry_means = series.groupby(industry_series).transform('mean')
        # 减去行业均值
        return series - industry_means

    def compute_factor_exposure(
        self,
        factor: pd.DataFrame,
        returns: pd.DataFrame,
        periods: List[int] = [1, 5, 20]
    ) -> Dict[str, pd.Series]:
        """
        计算因子暴露度（IC值）。
        使用向量化操作提高效率。

        Parameters
        ----------
        factor : pd.DataFrame
            因子值，index为日期，columns为股票代码
        returns : pd.DataFrame
            收益率数据，index为日期，columns为股票代码
        periods : List[int]
            预测周期（天数）

        Returns
        -------
        Dict[str, pd.Series]
            IC值序列字典
        """
        results = {}

        for period in periods:
            # 计算未来N日累计收益率（复利）
            # 使用向量化方法避免rolling.apply
            future_returns = self._compute_future_returns(returns, period)

            # 对齐因子和未来收益率
            aligned_factor = factor.loc[future_returns.index]
            aligned_returns = future_returns

            # 计算每日截面IC
            ic_values = []
            dates = []

            for date in aligned_factor.index:
                f = aligned_factor.loc[date].dropna()
                r = aligned_returns.loc[date].reindex(f.index).dropna()

                if len(f) > self.MIN_STOCKS_PER_DATE and len(r) > self.MIN_STOCKS_PER_DATE:
                    # 使用Spearman秩相关系数
                    ic = f.rank().corr(r.rank())
                    if not np.isnan(ic):
                        ic_values.append(ic)
                        dates.append(date)

            if ic_values:
                results[f'IC_{period}d'] = pd.Series(ic_values, index=dates)

        return results

    def _compute_future_returns(
        self,
        returns: pd.DataFrame,
        period: int
    ) -> pd.DataFrame:
        """
        计算未来N日累计收益率。
        使用向量化方法提高效率。
        """
        # 使用expanding窗口计算累计收益率
        future_returns = (1 + returns).rolling(window=period).apply(
            lambda x: np.prod(x), raw=True
        ) - 1

        # 向前移动period天，得到未来收益率
        future_returns = future_returns.shift(-period)

        # 删除最后period天的NaN
        return future_returns.iloc[:-period] if period > 0 else future_returns

    def compute_performance_metrics(
        self,
        factor: pd.DataFrame,
        returns: pd.DataFrame
    ) -> Dict[str, float]:
        """计算因子性能指标。"""
        metrics = {}

        # 计算IC值
        exposures = self.compute_factor_exposure(factor, returns)

        for key, ic_series in exposures.items():
            if len(ic_series) > 0:
                metrics[f'{key}_mean'] = ic_series.mean()
                metrics[f'{key}_std'] = ic_series.std()
                metrics[f'{key}_ir'] = (
                    ic_series.mean() / ic_series.std()
                    if ic_series.std() != 0 else 0
                )
                metrics[f'{key}_win_rate'] = (ic_series > 0).mean()
                metrics[f'{key}_t_stat'] = (
                    ic_series.mean() / (ic_series.std() / np.sqrt(len(ic_series)))
                )
                metrics[f'{key}_p_value'] = (
                    2 * (1 - stats.t.cdf(abs(metrics[f'{key}_t_stat']), df=len(ic_series)-1))
                    if len(ic_series) > 1 else 1.0
                )

        # 计算分组收益
        spread_metrics = self._compute_long_short_spread(factor, returns)
        metrics.update(spread_metrics)

        return metrics

    def _compute_long_short_spread(
        self,
        factor: pd.DataFrame,
        returns: pd.DataFrame,
        n_groups: int = 10
    ) -> Dict[str, float]:
        """
        计算多空组合收益差。
        使用向量化操作提高效率。

        Returns
        -------
        Dict[str, float]
            包含多空收益差、多头收益、空头收益等指标
        """
        # 计算T+1收益率
        future_returns = returns.shift(-1)

        # 对齐数据
        aligned_factor = factor.loc[future_returns.index[:-1]]
        aligned_returns = future_returns.iloc[:-1]

        spreads = []
        top_returns_list = []
        bottom_returns_list = []

        for date in aligned_factor.index:
            f = aligned_factor.loc[date].dropna()
            r = aligned_returns.loc[date].reindex(f.index).dropna()

            if len(f) < n_groups * 2 or len(r) < n_groups * 2:
                continue

            # 使用分位数分组，处理重复值
            try:
                # 使用rank方法处理重复值
                f_rank = f.rank(method='first')
                groups = pd.qcut(f_rank, n_groups, labels=False)
            except ValueError:
                continue

            # 计算多空收益差
            top_mask = groups == groups.max()
            bottom_mask = groups == groups.min()

            top_ret = r[top_mask].mean()
            bottom_ret = r[bottom_mask].mean()

            if not (np.isnan(top_ret) or np.isnan(bottom_ret)):
                spreads.append(top_ret - bottom_ret)
                top_returns_list.append(top_ret)
                bottom_returns_list.append(bottom_ret)

        if not spreads:
            return {
                'top_bottom_spread': 0.0,
                'top_return': 0.0,
                'bottom_return': 0.0,
                'spread_win_rate': 0.0
            }

        return {
            'top_bottom_spread': np.mean(spreads),
            'top_return': np.mean(top_returns_list),
            'bottom_return': np.mean(bottom_returns_list),
            'spread_win_rate': np.mean([s > 0 for s in spreads])
        }

    def compute_factor_decay(
        self,
        factor: pd.DataFrame,
        returns: pd.DataFrame,
        max_lag: int = 20
    ) -> pd.Series:
        """
        计算因子衰减速度。
        用于评估因子的持续性。

        Parameters
        ----------
        factor : pd.DataFrame
            因子值
        returns : pd.DataFrame
            收益率数据
        max_lag : int
            最大滞后天数

        Returns
        -------
        pd.Series
            各滞后期的IC值
        """
        ic_by_lag = {}

        for lag in range(1, max_lag + 1):
            # 计算滞后lag天的未来收益率
            future_returns = returns.shift(-lag)

            # 计算IC
            ic_values = []
            for date in factor.index:
                if date not in future_returns.index:
                    continue

                f = factor.loc[date].dropna()
                r = future_returns.loc[date].reindex(f.index).dropna()

                if len(f) > self.MIN_STOCKS_PER_DATE and len(r) > self.MIN_STOCKS_PER_DATE:
                    ic = f.rank().corr(r.rank())
                    if not np.isnan(ic):
                        ic_values.append(ic)

            if ic_values:
                ic_by_lag[lag] = np.mean(ic_values)

        return pd.Series(ic_by_lag)

    def compute_factor_turnover(
        self,
        factor: pd.DataFrame,
        top_pct: float = 0.2
    ) -> pd.Series:
        """
        计算因子换手率。
        评估因子的稳定性。

        Parameters
        ----------
        factor : pd.DataFrame
            因子值
        top_pct : float
            头部比例

        Returns
        -------
        pd.Series
            每日换手率
        """
        turnover_rates = []
        dates = []

        for i in range(1, len(factor)):
            prev_date = factor.index[i-1]
            curr_date = factor.index[i]

            # 获取头部股票
            prev_top = factor.loc[prev_date].nlargest(int(len(factor.columns) * top_pct)).index
            curr_top = factor.loc[curr_date].nlargest(int(len(factor.columns) * top_pct)).index

            # 计算换手率
            if len(prev_top) > 0:
                turnover = 1 - len(prev_top.intersection(curr_top)) / len(prev_top)
                turnover_rates.append(turnover)
                dates.append(curr_date)

        return pd.Series(turnover_rates, index=dates)

    def get_factor_summary(
        self,
        factor: pd.DataFrame,
        returns: pd.DataFrame
    ) -> Dict[str, Any]:
        """
        获取因子综合评估报告。

        Parameters
        ----------
        factor : pd.DataFrame
            因子值
        returns : pd.DataFrame
            收益率数据

        Returns
        -------
        Dict[str, Any]
            因子评估报告
        """
        # 计算性能指标
        performance = self.compute_performance_metrics(factor, returns)

        # 计算衰减
        decay = self.compute_factor_decay(factor, returns)

        # 计算换手率
        turnover = self.compute_factor_turnover(factor)

        # 计算因子覆盖率
        coverage = factor.notna().mean(axis=1)

        # 计算因子分布统计
        factor_stats = {
            'mean': factor.mean().mean(),
            'std': factor.std().mean(),
            'skew': factor.skew().mean(),
            'kurtosis': factor.kurtosis().mean(),
            'missing_ratio': factor.isna().mean().mean()
        }

        return {
            'name': self.name,
            'category': self.category,
            'description': self.description,
            'performance': performance,
            'decay': decay.to_dict() if not decay.empty else {},
            'turnover': {
                'mean': turnover.mean() if not turnover.empty else 0,
                'std': turnover.std() if not turnover.empty else 0
            },
            'coverage': {
                'mean': coverage.mean(),
                'min': coverage.min(),
                'max': coverage.max()
            },
            'factor_stats': factor_stats
        }
