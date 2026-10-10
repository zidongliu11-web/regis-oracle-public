"""
Regis Oracle v3 — 实战交易信号引擎

板块多样性 + 进攻型信号 + 跨行业推荐。
针对 20 万账户（最多 5 个持仓、单仓≤20%、总暴露≤80%、现金≥20%），综合量化排名、
技术位与已核验时效的资金流；Kronos 只作未验证研究展示。
行业仅用于分散与风险预算，不作为候选资格黑名单。
"""
from __future__ import annotations

import json
import math
import os
import re
import time as _time
from collections import defaultdict
from datetime import datetime, date, time, timedelta
from pathlib import Path
from typing import Any

from .data_semantics import (
    FLOW_MAGNITUDE_SCALE,
    MAX_POSITIONS,
    MAX_SINGLE_POSITION_PCT,
    MAX_TOTAL_EXPOSURE_PCT,
    MIN_CASH_RESERVE_PCT,
    normalize_fund_flow_yi,
)
from .trading_calendar import audited_trading_day_age, trading_day_status

# ── 路径 ──────────────────────────────────────
_PROJECT = Path(__file__).resolve().parent.parent
_QUANT_SNAPSHOT = _PROJECT / "research" / "quant_stock_snapshot.json"
_FUNDFLOW = _PROJECT / "research" / "fund_flow_live.json"
_KRONOS_SNAPSHOT = _PROJECT / "research" / "kronos_stock_snapshot.json"
_INDUSTRY_MAP = _PROJECT / "research" / "industry_map.json"
_SIGNALS_DB = _PROJECT / "research" / "mom_signals.json"

# ── 账户参数 ──────────────────────────────────
# 账户本金只是该页试算基数；风险上限统一定义在 data_semantics。
ACCOUNT_CAPITAL = 200_000
MAX_SIGNALS_PER_DAY = 5
POSITION_SIZE_PCT = MAX_SINGLE_POSITION_PCT / 100
MAX_FULL_SIZE_POSITIONS = MAX_TOTAL_EXPOSURE_PCT // MAX_SINGLE_POSITION_PCT
LOT_SIZE = 100
SELL_THRESHOLD_RANK = 60   # 放宽卖出阈值（原来40太紧）
BUY_MIN_RANK = 50          # 放宽买入门槛（原来30太紧）

# ── 交易时段 ──────────────────────────────────
MORNING_OPEN = time(9, 30)
MORNING_CLOSE = time(11, 30)
AFTERNOON_OPEN = time(13, 0)
AFTERNOON_CLOSE = time(15, 0)
ENTRY_WINDOW_AFTERNOON_END = time(14, 30)

ENTRY_WINDOW_MORNING_START = time(9, 30)
ENTRY_WINDOW_MORNING_END = time(11, 0)


def manual_real_money_mode() -> bool:
    """User-authorised manual broker-ticket mode; never means broker connected."""
    return os.environ.get("OLYMPUS_MANUAL_REAL_MONEY_MODE") == "1"


def is_recommendable_stock(code: str, name: str = "") -> bool:
    """Keep stock recommendations stock-only; exclude funds/ETFs/index instruments."""
    normalized = str(code or "").strip().split(".")[0]
    label = str(name or "").upper()
    return bool(re.fullmatch(r"(?:0|3|6)\d{5}", normalized)) and not any(
        marker in label for marker in ("ETF", "LOF", "基金", "指数")
    )

# ── v3: 进攻型信号阈值 ─────────────────────────
AGGRESSIVE_FLOW_MIN = 0.5        # 主力净流入超过 0.5 亿
AGGRESSIVE_RANK_MAX = 80         # 排名不超过 80

# ── v3.2: 文艺复兴审查 — 信号质量门禁 ──────────
# 低质量信号比没有信号更危险
MIN_CONFIDENCE = 40.0            # 置信度 < 40% 不发信号（目标：高于随机50%）
BLOCK_NEGATIVE_FLOW = False      # 第三方大单代理仅展示，不得阻断买入
MAX_NEGATIVE_FLOW = -0.3         # 净流出超过 0.3 亿坚决不买
# FLOW_MAGNITUDE_SCALE 从 data_semantics 导入并继续在本模块导出，兼容现有调用方。

# ── 板块中英文映射（申万一级 → 简称） ─────────
INDUSTRY_SHORT = {
    "电力、热力生产和供应业": "公用事业",
    "电力": "公用事业",
    "水的生产和供应业": "公用事业",
    "燃气生产和供应业": "公用事业",
    "煤炭开采和洗选业": "能源",
    "石油和天然气开采业": "能源",
    "有色金属矿采选业": "有色",
    "有色金属冶炼和压延加工业": "有色",
    "黑色金属矿采选业": "钢铁",
    "黑色金属冶炼和压延加工业": "钢铁",
    "化学原料和化学制品制造业": "化工",
    "化学纤维制造业": "化工",
    "橡胶和塑料制品业": "化工",
    "医药制造业": "医药生物",
    "专用设备制造业": "机械设备",
    "通用设备制造业": "机械设备",
    "电气机械和器材制造业": "电力设备",
    "汽车制造业": "汽车",
    "铁路、船舶、航空航天和其他运输设备制造业": "交运设备",
    "计算机、通信和其他电子设备制造业": "电子",
    "软件和信息技术服务业": "计算机",
    "互联网和相关服务": "计算机",
    "电信、广播电视和卫星传输服务": "通信",
    "食品制造业": "食品饮料",
    "酒、饮料和精制茶制造业": "食品饮料",
    "农副食品加工业": "农林牧渔",
    "纺织业": "纺织服装",
    "纺织服装、服饰业": "纺织服装",
    "非金属矿物制品业": "建材",
    "金属制品业": "制造",
    "房地产业": "房地产",
    "建筑业": "建筑装饰",
    "批发业": "商贸零售",
    "零售业": "商贸零售",
    "货币金融服务": "银行",
    "资本市场服务": "非银金融",
    "保险业": "非银金融",
    "其他金融业": "非银金融",
    "交通运输、仓储和邮政业": "交通运输",
    "道路运输业": "交通运输",
    "航空运输业": "交通运输",
    "水上运输业": "交通运输",
    "铁路运输业": "交通运输",
    "住宿业": "休闲服务",
    "餐饮业": "休闲服务",
    "新闻和出版业": "传媒",
    "广播、电视、电影和影视录音制作业": "传媒",
    "租赁业": "综合",
    "商务服务业": "综合",
    "研究和试验发展": "综合",
    "专业技术服务业": "综合",
    "生态保护和环境治理业": "环保",
    "废弃资源综合利用业": "环保",
    "家具制造业": "轻工制造",
    "造纸和纸制品业": "轻工制造",
    "印刷和记录媒介复制业": "轻工制造",
    "文教、工美、体育和娱乐用品制造业": "轻工制造",
    "木材加工和木、竹、藤、棕、草制品业": "轻工制造",
    "皮革、毛皮、羽毛及其制品和制鞋业": "纺织服装",
    "仪器仪表制造业": "机械设备",
    "开采辅助活动": "采掘",
    "其他制造业": "制造",
    "综合": "综合",
}


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _load_signals_db() -> list[dict]:
    data = _load_json(_SIGNALS_DB)
    return data.get("signals", []) if isinstance(data, dict) else []


def _save_signals_db(signals: list[dict]) -> None:
    _SIGNALS_DB.parent.mkdir(parents=True, exist_ok=True)
    _SIGNALS_DB.write_text(
        json.dumps({
            "updated_at": datetime.now().isoformat(),
            "count": len(signals),
            "signals": signals,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _get_held_codes(history: list[dict]) -> set[str]:
    held: set[str] = set()
    for sig in sorted(history, key=lambda s: s.get("date", "")):
        try:
            if date.fromisoformat(str(sig.get("date", ""))[:10]).weekday() >= 5:
                continue
        except ValueError:
            continue
        if sig.get("action") == "BUY":
            held.add(sig["code"])
        elif sig.get("action") == "SELL":
            held.discard(sig["code"])
    return held


def _can_afford(price: float | None) -> bool:
    try:
        price = float(price)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(price) or price <= 0:
        return False
    cost = price * LOT_SIZE
    max_per_stock = ACCOUNT_CAPITAL * POSITION_SIZE_PCT
    return cost <= max_per_stock


def _get_industry(code: str) -> str:
    """获取申万一级行业简称，无数据返回空字符串."""
    if not _INDUSTRY_MAP.exists():
        return ""
    try:
        industry_map = json.loads(_INDUSTRY_MAP.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    raw = industry_map.get(code, "") or ""
    return INDUSTRY_SHORT.get(raw, raw)


def _board_type(code: str) -> str:
    """根据代码前缀返回板块类型."""
    if code.startswith("688") or code.startswith("689"):
        return "科创板"
    if code.startswith("300") or code.startswith("301"):
        return "创业板"
    if code.startswith("8") or code.startswith("4"):
        return "北交所"
    if code.startswith("6"):
        return "主板"
    if code.startswith("0") or code.startswith("3"):
        return "深市"
    return ""


def _industry_tag(code: str) -> str:
    """获取板块标签，优先行业分类，fallback 到板块类型."""
    ind = _get_industry(code)
    return ind or _board_type(code) or "全市场"


def _is_blacklisted(code: str, name: str) -> bool:
    """兼容旧调用；行业与国企身份不再决定候选资格。"""
    del code, name
    return False


def _entry_zone(prev_close: float, atr_pct: float = 0.02) -> dict:
    half_range = prev_close * atr_pct * 0.5
    return {
        "min": round(prev_close - half_range, 2),
        "max": round(prev_close + half_range, 2),
        "ideal": round(prev_close, 2),
        "atr_pct_used": round(atr_pct * 100, 1),
    }


def _stop_target(entry: float, prev_close: float, atr_pct: float = 0.02) -> dict:
    atr = prev_close * atr_pct
    return {
        "stop_loss": round(entry - atr * 1.5, 2),
        "target_1": round(entry + atr * 2.0, 2),
        "target_2": round(entry + atr * 3.0, 2),
        "risk_reward": "1:2",
    }


def _entry_timing(now: datetime | None = None) -> dict:
    now = now or datetime.now()
    now_t = now.time()
    calendar = trading_day_status(now)
    combine = lambda day, clock: datetime.combine(day, clock, tzinfo=now.tzinfo)

    if not calendar["is_trading_day"]:
        return {
            "status": "closed", "message": calendar["reason"],
            "next_session": (_next_trading_day(now) or now.date()).isoformat(),
            "countdown_seconds": None,
            "calendar": calendar,
        }

    if ENTRY_WINDOW_MORNING_START <= now_t <= ENTRY_WINDOW_MORNING_END:
        remaining = int((combine(now.date(), ENTRY_WINDOW_MORNING_END) - now).total_seconds())
        return {
            "status": "active", "window": "上午人工复核窗口",
            "message": "当前可复核建议；仍需收益与风控门禁通过",
            "window_end": ENTRY_WINDOW_MORNING_END.strftime("%H:%M"),
            "countdown_seconds": remaining,
            "calendar": calendar,
        }
    if now_t < ENTRY_WINDOW_MORNING_START and now_t >= MORNING_OPEN:
        remaining = int((combine(now.date(), ENTRY_WINDOW_MORNING_START) - now).total_seconds())
        return {
            "status": "waiting", "window": "等待早盘窗口",
            "message": f"等待开盘消化集合竞价，{ENTRY_WINDOW_MORNING_START.strftime('%H:%M')} 后可入场",
            "window_start": ENTRY_WINDOW_MORNING_START.strftime("%H:%M"),
            "countdown_seconds": remaining,
            "calendar": calendar,
        }

    if now_t < MORNING_OPEN:
        remaining = int((combine(now.date(), MORNING_OPEN) - now).total_seconds())
        return {
            "status": "waiting", "window": "开盘前",
            "message": "今日为交易日，等待 09:30 开盘",
            "next_session": now.date().isoformat(),
            "window_start": MORNING_OPEN.strftime("%H:%M"),
            "countdown_seconds": remaining,
            "calendar": calendar,
        }

    if ENTRY_WINDOW_MORNING_END < now_t < AFTERNOON_OPEN:
        return {
            "status": "waiting", "window": "等待午后复核窗口",
            "message": "13:00 后恢复人工复核",
            "countdown_seconds": max(0, int((combine(now.date(), AFTERNOON_OPEN) - now).total_seconds())),
            "calendar": calendar,
        }

    if AFTERNOON_OPEN <= now_t <= ENTRY_WINDOW_AFTERNOON_END:
        remaining = int((combine(now.date(), ENTRY_WINDOW_AFTERNOON_END) - now).total_seconds())
        return {
            "status": "active", "window": "午后人工复核窗口",
            "message": "当前可复核建议；仍需收益与风控门禁通过",
            "window_end": ENTRY_WINDOW_AFTERNOON_END.strftime("%H:%M"),
            "countdown_seconds": max(0, remaining),
            "calendar": calendar,
        }

    if ENTRY_WINDOW_AFTERNOON_END < now_t <= AFTERNOON_CLOSE:
        return {
            "status": "caution", "window": "尾盘谨慎窗口",
            "message": "14:30 后不新开仓，继续持仓风险观察",
            "countdown_seconds": None,
            "calendar": calendar,
        }

    if now_t > AFTERNOON_CLOSE:
        next_day = _next_trading_day(now)
        remaining = (int((combine(next_day, MORNING_OPEN) - now).total_seconds())
                     if next_day else None)
        return {
            "status": "closed",
            "message": (
                "已收盘，下一交易日 " + next_day.strftime("%m月%d日") + " 9:30 开盘"
                if next_day else "已收盘；交易日历待更新"
            ),
            "next_session": next_day.isoformat() if next_day else None,
            "countdown_seconds": remaining,
            "calendar": calendar,
        }

    return {
        "status": "caution", "window": "非最佳窗口",
        "message": "当前非最佳入场时段，建议等待早盘或尾盘窗口",
        "countdown_seconds": None,
        "calendar": calendar,
    }


def _next_trading_day(d: datetime) -> date | None:
    nd = d.date() + timedelta(days=1)
    for _ in range(370):
        if trading_day_status(nd)["is_trading_day"]:
            return nd
        nd += timedelta(days=1)
    return None


def _build_signal_reason(
    rank: int, stock: dict, flow_net: float | None,
    kronos_ret: float | None, entry_zone: dict, stop_target: dict,
    industry_label: str = "", include_execution_levels: bool = True,
) -> str:
    """Explain the research score; add price levels only after the gate opens."""
    parts = []

    if industry_label:
        parts.append(f"【{industry_label}】板块精选")

    # ── 排名定位 ──
    if rank <= 5:
        parts.append(f"量化排名第{rank}（全市场前2%）")
    elif rank <= 15:
        parts.append(f"量化排名第{rank}（全市场前5%）")
    elif rank <= 30:
        parts.append(f"量化排名第{rank}（全市场前10%）")
    else:
        parts.append(f"量化排名第{rank}")

    # ── 因子贡献分解 ──
    factors = stock.get("factors", {})
    if factors:
        # 提取关键因子：正向贡献最大的3个 + 拖后腿的1个
        factor_scores: list[tuple[str, float]] = []
        # 因子标准化后绝对值越大影响越大
        key_factors = {
            "size_proxy": ("大盘/小盘", -1),     # size因子通常负向（小盘>大盘）
            "ffc_res_vol_20d": ("低波", 1),
            "idio_vol_60d": ("特质波", -1),
            "amihud_illiq": ("流动性", 1),
            "gk_vol_20d": ("波动", -1),
            "low_vol_60d": ("防御", 1),
            "momentum_20d": ("动量", 1),
            "momentum_5d": ("短期动能", 1),
            "dollar_volume_20d": ("成交额", 1),
            "turnover_20d": ("换手", -1),
            "roe_ttm": ("ROE", 1),
            "gross_margin_ttm": ("毛利率", 1),
            "net_profit_yoy": ("利润增速", 1),
        }
        for fname, (label, direction) in key_factors.items():
            val = factors.get(fname, 0)
            if abs(val) > 0.3:  # 有意义的标准差偏离
                impact = val * direction  # 方向修正后，正=利好
                factor_scores.append((label, impact, val))

        # 按贡献绝对值排序
        factor_scores.sort(key=lambda x: abs(x[1]), reverse=True)
        positive = [f for f in factor_scores if f[1] > 0][:2]
        negative = [f for f in factor_scores if f[1] < 0][:1]

        if positive:
            pos_names = "、".join(p[0] for p in positive)
            parts.append(f"主力因子：{pos_names}贡献正面")
        if negative:
            parts.append(f"注意因子：{negative[0][0]}偏弱")

    if include_execution_levels:
        parts.append(
            f"入场 ¥{entry_zone['min']}–¥{entry_zone['max']} "
            f"| 止损 ¥{stop_target['stop_loss']} "
            f"| 目标 ¥{stop_target['target_1']}/¥{stop_target['target_2']}"
        )

    # ── 资金流 ──
    if flow_net and flow_net > 0.5:
        parts.append(f"主力净流入 {flow_net:.2f}亿")
    elif flow_net and flow_net > 0.1:
        parts.append(f"主力小幅流入 {flow_net:.2f}亿")
    elif flow_net and flow_net < 0:
        parts.append(f"⚠️ 主力净流出 {abs(flow_net):.2f}亿，注意回调")

    if kronos_ret is not None:
        parts.append(
            f"Kronos研究输出 {kronos_ret:+.0f}（未通过收益验证，"
            "包括成本后验证；不参与本次评分）"
        )

    return "；".join(parts)


def generate_today_signals(
    names: dict[str, str] | None = None,
    current_prices: dict[str, float] | None = None,
    prev_closes: dict[str, float] | None = None,
    allow_executable: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    v3 信号生成：板块多样性 + 进攻型信号。

    1. 从量化快照 top-80 中选取（扩大池子以覆盖更多板块）
    2. 过滤：价格 ≤ 33元（10,000/3/100）
    3. 排除已持仓个股
    4. 按行业分组，每个行业最多推荐 1 只
    5. 综合打分（量化排名 + 已核验时效的资金流）
    6. Kronos 仅作未验证研究展示，不进入打分或通过/否决门
    7. 确保至少覆盖 3 个不同板块
    """
    quant = _load_json(_QUANT_SNAPSHOT)
    ff = _load_json(_FUNDFLOW)
    kronos = _load_json(_KRONOS_SNAPSHOT)
    history = _load_signals_db()

    names = names or {}
    prices = current_prices or {}
    prevs = prev_closes or {}

    stocks = quant.get("stocks", [])
    ff_stocks: dict = ff.get("stocks", {}) if isinstance(ff.get("stocks"), dict) else {}
    kronos_stocks: dict = kronos.get("stocks", {}) if isinstance(kronos.get("stocks"), dict) else {}

    current = now or datetime.now()
    today_str = current.date().isoformat()
    timing = _entry_timing(current)
    quant_freshness = audited_trading_day_age(
        quant.get("data_asof") or quant.get("snapshot_date") or "",
        now=current,
    )
    flow_freshness = audited_trading_day_age(
        ff.get("ts") or ff.get("date") or "",
        now=current,
    )
    kronos_source_as_of = str(kronos.get("snapshot_date") or kronos.get("data_asof") or "")
    kronos_freshness = audited_trading_day_age(kronos_source_as_of, now=current)
    execution_time_allowed = (
        timing.get("status") == "active"
        and quant_freshness.get("status") == "fresh"
    )
    today_signals: list[dict] = []
    held_codes = _get_held_codes(history) if allow_executable else set()
    used_slots = len(held_codes)

    stock_map = {s["code"]: s for s in stocks}

    # 已否决策略只保留研究观察，不再从旧信号账本生成执行性卖出建议。
    for code in list(held_codes) if allow_executable else []:
        stock = stock_map.get(code)
        if not stock:
            continue
        rank = stock.get("rank", 999)
        if rank > SELL_THRESHOLD_RANK:
            today_signals.append({
                "code": code, "name": names.get(code, code),
                "action": "SELL" if execution_time_allowed else "WATCH", "rank": rank, "confidence": 90.0,
                "industry": _industry_tag(code),
                "reason": (
                    f"量化排名已降至第{rank}（阈值{SELL_THRESHOLD_RANK}），"
                    f"跌出核心持仓区间。建议在 ¥{prices.get(code, '当前价')} 附近止盈/止损离场，"
                    f"释放仓位用于更优标的。"
                ),
                "price": prices.get(code),
                "signal_number": None,
                "entry_zone": None, "stop_target": None,
            })
            used_slots -= 1

    # ── 2. 可买标的筛选（扩大池子） ──────────
    available_slots = min(
        MAX_SIGNALS_PER_DAY,
        MAX_POSITIONS - used_slots,
        MAX_FULL_SIZE_POSITIONS - used_slots,
    )
    if available_slots <= 0:
        if today_signals:
            _append_to_db(history, today_signals, today_str)
        return _build_response(quant, today_signals, today_str, allow_executable, timing)

    # 候选池 rank≤100；最终同时受持仓数、单仓和80%总暴露硬上限约束。
    # 不再限制 in_portfolio——top20 被银行/国家设施占满，黑名单剔除后所剩无几
    candidates = [
        s for s in stocks
        if s.get("rank", 999) <= 100
        and is_recommendable_stock(s.get("code", ""), names.get(s.get("code", ""), ""))
    ]

    # 综合打分
    scored = []
    for s in candidates:
        code = s["code"]
        if code in held_codes:
            continue
        try:
            price = float(prices.get(code))
        except (TypeError, ValueError):
            price = None
        if price is None or not math.isfinite(price) or price <= 0:
            continue
        if not _can_afford(price):
            continue

        rank = s.get("rank", 999)
        ff_info = ff_stocks.get(code, {})
        raw_net_yi = (
            ff_info.get("net_yi")
            if isinstance(ff_info, dict) and flow_freshness.get("status") == "fresh"
            else None
        )
        # THS 采集器已输出亿元；该第三方大单代理仅用于可追溯展示。
        net_yi = normalize_fund_flow_yi(raw_net_yi)
        k_ret = _get_kronos_ret(code, kronos_stocks)

        # 旧研究分仍只作遗留排序输入；行业不会提高或降低候选资格。
        rank_score = max(0, 80 - rank * 1.5)
        total = rank_score

        # v3: 进攻型加分
        signal_type = "quality"  # 默认：质优型
        industry = _industry_tag(code)
        # 兼容旧接口；行业硬黑名单已停用，行业只在下方做组合分散。
        if _is_blacklisted(code, names.get(code, code) or code):
            continue
        scored.append((total, s, net_yi, k_ret, price, industry, signal_type))

    scored.sort(key=lambda x: x[0], reverse=True)

    # ── 3. v3: 板块多样性筛选 ─────────────────
    selected: list = []
    used_industries: set[str] = set()

    # 第一轮：每个行业挑 top 1
    by_industry: dict[str, list] = defaultdict(list)
    for item in scored:
        by_industry[item[5]].append(item)

    for industry, items in sorted(by_industry.items(), key=lambda kv: -kv[1][0][0]):
        if len(selected) >= available_slots:
            break
        # 跳过已使用的行业（除非我们需要更多标的但行业不够）
        if industry in used_industries:
            continue
        best = items[0]
        selected.append(best)
        used_industries.add(industry)

    # 第二轮：如果还不够，从剩余中按得分补足
    if len(selected) < available_slots:
        for item in scored:
            if len(selected) >= available_slots:
                break
            if item in selected:
                continue
            selected.append(item)

    # ── 4. 生成买入信号 ──────────────────────
    signal_no = 0
    for total, s, net_yi, k_ret, price, industry, sig_type in selected:
        code = s["code"]
        rank = s.get("rank", 999)
        confidence = round(min(95.0, max(25.0, total)), 1)

        # v3.2: 文艺复兴门禁——置信度不足不发信号
        if confidence < MIN_CONFIDENCE:
            continue

        signal_no += 1
        try:
            prev_close = float(prevs.get(code))
        except (TypeError, ValueError):
            prev_close = price
        if not math.isfinite(prev_close) or prev_close <= 0:
            prev_close = price

        atr_pct = _estimate_volatility(s)
        entry_zone = _entry_zone(prev_close, atr_pct)
        stop_tgt = _stop_target(entry_zone["ideal"], prev_close, atr_pct)

        flow_text = (
            _flow_text(code, ff_stocks)
            if flow_freshness.get("status") == "fresh"
            else f"第三方大单快照时效为 {flow_freshness.get('status')}，仅展示"
        )
        fd = _flow_direction(net_yi)

        executable = bool(allow_executable and execution_time_allowed)
        today_signals.append({
            "code": code, "name": names.get(code, code),
            "action": "BUY" if executable else "WATCH",
            "confidence": confidence,
            "price": price,
            "prev_close": round(prev_close, 2),
            "industry": industry,
            "signal_type": sig_type,
            "reason": _build_signal_reason(
                rank, s, net_yi, k_ret, entry_zone, stop_tgt, industry,
                include_execution_levels=executable,
            ),
            "entry_zone": entry_zone if executable else None,
            "stop_target": stop_tgt if executable else None,
            "signal_number": signal_no,
            "max_signals_today": available_slots,
            "position_pct": round(POSITION_SIZE_PCT * 100) if executable else None,
            "estimated_cost": round(price * LOT_SIZE) if executable and price else None,
            "flow_direction": fd, "flow_text": flow_text,
            "fund_flow_freshness": flow_freshness,
            "kronos_20d": k_ret,
            "kronos_research": {
                "value_20d_pct": k_ret,
                "source_data_as_of": kronos_source_as_of or None,
                "freshness": kronos_freshness,
                "decision_eligible": False,
                "research_only": True,
                "validation_status": "unvalidated_model_output",
            },
            "affordable": True if executable else None,
        })

    # ── 5. 持久化 ────────────────────────────
    if allow_executable and execution_time_allowed and today_signals:
        _append_to_db(history, today_signals, today_str)

    return _build_response(quant, today_signals, today_str, allow_executable, timing)


def _append_to_db(history: list[dict], signals: list[dict], today_str: str) -> None:
    """追加信号到数据库（强制仓位上限，防止并发重复写入）."""
    history = [s for s in history if not s.get("date", "").startswith(today_str)]
    held = _get_held_codes(history)
    buy_slots_remaining = max(
        0, min(MAX_POSITIONS, MAX_FULL_SIZE_POSITIONS) - len(held),
    )
    kept: list[dict] = []
    for sig in signals:
        sig["date"] = datetime.now().isoformat()
        if sig.get("action") == "BUY":
            code = sig["code"]
            if code in held:
                continue
            if buy_slots_remaining <= 0:
                continue
            held.add(code)
            buy_slots_remaining -= 1
        kept.append(sig)
    history.extend(kept)
    _save_signals_db(history)


def _completed_trading_days_since(
    as_of: date,
    *,
    now: datetime | None = None,
) -> int:
    """Count only sessions whose close should already exist."""
    local_now = now or datetime.now().astimezone()
    today = local_now.date()
    completed = 0
    cursor = as_of
    while cursor < today:
        cursor += timedelta(days=1)
        if not trading_day_status(cursor)["is_trading_day"]:
            continue
        if cursor < today or local_now.timetz().replace(tzinfo=None) >= time(16, 0):
            completed += 1
    return completed


def _build_response(
    quant: dict, signals: list[dict], today_str: str, allow_executable: bool,
    timing: dict | None = None,
) -> dict:
    if allow_executable and not signals:
        history = _load_signals_db()
        today_existing = [s for s in history if s.get("date", "").startswith(today_str)]
        if today_existing:
            signals = today_existing

    buy_count = sum(1 for s in signals if s.get("action") == "BUY")
    sell_count = sum(1 for s in signals if s.get("action") == "SELL")

    # 统计板块覆盖
    industries_covered = list(dict.fromkeys(
        s.get("industry", "") for s in signals if s.get("industry")
    ))

    # 数据新鲜度：以复合分实际用到的日频数据截止日 data_asof 为准。
    # snapshot_date 是最近 IC 有效调仓日（权重训练参考），不是信号数据日期，
    # 用它计算会误报"过期16天"。日频策略盘后更新，周末/假期 1-2 天属正常，
    # 只要错过一个已结束的已审计交易日就提示过期；周末/假期不累计。
    snapshot_date = quant.get("snapshot_date", "")
    data_asof = quant.get("data_asof") or snapshot_date
    data_staleness_days = None
    data_stale = True
    freshness = {"status": "unknown", "days": None, "reason": "数据日期缺失"}
    if data_asof:
        freshness = audited_trading_day_age(data_asof)
        data_staleness_days = freshness.get("days")
        data_stale = freshness.get("status") != "fresh"

    return {
        "date": today_str,
        "snapshot_date": snapshot_date,
        "data_asof": data_asof,
        "data_staleness_days": data_staleness_days,
        "data_stale": data_stale,
        "data_freshness": freshness,
        "headline": _safe_headline(quant.get("headline")),
        "research_only": not allow_executable,
        "account": {
            "capital": ACCOUNT_CAPITAL,
            "max_positions": MAX_POSITIONS,
            "lot_size": LOT_SIZE,
            "position_pct": MAX_SINGLE_POSITION_PCT,
            "max_single_position_pct": MAX_SINGLE_POSITION_PCT,
            "max_total_exposure_pct": MAX_TOTAL_EXPOSURE_PCT,
            "min_cash_reserve_pct": MIN_CASH_RESERVE_PCT,
            "max_full_size_positions": MAX_FULL_SIZE_POSITIONS,
            "risk_policy": "hard_limits_not_return_advice",
        },
        "entry_timing": timing or _entry_timing(),
        "signals": signals,
        "industries_covered": industries_covered,
        "summary": {
            "total": len(signals),
            "buy": buy_count,
            "sell": sell_count,
            "max_per_day": MAX_SIGNALS_PER_DAY,
            "held_positions": (
                len(_get_held_codes(_load_signals_db()))
                if allow_executable else None
            ),
            "industries": len(industries_covered),
        },
    }


def _estimate_volatility(stock: dict) -> float:
    factors = stock.get("factors", {})
    idio_vol = abs(factors.get("idio_vol_60d", 0))
    low_vol = factors.get("low_vol_60d", 0)
    base_vol = 0.025
    if low_vol > 1.0:
        base_vol *= 0.6
    elif low_vol < -1.0:
        base_vol *= 1.4
    if idio_vol > 1.0:
        base_vol *= 1.2
    return round(base_vol, 4)


def _flow_score(net_yi: float | None) -> float:
    if net_yi is None: return 0.0
    if net_yi > 1.0: return 1.0
    if net_yi > 0.3: return 0.5
    if net_yi < -1.0: return -1.0
    if net_yi < -0.3: return -0.5
    return 0.0


def _flow_direction(net_yi: float | None) -> str:
    if net_yi is None: return "none"
    if net_yi > 0.1: return "in"
    if net_yi < -0.1: return "out"
    return "none"


def _flow_text(code: str, ff_stocks: dict) -> str:
    f = ff_stocks.get(code)
    if not f or not isinstance(f, dict): return ""
    raw = f.get("net_yi")
    if raw is None:
        # P4-2c：缺值不再被 (None or 0) 折叠成“零”，如实标记不可得
        return "资金流数据不可得（缺失≠零流入）"
    net = normalize_fund_flow_yi(raw)
    if net is None:
        return "资金流数据不可得（非法数值）"
    if net > 0.5:  return f"主力净流入 {net:.2f}亿"
    if net > 0:    return f"主力小幅流入 {net:.2f}亿"
    if net < -0.5: return f"主力净流出 {abs(net):.2f}亿"
    if net < 0:    return f"主力小幅流出 {abs(net):.2f}亿"
    return "资金动向不明"


def _safe_headline(raw: object) -> dict | None:
    """Never repeat metrics from a backtest that the snapshot itself revoked."""
    if not isinstance(raw, dict):
        return None
    if raw.get("invalidated") is True:
        return {
            "invalidated": True,
            "method": "旧回测已否决",
            "audit_message": raw.get("audit_message") or "旧回测不可用于收益或交易判断。",
        }
    return raw


def _get_kronos_ret(code: str, kronos_stocks: dict) -> float | None:
    """获取 Kronos 20日原始模型输出；显示与后续复核均不截断该数值."""
    info = kronos_stocks.get(code, {})
    if not isinstance(info, dict): return None
    if info.get("pred_ret_20d") is None:
        return None
    try:
        raw = float(info["pred_ret_20d"]) * 100
        return round(raw, 2)
    except (TypeError, ValueError):
        return None


def get_signal_history(limit: int = 100) -> list[dict]:
    all_signals = _load_signals_db()
    all_signals.sort(key=lambda s: s.get("date", ""), reverse=True)
    return all_signals[:limit]


def get_portfolio_status(quotes_provider=None) -> dict:
    history = _load_signals_db()
    positions: dict[str, dict] = {}
    closed_trades: list[dict] = []
    realized_pnl = 0.0

    for sig in sorted(history, key=lambda s: s.get("date", "")):
        code = sig["code"]
        action = sig.get("action")
        price = sig.get("price")
        if not price: continue
        if action == "BUY" and code not in positions:
            positions[code] = {
                "code": code, "name": sig.get("name", code),
                "entry_price": price, "entry_date": sig.get("date", ""),
                "entry_rank": sig.get("rank"),
                "industry": sig.get("industry", ""),
            }
        elif action == "SELL" and code in positions:
            entry = positions.pop(code)
            pnl = (price - entry["entry_price"]) / entry["entry_price"]
            realized_pnl += pnl
            closed_trades.append({
                **entry, "exit_price": price,
                "exit_date": sig.get("date", ""),
                "pnl_pct": round(pnl * 100, 2),
            })

    if quotes_provider and positions:
        codes = list(positions.keys())
        try:
            quotes = quotes_provider.get_quotes(codes)
            if quotes:
                for code, q in quotes.items():
                    if code in positions:
                        cp = getattr(q, "price", None) or 0
                        if cp > 0:
                            positions[code]["current_price"] = cp
                            entry = positions[code]["entry_price"]
                            positions[code]["unrealized_pnl_pct"] = round(
                                (cp - entry) / entry * 100, 2
                            )
        except Exception:
            pass

    return {
        "positions": list(positions.values()),
        "position_count": len(positions),
        "max_positions": MAX_POSITIONS,
        "closed_count": len(closed_trades),
        "realized_pnl_pct": round(realized_pnl * 100, 2),
        "recent_closed": closed_trades[-5:],
        "account_capital": ACCOUNT_CAPITAL,
    }
