"""Cloud-owned daily market history for the existing emotion classifier.

This is a new, explicitly identified input reconstruction, not the old frozen
epoch or a forward observation. No strategy performance, account or alert is
read here. The published preclose chain is a price-comparison index, not cash
or share-entitlement accounting.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import deque
import csv
from datetime import date, datetime, timezone
import fcntl
import gzip
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

import numpy as np
import pandas as pd

from research.market_rules import NO_LIMIT_EVENT_TYPES, daily_price_limits
from research.download_v4_daily_baostock import START_DATE, FIELDS
from research.build_state_conditioned_mainline_inputs import _market_state_rows
from research.build_market_state_feature_epoch_v2 import _apply_calendar_features
from .cloud_pattern_inputs import _source_universe, sha, valid_day
from .cloud_pattern_features import _bound, digest
from .cloud_research_worker import ROOT, publish, read_json
from .emotion_pattern_research import PLAYBOOKS

SCHEMA = "cloud_market_emotion.v1"
COLUMNS = ["trade_status", "pct_chg", "amount", "turnover_rate", "adj_close",
           "sma20", "high20", "low20", "is_st", "limit_evaluable",
           "limit_touched", "limit_closed", "prior_limit_continuation_evaluable",
           "limit_streak", "input_missing", "limit_unknown"]
STATES = {"UNKNOWN": "输入不足，情绪未确认", "RISK_OFF": "风险关闭",
          "STABILIZING": "弱修复", "RISK_ON": "情绪上升", "OVERHEATED": "高潮拥挤"}
BOUNDARY = {"production_eligible": False, "generates_alert": False,
            "account_returns_computed": False, "return_labels_read": False,
            "historical_input_reconstruction": True, "forward_evidence": False,
            "state_model_validated": False, "full_a_share_coverage": False}
IMPLEMENTATION_SHA256 = sha(Path(__file__))
PRICE_POLICY = "published_close_preclose_relative_price_chain;20_consecutive_active_sessions"
PRICE_REFERENCE = "https://www.baostock.com/helpdocs/pdf/BaoStock%E5%A4%8D%E6%9D%83%E5%9B%A0%E5%AD%90%E7%AE%80%E4%BB%8B.pdf"


def stock_history(path, *, code, ipo, out_date, calendar, special_events):
    """One row per official session; unobserved listed sessions remain unknown."""
    if (not valid_day(ipo) or (out_date and (not valid_day(out_date) or out_date < ipo))
            or not calendar or any(not valid_day(d) for d in calendar)
            or calendar != sorted(set(calendar))):
        raise ValueError("market_history_calendar_or_listing_invalid")
    days = {day: i for i, day in enumerate(calendar)}
    columns = {name: i for i, name in enumerate(COLUMNS)}
    matrix = np.full((len(calendar), len(COLUMNS)), np.nan)
    matrix[:, columns["input_missing"]] = [int(ipo <= day <= (out_date or calendar[-1])) for day in calendar]
    matrix[:, columns["limit_unknown"]] = 0
    closes, previous_index = deque(maxlen=20), None
    prior_closed, streak = (None, None) if ipo < calendar[0] else (False, 0)
    previous_day, price_index = "", None
    rule_mismatches = []
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or len(reader.fieldnames) != len(FIELDS.split(",")) or set(reader.fieldnames) != set(FIELDS.split(",")):
            raise ValueError("market_history_source_columns_invalid")
        for raw in reader:
            day = raw.get("date", "")
            if (day not in days or day <= previous_day or not ipo <= day <= (out_date or calendar[-1])
                    or raw.get("code") != code or raw.get("adjustflag") != "3"):
                raise ValueError("market_history_source_identity_invalid")
            previous_day, index = day, days[day]
            status, st = raw.get("tradestatus"), raw.get("isST")
            if status not in {"0", "1"} or st not in {"0", "1"}:
                raise ValueError("market_history_security_status_invalid")
            consecutive = previous_index is not None and index == previous_index + 1
            if previous_index is not None and not consecutive:
                closes.clear()
                price_index, prior_closed, streak = None, None, None
            row = {"trade_status": int(status), "is_st": int(st), "input_missing": 0,
                   "limit_evaluable": 0, "limit_touched": 0, "limit_closed": 0,
                   "prior_limit_continuation_evaluable": 0, "limit_streak": 0, "limit_unknown": 0}
            if status == "0":
                closes.clear()
                price_index, prior_closed, streak = None, False, 0
            else:
                values = {name: float(raw[name]) for name in ("open", "high", "low", "close", "preclose", "amount", "volume", "pctChg", "turn")}
                if (not all(math.isfinite(v) for v in values.values())
                        or min(values[k] for k in ("open", "high", "low", "close", "preclose")) <= 0
                        or min(values[k] for k in ("amount", "volume", "turn")) < 0
                        or values["high"] < max(values[k] for k in ("open", "close", "low"))
                        or values["low"] > min(values[k] for k in ("open", "close", "high"))):
                    raise ValueError("market_history_price_or_activity_invalid")
                if abs((values["close"]/values["preclose"]-1)*100-values["pctChg"]) > 0.02:
                    raise ValueError("market_history_reported_change_mismatch")
                # 100 is an arbitrary relative-index origin, NOT an assumed
                # corporate-action factor of 1. Only price ratios are used.
                price_index = 100.0 if price_index is None else price_index*(values["close"]/values["preclose"])
                if not math.isfinite(price_index) or price_index <= 0:
                    raise ValueError("market_history_price_chain_invalid")
                closes.append(price_index)
                row.update(pct_chg=values["pctChg"], amount=values["amount"],
                           turnover_rate=values["turn"]/100, adj_close=price_index)
                if len(closes) == 20:
                    row.update(sma20=sum(closes)/20, high20=max(closes), low20=min(closes))
                listed = (6+index if ipo < calendar[0] else bisect_right(calendar, day)-bisect_left(calendar, ipo))
                limits = daily_price_limits(code, date.fromisoformat(day), values["preclose"],
                                            is_st=st == "1", listed_trading_days=listed,
                                            special_event=special_events.get((code, day)))
                ordinary = st == "0" and limits["ratio"] is not None
                mismatch = ordinary and (values["high"] > limits["up_limit"]+0.011
                                          or values["low"] < limits["down_limit"]-0.011)
                if mismatch:
                    row["limit_unknown"] = 1
                    rule_mismatches.append(day)
                    prior_closed, streak = None, None
                elif ordinary:
                    closed = abs(values["close"]-limits["up_limit"]) <= 0.0051
                    touched = values["high"] >= limits["up_limit"]-0.0051
                    row.update(limit_evaluable=1, limit_closed=int(closed), limit_touched=int(touched))
                    if prior_closed is None or (closed and streak is None):
                        row["limit_unknown"] = 1
                    if consecutive and prior_closed is True:
                        row["prior_limit_continuation_evaluable"] = 1
                    streak = (None if streak is None and closed else
                              streak+1 if closed and consecutive and prior_closed else int(closed))
                    row["limit_streak"] = streak if streak is not None else 0
                    prior_closed = bool(closed)
                else:
                    prior_closed, streak = False, 0
            for key, value in row.items():
                matrix[index, columns[key]] = value
            previous_index = index
    # An empty, schema-correct receipt still says nothing about listed days.
    # Preserve those days as unknown rather than dropping a delisted security
    # or preventing otherwise independent later sessions from being evaluated.
    return matrix, {"missing_listed_sessions": int(matrix[:, columns["input_missing"]].sum()),
                    "unresolved_limit_sessions": int(matrix[:, columns["limit_unknown"]].sum()),
                    "rule_mismatch_dates_sample": rule_mismatches[:20]}


def emotion_rows(cube, *, calendar):
    """Reuse the frozen B/L/F/C aggregation and 504/252 classifier unchanged."""
    rows = []
    for i, day in enumerate(calendar):
        frame = pd.DataFrame(cube[i], columns=COLUMNS)
        missing = int(frame.input_missing.sum())
        unknown_limits = int(frame.limit_unknown.sum())
        for name in ("limit_evaluable", "limit_touched", "limit_closed", "prior_limit_continuation_evaluable"):
            frame[name] = frame[name].eq(1)
        frame["trade_date"] = pd.Timestamp(day)
        row = _market_state_rows(frame)[0]
        # Partial counts can be diagnosed, never silently treated as a complete
        # market. Keep the calendar session in the percentile window as null.
        if missing:
            row["B"].update(up_ratio_pct=None, above_ma20_ratio_pct=None, new_high_minus_low_ratio_pct=None)
            row["C"]["top_5pct_amount_share_pct"] = None
        if missing or unknown_limits:
            row["L"]["yesterday_limit_continuation_ratio_pct"] = None
            row["F"]["eod_failed_limit_ratio_pct"] = None
        row["coverage"] = {"missing_listed_sessions": missing, "unresolved_limit_sessions": unknown_limits,
                           "source_rows_observed": int(frame.trade_status.notna().sum()),
                           "raw_counts_diagnostic_only": bool(missing or unknown_limits)}
        rows.append(row)
    _apply_calendar_features(rows)
    return rows


def route_research(state):
    """Applicable recipes, never selected stocks or a trading permission."""
    return [{"name": p["name"], "kind": p["kind"], "characteristic": p["characteristic"],
             "entry": p["entry"], "exit": p["exit"], "status": "recipe_only_not_admitted"}
            for p in PLAYBOOKS if state in p["states"]]


def load_special_events(root):
    """Revalidate current event evidence before either producing or consuming."""
    path = root / "research/data/special_price_limit_events.csv"
    events, local_sha = {}, None
    if path.exists():
        if path.is_symlink():
            raise ValueError("market_special_events_path_invalid")
        local_sha = sha(path)
        with path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                if row.get("verification_status") != "verified":
                    continue
                key = (row["code"], row["date"])
                if key in events or not valid_day(key[1]) or row["event_type"] not in NO_LIMIT_EVENT_TYPES:
                    raise ValueError("market_special_events_identity_invalid")
                events[key] = row["event_type"]
    from .cloud_price_limit_events import load_events
    events, cloud_sha = load_events(root, events)
    return events, local_sha, cloud_sha


def history_identity(raw, code, target, contract, events):
    # Numerical histories depend on this security's validated events, not on
    # unrelated additions to the disclosure index. The aggregate below still
    # binds the entire current evidence contract, including both source hashes.
    numerical = {key: value for key, value in contract.items()
                 if key not in {"special_events_sha256", "cloud_price_events_sha256"}}
    numerical["security_events"] = sorted((day, kind) for (stock, day), kind in events.items() if stock == code)
    return digest({"source": raw["sha256"], "code": code, "target": target, "contract": numerical})


def snapshot_identity(references, codes, contract):
    return digest({"histories": [references[code]["input_identity"] for code in codes], "source_contract": contract})


def run(*, root=ROOT, batch_limit=100, now=None):
    if os.environ.get("REGIS_EXECUTION_ROLE") != "cloud":
        return {"status": "local_unchanged", "summary": "云端情绪历史任务不改动开发电脑研究数据。"}
    if type(batch_limit) is not int or not 1 <= batch_limit <= 500:
        raise ValueError("market_history_batch_limit_invalid")
    now = now or (lambda: datetime.now(timezone.utc))
    current_time = now()
    if current_time.tzinfo is None:
        raise ValueError("market_history_timestamp_invalid")
    if shutil.disk_usage(root).free < 2*1024**3:
        return {"status": "waiting_storage", "summary": "可用磁盘不足2GB，保留情绪历史观察。", **BOUNDARY}
    try:
        target, master_sha, codes = _source_universe(root)
        if current_time < datetime.fromisoformat(target+"T15:00:00+08:00"):
            raise ValueError("market_daily_bar_not_complete")
        daily = read_json(root / "reports/cloud_market_data.json")
        calendar = daily.get("calendar_dates") or []
        if not calendar or any(not valid_day(d) for d in calendar) or calendar != sorted(set(calendar)) or calendar[-1] != target:
            raise ValueError("market_history_calendar_invalid")
        manifest = read_json(root / "reports/v4_daily_download_manifest.json")
        query = manifest.get("query") or {}
        if (not str(manifest.get("source", "")).startswith("BaoStock ") or query.get("frequency") != "d"
                or query.get("end_date") != target or query.get("start_date") != START_DATE
                or query.get("fields") != FIELDS or query.get("adjustflag") != "3 (不复权)"
                or (manifest.get("universe_security_master") or {}).get("sha256") != master_sha):
            raise ValueError("market_history_manifest_identity_invalid")
        raw_files = {entry["code"]: entry for entry in manifest.get("files", [])}
        if len(raw_files) != len(manifest.get("files", [])):
            raise ValueError("market_history_duplicate_source")
        if not set(raw_files) <= {code.split(".")[1] for code in codes}:
            raise ValueError("market_history_universe_mismatch")
        from .cloud_daily_revisions import load_files
        raw_files = load_files(root, raw_files, target, now=current_time)
        with (root / "research/data/stock_basic_baostock.csv").open(encoding="utf-8-sig", newline="") as stream:
            ends = {row["code"]: row["out_date"] for row in csv.DictReader(stream) if row["code"] in codes}
        events, event_sha, cloud_event_sha = load_special_events(root)
    except (OSError, ValueError, KeyError) as exc:
        return {"status": "waiting_inputs", "reason": str(exc),
                "summary": "情绪历史所需日线、日历或证券存续身份不完整，未判定市场没有机会。", **BOUNDARY}
    contract = {"implementation_sha256": IMPLEMENTATION_SHA256, "master_sha256": master_sha,
                "calendar_sha256": digest(calendar), "special_events_sha256": event_sha,
                "cloud_price_events_sha256": cloud_event_sha,
                "price_policy": PRICE_POLICY, "price_reference": PRICE_REFERENCE,
                "special_event_coverage_verified": False, "ordinary_limit_rules_inference_only": True,
                "source_modules": {name: sha(Path(__file__).resolve().parents[1] / name) for name in (
                    "research/market_rules.py", "research/build_state_conditioned_mainline_inputs.py",
                    "research/build_market_state_feature_epoch_v1.py", "research/build_market_state_feature_epoch_v2.py",
                    "mom_trading/cloud_pattern_inputs.py", "mom_trading/cloud_pattern_features.py",
                    "mom_trading/emotion_pattern_research.py", "mom_trading/cloud_daily_revisions.py")}}
    directory = root / "reports/cloud_market_emotion" / target
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "owned_elsewhere", "summary": "同日情绪历史已有进程，不重复启动。", **BOUNDARY}
        state_path = directory / "manifest.json"
        state = read_json(state_path)
        if state_path.exists() and (state.get("schema") != SCHEMA or state.get("master_sha256") != master_sha or state.get("target_date") != target):
            return {"status": "waiting_identity", "summary": "情绪历史批次身份冲突，保留原件。", **BOUNDARY}
        state = state or {"schema": SCHEMA, "master_sha256": master_sha, "target_date": target, "items": {}}
        outputs = directory / "observations"
        outputs.mkdir(exist_ok=True)
        available, pending = {}, []
        for code in codes:
            raw = raw_files.get(code.split(".")[1])
            if not raw:
                continue
            identity = history_identity(raw, code, target, contract, events)
            previous = state["items"].get(code)
            if previous and previous["input_identity"] == identity:
                _bound(root, raw, root / "research/data/v4_daily")
                if previous["status"] == "computed":
                    _bound(root, previous, outputs)
                available[code] = previous
            else:
                pending.append((code, raw, identity))
        reused = sum(item["status"] == "computed" for item in available.values())
        for code, raw, identity in pending[:batch_limit]:
            path = _bound(root, raw, root / "research/data/v4_daily")
            item = {"input_identity": identity, "source": raw, "observed_at": now().isoformat()}
            try:
                matrix, checks = stock_history(path, code=code, ipo=codes[code], out_date=ends[code], calendar=calendar, special_events=events)
                output = outputs / (code+"."+uuid4().hex+".npz")
                metadata = json.dumps({"schema": SCHEMA, "code": code, "input_identity": identity, "source_contract": contract})
                with output.open("xb") as stream:
                    np.savez_compressed(stream, rows=matrix, metadata=metadata)
                item.update(status="computed", path=str(output.relative_to(root)), sha256=sha(output), **checks)
            except (ValueError, KeyError) as exc:
                item.update(status="needs_review", reason=str(exc))
            _bound(root, raw, root / "research/data/v4_daily")
            state["items"][code] = item
            publish(state_path, state)
            available[code] = item
        computed = sum(item["status"] == "computed" for item in available.values())
        reviews = sum(item["status"] == "needs_review" for item in available.values())
        snapshot, aggregate_status = None, "collecting" if len(available) < len(codes) else "waiting_inputs"
        if computed == len(codes):
            identity = snapshot_identity(available, codes, contract)
            existing = state.get("snapshot")
            if existing and existing.get("input_identity") == identity:
                snapshot = read_json(_bound(root, existing, directory))
            else:
                required = len(calendar)*len(codes)*len(COLUMNS)*8
                if shutil.disk_usage(root).free < required+2*1024**3:
                    aggregate_status = "waiting_storage"
                else:
                    # ponytail: one disk-backed cube bounds RAM; only one day
                    # becomes a DataFrame, using the existing pure aggregator.
                    with tempfile.TemporaryDirectory(prefix="regis-market-emotion-", dir=directory) as temporary:
                        cube = np.memmap(Path(temporary) / "history.bin", dtype="float64", mode="w+", shape=(len(codes), len(calendar), len(COLUMNS)))
                        for j, code in enumerate(codes):
                            item = available[code]
                            with np.load(_bound(root, item, outputs), allow_pickle=False) as stored:
                                metadata = json.loads(str(stored["metadata"]))
                                if metadata["code"] != code or metadata["input_identity"] != item["input_identity"] or stored["rows"].shape != (len(calendar), len(COLUMNS)):
                                    raise ValueError("market_history_observation_identity_invalid")
                                cube[j, :, :] = stored["rows"]
                        rows = emotion_rows(cube.transpose(1, 0, 2), calendar=calendar)
                        del cube
                    latest = rows[-1]["features"]["confirmed_state"]
                    snapshot = {"schema": SCHEMA, "input_identity": identity, "source_contract": contract,
                                "observed_at": now().isoformat(), "data_as_of": target, "rows": rows,
                                "source_references": available, "latest_state": latest,
                                "applicable_research_recipes": route_research(latest), **BOUNDARY}
                    output = directory / ("snapshot."+identity+"."+uuid4().hex+".json")
                    publish(output, snapshot)
                    state["snapshot"] = {"input_identity": identity, "path": str(output.relative_to(root)), "sha256": sha(output)}
                    publish(state_path, state)
            if snapshot:
                aggregate_status = "complete"
        latest_daily = read_json(root / "reports/cloud_market_data.json")
        if (sha(root / "research/data/stock_basic_baostock.csv") != master_sha
                or latest_daily.get("target_date") != target or digest(latest_daily.get("calendar_dates")) != contract["calendar_sha256"]
                or load_special_events(root)[1:] != (event_sha, cloud_event_sha)):
            raise ValueError("market_history_inputs_changed_during_computation")
        latest = snapshot["latest_state"] if snapshot else "UNKNOWN"
        known_sessions = sum(row["features"]["confirmed_state"] != "UNKNOWN" for row in snapshot["rows"]) if snapshot else 0
        recipes = snapshot["applicable_research_recipes"] if snapshot else []
        report = {"schema": SCHEMA, "status": aggregate_status, "data_as_of": target,
                  "implementation_sha256": IMPLEMENTATION_SHA256, "job_run_id": os.environ.get("REGIS_RESEARCH_JOB_ID"),
                  "histories_computed": computed, "target": len(codes), "review_required": reviews,
                  "histories_reused": reused, "histories_computed_this_run": computed-reused,
                  "revised_histories": sum("revision" in row for row in raw_files.values()),
                  "classified_sessions": known_sessions, "latest_state": latest, "latest_state_name": STATES[latest],
                  "applicable_research_recipes": recipes,
                  "summary": f"截至{target}的沪深主板/创业板历史：已计算{computed}/{len(codes)}只，{reviews}只输入需复核；"+
                      (f"历史情绪标签为{STATES[latest]}，适用研究配方：{'、'.join(p['name'] for p in recipes) or '无'}。" if snapshot else "完整市场尚未齐备，不能把缺口解释为没有机会。")+
                      f"已接入{sum('revision' in row for row in raw_files.values())}只版本化日线修订，旧原件保留；"
                      "价格与规则推断仍需独立核验；不是盘中情绪确认、通过验证的买入策略或买卖提醒。", **BOUNDARY}
        publish(root / "reports/cloud_market_emotion_progress.json", report)
        return report
