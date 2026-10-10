"""Read a content-bound daily publication for historical risk, without writes/API calls.

Individual securities may pass while the factor universe is incomplete. An old
audit without effective-file fingerprints is not enough: the cloud worker must
first re-audit its explicit publication. Historical prices never authorize orders.
"""
from __future__ import annotations

import csv
import functools
import gzip
import hashlib
import io
import json
import math
import zlib
from datetime import date, datetime, timezone
from pathlib import Path


class HistoryUnavailable(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise HistoryUnavailable(reason)


def _day(value):
    _require(isinstance(value, str) and date.fromisoformat(value).isoformat() == value,
             "日线日期不规范")
    return value


def _stamp(path):
    _require(not path.is_symlink(), "不接受单个输入文件的符号链接")
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _bytes(path, stamp):
    # A stat tuple is a change hint, not a content identity. Rapid same-size
    # replacements can retain the same reported timestamp resolution.
    raw = path.read_bytes()
    _require(_stamp(path) == stamp, "读取期间输入发生变化")
    return raw, hashlib.sha256(raw).hexdigest()


def _document(path, stamp):
    raw, identity = _bytes(path, stamp)
    result = json.loads(raw)
    _require(isinstance(result, dict), "发布元数据不是对象")
    return result, identity


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@functools.lru_cache(maxsize=64)
def _rows(path, stamp, identity, code, start, end):
    raw, actual = _bytes(path, stamp)
    _require(actual == identity, "日线文件内容与发布清单不一致")
    reader = csv.DictReader(io.StringIO(gzip.decompress(raw).decode("utf-8-sig")))
    required = {"date", "code", "open", "high", "low", "close", "preclose", "volume",
                "amount", "adjustflag", "tradestatus", "pctChg", "isST"}
    _require(required <= set(reader.fieldnames or []), "日线缺少价格、状态或收益身份字段")
    _require(len(reader.fieldnames) == len(set(reader.fieldnames)), "日线存在重复列")
    symbol = ("sh." if code.startswith("6") else "sz.") + code
    result, previous_day, previous_close = [], "", None
    for row in reader:
        _require(None not in row and all(value is not None for value in row.values()), "日线行字段数量不一致")
        day = _day(row["date"])
        _require(start <= day <= end and day > previous_day, "日线越界、乱序或日期重复")
        _require(row["code"] == symbol and row["adjustflag"] == "3", "证券或不复权口径不一致")
        _require(row["tradestatus"] in {"0", "1"} and row["isST"] in {"0", "1"}, "交易状态不合法")
        previous_day = day
        if row["tradestatus"] == "0":
            # Suspended rows can legitimately lack OHLC. Preserve the break,
            # never invent a zero return or bridge missing prices in ATR.
            result.append({"date": day, "tradable": False})
            previous_close = None
            continue
        values = {key: float(row[key]) for key in (
            "open", "high", "low", "close", "preclose", "volume", "amount", "pctChg")}
        _require(all(math.isfinite(value) for value in values.values()), "日线有非有限数值")
        o, h, l, c, p = (values[key] for key in ("open", "high", "low", "close", "preclose"))
        _require(min(o, h, l, c, p) > 0 and l <= min(o, c) <= max(o, c) <= h,
                 "日线OHLC几何无效")
        _require(values["volume"] >= 0 and values["amount"] >= 0, "日线成交量或额为负")
        _require(values["pctChg"] > -100 and abs((c / p - 1) * 100 - values["pctChg"]) <= .02 + 1e-10,
                 "涨跌幅与前收盘价格身份不一致")
        result.append({"date": day, **values, "tradable": True,
                       "price_basis_break": previous_close is not None and not math.isclose(p, previous_close, abs_tol=.00011, rel_tol=0),
                       "price_basis": "audited_raw"})
        previous_close = c
    _require(bool(result), "日线为空")
    return tuple(result)


def _load(root: Path, code: str, cutoff: date, *, now=None):
    """Return checked rows and provenance, or a safe human-readable failure.

    Only the known daily directory is trusted; the directory may be a deployed
    runtime mount/symlink, but individual files cannot escape it.
    """
    now = now or datetime.now(timezone.utc)
    _require(isinstance(code, str) and len(code) == 6 and code.isascii() and code.isdigit(), "证券代码无效")
    _require(type(cutoff) is date and now.tzinfo is not None, "已完成交易日日历不可用")
    observed = {}

    def document(relative):
        path = root / relative
        stamp = observed.setdefault(path, _stamp(path))
        return _document(path, stamp)

    def identity(relative):
        path = root / relative
        stamp = observed.setdefault(path, _stamp(path))
        return _bytes(path, stamp)[1]

    manifest, manifest_sha = document("reports/v4_daily_download_manifest.json")
    audit, audit_sha = document("reports/v4_daily_quality_audit.json")
    calendar, calendar_sha = document("reports/v4_market_calendar.json")
    master_sha = identity("research/data/stock_basic_baostock.csv")
    query = manifest["query"]
    start, end = _day(query["start_date"]), _day(query["end_date"])
    _require(start <= cutoff.isoformat() <= end, "发布日线未覆盖所需已完成交易日")
    _require(str(manifest.get("source", "")).startswith("BaoStock ") and query.get("frequency") == "d"
             and query.get("adjustflag") == "3 (不复权)", "日线来源或复权协议不一致")
    _require(manifest["universe_security_master"]["sha256"] == master_sha, "证券主表身份已变化")
    bindings = audit["input_fingerprints"]
    _require(bindings.get("download_manifest_sha256") == manifest_sha
             and bindings.get("security_master_sha256") == master_sha
             and bindings.get("market_calendar_sha256") == calendar_sha, "审计未绑定当前清单、主表和交易日历")
    _require(all(audit["query_window"].get(k) == query[k] for k in ("start_date", "end_date")), "审计窗口不一致")
    generated = datetime.fromisoformat(audit["generated_at"])
    _require(generated.tzinfo is not None and generated <= now, "审计时间缺失时区或晚于当前时间")
    dates = calendar["trading_dates"]
    _require(calendar.get("source") == "BaoStock query_trade_dates" and isinstance(dates, list)
             and dates and dates == sorted(set(dates)) and all(_day(d) for d in dates)
             and calendar.get("start_date") == dates[0] and calendar.get("end_date") == dates[-1]
             and cutoff.isoformat() in dates and end in dates, "源交易日历无效或覆盖不足")
    entries = manifest["files"]
    _require(isinstance(entries, list) and entries, "发布文件清单缺失")
    indexed = {entry["code"]: entry for entry in entries}
    _require(len(indexed) == len(entries) and code in indexed, "证券缺失或发布清单重复")
    revision_path = Path("reports/cloud_daily_revisions") / end / "index.json"
    revision_sha = identity(revision_path) if (root / revision_path).exists() else None
    _require(bindings.get("revision_index_sha256") == revision_sha, "日线修订尚未经过当前审计")
    if revision_sha:
        from .cloud_daily_revisions import load_files
        # The shared resolver also checks every immutable repair receipt and
        # original/replacement hash. Do not reconstruct a guessed file name.
        indexed = load_files(root, indexed, end, now=now)
    effective = [indexed[entry["code"]] for entry in entries]
    _require(bindings.get("effective_files_sha256") == _digest(effective), "审计缺少或不匹配日线内容身份；需后台重审")
    special = Path("research/data/special_price_limit_events.csv")
    _require(bindings.get("special_events_sha256") == (identity(special) if (root / special).exists() else None),
             "特殊交易制度来源已变化")
    from .cloud_price_limit_events import load_events
    _require(bindings.get("cloud_price_events_sha256") == load_events(root)[1], "制度公告审计身份已变化")
    entry = indexed[code]
    relative = Path(entry["path"])
    path = root / relative
    _require(not relative.is_absolute() and ".." not in relative.parts
             and path.parent.resolve() == (root / "research/data/v4_daily").resolve()
             and path.name.startswith(code + ".") and path.name.endswith(".gz"), "日线路径不在受信运行目录")
    stamp = observed.setdefault(path, _stamp(path))
    _require(entry.get("bytes") == stamp[2], "日线文件大小不匹配")
    # Decoded rows are cached by the frozen publication identity only after
    # current on-disk bytes have been compared with that identity.
    _require(_bytes(path, stamp)[1] == entry["sha256"], "日线文件内容与发布清单不一致")
    symbol = ("sh." if code.startswith("6") else "sz.") + code
    files = [item for item in audit["files"] if item.get("code") == symbol and item.get("file") == entry["path"]]
    _require(len(files) == 1, "缺少唯一的个股审计记录")
    checked = files[0]
    _require(checked.get("status") == "pass" and checked.get("errors") == []
             and checked.get("ordinary_limit_violations") == 0 and checked.get("missing_market_date_count") == 0,
             "个股日线审计尚未通过")
    pct = checked["pctchg_preclose_identity"]
    _require(type(checked.get("active_rows")) is int and checked["active_rows"] > 0
             and all(pct.get(k) == checked["active_rows"] for k in ("expected_evaluable_rows", "evaluable_rows", "passed_rows"))
             and all(pct.get(k) == 0 for k in ("missing_required_input_rows", "mismatch_rows_above_0_02pp", "unknown_state_rows")),
             "个股收益身份审计不完整")
    rows = _rows(path, stamp, entry["sha256"], code, start, end)
    _require(len(rows) == checked.get("rows")
             and rows[0]["date"] == checked.get("start")
             and rows[-1]["date"] == checked.get("end")
             and sum(row["tradable"] for row in rows) == checked["active_rows"], "日线内容与个股审计统计不一致")
    # Older manifest entries omitted descriptive ranges. The required raw
    # hash and the auditor's actual row/range checks remain authoritative.
    _require(all(entry.get(k, value) == value for k, value in
                 (("rows", len(rows)), ("start", rows[0]["date"]), ("end", rows[-1]["date"]))),
             "发布清单描述与日线内容不一致")
    _require([row["date"] for row in rows] == [d for d in dates if rows[0]["date"] <= d <= rows[-1]["date"]],
             "日线与源交易日历不完整对齐")
    selected = [dict(row) for row in rows if row["date"] <= cutoff.isoformat()]
    _require(selected and selected[-1]["date"] == cutoff.isoformat(), "个股日线未到所需截止日")
    _require(all(_stamp(p) == s for p, s in observed.items()), "发布在读取期间变化；等待一致批次")
    return selected, {"status": "complete", "source": "audited_raw_v4_daily",
        "data_as_of": selected[-1]["date"], "raw_sha256": entry["sha256"],
        "audit_sha256": audit_sha, "manifest_sha256": manifest_sha,
        "historical_risk_eligible": True, "execution_price_eligible": False,
        "return_basis": "audited_pctChg_preclose_price_reference_not_total_return",
        "note": "文件内容、个股审计与源交易日历一致；仅供历史风险统计，非实时执行价格或策略准入。"}


def load(root: Path, code: str, cutoff: date, *, now=None):
    try:
        return _load(root, code, cutoff, now=now)
    except (FileNotFoundError, HistoryUnavailable):
        raise
    except (OSError, EOFError, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, zlib.error) as exc:
        raise HistoryUnavailable("日线发布或审计输入格式无法核验") from exc
