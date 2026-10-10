"""Join cloud-owned daily evidence using the existing fixed research rules.

No price/emotion producer changes, backtests, account access or alert writes.
An observed condition is not an admitted strategy or an executable buy plan.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import deque
import csv
from datetime import datetime, timezone
import fcntl
import gzip
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

import pandas as pd

from . import cloud_market_emotion as emotion
from . import cloud_pattern_features as feature
from .cloud_pattern_inputs import _source_universe, assess_receipt, sha
from .cloud_research_worker import ROOT, publish, read_json
from .emotion_pattern_research import PLAYBOOKS, build_research_candidates, research_catalog

SCHEMA = "cloud_pattern_route.v1"
IMPLEMENTATION_SHA256 = sha(Path(__file__))
BOUNDARY = {"production_eligible": False, "generates_alert": False,
            "automatic_trading": False, "account_returns_computed": False,
            "return_labels_read": False, "forward_evidence": False,
            "intraday_sector_confirmation": False, "suggested_shares": None}


def dependencies(regime):
    """Observation dependencies are not production-strategy admission gates."""
    if regime not in emotion.STATES or regime == "UNKNOWN":
        raise ValueError("market_emotion_not_classified")
    mode = "defense" if regime == "RISK_OFF" else "exit_review" if regime == "OVERHEATED" else "entry_research"
    return {"route_mode": mode,
            "route_label": {"defense": "退潮防守", "exit_review": "高潮持仓退出复核", "entry_research": "主线入场条件研究"}[mode],
            "condition_scan_performed": mode != "defense", "sector_evaluated": mode == "entry_research",
            "auxiliary_factors_required": False}


def condition_cross_section(observations, target_count, regime):
    if regime != "OVERHEATED":
        return feature.cross_section(observations, target_count=target_count)
    # Exit shapes do not use industry ranking. Do not invent a membership to
    # make the entry cross-section checker pass; keep that factor unavailable.
    if len(observations) != target_count or len({r["code"] for r in observations}) != target_count:
        raise ValueError("complete_price_cross_section_required")
    if len({r["data_as_of"] for r in observations}) > 1:
        raise ValueError("cross_section_mixed_dates")
    eligible = [r for r in observations if r["status"] != "ineligible_history" and r.get("is_st") is not True]
    if any(r["status"] != "price_features_computed" for r in eligible):
        raise ValueError("waiting_price_checks")
    return {"status": "complete", "research_sectors": [], "excluded_history_or_st": len(observations)-len(eligible),
            "rows": [{"code": r["code"], "factor_values": {**r["factor_values"], "industry_excess_20": None}} for r in eligible]}


def _implementations():
    source_root = Path(__file__).resolve().parents[1]
    return feature.digest({name: sha(source_root / name) for name in (
        "mom_trading/cloud_pattern_route.py", "mom_trading/cloud_pattern_features.py",
        "mom_trading/cloud_pattern_inputs.py", "mom_trading/cloud_industry_daily.py",
        "mom_trading/cloud_market_emotion.py", "mom_trading/emotion_pattern_research.py",
        "mom_trading/cloud_daily_revisions.py",
        "research/market_rules.py", "research/build_state_conditioned_mainline_inputs.py",
        "research/build_market_state_feature_epoch_v1.py", "research/build_market_state_feature_epoch_v2.py",
    )})


def _observed(value, now):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or not parsed <= now:
        raise ValueError("source_observation_timestamp_invalid")
    return parsed


def _price_terms(root, observation, target):
    """Recover only prices already validated by the unchanged price producer."""
    refs = observation["input_references"]
    path = feature._bound(root, refs["raw"], root / "research/data/v4_daily")
    adjustment = read_json(feature._bound(root, refs["adjustment"], root / "reports/cloud_pattern_inputs" / target / "receipts"))
    if assess_receipt(adjustment) != "captured_not_validated":
        raise ValueError("adjustment_receipt_changed")
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as stream:
        rows = list(deque(csv.DictReader(stream), maxlen=4))
    if len(rows) != 4 or rows[-1]["date"] != target or any(row["code"] != observation["code"] for row in rows):
        raise ValueError("candidate_price_identity_invalid")
    events = sorted((dict(zip(adjustment["fields"], row)) for row in adjustment["rows"]), key=lambda row: row["dividOperateDate"])
    days = [row["dividOperateDate"] for row in events]
    indices = [bisect_right(days, row["date"]) - 1 for row in rows]
    if min(indices) < 0:
        raise ValueError("candidate_adjustment_prefix_missing")
    factors = [float(events[i]["backAdjustFactor"]) for i in indices]
    return {"close": float(rows[-1]["close"]), "low": float(rows[-1]["low"]),
            "pullback_stop": min(float(row["low"])*factor for row, factor in zip(rows, factors))/factors[-1]}


def _load(root, now):
    target, master_sha, universe = _source_universe(root)
    if now < datetime.fromisoformat(target+"T15:00:00+08:00"):
        raise ValueError("completed_daily_bars_required")
    daily = read_json(root / "reports/cloud_market_data.json")
    calendar = daily.get("calendar_dates") or []
    if not calendar or calendar[-1] != target or calendar != sorted(set(calendar)):
        raise ValueError("calendar_identity_invalid")
    calendar_sha = feature.digest(calendar)
    directory = root / "reports/cloud_market_emotion" / target
    state = read_json(directory / "manifest.json")
    if (state.get("schema") != emotion.SCHEMA or state.get("master_sha256") != master_sha
            or state.get("target_date") != target or not state.get("snapshot")):
        raise ValueError("complete_market_emotion_required")
    snapshot = read_json(feature._bound(root, state["snapshot"], directory))
    contract = snapshot.get("source_contract") or {}
    if (snapshot.get("schema") != emotion.SCHEMA or snapshot.get("data_as_of") != target
            or contract.get("implementation_sha256") != emotion.IMPLEMENTATION_SHA256
            or contract.get("master_sha256") != master_sha or contract.get("calendar_sha256") != calendar_sha):
        raise ValueError("market_emotion_identity_invalid")
    source_root = Path(__file__).resolve().parents[1]
    expected_modules = ("research/market_rules.py", "research/build_state_conditioned_mainline_inputs.py",
                        "research/build_market_state_feature_epoch_v1.py", "research/build_market_state_feature_epoch_v2.py",
                        "mom_trading/cloud_pattern_inputs.py", "mom_trading/cloud_pattern_features.py",
                        "mom_trading/emotion_pattern_research.py", "mom_trading/cloud_daily_revisions.py")
    if contract.get("source_modules") != {name: sha(source_root / name) for name in expected_modules}:
        raise ValueError("market_emotion_source_implementation_changed")
    refs = snapshot.get("source_references") or {}
    if set(refs) != set(universe) or any(row.get("status") != "computed" for row in refs.values()):
        raise ValueError("market_emotion_coverage_incomplete")
    raw_manifest = read_json(root / "reports/v4_daily_download_manifest.json")
    raw_files = {row["code"]: row for row in raw_manifest.get("files", [])}
    from .cloud_daily_revisions import load_files
    raw_files = load_files(root, raw_files, target, now=now)
    if (len(raw_files) != len(raw_manifest.get("files", []))
            or any(raw_files.get(code.split(".")[1]) != row["source"] for code, row in refs.items())):
        raise ValueError("current_raw_manifest_changed")
    if snapshot.get("input_identity") != emotion.snapshot_identity(refs, universe, contract):
        raise ValueError("market_emotion_input_identity_invalid")
    events, special_sha, cloud_event_sha = emotion.load_special_events(root)
    if special_sha != contract.get("special_events_sha256"):
        raise ValueError("market_emotion_special_events_changed")
    if cloud_event_sha != contract.get("cloud_price_events_sha256"):
        raise ValueError("market_emotion_cloud_price_events_changed")
    _observed(snapshot["observed_at"], now)
    last = (snapshot.get("rows") or [{}])[-1]
    if str(last.get("trade_date", ""))[:10] != target or snapshot.get("latest_state") != (last.get("features") or {}).get("confirmed_state"):
        raise ValueError("market_emotion_last_session_invalid")
    regime = snapshot["latest_state"]
    policy = dependencies(regime)
    # Incomplete feature coverage must wait before hashing thousands of histories.
    with (root / "research/data/stock_basic_baostock.csv").open(encoding="utf-8-sig", newline="") as stream:
        codes = {row["code"] for row in csv.DictReader(stream) if row["code"] in universe and (not row["out_date"] or row["out_date"] >= target)}
    price_directory = root / "reports/cloud_pattern_features" / target
    price_state = read_json(price_directory / "manifest.json") if policy["condition_scan_performed"] else {}
    if policy["condition_scan_performed"] and (price_state.get("schema") != feature.SCHEMA or price_state.get("master_sha256") != master_sha
            or price_state.get("target_date") != target or set(price_state.get("items", {})) != codes):
        raise ValueError("complete_price_cross_section_required")
    for code, entry in refs.items():
        expected = emotion.history_identity(entry["source"], code, target, contract, events)
        if expected != entry["input_identity"]:
            raise ValueError("market_emotion_security_identity_invalid")
        feature._bound(root, entry["source"], root / "research/data/v4_daily")
        feature._bound(root, entry, directory / "observations")
    if not policy["condition_scan_performed"]:
        cross = {"status": "not_required_for_defense", "rows": [], "research_sectors": [], "excluded_history_or_st": 0}
        return target, master_sha, calendar_sha, regime, [], cross, {}, snapshot["input_identity"]

    directory, state = price_directory, price_state
    observations = []
    for code in sorted(codes):
        entry = state["items"][code]
        row = read_json(feature._bound(root, entry, directory / "observations"))
        source = row.get("source_contract") or {}
        if (row.get("schema") != feature.SCHEMA or row.get("code") != code or row.get("data_as_of") != target
                or row.get("input_identity") != entry.get("input_identity")
                or source.get("feature_implementation_sha256") != feature.IMPLEMENTATION_SHA256
                or source.get("rule_sha256") != research_catalog()["rule_sha256"]
                or source.get("rule_implementation_sha256") != feature.ENGINE_SHA256
                or source.get("calendar_sha256") != calendar_sha or source.get("master_sha256") != master_sha
                or source.get("receipt_validation_sha256") != sha(source_root / "mom_trading/cloud_pattern_inputs.py")
                or source.get("daily_industry_validation_sha256") != feature.INDUSTRY_SHA256
                or source.get("daily_revision_validation_sha256") != sha(source_root / "mom_trading/cloud_daily_revisions.py")):
            raise ValueError("price_observation_identity_invalid")
        _observed(row["observed_at"], now)
        inputs = row["input_references"]
        identity = feature.digest({"code": code, "target": target, "source_contract": source,
                                   "source_sha256": {k: v["sha256"] if v else None for k, v in inputs.items()}})
        if identity != row["input_identity"] or inputs["raw"]["sha256"] != refs[code]["source"]["sha256"]:
            raise ValueError("price_emotion_raw_source_mismatch")
        feature._bound(root, inputs["raw"], root / "research/data/v4_daily")
        feature._bound(root, inputs["adjustment"], root / "reports/cloud_pattern_inputs" / target / "receipts")
        if inputs.get("industry"):
            parent = (root / inputs["industry"]["path"]).parent
            if parent not in {root / "reports/cloud_pattern_inputs" / target / "receipts", root / "reports/cloud_industry_daily" / target / "receipts"}:
                raise ValueError("industry_receipt_path_invalid")
            feature._bound(root, inputs["industry"], parent)
        observations.append(row)
    cross = condition_cross_section(observations, len(codes), regime)
    if cross["status"] != "complete":
        raise ValueError(cross["status"])
    return target, master_sha, calendar_sha, regime, observations, cross, state["items"], snapshot["input_identity"]


def joined_candidates(observations, cross, regime, *, target, price_terms):
    sectors = {row["industry_code"] for row in cross["research_sectors"]}
    factors = {row["code"]: row["factor_values"] for row in cross["rows"]}
    rows = []
    for row in observations:
        if row["code"] not in factors:
            continue
        if not any(regime in p["states"] and row["shape_conditions"][p["id"]] for p in PLAYBOOKS):
            continue
        rows.append({"code": row["code"].split(".")[1], "trade_date": target,
                     "industry_code": row["industry_code"], "eligible": True,
                     "sector_confirmed": row["industry_code"] in sectors,
                     **row["shape_conditions"], **factors[row["code"]], **price_terms(row)})
    if not rows:
        return []
    frame = pd.DataFrame(rows)
    frame["trade_date"] = pd.to_datetime(frame.trade_date)
    result = build_research_candidates(frame, pd.DataFrame([{"trade_date": target, "confirmed_state": regime}]))
    values = json.loads(result.to_json(orient="records", date_format="iso", double_precision=15))
    for row in values:
        row["trade_date"] = target
    return values


def run(*, root=ROOT, now=None):
    if os.environ.get("REGIS_EXECUTION_ROLE") != "cloud":
        return {"status": "local_unchanged", "summary": "联合研究任务不改动开发电脑数据。", **BOUNDARY}
    now = now or (lambda: datetime.now(timezone.utc))
    stamp = now()
    _observed(stamp.isoformat(), stamp)
    directory = root / "reports/cloud_pattern_route"
    directory.mkdir(parents=True, exist_ok=True)
    base = {"schema": SCHEMA, "implementation_sha256": IMPLEMENTATION_SHA256,
            "source_implementations_sha256": _implementations(),
            "observed_at": stamp.isoformat(), "job_run_id": os.environ.get("REGIS_RESEARCH_JOB_ID"), **BOUNDARY}
    with (directory / ".lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**base, "status": "owned_elsewhere", "summary": "已有联合研究任务，不重复计算。"}
        try:
            if shutil.disk_usage(root).free < 2*1024**3:
                raise ValueError("storage_below_two_gigabytes")
            target, master, calendar, regime, observations, cross, refs, market_identity = _load(root, stamp)
            identity = feature.digest({"implementation": IMPLEMENTATION_SHA256, "target": target,
                                       "price_observations": refs, "market_identity": market_identity})
            previous = read_json(root / "reports/cloud_pattern_route_progress.json")
            if previous.get("input_identity") == identity and previous.get("status") == "complete":
                feature._bound(root, previous["snapshot"], directory)
                result = {**previous, **base, "observed_at": previous["observed_at"], "checked_at": stamp.isoformat()}
                publish(root / "reports/cloud_pattern_route_progress.json", result)
                return result
            rows = joined_candidates(observations, cross, regime, target=target, price_terms=lambda row: _price_terms(root, row, target))
            policy = dependencies(regime)
            playbooks = [{"name": p["name"], "characteristic": p["characteristic"]} for p in PLAYBOOKS if regime in p["states"]]
            if policy["route_mode"] == "defense":
                playbooks = [{"name": "退潮防守", "characteristic": "风险关闭时不研究新开仓；持仓仍按原计划、真实现价和当日可卖数量另行复核，不推算卖出股数。"}]
            explanation = (
                "退潮防守；未执行个股条件筛选。价格特征与行业采集仍独立推进，不因其未齐备把已识别情绪改为未知。"
                if policy["route_mode"] == "defense" else
                f"{len(rows)}条退出复核条件；未执行行业排序，不等待入场板块或辅助因子，不代表实际持有这些股票。"
                if policy["route_mode"] == "exit_review" else
                f"{len(cross['research_sectors'])}个日线研究板块，{len(rows)}条条件命中。"
            )
            # Check again after price reads; no mixed-generation snapshot if another writer changed inputs.
            checked = _load(root, stamp)
            if checked[:4] != (target, master, calendar, regime) or checked[6:] != (refs, market_identity):
                raise ValueError("inputs_changed_during_joint_read")
            report = {**base, **policy, "status": "complete", "data_as_of": target,
                      "emotion_state": regime, "emotion_name": emotion.STATES[regime],
                      "scope": "沪深主板与创业板，不含科创板、北交所；日线研究，不是盘中确认。",
                      "covered_stocks": len(observations), "eligible_stocks": len(cross["rows"]),
                      "excluded_stocks": cross["excluded_history_or_st"],
                      "applicable_playbooks": playbooks,
                      "research_sectors": cross["research_sectors"], "matched_conditions": len(rows),
                      "entry_conditions": sum(row["kind"] == "entry_research" for row in rows),
                      "candidates": rows, "price_observation_references": refs,
                      "market_input_identity": market_identity, "calendar_sha256": calendar,
                      "master_sha256": master,
                      "rule_sha256": research_catalog()["rule_sha256"],
                      "summary": f"截至{target}，{emotion.STATES[regime]}，" + explanation + "不是盘中主线确认、验证通过的策略或买卖提醒；不生成股数。"}
            path = directory / ("observation."+uuid4().hex+".json")
            publish(path, report)
            result = {**base, **policy, "status": "complete", "input_identity": identity, "data_as_of": target, "summary": report["summary"],
                      "matched_conditions": len(rows), "snapshot": {"path": str(path.relative_to(root)), "sha256": sha(path)}}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "input_files_unavailable_or_invalid"
            labels = {"complete_market_emotion_required": "完整市场情绪历史尚未齐备",
                      "market_emotion_not_classified": "同日日线情绪仍未分类",
                      "complete_price_cross_section_required": "全范围个股价格特征尚未收齐",
                      "waiting_price_checks": "部分个股价格输入尚待核验",
                      "waiting_same_day_industry": "部分股票缺少同日行业归属",
                      "completed_daily_bars_required": "当日日线尚未收盘",
                      "storage_below_two_gigabytes": "云端可用存储不足2GB"}
            result = {**base, "status": "waiting_inputs", "reason": reason, "matched_conditions": None,
                      "summary": (labels.get(reason) or "联合研究输入不可核验或身份不一致") + "；云端后续轮次自动核验，不能据此判断没有交易机会。"}
        publish(root / "reports/cloud_pattern_route_progress.json", result)
        return result


def public_observation(*, root=ROOT):
    progress = read_json(root / "reports/cloud_pattern_route_progress.json")
    if progress.get("status") != "complete":
        return {"status": "waiting_inputs", "summary": progress.get("summary") or "等待云端同日情绪、完整行业与战法形态联合核验。", **BOUNDARY}
    try:
        snapshot = read_json(feature._bound(root, progress["snapshot"], root / "reports/cloud_pattern_route"))
        daily = read_json(root / "reports/cloud_market_data.json")
        if (snapshot.get("schema") != SCHEMA or snapshot.get("implementation_sha256") != IMPLEMENTATION_SHA256
                or snapshot.get("source_implementations_sha256") != _implementations()
                or snapshot.get("data_as_of") != daily.get("target_date")
                or snapshot.get("master_sha256") != daily.get("master_sha256")
                or snapshot.get("calendar_sha256") != feature.digest(daily.get("calendar_dates"))
                or any(snapshot.get(k) != v for k, v in BOUNDARY.items())
                or any(snapshot.get(k) != v for k, v in dependencies(snapshot.get("emotion_state")).items())
                or not isinstance(snapshot.get("candidates"), list)
                or snapshot.get("matched_conditions") != len(snapshot["candidates"])):
            raise ValueError("joint_observation_stale")
        _observed(snapshot["observed_at"], datetime.now(timezone.utc))
        return {k: (v[:50] if k == "candidates" else v) for k, v in snapshot.items()
                if k not in {"price_observation_references", "job_run_id"}}
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "unavailable", "summary": "联合研究快照不可核验或已换日；不沿用旧股票清单。", **BOUNDARY}
