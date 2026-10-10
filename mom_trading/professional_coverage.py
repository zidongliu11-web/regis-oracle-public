"""Deterministic, bounded coverage repair for full-market professional scores."""

from __future__ import annotations

import argparse
import bisect
import fcntl
import hashlib
import json
import math
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


_PROJECT = Path(__file__).resolve().parent.parent
_DEFAULT_SNAPSHOT = _PROJECT / "research" / "full_market_top300.json"
_DEFAULT_DATABASE = _PROJECT / "research" / "professional_coverage.sqlite3"
_DEFAULT_REPORT = _PROJECT / "reports" / "professional_coverage_worker_state.json"
_SH_TZ = ZoneInfo("Asia/Shanghai")


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _codes_sha(codes: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(codes, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _bands(rows: list[dict], field: str) -> dict[str, str]:
    values = sorted(
        value for row in rows
        if (value := _finite(row.get(field))) is not None and value > 0
    )
    count = len(values)
    output: dict[str, str] = {}
    for row in rows:
        code = str(row.get("code") or "")
        value = _finite(row.get(field))
        if value is None or value <= 0 or count == 0:
            output[code] = "UNKNOWN"
            continue
        less = bisect.bisect_left(values, value)
        equal = bisect.bisect_right(values, value) - less
        midrank = (less + 0.5 * equal) / count
        output[code] = "L" if midrank <= 1 / 3 else "M" if midrank <= 2 / 3 else "H"
    return output


def _comparable(row: dict) -> bool:
    preview = row.get("preselection_professional_preview") or {}
    return bool(
        preview.get("status") == "available"
        and _finite(preview.get("professional_score")) is not None
        and preview.get("coverage_status") in {"complete", "partial", None}
    )


def coverage_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    rows = snapshot.get("eligible") if isinstance(snapshot.get("eligible"), list) else []
    data_asof = str(snapshot.get("data_asof") or "")
    codes = [str(row.get("code") or "") for row in rows if isinstance(row, dict)]
    valid = bool(
        snapshot.get("eligible_complete") is True
        and data_asof
        and rows
        and len(codes) == len(set(codes))
        and all(str(row.get("data_asof") or "") == data_asof for row in rows)
    )
    if not valid:
        return {"status": "unavailable_unverified_snapshot", "rows": []}
    size_bands = _bands(rows, "float_cap_proxy_20d_median")
    liquidity_bands = _bands(rows, "avg_amount_20d")
    classified = []
    cells: dict[str, dict[str, int]] = {}
    for row in rows:
        code = str(row["code"])
        classification = str(
            row.get("classification_label")
            or f"__UNMAPPED__:{row.get('market_segment') or 'UNKNOWN'}"
        )
        cell = f"{classification}|size={size_bands[code]}|liquidity={liquidity_bands[code]}"
        comparable = _comparable(row)
        classified.append({**row, "coverage_cell": cell, "coverage_comparable": comparable})
        stats = cells.setdefault(cell, {"total": 0, "comparable": 0})
        stats["total"] += 1
        stats["comparable"] += int(comparable)
    comparable_count = sum(row["coverage_comparable"] for row in classified)
    large_cells = [value for value in cells.values() if value["total"] >= 10]
    overall_pct = comparable_count / len(classified) * 100.0
    minimum_large_cell_pct = min(
        (cell["comparable"] / cell["total"] * 100.0 for cell in large_cells),
        default=100.0,
    )
    identity = {
        "data_asof": data_asof,
        "ordered_codes_sha256": _codes_sha(codes),
        "stratification_inputs_sha256": hashlib.sha256(
            json.dumps([
                [
                    str(row.get("code") or ""),
                    str(row.get("classification_label") or ""),
                    _finite(row.get("float_cap_proxy_20d_median")),
                    _finite(row.get("avg_amount_20d")),
                ]
                for row in rows
            ], ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest(),
        "classification_sha256": str(
            ((snapshot.get("scan_audit") or {}).get("industry_classification") or {}).get(
                "sha256"
            ) or ""
        ),
        "formula": "classification_x_float_cap_midrank_tercile_x_liquidity_midrank_tercile_v1",
    }
    epoch_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "status": "ready",
        "epoch_id": epoch_id,
        "data_asof": data_asof,
        "rows": classified,
        "cells": cells,
        "eligible_count": len(classified),
        "comparable_count": comparable_count,
        "backlog_count": len(classified) - comparable_count,
        "comparable_coverage_pct": round(overall_pct, 2),
        "minimum_large_cell_coverage_pct": round(minimum_large_cell_pct, 2),
        "claim_scope": (
            "full_comparable"
            if overall_pct >= 90.0 and minimum_large_cell_pct >= 80.0
            else "scored_subset"
        ),
        "selection_inputs_excluded": [
            "screen_score", "professional_score_value", "returns", "labels",
        ],
    }


def claim_coverage_batch(
    snapshot: dict[str, Any], db_path: Path, *, now: datetime, limit: int = 4,
) -> dict[str, Any]:
    state = coverage_state(snapshot)
    limit = max(0, min(4, int(limit)))
    if state.get("status") != "ready" or limit == 0:
        return {**state, "selected": [], "reason": "snapshot_unavailable_or_limit_zero"}
    epoch_id = str(state["epoch_id"])
    minute = now.replace(second=0, microsecond=0).isoformat()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=10) as connection, connection:
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS coverage_attempt (
                   epoch_id TEXT NOT NULL, code TEXT NOT NULL,
                   attempt_count INTEGER NOT NULL, last_attempt TEXT NOT NULL,
                   retry_after TEXT NOT NULL,
                   PRIMARY KEY(epoch_id, code)
               )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS coverage_batch (
                   epoch_id TEXT NOT NULL, minute TEXT NOT NULL,
                   PRIMARY KEY(epoch_id, minute)
               )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS coverage_global_minute (
                   minute TEXT PRIMARY KEY
               )"""
        )
        # Existing databases may predate the global bucket.  Preserve their
        # already-consumed minute budgets before attempting a new claim.
        connection.execute(
            """INSERT OR IGNORE INTO coverage_global_minute(minute)
               SELECT DISTINCT minute FROM coverage_batch"""
        )
        global_inserted = connection.execute(
            "INSERT OR IGNORE INTO coverage_global_minute(minute) VALUES (?)",
            (minute,),
        ).rowcount
        if global_inserted == 0:
            return {
                **state, "selected": [],
                "reason": "global_minute_budget_already_claimed",
            }
        inserted = connection.execute(
            "INSERT OR IGNORE INTO coverage_batch(epoch_id, minute) VALUES (?, ?)",
            (epoch_id, minute),
        ).rowcount
        if inserted == 0:
            return {**state, "selected": [], "reason": "minute_budget_already_claimed"}
        attempts = {
            code: (int(count), str(retry_after))
            for code, count, retry_after in connection.execute(
                "SELECT code, attempt_count, retry_after FROM coverage_attempt WHERE epoch_id = ?",
                (epoch_id,),
            )
        }
        by_cell: dict[str, list[dict]] = {}
        for row in state["rows"]:
            if row["coverage_comparable"]:
                continue
            count, retry_after = attempts.get(str(row["code"]), (0, ""))
            if count >= 3 or (retry_after and retry_after > now.isoformat()):
                continue
            by_cell.setdefault(str(row["coverage_cell"]), []).append(row)
        cells = state["cells"]
        ordered_cells = sorted(
            by_cell,
            key=lambda key: (
                cells[key]["comparable"] / cells[key]["total"],
                -len(by_cell[key]), key,
            ),
        )
        selected = []
        while ordered_cells and len(selected) < limit:
            next_cells = []
            for key in ordered_cells:
                candidates = by_cell[key]
                candidates.sort(key=lambda row: (
                    attempts.get(str(row["code"]), (0, ""))[0],
                    str(row["code"]),
                ))
                if candidates and len(selected) < limit:
                    selected.append(candidates.pop(0))
                if candidates:
                    next_cells.append(key)
            ordered_cells = next_cells
        delays = (1, 4, 24)
        for row in selected:
            code = str(row["code"])
            previous = attempts.get(code, (0, ""))[0]
            count = previous + 1
            retry_after = now + timedelta(hours=delays[min(count - 1, 2)])
            connection.execute(
                """INSERT INTO coverage_attempt(
                       epoch_id, code, attempt_count, last_attempt, retry_after
                   ) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(epoch_id, code) DO UPDATE SET
                       attempt_count=excluded.attempt_count,
                       last_attempt=excluded.last_attempt,
                       retry_after=excluded.retry_after""",
                (epoch_id, code, count, now.isoformat(), retry_after.isoformat()),
            )
    return {
        **{key: value for key, value in state.items() if key != "rows"},
        "selected": [
            {
                "code": row["code"], "name": row.get("name") or row["code"],
                "coverage_cell": row["coverage_cell"],
            }
            for row in selected
        ],
        "reason": "bounded_stratified_backlog_claim",
        "per_minute_limit": 4,
    }


def _ensure_worker_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS coverage_job (
               epoch_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL,
               coverage_cell TEXT NOT NULL, queue_kind TEXT NOT NULL,
               state TEXT NOT NULL, lease_token TEXT, lease_until TEXT,
               attempt_count INTEGER NOT NULL DEFAULT 0,
               last_started TEXT, last_finished TEXT, retry_after TEXT,
               last_outcome TEXT, last_error TEXT,
               parser_before TEXT, parser_after TEXT,
               PRIMARY KEY(epoch_id, code)
           )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS coverage_code_state (
               code TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL,
               epoch_id TEXT NOT NULL, comparable INTEGER NOT NULL,
               reason TEXT, last_seen_at TEXT NOT NULL
           )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS coverage_snapshot (
               snapshot_id TEXT PRIMARY KEY, generated_at TEXT NOT NULL,
               epoch_id TEXT NOT NULL, available INTEGER NOT NULL,
               backlog INTEGER NOT NULL, gross_gains INTEGER NOT NULL,
               gross_regressions INTEGER NOT NULL, new_codes INTEGER NOT NULL,
               removed_codes INTEGER NOT NULL, reason_counts_json TEXT NOT NULL
           )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS coverage_global_minute (
               minute TEXT PRIMARY KEY
           )"""
    )


def _financial_gap(row: dict) -> bool:
    preview = row.get("preselection_professional_preview") or {}
    missing = preview.get("data_gap_missing") or []
    return bool(not row.get("coverage_comparable") and {"growth", "quality"} & set(missing))


def _queue_kind(cache_state: str) -> str:
    if cache_state == "legacy_parser":
        return "legacy_parser_refresh"
    if cache_state == "stale_ttl":
        return "stale_financial_source"
    return "missing_financial_source"


def _select_stratified(
    candidates: list[dict], cells: dict[str, dict[str, int]], limit: int,
) -> list[dict]:
    selected: list[dict] = []
    for queue_kind in (
        "legacy_parser_refresh", "stale_financial_source", "missing_financial_source",
    ):
        by_cell: dict[str, list[dict]] = {}
        for row in candidates:
            if row["queue_kind"] == queue_kind:
                by_cell.setdefault(str(row["coverage_cell"]), []).append(row)
        ordered_cells = sorted(
            by_cell,
            key=lambda key: (
                cells[key]["comparable"] / cells[key]["total"],
                -len(by_cell[key]), key,
            ),
        )
        while ordered_cells and len(selected) < limit:
            next_cells = []
            for key in ordered_cells:
                rows = by_cell[key]
                rows.sort(key=lambda row: (row["attempt_count"], str(row["code"])))
                if rows and len(selected) < limit:
                    selected.append(rows.pop(0))
                if rows:
                    next_cells.append(key)
            ordered_cells = next_cells
        if len(selected) >= limit:
            break
    return selected


def lease_coverage_batch(
    snapshot: dict[str, Any], db_path: Path, *, now: datetime, limit: int = 4,
) -> dict[str, Any]:
    """Lease a score-blind financial-only batch without consuming attempts."""
    from .company_profile import cached_financial_status

    state = coverage_state(snapshot)
    limit = max(0, min(4, int(limit)))
    if state.get("status") != "ready" or limit == 0:
        return {**state, "selected": [], "reason": "snapshot_unavailable_or_limit_zero"}
    epoch_id = str(state["epoch_id"])
    now_text = now.isoformat()
    minute = now.replace(second=0, microsecond=0).isoformat()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("BEGIN IMMEDIATE")
        _ensure_worker_tables(connection)
        connection.execute(
            """UPDATE coverage_job
               SET state='failed', lease_token=NULL, lease_until=NULL,
                   retry_after=?, last_finished=?, last_outcome='worker_lost',
                   last_error='lease_expired_before_finish'
               WHERE epoch_id=? AND state IN ('leased','running')
                 AND lease_until IS NOT NULL AND lease_until <= ?""",
            (now_text, now_text, epoch_id, now_text),
        )
        eligible_rows = [row for row in state["rows"] if _financial_gap(row)]
        for row in eligible_rows:
            code = str(row["code"])
            cache = cached_financial_status(code, now=now)
            if cache["cache_state"] == "fresh_v2":
                connection.execute(
                    """INSERT INTO coverage_job(
                           epoch_id,code,name,coverage_cell,queue_kind,state,
                           last_finished,last_outcome,parser_before,parser_after
                       ) VALUES (?,?,?,?,?,'success',?,'input_already_ready',?,?)
                       ON CONFLICT(epoch_id,code) DO UPDATE SET
                           state='success', lease_token=NULL, lease_until=NULL,
                           last_finished=excluded.last_finished,
                           last_outcome=excluded.last_outcome,
                           last_error=NULL, parser_after=excluded.parser_after""",
                    (
                        epoch_id, code, str(row.get("name") or code),
                        str(row["coverage_cell"]), _queue_kind(cache["cache_state"]),
                        now_text, cache.get("parser_version"), cache.get("parser_version"),
                    ),
                )
                continue
            connection.execute(
                """INSERT OR IGNORE INTO coverage_job(
                       epoch_id,code,name,coverage_cell,queue_kind,state,parser_before
                   ) VALUES (?,?,?,?,?,'pending',?)""",
                (
                    epoch_id, code, str(row.get("name") or code),
                    str(row["coverage_cell"]), _queue_kind(cache["cache_state"]),
                    cache.get("parser_version"),
                ),
            )
        jobs = {
            str(row[0]): {
                "state": str(row[1]), "attempt_count": int(row[2]),
                "retry_after": str(row[3] or ""), "queue_kind": str(row[4]),
            }
            for row in connection.execute(
                """SELECT code,state,attempt_count,retry_after,queue_kind
                   FROM coverage_job WHERE epoch_id=?""",
                (epoch_id,),
            )
        }
        candidates = []
        for row in eligible_rows:
            job = jobs.get(str(row["code"]))
            if not job or job["state"] not in {"pending", "failed"}:
                continue
            if job["attempt_count"] >= 3 or (
                job["retry_after"] and job["retry_after"] > now_text
            ):
                continue
            candidates.append({**row, **job})
        selected = _select_stratified(candidates, state["cells"], limit)
        if not selected:
            connection.commit()
            return {
                **{key: value for key, value in state.items() if key != "rows"},
                "selected": [], "reason": "no_due_financial_coverage_jobs",
            }
        inserted = connection.execute(
            "INSERT OR IGNORE INTO coverage_global_minute(minute) VALUES (?)", (minute,),
        ).rowcount
        if inserted == 0:
            connection.commit()
            return {
                **{key: value for key, value in state.items() if key != "rows"},
                "selected": [], "reason": "global_minute_budget_already_claimed",
            }
        output = []
        lease_until = (now + timedelta(minutes=5)).isoformat()
        for row in selected:
            token = uuid.uuid4().hex
            connection.execute(
                """UPDATE coverage_job
                   SET state='leased',lease_token=?,lease_until=?
                   WHERE epoch_id=? AND code=? AND state IN ('pending','failed')""",
                (token, lease_until, epoch_id, str(row["code"])),
            )
            output.append({
                "code": str(row["code"]), "name": str(row.get("name") or row["code"]),
                "coverage_cell": str(row["coverage_cell"]),
                "queue_kind": str(row["queue_kind"]), "lease_token": token,
            })
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        **{key: value for key, value in state.items() if key != "rows"},
        "selected": output, "reason": "financial_only_jobs_leased",
        "per_minute_limit": 4,
    }


def mark_coverage_started(
    db_path: Path, *, epoch_id: str, code: str, lease_token: str, now: datetime,
) -> bool:
    with sqlite3.connect(db_path, timeout=10) as connection, connection:
        connection.execute("PRAGMA busy_timeout = 10000")
        changed = connection.execute(
            """UPDATE coverage_job
               SET state='running',attempt_count=attempt_count+1,last_started=?
               WHERE epoch_id=? AND code=? AND state='leased' AND lease_token=?
                 AND lease_until IS NOT NULL AND lease_until>?""",
            (now.isoformat(), epoch_id, code, lease_token, now.isoformat()),
        ).rowcount
    return changed == 1


def finish_coverage_attempt(
    db_path: Path, *, epoch_id: str, code: str, lease_token: str,
    now: datetime, result: dict[str, Any],
) -> bool:
    success = result.get("status") == "success"
    after = result.get("after") if isinstance(result.get("after"), dict) else {}
    error = None if success else str(result.get("reason") or "unknown_failure")[:500]
    connection = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT attempt_count FROM coverage_job
               WHERE epoch_id=? AND code=? AND state='running' AND lease_token=?""",
            (epoch_id, code, lease_token),
        ).fetchone()
        if not row:
            connection.rollback()
            return False
        attempts = int(row[0])
        terminal = success or attempts >= 3
        state = "success" if success else "exhausted" if terminal else "failed"
        retry_after = None
        if not terminal:
            retry_after = (now + timedelta(hours=(1, 4, 24)[min(attempts - 1, 2)])).isoformat()
        changed = connection.execute(
            """UPDATE coverage_job
               SET state=?,lease_token=NULL,lease_until=NULL,last_finished=?,
                   retry_after=?,last_outcome=?,last_error=?,parser_after=?
               WHERE epoch_id=? AND code=? AND state='running' AND lease_token=?""",
            (
                state, now.isoformat(), retry_after,
                "financial_input_ready" if success else "financial_refresh_failed",
                error, after.get("parser_version"), epoch_id, code, lease_token,
            ),
        ).rowcount
        connection.commit()
        return changed == 1
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def record_coverage_snapshot(
    snapshot: dict[str, Any], db_path: Path, *, now: datetime,
) -> dict[str, Any]:
    """Persist formal score coverage changes; source refresh alone is not a gain."""
    state = coverage_state(snapshot)
    if state.get("status") != "ready":
        return {"status": state.get("status"), "recorded": False}
    rows = state["rows"]
    reasons = {}
    current = {}
    for row in rows:
        preview = row.get("preselection_professional_preview") or {}
        reason = str(preview.get("status") or "unknown")
        reasons[reason] = reasons.get(reason, 0) + 1
        current[str(row["code"])] = (int(row["coverage_comparable"]), reason)
    identity = {
        "epoch_id": state["epoch_id"],
        "generated_at": str(snapshot.get("generated_at") or ""),
        "states": sorted((code, *value) for code, value in current.items()),
    }
    snapshot_id = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=10) as connection, connection:
        _ensure_worker_tables(connection)
        existing = connection.execute(
            "SELECT snapshot_id FROM coverage_snapshot WHERE snapshot_id=?", (snapshot_id,),
        ).fetchone()
        if existing:
            return {"status": "ready", "recorded": False, "snapshot_id": snapshot_id}
        previous = {
            str(code): (int(comparable), str(reason or ""))
            for code, comparable, reason in connection.execute(
                "SELECT code,comparable,reason FROM coverage_code_state"
            )
        }
        common = set(previous) & set(current)
        gains = sum(previous[code][0] == 0 and current[code][0] == 1 for code in common)
        regressions = sum(previous[code][0] == 1 and current[code][0] == 0 for code in common)
        new_codes = len(set(current) - set(previous))
        removed_codes = len(set(previous) - set(current))
        connection.execute(
            """INSERT INTO coverage_snapshot VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                snapshot_id, now.isoformat(), str(state["epoch_id"]),
                int(state["comparable_count"]), int(state["backlog_count"]),
                int(gains), int(regressions), int(new_codes), int(removed_codes),
                json.dumps(reasons, ensure_ascii=False, sort_keys=True),
            ),
        )
        connection.execute("DELETE FROM coverage_code_state")
        connection.executemany(
            """INSERT INTO coverage_code_state(
                   code,snapshot_id,epoch_id,comparable,reason,last_seen_at
               ) VALUES (?,?,?,?,?,?)""",
            [
                (code, snapshot_id, str(state["epoch_id"]), value[0], value[1], now.isoformat())
                for code, value in current.items()
            ],
        )
    return {
        "status": "ready", "recorded": True, "snapshot_id": snapshot_id,
        "available": state["comparable_count"], "backlog": state["backlog_count"],
        "gross_gains": gains, "gross_regressions": regressions,
        "new_codes": new_codes, "removed_codes": removed_codes,
    }


def coverage_job_status(db_path: Path, epoch_id: str) -> dict[str, Any]:
    if not db_path.exists():
        return {"total": 0, "states": {}, "queue_kinds": {}}
    with sqlite3.connect(db_path, timeout=10) as connection:
        _ensure_worker_tables(connection)
        states = dict(connection.execute(
            "SELECT state,COUNT(*) FROM coverage_job WHERE epoch_id=? GROUP BY state",
            (epoch_id,),
        ))
        queues = dict(connection.execute(
            "SELECT queue_kind,COUNT(*) FROM coverage_job WHERE epoch_id=? GROUP BY queue_kind",
            (epoch_id,),
        ))
    return {
        "total": sum(int(value) for value in states.values()),
        "states": {str(key): int(value) for key, value in states.items()},
        "queue_kinds": {str(key): int(value) for key, value in queues.items()},
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    temporary.replace(path)


def run_once(
    *, snapshot_path: Path = _DEFAULT_SNAPSHOT,
    db_path: Path = _DEFAULT_DATABASE, report_path: Path = _DEFAULT_REPORT,
    now: datetime | None = None, limit: int = 4, workers: int = 3,
) -> dict[str, Any]:
    """Run one bounded financial-only coverage cycle and exit."""
    from .company_profile import refresh_financial_cache

    current = now or datetime.now(_SH_TZ)
    lock_path = db_path.with_suffix(db_path.suffix + ".worker.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "skipped", "reason": "worker_already_running"}
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot_audit = record_coverage_snapshot(snapshot, db_path, now=current)
            leased = lease_coverage_batch(snapshot, db_path, now=current, limit=limit)
            epoch_id = str(leased.get("epoch_id") or "")

            def execute(row: dict) -> dict:
                started_at = datetime.now(_SH_TZ)
                if not mark_coverage_started(
                    db_path, epoch_id=epoch_id, code=row["code"],
                    lease_token=row["lease_token"], now=started_at,
                ):
                    return {"code": row["code"], "status": "not_started"}
                result = refresh_financial_cache(row["code"])
                finish_coverage_attempt(
                    db_path, epoch_id=epoch_id, code=row["code"],
                    lease_token=row["lease_token"], now=datetime.now(_SH_TZ), result=result,
                )
                return {"code": row["code"], "status": result.get("status"), "reason": result.get("reason")}

            selected = leased.get("selected") or []
            with ThreadPoolExecutor(max_workers=max(1, min(3, int(workers)))) as executor:
                results = list(executor.map(execute, selected))
            payload = {
                "status": "completed", "generated_at": datetime.now(_SH_TZ).isoformat(),
                "snapshot_path": str(snapshot_path), "database_path": str(db_path),
                "lease_reason": leased.get("reason"), "selected": selected,
                "results": results, "snapshot_audit": snapshot_audit,
                "jobs": coverage_job_status(db_path, epoch_id) if epoch_id else {},
                "limits": {"per_minute": 4, "workers": max(1, min(3, int(workers)))},
                "scope": "financials_only_no_score_formula_change",
            }
            _atomic_write_json(report_path, payload)
            return payload
        except Exception as exc:
            payload = {
                "status": "failed", "generated_at": datetime.now(_SH_TZ).isoformat(),
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }
            _atomic_write_json(report_path, payload)
            return payload
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one bounded cycle")
    parser.add_argument("--snapshot", type=Path, default=_DEFAULT_SNAPSHOT)
    parser.add_argument("--database", type=Path, default=_DEFAULT_DATABASE)
    parser.add_argument("--report", type=Path, default=_DEFAULT_REPORT)
    arguments = parser.parse_args()
    if not arguments.once:
        parser.error("only --once is supported; this module is intentionally not a daemon")
    print(json.dumps(run_once(
        snapshot_path=arguments.snapshot,
        db_path=arguments.database,
        report_path=arguments.report,
    ), ensure_ascii=False, indent=2))


__all__ = [
    "coverage_state", "claim_coverage_batch", "lease_coverage_batch",
    "mark_coverage_started", "finish_coverage_attempt",
    "record_coverage_snapshot", "coverage_job_status", "run_once",
]


if __name__ == "__main__":
    _main()
