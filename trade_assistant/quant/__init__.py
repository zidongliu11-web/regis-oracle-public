"""量化研究子包.

提供:
- Factor Library: 64 个 A 股因子 (a_share_library)
- IC Tester: 因子 IC 测试与分级 (ic_tester)
- Composite Builder: 因子复合构建 (composite)
- Strategy Pipeline: 完整回测流水线 (pipeline)
- Data Source: 日线数据加载 (data_source)
"""
from __future__ import annotations

from .ic_tester import (
    ICResult,
    classify_tier,
    compute_composite_ic,
    compute_rank_ic,
    run_ic_tests,
)
from .composite import (
    build_composite,
    compute_ic_weights,
    select_factors_cross_group,
    select_factors_top_n,
    zscore_cross_section,
)
from .pipeline import PipelineReport, StrategyPipeline
from .a_share_library import (
    ALL_A_SHARE_FACTORS,
    FACTOR_GROUP_MAP,
    compute_all_factors_from_panel,
    get_factor_summary_by_group,
    get_factor_group_info,
)
from .data_source import (
    BIG50_CODES,
    DEFAULT_END,
    DEFAULT_START,
    baostock_symbol,
    load_long_df,
    load_panel,
)
from .runner import run_quant_backtest

__all__ = [
    "ICResult",
    "classify_tier",
    "compute_composite_ic",
    "compute_rank_ic",
    "run_ic_tests",
    "build_composite",
    "compute_ic_weights",
    "select_factors_cross_group",
    "select_factors_top_n",
    "zscore_cross_section",
    "PipelineReport",
    "StrategyPipeline",
    "ALL_A_SHARE_FACTORS",
    "FACTOR_GROUP_MAP",
    "compute_all_factors_from_panel",
    "get_factor_summary_by_group",
    "get_factor_group_info",
    "BIG50_CODES",
    "DEFAULT_START",
    "DEFAULT_END",
    "baostock_symbol",
    "load_long_df",
    "load_panel",
    "run_quant_backtest",
]
