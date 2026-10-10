#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data_loader.py — 数据加载与 PIT 对齐

数据源：
  1. backtest_project/data/universe_daily.parquet（build_panel.py 构建，全市场日线，真实）
  2. backtest_project/data/stock_meta.parquet（首末日期 + 行业分类）
  3. backtest_project/data/pit_fundamentals.parquet（baostock peTTM/pbMRQ，PIT 日度值）
  4. backtest_project/data/pit_pe_pb.pkl（EP/BP 因子用 PIT PE/PB 面板）

PIT 对齐规则（日线粒度实现，与任务书分钟级规则对应的日线版本）：
  - 日 K 线：当日 15:00 后可得 → 决策可用时点 = 次日 09:30（即 T 日信号，T+1 日成交）
  - 财务 PE/PB：baostock 日度 peTTM/pbMRQ 即为"截至当日已披露财务"的时点值（PIT），
    天然满足"披露日次日 09:30 可用"——T 日可用 T 日值，无前视
  - 指数成分 / ST / 北向：日 K 的 is_st、trade_status 为当日状态，仅当日收盘后可用
    → 决策当日不读当日状态（信号 T 收盘生成、T+1 成交，成交时 T+1 的 is_st/停牌状态
      用于涨跌停与可交易性检查，不用于信号）
  - 时点选股池：环境起始日 D 构建，仅用 D 及之前的日 K（防幸存者偏差 + 防前视）

数据校验：OHLC 逻辑、成交量非负、时间戳连续性、缺失标记 NaN（不补全）。
"""
from __future__ import annotations
import json, os
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

from .data_source import PROVENANCE, record_panel_access, data_layer_health

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / 'data'
CONFIG_DIR = ROOT / 'config'

# 老登股行业排除（证监会行业分类代码前缀）
EXCLUDED_INDUSTRY_PREFIXES = {
    'J66': '银行', 'J67': '证券', 'J68': '保险',
    'B06': '煤炭', 'B07': '石油开采', 'C25': '石油加工',
    'D44': '电力', 'D45': '燃气',
    'G53': '铁路', 'G54': '公路', 'G55': '港口', 'G56': '航空机场',
    'C15': '白酒/啤酒', 'C14': '乳品',
    'C31': '钢铁', 'C32': '有色', 'C30': '水泥', 'C26': '化工',
}
# 对应任务书行业名（用于报告展示）
EXCLUDED_INDUSTRY_LABELS = ('银行','保险','证券','石油','煤炭','电力','燃气','高速公路',
                            '港口','铁路','机场','白酒','啤酒','乳品','钢铁','有色','水泥','化工')


def load_config() -> dict:
    cfg = json.loads((CONFIG_DIR / 'environments.json').read_text())
    return cfg


def load_panel(columns: Optional[List[str]] = None) -> pd.DataFrame:
    """加载全市场日线面板（universe_daily.parquet）。

    Step 0.5 统一数据层接入：批量冻结面板仍读本地 parquet 为权威源（统一接口为单票粒度+
    实时/延迟源，无法承载 5163×1200 冻结 PIT 面板，见 data_source.py 诚实适配），
    但每次加载做数据层健康检查并记录 source/as_of/health_status 到 PROVENANCE。
    """
    p = DATA_DIR / 'universe_daily.parquet'
    if not p.exists():
        raise FileNotFoundError(f'面板不存在: {p}。先运行 scripts/build_panel.py')
    df = pd.read_parquet(p, columns=columns) if columns else pd.read_parquet(p)
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    as_of = df['trade_date'].max().strftime('%Y-%m-%d') if len(df) else 'unknown'
    health = record_panel_access('universe_daily.parquet', as_of=as_of,
                                 note='全市场日线冻结面板（含退市股与ST），本地 parquet 权威源')
    if health is None:
        print('[data] ⚠ 统一数据层不可达，批量面板走本地 parquet（如实降级）', flush=True)
    return df


def load_meta() -> pd.DataFrame:
    df = pd.read_parquet(DATA_DIR / 'stock_meta.parquet')
    as_of = '2026-08-07'  # stock_meta.parquet 快照口径（2026-08 行业快照）
    record_panel_access('stock_meta.parquet', as_of=as_of,
                        note='行业/区间元数据快照（2026-08），本地 parquet 权威源')
    return df


def load_pit_fundamentals() -> pd.DataFrame:
    """加载 baostock PIT PE/PB 面板（peTTM/pbMRQ，日度，PIT）。"""
    p = DATA_DIR / 'pit_fundamentals.parquet'
    if not p.exists():
        PROVENANCE.record('load_pit_fundamentals', source='missing', as_of='missing',
                          health_status='unavailable', participates_in_decision=False,
                          note='PIT 面板缺失，返回空表')
        return pd.DataFrame()
    df = pd.read_parquet(p)
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    as_of = df['trade_date'].max().strftime('%Y-%m-%d') if len(df) else 'unknown'
    record_panel_access('pit_fundamentals.parquet', as_of=as_of,
                        note='baostock 日度 peTTM/pbMRQ（PIT 按披露日），本地 parquet 权威源')
    return df


def dump_data_provenance(path) -> dict:
    """写数据来源审计 JSON（source/as_of/health_status/participates_in_decision）。"""
    PROVENANCE.dump(path)
    return {'n_records': len(PROVENANCE.records), 'summary': PROVENANCE.health_summary()}


def ensure_pit_fundamentals(codes: List[str], start: str = '2021-09-01',
                            end: str = '2024-12-31', force: bool = False) -> pd.DataFrame:
    """
    为给定 ts_codes 拉取 baostock 日度 PIT PE/PB（peTTM/pbMRQ），增量缓存到
    data/pit_fundamentals.parquet。baostock 非 HTTP，本机实测可用。
    返回合并后的全量 PIT 面板（可能覆盖更多股票）。
    """
    p = DATA_DIR / 'pit_fundamentals.parquet'
    existing = load_pit_fundamentals() if p.exists() else pd.DataFrame()
    have = set(existing['ts_code'].unique()) if len(existing) else set()
    need = [c for c in codes if c not in have]
    if not need and not force:
        return existing
    if not need:
        return existing

    import baostock as bs
    for v in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy']:
        os.environ.pop(v, None)
    lg = bs.login()
    if lg.error_code != '0':
        PROVENANCE.record('ensure_pit_fundamentals', source='baostock',
                          as_of='na', health_status='unavailable',
                          participates_in_decision=False,
                          note=f'baostock 登录失败: {lg.error_msg}')
        print(f'[fund] baostock 登录失败: {lg.error_msg}，EP/BP 因子将不可用', flush=True)
        return existing

    def _to_bs(ts: str) -> str:
        c = ts.split('.')[0]
        return f'sh.{c}' if ts.endswith('.SH') else f'sz.{c}'

    parts, n_ok = [], 0
    for i, ts in enumerate(need):
        try:
            rq = bs.query_history_k_data_plus(
                _to_bs(ts), 'date,peTTM,pbMRQ', start_date=start, end_date=end,
                frequency='d', adjustflag='3')
            rows = []
            while (rq.error_code == '0') & rq.next():
                r = rq.get_row_data()
                rows.append((ts, r[0], r[1], r[2]))
        except Exception:
            rows = []
        if rows:
            parts.append(pd.DataFrame(rows, columns=['ts_code', 'trade_date', 'pe_ttm', 'pb_mrq']))
            n_ok += 1
        if (i + 1) % 50 == 0:
            print(f'[fund] 已拉 {i+1}/{len(need)}（成功 {n_ok}）', flush=True)
    bs.logout()
    if parts:
        new = pd.concat(parts, ignore_index=True)
        merged = pd.concat([existing, new], ignore_index=True) if len(existing) else new
        merged['trade_date'] = pd.to_datetime(merged['trade_date'])
        for c in ['pe_ttm', 'pb_mrq']:
            merged[c] = pd.to_numeric(merged[c], errors='coerce')
        merged = merged.drop_duplicates(['ts_code', 'trade_date']).sort_values(['ts_code', 'trade_date'])
        merged.to_parquet(p, index=False)
        PROVENANCE.record('ensure_pit_fundamentals', source='baostock',
                          as_of='2024-12-31', health_status='healthy',
                          participates_in_decision=True,
                          note=f'增量拉取 PIT PE/PB：新增 {n_ok} 只，累计 {merged["ts_code"].nunique()} 只')
        print(f'[fund] PIT PE/PB 已写 {len(merged)} 行 / {merged["ts_code"].nunique()} 只（新增 {n_ok}）', flush=True)
        return merged
    print('[fund] 无新增（全部失败）', flush=True)
    return existing


def is_excluded_industry(industry: str) -> Tuple[bool, str]:
    """按证监会行业代码前缀判断是否属于'老登股'排除板块。industry 形如 'J66货币金融服务'。"""
    if not industry or not isinstance(industry, str):
        return False, ''
    head = industry[:3]
    if head in EXCLUDED_INDUSTRY_PREFIXES:
        return True, EXCLUDED_INDUSTRY_PREFIXES[head]
    # 部分行业名不含代码前缀
    for label in EXCLUDED_INDUSTRY_LABELS:
        if label in industry:
            return True, label
    return False, ''


def trading_days_before(dates: np.ndarray, d: pd.Timestamp, n: int) -> Optional[pd.Timestamp]:
    """dates 为升序交易日 ndarray；返回 d 之前第 n 个交易日（含 d 往前数）。"""
    ds = dates[dates <= d]
    if len(ds) < n:
        return None
    return pd.Timestamp(ds[-n])


def build_timepoint_pool(panel: pd.DataFrame, env_start: str, cfg: dict,
                         meta: Optional[pd.DataFrame] = None) -> Tuple[Set[str], dict]:
    """
    构建时点选股池（防幸存者偏差 + 排除 ST/停牌/次新/老登股）。

    返回 (ts_codes集合, 统计dict)。
    """
    d0 = pd.Timestamp(env_start)
    lookback = cfg['backtest']['lookback_days']          # 90 自然日
    need = d0 - pd.Timedelta(days=lookback)
    seg = panel[(panel['trade_date'] >= need) & (panel['trade_date'] <= d0)].copy()
    if len(seg) == 0:
        return set(), {'n_pool': 0, 'reason': '无数据'}

    # 每只股票截至 d0 的自身交易天数（用全面板，非 90 天窗口）——用于上市时长检查
    hist_len_map = panel[panel['trade_date'] <= d0].groupby('ts_code').size()

    # 存活判定：d0 附近(近10个交易日)有交易记录
    alive_window = d0 - pd.Timedelta(days=15)
    alive = seg[seg['trade_date'] >= alive_window]['ts_code'].unique()

    def _agg(g: pd.DataFrame) -> dict:
        tr = g['turnover_rate'].dropna()
        pct = g['pct_chg'].dropna()
        mv = g['float_mv'].dropna()
        susp = (g['trade_status'] == 0).sum()
        n_bars = len(g)
        return {
            'n_bars': n_bars,
            'susp_days': susp,
            'turn20': tr.tail(20).mean() if len(tr.tail(20)) >= 10 else np.nan,
            'vol60': float(np.nanstd(pct.tail(60)) * np.sqrt(250)) if len(pct.tail(60)) >= 30 else np.nan,
            'float_mv': mv.tail(5).median() if len(mv.tail(5)) >= 1 else np.nan,
            'is_st_last': int(g['is_st'].iloc[-1]) if len(g) else 0,
            'first_bar': g['trade_date'].iloc[0],
        }

    agg = {code: _agg(g) for code, g in seg.groupby('ts_code')}
    codes = set(agg.keys())

    stats = {
        'n_all_with_data': len(codes),
        'n_alive': int(len(alive)),
    }
    pools = {'alive': [], 'not_st': [], 'not_susp': [], 'listed_ok': [], 'not_old': []}

    def _check_old(row: dict) -> Tuple[bool, str]:
        mv = row['float_mv']
        if mv is not None and not np.isnan(mv) and mv > cfg['backtest']['max_listed_market_cap']:
            return True, f'市值{mv/1e8:.0f}亿>2000亿'
        t = row['turn20']
        if t is not None and not np.isnan(t) and t < cfg['backtest']['min_turnover_20d']:
            return True, f'换手{t*100:.1f}%<0.8%'
        v = row['vol60']
        if v is not None and not np.isnan(v) and v < cfg['backtest']['min_volatility_annual']:
            return True, f'波动{v*100:.0f}%<25%'
        return False, ''

    for code in codes:
        row = agg[code]
        if code not in alive:
            continue
        pools['alive'].append(code)
        if row['is_st_last']:
            continue
        pools['not_st'].append(code)
        # 停牌>30天（近90天窗口内停牌天数）
        if row['susp_days'] > cfg['backtest']['max_suspension_days']:
            continue
        pools['not_susp'].append(code)
        # 上市不足60日：截至 d0 自身交易天数 < 60（真实上市时长，避免把窗口起点当上市日）
        if int(hist_len_map.get(code, 0)) < cfg['backtest']['min_listed_days']:
            continue
        pools['listed_ok'].append(code)
        # 老登股排除
        is_old, reason = _check_old(row)
        if is_old:
            continue
        pools['not_old'].append(code)

    # 行业排除（meta 提供当前行业分类）
    final = set(pools['not_old'])
    excluded_ind = []
    if meta is not None:
        m = meta.set_index('ts_code')['industry'].to_dict()
        still = []
        for code in final:
            ind = m.get(code, '')
            ex, label = is_excluded_industry(ind)
            if ex:
                excluded_ind.append((code, label))
            else:
                still.append(code)
        final = set(still)

    stats.update({
        'n_pool': len(final),
        'n_excluded_st': stats['n_alive'] - len(pools['not_st']),
        'n_excluded_susp': len(pools['not_st']) - len(pools['not_susp']),
        'n_excluded_new': len(pools['not_susp']) - len(pools['listed_ok']),
        'n_excluded_old_quant': len(pools['listed_ok']) - len(pools['not_old']),
        'n_excluded_industry': len(excluded_ind),
        'excluded_industry_sample': [c for c, _ in excluded_ind[:5]],
    })
    return final, stats


def slice_panel(panel: pd.DataFrame, codes: Iterable[str],
                start: str, end: str, buffer_days: int = 70) -> pd.DataFrame:
    """切片面板到 [start - buffer 交易日, end]，用于因子计算（缓冲提供动量/波动回看）。"""
    s = pd.Timestamp(start) - pd.Timedelta(days=buffer_days)
    e = pd.Timestamp(end)
    codes = list(codes)
    return panel[(panel['ts_code'].isin(codes)) & (panel['trade_date'] >= s) & (panel['trade_date'] <= e)]


def merge_pit_fundamentals(df: pd.DataFrame, pit: pd.DataFrame) -> pd.DataFrame:
    """合并 PIT PE/PB（peTTM→EP 因子，pbMRQ→BP 因子）到面板切片（df 为主表）。"""
    df = df.copy()
    if len(pit) == 0:
        df['pe_ttm'] = np.nan
        df['pb_mrq'] = np.nan
        return df
    m = df.merge(pit[['ts_code', 'trade_date', 'pe_ttm', 'pb_mrq']],
                 on=['ts_code', 'trade_date'], how='left')
    return m


def validate_ohlc(df: pd.DataFrame) -> List[str]:
    """OHLC 逻辑 + 成交量非负 + 连续性校验。返回违规清单（空=无违规）。"""
    issues = []
    bad = df[(df['low'] > df['close']) | (df['low'] > df['open']) | (df['low'] > df['high']) |
             (df['high'] < df['open']) | (df['high'] < df['close'])]
    if len(bad):
        issues.append(f'OHLC 逻辑违规 {len(bad)} 行（示例: {bad["ts_code"].iloc[0]}@{bad["trade_date"].iloc[0]}）')
    neg = df[df['vol'] < 0]
    if len(neg):
        issues.append(f'成交量非负违规 {len(neg)} 行')
    # 连续性：同股票相邻交易日间隔不应 >5 天（缺交易日=停牌/缺失，记录但不报错）
    df = df.sort_values(['ts_code','trade_date'])
    gaps = df.groupby('ts_code')['trade_date'].diff().dt.days
    big = gaps[gaps > 5]
    if len(big):
        issues.append(f'时间缺口>5天共 {len(big)} 处（停牌/缺失，引擎按 NaN 跳过）')
    return issues
