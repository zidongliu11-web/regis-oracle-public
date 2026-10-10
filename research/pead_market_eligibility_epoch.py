"""Freeze causal PEAD entry eligibility before any holding outcome is computed."""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
FORMULA_MANIFEST_PATH = ROOT / "research/data/pead_unexpected_earnings_epoch_v1/manifest.json"
FORMULA_AUDIT_PATH = ROOT / "research/PEAD_UNEXPECTED_EARNINGS_EPOCH_V1_OUTPUT_AUDIT.json"
AMENDMENT_PATH = ROOT / "research/PEAD_ACCOUNTING_UNDERREACTION_LONG_ONLY_V1_PREREGISTRATION_AMENDMENT_2.json"
PORTFOLIO_AMENDMENT_PATH = (
    ROOT / "research/PEAD_ACCOUNTING_UNDERREACTION_LONG_ONLY_V1_PREREGISTRATION_AMENDMENT_3.json"
)
REPLAY_AMENDMENT_PATH = (
    ROOT / "research/PEAD_ACCOUNTING_UNDERREACTION_LONG_ONLY_V1_PREREGISTRATION_AMENDMENT_4.json"
)
VALUATION_AMENDMENT_PATH = (
    ROOT / "research/PEAD_ACCOUNTING_UNDERREACTION_LONG_ONLY_V1_PREREGISTRATION_AMENDMENT_5.json"
)
EVALUATION_AMENDMENT_PATH = (
    ROOT / "research/PEAD_ACCOUNTING_UNDERREACTION_LONG_ONLY_V1_PREREGISTRATION_AMENDMENT_6.json"
)
OUTPUT_DIR = ROOT / "research/data/pead_market_eligibility_epoch_v1"
EVENTS_PATH = OUTPUT_DIR / "event_eligibility.csv"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"

MIN_MEDIAN_TURNOVER = 50_000_000.0
WINDOW_SESSIONS = 252
EVALUATION_START = pd.Timestamp("2018-01-01")
EVALUATION_END = pd.Timestamp("2025-12-31")
MAIN_PREFIXES = ("000", "001", "002", "003", "600", "601", "603", "605")
CHINEXT_PREFIXES = ("300", "301")
OUTPUT_FIELDS = (
    "issuer_code", "stat_date", "pub_datetime", "announcement_id",
    "unexpected_earnings", "entry_session", "entry_session_index", "stable_id",
    "alias", "raw_open", "adjusted_open", "lagged_median_turnover_yuan",
    "lagged_p90_unexpected_earnings", "threshold_population_n", "threshold_rank",
    "market_state_observation_date", "confirmed_state", "raw_state", "routed_state",
    "screen_status", "threshold_eligible", "primary_route_eligible",
)


class EligibilityError(ValueError):
    """A frozen causal input cannot be interpreted without guessing."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _bound_path(binding: Mapping[str, Any]) -> Path:
    raw = Path(str(binding.get("path") or ""))
    path = raw if raw.is_absolute() else ROOT / raw
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT.resolve()):
        raise EligibilityError("binding escapes the project root")
    if not resolved.is_file() or sha256(resolved) != binding.get("sha256"):
        raise EligibilityError(f"binding mismatch: {raw}")
    return resolved


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise EligibilityError(f"invalid unexpected earnings: {value!r}") from exc
    if not result.is_finite():
        raise EligibilityError(f"non-finite unexpected earnings: {value!r}")
    return result


def _finite(value: Any, *, positive: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or (positive and result <= 0):
        return None
    return result


def board(code: str) -> str | None:
    code = str(code)
    if len(code) != 6 or not code.isdigit():
        return None
    if code.startswith(MAIN_PREFIXES):
        return "main"
    if code.startswith(CHINEXT_PREFIXES):
        return "chinext"
    return None


def route_state(confirmed: str, raw: str) -> str:
    confirmed, raw = str(confirmed or "UNKNOWN"), str(raw or "UNKNOWN")
    if confirmed in {"RISK_ON", "OVERHEATED"} and raw in {"STABILIZING", "RISK_OFF"}:
        return "retreat"
    return {
        "RISK_OFF": "risk_closed",
        "STABILIZING": "weak_repair",
        "RISK_ON": "sentiment_rising",
        "OVERHEATED": "crowded_climax",
        "UNKNOWN": "unknown",
    }.get(confirmed, "unknown")


class _Fenwick:
    def __init__(self, size: int):
        self.values = [0] * (size + 1)

    def add(self, index: int, delta: int) -> None:
        index += 1
        while index < len(self.values):
            self.values[index] += delta
            index += index & -index

    def kth(self, ordinal: int) -> int:
        if ordinal < 1:
            raise EligibilityError("Fenwick ordinal must be positive")
        index = 0
        bit = 1 << (len(self.values).bit_length() - 1)
        while bit:
            candidate = index + bit
            if candidate < len(self.values) and self.values[candidate] < ordinal:
                ordinal -= self.values[candidate]
                index = candidate
            bit >>= 1
        return index


def apply_lagged_thresholds(
    rows: list[dict[str, Any]], *, first_formula_entry_index: int,
) -> None:
    """Mutate base-eligible rows with the frozen prior-252-session threshold."""
    base = [row for row in rows if row["screen_status"] == "base_eligible"]
    if not base:
        return
    coordinates = sorted({row["_unexpected_decimal"] for row in base})
    coordinate_index = {value: index for index, value in enumerate(coordinates)}
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in base:
        grouped[int(row["entry_session_index"])].append(row)
    tree = _Fenwick(len(coordinates))
    active: deque[tuple[int, list[int]]] = deque()
    population_n = 0
    for session_index in sorted(grouped):
        while active and active[0][0] < session_index - WINDOW_SESSIONS:
            _, expired = active.popleft()
            for coordinate in expired:
                tree.add(coordinate, -1)
                population_n -= 1
        current = grouped[session_index]
        complete = session_index - WINDOW_SESSIONS >= first_formula_entry_index
        if not complete:
            for row in current:
                row["screen_status"] = "ineligible_threshold_history_incomplete"
        elif population_n == 0:
            for row in current:
                row["screen_status"] = "ineligible_threshold_history_empty"
        else:
            ordinal = math.ceil(0.90 * population_n)
            threshold = coordinates[tree.kth(ordinal)]
            for row in current:
                row["lagged_p90_unexpected_earnings"] = str(threshold)
                row["threshold_population_n"] = str(population_n)
                row["screen_status"] = (
                    "threshold_pass" if row["_unexpected_decimal"] >= threshold
                    else "below_lagged_p90"
                )
        additions: list[int] = []
        for row in current:
            coordinate = coordinate_index[row["_unexpected_decimal"]]
            tree.add(coordinate, 1)
            population_n += 1
            additions.append(coordinate)
        active.append((session_index, additions))


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _load_contract() -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], Path, Path, Path, Path,
]:
    amendment = _json(AMENDMENT_PATH)
    if amendment.get("status") != (
        "market_eligibility_edge_cases_frozen_no_candidate_price_or_outcome_access"
    ) or any((amendment.get("authority") or {}).values()):
        raise EligibilityError("market-eligibility amendment is not fail-closed")
    amendment_1_path = _bound_path(amendment["binding"])
    amendment_1 = _json(amendment_1_path)
    bindings = amendment_1["bindings"]
    price_manifest_path = _bound_path(bindings["outcome_blind_price_source_manifest"])
    execution_manifest_path = _bound_path(
        bindings["outcome_blind_execution_source_manifest"]
    )
    state_path = _bound_path(bindings["historical_market_state_feature_epoch"])
    replay_amendment = _json(REPLAY_AMENDMENT_PATH)
    if (
        replay_amendment.get("status")
        != "replay_fail_closed_edges_and_metric_scopes_frozen_no_candidate_outcome_access"
        or replay_amendment.get("candidate_id") != amendment.get("candidate_id")
        or any((replay_amendment.get("authority") or {}).values())
        or _bound_path(replay_amendment["binding"]) != PORTFOLIO_AMENDMENT_PATH.resolve()
    ):
        raise EligibilityError("PEAD replay contract is not frozen")
    valuation_amendment = _json(VALUATION_AMENDMENT_PATH)
    if (
        valuation_amendment.get("status")
        != "held_inventory_missing_mark_semantics_frozen_no_candidate_outcome_access"
        or valuation_amendment.get("candidate_id") != amendment.get("candidate_id")
        or any((valuation_amendment.get("authority") or {}).values())
        or _bound_path(valuation_amendment["binding"]) != REPLAY_AMENDMENT_PATH.resolve()
    ):
        raise EligibilityError("PEAD valuation contract is not frozen")
    evaluation_amendment = _json(EVALUATION_AMENDMENT_PATH)
    if (
        evaluation_amendment.get("status")
        != "first_public_report_identity_and_revision_validation_frozen_no_candidate_outcome_access"
        or evaluation_amendment.get("candidate_id") != amendment.get("candidate_id")
        or any((evaluation_amendment.get("authority") or {}).values())
        or _bound_path(evaluation_amendment["binding"])
        != VALUATION_AMENDMENT_PATH.resolve()
    ):
        raise EligibilityError("PEAD latest evaluation contract is not frozen")
    formula_manifest = _json(FORMULA_MANIFEST_PATH)
    if formula_manifest.get("status") != (
        "unexpected_earnings_epoch_ready_for_market_eligibility_freeze"
    ) or formula_manifest.get("candidate_id") != amendment.get("candidate_id"):
        raise EligibilityError("formula epoch is not ready for market eligibility")
    formula_audit = _json(FORMULA_AUDIT_PATH)
    if not (
        formula_audit.get("status") == "unexpected_earnings_epoch_independently_verified"
        and formula_audit.get("ready_for_market_eligibility_freeze") is True
        and formula_audit.get("prices_or_returns_read") is False
        and (formula_audit.get("manifest") or {}).get("path")
        == str(FORMULA_MANIFEST_PATH.relative_to(ROOT))
        and (formula_audit.get("manifest") or {}).get("sha256")
        == sha256(FORMULA_MANIFEST_PATH)
    ):
        raise EligibilityError("formula epoch is not independently verified")
    formula_path = _bound_path({
        "path": formula_manifest["unexpected_earnings_values"],
        "sha256": formula_manifest["unexpected_earnings_values_sha256"],
    })
    price_manifest = _json(price_manifest_path)
    execution_manifest = _json(execution_manifest_path)
    if price_manifest.get("status") != "candidate_monthly_input_frozen_no_outcomes":
        raise EligibilityError("price source manifest is not outcome-blind")
    if execution_manifest.get("status") != "execution_addon_content_addressed_no_returns":
        raise EligibilityError("execution source manifest is not outcome-blind")
    price_path = _bound_path(price_manifest["output"])
    execution_path = _bound_path(execution_manifest["output"])
    return (
        formula_manifest, amendment, evaluation_amendment, formula_path,
        price_path, execution_path, state_path,
    )


def _market_panel(price_path: Path, execution_path: Path) -> tuple[pd.DataFrame, pd.DatetimeIndex]:
    daily_columns = [
        "stable_id", "alias", "trade_date", "raw_open", "raw_high", "raw_low",
        "raw_close", "adjusted_open", "trade_status",
    ]
    execution_columns = ["alias", "trade_date", "turnover_yuan", "trade_status", "is_st"]
    filters = [("trade_date", "<=", EVALUATION_END.to_pydatetime())]
    daily = pd.read_parquet(
        price_path, columns=daily_columns, filters=filters, dtype_backend="pyarrow"
    ).rename(columns={"trade_status": "price_trade_status"})
    execution = pd.read_parquet(
        execution_path, columns=execution_columns, filters=filters,
        dtype_backend="pyarrow",
    ).rename(columns={"trade_status": "execution_trade_status"})
    daily["trade_date"] = pd.to_datetime(daily["trade_date"]).dt.normalize()
    execution["trade_date"] = pd.to_datetime(execution["trade_date"]).dt.normalize()
    if daily.duplicated(["alias", "trade_date"]).any() or execution.duplicated(
        ["alias", "trade_date"]
    ).any():
        raise EligibilityError("market alias-date identity is not unique")
    panel = daily.merge(
        execution, on=["alias", "trade_date"], how="left", validate="one_to_one"
    )
    if panel[["execution_trade_status", "is_st"]].isna().any().any():
        raise EligibilityError("execution addon join is incomplete")
    if panel["price_trade_status"].astype("int8").ne(
        panel["execution_trade_status"].astype("int8")
    ).any():
        raise EligibilityError("market trade-status sources disagree")
    sessions = pd.DatetimeIndex(panel["trade_date"].unique()).sort_values()
    if sessions.empty or sessions.has_duplicates or not sessions.is_monotonic_increasing:
        raise EligibilityError("frozen exchange-session calendar is invalid")
    session_index = {pd.Timestamp(day): index for index, day in enumerate(sessions)}
    panel["session_index"] = panel["trade_date"].map(session_index).astype("int32")
    panel["issuer_code"] = panel["alias"].astype(str).str.split(".", n=1).str[0]
    if panel["issuer_code"].str.fullmatch(r"\d{6}").ne(True).any():
        raise EligibilityError("market alias does not contain a six-digit issuer code")
    for column in (
        "raw_open", "raw_high", "raw_low", "raw_close", "adjusted_open",
        "turnover_yuan",
    ):
        panel[column] = pd.to_numeric(panel[column], errors="coerce")
    panel.sort_values(["stable_id", "trade_date"], kind="mergesort", inplace=True)
    grouped = panel.groupby("stable_id", sort=False, observed=True)
    panel["lagged_median_turnover"] = grouped["turnover_yuan"].transform(
        lambda values: values.shift(1).rolling(20, min_periods=20).median()
    )
    panel["twentieth_prior_index"] = grouped["session_index"].shift(20)
    panel["previous_session_index"] = grouped["session_index"].shift(1)
    panel["prior_raw_close"] = grouped["raw_close"].shift(1)
    panel["history_20_contiguous"] = panel["twentieth_prior_index"].eq(
        panel["session_index"] - 20
    )
    panel["prior_session_contiguous"] = panel["previous_session_index"].eq(
        panel["session_index"] - 1
    )
    if panel.duplicated(["issuer_code", "trade_date"]).any():
        raise EligibilityError("issuer-date market identity is ambiguous")
    panel.drop(columns=[
        "execution_trade_status", "raw_close", "turnover_yuan", "session_index",
        "twentieth_prior_index", "previous_session_index",
    ], inplace=True)
    panel.set_index(["issuer_code", "trade_date"], inplace=True)
    return panel, sessions


def _state_index(path: Path) -> tuple[list[pd.Timestamp], dict[pd.Timestamp, Mapping[str, Any]]]:
    rows = _json(path).get("rows") or []
    index: dict[pd.Timestamp, Mapping[str, Any]] = {}
    for row in rows:
        day = pd.Timestamp(row["trade_date"]).normalize()
        if day in index:
            raise EligibilityError("duplicate market-state date")
        index[day] = row
    return sorted(index), index


def build_event_rows(
    formula_rows: Iterable[Mapping[str, Any]], panel: pd.DataFrame,
    sessions: pd.DatetimeIndex, state_dates: list[pd.Timestamp],
    states: Mapping[pd.Timestamp, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    session_index = {pd.Timestamp(day): index for index, day in enumerate(sessions)}
    output: list[dict[str, Any]] = []
    first_formula_entry_index: int | None = None
    for source in formula_rows:
        code = str(source.get("issuer_code") or "").zfill(6)
        value = _decimal(source.get("unexpected_earnings"))
        row = {field: "" for field in OUTPUT_FIELDS}
        row.update({
            "issuer_code": code,
            "stat_date": str(source.get("stat_date") or ""),
            "pub_datetime": str(source.get("pub_datetime") or ""),
            "announcement_id": str(source.get("announcement_id") or ""),
            "unexpected_earnings": str(value),
            "threshold_eligible": "false",
            "primary_route_eligible": "false",
            "_unexpected_decimal": value,
        })
        insertion = sessions.searchsorted(pd.Timestamp(source["pub_date"]), side="right")
        if insertion >= len(sessions):
            row["screen_status"] = "entry_session_missing"
            output.append(row)
            continue
        entry = pd.Timestamp(sessions[insertion])
        entry_index = session_index[entry]
        row["entry_session"] = entry.date().isoformat()
        row["entry_session_index"] = str(entry_index)
        if first_formula_entry_index is None or entry_index < first_formula_entry_index:
            first_formula_entry_index = entry_index
        if value <= 0:
            row["screen_status"] = "unexpected_earnings_nonpositive"
            output.append(row)
            continue
        issuer_board = board(code)
        if issuer_board is None:
            row["screen_status"] = "unsupported_board"
            output.append(row)
            continue
        if entry < EVALUATION_START or entry > EVALUATION_END:
            row["screen_status"] = "outside_frozen_evaluation_window"
            output.append(row)
            continue
        try:
            market = panel.loc[(code, entry)]
        except KeyError:
            row["screen_status"] = "entry_market_row_missing"
            output.append(row)
            continue
        if isinstance(market, pd.DataFrame):
            raise EligibilityError(f"ambiguous entry row: {code} {entry.date()}")
        row["stable_id"] = str(market["stable_id"])
        row["alias"] = str(market["alias"])
        if not bool(market["history_20_contiguous"]) or not bool(
            market["prior_session_contiguous"]
        ):
            row["screen_status"] = "listing_or_lagged_history_incomplete"
            output.append(row)
            continue
        if int(market["price_trade_status"]) != 1:
            row["screen_status"] = "entry_suspended"
            output.append(row)
            continue
        if bool(market["is_st"]):
            row["screen_status"] = "entry_st"
            output.append(row)
            continue
        raw_open = _finite(market["raw_open"], positive=True)
        raw_high = _finite(market["raw_high"], positive=True)
        raw_low = _finite(market["raw_low"], positive=True)
        adjusted_open = _finite(market["adjusted_open"], positive=True)
        prior_raw_close = _finite(market["prior_raw_close"], positive=True)
        median_turnover = _finite(market["lagged_median_turnover"])
        if None in {raw_open, raw_high, raw_low, adjusted_open, prior_raw_close}:
            row["screen_status"] = "entry_price_fact_missing"
            output.append(row)
            continue
        if median_turnover is None:
            row["screen_status"] = "lagged_turnover_incomplete"
            output.append(row)
            continue
        row["raw_open"] = str(raw_open)
        row["adjusted_open"] = str(adjusted_open)
        row["lagged_median_turnover_yuan"] = str(median_turnover)
        if median_turnover < MIN_MEDIAN_TURNOVER:
            row["screen_status"] = "lagged_median_turnover_below_50m"
            output.append(row)
            continue
        tolerance = 1e-8
        one_price = (
            abs(raw_open - raw_high) <= tolerance
            and abs(raw_open - raw_low) <= tolerance
        )
        lock_threshold = (
            0.195 if issuer_board == "chinext" and entry >= pd.Timestamp("2020-08-24")
            else 0.095
        )
        if one_price and raw_open / prior_raw_close - 1 >= lock_threshold:
            row["screen_status"] = "one_price_limit_up"
            output.append(row)
            continue
        row["screen_status"] = "base_eligible"
        output.append(row)
    if first_formula_entry_index is None:
        raise EligibilityError("formula epoch has no representable entry session")
    apply_lagged_thresholds(
        output, first_formula_entry_index=first_formula_entry_index,
    )

    passed = [row for row in output if row["screen_status"] == "threshold_pass"]
    for row in passed:
        row["threshold_eligible"] = "true"
    for (_, _), group in _group_rows(
        passed, lambda row: (row["issuer_code"], row["entry_session"]),
    ):
        ordered = sorted(
            group,
            key=lambda row: (
                -row["_unexpected_decimal"], -int(row["stat_date"].replace("-", "")),
                row["announcement_id"],
            ),
        )
        for duplicate in ordered[1:]:
            duplicate["screen_status"] = "same_issuer_entry_duplicate"
            duplicate["threshold_eligible"] = "false"

    for entry_session, group in _group_rows(
        [row for row in output if row["screen_status"] == "threshold_pass"],
        lambda row: row["entry_session"],
    ):
        ranked = sorted(
            group,
            key=lambda row: (
                -row["_unexpected_decimal"],
                -float(row["lagged_median_turnover_yuan"]), row["issuer_code"],
            ),
        )
        for rank, row in enumerate(ranked, start=1):
            row["threshold_rank"] = str(rank)
            entry = pd.Timestamp(entry_session)
            state_position = bisect.bisect_left(state_dates, entry) - 1
            if state_position < 0:
                row["routed_state"] = "unknown"
                row["screen_status"] = "state_ineligible_unknown"
                continue
            observation_date = state_dates[state_position]
            state = states[observation_date]
            features = state.get("features") or {}
            confirmed = str(features.get("confirmed_state") or "UNKNOWN")
            raw = str(features.get("raw_state") or "UNKNOWN")
            routed = route_state(confirmed, raw)
            row.update({
                "market_state_observation_date": observation_date.date().isoformat(),
                "confirmed_state": confirmed,
                "raw_state": raw,
                "routed_state": routed,
            })
            if routed in {"weak_repair", "sentiment_rising"}:
                row["screen_status"] = "admitted_primary_route"
                row["primary_route_eligible"] = "true"
            else:
                row["screen_status"] = f"state_ineligible_{routed}"
    return output


def _group_rows(rows: Iterable[dict[str, Any]], key):
    grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[key(row)].append(row)
    return sorted(grouped.items(), key=lambda item: item[0])


def build() -> dict[str, Any]:
    (
        formula_manifest, amendment, evaluation_amendment, formula_path,
        price_path, execution_path, state_path,
    ) = _load_contract()
    with formula_path.open(encoding="utf-8", newline="") as source:
        formula_rows = [dict(row) for row in csv.DictReader(source)]
    panel, sessions = _market_panel(price_path, execution_path)
    state_dates, states = _state_index(state_path)
    output_rows = build_event_rows(formula_rows, panel, sessions, state_dates, states)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    temporary = EVENTS_PATH.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in OUTPUT_FIELDS} for row in output_rows)
    temporary.replace(EVENTS_PATH)
    status_counts: dict[str, int] = defaultdict(int)
    for row in output_rows:
        status_counts[str(row["screen_status"])] += 1
    module_path = Path(__file__).resolve()
    manifest = {
        "schema_version": 1,
        "status": "market_eligibility_epoch_ready_for_one_chronological_evaluation",
        "candidate_id": formula_manifest["candidate_id"],
        "builder": str(module_path.relative_to(ROOT)),
        "builder_sha256": sha256(module_path),
        "amendment": {
            "path": str(AMENDMENT_PATH.relative_to(ROOT)),
            "sha256": sha256(AMENDMENT_PATH),
        },
        "evaluation_amendment": {
            "path": str(EVALUATION_AMENDMENT_PATH.relative_to(ROOT)),
            "sha256": sha256(EVALUATION_AMENDMENT_PATH),
        },
        "formula_manifest": {
            "path": str(FORMULA_MANIFEST_PATH.relative_to(ROOT)),
            "sha256": sha256(FORMULA_MANIFEST_PATH),
        },
        "formula_audit": {
            "path": str(FORMULA_AUDIT_PATH.relative_to(ROOT)),
            "sha256": sha256(FORMULA_AUDIT_PATH),
        },
        "market_source_hashes": {
            "price": sha256(price_path), "execution": sha256(execution_path),
            "market_state": sha256(state_path),
        },
        "event_eligibility": str(EVENTS_PATH.relative_to(ROOT)),
        "event_eligibility_sha256": sha256(EVENTS_PATH),
        "counts": {
            "formula_events": len(output_rows),
            "by_status": dict(sorted(status_counts.items())),
        },
        "all_formula_events_classified": sum(status_counts.values()) == len(output_rows),
        "candidate_price_rows_read": True,
        "forward_holding_prices_selected_persisted_or_used": False,
        "returns_computed": False,
        "portfolio_computed": False,
        "ready_for_one_chronological_evaluation": True,
        "production_eligible": False,
        "next_action": "Bind this epoch into the one allowed development, validation and sealed-test portfolio evaluation; any specification change requires a new candidate id.",
    }
    _atomic_json(MANIFEST_PATH, manifest)
    return manifest


def preflight() -> dict[str, Any]:
    amendment = _json(AMENDMENT_PATH)
    return {
        "status": "dry_run_only",
        "candidate_id": amendment.get("candidate_id"),
        "formula_epoch_required": True,
        "candidate_price_rows_read": False,
        "returns_computed": False,
        "ready_for_returns": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    result = build() if args.execute else preflight()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
