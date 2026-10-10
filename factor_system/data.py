"""数据管道层（任务书第二部分·诚实适配）。

加载 backtest_project 已建的 PIT 面板，并接入已审计 V4 增量（本地真实数据）：
  - universe_daily.parquet  全市场日线（OHLCV/换手/成交额/ST/停牌/复权/流通市值）
  - pit_fundamentals.parquet PIT PE/PB（按披露日对齐，杜绝未来函数）
  - stock_meta.parquet      行业/区间元数据

统一输出为「日期 × 代码」矩阵（pandas DataFrame，axis=0 时序 / axis=1 横截面），
供算子库与因子模板直接计算。数据一次载入，缓存于模块级，避免重复读盘。
"""

from __future__ import annotations

import gc
import hashlib
import io
import json
import os
import re
import secrets
import stat as stat_module
import sys
import time
import zipfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from . import config
from .v4_panel import append_verified_v4_rows, quality_cache_source
from quantqc import ashare as qa


PANEL_UNIVERSE_SCOPE = {
    "id": "sh_sz_main_chinext",
    "label": "沪深主板+创业板研究截面（不含科创板、北交所）",
    "included_boards": ["MAIN_SH", "MAIN_SZ", "GEM"],
    "excluded_segments": ["科创板", "北交所"],
}

# ── Step 0.5 统一数据层接入：数据来源审计（backtest_engine.data_source）──
_here = Path(__file__).resolve()
PROJECT_ROOT = _here.parent.parent
_PBT = _here.parent.parent / "research" / "backtest_project"
if str(_PBT) not in sys.path:
    sys.path.insert(0, str(_PBT))
try:
    from backtest_engine.data_source import record_panel_access, data_layer_health
except Exception as _e:  # pragma: no cover — 极窄降级：无法导入时审计不可用，不阻断
    def record_panel_access(*_a, **_k):  # type: ignore
        return None
    data_layer_health = None  # type: ignore


@dataclass
class Panel:
    close: pd.DataFrame          # 日期×代码，复权收盘
    ret: pd.DataFrame            # 日收益
    vol: pd.DataFrame            # 成交量
    amount: pd.DataFrame         # 成交额
    turnover: pd.DataFrame       # 换手率
    float_mv: pd.DataFrame       # 流通市值（近似）
    pct_chg: pd.DataFrame        # 涨跌幅
    open_: pd.DataFrame          # 开盘价（未复权，保留供口径对照）
    adj_open: pd.DataFrame       # 复权开盘价（P2-2：修复 open-to-open 除权除息虚假跌幅）
    high: pd.DataFrame           # 最高价
    low: pd.DataFrame            # 最低价
    pe_ttm: pd.DataFrame | None  # PIT PE
    pb_mrq: pd.DataFrame | None  # PIT PB
    # 执行状态与价格面板分离：停牌/ST 价格仍供持仓估值，
    # 但不得因为“有价格”就被当作可新入选/可成交。旧夹具可留 None。
    signal_eligible: pd.DataFrame | None = None  # 当日可被新信号选中
    tradable: pd.DataFrame | None = None         # 当日可在开盘成交（ST 仍可卖）
    is_st_panel: pd.DataFrame | None = None      # 当日 ST 状态
    is_delisted_panel: pd.Series | None = None   # 源面板明确标识的退市股
    last_date: pd.Series | None = None            # 每只股最后一个源数据日
    meta: pd.DataFrame = field(default_factory=pd.DataFrame)
    dates: list = field(default_factory=list)
    codes: list = field(default_factory=list)
    raw_close: pd.DataFrame | None = None  # 未复权源收盘：信号/估值，与限价参考分离
    limit_reference: pd.DataFrame | None = None  # 当执行日源参考价，缺失不能推成交

    def __post_init__(self) -> None:
        """让 ``research_close_forward_return(panel.close, ...)`` 自动获得退市口径。

        只绑定小型代码级字典；价格矩阵本身仍是普通 DataFrame。
        未明确标识为退市的代码不会因为缺价被推断为退市。
        """
        if self.is_delisted_panel is None or self.last_date is None:
            return
        # ``is_delisted_panel`` may arrive as nullable Int8 from parquet.
        # Filling that dtype with the boolean ``False`` raises in pandas;
        # compare first so missing/zero become an explicit boolean mask.
        flags = (pd.Series(self.is_delisted_panel)
                 .reindex(self.close.columns).eq(1).fillna(False))
        last = pd.to_datetime(
            pd.Series(self.last_date).reindex(self.close.columns), errors="coerce")
        self.close.attrs["factor_system_delisting"] = {
            "is_delisted_panel": {
                str(code): bool(flags.loc[code]) for code in self.close.columns
            },
            "last_date": {
                str(code): pd.Timestamp(last.loc[code]).isoformat()
                for code in self.close.columns if pd.notna(last.loc[code])
            },
        }


class PanelInputEvidenceError(RuntimeError):
    """Canonical panel is not bound to a passing input-integrity audit."""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_panel_manifest(path: Path) -> dict:
    manifest_path = path.with_name("panel_manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise PanelInputEvidenceError("日线面板输入完整性清单缺失或损坏") from exc
    expected = str(
        (manifest.get("outputs") or {}).get("universe_daily.parquet") or ""
    )
    audit = manifest.get("company_action_input_audit") or {}
    audit_reference = str(audit.get("path") or "")
    audit_expected_sha = str(audit.get("sha256") or "")
    audit_path = PROJECT_ROOT / audit_reference
    try:
        audit_path = audit_path.resolve(strict=True)
        audit_path.relative_to(PROJECT_ROOT.resolve())
    except (OSError, ValueError):
        audit_path = Path()
    audit_matches = bool(
        audit_reference
        and not Path(audit_reference).is_absolute()
        and len(audit_expected_sha) == 64
        and audit_path.is_file()
        and _file_sha256(audit_path) == audit_expected_sha
    )
    if (
        manifest.get("input_integrity_ready") is not True
        or manifest.get("status") != "input_integrity_ready"
        or not expected
        or _file_sha256(path) != expected
        or audit.get("matches_current_panel") is not True
        or not audit_matches
    ):
        raise PanelInputEvidenceError(
            "日线面板尚未通过当前公司行动/ST 输入完整性审计"
        )
    return manifest


def _read_panel() -> pd.DataFrame:
    p = config.RESEARCH_DATA / "universe_daily.parquet"
    if not p.exists():
        raise FileNotFoundError(f"日线面板缺失: {p}")
    _verified_panel_manifest(p)
    import pyarrow.parquet as pq
    available_columns = set(pq.read_schema(p).names)
    reference_columns = [name for name in ("pre_close", "pre_close_basis")
                         if name in available_columns]
    base = pd.read_parquet(p, columns=[
        "ts_code", "trade_date", "open", "high", "low", "close",
        "vol", "amount", "turnover_rate", "pct_chg", "is_st",
        "trade_status", "adjust_flag", "adj_close", "float_mv", "board",
        "is_delisted_panel", "price_source", "source_snapshot_sha256",
        "is_st_provenance", "is_st_source_sha256", "return_provenance",
        "adjustment_trusted", "stable_instrument_id", "exchange_code",
        "identity_provenance", "identity_source_sha256",
    ] + reference_columns)
    base["trade_date"] = pd.to_datetime(base["trade_date"])
    # 只追加基础 parquet 最大日期之后、被 manifest + quality + 官方日历
    # 共同证明的记录。当前 V4 清单绝不参与过滤/回填历史股票池。
    df, v4 = append_verified_v4_rows(base)
    as_of = df["trade_date"].max().strftime("%Y-%m-%d") if len(df) else "unknown"
    record_panel_access("universe_daily.parquet", as_of=as_of,
                        note=(
                            "因子系统批量面板（历史 parquet + 已审计 V4 增量；"
                            f"V4={v4['published_end']}；统一接口为单票粒度无法承载）"
                        ))
    return df


def _read_pit() -> pd.DataFrame:
    p = config.RESEARCH_DATA / "pit_fundamentals.parquet"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_parquet(p, columns=["ts_code", "trade_date", "pe_ttm", "pb_mrq"])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    as_of = df["trade_date"].max().strftime("%Y-%m-%d") if len(df) else "unknown"
    record_panel_access("pit_fundamentals.parquet", as_of=as_of,
                        note="因子系统 PIT PE/PB（本地 parquet 权威源）")
    return df


def _read_meta() -> pd.DataFrame:
    p = config.RESEARCH_DATA / "stock_meta.parquet"
    if not p.exists():
        return pd.DataFrame()
    frame = pd.read_parquet(p)
    as_of = "unknown"
    for column in ("last_date", "effective_date", "trade_date"):
        if column not in frame:
            continue
        dates = pd.to_datetime(frame[column], errors="coerce").dropna()
        if not dates.empty:
            as_of = dates.max().date().isoformat()
            break
    record_panel_access("stock_meta.parquet", as_of=as_of,
                        note="因子系统行业/区间元数据快照")
    return frame


def _panel_transform_source_sha256() -> dict[str, str]:
    """Bind cached Panels to the code that constructs their PIT values."""
    return {
        name: hashlib.sha256((_here.parent / name).read_bytes()).hexdigest()
        for name in ("data.py", "v4_panel.py")
    }


def _panel_cache_path() -> Path:
    """四源稳定 manifest 键；V4 只在新质量报告发布后推进。"""
    names = (
        "universe_daily.parquet",
        "pit_fundamentals.parquet",
        "stock_meta.parquet",
    )
    sources = []
    for name in names:
        path = config.RESEARCH_DATA / name
        stat = path.stat() if path.exists() else None
        sources.append({
            "name": name,
            "st_mtime_ns": int(stat.st_mtime_ns) if stat else 0,
            "st_ctime_ns": int(stat.st_ctime_ns) if stat else 0,
            "st_size": int(stat.st_size) if stat else 0,
            "st_ino": int(stat.st_ino) if stat else 0,
        })
    panel_manifest = config.RESEARCH_DATA / "panel_manifest.json"
    panel_manifest_stat = panel_manifest.stat() if panel_manifest.exists() else None
    sources.append({
        "name": "panel_manifest.json",
        "st_mtime_ns": int(panel_manifest_stat.st_mtime_ns) if panel_manifest_stat else 0,
        "st_ctime_ns": int(panel_manifest_stat.st_ctime_ns) if panel_manifest_stat else 0,
        "st_size": int(panel_manifest_stat.st_size) if panel_manifest_stat else 0,
        "st_ino": int(panel_manifest_stat.st_ino) if panel_manifest_stat else 0,
        "sha256": _file_sha256(panel_manifest) if panel_manifest_stat else "",
    })
    # 下载器会逐文件原子更新，manifest 也先于 quality 发布。若把这些中间
    # 文件直接放进 key，daemon 会误以为半批数据已可用。quality 是最后一道
    # 全量门禁，只有它原子发布后缓存才前进；冷构建再核对三证据和物理 SHA。
    sources.append(quality_cache_source())
    manifest = {
        "panel_schema": "v9-input-provenance-shsz-main-gem-audited-v4",
        "universe_scope": PANEL_UNIVERSE_SCOPE,
        "sources": sources,
        "transform_source_sha256": _panel_transform_source_sha256(),
    }
    digest = hashlib.sha256(json.dumps(
        manifest, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()[:20]
    return config.CACHE_DIR / f"panel_v9_{digest}.panel"


def _filter_universe(df: pd.DataFrame) -> pd.DataFrame:
    """宇宙基础过滤（诚实口径）+ 复权开盘。

    不在这里用全周期总 bars 删除代码：那会让未来交易数据决定股票
    在早期是否存在，造成生存者偏差。最小历史长度在 ``_build_panel``
    中按每个信号日当时可见的累计有效 bars 动态判定。
    """
    # 停牌/ST 行不能删：它们不可新入选，但已持仓仍需连续估值。
    # 可选/可交易性在 _build_panel 中用独立 mask 表达。
    codes = df["ts_code"].astype(str)
    scoped = ~codes.str.startswith(("688", "689")) & ~codes.str.endswith(".BJ")
    if "board" in df.columns:
        scoped &= ~df["board"].isin({"STAR", "BJ"})
    df = df[scoped]  # 与 V4 真实覆盖一致：沪深主板+创业板，排除科创/北交。
    df = df.copy()
    df["trade_status"] = pd.to_numeric(df["trade_status"], errors="coerce")
    # 未知 ST 状态不能冒充明确非 ST；eq(0) 会对 NaN fail-closed。
    df["is_st"] = pd.to_numeric(df["is_st"], errors="coerce")
    if "adjustment_trusted" not in df.columns:
        df["adjustment_trusted"] = False
    df["adjustment_trusted"] = df["adjustment_trusted"].eq(True)
    if "return_provenance" not in df.columns:
        df["return_provenance"] = "unavailable:missing_provenance"
    df["_return_chain_trusted"] = (
        df["return_provenance"].astype("string").str.startswith(
            "trusted:", na=False,
        )
    )
    if "is_delisted_panel" not in df.columns:
        df["is_delisted_panel"] = 0
    df["is_delisted_panel"] = pd.to_numeric(
        df["is_delisted_panel"], errors="coerce").fillna(0).astype(int)
    df["adj_close"] = df["adj_close"].replace(0, np.nan)
    df["_trusted_adj_close"] = df["adj_close"].where(
        df["adjustment_trusted"]
    )
    # P2-2 复权开盘：同源复权因子 adj_factor = adj_close/close，应用到 open 得到复权开盘。
    ratio = df["adj_close"] / df["close"].replace(0, np.nan)
    df["adj_open"] = (df["open"] * ratio).where(df["adjustment_trusted"])
    # 此处也不截 IS_START：信号窗之前的真实历史必须计入当日累计 bars。
    # 同时不设 OOS_END 上限，让 2026+ forward shadow 新数据进入面板。
    return df


def mask_signal_eligible(panel: Panel, factor: pd.DataFrame) -> pd.DataFrame:
    """仅保留信号日真实可入选股票；无显式 PIT mask 的旧夹具原样返回。"""
    eligible = getattr(panel, "signal_eligible", None)
    if not isinstance(eligible, pd.DataFrame):
        return factor
    mask = eligible.reindex(index=factor.index, columns=factor.columns).fillna(False)
    return factor.where(mask.astype(bool))


# M2.2 float32 瘦身（诚实口径）：
# 研究信号矩阵按现有口径降 float32，不承诺近等值 rank/IC 完全不变。
# raw_close / open_ 保留源 float64；float32 可移动半分舍入和涨跌停边界。
# float_mv 保留 float64（容量估算）。不能恢复接入前已丢失的源精度。
def _slim32(m: pd.DataFrame, keep64: bool = False) -> pd.DataFrame:
    if m is None or keep64:
        return m
    return m.astype(np.float32)


def _build_panel() -> Panel:
    """（重活）读源文件并构建 PIT 面板矩阵（M2.2：float32 瘦身）。"""
    df = _filter_universe(_read_panel())
    meta = _read_meta()
    pit = _read_pit()

    # PIT 资格：只计数当时可交易且非 ST 的有效 bar。历史特征是否已公开
    # 与当日能否新入选是两件事：MIN_BARS 只进入 signal/tradable mask，
    # 不能反向抹掉此前真实存在的价格、成交量、市值或 PIT 基本面。
    df = df.sort_values(["ts_code", "trade_date"], kind="stable").copy()
    derived_last_date = df.groupby("ts_code", sort=False)["trade_date"].max()
    delisted_codes = df.groupby("ts_code", sort=False)["is_delisted_panel"].max().astype(bool)
    valid_bar = (
        df["_trusted_adj_close"].notna()
        & df["adjustment_trusted"]
        & df["trade_status"].eq(1)
        & df["is_st"].eq(0)
    )
    df["_valid_bar"] = valid_bar
    history_bars = valid_bar.groupby(df["ts_code"], sort=False).cumsum()
    # panel.ret 的当前日观测口径不含 MIN_BARS。停牌/ST 行即使为持仓估值
    # 保留了价格，也不得因此伪造 0 收益；普通缺价由 fill_method=None 保持 NaN。
    df["_return_observed"] = valid_bar & df["_return_chain_trusted"]
    df["_valuation_eligible"] = history_bars.ge(config.MIN_BARS)
    df["_signal_eligible"] = (df["_valuation_eligible"] & valid_bar
                              & df["open"].notna())
    df["_tradable"] = (
        df["_valuation_eligible"]
        & df["trade_status"].eq(1)
        & df["is_st"].notna()
        & df["adjustment_trusted"]
        & df["open"].notna()
    )
    # 资格计算完成后再截研究起点，既保留起点前历史，也不会膨胀最终矩阵。
    df = df[df["trade_date"] >= pd.Timestamp(config.IS_START)]

    codes = sorted(df["ts_code"].unique())
    dates = sorted(df["trade_date"].unique())
    dates = [d for d in dates]

    def _pivot(col: str) -> pd.DataFrame:
        sub = df[["trade_date", "ts_code", col]].dropna(subset=[col])
        piv = sub.pivot(index="trade_date", columns="ts_code", values=col)
        return piv.reindex(index=dates, columns=codes)

    def _bool_pivot(col: str) -> pd.DataFrame:
        return (df.pivot(index="trade_date", columns="ts_code", values=col)
                .reindex(index=dates, columns=codes)
                .astype("boolean").fillna(False).astype(bool))

    def _nullable_bool_pivot(col: str) -> pd.DataFrame:
        # ST 的 unknown 不是 False。保留 pd.NA，供形成、执行与审计统一
        # fail-closed；普通资格 mask 才由 _bool_pivot 明确补为 False。
        return (df.pivot(index="trade_date", columns="ts_code", values=col)
                .reindex(index=dates, columns=codes).astype("boolean"))

    close = _slim32(_pivot("_trusted_adj_close"))
    raw_close = _pivot("close").astype(np.float64)
    return_observed = _bool_pivot("_return_observed")
    # Current provenance already proves a same-method previous price. Requiring
    # the immediately previous row to be a valid trading/non-ST observation
    # additionally blocks suspension/ST zero returns and the first resume day.
    previous_valid_bar = _bool_pivot("_valid_bar").shift(1).fillna(False)
    consecutive_observed = return_observed & previous_valid_bar
    ret = _slim32(
        close.pct_change(fill_method=None).astype(np.float64)
        .where(consecutive_observed)
    )  # 先 float64 算收益再降；停牌/ST/缺价会断开日收益链
    open_ = _pivot("open").astype(np.float64)
    adj_open = _slim32(_pivot("adj_open"))
    high = _slim32(_pivot("high"))
    low = _slim32(_pivot("low"))
    vol = _slim32(_pivot("vol"))
    amount = _slim32(_pivot("amount"))
    turnover = _slim32(_pivot("turnover_rate"))
    float_mv = _pivot("float_mv")        # 保留 float64（容量估算精度）
    pct_chg = _slim32(_pivot("pct_chg"))
    limit_reference = None
    if {"pre_close", "pre_close_basis"} <= set(df.columns):
        declared = df["pre_close_basis"].eq(
            "provider_ex_rights_reference_not_previous_raw_close")
        supplied = df.loc[declared, "pre_close"]
        if (getattr(supplied.dtype, "kind", None) == "f"
                and getattr(supplied.dtype, "itemsize", 8) < 8):
            raise ValueError("limit_reference_source_precision_lost")
        if supplied.map(lambda value: isinstance(value, (bool, np.bool_))).any():
            raise ValueError("limit_reference_numeric_dtype_invalid")
        df["_limit_reference"] = pd.to_numeric(
            df["pre_close"], errors="coerce").where(declared)
        limit_reference = _pivot("_limit_reference").astype(np.float64)
    signal_eligible = _bool_pivot("_signal_eligible")
    tradable = _bool_pivot("_tradable")
    is_st_panel = _nullable_bool_pivot("is_st")

    # 退市分类来自源 parquet 的 is_delisted_panel，最后日优先用
    # stock_meta.last_date，缺失时才回退到本次源面板最后日。
    last_date = derived_last_date.reindex(codes)
    if (isinstance(meta, pd.DataFrame) and not meta.empty
            and {"ts_code", "last_date"}.issubset(meta.columns)):
        meta_last = (meta.drop_duplicates("ts_code", keep="last")
                     .set_index("ts_code")["last_date"])
        meta_last = pd.to_datetime(meta_last, errors="coerce").reindex(codes)
        last_date = meta_last.combine_first(last_date)
    last_date = pd.to_datetime(last_date, errors="coerce")
    is_delisted_panel = delisted_codes.reindex(codes).fillna(False).astype(bool)

    # PIT 基本面：pe_ttm / pb_mrq
    pe_ttm = pb_mrq = None
    if not pit.empty:
        pe_ttm = _slim32(pit.pivot(index="trade_date", columns="ts_code", values="pe_ttm")
                         .reindex(index=dates, columns=codes))
        pb_mrq = _slim32(pit.pivot(index="trade_date", columns="ts_code", values="pb_mrq")
                         .reindex(index=dates, columns=codes))

    del df
    gc.collect()
    return Panel(
        close=close, ret=ret, vol=vol, amount=amount, turnover=turnover,
        float_mv=float_mv, pct_chg=pct_chg,
        open_=open_, adj_open=adj_open,
        high=high, low=low,
        pe_ttm=pe_ttm, pb_mrq=pb_mrq, meta=meta,
        signal_eligible=signal_eligible, tradable=tradable,
        is_st_panel=is_st_panel,
        is_delisted_panel=is_delisted_panel,
        last_date=last_date,
        dates=dates, codes=codes, raw_close=raw_close,
        limit_reference=limit_reference,
    )


def load_thin_panel() -> Panel:
    """回测区瘦子面板（M2.2）。

    因子重建依赖 close/ret/vol/amount/turnover/open_/low/high/adj_open/
    raw_close/float_mv/pe/pb/meta 全部矩阵。研究信号保留已有 float32 口径，
    执行开盘和收盘保留源 float64。旧瘦身阶段的 344MB 测量不能代表当前
    新增原价矩阵后的内存用量；挖掘/回测共用同一只读快照。
    """
    return load_panel()


class _PanelManifestChanged(RuntimeError):
    """Panel 载入期间源 manifest 发生变化；仅供 ``load_panel`` 有界重试。"""


def _require_current_panel_manifest(cached: Path) -> None:
    _verified_panel_manifest(config.RESEARCH_DATA / "universe_daily.parquet")
    current = _panel_cache_path()
    if current != cached:
        raise _PanelManifestChanged(
            f"Panel 载入期间数据源已变化: {cached.name} -> {current.name}"
        )


_PANEL_FRAME_FIELDS = (
    "close", "ret", "vol", "amount", "turnover", "float_mv", "pct_chg",
    "open_", "adj_open", "high", "low", "signal_eligible", "tradable",
    "is_st_panel", "meta",
)
_PANEL_OPTIONAL_FRAME_FIELDS = ("pe_ttm", "pb_mrq", "raw_close", "limit_reference")
_PANEL_CACHE_SCHEMA = "factor-panel-parquet-bundle-v1"
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _parquet_bytes(frame: pd.DataFrame) -> bytes:
    stream = io.BytesIO()
    frame.to_parquet(stream, index=True)
    return stream.getvalue()


def _read_panel_cache(path: Path) -> Panel:
    """Read the content-addressed cache without executable deserialization."""
    with zipfile.ZipFile(path, "r") as archive:
        try:
            manifest_raw = archive.read("manifest.json")
            manifest = json.loads(manifest_raw.decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Panel cache manifest invalid") from exc
        if manifest.get("schema") != _PANEL_CACHE_SCHEMA:
            raise ValueError("Panel cache schema unsupported")
        members = manifest.get("members")
        if not isinstance(members, dict):
            raise ValueError("Panel cache members missing")

        frames: dict[str, pd.DataFrame | None] = {}
        state: pd.DataFrame | None = None
        required = set(_PANEL_FRAME_FIELDS)
        optional = set(_PANEL_OPTIONAL_FRAME_FIELDS)
        if not required.issubset(members) or not set(members).issubset(
            required | optional | {"state"}
        ) or "state" not in members:
            raise ValueError("Panel cache member set invalid")
        for name, expected_sha in members.items():
            member_name = f"{name}.parquet"
            try:
                raw = archive.read(member_name)
            except KeyError as exc:
                raise ValueError(f"Panel cache member missing: {name}") from exc
            if hashlib.sha256(raw).hexdigest() != expected_sha:
                raise ValueError(f"Panel cache member hash mismatch: {name}")
            frame = pd.read_parquet(io.BytesIO(raw))
            if name == "state":
                state = frame
            else:
                frames[name] = frame

    close = frames.get("close")
    if not isinstance(close, pd.DataFrame):
        raise ValueError("Panel cache close matrix missing")
    for name in _PANEL_FRAME_FIELDS:
        frame = frames.get(name)
        if not isinstance(frame, pd.DataFrame):
            raise ValueError(f"Panel cache frame missing: {name}")
        if name != "meta" and (
            not frame.index.equals(close.index)
            or not frame.columns.equals(close.columns)
        ):
            raise ValueError(f"Panel cache axes mismatch: {name}")
    for name in _PANEL_OPTIONAL_FRAME_FIELDS:
        frame = frames.get(name)
        if frame is not None and (
            not frame.index.equals(close.index)
            or not frame.columns.equals(close.columns)
        ):
            raise ValueError(f"Panel cache axes mismatch: {name}")
    if not isinstance(state, pd.DataFrame) or not state.index.equals(close.columns):
        raise ValueError("Panel cache state axes mismatch")
    required_state = {"is_delisted_panel", "last_date"}
    if not required_state.issubset(state.columns):
        raise ValueError("Panel cache state fields missing")

    raw = frames.get("raw_close")
    if raw is not None:
        _validate_raw_close(raw, close)
    reference = frames.get("limit_reference")
    if reference is not None:
        _validate_limit_reference(reference, close)
    return Panel(
        close=close,
        ret=frames["ret"],
        vol=frames["vol"],
        amount=frames["amount"],
        turnover=frames["turnover"],
        float_mv=frames["float_mv"],
        pct_chg=frames["pct_chg"],
        open_=frames["open_"],
        adj_open=frames["adj_open"],
        high=frames["high"],
        low=frames["low"],
        pe_ttm=frames.get("pe_ttm"),
        pb_mrq=frames.get("pb_mrq"),
        signal_eligible=frames["signal_eligible"],
        tradable=frames["tradable"],
        is_st_panel=frames["is_st_panel"],
        is_delisted_panel=state["is_delisted_panel"].astype(bool),
        last_date=pd.to_datetime(state["last_date"], errors="coerce"),
        meta=frames["meta"],
        dates=list(close.index),
        codes=list(close.columns),
        raw_close=frames.get("raw_close"),
        limit_reference=frames.get("limit_reference"),
    )


def load_verified_panel_snapshot(path: str | Path, sha256: str) -> Panel:
    """Load one caller-named cache inode, without discovery or rebuilding.

    The blind evaluator must consume the exact file named by its frozen
    authorization.  Keep the verified descriptor open through decoding so a
    path replacement cannot make the hash check and Panel read see different
    files.
    """
    if not isinstance(path, (str, Path)) or not str(path):
        raise ValueError("[blind_panel_path_invalid] Panel 路径必须为项目内相对路径")
    relative = Path(path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("[blind_panel_path_invalid] Panel 路径必须为项目内相对路径")
    if not isinstance(sha256, str) or not _SHA256_HEX.fullmatch(sha256):
        raise ValueError("[blind_panel_sha256_invalid] Panel SHA-256 必须为小写64位十六进制")

    root = PROJECT_ROOT.resolve(strict=True)
    cache_root = config.CACHE_DIR.resolve(strict=True)
    unresolved = root.joinpath(relative)
    current = root
    try:
        for part in relative.parts:
            current = current / part
            if stat_module.S_ISLNK(os.lstat(current).st_mode):
                raise ValueError("[blind_panel_symlink_forbidden] Panel 路径不得包含符号链接")
        resolved = unresolved.resolve(strict=True)
        resolved.relative_to(root)
        resolved.relative_to(cache_root)
        path_stat = os.lstat(unresolved)
    except ValueError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ValueError("[blind_panel_path_invalid] Panel 缓存缺失或路径无效") from exc
    if not stat_module.S_ISREG(path_stat.st_mode):
        raise ValueError("[blind_panel_not_regular] Panel 缓存必须是普通文件")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise ValueError("[blind_panel_open_failed] Panel 缓存无法安全只读打开") from exc

    def identity(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    try:
        before = os.fstat(descriptor)
        if not stat_module.S_ISREG(before.st_mode) or (
            before.st_dev,
            before.st_ino,
        ) != (path_stat.st_dev, path_stat.st_ino):
            raise ValueError("[blind_panel_identity_changed] Panel 路径身份已变化")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after_hash = os.fstat(descriptor)
        if identity(after_hash) != identity(before):
            raise ValueError("[blind_panel_changed_during_hash] Panel 哈希期间发生变化")
        actual_sha256 = digest.hexdigest()
        if actual_sha256 != sha256:
            raise ValueError("[blind_panel_sha256_mismatch] Panel 物理 SHA-256 不匹配")

        os.lseek(descriptor, 0, os.SEEK_SET)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            panel = _read_panel_cache(stream)
            after_read = os.fstat(stream.fileno())
        if identity(after_read) != identity(before):
            raise ValueError("[blind_panel_changed_during_read] Panel 读取期间发生变化")
        final_path_stat = os.lstat(unresolved)
        if identity(final_path_stat) != identity(before):
            raise ValueError("[blind_panel_path_replaced] Panel 路径在读取期间被替换")
    finally:
        os.close(descriptor)

    if not isinstance(panel, Panel):
        raise ValueError("[blind_panel_type_invalid] Panel 缓存内容类型无效")
    panel._panel_cache_key = resolved.name
    panel._panel_cache_path = relative.as_posix()
    panel._panel_cache_sha256 = actual_sha256
    return panel


def _write_panel_cache(path: Path, panel: Panel) -> None:
    """Atomically publish a non-executable, member-hashed Panel archive."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    )
    raw_close_frame(panel, required=False)
    limit_reference_frame(panel, required=False)
    frames = {
        name: getattr(panel, name) for name in _PANEL_FRAME_FIELDS
    }
    for name in _PANEL_OPTIONAL_FRAME_FIELDS:
        frame = getattr(panel, name)
        if isinstance(frame, pd.DataFrame):
            frames[name] = frame
    frames["state"] = pd.DataFrame({
        "is_delisted_panel": pd.Series(
            panel.is_delisted_panel, index=panel.close.columns,
        ).eq(1).fillna(False).astype(bool),
        "last_date": pd.to_datetime(
            pd.Series(panel.last_date, index=panel.close.columns), errors="coerce",
        ),
    }, index=panel.close.columns)

    try:
        member_hashes: dict[str, str] = {}
        with zipfile.ZipFile(
            temporary, "x", compression=zipfile.ZIP_STORED, allowZip64=True,
        ) as archive:
            for name, frame in frames.items():
                if not isinstance(frame, pd.DataFrame):
                    raise TypeError(f"Panel cache frame invalid: {name}")
                raw = _parquet_bytes(frame)
                member_hashes[name] = hashlib.sha256(raw).hexdigest()
                archive.writestr(f"{name}.parquet", raw)
            archive.writestr("manifest.json", json.dumps({
                "schema": _PANEL_CACHE_SCHEMA,
                "members": member_hashes,
            }, sort_keys=True, separators=(",", ":")))
        # Windows _commit (os.fsync) needs a writable handle. r+b preserves
        # the completed archive while permitting its durable publish sync.
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        # Concurrent Windows replacements may briefly be denied; error5 can
        # also be a permanent ACL failure, so do not assume every refusal clears.
        for attempt in range(8):
            try:
                os.replace(temporary, path)
            except PermissionError as exc:
                if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 7:
                    raise
                time.sleep(.01 * (2 ** attempt))
            else:
                break
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


@lru_cache(maxsize=2)
def _load_panel_for_cache_path(cache_path: str) -> Panel:
    """按实际 source manifest key 缓存，避免 Web 长进程给旧 Panel 贴新 key。"""
    cached = Path(cache_path)
    cache_miss = object()
    panel = cache_miss
    if cached.exists():
        try:
            panel = _read_panel_cache(cached)
        except Exception:
            panel = cache_miss  # 缓存损坏则重建
    if panel is not cache_miss:
        _require_current_panel_manifest(cached)
        # 重绑轻量退市元数据，保证诊断标签口径一致。
        if isinstance(panel, Panel):
            panel.__post_init__()
            panel._panel_cache_key = cached.name
        return panel

    panel = _build_panel()
    # 必须在绑定 key 和写缓存前复核；否则 40 秒冷构建期间更新数据源，
    # 会把新旧混合 Panel 永久写到旧 manifest 文件名下。
    _require_current_panel_manifest(cached)
    if isinstance(panel, Panel):
        panel._panel_cache_key = cached.name
    try:
        _write_panel_cache(cached, panel)
    except Exception:
        pass  # 写缓存失败不影响本次使用
    return panel


def load_panel() -> Panel:
    """构建宇宙过滤后的 PIT 面板（进程内缓存 + 磁盘持久化缓存）。

    8GB 单机：避免每个新进程都重读 273MB parquet + 12 次 pivot（冷读 ~40s），
    首次构建后按历史三源 + V4 质量证据序列化，后续进程秒级加载；
    只有完整的新质量报告发布才触发重建，半批增量继续使用旧已审计快照。
    """
    last_change: _PanelManifestChanged | None = None
    for _attempt in range(2):
        _verified_panel_manifest(config.RESEARCH_DATA / "universe_daily.parquet")
        requested = _panel_cache_path()
        try:
            panel = _load_panel_for_cache_path(str(requested))
            current = _panel_cache_path()
            if current == requested:
                return panel
            last_change = _PanelManifestChanged(
                f"Panel 返回前数据源已变化: {requested.name} -> {current.name}"
            )
        except _PanelManifestChanged as exc:
            last_change = exc
        # lru 命中会跳过函数体内复核；发生漂移时必须丢掉所有进程内 Panel，
        # 下一次只允许按新的 manifest 重新载入/构建。
        _load_panel_for_cache_path.cache_clear()
    raise RuntimeError(
        "Panel 载入期间数据源持续变化，已重试一次并 fail-closed"
    ) from last_change


def load_last_good_panel(*, exclude: str | Path | None = None) -> Panel:
    """读取最近一个已成功落盘的内容寻址 Panel，不把它冒充当前输入。"""
    excluded = Path(exclude).name if exclude is not None else ""
    errors: list[str] = []
    try:
        candidates = sorted(
            config.CACHE_DIR.glob("panel_v9_*.panel"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError as exc:
        raise RuntimeError(f"无法枚举历史可信 Panel 缓存: {exc}") from exc
    for cached in candidates:
        if cached.name == excluded:
            continue
        try:
            panel = _read_panel_cache(cached)
            if not isinstance(panel, Panel):
                raise TypeError(f"缓存类型为 {type(panel).__name__}")
            panel.__post_init__()
            panel._panel_cache_key = cached.name
            return panel
        except Exception as exc:
            errors.append(f"{cached.name}: {exc}")
    detail = "; ".join(errors[-3:]) or "没有可用的旧缓存"
    raise RuntimeError(f"找不到可恢复的历史可信 Panel: {detail}")


# 保留既有调用契约（daemon/tests 会显式清缓存）。
load_panel.cache_clear = _load_panel_for_cache_path.cache_clear
load_panel.cache_info = _load_panel_for_cache_path.cache_info


def _forward_return_between(
    entry_price: pd.DataFrame,
    exit_price: pd.DataFrame,
    *,
    entry_offset: int,
    exit_offset: int,
    boundary: str | None,
    metadata_price: pd.DataFrame,
    is_delisted_panel: pd.Series | dict | None,
    last_date: pd.Series | dict | None,
    delisting_includes_entry_date: bool,
) -> pd.DataFrame:
    """按信号日位置对齐入场/退出价格，并统一处理退市与时间边界。"""
    entry = entry_price.shift(-entry_offset)
    fwd = exit_price.shift(-exit_offset) / entry - 1.0
    idx = fwd.index
    if len(idx) == 0:
        return fwd

    pos = np.arange(len(idx))
    entry_pos = pos + entry_offset
    exit_pos = pos + exit_offset
    entry_ok = entry_pos < len(idx)
    exit_ok = exit_pos < len(idx)
    values = idx.to_numpy()
    entry_dates = values[np.minimum(entry_pos, len(idx) - 1)]
    exit_dates = values[np.minimum(exit_pos, len(idx) - 1)]

    metadata = metadata_price.attrs.get("factor_system_delisting", {})
    flags = (metadata.get("is_delisted_panel", {})
             if is_delisted_panel is None else is_delisted_panel)
    endings = (metadata.get("last_date", {}) if last_date is None else last_date)
    flags = pd.Series(flags, dtype=object).reindex(fwd.columns).fillna(False)
    endings = pd.to_datetime(
        pd.Series(endings, dtype=object).reindex(fwd.columns), errors="coerce")
    for code in fwd.columns:
        if not bool(flags.loc[code]) or pd.isna(endings.loc[code]):
            continue
        last = np.datetime64(pd.Timestamp(endings.loc[code]).to_datetime64())
        entered_before_delisting = (
            entry_dates <= last if delisting_includes_entry_date
            else entry_dates < last
        )
        delisting_horizon = (
            entry_ok
            & exit_ok
            & entered_before_delisting
            & (exit_dates > last)
            & entry[code].notna().to_numpy()
        )
        if delisting_horizon.any():
            fwd.loc[idx[delisting_horizon], code] = -1.0

    if boundary is not None:
        cross = pd.Series(exit_dates > pd.Timestamp(boundary), index=idx)
        fwd[(~exit_ok) | cross] = np.nan
    return fwd


RESEARCH_CLOSE_FORWARD_LABEL = "research_diagnostic__t_close__t_h_close_v1"


def research_close_forward_return(
    close: pd.DataFrame,
    h: int = config.HOLD_DAYS,
    boundary: str | None = None,
    is_delisted_panel: pd.Series | dict | None = None,
    last_date: pd.Series | dict | None = None,
) -> pd.DataFrame:
    """研究诊断用 close-to-close 未来收益，不得用于候选晋升。

    ``fwd[t] = close[t+h]/close[t] - 1`` 会包含信号收盘到次日开盘的
    不可交易隔夜段。生产候选发现/验证必须调用 ``executable_forward_return``。

    普通未来缺价（停牌至窗口末/未知离场）保持 NaN。只有源面板明确标识
    ``is_delisted_panel`` 且满足 ``t < last_date < t+h`` 的代码，才按零回收
    结算为 -100%，避免退市负尾部从 IC/分层统计中被静默删失。元数据默认从
    ``Panel.close.attrs`` 读取，也可显式传入，兼容既有调用。

    P2-2 边界修复：给定 boundary（如 IS_END）时，fwd[t] 要求 t+h 落在 boundary 内，
    否则置 NaN——杜绝挖掘段尾部的未来收益借用验证段(或更远)价格做样本内选择（前视泄漏）。
    """
    horizon = max(1, int(h))
    fwd = _forward_return_between(
        close, close,
        entry_offset=0, exit_offset=horizon, boundary=boundary,
        metadata_price=close, is_delisted_panel=is_delisted_panel,
        last_date=last_date, delisting_includes_entry_date=False,
    )
    fwd.attrs["factor_system_label"] = {
        "id": RESEARCH_CLOSE_FORWARD_LABEL,
        "purpose": "research_diagnostic_only",
        "signal": "T_close",
        "entry": "T_close",
        "exit": "T_plus_H_close",
        "price": "adjusted_close",
    }
    return fwd


def forward_return(close: pd.DataFrame, h: int = config.HOLD_DAYS,
                   boundary: str | None = None,
                   is_delisted_panel: pd.Series | dict | None = None,
                   last_date: pd.Series | dict | None = None) -> pd.DataFrame:
    """兼容旧调用；新代码必须显式使用 ``research_close_forward_return``。

    该接口始终是 close-to-close 研究诊断标签，绝不能用于筛选、晋升或冻结。
    """
    return research_close_forward_return(
        close, h=h, boundary=boundary,
        is_delisted_panel=is_delisted_panel, last_date=last_date,
    )


EXECUTABLE_FORWARD_LABEL = (
    "t_close__t1_executable_open__t_h1_or_later_executable_open_v4_explicit_reference"
)


def _validate_raw_close(raw: pd.DataFrame, signal: pd.DataFrame) -> pd.DataFrame:
    """Validate a declared raw price matrix; never synthesize or realign it."""
    if not isinstance(raw, pd.DataFrame):
        raise ValueError("raw_close_matrix_missing_or_invalid")
    if (not raw.index.equals(signal.index) or not raw.columns.equals(signal.columns)
            or not raw.index.is_unique or not raw.columns.is_unique
            or not raw.index.is_monotonic_increasing):
        raise ValueError("raw_close_axes_invalid")
    if any(getattr(dtype, "kind", None) not in {"i", "u", "f"} for dtype in raw.dtypes):
        raise ValueError("raw_close_numeric_dtype_invalid")
    # Existing raw float64 snapshots are read-only; avoid a per-mark copy.
    if all(dtype == np.dtype("float64") for dtype in raw.dtypes):
        return raw
    return raw.astype(np.float64)


def raw_close_frame(panel: Panel, *, required: bool = True) -> pd.DataFrame | None:
    raw = getattr(panel, "raw_close", None)
    if raw is None and not required:
        return None
    return _validate_raw_close(raw, panel.close)


def _validate_limit_reference(reference: pd.DataFrame, signal: pd.DataFrame) -> pd.DataFrame:
    """Validate supplied execution-date references; never shift or synthesize."""
    if isinstance(reference, pd.DataFrame) and any(
            getattr(dtype, "kind", None) == "f" and getattr(dtype, "itemsize", 8) < 8
            for dtype in reference.dtypes):
        raise ValueError("limit_reference_source_precision_lost")
    try:
        return _validate_raw_close(reference, signal)
    except (TypeError, ValueError) as exc:
        raise ValueError(str(exc).replace("raw_close", "limit_reference")) from exc


def limit_reference_frame(panel: Panel, *, required: bool = True) -> pd.DataFrame | None:
    reference = getattr(panel, "limit_reference", None)
    if reference is None and not required:
        return None
    return _validate_limit_reference(reference, panel.close)


def _execution_endpoint_masks(
    panel: Panel,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """复用事件回测的开盘可买/可卖口径，unknown 状态一律不可执行。"""
    adj_open = panel.adj_open.astype(np.float64)
    raw_open = panel.open_.reindex_like(adj_open).astype(np.float64)
    raw_close_frame(panel, required=False)
    if (not panel.open_.index.equals(adj_open.index)
            or not panel.open_.columns.equals(adj_open.columns)):
        raise ValueError("raw_execution_open_axes_invalid")
    reference = limit_reference_frame(panel, required=False)
    if reference is None:
        blocked = pd.DataFrame(False, index=adj_open.index, columns=adj_open.columns)
        return blocked, blocked.copy()
    columns = adj_open.columns

    tradable = getattr(panel, "tradable", None)
    if isinstance(tradable, pd.DataFrame):
        tradable = (tradable.reindex_like(adj_open).fillna(False).astype(bool))
    else:
        tradable = raw_open.notna() & adj_open.notna()

    raw_st = getattr(panel, "is_st_panel", None)
    if isinstance(raw_st, pd.DataFrame):
        st = raw_st.reindex_like(adj_open).astype("boolean")
        state_known = st.notna()
        st_true = st.eq(True).fillna(False).astype(bool)
    else:
        state_known = pd.DataFrame(True, index=adj_open.index, columns=columns)
        st_true = pd.DataFrame(False, index=adj_open.index, columns=columns)

    prices_present = np.isfinite(raw_open) & (raw_open > 0) & np.isfinite(adj_open) & (adj_open > 0)
    common = dict(codes=list(columns), trade_dates=list(adj_open.index),
                  open_prices=raw_open.to_numpy(),
                  tradable=(tradable & state_known & prices_present).to_numpy(dtype=bool),
                  is_risk_warning=st_true.to_numpy(dtype=bool))
    # Same execution-date reference for both sides. It can differ from yesterday
    # raw close on ex-action dates; missing references are never forward filled.
    can_enter = pd.DataFrame(qa.opening_auction_mask(
        **common, side="buy", prev_closes=reference.to_numpy()),
        index=adj_open.index, columns=columns) & ~st_true
    can_exit = pd.DataFrame(qa.opening_auction_mask(
        **common, side="sell", prev_closes=reference.to_numpy()),
        index=adj_open.index, columns=columns)
    return can_enter.astype(bool), can_exit.astype(bool)


def executable_forward_return(
    panel: Panel,
    h: int = config.HOLD_DAYS,
    boundary: str | None = None,
) -> pd.DataFrame:
    """可晋升标签：T 收盘出信号，T+1 可买开盘入场，自然到期后可卖开盘退出。

    入场与事件回测一致：T+1 停牌、ST、状态未知或开盘贴涨停时订单取消且
    不顺延。自然退出从 T+H+1 开始；停牌或开盘贴跌停时逐日顺延到第一个
    可卖开盘。普通未成交保持 NaN；明确退市且已成功入场才按零回收结算。
    ``boundary`` 同时约束自然到期日和实际顺延退出日。
    """
    horizon = max(1, int(h))
    can_enter, can_exit = _execution_endpoint_masks(panel)
    entry = panel.adj_open.where(can_enter).shift(-1)
    executable_exit = panel.adj_open.where(can_exit)
    next_exit = executable_exit.bfill()

    positions = pd.DataFrame(
        np.broadcast_to(
            np.arange(len(executable_exit.index), dtype=float)[:, None],
            executable_exit.shape,
        ),
        index=executable_exit.index,
        columns=executable_exit.columns,
    )
    next_exit_position = positions.where(can_exit).bfill()
    exit_price = next_exit.shift(-(horizon + 1))
    actual_exit_position = next_exit_position.shift(-(horizon + 1))
    fwd = exit_price / entry - 1.0

    index = executable_exit.index
    n_dates = len(index)
    signal_pos = np.arange(n_dates)
    natural_exit_pos = signal_pos + horizon + 1
    boundary_pos = n_dates - 1
    if boundary is not None:
        boundary_pos = int(index.searchsorted(pd.Timestamp(boundary), side="right") - 1)
    natural_in_boundary = natural_exit_pos <= boundary_pos
    fwd = fwd.where(
        natural_in_boundary[:, None]
        & actual_exit_position.le(boundary_pos)
    )

    flags = pd.Series(panel.is_delisted_panel, dtype=object).reindex(
        fwd.columns
    ).fillna(False)
    endings = pd.to_datetime(
        pd.Series(panel.last_date, dtype=object).reindex(fwd.columns),
        errors="coerce",
    )
    entry_dates = index.to_numpy()[np.minimum(signal_pos + 1, n_dates - 1)]
    for column, code in enumerate(fwd.columns):
        if not bool(flags.loc[code]) or pd.isna(endings.loc[code]):
            continue
        last = pd.Timestamp(endings.loc[code])
        writeoff_pos = int(index.searchsorted(last, side="right"))
        if writeoff_pos >= n_dates or writeoff_pos > boundary_pos:
            continue
        actual = actual_exit_position[code].to_numpy(dtype=float)
        entered = entry[code].notna().to_numpy()
        lost_before_exit = np.isnan(actual) | (writeoff_pos <= actual)
        loss = (
            entered
            & (entry_dates <= np.datetime64(last.to_datetime64()))
            & natural_in_boundary
            & lost_before_exit
        )
        if loss.any():
            fwd.iloc[np.flatnonzero(loss), column] = -1.0

    fwd.attrs["factor_system_label"] = {
        "id": EXECUTABLE_FORWARD_LABEL,
        "purpose": "candidate_promotion",
        "signal": "T_close",
        "entry": "T_plus_1_executable_open",
        "exit": "T_plus_H_plus_1_or_first_executable_open",
        "price": "adjusted_open",
        "limit_reference": "supplied_execution_date_reference; no implicit fallback",
    }
    return fwd


def split_windows(dates: list[pd.Timestamp]):
    """按时间严格切分 挖掘段(IS) / 验证段(OOS)。"""
    is_start = pd.Timestamp(config.IS_START)
    is_end = pd.Timestamp(config.IS_END)
    oos_end = pd.Timestamp(config.OOS_END)
    is_dates = [d for d in dates if is_start <= d <= is_end]
    oos_dates = [d for d in dates if is_end < d <= oos_end]
    return is_dates, oos_dates


def split_research_windows(dates: list[pd.Timestamp]) -> dict[str, list[pd.Timestamp]]:
    """严格四段研究时间线。

    - discovery：GP/因子方向/策略权重只能使用此段；
    - validation：L2 模型选择段，允许反复使用但不是最终盲测；
    - historical_test：历史确认段。由于旧系统已查看，只能报研究证据；
    - forward_shadow：2026 后新到数据，达到最小日数才可证明真正前向。
    """
    bounds = {
        "discovery": (pd.Timestamp(config.DISCOVERY_START),
                      pd.Timestamp(config.DISCOVERY_END)),
        "validation": (pd.Timestamp(config.VALIDATION_START),
                       pd.Timestamp(config.VALIDATION_END)),
        "historical_test": (pd.Timestamp(config.HISTORICAL_TEST_START),
                            pd.Timestamp(config.HISTORICAL_TEST_END)),
    }
    out = {
        name: [d for d in dates if lo <= d <= hi]
        for name, (lo, hi) in bounds.items()
    }
    forward_start = pd.Timestamp(config.FORWARD_SHADOW_START)
    out["forward_shadow"] = [d for d in dates if d >= forward_start]
    return out
