"""一次性切换到严格 research epoch，并可恢复地隔离旧证据。

CLI: ``python -m factor_system.epoch_migration --apply``
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

from . import config
from . import daemon
from . import registry
from . import source_gate


INVALIDATION_REASON = (
    "旧研究证据失效：旧因子无可核验来源（source-free）、无回测前预注册，"
    "且旧面板股票池/增量源口径不一致；production evaluator、逐假设 LOND "
    "与内容寻址制品契约也已升级。原件完整归档，仅供审计，不计入当前严格 epoch。"
)
EVALUATOR_CONTRACT_INVALIDATION_REASON = (
    "研究评估器契约升级：L0 截面变化分母从全部发现日修正为真实合资格截面日；"
    "旧结论原件完整归档并标记为不可用于新 epoch，不能自动解释为通过。"
)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    body = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with tmp.open("w", encoding="utf-8") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _backup_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _archive_sources() -> list[Path]:
    """列出迁移会失效或后续批次可能覆盖的全部研究资产。"""
    files = [
        config.STATE_DIR / "mining.json",
        config.STATE_DIR / "backtest.json",
        config.STATE_DIR / "daemon.json",
        config.STATE_DIR / "daemon_runtime.json",
        config.REGISTRY_DIR / "novelty_archive.json",
        config.SOURCE_CATALOG,
        config.PROJECT_ROOT / "research" / "strategy_batch_state.json",
        config.PROJECT_ROOT / "research" / "strategy_batch_results.json",
    ]
    trees = [
        config.REGISTRY_DIR / "recipes",
        config.REGISTRY_DIR / "evidence",
        config.REGISTRY_DIR / "production",
        config.PROJECT_ROOT / "research" / "user_strategies",
        config.PROJECT_ROOT / "research" / "strategy_diagnostics",
        config.PROJECT_ROOT / "reports" / "user_strategy_backtests",
        config.PROJECT_ROOT / "research" / "data" / "user_strategy_backtests",
    ]
    if config.SOURCE_CATALOG.is_file():
        catalog = source_gate.load_catalog(config.SOURCE_CATALOG)
        verification = source_gate.verify_catalog(
            catalog, root=config.PROJECT_ROOT,
        )
        if not verification["valid"]:
            raise RuntimeError(
                "source catalog 无法安全归档: "
                + ",".join(verification["errors"])
            )
        for candidate in catalog.get("candidates") or []:
            preregistration = (
                candidate.get("preregistration")
                if isinstance(candidate, dict) else None
            ) or {}
            implementation = (
                candidate.get("implementation")
                if isinstance(candidate, dict) else None
            ) or {}
            evidence_paths = [
                preregistration.get("protocol_path"),
                preregistration.get("admission_path"),
                implementation.get("audit_path"),
            ]
            for value in evidence_paths:
                if not isinstance(value, str) or not value:
                    continue
                path = (config.PROJECT_ROOT / value).resolve()
                try:
                    path.relative_to(config.PROJECT_ROOT.resolve())
                except ValueError as exc:
                    raise RuntimeError(
                        f"source catalog evidence 越出项目目录: {value}"
                    ) from exc
                files.append(path)
    for root in trees:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise RuntimeError(f"迁移资产不接受符号链接: {path}")
            if path.is_file():
                files.append(path)
    # 相对路径去重；所有恢复目标都必须位于项目目录内。
    unique: dict[str, Path] = {}
    for path in files:
        if not path.is_file():
            continue
        try:
            rel = str(path.relative_to(config.PROJECT_ROOT))
        except ValueError as exc:
            raise RuntimeError(f"迁移资产越出项目目录: {path}") from exc
        unique[rel] = path
    return [unique[key] for key in sorted(unique)]


def _copy_verified_assets(archive_dir: Path) -> list[dict]:
    assets = []
    for source in _archive_sources():
        rel = source.relative_to(config.PROJECT_ROOT)
        target = archive_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        source_sha = _sha256(source)
        shutil.copy2(source, target)
        target_sha = _sha256(target)
        if target_sha != source_sha:
            raise RuntimeError(f"迁移归档哈希校验失败: {rel}")
        assets.append({
            "source": str(rel),
            "archive": str(rel),
            "sha256": source_sha,
            "size": int(source.stat().st_size),
        })
    return assets


def verify_archive(archive_dir: Path) -> dict:
    """逐文件复核完整归档；只有 complete manifest 才可声明可恢复。"""
    manifest_path = Path(archive_dir) / "manifest.json"
    manifest = _read_json(manifest_path)
    if (manifest.get("status") not in {"snapshot_complete", "complete"}
            or not manifest.get("recoverable")):
        raise RuntimeError(f"迁移归档未完成: {archive_dir}")
    checked = 0
    for asset in manifest.get("assets", []):
        path = Path(archive_dir) / asset["archive"]
        if (not path.is_file() or int(path.stat().st_size) != int(asset["size"])
                or _sha256(path) != asset["sha256"]):
            raise RuntimeError(f"迁移归档资产损坏: {asset.get('archive')}")
        checked += 1
    db_path = Path(archive_dir) / manifest["backup_db"]
    if (not db_path.is_file()
            or _sha256(db_path) != manifest.get("backup_db_sha256")):
        raise RuntimeError("迁移归档数据库损坏")
    return {"ok": True, "checked_assets": checked, "backup_db": str(db_path)}


def _completed_epoch_archive() -> Path | None:
    archives = config.FACTOR_ROOT / "archives"
    if not archives.is_dir():
        return None
    for manifest_path in sorted(archives.glob("*_strict_epoch/manifest.json"), reverse=True):
        manifest = _read_json(manifest_path)
        if (manifest.get("status") == "complete"
                and manifest.get("recoverable") is True
                and manifest.get("research_epoch") == config.RESEARCH_EPOCH):
            return manifest_path.parent
    return None


def _unfinished_snapshot_epoch_archive() -> Path | None:
    """返回当前 epoch 已生成备份但未 complete 的归档。"""
    archives = config.FACTOR_ROOT / "archives"
    if not archives.is_dir():
        return None
    for manifest_path in sorted(archives.glob("*_strict_epoch/manifest.json"), reverse=True):
        manifest = _read_json(manifest_path)
        if (manifest.get("research_epoch") == config.RESEARCH_EPOCH
                and manifest.get("status") != "complete"
                and (manifest.get("backup_db")
                     or (manifest_path.parent / "registry.db").is_file()
                     or manifest.get("status") in {
                         "snapshot_complete", "verification_failed",
                     })):
            return manifest_path.parent
    return None


_DAEMON_REQUIRED_SCHEMA = {
    "factors": {
        "factor_id", "name", "family", "formula", "description", "direction",
        "data_requires", "kind", "params_json", "status", "is_ic_mean",
        "is_ic_ir", "oos_ic_mean", "oos_ic_ir", "ic_retention",
        "long_short_annualized", "p_value", "backtest_annual_return",
        "backtest_sharpe", "backtest_max_drawdown", "backtest_win_rate",
        "backtest_excess_annual", "backtest_benchmark_annual",
        "backtest_fold_pass_rate", "backtest_t_stat", "backtest_evidence",
        "raw_ic_mean", "raw_ic_ir", "neutralize_mode", "monotonic_aligned",
        "ic_half_life", "turnover_ratio", "capacity_estimate", "incremental_ic",
        "diag_json", "research_epoch", "validation_data_key", "backtest_epoch",
        "oos_test_p_value", "backtest_gate_passed", "evidence_path",
        "evidence_sha256", "sealed_at", "sealed_data_end",
        "sealed_recipe_sha256", "evaluator_sealed_at",
        "evaluator_sealed_data_end", "evaluator_fingerprint",
        "production_score_path", "production_score_sha256",
        "production_data_key", "production_as_of",
        "production_evaluator_sha256", "created_at", "updated_at",
        "research_record_json", "research_record_sha256", "status_reason",
    },
    "factor_tests": {
        "epoch", "factor_id", "hypothesis_order", "p_value", "alpha_i",
        "gate_passed", "online_reject", "tested_at", "data_key",
        "p_value_method", "evidence_path", "evidence_sha256",
        "evaluator_fingerprint",
    },
    "test_epochs": {
        "epoch", "q", "dependent", "method", "method_version",
        "p_value_method", "initial_data_key", "created_at",
        "evaluator_fingerprint",
    },
    "test_observation_latches": {
        "epoch", "factor_id", "data_key", "p_value_method", "producer_type",
        "context_json", "context_sha256", "reserved_at",
        "evaluator_fingerprint",
    },
    "mining_batches": {
        "batch_id", "research_epoch", "data_key", "family_json",
        "family_sha256", "context_json", "progress_json", "fdr_input_json",
        "fdr_input_sha256", "status", "created_at", "completed_at",
    },
    "production_catalog": {
        "catalog_id", "research_epoch", "catalog_version", "manifest_json",
        "updated_at",
    },
}


def verify_daemon_epoch_ready() -> dict:
    """只读确认 daemon 可进入当前严格 epoch；不建库、不迁表、不改状态。"""
    archive_dir = _completed_epoch_archive()
    if archive_dir is None:
        raise RuntimeError(
            f"research epoch {config.RESEARCH_EPOCH} 尚无 complete+recoverable 迁移归档"
        )
    manifest = _read_json(archive_dir / "manifest.json")
    if (manifest.get("status") != "complete"
            or manifest.get("recoverable") is not True
            or manifest.get("research_epoch") != config.RESEARCH_EPOCH):
        raise RuntimeError(f"严格 epoch 迁移归档状态无效: {archive_dir}")
    archive_verification = verify_archive(archive_dir)

    db_path = config.REGISTRY_DIR / "registry.db"
    if not db_path.is_file():
        raise RuntimeError(f"因子注册表不存在: {db_path}")
    connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        for table, required in _DAEMON_REQUIRED_SCHEMA.items():
            columns = {
                str(row["name"])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            }
            missing = sorted(required - columns)
            if missing:
                raise RuntimeError(
                    f"严格 epoch 注册表 schema 不完整: {table} 缺少 {', '.join(missing)}"
                )
        legacy = connection.execute(
            """SELECT factor_id,status,research_epoch FROM factors
                 WHERE (research_epoch IS NULL OR research_epoch<>?)
                   AND status<>'invalidated'
                 ORDER BY factor_id LIMIT 5""",
            (config.RESEARCH_EPOCH,),
        ).fetchall()
        if legacy:
            sample = ", ".join(
                f"{row['factor_id']}[{row['status']}]" for row in legacy
            )
            raise RuntimeError(f"仍有未失效的旧 epoch 因子: {sample}")
    finally:
        connection.close()
    return {
        "ok": True,
        "research_epoch": config.RESEARCH_EPOCH,
        "archive_dir": str(archive_dir),
        "archive": archive_verification,
    }


def migrate_strict_epoch(timestamp: str | None = None) -> dict:
    """完整归档旧研究资产后失效旧结论；任一哈希失败则不宣称可恢复。"""
    lock = daemon._acquire_daemon_lock()
    if lock is None:
        raise RuntimeError("factor daemon 仍在运行；请先停止再迁移")
    mutation_lock = None
    try:
        # 与网站策略批次使用同一个 flock 文件。迁移必须同时排除 daemon 与
        # Web 研究写入，不能一边复制证据一边让 batch 覆盖它。
        mutation_lock = daemon._acquire_daemon_lock(
            config.STATE_DIR / "research_mutation.lock"
        )
        if mutation_lock is None:
            raise RuntimeError("策略研究批次仍在运行；请等待结束后再迁移")
        completed = _completed_epoch_archive()
        if completed is not None:
            raise RuntimeError(
                f"当前 research epoch 已完成迁移，拒绝重复失效: {completed}"
            )
        pending_snapshot = _unfinished_snapshot_epoch_archive()
        if pending_snapshot is not None:
            raise RuntimeError(
                "当前 research epoch 存在未完成的已生成快照，拒绝从可能已部分"
                "失效的 live 状态创建第二份快照；请从原快照恢复，或人工核验后 resume "
                f"原迁移: {pending_snapshot}"
            )
        daemon_state = _read_json(config.STATE_DIR / "daemon.json")
        state_epoch = daemon_state.get("research_epoch")
        previous_epoch = (
            str(state_epoch) if isinstance(state_epoch, str)
            and state_epoch and state_epoch != config.RESEARCH_EPOCH else None
        )
        invalidation_reason = (
            EVALUATOR_CONTRACT_INVALIDATION_REASON
            if previous_epoch else INVALIDATION_REASON
        )
        stamp = timestamp or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        archive_dir = config.FACTOR_ROOT / "archives" / f"{stamp}_strict_epoch"
        if archive_dir.exists():
            raise FileExistsError(f"迁移归档已存在: {archive_dir}")
        archive_dir.mkdir(parents=True)

        in_progress = {
            "schema_version": 2,
            "status": "in_progress",
            "recoverable": False,
            "started_at": _now(),
            "research_epoch": config.RESEARCH_EPOCH,
            "previous_research_epoch": previous_epoch,
            "migration_contract_id": getattr(
                config, "L0_CONTRACT_VERSION", "legacy-strict-epoch-migration",
            ),
            "reason": invalidation_reason,
        }
        _atomic_json(archive_dir / "manifest.json", in_progress)

        db = config.REGISTRY_DIR / "registry.db"
        if not db.is_file():
            raise FileNotFoundError(f"因子注册表不存在: {db}")
        backup_db = archive_dir / "registry.db"
        _backup_sqlite(db, backup_db)
        backup_db_sha256 = _sha256(backup_db)
        assets = _copy_verified_assets(archive_dir)
        archived_files = [asset["source"] for asset in assets]

        # 在改任何线上状态之前先提交一份完整、可校验的恢复映射。若进程随后
        # 在失效 DB/JSON 的任意一步崩溃，snapshot_complete 仍足以逐字节恢复。
        snapshot_manifest = {
            **in_progress,
            "status": "snapshot_complete",
            "snapshot_completed_at": _now(),
            "backup_db": "registry.db",
            "backup_db_sha256": backup_db_sha256,
            "archived_files": archived_files,
            "assets": assets,
            "restore_scope": "把 registry.db 与 assets 按 source 路径还原即可恢复迁移前字节",
            "recoverable": True,
        }
        _atomic_json(archive_dir / "manifest.json", snapshot_manifest)
        snapshot_verification = verify_archive(archive_dir)

        strategies_dir = config.PROJECT_ROOT / "research" / "user_strategies"
        strategy_paths = (sorted(strategies_dir.glob("user-*.json"))
                          if strategies_dir.is_dir() else [])

        now = _now()
        c = sqlite3.connect(str(db))
        try:
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(registry.SCHEMA)
            registry._migrate(c)
            if previous_epoch:
                rows = c.execute(
                    "SELECT factor_id,status,status_reason FROM factors "
                    "WHERE research_epoch=? AND status<>'invalidated'",
                    (previous_epoch,),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT factor_id,status,status_reason FROM factors "
                    "WHERE research_epoch IS NULL OR research_epoch<>?",
                    (config.RESEARCH_EPOCH,),
                ).fetchall()
            for row in rows:
                registry._append_factor_trial_snapshot(
                    c, row["factor_id"],
                    event="pre_evaluator_contract_invalidation",
                    details={
                        "previous_research_epoch": previous_epoch,
                        "next_research_epoch": config.RESEARCH_EPOCH,
                        "contract_id": getattr(
                            config, "L0_CONTRACT_VERSION", "legacy",
                        ),
                        "reason": invalidation_reason,
                    },
                )
                old_reason = row["status_reason"] or "未记录"
                reason = (
                    f"{invalidation_reason} 原状态={row['status']}；原原因={old_reason}"
                )
                c.execute(
                    """UPDATE factors
                          SET status='invalidated',status_reason=?,updated_at=?,
                              backtest_gate_passed=0,
                              backtest_epoch='legacy-invalidated',
                              oos_test_p_value=NULL,backtest_evidence=NULL,
                              evidence_path=NULL,evidence_sha256=NULL,
                              sealed_at=NULL,sealed_data_end=NULL,
                              sealed_recipe_sha256=NULL,
                              evaluator_sealed_at=NULL,
                              evaluator_sealed_data_end=NULL,
                              evaluator_fingerprint=NULL,
                              production_score_path=NULL,
                              production_score_sha256=NULL,
                              production_data_key=NULL,
                              production_as_of=NULL,
                              production_evaluator_sha256=NULL
                        WHERE factor_id=?""",
                    (reason, now, row["factor_id"]),
                )
                registry._append_factor_trial_snapshot(
                    c, row["factor_id"],
                    event="evaluator_contract_invalidated",
                    details={
                        "previous_research_epoch": previous_epoch,
                        "next_research_epoch": config.RESEARCH_EPOCH,
                        "contract_id": getattr(
                            config, "L0_CONTRACT_VERSION", "legacy",
                        ),
                        "reason": invalidation_reason,
                    },
                )
            # 旧 epoch 若在评估中断时留下到达顺序 latch，快照已经完整保留；
            # 新严格 epoch 不得被旧 pending 假设永久阻塞。
            c.execute("DELETE FROM test_observation_latches")
            invalidated_mining_batches = c.execute(
                "UPDATE mining_batches SET status='invalidated',completed_at=? "
                "WHERE status='pending'",
                (now,),
            ).rowcount
            c.commit()
            invalidated_factors = len(rows)
        finally:
            c.close()

        # 旧批次可能已经把历史确认 PASS 写入独立策略文件，甚至被手动解锁。
        # 只重置 batch_state 不足以 fail-closed：策略列表仍会把这些旧结论当可晋升。
        # 原 JSON 已在上方逐文件归档；线上文件保留规格和旧证据定位，但明确失效并锁回。
        invalidated_strategies = 0
        for path in strategy_paths:
            saved = _read_json(path)
            if not saved:
                continue
            if previous_epoch:
                if saved.get("research_epoch") != previous_epoch:
                    continue
            elif saved.get("research_epoch") == config.RESEARCH_EPOCH:
                continue
            previous = saved.get("promotion") if isinstance(
                saved.get("promotion"), dict) else {}
            promotion = dict(previous)
            promotion.update({
                "legacy_unified_verdict": previous.get("unified_verdict"),
                "legacy_gate_reason": previous.get("gate_reason"),
                "legacy_evidence_report": previous.get("evidence_report"),
                "unified_verdict": "INVALIDATED",
                "gate_reason": invalidation_reason,
                "unlocked": False,
                "unlocked_at": None,
                "invalidated_at": now,
                "invalidated_by_epoch": config.RESEARCH_EPOCH,
            })
            saved["promotion"] = promotion
            saved["recommendation_eligible"] = False
            _atomic_json(path, saved)
            invalidated_strategies += 1

        for name in ("strategy_batch_state.json", "strategy_batch_results.json"):
            path = config.PROJECT_ROOT / "research" / name
            old = _read_json(path)
            old_results = old.get("results") if isinstance(old.get("results"), list) else []
            results = []
            for row in old_results:
                item = dict(row) if isinstance(row, dict) else {"legacy_value": str(row)}
                item.update({
                    "passed": False,
                    "verdict": "INVALIDATED",
                    "strategy_id": None,
                    "reason": invalidation_reason,
                })
                results.append(item)
            _atomic_json(path, {
                "schema_version": 2,
                "research_epoch": config.RESEARCH_EPOCH,
                "running": False,
                "invalidated": True,
                "invalidated_at": now,
                "invalidated_reason": invalidation_reason,
                "archive_path": str(archive_dir.relative_to(config.PROJECT_ROOT)),
                "total": len(results),
                "done": len(results),
                "results": results,
            })

        for zone in ("mining", "backtest"):
            _atomic_json(config.STATE_DIR / f"{zone}.json", {
                "zone": zone,
                "status": "stopped",
                "phase": "epoch_reset",
                "detail": "旧证据已归档；等待严格 epoch 新批次",
                "research_epoch": config.RESEARCH_EPOCH,
                "queue_depth": 0,
                "throughput": 0,
                "success_rate": 0,
                "updated_at": now,
            })
        _atomic_json(config.STATE_DIR / "daemon.json", {
            "daemon": "stopped",
            "phase": "epoch_reset",
            "detail": "旧 daemon 已停止；等待新版验证后启动",
            "research_epoch": config.RESEARCH_EPOCH,
            "rounds": 0,
            "errors": 0,
            "novelty_today": 0,
            "updated_at": now,
        })
        _atomic_json(config.STATE_DIR / "daemon_runtime.json", {
            "research_epoch": config.RESEARCH_EPOCH,
            "started_at": "",
            "updated_at": now,
            "rounds": 0,
            "errors": 0,
            "last_source_audit": "",
            "last_backtest": "",
            "last_revalidate": "",
            "last_revalidate_data_key": "",
            "dry_rounds": 0,
        })

        manifest = {
            **snapshot_manifest,
            "status": "complete",
            "migrated_at": now,
            "invalidated_factors": invalidated_factors,
            "invalidated_strategies": invalidated_strategies,
            "invalidated_mining_batches": invalidated_mining_batches,
            "snapshot_verification": snapshot_verification,
            "recoverable": True,
        }
        _atomic_json(archive_dir / "manifest.json", manifest)
        try:
            verification = verify_archive(archive_dir)
        except Exception as exc:
            manifest.update({
                "status": "verification_failed",
                "recoverable": False,
                "verification_error": str(exc),
            })
            _atomic_json(archive_dir / "manifest.json", manifest)
            raise
        manifest["verification"] = verification
        _atomic_json(archive_dir / "manifest.json", manifest)
        return {**manifest, "archive_dir": str(archive_dir)}
    finally:
        daemon._release_daemon_lock(mutation_lock)
        daemon._release_daemon_lock(lock)


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="执行可恢复迁移")
    args = parser.parse_args()
    if not args.apply:
        parser.error("必须显式传 --apply；命令会备份并失效旧研究结论")
    print(json.dumps(migrate_strict_epoch(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
