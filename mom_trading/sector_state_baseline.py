"""Research-only, common-origin test of market state added to board persistence.

The target is the frozen sector screen qualifying again, not return or a buy.
Inputs are the verified sector label ledger and point-in-time market state rows.
No file is written and no route, factor or strategy admission is changed here.
"""

from __future__ import annotations

from datetime import date, datetime, time
import heapq
import hashlib
import json
import math
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .emotion_horizon_research import STATES, validate_input as validate_market_row


SCHEMA = "sector_state_common_origin_baseline.v1"
LEDGER_SCHEMA = "sector_forward_qualification_ledger.v1"
HORIZONS = (1, 3, 5)
KINDS = ("industry", "concept")
MIN_TRAIN_DAYS = 30
MIN_CELL_DAYS = 20
MIN_SCORE_DAYS = 20
MAX_ROWS = 100_000
MAX_BATCHES = 4
SHANGHAI = ZoneInfo("Asia/Shanghai")
BOUNDARY = {"research_only": True, "forecast_validated": False,
            "production_eligible": False, "generates_alert": False,
            "returns_computed": False, "leader_validated": False}


def _date(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field}_invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field}_invalid") from error
    if value != parsed.isoformat():
        raise ValueError(f"{field}_invalid")
    return value


def _stamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field}_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field}_invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field}_timezone_required")
    return parsed.astimezone(SHANGHAI)


def _hash(value: Any) -> str:
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    for chunk in encoder.iterencode(value):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def _hex(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and not set(value) - set("0123456789abcdef")


def _ledger(ledger: Mapping[str, Any]) -> tuple[dict[str, dict], list[dict]]:
    if (not isinstance(ledger, Mapping) or ledger.get("schema") != LEDGER_SCHEMA
            or ledger.get("research_only") is not True
            or ledger.get("forecast_validated") is not False
            or ledger.get("production_eligible") is not False
            or ledger.get("generates_alert") is not False
            or ledger.get("returns_computed") is not False
            or ledger.get("leader_validated") is not False):
        raise ValueError("sector_ledger_boundary_invalid")
    if ledger.get("status") not in {"research_labels_available", "insufficient_verified_history",
                                    "waiting_calendar", "batch_too_large"}:
        raise ValueError("sector_ledger_status_invalid")
    if ledger["status"] in {"waiting_calendar", "batch_too_large"}:
        return {}, []
    if not _hex(ledger.get("calendar_sha256")) or not _hex(ledger.get("input_sha256")):
        raise ValueError("sector_ledger_identity_invalid")
    days, rows = ledger.get("origin_days"), ledger.get("rows")
    if (not isinstance(days, list) or not isinstance(rows, list) or len(rows) > MAX_ROWS
            or len(days) > 60):
        raise ValueError("sector_ledger_size_invalid")
    by_day: dict[str, dict] = {}
    previous = None
    for offset, day in enumerate(days):
        if not isinstance(day, dict):
            raise ValueError("sector_origin_invalid")
        key = _date(day.get("day"), "sector_origin_day")
        if key in by_day or (previous and key <= previous):
            raise ValueError("sector_origin_order_invalid")
        next_open = (_stamp(day["next_open_at"], "sector_next_open_at")
                     if day.get("next_open_at") is not None else None)
        if (next_open is None and (day.get("status") != "calendar_successor_missing"
                                   or offset != len(days) - 1)):
            raise ValueError("sector_origin_successor_invalid")
        if ((next_open and (next_open.date().isoformat() <= key
                            or next_open.time() != time(9, 30)))
                or (previous and (by_day[previous].get("next_open_at") is None
                     or _stamp(by_day[previous]["next_open_at"],
                               "sector_previous_next_open_at").date().isoformat() != key))):
            raise ValueError("sector_origin_calendar_gap")
        if day.get("status") == "verified":
            observed = _stamp(day.get("observed_at"), "sector_observed_at")
            if (observed.date().isoformat() != key or observed.time() < time(15)
                    or next_open is None or not observed < next_open
                    or not day.get("packet_id") or not day.get("source")
                    or not _hex(day.get("source_contract_id"))):
                raise ValueError("sector_origin_receipt_invalid")
            population = day.get("population")
            if (not isinstance(population, dict)
                    or population.get("scope") != "this_verified_supplier_packet_only"
                    or not isinstance(population.get("by_kind"), dict)):
                raise ValueError("sector_population_missing")
            for kind in KINDS:
                count = population["by_kind"].get(kind)
                if not isinstance(count, dict) or any(type(count.get(field)) is not int
                    for field in ("reported", "evaluable", "qualified", "displayed",
                                  "unassessable", "not_qualified")):
                    raise ValueError("sector_population_counts_invalid")
                if (not 0 <= count["displayed"] <= count["qualified"]
                        <= count["evaluable"] <= count["reported"]
                        or count["unassessable"] != count["reported"] - count["evaluable"]
                        or count["not_qualified"] != count["evaluable"] - count["qualified"]):
                    raise ValueError("sector_population_partition_invalid")
        by_day[key] = day
        previous = key
    seen = set()
    actual = {str(h): {kind: {"attempted": 0, "labelled": 0, "positive": 0,
                                     "unknown": 0, "reasons": {}, "origin_qualified": 0,
                                     "by_origin_qualification": {label: {
                                         "attempted": 0, "labelled": 0, "positive": 0,
                                         "unknown": 0, "reasons": {}}
                                         for label in ("qualified", "not_qualified")}}
                       for kind in KINDS} for h in HORIZONS}
    daily_counts: dict[tuple[str, int, str], list[int]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("sector_label_row_invalid")
        key = (_date(row.get("origin_day"), "sector_label_day"), row.get("horizon"),
               row.get("kind"), row.get("code"))
        origin = by_day.get(key[0])
        if (key in seen or key[1] not in HORIZONS or key[2] not in KINDS
                or not isinstance(key[3], str) or not key[3]
                or not isinstance(row.get("name"), str) or not row["name"]
                or not origin or origin.get("status") != "verified"
                or row.get("origin_packet_id") != origin.get("packet_id")
                or type(row.get("origin_qualified")) is not bool):
            raise ValueError("sector_label_identity_invalid")
        seen.add(key)
        daily = daily_counts.setdefault((key[0], key[1], key[2]), [0, 0])
        daily[0] += 1
        daily[1] += int(row["origin_qualified"])
        counter = actual[str(key[1])][key[2]]
        subgroup = counter["by_origin_qualification"][
            "qualified" if row["origin_qualified"] else "not_qualified"]
        counter["origin_qualified"] += int(row["origin_qualified"])
        for bucket in (counter, subgroup):
            bucket["attempted"] += 1
        outcome = row.get("future_qualified")
        if outcome is None:
            if not row.get("unknown_reason") or row.get("label_available_at") is not None:
                raise ValueError("sector_unknown_label_invalid")
            for bucket in (counter, subgroup):
                bucket["unknown"] += 1
                reason = row["unknown_reason"]
                bucket["reasons"][reason] = bucket["reasons"].get(reason, 0) + 1
        elif (type(outcome) is not int or outcome not in (0, 1)
              or row.get("unknown_reason") is not None
              or not row.get("target_packet_id")
              or _date(row.get("target_day"), "sector_target_day") <= key[0]
              or row.get("label_available_at") != row.get("target_observed_at")
              or _stamp(row.get("target_observed_at"), "sector_target_observed_at")
              .date().isoformat() != row["target_day"]
              or _stamp(row.get("label_available_at"), "sector_label_available_at")
              <= _stamp(origin["observed_at"], "sector_origin_observed_at")):
            raise ValueError("sector_known_label_invalid")
        else:
            for bucket in (counter, subgroup):
                bucket["labelled"] += 1
                bucket["positive"] += outcome
    for day, origin in by_day.items():
        for h in HORIZONS:
            for kind in KINDS:
                got = daily_counts.get((day, h, kind), [0, 0])
                population = (origin["population"]["by_kind"][kind]
                              if origin.get("status") == "verified" else None)
                expected = ([population["evaluable"], population["qualified"]]
                            if population else [0, 0])
                if got != expected:
                    raise ValueError("sector_population_rows_incomplete")
    if ledger.get("counts") != actual:
        raise ValueError("sector_ledger_counts_mismatch")
    return by_day, rows


def _ledgers(value: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> tuple[dict[str, dict], list[dict], list[dict]]:
    batches = [value] if isinstance(value, Mapping) else value
    if not isinstance(batches, (list, tuple)) or not 1 <= len(batches) <= MAX_BATCHES:
        raise ValueError("sector_ledger_batch_count_invalid")
    origins: dict[str, dict] = {}
    rows: list[dict] = []
    prior_last = None
    for batch in batches:
        days, labels = _ledger(batch)
        if len(batches) > 1 and batch["status"] in {"waiting_calendar", "batch_too_large"}:
            raise ValueError("sector_ledger_batch_unavailable")
        if len(batches) > 1 and not days:
            raise ValueError("sector_ledger_batch_empty")
        if days:
            first = next(iter(days))
            if (first in origins or (prior_last is not None
                    and (origins[prior_last].get("next_open_at") is None
                         or _stamp(origins[prior_last]["next_open_at"], "sector_batch_next_open_at")
                         .date().isoformat() != first))):
                raise ValueError("sector_ledger_batches_not_contiguous")
            for day in days:
                if day in origins:
                    raise ValueError("sector_ledger_batches_overlap")
            origins.update(days)
            prior_last = next(reversed(days))
        rows.extend(labels)
    if len(rows) > MAX_ROWS * MAX_BATCHES:
        raise ValueError("sector_ledger_combined_size_invalid")
    positions = {day: index for index, day in enumerate(origins)}
    for row in rows:
        if row["future_qualified"] is None:
            continue
        target = row["target_day"]
        if target in positions:
            target_origin = origins[target]
            if (positions[target] - positions[row["origin_day"]] != row["horizon"]
                    or target_origin.get("status") != "verified"
                    or target_origin.get("packet_id") != row["target_packet_id"]
                    or target_origin.get("observed_at") != row["target_observed_at"]):
                raise ValueError("sector_target_identity_invalid")
        elif origins and target <= next(reversed(origins)):
            raise ValueError("sector_target_calendar_gap")
    return origins, rows, list(batches)


def _market(rows: Sequence[Mapping[str, Any]], days: Mapping[str, dict]) -> dict[str, dict]:
    if not isinstance(rows, (list, tuple)) or len(rows) > 60 * MAX_BATCHES:
        raise ValueError("market_rows_size_invalid")
    result: dict[str, dict] = {}
    identity = None
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ValueError("market_row_invalid")
        day = _date(raw.get("trade_date"), "market_trade_date")
        if day not in days or day in result:
            raise ValueError("market_day_not_in_sector_ledger_or_duplicate")
        normalized = validate_market_row([raw], [day])[0]
        if identity is not None and normalized["identity"] != identity:
            raise ValueError("market_scope_or_version_changed")
        identity = normalized["identity"]
        result[day] = normalized
    return result


def _market_at(day: str, market: Mapping[str, dict], origins: Mapping[str, dict],
               cutoff: datetime | None = None) -> str | None:
    observation, origin = market.get(day), origins.get(day)
    if not observation or not origin or origin.get("status") != "verified":
        return None
    next_open = _stamp(origin["next_open_at"], "sector_next_open_at")
    return observation["state"] if (observation["state"] in STATES
        and observation["state_available_at"] <= next_open
        and observation["decision_at"] <= next_open
        and (cutoff is None or (observation["state_available_at"] <= cutoff
                                and observation["decision_at"] <= cutoff))) else None


def _bucket(aggregates: dict, key: tuple) -> dict:
    return aggregates.setdefault(key, {"count": 0, "positive": 0, "days": set()})


def _probability(bucket: dict) -> float:
    return (1 + bucket["positive"]) / (2 + bucket["count"])


def _score(probability: float, outcome: int) -> tuple[float, float]:
    return ((probability - outcome) ** 2,
            -(outcome * math.log(probability) + (1 - outcome) * math.log(1 - probability)))


def _score_input_hash(origins: Mapping[str, dict], rows: Sequence[dict],
                      market: Mapping[str, dict], as_of: datetime,
                      start: str, frozen: datetime) -> str:
    visible_origins = {}
    verified_at_cutoff = set()
    for day, origin in origins.items():
        if _stamp(day + "T15:00:00+08:00", "sector_day_close") > as_of:
            continue
        if (origin.get("status") == "verified"
                and _stamp(origin["observed_at"], "sector_observed_at") <= as_of):
            visible_origins[day] = origin
            verified_at_cutoff.add(day)
        else:
            # A late, invalid or missing slot may only be classified later.
            # Its actual future status belongs in supplied_snapshot_sha256.
            visible_origins[day] = {"day": day, "status": "no_verified_packet_by_cutoff"}
    visible_rows = []
    for row in rows:
        if row["origin_day"] not in verified_at_cutoff:
            continue
        basic = {key: row.get(key) for key in ("origin_day", "origin_packet_id",
                 "kind", "code", "name", "origin_qualified", "horizon", "target_day")}
        available = row.get("label_available_at")
        if available and _stamp(available, "sector_label_available_at") <= as_of:
            basic.update(future_qualified=row["future_qualified"],
                         label_available_at=available,
                         target_packet_id=row["target_packet_id"])
        visible_rows.append(basic)
    visible_market = [{"day": day, "state": row["state"],
                       "available_at": row["state_available_at"].isoformat(),
                       "decision_at": row["decision_at"].isoformat(),
                       "source_sha256": row["source_snapshot_sha256"],
                       "universe_sha256": row["universe_snapshot_sha256"],
                       "identity": row["identity"]}
                      for day, row in sorted(market.items())
                      if row["state_available_at"] <= as_of and row["decision_at"] <= as_of]
    return _hash({"start": start, "frozen": frozen.isoformat(), "as_of": as_of.isoformat(),
                  "origins": visible_origins, "rows": visible_rows, "market": visible_market})


def evaluate(ledger: Mapping[str, Any] | Sequence[Mapping[str, Any]],
             market_rows: Sequence[Mapping[str, Any]], *,
             evaluation_start: str, protocol_frozen_at: str,
             evaluation_as_of: str,
             score_origin_days: Sequence[str] | None = None) -> dict[str, Any]:
    """Score the incremental market-state condition on the same board/day sample.

    Market rows must be bound to real producer receipts by the caller. The
    score is only an historical diagnostic unless a separately frozen forward
    issuance and independent validation process exists.
    """
    start = _date(evaluation_start, "evaluation_start")
    frozen = _stamp(protocol_frozen_at, "protocol_frozen_at")
    as_of = _stamp(evaluation_as_of, "evaluation_as_of")
    origins, rows, batches = _ledgers(ledger)
    selected_days = None
    if score_origin_days is not None:
        if (not isinstance(score_origin_days, (list, tuple))
                or len(score_origin_days) > len(origins)
                or any(_date(day, "score_origin_day") not in origins for day in score_origin_days)
                or list(score_origin_days) != sorted(set(score_origin_days))):
            raise ValueError("score_origin_days_invalid")
        selected_days = set(score_origin_days)
    source_status = batches[0]["status"] if len(batches) == 1 else "bounded_contiguous_batches"
    result = {"schema": SCHEMA, "status": "insufficient_verified_history",
              "evaluation_start": start, "protocol_frozen_at": frozen.isoformat(),
              "evaluation_as_of": as_of.isoformat(), "source_status": source_status,
              "supplied_ledger_ids_sha256": _hash([batch.get("input_sha256") for batch in batches]),
              "ledger_batches": len(batches),
              "market_input_sha256": None, "groups": {},
              "score_origin_scope": "caller_selected_research_only" if selected_days is not None
                                    else "historical_diagnostic_all_eligible",
              "score_origin_days_sha256": _hash(score_origin_days) if selected_days is not None else None,
              "support_contract": {"train_days": MIN_TRAIN_DAYS,
                                   "cell_days": MIN_CELL_DAYS,
                                   "score_days": MIN_SCORE_DAYS}, **BOUNDARY}
    if source_status in {"waiting_calendar", "batch_too_large"}:
        result["status"] = source_status
        return result
    market = _market(market_rows, origins)
    if not market:
        result["status"] = "market_history_missing"
        return result
    result["market_input_sha256"] = _hash([{
        "day": day, "state": row["state"], "available_at": row["state_available_at"].isoformat(),
        "decision_at": row["decision_at"].isoformat(),
        "source_sha256": row["source_snapshot_sha256"],
        "universe_sha256": row["universe_snapshot_sha256"],
        "identity": row["identity"]} for day, row in sorted(market.items())
        if row["state_available_at"] <= as_of and row["decision_at"] <= as_of])
    visible_scope = next((row["identity"] for row in market.values()
                          if row["decision_at"] <= as_of), None)
    result["market_scope_identity"] = list(visible_scope) if visible_scope else None
    result["source_authentication"] = "caller_must_verify_ledger_and_market_receipts"
    result["score_hash_scope"] = "verified_observations_by_cutoff_unverified_slots_normalized_not_publication_proof"
    eligible_days = [day for day in origins if day >= start]
    score_days = [day for day in eligible_days if selected_days is None or day in selected_days]
    if not score_days:
        result["status"] = "evaluation_window_empty"
        return result
    market_state_by_day = {day: _market_at(day, market, origins) for day in origins}
    for horizon in HORIZONS:
        for kind in KINDS:
            group_rows = [row for row in rows if row["horizon"] == horizon and row["kind"] == kind]
            by_day: dict[str, list[dict]] = {}
            for row in group_rows:
                by_day.setdefault(row["origin_day"], []).append(row)
            skipped: dict[str, int] = {}
            day_scores = []
            outcomes_seen = 0
            unknown = 0
            unknown_reasons: dict[str, int] = {}
            known_total = 0
            pending = []
            sequence = 0
            aggregates: dict[tuple, dict] = {}
            previous_day = None
            for day in origins:
                current_origin = origins[day]
                current_open = (_stamp(current_origin["next_open_at"], "sector_next_open_at")
                                if current_origin.get("next_open_at") is not None else None)
                if previous_day is not None and market_state_by_day[previous_day] is not None:
                    for prior in by_day.get(previous_day, []):
                        if prior["future_qualified"] is not None:
                            sequence += 1
                            heapq.heappush(pending, (
                                _stamp(prior["label_available_at"], "sector_label_available_at"),
                                sequence, prior))
                while pending and current_open is not None and pending[0][0] <= current_open:
                    _, _, prior = heapq.heappop(pending)
                    prior_origin = origins[prior["origin_day"]]
                    source_key = (prior_origin["source"], prior_origin["source_contract_id"])
                    qualification = prior["origin_qualified"]
                    state_key = market_state_by_day[prior["origin_day"]]
                    for aggregate_key in ((*source_key, "all"),
                                          (*source_key, "qualification", qualification),
                                          (*source_key, "state_qualification", state_key, qualification)):
                        bucket = _bucket(aggregates, aggregate_key)
                        bucket["count"] += 1
                        bucket["positive"] += prior["future_qualified"]
                        bucket["days"].add(prior["origin_day"])
                previous_day = day
                if day < start:
                    continue
                if selected_days is not None and day not in selected_days:
                    skipped["not_in_frozen_score_origin_set"] = skipped.get("not_in_frozen_score_origin_set", 0) + 1
                    continue
                candidates = by_day.get(day, [])
                unknown += sum(row["future_qualified"] is None for row in candidates)
                known_total += sum(row["future_qualified"] is not None for row in candidates)
                for row in candidates:
                    if row["future_qualified"] is None:
                        reason = row["unknown_reason"]
                        unknown_reasons[reason] = unknown_reasons.get(reason, 0) + 1
                if current_origin.get("status") != "verified":
                    skipped["sector_origin_unverified"] = skipped.get("sector_origin_unverified", 0) + 1
                    continue
                if current_open > as_of:
                    skipped["origin_not_available_at_evaluation"] = skipped.get("origin_not_available_at_evaluation", 0) + 1
                    continue
                state = market_state_by_day[day]
                if state is None:
                    skipped["market_state_missing_or_late"] = skipped.get("market_state_missing_or_late", 0) + 1
                    continue
                known = [row for row in candidates if row["future_qualified"] is not None
                         and _stamp(row["label_available_at"], "sector_label_available_at") <= as_of]
                outcomes_seen += len(known)
                if not known:
                    skipped["labels_missing_or_unmatured"] = skipped.get("labels_missing_or_unmatured", 0) + 1
                    continue
                # Matured labels enter once, after their source day. All three
                # models use one source/protocol and market-complete cohort.
                source_key = (current_origin["source"], current_origin["source_contract_id"])
                all_history = _bucket(aggregates, (*source_key, "all"))
                if len(all_history["days"]) < MIN_TRAIN_DAYS:
                    skipped["insufficient_mature_training_days"] = skipped.get("insufficient_mature_training_days", 0) + 1
                    continue
                probabilities = {}
                for qualification in (False, True):
                    q_history = _bucket(aggregates, (*source_key, "qualification", qualification))
                    state_history = _bucket(aggregates, (*source_key, "state_qualification", state, qualification))
                    if (len(q_history["days"]) >= MIN_CELL_DAYS
                            and len(state_history["days"]) >= MIN_CELL_DAYS):
                        probabilities[qualification] = {
                            "unconditional": _probability(all_history),
                            "qualification": _probability(q_history),
                            "market_state_qualification": _probability(state_history),
                        }
                scores = {name: [] for name in ("unconditional", "qualification", "market_state_qualification")}
                for row in known:
                    qualification = row["origin_qualified"]
                    if qualification not in probabilities:
                        continue
                    for name, probability in probabilities[qualification].items():
                        scores[name].append(_score(probability, row["future_qualified"]))
                matched = len(scores["market_state_qualification"])
                if matched == 0:
                    skipped["insufficient_state_cell_days"] = skipped.get("insufficient_state_cell_days", 0) + 1
                    continue
                if any(len(values) != matched for values in scores.values()):
                    raise ValueError("sector_common_sample_mismatch")
                day_scores.append({"origin_day": day, "market_state": state,
                    "origin_packet_id": current_origin["packet_id"],
                    "scored_boards": matched, "available_outcomes": len(known),
                    "mean_brier": {name: sum(value[0] for value in values) / matched
                                   for name, values in scores.items()},
                    "mean_log_loss": {name: sum(value[1] for value in values) / matched
                                      for name, values in scores.items()}})
            supported = len(day_scores) >= MIN_SCORE_DAYS
            result["groups"][f"{horizon}:{kind}"] = {
                "status": "research_diagnostic" if supported else "insufficient_scored_origin_days",
                "attempted_origin_days": len(score_days),
                "market_state_days": sum(
                    origins[day].get("status") == "verified"
                    and _stamp(origins[day]["next_open_at"], "sector_next_open_at") <= as_of
                    and market_state_by_day[day] is not None
                    for day in score_days),
                "known_label_rows": known_total,
                "known_labels_with_market_at_evaluation": outcomes_seen,
                "unknown_label_rows": unknown, "unknown_reason_counts": unknown_reasons,
                "skipped_origin_days": skipped,
                "common_origin_days": len(day_scores),
                "common_board_rows": sum(item["scored_boards"] for item in day_scores),
                "scores_equal_origin_day": {
                    metric: {name: sum(item[metric][name] for item in day_scores) / len(day_scores)
                             for name in ("unconditional", "qualification", "market_state_qualification")}
                    for metric in ("mean_brier", "mean_log_loss")
                } if supported else None,
                "per_origin": day_scores if supported else [],
            }
    result["status"] = "research_diagnostic" if any(
        group["status"] == "research_diagnostic" for group in result["groups"].values()
    ) else "insufficient_verified_history"
    result["chronology"] = ("historical_diagnostic_only" if any(
        _stamp(origins[day]["next_open_at"], "sector_next_open_at") < frozen
        for day in score_days if origins[day].get("status") == "verified")
        else "post_freeze_candidate_not_admitted")
    original_score_identity = _score_input_hash(origins, rows, market, as_of, start, frozen)
    result["score_input_sha256"] = (_hash({"original": original_score_identity,
                                           "score_origin_days": list(score_origin_days)})
                                     if selected_days is not None else original_score_identity)
    supplied_identity = {"ledger": batches, "market": list(market_rows),
                         "evaluation_start": start, "frozen": frozen.isoformat(),
                         "as_of": as_of.isoformat()}
    if selected_days is not None:
        supplied_identity["score_origin_days"] = list(score_origin_days)
    result["supplied_snapshot_sha256"] = _hash(supplied_identity)
    return result


__all__ = ["evaluate", "SCHEMA", "MIN_TRAIN_DAYS", "MIN_CELL_DAYS", "MIN_SCORE_DAYS"]
