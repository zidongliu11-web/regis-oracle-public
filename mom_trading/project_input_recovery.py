"""Read actual local inputs without creating databases or admitting strategies.

The explicit restore command imports frozen registry bytes only. It cannot
authenticate an archive, recover missing recipes, or validate historical alpha.
"""
from __future__ import annotations

import hashlib
import ast
import json
import os
import re
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

SCHEMA = "project_input_recovery.v1"
REGISTRY = "factor_system/registry/registry.db"
MARKET_STORE = "data/quant_data/quant_data.sqlite3"
PANEL_ROOT = "research/backtest_project/data"
PANELS = ("universe_daily.parquet", "pit_fundamentals.parquet", "stock_meta.parquet")
MARKET_TABLES = ("daily_kline", "minute_kline", "fundamentals", "fund_flow",
                 "global_asset", "data_health", "data_versions")
MAX_REGISTRY_BYTES = 256 * 1024 * 1024
MAX_AUDIT_BYTES = 32 * 1024 * 1024
PUBLIC_STATUSES = ("candidate", "validated", "registered", "backtesting",
                   "historical_pass", "production", "rejected", "invalid",
                   "invalidated", "frozen")


def _redirect(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _fixed(root: Path, relative: str) -> Path:
    root = Path(root).absolute()
    if any(_redirect(p) for p in (root, *root.parents)):
        raise ValueError("path_redirect")
    current = root
    for part in relative.split("/"):
        if not part or part in (".", "..") or "\\" in part or ":" in part:
            raise ValueError("path_invalid")
        current = current / part
        if _redirect(current):
            raise ValueError("path_redirect")
    return current


@lru_cache(maxsize=1)
def _registry_schema_columns() -> dict:
    # Parse the checked-in schema without importing the runtime or initializing it.
    schema_path = Path(__file__).resolve().parents[1] / "factor_system/registry.py"
    parsed = ast.parse(schema_path.read_text(encoding="utf-8"))
    definition = next(node for node in parsed.body if isinstance(node, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "SCHEMA" for t in node.targets))
    with closing(sqlite3.connect(":memory:")) as expected:
        expected.executescript(ast.literal_eval(definition.value))
        names = [r[0] for r in expected.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {t: {r[1] for r in expected.execute(f'PRAGMA table_info("{t}")')}
                for t in names}


def _database(path: Path, tables: tuple[str, ...], *, max_bytes=MAX_AUDIT_BYTES) -> dict:
    if not path.is_file():
        return {"state": "missing", "rows": None, "table_counts": {}}
    unavailable = {"rows": None, "table_counts": {}}
    sidecars = tuple(Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal"))
    try:
        if any(p.exists() for p in sidecars):
            return {"state": "consistent_snapshot_required", **unavailable}
        with path.open("rb") as original:
            before = os.fstat(original.fileno())
            if before.st_size > max_bytes:
                return {"state": "inspection_size_limit", **unavailable}
            snapshot = original.read(max_bytes + 1)
            after = os.fstat(original.fileno())
        if len(snapshot) > max_bytes:
            return {"state": "inspection_size_limit", **unavailable}
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or any(
                p.exists() for p in sidecars):
            return {"state": "consistent_snapshot_required", **unavailable}
        if snapshot[18:20] == b"\x02\x02":
            # WAL bytes are not a complete detached snapshot. Never open the source
            # through SQLite, which can create sidecars even with mode=ro.
            return {"state": "consistent_snapshot_required", **unavailable}
        if not snapshot:
            return {"state": "schema_missing", **unavailable}
        deadline = time.monotonic() + 0.75
        with closing(sqlite3.connect(":memory:")) as connection:
            if not hasattr(connection, "deserialize"):
                return {"state": "snapshot_inspection_unsupported", **unavailable}
            connection.deserialize(snapshot)
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            connection.execute("PRAGMA query_only=ON")
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                return {"state": "invalid_database", "rows": None, "table_counts": {}}
            available = {r[0] for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not set(tables).issubset(available):
                return {"state": "schema_missing", "rows": None, "table_counts": {}}
            if tables == ("factors",):
                columns = {r[1] for r in connection.execute("PRAGMA table_info(factors)")}
                if not {"factor_id", "status"}.issubset(columns):
                    return {"state": "schema_missing", "rows": None, "table_counts": {}}
            counts = {t: connection.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                      for t in tables}
            result = {"state": "nonempty_unvalidated" if any(counts.values()) else "empty",
                      "rows": counts.get("factors"), "table_counts": counts}
            if tables == ("factors",):
                # Only counts, never expressions, identifiers, account data or secrets.
                placeholders = ",".join("?" for _ in PUBLIC_STATUSES)
                result["status_counts"] = dict(connection.execute(
                    f"SELECT CASE WHEN status IN ({placeholders}) THEN status ELSE 'unknown' END "
                    "AS public_status, COUNT(*) FROM factors GROUP BY public_status", PUBLIC_STATUSES))
                expected = _registry_schema_columns()
                result["registry_schema_compatible"] = all(
                    t in available and columns.issubset({r[1] for r in connection.execute(
                        f'PRAGMA table_info("{t}")')}) for t, columns in expected.items())
            result["snapshot_consistency_verified"] = False
            return result
    except (OSError, sqlite3.Error):
        return {"state": "unreadable_or_timed_out", "rows": None, "table_counts": {}}


def _panel(root: Path, name: str) -> dict:
    relative = f"{PANEL_ROOT}/{name}"
    result = {"path": relative, "state": "missing", "source_time": None}
    try:
        path = _fixed(root, relative)
        if path.is_file():
            result["bytes"] = path.stat().st_size
            if result["bytes"] < 12:
                result["state"] = "invalid_parquet_bytes"
                return result
            with path.open("rb") as stream:
                first = stream.read(4)
                stream.seek(-4, os.SEEK_END)
                last = stream.read(4)
            result["state"] = ("bytes_present_unvalidated" if first == last == b"PAR1"
                               else "invalid_parquet_bytes")
    except ValueError:
        result["state"] = "path_redirect"
    except OSError:
        result["state"] = "unreadable"
    return result


def audit(root: Path) -> dict:
    """Filesystem/read-only SQL observations, never source/PIT/economic proof."""
    root = Path(root)
    result = {"schema": SCHEMA, "observed_at_utc": datetime.now(timezone.utc).isoformat(),
              "execution_location": "local", "source_time": None,
              "writes_performed": False, "closed_loop_ready": False,
              "full_a_data_verified": False, "strategy_admission_ready": False,
              "economic_target_A_validated": False, "sequential_rl_ready": False}
    for key, relative, tables in (("registry", REGISTRY, ("factors",)),
                                  ("market_store", MARKET_STORE, MARKET_TABLES)):
        try:
            result[key] = {"path": relative, **_database(_fixed(root, relative), tables)}
        except (OSError, ValueError):
            result[key] = {"path": relative, "state": "path_unavailable",
                           "rows": None, "table_counts": {}}
    result["panels"] = [_panel(root, name) for name in PANELS]
    try:
        from .mainline_archive_audit import audit as legacy_audit
        # Reject redirects before the legacy reader; its result is byte binding only.
        _fixed(root, "research/STATE_CONDITIONED_MAINLINE_SWING_V1_TERMINAL_ARCHIVE.json")
        result["legacy"] = legacy_audit(root)
    except (OSError, ValueError):
        result["legacy"] = {"status": "unavailable", "protocols": {}, "references": {}}
    blockers = []
    registry = result["registry"]
    if registry["state"] == "missing":
        blockers.append("原因子库缺失，历史因子数量未知")
    elif registry["state"] == "empty":
        blockers.append("因子库存在但为空，未恢复历史记录")
    elif registry["state"] != "nonempty_unvalidated":
        blockers.append("因子库无法核读或须取得一致快照")
    elif not registry.get("registry_schema_compatible"):
        blockers.append("因子记录已见，库结构与当前注册中心不兼容")
    else:
        blockers.append("因子记录已见，原件来源、配方与研究证据仍待核验")
    missing = sum(p["state"] == "missing" for p in result["panels"])
    unvalidated = sum(p["state"] == "bytes_present_unvalidated" for p in result["panels"])
    invalid = len(PANELS) - missing - unvalidated
    if missing:
        blockers.append(f"历史面板缺失 {missing}/3")
    if unvalidated:
        blockers.append(f"面板字节已见 {unvalidated}/3，时点与输入审计仍待验证")
    if invalid:
        blockers.append(f"历史面板空、损坏或无法核读 {invalid}/3")
    market = result["market_store"]
    if market["state"] == "empty":
        blockers.append("本机行情库七张表均无数据")
    elif market["state"] != "nonempty_unvalidated":
        blockers.append("本机行情库缺失或无法核读")
    else:
        blockers.append("行情记录已见，覆盖、许可、时点与新鲜度尚待验证")
    protocols = result["legacy"].get("protocol_reproduction", {}).get("files", {})
    references = result["legacy"].get("references", {})
    missing_protocols = sum(p.get("state") == "missing" for p in protocols.values())
    missing_results = sum(p.get("state") == "missing" for p in references.values())
    if missing_protocols or missing_results:
        results_text = (f"{missing_results} 份结果原件" if len(references) == 3
                        else "结果绑定未知")
        blockers.append(f"旧研究缺 {missing_protocols} 份协议、{results_text}")
    else:
        blockers.append("旧研究原件尚须逐项核验与复算，当前未证明收益")
    result.update(status="inputs_incomplete", blockers=blockers,
                  summary="；".join(blockers) + "。",
                  next_action="取得原件与合法数据来源后逐项恢复；缺失时不计算可成交净超额或发行新买点。")
    return result


def _sha(path: Path) -> str:
    if path.stat().st_size > MAX_REGISTRY_BYTES:
        raise ValueError("snapshot_oversized")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def restore_registry(root: Path, source: Path, expected_sha256: str) -> dict:
    """Explicit, no-overwrite import of a nonempty frozen SQLite snapshot.

    The supplied digest must be recorded before import. Matching bytes are
    not evidence that the archive is genuine or that its factors are usable.
    """
    if not re.fullmatch("[0-9a-f]{64}", expected_sha256):
        raise ValueError("expected_sha256_required")
    source = Path(source).absolute()
    if any(_redirect(p) for p in (source, *source.parents)):
        raise ValueError("source_redirect")
    if any(Path(str(source) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("frozen_snapshot_required")
    destination = _fixed(Path(root), REGISTRY)
    if destination.exists():
        raise ValueError("destination_exists")
    if _sha(source) != expected_sha256:
        raise ValueError("source_hash_mismatch")
    inspection = _database(source, ("factors",), max_bytes=MAX_REGISTRY_BYTES)
    if inspection["state"] != "nonempty_unvalidated":
        raise ValueError("nonempty_readable_registry_required")
    if not inspection.get("registry_schema_compatible"):
        raise ValueError("compatible_registry_schema_required")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _fixed(Path(root), REGISTRY)
    fd, name = tempfile.mkstemp(prefix=".frozen-registry-", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as target, source.open("rb") as original:
            consumed = 0
            for chunk in iter(lambda: original.read(1024 * 1024), b""):
                consumed += len(chunk)
                if consumed > MAX_REGISTRY_BYTES:
                    raise ValueError("snapshot_oversized")
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if _sha(temporary) != expected_sha256 or _sha(source) != expected_sha256:
            raise ValueError("source_changed_during_import")
        if any(Path(str(source) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise ValueError("snapshot_changed_during_import")
        # Publish without replacing a destination created since preflight.
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {"schema": "project_registry_restore.v1", "status": "frozen_bytes_imported",
            "destination": REGISTRY, "sha256": expected_sha256,
            "rows": inspection["rows"], "status_counts": inspection["status_counts"],
            "registry_schema_compatible": True,
            "source_authenticity_verified": False, "production_eligible": False,
            "recipes_and_research_evidence_verified": False}
