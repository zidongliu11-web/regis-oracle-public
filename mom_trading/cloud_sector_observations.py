"""Independent sector observations, not H1 packets or trading confirmations.

One attempt per five-minute weekday observation bucket. Missing/rejected buckets
break continuity; neither a later retry nor an old quotation fills a missed slot.
"""
from __future__ import annotations

from datetime import datetime
import fcntl
import hashlib
import math
import os
from pathlib import Path
import shutil

from .cloud_research_worker import ROOT, publish, read_json
from .market_intelligence import MarketIntelligence
from .market_sector_strategy_adapter import SH_TZ, adapt_sector_snapshot, _sector_rows
from .market_state_daily import _atomic_publish, _canonical_bytes, _sha256

SCHEMA = "cloud_sector_observations.v1"
IMPLEMENTATION_SHA256 = _sha256(Path(__file__))
BOUNDARY = {"research_only": True, "generates_alert": False, "changes_trade_authority": False,
            "calendar_verified": False, "forward_sample": False, "returns_read": False}


def health_header(status):
    job = (status.get("jobs") or {}).get("sector_observations") or {}
    state = job.get("status") if status.get("online") and job.get("status") in {
        "complete", "scheduled", "running", "waiting_inputs", "failed", "owned_elsewhere",
    } else "unverified"
    attempted, accepted = job.get("attempts"), job.get("accepted")
    if not (status.get("online") and type(attempted) is int and type(accepted) is int and 0 <= accepted <= attempted <= 48):
        attempted = accepted = "unknown"
    identity = job.get("implementation_sha256")
    suffix = ";implementation="+identity if (isinstance(identity, str) and len(identity) == 64
                                               and all(c in "0123456789abcdef" for c in identity)) else ""
    reason = job.get("failure_reason")
    check = ";check="+reason if reason in {
        "source_request_failed", "sector_source_not_verified", "sector_snapshot_invalid", "sector_coverage_invalid",
        "sector_identity_invalid", "sector_breadth_invalid", "sector_change_invalid", "sector_time_invalid",
        "coverage_complete", "industry_complete", "concept_complete", "timestamp_complete", "breadth_count_complete",
    } else ""
    return f"{state};attempts={attempted};accepted={accepted}{suffix}{check}"


def bucket(stamp):
    if stamp.tzinfo is None:
        raise ValueError("timezone_required")
    stamp = stamp.astimezone(SH_TZ)
    minute = stamp.hour*60+stamp.minute
    if stamp.weekday() >= 5:
        return None
    if 570 <= minute < 690:
        return (minute-570)//5
    if 780 <= minute < 900:
        return 24+(minute-780)//5
    return None


def contract():
    directory = Path(__file__).parent
    return {name: _sha256(directory / name) for name in (
        "cloud_sector_observations.py", "market_intelligence.py",
        "market_sector_strategy_adapter.py", "market_sector_route_forward.py",
    )}


def inspect(snapshot, observed):
    """Validate the normalized supplier snapshot independently of an H1 state."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("meta"), dict):
        return ["sector_snapshot_invalid"], None
    errors = []
    if bucket(observed) is None:
        errors.append("outside_observation_hours")
    meta = snapshot.get("meta") or {}
    for key in ("coverage_complete", "industry_complete", "concept_complete", "timestamp_complete", "breadth_count_complete"):
        if meta.get(key) is not True:
            errors.append(key)
    for key, count in (("industries", "industry_reported_total"), ("concepts", "concept_reported_total")):
        rows = snapshot.get(key)
        if (not isinstance(rows, list) or type(meta.get(count)) is not int
                or meta[count] <= 0 or len(rows) != meta[count]):
            errors.append("sector_coverage_invalid")
            continue
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                errors.append("sector_identity_invalid")
                continue
            code, name = row.get("code"), row.get("name")
            if not isinstance(code, str) or not code.strip() or code in seen or not isinstance(name, str) or not name.strip():
                errors.append("sector_identity_invalid")
            if isinstance(code, str):
                seen.add(code)
            if type(row.get("change_pct")) not in (int, float) or not math.isfinite(row["change_pct"]):
                errors.append("sector_change_invalid")
            if any(type(row.get(field)) is not int or row[field] < 0 for field in ("up_count", "down_count")):
                errors.append("sector_breadth_invalid")
            try:
                stamp = datetime.fromisoformat(row["data_as_of"])
                if (stamp.tzinfo is None or stamp.astimezone(SH_TZ).date() != observed.date()
                        or not 0 <= (observed-stamp).total_seconds() <= 180):
                    raise ValueError("source_time")
            except (ValueError, TypeError, KeyError):
                errors.append("sector_time_invalid")
    if errors:
        return sorted(set(errors)), None
    adapted = adapt_sector_snapshot(snapshot, now=observed)
    if not adapted["observation_ready"]:
        return ["sector_source_not_verified"], None
    return [], adapted


def read_packet(path, day):
    if path.is_symlink():
        raise ValueError("observation_symlink")
    packet = read_json(path)
    identity = dict(packet)
    packet_id = identity.pop("packet_id", None)
    if (hashlib.sha256(_canonical_bytes(identity)).hexdigest() != packet_id
            or packet.get("schema") != SCHEMA or packet.get("source_contract") != contract()
            or any(packet.get(k) != v for k, v in BOUNDARY.items())):
        raise ValueError("observation_identity_invalid")
    observed = datetime.fromisoformat(packet["observed_at"])
    started = datetime.fromisoformat(packet["started_at"])
    if (observed.tzinfo is None or observed.astimezone(SH_TZ).date().isoformat() != day
            or started.tzinfo is None or started > observed or bucket(started) != bucket(observed)
            or bucket(observed) != packet.get("bucket") or path.name != f"{packet['bucket']:02}.json"):
        raise ValueError("observation_bucket_invalid")
    if packet.get("source_error"):
        errors = ["source_request_failed"]
    else:
        errors, _ = inspect(packet["snapshot"], observed)
    if errors != packet.get("errors"):
        raise ValueError("observation_assessment_changed")
    return packet


def run(*, root=ROOT, now=None, fetch=None):
    if os.environ.get("REGIS_EXECUTION_ROLE") != "cloud":
        return {"status": "local_unchanged", "summary": "不在开发电脑生成云端板块留档。", **BOUNDARY}
    now = now or (lambda: datetime.now(SH_TZ))
    start = now()
    bucket(start)  # Reject naive clocks before conversion.
    start = start.astimezone(SH_TZ)
    index = bucket(start)
    base = {"implementation_sha256": IMPLEMENTATION_SHA256, "job_run_id": os.environ.get("REGIS_RESEARCH_JOB_ID"), **BOUNDARY}
    if index is None:
        return {**base, "status": "scheduled", "summary": "工作日9:30–11:30、13:00–15:00记录供应商同日板块快照；不把时间窗口当交易日历核验。"}
    if shutil.disk_usage(root).free < 2*1024**3:
        return {**base, "status": "waiting_inputs", "summary": "存储不足2GB，保留已有板块观察，不增加原件。"}
    day = start.date().isoformat()
    directory = root / "reports/cloud_sector_observations" / day
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**base, "status": "owned_elsewhere", "summary": "本日板块观察已有写入者。"}
        path = directory / f"{index:02}.json"
        if path.exists():
            packet = read_packet(path, day)
        else:
            source_error = None
            try:
                if fetch is None:
                    service = MarketIntelligence(None, root / "unused_sector_only", lambda _: ([], {}))
                    snapshot = service.market_sectors(force=True)
                else:
                    snapshot = fetch()
            except Exception as exc:
                snapshot, source_error = {}, type(exc).__name__
            finish = now()
            bucket(finish)
            finish = finish.astimezone(SH_TZ)
            if finish.date() != start.date() or bucket(finish) != index or finish < start:
                return {**base, "status": "waiting_inputs", "summary": "板块请求跨越观察桶，不回填已错过时点。"}
            errors, _ = (["source_request_failed"], None) if source_error else inspect(snapshot, finish)
            packet = {"schema": SCHEMA, "bucket": index, "observed_at": finish.isoformat(),
                      "started_at": start.isoformat(), "source_contract": contract(), "source_error": source_error,
                      "snapshot": snapshot, "errors": errors, **BOUNDARY}
            packet["packet_id"] = hashlib.sha256(_canonical_bytes(packet)).hexdigest()
            _atomic_publish(path, _canonical_bytes(packet))
        packets = [read_packet(p, day) for p in sorted(directory.glob("[0-4][0-9].json"))]
        accepted = sum(not p["errors"] for p in packets)
        result = {**base, "status": "complete" if not packet["errors"] else "waiting_inputs", "data_as_of": day,
                  "failure_reason": packet["errors"][0] if packet["errors"] else None,
                  "attempts": len(packets), "accepted": accepted, "observed_at": packet["observed_at"],
                  "summary": f"{day}已留档{len(packets)}个观察桶，其中{accepted}个通过板块来源核验；缺桶和失败不回填，不替代H1情绪或买入准入。"}
        publish(root / "reports/cloud_sector_observations_progress.json", result)
        return result


def history(*, root=ROOT, now=None):
    """Describe successive top-three observations, without predicting returns."""
    now = (now or datetime.now(SH_TZ)).astimezone(SH_TZ)
    day = now.date().isoformat()
    directory = root / "reports/cloud_sector_observations" / day
    empty = {"status": "unavailable", "summary": "暂无可核验的同日连续板块观察；单次排名不能证明主线持续。", "rows": [], **BOUNDARY}
    try:
        packets = [read_packet(p, day) for p in sorted(directory.glob("[0-4][0-9].json"))]
        if not packets or any(datetime.fromisoformat(p["observed_at"]) > now for p in packets):
            return empty
        last = packets[-1]
        if last["errors"]:
            return {**empty, "summary": "最新板块观察未通过来源核验，不沿用之前的强势名单。"}
        observations, wanted = [], {}
        for packet in packets:
            stamp = datetime.fromisoformat(packet["observed_at"])
            adapted = inspect(packet["snapshot"], stamp)[1] if not packet["errors"] else None
            rows = _sector_rows(packet["snapshot"])[0] if adapted else []
            leaders = {(r["kind"], r["code"]): r for r in adapted["observed_sectors"]} if adapted else {}
            observations.append({"bucket": packet["bucket"], "time": packet["observed_at"], "leaders": leaders,
                                 "universe": (packet["snapshot"].get("source"), sorted((r["kind"], r["code"], r["name"]) for r in rows)),
                                 "values": {(r["kind"], r["code"]): r for r in rows}})
        for observation in observations[-2:]:
            wanted.update(observation["leaders"])
        result = []
        for key, label in wanted.items():
            current = observations[-1]["values"].get(key)
            if not current:
                continue
            chain = []
            for previous in reversed(observations):
                if (key not in previous["leaders"] or (chain and (
                        previous["bucket"] != chain[-1]["bucket"]-1
                        or previous["universe"] != chain[-1]["universe"]
                        or datetime.fromisoformat(previous["leaders"][key]["data_as_of"]) >= datetime.fromisoformat(chain[-1]["leaders"][key]["data_as_of"])))):
                    break
                chain.append(previous)
            comparison = observations[-2] if (len(observations) >= 2 and observations[-2]["bucket"] == last["bucket"]-1
                                                and observations[-2]["universe"] == observations[-1]["universe"]) else None
            old = comparison["values"].get(key) if comparison else None
            # Matching board identifiers cannot establish a fixed constituent set.
            delta = current["advance_coverage_pct"]-old["advance_coverage_pct"] if old and None not in (old["advance_coverage_pct"], current["advance_coverage_pct"]) else None
            result.append({"name": current["name"], "kind": key[0], "code": key[1],
                           "in_latest_top_three": key in observations[-1]["leaders"],
                           "consecutive_buckets": len(chain), "breadth_change_pp": delta,
                           "comparison_at": comparison["time"] if comparison else None,
                           "first_consecutive_at": chain[-1]["time"] if chain else None,
                           "latest_at": observations[-1]["time"], "current_breadth_pct": current["advance_coverage_pct"]})
        age = (now-datetime.fromisoformat(last["observed_at"])).total_seconds()
        return {"status": "observed", "summary": "固定5分钟观察桶内每桶一次；展示是否连续进入前3观察名单及广度变化。不是连续成交验证、收益证据或生产主线确认。",
                "observed_at": last["observed_at"], "age_seconds": age,
                "historical_only": age > 600 or bucket(now) is None,
                "attempts": len(packets), "accepted": sum(not p["errors"] for p in packets),
                "rows": result, **BOUNDARY}
    except (OSError, ValueError, KeyError, TypeError):
        return {**empty, "summary": "板块历史原件或规则身份不可核验；不沿用旧的持续性判断。"}
