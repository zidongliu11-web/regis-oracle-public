"""Bounded after-close sector review. Observations never become route authority."""

from datetime import date, datetime, time
import hashlib
import json

from .cloud_research_worker import ROOT, read_json
from .market_sector_strategy_adapter import SH_TZ
from .next_session_sectors import _market_cohort, _preparation_pool, session_dates

SCHEMA = "sector_rotation_review.v1"
BOUNDARY = {
    "research_only": True,
    "historical_only": True,
    "forecast_validated": False,
    "leader_validated": False,
    "generates_alert": False,
    "changes_trade_authority": False,
}


def _stamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("timezone_required")
    return stamp.astimezone(SH_TZ)


def _read_close(root, day, now):
    from .cloud_sector_close import read_packet
    return read_packet(root, day, now)


def _read_retired_identity(root, day, now):
    """Check a retired packet's receipt hashes/identity, never re-score its tape."""
    from .cloud_sector_close import _load, contract

    packet = _load(root, day, now, allow_retired_source=True)
    if packet["source_contract"] == contract() or packet["error"]:
        raise ValueError("not_a_usable_retired_identity")
    return packet


def _row(raw):
    return {key: raw.get(key) for key in (
        "kind", "code", "name", "change_pct", "advance_coverage_pct",
        "change_rank_pct", "data_as_of", "up_count", "down_count",
    )}


def _population(snapshot, qualifying, displayed):
    """Account for the complete verified supplier tape, not just its winners."""
    from .market_sector_strategy_adapter import _sector_rows

    counts = {kind: {"reported": 0, "evaluable": 0, "qualified": 0, "displayed": 0,
                     "unassessable": 0, "not_qualified": 0}
              for kind in ("industry", "concept")}
    identities = {}
    evaluable = set()
    evaluable_rows = {}
    excluded_cohorts = 0
    for kind, name in (("industry", "industries"), ("concept", "concepts")):
        for raw in snapshot[name]:
            candidate = {**raw, "kind": kind}
            if _market_cohort(candidate):
                excluded_cohorts += 1
                continue
            key = (kind, raw["code"])
            if key in identities or not raw["code"] or not raw["name"]:
                raise ValueError("sector_population_identity_invalid")
            identities[key] = raw["name"]
            counts[kind]["reported"] += 1
    board_rows, _ = _sector_rows(snapshot)
    for row in board_rows:
        if (_market_cohort(row) or row["constituent_count"] < 5
                or row["advance_coverage_pct"] is None):
            continue
        counts[row["kind"]]["evaluable"] += 1
        key = (row["kind"], row["code"])
        evaluable.add(key)
        evaluable_rows[key] = _row(row)
    for group, field in ((qualifying, "qualified"), (displayed, "displayed")):
        for row in group:
            counts[row["kind"]][field] += 1
    for count in counts.values():
        if not 0 <= count["displayed"] <= count["qualified"] <= count["evaluable"] <= count["reported"]:
            raise ValueError("sector_population_counts_invalid")
        count["unassessable"] = count["reported"] - count["evaluable"]
        count["not_qualified"] = count["evaluable"] - count["qualified"]
    return ({"scope": "this_verified_supplier_packet_only", "by_kind": counts,
             "excluded_behavior_cohorts": excluded_cohorts}, identities, evaluable,
            evaluable_rows)


def _contract_id(packet):
    """A source name alone cannot establish a comparable collection contract."""
    contract = packet.get("source_contract")
    if not isinstance(contract, dict) or not contract:
        raise ValueError("sector_source_contract_missing")
    return hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _trajectory(records, current):
    """Five-session states for all ever-qualified boards of this exact source."""
    matched = [slot for slot in records if slot["status"] == "verified"
               and slot["source"] == current["source"]
               and slot["source_contract_id"] == current["source_contract_id"]]
    universe = {(row["kind"], row["code"], row["name"])
                for slot in matched for row in slot["eligible_rows"]}
    current_keys = {(row["kind"], row["code"], row["name"])
                    for row in current["eligible_rows"]}
    ordered = sorted(universe, key=lambda key: (key not in current_keys, key[0], key[1], key[2]))
    output = []
    for kind, code, name in ordered[:500]:
        cells = []
        key = (kind, code)
        for slot in records:
            cell = {"day": slot["day"], "state": None, "change_pct": None,
                    "advance_coverage_pct": None, "change_rank_pct": None,
                    "board_packet_id": slot["board_packet_id"]}
            if slot["status"] != "verified":
                cell["state"] = slot["status"]
            elif (slot["source"] != current["source"]
                  or slot["source_contract_id"] != current["source_contract_id"]):
                cell["state"] = "source_changed"
            elif key not in slot["identities"]:
                cell["state"] = "absent"
            elif slot["identities"][key] != name:
                cell["state"] = "identity_changed"
            elif key not in slot["evaluable"]:
                cell["state"] = "unassessable"
            else:
                qualified = slot["eligible_lookup"].get(key)
                detail = qualified or slot["evaluable_rows"][key]
                cell.update(state="qualified" if qualified else "not_qualified",
                            change_pct=detail["change_pct"],
                            advance_coverage_pct=detail["advance_coverage_pct"],
                            change_rank_pct=detail["change_rank_pct"] if qualified else None)
            cells.append(cell)
        output.append({"kind": kind, "code": code, "name": name,
                       "current_qualified": (kind, code, name) in current_keys,
                       "cells": cells})
    return {"status": "observed", "scope": "same_supplier_contract_ever_qualified_only",
            "days": [slot["day"] for slot in records], "total_rows": len(ordered),
            "shown_rows": len(output), "omitted_rows": len(ordered)-len(output),
            "rows": output}


def _transitions(previous, current):
    """Partition every prior qualifier; missing/renamed is not a failed trend."""
    prior = {(row["kind"], row["code"]): row for row in previous["eligible_rows"]}
    latest = {(row["kind"], row["code"]): row for row in current["eligible_rows"]}
    before, after = previous["identities"], current["identities"]
    by_kind = {kind: {name: 0 for name in (
        "prior_qualified", "current_qualified", "continued_qualified",
        "lost_qualification", "newly_qualified", "prior_identity_unmatched",
        "current_identity_unmatched")}
        for kind in ("industry", "concept")}
    lost_rows = []
    for key, row in prior.items():
        count = by_kind[key[0]]
        count["prior_qualified"] += 1
        if key in latest and latest[key]["name"] == row["name"]:
            count["continued_qualified"] += 1
        elif after.get(key) == row["name"] and key in current["evaluable"]:
            count["lost_qualification"] += 1
            lost_rows.append({"kind": key[0], "code": key[1], "name": row["name"],
                              "reason": "same_identity_no_longer_qualified"})
        else:
            count["prior_identity_unmatched"] += 1
            lost_rows.append({"kind": key[0], "code": key[1], "name": row["name"],
                              "reason": ("same_identity_not_evaluable" if after.get(key) == row["name"]
                                         else "renamed_or_absent_in_latest_tape")})
    for key, row in latest.items():
        count = by_kind[key[0]]
        count["current_qualified"] += 1
        if key in prior and prior[key]["name"] == row["name"]:
            continue
        if before.get(key) == row["name"] and key in previous["evaluable"]:
            count["newly_qualified"] += 1
        else:
            count["current_identity_unmatched"] += 1
    for count in by_kind.values():
        if (count["prior_qualified"] != count["continued_qualified"]
                + count["lost_qualification"] + count["prior_identity_unmatched"]
                or count["current_qualified"] != count["continued_qualified"]
                + count["newly_qualified"] + count["current_identity_unmatched"]):
            raise ValueError("sector_transition_partition_invalid")
    return {"status": "comparable", "by_kind": by_kind,
            "prior_packet_id": previous["board_packet_id"],
            "current_packet_id": current["board_packet_id"],
            "lost_or_unmatched_examples": sorted(lost_rows, key=lambda row: (row["kind"], row["code"]))[:5]}


def build(*, root=ROOT, now=None, reader=None, preparation=None):
    """Use only recorded receipts already available by the following open.

    ``reader`` is injectable for pure synthetic tests. The production reader
    verifies the packet, manifest, source implementation and full board tape.
    """
    now = now or datetime.now(SH_TZ)
    result = {"schema": SCHEMA, "status": "waiting_calendar", "reference_day": None,
              "target_day": None, "sessions": [], "rows": [], "hypotheses": [],
              "window_sessions": 5, "population_audit": None,
              "trajectory_audit": {"status": "unavailable"},
              "transition_audit": {"status": "unavailable"}, **BOUNDARY}
    try:
        reference, _ = session_dates(root, now)
        now = now.astimezone(SH_TZ)
        calendar = read_json(root / "reports/current_market_calendar.json")
        dates = [date.fromisoformat(day).isoformat() for day in calendar["trading_dates"]]
        index = dates.index(reference)
        target = dates[index + 1]
        lookback = dates[max(0, index - 4): index + 1]
        target_open = datetime.combine(date.fromisoformat(target), time(9, 30), SH_TZ)
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return {**result, "summary": "审计交易日历没有完整的已完成日和下一交易日；不猜日期。"}
    result.update(reference_day=reference, target_day=target, status="waiting_sources",
                  hypothesis_window_open=now < target_open)
    records = []
    for day in lookback:
        following = dates[dates.index(day) + 1]
        following_open = datetime.combine(date.fromisoformat(following), time(9, 30), SH_TZ)
        slot = {"day": day, "status": "missing", "rows": [], "eligible_rows": [],
                "eligible_lookup": {}, "identities": {}, "evaluable": set(),
                "evaluable_rows": {}, "board_packet_id": None,
                "observed_at": None, "source_as_of": None, "source": None,
                "source_contract_id": None,
                "population": None}
        pointer = root / "reports/cloud_sector_close" / day / "latest.json"
        if pointer.exists():
            try:
                packet, assessment = (reader or _read_close)(root, day, now)
                observed = _stamp(packet["observed_at"])
                if observed >= following_open:
                    slot["status"] = "late_capture"
                else:
                    pool = _preparation_pool(packet["snapshot"], assessment["rows"])
                    eligible = [_row(row) for row in pool if not _market_cohort(row)]
                    sectors = eligible[:3]
                    population, identities, evaluable, evaluable_rows = _population(
                        packet["snapshot"], eligible, sectors)
                    slot.update(status="verified", rows=sectors,
                                eligible_rows=eligible,
                                eligible_lookup={(row["kind"], row["code"]): row for row in eligible},
                                identities=identities, evaluable=evaluable,
                                evaluable_rows=evaluable_rows,
                                population=population,
                                board_packet_id=packet["packet_id"],
                                observed_at=packet["observed_at"],
                                source_as_of=assessment["source_as_of"],
                                source=packet["snapshot"]["source"],
                                source_contract_id=_contract_id(packet))
            except (OSError, ValueError, KeyError, TypeError, ImportError, AttributeError):
                try:
                    # The current-contract bad observation stays invalid. A
                    # hash-checked retired contract is an identity gap, not data.
                    retired = _read_retired_identity(root, day, now) if reader is None else None
                    if retired is None:
                        raise ValueError("no_retired_reader")
                    observed = _stamp(retired["observed_at"])
                    slot.update(status="late_capture" if observed >= following_open else "source_changed",
                                board_packet_id=retired["packet_id"],
                                observed_at=retired["observed_at"],
                                source_contract_id=_contract_id(retired))
                except (OSError, ValueError, KeyError, TypeError, ImportError, AttributeError):
                    slot["status"] = "invalid_receipt"
        records.append(slot)
    current = records[-1]
    # A missing or invalid latest receipt cannot revive the previous day's list.
    result["sessions"] = [{k: v for k, v in slot.items() if k not in {
        "rows", "eligible_rows", "eligible_lookup", "identities", "evaluable", "evaluable_rows"}} | {
        "sector_count": len(slot["rows"])} for slot in records]
    if current["status"] != "verified":
        result["summary"] = f"{reference} 板块盘后原件{ {'missing':'缺失','late_capture':'在下一交易日开盘后才获取','invalid_receipt':'校验未通过','source_changed':'采集协议已变化，旧包仅身份校验'}.get(current['status'], '不可用')}；不沿用此前强势板块。"
        return result
    result["status"] = "observed" if current["rows"] else "no_matching_observation"
    result.update(source=current["source"], observed_at=current["observed_at"],
                  source_as_of=current["source_as_of"], board_packet_id=current["board_packet_id"],
                  population_audit=current["population"],
                  trajectory_audit=_trajectory(records, current))
    previous = records[-2] if len(records) > 1 else None
    comparable = bool(previous and previous["status"] == "verified"
                      and previous["source"] == current["source"]
                      and previous["source_contract_id"] == current["source_contract_id"])
    if comparable:
        result["transition_audit"] = _transitions(previous, current)
    else:
        result["transition_audit"] = {"status": "unavailable", "reason":
            "previous_receipt_missing_or_changed_source"}
    prep_rows = {}
    if (isinstance(preparation, dict) and preparation.get("observation_mode") == "after_close"
            and preparation.get("reference_day") == reference
            and preparation.get("board_packet_id") == current["board_packet_id"]):
        prep_rows = {(row["kind"], row["code"]): row for row in
                     preparation.get("sector_watch_rows", []) if isinstance(row, dict)
                     and row.get("kind") in {"industry", "concept"} and row.get("code")}
    output = []
    hypotheses = []
    for row in current["rows"]:
        key = (row["kind"], row["code"])
        changed_identity = bool(comparable and key in previous["identities"]
                                and previous["identities"][key] != row["name"])
        same_prior_identity = bool(comparable and previous["identities"].get(key) == row["name"])
        prior_row = next((r for r in previous["eligible_rows"] if
                          (r["kind"], r["code"], r["name"]) == (*key, row["name"])), None) if same_prior_identity else None
        role = ("identity_changed" if changed_identity else "continued_observation" if prior_row
                else "new_to_watch" if same_prior_identity and key in previous["evaluable"] else
                "prior_unassessable" if same_prior_identity else
                "identity_unmatched" if comparable else "history_incomplete")
        delta = round(row["advance_coverage_pct"] - prior_row["advance_coverage_pct"], 3) if prior_row else None
        streak = 1
        if prior_row:
            for older in reversed(records[:-1]):
                if (older["status"] != "verified" or older["source"] != current["source"]
                        or older["source_contract_id"] != current["source_contract_id"]):
                    break
                if not any((r["kind"], r["code"], r["name"]) == (*key, row["name"])
                           for r in older["eligible_rows"]):
                    break
                streak += 1
        detail = prep_rows.get(key) or {}
        metrics = detail.get("constituent_metrics") if detail.get("constituent_status") == "verified_preparation" else None
        member_at = detail.get("constituent_observed_at")
        try:
            source_min = metrics["source_as_of_min"]
            source_max = metrics["source_as_of_max"]
            member_time_valid = bool(detail.get("constituent_packet_id") and member_at
                                     and _stamp(source_min) <= _stamp(source_max)
                                     <= _stamp(member_at) < target_open
                                     and _stamp(current["observed_at"]) <= _stamp(member_at))
        except (KeyError, TypeError, ValueError):
            member_time_valid = False
        if not member_time_valid:
            metrics = None
        core = [{"stock_code": stock.get("stock_code"), "stock_name": stock.get("stock_name")}
                for stock in (metrics.get("core_observations") or [])[:3]
                if isinstance(stock, dict)] if isinstance(metrics, dict) else []
        output.append({**row, "role": role, "consecutive_observed_sessions": streak,
                       "breadth_delta_pp": delta, "member_packet_id": detail.get("constituent_packet_id") if core else None,
                       "member_observed_at": member_at if core else None,
                       "member_source_as_of_min": source_min if core else None,
                       "member_source_as_of_max": source_max if core else None,
                       "core_observations": core,
                       "core_scope": "实际获取时点的成交额核心观察；历史成员与龙头资格均未核验。"})
        if role in {"continued_observation", "new_to_watch"} and now < target_open:
            scenario = ("延续但广度回落，次日优先核退潮" if prior_row and delta < 0 else
                        "延续观察，次日核广度和市场状态" if prior_row else
                        "新进入达标观察，次日核是否只是短暂轮动")
            hypotheses.append({"kind": row["kind"], "code": row["code"], "name": row["name"],
                               "scenario": scenario, "status": "unvalidated_condition",
                               "confirm": "下一交易日同槽行情、扩散和市场情绪重新核验；低迷时不新增交易。",
                               "invalidate": "未再次通过完整来源与既有板块规则，或来源缺失时撤下假设。"})
    result.update(rows=output, hypotheses=hypotheses,
                  comparison_status="comparable" if comparable else "insufficient_or_changed_source",
                  leader_status="unverified_historical_membership",
                  summary=("本日没有达标行业/供应商概念观察；不等于市场没有机会。" if not output else
                           "跨日只记录连续、新达标或身份/覆盖不可比；供应商概念的题材属性待核，条件推演尚未通过 1–5 日样本外验证。"))
    return result
