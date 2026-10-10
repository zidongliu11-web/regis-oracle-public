#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
runner.py — 5200 量化研究平台回测主循环

任务书执行（Step 12-14 的引擎实现）：
  - 5 环境（E1-E4 样本内，E5 样本外）× 每环境 10 次 = 50 次回测
  - 每次：从时点选股池随机抽 100 只（random.Random(seed) 可复现），初始资金 1M
  - 等权多因子综合得分，周频调仓，每日止损止盈，T 日信号 T+1 成交（PIT）
  - 种子：SystemRandom 产生 master_seed，再由 master_seed 派生 50 个 run_seed
    （D6 记录全部种子，可完全复现）

输出：
  data/results/metrics.csv          每次回测的业绩指标
  data/results/runs/E{env}_run{k}/   equity.csv + trades.csv
  data/results/stock_lists/         每次回测的 100 只选股清单
  data/results/seeds.json            master_seed + 50 run_seed

两阶段：
  Phase A：构建 5 个时点选股池 → 抽取 50×100 → 对并集拉 baostock PIT PE/PB（缓存）
  Phase B：串行执行 50 次回测（8GB 机器，不并行）
"""
from __future__ import annotations
import json, os, random, secrets, sys, time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
RESULTS = DATA / 'results'
out_sub = os.environ.get('BT_OUT', '')
if out_sub:
    RESULTS = DATA / out_sub          # 变体输出隔离：data/results_{variant}/，不覆盖基线
RUNS = RESULTS / 'runs'
LISTS = RESULTS / 'stock_lists'
for d in (RESULTS, RUNS, LISTS):
    d.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parents[1]))

from backtest_engine import data_loader as dl
from backtest_engine import factor_library as fl
from backtest_engine import signal_generator as sg
from backtest_engine import portfolio as pf
from backtest_engine import metrics as mt
from backtest_engine.strategies import get_strategy, list_strategies
from mom_trading.trading_calendar import backtest_market_allow, index_data_evidence

# 全仓位模式配置（项目 5400 预注册，见 research/full_exp_project/taskbook.md；跑数前写定，不后改）
FULL_EXP_CFG = {
    'n_buy': 30,                 # 调仓日买入：composite 横截面 top-30 且 > 0
    'n_sell_rank': 40,           # 月度旋转：跌出 top-40 卖出
    'target_gross': 0.90,        # 目标总暴露 90%（正常波动下）
    'vol_target': 0.18,          # 市场波动率目标 18% 年化
    'vol_scale_clip': (0.4, 1.0),# 波动率目标缩放裁剪
    'risk_mult_clip': (0.3, 3.0),# 个股风险权重裁剪
    'stop_loss': -0.08,          # 止损 -8%（全仓位月度下抗噪）
    'take_profit': None,         # 不设止盈（月度调仓实现收益）
    'max_position': 0.08,        # 单股最大仓位 8%
}
CSI500_BAR = ROOT.parents[1] / 'research' / 'data' / 'current' / 'sh000905.csv'
_csi_cache: Optional[pd.Series] = None
PIT_FUNDAMENTAL_STRATEGIES = {
    's1_multifactor', 'research_value',
    'combo_equal', 'combo_volinv', 'combo_corr',
}


def _load_csi500_close() -> Optional[pd.Series]:
    """CSI500 日收盘（date→close），模块级缓存；不可用返回 None（波动率目标自动降级为 1.0）。"""
    global _csi_cache
    if _csi_cache is not None:
        return _csi_cache
    try:
        if not CSI500_BAR.exists():
            return None
        bar = pd.read_csv(CSI500_BAR)
        dcol = 'date' if 'date' in bar.columns else 'trade_date'
        bar[dcol] = pd.to_datetime(bar[dcol])
        s = bar.set_index(dcol)['close'].sort_index()
        _csi_cache = s
        return s
    except Exception:
        return None


def draw_samples(pool: set, k: int, seed: int) -> List[str]:
    """从时点选股池确定性抽 k 只（sorted 保证顺序稳定）。"""
    rng = random.Random(seed)
    return rng.sample(sorted(pool), min(k, len(pool)))


POOL_MODE = os.environ.get('BT_POOL', 'legacy')
POOL_V2 = POOL_MODE in {'v2', 'v2_pit'}
POOL_V2_PIT = POOL_MODE == 'v2_pit'


def _build_pool_v2(panel, meta, env_start, cfg, env_id):
    """Step 0 v2 池：中小盘活跃 + 热点加权。返回 (pool, stats, hot_codes)。"""
    from backtest_engine import pool_v2 as pv
    selection_meta = None if POOL_V2_PIT else meta
    pool, stats = pv.build_smallcap_active_pool(panel, env_start, cfg, selection_meta)
    hot = set() if POOL_V2_PIT else pv.hot_codes_of(pool, meta)
    stats['selection_uses_current_industry_snapshot'] = not POOL_V2_PIT
    stats['sampling'] = 'uniform_without_replacement' if POOL_V2_PIT else 'current_hot_industry_weighted'
    stats['n_hot'] = len(hot)
    stats['hot_share'] = round(len(hot) / len(pool), 4) if pool else 0.0
    print(f'[A] {env_id} v2池 {len(pool)} 只（热点 {len(hot)} 只 {stats["hot_share"]*100:.1f}%）', flush=True)
    return pool, stats, hot


def phase_a(cfg: dict, panel: pd.DataFrame, meta: pd.DataFrame,
            master_seed: int) -> tuple:
    """构建时点池 + 抽 50×100 + 返回 (samples, seeds, pools)."""
    envs = cfg['environments']
    rng = random.Random(master_seed)
    seeds = [rng.randrange(0, 2**32) for _ in range(len(envs) * cfg['backtest']['n_runs_per_env'])]
    samples: Dict[str, List[tuple]] = {}   # env_id -> [(run_idx, seed, codes)]
    pools: Dict[str, dict] = {}
    all_pool_stats = {}
    si = 0
    for env in envs:
        eid = env['id']
        if POOL_V2:
            pool, stats, hot = _build_pool_v2(panel, meta, env['start'], cfg, eid)
        else:
            pool, stats = dl.build_timepoint_pool(panel, env['start'], cfg, meta)
            hot = None
        pools[eid] = pool
        all_pool_stats[eid] = stats
        if len(pool) == 0:
            print(f'[A] {eid} 选股池为空！跳过', flush=True)
            samples[eid] = []
            continue
        samples[eid] = []
        hot_weight = cfg.get('pool_v2', {}).get('hot_weight', 2.0) if POOL_V2 else 1.0
        for k in range(cfg['backtest']['n_runs_per_env']):
            if POOL_V2:
                from backtest_engine import pool_v2 as pv
                codes = pv.draw_weighted_samples(pool, cfg['backtest']['stocks_per_run'],
                                                 seeds[si], hot=hot, hot_weight=hot_weight)
            else:
                codes = draw_samples(pool, cfg['backtest']['stocks_per_run'], seeds[si])
            samples[eid].append((k, seeds[si], codes))
            si += 1
        print(f'[A] {eid} 池 {len(pool)} 只，抽 {cfg["backtest"]["n_runs_per_env"]} 次完成', flush=True)

    # 保存 stock_lists (D7)
    for eid, runs in samples.items():
        for (k, seed, codes) in runs:
            pd.DataFrame({'ts_code': codes, 'rank': range(1, len(codes) + 1),
                          'run_seed': seed}).to_csv(
                LISTS / f'{eid}_run{k}_codes.csv', index=False)
    if POOL_V2:
        # v2 池统计：合并全部 stats 键（含热点主题分布）
        rows = []
        for eid in pools:
            st = all_pool_stats[eid]
            row = {'env': eid}
            for kk in ('n_alive', 'n_pool', 'n_hot', 'hot_share',
                       'n_excluded_st', 'n_excluded_susp', 'n_excluded_new',
                       'n_excluded_old_v2', 'n_excluded_old_industry',
                       'n_excluded_v2_industry'):
                row[kk] = st.get(kk, 0)
            row['hot_theme_counts'] = str(st.get('hot_theme_counts', {}))
            rows.append(row)
        pd.DataFrame(rows).to_csv(RESULTS / 'pool_stats.csv', index=False)
    else:
        pd.DataFrame({
            'env': [eid for eid in pools for _ in [0]],
            'n_alive': [all_pool_stats[eid]['n_alive'] for eid in pools],
            'n_pool': [all_pool_stats[eid]['n_pool'] for eid in pools],
            'n_excluded_st': [all_pool_stats[eid].get('n_excluded_st', 0) for eid in pools],
            'n_excluded_susp': [all_pool_stats[eid].get('n_excluded_susp', 0) for eid in pools],
            'n_excluded_new': [all_pool_stats[eid].get('n_excluded_new', 0) for eid in pools],
            'n_excluded_old_quant': [all_pool_stats[eid].get('n_excluded_old_quant', 0) for eid in pools],
            'n_excluded_industry': [all_pool_stats[eid].get('n_excluded_industry', 0) for eid in pools],
        }).to_csv(RESULTS / 'pool_stats.csv', index=False)
    return samples, seeds, all_pool_stats


def fetch_pit_for_samples(samples: Dict[str, List[tuple]], cfg: dict):
    """对 50 次抽样选中的股票并集拉取 PIT PE/PB（增量缓存）。"""
    union: set = set()
    for eid, runs in samples.items():
        for (_, _, codes) in runs:
            union.update(codes)
    union = sorted(union)
    print(f'[A] 需要 PIT PE/PB 的股票并集: {len(union)} 只', flush=True)
    t0 = time.time()
    dl.ensure_pit_fundamentals(union,
                               start='2021-09-01', end='2024-12-31')
    print(f'[A] PIT PE/PB 就绪（{time.time()-t0:.0f}s）', flush=True)


def _pivot(sl: pd.DataFrame, col: str, codes: List[str], dates: np.ndarray) -> pd.DataFrame:
    sub = sl[sl['trade_date'].isin(dates)][['trade_date', 'ts_code', col]].dropna(subset=[col])
    w = sub.pivot(index='trade_date', columns='ts_code', values=col).reindex(dates)
    return w.reindex(columns=[c for c in codes if c in w.columns])


def execution_inputs(sl: pd.DataFrame, codes: List[str], dates: np.ndarray) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Preserve absent ST, trading status and turnover as absent; never impute fills."""
    def maybe_pivot(col: str) -> pd.DataFrame:
        if col not in sl.columns:
            return pd.DataFrame(np.nan, index=dates, columns=codes)
        return _pivot(sl, col, codes, dates)

    return (maybe_pivot('is_st'), maybe_pivot('trade_status'), maybe_pivot('amount'))


def run_one(env: dict, run_idx: int, seed: int, codes: List[str],
            panel: pd.DataFrame, meta: pd.DataFrame, pit: pd.DataFrame,
            cfg: dict) -> tuple:
    """单次回测 → (metrics dict, equity Series, trades list)."""
    bt = cfg['backtest']
    eid = env['id']
    # dir_sign（walk-forward 因子方向）需要更长缓冲供 60 交易日 IC 窗口使用
    buffer_days = 130 if bt.get('dir_sign') else 70
    sl = dl.slice_panel(panel, codes, env['start'], env['end'], buffer_days=buffer_days)
    if len(sl) == 0:
        print(f'[B] {eid}_run{run_idx} 无面板数据，跳过', flush=True)
        return {'env': eid, 'run': run_idx, 'seed': seed, 'error': 'no_panel'}, None, []

    sl = dl.merge_pit_fundamentals(sl, pit)
    F = fl.compute_factors(sl, codes, need_pe_pb=True)

    env_dates = np.sort(sl[(sl['trade_date'] >= pd.Timestamp(env['start']))
                           & (sl['trade_date'] <= pd.Timestamp(env['end']))]['trade_date'].unique())
    if len(env_dates) == 0:
        print(f'[B] {eid}_run{run_idx} 窗口无交易日，跳过', flush=True)
        return {'env': eid, 'run': run_idx, 'seed': seed, 'error': 'no_dates'}, None, []

    if bt.get('dir_sign'):
        # walk-forward 因子方向：IC 窗口需覆盖环境起始前的缓冲交易日
        all_dates = np.sort(sl['trade_date'].unique())
        close_all = _pivot(sl, 'adj_close', codes, all_dates)
        comp = sg.dynamic_direction_score(F, codes, close_all, list(env_dates),
                                          ic_tdays=60, min_obs=20)
    else:
        # Step 1 策略库：BT_STRATEGY 选择策略（S1-S5 或组合），缺省 S1 等权多因子
        strat = get_strategy()
        comp = strat.score(F, codes)

    prices = _pivot(sl, 'adj_close', codes, env_dates)
    pct = _pivot(sl, 'pct_chg', codes, env_dates)
    st, ts, amt = execution_inputs(sl, codes, env_dates)
    comp = comp.reindex(env_dates)

    market_allow = backtest_market_allow(list(env_dates)) if bt.get('market_timing') else None
    atr_pct = None
    if bt.get('taskbook_risk'):
        raw_close = _pivot(sl, 'close', codes, env_dates)
        high = _pivot(sl, 'high', codes, env_dates)
        low = _pivot(sl, 'low', codes, env_dates)
        previous = raw_close.shift(1)
        true_range = pd.concat([high - low, (high - previous).abs(), (low - previous).abs()],
                               keys=['hl', 'hp', 'lp']).groupby(level=1).max()
        atr_pct = true_range.rolling(14, min_periods=5).mean().div(raw_close).replace([np.inf, -np.inf], np.nan)

    # 变体支持（任务书解禁方向）：
    #   monthly    → rebal_period=21（月度调仓，降换手）
    #   risk_budget→ 风险平价预算：单股预算 = 基准1% × (横截面中位数波动/自身20日年化波动)，裁剪[0.2,5]
    #   full_exp   → 全仓位·风险加权·波动率目标（项目 5400 预注册）：目标 90% 暴露、
    #                市场波动率目标（CSI500 尾随 20 日年化波动 → 缩放 clip[0.4,1]）、
    #                个股风险权重 clip[0.3,3]、top-30 买入 / top-40 月度旋转 / 止损-8% / 无止盈
    #                波动全用因果 rolling，信号日决定、T+1 成交，无前视
    full_exp = FULL_EXP_CFG if bt.get('full_exp') else None
    risk_mult = None
    vol_scale = None
    if bt.get('risk_budget') or full_exp:
        vol = pct.rolling(20, min_periods=10).std() * np.sqrt(250)
        med = vol.median(axis=1)                  # 每日横截面中位数波动
        clip = full_exp['risk_mult_clip'] if full_exp else (0.2, 5.0)
        risk_mult = vol.rdiv(med, axis=0).clip(*clip)   # 每格 = 中位数波动/自身波动
    if full_exp:
        csi = _load_csi500_close()
        if csi is not None:
            csi_vol = csi.pct_change().rolling(20, min_periods=10).std() * np.sqrt(250)
            scale = (full_exp['vol_target'] / csi_vol).clip(*full_exp['vol_scale_clip'])
            vol_scale = scale.reindex(env_dates)
            # 全仓位模式基准 = CSI500 同窗口收益（价格指数，不含分红）
            csi_win = csi.loc[lambda s: s.index.isin([pd.Timestamp(env['start']), pd.Timestamp(env['end'])])]
            if len(csi_win) >= 2:
                csi_ret = float(csi_win.iloc[-1] / csi_win.iloc[0] - 1.0)
                env = {**env, 'actual_return_pct': csi_ret * 100.0}
    port = pf.Portfolio(bt['initial_capital'], cfg, prices, pct, st, ts, amt,
                        comp, list(prices.columns), list(env_dates),
                        rebal_period=bt.get('rebal_period', 5),
                        risk_mult=risk_mult, full_exp=full_exp, vol_scale=vol_scale,
                        market_allow=market_allow, atr_pct=atr_pct,
                        drawdown_stop=bt.get('drawdown_stop_live'))
    res = port.run()
    bench = env.get('actual_return_pct')
    m = mt.compute_metrics(res['equity'], res['trades'],
                           benchmark_total_return=(bench / 100.0) if bench is not None else None)
    m.update({'env': eid, 'run': run_idx, 'seed': seed,
              'n_codes': len(codes), 'n_pool': None})
    return m, res['equity'], res['trades']


def phase_b(samples: Dict[str, List[tuple]], panel: pd.DataFrame, meta: pd.DataFrame,
            pit: pd.DataFrame, cfg: dict) -> List[dict]:
    envs = {e['id']: e for e in cfg['environments']}
    all_metrics = []
    total_runs = sum(len(runs) for runs in samples.values())
    t0 = time.time()
    for eid, runs in samples.items():
        env = envs[eid]
        for (k, seed, codes) in runs:
            m, eq, trades = run_one(env, k, seed, codes, panel, meta, pit, cfg)
            if eq is not None and len(eq):
                eq.to_csv(RUNS / f'{eid}_run{k}_equity.csv')
                if trades:
                    pd.DataFrame(trades).to_csv(RUNS / f'{eid}_run{k}_trades.csv', index=False)
            if 'error' not in m:
                all_metrics.append(m)
            print(f'[B] {eid}_run{k} seed={seed} 总收益 {m.get("total_return")*100 if m.get("total_return") is not None else "?"}%'
                  f' （{len(all_metrics)}/{total_runs}，已用 {time.time()-t0:.0f}s）', flush=True)
    metrics_df = pd.DataFrame(all_metrics)
    metrics_df.to_csv(RESULTS / 'metrics.csv', index=False)
    return all_metrics


def main(master_seed: Optional[int] = None):
    cfg = dl.load_config()
    bt = cfg['backtest']
    only_envs = {x.strip() for x in os.environ.get('BT_ENV_IDS', '').split(',') if x.strip()}
    if only_envs:
        cfg['environments'] = [e for e in cfg['environments'] if e['id'] in only_envs]
        if not cfg['environments']:
            raise ValueError(f'BT_ENV_IDS 没有匹配环境: {sorted(only_envs)}')
    if os.environ.get('BT_RUNS_PER_ENV'):
        bt['n_runs_per_env'] = int(os.environ['BT_RUNS_PER_ENV'])
    if os.environ.get('BT_REBAL_PERIOD'):
        bt['rebal_period'] = int(os.environ['BT_REBAL_PERIOD'])
    if os.environ.get('BT_RISK_BUDGET') == '1':
        bt['risk_budget'] = True
    if os.environ.get('BT_DIRSIGN') == '1':
        bt['dir_sign'] = True
    if os.environ.get('BT_FULL_EXP') == '1':
        bt['full_exp'] = True
        if not os.environ.get('BT_REBAL_PERIOD'):
            bt['rebal_period'] = 21      # 全仓位默认月度调仓（任务书 5400 预注册）
    if os.environ.get('BT_MARKET_TIMING') == '1':
        bt['market_timing'] = True
    if os.environ.get('BT_MARKET_LIQUIDATE') == '0':
        bt['market_liquidate'] = False
    if os.environ.get('BT_BUY_THRESHOLD'):
        bt['buy_threshold'] = float(os.environ['BT_BUY_THRESHOLD'])
    if os.environ.get('BT_SELL_THRESHOLD'):
        bt['sell_threshold'] = float(os.environ['BT_SELL_THRESHOLD'])
    if os.environ.get('BT_DRAWDOWN_STOP'):
        bt['drawdown_stop_live'] = float(os.environ['BT_DRAWDOWN_STOP'])
    if os.environ.get('BT_TASKBOOK_RISK') == '1':
        bt['taskbook_risk'] = True
        bt['stop_loss'] = -0.08
        bt['take_profit'] = None
    cost_multiplier = float(os.environ.get('BT_COST_MULTIPLIER', '1'))
    if not np.isfinite(cost_multiplier) or cost_multiplier <= 0:
        raise ValueError('BT_COST_MULTIPLIER 必须是正数')
    for key in ('slippage', 'stamp_duty', 'commission', 'min_commission', 'transfer_fee'):
        bt[key] *= cost_multiplier
    print('[runner] 加载面板与 meta...', flush=True)
    panel = dl.load_panel()
    # v2_pit 的选股与抽样不读取 2026-08 当前行业快照，避免把未来分类带入历史窗口。
    meta = pd.DataFrame() if POOL_V2_PIT else dl.load_meta()
    strategy_name = get_strategy().name
    needs_pit_fundamentals = strategy_name in PIT_FUNDAMENTAL_STRATEGIES
    pit = dl.load_pit_fundamentals() if needs_pit_fundamentals else pd.DataFrame()

    if master_seed is None:
        master_seed = secrets.SystemRandom().randrange(0, 2**32)
    print(f'[runner] master_seed = {master_seed}', flush=True)
    _strat = get_strategy()
    print(f'[runner] 策略 = {_strat.name}（{_strat.description}）  '
          f'可用: {[s["name"] for s in list_strategies()]}', flush=True)

    # 历史目录没有本清单时，不能被误写成统一数据层已验证的本轮产物。
    market_timing_data = index_data_evidence() if bt.get('market_timing') else None
    if market_timing_data:
        first_required = min(env['start'] for env in cfg['environments'])
        last_required = max(env['end'] for env in cfg['environments'])
        index_files = market_timing_data.get('files') or {}
        history_covers_window = bool(
            market_timing_data.get('ready') is True
            and market_timing_data.get('common_as_of')
            and market_timing_data['common_as_of'] >= last_required
            and all(
                row.get('first_date') and row['first_date'] <= first_required
                for row in index_files.values()
            )
        )
        market_timing_data['required_window'] = [first_required, last_required]
        market_timing_data['history_covers_window'] = history_covers_window
        if not history_covers_window:
            raise RuntimeError('市场择时指数文件没有完整覆盖回测窗口')
    (RESULTS / 'run_manifest.json').write_text(json.dumps({
        'generated_at': pd.Timestamp.now().isoformat(),
        'master_seed': master_seed,
        'pool_mode': ('v2_price_only_pit' if POOL_V2_PIT else
                      'v2_smallcap_active_current_industry_weighted' if POOL_V2 else
                      'legacy_pool'),
        'selection_uses_current_industry_snapshot': not POOL_V2_PIT,
        'sampling': 'uniform_without_replacement' if POOL_V2_PIT else
                    'current_hot_industry_weighted' if POOL_V2 else 'uniform_without_replacement',
        'strategy': _strat.name,
        'market_timing': bool(bt.get('market_timing')),
        'market_timing_data': market_timing_data,
        'market_liquidate': bool(bt.get('market_liquidate', True)),
        'drawdown_stop': bt.get('drawdown_stop_live'),
        'buy_threshold': bt.get('buy_threshold'),
        'sell_threshold': bt.get('sell_threshold'),
        'volatility_inverse_sizing': bool(bt.get('risk_budget')),
        'taskbook_exit_rules': bool(bt.get('taskbook_risk')),
        'rebalance_trading_days': bt.get('rebal_period', 5),
        'cost_multiplier': cost_multiplier,
        'expected_runs': len(cfg['environments']) * cfg['backtest']['n_runs_per_env'],
        'execution': 'T日收盘信号，T+1成交；结果仅研究用途，不生成订单',
        'data_contract': '单票路径优先/api/data/*；全市场冻结PIT面板因接口能力边界使用本地parquet，并在data_provenance.json披露。',
    }, ensure_ascii=False, indent=2), encoding='utf-8')

    # Phase A
    samples, seeds, pool_stats = phase_a(cfg, panel, meta, master_seed)
    if not any(samples.values()):
        print('[runner] 无有效选股池，中止', flush=True)
        sys.exit(1)
    if needs_pit_fundamentals:
        fetch_pit_for_samples(samples, cfg)
        pit = dl.load_pit_fundamentals()      # 重新加载含新拉的 PE/PB
    else:
        print(f'[A] 策略 {strategy_name} 不使用 PE/PB，跳过无关数据拉取', flush=True)

    seeds_doc = {
        'master_seed': master_seed,
        'n_runs': len(seeds),
        'runs': [],
        'method': 'master_seed 由 SystemRandom 产生；每 run_seed=Random(master_seed).randrange(2^32)；'
                  '抽样=Random(run_seed).sample(sorted(pool), 100)',
    }
    for eid, runs in samples.items():
        for (k, seed, _) in runs:
            seeds_doc['runs'].append({'env': eid, 'run': k, 'run_seed': seed})
    (RESULTS / 'seeds.json').write_text(json.dumps(seeds_doc, indent=2, ensure_ascii=False))

    # Phase B
    expected_runs = sum(len(runs) for runs in samples.values())
    print(f'[runner] Phase B：串行执行 {expected_runs} 次回测...', flush=True)
    t0 = time.time()
    all_metrics = phase_b(samples, panel, meta, pit, cfg)
    pos = sum(1 for m in all_metrics if m.get('total_return') is not None and m['total_return'] > 0)
    print(f'[runner] 完成 {len(all_metrics)} 次回测，正收益 {pos} 次，'
          f'总耗时 {time.time()-t0:.0f}s', flush=True)
    try:
        prov = dl.dump_data_provenance(RESULTS / 'data_provenance.json')
        print(f'[runner] 数据来源审计已写 data/results/data_provenance.json（{prov["summary"]}）', flush=True)
    except Exception as e:
        print(f'[runner] ⚠ 数据来源审计落盘失败: {e}', flush=True)
    print(f'[runner] 结果已写 {RESULTS}（metrics.csv, runs/, stock_lists/, seeds.json）', flush=True)


if __name__ == '__main__':
    ms = None
    if len(sys.argv) > 1:
        ms = int(sys.argv[1])
    main(ms)
