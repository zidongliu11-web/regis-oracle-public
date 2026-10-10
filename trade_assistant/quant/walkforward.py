"""Walk-forward 因子加权回测引擎（诚实样本外验证的核心）。

从 research/walkforward_research.py 忠实移植，作为生产环境的单一权威实现。

方法学要点（修复旧系统"全样本选因子 → 同一段回测"的样本内过拟合）:
1. 扩大宇宙到 300 只流动性最好股票（本地 v4_daily, 2018-2026），IC 才有统计意义。
2. 消除前视偏差：每个调仓日只用【当日之前的 trailing 窗口】计算 IC 统计量，
   据此选因子、算权重，再持有下一期。绝不用全样本 IC 选因子后在同一段回测。
3. 加权方法头对头比较：equal / ic / icir / decay / shrunk / comb。
4. 向量化 Rank-IC（pandas rank + 逐日 pearson），比 scipy.spearmanr 快 ~100x。
5. 市值中性化开关（可选）：用 amount/turn 作为市值代理做截面回归残差。
6. 诚实成本：每次调仓按换手率扣手续费/印花税/滑点；ST + 最低 20% 流动性剔除。

当前结论以每次 ``production_runner.py`` 的严格 PIT 重算为准。旧的高收益
结果已因 IC 读取未滞后完整前瞻窗口而否决，不得写回生产门禁。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Union

import numpy as np
import pandas as pd

from .logger import get_logger
from .composite import _infer_group

logger = get_logger()

# 项目根目录 / research
_PROJ = Path(__file__).resolve().parent.parent.parent
_DATA_ROOT = _PROJ / "research" / "data" / "v4_daily"
_UNIVERSE_CACHE = _PROJ / "research" / "universe_meta.json"
_RESEARCH_CACHE_PKL = _PROJ / "research" / "research_cache.pkl"
_PRODUCTION_OUT = _PROJ / "research" / "production_results.json"

MIN_DAYS = 500          # 至少 500 个交易日
MIN_RECENT = 250        # 近 3 年至少 250 个交易日
START = "2018-01-02"
# 默认不截断数据末日；显式传 end 时才用于复现实验期边界。
END = None
LOOKAHEAD_COLS = ["date", "code", "open", "high", "low", "close",
                  "volume", "amount", "turn", "isST"]

# 默认诚实成本（A股 2024 标准: 佣金 2.5bp 双边, 印花税 10bp 卖出, 滑点 10bp）
# 印花税：2018-2026 实际为千1（仅 2023-08-28~12-31 减半为万5）；此前的 0.0005 低估了 2×。
# P3-1 对齐 quantqc/quantqc.costs（同花顺口径）：印花税仅卖出 0.05%（原 0.1% 偏高）、
# 新增过户费 0.001% 双边（沪市实际收费，按组合级代理计入）。佣金万2.5 双边最低¥5 见下。
DEFAULT_COSTS = {"commission": 0.00025, "stamp": 0.0005, "transfer": 0.00001, "slippage": 0.001}
# 调仓扣成本公式: cost = turnover × (佣金×2 + 印花税×0.5 + 过户费×2 + 滑点×2)
# 印花税×0.5: 印花税只在卖出侧，调仓换手约一半是卖出。过户费双边全计（沪市）。
# 审计项#10: 最低佣金 5 元/笔。仅按比例计佣金时, 小仓位下单的实际佣金被低估——
#   单笔 ≥5 元才不触发: 需 5 < 佣金率×单仓市值 = 0.00025×(账户/20) → 账户 > 40万。
# 对 20 万账户(生产默认)等权 top-20, 单仓 1 万 → 佣金 2.5 元 < 5 元 → 实际费率翻倍。
MIN_COMMISSION_YUAN = 5.0          # 券商最低佣金（元/笔）
ACCOUNT_CAPITAL = 200_000.0        # 假设账户 20 万元（等权 top-20 → 单仓约 1 万）


def _scaled_transaction_cost(
    composition_cost: float,
    previous_exposure: float,
    new_exposure: float,
    *,
    commission: float,
    stamp: float,
    transfer: float,
    slippage: float,
) -> float:
    """Charge both invested-book turnover and cash/exposure transitions."""
    shared_exposure = min(previous_exposure, new_exposure)
    entry = max(0.0, new_exposure - previous_exposure)
    exit_ = max(0.0, previous_exposure - new_exposure)
    return (
        composition_cost * shared_exposure
        + entry * (commission + transfer + slippage)
        + exit_ * (commission + transfer + stamp + slippage)
    )


# ————————————————————————————————————————————————
# 宇宙构建（扫描 + 缓存）
# ————————————————————————————————————————————————
def _norm_code(code: str) -> str:
    """任意格式 → 6 位数字代码（处理 sh./sz. 前缀）."""
    digits = "".join(ch for ch in str(code) if ch.isdigit())
    return digits[-6:].zfill(6)


def scan_universe(n: int, pit: bool = False) -> list:
    """扫描本地 v4_daily，返回流动性最好的前 n 只代码（带缓存）.

    pit=True（点时宇宙）：不过滤"近3年仍有数据"，因此包含中途退市/长期停牌的
    股票 —— 供 walk_forward 的点时流动性门控使用，消除幸存者偏差。
    池子取全历史 amount 前 n 只（历史头部），具体入选由调仓时点实时排名决定。
    """
    cache_file = _PROJ / "research" / ("universe_meta_pit.json" if pit else "universe_meta.json")
    if cache_file.exists():
        meta = json.loads(cache_file.read_text(encoding="utf-8"))
        if meta.get("n") >= n:
            return meta["codes"][:n]

    rows = []
    files = sorted(_DATA_ROOT.glob("*.csv.gz"))
    for path in files:
        try:
            df = pd.read_csv(path, usecols=["date", "code", "amount", "tradestatus", "isST"],
                             dtype={"date": str, "code": str})
        except Exception:
            continue
        if df.empty:
            continue
        df = df[df["tradestatus"] == 1]
        if len(df) < MIN_DAYS:
            continue
        if not pit:
            recent = df[df["date"] >= "2023-06-01"]
            if len(recent) < MIN_RECENT:
                continue
        if df["isST"].tail(60).mean() > 0.5:
            continue  # 近期 ST
        rows.append({"code": _norm_code(df["code"].iloc[0]),
                     "n_days": len(df), "amount": float(df["amount"].sum())})

    uni = pd.DataFrame(rows).sort_values("amount", ascending=False).reset_index(drop=True)
    codes = uni["code"].astype(str).str.zfill(6).tolist()
    cache_file.write_text(json.dumps({"n": len(codes), "codes": codes}, ensure_ascii=False),
                          encoding="utf-8")
    logger.info(f"universe candidates: {len(codes)} (pit={pit})")
    return codes[:n]


# ————————————————————————————————————————————————
# 数据加载 → 面板
# ————————————————————————————————————————————————
def load_panel(
    codes: list, start: str = START, end: str | None = END,
) -> dict:
    frames = []
    for code in codes:
        path = _DATA_ROOT / f"{code}.csv.gz"
        if not path.exists():
            continue
        try:
            df = pd.read_csv(path, dtype={"date": str, "code": str, "isST": "int8"})
        except Exception:
            continue
        if "tradestatus" in df.columns:
            df = df[df["tradestatus"] == 1]
        df = df[df["date"] >= start]
        if end:
            df = df[df["date"] <= end]
        df["code"] = df["code"].str.replace(r"^(sh|sz)\.", "", regex=True)
        frames.append(df[LOOKAHEAD_COLS])
    long_df = pd.concat(frames, ignore_index=True)
    for col in ["open", "high", "low", "close", "volume", "amount", "turn"]:
        if col in long_df.columns:
            long_df[col] = pd.to_numeric(long_df[col], errors="coerce")
    long_df = long_df.dropna(subset=["close"])

    panel = {}
    for col in ["open", "high", "low", "close", "volume", "amount", "turn", "isST"]:
        if col not in long_df.columns:
            continue
        p = long_df.pivot_table(index="date", columns="code", values=col, aggfunc="last")
        p.index = pd.to_datetime(p.index)
        panel[col] = p.sort_index().sort_index(axis=1)
    return panel


# ————————————————————————————————————————————————
# 向量化 Rank-IC
# ————————————————————————————————————————————————
def daily_rank_ic(factor: pd.DataFrame, fwd: pd.DataFrame, min_samples: int = 30) -> pd.Series:
    """逐日截面 Rank IC（向量化）. 返回 index 为 datetime 的 Series."""
    common_dates = factor.index.intersection(fwd.index)
    cols = factor.columns.intersection(fwd.columns)
    f = factor.loc[common_dates, cols]
    r = fwd.loc[common_dates, cols]
    fr = f.rank(axis=1, pct=True)
    rr = r.rank(axis=1, pct=True)
    valid = fr.notna() & rr.notna()
    n_valid = valid.sum(axis=1)

    fr_z = fr.sub(fr.mean(axis=1), axis=0).div(fr.std(axis=1), axis=0)
    rr_z = rr.sub(rr.mean(axis=1), axis=0).div(rr.std(axis=1), axis=0)
    ic = (fr_z * rr_z).mean(axis=1)

    ic = ic.mask(n_valid < min_samples)
    ic.index = pd.to_datetime(ic.index)
    ic.name = None
    return ic


def forward_returns(close: pd.DataFrame, h: int) -> pd.DataFrame:
    return close.shift(-h) / close - 1.0


def zscore_cs(df: pd.DataFrame) -> pd.DataFrame:
    mu = df.mean(axis=1)
    sd = df.std(axis=1).replace(0, 1.0)
    return df.sub(mu, axis=0).div(sd, axis=0)


# ————————————————————————————————————————————————
# 滚动 IC 统计 + 因子选择/加权（无前视）
# ————————————————————————————————————————————————
def precompute_ic_stats(ic_series: dict, window: int) -> dict:
    """预计算每因子的滚动 IC 统计（mean / std / ir / 半衰期衰减均值）.

    用整段序列一次性 rolling，调仓时直接按日期取值——不重复切片，
    且每个 t 只用 [t-window, t] 的历史数据（rolling 天然无前视）。

    Returns: {factor: DataFrame(index=date, columns=[mean, ir, decay])}
    """
    stats = {}
    for f, ic in ic_series.items():
        s = ic.dropna()
        if len(s) < 40:
            continue
        rm = s.rolling(window, min_periods=40).mean()
        rs = s.rolling(window, min_periods=40).std(ddof=1)
        ir = rm / rs.replace(0, np.nan)
        # 半衰期指数加权的滚动均值（等效衰减权重，逐日向量化）
        lam = np.log(2) / 40.0
        decay = s.ewm(alpha=1 - np.exp(-lam), min_periods=40, adjust=False).mean()
        # 因子胜率：t 日往回看 window 天，IC 符号与 trailing 方向一致的天数占比。
        #   全 PIT——方向参考点 rm_t 只用 t 之前数据，逐日向量化。
        dirsign = np.sign(rm).replace(0, np.nan)
        hit = (np.sign(s) == dirsign).rolling(window, min_periods=40).mean()
        stats[f] = pd.DataFrame({"mean": rm, "ir": ir, "decay": decay, "hit": hit})
    return stats


def _has(row, key) -> bool:
    try:
        v = row[key]
        return bool(v is not None and not (isinstance(v, float) and np.isnan(v)))
    except (KeyError, TypeError):
        return False


def select_and_weight(method: str, stats: pd.DataFrame, top_n: int, min_ic: float = 0.003,
                      max_per_group: Optional[int] = None,
                      min_hit_rate: float = 0.0, min_ir: float = 0.0,
                      orthogonal_rho: float = 0.0,
                      corr_matrix: Optional[dict] = None):
    """根据 t 日 trailing IC 统计量选因子并计算权重.

    stats: {factor: DataFrame} —— 传入本因子在 t 日的那一行（Series）即可.

    max_per_group: 非 None 时做跨族分散——按 |mean| 降序逐个选取，
       每个因子族（_infer_group）最多取 max_per_group 个，消除单一风格
       过度集中（如 8 因子中 6 个低波）。总数仍受 top_n 限制。

    min_hit_rate / min_ir（胜率感知门槛）: >0 时只收 IC 符号正确率（hit）达标
       且 ICIR（ir）达标的因子——剔除符号不稳、时灵时不灵的因子。

    orthogonal_rho + corr_matrix（R5 正交化）: >0 时改为按 ICIR 排序 + 与
       已选因子最大 |ρ| < orthogonal_rho 贪心选取（corr_matrix: {f: {g: ρ}}，
       预计算的因子截面相关矩阵，训练期固定）。取代 |mean| 排序 + 跨族约束。

    Returns: (selected, weights)  weight 为【带符号】权重（sign*|w|）,
             composite = Σ w_f * zscore(factor_f) —— 负 IC 因子自动反转为正贡献.
    """
    names = [f for f, row in stats.items()
             if _has(row, "mean") and abs(row["mean"]) >= min_ic
             and (min_hit_rate <= 0 or not _has(row, "hit") or row["hit"] >= min_hit_rate)
             and (min_ir <= 0 or not _has(row, "ir") or row["ir"] >= min_ir)]
    if len(names) < 3:
        names = [f for f, row in stats.items() if _has(row, "mean")]
    if not names:
        return [], {}
    if orthogonal_rho > 0 and corr_matrix:
        # R5 正交化：ICIR 排序 + 与已选因子去相关贪心
        names.sort(key=lambda f: (abs(stats[f]["ir"]) if _has(stats[f], "ir")
                                  else abs(stats[f]["mean"])), reverse=True)
        picked: List[str] = []
        for f in names:
            if all(abs(corr_matrix.get(f, {}).get(p, 0.0)) < orthogonal_rho for p in picked):
                picked.append(f)
            if len(picked) >= top_n:
                break
        selected = picked if len(picked) >= 3 else names[:top_n]
    elif max_per_group and max_per_group > 0:
        names.sort(key=lambda f: abs(stats[f]["mean"]), reverse=True)
        counts: Dict[str, int] = {}
        picked = []
        for f in names:
            g = _infer_group(f)
            if counts.get(g, 0) >= max_per_group:
                continue
            counts[g] = counts.get(g, 0) + 1
            picked.append(f)
            if len(picked) >= top_n:
                break
        # 跨族约束下若凑不足 3 个，回退到无约束 top-n（保持组合有足够因子）
        selected = picked if len(picked) >= 3 else names[:top_n]
    else:
        selected = names[:top_n]

    if method == "equal":
        w = {f: 1.0 / len(selected) for f in selected}
    elif method == "ic":
        total = sum(abs(stats[f]["mean"]) for f in selected)
        w = {f: abs(stats[f]["mean"]) / total for f in selected}
    elif method == "icir":
        total = sum(abs(stats[f]["ir"]) for f in selected)
        w = {f: abs(stats[f]["ir"]) / total for f in selected}
    elif method == "decay":
        total = sum(abs(stats[f]["decay"]) for f in selected)
        w = {f: abs(stats[f]["decay"]) / total for f in selected}
    elif method == "shrunk":
        total = sum(abs(stats[f]["ir"]) for f in selected)
        w_ir = {f: abs(stats[f]["ir"]) / total for f in selected}
        w = {f: 0.5 * w_ir[f] + 0.5 / len(selected) for f in selected}
    elif method == "comb":
        total = sum(abs(stats[f]["decay"]) for f in selected)
        w = {f: abs(stats[f]["decay"]) / total for f in selected}
    else:
        raise ValueError(method)

    signed = {f: w[f] * (1.0 if stats[f]["mean"] > 0 else -1.0) for f in selected}
    return selected, signed


def _build_composite(factors: dict, selected: list, weights: dict, t: pd.Timestamp,
                     neutral_size: bool, mcap, sector_map: Optional[dict] = None) -> pd.Series:
    """构建 t 日复合因子分：截面 zscore（可选行业/板块相对化 + 市值中性化）× 带符号权重."""
    comp = None
    for f in selected:
        zrow = factors[f].loc[t]
        zmu = zrow.mean()
        zsd = zrow.std()
        z = (zrow - zmu) / zsd if zsd > 0 else zrow * 0.0
        if sector_map is not None:
            # R3 行业中性化：z 减行业/板块均值（代理行业），剔除行业集中暴露
            sec = pd.Series(sector_map, dtype="object").reindex(z.index)
            z = z - z.groupby(sec).transform("mean")
        if neutral_size and mcap is not None:
            size = np.log(mcap.loc[t])
            idx = z.notna() & size.notna() & np.isfinite(size)
            if idx.sum() > 20:
                x = np.log(size[idx].astype(float))
                y = z[idx].astype(float)
                ok = np.isfinite(x) & np.isfinite(y)
                beta = np.polyfit(x[ok], y[ok], 1) if ok.sum() > 1 else [0.0, 0.0]
                resid = pd.Series(np.nan, index=z.index)
                resid[idx] = y - (beta[0] * x + beta[1])
                z = resid
        if comp is None:
            comp = z * weights[f]
        else:
            comp = comp.add(z * weights[f], fill_value=0.0)
    return comp


# ————————————————————————————————————————————————
# 市场状态过滤（可选）
# ————————————————————————————————————————————————
def _regime_exposure(close: pd.DataFrame, t: pd.Timestamp, regime: dict) -> float:
    """市场状态过滤（仅用 t 之前数据）.

    regime = {"style": "ma", "ma_full": 60, "ma_zero": 120}
    style:
      "ret20": 等权市场 20 日累计收益, full=-2% 半仓, zero=-5% 空仓
      "ma"   : 等权市场指数跌破 ma_full 日线 → 半仓; 跌破 ma_zero → 空仓
      "vol"  : 波动率目标, 目标年化 25%, 暴露 = min(1, 0.25/年化已实现波动)
    """
    style = regime.get("style", "ma")
    close = close.loc[:t]

    if style == "ret20":
        ret = float(close.pct_change().tail(20).mean(axis=1).sum())
        if ret <= regime["zero"]:
            return 0.0
        if ret <= regime["full"]:
            return 0.5
        return 1.0

    if style == "ma":
        mkt = close.mean(axis=1)
        ma_full = float(mkt.tail(regime["ma_full"]).mean())
        ma_zero = float(mkt.tail(regime["ma_zero"]).mean())
        price = float(mkt.iloc[-1])
        if price <= ma_zero:
            return 0.0
        if price <= ma_full:
            return 0.5
        return 1.0

    if style == "vol":
        daily = close.pct_change().tail(60).mean(axis=1)
        rv = float(daily.std(ddof=1)) * np.sqrt(252)
        if not np.isfinite(rv) or rv <= 0:
            return 1.0
        target = regime.get("target_vol", 0.25)
        return float(min(1.0, target / rv))

    return 1.0


# ————————————————————————————————————————————————
# Walk-forward 回测主引擎
# ————————————————————————————————————————————————
# fixed_factors: 非 None 时完全绕过 IC 选择（副线固定因子族/等权多因子用）。
#   dict 形如 {factor: 带符号权重}，权重在回测前预注册、全程固定；决策时刻只读
#   因子 zscore（已发生数据），彻底消除前视源。此时 signal_lag/ic_stats 不参与决策。
def walk_forward(panel: dict, factors: dict, ic_stats: dict, horizon: int,
                 method: str, topk: int, holding: int, window: int,
                 costs: dict, neutral_size: bool,
                 min_ic: float = 0.003,
                 regime: Optional[dict] = None,
                 buffer_mult: float = 1.0,
                 port_vol_target: float = 0.0,
                 dynamic_vol: bool = False,
                 weighting: str = "equal",
                 top_n: int = 8,
                 pit_topk: int = 0,
                 min_commission_yuan: float = MIN_COMMISSION_YUAN,
                 account_capital: float = ACCOUNT_CAPITAL,
                 max_per_group: Optional[int] = None,
                 min_hit_rate: float = 0.0,
                 min_ir: float = 0.0,
                 dd_gate_pct: float = 0.0,
                 dd_exposure: float = 0.6,
                 sector_map: Optional[dict] = None,
                 blend_ics: Optional[dict] = None,
                 blend_weights: Optional[dict] = None,
                 corr_matrix: Optional[dict] = None,
                 orthogonal_rho: float = 0.0,
                 start: Optional[str] = None,
                 end: Optional[str] = None,
                 signal_lag: Optional[Union[int, Dict[int, int]]] = None,
                 fixed_factors: Optional[Dict[str, float]] = None) -> dict:
    close = panel["close"]
    open_ = panel["open"] if "open" in panel and panel["open"] is not None else close
    dates = close.index
    rebal_dates = dates[::holding]
    # 出样确认：可选评估窗口——只限制【评估/交易】日期，滚动 IC 统计仍用全历史
    if start is not None:
        rebal_dates = rebal_dates[rebal_dates >= pd.Timestamp(start)]
    if end is not None:
        rebal_dates = rebal_dates[rebal_dates <= pd.Timestamp(end)]

    # 市值代理（用于中性化）
    if neutral_size and "turn" in panel:
        mcap = panel["amount"] / panel["turn"].replace(0, np.nan)
    else:
        mcap = None

    fwd = forward_returns(close, horizon)

    # 涨跌停幅度（ST 5% / 创业板&科创板 20% / 北交所 30% / 主板 10%）
    # 注：ST 已被上方 valid 过滤，持仓中的"新变 ST"极罕见，忽略不计。
    def _limit_pct(code):
        c = str(code)
        if c[:3] in ("300", "301") or c[:3] in ("688", "689"):
            return 0.20
        if c[0] in ("4", "8", "9"):
            return 0.30
        return 0.10

    nav = 1.0
    nav_log = []
    daily_rets = []
    positions = []  # 当前持仓 code list（等权）
    total_turnover = 0.0
    n_rebal = 0
    ic_of_portfolio = []
    trades = []
    # 组合自身已实现收益序列（用于滞后一期的波动率目标）
    own_ret_history = []
    prev_d0 = None  # 上一调仓的成交日（t+1）
    previous_exposure = 0.0
    weights_now = {}  # 当前持仓权重
    dd_reduced = False  # R2 回撤熔断是否处于降暴露状态（创新高后解除）

    for i, t in enumerate(rebal_dates):
        t_pos = dates.get_loc(t)
        if t_pos + horizon > len(dates) - 1:
            break

        # —— 决策（严格 PIT 默认） ——
        # IC(d)=rankic(因子_d, fwd[d, d+h]) 嵌入 h 天前视收益；在决策时刻 t，最新完全
        # 实现的 IC 是 IC(t-h)（其前视窗口 [t-h, t] 终点=t）。故默认把统计读取滞后 h
        # 天，选择/方向/权重只用已实现 IC，消除前视。signal_lag 可显式覆盖（0=旧泄漏口径，仅测试用）。
        comp = None
        if blend_ics:
            # R4 多周期融合：对每个 horizon 独立选因子/建复合分，再按 blend_weights 加权
            for h, hics in blend_ics.items():
                if signal_lag is None:
                    lag = h                      # 严格 PIT：每腿用自身 horizon
                elif isinstance(signal_lag, dict):
                    lag = signal_lag.get(h, 0)
                else:
                    lag = signal_lag
                tt = dates[max(0, t_pos - lag)] if lag else t
                hstats = {}
                for f, sdf in hics.items():
                    if tt in sdf.index:
                        row = sdf.loc[tt]
                        if _has(row, "mean"):
                            hstats[f] = row
                if not hstats:
                    continue
                hsel, hw = select_and_weight(method, hstats, top_n=top_n, min_ic=min_ic,
                                             max_per_group=max_per_group,
                                             min_hit_rate=min_hit_rate, min_ir=min_ir,
                                             orthogonal_rho=orthogonal_rho,
                                             corr_matrix=corr_matrix)
                if not hsel:
                    continue
                hcomp = _build_composite(factors, hsel, hw, t, neutral_size, mcap, sector_map)
                if hcomp is None:
                    continue
                hwgt = blend_weights.get(h, 1.0 / len(blend_ics)) if blend_weights else 1.0
                comp = hcomp * hwgt if comp is None else comp + hcomp * hwgt
        else:
            if fixed_factors is not None:
                # 副线：固定因子族/等权多因子 —— 决策完全不读 IC，消除前视源。
                # 因子集与带符号权重在回测前预注册；随时间变化的只有因子 zscore 本身
                # （只用已发生数据）。portfolio_ic 仍按复合分 vs 前瞻收益测量预测力。
                comp = _build_composite(factors, list(fixed_factors), fixed_factors,
                                        t, neutral_size, mcap, sector_map)
            else:
                if signal_lag is None:
                    lag = horizon                  # 严格 PIT 默认
                elif isinstance(signal_lag, dict):
                    lag = signal_lag.get(horizon, 0)
                else:
                    lag = signal_lag
                tt = dates[max(0, t_pos - lag)] if lag else t
                stats = {}
                for f, sdf in ic_stats.items():
                    if tt in sdf.index:
                        row = sdf.loc[tt]
                        if _has(row, "mean"):
                            stats[f] = row
                if not stats:
                    continue
                selected, weights = select_and_weight(method, stats, top_n=top_n, min_ic=min_ic,
                                                      max_per_group=max_per_group,
                                                      min_hit_rate=min_hit_rate, min_ir=min_ir,
                                                      orthogonal_rho=orthogonal_rho,
                                                      corr_matrix=corr_matrix)
                if not selected:
                    continue
                comp = _build_composite(factors, selected, weights, t, neutral_size, mcap, sector_map)
        if comp is None:
            continue

        # 持仓选择：剔除 ST / 流动性差
        valid = comp.notna()
        if "isST" in panel:
            valid &= panel["isST"].loc[t].eq(0) if t in panel["isST"].index else valid
        amt20 = panel["amount"].loc[:t].tail(20).mean()
        if amt20 is not None and len(amt20.dropna()) > 0:
            if pit_topk > 0:
                # 点时流动性门控：仅保留近20日成交额排名前 pit_topk 的股票。
                # 宇宙含退市/失效股票时，它们在失效日后 amt20=NaN → 自动出局，
                # 相当于逐日重建"当时流动性最好的前 N"，消除幸存者偏差。
                amt_rank = amt20.rank(ascending=False)
                valid &= amt20.notna() & (amt_rank <= pit_topk)
            else:
                liq_thr = amt20.dropna().quantile(0.2)
                valid &= amt20.notna() & (amt20 >= liq_thr)

        comp = comp[valid]
        # —— 目标持仓选择：换手缓冲（buffer_mult=1 为纯 top-k；>1 保留仍在
        #    排名缓冲带内的旧仓，只换明显掉队的，以降低换手与成本拖累） ——
        ranked = comp.sort_values(ascending=False)
        rank_order = list(ranked.index)

        # 成交日 T+1：决策在 t 收盘生成 → 在 d0=t+1 开盘执行。
        #   涨停开盘 → 买不进；跌停开盘 → 卖不出（顺延进本期）。
        if t_pos + 1 >= len(dates):
            break
        d0 = dates[t_pos + 1]
        d0_open = open_.loc[d0]
        d0_prev_close = close.loc[t]

        def _fillable(code):
            o, pc = d0_open.get(code, np.nan), d0_prev_close.get(code, np.nan)
            if pd.isna(o) or pd.isna(pc):
                return False
            return o < pc * (1 + _limit_pct(code)) * 0.995  # 未涨停开盘 → 可买

        blocked = []  # 跌停开盘卖不出 → 顺延进本期
        if prev_d0 is not None and positions:
            for s in positions:
                o, pc = d0_open.get(s, np.nan), d0_prev_close.get(s, np.nan)
                if pd.isna(o) or pd.isna(pc):
                    continue
                if o <= pc * (1 - _limit_pct(s)) * 1.005:
                    blocked.append(s)

        keep = []
        if buffer_mult > 1.0 and positions:
            keep_cutoff = int(topk * buffer_mult)
            keep = [s for s in positions
                    if s in rank_order and rank_order.index(s) < keep_cutoff
                    and s not in blocked]
        targets = list(blocked) + keep
        for s in rank_order:
            if len(targets) >= topk:
                break
            if s in targets:
                continue
            if _fillable(s):
                targets.append(s)
        if len(targets) < 3:
            continue

        # —— 调仓与成本 ——
        old_set = set(positions)
        new_set = set(targets)
        turnover = len(old_set ^ new_set) / topk
        total_turnover += turnover
        # 审计项#10: 最低佣金 5 元/笔。单仓市值 = 账户/topk → 折算成费率下限:
        #   max(比例佣金, 5元×topk/账户)。20万账户 top20 → 佣金费率 0.025%→0.05%(翻倍)。
        eff_comm = costs["commission"]
        if min_commission_yuan > 0 and topk > 0 and account_capital > 0:
            eff_comm = max(eff_comm, min_commission_yuan * topk / account_capital)
        composition_cost = turnover * (
            eff_comm * 2 + costs["stamp"] * 0.5
            + costs.get("transfer", 0.0) * 2 + costs["slippage"] * 2
        )
        n_rebal += 1
        for s in sorted(old_set - new_set):
            trades.append({"date": str(d0)[:10], "stock": s, "action": "SELL"})
        for s in sorted(new_set - old_set):
            trades.append({"date": str(d0)[:10], "stock": s, "action": "BUY"})

        # 组合内权重：等权 或 按复合分数排名加权
        #   rank=线性分数加权 | rank_sqrt=半浓度（偏分散） | rank_2=高浓度（偏集中）
        if weighting.startswith("rank"):
            wc = comp.loc[targets].rank(ascending=True)
            if weighting == "rank_sqrt":
                wc = wc ** 0.5
            elif weighting == "rank_2":
                wc = wc ** 2
            new_weights = (wc / wc.sum()).to_dict()
        else:
            new_weights = {s: 1.0 / len(targets) for s in targets}

        def _wmean(sr, wdict, cols):
            cols = [c for c in cols if c in sr.index and pd.notna(sr[c])]
            if not cols:
                return 0.0
            wsum = sum(wdict.get(c, 0.0) for c in cols)
            if wsum > 0:
                return float(sum(wdict[c] * sr[c] for c in cols) / wsum)
            return float(sr[cols].mean())

        # 组合 IC：复合因子与前瞻收益的截面相关
        fwd_t = fwd.loc[t].reindex(comp.index)
        both = pd.DataFrame({"comp": comp, "fwd": fwd_t}).dropna()
        if len(both) >= 30:
            ic_of_portfolio.append(float(both["comp"].corr(both["fwd"], method="spearman")))

        # —— 持有期收益（诚实时序：T 收盘决策 → T+1 开盘成交 → T'+1 开盘卖出） ——
        exposure = 1.0
        if regime is not None:
            exposure = _regime_exposure(close, t, regime)
        if port_vol_target > 0 and len(own_ret_history) >= 60:
            # 组合自身已实现波动率目标（滞后一期，无前视）
            rv = float(pd.Series(own_ret_history[-252:]).std(ddof=1)) * np.sqrt(252)
            if np.isfinite(rv) and rv > 0:
                target = port_vol_target
                if dynamic_vol and len(own_ret_history) >= 10:
                    # 动态风险预算：近10日波动相对1年基线尖峰 → 自动降低目标
                    rv_short = float(pd.Series(own_ret_history[-10:]).std(ddof=1)) * np.sqrt(252)
                    if np.isfinite(rv_short) and rv > 0 and (rv_short / rv) > 1.3:
                        target = port_vol_target * 0.6
                exposure *= float(min(1.0, target / rv))

        # R2 回撤熔断覆盖层：NAV 跌破峰值 -dd_gate_pct → 下一期降暴露 dd_exposure；
        #   NAV 创新高 → 解除。只用历史已实现 NAV（nav_log），无未来函数。
        if dd_gate_pct > 0 and len(nav_log) >= 5:
            peak = max(r["nav"] for r in nav_log)
            if nav >= peak:
                dd_reduced = False
            elif nav / peak - 1.0 <= -dd_gate_pct:
                dd_reduced = True
        if dd_reduced:
            exposure *= dd_exposure

        # 1) 上一段持仓在 [prev_d0+1, d0) 的 close-to-close 中间日收益（不含调仓日 d0）
        if prev_d0 is not None and positions:
            seg = dates[dates.get_loc(prev_d0) + 1:dates.get_loc(d0)]
            for dd in seg:
                dd_prev = dates[dates.get_loc(dd) - 1]
                rr = (close.loc[dd] / close.loc[dd_prev] - 1.0)
                gross = _wmean(rr, weights_now, positions)
                own_ret_history.append(gross)
                net = gross * exposure
                nav *= (1.0 + net)
                daily_rets.append(net)
                nav_log.append({"date": str(dd)[:10], "nav": round(nav, 4)})

        # 2) 调仓日 d0：旧仓开盘卖出（隔夜跳空 gap）× 新仓开盘买入（当日 intraday）
        old_gap = 0.0
        if prev_d0 is not None and positions:
            rr = (d0_open / d0_prev_close - 1.0)
            old_gap = _wmean(rr, weights_now, positions)
        rr = (close.loc[d0] / d0_open - 1.0)
        new_intra = _wmean(rr, new_weights, targets)
        raw_day_ret = (1.0 + old_gap) * (1.0 + new_intra) - 1.0
        own_ret_history.append(raw_day_ret)
        transaction_cost = _scaled_transaction_cost(
            composition_cost,
            previous_exposure,
            exposure,
            commission=eff_comm,
            stamp=costs["stamp"],
            transfer=costs.get("transfer", 0.0),
            slippage=costs["slippage"],
        )
        # 隔夜跳空只作用于昨日真实暴露；日内收益作用于今日开盘后暴露。
        net = (
            (1.0 + old_gap * previous_exposure)
            * (1.0 + new_intra * exposure)
            - 1.0
            - transaction_cost
        )
        nav *= (1.0 + net)
        daily_rets.append(net)
        nav_log.append({"date": str(d0)[:10], "nav": round(nav, 4)})

        positions = targets
        weights_now = new_weights
        prev_d0 = d0
        previous_exposure = exposure

    # —— 尾段：最后一段持仓持至面板末端（close-to-close） ——
    # 注意：评估窗口 end 过滤只约束了 rebal_dates，这里必须同样约束尾段，
    # 否则分段时间切片（5 折验证）会把后续年份的持仓收益算进本段，数字虚高。
    if prev_d0 is not None and positions:
        seg = dates[dates.get_loc(prev_d0) + 1:]
        if end is not None:
            seg = seg[seg <= pd.Timestamp(end)]
        for dd in seg:
            dd_prev = dates[dates.get_loc(dd) - 1]
            rr = (close.loc[dd] / close.loc[dd_prev] - 1.0)
            gross = _wmean(rr, weights_now, positions)
            own_ret_history.append(gross)
            net = gross * exposure
            nav *= (1.0 + net)
            daily_rets.append(net)
            nav_log.append({"date": str(dd)[:10], "nav": round(nav, 4)})

    # —— 统计 ——
    if len(daily_rets) < 30:
        return {"error": "样本不足", "method": method, "nav_log": nav_log}

    rs = pd.Series(daily_rets)
    cum = (1 + rs).cumprod()
    total_ret = float(cum.iloc[-1] - 1)
    n = len(rs)
    annual = (1 + total_ret) ** (252 / n) - 1 if n > 0 else 0.0
    sharpe = float(rs.mean() / rs.std() * np.sqrt(252)) if rs.std() > 0 else 0.0
    roll_max = cum.cummax()
    max_dd = float(((cum - roll_max) / roll_max).min())
    calmar = annual / abs(max_dd) if max_dd < 0 else 0.0

    # 基准：等权全宇宙
    bench_rets = close.pct_change().mean(axis=1).dropna()
    bench_rets = bench_rets[bench_rets.index.isin(rebal_dates[1:])]
    bench_annual = 0.0
    if len(bench_rets) > 30:
        bench_total = float((1 + bench_rets).prod() - 1)
        bench_annual = (1 + bench_total) ** (252 / len(bench_rets)) - 1

    # 分年度收益（从 NAV 轨迹）
    period_rets = {}
    prev_nav = 1.0
    prev_year = None
    year_start = None
    for rec in nav_log:
        year = rec["date"][:4]
        if prev_year is not None and year != prev_year:
            period_rets[prev_year] = round((year_start - prev_nav) / prev_nav * 100, 1)
            prev_nav = year_start
        prev_year = year
        year_start = rec["nav"]
    if prev_year and year_start is not None and prev_nav > 0:
        period_rets[prev_year] = round((year_start - prev_nav) / prev_nav * 100, 1)

    # 正收益年份占比（稳定盈利的关键指标）
    years_pos = sum(1 for v in period_rets.values() if v > 0)
    n_years = len(period_rets)

    return {
        "method": method, "topk": topk, "holding": holding, "window": window,
        "n_rebalances": n_rebal, "total_turnover": round(total_turnover, 1),
        "metrics": {
            "total_return_pct": round(total_ret * 100, 2),
            "annualized_pct": round(annual * 100, 2),
            "sharpe": round(sharpe, 3),
            "max_dd_pct": round(max_dd * 100, 1),
            "calmar": round(calmar, 3),
            "win_rate_pct": round(float((rs > 0).mean() * 100), 1),
            "avg_daily_pct": round(float(rs.mean() * 100), 4),
            "portfolio_ic": round(float(np.mean(ic_of_portfolio)), 4) if ic_of_portfolio else 0.0,
        },
        "period_returns": period_rets,
        "years_positive": years_pos,
        "years_total": n_years,
        "benchmark_annual_pct": round(bench_annual * 100, 2),
        "alpha_pct": round((annual - bench_annual) * 100, 2),
        "n_trades": len(trades),
        "nav_log": nav_log,
        "trades": trades,
    }


# ————————————————————————————————————————————————
# 因子研究（全样本诚实统计）
# ————————————————————————————————————————————————
def factor_stats(factors: dict, ic_series: dict) -> list:
    rows = []
    for f, ics in ic_series.items():
        vals = ics.dropna()
        if len(vals) < 40:
            continue
        mean_ic = float(vals.mean())
        std_ic = float(vals.std(ddof=1))
        ir = mean_ic / std_ic if std_ic > 0 else 0.0
        pos = float((vals > 0).mean())
        # IC 稳定性：分三段 IC 方向一致性
        n3 = max(1, len(vals) // 3)
        seg = [float(vals.iloc[i * n3:(i + 1) * n3].mean()) for i in range(3)]
        stable = all(s > 0 for s in seg) or all(s < 0 for s in seg)
        rows.append({
            "factor": f, "mean_ic": round(mean_ic, 5), "icir": round(ir, 3),
            "ic_pos_ratio": round(pos, 3), "n": len(vals),
            "seg1": round(seg[0], 4), "seg2": round(seg[1], 4), "seg3": round(seg[2], 4),
            "sign_stable": bool(stable),
        })
    rows.sort(key=lambda r: abs(r["mean_ic"]), reverse=True)
    return rows


def tier_of(r: dict) -> str:
    """按诚实全样本 IC 统计给因子分级."""
    m = abs(r["mean_ic"])
    if m >= 0.02 and r["sign_stable"]:
        return "t1_excellent"
    if m >= 0.01 and (r["sign_stable"] or 0.35 <= r["ic_pos_ratio"] <= 0.65):
        return "t2_good"
    if m >= 0.005:
        return "t3_marginal"
    return "t4_weak"


def build_factor_library(panel: dict, factors: dict, horizon: int = 10):
    """计算日度 Rank-IC 序列，返回 (factor_stats_rows, ic_series)."""
    close = panel["close"]
    fwd = forward_returns(close, horizon)
    ic_series = {}
    min_samples = max(20, int(len(close.columns) * 0.2))
    for f, df in factors.items():
        ic_series[f] = daily_rank_ic(df, fwd, min_samples=min_samples)
    rows = factor_stats(factors, ic_series)
    for r in rows:
        r["tier"] = tier_of(r)
    return rows, ic_series
