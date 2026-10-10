"""把已审计 V4 日线安全接到因子面板。

这里不下载、不审计，也不改写历史证据。只有下载清单、质量报告、官方交易
日历和物理文件完全一致时，才把基础 parquet 截止日之后的真实记录接上。
质量报告尚未发布时，调用方的缓存 key 不前进，因此不会读到半更新数据。
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = PROJECT_ROOT / "reports" / "v4_daily_download_manifest.json"
QUALITY_PATH = PROJECT_ROOT / "reports" / "v4_daily_quality_audit.json"
CALENDAR_PATH = PROJECT_ROOT / "reports" / "v4_market_calendar.json"
V4_DAILY_DIR = PROJECT_ROOT / "research" / "data" / "v4_daily"
SECURITY_MASTER_PATH = (
    PROJECT_ROOT / "research" / "data" / "stock_basic_baostock.csv"
)

_SOURCE_COLUMNS = [
    "date", "code", "open", "high", "low", "close", "preclose",
    "volume", "amount", "turn", "tradestatus", "pctChg", "isST",
]
_NUMERIC_COLUMNS = [
    "open", "high", "low", "close", "preclose", "volume", "amount",
    "turn", "pctChg",
]


class V4PanelEvidenceError(RuntimeError):
    """V4 输入不能被当前审计证据完整证明。"""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise V4PanelEvidenceError(f"V4 证据缺失或不可解析: {path}") from exc
    if not isinstance(payload, dict):
        raise V4PanelEvidenceError(f"V4 证据不是对象: {path}")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise V4PanelEvidenceError(f"V4 物理文件不可读: {path}") from exc
    return digest.hexdigest()


def quality_cache_source(path: Path = QUALITY_PATH) -> dict[str, int | str]:
    """返回便宜且稳定的已审计快照键；未发布新 audit 时键不前进。"""
    try:
        stat = path.stat()
    except OSError:
        return {
            "name": "v4_daily_quality_audit.json",
            "st_mtime_ns": 0,
            "st_ctime_ns": 0,
            "st_size": 0,
            "st_ino": 0,
        }
    return {
        "name": "v4_daily_quality_audit.json",
        "st_mtime_ns": int(stat.st_mtime_ns),
        "st_ctime_ns": int(stat.st_ctime_ns),
        "st_size": int(stat.st_size),
        "st_ino": int(stat.st_ino),
    }


def _normalized_ts_code(value: object) -> str:
    text = str(value or "").strip().lower()
    if "." in text:
        market, number = text.split(".", 1)
    else:
        number = text
        market = "sh" if number.startswith("6") else "sz"
    if len(number) != 6 or not number.isdigit() or market not in {"sh", "sz"}:
        raise V4PanelEvidenceError(f"V4 证券代码非法: {value!r}")
    return f"{number}.{market.upper()}"


def _published_evidence(
    *,
    manifest_path: Path = MANIFEST_PATH,
    quality_path: Path = QUALITY_PATH,
    calendar_path: Path = CALENDAR_PATH,
    project_root: Path = PROJECT_ROOT,
    daily_dir: Path = V4_DAILY_DIR,
    security_master_path: Path = SECURITY_MASTER_PATH,
) -> dict[str, Any]:
    manifest = _read_json(manifest_path)
    quality = _read_json(quality_path)
    calendar = _read_json(calendar_path)

    manifest_sha = _sha256(manifest_path)
    audited_manifest_sha = str(
        (quality.get("input_fingerprints") or {}).get(
            "download_manifest_sha256"
        ) or ""
    )
    if audited_manifest_sha != manifest_sha:
        raise V4PanelEvidenceError(
            "V4 下载清单未被当前质量报告审计"
        )

    target = int(manifest.get("target_symbols") or 0)
    manifest_files = manifest.get("files") or []
    quality_files = quality.get("files") or []
    ready = (
        target > 0
        and manifest.get("ready_for_factor_research") is True
        and int(manifest.get("downloaded_symbols") or 0) == target
        and len(manifest_files) == target
        and not (manifest.get("failures") or [])
        and quality.get("ready_for_factor_research") is True
        and quality.get("full_universe_ready") is True
        and quality.get("all_downloaded_files_passed") is True
        and quality.get("market_rule_event_review_ready") is True
        and int(quality.get("target_symbols") or 0) == target
        and int(quality.get("file_count") or 0) == target
        and int(quality.get("passed_files") or 0) == target
        and int(quality.get("failed_files") or 0) == 0
        and int(quality.get("missing_market_dates") or 0) == 0
        and int(quality.get("ordinary_limit_violations") or 0) == 0
        and len(quality_files) == target
    )
    if not ready:
        raise V4PanelEvidenceError("V4 全量质量门禁未通过")

    manifest_query = manifest.get("query") or {}
    quality_query = quality.get("query_window") or {}
    manifest_start = str(manifest_query.get("start_date") or "")
    quality_start = str(quality_query.get("start_date") or "")
    calendar_start = str(calendar.get("start_date") or "")
    manifest_end = str(manifest_query.get("end_date") or "")
    quality_end = str(quality_query.get("end_date") or "")
    calendar_end = str(calendar.get("end_date") or "")
    try:
        query_start = pd.Timestamp(manifest_start).normalize()
        published_end = pd.Timestamp(manifest_end).normalize()
    except (TypeError, ValueError) as exc:
        raise V4PanelEvidenceError("V4 发布查询窗口非法") from exc
    if (
        not manifest_start
        or manifest_start != quality_start
        or not manifest_end
        or manifest_end != quality_end
        or manifest_end != calendar_end
    ):
        raise V4PanelEvidenceError(
            "V4 manifest、quality 与官方交易日历查询窗口不一致"
        )

    calendar_dates = pd.to_datetime(
        calendar.get("trading_dates") or [], errors="coerce"
    )
    if (
        len(calendar_dates) == 0
        or pd.isna(calendar_dates).any()
        or not pd.Index(calendar_dates).is_monotonic_increasing
        or pd.Index(calendar_dates).duplicated().any()
        or not calendar_start
        or pd.Timestamp(calendar_dates[0]).normalize()
        != pd.Timestamp(calendar_start).normalize()
        or pd.Timestamp(calendar_dates[0]).normalize() < query_start
        or pd.Timestamp(calendar_dates[-1]).normalize() != published_end
    ):
        raise V4PanelEvidenceError("V4 官方交易日历不完整或非严格升序")

    # 增量流程先发布 manifest/calendar，随后才发布 audit。证据文件若在 audit
    # 之后又被覆盖，旧 audit 不能给它背书。
    quality_mtime = quality_path.stat().st_mtime_ns
    if (
        manifest_path.stat().st_mtime_ns > quality_mtime
        or calendar_path.stat().st_mtime_ns > quality_mtime
    ):
        raise V4PanelEvidenceError("V4 输入证据在质量审计后发生变化")

    quality_by_path: dict[str, dict[str, Any]] = {}
    for item in quality_files:
        display = str(item.get("file") or "")
        if not display or display in quality_by_path:
            raise V4PanelEvidenceError("V4 质量报告存在空路径或重复路径")
        if item.get("status") != "pass":
            raise V4PanelEvidenceError(f"V4 文件未通过审计: {display}")
        quality_by_path[display] = item

    inventory: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    seen_codes: set[str] = set()
    allowed_root = daily_dir.resolve()
    for item in manifest_files:
        display = str(item.get("path") or "")
        code = _normalized_ts_code(item.get("code") or Path(display).name[:6])
        if not display or display in seen_paths or code in seen_codes:
            raise V4PanelEvidenceError("V4 下载清单存在空值或重复证券")
        physical = (project_root / display).resolve()
        if physical.parent != allowed_root:
            raise V4PanelEvidenceError(f"V4 文件越出授权目录: {display}")
        audited = quality_by_path.get(display)
        if audited is None or _normalized_ts_code(audited.get("code")) != code:
            raise V4PanelEvidenceError(f"V4 清单与质量报告证券不一致: {display}")
        inventory.append({
            "code": code,
            "display_path": display,
            "path": physical,
            "bytes": int(item.get("bytes") or 0),
            "sha256": str(item.get("sha256") or ""),
            "audited_rows": int(audited.get("rows") or 0),
            "audited_start": str(audited.get("start") or ""),
            "audited_end": str(audited.get("end") or ""),
        })
        seen_paths.add(display)
        seen_codes.add(code)

    if len(quality_by_path) != len(inventory):
        raise V4PanelEvidenceError("V4 manifest 与 quality 文件集合不一致")

    membership: dict[str, dict[str, pd.Timestamp | None]] = {}
    manifest_master = manifest.get("universe_security_master") or {}
    manifest_master_sha = str(manifest_master.get("sha256") or "")
    quality_master_sha = str(
        (quality.get("input_fingerprints") or {}).get(
            "security_master_sha256"
        ) or ""
    )
    if manifest_master_sha or quality_master_sha:
        if (
            not manifest_master_sha
            or manifest_master_sha != quality_master_sha
            or not security_master_path.is_file()
            or _sha256(security_master_path) != manifest_master_sha
        ):
            raise V4PanelEvidenceError("V4 证券主档未被当前质量报告绑定")
        try:
            master = pd.read_csv(
                security_master_path,
                usecols=["code", "ipo_date", "out_date", "type"],
                dtype=str,
            ).fillna("")
        except (OSError, ValueError) as exc:
            raise V4PanelEvidenceError("V4 证券主档不可解析") from exc
        master = master[master["type"].eq("1")].copy()
        for row in master.itertuples(index=False):
            code = _normalized_ts_code(row.code)
            if code in membership:
                raise V4PanelEvidenceError(f"V4 证券主档代码重复: {code}")
            ipo_date = pd.to_datetime(row.ipo_date, errors="coerce")
            out_date = pd.to_datetime(row.out_date or pd.NA, errors="coerce")
            if pd.isna(ipo_date):
                raise V4PanelEvidenceError(f"V4 证券主档上市日非法: {code}")
            membership[code] = {
                "ipo_date": pd.Timestamp(ipo_date).normalize(),
                "out_date": (
                    pd.Timestamp(out_date).normalize()
                    if pd.notna(out_date) else None
                ),
            }
        unknown_codes = seen_codes - set(membership)
        if unknown_codes:
            sample = ",".join(sorted(unknown_codes)[:3])
            raise V4PanelEvidenceError(f"V4 证券主档缺少清单代码: {sample}")
    return {
        "published_end": published_end,
        "calendar_dates": pd.DatetimeIndex(calendar_dates).normalize(),
        "manifest_sha256": manifest_sha,
        "quality_sha256": _sha256(quality_path),
        "security_master_sha256": manifest_master_sha,
        "inventory": inventory,
        "codes": seen_codes,
        "membership": membership,
    }


def _board(code: str) -> str:
    if code.endswith(".SH"):
        return "STAR" if code.startswith("68") else "MAIN_SH"
    return "GEM" if code.startswith(("30", "31")) else "MAIN_SZ"


def _source_rows(path: Path) -> pd.DataFrame:
    try:
        frame = pd.read_csv(path, usecols=_SOURCE_COLUMNS, dtype=str)
    except (OSError, ValueError) as exc:
        raise V4PanelEvidenceError(f"V4 日线不可读取: {path}") from exc
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in _NUMERIC_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["tradestatus"] = pd.to_numeric(
        frame["tradestatus"], errors="coerce"
    )
    frame["isST"] = pd.to_numeric(frame["isST"], errors="coerce")
    if frame["date"].isna().any() or frame["date"].duplicated().any():
        raise V4PanelEvidenceError(f"V4 日线日期非法或重复: {path}")
    return frame.sort_values("date", kind="stable").reset_index(drop=True)


def _converted_rows(
    source: pd.DataFrame,
    *,
    code: str,
    previous_adjusted_close: float | None,
    is_delisted: bool,
    source_sha256: str,
    stable_instrument_id: str,
    identity_source_sha256: str,
    identity_provenance: str = "hash_bound_v4_exchange_identity",
) -> pd.DataFrame:
    if source.empty:
        return pd.DataFrame()
    if source["code"].isna().any():
        raise V4PanelEvidenceError(f"V4 文件内证券代码缺失: {code}")
    observed_codes = set(source["code"].dropna().astype(str).map(_normalized_ts_code))
    if observed_codes != {code}:
        raise V4PanelEvidenceError(f"V4 文件内证券代码不一致: {code}")
    states = source[["tradestatus", "isST"]].where(
        source[["tradestatus", "isST"]].isin([0, 1])
    )
    required_numeric = ["open", "high", "low", "close", "preclose"]
    prices = source[required_numeric]
    active = source["tradestatus"].eq(1)
    suspended = states["tradestatus"].eq(0)
    if prices.loc[active].isna().any().any():
        raise V4PanelEvidenceError(f"V4 交易日包含缺失的必需价格: {code}")
    partly_missing_price = prices.isna().any(axis=1) & ~prices.isna().all(axis=1)
    if partly_missing_price.any():
        raise V4PanelEvidenceError(f"V4 日线价格仅部分缺失: {code}")
    # pandas 3 keeps nulls in ``stack``; suspended all-null price rows are a
    # valid raw state and must not be rejected by the finite-value check.
    finite_prices = prices.stack().dropna()
    if (
        (finite_prices.empty and not suspended.all())
        or not np.isfinite(finite_prices).all()
        or (finite_prices <= 0).any()
    ):
        raise V4PanelEvidenceError(f"V4 日线价格非法: {code}")
    active_values = source.loc[active, ["volume", "amount", "turn", "pctChg"]]
    if active_values.isna().any().any() or not np.isfinite(active_values).all().all():
        raise V4PanelEvidenceError(f"V4 交易日包含缺失的成交数值: {code}")
    if (source.loc[active, ["volume", "amount", "turn"]] < 0).any().any():
        raise V4PanelEvidenceError(f"V4 交易日包含负成交数值: {code}")
    calculated_pct = (source["close"] / source["preclose"] - 1.0) * 100.0
    pct_diff = (calculated_pct - source["pctChg"]).abs()
    if (pct_diff.loc[active] > 0.02).any():
        raise V4PanelEvidenceError(f"V4 pctChg 与 close/preclose 不一致: {code}")

    adjusted: list[float] = []
    previous = previous_adjusted_close
    for row in source.itertuples(index=False):
        if pd.isna(row.close) or pd.isna(row.preclose):
            adjusted.append(float("nan"))
            continue
        close = float(row.close)
        preclose = float(row.preclose)
        if not math.isfinite(close) or not math.isfinite(preclose) or min(close, preclose) <= 0:
            raise V4PanelEvidenceError(f"V4 日线价格非法: {code} {row.date}")
        current = close if previous is None else previous * close / preclose
        adjusted.append(current)
        previous = current

    turnover = source["turn"] / 100.0
    with np.errstate(divide="ignore", invalid="ignore"):
        float_mv = source["amount"] / turnover.where(turnover > 0)
    close = source["close"].astype(float)
    adjusted_series = pd.Series(adjusted, index=source.index, dtype=float)
    state_known = states["tradestatus"].notna() & states["isST"].notna()
    provenance = pd.Series(
        "unavailable:v4_company_action_review_pending",
        index=source.index,
        dtype="string",
    )
    provenance.loc[suspended | prices.isna().all(axis=1)] = (
        "unavailable:suspended_missing_price"
    )
    provenance.loc[~state_known] = "unavailable:unknown_trade_or_st_state"
    return pd.DataFrame({
        "ts_code": code,
        "trade_date": source["date"].to_numpy(),
        "open": source["open"].astype(float).to_numpy(),
        "high": source["high"].astype(float).to_numpy(),
        "low": source["low"].astype(float).to_numpy(),
        "close": close.to_numpy(),
        "vol": source["volume"].astype(float).to_numpy(),
        "amount": source["amount"].astype(float).to_numpy(),
        "turnover_rate": turnover.to_numpy(),
        "pct_chg": source["pctChg"].astype(float).to_numpy(),
        "is_st": states["isST"].astype("Int8").array,
        "trade_status": states["tradestatus"].astype("Int8").array,
        "adjust_flag": pd.NA,
        "adj_factor": (adjusted_series / close).to_numpy(),
        "adj_close": adjusted_series.to_numpy(),
        "float_mv": float_mv.to_numpy(),
        "board": _board(code),
        "is_delisted_panel": int(is_delisted),
        "price_source": "baostock_v4_pending_company_action_review",
        "source_snapshot_sha256": source_sha256,
        "is_st_provenance": "baostock_v4_raw_isST",
        "is_st_source_sha256": source_sha256,
        "return_provenance": provenance.array,
        "adjustment_trusted": False,
        "stable_instrument_id": stable_instrument_id,
        "exchange_code": code,
        "identity_provenance": identity_provenance,
        "identity_source_sha256": identity_source_sha256,
    })


def append_verified_v4_rows(
    base: pd.DataFrame,
    *,
    manifest_path: Path = MANIFEST_PATH,
    quality_path: Path = QUALITY_PATH,
    calendar_path: Path = CALENDAR_PATH,
    project_root: Path = PROJECT_ROOT,
    daily_dir: Path = V4_DAILY_DIR,
    security_master_path: Path = SECURITY_MASTER_PATH,
) -> tuple[pd.DataFrame, dict[str, str | int]]:
    """只追加严格晚于基础截止日的真实记录，历史行一字不改。

    当前 V4 清单不能反过来定义历史股票池；否则会让未来清单决定过去成分，
    形成幸存者偏差。V4 中不在基础 PIT 面板的新代码也不能仅凭日线加入。
    """
    if base.empty or not {"ts_code", "trade_date", "close", "adj_close"}.issubset(base):
        raise V4PanelEvidenceError("基础因子面板为空或缺少锚定列")
    evidence = _published_evidence(
        manifest_path=manifest_path,
        quality_path=quality_path,
        calendar_path=calendar_path,
        project_root=project_root,
        daily_dir=daily_dir,
        security_master_path=security_master_path,
    )
    frame = base.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    if frame["trade_date"].isna().any():
        raise V4PanelEvidenceError("基础因子面板包含非法日期")
    base_end = pd.Timestamp(frame["trade_date"].max()).normalize()
    published_end = pd.Timestamp(evidence["published_end"]).normalize()
    anchors = (
        frame.loc[frame["trade_date"].eq(base_end)]
        .drop_duplicates("ts_code", keep="last")
        .set_index("ts_code")
    )
    additions: list[pd.DataFrame] = []
    appended_codes = 0
    appended_rows = 0
    appended_new_codes = 0
    base_codes = set(frame["ts_code"].astype(str))

    for item in evidence["inventory"]:
        path = Path(item["path"])
        if not path.is_file() or path.stat().st_size != int(item["bytes"]):
            raise V4PanelEvidenceError(f"V4 文件大小与清单不一致: {item['display_path']}")
        if _sha256(path) != item["sha256"]:
            raise V4PanelEvidenceError(f"V4 文件哈希与清单不一致: {item['display_path']}")

        audited_end_text = str(item["audited_end"] or "")
        try:
            audited_end = pd.Timestamp(audited_end_text).normalize()
        except (TypeError, ValueError) as exc:
            raise V4PanelEvidenceError(
                f"V4 文件审计截止日非法: {item['display_path']}"
            ) from exc
        code = str(item["code"])
        missing_from_base = code not in base_codes
        # A short file is missing data, not a delisting notice. Only the
        # bound master lifecycle may shorten this instrument's date coverage.
        member = evidence["membership"].get(code)
        out_date = member["out_date"] if member is not None else None
        if out_date is not None and out_date < member["ipo_date"]:
            raise V4PanelEvidenceError(f"V4 证券主档退市日早于上市日: {code}")
        coverage_end = published_end if out_date is None else min(published_end, out_date)
        is_delisted = out_date is not None and out_date <= published_end
        if coverage_end <= base_end:
            continue
        if audited_end <= base_end:
            raise V4PanelEvidenceError(f"V4 缺少已上市证券的必需增量: {code}")

        source = _source_rows(path)
        listing_start = base_end + pd.Timedelta(days=1)
        anchor_adjusted_close: float | None = None
        stable_instrument_id = f"exchange:{code}"
        identity_source_sha256 = str(
            evidence["security_master_sha256"] or item["sha256"]
        )
        if missing_from_base:
            member = evidence["membership"].get(code)
            if member is None or member["ipo_date"] <= base_end:
                raise V4PanelEvidenceError(
                    f"V4 新代码缺少基础 PIT 元数据，拒绝自动纳入: {code}"
                )
            listing_start = member["ipo_date"]
            out_date = member["out_date"]
            if out_date is not None and out_date < listing_start:
                raise V4PanelEvidenceError(f"V4 证券主档退市日早于上市日: {code}")
            if source["date"].min() < listing_start:
                raise V4PanelEvidenceError(f"V4 新代码包含上市日前记录: {code}")
        else:
            anchor = anchors.loc[code] if code in anchors.index else None
            if anchor is None:
                raise V4PanelEvidenceError(f"V4 增量缺少基础截止日锚点: {code}")
            anchor_rows = source[source["date"].eq(base_end)]
            if len(anchor_rows) != 1:
                raise V4PanelEvidenceError(f"V4 增量缺少同源收盘锚点: {code}")
            source_anchor_close = float(anchor_rows.iloc[0]["close"])
            if not math.isclose(
                source_anchor_close,
                float(anchor["close"]),
                rel_tol=1e-6,
                abs_tol=0.011,
            ):
                raise V4PanelEvidenceError(f"V4 与基础面板收盘锚点不一致: {code}")
            anchor_adjusted_close = float(anchor["adj_close"])
            if not math.isfinite(anchor_adjusted_close) or anchor_adjusted_close <= 0:
                raise V4PanelEvidenceError(f"基础面板复权收盘锚点非法: {code}")
            stable_value = anchor.get("stable_instrument_id")
            identity_hash = anchor.get("identity_source_sha256")
            if pd.notna(stable_value) and str(stable_value).strip():
                stable_instrument_id = str(stable_value)
            if pd.notna(identity_hash) and str(identity_hash).strip():
                identity_source_sha256 = str(identity_hash)
        selected = source[
            (source["date"] > base_end) & (source["date"] >= listing_start)
        ].copy()
        expected = evidence["calendar_dates"]
        expected = expected[
            (expected > base_end)
            & (expected >= listing_start)
            & (expected <= coverage_end)
        ]
        observed = pd.DatetimeIndex(selected["date"])
        if not observed.equals(expected):
            raise V4PanelEvidenceError(f"V4 增量交易日不连续: {code}")
        if not selected.empty:
            converted = _converted_rows(
                selected,
                code=code,
                previous_adjusted_close=anchor_adjusted_close,
                is_delisted=is_delisted,
                source_sha256=str(item["sha256"]),
                stable_instrument_id=stable_instrument_id,
                identity_source_sha256=identity_source_sha256,
            )
            additions.append(converted)
            appended_codes += 1
            appended_rows += len(converted)
            appended_new_codes += int(missing_from_base)

    if additions:
        frame = pd.concat([frame, *additions], ignore_index=True)
    if frame.duplicated(["ts_code", "trade_date"]).any():
        raise V4PanelEvidenceError("合并后的因子面板存在证券日期重复")
    frame = frame.sort_values(["ts_code", "trade_date"], kind="stable").reset_index(drop=True)
    actual_end = pd.Timestamp(frame["trade_date"].max()).normalize()
    if published_end > base_end and actual_end != published_end:
        raise V4PanelEvidenceError("已审计 V4 增量没有推进因子面板截止日")
    return frame, {
        "source": "audited_v4_baostock_daily",
        "base_end": base_end.date().isoformat(),
        "published_end": published_end.date().isoformat(),
        "appended_codes": appended_codes,
        "appended_rows": appended_rows,
        "appended_new_codes": appended_new_codes,
        "new_code_policy": "admit_only_hash_bound_security_master_after_ipo",
        "manifest_sha256": str(evidence["manifest_sha256"]),
        "quality_sha256": str(evidence["quality_sha256"]),
    }
