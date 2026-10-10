"""Risk-warning-guarded V3 observer built on the immutable V2 calculation."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import leader_crowding_factor_forward_v2 as v2


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = ROOT / "research/RECENT_LIMIT_HIT_CROWDING_V3_FORWARD_PROTOCOL.json"
FORWARD_DIR = ROOT / "research/forward/recent_limit_hit_crowding_v3"
JOURNAL_PATH = FORWARD_DIR / "observations.jsonl"
LATEST_PATH = ROOT / "reports/recent_limit_hit_crowding_forward_v3_latest.json"
SH_TZ = ZoneInfo("Asia/Shanghai")
RISK_WARNING_PREFIXES = ("*ST", "S*ST", "SST", "ST")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(_canonical(value))
    os.replace(temporary, path)


def _risk_warning_name(value: object) -> bool:
    normalized = str(value or "").upper().replace(" ", "")
    return normalized.startswith(RISK_WARNING_PREFIXES)


def _load_protocol(root: Path) -> dict[str, Any]:
    path = root / PROTOCOL_PATH.relative_to(ROOT)
    protocol = json.loads(path.read_text(encoding="utf-8"))
    if protocol.get("status") != (
        "risk_warning_guarded_forward_protocol_frozen_before_any_forward_label"
    ):
        raise RuntimeError("V3 forward factor protocol is not frozen")
    for name, ref in protocol["bindings"].items():
        bound = root / ref["path"]
        if not bound.is_file() or _sha(bound) != ref["sha256"]:
            raise RuntimeError(f"V3 forward protocol binding invalid: {name}")
    historical = json.loads(
        (root / protocol["bindings"]["historical_result"]["path"])
        .read_text(encoding="utf-8")
    )
    state_matrix = json.loads(
        (root / protocol["bindings"]["emotion_state_matrix"]["path"])
        .read_text(encoding="utf-8")
    )
    amendment = json.loads(
        (root / protocol["bindings"]["v2_invalidation_amendment"]["path"])
        .read_text(encoding="utf-8")
    )
    if historical.get("historical_factor_gate_passed") is not True:
        raise RuntimeError("historical factor gate is not passed")
    if state_matrix.get("historically_applicable_states") != ["STABILIZING"]:
        raise RuntimeError("emotion-state factor matrix identity changed")
    if amendment.get("status") != "v2_observation_invalidated_before_any_forward_label":
        raise RuntimeError("V2 invalidation boundary is missing")
    return protocol


def _apply_name_guard(
    observation: dict[str, Any], eligible: list[dict[str, Any]], root: Path,
    protocol: dict[str, Any],
) -> dict[str, Any]:
    special_events = v2._special_event_keys(root, protocol)
    conflicted_codes: list[str] = []
    conflicted_hitter_codes: list[str] = []
    for item in eligible:
        if not _risk_warning_name(item.get("name")):
            continue
        code = str(item.get("code") or "").split(".")[-1].zfill(6)
        stock_rows = v2._stock_rows(root, code, str(observation["data_asof"]))
        if v2._universe_reason(stock_rows, code) is not None:
            continue
        count = v2._hit_count(stock_rows, code, special_events)
        if count is None:
            continue
        conflicted_codes.append(code)
        observation["factor_universe_rows"] -= 1
        key = str(count)
        observation["hit_count_distribution"][key] -= 1
        if observation["hit_count_distribution"][key] == 0:
            del observation["hit_count_distribution"][key]
        if count >= 1:
            conflicted_hitter_codes.append(code)
    if conflicted_hitter_codes:
        rejected = set(conflicted_hitter_codes)
        observation["rows"] = [
            row for row in observation["rows"] if row.get("code") not in rejected
        ]
    observation["recent_limit_hitter_rows"] = len(observation["rows"])
    if conflicted_codes:
        observation["exclusion_counts"]["signal_day_st_source_conflict"] = len(
            conflicted_codes
        )
    observation["risk_warning_source_conflict_codes"] = sorted(conflicted_codes)
    return observation


def build_observation(root: Path = ROOT) -> dict[str, Any]:
    protocol = _load_protocol(root)
    snapshot_path = root / protocol["input"]["snapshot"]
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    observation = v2.build_observation(root)
    observation = _apply_name_guard(
        observation, list(snapshot.get("eligible") or []), root, protocol,
    )
    observation.update({
        "schema": "recent_limit_hit_crowding_forward_observation.v3",
        "created_at": datetime.now(SH_TZ).isoformat(timespec="seconds"),
        "status": "historical_stabilizing_only_forward_pending",
        "protocol": {
            "path": str(PROTOCOL_PATH.relative_to(ROOT)),
            "sha256": _sha(root / PROTOCOL_PATH.relative_to(ROOT)),
        },
        "historically_applicable_states": ["STABILIZING"],
        "historically_inapplicable_or_insufficient_states": [
            "RISK_ON", "OVERHEATED", "RISK_OFF",
        ],
        "state_applicability_status": "historical_STABILIZING_pass_forward_pending",
        "current_state_applicable": False,
        "forward_completed_weekly_labels": 0,
        "forward_required_weekly_labels_in_same_state": int(
            protocol["emotion_route"]["minimum_completed_weekly_labels_in_same_state"]
        ),
        "production_factor": False,
        "generates_buy_or_sell_alert": False,
        "blocks_non_factor_routes": False,
    })
    observation.pop("forward_required_weekly_labels", None)
    observation.pop("observation_sha256", None)
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
            raise RuntimeError("same-day V3 forward input identity changed")
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
