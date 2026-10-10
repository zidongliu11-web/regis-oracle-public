"""Explain a completed reconstruction without changing its inputs or classifier."""
from datetime import datetime, timezone
import csv
import json
import math
from pathlib import Path

import numpy as np

from .cloud_pattern_features import _bound
from .cloud_pattern_inputs import sha, valid_day
from .cloud_research_worker import ROOT, read_json

IMPLEMENTATION_SHA256 = sha(Path(__file__))
LABELS = {
    "up_ratio_pct": "上涨家数占比", "above_ma20_ratio_pct": "站上20日均线占比",
    "new_high_minus_low_ratio_pct": "新高减新低", "yesterday_limit_continuation_ratio_pct": "昨日涨停延续",
    "highest_board": "连板高度", "closed_limit_ratio_pct": "收盘涨停占比",
    "eod_failed_limit_ratio_pct": "收盘炸板率", "top_5pct_amount_share_pct": "成交集中度",
}
REASONS = {"missing_listed_bars", "limit_rules_unresolved", "undefined_current_metrics",
           "insufficient_prior_sessions", "awaiting_state_confirmation", "classified"}


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def assess(snapshot, target_count):
    rows = snapshot["rows"]
    last = rows[-1]
    coverage, features = last["coverage"], last["features"]
    missing, limits = (coverage[key] for key in ("missing_listed_sessions", "unresolved_limit_sessions"))
    if any(type(value) is not int or not 0 <= value <= target_count for value in (missing, limits)):
        raise ValueError("coverage_invalid")
    health = []
    for key, name in LABELS.items():
        value = features[key]
        if value is not None and not finite(value):
            raise ValueError("feature_invalid")
        prior = sum(finite(row["features"][key]) for row in rows[-505:-1])
        health.append({"name": name, "current_available": value is not None,
                       "prior_valid_sessions": prior, "required_prior_sessions": 252})
    current_gaps = [row["name"] for row in health if not row["current_available"]]
    prior_gaps = [row["name"] for row in health if row["prior_valid_sessions"] < 252]
    reason = ("missing_listed_bars" if missing else "limit_rules_unresolved" if limits else
              "undefined_current_metrics" if current_gaps else "insufficient_prior_sessions" if prior_gaps else
              "awaiting_state_confirmation" if features["confirmed_state"] == "UNKNOWN" else "classified")
    return {"reason": reason, "missing_listed_sessions": missing, "unresolved_limit_sessions": limits,
            "current_metric_gaps": current_gaps, "prior_metric_gaps": prior_gaps, "features": health,
            "summary": f"目标日缺失已上市证券日线{missing}只，涨跌停制度/连续性待核{limits}只；"
                       f"当日未定义指标：{'、'.join(current_gaps) or '无'}；"
                       f"过去504个交易日内有效参考不足252日：{'、'.join(prior_gaps) or '无'}。"
                       + ("等待原规则的连续状态确认。" if reason == "awaiting_state_confirmation" else "")}


def missing_securities(snapshot, root):
    """Reconcile the aggregate gap with bound per-security input masks."""
    from .cloud_market_emotion import COLUMNS, SCHEMA, history_identity, load_special_events

    day = snapshot["data_as_of"]
    expected = snapshot["rows"][-1]["coverage"]["missing_listed_sessions"]
    if expected == 0:
        return []
    master = root / "research/data/stock_basic_baostock.csv"
    if sha(master) != snapshot["source_contract"]["master_sha256"]:
        raise ValueError("gap_master_changed")
    events, local_sha, cloud_sha = load_special_events(root)
    contract = snapshot["source_contract"]
    if (local_sha, cloud_sha) != (contract.get("special_events_sha256"), contract.get("cloud_price_events_sha256")):
        raise ValueError("gap_events_changed")
    with master.open(encoding="utf-8-sig", newline="") as stream:
        securities = {row["code"]: row for row in csv.DictReader(stream)}
    directory = root / "reports/cloud_market_emotion" / day / "observations"
    gaps = []
    for code, reference in snapshot["source_references"].items():
        if not reference["missing_listed_sessions"]:
            continue
        path = _bound(root, reference, directory)
        with np.load(path, allow_pickle=False) as stored:
            metadata, values = json.loads(str(stored["metadata"])), stored["rows"]
            if (metadata.get("schema") != SCHEMA or metadata.get("code") != code
                    or metadata.get("input_identity") != reference["input_identity"]
                    or reference["input_identity"] != history_identity(reference["source"], code, day, contract, events)
                    or values.shape != (len(snapshot["rows"]), len(COLUMNS))):
                raise ValueError("gap_observation_identity_invalid")
            if values[-1, COLUMNS.index("input_missing")] != 1:
                continue
            if np.isfinite(values[-1, COLUMNS.index("trade_status")]):
                raise ValueError("gap_mask_disagrees_with_observation")
            observed = np.flatnonzero(np.isfinite(values[:, COLUMNS.index("trade_status")]))
            identity = securities[code]
            if not identity["ipo_date"] <= day <= (identity["out_date"] or day):
                raise ValueError("gap_listing_identity_invalid")
            # No last physical row is NOT a verified suspension or delisting.
            kind = "empty_history" if not len(observed) else "exit_date_boundary" if identity["out_date"] and identity["out_date"] <= day else "active_tail"
            gaps.append({"code": code, "name": identity.get("name") or code, "data_as_of": day,
                         "ipo_date": identity["ipo_date"], "out_date": identity["out_date"], "kind": kind,
                         "last_observed_day": str(snapshot["rows"][int(observed[-1])]["trade_date"])[:10] if len(observed) else None,
                         "raw_reference": reference["source"], "observation_reference": {
                             key: reference[key] for key in ("path", "sha256", "input_identity")}})
    if len(gaps) != expected:
        raise ValueError("gap_count_does_not_reconcile")
    return gaps


def enrich(result, *, root=None, now=None):
    """Called immediately after the producer, never reclassifies or repairs data."""
    if result.get("status") != "complete":
        return result
    root = root or ROOT
    now = now or datetime.now(timezone.utc)
    try:
        from .cloud_market_emotion import IMPLEMENTATION_SHA256 as producer_sha, SCHEMA

        day = result.get("data_as_of")
        if not valid_day(day) or now.tzinfo is None or type(result.get("target")) is not int:
            raise ValueError("report_identity_invalid")
        directory = root / "reports/cloud_market_emotion" / day
        manifest = read_json(directory / "manifest.json")
        reference = manifest["snapshot"]
        snapshot = read_json(_bound(root, reference, directory))
        rows = snapshot["rows"]
        observed = datetime.fromisoformat(snapshot["observed_at"])
        if (manifest["target_date"] != day or snapshot["schema"] != SCHEMA
                or snapshot["data_as_of"] != day or str(rows[-1]["trade_date"])[:10] != day
                or observed.tzinfo is None or observed > now
                or snapshot["input_identity"] != reference["input_identity"]
                or snapshot["source_contract"]["implementation_sha256"] != producer_sha
                or result.get("implementation_sha256") != producer_sha
                or snapshot["latest_state"] != result["latest_state"]
                or rows[-1]["features"]["confirmed_state"] != result["latest_state"]):
            raise ValueError("snapshot_identity_invalid")
        diagnostic = {**assess(snapshot, result["target"]), "status": "available", "data_as_of": day,
                      "snapshot_sha256": reference["sha256"], "implementation_sha256": IMPLEMENTATION_SHA256,
                      "research_only": True, "changes_trade_authority": False}
        try:
            gaps = missing_securities(snapshot, root)
            diagnostic.update(gap_details_status="available", gap_securities=gaps,
                              gap_kinds={kind: sum(row["kind"] == kind for row in gaps)
                                         for kind in ("empty_history", "exit_date_boundary", "active_tail")})
            if gaps:
                diagnostic["summary"] += " 已绑定缺失证券："+"；".join(
                    f"{row['name']}（{row['code']}），最后原件日{row['last_observed_day'] or '没有行'}，"
                    f"上市{row['ipo_date']}，退市登记{row['out_date'] or '未登记'}" for row in gaps[:10])+"。"
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            diagnostic.update(gap_details_status="unavailable", gap_securities=[])
            diagnostic["summary"] += " 逐股原件与缺失数量尚未核对一致，不猜证券。"
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        diagnostic = {"status": "unavailable", "summary": "原件身份或诊断字段不可核验，不能解释为零缺口。",
                      "implementation_sha256": IMPLEMENTATION_SHA256, "research_only": True,
                      "changes_trade_authority": False}
    return {**result, "input_diagnostics": diagnostic,
            "summary": result.get("summary", "")+" 输入诊断："+diagnostic["summary"]}


def header(job):
    d = job.get("input_diagnostics") or {}
    if (job.get("status") != "complete" or d.get("status") != "available" or d.get("reason") not in REASONS
            or d.get("implementation_sha256") != IMPLEMENTATION_SHA256 or d.get("data_as_of") != job.get("data_as_of")):
        return ""
    day = d.get("data_as_of")
    values = [d.get(k) for k in ("missing_listed_sessions", "unresolved_limit_sessions")]
    if not valid_day(day) or any(type(v) is not int or not 0 <= v <= 100000 for v in values):
        return ""
    kinds = d.get("gap_kinds") or {}
    counts = [kinds.get(key) for key in ("empty_history", "exit_date_boundary", "active_tail")]
    detail = (f";empty_history={counts[0]};exit_date_boundary={counts[1]};active_tail={counts[2]}"
              if d.get("gap_details_status") == "available" and all(type(v) is int and 0 <= v <= values[0] for v in counts)
              and sum(counts) == values[0] else "")
    return f";day={day};missing={values[0]};limits={values[1]};reason={d['reason']};diagnostic={IMPLEMENTATION_SHA256}{detail}"
