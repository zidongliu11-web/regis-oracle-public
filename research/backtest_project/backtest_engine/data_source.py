#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data_source.py — 统一数据层客户端 + 数据来源审计（任务书 Step 0.5）

接入 Codex 统一数据层 `/api/data/*`（quant_data/api/server.py，宿主 5200）。
纯标准库实现（urllib），backtest_engine 与 factor_system 均可直接 import，无跨包依赖。

诚实适配（关键约束，如实披露）：
  /api/data/* 为「单票粒度」接口，且实测服务 5200 站点蜡烛源——返回窗口最早仅 2023-12、
  health_status=delayed、participates_in_decision=false、可带 start/end 过滤。
  它无法承载回测所需的 2020-10~2025-12 全市场 5163×1200 冻结 PIT 面板，原因：
    1. 无批量端点：全市场逐票 HTTP 拉取在 8GB 单机不可行；
    2. 时间窗缺失：E1/E2/E3 回测环境（2022-01 起）早于接口最早数据 2023-12；
    3. 幸存者偏差：实时源不含退市股，而回测面板"含退市股与 ST"；
    4. 复权前视：qfq 复权用事后分红信息调整历史价 → 冻结回测不可接受。

因此的分层（诚实不伪装）：
  - 批量冻结面板（load_panel/load_meta/load_pit_fundamentals）：仍读本地 parquet 为权威源，
    但每次加载做数据层健康检查并记录 source/as_of/health_status/participates_in_decision。
  - 单票实时/增量路径（quote/kline/fundamental/fund_flow）：走 /api/data/* 接口优先，
    失败降级本地缓存并标注。Step 3 实时信号 / Step 4 工作台以此为准。
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

API_BASE = os.environ.get('BT_DATA_API', 'http://127.0.0.1:5200')
HTTP_TIMEOUT = float(os.environ.get('BT_DATA_TIMEOUT', '8'))


def _http_json(url: str, timeout: float = HTTP_TIMEOUT) -> Optional[dict]:
    """GET 并解析 JSON；任何失败（连接/超时/解析）返回 None。不伪装。"""
    req = urllib.request.Request(url, headers={'User-Agent': 'backtest-data-source/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode('utf-8')
        return json.loads(raw)
    except Exception:
        return None


def data_layer_health() -> Optional[dict]:
    """GET /api/data/health → meta dict；接口不可达返回 None。"""
    payload = _http_json(f'{API_BASE}/api/data/health')
    if not payload or payload.get('code') != 0:
        return None
    return payload.get('meta') or payload.get('data') or {}


# ── 单票接口（接口优先，失败返回 None 由调用方降级）──

def fetch_kline(code: str, period: str = '1d', adjust: str = 'qfq',
                start: Optional[str] = None, end: Optional[str] = None,
                features: bool = False) -> tuple[Optional[list], dict]:
    """GET /api/data/kline → (rows, meta)。失败返回 (None, {error})。"""
    q = f'code={code}&period={period}&adjust={adjust}'
    if start:
        q += f'&start={start}'
    if end:
        q += f'&end={end}'
    if features:
        q += '&features=1'
    payload = _http_json(f'{API_BASE}/api/data/kline?{q}')
    if not payload or payload.get('code') != 0:
        return None, {'error': '接口不可达或返回异常', 'code': code, 'ts': _now()}
    data = payload.get('data', {})
    meta = payload.get('meta', {})
    return data.get('rows'), meta


def fetch_quote(code: str) -> tuple[Optional[dict], dict]:
    payload = _http_json(f'{API_BASE}/api/data/quote?code={code}')
    if not payload or payload.get('code') != 0:
        return None, {'error': '接口不可达或返回异常', 'code': code, 'ts': _now()}
    return payload.get('data'), payload.get('meta', {})


def fetch_fundamental(code: str) -> tuple[Optional[dict], dict]:
    payload = _http_json(f'{API_BASE}/api/data/fundamental?code={code}')
    if not payload or payload.get('code') != 0:
        return None, {'error': '接口不可达或返回异常', 'code': code, 'ts': _now()}
    return payload.get('data'), payload.get('meta', {})


def fetch_fund_flow(code: str) -> tuple[Optional[dict], dict]:
    payload = _http_json(f'{API_BASE}/api/data/fund_flow?code={code}')
    if not payload or payload.get('code') != 0:
        return None, {'error': '接口不可达或返回异常', 'code': code, 'ts': _now()}
    return payload.get('data'), payload.get('meta', {})


# ── 数据来源审计（provenance）──

def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


@dataclass
class ProvenanceRecord:
    access_point: str              # 如 load_panel / api_kline
    source: str                    # parquet_panel / api_5200 / baostock / fallback
    as_of: str                     # 数据时点（面板截至/接口 meta.as_of）
    health_status: str             # healthy / delayed / degraded / unavailable
    participates_in_decision: bool
    participates_in_backtest: bool = False
    participates_in_live_decision: bool = False
    note: str = ''
    ts: str = field(default_factory=_now)


class Provenance:
    def __init__(self) -> None:
        self.records: List[dict] = []

    def record(self, access_point: str, source: str, as_of: str,
               health_status: str, participates_in_decision: bool, note: str = '',
               participates_in_backtest: bool = False,
               participates_in_live_decision: bool = False) -> None:
        rec = ProvenanceRecord(
            access_point=access_point, source=source, as_of=as_of,
            health_status=health_status, participates_in_decision=participates_in_decision,
            participates_in_backtest=participates_in_backtest,
            participates_in_live_decision=participates_in_live_decision,
            note=note)
        self.records.append(asdict(rec))

    def dump(self, path) -> None:
        import pathlib
        p = pathlib.Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            'api_base': API_BASE,
            'generated_at': _now(),
            'records': self.records,
        }, ensure_ascii=False, indent=2), 'utf-8')

    def health_summary(self) -> str:
        if not self.records:
            return '无记录'
        ok = sum(1 for r in self.records if r['health_status'] not in ('degraded', 'unavailable'))
        return f'{ok}/{len(self.records)} 健康'


PROVENANCE = Provenance()


def record_panel_access(source_file: str, as_of: str, note: str = '') -> dict:
    """批量面板加载时调用：做数据层健康检查并记录 provenance。返回健康检查结果。"""
    health = data_layer_health()
    if health is None:
        h, p = 'unavailable', False
    else:
        h = health.get('health_status', 'unknown')
        p = bool(health.get('participates_in_decision', False))
    PROVENANCE.record(
        access_point=f'load_panel({source_file})',
        source='parquet_frozen_panel' if source_file.startswith('universe') else 'parquet_local',
        as_of=as_of,
        health_status=h,
        participates_in_decision=p,
        participates_in_backtest=True,
        participates_in_live_decision=False,
        note=note or '批量冻结面板走本地 parquet（统一接口为单票粒度+实时/延迟源，无法承载 5163×1200 冻结 PIT 面板）',
    )
    return health
