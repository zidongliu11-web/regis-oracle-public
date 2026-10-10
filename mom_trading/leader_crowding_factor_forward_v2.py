"""Corrected append-only forward observations for the limit-hit crowding factor."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import os
import statistics
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from quantqc.ashare import board_of, price_limit_rule


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = ROOT / "research/RECENT_LIMIT_HIT_CROWDING_V2_FORWARD_PROTOCOL.json"
SNAPSHOT_PATH = ROOT / "research/full_market_top300.json"
DAILY_DIR = ROOT / "research/data/v4_daily"
FORWARD_DIR = ROOT / "research/forward/recent_limit_hit_crowding_v2"
JOURNAL_PATH = FORWARD_DIR / "observations.jsonl"
LATEST_PATH = ROOT / "reports/recent_limit_hit_crowding_forward_v2_latest.json"
SH_TZ = ZoneInfo("Asia/Shanghai")
PRICE_TICK = Decimal("0.01")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_bytes(_canonical(value))
    os.replace(temp, path)


def _load_protocol(root: Path) -> dict[str, Any]:
    path = root / PROTOCOL_PATH.relative_to(ROOT)
    protocol = json.loads(path.read_text(encoding="utf-8"))
    if protocol.get("status") != (
        "corrected_forward_protocol_frozen_before_any_forward_label"
    ):
        raise RuntimeError("corrected forward factor protocol is not frozen")
    for name, ref in protocol["bindings"].items():
        bound = root / ref["path"]
        if not bound.is_file() or _sha(bound) != ref["sha256"]:
            raise RuntimeError(f"corrected forward protocol binding invalid: {name}")
    historical = json.loads(
        (root / protocol["bindings"]["historical_result"]["path"])
        .read_text(encoding="utf-8")
    )
    amendment = json.loads(
        (root / protocol["bindings"]["methodology_amendment"]["path"])
        .read_text(encoding="utf-8")
    )
    if historical.get("historical_factor_gate_passed") is not True:
        raise RuntimeError("historical factor gate is not passed")
    if amendment.get("status") != "v1_observation_invalidated_before_any_forward_label":
        raise RuntimeError("v1 invalidation boundary is missing")
    return protocol


def _special_event_keys(root: Path, protocol: dict[str, Any]) -> set[tuple[str, str]]:
    path = root / protocol["bindings"]["special_price_limit_events"]["path"]
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return {
            (str(row.get("code") or "").split(".")[-1].zfill(6), str(row.get("date") or ""))
            for row in csv.DictReader(stream)
            if str(row.get("verification_status") or "") == "verified"
        }


def _stock_rows(root: Path, code: str, data_asof: str) -> list[dict[str, Any]]:
    path = root / "research/data/v4_daily" / f"{code}.csv.gz"
    try:
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as stream:
            source = [
                row for row in csv.DictReader(stream)
                if str(row.get("date") or "") <= data_asof
                and str(row.get("tradestatus") or "") == "1"
            ]
    except (OSError, EOFError):
        return []
    parsed: list[dict[str, Any]] = []
    for row in source:
        try:
            values = {
                "date": str(row["date"]),
                "high": float(row["high"]),
                "close": float(row["close"]),
                "preclose": float(row["preclose"]),
                "amount": float(row["amount"]),
                "volume": float(row["volume"]),
                "turn": float(row["turn"]),
                "is_st": int(row["isST"]),
            }
        except (KeyError, TypeError, ValueError):
            return []
        if not all(
            math.isfinite(values[field])
            for field in ("high", "close", "preclose", "amount", "volume", "turn")
        ):
            return []
        parsed.append(values)
    return parsed


def _corporate_action_bridge_changed(
    previous_close: float | None, reference_preclose: float,
) -> bool:
    if previous_close is None or previous_close <= 0 or reference_preclose <= 0:
        return True
    return abs(float(previous_close) - float(reference_preclose)) > 0.0051


def _upper_limit(code: str, row: dict[str, Any]) -> float | None:
    rule = price_limit_rule(
        code, trade_date=row["date"], is_risk_warning=False,
        no_price_limit=False,
    )
    if rule.get("valid") is not True or rule.get("rate") is None:
        return None
    return float(
        (Decimal(str(row["preclose"])) * (Decimal("1") + Decimal(str(rule["rate"]))))
        .quantize(PRICE_TICK, rounding=ROUND_HALF_UP)
    )


def _hit_count(
    rows: list[dict[str, Any]], code: str,
    special_events: set[tuple[str, str]] | None = None,
) -> int | None:
    if len(rows) < 60:
        return None
    events = special_events or set()
    latest = rows[-1]
    if latest["is_st"] != 0 or _corporate_action_bridge_changed(
        rows[-2]["close"], latest["preclose"],
    ):
        return None
    hits = 0
    start = len(rows) - 20
    for index in range(start, len(rows)):
        row = rows[index]
        previous_close = rows[index - 1]["close"] if index else None
        evaluable = (
            index >= 5
            and row["is_st"] == 0
            and (code, row["date"]) not in events
            and not _corporate_action_bridge_changed(previous_close, row["preclose"])
        )
        if not evaluable:
            continue
        upper = _upper_limit(code, row)
        if upper is None:
            return None
        hits += int(float(row["high"]) >= upper - 0.0051)
    return hits


def _universe_reason(rows: list[dict[str, Any]], code: str) -> str | None:
    if board_of(code) not in {"main", "gemb"}:
        return "unsupported_board"
    if len(rows) < 60:
        return "insufficient_history"
    latest = rows[-1]
    if latest["is_st"] != 0:
        return "signal_day_st"
    if _corporate_action_bridge_changed(rows[-2]["close"], latest["preclose"]):
        return "signal_day_corporate_action"
    if latest["close"] < 3.0:
        return "signal_close_below_3"
    window = rows[-20:]
    if statistics.median(row["amount"] for row in window) < 50_000_000:
        return "median_amount_below_50m"
    caps = [
        row["close"] * row["volume"] / (row["turn"] / 100.0)
        for row in window if row["close"] > 0 and row["volume"] > 0 and row["turn"] > 0
    ]
    if len(caps) < 10:
        return "float_market_cap_proxy_missing"
    cap = statistics.median(caps)
    if not 2_000_000_000 <= cap <= 80_000_000_000:
        return "float_market_cap_outside_2b_80b"
    return None


def build_observation(root: Path = ROOT) -> dict[str, Any]:
    protocol = _load_protocol(root)
    snapshot_path = root / protocol["input"]["snapshot"]
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    eligible = snapshot.get("eligible") or []
    audit = snapshot.get("scan_audit") or {}
    if not (
        snapshot.get("eligible_complete") is True
        and audit.get("status") == "verified"
        and audit.get("complete_path_scan") is True
        and int(snapshot.get("eligible_count") or 0) == len(eligible)
    ):
        raise RuntimeError("full-market factor input is incomplete")
    data_asof = str(snapshot.get("data_asof") or "")
    if not data_asof:
        raise RuntimeError("full-market factor input data_asof is missing")
    special_events = _special_event_keys(root, protocol)
    factor_rows: list[dict[str, Any]] = []
    exclusions: dict[str, int] = {}
    distribution: dict[str, int] = {}
    supported_board_rows = 0
    for item in eligible:
        code = str(item.get("code") or "").split(".")[-1].zfill(6)
        if board_of(code) in {"main", "gemb"}:
            supported_board_rows += 1
        stock_rows = _stock_rows(root, code, data_asof)
        reason = _universe_reason(stock_rows, code)
        if reason:
            exclusions[reason] = exclusions.get(reason, 0) + 1
            continue
        count = _hit_count(stock_rows, code, special_events)
        if count is None:
            exclusions["limit_identity_unresolved"] = exclusions.get(
                "limit_identity_unresolved", 0,
            ) + 1
            continue
        distribution[str(count)] = distribution.get(str(count), 0) + 1
        if count >= 1:
            factor_rows.append({
                "code": code,
                "name": str(item.get("name") or code),
                "industry": str(item.get("industry") or ""),
                "limit_hit_count_20": count,
                "negative_limit_hit_count_20_v1": -count,
                "data_asof": data_asof,
            })
    factor_rows.sort(key=lambda row: (row["limit_hit_count_20"], row["code"]))
    factor_universe_rows = sum(distribution.values())
    observation = {
        "schema": "recent_limit_hit_crowding_forward_observation.v2",
        "created_at": datetime.now(SH_TZ).isoformat(timespec="seconds"),
        "status": "historical_pass_state_applicability_and_forward_pending",
        "factor_id": protocol["factor_id"],
        "data_asof": data_asof,
        "protocol": {
            "path": str(PROTOCOL_PATH.relative_to(ROOT)),
            "sha256": _sha(root / PROTOCOL_PATH.relative_to(ROOT)),
        },
        "input": {
            "path": str(snapshot_path.relative_to(root)),
            "sha256": _sha(snapshot_path),
        },
        "eligible_input_rows": len(eligible),
        "supported_board_rows": supported_board_rows,
        "factor_universe_rows": factor_universe_rows,
        "recent_limit_hitter_rows": len(factor_rows),
        "exclusion_counts": dict(sorted(exclusions.items())),
        "hit_count_distribution": dict(sorted(distribution.items(), key=lambda item: int(item[0]))),
        "rows": factor_rows,
        "state_applicability_status": "pending_frozen_state_conditioned_validation",
        "current_state_applicable": False,
        "forward_completed_weekly_labels": 0,
        "forward_required_weekly_labels": int(
            protocol["observation"]["minimum_completed_weekly_labels"]
        ),
        "production_factor": False,
        "generates_buy_or_sell_alert": False,
        "blocks_non_factor_routes": False,
    }
    observation["observation_sha256"] = hashlib.sha256(
        _canonical(observation)
    ).hexdigest()
    return observation


def capture(root: Path = ROOT) -> dict[str, Any]:
    observation = build_observation(root)
    journal = root / JOURNAL_PATH.relative_to(ROOT)
    latest = root / LATEST_PATH.relative_to(ROOT)
    journal.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if journal.is_file():
        existing = [
            json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    same_day = [row for row in existing if row.get("data_asof") == observation["data_asof"]]
    if same_day:
        if same_day[-1].get("input") != observation.get("input"):
            raise RuntimeError("same-day corrected forward input identity changed")
        _write_json(latest, same_day[-1])
        return same_day[-1]
    with journal.open("ab") as stream:
        stream.write(_canonical(observation) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    _write_json(latest, observation)
    return observation


if __name__ == "__main__":
    print(json.dumps(capture(), ensure_ascii=False, indent=2))
