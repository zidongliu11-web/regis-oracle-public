"""Append-only forward observations for the validated limit-hit crowding factor."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from quantqc.ashare import limit_up_price


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = ROOT / "research/RECENT_LIMIT_HIT_CROWDING_V1_FORWARD_PROTOCOL.json"
SNAPSHOT_PATH = ROOT / "research/full_market_top300.json"
DAILY_DIR = ROOT / "research/data/v4_daily"
FORWARD_DIR = ROOT / "research/forward/recent_limit_hit_crowding_v1"
JOURNAL_PATH = FORWARD_DIR / "observations.jsonl"
LATEST_PATH = ROOT / "reports/recent_limit_hit_crowding_forward_latest.json"
SH_TZ = ZoneInfo("Asia/Shanghai")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_bytes(_canonical(value))
    os.replace(temp, path)


def _load_protocol(root: Path) -> dict[str, Any]:
    path = root / PROTOCOL_PATH.relative_to(ROOT)
    protocol = json.loads(path.read_text(encoding="utf-8"))
    if protocol.get("status") != "forward_protocol_frozen_before_first_forward_label":
        raise RuntimeError("forward factor protocol is not frozen")
    for name, ref in protocol["bindings"].items():
        bound = root / ref["path"]
        if not bound.is_file() or _sha(bound) != ref["sha256"]:
            raise RuntimeError(f"forward protocol binding invalid: {name}")
    historical = json.loads((root / protocol["bindings"]["historical_result"]["path"]).read_text())
    if historical.get("historical_factor_gate_passed") is not True:
        raise RuntimeError("historical factor gate is not passed")
    return protocol


def _twenty_bars(root: Path, code: str, data_asof: str) -> list[dict[str, Any]]:
    path = root / "research/data/v4_daily" / f"{code}.csv.gz"
    try:
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as stream:
            rows = [row for row in csv.DictReader(stream) if str(row.get("date") or "") <= data_asof and str(row.get("tradestatus") or "") == "1"]
    except (OSError, EOFError):
        return []
    parsed: list[dict[str, Any]] = []
    for row in rows[-20:]:
        try:
            parsed.append({
                "date": str(row["date"]), "high": float(row["high"]),
                "preclose": float(row["preclose"]), "is_st": int(row["isST"]),
            })
        except (KeyError, TypeError, ValueError):
            return []
    return parsed


def _hit_count(rows: list[dict[str, Any]], code: str) -> int | None:
    if len(rows) != 20 or rows[-1]["is_st"] != 0:
        return None
    hits = 0
    for row in rows:
        upper = limit_up_price(row["preclose"], code)
        if upper is None:
            return None
        hits += int(float(row["high"]) >= float(upper) - 0.001)
    return hits


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
    rows: list[dict[str, Any]] = []
    missing = 0
    distribution: dict[str, int] = {}
    for item in eligible:
        code = str(item.get("code") or "").zfill(6)
        count = _hit_count(_twenty_bars(root, code, data_asof), code)
        if count is None:
            missing += 1
            continue
        distribution[str(count)] = distribution.get(str(count), 0) + 1
        if count >= 1:
            rows.append({
                "code": code, "name": str(item.get("name") or code),
                "industry": str(item.get("industry") or ""),
                "limit_hit_count_20": count,
                "negative_limit_hit_count_20_v1": -count,
                "data_asof": data_asof,
            })
    rows.sort(key=lambda row: (row["limit_hit_count_20"], row["code"]))
    observation = {
        "schema": "recent_limit_hit_crowding_forward_observation.v1",
        "created_at": datetime.now(SH_TZ).isoformat(timespec="seconds"),
        "status": "historical_pass_forward_observation_no_label_yet",
        "factor_id": protocol["factor_id"], "data_asof": data_asof,
        "protocol": {"path": str(PROTOCOL_PATH.relative_to(ROOT)), "sha256": _sha(root / PROTOCOL_PATH.relative_to(ROOT))},
        "input": {"path": str(snapshot_path.relative_to(root)), "sha256": _sha(snapshot_path)},
        "eligible_input_rows": len(eligible), "factor_value_rows": len(eligible) - missing,
        "missing_twenty_bar_rows": missing, "recent_limit_hitter_rows": len(rows),
        "hit_count_distribution": distribution, "rows": rows,
        "forward_completed_weekly_labels": 0,
        "forward_required_weekly_labels": int(protocol["observation"]["minimum_completed_weekly_labels"]),
        "production_factor": False, "generates_buy_or_sell_alert": False,
        "blocks_non_factor_routes": False,
    }
    observation["observation_sha256"] = hashlib.sha256(_canonical(observation)).hexdigest()
    return observation


def capture(root: Path = ROOT) -> dict[str, Any]:
    observation = build_observation(root)
    journal = root / JOURNAL_PATH.relative_to(ROOT)
    latest = root / LATEST_PATH.relative_to(ROOT)
    journal.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if journal.is_file():
        existing = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
    same_day = [row for row in existing if row.get("data_asof") == observation["data_asof"]]
    if same_day:
        if same_day[-1].get("input") != observation.get("input"):
            raise RuntimeError("same-day forward input identity changed")
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
