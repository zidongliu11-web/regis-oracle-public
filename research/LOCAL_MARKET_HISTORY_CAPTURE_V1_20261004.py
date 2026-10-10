"""One bounded descriptive pass over the already captured native stock snapshot.

This counts unadjusted close-to-close directions, not investment returns or
historically investable universes. No external source or strategy is invoked.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from mom_trading.local_research_history import (captured_reader, _safe_dataset,
                                              _signature, FIELDS, END)

RECEIPT = "research/LOCAL_MARKET_HISTORY_RECEIPT_20261004.json"
FILENAME = "website_market_history_30.json"
BOARDS = ("main", "chinext", "star")
COUNTERS = ("snapshot_members", "no_current", "invalid_current", "zero_volume",
            "no_previous", "invalid_previous", "up", "down", "unchanged")
LIMITS = [
    "只统计已保存快照中的沪深股票；没有认证历史上市全集、退市覆盖或幸存偏差，不能称全A或当时可投资股票池。",
    "比较未复权原始收盘价方向，不是总回报；分红、拆并股及其他公司行动可能影响涨跌家数。",
    "仅比较相邻交易日且当日有成交量的有效日线；缺日、零成交与无效记录单列，不跨缺口、不补零、不推定可成交。",
    "主板、创业板、科创板是代码分组；科创板仅供研究，不扩张本项目交易范围。没有行业/概念映射，不能替代板块情绪。",
    "当前快照不证明历史逐行当时可见；截至2026-09-30，非实时行情、非交易信号、非策略准入或样本外验证。",
]


def board(code):
    if len(code) != 6 or not code.isascii() or not code.isdigit(): return None
    if code[:3] in {"688", "689"}: return "star"
    if code[:3] in {"300", "301"}: return "chinext"
    if code[:3] in {"000", "001", "002", "003", "600", "601", "603", "605"}: return "main"
    return None


def valid_bar(row):
    import math
    if row is None: return False
    values = {key: float(row[key]) for key in FIELDS[1:]}
    return (all(math.isfinite(v) for v in values.values())
            and 0 < values["low"] <= min(values["open"], values["close"])
            <= max(values["open"], values["close"]) <= values["high"]
            and values["volume"] >= 0 and values["total_turnover"] >= 0)


def empty_days(calendar):
    return [{"date": day, "previous_date": calendar[i],
             "boards": {b: {key: 0 for key in COUNTERS} for b in BOARDS}}
            for i, day in enumerate(calendar[1:])]


def add_instrument(days, group, records):
    for day in days:
        counts = day["boards"][group]; counts["snapshot_members"] += 1
        current, previous = records.get(day["date"]), records.get(day["previous_date"])
        if current is None: reason = "no_current"
        elif not valid_bar(current): reason = "invalid_current"
        elif float(current["volume"]) == 0: reason = "zero_volume"
        elif previous is None: reason = "no_previous"
        elif not valid_bar(previous): reason = "invalid_previous"
        else:
            close, prior = float(current["close"]), float(previous["close"])
            reason = "up" if close > prior else "down" if close < prior else "unchanged"
        counts[reason] += 1


def validate_summary(data):
    if (data.get("schema") != "regis.local_market_history.v1" or data.get("status") != "available"
            or data.get("research_only") is not True or data.get("live_market_data") is not False
            or data.get("model_admitted") is not False or data.get("full_A_coverage_certified") is not False
            or data.get("historical_feature_PIT_certified") is not False
            or data.get("conditions_and_limits") != LIMITS
            or type(data.get("included_datasets")) is not int or not 0 < data["included_datasets"] <= 7000):
        raise ValueError("summary_scope_invalid")
    days = data.get("days")
    if not isinstance(days, list) or not 1 <= len(days) <= 30: raise ValueError("summary_days_invalid")
    prior = None
    for day in days:
        if (type(day["date"]) is not int or type(day["previous_date"]) is not int
                or not 20130101 <= day["previous_date"] < day["date"] <= 20260930
                or prior is not None and day["previous_date"] != prior):
            raise ValueError("summary_calendar_invalid")
        datetime.strptime(str(day["date"]), "%Y%m%d");datetime.strptime(str(day["previous_date"]), "%Y%m%d")
        prior = day["date"]
        if set(day["boards"]) != set(BOARDS): raise ValueError("summary_board_invalid")
        totals = {key: 0 for key in COUNTERS}
        for c in day["boards"].values():
            if set(c) != set(COUNTERS) or any(type(v) is not int or v < 0 for v in c.values()):
                raise ValueError("summary_counter_invalid")
            if c["snapshot_members"] != sum(c[k] for k in COUNTERS[1:]): raise ValueError("summary_count_identity")
            for k in COUNTERS: totals[k] += c[k]
        if totals != day["total"] or totals["snapshot_members"] != data["included_datasets"]:
            raise ValueError("summary_total_invalid")
    if data["start_date"] != days[0]["date"] or data["end_date"] != days[-1]["date"]:
        raise ValueError("summary_window_invalid")


def build(reader, *, sessions=30, max_seconds=120):
    from research.inspect_short_rq_full import validate_bar_dtype
    if type(sessions) is not int or not 1 <= sessions <= 30: raise ValueError("invalid_sessions")
    started = time.monotonic(); np = reader.np
    cutoff = int(END.replace("-", ""))
    calendar = sorted(d for d in reader.calendar if 20130101 <= d <= cutoff)
    if len(calendar) < sessions + 1: raise ValueError("calendar_too_short")
    calendar = calendar[-sessions-1:]; days = empty_days(calendar); included = ignored = 0
    with reader.h5py.File(reader.path, "r", rdcc_nbytes=1048576) as file:
        if len(file) > 7000: raise ValueError("dataset_budget_exceeded")
        for key in file:
            if time.monotonic() - started > max_seconds: raise TimeoutError("native_budget_exceeded")
            code, separator, suffix = key.partition("."); group = board(code)
            if not group or separator != "." or suffix != ("XSHG" if code.startswith("6") else "XSHE"):
                ignored += 1; continue
            ds = _safe_dataset(file, key, reader.h5py, np);validate_bar_dtype(ds.dtype, FIELDS)
            times = ds.fields("datetime")[:]
            if times.dtype.kind not in "iu": raise ValueError("source_date_type")
            long = times > 100000000
            if not np.all((~long) | (times % 1000000 == 0)): raise ValueError("source_time_component")
            dates = np.where(long, times // 1000000, times).astype(np.int64)
            if np.any(np.diff(dates) <= 0): raise ValueError("source_date_order")
            lo, hi = np.searchsorted(dates, [calendar[0], calendar[-1]], side="left")
            hi += int(hi < len(dates) and dates[hi] == calendar[-1])
            records = {}
            if hi > lo:
                selected = ds.fields(FIELDS)[int(lo):int(hi)]
                for day, row in zip(dates[lo:hi], selected):
                    if int(day) not in reader.calendar: raise ValueError("source_off_calendar")
                    records[int(day)] = row
            add_instrument(days, group, records); included += 1
    if time.monotonic() - started > max_seconds: raise TimeoutError("native_budget_exceeded")
    if _signature(reader.path) != reader.signature or _signature(reader.calendar_path) != reader.calendar_signature:
        raise ValueError("source_changed_during_aggregation")
    for day in days: day["total"] = {k: sum(c[k] for c in day["boards"].values()) for k in COUNTERS}
    data = {"schema": "regis.local_market_history.v1", "status": "available", "source_sha256": reader.sha,
            "source_captured_at": reader.captured_at, "created_utc": datetime.now(timezone.utc).isoformat(),
            "start_date": days[0]["date"], "end_date": days[-1]["date"], "days": days,
            "included_datasets": included, "ignored_datasets": ignored, "conditions_and_limits": LIMITS,
            "research_only": True, "live_market_data": False, "model_admitted": False,
            "full_A_coverage_certified": False, "historical_feature_PIT_certified": False}
    validate_summary(data)
    return data, round(time.monotonic() - started, 3)


def capture(root):
    root = Path(root).resolve(); reader = captured_reader(root)
    output = reader.path.parent / FILENAME
    if output.exists() or (root / RECEIPT).exists(): raise FileExistsError("existing_capture_not_overwritten")
    data, elapsed = build(reader)
    raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
    sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
    receipt = {"schema": "regis.local_market_history_receipt.v1", "state": "captured", "native_path": str(output),
               "summary_sha256": hashlib.sha256(raw).hexdigest(), "summary_bytes": len(raw),
               "source_sha256": reader.sha, "calendar_sha256": sha(reader.calendar_path),
               "generator_sha256": sha(__file__), "source_reader_sha256": sha(root / "mom_trading/local_research_history.py"),
               "start_date": data["start_date"], "end_date": data["end_date"], "sessions": len(data["days"]),
               "included_datasets": data["included_datasets"], "ignored_datasets": data["ignored_datasets"],
               "elapsed_seconds": elapsed, "native_passes": 1, "raw_to_AI_Git_cloud": False,
               "new_economic_runs": 0, "full_A_PIT_model_admitted": False, "created_utc": data["created_utc"]}
    with output.open("xb") as f: f.write(raw)
    with (root / RECEIPT).open("x", encoding="utf-8") as f: json.dump(receipt, f, ensure_ascii=False, indent=2);f.write("\n")
    return receipt


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser();parser.add_argument("--root", type=Path, required=True)
    print(json.dumps(capture(parser.parse_args().root), ensure_ascii=False))
