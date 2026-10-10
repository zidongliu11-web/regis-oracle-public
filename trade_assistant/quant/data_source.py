"""量化数据源模块.

优先使用 baostock 前复权日线（big50 硬编码股票，2021-01-01 至 2026-07-31），
失败时回退到本地 v4_daily 审计库（4,802 只，2018-01-02 至 2026-08-05）。
两种数据源最终都输出统一的长格式 DataFrame：
    columns=[date, code, open, high, low, close, volume, amount]
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from .logger import get_logger

logger = get_logger()


# ——— 50 只大盘股（6 位代码，无前缀） ———
BIG50_CODES: List[str] = [
    "600000", "600016", "600028", "600030", "600031",
    "600036", "600048", "600050", "600104", "600276",
    "600309", "600519", "600585", "600690", "600809",
    "600887", "600900", "600941", "601012", "601088",
    "601111", "601166", "601288", "601318", "601398",
    "601601", "601628", "601633", "601668", "601728",
    "601766", "601818", "601857", "601888", "601899",
    "601919", "601939", "601988", "603288", "000001",
    "000002", "000333", "000568", "000651", "000858",
    "002142", "002304", "002415", "002594", "300750",
]

DEFAULT_START = "2021-01-01"
DEFAULT_END = "2026-07-31"

# 项目根目录/research/data
_DATA_ROOT = (
    Path(__file__).resolve().parent.parent.parent
    / "research"
    / "data"
)


def baostock_symbol(code: str) -> str:
    """6 位代码 → baostock 符号 (sh./sz. 前缀)."""
    code = str(code).strip().lower().split(".")[0]
    return f"sh.{code}" if code.startswith(("6", "9")) else f"sz.{code}"


def _norm_code(code: str) -> str:
    """任意格式 → 6 位数字代码."""
    return str(code).strip().lower().split(".")[0][-6:]


def load_baostock_long_df(
    codes: Optional[List[str]] = None,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
) -> pd.DataFrame:
    """从 baostock 读取前复权日线，返回长格式 DataFrame.

    Returns:
        columns=[date, code, open, high, low, close, volume, amount]
    """
    import baostock as bs

    codes = codes or BIG50_CODES
    logger.info(f"baostock 登录中…")
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"baostock 登录失败: {login.error_msg}")

    fields = "date,code,open,high,low,close,volume,amount"
    frames: List[pd.DataFrame] = []
    try:
        for code in codes:
            rs = bs.query_history_k_data_plus(
                baostock_symbol(code),
                fields,
                start_date=start,
                end_date=end,
                frequency="d",
                adjustflag="2",  # 前复权
            )
            if rs.error_code != "0":
                logger.warning(f"  {code} 查询失败: {rs.error_msg}")
                continue
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if rows:
                frames.append(pd.DataFrame(rows, columns=rs.fields))
    finally:
        bs.logout()

    if not frames:
        raise RuntimeError("baostock 未返回任何数据")

    df = pd.concat(frames, ignore_index=True)
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close", "volume"])
    logger.info(f"baostock 数据: {len(df)} 行, {df['code'].nunique()} 只股票")
    return df


def load_local_long_df(
    codes: Optional[List[str]] = None,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
) -> pd.DataFrame:
    """从本地 v4_daily 审计库读取日线，返回长格式 DataFrame."""
    codes = codes or BIG50_CODES
    keep_cols = ["date", "code", "open", "high", "low", "close", "volume", "amount"]

    frames: List[pd.DataFrame] = []
    for code in codes:
        path = _DATA_ROOT / "v4_daily" / f"{_norm_code(code)}.csv.gz"
        if not path.exists():
            continue
        try:
            df = pd.read_csv(path, dtype={"code": str, "date": str})
        except Exception as exc:
            logger.warning(f"  读取 {path.name} 失败: {exc}")
            continue
        if "tradestatus" in df.columns:
            df = df[df["tradestatus"] == 1]
        df = df[(df["date"] >= start) & (df["date"] <= end)]
        df = df[keep_cols]
        frames.append(df)

    if not frames:
        raise RuntimeError(f"本地数据无匹配: {_DATA_ROOT}/v4_daily")

    out = pd.concat(frames, ignore_index=True)
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["open", "high", "low", "close", "volume"])
    logger.info(f"本地数据: {len(out)} 行, {out['code'].nunique()} 只股票")
    return out


def load_long_df(
    codes: Optional[List[str]] = None,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
) -> tuple[pd.DataFrame, str]:
    """优先 baostock，失败回退本地审计库.

    Returns:
        (long_df, source) 其中 source ∈ {"baostock", "local"}
    """
    codes = codes or BIG50_CODES
    try:
        return load_baostock_long_df(codes=codes, start=start, end=end), "baostock"
    except Exception as exc:
        logger.warning(f"baostock 数据源失败，回退本地审计库: {exc}")
        return load_local_long_df(codes=codes, start=start, end=end), "local"


def load_panel(
    codes: Optional[List[str]] = None,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    min_stocks: Optional[int] = None,
    return_source: bool = False,
):
    """加载数据并转换为面板格式.

    Args:
        codes: 6 位股票代码列表，默认 big50
        start/end: 日期区间
        min_stocks: 每日最少股票数，默认 max(5, n_codes // 4)
        return_source: 是否同时返回数据源标识

    Returns:
        面板 dict {"open": ..., "close": ...}，或 (panel, source) 元组
    """
    from .pipeline import StrategyPipeline

    codes = codes or BIG50_CODES
    if min_stocks is None:
        min_stocks = max(5, len(codes) // 4)

    long_df, source = load_long_df(codes=codes, start=start, end=end)
    panel = StrategyPipeline.long_to_panel(long_df, min_stocks=min_stocks)

    if len(panel["close"]) == 0 or len(panel["close"].columns) == 0:
        raise RuntimeError("面板数据为空")

    logger.info(
        f"面板: {len(panel['close'].index)} 天 × "
        f"{len(panel['close'].columns)} 只股票 (source={source})"
    )
    if return_source:
        return panel, source
    return panel
