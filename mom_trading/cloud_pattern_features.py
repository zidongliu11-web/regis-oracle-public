"""Cloud price-feature observations; no historical backfill or trade admission."""
from __future__ import annotations

from bisect import bisect_right
from collections import deque
import csv
from datetime import datetime, timedelta, timezone
import fcntl
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
from statistics import median
from uuid import uuid4

import pandas as pd

from .cloud_pattern_inputs import SCHEMA as INPUT_SCHEMA, _source_universe, assess_receipt, sha, valid_day
from .cloud_industry_daily import IMPLEMENTATION_SHA256 as INDUSTRY_SHA256, load_entries as load_daily_industries
from .cloud_research_worker import ROOT, publish, read_json
from .emotion_pattern_research import ENGINE_SHA256, FACTORS, PLAYBOOKS, compute_features, research_catalog


SCHEMA = "cloud_pattern_features.v1"
SHANGHAI = timezone(timedelta(hours=8))
PRICE_ABS_TOLERANCE = 0.0001  # Provider raw prices are published to four decimals.
PRICE_REL_TOLERANCE = 0.000001
BOUNDARY = {"production_eligible": False, "generates_alert": False,
            "return_labels_read": False, "account_returns_computed": False}
IMPLEMENTATION_SHA256 = sha(Path(__file__))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def stock_observation(raw_path, adjustment, industry, *, calendar, target, code, as_of):
    """62 raw sessions establish 61 checked rows for the existing fixed rules.

    Price factors can be described without industry membership. That does not
    authorize sector ranking, emotion routing, an entry candidate, or a fill.
    """
    observed = datetime.fromisoformat(as_of)
    if observed.tzinfo is None or not valid_day(target):
        raise ValueError("observation_timestamp_invalid")
    close_at = datetime.fromisoformat(target+"T15:00:00+08:00")
    if observed < close_at:
        raise ValueError("daily_bar_not_complete")
    if (not calendar or any(not valid_day(d) for d in calendar)
            or calendar != sorted(set(calendar)) or calendar[-1] != target):
        raise ValueError("calendar_identity_invalid")
    expected = calendar[-62:]
    if len(expected) != 62:
        raise ValueError("calendar_history_insufficient")
    request = adjustment.get("request") or {}
    if (adjustment.get("schema") != INPUT_SCHEMA or assess_receipt(adjustment) != "captured_not_validated"
            or request.get("endpoint") != "query_adjust_factor" or request.get("code") != code
            or request.get("end_date") != target or request.get("start_date", "z") > expected[0]):
        raise ValueError("adjustment_receipt_not_verified")
    completed = datetime.fromisoformat(adjustment["completed_at"])
    started = datetime.fromisoformat(adjustment["observed_at"])
    if completed.tzinfo is None or started.tzinfo is None or not started <= completed <= observed:
        raise ValueError("adjustment_not_yet_observed")
    events = sorted((dict(zip(adjustment["fields"], row)) for row in adjustment["rows"]), key=lambda row: row["dividOperateDate"])
    event_dates = [row["dividOperateDate"] for row in events]
    window, previous_day = deque(maxlen=62), ""
    with gzip.open(raw_path, "rt", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            day = row.get("date")
            if not valid_day(day) or day <= previous_day or day > target:
                raise ValueError("daily_date_identity_invalid")
            if row.get("code") != code or row.get("adjustflag") != "3":
                raise ValueError("daily_security_or_price_basis_mismatch")
            previous_day = day
            window.append(row)
    if [row["date"] for row in window] != expected:
        raise ValueError("daily_window_missing_sessions")
    rows, previous = [], None
    event_transitions = 0
    for row in window:
        if row["tradestatus"] not in {"0", "1"} or row["isST"] not in {"0", "1"}:
            raise ValueError("daily_security_status_invalid")
        if row["tradestatus"] == "0":
            raise ValueError("history_not_continuously_tradable")
        values = {name: float(row[name]) for name in ("open", "high", "low", "close", "preclose", "amount", "volume", "pctChg")}
        if (not all(math.isfinite(v) for v in values.values())
                or min(v for k, v in values.items() if k != "pctChg") <= 0
                or values["high"] < max(values["open"], values["close"], values["low"])
                or values["low"] > min(values["open"], values["close"], values["high"])):
            raise ValueError("raw_price_or_activity_invalid")
        if abs((values["close"] / values["preclose"] - 1) * 100 - values["pctChg"]) > 0.02:
            raise ValueError("reported_change_mismatch")
        index = bisect_right(event_dates, row["date"]) - 1
        if index < 0:
            raise ValueError("adjustment_prefix_uncovered")
        factor = float(events[index]["backAdjustFactor"])
        if previous is not None:
            expected_preclose = previous["close"] * previous["adj_factor"] / factor
            if not math.isclose(values["preclose"], expected_preclose, rel_tol=PRICE_REL_TOLERANCE, abs_tol=PRICE_ABS_TOLERANCE):
                raise ValueError("adjustment_price_chain_mismatch")
            event_transitions += factor != previous["adj_factor"]
        previous = {**values, "adj_factor": factor}
        rows.append({**values, "adj_factor": factor, "code": code.split(".")[1], "trade_date": row["date"],
                     "session_index": len(calendar)-62+len(rows), "trade_status": 1,
                     "is_st": row["isST"] == "1", "adjustment_trusted": True, "industry_code": None})

    industry_code, industry_reason = None, "same_day_industry_not_observed"
    if industry is not None and industry.get("schema") == INPUT_SCHEMA and assess_receipt(industry) == "captured_not_validated":
        industry_request = industry.get("request") or {}
        stamp = datetime.fromisoformat(industry["observed_at"])
        finish = datetime.fromisoformat(industry["completed_at"])
        if (industry_request.get("endpoint") == "query_stock_industry" and industry_request.get("code") == code
                and stamp.tzinfo is not None and finish.tzinfo is not None
                and stamp <= finish <= observed and stamp.astimezone(SHANGHAI).date().isoformat() == target):
            member = dict(zip(industry["fields"], industry["rows"][0]))
            if member["industry"].strip() and member["industryClassification"].strip():
                industry_code = member["industryClassification"]+":"+member["industry"]
                industry_reason = None
    rows[-1]["industry_code"] = industry_code
    # Keep the original formula/shape implementation immutable. One stock's
    # local industry median is not a cross-sectional factor and is discarded.
    features = compute_features(pd.DataFrame(rows[1:]), as_of=as_of)
    last = features.iloc[-1]
    checked = rows[1:]
    ranges = [(r["high"]-r["low"])*r["adj_factor"] for r in checked]
    mean = lambda values: sum(values)/len(values)
    finite_or_none = lambda value: float(value) if pd.notna(value) and math.isfinite(float(value)) else None
    # These four descriptive values do not need an industry. Their equivalence
    # to the original eligible-row formulas is covered by a regression test.
    price_factors = {
        "range_contraction_5_20": -mean(ranges[-5:])/mean(ranges[-20:]) if mean(ranges[-20:]) else None,
        "breakout_distance_20": finite_or_none(last.a_close/last.high20_before-1),
        "close_acceptance": finite_or_none((last.a_close-last.a_low)/last.day_range) if last.day_range else None,
        "pullback_volume_3_20": -mean([r["amount"] for r in checked[-4:-1]])/mean([r["amount"] for r in checked[-24:-4]]),
        "industry_excess_20": None,
    }
    return {"schema": SCHEMA, "code": code, "data_as_of": target, "observed_at": as_of,
            "status": "price_features_computed", "checked_price_rows": 61, "raw_window_rows": 62,
            "adjustment_event_transitions_checked": event_transitions,
            "factor_values": price_factors, "industry_code": industry_code, "industry_gap": industry_reason,
            "ret20": finite_or_none(last.ret20), "above_ma20": bool(last.a_close > last.ma20),
            "is_st": bool(last.is_st), "shape_conditions": {p["id"]: bool(last[p["id"]]) for p in PLAYBOOKS},
            "sector_confirmed": False, "emotion_route_evaluated": False,
            "scope": "单股价格特征观察；未进行完整横截面行业排序、情绪路由或收益验证。",
            **BOUNDARY}


def cross_section(observations, *, target_count):
    """Require known coverage before using the original industry median rule."""
    if len({row["code"] for row in observations}) != len(observations):
        raise ValueError("cross_section_duplicate_security")
    if len({row["data_as_of"] for row in observations}) > 1:
        raise ValueError("cross_section_mixed_dates")
    if len(observations) != target_count:
        return {"status": "waiting_price_coverage", "rows": [], **BOUNDARY}
    eligible, excluded = [], 0
    for row in observations:
        if row["status"] == "ineligible_history" or row.get("is_st") is True:
            excluded += 1
            continue
        if row["status"] != "price_features_computed":
            return {"status": "waiting_price_checks", "rows": [], **BOUNDARY}
        if not row.get("industry_code"):
            return {"status": "waiting_same_day_industry", "rows": [], **BOUNDARY}
        eligible.append(row)
    groups = {}
    for row in eligible:
        groups.setdefault(row["industry_code"], []).append(row)
    sectors, result = [], []
    for name, group in groups.items():
        typical = median(row["ret20"] for row in group)
        breadth = sum(row["above_ma20"] for row in group)/len(group)
        if len(group) >= 12 and typical > 0 and breadth >= 0.6:
            sectors.append({"industry_code": name, "median_return20": typical, "breadth": breadth, "count": len(group)})
        for row in group:
            result.append({"code": row["code"], "industry_code": name,
                           "factor_values": {**row["factor_values"], "industry_excess_20": row["ret20"]-typical}})
    sectors.sort(key=lambda row: (-row["median_return20"], row["industry_code"]))
    return {"status": "complete", "rows": sorted(result, key=lambda row: row["code"]),
            "research_sectors": sectors[:2], "excluded_history_or_st": excluded,
            "intraday_sector_confirmation": False, "emotion_route_evaluated": False, **BOUNDARY}


def _bound(root, entry, directory):
    path = root / entry["path"]
    if (path.is_symlink() or path.parent.resolve() != directory.resolve()
            or not path.is_file() or sha(path) != entry.get("sha256")):
        raise ValueError("source_file_identity_mismatch")
    return path


def run(*, root=ROOT, batch_limit=20, now=None):
    if os.environ.get("REGIS_EXECUTION_ROLE") != "cloud":
        return {"status": "local_unchanged", "summary": "云端价格特征任务不改动开发电脑研究数据。"}
    if type(batch_limit) is not int or not 1 <= batch_limit <= 100:
        raise ValueError("feature_batch_limit_invalid")
    if shutil.disk_usage(root).free < 2*1024**3:
        return {"status": "waiting_storage", "summary": "可用磁盘不足2GB，保留既有研究观察，不新增计算文件。", **BOUNDARY}
    now = now or (lambda: datetime.now(timezone.utc))
    try:
        target, master_sha, all_codes = _source_universe(root)
        daily_path = root / "reports/cloud_market_data.json"
        daily = read_json(daily_path)
        calendar = daily.get("calendar_dates") or []
        if not calendar or any(not valid_day(day) for day in calendar) or calendar != sorted(set(calendar)) or calendar[-1] != target:
            raise ValueError("calendar_identity_invalid")
        if len(calendar) < 62:
            raise ValueError("calendar_history_insufficient")
        raw_manifest_path = root / "reports/v4_daily_download_manifest.json"
        raw_manifest = read_json(raw_manifest_path)
        if (not str(raw_manifest.get("source", "")).startswith("BaoStock ")
                or (raw_manifest.get("query") or {}).get("frequency") != "d"
                or (raw_manifest.get("query") or {}).get("end_date") != target
                or (raw_manifest.get("universe_security_master") or {}).get("sha256") != master_sha):
            raise ValueError("raw_manifest_identity_mismatch")
        input_dir = root / "reports/cloud_pattern_inputs" / target
        inputs = read_json(input_dir / "manifest.json")
        if (inputs.get("schema") != INPUT_SCHEMA or inputs.get("master_sha256") != master_sha
                or inputs.get("target_date") != target):
            raise ValueError("adjustment_batch_identity_missing")
        raw_files = {entry["code"]: entry for entry in raw_manifest.get("files", [])}
        if len(raw_files) != len(raw_manifest.get("files", [])):
            raise ValueError("raw_manifest_duplicates")
        from .cloud_daily_revisions import load_files
        raw_files = load_files(root, raw_files, target, now=now())
        with (root / "research/data/stock_basic_baostock.csv").open(encoding="utf-8-sig", newline="") as stream:
            codes = sorted(row["code"] for row in csv.DictReader(stream) if row["code"] in all_codes
                           and (not row["out_date"] or row["out_date"] >= target))
        daily_industries = load_daily_industries(root, target, now=now())
    except (OSError, ValueError, KeyError) as exc:
        return {"status": "waiting_inputs", "reason": str(exc), "summary": "价格特征所需日线、日历或复权批次身份尚未齐备；没有开始计算。", **BOUNDARY}
    directory = root / "reports/cloud_pattern_features" / target
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "owned_elsewhere", "summary": "同日价格特征已有执行进程，不重复启动。", **BOUNDARY}
        state_path = directory / "manifest.json"
        state = read_json(state_path)
        if state_path.exists() and (state.get("schema") != SCHEMA or state.get("master_sha256") != master_sha):
            return {"status": "waiting_identity", "summary": "已存价格特征批次身份不匹配，保留原件。", **BOUNDARY}
        state = state or {"schema": SCHEMA, "master_sha256": master_sha, "target_date": target, "items": {}}
        output_dir = directory / "observations"
        output_dir.mkdir(exist_ok=True)
        source_contract = {"feature_implementation_sha256": IMPLEMENTATION_SHA256,
                           "rule_sha256": research_catalog()["rule_sha256"],
                           "rule_implementation_sha256": ENGINE_SHA256,
                           "receipt_validation_sha256": sha(Path(__file__).with_name("cloud_pattern_inputs.py")),
                           "daily_industry_validation_sha256": INDUSTRY_SHA256,
                           "daily_revision_validation_sha256": sha(Path(__file__).with_name("cloud_daily_revisions.py")),
                           "calendar_sha256": digest(calendar), "master_sha256": master_sha}
        current, pending, industry_directories = [], [], {}
        for code in codes:
            raw = raw_files.get(code.split(".")[1])
            adjust = (inputs.get("adjustments") or {}).get(code)
            industry = daily_industries.get(code) or (inputs.get("industries") or {}).get(code)
            industry_directories[code] = (root / "reports/cloud_industry_daily" / target / "receipts"
                                          if code in daily_industries else input_dir / "receipts")
            if not raw or not adjust:
                continue
            refs = {"raw": raw, "adjustment": adjust, "industry": industry}
            identity = digest({"code": code, "target": target, "source_contract": source_contract,
                               "source_sha256": {k: v["sha256"] if v else None for k, v in refs.items()}})
            previous = state["items"].get(code)
            if previous and previous.get("input_identity") == identity:
                path = _bound(root, previous, output_dir)
                # Check physical source bytes as well as their producer's
                # manifest; a stale manifest cannot bless a changed raw file.
                _bound(root, raw, root / "research/data/v4_daily")
                _bound(root, adjust, input_dir / "receipts")
                if industry:
                    _bound(root, industry, industry_directories[code])
                cached = read_json(path)
                if cached.get("input_identity") != identity or cached.get("code") != code or cached.get("data_as_of") != target:
                    raise ValueError("cached_observation_identity_mismatch")
                current.append(cached)
            else:
                pending.append((code, refs, identity))
        for code, refs, identity in pending[:batch_limit]:
            raw_path = _bound(root, refs["raw"], root / "research/data/v4_daily")
            adjustment = read_json(_bound(root, refs["adjustment"], input_dir / "receipts"))
            industry = read_json(_bound(root, refs["industry"], industry_directories[code])) if refs["industry"] else None
            try:
                if all_codes[code] > calendar[-62]:
                    raise ValueError("recent_listing_insufficient_history")
                observation = stock_observation(raw_path, adjustment, industry, calendar=calendar, target=target, code=code, as_of=now().isoformat())
            except ValueError as exc:
                reason = str(exc)
                status = "ineligible_history" if reason in {"history_not_continuously_tradable", "recent_listing_insufficient_history"} else "needs_review"
                observation = {"schema": SCHEMA, "code": code, "data_as_of": target, "observed_at": now().isoformat(),
                               "status": status, "reason": reason, **BOUNDARY}
            observation.update(input_identity=identity, source_contract=source_contract,
                               input_references=refs, historical_input_reconstruction=True, forward_evidence=False)
            # Recheck after reading before publishing any trusted price result.
            _bound(root, refs["raw"], root / "research/data/v4_daily")
            _bound(root, refs["adjustment"], input_dir / "receipts")
            if refs["industry"]:
                _bound(root, refs["industry"], industry_directories[code])
            # A crash between observation and manifest publication may leave
            # an orphan. Keep it; a retry writes another immutable observation.
            path = output_dir / (code+"."+identity+"."+uuid4().hex+".json")
            if path.exists():
                raise ValueError("unreferenced_observation_already_exists")
            publish(path, observation)
            state["items"][code] = {"input_identity": identity, "path": str(path.relative_to(root)), "sha256": sha(path)}
            publish(state_path, state)
            current.append(observation)
        if sha(root / "research/data/stock_basic_baostock.csv") != master_sha:
            raise ValueError("master_changed_during_feature_computation")
        latest_daily = read_json(daily_path)
        if latest_daily.get("target_date") != target or digest(latest_daily.get("calendar_dates")) != source_contract["calendar_sha256"]:
            raise ValueError("calendar_changed_during_feature_computation")
        cross = cross_section(current, target_count=len(codes))
        complete = cross["status"] == "complete"
        if complete:
            identity = digest(sorted(row["input_identity"] for row in current))
            cross_path = directory / ("cross_section."+identity+".json")
            if not cross_path.exists():
                publish(cross_path, {**cross, "schema": SCHEMA, "input_identity": identity,
                                     "observed_at": now().isoformat(), "data_as_of": target,
                                     "input_references": [state["items"][row["code"]] for row in current]})
            else:
                existing = read_json(cross_path)
                if existing.get("input_identity") != identity or any(existing.get(k) != v for k, v in cross.items()):
                    raise ValueError("existing_cross_section_identity_mismatch")
        price_ready = [row for row in current if row["status"] == "price_features_computed"]
        factors = {factor["name"]: sum(row["factor_values"].get(factor["id"]) is not None for row in price_ready) for factor in FACTORS}
        if complete:
            factors["行业相对强度"] = len(cross["rows"])
        status = "complete" if complete else ("collecting" if len(current) < len(codes) else "waiting_inputs")
        review_count = sum(row["status"] == "needs_review" for row in current)
        report = {"schema": SCHEMA, "status": status,
                  "implementation_sha256": IMPLEMENTATION_SHA256,
                  "job_run_id": os.environ.get("REGIS_RESEARCH_JOB_ID"),
                  "data_as_of": target, "target": len(codes), "price_windows": len(price_ready),
                  "universe_scope": "sh_sz_main_chinext_listed_at_target", "full_a_share_coverage": False,
                  "review_required": review_count,
                  "factor_finite_counts": factors, "cross_section_status": cross["status"],
                  "summary": f"日线截至{target}（沪深主板与创业板）；已校验 {len(price_ready)}/{len(codes)} 只股票的61日价格窗口并计算四个价格特征，{review_count}只输入需复核；行业相对强度{'已完成研究计算' if complete else '等待完整横截面与同日行业'}。情绪战法路由、成本后及前向验证尚未执行。",
                  **BOUNDARY}
        publish(root / "reports/cloud_pattern_features_progress.json", report)
        return report
