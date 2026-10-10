"""Persistent, source-only review queue for public factor research leads.

The queue bridges metadata discovery to semantic source review.  It never
registers a factor and never reads prices, returns, IC, or backtest outputs.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path

from . import config


QUEUE_PATH = config.STATE_DIR / "public_source_review_queue.json"
REVIEW_SLA = dt.timedelta(hours=6)
_PENDING_STATUSES = {
    "unreviewed_research_source": "paper_source_review",
    "formula_pending": "paper_formula_resolution",
    "source_contract_pending": "paper_input_contract_review",
}
_QUEUE_POLICY = {
    "source_review_only": True,
    "factor_registration_allowed": False,
    "factor_computation_allowed": False,
    "mining_allowed": False,
    "backtest_allowed": False,
    "production_allowed": False,
    "recommendation_allowed": False,
    "automatic_trading_allowed": False,
}


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _stamp(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.astimezone()
    return value.isoformat(timespec="seconds")


def _parse_stamp(value: object) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def _source_state(path: Path) -> tuple[dict, str]:
    body = path.read_bytes()
    value = json.loads(body.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("source scout state is not an object")
    policy = value.get("policy")
    if not isinstance(policy, dict) or any((
        policy.get("research_only") is not True,
        policy.get("automatic_candidate_registration") is not False,
        policy.get("automatic_backtest") is not False,
        policy.get("automatic_production") is not False,
        policy.get("automatic_factor_computation") is not False,
        policy.get("automatic_repository_candidate_registration") is not False,
        policy.get("automatic_repository_execution") is not False,
    )):
        raise ValueError("source scout authority boundary is invalid")
    return value, hashlib.sha256(body).hexdigest()


def _empty_queue() -> dict:
    return {
        "schema_version": 1,
        "updated_at": None,
        "source_state_sha256": None,
        "policy": dict(_QUEUE_POLICY),
        "tasks": {},
        "summary": {
            "task_count": 0,
            "pending_count": 0,
            "overdue_count": 0,
            "resolved_count": 0,
            "paper_pending_count": 0,
            "repository_pending_count": 0,
        },
    }


def _load_queue(path: Path) -> dict:
    if not path.exists():
        return _empty_queue()
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("policy") != _QUEUE_POLICY
        or not isinstance(value.get("tasks"), dict)
    ):
        raise ValueError("source review queue is invalid")
    return value


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    os.replace(temporary, path)


def _task_id(kind: str, source_id: str) -> str:
    digest = hashlib.sha256(f"{kind}\x1f{source_id}".encode()).hexdigest()[:20]
    return f"source_review_{digest}"


def _paper_work(item: dict) -> dict | None:
    status = str(item.get("status") or "unreviewed_research_source")
    kind = _PENDING_STATUSES.get(status)
    source_id = str(item.get("scout_id") or "").strip()
    if not kind or not source_id:
        return None
    work = {
        "task_id": _task_id("paper", source_id),
        "kind": kind,
        "source_id": source_id,
        "identity": str(item.get("identity") or item.get("doi") or source_id),
        "title": str(item.get("title") or source_id),
        "source_url": str(
            item.get("primary_source_url")
            or item.get("open_access_url")
            or item.get("url")
            or ""
        ),
        "immutable_ref": str(item.get("doi") or item.get("openalex_id") or ""),
        "source_status": status,
        "source_first_seen_at": item.get("first_seen_at"),
    }
    resolution = _audited_external_gap_resolution(item)
    if resolution:
        work["_immediate_resolution"] = resolution
        work["_review_artifact"] = item.get("input_audit_artifact")
    return work


def _audited_external_gap_resolution(item: dict) -> str | None:
    """Close human review once a frozen audit names only external input gaps."""
    gaps = item.get("input_contract_gaps")
    if not (
        item.get("status") == "source_contract_pending"
        and isinstance(gaps, list) and gaps
        and item.get("input_audited_at")
        and item.get("input_audit_artifact")
        and item.get("input_audit_artifact_sha256")
        and item.get("runnable") is False
        and item.get("factor_values_computed") is False
        and item.get("outcomes_read") is False
    ):
        return None
    return "reviewed_input_contract_external_evidence_pending"


def _repository_work(item: dict) -> dict | None:
    if str(item.get("status") or "") != "unreviewed_repository_source":
        return None
    source_id = str(item.get("scout_id") or "").strip()
    if not source_id:
        return None
    return {
        "task_id": _task_id("repository", source_id),
        "kind": "repository_source_review",
        "source_id": source_id,
        "identity": str(item.get("identity") or item.get("repository") or source_id),
        "title": str(item.get("repository") or item.get("name") or source_id),
        "source_url": str(item.get("url") or ""),
        "immutable_ref": str(item.get("reviewed_source_commit_sha") or ""),
        "source_status": "unreviewed_repository_source",
        "source_first_seen_at": item.get("first_seen_at"),
    }


def _active_work(source: dict) -> tuple[dict[str, dict], dict[str, dict]]:
    work: dict[str, dict] = {}
    all_sources: dict[str, dict] = {}
    for item in source.get("candidates") or []:
        if not isinstance(item, dict) or not item.get("scout_id"):
            continue
        all_sources[str(item["scout_id"])] = item
        row = _paper_work(item)
        if row:
            work[row["task_id"]] = row
    for item in source.get("repository_discoveries") or []:
        if not isinstance(item, dict) or not item.get("scout_id"):
            continue
        all_sources[str(item["scout_id"])] = item
        row = _repository_work(item)
        if row:
            work[row["task_id"]] = row
    return work, all_sources


def _summary(tasks: dict[str, dict], now: dt.datetime) -> dict:
    pending = [
        item for item in tasks.values()
        if isinstance(item, dict) and item.get("status") == "pending_source_review"
    ]
    overdue = [
        item for item in pending
        if (due := _parse_stamp(item.get("sla_due_at"))) is not None and now > due
    ]
    return {
        "task_count": len(tasks),
        "pending_count": len(pending),
        "overdue_count": len(overdue),
        "resolved_count": sum(
            isinstance(item, dict) and item.get("status") == "resolved"
            for item in tasks.values()
        ),
        "paper_pending_count": sum(
            str(item.get("kind") or "").startswith("paper_") for item in pending
        ),
        "repository_pending_count": sum(
            item.get("kind") == "repository_source_review" for item in pending
        ),
    }


def sync(
    *, source_state_path: Path, queue_path: Path = QUEUE_PATH,
    now: dt.datetime | None = None,
) -> dict:
    """Synchronize the source inbox into an idempotent, non-executable queue."""
    now = now or _now()
    source, source_sha = _source_state(source_state_path)
    queue = _load_queue(queue_path)
    tasks = {
        str(key): dict(value) for key, value in queue["tasks"].items()
        if isinstance(value, dict)
    }
    active, all_sources = _active_work(source)
    changed = queue.get("source_state_sha256") != source_sha

    for task_id, work in active.items():
        immediate_resolution = work.pop("_immediate_resolution", None)
        immediate_artifact = work.pop("_review_artifact", None)
        previous = tasks.get(task_id)
        if immediate_resolution:
            timestamp = _stamp(now)
            resolved = {
                **work,
                "status": "resolved",
                "enqueued_at": (
                    previous.get("enqueued_at") if previous else timestamp
                ),
                "sla_due_at": (
                    previous.get("sla_due_at") if previous else
                    _stamp(now + REVIEW_SLA)
                ),
                "source_state_sha256_at_enqueue": (
                    previous.get("source_state_sha256_at_enqueue")
                    if previous else source_sha
                ),
                "last_seen_source_state_sha256": source_sha,
                "resolved_at": (
                    previous.get("resolved_at")
                    if previous and previous.get("resolution_status") == immediate_resolution
                    else timestamp
                ),
                "resolution_status": immediate_resolution,
                "review_artifact": immediate_artifact,
                "research_only": True,
                "factor_registration_allowed": False,
                "factor_computation_allowed": False,
                "mining_allowed": False,
                "backtest_allowed": False,
                "production_allowed": False,
            }
            if resolved != previous:
                tasks[task_id] = resolved
                changed = True
            continue
        if previous is None:
            enqueued_at = _stamp(now)
            tasks[task_id] = {
                **work,
                "status": "pending_source_review",
                "enqueued_at": enqueued_at,
                "sla_due_at": _stamp(now + REVIEW_SLA),
                "source_state_sha256_at_enqueue": source_sha,
                "last_seen_source_state_sha256": source_sha,
                "resolved_at": None,
                "resolution_status": None,
                "review_artifact": None,
                "research_only": True,
                "factor_registration_allowed": False,
                "factor_computation_allowed": False,
                "mining_allowed": False,
                "backtest_allowed": False,
                "production_allowed": False,
            }
            changed = True
            continue
        refreshed = {
            **previous,
            **work,
            "status": "pending_source_review",
            "last_seen_source_state_sha256": source_sha,
            "resolved_at": None,
            "resolution_status": None,
            "review_artifact": None,
        }
        if refreshed != previous:
            tasks[task_id] = refreshed
            changed = True

    for task_id, task in list(tasks.items()):
        if task_id in active:
            continue
        source_item = all_sources.get(str(task.get("source_id") or ""))
        if source_item is None:
            continue
        resolution = str(source_item.get("status") or "")
        if resolution in _PENDING_STATUSES or resolution == "unreviewed_repository_source":
            continue
        previous_resolution = str(task.get("resolution_status") or "")
        resolved = {
            **task,
            "status": "resolved",
            "resolved_at": (
                task.get("resolved_at")
                if previous_resolution == resolution else _stamp(now)
            ),
            "resolution_status": resolution or "reviewed",
            "source_status": resolution or "reviewed",
            "review_artifact": (
                source_item.get("closure_artifact")
                or source_item.get("input_audit_artifact")
                or source_item.get("review_artifact")
            ),
            "last_seen_source_state_sha256": source_sha,
        }
        if resolved != task:
            tasks[task_id] = resolved
            changed = True

    summary = _summary(tasks, now)
    if summary != queue.get("summary"):
        changed = True
    result = {
        "schema_version": 1,
        "updated_at": queue.get("updated_at"),
        "source_state_sha256": source_sha,
        "policy": dict(_QUEUE_POLICY),
        "tasks": tasks,
        "summary": summary,
    }
    if changed or not queue_path.exists():
        result["updated_at"] = _stamp(now)
        _write(queue_path, result)
    return result


def public_status(
    path: Path = QUEUE_PATH, *, now: dt.datetime | None = None,
) -> dict:
    """Return a bounded queue view for UI/API status surfaces."""
    empty = _empty_queue()
    try:
        if not path.is_file():
            raise FileNotFoundError(path)
        queue = _load_queue(path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            **empty,
            "status": "unavailable",
            "error": f"{type(exc).__name__}: source review queue invalid",
            "summary": None,
            "tasks": [],
        }
    summary = _summary(queue["tasks"], now or _now())
    tasks = [dict(item) for item in queue["tasks"].values()]
    tasks.sort(key=lambda item: str(item.get("enqueued_at") or ""), reverse=True)
    return {
        **queue,
        "summary": summary,
        "status": "review_pending" if summary["pending_count"] else "idle",
        "tasks": tasks[:20],
    }


__all__ = ["QUEUE_PATH", "public_status", "sync"]
