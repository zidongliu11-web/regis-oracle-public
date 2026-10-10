#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pool_v2.py — 中小盘活跃股选股池（任务书 Step 0 · 地基）

与旧池（data_loader.build_timepoint_pool）的核心差异：
  1. 市值口径收窄：流通市值 50 亿 ~ 500 亿（旧池仅排除 >2000 亿）
  2. 换手率带通：20 日均换手 1.5% ~ 15%（旧池仅下限 0.8%）
  3. 波动率下限抬高：60 日年化波动 ≥ 30%（旧池 25%）
  4. 老登行业追加：地产(K70) / 建筑(E48)（旧池缺失）
  5. 热点行业加权：科技硬件/AI算力/新能源/智能制造/半导体/消费电子/医药创新/军工，
     抽样时权重 ×2（在大池子内提升权重，不强制排除）

诚实声明（Step 0 验收项，如实披露）：
  - 市值用 float_mv（流通市值 ≈ amount/turnover_rate，面板无总股本/总市值字段），
    "50-500 亿"为流通市值口径，非总市值
  - 行业为 stock_meta.parquet 的 2026-08 快照（证监会行业分类），热点主题按 3 位行业
    代码前缀映射；证监会粒度无法进一步细分（如"AI算力"与"消费电子"同属 C39），如实标注
  - 加权抽样用 random.Random(seed).choices 逐只不放回抽取，种子可复现
"""
from __future__ import annotations
import random
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

# ── 热点行业（证监会行业 3 位前缀 → 主题标签；任务书 8 类热点）──
HOT_INDUSTRY_PREFIXES: Dict[str, str] = {
    'C39': '科技硬件/AI算力/消费电子',  # 计算机、通信和其他电子设备制造业
    'I65': '科技硬件/AI算力/软件',       # 软件和信息技术服务业
    'I64': '科技硬件/AI算力/互联网',     # 互联网和相关服务
    'C38': '新能源/电气设备',            # 电气机械和器材制造业（光伏/锂电/储能）
    'C36': '新能源/汽车',                # 汽车制造业（新能源车）
    'C35': '智能制造/专用设备',          # 专用设备制造业（机器人/工控）
    'C34': '智能制造/通用设备',          # 通用设备制造业
    'C40': '智能制造/仪器仪表',          # 仪器仪表制造业
    'C27': '医药创新',                   # 医药制造业
    'C37': '军工/航空航天',              # 铁路船舶航空航天（军工）
}

# 旧池遗漏的老登行业（任务书明确列出：地产/建筑）
ADDITIONAL_OLD_INDUSTRY_PREFIXES: Dict[str, str] = {
    'K70': '地产',
    'E48': '建筑',
}


def hot_industry_label(industry: str) -> Optional[str]:
    """证监会行业代码 → 热点主题标签；非热点返回 None。"""
    if not industry or not isinstance(industry, str):
        return None
    return HOT_INDUSTRY_PREFIXES.get(industry[:3])


def is_additional_old_industry(industry: str) -> Tuple[bool, str]:
    """地产/建筑等旧池未排除的老登行业。"""
    if not industry or not isinstance(industry, str):
        return False, ''
    head = industry[:3]
    if head in ADDITIONAL_OLD_INDUSTRY_PREFIXES:
        return True, ADDITIONAL_OLD_INDUSTRY_PREFIXES[head]
    return False, ''


def build_smallcap_active_pool(panel: pd.DataFrame, env_start: str, cfg: dict,
                               meta: Optional[pd.DataFrame] = None) -> Tuple[Set[str], dict]:
    """
    中小盘活跃股时点池（Step 0 健壮过滤 + 热点行业标记）。

    过滤链（与旧池一致的 PIT 口径，追加收窄条件）：
      alive → not_st → not_susp(停牌≤30天) → listed_ok(上市≥60日)
            → not_old_v2（流通市值 50-500 亿 & 20日换手 1.5%-15% & 60日年化波动≥30%）
            → 行业排除（旧池老登 + 地产/建筑）

    返回 (final 池集合, stats dict)。
    stats 附加键：n_hot、hot_share、hot_theme_counts、n_excluded_old_v2、
                 n_excluded_industry_v2（地产/建筑）。
    """
    d0 = pd.Timestamp(env_start)
    p2 = cfg.get('pool_v2', {})
    lookback = cfg['backtest']['lookback_days']          # 90 自然日
    need = d0 - pd.Timedelta(days=lookback)
    seg = panel[(panel['trade_date'] >= need) & (panel['trade_date'] <= d0)].copy()
    if len(seg) == 0:
        return set(), {'n_pool': 0, 'reason': '无数据'}

    # 市值/换手/波动阈值（pool_v2 配置块，缺省用旧池口径的收窄值）
    min_mv = p2.get('min_mv', 50e8)          # 50 亿（流通市值口径）
    max_mv = p2.get('max_mv', 500e8)         # 500 亿
    min_turn = p2.get('min_turnover_20d', 0.015)   # 1.5%
    max_turn = p2.get('max_turnover_20d', 0.15)    # 15%
    min_vol = p2.get('min_volatility_annual', 0.30)  # 30%

    hist_len_map = panel[panel['trade_date'] <= d0].groupby('ts_code').size()
    alive_window = d0 - pd.Timedelta(days=15)
    alive = seg[seg['trade_date'] >= alive_window]['ts_code'].unique()

    def _agg(g: pd.DataFrame) -> dict:
        tr = g['turnover_rate'].dropna()
        pct = g['pct_chg'].dropna()
        mv = g['float_mv'].dropna()
        return {
            'n_bars': len(g),
            'susp_days': int((g['trade_status'] == 0).sum()),
            'turn20': tr.tail(20).mean() if len(tr.tail(20)) >= 10 else np.nan,
            'vol60': float(np.nanstd(pct.tail(60)) * np.sqrt(250)) if len(pct.tail(60)) >= 30 else np.nan,
            'float_mv': mv.tail(5).median() if len(mv.tail(5)) >= 1 else np.nan,
            'is_st_last': int(g['is_st'].iloc[-1]) if len(g) else 0,
        }

    agg = {code: _agg(g) for code, g in seg.groupby('ts_code')}
    codes = set(agg.keys())

    stats = {'n_all_with_data': len(codes), 'n_alive': int(len(alive))}
    pools = {'alive': [], 'not_st': [], 'not_susp': [], 'listed_ok': [], 'not_old_v2': []}

    def _check_old_v2(row: dict) -> Tuple[bool, str]:
        mv = row['float_mv']
        if mv is not None and not np.isnan(mv):
            if mv < min_mv:
                return True, f'流通市值{mv/1e8:.0f}亿<50亿'
            if mv > max_mv:
                return True, f'流通市值{mv/1e8:.0f}亿>500亿'
        t = row['turn20']
        if t is not None and not np.isnan(t):
            if t < min_turn:
                return True, f'换手{t*100:.1f}%<1.5%'
            if t > max_turn:
                return True, f'换手{t*100:.1f}%>15%'
        v = row['vol60']
        if v is not None and not np.isnan(v) and v < min_vol:
            return True, f'波动{v*100:.0f}%<30%'
        return False, ''

    for code in codes:
        row = agg[code]
        if code not in alive:
            continue
        pools['alive'].append(code)
        if row['is_st_last']:
            continue
        pools['not_st'].append(code)
        if row['susp_days'] > cfg['backtest']['max_suspension_days']:
            continue
        pools['not_susp'].append(code)
        if int(hist_len_map.get(code, 0)) < cfg['backtest']['min_listed_days']:
            continue
        pools['listed_ok'].append(code)
        is_old, _reason = _check_old_v2(row)
        if is_old:
            continue
        pools['not_old_v2'].append(code)

    # 行业排除：旧池老登（data_loader.is_excluded_industry）+ 地产/建筑
    from backtest_engine import data_loader as dl
    final = set(pools['not_old_v2'])
    excluded_old_ind, excluded_v2_ind = [], []
    hot_theme_counts: Dict[str, int] = {}
    if meta is not None:
        m = meta.set_index('ts_code')['industry'].to_dict()
        still = []
        for code in final:
            ind = m.get(code, '')
            ex, label = dl.is_excluded_industry(ind)
            if ex:
                excluded_old_ind.append((code, label))
                continue
            ex2, label2 = is_additional_old_industry(ind)
            if ex2:
                excluded_v2_ind.append((code, label2))
                continue
            still.append(code)
            hot = hot_industry_label(ind)
            if hot:
                hot_theme_counts[hot] = hot_theme_counts.get(hot, 0) + 1
        final = set(still)

    n_hot = sum(hot_theme_counts.values())
    stats.update({
        'n_pool': len(final),
        'n_hot': n_hot,
        'hot_share': round(n_hot / len(final), 4) if final else 0.0,
        'hot_theme_counts': hot_theme_counts,
        'n_excluded_st': stats['n_alive'] - len(pools['not_st']),
        'n_excluded_susp': len(pools['not_st']) - len(pools['not_susp']),
        'n_excluded_new': len(pools['not_susp']) - len(pools['listed_ok']),
        'n_excluded_old_v2': len(pools['listed_ok']) - len(pools['not_old_v2']),
        'n_excluded_old_industry': len(excluded_old_ind),
        'n_excluded_v2_industry': len(excluded_v2_ind),
        'excluded_v2_industry_sample': [c for c, _ in excluded_v2_ind[:5]],
    })
    return final, stats


def hot_codes_of(final: Set[str], meta: pd.DataFrame) -> Set[str]:
    """池内热点行业股票集合（供加权抽样）。"""
    if meta is None:
        return set()
    m = meta.set_index('ts_code')['industry'].to_dict()
    return {c for c in final if hot_industry_label(m.get(c, ''))}


def draw_weighted_samples(pool: Set[str], k: int, seed: int,
                          hot: Optional[Set[str]] = None,
                          hot_weight: float = 2.0) -> List[str]:
    """
    带权不放回抽样：热点股票权重 ×hot_weight，其余 ×1。
    逐只加权抽取（rng.choices 后剔除已选），随机种子可复现。
    """
    items = sorted(pool)
    if k >= len(items):
        return items
    if not hot:
        rng = random.Random(seed)
        return rng.sample(items, k)
    rng = random.Random(seed)
    chosen: Set[str] = set()
    while len(chosen) < k:
        rem = [c for c in items if c not in chosen]
        w = [hot_weight if c in hot else 1.0 for c in rem]
        c = rng.choices(rem, weights=w, k=1)[0]
        chosen.add(c)
    return list(chosen)
