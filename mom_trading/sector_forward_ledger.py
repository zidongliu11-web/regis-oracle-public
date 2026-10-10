"""Research-only exact-session board qualification labels from verified close tapes.

The label means the same frozen board screen passes again after 1, 3 or 5
trading sessions. It does not mean a profitable trade or a validated mainline.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import hashlib
import json
from pathlib import Path

from .cloud_research_worker import ROOT, read_json
from .market_sector_strategy_adapter import SH_TZ
from .next_session_sectors import _market_cohort, _preparation_pool
from .research_artifact_paths import research_artifact_path
from .sector_rotation_review import _population, _stamp


SCHEMA = "sector_forward_qualification_ledger.v1"
HORIZONS = (1, 3, 5)
MAX_ORIGINS = 60
MAX_LABEL_ROWS = 100_000
MAX_CALENDAR_BYTES = 64 * 1024
MAX_CALENDAR_RECEIPT_BYTES = 128 * 1024
BOUNDARY = {"research_only": True, "production_eligible": False,
            "generates_alert": False, "leader_validated": False,
            "returns_computed": False, "forecast_validated": False}


def _date(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field}_invalid")
    try:
        day = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field}_invalid") from error
    if day.isoformat() != value:
        raise ValueError(f"{field}_invalid")
    return value


def _bounded_file(path, limit, field):
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"{field}_too_large")
    return raw


def _calendar(root, as_of, *, frozen_raw=None):
    """Validate current files or a bounded, original calendar/receipt byte pair.

    Frozen bytes retain the same source, clock and complete-calendar checks;
    refreshing the current pointer cannot change an already captured identity.
    """
    if frozen_raw is None:
        path = root / "reports/current_market_calendar.json"
        if path.is_symlink():
            raise ValueError("calendar_file_symlink")
        raw = _bounded_file(path, MAX_CALENDAR_BYTES, "calendar_file")
    else:
        if (type(frozen_raw) is not tuple or len(frozen_raw) != 2
                or any(type(value) is not bytes for value in frozen_raw)):
            raise ValueError("calendar_frozen_bytes_invalid")
        raw, receipt_raw = frozen_raw
        if len(raw) > MAX_CALENDAR_BYTES:
            raise ValueError("calendar_file_too_large")
        if len(receipt_raw) > MAX_CALENDAR_RECEIPT_BYTES:
            raise ValueError("calendar_source_receipt_too_large")
    calendar = json.loads(raw)
    receipt = calendar["source_receipt"]
    receipt_name = receipt["path"]
    expected = receipt["sha256"]
    if (calendar.get("source") != "BaoStock query_trade_dates"
            or calendar.get("status") != "current"
            or _stamp(calendar["generated_at"]) > as_of
            or not isinstance(receipt_name, str)
            or not receipt_name.startswith("reports/cloud_daily_epochs/calendars/")
            or not isinstance(expected, str) or len(expected) != 64
            or set(expected) - set("0123456789abcdef")):
        raise ValueError("calendar_source_receipt_invalid")
    receipt_path = research_artifact_path(receipt_name, project_root=root)
    if receipt_path.is_symlink():
        raise ValueError("calendar_source_receipt_hash_invalid")
    if frozen_raw is None:
        receipt_raw = _bounded_file(receipt_path, MAX_CALENDAR_RECEIPT_BYTES,
                                    "calendar_source_receipt")
    if hashlib.sha256(receipt_raw).hexdigest() != expected:
        raise ValueError("calendar_source_receipt_hash_invalid")
    packet = json.loads(receipt_raw)
    if not isinstance(packet, dict):
        raise ValueError("calendar_source_receipt_invalid")
    first = _date(calendar["coverage_start"], "coverage_start")
    last = _date(calendar["coverage_end"], "coverage_end")
    first_day, last_day = date.fromisoformat(first), date.fromisoformat(last)
    if first_day > last_day or (last_day - first_day).days >= 512:
        raise ValueError("calendar_coverage_invalid")
    days = [(first_day + timedelta(days=i)).isoformat()
            for i in range((last_day - first_day).days + 1)]
    rows = packet.get("rows")
    if (packet.get("schema") not in {"cloud_daily_epochs.v1", "local_calendar_acquisition.v1"}
            or packet.get("provider") != "BaoStock"
            or packet.get("endpoint") != "query_trade_dates"
            or packet.get("start_date") != first or packet.get("end_date") != last
            or packet.get("fields") != ["calendar_date", "is_trading_day"]
            or packet.get("error_code") != "0" or packet.get("deadline_expired") is not False
            or not isinstance(rows, list) or len(rows) != len(days)
            or any(not isinstance(row, list) or len(row) != 2 or row[1] not in {"0", "1"}
                   for row in rows)
            or [row[0] for row in rows] != days
            or type(packet.get("page_rows")) is not int or packet["page_rows"] != len(days)
            or type(packet.get("cursor")) is not int or packet["cursor"] != len(days)
            or packet.get("page_number") != "1"):
        raise ValueError("calendar_source_receipt_invalid")
    started, completed = (_stamp(packet[key]) for key in ("started_at", "completed_at"))
    if not started <= completed <= _stamp(calendar["generated_at"]) <= as_of:
        raise ValueError("calendar_source_receipt_invalid")
    sessions = [_date(day, "calendar_day") for day in calendar["trading_dates"]]
    if (not sessions or sessions != [row[0] for row in rows if row[1] == "1"]
            or sessions != sorted(set(sessions))
            or len(sessions) > 5000
            or sessions[0] < _date(calendar["coverage_start"], "coverage_start")
            or sessions[-1] > _date(calendar["coverage_end"], "coverage_end")):
        raise ValueError("calendar_coverage_invalid")
    return sessions, hashlib.sha256(raw).hexdigest()


def _read_close(root, day, as_of):
    from .cloud_sector_close import read_packet
    return read_packet(root, day, as_of)


def _slot(root, sessions, index, as_of, reader):
    day = sessions[index]
    result = {"day": day, "status": "missing", "packet_id": None,
              "source": None, "source_contract_id": None, "observed_at": None,
              "source_as_of": None,
              "next_open_at": (datetime.combine(date.fromisoformat(sessions[index + 1]),
                               time(9, 30), SH_TZ).isoformat()
                               if index + 1 < len(sessions) else None),
              "population": None, "identities": {}, "evaluable": set(), "qualified": set()}
    if as_of < datetime.combine(date.fromisoformat(day), time(15), SH_TZ):
        return {**result, "status": "not_matured_as_of"}
    pointer = root / "reports/cloud_sector_close" / day / "latest.json"
    if not pointer.exists():
        return result
    if index + 1 >= len(sessions):
        return {**result, "status": "calendar_successor_missing"}
    try:
        packet, assessment = reader(root, day, as_of)
        observed = _stamp(packet["observed_at"])
        source_as_of = _stamp(assessment["source_as_of"])
        next_open = datetime.combine(date.fromisoformat(sessions[index + 1]), time(9, 30), SH_TZ)
        contract = packet["source_contract"]
        if (not isinstance(contract, dict) or not contract
                or any(not isinstance(k, str) or not isinstance(v, str) or len(v) != 64
                       or set(v) - set("0123456789abcdef") for k, v in contract.items())
                or packet.get("reference_day") not in (None, day)
                or not isinstance(packet.get("packet_id"), str) or not packet["packet_id"]
                or not isinstance(packet["snapshot"].get("source"), str)
                or not packet["snapshot"]["source"].strip()
                or source_as_of.date().isoformat() != day
                or source_as_of.time() < time(15)
                or not source_as_of <= observed <= as_of):
            raise ValueError("sector_label_receipt_invalid")
        if (observed.date().isoformat() != day or observed.time() < time(15)
                or observed >= next_open):
            return {**result, "status": "late_or_wrong_day_capture"}
        selected = _preparation_pool(packet["snapshot"], assessment["rows"])
        selected = [row for row in selected if row["kind"] in {"industry", "concept"}
                    and not _market_cohort(row)]
        population, identities, evaluable, _ = _population(packet["snapshot"], selected, selected[:3])
        qualified = {(row["kind"], row["code"]) for row in selected}
        contract_id = hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":"))
                                     .encode("utf-8")).hexdigest()
        return {**result, "status": "verified", "packet_id": packet["packet_id"],
                "source": packet["snapshot"]["source"], "source_contract_id": contract_id,
                "observed_at": observed.isoformat(), "source_as_of": source_as_of.isoformat(),
                "population": population,
                "identities": identities, "evaluable": evaluable, "qualified": qualified}
    except (OSError, ValueError, KeyError, TypeError, ImportError, AttributeError):
        return {**result, "status": "invalid_receipt"}


def _outcome(origin, path, key, name):
    verified_target = path[-1] if path[-1]["status"] == "verified" else None
    for target in path:
        if target["status"] != "verified":
            return None, f"intervening_{target['status']}", verified_target
        if target["source"] != origin["source"]:
            return None, "source_changed", verified_target
        if target["source_contract_id"] != origin["source_contract_id"]:
            return None, "source_protocol_changed", verified_target
        if target["identities"].get(key) != name:
            return None, "board_identity_changed_or_absent", verified_target
        if key not in target["evaluable"]:
            return None, "board_not_evaluable", verified_target
    target = path[-1]
    return int(key in target["qualified"]), None, target


def build(*, root=ROOT, start_day, end_day, evaluation_as_of, reader=None):
    """Build a bounded, complete-denominator label ledger; never infer prices.

    ``reader`` is for synthetic checks. Production uses the manifest/hash/source
    verifying ``cloud_sector_close.read_packet`` and fails closed on old code.
    """
    root = Path(root)
    as_of = _stamp(evaluation_as_of)
    start, end = _date(start_day, "start_day"), _date(end_day, "end_day")
    result = {"schema": SCHEMA, "status": "waiting_calendar", "start_day": start,
              "end_day": end, "evaluation_as_of": as_of.isoformat(),
              "horizons": list(HORIZONS), "rows": [], "origin_days": [],
              "counts": {}, "calendar_sha256": None, "input_sha256": None, **BOUNDARY}
    try:
        sessions, calendar_sha = _calendar(root, as_of)
        start_index, end_index = sessions.index(start), sessions.index(end)
        if start_index > end_index or end_index - start_index + 1 > MAX_ORIGINS:
            raise ValueError("origin_range_invalid_or_too_large")
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return {**result, "summary": "审计日历或研究区间不可用，不猜交易日。"}
    last = min(len(sessions) - 1, end_index + max(HORIZONS))
    loaded = {index: _slot(root, sessions, index, as_of, reader or _read_close)
              for index in range(start_index, last + 1)}
    estimated_rows = sum(len(loaded[i]["evaluable"]) * len(HORIZONS)
                         for i in range(start_index, end_index + 1))
    if estimated_rows > MAX_LABEL_ROWS:
        return {**result, "status": "batch_too_large", "estimated_rows": estimated_rows,
                "calendar_sha256": calendar_sha,
                "summary": "本批标签行数超限；按更短交易日区间分批，不能截断失败母体。"}
    def empty_count():
        return {"attempted": 0, "labelled": 0, "positive": 0,
                "unknown": 0, "reasons": {}}
    counts = {str(h): {kind: {**empty_count(), "origin_qualified": 0,
                            "by_origin_qualification": {
                                "qualified": empty_count(), "not_qualified": empty_count()}}
                       for kind in ("industry", "concept")}
              for h in HORIZONS}
    rows, days = [], []
    for index in range(start_index, end_index + 1):
        origin = loaded[index]
        days.append({"day": origin["day"], "status": origin["status"],
                     "packet_id": origin["packet_id"], "population": origin["population"],
                     "source_as_of": origin["source_as_of"],
                     "observed_at": origin["observed_at"],
                     "next_open_at": origin["next_open_at"],
                     "source": origin["source"],
                     "source_contract_id": origin["source_contract_id"]})
        if origin["status"] != "verified":
            continue
        for key in sorted(origin["evaluable"]):
            kind, code = key
            name = origin["identities"][key]
            is_qualified = key in origin["qualified"]
            for horizon in HORIZONS:
                counter = counts[str(horizon)][kind]
                subgroup = counter["by_origin_qualification"][
                    "qualified" if is_qualified else "not_qualified"]
                for bucket in (counter, subgroup):
                    bucket["attempted"] += 1
                counter["origin_qualified"] += int(is_qualified)
                target_index = index + horizon
                if target_index > last:
                    label, reason, target = None, "future_calendar_missing", None
                else:
                    label, reason, target = _outcome(origin,
                        [loaded[i] for i in range(index + 1, target_index + 1)], key, name)
                if reason:
                    for bucket in (counter, subgroup):
                        bucket["unknown"] += 1
                        bucket["reasons"][reason] = bucket["reasons"].get(reason, 0) + 1
                else:
                    for bucket in (counter, subgroup):
                        bucket["labelled"] += 1
                        bucket["positive"] += label
                rows.append({"origin_day": origin["day"], "origin_packet_id": origin["packet_id"],
                             "kind": kind, "code": code, "name": name,
                             "origin_qualified": is_qualified, "horizon": horizon,
                             "target_day": sessions[target_index] if target_index < len(sessions) else None,
                             "target_packet_id": target["packet_id"] if target else None,
                             "target_observed_at": target["observed_at"] if target else None,
                             "label_available_at": target["observed_at"] if target and reason is None else None,
                             "future_qualified": label, "unknown_reason": reason})
    for group in counts.values():
        for counter in group.values():
            if counter["attempted"] != counter["labelled"] + counter["unknown"]:
                raise ValueError("sector_label_partition_invalid")
            parts = counter["by_origin_qualification"]
            for field in ("attempted", "labelled", "positive", "unknown"):
                if counter[field] != sum(part[field] for part in parts.values()):
                    raise ValueError("sector_label_subgroup_invalid")
            if counter["origin_qualified"] != parts["qualified"]["attempted"]:
                raise ValueError("sector_label_origin_count_invalid")
            if any(part["attempted"] != part["labelled"] + part["unknown"]
                   for part in parts.values()):
                raise ValueError("sector_label_subgroup_partition_invalid")
    identity = {"schema": SCHEMA, "start_day": start, "end_day": end,
                "calendar_sha256": calendar_sha, "calendar": sessions[start_index:last + 1],
                "as_of": as_of.isoformat(),
                "slots": [{"day": loaded[i]["day"], "status": loaded[i]["status"],
                           "packet_id": loaded[i]["packet_id"],
                           "source": loaded[i]["source"],
                           "source_as_of": loaded[i]["source_as_of"],
                           "observed_at": loaded[i]["observed_at"],
                           "source_contract_id": loaded[i]["source_contract_id"]}
                          for i in range(start_index, last + 1)]}
    result.update(status="research_labels_available" if any(
        c["labelled"] for group in counts.values() for c in group.values()) else "insufficient_verified_history",
                  rows=rows, origin_days=days, counts=counts, calendar_sha256=calendar_sha,
                  input_sha256=hashlib.sha256(json.dumps(identity, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")).hexdigest(),
                  summary="仅记录冻结筛选规则的未来再达标，尚未评估预测力、成本或真实主流板块。")
    return result
